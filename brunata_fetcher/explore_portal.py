#!/usr/bin/env python3
"""Interactive Brunata portal exploration tool.

Launches a non-headless Chromium, logs in with .env credentials, navigates to
the Verbrauch tab, then hands the browser to you. Drive it through whatever
variations you want to capture (year selector, Darstellung anpassen, …). Every
request + response goes into portal_network.jsonl in the system temp dir.

Press ENTER in the terminal to close the browser and finalize the log.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from _brunata_scraper import _CHROMIUM_ARGS, _DEBUG_DIR, _attach_network_logger
from run_scraper_once import _read_env_file


_LOGIN_URL = (
    "https://nutzerportal.brunata-muenchen.de/np_anmeldung/index.html?sap-language=DE"
)
_SEL_EMAIL = "#__component0---Start--idEmailInput-inner"
_SEL_PASSWORD = "#__component0---Start--idPassword-inner"
_SEL_LOGIN_BUTTON = 'button:has-text("Anmelden")'

_LOGGER = logging.getLogger("brunata_fetcher.explore")


async def _navigate_to_verbrauch(page) -> bool:
    for toggle_sel in (
        'button[aria-label*="Navigation"]',
        'button[title="Navigation"]',
    ):
        try:
            await page.click(toggle_sel, timeout=2000)
            await page.wait_for_timeout(500)
            _LOGGER.info("Opened nav drawer via %s", toggle_sel)
            break
        except Exception:
            continue

    for sel in (
        '[id$="menuTree-1-content"]',
        'li[role="treeitem"]:has-text("Verbrauch")',
        'div.bmeAppLauncherTreeContent:has-text("Verbrauch")',
        'text=Verbrauch',
    ):
        try:
            locator = page.locator(sel).first
            await locator.scroll_into_view_if_needed(timeout=2000)
            await locator.click(timeout=4000, force=True)
            _LOGGER.info("Clicked Verbrauch via %s", sel)
            return True
        except Exception:
            continue
    return False


async def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive Brunata portal explorer")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--no-verbrauch",
        action="store_true",
        help="Skip auto-navigation to the Verbrauch tab after login",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    env = _read_env_file(env_path)
    email = env.get("BRUNATA_EMAIL", "").strip()
    password = env.get("BRUNATA_PASSWORD", "").strip()
    if not email or not password:
        print("BRUNATA_EMAIL or BRUNATA_PASSWORD missing in .env", file=sys.stderr)
        sys.exit(2)

    from playwright.async_api import async_playwright

    log_path = os.path.join(_DEBUG_DIR, "portal_network.jsonl")
    _LOGGER.info("Network log: %s", log_path)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=_CHROMIUM_ARGS)
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            try:
                page = await context.new_page()
                page.set_default_timeout(30000)
                fh = _attach_network_logger(page, log_path)
                try:
                    _LOGGER.info("Logging in")
                    await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
                    await page.wait_for_selector(_SEL_EMAIL)
                    await page.fill(_SEL_EMAIL, email)
                    await page.fill(_SEL_PASSWORD, password)
                    await page.click(_SEL_LOGIN_BUTTON)
                    try:
                        await page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        pass
                    await page.wait_for_timeout(3000)
                    _LOGGER.info("Login complete")

                    if not args.no_verbrauch:
                        if await _navigate_to_verbrauch(page):
                            try:
                                await page.wait_for_load_state(
                                    "networkidle", timeout=15000
                                )
                            except Exception:
                                pass

                    print()
                    print("=" * 60)
                    print("Browser is yours. Click through the variations you want")
                    print("to capture (year, Darstellung anpassen, etc.).")
                    print(f"All traffic is being logged to {log_path}")
                    print("Press ENTER here to close and finalize the log.")
                    print("=" * 60)
                    await asyncio.to_thread(input, "> ")
                finally:
                    fh.close()
                    try:
                        await page.close()
                    except Exception:
                        pass
            finally:
                await context.close()
        finally:
            await browser.close()

    _LOGGER.info("Log saved: %s", log_path)


if __name__ == "__main__":
    asyncio.run(main())

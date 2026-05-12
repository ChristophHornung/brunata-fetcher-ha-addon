#!/usr/bin/env python3
"""Standalone Brunata portal scraper, invoked as a subprocess by HA.

Reads a JSON config from stdin, scrapes the Brunata portal using Playwright,
and writes the result as JSON to stdout.

Output on success:
    {"status": "ok", "data": {"Heizung": 2150.0, "last_update_date": "28.02.2026"}}
Output on error:
    {"status": "error", "type": "login"|"scraping"|"config", "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
from typing import TypedDict


_LOGGER = logging.getLogger("brunata_fetcher.scraper")
_DEBUG_DIR = tempfile.gettempdir()


# Chromium launch flags. Most are standard headless-server hygiene; the
# notable / less-obvious ones get an inline comment.
_CHROMIUM_ARGS: list[str] = [
    # Required in HA addon Docker containers
    "--no-sandbox",
    "--disable-dev-shm-usage",  # /dev/shm is small in containers; force /tmp
    # Headless-server quiet flags
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--mute-audio",
    "--disable-breakpad",
    "--disable-extensions",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-component-update",
    "--metrics-recording-only",
    "--no-first-run",
    "--safebrowsing-disable-auto-update",
    # Keep background pages responsive (headless Chromium throttles otherwise)
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    # Avoid keychain prompts on Linux hosts that don't have one
    "--password-store=basic",
    "--use-mock-keychain",
    # Brunata uses SAPUI5; site isolation has caused frame loss in the past
    "--disable-features=site-per-process",
    # Reduce surface area for the portal's bot detection
    "--disable-blink-features=AutomationControlled",
]


class ScraperConfig(TypedDict, total=False):
    """Shape of the JSON config passed to ``scrape``.

    All fields are declared optional at the type level (``total=False``);
    runtime validation in ``main`` enforces the required ones so missing
    keys surface as a ``config`` error instead of a generic ``scraping`` one.
    """

    # Required at runtime
    email: str
    password: str
    energy_types: list[str]
    login_url: str
    selector_email: str
    selector_password: str
    selector_login_button: str
    selector_date: str
    selector_value: str
    # Optional
    timeout_after_login: int
    timeout_between_clicks: int
    playwright_timeout: int
    headless: bool
    debug: bool
    energy_type_labels: dict[str, str]


_REQUIRED_CONFIG_KEYS: tuple[str, ...] = (
    "email",
    "password",
    "energy_types",
    "login_url",
    "selector_email",
    "selector_password",
    "selector_login_button",
    "selector_date",
    "selector_value",
)


def _parse_german_number(text: str) -> float:
    if not text:
        raise ValueError("Text is empty")
    normalized = re.sub(
        r"\s*(kWh|m\xb3|m\xb3\/h|Liter|L|l)\s*$", "", text, flags=re.IGNORECASE
    ).strip()
    as_number = normalized.replace(".", "").replace(",", ".")
    try:
        return float(as_number)
    except ValueError as ex:
        raise ValueError(f"Could not parse '{text}' as number") from ex


async def _dump_debug(
    page, name: str, *, html: bool = False, screenshot: bool = False
) -> None:
    """Write debug HTML and/or screenshot under the system temp dir. Best-effort."""
    try:
        if html:
            content = await page.content()
            with open(
                os.path.join(_DEBUG_DIR, f"{name}.html"), "w", encoding="utf-8"
            ) as fh:
                fh.write(content)
        if screenshot:
            await page.screenshot(path=os.path.join(_DEBUG_DIR, f"{name}.png"))
        _LOGGER.debug("Wrote debug artifact: %s", name)
    except Exception as ex:
        _LOGGER.warning("Failed to write debug artifact %s: %s", name, ex)


# Response content types worth logging (text-shaped only). Binary blobs like
# images/fonts are skipped to keep the JSONL log readable. SAP OData batch
# responses come back as multipart/mixed wrapping text parts, so include that.
_LOGGABLE_CT_PREFIXES = (
    "application/json",
    "application/xml",
    "application/javascript",
    "text/",
    "multipart/mixed",
)
_MAX_BODY_BYTES = 200_000


def _attach_network_logger(page, path: str):
    """Attach a response listener that appends each response to a JSONL file.

    Request bodies are intentionally NOT logged — the login POST contains the
    password. We capture the URL/method from the request side and the status
    + body from the response side.
    """
    fh = open(path, "w", encoding="utf-8")

    def _write(record: dict) -> None:
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
        except Exception as ex:
            _LOGGER.debug("Network log write failed: %s", ex)

    async def _on_response(response) -> None:
        try:
            request = response.request
            record: dict = {
                "ts": time.time(),
                "method": request.method,
                "url": response.url,
                "status": response.status,
                "resource_type": request.resource_type,
                "content_type": response.headers.get("content-type", ""),
            }
            # Capture the inner OData sub-requests carried in /$batch POST bodies
            # so we can see filter / select / unit parameters. Skip login POSTs
            # since their body contains the password.
            if request.method == "POST" and "/np_anmeldung/" not in request.url:
                try:
                    post_data = request.post_data
                except Exception:
                    post_data = None
                if post_data:
                    record["request_body"] = (
                        post_data[:_MAX_BODY_BYTES]
                        if len(post_data) > _MAX_BODY_BYTES
                        else post_data
                    )
            ct = record["content_type"].lower()
            if any(ct.startswith(prefix) for prefix in _LOGGABLE_CT_PREFIXES):
                try:
                    body = await response.body()
                except Exception as ex:
                    record["body_error"] = str(ex)
                else:
                    if len(body) > _MAX_BODY_BYTES:
                        record["body_truncated"] = True
                        body = body[:_MAX_BODY_BYTES]
                    record["body"] = body.decode("utf-8", errors="replace")
            _write(record)
        except Exception as ex:
            _LOGGER.debug("Network log handler error: %s", ex)

    page.on("response", lambda r: asyncio.create_task(_on_response(r)))
    return fh


async def scrape(config: ScraperConfig) -> dict[str, float | str | None]:
    from playwright.async_api import async_playwright

    start = time.monotonic()
    _LOGGER.info("Scraper entry")

    email = config["email"]
    password = config["password"]
    energy_types = config["energy_types"]
    login_url = config["login_url"]
    sel_email = config["selector_email"]
    sel_password = config["selector_password"]
    sel_login = config["selector_login_button"]
    sel_date = config["selector_date"]
    sel_value = config["selector_value"]
    timeout_after = config.get("timeout_after_login", 5000)
    timeout_clicks = config.get("timeout_between_clicks", 2000)
    pw_timeout = config.get("playwright_timeout", 30000)
    headless = config.get("headless", True)
    debug = config.get("debug", False)
    energy_type_labels = config.get("energy_type_labels", {})
    masked_email = f"***{email[-4:]}" if len(email) >= 4 else "***"
    _LOGGER.info(
        "Scraper config loaded: user=%s energy_types=%s headless=%s timeout_ms=%s debug=%s",
        masked_email,
        energy_types,
        headless,
        pw_timeout,
        debug,
    )

    async with async_playwright() as pw:
        _LOGGER.info("Playwright start")
        browser = await pw.chromium.launch(headless=headless, args=_CHROMIUM_ARGS)
        try:
            _LOGGER.info("Browser launched")
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            try:
                page = await context.new_page()
                page.set_default_timeout(pw_timeout)
                network_log_fh = None
                if debug:
                    network_log_path = os.path.join(_DEBUG_DIR, "portal_network.jsonl")
                    network_log_fh = _attach_network_logger(page, network_log_path)
                    _LOGGER.info("Network logger attached: %s", network_log_path)
                try:
                    consumption = await _do_scrape(
                        page=page,
                        login_url=login_url,
                        email=email,
                        password=password,
                        sel_email=sel_email,
                        sel_password=sel_password,
                        sel_login=sel_login,
                        sel_date=sel_date,
                        sel_value=sel_value,
                        energy_types=energy_types,
                        energy_type_labels=energy_type_labels,
                        timeout_after=timeout_after,
                        timeout_clicks=timeout_clicks,
                        debug=debug,
                    )
                finally:
                    _LOGGER.info("Scraper cleanup start")
                    await page.close()
                    if network_log_fh is not None:
                        try:
                            network_log_fh.close()
                        except Exception:
                            pass
            finally:
                await context.close()
        finally:
            await browser.close()
            _LOGGER.info("Scraper cleanup done")

    duration = time.monotonic() - start
    _LOGGER.info("Scraper exit success in %.2fs", duration)
    return consumption


async def _do_scrape(
    *,
    page,
    login_url: str,
    email: str,
    password: str,
    sel_email: str,
    sel_password: str,
    sel_login: str,
    sel_date: str,
    sel_value: str,
    energy_types: list[str],
    energy_type_labels: dict[str, str],
    timeout_after: int,
    timeout_clicks: int,
    debug: bool,
) -> dict[str, float | str | None]:
    _LOGGER.info("Open login page")
    await page.goto(login_url, wait_until="domcontentloaded")
    if debug:
        await _dump_debug(page, "portal_debug1", html=True)

    await page.wait_for_selector(sel_email)
    _LOGGER.info("Login page loaded and email selector found")
    if debug:
        await _dump_debug(page, "portal_debug2", html=True, screenshot=True)

    await page.fill(sel_email, email)
    await page.fill(sel_password, password)
    _LOGGER.info("Credentials filled")
    if debug:
        await _dump_debug(page, "portal_debug3", screenshot=True)

    await page.click(sel_login)
    _LOGGER.info("Login button clicked")
    try:
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        _LOGGER.warning("wait_for_load_state(domcontentloaded) timed out after login click")
    _LOGGER.info("Post-login wait complete")

    if debug:
        await _dump_debug(page, "portal_debug4", screenshot=True)

    # Login failure detection: only consider error words if we are still on
    # the login page. Otherwise dashboard text like "Fehlerprotokoll" trips
    # false positives.
    current_url = page.url.lower()
    still_on_login = "anmeldung" in current_url or "login" in current_url
    if still_on_login:
        page_text = (await page.text_content("body") or "").lower()
        if any(
            w in page_text
            for w in ("ungültig", "invalid", "fehler", "error", "incorrect")
        ):
            _LOGGER.error("Login failure detected via page text/url")
            raise RuntimeError("LOGIN_FAILED")
    _LOGGER.info("No immediate login failure detected")

    await page.wait_for_timeout(timeout_after)
    _LOGGER.info("After-login settle wait complete")

    if debug:
        await _dump_debug(page, "portal_debug5", screenshot=True)

    consumption: dict[str, float | str | None] = {"last_update_date": None}
    _LOGGER.info("Starting energy type extraction")

    for energy_type in energy_types:
        _LOGGER.info("Energy extraction start: %s", energy_type)
        label = energy_type_labels.get(energy_type, energy_type)
        clicked = False
        for btn_sel in (
            f'button:has-text("{energy_type}")',
            f'button:has-text("{label}")',
        ):
            try:
                _LOGGER.debug("Trying selector for %s: %s", energy_type, btn_sel)
                await page.wait_for_selector(btn_sel)
                await page.click(btn_sel)
                clicked = True
                _LOGGER.info(
                    "Selector click success for %s: %s", energy_type, btn_sel
                )
                break
            except Exception:
                _LOGGER.debug(
                    "Selector click failed for %s: %s",
                    energy_type,
                    btn_sel,
                    exc_info=True,
                )
                continue

        if not clicked:
            _LOGGER.warning("No selector matched for energy type: %s", energy_type)
            consumption[energy_type] = None
            continue

        await page.wait_for_selector(sel_date)
        # Give the SAPUI5 view time to swap in the new energy type's values
        # before reading the date and value fields.
        await page.wait_for_timeout(timeout_clicks)
        _LOGGER.info("Post-click wait complete for %s", energy_type)

        if consumption["last_update_date"] is None:
            raw_date = await page.text_content(sel_date)
            if raw_date:
                candidate = raw_date.strip()
                if candidate and candidate != "--":
                    consumption["last_update_date"] = candidate
                    _LOGGER.info("Detected last_update_date=%s", candidate)

        await page.wait_for_selector(sel_value)
        value_text = await page.text_content(sel_value)
        if not value_text:
            _LOGGER.warning("No value text found for %s", energy_type)
            consumption[energy_type] = None
            continue
        try:
            consumption[energy_type] = _parse_german_number(value_text.strip())
            _LOGGER.info(
                "Parsed %s value=%s", energy_type, consumption[energy_type]
            )
        except ValueError:
            _LOGGER.warning(
                "Failed to parse value for %s: %s", energy_type, value_text
            )
            consumption[energy_type] = None

    _LOGGER.info("Energy extraction finished")

    if debug:
        await _debug_visit_verbrauch(page, timeout_clicks)

    return consumption


async def _debug_visit_verbrauch(page, settle_ms: int) -> None:
    """Click the Verbrauch tab and let its API calls fire, for network capture."""
    _LOGGER.info("Debug: visiting Verbrauch tab")

    # Snapshot the dashboard before trying to navigate, so we can mine its DOM
    # for the right selector if our clicks miss.
    await _dump_debug(page, "portal_dashboard", html=True, screenshot=True)

    # The Verbrauch entry is a SAPUI5 tree-menu leaf inside the app launcher
    # navigation. It may sit inside a collapsed side menu, so try opening that
    # first if a likely toggle is present.
    for toggle_sel in (
        'button[aria-label*="Navigation"]',
        'button[aria-label*="Menü"]',
        'button[title="Navigation"]',
        '.sapMBtn[title*="Navigation"]',
    ):
        try:
            await page.click(toggle_sel, timeout=1500)
            _LOGGER.info("Debug: opened nav drawer via %s", toggle_sel)
            await page.wait_for_timeout(500)
            break
        except Exception:
            continue

    clicked = False
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
            _LOGGER.info("Debug: clicked Verbrauch via %s", sel)
            clicked = True
            break
        except Exception as ex:
            _LOGGER.debug("Debug: Verbrauch click miss on %s: %s", sel, ex)
            continue

    if not clicked:
        _LOGGER.warning("Debug: could not find Verbrauch nav element")
        await _dump_debug(page, "portal_verbrauch_miss", html=True, screenshot=True)
        return

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        _LOGGER.debug("Debug: networkidle wait timed out on Verbrauch", exc_info=True)
    await page.wait_for_timeout(max(settle_ms, 3000))
    await _dump_debug(page, "portal_verbrauch", html=True, screenshot=True)
    _LOGGER.info("Debug: Verbrauch settle complete")


def _validate_config(config: object) -> ScraperConfig:
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object")
    missing = [key for key in _REQUIRED_CONFIG_KEYS if key not in config]
    if missing:
        raise ValueError(f"Missing required config key(s): {', '.join(missing)}")
    return config  # type: ignore[return-value]


def main() -> None:
    try:
        raw_config = json.loads(sys.stdin.read())
    except Exception as ex:
        _LOGGER.exception("Config decode failed")
        print(json.dumps({"status": "error", "type": "config", "message": str(ex)}))
        sys.exit(1)

    try:
        config = _validate_config(raw_config)
    except ValueError as ex:
        _LOGGER.error("Config validation failed: %s", ex)
        print(json.dumps({"status": "error", "type": "config", "message": str(ex)}))
        sys.exit(1)

    try:
        result = asyncio.run(scrape(config))
        print(json.dumps({"status": "ok", "data": result}))
    except RuntimeError as ex:
        if "LOGIN_FAILED" in str(ex):
            _LOGGER.error("Scraper runtime login error")
            print(
                json.dumps(
                    {
                        "status": "error",
                        "type": "login",
                        "message": "Login failed: invalid credentials",
                    }
                )
            )
        else:
            _LOGGER.exception("Scraper runtime error")
            print(
                json.dumps({"status": "error", "type": "scraping", "message": str(ex)})
            )
        sys.exit(1)
    except Exception as ex:
        _LOGGER.exception("Unhandled scraper exception")
        print(json.dumps({"status": "error", "type": "scraping", "message": str(ex)}))
        sys.exit(1)


if __name__ == "__main__":
    main()

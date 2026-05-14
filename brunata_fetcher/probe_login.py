#!/usr/bin/env python3
"""Probe: capture what the SAPUI5 "Anmelden" click actually sends.

Goal: find out whether the browser login can be replaced with a plain
HTTP call (like the data fetches already are), or whether it needs JS
execution we can't replicate headlessly.

Drives the real login via Playwright but, instead of just using the
resulting cookies, it records every request/response from the moment
the page loads until the post-login navigation settles, then prints:

  * every POST (method, URL, headers, body) — the auth POST is in here
  * any request whose URL or body carries the email (the login call)
  * the response body of the NP_REG_LOGON_SRV_01 $batch call (success
    AND failure envelopes — run with --bad-password to see failure)
  * the redirect chain after the POST
  * which cookies exist in the context afterwards (the session anchor
    we'd need to reproduce)

The email and password VALUES are redacted in all output (raw,
URL-encoded, AND Base64 — the portal sends the password Base64-encoded).
Field names, endpoints, and structure are preserved.

Run from brunata_fetcher/ with credentials in .env.
  python probe_login.py                # successful login
  python probe_login.py --bad-password # capture the failure envelope
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import sys
from pathlib import Path
from urllib.parse import quote

from _brunata_api import (
    _CHROMIUM_ARGS,
    _LOGIN_URL,
    _SEL_EMAIL,
    _SEL_LOGIN_BUTTON,
    _SEL_PASSWORD,
)
from _env_utils import env_bool as _env_bool
from _env_utils import read_env_file as _read_env_file

_LOGIN_BATCH_MARKER = "NP_REG_LOGON_SRV_01"


def _make_redactor(email: str, secrets: list[str]):
    """Return a function that masks the email + every secret.

    For each secret, covers the raw value, URL-encoded forms, AND the
    Base64 form — the portal sends the password as ``base64(plaintext)``
    in the login body, so the raw-string match alone would leak it.
    """
    replacements: list[tuple[str, str]] = []
    for secret in secrets:
        if not secret:
            continue
        b64 = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        replacements.extend(
            [
                (secret, "***PASSWORD***"),
                (b64, "***PASSWORD-B64***"),
                (b64.rstrip("="), "***PASSWORD-B64***"),
                (quote(secret), "***PASSWORD-URLENC***"),
                (quote(secret, safe=""), "***PASSWORD-URLENC***"),
            ]
        )
    if email:
        replacements.extend(
            [
                (email, "***EMAIL***"),
                (quote(email), "***EMAIL-URLENC***"),
                (quote(email, safe=""), "***EMAIL-URLENC***"),
            ]
        )
    # Longest needles first so a substring match can't pre-empt a longer one.
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)

    def redact(text: str | None) -> str:
        if not text:
            return ""
        out = text
        for needle, mask in replacements:
            if needle:
                out = out.replace(needle, mask)
        return out

    return redact


async def _main_async(
    email: str,
    real_password: str,
    effective_password: str,
    headless: bool,
    timeout_ms: int,
) -> None:
    from playwright.async_api import async_playwright

    # Redact both the password actually sent and the real one, in case the
    # real password shows up anywhere (e.g. as a prefix inside the bad one).
    redact = _make_redactor(email, [effective_password, real_password])
    requests: list[dict] = []
    responses: list[dict] = []
    login_bodies: list[tuple[str, int, str]] = []
    body_reads: list = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, args=_CHROMIUM_ARGS)
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            page.set_default_timeout(timeout_ms)

            def on_request(request) -> None:
                try:
                    post_data = request.post_data
                except Exception:
                    post_data = None
                requests.append(
                    {
                        "method": request.method,
                        "url": request.url,
                        "resource_type": request.resource_type,
                        "headers": dict(request.headers),
                        "post_data": post_data,
                    }
                )

            def on_response(response) -> None:
                record = {
                    "status": response.status,
                    "url": response.url,
                    "headers": dict(response.headers),
                    "request_method": response.request.method,
                    "set_cookie_names": [],
                }
                responses.append(record)

                async def _enrich(resp=response, rec=record) -> None:
                    # all_headers() includes set-cookie, which the plain
                    # .headers dict hides (browser security).
                    try:
                        all_h = await resp.all_headers()
                    except Exception:  # pragma: no cover - diagnostic
                        all_h = {}
                    raw_sc = all_h.get("set-cookie", "")
                    for piece in raw_sc.split("\n"):
                        piece = piece.strip()
                        if "=" in piece:
                            rec["set_cookie_names"].append(
                                piece.split("=", 1)[0]
                            )

                body_reads.append(asyncio.ensure_future(_enrich()))

                if _LOGIN_BATCH_MARKER in response.url and "$batch" in response.url:

                    async def _read(resp=response) -> None:
                        try:
                            txt = await resp.text()
                        except Exception as ex:  # pragma: no cover - diagnostic
                            txt = f"<could not read body: {ex}>"
                        login_bodies.append((resp.url, resp.status, txt))

                    body_reads.append(asyncio.ensure_future(_read()))

            page.on("request", on_request)
            page.on("response", on_response)

            print(f"Navigating to {_LOGIN_URL}")
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            pre_login_count = len(requests)
            print(f"Login page settled. {pre_login_count} requests so far.")

            await page.wait_for_selector(_SEL_EMAIL)
            await page.fill(_SEL_EMAIL, email)
            await page.fill(_SEL_PASSWORD, effective_password)
            print("Filled credentials, clicking Anmelden...")
            await page.click(_SEL_LOGIN_BUTTON)

            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            if body_reads:
                await asyncio.gather(*body_reads, return_exceptions=True)

            cookies = await context.cookies()
            final_url = page.url
            await page.close()
            await context.close()
        finally:
            await browser.close()

    print()
    print("=" * 80)
    print(f"TOTAL: {len(requests)} requests, {len(responses)} responses")
    print(f"Requests during page load (pre-click): {pre_login_count}")
    print(f"Requests from click onward: {len(requests) - pre_login_count}")
    print(f"Final URL after login: {redact(final_url)}")
    print("=" * 80)

    # 1. The NP_REG_LOGON_SRV_01 $batch response body — success vs failure.
    print()
    print("### LOGIN $batch RESPONSE BODY " + "#" * 49)
    if not login_bodies:
        print("(no NP_REG_LOGON_SRV_01 $batch response captured)")
    for url, status, body in login_bodies:
        print(f"\nHTTP {status}  {redact(url)}")
        print("-" * 80)
        print(redact(body))
        print("-" * 80)

    # 2. Every POST — the auth call is one of these.
    print()
    print("### ALL POST REQUESTS " + "#" * 58)
    posts = [r for r in requests if r["method"] == "POST"]
    if not posts:
        print("(none)")
    for i, r in enumerate(posts):
        print(f"\n--- POST #{i} ---")
        print(f"URL:           {redact(r['url'])}")
        print(f"resource_type: {r['resource_type']}")
        interesting_headers = {
            k: v
            for k, v in r["headers"].items()
            if k.lower()
            in (
                "content-type",
                "x-csrf-token",
                "x-requested-with",
                "authorization",
                "accept",
                "origin",
                "referer",
            )
        }
        print(f"Headers:       {interesting_headers}")
        body = r["post_data"]
        if body:
            print(f"Body:\n{redact(body)}")
        else:
            print("Body:          (empty / not captured)")

    # 3. Redirect chain (3xx) — shows the post-auth navigation (or lack of).
    print()
    print("### REDIRECTS (3xx) " + "#" * 60)
    for r in responses:
        if 300 <= r["status"] < 400:
            loc = r["headers"].get("location", "(no location header)")
            print(f"{r['status']}  {redact(r['url'])}\n      -> {redact(loc)}")

    # 3b. Full request/response sequence from the click onward, flagging
    #     any response that sets a cookie — pinpoints which call actually
    #     establishes the SAP session (MYSAPSSO2 etc.).
    print()
    print("### REQUEST SEQUENCE FROM CLICK ONWARD " + "#" * 41)
    for i, r in enumerate(requests):
        if i < pre_login_count:
            continue
        # Match the response by index isn't reliable; find by url+method.
        flag = ""
        for resp in responses:
            if (
                resp["url"] == r["url"]
                and resp["request_method"] == r["method"]
                and resp["set_cookie_names"]
            ):
                flag = f"   <== SETS COOKIES: {resp['set_cookie_names']}"
                break
        print(f"  {r['method']:<5} {redact(r['url'])}{flag}")

    # 3c. Full headers of the requests that hit the bare /np_dienste path —
    #     that's the call that issues MYSAPSSO2, so whatever makes SAP
    #     authenticate it must be in here.
    print()
    print("### /np_dienste REQUEST HEADERS " + "#" * 48)
    for i, r in enumerate(requests):
        path = r["url"].split("?")[0]
        if not path.endswith("/np_dienste"):
            continue
        print(f"\n--- request #{i}: {r['method']} {redact(r['url'])} ---")
        for k, v in sorted(r["headers"].items()):
            print(f"  {k}: {redact(v)}")
        if r["post_data"]:
            print(f"  [body] {redact(r['post_data'])}")

    # 4. Final cookies — what we'd need to reproduce on success.
    print()
    print("### CONTEXT COOKIES AFTER LOGIN " + "#" * 48)
    if not cookies:
        print("(none — expected for a failed login)")
    for c in cookies:
        print(
            f"  {c['name']:<32} domain={c['domain']:<40} "
            f"httpOnly={c.get('httpOnly')} secure={c.get('secure')} "
            f"value_len={len(c.get('value', ''))}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bad-password",
        action="store_true",
        help="Append garbage to the password to capture the login-failure envelope",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    env_path = Path.cwd() / ".env"
    env = {**os.environ, **_read_env_file(env_path)}
    email = env.get("BRUNATA_EMAIL", "").strip()
    password = env.get("BRUNATA_PASSWORD", "").strip()
    if not email or not password:
        print("Missing BRUNATA_EMAIL or BRUNATA_PASSWORD", file=sys.stderr)
        sys.exit(2)
    headless = _env_bool(env.get("BRUNATA_HEADLESS", "true"), True)
    timeout_ms = int(env.get("BRUNATA_PLAYWRIGHT_TIMEOUT_MS", "60000"))

    effective_password = password
    if args.bad_password:
        effective_password = password + "_WRONGPW_PROBE"
        print("Running with a deliberately WRONG password to capture failure.")

    asyncio.run(
        _main_async(email, password, effective_password, headless, timeout_ms)
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Probe how Bis / Datum parameters shape NP_UVI_SRV/CumuConsumptionMonSet.

Tries a handful of Bis values (current YTD, end-of-current-month, today,
historical month-ends) and also tries narrowing to a single month via a
``Datum eq`` filter. Prints what each probe returns so we can decide whether
month-granular fetches (and current-month preliminary values) are reachable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from _brunata_api import (
    _CHROMIUM_ARGS,
    _LOGIN_URL,
    _SAP_CLIENT,
    _SEL_EMAIL,
    _SEL_LOGIN_BUTTON,
    _SEL_PASSWORD,
    _UVI_BASE,
    _build_uvi_inner_get,
    _login,
    _odata_batch_get,
    _odata_filter,
    _discover_user_context,
    _parse_sap_date,
    _results,
    _to_float,
)
from run_scraper_once import _read_env_file


# Each probe is a list of $filter clauses appended to (Nutzein, Kotyp, …).
def _probes(today_iso: str) -> list[tuple[str, list[str]]]:
    today = today_iso  # e.g. "2026-05-12"
    year = today[:4]
    return [
        (
            "Bis = last available month-end (2026-04-30)",
            ["Bis eq datetime'2026-04-30T00:00:00'"],
        ),
        (
            "Bis = end of current month (2026-05-31) — partial?",
            ["Bis eq datetime'2026-05-31T00:00:00'"],
        ),
        (
            f"Bis = today ({today}) — preliminary?",
            [f"Bis eq datetime'{today}T00:00:00'"],
        ),
        (
            "Bis = 2026-03-31 (past month-end)",
            ["Bis eq datetime'2026-03-31T00:00:00'"],
        ),
        (
            "Bis = 2026-01-31 (very early in year)",
            ["Bis eq datetime'2026-01-31T00:00:00'"],
        ),
        (
            "Bis = full prev year-end (2025-12-31)",
            ["Bis eq datetime'2025-12-31T00:00:00'"],
        ),
        (
            "Bis = 2026-04-30 + Datum filter to March only",
            [
                "Bis eq datetime'2026-04-30T00:00:00'",
                "Datum eq datetime'2026-03-31T00:00:00'",
            ],
        ),
        (
            "Bis = 2026-04-30 + Datum filter to April only",
            [
                "Bis eq datetime'2026-04-30T00:00:00'",
                "Datum eq datetime'2026-04-30T00:00:00'",
            ],
        ),
        (
            "Bis = 2026-04-30 + Datum filter to May (no data yet)",
            [
                "Bis eq datetime'2026-04-30T00:00:00'",
                "Datum eq datetime'2026-05-31T00:00:00'",
            ],
        ),
        (
            "Bis = 2026-05-31 + Datum filter to May (preliminary?)",
            [
                "Bis eq datetime'2026-05-31T00:00:00'",
                "Datum eq datetime'2026-05-31T00:00:00'",
            ],
        ),
    ]


def _summary(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    parts = []
    for row in rows:
        date = _parse_sap_date(row.get("Datum")) or "?"
        v = _to_float(row.get("Verbrauch"))
        m = row.get("MassreadTxt", "")
        parts.append(f"{date}={v} {m}".rstrip())
    return ", ".join(parts)


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)

    env = _read_env_file(Path(".env"))
    email = env.get("BRUNATA_EMAIL", "").strip()
    password = env.get("BRUNATA_PASSWORD", "").strip()
    if not email or not password:
        print("Missing BRUNATA_EMAIL/BRUNATA_PASSWORD in .env", file=sys.stderr)
        sys.exit(2)

    from datetime import date as _date

    today_iso = _date.today().isoformat()

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(30000)
            await _login(page, email, password)
            await page.close()

            nutzein, partner = await _discover_user_context(context.request)
            print(
                f"Logged in. Nutzein={nutzein} Partner={partner} today={today_iso}"
            )
            print()

            for label, extra_clauses in _probes(today_iso):
                clauses = [
                    f"Nutzein eq '{nutzein}'",
                    *extra_clauses,
                    "Kotyp eq 'HZ01'",
                    "InKwh eq true",
                    "IsWeatherAdjusted eq false",
                ]
                inner = _build_uvi_inner_get("CumuConsumptionMonSet", clauses)
                try:
                    payloads = await _odata_batch_get(
                        context.request,
                        _UVI_BASE,
                        [inner],
                        user_unit_id=nutzein,
                        contact_person=partner,
                    )
                except Exception as ex:
                    print(f"  {label}")
                    print(f"    -> error: {ex}")
                    print()
                    continue
                if not payloads:
                    print(f"  {label}")
                    print("    -> no payload")
                    print()
                    continue
                payload = payloads[0]
                # Surface OData error responses cleanly.
                err = payload.get("error") if isinstance(payload, dict) else None
                if err:
                    err_msg = ""
                    try:
                        err_msg = err["message"]["value"]
                    except Exception:
                        err_msg = str(err)
                    print(f"  {label}")
                    print(f"    -> server error: {err_msg}")
                    print()
                    continue
                rows = _results(payload)
                print(f"  {label}")
                print(f"    rows={len(rows)}  {_summary(rows)}")
                print()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

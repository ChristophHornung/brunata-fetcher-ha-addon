#!/usr/bin/env python3
"""Probe: dump every field of the portal's OData rows.

We currently parse only Datum + Verbrauch out of CumuConsumptionMonSet.
SAP OData entities usually carry far more — this dumps the full row
structure for CumuConsumptionMonSet, DatesSet, and UserContextSet so we
can see whether the portal exposes a "data as of" / last-computed
timestamp we could read directly instead of inferring refresh cadence
from value jumps.

Run from brunata_fetcher/ with credentials in .env.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from _brunata_api import (
    _APPLAUNCHER_BASE,
    _CHROMIUM_ARGS,
    _SAP_CLIENT,
    _UVI_BASE,
    _build_uvi_inner_get,
    _discover_user_context,
    _discover_period,
    _login,
    _odata_batch_get,
    _odata_get,
    _results,
)
from run_scraper_once import _env_bool, _read_env_file


def _dump_rows(label: str, rows: list[dict]) -> None:
    print()
    print("=" * 78)
    print(f"{label}: {len(rows)} row(s)")
    print("=" * 78)
    if not rows:
        return
    # Field inventory from the first row.
    print("Fields on row[0]:")
    for key in sorted(rows[0].keys()):
        print(f"  - {key}")
    print()
    # Full dump of the first and last row (the rest are usually same shape).
    for tag, row in (("FIRST", rows[0]), ("LAST", rows[-1])):
        print(f"--- {tag} ROW (full) ---")
        print(json.dumps(row, ensure_ascii=False, indent=2, default=str))
        print()


async def _main_async(email: str, password: str, headless: bool, timeout_ms: int) -> None:
    from playwright.async_api import async_playwright

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
            try:
                page = await context.new_page()
                page.set_default_timeout(timeout_ms)
                try:
                    await _login(page, email, password)
                finally:
                    await page.close()
                request = context.request

                # UserContextSet — plain GET.
                ctx_payload = await _odata_get(
                    request,
                    f"{_APPLAUNCHER_BASE}/UserContextSet?sap-client={_SAP_CLIENT}",
                )
                _dump_rows("UserContextSet", _results(ctx_payload))

                nutzein, partner = await _discover_user_context(request)
                bis_literal, official_iso = await _discover_period(
                    request, nutzein, partner
                )
                print(f"\nquery Bis literal: {bis_literal}")
                print(f"official last update (unbumped Bisdatum): {official_iso}")

                # DatesSet — full rows.
                from urllib.parse import quote

                filter_qs = quote(
                    f"Nutzein eq '{nutzein}' and IsCalendar eq true", safe=""
                )
                dates_inner = (
                    f"DatesSet?sap-client={_SAP_CLIENT}"
                    f"&$expand=Units&$filter={filter_qs}"
                )
                # CumuConsumptionMonSet — full rows for Heizung, current period.
                mon_inner = _build_uvi_inner_get(
                    "CumuConsumptionMonSet",
                    [
                        f"Nutzein eq '{nutzein}'",
                        f"Bis eq {bis_literal}",
                        "Kotyp eq 'HZ01'",
                        "InKwh eq true",
                        "IsWeatherAdjusted eq false",
                    ],
                )
                payloads = await _odata_batch_get(
                    request,
                    _UVI_BASE,
                    [dates_inner, mon_inner],
                    user_unit_id=nutzein,
                    contact_person=partner,
                )
                _dump_rows("DatesSet", _results(payloads[0]))

                # All 12 monthly rows — focus on Vbkz / status per month.
                mon_rows = _results(payloads[1])
                print()
                print("=" * 78)
                print(f"CumuConsumptionMonSet (HZ01): all {len(mon_rows)} rows")
                print("=" * 78)
                print(f"{'Datum':<12}{'Verbrauch':>12}{'Vbkz':>8}{'Massread':>12}")
                for row in mon_rows:
                    from _brunata_api import _parse_sap_date

                    datum = _parse_sap_date(row.get("Datum")) or "?"
                    verb = row.get("Verbrauch")
                    vbkz = row.get("Vbkz")
                    mr = row.get("Massread")
                    print(
                        f"{datum:<12}{str(verb):>12}{str(vbkz)!r:>8}{str(mr):>12}"
                    )

                # $metadata — enumerate every entity set + property to spot
                # any status/freshness-related set we haven't discovered.
                meta_resp = await request.get(
                    f"{_UVI_BASE}/$metadata?sap-client={_SAP_CLIENT}",
                    headers={"Accept": "application/xml"},
                )
                meta_xml = await meta_resp.text()
                print()
                print("=" * 78)
                print(f"NP_UVI_SRV $metadata ({len(meta_xml)} chars)")
                print("=" * 78)
                import re as _re

                entity_sets = _re.findall(r'<EntitySet Name="([^"]+)"', meta_xml)
                print(f"EntitySets ({len(entity_sets)}):")
                for name in entity_sets:
                    print(f"  - {name}")
                # Property names that hint at freshness / status / timestamps.
                interesting = sorted(
                    set(
                        _re.findall(
                            r'<Property Name="([^"]*'
                            r'(?:[Dd]at|[Tt]ime|[Ss]tand|[Aa]ktual|'
                            r'[Ss]tatus|[Uu]pdate|[Vv]bkz|[Ff]lag)[^"]*)"',
                            meta_xml,
                        )
                    )
                )
                print(f"\nFreshness/status-ish property names ({len(interesting)}):")
                for name in interesting:
                    print(f"  - {name}")
            finally:
                await context.close()
        finally:
            await browser.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
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
    asyncio.run(_main_async(email, password, headless, timeout_ms))


if __name__ == "__main__":
    main()

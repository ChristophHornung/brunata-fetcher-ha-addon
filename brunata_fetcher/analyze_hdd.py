#!/usr/bin/env python3
"""HDD-based weather-normalization analysis for the portal's heating data.

Logs in via Playwright, pulls every available year's monthly Verbrauch
through the same path as the backfill, then joins that with daily mean
temperatures from Open-Meteo's free historical archive to derive
``kWh / Heating Degree Day`` (German G20/15 convention) per month.

If the heating system + occupancy are stable, ``kWh/HDD`` should be
roughly flat across years — drift indicates a real behavioural change,
not just weather. A useful sanity check against the portal's own
``IsWeatherAdjusted`` (witterungsbereinigt) numbers.

Usage::

    python analyze_hdd.py --lat 52.52 --lon 13.41

Credentials come from ``.env`` (same file as ``run_api_once.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

from _brunata_api import _CHROMIUM_ARGS, _discover_user_context, _login
from _brunata_backfill import _fetch_history
from run_scraper_once import _env_bool, _read_env_file


def fetch_daily_temps(lat: float, lon: float, start: date, end: date) -> dict[date, float]:
    """Return ``{date: daily_mean_temp_C}`` from Open-Meteo's archive API."""
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
        "&daily=temperature_2m_mean&timezone=Europe%2FBerlin"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    dates = payload["daily"]["time"]
    temps = payload["daily"]["temperature_2m_mean"]
    return {date.fromisoformat(d): t for d, t in zip(dates, temps) if t is not None}


def monthly_hdd(temps: dict[date, float]) -> dict[str, float]:
    """G20/15 HDD aggregated per ``YYYY-MM``.

    German residential convention: reference temperature 20 °C, heating
    threshold 15 °C. ``HDD_day = max(0, 20 - T_mean)`` if ``T_mean < 15``
    else 0.
    """
    result: dict[str, float] = {}
    for d, t in temps.items():
        if t >= 15.0:
            continue
        hdd = 20.0 - t
        key = d.strftime("%Y-%m")
        result[key] = result.get(key, 0.0) + hdd
    return result


async def fetch_heizung_history(
    email: str, password: str, headless: bool, timeout_ms: int
) -> tuple[dict[str, float], dict[str, float]]:
    """Return ``(raw_by_month, wb_by_month)`` keyed by ``YYYY-MM``."""
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
                nutzein, partner = await _discover_user_context(context.request)
                years_data = await _fetch_history(
                    context.request, nutzein, partner, ["Heizung"]
                )
            finally:
                await context.close()
        finally:
            await browser.close()

    raw: dict[str, float] = {}
    wb: dict[str, float] = {}
    for year_data in years_data:
        for datum, value in year_data["monthly"].get("Heizung", []):
            raw[datum[:7]] = value
        for datum, value in year_data["monthly_wb"].get("Heizung", []):
            wb[datum[:7]] = value
    return raw, wb


def _print_report(
    raw: dict[str, float],
    wb: dict[str, float],
    hdd_monthly: dict[str, float],
    reference_hdd_year: float,
) -> None:
    print()
    print("=" * 90)
    print(
        f"{'Month':<10}{'Raw kWh':>11}{'WB kWh':>11}{'HDD (G20/15)':>15}"
        f"{'kWh/HDD':>11}{'WB/HDD':>11}{'norm@300':>14}"
    )
    print("=" * 90)

    years: dict[int, dict[str, float]] = {}
    for month_key in sorted(set(raw) | set(hdd_monthly)):
        year = int(month_key[:4])
        raw_v = raw.get(month_key)
        wb_v = wb.get(month_key)
        hdd = hdd_monthly.get(month_key, 0.0)
        if raw_v is None and hdd == 0:
            continue
        kwh_per_hdd = (raw_v / hdd) if (raw_v is not None and hdd > 0) else None
        wb_per_hdd = (wb_v / hdd) if (wb_v is not None and hdd > 0) else None
        norm = (kwh_per_hdd * 300) if kwh_per_hdd is not None else None

        years.setdefault(year, {"raw_kwh": 0.0, "wb_kwh": 0.0, "hdd": 0.0})
        if raw_v is not None:
            years[year]["raw_kwh"] += raw_v
        if wb_v is not None:
            years[year]["wb_kwh"] += wb_v
        years[year]["hdd"] += hdd

        raw_s = f"{raw_v:>11.1f}" if raw_v is not None else f"{'-':>11}"
        wb_s = f"{wb_v:>11.1f}" if wb_v is not None else f"{'-':>11}"
        hdd_s = f"{hdd:>15.1f}"
        kwhhdd_s = (
            f"{kwh_per_hdd:>11.2f}" if kwh_per_hdd is not None else f"{'-':>11}"
        )
        wbhdd_s = f"{wb_per_hdd:>11.2f}" if wb_per_hdd is not None else f"{'-':>11}"
        norm_s = f"{norm:>14.0f}" if norm is not None else f"{'-':>14}"
        print(f"{month_key:<10}{raw_s}{wb_s}{hdd_s}{kwhhdd_s}{wbhdd_s}{norm_s}")

    print("=" * 90)
    print()
    print("Yearly summary")
    print("-" * 72)
    print(
        f"{'Year':<6}{'Raw kWh':>10}{'WB kWh':>10}{'HDD':>10}"
        f"{'kWh/HDD':>10}{'WB/HDD':>10}{'norm@' + str(int(reference_hdd_year)):>14}"
    )
    for year in sorted(years):
        v = years[year]
        if v["hdd"] <= 0:
            continue
        kwh_per_hdd = v["raw_kwh"] / v["hdd"]
        wb_per_hdd = v["wb_kwh"] / v["hdd"] if v["wb_kwh"] else 0.0
        normalized = kwh_per_hdd * reference_hdd_year
        print(
            f"{year:<6}{v['raw_kwh']:>10.0f}{v['wb_kwh']:>10.0f}{v['hdd']:>10.0f}"
            f"{kwh_per_hdd:>10.2f}{wb_per_hdd:>10.2f}{normalized:>14.0f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lat", type=float, required=True, help="Latitude (e.g. 52.52 for Berlin Mitte)"
    )
    parser.add_argument(
        "--lon", type=float, required=True, help="Longitude (e.g. 13.41 for Berlin Mitte)"
    )
    parser.add_argument(
        "--env-file", default=".env", help="Path to credentials env file"
    )
    parser.add_argument(
        "--start",
        default="2023-01-01",
        help="Earliest date to include in HDD pull (default: 2023-01-01)",
    )
    parser.add_argument(
        "--reference-hdd",
        type=float,
        default=3500.0,
        help="Reference annual HDD for the normalized projection (default: 3500)",
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
    env_values = _read_env_file(env_path)
    env = {**os.environ, **env_values}
    email = env.get("BRUNATA_EMAIL", "").strip()
    password = env.get("BRUNATA_PASSWORD", "").strip()
    if not email or not password:
        print("Missing BRUNATA_EMAIL or BRUNATA_PASSWORD", file=sys.stderr)
        sys.exit(2)
    headless = _env_bool(env.get("BRUNATA_HEADLESS", "true"), True)
    timeout_ms = int(env.get("BRUNATA_PLAYWRIGHT_TIMEOUT_MS", "60000"))

    print(
        f"Fetching Heizung history from portal and daily mean temps for "
        f"({args.lat}, {args.lon}) starting {args.start}..."
    )
    raw, wb = asyncio.run(fetch_heizung_history(email, password, headless, timeout_ms))
    temps = fetch_daily_temps(args.lat, args.lon, date.fromisoformat(args.start), date.today())
    hdd_monthly = monthly_hdd(temps)
    _print_report(raw, wb, hdd_monthly, args.reference_hdd)


if __name__ == "__main__":
    main()

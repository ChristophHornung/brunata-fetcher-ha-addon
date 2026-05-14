#!/usr/bin/env python3
"""Dump monthly raw vs. weather-adjusted Heizung values for all years.

Reuses the backfill module's _fetch_history so the output mirrors exactly
what the addon imports as history.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from _brunata_api import _USER_AGENT, _discover_user_context, _login_http
from _brunata_backfill import _fetch_history
from _env_utils import read_env_file


async def _main_async(email: str, password: str, timeout_s: float) -> None:
    import httpx

    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT},
        timeout=httpx.Timeout(timeout_s),
        follow_redirects=True,
    ) as client:
        await _login_http(client, email, password)
        nutzein, partner = await _discover_user_context(client)
        years_data = await _fetch_history(
            client, nutzein, partner, ["Heizung", "Kaltwasser", "Warmwasser"]
        )

    print()
    print("=" * 78)
    print(f"{'Year-Month':<12}{'Raw (kWh)':>14}{'WB (kWh)':>14}{'WB/Raw':>10}{'Delta':>14}")
    print("=" * 78)
    for year_data in years_data:
        year = year_data["year"]
        raw = dict(year_data["monthly"].get("Heizung", []))
        wb = dict(year_data["monthly_wb"].get("Heizung", []))
        all_months = sorted(set(raw) | set(wb))
        for datum in all_months:
            raw_val = raw.get(datum)
            wb_val = wb.get(datum)
            if raw_val is None and wb_val is None:
                continue
            month_label = datum[:7]
            raw_s = f"{raw_val:>14.1f}" if raw_val is not None else f"{'—':>14}"
            wb_s = f"{wb_val:>14.1f}" if wb_val is not None else f"{'—':>14}"
            if raw_val and wb_val and raw_val != 0:
                ratio = wb_val / raw_val
                ratio_s = f"{ratio:>9.1%}"
                delta_s = f"{wb_val - raw_val:>+14.1f}"
            else:
                ratio_s = f"{'—':>10}"
                delta_s = f"{'—':>14}"
            print(f"{month_label:<12}{raw_s}{wb_s}{ratio_s}{delta_s}")
        print("-" * 78)
        print(f"{year} totals: raw={sum(raw.values()):.1f}  wb={sum(wb.values()):.1f}")
        print("-" * 78)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path

    env_values = read_env_file(env_path)
    env = {**os.environ, **env_values}
    email = env.get("BRUNATA_EMAIL", "").strip()
    password = env.get("BRUNATA_PASSWORD", "").strip()
    if not email or not password:
        print("Missing BRUNATA_EMAIL or BRUNATA_PASSWORD", file=sys.stderr)
        sys.exit(2)
    timeout_s = float(env.get("BRUNATA_HTTP_TIMEOUT_S", "60"))

    asyncio.run(_main_async(email, password, timeout_s))


if __name__ == "__main__":
    main()

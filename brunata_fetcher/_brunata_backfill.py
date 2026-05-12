#!/usr/bin/env python3
"""One-shot historical backfill for HA long-term statistics.

The Brunata portal exposes monthly totals back to 2023 via
``NP_UVI_SRV/CumuConsumptionMonSet``. This module:

1. Logs in via Playwright (same flow as ``_brunata_api.fetch``).
2. Lists available years from ``DatesSet`` and fetches every year's
   monthly Verbrauch (per cost type) and per-room Anteil shares.
3. Interpolates each month linearly across its days so the HA Energy
   Dashboard's daily / weekly views look smooth instead of one tall
   spike on each month-end.
4. Pushes per-day statistic rows to HA via the Supervisor REST proxy:
   ``POST /core/api/services/recorder/import_statistics``. Existing
   stats at the same timestamps are replaced, so re-running is safe.

Triggered by an MQTT command published by the user (see ``server.py``).
Skips the in-progress current month so the live polling owns it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from _brunata_api import (
    _CHROMIUM_ARGS,
    _ENERGY_TYPE_KOTYP,
    _ROOM_LABELS,
    _SAP_CLIENT,
    _UVI_BASE,
    _build_uvi_inner_get,
    _discover_user_context,
    _login,
    _odata_batch_get,
    _parse_sap_date,
    _results,
    _to_float,
)


_LOGGER = logging.getLogger("brunata_fetcher.backfill")


# Map energy_type label to the HA entity id we publish via Discovery.
_ENTITY_ID_FOR_ENERGY_TYPE: dict[str, tuple[str, str]] = {
    "Heizung": ("sensor.brunata_fetcher_heizung", "kWh"),
    "Kaltwasser": ("sensor.brunata_fetcher_kaltwasser", "m³"),
    "Warmwasser": ("sensor.brunata_fetcher_warmwasser", "kWh"),
}


def _slug_for_room(name: str) -> str:
    """Mirror server.py's slug helper. Kept local to avoid a server.py import."""
    import re

    folded = (
        name.lower()
        .replace("ü", "ue")
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ß", "ss")
    )
    return re.sub(r"[^a-z0-9]+", "_", folded).strip("_")


# --- Fetch ------------------------------------------------------------------


async def _fetch_history(
    request, nutzein: str, partner: str, energy_types: list[str]
) -> list[dict]:
    """Fetch every year's monthly + per-room data from NP_UVI_SRV."""
    from urllib.parse import quote

    filter_qs = quote(
        f"Nutzein eq '{nutzein}' and IsCalendar eq true", safe=""
    )
    dates_inner = (
        f"DatesSet?sap-client={_SAP_CLIENT}"
        f"&$expand=Units"
        f"&$filter={filter_qs}"
    )
    dates_payloads = await _odata_batch_get(
        request,
        _UVI_BASE,
        [dates_inner],
        user_unit_id=nutzein,
        contact_person=partner,
    )
    dates_rows = _results(dates_payloads[0])
    if not dates_rows:
        raise RuntimeError("DatesSet returned no rows")

    years: list[dict[str, str]] = []
    for row in dates_rows:
        abdatum = _parse_sap_date(row.get("Abdatum"))
        bisdatum = _parse_sap_date(row.get("Bisdatum"))
        if abdatum and bisdatum:
            years.append({"abdatum": abdatum, "bisdatum": bisdatum})
    years.sort(key=lambda y: y["abdatum"])
    _LOGGER.info("Backfill: discovered %d historical years", len(years))

    results: list[dict] = []
    for year_info in years:
        bisdatum = year_info["bisdatum"]
        bis_literal = f"datetime'{bisdatum}T00:00:00'"
        year = int(year_info["abdatum"][:4])

        inner_gets: list[str] = []
        index_map: list[tuple[str, str]] = []
        for energy_type in energy_types:
            kotyp_unit = _ENERGY_TYPE_KOTYP.get(energy_type)
            if kotyp_unit is None:
                continue
            kotyp, in_kwh = kotyp_unit
            in_kwh_lit = "true" if in_kwh else "false"
            inner_gets.append(
                _build_uvi_inner_get(
                    "CumuConsumptionMonSet",
                    [
                        f"Nutzein eq '{nutzein}'",
                        f"Bis eq {bis_literal}",
                        f"Kotyp eq '{kotyp}'",
                        f"InKwh eq {in_kwh_lit}",
                        "IsWeatherAdjusted eq false",
                    ],
                )
            )
            index_map.append(("monthly", energy_type))

        inner_gets.append(
            _build_uvi_inner_get(
                "CumuConsumptionRoomSet",
                [f"Nutzein eq '{nutzein}'", f"Bis eq {bis_literal}"],
            )
        )
        index_map.append(("rooms", ""))

        payloads = await _odata_batch_get(
            request,
            _UVI_BASE,
            inner_gets,
            user_unit_id=nutzein,
            contact_person=partner,
        )

        year_data: dict[str, Any] = {
            "year": year,
            "bisdatum": bisdatum,
            "monthly": {},
            "rooms": [],
        }
        for (kind, energy_type), payload in zip(index_map, payloads):
            if kind == "monthly":
                monthly_rows: list[tuple[str, float]] = []
                for row in _results(payload):
                    datum = _parse_sap_date(row.get("Datum"))
                    verbrauch = _to_float(row.get("Verbrauch"))
                    if datum and verbrauch is not None:
                        monthly_rows.append((datum, verbrauch))
                year_data["monthly"][energy_type] = sorted(monthly_rows)
            elif kind == "rooms":
                for row in _results(payload):
                    raum = str(row.get("Raum") or "").strip()
                    anteil = _to_float(row.get("Anteil"))
                    if raum and anteil is not None:
                        year_data["rooms"].append(
                            {
                                "Raum": raum,
                                "RaumTxt": row.get("RaumTxt")
                                or _ROOM_LABELS.get(raum, raum),
                                "Anteil": anteil,
                            }
                        )
        results.append(year_data)
        _LOGGER.info(
            "Backfill: year %d fetched (%d energy types, %d rooms, Bis=%s)",
            year,
            len(year_data["monthly"]),
            len(year_data["rooms"]),
            bisdatum,
        )
    return results


# --- Stats generation -------------------------------------------------------


def _month_bounds(month_end_iso: str) -> tuple[date, date, int]:
    month_end = date.fromisoformat(month_end_iso)
    month_start = month_end.replace(day=1)
    days = (month_end - month_start).days + 1
    return month_start, month_end, days


def _day_start_utc(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)


def _emit_daily_stats(
    monthly_rows: list[tuple[str, float]],
    *,
    starting_sum: float,
    cutoff: date | None,
) -> tuple[list[dict], float]:
    """Linearly distribute each month's value across its days.

    Returns ``(stats, final_running_sum)``. ``stats`` is a list of dicts
    suitable for ``recorder.import_statistics``. ``state`` resets at the
    start of each iteration (caller resets it per-year if needed); ``sum``
    grows monotonically from ``starting_sum``.

    Days at or after ``cutoff`` are skipped — that's where live polling
    will own the data and we don't want to overwrite it.
    """
    stats: list[dict] = []
    ytd_state = 0.0
    running_sum = starting_sum

    for datum_iso, monthly_value in monthly_rows:
        month_start, _, days_in_month = _month_bounds(datum_iso)
        daily_value = monthly_value / days_in_month
        for day_offset in range(days_in_month):
            day = month_start + timedelta(days=day_offset)
            if cutoff is not None and day >= cutoff:
                # Stop before the live-data window starts.
                return stats, running_sum
            stats.append(
                {
                    "start": _day_start_utc(day).isoformat(),
                    "state": round(ytd_state, 3),
                    "sum": round(running_sum, 3),
                }
            )
            ytd_state += daily_value
            running_sum += daily_value
    return stats, running_sum


def _stats_for_energy_type(
    years_data: list[dict], energy_type: str, cutoff: date | None
) -> list[dict]:
    """Build the full daily-stats list across all years for one cost type."""
    all_stats: list[dict] = []
    running_sum = 0.0
    for year_data in years_data:
        monthly_rows = year_data["monthly"].get(energy_type, [])
        if not monthly_rows:
            continue
        year_stats, running_sum = _emit_daily_stats(
            monthly_rows, starting_sum=running_sum, cutoff=cutoff
        )
        all_stats.extend(year_stats)
        if cutoff is not None and year_data["year"] == cutoff.year:
            # We stopped mid-year at the cutoff; no later years to process.
            break
    return all_stats


def _stats_for_room(
    years_data: list[dict], room_label: str, cutoff: date | None
) -> list[dict]:
    """Per-room heating daily stats: (Anteil/100) × monthly heating spread daily."""
    all_stats: list[dict] = []
    running_sum = 0.0
    for year_data in years_data:
        # Find this year's Anteil for the room. The room may not exist in
        # every year (renovations, sub-meter changes).
        anteil: float | None = None
        for room in year_data["rooms"]:
            if room["RaumTxt"] == room_label:
                anteil = float(room["Anteil"])
                break
        if anteil is None:
            continue
        monthly_rows = year_data["monthly"].get("Heizung", [])
        if not monthly_rows:
            continue
        # Scale each month's heating value by the room's share.
        scaled_rows = [(d, v * anteil / 100.0) for d, v in monthly_rows]
        year_stats, running_sum = _emit_daily_stats(
            scaled_rows, starting_sum=running_sum, cutoff=cutoff
        )
        all_stats.extend(year_stats)
        if cutoff is not None and year_data["year"] == cutoff.year:
            break
    return all_stats


def _all_room_labels(years_data: list[dict]) -> list[str]:
    """Union of all room labels seen across years (oldest year first)."""
    seen: list[str] = []
    for year_data in years_data:
        for room in year_data["rooms"]:
            label = room["RaumTxt"]
            if label not in seen:
                seen.append(label)
    return seen


# --- HA push ----------------------------------------------------------------


def _push_statistics(
    supervisor_token: str,
    statistic_id: str,
    unit: str,
    name: str,
    stats: list[dict],
) -> None:
    """Call HA's ``recorder.import_statistics`` service via supervisor proxy."""
    if not stats:
        _LOGGER.info("No stats to push for %s; skipping", statistic_id)
        return
    payload = {
        "statistic_id": statistic_id,
        "source": "recorder",
        "name": name,
        "unit_of_measurement": unit,
        "has_mean": False,
        "has_sum": True,
        "stats": stats,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        "http://supervisor/core/api/services/recorder/import_statistics",
        data=data,
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=60) as response:
            response.read()
    except urlerror.HTTPError as ex:
        body = ""
        try:
            body = ex.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            body = "<unavailable>"
        raise RuntimeError(
            f"import_statistics HTTP {ex.code} for {statistic_id}: {body[:300]}"
        ) from ex
    except (urlerror.URLError, TimeoutError) as ex:
        raise RuntimeError(
            f"import_statistics network error for {statistic_id}: {ex}"
        ) from ex
    _LOGGER.info("Imported %d daily stats for %s", len(stats), statistic_id)


# --- Public entry point -----------------------------------------------------


async def backfill_history(
    *,
    supervisor_token: str,
    email: str,
    password: str,
    energy_types: list[str],
    headless: bool = True,
    playwright_timeout: int = 60000,
) -> None:
    """End-to-end backfill driven by the values configured for the live fetch."""
    from playwright.async_api import async_playwright

    start = time.monotonic()
    _LOGGER.info("Backfill: starting, energy_types=%s", energy_types)

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
                page.set_default_timeout(playwright_timeout)
                try:
                    await _login(page, email, password)
                finally:
                    await page.close()
                _LOGGER.info("Backfill: login complete; fetching history")
                nutzein, partner = await _discover_user_context(context.request)
                years_data = await _fetch_history(
                    context.request, nutzein, partner, energy_types
                )
            finally:
                await context.close()
        finally:
            await browser.close()

    # The live-polling path owns the current calendar month. Cut backfill at
    # the first day of it so we don't overwrite the addon's real daily samples.
    today = date.today()
    cutoff = today.replace(day=1)
    _LOGGER.info("Backfill: cutoff = %s (start of current month)", cutoff)

    # Global per-energy-type sensors.
    for energy_type in energy_types:
        entity_unit = _ENTITY_ID_FOR_ENERGY_TYPE.get(energy_type)
        if entity_unit is None:
            _LOGGER.warning("Unknown energy_type for backfill: %s", energy_type)
            continue
        statistic_id, unit = entity_unit
        stats = _stats_for_energy_type(years_data, energy_type, cutoff)
        _push_statistics(
            supervisor_token, statistic_id, unit, name=energy_type, stats=stats
        )

    # Per-room heating sensors. We backfill every room that has ever appeared
    # in the history. If a room currently doesn't exist (renamed, removed),
    # its sensor in HA may not yet exist — HA will still accept the import
    # and create the row; once Discovery republishes the entity it will be
    # linked back up.
    if "Heizung" in energy_types:
        for room_label in _all_room_labels(years_data):
            slug = _slug_for_room(room_label)
            if not slug:
                continue
            statistic_id = f"sensor.brunata_fetcher_heizung_{slug}"
            stats = _stats_for_room(years_data, room_label, cutoff)
            _push_statistics(
                supervisor_token,
                statistic_id,
                "kWh",
                name=f"Heizung {room_label}",
                stats=stats,
            )

    duration = time.monotonic() - start
    _LOGGER.info("Backfill: done in %.1fs", duration)

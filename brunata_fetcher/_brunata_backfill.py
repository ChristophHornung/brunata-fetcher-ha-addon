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

from _brunata_api import (
    _ENERGY_TYPE_KOTYP,
    _ROOM_LABELS,
    _SAP_CLIENT,
    _USER_AGENT,
    _UVI_BASE,
    _build_uvi_inner_get,
    _discover_user_context,
    _login_http,
    _odata_batch_get,
    _parse_sap_date,
    _results,
    _to_float,
)


_LOGGER = logging.getLogger("brunata_fetcher.backfill")


# Signal raised by server.py after every successful live fetch's state publish.
# The seam-bridge task waits on this so it adjusts against a short-term row
# that already carries the latest portal value. Without that wait, the bridge
# can run against ``state=0`` on a fresh install and the next live poll then
# creates a delta spike equal to the YTD value.
_LIVE_FETCH_EVENT: asyncio.Event | None = None


def _get_live_fetch_event() -> asyncio.Event:
    """Lazy-create the live-fetch event so module import doesn't need a loop."""
    global _LIVE_FETCH_EVENT
    if _LIVE_FETCH_EVENT is None:
        _LIVE_FETCH_EVENT = asyncio.Event()
    return _LIVE_FETCH_EVENT


def signal_live_fetch_completed() -> None:
    """Mark that the live fetcher just published a fresh state to MQTT.

    Called by server.py after each successful publish_state cycle. The
    seam-bridge waits on this signal before adjusting so HA's short-term
    table has had a chance to ingest the latest portal value.
    """
    _get_live_fetch_event().set()


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
    client, nutzein: str, partner: str, energy_types: list[str]
) -> list[dict]:
    """Fetch every year's monthly + per-room data from NP_UVI_SRV.

    ``client`` is an ``httpx.AsyncClient`` carrying the SAP session cookies.
    """
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
        client,
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

    today = date.today()
    results: list[dict] = []
    for year_info in years:
        year = int(year_info["abdatum"][:4])
        unbumped_bisdatum = year_info["bisdatum"]
        bisdatum = unbumped_bisdatum
        # For the current year, override Bisdatum from "last completed month-end"
        # to "end of current month" so the API also returns the preliminary
        # in-progress month. This is the same trick the live fetch uses, and
        # it shrinks the gap between the last backfilled day and the first
        # live sample down to hours — important because HA's stats compile
        # falls back to sum=0 when it can't find a recent baseline.
        if year == today.year:
            if today.month == 12:
                month_end = date(today.year, 12, 31)
            else:
                month_end = date(today.year, today.month + 1, 1) - timedelta(days=1)
            bisdatum = month_end.isoformat()
        bis_literal = f"datetime'{bisdatum}T00:00:00'"
        # Weather-adjusted queries don't accept the bumped Bis — the portal
        # only computes WB across closed months, so we keep the original
        # DatesSet-published Bisdatum for those.
        wb_bis_literal = f"datetime'{unbumped_bisdatum}T00:00:00'"

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

        # Weather-adjusted Heizung — separate stream of history that powers
        # the "Heizung (witterungsbereinigt)" sensors. Uses the unbumped Bis
        # because WB only exists for closed months.
        if "Heizung" in energy_types:
            kotyp, in_kwh = _ENERGY_TYPE_KOTYP["Heizung"]
            in_kwh_lit = "true" if in_kwh else "false"
            inner_gets.append(
                _build_uvi_inner_get(
                    "CumuConsumptionMonSet",
                    [
                        f"Nutzein eq '{nutzein}'",
                        f"Bis eq {wb_bis_literal}",
                        f"Kotyp eq '{kotyp}'",
                        f"InKwh eq {in_kwh_lit}",
                        "IsWeatherAdjusted eq true",
                    ],
                )
            )
            index_map.append(("monthly_wb", "Heizung"))

        inner_gets.append(
            _build_uvi_inner_get(
                "CumuConsumptionRoomSet",
                [f"Nutzein eq '{nutzein}'", f"Bis eq {bis_literal}"],
            )
        )
        index_map.append(("rooms", ""))

        payloads = await _odata_batch_get(
            client,
            _UVI_BASE,
            inner_gets,
            user_unit_id=nutzein,
            contact_person=partner,
        )

        year_data: dict[str, Any] = {
            "year": year,
            "bisdatum": bisdatum,
            "monthly": {},
            "monthly_wb": {},
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
            elif kind == "monthly_wb":
                wb_rows: list[tuple[str, float]] = []
                for row in _results(payload):
                    datum = _parse_sap_date(row.get("Datum"))
                    verbrauch = _to_float(row.get("Verbrauch"))
                    if datum and verbrauch is not None:
                        wb_rows.append((datum, verbrauch))
                year_data["monthly_wb"][energy_type] = sorted(wb_rows)
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

    For the in-progress current month, the monthly value is the portal's
    preliminary cumulative-so-far reading. We divide it across the days
    that have actually elapsed (today inclusive), not the calendar length
    of the month — otherwise the daily values would be too low and there'd
    be a fake "missing consumption" jump when live polling first lands.

    Days at or after ``cutoff`` are skipped.
    """
    today = date.today()
    stats: list[dict] = []
    ytd_state = 0.0
    running_sum = starting_sum

    for datum_iso, monthly_value in monthly_rows:
        month_start, month_end, days_in_month = _month_bounds(datum_iso)
        if month_start <= today <= month_end:
            # Current in-progress month: distribute over days elapsed so far.
            denom = max(1, (today - month_start).days + 1)
        else:
            denom = days_in_month
        daily_value = monthly_value / denom
        for day_offset in range(days_in_month):
            day = month_start + timedelta(days=day_offset)
            if cutoff is not None and day >= cutoff:
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
    years_data: list[dict],
    energy_type: str,
    cutoff: date | None,
    *,
    monthly_key: str = "monthly",
) -> list[dict]:
    """Build the full daily-stats list across all years for one cost type.

    ``monthly_key`` selects the per-year data source: ``"monthly"`` for the
    raw consumption (default), ``"monthly_wb"`` for the weather-adjusted
    Heizung stream.
    """
    all_stats: list[dict] = []
    running_sum = 0.0
    for year_data in years_data:
        monthly_rows = year_data.get(monthly_key, {}).get(energy_type, [])
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
    years_data: list[dict],
    room_label: str,
    cutoff: date | None,
    *,
    monthly_key: str = "monthly",
) -> list[dict]:
    """Per-room heating daily stats: (Anteil/100) × monthly heating spread daily.

    ``monthly_key`` selects the heating source month series. Use the default
    for raw heating, ``"monthly_wb"`` for the weather-adjusted variant — the
    per-room percentages are identical either way; only the heating total
    that gets scaled differs.
    """
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
        monthly_rows = year_data.get(monthly_key, {}).get("Heizung", [])
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


_WS_URI = "ws://supervisor/core/api/websocket"


async def _ws_auth(ws, supervisor_token: str) -> None:
    """Walk through HA's auth_required / auth handshake."""
    msg = json.loads(await ws.recv())
    if msg.get("type") != "auth_required":
        raise RuntimeError(f"Expected auth_required from HA, got: {msg}")
    await ws.send(
        json.dumps({"type": "auth", "access_token": supervisor_token})
    )
    msg = json.loads(await ws.recv())
    if msg.get("type") != "auth_ok":
        raise RuntimeError(f"HA WebSocket auth failed: {msg}")


async def _clear_existing_statistics(
    supervisor_token: str, statistic_ids: list[str]
) -> None:
    """Clear all existing statistics for the given statistic_ids.

    The live-polling path generates its own statistics for these entities
    starting from sum=0 (HA's default initialization). If we backfill without
    clearing first, HA ends up with two disjoint "histories" for the same
    entity — the backfilled one with a cumulative sum, and the live one
    rebased to zero. The Energy Dashboard / Statistics Graph then computes
    the bridge bucket as ``small_live_sum - huge_backfill_sum``, producing
    a huge negative bar.

    Clearing the existing stats lets HA's compiler recompute from the
    backfilled baseline once new short-term samples come in.
    """
    if not statistic_ids:
        return
    import websockets

    async with websockets.connect(_WS_URI, max_size=2**24) as ws:
        await _ws_auth(ws, supervisor_token)
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "recorder/clear_statistics",
                    "statistic_ids": statistic_ids,
                }
            )
        )
        msg = json.loads(await ws.recv())
    if not msg.get("success"):
        raise RuntimeError(f"recorder/clear_statistics failed: {msg}")
    _LOGGER.info(
        "Cleared existing statistics for %d entities: %s",
        len(statistic_ids),
        statistic_ids,
    )


async def _fetch_unique_id_to_entity_id(supervisor_token: str) -> dict[str, str]:
    """Return ``{unique_id: current_entity_id}`` for this addon's entities.

    The MQTT Discovery payloads carry stable ``unique_id`` values
    (``brunata_fetcher_heizung_kinderzimmer`` etc.), but the user may have
    renamed the entity_id in HA's UI afterwards. Statistics need to be
    imported under the **current** entity_id so the renamed live entity owns
    its history. We query HA's entity registry via the WebSocket API and
    build the unique_id → entity_id map for our addon's entries.
    """
    import websockets

    async with websockets.connect(_WS_URI, max_size=2**24) as ws:
        await _ws_auth(ws, supervisor_token)
        await ws.send(
            json.dumps({"id": 1, "type": "config/entity_registry/list"})
        )
        msg = json.loads(await ws.recv())

    if not msg.get("success"):
        raise RuntimeError(f"entity_registry/list failed: {msg}")
    mapping: dict[str, str] = {}
    for entry in msg.get("result", []):
        unique_id = entry.get("unique_id") or ""
        entity_id = entry.get("entity_id") or ""
        if unique_id.startswith("brunata_fetcher_") and entity_id:
            mapping[unique_id] = entity_id
    return mapping


def _resolve_statistic_id(
    unique_id_map: dict[str, str], unique_id: str, fallback: str
) -> str:
    """Look up the current entity_id for ``unique_id``; fall back if missing.

    Falls back to the original sensor.brunata_fetcher_… name so a missing
    registry entry (e.g. entity disabled in HA) doesn't break the backfill.
    """
    resolved = unique_id_map.get(unique_id)
    if resolved and resolved != fallback:
        _LOGGER.info(
            "Backfill: resolved %s -> %s via entity_registry (renamed)",
            unique_id,
            resolved,
        )
    return resolved or fallback


async def _adjust_sum_statistics(
    supervisor_token: str,
    statistic_id: str,
    start_time_iso: str,
    adjustment: float,
    unit_of_measurement: str | None,
) -> None:
    """Add ``adjustment`` to ``sum`` on all stats from ``start_time_iso`` forward.

    This is the same WebSocket command HA's "Adjust a statistic" UI uses.
    It walks both ``statistics`` and ``statistics_short_term`` and adds the
    delta to the ``sum`` column, leaving ``state`` alone. We use it after
    backfill to bridge the seam between the imported history and the live
    short-term rows HA generates with ``sum=0`` baseline.
    """
    import websockets

    async with websockets.connect(_WS_URI, max_size=2**24) as ws:
        await _ws_auth(ws, supervisor_token)
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "recorder/adjust_sum_statistics",
                    "statistic_id": statistic_id,
                    "start_time": start_time_iso,
                    "adjustment": adjustment,
                    "adjustment_unit_of_measurement": unit_of_measurement,
                }
            )
        )
        msg = json.loads(await ws.recv())
    if not msg.get("success"):
        raise RuntimeError(
            f"recorder/adjust_sum_statistics rejected {statistic_id}: {msg}"
        )
    _LOGGER.info(
        "Adjusted %s sum by %+.3f %s from %s",
        statistic_id,
        adjustment,
        unit_of_measurement or "",
        start_time_iso,
    )


async def _query_statistics_during_period(
    supervisor_token: str, statistic_id: str, start_time_iso: str
) -> dict | None:
    """Return the latest stat row's `{state, sum, ...}` since ``start_time_iso``.

    Used to detect whether HA has already chained sum correctly off our
    backfill (no adjustment needed) or is still stuck at `sum=0` (adjust).
    """
    import websockets

    async with websockets.connect(_WS_URI, max_size=2**24) as ws:
        await _ws_auth(ws, supervisor_token)
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "recorder/statistics_during_period",
                    "start_time": start_time_iso,
                    "statistic_ids": [statistic_id],
                    "period": "5minute",
                    "types": ["state", "sum"],
                }
            )
        )
        msg = json.loads(await ws.recv())
    if not msg.get("success"):
        return None
    rows = (msg.get("result") or {}).get(statistic_id) or []
    return rows[-1] if rows else None


async def _push_statistics(
    supervisor_token: str,
    statistic_id: str,
    unit: str,
    name: str,
    stats: list[dict],
    ws_message_id: int = 1,
) -> None:
    """Push daily stats via HA's recorder/import_statistics WebSocket command.

    HA's recorder uses a WebSocket API for stats imports — it isn't exposed
    as a regular service call. We talk to it through the Supervisor's
    WebSocket proxy at ws://supervisor/core/api/websocket using the addon's
    SUPERVISOR_TOKEN.
    """
    if not stats:
        _LOGGER.info("No stats to push for %s; skipping", statistic_id)
        return
    import websockets

    metadata = {
        "has_mean": False,
        "has_sum": True,
        "name": name,
        "source": "recorder",
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }

    try:
        async with websockets.connect(_WS_URI, max_size=2**24) as ws:
            await _ws_auth(ws, supervisor_token)
            await ws.send(
                json.dumps(
                    {
                        "id": ws_message_id,
                        "type": "recorder/import_statistics",
                        "metadata": metadata,
                        "stats": stats,
                    }
                )
            )
            result = json.loads(await ws.recv())
    except Exception as ex:
        raise RuntimeError(
            f"recorder/import_statistics websocket call failed for "
            f"{statistic_id}: {ex}"
        ) from ex

    if not result.get("success"):
        raise RuntimeError(
            f"recorder/import_statistics rejected {statistic_id}: {result}"
        )
    _LOGGER.info("Imported %d daily stats for %s", len(stats), statistic_id)


# --- Public entry point -----------------------------------------------------


async def backfill_history(
    *,
    supervisor_token: str,
    email: str,
    password: str,
    energy_types: list[str],
    http_timeout: float = 60.0,
) -> None:
    """End-to-end backfill driven by the values configured for the live fetch."""
    import httpx

    start = time.monotonic()
    _LOGGER.info("Backfill: starting, energy_types=%s", energy_types)

    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT},
        timeout=httpx.Timeout(http_timeout),
        follow_redirects=True,
    ) as client:
        await _login_http(client, email, password)
        _LOGGER.info("Backfill: login complete; fetching history")
        nutzein, partner = await _discover_user_context(client)
        years_data = await _fetch_history(client, nutzein, partner, energy_types)

    # Resolve unique_id -> current entity_id from HA's entity registry, so
    # we import stats to renamed entities under their new name. Live-polling
    # already respects renames because MQTT tracks unique_id; backfill needs
    # to do the same lookup explicitly.
    try:
        unique_id_map = await _fetch_unique_id_to_entity_id(supervisor_token)
        _LOGGER.info(
            "Backfill: entity registry resolved %d brunata_fetcher entries",
            len(unique_id_map),
        )
    except Exception as ex:
        _LOGGER.warning(
            "Backfill: entity_registry lookup failed (%s); "
            "falling back to default entity_ids",
            ex,
        )
        unique_id_map = {}

    # Backfill through today inclusive — including a daily interpolation
    # of the in-progress month from the portal's preliminary value. This
    # leaves no time gap for HA's stats compile to "lose" the baseline
    # when the next live sample lands.
    today = date.today()
    cutoff = today + timedelta(days=1)
    _LOGGER.info("Backfill: cutoff = %s (today inclusive)", cutoff)

    # Build the full list of (statistic_id, unit, name, stats) tuples first.
    # We resolve all the IDs up front so we can clear them all in one
    # WebSocket call before importing — otherwise live-polling stats with
    # a from-zero sum baseline would coexist with our backfilled cumulative
    # sums and HA would render a giant negative bar at the seam.
    plan: list[tuple[str, str, str, list[dict]]] = []
    for energy_type in energy_types:
        entity_unit = _ENTITY_ID_FOR_ENERGY_TYPE.get(energy_type)
        if entity_unit is None:
            _LOGGER.warning("Unknown energy_type for backfill: %s", energy_type)
            continue
        default_statistic_id, unit = entity_unit
        unique_id = default_statistic_id.removeprefix("sensor.")
        statistic_id = _resolve_statistic_id(
            unique_id_map, unique_id, default_statistic_id
        )
        plan.append(
            (
                statistic_id,
                unit,
                energy_type,
                _stats_for_energy_type(years_data, energy_type, cutoff),
            )
        )

    # Weather-adjusted Heizung total (one extra entity on the BRUdirekt device).
    if "Heizung" in energy_types:
        unique_id = "brunata_fetcher_heizung_wb"
        default_statistic_id = "sensor.brunata_fetcher_heizung_wb"
        statistic_id = _resolve_statistic_id(
            unique_id_map, unique_id, default_statistic_id
        )
        plan.append(
            (
                statistic_id,
                "kWh",
                "Heizung (witterungsbereinigt)",
                _stats_for_energy_type(
                    years_data, "Heizung", cutoff, monthly_key="monthly_wb"
                ),
            )
        )

    if "Heizung" in energy_types:
        for room_label in _all_room_labels(years_data):
            slug = _slug_for_room(room_label)
            if not slug:
                continue
            unique_id = f"brunata_fetcher_heizung_{slug}"
            default_statistic_id = f"sensor.brunata_fetcher_heizung_{slug}"
            statistic_id = _resolve_statistic_id(
                unique_id_map, unique_id, default_statistic_id
            )
            plan.append(
                (
                    statistic_id,
                    "kWh",
                    f"Heizung {room_label}",
                    _stats_for_room(years_data, room_label, cutoff),
                )
            )

    # Weather-adjusted per-room heating (one extra entity per Heizkostenverteiler).
    if "Heizung" in energy_types:
        for room_label in _all_room_labels(years_data):
            slug = _slug_for_room(room_label)
            if not slug:
                continue
            unique_id = f"brunata_fetcher_heizung_{slug}_wb"
            default_statistic_id = f"sensor.brunata_fetcher_heizung_{slug}_wb"
            statistic_id = _resolve_statistic_id(
                unique_id_map, unique_id, default_statistic_id
            )
            plan.append(
                (
                    statistic_id,
                    "kWh",
                    f"Heizung {room_label} (witterungsbereinigt)",
                    _stats_for_room(
                        years_data, room_label, cutoff, monthly_key="monthly_wb"
                    ),
                )
            )

    statistic_ids_to_clear = [
        statistic_id for statistic_id, _, _, stats in plan if stats
    ]
    try:
        await _clear_existing_statistics(supervisor_token, statistic_ids_to_clear)
    except Exception as ex:
        _LOGGER.warning(
            "Backfill: clear_statistics failed (%s); the imported stats will"
            " coexist with any existing live-baseline stats and may produce a"
            " transient discontinuity bar",
            ex,
        )

    # Capture the seam timestamp BEFORE the import — HA will start generating
    # bad ``sum=0`` short-term rows shortly after this moment, and the bridge
    # task needs that timestamp as the ``start_time`` for adjust_sum_statistics.
    seam_iso = datetime.now(timezone.utc).isoformat()

    ws_id = 1
    expected_anchors: dict[str, tuple[float, float, str]] = {}
    for statistic_id, unit, name, stats in plan:
        await _push_statistics(
            supervisor_token,
            statistic_id,
            unit,
            name=name,
            stats=stats,
            ws_message_id=ws_id,
        )
        ws_id += 1
        # Track the final imported row's (state, sum) so the bridge task can
        # compute the right adjustment without re-reading the import.
        if stats:
            last = stats[-1]
            expected_anchors[statistic_id] = (
                float(last["state"]),
                float(last["sum"]),
                unit,
            )

    duration = time.monotonic() - start
    _LOGGER.info("Backfill: done in %.1fs", duration)

    # Spawn the seam-bridge task in the background. We can't bridge synchronously
    # because HA hasn't yet generated the bad sum=0 rows — that happens after
    # the next hourly compile fires AND after the next live fetch publishes
    # a fresh state.
    if expected_anchors:
        # Reset the event so the bridge specifically waits for a fetch that
        # happens AFTER this point — any live fetch from before backfill is
        # irrelevant.
        _get_live_fetch_event().clear()
        asyncio.create_task(
            _bridge_seam_after_delay(
                supervisor_token, expected_anchors, seam_iso
            )
        )
        _LOGGER.info(
            "Backfill: seam-bridge task scheduled for %d entities",
            len(expected_anchors),
        )


async def _bridge_seam_after_delay(
    supervisor_token: str,
    expected_anchors: dict[str, tuple[float, float, str]],
    seam_iso: str,
    *,
    max_attempts: int = 3,
    retry_interval_s: int = 3600,
    live_fetch_timeout_s: int = 90000,
) -> None:
    """Wait for HA's hourly compile + a fresh live fetch, then bridge the seam.

    HA's compile uses ``statistics_short_term`` (which we cleared) as the
    baseline for new long-term rows. Without a fresh baseline, every new
    short-term row gets ``sum=0`` and that propagates into hourly long-term
    rows too. We can't seed short-term ourselves (no public API), but we
    can wait for HA to produce rows and then apply
    ``adjust_sum_statistics`` to shift them up to the correct sum.

    Two timing constraints matter:

    1. HA's long-term compile fires on each UTC hour boundary, so we wait
       until ``next-hour-boundary + 5 min`` for the first attempt, ensuring
       at least one bad hourly row exists to be corrected.
    2. ``adjust_sum_statistics`` only shifts ``sum``, not ``state``. If the
       latest short-term row still has the pre-backfill state (or no state
       at all on a fresh install), the next live fetch will publish a state
       jump and HA's compile will treat the delta as new consumption,
       producing a spike on top of our adjustment. So we also wait for the
       next live fetch to land + a 6-minute compile buffer before adjusting,
       guaranteeing short-term carries the latest portal value.

    If a given entity still has no live stats after both waits, we retry up
    to ``max_attempts`` times spaced ``retry_interval_s`` apart.

    Idempotent: re-running with already-corrected data computes
    ``adjustment ≈ 0`` and we skip.
    """
    now = datetime.now(timezone.utc)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    first_wake = next_hour + timedelta(minutes=5)
    # Minimum 6 minutes so a backfill triggered seconds before a UTC hour
    # boundary still gives HA's compile time to actually run.
    initial_delay = max((first_wake - now).total_seconds(), 360.0)
    _LOGGER.info(
        "Bridge: sleeping %.0f s (until %s) for HA's first hourly compile after seam",
        initial_delay,
        first_wake.isoformat(),
    )
    await asyncio.sleep(initial_delay)

    # Wait for the live fetcher to publish a fresh state since the seam, so
    # short-term carries the latest portal value. Then give HA's 5-minute
    # compile a 6-minute buffer to ingest that state before we adjust.
    event = _get_live_fetch_event()
    if event.is_set():
        _LOGGER.info(
            "Bridge: live fetch already happened since seam; proceeding to adjust"
        )
    else:
        _LOGGER.info(
            "Bridge: waiting up to %d s for the next live fetch before adjusting",
            live_fetch_timeout_s,
        )
        try:
            await asyncio.wait_for(event.wait(), timeout=live_fetch_timeout_s)
            _LOGGER.info(
                "Bridge: live fetch detected; pausing 6 min for HA to compile "
                "the published state into short-term"
            )
            await asyncio.sleep(360)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Bridge: no live fetch within %d s; proceeding with stale "
                "short-term — re-trigger the backfill after a live fetch if "
                "the dashboard still shows a discontinuity",
                live_fetch_timeout_s,
            )

    pending: dict[str, tuple[float, float, str]] = dict(expected_anchors)
    for attempt in range(1, max_attempts + 1):
        if not pending:
            break
        _LOGGER.info(
            "Bridge: attempt %d/%d for %d entities",
            attempt,
            max_attempts,
            len(pending),
        )
        still_pending: dict[str, tuple[float, float, str]] = {}
        for statistic_id, (expected_state, expected_sum, unit) in pending.items():
            try:
                latest = await _query_statistics_during_period(
                    supervisor_token, statistic_id, seam_iso
                )
            except Exception as ex:
                _LOGGER.warning(
                    "Bridge: query failed for %s (%s); will retry",
                    statistic_id,
                    ex,
                )
                still_pending[statistic_id] = (expected_state, expected_sum, unit)
                continue
            if latest is None:
                _LOGGER.info(
                    "Bridge: no live stats yet for %s; will retry", statistic_id
                )
                still_pending[statistic_id] = (expected_state, expected_sum, unit)
                continue
            actual_sum = float(latest.get("sum", 0) or 0)
            actual_state = float(latest.get("state", 0) or 0)
            target_sum = expected_sum + max(0.0, actual_state - expected_state)
            adjustment = target_sum - actual_sum
            if abs(adjustment) < 0.5:
                _LOGGER.info(
                    "Bridge: %s already chained (sum=%.1f, target=%.1f); done",
                    statistic_id,
                    actual_sum,
                    target_sum,
                )
                continue
            try:
                await _adjust_sum_statistics(
                    supervisor_token,
                    statistic_id,
                    start_time_iso=seam_iso,
                    adjustment=adjustment,
                    unit_of_measurement=unit,
                )
            except Exception as ex:
                _LOGGER.warning(
                    "Bridge: adjust_sum_statistics failed for %s: %s; will retry",
                    statistic_id,
                    ex,
                )
                still_pending[statistic_id] = (expected_state, expected_sum, unit)
        pending = still_pending
        if pending and attempt < max_attempts:
            _LOGGER.info(
                "Bridge: %d entities still pending; sleeping %d s before retry",
                len(pending),
                retry_interval_s,
            )
            await asyncio.sleep(retry_interval_s)

    if pending:
        _LOGGER.warning(
            "Bridge: gave up on %d entities after %d attempts: %s",
            len(pending),
            max_attempts,
            sorted(pending.keys()),
        )

#!/usr/bin/env python3
"""Cookie-authed Brunata OData fetcher.

Logs in via Playwright (to obtain the SAP session cookies), then makes plain
OData GETs against ``NP_UVI_SRV`` for consumption, per-room, and building
comparison data. See ``docs/portal-api.md`` for the reverse-engineered
reference of the endpoints used here.

Output on success::

    {
        "status": "ok",
        "data": {
            "last_update_date": "30.04.2026",
            "Heizung": 5681.0,            # kWh
            "Kaltwasser": 28.38,           # m³
            "Warmwasser": 448.0,           # kWh
            "comparison_pct": {            # your / building * 100
                "Heizung": 109.3,
                "Kaltwasser": 109.2,
                "Warmwasser": 90.5
            },
            "rooms_kwh": {                 # heating, recalculated to kWh
                "Bad": 329.5,
                ...
            },
            "rooms_pct": {                 # raw Anteil
                "Bad": 5.80,
                ...
            }
        }
    }

Output on error matches the existing scraper contract::

    {"status": "error", "type": "login"|"fetch"|"config", "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from typing import Any, TypedDict
from urllib.parse import quote


_LOGGER = logging.getLogger("brunata_fetcher.api")


_PORTAL_HOST = "https://nutzerportal.brunata-muenchen.de"
_LOGIN_URL = f"{_PORTAL_HOST}/np_anmeldung/index.html?sap-language=DE"
_UVI_BASE = f"{_PORTAL_HOST}/sap/opu/odata/bme/NP_UVI_SRV"
_APPLAUNCHER_BASE = f"{_PORTAL_HOST}/sap/opu/odata/bme/NP_APPLAUNCHER_SRV"
_SAP_CLIENT = "201"

_SEL_EMAIL = "#__component0---Start--idEmailInput-inner"
_SEL_PASSWORD = "#__component0---Start--idPassword-inner"
_SEL_LOGIN_BUTTON = 'button:has-text("Anmelden")'

# Maps the HA-facing energy-type label to the SAP Kostenart and whether the
# value comes back as kWh. Kaltwasser is always m³ — there is no kWh option.
_ENERGY_TYPE_KOTYP: dict[str, tuple[str, bool]] = {
    "Heizung": ("HZ01", True),
    "Kaltwasser": ("KW01", False),
    "Warmwasser": ("WW01", True),
}

# Display names for the room codes the portal returns. Anything outside this
# map is exposed under its raw code so we don't drop unfamiliar rooms.
_ROOM_LABELS: dict[str, str] = {
    "BAD": "Bad",
    "ESS": "Esszimmer",
    "KIN": "Kinderzimmer",
    "KUE": "Küche",
    "SZ": "Schlafzimmer",
    "WZ": "Wohnzimmer",
}


class FetcherConfig(TypedDict, total=False):
    """Config accepted by ``fetch``.

    ``email`` / ``password`` / ``energy_types`` are required at runtime;
    the rest are optional.
    """

    email: str
    password: str
    energy_types: list[str]
    headless: bool
    debug: bool
    playwright_timeout: int


_REQUIRED_CONFIG_KEYS: tuple[str, ...] = ("email", "password", "energy_types")


# Chromium flags — same logic as _brunata_scraper.py. Kept inline here so the
# API module stays self-contained.
_CHROMIUM_ARGS: list[str] = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
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
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-features=site-per-process",
    "--disable-blink-features=AutomationControlled",
]


# --- Helpers ----------------------------------------------------------------


def _parse_sap_date(raw: str | None) -> str | None:
    """Convert SAP ``/Date(ms)/`` strings to ISO ``YYYY-MM-DD``.

    Falls back to ``None`` for anything we can't parse. The portal mixes UTC
    midnight and ``T22:00:00`` (midnight Europe/Berlin in UTC), so we always
    extract the date portion of the UTC instant — close enough for our use.
    """
    if not raw:
        return None
    match = re.search(r"/Date\((-?\d+)\)/", raw)
    if not match:
        return None
    ms = int(match.group(1))
    # Bias by 12h so the local date wins regardless of UTC offset encoding
    days = (ms // 1000 + 43200) // 86400
    from datetime import date, timedelta

    return (date(1970, 1, 1) + timedelta(days=days)).isoformat()


def _to_de_date(iso: str | None) -> str | None:
    """Format ``YYYY-MM-DD`` as ``DD.MM.YYYY`` for back-compat MQTT payload."""
    if not iso:
        return None
    try:
        y, m, d = iso.split("-")
        return f"{d}.{m}.{y}"
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    """Parse a numeric string from SAP OData, returning ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _odata_filter(parts: list[str]) -> str:
    """Join filter clauses with ``and`` and URL-encode the whole thing."""
    return quote(" and ".join(parts), safe="")


async def _odata_get(request, url: str) -> dict:
    """GET an OData JSON resource and parse it. Raises on non-2xx.

    Note: not all SAP entity sets accept direct GETs; many production OData
    services require the ``$batch`` envelope. Use :func:`_odata_batch_get`
    for those.
    """
    response = await request.get(
        url,
        headers={
            "Accept": "application/json",
            "DataServiceVersion": "2.0",
            "MaxDataServiceVersion": "2.0",
        },
    )
    if not response.ok:
        body_preview = (await response.text())[:300]
        raise RuntimeError(
            f"OData GET failed: status={response.status} url={url} body={body_preview!r}"
        )
    return await response.json()


async def _fetch_csrf_token(request, service_base: str) -> str:
    """Fetch the SAP X-CSRF-Token for a service. Required for ``$batch`` POST."""
    response = await request.fetch(
        f"{service_base}/?sap-client={_SAP_CLIENT}",
        method="HEAD",
        headers={"X-CSRF-Token": "Fetch"},
    )
    if not response.ok:
        raise RuntimeError(
            f"CSRF token fetch failed: status={response.status} url={service_base}"
        )
    # Playwright lower-cases header names.
    token = response.headers.get("x-csrf-token", "")
    if not token or token.lower() == "required":
        raise RuntimeError(f"CSRF token fetch returned {token!r}")
    return token


def _parse_batch_response(text: str) -> list[dict]:
    """Split a ``multipart/mixed`` ``$batch`` response into JSON payloads."""
    # The outer boundary is the first line starting with ``--``.
    lines = text.split("\r\n")
    if not lines:
        return []
    outer = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("--") and stripped != "--":
            outer = stripped.rstrip("-")
            break
    if not outer:
        raise RuntimeError("Could not find outer multipart boundary")

    parts = text.split(outer)
    payloads: list[dict] = []
    for part in parts:
        # Each inner part contains its own HTTP response. Find the JSON body
        # (after the blank line that follows the inner HTTP headers).
        marker = "\r\n\r\n"
        # First blank line ends the multipart headers, second ends the inner
        # HTTP response headers, then the JSON body starts.
        first = part.find(marker)
        if first < 0:
            continue
        second = part.find(marker, first + len(marker))
        if second < 0:
            continue
        body = part[second + len(marker) :].strip()
        if not body or not body.startswith("{"):
            continue
        # Trim a trailing boundary line ("--") that may be appended.
        if body.endswith("--"):
            body = body.rsplit("\n", 1)[0].strip()
        try:
            payloads.append(json.loads(body))
        except json.JSONDecodeError:
            continue
    return payloads


async def _odata_batch_get(
    request,
    service_base: str,
    inner_gets: list[str],
    *,
    user_unit_id: str | None = None,
    contact_person: str | None = None,
) -> list[dict]:
    """Send a batched GET to a SAP OData v2 service.

    ``inner_gets`` is a list of paths relative to the service root (e.g.
    ``CumuConsumptionMonSet?$filter=...``). Returns one parsed JSON payload
    per inner GET, in the same order.

    The multipart body and inner-request headers are matched byte-for-byte
    against what the SAPUI5 frontend produces. SAP NetWeaver's batch parser
    is picky — leading ``\\r\\n``, the extra blank line for the empty GET
    body, and the ``X-Requested-With`` / ``sap-cancel-on-close`` headers all
    matter for the request to be accepted.
    """
    import uuid

    token = await _fetch_csrf_token(request, service_base)
    boundary = "batch_" + uuid.uuid4().hex

    crlf = "\r\n"
    sap_headers: list[str] = []
    if user_unit_id:
        sap_headers.append(f"UserUnitID: {user_unit_id}{crlf}")
    if contact_person:
        sap_headers.append(f"ContactPerson: {contact_person}{crlf}")

    body_parts: list[str] = [crlf]  # frontend prepends one CRLF
    for path in inner_gets:
        body_parts.extend(
            [
                f"--{boundary}{crlf}",
                f"Content-Type: application/http{crlf}",
                f"Content-Transfer-Encoding: binary{crlf}",
                crlf,
                f"GET {path} HTTP/1.1{crlf}",
                f"sap-cancel-on-close: true{crlf}",
                *sap_headers,
                f"sap-contextid-accept: header{crlf}",
                f"Accept: application/json{crlf}",
                f"x-csrf-token: {token}{crlf}",
                f"Accept-Language: de{crlf}",
                f"DataServiceVersion: 2.0{crlf}",
                f"MaxDataServiceVersion: 2.0{crlf}",
                f"X-Requested-With: XMLHttpRequest{crlf}",
                crlf,  # blank line ends inner request headers
                crlf,  # empty GET body
            ]
        )
    body_parts.append(f"--{boundary}--{crlf}")
    body = "".join(body_parts)

    response = await request.post(
        f"{service_base}/$batch?sap-client={_SAP_CLIENT}",
        headers={
            "Content-Type": f"multipart/mixed; boundary={boundary}",
            "X-CSRF-Token": token,
            "Accept": "multipart/mixed",
            "DataServiceVersion": "2.0",
            "MaxDataServiceVersion": "2.0",
            "sap-cancel-on-close": "true",
            "sap-contextid-accept": "header",
            "X-Requested-With": "XMLHttpRequest",
        },
        data=body,
    )
    if not response.ok:
        body_preview = (await response.text())[:300]
        raise RuntimeError(
            f"$batch POST failed: status={response.status} body={body_preview!r}"
        )
    text = await response.text()
    payloads = _parse_batch_response(text)
    if len(payloads) != len(inner_gets):
        _LOGGER.warning(
            "$batch returned %d payloads for %d requests; raw response head: %r",
            len(payloads),
            len(inner_gets),
            text[:800],
        )
    return payloads


def _results(payload: dict) -> list[dict]:
    """Pull the ``d.results`` array out of a SAP OData v2 JSON response."""
    if not isinstance(payload, dict):
        return []
    d = payload.get("d")
    if isinstance(d, dict):
        results = d.get("results")
        if isinstance(results, list):
            return results
        # Single-entity response: ``d`` is the row directly.
        return [d] if "__metadata" in d else []
    return []


# --- OData queries ----------------------------------------------------------


async def _discover_user_context(request) -> tuple[str, str]:
    """Return ``(UserUnitID, Partner)`` from the app-launcher user context.

    SAP's backend uses these two as application-level headers (``UserUnitID``
    and ``ContactPerson``) on every subsequent business request inside a
    ``$batch`` envelope. Without them the data services return a generic
    500.
    """
    url = f"{_APPLAUNCHER_BASE}/UserContextSet?sap-client={_SAP_CLIENT}"
    payload = await _odata_get(request, url)
    rows = _results(payload)
    if not rows:
        raise RuntimeError("UserContextSet returned no rows")
    nutzein = str(rows[0].get("UserUnitID") or "").strip()
    partner = str(rows[0].get("Partner") or "").strip()
    if not nutzein or not partner:
        raise RuntimeError(
            f"UserContextSet missing UserUnitID/Partner: {rows[0]!r}"
        )
    return nutzein, partner


async def _discover_period(
    request, nutzein: str, partner: str
) -> tuple[str, str]:
    """Return ``(query_bis_literal, official_last_update_iso)``.

    The portal's ``DatesSet`` advertises the last *fully closed* month as
    ``Bisdatum`` (e.g. 2026-04-30 on May 12). Querying with that misses
    the in-progress month, so for the actual data fetch we bump ``Bis``
    forward to end-of-current-month — that unlocks the portal's preliminary
    value for the in-progress month.

    The second return value is the **unbumped** Bisdatum — i.e. the last
    fully-confirmed month-end. It's surfaced as the ``last_update_date``
    sensor so HA shows when Brunata last closed a month, not the
    forward-looking query window.
    """
    from datetime import date as _date, timedelta as _timedelta

    filter_qs = _odata_filter(
        [f"Nutzein eq '{nutzein}'", "IsCalendar eq true"]
    )
    inner = (
        f"DatesSet?sap-client={_SAP_CLIENT}"
        f"&$expand=Units"
        f"&$filter={filter_qs}"
    )
    payloads = await _odata_batch_get(
        request,
        _UVI_BASE,
        [inner],
        user_unit_id=nutzein,
        contact_person=partner,
    )
    if not payloads:
        raise RuntimeError("DatesSet $batch returned no payloads")
    rows = _results(payloads[0])
    if not rows:
        raise RuntimeError(f"DatesSet returned no rows; payload={payloads[0]!r}")

    def _abdatum_iso(row: dict) -> str:
        return _parse_sap_date(row.get("Abdatum")) or ""

    latest = max(rows, key=_abdatum_iso)
    official_iso = _parse_sap_date(latest.get("Bisdatum"))
    if not official_iso:
        raise RuntimeError(f"DatesSet row missing Bisdatum: {latest!r}")

    query_iso = official_iso
    today = _date.today()
    if today.year == int(official_iso[:4]):
        if today.month == 12:
            month_end = _date(today.year, 12, 31)
        else:
            month_end = _date(today.year, today.month + 1, 1) - _timedelta(days=1)
        if month_end.isoformat() > query_iso:
            query_iso = month_end.isoformat()
    return f"datetime'{query_iso}T00:00:00'", official_iso


def _build_uvi_inner_get(entity_set: str, filter_clauses: list[str]) -> str:
    """Build the inner path component of a ``$batch`` GET for NP_UVI_SRV."""
    return (
        f"{entity_set}?sap-client={_SAP_CLIENT}"
        f"&$filter={_odata_filter(filter_clauses)}"
    )


def _parse_monthly(payload: dict) -> tuple[float | None, str | None]:
    """Return (sum_of_monthly_verbrauch, latest_row_date_iso).

    ``NP_UVI_SRV/CumuConsumptionMonSet`` despite its name returns *per-month*
    consumption in the ``Verbrauch`` field, not a cumulative ``Aktuell`` like
    the dashboard's CumuConsMonCompSet. Summing the months gives the YTD
    total for the period.
    """
    rows = _results(payload)
    if not rows:
        return None, None

    def _row_date(row: dict) -> str:
        return _parse_sap_date(row.get("Datum")) or ""

    total = 0.0
    latest_date: str = ""
    saw_value = False
    for row in rows:
        value = _to_float(row.get("Verbrauch"))
        if value is None:
            continue
        total += value
        saw_value = True
        row_date = _row_date(row)
        if row_date > latest_date:
            latest_date = row_date
    if not saw_value:
        return None, None
    return round(total, 2), latest_date or None


def _parse_comparison(payload: dict) -> float | None:
    """Return your-consumption / building-average × 100, or None."""
    for row in _results(payload):
        you = _to_float(row.get("Verbrauch"))
        them = _to_float(row.get("LgVerbrauch"))
        if you is not None and them is not None and them != 0:
            return round(you / them * 100, 1)
    return None


def _parse_rooms(payload: dict) -> list[dict]:
    rooms: list[dict] = []
    for row in _results(payload):
        raum = str(row.get("Raum") or "").strip()
        if not raum:
            continue
        anteil = _to_float(row.get("Anteil"))
        if anteil is None:
            continue
        rooms.append(
            {
                "Raum": raum,
                "RaumTxt": row.get("RaumTxt") or _ROOM_LABELS.get(raum, raum),
                "Anteil": anteil,
            }
        )
    return rooms


# --- Login ------------------------------------------------------------------


async def _login(page, email: str, password: str) -> None:
    _LOGGER.info("Opening login page")
    await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
    # SAPUI5 bootstraps after DOMContentLoaded and re-renders the form a
    # couple of times while data binding settles. Wait for network to go
    # quiet before checking for the email field — without this the
    # subsequent wait_for_selector can time out on slow hardware even
    # though the element is briefly visible, because Playwright's
    # actionability re-check keeps tripping on the re-renders.
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        _LOGGER.debug("networkidle wait timed out before login", exc_info=True)
    await page.wait_for_selector(_SEL_EMAIL)
    await page.fill(_SEL_EMAIL, email)
    await page.fill(_SEL_PASSWORD, password)
    await page.click(_SEL_LOGIN_BUTTON)
    try:
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        _LOGGER.debug("Post-login load state wait raised", exc_info=True)
    # Brief settle so SAP can set its session cookies before we hit OData.
    await page.wait_for_timeout(1500)

    current_url = page.url.lower()
    if "anmeldung" in current_url or "login" in current_url:
        body = (await page.text_content("body") or "").lower()
        if any(
            w in body
            for w in ("ungültig", "invalid", "fehler", "error", "incorrect")
        ):
            raise RuntimeError("LOGIN_FAILED")


# --- Public API -------------------------------------------------------------


async def fetch(config: FetcherConfig) -> dict:
    """End-to-end: login, fetch OData, return structured result."""
    from playwright.async_api import async_playwright

    start = time.monotonic()
    email = config["email"]
    password = config["password"]
    energy_types = config["energy_types"]
    headless = config.get("headless", True)
    pw_timeout = config.get("playwright_timeout", 30000)

    masked = f"***{email[-4:]}" if len(email) >= 4 else "***"
    _LOGGER.info(
        "Fetcher entry: user=%s energy_types=%s headless=%s",
        masked,
        energy_types,
        headless,
    )

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
                page.set_default_timeout(pw_timeout)
                try:
                    await _login(page, email, password)
                finally:
                    await page.close()
                _LOGGER.info("Login complete; switching to OData calls")

                result = await _fetch_all(context.request, energy_types)
            finally:
                await context.close()
        finally:
            await browser.close()

    duration = time.monotonic() - start
    _LOGGER.info("Fetcher exit in %.2fs", duration)
    return result


async def _fetch_all(request, energy_types: list[str]) -> dict:
    nutzein, partner = await _discover_user_context(request)
    bis_literal, official_last_update_iso = await _discover_period(
        request, nutzein, partner
    )
    bis_iso = bis_literal.split("'")[1][:10]
    _LOGGER.info(
        "Period: Nutzein=%s Partner=%s query_Bis=%s last_update=%s",
        nutzein,
        partner,
        bis_iso,
        official_last_update_iso,
    )

    # Build all the inner GETs into a single batched call. Order matters —
    # we map indices back to entity types below.
    common = [f"Nutzein eq '{nutzein}'", f"Bis eq {bis_literal}"]
    inner_gets: list[str] = []
    index_map: list[tuple[str, str]] = []  # (kind, energy_type)

    for energy_type in energy_types:
        kotyp_unit = _ENERGY_TYPE_KOTYP.get(energy_type)
        if kotyp_unit is None:
            _LOGGER.warning("Unknown energy type %r; skipping", energy_type)
            continue
        kotyp, in_kwh = kotyp_unit
        in_kwh_lit = "true" if in_kwh else "false"
        inner_gets.append(
            _build_uvi_inner_get(
                "CumuConsumptionMonSet",
                common
                + [
                    f"Kotyp eq '{kotyp}'",
                    f"InKwh eq {in_kwh_lit}",
                    "IsWeatherAdjusted eq false",
                ],
            )
        )
        index_map.append(("monthly", energy_type))
        inner_gets.append(
            _build_uvi_inner_get(
                "CumuConsLGCompSet",
                common
                + [
                    f"Kotyp eq '{kotyp}'",
                    f"InKwh eq {in_kwh_lit}",
                    "KJahr eq true",
                ],
            )
        )
        index_map.append(("comparison", energy_type))

    # Weather-adjusted Heizung — same monthly set, IsWeatherAdjusted=true.
    # We only do it for Heizung since heating is the only cost type where
    # seasonality matters; cold/warm water consumption isn't weather-driven.
    #
    # Important: the WB query MUST use the unbumped Bisdatum (i.e. the last
    # completed month-end as published by DatesSet) — querying with the
    # bumped current-month-end returns 0 rows. The portal only computes
    # weather adjustment over fully-closed months. Consequence: in the
    # in-progress month the WB sensor lags the raw one by up to ~30 days.
    if "Heizung" in energy_types:
        kotyp, in_kwh = _ENERGY_TYPE_KOTYP["Heizung"]
        in_kwh_lit = "true" if in_kwh else "false"
        wb_bis_literal = f"datetime'{official_last_update_iso}T00:00:00'"
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

    inner_gets.append(_build_uvi_inner_get("CumuConsumptionRoomSet", common))
    index_map.append(("rooms", ""))

    payloads = await _odata_batch_get(
        request,
        _UVI_BASE,
        inner_gets,
        user_unit_id=nutzein,
        contact_person=partner,
    )
    if len(payloads) != len(index_map):
        raise RuntimeError(
            f"$batch returned {len(payloads)} payloads, expected {len(index_map)}"
        )

    out: dict[str, Any] = {
        "last_update_date": _to_de_date(official_last_update_iso),
        "bis_iso": official_last_update_iso,
        "query_bis_iso": bis_iso,
        "comparison_pct": {},
    }
    heating_total_kwh: float | None = None
    heating_wb_total_kwh: float | None = None
    rooms_payload: dict | None = None

    for (kind, energy_type), payload in zip(index_map, payloads):
        if kind == "monthly":
            total, _row_date = _parse_monthly(payload)
            out[energy_type] = total
            if energy_type == "Heizung":
                heating_total_kwh = total
        elif kind == "monthly_wb":
            total, _row_date = _parse_monthly(payload)
            out["Heizung_witterungsbereinigt"] = total
            heating_wb_total_kwh = total
        elif kind == "comparison":
            out["comparison_pct"][energy_type] = _parse_comparison(payload)
        elif kind == "rooms":
            rooms_payload = payload

    rooms_pct: dict[str, float] = {}
    rooms_kwh: dict[str, float] = {}
    rooms_kwh_wb: dict[str, float] = {}
    if rooms_payload is not None:
        for room in _parse_rooms(rooms_payload):
            label = room["RaumTxt"]
            anteil = float(room["Anteil"])
            rooms_pct[label] = round(anteil, 2)
            if heating_total_kwh is not None:
                rooms_kwh[label] = round(anteil / 100 * heating_total_kwh, 1)
            if heating_wb_total_kwh is not None:
                rooms_kwh_wb[label] = round(
                    anteil / 100 * heating_wb_total_kwh, 1
                )
    out["rooms_pct"] = rooms_pct
    out["rooms_kwh"] = rooms_kwh
    out["rooms_kwh_witterungsbereinigt"] = rooms_kwh_wb

    return out


# --- CLI entry point --------------------------------------------------------


def _validate_config(raw: object) -> FetcherConfig:
    if not isinstance(raw, dict):
        raise ValueError("Config must be a JSON object")
    missing = [k for k in _REQUIRED_CONFIG_KEYS if k not in raw]
    if missing:
        raise ValueError(f"Missing required config key(s): {', '.join(missing)}")
    return raw  # type: ignore[return-value]


def main() -> None:
    try:
        raw = json.loads(sys.stdin.read())
    except Exception as ex:
        _LOGGER.exception("Config decode failed")
        print(json.dumps({"status": "error", "type": "config", "message": str(ex)}))
        sys.exit(1)

    try:
        config = _validate_config(raw)
    except ValueError as ex:
        _LOGGER.error("Config validation failed: %s", ex)
        print(json.dumps({"status": "error", "type": "config", "message": str(ex)}))
        sys.exit(1)

    try:
        result = asyncio.run(fetch(config))
        print(json.dumps({"status": "ok", "data": result}))
    except RuntimeError as ex:
        if "LOGIN_FAILED" in str(ex):
            _LOGGER.error("Login failed")
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
            _LOGGER.exception("Fetch runtime error")
            print(
                json.dumps({"status": "error", "type": "fetch", "message": str(ex)})
            )
        sys.exit(1)
    except Exception as ex:
        _LOGGER.exception("Unhandled fetcher exception")
        print(json.dumps({"status": "error", "type": "fetch", "message": str(ex)}))
        sys.exit(1)


if __name__ == "__main__":
    main()

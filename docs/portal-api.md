# Brunata Nutzerportal — Reverse-engineered API reference

This document captures what we learned by observing the SAPUI5 frontend at
`https://nutzerportal.brunata-muenchen.de/`. The portal is a SAP NetWeaver
backend with OData v2 services. After interactive login, all data is fetched
through `POST /sap/opu/odata/.../$batch` requests that wrap inner OData GETs in
multipart/mixed envelopes.

Captured 2026-05-12 against the production portal.

## Authentication

Login is done against `https://nutzerportal.brunata-muenchen.de/np_anmeldung/`
using the standard form fields:

| Selector | Field |
|---|---|
| `#__component0---Start--idEmailInput-inner` | email |
| `#__component0---Start--idPassword-inner` | password |
| `button:has-text("Anmelden")` | submit |

Authentication state is held in cookies (SAP session cookies). To make
``$batch`` POSTs we additionally need an ``X-CSRF-Token``, which is obtained
by sending ``HEAD <service>/?sap-client=201`` with the header
``X-CSRF-Token: Fetch``. The token comes back in the ``X-CSRF-Token``
response header and stays valid for the lifetime of the session.

## SAP context headers — required for `$batch`

Beyond cookies and the CSRF token, every business-data request that goes
through ``$batch`` needs two application-level headers on the **inner**
HTTP request inside the multipart body:

| Header | Value |
|---|---|
| `UserUnitID` | the user's `Nutzein` |
| `ContactPerson` | the user's `Partner` ID |

Without them the SAP gateway throws a generic
``/BME/KP_MSG_CORE/224 — "Oh Nein ... dies hätte nicht passieren dürfen."``
500 error. Both values are sourced from `NP_APPLAUNCHER_SRV/UserContextSet`
on every cold-start.

## Direct GETs vs `$batch`

`NP_APPLAUNCHER_SRV/UserContextSet` accepts a plain HTTP GET. The
data-bearing services (`NP_UVI_SRV`, `NP_DASHBOARD_SRV`) do **not** —
the SAP gateway rejects direct GETs to their entity sets with the same
generic 500, and only the `$batch` POST envelope works. We therefore use
direct GET only for the cold-start UserContextSet discovery, and `$batch`
for everything else.

## User identity

Every data query is keyed by `Nutzein` (the Nutzeinheit / user-unit ID). This
is a numeric string like `'2000685740'`. It can be discovered via:

```
GET /sap/opu/odata/bme/NP_UVI_SRV/DatesSet?$filter=IsCalendar eq true
```

— the response rows all carry the user's `Nutzein`. (The portal also publishes
the same ID through `NP_GENERIC_SRV` and `NP_APPLAUNCHER_SRV` very early in
the load sequence, so any of those work.)

## Services overview

| Service | Purpose |
|---|---|
| `NP_REG_LOGON_SRV_01` | Registration / logon flow |
| `NP_GENERIC_SRV` | Generic helpers (config, language, …) |
| `NP_APPLAUNCHER_SRV` | Navigation menu, available apps |
| `NP_NOTIFICATION_SRV` | Inbox notifications |
| `NP_MEAREA_SRV` | Measurement area metadata |
| `NP_DASHBOARD_SRV` | Übersicht page: YTD totals, YoY, building comparison |
| `NP_UVI_SRV` | Verbrauch page: monthly breakdown, per-room, time selection |

For our purposes only **`NP_DASHBOARD_SRV`** and **`NP_UVI_SRV`** matter.

## Query parameters

These parameters appear in `$filter` clauses on the relevant entity sets.

| Param | Meaning | Values |
|---|---|---|
| `Nutzein` | User unit ID | `'2000685740'` |
| `Bis` | Period end date (inclusive) | `datetime'YYYY-MM-DDT00:00:00'`. YTD uses the last day with data (e.g. `2026-04-30`). Full years use the year-end (`2025-12-31`, `2024-12-31`, …). The dashboard variant shifts to `T22:00:00`; both are accepted. |
| `Kotyp` | Cost type (Kostenart) | `HZ01` Heizung, `KW01` Kaltwasser, `WW01` Warmwasser |
| `InKwh` | "kWh vs. Einheiten" toggle | `true` = values reported in kWh, `false` = values in Verbrauchswerteinheiten ("Einh." = allocator units). Kaltwasser is always `false` because m³ isn't kWh-convertible. |
| `IsWeatherAdjusted` | "Witterungsbereinigt" toggle | `true` / `false`. Only meaningful for Heizung. UVI sends this explicitly; the dashboard omits it (server treats omission as `false`). |
| `KJahr` | "Kalenderjahr" — building-comparison mode | `true` = compare against full calendar year of building, used by `CumuConsLGCompSet` |

`InKwh=true` is what HA wants. We never expose `InKwh=false` results.

## Entity sets we use

### `NP_UVI_SRV/CumuConsumptionMonSet`

**Per-month** consumption for one cost type over a period. Despite the "Cumu"
in the name, this set is **not cumulative** — each row is one month's value
in the ``Verbrauch`` field. Sum across rows for YTD.

(The Übersicht widget uses ``NP_DASHBOARD_SRV/CumuConsMonCompSet`` instead,
which returns one row per month-end with a cumulative ``Aktuell`` already
joined to last year's ``Vorjahr``. Different entity set, different shape.)

**Filter:** `Nutzein, Bis, Kotyp, InKwh, IsWeatherAdjusted`

**Per-row fields of interest:**

| Field | Meaning |
|---|---|
| `Datum` | Month end (`/Date(ms)/` epoch) |
| `Verbrauch` | Per-month consumption value |
| `Massread` | `KWH` / `MTQ` (m³) / `VBW` (Einheiten) |
| `MassreadTxt` | Display unit string |
| `InKwh` | Echo of the request param |
| `IsWeatherAdjusted` | Echo of the request param |

To get the up-to-date YTD for the period: sum `Verbrauch` over all rows.

### `NP_UVI_SRV/CumuConsumptionRoomSet`

Per-room heating breakdown. **Heizung only** — even if you request it with
`Kotyp='KW01'` (water), the response always contains HZ01 entries.

**Filter:** `Nutzein, Bis` (Kotyp and unit toggles are ignored)

**Per-row fields:**

| Field | Meaning |
|---|---|
| `Raum` | Room code: `BAD`, `ESS`, `KIN`, `KUE`, `SZ`, `WZ` |
| `RaumTxt` | German display name: Bad, Esszimmer, Kinderzimmer, Küche, Schlafzimmer, Wohnzimmer |
| `Anteil` | Percentage of total heating in this room (e.g. `"32.15"`) |
| `Verbrauch` | Absolute consumption — **always in VBW Einheiten**, not kWh |
| `Massread` | `"VBW"` |

To get per-room **kWh**, we don't use `Verbrauch` directly — we compute
`room_kwh = Anteil/100 × heizung_total_kwh` where the total comes from
`CumuConsumptionMonSet` with `InKwh=true`.

### `NP_UVI_SRV/CumuConsLGCompSet`

Building-average comparison (Liegenschaftsvergleich).

**Filter:** `Nutzein, Bis, Kotyp, InKwh, KJahr=true`

**Per-row fields:**

| Field | Meaning |
|---|---|
| `Verbrauch` | Your consumption |
| `LgVerbrauch` | Building average |
| `Vbkz` | Comparison flag — `"I"` if you're in line with the building, others TBD |
| `Refnutzer` | Reference user (anonymous) |

Comparison percentage we expose: `your / building × 100`.

### `NP_DASHBOARD_SRV/CumuConsMonCompSet`

Used by the Übersicht widget — same shape as `CumuConsumptionMonSet` but
returns one row per month-end with the YoY pair already joined. For the
pivoted flow we use the UVI variant instead so we have a single source.

## Time conventions

- All `Bis` / `Datum` request parameters use OData v2 `datetime'YYYY-MM-DDTHH:MM:SS'` literals. The portal accepts both `T00:00:00` and `T22:00:00` for the same logical period.
- Response timestamps come as `/Date(milliseconds_since_epoch)/`. They are UTC instants — the portal sometimes encodes midnight Europe/Berlin as `Date(...22:00 UTC)`, sometimes as `Date(...00:00 UTC)`. Treat the date portion only.

## Per-period boundaries

`DatesSet` (with `$expand=Units` and `$filter=IsCalendar eq true`) returns
one row per available year, each with `Abdatum` (period start) and `Bisdatum`
(period end). For the current year, `Bisdatum` is the last **fully-closed**
month-end (e.g. on 2026-05-12 the portal reports `Bisdatum=2026-04-30`).

Quirks of the data-side `Bis` filter we discovered by probing:

- The response always contains exactly 12 rows (Jan–Dec); `$filter` clauses
  on `Datum` are silently ignored. Selectivity comes only from `Bis`.
- Setting `Bis` to a **future month-end** within the current year unlocks
  the portal's preliminary in-progress-month `Verbrauch` for that month
  (e.g. `Bis=2026-05-31` returns May with a non-null value on May 12).
- Setting `Bis` to a date that is **not a month-end** (e.g. today's date)
  is silently treated like `Bis=<last-completed-month-end>` — no
  preliminary value is returned.

### Bis policy for HA polling

The fetcher uses **end-of-current-month** as `Bis` (e.g. `2026-05-31`) to
include the preliminary in-progress month in the YTD total — this keeps the
HA energy sensor fresh between Brunata's monthly meter reads. Year boundaries
are handled defensively: we never advance `Bis` into a year the portal
doesn't already know about (so on e.g. 2027-01-05 we stay on
`Bis=2026-12-31` until the portal acknowledges 2027).

The unbumped `Bisdatum` from `DatesSet` is still surfaced separately as
`last_update_date` — that is the date HA users see for "when did the portal
last close a month", which only ticks forward when Brunata processes
end-of-month readings.

### How often the preliminary value actually updates

Empirically (observed over a multi-day run): Brunata refreshes the
preliminary current-month `Verbrauch` **once per day**, in the late
afternoon — in one observed case the new value showed up shortly after
17:00 local time. Polling more often than once a day returns the same
preliminary value over and over. The default `scan_interval_hours: 24`
is therefore already correct; anything shorter mostly wastes Pi CPU /
SD writes / Brunata's rate limit headroom. If you want to *catch* the
daily bump tightly, schedule the polling to fire after 18:00 local time;
otherwise the addon's once-daily cycle on whatever clock alignment it
landed on is fine.

## Why we still use Playwright at all

The login itself goes through a SAPUI5 form with anti-CSRF tokens we don't
manually want to chase. Once Playwright drives the login successfully, we hand
the `BrowserContext.request` object to the data layer — it shares the cookie
jar and CSRF state, so subsequent `POST /$batch` calls work without further
fuss.

Login + Playwright launch is a one-time ~3 s cost per cycle. If we ever want
to eliminate Playwright entirely, the next investigation step is:

1. Capture the exact login `POST` payload (currently skipped by the network
   logger to keep the password out of the JSONL).
2. Capture the SAP `X-CSRF-Token` round-trip.
3. Reproduce both with `urllib.request` / `aiohttp`.

For now Playwright stays.

## Investigation tooling (preserved)

The DOM-scraping code path lives on as `brunata_fetcher/_brunata_scraper.py`
and is still wired through `run_scraper_once.py`. The interactive explorer
`brunata_fetcher/explore_portal.py` opens a non-headless browser, logs in,
navigates to Verbrauch, and lets you click around while a network logger
writes `portal_network.jsonl` to the system temp dir. Use these to probe new
API surfaces before adding them to the main flow.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Home Assistant add-on that pulls consumption data (heating, cold water, warm water) from the **Brunata München Nutzerportal / BRUdirekt** and publishes MQTT Discovery entities. Ships as a Docker container — pure Python, no browser. The portal is a SAP NetWeaver / SAPUI5 frontend backed by OData v2 services, and the add-on talks to it entirely over plain HTTP (`httpx`), login included.

It exists as an add-on (not a custom HA integration) for historical reasons: an earlier integration-form attempt hit unreliable Playwright auto-install on the HA core runtime, so the project shipped Playwright + Chromium baked into an addon container instead. As of **v0.4.0** the production path no longer uses a browser at all — the login was reverse-engineered to a plain HTTP call — so that constraint is gone, but the add-on form is kept.

## Architecture pivots (read first)

Two pivots shaped the codebase; both old paths still live in the tree, intentionally. Trust `docs/portal-api.md` and the code over the README.

- **v0.3.0** — DOM scraping → cookie-authed OData API.
- **v0.4.0** — Playwright → pure HTTP. The browser login was reverse-engineered to a plain two-step HTTP call (validate credentials, then a Basic-auth session GET — see `docs/portal-api.md`), so the production path no longer launches a browser. `httpx` is the only HTTP dependency; Playwright is dev-only.

- **Production path:** [`brunata_fetcher/_brunata_api.py`](brunata_fetcher/_brunata_api.py). `_login_http` does the two-step HTTP login to obtain SAP session cookies on an `httpx.AsyncClient`; subsequent data fetches POST to `NP_UVI_SRV/$batch` on the same client. A full cycle is ~6s.
- **Legacy/fallback path:** [`brunata_fetcher/_brunata_scraper.py`](brunata_fetcher/_brunata_scraper.py). DOM-scrapes the Verbrauch widget via Playwright. Brittle to portal UI changes, needs `requirements-dev.txt`. Kept as a reference only.
- **Investigation tooling:** several non-production helper scripts for poking at the portal API and the consumption data — see [Analysis & investigation scripts](#analysis--investigation-scripts). Reach for them when the API changes or the data looks off, before touching production code.

[`server.py`](brunata_fetcher/server.py) is the long-running HA entrypoint — reads `/data/options.json`, connects to MQTT, runs the fetch loop, publishes Discovery + state.

## OData call protocol (gotchas)

Reverse-engineered details live in [`docs/portal-api.md`](docs/portal-api.md). The non-obvious bits that bite:

- Data services (`NP_UVI_SRV`, `NP_DASHBOARD_SRV`) **reject direct GETs** with a generic `/BME/KP_MSG_CORE/224` 500. They only accept the `$batch` POST envelope. `NP_APPLAUNCHER_SRV/UserContextSet` is the exception — it accepts a plain GET and is used to bootstrap.
- Every inner `$batch` GET requires two SAP-specific headers: `UserUnitID` (= `Nutzein`) and `ContactPerson` (= `Partner`). Both come from `UserContextSet`. Without them, same 500.
- `$batch` POSTs need a fresh `X-CSRF-Token`, fetched via `HEAD <service>/?sap-client=201` with `X-CSRF-Token: Fetch`. The multipart body format is SAP-picky — leading `\r\n`, double-blank between parts, `X-Requested-With: XMLHttpRequest` all matter. See `_odata_batch_get` for the exact shape that works.
- `CumuConsumptionMonSet` despite the "Cumu" in the name returns **per-month** values in `Verbrauch`. Sum across rows for YTD. The dashboard's `CumuConsMonCompSet` (different entity set) is the truly cumulative one.
- `$filter` on `Datum eq ...` is **silently ignored** — every response has all 12 months; selectivity comes only from `Bis`.
- `Bis` rounded to non-month-end dates (e.g. today's date) is silently treated as last-completed-month-end. Use month-end dates only.

## Bis policy

`_discover_period` bumps `Bis` forward to the **end of the current calendar month** so YTD totals include the portal's preliminary in-progress-month value. The unbumped `Bisdatum` is returned separately as `official_last_update_iso` and surfaces as `sensor.brunata_fetcher_last_update` so HA users see when Brunata last *closed* a month (stable, ticks monthly), while the energy sensors stay current. Don't conflate the two.

## MQTT entity model

One HA device (`BRUdirekt`). Entity IDs are derived from `unique_id` in the Discovery payload — that's the stable anchor; the `name` field is just the initial label, users can rename freely in HA without breaking anything.

- **Preserve Discovery topic + `unique_id` stability across versions.** Renaming a slug or changing the topic path causes HA to orphan the old entity and create a new one — users lose history. Any rename needs an explicit migration path (publish an empty retained payload to the old topic to remove it).
- Per-room heating sensors are **dynamically discovered**: each `RaumTxt` seen for the first time triggers a one-shot Discovery publish. The label-to-slug helper is `_slug_for_room` in `server.py` (German ASCII-fold: `Küche → kueche`).
- Heating sensors (totals + per-room) use `state_class: total_increasing` so HA's long-term-statistics engine handles year-boundary YTD resets automatically. **Don't** publish per-month entities — that's not the HA way; the Energy Dashboard buckets the cumulative sensor.

## Login failure detection

The HTTP path (`_login_http`) detects bad credentials from the inner `$batch` response: `validLCR` comes back with an OData error envelope, code `/BME/KP_MSG_CORE/008` (and no `Userid` / no session cookie). The legacy scraper detects failures by substring-matching error words against the page body, gated on the URL still being on the login domain. Both raise `RuntimeError("LOGIN_FAILED")` — `server.py` translates that into a user-facing log line. If you change either path, keep the sentinel string intact.

## Common commands

Run from `brunata_fetcher/` (the addon directory) unless noted.

```powershell
# Production deps only (what the container installs) — pure Python, no browser
pip install -r requirements.txt

# Dev + investigation deps — adds Playwright for the browser-driven scripts
pip install -r requirements-dev.txt
python -m playwright install chromium      # ~300 MB download, dev only

# Smoke test — parser + MQTT payload shape, no network
python smoke_local.py

# End-to-end against the real portal (current production path, pure HTTP)
python run_api_once.py

# End-to-end against the real portal via the legacy DOM scraper (needs Playwright)
python run_scraper_once.py

# Compile-check before committing
python -m py_compile _brunata_api.py _brunata_backfill.py server.py
```

For the API/data investigation helpers (`explore_portal.py`, `probe_login.py`, `probe_freshness.py`, `dump_monthly.py`, `analyze_hdd.py`, …) see the next section.

Local credentials live in [`brunata_fetcher/.env`](brunata_fetcher/.env) (gitignored). `BRUNATA_DEBUG=true` enables HTML/screenshot/network-log dumps to `tempfile.gettempdir()` during the browser-driven investigation runs (see Debug artifacts below).

## Analysis & investigation scripts

Non-production helpers for poking at the portal API and the consumption data — **internal tooling, not shipped or referenced by the addon**. None are wired into `server.py`; all need `.env` credentials and run from `brunata_fetcher/`. Keep them around: they're the first thing to reach for when the API changes or the data looks wrong, before editing production code.

Dependency-wise they split two ways:

- **httpx-only** (`requirements.txt` is enough): `probe_freshness.py`, `dump_monthly.py`, `analyze_hdd.py` — they use the production `_login_http` + OData helpers.
- **browser-driven** (`requirements-dev.txt` + `playwright install chromium`): `explore_portal.py`, `probe_login.py`, and the legacy `run_scraper_once.py`.

- `explore_portal.py` — interactive non-headless browser. Logs in, opens the Verbrauch page, hands you the browser to click around. All traffic captured to `%TEMP%/portal_network.jsonl`.
- `probe_login.py` — drives the browser login and records every request/response (with the email/password redacted), so you can see exactly what the SAPUI5 "Anmelden" button sends. This is how the two-step HTTP login in `_login_http` was reverse-engineered. Run with `--bad-password` to capture the failure envelope.
- `explore_uvi_dates.py` — one-shot probe of `NP_UVI_SRV` `Bis`/`Datum` parameter behaviour (how the portal interprets non-month-end `Bis`, ignores `Datum` filters, etc.).
- `probe_freshness.py` — dumps the full OData row structure of `UserContextSet` / `DatesSet` / `CumuConsumptionMonSet` (we only parse 2 of ~10 fields in production) plus the `$metadata` entity-set + property inventory. Written to answer "does the portal expose a last-refreshed timestamp?" — it doesn't, but `Vbkz='E'` on a `CumuConsumptionMonSet` row flags the in-progress month's *estimated* value vs. `Vbkz=''` for closed months.
- `dump_monthly.py` — prints per-month raw + weather-adjusted (`IsWeatherAdjusted`) Heizung values across all years, mirroring exactly what the backfill imports. Good for sanity-checking the WB sensor.
- `analyze_hdd.py` — joins the per-month data with Open-Meteo historical temperatures, computes German G20/15 Heating Degree Days, and derives `kWh/HDD` per month and year. Takes `--lat` / `--lon`. Used to gut-check whether the portal's witterungsbereinigt numbers actually behave like a real weather normalization (spoiler: not really).

## Debug artifacts

`BRUNATA_DEBUG=true` in `.env` enables diagnostic dumps from the **browser-driven** investigation paths only — the legacy `_brunata_scraper.py` (via `run_scraper_once.py`) and `explore_portal.py`. There is no addon-side debug option: the production httpx path doesn't write dumps (use the investigation scripts instead). Artifacts land in the system temp dir:

- `portal_debug{1..5}.{html,png}` — page snapshots during the scraper login flow
- `portal_network.jsonl` — every request/response with method/URL/status/body. POST bodies are captured **except** for the login URL (which contains the password). Multipart `$batch` bodies are captured in full so you can inspect inner OData GETs.

## Environment notes

- Local dev is Windows + PowerShell; the addon runtime is Debian Linux.
- Python 3.14 locally, 3.x (system) in the addon container.
- The container installs `requirements.txt` only (`httpx`, `paho-mqtt`, `websockets`). Playwright is in `requirements-dev.txt` — dev/investigation only.
- Long-running command: `python -m playwright install chromium` (dev only) takes a few minutes.
- The repo's git identity is set per-repo (`Christoph Hornung <christoph.hornung@crosberg.de>`), not global.

## Conventions

- Default to ASCII in code and comments. The portal returns German Unicode (`Küche`, `Wohnzimmer`, `m³`) and those are preserved verbatim in data flow, but new code/comments should stay ASCII unless the file already uses Unicode.
- Prefer explicit, observable logging at INFO for login, fetch, and publish boundaries — this is what add-on users see in the Supervisor log and is the only debugging surface they have.

## Versioning + changelog

Always bump `version` in [`brunata_fetcher/config.yaml`](brunata_fetcher/config.yaml) and prepend an entry to [`brunata_fetcher/CHANGELOG.md`](brunata_fetcher/CHANGELOG.md) for any user-visible change. HA Supervisor uses the version field to drive update prompts.

**The CHANGELOG is user-facing.** HA shows it in the add-on's "What's new" panel before the user clicks Update. Write entries from the perspective of someone running the add-on in Home Assistant — what new entities or devices they'll see, behaviour changes that affect dashboards, install/upgrade impact. Skip module names, file paths, internal API names, SAP/OData terminology, container internals, or anything they can't observe from HA's UI. If a change has no observable effect for users (refactors, build-system tidy-up, dev-tooling additions, dependency bumps that don't change behaviour), either roll it into a release with user-visible changes and omit it from the entry, or skip the version bump entirely. The detailed technical "why" belongs in the commit message and the PR description, not the CHANGELOG.

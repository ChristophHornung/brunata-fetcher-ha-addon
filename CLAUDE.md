# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Home Assistant add-on that pulls consumption data (heating, cold water, warm water) from the **Brunata München Nutzerportal / BRUdirekt** and publishes MQTT Discovery entities. Ships as a Docker container with Python + Playwright + Chromium baked in. The portal is a SAP NetWeaver / SAPUI5 frontend backed by OData v2 services.

It exists as an add-on (not a custom HA integration) because an earlier integration-form attempt hit unreliable Playwright auto-install on the HA core runtime. Shipping Playwright + Chromium baked into the addon container sidesteps that entirely. Don't move Playwright installation to runtime — keep it container-build based.

## Architecture pivot (read first)

The codebase shifted from DOM scraping to a cookie-authed OData API in **v0.3.0**. Both paths still live in the tree and that's intentional. The README predates the pivot and describes the old architecture — trust `docs/portal-api.md` and the code over it.

- **Production path:** [`brunata_fetcher/_brunata_api.py`](brunata_fetcher/_brunata_api.py). Playwright logs in (one-shot, to obtain SAP session cookies); subsequent data fetches POST to `NP_UVI_SRV/$batch` directly. A full cycle is ~10s.
- **Legacy/fallback path:** [`brunata_fetcher/_brunata_scraper.py`](brunata_fetcher/_brunata_scraper.py). DOM-scrapes the Verbrauch widget via Playwright. ~19s, brittle to portal UI changes. Kept as a reference and for emergency fallback.
- **Investigation tooling:** [`explore_portal.py`](brunata_fetcher/explore_portal.py) (interactive non-headless browser with network logger), [`explore_uvi_dates.py`](brunata_fetcher/explore_uvi_dates.py) (one-shot probe for API parameter behavior). Use these when the API changes before touching production code.

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

The legacy scraper detects login failures by substring-matching error words against the page body, gated on the URL still being on the login domain. The new API path detects them via HTTP status / OData error envelopes. Both raise `RuntimeError("LOGIN_FAILED")` — `server.py` translates that into a user-facing log line. If you change either path, keep the sentinel string intact.

## Common commands

Run from `brunata_fetcher/` (the addon directory) unless noted.

```powershell
# One-time setup
pip install -r requirements.txt
python -m playwright install chromium      # ~300 MB download

# Smoke test — parser + MQTT payload shape, no network
python smoke_local.py

# End-to-end against the real portal (current production path)
python run_api_once.py

# End-to-end against the real portal via the legacy DOM scraper
python run_scraper_once.py

# Interactive non-headless explorer — logs in, opens Verbrauch, hands you
# the browser. Click around; all traffic captured to %TEMP%/portal_network.jsonl
python explore_portal.py

# One-shot probe of NP_UVI_SRV Bis/Datum parameter behavior
python explore_uvi_dates.py

# Compile-check before committing
python -m py_compile _brunata_api.py server.py
```

Local credentials live in [`brunata_fetcher/.env`](brunata_fetcher/.env) (gitignored). `BRUNATA_DEBUG=true` enables HTML/screenshot/network-log dumps to `tempfile.gettempdir()` during any run.

## Debug artifacts

When `BRUNATA_DEBUG=true` (either via `.env` for local runs, or `advanced.debug: true` in the addon options for container runs), both the API and scraper modules write to the system temp dir:

- `portal_debug{1..5}.{html,png}` — page snapshots during the scraper login flow
- `portal_network.jsonl` — every request/response with method/URL/status/body. POST bodies are captured **except** for the login URL (which contains the password). Multipart `$batch` bodies are captured in full so you can inspect inner OData GETs.

## Environment notes

- Local dev is Windows + PowerShell; the addon runtime is Debian Linux.
- Python 3.14 locally, 3.x (system) in the addon container.
- Long-running command: `python -m playwright install chromium` takes a few minutes.
- The repo's git identity is set per-repo (`Christoph Hornung <christoph.hornung@crosberg.de>`), not global.

## Conventions

- Default to ASCII in code and comments. The portal returns German Unicode (`Küche`, `Wohnzimmer`, `m³`) and those are preserved verbatim in data flow, but new code/comments should stay ASCII unless the file already uses Unicode.
- Prefer explicit, observable logging at INFO for login, fetch, and publish boundaries — this is what add-on users see in the Supervisor log and is the only debugging surface they have.

## Versioning + changelog

Always bump `version` in [`brunata_fetcher/config.yaml`](brunata_fetcher/config.yaml) and prepend an entry to [`brunata_fetcher/CHANGELOG.md`](brunata_fetcher/CHANGELOG.md) for any user-visible change. HA Supervisor uses the version field to drive update prompts.

**The CHANGELOG is user-facing.** HA shows it in the add-on's "What's new" panel before the user clicks Update. Write entries from the perspective of someone running the add-on in Home Assistant — what new entities or devices they'll see, behaviour changes that affect dashboards, install/upgrade impact. Skip module names, file paths, internal API names, SAP/OData terminology, container internals, or anything they can't observe from HA's UI. If a change has no observable effect for users (refactors, build-system tidy-up, dev-tooling additions, dependency bumps that don't change behaviour), either roll it into a release with user-visible changes and omit it from the entry, or skip the version bump entirely. The detailed technical "why" belongs in the commit message and the PR description, not the CHANGELOG.

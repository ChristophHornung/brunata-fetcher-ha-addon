# Brunata Fetcher Home Assistant Add-on

Home Assistant add-on that logs in to the Brunata München Nutzerportal,
pulls consumption data through the portal's OData API, and publishes
entities via MQTT Discovery.

Keywords: Brunata München Nutzerportal, BRUdirekt, BRUNATA-METRONA.

## Features

- Cookie-authed OData fetch (~10 s per cycle, no DOM-scrape brittleness)
- Energy types `Heizung`, `Kaltwasser`, `Warmwasser`
- **Per-room heating breakdown** in kWh, derived from the portal's
  `Raumvergleich` distribution × the heating YTD total
- **Building-average comparison** percentage per cost type (your usage as a
  % of the building average)
- Includes the portal's preliminary in-progress-month value, so the HA
  Energy Dashboard stays current between Brunata's monthly meter reads
- MQTT Discovery (no manual entity setup)
- Supervisor MQTT service discovery when manual MQTT settings are empty
- Portal-health binary sensor (`device_class: problem`) plus a persistent
  HA notification when fetches fail

## Requirements

- Home Assistant OS / Supervised with Add-on Store
- An MQTT broker reachable from HA (e.g. `core-mosquitto`)
- Brunata portal credentials (`email`, `password`)

## Installation

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add
   `https://github.com/ChristophHornung/brunata-fetcher-ha-addon`.
2. Find **Brunata Fetcher** in the store and click **Install**. First
   build takes a few minutes (Playwright + Chromium download into the
   container image).
3. Configure `email` and `password` (and MQTT options if not using
   Supervisor auto-discovery).
4. Start the add-on. Each fetch cycle runs in ~10 s; the default poll
   interval is 24 h.
5. New entities appear under the **BRUdirekt** MQTT device in HA.

## Configuration

Main options:

- `email` (required) — Brunata portal email
- `password` (required) — Brunata portal password
- `energy_types` — checkboxes for `Heizung`, `Kaltwasser`, `Warmwasser`
- `scan_interval_hours` — 1 to 168

Advanced options:

- `mqtt_host` / `mqtt_port` / `mqtt_user` / `mqtt_password` — leave empty
  to let the add-on resolve MQTT through Supervisor's `/services/mqtt`
- `debug` — when on, the add-on writes `portal_debug{1..5}.{html,png}`
  and `portal_network.jsonl` to the container's temp dir for diagnosis.
  Off by default.

The `scraper_url` advanced option is preserved for back-compat but no
longer drives behaviour — the OData endpoints are hard-coded.

## Published entities

A default setup (all three energy types, a typical apartment with six
rooms) gives you 16 entities under the **BRUdirekt** MQTT device.

Energy totals (`state_class: total_increasing`, Energy Dashboard ready):

- `sensor.brunata_fetcher_heizung` — kWh
- `sensor.brunata_fetcher_kaltwasser` — m³
- `sensor.brunata_fetcher_warmwasser` — kWh

Per-room heating (kWh, `total_increasing`, registered dynamically the
first time each room is seen):

- `sensor.brunata_fetcher_heizung_<room>` — one per room reported by the
  portal (typically Bad, Esszimmer, Kinderzimmer, Küche, Schlafzimmer,
  Wohnzimmer)

Building-average comparison (your value as a percentage of the building
average):

- `sensor.brunata_fetcher_heizung_vs_avg`
- `sensor.brunata_fetcher_kaltwasser_vs_avg`
- `sensor.brunata_fetcher_warmwasser_vs_avg`

Metadata + health:

- `sensor.brunata_fetcher_last_update` — date of the last fully-closed
  month the portal published (stable, ticks ~monthly)
- `sensor.brunata_fetcher_last_portal_query` — timestamp of the last
  fetch cycle
- `sensor.brunata_fetcher_next_portal_query` — timestamp of the next
  planned fetch
- `binary_sensor.brunata_fetcher_portal_query_problem` —
  `device_class: problem`. `ON` if the latest fetch failed validation,
  `OFF` otherwise; icon toggles between `mdi:check-decagram-outline` and
  `mdi:alert-decagram-outline`

You can rename any entity in HA without breaking the add-on — the
`unique_id` published over MQTT Discovery is the stable anchor.

## How query success is evaluated

A cycle is treated as successful only if:

- at least one configured energy value is present in the response, **and**
- `last_update_date` is present and plausible (`DD.MM.YYYY`, not in the
  future, not before 2000-01-01).

A failed cycle flips the health binary sensor to `ON` and fires a
persistent notification.

## Troubleshooting

- Add-on log first. Startup logs `SUPERVISOR_TOKEN present: true` and
  `MQTT broker connection acknowledged` on a healthy boot.
- `LOGIN_FAILED` in the log → check `email` / `password`.
- A `/BME/KP_MSG_CORE/224` 500 from the portal → likely an API change.
  See `docs/portal-api.md` for the reverse-engineered protocol, then
  flip `advanced.debug: true` to capture `portal_network.jsonl`.
- No entities appear → confirm the HA MQTT integration is loaded and the
  broker is reachable.

## Related projects and portal compatibility

These projects exist but target other Brunata portal stacks and won't
authenticate against the Munich BRUdirekt portal:

1. `Minol-MQTT-Bridge` — https://github.com/Gr4ph1xZ/Minol-MQTT-Bridge
   (`https://minolauth.b2clogin.com/`)
2. `hacs-brunata` — https://codeberg.org/YukiElectronics/hacs-brunata
   (`https://online.brunata.com/`, `brunatab2cprod.b2clogin.com/`)
3. `brunata-to-home-assistant` —
   https://github.com/patricklind/brunata-to-home-assistant
   (same portals as #2)

This add-on targets the Brunata München Nutzerportal (BRUdirekt) flow
only.

## Development notes

- Architecture overview + protocol reference: `docs/portal-api.md`
- Guidance for AI assistants / contributors: `CLAUDE.md`
- Production fetch path: `brunata_fetcher/_brunata_api.py`
- Legacy DOM-scraping path (kept for fallback / future investigation):
  `brunata_fetcher/_brunata_scraper.py`
- Release history: `brunata_fetcher/CHANGELOG.md`

Local development:

```bash
cp brunata_fetcher/.env.example brunata_fetcher/.env
# Edit BRUNATA_EMAIL + BRUNATA_PASSWORD

cd brunata_fetcher
pip install -r requirements.txt
python -m playwright install chromium     # one-time ~300 MB download

python smoke_local.py                     # parser + MQTT payload check, offline
python run_api_once.py                    # current production fetch path
python run_scraper_once.py                # legacy DOM scraper
python explore_portal.py                  # interactive non-headless explorer
```

`BRUNATA_DEBUG=true` in `.env` writes HTML / screenshot / network-log
dumps to the system temp dir during any of the runs above.

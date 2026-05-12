# Changelog

## 0.3.1

- Switched the container image to `playwright install chromium-headless-shell`
  instead of the full `chromium` build. The shell is ~100 MB instead of
  ~300 MB and `--with-deps` pulls a smaller set of system libs (no X11 /
  GTK / audio). Estimated image size reduction is ~400–700 MB.
  `server.py` always launches with `headless=True`, so the full Chromium
  build was unused weight.

## 0.3.0

- Replaced Playwright DOM scraping with a cookie-authed OData fetcher
  (`_brunata_api.py`). Playwright still handles the login itself, but data
  retrieval now goes through SAP's `NP_UVI_SRV/$batch` directly — full cycle
  ~10s instead of ~19s, no more selector-driven waits.
- Added per-room heating sensors in kWh (computed from `Anteil` × YTD total).
  One sensor per room found in `CumuConsumptionRoomSet`, discovered
  dynamically.
- Added "vs. Gebäude" comparison percentage sensors per energy type
  (`heizung_vs_avg`, `kaltwasser_vs_avg`, `warmwasser_vs_avg`).
- Documented the reverse-engineered portal API in `docs/portal-api.md`.
- The DOM-scraping path (`_brunata_scraper.py`) and the interactive explorer
  (`explore_portal.py`) are preserved for future investigations.

## 0.2.1

- Hardened failure detection and scraping sequence

## 0.2.0

- Added Supervisor MQTT service discovery with fallback to manual/default settings
- Moved MQTT and scraper URL settings into `advanced` options
- Improved startup reliability by waiting for MQTT connection acknowledgment before publishing
- Added portal query health monitoring via `binary_sensor` (`device_class: problem`)
- Added persistent notification on failed portal queries

## 0.1.4

- Set all known `energy_types` as default (`Heizung`, `Kaltwasser`, `Warmwasser`)
- Restrict `energy_types` option values to known types in add-on schema
- Add cleanup for disabled energy types by removing retained discovery/state topics

## 0.1.3

- Group Home Assistant discovery topics under `homeassistant/sensor/brunata_fetcher/*/config`
- Add per-type `suggested_display_precision` metadata
- Add timestamp entities for last and next planned portal query
- Improve publish reliability with retained MQTT publish helper and broker acknowledgement

## 0.1.2

- Add detailed runtime logging for startup, MQTT, scraping, and publish flow
- Harden add-on option schema (`email`, `password`, `port`, optional MQTT credentials)

## 0.1.1

- Switch to Home Assistant Debian base images for stable Playwright runtime
- Fix build configuration to use valid Home Assistant `build_from` image references

## 0.1.0

- Initial add-on implementation
- Brunata portal scraper via Playwright
- MQTT discovery and state publishing for Brunata sensors

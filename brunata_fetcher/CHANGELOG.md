# Changelog

## 0.3.3

- More reliable login on slower hardware (Raspberry Pi-class with an SD
  card). Cold-start fetches occasionally timed out before the Brunata
  portal had finished rendering; the add-on now waits for the page to
  settle and allows more time for the login step.

## 0.3.2

- Each room now shows up as its own device in Home Assistant
  ("Heizkostenverteiler Kinderzimmer", "Heizkostenverteiler Bad", …),
  nested under the main BRUdirekt device. You can assign each room
  device to its matching Area in HA for per-room dashboards. History
  on the existing per-room sensors is preserved across the upgrade.

## 0.3.1

- Add-on image is significantly smaller, so installs and updates pull
  less data and finish faster — useful on Pi-class hardware with an SD
  card.

## 0.3.0

- Faster portal polling: each fetch cycle takes around 10 seconds
  instead of ~20.
- New **per-room heating sensors**, in kWh — one per room the portal
  reports (typically Bad, Esszimmer, Kinderzimmer, Küche, Schlafzimmer,
  Wohnzimmer).
- New **building-average comparison** sensors per energy type: your
  consumption as a percentage of your building's average for Heizung,
  Kaltwasser and Warmwasser.
- The "Letztes Update" sensor now shows the last fully-closed month
  reported by the portal and stays stable until the next month closes;
  the energy values themselves keep updating daily as Brunata publishes
  preliminary current-month readings.

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

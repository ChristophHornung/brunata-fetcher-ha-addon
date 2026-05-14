# Changelog

## 0.4.0

- The add-on no longer bundles a browser. It now logs in to the Brunata
  portal directly over HTTP instead of driving a headless Chromium. The
  effect for you: the add-on image is dramatically smaller and faster to
  install and update, it uses noticeably less memory, and the login
  timeouts that could happen on slower hardware (Raspberry Pi on an SD
  card) are gone. Each fetch cycle is a bit quicker too. No configuration
  changes, and the entities in Home Assistant are exactly the same.

## 0.3.13

- Improved backfill reliability. The automatic seam-fix now waits for
  both Home Assistant's hourly statistics compile and the next live
  portal fetch before reconciling, so fresh installs no longer show a
  one-off spike where the imported history meets live data. Re-run the
  backfill after upgrading if the seam still looks off.

## 0.3.12

- Backfill's automatic seam-fix now actually catches the dip. In 0.3.10
  the fix ran 6 minutes after the backfill, which sometimes missed
  Home Assistant's hourly statistics compile — meaning the negative bar
  could still reappear in the Energy Dashboard. The fix now waits until
  just after the next full UTC hour (when HA writes its compiled values)
  and retries hourly for up to three hours if the live data hasn't
  arrived yet. Re-run the backfill after upgrading to clear any
  remaining dip.

## 0.3.11

- New weather-adjusted heating sensors. For every Heizung entity
  (the main total and each per-room sensor) there's now a
  "(witterungsbereinigt)" companion that normalises out year-over-year
  weather differences, so a cold January and a mild January compare
  meaningfully. The backfill also writes the full historical record
  for these new sensors. Because Brunata only computes weather
  adjustment for fully-closed months, the weather-adjusted variant
  always lags the raw one by up to a month — that's the portal's
  behaviour, not an addon bug.

## 0.3.10

- Backfill now auto-fixes the seam between historical and live data. The
  Energy Dashboard used to show a giant negative bar in the current
  month right after a backfill because Home Assistant briefly treated
  the imported history and the live polling as disconnected histories.
  About 6 minutes after a backfill finishes, the add-on now applies the
  same correction Home Assistant's built-in "Adjust a statistic" tool
  uses, automatically. You no longer need to fix anything manually after
  a backfill.

## 0.3.9

- Backfill now includes the current in-progress month too, distributing
  the portal's preliminary value across the days that have actually
  elapsed. Without this, there used to be a multi-day gap between the
  last backfilled day and the first live sample, and Home Assistant
  would sometimes render the gap as a giant negative bar in the Energy
  Dashboard. Re-run the backfill after upgrading to clear that out.

## 0.3.8

- Backfill now clears each entity's existing statistics before
  importing. Without this step, the historical data and the
  recent live-polling data could end up on independent cumulative
  baselines, producing a large negative bar in the Energy Dashboard
  for the month where they meet. Re-running the backfill after
  upgrading replaces the imported history cleanly.

## 0.3.7

- Historical backfill now respects entity renames. If you've renamed
  any of the Brunata sensors in Home Assistant (e.g. a per-room
  heating sensor to match your own room name), the backfill will
  write the historical statistics to the renamed entity instead of
  the original one. Re-run the backfill after upgrading to land the
  history on the right entity.

## 0.3.6

- More patience for the Brunata login page on slow hardware. Some
  Pi-class installs were hitting login timeouts even with the 0.3.3
  bump; the per-action wait is now generous enough that the addon
  waits rather than fails when SAPUI5 is being especially slow to
  bootstrap.

## 0.3.5

- Fixes the historical backfill introduced in 0.3.4: the import call
  was using the wrong Home Assistant API and reliably returned HTTP 400.
  It now uses the WebSocket recorder API correctly, and the backfill
  completes end-to-end.

## 0.3.4

- New: historical data backfill. The Brunata portal exposes monthly
  consumption back to 2023; you can pull all of it into Home Assistant
  as long-term statistics, so the Energy Dashboard shows
  year-over-year comparisons from day one instead of only what the
  add-on has polled since install. Per-room heating history is
  included too. Months are linearly distributed across their days, so
  daily and weekly views look smooth.

  To trigger it: Developer Tools → Actions → `mqtt.publish` →
  topic `brunata_fetcher/cmd/backfill`, empty payload. The backfill
  runs once in the background (a couple of minutes) and skips the
  current month so it doesn't conflict with live polling. You can
  re-run it later if you want — existing statistics at the same
  timestamps are replaced safely.

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

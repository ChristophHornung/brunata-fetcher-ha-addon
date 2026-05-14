# Changelog

## 0.4.0

- Login no longer requires a headless Chromium browser — the add-on now
  talks to the portal directly over HTTP. Smaller image, lower memory
  use, and more reliable, especially on Raspberry Pi.

## 0.3.13

- More reliable backfill: the automatic seam-fix now waits for live data
  before correcting, so fresh installs no longer show a spike where
  imported history meets live polling.

## 0.3.12

- Backfill's automatic seam-fix is now correctly timed to Home
  Assistant's hourly statistics update, so the Energy Dashboard dip no
  longer reappears.

## 0.3.11

- New weather-adjusted ("witterungsbereinigt") heating sensors — a
  companion to the main total and to each per-room sensor — so cold and
  mild years compare meaningfully. Their history is backfilled too.

## 0.3.10

- Backfill now fixes the seam between historical and live data
  automatically — no manual correction needed afterwards.

## 0.3.9

- Backfill now includes the current in-progress month, closing the gap
  to live data. Re-run the backfill after upgrading.

## 0.3.8

- Backfill now replaces existing statistics cleanly, preventing a large
  negative bar in the Energy Dashboard. Re-run the backfill after
  upgrading.

## 0.3.7

- Backfill now respects renamed sensors — history lands on the right
  entity. Re-run the backfill after upgrading.

## 0.3.6

- Fixes remaining login timeouts on slow hardware (Raspberry Pi on an
  SD card).

## 0.3.5

- Fixes the historical backfill — it now completes end-to-end.

## 0.3.4

- New: historical data backfill. Pull your Brunata consumption back to
  2023 into Home Assistant for year-over-year Energy Dashboard
  comparisons. Trigger it from Developer Tools → Actions →
  `mqtt.publish`, topic `brunata_fetcher/cmd/backfill` (empty payload).

## 0.3.3

- More reliable login on slower hardware (Raspberry Pi on an SD card).

## 0.3.2

- Each room now appears as its own device, so you can assign it to a
  Home Assistant Area. Existing history is preserved.

## 0.3.1

- Smaller add-on image — faster installs and updates.

## 0.3.0

- Faster polling — about 10 seconds per cycle instead of 20.
- New per-room heating sensors, in kWh.
- New building-average comparison sensors — your usage as a percentage
  of the building average.
- "Letztes Update" now shows the last fully-closed month and stays
  stable until the next one closes.

## 0.2.1

- Hardened failure detection.

## 0.2.0

- Automatic MQTT setup via Supervisor, with manual fallback.
- New portal-health sensor plus a notification when a fetch fails.
- More reliable startup.

## 0.1.4

- All three energy types enabled by default; disabled ones are now
  cleaned up properly.

## 0.1.3

- New "last" and "next portal query" timestamp sensors.
- More reliable MQTT publishing.

## 0.1.2

- More detailed logging and stricter option validation.

## 0.1.1

- Switched to Home Assistant Debian base images for a more stable
  runtime.

## 0.1.0

- Initial release: fetches Brunata portal data and publishes it as Home
  Assistant sensors via MQTT.

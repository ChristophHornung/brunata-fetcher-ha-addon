# Brunata Fetcher

Pulls heating, cold-water and warm-water consumption from the Brunata
München Nutzerportal (BRUdirekt) and publishes it to Home Assistant as
MQTT sensors — ready for the Energy Dashboard.

## Configuration

| Option | Description |
|---|---|
| `email` | Your Brunata portal email (required) |
| `password` | Your Brunata portal password (required) |
| `energy_types` | Which consumption types to fetch — Heizung, Kaltwasser, Warmwasser |
| `scan_interval_hours` | How often to poll the portal (1–168, default 24) |

The advanced `mqtt_*` options only need filling in if you're **not** using
the Home Assistant MQTT add-on — leave them empty and the broker is
resolved automatically.

## What you get

Entities appear under the **BRUdirekt** device, plus one device per room:

- Heating, cold-water and warm-water totals — Energy Dashboard ready
- A weather-adjusted ("witterungsbereinigt") companion for each heating
  sensor
- Per-room heating breakdown in kWh
- Building-average comparison — your usage as a percentage of the
  building average
- A portal-health sensor, plus a notification if a fetch fails

You can rename any entity in Home Assistant without breaking the add-on.

## Historical backfill

To pull your consumption history back to 2023 into the Energy Dashboard:

**Developer Tools → Actions → `mqtt.publish`**, topic
`brunata_fetcher/cmd/backfill`, empty payload, then **Perform Action**.

It runs once in the background (a couple of minutes) and is safe to
re-run.

## Troubleshooting

- Check the add-on log first.
- `LOGIN_FAILED` in the log → check your `email` and `password`.
- No entities appear → make sure the Home Assistant MQTT integration is
  set up and the broker is reachable.
- Values haven't changed in days → almost always normal. Brunata
  recomputes the current month's "Prognose" (forecast) on an irregular
  schedule and it can sit unchanged for several days at a time. Compare
  against the Prognose number in the Brunata portal UI to confirm.

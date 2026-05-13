#!/usr/bin/env python3
"""Local smoke checks for Brunata add-on publishing and parsing.

This script validates two core behaviors without requiring a real MQTT broker
or Brunata login:
1. MQTT Discovery and state payload/topic generation
2. German number parsing for common portal formats

Run from this directory:
    python3 smoke_local.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from _brunata_scraper import _parse_german_number
from server import (
    _clear_removed_energy_type_entities,
    _publish_portal_query_problem_state,
    _publish_discovery,
    _publish_schedule_state,
    _publish_state,
    _slug_for_room,
    _validate_scrape_result,
)


class CapturingMqttClient:
    """Minimal MQTT client test double for publish capture."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []

    class _Info:
        def __init__(self) -> None:
            self.rc = 0

        def wait_for_publish(self) -> None:
            return

    def publish(
        self,
        topic: str,
        payload: str,
        qos: int = 0,
        retain: bool = False,
    ) -> _Info:
        """Capture publish calls in-memory."""
        _ = qos
        self.published.append((topic, payload, retain))
        return self._Info()


def _assert_parser() -> None:
    """Validate German number parsing behavior."""
    cases = {
        "1.234,56": 1234.56,
        "2.150,0 kWh": 2150.0,
        "13,25 m³": 13.25,
        "0,5": 0.5,
    }
    for raw, expected in cases.items():
        value = _parse_german_number(raw)
        if value != expected:
            raise AssertionError(f"Parser mismatch for '{raw}': {value} != {expected}")


def _assert_discovery_and_state() -> None:
    """Validate discovery/state topic and payload shape."""
    client = CapturingMqttClient()

    energy_types = ["Heizung", "Kaltwasser"]
    _publish_discovery(client, energy_types)
    _clear_removed_energy_type_entities(client, energy_types)
    rooms_discovered: set[str] = set()
    _publish_state(
        client,
        {
            "Heizung": 2150.0,
            "Heizung_witterungsbereinigt": 1980.0,
            "Kaltwasser": 12.5,
            "last_update_date": "28.02.2026",
            "comparison_pct": {
                "Heizung": 151.3,
                "Kaltwasser": 109.2,
            },
            "rooms_kwh": {
                "Kinderzimmer": 691.0,
                "Küche": 94.0,
            },
            "rooms_kwh_witterungsbereinigt": {
                "Kinderzimmer": 636.6,
                "Küche": 86.6,
            },
            "rooms_pct": {
                "Kinderzimmer": 32.15,
                "Küche": 4.37,
            },
        },
        energy_types,
        rooms_discovered=rooms_discovered,
    )
    _publish_schedule_state(
        client,
        datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
        datetime(2026, 3, 2, 10, 0, tzinfo=UTC) + timedelta(minutes=1),
    )
    _publish_portal_query_problem_state(client, True)

    topics = [topic for topic, _, _ in client.published]
    expected_topics = {
        "homeassistant/sensor/brunata_fetcher/heizung/config",
        "homeassistant/sensor/brunata_fetcher/heizung_vs_avg/config",
        "homeassistant/sensor/brunata_fetcher/heizung_wb/config",
        "homeassistant/sensor/brunata_fetcher/kaltwasser/config",
        "homeassistant/sensor/brunata_fetcher/kaltwasser_vs_avg/config",
        "homeassistant/sensor/brunata_fetcher/warmwasser/config",
        "homeassistant/sensor/brunata_fetcher/last_update/config",
        "homeassistant/sensor/brunata_fetcher/last_portal_query/config",
        "homeassistant/sensor/brunata_fetcher/next_portal_query/config",
        "homeassistant/binary_sensor/brunata_fetcher/portal_query_problem/config",
        "homeassistant/sensor/brunata_fetcher/heizung_kinderzimmer/config",
        "homeassistant/sensor/brunata_fetcher/heizung_kinderzimmer_wb/config",
        "homeassistant/sensor/brunata_fetcher/heizung_kueche/config",
        "homeassistant/sensor/brunata_fetcher/heizung_kueche_wb/config",
        "brunata_fetcher/sensor/heizung/state",
        "brunata_fetcher/sensor/heizung_vs_avg/state",
        "brunata_fetcher/sensor/heizung_wb/state",
        "brunata_fetcher/sensor/kaltwasser/state",
        "brunata_fetcher/sensor/kaltwasser_vs_avg/state",
        "brunata_fetcher/sensor/last_update/state",
        "brunata_fetcher/sensor/last_portal_query/state",
        "brunata_fetcher/sensor/next_portal_query/state",
        "brunata_fetcher/binary_sensor/portal_query_problem/state",
        "brunata_fetcher/sensor/heizung_kinderzimmer/state",
        "brunata_fetcher/sensor/heizung_kinderzimmer_wb/state",
        "brunata_fetcher/sensor/heizung_kueche/state",
        "brunata_fetcher/sensor/heizung_kueche_wb/state",
    }

    missing = expected_topics - set(topics)
    if missing:
        raise AssertionError(f"Missing expected MQTT topics: {sorted(missing)}")

    if rooms_discovered != {"Kinderzimmer", "Küche"}:
        raise AssertionError(
            f"Unexpected rooms_discovered set: {rooms_discovered}"
        )

    if _slug_for_room("Küche") != "kueche":
        raise AssertionError("Slug for Küche should fold to 'kueche'")
    if _slug_for_room("Wohnzimmer Süd") != "wohnzimmer_sued":
        raise AssertionError("Slug should ASCII-fold + replace spaces")

    discovery_payload = next(
        payload
        for topic, payload, _ in client.published
        if topic == "homeassistant/sensor/brunata_fetcher/heizung/config"
    )
    discovery = json.loads(discovery_payload)
    if discovery["state_topic"] != "brunata_fetcher/sensor/heizung/state":
        raise AssertionError("Unexpected state_topic in Heizung discovery payload")
    if discovery["unit_of_measurement"] != "kWh":
        raise AssertionError("Unexpected unit_of_measurement in Heizung payload")
    if discovery["suggested_display_precision"] != 0:
        raise AssertionError(
            "Unexpected suggested_display_precision in Heizung payload"
        )

    cold_water_payload = next(
        payload
        for topic, payload, _ in client.published
        if topic == "homeassistant/sensor/brunata_fetcher/kaltwasser/config"
    )
    cold_water_discovery = json.loads(cold_water_payload)
    if cold_water_discovery["suggested_display_precision"] != 1:
        raise AssertionError(
            "Unexpected suggested_display_precision in Kaltwasser payload"
        )

    # Per-room entities each live on their own logical device, linked back
    # to the main BRUdirekt device via via_device.
    room_payload = next(
        payload
        for topic, payload, _ in client.published
        if topic == "homeassistant/sensor/brunata_fetcher/heizung_kinderzimmer/config"
    )
    room_discovery = json.loads(room_payload)
    room_device = room_discovery["device"]
    if room_device.get("identifiers") != ["brunata_fetcher_room_kinderzimmer"]:
        raise AssertionError(
            f"Per-room device must have its own identifiers, got: {room_device}"
        )
    if room_device.get("via_device") != "brunata_fetcher":
        raise AssertionError(
            f"Per-room device must link to main device via via_device, got: {room_device}"
        )
    if "Kinderzimmer" not in room_device.get("name", ""):
        raise AssertionError(
            f"Per-room device name should include room label, got: {room_device.get('name')}"
        )
    if room_discovery.get("name") != "Heizung":
        raise AssertionError(
            f"Per-room entity name should be 'Heizung', got: {room_discovery.get('name')}"
        )

    # The main consumption entities stay on the BRUdirekt device.
    heating_total_payload = next(
        payload
        for topic, payload, _ in client.published
        if topic == "homeassistant/sensor/brunata_fetcher/heizung/config"
    )
    main_device = json.loads(heating_total_payload)["device"]
    if main_device.get("identifiers") != ["brunata_fetcher"]:
        raise AssertionError(
            f"Main consumption entity must stay on BRUdirekt device, got: {main_device}"
        )

    if not all(retain for _, _, retain in client.published):
        raise AssertionError("All publish calls must be retained for this smoke test")


def _assert_result_validation() -> None:
    """Validate success/failure classification for scrape results."""
    ok, reason = _validate_scrape_result(
        {
            "Heizung": 2150.0,
            "Kaltwasser": None,
            "last_update_date": "28.02.2026",
        },
        ["Heizung", "Kaltwasser"],
    )
    if not ok:
        raise AssertionError(f"Expected valid result, got invalid: {reason}")

    ok, _ = _validate_scrape_result(
        {
            "Heizung": None,
            "Kaltwasser": None,
            "last_update_date": "28.02.2026",
        },
        ["Heizung", "Kaltwasser"],
    )
    if ok:
        raise AssertionError("Expected invalid result when no configured energy values exist")

    ok, _ = _validate_scrape_result(
        {
            "Heizung": 10.0,
            "last_update_date": "2099-02-28",
        },
        ["Heizung"],
    )
    if ok:
        raise AssertionError("Expected invalid result for malformed last_update_date")


def main() -> None:
    """Run local smoke checks and print a short result."""
    _assert_parser()
    _assert_discovery_and_state()
    _assert_result_validation()
    print("Smoke test passed: parser and MQTT payload generation look good")


if __name__ == "__main__":
    main()

"""The feature declaration arrives over MQTT when the REST entry has lost it.

Since 2026-10-26 the live NR-67D's REST entry carries ``Properties.did: null``. Its answer to
status/start still holds ``payload.response.feature`` and ``macAddress`` but no ``status``,
and that answer was being dropped as ``dropped_no_status``, so every feature-gated entity
and the gas query stayed away.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from harness import Report
from navien_smarthome.boiler import (
    BOILER_FEATURE_UPDATE,
    BoilerDevice,
    extract_boiler_status,
)
from navien_smarthome.coordinator import NavienSmartCoordinator
from navien_smarthome.mqtt import NavienSmartMqtt

r = Report()

PHYSICAL_ID = "001122334455"
FEATURE = {
    "powerUse": 2,
    "ondolUse": 2,
    "ondolTemperatureMin": 60,
    "ondolTemperatureMax": 130,
    "hotWaterTemperatureSettingUse": 2,
    "hotWaterTemperatureMin": 60,
    "hotWaterTemperatureMax": 120,
    "fastDHWUse": 2,
    "smartFastDHWUse": 2,
    "DHWBoostUse": 2,
    "gasUsageUse": 2,
}
RAW_DID_NULL: dict[str, Any] = {
    "deviceId": "0011223344556272",
    "deviceSeq": 14,
    "serviceCode": 100,
    "modelCode": "20",
    "modelName": "NR-67D",
    "connected": 1,
    "Properties": {"nickName": "테스트 보일러", "did": None},
}


def envelope(response: dict[str, Any]) -> bytes:
    return json.dumps(
        {"topic": "private-response-topic", "payload": {"response": response}}
    ).encode()


def parse_did_null() -> BoilerDevice:
    device = BoilerDevice.parse(copy.deepcopy(RAW_DID_NULL))
    assert device is not None
    return device


class ImmediateLoop:
    def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
        callback(*args)


def make_coordinator(*boilers: BoilerDevice) -> NavienSmartCoordinator:
    hass = SimpleNamespace(loop=ImmediateLoop())
    coordinator = NavienSmartCoordinator(hass, Mock(), Mock(), 7)  # type: ignore[arg-type]
    # The offline stub carries no DataUpdateCoordinator behaviour; these are its attributes.
    coordinator.hass = hass  # type: ignore[assignment]
    coordinator.data = {}
    coordinator.update_interval = timedelta(minutes=15)
    coordinator._debounced_refresh = SimpleNamespace(cooldown=0)  # type: ignore[assignment]
    coordinator.async_update_listeners = Mock()
    coordinator.boilers = {boiler.device_id: boiler for boiler in boilers}
    return coordinator


def mqtt_for(coordinator: NavienSmartCoordinator) -> NavienSmartMqtt:
    return NavienSmartMqtt(
        coordinator.hass,
        7,
        42,
        {"smarttok"},
        AsyncMock(return_value=None),
        lambda *_: None,
        on_boiler_reported=coordinator._handle_boiler_reported,
        on_boiler_observation=lambda *_: None,
    )


def receive(client: NavienSmartMqtt, response: dict[str, Any]) -> None:
    client._on_message(
        None,
        None,
        SimpleNamespace(topic="7/smarttok/device", payload=envelope(response)),
    )


r.section("B6(a): a feature-only MQTT answer declares the features of a did-null boiler")

boiler = parse_did_null()
r.ok(boiler.feature == {}, "a REST entry with Properties.did null carries no feature")
r.ok(
    not boiler.supports_feature("powerUse")
    and boiler.temperature_bounds("hot_water") is None,
    "before the answer nothing is declared",
)
boiler.apply_status({"operationMode": 2}, now=100.0)
coordinator = make_coordinator(boiler)
client = mqtt_for(coordinator)
receive(client, {"macAddress": PHYSICAL_ID, "feature": FEATURE})
r.ok(
    boiler.supports_feature("powerUse")
    and boiler.temperature_bounds("hot_water") == (30.0, 60.0)
    and boiler.temperature_bounds("ondol") == (30.0, 65.0)
    and boiler.supports_feature("gasUsageUse"),
    "supports_feature and temperature_bounds answer from the MQTT feature",
)
r.ok(boiler.physical_device_id == PHYSICAL_ID, "the single boiler takes the macAddress")
r.ok(
    client.stats.get("boiler_feature_accepted") == 1
    and "boiler_accepted" not in client.stats
    and "boiler_dropped_no_status" not in client.stats,
    "a feature-only message is counted as feature_accepted",
)
r.ok(
    boiler.status == {"operationMode": 2}
    and boiler.status_received_at == 100.0
    and boiler.last_communication_at == 100.0
    and boiler.gas_meter == {}
    and boiler.gas_received_at is None,
    "a feature-only message leaves status, gas data and timestamps alone",
)
r.ok(
    coordinator.async_update_listeners.call_count == 1,  # type: ignore[attr-defined]
    "listeners are told so late entities can appear",
)

coordinator.api.async_get_devices = AsyncMock(
    return_value=[copy.deepcopy(RAW_DID_NULL)]
)
asyncio.run(coordinator._async_collect())
rebuilt = coordinator.boilers[boiler.device_id]
r.ok(rebuilt is not boiler, "the poll rebuilds the device object")
r.ok(
    rebuilt.feature == FEATURE and rebuilt.supports_feature("powerUse"),
    "a REST rebuild with Properties.did null keeps the MQTT feature",
)

with_feature = copy.deepcopy(RAW_DID_NULL)
with_feature["Properties"]["did"] = {"response": {"feature": {"powerUse": 1}}}
coordinator.api.async_get_devices = AsyncMock(return_value=[with_feature])
asyncio.run(coordinator._async_collect())
r.ok(
    coordinator.boilers[boiler.device_id].feature == {"powerUse": 1},
    "a REST entry with a non-empty feature replaces it",
)
for label, did in (
    ("missing response", {}),
    ("feature not an object", {"response": {"feature": [1]}}),
    ("empty feature", {"response": {"feature": {}}}),
):
    entry = copy.deepcopy(RAW_DID_NULL)
    entry["Properties"]["did"] = did
    coordinator.api.async_get_devices = AsyncMock(return_value=[entry])
    asyncio.run(coordinator._async_collect())
    r.ok(
        coordinator.boilers[boiler.device_id].feature == {"powerUse": 1},
        f"a REST entry with {label} keeps the known feature",
    )


r.section("B1: parsing and matching")

stats: dict[str, Any] = {}
parsed = extract_boiler_status(
    envelope({"macAddress": PHYSICAL_ID, "status": {"operationMode": 2}, "feature": FEATURE}),
    stats,
)
r.ok(
    parsed == (PHYSICAL_ID, {"operationMode": 2, BOILER_FEATURE_UPDATE: FEATURE})
    and stats == {"accepted": 1},
    "status with feature carries both and counts as accepted",
)
stats = {}
parsed = extract_boiler_status(
    envelope({"macAddress": PHYSICAL_ID, "gasMeter": {"m": 1}, "feature": FEATURE}), stats
)
r.ok(
    parsed is not None
    and parsed[1].get(BOILER_FEATURE_UPDATE) == FEATURE
    and stats == {"accepted": 1},
    "gasMeter with feature carries both and counts as accepted",
)
for label, feature in (("not an object", [1, 2]), ("an empty object", {}), ("a string", "x")):
    stats = {}
    r.ok(
        extract_boiler_status(
            envelope({"macAddress": PHYSICAL_ID, "feature": feature}), stats
        )
        is None
        and stats == {"dropped_no_status": 1},
        f"a feature that is {label} alone is dropped as dropped_no_status",
    )
    stats = {}
    parsed = extract_boiler_status(
        envelope({"macAddress": PHYSICAL_ID, "status": {"a": 1}, "feature": feature}),
        stats,
    )
    r.ok(
        parsed == (PHYSICAL_ID, {"a": 1}) and stats == {"accepted": 1},
        f"a feature that is {label} adds nothing to a status",
    )
stats = {}
r.ok(
    extract_boiler_status(envelope({"feature": FEATURE}), stats) is None
    and stats == {"dropped_no_device_id": 1},
    "a feature without macAddress is dropped_no_device_id",
)

two_a = parse_did_null()
two_b = parse_did_null()
two_b.device_id = "0011223344559999"
pair = make_coordinator(two_a, two_b)
pair_client = mqtt_for(pair)
receive(pair_client, {"macAddress": PHYSICAL_ID, "feature": FEATURE})
r.ok(
    pair.drop_counts["boiler_no_device"] == 1 and two_a.feature == {} and two_b.feature == {},
    "with two boilers and no physical id match the feature counts as boiler_no_device",
)
two_b.physical_device_id = PHYSICAL_ID
receive(pair_client, {"macAddress": PHYSICAL_ID, "feature": FEATURE})
r.ok(two_b.feature == FEATURE and two_a.feature == {}, "the macAddress match picks the boiler")

both = parse_did_null()
both_coordinator = make_coordinator(both)
both_coordinator._schedule_boiler_silence_check = Mock()
receive(
    mqtt_for(both_coordinator),
    {"macAddress": PHYSICAL_ID, "status": {"operationMode": 2}, "feature": FEATURE},
)
r.ok(
    both.feature == FEATURE
    and both.status == {"operationMode": 2}
    and both.status_received_at is not None,
    "a message with feature and status applies both",
)
replaced = parse_did_null()
replaced.feature = {"powerUse": 2, "gasUsageUse": 2}
replaced.apply_status({BOILER_FEATURE_UPDATE: {"powerUse": 1}})
r.ok(replaced.feature == {"powerUse": 1}, "a new declaration replaces the previous one")


sys.exit(r.finish())

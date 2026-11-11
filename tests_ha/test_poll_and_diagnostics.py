"""Cover poll rebuild retention and diagnostics privacy with real HA objects."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.navien_smarthome.boiler import observe_boiler_message
from custom_components.navien_smarthome.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_poll_rebuild_retains_live_state_and_records(
    hass: HomeAssistant, loaded_entry
) -> None:
    """Fresh REST objects retain every field that belongs to the MQTT lifecycle."""
    coordinator = loaded_entry.runtime_data
    mat = coordinator.data["AABBCCDDEEFF"]
    airone = coordinator.airone["AIRONE001"]
    boiler = coordinator.boilers["0011223344556272"]

    mat.note_command({"operationMode": 1})
    airone.note_command("power", {"roomController": {"running": 1}})
    airone.air_sensor_errors = 3
    boiler.gas_meter = {"thisYearMonthTotalGasUsage": 74}
    boiler.gas_received_at = 123.0
    boiler.apply_status(
        {"command": 33554438, "mqttOnlyField": 7, "operationMode": 2},
        now=150.0,
    )
    rest_boiler = deepcopy(boiler.raw)
    del rest_boiler["Properties"]["did"]["response"]["macAddress"]
    rest_boiler["Properties"]["did"]["response"]["status"] = {
        "operationMode": 4,
        "restOnlyField": 9,
    }

    coordinator.api.async_get_devices = AsyncMock(
        return_value=[mat.raw, airone.raw, rest_boiler]
    )
    coordinator._async_update_air_sensors = AsyncMock()

    rebuilt = await coordinator._async_collect()
    rebuilt_mat = rebuilt[mat.device_id]
    rebuilt_airone = coordinator.airone[airone.device_id]
    rebuilt_boiler = coordinator.boilers[boiler.device_id]

    assert rebuilt_mat is not mat
    assert rebuilt_mat.reported == mat.reported
    assert rebuilt_mat.command_log == mat.command_log
    assert rebuilt_airone is not airone
    assert rebuilt_airone.reported == airone.reported
    assert rebuilt_airone.air_sensors == airone.air_sensors
    assert rebuilt_airone.known_sensor_kinds == airone.known_sensor_kinds
    assert rebuilt_airone.air_sensor_errors == 3
    assert rebuilt_boiler is not boiler
    assert rebuilt_boiler.status["mqttOnlyField"] == 7
    assert rebuilt_boiler.status["command"] == 33554438
    assert rebuilt_boiler.status["restOnlyField"] == 9
    assert rebuilt_boiler.status["operationMode"] == 2
    assert rebuilt_boiler.status_received_at == 150.0
    assert rebuilt_boiler.physical_device_id == boiler.physical_device_id
    assert rebuilt_boiler.observed_commands == {33554438: 1}
    assert rebuilt_boiler.gas_meter == boiler.gas_meter
    assert rebuilt_boiler.gas_received_at == 123.0

    with patch.object(coordinator, "_schedule_boiler_silence_check"):
        coordinator._handle_boiler_reported(
            boiler.physical_device_id or "", {"mqttAfterPoll": 11}
        )
    assert rebuilt_boiler.status["mqttAfterPoll"] == 11


async def test_diagnostics_redacts_keys_and_embedded_identifiers(
    hass: HomeAssistant, loaded_entry
) -> None:
    """Sensitive values cannot survive either as fields or inside MQTT topics."""
    coordinator = loaded_entry.runtime_data
    secret = "SECRETDEVICE123456"
    raw = coordinator.raw_devices[0]
    raw["deviceId"] = secret
    raw["mqttTopicKey"] = "PRIVATE-TOPIC-123"
    raw["requestTopic"] = f"dt/rc/7/{secret}/did"
    raw["Properties"]["nickName"] = {"mainItem": "홍길동의 침실"}
    coordinator.raw_devices.append(
        {
            "serviceCode": "future-900",
            "modelName": "Unknown",
            "modelCode": "900",
        }
    )

    result = await async_get_config_entry_diagnostics(hass, loaded_entry)
    rendered = json.dumps(result, ensure_ascii=False)

    assert secret not in rendered
    assert "PRIVATE-TOPIC-123" not in rendered
    assert "홍길동의 침실" not in rendered
    assert "**REDACTED**" in rendered
    assert result["out_of_scope_devices"][-1]["serviceCode"] == "future-900"


@pytest.mark.parametrize("sequence", [1234, "1234"])
async def test_diagnostics_redacts_boiler_sequence_fields(
    hass: HomeAssistant, loaded_entry, sequence: int | str
) -> None:
    """New sequence keys fail on the base; existing redactions and state are preserved."""
    coordinator = loaded_entry.runtime_data
    raw = next(item for item in coordinator.raw_devices if item["serviceCode"] == 100)
    raw["deviceSeq"] = 987654
    raw["roomContorllerSeq"] = "SYNTHETIC-ROOM-SEQ"
    raw["additionalValue"] = "7B3F"
    for key in ("homeSeq", "userSeq", "roomControllerSeq"):
        raw[key] = sequence
    nested = raw["Properties"]["did"]["response"]
    nested["additionalValue"] = "7B3F"

    result = await async_get_config_entry_diagnostics(hass, loaded_entry)
    entry = next(
        item for item in result["supported_devices"] if item["serviceCode"] == 100
    )

    assert entry["deviceSeq"] == "**REDACTED**"
    assert entry["roomContorllerSeq"] == "**REDACTED**"
    assert entry["additionalValue"] == "**REDACTED**"
    for key in ("homeSeq", "userSeq", "roomControllerSeq"):
        assert entry[key] == "**REDACTED**"
    nested_entry = entry["Properties"]["did"]["response"]
    assert nested_entry["additionalValue"] == "**REDACTED**"
    assert entry["serviceCode"] == 100
    assert entry["modelCode"] == "20"
    assert nested_entry["feature"]["powerUse"] == 2
    rendered = json.dumps(result, ensure_ascii=False)
    assert "SYNTHETIC-ROOM-SEQ" not in rendered
    assert "7B3F" not in rendered


@pytest.mark.parametrize("with_identifier", [False, True])
async def test_diagnostics_redacts_mqtt_shapes_without_key_names(
    hass: HomeAssistant, loaded_entry, with_identifier: bool
) -> None:
    """Shape assertions fail on the base, even without identifiers; other data is preserved."""
    coordinator = loaded_entry.runtime_data
    client_id = "12345678-abcd-4abc-8def-123456789abc"
    prefixes = ("smarttok", "mate", "airone", "scada", "homeauto")
    raw: dict[str, Any] = {
        "serviceCode": 100,
        "diagnosticText": {
            "clients": f"before{client_id}after and {client_id.upper()}-U2345 connected",
            "topics": [f"topic 12345/{prefix}/x ready" for prefix in prefixes],
            "topic_ends": [f"topic 12345/{prefix}" for prefix in prefixes],
            "ordinary": "ordinary system status",
            "number": 17,
            "flag": True,
            "missing": None,
            "non_topics": [
                "x12345/smarttok/x",
                "_12345/mate/x",
                "12345/airone-extra/x",
                "12345/scadaextra",
                "12345/homeautomatic/x",
            ],
        },
    }
    if with_identifier:
        raw["deviceId"] = "DEVICE-SYNTHETIC-ABC"
    coordinator.raw_devices = [raw]

    result = await async_get_config_entry_diagnostics(hass, loaded_entry)
    exported = result["supported_devices"][0]["diagnosticText"]

    assert exported["clients"] == "before**REDACTED**after and **REDACTED** connected"
    assert exported["topics"] == [
        f"topic **REDACTED**/{prefix}/x ready" for prefix in prefixes
    ]
    assert exported["topic_ends"] == [
        f"topic **REDACTED**/{prefix}" for prefix in prefixes
    ]
    for key in ("ordinary", "number", "flag", "missing", "non_topics"):
        assert exported[key] == raw["diagnosticText"][key]


async def test_diagnostics_redacts_client_id_overlapping_collected_uuid(
    hass: HomeAssistant, loaded_entry
) -> None:
    """Fail on the base and literal-first scrubbing, which can leave the member suffix."""
    coordinator = loaded_entry.runtime_data
    client_id = "12345678-abcd-4abc-8def-123456789abc"
    coordinator.raw_devices = [
        {
            "serviceCode": 100,
            "clientId": client_id,
            "diagnosticText": f"before {client_id}-U2345 after",
        }
    ]

    result = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert result["supported_devices"][0]["diagnosticText"] == "before **REDACTED** after"


async def test_diagnostics_redacts_collected_topic_overlapping_home_number(
    hass: HomeAssistant, loaded_entry
) -> None:
    """Preserve the base's whole-topic redaction against a shape-first regression."""
    coordinator = loaded_entry.runtime_data
    topic = "12345/smarttok/DEVICE-SYNTHETIC-ABC/status"
    coordinator.raw_devices = [
        {
            "serviceCode": 100,
            "requestTopic": topic,
            "diagnosticText": f"before {topic} after",
        }
    ]

    result = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert result["supported_devices"][0]["diagnosticText"] == "before **REDACTED** after"


@pytest.mark.parametrize(
    ("secret", "text"),
    [
        ("DEVICE-DEVICE", "DEVICE-DEVICE-DEVICE"),
        (
            "DEVICE-SYNTHETIC-ABC",
            "DEVICE-SYNTHETIC-ABC12345678-abcd-4abc-8def-123456789abc-U2345",
        ),
        (
            "DEVICE-SYNTHETIC-ABC",
            "11111111-2222-3333-4444-555566667777-8888-9999-aaaa-bbbbbbbbbbbb-U2345",
        ),
    ],
)
async def test_diagnostics_merges_overlapping_and_touching_spans(
    hass: HomeAssistant, loaded_entry, secret: str, text: str
) -> None:
    """Regress self-overlapping literals, touching spans and overlapping client-id shapes."""
    coordinator = loaded_entry.runtime_data
    coordinator.raw_devices = [
        {"serviceCode": 100, "deviceId": secret, "diagnosticText": f"before {text} after"}
    ]

    result = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert result["supported_devices"][0]["diagnosticText"] == "before **REDACTED** after"


async def test_diagnostics_redacts_boiler_observation_sequences(
    hass: HomeAssistant, loaded_entry
) -> None:
    """New numeric and decimal-string sequence redactions fail on the base; mode stays."""
    coordinator = loaded_entry.runtime_data
    keys = ("homeSeq", "userSeq", "roomControllerSeq")
    for sequence in (1234, "1234", None):
        payload = {key: sequence for key in keys}
        payload["mode"] = 2
        coordinator._handle_boiler_observation(
            observe_boiler_message(
                json.dumps({"payload": payload}).encode(),
                "12345/smarttok/DEVICE-SYNTHETIC-ABC/status",
            )
        )

    result = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert len(result["boiler_observations"]) == 3
    for observation in result["boiler_observations"]:
        shape = observation["shape"]["payload"]
        for key in keys:
            assert shape[key] == {"kind": "redacted"}
        assert shape["mode"] == 2

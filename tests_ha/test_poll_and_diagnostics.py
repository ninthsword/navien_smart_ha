"""Cover poll rebuild retention and diagnostics privacy with real HA objects."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

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

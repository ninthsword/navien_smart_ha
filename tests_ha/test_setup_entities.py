"""Verify setup lifecycle and entity contracts against Home Assistant 2026.8."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant

from .conftest import entity_id_for_unique_id


async def test_setup_unload_and_reload_listener(
    hass: HomeAssistant, loaded_entry
) -> None:
    """The entry loads, reloads on an update, and unloads all platforms."""
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert loaded_entry.runtime_data.data
    assert loaded_entry.runtime_data.airone
    assert loaded_entry.runtime_data.boilers

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as reload_entry:
        hass.config_entries.async_update_entry(
            loaded_entry, data={**loaded_entry.data, "password": "changed"}
        )
        await hass.async_block_till_done()
        reload_entry.assert_awaited_once_with(loaded_entry.entry_id)

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED


async def test_representative_entity_state_metadata_and_unique_ids(
    hass: HomeAssistant, loaded_entry
) -> None:
    """Mat, Airone, and boiler entities expose stable HA state metadata."""
    mat = entity_id_for_unique_id(hass, "climate", "AABBCCDDEEFF_left_thermostat")
    mat_state = hass.states.get(mat)
    assert mat_state is not None
    assert mat_state.state == "heat"
    assert mat_state.attributes["current_temperature"] == 34.5
    assert mat_state.attributes["temperature"] == 35.0

    air = entity_id_for_unique_id(hass, "sensor", "AIRONE001_air_co2")
    air_state = hass.states.get(air)
    assert air_state is not None
    assert air_state.state == "605.0"
    assert air_state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "ppm"
    assert air_state.attributes[ATTR_DEVICE_CLASS] == "carbon_dioxide"
    assert air_state.attributes["kind"] == "co2"

    boiler = entity_id_for_unique_id(
        hass, "sensor", "0011223344556272_indoor_temperature"
    )
    boiler_state = hass.states.get(boiler)
    assert boiler_state is not None
    assert boiler_state.state == "21.5"
    assert boiler_state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "°C"
    assert boiler_state.attributes[ATTR_DEVICE_CLASS] == "temperature"
    assert boiler_state.attributes["state_class"] == "measurement"

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    assert registry.async_get(mat).unique_id == "AABBCCDDEEFF_left_thermostat"
    assert registry.async_get(air).unique_id == "AIRONE001_air_co2"
    assert registry.async_get(boiler).unique_id == "0011223344556272_indoor_temperature"


async def test_unavailable_and_recovery_propagate_to_real_states(
    hass: HomeAssistant, loaded_entry
) -> None:
    """A coordinator update makes an offline device unavailable and recoverable."""
    coordinator = loaded_entry.runtime_data
    entity_id = entity_id_for_unique_id(hass, "switch", "AABBCCDDEEFF_power")
    mat = coordinator.data["AABBCCDDEEFF"]
    assert hass.states.get(entity_id).state == "on"

    mat.apply_reported({"connected": False})
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"

    mat.apply_reported({"connected": True})
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"


async def test_entity_commands_build_verified_payloads(
    hass: HomeAssistant, loaded_entry
) -> None:
    """HA service calls reach mocked transports with the confirmed command bodies."""
    coordinator = loaded_entry.runtime_data
    coordinator.api.async_control = AsyncMock()
    coordinator.api.async_airone_request = AsyncMock()
    coordinator.api.async_boiler_request = AsyncMock()
    coordinator._schedule_airone_readback = Mock()
    coordinator._schedule_boiler_readback = Mock()
    coordinator._schedule_boiler_silence_check = Mock()
    coordinator._boiler_client_id = lambda: "contract-client"

    mat_switch = entity_id_for_unique_id(hass, "switch", "AABBCCDDEEFF_power")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": mat_switch}, blocking=True
    )
    assert coordinator.api.async_control.await_args.args[2] == {"operationMode": 0}

    air_switch = entity_id_for_unique_id(hass, "switch", "AIRONE001_power")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": air_switch}, blocking=True
    )
    air_call = coordinator.api.async_airone_request.await_args.kwargs
    assert air_call["command"] == "power"
    assert air_call["desired"] == {
        "roomController": {"deviceId": "RC001", "running": 2, "zoneId": 1}
    }
    assert air_call["legacy"] is False

    boiler_switch = entity_id_for_unique_id(
        hass, "switch", "0011223344556272_boiler_power"
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": boiler_switch}, blocking=True
    )
    boiler_call = coordinator.api.async_boiler_request.await_args.kwargs
    assert boiler_call["device_seq"] == 3
    assert boiler_call["service_code"] == 100
    payload = boiler_call["payload"]
    assert payload["clientID"] == "mobile-contract-client"
    assert payload["request"]["mode"] == "power-off"
    assert payload["request"]["command"] == 33554433

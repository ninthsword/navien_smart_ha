"""Verify setup lifecycle and entity contracts against Home Assistant 2026.8."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
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
    mat_entry = registry.async_get(mat)
    assert mat_entry is not None
    assert mat_entry.unique_id == "AABBCCDDEEFF_left_thermostat"
    air_entry = registry.async_get(air)
    assert air_entry is not None
    assert air_entry.unique_id == "AIRONE001_air_co2"
    boiler_entry = registry.async_get(boiler)
    assert boiler_entry is not None
    assert boiler_entry.unique_id == "0011223344556272_indoor_temperature"


async def test_unavailable_and_recovery_propagate_to_real_states(
    hass: HomeAssistant, loaded_entry
) -> None:
    """A coordinator update makes an offline device unavailable and recoverable."""
    coordinator = loaded_entry.runtime_data
    entity_id = entity_id_for_unique_id(hass, "switch", "AABBCCDDEEFF_power")
    mat = coordinator.data["AABBCCDDEEFF"]
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"

    mat.apply_reported({"connected": False})
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unavailable"

    mat.apply_reported({"connected": True})
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "on"


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
    assert coordinator.api.async_control.await_args is not None
    assert coordinator.api.async_control.await_args.args[2] == {"operationMode": 0}

    air_switch = entity_id_for_unique_id(hass, "switch", "AIRONE001_power")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": air_switch}, blocking=True
    )
    assert coordinator.api.async_airone_request.await_args is not None
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
    assert coordinator.api.async_boiler_request.await_args is not None
    boiler_call = coordinator.api.async_boiler_request.await_args.kwargs
    assert boiler_call["device_seq"] == 3
    assert boiler_call["service_code"] == 100
    payload = boiler_call["payload"]
    assert payload["clientID"] == "mobile-contract-client"
    assert payload["request"]["mode"] == "power-off"
    assert payload["request"]["command"] == 33554433


async def test_parent_registered_before_forwarding(hass, config_entry, devices):
    """Catch missing explicit registration independently of platform load order."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.navien_smarthome import async_setup_entry
    from custom_components.navien_smarthome.const import DOMAIN
    from custom_components.navien_smarthome.coordinator import NavienSmartCoordinator

    airone = devices[1]

    async def refresh(coordinator):
        coordinator.airone = {airone.device_id: airone}
        coordinator.async_set_updated_data({})

    async def forward(entry, platforms):
        parent = next((item for item in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        ) if (DOMAIN, airone.device_id) in item.identifiers), None)
        assert parent is not None, "AIRONE_PARENT_NOT_REGISTERED_BEFORE_FORWARD"
        assert entry.runtime_data.airone_device_registry_ids[airone.device_id] == parent.id
        assert parent.name == airone.nickname
        assert parent.model == airone.model_name
        assert parent.model_id == airone.model_code
        assert parent.serial_number == airone.device_id

    with (
        patch("custom_components.navien_smarthome.NavienSmartApi.async_login", new=AsyncMock()),
        patch.object(NavienSmartCoordinator, "async_config_entry_first_refresh", refresh),
        patch.object(NavienSmartCoordinator, "async_restore_state", new=AsyncMock()),
        patch.object(NavienSmartCoordinator, "async_start_mqtt", new=AsyncMock()),
        patch.object(hass.config_entries, "async_forward_entry_setups", forward),
    ):
        assert await async_setup_entry(hass, config_entry)


@pytest.fixture(params=["MONITOR001", None, ""])
def monitor_case(hass, config_entry, devices, request):
    from homeassistant.helpers import device_registry as dr

    from custom_components.navien_smarthome.const import DOMAIN

    airone = devices[1]
    monitor = {"deviceId": request.param, "modelCode": 35, "version": "1.2"}
    airone.air_monitors = (monitor,)
    identifier = request.param or "AIRONE001_airmonitor"
    registry = dr.async_get(hass)
    parent = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, airone.device_id)}
    )
    child = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, identifier)},
        via_device_id=parent.id,
    )
    return parent, child, identifier, monitor


@pytest.fixture
async def monitored_entry(monitor_case, loaded_entry):
    return loaded_entry, monitor_case


async def test_monitor_registry_identity_survives_reload(hass, monitored_entry, devices):
    """An existing monitor remains separate and attached to the same parent."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.navien_smarthome.entity import AironeMonitorEntity

    entry, (parent, child, identifier, monitor) = monitored_entry
    entity_id = entity_id_for_unique_id(hass, "sensor", f"{identifier}_air_co2")
    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None and entity.device_id == child.id
    assert child.id != parent.id
    registered = dr.async_get(hass).async_get(child.id)
    assert isinstance(registered, dr.DeviceEntry)
    assert registered.via_device_id == parent.id
    info = AironeMonitorEntity(entry.runtime_data, devices[1], monitor).device_info
    assert info is not None and info.get("via_device_id") == parent.id
    assert "via_device" not in info
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = er.async_get(hass).async_get(entity_id)
    assert reloaded is not None and reloaded.id == entity.id
    assert reloaded.device_id == child.id
    assert entry.runtime_data.airone_device_registry_ids["AIRONE001"] == parent.id
    registered = dr.async_get(hass).async_get(child.id)
    assert isinstance(registered, dr.DeviceEntry)
    assert registered.via_device_id == parent.id


async def test_absent_monitor_keeps_sensor_on_parent(hass, loaded_entry):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.navien_smarthome.const import DOMAIN

    registry = dr.async_get(hass)
    parent = next(item for item in dr.async_entries_for_config_entry(
        registry, loaded_entry.entry_id
    ) if (DOMAIN, "AIRONE001") in item.identifiers)
    assert parent is not None
    sensor = er.async_get(hass).async_get(
        entity_id_for_unique_id(hass, "sensor", "AIRONE001_air_co2")
    )
    assert sensor is not None and sensor.device_id == parent.id
    assert not any((DOMAIN, "AIRONE001_airmonitor") in item.identifiers
                   for item in dr.async_entries_for_config_entry(registry, loaded_entry.entry_id))

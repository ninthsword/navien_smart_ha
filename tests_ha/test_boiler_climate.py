"""The NR-67D boiler's thermostat and water heater, through Home Assistant's own services."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.components.water_heater import WaterHeaterEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.navien_smarthome.boiler import BoilerDevice
from custom_components.navien_smarthome.const import DOMAIN
from custom_components.navien_smarthome.coordinator import NavienSmartCoordinator

from .conftest import entity_id_for_unique_id

# Kept before loaded_entry patches the class, so a test can cancel the readback timers.
REAL_STOP_MQTT = NavienSmartCoordinator.async_stop_mqtt

DEVICE_ID = "0011223344556272"
ONDOL_COMMAND = 33554438
HOT_WATER_COMMAND = 33554443
POWER_ON_COMMAND = 33554434
POWER_OFF_COMMAND = 33554433


@pytest.fixture
async def wired(hass: HomeAssistant, loaded_entry) -> AsyncIterator[NavienSmartCoordinator]:
    """The loaded coordinator with only the boiler request boundary mocked."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    coordinator.api.async_boiler_request = AsyncMock()
    coordinator._mqtt = Mock(
        connected=True, client_id="contract-client", async_stop=AsyncMock()
    )
    try:
        yield coordinator
    finally:
        await REAL_STOP_MQTT(coordinator)


def boiler(coordinator: NavienSmartCoordinator) -> BoilerDevice:
    return coordinator.boilers[DEVICE_ID]


def thermostat_id(hass: HomeAssistant) -> str:
    return entity_id_for_unique_id(hass, "climate", f"{DEVICE_ID}_boiler_heating")


def water_heater_id(hass: HomeAssistant) -> str:
    return entity_id_for_unique_id(hass, "water_heater", f"{DEVICE_ID}_boiler_water_heater")


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


def sent(coordinator: NavienSmartCoordinator) -> list[tuple[int, list[float]]]:
    """Every command at the mocked boundary, in order, as (command, param)."""
    mock: Any = coordinator.api.async_boiler_request
    return [
        (call.kwargs["payload"]["request"]["command"], call.kwargs["payload"]["request"]["param"])
        for call in mock.await_args_list
    ]


async def refresh(hass: HomeAssistant, coordinator: NavienSmartCoordinator) -> None:
    coordinator.async_update_listeners()
    await hass.async_block_till_done()


async def set_power_state(
    hass: HomeAssistant, coordinator: NavienSmartCoordinator, mode: int | None
) -> None:
    """Set the reported operationMode (1 is off), or remove it when None."""
    if mode is None:
        boiler(coordinator).status.pop("operationMode", None)
    else:
        boiler(coordinator).status["operationMode"] = mode
    await refresh(hass, coordinator)


async def drop_power_use(hass: HomeAssistant, coordinator: NavienSmartCoordinator) -> None:
    device = boiler(coordinator)
    device.feature = {**device.feature, "powerUse": 1}
    await refresh(hass, coordinator)


async def climate_call(
    hass: HomeAssistant, entity_id: str, service: str, **data: Any
) -> None:
    await hass.services.async_call(
        "climate", service, {"entity_id": entity_id, **data}, blocking=True
    )


async def water_heater_call(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(
        "water_heater", "set_temperature", {"entity_id": entity_id, **data}, blocking=True
    )


async def test_thermostat_state_and_attributes(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    state = state_of(hass, thermostat_id(hass))
    assert state.name.endswith("난방")
    assert state.state == HVACMode.HEAT
    assert state.attributes["current_temperature"] == 55.0
    assert state.attributes["temperature"] == 50.0
    assert state.attributes["min_temp"] == 30.0
    assert state.attributes["max_temp"] == 65.0
    assert state.attributes["target_temp_step"] == 0.5
    assert set(state.attributes["hvac_modes"]) == {HVACMode.HEAT, HVACMode.OFF}
    assert state.attributes["hvac_action"] == "heating"
    assert "preset_modes" not in state.attributes
    assert state.attributes["operation_busy"] == 2
    assert state.attributes["heating_idle"] is False
    assert "status_age_seconds" in state.attributes
    features = ClimateEntityFeature(state.attributes["supported_features"])
    assert features == (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )


@pytest.mark.parametrize(
    ("mode", "busy", "hvac", "action", "water"),
    [
        (2, 2, HVACMode.HEAT, "heating", "gas"),
        (2, 1, HVACMode.HEAT, "idle", "gas"),
        (2, None, HVACMode.HEAT, None, "gas"),
        (1, 1, HVACMode.OFF, "off", "off"),
    ],
)
async def test_states_follow_the_boiler(
    hass: HomeAssistant,
    wired: NavienSmartCoordinator,
    mode: int,
    busy: int | None,
    hvac: HVACMode,
    action: str | None,
    water: str,
) -> None:
    device = boiler(wired)
    if busy is None:
        device.status.pop("operationBusy", None)
    else:
        device.status["operationBusy"] = busy
    await set_power_state(hass, wired, mode)
    thermostat = state_of(hass, thermostat_id(hass))
    assert thermostat.state == hvac
    assert thermostat.attributes.get("hvac_action") == action
    assert state_of(hass, water_heater_id(hass)).state == water


async def test_unknown_operation_mode_is_unknown(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    await set_power_state(hass, wired, None)
    assert state_of(hass, thermostat_id(hass)).state == STATE_UNKNOWN
    assert state_of(hass, water_heater_id(hass)).state == STATE_UNKNOWN

    await climate_call(hass, thermostat_id(hass), "set_temperature", temperature=40.0)
    assert sent(wired) == [(ONDOL_COMMAND, [80.0])]


async def test_power_off_thermostat_stores_value_without_power(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    await set_power_state(hass, wired, 1)
    await climate_call(hass, thermostat_id(hass), "set_temperature", temperature=40.5)
    assert sent(wired) == [(ONDOL_COMMAND, [81.0])]


async def test_temperature_then_power_in_order(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    await climate_call(
        hass, thermostat_id(hass), "set_temperature", temperature=40.0, hvac_mode="off"
    )
    assert sent(wired) == [(ONDOL_COMMAND, [80.0]), (POWER_OFF_COMMAND, [])]


async def test_heat_on_a_powered_off_boiler_sends_power_on(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    await set_power_state(hass, wired, 1)
    await climate_call(
        hass, thermostat_id(hass), "set_temperature", temperature=40.0, hvac_mode="heat"
    )
    assert sent(wired) == [(ONDOL_COMMAND, [80.0]), (POWER_ON_COMMAND, [])]


async def test_unknown_power_counts_as_different(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    await set_power_state(hass, wired, None)
    await climate_call(hass, thermostat_id(hass), "set_hvac_mode", hvac_mode="heat")
    assert sent(wired) == [(POWER_ON_COMMAND, [])]


async def test_matching_mode_sends_no_power(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    await climate_call(
        hass, thermostat_id(hass), "set_temperature", temperature=40.0, hvac_mode="heat"
    )
    assert sent(wired) == [(ONDOL_COMMAND, [80.0])]
    await climate_call(hass, thermostat_id(hass), "set_hvac_mode", hvac_mode="heat")
    assert sent(wired) == [(ONDOL_COMMAND, [80.0])]


async def test_unoffered_mode_refused_before_anything_is_sent(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    with pytest.raises(ServiceValidationError):
        await climate_call(
            hass, thermostat_id(hass), "set_temperature", temperature=40.0, hvac_mode="cool"
        )
    assert sent(wired) == []


async def test_power_failure_after_temperature_reaches_the_caller(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    """The setpoint was sent; the power error surfaces and a retry is safe."""
    mock: Any = wired.api.async_boiler_request

    async def fail_power(*_args: Any, **kwargs: Any) -> None:
        if kwargs["payload"]["request"]["command"] == POWER_OFF_COMMAND:
            raise HomeAssistantError("synthetic power failure")

    mock.side_effect = fail_power
    with pytest.raises(HomeAssistantError, match="synthetic power failure"):
        await climate_call(
            hass, thermostat_id(hass), "set_temperature", temperature=40.0, hvac_mode="off"
        )
    assert sent(wired) == [(ONDOL_COMMAND, [80.0]), (POWER_OFF_COMMAND, [])]


async def test_value_the_builder_refuses_sends_nothing(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    with pytest.raises(HomeAssistantError):
        await climate_call(
            hass, thermostat_id(hass), "set_temperature", temperature=40.25, hvac_mode="off"
        )
    assert sent(wired) == []


async def test_call_without_temperature_sends_nothing(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    component: EntityComponent[ClimateEntity] = hass.data["climate"]
    entity = component.get_entity(thermostat_id(hass))
    assert entity is not None
    await entity.async_set_temperature(hvac_mode=HVACMode.OFF)
    assert sent(wired) == []


async def test_boiler_without_power_use(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    await drop_power_use(hass, wired)
    entity_id = thermostat_id(hass)
    state = state_of(hass, entity_id)
    assert state.attributes["hvac_modes"] == [HVACMode.HEAT]
    assert ClimateEntityFeature(state.attributes["supported_features"]) == (
        ClimateEntityFeature.TARGET_TEMPERATURE
    )

    await climate_call(hass, entity_id, "set_hvac_mode", hvac_mode="heat")
    assert sent(wired) == []
    await climate_call(hass, entity_id, "set_temperature", temperature=40.0, hvac_mode="heat")
    assert sent(wired) == [(ONDOL_COMMAND, [80.0])]

    with pytest.raises(ServiceValidationError):
        await climate_call(hass, entity_id, "set_temperature", temperature=41.0, hvac_mode="off")
    assert sent(wired) == [(ONDOL_COMMAND, [80.0])]

    await set_power_state(hass, wired, 1)
    assert state_of(hass, entity_id).state == HVACMode.OFF


async def test_water_heater_state_and_command(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    entity_id = water_heater_id(hass)
    state = state_of(hass, entity_id)
    assert state.name.endswith("온수")
    assert state.state == "gas"
    assert state.attributes["current_temperature"] == 42.0
    assert state.attributes["temperature"] == 45.0
    assert state.attributes["min_temp"] == 30.0
    assert state.attributes["max_temp"] == 60.0
    assert state.attributes["target_temp_step"] == 0.5
    assert state.attributes["supported_features"] == 1
    assert state.attributes["operation_busy"] == 2
    assert state.attributes["heating_idle"] is False
    assert "status_age_seconds" in state.attributes
    assert "operation_list" not in state.attributes

    await set_power_state(hass, wired, 1)
    await water_heater_call(hass, entity_id, temperature=47.5)
    assert sent(wired) == [(HOT_WATER_COMMAND, [95.0])]


async def test_water_heater_ignores_operation_mode(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    await water_heater_call(
        hass, water_heater_id(hass), temperature=40.0, operation_mode="eco"
    )
    assert sent(wired) == [(HOT_WATER_COMMAND, [80.0])]


async def test_range_follows_the_feature_and_falls_back(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    device = boiler(wired)
    original = dict(device.feature)
    device.feature = {
        **original,
        "ondolTemperatureMin": 70,
        "ondolTemperatureMax": 120,
        "hotWaterTemperatureMin": 80,
        "hotWaterTemperatureMax": 100,
    }
    await refresh(hass, wired)
    thermostat = state_of(hass, thermostat_id(hass))
    assert (thermostat.attributes["min_temp"], thermostat.attributes["max_temp"]) == (35.0, 60.0)
    water = state_of(hass, water_heater_id(hass))
    assert (water.attributes["min_temp"], water.attributes["max_temp"]) == (40.0, 50.0)

    climate: EntityComponent[ClimateEntity] = hass.data["climate"]
    heater: EntityComponent[WaterHeaterEntity] = hass.data["water_heater"]
    thermostat_entity = climate.get_entity(thermostat_id(hass))
    heater_entity = heater.get_entity(water_heater_id(hass))
    assert thermostat_entity is not None
    assert heater_entity is not None

    # The range goes missing: the range seen at creation, and the entity is unavailable.
    device.feature = {
        key: value
        for key, value in original.items()
        if key not in ("ondolUse", "hotWaterTemperatureSettingUse")
    }
    await refresh(hass, wired)
    assert (thermostat_entity.min_temp, thermostat_entity.max_temp) == (30.0, 65.0)
    assert (heater_entity.min_temp, heater_entity.max_temp) == (30.0, 60.0)
    assert state_of(hass, thermostat_id(hass)).state == STATE_UNAVAILABLE
    assert state_of(hass, water_heater_id(hass)).state == STATE_UNAVAILABLE

    # The device goes missing: the same fallback.
    device.feature = original
    del wired.boilers[DEVICE_ID]
    await refresh(hass, wired)
    assert (thermostat_entity.min_temp, thermostat_entity.max_temp) == (30.0, 65.0)
    assert (heater_entity.min_temp, heater_entity.max_temp) == (30.0, 60.0)
    assert state_of(hass, thermostat_id(hass)).state == STATE_UNAVAILABLE
    wired.boilers[DEVICE_ID] = device


async def test_numbers_are_gone_and_sensors_and_switches_remain(
    hass: HomeAssistant, wired: NavienSmartCoordinator
) -> None:
    registry = er.async_get(hass)
    for removed in ("hot_water_temperature_setting", "ondol_temperature_setting"):
        assert registry.async_get_entity_id("number", DOMAIN, f"{DEVICE_ID}_{removed}") is None
    assert registry.async_get_entity_id("switch", DOMAIN, f"{DEVICE_ID}_boiler_power")
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{DEVICE_ID}_hot_water_target_temperature")
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{DEVICE_ID}_ondol_target_temperature")

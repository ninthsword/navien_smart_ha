"""The estimated heating pump and circulation flow sensors and their options form."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import CONF_ADDRESS, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import event as event_helpers
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.navien_smarthome.ble_mat import CONF_MAT_MODEL
from custom_components.navien_smarthome.ble_mat.connection import CONF_MAT_ID
from custom_components.navien_smarthome.boiler import BoilerDevice
from custom_components.navien_smarthome.const import CONF_HEATING_ENTITIES, DOMAIN
from custom_components.navien_smarthome.models import NavienDevice

from .conftest import entity_id_for_unique_id

BOILER_ID = "0011223344556272"
ROOM_A = "climate.synthetic_room_a"
ROOM_B = "climate.synthetic_room_b"
HEATING = ("heat", {"hvac_action": "heating"})
IDLE = ("heat", {"hvac_action": "idle"})
STOPPED = ("off", {"hvac_action": "off"})
NO_ACTION = ("heat", {})
UNAVAILABLE = ("unavailable", {})
UNKNOWN_STATE = ("unknown", {})

Devices = tuple[NavienDevice, Any, BoilerDevice]
StateSpec = tuple[str, dict[str, str]] | None


def pump_id(hass: HomeAssistant) -> str:
    return entity_id_for_unique_id(
        hass, "binary_sensor", f"{BOILER_ID}_heating_pump_running"
    )


def flow_id(hass: HomeAssistant) -> str:
    return entity_id_for_unique_id(
        hass, "sensor", f"{BOILER_ID}_heating_circulation_flow"
    )


def state_of(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def select(
    hass: HomeAssistant, entry: MockConfigEntry, *entities: str
) -> None:
    """Store a selection; the entry's update listener reloads it."""
    hass.config_entries.async_update_entry(
        entry, options={CONF_HEATING_ENTITIES: list(entities)}
    )
    await hass.async_block_till_done()


def set_climate(hass: HomeAssistant, entity_id: str, spec: StateSpec) -> None:
    if spec is None:
        hass.states.async_remove(entity_id)
    else:
        hass.states.async_set(entity_id, spec[0], spec[1])


def set_boiler(
    entry: MockConfigEntry, boiler: BoilerDevice, **status: Any
) -> None:
    """Change the boiler and tell the entities, as an MQTT update does."""
    boiler.status.update(status)
    entry.runtime_data.async_update_listeners()


async def arrange(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    boiler: BoilerDevice,
    specs: dict[str, StateSpec],
    **status: Any,
) -> None:
    boiler.status.update(status)
    for entity_id, spec in specs.items():
        set_climate(hass, entity_id, spec)
    await select(hass, entry, *specs)


# Options form


def form_field(result: Any) -> Any:
    """Return the schema key of the one field on the options form."""
    schema = result["data_schema"].schema
    assert len(schema) == 1
    return next(iter(schema))


async def test_options_form_offered_and_prefilled(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    assert loaded_entry.supports_options
    await select(hass, loaded_entry, ROOM_A)

    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)

    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "init"
    field = form_field(result)
    assert str(field) == CONF_HEATING_ENTITIES
    assert field.description == {"suggested_value": [ROOM_A]}


@pytest.mark.parametrize(
    ("submitted", "stored"),
    [
        ({CONF_HEATING_ENTITIES: [ROOM_A, ROOM_B]}, [ROOM_A, ROOM_B]),
        ({CONF_HEATING_ENTITIES: []}, []),
        ({}, []),
    ],
    ids=["saves", "empty-clears", "omitted-clears"],
)
async def test_options_save_stores_and_reloads(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    submitted: dict[str, Any],
    stored: list[str],
) -> None:
    await select(hass, loaded_entry, "climate.synthetic_previous")

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], submitted
        )
        await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert loaded_entry.options == {CONF_HEATING_ENTITIES: stored}
    reload.assert_called_once_with(loaded_entry.entry_id)


async def test_bluetooth_mat_entry_offers_model_options(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MAT_ID: "00112233"},
    )
    entry.add_to_hass(hass)

    import_module("custom_components.navien_smarthome.config_flow")
    assert entry.supports_options
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("type") is FlowResultType.MENU
    assert result.get("step_id") == "mat_menu"
    assert result.get("menu_options") == ["mat_model", "custom_program"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "mat_model"}
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "mat_model"
    assert str(form_field(result)) == CONF_MAT_MODEL


# The pump rule


@pytest.mark.parametrize(
    ("specs", "expected"),
    [
        ({ROOM_A: HEATING}, STATE_ON),
        ({ROOM_A: IDLE}, STATE_OFF),
        ({ROOM_A: STOPPED}, STATE_OFF),
        ({ROOM_A: UNAVAILABLE}, STATE_UNKNOWN),
        ({ROOM_A: UNKNOWN_STATE}, STATE_UNKNOWN),
        ({ROOM_A: None}, STATE_UNKNOWN),
        ({ROOM_A: NO_ACTION}, STATE_UNKNOWN),
        ({ROOM_A: HEATING, ROOM_B: IDLE}, STATE_ON),
        ({ROOM_A: IDLE, ROOM_B: STOPPED}, STATE_OFF),
        ({ROOM_A: HEATING, ROOM_B: UNAVAILABLE}, STATE_ON),
        ({ROOM_A: HEATING, ROOM_B: None}, STATE_ON),
        ({ROOM_A: IDLE, ROOM_B: UNAVAILABLE}, STATE_UNKNOWN),
        ({ROOM_A: IDLE, ROOM_B: None}, STATE_UNKNOWN),
        ({ROOM_A: IDLE, ROOM_B: NO_ACTION}, STATE_UNKNOWN),
    ],
)
async def test_pump_follows_selected_entities(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    devices: Devices,
    specs: dict[str, StateSpec],
    expected: str,
) -> None:
    await arrange(hass, loaded_entry, devices[2], specs)

    assert state_of(hass, pump_id(hass)) == expected


async def test_nothing_selected_is_unknown(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    assert state_of(hass, pump_id(hass)) == STATE_UNKNOWN
    assert state_of(hass, flow_id(hass)) == STATE_UNKNOWN


async def test_nothing_selected_while_hot_water_in_use_is_unknown(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    set_boiler(loaded_entry, devices[2], DHWUse=2, heatFlowRate=20)
    await hass.async_block_till_done()

    assert state_of(hass, pump_id(hass)) == STATE_UNKNOWN
    assert state_of(hass, flow_id(hass)) == STATE_UNKNOWN


@pytest.mark.parametrize(
    "spec", [HEATING, IDLE, UNAVAILABLE, None, NO_ACTION], ids=str
)
async def test_hot_water_in_use_is_off_whatever_the_entities_say(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    devices: Devices,
    spec: StateSpec,
) -> None:
    await arrange(hass, loaded_entry, devices[2], {ROOM_A: spec}, DHWUse=2)

    assert state_of(hass, pump_id(hass)) == STATE_OFF


async def test_unknown_hot_water_with_a_heating_request_is_unknown(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(hass, loaded_entry, devices[2], {ROOM_A: HEATING}, DHWUse=0)

    assert state_of(hass, pump_id(hass)) == STATE_UNKNOWN


async def test_unknown_hot_water_with_none_heating_is_off(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(
        hass, loaded_entry, devices[2], {ROOM_A: IDLE, ROOM_B: STOPPED}, DHWUse=0
    )

    assert state_of(hass, pump_id(hass)) == STATE_OFF


async def test_burning_does_not_enter_the_rule(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(
        hass, loaded_entry, devices[2], {ROOM_A: IDLE}, operationBusy=2, operationMode=2
    )

    assert state_of(hass, pump_id(hass)) == STATE_OFF


async def test_a_heating_request_runs_the_pump_while_the_burner_is_idle(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(
        hass,
        loaded_entry,
        devices[2],
        {ROOM_A: HEATING},
        operationBusy=1,
        DHWUse=1,
        heatFlowRate=21,
    )

    assert state_of(hass, pump_id(hass)) == STATE_ON
    assert float(state_of(hass, flow_id(hass))) == 21


async def test_the_pump_sensor_name_carries_no_estimate_mark(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(hass, loaded_entry, devices[2], {ROOM_A: IDLE})

    state = hass.states.get(pump_id(hass))
    assert state is not None
    assert state.attributes["friendly_name"].endswith(" 난방 펌프 가동")


async def test_the_circulation_flow_sensor_name_carries_no_estimate_mark(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(hass, loaded_entry, devices[2], {ROOM_A: IDLE})

    state = hass.states.get(flow_id(hass))
    assert state is not None
    assert state.attributes["friendly_name"].endswith(" 난방 순환 유량")


async def test_both_sensors_are_unavailable_with_the_boiler(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(hass, loaded_entry, devices[2], {ROOM_A: HEATING})

    with patch.object(BoilerDevice, "available", False):
        loaded_entry.runtime_data.async_update_listeners()
        await hass.async_block_till_done()

        assert state_of(hass, pump_id(hass)) == "unavailable"
        assert state_of(hass, flow_id(hass)) == "unavailable"


# The circulation flow


@pytest.mark.parametrize(
    ("spec", "status", "expected"),
    [
        (HEATING, {"heatFlowRate": 21}, 21.0),
        (HEATING, {"heatFlowRate": None}, None),
        (IDLE, {"heatFlowRate": 21}, 0.0),
        (IDLE, {"heatFlowRate": None}, 0.0),
        (HEATING, {"heatFlowRate": 21, "DHWUse": 2}, 0.0),
        (HEATING, {"heatFlowRate": 21, "DHWUse": 0}, None),
        (UNAVAILABLE, {"heatFlowRate": 21}, None),
    ],
    ids=[
        "on",
        "on-unknown-flow",
        "off",
        "off-unknown-flow",
        "hot-water-off",
        "rule-unknown-hot-water",
        "rule-unknown-entity",
    ],
)
async def test_circulation_flow(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    devices: Devices,
    spec: StateSpec,
    status: dict[str, Any],
    expected: float | None,
) -> None:
    await arrange(hass, loaded_entry, devices[2], {ROOM_A: spec}, **status)

    value = state_of(hass, flow_id(hass))
    if expected is None:
        assert value == STATE_UNKNOWN
    else:
        assert float(value) == expected


# Updates and side effects


async def test_entity_change_updates_without_a_boiler_update(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(
        hass, loaded_entry, devices[2], {ROOM_A: IDLE}, heatFlowRate=21
    )
    assert state_of(hass, pump_id(hass)) == STATE_OFF

    set_climate(hass, ROOM_A, HEATING)
    await hass.async_block_till_done()

    assert state_of(hass, pump_id(hass)) == STATE_ON
    assert float(state_of(hass, flow_id(hass))) == 21.0


async def test_entity_appearing_after_startup_is_picked_up(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(hass, loaded_entry, devices[2], {ROOM_A: None})
    assert state_of(hass, pump_id(hass)) == STATE_UNKNOWN

    set_climate(hass, ROOM_A, HEATING)
    await hass.async_block_till_done()

    assert state_of(hass, pump_id(hass)) == STATE_ON


async def test_boiler_update_reaches_both_sensors(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    await arrange(hass, loaded_entry, devices[2], {ROOM_A: HEATING}, heatFlowRate=21)
    assert state_of(hass, pump_id(hass)) == STATE_ON

    set_boiler(loaded_entry, devices[2], DHWUse=2)
    await hass.async_block_till_done()

    assert state_of(hass, pump_id(hass)) == STATE_OFF
    assert float(state_of(hass, flow_id(hass))) == 0.0


async def test_listener_is_gone_after_unload(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    removers: list[Mock] = []
    real: Callable[..., Any] = event_helpers.async_track_state_change_event

    def tracking(*args: Any, **kwargs: Any) -> Mock:
        remover = Mock(side_effect=real(*args, **kwargs))
        removers.append(remover)
        return remover

    with patch(
        "custom_components.navien_smarthome.entity.async_track_state_change_event",
        new=tracking,
    ):
        await select(hass, loaded_entry, ROOM_A)
    assert len(removers) == 2

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    for remover in removers:
        remover.assert_called_once_with()


async def test_unique_ids_are_deterministic(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    assert pump_id(hass)
    assert flow_id(hass)


async def test_no_service_is_called_on_a_selected_entity(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, devices: Devices
) -> None:
    calls: list[ServiceCall] = []

    async def record(call: ServiceCall) -> None:
        calls.append(call)

    for service in ("turn_on", "turn_off", "set_hvac_mode", "set_temperature"):
        hass.services.async_register("climate", service, AsyncMock(side_effect=record))

    await arrange(hass, loaded_entry, devices[2], {ROOM_A: HEATING})
    set_climate(hass, ROOM_A, IDLE)
    await hass.async_block_till_done()

    assert calls == []

"""Verify the Airone fan through Home Assistant's entity and service APIs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.fan import FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.navien_smarthome.airone import AironeDevice
from custom_components.navien_smarthome.coordinator import NavienSmartCoordinator
from custom_components.navien_smarthome.models import NavienDevice

from .conftest import entity_id_for_unique_id
from .devices import FAN_AIRONE_ID, make_fan_airone, make_mat

# Save the real methods before loaded_entry replaces these paths for offline setup.
REAL_STOP_MQTT = NavienSmartCoordinator.async_stop_mqtt
REAL_RESTORE_STATE = NavienSmartCoordinator.async_restore_state


@pytest.fixture
def devices(hass: HomeAssistant) -> tuple[NavienDevice, AironeDevice, None]:
    hass.config.language = "ko"
    return make_mat(), make_fan_airone(), None


@pytest.fixture
def fan_id(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> str:
    return entity_id_for_unique_id(hass, "fan", f"{FAN_AIRONE_ID}_fan_control")


@pytest.fixture
async def fan_transport(
    loaded_entry: MockConfigEntry,
) -> AsyncIterator[tuple[NavienSmartCoordinator, AsyncMock]]:
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    transport = AsyncMock()
    with patch.object(coordinator.api, "async_airone_request", transport):
        try:
            yield coordinator, transport
        finally:
            await REAL_STOP_MQTT(coordinator)


def _report(
    coordinator: NavienSmartCoordinator,
    *,
    running: int = 1,
    mode: int = 4,
    option: int = 1,
    air_volume: int = 2,
) -> None:
    coordinator._handle_airone_reported(
        FAN_AIRONE_ID,
        {
            "roomController": {
                "running": running,
                "mode": mode,
                "option": option,
                "airVolume": air_volume,
            }
        },
    )


async def _call_fan(
    hass: HomeAssistant, fan_id: str, service: str, **data: Any
) -> None:
    await hass.services.async_call(
        "fan", service, {"entity_id": fan_id, **data}, blocking=True
    )


async def _select_request(
    hass: HomeAssistant, transport: AsyncMock, label: str
) -> Mapping[str, Any]:
    select_id = entity_id_for_unique_id(hass, "select", f"{FAN_AIRONE_ID}_fan")
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select_id, "option": label},
        blocking=True,
    )
    (request,) = transport.await_args_list
    transport.reset_mock()
    assert request.kwargs["command"] == "change-mode"
    return request.kwargs


def _assert_power(request: Mapping[str, Any], running: int) -> None:
    assert request["command"] == "power"
    assert request["desired"] == {
        "roomController": {"deviceId": "RC_TEST_FAN", "running": running, "zoneId": 2}
    }


def _assert_mode(
    request: Mapping[str, Any], *, mode: int, option: int, air_volume: int
) -> None:
    assert request["command"] == "change-mode"
    assert request["desired"] == {
        "roomController": {"mode": mode, "option": option, "airVolume": air_volume}
    }


async def test_fan_registers_and_publishes_live_capabilities(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, _ = fan_transport
    fan = er.async_get(hass).async_get(fan_id)
    assert fan is not None
    assert fan.config_entry_id == loaded_entry.entry_id
    assert fan.original_name == "팬"
    power_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF

    state = hass.states.get(fan_id)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["supported_features"] == power_features

    _report(coordinator)
    await hass.async_block_till_done()
    state = hass.states.get(fan_id)
    assert state is not None
    assert state.state == "on"
    device = coordinator.airone[FAN_AIRONE_ID]
    assert state.attributes["friendly_name"] == f"{device.nickname} 팬"
    assert state.attributes["percentage"] == 66
    assert state.attributes["percentage_step"] == 100 / 3
    assert state.attributes["preset_mode"] is None
    assert state.attributes["preset_modes"] == ["자동", "터보", "절전"]
    assert state.attributes["supported_features"] == (
        power_features | FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE
    )

    _report(coordinator, mode=12, air_volume=4)
    await hass.async_block_till_done()
    state = hass.states.get(fan_id)
    assert state is not None
    assert state.state == "on"
    assert "percentage" not in state.attributes
    assert state.attributes["preset_mode"] == "자동"
    assert state.attributes["preset_modes"] == ["자동"]
    assert state.attributes["supported_features"] == (
        power_features | FanEntityFeature.PRESET_MODE
    )


@pytest.mark.parametrize(
    ("service", "data"), [("turn_off", {}), ("set_percentage", {"percentage": 0})]
)
async def test_power_off_sends_one_command(
    hass: HomeAssistant,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
    service: str,
    data: dict[str, int],
) -> None:
    coordinator, transport = fan_transport
    _report(coordinator)
    await _call_fan(hass, fan_id, service, **data)
    (power,) = transport.await_args_list
    _assert_power(power.kwargs, 2)


@pytest.mark.parametrize(
    ("data", "label"),
    [
        ({"percentage": 66}, "약풍"),
        ({"preset_mode": "터보"}, "터보"),
        ({"percentage": 66, "preset_mode": "절전"}, "절전"),
    ],
)
async def test_turn_on_powers_once_then_matches_the_select_without_a_power_report(
    hass: HomeAssistant,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
    data: dict[str, int | str],
    label: str,
) -> None:
    coordinator, transport = fan_transport
    _report(coordinator, running=2)
    expected = await _select_request(hass, transport, label)

    await _call_fan(hass, fan_id, "turn_on", **data)
    power, choice = transport.await_args_list
    _assert_power(power.kwargs, 1)
    assert choice.kwargs == expected
    state = hass.states.get(fan_id)
    assert state is not None and state.state == "on"


@pytest.mark.parametrize(
    ("mode", "running", "data"),
    [
        (4, 1, {}),
        (4, 2, {"percentage": 0}),
        (12, 2, {"percentage": 66}),
        (None, 2, {"percentage": 66}),
    ],
)
async def test_turn_on_sends_power_even_without_a_choice(
    hass: HomeAssistant,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
    mode: int | None,
    running: int,
    data: dict[str, int],
) -> None:
    coordinator, transport = fan_transport
    if mode is not None:
        air_volume = 4 if mode == 12 else 2
        _report(coordinator, mode=mode, running=running, air_volume=air_volume)
    await _call_fan(hass, fan_id, "turn_on", **data)
    (power,) = transport.await_args_list
    _assert_power(power.kwargs, 1)


@pytest.mark.parametrize("running", [1, 2])
@pytest.mark.parametrize(
    ("service", "data", "label"),
    [
        ("set_percentage", {"percentage": 66}, "약풍"),
        ("set_preset_mode", {"preset_mode": "터보"}, "터보"),
    ],
)
async def test_setting_a_choice_powers_on_only_if_needed(
    hass: HomeAssistant,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
    running: int,
    service: str,
    data: dict[str, int | str],
    label: str,
) -> None:
    coordinator, transport = fan_transport
    _report(coordinator, running=running)
    expected = await _select_request(hass, transport, label)

    await _call_fan(hass, fan_id, service, **data)
    if running == 2:
        power, choice = transport.await_args_list
        _assert_power(power.kwargs, 1)
    else:
        (choice,) = transport.await_args_list
    assert choice.kwargs == expected


@pytest.mark.parametrize(
    ("new_choice", "expected_mode"),
    [
        pytest.param((6, 1, 3), None, id="cooking-loses-low"),
        pytest.param((4, 4, 4), None, id="sleep-loses-low"),
        pytest.param(None, None, id="unavailable"),
        pytest.param((8, 1, 2), 8, id="same-label-at-a-new-percentage"),
    ],
)
async def test_turn_on_rechecks_the_reported_choice_after_power(
    hass: HomeAssistant,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
    new_choice: tuple[int, int, int] | None,
    expected_mode: int | None,
) -> None:
    coordinator, transport = fan_transport
    _report(coordinator, running=2)

    def update_during_power(_home_seq: int, *, command: str, **kwargs: Any) -> None:
        if command != "power":
            return
        if new_choice is None:
            device = coordinator.airone[FAN_AIRONE_ID]
            coordinator.airone[FAN_AIRONE_ID] = replace(
                device, connected_registry=False
            )
            coordinator.async_update_listeners()
        else:
            mode, option, air_volume = new_choice
            _report(
                coordinator, running=2, mode=mode, option=option, air_volume=air_volume
            )

    transport.side_effect = update_during_power
    await _call_fan(hass, fan_id, "turn_on", percentage=66)
    if expected_mode is None:
        (power,) = transport.await_args_list
    else:
        power, choice = transport.await_args_list
        _assert_mode(choice.kwargs, mode=expected_mode, option=1, air_volume=2)
    _assert_power(power.kwargs, 1)


async def test_restored_on_state_sets_choices_without_power_but_turn_on_still_sends_it(
    hass: HomeAssistant,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, transport = fan_transport
    stored = {
        FAN_AIRONE_ID: {
            "roomController": {"running": 1, "mode": 4, "option": 1, "airVolume": 2}
        }
    }
    with (
        patch.object(
            coordinator._store, "async_load", new=AsyncMock(return_value=stored)
        ),
        patch.object(coordinator._store, "async_delay_save"),
    ):
        await REAL_RESTORE_STATE(coordinator)
    coordinator.async_update_listeners()
    assert FAN_AIRONE_ID in coordinator.restored_devices
    state = hass.states.get(fan_id)
    assert state is not None and state.state == "on"

    await _call_fan(hass, fan_id, "set_percentage", percentage=33)
    await _call_fan(hass, fan_id, "set_preset_mode", preset_mode="터보")
    speed, preset = transport.await_args_list
    _assert_mode(speed.kwargs, mode=4, option=1, air_volume=1)
    _assert_mode(preset.kwargs, mode=4, option=2, air_volume=3)

    transport.reset_mock()
    await _call_fan(hass, fan_id, "turn_on", percentage=33)
    power, speed = transport.await_args_list
    _assert_power(power.kwargs, 1)
    _assert_mode(speed.kwargs, mode=4, option=1, air_volume=1)
    assert FAN_AIRONE_ID in coordinator.restored_devices


@pytest.mark.parametrize(
    ("mode", "speeds"),
    [(4, [(1, 33), (2, 66), (3, 100)]), (8, [(2, 50), (3, 100)]), (6, [(3, 100)])],
)
async def test_published_percentages_round_trip_to_the_same_speed(
    hass: HomeAssistant,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
    mode: int,
    speeds: list[tuple[int, int]],
) -> None:
    coordinator, transport = fan_transport
    for air_volume, expected in speeds:
        _report(coordinator, mode=mode, air_volume=air_volume)
        await hass.async_block_till_done()
        state = hass.states.get(fan_id)
        assert state is not None
        percentage = state.attributes["percentage"]
        assert percentage == expected

        await _call_fan(hass, fan_id, "set_percentage", percentage=percentage)
        (choice,) = transport.await_args_list
        _assert_mode(choice.kwargs, mode=mode, option=1, air_volume=air_volume)
        transport.reset_mock()


@pytest.fixture
def power_id(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> str:
    return entity_id_for_unique_id(hass, "switch", f"{FAN_AIRONE_ID}_power")


def _assert_power_states(
    hass: HomeAssistant, fan_id: str, power_id: str, expected: str
) -> None:
    for entity_id in (fan_id, power_id):
        state = hass.states.get(entity_id)
        assert state is not None and state.state == expected


async def test_fan_and_switch_show_pending_power_until_a_matching_report(
    hass: HomeAssistant,
    fan_id: str,
    power_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, _ = fan_transport
    _report(coordinator, running=2)
    await _call_fan(hass, fan_id, "turn_on")
    _assert_power_states(hass, fan_id, power_id, "on")

    _report(coordinator, running=2)
    _assert_power_states(hass, fan_id, power_id, "on")

    _report(coordinator, running=1)
    _report(coordinator, running=2)
    _assert_power_states(hass, fan_id, power_id, "off")


async def test_pending_power_expires_on_the_current_device_after_a_poll(
    hass: HomeAssistant,
    fan_id: str,
    power_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, transport = fan_transport
    _report(coordinator, running=2)
    original = coordinator.airone[FAN_AIRONE_ID]

    async def rebuild_during_request(*_args: Any, **_kwargs: Any) -> None:
        await coordinator._async_collect()
        _assert_power_states(hass, fan_id, power_id, "off")

    with (
        patch.object(
            coordinator.api,
            "async_get_devices",
            new=AsyncMock(return_value=coordinator.raw_devices),
        ),
        patch.object(coordinator, "_async_update_air_sensors", new=AsyncMock()),
    ):
        transport.side_effect = rebuild_during_request
        await _call_fan(hass, fan_id, "turn_on")
        deadline = dt_util.utcnow() + timedelta(seconds=10)
        transport.side_effect = None
        current = coordinator.airone[FAN_AIRONE_ID]
        assert current is not original
        _assert_power_states(hass, fan_id, power_id, "on")

        await coordinator._async_collect()
        assert coordinator.airone[FAN_AIRONE_ID] is not current

    coordinator.async_update_listeners()
    _assert_power_states(hass, fan_id, power_id, "on")

    async_fire_time_changed(hass, deadline - timedelta(seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    _assert_power_states(hass, fan_id, power_id, "on")
    assert [call.kwargs["command"] for call in transport.await_args_list] == [
        "power",
        "status",
    ]

    async_fire_time_changed(hass, deadline)
    await hass.async_block_till_done(wait_background_tasks=True)
    _assert_power_states(hass, fan_id, power_id, "off")


async def test_switch_off_replaces_pending_power_and_clears_the_fan_percentage(
    hass: HomeAssistant,
    fan_id: str,
    power_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, _ = fan_transport
    _report(coordinator)
    await _call_fan(hass, fan_id, "turn_on")
    device = coordinator.airone[FAN_AIRONE_ID]
    first = device.pending_power
    assert first is not None and first.unsub in coordinator._oneshot_unsubs

    coordinator._handle_airone_reported(FAN_AIRONE_ID, {"roomController": {"mode": 4}})
    assert device.pending_power is first
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": power_id}, blocking=True
    )
    _assert_power_states(hass, fan_id, power_id, "off")
    state = hass.states.get(fan_id)
    assert state is not None and state.attributes["percentage"] == 0

    second = device.pending_power
    assert second is not None and second.unsub in coordinator._oneshot_unsubs
    assert first.unsub not in coordinator._oneshot_unsubs
    _report(coordinator)
    _assert_power_states(hass, fan_id, power_id, "off")

    _report(coordinator, running=3)
    _report(coordinator)
    _assert_power_states(hass, fan_id, power_id, "on")


async def test_failed_power_command_keeps_the_reported_display(
    hass: HomeAssistant,
    fan_id: str,
    power_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, transport = fan_transport
    _report(coordinator, running=2)
    transport.side_effect = HomeAssistantError("synthetic power failure")

    with pytest.raises(HomeAssistantError, match="synthetic power failure"):
        await _call_fan(hass, fan_id, "turn_on")

    _assert_power_states(hass, fan_id, power_id, "off")
    assert coordinator.airone[FAN_AIRONE_ID].pending_power is None
    assert not coordinator._oneshot_unsubs


async def test_pending_on_does_not_skip_power_for_a_fan_speed_change(
    hass: HomeAssistant,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, transport = fan_transport
    _report(coordinator, running=2)
    await _call_fan(hass, fan_id, "turn_on")
    state = hass.states.get(fan_id)
    assert state is not None and state.state == "on"
    transport.reset_mock()

    await _call_fan(hass, fan_id, "set_percentage", percentage=66)
    power, choice = transport.await_args_list
    _assert_power(power.kwargs, 1)
    _assert_mode(choice.kwargs, mode=4, option=1, air_volume=2)


async def test_stopping_mqtt_cancels_pending_power_even_during_another_command(
    hass: HomeAssistant,
    fan_id: str,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, transport = fan_transport
    _report(coordinator, running=2)
    await _call_fan(hass, fan_id, "turn_on")
    device = coordinator.airone[FAN_AIRONE_ID]
    pending = device.pending_power
    assert pending is not None and pending.unsub in coordinator._oneshot_unsubs

    async def stop_during_request(*_args: Any, **_kwargs: Any) -> None:
        await REAL_STOP_MQTT(coordinator)

    transport.side_effect = stop_during_request
    await _call_fan(hass, fan_id, "turn_off")

    assert device.pending_power is None
    assert not coordinator._oneshot_unsubs
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert transport.await_count == 2


async def _mode_request(
    hass: HomeAssistant, transport: AsyncMock, label: str
) -> Mapping[str, Any]:
    select_id = entity_id_for_unique_id(hass, "select", f"{FAN_AIRONE_ID}_mode")
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select_id, "option": label},
        blocking=True,
    )
    (request,) = transport.await_args_list
    transport.reset_mock()
    assert request.kwargs["command"] == "change-mode"
    return request.kwargs["desired"]["roomController"]


async def test_returning_to_a_mode_restores_its_last_fan_speed(
    hass: HomeAssistant,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, transport = fan_transport
    # Ventilation at gentle (1); its declared speed is 2 and cooking is fixed at high (3).
    _report(coordinator, mode=4, air_volume=1)
    assert (await _mode_request(hass, transport, "요리"))["airVolume"] == 3
    _report(coordinator, mode=6, air_volume=3)
    # The memory survives the poll that rebuilds the device.
    with (
        patch.object(
            coordinator.api,
            "async_get_devices",
            new=AsyncMock(return_value=coordinator.raw_devices),
        ),
        patch.object(coordinator, "_async_update_air_sensors", new=AsyncMock()),
    ):
        original = coordinator.airone[FAN_AIRONE_ID]
        await coordinator._async_collect()
        assert coordinator.airone[FAN_AIRONE_ID] is not original
    assert (await _mode_request(hass, transport, "환기")) == {
        "mode": 4,
        "option": 1,
        "airVolume": 1,
    }


async def test_leaving_sleep_restores_the_parent_mode_speed(
    hass: HomeAssistant,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, transport = fan_transport
    # Sleep is (4, 4) under ventilation (4, 1) and reports auto (4), which 4:1 also allows.
    _report(coordinator, mode=4, option=1, air_volume=1)
    assert (await _mode_request(hass, transport, "숙면"))["option"] == 4
    _report(coordinator, mode=4, option=4, air_volume=4)
    assert (await _mode_request(hass, transport, "환기"))["airVolume"] == 1


async def test_entering_a_mode_without_memory_uses_its_declared_speed(
    hass: HomeAssistant,
    fan_transport: tuple[NavienSmartCoordinator, AsyncMock],
) -> None:
    coordinator, transport = fan_transport
    _report(coordinator, mode=6, air_volume=3)
    # As the app does: the speed ventilation declares, not cooking's high.
    assert (await _mode_request(hass, transport, "환기"))["airVolume"] == 2

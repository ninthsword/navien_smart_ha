"""Verify single-zone mat off and stepped level lists through real Home Assistant services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.navien_smarthome.airone import AironeDevice
from custom_components.navien_smarthome.boiler import BoilerDevice
from custom_components.navien_smarthome.models import NavienDevice

from .conftest import entity_id_for_unique_id, make_airone, make_boiler

DEVICE_ID = "AABBCCDDEEFF"
STANDBY = "운전 대기"
STEPS = [f"{level}단계" for level in range(1, 9)]


def _make_mat(heat_control: dict[str, Any], heater: dict[str, Any]) -> NavienDevice:
    """Build a mat without contacting Navien; a nickname side would make it a double mat."""
    double = "left" in heater
    nick: dict[str, Any] = {"mainItem": "계약 테스트 매트"}
    if double:
        nick["side"] = {"left": "좌측", "right": "우측"}
    device = NavienDevice.parse(
        {
            "deviceId": DEVICE_ID,
            "deviceSeq": 1,
            "serviceCode": 200,
            "modelCode": "258",
            "modelName": "EME-520",
            "connected": True,
            "Properties": {
                "nickName": nick,
                "registry": {
                    "attributes": {
                        "model": "EME-520",
                        "modelType": "em",
                        "mcu": {"capacity": 2 if double else 1, "modelCode": 258},
                        "functions": {"powerCtrl": True, "heatControl": heat_control},
                    }
                },
            },
        }
    )
    assert device is not None
    device.apply_reported(
        {"connected": True, "operationMode": 1, "heater": heater, "errorCode": 0}
    )
    return device


def _temperature_zone(value: float) -> dict[str, Any]:
    return {"enable": True, "temperature": {"set": value, "current": value}}


def _level_zone(value: int) -> dict[str, Any]:
    return {"enable": value > 0, "level": {"set": value}}


def _single_temperature_mat() -> NavienDevice:
    return _make_mat(
        {"unit": "0.5C", "rangeMin": 28, "rangeMax": 50},
        {"single": _temperature_zone(35.0)},
    )


def _single_stepped_mat() -> NavienDevice:
    return _make_mat(
        {"unit": "1.0L", "rangeMin": 1, "rangeMax": 8}, {"single": _level_zone(3)}
    )


def _single_stepped_mat_at_zero() -> NavienDevice:
    return _make_mat(
        {"unit": "1.0L", "rangeMin": 1, "rangeMax": 8}, {"single": _level_zone(0)}
    )


def _double_stepped_mat() -> NavienDevice:
    return _make_mat(
        {"unit": "1.0L", "rangeMin": 1, "rangeMax": 8},
        {"left": _level_zone(3), "right": _level_zone(2)},
    )


@pytest.fixture
def devices(
    request: pytest.FixtureRequest,
) -> tuple[NavienDevice, AironeDevice, BoilerDevice]:
    """Replace conftest's double mat with the mat the test names."""
    build: Callable[[], NavienDevice] = request.param
    return build(), make_airone(), make_boiler()


def _stub_control(loaded_entry: Any) -> AsyncMock:
    control = AsyncMock()
    loaded_entry.runtime_data.api.async_control = control
    return control


@pytest.mark.parametrize("devices", [_single_temperature_mat], indirect=True)
async def test_single_temperature_mat_off_sends_the_power_command(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """The thermostat's off reaches the transport as a power-off and raises nothing."""
    control = _stub_control(loaded_entry)
    climate = entity_id_for_unique_id(hass, "climate", f"{DEVICE_ID}_single_thermostat")

    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": climate, "hvac_mode": "off"},
        blocking=True,
    )

    assert control.await_args is not None
    assert control.await_args.args[2] == {"operationMode": 0}


@pytest.mark.parametrize("devices", [_single_stepped_mat], indirect=True)
async def test_single_stepped_mat_lists_only_range_steps(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """The list has no standby, and asking for it is an invalid option that sends nothing."""
    control = _stub_control(loaded_entry)
    select = entity_id_for_unique_id(hass, "select", f"{DEVICE_ID}_single_level")
    state = hass.states.get(select)
    assert state is not None
    assert state.attributes["options"] == STEPS
    assert state.state == "3단계"

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select, "option": STANDBY},
            blocking=True,
        )
    control.assert_not_awaited()


@pytest.mark.parametrize("devices", [_single_stepped_mat], indirect=True)
async def test_single_stepped_mat_step_sends_the_heater_desired(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """Selecting a step maps to that level and does not touch the power."""
    control = _stub_control(loaded_entry)
    select = entity_id_for_unique_id(hass, "select", f"{DEVICE_ID}_single_level")

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select, "option": "1단계"},
        blocking=True,
    )

    assert control.await_args is not None
    assert control.await_args.args[2] == {
        "heater": {"single": {"enable": True, "level": {"set": 1}}}
    }


@pytest.mark.parametrize("devices", [_single_stepped_mat_at_zero], indirect=True)
async def test_single_stepped_mat_reporting_zero_shows_no_option(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """A value outside the list leaves the state empty."""
    select = entity_id_for_unique_id(hass, "select", f"{DEVICE_ID}_single_level")
    state = hass.states.get(select)
    assert state is not None
    assert state.state == "unknown"


@pytest.mark.parametrize("devices", [_double_stepped_mat], indirect=True)
async def test_double_stepped_mat_keeps_standby(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """Both sides still list standby, and selecting it on one side sends the heater desired."""
    control = _stub_control(loaded_entry)
    left = entity_id_for_unique_id(hass, "select", f"{DEVICE_ID}_left_level")
    right = entity_id_for_unique_id(hass, "select", f"{DEVICE_ID}_right_level")
    for entity_id in (left, right):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["options"] == [STANDBY, *STEPS]

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": left, "option": STANDBY},
        blocking=True,
    )

    assert control.await_args is not None
    assert control.await_args.args[2] == {
        "heater": {
            "left": {"enable": False, "level": {"set": 0}},
            "right": {"enable": True, "level": {"set": 2}},
        }
    }

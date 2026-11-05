"""1.0C thermostats, set_temperature on a powered-off mat and the control beep.

Through real Home Assistant services, with only the Navien transports stubbed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.navien_smarthome.airone import AironeDevice
from custom_components.navien_smarthome.boiler import BoilerDevice
from custom_components.navien_smarthome.const import DOMAIN
from custom_components.navien_smarthome.models import NavienDevice

from .conftest import entity_id_for_unique_id, make_airone, make_boiler

DEVICE_ID = "AABBCCDDEEFF"
TEMP_05 = {"unit": "0.5C", "rangeMin": 28, "rangeMax": 50}
TEMP_10 = {"unit": "1.0C", "rangeMin": 28, "rangeMax": 45}


def _make_mat(
    heat_control: dict[str, Any],
    *,
    double: bool,
    operation_mode: int | None = 1,
    model_code: int = 258,
) -> NavienDevice:
    """Build a synthetic mat; `operation_mode=None` leaves it out of the reported state."""
    nick: dict[str, Any] = {"mainItem": "계약 테스트 매트"}
    if double:
        nick["side"] = {"left": "좌측", "right": "우측"}
    device = NavienDevice.parse(
        {
            "deviceId": DEVICE_ID,
            "deviceSeq": 1,
            "serviceCode": 200,
            "modelCode": str(model_code),
            "modelName": "EQM-TEST",
            "connected": True,
            "Properties": {
                "nickName": nick,
                "registry": {
                    "attributes": {
                        "model": "EQM-TEST",
                        "modelType": "em",
                        "mcu": {"capacity": 2 if double else 1, "modelCode": model_code},
                        "functions": {"powerCtrl": True, "heatControl": heat_control},
                    }
                },
            },
        }
    )
    assert device is not None
    zone = {"enable": True, "temperature": {"set": 33.0, "current": 32.0}}
    heater = {"left": zone, "right": zone} if double else {"single": zone}
    reported: dict[str, Any] = {"connected": True, "heater": heater, "errorCode": 0}
    if operation_mode is not None:
        reported["operationMode"] = operation_mode
    device.apply_reported(reported)
    return device


def _double_one_degree() -> NavienDevice:
    return _make_mat(TEMP_10, double=True)


def _double_off() -> NavienDevice:
    return _make_mat(TEMP_05, double=True, operation_mode=0)


def _double_on() -> NavienDevice:
    return _make_mat(TEMP_05, double=True, operation_mode=1)


def _double_unknown() -> NavienDevice:
    return _make_mat(TEMP_05, double=True, operation_mode=None)


def _single_off() -> NavienDevice:
    return _make_mat(TEMP_05, double=False, operation_mode=0)


def _beep_model_off() -> NavienDevice:
    return _make_mat(TEMP_05, double=True, operation_mode=0, model_code=17)


def _plain_model_off() -> NavienDevice:
    return _make_mat(TEMP_05, double=True, operation_mode=0, model_code=258)


@pytest.fixture
def devices(
    request: pytest.FixtureRequest,
) -> tuple[NavienDevice, AironeDevice | None, BoilerDevice | None]:
    """Replace conftest's double mat with the mat the test names.

    A builder marked `alone` gets no Airone and no boiler.
    """
    build: Callable[[], NavienDevice] = request.param
    if getattr(build, "alone", False):
        return build(), None, None
    return build(), make_airone(), make_boiler()


def _stub_control(loaded_entry: Any) -> AsyncMock:
    control = AsyncMock()
    loaded_entry.runtime_data.api.async_control = control
    return control


async def _set_temperature(hass: HomeAssistant, zone: str, **extra: Any) -> None:
    climate = entity_id_for_unique_id(hass, "climate", f"{DEVICE_ID}_{zone}_thermostat")
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": climate, "temperature": 35, **extra},
        blocking=True,
    )


@pytest.mark.parametrize("devices", [_double_one_degree], indirect=True)
async def test_one_degree_mat_gets_a_thermostat_without_level_select(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """1.0C makes one 1-degree thermostat per zone and no level select."""
    control = _stub_control(loaded_entry)
    registry = er.async_get(hass)
    for zone in ("left", "right"):
        climate = entity_id_for_unique_id(hass, "climate", f"{DEVICE_ID}_{zone}_thermostat")
        state = hass.states.get(climate)
        assert state is not None
        assert state.attributes["target_temp_step"] == 1
        assert registry.async_get_entity_id("select", DOMAIN, f"{DEVICE_ID}_{zone}_level") is None

    left = entity_id_for_unique_id(hass, "climate", f"{DEVICE_ID}_left_thermostat")
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": left, "hvac_mode": "off"}, blocking=True
    )

    assert control.await_args is not None
    assert control.await_args.args[2]["heater"]["left"]["temperature"]["set"] == 27.0


@pytest.mark.parametrize("devices", [_double_off], indirect=True)
async def test_set_temperature_on_a_powered_off_mat_only_stores_it(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """Without hvac_mode, a mat reported off gets the heater desired alone."""
    control = _stub_control(loaded_entry)

    await _set_temperature(hass, "left")

    assert control.await_count == 1
    assert control.await_args is not None
    body = control.await_args.args[2]
    assert "operationMode" not in body
    assert body["heater"]["left"]["temperature"]["set"] == 35.0


@pytest.mark.parametrize("devices", [_double_on], indirect=True)
async def test_set_temperature_on_a_powered_on_mat_keeps_heat(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """A powered-on mat still gets operationMode heat with the value."""
    control = _stub_control(loaded_entry)

    await _set_temperature(hass, "left")

    assert control.await_args is not None
    assert control.await_args.args[2]["operationMode"] == 1


@pytest.mark.parametrize("devices", [_double_unknown], indirect=True)
async def test_set_temperature_with_unknown_power_powers_on(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """No reported operationMode is not taken as off."""
    control = _stub_control(loaded_entry)

    await _set_temperature(hass, "left")

    assert control.await_args is not None
    assert control.await_args.args[2]["operationMode"] == 1


@pytest.mark.parametrize("devices", [_double_off], indirect=True)
async def test_set_temperature_with_hvac_mode_heat_powers_on(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """hvac_mode heat turns a powered-off mat on with the value."""
    control = _stub_control(loaded_entry)

    await _set_temperature(hass, "left", hvac_mode="heat")

    assert control.await_count == 1
    assert control.await_args is not None
    body = control.await_args.args[2]
    assert body["operationMode"] == 1
    assert body["heater"]["left"]["temperature"]["set"] == 35.0


@pytest.mark.parametrize("devices", [_single_off], indirect=True)
async def test_set_temperature_with_hvac_mode_off_stores_then_powers_off(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """hvac_mode off on a single mat sends the value, then the power command."""
    control = _stub_control(loaded_entry)

    await _set_temperature(hass, "single", hvac_mode="off")

    bodies = [call.args[2] for call in control.await_args_list]
    assert len(bodies) == 2
    assert "operationMode" not in bodies[0]
    assert bodies[0]["heater"]["single"]["temperature"]["set"] == 35.0
    assert bodies[1] == {"operationMode": 0}


@pytest.mark.parametrize("devices", [_double_off], indirect=True)
async def test_set_temperature_with_unoffered_hvac_mode_is_refused(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """A mode the entity does not offer raises and sends nothing."""
    control = _stub_control(loaded_entry)

    with pytest.raises(ServiceValidationError):
        await _set_temperature(hass, "left", hvac_mode="auto")

    control.assert_not_awaited()


@pytest.mark.parametrize("devices", [_beep_model_off], indirect=True)
async def test_listed_model_power_on_carries_beep(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """modelCode 17 is one of the app's 2024-or-later mats."""
    control = _stub_control(loaded_entry)
    power = entity_id_for_unique_id(hass, "switch", f"{DEVICE_ID}_power")

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": power}, blocking=True
    )

    assert control.await_args is not None
    assert control.await_args.args[2] == {"operationMode": 1, "beep": True}


@pytest.mark.parametrize("devices", [_plain_model_off], indirect=True)
async def test_unlisted_model_power_on_has_no_beep(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """modelCode 258 is not on the app's list."""
    control = _stub_control(loaded_entry)
    power = entity_id_for_unique_id(hass, "switch", f"{DEVICE_ID}_power")

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": power}, blocking=True
    )

    assert control.await_args is not None
    assert control.await_args.args[2] == {"operationMode": 1}


def _beep_model_on_alone() -> NavienDevice:
    return _make_mat(TEMP_05, double=True, operation_mode=1, model_code=17)


_beep_model_on_alone.alone = True  # type: ignore[attr-defined]


@pytest.mark.parametrize("devices", [_beep_model_on_alone], indirect=True)
async def test_startup_state_request_carries_no_beep(
    hass: HomeAssistant, loaded_entry: Any
) -> None:
    """The empty state request goes straight to the transport, without beep."""
    api = loaded_entry.runtime_data.api
    api.async_request_shadow = AsyncMock()
    control = _stub_control(loaded_entry)

    await loaded_entry.runtime_data._async_request_initial_state()
    await hass.async_block_till_done()

    assert control.await_count == 1
    assert control.await_args is not None
    assert control.await_args.args[2] == {}

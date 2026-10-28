"""A boiler whose REST entry lost Properties.did gets its entities and gas query from MQTT."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.navien_smarthome.airone import AironeDevice
from custom_components.navien_smarthome.boiler import (
    BoilerDevice,
    extract_boiler_status,
)
from custom_components.navien_smarthome.const import DOMAIN
from custom_components.navien_smarthome.coordinator import NavienSmartCoordinator
from custom_components.navien_smarthome.models import NavienDevice

from .conftest import make_airone, make_mat

# Kept before loaded_entry patches the class, so a test can run the real stop path.
REAL_STOP_MQTT = NavienSmartCoordinator.async_stop_mqtt

DEVICE_ID = "0011223344556272"
PHYSICAL_ID = "001122334455"
START_COMMAND = 16777217
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
GATED = (
    ("switch", f"{DEVICE_ID}_boiler_power"),
    ("switch", f"{DEVICE_ID}_fast_dhw"),
    ("switch", f"{DEVICE_ID}_smart_fast_dhw"),
    ("switch", f"{DEVICE_ID}_dhw_boost"),
    ("number", f"{DEVICE_ID}_hot_water_temperature_setting"),
    ("number", f"{DEVICE_ID}_ondol_temperature_setting"),
    ("sensor", f"{DEVICE_ID}_monthly_gas_usage"),
    ("sensor", f"{DEVICE_ID}_daily_gas_usage"),
)


def did_null_raw() -> dict[str, Any]:
    return {
        "deviceId": DEVICE_ID,
        "deviceSeq": 3,
        "serviceCode": 100,
        "modelCode": "20",
        "modelName": "NR-67D",
        "mqttTopicKey": "contract-topic",
        "connected": 1,
        "Properties": {"nickName": "계약 테스트 보일러", "did": None},
    }


def make_did_null_boiler() -> BoilerDevice:
    device = BoilerDevice.parse(did_null_raw())
    assert device is not None
    device.apply_status({"operationMode": 2, "operationBusy": 2}, now=100.0)
    return device


@pytest.fixture
def devices() -> tuple[NavienDevice, AironeDevice, BoilerDevice]:
    """The shared devices, with a boiler whose REST entry carries Properties.did null."""
    return make_mat(), make_airone(), make_did_null_boiler()


def deliver(coordinator: NavienSmartCoordinator, response: dict[str, Any]) -> None:
    """Hand raw MQTT bytes to the callback the coordinator registers with the client."""
    payload = json.dumps({"topic": "contract", "payload": {"response": response}})
    result = extract_boiler_status(payload.encode())
    assert result is not None
    coordinator._handle_boiler_reported(*result)


def gated_present(hass: HomeAssistant) -> list[bool]:
    registry = er.async_get(hass)
    return [
        registry.async_get_entity_id(platform, DOMAIN, unique_id) is not None
        for platform, unique_id in GATED
    ]


async def test_late_feature_adds_gated_entities_once(
    hass: HomeAssistant, loaded_entry
) -> None:
    """All eight feature-gated entities appear when the feature arrives, and only once."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    assert coordinator.boilers[DEVICE_ID].feature == {}
    assert gated_present(hass) == [False] * len(GATED)
    before = len(er.async_entries_for_config_entry(er.async_get(hass), loaded_entry.entry_id))

    deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": FEATURE})
    await hass.async_block_till_done()
    assert gated_present(hass) == [True] * len(GATED)
    for platform, unique_id in GATED:
        entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
        assert entity_id is not None
        assert hass.states.get(entity_id) is not None
    after = len(er.async_entries_for_config_entry(er.async_get(hass), loaded_entry.entry_id))
    assert after == before + len(GATED)

    deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": FEATURE})
    await hass.async_block_till_done()
    again = len(er.async_entries_for_config_entry(er.async_get(hass), loaded_entry.entry_id))
    assert again == after


def wire_transports(coordinator: NavienSmartCoordinator) -> None:
    coordinator.api.async_request_shadow = AsyncMock()
    coordinator.api.async_control = AsyncMock()
    coordinator.api.async_airone_request = AsyncMock()
    coordinator.api.async_boiler_request = AsyncMock()
    coordinator._mqtt = Mock(
        connected=True, client_id="contract-client", async_stop=AsyncMock()
    )


def is_start(call_kwargs: dict[str, Any]) -> bool:
    return call_kwargs["payload"]["request"]["command"] == START_COMMAND


async def test_feature_during_start_send_starts_gas_once(
    hass: HomeAssistant, loaded_entry
) -> None:
    """The status/start answer handled during its own send leaves the step the one request."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)

    async def answer(*_args: Any, **kwargs: Any) -> None:
        if is_start(kwargs):
            deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": FEATURE})

    coordinator.api.async_boiler_request = AsyncMock(side_effect=answer)
    try:
        await coordinator._async_request_initial_state()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.boiler_gas_requests == 1

        deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": FEATURE})
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.boiler_gas_requests == 1
        assert len(coordinator._boiler_gas_unsubs) == 1
    finally:
        await REAL_STOP_MQTT(coordinator)


async def test_feature_after_gas_step_starts_gas_once_per_run(
    hass: HomeAssistant, loaded_entry
) -> None:
    """A feature after the step starts gas once; a repeat adds none; a new run adds one."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)
    try:
        await coordinator._async_request_initial_state()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.boiler_gas_requests == 0

        deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": FEATURE})
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.boiler_gas_requests == 1
        assert len(coordinator._boiler_gas_unsubs) == 1

        deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": FEATURE})
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.boiler_gas_requests == 1

        await coordinator._async_request_initial_state()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.boiler_gas_requests == 2
        assert len(coordinator._boiler_gas_unsubs) == 1
    finally:
        await REAL_STOP_MQTT(coordinator)


async def test_poll_rebuild_during_start_send_then_feature(
    hass: HomeAssistant, loaded_entry
) -> None:
    """A poll that rebuilds the boiler during the send, then one feature: one gas request."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)
    coordinator.api.async_get_devices = AsyncMock(return_value=[did_null_raw()])
    original = coordinator.boilers[DEVICE_ID]

    async def rebuild_then_answer(*_args: Any, **kwargs: Any) -> None:
        if is_start(kwargs):
            await coordinator._async_collect()
            deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": FEATURE})

    coordinator.api.async_boiler_request = AsyncMock(side_effect=rebuild_then_answer)
    try:
        await coordinator._async_request_initial_state()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.boilers[DEVICE_ID] is not original
        assert coordinator.boiler_gas_requests == 1
    finally:
        await REAL_STOP_MQTT(coordinator)

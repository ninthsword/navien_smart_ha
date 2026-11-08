"""A boiler whose REST entry lost Properties.did gets its entities and gas query from MQTT."""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.navien_smarthome.airone import AironeDevice
from custom_components.navien_smarthome.boiler import (
    BOILER_READBACK_DELAY_SECONDS,
    BOILER_SILENCE_REFRESH_SECONDS,
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
    ("water_heater", f"{DEVICE_ID}_boiler_water_heater"),
    ("climate", f"{DEVICE_ID}_boiler_heating"),
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


async def test_stop_during_late_gas_request_leaves_no_timer(
    hass: HomeAssistant, loaded_entry
) -> None:
    """A late gas request still in flight when MQTT stops schedules nothing afterwards."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)
    try:
        await coordinator._async_request_initial_state()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.boiler_gas_requests == 0

        sent = asyncio.Event()
        release = asyncio.Event()

        async def held(*_args: Any, **_kwargs: Any) -> None:
            sent.set()
            await release.wait()

        coordinator.api.async_boiler_request = AsyncMock(side_effect=held)
        deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": FEATURE})
        await asyncio.wait_for(sent.wait(), 5)

        await REAL_STOP_MQTT(coordinator)
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator._boiler_gas_unsubs == {}
        assert coordinator._boiler_silence_unsubs == {}
        assert coordinator._boiler_gas_tasks == set()
    finally:
        await REAL_STOP_MQTT(coordinator)


# Synthetic ids. The first 12 characters of the supported id hold letters, so a reply that
# differs in letter case only is a different string until the comparison normalises it.
UNSUPPORTED_ID = "0F1E2D3C4B5A9999"
SUPPORTED_ID = "A1B2C3D4E5F66272"
SUPPORTED_SEQ = 5
LEARNED_ID = "C0C1C2C3C4C5"
LOGGER_NAME = "custom_components.navien_smarthome"


def mixed_rest_entries() -> list[dict[str, Any]]:
    """Two did-null boilers, the one whose requests cannot be built listed first."""
    return [
        {
            **did_null_raw(),
            "deviceId": UNSUPPORTED_ID,
            "deviceSeq": 4,
            "modelCode": "002",
            "modelName": "synthetic-unsupported",
        },
        {**did_null_raw(), "deviceId": SUPPORTED_ID, "deviceSeq": SUPPORTED_SEQ},
    ]


async def collect_with_boilers(
    coordinator: NavienSmartCoordinator, boilers: list[dict[str, Any]]
) -> None:
    """Run the real poll with the fixture mat and Airone beside the given boiler entries."""
    others = [
        raw for raw in coordinator.raw_devices if raw.get("serviceCode") != 100
    ]
    coordinator.api.async_get_devices = AsyncMock(return_value=[*others, *boilers])
    coordinator._async_update_air_sensors = AsyncMock()
    await coordinator._async_collect()


def error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_unsupported_boiler_leaves_the_start_sequence_running(
    hass: HomeAssistant, loaded_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the model-20 boiler gets a request; the mat and the Airone keep theirs."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)
    api: Any = coordinator.api
    await collect_with_boilers(coordinator, mixed_rest_entries())
    assert list(coordinator.boilers) == [UNSUPPORTED_ID, SUPPORTED_ID]
    try:
        await coordinator._async_request_initial_state()
        await hass.async_block_till_done(wait_background_tasks=True)

        calls = api.async_boiler_request.await_args_list
        assert calls
        assert {call.kwargs["device_seq"] for call in calls} == {SUPPORTED_SEQ}
        api.async_control.assert_awaited()
        api.async_airone_request.assert_awaited()
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and "초기 상태 요청 실패" in record.getMessage()
        ]
        assert len(warnings) == 1
        assert "002" in warnings[0]
        assert error_records(caplog) == []
    finally:
        await REAL_STOP_MQTT(coordinator)


async def test_unsupported_boiler_silence_check_counts_a_failure(
    hass: HomeAssistant, loaded_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """The timer sends nothing, logs no error, and comes back five minutes later."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)
    api: Any = coordinator.api
    await collect_with_boilers(coordinator, mixed_rest_entries())
    unsupported = coordinator.boilers[UNSUPPORTED_ID]
    assert unsupported.last_communication_at is None
    before = coordinator.boiler_silence_failures
    try:
        coordinator._schedule_boiler_silence_check(unsupported)
        async_fire_time_changed(
            hass,
            dt_util.utcnow() + timedelta(seconds=BOILER_SILENCE_REFRESH_SECONDS + 1),
        )
        await hass.async_block_till_done(wait_background_tasks=True)

        api.async_boiler_request.assert_not_awaited()
        assert coordinator.boiler_silence_failures == before + 1
        assert UNSUPPORTED_ID in coordinator._boiler_silence_unsubs
        assert error_records(caplog) == []
    finally:
        await REAL_STOP_MQTT(coordinator)


async def test_unsupported_boiler_readback_sends_nothing(
    hass: HomeAssistant, loaded_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """The readback takes the existing debug path of a failed send and logs no error."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)
    api: Any = coordinator.api
    await collect_with_boilers(coordinator, mixed_rest_entries())
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            coordinator._schedule_boiler_readback(coordinator.boilers[UNSUPPORTED_ID])
            async_fire_time_changed(
                hass,
                dt_util.utcnow() + timedelta(seconds=BOILER_READBACK_DELAY_SECONDS + 1),
            )
            await hass.async_block_till_done(wait_background_tasks=True)

        api.async_boiler_request.assert_not_awaited()
        assert any(
            record.levelno == logging.DEBUG and "보일러 상태 재확인 실패" in record.getMessage()
            for record in caplog.records
        )
        assert error_records(caplog) == []
    finally:
        await REAL_STOP_MQTT(coordinator)


async def test_reply_in_other_letter_case_attaches_to_its_boiler(
    hass: HomeAssistant, loaded_entry
) -> None:
    """Two did-null boilers: the derived id finds the right one despite the letter case."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)
    await collect_with_boilers(coordinator, mixed_rest_entries())
    reply_id = SUPPORTED_ID[:12].swapcase()
    assert reply_id != SUPPORTED_ID[:12]
    try:
        deliver(coordinator, {"macAddress": reply_id, "feature": FEATURE})
        await hass.async_block_till_done(wait_background_tasks=True)

        assert coordinator.boilers[SUPPORTED_ID].feature == FEATURE
        assert coordinator.boilers[UNSUPPORTED_ID].feature == {}
        assert coordinator.drop_counts["boiler_no_device"] == 0
    finally:
        await REAL_STOP_MQTT(coordinator)


async def test_short_device_id_keeps_its_learned_id_across_a_poll(
    hass: HomeAssistant, loaded_entry
) -> None:
    """A deviceId too short to derive from keeps the id a reply taught it."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)
    short_id = "A1B2C3D4E5F6"
    entry = {**did_null_raw(), "deviceId": short_id}
    await collect_with_boilers(coordinator, [entry])
    assert coordinator.boilers[short_id].physical_device_id is None
    try:
        deliver(coordinator, {"macAddress": LEARNED_ID, "feature": FEATURE})
        learned = coordinator.boilers[short_id]
        assert learned.physical_device_id == LEARNED_ID

        await collect_with_boilers(coordinator, [deepcopy(entry)])

        assert coordinator.boilers[short_id] is not learned
        assert coordinator.boilers[short_id].physical_device_id == LEARNED_ID
    finally:
        await REAL_STOP_MQTT(coordinator)


async def test_known_id_outranks_the_derived_one_in_a_two_boiler_account(
    hass: HomeAssistant, loaded_entry
) -> None:
    """A poll without macAddress keeps the known id, and the next reply still attaches."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    wire_transports(coordinator)
    declared = mixed_rest_entries()
    declared[1]["Properties"] = {
        "nickName": "계약 테스트 보일러",
        "did": {"response": {"macAddress": LEARNED_ID}},
    }
    await collect_with_boilers(coordinator, declared)
    known = coordinator.boilers[SUPPORTED_ID]
    assert known.physical_device_id == LEARNED_ID
    try:
        await collect_with_boilers(coordinator, mixed_rest_entries())

        rebuilt = coordinator.boilers[SUPPORTED_ID]
        assert rebuilt is not known
        assert rebuilt.physical_device_id == LEARNED_ID

        deliver(coordinator, {"macAddress": LEARNED_ID, "feature": FEATURE})
        await hass.async_block_till_done(wait_background_tasks=True)
        assert rebuilt.feature == FEATURE
        assert coordinator.boilers[UNSUPPORTED_ID].feature == {}
        assert coordinator.drop_counts["boiler_no_device"] == 0
    finally:
        await REAL_STOP_MQTT(coordinator)


async def test_undeclared_readings_appear_only_when_declared(
    hass: HomeAssistant, loaded_entry
) -> None:
    """Heating intensity and the reservation sensor exist only for a declared feature (2)."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    registry = er.async_get(hass)
    readings = (f"{DEVICE_ID}_heating_intensity", f"{DEVICE_ID}_reservation")

    def present() -> list[bool]:
        return [
            registry.async_get_entity_id("sensor", DOMAIN, unique_id) is not None
            for unique_id in readings
        ]

    undeclared = {
        **FEATURE,
        "heatingIntensityUse": 1,
        "weeklyReservationUse": 1,
        "fastDHWReservationUse": 1,
        "timeCycleReservationUse": 1,
        "dayCycleReservationUse": 1,
    }
    deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": undeclared})
    await hass.async_block_till_done()
    assert present() == [False, False]
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{DEVICE_ID}_monthly_gas_usage")

    schedule_only = {**undeclared, "timeCycleReservationUse": 2}
    deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": schedule_only})
    await hass.async_block_till_done()
    assert present() == [False, True]

    both = {**schedule_only, "heatingIntensityUse": 2}
    deliver(coordinator, {"macAddress": PHYSICAL_ID, "feature": both})
    await hass.async_block_till_done()
    assert present() == [True, True]

"""After MQTT stops, late messages and schedulers change nothing; a restart runs again."""

from __future__ import annotations

import copy
import json
from collections import Counter
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant

from custom_components.navien_smarthome.boiler import (
    BoilerDevice,
    extract_boiler_status,
)
from custom_components.navien_smarthome.coordinator import NavienSmartCoordinator
from custom_components.navien_smarthome.entity import boiler_entities_as_declared

# Kept before loaded_entry patches the class, so a test can run the real start and stop paths.
REAL_START_MQTT = NavienSmartCoordinator.async_start_mqtt
REAL_STOP_MQTT = NavienSmartCoordinator.async_stop_mqtt

MAT_ID = "AABBCCDDEEFF"
AIRONE_ID = "AIRONE001"
BOILER_ID = "0011223344556272"
PHYSICAL_ID = "001122334455"
GAS_METER = {
    "thisYearMonthTotalGasUsage": 142,
    "thisYearMonthTotalHeatGasUsage": 99,
    "thisYearMonthTotalHotWaterGasUsage": 43,
    "gasMeterThisMonth": [
        {
            "year": 2026,
            "month": 8,
            "day": 20,
            "gasMeter": 3,
            "heatGasMeter": 1,
            "hotWaterGasMeter": 2,
        }
    ],
}


def boiler_update(response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Parse a boiler envelope the way the MQTT client does before calling back."""
    payload = json.dumps({"topic": "contract", "payload": {"response": response}})
    result = extract_boiler_status(payload.encode())
    assert result is not None
    return result


def device_states(coordinator: NavienSmartCoordinator) -> list[Any]:
    return copy.deepcopy(
        [
            dict(coordinator.data or {}),
            dict(coordinator.airone),
            dict(coordinator.boilers),
            dict(coordinator.drop_counts),
            set(coordinator.restored_devices),
        ]
    )


async def test_late_messages_after_stop_change_nothing_until_restart(
    hass: HomeAssistant, loaded_entry
) -> None:
    """Every MQTT callback and scheduler is a no-op once the real stop has run, and a real
    start afterwards schedules the silence check on a boiler status again."""
    coordinator: NavienSmartCoordinator = loaded_entry.runtime_data
    listener = Mock()
    remove_listener = coordinator.async_add_listener(listener)
    importer = AsyncMock()
    client = Mock(connected=True, client_id="contract-client")
    client.async_start = AsyncMock()
    client.async_stop = AsyncMock()
    try:
        await REAL_STOP_MQTT(coordinator)
        await hass.async_block_till_done(wait_background_tasks=True)
        before = device_states(coordinator)
        observations = list(coordinator.boiler_observations)
        boiler = coordinator.boilers[BOILER_ID]
        airone = coordinator.airone[AIRONE_ID]

        with patch(
            "custom_components.navien_smarthome.coordinator.async_import_gas_statistics",
            importer,
        ):
            coordinator._handle_reported(
                MAT_ID,
                {"heater": {"left": {"temperature": {"set": 40.0, "current": 39.0}}}},
            )
            coordinator._handle_airone_reported(
                AIRONE_ID, {"roomController": {"running": 0, "airVolume": 3}}
            )
            coordinator._handle_boiler_reported(
                *boiler_update(
                    {
                        "macAddress": PHYSICAL_ID,
                        "status": {"operationMode": 3, "operationBusy": 1},
                    }
                )
            )
            coordinator._handle_boiler_reported(
                *boiler_update({"macAddress": PHYSICAL_ID, "gasMeter": GAS_METER})
            )
            coordinator._handle_boiler_observation({"kind": "late"})
            coordinator._schedule_boiler_silence_check(boiler)
            coordinator._schedule_boiler_gas_refresh(boiler)
            coordinator._schedule_boiler_readback(boiler)
            coordinator._schedule_airone_silence_check(airone)
            coordinator._schedule_airone_readback(airone)
            await hass.async_block_till_done(wait_background_tasks=True)

            assert coordinator._boiler_silence_unsubs == {}
            assert coordinator._boiler_gas_unsubs == {}
            assert coordinator._oneshot_unsubs == set()
            assert coordinator._boiler_gas_tasks == set()
            assert coordinator.boiler_observations == observations
            assert device_states(coordinator) == before
            listener.assert_not_called()
            assert importer.call_count == 0

        with patch(
            "custom_components.navien_smarthome.coordinator.NavienSmartMqtt",
            Mock(return_value=client),
        ):
            await REAL_START_MQTT(coordinator)
        client.async_start.assert_awaited_once()

        coordinator._handle_boiler_reported(
            *boiler_update(
                {
                    "macAddress": PHYSICAL_ID,
                    "status": {"operationMode": 2, "operationBusy": 1},
                }
            )
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert set(coordinator._boiler_silence_unsubs) == {BOILER_ID}
    finally:
        remove_listener()
        await REAL_STOP_MQTT(coordinator)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator._boiler_silence_unsubs == {}


async def test_build_runs_only_when_a_boiler_feature_changes(hass: HomeAssistant) -> None:
    """Repeated notifications build once per boiler; a changed feature builds that one again."""
    first = BoilerDevice.parse(
        {"deviceId": "BOILER-A", "deviceSeq": 1, "serviceCode": 100, "modelCode": "20"}
    )
    second = BoilerDevice.parse(
        {"deviceId": "BOILER-B", "deviceSeq": 2, "serviceCode": 100, "modelCode": "20"}
    )
    assert first is not None and second is not None
    first.feature = {"powerUse": 2}
    listeners: list[Callable[[], None]] = []
    coordinator = Mock(boilers={"BOILER-A": first, "BOILER-B": second})
    coordinator.async_add_listener = Mock(
        side_effect=lambda update: listeners.append(update) or (lambda: None)
    )
    entry = Mock()
    calls: Counter[str] = Counter()

    def build(boiler: BoilerDevice) -> list[Any]:
        calls[boiler.device_id] += 1
        return []

    assert boiler_entities_as_declared(entry, coordinator, Mock(), build) == []
    assert len(listeners) == 1
    for _ in range(3):
        listeners[0]()
    assert calls == {"BOILER-A": 1, "BOILER-B": 1}

    second.feature = {"powerUse": 2, "gasUsageUse": 2}
    listeners[0]()
    listeners[0]()
    assert calls == {"BOILER-A": 1, "BOILER-B": 2}

    first.feature = {"powerUse": 2}
    listeners[0]()
    assert calls == {"BOILER-A": 1, "BOILER-B": 2}

"""Fixtures for tests that run against the real Home Assistant core."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Pytest 9 uses importlib collection and does not prepend the checkout automatically.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.navien_smarthome.airone import AironeDevice
from custom_components.navien_smarthome.api import NavienSmartSession
from custom_components.navien_smarthome.boiler import BoilerDevice
from custom_components.navien_smarthome.const import CONF_HOME_SEQ, DOMAIN
from custom_components.navien_smarthome.coordinator import NavienSmartCoordinator
from custom_components.navien_smarthome.models import NavienDevice


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Tell the HA loader to import the integration from this checkout."""


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare the recorder database URL before HA creates its event loop."""


@pytest.fixture(autouse=True)
def _real_recorder(recorder_mock: Any) -> None:
    """Satisfy the manifest dependency with HA's in-memory recorder fixture."""


def make_mat() -> NavienDevice:
    """Build a representative double temperature mat without contacting Navien."""
    device = NavienDevice.parse(
        {
            "deviceId": "AABBCCDDEEFF",
            "deviceSeq": 1,
            "serviceCode": 200,
            "modelCode": "258",
            "modelName": "EME-520",
            "connected": True,
            "Properties": {
                "nickName": {
                    "mainItem": "계약 테스트 매트",
                    "side": {"left": "좌측", "right": "우측"},
                },
                "registry": {
                    "attributes": {
                        "model": "EME-520",
                        "modelType": "em",
                        "mcu": {"capacity": 2, "modelCode": 258},
                        "functions": {
                            "powerCtrl": True,
                            "lockMode": True,
                            "heatControl": {
                                "unit": "0.5C",
                                "rangeMin": 28,
                                "rangeMax": 50,
                                "safeValue": 37.5,
                                "enableSafe": True,
                            },
                        },
                    }
                },
            },
        }
    )
    assert device is not None
    device.apply_reported(
        {
            "connected": True,
            "operationMode": 1,
            "heater": {
                "left": {
                    "enable": True,
                    "temperature": {"set": 35.0, "current": 34.5},
                },
                "right": {
                    "enable": True,
                    "temperature": {"set": 36.0, "current": 35.5},
                },
            },
            "errorCode": 0,
        }
    )
    return device


def make_airone() -> AironeDevice:
    """Build a representative v2 Airone with one real air-quality value."""
    device = AironeDevice.parse(
        {
            "deviceId": "AIRONE001",
            "deviceSeq": 2,
            "serviceCode": 300,
            "modelCode": "1901",
            "modelName": "NRT-20D",
            "connected": True,
            "Properties": {
                "nickName": "계약 테스트 환기",
                "data": {
                    "did": {
                        "reported": {
                            "roomController": {
                                "deviceId": "RC001",
                                "zoneId": 1,
                                "sensor": ["co2"],
                                "mode": [
                                    {
                                        "name": 4,
                                        "option": 0,
                                        "airVolume": 2,
                                        "supportedAirVolumes": [1, 2, 3],
                                    }
                                ],
                            },
                            "odu": {"filter": [{"type": 1}]},
                            "airMonitor": [],
                        }
                    }
                },
            },
        }
    )
    assert device is not None
    device.apply_reported(
        {
            "roomController": {
                "running": 1,
                "mode": 4,
                "option": 0,
                "airVolume": 2,
                "error": {"code": 0},
            },
            "odu": {"filter": [{"type": 1, "usage": {"percent": 87}}]},
        }
    )
    device.set_air_sensors([{"type": "co2", "value": "605", "grade": 1}])
    return device


def make_boiler() -> BoilerDevice:
    """Build a representative NR-67D boiler with confirmed status fields."""
    device = BoilerDevice.parse(
        {
            "deviceId": "0011223344556272",
            "deviceSeq": 3,
            "serviceCode": 100,
            "modelCode": "20",
            "modelName": "NR-67D",
            "mqttTopicKey": "contract-topic",
            "connected": 1,
            "Properties": {
                "nickName": "계약 테스트 보일러",
                "did": {
                    "response": {
                        "macAddress": "001122334455",
                        "feature": {
                            "powerUse": 2,
                            "gasUsageUse": 2,
                            "outsideTemperatureDisplayUse": 2,
                        },
                    }
                },
            },
        }
    )
    assert device is not None
    device.apply_status(
        {
            "operationMode": 2,
            "operationBusy": 2,
            "actualInsideTemperature": 215,
            "outsideTemperature": 70,
            "DHWUse": 1,
            "faultStatus1": 0,
            "faultStatus2": 0,
            "error": 0,
        },
        now=100.0,
    )
    return device


@pytest.fixture
def devices() -> tuple[NavienDevice, AironeDevice, BoilerDevice]:
    """Return fresh representative devices for one HA instance."""
    return make_mat(), make_airone(), make_boiler()


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Add a Navien config entry with deliberately fake credentials."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="나비엔 스마트 (계약 테스트)",
        unique_id="42",
        data={
            CONF_USERNAME: "nobody@example.invalid",
            CONF_PASSWORD: "not-a-password",
            CONF_HOME_SEQ: 7,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    devices: tuple[NavienDevice, AironeDevice, BoilerDevice],
) -> AsyncIterator[MockConfigEntry]:
    """Load all platforms while replacing every Navien network boundary."""
    mat, airone, boiler = devices
    session = NavienSmartSession(
        access_token="test-token",
        refresh_token=None,
        user_id="test-user",
        account_seq=1,
        user_seq=42,
        homes=[{"homeSeq": 7, "nickname": "계약 테스트"}],
        aws=None,
    )

    async def fake_update(
        coordinator: NavienSmartCoordinator,
    ) -> dict[str, NavienDevice]:
        coordinator.raw_devices = [mat.raw, airone.raw, boiler.raw]
        coordinator.airone = {airone.device_id: airone}
        coordinator.boilers = {boiler.device_id: boiler}
        return {mat.device_id: mat}

    with (
        patch(
            "custom_components.navien_smarthome.NavienSmartApi.async_login",
            new=AsyncMock(return_value=session),
        ),
        patch.object(NavienSmartCoordinator, "_async_update_data", fake_update),
        patch.object(
            NavienSmartCoordinator, "async_restore_state", new=AsyncMock()
        ),
        patch.object(NavienSmartCoordinator, "async_start_mqtt", new=AsyncMock()),
        patch.object(NavienSmartCoordinator, "async_stop_mqtt", new=AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        yield config_entry
        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


def entity_id_for_unique_id(
    hass: HomeAssistant, platform: str, unique_id: str
) -> str:
    """Resolve an entity id without depending on Korean slug generation."""
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id

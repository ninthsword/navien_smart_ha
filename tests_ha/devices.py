"""Synthetic Navien devices for the real Home Assistant tests and tools.

Imports neither pytest nor its Home Assistant plugin, so the devtools that run Home Assistant
itself can use the same devices.
"""

from __future__ import annotations

from custom_components.navien_smarthome.airone import AironeDevice
from custom_components.navien_smarthome.boiler import BoilerDevice
from custom_components.navien_smarthome.models import NavienDevice


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
                            "ondolUse": 2,
                            "ondolTemperatureMin": 60,
                            "ondolTemperatureMax": 130,
                            "hotWaterTemperatureSettingUse": 2,
                            "hotWaterTemperatureMin": 60,
                            "hotWaterTemperatureMax": 120,
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
            "supplyTemperature": 110,
            "ondolTemperatureSetting": 100,
            "hotWaterTemperature": 84,
            "hotWaterTemperatureSetting": 90,
            "DHWUse": 1,
            "faultStatus1": 0,
            "faultStatus2": 0,
            "error": 0,
        },
        now=100.0,
    )
    return device

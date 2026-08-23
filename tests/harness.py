"""Shared test parts: set up paths, count passes and failures, build devices.

**It runs without Home Assistant installed.** `ha_stub` imitates just the modules that are
needed, so `python3 tests/run.py` is the whole story. No external packages either.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_stub  # noqa: E402,F401  (importing it is what registers the stubs)

sys.path.insert(0, str(ROOT / "custom_components"))

SRC = ROOT / "custom_components" / "navien_smarthome"


class Report:
    """Passes and failures for one test file."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def ok(self, cond: bool, label: str) -> None:
        if cond:
            self.passed += 1
            print(f"  ✓ {label}")
        else:
            self.failed += 1
            print(f"  ✗ {label}")

    def section(self, title: str) -> None:
        print(f"\n[{title}]")

    def finish(self) -> int:
        print(f"\n{self.passed} passed / {self.failed} failed")
        return 1 if self.failed else 0


def source(name: str) -> str:
    """Read an integration source file verbatim.

    Recording **why** a thing was done, in comments and docstrings, is a rule of this
    repository, so the tests also check that the reasoning has not been deleted.
    """
    return (SRC / name).read_text("utf-8")


def make_mat(
    *,
    unit: str,
    range_min: float,
    range_max: float,
    zones: dict[str, float],
    capacity: int,
    power_ctrl: bool = True,
    enables: dict[str, bool] | None = None,
    model_code: str = "258",
    model_name: str = "EME-520",
) -> Any:
    """Build one mat.

    **`enable` is derived from the value**, because that is how the device reports it and how
    the app sends it: `enable = (value >= rangeMin)` rides along with the temperature. A
    combination like an off value with `enable: true` does not occur on a real device, so the
    tests do not construct one either.
    """
    from navien_smarthome.models import NavienDevice

    side = {"left": "좌측", "right": "우측"} if capacity == 2 else {}
    nick: dict[str, Any] = {"mainItem": "매트"}
    if side:
        nick["side"] = side

    device = NavienDevice.parse(
        {
            "deviceId": "AABBCCDDEEFF",
            "deviceSeq": 1,
            "serviceCode": 200,
            "modelCode": model_code,
            "modelName": model_name,
            "Properties": {
                "nickName": nick,
                "registry": {
                    "attributes": {
                        "model": model_name,
                        "modelType": "em",
                        "mcu": {"capacity": capacity, "modelCode": int(model_code)},
                        "functions": {
                            "powerCtrl": power_ctrl,
                            "heatControl": {
                                "unit": unit,
                                "rangeMin": range_min,
                                "rangeMax": range_max,
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

    axis = "level" if unit.endswith("L") else "temperature"
    step = 0.5 if unit == "0.5C" else 1.0
    off_value = range_min - step
    overrides = enables or {}
    device.apply_reported(
        {
            "operationMode": 1,
            "heater": {
                zone: {
                    "enable": overrides.get(zone, value > off_value),
                    axis: {"set": value},
                }
                for zone, value in zones.items()
            },
        }
    )
    return device


def make_airone(*, filters: list[float | None], model_code: str = "1901") -> Any:
    """Build one Airone unit, filling in only the filter values."""
    from navien_smarthome.airone import AironeDevice

    device = AironeDevice.parse(
        {
            "deviceSeq": 1,
            "serviceCode": 300,
            "deviceId": "AABB",
            "modelCode": model_code,
            "Properties": {
                "nickName": "환기",
                "data": {
                    "did": {
                        "reported": {
                            "odu": {
                                "filter": [{"type": i + 1} for i in range(len(filters))]
                            },
                            "airMonitor": [],
                            "roomController": {"mode": [], "zoneId": 1, "sensor": []},
                        }
                    }
                },
            },
        }
    )
    assert device is not None
    device.apply_reported(
        {
            "odu": {
                "filter": [
                    {"type": i + 1, "usage": {"percent": percent}}
                    for i, percent in enumerate(filters)
                ]
            }
        }
    )
    return device

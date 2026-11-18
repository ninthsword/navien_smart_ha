"""Exercise the Airone choice and power states without Home Assistant installed."""

from __future__ import annotations

import sys
from unittest.mock import create_autospec

from harness import Report
from homeassistant.components.fan import FanEntityFeature
from navien_smarthome.airone import AironeDevice
from navien_smarthome.coordinator import NavienSmartCoordinator
from navien_smarthome.fan import AironeFan

r = Report()
POWER = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
SPEED = FanEntityFeature.SET_SPEED
PRESET = FanEntityFeature.PRESET_MODE


def make_fan(
    modes: list[dict[str, int | list[int]]], *, model_code: str = "1901"
) -> tuple[AironeFan, AironeDevice]:
    device = AironeDevice.parse(
        {
            "deviceId": "SYNTHETIC_FAN",
            "deviceSeq": 1,
            "serviceCode": 300,
            "modelCode": model_code,
            "Properties": {
                "data": {"did": {"reported": {"roomController": {"mode": modes}}}}
            },
        }
    )
    assert device is not None
    coordinator = create_autospec(
        NavienSmartCoordinator, instance=True, airone={device.device_id: device}
    )
    fan = AironeFan(coordinator, device)
    # The offline CoordinatorEntity stub does not store its constructor arguments.
    fan.coordinator = coordinator
    return fan, device


r.section("choices and features change with the reported mode and option")
fan, device = make_fan(
    [
        {"name": 4, "option": 1, "airVolume": 2, "supportedAirVolumes": [3, 1, 4, 2]},
        {"name": 4, "option": 2, "airVolume": 3},
        {"name": 4, "option": 3, "airVolume": 4},
        {"name": 4, "option": 4, "airVolume": 4},
        {"name": 6, "option": 1, "airVolume": 3},
        {"name": 12, "option": 1, "airVolume": 4},
    ]
)
r.ok(fan.is_on is None and fan.percentage is None, "unknown before a report")
r.ok(fan.supported_features == POWER, "only power with no reported mode")

device.apply_reported(
    {"roomController": {"running": 1, "mode": 4, "option": 1, "airVolume": 2}}
)
r.ok([choice.label for choice in fan._speeds] == ["미풍", "약풍", "강풍"], "ordered speeds")
r.ok(fan.speed_count == 3 and fan.preset_mode is None, "current choice is a speed")
r.ok(fan.preset_modes == ["자동", "터보", "절전"], "all three unordered choices are presets")
r.ok(fan.supported_features == POWER | SPEED | PRESET, "supports speed and presets")

device.apply_reported({"roomController": {"mode": 6, "option": 1, "airVolume": 3}})
r.ok(fan.speed_count == 1 and fan.preset_modes is None, "cooking has one speed")
r.ok(fan.supported_features == POWER | SPEED, "speed feature without any presets")

for mode, option, label in [(12, 1, "auto"), (4, 4, "sleep")]:
    device.apply_reported(
        {"roomController": {"mode": mode, "option": option, "airVolume": 4}}
    )
    r.ok(fan.speed_count == 0 and fan.preset_modes == ["자동"], f"{label} offers auto")
    r.ok(fan.supported_features == POWER | PRESET, f"{label} has no speed feature")
    r.ok(fan.preset_mode == "자동" and fan.percentage is None, f"{label} reports auto")

fan, device = make_fan([{"name": 4, "option": 2}, {"name": 4, "option": 3}])
device.apply_reported({"roomController": {"running": 1, "mode": 4, "option": 3}})
r.ok(fan.preset_modes == ["터보", "절전"], "a device with turbo and saving only")
r.ok(fan.supported_features == POWER | PRESET, "turbo and saving do not give speeds")
r.ok(fan.preset_mode == "절전" and fan.percentage is None, "saving is the current preset")

fan, device = make_fan([], model_code="20")
device.apply_reported(
    {"roomController": {"running": 1, "mode": 4, "option": 1, "airVolume": 1}}
)
r.ok(fan.speed_count == 3, "older unit's speeds come from the merged table")
r.ok(fan.preset_modes == ["자동", "터보", "절전"], "older unit's merged presets are offered")

r.section("the current choice follows the same exact label match as the select")
fan, device = make_fan([{"name": 4, "option": 1, "supportedAirVolumes": [1, 5, 6]}])
device.apply_reported(
    {"roomController": {"running": 1, "mode": 4, "option": 1, "airVolume": 5}}
)
r.ok(fan.preset_modes == ["기저"] and fan.preset_mode == "기저", "first baseline wins")
device.apply_reported({"roomController": {"airVolume": 6}})
r.ok(device.current_fan_label() is None, "deduplicated value does not match the select")
r.ok(fan.preset_mode is None and fan.percentage is None, "fan has no matching choice")

r.section("power is independent of the current fan choice")
fan, device = make_fan([{"name": 4, "option": 2, "airVolume": 3}])
for running, is_on, percentage in [
    (1, True, None),
    (2, False, 0),
    (3, False, 0),
    (4, False, 0),
    (None, None, None),
]:
    device.apply_reported(
        {"roomController": {"running": running, "mode": 4, "option": 2}}
    )
    r.ok(fan.is_on is is_on, f"running={running} gives is_on={is_on}")
    r.ok(fan.percentage == percentage, f"running={running}, percentage={percentage}")
    r.ok(fan.preset_mode == "터보", f"running={running} keeps the reported preset")

device.apply_reported({"odu": {"running": 1}})
r.ok(fan.is_on is True, "missing controller running falls back to the outdoor unit")

sys.exit(r.finish())

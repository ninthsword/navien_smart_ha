"""Verify the previous local day's gas reading and the response freshness rule."""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from unittest.mock import create_autospec, patch

from harness import Report
from navien_smarthome.boiler import BOILER_GAS_METER_UPDATE, BoilerDevice
from navien_smarthome.coordinator import NavienSmartCoordinator
from navien_smarthome.sensor import BoilerYesterdayGasSensor

r = Report()

device = BoilerDevice.parse(
    {
        "deviceId": "0011223344556272",
        "deviceSeq": 14,
        "serviceCode": 100,
        "modelCode": "20",
        "connected": 1,
        "Properties": {"nickName": "테스트 보일러"},
    }
)
assert device is not None
coordinator = create_autospec(
    NavienSmartCoordinator, instance=True, boilers={device.device_id: device}
)
sensor = BoilerYesterdayGasSensor(coordinator, device)
sensor.coordinator = coordinator


def row(
    year: int,
    month: int,
    day: int,
    total: int | None,
    heating: int | None = 0,
    hot_water: int | None = 0,
) -> dict[str, int | None]:
    return {
        "year": year,
        "month": month,
        "day": day,
        "gasMeter": total,
        "heatGasMeter": heating,
        "hotWaterGasMeter": hot_water,
    }


def check(
    label: str,
    today: date,
    response: dict[str, Any],
    expected: float | None,
    attributes: dict[str, Any] | None = None,
) -> None:
    assert device is not None
    device.apply_status({BOILER_GAS_METER_UPDATE: response})
    now = datetime.combine(today, time(1), tzinfo=timezone(timedelta(hours=9)))
    with patch("navien_smarthome.sensor.dt_util.now", return_value=now):
        r.ok(sensor.native_value == expected, f"{label}: value")
        r.ok(sensor.extra_state_attributes == attributes, f"{label}: attributes")


r.section("the previous local date and tenths of a cubic meter")

today = date(2026, 8, 21)
current = row(2026, 8, 21, 0)
previous = row(2026, 8, 20, 35, 21, 14)
check(
    "yesterday in this month's array, with a zero but non-null row for today",
    today,
    {"gasMeterThisMonth": [previous, current]},
    3.5,
    {"기준일": "2026-08-20", "난방": 2.1, "온수": 1.4},
)
check(
    "first of a month uses last month's array",
    date(2026, 9, 1),
    {
        "gasMeterThisMonth": [row(2026, 9, 1, 0)],
        "gasMeterLastMonth": [row(2026, 8, 31, 56, 32, 24)],
    },
    5.6,
    {"기준일": "2026-08-31", "난방": 3.2, "온수": 2.4},
)
check(
    "first of January uses the preceding December",
    date(2027, 1, 1),
    {
        "gasMeterThisMonth": [row(2027, 1, 1, 0)],
        "gasMeterLastMonth": [row(2026, 12, 31, 12, 4, 8)],
    },
    1.2,
    {"기준일": "2026-12-31", "난방": 0.4, "온수": 0.8},
)
check(
    "a missing date within the present month reads zero",
    today,
    {"gasMeterThisMonth": [current]},
    0.0,
    {"기준일": "2026-08-20", "난방": 0.0, "온수": 0.0},
)
check(
    "a missing date within last month's array reads zero",
    date(2026, 9, 1),
    {
        "gasMeterThisMonth": [row(2026, 9, 1, 0)],
        "gasMeterLastMonth": [row(2026, 8, 30, 10)],
    },
    0.0,
    {"기준일": "2026-08-31", "난방": 0.0, "온수": 0.0},
)
for heating, hot_water, attributes in (
    (None, 14, {"온수": 1.4}),
    (21, None, {"난방": 2.1}),
    (None, None, {}),
):
    check(
        "null component values omit only their own attributes",
        today,
        {"gasMeterThisMonth": [row(2026, 8, 20, 35, heating, hot_water), current]},
        3.5,
        {"기준일": "2026-08-20", **attributes},
    )


r.section("unknown readings never carry a date or component attributes")

check("no gas response", today, {}, None)
check(
    "yesterday's null total is unknown even with non-null components",
    today,
    {"gasMeterThisMonth": [row(2026, 8, 20, None, 21, 14), current]},
    None,
)
check(
    "yesterday's all-null row is unknown",
    today,
    {"gasMeterThisMonth": [row(2026, 8, 20, None, None, None), current]},
    None,
)
for last_month in (None, {}, [row(2026, 7, 31, 56)], [row(2025, 8, 31, 56)]):
    check(
        "absent or mismatched last month's array is unknown",
        date(2026, 9, 1),
        {
            "gasMeterThisMonth": [row(2026, 9, 1, 0)],
            "gasMeterLastMonth": last_month,
        },
        None,
    )
check(
    "last month's key omitted on the first is unknown",
    date(2026, 9, 1),
    {"gasMeterThisMonth": [row(2026, 9, 1, 0)]},
    None,
)
check(
    "a response from before midnight has a future all-null row for today",
    today,
    {"gasMeterThisMonth": [previous, row(2026, 8, 21, None, None, None)]},
    None,
)
check(
    "today's null total does not prove freshness even with non-null components",
    today,
    {"gasMeterThisMonth": [previous, row(2026, 8, 21, None, 1, 2)]},
    None,
)
check(
    "today's row missing is unknown even when yesterday has a value",
    today,
    {"gasMeterThisMonth": [previous]},
    None,
)
check(
    "a response from another month is unknown",
    today,
    {"gasMeterThisMonth": [row(2026, 7, 20, 35), row(2026, 7, 21, 0)]},
    None,
)
check(
    "today's row from another year is unknown",
    today,
    {"gasMeterThisMonth": [previous, row(2025, 8, 21, 0)]},
    None,
)
check(
    "today's row in last month's array does not prove freshness",
    today,
    {"gasMeterThisMonth": [previous], "gasMeterLastMonth": [current]},
    None,
)
check(
    "malformed rows are skipped",
    today,
    {"gasMeterThisMonth": [None, "invalid", {"year": "invalid"}, previous, current]},
    3.5,
    {"기준일": "2026-08-20", "난방": 2.1, "온수": 1.4},
)


r.section("the snapshot has no long-term statistics of its own")

r.ok(sensor._attr_name == "어제 가스 사용량", "the name describes the previous day")
r.ok(
    sensor._attr_unique_id == f"{device.device_id}_yesterday_gas_usage",
    "the replacement has its own unique id",
)
r.ok(sensor._attr_device_class == "gas", "the device class is gas")
r.ok(sensor._attr_native_unit_of_measurement == "m³", "the unit is cubic meters")
r.ok(sensor._attr_suggested_display_precision == 1, "one decimal place is suggested")
r.ok(getattr(sensor, "_attr_state_class", None) is None, "no state class is assigned")
r.ok(getattr(sensor, "last_reset", None) is None, "there is no last_reset")

sys.exit(r.finish())

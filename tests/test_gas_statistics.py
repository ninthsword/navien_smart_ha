"""Verify the path that moves gas-usage history into long-term statistics.

The responses used here are **exactly the shape received from a real NR-67D**. That one gas
query returns two months of daily figures alongside two years of monthly ones, and that a
day which has not happened yet arrives as `null`, are both observations.
"""

from __future__ import annotations

import sys
from datetime import date

from harness import Report, source
from navien_smarthome.boiler import BoilerDevice, extract_boiler_status
from navien_smarthome.gas_statistics import GAS_STATISTIC_KINDS, statistic_id

r = Report()


def make_boiler() -> BoilerDevice:
    device = BoilerDevice.parse(
        {
            "deviceId": "0011223344556272",
            "deviceSeq": 14,
            "serviceCode": 100,
            "modelCode": "20",
            "modelName": "NR-67D",
            "mqttTopicKey": "private-topic-key",
            "connected": 1,
            "Properties": {
                "nickName": "보일러",
                "did": {
                    "response": {
                        "macAddress": "001122334455",
                        "feature": {"gasUsageUse": 2},
                    }
                },
            },
        }
    )
    assert device is not None
    return device


def day_row(year: int, month: int, day: int, total, heat: int | None = 0, water=None):
    return {
        "year": year,
        "month": month,
        "day": day,
        "gasMeter": total,
        "heatGasMeter": heat,
        "hotWaterGasMeter": total if water is None else water,
    }


# The same four arrays as a real device response. July appears in **both** the daily and the
# monthly ones.
GAS_METER = {
    "gasMeterLastMonth": [day_row(2026, 7, 30, 30), day_row(2026, 7, 31, 60)],
    "gasMeterThisMonth": [
        day_row(2026, 8, 1, 60),
        day_row(2026, 8, 2, 50),
        # A day that has not happened yet has all three values null.
        day_row(2026, 8, 3, None, None, None),
    ],
    "gasMeterLastYear": [
        {
            "year": 2025,
            "month": 12,
            "day": 0,
            "gasMeter": 1610,
            "heatGasMeter": 1208,
            "hotWaterGasMeter": 402,
        }
    ],
    "gasMeterThisYear": [
        {
            "year": 2026,
            "month": 7,
            "day": 0,
            "gasMeter": 142,
            "heatGasMeter": 0,
            "hotWaterGasMeter": 142,
        },
        {
            "year": 2026,
            "month": 8,
            "day": 0,
            "gasMeter": 74,
            "heatGasMeter": 0,
            "hotWaterGasMeter": 74,
        },
        {
            "year": 2026,
            "month": 9,
            "day": 0,
            "gasMeter": None,
            "heatGasMeter": None,
            "hotWaterGasMeter": None,
        },
    ],
    "thisYearMonthTotalGasUsage": 74,
}


r.section("one query returns all four arrays")

envelope = (
    b'{"payload":{"response":{"macAddress":"001122334455","gasMeter":'
    b'{"gasMeterThisMonth":[{"year":2026,"month":8,"day":1,"gasMeter":6}]}}}}'
)
parsed = extract_boiler_status(envelope)
r.ok(parsed is not None, "the gas response is accepted as a state update")

device = make_boiler()
device.apply_status({"__gas_meter__": GAS_METER}, now=100.0)
history = device.gas_history()

r.ok([b.start for b in history] == sorted(b.start for b in history), "returned in chronological order")
r.ok(history[0].start == date(2025, 12, 1), "the oldest bucket is last December")
r.ok(history[0].monthly, "that bucket is a monthly one")
r.ok(history[0].total == 161.0, "monthly raw values are divided by 10 as well")
r.ok(history[0].heating == 120.8 and history[0].hot_water == 40.2, "heating and hot water are split out")


r.section("an overlapping month keeps only its daily figures")

months = [b.start for b in history if b.monthly]
r.ok(date(2026, 7, 1) not in months, "July has daily figures, so it gets no monthly bucket")
r.ok(date(2026, 8, 1) not in months, "the same holds for August")
r.ok(months == [date(2025, 12, 1)], "only a month with no daily figures stays monthly")
r.ok(
    sum(b.total for b in history if not b.monthly) == 20.0,
    "the daily buckets are two days in July and two in August",
)


r.section("a date that has not happened yet never becomes a statistic")

starts = [b.start for b in history]
r.ok(date(2026, 8, 3) not in starts, "a null day means no data, not zero usage")
r.ok(date(2026, 9, 1) not in starts, "a null month is left out too")
r.ok(
    date(2026, 8, 1) in starts and date(2026, 8, 2) in starts,
    "every day that carried a value is included",
)


r.section("monthly and daily buckets are never mixed")

device.apply_status(
    {"__gas_meter__": {"gasMeterThisYear": [day_row(2026, 5, 12, 10)]}}, now=200.0
)
r.ok(device.gas_history() == [], "a daily row in a monthly array means something unknown and is discarded")

device.apply_status(
    {"__gas_meter__": {"gasMeterThisMonth": [day_row(2026, 2, 30, 10)]}}, now=300.0
)
r.ok(device.gas_history() == [], "a date that does not exist on the calendar is discarded")


r.section("the cycle start comes from the server, not the wall clock")

device.apply_status({"__gas_meter__": GAS_METER}, now=400.0)
r.ok(device.gas_month_start == date(2026, 8, 1), "the first of the month this month's array names")
device.apply_status({"__gas_meter__": {}}, now=500.0)
r.ok(device.gas_month_start is None, "with no array, the month is not guessed")
r.ok(
    "def last_reset" in source("sensor.py") and "gas_month_start" in source("sensor.py"),
    "the monthly sensor exports last_reset",
)
r.ok(
    "start_of_local_day()" in source("sensor.py"),
    "the daily sensor uses midnight today as its cycle start",
)


r.section("statistic ids and series")

boiler = make_boiler()
r.ok(
    statistic_id(boiler, "total") == "navien_smarthome:boiler_0011223344556272_gas_total",
    "a statistic id begins with the integration domain",
)
r.ok(
    set(GAS_STATISTIC_KINDS) == {"total", "heating", "hot_water"},
    "the same three series as the app",
)


r.section("the running total continues from the preceding bucket")

stats_source = source("gas_statistics.py")
r.ok("running += value" in stats_source, "each bucket adds to the running total")
r.ok("_async_baseline" in stats_source, "the total is carried over from what is already stored")
r.ok(
    'float(total) - float(state)' in stats_source,
    "subtracting the bucket from an inclusive total gives the total up to just before it",
)
r.ok("async_add_external_statistics" in stats_source, "written as external statistics")
r.ok(
    "async_import_gas_statistics" in source("coordinator.py"),
    "statistics are applied on every gas response",
)
r.ok(
    "boiler_gas_statistics_failures" in source("coordinator.py"),
    "a statistics failure is counted rather than stopping the integration",
)
r.ok('"recorder"' in source("manifest.json"), "the recorder dependency is declared")

diagnostics_source = source("diagnostics.py")
r.ok("gas_history_span" in diagnostics_source, "diagnostics records the history span")
r.ok("gas_history_daily" in diagnostics_source, "daily and monthly bucket counts are recorded separately")
r.ok("gas_arrays" in diagnostics_source, "the array names the server sent are recorded")
r.ok(
    "「이력」 화면에는 안 나옵니다" in source("../../README.md"),
    "the README says this is statistics, not the history screen",
)
r.ok("statistic-graph" in source("../../README.md"), "the README names the card that displays it")


r.section("the reasoning is recorded in the code")

r.ok("purge_keep_days" in stats_source, "it records how retention differs between state history and statistics")
r.ok("are overwritten" in stats_source, "it records the rewrite-and-self-heal design")
r.ok("day: 0" in source("boiler.py"), "it records the observation that monthly rows carry day=0")


sys.exit(r.finish())

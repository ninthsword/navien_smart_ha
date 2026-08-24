"""Verify gas cumulative continuity in Home Assistant's real recorder."""

from __future__ import annotations

from datetime import date, timedelta

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

from custom_components.navien_smarthome.gas_statistics import (
    async_import_gas_statistics,
    statistic_id,
)

from .conftest import make_boiler


def day_row(day: int, total: int) -> dict[str, int]:
    """Return one daily gas row in the server's tenths-of-m³ unit."""
    return {
        "year": 2026,
        "month": 8,
        "day": day,
        "gasMeter": total,
        "heatGasMeter": 0,
        "hotWaterGasMeter": total,
    }


async def read_total_rows(
    hass: HomeAssistant, stat_id: str
) -> list[dict[str, float]]:
    """Read the imported rows on the recorder executor."""
    start = dt_util.start_of_local_day(date(2026, 8, 1))
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        start + timedelta(days=4),
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return result[stat_id]


async def test_gas_rewrite_keeps_the_preceding_cumulative_sum(
    hass: HomeAssistant,
) -> None:
    """A later shorter server window splices onto the prior cumulative series."""
    boiler = make_boiler()
    boiler.apply_status(
        {
            "__gas_meter__": {
                "gasMeterThisMonth": [day_row(1, 10), day_row(2, 20)]
            }
        },
        now=100.0,
    )
    stat_id = statistic_id(boiler, "total")

    assert await async_import_gas_statistics(hass, boiler) == 2
    await async_recorder_block_till_done(hass)
    rows = await read_total_rows(hass, stat_id)
    assert [(row["state"], row["sum"]) for row in rows] == [
        (1.0, 1.0),
        (2.0, 3.0),
    ]

    boiler.apply_status(
        {
            "__gas_meter__": {
                "gasMeterThisMonth": [day_row(2, 25), day_row(3, 40)]
            }
        },
        now=200.0,
    )
    assert await async_import_gas_statistics(hass, boiler) == 2
    await async_recorder_block_till_done(hass)
    rows = await read_total_rows(hass, stat_id)

    assert [(row["state"], row["sum"]) for row in rows] == [
        (1.0, 1.0),
        (2.5, 3.5),
        (4.0, 7.5),
    ]

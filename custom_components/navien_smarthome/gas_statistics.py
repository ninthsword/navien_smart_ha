"""Move the boiler's gas usage into Home Assistant long-term statistics.

A plain sensor would record **only the time HA was running**. Gaps from restarts, power
cuts, or integration errors would never fill in, and missing the moment a month rolls over
would lose that whole month.

None of that is necessary. One gas query returns **everything** the app's gas-usage screen
draws: two months of daily figures and two years of monthly ones. So every query rewrites
the statistics for that whole span. Rows at the same timestamp are overwritten, so HA can
be off for days and still repair itself on the next query, and it follows along when Navien
corrects a figure after the fact. **What the server says now** is always the truth, not what
we stored.

These go in as external statistics rather than a `sensor` entity for two reasons. An entity
cannot take a value at a past timestamp; and state history is purged once `purge_keep_days`
passes, while statistics are not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .boiler import BoilerDevice, GasUsageBucket
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Look-back windows used to find the preceding cumulative sum, nearest first — it is usually
# in the month just before, so the search normally ends on the first one. The last window
# means "there is nothing earlier than this".
_BASELINE_LOOKBACK_DAYS: tuple[int, ...] = (40, 400, 1200)

# The same three splits the app's gas-usage screen shows.
GAS_STATISTIC_KINDS: dict[str, str] = {
    "total": "가스 사용량",
    "heating": "난방 가스 사용량",
    "hot_water": "온수 가스 사용량",
}


def statistic_id(device: BoilerDevice, kind: str) -> str:
    """A statistic id of the form ``navien_smarthome:boiler_<device>_gas_total``.

    Statistic ids accept the same characters as an entity_id, so only lower case and
    underscores survive.
    """
    safe = "".join(ch if ch.isalnum() else "_" for ch in device.device_id.lower())
    return f"{DOMAIN}:boiler_{safe}_gas_{kind}"


def _metadata(device: BoilerDevice, kind: str) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{device.nickname} {GAS_STATISTIC_KINDS[kind]}",
        source=DOMAIN,
        statistic_id=statistic_id(device, kind),
        unit_class="volume",
        unit_of_measurement=UnitOfVolume.CUBIC_METERS,
    )


def _value(bucket: GasUsageBucket, kind: str) -> float:
    if kind == "heating":
        return bucket.heating
    if kind == "hot_water":
        return bucket.hot_water
    return bucket.total


def _start(bucket: GasUsageBucket) -> datetime:
    """Local midnight at which the bucket starts. A statistics row can only sit on the hour."""
    return dt_util.start_of_local_day(bucket.start)


async def _async_baseline(
    hass: HomeAssistant, stat_id: str, first_start: datetime
) -> float:
    """The cumulative total **up to just before** the span being rewritten.

    A statistics ``sum`` accumulates across the whole series, so cutting the front off throws
    everything after it out of line. The range the server returns loses its front as the year
    turns, so the running total up to that point is recovered from what is already stored and
    spliced back on.

    **Two things are checked, in order.**

    1. If a row already sits at `first_start`, use it. Its ``sum`` **includes** that bucket,
       so that bucket's usage has to be subtracted to get the total up to just before it.
    2. Otherwise use the ``sum`` of **the last row before that point**. There really are
       cases with no row there: if a month had no usage at all, the server sends no row for
       it. This used to return 0, which restarted a two-year series from zero and drew a
       **huge negative usage at the boundary**, because HA derives usage from differences in
       ``sum``.

    With neither available this is the first write, so 0 is correct.
    """
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        first_start,
        first_start + timedelta(hours=1),
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    for row in rows.get(stat_id) or ():
        total = row.get("sum")
        state = row.get("state")
        if total is None or state is None:
            continue
        return float(total) - float(state)

    # No row there. Carry on from the last cumulative total stored before it.
    return await _async_last_sum_before(hass, stat_id, first_start)


async def _async_last_sum_before(
    hass: HomeAssistant, stat_id: str, start: datetime
) -> float:
    """The last cumulative total stored **before** ``start``, or 0 if there is none.

    This does not scan the whole range: given no start, `statistics_during_period` reads from
    the very beginning of what is stored, while only the last row is needed. So the search
    widens backwards window by window, and finding nothing means this is the first write.
    """
    for days in _BASELINE_LOOKBACK_DAYS:
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start - timedelta(days=days),
            start,
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        found = [row for row in rows.get(stat_id) or () if row.get("sum") is not None]
        if found:
            total = found[-1].get("sum")
            if total is not None:
                return float(total)
    return 0.0


async def async_import_gas_statistics(
    hass: HomeAssistant, device: BoilerDevice
) -> int:
    """Apply the gas history the server returned to long-term statistics; return the bucket count."""
    buckets = device.gas_history()
    if not buckets:
        return 0

    first_start = _start(buckets[0])
    for kind in GAS_STATISTIC_KINDS:
        stat_id = statistic_id(device, kind)
        running = await _async_baseline(hass, stat_id, first_start)
        rows: list[StatisticData] = []
        for bucket in buckets:
            value = _value(bucket, kind)
            running += value
            rows.append(
                StatisticData(start=_start(bucket), state=value, sum=running)
            )
        async_add_external_statistics(hass, _metadata(device, kind), rows)

    _LOGGER.debug("가스 통계 %d칸을 반영했습니다", len(buckets))
    return len(buckets)

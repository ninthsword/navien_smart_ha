"""보일러 가스 사용량을 Home Assistant 장기 통계로 옮긴다.

센서 하나만 두면 **HA 가 켜져 있던 시간만** 기록된다. 재시작·정전·통합 오류로
비는 구간은 영영 채워지지 않고, 달이 바뀌는 순간을 놓치면 그 달치가 통째로
사라진다.

그럴 필요가 없다. 가스 조회 응답 한 번에 앱의 가스 사용량 화면이 그리는 자료가
**전부** 들어 있다 — 일별 두 달치와 월별 두 해치다. 그래서 조회할 때마다 그
기간의 통계를 통째로 다시 써넣는다. 같은 시각의 행은 덮어써지므로 HA 가 며칠
꺼져 있었어도 다음 조회 한 번에 스스로 메워지고, 나비엔이 값을 나중에 고쳐도
따라간다. 우리가 보관한 값이 아니라 **서버가 지금 말하는 값**이 언제나 정답이다.

`sensor` 엔티티가 아니라 외부 통계로 넣는 이유는 두 가지다. 첫째, 엔티티에는
과거 시각의 값을 넣을 수 없다. 둘째, 상태 이력(states)은 `purge_keep_days` 가
지나면 지워지지만 통계는 지워지지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .boiler import BoilerDevice, GasUsageBucket
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# 앱의 가스 사용량 화면이 나누는 것과 같은 세 갈래다.
GAS_STATISTIC_KINDS: dict[str, str] = {
    "total": "가스 사용량",
    "heating": "난방 가스 사용량",
    "hot_water": "온수 가스 사용량",
}


def statistic_id(device: BoilerDevice, kind: str) -> str:
    """``navien_smarthome:boiler_<기기>_gas_total`` 형태의 통계 ID.

    통계 ID 는 entity_id 와 같은 문자만 쓸 수 있어서 소문자와 밑줄만 남긴다.
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
    """그 칸이 시작하는 현지 자정. 통계 행은 정시에만 놓을 수 있다."""
    return dt_util.start_of_local_day(bucket.start)


async def _async_baseline(
    hass: HomeAssistant, stat_id: str, first_start: datetime
) -> float:
    """이번에 다시 쓸 구간 **직전까지의 누적값**.

    통계의 ``sum`` 은 시리즈 전체에 걸친 누적이라 앞을 잘라내면 뒤가 전부
    어긋난다. 서버가 주는 범위는 해가 바뀌면 앞쪽이 빠지므로, 이미 저장된 첫
    행에서 그때까지의 누적을 되찾아 이어 붙인다.

    ``sum`` 은 그 칸까지 **포함한** 누적이므로 그 칸의 사용량을 빼야 직전까지의
    누적이 된다.
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
    return 0.0


async def async_import_gas_statistics(
    hass: HomeAssistant, device: BoilerDevice
) -> int:
    """서버가 준 가스 이력을 장기 통계에 반영하고 넣은 칸 수를 돌려준다."""
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

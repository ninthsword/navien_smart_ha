"""가스 사용량 이력을 장기 통계로 옮기는 길을 검증한다.

여기 쓰는 응답은 **실기기(NR-67D)에서 받은 모양 그대로**다. 한 번의 가스 조회에
일별 두 달치와 월별 두 해치가 함께 온다는 것, 아직 오지 않은 날은 값이 `null`
로 온다는 것 모두 실측이다.
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


def day_row(year: int, month: int, day: int, total, heat=0, water=None):
    return {
        "year": year,
        "month": month,
        "day": day,
        "gasMeter": total,
        "heatGasMeter": heat,
        "hotWaterGasMeter": total if water is None else water,
    }


# 실기기 응답과 같은 네 배열. 7월은 일별과 월별에 **둘 다** 들어 있다.
GAS_METER = {
    "gasMeterLastMonth": [day_row(2026, 7, 30, 30), day_row(2026, 7, 31, 60)],
    "gasMeterThisMonth": [
        day_row(2026, 8, 1, 60),
        day_row(2026, 8, 2, 50),
        # 아직 오지 않은 날은 세 값이 모두 null 이다.
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


r.section("한 번의 조회에 네 배열이 함께 온다")

envelope = (
    b'{"payload":{"response":{"macAddress":"001122334455","gasMeter":'
    b'{"gasMeterThisMonth":[{"year":2026,"month":8,"day":1,"gasMeter":6}]}}}}'
)
parsed = extract_boiler_status(envelope)
r.ok(parsed is not None, "가스 응답을 상태 갱신으로 받는다")

device = make_boiler()
device.apply_status({"__gas_meter__": GAS_METER}, now=100.0)
history = device.gas_history()

r.ok([b.start for b in history] == sorted(b.start for b in history), "시간순으로 준다")
r.ok(history[0].start == date(2025, 12, 1), "가장 오래된 칸은 작년 12월이다")
r.ok(history[0].monthly, "그 칸은 월별 칸이다")
r.ok(history[0].total == 161.0, "월별 원시값도 10으로 나눈다")
r.ok(history[0].heating == 120.8 and history[0].hot_water == 40.2, "난방·온수를 나눈다")


r.section("겹치는 달은 일별만 남긴다")

months = [b.start for b in history if b.monthly]
r.ok(date(2026, 7, 1) not in months, "일별이 있는 7월은 월별 칸을 만들지 않는다")
r.ok(date(2026, 8, 1) not in months, "일별이 있는 8월도 마찬가지다")
r.ok(months == [date(2025, 12, 1)], "일별이 없는 달만 월별로 남는다")
r.ok(
    sum(b.total for b in history if not b.monthly) == 20.0,
    "일별 칸은 7월 두 날과 8월 두 날뿐이다",
)


r.section("아직 오지 않은 날짜는 통계로 만들지 않는다")

starts = [b.start for b in history]
r.ok(date(2026, 8, 3) not in starts, "값이 null 인 날은 사용량 0 이 아니라 없는 것이다")
r.ok(date(2026, 9, 1) not in starts, "값이 null 인 달도 빼놓는다")
r.ok(
    date(2026, 8, 1) in starts and date(2026, 8, 2) in starts,
    "값이 온 날은 모두 넣는다",
)


r.section("월별 칸과 일별 칸을 섞지 않는다")

device.apply_status(
    {"__gas_meter__": {"gasMeterThisYear": [day_row(2026, 5, 12, 10)]}}, now=200.0
)
r.ok(device.gas_history() == [], "월별 배열에 일별 행이 오면 뜻을 모르므로 버린다")

device.apply_status(
    {"__gas_meter__": {"gasMeterThisMonth": [day_row(2026, 2, 30, 10)]}}, now=300.0
)
r.ok(device.gas_history() == [], "달력에 없는 날짜는 버린다")


r.section("주기 시작은 벽시계가 아니라 서버 값으로 잡는다")

device.apply_status({"__gas_meter__": GAS_METER}, now=400.0)
r.ok(device.gas_month_start == date(2026, 8, 1), "이번 달 배열이 말하는 달의 1일이다")
device.apply_status({"__gas_meter__": {}}, now=500.0)
r.ok(device.gas_month_start is None, "배열이 없으면 달을 추측하지 않는다")
r.ok(
    "def last_reset" in source("sensor.py") and "gas_month_start" in source("sensor.py"),
    "월간 센서가 last_reset 을 내보낸다",
)
r.ok(
    "start_of_local_day()" in source("sensor.py"),
    "일간 센서는 오늘 자정을 주기 시작으로 쓴다",
)


r.section("통계 ID 와 갈래")

boiler = make_boiler()
r.ok(
    statistic_id(boiler, "total") == "navien_smarthome:boiler_0011223344556272_gas_total",
    "통계 ID 는 통합 도메인으로 시작한다",
)
r.ok(
    set(GAS_STATISTIC_KINDS) == {"total", "heating", "hot_water"},
    "앱과 같은 세 갈래를 만든다",
)


r.section("누적은 앞 칸에 이어 붙인다")

stats_source = source("gas_statistics.py")
r.ok("running += value" in stats_source, "칸마다 누적을 더한다")
r.ok("_async_baseline" in stats_source, "이미 저장된 앞 구간에서 누적을 이어받는다")
r.ok(
    'float(total) - float(state)' in stats_source,
    "그 칸을 포함한 누적에서 그 칸을 빼야 직전까지의 누적이다",
)
r.ok("async_add_external_statistics" in stats_source, "외부 통계로 넣는다")
r.ok(
    "async_import_gas_statistics" in source("coordinator.py"),
    "가스 응답을 받을 때마다 통계를 반영한다",
)
r.ok(
    "boiler_gas_statistics_failures" in source("coordinator.py"),
    "통계 실패가 통합을 멈추지 않고 집계된다",
)
r.ok('"recorder"' in source("manifest.json"), "recorder 의존을 선언한다")

diagnostics_source = source("diagnostics.py")
r.ok("gas_history_span" in diagnostics_source, "진단에 이력 구간을 남긴다")
r.ok("gas_history_daily" in diagnostics_source, "일별·월별 칸 수를 나눠 남긴다")
r.ok("gas_arrays" in diagnostics_source, "서버가 준 배열 이름을 남긴다")
r.ok(
    "「이력」 화면에는 안 나옵니다" in source("../../README.md"),
    "README 에 이력 화면이 아니라 통계라는 것을 적었다",
)
r.ok("statistic-graph" in source("../../README.md"), "README 에 볼 수 있는 카드를 적었다")


r.section("근거를 코드에 남겼다")

r.ok("purge_keep_days" in stats_source, "상태 이력과 통계의 보관 기간 차이를 적었다")
r.ok("덮어써지" in stats_source, "다시 써넣어 스스로 메우는 설계를 적었다")
r.ok("day: 0" in source("boiler.py"), "월별 행이 day=0 이라는 실측 근거를 적었다")


sys.exit(r.finish())

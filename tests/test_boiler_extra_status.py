"""그동안 파싱만 하고 버리던 보일러 상태 필드를 검증한다.

여기 쓰는 값은 실기기(NR-67D) 진단 덤프에서 그대로 가져왔다. 44개 status 필드
중 25개가 쓰이지 않고 있었고, 그중 뜻이 확실한 것만 연다. 확실하지 않은 것은
**이름을 붙이지 않고 원시값으로** 남기는 것이 이 저장소의 규칙이다.
"""

from __future__ import annotations

import sys

from harness import Report, source
from navien_smarthome.boiler import BoilerDevice

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
                        # 이 기기는 온수를 분명히 쓰는데도 hotWaterUse 가 1 이다.
                        # feature 의 *Use 를 status 와 같은 규약으로 읽으면 안 된다.
                        "feature": {"hotWaterUse": 1, "outsideTemperatureDisplayUse": 1},
                    }
                },
            },
        }
    )
    assert device is not None
    return device


# 실기기 진단 덤프의 status 그대로다.
STATUS = {
    "outsideTemperature": 270,
    "DHWUse": 1,
    "DHWUseSustained": 1,
    "DHWInternalFlowRate": 32,
    "heatFlowRate": 22,
    "wifiRssi": 54,
    "faultStatus1": 0,
    "faultStatus2": 0,
    "heatingIntensityModeSetting": 3,
    "timeCycleReservationSettingHour": 1,
    "timeCycleReservationSettingMinute": 10,
    "dayCycleReservationSetting": "000000000000000000000000",
    "programReservationUse": 1,
    "fastDHWReservationUse": 1,
}

device = make_boiler()
device.apply_status(STATUS, now=100.0)


r.section("외기 온도")

r.ok(device.outside_temperature == 27.0, "0.1℃ 단위로 읽는다")
# 이 기기는 outsideTemperatureDisplayUse 가 1 인데도 값이 온다. 그 플래그를
# 조건으로 걸었다면 위 27.0 이 아니라 None 이 나왔을 것이다.
r.ok(
    device.supports_feature("outsideTemperatureDisplayUse") is False
    and device.outside_temperature == 27.0,
    "feature 플래그가 꺼져 있어도 값이 오면 읽는다",
)
r.ok(
    "powerCtrl" in source("boiler.py"),
    "근거 없는 플래그로 엔티티를 막았던 전례를 근거로 적었다",
)
device.apply_status({"outsideTemperature": None}, now=110.0)
r.ok(device.outside_temperature is None, "값이 없으면 추측하지 않는다")
device.apply_status({"outsideTemperature": 270}, now=120.0)


r.section("온수 사용 감지")

r.ok(device.hot_water_running is False, "DHWUse 1 은 사용 안 함이다")
r.ok(device.hot_water_sustained is False, "DHWUseSustained 도 같은 인코딩이다")
device.apply_status({"DHWUse": 2, "DHWUseSustained": 2}, now=130.0)
r.ok(device.hot_water_running is True, "2 는 사용 중이다")
r.ok(device.hot_water_sustained is True, "연속 사용도 2 가 켜짐이다")
device.apply_status({"DHWUse": 7}, now=140.0)
r.ok(device.hot_water_running is None, "모르는 값은 켜짐으로 추측하지 않는다")
device.apply_status({"DHWUse": 1}, now=150.0)


r.section("유량과 신호")

r.ok(device.hot_water_flow_rate == 3.2, "온수 유량은 0.1 단위다")
r.ok(device.heating_flow_rate == 2.2, "난방 유량도 0.1 단위다")
r.ok(device.wifi_rssi == 54, "Wi-Fi 신호는 원시 정수 그대로다")
r.ok("단위를 확인하지 못했다" in source("boiler.py"), "단위를 모른다고 적었다")
r.ok("L/min" in source("sensor.py"), "유량 센서에 분당 리터를 쓴다")


r.section("고장 비트")

r.ok(device.fault_status == (0, 0), "두 값을 함께 읽는다")
device.apply_status({"faultStatus1": 4}, now=160.0)
r.ok(device.fault_status == (4, 0), "한쪽만 와도 나머지를 0 으로 채운다")
device.apply_status({"faultStatus1": 0}, now=170.0)
empty = make_boiler()
r.ok(empty.fault_status is None, "둘 다 없으면 판단하지 않는다")


r.section("난방 강도는 읽기만 한다")

r.ok(device.heating_intensity == 3, "원시 단계 값을 그대로 준다")
intensity_source = source("sensor.py").split("class BoilerHeatingIntensitySensor")[1]
r.ok("이름을 붙이지 않는다" in intensity_source, "단계 이름을 붙이지 않는 이유를 적었다")
r.ok(
    "async_set" not in intensity_source.split("class ")[0],
    "제어 경로를 만들지 않는다",
)
r.ok(
    "heatingIntensity" not in source("switch.py")
    and "heatingIntensity" not in source("number.py")
    and "heatingIntensity" not in source("select.py"),
    "난방 강도를 제어 엔티티로 만들지 않는다",
)


r.section("예약은 읽기만 한다")

r.ok(device.repeat_reservation_interval == (1, 10), "반복 예약 주기를 시·분으로 읽는다")
r.ok(device.day_cycle_reservation == "0" * 24, "24시간 예약 원문을 그대로 남긴다")
r.ok(device.reservation_enabled("programReservationUse") is False, "예약 꺼짐을 읽는다")
device.apply_status({"programReservationUse": 2}, now=180.0)
r.ok(device.reservation_enabled("programReservationUse") is True, "예약 켜짐을 읽는다")
r.ok(
    device.reservation_enabled("없는키") is None,
    "값이 없으면 예약 상태를 추측하지 않는다",
)
reservation_source = source("sensor.py").split("class BoilerReservationSensor")[1]
r.ok("바꾸지 않는다" in reservation_source, "예약을 쓰지 않는 이유를 적었다")
r.ok("해석하지 않고" in source("boiler.py"), "시간표 각 자리를 해석하지 않는다고 적었다")


r.section("모르는 명령 코드를 모은다")

fresh = make_boiler()
fresh.apply_status({"command": 33554438}, now=200.0)
fresh.apply_status({"command": 33554438}, now=210.0)
fresh.apply_status({"command": 33554434}, now=220.0)
r.ok(
    fresh.observed_commands == {33554438: 2, 33554434: 1},
    "본 적 있는 명령 코드를 횟수와 함께 모은다",
)
r.ok(
    "observed_commands" in source("diagnostics.py"),
    "진단에 남겨 앱 조작으로 코드를 알아낼 수 있게 한다",
)


r.section("새 엔티티가 실제로 만들어진다")

sensor_setup = source("sensor.py").split("async_add_entities(entities)")[0]
for name in (
    "outside_temperature",
    "BoilerFlowRateSensor",
    "BoilerWifiSignalSensor",
    "BoilerHeatingIntensitySensor",
    "BoilerReservationSensor",
):
    r.ok(name in sensor_setup, f"{name} 를 등록한다")

binary_setup = source("binary_sensor.py").split("async_add_entities(entities)")[0]
r.ok("BoilerHotWaterRunning" in binary_setup, "온수 사용 중을 등록한다")
r.ok("BoilerFaultProblem" in binary_setup, "고장 상태를 등록한다")


sys.exit(r.finish())

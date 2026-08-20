"""보일러 상태와 NR-67D 설정온도 명령을 검증한다."""

from __future__ import annotations

import json
import sys
from datetime import date

from harness import Report, source
from navien_smarthome.boiler import (
    BoilerDevice,
    extract_boiler_status,
    observe_boiler_message,
    sanitize_boiler_value,
)
from navien_smarthome.const import (
    SERVICE_BOILER,
    SUPPORTED_SERVICE_CODES,
    TOPIC_PREFIX,
)
from navien_smarthome.diagnostics import TO_REDACT, _identifiers, _scrub

r = Report()


r.section("상태 센서와 제한된 제어")

r.ok(TOPIC_PREFIX[SERVICE_BOILER] == "smarttok", "앱과 같은 관찰 토픽을 구독한다")
r.ok(SERVICE_BOILER in SUPPORTED_SERVICE_CODES, "보일러를 지원 기기로 분류한다")
r.ok("async_boiler_request" in source("api.py"), "보일러 전용 봉투만 중계한다")
r.ok("boiler.build_start_payload" in source("coordinator.py"), "MQTT 연결 뒤 초기 상태를 요청한다")
r.ok(
    "_schedule_boiler_readback(boiler)" in source("coordinator.py"),
    "초기 요청 뒤 일반 상태 조회도 한 번 보낸다",
)
r.ok(
    "전원·빠른온수·터보온수·설정온도 제어" in source("../../README.md"),
    "README 에 지원 범위를 적었다",
)
r.ok("`대기`, `히팅`" in source("../../README.md"), "README 에 상태 센서를 적었다")
r.ok("히팅 여부와 관계없이" in source("../../README.md"), "README 에 제어 조건을 적었다")


r.section("실측 상태 봉투")

status = {
    "operationMode": 6,
    "operationBusy": 2,
    "errorCode": 0,
    "subErrorCode": 0,
    "insideTemperature": 60,
    "actualInsideTemperature": 303,
    "supplyTemperature": 67,
    "returnTemperature": 64,
    "ondolTemperatureSetting": 60,
    "hotWaterTemperature": 62,
    "hotWaterTemperatureSetting": 86,
    "insideHumidity": 585,
    "fastDHWUse": 1,
    "smartFastDHW": 1,
    "DHWBoost": 2,
}
envelope = {
    "topic": "private-response-topic",
    "payload": {
        "response": {
            "macAddress": "001122334455",
            "status": status,
        }
    },
}
stats = {}
parsed = extract_boiler_status(json.dumps(envelope).encode(), stats)
r.ok(parsed == ("001122334455", status), "status 봉투와 물리 기기 ID를 꺼낸다")
r.ok(stats == {"accepted": 1}, "받은 상태를 집계한다")

ignored_stats = {}
r.ok(
    extract_boiler_status(b'{"payload":{"response":{"feature":{}}}}', ignored_stats)
    is None,
    "DID 응답은 상태로 쓰지 않는다",
)
r.ok(ignored_stats == {"dropped_no_status": 1}, "상태 아닌 응답을 집계한다")

raw_device = {
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
                "feature": {
                    "ondolTemperatureMin": 60,
                    "ondolTemperatureMax": 130,
                    "ondolUse": 2,
                    "hotWaterTemperatureMin": 60,
                    "hotWaterTemperatureMax": 120,
                    "hotWaterTemperatureSettingUse": 2,
                    "powerUse": 2,
                    "fastDHWUse": 2,
                    "smartFastDHWUse": 2,
                    "DHWBoostUse": 2,
                    "gasUsageUse": 2,
                },
            }
        },
    },
}
device = BoilerDevice.parse(raw_device)
r.ok(device is not None, "REST 메타데이터로 보일러를 만든다")
assert device is not None
r.ok(device.nickname == "보일러", "문자열 별칭을 기기명으로 쓴다")
dict_nick_device = BoilerDevice.parse(
    {
        **raw_device,
        "Properties": {
            **raw_device["Properties"],
            "nickName": {"mainItem": "보일러"},
        },
    }
)
r.ok(
    dict_nick_device is not None and dict_nick_device.nickname == "보일러",
    "mainItem 형태 별칭을 기기명으로 쓴다",
)
device.apply_status(status)
r.ok(device.indoor_temperature == 30.3, "정밀 실내온도는 0.1℃ 단위다")
r.ok(device.supply_temperature == 33.5, "난방수 온도는 0.5℃ 단위다")
r.ok(device.return_temperature == 32.0, "환수 온도는 0.5℃ 단위다")
r.ok(device.ondol_target_temperature == 30.0, "온돌 설정은 0.5℃ 단위다")
r.ok(device.hot_water_temperature == 31.0, "온수 현재값은 0.5℃ 단위다")
r.ok(device.hot_water_target_temperature == 43.0, "온수 설정값은 0.5℃ 단위다")
r.ok(device.indoor_humidity == 58.5, "실내 습도는 0.1% 단위다")
r.ok(device.operation_mode == 6, "원시 모드 코드를 보존한다")
r.ok(device.operation_mode_name == "온돌 난방", "operationMode=6은 온돌 난방 모드다")
r.ok(device.operation_busy == 2 and not device.heating_is_idle, "실측 히팅 값 2를 보존한다")
r.ok(device.operating_state == "히팅", "전원이 켜지고 operationBusy=2면 히팅이다")
device.apply_status({"operationBusy": 1})
r.ok(device.heating_is_idle, "실측 대기 값 1을 보존한다")
r.ok(device.operating_state == "대기", "전원이 켜지고 operationBusy=1이면 대기다")
device.apply_status({"operationBusy": 3})
r.ok(device.operating_state is None, "모르는 operationBusy 값은 히팅으로 추측하지 않는다")
device.apply_status({"operationMode": 1})
r.ok(device.operating_state == "꺼짐", "operationMode=1이면 꺼짐이다")
r.ok(device.operation_mode_name == "꺼짐", "operationMode=1의 모드 이름도 꺼짐이다")
device.apply_status({"operationMode": 99})
r.ok(device.operation_mode_name is None, "모르는 운전 모드는 이름을 추측하지 않는다")
device.apply_status({"operationMode": 6, "operationBusy": 2})
r.ok(device.status_age is not None, "MQTT 상태를 받은 시각을 진단 정보로 남긴다")
r.ok(device.error_code == 0 and device.available, "상태를 받은 연결 기기는 사용 가능하다")
r.ok(device.switch_state("power"), "operationMode가 꺼짐이 아니면 전원 켜짐이다")
r.ok(device.switch_state("fast_dhw") is False, "빠른온수 1은 꺼짐이다")
r.ok(device.switch_state("smart_fast_dhw") is False, "스마트운전 1은 꺼짐이다")
r.ok(device.switch_state("dhw_boost") is True, "터보온수 2는 켜짐이다")

gas_meter = {
    "thisYearMonthTotalGasUsage": 142,
    "thisYearMonthTotalHeatGasUsage": 99,
    "thisYearMonthTotalHotWaterGasUsage": 43,
    "gasMeterThisMonth": [
        {
            "year": 2026,
            "month": 8,
            "day": 20,
            "gasMeter": 3,
            "heatGasMeter": 1,
            "hotWaterGasMeter": 2,
        }
    ],
}
gas_envelope = {
    "payload": {
        "response": {
            "macAddress": "001122334455",
            "gasMeter": gas_meter,
        }
    }
}
gas_update = extract_boiler_status(json.dumps(gas_envelope).encode())
r.ok(gas_update is not None, "가스 사용량 응답도 보일러에 붙인다")
assert gas_update is not None
device.apply_status(gas_update[1], now=700.0)
r.ok(device.gas_total_month == 14.2, "월 가스 원시값을 앱처럼 10으로 나눈다")
r.ok(device.gas_heating_month == 9.9, "월 난방 가스 사용량을 푼다")
r.ok(device.gas_hot_water_month == 4.3, "월 온수 가스 사용량을 푼다")
r.ok(device.gas_day(date(2026, 8, 20)) == (0.3, 0.1, 0.2), "오늘 일간 가스 3종을 푼다")
r.ok(device.gas_day(date(2026, 8, 19)) == (0.0, 0.0, 0.0), "같은 달의 빠진 날짜는 0이다")
r.ok(device.gas_day(date(2026, 7, 31)) is None, "다른 달의 오래된 배열을 오늘 값으로 쓰지 않는다")
r.ok(device.operation_mode == 6, "가스 응답이 기존 운전 상태를 지우지 않는다")


r.section("5분 무통신 상태 확인")

device.apply_status({"operationBusy": 1}, now=100.0)
r.ok(device.communication_age(now=220.0) == 120.0, "수신 뒤 흐른 시간을 계산한다")
r.ok(device.silence_refresh_delay(now=220.0) == 180.0, "5분에서 통신 경과 시간을 뺀다")
device.note_communication(now=250.0)
r.ok(device.communication_age(now=260.0) == 10.0, "HA가 보낸 요청도 통신 시각을 갱신한다")
r.ok(device.silence_refresh_delay(now=600.0) == 1.0, "이미 5분이 지났어도 즉시 반복하지 않는다")
r.ok(
    "_schedule_boiler_silence_check(device)" in source("coordinator.py")
    and "BOILER_SILENCE_REFRESH_SECONDS" in source("coordinator.py"),
    "수신할 때마다 5분 타이머를 다시 잡는다",
)
r.ok(
    "boiler.last_communication_at = old.last_communication_at" in source("coordinator.py"),
    "5분 REST 갱신이 마지막 통신 시각을 지우지 않는다",
)
r.ok(
    "not target.connected or not self.mqtt_connected" in source("coordinator.py"),
    "연결이 끊겼을 때 상태 요청을 보내지 않는다",
)


r.section("NR-67D 설정온도 봉투")

r.ok(device.temperature_bounds("hot_water") == (30.0, 60.0), "온수 범위는 서버값을 0.5℃로 푼다")
r.ok(device.temperature_bounds("ondol") == (30.0, 65.0), "난방수 범위는 서버값을 0.5℃로 푼다")

status_payload = device.build_status_payload("mqtt-client", now_ms=123456)
r.ok(status_payload["protocolVersion"] == 1, "상태 요청 프로토콜 버전은 1이다")
r.ok(
    status_payload["requestTopic"] == "cmd/20/roomcon-001122334455/status",
    "상태 요청 토픽은 앱과 같다",
)
r.ok(
    status_payload["responseTopic"]
    == "cmd/20/private-topic-key/mobile-mqtt-client/res",
    "응답 토픽은 앱 MQTT 클라이언트에 묶는다",
)
r.ok(
    status_payload["request"]
    == {
        "macAddress": "001122334455",
        "registerAt": "123456",
        "additionalValue": "6272",
        "param": [],
        "paramStr": "",
        "command": 16777219,
        "deviceType": 20,
    },
    "상태 요청 내부 봉투를 앱과 같게 만든다",
)

start_payload = device.build_start_payload("mqtt-client", now_ms=123456)
r.ok(start_payload["requestTopic"].endswith("/status/start"), "최초 상태 요청 토픽")
r.ok(start_payload["responseTopic"].endswith("/res/start"), "최초 상태 응답 토픽")
r.ok(start_payload["request"]["command"] == 16777217, "최초 상태 요청 명령 코드")

gas_payload = device.build_gas_payload("mqtt-client", now_ms=123456)
r.ok(gas_payload["requestTopic"].endswith("/status/gas-meter-query"), "가스 조회 요청 토픽")
r.ok(gas_payload["responseTopic"].endswith("/res/gas-meter"), "가스 조회 응답 토픽")
r.ok(gas_payload["request"]["command"] == 16777224, "가스 조회 명령 코드")

hot_water = device.build_temperature_payload(
    "hot_water", 43, "mqtt-client", now_ms=123456
)
r.ok(hot_water["request"]["mode"] == "hotwater-temperature", "온수 모드 문자열")
r.ok(hot_water["request"]["command"] == 33554443, "온수 명령 코드")
r.ok(hot_water["request"]["param"] == [86.0], "NR-67D는 섭씨×2 원시값을 보낸다")
r.ok(hot_water["request"]["roomUseSetting"] == "10000000", "온수 비트마스크")

hot_water_half = device.build_temperature_payload(
    "hot_water", 43.5, "mqtt-client", now_ms=123456
)
r.ok(hot_water_half["request"]["param"] == [87.0], "0.5℃ 단계도 원시 정수로 보낸다")

ondol = device.build_temperature_payload("ondol", 50, "mqtt-client", now_ms=123456)
r.ok(ondol["request"]["mode"] == "ondol-heat", "난방수 모드 문자열")
r.ok(ondol["request"]["command"] == 33554438, "난방수 명령 코드")
r.ok(ondol["request"]["param"] == [100.0], "난방수도 섭씨×2 원시값을 보낸다")
r.ok(ondol["request"]["roomUseSetting"] == "11111111", "난방수 비트마스크")

try:
    device.build_temperature_payload("hot_water", 43.25, "mqtt-client")
except ValueError:
    half_step_rejected = True
else:
    half_step_rejected = False
r.ok(half_step_rejected, "modelCode=20 앱에 없는 0.25℃ 명령은 막는다")

try:
    device.build_temperature_payload("ondol", 66, "mqtt-client")
except ValueError:
    outside_rejected = True
else:
    outside_rejected = False
r.ok(outside_rejected, "서버 범위 밖 명령은 막는다")
r.ok("_attr_native_step = 0.5" in source("number.py"), "number 슬라이더도 0.5℃ 단위다")
r.ok(
    "async_refresh_boiler_status(device)" not in source("coordinator.py")
    and "not current.heating_is_idle" not in source("coordinator.py"),
    "히팅 상태 사전 차단 없이 온도 명령을 보낸다",
)


r.section("NR-67D 앱 기능 스위치 봉투")

power_on = device.build_power_payload(True, "mqtt-client", now_ms=123456)
r.ok(power_on["request"]["mode"] == "power-on", "전원 켜기 모드 문자열")
r.ok(power_on["request"]["command"] == 33554434, "전원 켜기 명령 코드")
r.ok(power_on["request"]["param"] == [], "전원 명령은 값 배열이 비어 있다")
r.ok(power_on["request"]["roomUseSetting"] == "11111111", "전원은 전체 룸콘 대상이다")

power_off = device.build_power_payload(False, "mqtt-client", now_ms=123456)
r.ok(power_off["request"]["mode"] == "power-off", "전원 끄기 모드 문자열")
r.ok(power_off["request"]["command"] == 33554433, "전원 끄기 명령 코드")

for kind, mode, command in (
    ("fast_dhw", "fastDHW", 33554444),
    ("smart_fast_dhw", "smartFastDHW", 33554456),
    ("dhw_boost", "DHWBoost", 33554460),
):
    enabled = device.build_switch_payload(kind, True, "mqtt-client", now_ms=123456)
    disabled = device.build_switch_payload(kind, False, "mqtt-client", now_ms=123456)
    r.ok(enabled["request"]["mode"] == mode, f"{kind} 앱 모드 문자열")
    r.ok(enabled["request"]["command"] == command, f"{kind} 명령 코드")
    r.ok(enabled["request"]["param"] == [2], f"{kind} 켜기는 값 2")
    r.ok(disabled["request"]["param"] == [1], f"{kind} 끄기는 값 1")
    r.ok(enabled["request"]["roomUseSetting"] == "10000000", f"{kind} 온수 비트마스크")

r.ok("async_boiler_power" in source("coordinator.py"), "전원 제어 경로를 코디네이터에 둔다")
r.ok("async_boiler_switch" in source("coordinator.py"), "온수 기능 제어 경로를 코디네이터에 둔다")
r.ok("BoilerMonthlyGasSensor" in source("sensor.py"), "월간 가스 센서를 만든다")
r.ok("BoilerDailyGasSensor" in source("sensor.py"), "오늘 가스 센서를 만든다")
r.ok("async_track_time_change" in source("sensor.py"), "자정에 날짜 기준을 바꾼다")
r.ok("BOILER_GAS_REFRESH_SECONDS" in source("coordinator.py"), "가스 사용량은 저빈도로 갱신한다")


r.section("보일러 진단 식별정보")

for key in (
    "clientID",
    "sessionID",
    "macAddress",
    "requestTopic",
    "responseTopic",
    "boilerControllerSerialNumber",
):
    r.ok(key in TO_REDACT, f"{key} 키를 가린다")

sensitive = {
    "clientID": "client-secret-123",
    "macAddress": "001122334455",
    "requestTopic": "cmd/20/roomcon-001122334455/status/start",
}
scrubbed = repr(_scrub(sensitive, _identifiers([sensitive])))
r.ok("client-secret" not in scrubbed, "clientID 값이 진단 어디에도 남지 않는다")
r.ok("001122334455" not in scrubbed, "MAC 값이 토픽 안에도 남지 않는다")


r.section("JSON 구조만 남기고 식별값은 지운다")

raw = {
    "payload": {
        "deviceId": "DEVICE-SECRET-1234",
        "clientID": "CLIENT-SECRET-1234",
        "requestTopic": "cmd/20/roomcon-secret/status/start",
        "insideTemperature": "23.5",
        "mode": 2,
        "epoch": 1_787_210_786,
        "label": "우리집 보일러",
        "zones": [1, 2, 3],
    },
    "ABCDEF1234567890": {"value": 7},
}
observation = observe_boiler_message(
    json.dumps(raw, ensure_ascii=False).encode(),
    "12345/smarttok/DEVICE-SECRET-1234/status",
)
text = repr(observation)

r.ok(observation["encoding"] == "json", "JSON 봉투를 구분한다")
r.ok(observation["topic_suffix_depth"] == 2, "원문 없이 토픽 깊이만 남긴다")
r.ok(
    observation["shape"]["payload"]["insideTemperature"] == 23.5,
    "짧은 수치는 남긴다",
)
r.ok(observation["shape"]["payload"]["mode"] == 2, "작은 정수 상태도 남긴다")
r.ok(observation["shape"]["payload"]["epoch"] == {"kind": "number"}, "큰 숫자는 가린다")
r.ok("DEVICE-SECRET" not in text, "deviceId 값이 남지 않는다")
r.ok("CLIENT-SECRET" not in text, "clientID 값이 남지 않는다")
r.ok("roomcon-secret" not in text, "토픽 문자열이 남지 않는다")
r.ok("우리집" not in text, "일반 문자열도 원문을 남기지 않는다")
r.ok("ABCDEF1234567890" not in text, "식별자 모양의 dict 키도 가린다")


r.section("바이너리와 크기 제한")

binary = observe_boiler_message(b"\x00\xffDEVICE-SECRET", "1/smarttok/secret")
r.ok(binary["encoding"] == "binary_or_text", "JSON 이 아니면 종류만 남긴다")
r.ok("DEVICE-SECRET" not in repr(binary), "바이너리 원문은 남기지 않는다")

oversize = observe_boiler_message(b"[" + b"0," * 40_000 + b"0]", "1/smarttok/x")
r.ok(oversize["encoding"] == "oversize", "큰 JSON 은 파싱하지 않는다")
r.ok("shape" not in oversize, "큰 JSON 내용을 메모리에 다시 펼치지 않는다")

many = sanitize_boiler_value(list(range(40)))
r.ok(len(many) == 33, "목록은 32개와 잘림 표지만 남긴다")
r.ok(many[-1] == {"kind": "truncated_items", "count": 8}, "잘린 개수를 알린다")


r.section("근거를 코드에 남겼다")

boiler_source = source("boiler.py")
r.ok(
    "modelCode=20" in boiler_source and "operationMode=1" in boiler_source,
    "모델과 상태 매핑 근거를 적었다",
)
r.ok(
    "공식\n        NR-67D 설명서" in boiler_source and "불꽃 표시" in boiler_source,
    "설명서의 선택 모드·실제 가동 구분을 적었다",
)
r.ok("바이너리 원문" in boiler_source, "바이너리를 보관하지 않는 이유를 적었다")


sys.exit(r.finish())

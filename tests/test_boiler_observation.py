"""보일러는 제어 없이 확인된 MQTT 상태만 읽는다."""

from __future__ import annotations

import json
import sys

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


r.section("읽기 전용 지원")

r.ok(TOPIC_PREFIX[SERVICE_BOILER] == "smarttok", "앱과 같은 관찰 토픽을 구독한다")
r.ok(SERVICE_BOILER in SUPPORTED_SERVICE_CODES, "보일러를 지원 기기로 분류한다")
r.ok("async_boiler" not in source("api.py"), "보일러 제어 API 를 만들지 않았다")
r.ok("읽기 전용 센서가 됩니다" in source("../../README.md"), "README 에 지원 범위를 적었다")
r.ok("보일러 제어는 만들지 않았습니다" in source("../../README.md"), "README 에 제어 제외를 적었다")


r.section("실측 상태 봉투")

status = {
    "operationMode": 6,
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
    "deviceId": "cloud-device-id",
    "deviceSeq": 14,
    "modelCode": "20",
    "modelName": "NR-67D",
    "connected": 1,
    "Properties": {
        "nickName": "보일러",
        "did": {
            "response": {
                "macAddress": "001122334455",
                "feature": {
                    "ondolTemperatureMin": 60,
                    "ondolTemperatureMax": 130,
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
r.ok(device.operation_mode == 6, "뜻을 추측하지 않고 모드 코드를 보존한다")
r.ok(device.error_code == 0 and device.available, "상태를 받은 연결 기기는 사용 가능하다")


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
    "확인된 상태만" in boiler_source and "명령은 만들지 않는다" in boiler_source,
    "읽기 전용 안전 경계를 적었다",
)
r.ok("바이너리 원문" in boiler_source, "바이너리를 보관하지 않는 이유를 적었다")


sys.exit(r.finish())

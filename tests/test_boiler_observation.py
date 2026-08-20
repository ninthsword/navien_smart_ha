"""보일러 1단계는 지원이 아니라 개인정보 없는 수동 조작 없는 관찰이다."""

from __future__ import annotations

import json
import sys

from harness import Report, source
from navien_smarthome.boiler import observe_boiler_message, sanitize_boiler_value
from navien_smarthome.const import (
    SERVICE_BOILER,
    SUPPORTED_SERVICE_CODES,
    TOPIC_PREFIX,
)

r = Report()


r.section("지원으로 열지 않는다")

r.ok(TOPIC_PREFIX[SERVICE_BOILER] == "smarttok", "앱과 같은 관찰 토픽을 구독한다")
r.ok(SERVICE_BOILER not in SUPPORTED_SERVICE_CODES, "지원 기기 목록에는 넣지 않는다")
r.ok("async_boiler" not in source("api.py"), "보일러 제어 API 를 만들지 않았다")


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
    "엔티티도 명령도 만들지 않는다" in boiler_source,
    "관찰 단계의 안전 경계를 적었다",
)
r.ok("바이너리 원문" in boiler_source, "바이너리를 보관하지 않는 이유를 적었다")


sys.exit(r.finish())

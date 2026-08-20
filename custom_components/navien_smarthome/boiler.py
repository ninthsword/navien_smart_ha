"""보일러 MQTT 를 해석하기 전의 **수동 조작 없는 관찰 단계**.

보일러는 매트와 상태 모델이 다르고, 컨트롤러 종류별 인코딩도 다르다. 특히
온도값을 짐작해 제어하면 실제 난방·온수 설정을 바꿀 수 있으므로 패킷 구조가
확인되기 전에는 엔티티도 명령도 만들지 않는다.

여기서는 앱과 같은 ``smarttok`` 구독에서 들어온 메시지의 **모양만** 남긴다.
진단 파일이 공개 이슈에 첨부될 수 있으므로 원문 문자열·토픽·큰 숫자·바이너리는
보관하지 않는다. 작은 수치와 키 구조만으로 상태 봉투를 가른 뒤, 실제 필드의 뜻은
실기기 관찰과 앱 코드가 서로 맞을 때 별도 단계에서 연다.
"""

from __future__ import annotations

import json
import re
from typing import Any

BOILER_TOPIC_PREFIX = "smarttok"
BOILER_OBSERVATION_KEEP = 8

_MAX_DEPTH = 8
_MAX_DICT_ITEMS = 64
_MAX_LIST_ITEMS = 32
_MAX_JSON_BYTES = 64 * 1024
_MAX_SAFE_NUMBER = 10_000
_SAFE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
_INTEGER_TEXT = re.compile(r"-?\d{1,6}")
_FLOAT_TEXT = re.compile(r"-?\d{1,4}\.\d{1,3}")
_IDENTIFIER_KEY = re.compile(r"(?:[0-9a-fA-F]{12,}|\d{10,})")

# 키 이름은 구조를 파악하는 데 필요하므로 남기되, 그 아래 값은 종류조차 드러내지
# 않는다. 대소문자와 ``-``/``_`` 차이를 없애 새 변형도 같은 규칙에 걸리게 한다.
_SENSITIVE_KEYS = {
    "accesstoken",
    "authorization",
    "boilercontrollerserialnumber",
    "clientid",
    "deviceid",
    "mac",
    "macaddress",
    "mqtttopickey",
    "password",
    "refreshtoken",
    "requesttopic",
    "responsetopic",
    "sessionid",
    "ssid",
    "token",
    "userid",
}


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _safe_key(key: Any, index: int) -> str:
    """필드명처럼 보이는 키만 남긴다. 식별자가 dict 키인 경우도 가린다."""
    text = str(key)
    if not _SAFE_KEY.fullmatch(text) or _IDENTIFIER_KEY.search(text):
        return f"<key:{index}>"
    return text


def _safe_numeric_text(value: str) -> int | float | None:
    """프로토콜 값으로 쓸 만한 짧은 숫자 문자열만 수치로 남긴다."""
    text = value.strip()
    try:
        if _INTEGER_TEXT.fullmatch(text):
            number = int(text)
        elif _FLOAT_TEXT.fullmatch(text):
            number = float(text)
        else:
            return None
    except ValueError:
        return None
    return number if abs(number) <= _MAX_SAFE_NUMBER else None


def sanitize_boiler_value(value: Any, depth: int = 0) -> Any:
    """공개 진단에 넣어도 되는 구조·작은 수치만 재귀적으로 남긴다."""
    if depth >= _MAX_DEPTH:
        return {"kind": "truncated"}

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, inner) in enumerate(value.items()):
            if index >= _MAX_DICT_ITEMS:
                break
            key = _safe_key(raw_key, index)
            if _normalized_key(str(raw_key)) in _SENSITIVE_KEYS:
                result[key] = {"kind": "redacted"}
            else:
                result[key] = sanitize_boiler_value(inner, depth + 1)
        if len(value) > _MAX_DICT_ITEMS:
            result["<truncated_keys>"] = len(value) - _MAX_DICT_ITEMS
        return result

    if isinstance(value, list):
        result = [
            sanitize_boiler_value(inner, depth + 1)
            for inner in value[:_MAX_LIST_ITEMS]
        ]
        if len(value) > _MAX_LIST_ITEMS:
            result.append(
                {"kind": "truncated_items", "count": len(value) - _MAX_LIST_ITEMS}
            )
        return result

    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        if abs(value) <= _MAX_SAFE_NUMBER:
            return value
        return {"kind": "number"}
    if isinstance(value, str):
        if (number := _safe_numeric_text(value)) is not None:
            return number
        return {"kind": "string", "length": len(value)}

    # JSON 밖의 값이 들어와도 repr 로 원문을 남기지 않는다.
    return {"kind": type(value).__name__}


def observe_boiler_message(payload: bytes, topic: str) -> dict[str, Any]:
    """한 MQTT 메시지를 개인정보 없는 관찰 레코드로 바꾼다.

    JSON 이 아니면 길이만 남긴다. 바이너리 원문이나 앞부분조차 기기 식별자를
    품을 수 있으므로 hex/base64 로 보관하지 않는다.
    """
    parts = topic.split("/")
    try:
        prefix_index = parts.index(BOILER_TOPIC_PREFIX)
    except ValueError:
        suffix_depth = None
    else:
        suffix_depth = len(parts) - prefix_index - 1

    observation: dict[str, Any] = {
        "payload_bytes": len(payload),
        "topic_suffix_depth": suffix_depth,
    }
    # MQTT 프레임은 이미 메모리에 있지만, 거대한 JSON 을 다시 트리로 펼쳐 이벤트
    # 루프를 오래 잡는 일은 막는다. 크기와 인코딩 종류만으로 진단에는 충분하다.
    if len(payload) > _MAX_JSON_BYTES:
        observation["encoding"] = "oversize"
        return observation
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        observation["encoding"] = "binary_or_text"
        return observation

    observation["encoding"] = "json"
    observation["shape"] = sanitize_boiler_value(event)
    return observation

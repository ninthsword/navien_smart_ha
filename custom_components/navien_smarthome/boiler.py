"""보일러 MQTT 상태와 NR-67D 온도 제어 프로토콜.

보일러는 매트와 상태 모델이 다르고, 컨트롤러 종류별 인코딩도 다르다. 특히
온도값을 짐작해 제어하면 실제 난방·온수 설정을 바꿀 수 있으므로, 현재 앱에서
확인한 ``modelCode=20`` 의 온수·난방수 설정만 연다.

여기서는 앱과 같은 ``smarttok`` 구독에서 들어온 메시지의 **모양만** 남긴다.
진단 파일이 공개 이슈에 첨부될 수 있으므로 원문 문자열·토픽·큰 숫자·바이너리는
보관하지 않는다. 작은 수치와 키 구조만으로 상태 봉투를 가른 뒤, 실제 필드의 뜻은
실기기 관찰과 앱 코드가 서로 맞을 때 별도 단계에서 연다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

BOILER_TOPIC_PREFIX = "smarttok"
BOILER_OBSERVATION_KEEP = 8
BOILER_MODEL_MGPP = "20"
BOILER_OPERATION_OFF = 1
BOILER_BUSY_IDLE = 1
BOILER_BUSY_HEATING = 2
BOILER_READBACK_DELAY_SECONDS = 3
BOILER_SILENCE_REFRESH_SECONDS = 300
BOILER_GAS_REFRESH_SECONDS = 3600
BOILER_GAS_METER_UPDATE = "__gas_meter__"

BOILER_STATE_OFF = "꺼짐"
BOILER_STATE_IDLE = "대기"
# 공식 NCB753 설명서가 이 상태를 「연소」로 부른다. 처음에 쓰던 「히팅」은
# 설명서에도 앱에도 없는 말이었다.
BOILER_STATE_HEATING = "연소"

# Navien Smart 2.10.4의 NR-67D(modelCode=20) 탭 선택 분기에서 확인했다.
# 이 값은 현재 연소 여부가 아니라 룸콘에서 선택한 운전 모드다. 실제 가동 여부는
# 별도 ``operationBusy`` 값으로 판단한다.
BOILER_OPERATION_MODE_NAMES: dict[int, str] = {
    1: "꺼짐",
    4: "외출",
    5: "실내 난방",
    6: "온돌 난방",
    7: "반복 예약",
    8: "24시간 예약",
    10: "온수 전용",
}

# 실기기에서 확인한 운전모드 명령. **추측이 아니라 관측이다** — 룸콘은 마지막으로
# 처리한 명령 코드를 상태의 ``command`` 로 되돌려주므로, 앱에서 그 버튼을 누르면
# 코드가 드러난다.
#
#   0x2000001 = 33554433  전원 끄기      (mode 1 꺼짐)
#   0x2000004 = 33554436  외출           (mode 4 외출)
#   0x2000006 = 33554438  온돌 난방 온도  (mode 6 온돌 난방)
#
# 하위 자리가 운전모드 값과 맞는다. 다만 **아직 명령을 열지 않는다.** 이 기기에서
# 외출 명령은 룸콘이 7번 되돌려줬는데도 ``operationMode`` 가 6 에서 바뀌지 않았다.
# 기기가 받기만 하고 실행하지 않는 명령을 통합이 보내면, 사용자는 눌렀는데 아무
# 일도 안 일어나는 스위치를 갖게 된다. 어떤 기기가 실제로 실행하는지 확인한 뒤에
# 연다 — `feature` 의 지원 플래그와 함께 봐야 한다(아래).
#
# **동작이 확인된 기능은 예외 없이 `feature` 값이 2 다.** powerUse · ondolUse ·
# gasUsageUse · fastDHWUse · smartFastDHWUse · DHWBoostUse ·
# hotWaterTemperatureSettingUse 가 모두 2 이고 전부 실기기에서 동작한다. 반대로
# 이 기기에서 듣지 않는 외출은 ``gooutUse`` 가 1 이다. 앱의 「온수전용·외출」
# 버튼이 통째로 안 먹는 것도 ``hotWaterUse`` 가 1 인 것과 맞는다 — 그 값은
# 「온수 기능」이 아니라 **온수 전용 운전모드** 지원 여부로 읽어야 앞뒤가 맞는다.
# 공식 `NCB753` 사용설명서(2025-01-08판) 「12. 자가 진단 조치 방법」 표를 그대로
# 옮겼다. 추가하거나 짐작한 항목은 없다.
#
# **번호를 어떻게 맞췄는지 밝혀 둔다.** 설명서는 `E001` 처럼 적고 서버는 정수로
# 준다. 이 표는 그 정수를 설명서의 세 자리 번호로 읽는다 — 실기기에서 오류를
# 재현해 확인한 것이 아니라 두 표기를 맞춘 것이다. 그래서 **이름을 못 찾으면
# 비워 두고 숫자만 보여준다.** 틀린 이름을 붙이는 것보다 낫다.
BOILER_ERROR_NAMES: dict[int, str] = {
    1: "열교환기 과열",
    3: "불착화",
    4: "의사 화염",
    12: "실화",
    14: "가스 알람",
    16: "열교환기 과열",
    26: "버너 이상",
    30: "배기가스 온도 이상",
    46: "열교환기 과열 감지기 이상",
    47: "배기가스 온도 센서 이상",
    60: "듀얼벤추리 이상",
    109: "송풍기 회전 수 감지 이상",
    110: "배기폐쇄",
    205: "난방 공급 온도 센서 이상",
    218: "난방 환수 온도 센서 이상",
    228: "배관 누수",
    250: "동결 상태",
    302: "저수위 이상",
    311: "수위 이상",
    324: "난방 공급 라인 이상",
    325: "난방 순환 라인 이상",
    351: "물 보충 이상",
    407: "온수 출구 온도 센서 이상",
    421: "직수 온도 센서 이상",
    441: "온수 출구 온도 센서 이상",
    445: "믹싱밸브 이상",
    515: "컨트롤러 이상",
    517: "Dip 스위치 설정 이상",
    594: "EEPROM 이상",
    615: "입력 및 메모리 이상",
    792: "환탕 라인 순환 이상",
}

BOILER_TEMPERATURE_CONTROLS: dict[str, tuple[str, int, str]] = {
    # Navien Smart 2.10.4 의 modelCode=20 분기. 이 세 값은 한 묶음이다.
    "hot_water": ("hotwater-temperature", 33554443, "10000000"),
    "ondol": ("ondol-heat", 33554438, "11111111"),
}

# Navien Smart 현재 앱의 modelCode=20 제어 호출에서 확인한 단일 값 스위치다.
# 상태와 명령은 1=끔, 2=켬이며, 전체 룸콘(11111111)은 온수 기능을 첫 비트
# (10000000)로 바꿔 보낸다.
BOILER_SWITCH_CONTROLS: dict[str, tuple[str, str, int]] = {
    "fast_dhw": ("fastDHWUse", "fastDHW", 33554444),
    "smart_fast_dhw": ("smartFastDHW", "smartFastDHW", 33554456),
    "dhw_boost": ("DHWBoost", "DHWBoost", 33554460),
}

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


def _dig(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _half_degree(value: Any) -> float | None:
    """보일러의 0.5℃ 단위 값을 섭씨로 바꾼다.

    서버가 알려준 범위가 온돌 60~130 → 30~65℃, 온수 60~120 → 30~60℃로
    정확히 맞고, 실측 설정값 86이 앱의 43℃와 맞는다.
    """
    number = _number(value)
    return None if number is None else number / 2


def _tenth(value: Any) -> float | None:
    """실측 정밀 센서의 0.1 단위 값을 사람이 읽는 값으로 바꾼다."""
    number = _number(value)
    return None if number is None else number / 10


def _bump(stats: dict[str, Any] | None, key: str) -> None:
    if stats is not None:
        stats[key] = int(stats.get(key) or 0) + 1


@dataclass(frozen=True)
class GasUsageBucket:
    """가스 사용량 한 칸. ``start`` 는 그 칸이 시작하는 현지 날짜다."""

    start: date
    monthly: bool
    total: float
    heating: float
    hot_water: float


def _gas_bucket(row: dict[str, Any], *, monthly: bool) -> GasUsageBucket | None:
    """가스 배열의 행 하나를 사용량 칸으로 바꾼다.

    아직 오지 않은 날·달은 세 값이 모두 ``null`` 로 온다(실기기 응답에서 확인).
    그런 행은 사용량 0 이 아니라 **자료 없음**이라 통계로 만들지 않는다.
    """
    year = _integer(row.get("year"))
    month = _integer(row.get("month"))
    if year is None or month is None or not 1 <= month <= 12:
        return None
    day = _integer(row.get("day"))
    if monthly:
        # 월별 배열은 모든 행이 day=0 이다. 일별 행이 섞여 오면 뜻을 모르므로 버린다.
        if day not in (0, None):
            return None
        start = date(year, month, 1)
    else:
        if day is None or not 1 <= day <= 31:
            return None
        try:
            start = date(year, month, day)
        except ValueError:
            return None
    total = _tenth(row.get("gasMeter"))
    if total is None:
        return None
    return GasUsageBucket(
        start=start,
        monthly=monthly,
        total=total,
        heating=_tenth(row.get("heatGasMeter")) or 0.0,
        hot_water=_tenth(row.get("hotWaterGasMeter")) or 0.0,
    )


@dataclass
class BoilerDevice:
    """REST 기기 정보와 MQTT 상태를 합친 보일러."""

    raw: dict[str, Any]
    device_id: str
    device_seq: int
    model_code: str
    model_name: str
    nickname: str
    connected: bool
    physical_device_id: str | None = None
    feature: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    gas_meter: dict[str, Any] = field(default_factory=dict)
    # REST 목록에 든 상태는 서버 캐시일 수 있다. 센서가 실제 MQTT 프레임을 언제
    # 받았는지 구분할 수 있도록 그때에만 이 시각을 채운다.
    status_received_at: float | None = None
    # 수신 상태와 HA가 보낸 요청을 합친 마지막 통신 시각. 보일러가 변화를 스스로
    # 올리는 동안에는 폴링하지 않고, 양방향 통신이 5분간 없을 때만 상태를 묻는다.
    last_communication_at: float | None = None
    gas_received_at: float | None = None
    # 룸콘이 마지막으로 처리한 명령 코드를 상태가 되돌려준다. 아직 뜻을 모르는
    # 명령(운전모드 변경 등)을 추측 없이 알아내는 유일한 길이라, 본 적 있는
    # 코드를 모아 진단에 남긴다. 값은 프로토콜 상수이지 식별정보가 아니다.
    observed_commands: dict[int, int] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> BoilerDevice | None:
        device_id = str(raw.get("deviceId") or "")
        device_seq = _integer(raw.get("deviceSeq"))
        if not device_id or device_seq is None:
            return None

        response = _dig(raw, "Properties", "did", "response")
        response = response if isinstance(response, dict) else {}
        feature = response.get("feature")
        status = response.get("status")
        model_name = str(raw.get("modelName") or "보일러")
        # 앱 응답은 보통 ``{"mainItem": "보일러"}`` 이지만 계정/세대에 따라
        # 문자열 하나로 올 수도 있다. dict 자체를 문자열로 바꾸면 HA 기기명이
        # ``{'mainItem': '보일러'}`` 로 노출되므로 두 형태를 명시적으로 가른다.
        raw_nick = _dig(raw, "Properties", "nickName")
        nick = raw_nick if isinstance(raw_nick, dict) else {}
        nick_text = raw_nick.strip() if isinstance(raw_nick, str) else ""
        nickname = str(nick.get("mainItem") or nick_text or model_name)
        physical_id = response.get("macAddress")
        return cls(
            raw=raw,
            device_id=device_id,
            device_seq=device_seq,
            model_code=str(raw.get("modelCode") or ""),
            model_name=model_name,
            nickname=nickname,
            connected=bool(_integer(raw.get("connected"))),
            physical_device_id=(str(physical_id) if physical_id else None),
            feature=feature if isinstance(feature, dict) else {},
            status=status if isinstance(status, dict) else {},
        )

    def apply_status(self, status: dict[str, Any], *, now: float | None = None) -> None:
        """부분 응답이 와도 전에 알던 필드를 잃지 않는다."""
        update = dict(status)
        gas_meter = update.pop(BOILER_GAS_METER_UPDATE, None)
        stamp = time.monotonic() if now is None else now
        if isinstance(gas_meter, dict):
            self.gas_meter = gas_meter
            self.gas_received_at = stamp
        if update:
            self.status.update(update)
            self.status_received_at = stamp
            if (command := _integer(update.get("command"))) is not None:
                seen = self.observed_commands
                seen[command] = seen.get(command, 0) + 1
        self.last_communication_at = stamp

    def note_communication(self, *, now: float | None = None) -> None:
        """성공적으로 서버에 보낸 보일러 요청도 마지막 통신으로 센다."""
        self.last_communication_at = time.monotonic() if now is None else now

    def communication_age(self, *, now: float | None = None) -> float | None:
        """마지막 보일러 송수신 뒤 흐른 초."""
        if self.last_communication_at is None:
            return None
        stamp = time.monotonic() if now is None else now
        return max(0.0, stamp - self.last_communication_at)

    def silence_refresh_delay(self, *, now: float | None = None) -> float:
        """5분 무통신 상태 요청까지 남은 시간. 1초보다 짧게 재예약하지 않는다."""
        age = self.communication_age(now=now)
        if age is None:
            return float(BOILER_SILENCE_REFRESH_SECONDS)
        return max(1.0, BOILER_SILENCE_REFRESH_SECONDS - age)

    @property
    def available(self) -> bool:
        return self.connected and bool(self.status)

    @property
    def indoor_temperature(self) -> float | None:
        # actualInsideTemperature 는 0.1℃ 정밀값이다. 없는 모델만 0.5℃ 값을 쓴다.
        precise = _tenth(self.status.get("actualInsideTemperature"))
        return precise if precise is not None else _half_degree(
            self.status.get("insideTemperature")
        )

    @property
    def supply_temperature(self) -> float | None:
        return _half_degree(self.status.get("supplyTemperature"))

    @property
    def return_temperature(self) -> float | None:
        return _half_degree(self.status.get("returnTemperature"))

    @property
    def hot_water_temperature(self) -> float | None:
        return _half_degree(self.status.get("hotWaterTemperature"))

    @property
    def ondol_target_temperature(self) -> float | None:
        return _half_degree(self.status.get("ondolTemperatureSetting"))

    @property
    def hot_water_target_temperature(self) -> float | None:
        return _half_degree(self.status.get("hotWaterTemperatureSetting"))

    @property
    def indoor_humidity(self) -> float | None:
        return _tenth(self.status.get("insideHumidity"))

    @property
    def operation_mode(self) -> int | None:
        return _integer(self.status.get("operationMode"))

    @property
    def operation_busy(self) -> int | None:
        return _integer(self.status.get("operationBusy"))

    @property
    def heating_is_idle(self) -> bool:
        """실기기에서 난방수 온도가 오르지 않은 ``operationBusy=1`` 인가."""
        return self.operation_busy == BOILER_BUSY_IDLE

    @property
    def operation_mode_name(self) -> str | None:
        """앱의 NR-67D 탭 이름으로 확인된 운전 모드 이름."""
        mode = self.operation_mode
        return None if mode is None else BOILER_OPERATION_MODE_NAMES.get(mode)

    @property
    def operating_state(self) -> str | None:
        """앱 표시 로직으로 확인한 전원·히팅 상태를 사람이 읽는 값으로.

        Navien Smart 2.10.4의 ``modelCode=20`` 화면은 ``operationMode=1``일
        때 전원 꺼짐으로 처리한다. 실기기에서 ``operationBusy=1``일 때 난방수
        온도가 유지됐고, ``2``로 바뀐 뒤 공급·환수 온도가 함께 상승했다. 공식
        NR-67D 설명서도 선택 운전 모드와 실제 가동 시 켜지는 불꽃 표시를 구분한다.
        이 둘 외 값은 히팅으로 추측하지 않고 ``None``으로 둔다.
        """
        mode = self.operation_mode
        busy = self.operation_busy
        if mode is None:
            return None
        if mode == BOILER_OPERATION_OFF:
            return BOILER_STATE_OFF
        if busy is None:
            return None
        if busy == BOILER_BUSY_IDLE:
            return BOILER_STATE_IDLE
        if busy == BOILER_BUSY_HEATING:
            return BOILER_STATE_HEATING
        return None

    @property
    def status_age(self) -> float | None:
        if self.status_received_at is None:
            return None
        return max(0.0, time.monotonic() - self.status_received_at)

    def temperature_bounds(self, kind: str) -> tuple[float, float] | None:
        """서버가 이 기기에 허용한 설정 범위를 섭씨로 돌려준다."""
        if kind == "hot_water":
            use_key = "hotWaterTemperatureSettingUse"
            low_key = "hotWaterTemperatureMin"
            high_key = "hotWaterTemperatureMax"
        elif kind == "ondol":
            use_key = "ondolUse"
            low_key = "ondolTemperatureMin"
            high_key = "ondolTemperatureMax"
        else:
            return None
        if _integer(self.feature.get(use_key)) != 2:
            return None
        low = _half_degree(self.feature.get(low_key))
        high = _half_degree(self.feature.get(high_key))
        if low is None or high is None or low >= high:
            return None
        return low, high

    def supports_feature(self, key: str) -> bool:
        """서버가 이 보일러에 기능 사용 가능(2)을 선언했는가."""
        return _integer(self.feature.get(key)) == 2

    def switch_state(self, kind: str) -> bool | None:
        """앱과 같은 1=끔, 2=켬 상태를 bool로 바꾼다."""
        if kind == "power":
            mode = self.operation_mode
            return None if mode is None else mode != BOILER_OPERATION_OFF
        try:
            status_key = BOILER_SWITCH_CONTROLS[kind][0]
        except KeyError:
            return None
        value = _integer(self.status.get(status_key))
        return None if value not in (1, 2) else value == 2

    @property
    def outside_temperature(self) -> float | None:
        """외기 온도. **보일러가 잰 값이 아니라 지역 기상 관측값이다.**

        실측 대조로 확인했다. 어느 시점에 이 값이 27.0℃ 였을 때 같은 시각
        기상청 서울(종로구 송월동) 관측이 **27.0℃ 로 정확히 같았고**, 동네
        추정치(응암2동)는 25.8℃ 로 달랐다. 지금까지 본 값이 260 · 270 처럼 늘
        정수 ℃ 인 것도 관측소 값을 그대로 받는 것과 맞는다.

        그래서 **집 마당 기온이 아니다.** 보일러에 외기 센서가 달려 있지 않아도
        값이 온다. 외기보상 제어(``outsideTemperatureControlUse`` ·
        ``outsideTemperatureStopUse``)와는 별개다.

        **``outsideTemperatureDisplayUse`` 를 조건으로 쓰지 않는다.** 그 값은
        룸콘 화면에 외기온도를 띄울지에 대한 것이지 자료가 오는지가 아니다 —
        이 기기는 그 값이 1 인데도 온도가 정상으로 온다.
        """
        return _tenth(self.status.get("outsideTemperature"))

    @property
    def hot_water_flow_rate(self) -> float | None:
        """온수 유량(L/분). 다른 0.1 단위 값과 같은 배율로 읽는다."""
        return _tenth(self.status.get("DHWInternalFlowRate"))

    @property
    def heating_flow_rate(self) -> float | None:
        """난방 유량(L/분)."""
        return _tenth(self.status.get("heatFlowRate"))

    @property
    def wifi_rssi(self) -> int | None:
        """룸콘 Wi-Fi 신호 세기. 서버가 알려주는 원시값 그대로다.

        단위를 확인하지 못했다 — dBm 의 절댓값인지 백분율인지 모른다. 그래서
        단위를 붙이지 않고 숫자만 진단으로 남긴다.
        """
        return _integer(self.status.get("wifiRssi"))

    @property
    def hot_water_running(self) -> bool | None:
        """지금 온수를 쓰고 있는지.

        ``fastDHWUse`` · ``smartFastDHW`` · ``DHWBoost`` 와 같은 1=끔·2=켬 값이다.
        """
        value = _integer(self.status.get("DHWUse"))
        return None if value not in (1, 2) else value == 2

    @property
    def hot_water_sustained(self) -> bool | None:
        """온수 사용이 이어지고 있는지. ``DHWUse`` 와 같은 인코딩이다."""
        value = _integer(self.status.get("DHWUseSustained"))
        return None if value not in (1, 2) else value == 2

    @property
    def fault_status(self) -> tuple[int, int] | None:
        """``faultStatus1`` · ``faultStatus2`` 비트묶음.

        각 비트의 뜻은 모른다. 0 이 아니면 무언가 걸렸다는 것만 알린다 —
        ``errorCode`` 와는 별개 필드다.
        """
        first = _integer(self.status.get("faultStatus1"))
        second = _integer(self.status.get("faultStatus2"))
        if first is None and second is None:
            return None
        return (first or 0, second or 0)

    @property
    def heating_intensity(self) -> int | None:
        """난방 강도 설정. **원시값 그대로 읽기만 한다.**

        NCB753 계열 설명서에서 단계 이름과 순서를 확인하지 못했고, 서버가 주는
        범위도 ``heatingIntensityMin: 3`` · ``heatingIntensityMax: 1`` 로 최소가
        최대보다 커서 방향조차 확정할 수 없다. 이름을 붙이거나 제어를 열지
        않는다.
        """
        return _integer(self.status.get("heatingIntensityModeSetting"))

    @property
    def repeat_reservation_interval(self) -> tuple[int, int] | None:
        """반복 예약 주기 (시, 분)."""
        hour = _integer(self.status.get("timeCycleReservationSettingHour"))
        minute = _integer(self.status.get("timeCycleReservationSettingMinute"))
        if hour is None and minute is None:
            return None
        return (hour or 0, minute or 0)

    @property
    def day_cycle_reservation(self) -> str | None:
        """24시간 예약 시간표 원문.

        실기기에서 24자 문자열로 온다 — 한 시간에 한 자리로 보이지만 각 자리의
        뜻은 확인하지 못했다. 해석하지 않고 원문만 남긴다.
        """
        value = self.status.get("dayCycleReservationSetting")
        return value if isinstance(value, str) and value else None

    @property
    def error_name(self) -> str | None:
        """설명서에 적힌 이상 발생 내용. 표에 없는 번호면 아무 이름도 주지 않는다."""
        code = self.error_code
        if not code:
            return None
        return BOILER_ERROR_NAMES.get(code)

    @property
    def error_label(self) -> str | None:
        """설명서 표기와 같은 ``E001`` 형태의 오류 번호."""
        code = self.error_code
        return None if not code else f"E{code:03d}"

    def reservation_enabled(self, key: str) -> bool | None:
        """예약 사용 여부. ``programReservationUse`` 계열의 1=끔·2=켬."""
        value = _integer(self.status.get(key))
        return None if value not in (1, 2) else value == 2

    @property
    def gas_month_start(self) -> date | None:
        """이번 달 누적값이 0으로 돌아간 시점 — 서버가 말한 달의 1일.

        벽시계로 계산하지 않는다. 달이 바뀌어도 다음 가스 응답이 오기 전까지는
        아직 지난달 누적값을 들고 있어서, 그 값의 주기 시작을 잘못 옮기면 장기
        통계가 한 달치를 통째로 잃는다.
        """
        for row in self._gas_rows("gasMeterThisMonth"):
            year = _integer(row.get("year"))
            month = _integer(row.get("month"))
            if year is not None and month is not None:
                return date(year, month, 1)
        return None

    @property
    def gas_total_month(self) -> float | None:
        return _tenth(self.gas_meter.get("thisYearMonthTotalGasUsage"))

    @property
    def gas_heating_month(self) -> float | None:
        return _tenth(self.gas_meter.get("thisYearMonthTotalHeatGasUsage"))

    @property
    def gas_hot_water_month(self) -> float | None:
        return _tenth(self.gas_meter.get("thisYearMonthTotalHotWaterGasUsage"))

    def gas_day(
        self, wanted: date
    ) -> tuple[float | None, float | None, float | None] | None:
        """앱의 일별 배열에서 지정한 현지 날짜의 전체·난방·온수 사용량."""
        rows = self.gas_meter.get("gasMeterThisMonth")
        if not isinstance(rows, list):
            return None
        same_month = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (
                _integer(row.get("year")) != wanted.year
                or _integer(row.get("month")) != wanted.month
            ):
                continue
            same_month = True
            if _integer(row.get("day")) == wanted.day:
                return (
                    _tenth(row.get("gasMeter")),
                    _tenth(row.get("heatGasMeter")),
                    _tenth(row.get("hotWaterGasMeter")),
                )
        # 앱 차트도 해당 월 배열에 빠진 날짜는 사용량 0으로 그린다. 다른 월의
        # 오래된 응답이면 0으로 단정하지 않고 unknown을 유지한다.
        return (0.0, 0.0, 0.0) if same_month else None

    def _gas_rows(self, key: str) -> list[dict[str, Any]]:
        rows = self.gas_meter.get(key)
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    def gas_history(self) -> list[GasUsageBucket]:
        """가스 응답의 네 배열을 하나의 시간순 사용량 목록으로 합친다.

        앱의 가스 사용량 화면이 그리는 것과 같은 자료다. 한 번의 조회 응답에
        **일별 두 달치**(``gasMeterLastMonth`` · ``gasMeterThisMonth``)와
        **월별 두 해치**(``gasMeterLastYear`` · ``gasMeterThisYear``)가 함께 온다.

        일별과 월별이 겹치는 달은 **일별만 남긴다.** 같은 사용량을 두 번 세지
        않기 위해서다. ``day`` 가 0 인 행이 그 달 전체의 합계라는 것은 실기기
        응답에서 확인했다 — 월별 배열은 12 개 행 모두 ``day: 0`` 이다.
        """
        daily: dict[date, GasUsageBucket] = {}
        for key in ("gasMeterLastMonth", "gasMeterThisMonth"):
            for row in self._gas_rows(key):
                if (bucket := _gas_bucket(row, monthly=False)) is not None:
                    daily[bucket.start] = bucket

        covered = {(bucket.start.year, bucket.start.month) for bucket in daily.values()}
        monthly: dict[date, GasUsageBucket] = {}
        for key in ("gasMeterLastYear", "gasMeterThisYear"):
            for row in self._gas_rows(key):
                bucket = _gas_bucket(row, monthly=True)
                if bucket is None:
                    continue
                if (bucket.start.year, bucket.start.month) in covered:
                    continue
                monthly[bucket.start] = bucket

        return sorted((monthly | daily).values(), key=lambda bucket: bucket.start)

    def _identity_parts(self) -> tuple[str, str, str]:
        """앱의 ``deviceId.substring(0, 12/16)`` 분기를 그대로 적용한다."""
        if self.model_code != BOILER_MODEL_MGPP:
            raise ValueError(f"지원하지 않는 보일러 modelCode: {self.model_code}")
        if len(self.device_id) < 16:
            raise ValueError("보일러 deviceId가 16자보다 짧습니다")
        mqtt_topic_key = str(self.raw.get("mqttTopicKey") or "")
        if not mqtt_topic_key:
            raise ValueError("보일러 mqttTopicKey가 없습니다")
        return self.device_id[:12], self.device_id[12:16], mqtt_topic_key

    def build_request_payload(
        self,
        request: str,
        response: str,
        client_id: str,
        *,
        now_ms: int | None = None,
        mode: str | None = None,
        param: list[float] | None = None,
        command: int | None = None,
        room_use_setting: str | None = None,
    ) -> dict[str, Any]:
        """Navien Smart 2.10.4 ``boilerMGPPMqttPayload`` 와 같은 봉투."""
        if not client_id:
            raise ValueError("MQTT clientId가 없습니다")
        mac, additional, mqtt_topic_key = self._identity_parts()
        stamp = int(time.time() * 1000) if now_ms is None else now_ms
        inner: dict[str, Any] = {
            "macAddress": mac,
            "registerAt": str(stamp),
            "additionalValue": additional,
            "param": [] if param is None else param,
            "paramStr": "",
            "deviceType": int(self.model_code),
        }
        if mode is not None:
            inner["mode"] = mode
        if command is not None:
            inner["command"] = command
        if room_use_setting is not None:
            inner["roomUseSetting"] = room_use_setting
        return {
            "protocolVersion": 1,
            "sessionID": str(stamp),
            "clientID": f"mobile-{client_id}",
            "requestTopic": f"cmd/{self.model_code}/roomcon-{mac}/{request}",
            "responseTopic": (
                f"cmd/{self.model_code}/{mqtt_topic_key}/mobile-{client_id}/{response}"
            ),
            "request": inner,
        }

    def build_status_payload(
        self, client_id: str, *, now_ms: int | None = None
    ) -> dict[str, Any]:
        """앱의 ``getDeviceStatus`` 와 같은 상태 요청."""
        return self.build_request_payload(
            "status", "res", client_id, now_ms=now_ms, command=16777219
        )

    def build_start_payload(
        self, client_id: str, *, now_ms: int | None = None
    ) -> dict[str, Any]:
        """앱의 ``getDeviceStart`` 와 같은 최초 상태 요청."""
        return self.build_request_payload(
            "status/start",
            "res/start",
            client_id,
            now_ms=now_ms,
            command=16777217,
        )

    def build_gas_payload(
        self, client_id: str, *, now_ms: int | None = None
    ) -> dict[str, Any]:
        """앱의 가스 사용량 화면이 보내는 읽기 요청."""
        if not self.supports_feature("gasUsageUse"):
            raise ValueError("가스 사용량 조회를 서버가 허용하지 않았습니다")
        return self.build_request_payload(
            "status/gas-meter-query",
            "res/gas-meter",
            client_id,
            now_ms=now_ms,
            command=16777224,
        )

    def build_power_payload(
        self, turn_on: bool, client_id: str, *, now_ms: int | None = None
    ) -> dict[str, Any]:
        """NR-67D 전원 명령. 앱처럼 전체 룸콘 비트마스크를 쓴다."""
        if not self.supports_feature("powerUse"):
            raise ValueError("전원 제어를 서버가 허용하지 않았습니다")
        return self.build_request_payload(
            "control",
            "res",
            client_id,
            now_ms=now_ms,
            mode="power-on" if turn_on else "power-off",
            command=33554434 if turn_on else 33554433,
            room_use_setting="11111111",
        )

    def build_switch_payload(
        self,
        kind: str,
        turn_on: bool,
        client_id: str,
        *,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """빠른온수·스마트운전·터보온수 명령을 만든다."""
        feature_keys = {
            "fast_dhw": "fastDHWUse",
            "smart_fast_dhw": "smartFastDHWUse",
            "dhw_boost": "DHWBoostUse",
        }
        try:
            feature_key = feature_keys[kind]
            _status_key, mode, command = BOILER_SWITCH_CONTROLS[kind]
        except KeyError as err:
            raise ValueError(f"알 수 없는 보일러 스위치 종류: {kind}") from err
        if not self.supports_feature(feature_key):
            raise ValueError(f"{kind} 제어를 서버가 허용하지 않았습니다")
        return self.build_request_payload(
            "control",
            "res",
            client_id,
            now_ms=now_ms,
            mode=mode,
            param=[2 if turn_on else 1],
            command=command,
            room_use_setting="10000000",
        )

    def build_temperature_payload(
        self, kind: str, target: float, client_id: str, *, now_ms: int | None = None
    ) -> dict[str, Any]:
        """NR-67D 설정온도 명령을 만든다. 섭씨값은 0.5℃ 원시값으로 바꾼다."""
        bounds = self.temperature_bounds(kind)
        if bounds is None:
            raise ValueError(f"{kind} 설정온도 제어를 서버가 허용하지 않았습니다")
        target = float(target)
        # modelCode=20 앱 분기는 SeekBar 원시 정수를 Float 로만 바꿔 보낸다.
        # 상태 61이 화면의 30.5℃이므로 전송도 섭씨×2인 61.0이어야 한다.
        raw_target = target * 2
        if not raw_target.is_integer():
            raise ValueError("NR-67D 설정온도는 0.5℃ 단위여야 합니다")
        if not bounds[0] <= target <= bounds[1]:
            raise ValueError(f"설정온도 {target:g}℃가 허용 범위를 벗어났습니다")
        try:
            mode, command, room_use_setting = BOILER_TEMPERATURE_CONTROLS[kind]
        except KeyError as err:
            raise ValueError(f"알 수 없는 설정온도 종류: {kind}") from err
        return self.build_request_payload(
            "control",
            "res",
            client_id,
            now_ms=now_ms,
            mode=mode,
            param=[raw_target],
            command=command,
            room_use_setting=room_use_setting,
        )

    @property
    def error_code(self) -> int | None:
        return _integer(self.status.get("errorCode"))

    @property
    def sub_error_code(self) -> int | None:
        return _integer(self.status.get("subErrorCode"))


def extract_boiler_status(
    payload: bytes, stats: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]] | None:
    """보일러 봉투에서 물리 기기 ID와 상태만 꺼낸다.

    실측 봉투는 ``payload.response.status`` 이고 기기 목록의
    ``Properties.did.response.macAddress`` 와 같은 값으로 기기를 찾는다. 명령 응답과
    DID/펌웨어 응답은 ``status`` 가 없으므로 상태로 쓰지 않는다.
    """
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _bump(stats, "dropped_not_json")
        return None
    response = _dig(event, "payload", "response")
    status = response.get("status") if isinstance(response, dict) else None
    gas_meter = response.get("gasMeter") if isinstance(response, dict) else None
    if isinstance(status, dict):
        update = status
    elif isinstance(gas_meter, dict):
        update = {BOILER_GAS_METER_UPDATE: gas_meter}
    else:
        _bump(stats, "dropped_no_status")
        return None
    physical_id = response.get("macAddress")
    if not physical_id:
        _bump(stats, "dropped_no_device_id")
        return None
    _bump(stats, "accepted")
    return str(physical_id), update


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

"""Boiler MQTT state and the NR-67D temperature control protocol.

A boiler has a different state model from a mat, and its encoding differs per controller
type. Guessing at a temperature value here would change someone's real heating or hot-water
setting, so only the hot-water and heating-water settings of ``modelCode=20``, confirmed in
the current app, are opened up.

What is kept here is **only the shape** of messages arriving on the same ``smarttok``
subscription the app uses. A diagnostics file can end up attached to a public issue, so raw
strings, topics, large numbers and binary are never retained. Small numbers and the key
structure are enough to tell the state envelopes apart; the meaning of an actual field is
opened in a separate step, once real-device observation and the app code agree.
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
# The official NCB753 manual calls this state 연소 (combustion). The 히팅 used at first
# appears in neither the manual nor the app.
BOILER_STATE_HEATING = "연소"

# Confirmed from the NR-67D (modelCode=20) tab-selection branch of Navien Smart 2.10.4.
# This value is the operating mode selected on the room controller, not whether it is burning
# right now; that is decided from the separate ``operationBusy`` value.
BOILER_OPERATION_MODE_NAMES: dict[int, str] = {
    1: "꺼짐",
    4: "외출",
    5: "실내 난방",
    6: "온돌 난방",
    7: "반복 예약",
    8: "24시간 예약",
    10: "온수 전용",
}

# Operating-mode commands confirmed on a real device. **These are observations, not
# guesses**: the room controller echoes the last command code it processed back in the
# status ``command`` field, so pressing that button in the app reveals the code.
#
#   0x2000001 = 33554433  power off       (mode 1, 꺼짐)
#   0x2000004 = 33554436  away            (mode 4, 외출)
#   0x2000006 = 33554438  underfloor temp (mode 6, 온돌 난방)
#
# The low digits line up with the operating-mode values. **The commands stay closed even
# so.** On this device the away command was echoed back seven times while ``operationMode``
# never moved off 6. Shipping a command the device accepts but never executes would give the
# user a switch that does nothing when pressed. It opens once some device is confirmed to
# execute it — read together with the support flags in `feature` (below).
#
# **Every feature confirmed to work has a `feature` value of 2.** powerUse, ondolUse and
# gasUsageUse · fastDHWUse · smartFastDHWUse · DHWBoostUse ·
# hotWaterTemperatureSettingUse are all 2, and all of them work on the real device.
# Conversely the away mode, which this device ignores, has ``gooutUse`` at 1. That the app's
# hot-water-only and away buttons do nothing at all also fits ``hotWaterUse`` being 1 — that
# value only makes sense read as support for the **hot-water-only operating mode**, not for
# the hot-water function itself.
#
# **The cause is still unknown.** Neither the app nor the room controller switches it to
# away. In this installation the room controller and the boiler are wired through a
# volt-free contact, so the away mode may simply not be conveyable over that wiring — or the
# device may be faulty. The evidence at hand cannot separate the two; that needs a question
# to the manufacturer. Either way the conclusion, **do not open the command**, is unchanged.
# Transcribed verbatim from the "12. 자가 진단 조치 방법" table of the official `NCB753`
# manual (2025-01-08 edition). Nothing was added or guessed.
#
# **How the numbers were matched, stated openly.** The manual writes them as `E001` while
# the server sends integers, and this table reads those integers as the manual's three-digit
# numbers. That is an alignment of two notations, not something confirmed by reproducing an
# error on a real device. So **an unmatched number is left unnamed and shown as a number** —
# better than attaching the wrong name.
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
    # From the modelCode=20 branch of Navien Smart 2.10.4. These three values move together.
    "hot_water": ("hotwater-temperature", 33554443, "10000000"),
    "ondol": ("ondol-heat", 33554438, "11111111"),
}

# Single-value switches confirmed from the modelCode=20 control calls of the current Navien
# Smart app. State and command are both 1=off, 2=on, and the all-controllers mask
# (11111111) is replaced by the first bit (10000000) for the hot-water function.
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
_MIXED_IDENTIFIER_KEY = re.compile(
    r"(?=[A-Za-z0-9]{12,})(?=(?:\D*\d){4})[A-Za-z0-9]{12,}"
)

# Key names are kept because the structure cannot be understood without them, but the values
# beneath them do not even reveal their type. Case and ``-``/``_`` differences are normalised
# away so a new variant falls under the same rule.
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
    """Convert a boiler value in 0.5C units to degrees Celsius.

    The ranges the server reports line up exactly — underfloor 60-130 maps to 30-65C and hot
    water 60-120 maps to 30-60C — and an observed setpoint of 86 matches the 43C shown in the
    app.
    """
    number = _number(value)
    return None if number is None else number / 2


def _tenth(value: Any) -> float | None:
    """Convert a 0.1-unit value from a precision sensor into a human-readable one."""
    number = _number(value)
    return None if number is None else number / 10


def _bump(stats: dict[str, Any] | None, key: str) -> None:
    if stats is not None:
        stats[key] = int(stats.get(key) or 0) + 1


@dataclass(frozen=True)
class GasUsageBucket:
    """One gas-usage bucket. ``start`` is the local date the bucket begins on."""

    start: date
    monthly: bool
    total: float
    heating: float
    hot_water: float


def _gas_bucket(row: dict[str, Any], *, monthly: bool) -> GasUsageBucket | None:
    """Turn one row of a gas array into a usage bucket.

    A day or month that has not happened yet arrives with all three values ``null``
    (confirmed in a real device response). Such a row means **no data**, not zero usage, and
    never becomes a statistics bucket.
    """
    year = _integer(row.get("year"))
    month = _integer(row.get("month"))
    if year is None or month is None or not 1 <= month <= 12:
        return None
    day = _integer(row.get("day"))
    if monthly:
        # Every row of a monthly array has day=0. A daily row mixed in would mean something
        # unknown, so it is discarded.
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
    """A boiler, combining REST device information with MQTT state."""

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
    # The state in the REST list may be a server cache. This timestamp is filled in only when
    # an actual MQTT frame arrives, so a sensor can tell the two apart.
    status_received_at: float | None = None
    # The last communication in either direction, covering both received state and requests
    # HA sent. While the boiler pushes its own changes nothing is polled; state is only asked
    # for after five minutes with no traffic either way.
    last_communication_at: float | None = None
    gas_received_at: float | None = None
    # The status echoes back the last command code the room controller processed. That is the
    # only way to learn a command whose meaning is still unknown (a mode change, say) without
    # guessing, so every code ever seen is collected into diagnostics. The values are protocol
    # constants, not identifying information.
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
        # The app response is usually ``{"mainItem": "보일러"}``, but depending on the account
        # or household it can arrive as a bare string. Stringifying the dict would expose
        # ``{'mainItem': '보일러'}`` as the HA device name, so the two shapes are handled
        # explicitly.
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
        """A partial response must not cost us fields that were already known."""
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
        """A boiler request successfully sent to the server also counts as communication."""
        self.last_communication_at = time.monotonic() if now is None else now

    def communication_age(self, *, now: float | None = None) -> float | None:
        """Seconds since the last boiler message in either direction."""
        if self.last_communication_at is None:
            return None
        stamp = time.monotonic() if now is None else now
        return max(0.0, stamp - self.last_communication_at)

    def silence_refresh_delay(self, *, now: float | None = None) -> float:
        """Time left before the five-minute idle status request; never rescheduled below 1s."""
        age = self.communication_age(now=now)
        if age is None:
            return float(BOILER_SILENCE_REFRESH_SECONDS)
        return max(1.0, BOILER_SILENCE_REFRESH_SECONDS - age)

    @property
    def available(self) -> bool:
        return self.connected and bool(self.status)

    @property
    def indoor_temperature(self) -> float | None:
        # actualInsideTemperature is a 0.1C precision value. Only a model lacking it falls
        # back to the 0.5C value.
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
        """The ``operationBusy=1`` state, in which the real device's water temperature did not rise."""
        return self.operation_busy == BOILER_BUSY_IDLE

    @property
    def operation_mode_name(self) -> str | None:
        """The operating-mode name, confirmed from the NR-67D tab labels in the app."""
        mode = self.operation_mode
        return None if mode is None else BOILER_OPERATION_MODE_NAMES.get(mode)

    @property
    def operating_state(self) -> str | None:
        """Power and heating state as a human-readable value, confirmed from the app's display logic.

        The ``modelCode=20`` screen of Navien Smart 2.10.4 treats ``operationMode=1`` as
        powered off. On a real device the heating-water temperature held steady while
        ``operationBusy=1`` and both supply and return temperatures rose after it changed to
        ``2``. The official NR-67D manual likewise separates the selected operating mode from
        the flame indicator that lights while it is actually running. Any other value is left
        as ``None`` rather than guessed to be heating.
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
        """The setting range the server allows for this device, in degrees Celsius."""
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
        """Whether the server declared this feature available (2) for this boiler."""
        return _integer(self.feature.get(key)) == 2

    def switch_state(self, kind: str) -> bool | None:
        """Convert the app's 1=off, 2=on state into a bool."""
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
        """Outside temperature. **Not measured by the boiler — a regional weather observation.**

        Confirmed by comparison. At one point this value read 27.0C while the KMA observation
        for Seoul (Songwol-dong, Jongno-gu) at the same moment was **exactly 27.0C**, whereas
        the neighbourhood estimate (Eungam 2-dong) was 25.8C. That every value seen so far has
        been a whole degree (260, 270) also fits a station reading passed straight through.

        So **this is not the temperature in the user's yard.** It arrives even on a boiler
        with no outside sensor fitted, and it is unrelated to outside-compensation control
        (``outsideTemperatureControlUse``, ``outsideTemperatureStopUse``).

        **``outsideTemperatureDisplayUse`` is not used as a condition.** That value governs
        whether the room controller shows the outside temperature on its screen, not whether
        the data arrives — this device has it at 1 and still receives the temperature.
        """
        return _tenth(self.status.get("outsideTemperature"))

    @property
    def hot_water_flow_rate(self) -> float | None:
        """Hot-water flow in L/min, read with the same scale as the other 0.1-unit values."""
        return _tenth(self.status.get("DHWInternalFlowRate"))

    @property
    def heating_flow_rate(self) -> float | None:
        """Heating flow in L/min."""
        return _tenth(self.status.get("heatFlowRate"))

    @property
    def wifi_rssi(self) -> int | None:
        """Room-controller Wi-Fi signal strength, exactly as the server reports it.

        The unit was never confirmed — it may be an absolute dBm value or a percentage. So no
        unit is attached and only the number is kept, for diagnostics.
        """
        return _integer(self.status.get("wifiRssi"))

    @property
    def hot_water_running(self) -> bool | None:
        """Whether hot water is being drawn right now.

        A 1=off, 2=on value, like ``fastDHWUse``, ``smartFastDHW`` and ``DHWBoost``.
        """
        value = _integer(self.status.get("DHWUse"))
        return None if value not in (1, 2) else value == 2

    @property
    def hot_water_sustained(self) -> bool | None:
        """Whether hot-water use is sustained. Same encoding as ``DHWUse``."""
        value = _integer(self.status.get("DHWUseSustained"))
        return None if value not in (1, 2) else value == 2

    @property
    def fault_status(self) -> tuple[int, int] | None:
        """The ``faultStatus1`` and ``faultStatus2`` bit fields.

        What the individual bits mean is unknown. A non-zero value only reports that something
        is flagged — this is a separate field from ``errorCode``.
        """
        first = _integer(self.status.get("faultStatus1"))
        second = _integer(self.status.get("faultStatus2"))
        if first is None and second is None:
            return None
        return (first or 0, second or 0)

    @property
    def heating_intensity(self) -> int | None:
        """The heating-intensity setting. **Read only, as a raw value.**

        The NCB753-family manual never names or orders the steps, and the range the server
        sends has ``heatingIntensityMin: 3`` above ``heatingIntensityMax: 1``, so even the
        direction cannot be settled. No names are attached and no control is opened.
        """
        return _integer(self.status.get("heatingIntensityModeSetting"))

    @property
    def repeat_reservation_interval(self) -> tuple[int, int] | None:
        """The repeat-schedule interval, as (hours, minutes)."""
        hour = _integer(self.status.get("timeCycleReservationSettingHour"))
        minute = _integer(self.status.get("timeCycleReservationSettingMinute"))
        if hour is None and minute is None:
            return None
        return (hour or 0, minute or 0)

    @property
    def day_cycle_reservation(self) -> str | None:
        """The raw 24-hour schedule table.

        It arrives from a real device as a 24-character string — apparently one digit per
        hour, but what each digit means was never confirmed. It is kept raw, uninterpreted.
        """
        value = self.status.get("dayCycleReservationSetting")
        return value if isinstance(value, str) and value else None

    @property
    def error_name(self) -> str | None:
        """The fault description from the manual. A number absent from the table gets no name."""
        code = self.error_code
        if not code:
            return None
        return BOILER_ERROR_NAMES.get(code)

    @property
    def error_label(self) -> str | None:
        """The error number in the manual's own ``E001`` form."""
        code = self.error_code
        return None if not code else f"E{code:03d}"

    def reservation_enabled(self, key: str) -> bool | None:
        """Whether a schedule is in use — the 1=off, 2=on family of ``programReservationUse``."""
        value = _integer(self.status.get(key))
        return None if value not in (1, 2) else value == 2

    @property
    def gas_month_start(self) -> date | None:
        """When this month's total last reset — the first of the month the server named.

        It is not computed from the wall clock. After the month rolls over, the value on hand
        is still last month's total until the next gas response arrives, and moving that
        value's cycle start would cost the long-term statistics an entire month.
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
        """Total, heating and hot-water usage for a given local date, from the app's daily array."""
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
        # The app chart also draws a date missing from that month's array as zero usage. For a
        # stale response from another month, unknown is kept rather than asserting zero.
        return (0.0, 0.0, 0.0) if same_month else None

    def _gas_rows(self, key: str) -> list[dict[str, Any]]:
        rows = self.gas_meter.get(key)
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    def gas_history(self) -> list[GasUsageBucket]:
        """Merge the four arrays of a gas response into one chronological usage list.

        This is the same data the app's gas-usage screen draws. A single query response
        carries **two months of daily figures** (``gasMeterLastMonth``, ``gasMeterThisMonth``)
        and **two years of monthly ones** (``gasMeterLastYear``, ``gasMeterThisYear``).

        Where daily and monthly overlap, **only the daily figures are kept**, so the same
        usage is never counted twice. That a row with ``day`` of 0 is the whole month's total
        was confirmed from a real device response — all twelve rows of a monthly array carry
        ``day: 0``.
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
        """Apply the app's ``deviceId.substring(0, 12/16)`` branch exactly."""
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
        """The same envelope as ``boilerMGPPMqttPayload`` in Navien Smart 2.10.4."""
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
        """The same status request as the app's ``getDeviceStatus``."""
        return self.build_request_payload(
            "status", "res", client_id, now_ms=now_ms, command=16777219
        )

    def build_start_payload(
        self, client_id: str, *, now_ms: int | None = None
    ) -> dict[str, Any]:
        """The same initial status request as the app's ``getDeviceStart``."""
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
        """The read request the app's gas-usage screen sends."""
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
        """The NR-67D power command, using the all-controllers bitmask as the app does."""
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
        """Build a fast-hot-water, smart-operation or turbo-hot-water command."""
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
        """Build an NR-67D setpoint command, converting Celsius into the raw 0.5C value."""
        bounds = self.temperature_bounds(kind)
        if bounds is None:
            raise ValueError(f"{kind} 설정온도 제어를 서버가 허용하지 않았습니다")
        target = float(target)
        # The modelCode=20 branch of the app sends the SeekBar's raw integer with nothing but a
        # cast to Float. A status of 61 is 30.5C on screen, so what goes out must likewise be
        # Celsius x 2, or 61.0.
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
    """Extract only the physical device id and the state from a boiler envelope.

    The observed envelope is ``payload.response.status``, and the device is found by matching
    ``Properties.did.response.macAddress`` from the device list. Command responses and
    DID/firmware responses carry no ``status`` and are never used as state.
    """
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _bump(stats, "dropped_not_json")
        return None
    if not isinstance(event, dict):
        _bump(stats, "dropped_not_object")
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


def safe_diagnostic_key(key: Any, index: int) -> str:
    """Keep only keys that look like field names, redacting identifiers used as dict keys."""
    text = str(key)
    compact = re.sub(r"[^A-Za-z0-9]", "", text)
    if (
        not _SAFE_KEY.fullmatch(text)
        or _IDENTIFIER_KEY.search(text)
        or _MIXED_IDENTIFIER_KEY.search(compact)
    ):
        return f"<key:{index}>"
    return text


def _safe_numeric_text(value: str) -> int | float | None:
    """Keep as numbers only the short numeric strings plausible as protocol values."""
    text = value.strip()
    number: int | float
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
            key = safe_diagnostic_key(raw_key, index)
            if _normalized_key(str(raw_key)) in _SENSITIVE_KEYS:
                result[key] = {"kind": "redacted"}
            else:
                result[key] = sanitize_boiler_value(inner, depth + 1)
        if len(value) > _MAX_DICT_ITEMS:
            result["<truncated_keys>"] = len(value) - _MAX_DICT_ITEMS
        return result

    if isinstance(value, list):
        items = [
            sanitize_boiler_value(inner, depth + 1)
            for inner in value[:_MAX_LIST_ITEMS]
        ]
        if len(value) > _MAX_LIST_ITEMS:
            items.append(
                {"kind": "truncated_items", "count": len(value) - _MAX_LIST_ITEMS}
            )
        return items

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

    # Even for a value outside JSON, the original is never left behind through repr.
    return {"kind": type(value).__name__}


def observe_boiler_message(payload: bytes, topic: str) -> dict[str, Any]:
    """Turn one MQTT message into an observation record carrying nothing personal.

    Anything that is not JSON keeps only its length. Raw binary, even a leading fragment of
    it, can hold a device identifier, so none of it is retained as hex or base64.
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
    # The MQTT frame is already in memory, but re-expanding a huge JSON document into a tree
    # would hold the event loop for too long. Its size and encoding kind are enough for
    # diagnostics.
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

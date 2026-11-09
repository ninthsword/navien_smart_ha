"""The mat's Bluetooth frames. Knows nothing of Home Assistant.

Every frame is 20 bytes, 0xFE where nothing is set, 0xB2 first and the low byte of the sum of
bytes 1 to 18 last.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .programs import Section

FRAME_LENGTH = 20
HEADER = 0xB2
FILL = 0xFE

KIND_STATUS = 0x00
KIND_REGISTER = 0x01
KIND_CONTROL = 0x80
KIND_REPLY = 0xF1

REPLY_REFUSED = 0
REPLY_ISSUED = 1
REPLY_AUTHENTICATED = 2

MODE_HEAT = 1
MODE_RESERVATION = 2
MODE_SLEEP = 3
MODE_ERROR = 5
MODE_OFF = 6
MODE_DISCHARGE_OR_FAST_HEAT = 7
MODE_ION_CARE = 8
MODE_CUSTOM_SLEEP = 9

HEAT_BOTH = 2
HEAT_LEFT_ONLY = 3
HEAT_RIGHT_ONLY = 4

FLAG_SINGLE = 0x01
FLAG_POWER = 0x02
FLAG_LOCK = 0x04

DEFAULT_HEAT_RANGE = (28, 45)
HEAT_RANGES = {
    0: (25, 43),
    1: (25, 45),
    2: (25, 48),
    3: (25, 50),
    4: (28, 45),
    5: (28, 45),
}
# Stands in wherever no temperature is remembered; it lies inside every range above.
INITIAL_TARGET = 30

READBACK = 0xFF
MAX_FAST_HEAT_MINUTES = 30
MAX_VOLUME = 3
WATER_LEVELS = (1, 2, 3)

ID_LENGTH = 6


class MatState(Enum):
    """The whole mat's state, the only input to the command rules."""

    HEATING = "heating"
    OFF = "off"
    OTHER = "other"


@dataclass(frozen=True)
class MatStatus:
    """One status frame."""

    raw: bytes

    @property
    def mode(self) -> int:
        return self.raw[2]

    @property
    def is_double(self) -> bool:
        return not self.raw[3] & FLAG_SINGLE

    @property
    def power(self) -> bool:
        return bool(self.raw[3] & FLAG_POWER)

    @property
    def lock(self) -> bool:
        return bool(self.raw[3] & FLAG_LOCK)

    @property
    def heat_type(self) -> int:
        return self.raw[5]

    @property
    def selected_sides(self) -> tuple[bool, bool]:
        """The sides selected by byte 5, independently of the operating mode."""
        return (
            self.heat_type in (HEAT_BOTH, HEAT_LEFT_ONLY),
            self.heat_type in (HEAT_BOTH, HEAT_RIGHT_ONLY),
        )

    @property
    def off_reservation_active(self) -> bool:
        return self.mode == MODE_RESERVATION and self.power

    @property
    def off_reservation_hours(self) -> int | None:
        return self.raw[12] if self.off_reservation_active else None

    @property
    def on_reservation_active(self) -> bool:
        return (self.mode == MODE_OFF and self.raw[10] == 1) or (
            self.mode == MODE_RESERVATION and not self.power
        )

    @property
    def on_reservation_hours(self) -> int | None:
        if not self.on_reservation_active:
            return None
        return self.raw[11] if self.mode == MODE_OFF else self.raw[13]

    @property
    def sleep_running(self) -> bool:
        return self.mode == MODE_SLEEP and self.power

    @property
    def custom_readback(self) -> bool:
        return self.mode == MODE_CUSTOM_SLEEP and self.raw[18] == READBACK

    @property
    def custom_sleep_running(self) -> bool:
        return (
            self.mode == MODE_CUSTOM_SLEEP
            and self.power
            and not self.custom_readback
            and self.raw[14] != 0
        )

    @property
    def sleep_range(self) -> tuple[int, int] | None:
        """This frame's sleep range; unknown codes do not inherit an earlier range."""
        if self.raw[17] == FILL:
            return (30, 37)
        code = self.raw[17] >> 4
        if code == 0:
            return (30, 35)
        if code == 1:
            return (30, 33)
        return None

    @property
    def volume(self) -> int:
        return min(self.raw[4] >> 4, MAX_VOLUME)

    @property
    def water_level(self) -> int | None:
        level = self.raw[4] & 0x0F
        return level if level in WATER_LEVELS else None

    @property
    def _reads_capabilities(self) -> bool:
        """Whether this frame's mode carries bytes 16 to 18; others report the defaults."""
        if self.mode == MODE_CUSTOM_SLEEP:
            return self.raw[18] != READBACK
        return self.mode in (MODE_HEAT, 2, 3, MODE_OFF)

    @property
    def model_code(self) -> int:
        if not self._reads_capabilities or self.raw[17] == FILL:
            return 0
        code = self.raw[17] & 0x0F
        return code if code in (2, 3) else 1

    @property
    def has_extras(self) -> bool:
        """Whether the model has fast heat and ion care."""
        return self.model_code >= 2

    @property
    def heat_range(self) -> tuple[int, int]:
        """This frame's own (minimum, maximum) target; nothing is carried between frames."""
        if not self._reads_capabilities:
            return DEFAULT_HEAT_RANGE
        return HEAT_RANGES.get(self.raw[18], DEFAULT_HEAT_RANGE)

    @property
    def min_target(self) -> int:
        return self.heat_range[0]

    def is_valid_target(self, value: int) -> bool:
        """Tell a real target from an off mat's 0 or any value outside this frame's range."""
        low, high = self.heat_range
        return low <= value <= high

    @property
    def fast_heat_minutes(self) -> int:
        if not self.has_extras or self.mode == MODE_OFF:
            return 0
        return min(self.raw[15], MAX_FAST_HEAT_MINUTES)

    @property
    def fast_heat_running(self) -> bool:
        return self.fast_heat_minutes > 0

    @property
    def ion_care_running(self) -> bool:
        return self.has_extras and bool(self.raw[16] & 0x0F)

    @property
    def ion_care_period(self) -> int:
        return self.raw[16] >> 4 if self.has_extras else 0

    @property
    def error_code(self) -> int:
        return self.raw[9] * 100 + self.raw[10] if self.mode == MODE_ERROR else 0

    @property
    def left_current(self) -> int:
        return self.raw[7]

    @property
    def right_current(self) -> int:
        return self.raw[8]

    @property
    def left_target(self) -> int:
        return self.raw[10]

    @property
    def right_target(self) -> int:
        return self.raw[11]

    @property
    def state(self) -> MatState:
        if not self.power or self.mode == MODE_OFF:
            return MatState.OFF
        if self.mode == MODE_HEAT and self.heat_type in (
            HEAT_BOTH,
            HEAT_LEFT_ONLY,
            HEAT_RIGHT_ONLY,
        ):
            return MatState.HEATING
        return MatState.OTHER

    @property
    def left_on(self) -> bool:
        return self.state is MatState.HEATING and self.heat_type != HEAT_RIGHT_ONLY

    @property
    def right_on(self) -> bool:
        return self.state is MatState.HEATING and self.heat_type != HEAT_LEFT_ONLY

    @property
    def is_restorable(self) -> bool:
        """Whether a stored frame can supply a power-on, validated by its own range."""
        return (
            self.state is MatState.HEATING
            and self.is_valid_target(self.left_target)
            and self.is_valid_target(self.right_target)
        )

    @property
    def is_storable(self) -> bool:
        """Whether this live status replaces the stored frame whole."""
        return self.is_restorable and self.heat_type == HEAT_BOTH


@dataclass(frozen=True)
class HandshakeReply:
    """One reply to a registration or authentication frame."""

    code: int
    issued_id: bytes | None = None


def _checksum(frame: bytes | bytearray) -> int:
    return sum(frame[1:19]) & 0xFF


def _finish(frame: bytearray) -> bytes:
    frame[19] = _checksum(frame)
    return bytes(frame)


def _blank(kind: int) -> bytearray:
    frame = bytearray([FILL]) * FRAME_LENGTH
    frame[0] = HEADER
    frame[1] = kind
    return frame


def parse_frame(data: bytes | bytearray) -> MatStatus | HandshakeReply | None:
    """Read a received frame; anything malformed or unknown is None and is ignored."""
    if (
        len(data) != FRAME_LENGTH
        or data[0] != HEADER
        or data[19] != _checksum(data)
    ):
        return None
    if data[1] == KIND_STATUS:
        return MatStatus(bytes(data))
    if data[1] == KIND_REPLY:
        issued = bytes(data[3:3 + ID_LENGTH]) if data[2] == REPLY_ISSUED else None
        return HandshakeReply(data[2], issued)
    return None


def parse_status(data: bytes) -> MatStatus | None:
    """Read a stored status frame back."""
    frame = parse_frame(data)
    return frame if isinstance(frame, MatStatus) else None


def build_registration_frame() -> bytes:
    """Ask a mat in pairing mode to issue an id."""
    return _finish(_blank(KIND_REGISTER))


def build_authentication_frame(issued_id: bytes) -> bytes:
    """Present the issued id; also the answer to the registration reply."""
    frame = _blank(KIND_REGISTER)
    frame[2:2 + ID_LENGTH] = issued_id
    return _finish(frame)


def _volume(latest: MatStatus) -> int:
    return min(latest.raw[4] >> 4, 3) << 4


def build_heating_frame(
    latest: MatStatus,
    source: MatStatus,
    left: int,
    right: int,
    heat_type: int = HEAT_BOTH,
    lock: bool | None = None,
) -> bytes:
    """Set both sides' targets and run; `source` supplies byte 9, `latest` the rest."""
    frame = _blank(KIND_CONTROL)
    frame[2] = MODE_HEAT
    locked = latest.lock if lock is None else lock
    frame[3] = FLAG_POWER | (FLAG_LOCK if locked else 0)
    frame[4] = _volume(latest)
    frame[5] = heat_type
    frame[6:9] = latest.raw[6:9]
    frame[9] = source.raw[9]
    frame[10] = left
    frame[11] = right
    return _finish(frame)


def _running_frame(latest: MatStatus, mode: int) -> bytearray:
    """A command that keeps the mat running: byte 5 and bytes 6 to 11 echoed."""
    frame = _blank(KIND_CONTROL)
    frame[2] = mode
    frame[3] = FLAG_POWER | (latest.raw[3] & FLAG_LOCK)
    frame[4] = _volume(latest)
    frame[5:12] = latest.raw[5:12]
    return frame


def build_volume_frame(latest: MatStatus, volume: int) -> bytes:
    """Change the sound volume; unlike a heating frame, bytes 6 and 9 stay unset."""
    frame = _running_frame(latest, MODE_HEAT)
    frame[4] = volume << 4
    frame[6] = FILL
    frame[9] = FILL
    return _finish(frame)


def build_fast_heat_frame(latest: MatStatus, on: bool) -> bytes:
    frame = _running_frame(latest, MODE_DISCHARGE_OR_FAST_HEAT)
    frame[12] = int(on)
    return _finish(frame)


def build_off_reservation_frame(latest: MatStatus, hours: int | None) -> bytes:
    """Set an off reservation, or cancel it with a normal heat frame."""
    frame = _running_frame(latest, MODE_HEAT if hours is None else MODE_RESERVATION)
    if hours is not None:
        frame[12] = hours
    return _finish(frame)


def build_on_reservation_frame(
    latest: MatStatus, hours: int | None, temperature: int
) -> bytes:
    """Set an on reservation; cancellation echoes its value and sets the power bit."""
    frame = _blank(KIND_CONTROL)
    frame[2] = MODE_OFF
    frame[3] = (latest.raw[3] & FLAG_LOCK) | (FLAG_POWER if hours is None else 0)
    frame[4] = _volume(latest)
    frame[6:9] = latest.raw[6:9]
    frame[10] = 1
    if hours is None:
        frame[11] = latest.raw[11] if latest.mode == MODE_OFF else latest.raw[13]
    else:
        frame[11] = hours
        frame[12] = 0
    frame[15] = temperature
    return _finish(frame)


def build_sleep_frame(
    latest: MatStatus, left_hours: int, right_hours: int, on: bool
) -> bytes:
    frame = _running_frame(latest, MODE_SLEEP)
    frame[12] = 0
    frame[13] = left_hours
    frame[14] = right_hours
    frame[15] = int(on)
    frame[16] = int(on)
    return _finish(frame)


def build_ion_care_frame(latest: MatStatus, period: int, start: bool) -> bytes:
    """Run ion care or set its period, which share one layout; period 0 leaves byte 16 unset."""
    frame = _running_frame(latest, MODE_ION_CARE)
    frame[12] = int(start)
    if period > 0:
        frame[16] = period << 4
    return _finish(frame)


def build_custom_sleep_frame(
    latest: MatStatus, heat_type: int, half: int, sections: list[Section]
) -> bytes:
    """Send one side; the count makes zero-filled unused sections irrelevant."""
    frame = _blank(KIND_CONTROL)
    frame[2] = MODE_CUSTOM_SLEEP
    frame[3] = FLAG_POWER | (latest.raw[3] & FLAG_LOCK)
    frame[4] = _volume(latest)
    frame[5] = heat_type
    frame[6] = half
    frame[7] = len(sections)
    frame[8:18] = bytes(10)
    for index, section in enumerate(sections):
        frame[8 + index] = section["hours"]
        frame[13 + index] = section["temperature"]
    return _finish(frame)


def build_custom_sleep_stop_frame(latest: MatStatus) -> bytes:
    return _finish(_running_frame(latest, MODE_HEAT))


def merge_one_side(stored: MatStatus | None, status: MatStatus) -> MatStatus | None:
    """The stored frame updated from a one-side heating `status`, or a new one made from it.

    Takes the sides that are on, the range and the active side's target from `status`. The off
    side keeps its remembered target, or the initial one if none is remembered or it is outside
    the copied range. None when `status` is not one-side heating or its active target is invalid.
    """
    if status.state is not MatState.HEATING or status.heat_type == HEAT_BOTH:
        return None
    index, other = (10, 11) if status.heat_type == HEAT_LEFT_ONLY else (11, 10)
    if not status.is_valid_target(status.raw[index]):
        return None
    frame = bytearray(status.raw if stored is None else stored.raw)
    frame[5] = status.raw[5]
    frame[18] = status.raw[18]
    frame[index] = status.raw[index]
    if stored is None or not status.is_valid_target(frame[other]):
        frame[other] = INITIAL_TARGET
    return MatStatus(_finish(frame))


def build_off_frame(latest: MatStatus) -> bytes:
    """Turn the whole mat off."""
    frame = _blank(KIND_CONTROL)
    frame[2] = MODE_OFF
    frame[3] = latest.raw[3] & FLAG_LOCK
    frame[4] = _volume(latest)
    frame[6:9] = latest.raw[6:9]
    frame[10:15] = bytes(5)
    return _finish(frame)


def build_off_lock_frame(latest: MatStatus, lock: bool) -> bytes:
    """Change the lock of a mat that is off, keeping any on-reservation."""
    frame = _blank(KIND_CONTROL)
    frame[2] = MODE_OFF
    frame[3] = FLAG_LOCK if lock else 0
    frame[4] = _volume(latest)
    frame[6:9] = latest.raw[6:9]
    frame[10:13] = latest.raw[10:13]
    frame[13] = latest.raw[11]
    frame[14] = latest.raw[12]
    return _finish(frame)

"""The mat's Bluetooth frames. Knows nothing of Home Assistant.

Every frame is 20 bytes, 0xFE where nothing is set, 0xB2 first and the low byte of the sum of
bytes 1 to 18 last.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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
MODE_ERROR = 5
MODE_OFF = 6

HEAT_BOTH = 2
HEAT_LEFT_ONLY = 3
HEAT_RIGHT_ONLY = 4

FLAG_SINGLE = 0x01
FLAG_POWER = 0x02
FLAG_LOCK = 0x04

MIN_TARGET = 28
MAX_TARGET = 45

ID_LENGTH = 6


class MatState(Enum):
    """The whole mat's state, the only input to the command rules."""

    HEATING = "heating"
    OFF = "off"
    OTHER = "other"


def is_valid_target(value: int) -> bool:
    """Tell a real target from an off mat's 0 or any out-of-range value."""
    return MIN_TARGET <= value <= MAX_TARGET


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
        if self.mode == MODE_HEAT and self.heat_type == HEAT_BOTH:
            return MatState.HEATING
        return MatState.OTHER

    @property
    def is_storable(self) -> bool:
        """Whether this status can later supply a power-on frame's targets."""
        return (
            self.state is MatState.HEATING
            and is_valid_target(self.left_target)
            and is_valid_target(self.right_target)
        )


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
    latest: MatStatus, source: MatStatus, left: int, right: int
) -> bytes:
    """Set both sides' targets and run; `source` supplies byte 9, `latest` the rest."""
    frame = _blank(KIND_CONTROL)
    frame[2] = MODE_HEAT
    frame[3] = FLAG_POWER | (latest.raw[3] & FLAG_LOCK)
    frame[4] = _volume(latest)
    frame[5] = HEAT_BOTH
    frame[6:9] = latest.raw[6:9]
    frame[9] = source.raw[9]
    frame[10] = left
    frame[11] = right
    return _finish(frame)


def build_off_frame(latest: MatStatus) -> bytes:
    """Turn the whole mat off."""
    frame = _blank(KIND_CONTROL)
    frame[2] = MODE_OFF
    frame[3] = latest.raw[3] & FLAG_LOCK
    frame[4] = _volume(latest)
    frame[6:9] = latest.raw[6:9]
    frame[10:15] = bytes(5)
    return _finish(frame)

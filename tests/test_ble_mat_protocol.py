"""The Bluetooth mat's frames, against vectors recorded from a real mat."""

from __future__ import annotations

import sys

from harness import Report
from navien_smarthome.ble_mat.protocol import (
    HEAT_LEFT_ONLY,
    HEAT_RIGHT_ONLY,
    HandshakeReply,
    MatState,
    MatStatus,
    build_authentication_frame,
    build_heating_frame,
    build_off_frame,
    build_registration_frame,
    parse_frame,
)
from navien_smarthome.ble_mat.transport import CHARACTERISTIC_WRITE

r = Report()

HEATING = bytes.fromhex("b20001020302241e1c231e1cfefefe0060120433")
HEATING_2 = bytes.fromhex("b20001020302231e1c231e1cfefefe0060120432")
OFF = bytes.fromhex("b20006000300231e1cfe000000000000601204da")
SYNTHETIC_ID = bytes.fromhex("010203040506")


def status(raw: bytes) -> MatStatus:
    frame = parse_frame(raw)
    assert isinstance(frame, MatStatus)
    return frame


def with_byte(raw: bytes, index: int, value: int) -> bytes:
    data = bytearray(raw)
    data[index] = value
    data[19] = sum(data[1:19]) & 0xFF
    return bytes(data)


r.section("status parsing")
s = status(HEATING)
r.ok(s.mode == 1 and s.power and s.is_double and not s.lock, "mode and flags")
r.ok(
    (s.heat_type, s.left_current, s.right_current, s.left_target, s.right_target)
    == (2, 30, 28, 30, 28),
    "byte 5, currents and targets",
)
r.ok(s.state is MatState.HEATING, "heating state")
r.ok(status(OFF).state is MatState.OFF, "off example is off")
r.ok(
    status(with_byte(HEATING, 3, 0x03)).is_double is False,
    "byte 3 bit 0 marks a single mat",
)
sleep = status(with_byte(HEATING, 2, 3))
r.ok(sleep.state is MatState.OTHER, "sleep mode is other")
r.ok(
    status(with_byte(HEATING, 5, 3)).state is MatState.HEATING,
    "left side only is heating",
)
r.ok(
    status(with_byte(HEATING, 5, 4)).state is MatState.HEATING,
    "right side only is heating",
)
r.ok(status(with_byte(HEATING, 5, 0)).state is MatState.OTHER, "byte 5 of 0 is other")
r.ok(status(with_byte(HEATING, 5, 9)).state is MatState.OTHER, "an unnamed byte 5 is other")
r.ok(
    status(with_byte(with_byte(HEATING, 2, 6), 3, 0x02)).state is MatState.OFF,
    "mode 6 with the power bit is off",
)

r.section("invalid frames are ignored")
r.ok(parse_frame(HEATING[:-1]) is None, "wrong length")
r.ok(parse_frame(b"\xa0" + HEATING[1:]) is None, "wrong first byte")
r.ok(parse_frame(HEATING[:-1] + b"\x00") is None, "wrong checksum")

r.section("handshake frames")
frame = parse_frame(with_byte(with_byte(HEATING, 1, 0xF1), 2, 1))
r.ok(
    isinstance(frame, HandshakeReply)
    and frame.code == 1
    and frame.issued_id == with_byte(HEATING, 1, 0xF1)[3:9],
    "reply 1 carries the id",
)
reg = build_registration_frame()
r.ok(
    len(reg) == 20 and reg[0] == 0xB2 and reg[1] == 0x01 and reg[2:8] == b"\xfe" * 6,
    "registration frame has no id",
)
auth = build_authentication_frame(SYNTHETIC_ID)
r.ok(auth[2:8] == SYNTHETIC_ID and auth[8] == 0xFE, "authentication frame carries the id")
r.ok(auth[19] == sum(auth[1:19]) & 0xFF, "checksum is the sum of bytes 1 to 18")

r.section("control vectors, byte for byte")
latest = status(HEATING)
r.ok(
    build_heating_frame(latest, latest, 30, 29).hex()
    == "b28001020002241e1c231e1dfefefefefefefe33",
    "heating frame, right target 29",
)
latest2 = status(HEATING_2)
r.ok(
    build_off_frame(latest2).hex() == "b280060000fe231e1cfe0000000000fefefefed7",
    "off frame",
)
r.ok(
    build_heating_frame(latest2, latest2, 30, 28).hex()
    == "b28001020002231e1c231e1cfefefefefefefe31",
    "on frame with targets 30 and 28",
)

r.ok(
    build_heating_frame(latest, latest, 30, 28, HEAT_LEFT_ONLY).hex()
    == "b28001020003241e1c231e1cfefefefefefefe33",
    "heating frame, byte 5 of 3 (left only)",
)
r.ok(
    build_heating_frame(latest, latest, 28, 34, HEAT_RIGHT_ONLY).hex()
    == "b28001020004241e1c231c22fefefefefefefe38",
    "heating frame, byte 5 of 4 (right only)",
)

r.section("the write characteristic")
r.ok(
    CHARACTERISTIC_WRITE == "00001c0d-d102-11e1-9b23-2ce2a80200dd",
    "frames go to the characteristic the live tests wrote to",
)

r.section("byte 4 and the lock bit")
nibble = status(with_byte(HEATING, 4, 0x25))
r.ok(build_heating_frame(nibble, nibble, 30, 28)[4] == 0x20, "non-zero high nibble")
over = status(with_byte(HEATING, 4, 0x73))
r.ok(build_heating_frame(over, over, 30, 28)[4] == 0x30, "over-range nibble capped at 3")
locked = status(with_byte(HEATING, 3, 0x06))
r.ok(build_heating_frame(locked, locked, 30, 28)[3] == 0x06, "heating keeps the lock bit")
r.ok(build_off_frame(locked)[3] == 0x04, "off keeps only the lock bit")
source = status(with_byte(HEATING, 9, 0x20))
r.ok(build_heating_frame(latest, source, 30, 28)[9] == 0x20, "byte 9 comes from the source")

sys.exit(r.finish())

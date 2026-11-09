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
    build_off_reservation_frame,
    build_on_reservation_frame,
    build_registration_frame,
    build_sleep_frame,
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

r.section("the status model")
full = status(with_byte(with_byte(with_byte(HEATING, 4, 0x71), 15, 45), 16, 0x51))
r.ok(full.volume == 3 and full.water_level == 1, "byte 4 volume capped and water level")
r.ok(full.fast_heat_minutes == 30 and full.fast_heat_running, "byte 15 capped at 30")
r.ok(full.ion_care_running and full.ion_care_period == 5, "byte 16 both nibbles")
r.ok(status(HEATING).model_code == 2, "the recorded mat is model code 2")
r.ok(status(HEATING).heat_range == (28, 45), "the recorded mat is 28 to 45")
r.ok(status(with_byte(HEATING, 17, 0xFE)).model_code == 0, "byte 17 of 0xFE is code 0")
idle = status(with_byte(with_byte(HEATING, 17, 0x11), 15, 9))
r.ok(idle.model_code == 1 and not idle.fast_heat_running, "code 1 forces idle")
r.ok(status(with_byte(HEATING, 18, 3)).heat_range == (25, 50), "range code 3")
r.ok(status(with_byte(HEATING, 18, 0x20)).heat_range == (28, 45), "an unknown range code")
readback = status(with_byte(with_byte(HEATING, 2, 9), 18, 0xFF))
r.ok(readback.heat_range == (28, 45) and readback.model_code == 0, "a readback carries defaults")
r.ok(status(with_byte(HEATING, 2, 8)).model_code == 0, "mode 8 carries no capabilities")
off_status = status(with_byte(OFF, 15, 9))
r.ok(off_status.fast_heat_minutes == 0, "an off frame reads no fast heat")

r.section("reservation and sleep status is per frame")
reserved = status(with_byte(with_byte(HEATING, 2, 2), 12, 5))
r.ok(
    reserved.off_reservation_active and reserved.off_reservation_hours == 5,
    "mode 2 with power reads the off reservation from byte 12",
)
r.ok(reserved.state is MatState.OTHER, "a powered reservation stays other")
on_reserved = status(with_byte(with_byte(with_byte(OFF, 10, 1), 11, 7), 13, 9))
r.ok(
    on_reserved.on_reservation_active and on_reserved.on_reservation_hours == 7,
    "mode 6 reads the on reservation from byte 11",
)
on_mode_2 = status(with_byte(with_byte(on_reserved.raw, 2, 2), 13, 9))
r.ok(
    on_mode_2.on_reservation_active and on_mode_2.on_reservation_hours == 9,
    "unpowered mode 2 reads the on reservation from byte 13",
)
r.ok(
    not status(HEATING).off_reservation_active
    and status(HEATING).off_reservation_hours is None
    and not status(OFF).on_reservation_active
    and status(OFF).on_reservation_hours is None,
    "an inactive reservation has no carried hours",
)
r.ok(sleep.sleep_running and sleep.state is MatState.OTHER, "powered mode 3 runs sleep")
r.ok(
    not status(with_byte(sleep.raw, 3, 0)).sleep_running,
    "unpowered mode 3 does not run sleep",
)
for value, bounds in (
    (0xFE, (30, 37)),
    (0x00, (30, 35)),
    (0x02, (30, 35)),
    (0x03, (30, 35)),
    (0x12, (30, 33)),
    (0x1F, (30, 33)),
    (0x22, None),
    (0xFF, None),
):
    r.ok(status(with_byte(HEATING, 17, value)).sleep_range == bounds, f"sleep range {value:#x}")
for mode in (1, 2, 3, 6, 9):
    for heat_type, sides in (
        (2, (True, True)),
        (3, (True, False)),
        (4, (False, True)),
        (0, (False, False)),
        (9, (False, False)),
    ):
        frame = status(with_byte(with_byte(HEATING, 2, mode), 5, heat_type))
        r.ok(frame.selected_sides == sides, f"mode {mode}, byte 5 {heat_type}: selected sides")

r.section("reservation and sleep control vectors")
r.ok(
    build_off_reservation_frame(status(HEATING), 5)
    == bytes.fromhex("b28002020002241e1c231e1c05fefefefefefe3a"),
    "set off reservation",
)
r.ok(
    build_off_reservation_frame(reserved, None)
    == bytes.fromhex("b28001020002241e1c231e1cfefefefefefefe32"),
    "cancel off reservation",
)
off_source = status(with_byte(with_byte(HEATING, 2, 6), 3, 0))
r.ok(
    build_on_reservation_frame(off_source, 5, 30)
    == bytes.fromhex("b280060000fe241e1cfe010500fefe1efefefefa"),
    "set on reservation",
)
on_source = status(with_byte(with_byte(off_source.raw, 10, 1), 11, 5))
r.ok(
    build_on_reservation_frame(on_source, None, 0)
    == bytes.fromhex("b280060200fe241e1cfe0105fefefe00fefefedc"),
    "cancel on reservation",
)
sleep_source = status(with_byte(with_byte(HEATING, 10, 31), 11, 32))
r.ok(
    build_sleep_frame(sleep_source, 8, 8, True)
    == bytes.fromhex("b28003020002241e1c231f200008080101fefe55"),
    "start sleep",
)
r.ok(
    build_sleep_frame(sleep_source, 0, 0, False)
    == bytes.fromhex("b28003020002241e1c231f200000000000fefe43"),
    "stop sleep",
)

sys.exit(r.finish())

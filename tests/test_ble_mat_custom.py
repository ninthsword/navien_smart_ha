"""Custom program storage and command exchanges without third-party dependencies."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from harness import Report
from navien_smarthome.ble_mat import connection
from navien_smarthome.ble_mat.programs import CONF_CUSTOM_PROGRAMS, read_program
from navien_smarthome.ble_mat.protocol import MatStatus
from navien_smarthome.config_flow import _custom_sections

r = Report()
LEFT = [{"hours": 2, "temperature": 30}, {"hours": 3, "temperature": 31}]
RIGHT = [{"hours": 4, "temperature": 32}]
PROGRAM = {"left": LEFT, "right": RIGHT}
READY = bytes.fromhex("b200010e2302241e1c231f20fefefe0060120464")
LEFT_FRAME = bytes.fromhex("b28009062002010202030000001e1f000000fef4")
RIGHT_FRAME = bytes.fromhex("b28009062002020104000000002000000000fed6")


def status(
    mode: int = 9, state: int = 0, readback: bool = False, *, heat: int = 2, power: bool = True,
) -> bytes:
    frame = bytearray(READY)
    frame[2] = mode
    if not power:
        frame[3] &= ~2
    frame[5] = heat
    frame[14] = state
    if readback:
        frame[18] = 0xFF
    frame[19] = sum(frame[1:19]) & 255
    return bytes(frame)


r.section("stored custom programs")
r.ok(read_program({CONF_CUSTOM_PROGRAMS: {"1": PROGRAM}}, "1") == PROGRAM, "valid program")
r.ok(read_program({}, "1") is None, "older options have no programs")
for invalid in (
    None, [], {}, {"left": [], "right": []}, {"left": LEFT},
    {"left": [{"hours": 0, "temperature": 30}], "right": RIGHT},
    {"left": [{"hours": True, "temperature": 30}], "right": RIGHT},
    {"left": [{"hours": 1, "temperature": 30.5}], "right": RIGHT},
    {"left": [{"hours": 1, "temperature": 24}], "right": RIGHT},
    {"left": [{"hours": 1, "temperature": 51}], "right": RIGHT},
    {"left": [{"hours": 7, "temperature": 30}] * 2, "right": RIGHT},
    {"left": [{"hours": 1, "temperature": 30}] * 6, "right": RIGHT},
):
    r.ok(read_program({CONF_CUSTOM_PROGRAMS: {"1": invalid}}, "1") is None, "malformed program is absent")

r.section("form row validation")
for fields, error in (
    ({"enabled": False, "hours_1": 2}, None),
    ({"enabled": True, "hours_1": 2}, None),
    ({"enabled": True}, "custom_hours"),
    ({"enabled": True, "hours_1": 12, "hours_2": 1}, "custom_hours"),
    ({"enabled": True, "hours_2": 2}, "custom_contiguous"),
    ({"enabled": True, "hours_1": 1, "hours_3": 2}, "custom_contiguous"),
    ({"enabled": True, "hours_1": 1.5}, "custom_rows"),
    ({"enabled": True, "hours_1": 1, "temperature_1": 30.5}, "custom_rows"),
):
    _, actual = _custom_sections(fields)
    r.ok(actual == error, f"form validation: {error}")

r.section("custom status")
for mode, state, readback, running in (
    (9, 0, False, False), (9, 1, False, True), (9, 2, False, True),
    (9, 3, False, True), (9, 1, True, False), (1, 1, False, False),
):
    r.ok(MatStatus(status(mode, state, readback)).custom_sleep_running == running, "running rule")


async def exchange(
    replies: list[bytes | None], expected: list[bytes], error: str | None = None,
    *, on: bool = True, initial: bytes | None = None,
) -> None:
    hass: Any = SimpleNamespace(loop=asyncio.get_running_loop())
    entry: Any = SimpleNamespace(
        entry_id="synthetic", data={"address": "00:11:22:33:44:55", "mat_id": "a1a2a3a4a5a6"},
        options={CONF_CUSTOM_PROGRAMS: {"1": PROGRAM}},
    )
    conn = connection.MatConnection(hass, entry)
    store: Any = SimpleNamespace(async_delay_save=lambda *args: None)
    conn._store = store
    conn.authenticated = True
    conn._handle_frame(status(1) if initial is None else initial)
    previous = conn.status
    writes: list[bytes] = []
    answers = iter(replies)

    async def write(_client: Any, frame: bytes) -> None:
        writes.append(frame)
        reply = next(answers, None)
        if reply is not None:
            hass.loop.call_soon(conn._handle_frame, reply)

    with (
        patch.object(connection.transport, "async_write", write),
        patch.object(connection, "CONFIRM_TIMEOUT", 0.002),
    ):
        try:
            await conn.async_set_custom_sleep(on)
        except connection.HomeAssistantError as err:
            r.ok(error is not None and str(err) == error, "exchange error matches")
        else:
            r.ok(error is None, "exchange completed")
    r.ok(writes == expected, "literal exchange frames")
    if replies and all(answer is None or MatStatus(answer).custom_readback for answer in replies):
        r.ok(conn.status is previous, "custom readback does not replace the live status")


async def exchanges() -> None:
    await exchange([status(state=1), status(state=3)], [LEFT_FRAME, RIGHT_FRAME])
    await exchange(
        [status(state=0), status(state=1), status(state=1), status(state=3)],
        [LEFT_FRAME, LEFT_FRAME, RIGHT_FRAME, RIGHT_FRAME],
    )
    await exchange(
        [status(state=1), status(state=1), status(state=1)],
        [LEFT_FRAME, RIGHT_FRAME, RIGHT_FRAME], connection.ERROR_UNCONFIRMED,
    )
    for mode in (1, 2, 3):
        await exchange([], [LEFT_FRAME] * 2, connection.ERROR_UNCONFIRMED, initial=status(mode))
        await exchange([status(mode)], [LEFT_FRAME] * 2, connection.ERROR_UNCONFIRMED)
        await exchange(
            [status(mode), status(state=1), status(state=3)],
            [LEFT_FRAME, LEFT_FRAME, RIGHT_FRAME],
        )
    await exchange([status(6, power=False)], [LEFT_FRAME], connection.ERROR_UNCONFIRMED)
    for mode in (1, 2, 3, 9):
        for heat in (3, 4):
            await exchange([status(mode, heat=heat)], [LEFT_FRAME], connection.ERROR_UNCONFIRMED)
    for moved in (status(1), status(2), status(3), status(6, power=False), status(power=False)):
        await exchange([status(state=1), moved], [LEFT_FRAME, RIGHT_FRAME], connection.ERROR_UNCONFIRMED)
    await exchange([status(state=1, readback=True)], [LEFT_FRAME] * 2, connection.ERROR_UNCONFIRMED)
    await exchange([], [], initial=status(state=1))
    await exchange([], [], on=False)
    await exchange(
        [status(1)], [bytes.fromhex("b28001062002241e1c231f20fefefefefefefe5b")],
        on=False, initial=status(state=3),
    )


r.section("command exchanges")
asyncio.run(exchanges())
sys.exit(r.finish())

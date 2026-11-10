"""Focused reservation exchanges and clocks without third-party dependencies."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from harness import Report
from navien_smarthome.ble_mat import connection
from navien_smarthome.ble_mat.entities import (
    MatModeSensor,
    MatReservationSelect,
    MatReservationTimeSensor,
)

r = Report()
START = datetime(2026, 1, 2, 3, 0, tzinfo=UTC)
HEATING = bytes.fromhex("b20001020302241e1c231e1cfefefe0060120433")
CANCEL = bytes.fromhex("b280060200fe241e1cfe0105fefefe00fefefedc")
OFF_FRAME = bytes.fromhex("b280060000fe241e1cfe0000000000fefefefed8")


def status(mode: int, hours: int = 0, *, power: bool = False) -> bytes:
    raw = bytearray(HEATING)
    raw[2] = mode
    raw[3] = 2 if power else 0
    if mode == 6:
        raw[5] = 0
        raw[9] = 0xFE
        raw[10] = 1 if hours else 0
        raw[11] = hours
        raw[12] = 7
    elif mode == 2:
        raw[12 if power else 13] = hours
    raw[19] = sum(raw[1:19]) & 255
    return bytes(raw)


def make_connection() -> connection.MatConnection:
    hass: Any = SimpleNamespace(loop=asyncio.get_running_loop())
    entry: Any = SimpleNamespace(
        entry_id="synthetic", options={},
        data={"address": "00:11:22:33:44:55", "mat_id": "a1a2a3a4a5a6"},
    )
    conn = connection.MatConnection(hass, entry)
    conn._store = Mock()
    conn.authenticated = True
    return conn


def check_record(
    conn: connection.MatConnection, set_hours: int | None,
    last: int | None, run_time: datetime | None,
) -> None:
    record = conn.reservation
    assert record is not None
    r.ok((record.set_hours, record.last_reported_hours, record.run_time) == (
        set_hours, last, run_time,
    ), "retained record matches the controlled clock")
    sensor = MatReservationTimeSensor(conn, record.kind == "on")
    r.ok(sensor._attr_native_value == run_time, "the matching timestamp reads the retained time")
    other = MatReservationTimeSensor(conn, record.kind != "on")
    r.ok(other._attr_native_value is None, "the other reservation timestamp has no value")


async def records() -> None:
    clock = Mock(wraps=datetime)
    clock.now.return_value = START
    with patch.object(connection, "datetime", clock):
        for power in (False, True):
            conn = make_connection()
            conn._handle_frame(status(2, 3, power=power))
            check_record(conn, 3, 3, START + timedelta(hours=3))
            r.ok(MatModeSensor(conn)._attr_native_value == (
                "꺼짐 예약" if power else "켜짐 예약"
            ), "mode distinguishes the reservation kind")
            clock.now.return_value = START + timedelta(minutes=50)
            conn._handle_frame(status(2, 2, power=power))
            exact = START + timedelta(hours=2, minutes=50)
            check_record(conn, 3, 2, exact)
            r.ok(MatReservationSelect(conn, not power)._attr_current_option == "3시간",
                 "a countdown keeps the select's initial hours")
            clock.now.return_value = START + timedelta(hours=2)
            conn._handle_frame(status(2, 2, power=power))
            check_record(conn, 3, 2, exact)
            conn._link_lost()
            for on in (False, True):
                r.ok(MatReservationTimeSensor(conn, on)._attr_native_value is None,
                     "a disconnected timestamp has no value")
            conn.authenticated = True
            conn._handle_frame(status(2, 1, power=power))
            check_record(conn, 3, 1, exact)
            saved = conn._payload()
            restored = make_connection()
            restored._store = Mock(async_load=AsyncMock(return_value=saved))
            await restored.async_load()
            r.ok(restored.reservation == conn.reservation, "the whole record reloads")
            for on in (False, True):
                r.ok(MatReservationTimeSensor(restored, on)._attr_native_value is None,
                     "a reloaded record waits for a live status")
            restored._handle_frame(status(2, 1, power=power))
            check_record(restored, 3, 1, exact)
            restored._handle_frame(HEATING)
            r.ok(restored.reservation is None, "a non-reservation status clears the record")
            for on in (False, True):
                r.ok(MatReservationTimeSensor(restored, on)._attr_native_value is None,
                     "neither timestamp has a value without a reservation")
            clock.now.return_value = START

        for unknown in (0, 99, 0xFE):
            conn = make_connection()
            conn._handle_frame(status(2, unknown, power=True))
            check_record(conn, None, None, None)
            clock.now.return_value = START + timedelta(minutes=30)
            conn._handle_frame(status(2, 9, power=True))
            check_record(conn, None, 9, START + timedelta(hours=9, minutes=30))
            select = MatReservationSelect(conn, False)
            r.ok(select._attr_current_option == "설정됨", "unknown initial hours stay configured")
            await select.async_select_option("설정됨")
            conn._handle_frame(status(2, unknown, power=True))
            check_record(conn, None, 9, START + timedelta(hours=9, minutes=30))
            clock.now.return_value = START
            conn = make_connection()
            conn._handle_frame(status(2, 3, power=True))
            conn._handle_frame(status(2, unknown, power=True))
            check_record(conn, 3, 3, START + timedelta(hours=3))
            conn._handle_frame(status(2, 5, power=True))
            check_record(conn, 5, 5, START + timedelta(hours=5))

        conn = make_connection()
        conn._handle_frame(status(2, 5, power=True))
        clock.now.return_value = START + timedelta(hours=2)
        conn._handle_frame(status(2, 5, power=True))
        check_record(conn, 5, 5, START + timedelta(hours=5))

        async def confirm_set(_client: Any, _frame: bytes) -> None:
            asyncio.get_running_loop().call_soon(conn._handle_frame, status(2, 5, power=True))

        with patch.object(connection.transport, "async_write", confirm_set):
            await conn.async_set_off_reservation(5)
        check_record(conn, 5, 5, START + timedelta(hours=7))
        conn._handle_frame(status(6, 3))
        check_record(conn, 3, 3, START + timedelta(hours=5))
        for hours in (1, 2):
            conn._handle_frame(HEATING)
            conn._handle_frame(status(6, hours))
            select = MatReservationSelect(conn, True)
            r.ok(select._attr_current_option == "설정됨", "unselectable initial hours stay readable")
            await select.async_select_option("설정됨")


async def cancel_exchange(during_attempt: int, *, off_reply: bool = True) -> None:
    conn = make_connection()
    conn._handle_frame(status(6, 5))
    writes: list[bytes] = []

    async def write(_client: Any, frame: bytes) -> None:
        writes.append(frame)
        if frame == CANCEL and len(writes) == during_attempt:
            conn._handle_frame(HEATING)
        elif frame == OFF_FRAME and off_reply:
            asyncio.get_running_loop().call_soon(conn._handle_frame, status(6))

    async def confirm(send: Any, received: list[Any]) -> bool:
        # No reply on the first attempt or to an off frame expires without a wall-clock wait.
        await asyncio.sleep(0)
        return any(send.confirmed(item) for item in received)

    with (
        patch.object(connection.transport, "async_write", write),
        patch.object(conn, "_async_confirmed", confirm),
    ):
        try:
            await conn.async_set_on_reservation(None)
        except connection.HomeAssistantError as err:
            r.ok(not off_reply and str(err) == connection.ERROR_UNCONFIRMED,
                 "an unconfirmed off reports the existing error")
        else:
            r.ok(off_reply, "a cancel and off are confirmed")
    r.ok(writes == [CANCEL] * during_attempt + [OFF_FRAME] * (1 if off_reply else 2),
         "cancel receipts during writes stop resends and off retries once")


async def cancel_at_timeout(receipt_attempt: int) -> None:
    conn = make_connection()
    conn._handle_frame(status(6, 5))
    writes: list[bytes] = []
    attempts = 0

    async def write(_client: Any, frame: bytes) -> None:
        writes.append(frame)
        if frame == OFF_FRAME:
            asyncio.get_running_loop().call_soon(conn._handle_frame, status(6))

    original = conn._async_confirmed

    async def confirm(send: Any, received: list[Any]) -> bool:
        nonlocal attempts
        if send.frame == CANCEL:
            attempts += 1
            if attempts == receipt_attempt:
                conn._handle_frame(HEATING)
            return False
        return await original(send, received)

    with (
        patch.object(connection.transport, "async_write", write),
        patch.object(conn, "_async_confirmed", confirm),
    ):
        await conn.async_set_on_reservation(None)
    r.ok(writes == [CANCEL] * receipt_attempt + [OFF_FRAME],
         "a receipt at either timeout confirms cancellation")


async def main() -> None:
    r.section("reservation records and entities")
    await records()
    r.section("cancel command frames")
    await cancel_exchange(1)
    await cancel_exchange(2)
    await cancel_exchange(1, off_reply=False)
    await cancel_at_timeout(1)
    await cancel_at_timeout(2)
    for final in (status(6), status(4, power=True)):
        conn = make_connection()
        conn._handle_frame(status(6, 5))
        writes: list[bytes] = []

        async def write(
            _client: Any, frame: bytes, answer: bytes = final,
            current: connection.MatConnection = conn, sent: list[bytes] = writes,
        ) -> None:
            sent.append(frame)
            asyncio.get_running_loop().call_soon(current._handle_frame, answer)

        with patch.object(connection.transport, "async_write", write):
            await conn.async_set_on_reservation(None)
            await conn.async_set_on_reservation(None)
        r.ok(writes == [CANCEL], "off and unaccepted cancel results need no further frame")


with patch.object(connection, "datetime", Mock(wraps=datetime)) as fixed_clock:
    fixed_clock.now.return_value = START
    asyncio.run(main())
sys.exit(r.finish())

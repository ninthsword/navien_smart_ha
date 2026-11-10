"""Reservation commands and retained times through the real integration entry points."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.navien_smarthome.ble_mat import connection, transport
from custom_components.navien_smarthome.const import DOMAIN

from .test_ble_mat import (
    HEATING,
    OFF_RESERVED,
    OFF_STATUS,
    FakeMat,
    choose,
    confirm_with,
    feed_and_settle,
    make_entry,
    schedule_command,
    settle,
    setup_mat,
    state_of,
    status,
    turn,
    unique,
)
from .test_ble_mat import mat as mat

CANCEL_FRAME = bytes.fromhex("b280060200fe241e1cfe0105fefefe00fefefedc")
OFF_FRAME = bytes.fromhex("b280060000fe241e1cfe0000000000fefefefed8")
START = datetime(2026, 1, 2, 3, 0, tzinfo=UTC)


@dataclass
class ReservationClock:
    now: datetime = START

    def advance(self, *, hours: float = 0, minutes: float = 0) -> None:
        self.now += timedelta(hours=hours, minutes=minutes)


@pytest.fixture(autouse=True)
def reservation_clock(monkeypatch: pytest.MonkeyPatch) -> ReservationClock:
    """Only the integration's UTC clock moves; no test waits for a reservation hour."""
    clock = ReservationClock()
    date = Mock(wraps=datetime)
    date.now.side_effect = lambda _zone: clock.now
    monkeypatch.setattr(connection, "datetime", date)
    return clock


def reservation_status(kind: str, hours: int) -> bytes:
    if kind == "off":
        return status(mode=2, byte12=hours)
    return status(mode=6, flags=0, left=1, right=hours)


def time_state(hass: HomeAssistant) -> Any:
    return state_of(hass, unique(hass, "sensor", "reservation_time"))


def assert_record(
    hass: HomeAssistant, entry: MockConfigEntry, kind: str,
    set_hours: int | None, last_hours: int | None, run_time: datetime | None,
) -> None:
    record = entry.runtime_data.reservation
    assert record is not None
    assert (record.kind, record.set_hours, record.last_reported_hours, record.run_time) == (
        kind, set_hours, last_hours, run_time,
    )
    assert time_state(hass).state == ("unknown" if run_time is None else run_time.isoformat())
    assert time_state(hass).attributes["device_class"] == "timestamp"


@pytest.mark.parametrize("confirmed", [OFF_STATUS, status(mode=4), status(mode=5), status(heat=0)])
async def test_cancel_needs_no_off_frame_in_an_off_or_unaccepted_state(
    hass: HomeAssistant, mat: FakeMat, confirmed: bytes,
) -> None:
    mat.first_status = OFF_RESERVED
    await setup_mat(hass, mat)
    mat.respond = confirm_with(confirmed)
    await schedule_command(hass, "on_cancel")
    assert mat.controls() == [CANCEL_FRAME]
    await schedule_command(hass, "on_cancel")
    assert mat.controls() == [CANCEL_FRAME]


@pytest.mark.parametrize("confirm_attempt", [1, 2])
async def test_cancel_receives_confirmation_during_first_or_resend_write(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch,
    confirm_attempt: int,
) -> None:
    mat.first_status = OFF_RESERVED
    await setup_mat(hass, mat)
    write = mat.write
    attempts = 0

    async def during_write(client: Any, frame: bytes) -> None:
        nonlocal attempts
        if frame == CANCEL_FRAME:
            attempts += 1
            # The unanswered first attempt expires immediately, without real-time waits.
            monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 0 if attempts < confirm_attempt else 3)
            if attempts == confirm_attempt:
                mat.feed(HEATING)
        await write(client, frame)

    monkeypatch.setattr(transport, "async_write", during_write)
    mat.respond = lambda frame: OFF_STATUS if frame == OFF_FRAME else None
    await schedule_command(hass, "on_cancel")
    assert mat.controls() == [CANCEL_FRAME] * confirm_attempt + [OFF_FRAME]
    assert state_of(hass, unique(hass, "switch", "power")).state == "off"


@pytest.mark.parametrize("receipt_attempt", [1, 2])
async def test_cancel_receipt_at_either_confirmation_timeout_stops_the_cancel(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch,
    receipt_attempt: int,
) -> None:
    mat.first_status = OFF_RESERVED
    entry = await setup_mat(hass, mat)
    original = entry.runtime_data._async_confirmed
    attempts = 0

    async def at_timeout(send: Any, received: list[Any]) -> bool:
        nonlocal attempts
        if send.frame == CANCEL_FRAME:
            attempts += 1
            if attempts == receipt_attempt:
                mat.feed(HEATING)
            return False
        return bool(await original(send, received))

    monkeypatch.setattr(entry.runtime_data, "_async_confirmed", at_timeout)
    mat.respond = lambda frame: OFF_STATUS if frame == OFF_FRAME else None
    await schedule_command(hass, "on_cancel")
    assert mat.controls() == [CANCEL_FRAME] * receipt_attempt + [OFF_FRAME]


async def test_cancel_during_every_write_then_unconfirmed_off_resends_once(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mat.first_status = OFF_RESERVED
    await setup_mat(hass, mat)
    mat.during_write = HEATING
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 0)
    with pytest.raises(HomeAssistantError) as raised:
        await schedule_command(hass, "on_cancel")
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert mat.controls() == [CANCEL_FRAME, OFF_FRAME, OFF_FRAME]


async def test_cancel_checks_the_latest_status_before_powering_off(
    hass: HomeAssistant, mat: FakeMat,
) -> None:
    mat.first_status = OFF_RESERVED
    await setup_mat(hass, mat)

    def answer(_frame: bytes) -> None:
        mat.feed(HEATING)
        mat.feed(status(mode=4))

    mat.respond = answer
    await schedule_command(hass, "on_cancel")
    assert mat.controls() == [CANCEL_FRAME]


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (status(mode=2, byte12=5), "꺼짐 예약"),
        (OFF_RESERVED, "켜짐 예약"),
        (status(mode=2, flags=0, byte13=5), "켜짐 예약"),
        (OFF_STATUS, "꺼짐"),
        (HEATING, "난방"),
        (status(mode=3), "수면"),
        (status(mode=4), "살균"),
        (status(mode=5), "오류"),
        (status(mode=7), "물빼기"),
        (status(mode=8), "이온 케어"),
        (status(mode=9), "맞춤 수면"),
    ],
)
async def test_operating_mode_names_and_option_order(
    hass: HomeAssistant, mat: FakeMat, frame: bytes, expected: str,
) -> None:
    mat.first_status = frame
    await setup_mat(hass, mat)
    mode = state_of(hass, unique(hass, "sensor", "mode"))
    assert mode.state == expected
    assert mode.attributes["options"] == [
        "난방", "꺼짐 예약", "켜짐 예약", "수면", "살균", "오류", "꺼짐", "물빼기",
        "이온 케어", "맞춤 수면",
    ]


@pytest.mark.parametrize("kind", ["off", "on"])
async def test_live_decrement_corrects_time_and_preserves_initial_select_hours(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock, kind: str,
) -> None:
    mat.first_status = reservation_status(kind, 3)
    entry = await setup_mat(hass, mat)
    assert_record(hass, entry, kind, 3, 3, START + timedelta(hours=3))
    reservation_clock.advance(minutes=50)
    await feed_and_settle(hass, mat, reservation_status(kind, 2))
    exact = START + timedelta(hours=2, minutes=50)
    assert_record(hass, entry, kind, 3, 2, exact)
    select = state_of(hass, unique(hass, "select", f"{kind}_reservation"))
    assert select.state == "3시간"
    assert "설정됨" not in select.attributes["options"]
    reservation_clock.advance(minutes=10)
    await feed_and_settle(hass, mat, reservation_status(kind, 2))
    assert_record(hass, entry, kind, 3, 2, exact)


async def test_unchanged_off_hours_for_hours_keep_the_time_but_higher_hours_restart(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock,
) -> None:
    mat.first_status = reservation_status("off", 5)
    entry = await setup_mat(hass, mat)
    reservation_clock.advance(hours=2)
    await feed_and_settle(hass, mat, reservation_status("off", 5))
    assert_record(hass, entry, "off", 5, 5, START + timedelta(hours=5))
    reservation_clock.advance(minutes=30)
    await feed_and_settle(hass, mat, reservation_status("off", 6))
    assert_record(hass, entry, "off", 6, 6, START + timedelta(hours=8, minutes=30))


@pytest.mark.parametrize("kind", ["off", "on"])
async def test_first_decrement_after_a_reconnect_keeps_the_known_time(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock, kind: str,
) -> None:
    mat.first_status = reservation_status(kind, 3)
    entry = await setup_mat(hass, mat)
    await turn(hass, "turn_off", unique(hass, "switch", "enabled"))
    assert time_state(hass).state == "unavailable"
    reservation_clock.advance(hours=1, minutes=10)
    mat.first_status = reservation_status(kind, 2)
    await turn(hass, "turn_on", unique(hass, "switch", "enabled"))
    await hass.async_block_till_done()
    await settle()
    assert_record(hass, entry, kind, 3, 2, START + timedelta(hours=3))
    assert state_of(hass, unique(hass, "select", f"{kind}_reservation")).state == "3시간"


@pytest.mark.parametrize("kind", ["off", "on"])
@pytest.mark.parametrize("reported", [5, 9])
async def test_confirmed_set_restarts_from_reported_hours_even_when_unchanged(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock,
    kind: str, reported: int,
) -> None:
    mat.first_status = reservation_status(kind, 5)
    entry = await setup_mat(hass, mat)
    reservation_clock.advance(hours=1)
    mat.respond = confirm_with(reservation_status(kind, reported))
    await schedule_command(hass, f"{kind}_set")
    await hass.async_block_till_done()
    assert_record(hass, entry, kind, reported, reported, START + timedelta(hours=1 + reported))
    assert state_of(hass, unique(hass, "select", f"{kind}_reservation")).state == f"{reported}시간"
    expected = (
        "b28002020002241e1c231e1c05fefefefefefe3a" if kind == "off"
        else "b280060000fe241e1cfe010500fefe1efefefefa"
    )
    assert mat.controls() == [bytes.fromhex(expected)]


async def test_unconfirmed_set_does_not_restart_the_record(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mat.first_status = reservation_status("off", 5)
    entry = await setup_mat(hass, mat)
    reservation_clock.advance(hours=1)
    mat.during_write = reservation_status("off", 5)
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 0)
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED):
        await schedule_command(hass, "off_set")
    assert_record(hass, entry, "off", 5, 5, START + timedelta(hours=5))


async def test_kind_change_starts_a_record_and_no_reservation_clears_it(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock,
) -> None:
    mat.first_status = reservation_status("off", 5)
    entry = await setup_mat(hass, mat)
    reservation_clock.advance(minutes=30)
    await feed_and_settle(hass, mat, reservation_status("on", 3))
    assert_record(hass, entry, "on", 3, 3, START + timedelta(hours=3, minutes=30))
    await feed_and_settle(hass, mat, OFF_STATUS)
    assert entry.runtime_data.reservation is None
    assert time_state(hass).state == "unknown"
    for kind in ("off", "on"):
        select = state_of(hass, unique(hass, "select", f"{kind}_reservation"))
        assert select.state == "해제"
        assert "설정됨" not in select.attributes["options"]


@pytest.mark.parametrize("kind", ["off", "on"])
@pytest.mark.parametrize("unknown", [0, 99, 0xFE])
async def test_unknown_then_valid_hours_fill_the_time_but_keep_unknown_set_hours(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock,
    kind: str, unknown: int,
) -> None:
    mat.first_status = reservation_status(kind, unknown)
    entry = await setup_mat(hass, mat)
    assert_record(hass, entry, kind, None, None, None)
    select = unique(hass, "select", f"{kind}_reservation")
    assert state_of(hass, select).state == "설정됨"
    await choose(hass, select, "설정됨")
    assert mat.controls() == []
    reservation_clock.advance(minutes=30)
    await feed_and_settle(hass, mat, reservation_status(kind, 9))
    assert_record(hass, entry, kind, None, 9, START + timedelta(hours=9, minutes=30))
    assert state_of(hass, select).state == "설정됨"
    reservation_clock.advance(minutes=30)
    await feed_and_settle(hass, mat, reservation_status(kind, unknown))
    assert_record(hass, entry, kind, None, 9, START + timedelta(hours=9, minutes=30))


@pytest.mark.parametrize("unknown", [0, 99, 0xFE])
async def test_unknown_count_preserves_the_last_valid_count_for_a_later_increase(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock, unknown: int,
) -> None:
    mat.first_status = reservation_status("on", 3)
    entry = await setup_mat(hass, mat)
    reservation_clock.advance(minutes=10)
    await feed_and_settle(hass, mat, reservation_status("on", unknown))
    assert_record(hass, entry, "on", 3, 3, START + timedelta(hours=3))
    reservation_clock.advance(minutes=10)
    await feed_and_settle(hass, mat, reservation_status("on", 5))
    assert_record(hass, entry, "on", 5, 5, START + timedelta(hours=5, minutes=20))
    assert state_of(hass, unique(hass, "select", "on_reservation")).state == "5시간"


@pytest.mark.parametrize("kind", ["off", "on"])
@pytest.mark.parametrize("hours", [1, 2, 13, 14])
async def test_unselectable_initial_hours_show_a_temporary_option_that_sends_nothing(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock,
    kind: str, hours: int,
) -> None:
    mat.first_status = reservation_status(kind, hours)
    entry = await setup_mat(hass, mat)
    assert_record(hass, entry, kind, hours, hours, START + timedelta(hours=hours))
    select = unique(hass, "select", f"{kind}_reservation")
    assert state_of(hass, select).state == "설정됨"
    assert state_of(hass, select).attributes["options"] == [
        "해제", *[f"{h}시간" for h in range(3, 13)], "설정됨",
    ]
    await choose(hass, select, "설정됨")
    assert mat.controls() == []
    await feed_and_settle(hass, mat, HEATING)
    assert "설정됨" not in state_of(hass, select).attributes["options"]


@pytest.mark.parametrize("hours", [1, 2, 14])
async def test_model_three_off_reservation_keeps_its_wider_command_range(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock, hours: int,
) -> None:
    mat.first_status = status(mode=2, model=0x13, byte12=hours)
    await setup_mat(hass, mat)
    select = state_of(hass, unique(hass, "select", "off_reservation"))
    assert select.state == f"{hours}시간"
    assert select.attributes["options"] == ["해제", *[f"{h}시간" for h in range(1, 15)]]


@pytest.mark.parametrize(("kind", "hours"), [("on", 2), ("on", 5), ("off", 4)])
async def test_whole_record_survives_reload_until_the_first_status_applies_the_rule(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock,
    hass_storage: dict[str, Any], kind: str, hours: int,
) -> None:
    mat.first_status = reservation_status("on", 3)
    entry = await setup_mat(hass, mat)
    reservation_clock.advance(minutes=50)
    await feed_and_settle(hass, mat, reservation_status("on", 2))
    exact = START + timedelta(hours=2, minutes=50)
    assert await hass.config_entries.async_unload(entry.entry_id)
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    assert hass_storage[key]["data"]["reservation"] == {
        "kind": "on", "set_hours": 3, "last_reported_hours": 2, "run_time": exact.isoformat(),
    }
    reservation_clock.advance(minutes=30)
    mat.first_status = None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await settle()
    assert entry.runtime_data.status is None
    assert entry.runtime_data.reservation.run_time == exact
    assert time_state(hass).state == "unavailable"
    await feed_and_settle(hass, mat, reservation_status(kind, hours))
    same = kind == "on" and hours == 2
    assert_record(
        hass, entry, kind, 3 if same else hours, hours,
        exact if same else reservation_clock.now + timedelta(hours=hours),
    )


@pytest.mark.parametrize(
    "stored",
    [
        None, [], {}, {"kind": "on"},
        {"kind": "other", "set_hours": 3, "last_reported_hours": 3, "run_time": None},
        {"kind": "on", "set_hours": True, "last_reported_hours": 3, "run_time": None},
        {"kind": "on", "set_hours": 3, "last_reported_hours": 99, "run_time": None},
        {"kind": "on", "set_hours": 3, "last_reported_hours": 3, "run_time": 3},
        {"kind": "on", "set_hours": 3, "last_reported_hours": 3, "run_time": "bad time"},
        {"kind": "on", "set_hours": 3, "last_reported_hours": 3, "run_time": "2026-01-02T03:00:00"},
    ],
)
async def test_absent_or_malformed_reservation_is_ignored_before_first_status(
    hass: HomeAssistant, mat: FakeMat, reservation_clock: ReservationClock,
    hass_storage: dict[str, Any], stored: Any,
) -> None:
    entry = make_entry(hass)
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1, "minor_version": 1, "key": key, "data": {"reservation": stored},
    }
    mat.first_status = None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await settle()
    assert entry.runtime_data.reservation is None
    assert time_state(hass).state == "unavailable"
    await feed_and_settle(hass, mat, reservation_status("on", 3))
    assert_record(hass, entry, "on", 3, 3, START + timedelta(hours=3))

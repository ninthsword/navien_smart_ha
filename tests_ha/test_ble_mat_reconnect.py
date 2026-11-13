"""Reconnect through the real entry lifecycle, with controlled retry sleeps and time."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.navien_smarthome.ble_mat import connection, transport

from .test_ble_mat import (
    ADDRESS,
    HEATING,
    FakeMat,
    reply,
    settle,
    setup_mat,
    status,
    turn,
    unique,
    until,
)
from .test_ble_mat import mat as mat


async def spend_immediate_retry(entry, mat: FakeMat) -> None:
    """Exercise ordinary fallback only after consuming the one-shot on a real loss."""
    connects = mat.connect_calls
    mat.lose_link()
    await until(lambda: mat.connect_calls == connects + 1 and entry.runtime_data.status is not None)


class RetryWaits:
    """Let each retry sleep finish only when the test advances its monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: asyncio.Queue[tuple[float, asyncio.Future[None]]] = asyncio.Queue()

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waits.put_nowait((delay, done))
        await done
        self.now += delay

    async def next(self, expected: float) -> asyncio.Future[None]:
        async with asyncio.timeout(0.5):
            delay, done = await self.waits.get()
        assert delay == pytest.approx(expected)
        return done

    async def advertise(self, mat: FakeMat, floor: float) -> None:
        (await self.next(floor)).set_result(None)
        poll = await self.next(connection.ADVERTISEMENT_POLL_INTERVAL)
        assert isinstance(mat.last_advertisement, float)
        mat.last_advertisement += 1
        lookups = mat.device_lookups
        poll.set_result(None)
        await until(lambda: mat.device_lookups > lookups)


@pytest.fixture
def retry_waits(monkeypatch: pytest.MonkeyPatch, mat: FakeMat) -> RetryWaits:
    assert 0 < connection.ADVERTISEMENT_POLL_INTERVAL <= 0.5
    waits = RetryWaits()
    monkeypatch.setattr(connection, "RETRY_DELAY", 30.0)
    monkeypatch.setattr(connection, "RETRY_FLOOR", 1.0)
    monkeypatch.setattr(connection, "ADVERTISEMENT_POLL_INTERVAL", 0.5)
    monkeypatch.setattr(connection, "sleep", waits.sleep)
    monkeypatch.setattr(connection, "monotonic", waits.time)
    return waits


@pytest.mark.parametrize("seen", [None, 12.5])
def test_transport_reads_last_connectable_advertisement(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, seen: float | None
) -> None:
    info = None if seen is None else SimpleNamespace(time=seen)
    last_info = Mock(return_value=info)
    bluetooth = ModuleType("homeassistant.components.bluetooth")
    vars(bluetooth)["async_last_service_info"] = last_info
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", bluetooth)
    assert transport.async_last_advertisement_time(hass, ADDRESS) == seen
    last_info.assert_called_once_with(hass, ADDRESS, connectable=True)


async def test_lost_link_waits_for_floor_then_new_advertisement(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits
) -> None:
    entry = await setup_mat(hass, mat)
    await spend_immediate_retry(entry, mat)
    mat.lose_link()
    floor = await retry_waits.next(1.0)
    mat.last_advertisement = 2.0
    await settle()
    assert mat.connect_calls == 2
    assert mat.advertisement_reads == 0

    floor.set_result(None)
    poll = await retry_waits.next(0.5)
    assert mat.connect_calls == 2
    assert mat.advertisement_reads == 1
    mat.last_advertisement = 3.0
    poll.set_result(None)
    await until(lambda: entry.runtime_data.status is not None)
    assert mat.connect_calls == 3
    assert retry_waits.now == 1.5
    assert connection.RETRY_DELAY == 30.0


@pytest.mark.parametrize("seen", [None, 1.0, RuntimeError("history unavailable")])
async def test_retry_deadline_with_absent_unchanged_or_failing_history(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits,
    seen: float | Exception | None,
) -> None:
    entry = await setup_mat(hass, mat)
    await spend_immediate_retry(entry, mat)
    mat.last_advertisement = seen
    mat.lose_link()
    (await retry_waits.next(1.0)).set_result(None)
    for _ in range(58):
        poll = await retry_waits.next(0.5)
        assert mat.connect_calls == 2
        poll.set_result(None)
    await until(lambda: entry.runtime_data.status is not None)
    assert mat.connect_calls == 3
    assert retry_waits.now == 30.0


@pytest.mark.parametrize(
    ("delay", "floors"),
    [(30.0, [1, 2, 4, 8, 16, 30, 30]), (5.0, [1, 2, 4, 5, 5]), (0.5, [0.5, 0.5])],
)
async def test_consecutive_failure_floors_and_retry_delay_limit(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits,
    monkeypatch: pytest.MonkeyPatch, delay: float, floors: list[float],
) -> None:
    monkeypatch.setattr(connection, "RETRY_DELAY", delay)
    entry = await setup_mat(hass, mat)
    await spend_immediate_retry(entry, mat)
    mat.connect_error = OSError("connect failed")
    mat.lose_link()
    for attempts, floor in enumerate(floors, start=2):
        done = await retry_waits.next(floor)
        assert mat.connect_calls == attempts
        if attempts == len(floors) + 1:
            break
        reads = mat.advertisement_reads
        done.set_result(None)
        if floor < delay:
            poll = await retry_waits.next(0.5)
            assert isinstance(mat.last_advertisement, float)
            mat.last_advertisement += 1
            poll.set_result(None)
        else:
            await until(lambda expected=attempts + 1: mat.connect_calls == expected)
            assert mat.advertisement_reads == reads
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_authentication_resets_the_failure_floor(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits
) -> None:
    mat.connect_error = OSError("connect failed")
    entry = await setup_mat(hass, mat, wait_status=False)
    await retry_waits.advertise(mat, 2.0)
    mat.connect_error = None
    await retry_waits.advertise(mat, 4.0)
    await until(lambda: entry.runtime_data.status is not None)
    await spend_immediate_retry(entry, mat)
    mat.lose_link()
    floor = await retry_waits.next(1.0)
    assert mat.connect_calls == 4
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert floor.cancelled()


async def test_device_not_found_resets_floor_and_none_to_value_reconnects(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits
) -> None:
    mat.connect_error = OSError("connect failed")
    entry = await setup_mat(hass, mat, wait_status=False)
    await retry_waits.advertise(mat, 2.0)
    mat.visible = False
    await retry_waits.advertise(mat, 4.0)
    floor = await retry_waits.next(1.0)
    assert mat.device_lookups == 3
    assert mat.connect_calls == 2
    mat.last_advertisement = None
    floor.set_result(None)
    poll = await retry_waits.next(0.5)
    assert mat.device_lookups == 3
    mat.visible = True
    mat.connect_error = None
    mat.last_advertisement = 5.0
    poll.set_result(None)
    await until(lambda: entry.runtime_data.status is not None)
    assert mat.device_lookups == 4
    assert mat.connect_calls == 3


async def test_refusal_waits_full_delay_and_preserves_failure_floor(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits
) -> None:
    mat.connect_error = OSError("connect failed")
    entry = await setup_mat(hass, mat, wait_status=False)
    mat.connect_error = None
    mat.auth_replies = [reply(0)]
    await retry_waits.advertise(mat, 2.0)
    refusal = await retry_waits.next(30.0)
    reads = mat.advertisement_reads
    mat.last_advertisement = 10.0
    await settle()
    assert mat.connect_calls == 2
    assert mat.advertisement_reads == reads
    mat.auth_replies = [reply(1)]
    refusal.set_result(None)
    floor = await retry_waits.next(4.0)
    assert mat.connect_calls == 3
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert floor.cancelled()


@pytest.mark.parametrize("failed_baseline", [True, False])
async def test_failed_read_uses_the_first_successful_baseline(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits, failed_baseline: bool
) -> None:
    entry = await setup_mat(hass, mat)
    await spend_immediate_retry(entry, mat)
    mat.last_advertisement = RuntimeError("history unavailable") if failed_baseline else 1.0
    mat.lose_link()
    (await retry_waits.next(1.0)).set_result(None)
    poll = await retry_waits.next(0.5)
    mat.last_advertisement = 1.0 if failed_baseline else RuntimeError("history unavailable")
    poll.set_result(None)
    poll = await retry_waits.next(0.5)
    assert mat.connect_calls == 2
    mat.last_advertisement = 1.0
    poll.set_result(None)
    poll = await retry_waits.next(0.5)
    assert mat.connect_calls == 2
    mat.last_advertisement = 2.0
    poll.set_result(None)
    await until(lambda: entry.runtime_data.status is not None)
    assert mat.connect_calls == 3


@pytest.mark.parametrize("phase", ["floor", "advertisement", "refusal"])
@pytest.mark.parametrize("stop", ["unload", "switch"])
async def test_stop_cancels_retry_wait(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits, phase: str, stop: str
) -> None:
    if phase == "refusal":
        mat.auth_replies = [reply(0)]
    entry = await setup_mat(hass, mat, wait_status=phase != "refusal")
    if phase == "refusal":
        pending = await retry_waits.next(30.0)
    else:
        await spend_immediate_retry(entry, mat)
        mat.lose_link()
        pending = await retry_waits.next(1.0)
        if phase == "advertisement":
            pending.set_result(None)
            pending = await retry_waits.next(0.5)
    if stop == "unload":
        assert await hass.config_entries.async_unload(entry.entry_id)
    else:
        await turn(hass, "turn_off", unique(hass, "switch", "enabled"))
        assert entry.runtime_data.enabled is False
    assert pending.cancelled()
    mat.last_advertisement = 20.0
    await settle()
    assert mat.connect_calls == (1 if phase == "refusal" else 2)
    assert mat.disconnected == mat.clients


@pytest.mark.parametrize("scenario", [
    "failed_immediate_attempt", "next_authenticated_flap", "initial_failure",
    "auth_only_loss", "silence_timeout", "cleanup_only",
])
async def test_recovery_retry_fallback(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits,
    monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    if scenario == "initial_failure":
        mat.connect_error = OSError("synthetic failure")
    if scenario == "auth_only_loss":
        mat.first_status = None
    if scenario == "silence_timeout":
        monkeypatch.setattr(connection, "SILENCE_TIMEOUT", 0.02)
    entry = await setup_mat(hass, mat, wait_status=scenario not in ("initial_failure", "auth_only_loss"))
    conn = entry.runtime_data
    if scenario == "failed_immediate_attempt":
        mat.connect_error = OSError("synthetic immediate failure")
        mat.lose_link()
        await retry_waits.next(2.0)
        assert mat.connect_calls == 2
    elif scenario == "next_authenticated_flap":
        await spend_immediate_retry(entry, mat)
        mat.lose_link()
        await retry_waits.next(1.0)
        assert mat.connect_calls == 2
        assert conn.display_status is not None
    elif scenario == "initial_failure":
        await retry_waits.next(2.0)
        assert mat.connect_calls == 1
    else:
        if scenario == "auth_only_loss":
            await until(lambda: conn.authenticated)
            mat.lose_link()
        elif scenario == "cleanup_only":
            conn._link_lost()
        await retry_waits.next(1.0)
        assert mat.connect_calls == 1
        assert conn.display_status is None
    if scenario not in ("failed_immediate_attempt", "next_authenticated_flap"):
        assert conn._immediate_retry_available
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("scenario", [
    "at_59_999", "at_60", "duplicate_ordinary_status", "auth_only",
    "elapsed_without_status", "advertisement", "invalid_frame", "custom_readback",
    "loss_resets_origin", "disable_enable_no_reset",
])
async def test_recovery_budget_requires_stable_status(
    hass: HomeAssistant, mat: FakeMat, retry_waits: RetryWaits, scenario: str,
) -> None:
    entry = await setup_mat(hass, mat)
    await spend_immediate_retry(entry, mat)
    conn = entry.runtime_data
    assert not conn._immediate_retry_available
    retry_waits.now = 59.999 if scenario == "at_59_999" else 60.0
    if scenario in ("at_59_999", "at_60", "duplicate_ordinary_status"):
        mat.feed(status(left=31) if scenario != "duplicate_ordinary_status" else HEATING)
    elif scenario == "auth_only":
        mat.first_status = None
        mat.lose_link()
        await retry_waits.advertise(mat, 1.0)
        await until(lambda: conn.authenticated)
        assert conn.status is None
        retry_waits.now += 60.0
        mat.feed(reply(2))
    elif scenario == "advertisement":
        mat.last_advertisement = 20.0
    elif scenario == "invalid_frame":
        mat.feed(b"invalid")
    elif scenario == "custom_readback":
        mat.feed(status(mode=9, range=0xFF))
    elif scenario == "loss_resets_origin":
        retry_waits.now = 59.0
        mat.lose_link()
        await retry_waits.advertise(mat, 1.0)
        await until(lambda: conn.status is not None)
        retry_waits.now = 61.0
        mat.feed(HEATING)
    elif scenario == "disable_enable_no_reset":
        await turn(hass, "turn_off", unique(hass, "switch", "enabled"))
        await turn(hass, "turn_on", unique(hass, "switch", "enabled"))
        await until(lambda: conn.status is not None)
        mat.feed(HEATING)
    connects = mat.connect_calls
    mat.lose_link()
    if scenario in ("at_60", "duplicate_ordinary_status"):
        await until(lambda: mat.connect_calls == connects + 1 and conn.status is not None)
        assert retry_waits.waits.empty()
    else:
        await retry_waits.next(1.0)
        assert mat.connect_calls == connects
    assert await hass.config_entries.async_unload(entry.entry_id)

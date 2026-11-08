"""The Bluetooth mat through Home Assistant's own services, with transport.py replaced."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.navien_smarthome.ble_mat import connection, transport
from custom_components.navien_smarthome.ble_mat.connection import (
    CONF_MAT_ID,
    LINK_CONNECTED,
    LINK_CONNECTING,
    LINK_DISABLED,
    LINK_NEEDS_REGISTRATION,
)
from custom_components.navien_smarthome.const import DOMAIN
from custom_components.navien_smarthome.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import entity_id_for_unique_id

ADDRESS = "00:11:22:33:44:55"
PREFIX = "001122334455"
MAT_ID = "a1a2a3a4a5a6"
HEATING = bytes.fromhex("b20001020302241e1c231e1cfefefe0060120433")


def checksum(raw: bytearray) -> bytes:
    raw[19] = sum(raw[1:19]) & 0xFF
    return bytes(raw)


def status(**fields: int) -> bytes:
    """Build a status frame from the heating example, naming only what differs."""
    raw = bytearray(HEATING)
    indexes = {
        "mode": 2,
        "flags": 3,
        "volume": 4,
        "heat": 5,
        "left": 10,
        "right": 11,
        "byte9": 9,
    }
    for name, value in fields.items():
        raw[indexes[name]] = value
    return checksum(raw)


def reply(code: int, issued: bytes = bytes.fromhex(MAT_ID)) -> bytes:
    raw = bytearray([0xFE]) * 20
    raw[0] = 0xB2
    raw[1] = 0xF1
    raw[2] = code
    raw[3:9] = issued
    return checksum(raw)


OFF_STATUS = status(mode=6, flags=0, heat=0, left=0, right=0, byte9=0xFE)
SLEEP = status(mode=3)


class FakeMat:
    """Replaces transport.py: records writes and answers like a mat."""

    def __init__(self) -> None:
        self.visible = True
        self.connect_error: Exception | None = None
        self.write_error: list[Exception] = []
        self.missing_characteristics = False
        self.auth_replies: list[bytes | None] = [reply(2)]
        self.register_replies: list[bytes | None] = [reply(1), reply(2)]
        self.first_status: bytes | None = HEATING
        self.status_before_reply2: bytes | None = None
        self.during_write: bytes | None = None
        self.write_hangs = 0
        self.discovered: list[transport.DiscoveredMat] | Exception = []
        self.respond: Callable[[bytes], bytes | None] | None = None
        self.writes: list[bytes] = []
        self.attempts: list[bytes] = []
        self.clients: list[object] = []
        self.disconnected: list[object] = []
        self.cache_clears = 0
        self.connect_calls = 0
        self.on_frame: Callable[[bytes], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None

    def controls(self) -> list[bytes]:
        return [frame for frame in self.writes if frame[1] == 0x80]

    def feed(self, frame: bytes) -> None:
        assert self.on_frame is not None
        self.on_frame(frame)

    def lose_link(self) -> None:
        assert self.on_disconnect is not None
        self.on_disconnect()

    def _answer(self, frame: bytes | None) -> None:
        if frame is not None:
            asyncio.get_running_loop().call_soon(self.feed, frame)

    # The functions of transport.py.
    def discovered_mats(self, hass: HomeAssistant) -> list[transport.DiscoveredMat]:
        if isinstance(self.discovered, Exception):
            raise self.discovered
        return self.discovered

    def ble_device(self, hass: HomeAssistant, address: str) -> Any:
        return object() if self.visible else None

    async def connect(self, device: Any, on_disconnect: Callable[[], None]) -> Any:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        client = object()
        self.clients.append(client)
        self.on_disconnect = on_disconnect
        return client

    async def start_notify(
        self, client: Any, on_frame: Callable[[bytes], None]
    ) -> None:
        if self.missing_characteristics:
            raise transport.MissingCharacteristic
        self.on_frame = on_frame

    async def write(self, client: Any, frame: bytes) -> None:
        self.attempts.append(frame)
        if self.during_write is not None and frame[1] == 0x80:
            self.feed(self.during_write)
        if frame[1] == 0x80 and self.write_hangs:
            self.write_hangs -= 1
            await asyncio.Event().wait()
        if self.write_error:
            raise self.write_error.pop(0)
        self.writes.append(frame)
        if frame[1] == 0x01:
            if frame[2] == 0xFE:
                self._answer(self.register_replies.pop(0))
                return
            self._answer(self.status_before_reply2)
            queue = self.auth_replies
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            self._answer(answer)
            if answer == reply(2):
                self._answer(self.first_status)
            return
        if self.respond is not None:
            self._answer(self.respond(frame))

    async def clear_cache(self, client: Any) -> None:
        self.cache_clears += 1

    async def disconnect(self, client: Any) -> None:
        self.disconnected.append(client)


@pytest.fixture
def mat(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMat]:
    fake = FakeMat()
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 0.05)
    monkeypatch.setattr(connection, "SEND_TIMEOUT", 0.02)
    monkeypatch.setattr(connection, "RETRY_DELAY", 0.01)
    monkeypatch.setattr(connection, "REPLY_TIMEOUT", 0.05)
    monkeypatch.setattr(connection, "FIRST_STATUS_TIMEOUT", 0.05)
    with patch.multiple(
        transport,
        async_discovered_mats=fake.discovered_mats,
        async_ble_device=fake.ble_device,
        async_connect=fake.connect,
        async_start_notify=fake.start_notify,
        async_write=fake.write,
        async_clear_cache=fake.clear_cache,
        async_disconnect=fake.disconnect,
    ):
        yield fake


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


def make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="매트",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_MAT_ID: MAT_ID},
    )
    entry.add_to_hass(hass)
    return entry


async def setup_mat(
    hass: HomeAssistant, mat: FakeMat, wait_status: bool = True
) -> MockConfigEntry:
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if wait_status:
        await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    return entry


def climate_id(hass: HomeAssistant, side: str) -> str:
    return entity_id_for_unique_id(hass, "climate", f"{PREFIX}_{side}_thermostat")


def state_of(hass: HomeAssistant, entity_id: str) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


def assert_status_entities_unavailable(hass: HomeAssistant) -> None:
    """Both thermostats, 운전 모드 and 오류 wait for a live status; the others do not."""
    for side in ("left", "right"):
        assert state_of(hass, climate_id(hass, side)).state == "unavailable"
    mode = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_mode")
    error = entity_id_for_unique_id(hass, "binary_sensor", f"{PREFIX}_error")
    assert state_of(hass, mode).state == "unavailable"
    assert state_of(hass, error).state == "unavailable"


async def set_temp(
    hass: HomeAssistant, side: str, value: float, hvac_mode: str | None = None
) -> None:
    data: dict[str, Any] = {"entity_id": climate_id(hass, side), "temperature": value}
    if hvac_mode is not None:
        data["hvac_mode"] = hvac_mode
    await hass.services.async_call("climate", "set_temperature", data, blocking=True)


async def set_mode(hass: HomeAssistant, side: str, mode: str) -> None:
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": climate_id(hass, side), "hvac_mode": mode},
        blocking=True,
    )


async def feed_and_settle(hass: HomeAssistant, mat: FakeMat, frame: bytes) -> None:
    mat.feed(frame)
    await hass.async_block_till_done()


def confirm_with(frame: bytes) -> Callable[[bytes], bytes]:
    return lambda _sent: frame


# Availability and state.


async def test_entities_exist_and_are_unavailable_before_first_status(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = None
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.authenticated)
    await hass.async_block_till_done()
    for side in ("left", "right"):
        assert state_of(hass, climate_id(hass, side)).state == "unavailable"
    mode = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_mode")
    error = entity_id_for_unique_id(hass, "binary_sensor", f"{PREFIX}_error")
    link = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_link")
    switch = entity_id_for_unique_id(hass, "switch", f"{PREFIX}_enabled")
    assert state_of(hass, mode).state == "unavailable"
    assert state_of(hass, error).state == "unavailable"
    assert state_of(hass, link).state == LINK_CONNECTED
    assert state_of(hass, switch).state == "on"

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await until(lambda: entry.runtime_data.authenticated)
    await hass.async_block_till_done()
    assert_status_entities_unavailable(hass)


async def test_heating_off_and_other_states(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    left = state_of(hass, climate_id(hass, "left"))
    assert left.state == "heat"
    assert left.attributes["hvac_action"] == "heating"
    assert left.attributes["current_temperature"] == 30
    assert left.attributes["temperature"] == 30
    assert left.attributes["min_temp"] == 28
    assert left.attributes["max_temp"] == 45

    await feed_and_settle(hass, mat, OFF_STATUS)
    off = state_of(hass, climate_id(hass, "left"))
    assert off.state == "off"
    assert off.attributes["hvac_action"] == "off"
    assert off.attributes["temperature"] is None

    await feed_and_settle(hass, mat, status(heat=3))
    assert state_of(hass, climate_id(hass, "left")).state == "heat"
    assert state_of(hass, climate_id(hass, "right")).state == "off"

    await feed_and_settle(hass, mat, status(heat=4))
    assert state_of(hass, climate_id(hass, "left")).state == "off"

    mode = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_mode")
    error = entity_id_for_unique_id(hass, "binary_sensor", f"{PREFIX}_error")
    await feed_and_settle(hass, mat, status(mode=3))
    assert state_of(hass, mode).state == "수면"
    assert state_of(hass, error).state == "off"
    await feed_and_settle(hass, mat, status(mode=5))
    assert state_of(hass, error).state == "on"
    await feed_and_settle(hass, mat, status(mode=0x20))
    assert state_of(hass, mode).state == "unknown"


async def test_reconnect_needs_a_fresh_status(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_mat(hass, mat)
    mat.first_status = None
    sent_before = len(mat.controls())
    mat.lose_link()
    await hass.async_block_till_done()
    assert_status_entities_unavailable(hass)
    await set_temp(hass, "left", 31)  # raises nothing and sends nothing
    assert len(mat.controls()) == sent_before

    await until(lambda: entry.runtime_data.authenticated)
    await hass.async_block_till_done()
    assert_status_entities_unavailable(hass)
    await set_temp(hass, "left", 31)
    assert len(mat.controls()) == sent_before

    await feed_and_settle(hass, mat, status(left=33))
    assert state_of(hass, climate_id(hass, "left")).state == "heat"
    mat.respond = confirm_with(status(left=33, right=29))
    await set_temp(hass, "right", 29)
    assert mat.controls()[-1][10:12] == bytes([33, 29])


# Commands.


async def test_temperature_exact_frame_and_confirmation(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.respond = confirm_with(status(right=29))
    await set_temp(hass, "right", 29)
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c231e1dfefefefefefefe33"
    ]


async def test_resend_then_confirmed(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    answers = iter([None, status(right=29)])
    mat.respond = lambda _frame: next(answers)
    await set_temp(hass, "right", 29)
    assert len(mat.controls()) == 2


async def test_unconfirmed_raises_after_one_resend(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "right", 29)
    assert not isinstance(raised.value, ServiceValidationError)
    assert len(mat.controls()) == 2


async def test_off_exact_frame_resend_and_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await set_mode(hass, "left", "off")
    assert not isinstance(raised.value, ServiceValidationError)
    assert [frame.hex() for frame in mat.controls()] == [
        "b280060000fe241e1cfe0000000000fefefefed8"
    ] * 2


async def test_heat_resend_and_final_error(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    answers = iter([None, HEATING])
    mat.respond = lambda _frame: next(answers)
    await set_mode(hass, "left", "heat")
    assert len(mat.controls()) == 2
    assert mat.controls()[0][2] == 1 and mat.controls()[0][10:12] == bytes([30, 28])

    await feed_and_settle(hass, mat, OFF_STATUS)
    mat.respond = None
    with pytest.raises(HomeAssistantError) as raised:
        await set_mode(hass, "left", "heat")
    assert not isinstance(raised.value, ServiceValidationError)
    assert len(mat.controls()) == 4


async def test_heat_resends_when_heating_at_other_targets(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    answers = iter([status(left=31, right=29), HEATING])
    mat.respond = lambda _frame: next(answers)
    await set_mode(hass, "left", "heat")
    assert len(mat.controls()) == 2
    assert [frame[10:12] for frame in mat.controls()] == [bytes([30, 28])] * 2


async def test_heat_fails_when_only_other_targets_arrive(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    mat.respond = confirm_with(status(left=31, right=29))
    with pytest.raises(HomeAssistantError) as raised:
        await set_mode(hass, "left", "heat")
    assert not isinstance(raised.value, ServiceValidationError)
    assert [frame[10:12] for frame in mat.controls()] == [bytes([30, 28])] * 2


async def test_failed_send_counts_as_unconfirmed(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.write_error = [OSError("dying link")]
    mat.respond = confirm_with(status(right=29))
    await set_temp(hass, "right", 29)
    assert len(mat.controls()) == 1

    mat.respond = None
    mat.write_error = [OSError("dying link"), OSError("dying link")]
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "right", 30)
    assert "dying" not in str(raised.value)


async def test_write_raises_resends_then_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.write_error = [OSError("dying link"), OSError("dying link")]
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "right", 29)
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert [frame.hex() for frame in mat.attempts if frame[1] == 0x80] == [
        "b28001020002241e1c231e1dfefefefefefefe33"
    ] * 2
    assert mat.controls() == []


async def test_write_hangs_resends_then_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.write_hangs = 2
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "right", 29)
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert [frame.hex() for frame in mat.attempts if frame[1] == 0x80] == [
        "b28001020002241e1c231e1dfefefefefefefe33"
    ] * 2
    assert mat.controls() == []


async def test_write_hangs_once_then_confirmed(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.write_hangs = 1
    mat.respond = confirm_with(status(right=29))
    await set_temp(hass, "right", 29)
    assert len([frame for frame in mat.attempts if frame[1] == 0x80]) == 2
    assert len(mat.controls()) == 1


async def test_same_target_without_later_status_is_unconfirmed(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "left", 30)  # the held status already shows 30 and 28
    assert not isinstance(raised.value, ServiceValidationError)
    assert len(mat.controls()) == 2


async def test_identical_status_after_the_send_confirms(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.respond = confirm_with(HEATING)
    await set_temp(hass, "left", 30)
    assert len(mat.controls()) == 1


async def test_status_received_during_the_write_does_not_confirm(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.during_write = status(right=29)
    with pytest.raises(HomeAssistantError):
        await set_temp(hass, "right", 29)
    assert len(mat.controls()) == 2


async def test_link_lost_during_confirmation(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 5.0)
    await setup_mat(hass, mat)
    first = hass.async_create_task(set_temp(hass, "right", 29))
    await until(lambda: len(mat.controls()) == 1)
    waiting = hass.async_create_task(set_temp(hass, "left", 31))
    await asyncio.sleep(0.05)  # the second call is now behind the lock
    mat.lose_link()
    with pytest.raises(HomeAssistantError) as one:
        await first
    assert not isinstance(one.value, ServiceValidationError)
    with pytest.raises(HomeAssistantError) as two:
        await waiting
    assert not isinstance(two.value, ServiceValidationError)
    assert len(mat.controls()) == 1


async def test_callbacks_after_a_lost_link_are_ignored(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "REPLY_TIMEOUT", 30.0)
    monkeypatch.setattr(connection, "RETRY_DELAY", 30.0)
    mat.auth_replies = [None]
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: len(mat.writes) == 1)
    stale = mat.on_frame
    assert stale is not None
    conn = entry.runtime_data

    mat.lose_link()
    stale(reply(2))
    stale(HEATING)
    assert conn.authenticated is False
    assert conn.status is None

    # The attempt ends at once rather than after REPLY_TIMEOUT.
    await until(lambda: mat.disconnected == mat.clients)
    assert conn.authenticated is False
    assert conn.status is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_status_after_a_lost_link_is_ignored(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "RETRY_DELAY", 30.0)
    entry = await setup_mat(hass, mat)
    stale = mat.on_frame
    assert stale is not None
    mat.lose_link()
    stale(status(left=33))
    assert entry.runtime_data.status is None
    assert entry.runtime_data.authenticated is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_two_temperatures_in_a_row_both_applied(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_temp(hass, "left", 33)
    await set_temp(hass, "right", 34)
    assert mat.controls()[-1][10:12] == bytes([33, 34])


async def test_rounding_half_up(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_temp(hass, "left", 32.5)
    assert mat.controls()[-1][10] == 33
    await set_temp(hass, "left", 31.4)
    assert mat.controls()[-1][10] == 31


@pytest.mark.parametrize("frame", [SLEEP, status(heat=3), status(heat=4)])
async def test_other_state_refused_from_both_sides(
    hass: HomeAssistant, mat: FakeMat, frame: bytes
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, frame)
    for side in ("left", "right"):
        with pytest.raises(ServiceValidationError):
            await set_temp(hass, side, 33)
        with pytest.raises(ServiceValidationError):
            await set_temp(hass, side, 33, "off")
        with pytest.raises(ServiceValidationError):
            await set_mode(hass, side, "off")
    assert mat.controls() == []


async def test_temperature_alone_refused_while_off(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    with pytest.raises(ServiceValidationError):
        await set_temp(hass, "left", 33)
    assert mat.controls() == []


async def test_temperature_with_hvac_mode_while_heating(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_temp(hass, "left", 33, "heat")
    assert mat.controls()[-1][2] == 1
    assert mat.controls()[-1][10] == 33

    mat.respond = confirm_with(OFF_STATUS)
    await set_temp(hass, "left", 35, "off")
    assert mat.controls()[-1][2] == 6


async def test_temperature_with_hvac_mode_while_off(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    await set_temp(hass, "left", 33, "off")
    assert mat.controls() == []  # already off

    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_temp(hass, "left", 33, "heat")
    assert mat.controls()[-1][10:12] == bytes([33, 28])
    assert mat.controls()[-1][2] == 1


async def test_off_and_heat_frames_and_idempotence(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await set_mode(hass, "left", "heat")
    assert mat.controls() == []  # already heating

    mat.respond = confirm_with(OFF_STATUS)
    await set_mode(hass, "right", "off")
    assert mat.controls()[-1].hex().startswith("b280060000fe241e1cfe0000000000")

    await hass.async_block_till_done()
    sent = len(mat.controls())
    await set_mode(hass, "left", "off")
    assert len(mat.controls()) == sent  # already off

    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_mode(hass, "left", "heat")
    assert mat.controls()[-1][2] == 1
    assert mat.controls()[-1][10:12] == bytes([30, 28])


async def test_power_on_mixes_latest_and_stored(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(byte9=0x21, volume=0x20)
    await setup_mat(hass, mat)
    await feed_and_settle(
        hass, mat, status(mode=6, flags=0x04, volume=0x13, heat=0, left=0, right=0)
    )
    mat.respond = confirm_with(status(left=30, right=28, flags=0x06, byte9=0x21))
    await set_mode(hass, "left", "heat")
    # Byte 3's lock bit and byte 4 are the latest status's; byte 9 and the targets the stored one's.
    assert mat.controls()[-1].hex() == "b28001061002241e1c211e1cfefefefefefefe44"

    await feed_and_settle(
        hass, mat, status(mode=6, flags=0x04, volume=0x13, heat=0, left=0, right=0)
    )
    mat.respond = confirm_with(status(left=33, right=28, flags=0x06, byte9=0x21))
    await set_temp(hass, "left", 33, "heat")
    assert mat.controls()[-1].hex() == "b28001061002241e1c21211cfefefefefefefe47"


async def test_no_stored_status_refuses_power_on(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = OFF_STATUS
    await setup_mat(hass, mat)
    with pytest.raises(ServiceValidationError):
        await set_mode(hass, "left", "heat")
    with pytest.raises(ServiceValidationError):
        await set_temp(hass, "left", 33, "heat")
    assert mat.controls() == []


async def test_stored_status_survives_reload_and_is_removed(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    remembered = status(byte9=0x21, left=33, right=29)
    mat.first_status = remembered
    entry = await setup_mat(hass, mat)

    mat.first_status = OFF_STATUS
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert entry.runtime_data.status.raw == OFF_STATUS
    assert entry.runtime_data.stored_status.raw == remembered

    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    assert hass_storage[key]["data"]["heating_status"] == remembered.hex()

    mat.respond = confirm_with(remembered)
    await set_mode(hass, "left", "heat")
    frame = mat.controls()[-1]
    assert frame[9] == 0x21
    assert frame[10:12] == bytes([33, 29])

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


@pytest.mark.parametrize(
    "stored",
    [
        "zz",
        12345,
        HEATING.hex()[:-2],
        bytes(20).hex(),
        status(left=0).hex(),
        OFF_STATUS.hex(),
    ],
)
async def test_malformed_store_is_absent(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any], stored: Any
) -> None:
    mat.first_status = OFF_STATUS
    entry = make_entry(hass)
    hass_storage[f"{DOMAIN}.ble_mat.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.ble_mat.{entry.entry_id}",
        "data": {"enabled": True, "heating_status": stored},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.stored_status is None
    with pytest.raises(ServiceValidationError):
        await set_mode(hass, "left", "heat")
    assert mat.controls() == []


async def test_store_holding_no_mapping_is_absent(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    entry = make_entry(hass)
    hass_storage[f"{DOMAIN}.ble_mat.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.ble_mat.{entry.entry_id}",
        "data": ["not", "a", "mapping"],
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.enabled is True


async def test_store_is_written_only_for_what_a_power_on_uses(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_mat(hass, mat)
    conn = entry.runtime_data
    with patch.object(conn._store, "async_delay_save") as save:
        raw = bytearray(HEATING)
        raw[7] = 31  # the left current temperature
        await feed_and_settle(hass, mat, checksum(raw))
        save.assert_not_called()
        assert conn.stored_status.raw == HEATING

        await feed_and_settle(hass, mat, status(left=33))
        save.assert_called_once()
        await feed_and_settle(hass, mat, status(left=33, byte9=0x21))
        assert save.call_count == 2


async def test_switch_and_its_persistence(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    entry = await setup_mat(hass, mat)
    switch = entity_id_for_unique_id(hass, "switch", f"{PREFIX}_enabled")
    link = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_link")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch}, blocking=True
    )
    await hass.async_block_till_done()
    assert mat.disconnected
    assert state_of(hass, switch).state == "off"
    assert state_of(hass, link).state == LINK_DISABLED
    assert_status_entities_unavailable(hass)
    await set_temp(hass, "left", 31)  # skipped by Home Assistant
    assert mat.controls() == []
    assert hass_storage[f"{DOMAIN}.ble_mat.{entry.entry_id}"]["data"]["enabled"] is False

    connects = mat.connect_calls
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await settle()
    assert mat.connect_calls == connects
    assert state_of(hass, switch).state == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch}, blocking=True
    )
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert state_of(hass, link).state == LINK_CONNECTED


# Recovery paths.


async def test_not_seen_then_found(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.visible = False
    entry = await setup_mat(hass, mat, wait_status=False)
    assert entry.state is ConfigEntryState.LOADED
    link = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_link")
    assert state_of(hass, link).state == LINK_CONNECTING
    mat.visible = True
    await until(lambda: entry.runtime_data.status is not None)


async def test_connect_failure_then_retry(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.connect_error = OSError("no route")
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: mat.connect_calls >= 2)
    mat.connect_error = None
    await until(lambda: entry.runtime_data.status is not None)


async def test_no_reply_then_retry(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.auth_replies = [None, reply(2)]
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.status is not None)
    assert len(mat.clients) == 2
    assert mat.disconnected[0] is mat.clients[0]


async def test_lost_link_reconnects(hass: HomeAssistant, mat: FakeMat) -> None:
    entry = await setup_mat(hass, mat)
    mat.lose_link()
    assert entry.runtime_data.status is None
    await until(lambda: entry.runtime_data.status is not None)
    assert len(mat.clients) == 2


async def test_silence_reconnects(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "SILENCE_TIMEOUT", 0.02)
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: len(mat.clients) >= 2)
    assert mat.disconnected[0] is mat.clients[0]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_missing_characteristics_clear_cache_once(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.missing_characteristics = True
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: mat.connect_calls >= 3)
    assert mat.cache_clears == 1
    mat.missing_characteristics = False
    await until(lambda: entry.runtime_data.status is not None)


async def test_unexpected_exception_retries(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.connect_error = RuntimeError("anything")
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: mat.connect_calls >= 2)
    mat.connect_error = None
    await until(lambda: entry.runtime_data.status is not None)


async def test_three_refusals_stop_and_switch_restarts(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.auth_replies = [reply(0)]
    entry = await setup_mat(hass, mat, wait_status=False)
    link = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_link")
    switch = entity_id_for_unique_id(hass, "switch", f"{PREFIX}_enabled")
    await until(lambda: entry.runtime_data.needs_registration)
    await hass.async_block_till_done()
    assert state_of(hass, link).state == LINK_NEEDS_REGISTRATION
    connects = mat.connect_calls
    assert connects == 3
    await asyncio.sleep(0.03)
    assert mat.connect_calls == connects

    mat.auth_replies = [reply(2)]
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch}, blocking=True
    )
    await until(lambda: entry.runtime_data.status is not None)


async def test_only_consecutive_refusals_count(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.auth_replies = [reply(0), reply(0), None, reply(0), reply(0), reply(2)]
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.status is not None)
    assert mat.connect_calls == 6
    assert entry.runtime_data.needs_registration is False


async def test_reload_restarts_after_refusals(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.auth_replies = [reply(0)]
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.needs_registration)
    mat.auth_replies = [reply(2)]
    await hass.config_entries.async_reload(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)


async def test_unload_disconnects(hass: HomeAssistant, mat: FakeMat) -> None:
    entry = await setup_mat(hass, mat)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert mat.disconnected == mat.clients


async def test_no_control_frame_before_authentication(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = None
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.authenticated)
    assert mat.writes[0][1] == 0x01
    assert mat.controls() == []


# Platforms and diagnostics.


async def test_forwards_only_its_four_platforms(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = make_entry(hass)
    with patch.object(
        hass.config_entries, "async_forward_entry_setups", return_value=None
    ) as forward:
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert {str(p) for p in forward.call_args.args[1]} == {
        "binary_sensor",
        "climate",
        "sensor",
        "switch",
    }
    await hass.config_entries.async_unload(entry.entry_id)


async def test_cloud_entry_still_loads_every_platform(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    domains = {entity.domain for entity in hass.states.async_all()}
    assert {"climate", "sensor", "switch", "binary_sensor", "water_heater"} <= domains


async def test_diagnostics_hold_no_address_or_id(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_mat(hass, mat)
    result = await async_get_config_entry_diagnostics(hass, entry)
    text = str(result).lower()
    assert ADDRESS.lower() not in text
    assert PREFIX not in text
    assert MAT_ID not in text


# The Bluetooth config flow.


async def test_reauth_aborts_for_a_bluetooth_entry(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = make_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "bluetooth_reauth_unsupported"


async def _flow_to_bluetooth(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result.get("type") is FlowResultType.MENU
    return dict(
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "bluetooth_mat"}
        )
    )


async def _flow_to_pair(hass: HomeAssistant, mat: FakeMat) -> dict[str, Any]:
    mat.discovered = [transport.DiscoveredMat(ADDRESS, "매트")]
    result = await _flow_to_bluetooth(hass)
    assert result.get("step_id") == "bluetooth_mat"
    result = dict(
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
    )
    assert result.get("step_id") == "bluetooth_mat_pair"
    assert mat.connect_calls == 0
    return result


async def _submit(hass: HomeAssistant, flow_id: str) -> Any:
    return await hass.config_entries.flow.async_configure(flow_id, {})


async def test_flow_no_devices(hass: HomeAssistant, mat: FakeMat) -> None:
    result = await _flow_to_bluetooth(hass)
    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "no_devices"


async def test_flow_bluetooth_unavailable(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.discovered = RuntimeError("no bluetooth")
    result = await _flow_to_bluetooth(hass)
    assert result.get("reason") == "bluetooth_unavailable"


async def test_flow_hides_configured_and_aborts_when_picked(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.discovered = [transport.DiscoveredMat(ADDRESS, "매트")]
    picker = await _flow_to_bluetooth(hass)
    make_entry(hass)
    result = dict(
        await hass.config_entries.flow.async_configure(
            picker["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
    )
    assert result.get("reason") == "already_configured"

    assert (await _flow_to_bluetooth(hass)).get("reason") == "no_devices"


async def test_flow_success_sends_registration_first(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    with patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None):
        done = await _submit(hass, result["flow_id"])
        await hass.async_block_till_done()
    assert done.get("type") is FlowResultType.CREATE_ENTRY
    assert done.get("data") == {CONF_ADDRESS: ADDRESS, CONF_MAT_ID: MAT_ID}
    assert mat.writes[0][1] == 0x01 and mat.writes[0][2:8] == b"\xfe" * 6
    assert mat.writes[1][2:8].hex() == MAT_ID
    assert mat.disconnected[0] is mat.clients[0]


async def test_flow_refusal_keeps_form_and_closes(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.register_replies = [reply(0)]
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_refused"}
    assert mat.disconnected == mat.clients


async def test_flow_timeout_keeps_form(hass: HomeAssistant, mat: FakeMat) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.register_replies = [None]
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_connect_failed"}
    assert mat.disconnected == mat.clients


async def test_flow_connect_failure_keeps_form(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.connect_error = OSError("no route")
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_connect_failed"}


async def test_flow_no_status_keeps_form(hass: HomeAssistant, mat: FakeMat) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.first_status = None
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_no_status"}
    assert mat.disconnected == mat.clients


async def test_flow_status_before_reply_2_does_not_count(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.status_before_reply2 = HEATING
    mat.first_status = None
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_no_status"}
    assert mat.disconnected == mat.clients


async def test_flow_uses_the_status_after_reply_2(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.status_before_reply2 = status(flags=0x03)  # a single mat, were it to count
    with patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None):
        done = await _submit(hass, result["flow_id"])
        await hass.async_block_till_done()
    assert done.get("type") is FlowResultType.CREATE_ENTRY


async def test_flow_single_mat_aborts(hass: HomeAssistant, mat: FakeMat) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.first_status = status(flags=0x03)
    done = await _submit(hass, result["flow_id"])
    assert done.get("type") is FlowResultType.ABORT
    assert done.get("reason") == "single_mat"
    assert not hass.config_entries.async_entries(DOMAIN)
    assert mat.disconnected == mat.clients

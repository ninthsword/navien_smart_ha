"""The link to one mat: the reconnect loop, the status it publishes and the command path.

The only module that writes to the mat. Errors are logged by type only, because a Bluetooth
library's message can hold the device address.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from . import transport
from .protocol import (
    HEAT_BOTH,
    HEAT_LEFT_ONLY,
    HEAT_RIGHT_ONLY,
    INITIAL_TARGET,
    MODE_OFF,
    REPLY_AUTHENTICATED,
    REPLY_ISSUED,
    REPLY_REFUSED,
    HandshakeReply,
    MatState,
    MatStatus,
    build_authentication_frame,
    build_fast_heat_frame,
    build_heating_frame,
    build_ion_care_frame,
    build_off_frame,
    build_off_lock_frame,
    build_registration_frame,
    build_volume_frame,
    merge_one_side,
    parse_frame,
    parse_status,
)

_LOGGER = logging.getLogger(__name__)

CONF_MAT_ID = "mat_id"

RETRY_DELAY = 30.0
SILENCE_TIMEOUT = 60.0
REPLY_TIMEOUT = 15.0
FIRST_STATUS_TIMEOUT = 15.0
CONFIRM_TIMEOUT = 3.0
SEND_TIMEOUT = 10.0
SEND_ATTEMPTS = 2
MAX_REFUSALS = 3

STORAGE_VERSION = 1

LINK_DISABLED = "사용 안 함"
LINK_CONNECTING = "연결 중"
LINK_CONNECTED = "연결됨"
LINK_NEEDS_REGISTRATION = "재등록 필요"
LINK_STATES = [LINK_DISABLED, LINK_CONNECTING, LINK_CONNECTED, LINK_NEEDS_REGISTRATION]

SIDE_LEFT = "left"
SIDE_RIGHT = "right"

HVAC_HEAT = "heat"
HVAC_OFF = "off"

ERROR_NO_LINK = "블루투스 매트와 연결되어 있지 않습니다."
ERROR_OTHER_MODE = "매트가 다른 운전 모드(수면 등)일 때는 제어할 수 없습니다."
ERROR_UNCONFIRMED = "매트가 명령을 확인해 주지 않았습니다."
ERROR_OFF_TEMPERATURE = "매트가 꺼져 있을 때는 온도만 바꿀 수 없습니다. 먼저 매트를 켜 주세요."
ERROR_UNSUPPORTED_MODE = "이 매트는 '{mode}' 모드를 지원하지 않습니다."
ERROR_NOT_HEATING = "매트가 난방 중일 때만 사용할 수 있습니다."
ERROR_LOCK_STATE = "지금 상태에서는 조작 잠금을 바꿀 수 없습니다."
ERROR_SIDE_OFF = "이쪽이 켜져 있을 때만 사용할 수 있습니다."


class RegistrationRefused(Exception):
    """The mat refused registration."""


class RegistrationNoStatus(Exception):
    """The mat registered but sent no status in time."""


class _LinkError(Exception):
    """One failed attempt of the reconnect loop."""


class _Refused(_LinkError):
    """The mat refused the stored id."""


@dataclass(frozen=True)
class Registration:
    """What a successful registration learned."""

    issued_id: bytes
    is_double: bool


@dataclass(frozen=True)
class _Send:
    frame: bytes
    confirmed: Callable[[MatStatus], bool]


@dataclass(frozen=True)
class _Intent:
    """What a command decided on its first attempt; a resend builds the same frame anew."""

    send: Callable[[MatStatus], _Send]
    # What must still be true for a resend; without it a command must not repeat itself on a mat
    # that has moved on, such as turning back on one that left the heating state meanwhile.
    holds: Callable[[MatStatus], bool] | None = None


def _store_for(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.ble_mat.{entry_id}")


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    await _store_for(hass, entry_id).async_remove()


async def _async_close(client: Any) -> None:
    """Disconnect, whatever state the link is in."""
    try:
        await transport.async_disconnect(client)
    except Exception as err:
        _LOGGER.debug("블루투스 연결 해제 중 오류: %s", type(err).__name__)


async def async_register_mat(hass: HomeAssistant, address: str) -> Registration:
    """Register with a mat in pairing mode; the registration frame goes first."""
    device = transport.async_ble_device(hass, address)
    if device is None:
        raise _LinkError
    replies: asyncio.Queue[HandshakeReply] = asyncio.Queue()
    first_status: asyncio.Future[MatStatus] = hass.loop.create_future()
    authenticated = False

    def on_frame(data: bytes) -> None:
        nonlocal authenticated
        frame = parse_frame(data)
        if isinstance(frame, HandshakeReply):
            # Set here, not when the flow resumes: a status can follow in the same tick.
            authenticated = frame.code == REPLY_AUTHENTICATED
            replies.put_nowait(frame)
        elif isinstance(frame, MatStatus) and authenticated and not first_status.done():
            first_status.set_result(frame)

    client = await transport.async_connect(device, lambda: None)
    try:
        await transport.async_start_notify(client, on_frame)
        await transport.async_write(client, build_registration_frame())
        issued = await _async_reply(replies)
        if issued.code != REPLY_ISSUED or issued.issued_id is None:
            raise RegistrationRefused
        await transport.async_write(
            client, build_authentication_frame(issued.issued_id)
        )
        if (await _async_reply(replies)).code != REPLY_AUTHENTICATED:
            raise RegistrationRefused
        try:
            async with asyncio.timeout(FIRST_STATUS_TIMEOUT):
                status = await first_status
        except TimeoutError as err:
            raise RegistrationNoStatus from err
        return Registration(issued.issued_id, status.is_double)
    finally:
        await _async_close(client)


async def _async_reply(replies: asyncio.Queue[HandshakeReply]) -> HandshakeReply:
    async with asyncio.timeout(REPLY_TIMEOUT):
        return await replies.get()


def round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


class MatConnection:
    """Holds one Bluetooth mat entry's link."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self._mat_id = bytes.fromhex(entry.data[CONF_MAT_ID])
        self._store = _store_for(hass, entry.entry_id)
        self.enabled = True
        self.status: MatStatus | None = None
        self.stored_status: MatStatus | None = None
        self.authenticated = False
        self.needs_registration = False
        self._listeners: list[CALLBACK_TYPE] = []
        self._task: asyncio.Task[None] | None = None
        self._client: Any = None
        self._generation = 0
        self._lost = asyncio.Event()
        self._activity = asyncio.Event()
        self._changed = asyncio.Event()
        self._since_send: list[MatStatus] | None = None
        self._reply: asyncio.Future[HandshakeReply | None] | None = None
        self._command_lock = asyncio.Lock()
        self._refusals = 0
        self._cache_cleared = False

    @property
    def link_state(self) -> str:
        if not self.enabled:
            return LINK_DISABLED
        if self.needs_registration:
            return LINK_NEEDS_REGISTRATION
        return LINK_CONNECTED if self.authenticated else LINK_CONNECTING

    async def async_load(self) -> None:
        """Read the switch state and the stored heating status before the loop starts."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            data = {}
        self.enabled = data.get("enabled", True)
        self.stored_status = _parse_stored_status(data.get("heating_status"))

    def _payload(self) -> dict[str, Any]:
        stored = self.stored_status
        return {
            "enabled": self.enabled,
            "heating_status": None if stored is None else stored.raw.hex(),
        }

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    @callback
    def _publish(self) -> None:
        self._changed.set()
        for listener in list(self._listeners):
            listener()

    @callback
    def async_start(self) -> None:
        """Start the loop in the background, if the switch allows it."""
        if not self.enabled or self._task is not None:
            return
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_run(), "navien_ble_mat"
        )

    async def async_stop(self) -> None:
        """Cancel the loop, which always disconnects, and save the store."""
        await self._async_cancel_loop()
        await self._store.async_save(self._payload())

    async def _async_cancel_loop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def async_set_enabled(self, enabled: bool) -> None:
        """Drop the link and stay away, or start again from a clean slate."""
        if enabled == self.enabled:
            return
        self.enabled = enabled
        await self._async_cancel_loop()
        self.needs_registration = False
        await self._store.async_save(self._payload())
        self.async_start()
        self._publish()

    async def _async_run(self) -> None:
        self._refusals = 0
        self._cache_cleared = False
        while True:
            try:
                await self._async_attempt()
            except _Refused:
                self._refusals += 1
                if self._refusals >= MAX_REFUSALS:
                    _LOGGER.warning("블루투스 매트가 등록을 거부했습니다. 다시 등록해 주세요.")
                    self.needs_registration = True
                    self._publish()
                    return
            except Exception as err:
                self._refusals = 0
                _LOGGER.debug("블루투스 매트 연결 실패: %s", type(err).__name__)
            await asyncio.sleep(RETRY_DELAY)

    async def _async_attempt(self) -> None:
        device = transport.async_ble_device(self.hass, self.address)
        if device is None:
            raise _LinkError("not seen")
        self._generation += 1
        generation = self._generation

        @callback
        def on_disconnect() -> None:
            if generation == self._generation:
                self._generation += 1
                self._link_lost()

        @callback
        def on_frame(data: bytes) -> None:
            if generation == self._generation:
                self._handle_frame(data)

        self._lost.clear()
        self._activity.clear()
        self._reply = self.hass.loop.create_future()
        client = await transport.async_connect(device, on_disconnect)
        self._client = client
        try:
            await self._async_subscribe(client, on_frame)
            await transport.async_write(client, build_authentication_frame(self._mat_id))
            async with asyncio.timeout(REPLY_TIMEOUT):
                reply = await self._reply
            if reply is None:
                raise _LinkError("link lost")
            if reply.code == REPLY_REFUSED:
                raise _Refused
            if reply.code != REPLY_AUTHENTICATED:
                raise _LinkError("unexpected reply")
            self._publish()
            await self._async_watch()
        finally:
            self._generation += 1
            self._link_lost()
            self._client = None
            await _async_close(client)

    async def _async_subscribe(
        self, client: Any, on_frame: Callable[[bytes], None]
    ) -> None:
        try:
            await transport.async_start_notify(client, on_frame)
        except transport.MissingCharacteristic:
            if not self._cache_cleared:
                self._cache_cleared = True
                await transport.async_clear_cache(client)
            raise

    async def _async_watch(self) -> None:
        """Return only by raising: the link was lost or fell silent."""
        while True:
            if self._lost.is_set():
                raise _LinkError("link lost")
            self._activity.clear()
            async with asyncio.timeout(SILENCE_TIMEOUT):
                await self._activity.wait()

    @callback
    def _link_lost(self) -> None:
        """Discard everything that was only true of this connection."""
        self._lost.set()
        self._activity.set()
        if self._reply is not None and not self._reply.done():
            self._reply.set_result(None)
        self.authenticated = False
        self.status = None
        self._publish()

    @callback
    def _handle_frame(self, data: bytes) -> None:
        frame = parse_frame(data)
        if isinstance(frame, HandshakeReply):
            if self._reply is not None and not self._reply.done():
                # Set here, not when the loop resumes: a status can follow in the same tick.
                self.authenticated = frame.code == REPLY_AUTHENTICATED
                self._reply.set_result(frame)
            return
        if frame is None or not self.authenticated:
            return
        self._activity.set()
        if self._since_send is not None:
            self._since_send.append(frame)
            self._changed.set()
        if self.status is not None and frame.raw == self.status.raw:
            return
        self.status = frame
        self._remember(frame)
        self._publish()

    @callback
    def _remember(self, status: MatStatus) -> None:
        stored = self.stored_status
        merged = status if status.is_storable else merge_one_side(stored, status)
        if merged is not None and (
            stored is None or _power_on_values(merged) != _power_on_values(stored)
        ):
            self.stored_status = merged
            self._store.async_delay_save(self._payload, 1)

    def diagnostics(self) -> dict[str, Any]:
        status = self.status
        return {
            "connection": "bluetooth_mat",
            "link_state": self.link_state,
            "enabled": self.enabled,
            "has_stored_heating_status": self.stored_status is not None,
            "status": None if status is None else status.raw.hex(),
        }

    # Commands. One path: decide once, re-read the state, build at send time, send, confirm,
    # resend.

    async def async_set_temperature(
        self, side: str, temperature: float, hvac_mode: str | None
    ) -> None:
        target = round_half_up(temperature)

        def decide(status: MatStatus, stored: MatStatus | None) -> _Intent | None:
            if hvac_mode == HVAC_OFF:
                return _side_off_intent(status, side)
            if hvac_mode not in (None, HVAC_HEAT):
                raise ServiceValidationError(ERROR_UNSUPPORTED_MODE.format(mode=hvac_mode))
            if status.state is MatState.HEATING:
                return _side_temperature_intent(status, side, target)
            if hvac_mode is None:
                raise ServiceValidationError(ERROR_OFF_TEMPERATURE)
            return _power_on_intent(stored, side, target)

        await self._async_command(decide)

    async def async_set_hvac_mode(self, side: str, hvac_mode: str) -> None:
        def decide(status: MatStatus, stored: MatStatus | None) -> _Intent | None:
            if hvac_mode == HVAC_OFF:
                return _side_off_intent(status, side)
            if hvac_mode != HVAC_HEAT:
                raise ServiceValidationError(ERROR_UNSUPPORTED_MODE.format(mode=hvac_mode))
            if status.state is MatState.HEATING:
                return _side_on_intent(status, stored, side)
            return _power_on_intent(stored, side, None)

        await self._async_command(decide)

    async def async_set_power(self, on: bool) -> None:
        """Turn the whole mat on or off."""

        def decide(status: MatStatus, stored: MatStatus | None) -> _Intent | None:
            if not on:
                return _off_intent(status)
            if status.state is MatState.HEATING:
                return None
            return _power_on_intent(stored, None, None)

        await self._async_command(decide)

    async def async_set_lock(self, locked: bool) -> None:
        await self._async_command(lambda status, _stored: _lock_intent(status, locked))

    async def async_set_volume(self, volume: int) -> None:
        await self._async_command(lambda status, _stored: _volume_intent(status, volume))

    async def async_set_fast_heat(self, on: bool) -> None:
        await self._async_command(lambda status, _stored: _fast_heat_intent(status, on))

    async def async_run_ion_care(self) -> None:
        await self._async_command(lambda status, _stored: _ion_care_run_intent(status))

    async def async_set_ion_care_period(self, period: int) -> None:
        await self._async_command(
            lambda status, _stored: _ion_care_period_intent(status, period)
        )

    async def async_match_temperature(self, side: str) -> None:
        """Set both sides to `side`'s target."""
        await self._async_command(lambda status, _stored: _match_intent(status, side))

    async def _async_command(
        self, decide: Callable[[MatStatus, MatStatus | None], _Intent | None]
    ) -> None:
        async with self._command_lock:
            # Read once: a reply to the first send can replace the stored status.
            stored = self.stored_status
            intent: _Intent | None = None
            for _ in range(SEND_ATTEMPTS):
                status = self.status
                if status is None:
                    raise HomeAssistantError(ERROR_NO_LINK)
                if (
                    intent is not None
                    and intent.holds is not None
                    and not intent.holds(status)
                ):
                    raise HomeAssistantError(ERROR_UNCONFIRMED)
                if status.state is MatState.OTHER:
                    raise ServiceValidationError(ERROR_OTHER_MODE)
                if intent is None:
                    intent = decide(status, stored)
                    if intent is None:
                        return
                if await self._async_send_and_confirm(intent.send(status)):
                    return
            raise HomeAssistantError(ERROR_UNCONFIRMED)

    async def _async_send_and_confirm(self, send: _Send) -> bool:
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                await transport.async_write(self._client, send.frame)
        except Exception as err:
            _LOGGER.debug("블루투스 매트 전송 실패: %s", type(err).__name__)
            return False
        # Only a status received from here on can confirm this send.
        received: list[MatStatus] = []
        self._since_send = received
        try:
            return await self._async_confirmed(send, received)
        finally:
            self._since_send = None

    async def _async_confirmed(self, send: _Send, received: list[MatStatus]) -> bool:
        loop = self.hass.loop
        deadline = loop.time() + CONFIRM_TIMEOUT
        while True:
            self._changed.clear()
            if self.status is None:
                return False
            if any(send.confirmed(status) for status in received):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                async with asyncio.timeout(remaining):
                    await self._changed.wait()
            except TimeoutError:
                return False


def _parse_stored_status(value: Any) -> MatStatus | None:
    """Read the store's heating status; one that cannot be parsed counts as absent."""
    try:
        status = parse_status(bytes.fromhex(value))
    except (TypeError, ValueError):
        return None
    return status if status is not None and status.is_restorable else None


def _power_on_values(status: MatStatus) -> tuple[int, int, int, int, int]:
    """What a power-on takes from the stored status, and the range that validates it."""
    return (
        status.raw[5],
        status.raw[9],
        status.raw[18],
        status.left_target,
        status.right_target,
    )


def _is_heating(status: MatStatus) -> bool:
    return status.state is MatState.HEATING


def _confirms_heating(left: int, right: int) -> Callable[[MatStatus], bool]:
    def confirmed(status: MatStatus) -> bool:
        return (
            status.state is MatState.HEATING
            and status.heat_type == HEAT_BOTH
            and status.left_target == left
            and status.right_target == right
        )

    return confirmed


def _side_on(status: MatStatus, side: str) -> bool:
    return status.left_on if side == SIDE_LEFT else status.right_on


def _other_side(side: str) -> str:
    return SIDE_RIGHT if side == SIDE_LEFT else SIDE_LEFT


def _target_of(status: MatStatus, side: str) -> int:
    return status.left_target if side == SIDE_LEFT else status.right_target


def _targets(side: str, target: int, other: int) -> tuple[int, int]:
    """The (left, right) pair for `side` at `target` and the other side at `other`."""
    return (target, other) if side == SIDE_LEFT else (other, target)


def _side_intent(
    heat_type: int,
    left: int,
    right: int,
    confirmed: Callable[[MatStatus], bool],
) -> _Intent:
    def send(status: MatStatus) -> _Send:
        return _Send(
            build_heating_frame(status, status, left, right, heat_type), confirmed
        )

    return _Intent(send, _is_heating)


def _confirms_heat_type(
    heat_type: int, side: str | None, target: int | None
) -> Callable[[MatStatus], bool]:
    """Confirm byte 5 and, when a side is named, that side's target."""

    def confirmed(status: MatStatus) -> bool:
        if status.state is not MatState.HEATING or status.heat_type != heat_type:
            return False
        return side is None or _target_of(status, side) == target

    return confirmed


def _side_off_intent(status: MatStatus, side: str) -> _Intent | None:
    """Turn off one side; the last side on takes the whole mat off with it."""
    if status.state is not MatState.HEATING or not _side_on(status, side):
        return None
    other = _other_side(side)
    if not _side_on(status, other):
        heat_type = status.heat_type
        return _off_intent(
            status, lambda s: _is_heating(s) and s.heat_type == heat_type
        )
    heat_type = HEAT_RIGHT_ONLY if side == SIDE_LEFT else HEAT_LEFT_ONLY
    left, right = _targets(side, status.min_target, _target_of(status, other))
    return _side_intent(heat_type, left, right, _confirms_heat_type(heat_type, None, None))


def _side_on_intent(
    status: MatStatus, stored: MatStatus | None, side: str
) -> _Intent | None:
    """Turn on one side at its remembered target; nothing to do if it is already on."""
    if _side_on(status, side):
        return None
    target = INITIAL_TARGET if stored is None else _target_of(stored, side)
    return _side_temperature_intent(status, side, target)


def _side_temperature_intent(status: MatStatus, side: str, target: int) -> _Intent:
    """Set one side's target, turning that side on if it is off."""
    other = _other_side(side)
    if _side_on(status, side) and not _side_on(status, other):
        heat_type = status.heat_type
        other_target = status.min_target
    else:
        heat_type = HEAT_BOTH
        other_target = _target_of(status, other)
    left, right = _targets(side, target, other_target)
    return _side_intent(heat_type, left, right, _confirms_heat_type(heat_type, side, target))


def _off_intent(
    status: MatStatus, holds: Callable[[MatStatus], bool] | None = None
) -> _Intent | None:
    """Turn off the whole mat."""
    if status.state is MatState.OFF:
        return None
    return _Intent(
        lambda latest: _Send(build_off_frame(latest), lambda s: s.state is MatState.OFF),
        holds,
    )


def _both_sides_on_intent(
    stored: MatStatus | None, left: int, right: int
) -> _Intent:
    """Heat both sides; byte 9 comes from the stored status, else from the live one."""

    def send(status: MatStatus) -> _Send:
        source = status if stored is None else stored
        return _Send(
            build_heating_frame(status, source, left, right), _confirms_heating(left, right)
        )

    return _Intent(send)


def _one_side_on_intent(stored: MatStatus | None, side: str, target: int) -> _Intent:
    """Heat only `side`, the other side's byte at the minimum of the frame that supplies it."""
    heat_type = HEAT_LEFT_ONLY if side == SIDE_LEFT else HEAT_RIGHT_ONLY
    confirmed = _confirms_heat_type(heat_type, side, target)

    def send(status: MatStatus) -> _Send:
        source = status if stored is None else stored
        left, right = _targets(side, target, source.min_target)
        return _Send(build_heating_frame(status, source, left, right, heat_type), confirmed)

    return _Intent(send)


def _power_on_intent(
    stored: MatStatus | None, side: str | None, target: int | None
) -> _Intent:
    """Power on from off: the last state, or for a thermostat whose side was off, that side.

    `side` is None for the 전원 switch. `target` replaces the side's remembered target.
    """
    if stored is None:
        if side is None:
            return _both_sides_on_intent(None, INITIAL_TARGET, INITIAL_TARGET)
        return _one_side_on_intent(
            None, side, INITIAL_TARGET if target is None else target
        )
    if side is not None and not _side_on(stored, side):
        return _one_side_on_intent(
            stored, side, _target_of(stored, side) if target is None else target
        )
    if stored.heat_type == HEAT_BOTH:
        left = target if side == SIDE_LEFT and target is not None else stored.left_target
        right = target if side == SIDE_RIGHT and target is not None else stored.right_target
        return _both_sides_on_intent(stored, left, right)
    on_side = SIDE_LEFT if stored.heat_type == HEAT_LEFT_ONLY else SIDE_RIGHT
    return _one_side_on_intent(
        stored, on_side, _target_of(stored, on_side) if target is None else target
    )


def _require_heating(status: MatStatus) -> None:
    if status.state is not MatState.HEATING:
        raise ServiceValidationError(ERROR_NOT_HEATING)


def _lock_intent(status: MatStatus, locked: bool) -> _Intent | None:
    """Change the lock bit in the frame of the state the mat is in."""
    if status.state is not MatState.HEATING and status.mode != MODE_OFF:
        raise ServiceValidationError(ERROR_LOCK_STATE)
    if status.lock == locked:
        return None

    def confirmed(latest: MatStatus) -> bool:
        return latest.lock == locked

    if status.state is MatState.HEATING:
        return _Intent(
            lambda latest: _Send(
                build_heating_frame(
                    latest,
                    latest,
                    latest.left_target,
                    latest.right_target,
                    latest.heat_type,
                    locked,
                ),
                confirmed,
            ),
            _is_heating,
        )
    return _Intent(
        lambda latest: _Send(build_off_lock_frame(latest, locked), confirmed),
        lambda latest: latest.mode == MODE_OFF,
    )


def _volume_intent(status: MatStatus, volume: int) -> _Intent | None:
    _require_heating(status)
    if status.volume == volume:
        return None
    return _Intent(
        lambda latest: _Send(
            build_volume_frame(latest, volume), lambda s: s.volume == volume
        ),
        _is_heating,
    )


def _fast_heat_intent(status: MatStatus, on: bool) -> _Intent | None:
    _require_heating(status)
    if status.fast_heat_running == on:
        return None
    return _Intent(
        lambda latest: _Send(
            build_fast_heat_frame(latest, on), lambda s: s.fast_heat_running == on
        ),
        _is_heating,
    )


def _ion_care_run_intent(status: MatStatus) -> _Intent | None:
    _require_heating(status)
    if status.ion_care_running:
        return None
    return _Intent(
        lambda latest: _Send(
            build_ion_care_frame(latest, latest.ion_care_period, True),
            lambda s: s.ion_care_running,
        ),
        lambda latest: _is_heating(latest) and not latest.ion_care_running,
    )


def _ion_care_period_intent(status: MatStatus, period: int) -> _Intent | None:
    _require_heating(status)
    if status.ion_care_period == period:
        return None
    start = not status.ion_care_running
    return _Intent(
        lambda latest: _Send(
            build_ion_care_frame(latest, period, start),
            lambda s: s.ion_care_period == period,
        ),
        _is_heating,
    )


def _match_intent(status: MatStatus, side: str) -> _Intent:
    """Set both sides to `side`'s target, whether or not they already match."""
    if not _side_on(status, side):
        raise ServiceValidationError(ERROR_SIDE_OFF)
    target = _target_of(status, side)
    confirmed = _confirms_heating(target, target)
    return _Intent(
        lambda latest: _Send(
            build_heating_frame(latest, latest, target, target), confirmed
        ),
        lambda latest: _side_on(latest, side),
    )

"""AWS IoT MQTT-over-WebSocket subscription.

Devices push their own state — confirmed on a real device — so this subscription, not
polling, is the primary path.

Mats, Airone units and boilers each apply state through their own verified parser. The
boiler `smarttok` path reads only confirmed fields and leaves nothing but a de-identified
packet shape in public diagnostics. No boiler command is ever sent.

A single change on a mat produces up to three kinds of shadow event, and **only
`/update/accepted` messages carrying `state.reported`** are used. Applying the others
(`/delta`, `/documents`, and `/accepted` without `reported`) would put HA ahead of the
device.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util.ssl import get_default_context

from .api import AwsCredentials
from .boiler import BOILER_TOPIC_PREFIX, extract_boiler_status, observe_boiler_message
from .const import (
    IOT_ENDPOINT,
    IOT_REGION,
    IOT_SERVICE,
    LEGACY_ACTUAL_FIELDS,
    LEGACY_EXTRA_FIELDS,
    LEGACY_RUNNING_TO_V2,
    LEGACY_STATUS_TO_CONTROLLER,
)

_LOGGER = logging.getLogger(__name__)

# **Two things are received.**
#
#   /update/accepted  the device pushed its state
#   /get/accepted     we asked for the document stored in the shadow
#
# The second is used once, right after connecting. The server answers even when the device
# is off, and without it the state stays empty until the device sends something by itself.
_ACCEPTED_SUFFIXES = ("/update/accepted", "/get/accepted")
_RECONNECT_DELAYS = (5, 15, 30, 60, 120, 300)
# Staying connected this long counts as a real connection and resets the backoff. CONNACK
# alone must not reset it: when the link drops immediately after connecting, that would sit
# on the first delay (5s) forever — which really happens, because the account allows one
# session and the user may have the app open.
_STABLE_CONNECTION_SECONDS = 60.0

# How an Airone message is told apart from a mat message. The app decides the same way,
# from the subscription topic string (the `/airone/#` and `/mate/#` branches in
# `HomeViewModel`).
AIRONE_PREFIX = "airone"


def _uri_encode(value: str) -> str:
    """SigV4 canonical encoding, keeping only unreserved characters.

    The '/' in `X-Amz-Credential` has to go out as `%2F`; `urlencode` leaves '/' intact by
    default, which breaks the signature.
    """
    return urllib.parse.quote(value, safe="-_.~")


def build_signed_ws_path(creds: AwsCredentials, region: str = IOT_REGION) -> str:
    """Build the SigV4 pre-signed path for the AWS IoT WebSocket.

    The security token is appended **after** the signature is computed. That is AWS IoT's
    rule.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/{IOT_SERVICE}/aws4_request"

    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{creds.access_key_id}/{scope}",
        "X-Amz-Date": amzdate,
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{_uri_encode(key)}={_uri_encode(value)}" for key, value in sorted(query.items())
    )
    empty_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join(
        [
            "GET",
            "/mqtt",
            canonical_query,
            f"host:{IOT_ENDPOINT}\n",
            "host",
            empty_hash,
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amzdate,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    key = f"AWS4{creds.secret_key}".encode()
    for part in (datestamp, region, IOT_SERVICE, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    token = urllib.parse.quote(creds.session_token, safe="")
    return (
        f"/mqtt?{canonical_query}"
        f"&X-Amz-Signature={signature}"
        f"&X-Amz-Security-Token={token}"
    )


def extract_reported(payload: bytes, topic: str) -> tuple[str, dict[str, Any]] | None:
    """Let through only the events worth using.

    Two conditions have to hold: the shadow topic is `/update/accepted` or `/get/accepted`,
    and `state.reported` is present. Returns `(deviceId, reported)`.

    A `/get/accepted` response also carries `desired` and `metadata`. **Only `reported` is
    read** — `desired` is what was sent, not what the device confirmed.
    """
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _LOGGER.debug("JSON 이 아닌 MQTT 메시지 무시: topic=%s", topic)
        return None

    shadow_topic = event.get("topic") or ""
    if not shadow_topic.endswith(_ACCEPTED_SUFFIXES):
        return None

    state = ((event.get("payload") or {}).get("state")) or {}
    reported = state.get("reported")
    if not isinstance(reported, dict):
        # This event fires when the command lands in the shadow. The device does not know yet.
        return None

    device_id = (reported.get("info") or {}).get("deviceId")
    if not device_id:
        # The last topic segment is the deviceId — `{homeSeq}/mate/{deviceId}`.
        device_id = topic.rsplit("/", 1)[-1]
    if not device_id:
        return None
    return device_id, reported


def extract_airone_reported(
    payload: bytes, topic: str, stats: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]] | None:
    """Pull `reported` out of an Airone state message.

    The envelope differs from a mat: this is not a shadow, so there is no `/update/accepted`
    and no `state` wrapper. The shape is `{topic, payload: {reported: {...}}}`
    (`AironeGetStatus.AironeStatusEachRoom`).
    """
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _LOGGER.debug("JSON 이 아닌 에어원 메시지 무시: topic=%s", topic)
        _bump(stats, "dropped_not_json")
        return None
    if not isinstance(event, dict):
        _bump(stats, "dropped_not_json")
        return None

    inner = event.get("payload")
    reported = inner.get("reported") if isinstance(inner, dict) else None
    if reported is None and isinstance(inner, dict):
        # The older generation sends a flat frame with no `reported` wrapper.
        reported = normalize_legacy_status(inner)
        if reported is not None:
            _bump(stats, "legacy_normalized")
    if not isinstance(reported, dict):
        # This may be an acknowledgement of a command. If it is not state, it is not used.
        _bump(stats, "dropped_no_reported")
        return None
    # `idu` (indoor unit) is accepted too. On an all-in-one room controller the controller
    # and the indoor unit are one assembly, so state may well arrive here — the field really
    # does exist in `did`. The values are not interpreted: they are captured into diagnostics
    # so their shape can be judged first.
    if not any(
        key in reported for key in ("roomController", "odu", "airMonitor", "idu")
    ):
        # **Never discarded silently.** As DEBUG this left no trace on a default install, so
        # a report could not tell us why state was not arriving. Only key names are logged,
        # never values.
        _LOGGER.warning(
            "에어원 상태 메시지의 모양을 알지 못해 쓰지 못했습니다 "
            "(topic 끝=%s, 최상위 키=%s). 이 로그를 이슈에 붙여 주시면 "
            "바로 넓힐 수 있습니다.",
            topic.rsplit("/", 1)[-1],
            sorted(reported),
        )
        _bump(stats, "dropped_unknown_shape")
        if stats is not None:
            stats["last_unknown_shape_keys"] = sorted(reported)
        return None

    # The last segment of `{homeSeq}/airone/{deviceId}` is the deviceId from the device list.
    device_id = topic.rsplit("/", 1)[-1]
    if not device_id or device_id == AIRONE_PREFIX:
        controller = reported.get("roomController")
        if isinstance(controller, dict):
            device_id = str(controller.get("deviceId") or "")
    if not device_id:
        _bump(stats, "dropped_no_device_id")
        return None
    _bump(stats, "accepted")
    return device_id, reported


def normalize_legacy_status(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Translate an older flat status frame into the newer `reported` shape.

    **Translated only at the edge.** Leaving the models and entities to know a single, newer
    vocabulary disturbs the verified newer path far less than branching on generation
    throughout.

    It reads the setpoints — see the table above `LEGACY_STATUS_TO_CONTROLLER` for why.
    """
    if "isRunning" not in payload:
        return None

    controller: dict[str, Any] = {}
    running = LEGACY_RUNNING_TO_V2.get(payload.get("isRunning"))
    if running is not None:
        controller["running"] = running
    for source, target in LEGACY_STATUS_TO_CONTROLLER.items():
        if source in payload:
            controller[target] = payload[source]

    error_code = payload.get("errorCode")
    if error_code is not None:
        controller["error"] = {"code": error_code}

    # What the outdoor unit is actually doing. Kept to diagnostics rather than used as
    # control state: it changes constantly with conditions, and reading it as the mode would
    # make the display jitter.
    actual = {key: payload[key] for key in LEGACY_ACTUAL_FIELDS if key in payload}
    reported: dict[str, Any] = {"roomController": controller}
    if actual:
        reported["legacyActual"] = actual

    # Values with no counterpart in the newer protocol. Exported to diagnostics only, never
    # interpreted for control.
    extras = {
        key: payload[field]
        for field, (key, _label, _table) in LEGACY_EXTRA_FIELDS.items()
        if payload.get(field) is not None
    }
    if extras:
        reported["legacyExtras"] = extras
    return reported


def _bump(stats: dict[str, Any] | None, key: str) -> None:
    """Counters only; no values.

    This exists so "nothing arrives" can be told from "it arrives and is discarded"
    **from the diagnostics download alone, with no logging**. Asking a user to turn logging
    on and attach it cuts the response rate sharply.
    """
    if stats is None:
        return
    stats[key] = int(stats.get(key) or 0) + 1


class NavienSmartMqtt:
    """Subscribe-only MQTT client. It never publishes — control goes over REST."""

    def __init__(
        self,
        hass: HomeAssistant,
        home_seq: int,
        user_seq: int,
        topic_prefixes: set[str],
        credentials_provider: Callable[[], Awaitable[AwsCredentials | None]],
        on_reported: Callable[[str, dict[str, Any]], None],
        on_subscribed: Callable[[], Awaitable[None]] | None = None,
        on_airone_reported: Callable[[str, dict[str, Any]], None] | None = None,
        on_boiler_reported: Callable[[str, dict[str, Any]], None] | None = None,
        on_boiler_observation: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._hass = hass
        # Received and discarded counters. Nothing personal — counts and key names only.
        self.stats: dict[str, Any] = {}
        self._home_seq = home_seq
        self._user_seq = user_seq
        self._prefixes = topic_prefixes or {"mate"}
        self._credentials_provider = credentials_provider
        self._on_reported = on_reported
        self._on_airone_reported = on_airone_reported
        self._on_boiler_reported = on_boiler_reported
        self._on_boiler_observation = on_boiler_observation
        # The initial state must be requested after the subscription is in place; reversed,
        # the response is missed.
        self._on_subscribed = on_subscribed
        self._client_id = ""
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._attempt = 0
        self.connected = False

    @property
    def topics(self) -> list[str]:
        # The same `#` the app uses (`HomeViewModel` subscribes to `/{prefix}/#`). An Airone
        # response can arrive one level deeper, which `+` would miss.
        return [f"{self._home_seq}/{prefix}/#" for prefix in sorted(self._prefixes)]

    @property
    def client_id(self) -> str:
        """The MQTT clientId currently in use. It has to go into the Airone control envelope."""
        return self._client_id

    async def async_start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = self._hass.async_create_background_task(
                self._async_run(), name="navien_smart_mqtt"
            )

    async def async_stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._async_disconnect()

    async def _async_disconnect(self) -> None:
        client, self._client = self._client, None
        self.connected = False
        if client is None:
            return
        await self._hass.async_add_executor_job(self._disconnect_blocking, client)

    @staticmethod
    def _disconnect_blocking(client: Any) -> None:
        with contextlib.suppress(Exception):
            client.disconnect()
        with contextlib.suppress(Exception):
            client.loop_stop()

    async def _async_run(self) -> None:
        """Keep the connection up, fetching fresh credentials and reconnecting after a drop."""
        while not self._stopping:
            try:
                await self._async_connect_once()
                # `connect()` returns before CONNACK. Without waiting here, the watch loop
                # below sees `connected=False`, leaves at once, and tears down the connection
                # it just made — reconnecting forever.
                await self._async_wait_connected()
                connected_at = time.monotonic()

                # Request the initial state once subscribed. Shadow events only fire on a
                # change, so without this the state stays empty for as long as nobody touches
                # the device.
                if self._on_subscribed is not None:
                    await self._on_subscribed()

                # While the connection holds, the paho thread does the work; only the drop
                # is watched for here.
                while not self._stopping and self.connected:
                    await asyncio.sleep(5)
                    if time.monotonic() - connected_at >= _STABLE_CONNECTION_SECONDS:
                        # Having lasted this long, the next drop counts as a new incident.
                        self._attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any failure is absorbed into a retry
                _LOGGER.warning("MQTT 접속 실패: %s", err)

            if self._stopping:
                break
            await self._async_disconnect()
            delay = _RECONNECT_DELAYS[min(self._attempt, len(_RECONNECT_DELAYS) - 1)]
            self._attempt += 1
            _LOGGER.debug("%s초 후 MQTT 재접속", delay)
            await asyncio.sleep(delay)

    async def _async_wait_connected(self, timeout: float = 15.0) -> None:
        """Wait for CONNACK. The `_on_connect` callback is what sets `connected`."""
        deadline = timeout
        while deadline > 0:
            if self._stopping or self.connected:
                return
            await asyncio.sleep(0.2)
            deadline -= 0.2
        raise TimeoutError(f"{timeout}초 안에 MQTT CONNACK 이 오지 않았습니다.")

    async def _async_connect_once(self) -> None:
        creds = await self._credentials_provider()
        if creds is None:
            raise RuntimeError("AWS 자격증명을 받지 못했습니다.")

        import paho.mqtt.client as mqtt  # deferred import so HA startup is not blocked

        # Matches the format the app uses — `{uuid}-U{userSeq}`. This was corrected from
        # `homeSeq`. An A/B check showed both subscribe fine, but if server policy ever starts
        # inspecting the clientId, whichever differs from the app gets blocked first.
        client_id = f"{uuid.uuid4()}-U{self._user_seq}"
        # This value has to go into the Airone control envelope for the server to answer.
        self._client_id = client_id
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                transport="websockets",
            )
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=client_id, transport="websockets")

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        # `ssl.create_default_context()` reads certificates from disk and blocks the event
        # loop. Use the context HA builds and caches at startup instead.
        client.tls_set_context(get_default_context())
        client.ws_set_options(path=build_signed_ws_path(creds))

        self._client = client
        await self._hass.async_add_executor_job(self._connect_blocking, client)

    @staticmethod
    def _connect_blocking(client: Any) -> None:
        client.connect(IOT_ENDPOINT, 443, keepalive=60)
        client.loop_start()

    # -- paho callbacks, invoked on a separate thread ----------------------

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, reason: Any, *_: Any) -> None:
        code = getattr(reason, "value", reason)
        if code != 0:
            _LOGGER.warning("MQTT 접속 거부 (code=%s)", code)
            return
        self.connected = True
        for topic in self.topics:
            client.subscribe(topic, qos=0)
        _LOGGER.debug("MQTT 구독 시작: %s", ", ".join(self.topics))

    def _on_disconnect(self, _client: Any, _userdata: Any, *args: Any) -> None:
        self.connected = False
        _LOGGER.debug("MQTT 연결이 끊겼습니다 %s", args[:1])

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        # Split by subscription topic, the way the app does it. The envelopes differ
        # completely, and one parser for both would silently drop one of them.
        if f"/{BOILER_TOPIC_PREFIX}/" in message.topic:
            _bump(self.stats, "boiler_received")
            self._handle_boiler_message(message)
            return
        if f"/{AIRONE_PREFIX}/" in message.topic:
            _bump(self.stats, "airone_received")
            self._handle_airone_message(message)
            return

        _bump(self.stats, "mate_received")
        result = extract_reported(message.payload, message.topic)
        if result is None:
            # Discarded events are logged too. Without this, "no state arrives" could not be
            # told from "it arrives and is dropped", which dragged out debugging.
            _LOGGER.debug("MQTT 이벤트 무시 (reported 없음): %s", message.topic)
            _bump(self.stats, "mate_dropped_no_reported")
            return
        device_id, reported = result
        _LOGGER.debug(
            "MQTT reported 수신: %s heater=%s",
            device_id,
            reported.get("heater"),
        )
        # HA state must never be touched directly from the paho thread.
        self._hass.loop.call_soon_threadsafe(self._on_reported, device_id, reported)

    def _handle_boiler_message(self, message: Any) -> None:
        """Apply the state to the device and hand public diagnostics only a safe structure."""
        observation = observe_boiler_message(message.payload, message.topic)
        _bump(self.stats, f"boiler_{observation['encoding']}")
        if self._on_boiler_observation is not None:
            self._hass.loop.call_soon_threadsafe(
                self._on_boiler_observation, observation
            )

        boiler_stats: dict[str, Any] = {}
        result = extract_boiler_status(message.payload, boiler_stats)
        for key in boiler_stats:
            _bump(self.stats, f"boiler_{key}")
        if result is None:
            return
        if self._on_boiler_reported is None:
            _bump(self.stats, "boiler_dropped_no_handler")
            return
        physical_id, status = result
        self._hass.loop.call_soon_threadsafe(
            self._on_boiler_reported, physical_id, status
        )

    def _handle_airone_message(self, message: Any) -> None:
        if self._on_airone_reported is None:
            _LOGGER.debug("에어원 메시지 무시 (처리기 없음): %s", message.topic)
            _bump(self.stats, "airone_dropped_no_handler")
            return
        airone_stats: dict[str, Any] = {}
        result = extract_airone_reported(message.payload, message.topic, airone_stats)
        for key, value in airone_stats.items():
            if key == "last_unknown_shape_keys":
                self.stats["airone_last_unknown_shape_keys"] = value
            else:
                _bump(self.stats, f"airone_{key}")
        if result is None:
            _LOGGER.debug("에어원 이벤트 무시 (reported 없음): %s", message.topic)
            return
        device_id, reported = result
        _LOGGER.debug(
            "에어원 reported 수신: %s roomController=%s",
            device_id,
            reported.get("roomController"),
        )
        self._hass.loop.call_soon_threadsafe(
            self._on_airone_reported, device_id, reported
        )

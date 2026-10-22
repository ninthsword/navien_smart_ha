"""Exercise production MQTT construction with the installed Paho dependency."""

from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, Mock, call, patch

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.navien_smarthome.api import AwsCredentials
from custom_components.navien_smarthome.const import IOT_ENDPOINT
from custom_components.navien_smarthome.mqtt import NavienSmartMqtt


async def test_production_client_uses_paho_v2_callbacks(hass: HomeAssistant) -> None:
    """Keep the real constructor and callback types, intercepting all I/O."""
    credentials = AsyncMock(return_value=AwsCredentials("test", "test", "test"))
    on_reported = Mock()
    subscription = NavienSmartMqtt(
        hass=hass,
        home_seq=7,
        user_seq=42,
        topic_prefixes={"mate", "airone"},
        credentials_provider=credentials,
        on_reported=on_reported,
    )

    async def run_inline(job: Callable[[mqtt.Client], None], client: mqtt.Client) -> None:
        # Execute production connection/cleanup code without starting an executor thread.
        job(client)

    with (
        patch.object(hass, "async_add_executor_job", side_effect=run_inline),
        patch.object(mqtt.Client, "connect", autospec=True) as connect,
        patch.object(mqtt.Client, "loop_start", autospec=True) as loop_start,
        patch.object(mqtt.Client, "subscribe", autospec=True) as subscribe,
        patch.object(mqtt.Client, "disconnect", autospec=True) as disconnect,
        patch.object(mqtt.Client, "loop_stop", autospec=True) as loop_stop,
        patch("threading.Thread.start", side_effect=AssertionError("Unexpected thread")),
        patch(
            "custom_components.navien_smarthome.mqtt.get_default_context",
            return_value=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        ),
    ):
        await subscription._async_connect_once()
        client = subscription._client
        assert isinstance(client, mqtt.Client)
        assert client.callback_api_version is CallbackAPIVersion.VERSION2
        assert client.transport == "websockets"
        assert client.on_connect == subscription._on_connect
        assert client.on_disconnect == subscription._on_disconnect
        assert client.on_message == subscription._on_message
        credentials.assert_awaited_once_with()
        connect.assert_called_once_with(client, IOT_ENDPOINT, 443, keepalive=60)
        loop_start.assert_called_once_with(client)
        assert not subscription.connected

        assert client.on_connect is not None
        # Paho exposes a union of legacy and VERSION2 callback signatures.
        on_connect = cast(
            Callable[[mqtt.Client, object, mqtt.ConnectFlags, ReasonCode, Properties], None],
            client.on_connect,
        )
        on_connect(
            client, None, mqtt.ConnectFlags(False),
            ReasonCode(PacketTypes.CONNACK, "Not authorized"),
            Properties(PacketTypes.CONNACK),
        )
        assert not subscription.connected
        subscribe.assert_not_called()

        on_connect(
            client, None, mqtt.ConnectFlags(False),
            ReasonCode(PacketTypes.CONNACK, "Success"),
            Properties(PacketTypes.CONNACK),
        )
        assert subscription.connected
        assert subscribe.call_args_list == [
            call(client, "7/airone/#", qos=0),
            call(client, "7/mate/#", qos=0),
        ]

        message = mqtt.MQTTMessage(topic=b"7/mate/example")
        message.payload = b"not-json"
        assert client.on_message is not None
        client.on_message(client, None, message)
        assert subscription.stats == {"mate_received": 1, "mate_dropped_not_json": 1}
        on_reported.assert_not_called()

        assert client.on_disconnect is not None
        on_disconnect = cast(
            Callable[[mqtt.Client, object, mqtt.DisconnectFlags, ReasonCode, Properties], None],
            client.on_disconnect,
        )
        on_disconnect(
            client, None, mqtt.DisconnectFlags(False),
            ReasonCode(PacketTypes.DISCONNECT, "Normal disconnection"),
            Properties(PacketTypes.DISCONNECT),
        )
        assert not subscription.connected
        await subscription._async_disconnect()
        disconnect.assert_called_once_with(client)
        loop_stop.assert_called_once_with(client)
        assert subscription._client is None

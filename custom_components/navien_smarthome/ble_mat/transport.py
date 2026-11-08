"""Every use of Home Assistant's bluetooth integration and of bleak-retry-connector.

The imports stay inside the functions, so a cloud-only installation never loads them, and the
tests replace this module's functions and never import the real libraries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

SERVICE_UUID_ADVERTISED = "00001c0e-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_STATUS = "00001c0d-d102-11e1-9b23-2ce2a80100dd"
CHARACTERISTIC_HANDSHAKE = "00001c0d-d102-11e1-9b23-2ce2a80200dd"
CHARACTERISTIC_WRITE = CHARACTERISTIC_HANDSHAKE


class MissingCharacteristic(Exception):
    """The connected device did not report the mat's characteristics."""


@dataclass(frozen=True)
class DiscoveredMat:
    """A connectable device advertising the mat's service."""

    address: str
    name: str


def async_discovered_mats(hass: HomeAssistant) -> list[DiscoveredMat]:
    """List mats seen advertising; raises when bluetooth is unavailable."""
    from homeassistant.components.bluetooth import async_discovered_service_info

    return [
        DiscoveredMat(info.address, info.name or info.address)
        for info in async_discovered_service_info(hass, connectable=True)
        if SERVICE_UUID_ADVERTISED in info.service_uuids
    ]


def async_ble_device(hass: HomeAssistant, address: str) -> Any:
    """Find the device by its address; None while it is not seen."""
    from homeassistant.components.bluetooth import async_ble_device_from_address

    return async_ble_device_from_address(hass, address, connectable=True)


async def async_connect(device: Any, on_disconnect: Callable[[], None]) -> Any:
    from bleak_retry_connector import (
        BleakClientWithServiceCache,
        establish_connection,
    )

    return await establish_connection(
        BleakClientWithServiceCache,
        device,
        "navien_ble_mat",
        disconnected_callback=lambda _client: on_disconnect(),
    )


async def async_start_notify(
    client: Any, on_frame: Callable[[bytes], None]
) -> None:
    """Subscribe to both characteristics, or raise MissingCharacteristic."""
    uuids = (CHARACTERISTIC_STATUS, CHARACTERISTIC_HANDSHAKE)
    if any(client.services.get_characteristic(uuid) is None for uuid in uuids):
        raise MissingCharacteristic
    for uuid in uuids:
        await client.start_notify(uuid, lambda _char, data: on_frame(bytes(data)))


async def async_write(client: Any, frame: bytes) -> None:
    await client.write_gatt_char(CHARACTERISTIC_WRITE, frame, response=True)


async def async_clear_cache(client: Any) -> None:
    await client.clear_cache()


async def async_disconnect(client: Any) -> None:
    await client.disconnect()

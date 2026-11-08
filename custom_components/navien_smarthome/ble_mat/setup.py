"""Setting up, unloading and removing a Bluetooth mat entry."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .connection import MatConnection, async_remove_store

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_mat_entry(hass: HomeAssistant, entry: ConfigEntry[Any]) -> bool:
    """Set up the entities, then start the link in the background."""
    conn = MatConnection(hass, entry)
    await conn.async_load()
    entry.runtime_data = conn
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    conn.async_start()
    return True


async def async_unload_mat_entry(hass: HomeAssistant, entry: ConfigEntry[Any]) -> bool:
    """Always disconnect, whatever the platforms report."""
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_mat_entry(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    await async_remove_store(hass, entry.entry_id)


def mat_diagnostics(entry: ConfigEntry[Any]) -> dict[str, Any]:
    """Holds no address and no id."""
    conn: MatConnection = entry.runtime_data
    return conn.diagnostics()

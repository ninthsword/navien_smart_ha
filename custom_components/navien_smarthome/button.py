"""Buttons exist only for a Bluetooth mat; a cloud entry never loads this platform."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .ble_mat.entities import async_setup_mat_buttons


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    await async_setup_mat_buttons(hass, entry, async_add_entities)

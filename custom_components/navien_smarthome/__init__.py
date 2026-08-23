"""Navien Smart integration.

Talks straight to the servers the Navien Smart app uses. This is not an official API.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import NavienSmartApi, NavienSmartAuthError, NavienSmartError
from .const import CONF_HOME_SEQ
from .coordinator import NavienSmartCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    # Temperature mats (`0.5C`) become climate, stepped mats (`1.0L`) become select.
    # Both platforms are registered and each picks up only the devices on its own axis.
    Platform.CLIMATE,
    # Only the Airone target humidity uses this. Mat steps are `select` — a step is not
    # a continuous quantity.
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

NavienSmartConfigEntry = ConfigEntry[NavienSmartCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: NavienSmartConfigEntry) -> bool:
    """Log in with the stored credentials and stand up the coordinator."""
    # Form login needs to hold cookies, so this uses its own session rather than
    # polluting the shared one.
    http = async_create_clientsession(hass)
    api = NavienSmartApi(http, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])

    try:
        session = await api.async_login()
    except NavienSmartAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except NavienSmartError as err:
        raise ConfigEntryNotReady(str(err)) from err

    home_seq = entry.data.get(CONF_HOME_SEQ) or session.homes[0]["homeSeq"]

    coordinator = NavienSmartCoordinator(hass, entry, api, int(home_seq))
    await coordinator.async_config_entry_first_refresh()
    # Older devices never answer a status request, so seed them with the last known values.
    await coordinator.async_restore_state()

    if not coordinator.data and not coordinator.airone and not coordinator.boilers:
        _LOGGER.warning(
            "home %s 에서 지원 가능한 기기를 찾지 못했습니다. "
            "건너뛴 기기가 있으면 위 경고를 확인해 주세요.",
            home_seq,
        )

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Subscribe only once the entities exist.
    await coordinator.async_start_mqtt()
    entry.async_on_unload(coordinator.async_stop_mqtt)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NavienSmartConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: NavienSmartConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

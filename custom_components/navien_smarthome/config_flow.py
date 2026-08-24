"""Config flow.

The username and password go into the config entry; they are never hard-coded in YAML or
in the source. When the account holds more than one home, the user picks which one to use.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import NavienSmartApi, NavienSmartAuthError, NavienSmartError
from .const import CONF_HOME_SEQ, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
)


class NavienSmartConfigFlow(ConfigFlow, domain=DOMAIN):
    """Username and password, then a home to use if there is more than one."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._homes: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            try:
                session = await self._async_validate(username, password)
            except NavienSmartAuthError as err:
                _LOGGER.debug("인증 실패: %s", err)
                errors["base"] = "invalid_auth"
            except NavienSmartError as err:
                _LOGGER.debug("접속 실패: %s", err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(str(session.user_seq))
                self._abort_if_unique_id_configured()

                self._username = username
                self._password = password
                self._homes = session.homes

                if len(self._homes) == 1:
                    return self._create_entry(self._homes[0])
                return await self.async_step_home()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_home(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Shown only when the account holds more than one home."""
        if user_input is not None:
            chosen = int(user_input[CONF_HOME_SEQ])
            home = next(
                (h for h in self._homes if int(h.get("homeSeq", -1)) == chosen),
                self._homes[0],
            )
            return self._create_entry(home)

        options = {
            str(home.get("homeSeq")): (
                f"{home.get('nickname') or 'home'} "
                f"(기기 {len(home.get('devices') or [])}대)"
            )
            for home in self._homes
        }
        return self.async_show_form(
            step_id="home",
            data_schema=vol.Schema({vol.Required(CONF_HOME_SEQ): vol.In(options)}),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Entry point for re-authentication.

        Raising `ConfigEntryAuthFailed` makes HA open the flow with `SOURCE_REAUTH` and look
        for **`async_step_reauth`**. With only a confirmation step and no entry point here,
        the flow never opens, and a user who changed their password would have to delete
        and reinstall the integration — losing every entity id and all long-term statistics.

        `entry_data` is unused; the username is read from the existing entry below.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Used when the password changed or the session keeps failing."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            try:
                await self._async_validate(
                    entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except NavienSmartAuthError:
                errors["base"] = "invalid_auth"
            except NavienSmartError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def _async_validate(self, username: str, password: str) -> Any:
        http = async_create_clientsession(self.hass)
        api = NavienSmartApi(http, username, password)
        return await api.async_login()

    def _create_entry(self, home: dict[str, Any]) -> ConfigFlowResult:
        assert self._username is not None and self._password is not None
        return self.async_create_entry(
            title=f"나비엔 스마트 ({home.get('nickname') or 'home'})",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_HOME_SEQ: int(home["homeSeq"]),
            },
        )

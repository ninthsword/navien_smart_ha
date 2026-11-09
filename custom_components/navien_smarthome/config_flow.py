"""Config flow.

The username and password go into the config entry; they are never hard-coded in YAML or
in the source. When the account holds more than one home, the user picks which one to use.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import NavienSmartApi, NavienSmartAuthError, NavienSmartError
from .ble_mat import CONF_MAT_MODEL, MAT_MODELS, is_mat_entry, transport
from .ble_mat.connection import (
    CONF_MAT_ID,
    RegistrationNoStatus,
    RegistrationRefused,
    async_register_mat,
)
from .const import CONF_HEATING_ENTITIES, CONF_HOME_SEQ, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
)


def _form_schema(schema: vol.Schema) -> Any:
    """Pass a voluptuous schema as `data_schema` on every supported Home Assistant version.

    Home Assistant 2026.11 types `data_schema` as probatio's schema, while 2026.8 to 2026.10
    type it as voluptuous's; the form itself is the same on all of them.
    """
    return schema


class NavienSmartConfigFlow(ConfigFlow, domain=DOMAIN):
    """Username and password, then a home to use if there is more than one."""

    VERSION = 1

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """Both cloud and Bluetooth mat entries have options."""
        return True

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Offer the options for the entry's connection type."""
        return NavienSmartOptionsFlow()

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._homes: list[dict[str, Any]] = []
        self._mat_address: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the connection type; credentials go straight to the login step."""
        if user_input is not None:
            return await self.async_step_cloud(user_input)
        return self.async_show_menu(step_id="user", menu_options=["cloud", "bluetooth_mat"])

    async def async_step_bluetooth_mat(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a mat that is advertising and not configured yet."""
        if user_input is not None:
            self._mat_address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(self._mat_address)
            self._abort_if_unique_id_configured()
            return await self.async_step_bluetooth_mat_pair()
        try:
            found = transport.async_discovered_mats(self.hass)
        except Exception as err:
            _LOGGER.debug("블루투스 사용 불가: %s", type(err).__name__)
            return self.async_abort(reason="bluetooth_unavailable")
        configured = self._async_current_ids()
        options = {
            mat.address: f"{mat.name} ({mat.address})"
            for mat in found
            if mat.address not in configured
        }
        if not options:
            return self.async_abort(reason="no_devices")
        return self.async_show_form(
            step_id="bluetooth_mat",
            data_schema=_form_schema(
                vol.Schema({vol.Required(CONF_ADDRESS): vol.In(options)})
            ),
        )

    async def async_step_bluetooth_mat_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect only on submit, with the app closed and the mat in pairing mode."""
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._mat_address is not None
            try:
                registration = await async_register_mat(self.hass, self._mat_address)
            except RegistrationRefused:
                errors["base"] = "mat_refused"
            except RegistrationNoStatus:
                errors["base"] = "mat_no_status"
            except Exception as err:
                _LOGGER.debug("블루투스 매트 등록 실패: %s", type(err).__name__)
                errors["base"] = "mat_connect_failed"
            else:
                if not registration.is_double:
                    return self.async_abort(reason="single_mat")
                return self.async_create_entry(
                    title="나비엔 온수매트 (블루투스)",
                    data={
                        CONF_ADDRESS: self._mat_address,
                        CONF_MAT_ID: registration.issued_id.hex(),
                    },
                )
        return self.async_show_form(
            step_id="bluetooth_mat_pair",
            data_schema=_form_schema(vol.Schema({})),
            errors=errors,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The account login."""
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
            step_id="cloud", data_schema=_form_schema(STEP_USER_SCHEMA), errors=errors
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
            data_schema=_form_schema(
                vol.Schema({vol.Required(CONF_HOME_SEQ): vol.In(options)})
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Entry point for re-authentication.

        Raising `ConfigEntryAuthFailed` makes HA open the flow with `SOURCE_REAUTH` and look
        for **`async_step_reauth`**. With only a confirmation step and no entry point here,
        the flow never opens, and a user who changed their password would have to delete
        and reinstall the integration — losing every entity id and all long-term statistics.

        `entry_data` is otherwise unused; the username is read from the existing entry below.
        A Bluetooth entry has no account to log in to.
        """
        if CONF_MAT_ID in entry_data:
            return self.async_abort(reason="bluetooth_reauth_unsupported")
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
            data_schema=_form_schema(vol.Schema({vol.Required(CONF_PASSWORD): str})),
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


class NavienSmartOptionsFlow(OptionsFlow):
    """Select heating entities for a cloud entry or a product name for a mat."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the selection; the entry's update listener then reloads it.

        The stored list is a suggested value, not a default: voluptuous fills a default in
        when the field is omitted, and an omitted field has to clear the selection.
        """
        if is_mat_entry(self.config_entry):
            return await self.async_step_mat_model(user_input)
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_HEATING_ENTITIES: user_input.get(CONF_HEATING_ENTITIES, [])}
            )
        stored = self.config_entry.options.get(CONF_HEATING_ENTITIES, [])
        return self.async_show_form(
            step_id="init",
            data_schema=_form_schema(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_HEATING_ENTITIES,
                            description={"suggested_value": stored},
                        ): EntitySelector(
                            EntitySelectorConfig(domain="climate", multiple=True)
                        )
                    }
                )
            ),
        )

    async def async_step_mat_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the product name; omitting the optional field clears it."""
        if user_input is not None:
            return self.async_create_entry(
                data=(
                    {CONF_MAT_MODEL: user_input[CONF_MAT_MODEL]}
                    if CONF_MAT_MODEL in user_input
                    else {}
                )
            )
        stored = self.config_entry.options.get(CONF_MAT_MODEL)
        return self.async_show_form(
            step_id="mat_model",
            data_schema=_form_schema(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_MAT_MODEL,
                            description={"suggested_value": stored},
                        ): SelectSelector(
                            SelectSelectorConfig(
                                options=MAT_MODELS, mode=SelectSelectorMode.DROPDOWN
                            )
                        )
                    }
                )
            ),
        )

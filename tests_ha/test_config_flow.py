"""Exercise the config flow through Home Assistant's real flow manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.navien_smarthome.api import (
    NavienSmartAuthError,
    NavienSmartSession,
)
from custom_components.navien_smarthome.const import CONF_HOME_SEQ, DOMAIN


def session() -> NavienSmartSession:
    """Return a login result with one home."""
    return NavienSmartSession(
        access_token="token",
        refresh_token=None,
        user_id="user",
        account_seq=1,
        user_seq=42,
        homes=[{"homeSeq": 7, "nickname": "우리집", "devices": []}],
        aws=None,
    )


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """A valid account creates an entry without making a real HTTP request."""
    with patch(
        "custom_components.navien_smarthome.config_flow."
        "NavienSmartConfigFlow._async_validate",
        new=AsyncMock(return_value=session()),
    ) as validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.invalid", CONF_PASSWORD: "secret"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "나비엔 스마트 (우리집)"
    assert result["data"] == {
        CONF_USERNAME: "user@example.invalid",
        CONF_PASSWORD: "secret",
        CONF_HOME_SEQ: 7,
    }
    validate.assert_awaited_once_with("user@example.invalid", "secret")


async def test_user_flow_reports_invalid_auth(hass: HomeAssistant) -> None:
    """An authentication error stays in the form and does not create an entry."""
    with patch(
        "custom_components.navien_smarthome.config_flow."
        "NavienSmartConfigFlow._async_validate",
        new=AsyncMock(side_effect=NavienSmartAuthError("bad credentials")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data={CONF_USERNAME: "user@example.invalid", CONF_PASSWORD: "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_updates_password_and_reloads(
    hass: HomeAssistant, config_entry
) -> None:
    """The HA reauth source preserves the entry and replaces only its password."""
    with (
        patch(
            "custom_components.navien_smarthome.config_flow."
            "NavienSmartConfigFlow._async_validate",
            new=AsyncMock(return_value=session()),
        ) as validate,
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": config_entry.entry_id},
            data=config_entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-secret"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-secret"
    validate.assert_awaited_once_with("nobody@example.invalid", "new-secret")
    reload_entry.assert_awaited_once_with(config_entry.entry_id)

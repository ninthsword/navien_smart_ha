"""Run a real Home Assistant process with the Navien network boundary replaced by synthetic devices.

Usage: python devtools/ui_check/run_hass.py CONFIG_DIR
The Navien login, REST refresh, state restore and MQTT start/stop are replaced as
tests_ha/conftest.py's loaded_entry does, and the integration's own aiohttp session factory
returns a placeholder, so the process never contacts Navien. Home Assistant itself (core,
frontend, config flow, registries) runs unmodified through its own entry point.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from custom_components.navien_smarthome.api import (  # noqa: E402
    NavienSmartApi,
    NavienSmartSession,
)
from custom_components.navien_smarthome.coordinator import (  # noqa: E402
    NavienSmartCoordinator,
)
from custom_components.navien_smarthome.models import NavienDevice  # noqa: E402
from tests_ha.conftest import make_airone, make_boiler, make_mat  # noqa: E402

SESSION = NavienSmartSession(
    access_token="synthetic-token",
    refresh_token=None,
    user_id="synthetic-user",
    account_seq=1,
    user_seq=42,
    homes=[{"homeSeq": 7, "nickname": "검증용 집"}],
    aws=None,
)


async def _synthetic_update(
    coordinator: NavienSmartCoordinator,
) -> dict[str, NavienDevice]:
    """Return fresh synthetic devices on every refresh, as loaded_entry's fake_update does."""
    mat, airone, boiler = make_mat(), make_airone(), make_boiler()
    coordinator.raw_devices = [mat.raw, airone.raw, boiler.raw]
    coordinator.airone = {airone.device_id: airone}
    coordinator.boilers = {boiler.device_id: boiler}
    return {mat.device_id: mat}


def _placeholder_session(hass: Any) -> object:
    return object()


def main() -> int:
    config_dir = sys.argv[1]
    with (
        patch.object(
            NavienSmartApi, "async_login", new=AsyncMock(return_value=SESSION)
        ),
        patch(
            "custom_components.navien_smarthome.async_create_clientsession",
            new=_placeholder_session,
        ),
        patch(
            "custom_components.navien_smarthome.config_flow.async_create_clientsession",
            new=_placeholder_session,
        ),
        patch.object(NavienSmartCoordinator, "_async_update_data", _synthetic_update),
        patch.object(NavienSmartCoordinator, "async_restore_state", new=AsyncMock()),
        patch.object(NavienSmartCoordinator, "async_start_mqtt", new=AsyncMock()),
        patch.object(NavienSmartCoordinator, "async_stop_mqtt", new=AsyncMock()),
    ):
        from homeassistant.__main__ import main as hass_main

        sys.argv = ["hass", "--config", config_dir, "--skip-pip"]
        return hass_main()


if __name__ == "__main__":
    sys.exit(main())

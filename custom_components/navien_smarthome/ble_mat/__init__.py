"""A Bluetooth hot-water mat as a second connection type of this integration.

Nothing here shares code with the cloud API, its coordinator or its MQTT client.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_ADDRESS


def is_mat_entry(entry: Any) -> bool:
    """Tell a Bluetooth mat entry from a cloud account entry."""
    return CONF_ADDRESS in entry.data

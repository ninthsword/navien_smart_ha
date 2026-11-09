"""A Bluetooth hot-water mat as a second connection type of this integration.

Nothing here shares code with the cloud API, its coordinator or its MQTT client.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_ADDRESS

CONF_MAT_MODEL = "mat_model"
# The app's DeviceAddMatFragment list; the mat does not report its product name.
MAT_MODELS = [
    "EQM565",
    "EQM555",
    "EQM553",
    "EQM562",
    "EQM561",
    "EQM560",
    "EQM552",
    "EQM550",
    "EQM551",
    "EQM571",
    "EQM570",
    "EQM530",
    "EQH40/20DN",
]


def is_mat_entry(entry: Any) -> bool:
    """Tell a Bluetooth mat entry from a cloud account entry."""
    return CONF_ADDRESS in entry.data

"""Keep malformed MQTT envelopes out of the event-loop callback path."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from harness import Report
from navien_smarthome.boiler import extract_boiler_status, observe_boiler_message
from navien_smarthome.mqtt import (
    NavienSmartMqtt,
    extract_airone_reported,
    extract_reported,
)

r = Report()


def envelope(*, payload: object, topic: object = "shadow/update/accepted") -> bytes:
    return json.dumps({"topic": topic, "payload": payload}).encode()


r.section("malformed JSON envelopes are dropped")

for label, payload in (
    ("JSON null", b"null"),
    ("JSON array", b"[]"),
    ("JSON string", b'"event"'),
    ("JSON number", b"1"),
):
    mate_stats: dict[str, object] = {}
    try:
        result = extract_reported(payload, "home/mate/device", mate_stats)
    except Exception as err:  # noqa: BLE001 - this is the regression under test
        r.ok(False, f"{label} does not raise ({type(err).__name__})")
    else:
        r.ok(result is None, f"{label} is dropped")
        r.ok(
            mate_stats.get("dropped_not_object") == 1,
            f"{label} has a distinct mate diagnostic count",
        )

    airone_stats: dict[str, object] = {}
    boiler_stats: dict[str, object] = {}
    try:
        airone_result = extract_airone_reported(
            payload, "home/airone/device", airone_stats
        )
        boiler_result = extract_boiler_status(payload, boiler_stats)
        observation = observe_boiler_message(payload, "home/smarttok/device")
    except Exception as err:  # noqa: BLE001 - this is the regression under test
        r.ok(False, f"{label} is safe on Airone and boiler paths ({type(err).__name__})")
    else:
        r.ok(
            airone_result is None
            and boiler_result is None
            and observation["encoding"] == "json",
            f"{label} is dropped safely by Airone and boiler paths",
        )
        r.ok(
            airone_stats.get("dropped_not_object") == 1
            and boiler_stats.get("dropped_not_object") == 1,
            f"{label} has a distinct non-object diagnostic count",
        )


r.section("malformed payload and info shapes are dropped")

valid_reported = {"info": {"deviceId": "device"}, "heater": {}}
for label, payload in (
    ("payload list", []),
    ("payload string", "payload"),
    ("payload null", None),
    ("state list", {"state": []}),
    ("state string", {"state": "state"}),
    ("reported info list", {"state": {"reported": {"info": []}}}),
    ("reported info string", {"state": {"reported": {"info": "info"}}}),
    (
        "reported device id list",
        {"state": {"reported": {"info": {"deviceId": []}}}},
    ),
):
    try:
        result = extract_reported(envelope(payload=payload), "home/mate/device")
    except Exception as err:  # noqa: BLE001 - this is the regression under test
        r.ok(False, f"{label} does not raise ({type(err).__name__})")
    else:
        r.ok(result is None, f"{label} is dropped")


r.section("valid reported state remains accepted")

result = extract_reported(
    envelope(payload={"state": {"reported": valid_reported}}),
    "home/mate/device",
)
r.ok(result == ("device", valid_reported), "a valid reported envelope is unchanged")


r.section("Airone and boiler payload shapes are guarded")

for label, payload in (
    ("payload list", []),
    ("payload string", "payload"),
    ("payload null", None),
    ("response list", {"response": []}),
    ("response string", {"response": "response"}),
):
    encoded = envelope(payload=payload)
    airone_stats = {}
    boiler_stats = {}
    try:
        airone_result = extract_airone_reported(
            encoded, "home/airone/device", airone_stats
        )
        boiler_result = extract_boiler_status(encoded, boiler_stats)
    except Exception as err:  # noqa: BLE001 - this is the regression under test
        r.ok(False, f"{label} does not raise on Airone or boiler ({type(err).__name__})")
    else:
        r.ok(
            airone_result is None and boiler_result is None,
            f"{label} is dropped by Airone and boiler parsers",
        )

airone_valid = {"roomController": {"running": 1}}
airone_stats = {}
airone_result = extract_airone_reported(
    envelope(payload={"reported": airone_valid}),
    "home/airone/device",
    airone_stats,
)
r.ok(
    airone_result == ("device", airone_valid) and airone_stats.get("accepted") == 1,
    "valid Airone reported state remains accepted",
)

physical_id = "001122334455"
boiler_status = {"operationMode": 2}
boiler_stats = {}
boiler_result = extract_boiler_status(
    envelope(
        payload={
            "response": {"status": boiler_status, "macAddress": physical_id}
        }
    ),
    boiler_stats,
)
r.ok(
    boiler_result == (physical_id, boiler_status)
    and boiler_stats.get("accepted") == 1,
    "valid boiler status remains accepted",
)

identifier_key = "0011223344556677"
unknown_stats: dict[str, object] = {}
with patch("navien_smarthome.mqtt._LOGGER.warning") as warning:
    extract_airone_reported(
        envelope(payload={"reported": {identifier_key: {"value": 1}}}),
        "home/airone/device",
        unknown_stats,
    )
unknown_keys = unknown_stats.get("last_unknown_shape_keys")
r.ok(
    isinstance(unknown_keys, list)
    and identifier_key not in unknown_keys
    and unknown_keys == ["<key:0>"],
    "unknown Airone shape diagnostics redact identifier-shaped keys",
)
r.ok(
    warning.call_count == 1 and identifier_key not in repr(warning.call_args),
    "issue-directed Airone warning arguments contain no identifier-shaped key",
)

separated_identifier_key = "_".join(("SN1234", "5678ABCD"))
separated_stats: dict[str, object] = {}
with patch("navien_smarthome.mqtt._LOGGER.warning") as warning:
    extract_airone_reported(
        envelope(
            payload={"reported": {separated_identifier_key: {"value": 1}}}
        ),
        "home/airone/device",
        separated_stats,
    )
r.ok(
    separated_stats.get("last_unknown_shape_keys") == ["<key:0>"]
    and separated_identifier_key not in repr(warning.call_args),
    "separated identifier keys are redacted from Airone warnings and diagnostics",
)


r.section("MQTT callback routing preserves diagnostic counter names")


class ImmediateLoop:
    def call_soon_threadsafe(self, callback: object, *args: object) -> None:
        callback(*args)  # type: ignore[operator]


def mqtt_client(**handlers: object) -> NavienSmartMqtt:
    return NavienSmartMqtt(
        SimpleNamespace(loop=ImmediateLoop()),  # type: ignore[arg-type]
        7,
        42,
        {"mate", "airone", "smarttok"},
        AsyncMock(return_value=None),
        handlers.get("on_reported", lambda *_: None),  # type: ignore[arg-type]
        on_airone_reported=handlers.get("on_airone_reported"),  # type: ignore[arg-type]
        on_boiler_reported=handlers.get("on_boiler_reported"),  # type: ignore[arg-type]
        on_boiler_observation=handlers.get("on_boiler_observation"),  # type: ignore[arg-type]
    )


mate = mqtt_client()
mate._on_message(
    None,
    None,
    SimpleNamespace(topic="7/mate/device", payload=b"null"),
)
r.ok(
    mate.stats == {"mate_received": 1, "mate_dropped_not_object": 1},
    "mate callback exposes the distinct non-object counter",
)

boiler = mqtt_client(on_boiler_observation=lambda *_: None)
boiler._on_message(
    None,
    None,
    SimpleNamespace(
        topic="7/smarttok/device",
        payload=envelope(
            payload={
                "response": {"status": boiler_status, "macAddress": physical_id}
            }
        ),
    ),
)
r.ok(
    boiler.stats.get("boiler_received") == 1
    and boiler.stats.get("boiler_accepted") == 1
    and boiler.stats.get("boiler_dropped_no_handler") == 1,
    "boiler callback prefixes parser and missing-handler counters",
)

airone = mqtt_client(on_airone_reported=lambda *_: None)
airone._on_message(
    None,
    None,
    SimpleNamespace(
        topic="7/airone/device",
        payload=envelope(payload={"reported": {identifier_key: {"value": 1}}}),
    ),
)
r.ok(
    airone.stats.get("airone_received") == 1
    and airone.stats.get("airone_dropped_unknown_shape") == 1
    and airone.stats.get("airone_last_unknown_shape_keys") == ["<key:0>"],
    "Airone callback prefixes counters and preserves safe unknown keys",
)

airone_no_handler = mqtt_client()
airone_no_handler._on_message(
    None,
    None,
    SimpleNamespace(
        topic="7/airone/device",
        payload=envelope(payload={"reported": airone_valid}),
    ),
)
r.ok(
    airone_no_handler.stats
    == {"airone_received": 1, "airone_dropped_no_handler": 1},
    "Airone callback records a missing handler without parsing values",
)

sys.exit(r.finish())

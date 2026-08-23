"""Diagnostics export.

Downloaded from Settings -> Devices & services -> Navien Smart -> menu -> Download
diagnostics (the Korean translation reads 통계정보 다운로드). The point is that a user
reporting a problem should not have to know what to attach.

**Airone and boiler payloads go in whole** — that is the only evidence there is for
widening support. Devices outside the scope (commercial units, wall pads) are summarised
only; there is no reason to export data nobody will use.

Every identifier is redacted, nicknames included — those often carry a person's name.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import NavienSmartConfigEntry
from .boiler import BOILER_GAS_REFRESH_SECONDS, BOILER_SILENCE_REFRESH_SECONDS
from .const import (
    AIRONE_INFERRED_UNITS,
    AIRONE_SENSOR_KINDS,
    IOT_ENDPOINT,
    KNOWN_UNITS,
    REPORT_WANTED_SERVICE_CODES,
    SERVICE_NAMES,
    SUPPORTED_SERVICE_CODES,
)

# Redaction is keyed by name. `heater.left` and `right` are structure and must not be
# touched, so nicknames are redacted through their parent key (`nickName`, `userInfo`).
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "accessToken",
    "refreshToken",
    "accountId",
    "clientId",
    "clientID",
    "defaultClientId",
    "deviceId",
    "eventId",
    "localIp",
    "mac",
    "macAddress",
    "mqttTopicKey",
    "nickName",
    "regionCode",
    "sessionId",
    "sessionID",
    "ssid",
    "requestTopic",
    "responseTopic",
    "boilerControllerSerialNumber",
    "thingArn",
    "thingId",
    "thingName",
    "userId",
    "userInfo",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NavienSmartConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data

    supported: list[dict[str, Any]] = []
    report_wanted: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []

    for raw in coordinator.raw_devices:
        service_code = raw.get("serviceCode")
        if service_code in SUPPORTED_SERVICE_CODES:
            supported.append(async_redact_data(raw, TO_REDACT))
        elif service_code in REPORT_WANTED_SERVICE_CODES:
            # Candidates for widening support. The structure has to be visible, so the raw
            # payload goes in.
            report_wanted.append(async_redact_data(raw, TO_REDACT))
        else:
            out_of_scope.append(
                {
                    "serviceCode": service_code,
                    "service": SERVICE_NAMES.get(service_code),
                    "modelName": raw.get("modelName"),
                    "modelCode": raw.get("modelCode"),
                }
            )

    payload = {
        "integration": {
            "iot_endpoint": IOT_ENDPOINT,
            "mqtt_connected": coordinator.mqtt_connected,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            # **Check first whether polling is running at all.** If this is far larger than
            # the interval, the reason the values are stale is us, not the device or server.
            # **Read `poll_attempts` first**: from the last success time alone, "we keep
            # failing" and "HA never calls us" look identical.
            "poll_attempts": coordinator.poll_attempts,
            "last_poll_seconds_ago": coordinator.poll_age,
            "poll_failures": coordinator.poll_failures,
            # What the last failure was — the exception name and message, nothing more.
            "poll_last_error": coordinator.poll_last_error,
            # **Polling can be switched off on the HA side.**
            #
            #   Settings -> Devices & services -> Navien Smart -> menu -> System options
            #   -> "Enable polling for updates"
            #
            # Switched off, HA stops periodic refresh entirely. MQTT stays alive because we
            # connect to it ourselves — so **mode and power look fine while air quality
            # freezes**. In that state only `last_poll_seconds_ago` grows while
            # `poll_failures` stays 0, and without this field the reason has to be asked for.
            "polling_disabled_in_ha": bool(
                getattr(entry, "pref_disable_polling", False)
            ),
            "known_control_units": list(KNOWN_UNITS),
            # **Tells apart the causes of "no state arrives" without any logging.**
            # A received count of 0 means nothing arrives; a non-zero discarded count means it
            # arrives and cannot be used, and then `airone_last_unknown_shape_keys` says what
            # shape it had. Counts and key names only, so nothing personal.
            "mqtt_messages": coordinator.mqtt_stats,
            "boiler_silence_refresh_seconds": BOILER_SILENCE_REFRESH_SECONDS,
            "boiler_silence_timers": len(coordinator._boiler_silence_unsubs),
            "boiler_silence_requests": coordinator.boiler_silence_requests,
            "boiler_silence_failures": coordinator.boiler_silence_failures,
            "boiler_gas_refresh_seconds": BOILER_GAS_REFRESH_SECONDS,
            "boiler_gas_timers": len(coordinator._boiler_gas_unsubs),
            "boiler_gas_requests": coordinator.boiler_gas_requests,
            "boiler_gas_failures": coordinator.boiler_gas_failures,
            "boiler_gas_statistics_failures": (
                coordinator.boiler_gas_statistics_failures
            ),
        },
        "counts": {
            "total": len(coordinator.raw_devices),
            "supported": len(supported),
            "report_wanted": len(report_wanted),
            "out_of_scope": len(out_of_scope),
        },
        # Not the raw boiler payload: a structural observation with strings, topics, large
        # numbers and binary stripped out.
        "boiler_observations": list(coordinator.boiler_observations),
        "entities": [_entity_view(device) for device in (coordinator.data or {}).values()],
        # Airone is unverified on a real device. The parsed result goes in as-is, to serve as
        # evidence in a report.
        "airone_entities": [
            _airone_view(device, device.device_id in coordinator.restored_devices)
            for device in coordinator.airone.values()
        ],
        "boiler_entities": [
            _boiler_view(device) for device in coordinator.boilers.values()
        ],
        "supported_devices": supported,
        # If this entry is not empty, attach it to the issue as it stands.
        "report_wanted_devices": report_wanted,
        "out_of_scope_devices": out_of_scope,
    }
    # **Redacting by key name is not enough.** An identifier can sit inside a value, as in
    # `requestTopic: "dt/rc/7/1097BD3F5CACB84E/did"`. `requestTopic` was not on the redaction
    # list, and a reporter posted that line publicly.
    #
    # Adding that key to the list is not the end of it — **the next leak has to be stopped
    # too.** The identifier values are collected first, then those strings are scrubbed from
    # the whole result. The topic shape (`dt/rc/7/**REDACTED**/did`) survives, so no evidence
    # for widening support is lost.
    return _scrub(payload, _identifiers(coordinator.raw_devices))


def _identifiers(raw_devices: list[dict[str, Any]]) -> list[str]:
    """Collect the identifier **values** that have to be redacted.

    The targets are the strings carried by the keys in `TO_REDACT`. Anything shorter than 8
    characters is dropped: a short value can occur by chance inside another string, and
    scrubbing it would corrupt a perfectly good value.
    """
    found: set[str] = set()

    def walk(value: Any, key: str | None) -> None:
        if isinstance(value, dict):
            for inner_key, inner in value.items():
                walk(inner, inner_key)
        elif isinstance(value, list):
            for inner in value:
                walk(inner, key)
        elif isinstance(value, str) and key in TO_REDACT:
            text = value.strip()
            if len(text) >= 8:
                found.add(text)

    walk(raw_devices, None)
    # Longest first. When a short value is part of a longer one, the reverse order leaves a
    # fragment behind.
    return sorted(found, key=len, reverse=True)


def _scrub(value: Any, secrets: list[str]) -> Any:
    """Walk the whole result and scrub the identifier strings."""
    if not secrets:
        return value
    if isinstance(value, dict):
        return {key: _scrub(inner, secrets) for key, inner in value.items()}
    if isinstance(value, list):
        return [_scrub(inner, secrets) for inner in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, "**REDACTED**")
        return value
    return value


def _entity_view(device: Any) -> dict[str, Any]:
    """How the integration parsed a device — used to find where a misreading started."""
    heat = device.heat_control
    cool = device.cool_control
    return {
        "service_code": device.service_code,
        "model_code": device.model_code,
        "model_name": device.model_name,
        "model_type": device.model_type,
        "capacity": device.capacity,
        "zones": list(device.zones),
        "is_double": device.is_double,
        "is_four_season": device.is_four_season,
        "season": device.season,
        "is_cooling": device.is_cooling,
        "has_unknown_season": device.has_unknown_season,
        # The values the app keeps on its device settings screen. The lock is read-only; the
        # volume can be written.
        "child_lock": device.child_lock,
        "volume": device.volume,
        # Which groups of state have arrived. A four-season model once sent a partial response
        # that omitted `season` and `operationMode` (v0.9.0).
        "reported_keys": sorted(device.reported or {}),
        "reported_heater_zones": sorted((device.reported or {}).get("heater") or {}),
        # **Settling cooling requires seeing the sequence.** Whether the device returns 26.0
        # after 26.0 was sent, whether both sides follow when only one was sent, and what
        # `season` actually changes to cannot be told from a single moment's values.
        # `at` is not an absolute time but **a seconds-scale ruler for reading intervals**
        # (seconds since the device booted). The value itself means nothing; only the
        # differences between rows are used. Temperatures and steps only, so nothing personal.
        "command_log": list(device.command_log),
        "state_log": list(device.state_log),
        "available": device.available,
        "operation_mode": device.operation_mode,
        "error_code": device.error_code,
        "heat_control": heat.as_diagnostics() if heat else None,
        "cool_control": cool.as_diagnostics() if cool else None,
        "control_unit_known": bool(heat and heat.is_known),
        "functions": {
            "power_ctrl": device.has_power_ctrl,
            "beep": device.has_beep,
            "lock_mode": device.has_lock_mode,
            "power_saving": device.has_power_saving,
            "sleep_mode": device.has_sleep_mode,
            "sleep_durations_minutes": device.sleep_durations,
            "schedule_kinds": list(device.schedule_kinds),
        },
        "zone_state": {
            zone: {
                "setting": device.zone_setting(zone),
                "current": device.zone_current(zone),
                "enabled": device.zone_enabled(zone),
            }
            for zone in device.zones
        },
    }


def _numbers_only(raw: Any) -> dict[str, Any]:
    """Keep **only numbers and booleans** from a dictionary.

    This is the net that lets values be seen without knowing their names. Which key carries
    the target humidity is still unknown, so no key can be named.

    Dropping strings wholesale is what does the redacting: device ids, SSIDs, nicknames and
    MACs are all strings and none of them get through. **Listing keys to redact leaks the
    moment a new key appears**, whereas this approach keeps a new string key out by default.
    """
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if isinstance(value, (int, float, bool)) and key not in TO_REDACT
    }


def _gas_history_view(device: Any) -> dict[str, Any]:
    """The span the history written into statistics covers.

    These two lines are the first thing to read in a "no history appears" report. A bucket
    count of 0 means the server sent no array; monthly present with daily at 0 means the last
    two months are missing. The two cases point at different places to look.
    """
    buckets = device.gas_history()
    if not buckets:
        return {"gas_history_buckets": 0}
    return {
        "gas_history_buckets": len(buckets),
        "gas_history_daily": sum(1 for bucket in buckets if not bucket.monthly),
        "gas_history_monthly": sum(1 for bucket in buckets if bucket.monthly),
        "gas_history_span": (
            f"{buckets[0].start.isoformat()}..{buckets[-1].start.isoformat()}"
        ),
    }


def _boiler_view(device: Any) -> dict[str, Any]:
    """The parsed boiler result — scale factors and whether state arrived, with no identifiers."""
    communication_age = device.communication_age()
    return {
        "model_code": device.model_code,
        "model_name": device.model_name,
        "available": device.available,
        "status_received": bool(device.status),
        "last_communication_seconds_ago": (
            None if communication_age is None else round(communication_age, 1)
        ),
        "status_keys": sorted(device.status),
        "operation_mode": device.operation_mode,
        "error_code": device.error_code,
        "sub_error_code": device.sub_error_code,
        "indoor_temperature": device.indoor_temperature,
        "supply_temperature": device.supply_temperature,
        "return_temperature": device.return_temperature,
        "hot_water_temperature": device.hot_water_temperature,
        "ondol_target_temperature": device.ondol_target_temperature,
        "hot_water_target_temperature": device.hot_water_target_temperature,
        "indoor_humidity": device.indoor_humidity,
        "gas_received": bool(device.gas_meter),
        # Whether the history reached statistics is judged from how many buckets span which
        # range. Dates alone are not identifying, and no usage figures are kept.
        **_gas_history_view(device),
        "gas_arrays": sorted(
            key for key in device.gas_meter if key.startswith("gasMeter")
        ),
        "outside_temperature": device.outside_temperature,
        "hot_water_flow_rate": device.hot_water_flow_rate,
        "heating_flow_rate": device.heating_flow_rate,
        "hot_water_running": device.hot_water_running,
        "fault_status": device.fault_status,
        "heating_intensity": device.heating_intensity,
        "repeat_reservation_interval": device.repeat_reservation_interval,
        "day_cycle_reservation_length": (
            None if device.day_cycle_reservation is None
            else len(device.day_cycle_reservation)
        ),
        # A lead on command codes whose meaning is still unknown: changing the mode in the app
        # makes the room controller echo that command code back as state — a way to confirm it
        # without guessing.
        "observed_commands": dict(sorted(device.observed_commands.items())),
        "gas_total_month": device.gas_total_month,
        "gas_heating_month": device.gas_heating_month,
        "gas_hot_water_month": device.gas_hot_water_month,
        # Flags whose meaning is not settled keep their raw numbers and nothing more.
        "status_numbers": _numbers_only(device.status),
        "feature_numbers": _numbers_only(device.feature),
    }


def _airone_view(device: Any, restored: bool = False) -> dict[str, Any]:
    """How Airone was parsed.

    **This area is unverified on a real device, which makes this table the heart of a
    report.** It places the combinations the server declared next to the options the
    integration built, so a mismatch is visible at a glance.
    """
    return {
        "service_code": device.service_code,
        "model_code": device.model_code,
        "model_name": device.model_name,
        "odu_model_code": device.odu_model_code,
        "is_v2_generation": device.is_v2_generation,
        # **Values that tell "no state arrives" from "it arrives and cannot be attached".**
        # They carry relationships and presence, never the values themselves.
        #
        # Whether the identifier used in the topic matches the one in the device list — an
        # all-in-one room controller and a split unit can differ here. Redaction makes the two
        # ids impossible to compare by eye, so the answer is recorded separately.
        "physical_id_same_as_device_id": (
            device.physical_device_id == device.device_id
        ),
        # Whether state ever arrived, and which groups of it.
        "reported_received": bool(device.reported),
        # Whether the value was restored. Until the device pushes a fresh one it is
        # provisional, so something showing as on may in fact be off.
        "state_restored": restored,
        "reported_keys": sorted(device.reported or {}),
        "reported_room_controller_keys": sorted(
            (device.reported or {}).get("roomController") or {}
        ),
        # **Key names alone were not enough.** Finding where the target humidity arrives means
        # seeing values, so **only numbers** go in and strings are dropped wholesale. Nicknames
        # (`zoneNickname`), identifiers and SSIDs are all strings and none pass this net. Only
        # booleans, integers and floats leave.
        "reported_room_controller_numbers": _numbers_only(
            (device.reported or {}).get("roomController")
        ),
        # `additionalData` is a list whose meaning depends on where it sits (humidity 40-65
        # inside a mode, 0-4 at controller level). Which one arrives has to be observed.
        "reported_additional_data": [
            _numbers_only(item)
            for item in (
                ((device.reported or {}).get("roomController") or {}).get(
                    "additionalData"
                )
                or []
            )
            if isinstance(item, dict)
        ],
        "last_humidity_remembered": device.last_humidity,
        # **A record kept for its ordering.** Whether the device reverts a humidity sent along
        # with a mode change cannot be told from a single moment's values.
        # `at` is a ruler for intervals, not an absolute time. Nothing personal.
        "command_log": list(device.command_log),
        "humidity_log": list(device.humidity_log),
        "available": device.available,
        "zone_id": device.zone_id,
        "running": device.running,
        "running_name": device.running_name,
        "mode": device.mode,
        "option": device.option,
        "mode_label": device.mode_label,
        "air_volume": device.air_volume,
        "wind_label": device.wind_label,
        "target_humidity": device.target_humidity,
        "error_code": device.error_code,
        "filters": list(device.filters),
        "air_sensor_kinds": list(device.sensor_kinds),
        # When the kinds arriving now differ from **the kinds ever seen**, the server left
        # some out of this poll — which is what happens when the air monitor drops out.
        "air_sensor_kinds_known": list(device.known_sensor_kinds),
        "air_sensor_kinds_missing": [
            kind for kind in device.known_sensor_kinds if kind not in device.sensor_kinds
        ],
        # **Values that resolve "the app disagrees with HA".** Air quality is re-read over
        # REST every five minutes, and since an empty response stopped clearing values, a
        # stalled refresh leaves the old numbers on screen. The three fields below separate "the
        # room is simply quiet" from "we cannot read it". Seconds and counts only, so nothing
        # personal.
        # **Whether this device is asked about air quality at all.** With no sensor anywhere,
        # it is not asked — and the fields below reading 0 or null is then not a fault.
        "wants_air_sensors": device.wants_air_sensors,
        "declared_sensor_count": (
            None if device.declared_sensors is None else len(device.declared_sensors)
        ),
        "air_sensor_changed_seconds_ago": device.air_sensor_age,
        "air_sensor_empty_responses": device.air_sensor_empty,
        "air_sensor_read_errors": device.air_sensor_errors,
        "air_sensor_unchanged_reads": device.air_sensor_unchanged,
        # Items whose unit was a judgement call because the app never revealed one. A report
        # saying it is wrong is what corrects them.
        "air_sensor_units": {
            kind: {
                "unit": AIRONE_SENSOR_KINDS[kind][1],
                "inferred": kind in AIRONE_INFERRED_UNITS,
                "value": (device.air_sensors.get(kind) or {}).get("value"),
                "level": (device.air_sensors.get(kind) or {}).get("level"),
            }
            for kind in device.sensor_kinds
        },
        # **Redaction applies to tables we build ourselves too.** `supported_devices` passes
        # through `async_redact_data`, but this table is assembled here, and left alone it ships
        # the air monitor's `deviceId` verbatim — which is exactly what happened once.
        "air_monitors": [
            async_redact_data(monitor, TO_REDACT) for monitor in device.air_monitors
        ],
        "modes_from_server": [
            {
                "mode": mode.mode,
                "option": mode.option,
                "air_volume": mode.air_volume,
                "configurable": mode.configurable,
                "humidity_min": mode.humidity_min,
                "humidity_max": mode.humidity_max,
            }
            for mode in device.modes
        ],
        "selectable_modes": [
            {"mode": m.mode, "option": m.option, "label": m.label}
            for m in device.selectable_modes
        ],
        "fan_choices": {
            f"{m.mode}:{m.option}": [
                {"option": c.option, "air_volume": c.air_volume, "label": c.label}
                for c in device.fan_choices(m.mode, m.option)
            ]
            for m in device.selectable_modes
        },
        "current_fan_label": device.current_fan_label(),
    }

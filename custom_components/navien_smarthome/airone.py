"""Airone (ventilation and air purification) device model.

**The scheme differs from a mat.** A mat uses an AWS shadow (`state.desired` /
`state.reported`), while Airone exchanges messages directly on `cmd/rc/v2/...` topics. So
the two are kept in separate classes rather than merged — leaving the verified mat path
undisturbed comes first.

Every field name and value here was confirmed in the app (`AironeConstants`, `PubSubData`,
`ModeDid`, `RoomControllerStatus`). **State, control and target humidity were confirmed by
real-device reports** (split room controller 1901, all-in-one room controller 1900). Nothing
is confirmed for any other model.

Anything whose value scheme is unknown is left empty rather than filled in.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Final

from .const import (
    AIRONE_AUTO_DRY_TYPE,
    AIRONE_HUMIDITY_REPORT_TYPE,
    AIRONE_HUMIDITY_TYPE,
    AIRONE_LEVEL_NAMES,
    AIRONE_MODE_BYPASS,
    AIRONE_MODE_NAMES,
    AIRONE_MODE_SLEEP_LABEL,
    AIRONE_MODES_WITH_HUMIDITY,
    AIRONE_OPTION_NAMES,
    AIRONE_OPTION_NONE,
    AIRONE_OPTION_SLEEP,
    AIRONE_OPTIONS_WITH_WIND,
    AIRONE_RUN_AUTO_DRY,
    AIRONE_RUN_AWAY,
    AIRONE_RUN_NAMES,
    AIRONE_RUN_OFF,
    AIRONE_RUN_ON,
    AIRONE_SELECTABLE_AIR_VOLUMES,
    AIRONE_SENSOR_ALIASES,
    AIRONE_SENSOR_KINDS,
    AIRONE_V2_MIN_MODEL_CODE,
    AIRONE_WIND_NAMES,
    LEGACY_BYPASS_MODEL_PREFIXES,
    LEGACY_DEFAULT_AIR_VOLUMES,
    LEGACY_MODE_DID,
    airone_mode_label,
)

_LOGGER = logging.getLogger(__name__)

# How many records diagnostics keeps. The point is to see the ordering, so it need not be long.
_LOG_KEEP = 8


def _dig(source: Any, *keys: str) -> Any:
    current = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _strip_capability_fields(incoming: dict[str, Any]) -> dict[str, Any]:
    """Strip the **capability descriptors** that arrive mixed into a status response.

    A device sometimes answers a `status` request with **the whole DID document**. Inside it,
    `roomController.mode` is not the current operating mode (an integer) but **an array of
    supported combinations**. Merging that as-is lets the array overwrite the mode number
    that just arrived, and the mode disappears.

    Report #12 (`NRT-530Z3`) shows exactly that:

        mode: 4     <- change-mode response
        mode: null  <- nine seconds later a status response overwrote it with an array

    From then on the mode stayed empty, and the fan-speed select concluded there was nothing
    to choose and went **`unavailable`**. That is what "the device looks dead" really was.

    The capability list has already been read from the REST device list and is held in
    `modes`, so it is discarded here — only state survives.

    **`additionalData` is the same trap.** As state it is a list carrying values
    (`{"type": 3, "value": 40}`); in a DID document it is a table of ranges
    (`{"type": 1, "min": 0, "max": 4}`). Letting the range table overwrite it loses the
    target humidity already read. **A list with no values at all is not state and is
    discarded.**
    """
    controller = incoming.get("roomController")
    if not isinstance(controller, dict):
        return incoming

    inner = dict(controller)
    changed = False

    if isinstance(inner.get("mode"), list):
        del inner["mode"]
        changed = True

    extra = inner.get("additionalData")
    if isinstance(extra, list) and not any(
        isinstance(item, dict) and "value" in item for item in extra
    ):
        del inner["additionalData"]
        changed = True

    if not changed:
        return incoming
    trimmed = dict(incoming)
    trimmed["roomController"] = inner
    return trimmed


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# `running` was confirmed to arrive as an integer. The server may still send a boolean or a
# string depending on device and firmware, so those are read too — **only the reading side is
# widened.** What goes out is always an integer.
_RUNNING_TEXT: Final = {
    "on": AIRONE_RUN_ON, "run": AIRONE_RUN_ON, "running": AIRONE_RUN_ON,
    "true": AIRONE_RUN_ON, "y": AIRONE_RUN_ON, "yes": AIRONE_RUN_ON,
    "off": AIRONE_RUN_OFF, "stop": AIRONE_RUN_OFF, "stopped": AIRONE_RUN_OFF,
    "false": AIRONE_RUN_OFF, "n": AIRONE_RUN_OFF, "no": AIRONE_RUN_OFF,
    "away": AIRONE_RUN_AWAY, "out": AIRONE_RUN_AWAY,
}


def _as_running(value: Any) -> int | None:
    """Coerce a running-state value to an integer, accepting booleans and strings too."""
    if value is None:
        return None
    if isinstance(value, bool):
        return AIRONE_RUN_ON if value else AIRONE_RUN_OFF
    if (number := _as_int(value)) is not None:
        # 0 is read as stopped. The server uses 1/2/3, but if some device sends 0, stopped is
        # a better reading than unknown — a running device sends 1.
        return AIRONE_RUN_OFF if number == 0 else number
    if isinstance(value, str):
        return _RUNNING_TEXT.get(value.strip().lower())
    return None


def as_number(value: Any) -> float | None:
    """A number if it reads as one, otherwise None. An empty string counts as no value.

    Air-quality values are numbers for some kinds and strings for others, so this decision is
    needed — the app **displays** `tvoc`, `radon` and `total` as grades, but numbers arrive.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def level_text(raw: dict[str, Any]) -> str | None:
    """Render an air-quality grade in Korean, passing through what the server already sends in Korean."""
    level = raw.get("level")
    if level is None or level == "":
        return None
    if isinstance(level, str) and not level.lstrip("-").isdigit():
        return level
    try:
        return AIRONE_LEVEL_NAMES.get(int(level))
    except (TypeError, ValueError):
        return str(level)


def _version_text(current: Any) -> str | None:
    if current is None:
        return None
    text = str(current).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class AironeMode:
    """One operating combination the server declared (`ModeDid`).

    A `mode` and `option` pair corresponds to one button on the app's screen. Whether
    `air_volume` is **a single value or a bitmask was never confirmed**, so any value absent
    from the known table is discarded (spec 6-5).
    """

    mode: int
    option: int
    # The current value (or a default). **Not the list of choices** — confirmed by a
    # real-device report. The list is `supported_air_volumes`.
    air_volume: int | None
    # The server declares which fan speeds this combination allows (`supportedAirVolumes`).
    # The field does not exist in the APK classes. Absent, it is an empty tuple.
    supported_air_volumes: tuple[int, ...]
    # **This does not mean "can this mode be selected".** In real-device responses the items
    # with `configurable: true` were exactly those carrying `supportedAirVolumes` or
    # `additionalData` — that is, "can fan speed or humidity be adjusted within this mode".
    # Auto, cooking, sleep, turbo and saving all arrive as false, yet all are selectable in
    # the app. So the mode list is never filtered by this value; it is kept for diagnostics.
    configurable: bool
    humidity_min: int | None
    humidity_max: int | None

    @property
    def key(self) -> tuple[int, int]:
        return (self.mode, self.option)

    @property
    def label(self) -> str:
        return airone_mode_label(self.mode, self.option)

    @property
    def wants_wind(self) -> bool:
        return self.option in AIRONE_OPTIONS_WITH_WIND

    @property
    def wants_humidity(self) -> bool:
        return (
            self.mode in AIRONE_MODES_WITH_HUMIDITY
            and self.humidity_min is not None
            and self.humidity_max is not None
            and self.humidity_min < self.humidity_max
        )

    @classmethod
    def parse(cls, raw: Any) -> AironeMode | None:
        if not isinstance(raw, dict):
            return None
        mode = _as_int(raw.get("name"))
        if mode is None:
            return None
        option = _as_int(raw.get("option"))
        wind = _as_int(raw.get("airVolume"))
        if wind is not None and wind not in AIRONE_WIND_NAMES:
            # A real device sends 0 for "not applicable". Values absent from the confirmed
            # table are not used.
            _LOGGER.debug(
                "에어원 airVolume %s 는 확인된 값이 아니라 무시합니다 (mode=%s)", wind, mode
            )
            wind = None

        supported = tuple(
            value
            for item in raw.get("supportedAirVolumes") or []
            if (value := _as_int(item)) is not None and value in AIRONE_WIND_NAMES
        )

        low = high = None
        for extra in raw.get("additionalData") or []:
            if not isinstance(extra, dict):
                continue
            if _as_int(extra.get("type")) != AIRONE_HUMIDITY_TYPE:
                continue
            low = _as_int(extra.get("min"))
            high = _as_int(extra.get("max"))
            break

        return cls(
            mode=mode,
            option=AIRONE_OPTION_NONE if option is None else option,
            air_volume=wind,
            supported_air_volumes=supported,
            configurable=bool(raw.get("configurable")),
            humidity_min=low,
            humidity_max=high,
        )


@dataclass(frozen=True, slots=True)
class AironeModeChoice:
    """One entry in the operating-mode list.

    The app treats mode and fan speed as **separate axes**. Turbo, saving and baseline belong
    to fan speed; sleep is the one exception that belongs to mode
    (`AironeModeCode.rawToUi`).
    """

    mode: int
    option: int
    label: str

    @property
    def is_sleep(self) -> bool:
        return self.option == AIRONE_OPTION_SLEEP


@dataclass(frozen=True, slots=True)
class AironeFanChoice:
    """One entry in the fan-speed list.

    With `option == 1` the `airVolume` decides the speed; otherwise the option itself stands
    in for it (turbo, saving, baseline). This is the same rule as the app's `labelFor`.
    """

    option: int
    air_volume: int | None
    label: str


@dataclass(slots=True)
class AironeDevice:
    """One Airone unit. `reported` changes with every MQTT message that arrives."""

    device_seq: int
    device_id: str
    service_code: int
    model_code: str
    model_name: str
    nickname: str
    # The identifier used in topics. `did.roomController.deviceId` can differ from the
    # `deviceId` in the device list, so it is kept separately.
    physical_device_id: str
    zone_id: int | None
    modes: tuple[AironeMode, ...]
    # The filter **count** comes from metadata. The remaining life comes from state, but the
    # entities are created before MQTT connects, so reading the count from state would create
    # no sensors at all.
    filter_types: tuple[int | None, ...]
    # The air monitor. It registers as a separate device and carries the air-quality sensors.
    # Its `modelCode` is below 1000 (observed: NAA-21DM = 35), but it **takes no commands**,
    # so that says nothing about the protocol generation. For now air quality attaches to the
    # main device and this information only goes into diagnostics, to be settled by a report
    # (spec 6-6).
    air_monitors: tuple[dict[str, Any], ...]
    # **The device's own declaration of which sensors it has.**
    #
    #   NRT-530S3 (no monitor)   the table is in roomController.sensor  <- built into the RC
    #   NRT-530Z3 (with monitor)  roomController.sensor is an empty array
    #                             and the table is in airMonitor[].sensor  <- separate device
    #
    # Some devices have neither — a heat-recovery unit bought without a monitor, for one.
    # Asking such a device about air quality is pointless (`wants_air_sensors`).
    declared_sensors: tuple[Any, ...] | None
    sensor_kinds: tuple[str, ...]
    rc_version: str | None
    odu_version: str | None
    odu_model_code: str | None
    connected_registry: bool
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    reported: dict[str, Any] = field(repr=False, default_factory=dict)
    air_sensors: dict[str, dict[str, Any]] = field(repr=False, default_factory=dict)
    # Outside dehumidify mode the device stops reporting humidity. Re-entering dehumidify
    # without sending this value back makes **the device fall back to its own minimum** —
    # confirmed by a user report (set it, round-trip the mode, and it resets to 40%).
    last_humidity: int | None = field(repr=False, default=None)
    # A short record of **what was sent and what came back**. Some questions can only be
    # settled by seeing the order of values — whether the device reverts a humidity sent
    # along with a mode change cannot be told from a single moment. Kept in diagnostics so one
    # report closes it. Nothing personal — mode numbers and humidity values only.
    command_log: list[dict[str, Any]] = field(repr=False, default_factory=list)
    # **Leave a trace even when the values freeze.** The price of not clearing on an empty
    # response is that "the refresh is stuck" and "the value did not change" became
    # indistinguishable. So the last time a value really changed, and how many polls came back
    # identical, are both counted.
    air_sensor_stamp: float | None = field(repr=False, default=None)
    air_sensor_empty: int = field(repr=False, default=0)
    air_sensor_errors: int = field(repr=False, default=0)
    air_sensor_unchanged: int = field(repr=False, default=0)
    # **Air-quality kinds that have ever produced a value.** Used when creating entities.
    #
    # There is a reason only the kinds are remembered, not the values. Within a session an
    # empty response cannot erase what was already received (`set_air_sensors`), but a restart
    # removes that protection: entities are created exactly once at startup, so if the server
    # happens to send only temperature and humidity at that moment, **every other air-quality
    # sensor disappears.** Restarting HA while the air monitor is briefly absent ends the CO2
    # history right there.
    #
    # So the kinds are stored and restored on the next start. **The values are not restored**
    # — showing a days-old number as if it were current is worse than unknown. With the
    # entity in place, the history simply resumes when values return.
    known_sensor_kinds: tuple[str, ...] = field(repr=False, default=())
    humidity_log: list[dict[str, Any]] = field(repr=False, default_factory=list)

    # -- construction ------------------------------------------------------

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> AironeDevice | None:
        device_id = raw.get("deviceId")
        device_seq = raw.get("deviceSeq")
        service_code = raw.get("serviceCode")
        if not device_id or device_seq is None or service_code is None:
            return None

        # The older generation nests one more `state` layer under `did` (observed on an
        # NRT-20DSW). Whichever one exists is used without checking the generation — either
        # way it only appears in one place.
        did = (
            _dig(raw, "Properties", "data", "did", "reported")
            or _dig(raw, "Properties", "data", "did", "state", "reported")
            or {}
        )
        controller = did.get("roomController")
        if not isinstance(controller, dict):
            # **Never give up on the device.** Missing capability metadata only means the
            # choices are unknown; power, running state and errors still come from the status
            # response.
            #
            # There are moments — just after registration, say — when the device has not
            # pushed its `did` yet. Returning `None` here would leave that user with no
            # entities at all, which is what "nothing shows up" was. Build what exists and
            # skip what does not.
            controller = {}
        odu = did.get("odu") if isinstance(did.get("odu"), dict) else {}

        modes = tuple(
            mode
            for mode in (AironeMode.parse(item) for item in controller.get("mode") or [])
            if mode is not None
        )
        modes = cls._legacy_modes(
            modes, raw.get("modelCode"), str(raw.get("modelName") or "")
        )

        # **This can arrive as a string.** A mat sends `{"mainItem": ..., "side": {...}}`,
        # but nothing rules out an account that never split its nicknames sending a single
        # name instead. Calling `.get` on that kills the whole integration during setup — not
        # one device but **all** of them disappear. A plain string is used as the name.
        raw_nick = _dig(raw, "Properties", "nickName")
        nick = raw_nick if isinstance(raw_nick, dict) else {}
        nick_text = raw_nick.strip() if isinstance(raw_nick, str) else ""
        nickname = (
            nick.get("mainItem")
            or nick_text
            or controller.get("zoneNickname")
            or raw.get("modelName")
            or "나비엔 환기청정"
        )

        return cls(
            device_seq=int(device_seq),
            device_id=str(device_id),
            service_code=int(service_code),
            model_code=str(raw.get("modelCode") or ""),
            model_name=str(raw.get("modelName") or "나비엔 환기청정"),
            nickname=str(nickname),
            physical_device_id=str(controller.get("deviceId") or device_id),
            zone_id=_as_int(controller.get("zoneId")),
            modes=modes,
            filter_types=tuple(
                _as_int(item.get("type"))
                for item in odu.get("filter") or []
                if isinstance(item, dict)
            ),
            air_monitors=tuple(
                {
                    "deviceId": item.get("deviceId"),
                    "modelCode": item.get("modelCode"),
                    "version": item.get("version"),
                    "zoneId": item.get("zoneId"),
                    "sensor": item.get("sensor"),
                }
                for item in did.get("airMonitor") or []
                if isinstance(item, dict)
            ),
            # **A missing key means "unknown", not "absent".** In that case, ask.
            declared_sensors=(
                tuple(controller["sensor"])
                if isinstance(controller.get("sensor"), list)
                else None
            ),
            sensor_kinds=(),
            rc_version=_version_text(controller.get("version")),
            odu_version=_version_text(odu.get("version")),
            odu_model_code=(
                str(odu.get("modelCode")) if odu.get("modelCode") is not None else None
            ),
            connected_registry=bool(raw.get("connected")),
            raw=raw,
        )

    # -- applying state ----------------------------------------------------

    @staticmethod
    def _legacy_modes(
        modes: tuple[AironeMode, ...], model_code: Any, model_name: str | None = None
    ) -> tuple[AironeMode, ...]:
        """Build the older generation's mode list the way the app does.

        **Treating the DID as the capability list was the original mistake.** For the older
        generation the app never looks at the DID at all: with `modelCode < 1000` it calls
        `loadlegacyModeDataFromFile()` and uses a file bundled in the app as the mode list
        (`AirOneControlViewModel`). That file's contents are `LEGACY_MODE_DID`.

        This is how it was found. Two devices (`NRT-20DS`, `NRT-20DSW`) showed six modes on
        the app's screen while their DIDs held fewer. **Building the list from the DID gives
        fewer modes than the app** — auto and cooking were missing.

        So the app's file is the baseline and **whatever the DID provides is layered on top.**
        A DID entry is that device's actual value, so things like the default fan speed are
        more accurate from the DID.
        """
        code = _as_int(model_code)
        if code is None or code >= AIRONE_V2_MIN_MODEL_CODE:
            return modes

        # **The file decides whether the fan speed is selectable.** Guessing that `option == 1`
        # meant selectable was wrong for auto (12) and cooking (6): both are
        # `configurable: false`, yet gentle, low and high were being offered.
        table = {(m, o): (vol, conf) for m, o, vol, conf in LEGACY_MODE_DID}

        # **Bypass only for models confirmed to have it** (`LEGACY_BYPASS_MODEL_PREFIXES`).
        # Unknown models do not get it: offering one that does not exist leaves the user who
        # pressed it having to undo the device, while a missing one only needs a line in a
        # report to add.
        plain = re.sub(r"[^A-Z0-9]", "", (model_name or "").upper())
        if not plain.startswith(LEGACY_BYPASS_MODEL_PREFIXES):
            table.pop((AIRONE_MODE_BYPASS, AIRONE_OPTION_NONE), None)

        have = {(item.mode, item.option) for item in modes}
        merged = list(modes)
        for (mode_code, option), (air_volume, conf) in table.items():
            if (mode_code, option) in have:
                continue
            merged.append(
                AironeMode(
                    mode=mode_code,
                    option=option,
                    air_volume=air_volume,
                    supported_air_volumes=(
                        LEGACY_DEFAULT_AIR_VOLUMES if conf else ()
                    ),
                    configurable=conf,
                    humidity_min=None,
                    humidity_max=None,
                )
            )
        # Combinations from the DID carry no `supportedAirVolumes` either. **Since the app
        # ignores the DID entirely for this generation**, a combination the file knows about is
        # overridden by the file's judgement.
        return tuple(
            (
                item
                if item.key not in table or item.supported_air_volumes
                else AironeMode(
                    mode=item.mode,
                    option=item.option,
                    air_volume=item.air_volume,
                    supported_air_volumes=(
                        LEGACY_DEFAULT_AIR_VOLUMES if table[item.key][1] else ()
                    ),
                    configurable=table[item.key][1],
                    humidity_min=item.humidity_min,
                    humidity_max=item.humidity_max,
                )
            )
            for item in merged
        )

    def apply_reported(self, incoming: dict[str, Any]) -> None:
        """Merge incoming state **rather than overwriting it.**

        Airone answers each command separately and **those answers are partial.** Turning the
        power on may return only what changed, as in `{"roomController": {"running": 1}}`, and
        sometimes only `odu` arrives.

        Replacing the whole document loses `mode`, `option` and `airVolume`, and in the worst
        case `running` as well, which drops **power into unknown** — confirmed by a user
        report.

        A mat needs none of this because its shadow always arrives complete. Only here does
        state merge.

        Stale values may survive, and that is accepted: when the device stops sending a field,
        its last value remains. **It beats everything reading as unknown.**
        """
        incoming = _strip_capability_fields(incoming)
        merged: dict[str, Any] = dict(self.reported or {})
        for key, value in incoming.items():
            current = merged.get(key)
            if isinstance(value, dict) and isinstance(current, dict):
                # Replace only the fields inside `roomController` that changed.
                inner = dict(current)
                inner.update(value)
                merged[key] = inner
            else:
                # Lists (`airMonitor`, `filter`) are replaced wholesale. Merging a partial list
                # item by item misaligns the positions.
                merged[key] = value
        self.reported = merged
        self._note_humidity()

    def _note_humidity(self) -> None:
        """Record one line when the observed target humidity changes; identical values are not stacked."""
        value = self.target_humidity
        entry = {"mode": self.mode, "option": self.option, "humidity": value}
        if self.humidity_log and {
            k: self.humidity_log[-1].get(k) for k in ("mode", "option", "humidity")
        } == entry:
            return
        self.humidity_log.append({**entry, "at": round(time.monotonic(), 1)})
        del self.humidity_log[:-_LOG_KEEP]

    def note_command(self, command: str, desired: dict[str, Any] | None) -> None:
        """Record one line per command sent — without it diagnostics cannot show the ordering."""
        controller = (desired or {}).get("roomController") or {}
        extra = controller.get("additionalData")
        self.command_log.append(
            {
                "command": command,
                "mode": controller.get("mode"),
                "option": controller.get("option"),
                "airVolume": controller.get("airVolume"),
                "running": controller.get("running"),
                # Whether a humidity was sent along is the point.
                "humidity_sent": (extra or {}).get("value") if isinstance(extra, dict) else None,
                "at": round(time.monotonic(), 1),
            }
        )
        del self.command_log[:-_LOG_KEEP]

    # -- generation --------------------------------------------------------

    @property
    def is_v2_generation(self) -> bool:
        """Whether this is the V2.1 generation.

        With `modelCode < 1000` the envelope and topics differ completely (spec 6-5), so the
        same code cannot address it.
        """
        code = _as_int(self.model_code)
        return code is not None and code >= AIRONE_V2_MIN_MODEL_CODE

    # -- state -------------------------------------------------------------

    @property
    def legacy_extras(self) -> dict[str, Any]:
        """Values only the older generation carries; an empty dict when absent."""
        value = (self.reported or {}).get("legacyExtras")
        return value if isinstance(value, dict) else {}

    @property
    def _controller(self) -> dict[str, Any]:
        value = (self.reported or {}).get("roomController")
        return value if isinstance(value, dict) else {}

    @property
    def _odu(self) -> dict[str, Any]:
        value = (self.reported or {}).get("odu")
        return value if isinstance(value, dict) else {}

    @property
    def available(self) -> bool:
        return self.connected_registry

    @property
    def running(self) -> int | None:
        """Running state, falling back to the outdoor unit when the room controller has none.

        **Both** `RoomControllerStatus` and `OduStatus` carry `running`. Reading only the room
        controller leaves the power switch permanently unknown on a device that sends that
        field empty — the value is there and simply not read.

        The room controller is read first, because that is what the user touches.
        """
        value = _as_running(self._controller.get("running"))
        if value is None:
            value = _as_running(self._odu.get("running"))
        return value

    @property
    def running_name(self) -> str | None:
        value = self.running
        if value is None:
            return None
        return AIRONE_RUN_NAMES.get(value, f"알 수 없음({value})")

    @property
    def is_on(self) -> bool:
        return self.running == AIRONE_RUN_ON

    @property
    def mode(self) -> int | None:
        return _as_int(self._controller.get("mode"))

    @property
    def option(self) -> int | None:
        return _as_int(self._controller.get("option"))

    @property
    def air_volume(self) -> int | None:
        return _as_int(self._controller.get("airVolume"))

    @property
    def mode_label(self) -> str | None:
        mode = self.mode
        if mode is None:
            return None
        option = self.option
        return airone_mode_label(mode, AIRONE_OPTION_NONE if option is None else option)

    @property
    def wind_label(self) -> str | None:
        value = self.air_volume
        if value is None:
            return None
        return AIRONE_WIND_NAMES.get(value)

    @property
    def error_code(self) -> int | None:
        code = _as_int(_dig(self._controller, "error", "code"))
        if code is None:
            code = _as_int(_dig(self._odu, "error", "code"))
        return code

    @property
    def has_error(self) -> bool:
        code = self.error_code
        return code is not None and code != 0

    @property
    def auto_dry_percent(self) -> int | None:
        """Auto-dry progress in percent, or `None` when not auto-drying.

        The app shows it inline on the status line as "자동건조 중 47%". Here **the state text
        stays as the auto-dry label and the progress moves to an attribute** — mixing a number
        into the state breaks any automation comparing the string.

        Read only while `running` is 4; the app also consults this value only under that
        condition.
        """
        if self.running != AIRONE_RUN_AUTO_DRY:
            return None
        # **Searched from the end**, as the app does — when the same number appears more than
        # once, the later one is the current value.
        for extra in reversed(self._controller.get("additionalData") or []):
            if not isinstance(extra, dict):
                continue
            if _as_int(extra.get("type")) != AIRONE_AUTO_DRY_TYPE:
                continue
            return _as_int(extra.get("value"))
        return None

    @property
    def target_humidity(self) -> int | None:
        """Dehumidify target humidity.

        **Searching by number alone never found it.** The server's capability data reports the
        range as `type: 1`, while device state returns the value as **`type: 3`**. The
        `type: 1` entry in that same list is a different item with a range of 0-4. Up to
        v0.9.1 only that was searched, which is why the field was always empty.

        So two things are consulted together.

        1. **A value inside the range the server declared for that mode** — this is the
           deciding test. In modes that declare no range (ventilate/purify, turbo/saving) a
           value is not used even when present.
        2. With several candidates, the number confirmed on a real device (`3`) wins.

        The number is not a hard condition because the observation covers only one device. A
        value inside the range is read whatever its number.
        """
        bounds = self.humidity_bounds(self.mode, self.option)
        if bounds is None:
            return None

        candidates: list[tuple[int | None, int]] = []
        for extra in self._controller.get("additionalData") or []:
            if not isinstance(extra, dict):
                continue
            value = _as_int(extra.get("value"))
            if value is None or not bounds[0] <= value <= bounds[1]:
                continue
            candidates.append((_as_int(extra.get("type")), value))

        if not candidates:
            return None
        if len(candidates) > 1:
            # Which one is the humidity cannot be settled. The confirmed number wins, and the
            # ambiguity is logged.
            _LOGGER.debug(
                "에어원 습도 후보가 여럿입니다 (범위 %s): %s", bounds, candidates
            )
        for kind, value in candidates:
            if kind == AIRONE_HUMIDITY_REPORT_TYPE:
                self.last_humidity = value
                return value
        value = candidates[0][1]
        self.last_humidity = value
        return value

    @property
    def filters(self) -> tuple[dict[str, Any], ...]:
        """Outdoor-unit filter state, holding as many slots as the metadata declared.

        Before any state arrives it returns slots whose `percent` is `None` — a length that
        moves would misalign the slots against the entities.

        **`percent` is the remaining life.** It comes from `usage.percent`, which reads like
        "amount used" while the value is the opposite: 87 means 87% left and 13% used.
        Confirmed on a real device against the Navien app display. The key name is left alone,
        since changing it would change the diagnostics format.
        """
        reported = [
            item for item in self._odu.get("filter") or [] if isinstance(item, dict)
        ]
        result: list[dict[str, Any]] = []
        for index, kind in enumerate(self.filter_types):
            item = reported[index] if index < len(reported) else {}
            result.append(
                {
                    "type": _as_int(item.get("type")) if item else kind,
                    "percent": _as_int(_dig(item, "usage", "percent")),
                    "replace_period": _as_int(item.get("replacePeriod")),
                }
            )
        return tuple(result)

    # -- capabilities ------------------------------------------------------

    @property
    def configurable_modes(self) -> tuple[AironeMode, ...]:
        """Combinations the server marked adjustable. For diagnostics.

        **The mode list is never filtered by this** — `configurable` says whether fan speed or
        humidity can be adjusted within a mode, not whether the mode can be selected.
        """
        return tuple(item for item in self.modes if item.configurable)

    @property
    def selectable_modes(self) -> tuple[AironeModeChoice, ...]:
        """The operating-mode list, **in the server's own order.**

        Cut along the same axis as the app: turbo, saving and baseline go to fan speed rather
        than here, and sleep is the one raised as its own mode.

        The server may give a mode no `option 1` and only turbo. So that such a mode does not
        vanish from the list, **the first combination of that mode** stands in for it.
        """
        result: list[AironeModeChoice] = []
        seen: set[tuple[int, int]] = set()

        for item in self.modes:
            if item.option == AIRONE_OPTION_SLEEP:
                key = (item.mode, AIRONE_OPTION_SLEEP)
                label = AIRONE_MODE_SLEEP_LABEL
            else:
                # The representative option for that mode: 1 if present, otherwise the first seen.
                options = [
                    other.option
                    for other in self.modes
                    if other.mode == item.mode and other.option != AIRONE_OPTION_SLEEP
                ]
                option = AIRONE_OPTION_NONE if AIRONE_OPTION_NONE in options else options[0]
                key = (item.mode, option)
                label = AIRONE_MODE_NAMES.get(item.mode) or f"알 수 없음({item.mode})"

            if key in seen:
                continue
            seen.add(key)
            result.append(AironeModeChoice(mode=key[0], option=key[1], label=label))

        # Sleep attached to several modes produces duplicate labels. Only then is the mode
        # appended to disambiguate.
        sleeps = [c for c in result if c.is_sleep]
        if len(sleeps) > 1:
            result = [
                AironeModeChoice(
                    c.mode,
                    c.option,
                    f"{AIRONE_MODE_NAMES.get(c.mode, c.mode)} {AIRONE_MODE_SLEEP_LABEL}",
                )
                if c.is_sleep
                else c
                for c in result
            ]
        return tuple(result)

    def mode_entries(self, mode: int, option: int) -> tuple[AironeMode, ...]:
        return tuple(item for item in self.modes if item.key == (mode, option))

    def fan_choices(self, mode: int | None, option: int | None) -> tuple[AironeFanChoice, ...]:
        """The fan speeds selectable in the current mode.

        Returns **only combinations that actually appeared in the server metadata**; no table
        is invented to fill it out.

        - `option == 1` -> `airVolume` decides between gentle, low, high and auto
        - `option` of turbo, saving or baseline -> the option itself becomes the entry
        - inside sleep mode, only that combination's `airVolume` is used (as in the app)
        """
        if mode is None:
            return ()
        sleeping = option == AIRONE_OPTION_SLEEP
        result: list[AironeFanChoice] = []
        seen: set[str] = set()
        # How many entries the server gave for the same combination. Several means that
        # enumeration is the list.
        enumerated: dict[tuple[int, int], int] = {}
        for item in self.modes:
            enumerated[item.key] = enumerated.get(item.key, 0) + 1

        def add(opt: int, wind: int | None, label: str | None) -> None:
            if not label or label in seen:
                return
            seen.add(label)
            result.append(AironeFanChoice(opt, wind, label))

        for item in self.modes:
            if item.mode != mode:
                continue
            if sleeping != (item.option == AIRONE_OPTION_SLEEP):
                # In sleep mode only sleep combinations are considered, and elsewhere only
                # non-sleep ones.
                continue

            if item.option in AIRONE_OPTIONS_WITH_WIND:
                # **`configurable` means "is the fan speed selectable".** That is how the app
                # uses it:
                # (`AirOneControlFragment.allowedWindChoicesFromDids`).
                #
                #     z10 = any entry of that mode has configurable true
                #     if (z10) { show gentle, low and high }
                #     else     { only those whose airVolume is 1, 2 or 3 }
                #
                # `supportedAirVolumes` is **a field absent from APK 2.10.4**. The server added
                # it later and older firmware does not send it. Trusting it alone collapses an
                # older-firmware device down to auto only — in a real-device report
                # (`NRT-530Z3`, RC 10.1) the app showed six where we showed three.
                #
                # Order of preference: the server's list if it sent one; otherwise, since it
                # said selectable, the app's whole table; failing that, the single current value.
                if item.supported_air_volumes:
                    values: tuple[int, ...] = item.supported_air_volumes
                elif (
                    item.configurable
                    and item.air_volume in AIRONE_SELECTABLE_AIR_VOLUMES
                    and enumerated.get(item.key, 0) == 1
                ):
                    # **Widen only when the combination came as a single entry.**
                    #
                    # When the server enumerates the same combination several times (`4:1`
                    # appearing three times, for speeds 1, 2 and 3), that enumeration is the
                    # list. Widening then would resurrect values the server deliberately left
                    # out.
                    #
                    # A single entry is a different case: it is the current value, not a list.
                    #
                    # **Widen only when that value is one of the four steps.** Widening on a
                    # device that reports baseline (5, 6) would **lose** the entry the server
                    # declared: baseline disappears and gentle/low/high/auto take its place.
                    # Trying to widen would take away what was there, so an unrecognised value
                    # is left alone. A device with no `airVolume` at all (a heat-recovery unit)
                    # is not widened either — it has no fan steps and the app shows only turbo
                    # and saving.
                    values = AIRONE_SELECTABLE_AIR_VOLUMES
                elif item.air_volume is not None:
                    values = (item.air_volume,)
                else:
                    values = ()
                for value in values:
                    add(item.option, value, AIRONE_WIND_NAMES.get(value))
            else:
                add(item.option, item.air_volume, AIRONE_OPTION_NAMES.get(item.option))
        return tuple(result)

    def current_fan_label(self) -> str | None:
        """The name of the fan-speed entry matching the current state."""
        for choice in self.fan_choices(self.mode, self.option):
            if choice.option != (self.option or AIRONE_OPTION_NONE):
                continue
            if choice.option in AIRONE_OPTIONS_WITH_WIND:
                if choice.air_volume == self.air_volume:
                    return choice.label
                continue
            return choice.label
        return None

    def humidity_bounds(self, mode: int | None, option: int | None) -> tuple[int, int] | None:
        """The dehumidify target-humidity range.

        **Accepted only on an exact combination match.** The server declares a range for
        dehumidify at the base fan speed (`9:1`) and none for turbo or saving (`9:2`, `9:3`).

        Up to v0.9.0 this borrowed from another combination of the same mode, on the grounds
        that "a range is a property of the mode". **That was a guess, and it contradicts the
        app**: the app resources contain `humidityAutoText` and `humiditySeekbarNone`, and in
        turbo and saving the app hides the slider and shows "auto". That band is the device's
        own business.

        Inventing a range makes a value the user cannot adjust look adjustable, and sends that
        value out in a command.
        """
        if mode is None or mode not in AIRONE_MODES_WITH_HUMIDITY:
            return None
        opt = AIRONE_OPTION_NONE if option is None else option
        for item in self.mode_entries(mode, opt):
            if not item.wants_humidity:
                continue
            assert item.humidity_min is not None and item.humidity_max is not None
            return (item.humidity_min, item.humidity_max)
        return None

    # -- air quality -------------------------------------------------------

    def set_air_sensors(self, airs: list[dict[str, Any]]) -> list[str]:
        """Apply an `/air-sensor` response and return the kinds that were not recognised.

        **Merged, not overwritten**, for the same reason as a status response
        (`apply_reported`). Air quality is re-read every five minutes, and a response that
        comes back empty or partial would otherwise **drop those sensors into unknown every
        time** — which happens whenever the air monitor blips or the server skips one round.

        An empty response erases nothing. A stale value surviving beats all of them vanishing.
        """
        unknown: list[str] = []
        table: dict[str, dict[str, Any]] = {}
        for item in airs:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if not isinstance(kind, str) or not kind:
                continue
            # The server may use a different name; everything is normalised to the standard one.
            kind = AIRONE_SENSOR_ALIASES.get(kind.strip().lower(), kind)
            if kind not in AIRONE_SENSOR_KINDS:
                unknown.append(kind)
                continue
            table[kind] = item

        if not table:
            # An empty response must not erase values already received.
            self.air_sensor_empty += 1
            _LOGGER.debug("공기질 응답이 비어 있어 앞서 받은 값을 유지합니다")
            return unknown

        merged = dict(self.air_sensors)
        merged.update(table)
        changed = merged != self.air_sensors
        self.air_sensors = merged
        self.sensor_kinds = tuple(k for k in AIRONE_SENSOR_KINDS if k in merged)
        self.remember_sensor_kinds(merged)
        if changed:
            self.air_sensor_stamp = time.monotonic()
            self.air_sensor_unchanged = 0
        else:
            # Values did arrive, but identical to last time. The room may simply be quiet, or
            # the server may be repeating a stale value — counted, and left to be judged.
            self.air_sensor_unchanged += 1
        return unknown

    def remember_sensor_kinds(self, kinds: Any) -> None:
        """Add to the kinds ever seen — known kinds only, kept in table order."""
        seen = set(self.known_sensor_kinds)
        seen.update(k for k in kinds if k in AIRONE_SENSOR_KINDS)
        self.known_sensor_kinds = tuple(k for k in AIRONE_SENSOR_KINDS if k in seen)

    @property
    def entity_sensor_kinds(self) -> tuple[str, ...]:
        """The kinds to create air-quality entities for.

        This uses **the kinds ever seen**, not the ones arriving right now. Even when the
        server sends only some of them this time, the entities have to remain for the history
        to resume when the values return.
        """
        return self.known_sensor_kinds or self.sensor_kinds

    @property
    def wants_air_sensors(self) -> bool:
        """Whether there is any reason to ask this device about air quality.

        **It stops asking only when it is sure there is none.** Any one of three is enough:

        - an air monitor is attached -> the sensors are there
        - the room controller published a sensor table -> they are built into it
        - no table arrived at all -> that is **unknown**, not absent

        A heat-recovery unit without a monitor is what this catches. Asking such a device
        every five minutes returns nothing but empty responses, and before v0.12.0 a slow call
        there **killed the entire poll.**
        """
        if self.air_monitors:
            return True
        if self.declared_sensors is None:
            return True
        return bool(self.declared_sensors)

    @property
    def air_sensor_age(self) -> float | None:
        """Seconds since an air-quality value last **changed**."""
        if self.air_sensor_stamp is None:
            return None
        return round(time.monotonic() - self.air_sensor_stamp, 1)

    # -- control -----------------------------------------------------------

    def build_power_desired(self, turn_on: bool) -> dict[str, Any]:
        """The body of a `power` command (`DesiredPowerRequestData`)."""
        controller: dict[str, Any] = {
            "deviceId": self.physical_device_id,
            "running": AIRONE_RUN_ON if turn_on else AIRONE_RUN_OFF,
        }
        if self.zone_id is not None:
            controller["zoneId"] = self.zone_id
        return {"roomController": controller}

    def build_mode_desired(
        self,
        mode: int,
        option: int,
        air_volume: int | None = None,
        humidity: int | None = None,
    ) -> dict[str, Any]:
        """The body of a `change-mode` command (`DesiredChangeModeRequestData`).

        The fan speed is chosen **only from values the server declared**. When the current
        combination has none, the field is omitted entirely — no 0 and no invented default.
        """
        controller: dict[str, Any] = {"mode": mode, "option": option}

        wind = air_volume
        if wind is not None and wind not in AIRONE_WIND_NAMES:
            # An unconfirmed value is **treated as unspecified.** Sending it as-is is wrong,
            # and dropping the field can make the device reset the fan speed to 0. It falls
            # back to a server-supplied value below.
            _LOGGER.debug("에어원 풍량 %s 는 확인된 값이 아니라 무시합니다", wind)
            wind = None
        if wind is None:
            allowed = [
                choice.air_volume
                for choice in self.fan_choices(mode, option)
                if choice.option == option and choice.air_volume is not None
            ]
            if self.air_volume in allowed:
                wind = self.air_volume
            elif allowed:
                wind = allowed[0]
        if wind is not None:
            controller["airVolume"] = wind

        # Consult the range of **the mode being entered**. `self.target_humidity` reads
        # against the *current* mode, so it is always `None` at the moment of moving from
        # ventilate into dehumidify. Trusting only that and omitting the humidity makes the
        # device fall back to its own minimum.
        target = humidity
        bounds = self.humidity_bounds(mode, option)
        if target is None and bounds is not None:
            for candidate in (self.target_humidity, self.last_humidity):
                if candidate is not None and bounds[0] <= candidate <= bounds[1]:
                    target = candidate
                    break
        if target is not None and bounds is not None:
            target = max(bounds[0], min(bounds[1], target))
            # Reused on the next round trip as well.
            self.last_humidity = target
            controller["additionalData"] = {
                "type": AIRONE_HUMIDITY_TYPE,
                "value": target,
            }

        return {"roomController": controller}

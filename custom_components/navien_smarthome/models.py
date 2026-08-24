"""Reshape device responses and shadow state into something the integration can use.

The traps found on real devices are absorbed here.

- `functions` drops keys per model. A feature that is absent gets no entity
- `heater.single` arrives as `null` alongside the others. Testing for key presence is wrong
- single versus double is decided by `mcu.capacity`, not `mcu.matType`
- `sleepMode` has a different shape under `functions` than it does in state. Do not mix them
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .const import (
    CAPACITY_DOUBLE,
    MAT_ERROR_NAMES,
    MAT_VOLUME_NAMES,
    MODE_HEAT,
    MODE_NAMES,
    MODES_ON,
    SEASON_NAMES,
    SEASON_SUMMER,
    SERVICE_NAMES,
    UNIT_CELSIUS,
    UNIT_LEVEL,
    ZONE_LEFT,
    ZONE_RIGHT,
    ZONE_SINGLE,
)


def _dig(source: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(source, dict):
            return None
        source = source.get(key)
    return source


# How many records diagnostics keeps. The point is to see the ordering, so it need not be long.
_LOG_KEEP = 8


def _merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge dictionaries at any depth. Lists and other values are replaced wholesale."""
    merged = dict(base)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _version_text(current: Any) -> str | None:
    """Render `{major, minor, build}` as `14.0.0`.

    A mat carries separate firmware for its MCU and its Wi-Fi module (observed: MCU 14.0.0,
    Wi-Fi 5.1.100).
    """
    if not isinstance(current, dict):
        return None
    parts = [current.get(key) for key in ("major", "minor", "build")]
    if any(part is None for part in parts):
        return None
    return ".".join(str(int(part)) for part in parts)


@dataclass(slots=True)
class HeatControl:
    """Either `functions.heatControl` or `functions.coolControl`.

    The two have the same shape; cooling adds `fanRPM` and `antiCondensation`. Cooling is
    Peltier-based, so a fan has to carry the heat away and condensation has to be managed.
    """

    unit: str | None
    range_min: float | None
    range_max: float | None
    safe_value: float | None
    enable_safe: bool
    fan_rpm: int | None = None
    anti_condensation: bool | None = None

    @property
    def is_level(self) -> bool:
        return self.unit == UNIT_LEVEL

    @property
    def is_celsius(self) -> bool:
        return self.unit == UNIT_CELSIUS

    @property
    def is_known(self) -> bool:
        return self.is_level or self.is_celsius

    @property
    def step(self) -> float:
        """The leading number of the `<step><axis>` encoding."""
        if self.is_celsius:
            return 0.5
        return 1.0

    @property
    def off_value(self) -> float | None:
        """The value sent to turn one zone off — **one step below `rangeMin`.**

        From the app (`MateWifiModelControlViewModel.setTemperature`):

            if (temp <= rangeMin - 1) temp = 0;      // for the app's own display
            ...
            if (temp < rangeMin) temp = rangeMin - controlUnit;   // what actually goes out

        **It does not send 0.** Zero is an intermediate value the app uses to draw "off" on
        screen; what reaches the device is `rangeMin - step`.

            stepped   1.0L (1-8)     1 - 1.0  = 0      <- verified on a real device
            temperature 0.5C (28-50)  28 - 0.5 = 27.5
            hot water 1.0C (28-45)   28 - 1.0 = 27.0

        **This is why stepped mats worked all along.** They were already sending `level 0`,
        which was not luck but the same rule. Only temperature mats skipped this calculation
        and sent `enable: false` alone, which the device ignored (issue #16).

        The app **does not touch `enable` when turning a zone off** —
        `updateLeftMatSettingTemp` calls only `temperature.setSet()`. We keep sending it,
        because that is the form verified on stepped mats, but the value is what matters.
        """
        if self.range_min is None or not self.is_known:
            return None
        return self.range_min - self.step

    @classmethod
    def parse(cls, raw: Any) -> HeatControl | None:
        if not isinstance(raw, dict):
            return None
        return cls(
            unit=raw.get("unit"),
            range_min=raw.get("rangeMin"),
            range_max=raw.get("rangeMax"),
            safe_value=raw.get("safeValue"),
            enable_safe=bool(raw.get("enableSafe")),
            fan_rpm=raw.get("fanRPM"),
            anti_condensation=raw.get("antiCondensation"),
        )

    def as_diagnostics(self) -> dict[str, Any]:
        """For reports: expose the raw value of an unsupported axis so a user can attach it."""
        data: dict[str, Any] = {
            "unit": self.unit,
            "range_min": self.range_min,
            "range_max": self.range_max,
            "safe_value": self.safe_value,
        }
        if self.fan_rpm is not None:
            data["fan_rpm"] = self.fan_rpm
        if self.anti_condensation is not None:
            data["anti_condensation"] = self.anti_condensation
        return data


@dataclass(slots=True)
class NavienDevice:
    """One device. `reported` changes with every MQTT message that arrives."""

    device_seq: int
    device_id: str
    service_code: int
    model_code: str
    model_name: str
    nickname: str
    model_type: str | None
    capacity: int | None
    zone_names: dict[str, str]
    heat_control: HeatControl | None
    cool_control: HeatControl | None
    has_power_ctrl: bool
    has_beep: bool
    has_lock_mode: bool
    has_power_saving: bool
    has_sleep_mode: bool
    sleep_durations: list[int]
    schedule_kinds: tuple[str, ...]
    connected_registry: bool
    mcu_version: str | None
    wifi_version: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    reported: dict[str, Any] = field(repr=False, default_factory=dict)
    # A short record of **what was sent and what came back**. The cooling value scheme was
    # never confirmed on a real device, and settling it means seeing whether a sent value
    # returns unchanged. Nothing personal is kept — mode numbers, temperatures and steps only.
    command_log: list[dict[str, Any]] = field(repr=False, default_factory=list)
    state_log: list[dict[str, Any]] = field(repr=False, default_factory=list)

    # -- construction ------------------------------------------------------

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> NavienDevice | None:
        device_id = raw.get("deviceId")
        device_seq = raw.get("deviceSeq")
        service_code = raw.get("serviceCode")
        if not device_id or device_seq is None or service_code is None:
            return None

        attrs = _dig(raw, "Properties", "registry", "attributes") or {}
        functions = attrs.get("functions") or {}
        mcu = attrs.get("mcu") or {}
        # **This can arrive as a string.** A mat sends `{"mainItem": ..., "side": {...}}`,
        # but nothing rules out an account that never split its nicknames sending a single
        # name instead. Calling `.get` on that kills the whole integration during setup — not
        # one device but **all** of them disappear. A plain string is used as the name.
        raw_nick = _dig(raw, "Properties", "nickName")
        nick = raw_nick if isinstance(raw_nick, dict) else {}
        nick_text = raw_nick.strip() if isinstance(raw_nick, str) else ""
        side = nick.get("side") if isinstance(nick.get("side"), dict) else {}

        capacity = mcu.get("capacity")
        if capacity == CAPACITY_DOUBLE or side:
            zone_names = {
                ZONE_LEFT: side.get("left") or "좌측",
                ZONE_RIGHT: side.get("right") or "우측",
            }
        else:
            zone_names = {ZONE_SINGLE: "난방"}

        sleep = functions.get("sleepMode") or {}
        schedule = functions.get("schedule") or {}

        return cls(
            device_seq=int(device_seq),
            device_id=str(device_id),
            service_code=int(service_code),
            model_code=str(raw.get("modelCode") or ""),
            model_name=str(raw.get("modelName") or attrs.get("model") or "나비엔"),
            nickname=str(
                nick.get("mainItem") or nick_text or raw.get("modelName") or "나비엔 기기"
            ),
            model_type=attrs.get("modelType"),
            capacity=capacity,
            zone_names=zone_names,
            heat_control=HeatControl.parse(functions.get("heatControl")),
            cool_control=HeatControl.parse(functions.get("coolControl")),
            has_power_ctrl=bool(functions.get("powerCtrl")),
            # The app's `Functions` class has no `beep` field at all — the server sends it and
            # the app never reads it. Here it decides whether a volume entity is created.
            has_beep=bool(functions.get("beep")),
            has_lock_mode=bool(functions.get("lockMode")),
            has_power_saving=bool(functions.get("powerSaving")),
            has_sleep_mode=bool(sleep.get("enable")),
            sleep_durations=list(sleep.get("durations") or []),
            schedule_kinds=tuple(
                kind for kind in ("oneTime", "weekly", "personal") if schedule.get(kind)
            ),
            connected_registry=bool(raw.get("connected")),
            mcu_version=_version_text(_dig(mcu, "version", "current")),
            wifi_version=_version_text(_dig(attrs, "wifi", "version", "current")),
            raw=raw,
        )

    # -- applying state ----------------------------------------------------

    def apply_reported(self, incoming: dict[str, Any]) -> None:
        """Merge incoming state **rather than overwriting it.**

        The two EME-500 units here always get a complete shadow — `heater` always carries
        `single`, `left` and `right` together. That led to **assuming every mat behaves that
        way.**

        It was wrong. A four-season (EMF520) report contained a response holding only
        `heater.right`, and replacing the whole document lost `operationMode`, `season` and
        `heater.left`. Power then read as unknown and the two sides emptied in turn.

        Dictionaries merge **at any depth**: a message carrying only
        `heater.right.temperature` must not cost us `level` or `enable`.
        Lists are replaced wholesale — merging a partial list item by item misaligns it.

        Stale values may survive, and that is accepted. **It beats everything reading as
        unknown.**
        """
        self.reported = _merge(self.reported or {}, incoming)
        self._note_state()

    def _note_state(self) -> None:
        """Record one line when the state changes; identical values are not stacked.

        Settling four-season cooling requires seeing **what `season` actually holds** and
        **whether a sent value returns unchanged**. Neither can be told from a single moment.
        """
        entry: dict[str, Any] = {
            "operationMode": self.operation_mode,
            "season": self.season,
            "cooling": self.is_cooling,
            "zones": {
                zone: {
                    "set": self._zone_setting_raw(zone),
                    "current": self._zone_current_raw(zone),
                    "enable": self._zone_enabled_raw(zone),
                }
                for zone in self.zones
            },
        }
        if self.state_log and {
            k: v for k, v in self.state_log[-1].items() if k != "at"
        } == entry:
            return
        self.state_log.append({**entry, "at": round(time.monotonic(), 1)})
        del self.state_log[:-_LOG_KEEP]

    def note_command(self, desired: dict[str, Any]) -> None:
        """Record one line for a command that was sent."""
        heater = desired.get("heater") or {}
        self.command_log.append(
            {
                "operationMode": desired.get("operationMode"),
                "cooling_at_send": self.is_cooling,
                "season_at_send": self.season,
                "zones": {
                    zone: {
                        "enable": value.get("enable"),
                        "set": (
                            (value.get("temperature") or value.get("level") or {}).get("set")
                            if isinstance(value, dict)
                            else None
                        ),
                    }
                    for zone, value in heater.items()
                    if isinstance(value, dict)
                },
                "at": round(time.monotonic(), 1),
            }
        )
        del self.command_log[:-_LOG_KEEP]

    # -- state -------------------------------------------------------------

    @property
    def zones(self) -> tuple[str, ...]:
        return tuple(self.zone_names)

    @property
    def is_double(self) -> bool:
        return ZONE_LEFT in self.zone_names

    @property
    def available(self) -> bool:
        """Prefer what the device itself reported through `reported.connected`."""
        if "connected" in self.reported:
            return bool(self.reported.get("connected"))
        return self.connected_registry

    @property
    def operation_mode(self) -> int | None:
        value = self.reported.get("operationMode")
        return int(value) if isinstance(value, (int, float)) else None

    @property
    def is_on(self) -> bool:
        return self.operation_mode in MODES_ON

    @property
    def mode_name(self) -> str | None:
        """The operating state as a human-readable name.

        **`operationMode` knows nothing about the season.** It reads 1 while running, whether
        heating or cooling, and `season` decides which. Using the table as-is therefore showed
        heating during cooling — confirmed by a real-device report (EMF520).

        Only a four-season model in cooling is overridden; everything else follows the table.
        """
        mode = self.operation_mode
        if mode is None:
            return None
        if mode == MODE_HEAT and self.is_cooling:
            # While running (1), `season` decides what it is actually doing.
            return "냉방"
        return MODE_NAMES.get(mode, f"알 수 없음({mode})")

    @property
    def is_four_season(self) -> bool:
        """The presence of `coolControl` marks a four-season model.

        The app decides from a hard-coded `modelCode` table (`setModelCodeAndFunction`), but
        that table is tied to an app version and cannot catch a new model. Whether the server
        sends `coolControl` holds up far longer.
        """
        return self.cool_control is not None

    @property
    def season(self) -> int | None:
        value = self.reported.get("season")
        return int(value) if isinstance(value, (int, float)) else None

    @property
    def child_lock(self) -> bool | None:
        """Control-lock state. `True` means locked."""
        value = self.reported.get("childLock")
        return bool(value) if isinstance(value, bool) else None

    def build_child_lock_desired(self, locked: bool) -> dict[str, Any]:
        """The control-lock desired (`lock-on` / `lock-off`).

        **This corrects v0.12.0's conclusion that the lock cannot be set over Wi-Fi.** A
        reporter sent a photograph of the padlock button on the app's control screen, and a
        second search found it.

            // MateWifiModelControlViewModel
            String str = !mateInfoData1.getLockState() ? "lock-on" : "lock-off";

            // MateConstants
            r11 = Boolean.valueOf(areEqual(r35, "lock-on"));   // childLock
            r4  = new Desired(r11, new Event(modelCode), null x 12);

        **It has the same shape as a season change** and needs no special topic — it goes
        through the default branch of `mateControlDevice`
        (`.../shadow/name/status/update`), the same topic already used for power, temperature
        and season.
        """
        return {"childLock": bool(locked)}

    @property
    def volume(self) -> int | None:
        """Button-sound volume, 0-3 as in the app."""
        value = self.reported.get("volume")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        number = int(value)
        return number if number in MAT_VOLUME_NAMES else None

    @property
    def volume_name(self) -> str | None:
        volume = self.volume
        return MAT_VOLUME_NAMES.get(volume) if volume is not None else None

    def build_volume_desired(self, volume: int) -> dict[str, Any]:
        """The volume desired (`control-volume`).

        The app's screen has exactly four steps (`selectedIndex` 0, 1, 2, 3) and puts that
        value straight into `Desired.volume`. **Nothing else is ever sent.**
        """
        if volume not in MAT_VOLUME_NAMES:
            raise ValueError(f"확인된 음량 값이 아닙니다: {volume}")
        return {"volume": volume}

    @property
    def season_name(self) -> str | None:
        season = self.season
        if season is None:
            return None
        return SEASON_NAMES.get(season, f"알 수 없음({season})")

    @property
    def is_cooling(self) -> bool:
        """Whether the mat is in COOL mode.

        `season` is not an automatic state but **a mode the user picks in the app**, which
        calls them WARM and COOL and keeps a separate schedule list per mode.

        **Cooling is assumed only when `season` is `SEASON_SUMMER` (2) and the cooling
        feature exists.** An unrecognised value falls back to heating and is logged — safer
        than applying the cooling range by mistake.

        `cool_control` is checked alongside because **a device that cannot cool but sends a
        `season` would otherwise read as cooling**, showing the wrong operating state and
        splitting the temperature range to the wrong bounds. Whether such a device exists is
        unknown, but the error should not fall on the side of enabling a missing feature.
        """
        return self.season == SEASON_SUMMER and self.cool_control is not None

    @property
    def error_text(self) -> str | None:
        """The name of an error code, or `None` when unknown.

        The table was transcribed from a device manual by a reporter. **It applies only to
        temperature mats**: its mentions of water tanks, circulation pumps and leaks mark it
        as a hot-water or four-season manual, and there is no evidence the same numbers mean
        the same things on a carbon (stepped) mat.

        **This is an attribute, not the state.** Automations using the number keep working.
        """
        control = self.heat_control
        if control is None or not control.is_celsius:
            return None
        code = self.error_code
        return None if code is None else MAT_ERROR_NAMES.get(code)

    @property
    def has_unknown_season(self) -> bool:
        """A four-season model whose `season` value cannot be interpreted."""
        season = self.season
        return (
            self.is_four_season
            and season is not None
            and season not in SEASON_NAMES
        )

    @property
    def active_control(self) -> HeatControl | None:
        """The control descriptor in force — `coolControl` in summer."""
        if self.is_cooling and self.cool_control is not None:
            return self.cool_control
        return self.heat_control

    @property
    def error_code(self) -> int | None:
        value = self.reported.get("errorCode")
        return int(value) if isinstance(value, (int, float)) else None

    def zone_state(self, zone: str) -> dict[str, Any] | None:
        """`heater.<zone>`, where `null` counts as absent."""
        heater = self.reported.get("heater")
        if not isinstance(heater, dict):
            return None
        state = heater.get(zone)
        return state if isinstance(state, dict) else None

    def _mirror_zone(self, zone: str) -> str | None:
        """The opposite zone to borrow a value from while cooling.

        **While cooling, both sides run at the same temperature** — the app's help text says
        so ("COOL 모드 … 매트의 좌우가 같은 온도로 동작합니다"). The server therefore
        sometimes fills in only one side, leaving the opposite entity empty.

        This is not a guess: it copies a value **documented as identical**. It is never done
        while heating, where the two sides are independent.
        """
        if not self.is_cooling or not self.is_double:
            return None
        return ZONE_RIGHT if zone == ZONE_LEFT else ZONE_LEFT

    def zone_setting(self, zone: str) -> float | None:
        """The setpoint: `level.set` on a stepped mat, `temperature.set` on a temperature mat.

        A stepped mat sends no `temperature.current` — the setpoint is the displayed value.
        """
        value = self._zone_setting_raw(zone)
        if value is None and (mirror := self._mirror_zone(zone)) is not None:
            value = self._zone_setting_raw(mirror)
        return value

    def _zone_setting_raw(self, zone: str) -> float | None:
        state = self.zone_state(zone)
        if state is None:
            return None
        level = state.get("level")
        if isinstance(level, dict) and level.get("set") is not None:
            return float(level["set"])
        temperature = state.get("temperature")
        if isinstance(temperature, dict) and temperature.get("set") is not None:
            return float(temperature["set"])
        return None

    def zone_is_off(self, zone: str) -> bool | None:
        """Whether this zone is off. **`None` when unknown** — never assume it is off.

        **`enable` is read first, because that is what the app does.**

        `MateInfoData.setHeatType()` decides on from off using `enable` alone.

            if (left.enable == TRUE  && right.enable == TRUE)  heatType = 4;
            if (left.enable == TRUE  && right.enable == FALSE) heatType = 2;
            if (left.enable == FALSE && right.enable == TRUE)  heatType = 3;
            else                                              heatType = 1;

        `setMatLeftTemp()` goes further: **when the zone is off it discards the temperature
        the device sent and overwrites it with 0.**

            if (leftMatEnable) leftMatSettingTemp = set;
            else               leftMatSettingTemp = 0;      // draws "off" on screen

        The existence of that branch means **a zone that is off can still report a normal
        temperature**, which is why the app never decides "off" from the temperature.

        Up to v0.17.1 this decided from the temperature, while `hvac_mode` decided the same
        question from `enable` — **the two disagreed.** Both now follow the app.

        With no `enable`, the value decides. On a stepped mat `level 0` and `enable false`
        move together, so either route gives the same answer (confirmed on a real device).
        """
        enabled = self.zone_enabled(zone)
        if enabled is not None:
            return not enabled
        control = self.active_control
        if control is None:
            return None
        off = control.off_value
        setting = self.zone_setting(zone)
        if off is None or setting is None:
            return None
        return setting <= off

    def build_zone_off(self, zones: Iterable[str]) -> dict[str, Any]:
        """Build the desired that turns a zone off.

        **`enable: false` on its own is ignored by the device** (issue #16: sent three times
        on an EME-520, ignored three times). The value has to drop to `off_value` for the
        zone to actually turn off.

        **The last remaining zone cannot be turned off** — the device refuses. Confirmed on a
        real stepped mat: with the left side at 0, setting the right to 0 does not take, and
        it stays at `0, 1`.

        **Power is never switched off on the user's behalf.** The app does not do that
        either; it refuses and explains.

            if (heatType != 2 && heatType != 1) return true;   // proceed
            CustomToast.show(mate_dual_temp_batch_control_one_side_off);
            return false;                                       // refuse

        The user asked to turn one zone off; powering the device down is **something they did
        not ask for**. Instead they are told to use the power switch, as the app advises. With
        the `powerCtrl` gate removed, every mat now has that switch.
        """
        control = self.active_control
        if control is None or not control.is_known:
            raise ValueError(
                f"제어 축을 모르는 기기입니다 (unit={control.unit if control else None})"
            )
        off = control.off_value
        if off is None:
            raise ValueError("이 기기의 꺼짐 값을 계산할 수 없습니다 (rangeMin 없음)")

        target = set(zones)
        # Is any zone that is not being turned off **confirmed to be on**? An unknown (`None`)
        # counts as on — better to send the command and let the device decide than to block a
        # user out of a control.
        stays_on = any(
            self.zone_is_off(zone) is not True
            for zone in self.zones
            if zone not in target
        )
        if not stays_on:
            # The app's exact wording (`strings.xml`).
            raise ValueError(
                "이미 다른 편측이 운전대기 상태입니다. "
                "난방을 끄시려면 매트 전원을 종료해 주세요."
                if len(self.zones) > 1
                else "난방을 끄시려면 매트 전원을 종료해 주세요."
            )

        heater = self.build_heater_desired(
            changes={zone: off for zone in target},
            enables={zone: False for zone in target},
        )
        return {"heater": heater}

    def build_zone_on(self, zones: Iterable[str]) -> dict[str, Any]:
        """Build the full desired that turns a zone on.

        **Exactly symmetric with turning off.** Just as not lowering the value failed to turn
        a zone off, not raising it fails to turn one on (issue #16, reporter item 7).

            what was sent:   operationMode 1 + temperature 27.5  -> powers on, zone stays off
            what to send:    operationMode 1 + temperature 28    -> actually turns on

        `27.5` means that zone is off, so resending it says "stay off". This matches the app,
        where pressing `+` from the off state lands on `rangeMin`.

        **A zone that is not off keeps its value.** Turning the mat on must not drag a zone
        the user set to 33 back down to 28.
        """
        control = self.active_control
        if control is None or not control.is_known:
            raise ValueError(
                f"제어 축을 모르는 기기입니다 (unit={control.unit if control else None})"
            )
        target = set(zones)
        changes: dict[str, float] = {}
        floor = control.range_min
        # **If the device has sent no state at all, do not invent a value here.** Let
        # `build_heater_desired` raise its "the power switch can still turn it on" message —
        # that tells the user something they can actually do.
        reported_heater = self.reported.get("heater")
        known = reported_heater if isinstance(reported_heater, dict) else {}
        if floor is not None:
            for zone in target:
                if zone not in known:
                    continue
                # **A zone the user asked to turn on always goes into the command.**
                #
                # `build_heater_desired` skips any zone whose value it does not know. So when
                # the device never sent that zone's temperature, pressing "on" produced a
                # command without the zone in it: power came on and the zone stayed as it was
                # (issue #16, candidate 7). Given an instruction to turn on, sending at least
                # the minimum value is the right thing.
                #
                # **Only a zone confirmed to be on keeps its value** — a side set to 33 must
                # not be dragged back to 28.
                if self.zone_is_off(zone) is not False:
                    changes[zone] = floor
        heater = self.build_heater_desired(
            changes=changes,
            enables={zone: True for zone in target},
        )
        return {"operationMode": MODE_HEAT, "heater": heater}

    def zone_current(self, zone: str) -> float | None:
        """The current value. Only temperature mats report one; a stepped mat gives `None`."""
        value = self._zone_current_raw(zone)
        if value is None and (mirror := self._mirror_zone(zone)) is not None:
            value = self._zone_current_raw(mirror)
        return value

    def _zone_current_raw(self, zone: str) -> float | None:
        state = self.zone_state(zone)
        if state is None:
            return None
        temperature = state.get("temperature")
        if isinstance(temperature, dict) and temperature.get("current") is not None:
            return float(temperature["current"])
        return None

    def zone_enabled(self, zone: str) -> bool | None:
        value = self._zone_enabled_raw(zone)
        if value is None and (mirror := self._mirror_zone(zone)) is not None:
            value = self._zone_enabled_raw(mirror)
        return value

    def _zone_enabled_raw(self, zone: str) -> bool | None:
        state = self.zone_state(zone)
        if state is None:
            return None
        value = state.get("enable")
        return bool(value) if value is not None else None

    @property
    def over_safe_value(self) -> bool:
        """Whether the high-temperature warning line is crossed. A warning, not a control cap.

        No judgement is made while cooling. What `coolControl.safeValue` means is unconfirmed
        — it could be a lower bound or a condensation threshold. Comparing it as if it were a
        heating threshold would report a cooling setpoint as overheating.
        """
        if self.is_cooling:
            return False
        control = self.heat_control
        if control is None or not control.enable_safe or control.safe_value is None:
            return False
        return any(
            (value := self.zone_setting(zone)) is not None and value > control.safe_value
            for zone in self.zones
        )

    @property
    def service_name(self) -> str:
        return SERVICE_NAMES.get(self.service_code, str(self.service_code))

    def build_season_desired(self, season: int) -> dict[str, Any]:
        """The desired that switches season (heating to cooling and back).

        **It does exactly what the app does.** `MateConstants.mateMqttPayload("seasonSetting")`
        produces nothing but `Desired(event=..., season=...)`, on the same shadow-update topic
        already in use. `async_control` attaches `event.modelCode` to every command — a path
        verified on a real device.

        **There are only two values.** The app's season screen has two buttons
        (`onWinterIconClick` -> 0, `onSummerIconClick` -> 2) and no third. The `coolPlus` that
        SmartThings displays is a label of theirs, not a Navien value — an exhaustive search of
        the app's strings finds none.

        So **only known values are sent**, and anything else is refused.
        """
        if season not in SEASON_NAMES:
            raise ValueError(f"확인된 계절 값이 아닙니다: {season}")
        return {"season": season}

    def build_heater_desired(
        self,
        changes: dict[str, float] | None = None,
        enables: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Build the `heater` desired.

        The app sends the current value of every zone, including the ones that did not change.
        This follows the same approach rather than relying on shadow merging — that is the
        form verified on a real device.

        `enables` can override `enable` per zone. `enable: true` was verified on a real
        device; **`false` was not**, because no temperature mat was available to test it.
        """
        changes = changes or {}
        enables = enables or {}
        # While cooling, `coolControl` applies. The setpoint path is the same as heating,
        # `heater.<zone>.temperature.set` — a real-device report showed a cooling setpoint of
        # 24.5 (cooling range 20-35) arriving on it.
        #
        # Cooling on a stepped (`1.0L`) four-season model is still unknown. `select` stays out
        # of the way while cooling, so control never reaches here.
        control = self.active_control
        if control is None or not control.is_known:
            raise ValueError(f"제어 축을 모르는 기기입니다 (unit={control.unit if control else None})")

        axis = "level" if control.is_level else "temperature"
        heater: dict[str, Any] = {}
        for zone in self.zones:
            value = changes.get(zone, self.zone_setting(zone))
            if value is None:
                continue
            number: Any = int(value) if control.is_level else float(value)

            if zone in enables:
                enabled = enables[zone]
            elif control.is_level and zone in changes:
                # On a stepped mat `level 0` and `enable false` move together — confirmed on a
                # real device. It is the leftmost position of the app's slider, 운전 대기.
                #
                # **Applied only to the zone being changed.** Without the `zone in changes`
                # condition, dropping one side to standby would overwrite the other side's
                # `enable` and switch it off too.
                enabled = number > 0
            else:
                current = self.zone_enabled(zone)
                enabled = True if current is None else current

            heater[zone] = {"enable": enabled, axis: {"set": number}}
        if not heater:
            # **There is effectively one way to reach this**: the device has never sent any
            # state. The app sends the current setpoints along with the command and this does
            # the same, which is impossible without knowing them.
            #
            # The `connected` flag in the REST device list is **closer to "is registered"**
            # and stays at 1 for a while after a device drops off Wi-Fi. So the entity looks
            # healthy and pressing it lands here.
            #
            # Reporting only "no zone value to send" leaves the user with nothing to do, so
            # the message says what to check as well.
            if not self.reported.get("heater"):
                # **The power switch still works in this situation.** It sends only
                # `operationMode`, so it can be built without knowing any setpoint. Once on,
                # the device sends state and temperature control starts working — the message
                # points at that route.
                raise ValueError(
                    "기기가 아직 상태를 보내오지 않아 온도를 함께 실을 수 "
                    "없습니다. 「전원」 스위치로는 켤 수 있습니다 — 켜면 기기가 "
                    "상태를 보내고 그 뒤로 온도 조절도 됩니다. 그래도 안 되면 "
                    "매트가 전원·Wi-Fi 에 연결돼 있는지 확인해 주세요."
                )
            raise ValueError("보낼 구역 값이 없습니다.")
        return heater

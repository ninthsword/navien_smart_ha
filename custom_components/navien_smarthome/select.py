"""Heating step selection (`heatControl.unit == "1.0L"`).

A step is not a continuous quantity. There are nine discrete states, and `0` among them is
not a number but the state the app calls 운전 대기 (standby). A slider gets three things
wrong:

- it has to be aimed — in a narrow card row, overshooting by one means a real temperature
  difference
- its ticks cannot be named — the app says 운전 대기 where a slider shows "step 0"
- the server reports `rangeMin` as 1, so 0 cannot be entered. It displays but never sets

This is the same reasoning that kept steps out of `climate` ("a step is not a temperature").
A step is not a continuous quantity either.

Temperature mats (`0.5C`) are genuinely continuous, so they use `climate`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .airone import AironeDevice
from .const import (
    AIRONE_OPTION_SLEEP,
    LEVEL_STANDBY,
    MAT_VOLUME_NAMES,
    SEASON_NAMES,
    ZONE_NAMES,
    level_label,
)
from .coordinator import NavienSmartCoordinator
from .entity import AironeEntity, NavienSmartEntity
from .models import NavienDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    for device in (coordinator.data or {}).values():
        # Only four-season models have a season, which is when the server sends `coolControl`.
        if device.is_four_season:
            entities.append(NavienSmartSeasonSelect(coordinator, device))

        # **`functions.beep` present means the device makes sound.** The app opens its volume
        # screen without consulting this value, and its actual criterion was never found —
        # the per-model table has no volume row either. Better to use what the server declares
        # about itself: not creating the entity is safer than sending a command to a device
        # that has no such control.
        if device.has_beep:
            entities.append(NavienSmartVolumeSelect(coordinator, device))

        control = device.heat_control
        if control is None or not control.is_level:
            continue
        entities.extend(
            NavienSmartLevelSelect(coordinator, device, zone) for zone in device.zones
        )

    for airone in coordinator.airone.values():
        # Not created unless the server reports the combinations that can be selected.
        if airone.selectable_modes:
            entities.append(AironeModeSelect(coordinator, airone))
        # The selectable fan speeds depend on the mode. Created only when at least one mode
        # offers two or more of them.
        if any(
            len(airone.fan_choices(mode.mode, mode.option)) > 1
            for mode in airone.selectable_modes
        ):
            entities.append(AironeFanSelect(coordinator, airone))

    async_add_entities(entities)


class NavienSmartSeasonSelect(NavienSmartEntity, SelectEntity):
    """The season of a four-season mat — heating or cooling.

    **This is not modelled as `climate` heat/cool.** The season is not what the device is
    doing right now but **which way it is configured**. The app puts it on the device settings
    screen rather than the control screen, and changing it splits the temperature range
    wholesale (heating 28-45, cooling 20-35). As a `climate` mode button, "currently heating"
    and "configured for heating" would collapse into the same place.

    Only the two values the app uses are offered. An unrecognised value from the server
    leaves the state empty.
    """

    _attr_icon = "mdi:sun-snowflake-variant"
    _attr_options = list(SEASON_NAMES.values())

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: NavienDevice,
    ) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_season"
        self._attr_name = "계절"

    @property
    def current_option(self) -> str | None:
        device = self.device
        if device is None:
            return None
        # Unknown values leave it empty. `season_name` can produce text that is not in the
        # option list (such as "알 수 없음(3)"), and using that as the state contradicts the list.
        return None if device.season is None else SEASON_NAMES.get(device.season)

    async def async_select_option(self, option: str) -> None:
        device = self.device
        if device is None:
            return
        for value, label in SEASON_NAMES.items():
            if label == option:
                await self.coordinator.async_send(
                    device, device.build_season_desired(value)
                )
                return


class NavienSmartVolumeSelect(NavienSmartEntity, SelectEntity):
    """Button sound volume — mute, 1, 2, 3.

    The steps match the app's volume screen: `MateDeviceSettingSoundVolumeFragment` puts the
    `selectedIndex` it picked (0-3) straight into `Desired.volume`.

    **Not a switch, because this is a level rather than an on/off.** A separate `control-beep`
    silences the button sound entirely, but it uses a different value scheme and the app only
    attaches it to models from 2024 onwards — leave it alone.
    """

    _attr_icon = "mdi:volume-high"
    _attr_options = list(MAT_VOLUME_NAMES.values())
    # **Set once and left alone, so it goes under configuration.** That moves it further down
    # the device page and drops it from the default Overview dashboard (the frontend's
    # `computeDefaultViewStates` hides anything with an `entity_category`).
    #
    # **Not applied to the control lock.** A household with children toggles that daily, so it
    # must not vanish from the Overview — which is exactly why v0.13.0 went to the trouble of
    # promoting a read-only sensor to a switch.
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: NavienDevice,
    ) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_volume"
        self._attr_name = "조작음 음량"

    @property
    def current_option(self) -> str | None:
        device = self.device
        return device.volume_name if device is not None else None

    async def async_select_option(self, option: str) -> None:
        device = self.device
        if device is None:
            return
        for value, label in MAT_VOLUME_NAMES.items():
            if label == option:
                await self.coordinator.async_send(
                    device, device.build_volume_desired(value)
                )
                return


class NavienSmartLevelSelect(NavienSmartEntity, SelectEntity):
    """Heating step for one zone."""

    _attr_icon = "mdi:thermometer-lines"

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: NavienDevice,
        zone: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._zone = zone
        self._attr_unique_id = f"{device.device_id}_{zone}_level"

        label = device.zone_names.get(zone) or ZONE_NAMES.get(zone, zone)
        self._attr_name = f"{label} 단계" if device.is_double else "난방 단계"

        control = device.heat_control
        assert control is not None  # filtered out during setup
        low = int(control.range_min if control.range_min is not None else 1)
        high = int(control.range_max if control.range_max is not None else 8)

        # The server reports `rangeMin` as 1, but the device accepts 0 (standby) — confirmed
        # on a real device. So 0 is prepended. This does not apply to temperature mats.
        self._levels = [LEVEL_STANDBY, *range(low, high + 1)]
        self._attr_options = [level_label(value) for value in self._levels]

    @property
    def available(self) -> bool:
        """Stay out of the way while cooling.

        **Stepped mats remain blocked.** The cooling value scheme was only confirmed for
        temperature mats (`0.5C`) — the real-device report was an EMF520, a temperature mat.
        What step range a stepped four-season model uses while cooling is still unknown.

        `options` is fixed once at construction, so a range that splits cannot be reflected at
        runtime. Open this up when a report arrives.
        """
        device = self.device
        return super().available and device is not None and not device.is_cooling

    @property
    def current_option(self) -> str | None:
        device = self.device
        if device is None or device.is_cooling:
            return None
        value = device.zone_setting(self._zone)
        if value is None:
            return None
        label = level_label(int(value))
        # A value outside the list leaves the state empty. Do not invent an option.
        return label if label in (self._attr_options or []) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        value = device.zone_setting(self._zone)
        attrs: dict[str, Any] = {
            "zone": self._zone,
            # The raw value stays available for automations and templates that need a number.
            "level": None if value is None else int(value),
            "enabled": device.zone_enabled(self._zone),
        }
        control = device.heat_control
        if control is not None and control.safe_value is not None:
            # The high-temperature warning line, not an upper bound — the app allows settings
            # above it too.
            attrs["high_temp_warning_level"] = int(control.safe_value)
        return attrs

    async def async_select_option(self, option: str) -> None:
        device = self.device
        if device is None:
            return
        try:
            level = self._levels[(self._attr_options or []).index(option)]
        except ValueError:
            return
        control = device.active_control
        off = control.off_value if control is not None else None
        if off is not None and level <= off:
            # **Standby turns that zone off.** It is the same command as "off" on a
            # temperature mat, so it shares the same path (issue #16).
            #
            # **Dropping one side to standby behaves exactly as before** — `build_zone_off`
            # produces the same `heater`. Only dropping **the last remaining zone** differs,
            # and the device refuses that anyway, so the reason is surfaced.
            # (Observed: with the left side at 0, setting the right to 0 leaves `0, 1`.)
            await self.coordinator.async_send(
                device, device.build_zone_off([self._zone])
            )
            return
        heater = device.build_heater_desired({self._zone: level})
        await self.coordinator.async_send(device, {"heater": heater})


class AironeModeSelect(AironeEntity, SelectEntity):
    """Operating mode.

    The options are built **only from server metadata** (`did.roomController.mode`). No model
    table goes in the source — the same approach that worked for mats.

    The axis is cut the way the app cuts it: turbo, saving and baseline belong to fan speed,
    not here. That keeps the list short, so changing only the fan speed does not mean digging
    through the mode list.
    """

    _attr_translation_key = "airone_mode"
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_mode"
        self._modes = device.selectable_modes
        self._attr_options = [mode.label for mode in self._modes]

    @property
    def current_option(self) -> str | None:
        device = self.device
        if device is None or device.mode is None:
            return None
        # Find which option the current state corresponds to. Sleep needs the option field
        # as well to be told apart.
        for choice in self._modes:
            if choice.mode != device.mode:
                continue
            if choice.is_sleep != (device.option == AIRONE_OPTION_SLEEP):
                continue
            return choice.label
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        return {
            "mode": device.mode,
            "option": device.option,
            # The full label the app shows, kept available for automations and templates.
            "full_label": device.mode_label,
        }

    async def async_select_option(self, option: str) -> None:
        device = self.device
        if device is None:
            return
        try:
            chosen = self._modes[(self._attr_options or []).index(option)]
        except ValueError:
            return
        # On a mode change, `build_mode_desired` fills the fan speed from the server values.
        await self.coordinator.async_airone_mode(device, chosen.mode, chosen.option)


class AironeFanSelect(AironeEntity, SelectEntity):
    """Fan speed.

    **The gentle/low/high/auto set and the turbo/saving/baseline set share one axis**, the way
    the app treats them (the second field of `AironeModeCode.labelFor`). A device may offer
    only one set or the other, and either way it should look like a single fan-speed control
    to the user.

    Only the combinations the server reports for the current mode are offered.
    """

    _attr_translation_key = "airone_fan"
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_fan"

    @property
    def _choices(self) -> tuple[Any, ...]:
        device = self.device
        if device is None:
            return ()
        return device.fan_choices(device.mode, device.option)

    @property
    def options(self) -> list[str]:
        return [choice.label for choice in self._choices]

    @property
    def available(self) -> bool:
        """Step aside **only when there is nothing to choose at all.**

        **One option and no options are different things.** In sleep mode the app still shows
        the fan speed as auto and merely makes it unpressable — it does not hide it. Cooking
        (high only) and auto operation (auto only) behave the same way.

        Written as `> 1`, the entity vanished entirely the moment the mode changed. To the
        user that removes any answer to "what is the fan speed right now", which is worse than
        useless. With one option, show it — selecting it changes nothing.
        """
        return super().available and bool(self._choices)

    @property
    def current_option(self) -> str | None:
        device = self.device
        if device is None:
            return None
        label = device.current_fan_label()
        return label if label in self.options else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        return {"option": device.option, "air_volume": device.air_volume}

    async def async_select_option(self, option: str) -> None:
        device = self.device
        if device is None or device.mode is None:
            return
        chosen = next((c for c in self._choices if c.label == option), None)
        if chosen is None:
            return
        await self.coordinator.async_airone_mode(
            device, device.mode, chosen.option, air_volume=chosen.air_volume
        )

"""Thermostats for temperature mats (`heatControl.unit == "0.5C"`).

A temperature mat also reports `temperature.current`, so `climate` is the right fit — the
current-temperature field of the thermostat card gets filled in.

Stepped mats (`1.0L`) get no entity on this platform. They send no current value, so half
the card would stay empty, and showing a step in degrees would mislead the user. Those use a
`number` slider instead.

**Caution: no temperature mat has ever been operated directly by us.** This household has
only stepped mats. The heating and cooling display and control of the four-season model
(EMF520) were **confirmed by a user report**. The structure was settled from the app code,
but sending `enable: false` in particular was never confirmed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .const import MODE_HEAT, ZONE_NAMES
from .coordinator import NavienSmartCoordinator
from .entity import NavienSmartEntity
from .models import NavienDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[NavienSmartThermostat] = []
    for device in (coordinator.data or {}).values():
        control = device.heat_control
        if control is None or not control.is_celsius:
            continue
        entities.extend(
            NavienSmartThermostat(coordinator, device, zone) for zone in device.zones
        )
    async_add_entities(entities)


class NavienSmartThermostat(NavienSmartEntity, ClimateEntity):
    """Heating temperature for one zone."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: NavienDevice,
        zone: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._zone = zone
        self._attr_unique_id = f"{device.device_id}_{zone}_thermostat"

        label = device.zone_names.get(zone) or ZONE_NAMES.get(zone, zone)
        self._attr_name = label if device.is_double else "난방"

        # The range is not frozen in `__init__`. On a four-season model, switching WARM/COOL
        # in the app **splits the range itself** (heating 28-45, cooling 20-35). Frozen, a
        # cooling setpoint would fall below its own minimum and break the card.
        assert device.heat_control is not None  # filtered out during setup

    @property
    def _control(self) -> Any:
        """The control descriptor in force right now — `coolControl` while cooling."""
        device = self.device
        return device.active_control if device is not None else None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Show cooling only while cooling, and heating otherwise.

        `season` (WARM/COOL) is **chosen in the app**, and no way to change it from here has
        been confirmed. So HA offers no heating/cooling switch: making it look selectable
        would mean nothing happens when it is pressed.
        """
        device = self.device
        cooling = device is not None and device.is_cooling
        return [HVACMode.OFF, HVACMode.COOL if cooling else HVACMode.HEAT]

    @property
    def min_temp(self) -> float:
        control = self._control
        return float((control.range_min if control else None) or 20)

    @property
    def max_temp(self) -> float:
        control = self._control
        return float((control.range_max if control else None) or 45)

    @property
    def target_temperature_step(self) -> float:
        control = self._control
        return control.step if control else 0.5

    @property
    def available(self) -> bool:
        """Used while cooling too.

        Up to v0.9.0 this stayed out of the way during cooling, because the value scheme was
        unknown. The server now sends `coolControl` (range, step, warning line), and a user
        report on a real device showed the setpoint arriving through the same
        `heater.<zone>.temperature.set` as heating.

        Cooling is assumed only when `season` is **`SEASON_SUMMER` (2)**. A missing or
        unrecognised value falls back to heating, so the wrong range is never used.
        """
        return super().available

    @property
    def current_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.zone_current(self._zone)

    @property
    def target_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.zone_setting(self._zone)

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Considers both the device power and the zone's `enable`.

        A powered-off device does not heat, even with the zone enabled.
        """
        device = self.device
        if device is None:
            return None
        # **Unknown is not "off".** `is_on` returns `False` when `operationMode` is absent,
        # and taking that at face value declares a device whose state has not arrived yet to
        # be off — the cause of both zones showing as off in the four-season report.
        if device.operation_mode is None:
            return None
        if not device.is_on:
            return HVACMode.OFF
        enabled = device.zone_enabled(self._zone)
        running = HVACMode.COOL if device.is_cooling else HVACMode.HEAT
        return running if enabled is not False else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self.hvac_mode
        if mode is None:
            return None
        if mode is HVACMode.HEAT:
            return HVACAction.HEATING
        if mode is HVACMode.COOL:
            return HVACAction.COOLING
        return HVACAction.OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        attrs: dict[str, Any] = {"zone": self._zone}
        control = device.heat_control
        if control is not None and control.safe_value is not None:
            # The high-temperature warning line, not an upper bound.
            attrs["high_temp_warning_temperature"] = control.safe_value
        if device.is_four_season:
            attrs["four_season"] = True
            attrs["season"] = device.season
        return attrs

    @property
    def _target_zones(self) -> tuple[str, ...]:
        """The zones this operation applies to — **always just this one.**

        v0.9.0 through v0.11.0 sent the same value to both zones when cooling on a split mat,
        on the strength of a line in the app:

            COOL 모드 — 매트의 좌우가 같은 온도로 동작합니다

        **That line leaves the model out.** Navien's product page says:

            0.5℃ 분리 냉난방 기술로 좌우 원하는 온도로
            해당 기능은 **사계절형 Pro 모델에만** 적용됩니다

        So Pro keeps the sides independent even while cooling, and Air ties them together.
        That amounted to **hard-coding per-model behaviour**, which this integration has
        decided not to do — and the server does not even report Pro versus Air.

        So the command goes **only to the zone that was touched.** A model that ties the sides
        together will return both values equal, and that is what gets displayed. **Do not
        pre-empt what the device does.**
        """
        return (self._zone,)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get("temperature")
        device = self.device
        if device is None or temperature is None:
            return
        value = float(temperature)
        zones = self._target_zones
        heater = device.build_heater_desired(
            changes={zone: value for zone in zones},
            enables={zone: True for zone in zones},
        )
        await self.coordinator.async_send(
            device, {"operationMode": MODE_HEAT, "heater": heater}
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        device = self.device
        if device is None:
            return
        zones = self._target_zones
        if hvac_mode is not HVACMode.OFF:
            # Turning on works the same for heating and cooling. `season` decides which of
            # the two it is, and that is chosen in the app.
            #
            # **A zone that was off needs its value raised as well** (issue #16). This used
            # to send `enable: true` while resending 27.5 (= off) as the temperature, so the
            # device powered on with that zone still off.
            await self.coordinator.async_send(device, device.build_zone_on(zones))
            return
        # **`enable: false` alone does not turn it off** (issue #16). The device only accepts
        # it once the value is lowered to `off_value`. With no zone left on, this falls back
        # to powering the device off — that decision lives inside `build_zone_off`.
        await self.coordinator.async_send(device, device.build_zone_off(zones))

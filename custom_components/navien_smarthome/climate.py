"""Thermostats for temperature mats (`heatControl.unit` `"0.5C"` or `"1.0C"`).

A temperature mat also reports `temperature.current`, so `climate` is the right fit — the
current-temperature field of the thermostat card gets filled in.

Stepped mats (`1.0L`) get no entity on this platform. They send no current value, so half
the card would stay empty, and showing a step in degrees would mislead the user. Those use a
`number` slider instead.

An NR-67D boiler that declares an ondol range gets one thermostat for its heating water.

**Caution: no temperature mat has ever been operated directly by us.** This household has
only stepped mats. The heating and cooling display and control of the four-season model
(EMF520) were **confirmed by a user report**. The structure was settled from the app code,
but sending `enable: false` in particular was never confirmed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .boiler import (
    BOILER_STATE_HEATING,
    BOILER_STATE_IDLE,
    BOILER_STATE_OFF,
    BoilerDevice,
)
from .const import MODE_HEAT, ZONE_NAMES
from .coordinator import NavienSmartCoordinator
from .entity import BoilerEntity, NavienSmartEntity, boiler_entities_as_declared
from .models import NavienDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[ClimateEntity] = []
    for device in (coordinator.data or {}).values():
        control = device.heat_control
        if control is None or not control.is_celsius:
            continue
        entities.extend(
            NavienSmartThermostat(coordinator, device, zone) for zone in device.zones
        )

    def boiler_thermostats(device: BoilerDevice) -> list[ClimateEntity]:
        if device.model_code != "20" or device.temperature_bounds("ondol") is None:
            return []
        return [NavienBoilerThermostat(coordinator, device)]

    entities.extend(
        boiler_entities_as_declared(
            entry, coordinator, async_add_entities, boiler_thermostats
        )
    )
    async_add_entities(entities)


class NavienSmartThermostat(NavienSmartEntity, ClimateEntity):
    """Heating temperature for one zone."""

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
    # HA declares a cached descriptor; retain dynamic property semantics.
    def hvac_modes(self) -> list[HVACMode]:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Show cooling only while cooling, and heating otherwise.

        `season` (WARM/COOL) is **chosen in the app**, and no way to change it from here has
        been confirmed. So HA offers no heating/cooling switch: making it look selectable
        would mean nothing happens when it is pressed.
        """
        device = self.device
        cooling = device is not None and device.is_cooling
        return [HVACMode.OFF, HVACMode.COOL if cooling else HVACMode.HEAT]

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def min_temp(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        control = self._control
        return float((control.range_min if control else None) or 20)

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def max_temp(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        control = self._control
        return float((control.range_max if control else None) or 45)

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def target_temperature_step(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        control = self._control
        return control.step if control else 0.5

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Used while cooling too.

        Up to v0.9.0 this stayed out of the way during cooling, because the value scheme was
        unknown. The server now sends `coolControl` (range, step, warning line), and a user
        report on a real device showed the setpoint arriving through the same
        `heater.<zone>.temperature.set` as heating.

        Cooling is assumed only when `season` is **`SEASON_SUMMER` (2)**. A missing or
        unrecognised value falls back to heating, so the wrong range is never used.
        """
        return super().available

    def _current_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.zone_current(self._zone)

    def _target_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.zone_setting(self._zone)

    # Home Assistant 2026.11 serves the temperatures under native_ names and declares the old
    # names Final and deprecated until 2027.11; 2026.8 to 2026.10 read only the old names. A
    # class providing both keeps the native ones without a deprecation warning. The block is
    # hidden from type checkers, which would otherwise see an override of a Final name on
    # 2026.11 or of a cached descriptor on the older versions.
    if not TYPE_CHECKING:
        _attr_temperature_unit = _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
        current_temperature = native_current_temperature = property(
            _current_temperature
        )
        target_temperature = native_target_temperature = property(_target_temperature)

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def hvac_mode(self) -> HVACMode | None:  # pyright: ignore[reportIncompatibleVariableOverride]
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
    # HA declares a cached descriptor; retain dynamic property semantics.
    def hvac_action(self) -> HVACAction | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        mode = self.hvac_mode
        if mode is None:
            return None
        if mode is HVACMode.HEAT:
            return HVACAction.HEATING
        if mode is HVACMode.COOL:
            return HVACAction.COOLING
        return HVACAction.OFF

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def extra_state_attributes(self) -> dict[str, Any] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
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
        """Store the value, and power on only when the device is on, unknown or asked to.

        The app sends the temperature **without `operationMode`** to a powered-off mat, and
        the device keeps the value and starts at it when powered on (upstream v0.21.0). So a
        mat known to be off only stores the value. An unknown state powers on as before.

        A call carrying `hvac_mode` applies that mode, as Home Assistant's service defines:
        `off` sends the value and then runs the existing off path, a mode this entity offers
        powers on, and any other mode is refused before anything is sent. Home Assistant
        passes `hvac_mode` through without checking it against `hvac_modes`.
        """
        temperature = kwargs.get("temperature")
        device = self.device
        if device is None or temperature is None:
            return
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode is not None and hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(
                f"이 매트는 지금 '{hvac_mode}' 모드를 지원하지 않습니다"
            )
        value = float(temperature)
        zones = self._target_zones
        heater = device.build_heater_desired(
            changes={zone: value for zone in zones},
            enables={zone: True for zone in zones},
        )
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_send(device, {"heater": heater})
            await self.async_set_hvac_mode(HVACMode.OFF)
            return
        if (
            hvac_mode is None
            and device.operation_mode is not None
            and not device.is_on
        ):
            await self.coordinator.async_send(device, {"heater": heater})
            return
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
        # it once the value is lowered to `off_value`. With no zone left on, a single-zone mat
        # powers off and a double mat is refused — that decision lives inside `build_zone_off`.
        await self.coordinator.async_send(device, device.build_zone_off(zones))


_BOILER_HVAC_ACTIONS = {
    BOILER_STATE_HEATING: HVACAction.HEATING,
    BOILER_STATE_IDLE: HVACAction.IDLE,
    BOILER_STATE_OFF: HVACAction.OFF,
}


class NavienBoilerThermostat(BoilerEntity, ClimateEntity):
    """The NR-67D heating-water setpoint and the boiler's power.

    Its off is the boiler's power off, which stops hot water as well. The setpoint is stored
    whether or not the boiler is on, as the app does.
    """

    _attr_name = "난방"
    _attr_icon = "mdi:radiator"
    _attr_precision = PRECISION_HALVES
    _attr_target_temperature_step = 0.5

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_boiler_heating"
        bounds = device.temperature_bounds("ondol")
        if bounds is None:
            raise ValueError("ondol 설정온도 범위가 없습니다")
        # The range seen at creation, kept only for the moment the device or the range is
        # missing; the properties below normally use the range the server reports now.
        self._fallback_bounds = bounds

    def _bounds(self) -> tuple[float, float]:
        device = self.device
        if device is None:
            return self._fallback_bounds
        return device.temperature_bounds("ondol") or self._fallback_bounds

    def _supports_power(self) -> bool:
        device = self.device
        return device is not None and device.supports_feature("powerUse")

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def supported_features(self) -> ClimateEntityFeature:  # pyright: ignore[reportIncompatibleVariableOverride]
        features = ClimateEntityFeature.TARGET_TEMPERATURE
        if self._supports_power():
            features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        return features

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def hvac_modes(self) -> list[HVACMode]:  # pyright: ignore[reportIncompatibleVariableOverride]
        if self._supports_power():
            return [HVACMode.HEAT, HVACMode.OFF]
        return [HVACMode.HEAT]

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def min_temp(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._bounds()[0]

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def max_temp(self) -> float:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._bounds()[1]

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        device = self.device
        return (
            super().available
            and device is not None
            and device.temperature_bounds("ondol") is not None
        )

    def _current_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.supply_temperature

    def _target_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.ondol_target_temperature

    # The old and native names, for the reason given on NavienSmartThermostat.
    if not TYPE_CHECKING:
        _attr_temperature_unit = _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
        current_temperature = native_current_temperature = property(
            _current_temperature
        )
        target_temperature = native_target_temperature = property(_target_temperature)

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def hvac_mode(self) -> HVACMode | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        device = self.device
        power = None if device is None else device.switch_state("power")
        if power is None:
            return None
        return HVACMode.HEAT if power else HVACMode.OFF

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def hvac_action(self) -> HVACAction | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        device = self.device
        if device is None or device.operating_state is None:
            return None
        return _BOILER_HVAC_ACTIONS.get(device.operating_state)

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def extra_state_attributes(self) -> dict[str, Any] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        device = self.device
        if device is None:
            return None
        return {
            "operation_busy": device.operation_busy,
            "heating_idle": device.heating_is_idle,
            "status_age_seconds": (
                None if device.status_age is None else round(device.status_age, 1)
            ),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Send the setpoint, then apply a mode the call carries.

        Home Assistant passes `hvac_mode` through without checking it against `hvac_modes`, so
        a mode this entity does not offer is refused here before anything is sent.
        """
        temperature = kwargs.get("temperature")
        device = self.device
        if device is None or temperature is None:
            return
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode is not None and hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(
                f"이 보일러는 '{hvac_mode}' 모드를 지원하지 않습니다"
            )
        await self.coordinator.async_boiler_temperature(
            device, "ondol", float(temperature)
        )
        if hvac_mode is not None:
            await self.async_set_hvac_mode(hvac_mode)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Send the power command only when it is declared and would change something."""
        device = self.device
        if device is None or not device.supports_feature("powerUse"):
            return
        turn_on = hvac_mode != HVACMode.OFF
        if device.switch_state("power") != turn_on:
            await self.coordinator.async_boiler_power(device, turn_on)

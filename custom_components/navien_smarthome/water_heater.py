"""The NR-67D boiler's hot-water setpoint.

The entity offers only its target temperature. Power stays on the thermostat and the power
switch, because the boiler's power button stops heating and hot water together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.water_heater import STATE_GAS, WaterHeaterEntity
from homeassistant.const import PRECISION_HALVES, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .boiler import BoilerDevice
from .coordinator import NavienSmartCoordinator
from .entity import BoilerEntity, boiler_entities_as_declared

# 2026.8 and 2026.9 define this name only in the package, and 2026.10 re-exports it there
# privately, so no single import location type-checks on every supported version.
if not TYPE_CHECKING:
    from homeassistant.components.water_heater import WaterHeaterEntityFeature


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    def boiler_water_heaters(device: BoilerDevice) -> list[WaterHeaterEntity]:
        if device.model_code != "20" or device.temperature_bounds("hot_water") is None:
            return []
        return [NavienBoilerWaterHeater(coordinator, device)]

    async_add_entities(
        boiler_entities_as_declared(
            entry, coordinator, async_add_entities, boiler_water_heaters
        )
    )


class NavienBoilerWaterHeater(BoilerEntity, WaterHeaterEntity):
    """The NR-67D hot-water setpoint, stored whether or not the boiler is on."""

    _attr_name = "온수"
    _attr_icon = "mdi:water-thermometer"
    _attr_precision = PRECISION_HALVES
    _attr_target_temperature_step = 0.5

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_boiler_water_heater"
        bounds = device.temperature_bounds("hot_water")
        if bounds is None:
            raise ValueError("hot_water 설정온도 범위가 없습니다")
        # The range seen at creation, kept only for the moment the device or the range is
        # missing; the properties below normally use the range the server reports now.
        self._fallback_bounds = bounds

    def _bounds(self) -> tuple[float, float]:
        device = self.device
        if device is None:
            return self._fallback_bounds
        return device.temperature_bounds("hot_water") or self._fallback_bounds

    @property
    def min_temp(self) -> float:
        return self._bounds()[0]

    @property
    def max_temp(self) -> float:
        return self._bounds()[1]

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        device = self.device
        return (
            super().available
            and device is not None
            and device.temperature_bounds("hot_water") is not None
        )

    def _current_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.hot_water_temperature

    def _target_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.hot_water_target_temperature

    # Home Assistant 2026.11 serves the temperatures under native_ names and declares the old
    # names Final and deprecated until 2027.11; 2026.8 to 2026.10 read only the old names. A
    # class providing both keeps the native ones without a deprecation warning. The block is
    # hidden from type checkers, which would otherwise see an override of a Final name on
    # 2026.11 or of a cached descriptor on the older versions. The feature flag is here too,
    # because its name type-checks on no single import location.
    if not TYPE_CHECKING:
        _attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE
        _attr_temperature_unit = _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
        current_temperature = native_current_temperature = property(
            _current_temperature
        )
        target_temperature = native_target_temperature = property(_target_temperature)

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def current_operation(self) -> str | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Home Assistant's water heater has no plain "on"; the boiler is a gas appliance."""
        device = self.device
        power = None if device is None else device.switch_state("power")
        if power is None:
            return None
        return STATE_GAS if power else STATE_OFF

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
        """Send the setpoint; an `operation_mode` is ignored, as there is no operation list."""
        temperature = kwargs.get("temperature")
        device = self.device
        if device is None or temperature is None:
            return
        await self.coordinator.async_boiler_temperature(
            device, "hot_water", float(temperature)
        )

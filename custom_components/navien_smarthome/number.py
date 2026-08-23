"""Airone target humidity and NR-67D boiler setpoints.

Humidity is used only in dehumidify and ventilating-dehumidify. **The range is never
hard-coded** — the server reports `min`/`max` in `additionalData` for each operating
combination, and that is the only source. If it reports none, no entity is created.

Unlike a step, humidity is a genuinely continuous quantity, so a slider fits — the
opposite of the reasoning that turned mat steps into `select`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .airone import AironeDevice
from .boiler import BoilerDevice
from .const import AIRONE_HUMIDITY_STEP, AIRONE_OPTION_NONE
from .coordinator import NavienSmartCoordinator
from .entity import AironeEntity, BoilerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        AironeHumidityNumber(coordinator, device)
        for device in coordinator.airone.values()
        # Created only when the server reports a humidity range for some combination.
        if any(mode.wants_humidity for mode in device.modes)
    ]
    for device in coordinator.boilers.values():
        if device.model_code != "20":
            continue
        if device.temperature_bounds("hot_water") is not None:
            entities.append(BoilerTemperatureNumber(coordinator, device, "hot_water"))
        if device.temperature_bounds("ondol") is not None:
            entities.append(BoilerTemperatureNumber(coordinator, device, "ondol"))
    async_add_entities(entities)


_BOILER_NUMBER_NAMES = {
    "hot_water": ("온수 설정 온도", "mdi:water-thermometer"),
    "ondol": ("난방수 설정 온도", "mdi:radiator"),
}


class BoilerTemperatureNumber(BoilerEntity, NumberEntity):
    """An NR-67D setpoint, controlled inside the server's allowed range whether or not
    the boiler is currently heating."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: BoilerDevice,
        kind: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._kind = kind
        self._attr_unique_id = f"{device.device_id}_{kind}_temperature_setting"
        self._attr_name, self._attr_icon = _BOILER_NUMBER_NAMES[kind]
        bounds = device.temperature_bounds(kind)
        if bounds is None:
            raise ValueError(f"{kind} 설정온도 범위가 없습니다")
        # The range seen at startup. Kept so the slider does not lose its shape the moment
        # the device disappears; normally the property below uses the current range.
        self._fallback_bounds = bounds

    def _bounds(self) -> tuple[float, float]:
        """The range the server reports **now**.

        Reading it once at startup and freezing it would leave the slider showing the old
        range after the server changes it: the user moves the slider and the command is
        rejected, because `build_temperature_payload` validates it again.
        """
        device = self.device
        if device is None:
            return self._fallback_bounds
        return device.temperature_bounds(self._kind) or self._fallback_bounds

    @property
    def native_min_value(self) -> float:
        return self._bounds()[0]

    @property
    def native_max_value(self) -> float:
        return self._bounds()[1]

    @property
    def available(self) -> bool:
        device = self.device
        return (
            super().available
            and device is not None
            and device.temperature_bounds(self._kind) is not None
        )

    @property
    def native_value(self) -> float | None:
        device = self.device
        if device is None:
            return None
        if self._kind == "hot_water":
            return device.hot_water_target_temperature
        return device.ondol_target_temperature

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
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

    async def async_set_native_value(self, value: float) -> None:
        device = self.device
        if device is None:
            return
        await self.coordinator.async_boiler_temperature(device, self._kind, value)


class AironeHumidityNumber(AironeEntity, NumberEntity):
    """Dehumidify target humidity, in percent."""

    _attr_name = "희망습도"
    _attr_icon = "mdi:water-percent"
    _attr_native_unit_of_measurement = "%"
    # The app's −/+ buttons move in steps of 5. The server never reports a step, so follow
    # the app.
    _attr_native_step = AIRONE_HUMIDITY_STEP
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_humidity"
        # The slider bounds are fixed once, at creation. Take a span wide enough to cover
        # every range the server reported, and reject values outside the current
        # combination when the command is built.
        bounds = [
            (mode.humidity_min, mode.humidity_max)
            for mode in device.modes
            if mode.wants_humidity
        ]
        self._attr_native_min_value = min(low for low, _ in bounds if low is not None)
        self._attr_native_max_value = max(high for _, high in bounds if high is not None)

    @property
    def _bounds(self) -> tuple[int, int] | None:
        device = self.device
        if device is None:
            return None
        return device.humidity_bounds(device.mode, device.option)

    @property
    def available(self) -> bool:
        """Stay out of the way outside the dehumidify family — the app shows humidity only there."""
        return super().available and self._bounds is not None

    @property
    def native_value(self) -> float | None:
        device = self.device
        if device is None:
            return None
        value = device.target_humidity
        return None if value is None else float(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        bounds = self._bounds
        if bounds is None:
            return None
        return {"allowed_min": bounds[0], "allowed_max": bounds[1]}

    async def async_set_native_value(self, value: float) -> None:
        device = self.device
        if device is None or device.mode is None:
            return
        bounds = self._bounds
        if bounds is None:
            return
        # The slider steps by 5, but an automation can send any value. Snap to a multiple
        # of 5, then clamp to the range the server reported.
        stepped = int(round(value / AIRONE_HUMIDITY_STEP) * AIRONE_HUMIDITY_STEP)
        target = max(bounds[0], min(bounds[1], stepped))
        option = AIRONE_OPTION_NONE if device.option is None else device.option
        await self.coordinator.async_airone_mode(
            device, device.mode, option, humidity=target
        )

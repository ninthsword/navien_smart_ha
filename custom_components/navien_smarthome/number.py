"""Airone target humidity.

Humidity is used only in dehumidify and ventilating-dehumidify. **The range is never
hard-coded** — the server reports `min`/`max` in `additionalData` for each operating
combination, and that is the only source. If it reports none, no entity is created.

Unlike a step, humidity is a genuinely continuous quantity, so a slider fits — the
opposite of the reasoning that turned mat steps into `select`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .airone import AironeDevice
from .ble_mat import is_mat_entry
from .ble_mat.entities import async_setup_mat_numbers
from .const import AIRONE_HUMIDITY_STEP, AIRONE_OPTION_NONE
from .coordinator import NavienSmartCoordinator
from .entity import AironeEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if is_mat_entry(entry):
        await async_setup_mat_numbers(hass, entry, async_add_entities)
        return
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        AironeHumidityNumber(coordinator, device)
        for device in coordinator.airone.values()
        # Created only when the server reports a humidity range for some combination.
        if any(mode.wants_humidity for mode in device.modes)
    ]
    async_add_entities(entities)


class AironeHumidityNumber(AironeEntity, NumberEntity):
    """Dehumidify target humidity, in percent."""

    _attr_name = "희망습도"
    _attr_icon = "mdi:water-percent"
    _attr_native_unit_of_measurement = "%"
    # The app's -/+ buttons move in steps of 5. The server never reports a step, so follow
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
    # HA declares a cached descriptor; retain dynamic property semantics.
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Stay out of the way outside the dehumidify family — the app shows humidity only there."""
        return super().available and self._bounds is not None

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def native_value(self) -> float | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        device = self.device
        if device is None:
            return None
        value = device.target_humidity
        return None if value is None else float(value)

    @property
    # HA declares a cached descriptor; retain dynamic property semantics.
    def extra_state_attributes(self) -> dict[str, Any] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
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

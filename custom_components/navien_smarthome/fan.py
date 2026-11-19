"""Fan controls for cloud Airone devices."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from . import NavienSmartConfigEntry
from .airone import AironeDevice, AironeFanChoice
from .const import AIRONE_OPTIONS_WITH_WIND
from .coordinator import NavienSmartCoordinator
from .entity import AironeEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a fan for each Airone present at cloud setup."""
    coordinator = entry.runtime_data
    async_add_entities(
        [AironeFan(coordinator, device) for device in coordinator.airone.values()]
    )


def _is_speed(choice: AironeFanChoice) -> bool:
    return choice.option in AIRONE_OPTIONS_WITH_WIND and choice.air_volume in (1, 2, 3)


# Preserve Airone's dynamic availability over HA's cached descriptor.
class AironeFan(AironeEntity, FanEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Combine power and the current operating mode's fan choices."""

    _attr_translation_key = "airone_fan_control"

    def __init__(
        self, coordinator: NavienSmartCoordinator, device: AironeDevice
    ) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_fan_control"

    @property
    def _choices(self) -> tuple[AironeFanChoice, ...]:
        device = self.device
        return () if device is None else device.fan_choices(device.mode, device.option)

    @property
    def _speeds(self) -> list[AironeFanChoice]:
        return sorted(
            (choice for choice in self._choices if _is_speed(choice)),
            key=lambda choice: choice.air_volume or 0,
        )

    def _choice_by_label(self, label: str | None) -> AironeFanChoice | None:
        return next((choice for choice in self._choices if choice.label == label), None)

    @property
    def _current_choice(self) -> AironeFanChoice | None:
        device = self.device
        if device is None:
            return None
        return self._choice_by_label(device.current_fan_label())

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return None if device is None else device.displayed_is_on

    # The choices can change with each report, so keep these HA descriptors dynamic.
    @property
    def percentage(self) -> int | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        is_on = self.is_on
        if is_on is None:
            return None
        if not is_on:
            return 0
        choice = self._current_choice
        if choice is None or not _is_speed(choice):
            return None
        return ordered_list_item_to_percentage(self._speeds, choice)

    @property
    def speed_count(self) -> int:  # pyright: ignore[reportIncompatibleVariableOverride]
        return len(self._speeds)

    @property
    def preset_mode(self) -> str | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        choice = self._current_choice
        return choice.label if choice is not None and not _is_speed(choice) else None

    @property
    def preset_modes(self) -> list[str] | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        presets = [choice.label for choice in self._choices if not _is_speed(choice)]
        return presets or None

    @property
    def supported_features(self) -> FanEntityFeature:  # pyright: ignore[reportIncompatibleVariableOverride]
        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if self.speed_count:
            features |= FanEntityFeature.SET_SPEED
        if self.preset_modes:
            features |= FanEntityFeature.PRESET_MODE
        return features

    def _choice_for_percentage(self, percentage: int | None) -> AironeFanChoice | None:
        if percentage is None or percentage <= 0 or not (speeds := self._speeds):
            return None
        return percentage_to_ordered_list_item(speeds, percentage)

    def _choice_for_preset(self, preset_mode: str) -> AironeFanChoice | None:
        choice = self._choice_by_label(preset_mode)
        return choice if choice is not None and not _is_speed(choice) else None

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        choice = (
            self._choice_for_preset(preset_mode)
            if preset_mode is not None
            else self._choice_for_percentage(percentage)
        )
        await self._async_apply_choice(choice, always_turn_on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if (device := self.device) is not None:
            await self.coordinator.async_airone_power(device, False)

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        await self._async_apply_choice(self._choice_for_percentage(percentage))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self._async_apply_choice(self._choice_for_preset(preset_mode))

    async def _async_apply_choice(
        self, choice: AironeFanChoice | None, *, always_turn_on: bool = False
    ) -> None:
        device = self.device
        if device is None or (choice is None and not always_turn_on):
            return

        if always_turn_on or not device.is_on:
            await self.coordinator.async_airone_power(device, True)
            if choice is None or not self.available:
                return
            # Power acceptance can race with a report or a poll. Keep the chosen
            # label, since the same percentage can mean a new speed.
            device = self.device
            choice = self._choice_by_label(choice.label)

        if (
            device is not None
            and choice is not None
            and (mode := device.mode) is not None
        ):
            await self.coordinator.async_airone_mode(
                device, mode, choice.option, air_volume=choice.air_volume
            )

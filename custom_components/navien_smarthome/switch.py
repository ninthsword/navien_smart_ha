"""Power switches.

**A mat always gets one.** `functions.powerCtrl` does not gate it — that rule had no
evidence behind it, and it left EME-520 owners with no way to turn the device off at all
(issue #16).

The app does not even **read** that field: nothing in it calls
`ResponseDataSource.getPowerCtrl()` (checked exhaustively against APK 2.10.4). The value is
parsed and discarded.

Power goes through `operationMode`. The EME-520 of the reporter in issue #16 had
`powerCtrl: false`, yet its status history shows `operationMode` moving 1↔0 four times, and
all five `operationMode: 1` commands we sent took effect.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .airone import AironeDevice
from .boiler import BoilerDevice
from .const import MODE_HEAT, MODE_POWER_OFF
from .coordinator import NavienSmartCoordinator
from .entity import AironeEntity, BoilerEntity, NavienSmartEntity
from .models import NavienDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    # Not filtered by `has_power_ctrl` — see the module docstring. The field stays in
    # diagnostics, so what the server reports is still visible.
    entities: list[SwitchEntity] = [
        NavienSmartPowerSwitch(coordinator, device)
        for device in (coordinator.data or {}).values()
    ]
    # **Created only when the server declares the lock feature.** The app decides from a
    # model-number table (`case 257: supportLock = false` in `MateInfoData`), but that table
    # lists only five models and cannot keep up with new ones. Trust the server instead.
    entities.extend(
        NavienSmartChildLockSwitch(coordinator, device)
        for device in (coordinator.data or {}).values()
        if device.has_lock_mode
    )
    entities.extend(
        AironePowerSwitch(coordinator, device) for device in coordinator.airone.values()
    )
    for boiler in coordinator.boilers.values():
        if boiler.supports_feature("powerUse"):
            entities.append(BoilerPowerSwitch(coordinator, boiler))
        for kind, feature_key, label, icon in (
            ("fast_dhw", "fastDHWUse", "빠른온수", "mdi:water-boiler"),
            (
                "smart_fast_dhw",
                "smartFastDHWUse",
                "빠른온수 스마트운전",
                "mdi:auto-mode",
            ),
            ("dhw_boost", "DHWBoostUse", "터보온수", "mdi:fire"),
        ):
            if boiler.supports_feature(feature_key):
                entities.append(
                    BoilerFeatureSwitch(coordinator, boiler, kind, label, icon)
                )
    async_add_entities(entities)


class BoilerPowerSwitch(BoilerEntity, SwitchEntity):
    """NR-67D power, the same control as the power button at the top of the app."""

    _attr_name = "전원"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_boiler_power"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return None if device is None else device.switch_state("power")

    async def async_turn_on(self, **kwargs: Any) -> None:
        device = self.device
        if device is not None:
            await self.coordinator.async_boiler_power(device, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        device = self.device
        if device is not None:
            await self.coordinator.async_boiler_power(device, False)


class BoilerFeatureSwitch(BoilerEntity, SwitchEntity):
    """A 1/2-valued switch in the fast-hot-water family, confirmed against the app."""

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: BoilerDevice,
        kind: str,
        label: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._kind = kind
        self._attr_name = label
        self._attr_icon = icon
        self._attr_unique_id = f"{device.device_id}_{kind}"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return None if device is None else device.switch_state(self._kind)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, turn_on: bool) -> None:
        device = self.device
        if device is not None:
            await self.coordinator.async_boiler_switch(device, self._kind, turn_on)


class NavienSmartPowerSwitch(NavienSmartEntity, SwitchEntity):
    """Turns power off and on through `operationMode` 0/1."""

    # **This is the device's primary entity.** With no name of its own, HA shows the device
    # (`Entity.use_device_name` — "the single main feature of a device").
    #
    # so it sorts **first** in the list. The device page orders entities by display name, and
    # in Korean `우(ㅇ)` sorts before `좌(ㅈ)`, which on a split left/right mat produced the
    # odd order `right → power → left`.
    #
    # **Does it qualify?** Every model has exactly one power control per device.
    # `operationMode` sits outside `heater`, and no amount of touching left or right moves it
    # (observed on a real device). The left/right heating steps come in pairs, so they keep
    # their own names. This is the rule `tplink` uses: one per device takes the device name,
    # several keep their own.
    #
    # `_attr_name` wins over `translation_key` (first line of `Entity._name_internal`). The
    # translation entry stays because the Airone power switch still uses it.
    _attr_name = None
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_power"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None or device.operation_mode is None:
            return None
        return device.is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        return {"operation_mode": device.operation_mode, "mode": device.mode_name}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_mode(MODE_HEAT)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_mode(MODE_POWER_OFF)

    async def _async_set_mode(self, mode: int) -> None:
        device = self.device
        if device is None:
            return
        await self.coordinator.async_send(device, {"operationMode": mode})


class NavienSmartChildLockSwitch(NavienSmartEntity, SwitchEntity):
    """Control lock, which locks the buttons on the device itself.

    **v0.12.0 shipped this as a read-only sensor. That was wrong.** The app has a padlock
    button and we missed it: the search only covered calls passing the command as a literal,
    while the lock passes `"lock-on"`/`"lock-off"` through a **variable**.

    On means locked, the same direction as the app's button.
    """

    _attr_icon = "mdi:lock"

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_child_lock"
        self._attr_name = "조작 잠금"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.child_lock if device is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, locked: bool) -> None:
        device = self.device
        if device is None:
            return
        await self.coordinator.async_send(
            device, device.build_child_lock_desired(locked)
        )


class AironePowerSwitch(AironeEntity, SwitchEntity):
    """Turns power off and on through `running` 1/2.

    The older generation inverts this value (running = 2). The coordinator filters those out,
    so only the newer protocol is handled here — deciding the generation in two places invites
    fixing only one of them.
    """

    # The primary entity for the same reason as the mat power switch (see the comment on
    # `NavienSmartPowerSwitch` above). Airone also has one power control per device, while
    # mode, fan speed and target humidity come in several and keep their own names.
    _attr_name = None
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_power"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None or device.running is None:
            return None
        return device.is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        return {"running": device.running, "state": device.running_name}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_power(False)

    async def _async_power(self, turn_on: bool) -> None:
        device = self.device
        if device is None:
            return
        await self.coordinator.async_airone_power(device, turn_on)

"""High-temperature warning and live connection state."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .airone import AironeDevice
from .boiler import BoilerDevice
from .const import AIRONE_SENSOR_KINDS
from .coordinator import NavienSmartCoordinator
from .entity import AironeEntity, BoilerEntity, NavienSmartEntity
from .models import NavienDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for device in (coordinator.data or {}).values():
        control = device.heat_control
        if control is not None and control.enable_safe and control.safe_value is not None:
            entities.append(NavienSmartHighTempWarning(coordinator, device))
        entities.append(NavienSmartErrorProblem(coordinator, device))

    for airone in coordinator.airone.values():
        entities.append(AironeErrorProblem(coordinator, airone))
        if airone.wants_air_sensors:
            entities.append(AironeAirDataMissing(coordinator, airone))

    for boiler in coordinator.boilers.values():
        entities.append(BoilerHotWaterRunning(coordinator, boiler))
        entities.append(BoilerFaultProblem(coordinator, boiler))

    async_add_entities(entities)


class NavienSmartHighTempWarning(NavienSmartEntity, BinarySensorEntity):
    """Whether the setpoint step is above the high-temperature warning line.

    It blocks nothing: the app allows settings above the line too, and only shows a warning.
    """

    _attr_name = "고온경고"
    _attr_device_class = BinarySensorDeviceClass.HEAT
    _attr_icon = "mdi:thermometer-alert"

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_high_temp_warning"

    @property
    def available(self) -> bool:
        """No judgement while cooling — what the safety threshold means in cooling is unconfirmed."""
        device = self.device
        return super().available and device is not None and not device.is_cooling

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return None if device is None else device.over_safe_value


class NavienSmartErrorProblem(NavienSmartEntity, BinarySensorEntity):
    """A non-zero `errorCode` is a problem."""

    _attr_name = "오류"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_problem"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None or device.error_code is None:
            return None
        return device.error_code != 0


class AironeAirDataMissing(AironeEntity, BinarySensorEntity):
    """Whether air-quality values that used to arrive have stopped arriving.

    **The problem was that nothing signalled it at all.** When the link between the air
    monitor and the room controller drops, the server starts sending only temperature and
    humidity; the remaining sensors either freeze at their last value or turn up as
    `unavailable` after a restart. No error code is sent and the poll still succeeds, so
    nothing anywhere shows it.

    The device never claims "this sensor is absent" — all we have is the observation that
    **we used to receive it and now do not**. So this reports missing data rather than a
    device fault.
    """

    _attr_name = "공기질 자료 끊김"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:air-filter"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_air_data_missing"

    def _missing(self) -> list[str] | None:
        device = self.device
        if device is None or not device.known_sensor_kinds:
            return None
        return [
            kind for kind in device.known_sensor_kinds if kind not in device.sensor_kinds
        ]

    @property
    def is_on(self) -> bool | None:
        missing = self._missing()
        return None if missing is None else bool(missing)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        missing = self._missing()
        if device is None or missing is None:
            return None
        attrs: dict[str, Any] = {
            "빠진 항목": [AIRONE_SENSOR_KINDS[k][0] for k in missing],
            "받고 있는 항목": [AIRONE_SENSOR_KINDS[k][0] for k in device.sensor_kinds],
        }
        # A value that froze and a value that stopped arriving are different things. Show both.
        if (age := device.air_sensor_age) is not None:
            attrs["마지막으로 값이 바뀐 뒤(초)"] = age
        attrs["같은 값 반복 조회"] = device.air_sensor_unchanged
        return attrs


class BoilerHotWaterRunning(BoilerEntity, BinarySensorEntity):
    """Whether hot water is being drawn right now.

    ``DHWUse`` carries the same 1=off / 2=on value the app uses for its hot-water switch.
    It turns on when a tap opens, so it can detect a shower or the washing-up.
    """

    _attr_name = "온수 사용 중"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:shower-head"

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_hot_water_running"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return None if device is None else device.hot_water_running

    @property
    def extra_state_attributes(self) -> dict[str, bool] | None:
        device = self.device
        if device is None or (sustained := device.hot_water_sustained) is None:
            return None
        return {"연속 사용": sustained}


class BoilerFaultProblem(BoilerEntity, BinarySensorEntity):
    """A non-zero ``faultStatus1`` or ``faultStatus2`` is a problem.

    What the individual bits mean is unknown. The raw values stay as attributes so they can
    be cross-checked against a user report.
    """

    _attr_name = "고장 상태"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_fault_status"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None or (status := device.fault_status) is None:
            return None
        return any(status)

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        device = self.device
        if device is None or (status := device.fault_status) is None:
            return None
        return {"faultStatus1": status[0], "faultStatus2": status[1]}


class AironeErrorProblem(AironeEntity, BinarySensorEntity):
    """Error state: on when either the room controller or the outdoor unit reports one."""

    _attr_name = "오류"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_problem"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None or device.error_code is None:
            return None
        return device.has_error

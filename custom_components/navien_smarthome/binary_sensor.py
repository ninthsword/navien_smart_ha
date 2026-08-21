"""고온경고와 실시간 연결 상태."""

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
    """설정 단계가 고온경고선을 넘었는지.

    제어를 막지 않는다 — 앱도 이 위로 설정할 수 있고, 경고 표시만 한다.
    """

    _attr_name = "고온경고"
    _attr_device_class = BinarySensorDeviceClass.HEAT
    _attr_icon = "mdi:thermometer-alert"

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_high_temp_warning"

    @property
    def available(self) -> bool:
        """냉방 중에는 판정하지 않는다. 냉방의 안전 기준값 의미가 미확인이다."""
        device = self.device
        return super().available and device is not None and not device.is_cooling

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return None if device is None else device.over_safe_value


class NavienSmartErrorProblem(NavienSmartEntity, BinarySensorEntity):
    """`errorCode` 가 0 이 아니면 문제."""

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
    """전에 오던 공기질 값이 지금 안 오는지.

    **아무 신호가 없던 것이 문제였다.** 에어모니터와 룸콘 사이 통신이 끊기면
    서버는 온도·습도만 주기 시작하고, 나머지 센서는 값이 멈춘 채 남거나
    재시작 뒤 `사용할 수 없음` 이 된다. 오류 코드도 안 오고 조회도 성공하므로
    어디에도 티가 나지 않는다.

    기기가 「센서가 없다」고 말한 적은 없다 — **우리가 전에 받아봤는데 지금은
    안 온다**는 관찰뿐이다. 그래서 기기 고장이라고 하지 않고 자료가 빠졌다고만
    알린다.
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
        # 값이 멈춘 것과 아예 안 오는 것은 다르다. 둘 다 보여준다.
        if (age := device.air_sensor_age) is not None:
            attrs["마지막으로 값이 바뀐 뒤(초)"] = age
        attrs["같은 값 반복 조회"] = device.air_sensor_unchanged
        return attrs


class BoilerHotWaterRunning(BoilerEntity, BinarySensorEntity):
    """지금 온수를 쓰고 있는지.

    ``DHWUse`` 는 앱이 온수 기능 스위치에 쓰는 것과 같은 1=끔·2=켬 값이다.
    수도를 열면 켜지므로 샤워·설거지 감지에 쓸 수 있다.
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
    """``faultStatus1`` · ``faultStatus2`` 중 하나라도 0 이 아니면 문제.

    각 비트의 뜻은 모른다. 원시값은 속성으로 남겨 제보 때 대조할 수 있게 한다.
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
    """오류 여부. 방 컨트롤러와 실외기 중 하나라도 오류면 켜진다."""

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

"""The mat's entities. They only read the connection and call its commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    PRECISION_WHOLE,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..const import DOMAIN
from . import CONF_MAT_MODEL
from .connection import LINK_STATES, SIDE_LEFT, SIDE_RIGHT, MatConnection
from .programs import PROGRAM_OPTIONS
from .protocol import (
    DEFAULT_HEAT_RANGE,
    HEAT_LEFT_ONLY,
    HEAT_RIGHT_ONLY,
    MODE_ERROR,
    MatState,
)

MODE_NAMES = {
    1: "난방",
    2: "꺼짐 예약",
    3: "수면",
    4: "살균",
    5: "오류",
    6: "꺼짐",
    7: "물빼기",
    8: "이온 케어",
    9: "맞춤 수면",
}

WATER_LEVEL_NAMES = {1: "낮음", 2: "보통", 3: "가득"}
VOLUME_OPTIONS = ["음소거", "1단계", "2단계", "3단계"]
ION_CARE_PERIOD_OPTIONS = [f"{hours}시간" for hours in range(1, 13)]
RESERVATION_CANCEL = "해제"
RESERVATION_SET = "설정됨"
SLEEP_HOURS_OPTIONS = [f"{hours}시간" for hours in range(5, 11)]

ERROR_INSPECT = "보일러를 점검해 주세요."
ERROR_CONTACT = "1588-1144로 연락해주세요."
# The wording of each named error comes from the mat's manual ("에러코드 안내").
ERROR_WATER_LOW = (
    "물 부족 에러(Er 02): 물이 부족한 경우입니다. 물 보충 안내 방법에 따라 물을 보충해주세요."
)
ERROR_TANK_SENSOR = (
    f"물탱크 공급 온도 센서 에러(Er 05): 공급 온도에 이상이 발생된 경우입니다. {ERROR_CONTACT}"
)
ERROR_WATER_LEVEL = f"수위 감지 에러(Er 11): 정상 수위 감지가 아닐 경우입니다. {ERROR_CONTACT}"
ERROR_ION_CARE = f"이온 케어 에러(Er 15): 이온 케어 장치에 이상이 발생된 경우입니다. {ERROR_CONTACT}"
ERROR_TANK_OVERHEAT = f"물탱크 과열 에러(Er 16): 물탱크의 온도가 과열되었을 경우입니다. {ERROR_CONTACT}"
ERROR_HEATING = (
    "난방 이상 에러(Er 17): 난방과 관련된 부품(온도퓨즈, 전류퓨즈)에 이상이 생긴 경우입니다. "
    f"{ERROR_CONTACT}"
)
ERROR_RETURN_SENSOR = f"환수 온도 센서 에러(Er 18): 환수 온도 센서에 이상이 발생된 경우입니다. {ERROR_CONTACT}"
ERROR_LEAK = f"누수 에러(Er 28): 매트 내부 호스 파손에 의한 누수가 감지되었을 경우입니다. {ERROR_CONTACT}"
ERROR_GENERIC = f"에러가 발생했습니다. {ERROR_INSPECT}"
ERROR_MESSAGES = {
    2: ERROR_WATER_LOW,
    302: ERROR_WATER_LOW,
    5: ERROR_TANK_SENSOR,
    205: ERROR_TANK_SENSOR,
    11: ERROR_WATER_LEVEL,
    311: ERROR_WATER_LEVEL,
    15: ERROR_ION_CARE,
    16: ERROR_TANK_OVERHEAT,
    17: ERROR_HEATING,
    18: ERROR_RETURN_SENSOR,
    19: ERROR_RETURN_SENSOR,
    218: ERROR_RETURN_SENSOR,
    219: ERROR_RETURN_SENSOR,
    28: ERROR_LEAK,
    228: ERROR_LEAK,
}


class _MatEntity(Entity):
    """Reads everything from the connection whenever it publishes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Only models with fast heat and ion care have this entity.
    _needs_extras = False

    def __init__(self, conn: MatConnection, suffix: str) -> None:
        self._conn = conn
        self._attr_unique_id = f"{conn.address.replace(':', '').lower()}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, conn.address)},
            name="숙면매트",
            manufacturer="경동나비엔",
            model=conn.entry.options.get(CONF_MAT_MODEL),
        )
        self._refresh()

    def _refresh(self) -> None:
        """Copy the connection's state into this entity's attributes."""
        status = self._conn.status
        self._attr_available = status is not None and (
            status.has_extras or not self._needs_extras
        )

    @callback
    def _async_changed(self) -> None:
        self._refresh()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._conn.async_add_listener(self._async_changed))


class MatThermostat(_MatEntity, ClimateEntity):
    """One side of the mat."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_target_temperature_step = 1
    _attr_precision = PRECISION_WHOLE

    def __init__(self, conn: MatConnection, side: str) -> None:
        self._side = side
        self._attr_name = "왼쪽" if side == SIDE_LEFT else "오른쪽"
        super().__init__(conn, f"{side}_thermostat")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        low, high = DEFAULT_HEAT_RANGE if status is None else status.heat_range
        self._attr_min_temp = low
        self._attr_max_temp = high
        if status is None:
            self._attr_hvac_mode = None
            self._attr_hvac_action = None
            return
        other_only = HEAT_RIGHT_ONLY if self._side == SIDE_LEFT else HEAT_LEFT_ONLY
        off = status.state is MatState.OFF or status.heat_type == other_only
        self._attr_hvac_mode = HVACMode.OFF if off else HVACMode.HEAT
        self._attr_hvac_action = HVACAction.OFF if off else HVACAction.HEATING

    def _current_temperature(self) -> float | None:
        status = self._conn.status
        if status is None:
            return None
        return status.left_current if self._side == SIDE_LEFT else status.right_current

    def _target_temperature(self) -> float | None:
        status = self._conn.status
        if status is None:
            return None
        target = status.left_target if self._side == SIDE_LEFT else status.right_target
        return target if status.is_valid_target(target) else None

    # Home Assistant 2026.11 serves these under native_ names and declares the old names Final
    # and deprecated; 2026.8 to 2026.10 read only the old names. A class providing both keeps
    # the native ones without a deprecation warning, hidden from type checkers as in climate.py.
    if not TYPE_CHECKING:
        _attr_temperature_unit = _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
        current_temperature = native_current_temperature = property(
            _current_temperature
        )
        target_temperature = native_target_temperature = property(_target_temperature)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self._conn.async_set_temperature(
            self._side, float(temperature), kwargs.get(ATTR_HVAC_MODE)
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._conn.async_set_hvac_mode(self._side, hvac_mode)


class MatModeSensor(_MatEntity, SensorEntity):
    _attr_name = "운전 모드"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        MODE_NAMES[1], MODE_NAMES[2], "켜짐 예약",
        *[MODE_NAMES[mode] for mode in range(3, 10)],
    ]

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "mode")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        if status is None:
            self._attr_native_value = None
        elif status.on_reservation_active:
            self._attr_native_value = "켜짐 예약"
        else:
            self._attr_native_value = MODE_NAMES.get(status.mode)


class MatReservationTimeSensor(_MatEntity, SensorEntity):
    _attr_name = "예약 실행 시각"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "reservation_time")

    def _refresh(self) -> None:
        super()._refresh()
        reservation = self._conn.reservation
        self._attr_native_value = (
            None if self._conn.status is None or reservation is None else reservation.run_time
        )


class MatLinkSensor(_MatEntity, SensorEntity):
    """Stays available while the link is down: that is what it reports."""

    _attr_name = "블루투스 상태"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = LINK_STATES

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "link")

    def _refresh(self) -> None:
        self._attr_available = True
        self._attr_native_value = self._conn.link_state


class MatErrorSensor(_MatEntity, BinarySensorEntity):
    _attr_name = "오류"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "error")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_is_on = None if status is None else status.mode == MODE_ERROR


class MatWaterLevelSensor(_MatEntity, SensorEntity):
    _attr_name = "수위"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(WATER_LEVEL_NAMES.values())

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "water_level")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        level = None if status is None else status.water_level
        self._attr_native_value = None if level is None else WATER_LEVEL_NAMES[level]


class MatErrorDetailSensor(_MatEntity, SensorEntity):
    _attr_name = "오류 내용"

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "error_detail")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        code = 0 if status is None else status.error_code
        self._attr_native_value = (
            f"{code}: {ERROR_MESSAGES.get(code, ERROR_GENERIC)}" if code else None
        )


class MatFastHeatMinutesSensor(_MatEntity, SensorEntity):
    _attr_name = "빠른 난방 남은 시간"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _needs_extras = True

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "fast_heat_minutes")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_native_value = None if status is None else status.fast_heat_minutes


class MatIonCareRunningSensor(_MatEntity, BinarySensorEntity):
    _attr_name = "이온케어 실행 중"
    _needs_extras = True

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "ion_care_running")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_is_on = None if status is None else status.ion_care_running


class MatEnableSwitch(_MatEntity, SwitchEntity):
    """Off drops the link so the phone app can reach the mat."""

    _attr_name = "블루투스 사용"

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "enabled")

    def _refresh(self) -> None:
        self._attr_available = True
        self._attr_is_on = self._conn.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._conn.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._conn.async_set_enabled(False)


class MatPowerSwitch(_MatEntity, SwitchEntity):
    """The whole mat's power; on returns to the last state."""

    _attr_name = "전원"

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "power")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_is_on = None if status is None else status.state is not MatState.OFF

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._conn.async_set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._conn.async_set_power(False)


class MatLockSwitch(_MatEntity, SwitchEntity):
    _attr_name = "조작 잠금"

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "lock")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_is_on = None if status is None else status.lock

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._conn.async_set_lock(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._conn.async_set_lock(False)


class MatFastHeatSwitch(_MatEntity, SwitchEntity):
    _attr_name = "빠른 난방"
    _needs_extras = True

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "fast_heat")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_is_on = None if status is None else status.fast_heat_running

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._conn.async_set_fast_heat(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._conn.async_set_fast_heat(False)


class MatSleepSwitch(_MatEntity, SwitchEntity):
    _attr_name = "수면 모드"

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "sleep")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_is_on = None if status is None else status.sleep_running

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._conn.async_set_sleep(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._conn.async_set_sleep(False)


class MatCustomSleepSwitch(_MatEntity, SwitchEntity):
    _attr_name = "맞춤 수면"
    _needs_extras = True

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "custom_sleep")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_is_on = None if status is None else status.custom_sleep_running

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._conn.async_set_custom_sleep(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._conn.async_set_custom_sleep(False)


class MatCustomProgramSelect(_MatEntity, SelectEntity):
    """Choose a stored program without sending anything to the mat."""

    _attr_name = "맞춤 수면 프로그램"
    _attr_options = PROGRAM_OPTIONS
    _needs_extras = True

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "custom_program")

    def _refresh(self) -> None:
        super()._refresh()
        self._attr_current_option = PROGRAM_OPTIONS[self._conn.custom_program - 1]

    async def async_select_option(self, option: str) -> None:
        await self._conn.async_select_custom_program(PROGRAM_OPTIONS.index(option) + 1)


class MatReservationSelect(_MatEntity, SelectEntity):
    """Whole-mat reservations; only their active state confirms a changed value."""

    def __init__(self, conn: MatConnection, on: bool) -> None:
        self._on = on
        self._attr_name = "켜짐 예약" if on else "꺼짐 예약"
        super().__init__(conn, "on_reservation" if on else "off_reservation")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        hours_range = (
            range(1, 15)
            if not self._on and status is not None and status.model_code == 3
            else range(3, 13)
        )
        self._attr_options = [RESERVATION_CANCEL, *[f"{h}시간" for h in hours_range]]
        if status is None:
            self._attr_current_option = None
            return
        active = status.on_reservation_active if self._on else status.off_reservation_active
        if not active:
            self._attr_current_option = RESERVATION_CANCEL
            return
        reservation = self._conn.reservation
        hours = None if reservation is None else reservation.set_hours
        if hours in hours_range:
            self._attr_current_option = f"{hours}시간"
        else:
            self._attr_options.append(RESERVATION_SET)
            self._attr_current_option = RESERVATION_SET

    async def async_select_option(self, option: str) -> None:
        if option == RESERVATION_SET:
            return
        hours = None if option == RESERVATION_CANCEL else int(option.removesuffix("시간"))
        if self._on:
            await self._conn.async_set_on_reservation(hours)
        else:
            await self._conn.async_set_off_reservation(hours)


class MatSleepHoursSelect(_MatEntity, SelectEntity):
    """A stored setting; changing it does not start or update sleep on the mat."""

    _attr_options = SLEEP_HOURS_OPTIONS

    def __init__(self, conn: MatConnection, side: str) -> None:
        self._side = side
        self._attr_name = "왼쪽 수면 시간" if side == SIDE_LEFT else "오른쪽 수면 시간"
        super().__init__(conn, f"{side}_sleep_hours")

    def _refresh(self) -> None:
        super()._refresh()
        hours = (
            self._conn.left_sleep_hours
            if self._side == SIDE_LEFT
            else self._conn.right_sleep_hours
        )
        self._attr_current_option = f"{hours}시간"

    async def async_select_option(self, option: str) -> None:
        await self._conn.async_set_sleep_hours(
            self._side, SLEEP_HOURS_OPTIONS.index(option) + 5
        )


class MatOnReservationTemperature(_MatEntity, NumberEntity):
    _attr_name = "켜짐 예약 온도"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "on_reservation_temperature")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        low, high = DEFAULT_HEAT_RANGE if status is None else status.heat_range
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_value = self._conn.on_reservation_temperature

    async def async_set_native_value(self, value: float) -> None:
        await self._conn.async_set_on_reservation_temperature(value)


class MatVolumeSelect(_MatEntity, SelectEntity):
    _attr_name = "조작음"
    _attr_options = VOLUME_OPTIONS

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "volume")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_current_option = (
            None if status is None else VOLUME_OPTIONS[status.volume]
        )

    async def async_select_option(self, option: str) -> None:
        await self._conn.async_set_volume(VOLUME_OPTIONS.index(option))


class MatIonCarePeriodSelect(_MatEntity, SelectEntity):
    _attr_name = "이온케어 주기"
    _attr_options = ION_CARE_PERIOD_OPTIONS
    _needs_extras = True

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "ion_care_period")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        period = 0 if status is None else status.ion_care_period
        self._attr_current_option = (
            ION_CARE_PERIOD_OPTIONS[period - 1]
            if 1 <= period <= len(ION_CARE_PERIOD_OPTIONS)
            else None
        )

    async def async_select_option(self, option: str) -> None:
        await self._conn.async_set_ion_care_period(
            ION_CARE_PERIOD_OPTIONS.index(option) + 1
        )


class MatIonCareButton(_MatEntity, ButtonEntity):
    _attr_name = "이온케어 실행"
    _needs_extras = True

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "ion_care_run")

    async def async_press(self) -> None:
        await self._conn.async_run_ion_care()


class MatMatchTemperatureButton(_MatEntity, ButtonEntity):
    """Sets both sides to this side's target."""

    def __init__(self, conn: MatConnection, side: str) -> None:
        self._side = side
        self._attr_name = (
            "왼쪽 온도로 맞추기" if side == SIDE_LEFT else "오른쪽 온도로 맞추기"
        )
        super().__init__(conn, f"{side}_match_temperature")

    async def async_press(self) -> None:
        await self._conn.async_match_temperature(self._side)


async def async_setup_mat_climate(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    conn: MatConnection = entry.runtime_data
    async_add_entities(
        [MatThermostat(conn, SIDE_LEFT), MatThermostat(conn, SIDE_RIGHT)]
    )


async def async_setup_mat_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    conn: MatConnection = entry.runtime_data
    async_add_entities(
        [
            MatModeSensor(conn),
            MatReservationTimeSensor(conn),
            MatLinkSensor(conn),
            MatWaterLevelSensor(conn),
            MatErrorDetailSensor(conn),
            MatFastHeatMinutesSensor(conn),
        ]
    )


async def async_setup_mat_binary_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    conn: MatConnection = entry.runtime_data
    async_add_entities([MatErrorSensor(conn), MatIonCareRunningSensor(conn)])


async def async_setup_mat_switches(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    conn: MatConnection = entry.runtime_data
    async_add_entities(
        [
            MatEnableSwitch(conn),
            MatPowerSwitch(conn),
            MatLockSwitch(conn),
            MatFastHeatSwitch(conn),
            MatSleepSwitch(conn),
            MatCustomSleepSwitch(conn),
        ]
    )


async def async_setup_mat_selects(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    conn: MatConnection = entry.runtime_data
    async_add_entities(
        [
            MatVolumeSelect(conn),
            MatIonCarePeriodSelect(conn),
            MatReservationSelect(conn, False),
            MatReservationSelect(conn, True),
            MatSleepHoursSelect(conn, SIDE_LEFT),
            MatSleepHoursSelect(conn, SIDE_RIGHT),
            MatCustomProgramSelect(conn),
        ]
    )


async def async_setup_mat_numbers(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    conn: MatConnection = entry.runtime_data
    async_add_entities([MatOnReservationTemperature(conn)])


async def async_setup_mat_buttons(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    conn: MatConnection = entry.runtime_data
    async_add_entities(
        [
            MatIonCareButton(conn),
            MatMatchTemperatureButton(conn, SIDE_LEFT),
            MatMatchTemperatureButton(conn, SIDE_RIGHT),
        ]
    )

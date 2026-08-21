"""운전 상태와 오류 코드."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from . import NavienSmartConfigEntry
from .airone import AironeDevice, as_number, level_text, text_or_none
from .boiler import (
    BOILER_OPERATION_MODE_NAMES,
    BOILER_STATE_HEATING,
    BOILER_STATE_IDLE,
    BOILER_STATE_OFF,
    BoilerDevice,
)
from .const import (
    AIRONE_INFERRED_UNITS,
    AIRONE_SENSOR_KINDS,
    LEGACY_EXTRA_FIELDS,
    LEGACY_VALUE_TABLES,
)
from .coordinator import NavienSmartCoordinator
from .entity import AironeEntity, AironeMonitorEntity, BoilerEntity, NavienSmartEntity
from .models import NavienDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for device in (coordinator.data or {}).values():
        entities.append(NavienSmartModeSensor(coordinator, device))
        entities.append(NavienSmartErrorSensor(coordinator, device))

    for airone in coordinator.airone.values():
        entities.append(AironeStateSensor(coordinator, airone))
        entities.append(AironeErrorSensor(coordinator, airone))
        # 구세대만 싣는 값. **상태로 세지 않는다** — 엔티티는 MQTT 가 붙기 전에
        # 만들어지므로 그때 세면 하나도 안 생긴다 (필터와 같은 이유).
        if not airone.is_v2_generation:
            for key, label, table in LEGACY_EXTRA_FIELDS.values():
                entities.append(AironeLegacySensor(coordinator, airone, key, label, table))
        # 공기질은 서버가 실제로 값을 준 항목만 만든다. 목록을 미리 정하지 않는다.
        #
        # 에어모니터가 등록돼 있으면 **그 기기 카드에** 붙인다. 앱에서도 별도
        # 부속이고, 본체 카드에 다 몰아넣으면 목록이 길어져 읽기 어렵다.
        monitor = airone.air_monitors[0] if airone.air_monitors else None
        for kind in airone.sensor_kinds:
            if monitor is not None:
                entities.append(AironeMonitorSensor(coordinator, airone, monitor, kind))
            else:
                entities.append(AironeAirSensor(coordinator, airone, kind))
        # 개수는 메타데이터에서 온다. 잔량은 상태에서 오지만, 엔티티는 MQTT 가
        # 붙기 전에 만들어지므로 상태로 세면 하나도 안 생긴다.
        entities.extend(
            AironeFilterSensor(coordinator, airone, index)
            for index in range(len(airone.filter_types))
        )

    for boiler in coordinator.boilers.values():
        entities.append(BoilerOperatingStateSensor(coordinator, boiler))
        entities.append(BoilerOperatingModeSensor(coordinator, boiler))
        entities.extend(
            BoilerTemperatureSensor(coordinator, boiler, key, label, measured)
            for key, label, measured in (
                ("indoor_temperature", "실내 온도", True),
                ("supply_temperature", "난방수 공급 온도", True),
                ("return_temperature", "난방수 환수 온도", True),
                ("hot_water_temperature", "온수 온도", True),
                ("ondol_target_temperature", "온돌 설정 온도", False),
                ("hot_water_target_temperature", "온수 설정 온도", False),
                ("outside_temperature", "외기 온도", True),
            )
        )
        entities.extend(
            BoilerFlowRateSensor(coordinator, boiler, key, label)
            for key, label in (
                ("hot_water_flow_rate", "온수 유량"),
                ("heating_flow_rate", "난방 유량"),
            )
        )
        entities.append(BoilerWifiSignalSensor(coordinator, boiler))
        entities.append(BoilerHeatingIntensitySensor(coordinator, boiler))
        entities.append(BoilerReservationSensor(coordinator, boiler))
        entities.append(BoilerHumiditySensor(coordinator, boiler))
        entities.append(BoilerModeSensor(coordinator, boiler))
        entities.append(BoilerErrorSensor(coordinator, boiler))
        if boiler.supports_feature("gasUsageUse"):
            entities.append(BoilerMonthlyGasSensor(coordinator, boiler))
            entities.append(BoilerDailyGasSensor(coordinator, boiler))

    async_add_entities(entities)


class BoilerOperatingStateSensor(BoilerEntity, SensorEntity):
    """앱 표시 로직으로 확인한 보일러 전원·히팅 상태."""

    _attr_name = "운전 상태"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [BOILER_STATE_OFF, BOILER_STATE_IDLE, BOILER_STATE_HEATING]

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_operating_state"

    @property
    def native_value(self) -> str | None:
        device = self.device
        return None if device is None else device.operating_state

    @property
    def icon(self) -> str:
        if self.native_value == BOILER_STATE_OFF:
            return "mdi:radiator-off"
        if self.native_value == BOILER_STATE_HEATING:
            return "mdi:radiator"
        return "mdi:radiator-disabled"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        return {
            "operation_mode": device.operation_mode,
            "operation_busy": device.operation_busy,
        }


class BoilerOperatingModeSensor(BoilerEntity, SensorEntity):
    """NR-67D 앱과 설명서에서 확인한 선택 운전 모드."""

    _attr_name = "운전 모드"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(BOILER_OPERATION_MODE_NAMES.values())
    _attr_icon = "mdi:radiator"

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_operating_mode"

    @property
    def native_value(self) -> str | None:
        device = self.device
        return None if device is None else device.operation_mode_name

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        return {"operation_mode": device.operation_mode}


class BoilerTemperatureSensor(BoilerEntity, SensorEntity):
    """실측으로 배율이 확인된 보일러 온도 하나."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: BoilerDevice,
        key: str,
        label: str,
        measured: bool,
    ) -> None:
        super().__init__(coordinator, device)
        self._key = key
        self._attr_name = label
        self._attr_unique_id = f"{device.device_id}_{key}"
        if measured:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        device = self.device
        return None if device is None else getattr(device, self._key)


class BoilerFlowRateSensor(BoilerEntity, SensorEntity):
    """난방·온수 유량. 단위는 확인하지 못해 분당 리터로만 표시한다."""

    _attr_native_unit_of_measurement = "L/min"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:waves-arrow-right"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: BoilerDevice,
        key: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._key = key
        self._attr_name = label
        self._attr_unique_id = f"{device.device_id}_{key}"

    @property
    def native_value(self) -> float | None:
        device = self.device
        return None if device is None else getattr(device, self._key)


class BoilerWifiSignalSensor(BoilerEntity, SensorEntity):
    """룸콘 Wi-Fi 신호. **단위를 확인하지 못해 숫자만 남긴다.**"""

    _attr_name = "Wi-Fi 신호"
    _attr_icon = "mdi:wifi"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_wifi_rssi"

    @property
    def native_value(self) -> int | None:
        device = self.device
        return None if device is None else device.wifi_rssi


class BoilerHeatingIntensitySensor(BoilerEntity, SensorEntity):
    """난방 강도 설정. 원시 단계 값만 보여주고 이름을 붙이지 않는다.

    앱과 설명서에서 단계 이름을 확인하지 못했고, 서버가 준 범위도 최소가 최대보다
    커서(min 3 · max 1) 방향을 정할 수 없다. 뜻이 확인되면 그때 이름을 붙인다.
    """

    _attr_name = "난방 강도"
    _attr_icon = "mdi:fire"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_heating_intensity"

    @property
    def native_value(self) -> int | None:
        device = self.device
        return None if device is None else device.heating_intensity


class BoilerReservationSensor(BoilerEntity, SensorEntity):
    """예약 설정을 읽기만 한다.

    **바꾸지 않는다.** 예약은 시간표 전체를 한 번에 덮는 별도 프로토콜이라,
    잘못 보내면 실제 예약을 지운다. 지금 무엇이 잡혀 있는지 보는 것까지가
    안전하게 할 수 있는 일이다.
    """

    _attr_name = "예약"
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_reservation"

    @property
    def native_value(self) -> str | None:
        device = self.device
        if device is None:
            return None
        enabled = [
            label
            for label, key in (
                ("주간", "programReservationUse"),
                ("빠른온수", "fastDHWReservationUse"),
            )
            if device.reservation_enabled(key)
        ]
        return ", ".join(enabled) if enabled else "없음"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        attrs: dict[str, Any] = {}
        if (interval := device.repeat_reservation_interval) is not None:
            attrs["반복 주기"] = f"{interval[0]}시간 {interval[1]}분"
        if (table := device.day_cycle_reservation) is not None:
            # 각 자리의 뜻을 모르므로 해석하지 않고 원문 그대로 남긴다.
            attrs["24시간 예약 원문"] = table
        return attrs or None


class BoilerHumiditySensor(BoilerEntity, SensorEntity):
    """룸콘이 보고한 실내 습도."""

    _attr_name = "실내 습도"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_indoor_humidity"

    @property
    def native_value(self) -> float | None:
        device = self.device
        return None if device is None else device.indoor_humidity


class BoilerModeSensor(BoilerEntity, SensorEntity):
    """뜻을 추측하지 않은 운전 모드 코드."""

    _attr_name = "운전 모드 코드"
    _attr_icon = "mdi:radiator"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_operation_mode"

    @property
    def native_value(self) -> int | None:
        device = self.device
        return None if device is None else device.operation_mode

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        # 의미가 검증되지 않은 값은 자동화하기 쉬운 별도 엔티티로 만들지 않는다.
        return {
            key: device.status.get(key)
            for key in (
                "operationBusy",
                "DHWUse",
                "DHWUseSustained",
                "fastDHWUse",
                "DHWBoost",
            )
            if key in device.status
        }


class BoilerErrorSensor(BoilerEntity, SensorEntity):
    """보일러 주 오류 코드. 0이면 장치가 정상으로 보고한 것이다."""

    _attr_name = "오류 코드"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_error_code"

    @property
    def native_value(self) -> int | None:
        device = self.device
        return None if device is None else device.error_code

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        attrs: dict[str, Any] = {}
        if device.sub_error_code is not None:
            attrs["sub_error_code"] = device.sub_error_code
        # 설명서와 같은 표기와 이름. 표에 없는 번호면 넣지 않는다.
        if (label := device.error_label) is not None:
            attrs["에러코드"] = label
        if (name := device.error_name) is not None:
            attrs["이상 발생 내용"] = name
        return attrs or None


class BoilerMonthlyGasSensor(BoilerEntity, SensorEntity):
    """앱 가스 사용량 화면의 이번 달 보일러 누적 사용량."""

    _attr_name = "이번 달 가스 사용량"
    _attr_icon = "mdi:meter-gas"
    _attr_device_class = SensorDeviceClass.GAS
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    # 월이 바뀌면 0으로 돌아가는 누적값이라 TOTAL_INCREASING이 아니다.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_monthly_gas_usage"

    @property
    def native_value(self) -> float | None:
        device = self.device
        return None if device is None else device.gas_total_month

    @property
    def last_reset(self) -> datetime | None:
        """이번 주기가 시작한 시각 — 이 달 1일 자정.

        ``TOTAL`` 은 **``last_reset`` 이 바뀔 때만** 주기가 끝난 것으로 본다
        (HA `sensor/recorder.py`). 이 값을 내보내지 않으면 월말에 값이 0 으로
        돌아갈 때 리셋이 아니라 감소로 읽혀 장기 통계 누적에서 그 달치가 통째로
        빠진다.
        """
        device = self.device
        if device is None or (start := device.gas_month_start) is None:
            return None
        return dt_util.start_of_local_day(start)

    @property
    def extra_state_attributes(self) -> dict[str, float] | None:
        device = self.device
        if device is None:
            return None
        values = {
            "난방": device.gas_heating_month,
            "온수": device.gas_hot_water_month,
        }
        return {key: value for key, value in values.items() if value is not None} or None


class BoilerDailyGasSensor(BoilerEntity, SensorEntity):
    """Home Assistant 현지 날짜에 해당하는 오늘의 보일러 가스 사용량."""

    _attr_name = "오늘 가스 사용량"
    _attr_icon = "mdi:meter-gas-outline"
    _attr_device_class = SensorDeviceClass.GAS
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_daily_gas_usage"

    def _today_values(
        self,
    ) -> tuple[float | None, float | None, float | None] | None:
        device = self.device
        return None if device is None else device.gas_day(dt_util.now().date())

    @property
    def native_value(self) -> float | None:
        values = self._today_values()
        return None if values is None else values[0]

    @property
    def last_reset(self) -> datetime | None:
        """오늘 자정. 월간 센서와 같은 이유로 반드시 내보내야 한다."""
        return dt_util.start_of_local_day()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        values = self._today_values()
        if values is None:
            return None
        attrs: dict[str, Any] = {"기준일": dt_util.now().date().isoformat()}
        if values[1] is not None:
            attrs["난방"] = values[1]
        if values[2] is not None:
            attrs["온수"] = values[2]
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # 네트워크 조회와 별개로 자정에 날짜 선택을 바꾼다. 다음 시간별 가스
        # 응답이 올 때까지 다른 달의 오래된 값을 오늘 값으로 표시하지 않는다.
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._async_midnight_update, hour=0, minute=0, second=0
            )
        )

    @callback
    def _async_midnight_update(self, _now: Any) -> None:
        self.async_write_ha_state()


class NavienSmartModeSensor(NavienSmartEntity, SensorEntity):
    """`operationMode` 를 사람이 읽는 이름으로."""

    _attr_name = "운전상태"
    _attr_icon = "mdi:bed"

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_mode"
        # `options` 를 쓰지 않는다. `device_class = ENUM` 이 함께 필요하고, ENUM 은
        # 값이 반드시 목록 안에 있어야 한다. `mode_name` 은 모르는 모드에
        # `알 수 없음(N)` 을 돌려주므로 목록을 닫을 수 없다 —
        # 나비엔이 새 모드를 추가하면 그때부터 이 센서가 예외로 죽는다.

    @property
    def native_value(self) -> str | None:
        device = self.device
        return None if device is None else device.mode_name

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        attrs: dict[str, Any] = {
            "operation_mode": device.operation_mode,
            "model_code": device.model_code,
            "model_type": device.model_type,
        }
        if device.heat_control is not None:
            attrs["control_unit"] = device.heat_control.unit
        if device.is_four_season:
            # 사계절 냉방은 값 체계가 확인되지 않았다. 제보용으로 그대로 노출한다.
            attrs["four_season"] = True
            attrs["season"] = device.season
            attrs["season_name"] = device.season_name
            attrs["cooling"] = device.is_cooling
            if device.cool_control is not None:
                attrs["cool_control"] = device.cool_control.as_diagnostics()
        if device.has_sleep_mode and device.sleep_durations:
            # 분 단위다. 3~12시간, 30분 간격.
            attrs["sleep_durations_minutes"] = device.sleep_durations
        if device.schedule_kinds:
            attrs["schedule_kinds"] = list(device.schedule_kinds)
        return attrs


class NavienSmartErrorSensor(NavienSmartEntity, SensorEntity):
    """`errorCode`. 0 이면 정상이다."""

    _attr_name = "오류 코드"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_error_code"

    @property
    def native_value(self) -> int | None:
        device = self.device
        return None if device is None else device.error_code

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """오류 이름을 속성으로 붙인다.

        **상태값은 숫자 그대로 둔다.** 글자로 바꾸면 이 값을 쓰던 자동화가 깨진다.
        이름은 제보자가 기기 설명서에서 옮겨 준 표이고 온도형에만 붙는다.
        """
        device = self.device
        if device is None:
            return None
        text = device.error_text
        return None if text is None else {"error_text": text}


class AironeStateSensor(AironeEntity, SensorEntity):
    """`running` 을 사람이 읽는 이름으로. 운전 / 정지 / 외출."""

    _attr_name = "운전상태"
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_state"

    @property
    def native_value(self) -> str | None:
        device = self.device
        return None if device is None else device.running_name

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        attrs: dict[str, Any] = {
            "running": device.running,
            "mode": device.mode,
            "option": device.option,
            "air_volume": device.air_volume,
            "model_code": device.model_code,
            # 실기기 미검증이라는 사실을 상태에도 남긴다. 자동화를 만들기 전에
            # 사용자가 알 수 있어야 한다.
            "verified_on_hardware": False,
        }
        if device.odu_model_code:
            attrs["odu_model_code"] = device.odu_model_code
        if device.target_humidity is not None:
            attrs["target_humidity"] = device.target_humidity
        # 자동건조 중일 때만 있다. 상태 문구는 「자동건조」로 두고 숫자는 여기 둔다 —
        # 상태에 섞으면 문자열을 비교하는 자동화가 매번 깨진다.
        if device.auto_dry_percent is not None:
            attrs["auto_dry_percent"] = device.auto_dry_percent
        return attrs


class AironeErrorSensor(AironeEntity, SensorEntity):
    """오류 코드. 0 이면 정상이다."""

    _attr_name = "오류 코드"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_error_code"

    @property
    def native_value(self) -> int | None:
        device = self.device
        return None if device is None else device.error_code


class _AirSensorMixin:
    """공기질 항목 하나. 본체에 붙일 때와 에어모니터에 붙일 때가 같다."""

    _kind: str
    _numeric: bool

    def _setup_air(self, device: AironeDevice, kind: str, unique_prefix: str) -> None:
        self._kind = kind
        self._attr_unique_id = f"{unique_prefix}_air_{kind}"
        label, unit, device_class = AIRONE_SENSOR_KINDS[kind]
        self._attr_name = label

        # 숫자인지 **첫 값을 보고** 정한다. 종류만 보고 정하면 틀린다 — 앱이
        # tvoc·radon·종합을 등급으로 표시하길래 값이 없다고 봤는데, 실사용
        # 제보로 숫자가 온다는 것이 확인됐다.
        #
        # 엔티티가 만들어질 땐 첫 조회가 끝나 있어서 값이 이미 있다.
        raw = device.air_sensors.get(kind) or {}
        self._numeric = as_number(raw.get("value")) is not None
        if self._numeric:
            self._attr_state_class = "measurement"
            if unit is not None:
                self._attr_native_unit_of_measurement = unit
            # HA 가 아이콘·히스토리 그래프·단위 변환에 쓴다. 단위가 맞아야
            # 붙일 수 있으므로 숫자일 때만 붙인다.
            if device_class is not None:
                self._attr_device_class = device_class

    @property
    def _raw(self) -> dict[str, Any] | None:
        device = self.device  # type: ignore[attr-defined]
        if device is None:
            return None
        return device.air_sensors.get(self._kind)

    @property
    def native_value(self) -> float | str | None:
        raw = self._raw
        if raw is None:
            return None
        if not self._numeric:
            # 등급을 못 읽으면 값이라도 문자열로 낸다.
            return level_text(raw) or text_or_none(raw.get("value"))
        # 숫자로 시작했는데 문자열이 오면 상태를 비운다. 섞어 내면
        # `state_class` 가 붙은 센서가 예외로 죽는다.
        return as_number(raw.get("value"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        raw = self._raw
        if raw is None:
            return None
        attrs: dict[str, Any] = {"kind": self._kind}
        if (level := level_text(raw)) is not None:
            # 숫자 센서에도 등급을 남긴다 — 앱이 보여주는 것이 이쪽이다.
            attrs["grade"] = level
        if self._kind in AIRONE_INFERRED_UNITS:
            # 앱에서 뽑은 단위가 아니라 판단으로 정한 것이다. 밖에서 볼 수 있게 남긴다.
            attrs["unit_inferred"] = True
        return attrs


class AironeAirSensor(_AirSensorMixin, AironeEntity, SensorEntity):
    """공기질 항목. 에어모니터가 없으면 본체 기기에 붙는다."""

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: AironeDevice,
        kind: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._setup_air(device, kind, device.device_id)


class AironeMonitorSensor(_AirSensorMixin, AironeMonitorEntity, SensorEntity):
    """공기질 항목. 에어모니터 기기에 붙는다."""

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: AironeDevice,
        monitor: dict[str, Any],
        kind: str,
    ) -> None:
        super().__init__(coordinator, device, monitor)
        self._setup_air(device, kind, self._monitor_id)


class AironeLegacySensor(AironeEntity, SensorEntity):
    """구세대 상태 프레임이 더 싣는 값.

    뜻이 확인된 것만 이름을 붙였고, 값은 해석하지 않고 그대로 보여준다 —
    1/2 플래그 체계가 확인되지 않아 켜짐·꺼짐으로 옮기지 않는다.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:code-braces"

    def __init__(self, coordinator, device, key: str, label: str, table: str | None) -> None:
        super().__init__(coordinator, device)
        self._key = key
        self._table = LEGACY_VALUE_TABLES.get(table or "")
        self._attr_unique_id = f"{device.device_id}_legacy_{key}"
        self._attr_name = label
        # 필터 사용 시간의 단위를 확인하지 못했다. 시간인지 분인지 모르는 채로
        # 「h」 를 붙이면 60배 틀린 값이 그럴듯하게 보인다. 숫자만 둔다.

    @property
    def native_value(self):
        value = self.device.legacy_extras.get(self._key)
        if self._table is None or value is None:
            return value
        # 표에 없는 값은 숫자를 그대로 보여준다 — 모르는 값을 숨기지 않는다.
        return self._table.get(value, value)


class AironeFilterSensor(AironeEntity, SensorEntity):
    """필터 **잔량**(%). 실외기가 알려준 필터마다 하나씩.

    **필드 이름과 뜻이 반대다.** 값은 `odu.filter[i].usage.percent` 에서 오고
    이름만 보면 「쓴 만큼」 같지만 남은 수명이다 — 87 이면 87% 남았고 13% 썼다.
    실기기에서 나비엔 앱 표시와 대조해 확인했다.
    """

    _attr_icon = "mdi:air-filter"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = "measurement"

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: AironeDevice,
        index: int,
    ) -> None:
        super().__init__(coordinator, device)
        self._index = index
        self._attr_unique_id = f"{device.device_id}_filter_{index}"
        # 필터 `type` 의 뜻이 확인되지 않았다. 이름에 종류를 적지 않고 번호만 쓴다.
        count = len(device.filter_types)
        self._attr_name = "필터 잔량" if count == 1 else f"필터 {index + 1} 잔량"

    @property
    def _raw(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        filters = device.filters
        return filters[self._index] if self._index < len(filters) else None

    @property
    def native_value(self) -> int | None:
        raw = self._raw
        return None if raw is None else raw.get("percent")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        raw = self._raw
        if raw is None:
            return None
        return {
            "filter_type": raw.get("type"),
            "replace_period": raw.get("replace_period"),
        }

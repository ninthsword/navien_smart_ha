"""Operating state and error codes."""

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
        # Values only the older generation carries. **Do not count them from state**: the
        # entities are created before MQTT connects, so counting then produces none at all
        # (the same reason as for filters).
        if not airone.is_v2_generation:
            for key, label, table in LEGACY_EXTRA_FIELDS.values():
                entities.append(AironeLegacySensor(coordinator, airone, key, label, table))
        # Air-quality entities are created only for items the server actually sent values
        # for. The list is never fixed in advance.
        #
        # With an air monitor registered, they attach to **its** device card. The app treats
        # it as a separate accessory too, and piling everything onto the main card makes the
        # list long and hard to read.
        monitor = airone.air_monitors[0] if airone.air_monitors else None
        for kind in airone.entity_sensor_kinds:
            if monitor is not None:
                entities.append(AironeMonitorSensor(coordinator, airone, monitor, kind))
            else:
                entities.append(AironeAirSensor(coordinator, airone, kind))
        # The count comes from metadata. The remaining life comes from state, but the
        # entities are created before MQTT connects, so counting from state produces none.
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
    """Boiler power and heating state, confirmed from the app's display logic."""

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
    """The selected operating mode, confirmed from the NR-67D app and manual."""

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
    """One boiler temperature whose scale factor was confirmed on a real device."""

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
    """Heating and hot-water flow. The unit was never confirmed, so it is shown only as L/min."""

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
    """Room-controller Wi-Fi signal. **The unit was never confirmed, so only the number is kept.**"""

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
    """The heating-intensity setting, shown as a raw step with no label.

    Neither the app nor the manual names the steps, and the range the server sends has a
    minimum above its maximum (min 3, max 1), so even the direction cannot be settled. Names
    can be added once the meaning is confirmed.
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
    """Read the schedule setting, and only read it.

    **Never write it.** A schedule is a separate protocol that overwrites the whole timetable
    at once, so a wrong command erases the user's real schedule. Showing what is currently
    set is as far as this can safely go.
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
            # What each position means is unknown, so the raw table is kept uninterpreted.
            attrs["24시간 예약 원문"] = table
        return attrs or None


class BoilerHumiditySensor(BoilerEntity, SensorEntity):
    """Indoor humidity as reported by the room controller."""

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
    """The operating-mode code, with no guess at what it means."""

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
        # Values whose meaning is unverified do not get their own easily automated entity.
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
    """The boiler's primary error code. Zero means the device reported itself healthy."""

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
        # The same notation and names as the manual. A number absent from the table is omitted.
        if (label := device.error_label) is not None:
            attrs["에러코드"] = label
        if (name := device.error_name) is not None:
            attrs["이상 발생 내용"] = name
        return attrs or None


class BoilerMonthlyGasSensor(BoilerEntity, SensorEntity):
    """This month's cumulative boiler gas usage, as shown on the app's gas-usage screen."""

    _attr_name = "이번 달 가스 사용량"
    _attr_icon = "mdi:meter-gas"
    _attr_device_class = SensorDeviceClass.GAS
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    # The total resets to zero when the month rolls over, so this is not TOTAL_INCREASING.
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
        """When the current cycle began — midnight on the first of this month.

        ``TOTAL`` treats a cycle as finished **only when ``last_reset`` changes** (HA's
        `sensor/recorder.py`). Without exporting this, the drop to zero at the end of a month
        reads as a decrease rather than a reset, and that month falls out of the long-term
        statistics total entirely.
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
    """Today's boiler gas usage, for the local date Home Assistant is on."""

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
        """Midnight today. This has to be exported for the same reason as the monthly sensor."""
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
        # The date selection rolls at midnight independently of any network query, so a stale
        # value from another month is never shown as today until the next hourly gas response
        # arrives.
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._async_midnight_update, hour=0, minute=0, second=0
            )
        )

    @callback
    def _async_midnight_update(self, _now: Any) -> None:
        self.async_write_ha_state()


class NavienSmartModeSensor(NavienSmartEntity, SensorEntity):
    """`operationMode` rendered as a human-readable name."""

    _attr_name = "운전상태"
    _attr_icon = "mdi:bed"

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_mode"
        # `options` is not used. It requires `device_class = ENUM`, and ENUM demands that the
        # value always be in the list. `mode_name` returns "알 수 없음(N)" for an unknown mode,
        # so the list cannot be closed — the moment Navien adds a mode, this sensor would start
        # dying with an exception.

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
            # The four-season cooling value scheme is unconfirmed. It is exposed as-is so it
            # can be reported back.
            attrs["four_season"] = True
            attrs["season"] = device.season
            attrs["season_name"] = device.season_name
            attrs["cooling"] = device.is_cooling
            if device.cool_control is not None:
                attrs["cool_control"] = device.cool_control.as_diagnostics()
        if device.has_sleep_mode and device.sleep_durations:
            # In minutes: 3 to 12 hours, in 30-minute increments.
            attrs["sleep_durations_minutes"] = device.sleep_durations
        if device.schedule_kinds:
            attrs["schedule_kinds"] = list(device.schedule_kinds)
        return attrs


class NavienSmartErrorSensor(NavienSmartEntity, SensorEntity):
    """`errorCode`. Zero means healthy."""

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
        """Attach the error name as an attribute.

        **The state stays a number.** Turning it into text would break automations already
        using the value. The names come from a table a reporter transcribed from the device
        manual, and apply only to temperature mats.
        """
        device = self.device
        if device is None:
            return None
        text = device.error_text
        return None if text is None else {"error_text": text}


class AironeStateSensor(AironeEntity, SensorEntity):
    """`running` rendered as a human-readable name: running, stopped, or away."""

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
            # The state itself says this is unverified on a real device, so the user knows
            # before building an automation on it.
            "verified_on_hardware": False,
        }
        if device.odu_model_code:
            attrs["odu_model_code"] = device.odu_model_code
        if device.target_humidity is not None:
            attrs["target_humidity"] = device.target_humidity
        # Present only while auto-drying. The state text stays as the auto-dry label and the
        # number lives here — mixing it into the state would break any automation comparing
        # the string.
        if device.auto_dry_percent is not None:
            attrs["auto_dry_percent"] = device.auto_dry_percent
        return attrs


class AironeErrorSensor(AironeEntity, SensorEntity):
    """Error code. Zero means healthy."""

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
    """One air-quality item, identical whether it attaches to the main unit or the air monitor."""

    _kind: str
    _numeric: bool

    def _setup_air(self, device: AironeDevice, kind: str, unique_prefix: str) -> None:
        self._kind = kind
        self._attr_unique_id = f"{unique_prefix}_air_{kind}"
        label, unit, device_class = AIRONE_SENSOR_KINDS[kind]
        self._attr_name = label

        # Whether it is numeric is decided **from the first value**. Deciding from the kind
        # alone gets it wrong: the app displays tvoc, radon and the overall index as grades,
        # which suggested there was no number, but user reports confirmed numbers do arrive.
        #
        # **There may be no value yet**, when the server left that kind out of this poll
        # (which is what happens if the air monitor drops out briefly). The kind is remembered
        # and the entity created anyway, but settling on "string" here would freeze it without
        # a unit or a device_class once the value returns — worse than a gap in history. All
        # nine kinds confirmed on real devices arrived as numbers, so numeric is assumed.
        raw = device.air_sensors.get(kind)
        self._numeric = (
            as_number((raw or {}).get("value")) is not None if raw else True
        )
        if self._numeric:
            self._attr_state_class = "measurement"
            if unit is not None:
                self._attr_native_unit_of_measurement = unit
            # HA uses this for icons, history graphs and unit conversion. It only fits when
            # the unit matches, so it is attached only for numeric values.
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
            # With no grade available, fall back to the raw value as a string.
            return level_text(raw) or text_or_none(raw.get("value"))
        # A sensor that started numeric leaves the state empty if a string arrives. Mixing the
        # two kills a sensor carrying a `state_class` with an exception.
        return as_number(raw.get("value"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        raw = self._raw
        if raw is None:
            return None
        attrs: dict[str, Any] = {"kind": self._kind}
        if (level := level_text(raw)) is not None:
            # Numeric sensors keep the grade too — the grade is what the app displays.
            attrs["grade"] = level
        if self._kind in AIRONE_INFERRED_UNITS:
            # This unit is a judgement call, not something taken from the app. Exposed so the
            # choice is visible from outside.
            attrs["unit_inferred"] = True
        return attrs


class AironeAirSensor(_AirSensorMixin, AironeEntity, SensorEntity):
    """An air-quality item, attached to the main device when there is no air monitor."""

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: AironeDevice,
        kind: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._setup_air(device, kind, device.device_id)


class AironeMonitorSensor(_AirSensorMixin, AironeMonitorEntity, SensorEntity):
    """An air-quality item, attached to the air monitor device."""

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
    """Values that only the older status frame carries.

    Only the ones whose meaning is confirmed get a name, and the values are shown raw rather
    than interpreted — the 1/2 flag scheme is unconfirmed, so they are not translated into
    on and off.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:code-braces"

    def __init__(self, coordinator, device, key: str, label: str, table: str | None) -> None:
        super().__init__(coordinator, device)
        self._key = key
        self._table = LEGACY_VALUE_TABLES.get(table or "")
        self._attr_unique_id = f"{device.device_id}_legacy_{key}"
        self._attr_name = label
        # The unit of the filter usage time was never confirmed. Attaching "h" without knowing
        # whether it is hours or minutes would make a value that is wrong by 60x look
        # plausible. Only the number is kept.

    @property
    def native_value(self):
        value = self.device.legacy_extras.get(self._key)
        if self._table is None or value is None:
            return value
        # A value absent from the table is shown as its number — unknown values are not hidden.
        return self._table.get(value, value)


class AironeFilterSensor(AironeEntity, SensorEntity):
    """Filter life **remaining**, in percent — one per filter the outdoor unit reports.

    **The field name means the opposite of what it says.** The value comes from
    `odu.filter[i].usage.percent`, which reads like "amount used" but is the remaining life:
    87 means 87% left and 13% used. Confirmed on a real device against the Navien app display.
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
        # What the filter `type` means is unconfirmed, so the name carries a number rather
        # than a kind.
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

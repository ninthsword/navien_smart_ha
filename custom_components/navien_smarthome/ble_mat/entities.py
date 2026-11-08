"""The mat's entities. They only read the connection and call its commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..const import DOMAIN
from .connection import LINK_STATES, SIDE_LEFT, SIDE_RIGHT, MatConnection
from .protocol import (
    HEAT_LEFT_ONLY,
    HEAT_RIGHT_ONLY,
    MAX_TARGET,
    MIN_TARGET,
    MODE_ERROR,
    MatState,
    is_valid_target,
)

MODE_NAMES = {
    1: "난방",
    2: "예약",
    3: "수면",
    4: "살균",
    5: "오류",
    6: "꺼짐",
    7: "급속 난방",
    8: "이온 케어",
    9: "맞춤 수면",
}


class _MatEntity(Entity):
    """Reads everything from the connection whenever it publishes."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, conn: MatConnection, suffix: str) -> None:
        self._conn = conn
        self._attr_unique_id = f"{conn.address.replace(':', '').lower()}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, conn.address)},
            name="나비엔 온수매트",
            manufacturer="경동나비엔",
        )
        self._refresh()

    def _refresh(self) -> None:
        """Copy the connection's state into this entity's attributes."""
        self._attr_available = self._conn.status is not None

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
    _attr_min_temp = MIN_TARGET
    _attr_max_temp = MAX_TARGET
    _attr_target_temperature_step = 1
    _attr_precision = PRECISION_WHOLE

    def __init__(self, conn: MatConnection, side: str) -> None:
        self._side = side
        self._attr_name = "왼쪽" if side == SIDE_LEFT else "오른쪽"
        super().__init__(conn, f"{side}_thermostat")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
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
        return target if is_valid_target(target) else None

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
        await self._conn.async_set_hvac_mode(hvac_mode)


class MatModeSensor(_MatEntity, SensorEntity):
    _attr_name = "운전 모드"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(MODE_NAMES.values())

    def __init__(self, conn: MatConnection) -> None:
        super().__init__(conn, "mode")

    def _refresh(self) -> None:
        super()._refresh()
        status = self._conn.status
        self._attr_native_value = (
            None if status is None else MODE_NAMES.get(status.mode)
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
    async_add_entities([MatModeSensor(conn), MatLinkSensor(conn)])


async def async_setup_mat_binary_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    conn: MatConnection = entry.runtime_data
    async_add_entities([MatErrorSensor(conn)])


async def async_setup_mat_switches(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    conn: MatConnection = entry.runtime_data
    async_add_entities([MatEnableSwitch(conn)])

"""Shared entity bases."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .airone import AironeDevice
from .boiler import BoilerDevice
from .const import DOMAIN, MODEL_TYPE_LABELS
from .coordinator import NavienSmartCoordinator
from .models import NavienDevice

_EntityT = TypeVar("_EntityT", bound=Entity)


def boiler_entities_as_declared(
    entry: ConfigEntry,
    coordinator: NavienSmartCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[BoilerDevice], Iterable[_EntityT]],
) -> list[_EntityT]:
    """Return the feature-gated boiler entities whose condition holds now, and add the rest
    as soon as their condition holds.

    The feature can arrive after setup: an NR-67D whose REST entry lost `Properties.did`
    declares it only in its MQTT answer to status/start. Each unique_id is added once and
    never removed here, so a later change in the declaration neither duplicates nor drops an
    entity. The listener belongs to the config entry and goes with it on unload.
    """
    added: set[str] = set()

    def _fresh() -> list[_EntityT]:
        fresh: list[_EntityT] = []
        for boiler in coordinator.boilers.values():
            for entity in build(boiler):
                unique_id = entity.unique_id
                if unique_id is not None and unique_id not in added:
                    added.add(unique_id)
                    fresh.append(entity)
        return fresh

    @callback
    def _add_late() -> None:
        if fresh := _fresh():
            async_add_entities(fresh)

    entry.async_on_unload(coordinator.async_add_listener(_add_late))
    return _fresh()


class NavienSmartEntity(CoordinatorEntity[NavienSmartCoordinator]):
    """Hold the device by `deviceId`. `deviceSeq` can change when a device is re-registered."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator)
        self._device_id = device.device_id
        self._attr_unique_id = f"{device.device_id}"

        model = device.model_name
        if label := MODEL_TYPE_LABELS.get(device.model_type or ""):
            model = f"{model} ({label})"

        # A mat carries separate firmware for its MCU and its Wi-Fi module, but HA's
        # `DeviceInfo` has only one firmware field, so the two are joined into one line.
        #
        # The Wi-Fi firmware does not go in `hw_version`: HA labels that field "hardware",
        # so putting firmware there would state something false. The server never reports
        # a hardware revision.
        firmware = device.mcu_version
        if firmware and device.wifi_version:
            firmware = f"{firmware} (Wi-Fi {device.wifi_version})"
        elif not firmware and device.wifi_version:
            firmware = f"Wi-Fi {device.wifi_version}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer="경동나비엔",
            name=device.nickname,
            model=model,
            model_id=device.model_code or None,
            serial_number=device.device_id,
            sw_version=firmware,
        )

    @property
    def device(self) -> NavienDevice | None:
        return (self.coordinator.data or {}).get(self._device_id)

    @property
    def available(self) -> bool:
        device = self.device
        return super().available and device is not None and device.available


def airone_device_info(device: AironeDevice) -> DeviceInfo:
    """Share complete parent metadata between setup and platform entities."""
    firmware = device.rc_version
    if firmware and device.odu_version:
        firmware = f"{firmware} (실외기 {device.odu_version})"
    elif not firmware and device.odu_version:
        firmware = f"실외기 {device.odu_version}"

    return DeviceInfo(
        identifiers={(DOMAIN, device.device_id)},
        manufacturer="경동나비엔",
        name=device.nickname,
        model=device.model_name,
        model_id=device.model_code or None,
        serial_number=device.device_id,
        sw_version=firmware,
    )


class AironeEntity(CoordinatorEntity[NavienSmartCoordinator]):
    """Base for Airone entities.

    Device info is built differently from a mat: the indoor unit (room controller) and the
    outdoor unit each carry firmware, so the two are joined into one line. Airone has no
    `modelType`.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator)
        self._device_id = device.device_id
        self._attr_unique_id = f"{device.device_id}"

        self._attr_device_info = airone_device_info(device)

    @property
    def device(self) -> AironeDevice | None:
        return self.coordinator.airone.get(self._device_id)

    @property
    def available(self) -> bool:
        device = self.device
        return super().available and device is not None and device.available


class BoilerEntity(CoordinatorEntity[NavienSmartCoordinator]):
    """Base for boiler sensor and setpoint entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator)
        self._device_id = device.device_id
        self._attr_unique_id = device.device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer="경동나비엔",
            name=device.nickname,
            model=device.model_name,
            model_id=device.model_code or None,
            serial_number=device.device_id,
        )

    @property
    def device(self) -> BoilerDevice | None:
        return self.coordinator.boilers.get(self._device_id)

    @property
    def available(self) -> bool:
        device = self.device
        return super().available and device is not None and device.available


class AironeMonitorEntity(CoordinatorEntity[NavienSmartCoordinator]):
    """Base for the air monitor (the air-quality sensor unit) entities.

    It is modelled as a **separate device** from the main unit: the app registers and pairs
    it separately, and it carries its own model name and firmware. `via_device_id` records the
    relationship to the main unit.

    Its `modelCode` is below 1000 (observed: NAA-21DM = 35), but it **takes no commands**,
    so that number says nothing about the protocol generation.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: AironeDevice,
        monitor: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.device_id
        monitor_id = str(monitor.get("deviceId") or f"{device.device_id}_airmonitor")
        self._monitor_id = monitor_id

        model_code = monitor.get("modelCode")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, monitor_id)},
            manufacturer="경동나비엔",
            name=f"{device.nickname} 에어모니터",
            # Do not hard-code a model name. When the server sends only a code, show the code.
            model="에어모니터",
            model_id=str(model_code) if model_code is not None else None,
            serial_number=monitor_id,
            sw_version=monitor.get("version") or None,
            via_device_id=coordinator.airone_device_registry_ids[device.device_id],
        )

    @property
    def device(self) -> AironeDevice | None:
        return self.coordinator.airone.get(self._device_id)

    @property
    def available(self) -> bool:
        device = self.device
        return super().available and device is not None and device.available

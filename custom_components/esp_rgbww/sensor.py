"""Sensor platform for Lightinator (ESP RGBWW)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CHIP_ID, CONF_DEVICE_NAME, DOMAIN
from .coordinator import LightinatorCoordinator


@dataclass(frozen=True)
class LightinatorSensorDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with the nested JSON path to the value.

    info_path is a tuple of keys that drill into coordinator.data["info"].
    E.g. ("runtime", "uptime") resolves info["runtime"]["uptime"].
    """

    info_path: tuple[str, ...] = field(default_factory=tuple)


SENSORS: tuple[LightinatorSensorDescription, ...] = (
    LightinatorSensorDescription(
        key="uptime",
        info_path=("runtime", "uptime"),
        name="Uptime",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    LightinatorSensorDescription(
        key="heap_free",
        info_path=("runtime", "heap_free"),
        name="Free Heap",
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    LightinatorSensorDescription(
        key="wifi_connected",
        info_path=("connection", "connected"),
        name="WiFi Connected",
        device_class=None,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    LightinatorSensorDescription(
        key="ip_address",
        info_path=("connection", "ip"),
        name="IP Address",
        device_class=None,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lightinator sensor entities."""
    coordinator: LightinatorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LightinatorSensor(coordinator, entry, desc) for desc in SENSORS]
    )


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
        name=entry.data[CONF_DEVICE_NAME],
        manufacturer="ESP RGBWW Firmware",
        model="RGBWW Controller",
        configuration_url=f"http://{entry.data[CONF_HOST]}",
    )


class LightinatorSensor(CoordinatorEntity[LightinatorCoordinator], SensorEntity):
    """A diagnostic sensor backed by a field in the /info?v=2 response."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LightinatorCoordinator,
        entry: ConfigEntry,
        desc: LightinatorSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = desc
        self._info_path = desc.info_path
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_{desc.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> Any:
        """Drill into info JSON using the configured path."""
        node: Any = (self.coordinator.data or {}).get("info", {})
        for key in self._info_path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

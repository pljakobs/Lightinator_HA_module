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
from homeassistant.const import (
    CONF_HOST,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CHIP_ID, CONF_DEVICE_NAME, DOMAIN
from .coordinator import LightinatorCoordinator
from .extension_coordinator import ExtensionCoordinator
from .extension_readings import sensor_reading_value


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
        state_class=SensorStateClass.MEASUREMENT,
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
        key="rssi",
        info_path=("connection", "rssi"),
        name="Signal Strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
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
        + [
            ExtensionReadingSensor(coordinator.extensions, entry, extension_id, key, name, unit, device_class, category)
            for extension_id, key, name, unit, device_class, category in _EXTENSION_SENSORS
            if coordinator.extensions.supported[extension_id]
        ]
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


_CURRENT_SENSOR_FIELDS = (
    ("current_a", "Current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT),
    ("voltage_v", "Voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE),
    ("power_w", "Power", UnitOfPower.WATT, SensorDeviceClass.POWER),
)
_SPECTRUM_CHANNELS = tuple(
    (f"f{channel}", f"Spectral Channel F{channel}", "count", None)
    for channel in range(1, 9)
) + tuple(
    (key, name, "count", None)
    for key, name in (
        ("clear_low", "Clear Brightness Low (Raw ADC)"),
        ("clear_high", "Clear Brightness High (Raw ADC)"),
        ("nir_low", "Near Infrared Low"),
        ("nir_high", "Near Infrared High"),
    )
)
_EXTENSION_SENSORS = (
    *(
        ("current-1", key, name, unit, device_class, EntityCategory.DIAGNOSTIC if key == "error" else None)
        for key, name, unit, device_class in _CURRENT_SENSOR_FIELDS
    ),
    ("current-1", "error", "Current Sensor Error", None, None, EntityCategory.DIAGNOSTIC),
    ("current-1", "age_ms", "Current Reading Age", UnitOfTime.MILLISECONDS, None, EntityCategory.DIAGNOSTIC),
    ("spectrum-1", "state", "Spectral Measurement State", None, None, EntityCategory.DIAGNOSTIC),
    ("spectrum-1", "error", "Spectral Sensor Error", None, None, EntityCategory.DIAGNOSTIC),
    ("spectrum-1", "age_ms", "Spectral Reading Age", UnitOfTime.MILLISECONDS, None, EntityCategory.DIAGNOSTIC),
    ("spectrum-1", "atime", "Spectral Reading ATIME", None, None, EntityCategory.DIAGNOSTIC),
    ("spectrum-1", "astep", "Spectral Reading ASTEP", None, None, EntityCategory.DIAGNOSTIC),
    ("spectrum-1", "gain", "Spectral Reading Gain", None, None, EntityCategory.DIAGNOSTIC),
    *(
        ("spectrum-1", key, name, unit, None, None)
        for key, name, unit, _ in _SPECTRUM_CHANNELS
    ),
)


class ExtensionReadingSensor(CoordinatorEntity[ExtensionCoordinator], SensorEntity):
    """Expose extension readings without inventing values for invalid samples."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ExtensionCoordinator,
        entry: ConfigEntry,
        extension_id: str,
        key: str,
        name: str,
        unit: str | None,
        device_class: SensorDeviceClass | None,
        category: EntityCategory | None,
    ) -> None:
        super().__init__(coordinator)
        self._extension_id = extension_id
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_entity_category = category
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_{extension_id}_{key}"
        if key in {field[0] for field in _CURRENT_SENSOR_FIELDS} or key in {
            channel[0] for channel in _SPECTRUM_CHANNELS
        }:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_info = _device_info(entry)

    @property
    def available(self) -> bool:
        if self._key == "error":
            return super().available and bool(
                self.coordinator.supported[self._extension_id]
            )
        config = self.coordinator.configs.get(self._extension_id, {})
        if not (
            super().available
            and bool(config.get("enabled") and config.get("available"))
        ):
            return False
        extension = (self.coordinator.data or {}).get(
            "current" if self._extension_id == "current-1" else "spectrum", {}
        )
        if not extension.get("readings_available"):
            return False
        reading = extension.get("reading")
        if not isinstance(reading, dict):
            return False
        return self._key == "state" or reading.get("valid") is True

    @property
    def native_value(self) -> Any:
        extension = (self.coordinator.data or {}).get(
            "current" if self._extension_id == "current-1" else "spectrum", {}
        )
        return sensor_reading_value(extension, self._extension_id, self._key)

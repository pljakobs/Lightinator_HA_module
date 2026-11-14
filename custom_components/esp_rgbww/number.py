"""Numeric configuration entities for optional sensor extensions."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CHIP_ID, CONF_DEVICE_NAME, DOMAIN
from .coordinator import LightinatorCoordinator
from .extension_coordinator import ExtensionCoordinator

_LOGGER = logging.getLogger(__name__)

_CONFIG_NUMBERS = (
    ("current-1", "address", "Current Sensor I²C Address", 64, 79, 1, ""),
    ("current-1", "shunt_ohms", "Current Sensor Shunt Resistance", 0.00001, 1, 0.00001, "Ω"),
    ("current-1", "interval_ms", "Current Sensor Sampling Interval", 200, 3600000, 1, "ms"),
    ("spectrum-1", "atime", "Spectral Sensor ATIME", 0, 255, 1, ""),
    ("spectrum-1", "astep", "Spectral Sensor ASTEP", 0, 65535, 1, ""),
    ("spectrum-1", "gain", "Spectral Sensor Gain", 0, 10, 1, ""),
    ("spectrum-1", "interval_ms", "Spectral Sensor Interval (Stored Only)", 200, 3600000, 1, "ms"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up extension configuration number entities."""
    controller: LightinatorCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator: ExtensionCoordinator = controller.extensions
    async_add_entities(
        [
            ExtensionConfigNumber(coordinator, entry, *spec)
            for spec in _CONFIG_NUMBERS
            if coordinator.supported[spec[0]]
        ]
    )


class ExtensionConfigNumber(CoordinatorEntity[ExtensionCoordinator], NumberEntity):
    """One persistent number setting provided by an extension endpoint."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: ExtensionCoordinator,
        entry: ConfigEntry,
        extension_id: str,
        key: str,
        name: str,
        minimum: float,
        maximum: float,
        step: float,
        unit: str,
    ) -> None:
        super().__init__(coordinator)
        self._extension_id = extension_id
        self._key = key
        self._attr_name = name
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit or None
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_{extension_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="ESP RGBWW Firmware",
            model="RGBWW Controller",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    @property
    def available(self) -> bool:
        return super().available and self._extension_id in self.coordinator.configs

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.configs.get(self._extension_id, {}).get(self._key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    async def async_set_native_value(self, value: float) -> None:
        """Persist the setting through the authenticated firmware client."""
        try:
            if self._key != "shunt_ohms":
                if not float(value).is_integer():
                    raise ValueError(f"{self._key} must be an integer")
                value = int(value)
            await self.coordinator.async_update_extension_config(
                self._extension_id, {self._key: value}
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Unable to update %s: %s", self._key, err)
            raise HomeAssistantError(str(err)) from err

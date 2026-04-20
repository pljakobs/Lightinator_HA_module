"""Select platform for Lightinator (ESP RGBWW) — preset selection."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CHIP_ID, CONF_DEVICE_NAME, DOMAIN
from .coordinator import LightinatorCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lightinator preset select entity."""
    coordinator: LightinatorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LightinatorPresetSelect(coordinator, entry)])


class LightinatorPresetSelect(CoordinatorEntity[LightinatorCoordinator], SelectEntity):
    """A select entity listing the saved colour presets on the device.

    Selecting an option triggers a solid-colour command using the preset's
    HSV values.  The current option is always None because the firmware
    does not report which preset is active.
    """

    _attr_has_entity_name = True
    _attr_name = "Preset"

    def __init__(
        self, coordinator: LightinatorCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_preset"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="ESP RGBWW Firmware",
            model="RGBWW Controller",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    @property
    def options(self) -> list[str]:
        presets = (self.coordinator.data or {}).get("presets", [])
        return [p["name"] for p in presets if "name" in p]

    @property
    def current_option(self) -> str | None:
        # The firmware has no concept of an "active" preset
        return None

    async def async_select_option(self, option: str) -> None:
        """Apply the selected preset."""
        presets = (self.coordinator.data or {}).get("presets", [])
        preset = next((p for p in presets if p.get("name") == option), None)

        if preset is None:
            _LOGGER.warning("Preset '%s' not found in device data", option)
            return

        hsv = preset.get("color", {}).get("hsv")
        if not hsv:
            _LOGGER.warning("Preset '%s' has no HSV colour data", option)
            return

        await self.coordinator.post("/color", {"cmd": "solid", "hsv": hsv})

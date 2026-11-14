"""Enable controls for optional sensor extensions."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.components.switch import SwitchEntity
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
_SWITCHES = (
    ("current-1", "Current Sensor Enabled"),
    ("spectrum-1", "Spectral Sensor Enabled"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up extension enable switches."""
    controller: LightinatorCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator: ExtensionCoordinator = controller.extensions
    async_add_entities(
        [
            ExtensionEnabledSwitch(coordinator, entry, extension_id, name)
            for extension_id, name in _SWITCHES
            if coordinator.supported[extension_id]
        ]
    )


class ExtensionEnabledSwitch(CoordinatorEntity[ExtensionCoordinator], SwitchEntity):
    """Enable or disable an optional firmware sensor."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: ExtensionCoordinator,
        entry: ConfigEntry,
        extension_id: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._extension_id = extension_id
        self._attr_name = name
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_{extension_id}_enabled"
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
    def is_on(self) -> bool | None:
        enabled = self.coordinator.configs.get(self._extension_id, {}).get("enabled")
        return enabled if isinstance(enabled, bool) else None

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._set_enabled(False)

    async def _set_enabled(self, enabled: bool) -> None:
        try:
            await self.coordinator.async_update_extension_config(
                self._extension_id, {"enabled": enabled}
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Unable to set %s enabled=%s: %s", self._extension_id, enabled, err)
            raise HomeAssistantError(str(err)) from err

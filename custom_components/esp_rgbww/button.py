"""Button platform for Lightinator (ESP RGBWW)."""

from __future__ import annotations

import aiohttp
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CHIP_ID, CONF_DEVICE_NAME, DOMAIN
from .coordinator import LightinatorCoordinator
from .extension_coordinator import ExtensionCoordinator

# (unique_key, display_name, HTTP endpoint)
_BUTTONS: list[tuple[str, str, str]] = [
    ("stop", "Stop Animation", "/stop"),
    ("skip", "Skip Step", "/skip"),
    ("pause", "Pause Animation", "/pause"),
    ("continue", "Resume Animation", "/continue"),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lightinator button entities."""
    coordinator: LightinatorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LightinatorButton(coordinator, entry, key, label, endpoint)
            for key, label, endpoint in _BUTTONS
        ]
        + [LightinatorFirmwareCheckButton(hass, entry)]
        + (
            [SpectrumMeasurementButton(coordinator.extensions, entry)]
            if coordinator.extensions.supported["spectrum-1"]
            else []
        )
    )


class LightinatorButton(ButtonEntity):
    """A button that sends a fire-and-forget POST command to the device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LightinatorCoordinator,
        entry: ConfigEntry,
        key: str,
        label: str,
        endpoint: str,
    ) -> None:
        self._coordinator = coordinator
        self._endpoint = endpoint
        self._attr_name = label
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="ESP RGBWW Firmware",
            model="RGBWW Controller",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    async def async_press(self) -> None:
        """Send the command to the device."""
        await self._coordinator.post(self._endpoint)


class LightinatorFirmwareCheckButton(ButtonEntity):
    """Refresh the device's firmware update entity on demand."""

    _attr_has_entity_name = True
    _attr_name = "Check for Updates"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._firmware_unique_id = f"{entry.data[CONF_CHIP_ID]}_firmware"
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_check_firmware"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="ESP RGBWW Firmware",
            model="RGBWW Controller",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    async def async_press(self) -> None:
        """Ask Home Assistant to refresh this device's firmware entity."""
        entity_id = er.async_get(self._hass).async_get_entity_id(
            "update", DOMAIN, self._firmware_unique_id
        )
        if entity_id is None:
            return
        await self._hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": entity_id},
            blocking=True,
        )


class SpectrumMeasurementButton(CoordinatorEntity[ExtensionCoordinator], ButtonEntity):
    """Start one on-demand AS7341 spectral reading."""

    _attr_has_entity_name = True
    _attr_name = "Start Spectral Measurement"

    def __init__(self, coordinator: ExtensionCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_spectrum-1_start_measurement"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="ESP RGBWW Firmware",
            model="RGBWW Controller",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    @property
    def available(self) -> bool:
        config = self.coordinator.configs.get("spectrum-1", {})
        return super().available and bool(config.get("enabled") and config.get("available"))

    async def async_press(self) -> None:
        """Request a measurement and report firmware errors to Home Assistant."""
        try:
            await self.coordinator.async_start_spectrum_measurement()
        except aiohttp.ClientResponseError as err:
            if err.status == 409:
                raise HomeAssistantError("A spectral measurement is already in progress") from err
            raise HomeAssistantError(str(err)) from err
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

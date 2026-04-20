"""Button platform for Lightinator (ESP RGBWW)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CHIP_ID, CONF_DEVICE_NAME, DOMAIN
from .coordinator import LightinatorCoordinator

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

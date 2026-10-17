"""Light platform for Lightinator (ESP RGBWW)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHIP_ID,
    CONF_DEVICE_NAME,
    DOMAIN,
    RAW_MAX,
    RAW_CHANNELS_BY_MODEL,
    configured_color_temperature_range,
)
from .coordinator import LightinatorCoordinator
from .presets import favorite_preset_colors

_LOGGER = logging.getLogger(__name__)
_CONTROLLER_ICON_TO_MDI = {
    "led-strip-variant": "mdi:led-strip-variant",
    "lights_led-strip-variant": "mdi:led-strip-variant",
    "lightbulb": "mdi:lightbulb",
    "lightbulb_outlined": "mdi:lightbulb-outline",
    "lightbulb-on": "mdi:lightbulb-on",
    "lightbulb-off": "mdi:lightbulb-off",
    "home": "mdi:home",
    "palette": "mdi:palette",
    "palette_outlined": "mdi:palette-outline",
}


def _controller_icon_to_mdi(icon: Any) -> str | None:
    if not isinstance(icon, str) or not icon:
        return None

    icon_name = icon
    if icon.startswith(("http://", "https://")):
        path_parts = urlsplit(icon).path.rstrip("/").split("/")
        if len(path_parts) >= 3:
            icon_name = path_parts[-3]

    return _CONTROLLER_ICON_TO_MDI.get(icon_name)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
        name=entry.data[CONF_DEVICE_NAME],
        manufacturer="ESP RGBWW Firmware",
        model="RGBWW Controller",
        configuration_url=f"http://{entry.data[CONF_HOST]}",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lightinator light entities."""
    coordinator: LightinatorCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[LightEntity] = [LightinatorMainLight(coordinator, entry)]

    raw = (coordinator.data or {}).get("raw", {})
    config = (coordinator.data or {}).get("config", {})
    color_config = config.get("color", {}) if isinstance(config, dict) else {}
    model = color_config.get("color_mode") if isinstance(color_config, dict) else None
    channels = RAW_CHANNELS_BY_MODEL.get(model)
    if channels is None:
        channels = tuple(
            (channel, label)
            for channel, label in (
                ("r", "Red"),
                ("g", "Green"),
                ("b", "Blue"),
                ("ww", "Warm White"),
                ("cw", "Cool White"),
            )
            if channel in raw
        )
    entities.extend(
        LightinatorChannelLight(coordinator, entry, channel, label)
        for channel, label in channels
        if channel in raw
    )

    async_add_entities(entities)


class LightinatorMainLight(CoordinatorEntity[LightinatorCoordinator], LightEntity):
    """Main RGBWW light — supports HS colour and colour temperature.

    The firmware's hsv.ct value is expressed in Kelvin.
    """

    _attr_has_entity_name = True
    _attr_name = None  # device name used as entity name
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.TRANSITION
    def __init__(
        self, coordinator: LightinatorCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = entry.data[CONF_CHIP_ID]
        self._attr_device_info = _device_info(entry)

    @property
    def min_color_temp_kelvin(self) -> int:
        config = (self.coordinator.data or {}).get("config", {})
        return configured_color_temperature_range(config)[0]

    @property
    def max_color_temp_kelvin(self) -> int:
        config = (self.coordinator.data or {}).get("config", {})
        return configured_color_temperature_range(config)[1]

    @property
    def icon(self) -> str | None:
        return _controller_icon_to_mdi(
            (self.coordinator.data or {}).get("controller_icon")
        )

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        hsv = (self.coordinator.data or {}).get("hsv", {})
        return hsv.get("v", 0) > 0

    @property
    def brightness(self) -> int | None:
        hsv = (self.coordinator.data or {}).get("hsv", {})
        return round(hsv.get("v", 0) * 255 / 100)

    @property
    def hs_color(self) -> tuple[float, float] | None:
        hsv = (self.coordinator.data or {}).get("hsv", {})
        return (float(hsv.get("h", 0)), float(hsv.get("s", 0)))

    @property
    def color_temp_kelvin(self) -> int | None:
        hsv = (self.coordinator.data or {}).get("hsv", {})
        ct = hsv.get("ct", 0)
        if not ct:
            return None
        return int(ct)

    @property
    def color_mode(self) -> ColorMode:
        hsv = (self.coordinator.data or {}).get("hsv", {})
        if hsv.get("ct", 0) > 0:
            return ColorMode.COLOR_TEMP
        return ColorMode.HS

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._sync_firmware_favorite_colors()

    def _handle_coordinator_update(self) -> None:
        self._sync_firmware_favorite_colors()
        super()._handle_coordinator_update()

    def _sync_firmware_favorite_colors(self) -> None:
        if not self.entity_id:
            return

        registry = er.async_get(self.hass)
        registry_entry = registry.async_get(self.entity_id)
        if registry_entry is None:
            return

        entity_options = dict(registry_entry.options.get("light", {}))
        current_colors = entity_options.get("favorite_colors")
        previous_firmware_colors = entity_options.get(
            "_lightinator_favorite_colors"
        )
        if current_colors is not None and (
            previous_firmware_colors is None
            or current_colors != previous_firmware_colors
        ):
            return

        coordinator_data = self.coordinator.data or {}
        firmware_colors = favorite_preset_colors(
            coordinator_data.get("presets", []),
            coordinator_data.get("config", {}),
        )
        if (
            current_colors == firmware_colors
            and previous_firmware_colors == firmware_colors
        ):
            return
        if not firmware_colors and previous_firmware_colors is None:
            return

        entity_options["favorite_colors"] = firmware_colors
        entity_options["_lightinator_favorite_colors"] = firmware_colors
        registry.async_update_entity_options(
            self.entity_id,
            "light",
            entity_options,
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on or change the light color."""
        # Work from current state so unspecified fields are preserved
        hsv: dict[str, Any] = dict((self.coordinator.data or {}).get("hsv", {}))
        ramp = int(kwargs.get(ATTR_TRANSITION, 0.5) * 1000)  # ms

        if ATTR_BRIGHTNESS in kwargs:
            requested = kwargs[ATTR_BRIGHTNESS]
            hsv["v"] = max(1, round(requested * 100 / 255)) if requested > 0 else 0
        if ATTR_HS_COLOR in kwargs:
            hsv["h"], hsv["s"] = kwargs[ATTR_HS_COLOR]
            hsv.pop("ct", None)
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            hsv["ct"] = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
            hsv["s"] = 0

        # Ensure the light actually turns on if brightness was 0
        if hsv.get("v", 0) == 0:
            hsv["v"] = 100

        await self.coordinator.post("/color", {"cmd": "fade", "hsv": hsv, "t": ramp})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self.coordinator.post("/off", {})


class LightinatorChannelLight(CoordinatorEntity[LightinatorCoordinator], LightEntity):
    """A raw PWM channel exposed as a brightness-only light."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(
        self,
        coordinator: LightinatorCoordinator,
        entry: ConfigEntry,
        channel: str,
        label: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._channel = channel
        self._attr_name = label
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_{channel}"
        self._attr_device_info = _device_info(entry)

    @property
    def _raw_value(self) -> int:
        raw = (self.coordinator.data or {}).get("raw", {})
        return int(raw.get(self._channel, 0))

    @property
    def is_on(self) -> bool:
        return self._raw_value > 0

    @property
    def brightness(self) -> int | None:
        return round(self._raw_value * 255 / RAW_MAX)

    async def async_turn_on(self, **kwargs: Any) -> None:
        raw_val = RAW_MAX
        if ATTR_BRIGHTNESS in kwargs:
            raw_val = round(kwargs[ATTR_BRIGHTNESS] * RAW_MAX / 255)
        await self.coordinator.post("/color", {"raw": {self._channel: raw_val}})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.post("/color", {"raw": {self._channel: 0}})
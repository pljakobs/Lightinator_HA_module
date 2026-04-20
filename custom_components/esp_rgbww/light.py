"""Light platform for Lightinator (ESP RGBWW)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHIP_ID,
    CONF_DEVICE_NAME,
    CT_MAX_MIREDS,
    CT_MIN_MIREDS,
    CT_MIREDS_RANGE,
    DOMAIN,
    RAW_MAX,
)
from .coordinator import LightinatorCoordinator

_LOGGER = logging.getLogger(__name__)


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

    # Per-channel raw lights for warm-white and cool-white channels
    raw = (coordinator.data or {}).get("raw", {})
    if "ww" in raw:
        entities.append(LightinatorChannelLight(coordinator, entry, "ww", "Warm White"))
    if "cw" in raw:
        entities.append(LightinatorChannelLight(coordinator, entry, "cw", "Cool White"))

    async_add_entities(entities)


class LightinatorMainLight(CoordinatorEntity[LightinatorCoordinator], LightEntity):
    """Main RGBWW light — supports HS colour and colour temperature.

    Colour temperature conversions:
      firmware ct (0-100)  ←→  mireds (153-370)
      ct = round((mireds - CT_MIN_MIREDS) * 100 / CT_MIREDS_RANGE)
    """

    _attr_has_entity_name = True
    _attr_name = None  # device name used as entity name
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.TRANSITION | LightEntityFeature.EFFECT
    _attr_min_mireds = CT_MIN_MIREDS
    _attr_max_mireds = CT_MAX_MIREDS

    def __init__(
        self, coordinator: LightinatorCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = entry.data[CONF_CHIP_ID]
        self._attr_device_info = _device_info(entry)

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
    def color_temp(self) -> int | None:
        hsv = (self.coordinator.data or {}).get("hsv", {})
        ct = hsv.get("ct", 0)
        if not ct:
            return None
        return CT_MIN_MIREDS + round(ct * CT_MIREDS_RANGE / 100)

    @property
    def color_mode(self) -> ColorMode:
        hsv = (self.coordinator.data or {}).get("hsv", {})
        if hsv.get("ct", 0) > 0:
            return ColorMode.COLOR_TEMP
        return ColorMode.HS

    @property
    def effect_list(self) -> list[str] | None:
        presets = (self.coordinator.data or {}).get("presets", [])
        names = [p["name"] for p in presets if "name" in p]
        return names or None

    @property
    def effect(self) -> str | None:
        # The firmware does not expose which preset is currently active
        return None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on / change colour / play preset."""
        # Work from current state so unspecified fields are preserved
        hsv: dict[str, Any] = dict((self.coordinator.data or {}).get("hsv", {}))
        ramp = int(kwargs.get(ATTR_TRANSITION, 0.5) * 1000)  # ms

        if ATTR_EFFECT in kwargs:
            preset_name: str = kwargs[ATTR_EFFECT]
            presets = (self.coordinator.data or {}).get("presets", [])
            preset = next(
                (p for p in presets if p.get("name") == preset_name), None
            )
            if preset:
                p_hsv = preset.get("color", {}).get("hsv", {})
                hsv.update(
                    {
                        "h": p_hsv.get("h", hsv.get("h", 0)),
                        "s": p_hsv.get("s", hsv.get("s", 100)),
                        "v": p_hsv.get("v", hsv.get("v", 100)),
                    }
                )
                hsv.pop("ct", None)
            else:
                _LOGGER.warning("Preset '%s' not found", preset_name)
        else:
            if ATTR_BRIGHTNESS in kwargs:
                hsv["v"] = round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)
            if ATTR_HS_COLOR in kwargs:
                hsv["h"], hsv["s"] = kwargs[ATTR_HS_COLOR]
                hsv.pop("ct", None)
            if ATTR_COLOR_TEMP in kwargs:
                mireds: int = kwargs[ATTR_COLOR_TEMP]
                hsv["ct"] = round(
                    (mireds - CT_MIN_MIREDS) * 100 / CT_MIREDS_RANGE
                )
                hsv["s"] = 0

        # Ensure the light actually turns on if brightness was 0
        if hsv.get("v", 0) == 0:
            hsv["v"] = 100

        await self.coordinator.post("/color", {"cmd": "fade", "hsv": hsv, "t": ramp})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self.coordinator.post("/off")


class LightinatorChannelLight(CoordinatorEntity[LightinatorCoordinator], LightEntity):
    """A single raw PWM channel (ww / cw) exposed as a brightness-only light."""

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

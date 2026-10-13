"""Light platform for Lightinator (ESP RGBWW)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
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
    DOMAIN,
    RAW_MAX,
    RAW_CHANNELS_BY_MODEL,
    configured_color_temperature_range,
)
from .coordinator import LightinatorCoordinator
from .presets import preset_command, preset_names

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
    _attr_supported_features = LightEntityFeature.TRANSITION | LightEntityFeature.EFFECT
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

    @property
    def effect_list(self) -> list[str] | None:
        presets = (self.coordinator.data or {}).get("presets", [])
        names = preset_names(presets)
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
                (
                    p
                    for p in presets
                    if isinstance(p, dict) and p.get("name") == preset_name
                ),
                None,
            )
            if preset is None:
                _LOGGER.warning("Preset '%s' not found", preset_name)
                return

            command = preset_command(
                preset,
                presets,
                default_command="fade",
                default_transition_ms=ramp,
                transition_ms=(
                    ramp if ATTR_TRANSITION in kwargs else None
                )
            )
            if command is None:
                _LOGGER.warning("Preset '%s' has no supported color", preset_name)
                return
            await self.coordinator.post("/color", command)
            return
        else:
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
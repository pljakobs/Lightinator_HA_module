"""Light platform for Lightinator (ESP RGBWW)."""

from __future__ import annotations

import logging
import colorsys
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
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
from .virtual_lights import (
    CHANNEL_LABELS,
    CONF_LEGACY_LIGHT_ID,
    VirtualLight,
    channel_command,
    raw_value,
)

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

    if coordinator.virtual_lights is not None:
        added: set[tuple[str, str | None]] = set()

        def add_virtual_lights() -> None:
            lights = coordinator.virtual_lights or {}
            registry = er.async_get(hass)
            legacy_id = entry.data.get(CONF_LEGACY_LIGHT_ID)
            if legacy_id is None and registry.async_get_entity_id(
                "light", DOMAIN, entry.data[CONF_CHIP_ID]
            ):
                default = next((light for light in lights.values() if light.is_default), None)
                if default is not None:
                    legacy_id = default.id
                    hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_LEGACY_LIGHT_ID: legacy_id}
                    )

            entities: list[LightEntity] = []
            for light in lights.values():
                if (light.id, None) not in added:
                    entities.append(LightinatorVirtualLight(
                        coordinator, entry, light, legacy=light.id == legacy_id
                    ))
                    added.add((light.id, None))
                for channel in light.channels:
                    if len(light.channels) == 1 or (light.id, channel) in added:
                        continue
                    old_id = f"{entry.data[CONF_CHIP_ID]}_{channel}"
                    legacy = light.id == legacy_id and bool(
                        registry.async_get_entity_id("light", DOMAIN, old_id)
                    )
                    entities.append(LightinatorVirtualChannelLight(
                        coordinator, entry, light, channel, legacy=legacy
                    ))
                    added.add((light.id, channel))
            if entities:
                async_add_entities(entities)

        add_virtual_lights()
        entry.async_on_unload(coordinator.async_add_listener(add_virtual_lights))
        return

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


class LightinatorVirtualLight(LightinatorMainLight):
    """One UUID-bound light on a 5.1 controller."""

    def __init__(
        self, coordinator: LightinatorCoordinator, entry: ConfigEntry,
        light: VirtualLight, *, legacy: bool = False,
    ) -> None:
        super().__init__(coordinator, entry)
        self._light_id = light.id
        self._attr_extra_state_attributes = {"light_id": light.id}
        self._initial_light = light
        self._last_on_raw: list[int] = []
        self._attr_unique_id = (
            entry.data[CONF_CHIP_ID] if legacy
            else f"{entry.data[CONF_CHIP_ID]}_{light.id}"
        )

    @property
    def _light(self) -> VirtualLight:
        return (self.coordinator.virtual_lights or {}).get(self._light_id, self._initial_light)

    @property
    def _state(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("light_states", {}).get(self._light_id, {})

    @property
    def name(self) -> str:
        return self._light.name

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._light_id in (self.coordinator.virtual_lights or {})
            and bool(self._state)
        )

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {
            "RGB": {ColorMode.HS},
            "RGBWW": {ColorMode.RGBW},
            "RGBCW": {ColorMode.RGBW},
            "RGBWWCW": {ColorMode.HS, ColorMode.COLOR_TEMP, ColorMode.RGBWW},
            "WWCW": {ColorMode.COLOR_TEMP},
        }.get(self._light.type, {ColorMode.BRIGHTNESS})

    @property
    def min_color_temp_kelvin(self) -> int:
        return configured_color_temperature_range({"color": self._light.color})[0]

    @property
    def max_color_temp_kelvin(self) -> int:
        return configured_color_temperature_range({"color": self._light.color})[1]

    @property
    def _raw(self) -> list[int]:
        return [raw_value(self._state, self._light, channel) or 0
                for channel in self._light.channels]

    @property
    def brightness(self) -> int | None:
        if "raw" in self._state:
            value = max(self._raw, default=0) * 255 / RAW_MAX
        else:
            value = self._state.get("hsv", {}).get("v", 0) * 255 / 100
        return max(1, round(value)) if value > 0 else 0

    @property
    def is_on(self) -> bool | None:
        return (self.brightness or 0) > 0

    @property
    def hs_color(self) -> tuple[float, float]:
        if "hsv" in self._state:
            hsv = self._state["hsv"]
            return float(hsv["h"]), float(hsv["s"])
        if not self._light.named_raw:
            return 0, 0
        r, g, b = self._raw[:3]
        h, s, _v = colorsys.rgb_to_hsv(r / RAW_MAX, g / RAW_MAX, b / RAW_MAX)
        return h * 360, s * 100

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._state.get("hsv", {}).get("ct") or None

    @property
    def color_mode(self) -> ColorMode:
        modes = self.supported_color_modes
        if len(modes) == 1:
            return next(iter(modes))
        if "raw" in self._state:
            return ColorMode.RGBWW
        return ColorMode.COLOR_TEMP if self.color_temp_kelvin else ColorMode.HS

    def _rgb_white(self, channels: tuple[str, ...]) -> tuple[int, ...]:
        if "raw" in self._state:
            values = [raw_value(self._state, self._light, channel) or 0 for channel in channels]
            peak = max(values, default=0)
            return tuple(round(value * 255 / peak) if peak else 0 for value in values)
        h, s = self.hs_color
        return (*[round(value * 255) for value in colorsys.hsv_to_rgb(h / 360, s / 100, 1)],
                *([0] * (len(channels) - 3)))

    @property
    def rgbw_color(self) -> tuple[int, int, int, int]:
        r, g, b, w = self._rgb_white(self._light.channels)
        return r, g, b, w

    @property
    def rgbww_color(self) -> tuple[int, int, int, int, int]:
        r, g, b, cw, ww = self._rgb_white(("r", "g", "b", "cw", "ww"))
        return r, g, b, cw, ww

    def _sync_firmware_favorite_colors(self) -> None:
        if self._light.type.startswith("RGB"):
            super()._sync_firmware_favorite_colors()

    def _handle_coordinator_update(self) -> None:
        if "raw" in self._state and any(self._raw):
            self._last_on_raw = self._raw
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        previous_brightness = round(max(self._last_on_raw, default=0) * 255 / RAW_MAX)
        brightness = kwargs.get(ATTR_BRIGHTNESS, self.brightness or previous_brightness or 255)
        ramp = round(kwargs.get(ATTR_TRANSITION, 0.5) * 1000)
        payload: dict[str, Any] = {"cmd": "fade", "t": ramp}
        if len(self._light.channels) == 1:
            payload.update(channel_command(
                self._light, self._light.channels[0], round(brightness * RAW_MAX / 255)
            ))
        elif ATTR_RGBW_COLOR in kwargs or ATTR_RGBWW_COLOR in kwargs:
            channels = self._light.channels
            if ATTR_RGBWW_COLOR in kwargs:
                channels = ("r", "g", "b", "cw", "ww")
                values = kwargs[ATTR_RGBWW_COLOR]
            else:
                values = kwargs[ATTR_RGBW_COLOR]
            payload["raw"] = {
                channel: round(value * brightness * RAW_MAX / (255 * 255))
                for channel, value in zip(channels, values, strict=True)
            }
        elif "raw" in self._state and not (
            ATTR_HS_COLOR in kwargs or ATTR_COLOR_TEMP_KELVIN in kwargs
        ):
            current_raw = self._raw
            if not any(current_raw) and len(self._last_on_raw) == len(current_raw):
                current_raw = self._last_on_raw
            peak = max(current_raw, default=0)
            values = [round(value / peak * brightness * RAW_MAX / 255) if peak
                      else round(brightness * RAW_MAX / 255) for value in current_raw]
            payload["raw"] = (
                dict(zip(self._light.channels, values, strict=True))
                if self._light.named_raw else values
            )
        else:
            hsv = dict(self._state.get("hsv", {"h": 0, "s": 0, "ct": 0}))
            hsv["v"] = brightness * 100 / 255
            if ATTR_HS_COLOR in kwargs:
                hsv["h"], hsv["s"] = kwargs[ATTR_HS_COLOR]
                hsv["ct"] = 0
            if ATTR_COLOR_TEMP_KELVIN in kwargs:
                hsv["ct"] = kwargs[ATTR_COLOR_TEMP_KELVIN]
                hsv["s"] = 0
            payload["hsv"] = hsv
        await self.coordinator.async_light_command(self._light_id, payload)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if "raw" in self._state and any(self._raw):
            self._last_on_raw = self._raw
        if "raw" in self._state or len(self._light.channels) == 1:
            raw: dict[str, int] | list[int] = (
                dict.fromkeys(self._light.channels, 0) if self._light.named_raw
                else [0] * len(self._light.channels)
            )
            color: dict[str, Any] = {"raw": raw}
        else:
            color = {"hsv": {**self._state.get("hsv", {}), "v": 0}}
        await self.coordinator.async_light_command(self._light_id, {
            "cmd": "fade", "t": round(kwargs.get(ATTR_TRANSITION, 0.5) * 1000), **color,
        })


class LightinatorVirtualChannelLight(LightinatorVirtualLight):
    """Optional raw-channel controls sharing the primary light's device."""

    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: LightinatorCoordinator, entry: ConfigEntry,
        light: VirtualLight, channel: str, *, legacy: bool = False,
    ) -> None:
        super().__init__(coordinator, entry, light)
        self._channel = channel
        self._attr_unique_id = (
            f"{entry.data[CONF_CHIP_ID]}_{channel}" if legacy
            else f"{entry.data[CONF_CHIP_ID]}_{light.id}_{channel}"
        )

    @property
    def name(self) -> str:
        return f"{self._light.name} {CHANNEL_LABELS[self._channel]}"

    @property
    def available(self) -> bool:
        return super().available and self._channel in self._light.channels

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {ColorMode.BRIGHTNESS}

    @property
    def supported_features(self) -> LightEntityFeature:
        return LightEntityFeature(0)

    @property
    def brightness(self) -> int | None:
        value = raw_value(self._state, self._light, self._channel)
        if value is None:
            return None
        return max(1, round(value * 255 / RAW_MAX)) if value > 0 else 0

    @property
    def is_on(self) -> bool | None:
        value = self.brightness
        return value > 0 if value is not None else None

    def _sync_firmware_favorite_colors(self) -> None:
        return

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_light_command(self._light_id, channel_command(
            self._light, self._channel,
            round(kwargs.get(ATTR_BRIGHTNESS, 255) * RAW_MAX / 255),
        ))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_light_command(
            self._light_id, channel_command(self._light, self._channel, 0)
        )
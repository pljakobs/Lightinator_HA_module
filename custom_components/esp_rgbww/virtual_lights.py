"""Firmware virtual-light identities and wire color conversion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CHANNELS = {
    "RGB": ("r", "g", "b"),
    "RGBWW": ("r", "g", "b", "ww"),
    "RGBCW": ("r", "g", "b", "cw"),
    "RGBWWCW": ("r", "g", "b", "ww", "cw"),
    "WWCW": ("ww", "cw"),
    "WW": ("ww",),
    "CW": ("cw",),
    "SINGLE": ("output",),
}
CHANNEL_LABELS = {
    "r": "Red", "g": "Green", "b": "Blue",
    "ww": "Warm White", "cw": "Cool White", "output": "Output",
}
CONF_LEGACY_LIGHT_ID = "legacy_light_id"


@dataclass(frozen=True)
class VirtualLight:
    """Persistent UUID, with a separately resolved firmware API index."""

    id: str
    index: int
    name: str
    type: str
    is_default: bool
    color: dict[str, Any]

    @property
    def channels(self) -> tuple[str, ...]:
        return CHANNELS[self.type]

    @property
    def named_raw(self) -> bool:
        return self.type.startswith("RGB")


def discover_lights(
    config: dict[str, Any], version: str
) -> dict[str, VirtualLight] | None:
    """Return None only for legacy firmware, not for broken 5.1 discovery."""
    if version.lstrip("Vv").startswith("5.0"):
        return None
    hardware = config.get("hardware", {})
    configuration = hardware.get("configuration", {}) if isinstance(hardware, dict) else {}
    lights = configuration.get("lights") if isinstance(configuration, dict) else None
    if lights is None and not version.lstrip("Vv").startswith("5.1"):
        return None
    if not isinstance(lights, list):
        raise ValueError("Virtual-light configuration is unavailable")
    result: dict[str, VirtualLight] = {}
    indexes: set[int] = set()
    for light in lights:
        if not isinstance(light, dict):
            raise ValueError("Invalid virtual-light configuration")
        light_id, index, light_type = light.get("id"), light.get("index"), light.get("type")
        if (
            not isinstance(light_id, str) or not light_id or light_id in result
            or not isinstance(index, int) or isinstance(index, bool)
            or not 0 <= index < 65535 or index in indexes
            or not isinstance(light_type, str) or light_type not in CHANNELS
            or not isinstance(light.get("color", {}), dict)
        ):
            raise ValueError("Invalid or duplicate virtual-light identity/type")
        result[light_id] = VirtualLight(
            light_id, index, str(light.get("name") or light_id),
            light_type, light.get("is_default") is True,
            light.get("color", {}),
        )
        indexes.add(index)
    if result and sum(light.is_default for light in result.values()) != 1:
        raise ValueError("Virtual lights must have exactly one default")
    return result


def decode_color(color: dict[str, Any]) -> dict[str, Any]:
    """Decode 5.1 HSV degrees/percent or RAW state without changing units."""
    if not isinstance(color, dict):
        raise ValueError("Invalid light state")
    hsv = color.get("hsv")
    if isinstance(hsv, dict):
        if any(not isinstance(hsv.get(key, 0), (int, float))
               or isinstance(hsv.get(key, 0), bool) for key in ("h", "s", "v", "ct")):
            raise ValueError("Invalid HSV light state")
        return {"hsv": {
            "h": hsv.get("h", 0),
            "s": hsv.get("s", 0),
            "v": hsv.get("v", 0),
            "ct": hsv.get("ct", 0),
        }}
    raw = color.get("raw")
    if isinstance(raw, (dict, list)):
        return {"raw": raw}
    raise ValueError("Controller returned no HSV or RAW light state")


def raw_value(state: dict[str, Any], light: VirtualLight, channel: str) -> int | None:
    """Read a channel only when the firmware reports measured RAW state."""
    raw = state.get("raw")
    if channel not in light.channels:
        return None
    value = None
    if isinstance(raw, dict):
        value = raw.get(channel)
    elif isinstance(raw, list):
        index = light.channels.index(channel)
        if index < len(raw):
            value = raw[index]
    return int(value) if isinstance(value, (int, float)) else None


def channel_command(light: VirtualLight, channel: str, value: int) -> dict[str, Any]:
    """Leave every other channel unchanged, including indexed white channels."""
    if channel not in light.channels:
        raise ValueError(f"Light {light.id} no longer has channel {channel}")
    if light.named_raw:
        return {"raw": {channel: value}}
    values: list[int | None] = [None] * len(light.channels)
    values[light.channels.index(channel)] = value
    return {"raw": values}

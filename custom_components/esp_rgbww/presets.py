"""Helpers for turning firmware data presets into light commands."""

from __future__ import annotations

from typing import Any

from homeassistant.util.color import color_temperature_to_rgb

_COLOR_FIELDS = {
    "hsv": ("h", "s", "v", "ct"),
    "raw": ("r", "g", "b", "ww", "cw"),
}
_COMMAND_FIELDS = ("s", "r", "d", "q", "channels")


def _preset_color(
    preset: dict[str, Any],
    presets: list[dict[str, Any]],
    seen_ids: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    if preset.get("raw_channels"):
        return None
    color = preset.get("color")
    if not isinstance(color, dict):
        color = preset

    containers = [color]
    if color is not preset:
        containers.append(preset)

    for container in containers:
        for mode, fields in _COLOR_FIELDS.items():
            nested = container.get(mode)
            if isinstance(nested, dict) and nested:
                return mode, nested, container

        for mode, fields in _COLOR_FIELDS.items():
            direct = {key: container[key] for key in fields if key in container}
            if direct:
                return mode, direct, container

    reference = color.get("Preset", color)
    preset_id = reference.get("id") if isinstance(reference, dict) else None
    if not isinstance(preset_id, str) or preset_id in seen_ids:
        return None

    referenced = next(
        (
            item
            for item in presets
            if isinstance(item, dict) and item.get("id") == preset_id
        ),
        None,
    )
    if not isinstance(referenced, dict):
        return None

    resolved = _preset_color(referenced, presets, seen_ids | {preset_id})
    if resolved is None:
        return None
    mode, values, options = resolved
    alias_options = {
        key: value for key, value in color.items() if key not in {"id", "Preset"}
    }
    return mode, values, {**options, **alias_options}


def preset_names(presets: list[dict[str, Any]]) -> list[str]:
    """Return names for presets with a resolvable HSV or raw color."""
    return [
        str(preset["name"])
        for preset in presets
        if isinstance(preset, dict)
        and isinstance(preset.get("name"), str)
        and preset["name"]
        and not preset.get("raw_channels")
        and _preset_color(preset, presets) is not None
    ]


def favorite_preset_colors(
    presets: list[dict[str, Any]], config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Convert favorite firmware presets to Home Assistant light swatches."""
    favorites: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()

    for preset in presets:
        if not isinstance(preset, dict) or preset.get("favorite") is not True:
            continue
        resolved = _preset_color(preset, presets)
        if resolved is None:
            continue

        mode, color, _options = resolved
        favorite: dict[str, Any] | None = None
        if mode == "hsv":
            color_temp = color.get("ct")
            if isinstance(color_temp, (int, float)) and not isinstance(color_temp, bool) and color_temp > 0:
                favorite = {"color_temp_kelvin": round(color_temp)}
            else:
                hue = color.get("h")
                saturation = color.get("s")
                if (
                    isinstance(hue, (int, float))
                    and not isinstance(hue, bool)
                    and isinstance(saturation, (int, float))
                    and not isinstance(saturation, bool)
                ):
                    favorite = {"hs_color": [float(hue), float(saturation)]}
        elif mode == "raw":
            rgb = [color.get(channel, 0) for channel in ("r", "g", "b")]
            has_rgb = any(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
                for value in rgb
            )
            white_kelvin = _raw_white_kelvin(color, config or {})
            if has_rgb:
                favorite = {
                    "rgb_color": _add_white_to_rgb(
                        [
                        round(max(0, min(1023, value)) * 255 / 1023)
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                        else 0
                        for value in rgb
                        ],
                        white_kelvin,
                        color,
                    )
                }
            elif white_kelvin is not None:
                favorite = {"color_temp_kelvin": white_kelvin}

        if favorite is None:
            continue
        signature = tuple(
            (key, tuple(value) if isinstance(value, list) else value)
            for key, value in favorite.items()
        )
        if signature not in seen:
            seen.add(signature)
            favorites.append(favorite)

    return favorites


def _raw_white_kelvin(
    color: dict[str, Any], config: dict[str, Any]
) -> int | None:
    warm = color.get("ww", 0)
    cool = color.get("cw", 0)
    if not isinstance(warm, (int, float)) or isinstance(warm, bool):
        warm = 0
    if not isinstance(cool, (int, float)) or isinstance(cool, bool):
        cool = 0
    if warm <= 0 and cool <= 0:
        return None

    color_config = config.get("color", {})
    colortemp = color_config.get("colortemp", {}) if isinstance(color_config, dict) else {}
    colortemp = colortemp if isinstance(colortemp, dict) else {}
    warm_kelvin = colortemp.get("ww", 2700)
    cool_kelvin = colortemp.get("cw", 6000)
    if (
        not isinstance(warm_kelvin, (int, float))
        or isinstance(warm_kelvin, bool)
        or warm_kelvin <= 0
    ):
        warm_kelvin = 2700
    if (
        not isinstance(cool_kelvin, (int, float))
        or isinstance(cool_kelvin, bool)
        or cool_kelvin <= 0
    ):
        cool_kelvin = 6000

    total = warm + cool
    mired = (warm * 1_000_000 / warm_kelvin + cool * 1_000_000 / cool_kelvin) / total
    return round(1_000_000 / mired)


def _add_white_to_rgb(
    rgb: list[int], white_kelvin: int | None, color: dict[str, Any]
) -> list[int]:
    if white_kelvin is None:
        return rgb

    warm = color.get("ww", 0)
    cool = color.get("cw", 0)
    white_level = max(0, min(1, (warm + cool) / 1023))
    white_rgb = color_temperature_to_rgb(white_kelvin)
    return [
        min(255, round(channel + white * white_level))
        for channel, white in zip(rgb, white_rgb, strict=True)
    ]


def preset_command(
    preset: dict[str, Any],
    presets: list[dict[str, Any]],
    *,
    default_command: str,
    default_transition_ms: int | None = None,
    transition_ms: int | None = None,
    light_type: str | None = None,
) -> dict[str, Any] | None:
    """Build the firmware color command for one data preset."""
    indexed = preset.get("raw_channels")
    if isinstance(indexed, list) and indexed:
        from .virtual_lights import CHANNELS
        if (
            light_type != preset.get("light_type")
            or len(indexed) != len(CHANNELS.get(light_type, ()))
            or any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1023
                   for value in indexed)
        ):
            raise ValueError("This RAW preset requires a matching light profile")
        return {"cmd": default_command, "raw": list(indexed)}
    resolved = _preset_color(preset, presets)
    if resolved is None:
        return None

    mode, color, options = resolved
    if mode == "raw" and light_type is not None and not light_type.startswith("RGB"):
        from .virtual_lights import CHANNELS
        channels = CHANNELS.get(light_type)
        if not channels or "output" in channels:
            raise ValueError("Named RAW preset is incompatible with this light")
        color = [color.get(channel, 0) for channel in channels]
    command: dict[str, Any] = {
        "cmd": options.get("cmd") or default_command,
        mode: color,
    }
    for key in _COMMAND_FIELDS:
        if key in options:
            command[key] = options[key]

    if transition_ms is not None:
        command["t"] = transition_ms
    elif "t" in options:
        command["t"] = options["t"]
    elif default_transition_ms is not None:
        command["t"] = default_transition_ms

    return command


def is_light_favorite(preset: dict[str, Any], light: Any = None) -> bool:
    """Keep 5.0 favorites; explicit UUID overrides never leak to other lights."""
    if light is None:
        return preset.get("favorite") is True
    for override in preset.get("light_favorites", []):
        if isinstance(override, dict) and override.get("light_id") == light.id:
            return override.get("favorite") is True
    return light.is_default and preset.get("favorite") is True


def with_light_favorite(
    preset: dict[str, Any], light: Any, favorite: bool
) -> dict[str, Any]:
    """Update one light's membership, retaining all other preset data."""
    if light is None:
        return {**preset, "favorite": favorite}
    return {**preset, "light_favorites": [
        *(item for item in preset.get("light_favorites", [])
          if item.get("light_id") != light.id),
        {"light_id": light.id, "favorite": favorite},
    ]}


def light_preset_swatches(
    presets: list[dict[str, Any]], light: Any = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Dashboard descriptors use IDs, not lossy color translations, when applied."""
    result = []
    for preset in presets:
        if not isinstance(preset, dict) or not preset.get("id") or not is_light_favorite(preset, light):
            continue
        try:
            command = preset_command(
                preset, presets, default_command="solid",
                light_type=light.type if light else None,
            )
        except ValueError:
            continue
        if command is None:
            continue
        import colorsys
        color = command.get("hsv")
        if isinstance(color, dict):
            h, s, v = (float(color.get(key, 0)) for key in ("h", "s", "v"))
            rgb = [round(channel * 255) for channel in colorsys.hsv_to_rgb(h / 360, s / 100, v / 100)]
            if color.get("ct", 0) > 0 and s == 0:
                rgb = [round(channel * v / 100) for channel in color_temperature_to_rgb(color["ct"])]
        else:
            raw = command.get("raw")
            if isinstance(raw, list):
                level = max(raw, default=0) / 1023
                rgb = [round(level * 255)] * 3
            else:
                rgb = [round(raw.get(channel, 0) * 255 / 1023) for channel in ("r", "g", "b")]
                rgb = _add_white_to_rgb(rgb, _raw_white_kelvin(raw, config or {}), raw)
        result.append({
            "id": preset["id"], "name": str(preset.get("name") or preset["id"]),
            "rgb": [max(0, min(255, value)) for value in rgb],
        })
    return result
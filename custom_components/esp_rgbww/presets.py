"""Helpers for turning firmware data presets into light commands."""

from __future__ import annotations

from typing import Any

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

    preset_id = color.get("id")
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
        key: value for key, value in color.items() if key != "id"
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
        and _preset_color(preset, presets) is not None
    ]


def preset_command(
    preset: dict[str, Any],
    presets: list[dict[str, Any]],
    *,
    default_command: str,
    default_transition_ms: int | None = None,
    transition_ms: int | None = None,
) -> dict[str, Any] | None:
    """Build the firmware color command for one data preset."""
    resolved = _preset_color(preset, presets)
    if resolved is None:
        return None

    mode, color, options = resolved
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
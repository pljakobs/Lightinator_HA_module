"""Pure helpers for interpreting optional extension readings."""

from __future__ import annotations

import math
from typing import Any


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def sensor_reading_value(extension: dict[str, Any], extension_id: str, key: str) -> Any:
    """Return a reading field only when the firmware marks the sample valid."""
    reading = extension.get("reading")
    if key == "error":
        sensor_error = reading.get("error") if isinstance(reading, dict) else None
        return sensor_error or extension.get("error") or None
    if not isinstance(reading, dict):
        return None
    if extension_id == "spectrum-1" and key == "state":
        return reading.get("state")
    if reading.get("valid") is not True:
        return None
    if key == "age_ms":
        return _finite_number(reading.get(key))
    values = reading.get("data", {}) if extension_id == "spectrum-1" else reading
    if not isinstance(values, dict):
        return None
    return _finite_number(values.get(key))

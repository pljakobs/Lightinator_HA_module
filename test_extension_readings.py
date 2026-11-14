"""Tests for optional sensor-extension readings."""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "extension_readings",
    Path(__file__).parent
    / "custom_components/esp_rgbww/extension_readings.py",
)
readings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readings)


class ExtensionReadingTests(unittest.TestCase):
    def test_current_values_are_read_only_from_valid_sample(self):
        extension = {
            "reading": {
                "valid": True,
                "current_a": 1.25,
                "voltage_v": 12.0,
                "power_w": 15.0,
            }
        }
        self.assertEqual(
            readings.sensor_reading_value(extension, "current-1", "current_a"),
            1.25,
        )
        self.assertEqual(
            readings.sensor_reading_value(extension, "current-1", "voltage_v"),
            12.0,
        )

    def test_spectral_adc_fields_are_read_from_data(self):
        extension = {
            "reading": {
                "valid": True,
                "state": "complete",
                "age_ms": 42,
                "data": {
                    "f1": 123,
                    "clear_low": 456,
                    "nir_high": 789,
                    "gain": 4,
                },
            }
        }
        self.assertEqual(
            readings.sensor_reading_value(extension, "spectrum-1", "f1"), 123
        )
        self.assertEqual(
            readings.sensor_reading_value(extension, "spectrum-1", "clear_low"),
            456,
        )
        self.assertEqual(
            readings.sensor_reading_value(extension, "spectrum-1", "state"),
            "complete",
        )
        self.assertEqual(
            readings.sensor_reading_value(extension, "spectrum-1", "age_ms"), 42
        )
        self.assertEqual(
            readings.sensor_reading_value(extension, "spectrum-1", "gain"), 4
        )

    def test_invalid_or_missing_readings_never_become_zero(self):
        for extension in (
            {"reading": {"valid": False, "current_a": 0}},
            {"reading": {"valid": True, "current_a": 0}},
            {"reading": None},
            {},
        ):
            reading = extension.get("reading") or {}
            expected = 0 if reading.get("valid") is True else None
            self.assertEqual(
                readings.sensor_reading_value(extension, "current-1", "current_a"),
                expected,
            )

    def test_real_firmware_and_transport_errors_are_preserved(self):
        self.assertEqual(
            readings.sensor_reading_value(
                {"reading": {"valid": False, "error": "sensor timeout"}},
                "spectrum-1",
                "error",
            ),
            "sensor timeout",
        )
        self.assertEqual(
            readings.sensor_reading_value(
                {"reading": None, "error": "HTTP 503"},
                "current-1",
                "error",
            ),
            "HTTP 503",
        )

    def test_nonfinite_values_never_become_sensor_readings(self):
        for value in (float("nan"), float("inf"), float("-inf"), True, "12"):
            for extension_id, key, reading in (
                ("current-1", "current_a", {"current_a": value}),
                ("spectrum-1", "f1", {"data": {"f1": value}}),
                ("spectrum-1", "age_ms", {"age_ms": value}),
            ):
                with self.subTest(value=value, extension_id=extension_id, key=key):
                    self.assertIsNone(
                        readings.sensor_reading_value(
                            {"reading": {"valid": True, **reading}}, extension_id, key
                        )
                    )


if __name__ == "__main__":
    unittest.main()

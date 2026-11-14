"""Exercise the real extension coordinator against an authenticated HTTP server.

Only the HA scheduling base is stubbed, so these tests also run outside HA.
"""

import copy
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer


class CoordinatorBase:
    """Minimal HA scheduler substitute; refresh executes the real poll."""

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, *args, **kwargs):
        self.data = None

    async def async_request_refresh(self):
        self.data = await self._async_update_data()


def load_coordinator():
    modules = {}
    for name, attributes in {
        "homeassistant": {},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.const": {
            "CONF_HOST": "host", "CONF_PORT": "port", "CONF_PASSWORD": "password"
        },
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.helpers": {},
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": CoordinatorBase
        },
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module
    spec = importlib.util.spec_from_file_location(
        "tested_extension_coordinator",
        Path(__file__).parent / "custom_components/esp_rgbww/extension_coordinator.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


extensions = load_coordinator()


class ExtensionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.configs = {
            "current-1": {
                "extension_id": "current-1", "enabled": True, "available": True,
                "address": 64, "shunt_ohms": 0.1, "interval_ms": 1000,
            },
            "spectrum-1": {
                "extension_id": "spectrum-1", "enabled": True, "available": True,
                "atime": 29, "astep": 599, "gain": 4, "interval_ms": 1000,
            },
        }
        self.readings = {
            "current-1": {"valid": True, "current_a": 1.25, "error": ""},
            "spectrum-1": {
                "valid": True, "state": "complete", "error": "",
                "data": {"f1": 123},
            },
        }
        self.overrides = {}
        self.requests = []
        app = web.Application()
        app.router.add_route("*", "/extensions/{extension_id}/{endpoint}", self.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        entry = SimpleNamespace(data={
            "host": self.server.host, "port": self.server.port, "password": "test-secret",
        })
        self.coordinator = extensions.ExtensionCoordinator(None, entry, self.session)

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def handle(self, request):
        payload = await request.json() if request.method == "POST" else None
        self.requests.append((request.method, request.path, payload, request.headers.get("Authorization")))
        if request.headers.get("Authorization") != aiohttp.BasicAuth("", "test-secret").encode():
            return web.json_response({"error": "authentication required"}, status=401)
        override = self.overrides.get((request.method, request.path))
        if override is not None:
            status, body = override
            if isinstance(body, (dict, list)):
                return web.json_response(body, status=status)
            return web.Response(text=body, status=status)
        extension_id = request.match_info["extension_id"]
        endpoint = request.match_info["endpoint"]
        if endpoint == "config":
            if request.method == "POST":
                writable = set(self.configs[extension_id]) - {"extension_id", "available"}
                if not payload.keys() <= writable:
                    return web.json_response({"error": "unknown fields"}, status=400)
                self.configs[extension_id].update(payload)
            return web.json_response(self.configs[extension_id])
        if endpoint == "readings":
            config = self.configs[extension_id]
            if not config["enabled"] or not config["available"]:
                return web.json_response({"error": "sensor unavailable"}, status=404)
            return web.json_response(self.readings[extension_id])
        if endpoint == "start_measurement":
            return web.Response(status=202)
        return web.Response(status=404)

    async def test_partial_posts_preserve_unrelated_settings_and_authentication(self):
        await self.coordinator.async_request_refresh()
        updates = (
            ("current-1", {"enabled": False}),
            ("current-1", {"address": 79}),
            ("current-1", {"shunt_ohms": 0.05}),
            ("current-1", {"interval_ms": 200}),
            ("spectrum-1", {"enabled": False}),
            ("spectrum-1", {"atime": 255}),
            ("spectrum-1", {"astep": 65535}),
            ("spectrum-1", {"gain": 10}),
            ("spectrum-1", {"interval_ms": 3600000}),
        )
        for extension_id, update in updates:
            with self.subTest(extension_id=extension_id, update=update):
                before = copy.deepcopy(self.configs[extension_id])
                await self.coordinator.async_update_extension_config(extension_id, update)
                posts = [call for call in self.requests if call[0] == "POST"]
                self.assertEqual(posts[-1][:3], (
                    "POST", f"/extensions/{extension_id}/config", update
                ))
                self.assertEqual(self.configs[extension_id], {**before, **update})
                self.assertEqual(self.coordinator.configs[extension_id], {**before, **update})
        self.assertTrue(all(
            call[3] == aiohttp.BasicAuth("", "test-secret").encode()
            for call in self.requests
        ))

    async def test_readonly_fields_are_rejected_without_posting(self):
        for update in ({"extension_id": "current-1"}, {"available": True}):
            with self.assertRaisesRegex(ValueError, "read-only"):
                await self.coordinator.async_update_extension_config("current-1", update)
        self.assertEqual(self.requests, [])

    async def test_readback_verification_rejects_unpersisted_changes(self):
        self.overrides[("POST", "/extensions/current-1/config")] = (200, {})
        with self.assertRaisesRegex(ValueError, "did not persist current-1 address"):
            await self.coordinator.async_update_extension_config("current-1", {"address": 79})
        self.assertEqual(self.configs["current-1"]["address"], 64)

    async def test_optional_config_404_does_not_fail_other_extension(self):
        await self.coordinator.async_request_refresh()
        self.overrides[("GET", "/extensions/current-1/config")] = (404, {"error": "not built"})
        await self.coordinator.async_request_refresh()
        self.assertFalse(self.coordinator.supported["current-1"])
        self.assertNotIn("current-1", self.coordinator.configs)
        self.assertIsNone(self.coordinator.data["current"]["error"])
        self.assertEqual(self.coordinator.data["spectrum"]["reading"]["data"]["f1"], 123)

    async def test_invalid_config_identity_does_not_report_extension_capability(self):
        await self.coordinator.async_request_refresh()
        self.overrides[("GET", "/extensions/current-1/config")] = (
            200, {**self.configs["current-1"], "extension_id": "spectrum-1"}
        )
        await self.coordinator.async_request_refresh()
        self.assertFalse(self.coordinator.supported["current-1"])
        self.assertNotIn("current-1", self.coordinator.configs)
        with self.assertRaisesRegex(ValueError, "capability has not been reported"):
            await self.coordinator.async_update_extension_config(
                "current-1", {"enabled": False}
            )

    async def test_readings_404_clears_old_sample_but_retains_configuration(self):
        await self.coordinator.async_request_refresh()
        self.overrides[("GET", "/extensions/current-1/readings")] = (404, {"error": "disconnected"})
        await self.coordinator.async_request_refresh()
        self.assertTrue(self.coordinator.supported["current-1"])
        self.assertIn("current-1", self.coordinator.configs)
        self.assertIsNone(self.coordinator.data["current"]["reading"])
        self.assertFalse(self.coordinator.data["current"]["readings_available"])

    async def test_disabled_or_disconnected_sensors_skip_readings(self):
        self.configs["current-1"]["enabled"] = False
        self.configs["spectrum-1"]["available"] = False
        await self.coordinator.async_request_refresh()
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(call[1].endswith("/config") for call in self.requests))
        self.assertIn("current-1", self.coordinator.configs)
        self.assertIn("spectrum-1", self.coordinator.configs)

    async def test_poll_errors_are_logged_and_retained_when_config_is_disabled(self):
        self.configs["current-1"]["enabled"] = False
        await self.coordinator.async_request_refresh()
        self.overrides[("GET", "/extensions/current-1/config")] = (503, {"error": "config storage failed"})
        with self.assertLogs(extensions._LOGGER, level="WARNING") as logs:
            await self.coordinator.async_request_refresh()
        self.assertIn("config storage failed", self.coordinator.data["current"]["error"])
        self.assertTrue(any("config storage failed" in line for line in logs.output))
        self.assertFalse(self.coordinator.configs["current-1"]["enabled"])

    async def test_uncached_config_error_and_reading_error_do_not_hide_failures(self):
        self.overrides[("GET", "/extensions/current-1/config")] = (401, {"error": "access denied"})
        self.overrides[("GET", "/extensions/spectrum-1/readings")] = (500, "I2C bus error")
        with self.assertLogs(extensions._LOGGER, level="WARNING") as logs:
            await self.coordinator.async_request_refresh()
        self.assertTrue(self.coordinator.supported["current-1"])
        self.assertIn("access denied", self.coordinator.data["current"]["error"])
        self.assertIn("I2C bus error", self.coordinator.data["spectrum"]["error"])
        self.assertEqual(len(logs.output), 2)

    async def test_command_errors_preserve_firmware_message_and_busy_status(self):
        self.overrides[("POST", "/extensions/current-1/config")] = (400, {"error": "invalid address"})
        with self.assertRaises(aiohttp.ClientResponseError) as caught:
            await self.coordinator.async_update_extension_config("current-1", {"address": 79})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.message, "invalid address")
        self.overrides[("POST", "/extensions/spectrum-1/start_measurement")] = (409, {"error": "busy"})
        with self.assertRaises(aiohttp.ClientResponseError) as caught:
            await self.coordinator.async_start_spectrum_measurement()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.message, "busy")

    async def test_connection_failures_are_logged_and_clear_cached_readings(self):
        await self.coordinator.async_request_refresh()
        await self.server.close()
        with self.assertLogs(extensions._LOGGER, level="WARNING") as logs:
            await self.coordinator.async_request_refresh()
        self.assertEqual(len(logs.output), 2)
        for name in ("current", "spectrum"):
            self.assertIsNone(self.coordinator.data[name]["reading"])
            self.assertIsNotNone(self.coordinator.data[name]["error"])

    async def test_malformed_config_and_readings_are_logged(self):
        self.overrides[("GET", "/extensions/current-1/config")] = (200, [])
        self.overrides[("GET", "/extensions/spectrum-1/readings")] = (200, "not a reading")
        with self.assertLogs(extensions._LOGGER, level="WARNING") as logs:
            await self.coordinator.async_request_refresh()
        self.assertEqual(
            self.coordinator.data["current"]["error"],
            "Invalid extension configuration response",
        )
        self.assertEqual(
            self.coordinator.data["spectrum"]["error"],
            "Invalid extension readings response",
        )
        self.assertEqual(len(logs.output), 2)

    async def test_measurement_accepts_empty_202_and_refreshes(self):
        await self.coordinator.async_start_spectrum_measurement()
        self.assertEqual(self.requests[0][:3], (
            "POST", "/extensions/spectrum-1/start_measurement", {}
        ))
        self.assertEqual(self.coordinator.data["spectrum"]["reading"]["state"], "complete")


if __name__ == "__main__":
    unittest.main()

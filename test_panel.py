"""Tests for the admin-only Lightinator controller overview."""

import tempfile
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized

from custom_components.esp_rgbww.panel import (
    async_setup_panel,
    controller_overview,
    websocket_controllers,
)


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.entry = SimpleNamespace(
            entry_id="test", title="Controller",
            data={"host": "controller.local", "password": "do-not-export"},
        )
        self.hass.config_entries = MagicMock()
        self.hass.config_entries.async_entries.return_value = [self.entry]
        self.coordinator = SimpleNamespace(
            data={"info": {"app": {"git_version": "V5.1.0"}, "device": {"soc": "esp32c3"}}},
            virtual_lights={}, last_update_success=True,
            async_get_config=AsyncMock(return_value={
                "network": {"password": "do-not-export"},
                "hardware": {
                    "pwm": {"timer": {"frequency": 8000}},
                    "configuration": {"board_id": "mini", "lights": [
                        {"id": "uuid", "name": "<script>unsafe</script>", "index": 3,
                         "type": "SINGLE", "pins": [4], "is_default": True,
                         "color": {"private": "do-not-export"}}
                    ]},
                },
            }),
        )
        self.hass.data["esp_rgbww"] = {"test": self.coordinator}

    async def test_list_does_not_fetch_config_or_expose_secrets(self):
        result = await controller_overview(self.hass)
        self.assertEqual(result[0]["name"], "Controller")
        self.assertTrue(result[0]["virtual"])
        self.assertNotIn("do-not-export", str(result))
        self.coordinator.async_get_config.assert_not_awaited()

    async def test_detail_whitelists_light_inventory(self):
        result = (await controller_overview(self.hass, "test"))[0]
        self.assertEqual(result["board"], "mini")
        self.assertEqual(result["lights"][0]["pins"], [4])
        self.assertNotIn("color", result["lights"][0])
        self.assertNotIn("do-not-export", str(result))
        self.coordinator.async_get_config.assert_awaited_once_with(force_refresh=True)

    async def test_unloaded_unavailable_and_legacy_entries(self):
        self.coordinator.last_update_success = False
        self.coordinator.virtual_lights = None
        result = (await controller_overview(self.hass))[0]
        self.assertFalse(result["virtual"])
        self.assertFalse(result["available"])
        self.hass.data["esp_rgbww"].clear()
        result = (await controller_overview(self.hass, "test"))[0]
        self.assertFalse(result["loaded"])
        self.assertNotIn("lights", result)

    async def test_unknown_controller_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            await controller_overview(self.hass, "missing")

    async def test_websocket_rejects_non_admin(self):
        connection = MagicMock()
        connection.user.is_admin = False
        with self.assertRaises(Unauthorized):
            websocket_controllers(
                self.hass, connection, {"id": 1, "type": "esp_rgbww/controllers"}
            )
        connection.send_result.assert_not_called()

    async def test_websocket_reports_controller_failure(self):
        connection = MagicMock()
        self.coordinator.async_get_config.side_effect = TimeoutError("Controller timed out")
        await inspect.unwrap(websocket_controllers)(
            self.hass, connection,
            {"id": 2, "type": "esp_rgbww/controllers", "entry_id": "test"},
        )
        connection.send_error.assert_called_once_with(
            2, "controller_unavailable", "Controller timed out"
        )
        connection.send_result.assert_not_called()

    async def test_websocket_sends_controller_list(self):
        connection = MagicMock()
        await inspect.unwrap(websocket_controllers)(
            self.hass, connection, {"id": 3, "type": "esp_rgbww/controllers"}
        )
        self.assertEqual(connection.send_result.call_args.args[0], 3)
        self.assertEqual(connection.send_result.call_args.args[1][0]["entry_id"], "test")

    async def test_registers_admin_panel_and_static_assets(self):
        self.hass.http = MagicMock()
        self.hass.http.async_register_static_paths = AsyncMock()
        with (
            patch("custom_components.esp_rgbww.panel.panel_custom.async_register_panel", new_callable=AsyncMock) as register,
            patch("custom_components.esp_rgbww.panel.websocket_api.async_register_command"),
        ):
            await async_setup_panel(self.hass)
        self.assertTrue(register.call_args.kwargs["require_admin"])
        self.assertEqual(register.call_args.kwargs["frontend_url_path"], "lightinator")
        self.hass.http.async_register_static_paths.assert_awaited_once()

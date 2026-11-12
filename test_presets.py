"""Per-light favorite persistence and exact preset application."""

import copy
import inspect
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.esp_rgbww.coordinator import LightinatorCoordinator
from custom_components.esp_rgbww.light import LightinatorVirtualLight
from custom_components.esp_rgbww.panel import websocket_preset
from custom_components.esp_rgbww.presets import (
    favorite_preset_colors, is_light_favorite, light_preset_swatches, preset_command,
    preset_names, with_light_favorite,
)
from custom_components.esp_rgbww.virtual_lights import CONF_LEGACY_LIGHT_ID, VirtualLight


class PresetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.entry = SimpleNamespace(
            data={"host": "device", "port": 80, "chip_id": "123", "device_name": "Device"},
            entry_id="test", async_on_unload=MagicMock(),
        )
        with patch("homeassistant.helpers.frame.report_usage"):
            self.coordinator = LightinatorCoordinator(self.hass, self.entry, MagicMock())
        self.light = VirtualLight("uuid", 9, "White", "WWCW", True, {})
        self.other = VirtualLight("other", 3, "Other", "WWCW", False, {})
        self.preset = {
            "id": "p", "name": "Warm", "favorite": True,
            "color": {"hsv": {"h": 0, "s": 0, "v": 50, "ct": 2700}},
            "light_favorites": [{"light_id": "other", "favorite": True}],
        }
        self.remote = {"presets": [copy.deepcopy(self.preset)]}
        self.coordinator.data = {
            "lights": {"uuid": self.light, "other": self.other},
            "presets": copy.deepcopy(self.remote["presets"]),
            "info": {"rgbww": {"preset_light_favorites": True}},
        }
        self.coordinator._get = AsyncMock(side_effect=lambda path: copy.deepcopy(self.remote))

        async def write(path, payload):
            self.assertEqual(path, "/data")
            self.remote["presets"] = [copy.deepcopy(next(iter(payload.values())))]
            return {"success": True}

        self.coordinator.async_post_http = AsyncMock(side_effect=write)
        self.coordinator.post = AsyncMock()

    def test_membership_is_per_uuid_with_legacy_default_fallback(self):
        self.assertTrue(is_light_favorite(self.preset, self.light))
        removed = with_light_favorite(self.preset, self.light, False)
        self.assertFalse(is_light_favorite(removed, self.light))
        self.assertTrue(is_light_favorite(removed, self.other))
        self.assertTrue(is_light_favorite(removed))
        self.assertEqual(removed["color"], self.preset["color"])
        self.assertEqual(len(removed["light_favorites"]), 2)

    async def test_membership_write_preserves_other_lights_and_latest_color(self):
        self.remote["presets"][0]["color"]["hsv"]["v"] = 80
        await self.coordinator.async_set_preset_favorite("p", False, "uuid")
        saved = self.coordinator.data["presets"][0]
        self.assertEqual(saved["color"]["hsv"]["v"], 80)
        self.assertFalse(is_light_favorite(saved, self.light))
        self.assertTrue(is_light_favorite(saved, self.other))

    async def test_old_51_firmware_cannot_silently_drop_membership(self):
        self.coordinator.data["info"] = {}
        with self.assertRaisesRegex(ValueError, "Update the 5.1"):
            await self.coordinator.async_set_preset_favorite("p", True, "uuid")
        self.coordinator.async_post_http.assert_not_awaited()

    async def test_save_current_indexed_raw_persists_profile_and_light_membership(self):
        self.coordinator.data["light_states"] = {"uuid": {"raw": [250, 750]}}
        response = MagicMock()

        async def posted():
            payload = self.coordinator._session.post.call_args.kwargs["json"]
            self.remote["presets"].extend(copy.deepcopy(payload["presets[]"]))
            return response

        self.coordinator._session.post.return_value.__aenter__.side_effect = posted
        await self.coordinator.save_current_preset("Mix", "raw", True, light_id="uuid")
        saved = self.coordinator.data["presets"][-1]
        self.assertEqual(saved["raw_channels"], [250, 750])
        self.assertEqual(saved["light_type"], "WWCW")
        self.assertFalse(saved["favorite"])
        self.assertTrue(is_light_favorite(saved, self.light))
        self.assertFalse(is_light_favorite(saved, self.other))

    async def test_save_current_rejects_lost_membership_on_readback(self):
        self.coordinator.data["light_states"] = {"uuid": self.preset["color"]}
        self.coordinator._session.post.return_value.__aenter__.return_value = MagicMock()
        with self.assertRaisesRegex(ValueError, "membership readback mismatch"):
            await self.coordinator.save_current_preset("New", "hsv", True, light_id="uuid")
        self.assertEqual(len(self.coordinator.data["presets"]), 1)

    async def test_save_current_accepts_configdb_zero_defaults_on_readback(self):
        self.coordinator.data["light_states"] = {"uuid": {"hsv": {"h": 30, "s": 40, "v": 50}}}
        response = MagicMock()

        async def posted():
            payload = self.coordinator._session.post.call_args.kwargs["json"]
            preset = copy.deepcopy(payload["presets[]"][0])
            preset["color"]["hsv"]["ct"] = 0
            self.remote["presets"].append(preset)
            return response

        self.coordinator._session.post.return_value.__aenter__.side_effect = posted
        await self.coordinator.save_current_preset("New", "hsv", True, light_id="uuid")
        self.assertEqual(self.coordinator.data["presets"][-1]["color"]["hsv"]["ct"], 0)

    async def test_invalid_favorite_readback_is_an_explicit_error(self):
        self.coordinator._get.side_effect = [self.remote, {"presets": None}]
        with self.assertRaisesRegex(ValueError, "invalid preset readback"):
            await self.coordinator.async_set_preset_favorite("p", False, "uuid")

    async def test_readback_failure_does_not_publish_success(self):
        self.coordinator.async_post_http = AsyncMock(return_value={"success": True})
        with self.assertRaisesRegex(ValueError, "readback mismatch"):
            await self.coordinator.async_set_preset_favorite("p", False, "uuid")
        self.assertTrue(is_light_favorite(self.coordinator.data["presets"][0], self.light))

    async def test_legacy_favorite_does_not_require_new_firmware(self):
        self.coordinator.data["lights"] = None
        self.coordinator.data["info"] = {}
        await self.coordinator.async_set_preset_favorite("p", False)
        self.assertFalse(self.remote["presets"][0]["favorite"])

    async def test_application_uses_uuid_and_preserves_temperature_and_brightness(self):
        await self.coordinator.async_apply_preset("p", "uuid")
        self.coordinator.post.assert_awaited_once_with(
            "/color", {"cmd": "solid", **self.preset["color"]}, light_id="uuid",
        )

    async def test_removed_light_and_preset_never_fall_back(self):
        with self.assertRaisesRegex(ValueError, "no longer configured"):
            await self.coordinator.async_apply_preset("p", "removed")
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            await self.coordinator.async_apply_preset("missing", "uuid")
        self.coordinator.post.assert_not_awaited()

    def test_indexed_raw_requires_matching_profile(self):
        preset = {"id": "raw", "name": "Mix", "favorite": True,
                  "raw_channels": [250, 750], "light_type": "WWCW"}
        self.assertEqual(preset_command(preset, [preset], default_command="solid", light_type="WWCW"),
                         {"cmd": "solid", "raw": [250, 750]})
        with self.assertRaisesRegex(ValueError, "matching light profile"):
            preset_command(preset, [preset], default_command="solid", light_type="RGB")
        self.assertEqual(light_preset_swatches([preset], self.light)[0]["id"], "raw")
        other = VirtualLight("rgb", 4, "RGB", "RGB", True, {})
        self.assertEqual(light_preset_swatches([preset], other), [])

    def test_legacy_select_and_native_colors_do_not_use_indexed_default_hsv(self):
        indexed = {
            "id": "raw", "name": "Indexed", "favorite": True,
            "raw_channels": [250, 750], "light_type": "WWCW",
            "color": {"hsv": {"h": 0, "s": 0, "v": 0, "ct": 0}},
        }
        alias = {"id": "alias", "name": "Alias", "favorite": True,
                 "color": {"Preset": {"id": "raw"}}}
        self.assertEqual(preset_names([indexed, alias]), [])
        self.assertEqual(favorite_preset_colors([indexed, alias]), [])

    def test_migrated_primary_keeps_its_persisted_uuid_not_the_new_default(self):
        self.entry.data[CONF_LEGACY_LIGHT_ID] = "other"
        self.assertEqual(self.coordinator.preset_entity_target("123"), "other")
        del self.coordinator.data["lights"]["other"]
        with self.assertRaisesRegex(ValueError, "no longer configured"):
            self.coordinator.preset_entity_target("123")

    def test_native_entity_exposes_only_its_favorite_swatches(self):
        preset = with_light_favorite(self.preset, self.light, False)
        self.coordinator.data["presets"] = [preset]
        primary = LightinatorVirtualLight(self.coordinator, self.entry, self.light)
        other = LightinatorVirtualLight(self.coordinator, self.entry, self.other)
        self.assertEqual(primary.extra_state_attributes["lightinator_presets"], [])
        self.assertEqual(other.extra_state_attributes["lightinator_presets"][0]["id"], "p")

    def test_tagged_and_flat_preset_references_are_equivalent(self):
        for color in ({"Preset": {"id": "p"}}, {"id": "p"}):
            alias = {"id": "alias", "color": color}
            self.assertEqual(
                preset_command(alias, [self.preset, alias], default_command="solid"),
                {"cmd": "solid", **self.preset["color"]},
            )

    async def test_websocket_requires_entity_control_permission(self):
        connection = MagicMock()
        connection.user.permissions.check_entity.return_value = False
        await inspect.unwrap(websocket_preset)(
            self.hass, connection,
            {"id": 1, "entity_id": "light.test", "preset_id": "p", "action": "apply"},
        )
        self.assertEqual(connection.send_error.call_args.args[1], "unauthorized")
        self.coordinator.post.assert_not_awaited()

    async def test_websocket_rejects_raw_channel_entity(self):
        connection = MagicMock()
        connection.user.permissions.check_entity.return_value = True
        entity = SimpleNamespace(platform="esp_rgbww", domain="light", config_entry_id="test",
                                 unique_id="123_uuid_ww")
        self.hass.data["esp_rgbww"] = {"test": self.coordinator}
        self.coordinator.last_update_success = True
        self.hass.states.async_set("light.test", "on", {"light_id": "other"})
        with patch("custom_components.esp_rgbww.panel.er.async_get") as registry:
            registry.return_value.async_get.return_value = entity
            await inspect.unwrap(websocket_preset)(
                self.hass, connection,
                {"id": 2, "entity_id": "light.test", "preset_id": "p", "action": "apply"},
            )
        self.assertEqual(connection.send_error.call_args.args[1], "invalid_target")
        self.coordinator.post.assert_not_awaited()

    async def test_websocket_applies_only_to_the_registered_primary_uuid(self):
        connection = MagicMock()
        connection.user.permissions.check_entity.return_value = True
        entity = SimpleNamespace(platform="esp_rgbww", domain="light", config_entry_id="test",
                                 unique_id="123_uuid")
        self.hass.data["esp_rgbww"] = {"test": self.coordinator}
        self.coordinator.last_update_success = True
        self.hass.states.async_set("light.test", "on", {"light_id": "other"})
        with patch("custom_components.esp_rgbww.panel.er.async_get") as registry:
            registry.return_value.async_get.return_value = entity
            await inspect.unwrap(websocket_preset)(
                self.hass, connection,
                {"id": 3, "entity_id": "light.test", "preset_id": "p", "action": "apply"},
            )
        self.coordinator.post.assert_awaited_once_with(
            "/color", {"cmd": "solid", **self.preset["color"]}, light_id="uuid",
        )
        connection.send_result.assert_called_once_with(3)

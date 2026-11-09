"""Compatibility tests using real HA entity/coordinator classes and mocked I/O."""

import asyncio
import copy
import math
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.light import ColorMode, valid_supported_color_modes
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.esp_rgbww.coordinator import LightinatorCoordinator
from custom_components.esp_rgbww.light import (
    LightinatorChannelLight,
    LightinatorMainLight,
    LightinatorVirtualChannelLight,
    LightinatorVirtualLight,
    async_setup_entry,
)
from custom_components.esp_rgbww.options_flow import LightinatorOptionsFlow
from custom_components.esp_rgbww.rpc import RpcError
from custom_components.esp_rgbww.virtual_lights import (
    CHANNELS,
    CONF_LEGACY_LIGHT_ID,
    channel_command,
    decode_color,
    discover_lights,
)


def configuration(*types):
    return {"hardware": {"configuration": {"lights": [
        {"id": f"uuid-{index}", "index": index + 3, "name": f"Light {index}",
         "type": light_type, "is_default": index == 0,
         "color": {"colortemp": {"ww": 2200, "cw": 6500}}}
        for index, light_type in enumerate(types)
    ]}}}


class VirtualLightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.entry = SimpleNamespace(
            data={"host": "controller", "port": 80, "chip_id": "123", "device_name": "Test"},
            entry_id="test-entry", async_on_unload=MagicMock(),
        )
        with patch("homeassistant.helpers.frame.report_usage"):
            self.coordinator = LightinatorCoordinator(self.hass, self.entry, MagicMock())
        self.coordinator._rpc_request = AsyncMock()
        self.coordinator._get = AsyncMock()
        self.hass.data["esp_rgbww"] = {self.entry.entry_id: self.coordinator}

    def install(self, *types):
        config = configuration(*types)
        self.coordinator._get.return_value = config
        lights = discover_lights(config, "V5.1.0")
        self.coordinator.data = {
            "lights": lights,
            "light_states": {light_id: {"hsv": {"h": 120, "s": 50, "v": 50, "ct": 0}}
                             for light_id in lights},
        }
        return lights

    def entity(self, light_id="uuid-0"):
        return LightinatorVirtualLight(
            self.coordinator, self.entry, self.coordinator.light(light_id)
        )

    def test_discovery_legacy_and_empty_virtual_controller(self):
        self.assertIsNone(discover_lights({}, "V5.0-42-develop"))
        self.assertIsNone(discover_lights(configuration("SINGLE"), "V5.0.0"))
        self.assertEqual(discover_lights(configuration(), "V5.1.0"), {})
        with self.assertRaisesRegex(ValueError, "unavailable"):
            discover_lights({}, "V5.1.0")

    def test_discovery_rejects_duplicate_or_unsafe_identities(self):
        for field, value in (("id", "uuid-0"), ("index", 3), ("index", True),
                             ("type", "unsupported"), ("is_default", True)):
            config = configuration("SINGLE", "RGB")
            config["hardware"]["configuration"]["lights"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                discover_lights(config, "V5.1.0")

    def test_wire_units_and_mode_replacement(self):
        state = decode_color({"hsv": {"h": math.pi, "s": 0.5, "v": 0.25, "ct": 3200}})
        self.assertEqual(state, {"hsv": {"h": 180, "s": 50, "v": 25, "ct": 3200}})
        self.assertEqual(decode_color({"raw": [512, 0]}), {"raw": [512, 0]})
        with self.assertRaises(ValueError):
            decode_color({})

    def test_all_profiles_have_valid_ha_modes(self):
        expected = {
            "SINGLE": {ColorMode.BRIGHTNESS}, "WW": {ColorMode.BRIGHTNESS},
            "CW": {ColorMode.BRIGHTNESS}, "WWCW": {ColorMode.COLOR_TEMP},
            "RGB": {ColorMode.HS}, "RGBWW": {ColorMode.RGBW},
            "RGBCW": {ColorMode.RGBW},
            "RGBWWCW": {ColorMode.HS, ColorMode.COLOR_TEMP, ColorMode.RGBWW},
        }
        for light_type in CHANNELS:
            with self.subTest(light_type=light_type):
                self.install(light_type)
                entity = self.entity()
                self.assertEqual(entity.supported_color_modes, expected[light_type])
                self.assertEqual(valid_supported_color_modes(entity.supported_color_modes),
                                 expected[light_type])
                self.assertIn(entity.color_mode, entity.supported_color_modes)
                entity.hass = self.hass
                entity.entity_id = "light.test"
                if ColorMode.COLOR_TEMP in entity.supported_color_modes:
                    self.coordinator.data["light_states"]["uuid-0"]["hsv"]["ct"] = 3500
                self.assertIsNotNone(entity.state_attributes)
                self.coordinator.data["light_states"]["uuid-0"] = (
                    {"raw": dict.fromkeys(entity._light.channels, 512)}
                    if entity._light.named_raw
                    else {"raw": [512] * len(entity._light.channels)}
                )
                self.assertIsNotNone(entity.state_attributes)

    async def test_poll_targets_every_virtual_index_and_normalizes_default(self):
        config = configuration("SINGLE", "WWCW")
        self.coordinator._get.side_effect = [config, {}]
        self.coordinator._rpc_request.side_effect = [
            {"app": {"git_version": "V5.1.0"}},
            {"raw": [1023]},
            {"hsv": {"h": math.pi, "s": 0, "v": 0.5, "ct": 4000}},
        ]
        data = await self.coordinator._async_update_data()
        self.assertEqual(data["light_states"]["uuid-0"], {"raw": [1023]})
        self.assertEqual(data["light_states"]["uuid-1"]["hsv"]["v"], 50)
        self.assertEqual(data["raw"], [1023])
        self.assertEqual(data["hsv"], {})
        self.assertEqual(self.coordinator._rpc_request.call_args_list[1].args,
                         ("color", {"light": 3}))
        self.assertEqual(self.coordinator._rpc_request.call_args_list[2].args,
                         ("color", {"light": 4}))

    async def test_empty_virtual_controller_never_reads_default(self):
        self.coordinator._get.side_effect = [configuration(), {}]
        self.coordinator._rpc_request.return_value = {"app": {"git_version": "V5.1.0"}}
        data = await self.coordinator._async_update_data()
        self.assertEqual(data["lights"], {})
        self.assertEqual(self.coordinator._rpc_request.await_count, 1)

    async def test_51_config_failure_does_not_fall_back_to_legacy(self):
        self.coordinator._rpc_request.return_value = {"app": {"git_version": "V5.1.0"}}
        self.coordinator._get.return_value = {}
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()
        self.assertEqual(self.coordinator._rpc_request.await_count, 1)

    async def test_50_poll_preserves_units_cache_and_untargeted_api(self):
        self.coordinator._config = {"color": {"color_mode": 0}}
        self.coordinator._get.return_value = {}
        self.coordinator._rpc_request.side_effect = [
            {"app": {"git_version": "V5.0-1000-develop"}},
            {"hsv": {"h": 180, "s": 50, "v": 25, "ct": 3000}},
        ]
        data = await self.coordinator._async_update_data()
        self.assertIsNone(data["lights"])
        self.assertEqual(data["hsv"]["v"], 25)
        self.coordinator._get.assert_awaited_once_with("/data")
        self.assertEqual(self.coordinator._rpc_request.call_args.args, ("color", {}))

    async def test_single_channel_commands_and_readback(self):
        self.install("SINGLE")
        entity = self.entity()
        self.coordinator.async_light_command = AsyncMock()
        await entity.async_turn_on(brightness=128)
        self.assertEqual(self.coordinator.async_light_command.call_args.args,
                         ("uuid-0", {"cmd": "fade", "t": 500, "raw": [514]}))
        await entity.async_turn_off()
        self.assertEqual(self.coordinator.async_light_command.call_args.args[1]["raw"], [0])
        self.coordinator.data["light_states"]["uuid-0"] = {"raw": [1023]}
        self.assertEqual(entity.brightness, 255)
        self.assertTrue(entity.is_on)

    def test_low_nonzero_pwm_remains_on(self):
        lights = self.install("WWCW")
        self.coordinator.data["light_states"]["uuid-0"] = {"raw": [1, 0]}
        primary = self.entity()
        warm = LightinatorVirtualChannelLight(
            self.coordinator, self.entry, lights["uuid-0"], "ww"
        )
        self.assertEqual(primary.brightness, 1)
        self.assertEqual(warm.brightness, 1)
        self.assertTrue(primary.is_on)
        self.assertTrue(warm.is_on)

    async def test_tw_ct_brightness_and_independent_raw_channels(self):
        lights = self.install("WWCW")
        entity = self.entity()
        self.assertEqual((entity.min_color_temp_kelvin, entity.max_color_temp_kelvin),
                         (2200, 6500))
        self.coordinator.async_light_command = AsyncMock()
        await entity.async_turn_on(brightness=153, color_temp_kelvin=4000)
        hsv = self.coordinator.async_light_command.call_args.args[1]["hsv"]
        self.assertEqual((hsv["v"], hsv["ct"], hsv["s"]), (60, 4000, 0))
        warm = LightinatorVirtualChannelLight(
            self.coordinator, self.entry, lights["uuid-0"], "ww"
        )
        self.assertFalse(warm.entity_registry_enabled_default)
        self.assertIsNone(warm.brightness)
        await warm.async_turn_on(brightness=255)
        self.assertEqual(self.coordinator.async_light_command.call_args.args,
                         ("uuid-0", {"raw": [1023, None]}))
        await warm.async_turn_off()
        self.assertEqual(self.coordinator.async_light_command.call_args.args[1],
                         {"raw": [0, None]})
        self.assertEqual(channel_command(lights["uuid-0"], "cw", 321), {"raw": [None, 321]})

    async def test_rgb_hs_and_brightness_zero(self):
        self.install("RGB")
        self.coordinator.async_light_command = AsyncMock()
        await self.entity().async_turn_on(hs_color=(240, 80), brightness=0)
        self.assertEqual(self.coordinator.async_light_command.call_args.args[1]["hsv"],
                         {"h": 240, "s": 80, "v": 0, "ct": 0})

    async def test_fixed_white_and_ha_rgbww_order(self):
        for light_type, attr, values, expected in (
            ("RGBWW", "rgbw_color", (255, 0, 0, 128), {"r": 1023, "g": 0, "b": 0, "ww": 514}),
            ("RGBCW", "rgbw_color", (0, 0, 0, 255), {"r": 0, "g": 0, "b": 0, "cw": 1023}),
            ("RGBWWCW", "rgbww_color", (0, 0, 0, 255, 0),
             {"r": 0, "g": 0, "b": 0, "cw": 1023, "ww": 0}),
        ):
            with self.subTest(light_type=light_type):
                self.install(light_type)
                entity = self.entity()
                self.coordinator.async_light_command = AsyncMock()
                await entity.async_turn_on(**{attr: values, "brightness": 255})
                self.assertEqual(self.coordinator.async_light_command.call_args.args[1]["raw"],
                                 expected)
                self.coordinator.data["light_states"]["uuid-0"] = {"raw": expected}
                self.assertEqual(getattr(entity, attr), values)

    async def test_raw_brightness_preserves_channel_mix(self):
        self.install("WWCW")
        self.coordinator.data["light_states"]["uuid-0"] = {"raw": [1023, 512]}
        self.coordinator.async_light_command = AsyncMock()
        await self.entity().async_turn_on(brightness=128)
        self.assertEqual(self.coordinator.async_light_command.call_args.args[1]["raw"],
                         [514, 257])

    async def test_raw_off_on_restores_channel_mix(self):
        self.install("RGBWWCW")
        entity = self.entity()
        self.coordinator.async_light_command = AsyncMock()
        self.coordinator.data["light_states"]["uuid-0"] = {
            "raw": {"r": 1023, "g": 0, "b": 0, "ww": 512, "cw": 0}
        }
        await entity.async_turn_off()
        self.coordinator.data["light_states"]["uuid-0"] = {
            "raw": dict.fromkeys(entity._light.channels, 0)
        }
        await entity.async_turn_on()
        self.assertEqual(self.coordinator.async_light_command.call_args.args[1]["raw"],
                         {"r": 1023, "g": 0, "b": 0, "ww": 512, "cw": 0})

    async def test_uuid_resolution_survives_reindex_and_default_change(self):
        lights = self.install("SINGLE", "SINGLE")
        config = configuration("SINGLE", "SINGLE")
        config["hardware"]["configuration"]["lights"].reverse()
        records = config["hardware"]["configuration"]["lights"]
        records[0]["is_default"] = True
        records[1].update(index=42, is_default=False)
        entity = self.entity()
        self.coordinator.data["lights"] = discover_lights(config, "V5.1.0")
        self.coordinator._get.return_value = config
        self.coordinator._rpc_request.return_value = {"raw": [512]}
        await entity.async_turn_on(brightness=128)
        self.assertEqual(self.coordinator._rpc_request.call_args_list[0].args[1]["light"], 42)
        self.assertEqual(self.coordinator.data["light_states"]["uuid-1"]["hsv"]["v"], 50)
        self.assertEqual(entity.unique_id, "123_uuid-0")
        self.assertEqual(entity.device_info["identifiers"], {("esp_rgbww", "123")})
        self.assertNotEqual(lights["uuid-0"].index, self.coordinator.light("uuid-0").index)

    async def test_external_reindex_is_resolved_before_next_poll(self):
        self.install("SINGLE", "SINGLE")
        config = configuration("SINGLE", "SINGLE")
        config["hardware"]["configuration"]["lights"][0]["index"] = 42
        config["hardware"]["configuration"]["lights"][1]["index"] = 3
        self.coordinator._get.return_value = config
        self.coordinator._rpc_request.return_value = {"raw": [0]}
        await self.entity().async_turn_off()
        self.assertEqual(self.coordinator._rpc_request.call_args_list[0].args[1]["light"], 42)

    async def test_external_delete_or_profile_change_blocks_stale_command(self):
        for change in ("delete", "profile"):
            with self.subTest(change=change):
                self.install("WWCW", "SINGLE")
                entity = self.entity()
                config = configuration("WWCW", "SINGLE")
                records = config["hardware"]["configuration"]["lights"]
                if change == "delete":
                    records.pop(0)
                    records[0]["index"] = 3
                    records[0]["is_default"] = True
                else:
                    records[0]["type"] = "RGB"
                self.coordinator._get.return_value = config
                with self.assertRaises(ValueError):
                    await entity.async_turn_off()
                self.coordinator._rpc_request.assert_not_awaited()

    async def test_removed_light_or_channel_cannot_control_default(self):
        lights = self.install("WWCW", "SINGLE")
        entity = self.entity()
        raw_entity = LightinatorVirtualChannelLight(
            self.coordinator, self.entry, lights["uuid-0"], "cw"
        )
        self.coordinator.data["lights"].pop("uuid-0")
        self.assertFalse(entity.available)
        self.assertFalse(raw_entity.available)
        with self.assertRaisesRegex(ValueError, "no longer configured"):
            await entity.async_turn_off()
        self.coordinator._rpc_request.assert_not_awaited()

    def test_removed_channel_becomes_unavailable_without_changing_primary_identity(self):
        lights = self.install("RGBWWCW")
        entity = self.entity()
        channel = LightinatorVirtualChannelLight(
            self.coordinator, self.entry, lights["uuid-0"], "cw"
        )
        self.coordinator.data["lights"] = discover_lights(configuration("RGB"), "V5.1.0")
        self.assertTrue(entity.available)
        self.assertFalse(channel.available)
        self.assertEqual(entity.unique_id, "123_uuid-0")
        self.assertEqual(entity.supported_color_modes, {ColorMode.HS})

    async def test_refresh_replaces_mode_only_on_target_uuid(self):
        self.install("SINGLE", "SINGLE")
        other = copy.deepcopy(self.coordinator.data["light_states"]["uuid-1"])
        self.coordinator._rpc_request.return_value = {"raw": [512]}
        await self.coordinator._refresh_color_state("uuid-0")
        self.assertEqual(self.coordinator.data["light_states"]["uuid-0"], {"raw": [512]})
        self.assertEqual(self.coordinator.data["light_states"]["uuid-1"], other)

    async def test_stale_refresh_cannot_update_reindexed_light(self):
        self.install("SINGLE")

        async def reindex(*_args):
            config = configuration("SINGLE")
            config["hardware"]["configuration"]["lights"][0]["index"] = 99
            self.coordinator.data["lights"] = discover_lights(config, "V5.1.0")
            return {"raw": [1023]}

        before = copy.deepcopy(self.coordinator.data["light_states"])
        self.coordinator._rpc_request.side_effect = reindex
        await self.coordinator._refresh_color_state("uuid-0")
        self.assertEqual(self.coordinator.data["light_states"], before)

    async def test_legacy_51_events_do_not_trigger_heavy_refreshes(self):
        self.install("SINGLE", "SINGLE")
        self.coordinator._refresh_color_state = AsyncMock()
        self.coordinator.async_request_refresh = AsyncMock()
        tasks = []

        def create_task(coroutine):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        with patch.object(self.hass, "async_create_task", side_effect=create_task):
            await self.coordinator._handle_ws_message(
                {"method": "color_event", "params": {"light": 4, "raw": [1023]}}
            )
            await asyncio.gather(*tasks)
            self.coordinator._refresh_color_state.assert_not_awaited()
            self.coordinator.async_request_refresh.assert_not_awaited()
            self.assertEqual(self.coordinator.data["light_states"]["uuid-1"], {"raw": [1023]})
            await self.coordinator._handle_ws_message(
                {"method": "color_event", "params": {"hsv": {"v": 1}}}
            )
            await asyncio.gather(*tasks)
            self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_subscribe_multiplexes_all_lights_on_existing_rpc_connection(self):
        self.coordinator._rpc_request.return_value = {"subscribed": True, "channel": "lights"}
        await self.coordinator._async_subscribe_light_updates()
        self.coordinator._rpc_request.assert_awaited_once_with(
            "subscribe", {"channel": "lights"}, wait_ready=False
        )
        self.assertTrue(self.coordinator._light_updates_subscribed)
        self.coordinator._session.ws_connect.assert_not_called()

    async def test_legacy_subscription_rejection_is_supported_but_auth_errors_surface(self):
        for code in (-32601, -32602):
            self.coordinator._rpc_request.side_effect = RpcError(code, "not supported")
            await self.coordinator._async_subscribe_light_updates()
            self.assertFalse(self.coordinator._light_updates_subscribed)
        self.coordinator._rpc_request.side_effect = RpcError(-32001, "auth failed")
        with self.assertRaises(RpcError):
            await self.coordinator._async_subscribe_light_updates()

    async def test_subscribed_command_is_one_guarded_rpc_without_http_or_immediate_read(self):
        self.install("SINGLE")
        self.coordinator._light_updates_subscribed = True
        self.coordinator._rpc_request.return_value = {"success": True}
        await self.entity().async_turn_on(brightness=255)
        self.coordinator._get.assert_not_awaited()
        self.coordinator._rpc_request.assert_awaited_once_with(
            "color", {"cmd": "fade", "t": 500, "raw": [1023],
                      "light": 3, "light_id": "uuid-0"}
        )
        self.assertEqual(self.entity().brightness, 255)
        await self.coordinator.async_disconnect_ws()

    async def test_subscribed_events_update_non_default_light_without_requesting_data(self):
        self.install("SINGLE", "SINGLE")
        first = copy.deepcopy(self.coordinator.data["light_states"]["uuid-0"])
        await self.coordinator._handle_ws_message({
            "method": "light_state",
            "params": {"light": 4, "light_id": "uuid-1", "state": {"raw": [789]}},
        })
        self.assertEqual(self.coordinator.data["light_states"]["uuid-1"], {"raw": [789]})
        self.assertEqual(self.coordinator.data["light_states"]["uuid-0"], first)
        self.coordinator._rpc_request.assert_not_awaited()
        self.coordinator._get.assert_not_awaited()

    async def test_config_notification_refreshes_discovery_even_with_heartbeats(self):
        self.install("SINGLE")
        self.coordinator._light_updates_subscribed = True
        self.coordinator.async_request_refresh = AsyncMock()
        tasks = []

        def create_task(coroutine):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        with patch.object(self.hass, "async_create_task", side_effect=create_task):
            await self.coordinator._handle_ws_message(
                {"method": "lights_changed", "params": {"revision": 42}}
            )
            await asyncio.gather(*tasks)
        self.coordinator.async_request_refresh.assert_awaited_once()
        self.assertTrue(self.coordinator._light_configuration_pending)
        self.coordinator._get.side_effect = [configuration("SINGLE", "WWCW"), {}]
        self.coordinator._rpc_request.side_effect = [
            {"app": {"git_version": "V5.1.0"}}, {"raw": [0]}, {"raw": [0, 0]},
        ]
        self.coordinator.data = await self.coordinator._async_update_data()
        self.assertEqual(len(self.coordinator.virtual_lights), 2)
        self.assertFalse(self.coordinator._light_configuration_pending)
        self.assertEqual(self.coordinator.update_interval.total_seconds(), 30)

    async def test_transition_does_not_read_or_apply_intermediate_state(self):
        self.install("SINGLE")
        self.coordinator._light_updates_subscribed = True
        self.coordinator._rpc_request.return_value = {"success": True}
        await self.entity().async_turn_on(brightness=255)
        await self.coordinator._handle_ws_message({
            "method": "light_state",
            "params": {"light": 3, "light_id": "uuid-0", "state": {"raw": [123]}},
        })
        self.assertEqual(self.entity().brightness, 255)
        await self.coordinator._refresh_color_state("uuid-0")
        self.assertEqual(self.coordinator._rpc_request.await_count, 1)
        self.coordinator._light_transition_until["uuid-0"] = 0
        self.coordinator._rpc_request.return_value = {"raw": [1023]}
        await self.coordinator._refresh_color_state("uuid-0")
        self.assertEqual(self.entity().brightness, 255)
        await self.coordinator.async_disconnect_ws()

    async def test_old_inflight_read_cannot_overwrite_newer_command_ack(self):
        self.install("SINGLE")
        self.coordinator._light_updates_subscribed = True
        started = asyncio.Event()
        finish = asyncio.Event()

        async def rpc(method, params):
            if "cmd" in params:
                return {"success": True}
            started.set()
            await finish.wait()
            return {"raw": [123]}

        self.coordinator._rpc_request.side_effect = rpc
        read = asyncio.create_task(self.coordinator._refresh_color_state("uuid-0"))
        await started.wait()
        await self.entity().async_turn_on(brightness=255)
        finish.set()
        await read
        self.assertEqual(self.entity().brightness, 255)
        await self.coordinator.async_disconnect_ws()

    async def test_multilight_poll_preserves_command_to_already_read_light(self):
        lights = self.install("SINGLE", "SINGLE")
        self.coordinator._light_updates_subscribed = True
        reading_second = asyncio.Event()
        finish = asyncio.Event()

        async def rpc(method, params):
            if "cmd" in params:
                return {"success": True}
            if params["light"] == 4:
                reading_second.set()
                await finish.wait()
            return {"raw": [123]}

        self.coordinator._rpc_request.side_effect = rpc
        poll = asyncio.create_task(self.coordinator._read_virtual_light_states(lights))
        await reading_second.wait()
        await self.entity().async_turn_on(brightness=255, transition=0)
        finish.set()
        states = await poll
        self.assertEqual(states["uuid-0"], {"raw": [1023]})
        self.assertEqual(states["uuid-1"], {"raw": [123]})
        await self.coordinator.async_disconnect_ws()

    async def test_metadata_poll_preserves_command_while_fetching_presets(self):
        self.install("SINGLE")
        self.coordinator._light_updates_subscribed = True
        reading_data = asyncio.Event()
        finish = asyncio.Event()

        async def get(path):
            if path == "/config":
                return configuration("SINGLE")
            self.assertEqual(path, "/data")
            reading_data.set()
            await finish.wait()
            return {}

        async def rpc(method, params):
            if method == "info":
                return {"app": {"git_version": "V5.1.0"}}
            return {"success": True} if "cmd" in params else {"raw": [123]}

        self.coordinator._get.side_effect = get
        self.coordinator._rpc_request.side_effect = rpc
        poll = asyncio.create_task(self.coordinator._async_update_data())
        await reading_data.wait()
        await self.entity().async_turn_on(brightness=255, transition=0)
        finish.set()
        data = await poll
        self.assertEqual(data["light_states"]["uuid-0"], {"raw": [1023]})
        self.assertEqual(data["raw"], [1023])
        await self.coordinator.async_disconnect_ws()

    async def test_malformed_light_notification_does_not_break_socket_reader(self):
        self.install("SINGLE")
        previous = self.coordinator.data
        for light_id, index in (({}, 3), ([], 3), ("uuid-0", True)):
            with self.assertLogs("custom_components.esp_rgbww.coordinator", level="WARNING"):
                await self.coordinator._handle_ws_message({
                    "method": "light_state",
                    "params": {"light_id": light_id, "light": index, "state": {"raw": [123]}},
                })
        self.assertIs(self.coordinator.data, previous)

    async def test_reconciliation_logs_invalid_color_response(self):
        self.install("SINGLE")
        self.coordinator._rpc_request.return_value = {"success": True}
        with self.assertLogs("custom_components.esp_rgbww.coordinator", level="WARNING"):
            await self.coordinator._refresh_color_state("uuid-0")

    async def test_reconciliation_waits_for_transition_and_is_cancelled_on_unload(self):
        self.install("SINGLE")
        self.coordinator._light_updates_subscribed = True
        self.coordinator._rpc_request.return_value = {"success": True}
        cancel = MagicMock()
        with patch("custom_components.esp_rgbww.coordinator.async_call_later",
                   return_value=cancel) as later:
            await self.entity().async_turn_on(brightness=255, transition=2)
            self.assertAlmostEqual(later.call_args.args[1], 2.1)
            self.coordinator._light_transition_until["uuid-0"] = 0
            self.coordinator._rpc_request.return_value = {"raw": [1023]}
            await later.call_args.args[2](None)
            self.assertEqual(self.coordinator._rpc_request.await_count, 2)
            await self.entity().async_turn_off()
            await self.coordinator.async_disconnect_ws()
            cancel.assert_called()

    async def test_runtime_update_does_not_replace_virtual_light_state(self):
        lights = self.install("WWCW", "SINGLE")
        states = self.coordinator.data["light_states"]
        await self.coordinator._handle_ws_message(
            {"method": "runtime_info", "params": {"uptime": 120}}
        )
        self.assertIs(self.coordinator.data["lights"], lights)
        self.assertIs(self.coordinator.data["light_states"], states)
        self.assertEqual(self.coordinator.data["runtime"]["uptime"], 120)

    async def test_heartbeats_do_not_postpone_discovery_poll(self):
        self.install("SINGLE")
        self.coordinator._listeners[0] = (MagicMock(), None)
        with patch.object(self.coordinator, "_schedule_refresh") as schedule:
            for uptime in (10, 20, 30, 40):
                await self.coordinator._handle_ws_message(
                    {"method": "runtime_info", "params": {"uptime": uptime}}
                )
        schedule.assert_not_called()

    async def test_command_ack_keeps_target_instead_of_reading_mid_fade(self):
        self.install("SINGLE")
        self.coordinator._rpc_request.return_value = {"success": True}
        await self.entity().async_turn_on(brightness=255)
        self.coordinator._rpc_request.assert_awaited_once_with(
            "color", {"cmd": "fade", "t": 500, "raw": [1023], "light": 3}
        )
        self.assertEqual(self.coordinator.data["light_states"]["uuid-0"], {"raw": [1023]})

    def test_partial_raw_ack_preserves_unchanged_channels(self):
        lights = self.install("RGB")
        state = self.coordinator._command_state(
            lights["uuid-0"], {"raw": {"r": 1, "g": 2, "b": 3}},
            {"raw": {"r": 1023, "g": None}},
        )
        self.assertEqual(state, {"raw": {"r": 1023, "g": 2, "b": 3}})
        lights = self.install("WWCW")
        state = self.coordinator._command_state(
            lights["uuid-0"], {"raw": [1, 2]}, {"raw": [1023]},
        )
        self.assertEqual(state, {"raw": [1023, 2]})

    def test_partial_raw_ack_does_not_invent_unknown_channels(self):
        for profile, raw in (("RGB", {"r": 1023}), ("WWCW", [1023])):
            lights = self.install(profile)
            self.assertIsNone(self.coordinator._command_state(
                lights["uuid-0"], {"hsv": {"v": 50}}, {"raw": raw},
            ))

    async def test_external_light_changes_are_polled_without_exporting_config_each_tick(self):
        self.coordinator._get.side_effect = [configuration("SINGLE", "SINGLE"), {}]
        self.coordinator._rpc_request.side_effect = [
            {"app": {"git_version": "V5.1.0"}}, {"raw": [0]}, {"raw": [0]},
            {"raw": [123]}, {"raw": [789]},
        ]
        self.coordinator.data = await self.coordinator._async_update_data()
        self.coordinator._get.reset_mock()
        self.coordinator.data = await self.coordinator._async_update_data()
        self.coordinator._get.assert_not_awaited()
        self.assertEqual(self.coordinator.data["light_states"]["uuid-1"], {"raw": [789]})
        self.assertLessEqual(self.coordinator.update_interval.total_seconds(), 1)

    async def test_50_color_event_and_refresh_still_use_legacy_state(self):
        self.coordinator.data = {"hsv": {"v": 20}, "raw": {}}
        self.coordinator._rpc_request.return_value = {"hsv": {"h": 180, "s": 50, "v": 25}}
        tasks = []

        def create_task(coroutine):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        with patch.object(self.hass, "async_create_task", side_effect=create_task):
            await self.coordinator._handle_ws_message(
                {"method": "color_event", "params": {"hsv": {"v": 25}}}
            )
            await asyncio.gather(*tasks)
        self.coordinator._rpc_request.assert_awaited_once_with("color", {})
        self.assertEqual(self.coordinator.data["hsv"], {"h": 180, "s": 50, "v": 25})

    async def setup_platform(self, existing_ids=()):
        registry = MagicMock()
        registry.async_get_entity_id.side_effect = (
            lambda _domain, _platform, unique_id: f"light.{unique_id}"
            if unique_id in existing_ids else None
        )
        entities = []
        callbacks = []
        self.hass.config_entries = MagicMock()

        def update_entry(entry, *, data):
            entry.data = data

        self.hass.config_entries.async_update_entry.side_effect = update_entry
        self.coordinator.async_add_listener = MagicMock(
            side_effect=lambda callback: callbacks.append(callback) or MagicMock()
        )
        with patch("custom_components.esp_rgbww.light.er.async_get", return_value=registry):
            await async_setup_entry(self.hass, self.entry, entities.extend)
            if callbacks:
                callbacks[0]()
        return entities, callbacks, registry

    async def test_new_install_and_dynamic_addition_have_uuid_entities(self):
        self.install("SINGLE", "WWCW")
        entities, callbacks, registry = await self.setup_platform()
        self.assertEqual([entity.unique_id for entity in entities],
                         ["123_uuid-0", "123_uuid-1", "123_uuid-1_ww", "123_uuid-1_cw"])
        self.assertNotIn(CONF_LEGACY_LIGHT_ID, self.entry.data)
        self.coordinator.data["lights"] = discover_lights(
            configuration("SINGLE", "WWCW", "RGB"), "V5.1.0"
        )
        with patch("custom_components.esp_rgbww.light.er.async_get", return_value=registry):
            callbacks[0]()
        self.assertEqual(len(entities), 8)

    async def test_legacy_migration_persists_binding_and_preserves_ids(self):
        self.install("RGBWWCW", "SINGLE")
        entities, _callbacks, _registry = await self.setup_platform({"123", "123_r", "123_ww"})
        self.assertEqual(self.entry.data[CONF_LEGACY_LIGHT_ID], "uuid-0")
        self.assertEqual(entities[0].unique_id, "123")
        self.assertEqual(entities[1].unique_id, "123_r")
        self.assertEqual(entities[4].unique_id, "123_ww")
        config = configuration("RGBWWCW", "SINGLE")
        records = config["hardware"]["configuration"]["lights"]
        records[0]["is_default"], records[1]["is_default"] = False, True
        self.coordinator.data["lights"] = discover_lights(config, "V5.1.0")
        entities, _callbacks, _registry = await self.setup_platform({"123"})
        self.assertEqual(entities[0]._light_id, "uuid-0")
        self.assertEqual(entities[0].unique_id, "123")
        self.assertEqual(entities[-1].unique_id, "123_uuid-1")

    async def test_deleted_legacy_uuid_does_not_rebind_after_restart(self):
        self.install("SINGLE", "SINGLE")
        self.entry.data[CONF_LEGACY_LIGHT_ID] = "removed-uuid"
        entities, _callbacks, _registry = await self.setup_platform({"123"})
        self.assertEqual([entity.unique_id for entity in entities], ["123_uuid-0", "123_uuid-1"])
        self.assertEqual(self.entry.data[CONF_LEGACY_LIGHT_ID], "removed-uuid")

    async def test_50_setup_and_commands_remain_unchanged(self):
        self.coordinator.data = {
            "hsv": {"h": 120, "s": 50, "v": 25, "ct": 3000},
            "raw": {"r": 1023, "g": 0, "b": 0},
            "config": {"color": {"color_mode": 0}},
        }
        entities, callbacks, _registry = await self.setup_platform()
        self.assertIs(type(entities[0]), LightinatorMainLight)
        self.assertIs(type(entities[1]), LightinatorChannelLight)
        self.assertEqual([entity.unique_id for entity in entities], ["123", "123_r", "123_g", "123_b"])
        self.assertFalse(callbacks)
        self.assertEqual(entities[0].brightness, 64)
        self.assertEqual(entities[0].hs_color, (120, 50))
        await entities[0].async_turn_on(brightness=255)
        self.assertEqual(self.coordinator._rpc_request.call_args.args,
                         ("color", {"cmd": "fade", "hsv": {"h": 120, "s": 50, "v": 100, "ct": 3000}, "t": 500}))
        await entities[0].async_turn_off()
        self.assertEqual(self.coordinator._rpc_request.call_args.args, ("off", {}))
        await entities[1].async_turn_off()
        self.assertEqual(self.coordinator._rpc_request.call_args.args, ("color", {"raw": {"r": 0}}))

    async def test_service_optional_uuid_is_resolved_and_legacy_default_unchanged(self):
        self.install("SINGLE", "SINGLE")
        await self.coordinator.post("/blink", {"t": 500}, light_id="uuid-1")
        self.assertEqual(self.coordinator._rpc_request.call_args.args,
                         ("blink", {"t": 500, "light": 4}))
        await self.coordinator.post("/blink", {"t": 500})
        self.assertEqual(self.coordinator._rpc_request.call_args.args, ("blink", {"t": 500}))
        with self.assertRaises(ValueError):
            await self.coordinator.post("/blink", {}, light_id="missing")
        await self.coordinator.post("/color", {"cmds": [{"hsv": {"v": 20}}]},
                                    light_id="uuid-1")
        self.assertEqual(self.coordinator._rpc_request.call_args.args[1]["cmds"][0]["light"], 4)
        with self.assertRaisesRegex(ValueError, "conflict"):
            await self.coordinator.post("/color", {"cmds": [{"light": 3, "hsv": {"v": 20}}]},
                                        light_id="uuid-1")

    async def test_unsupported_indexed_raw_preset_is_rejected_before_writing(self):
        self.install("WWCW")
        self.coordinator.data["light_states"]["uuid-0"] = {"raw": [123, 456]}
        with self.assertRaisesRegex(ValueError, "does not support indexed RAW"):
            await self.coordinator.save_current_preset("Warm", "raw", True, light_id="uuid-0")
        self.coordinator._session.post.assert_not_called()

    async def test_targeted_hsv_preset_saves_target_state_not_controller_default(self):
        self.install("RGB", "RGB")
        target = {"h": 240, "s": 80, "v": 30, "ct": 0}
        self.coordinator.data["hsv"] = {"h": 0, "s": 0, "v": 0}
        self.coordinator.data["light_states"]["uuid-1"] = {"hsv": target}
        self.coordinator._get.return_value = {"presets": []}
        response = MagicMock()
        self.coordinator._session.post.return_value.__aenter__.return_value = response
        await self.coordinator.save_current_preset("Blue", "hsv", light_id="uuid-1")
        payload = self.coordinator._session.post.call_args.kwargs["json"]
        self.assertEqual(payload["presets[]"][0]["color"], {"hsv": target})

    async def test_legacy_color_settings_only_offered_on_50(self):
        flow = LightinatorOptionsFlow(self.entry)
        flow.hass = self.hass
        with patch.object(flow, "async_show_menu", return_value={}) as menu:
            self.coordinator.data = {}
            await flow.async_step_init()
            self.assertIn("color", menu.call_args.kwargs["menu_options"])
            self.install("WWCW")
            await flow.async_step_init()
            self.assertNotIn("color", menu.call_args.kwargs["menu_options"])


if __name__ == "__main__":
    unittest.main()

"""Compatibility tests using real HA entity/coordinator classes and mocked I/O."""

import asyncio
import copy
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.light import ColorMode, valid_supported_color_modes
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.exceptions import HomeAssistantError
from homeassistant.data_entry_flow import AbortFlow

from custom_components.esp_rgbww import _register_services
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
from custom_components.esp_rgbww.sensor import LightinatorSensor, SENSORS
from homeassistant.components.sensor import SensorStateClass
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
    async def test_configure_unloaded_entry_aborts_instead_of_key_error(self):
        flow = LightinatorOptionsFlow(self.entry)
        flow.hass = self.hass
        for data in ({}, {"esp_rgbww": {}}):
            self.hass.data.pop("esp_rgbww", None)
            self.hass.data.update(data)
            with self.assertRaises(AbortFlow) as raised:
                await flow.async_step_init()
            self.assertEqual(raised.exception.reason, "controller_not_loaded")

    async def test_configure_entry_unloaded_while_form_is_open_aborts(self):
        self.install("SINGLE")
        flow = LightinatorOptionsFlow(self.entry)
        flow.hass = self.hass
        with patch.object(flow, "async_show_menu", return_value={}):
            await flow.async_step_init()
        del self.hass.data["esp_rgbww"][self.entry.entry_id]
        for step in (flow.async_step_hardware, flow.async_step_create_light,
                     flow.async_step_delete_light, flow.async_step_color):
            with self.assertRaises(AbortFlow) as raised:
                await step()
            self.assertEqual(raised.exception.reason, "controller_not_loaded")

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
        state = decode_color({"hsv": {"h": 180, "s": 50, "v": 25, "ct": 3200}})
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
            {"hsv": {"h": 180, "s": 0, "v": 50, "ct": 4000}},
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

    def test_hsv_readback_percentages(self):
        for profile in ("SINGLE", "WWCW", "RGB"):
            for value in (0, 0.25, 12.414467, 32.453568, 100):
                with self.subTest(profile=profile, value=value):
                    self.install(profile)
                    self.coordinator.data["light_states"]["uuid-0"] = decode_color(
                        {"hsv": {"h": 180, "s": 50, "v": value, "ct": 3200}}
                    )
                    expected = max(1, round(value * 255 / 100)) if value else 0
                    self.assertEqual(self.entity().brightness, expected)

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
        with self.assertRaisesRegex(ValueError, "Update the 5.1 firmware"):
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
            self.assertIn("create_light", menu.call_args.kwargs["menu_options"])
            self.assertIn("delete_light", menu.call_args.kwargs["menu_options"])

    def management_config(self, *types):
        self.install(*types)
        config = configuration(*types)
        hardware = config["hardware"]["configuration"]
        hardware.update({
            "revision": 7, "soc": "esp32", "board_id": "", "template_id": "keep",
            "system": {"clear_config_enabled": True, "clear_config_pin": 21},
            "buttons": [{"pin": 22}], "buses": [{"sda": 23, "scl": 25}],
        })
        pin = 1
        for light in hardware["lights"]:
            light["pins"] = list(range(pin, pin + len(CHANNELS[light["type"]])))
            pin += len(light["pins"])
        self.saved_config = copy.deepcopy(config)
        self.coordinator._get.side_effect = lambda path: copy.deepcopy(self.saved_config)

        async def save(path, payload):
            self.assertEqual(path, "/config")
            self.saved_config["hardware"].update(copy.deepcopy(payload["hardware"]))
            return {"success": True}

        self.coordinator.async_post_http = AsyncMock(side_effect=save)
        self.coordinator.async_request_refresh = AsyncMock()
        return config

    async def test_create_all_profiles_preserves_hardware_and_refreshes(self):
        for light_type, channels in CHANNELS.items():
            with self.subTest(light_type=light_type):
                original = self.management_config("SINGLE")
                await self.coordinator.async_manage_light(
                    name=" New output ", light_type=light_type,
                    pins=list(range(10, 10 + len(channels))),
                )
                saved = self.saved_config["hardware"]["configuration"]
                self.assertEqual(saved["revision"], 8)
                self.assertEqual(saved["lights"][0], original["hardware"]["configuration"]["lights"][0])
                self.assertEqual(saved["lights"][1]["index"], 4)
                self.assertEqual(saved["lights"][1]["name"], "New output")
                self.assertFalse(saved["lights"][1]["is_default"])
                self.assertNotEqual(saved["lights"][1]["id"], "uuid-0")
                for key in ("system", "buttons", "buses", "template_id", "soc"):
                    self.assertEqual(saved[key], original["hardware"]["configuration"][key])
                self.coordinator.async_request_refresh.assert_awaited_once()
                self.assertEqual(self.coordinator._next_metadata_refresh, 0)
                self.assertTrue(self.coordinator._light_configuration_pending)

    async def test_first_created_light_is_default_and_can_replace_default(self):
        self.management_config()
        await self.coordinator.async_manage_light(name="First", pins=[18])
        first = self.saved_config["hardware"]["configuration"]["lights"][0]
        self.assertTrue(first["is_default"])
        self.assertEqual(first["index"], 0)
        await self.coordinator.async_manage_light(name="Second", pins=[19], is_default=True)
        lights = self.saved_config["hardware"]["configuration"]["lights"]
        self.assertFalse(lights[0]["is_default"])
        self.assertTrue(lights[1]["is_default"])
        self.assertNotEqual(lights[0]["id"], lights[1]["id"])

    async def test_creation_rejects_invalid_and_conflicting_pins_before_posting(self):
        invalid = ([1], [21], [22], [23], [25], [True], [1.5], [-1], [256], [])
        for pins in invalid:
            with self.subTest(pins=pins):
                self.management_config("SINGLE")
                with self.assertRaises(ValueError):
                    await self.coordinator.async_manage_light(name="New", pins=pins)
                self.coordinator.async_post_http.assert_not_awaited()
        self.management_config("SINGLE")
        with self.assertRaises(ValueError):
            await self.coordinator.async_manage_light(name="New", light_type="WWCW", pins=[18, 18])
        for name in (" ", "x" * 65):
            with self.assertRaises(ValueError):
                await self.coordinator.async_manage_light(name=name, pins=[18])
        with self.assertRaises(ValueError):
            await self.coordinator.async_manage_light(name="New", light_type="INVALID", pins=[18])
        self.coordinator.async_post_http.assert_not_awaited()

    async def test_creation_checks_board_outputs_and_reservations(self):
        self.management_config("SINGLE")
        self.saved_config["hardware"]["configuration"].update(board_id="board", board_revision=1)
        board = {"id": "board", "revision": 1, "soc": "esp32", "reserved_gpios": [18],
                 "implemented_pins": [{"gpio": pin, "allowed_functions": ["light.pwm"]}
                                      for pin in (1, 18, 19)] + [
                                          {"gpio": pin, "allowed_functions": [function]}
                                          for pin, function in ((21, "system.clear_config"), (22, "button.input"),
                                                                (23, "i2c.sda"), (25, "i2c.scl"))
                                      ]}
        self.coordinator._get.side_effect = lambda path: (
            {"boards": [board]} if path == "/catalog" else copy.deepcopy(self.saved_config)
        )
        for pin in (18, 20):
            with self.assertRaises(ValueError):
                await self.coordinator.async_manage_light(name="New", pins=[pin])
        self.coordinator.async_post_http.assert_not_awaited()
        await self.coordinator.async_manage_light(name="New", pins=[19])
        self.assertEqual(self.saved_config["hardware"]["configuration"]["board_profile"], board)
        self.coordinator._get.side_effect = lambda path: copy.deepcopy(self.saved_config) if path == "/config" else self.fail("Saved profile must not require catalog lookup")
        await self.coordinator._async_validate_hardware_pins(
            self.saved_config["hardware"]["configuration"], self.saved_config["hardware"]
        )

    async def test_creation_rejects_count_and_index_limits(self):
        self.management_config(*(["SINGLE"] * 16))
        with self.assertRaisesRegex(ValueError, "16"):
            await self.coordinator.async_manage_light(name="New", pins=[18])
        self.coordinator.async_post_http.assert_not_awaited()
        self.management_config("SINGLE")
        self.saved_config["hardware"]["configuration"]["lights"][0]["index"] = 65534
        with self.assertRaisesRegex(ValueError, "index"):
            await self.coordinator.async_manage_light(name="New", pins=[18])
        self.coordinator.async_post_http.assert_not_awaited()

    async def test_delete_requires_existing_uuid_and_explicit_replacement_default(self):
        original = self.management_config("SINGLE", "SINGLE")
        for args in ({"delete_id": "missing"}, {"delete_id": "uuid-0"},
                     {"delete_id": "uuid-0", "replacement_default": "uuid-0"},
                     {"delete_id": "uuid-1", "replacement_default": "uuid-0"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.coordinator.async_manage_light(**args)
        self.coordinator.async_post_http.assert_not_awaited()
        await self.coordinator.async_manage_light(delete_id="uuid-0", replacement_default="uuid-1")
        saved = self.saved_config["hardware"]["configuration"]["lights"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["id"], "uuid-1")
        self.assertEqual(saved[0]["index"], 4)
        self.assertTrue(saved[0]["is_default"])
        self.assertEqual(saved[0]["color"], original["hardware"]["configuration"]["lights"][1]["color"])

    async def test_delete_nondefault_and_final_light(self):
        self.management_config("SINGLE", "SINGLE")
        await self.coordinator.async_manage_light(delete_id="uuid-1")
        self.assertTrue(self.saved_config["hardware"]["configuration"]["lights"][0]["is_default"])
        await self.coordinator.async_manage_light(delete_id="uuid-0")
        self.assertEqual(self.saved_config["hardware"]["configuration"]["lights"], [])

    async def test_stale_form_and_concurrent_edit_are_not_overwritten(self):
        original = self.management_config("SINGLE")
        expected = copy.deepcopy(original["hardware"]["configuration"])
        expected["revision"] -= 1
        with self.assertRaisesRegex(ValueError, "changed"):
            await self.coordinator.async_manage_light(name="New", pins=[18], expected_configuration=expected)
        changed = copy.deepcopy(original)
        changed["hardware"]["configuration"]["revision"] += 1
        self.coordinator._get.side_effect = [original, changed]
        with self.assertRaisesRegex(ValueError, "changed"):
            await self.coordinator.async_manage_light(name="New", pins=[18])
        self.coordinator.async_post_http.assert_not_awaited()

    async def test_rejected_write_and_bad_readback_do_not_report_success(self):
        self.management_config("SINGLE")
        self.coordinator.async_post_http.side_effect = None
        self.coordinator.async_post_http.return_value = {"success": False, "error": "Rejected"}
        with self.assertRaisesRegex(ValueError, "rejected"):
            await self.coordinator.async_manage_light(name="New", pins=[18])
        self.coordinator.async_post_http.return_value = {"success": True}
        with self.assertRaisesRegex(ValueError, "readback"):
            await self.coordinator.async_manage_light(name="New", pins=[18])
        self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_management_is_never_available_on_50(self):
        self.coordinator.data = {}
        for args in ({"name": "New", "pins": [18]}, {"delete_id": "uuid-0"}):
            with self.assertRaisesRegex(ValueError, "5.1"):
                await self.coordinator.async_manage_light(**args)
        self.coordinator._get.assert_not_awaited()
        flow = LightinatorOptionsFlow(self.entry)
        flow.hass = self.hass
        with patch.object(flow, "async_show_menu", return_value={}) as menu:
            await flow.async_step_create_light({"name": "New"})
            await flow.async_step_delete_light({"light_id": "uuid-0", "confirm": True})
            for call in menu.call_args_list:
                self.assertNotIn("create_light", call.kwargs["menu_options"])
                self.assertNotIn("delete_light", call.kwargs["menu_options"])

    async def test_options_forms_create_and_require_delete_confirmation(self):
        self.entry.options = {}
        self.management_config("SINGLE")
        flow = LightinatorOptionsFlow(self.entry)
        flow.hass = self.hass
        with patch.object(flow, "async_show_form", return_value={}), \
                patch.object(flow, "async_create_entry", return_value={"type": "create_entry"}):
            await flow.async_step_create_light({"name": "White", "light_type": "WWCW", "is_default": False})
            result = await flow.async_step_light_pins({"ww": 18, "cw": 19})
            self.assertEqual(result["type"], "create_entry")
        self.management_config("SINGLE")
        flow = LightinatorOptionsFlow(self.entry)
        flow.hass = self.hass
        with patch.object(flow, "async_show_form", return_value={}) as form, \
                patch.object(flow, "async_create_entry", return_value={"type": "create_entry"}):
            await flow.async_step_delete_light()
            await flow.async_step_delete_light({"light_id": "uuid-0", "confirm": False})
            self.assertEqual(form.call_args.kwargs["errors"], {"confirm": "confirmation_required"})
            self.coordinator.async_post_http.assert_not_awaited()
            result = await flow.async_step_delete_light({"light_id": "uuid-0", "confirm": True})
            self.assertEqual(result["type"], "create_entry")

    async def test_management_services_route_validate_and_surface_errors(self):
        self.management_config("SINGLE")
        _register_services(self.hass)
        with patch("custom_components.esp_rgbww._get_coordinators_for_call", return_value=[self.coordinator]):
            await self.hass.services.async_call(
                "esp_rgbww", "create_light",
                {"device_id": "controller", "name": "New", "pins": [18]}, blocking=True,
            )
            await self.hass.services.async_call(
                "esp_rgbww", "delete_light",
                {"device_id": "controller", "light_id": "uuid-0",
                 "replacement_default": self.saved_config["hardware"]["configuration"]["lights"][1]["id"]},
                blocking=True,
            )
            with self.assertRaises(HomeAssistantError):
                await self.hass.services.async_call(
                    "esp_rgbww", "delete_light",
                    {"device_id": "controller", "light_id": "removed"}, blocking=True,
                )

    async def test_management_refresh_adds_entities_and_makes_deleted_uuid_unavailable(self):
        self.management_config("SINGLE")
        entities, callbacks, registry = await self.setup_platform()
        old = entities[0]
        self.assertTrue(old.available)
        self.assertEqual(old.extra_state_attributes, {"light_id": "uuid-0", "lightinator_presets": []})

        async def rpc(method, params):
            if method == "info":
                return {"app": {"git_version": "V5.1.0"}}
            return {"raw": [512] if params["light"] == 3 else [512, 256]}

        self.coordinator._rpc_request.side_effect = rpc
        del self.coordinator.async_add_listener
        for callback in callbacks:
            self.coordinator.async_add_listener(callback)
        self.coordinator.async_request_refresh = AsyncMock(side_effect=self.coordinator.async_refresh)
        with patch("custom_components.esp_rgbww.light.er.async_get", return_value=registry):
            await self.coordinator.async_manage_light(name="White", light_type="WWCW", pins=[18, 19])
            self.assertEqual(len(entities), 4)
            new_id = self.saved_config["hardware"]["configuration"]["lights"][1]["id"]
            self.assertEqual(entities[1].unique_id, f"123_{new_id}")
            self.assertEqual(entities[1].extra_state_attributes, {"light_id": new_id, "lightinator_presets": []})
            self.assertTrue(entities[1].available)
            await self.coordinator.async_manage_light(delete_id="uuid-0", replacement_default=new_id)
            self.assertFalse(old.available)
            self.assertTrue(entities[1].available)
            self.assertEqual(len(entities), 4)

    def hardware_config(self, soc="esp32"):
        self.management_config("SINGLE")
        self.coordinator.data["info"] = {"device": {"soc": soc}, "app": {"git_version": "V5.1.0"}}
        self.saved_config["hardware"]["configuration"]["soc"] = soc
        pwm = {
            "timer": {"speed_mode": "LOWSPEED", "frequency": 1000, "resolution": 10, "number": 0},
            "spreadSpectrum": {"mode": "ON", "width": 15, "subsampling": 4},
            "phaseShift": {"mode": "ON"},
        }
        self.saved_config["hardware"]["pwm"] = copy.deepcopy(pwm)
        board = {
            "id": "test-board", "revision": 2, "soc": soc, "name": "Test board",
            "reserved_gpios": [18],
            "implemented_pins": [
                {"gpio": pin, "allowed_functions": [function]}
                for pin, function in ((1, "light.pwm"), (21, "system.clear_config"),
                                      (22, "button.input"), (23, "i2c.sda"), (25, "i2c.scl"))
            ],
        }
        self.catalog = {"boards": [board, {**board, "id": "other-soc", "soc": "esp32c3" if soc == "esp32" else "esp32"}]}
        self.coordinator._get.side_effect = lambda path: (
            copy.deepcopy(self.catalog) if path == "/catalog" else copy.deepcopy(self.saved_config)
        )
        return copy.deepcopy(self.saved_config["hardware"]), pwm

    async def test_hardware_board_pwm_update_preserves_all_assignments(self):
        original, pwm = self.hardware_config()
        pwm["timer"].update(speed_mode="HIGHSPEED", frequency=2000, resolution=12, number=3)
        pwm["spreadSpectrum"].update(mode="OFF", width=0, subsampling=1)
        pwm["phaseShift"]["mode"] = "OFF"
        await self.coordinator.async_update_hardware("test-board", 2, pwm, expected_hardware=original)
        saved = self.saved_config["hardware"]
        expected_configuration = {**original["configuration"], "board_id": "test-board", "board_revision": 2,
                                  "revision": 8, "board_profile": self.catalog["boards"][0]}
        self.assertEqual(saved["configuration"], expected_configuration)
        self.assertEqual(saved["pwm"], pwm)
        self.coordinator.async_request_refresh.assert_awaited_once()
        self.coordinator.async_post_http.assert_awaited_once()
        self.assertEqual(set(self.coordinator.async_post_http.call_args.args[1]["hardware"]), {"configuration", "pwm"})
        next_expected = copy.deepcopy(saved)
        await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=next_expected)
        self.assertEqual(saved["configuration"]["board_id"], "")
        self.assertEqual(saved["configuration"]["board_revision"], 0)

    async def test_hardware_rejects_wrong_board_revision_soc_and_input_conflicts(self):
        for board_id, revision in (("test-board", 1), ("other-soc", 2), ("unknown", 2), ("", 1)):
            original, pwm = self.hardware_config()
            with self.subTest(board_id=board_id, revision=revision), self.assertRaises(ValueError):
                await self.coordinator.async_update_hardware(board_id, revision, pwm, expected_hardware=original)
            self.coordinator.async_post_http.assert_not_awaited()
        for pin in (1, 21, 22, 23, 25):
            original, pwm = self.hardware_config()
            self.catalog["boards"][0]["reserved_gpios"].append(pin)
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                await self.coordinator.async_update_hardware("test-board", 2, pwm, expected_hardware=original)
            self.coordinator.async_post_http.assert_not_awaited()

    async def test_hardware_pwm_validation_and_c3_modes(self):
        invalid = (
            ("timer", "frequency", 0), ("timer", "resolution", 0), ("timer", "resolution", 15),
            ("timer", "number", 4), ("timer", "frequency", True), ("timer", "speed_mode", "wrong"),
            ("spreadSpectrum", "width", 101), ("spreadSpectrum", "subsampling", 0),
            ("spreadSpectrum", "subsampling", 65536), ("phaseShift", "mode", "wrong"),
        )
        for section, key, value in invalid:
            original, pwm = self.hardware_config()
            pwm[section][key] = value
            with self.subTest(section=section, key=key, value=value), self.assertRaises(ValueError):
                await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)
            self.coordinator.async_post_http.assert_not_awaited()
        original, pwm = self.hardware_config("esp32c3")
        pwm["timer"]["speed_mode"] = "HIGHSPEED"
        with self.assertRaisesRegex(ValueError, "mode"):
            await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)
        self.coordinator.async_post_http.assert_not_awaited()
        pwm["timer"]["speed_mode"] = "LOWSPEED"
        await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)

    async def test_hardware_stale_pwm_and_bad_readback_surface_errors(self):
        original, pwm = self.hardware_config()
        original["pwm"]["timer"]["number"] = 1
        with self.assertRaisesRegex(ValueError, "changed"):
            await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)
        self.coordinator.async_post_http.assert_not_awaited()
        original, pwm = self.hardware_config()
        changed = copy.deepcopy(self.saved_config)
        changed["hardware"]["pwm"]["timer"]["frequency"] = 2000
        self.coordinator._get.side_effect = [copy.deepcopy(self.saved_config), changed]
        with self.assertRaisesRegex(ValueError, "changed"):
            await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)
        self.coordinator.async_post_http.assert_not_awaited()
        original, pwm = self.hardware_config()
        self.coordinator.async_post_http.side_effect = None
        self.coordinator.async_post_http.return_value = {"success": True}
        with self.assertRaisesRegex(ValueError, "readback"):
            await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)
        self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_custom_hardware_gpio_capabilities_and_soc_identity_are_checked(self):
        original, pwm = self.hardware_config()
        self.saved_config["hardware"]["available_pins"] = [{
            "soc": "esp32", "pins": [
                {"gpio": pin, "capability": 7}
                for pin in (1, 21, 22, 23, 25)
            ],
        }]
        self.saved_config["hardware"]["available_pins"][0]["pins"][0]["capability"] = 1
        with self.assertRaisesRegex(ValueError, "support"):
            await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)
        self.coordinator.async_post_http.assert_not_awaited()
        self.saved_config["hardware"]["available_pins"][0]["pins"][0]["capability"] = 7
        await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)
        original, pwm = self.hardware_config()
        self.coordinator.data["info"]["device"]["soc"] = "esp32c3"
        with self.assertRaisesRegex(ValueError, "SoC"):
            await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)
        self.coordinator.async_post_http.assert_not_awaited()

    async def test_hardware_configure_form_filters_boards_and_requires_confirmation(self):
        self.entry.options = {}
        original, _pwm = self.hardware_config("esp32c3")
        flow = LightinatorOptionsFlow(self.entry)
        flow.hass = self.hass
        with patch.object(flow, "async_show_form", return_value={}) as form, \
                patch.object(flow, "async_create_entry", return_value={"type": "create_entry"}):
            await flow.async_step_hardware()
            self.assertEqual(set(flow._boards), {"test-board:2"})
            schema = form.call_args.kwargs["data_schema"]
            values = schema({})
            self.assertEqual(values["pwm_frequency"], 1000)
            self.assertEqual(values["spread_subsampling"], 4)
            self.assertFalse(values["confirm"])
            mode_selector = next(value for key, value in schema.schema.items() if str(key) == "pwm_speed_mode")
            self.assertEqual(mode_selector.config["options"], ["LOWSPEED"])
            await flow.async_step_hardware(values)
            self.assertEqual(form.call_args.kwargs["errors"], {"confirm": "confirmation_required"})
            self.coordinator.async_post_http.assert_not_awaited()
            values.update(confirm=True, board="test-board:2", pwm_frequency=2000)
            result = await flow.async_step_hardware(values)
            self.assertEqual(result["type"], "create_entry")
            self.assertEqual(self.saved_config["hardware"]["configuration"]["lights"], original["configuration"]["lights"])
            self.assertEqual(self.saved_config["hardware"]["pwm"]["timer"]["frequency"], 2000)

    async def test_hardware_unavailable_catalog_and_legacy_are_not_writable(self):
        self.entry.options = {}
        original, pwm = self.hardware_config()
        flow = LightinatorOptionsFlow(self.entry)
        flow.hass = self.hass
        self.catalog = {"boards": None}
        with patch.object(flow, "async_show_form", return_value={}) as form:
            await flow.async_step_hardware()
            self.assertEqual(form.call_args.kwargs["errors"], {"base": "hardware_configuration_failed"})
        self.coordinator.data = {}
        with self.assertRaisesRegex(ValueError, "5.1"):
            await self.coordinator.async_update_hardware("", 0, pwm, expected_hardware=original)
        with patch.object(flow, "async_show_menu", return_value={}) as menu:
            await flow.async_step_hardware()
            self.assertNotIn("hardware", menu.call_args.kwargs["menu_options"])
        self.coordinator.async_post_http.assert_not_awaited()

    async def test_uptime_is_a_duration_measurement_and_can_reset(self):
        description = next(sensor for sensor in SENSORS if sensor.key == "uptime")
        sensor = LightinatorSensor(self.coordinator, self.entry, description)
        self.assertEqual(sensor.state_class, SensorStateClass.MEASUREMENT)
        for uptime in (30290, 30289, 0, 10):
            self.coordinator.data = {"info": {"runtime": {"uptime": uptime}}}
            self.assertEqual(sensor.native_value, uptime)

    async def test_info_poll_cannot_overwrite_newer_runtime_notification(self):
        self.coordinator.data = {"info": {"runtime": {"uptime": 100}}}
        self.coordinator._rpc_request.side_effect = [
            {"app": {"git_version": "V5.0"}, "runtime": {"uptime": 100}},
            {"hsv": {"v": 0}},
        ]
        self.coordinator._config = {}

        async def read_data(path):
            self.assertEqual(path, "/data")
            await self.coordinator._handle_ws_message(
                {"method": "runtime_info", "params": {"uptime": 101, "heap_free": 10000}}
            )
            return {}

        self.coordinator._get.side_effect = read_data
        data = await self.coordinator._async_update_data()
        self.assertEqual(data["info"]["runtime"], {"uptime": 101, "heap_free": 10000})

    async def test_info_refresh_preserves_newer_runtime_even_after_reboot(self):
        self.coordinator.data = {"info": {"runtime": {"uptime": 30290}}}

        async def read_info(*args):
            await self.coordinator._handle_ws_message(
                {"method": "runtime_info", "params": {"uptime": 1}}
            )
            return {"app": {"git_version": "V5.1"}, "runtime": {"uptime": 30289}}

        self.coordinator._rpc_request.side_effect = read_info
        info = await self.coordinator.async_refresh_info()
        self.assertEqual(info["runtime"]["uptime"], 1)
        self.assertEqual(self.coordinator.data["info"]["runtime"]["uptime"], 1)

    async def test_rpc_timeouts_identify_connection_and_method(self):
        del self.coordinator._rpc_request

        async def connection_timeout(awaitable, timeout):
            awaitable.close()
            raise TimeoutError()

        with patch("custom_components.esp_rgbww.coordinator.asyncio.wait_for",
                   side_effect=connection_timeout):
            with self.assertRaisesRegex(TimeoutError, "authenticated WebSocket.*controller:80.*info"):
                await self.coordinator._rpc_request("info", {})
        ws = MagicMock()
        ws.closed = False
        ws.send_json = AsyncMock()
        self.coordinator._ws = ws
        with patch("custom_components.esp_rgbww.coordinator.asyncio.wait_for",
                   side_effect=TimeoutError):
            with self.assertRaisesRegex(TimeoutError, "JSON-RPC 'color'.*request 1"):
                await self.coordinator._rpc_request("color", {}, wait_ready=False)
        self.assertEqual(self.coordinator._pending, {})
        ws.send_json.assert_awaited_once()

    async def test_blank_transport_error_has_nonempty_coordinator_context(self):
        self.coordinator._rpc_request.side_effect = TimeoutError()
        with self.assertRaisesRegex(UpdateFailed, "controller:80: TimeoutError"):
            await self.coordinator._async_update_data()

    async def test_closed_reader_reports_close_code_and_transport_exception(self):
        ws = MagicMock()
        ws.__aiter__.return_value = []
        ws.close_code = 1006
        ws.exception.return_value = ConnectionError("Connection reset by peer")
        pending = asyncio.get_running_loop().create_future()
        self.coordinator._pending[1] = pending
        await self.coordinator._listen_ws(ws)
        with self.assertRaisesRegex(ConnectionError, "controller:80.*1006.*Connection reset"):
            await pending
        self.coordinator._pending.clear()

    async def test_websocket_transport_failure_reconnects_and_resubscribes(self):
        ws = MagicMock()
        self.coordinator._session.ws_connect.return_value.__aenter__.return_value = ws
        self.coordinator._rpc_request.return_value = {"subscribed": True, "channel": "lights"}
        connections = 0

        async def listen(socket):
            nonlocal connections
            connections += 1
            if connections == 1:
                raise ConnectionError("Connection reset by peer")
            self.coordinator._closing = True

        with patch.object(self.coordinator, "_listen_ws", side_effect=listen), \
                patch("custom_components.esp_rgbww.coordinator.asyncio.sleep", new_callable=AsyncMock):
            await self.coordinator.async_connect_ws()
        self.assertEqual(connections, 2)
        self.assertEqual(self.coordinator.ws_generation, 2)
        self.assertEqual(self.coordinator._rpc_request.await_count, 4)
        self.assertFalse(self.coordinator.ws_connected)
        self.assertFalse(self.coordinator._ws_ready.is_set())


if __name__ == "__main__":
    unittest.main()

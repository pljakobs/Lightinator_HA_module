import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

spec = importlib.util.spec_from_file_location("firmware_api", Path(__file__).parent / "custom_components/esp_rgbww/firmware_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class FirmwareApiTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_legacy_and_versioned_identity(self):
        self.assertEqual(api.firmware_branch_from_version("V5.1.0-42-5.1-develop"), "5.1/develop")
        self.assertEqual(api.firmware_branch_from_version("V5.0-42-experimental"), "5.0/develop")
        self.assertEqual(api.firmware_branch_from_version("unknown"), "")
        self.assertEqual(api.firmware_identity("V5.0.0", "5.1/develop"), ("", ""))
        self.assertEqual(api.firmware_identity("V5.0-341-devel"), ("5.0", "develop"))

    def test_api_url_can_be_catalog_or_endpoint(self):
        self.assertEqual(api.firmware_api_base_url("https://example.test/api/firmware/versions"), "https://example.test/api")

    def test_saved_selection_preserves_line_and_stage(self):
        self.assertEqual(api.firmware_selection("V5.0-42-experimental", "experimental"), ("5.0", "develop"))
        self.assertEqual(api.firmware_selection("V5.0-42-develop", "5.1-develop"), ("5.1", "develop"))
        self.assertEqual(api.firmware_selection("V5.0-42-develop", "5.1/prod"), ("5.1", "prod"))
        self.assertEqual(api.firmware_selection("V5.0-42-develop", "stable", "5.0"), ("5.0", "prod"))
        with self.assertRaises(ValueError):
            api.firmware_selection("V5.0-42-develop", "5.1/develop", "5.0")
        with self.assertRaises(ValueError):
            api.firmware_selection("unknown", "stable")

    async def test_versions_preserve_explicit_line_and_filter_wrong_targets(self):
        client = api.FirmwareVersionApi(None, "https://example.test/version.json")
        client._get = AsyncMock(return_value=[
            {"version": "V5.1.0-42-5.1-develop", "branch": "5.1-develop", "soc": "esp32", "type": "release"},
            {"version": "V5.0-42-develop", "branch": "develop", "soc": "esp32", "type": "release"},
        ])
        result = await client.versions("esp32", "5.1/develop", "release")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["branch"], "5.1/develop")
        self.assertEqual(client._get.call_args.args[1]["release_line"], "5.1")
        self.assertEqual(client._get.call_args.args[1]["product"], "controller")

    async def test_rich_metadata_cannot_contradict_the_binary_identity(self):
        client = api.FirmwareVersionApi(None, "https://example.test/version.json")
        client._get = AsyncMock(return_value=[{"version": "V5.0-42-develop", "branch": "develop", "soc": "esp32", "type": "release", "release_line": "5.1", "stage": "develop"}])
        with self.assertRaisesRegex(ValueError, "Inconsistent"):
            await client.versions("esp32", "5.1/develop", "release")

    async def test_different_product_is_never_an_update_candidate(self):
        client = api.FirmwareVersionApi(None, "https://example.test/version.json")
        client._get = AsyncMock(return_value=[{"product": "wallpanel", "version": "V5.0-42-develop", "branch": "develop", "soc": "esp32", "type": "release"}])
        self.assertEqual(await client.versions("esp32", "5.0/develop", "release"), [])

    def test_final_version_sorts_after_prereleases(self):
        self.assertGreater(api.firmware_version_key("V5.1.0"), api.firmware_version_key("V5.1.0-rc.10"))
        self.assertGreater(api.firmware_version_key("V5.10.0"), api.firmware_version_key("V5.9.9"))

    async def test_same_line_install_does_not_require_migration_endpoint(self):
        client = api.FirmwareVersionApi(None, "https://example.test/version.json")
        candidate = {"id": "1", "version": "V5.0-43-develop", "branch": "5.0/develop", "soc": "esp32", "type": "release", "url": "https://example.test/rom.bin"}
        client._get = AsyncMock(return_value=[candidate])
        self.assertEqual((await client.verify_upgrade(candidate, "V5.0-42-develop", "legacy"))["version"], candidate["version"])
        self.assertEqual(client._get.await_count, 1)

    async def test_cross_line_install_requires_bridge(self):
        client = api.FirmwareVersionApi(None, "https://example.test/version.json")
        candidate = {"id": "2", "version": "V5.1.0-43-5.1-develop", "branch": "5.1/develop", "soc": "esp32", "type": "release", "url": "https://example.test/rom.bin"}
        client._get = AsyncMock(side_effect=[[candidate], {"status": "bridge_required", "reason": "Install bridge webapp first"}])
        with self.assertRaisesRegex(ValueError, "Install bridge"):
            await client.verify_upgrade(candidate, "V5.0-42-develop", "legacy")


if __name__ == "__main__":
    unittest.main()
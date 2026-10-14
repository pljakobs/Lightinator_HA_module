"""Native Home Assistant firmware update entity for Lightinator."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urljoin

import aiohttp
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHIP_ID,
    CONF_DEVICE_NAME,
    CONF_FIRMWARE_BRANCH,
    CONF_FIRMWARE_TYPE,
    DOMAIN,
)
from .coordinator import LightinatorCoordinator
from .firmware_api import (
    FirmwareVersionApi,
    firmware_branch_from_version,
    firmware_version_key,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=12)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LightinatorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LightinatorFirmwareUpdate(coordinator, entry)], update_before_add=True
    )


class LightinatorFirmwareUpdate(
    CoordinatorEntity[LightinatorCoordinator], UpdateEntity
):
    """Offer a ROM selected by SoC, configured branch, and build type."""

    _attr_has_entity_name = True
    _attr_name = "Firmware"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.INSTALL

    @property
    def should_poll(self) -> bool:
        return True

    def __init__(
        self, coordinator: LightinatorCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._session = async_get_clientsession(coordinator.hass)
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_firmware"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="ESP RGBWW Firmware",
            model="RGBWW Controller",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )
        self._candidate: dict[str, Any] | None = None
        self._ota_url = ""
        self._in_progress = False
        self._ota_message = ""

    async def async_update(self) -> None:
        """Fetch compatible firmware from the Lightinator version API."""
        try:
            config = await self.coordinator.async_get_config()
            ota = config.get("ota", {})
            self._ota_url = ota.get("url", "") if isinstance(ota, dict) else ""
            if not self._ota_url:
                self._candidate = None
                return

            info = (self.coordinator.data or {}).get("info", {})
            device = info.get("device", {}) if isinstance(info, dict) else {}
            app = info.get("app", {}) if isinstance(info, dict) else {}
            device = device if isinstance(device, dict) else {}
            app = app if isinstance(app, dict) else {}
            soc = str(device.get("soc", "")).lower()
            installed = str(app.get("git_version", ""))
            installed_branch = firmware_branch_from_version(installed)
            branch = str(
                self._entry.options.get(CONF_FIRMWARE_BRANCH, installed_branch)
            ).lower()
            installed_type = str(app.get("build_type", "")).lower()
            build_type = str(
                self._entry.options.get(CONF_FIRMWARE_TYPE, installed_type)
            ).lower()
            if not soc or not branch or not build_type:
                self._candidate = None
                return

            candidates = await FirmwareVersionApi(
                self._session, str(self._ota_url)
            ).versions(soc, branch, build_type)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.debug("Unable to fetch firmware API versions: %s", err)
            self._candidate = None
            return

        compatible = [
            item
            for item in candidates
            if isinstance(item.get("version"), str)
            and isinstance(item.get("url"), str)
        ]
        self._candidate = max(
            compatible,
            key=lambda item: firmware_version_key(
                str(item.get("fw_version") or item.get("version") or "")
            ),
            default=None,
        )

    @property
    def installed_version(self) -> str | None:
        app = ((self.coordinator.data or {}).get("info") or {}).get("app", {})
        return app.get("git_version") if isinstance(app, dict) else None

    @property
    def latest_version(self) -> str | None:
        if self._candidate is None:
            return None
        version = self._candidate.get("version")
        if not isinstance(version, str) or not version:
            return None

        app = ((self.coordinator.data or {}).get("info") or {}).get("app", {})
        installed = self.installed_version or ""
        installed_branch = firmware_branch_from_version(installed)
        installed_type = str(app.get("build_type", "")).lower()
        candidate_branch = str(self._candidate.get("branch", "")).lower()
        candidate_type = str(self._candidate.get("type", "")).lower()
        if candidate_branch != installed_branch or candidate_type != installed_type:
            return f"{version} ({candidate_branch}/{candidate_type})"
        return version

    @property
    def has_update(self) -> bool:
        """Compare build numbers and allow selected branch/type changes."""
        installed = self.installed_version
        if not installed or self._candidate is None:
            return False

        app = ((self.coordinator.data or {}).get("info") or {}).get("app", {})
        installed_type = str(app.get("build_type", "")).lower()
        installed_branch = firmware_branch_from_version(installed)
        selected_branch = str(self._candidate.get("branch", "")).lower()
        selected_type = str(self._candidate.get("type", "")).lower()
        if selected_branch != installed_branch or selected_type != installed_type:
            return True
        candidate_version = str(self._candidate.get("version", ""))
        return firmware_version_key(candidate_version) > firmware_version_key(
            installed
        )

    @property
    def release_summary(self) -> str | None:
        if self._candidate is None:
            return None
        summary = self._candidate.get("comment") or self._candidate.get("fw_comment")
        return str(summary) if summary else None

    @property
    def release_url(self) -> str | None:
        if self._candidate is None:
            return None
        relative_url = self._candidate.get("url")
        if not isinstance(relative_url, str) or not self._ota_url:
            return None
        return urljoin(self._ota_url, relative_url)

    @property
    def in_progress(self) -> bool:
        return self._in_progress

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"ota_message": self._ota_message} if self._ota_message else {}

    async def async_install(
        self, version: str | None = None, backup: bool = False
    ) -> None:
        """Start one confirmed ROM update for this controller."""
        del backup  # Firmware OTA manages its own flash/reboot lifecycle.
        if self._candidate is None or self.latest_version is None:
            raise RuntimeError("No compatible firmware release is available")
        candidate_version = str(self._candidate.get("version", ""))
        if version is not None and version not in {
            candidate_version,
            self.latest_version,
        }:
            raise RuntimeError("The requested firmware version is no longer selected")

        self._in_progress = True
        self._ota_message = "Firmware update started"
        self.async_write_ha_state()
        try:
            rom_url = self.release_url
            if rom_url is None:
                raise RuntimeError("The selected release has no ROM download URL")
            await self.coordinator.async_post_http(
                "/update", {"rom": {"url": rom_url}}
            )
        except Exception:
            self._in_progress = False
            self._ota_message = "Firmware update could not be started"
            self.async_write_ha_state()
            raise

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_ota_status", self._handle_ota_status)
        )

    @callback
    def _handle_ota_status(self, event) -> None:
        if event.data.get("entry_id") != self._entry.entry_id:
            return
        status = event.data.get("status")
        self._ota_message = str(event.data.get("message", ""))
        if status in (0, 4):
            self._in_progress = False
        else:
            self._in_progress = True
        self.async_write_ha_state()
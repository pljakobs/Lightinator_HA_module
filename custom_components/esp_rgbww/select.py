"""Select platform for Lightinator (ESP RGBWW) — preset selection."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CHIP_ID, CONF_DEVICE_NAME, CONF_GROUP_AREA_MAP, DOMAIN
from .coordinator import LightinatorCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Lightinator preset select entity."""
    coordinator: LightinatorCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = [LightinatorPresetSelect(coordinator, entry)]

    groups = (coordinator.data or {}).get("groups", [])
    seen_group_ids: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("id", "")).strip()
        if not group_id or group_id in seen_group_ids:
            continue
        seen_group_ids.add(group_id)
        group_name = str(group.get("name") or f"Group {group_id}")
        entities.append(
            LightinatorGroupAreaSelect(coordinator, entry, group_id, group_name)
        )

    async_add_entities(entities)


class LightinatorPresetSelect(CoordinatorEntity[LightinatorCoordinator], SelectEntity):
    """A select entity listing the saved colour presets on the device.

    Selecting an option triggers a solid-colour command using the preset's
    HSV values.  The current option is always None because the firmware
    does not report which preset is active.
    """

    _attr_has_entity_name = True
    _attr_name = "Preset"

    def __init__(
        self, coordinator: LightinatorCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_preset"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="ESP RGBWW Firmware",
            model="RGBWW Controller",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    @property
    def options(self) -> list[str]:
        presets = (self.coordinator.data or {}).get("presets", [])
        return [p["name"] for p in presets if "name" in p]

    @property
    def current_option(self) -> str | None:
        # The firmware has no concept of an "active" preset
        return None

    async def async_select_option(self, option: str) -> None:
        """Apply the selected preset."""
        presets = (self.coordinator.data or {}).get("presets", [])
        preset = next((p for p in presets if p.get("name") == option), None)

        if preset is None:
            _LOGGER.warning("Preset '%s' not found in device data", option)
            return

        hsv = preset.get("color", {}).get("hsv")
        if not hsv:
            _LOGGER.warning("Preset '%s' has no HSV colour data", option)
            return

        await self.coordinator.post("/color", {"cmd": "solid", "hsv": hsv})


class LightinatorGroupAreaSelect(CoordinatorEntity[LightinatorCoordinator], SelectEntity):
    """Maps a controller group to an HA Area (room)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LightinatorCoordinator,
        entry: ConfigEntry,
        group_id: str,
        group_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._group_id = group_id
        self._attr_name = f"Group {group_name} Room"
        self._attr_unique_id = f"{entry.data[CONF_CHIP_ID]}_group_{group_id}_room"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHIP_ID])},
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="ESP RGBWW Firmware",
            model="RGBWW Controller",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    @property
    def options(self) -> list[str]:
        area_reg = ar.async_get(self.hass)
        area_names = sorted(area.name for area in area_reg.async_list_areas())
        return ["Unassigned", *area_names]

    @property
    def current_option(self) -> str | None:
        mappings = self._entry.options.get(CONF_GROUP_AREA_MAP, {})
        area_id = mappings.get(self._group_id)
        if not area_id:
            return "Unassigned"

        area_reg = ar.async_get(self.hass)
        area = area_reg.async_get_area(area_id)
        return area.name if area else "Unassigned"

    async def async_select_option(self, option: str) -> None:
        area_reg = ar.async_get(self.hass)
        mappings = dict(self._entry.options.get(CONF_GROUP_AREA_MAP, {}))

        if option == "Unassigned":
            mappings.pop(self._group_id, None)
        else:
            selected_area = next(
                (area for area in area_reg.async_list_areas() if area.name == option),
                None,
            )
            if selected_area is None:
                _LOGGER.warning("Area '%s' not found for group mapping", option)
                return
            mappings[self._group_id] = selected_area.id

        new_options = dict(self._entry.options)
        new_options[CONF_GROUP_AREA_MAP] = mappings
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        self.async_write_ha_state()

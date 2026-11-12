"""Admin-only controller overview for the Lightinator sidebar panel."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.auth.permissions.const import POLICY_CONTROL

from .const import CONF_DEVICE_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)
_URL = "/esp_rgbww_panel"


async def controller_overview(hass: HomeAssistant, entry_id: str | None = None) -> list[dict[str, Any]]:
    """Return display-only fields, never controller credentials or raw configuration."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if entry_id is not None:
        entries = [entry for entry in entries if entry.entry_id == entry_id]
        if not entries:
            raise ValueError("Lightinator controller no longer exists")
    result = []
    for entry in entries:
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        info = ((coordinator.data or {}).get("info") or {}) if coordinator else {}
        device, app = info.get("device", {}), info.get("app", {})
        item: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "name": entry.data.get(CONF_DEVICE_NAME) or entry.title,
            "host": entry.data.get("host", ""),
            "loaded": coordinator is not None,
            "available": coordinator.last_update_success if coordinator else False,
            "firmware": app.get("git_version", ""),
            "soc": device.get("soc", ""),
            "virtual": coordinator.virtual_lights is not None if coordinator else False,
        }
        if entry_id is not None and coordinator is not None:
            config = await coordinator.async_get_config(force_refresh=True)
            hardware = config.get("hardware", {})
            configuration = hardware.get("configuration", {})
            item["board"] = configuration.get("board_id", "")
            item["pwm"] = hardware.get("pwm", {})
            item["lights"] = [
                {key: light.get(key) for key in ("id", "index", "name", "type", "pins", "is_default")}
                for light in configuration.get("lights", [])
            ]
        result.append(item)
    return result


@websocket_api.websocket_command({
    vol.Required("type"): "esp_rgbww/controllers",
    vol.Optional("entry_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_controllers(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Serve controller cards and a selected controller's inventory."""
    try:
        controllers = await controller_overview(hass, msg.get("entry_id"))
    except (aiohttp.ClientError, TimeoutError, ValueError, HomeAssistantError) as err:
        _LOGGER.warning("Unable to load Lightinator panel: %s", err)
        connection.send_error(msg["id"], "controller_unavailable", str(err))
        return
    connection.send_result(msg["id"], controllers)


@websocket_api.websocket_command({
    vol.Required("type"): "esp_rgbww/preset",
    vol.Required("entity_id"): str,
    vol.Required("preset_id"): str,
    vol.Required("action"): vol.In(["apply", "favorite"]),
    vol.Optional("favorite"): bool,
})
@websocket_api.async_response
async def websocket_preset(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Operate only on an authorized primary light, never a caller-supplied host."""
    entity_id = msg["entity_id"]
    if not connection.user or not connection.user.permissions.check_entity(entity_id, POLICY_CONTROL):
        connection.send_error(msg["id"], "unauthorized", "Light control permission required")
        return
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None or entity.platform != DOMAIN or entity.domain != "light":
        connection.send_error(msg["id"], "invalid_target", "Select a Lightinator primary light")
        return
    coordinator = hass.data.get(DOMAIN, {}).get(entity.config_entry_id)
    if coordinator is None or not coordinator.last_update_success:
        connection.send_error(msg["id"], "controller_unavailable", "Controller is unavailable")
        return
    try:
        light_id = coordinator.preset_entity_target(entity.unique_id)
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_target", str(err))
        return
    try:
        if msg["action"] == "apply":
            await coordinator.async_apply_preset(msg["preset_id"], light_id)
        else:
            if "favorite" not in msg:
                raise ValueError("favorite is required for membership changes")
            await coordinator.async_set_preset_favorite(msg["preset_id"], msg["favorite"], light_id)
    except (aiohttp.ClientError, TimeoutError, ValueError, HomeAssistantError) as err:
        _LOGGER.warning("Lightinator preset operation failed for %s: %s", entity_id, err)
        connection.send_error(msg["id"], "preset_failed", str(err))
        return
    connection.send_result(msg["id"])


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register one panel and its authenticated overview endpoint."""
    await hass.http.async_register_static_paths([
        StaticPathConfig(_URL, str(Path(__file__).parent / "frontend"), False)
    ])
    websocket_api.async_register_command(hass, websocket_controllers)
    websocket_api.async_register_command(hass, websocket_preset)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path="lightinator",
        webcomponent_name="lightinator-panel",
        sidebar_title="Lightinator",
        sidebar_icon="mdi:lightbulb-group",
        module_url=f"{_URL}/panel.js",
        require_admin=True,
        config_panel_domain=DOMAIN,
    )

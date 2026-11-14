"""Lightinator (ESP RGBWW) integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS
from .coordinator import LightinatorCoordinator
from .extension_coordinator import ExtensionCoordinator
from .virtual_lights import CHANNELS

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the central controller configuration panel."""
    from .panel import async_setup_panel

    await async_setup_panel(hass)
    return True


def _get_coordinators_for_call(
    hass: HomeAssistant, call: ServiceCall
) -> list[LightinatorCoordinator]:
    """Return the coordinators targeted by a service call's device_id list."""
    dev_reg = dr.async_get(hass)
    coordinators: list[LightinatorCoordinator] = []

    device_ids: list[str] | str = call.data.get("device_id", [])
    if isinstance(device_ids, str):
        device_ids = [device_ids]

    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if not device:
            _LOGGER.warning("Lightinator service: unknown device_id %s", device_id)
            continue
        for entry_id in device.config_entries:
            if entry_id in hass.data.get(DOMAIN, {}):
                coordinators.append(hass.data[DOMAIN][entry_id])

    return coordinators


def _register_services(hass: HomeAssistant) -> None:
    """Register custom Lightinator services (called once on first entry setup)."""

    async def handle_fade_sequence(call: ServiceCall) -> None:
        """Queue a multi-step colour sequence on the target device(s)."""
        steps: list[dict[str, Any]] = list(call.data["steps"])
        loop: bool = call.data.get("loop", False)

        # Auto-assign queue policies so the user doesn't have to.
        # First step resets the queue; subsequent steps are appended.
        for i, step in enumerate(steps):
            if "q" not in step:
                step["q"] = "front_reset" if i == 0 else "back"

        if loop and steps:
            steps[-1]["r"] = True

        for coordinator in _get_coordinators_for_call(hass, call):
            await coordinator.post("/color", {"cmds": steps}, light_id=call.data.get("light_id"))

    async def handle_blink(call: ServiceCall) -> None:
        """Trigger a blink animation on the target device(s)."""
        speed_ms: int = call.data.get("speed_ms", 500)
        loop: bool = call.data.get("loop", False)
        payload: dict[str, Any] = {"t": speed_ms}
        if loop:
            payload["r"] = True
        for coordinator in _get_coordinators_for_call(hass, call):
            await coordinator.post("/blink", payload, light_id=call.data.get("light_id"))

    async def handle_save_preset(call: ServiceCall) -> None:
        """Save the current controller color under a preset name."""
        name = str(call.data.get("name", "")).strip()
        if not name:
            raise ValueError("Preset name cannot be empty")
        color_mode = str(call.data.get("color_mode", "hsv"))
        favorite = call.data.get("favorite")

        for coordinator in _get_coordinators_for_call(hass, call):
            await coordinator.save_current_preset(
                name, color_mode, favorite, light_id=call.data.get("light_id")
            )

    async def handle_scan_wifi(call: ServiceCall) -> None:
        """Start a Wi-Fi scan on the selected controller(s)."""
        for coordinator in _get_coordinators_for_call(hass, call):
            await coordinator.async_post_http("/scan_networks", {})

    async def handle_connect_wifi(call: ServiceCall) -> None:
        """Connect a controller to the requested Wi-Fi network."""
        ssid = str(call.data["ssid"]).strip()
        if not ssid:
            raise ValueError("SSID cannot be empty")
        payload = {"ssid": ssid}
        password = call.data.get("password")
        if password:
            payload["password"] = password
        for coordinator in _get_coordinators_for_call(hass, call):
            await coordinator.async_post_http("/connect", payload)

    async def handle_manage_light(call: ServiceCall) -> None:
        coordinators = _get_coordinators_for_call(hass, call)
        if not coordinators:
            raise HomeAssistantError("Select a configured Lightinator controller")
        for coordinator in coordinators:
            try:
                if call.service == "delete_light":
                    await coordinator.async_manage_light(
                        delete_id=call.data["light_id"],
                        replacement_default=call.data.get("replacement_default"),
                    )
                else:
                    await coordinator.async_manage_light(
                        name=call.data["name"], light_type=call.data["light_type"],
                        pins=call.data["pins"], is_default=call.data["is_default"],
                    )
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise HomeAssistantError(str(err)) from err

    async def handle_discover_cluster(call: ServiceCall) -> None:
        """Discover and add controllers listed by /hosts on target device(s)."""
        added = 0
        seen_hosts: set[str] = set()

        for coordinator in _get_coordinators_for_call(hass, call):
            try:
                peers = await coordinator.get_cluster_peers()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Lightinator discovery failed for %s: %s",
                    coordinator.config_entry.data.get("host", "unknown"),
                    err,
                )
                continue

            for peer in peers:
                host = peer["host"]
                if host in seen_hosts:
                    continue
                seen_hosts.add(host)

                result = await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "user"},
                    data={
                        "host": host,
                        "port": coordinator.config_entry.data.get("port", 80),
                        "password": coordinator.config_entry.data.get("password", ""),
                        "device_name": peer.get("name", host),
                    },
                )
                if result.get("type") == "create_entry":
                    added += 1

        _LOGGER.info(
            "Lightinator discover_cluster checked %d host(s), added %d new device(s)",
            len(seen_hosts),
            added,
        )

    hass.services.async_register(DOMAIN, "fade_sequence", handle_fade_sequence)
    hass.services.async_register(DOMAIN, "blink", handle_blink)
    hass.services.async_register(DOMAIN, "discover_cluster", handle_discover_cluster)
    hass.services.async_register(DOMAIN, "save_preset", handle_save_preset)
    hass.services.async_register(DOMAIN, "scan_wifi", handle_scan_wifi)
    hass.services.async_register(DOMAIN, "connect_wifi", handle_connect_wifi)
    target_schema = {vol.Required("device_id"): vol.All(cv.ensure_list, [cv.string])}
    hass.services.async_register(
        DOMAIN, "create_light", handle_manage_light,
        schema=vol.Schema({
            **target_schema,
            vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=64)),
            vol.Required("light_type", default="SINGLE"): vol.In(CHANNELS),
            vol.Required("pins"): [vol.All(int, vol.Range(min=0, max=255))],
            vol.Optional("is_default", default=False): cv.boolean,
        }),
    )
    hass.services.async_register(
        DOMAIN, "delete_light", handle_manage_light,
        schema=vol.Schema({
            **target_schema,
            vol.Required("light_id"): cv.string,
            vol.Optional("replacement_default"): cv.string,
        }),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lightinator from a config entry."""
    session = async_get_clientsession(hass)
    coordinator = LightinatorCoordinator(hass, entry, session)
    entry.async_create_background_task(
        hass,
        coordinator.async_connect_ws(),
        f"lightinator_ws_{entry.entry_id}",
    )

    try:
        await coordinator.async_wait_connected()
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        await coordinator.async_disconnect_ws()
        raise ConfigEntryNotReady(f"Cannot connect to Lightinator: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    extensions = ExtensionCoordinator(hass, entry, session)
    await extensions.async_config_entry_first_refresh()
    coordinator.extensions = extensions

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register custom services on first entry load
    if not hass.services.has_service(DOMAIN, "fade_sequence"):
        _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: LightinatorCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_disconnect_ws()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

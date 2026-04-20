"""Lightinator (ESP RGBWW) integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS
from .coordinator import LightinatorCoordinator

_LOGGER = logging.getLogger(__name__)


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
            await coordinator.post("/color", {"cmds": steps})

    async def handle_blink(call: ServiceCall) -> None:
        """Trigger a blink animation on the target device(s)."""
        speed_ms: int = call.data.get("speed_ms", 500)
        loop: bool = call.data.get("loop", False)
        payload: dict[str, Any] = {"t": speed_ms}
        if loop:
            payload["r"] = True
        for coordinator in _get_coordinators_for_call(hass, call):
            await coordinator.post("/blink", payload)

    hass.services.async_register(DOMAIN, "fade_sequence", handle_fade_sequence)
    hass.services.async_register(DOMAIN, "blink", handle_blink)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lightinator from a config entry."""
    session = async_get_clientsession(hass)
    coordinator = LightinatorCoordinator(hass, entry, session)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(f"Cannot connect to Lightinator: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start WebSocket listener as a background task
    entry.async_create_background_task(
        hass,
        coordinator.async_connect_ws(),
        f"lightinator_ws_{entry.entry_id}",
    )

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

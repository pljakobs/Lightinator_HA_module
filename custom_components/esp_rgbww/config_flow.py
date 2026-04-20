"""Config flow for Lightinator (ESP RGBWW)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_CHIP_ID, CONF_DEVICE_NAME, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)


class InvalidAuth(Exception):
    """Raised when the device returns HTTP 401."""


async def _fetch_device_info(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    password: str,
) -> dict:
    """Call GET /info?v=2 and return the parsed JSON.

    Raises InvalidAuth on 401, aiohttp.ClientError on network failure.
    """
    auth = aiohttp.BasicAuth("", password) if password else None
    url = f"http://{host}:{port}/info?v=2"
    async with session.get(
        url, auth=auth, timeout=aiohttp.ClientTimeout(total=8)
    ) as resp:
        if resp.status == 401:
            raise InvalidAuth
        resp.raise_for_status()
        return await resp.json()


async def _fetch_device_name(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    password: str,
) -> str:
    """Call GET /config and extract the device name, falling back to host."""
    try:
        auth = aiohttp.BasicAuth("", password) if password else None
        url = f"http://{host}:{port}/config"
        async with session.get(
            url, auth=auth, timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            if resp.status != 200:
                return host
            cfg = await resp.json()
            # ConfigDB exports as nested structure; device name is under general
            return (
                cfg.get("general", {}).get("deviceName", "")
                or cfg.get("devicename", "")
                or host
            )
    except Exception:  # noqa: BLE001
        return host


class LightinatorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lightinator."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._name: str = ""
        self._chip_id: str = ""

    # ------------------------------------------------------------------
    # Manual entry
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual IP / hostname entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            password = user_input.get(CONF_PASSWORD, "")
            session = async_get_clientsession(self.hass)

            try:
                info = await _fetch_device_info(session, host, port, password)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during device validation")
                errors["base"] = "unknown"
            else:
                chip_id = str(
                    info.get("device", {}).get("deviceid")
                    or info.get("deviceid", host)
                )
                await self.async_set_unique_id(chip_id)
                self._abort_if_unique_id_configured()

                device_name = await _fetch_device_name(session, host, port, password)

                return self.async_create_entry(
                    title=device_name,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_PASSWORD: password,
                        CONF_CHIP_ID: chip_id,
                        CONF_DEVICE_NAME: device_name,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Optional(CONF_PASSWORD, default=""): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Zeroconf (mDNS) discovery
    # ------------------------------------------------------------------

    async def async_step_zeroconf(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a device discovered via mDNS."""
        self._host = discovery_info.host
        self._port = discovery_info.port or DEFAULT_PORT
        # TXT record fn= is the friendly name; fall back to service name
        self._name = (
            discovery_info.properties.get("fn")
            or discovery_info.name.split(".")[0]
        )

        chip_id = discovery_info.properties.get("id", "")
        if chip_id:
            await self.async_set_unique_id(chip_id)
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: self._host, CONF_PORT: self._port}
            )
            self._chip_id = chip_id

        self.context["title_placeholders"] = {
            "name": self._name,
            "host": self._host,
        }
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a Zeroconf-discovered device and optionally enter password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD, "")
            session = async_get_clientsession(self.hass)

            try:
                info = await _fetch_device_info(
                    session, self._host, self._port, password
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during device validation")
                errors["base"] = "unknown"
            else:
                chip_id = str(
                    info.get("device", {}).get("deviceid")
                    or info.get("deviceid", self._host)
                )
                if not self._chip_id:
                    await self.async_set_unique_id(chip_id)
                    self._abort_if_unique_id_configured()

                device_name = await _fetch_device_name(
                    session, self._host, self._port, password
                )

                return self.async_create_entry(
                    title=device_name or self._name,
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_PASSWORD: password,
                        CONF_CHIP_ID: chip_id,
                        CONF_DEVICE_NAME: device_name or self._name,
                    },
                )

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PASSWORD, default=""): str,
                }
            ),
            description_placeholders={
                "name": self._name,
                "host": self._host,
            },
            errors=errors,
        )

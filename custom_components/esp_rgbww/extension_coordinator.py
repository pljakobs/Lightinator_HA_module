"""Coordinators for optional firmware sensor extensions."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
_EXTENSIONS = ("current-1", "spectrum-1")
_WRITABLE_CONFIG = {
    "current-1": {"enabled", "address", "shunt_ohms", "interval_ms"},
    "spectrum-1": {"enabled", "atime", "astep", "gain", "interval_ms"},
}


class ExtensionCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll optional extension configuration and sensor readings."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Lightinator extensions {entry.data.get('device_name', entry.data[CONF_HOST])}",
            update_interval=timedelta(seconds=5),
        )
        self._session = session
        self._host = entry.data[CONF_HOST]
        self._port = entry.data[CONF_PORT]
        password = entry.data.get(CONF_PASSWORD, "")
        self._auth = aiohttp.BasicAuth("", password) if password else None
        self._config_lock = asyncio.Lock()
        self.supported: dict[str, bool | None] = {key: None for key in _EXTENSIONS}
        self.configs: dict[str, dict[str, Any]] = {}

    def _url(self, path: str) -> str:
        return f"http://{self._host}:{self._port}{path}"

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        request = self._session.get if method == "GET" else self._session.post
        kwargs: dict[str, Any] = {
            "auth": self._auth,
            "timeout": aiohttp.ClientTimeout(total=10),
        }
        if payload is not None:
            kwargs["json"] = payload
        async with request(self._url(path), **kwargs) as response:
            if response.status >= 400:
                message = await response.text()
                if response.content_type == "application/json":
                    try:
                        body = await response.json()
                    except ValueError:
                        body = None
                    if isinstance(body, dict):
                        message = str(body.get("error") or body.get("message") or message)
                raise aiohttp.ClientResponseError(
                    response.request_info,
                    response.history,
                    status=response.status,
                    message=message or response.reason or "Extension request failed",
                    headers=response.headers,
                )
            body = await response.text()
            if not body:
                return None
            if response.content_type == "application/json":
                return json.loads(body)
            return body

    async def _async_update_data(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for extension_id in _EXTENSIONS:
            name = "current" if extension_id == "current-1" else "spectrum"
            extension: dict[str, Any] = {
                "config": self.configs.get(extension_id),
                "reading": None,
                "readings_available": False,
                "error": None,
            }
            try:
                config = await self._request(
                    "GET", f"/extensions/{extension_id}/config"
                )
            except aiohttp.ClientResponseError as err:
                if err.status == 404:
                    self.supported[extension_id] = False
                    self.configs.pop(extension_id, None)
                    extension["config"] = None
                    state[name] = extension
                    continue
                extension["config"] = self.configs.get(extension_id)
                extension["error"] = str(err) or type(err).__name__
                _LOGGER.warning(
                    "Unable to poll %s config: %s", extension_id, extension["error"]
                )
                state[name] = extension
                continue
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                extension["config"] = self.configs.get(extension_id)
                extension["error"] = str(err) or type(err).__name__
                _LOGGER.warning(
                    "Unable to poll %s config: %s", extension_id, extension["error"]
                )
                state[name] = extension
                continue

            if (
                not isinstance(config, dict)
                or config.get("extension_id") != extension_id
                or not isinstance(config.get("available"), bool)
            ):
                self.supported[extension_id] = False
                self.configs.pop(extension_id, None)
                extension["config"] = None
                extension["error"] = "Invalid extension configuration response"
                _LOGGER.warning(
                    "Unable to poll %s config: %s", extension_id, extension["error"]
                )
                state[name] = extension
                continue

            self.supported[extension_id] = True
            self.configs[extension_id] = config
            extension["config"] = config
            if config.get("enabled") and config.get("available"):
                try:
                    reading = await self._request(
                        "GET", f"/extensions/{extension_id}/readings"
                    )
                    if isinstance(reading, dict):
                        extension["reading"] = reading
                        extension["readings_available"] = True
                    else:
                        extension["error"] = "Invalid extension readings response"
                except aiohttp.ClientResponseError as err:
                    if err.status != 404:
                        extension["error"] = str(err) or type(err).__name__
                except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                    extension["error"] = str(err) or type(err).__name__

            if extension["error"]:
                _LOGGER.warning(
                    "Unable to poll %s readings: %s", extension_id, extension["error"]
                )
            state[name] = extension
        return state

    async def async_update_extension_config(
        self, extension_id: str, update: dict[str, Any]
    ) -> None:
        """Persist a partial update without dropping the other settings."""
        if (
            extension_id not in _WRITABLE_CONFIG
            or not update.keys() <= _WRITABLE_CONFIG[extension_id]
        ):
            raise ValueError("Unknown extension or read-only configuration fields")
        if (
            self.supported.get(extension_id) is not True
            or self.configs.get(extension_id, {}).get("extension_id") != extension_id
        ):
            raise ValueError("Extension capability has not been reported by the controller")
        async with self._config_lock:
            await self._request(
                "POST", f"/extensions/{extension_id}/config", dict(update)
            )
            result = await self._request(
                "GET", f"/extensions/{extension_id}/config"
            )
            if (
                not isinstance(result, dict)
                or result.get("extension_id") != extension_id
                or not isinstance(result.get("available"), bool)
            ):
                raise ValueError(f"Invalid {extension_id} configuration response")
            for key, value in update.items():
                if result.get(key) != value:
                    raise ValueError(f"Firmware did not persist {extension_id} {key}")
            self.configs[extension_id] = result
        await self.async_request_refresh()

    async def async_start_spectrum_measurement(self) -> None:
        """Start one AS7341 measurement."""
        config = self.configs.get("spectrum-1", {})
        if (
            self.supported.get("spectrum-1") is not True
            or config.get("extension_id") != "spectrum-1"
            or not config.get("available")
        ):
            raise ValueError("Spectral measurement capability is not available")
        await self._request(
            "POST", "/extensions/spectrum-1/start_measurement", {}
        )
        await self.async_request_refresh()

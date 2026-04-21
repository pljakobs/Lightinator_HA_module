"""DataUpdateCoordinator for Lightinator (ESP RGBWW)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

_WS_BACKOFF_INITIAL = 2   # seconds
_WS_BACKOFF_MAX = 60      # seconds


class LightinatorCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages communication with a single Lightinator device.

    Primary data path is WebSocket (push). The periodic poll from
    DataUpdateCoordinator acts as a fallback / consistency check.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialise."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Lightinator {entry.data.get('device_name', entry.data[CONF_HOST])}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self._entry = entry
        self._session = session
        self._host: str = entry.data[CONF_HOST]
        self._port: int = entry.data[CONF_PORT]
        self._password: str = entry.data.get(CONF_PASSWORD, "")
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_connected: bool = False
        self._closing: bool = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def _auth(self) -> aiohttp.BasicAuth | None:
        return aiohttp.BasicAuth("", self._password) if self._password else None

    async def _get(self, path: str) -> Any:
        url = self._base_url() + path
        async with self._session.get(
            url, auth=self._auth(), timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ------------------------------------------------------------------
    # Public API used by entities
    # ------------------------------------------------------------------

    async def post(self, path: str, payload: dict | None = None) -> None:
        """POST a command to the device."""
        url = self._base_url() + path
        async with self._session.post(
            url,
            json=payload,
            auth=self._auth(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()

    async def get_json(self, path: str) -> Any:
        """GET and parse JSON from a device endpoint."""
        return await self._get(path)

    async def get_cluster_hosts(self) -> list[str]:
        """Read /hosts and return a deduplicated host list."""
        raw_hosts = await self._get("/hosts")
        candidates: list[str] = []

        if isinstance(raw_hosts, list):
            for item in raw_hosts:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    host = item.get("hostname") or item.get("host") or item.get("ip")
                    if isinstance(host, str):
                        candidates.append(host)
        elif isinstance(raw_hosts, dict):
            for key in ("hosts", "items", "data"):
                nested = raw_hosts.get(key)
                if isinstance(nested, list):
                    for item in nested:
                        if isinstance(item, str):
                            candidates.append(item)
                        elif isinstance(item, dict):
                            host = item.get("hostname") or item.get("host") or item.get("ip")
                            if isinstance(host, str):
                                candidates.append(host)

        seen: set[str] = set()
        out: list[str] = []
        for host in candidates:
            cleaned = host.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
        return out

    @property
    def ws_connected(self) -> bool:
        """Return True while the WebSocket link is up."""
        return self._ws_connected

    # ------------------------------------------------------------------
    # DataUpdateCoordinator hook  (fallback poll)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch current state from the device (HTTP fallback).

        When WS is connected this still runs periodically to keep
        info/preset data fresh.
        """
        try:
            color_resp = await self._get("/color")
            info_resp = await self._get("/info?v=2")
            data_resp = await self._get("/data")
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with Lightinator: {err}") from err

        presets = (
            data_resp.get("presets", [])
            if isinstance(data_resp, dict)
            else []
        )
        groups = (
            data_resp.get("groups", [])
            if isinstance(data_resp, dict)
            else []
        )
        current = self.data or {}
        return {
            "hsv": color_resp.get("hsv", current.get("hsv", {})),
            "raw": color_resp.get("raw", current.get("raw", {})),
            "info": info_resp,
            "presets": presets,
            "groups": groups,
        }

    # ------------------------------------------------------------------
    # WebSocket listener
    # ------------------------------------------------------------------

    async def async_connect_ws(self) -> None:
        """Open a WebSocket to the device and listen for push events.

        Reconnects automatically with exponential backoff.
        Stops cleanly when async_disconnect_ws() is called.
        """
        backoff = _WS_BACKOFF_INITIAL
        while not self._closing:
            try:
                ws_url = f"ws://{self._host}:{self._port}/ws"
                async with self._session.ws_connect(
                    ws_url, heartbeat=30
                ) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    backoff = _WS_BACKOFF_INITIAL
                    _LOGGER.debug("Lightinator WS connected to %s", ws_url)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                await self._handle_ws_message(msg.json())
                            except Exception:  # noqa: BLE001
                                _LOGGER.debug(
                                    "Failed to parse WS message: %s", msg.data
                                )
                        elif msg.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSE,
                        ):
                            break

            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.debug(
                    "Lightinator WS error (%s), retrying in %ds", err, backoff
                )
            finally:
                self._ws_connected = False
                self._ws = None

            if self._closing:
                return

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _WS_BACKOFF_MAX)

    async def _handle_ws_message(self, msg: dict) -> None:
        """Process an incoming WebSocket message and update coordinator data."""
        msg_type = msg.get("type")
        data = msg.get("data", {})

        if msg_type == "color":
            new_data = dict(self.data or {})
            if "hsv" in data:
                new_data["hsv"] = data["hsv"]
            if "raw" in data:
                new_data["raw"] = data["raw"]
            self.async_set_updated_data(new_data)

        elif msg_type == "transition_finished":
            self.hass.bus.async_fire(
                f"{DOMAIN}_transition_finished",
                {
                    "name": data.get("name", ""),
                    "requeued": data.get("requeued", False),
                    "entry_id": self._entry.entry_id,
                },
            )

    async def async_disconnect_ws(self) -> None:
        """Signal the WS listener to stop and close the socket."""
        self._closing = True
        if self._ws and not self._ws.closed:
            await self._ws.close()

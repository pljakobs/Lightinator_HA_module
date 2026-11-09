"""DataUpdateCoordinator for Lightinator (ESP RGBWW)."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import xml.etree.ElementTree as ET
from datetime import timedelta
from typing import Any, Callable

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_CHIP_ID, CONF_DEVICE_NAME, DOMAIN, UPDATE_INTERVAL
from .rpc import RpcAuthenticationError, RpcError, authentication_hash
from .virtual_lights import VirtualLight, decode_color, discover_lights

_LOGGER = logging.getLogger(__name__)

_WS_BACKOFF_INITIAL = 2   # seconds
_WS_BACKOFF_MAX = 60      # seconds
_LIGHT_POLL_INTERVAL = 1  # 5.1 firmware only pushes the default light


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
        self._ws_ready = asyncio.Event()
        self._ws_generation: int = 0
        self._ws_generation_changed = asyncio.Event()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_request_id = 1
        self._send_lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._authenticated = False
        self._data_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._next_metadata_refresh = 0.0
        self._light_command_versions: dict[str, int] = {}
        self._light_transition_until: dict[str, float] = {}
        self._light_updates_subscribed = False
        self._light_configuration_pending = False
        self._light_refresh_unsubs: dict[str, Callable[[], None]] = {}
        self._config: dict[str, Any] | None = None
        self._config_lock = asyncio.Lock()

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

    async def async_get_config(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Read controller config once, or refresh it for an explicit edit form."""
        async with self._config_lock:
            if self._config is None or force_refresh:
                config = await self._get("/config")
                self._config = config if isinstance(config, dict) else {}
            return self._config

    async def async_update_config(self, update: dict[str, Any]) -> None:
        """Apply one partial ConfigDB update without exporting unrelated data."""
        if not update:
            return

        async with self._config_lock:
            if self._config is None:
                config = await self._get("/config")
                self._config = config if isinstance(config, dict) else {}
            url = self._base_url() + "/config"
            async with self._session.post(
                url,
                json=update,
                auth=self._auth(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
            self._merge_config(self._config, update)
            safe_config = self._safe_config_view(self._config)

        new_data = dict(self.data or {})
        new_data["config"] = safe_config
        self.async_set_updated_data(new_data)

    async def async_post_http(self, path: str, payload: dict[str, Any]) -> Any:
        """POST one authenticated JSON request to a firmware HTTP endpoint."""
        url = self._base_url() + path
        async with self._session.post(
            url,
            json=payload,
            auth=self._auth(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            if resp.content_type == "application/json":
                return await resp.json()
            return await resp.text()

    async def async_get_controller_icons(self) -> list[str]:
        """Read icon identifiers from the controller's locally served sprite."""
        url = self._base_url() + "/icons/iconsSprite.svg"
        async with self._session.get(
            url, auth=self._auth(), timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            response.raise_for_status()
            sprite = await response.text()

        root = ET.fromstring(sprite)
        icons = sorted(
            {
                symbol.get("id", "")
                for symbol in root.iter()
                if symbol.tag.rsplit("}", 1)[-1] == "symbol"
                and symbol.get("id")
            }
        )
        if not icons:
            raise ValueError("The controller icon sprite has no symbols")
        return icons

    async def async_update_controller_icon(self, icon: str) -> None:
        """Persist an icon identifier to this controller's app-data metadata."""
        if not icon:
            raise ValueError("Controller icon cannot be empty")

        controller_id = str(self._entry.data[CONF_CHIP_ID])
        async with self._data_lock:
            app_data = await self._get("/data")
            controllers = app_data.get("controllers", []) if isinstance(app_data, dict) else []
            metadata = next(
                (
                    dict(item)
                    for item in controllers
                    if isinstance(item, dict)
                    and str(item.get("id", "")) == controller_id
                ),
                None,
            )
            timestamp = int(time.time() * 1000)
            if metadata is None:
                metadata = {
                    "id": controller_id,
                    "name": self._entry.data.get(CONF_DEVICE_NAME, self._host),
                    "ip-address": self._host,
                    "ts": timestamp,
                    "icon": icon,
                }
                payload = {"controllers[]": [metadata]}
            else:
                metadata["ts"] = timestamp
                metadata["icon"] = icon
                payload = {f"controllers[id={controller_id}]": metadata}

            await self.async_post_http("/data", payload)

        new_data = dict(self.data or {})
        new_data["controller_icon"] = icon
        self.async_set_updated_data(new_data)

    @classmethod
    def _merge_config(cls, target: dict[str, Any], update: dict[str, Any]) -> None:
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                cls._merge_config(target[key], value)
            else:
                target[key] = value

    @staticmethod
    def _safe_config_view(config: dict[str, Any]) -> dict[str, Any]:
        """Project non-secret values needed by HA without exposing credentials."""
        general = config.get("general", {})
        color = config.get("color", {})
        network = config.get("network", {})
        general = general if isinstance(general, dict) else {}
        color = color if isinstance(color, dict) else {}
        network = network if isinstance(network, dict) else {}

        color_view = {
            key: color[key]
            for key in ("color_mode", "brightness", "colortemp", "hsv")
            if key in color
        }
        network_view: dict[str, Any] = {}
        for section in ("connection", "rsyslog", "telemetry", "ntp"):
            values = network.get(section)
            if isinstance(values, dict):
                network_view[section] = {
                    key: value
                    for key, value in values.items()
                    if key not in {"password", "api_password"}
                }
        mqtt = network.get("mqtt")
        if isinstance(mqtt, dict):
            network_view["mqtt"] = {
                key: value
                for key, value in mqtt.items()
                if key not in {"password", "username"}
            }

        return {
            "general": {
                key: general[key]
                for key in ("device_name", "supported_color_models")
                if key in general
            },
            "color": color_view,
            "network": network_view,
        }

    # ------------------------------------------------------------------
    # Public API used by entities
    # ------------------------------------------------------------------

    def _publish_partial_data(self, data: dict[str, Any]) -> None:
        """A heartbeat/color push must not postpone discovery or clear errors."""
        self.data = data
        self.async_update_listeners()

    @property
    def virtual_lights(self) -> dict[str, VirtualLight] | None:
        """None denotes the unchanged 5.0 single-light interface."""
        return (self.data or {}).get("lights")

    def light(self, light_id: str) -> VirtualLight:
        """Resolve a UUID at command time; never fall back to the default."""
        light = (self.virtual_lights or {}).get(light_id)
        if light is None:
            raise ValueError(f"Virtual light {light_id} is no longer configured")
        return light

    async def _async_resolve_light(self, light_id: str) -> VirtualLight:
        """Check configuration before sending to a potentially reused index."""
        previous = self.virtual_lights
        if previous is None:
            raise ValueError("This controller does not expose virtual lights")
        if self._light_updates_subscribed and not self._light_configuration_pending:
            return self.light(light_id)
        config = await self.async_get_config(force_refresh=True)
        lights = discover_lights(config, "5.1")
        if lights is None:
            raise ValueError("Virtual-light configuration is unavailable")
        if lights != previous:
            data = dict(self.data or {})
            states = {
                key: state for key, state in data.get("light_states", {}).items()
                if key in lights and key in previous
                and lights[key].index == previous[key].index
                and lights[key].type == previous[key].type
            }
            default = next((light for light in lights.values() if light.is_default), None)
            default_state = states.get(default.id, {}) if default else {}
            data.update(
                lights=lights, light_states=states,
                hsv=default_state.get("hsv", {}), raw=default_state.get("raw", {}),
                config=self._safe_config_view(config),
            )
            self._publish_partial_data(data)
        return self.light(light_id)

    async def async_light_command(
        self, light_id: str, payload: dict[str, Any]
    ) -> None:
        """Send an explicitly indexed color command and refresh that UUID."""
        previous = self.light(light_id)
        version = self._light_command_versions.get(light_id, 0) + 1
        self._light_command_versions[light_id] = version
        self._light_transition_until[light_id] = float("inf")
        acknowledged = False
        try:
            light = await self._async_resolve_light(light_id)
            if light.type != previous.type:
                raise ValueError("The virtual-light profile changed; retry with its new controls")
            params = {**payload, "light": light.index}
            if self._light_updates_subscribed:
                params["light_id"] = light_id
            await self._rpc_request("color", params)
            if self._light_command_versions[light_id] != version:
                return
            self._light_transition_until[light_id] = (
                time.monotonic() + payload.get("t", 0) / 1000
            )
            acknowledged = True
            current = (self.data or {}).get("light_states", {}).get(light_id, {})
            state = self._command_state(light, current, payload)
            if state is not None:
                self._publish_light_state(light, state)
            self._schedule_light_reconciliation(light_id, version, payload.get("t", 0) / 1000)
        finally:
            if not acknowledged and self._light_command_versions.get(light_id) == version:
                self._light_transition_until.pop(light_id, None)

    def _schedule_light_reconciliation(self, light_id: str, version: int, delay: float) -> None:
        cancel = self._light_refresh_unsubs.pop(light_id, None)
        if cancel is not None:
            cancel()

        async def reconcile(_now: Any) -> None:
            self._light_refresh_unsubs.pop(light_id, None)
            if self._light_command_versions.get(light_id) == version:
                await self._refresh_color_state(light_id)

        self._light_refresh_unsubs[light_id] = async_call_later(
            self.hass, delay + 0.1, reconcile
        )

    @staticmethod
    def _command_state(
        light: VirtualLight, current: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Publish acknowledged absolute targets, without inventing unknown RAW."""
        hsv = payload.get("hsv")
        if isinstance(hsv, dict):
            return {"hsv": {**current.get("hsv", {}), **hsv}}
        raw = payload.get("raw")
        previous = current.get("raw")
        if isinstance(raw, dict):
            channels = dict(previous) if isinstance(previous, dict) else {}
            channels.update({key: value for key, value in raw.items() if value is not None})
            if any(channel not in channels for channel in light.channels):
                return None
            return {"raw": channels}
        if isinstance(raw, list):
            values = []
            for index in range(len(light.channels)):
                value = raw[index] if index < len(raw) else None
                if value is None:
                    if not isinstance(previous, list) or index >= len(previous):
                        return None
                    value = previous[index]
                values.append(value)
            return {"raw": values}
        return None

    def _publish_light_state(self, light: VirtualLight, state: dict[str, Any]) -> None:
        data = dict(self.data or {})
        data["light_states"] = {**data.get("light_states", {}), light.id: state}
        if light.is_default:
            data["hsv"] = state.get("hsv", {})
            data["raw"] = state.get("raw", {})
        self._publish_partial_data(data)

    def _light_is_transitioning(self, light_id: str) -> bool:
        return time.monotonic() < self._light_transition_until.get(light_id, 0)

    async def _read_virtual_light_states(
        self, lights: dict[str, VirtualLight]
    ) -> dict[str, Any]:
        """Read current outputs without interrupting an acknowledged transition."""
        states = {}
        versions = dict(self._light_command_versions)
        for light_id, light in lights.items():
            before = (self.virtual_lights or {}).get(light_id)
            version = self._light_command_versions.get(light_id, 0)
            state = None
            if not self._light_is_transitioning(light_id):
                color = await self._rpc_request("color", {"light": light.index})
                if (
                    version == self._light_command_versions.get(light_id, 0)
                    and not self._light_is_transitioning(light_id)
                    and before == (self.virtual_lights or {}).get(light_id)
                ):
                    state = decode_color(color)
            if state is None and before == light:
                state = (self.data or {}).get("light_states", {}).get(light_id)
            if state is not None:
                states[light_id] = state
        return self._preserve_newer_targets(states, lights, versions)

    def _preserve_newer_targets(
        self, states: dict[str, Any], lights: dict[str, VirtualLight],
        versions: dict[str, int],
    ) -> dict[str, Any]:
        for light_id, light in lights.items():
            if (
                versions.get(light_id, 0) != self._light_command_versions.get(light_id, 0)
                or self._light_is_transitioning(light_id)
            ):
                current = (self.data or {}).get("light_states", {}).get(light_id)
                if current is not None and (self.virtual_lights or {}).get(light_id) == light:
                    states[light_id] = current
        return states

    async def post(
        self, path: str, payload: dict | None = None, *, light_id: str | None = None
    ) -> None:
        """Send a firmware command using its JSON-RPC method."""
        method = path.strip("/")
        if method not in {
            "color",
            "stop",
            "skip",
            "pause",
            "continue",
            "blink",
            "toggle",
            "on",
            "off",
        }:
            raise ValueError(f"Unsupported Lightinator command: {path}")
        params = dict(payload or {})
        if light_id is not None:
            index = (await self._async_resolve_light(light_id)).index
            params["light"] = index
            if self._light_updates_subscribed:
                params["light_id"] = light_id
            if "cmds" in params:
                steps = params["cmds"]
                if not isinstance(steps, list) or any(
                    not isinstance(step, dict) or step.get("light", index) != index
                    for step in steps
                ):
                    raise ValueError("Sequence steps conflict with the selected virtual light")
                params["cmds"] = [{**step, "light": index} for step in steps]
                if self._light_updates_subscribed:
                    params["cmds"] = [{**step, "light_id": light_id} for step in params["cmds"]]
        await self._rpc_request(method, params)

    async def get_json(self, path: str) -> Any:
        """GET and parse JSON from a device endpoint."""
        return await self._get(path)

    async def save_current_preset(
        self, name: str, color_mode: str, favorite: bool | None = None,
        *, light_id: str | None = None,
    ) -> None:
        """Create or update a preset from this controller's current color."""
        if color_mode not in {"hsv", "raw"}:
            raise ValueError("color_mode must be 'hsv' or 'raw'")

        state = self.data or {}
        if light_id is not None:
            self.light(light_id)
            state = state.get("light_states", {}).get(light_id, {})
        current_color = state.get(color_mode)
        if isinstance(current_color, list):
            raise ValueError(
                "The firmware preset store does not support indexed RAW colors; "
                "use an HA scene for single-channel/tunable-white RAW state"
            )
        if not isinstance(current_color, dict) or not current_color:
            raise ValueError(f"Current {color_mode} color is not available")

        color_fields = (
            ("h", "s", "v", "ct")
            if color_mode == "hsv"
            else ("r", "g", "b", "ww", "cw")
        )
        color_values = {
            key: current_color[key]
            for key in color_fields
            if key in current_color and current_color[key] is not None
        }
        if not color_values:
            raise ValueError(f"Current {color_mode} color is empty")

        async with self._data_lock:
            data_doc = await self._get("/data")
            if not isinstance(data_doc, dict):
                raise ValueError("Controller returned invalid /data")

            presets = data_doc.get("presets", [])
            if not isinstance(presets, list):
                raise ValueError("Controller /data presets field is not a list")

            now_ms = int(time.time() * 1000)
            preset_index = next(
                (
                    index
                    for index, preset in enumerate(presets)
                    if isinstance(preset, dict) and preset.get("name") == name
                ),
                None,
            )

            if preset_index is None:
                chip_id = str(self._entry.data.get("chip_id", self._host))
                preset: dict[str, Any] = {
                    "id": f"{chip_id}-{secrets.randbelow(90_000_000) + 10_000_000}",
                    "name": name,
                    "favorite": favorite if favorite is not None else False,
                    "ts": now_ms,
                    "icon": "palette",
                    "color": {},
                }
                preset["color"][color_mode] = color_values
                presets.append(preset)
                payload = {"presets[]": [preset]}
            else:
                preset = dict(presets[preset_index])
                color_options = preset.get("color")
                if isinstance(color_options, dict):
                    color_options = dict(color_options)
                else:
                    color_options = {
                        key: preset[key]
                        for key in ("cmd", "t", "s", "q", "d", "channels")
                        if key in preset
                    }

                color_options.pop("id", None)
                color_options.pop("hsv", None)
                color_options.pop("raw", None)
                color_options[color_mode] = color_values
                preset["color"] = color_options
                preset["name"] = name
                preset["ts"] = now_ms
                if favorite is not None:
                    preset["favorite"] = favorite
                presets[preset_index] = preset
                preset_id = preset.get("id")
                selector = (
                    f"presets[id={preset_id}]"
                    if isinstance(preset_id, str) and preset_id
                    else f"presets[{preset_index}]"
                )
                payload = {selector: preset}

            url = self._base_url() + "/data"
            async with self._session.post(
                url,
                json=payload,
                auth=self._auth(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()

            new_data = dict(self.data or {})
            new_data["presets"] = presets
            self.async_set_updated_data(new_data)

    async def get_cluster_peers(self) -> list[dict[str, str]]:
        """Read /hosts and return deduplicated peers with host and display name."""
        raw_hosts = await self._get("/hosts")
        candidates: list[dict[str, str]] = []

        def _append_peer(item: Any) -> None:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    candidates.append({"host": cleaned, "name": cleaned})
                return

            if not isinstance(item, dict):
                return

            connect_host = (
                item.get("host")
                or item.get("ip")
                or item.get("address")
                or item.get("ip_address")
                or item.get("hostname")
            )
            display_name = (
                item.get("hostname")
                or item.get("name")
                or item.get("host")
                or item.get("ip")
                or connect_host
            )

            if isinstance(connect_host, str) and connect_host.strip():
                host_clean = connect_host.strip()
                name_clean = (
                    display_name.strip()
                    if isinstance(display_name, str) and display_name.strip()
                    else host_clean
                )
                candidates.append({"host": host_clean, "name": name_clean})

        if isinstance(raw_hosts, list):
            for item in raw_hosts:
                _append_peer(item)
        elif isinstance(raw_hosts, dict):
            for key in ("hosts", "items", "data"):
                nested = raw_hosts.get(key)
                if isinstance(nested, list):
                    for item in nested:
                        _append_peer(item)

        seen_hosts: set[str] = set()
        peers: list[dict[str, str]] = []
        for peer in candidates:
            host = peer["host"]
            if host in seen_hosts:
                continue
            seen_hosts.add(host)
            peers.append(peer)

        return peers

    async def get_cluster_hosts(self) -> list[str]:
        """Read /hosts and return a deduplicated host list."""
        peers = await self.get_cluster_peers()
        return [peer["host"] for peer in peers]

    @property
    def ws_connected(self) -> bool:
        """Return True while the WebSocket link is up."""
        return self._ws_connected

    @property
    def ws_generation(self) -> int:
        """Return the number of authenticated WebSocket connections."""
        return self._ws_generation

    async def async_wait_for_reconnect(
        self, previous_generation: int, timeout: float = 300
    ) -> None:
        """Wait for an authenticated WebSocket connection newer than the given one."""
        async def wait_for_generation() -> None:
            while self._ws_generation <= previous_generation:
                self._ws_generation_changed.clear()
                if self._ws_generation > previous_generation:
                    return
                await self._ws_generation_changed.wait()

        await asyncio.wait_for(wait_for_generation(), timeout=timeout)

    async def async_refresh_info(self) -> dict[str, Any]:
        """Read fresh firmware info over RPC after a reconnect."""
        info = await self._rpc_request("info", {"sparse": "false"})
        new_data = dict(self.data or {})
        new_data["info"] = info
        self.async_set_updated_data(new_data)
        return info

    async def async_wait_connected(self) -> None:
        """Wait until the WebSocket is authenticated and ready for requests."""
        await asyncio.wait_for(self._ws_ready.wait(), timeout=10)

    # ------------------------------------------------------------------
    # DataUpdateCoordinator hook  (fallback poll)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch RPC state plus REST-only presets and groups."""
        try:
            versions = dict(self._light_command_versions)
            if self.virtual_lights is not None and time.monotonic() < self._next_metadata_refresh:
                lights = self.virtual_lights
                states = await self._read_virtual_light_states(lights)
                data = dict(self.data or {})
                default = next((light for light in lights.values() if light.is_default), None)
                state = states.get(default.id, {}) if default else {}
                return {
                    **data, "light_states": states,
                    "hsv": state.get("hsv", {}), "raw": state.get("raw", {}),
                }
            info_resp = await self._rpc_request("info", {"sparse": "false"})
            version = info_resp.get("app", {}).get("git_version", "")
            legacy = version.lstrip("Vv").startswith("5.0")
            try:
                config = await self.async_get_config(force_refresh=not legacy)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                if not legacy:
                    raise
                _LOGGER.warning("Unable to read legacy Lightinator /config: %s", err)
                self._config = config = {}
            lights = discover_lights(config, version)
            light_states = {}
            if lights is None:
                color_resp = await self._rpc_request("color", {})
            else:
                color_resp = {}
                light_states = await self._read_virtual_light_states(lights)
            async with self._data_lock:
                data_resp = await self._get("/data")
        except (aiohttp.ClientError, RpcError, asyncio.TimeoutError, ConnectionError, ValueError) as err:
            raise UpdateFailed(f"Error communicating with Lightinator: {err}") from err

        presets = (
            data_resp.get("presets", [])
            if isinstance(data_resp, dict)
            else []
        )
        controllers = (
            data_resp.get("controllers", [])
            if isinstance(data_resp, dict)
            else []
        )
        controller_id = str(self._entry.data.get(CONF_CHIP_ID, ""))
        controller_icon = next(
            (
                item.get("icon")
                for item in controllers
                if isinstance(item, dict)
                and str(item.get("id", "")) == controller_id
            ),
            None,
        )
        groups = (
            data_resp.get("groups", [])
            if isinstance(data_resp, dict)
            else []
        )
        current = self.data or {}
        if lights is not None:
            light_states = self._preserve_newer_targets(light_states, lights, versions)
            default = next((light for light in lights.values() if light.is_default), None)
            color_resp = light_states.get(default.id, {}) if default else {}
        self.update_interval = timedelta(
            seconds=(
                _LIGHT_POLL_INTERVAL
                if lights is not None and not self._light_updates_subscribed
                else UPDATE_INTERVAL
            )
        )
        self._next_metadata_refresh = time.monotonic() + UPDATE_INTERVAL
        self._light_configuration_pending = False
        return {
            "hsv": color_resp.get("hsv", current.get("hsv", {}) if lights is None else {}),
            "raw": color_resp.get("raw", current.get("raw", {}) if lights is None else {}),
            "lights": lights,
            "light_states": light_states,
            "info": info_resp,
            "config": self._safe_config_view(config),
            "presets": presets,
            "controller_icon": controller_icon,
            "groups": groups,
        }

    # ------------------------------------------------------------------
    # WebSocket listener
    # ------------------------------------------------------------------

    async def _rpc_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        wait_ready: bool = True,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request over the active WebSocket."""
        if wait_ready:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=10)
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionError("Lightinator WebSocket is not connected")

        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with self._send_lock:
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
            response = await asyncio.wait_for(future, timeout=10)
        finally:
            self._pending.pop(request_id, None)

        error = response.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            challenge = error.get("challenge")
            error_type = RpcAuthenticationError if code == -32001 else RpcError
            rpc_error = error_type(
                code,
                str(error.get("message", method)),
                challenge if isinstance(challenge, str) else None,
            )
            if not retry_auth or not isinstance(rpc_error, RpcAuthenticationError):
                raise rpc_error
            if not rpc_error.challenge:
                raise rpc_error

            async with self._auth_lock:
                if not self._authenticated:
                    auth_result = await self._rpc_request(
                        "authenticate",
                        {
                            "hash": authentication_hash(
                                rpc_error.challenge, self._password
                            )
                        },
                        wait_ready=wait_ready,
                        retry_auth=False,
                    )
                    if auth_result.get("authenticated") is not True:
                        raise RpcAuthenticationError(
                            None, "Invalid authentication response"
                        )
                    self._authenticated = True
            return await self._rpc_request(
                method, params, wait_ready=wait_ready, retry_auth=False
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RpcError(None, f"Invalid response for JSON-RPC method '{method}'")
        return result

    async def async_connect_ws(self) -> None:
        """Maintain an authenticated JSON-RPC WebSocket with reconnect backoff."""
        backoff = _WS_BACKOFF_INITIAL
        while not self._closing:
            reader: asyncio.Task | None = None
            try:
                ws_url = f"ws://{self._host}:{self._port}/ws"
                async with self._session.ws_connect(
                    ws_url, heartbeat=30
                ) as ws:
                    self._ws = ws
                    self._authenticated = False
                    self._next_request_id = 1
                    reader = asyncio.create_task(self._listen_ws(ws))
                    backoff = _WS_BACKOFF_INITIAL
                    _LOGGER.debug("Lightinator WS connected to %s", ws_url)
                    await self._rpc_request(
                        "subscribe", {"channel": "runtime_info"}, wait_ready=False
                    )
                    await self._async_subscribe_light_updates()
                    self._ws_connected = True
                    self._ws_generation += 1
                    self._ws_generation_changed.set()
                    self._next_metadata_refresh = 0
                    self._ws_ready.set()
                    await reader

            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.debug(
                    "Lightinator WS error (%s), retrying in %ds", err, backoff
                )
            finally:
                self._ws_ready.clear()
                self._ws_connected = False
                self._ws = None
                self._authenticated = False
                self._light_updates_subscribed = False
                self._fail_pending(ConnectionError("Lightinator WebSocket disconnected"))
                if reader and not reader.done():
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)

            if self._closing:
                return

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _WS_BACKOFF_MAX)

    async def _async_subscribe_light_updates(self) -> None:
        """Multiplex every light on the existing authenticated WebSocket."""
        self._light_updates_subscribed = False
        try:
            result = await self._rpc_request(
                "subscribe", {"channel": "lights"}, wait_ready=False
            )
        except RpcError as err:
            if err.code not in {-32601, -32602}:
                raise
            _LOGGER.info(
                "Controller does not support light notifications; using legacy updates"
            )
            return
        if result.get("subscribed") is not True or result.get("channel") != "lights":
            raise RpcError(None, "Invalid lights subscription response")
        self._light_updates_subscribed = True

    async def _listen_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Dispatch JSON-RPC responses and server notifications."""
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    if message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
                    continue
                try:
                    payload = message.json()
                except (ValueError, TypeError):
                    _LOGGER.debug("Ignoring invalid Lightinator WebSocket frame")
                    continue
                if not isinstance(payload, dict):
                    continue

                request_id = payload.get("id")
                future = self._pending.get(request_id) if isinstance(request_id, int) else None
                if future and not future.done():
                    future.set_result(payload)
                elif isinstance(payload.get("method"), str):
                    await self._handle_ws_message(payload)
        finally:
            self._fail_pending(ConnectionError("Lightinator WebSocket reader stopped"))

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    async def _handle_ws_message(self, msg: dict) -> None:
        """Process JSON-RPC notifications from the firmware."""
        method = msg.get("method")
        params = msg.get("params", {})
        if not isinstance(params, dict):
            return

        if method == "light_state":
            light_id = params.get("light_id")
            index = params.get("light")
            if (
                not isinstance(light_id, str)
                or not isinstance(index, int)
                or isinstance(index, bool)
            ):
                _LOGGER.warning("Invalid light notification identity: %s", params)
                return
            light = (self.virtual_lights or {}).get(light_id)
            if light is None or light.index != index:
                self._next_metadata_refresh = 0
                self._light_configuration_pending = True
                self.hass.async_create_task(self.async_request_refresh())
                return
            if not self._light_is_transitioning(light.id):
                try:
                    state = decode_color(params.get("state", {}))
                except ValueError as err:
                    _LOGGER.warning("Invalid light notification for %s: %s", light_id, err)
                    return
                self._publish_light_state(light, state)

        elif method == "lights_changed":
            self._next_metadata_refresh = 0
            self._light_configuration_pending = True
            self.hass.async_create_task(self.async_request_refresh())

        elif method == "color_event":
            if self.virtual_lights is not None:
                # Unindexed events identify no UUID. The lightweight poll reads
                # every light without exporting /config and /data per event.
                selector = params.get("light")
                if selector is None:
                    return
                else:
                    light = next(
                        (light for light in self.virtual_lights.values() if light.index == selector),
                        None,
                    )
                    if light is not None:
                        if not self._light_is_transitioning(light.id):
                            self._publish_light_state(light, decode_color(params))
                    else:
                        self._next_metadata_refresh = 0
                        self.hass.async_create_task(self.async_request_refresh())
                return
            new_data = dict(self.data or {})
            if "hsv" in params:
                new_data["hsv"] = params["hsv"]
            if "raw" in params:
                new_data["raw"] = params["raw"]
            self._publish_partial_data(new_data)
            self.hass.async_create_task(self._refresh_color_state())

        elif method == "runtime_info":
            new_data = dict(self.data or {})
            new_data["runtime"] = params
            info = dict(new_data.get("info", {}))
            info["runtime"] = params
            new_data["info"] = info
            self._publish_partial_data(new_data)

        elif method == "transition_finished":
            self.hass.bus.async_fire(
                f"{DOMAIN}_transition_finished",
                {
                    "name": params.get("name", ""),
                    "requeued": params.get("requeued", False),
                    "entry_id": self._entry.entry_id,
                    "light": params.get("light"),
                    "light_id": next(
                        (light.id for light in (self.virtual_lights or {}).values()
                         if light.index == params.get("light")),
                        None,
                    ),
                },
            )

        elif method == "ota_status":
            self.hass.bus.async_fire(
                f"{DOMAIN}_ota_status",
                {**params, "entry_id": self._entry.entry_id},
            )

    async def _refresh_color_state(self, light_id: str | None = None) -> None:
        """Read the complete color state after a one-of color notification."""
        try:
            if light_id is not None and light_id not in (self.virtual_lights or {}):
                _LOGGER.warning("Cannot refresh removed virtual light %s", light_id)
                return
            light = self.light(light_id) if light_id is not None else None
            version = self._light_command_versions.get(light_id, 0) if light_id is not None else 0
            if light_id is not None and self._light_is_transitioning(light_id):
                return
            color = await self._rpc_request("color", {"light": light.index} if light else {})
        except (
            aiohttp.ClientError,
            RpcError,
            asyncio.TimeoutError,
            ConnectionError,
        ) as err:
            _LOGGER.warning("Unable to refresh Lightinator color state: %s", err)
            return
        new_data = dict(self.data or {})
        if light is not None:
            current_light = (self.virtual_lights or {}).get(light.id)
            if (
                current_light != light
                or version != self._light_command_versions.get(light_id, 0)
                or self._light_is_transitioning(light.id)
            ):
                return
            try:
                state = decode_color(color)
            except ValueError as err:
                _LOGGER.warning("Invalid color state for virtual light %s: %s", light_id, err)
                return
            self._publish_light_state(light, state)
            return
        new_data["hsv"] = color.get("hsv", {})
        new_data["raw"] = color.get("raw", {})
        self._publish_partial_data(new_data)

    async def async_disconnect_ws(self) -> None:
        """Signal the WS listener to stop and close the socket."""
        self._closing = True
        for cancel in self._light_refresh_unsubs.values():
            cancel()
        self._light_refresh_unsubs.clear()
        self._ws_ready.clear()
        if self._ws and not self._ws.closed:
            await self._ws.close()

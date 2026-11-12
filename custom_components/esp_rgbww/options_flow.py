"""Options flow for Lightinator controller settings."""

from __future__ import annotations

import ipaddress
import logging
import xml.etree.ElementTree as ET
from copy import deepcopy
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_DEVICE_NAME,
    CONF_FIRMWARE_BRANCH,
    CONF_FIRMWARE_RELEASE_LINE,
    CONF_FIRMWARE_TYPE,
    DOMAIN,
)
from .coordinator import LightinatorCoordinator
from .firmware_api import FirmwareVersionApi, firmware_selection
from .virtual_lights import CHANNELS, CHANNEL_LABELS

_LOGGER = logging.getLogger(__name__)
_DEFAULT_COLOR_MODELS = ["RGB", "RGBWW", "RGBCW", "RGBWWCW"]
_TRANSITION_MODES = ["Normal", "Spektrum", "Rainbow"]


def _ipv4(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as err:
        raise vol.Invalid("Enter a valid IPv4 address") from err


class LightinatorOptionsFlow(OptionsFlow):
    """Edit non-secret controller settings through ConfigDB partial updates."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._light_edit_config: dict[str, Any] | None = None
        self._new_light: dict[str, Any] = {}
        self._hardware_edit: dict[str, Any] | None = None
        self._boards: dict[str, dict[str, Any]] = {}

    def _coordinator(self) -> LightinatorCoordinator:
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if coordinator is None:
            _LOGGER.warning(
                "Cannot configure Lightinator entry %s: controller setup is not loaded",
                self._entry.entry_id,
            )
            raise AbortFlow("controller_not_loaded")
        return coordinator

    async def _config(self) -> dict[str, Any]:
        return await self._coordinator().async_get_config(force_refresh=True)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=(
                ["hardware", "create_light", "delete_light", "network", "firmware", "icon"]
                if self._coordinator().virtual_lights is not None
                else ["color", "network", "firmware", "icon"]
            )
        )

    async def async_step_hardware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a board revision and controller-wide PWM settings."""
        coordinator = self._coordinator()
        if coordinator.virtual_lights is None:
            return await self.async_step_init()
        errors: dict[str, str] = {}
        try:
            if user_input is None or self._hardware_edit is None:
                config = await self._config()
                hardware = config["hardware"]
                soc = str((coordinator.data or {}).get("info", {}).get("device", {}).get("soc", "")).lower()
                if soc not in ("esp32", "esp32c3"):
                    raise ValueError("Controller SoC is unavailable")
                catalog = await coordinator.async_get_hardware_catalog()
                self._boards = {
                    f'{board["id"]}:{board["revision"]}': board
                    for board in catalog["boards"] if board["soc"].lower() == soc
                }
                self._hardware_edit = deepcopy({
                    "configuration": hardware["configuration"], "pwm": hardware.get("pwm", {})
                })
            if user_input is not None:
                if not user_input.get("confirm"):
                    errors["confirm"] = "confirmation_required"
                else:
                    board_key = user_input["board"]
                    board = self._boards.get(board_key)
                    if board_key and board is None:
                        raise ValueError("Selected board is no longer available")
                    pwm = {
                        "timer": {key: user_input[f"pwm_{key}"] for key in ("speed_mode", "frequency", "resolution", "number")},
                        "spreadSpectrum": {
                            "mode": "ON" if user_input["spread_enabled"] else "OFF",
                            "width": user_input["spread_width"],
                            "subsampling": user_input["spread_subsampling"],
                        },
                        "phaseShift": {"mode": "ON" if user_input["phase_enabled"] else "OFF"},
                    }
                    await coordinator.async_update_hardware(
                        board["id"] if board else "", board["revision"] if board else 0,
                        pwm, expected_hardware=self._hardware_edit,
                    )
                    return self.async_create_entry(title="", data=dict(self._entry.options))
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
            _LOGGER.warning("Unable to configure hardware: %s", err)
            errors["base"] = "hardware_configuration_failed"
        if self._hardware_edit is None:
            return self.async_show_form(step_id="hardware", data_schema=vol.Schema({}), errors=errors)
        configuration = self._hardware_edit["configuration"]
        pwm = self._hardware_edit["pwm"]
        timer = pwm.get("timer", {})
        spread = pwm.get("spreadSpectrum", {})
        board_key = f'{configuration.get("board_id")}:{configuration.get("board_revision")}' if configuration.get("board_id") else ""
        choices = [{"value": "", "label": "Custom GPIOs (no board profile)"}] + [
            {"value": key, "label": f'{board["name"]} (rev {board["revision"]})'}
            for key, board in self._boards.items()
        ]
        if board_key and board_key not in self._boards:
            choices.append({"value": board_key, "label": f"Current unavailable board: {board_key}"})
        modes = ["LOWSPEED", "HIGHSPEED"] if configuration.get("soc") == "esp32" else ["LOWSPEED"]
        defaults = {
            "board": board_key, "pwm_speed_mode": timer.get("speed_mode", "LOWSPEED"),
            "pwm_frequency": timer.get("frequency", 1000),
            "pwm_resolution": timer.get("resolution", 10), "pwm_number": timer.get("number", 0),
            "spread_enabled": spread.get("mode", "ON") == "ON",
            "spread_width": spread.get("width", 15), "spread_subsampling": spread.get("subsampling", 4),
            "phase_enabled": pwm.get("phaseShift", {}).get("mode", "ON") == "ON", "confirm": False,
        }
        if user_input is not None:
            defaults.update(user_input)
        validators = {
            "board": SelectSelector(SelectSelectorConfig(options=choices, mode=SelectSelectorMode.DROPDOWN)),
            "pwm_speed_mode": SelectSelector(SelectSelectorConfig(options=modes, mode=SelectSelectorMode.DROPDOWN)),
            "pwm_frequency": vol.All(int, vol.Range(min=1, max=40000000)),
            "pwm_resolution": vol.All(int, vol.Range(min=1, max=14)),
            "pwm_number": vol.All(int, vol.Range(min=0, max=3)),
            "spread_enabled": bool, "spread_width": vol.All(int, vol.Range(min=0, max=100)),
            "spread_subsampling": vol.All(int, vol.Range(min=1, max=65535)),
            "phase_enabled": bool, "confirm": bool,
        }
        return self.async_show_form(
            step_id="hardware",
            data_schema=vol.Schema({vol.Required(key, default=defaults[key]): validator for key, validator in validators.items()}),
            errors=errors,
        )

    async def async_step_create_light(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._coordinator().virtual_lights is None:
            return await self.async_step_init()
        if user_input is not None:
            self._new_light = dict(user_input)
            return await self.async_step_light_pins()
        return self.async_show_form(
            step_id="create_light",
            data_schema=vol.Schema({
                vol.Required("name"): vol.All(str, vol.Length(min=1, max=64)),
                vol.Required("light_type", default="SINGLE"): SelectSelector(
                    SelectSelectorConfig(options=list(CHANNELS), mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required("is_default", default=False): bool,
            }),
        )

    async def async_step_light_pins(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._coordinator().virtual_lights is None:
            return await self.async_step_init()
        if not self._new_light:
            return await self.async_step_create_light()
        errors: dict[str, str] = {}
        channels = CHANNELS[self._new_light["light_type"]]
        try:
            if user_input is None or self._light_edit_config is None:
                self._light_edit_config = deepcopy((await self._config())["hardware"]["configuration"])
            if user_input is not None:
                await self._coordinator().async_manage_light(
                    **self._new_light, pins=[user_input[channel] for channel in channels],
                    expected_configuration=self._light_edit_config,
                )
                return self.async_create_entry(title="", data=dict(self._entry.options))
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
            _LOGGER.warning("Unable to create light: %s", err)
            errors["base"] = "light_management_failed"
        return self.async_show_form(
            step_id="light_pins",
            data_schema=vol.Schema({
                vol.Required(channel): vol.All(int, vol.Range(min=0, max=255))
                for channel in channels
            }),
            errors=errors,
            description_placeholders={
                "channels": ", ".join(f"{channel}: {CHANNEL_LABELS[channel]}" for channel in channels)
            },
        )

    async def async_step_delete_light(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        coordinator = self._coordinator()
        if coordinator.virtual_lights is None:
            return await self.async_step_init()
        errors: dict[str, str] = {}
        try:
            if user_input is None or self._light_edit_config is None:
                self._light_edit_config = deepcopy((await self._config())["hardware"]["configuration"])
            if user_input is not None:
                if not user_input.get("confirm"):
                    errors["confirm"] = "confirmation_required"
                else:
                    await coordinator.async_manage_light(
                        delete_id=user_input["light_id"],
                        replacement_default=user_input.get("replacement_default"),
                        expected_configuration=self._light_edit_config,
                    )
                    return self.async_create_entry(title="", data=dict(self._entry.options))
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
            _LOGGER.warning("Unable to delete light: %s", err)
            errors["base"] = "light_management_failed"
        lights = (self._light_edit_config or {}).get("lights", [])
        if not lights and not errors:
            return self.async_abort(reason="no_lights")
        choices = [{"value": light["id"], "label": light["name"] or light["id"]} for light in lights]
        return self.async_show_form(
            step_id="delete_light",
            data_schema=vol.Schema({
                vol.Required("light_id"): SelectSelector(
                    SelectSelectorConfig(options=choices, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional("replacement_default"): SelectSelector(
                    SelectSelectorConfig(options=choices, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required("confirm", default=False): bool,
            }),
            errors=errors,
        )

    def _firmware_api(self, config: dict[str, Any]) -> FirmwareVersionApi:
        ota = config.get("ota", {})
        catalog_url = ota.get("url", "") if isinstance(ota, dict) else ""
        if not catalog_url:
            raise ValueError("The controller has no firmware catalog URL")
        return FirmwareVersionApi(
            async_get_clientsession(self.hass), str(catalog_url)
        )

    async def async_step_firmware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a firmware branch and build type from the version API."""
        errors: dict[str, str] = {}
        try:
            config = await self._config()
            info = (self._coordinator().data or {}).get("info", {})
            device = info.get("device", {}) if isinstance(info, dict) else {}
            app = info.get("app", {}) if isinstance(info, dict) else {}
            device = device if isinstance(device, dict) else {}
            app = app if isinstance(app, dict) else {}
            soc = str(device.get("soc", "")).lower()
            if not soc:
                raise ValueError("Controller SoC is unavailable")
            api = self._firmware_api(config)
            combinations = await api.options(soc)
            branches = sorted({item["stage"] for item in combinations})
            release_lines = sorted({item["release_line"] for item in combinations}, key=lambda value: tuple(map(int, value.split("."))))
            build_types = sorted({item["build_type"] for item in combinations})
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Unable to load firmware choices: %s", err)
            branches = []
            build_types = []
            errors["base"] = "firmware_api_unavailable"

        if not branches or not build_types:
            errors.setdefault("base", "firmware_api_unavailable")
            return self.async_show_form(
                step_id="firmware",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        installed = str(app.get("git_version", ""))
        try:
            default_line, default_branch = firmware_selection(
                installed,
                str(self._entry.options.get(CONF_FIRMWARE_BRANCH, "")),
                str(self._entry.options.get(CONF_FIRMWARE_RELEASE_LINE, "")),
            )
        except ValueError:
            default_line, default_branch = "", ""
        if default_line not in release_lines:
            default_line = ""
        if default_branch not in branches:
            default_branch = ""

        installed_type = str(app.get("build_type", "")).lower()
        default_type = self._entry.options.get(CONF_FIRMWARE_TYPE, installed_type)
        if default_type not in build_types:
            default_type = ""

        if user_input is not None:
            selected_branch = user_input[CONF_FIRMWARE_BRANCH]
            selected_line = user_input[CONF_FIRMWARE_RELEASE_LINE]
            selected_type = user_input[CONF_FIRMWARE_TYPE]
            default_line = selected_line
            if selected_branch in branches:
                default_branch = selected_branch
            if selected_type in build_types:
                default_type = selected_type
            try:
                if {"release_line": selected_line, "stage": selected_branch, "build_type": selected_type} not in combinations:
                    raise ValueError("Unavailable firmware option combination")
                versions = await api.versions(soc, f"{selected_line}/{selected_branch}", selected_type)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.debug("Unable to validate firmware selection: %s", err)
                errors["base"] = "firmware_api_unavailable"
            else:
                if not versions:
                    errors["base"] = "firmware_combination_unavailable"
                else:
                    new_options = dict(self._entry.options)
                    new_options[CONF_FIRMWARE_BRANCH] = selected_branch
                    new_options[CONF_FIRMWARE_RELEASE_LINE] = selected_line
                    new_options[CONF_FIRMWARE_TYPE] = selected_type
                    self.hass.config_entries.async_update_entry(
                        self._entry, options=new_options
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(self._entry.entry_id)
                    )
                    return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="firmware",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FIRMWARE_RELEASE_LINE, default=default_line): SelectSelector(
                        SelectSelectorConfig(options=release_lines, mode=SelectSelectorMode.DROPDOWN)
                    ),
                    vol.Required(
                        CONF_FIRMWARE_BRANCH, default=default_branch
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=branches,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_FIRMWARE_TYPE, default=default_type): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": item, "label": item.title()}
                                for item in build_types
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_icon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select and persist an icon from the controller's local SVG sprite."""
        coordinator = self._coordinator()
        errors: dict[str, str] = {}
        try:
            icons = await coordinator.async_get_controller_icons()
        except (aiohttp.ClientError, TimeoutError, ValueError, ET.ParseError) as err:
            _LOGGER.debug("Unable to load controller icon sprite: %s", err)
            icons = []
            errors["base"] = "controller_icon_unavailable"

        if not icons:
            errors.setdefault("base", "controller_icon_unavailable")
            return self.async_show_form(
                step_id="icon",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        current_icon = (coordinator.data or {}).get("controller_icon")
        current_icon = current_icon if isinstance(current_icon, str) else ""
        options: list[str | dict[str, str]] = [
            {
                "value": icon,
                "label": icon.replace("_", " ").replace("-", " ").title(),
            }
            for icon in icons
        ]
        if current_icon and current_icon not in icons:
            options.insert(0, {"value": current_icon, "label": "Current custom icon"})

        default_icon = current_icon if current_icon else "led-strip-variant"
        if default_icon not in {icon if isinstance(icon, str) else icon["value"] for icon in options}:
            default_icon = icons[0]

        if user_input is not None:
            selected_icon = str(user_input["controller_icon"])
            if selected_icon != current_icon:
                try:
                    await coordinator.async_update_controller_icon(selected_icon)
                except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                    _LOGGER.debug("Unable to save controller icon: %s", err)
                    errors["base"] = "controller_icon_unavailable"
                else:
                    return self.async_create_entry(
                        title="", data=dict(self._entry.options)
                    )
            else:
                return self.async_create_entry(
                    title="", data=dict(self._entry.options)
                )

        return self.async_show_form(
            step_id="icon",
            data_schema=vol.Schema(
                {
                    vol.Required("controller_icon", default=default_icon): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_color(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._coordinator().virtual_lights is not None:
            return await self.async_step_init()
        errors: dict[str, str] = {}
        try:
            config = await self._config()
        except (aiohttp.ClientError, TimeoutError):
            config = {}
            errors["base"] = "cannot_connect"

        general = config.get("general", {})
        color = config.get("color", {})
        general = general if isinstance(general, dict) else {}
        color = color if isinstance(color, dict) else {}
        brightness = color.get("brightness", {})
        colortemp = color.get("colortemp", {})
        hsv = color.get("hsv", {})
        brightness = brightness if isinstance(brightness, dict) else {}
        colortemp = colortemp if isinstance(colortemp, dict) else {}
        hsv = hsv if isinstance(hsv, dict) else {}

        models = general.get("supported_color_models", _DEFAULT_COLOR_MODELS)
        if not isinstance(models, list) or not models:
            models = _DEFAULT_COLOR_MODELS
        current_model = color.get("color_mode", 0)
        selected_model = (
            models[current_model]
            if isinstance(current_model, int) and 0 <= current_model < len(models)
            else models[0]
        )
        transition_index = hsv.get("model", 0)
        transition_mode = (
            _TRANSITION_MODES[transition_index]
            if isinstance(transition_index, int)
            and 0 <= transition_index < len(_TRANSITION_MODES)
            else _TRANSITION_MODES[0]
        )

        if user_input is not None and not errors:
            try:
                color_update = {
                    "color_mode": models.index(user_input["color_model"]),
                    "brightness": {
                        channel: user_input[f"brightness_{channel}"]
                        for channel in ("red", "green", "blue", "ww", "cw")
                    },
                    "colortemp": {
                        "ww": user_input["colortemp_ww"],
                        "cw": user_input["colortemp_cw"],
                    },
                    "hsv": {
                        "model": _TRANSITION_MODES.index(
                            user_input["transition_mode"]
                        ),
                        **{
                            channel: user_input[f"hsv_{channel}"]
                            for channel in (
                                "red", "yellow", "green", "cyan", "blue", "magenta"
                            )
                        },
                    },
                }
                await self._coordinator().async_update_config({"color": color_update})
                if color_update["color_mode"] != current_model:
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(self._entry.entry_id)
                    )
                return self.async_create_entry(title="", data={})
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.exception("Unable to update Lightinator color settings: %s", err)
                errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required("color_model", default=selected_model): SelectSelector(
                    SelectSelectorConfig(
                        options=models,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    "transition_mode", default=transition_mode
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_TRANSITION_MODES,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                **{
                    vol.Required(
                        f"brightness_{channel}",
                        default=brightness.get(channel, 100),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100))
                    for channel in ("red", "green", "blue", "ww", "cw")
                },
                vol.Required(
                    "colortemp_ww", default=colortemp.get("ww", 2700)
                ): vol.All(vol.Coerce(int), vol.Range(min=1000, max=10000)),
                vol.Required(
                    "colortemp_cw", default=colortemp.get("cw", 6000)
                ): vol.All(vol.Coerce(int), vol.Range(min=1000, max=10000)),
                **{
                    vol.Required(f"hsv_{channel}", default=hsv.get(channel, 0)): vol.All(
                        vol.Coerce(int), vol.Range(min=-30, max=30)
                    )
                    for channel in (
                        "red", "yellow", "green", "cyan", "blue", "magenta"
                    )
                },
            }
        )
        return self.async_show_form(
            step_id="color", data_schema=schema, errors=errors
        )

    async def async_step_network(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        try:
            config = await self._config()
        except (aiohttp.ClientError, TimeoutError):
            config = {}
            errors["base"] = "cannot_connect"

        general = config.get("general", {})
        network = config.get("network", {})
        general = general if isinstance(general, dict) else {}
        network = network if isinstance(network, dict) else {}
        connection = network.get("connection", {})
        mqtt = network.get("mqtt", {})
        homeassistant = mqtt.get("homeassistant", {}) if isinstance(mqtt, dict) else {}
        rsyslog = network.get("rsyslog", {})
        telemetry = network.get("telemetry", {})
        security = config.get("security", {})
        connection = connection if isinstance(connection, dict) else {}
        mqtt = mqtt if isinstance(mqtt, dict) else {}
        homeassistant = homeassistant if isinstance(homeassistant, dict) else {}
        rsyslog = rsyslog if isinstance(rsyslog, dict) else {}
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        security = security if isinstance(security, dict) else {}

        if user_input is not None and not errors:
            try:
                ip_value = _ipv4(user_input["ip"])
                netmask_value = _ipv4(user_input["netmask"])
                gateway_value = _ipv4(user_input["gateway"])
                if not user_input["dhcp"]:
                    subnet = ipaddress.IPv4Network(
                        f"{ip_value}/{netmask_value}", strict=False
                    )
                    if ipaddress.IPv4Address(gateway_value) not in subnet:
                        errors["base"] = "invalid_network"

                if not errors:
                    network_update: dict[str, Any] = {
                        "connection": {
                            "dhcp": user_input["dhcp"],
                            "ip": ip_value,
                            "netmask": netmask_value,
                            "gateway": gateway_value,
                        },
                        "mqtt": {
                            "enabled": user_input["mqtt_enabled"],
                            "server": user_input["mqtt_server"],
                            "port": user_input["mqtt_port"],
                            "topic_base": user_input["mqtt_topic_base"],
                            "username": user_input["mqtt_username"],
                            "homeassistant": {
                                "enable": user_input["ha_discovery_enabled"],
                                "discovery_prefix": user_input["ha_discovery_prefix"],
                                "node_id": user_input["ha_node_id"],
                            },
                        },
                        "rsyslog": {
                            "enabled": user_input["rsyslog_enabled"],
                            "host": user_input["rsyslog_host"],
                            "port": user_input["rsyslog_port"],
                        },
                        "telemetry": {
                            "statsEnabled": user_input["telemetry_stats_enabled"],
                            "logEnabled": user_input["telemetry_log_enabled"],
                            "url": user_input["telemetry_url"],
                            "user": user_input["telemetry_user"],
                        },
                    }
                    if user_input.get("mqtt_clear_password"):
                        network_update["mqtt"]["password"] = ""
                    elif user_input.get("mqtt_password", "").strip():
                        network_update["mqtt"]["password"] = user_input[
                            "mqtt_password"
                        ].strip()
                    if user_input.get("telemetry_clear_password"):
                        network_update["telemetry"]["password"] = ""
                    elif user_input.get("telemetry_password", "").strip():
                        network_update["telemetry"]["password"] = user_input[
                            "telemetry_password"
                        ].strip()

                    device_name = user_input["device_name"].strip()
                    update: dict[str, Any] = {"network": network_update}
                    if device_name:
                        update["general"] = {"device_name": device_name}

                    old_secured = bool(security.get("api_secured", False))
                    new_secured = bool(user_input["api_secured"])
                    new_api_password = user_input.get("api_password", "").strip()
                    entry_password = self._entry.data.get(CONF_PASSWORD, "")
                    if new_secured and not (new_api_password or entry_password):
                        errors["base"] = "password_required"
                    if not errors:
                        if new_secured:
                            security_update: dict[str, Any] = {
                                "api_secured": True
                            }
                            if new_api_password:
                                security_update["api_password"] = new_api_password
                            elif old_secured:
                                security_update["api_password"] = (
                                    security.get("api_password") or entry_password
                                )
                        else:
                            security_update = {
                                "api_secured": False,
                                "api_password": "",
                            }
                        update["security"] = security_update

                    previous_mqtt = mqtt
                    next_mqtt = {**mqtt, **network_update["mqtt"]}
                    previous_ha = mqtt.get("homeassistant", {})
                    next_ha = {
                        **(previous_ha if isinstance(previous_ha, dict) else {}),
                        **network_update["mqtt"]["homeassistant"],
                    }
                    next_mqtt["homeassistant"] = next_ha
                    mqtt_reconfigure = (
                        previous_mqtt.get("enabled")
                        and next_mqtt.get("enabled")
                        and any(
                            previous_mqtt.get(key) != next_mqtt.get(key)
                            for key in ("server", "port", "topic_base", "username", "password")
                        )
                    ) or (
                        previous_mqtt.get("enabled")
                        and next_mqtt.get("enabled")
                        and previous_ha != next_ha
                    )

                    previous_telemetry = telemetry
                    next_telemetry = {**telemetry, **network_update["telemetry"]}
                    telemetry_reconfigure = (
                        previous_telemetry.get("statsEnabled")
                        and next_telemetry.get("statsEnabled")
                        and any(
                            previous_telemetry.get(key) != next_telemetry.get(key)
                            for key in ("url", "user", "password")
                        )
                    )
                    network_connection_changed = any(
                        connection.get(key) != network_update["connection"].get(key)
                        for key in ("dhcp", "ip", "netmask", "gateway")
                    ) and not user_input["dhcp"]

                    if errors:
                        raise ValueError("API password is required when protection is enabled")
                    await self._coordinator().async_update_config(update)

                    if (mqtt_reconfigure or telemetry_reconfigure) and not network_connection_changed:
                        try:
                            await self._coordinator().async_post_http(
                                "/system", {"cmd": "restart"}
                            )
                        except aiohttp.ClientError:
                            _LOGGER.warning(
                                "Network settings saved; controller restart may be needed to apply broker/telemetry changes"
                            )

                    expected_host = (
                        ip_value
                        if not user_input["dhcp"]
                        else self._entry.data[CONF_HOST]
                    )
                    new_entry_data = dict(self._entry.data)
                    new_entry_data[CONF_HOST] = expected_host
                    new_entry_data[CONF_DEVICE_NAME] = (
                        device_name or self._entry.data[CONF_DEVICE_NAME]
                    )
                    credential_changed = (
                        new_api_password != entry_password
                        if new_api_password
                        else old_secured != new_secured
                    )
                    new_entry_data[CONF_PASSWORD] = (
                        new_api_password
                        if new_api_password
                        else entry_password if new_secured else ""
                    )
                    host_changed = expected_host != self._entry.data[CONF_HOST]
                    name_changed = (
                        new_entry_data[CONF_DEVICE_NAME]
                        != self._entry.data[CONF_DEVICE_NAME]
                    )
                    if host_changed or name_changed:
                        self.hass.config_entries.async_update_entry(
                            self._entry,
                            data=new_entry_data,
                            title=new_entry_data[CONF_DEVICE_NAME],
                        )
                    if host_changed or credential_changed:
                        self.hass.async_create_task(
                            self.hass.config_entries.async_reload(self._entry.entry_id)
                        )
                    return self.async_create_entry(title="", data={})
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.exception("Unable to update Lightinator network settings: %s", err)
                if "base" not in errors:
                    errors["base"] = (
                        "invalid_network"
                        if isinstance(err, vol.Invalid)
                        else "cannot_connect"
                    )

        schema = vol.Schema(
            {
                vol.Required("device_name", default=general.get("device_name", "")): str,
                vol.Required(
                    "api_secured", default=security.get("api_secured", False)
                ): bool,
                vol.Optional("api_password"): str,
                vol.Required("dhcp", default=connection.get("dhcp", True)): bool,
                vol.Required("ip", default=connection.get("ip", "192.168.1.1")): str,
                vol.Required("netmask", default=connection.get("netmask", "255.255.255.0")): str,
                vol.Required("gateway", default=connection.get("gateway", "192.168.1.255")): str,
                vol.Required("mqtt_enabled", default=mqtt.get("enabled", False)): bool,
                vol.Required("mqtt_server", default=mqtt.get("server", "mqtt.local")): str,
                vol.Required("mqtt_port", default=mqtt.get("port", 1883)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required("mqtt_topic_base", default=mqtt.get("topic_base", "home/")): str,
                vol.Required("mqtt_username", default=mqtt.get("username", "")): str,
                vol.Optional("mqtt_password"): str,
                vol.Optional("mqtt_clear_password", default=False): bool,
                vol.Required("ha_discovery_enabled", default=homeassistant.get("enable", True)): bool,
                vol.Required("ha_discovery_prefix", default=homeassistant.get("discovery_prefix", "homeassistant")): str,
                vol.Required("ha_node_id", default=homeassistant.get("node_id", "")): str,
                vol.Required("rsyslog_enabled", default=rsyslog.get("enabled", False)): bool,
                vol.Required("rsyslog_host", default=rsyslog.get("host", "")): str,
                vol.Required("rsyslog_port", default=rsyslog.get("port", 514)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required("telemetry_stats_enabled", default=telemetry.get("statsEnabled", False)): bool,
                vol.Required("telemetry_log_enabled", default=telemetry.get("logEnabled", False)): bool,
                vol.Required("telemetry_url", default=telemetry.get("url", "")): str,
                vol.Required("telemetry_user", default=telemetry.get("user", "")): str,
                vol.Optional("telemetry_password"): str,
                vol.Optional("telemetry_clear_password", default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="network", data_schema=schema, errors=errors
        )
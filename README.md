# Lightinator — Home Assistant Custom Integration

<img align="right" width="96" height="96" src="custom_components/esp_rgbww/brand/icon-dark.svg" alt="Lightinator Home Assistant icon">

A native Home Assistant integration for the [ESP RGBWW Firmware](https://github.com/pljakobs/esp_rgbww_firmware) RGBWW LED controller.

## Features

- **JSON-RPC WebSocket** for light state, commands, and runtime updates; HTTP is retained for presets/groups and cluster-host discovery, which the firmware does not expose over RPC
- **Automatic discovery** via mDNS (Zeroconf) — no IP configuration needed
- **Full light entity**: HS colour + colour-temperature (2703 K – 6536 K), brightness, transitions
- **RAW channel controls**: separate brightness lights for each active RGB, warm-white, and cool-white channel, based on the controller's color model
- **Preset select** — apply saved colour presets from the device
- **Save preset service** — save the current HSV or raw color under a preset name
- **Controller settings** — color model, channel correction, white balance, DHCP/static addressing, MQTT, syslog, telemetry, and API security
- **Firmware update entity** — selects compatible ROMs through the Lightinator Version API, with branch and debug/release choices
- **Wi-Fi services** — scan networks or connect a controller to an SSID
- **Animation controls** — Stop, Skip, Pause, Resume buttons
- **Diagnostic sensors** — uptime, free heap, WiFi IP

## Installation

### HACS (recommended)

1. In HACS → Integrations, click the three-dot menu → *Custom repositories*.
2. Add `https://github.com/pljakobs/Lightinator_HA_module` as an **Integration**.
3. Search for **Lightinator** and install.
4. Restart Home Assistant.

### Manual

Copy the `custom_components/esp_rgbww/` folder into your HA `config/custom_components/` directory and restart.

## Configuration

Devices are discovered automatically via mDNS (`_lightinator-api._tcp.local.`), using the controller's chip ID for stable identity.
You can also add a device manually via **Settings → Devices & Services → Add Integration → Lightinator**.

Use the integration's **Configure** action for color and network settings. Static
IP, netmask, gateway, and DHCP are submitted together. Some changes reboot the
controller; its stable `/info` device ID keeps the HA device identity unchanged.
The firmware update entity derives the Lightinator Version API URL from the
controller's configured OTA catalog URL. Use its Firmware options to choose an
available branch and debug or release build; available choices are filtered by
the controller's SoC.

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `light.<name>` | Light | Main RGBWW light (HS + colour-temp) |
| `light.<name>_red` / `_green` / `_blue` | Light | Direct raw brightness for RGB channels when enabled by the controller's color model |
| `light.<name>_warm_white` | Light | Warm-white channel (raw PWM) |
| `light.<name>_cool_white` | Light | Cool-white channel (raw PWM) |
| `select.<name>_preset` | Select | Apply a saved colour preset |
| `button.<name>_stop` | Button | Stop animation |
| `button.<name>_skip` | Button | Skip animation step |
| `button.<name>_pause` | Button | Pause animation |
| `button.<name>_resume` | Button | Resume animation |
| `sensor.<name>_uptime` | Sensor | Device uptime (seconds) |
| `sensor.<name>_free_heap` | Sensor | Free heap memory (bytes) |
| `sensor.<name>_wifi_connected` | Sensor | WiFi connection status |
| `sensor.<name>_ip_address` | Sensor | Device IP address |
| `update.<name>_firmware` | Update | Install a compatible controller ROM |

The `esp_rgbww.scan_wifi` and `esp_rgbww.connect_wifi` services start a scan and
connect a controller to an SSID. The `esp_rgbww.save_preset` service stores the
current HSV or raw channel state as a named controller preset.

## Colour Temperature

The firmware's `hsv.ct` value is a Kelvin temperature, not a 0–100 percentage.
The integration sends and reads it directly. Home Assistant displays the light
over its supported range of 2703–6536 K and handles Kelvin/mired conversion.

## License

MIT — see [LICENSE](LICENSE).

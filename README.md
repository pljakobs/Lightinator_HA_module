# Lightinator — Home Assistant Custom Integration

<img align="right" width="96" height="96" src="custom_components/esp_rgbww/brand/icon-dark.svg" alt="Lightinator Home Assistant icon">

A native Home Assistant integration for the [ESP RGBWW Firmware](https://github.com/pljakobs/esp_rgbww_firmware) RGBWW LED controller.

## Features

- **JSON-RPC WebSocket** for light state, commands, and runtime updates; HTTP is retained for presets/groups and cluster-host discovery, which the firmware does not expose over RPC
- **Automatic discovery** via mDNS (Zeroconf) — no IP configuration needed
- **5.0 compatibility**: the existing main light, raw channels, entity identities and untargeted API remain supported
- **5.1 virtual lights**: one primary light entity per configured firmware light, sharing one controller device and WebSocket
- **Profile-aware controls**: brightness for SINGLE/WW/CW, brightness and color temperature for WWCW, HS for RGB, RGBW for RGBWW/RGBCW, and HS/color temperature/RGBWW for RGBWWCW
- **RAW channel controls**: optional per-channel brightness lights (disabled by default for new 5.1 entities)
- **Preset select** — apply saved colour presets from the device
- **Favorite preset swatches** — show controller-favorite colors in the native Home Assistant light dialog
- **Save preset service** — save the current HSV or raw color under a preset name
- **Controller settings** — color model, channel correction, white balance, DHCP/static addressing, MQTT, syslog, telemetry, and API security
- **Controller icon** — select from the controller's local SVG sprite; recognized icons are reflected on the Home Assistant light
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
Use **Controller icon** in Configure to choose a locally available controller
icon; the selected sprite ID is saved to the controller's app-data.
On 5.1, configure light types, pins and per-light calibration in the controller's
webapp. The legacy controller-wide **Color** form is only offered on 5.0.
The firmware update entity derives the Lightinator Version API URL from the
controller's configured OTA catalog URL. Use its Firmware options to choose an
available branch and debug or release build; available choices are filtered by
the controller's SoC.

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `light.<name>` | Light | 5.0 main RGBWW light; on 5.1, one profile-aware primary entity per configured light |
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

### Virtual lights (5.1)

Each configured light is identified by controller chip ID plus its persistent
firmware light UUID. Names, list order, numeric API indexes and the configured
default may change without rebinding an entity. A single authenticated WebSocket
per controller subscribes to both runtime and light updates. On firmware with
the `lights` channel, UUID-tagged state notifications update all lights,
including non-default outputs. Configuration-change notifications trigger
discovery; a 30-second fallback refresh is retained and cannot be postponed by
runtime heartbeats. Removed lights/channels become unavailable.

On that firmware, commands send both the current index and a UUID guard checked
by the controller, avoiding a `/config` download per slider movement. HA publishes
the acknowledged target, ignores intermediate transition readbacks, and
reconciles once after the fade completes. In-flight older reads cannot overwrite
a newer command.

Older 5.1 firmware without this subscription uses lightweight one-second color
polls, a separate 30-second metadata deadline, and the per-command configuration
check needed to avoid reused-index misrouting. These firmware limitations do
not change the 5.0 entity or command path.

When upgrading an existing integration from 5.0, the existing main entity is
bound **once** to the configured default light, and that UUID is saved in the
config entry. Its old unique ID and entity ID are retained, as are matching
existing raw-channel entities. Later default changes do not redirect them.
If that UUID is deleted, those entities remain unavailable; a replacement UUID
creates new entities. New installations use UUID-based identities throughout.

Enable optional raw channels from the device's entity list. WWCW exposes warm
and cool white separately; the primary entity remains a tunable-white light.
The firmware reports either HSV or RAW, not both. Raw-channel brightness is
unknown while the light reports HSV; the integration does not invent PWM
readings. An indexed RAW command changes only the requested channel.

The preset selector and controller animation buttons continue to act on the
controller's **current default** light. `fade_sequence`, `blink` and
`save_preset` accept an optional `light_id` (firmware UUID) on 5.1 for explicit
targeting; omitting it preserves their legacy default-light behavior.
An invalid or removed UUID raises an error and never targets the default.
Standard HA light services, scenes and automations target individual entities.
The firmware preset store does not support indexed RAW arrays. Saving a
single-channel/WWCW RAW state as a controller preset therefore reports an error;
use an HA scene instead. HSV and named RGB RAW presets retain their existing
formats.

## Colour Temperature

The firmware's `hsv.ct` value is a Kelvin temperature, not a 0–100 percentage.
The integration sends and reads it directly. Home Assistant displays the light
over its supported range of 2703–6536 K and handles Kelvin/mired conversion.
On 5.1 the range comes from each light's configured warm/cool temperatures.
HSV readback radians and 0–1 saturation/brightness are converted to degrees and
percent before use in HA or subsequent commands. This conversion is not applied
to the legacy 5.0 path.

## Development tests

With Home Assistant installed in a Python environment, run:

```sh
python -m unittest -v test_firmware_api test_virtual_lights
```

The virtual-light tests exercise discovery, profile controls, UUID migration,
per-light command/state isolation and unchanged 5.0 setup/commands.

## License

MIT — see [LICENSE](LICENSE).

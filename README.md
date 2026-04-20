# Lightinator — Home Assistant Custom Integration

A native Home Assistant integration for the [ESP RGBWW Firmware](https://github.com/pljakobs/esp_rgbww_firmware) RGBWW LED controller.

## Features

- **Local push** via WebSocket — instant state updates without polling
- **Automatic discovery** via mDNS (Zeroconf) — no IP configuration needed
- **Full light entity**: HS colour + colour-temperature (2700 K – 6500 K), brightness, transitions
- **Per-channel lights** for the dedicated warm-white and cool-white channels
- **Preset select** — apply saved colour presets from the device
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

Devices are discovered automatically via mDNS (`_esprgbwwAPI._http._tcp.local.`).  
You can also add a device manually via **Settings → Devices & Services → Add Integration → Lightinator**.

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `light.<name>` | Light | Main RGBWW light (HS + colour-temp) |
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

## Colour Temperature

The firmware maps colour temperature to a 0–100 scale:

- **0** = pure RGB (no white channels)
- **100** = maximum warm-white / cool-white bias

HA mireds are converted as:

```
firmware_ct = round((mireds - 153) * 100 / 217)
```

## License

MIT — see [LICENSE](LICENSE).

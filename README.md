# Lightinator — Home Assistant Custom Integration

<img align="right" width="96" height="96" src="custom_components/esp_rgbww/brand/icon-dark.svg" alt="Lightinator Home Assistant icon">

A native Home Assistant integration for the [ESP RGBWW Firmware](https://github.com/pljakobs/esp_rgbww_firmware) RGBWW LED controller.

## Features

- **JSON-RPC WebSocket** for light state, commands, and runtime updates; HTTP is retained for presets/groups and cluster-host discovery, which the firmware does not expose over RPC
- **Automatic discovery** via mDNS (Zeroconf) — no IP configuration needed
- **5.0 compatibility**: the existing main light, raw channels, entity identities and untargeted API remain supported
- **5.1 virtual lights**: one primary light entity per configured firmware light, sharing one controller device and WebSocket
- **5.1 light management**: create and delete firmware lights from Configure or HA actions, with pin and default-light validation
- **5.1 hardware settings**: board/revision selection and controller-wide PWM timer, spread spectrum and phase shift in Configure
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
- **INA219 and AS7341 sensor extensions** — native current/voltage/power and spectral reading entities, persistent controls, and an on-demand spectral measurement button

Uptime is a duration measurement, not an accumulated total; controller restarts
can reset it. Runtime notifications received during an info refresh take
precedence over that older poll. Communication errors identify the controller,
RPC timeout stage, and WebSocket close code/error where available. The socket
reconnects with backoff; failed commands are not replayed automatically.

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
Configure requires a successfully loaded controller entry. If setup is still
retrying or the entry has been unloaded, it reports this explicitly instead of
opening an unusable settings form. Check the entry's setup error and reload it
after correcting connectivity or credentials.
Use **Controller icon** in Configure to choose a locally available controller
icon; the selected sprite ID is saved to the controller's app-data.
On 5.1, **Create light** in Configure lets you choose a name, output type and
unused PWM GPIOs. **Delete light** requires confirmation; when removing the
default with other lights remaining, choose its replacement. The final light
can be deleted. **Board and PWM settings** selects a SoC-compatible board and
revision from the controller's local catalog. PWM lights require a selected
board profile; custom GPIO assignment without a profile is not supported. It
also configures PWM frequency, resolution (1–14 bits), timer number,
speed mode (high speed only on ESP32), spread spectrum width/subsampling and
phase shift. Existing lights, calibration, buttons and I2C settings are preserved;
selecting a board saves the complete catalog profile in
`hardware.configuration.board_profile`, but does not install a template or remap
GPIOs. Light creation preserves and validates against that saved snapshot,
without requiring a catalog lookup. Older configurations acquire the snapshot
when the board is applied or a light is created. Firmware with controller-owned
PWM requires a selected board profile before deploying lights; its runtime setup
reads ConfigDB, not the catalog. Incompatible pin
assignments are rejected. Changes require confirmation, stale-edit checks and
persistent readback verification. PWM frequency/resolution combinations remain
subject to hardware clock limits; firmware may reject output initialization even
when configuration readback succeeds. Older 5.1 builds may require a restart.
Editing existing lights/calibration and input/bus assignments remains in the
controller's webapp. The legacy controller-wide **Color** form is only offered
on 5.0.
When the matching firmware extensions are present, Home Assistant also exposes
native configuration entities for the INA219 current sensor and AS7341 spectral
sensor. The AS7341 interval setting is stored as configuration only; it does not
start periodic measurements. Use **Start Spectral Measurement** for an explicit
sample. Sensor history and graphs are provided by Home Assistant's built-in
history/statistics features; this integration does not require a custom card.
The AS7341 channels, clear brightness, and near-infrared values are raw ADC
counts, not lux. Unsupported extensions are omitted without affecting existing
controllers.
Configuration controls send only changed writable fields and verify persistent
readback; response metadata such as `available` is never written. Invalid or
non-finite readings are not graphed as zero. Extension error diagnostics remain
available while disabled or when configuration polling fails, and polling errors
are also logged in Home Assistant.
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
| `sensor.<name>_current`, `_voltage`, `_power` | Sensor | Valid INA219 current (A), voltage (V), and power (W) readings |
| `sensor.<name>_spectral_channel_f1` … `_f8` | Sensor | AS7341 spectral channels F1–F8 (raw ADC counts) |
| `sensor.<name>_clear_brightness_low/high` | Sensor | AS7341 clear-channel brightness (raw ADC counts, not lux) |
| `sensor.<name>_near_infrared_low/high` | Sensor | AS7341 near-infrared channels (raw ADC counts) |
| `switch.<name>_current_sensor_enabled` / `_spectral_sensor_enabled` | Switch | Enable or disable the INA219 and AS7341 extensions |
| `number.<name>_current_sensor_*` / `_spectral_sensor_*` | Number | Persistent address, shunt, sampling, and AS7341 integration/gain settings |
| `button.<name>_start_spectral_measurement` | Button | Start one AS7341 measurement; unavailable while disabled or disconnected |
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
The `esp_rgbww.create_light` action takes a controller device target, `name`,
`light_type`, a `pins` integer list in channel order, and optional `is_default`.
For example, WWCW uses `[warm_white_gpio, cool_white_gpio]`; RGBWWCW uses
`[red_gpio, green_gpio, blue_gpio, warm_white_gpio, cool_white_gpio]`.
`esp_rgbww.delete_light` takes `light_id` and, only when deleting the default
with other lights remaining, `replacement_default` (both firmware UUIDs).
The UUID is exposed as the primary light entity's `light_id` state attribute.
These actions reject 5.0 controllers and invalid or stale configurations.
Writes preserve other hardware settings, use a new UUID for each new light,
and verify persistent configuration readback. The integration refreshes discovery
after a successful write; new entities appear and deleted entities become
unavailable without rebinding existing identities. Current 5.1 firmware reloads
outputs live; older builds may need a controller restart to activate changes.
Saving single-channel/WWCW RAW presets requires firmware advertising the preset
extension described below; older builds report an upgrade error. HSV and named
RGB RAW presets retain their existing formats. The legacy controller-level
preset selector excludes indexed RAW presets; use the per-light swatch card to
apply those with an exact profile match.

## Controller configuration page

Administrators can open **Lightinator** in the Home Assistant sidebar for a
central overview of all configured controllers. Select a controller to see its
connection status, firmware, configured lights, GPIO assignments and default
light. Add or delete 5.1 lights directly from that inventory. Controller-wide
board/PWM, network, firmware and icon settings are grouped below the lights.
Firmware 5.0 retains its single-light color settings without virtual-light actions.

The page uses the same validated options flows as **Configure** in Devices &
services, including confirmation and concurrent-edit checks. Existing light
entities and automations are unchanged. The panel and its controller-data API
are admin-only; the overview does not export passwords. Refresh updates the
inventory, and it refreshes automatically every 30 seconds outside an open form.
Restart Home Assistant after installing the integration to register the sidebar.

## Per-light preset swatch card

Presets stay controller-local; each 5.1 light has its own favourite selections,
stored against its persistent UUID. Select a light on the web UI colour card,
open **Presets**, and star the colours to show in Home Assistant. Legacy
controller favourites appear on the default light until explicitly overridden.
On 5.0, the existing controller favourites and untargeted commands are unchanged.
Saving membership or indexed RAW colours requires 5.1 firmware advertising
`/info.rgbww.preset_light_favorites: true`; older builds report an upgrade error.

After updating the integration and restarting Home Assistant, add this dashboard
resource under **Settings -> Dashboards -> Resources** (advanced mode), with type
**JavaScript Module**:

```text
/esp_rgbww_panel/preset-card.js
```

Then add a manual dashboard card:

```yaml
type: custom:lightinator-preset-card
entity: light.your_lightinator_light
title: Favourite colours
```

Use a primary Lightinator light entity, not an optional RAW channel entity.
The card is available to users with permission to control that entity, not only
administrators. It uses authenticated HA WebSocket calls; controller credentials
are never sent to the browser. Errors remain visible in the card.

Each swatch applies the saved preset by ID, preserving brightness, temperature
and white channels instead of translating the preview colour back into a new HA
colour command. Indexed RAW presets apply only to matching output profiles.
Preview colours are approximate, especially for white mixtures. The native HA
light dialog still receives representable favourite colours without replacing
user-customized favourites; use the card for exact preset application.

`esp_rgbww.save_preset` accepts `light_id` to save that light's current HSV or RAW
state. Its `favorite` option changes membership only for that UUID on 5.1.
The default light is used if omitted. Removing membership leaves the shared
controller preset intact. Writes read the latest preset and verify membership,
but do not provide cross-client compare-and-swap or scene deletion tombstones.

## Colour Temperature

The firmware's `hsv.ct` value is a Kelvin temperature, not a 0–100 percentage.
The integration sends and reads it directly. Home Assistant displays the light
over its supported range of 2703–6536 K and handles Kelvin/mired conversion.
On 5.1 the range comes from each light's configured warm/cool temperatures.
Both 5.0 and 5.1 HSV readback use hue in degrees and saturation/brightness in
percent, matching command units. No additional HSV scaling is applied.

## Development tests

The GitHub **Release** workflow runs on pushes to `master` and version tags.
It can also be started manually using **Actions -> Release -> Run workflow**.
It publishes the version in the integration manifest; existing version tags
must point to the selected commit.

With Home Assistant installed in a Python environment, run:

```sh
python -m unittest -v test_firmware_api test_virtual_lights test_panel test_presets
```

The virtual-light tests exercise discovery, profile controls, UUID migration,
per-light command/state isolation and unchanged 5.0 setup/commands.
Panel tests cover controller inventories, unloaded entries, admin access,
error reporting and exclusion of controller credentials.

## License

MIT — see [LICENSE](LICENSE).

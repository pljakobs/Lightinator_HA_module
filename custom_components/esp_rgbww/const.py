"""Constants for the Lightinator (ESP RGBWW) integration."""

DOMAIN = "esp_rgbww"

DEFAULT_PORT = 80
UPDATE_INTERVAL = 30  # seconds between fallback polls

MDNS_TYPE = "_http._tcp.local."

# Firmware colour-temperature range (mireds — used for conversion only)
CT_MIN_MIREDS = 153   # ~6536 K — cool white
CT_MAX_MIREDS = 370   # ~2703 K — warm white
CT_MIREDS_RANGE = CT_MAX_MIREDS - CT_MIN_MIREDS  # 217

# HA 2024.2+ uses Kelvin natively
CT_MIN_KELVIN = 2703   # warmest (370 mireds)
CT_MAX_KELVIN = 6536   # coolest (153 mireds)

# Raw per-channel duty scale (0–1023)
RAW_MAX = 1023

# Platforms provided by this integration
PLATFORMS = ["light", "button", "sensor", "select"]

# Config entry keys (beyond the HA standard CONF_HOST / CONF_PORT / CONF_PASSWORD)
CONF_CHIP_ID = "chip_id"
CONF_DEVICE_NAME = "device_name"

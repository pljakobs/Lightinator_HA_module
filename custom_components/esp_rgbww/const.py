"""Constants for the Lightinator (ESP RGBWW) integration."""

DOMAIN = "esp_rgbww"

DEFAULT_PORT = 80
UPDATE_INTERVAL = 30  # seconds between fallback polls

MDNS_TYPE = "_lightinator-api._tcp.local."

# HA 2024.2+ uses Kelvin natively
CT_MIN_KELVIN = 2703   # warmest (370 mireds)
CT_MAX_KELVIN = 6536   # coolest (153 mireds)

# Raw per-channel duty scale (0–1023)
RAW_MAX = 1023

RAW_CHANNELS_BY_MODEL = {
	0: (("r", "Red"), ("g", "Green"), ("b", "Blue")),
	1: (("r", "Red"), ("g", "Green"), ("b", "Blue"), ("ww", "Warm White")),
	2: (("r", "Red"), ("g", "Green"), ("b", "Blue"), ("cw", "Cool White")),
	3: (
		("r", "Red"),
		("g", "Green"),
		("b", "Blue"),
		("ww", "Warm White"),
		("cw", "Cool White"),
	),
}

# Platforms provided by this integration
PLATFORMS = ["light", "button", "sensor", "select", "update"]

# Config entry keys (beyond the HA standard CONF_HOST / CONF_PORT / CONF_PASSWORD)
CONF_CHIP_ID = "chip_id"
CONF_DEVICE_NAME = "device_name"
CONF_GROUP_AREA_MAP = "group_area_map"
CONF_FIRMWARE_BRANCH = "firmware_branch"
CONF_FIRMWARE_TYPE = "firmware_type"


def configured_color_temperature_range(config: dict) -> tuple[int, int]:
	"""Return sorted configured warm/cool white temperatures in Kelvin."""
	color_config = config.get("color", {})
	colortemp = color_config.get("colortemp", {}) if isinstance(color_config, dict) else {}
	if not isinstance(colortemp, dict):
		return CT_MIN_KELVIN, CT_MAX_KELVIN

	warm = colortemp.get("ww")
	cold = colortemp.get("cw")
	if (
		isinstance(warm, int)
		and not isinstance(warm, bool)
		and isinstance(cold, int)
		and not isinstance(cold, bool)
		and warm > 0
		and cold > 0
	):
		minimum, maximum = sorted((warm, cold))
		if minimum < maximum:
			return minimum, maximum

	return CT_MIN_KELVIN, CT_MAX_KELVIN

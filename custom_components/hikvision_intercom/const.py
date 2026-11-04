"""Constants for the Hikvision Intercom integration."""

DOMAIN = "hikvision_intercom"

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_BASE_URL = "base_url"
CONF_SERVER = "server"

DEFAULT_BASE_URL = "https://api.hik-connect.com"

# Predefined Hik-Connect regional API servers (value -> label). The global
# entry auto-routes via the login region redirect; picking a region is just
# faster/deterministic. "custom" reveals the free-text override below.
SERVER_CUSTOM = "custom"
SERVERS = {
    DEFAULT_BASE_URL: "Global (auto-route)",
    "https://apiieu.hik-connect.com": "Europe (apiieu)",
    "https://apiius.hik-connect.com": "Americas (apiius)",
}

# Live MJPEG transcode defaults
MJPEG_FPS = 8
MJPEG_QUALITY = 5
MJPEG_WIDTH = 1280
MJPEG_HEIGHT = 720

# Door latch re-locks itself a few seconds after unlock; mirror that in HA.
DOOR_LATCH_UNLOCKED_FOR = 5

# Baseline poll interval (s) for the cloud call-status endpoint. MQTT push
# accelerates this to near real time, so the baseline can stay gentle.
CALL_POLL_INTERVAL = 10
CONF_CALL_POLL_INTERVAL = "call_poll_interval"
MIN_CALL_POLL_INTERVAL = 3
MAX_CALL_POLL_INTERVAL = 60

CONF_NATIVE_STREAM = "native_stream"
NATIVE_STREAM_OFF = "off"
NATIVE_STREAM_H264 = "h264"
NATIVE_STREAM_ALL = "all"
NATIVE_STREAM_CODECS = {
    NATIVE_STREAM_OFF: frozenset(),
    NATIVE_STREAM_H264: frozenset({"h264"}),
    NATIVE_STREAM_ALL: frozenset({"h264", "hevc"}),
}


def native_stream_mode(options) -> str:
    mode = options.get(CONF_NATIVE_STREAM, NATIVE_STREAM_H264)
    if isinstance(mode, bool):
        return NATIVE_STREAM_H264 if mode else NATIVE_STREAM_OFF
    return mode if mode in NATIVE_STREAM_CODECS else NATIVE_STREAM_H264

# Device telemetry (online, wifi, firmware, disk) changes slowly — poll gently.
STATUS_POLL_INTERVAL = 60

CALL_STATES = ["idle", "ringing", "call in progress"]  # "unknown" -> None (reserved state)

# Realtime push (EZVIZ MQTT backend, shared by Hik-Connect). A push event is
# only used as a trigger to immediately re-poll the authoritative call status,
# so we never have to guess which opaque alert code means "incoming call".
MQTT_APP_KEY = "4c6b3cc2-b5eb-4813-a592-612c1374c1fe"
MQTT_APP_SECRET = "17454517-cc1c-42b3-a845-99b4a15dd3e6"


def call_signal(serial: str) -> str:
    return f"{DOMAIN}_call_push_{serial}"


# --- Local stations (config subentries) and the SDK bridge app ------------------
SUBENTRY_STATION = "station"
DEFAULT_STATION_TITLE = "Doorbell chime"
APP_REPOSITORY_URL = "https://github.com/berkkayhan34-sys/hikvision-intercom-chime"
APP_SLUG_SUFFIX = "hikvision_sdk"  # repository slug prefix varies; local installs use "local"
APP_PORT = 8099
DEFAULT_SDK_PORT = 8000
CHIME_POLL_INTERVAL = 10

# --- Chime audio ------------------------------------------------------------------
DEFAULT_REPEATS = 4
DEFAULT_GAIN = 0
MAX_FILE_BYTES = 1_048_576
MAX_SOURCE_SECONDS = 30
MAX_TOTAL_SECONDS = 60
MAX_SOURCES = 16
SAMPLE_RATE = 8000
PEAK = 32760

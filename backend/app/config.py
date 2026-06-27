"""Runtime configuration, overridable via environment variables (TMV71_*)."""
from __future__ import annotations

import json
import os

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_VERSION = "3.2"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TMV71_", env_file=".env")

    # Serial link to the radio
    serial_port: str = "/dev/ttyUSB0"
    serial_baud: int = 57600
    serial_timeout: float = 1.0

    # HTTP server (LAN only for now)
    host: str = "0.0.0.0"
    port: int = 8000

    # How often the background poller refreshes radio status (seconds)
    poll_interval: float = 0.8

    # Audio: direct WebRTC (Opus) between browser and backend via aiortc,
    # bridged to the radio's USB sound interface.
    audio_enabled: bool = True
    audio_device: str = "NAD"        # substring matched against sound devices
    rx_gain: float = 1.0             # digital gain radio -> browser
    tx_gain: float = 1.0             # digital gain browser mic -> radio
    tx_auto_gain: bool = False       # AGC on the TX mic path (overrides tx_gain)

    # Squelch threshold (0..31) per band, persisted so it is restored on the
    # radio after a power cycle. None = leave the radio's own setting alone.
    squelch_a: int | None = None
    squelch_b: int | None = None
    # TX path timing (ms). tx_buffer = mic backlog cap (latency vs jitter
    # tolerance); ptt_tail = how long TX stays keyed after release so the
    # buffered/in-flight tail plays out instead of being chopped.
    tx_buffer_ms: int = 250
    ptt_tail_ms: int = 250

    # TLS (required for browser microphone access / getUserMedia). When both are
    # set the run command / systemd unit should pass them to uvicorn.
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    # GPIO power switch: BCM pin driving a relay/MOSFET on the radio's DC line.
    # None = feature disabled until a pin is set (changeable in the web settings).
    gpio_power_pin: int | None = None
    gpio_active_high: bool = True    # False for active-low relay boards

    # Auto power off: the backend cuts power via GPIO after this many seconds of
    # inactivity (no control commands AND no connected client), without any API
    # call from the browser. Changeable in the web settings.
    auto_power_off_enabled: bool = False
    auto_power_off_seconds: int = 60

    # Operator callsign shown in the title bar. Persisted server-side so it
    # survives across browsers/devices (not just in one browser's localStorage).
    callsign: str = ""

    # Play a short beep on the radio mic at the end of each transmission.
    roger_beep_enabled: bool = False

    # Band-limit the transmitted (mic) audio with a low-pass filter so only the
    # voice range goes out — tames hiss/high-frequency content on TX.
    tx_lowpass_enabled: bool = False

    # Same voice low-pass on the received audio — cuts high-frequency hiss/noise
    # from the radio for more comfortable listening.
    rx_lowpass_enabled: bool = False

    # UI colour theme ("light" | "dark"). Persisted server-side so the choice
    # survives across browsers/devices and storage clears.
    theme: str = "light"

    # Logbook integration: Wavelog (locally installed) for QSO logging, QRZ.com
    # for callsign lookup. Secrets live only in runtime.json (gitignored).
    wavelog_url: str = ""            # e.g. https://wavelog.local
    wavelog_key: str = ""            # read/write API key
    wavelog_station_id: str = ""     # station profile id to log under
    qrz_api_key: str = ""
    qrz_username: str = ""
    qrz_password: str = ""

    # Own 5-tone selcall ID and last-used call (destination) code. Persisted
    # server-side so they survive restarts and are shared across browsers/devices.
    selcall_own: str = ""
    selcall_code: str = ""

    # Memory channel to restore after the radio is powered back on. Captured at
    # power-off (manual or auto) only if the control band was in memory mode, so
    # the radio doesn't come up on M001. -1 = nothing to restore.
    boot_mem_band: int = 0
    boot_mem_channel: int = -1

    # Highest memory channel number to scan when listing (TM-V71 has 0..999)
    max_memory_channels: int = 1000

    # Directory holding the (build-free) frontend, served at "/".
    # Relative to the app package dir (backend/app).
    frontend_dir: str = "../../frontend"


# Runtime overrides (e.g. serial port/baud changed from the web UI) are
# persisted next to the package so they survive restarts but stay out of git.
_RUNTIME_FILE = os.path.join(os.path.dirname(__file__), "runtime.json")
_RUNTIME_KEYS = ("serial_port", "serial_baud", "gpio_power_pin",
                 "rx_gain", "tx_gain", "tx_auto_gain", "audio_device",
                 "squelch_a", "squelch_b",
                 "tx_buffer_ms", "ptt_tail_ms",
                 "auto_power_off_enabled", "auto_power_off_seconds",
                 "callsign", "roger_beep_enabled", "theme",
                 "tx_lowpass_enabled", "rx_lowpass_enabled",
                 "wavelog_url", "wavelog_key", "wavelog_station_id",
                 "qrz_api_key", "qrz_username", "qrz_password",
                 "selcall_own", "selcall_code",
                 "boot_mem_band", "boot_mem_channel")


def _load_runtime() -> dict:
    try:
        with open(_RUNTIME_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_runtime(**values) -> None:
    """Persist a subset of settings (serial_port/serial_baud) to disk."""
    data = _load_runtime()
    data.update({k: v for k, v in values.items() if k in _RUNTIME_KEYS})
    with open(_RUNTIME_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


settings = Settings()

# Apply persisted runtime overrides on top of env/defaults.
for _k, _v in _load_runtime().items():
    if _k in _RUNTIME_KEYS:
        setattr(settings, _k, _v)

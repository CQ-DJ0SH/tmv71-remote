"""Pydantic models for the REST/WebSocket API.

These are the contract shared with the web frontend and (later) the Flutter
app, so keep field names stable and JSON-friendly.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class BandState(BaseModel):
    """Live state of one band (A=0 / B=1)."""
    band: int
    mode: int = Field(description="0=VFO, 1=memory, 2=call")
    rx_freq: int = Field(description="Receive frequency in Hz")
    tx_freq: Optional[int] = Field(default=None, description="TX freq for split, Hz")
    shift: int = Field(description="0=none, 1=+, 2=-")
    offset: int = Field(description="Repeater offset in Hz")
    fm_mode: int = Field(description="0=FM, 1=NFM, 2=AM")
    tone_on: bool = False
    ctcss_on: bool = False
    dcs_on: bool = False
    tone_hz: Optional[float] = None
    ctcss_hz: Optional[float] = None
    dcs_code: Optional[int] = None
    step_hz: int = 0
    power: Optional[int] = Field(default=None, description="0=High 50W, 1=Mid 10W, 2=Low 5W")
    squelch_level: Optional[int] = Field(default=None, description="Squelch threshold 0..31")
    squelch_open: bool = False
    memory_channel: Optional[int] = None
    memory_name: Optional[str] = None


class RadioStatus(BaseModel):
    """Full snapshot broadcast over the WebSocket and returned by GET /status."""
    connected: bool
    model: Optional[str] = None
    control_band: int = 0
    ptt_band: int = 0
    transmitting: bool = False
    single_band: bool = False
    # external data band (MU 38): 0=A, 1=B, 2=TX A/RX B, 3=TX B/RX A.
    # audio_band = which band's RX audio reaches the data connector / Pi.
    data_band: int = 0
    audio_band: int = 0
    # 1750 Hz tone hold (menu 402): off/on.
    tone_1750: bool = False
    bands: list[BandState] = []
    error: Optional[str] = None


class DataBandRequest(BaseModel):
    """External data band: 0=A, 1=B, 2=TX A/RX B, 3=TX B/RX A."""
    band: int = Field(ge=0, le=3)


class Tone1750Request(BaseModel):
    """1750 Hz tone hold (menu 402)."""
    on: bool


class FrequencyRequest(BaseModel):
    band: int = Field(ge=0, le=1)
    freq_hz: int = Field(gt=0)


class BandModeRequest(BaseModel):
    band: int = Field(ge=0, le=1)
    mode: int = Field(ge=0, le=2)


class VfoUpdate(BaseModel):
    """Change live VFO parameters (only the provided fields are applied)."""
    band: int = Field(ge=0, le=1)
    shift: Optional[int] = None        # 0 simplex / 1 + / 2 -
    offset: Optional[int] = None       # Hz
    fm_mode: Optional[int] = None      # 0 FM / 1 NFM
    tone_on: Optional[bool] = None
    ctcss_on: Optional[bool] = None
    dcs_on: Optional[bool] = None
    tone_idx: Optional[int] = None
    ctcss_idx: Optional[int] = None
    dcs_idx: Optional[int] = None


class PttRequest(BaseModel):
    transmit: bool


class ControlBandRequest(BaseModel):
    control_band: int = Field(ge=0, le=1)


class PttBandRequest(BaseModel):
    ptt_band: int = Field(ge=0, le=1)


class PowerRequest(BaseModel):
    band: int = Field(ge=0, le=1)
    level: int = Field(ge=0, le=2, description="0=High 50W, 1=Mid 10W, 2=Low 5W")


class SquelchRequest(BaseModel):
    band: int = Field(ge=0, le=1)
    level: int = Field(ge=0, le=31, description="Squelch threshold 0..31")


class StepRequest(BaseModel):
    band: int = Field(ge=0, le=1)
    direction: str = Field(pattern="^(up|down)$", description="Mic UP / DW key")


class BandDisplayRequest(BaseModel):
    single: bool = Field(description="True = single band (DL 1), False = dual (DL 0)")
    band: Optional[int] = Field(default=None, ge=0, le=1,
                                description="Active/control band when single")


class PowerSwitchRequest(BaseModel):
    on: bool


class GpioConfigRequest(BaseModel):
    pin: Optional[int] = Field(default=None, ge=0, le=27,
                               description="BCM pin for the power relay; null disables")


class AutoPowerOffRequest(BaseModel):
    """Server-side auto power off: cut GPIO power after N seconds of inactivity."""
    enabled: bool
    seconds: int = Field(default=60, ge=10, le=86400)


class CallsignRequest(BaseModel):
    """Operator callsign, persisted server-side."""
    callsign: str = Field(default="", max_length=12)


class ThemeRequest(BaseModel):
    """UI colour theme, persisted server-side."""
    theme: Literal["light", "dark"] = "light"


class ScanStartRequest(BaseModel):
    """Start a graphical sweep: a band (2m/70cm) or the memory bank (mem)."""
    band: str = Field(pattern="^(2m|70cm|mem)$")


class WebRTCOffer(BaseModel):
    sdp: str
    type: str


class AudioGainRequest(BaseModel):
    rx_gain: Optional[float] = Field(default=None, ge=0, le=12)
    tx_gain: Optional[float] = Field(default=None, ge=0, le=12)


class AudioDeviceRequest(BaseModel):
    device: str


class AudioBufferRequest(BaseModel):
    """TX path timing: mic backlog cap and the post-release transmit tail."""
    tx_buffer_ms: Optional[int] = Field(default=None, ge=20, le=1000)
    ptt_tail_ms: Optional[int] = Field(default=None, ge=0, le=1000)


class TonesRequest(BaseModel):
    """Toggle the roger beep, the two-tone (700/1900 Hz) mic test, and/or the
    voice low-pass on the TX path."""
    roger_beep: Optional[bool] = None
    test_tone: Optional[bool] = None
    tx_lowpass: Optional[bool] = None
    rx_lowpass: Optional[bool] = None
    mic_test: Optional[bool] = None


class DigiConfig(BaseModel):
    """CW/RTTY digimode parameters (all optional — only sent fields change)."""
    mode: Optional[str] = None          # "cw" | "rtty"
    rx: Optional[bool] = None           # start/stop the decoder
    cw_wpm: Optional[float] = Field(default=None, ge=5, le=60)
    cw_pitch: Optional[float] = Field(default=None, ge=300, le=1200)
    cw_auto: Optional[bool] = None      # auto-detect RX speed (WPM)
    rtty_baud: Optional[float] = Field(default=None, ge=10, le=100)
    rtty_shift: Optional[float] = Field(default=None, ge=50, le=1000)
    rtty_mark: Optional[float] = Field(default=None, ge=500, le=3000)


class DigiTxRequest(BaseModel):
    """Encode + transmit text in the current digimode (keys PTT)."""
    text: str


class SelcallConfig(BaseModel):
    """Classic 5-tone selective-call settings (all optional)."""
    standard: Optional[str] = None      # "zvei1" | "zvei2" | "ccir" | "eea"
    tone_ms: Optional[float] = Field(default=None, ge=20, le=200)
    rx: Optional[bool] = None           # run the decoder
    own: Optional[str] = None           # own 5-tone code (mute releases on match)


class SelcallTxRequest(BaseModel):
    """Encode + transmit a 5-tone selective call (keys PTT)."""
    code: str


class MixerSetRequest(BaseModel):
    """Set an ALSA simple-mixer control on the USB radio audio card."""
    name: str
    percent: Optional[int] = Field(default=None, ge=0, le=100)
    switch_on: Optional[bool] = None


class MemoryChannel(BaseModel):
    """A memory channel for read/write (CRUD) and CSV import/export."""
    channel: int = Field(ge=0, le=999)
    name: str = ""
    rx_freq: int = Field(gt=0)
    tx_freq: int = 0
    step: int = 0
    shift: int = 0
    reverse: int = 0
    tone_on: bool = False
    ctcss_on: bool = False
    dcs_on: bool = False
    tone_idx: int = 1
    ctcss_idx: int = 1
    dcs_idx: int = 0
    offset: int = 0
    fm_mode: int = 0
    lockout: int = 0


class RecallRequest(BaseModel):
    band: int = Field(ge=0, le=1)
    channel: int = Field(ge=0, le=999)


class RadioInfo(BaseModel):
    """Static device identification (ID / AE / FV / TY commands)."""
    app_version: str = "1.3"
    model: Optional[str] = None              # ID
    serial_number: Optional[str] = None      # AE
    firmware: Optional[str] = None           # FV 0 (joined version fields)
    market: Optional[str] = None             # TY p1: M=EU, K=US
    crossband: Optional[bool] = None         # TY p4 (== "1")
    radio_type: Optional[str] = None         # TY raw field string


class DtmfMemory(BaseModel):
    """One of the 10 DTMF autodial memories (DM command)."""
    channel: int = Field(ge=0, le=9)
    code: str = Field(default="", max_length=16)


class SerialConfig(BaseModel):
    """Serial link settings, changeable at runtime."""
    port: str
    baud: int = Field(gt=0)


class HackRFConfig(BaseModel):
    """HackRF waterfall settings (all optional; only sent fields are applied)."""
    mode: Optional[str] = Field(default=None, pattern="^(pan|sweep)$")
    follow: Optional[bool] = None
    center: Optional[int] = Field(default=None, gt=0, description="Panadapter centre, Hz")
    sweep_start: Optional[int] = Field(default=None, gt=0)
    sweep_stop: Optional[int] = Field(default=None, gt=0)
    lna: Optional[int] = Field(default=None, ge=0, le=40)
    vga: Optional[int] = Field(default=None, ge=0, le=62)
    amp: Optional[bool] = None

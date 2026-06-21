"""FastAPI application: REST control endpoints + live-status WebSocket.

Serves the built SPA frontend from ``settings.frontend_dir`` at "/" when present.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import mimetypes
import os
import re
import time
import urllib.request
from contextlib import asynccontextmanager

from fastapi import (FastAPI, HTTPException, Response, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import memory as memory_io
from .config import APP_VERSION, save_runtime, settings
from .models import (AudioDeviceRequest, AudioGainRequest, AutoPowerOffRequest,
                     BandDisplayRequest, BandModeRequest, CallsignRequest,
                     ThemeRequest,
                     ControlBandRequest, DataBandRequest,
                     DtmfMemory, FrequencyRequest, GpioConfigRequest,
                     AudioBufferRequest,
                     MemoryChannel, MixerSetRequest, PowerRequest,
                     PowerSwitchRequest, TonesRequest, DigiConfig, DigiTxRequest,
                     SelcallConfig, SelcallTxRequest,
                     PttBandRequest, PttRequest, RadioInfo, RadioStatus,
                     Tone1750Request,
                     RecallRequest, ScanStartRequest, SerialConfig,
                     SquelchRequest, StepRequest, VfoUpdate, WebRTCOffer,
                     HackRFConfig)
from . import mixer
from . import system_info
from . import updater
from .power_switch import PowerSwitch
from .webrtc_audio import RadioAudio, RadioRxTrack, consume_mic, SAMPLE_RATE
from . import digimodes
from . import selcall
from aiortc import RTCPeerConnection, RTCSessionDescription
from .radio_service import RadioService
from .tmv71 import TMV71Error
from .hackrf_sdr import HackRFSpectrum
from .state import ConnectionManager

logging.basicConfig(level=logging.INFO)

manager = ConnectionManager()
service = RadioService(manager)


def _control_band_freq():
    """Current control-band RX frequency (Hz) for the HackRF follow mode."""
    try:
        st = service.status
        b = st.control_band or 0
        return st.bands[b].rx_freq if st.bands and b < len(st.bands) else None
    except Exception:  # noqa: BLE001
        return None


sdr = HackRFSpectrum(freq_getter=_control_band_freq)


power_switch = PowerSwitch(settings.gpio_power_pin, settings.gpio_active_high)
radio_audio = RadioAudio(device=settings.audio_device,
                         rx_gain=settings.rx_gain, tx_gain=settings.tx_gain,
                         tx_buffer_ms=settings.tx_buffer_ms,
                         ptt_tail_ms=settings.ptt_tail_ms,
                         tx_lowpass=settings.tx_lowpass_enabled,
                         rx_lowpass=settings.rx_lowpass_enabled)
pcs: set = set()      # active WebRTC peer connections


class DigiService:
    """CW/RTTY decode (off the radio RX audio) + encode/transmit (keys PTT).

    The decoder runs as a background loop pulling RX blocks from ``audio``;
    decoded text is pushed to WebSocket subscribers. Transmit encodes the text,
    keys PTT, plays the audio into the mic path, then un-keys."""

    def __init__(self, audio: RadioAudio):
        self.audio = audio
        self.mode = "cw"
        self.cw_wpm = 18.0
        self.cw_pitch = 700.0
        self.cw_auto = True
        self.rtty_baud = 45.45
        self.rtty_shift = 170.0
        self.rtty_mark = 2125.0
        self.rx = False
        self._dec = None
        self._subs: set = set()
        self._task = None
        self._tx_lock = asyncio.Lock()

    def status(self) -> dict:
        return {"mode": self.mode, "rx": self.rx, "tx": self.audio.digi_tx_busy(),
                "cw_wpm": self.cw_wpm, "cw_pitch": self.cw_pitch, "cw_auto": self.cw_auto,
                "rtty_baud": self.rtty_baud, "rtty_shift": self.rtty_shift,
                "rtty_mark": self.rtty_mark}

    def _new_decoder(self):
        if self.mode == "rtty":
            return digimodes.RTTYDecoder(SAMPLE_RATE, self.rtty_baud,
                                         self.rtty_shift, self.rtty_mark)
        return digimodes.CWDecoder(SAMPLE_RATE, self.cw_pitch, self.cw_wpm, self.cw_auto)

    def configure(self, cfg: DigiConfig) -> dict:
        changed = False
        if cfg.cw_auto is not None and cfg.cw_auto != self.cw_auto:
            self.cw_auto = cfg.cw_auto
            changed = True
        for f in ("mode", "cw_wpm", "cw_pitch", "rtty_baud", "rtty_shift", "rtty_mark"):
            v = getattr(cfg, f)
            if v is not None and getattr(self, f) != v:
                setattr(self, f, v)
                changed = True
        if cfg.rx is not None:
            self.set_rx(cfg.rx)
        elif changed and self.rx:
            self._dec = self._new_decoder()       # restart decoder with new params
        return self.status()

    def set_rx(self, on: bool) -> None:
        self.rx = on
        self.audio.set_digi_rx(on)
        self._dec = self._new_decoder() if on else None

    def subscribe(self) -> "asyncio.Queue":
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        self._subs.discard(q)

    def _broadcast(self, text: str) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(text)
            except Exception:  # noqa: BLE001
                pass

    async def start(self) -> None:
        self._task = asyncio.create_task(self._decode_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _decode_loop(self) -> None:
        while True:
            await asyncio.sleep(0.12)
            if not self.rx or self._dec is None:
                continue
            pcm = self.audio.pop_digi_rx()
            if pcm is None or len(pcm) == 0:
                continue
            try:
                text = await asyncio.to_thread(self._dec.feed, pcm)
            except Exception:  # noqa: BLE001
                continue
            if text:
                self._broadcast(text)

    async def transmit(self, text: str, set_ptt) -> dict:
        text = (text or "").strip()
        if not text:
            return {"sent": ""}
        if self.mode == "rtty":
            pcm = digimodes.rtty_encode(text, self.rtty_baud, self.rtty_shift,
                                        self.rtty_mark, SAMPLE_RATE)
        else:
            pcm = digimodes.cw_encode(text, self.cw_wpm, self.cw_pitch, SAMPLE_RATE)
        if len(pcm) == 0:
            return {"sent": ""}
        async with self._tx_lock:
            await set_ptt(True)
            self.audio.play_digi(pcm)
            deadline = time.monotonic() + len(pcm) / SAMPLE_RATE + 3.0
            while self.audio.digi_tx_busy() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            self.audio.stop_digi_tx()
            await set_ptt(False)
        return {"sent": text}


digi = DigiService(radio_audio)


class SelcallService:
    """Classic 5-tone selective call: decode off RX, encode/transmit (keys PTT).

    When ``own`` is set, a decoded code equal to it is flagged as "our call"
    (the UI uses that to release its mute). Decoded codes + call events are
    pushed to WebSocket subscribers."""

    def __init__(self, audio: RadioAudio):
        self.audio = audio
        self.standard = "zvei1"
        self.tone_ms = 70.0
        self.own = ""
        self.rx = False
        self._dec = None
        self._subs: set = set()
        self._task = None
        self._tx_lock = asyncio.Lock()

    def status(self) -> dict:
        return {"standard": self.standard, "tone_ms": self.tone_ms,
                "own": self.own, "rx": self.rx, "tx": self.audio.digi_tx_busy(),
                "standards": list(selcall.STANDARDS.keys())}

    def _new_decoder(self):
        return selcall.SelcallDecoder(self.standard, self.tone_ms, SAMPLE_RATE)

    def configure(self, cfg: SelcallConfig) -> dict:
        changed = False
        for f in ("standard", "tone_ms"):
            v = getattr(cfg, f)
            if v is not None and getattr(self, f) != v:
                setattr(self, f, v)
                changed = True
        if cfg.own is not None:
            self.own = "".join(c for c in cfg.own if c.isdigit())[:5]
        if cfg.rx is not None:
            self.set_rx(cfg.rx)
        elif changed and self.rx:
            self._dec = self._new_decoder()
        return self.status()

    def set_rx(self, on: bool) -> None:
        self.rx = on
        self.audio.set_sel_rx(on)
        self._dec = self._new_decoder() if on else None

    def subscribe(self):
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        self._subs.discard(q)

    def _broadcast(self, msg: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except Exception:  # noqa: BLE001
                pass

    async def start(self) -> None:
        self._task = asyncio.create_task(self._decode_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _decode_loop(self) -> None:
        while True:
            await asyncio.sleep(0.12)
            if not self.rx or self._dec is None:
                continue
            pcm = self.audio.pop_sel_rx()
            if pcm is None or len(pcm) == 0:
                continue
            try:
                codes = await asyncio.to_thread(self._dec.feed, pcm)
            except Exception:  # noqa: BLE001
                continue
            for code in codes:
                mine = bool(self.own) and code == self.own
                self._broadcast({"t": "call", "code": code, "mine": mine})

    async def transmit(self, code: str, set_ptt) -> dict:
        code = "".join(c for c in (code or "") if c.isdigit())[:5]
        if not code:
            return {"sent": ""}
        pcm = selcall.encode(code, self.standard, self.tone_ms, SAMPLE_RATE)
        if len(pcm) == 0:
            return {"sent": ""}
        async with self._tx_lock:
            await set_ptt(True)
            self.audio.play_digi(pcm)
            deadline = time.monotonic() + len(pcm) / SAMPLE_RATE + 3.0
            while self.audio.digi_tx_busy() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            self.audio.stop_digi_tx()
            await set_ptt(False)
        return {"sent": code}


sel = SelcallService(radio_audio)

# Auto power off: the backend itself cuts GPIO power after a period of
# inactivity, so the radio shuts down even when the browser is gone (no API
# call required). Activity = any control command OR a connected WebSocket.
_last_activity = time.monotonic()

# Remote PTT safety: track a PTT engaged via the /api/ptt endpoint (held or
# latched). If every client disappears while the radio is still keyed (e.g. a
# Wi-Fi drop while PTT-locked), a watchdog drops it so the transmitter can't
# stay latched on with no one in control.
_remote_ptt = False
_remote_ptt_clientless_since: float | None = None


def touch_activity() -> None:
    global _last_activity
    _last_activity = time.monotonic()


def _set_remote_ptt(on: bool) -> None:
    global _remote_ptt, _remote_ptt_clientless_since
    _remote_ptt = on
    _remote_ptt_clientless_since = None


async def _shut_down_peripherals() -> None:
    """When the radio is powered off, also stop the HackRF and drop any WebRTC
    audio peers: there's nothing to listen to or control with the radio dark,
    and the SDR shouldn't keep holding the USB device. Best-effort / idempotent."""
    try:
        await sdr.stop()
    except Exception:  # noqa: BLE001
        pass
    for pc in list(pcs):
        try:
            await pc.close()
        except Exception:  # noqa: BLE001
            pass
        pcs.discard(pc)
    radio_audio.peers = 0


async def _auto_power_off_loop() -> None:
    while True:
        await asyncio.sleep(2.0)
        if not settings.auto_power_off_enabled:
            continue
        if not power_switch.available or power_switch.state is not True:
            continue
        # A connected client counts as activity (don't power off while watching).
        if manager.count > 0:
            touch_activity()
            continue
        if time.monotonic() - _last_activity >= settings.auto_power_off_seconds:
            try:
                power_switch.set(False)
                await _shut_down_peripherals()
                logging.getLogger("tmv71").info(
                    "auto power off after %ds inactivity",
                    settings.auto_power_off_seconds)
            except Exception:  # noqa: BLE001
                pass
            touch_activity()   # avoid immediate re-trigger


async def _ptt_safety_loop() -> None:
    """Release a remotely engaged PTT when every client vanishes while keyed: a
    dropped link must never leave the transmitter latched on. Digi/selcall/DTMF
    transmits don't go through /api/ptt, so they're unaffected (they un-key
    themselves)."""
    global _remote_ptt_clientless_since
    grace = 4.0          # tolerate brief WebSocket reconnects
    while True:
        await asyncio.sleep(1.0)
        if not _remote_ptt or not service.transmitting or manager.count > 0:
            _remote_ptt_clientless_since = None
            continue
        now = time.monotonic()
        if _remote_ptt_clientless_since is None:
            _remote_ptt_clientless_since = now
        elif now - _remote_ptt_clientless_since >= grace:
            try:
                await service.set_ptt(False)
            except Exception:  # noqa: BLE001
                pass
            _set_remote_ptt(False)
            logging.getLogger("tmv71").warning(
                "remote PTT released: all clients gone while keyed")


async def _audio_watchdog() -> None:
    """Recover from a stalled PortAudio capture: if rx_frames stops advancing
    while the stream should be running, reopen it (USB sound cards occasionally
    drop their callback after an xrun, with no error)."""
    log = logging.getLogger("tmv71")
    last = -1
    stalls = 0
    while True:
        await asyncio.sleep(1.5)
        if not settings.audio_enabled:
            last = radio_audio.rx_frames
            continue
        # A failed reopen leaves connected=False; keep watching anyway so we
        # retry rather than giving up permanently when the USB device blips.
        if not radio_audio.connected:
            log.warning("audio stream down, attempting reopen")
            try:
                await asyncio.to_thread(radio_audio.reopen)
            except Exception:  # noqa: BLE001
                pass
            last = radio_audio.rx_frames
            stalls = 0
            continue
        cur = radio_audio.rx_frames
        if cur == last and last >= 0:
            stalls += 1
            if stalls >= 2:               # ~3 s without new frames -> stalled
                log.warning("audio capture stalled (frames=%d), reopening", cur)
                try:
                    await asyncio.to_thread(radio_audio.reopen)
                except Exception:  # noqa: BLE001
                    pass
                stalls = 0
        else:
            stalls = 0
        last = radio_audio.rx_frames


@asynccontextmanager
async def lifespan(app: FastAPI):
    await service.start()
    audio_wd = None
    if settings.audio_enabled:
        radio_audio.start(asyncio.get_running_loop())

        async def _couple_ptt(transmit: bool) -> None:
            radio_audio.set_ptt_open(transmit)

        async def _roger_beep() -> None:
            # radio is still keyed + audio gate open here: first let the buffered
            # mic tail play out (so PTT release doesn't chop the last words),
            # then sound the roger beep.
            await radio_audio.drain_tx()
            if radio_audio.roger_beep and not radio_audio.test_tone:
                radio_audio.trigger_roger_beep()
                await asyncio.sleep(0.27)        # let the 250 ms beep finish before un-key

        radio_audio.roger_beep = settings.roger_beep_enabled
        service.on_ptt = _couple_ptt
        service.on_unkey = _roger_beep
        # 1750 Hz tone call is generated on the mic path, not by the radio
        service.tone_1750_sink = lambda on: setattr(radio_audio, "tone_1750", on)
        # let the band scan read the live RX AF level
        service.level_provider = lambda: radio_audio.rx_db
        await digi.start()
        await sel.start()
        audio_wd = asyncio.create_task(_audio_watchdog())
    apo_task = asyncio.create_task(_auto_power_off_loop())
    ptt_wd = asyncio.create_task(_ptt_safety_loop())
    yield
    if audio_wd:
        audio_wd.cancel()
    await digi.stop()
    await sel.stop()
    apo_task.cancel()
    ptt_wd.cancel()
    await sdr.stop()
    for pc in list(pcs):
        await pc.close()
    pcs.clear()
    radio_audio.stop()
    power_switch.close()
    await service.stop()


app = FastAPI(title="TM-V71 Remote", version=APP_VERSION + ".0", lifespan=lifespan)

@app.exception_handler(TMV71Error)
async def _radio_error_handler(request, exc: TMV71Error) -> JSONResponse:
    """Turn a radio CAT rejection/timeout into a clean 400 with a readable
    message, instead of a bare 500 the UI can't surface."""
    return JSONResponse(status_code=400,
                        content={"detail": f"Radio rejected the command ({exc})."})


# LAN-only deployment: allow any origin on the local network.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)


@app.middleware("http")
async def _track_activity(request, call_next):
    # Any control mutation resets the auto-power-off timer; status polling (GET)
    # does not, so an idle-but-open page still relies on the WebSocket presence.
    if request.method in ("POST", "PUT", "DELETE"):
        touch_activity()
    response = await call_next(request)
    # The GUI assets aren't content-hashed: force the browser to revalidate them
    # (cheap ETag 304s) so a deploy or a VPN reconnect never leaves a stale page
    # cached with a dead WebSocket — which looks exactly like "the API is down".
    path = request.url.path
    if not (path.startswith("/api") or path.startswith("/ws")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# --- control API ------------------------------------------------------------
@app.get("/api/status", response_model=RadioStatus)
async def get_status() -> RadioStatus:
    return service.status


@app.post("/api/frequency", response_model=RadioStatus)
async def set_frequency(req: FrequencyRequest) -> RadioStatus:
    await service.set_frequency(req.band, req.freq_hz)
    return service.status


@app.post("/api/band-mode", response_model=RadioStatus)
async def set_band_mode(req: BandModeRequest) -> RadioStatus:
    await service.set_band_mode(req.band, req.mode)
    return service.status


@app.post("/api/control-band", response_model=RadioStatus)
async def set_control_band(req: ControlBandRequest) -> RadioStatus:
    await service.set_control_band(req.control_band)
    return service.status


@app.post("/api/ptt-band", response_model=RadioStatus)
async def set_ptt_band(req: PttBandRequest) -> RadioStatus:
    await service.set_ptt_band(req.ptt_band)
    return service.status


@app.post("/api/data-band", response_model=RadioStatus)
async def set_data_band(req: DataBandRequest) -> RadioStatus:
    """External data band (MU 38): which band's audio reaches the data port."""
    try:
        return await service.set_data_band(req.band)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"data band: {exc}")


@app.post("/api/tone-1750", response_model=RadioStatus)
async def set_tone_1750(req: Tone1750Request) -> RadioStatus:
    """1750 Hz repeater tone call — generated in software on the mic path
    (sounds while PTT is keyed); the radio's menu-402 hold is not used."""
    try:
        return await service.set_tone_1750(req.on)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"1750 Hz: {exc}")


@app.post("/api/band-display", response_model=RadioStatus)
async def set_band_display(req: BandDisplayRequest) -> RadioStatus:
    await service.set_band_display(req.single, req.band)
    return service.status


@app.post("/api/vfo", response_model=RadioStatus)
async def update_vfo(req: VfoUpdate) -> RadioStatus:
    await service.update_vfo(req)
    return service.status


@app.post("/api/power", response_model=RadioStatus)
async def set_power(req: PowerRequest) -> RadioStatus:
    await service.set_power(req.band, req.level)
    return service.status


@app.post("/api/squelch", response_model=RadioStatus)
async def set_squelch(req: SquelchRequest) -> RadioStatus:
    await service.set_squelch(req.band, req.level)
    # persist per band so it can be restored after a radio power cycle
    if req.band == 0:
        settings.squelch_a = req.level
        save_runtime(squelch_a=req.level)
    else:
        settings.squelch_b = req.level
        save_runtime(squelch_b=req.level)
    return service.status


@app.post("/api/step", response_model=RadioStatus)
async def mic_step(req: StepRequest) -> RadioStatus:
    await service.mic_step(req.band, req.direction == "up")
    return service.status


@app.post("/api/recall", response_model=RadioStatus)
async def recall_memory(req: RecallRequest) -> RadioStatus:
    await service.recall_memory(req.band, req.channel)
    return service.status


@app.post("/api/airband", response_model=RadioStatus)
async def toggle_airband() -> RadioStatus:
    await service.toggle_airband_a()
    return service.status


# --- band scan --------------------------------------------------------------
@app.get("/api/scan")
async def scan_status() -> dict:
    return service.scan_status()


@app.post("/api/scan/start")
async def scan_start(req: ScanStartRequest) -> dict:
    try:
        return await service.start_scan(req.band)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))


@app.post("/api/scan/stop")
async def scan_stop() -> dict:
    service.stop_scan()
    return service.scan_status()


@app.post("/api/ptt", response_model=RadioStatus)
async def set_ptt(req: PttRequest) -> RadioStatus:
    await service.set_ptt(req.transmit)
    _set_remote_ptt(req.transmit)
    return service.status


# --- device info / DTMF / serial config -------------------------------------
@app.get("/api/info", response_model=RadioInfo)
async def get_info() -> RadioInfo:
    return await service.get_info()


@app.get("/api/dtmf", response_model=list[DtmfMemory])
async def list_dtmf() -> list[DtmfMemory]:
    return await service.list_dtmf()


@app.put("/api/dtmf/{channel}", response_model=DtmfMemory)
async def put_dtmf(channel: int, m: DtmfMemory) -> DtmfMemory:
    if not 0 <= channel <= 9:
        raise HTTPException(400, "DTMF channel must be 0-9")
    return await service.set_dtmf(channel, m.code)


@app.post("/api/dtmf/{channel}/send")
async def send_dtmf(channel: int) -> dict:
    if not 0 <= channel <= 9:
        raise HTTPException(400, "DTMF channel must be 0-9")
    code = await service.send_dtmf_memory(channel)
    return {"channel": channel, "sent": code}


@app.get("/api/serial-config")
async def get_serial_config() -> dict:
    ports = []
    try:
        from serial.tools import list_ports
        ports = [{"device": p.device, "description": p.description}
                 for p in list_ports.comports()]
    except Exception:  # noqa: BLE001
        pass
    return {"port": service.radio.port, "baud": service.radio.baudrate,
            "available": ports,
            "bauds": [9600, 19200, 38400, 57600, 115200]}


# --- GPIO power switch (relay on the radio's DC line) ------------------------
def _power_switch_status() -> dict:
    st = power_switch.status()
    st["auto_off_enabled"] = settings.auto_power_off_enabled
    st["auto_off_seconds"] = settings.auto_power_off_seconds
    return st


@app.get("/api/power-switch")
async def get_power_switch() -> dict:
    return _power_switch_status()


@app.post("/api/power-switch")
async def set_power_switch(req: PowerSwitchRequest) -> dict:
    try:
        power_switch.set(req.on)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    if not req.on:
        await _shut_down_peripherals()   # radio off -> stop HackRF + audio peers
    touch_activity()
    return _power_switch_status()


@app.post("/api/auto-power-off")
async def set_auto_power_off(req: AutoPowerOffRequest) -> dict:
    settings.auto_power_off_enabled = req.enabled
    settings.auto_power_off_seconds = req.seconds
    save_runtime(auto_power_off_enabled=req.enabled,
                 auto_power_off_seconds=req.seconds)
    touch_activity()
    return _power_switch_status()


@app.get("/api/callsign")
async def get_callsign() -> dict:
    return {"callsign": settings.callsign}


@app.post("/api/callsign")
async def set_callsign(req: CallsignRequest) -> dict:
    cs = req.callsign.strip().upper()
    settings.callsign = cs
    save_runtime(callsign=cs)
    return {"callsign": cs}


@app.get("/api/theme")
async def get_theme() -> dict:
    return {"theme": settings.theme}


@app.post("/api/theme")
async def set_theme(req: ThemeRequest) -> dict:
    settings.theme = req.theme
    save_runtime(theme=req.theme)
    return {"theme": req.theme}


GITHUB_URL = "https://github.com/CQ-DJ0SH/tmv71-remote"


def _build_date() -> str:
    """Build date: the HEAD commit date, or this file's mtime as a fallback."""
    d = updater.head_date()
    if d:
        return d
    try:
        import datetime
        return datetime.date.fromtimestamp(os.path.getmtime(__file__)).isoformat()
    except Exception:  # noqa: BLE001
        return ""


@app.get("/api/version")
async def get_version() -> dict:
    """App version, build date + source link (radio-independent — page footer)."""
    return {"version": APP_VERSION, "built": _build_date(), "repo": GITHUB_URL}


@app.post("/api/gpio-config")
async def set_gpio_config(cfg: GpioConfigRequest) -> dict:
    power_switch.configure(cfg.pin)
    save_runtime(gpio_power_pin=cfg.pin)
    settings.gpio_power_pin = cfg.pin
    if cfg.pin is not None and not power_switch.available:
        raise HTTPException(400, power_switch.error or "GPIO init failed")
    return _power_switch_status()


@app.post("/api/serial-config")
async def set_serial_config(cfg: SerialConfig) -> dict:
    try:
        model = await service.reconnect(cfg.port, cfg.baud)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"connect failed: {exc}")
    return {"port": cfg.port, "baud": cfg.baud, "model": model,
            "connected": service.status.connected}


# --- branding (title-bar logo) ----------------------------------------------
# The logo lives in a gitignored directory: it may be a trademarked brand logo
# (e.g. Kenwood), so it must never be committed to the public repository.
_BRANDING_DIR = os.path.join(os.path.dirname(__file__), "branding")
_LOGO_EXTS = ("svg", "png", "jpg", "jpeg", "gif", "webp")
# Kenwood wordmark on Wikimedia Commons (File:Kenwood Logo.svg).
KENWOOD_LOGO_URL = "https://upload.wikimedia.org/wikipedia/commons/0/07/Kenwood_Logo.svg"


def _logo_path() -> str | None:
    files = sorted(glob.glob(os.path.join(_BRANDING_DIR, "logo.*")))
    return files[0] if files else None


def _save_logo(data: bytes, ext: str) -> None:
    ext = ext.lstrip(".").lower()
    if ext not in _LOGO_EXTS:
        ext = "png"
    os.makedirs(_BRANDING_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(_BRANDING_DIR, "logo.*")):
        os.remove(old)
    with open(os.path.join(_BRANDING_DIR, f"logo.{ext}"), "wb") as fh:
        fh.write(data)


@app.get("/api/branding")
async def branding_status() -> dict:
    return {"has_logo": bool(_logo_path())}


@app.get("/api/branding/logo")
async def get_logo() -> FileResponse:
    p = _logo_path()
    if not p:
        raise HTTPException(404, "no logo set")
    mt = mimetypes.guess_type(p)[0] or "application/octet-stream"
    return FileResponse(p, media_type=mt,
                        headers={"Cache-Control": "no-cache"})


@app.post("/api/branding/logo")
async def upload_logo(file: UploadFile) -> dict:
    data = await file.read()
    ext = os.path.splitext(file.filename or "")[1] or ".png"
    _save_logo(data, ext)
    return {"has_logo": True}


@app.post("/api/branding/logo/kenwood")
async def fetch_kenwood_logo() -> dict:
    """Download the Kenwood wordmark from Wikimedia Commons (server-side)."""
    req = urllib.request.Request(
        KENWOOD_LOGO_URL,
        headers={"User-Agent": "tmv71-remote/2.1 (LAN ham radio remote)"})

    def _download() -> bytes:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()

    try:
        data = await asyncio.to_thread(_download)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"download failed: {exc}")
    _save_logo(data, "svg")
    return {"has_logo": True}


@app.delete("/api/branding/logo")
async def delete_logo() -> dict:
    for f in glob.glob(os.path.join(_BRANDING_DIR, "logo.*")):
        os.remove(f)
    return {"has_logo": False}


# --- memory channels --------------------------------------------------------
@app.get("/api/memories", response_model=list[MemoryChannel])
async def list_memories(start: int = 0, end: int = 199) -> list[MemoryChannel]:
    if start < 0 or end > 999 or end < start:
        raise HTTPException(400, "invalid channel range")
    return await service.list_memories(start, end)


@app.get("/api/memories/{channel}", response_model=MemoryChannel)
async def get_memory(channel: int) -> MemoryChannel:
    m = await service.get_memory(channel)
    if m is None:
        raise HTTPException(404, "channel empty")
    return m


@app.put("/api/memories/{channel}", response_model=MemoryChannel)
async def put_memory(channel: int, m: MemoryChannel) -> MemoryChannel:
    m.channel = channel
    await service.set_memory(m)
    result = await service.get_memory(channel)
    if result is None:
        raise HTTPException(500, "write failed")
    return result


@app.delete("/api/memories/{channel}")
async def delete_memory(channel: int) -> dict:
    await service.delete_memory(channel)
    return {"deleted": channel}


@app.get("/api/memories.csv")
async def export_memories_csv(start: int = 0, end: int = 999) -> Response:
    rows = await service.list_memories(start, end)
    return Response(memory_io.export_csv(rows), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=tmv71-memories.csv"})


@app.post("/api/memories/import")
async def import_memories_csv(file: UploadFile) -> dict:
    text = (await file.read()).decode("utf-8-sig")
    channels = memory_io.import_csv(text)
    for m in channels:
        await service.set_memory(m)
    return {"imported": len(channels)}


# --- WebRTC audio (direct browser <-> radio, Opus) --------------------------
@app.get("/api/audio/status")
async def audio_status() -> dict:
    if not settings.audio_enabled:
        return {"enabled": False}
    return radio_audio.status()


@app.post("/api/audio/gain")
async def set_audio_gain(req: AudioGainRequest) -> dict:
    if req.rx_gain is not None:
        radio_audio.rx_gain = req.rx_gain
    if req.tx_gain is not None:
        radio_audio.tx_gain = req.tx_gain
    save_runtime(rx_gain=radio_audio.rx_gain, tx_gain=radio_audio.tx_gain)
    settings.rx_gain = radio_audio.rx_gain
    settings.tx_gain = radio_audio.tx_gain
    return radio_audio.status()


@app.post("/api/audio/buffer")
async def set_audio_buffer(req: AudioBufferRequest) -> dict:
    radio_audio.set_tx_timing(tx_buffer_ms=req.tx_buffer_ms,
                              ptt_tail_ms=req.ptt_tail_ms)
    settings.tx_buffer_ms = radio_audio.tx_buffer_ms
    settings.ptt_tail_ms = radio_audio.ptt_tail_ms
    save_runtime(tx_buffer_ms=radio_audio.tx_buffer_ms,
                 ptt_tail_ms=radio_audio.ptt_tail_ms)
    return radio_audio.status()


@app.get("/api/audio/devices")
async def audio_devices() -> dict:
    return {"current": radio_audio.device, "devices": radio_audio.list_devices()}


@app.post("/api/audio/device")
async def set_audio_device(req: AudioDeviceRequest) -> dict:
    radio_audio.set_device(req.device)
    save_runtime(audio_device=req.device)
    settings.audio_device = req.device
    if not radio_audio.connected:
        raise HTTPException(400, radio_audio.error or "audio device open failed")
    return radio_audio.status()


@app.post("/api/audio/tones")
async def set_audio_tones(req: TonesRequest) -> dict:
    if req.roger_beep is not None:
        radio_audio.roger_beep = req.roger_beep
        settings.roger_beep_enabled = req.roger_beep
        save_runtime(roger_beep_enabled=req.roger_beep)
    if req.test_tone is not None:
        radio_audio.test_tone = req.test_tone
    if req.mic_test is not None:
        # records while on; on switch-off, replays the take over RX (no keying)
        radio_audio.set_mic_test(req.mic_test)
        if not req.mic_test and not radio_audio._ptt_open:
            radio_audio.tx_db = None      # clear the meter when leaving mic test
    if req.tx_lowpass is not None:
        radio_audio.tx_lowpass = req.tx_lowpass
        settings.tx_lowpass_enabled = req.tx_lowpass
        save_runtime(tx_lowpass_enabled=req.tx_lowpass)
    if req.rx_lowpass is not None:
        radio_audio.rx_lowpass = req.rx_lowpass
        settings.rx_lowpass_enabled = req.rx_lowpass
        save_runtime(rx_lowpass_enabled=req.rx_lowpass)
    return radio_audio.status()


# --- digimodes: CW / RTTY decode + encode -----------------------------------
@app.get("/api/digi")
async def digi_status() -> dict:
    return digi.status()


@app.post("/api/digi/config")
async def digi_configure(cfg: DigiConfig) -> dict:
    if not settings.audio_enabled:
        raise HTTPException(503, "audio disabled")
    return digi.configure(cfg)


@app.post("/api/digi/tx")
async def digi_transmit(req: DigiTxRequest) -> dict:
    if not settings.audio_enabled:
        raise HTTPException(503, "audio disabled")
    return await digi.transmit(req.text, service.set_ptt)


@app.websocket("/ws/digi")
async def ws_digi(ws: WebSocket) -> None:
    await ws.accept()
    q = digi.subscribe()
    try:
        await ws.send_json({"t": "status", **digi.status()})
        while True:
            try:
                text = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                await ws.send_json({"t": "idle"})       # keep-alive / detect drop
                continue
            await ws.send_json({"t": "rx", "text": text})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        digi.unsubscribe(q)


# --- selcall: classic 5-tone selective calling ------------------------------
@app.get("/api/selcall")
async def selcall_status() -> dict:
    return sel.status()


@app.post("/api/selcall/config")
async def selcall_configure(cfg: SelcallConfig) -> dict:
    if not settings.audio_enabled:
        raise HTTPException(503, "audio disabled")
    return sel.configure(cfg)


@app.post("/api/selcall/tx")
async def selcall_transmit(req: SelcallTxRequest) -> dict:
    if not settings.audio_enabled:
        raise HTTPException(503, "audio disabled")
    return await sel.transmit(req.code, service.set_ptt)


@app.websocket("/ws/selcall")
async def ws_selcall(ws: WebSocket) -> None:
    await ws.accept()
    q = sel.subscribe()
    try:
        await ws.send_json({"t": "status", **sel.status()})
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                await ws.send_json({"t": "idle"})
                continue
            await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        sel.unsubscribe(q)


@app.get("/api/system")
async def get_system() -> dict:
    """Raspberry Pi host metrics for the settings ▸ Hardware tab."""
    return await asyncio.to_thread(system_info.collect)


@app.get("/api/update")
async def update_status() -> dict:
    """Check the git checkout against its remote (settings ▸ Software update)."""
    return await asyncio.to_thread(updater.status)


@app.post("/api/update")
async def update_apply() -> dict:
    """Pull the latest code and restart the service."""
    return await asyncio.to_thread(updater.apply)


@app.get("/api/audio/mixer")
async def get_audio_mixer() -> dict:
    return await asyncio.to_thread(mixer.list_controls)


@app.post("/api/audio/mixer")
async def set_audio_mixer(req: MixerSetRequest) -> dict:
    try:
        return await asyncio.to_thread(
            mixer.set_control, req.name, req.percent, req.switch_on)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))


def _force_opus_mono(sdp: str) -> str:
    """Pin Opus to a single channel in both directions by setting
    stereo=0;sprop-stereo=0 on every Opus fmtp line. The radio audio is mono;
    without this Chrome negotiates stereo Opus and our mono RX frame only fills
    the left channel (the right carries static), so per-sample processing like
    the RX low-pass appears to affect just one ear."""
    sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(sep)
    pts = re.findall(r"a=rtpmap:(\d+) opus/48000", sdp)
    for pt in pts:
        prefix = f"a=fmtp:{pt} "
        idx = next((i for i, ln in enumerate(lines) if ln.startswith(prefix)), None)
        if idx is None:
            rt = next((i for i, ln in enumerate(lines)
                       if ln.startswith(f"a=rtpmap:{pt} opus")), None)
            if rt is not None:
                lines.insert(rt + 1, prefix + "minptime=10;useinbandfec=1;"
                                              "stereo=0;sprop-stereo=0")
            continue
        params = [p for p in lines[idx][len(prefix):].split(";") if p.strip()]
        kept = [p for p in params
                if p.split("=", 1)[0].strip() not in ("stereo", "sprop-stereo")]
        kept += ["stereo=0", "sprop-stereo=0"]
        lines[idx] = prefix + ";".join(kept)
    return sep.join(lines)


@app.post("/api/webrtc/offer")
async def webrtc_offer(req: WebRTCOffer) -> dict:
    """Browser SDP offer -> answer. Adds the radio RX track and consumes the
    browser mic track (Opus both ways, forced mono). Non-trickle ICE."""
    pc = RTCPeerConnection()
    pcs.add(pc)
    radio_audio.peers += 1

    @pc.on("connectionstatechange")
    async def _on_state() -> None:
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await pc.close()
            if pc in pcs:
                pcs.discard(pc)
                radio_audio.peers = max(0, radio_audio.peers - 1)

    @pc.on("track")
    def _on_track(track) -> None:
        if track.kind == "audio":
            asyncio.ensure_future(consume_mic(track, radio_audio))

    pc.addTrack(RadioRxTrack(radio_audio))
    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=_force_opus_mono(req.sdp), type=req.type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    while pc.iceGatheringState != "complete":   # non-trickle: wait for gather
        await asyncio.sleep(0.05)
    return {"sdp": _force_opus_mono(pc.localDescription.sdp),
            "type": pc.localDescription.type}


# --- live status WebSocket --------------------------------------------------
@app.websocket("/ws")
async def ws_status(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        await ws.send_json(service.status.model_dump())
        while True:
            # We only push; ignore anything the client sends but keep the
            # socket alive / detect disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception:  # noqa: BLE001
        await manager.disconnect(ws)



# --- HackRF spectrum / waterfall --------------------------------------------
@app.get("/api/hackrf")
async def hackrf_status() -> dict:
    await sdr.detect()        # refresh the cached "device connected" probe
    return sdr.status()


@app.post("/api/hackrf/start")
async def hackrf_start(cfg: HackRFConfig) -> dict:
    return await sdr.start(**cfg.model_dump(exclude_none=True))


@app.post("/api/hackrf/stop")
async def hackrf_stop() -> dict:
    return await sdr.stop()


@app.post("/api/hackrf/config")
async def hackrf_config(cfg: HackRFConfig) -> dict:
    return await sdr.configure(**cfg.model_dump(exclude_none=True))


@app.websocket("/ws/hackrf")
async def ws_hackrf(ws: WebSocket) -> None:
    await ws.accept()
    q = sdr.subscribe()
    try:
        await ws.send_json({"t": "status", **sdr.status()})
        while True:
            try:
                frame = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                await ws.send_json({"t": "idle"})   # keep-alive / detect disconnect
                continue
            await ws.send_json(frame)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        sdr.unsubscribe(q)


# --- static frontend (catch-all at "/", must be mounted last) ----------------
_frontend = os.path.join(os.path.dirname(__file__), settings.frontend_dir)
if os.path.isdir(_frontend):
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")

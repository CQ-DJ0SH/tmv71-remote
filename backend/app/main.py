"""FastAPI application: REST control endpoints + live-status WebSocket.

Serves the built SPA frontend from ``settings.frontend_dir`` at "/" when present.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import mimetypes
import os
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
                     ControlBandRequest, DataBandRequest,
                     DtmfMemory, FrequencyRequest, GpioConfigRequest,
                     AudioBufferRequest,
                     MemoryChannel, MixerSetRequest, PowerRequest,
                     PowerSwitchRequest, TonesRequest,
                     PttBandRequest, PttRequest, RadioInfo, RadioStatus,
                     Tone1750Request,
                     RecallRequest, ScanStartRequest, SerialConfig,
                     SquelchRequest, StepRequest, VfoUpdate, WebRTCOffer)
from . import mixer
from . import system_info
from . import updater
from .power_switch import PowerSwitch
from .webrtc_audio import RadioAudio, RadioRxTrack, consume_mic
from aiortc import RTCPeerConnection, RTCSessionDescription
from .radio_service import RadioService
from .tmv71 import TMV71Error
from .state import ConnectionManager

logging.basicConfig(level=logging.INFO)

manager = ConnectionManager()
service = RadioService(manager)


power_switch = PowerSwitch(settings.gpio_power_pin, settings.gpio_active_high)
radio_audio = RadioAudio(device=settings.audio_device,
                         rx_gain=settings.rx_gain, tx_gain=settings.tx_gain,
                         tx_buffer_ms=settings.tx_buffer_ms,
                         ptt_tail_ms=settings.ptt_tail_ms)
pcs: set = set()      # active WebRTC peer connections

# Auto power off: the backend itself cuts GPIO power after a period of
# inactivity, so the radio shuts down even when the browser is gone (no API
# call required). Activity = any control command OR a connected WebSocket.
_last_activity = time.monotonic()


def touch_activity() -> None:
    global _last_activity
    _last_activity = time.monotonic()


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
                logging.getLogger("tmv71").info(
                    "auto power off after %ds inactivity",
                    settings.auto_power_off_seconds)
            except Exception:  # noqa: BLE001
                pass
            touch_activity()   # avoid immediate re-trigger


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
                await asyncio.sleep(0.16)        # let the beep finish before un-key

        radio_audio.roger_beep = settings.roger_beep_enabled
        service.on_ptt = _couple_ptt
        service.on_unkey = _roger_beep
        # let the band scan read the live RX AF level
        service.level_provider = lambda: radio_audio.rx_db
        audio_wd = asyncio.create_task(_audio_watchdog())
    apo_task = asyncio.create_task(_auto_power_off_loop())
    yield
    if audio_wd:
        audio_wd.cancel()
    apo_task.cancel()
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
    return await call_next(request)


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
    """1750 Hz tone hold (menu 402)."""
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
        headers={"User-Agent": "tmv71-remote/1.3 (LAN ham radio remote)"})

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
    return radio_audio.status()


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


@app.post("/api/webrtc/offer")
async def webrtc_offer(req: WebRTCOffer) -> dict:
    """Browser SDP offer -> answer. Adds the radio RX track and consumes the
    browser mic track (Opus both ways). Non-trickle ICE (LAN host candidates)."""
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
    await pc.setRemoteDescription(RTCSessionDescription(sdp=req.sdp, type=req.type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    while pc.iceGatheringState != "complete":   # non-trickle: wait for gather
        await asyncio.sleep(0.05)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


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



# --- static frontend (catch-all at "/", must be mounted last) ----------------
_frontend = os.path.join(os.path.dirname(__file__), settings.frontend_dir)
if os.path.isdir(_frontend):
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")

"""Direct 2-way browser audio via WebRTC (aiortc) <-> radio USB sound device.

Replaces the Mumble path entirely: the browser talks WebRTC (Opus) straight to
this backend. The radio's RX audio (NAD USB capture) is sent to the browser;
the browser microphone is fed to the radio's mic input while PTT is engaged.
No murmur / mumble-web / mumble-web-proxy required.

Audio is 48 kHz / 16-bit / mono internally (Opus' native rate). The USB device
captures mono and plays back stereo, so mono is duplicated onto L/R.
"""
from __future__ import annotations

import asyncio
import fractions
import logging
import threading
import time
from collections import deque
from typing import Optional

import numpy as np
import sounddevice as sd
from av import AudioFrame
from av.audio.resampler import AudioResampler
from aiortc import MediaStreamTrack

log = logging.getLogger("tmv71.audio")

SAMPLE_RATE = 48000
BLOCK = 960          # 20 ms @ 48 kHz
DEF_TX_BUFFER_MS = 250    # default mic backlog cap — bounds TX latency vs jitter
DEF_PTT_TAIL_MS = 250     # default post-release transmit tail (drain settle)


def _level(samples, prev):
    """RMS of an int16 block as dBFS, with fast-attack / slow-release."""
    if samples.size == 0:
        return prev
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    db = 20.0 * np.log10(rms / 32768.0) if rms >= 1.0 else -90.0
    if prev is None:
        return round(db, 1)
    a = 0.6 if db > prev else 0.25       # snap up, ease down
    return round(a * db + (1 - a) * prev, 1)


class RadioAudio:
    """Owns the full-duplex NAD stream; fans RX out to WebRTC subscribers and
    accepts TX (browser mic) into the radio mic, gated by PTT."""

    def __init__(self, device: str = "NAD", rx_gain: float = 1.0,
                 tx_gain: float = 1.0, tx_buffer_ms: int = DEF_TX_BUFFER_MS,
                 ptt_tail_ms: int = DEF_PTT_TAIL_MS):
        self.device = device
        self.rx_gain = rx_gain
        self.tx_gain = tx_gain
        # TX timing (see set_tx_timing); stored in ms, applied as samples/seconds.
        self.tx_buffer_ms = int(tx_buffer_ms)
        self.ptt_tail_ms = int(ptt_tail_ms)
        self._stream: Optional[sd.Stream] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # latest captured RX block (bytes) + monotonic timestamp of its capture.
        # The WebRTC track is clock-paced and reads this; if the capture stalls
        # the block goes stale and the track emits silence instead of freezing.
        self._latest: Optional[bytes] = None
        self._latest_ts: float = 0.0
        self._playback: deque = deque()
        self._pb_lock = threading.Lock()
        self._ptt_open = False
        self.connected = False
        self.error: Optional[str] = None
        self.rx_frames = 0
        self.rx_db: Optional[float] = None
        self.tx_db: Optional[float] = None
        self.peers = 0
        # TX tone generators (replace the browser mic on the radio mic path)
        self.test_tone = False        # continuous 700+1900 Hz two-tone while keyed
        self.roger_beep = False       # short beep on un-key (preference)
        self._tone_phase = 0          # two-tone phase (sample counter, wraps)
        self._beep_phase = 0          # roger-beep phase
        self._beep_left = 0           # remaining roger-beep samples

    # -- device ------------------------------------------------------------
    def _find_device(self) -> int:
        devs = sd.query_devices()
        # 1) configured substring match (e.g. "NAD")
        for idx, d in enumerate(devs):
            if self.device.lower() in d["name"].lower():
                return idx
        # 2) fall back to the first full-duplex device. A USB radio interface
        #    can re-enumerate without its iProduct string, renaming it from
        #    "NAD USB Audio…" to "USB Device 0x17ae…" — which breaks the name
        #    match. The duplex (capture+playback) card is still the right one.
        for idx, d in enumerate(devs):
            if d["max_input_channels"] > 0 and d["max_output_channels"] > 0:
                log.warning("device %r not found by name; using full-duplex "
                            "device %d (%s)", self.device, idx, d["name"])
                return idx
        raise RuntimeError(f"audio device matching {self.device!r} not found")

    @staticmethod
    def list_devices() -> list[dict]:
        """Full-duplex-capable sound devices (need both capture and playback)."""
        out = []
        try:
            for idx, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and d["max_output_channels"] > 0:
                    out.append({"index": idx, "name": d["name"]})
        except Exception as exc:  # noqa: BLE001
            log.error("query_devices failed: %s", exc)
        return out

    def _open_stream(self) -> None:
        dev = self._find_device()
        self._stream = sd.Stream(
            samplerate=SAMPLE_RATE, blocksize=BLOCK, device=dev,
            channels=(1, 2), dtype="int16", callback=self._callback)
        self._stream.start()

    def _stop_stream(self) -> None:
        try:
            if self._stream:
                self._stream.stop(); self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        self._stream = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        try:
            self._open_stream()
            self.connected = True
            self.error = None
            log.info("WebRTC radio audio started on %s", self.device)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.connected = False
            log.error("WebRTC radio audio failed to start: %s", exc)

    def set_device(self, name: str) -> None:
        """Switch the sound device (substring match) and reopen the stream."""
        self._stop_stream()
        self.device = name
        try:
            self._open_stream()
            self.connected = True
            self.error = None
            log.info("WebRTC radio audio switched to %s", self.device)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.connected = False
            log.error("audio device switch to %r failed: %s", name, exc)

    def reopen(self) -> None:
        """Tear down and reopen the capture/playback stream (stall recovery)."""
        self._stop_stream()
        # Refresh PortAudio's cached device list. After a USB re-enumeration the
        # old ALSA mapping is stale, so opening fails with PaErrorCode -9999
        # ("PaAlsaStream_Configure failed"); a terminate+initialize re-reads the
        # current devices so _find_device picks up the new index/name.
        try:
            sd._terminate(); sd._initialize()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._open_stream()
            self.connected = True
            self.error = None
            log.info("WebRTC radio audio stream reopened")
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.connected = False
            log.error("audio stream reopen failed: %s", exc)

    def stop(self) -> None:
        self.connected = False
        self._stop_stream()

    # -- PortAudio callback (runs in PortAudio thread) ---------------------
    def _callback(self, indata, outdata, frames, time_info, status):
        if status:
            log.debug("audio status: %s", status)
        # RX: radio capture (mono ch0) -> WebRTC subscribers
        samples = indata[:, 0].copy()
        if self.rx_gain != 1.0:
            samples = np.clip(samples.astype(np.float32) * self.rx_gain,
                              -32768, 32767).astype(np.int16)
        self.rx_frames += 1
        self.rx_db = _level(samples, self.rx_db)
        # publish the latest block; the clock-paced RX track(s) pick it up.
        self._latest = samples.tobytes()
        self._latest_ts = time.monotonic()
        # TX: radio mic source. The two-tone test is emitted continuously on the
        # mic line regardless of PTT (so deviation can be set without holding the
        # key); the roger beep and queued browser mic only play while keyed.
        mono = np.zeros(frames, dtype=np.int16)
        if self.test_tone:
            mono = self._gen_tone(frames, (700.0, 1900.0), 0.40, "_tone_phase")
        elif self._ptt_open:
            if self._beep_left > 0:
                k = min(frames, self._beep_left)
                mono[:k] = self._gen_tone(k, (1000.0,), 0.5, "_beep_phase")
                self._beep_left -= k
            else:
                with self._pb_lock:
                    n = min(frames, len(self._playback))
                    for i in range(n):
                        mono[i] = self._playback.popleft()
        outdata[:, 0] = mono
        if outdata.shape[1] > 1:
            outdata[:, 1] = mono

    def _gen_tone(self, n, freqs, amp, phase_attr):
        """n samples of the summed sine `freqs`; phase counter wraps at the
        sample rate so it stays continuous and precise for integer Hz."""
        ph = getattr(self, phase_attr)
        idx = ph + np.arange(n, dtype=np.float64)
        sig = np.zeros(n, dtype=np.float64)
        for f in freqs:
            sig += np.sin(2 * np.pi * f * idx / SAMPLE_RATE)
        sig /= len(freqs)
        setattr(self, phase_attr, int((ph + n) % SAMPLE_RATE))
        return np.clip(sig * amp * 32767, -32768, 32767).astype(np.int16)

    def trigger_roger_beep(self) -> None:
        """Queue a short beep on the mic path — call while still keyed."""
        with self._pb_lock:
            self._playback.clear()         # drop trailing mic; beep only
        self._beep_phase = 0
        self._beep_left = int(SAMPLE_RATE * 0.14)   # 140 ms

    def latest_block(self) -> Optional[bytes]:
        """Most recent RX block, or None if the capture has gone stale
        (callback stopped firing) — the track emits silence in that case."""
        if self._latest is None:
            return None
        if time.monotonic() - self._latest_ts > 0.1:   # ~5 blocks late -> stale
            return None
        return self._latest

    # -- TX (browser mic) --------------------------------------------------
    def push_tx(self, pcm: np.ndarray) -> None:
        # Only buffer mic audio while keyed. The browser mic streams over WebRTC
        # continuously (the track isn't gated by PTT), so between overs it would
        # otherwise pile up a full backlog; on the next key-up that stale audio
        # plays out first (~1 s of latency) and is then cut on release. Dropping
        # it while un-keyed makes every transmission start fresh and low-latency.
        if not self._ptt_open:
            return
        if self.tx_gain != 1.0:
            pcm = np.clip(pcm.astype(np.float32) * self.tx_gain,
                          -32768, 32767).astype(np.int16)
        self.tx_db = _level(pcm, self.tx_db)
        with self._pb_lock:
            self._playback.extend(pcm.tolist())
            # keep the backlog tight so TX stays low-latency (bound jitter, not 1 s)
            cap = max(BLOCK, self.tx_buffer_ms * SAMPLE_RATE // 1000)
            excess = len(self._playback) - cap
            for _ in range(max(0, excess)):
                self._playback.popleft()

    async def drain_tx(self, settle: Optional[float] = None,
                       max_wait: float = 0.5) -> None:
        """On key-up, wait for the queued TX (mic) tail to play out before the
        radio un-keys, so the last words aren't chopped. `settle` (defaults to
        the configured ptt_tail_ms) first lets the WebRTC frames still in flight
        at release arrive and get queued (only enqueued while still keyed); then
        we wait until the playback backlog has drained at real-time rate.
        Bounded by `max_wait` so a stalled stream can't hold TX open."""
        if settle is None:
            settle = self.ptt_tail_ms / 1000.0
        start = time.monotonic()
        if settle > 0:
            await asyncio.sleep(settle)
        while time.monotonic() - start < settle + max_wait:
            with self._pb_lock:
                if not self._playback:
                    break
            await asyncio.sleep(0.02)

    def set_tx_timing(self, tx_buffer_ms: Optional[int] = None,
                      ptt_tail_ms: Optional[int] = None) -> None:
        if tx_buffer_ms is not None:
            self.tx_buffer_ms = int(tx_buffer_ms)
        if ptt_tail_ms is not None:
            self.ptt_tail_ms = int(ptt_tail_ms)

    def set_ptt_open(self, is_open: bool) -> None:
        if not is_open:
            with self._pb_lock:
                self._playback.clear()
            self.tx_db = None
        self._ptt_open = is_open

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected, "error": self.error,
                "rx_frames": self.rx_frames, "ptt_open": self._ptt_open,
                "rx_db": self.rx_db, "tx_db": self.tx_db,
                "rx_gain": self.rx_gain, "tx_gain": self.tx_gain,
                "tx_buffer_ms": self.tx_buffer_ms, "ptt_tail_ms": self.ptt_tail_ms,
                "device": self.device, "peers": self.peers,
                "test_tone": self.test_tone, "roger_beep": self.roger_beep,
                "transport": "webrtc", "web_client": self.peers > 0}


class RadioRxTrack(MediaStreamTrack):
    """Outgoing track: radio RX audio -> browser.

    Clock-paced: emits one 20 ms frame every 20 ms of wall time, independent of
    the PortAudio callback. It reads the radio's most recent capture block; if
    the capture has stalled (block stale) it emits silence. This keeps the
    WebRTC media timeline locked to real time, so a capture glitch produces a
    short gap of silence instead of a frozen stream that the browser jitter
    buffer can never recover from. When capture resumes, audio returns seamless.
    """
    kind = "audio"

    def __init__(self, radio: RadioAudio):
        super().__init__()
        self._radio = radio
        self._pts = 0
        self._start: Optional[float] = None
        self._silence = np.zeros((1, BLOCK), dtype=np.int16)

    async def recv(self) -> AudioFrame:
        loop = asyncio.get_event_loop()
        if self._start is None:
            self._start = loop.time()
        else:
            # pace to wall clock: frame N is due at start + N*20ms
            target = self._start + (self._pts + BLOCK) / SAMPLE_RATE
            await asyncio.sleep(max(0.0, target - loop.time()))
        data = self._radio.latest_block()
        if data is None:
            arr = self._silence
        else:
            arr = np.frombuffer(data, dtype=np.int16).reshape(1, -1)
            if arr.shape[1] != BLOCK:
                arr = self._silence
        frame = AudioFrame.from_ndarray(arr, format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._pts += BLOCK
        return frame

    def stop(self) -> None:
        super().stop()


async def consume_mic(track: MediaStreamTrack, radio: RadioAudio) -> None:
    """Incoming track: browser mic -> radio mic (resampled to 48 kHz mono)."""
    resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    try:
        while True:
            frame = await track.recv()
            for f in resampler.resample(frame):
                pcm = f.to_ndarray().reshape(-1).astype(np.int16)
                radio.push_tx(pcm)
    except Exception:  # noqa: BLE001
        pass

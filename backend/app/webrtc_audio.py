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
TX_LP_CUTOFF = 3500.0     # voice low-pass cutoff (Hz) for the TX mic path
RX_LP_CUTOFF = 3500.0     # voice low-pass cutoff (Hz) for the RX path (de-hiss)


class _FIRLowpass:
    """Windowed-sinc FIR low-pass, stateful across blocks via overlap-save.

    Vectorised (np.convolve), so it adds no per-sample Python loop to the audio
    path. ``process`` filters an int16 mono block and returns int16; the last
    ntaps-1 samples are carried over so block boundaries stay continuous."""

    def __init__(self, fc: float, fs: float, ntaps: int = 63):
        n = np.arange(ntaps)
        m = (ntaps - 1) / 2.0
        fcn = fc / (fs / 2.0)                       # cutoff as a fraction of Nyquist
        h = fcn * np.sinc(fcn * (n - m)) * np.hamming(ntaps)
        self.h = (h / h.sum()).astype(np.float64)   # unity DC gain
        self._tail = np.zeros(ntaps - 1, dtype=np.float64)

    def process(self, pcm: np.ndarray) -> np.ndarray:
        x = np.concatenate([self._tail, pcm.astype(np.float64)])
        y = np.convolve(x, self.h, mode="valid")    # len == len(pcm)
        self._tail = x[-(self.h.size - 1):]
        return np.clip(y, -32768, 32767).astype(np.int16)

    def reset(self) -> None:
        self._tail[:] = 0.0


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
                 ptt_tail_ms: int = DEF_PTT_TAIL_MS, tx_lowpass: bool = False,
                 rx_lowpass: bool = False, tx_auto_gain: bool = False):
        self.device = device
        self.rx_gain = rx_gain
        self.tx_gain = tx_gain
        # TX auto-gain (AGC on the mic path): drives the level toward a target,
        # overriding the manual tx_gain while on. _agc_gain is the live factor.
        self.tx_auto_gain = tx_auto_gain
        self._agc_gain = 1.0
        # Optional voice low-pass on the TX (mic) and RX paths.
        self.tx_lowpass = tx_lowpass
        self.rx_lowpass = rx_lowpass
        self._tx_lp = _FIRLowpass(TX_LP_CUTOFF, SAMPLE_RATE)
        self._rx_lp = _FIRLowpass(RX_LP_CUTOFF, SAMPLE_RATE)
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
        self.tone_1750 = False        # 1750 Hz repeater tone-call while keyed
        self.roger_beep = False       # short beep on un-key (preference)
        self.mic_test = False         # meter the browser mic without keying the radio
        # mic-test echo: record the mic while MIC TEST is on, then replay it over
        # the RX path once it's switched off (no RF, no keying).
        self._mic_rec_chunks: list = []
        self._echo_lock = threading.Lock()
        self._echo_buf: Optional[np.ndarray] = None   # samples being replayed
        self._echo_pos = 0
        self._mic_rec_cap = 30 * SAMPLE_RATE          # keep at most ~30 s
        # digimodes: tap RX for the decoder; inject CW/RTTY audio on TX
        self.digi_rx = False
        self._digi_rx_chunks: list = []
        self.sel_rx = False                          # 5-tone selcall decoder tap
        self._sel_rx_chunks: list = []
        self._digi_lock = threading.Lock()
        self._digi_tx: Optional[np.ndarray] = None   # queued encoded samples
        self._digi_pos = 0
        self._tone_phase = 0          # two-tone phase (sample counter, wraps)
        self._t1750_phase = 0         # 1750 Hz tone phase
        self._beep_phase = 0          # roger-beep phase
        self._beep_left = 0           # remaining roger-beep samples
        self._beep_seg = 0            # samples per tone of the two-tone roger beep
        self._beep_second = False     # second tone (1750 Hz) reached

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
        if self.rx_lowpass:
            samples = self._rx_lp.process(samples)
        self.rx_frames += 1
        self.rx_db = _level(samples, self.rx_db)
        # digimodes decoder tap: stash RX blocks for the decode loop to consume
        if self.digi_rx or self.sel_rx:
            with self._digi_lock:
                if self.digi_rx:
                    self._digi_rx_chunks.append(samples.copy())
                    if len(self._digi_rx_chunks) > 250:  # ~5 s cap, drop oldest
                        self._digi_rx_chunks.pop(0)
                if self.sel_rx:
                    self._sel_rx_chunks.append(samples.copy())
                    if len(self._sel_rx_chunks) > 250:
                        self._sel_rx_chunks.pop(0)
        # mic-test echo replay: while a recording is being played back, override
        # the published RX block with it (single pos writer = this callback).
        pub = samples
        if self._echo_buf is not None:
            e = self._echo_buf
            blk = np.zeros(frames, dtype=np.int16)
            k = min(frames, len(e) - self._echo_pos)
            if k > 0:
                blk[:k] = e[self._echo_pos:self._echo_pos + k]
                self._echo_pos += k
            if self._echo_pos >= len(e):
                self._echo_buf = None
            pub = blk
            self.rx_db = _level(blk, self.rx_db)
        elif self.mic_test:
            # mute the radio RX while a mic test is recording, so only the test
            # (and its replay on switch-off) is heard — not live radio audio.
            pub = np.zeros(frames, dtype=np.int16)
        # publish the latest block; the clock-paced RX track(s) pick it up.
        self._latest = pub.tobytes()
        self._latest_ts = time.monotonic()
        # TX: radio mic source. The two-tone test is emitted continuously on the
        # mic line regardless of PTT (so deviation can be set without holding the
        # key); the roger beep and queued browser mic only play while keyed.
        mono = np.zeros(frames, dtype=np.int16)
        if self.test_tone:
            mono = self._gen_tone(frames, (700.0, 1900.0), 0.40, "_tone_phase")
        elif self._ptt_open and self._digi_tx is not None:
            dt = self._digi_tx                       # CW/RTTY encoded audio
            k = min(frames, len(dt) - self._digi_pos)
            if k > 0:
                mono[:k] = dt[self._digi_pos:self._digi_pos + k]
                self._digi_pos += k
            if self._digi_pos >= len(dt):
                self._digi_tx = None                 # transmission finished
        elif self._ptt_open:
            if self.tone_1750:
                mono = self._gen_tone(frames, (1750.0,), 0.5, "_t1750_phase")
            elif self._beep_left > 0:
                k = min(frames, self._beep_left)
                # two-tone: first segment 1000 Hz, second 1750 Hz (reset the
                # phase at the change for a clean edge)
                if self._beep_left > self._beep_seg:
                    freq = 1000.0
                else:
                    if not self._beep_second:
                        self._beep_phase = 0
                        self._beep_second = True
                    freq = 1750.0
                mono[:k] = self._gen_tone(k, (freq,), 0.5, "_beep_phase")
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
        self._beep_second = False
        # two-tone roger beep: 1000 Hz then 1750 Hz, 125 ms each (250 ms total)
        self._beep_seg = int(SAMPLE_RATE * 0.125)
        self._beep_left = 2 * self._beep_seg

    # -- digimodes (CW/RTTY) ----------------------------------------------
    def pop_digi_rx(self) -> Optional[np.ndarray]:
        """Drain and return the RX samples captured since the last call."""
        with self._digi_lock:
            if not self._digi_rx_chunks:
                return None
            chunks = self._digi_rx_chunks
            self._digi_rx_chunks = []
        return np.concatenate(chunks)

    def set_digi_rx(self, on: bool) -> None:
        self.digi_rx = on
        if not on:
            with self._digi_lock:
                self._digi_rx_chunks = []

    def pop_sel_rx(self) -> Optional[np.ndarray]:
        with self._digi_lock:
            if not self._sel_rx_chunks:
                return None
            chunks = self._sel_rx_chunks
            self._sel_rx_chunks = []
        return np.concatenate(chunks)

    def set_sel_rx(self, on: bool) -> None:
        self.sel_rx = on
        if not on:
            with self._digi_lock:
                self._sel_rx_chunks = []

    def play_digi(self, pcm: np.ndarray) -> None:
        """Queue encoded CW/RTTY audio for the radio mic (played while keyed)."""
        self._digi_pos = 0
        self._digi_tx = pcm.astype(np.int16)

    def digi_tx_busy(self) -> bool:
        return self._digi_tx is not None

    def stop_digi_tx(self) -> None:
        self._digi_tx = None

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
        # Mic test: meter the mic level continuously (gain + low-pass applied so
        # it matches what TX would send) without routing anything to the radio.
        if not self._ptt_open and not self.mic_test:
            return
        if self.tx_auto_gain:
            # Simple AGC: aim for a target RMS. Lower the gain fast (avoid clipping
            # on loud bursts), raise it slowly, and hold it through pauses (a noise
            # gate keeps quiet gaps from being pumped up). Capped so noise can't
            # run away. Overrides the manual tx_gain while enabled.
            x = pcm.astype(np.float32)
            rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
            TARGET, NOISE, MAXG, MING = 5000.0, 180.0, 12.0, 0.3
            if rms > NOISE:
                desired = min(MAXG, max(MING, TARGET / rms))
                a = 0.5 if desired < self._agc_gain else 0.04   # fast down, slow up
                self._agc_gain += (desired - self._agc_gain) * a
            pcm = np.clip(x * self._agc_gain, -32768, 32767).astype(np.int16)
        elif self.tx_gain != 1.0:
            pcm = np.clip(pcm.astype(np.float32) * self.tx_gain,
                          -32768, 32767).astype(np.int16)
        if self.tx_lowpass:
            pcm = self._tx_lp.process(pcm)
        self.tx_db = _level(pcm, self.tx_db)
        if not self._ptt_open:
            # mic test only: level measured, nothing to the radio. Record the
            # audio so it can be replayed when MIC TEST is switched off.
            if self.mic_test:
                with self._echo_lock:
                    self._mic_rec_chunks.append(pcm.copy())
                    total = sum(len(c) for c in self._mic_rec_chunks)
                    while total > self._mic_rec_cap and len(self._mic_rec_chunks) > 1:
                        total -= len(self._mic_rec_chunks.pop(0))
            return
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
        else:
            self._tx_lp.reset()      # clear filter state at the start of each over
        self._ptt_open = is_open

    # -- mic-test echo -----------------------------------------------------
    def set_mic_test(self, on: bool) -> bool:
        """Toggle the mic-test meter. Turning it on starts a fresh recording;
        turning it off replays what was captured over the RX path. Returns True
        when a replay was started."""
        was = self.mic_test
        self.mic_test = on
        if on and not was:
            with self._echo_lock:
                self._mic_rec_chunks = []     # fresh take
            self._echo_buf = None             # stop any previous replay
            return False
        if was and not on:
            return self._start_echo_playback()
        return False

    def _start_echo_playback(self) -> bool:
        with self._echo_lock:
            chunks = self._mic_rec_chunks
            self._mic_rec_chunks = []
        if not chunks:
            return False
        buf = np.concatenate(chunks)
        if len(buf) < int(0.2 * SAMPLE_RATE):     # ignore a stray tap
            return False
        self._echo_pos = 0
        self._echo_buf = buf                       # picked up by the callback
        return True

    def echo_busy(self) -> bool:
        return self._echo_buf is not None

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected, "error": self.error,
                "rx_frames": self.rx_frames, "ptt_open": self._ptt_open,
                "rx_db": self.rx_db, "tx_db": self.tx_db,
                "rx_gain": self.rx_gain, "tx_gain": self.tx_gain,
                "tx_auto_gain": self.tx_auto_gain,
                "agc_gain": round(self._agc_gain, 2),   # live AGC factor (display)
                "tx_buffer_ms": self.tx_buffer_ms, "ptt_tail_ms": self.ptt_tail_ms,
                "device": self.device, "peers": self.peers,
                "test_tone": self.test_tone, "tone_1750": self.tone_1750,
                "roger_beep": self.roger_beep, "tx_lowpass": self.tx_lowpass,
                "rx_lowpass": self.rx_lowpass, "mic_test": self.mic_test,
                "echo_busy": self.echo_busy(),
                "digi_rx": self.digi_rx, "digi_tx": self.digi_tx_busy(),
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

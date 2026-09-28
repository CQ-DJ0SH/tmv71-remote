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
import io
import logging
import threading
import time
import wave
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
# Default mic backlog cap. Bounds TX latency against jitter — but it has to be
# larger than the clusters WebRTC delivers the mic in: measured on this link, a
# 40 ms cap starved and overflowed at the same time (2.3 s of silence inserted
# while 2.5 s were discarded), and 150 ms ran clean.
DEF_TX_BUFFER_MS = 150
# RX jitter buffer: three 20 ms blocks. Enough to absorb the drift between the
# sound-card clock and the event loop (which is what made the playback crackle),
# little enough that the added delay is not noticeable in a QSO.
DEF_RX_BUFFER_MS = 60
TAIL_GATE = 330            # -40 dBFS: quieter than this at the end of an over is silence
TAIL_MARGIN_MS = 40        # kept after the last sound (soft final consonants)
DEF_PTT_TAIL_MS = 250     # default post-release transmit tail (drain settle)
TX_LP_CUTOFF = 3500.0     # voice low-pass cutoff (Hz) for the TX mic path
RX_LP_CUTOFF = 3500.0     # voice low-pass cutoff (Hz) for the RX path (de-hiss)
ROGER_BEEP_AMP = 0.28     # roger-beep tone amplitude (fraction of full scale)
# Roger beep: the descending triad d6 - a5 - e5. The browser's squelch-mute
# chime uses the same three notes rising, so the two are recognisably related
# while still telling apart "sent on air" from "local".
ROGER_NOTES = (1174.7, 880.0, 659.3)   # d6 · a5 · e5
ROGER_TONE_S = 0.06       # length of each tone
ROGER_GAP_S = 0.03        # silence between them
# total play-out time of the figure — the un-key path waits exactly this long
ROGER_BEEP_S = len(ROGER_NOTES) * ROGER_TONE_S + (len(ROGER_NOTES) - 1) * ROGER_GAP_S


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


# FM de-emphasis time constant (µs). 75 µs = 6 dB/oct roll-off with a ~2.1 kHz
# corner — restores natural voice tone when the RX audio comes from a flat
# discriminator / 9600-baud data output (which has no de-emphasis).
DEEMPH_TAU_US = 75.0

# RX listen-path high-pass corner (−6 dB). The flat discriminator/9600 output
# passes the CTCSS/PL sub-audible tone (67–254 Hz) plus DC, which de-emphasis
# lifts further — audible as a low hum in the speaker. A ~180 Hz high-pass drops
# it while leaving voice (≳300 Hz) untouched.
RX_HP_CUTOFF_HZ = 180.0

# S-meter from FM quieting. On the flat discriminator output the high-band noise
# is INVERSE to signal strength: loud HF hiss = no signal (S0), full quieting =
# strong signal (S9). We measure a high-pass noise band (above the voice) and
# auto-range it between the two references. Measured on this radio: ~-19 dBFS
# open-squelch → ~-42 dBFS fully quieted (≈23 dB span). It is a relative quieting
# proxy — accurate up to full quieting, then it saturates (cannot tell S9 from
# S9+40 dB); the TM-V71 gives no numeric RSSI over CAT, only binary BUSY.
# An S9+ region was tried and removed: measured here, BUSY sits permanently at 1
# (unusable as a "carrier present" gate), and with no signal the AF feed drops to
# digital silence (broadband RMS ~3-6, high band ~-87 dBFS) — indistinguishable
# from a carrier that quiets the receiver completely. Real received audio never
# comes near that floor (RMS >1000), which is what makes SM_FLOOR_RMS work.
SM_HP_CUTOFF_HZ = 6000.0     # noise band: above the voice, rich in FM noise
SM_OPEN_DBFS = -18.5         # initial S0 reference (no signal / open squelch)
# FM quieting is steep and nonlinear: a readable signal is already well quieted,
# so map S9 to a modest quieting depth below the open-noise reference (a strong
# local signal quiets ~20+ dB and simply saturates at S9). ~14 dB puts a moderate
# but readable signal (≈9 dB quieting) around S5-6, not S1.
SM_S9_SPAN_DB = 14.0
SM_HI_DECAY = 0.005          # open-ref down-drift per block (pre-gain noise is ~fixed)
SM_FLOOR_RMS = 30.0          # below this broadband RMS the input is dead, not S9
# Display smoothing (per ~20 ms block): low = sluggish/calm needle. Asymmetric so
# it still rises promptly on a new signal but settles slowly (no jitter on voice).
SM_ATTACK = 0.20             # weight toward a rising reading
SM_RELEASE = 0.07            # weight toward a falling reading (slower = more inert)
SM_DROP = 0.6                # fast release on carrier loss (noise floor returns)

# Software-squelch hang time: keep audio open this long after BUSY drops, so the
# ~60 ms BUSY poll gaps and short speech pauses don't chop the audio.
SQ_HANG_S = 0.060


class _DeEmphasis:
    """1st-order FM de-emphasis (6 dB/oct) as a truncated-exponential FIR, stateful
    via overlap-save — same vectorised, loop-free block processing as _FIRLowpass."""

    def __init__(self, tau_us: float, fs: float, ntaps: int = 48):
        a = 1.0 - np.exp(-1.0 / (tau_us * 1e-6 * fs))
        h = a * (1.0 - a) ** np.arange(ntaps)
        self.h = (h / h.sum()).astype(np.float64)   # unity DC gain
        self._tail = np.zeros(ntaps - 1, dtype=np.float64)

    def process(self, pcm: np.ndarray) -> np.ndarray:
        x = np.concatenate([self._tail, pcm.astype(np.float64)])
        y = np.convolve(x, self.h, mode="valid")
        self._tail = x[-(self.h.size - 1):]
        return np.clip(y, -32768, 32767).astype(np.int16)

    def reset(self) -> None:
        self._tail[:] = 0.0


class _HighPass:
    """Linear-phase FIR high-pass for the RX listen path — removes the CTCSS/PL
    sub-audible tone (67–254 Hz) and DC/mains hum that the flat 9600/discriminator
    output passes and that de-emphasis further lifts. Windowed-sinc low-pass,
    spectrally inverted; Blackman window for a deep stopband (~−58 dB) so the
    sub-audio is gone while voice (≳300 Hz) is untouched. Stateful via overlap-save,
    same loop-free block processing as the other filters."""

    def __init__(self, fc: float, fs: float, ntaps: int = 2001):
        if ntaps % 2 == 0:
            ntaps += 1                       # odd -> integer group delay, exact centre
        m = (ntaps - 1) / 2.0
        k = np.arange(ntaps) - m
        lp = np.sinc(2.0 * fc / fs * k) * np.blackman(ntaps)
        lp /= lp.sum()
        hp = -lp
        hp[int(m)] += 1.0                    # spectral inversion: δ − lowpass
        self.h = hp.astype(np.float64)
        self._tail = np.zeros(ntaps - 1, dtype=np.float64)

    def process(self, pcm: np.ndarray) -> np.ndarray:
        x = np.concatenate([self._tail, pcm.astype(np.float64)])
        y = np.convolve(x, self.h, mode="valid")
        self._tail = x[-(self.h.size - 1):]
        return np.clip(y, -32768, 32767).astype(np.int16)

    def reset(self) -> None:
        self._tail[:] = 0.0


class _PreEmphasis:
    """1st-order FM pre-emphasis for the TX path — the exact inverse of
    _DeEmphasis, same time constant, so what one lifts the other lowers.

    Only meaningful into a FLAT input (the 9600-baud data port). The radio's own
    mic input and its 1200-baud input apply pre-emphasis themselves; doing it
    here as well would lift the treble twice and sound shrill.

    y[n] = (x[n] − a·x[n−1]) / (1 − a): unity gain at DC, +6 dB/octave above the
    corner (1/2πτ). A one-zero FIR keeps it loop-free. The lift keeps rising to
    Nyquist (+17 dB at 75 µs), so it is always followed by the voice low-pass —
    otherwise hiss above the voice band would go out lifted as well."""

    def __init__(self, tau_us: float, fs: float):
        self.a = float(np.exp(-1.0 / (tau_us * 1e-6 * fs)))
        self._x1 = 0.0

    def process(self, pcm: np.ndarray) -> np.ndarray:
        x = pcm.astype(np.float64)
        prev = np.concatenate(([self._x1], x[:-1]))
        self._x1 = float(x[-1]) if x.size else self._x1
        y = (x - self.a * prev) / (1.0 - self.a)
        return np.clip(y, -32768, 32767).astype(np.int16)

    def reset(self) -> None:
        self._x1 = 0.0


class _Limiter:
    """Peak ceiling for the end of the TX chain — ramped gain, never a hard cut.

    Pre-emphasis lifts the treble, and both the AGC and a loud syllable can push
    the lifted peaks past full scale. Clipping them in int16 is not a quiet
    failure: it turns every sibilant into a burst of broadband crackle, and the
    emphasis stage sits right before the band-limiting low-pass, so that crackle
    lands inside the voice band where it cannot be filtered out again. Gain is
    reduced over 2.5 ms slices and ramped between them, so nothing steps."""

    SLICE = 120

    def __init__(self, fs: float, ceiling_db: float = -1.0,
                 release_ms: float = 120.0):
        self.ceiling = 32768.0 * 10 ** (ceiling_db / 20.0)
        self.rel = 1.0 - np.exp(-(self.SLICE / fs) / (release_ms / 1000.0))
        self.reset()

    def reset(self) -> None:
        self._gain = 1.0
        self.hits = 0                     # slices that needed limiting

    def process(self, pcm: np.ndarray) -> np.ndarray:
        x = pcm.astype(np.float64)
        n = x.size
        if not n:
            return pcm
        k = self.SLICE
        m = max(1, n // k)
        out = np.empty(n, dtype=np.float64)
        pos = 0
        for i in range(m):
            end = n if i == m - 1 else pos + k
            seg = x[pos:end]
            peak = float(np.max(np.abs(seg))) if seg.size else 0.0
            need = self.ceiling / peak if peak > self.ceiling else 1.0
            if need < self._gain:
                g = need                  # attack: immediate, or it clips
                self.hits += 1
            else:                         # release: slow, or it pumps
                g = self._gain + (min(1.0, need) - self._gain) * self.rel
            ramp = np.linspace(self._gain, g, seg.size, endpoint=False)
            ramp = np.minimum(ramp, need) if need < 1.0 else ramp
            out[pos:end] = seg * ramp
            self._gain = g
            pos = end
        return np.clip(out, -32768, 32767).astype(np.int16)


class _Compressor:
    """Feed-forward dynamics compressor with makeup gain and a peak ceiling.

    Raises the AVERAGE modulation: quiet syllables and trailing words come up,
    loud bursts do not overdeviate. That is different from the AGC, which rides
    the overall level slowly (tens of seconds); this acts within a syllable.

    Levels are measured in 2.5 ms slices rather than per 20 ms block — at block
    resolution the attack would let the first 20 ms of a loud word through
    uncompressed, and the gain would step audibly between blocks. The gain is
    ramped linearly across each slice for the same reason. Below the gate
    threshold the makeup gain is withheld, so the pauses between words (room
    noise, fan, breath) are not pumped up to speech level."""

    SLICE = 120                          # 2.5 ms @ 48 kHz

    def __init__(self, fs: float, threshold_db: float = -26.0, ratio: float = 3.0,
                 knee_db: float = 6.0, attack_ms: float = 5.0,
                 release_ms: float = 180.0, makeup_db: float = 9.0,
                 gate_db: float = -52.0, ceiling_db: float = -3.0):
        self.thr, self.ratio, self.knee = threshold_db, ratio, knee_db
        self.makeup, self.gate = makeup_db, gate_db
        self.ceiling = 32768.0 * 10 ** (ceiling_db / 20.0)
        dt = self.SLICE / fs
        self.att = 1.0 - np.exp(-dt / (attack_ms / 1000.0))
        self.rel = 1.0 - np.exp(-dt / (release_ms / 1000.0))
        self.reset()

    def reset(self) -> None:
        self._env_db = -90.0
        self._gain = 1.0                 # last applied linear gain (ramp start)
        self.gain_db = 0.0               # exposed for the status readout

    def _curve(self, lvl_db: float) -> float:
        """Static gain (dB) for an input level: soft-knee downward compression."""
        over = lvl_db - self.thr
        if 2 * over <= -self.knee:
            g = 0.0
        elif 2 * abs(over) <= self.knee:
            g = (1.0 / self.ratio - 1.0) * (over + self.knee / 2) ** 2 / (2 * self.knee)
        else:
            g = (1.0 / self.ratio - 1.0) * over
        return g

    def process(self, pcm: np.ndarray) -> np.ndarray:
        x = pcm.astype(np.float64)
        n = x.size
        if not n:
            return pcm
        k = self.SLICE
        m = max(1, n // k)
        out = np.empty(n, dtype=np.float64)
        pos = 0
        for i in range(m):                        # 8 slices per block: cheap
            end = n if i == m - 1 else pos + k
            seg = x[pos:end]
            rms = float(np.sqrt(np.mean(seg * seg)))
            lvl = 20.0 * np.log10(rms / 32768.0) if rms >= 1.0 else -90.0
            c = self.att if lvl > self._env_db else self.rel
            self._env_db += (lvl - self._env_db) * c
            gdb = self._curve(self._env_db)
            if self._env_db > self.gate:          # makeup only on speech
                gdb += self.makeup
            g = 10 ** (gdb / 20.0)
            peak = float(np.max(np.abs(seg))) if seg.size else 0.0
            start = self._gain
            if peak > 0:
                lim = self.ceiling / peak
                g = min(g, lim)                   # never past the ceiling...
                # ...not even at the START of the ramp. The ramp begins at the
                # previous slice's gain, and after a pause that is the full
                # makeup gain: the first samples of a loud onset went out at
                # -1 dBFS against a -3 dBFS ceiling. Peaks are limited at once.
                start = min(start, lim)
            ramp = np.linspace(start, g, seg.size, endpoint=False)
            out[pos:end] = seg * ramp
            self._gain = g
            pos = end
        self.gain_db = round(20.0 * np.log10(max(self._gain, 1e-6)), 1)
        return np.clip(out, -32768, 32767).astype(np.int16)


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
                 rx_lowpass: bool = False, tx_auto_gain: bool = False,
                 rx_deemph: bool = False, rx_squelch: bool = False,
                 rx_deemph_us: float = DEEMPH_TAU_US,
                 rx_buffer_ms: int = DEF_RX_BUFFER_MS,
                 tx_preemph: bool = True, tx_comp: bool = False):
        self.device = device
        self.rx_gain = rx_gain
        self.tx_gain = tx_gain
        # RX de-emphasis for a flat discriminator / 9600-baud audio source
        self.rx_deemph = rx_deemph
        self.rx_deemph_us = rx_deemph_us
        self._rx_deemph = _DeEmphasis(rx_deemph_us, SAMPLE_RATE)
        # Sub-audio/CTCSS + DC hum removal on the listen path (always on — the flat
        # RX feed always carries it; the decoder taps upstream keep the raw signal).
        self._rx_hp = _HighPass(RX_HP_CUTOFF_HZ, SAMPLE_RATE)
        # FM-quieting S-meter (see SM_* constants): high-band noise auto-ranged
        # between open-squelch noise (S0) and full quieting (S9).
        self._sm_hp = _HighPass(SM_HP_CUTOFF_HZ, SAMPLE_RATE, ntaps=127)
        self._sm_hi = SM_OPEN_DBFS       # adaptive S0 reference (open-squelch noise)
        self.rx_s: float = 0.0           # 0..9 displayed S value
        # Software squelch gated by the radio's BUSY status (for the always-open
        # discriminator/9600 output). rx_busy is updated by a fast CAT poller;
        # audio is muted once BUSY has been closed for longer than the hang time.
        self.rx_squelch = rx_squelch
        self.rx_busy = True                     # fail-open until the poller reports
        self._sq_open_ts = 0.0
        # TX auto-gain (AGC on the mic path): drives the level toward a target,
        # overriding the manual tx_gain while on. _agc_gain is the live factor.
        self.tx_auto_gain = tx_auto_gain
        self._agc_gain = 1.0
        # Optional voice low-pass on the TX (mic) and RX paths.
        self.tx_lowpass = tx_lowpass
        self.rx_lowpass = rx_lowpass
        self._tx_lp = _FIRLowpass(TX_LP_CUTOFF, SAMPLE_RATE)
        # TX modulation stages, both off by default (see _PreEmphasis/_Compressor)
        self.tx_preemph = tx_preemph
        self.tx_comp = tx_comp
        self._tx_pre = _PreEmphasis(rx_deemph_us, SAMPLE_RATE)
        self._tx_comp = _Compressor(SAMPLE_RATE)
        self._tx_lim = _Limiter(SAMPLE_RATE)
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
        # RX jitter buffer, one queue per listening peer (see rx_subscribe)
        self._rx_subs: list = []
        self._rx_lock = threading.Lock()
        self.rx_buffer_ms: int = int(rx_buffer_ms)
        # Mic FIFO for the radio: a deque of int16 BLOCKS, not of single
        # samples. It used to hold one Python int per sample, which meant the
        # audio callback popped 960 of them one by one — a per-sample Python
        # loop in the most timing-critical place in the program, holding the
        # lock the producer needs. That is what the TX jitter was.
        self._playback: deque = deque()
        self._pb_off = 0          # read offset into the head block
        self._pb_len = 0          # samples queued in total
        self._pb_fill = True      # collect a cushion before playing out
        self._digi_keyed = False  # a digital transmission holds the mic line
        self._tx_closing = False  # key released: play out what is queued, take no more
        # Jitter accounting for the TX path, so a report of "it stutters" can be
        # answered with numbers instead of a theory: underruns (the card asked
        # for samples that had not arrived) against overruns (the backlog hit
        # the cap and audio was thrown away). They point at opposite causes.
        self.tx_under = 0         # blocks that ran dry AFTER play-out started
        self.tx_under_ms = 0.0    # silence inserted because of that
        self.tx_fill_ms = 0.0     # silence while the cushion filled (by design)
        self.tx_over_ms = 0.0     # audio discarded at the cap
        self.tx_late = 0          # mic chunks that arrived while dry
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
        self.roger_beep_level = ROGER_BEEP_AMP   # beep amplitude (0..1), settable
        self.mic_test = False         # meter the browser mic without keying the radio
        # mic-test echo: record the mic while MIC TEST is on, then replay it over
        # the RX path once it's switched off (no RF, no keying).
        self._mic_rec_chunks: list = []
        self._echo_lock = threading.Lock()
        self._echo_buf: Optional[np.ndarray] = None   # samples being replayed
        self._echo_pos = 0
        self._mic_rec_cap = 30 * SAMPLE_RATE          # keep at most ~30 s
        # raw RX recorder: capture the un-squelched RX feed (the same signal the
        # ASR sees) into a buffer for WAV download — e.g. to build ASR training data.
        self.rec_on = False
        self._rec_chunks: list = []
        self._rec_lock = threading.Lock()
        self._rec_cap = 60 * 60 * SAMPLE_RATE         # keep at most 60 min
        # digimodes: tap RX for the decoder; inject CW/RTTY audio on TX
        self.digi_rx = False
        self._digi_rx_chunks: list = []
        self.sel_rx = False                          # 5-tone selcall decoder tap
        self._sel_rx_chunks: list = []
        self.asr_rx = False                          # callsign-ASR (Vosk) tap
        self._asr_rx_chunks: list = []
        self._digi_lock = threading.Lock()
        self._digi_tx: Optional[np.ndarray] = None   # queued encoded samples
        self._digi_pos = 0
        self._tone_phase = 0          # two-tone phase (sample counter, wraps)
        self._t1750_phase = 0         # 1750 Hz tone phase
        self._beep_buf = None         # pre-rendered roger beep (int16), or None
        self._beep_pos = 0            # play-out position in _beep_buf

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

    def _update_smeter(self, raw: np.ndarray) -> None:
        """FM-quieting S-meter: high-band noise inversely tracks signal strength,
        auto-ranged between open-squelch noise (S0) and full quieting (S9). Fed the
        raw, pre-gain RX so it reflects the signal, not the AF volume setting."""
        y = self._sm_hp.process(raw).astype(np.float64)     # keep filter continuous
        bb = float(np.sqrt(np.mean(raw.astype(np.float64) ** 2))) if raw.size else 0.0
        if bb < SM_FLOOR_RMS:                    # dead/near-silent input is not "S9"
            self.rx_s = round(self.rx_s * 0.7, 2)
            return
        rms = float(np.sqrt(np.mean(y ** 2)))
        cur = 20.0 * np.log10(max(rms, 1.0) / 32768.0)      # high-band level, dBFS
        # open-squelch noise reference (S0): snap up to any louder noise (that IS
        # the no-signal level), drift down only very slowly. Quieting depth below
        # it maps linearly to S0..S9 over SM_S9_SPAN_DB, saturating at full quieting.
        if cur > self._sm_hi:
            self._sm_hi = cur
        else:
            self._sm_hi = max(self._sm_hi - SM_HI_DECAY, SM_OPEN_DBFS - 12.0)
        inst = 9.0 * (self._sm_hi - cur) / SM_S9_SPAN_DB
        inst = min(9.0, max(0.0, inst))
        # Sluggish needle for normal fluctuations, but snap down on carrier loss:
        # when the high-band noise floor returns, inst collapses to ~S0 from a real
        # reading -> drop fast; small dips (voice) keep the slow, inert release.
        if inst > self.rx_s:
            a = SM_ATTACK
        elif inst < 0.7 and self.rx_s > 2.0:      # carrier lost -> back to noise
            a = SM_DROP
        else:
            a = SM_RELEASE
        self.rx_s = round(a * inst + (1.0 - a) * self.rx_s, 2)

    # -- PortAudio callback (runs in PortAudio thread) ---------------------
    def _callback(self, indata, outdata, frames, time_info, status):
        if status:
            log.debug("audio status: %s", status)
        # RX: radio capture (mono ch0) -> WebRTC subscribers
        samples = indata[:, 0].copy()
        self._update_smeter(samples)            # FM-quieting S-meter (pre-gain, raw)
        if self.rx_gain != 1.0:
            samples = np.clip(samples.astype(np.float32) * self.rx_gain,
                              -32768, 32767).astype(np.int16)
        # ASR taps the full-band signal (pre listening low-pass); it applies its
        # own de-emphasis + high-pass, so the listening filters must not affect it.
        asr_src = samples
        # raw RX recorder (un-squelched, pre listening low-pass) for WAV download
        if self.rec_on:
            with self._rec_lock:
                self._rec_chunks.append(asr_src.copy())
                total = sum(len(c) for c in self._rec_chunks)
                while total > self._rec_cap and len(self._rec_chunks) > 1:
                    total -= len(self._rec_chunks.pop(0))
        if self.rx_lowpass:
            samples = self._rx_lp.process(samples)
        self.rx_frames += 1
        # digimodes decoder tap: stash RX blocks for the decode loop to consume
        if self.digi_rx or self.sel_rx or self.asr_rx:
            with self._digi_lock:
                if self.digi_rx:
                    self._digi_rx_chunks.append(samples.copy())
                    if len(self._digi_rx_chunks) > 250:  # ~5 s cap, drop oldest
                        self._digi_rx_chunks.pop(0)
                if self.sel_rx:
                    self._sel_rx_chunks.append(samples.copy())
                    if len(self._sel_rx_chunks) > 250:
                        self._sel_rx_chunks.pop(0)
                if self.asr_rx and not self.mic_test:   # mic test feeds ASR from the mic
                    self._asr_rx_chunks.append(asr_src.copy())
                    if len(self._asr_rx_chunks) > 250:
                        self._asr_rx_chunks.pop(0)
        # mic-test echo replay: while a recording is being played back, override
        # the published RX block with it (single pos writer = this callback).
        pub = samples
        live = True
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
            live = False
        elif self.mic_test:
            # mute the radio RX while a mic test is recording, so only the test
            # (and its replay on switch-off) is heard — not live radio audio.
            pub = np.zeros(frames, dtype=np.int16)
            live = False
        # live listen path only (after the decoder taps, which want the flat,
        # un-squelched signal); off for the echo replay / mic-test mute.
        if live:
            pub = self._rx_hp.process(pub)      # drop CTCSS/sub-audio + DC hum
            if self.rx_deemph:
                pub = self._rx_deemph.process(pub)
            if self.rx_squelch:                 # gate on the radio's BUSY status
                now = time.monotonic()
                if self.rx_busy:
                    self._sq_open_ts = now
                if now - self._sq_open_ts > SQ_HANG_S:   # closed long enough -> mute
                    pub = np.zeros(frames, dtype=np.int16)
        # RX meter (S-meter + level graph) reads rx_db — measure it from the final
        # published block, so a squelch/mic-test mute reads as silence. Decoders
        # still get the raw signal via the taps above.
        self.rx_db = _level(pub, self.rx_db)
        # publish the latest block; the clock-paced RX track(s) pick it up.
        blk = pub.tobytes()
        self._latest = blk
        self._latest_ts = time.monotonic()
        # Hand the block to every listening peer's own queue. A shared queue
        # would let two browsers steal blocks from each other; a shared "latest
        # block" (what this used to be) makes both of them resample the capture
        # on their own clock — which is where the crackle came from.
        if self._rx_subs:
            cap = self.rx_depth() * 3
            with self._rx_lock:
                for q in self._rx_subs:
                    q.append(blk)
                    while len(q) > cap:      # peer fell behind -> drop the oldest
                        q.popleft()
        # TX: radio mic source. The two-tone test is emitted continuously on the
        # mic line regardless of PTT (so deviation can be set without holding the
        # key); the roger beep and queued browser mic only play while keyed.
        mono = np.zeros(frames, dtype=np.int16)
        if self.test_tone:
            mono = self._gen_tone(frames, (700.0, 1900.0), 0.50, "_tone_phase")
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
            elif self._beep_buf is not None:
                # the whole triad is rendered once on trigger; here it is only
                # copied out block by block (see _render_roger_beep)
                b = self._beep_buf
                k = min(frames, len(b) - self._beep_pos)
                mono[:k] = b[self._beep_pos:self._beep_pos + k]
                self._beep_pos += k
                if self._beep_pos >= len(b):
                    self._beep_buf = None
            else:
                mono = self._pb_pull(frames)
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
        self._pb_clear()                   # drop trailing mic; beep only
        self._beep_buf = self._render_roger_beep(self.roger_beep_level)
        self._beep_pos = 0

    @staticmethod
    def _render_roger_beep(amp: float) -> np.ndarray:
        """Render ROGER_NOTES as tone/silence/tone/... into one int16 buffer.

        Rendering the whole figure up front keeps the audio callback to a plain
        memcpy and makes the number of notes a data question, not a branching
        one. Each tone starts and ends at zero phase and gets a short raised-
        cosine fade so the edges do not click."""
        seg = int(SAMPLE_RATE * ROGER_TONE_S)
        gap = int(SAMPLE_RATE * ROGER_GAP_S)
        fade = max(1, int(SAMPLE_RATE * 0.004))          # 4 ms in/out
        env = np.ones(seg)
        ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade) / fade))
        env[:fade] = ramp
        env[-fade:] = ramp[::-1]
        t = np.arange(seg) / SAMPLE_RATE
        parts = []
        for i, f in enumerate(ROGER_NOTES):
            if i:
                parts.append(np.zeros(gap))
            parts.append(np.sin(2.0 * np.pi * f * t) * env)
        sig = np.concatenate(parts) * amp
        return np.clip(sig * 32767, -32768, 32767).astype(np.int16)

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

    def pop_asr_rx(self) -> Optional[np.ndarray]:
        with self._digi_lock:
            if not self._asr_rx_chunks:
                return None
            chunks = self._asr_rx_chunks
            self._asr_rx_chunks = []
        return np.concatenate(chunks)

    def set_asr_rx(self, on: bool) -> None:
        self.asr_rx = on
        if not on:
            with self._digi_lock:
                self._asr_rx_chunks = []

    def play_digi(self, pcm: np.ndarray) -> None:
        """Queue encoded CW/RTTY audio for the radio mic (played while keyed).

        A digital transmission owns the mic line for as long as the radio stays
        keyed — not just while its own samples last. The browser mic streams
        over WebRTC all the time; it used to queue behind the CW, and when the
        CW ended the callback fell through to that queue and put about half a
        second of room audio and speech on the air before the key dropped."""
        self._digi_keyed = True
        self._pb_clear()                     # nothing of the mic may follow
        self._digi_pos = 0
        self._digi_tx = pcm.astype(np.int16)

    @property
    def digi_keyed(self) -> bool:
        """True from play_digi() until the radio is unkeyed."""
        return self._digi_keyed

    def digi_tx_busy(self) -> bool:
        return self._digi_tx is not None

    def stop_digi_tx(self) -> None:
        self._digi_tx = None

    def rx_depth(self) -> int:
        """Target queue depth in blocks (20 ms each)."""
        return max(1, int(round(self.rx_buffer_ms / 20.0)))

    def rx_subscribe(self) -> deque:
        q: deque = deque()
        with self._rx_lock:
            self._rx_subs.append(q)
        return q

    def rx_unsubscribe(self, q) -> None:
        with self._rx_lock:
            if q in self._rx_subs:
                self._rx_subs.remove(q)

    def set_rx_buffer(self, ms: Optional[int]) -> None:
        if ms is None:
            return
        self.rx_buffer_ms = max(20, min(300, int(ms)))
        with self._rx_lock:                 # start over at the new depth
            for q in self._rx_subs:
                q.clear()

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
        if self._digi_keyed:                 # CW/RTTY/POCSAG/APRS on the air
            return
        if self._tx_closing:                 # released: the over is complete
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
        # Order matters: compress first (level), then emphasise (tilt), then
        # band-limit. Emphasis before the compressor would make the treble drive
        # the gain down; the low-pass last removes the emphasis lift above the
        # voice band — which is why it runs whenever emphasis is on.
        if self.tx_comp:
            pcm = self._tx_comp.process(pcm)
        if self.tx_preemph:
            pcm = self._tx_pre.process(pcm)
        if self.tx_lowpass or self.tx_preemph:
            pcm = self._tx_lp.process(pcm)
        if self.tx_preemph or self.tx_comp:
            pcm = self._tx_lim.process(pcm)     # last in line: nothing clips
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
                # also evaluate the mic audio for callsigns (ASR) during the test
                if self.asr_rx:
                    with self._digi_lock:
                        self._asr_rx_chunks.append(pcm.copy())
                        if len(self._asr_rx_chunks) > 250:
                            self._asr_rx_chunks.pop(0)
            return
        with self._pb_lock:
            if self._pb_len == 0 and not self._pb_fill:
                self.tx_late += 1          # arrived after the queue had emptied
            self._playback.append(pcm.copy())
            self._pb_len += pcm.size
            # keep the backlog tight so TX stays low-latency (bound jitter, not 1 s)
            cap = max(BLOCK, self.tx_buffer_ms * SAMPLE_RATE // 1000)
            while self._pb_len > cap and self._playback:
                head = self._playback[0]
                avail = head.size - self._pb_off
                drop = min(avail, self._pb_len - cap)
                self._pb_len -= drop
                self.tx_over_ms += drop * 1000.0 / SAMPLE_RATE
                if drop >= avail:
                    self._playback.popleft()
                    self._pb_off = 0
                else:
                    self._pb_off += drop

    def _pb_pull(self, frames: int) -> np.ndarray:
        """`frames` samples of queued mic audio, zero-padded if it runs out.

        Slice copies only: a handful of numpy moves per block instead of one
        Python iteration per sample, and the lock is held for microseconds."""
        out = np.zeros(frames, dtype=np.int16)
        got = 0
        with self._pb_lock:
            # Wait for a cushion before starting to play out, and rebuild it
            # after a dry spell. The mic arrives over WebRTC in bursts, while the
            # sound card asks for exactly 960 samples every 20 ms: without a
            # cushion the queue is empty on one call and has 40 ms on the next,
            # so the callback pads with silence and the backlog later hits the
            # cap and is dropped — a gap in, a gap out. This is the same cushion
            # the RX track keeps, on the other side of the link.
            # (after release nothing more is coming: play out what is left
            # instead of waiting for a cushion that will never fill)
            if self._pb_fill and not self._tx_closing:
                if self._pb_len < self._pb_target():
                    # Filling the cushion is not a dropout — it is the lead-in
                    # at the start of an over, and counting it as a fault made
                    # a healthy transmission look broken.
                    if self._ptt_open:
                        self.tx_fill_ms += frames * 1000.0 / SAMPLE_RATE
                    return out
                self._pb_fill = False
            while got < frames and self._playback:
                head = self._playback[0]
                k = min(frames - got, head.size - self._pb_off)
                out[got:got + k] = head[self._pb_off:self._pb_off + k]
                got += k
                self._pb_off += k
                if self._pb_off >= head.size:
                    self._playback.popleft()
                    self._pb_off = 0
            self._pb_len -= got
            if got < frames and self._ptt_open:
                self.tx_under += 1
                self.tx_under_ms += (frames - got) * 1000.0 / SAMPLE_RATE
            if self._pb_len <= 0:          # ran dry -> rebuild the cushion
                self._pb_fill = True
        return out

    def _pb_target(self) -> int:
        """Cushion in samples: half the configured backlog, 40–80 ms.

        It has to be a real cushion. Measured on this link the mic arrives in
        clusters, not in even 20 ms steps: with a 40 ms cap, 2.3 s of silence
        were inserted for want of samples while 2.5 s were thrown away at the
        cap — starving and overflowing at the same time, which is the signature
        of a backlog smaller than the burst that feeds it."""
        ms = min(80, max(40, self.tx_buffer_ms // 2))
        return ms * SAMPLE_RATE // 1000

    def _trim_tail(self) -> None:
        """Drop the silence at the end of the queued mic audio.

        Most overs end with a breath of silence before the button is let go;
        with the queue closed that silence is all that holds the key open.
        Everything up to the last sound above the gate stays, plus a short
        margin so a soft final consonant is not clipped."""
        with self._pb_lock:
            if not self._playback:
                return
            buf = np.concatenate([self._playback[0][self._pb_off:],
                                  *list(self._playback)[1:]])
            loud = np.flatnonzero(np.abs(buf.astype(np.int32)) > TAIL_GATE)
            keep = 0 if loud.size == 0 else \
                min(buf.size, int(loud[-1]) + 1 + TAIL_MARGIN_MS * SAMPLE_RATE // 1000)
            self._playback.clear()
            self._pb_off = 0
            self._pb_len = keep
            if keep:
                self._playback.append(buf[:keep])

    def _pb_clear(self) -> None:
        with self._pb_lock:
            self._playback.clear()
            self._pb_off = 0
            self._pb_len = 0
            self._pb_fill = True

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
        # From here the over is complete. The browser mic streams continuously
        # and is not gated by the PTT button, so if we kept accepting it the
        # queue would never run empty and every over would be held open for
        # the full max_wait -- half a second of room noise on the air.
        self._tx_closing = True
        self._trim_tail()
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
        self._tx_closing = False
        if not is_open:
            self._pb_clear()
            self._digi_keyed = False
            self.tx_db = None
        else:
            self._tx_lp.reset()      # clear filter state at the start of each over
            self._tx_pre.reset()
            self._tx_comp.reset()    # no gain carried over from the last over
            self._tx_lim.reset()
            # counters are per over: totals over hours hide which transmission
            # went wrong, and one over is exactly the unit being judged
            self.tx_under = self.tx_late = 0
            self.tx_under_ms = self.tx_over_ms = self.tx_fill_ms = 0.0
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

    def set_deemph_us(self, us: float) -> None:
        """Change the de-emphasis time constant (rebuilds the filter)."""
        us = max(10.0, min(500.0, float(us)))
        self.rx_deemph_us = us
        self._rx_deemph = _DeEmphasis(us, SAMPLE_RATE)
        # TX pre-emphasis mirrors it: one time constant for both directions
        self._tx_pre = _PreEmphasis(us, SAMPLE_RATE)

    # -- raw RX recorder ---------------------------------------------------
    def set_record(self, on: bool) -> None:
        if on and not self.rec_on:
            with self._rec_lock:
                self._rec_chunks = []          # fresh take
        self.rec_on = on

    def rec_clear(self) -> None:
        with self._rec_lock:
            self._rec_chunks = []

    def rec_samples(self) -> int:
        with self._rec_lock:
            return sum(len(c) for c in self._rec_chunks)

    def rec_pcm(self) -> np.ndarray:
        """The recorded RX buffer as raw int16 samples (native 48 kHz)."""
        with self._rec_lock:
            if not self._rec_chunks:
                return np.zeros(0, dtype=np.int16)
            return np.concatenate(self._rec_chunks)

    def rec_wav_bytes(self) -> bytes:
        """The recorded RX buffer as a 16-bit mono WAV (native 48 kHz)."""
        with self._rec_lock:
            data = (np.concatenate(self._rec_chunks) if self._rec_chunks
                    else np.zeros(0, dtype=np.int16))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)                   # 48 kHz native
            w.writeframes(data.astype(np.int16).tobytes())
        return buf.getvalue()

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected, "error": self.error,
                "rx_frames": self.rx_frames, "ptt_open": self._ptt_open,
                "rx_db": self.rx_db, "tx_db": self.tx_db, "rx_s": self.rx_s,
                "rx_gain": self.rx_gain, "tx_gain": self.tx_gain,
                "tx_auto_gain": self.tx_auto_gain,
                "agc_gain": round(self._agc_gain, 2),   # live AGC factor (display)
                "tx_buffer_ms": self.tx_buffer_ms, "ptt_tail_ms": self.ptt_tail_ms,
                "rx_buffer_ms": self.rx_buffer_ms,
                "device": self.device, "peers": self.peers,
                "test_tone": self.test_tone, "tone_1750": self.tone_1750,
                "roger_beep": self.roger_beep,
                "roger_beep_level": round(self.roger_beep_level, 2),
                "tx_lowpass": self.tx_lowpass,
                "tx_preemph": self.tx_preemph, "tx_comp": self.tx_comp,
                "tx_under": self.tx_under, "tx_late": self.tx_late,
                "tx_lim_hits": self._tx_lim.hits,
                "tx_under_ms": round(self.tx_under_ms, 1),
                "tx_fill_ms": round(self.tx_fill_ms, 1),
                "tx_over_ms": round(self.tx_over_ms, 1),
                "tx_queue_ms": round(self._pb_len * 1000.0 / SAMPLE_RATE, 1),
                "comp_db": self._tx_comp.gain_db,     # live compressor gain
                "rx_lowpass": self.rx_lowpass, "rx_deemph": self.rx_deemph,
                "rx_deemph_us": self.rx_deemph_us,
                "rx_squelch": self.rx_squelch, "mic_test": self.mic_test,
                "echo_busy": self.echo_busy(),
                "recording": self.rec_on,
                "rec_seconds": round(self.rec_samples() / SAMPLE_RATE, 1),
                "rec_bytes": self.rec_samples() * 2,     # buffer RAM (16-bit mono)
                "digi_rx": self.digi_rx, "digi_tx": self.digi_tx_busy(),
                "transport": "webrtc", "web_client": self.peers > 0}


class RadioRxTrack(MediaStreamTrack):
    """Outgoing track: radio RX audio -> browser.

    Clock-paced: emits one 20 ms frame every 20 ms of wall time, independent of
    the PortAudio callback. That keeps the WebRTC media timeline locked to real
    time, so a capture glitch becomes a short gap of silence instead of a frozen
    stream the browser's jitter buffer can never recover from.

    Between the two clocks sits a small queue. Reading the "most recent block"
    instead — which is what this did — means two independent clocks sampling
    each other: whenever the event loop was a few ms late or early against the
    sound card, the same 20 ms went out twice or one was skipped, and each of
    those seams is an audible click. Under load (the Vosk decode, an x-vector)
    that happens often enough to sound like crackle. The queue absorbs that
    jitter; only a real drift between the two clocks still costs a block, and
    then rarely and boundedly: the queue is capped, so latency cannot grow.
    """
    kind = "audio"

    def __init__(self, radio: RadioAudio):
        super().__init__()
        self._radio = radio
        self._pts = 0
        self._start: Optional[float] = None
        self._silence = np.zeros((1, BLOCK), dtype=np.int16)
        self._q = radio.rx_subscribe()      # this peer's own block queue
        self._fill = True                   # refill before playing out

    async def recv(self) -> AudioFrame:
        loop = asyncio.get_event_loop()
        if self._start is None:
            self._start = loop.time()
        else:
            # pace to wall clock: frame N is due at start + N*20ms
            target = self._start + (self._pts + BLOCK) / SAMPLE_RATE
            await asyncio.sleep(max(0.0, target - loop.time()))
        data = self._next_block()
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

    def _next_block(self) -> Optional[bytes]:
        """One block from the queue, or None (silence) while it is filling.

        Running dry means the capture stalled or the loop overran; rather than
        limping along one block deep — where every further hiccup is another
        click — the buffer is refilled to its target first. A short silence at
        the start of an over is cheaper than continuous crackle.
        """
        q = self._q
        if self._fill:
            if len(q) < self._radio.rx_depth():
                return None
            self._fill = False
        if not q:
            self._fill = True
            return None
        return q.popleft()

    def stop(self) -> None:
        self._radio.rx_unsubscribe(self._q)
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

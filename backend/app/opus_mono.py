"""Encode the radio's RX audio as mono Opus, straight against libopus.

Why this file exists, measured on this Pi rather than guessed:

One listening browser cost about 0.46 MB/min of resident memory that was never
handed back. The Python heap gained nothing (two objects in five minutes), three
allocators (glibc default, glibc tuned, jemalloc) grew identically, and
rebuilding the codec context every minute reclaimed none of it — so not
fragmentation, and nothing reachable. Bisecting the send path:

    AudioFrame.from_ndarray alone        +0.19 MB, then flat
    + AudioResampler.resample           +0.22 MB/min
    + libopus encode (via PyAV)         +0.45 MB/min
    + SRTP protect                      +0.45 MB/min   (unchanged)

Then the same loop with the codec swapped:

    libopus via PyAV                    +0.23 MB/min
    pcm_s16le via PyAV                  +0.00 MB/min   (flat to the byte)
    libopus direct, via ctypes          +0.03 MB/min

Same frames, same PyAV machinery, same packet objects — so the leak is in
libavcodec's libopus wrapper (PyAV 16.1.0, bundled libavcodec 62.11.100), and
libopus itself is clean. This module therefore does two things:

1. Drops aiortc's resampler. Its ``OpusEncoder`` hard-codes ``layout="stereo"``
   and resamples our mono there, although the offer already pins Opus to one
   channel (``stereo=0;sprop-stereo=0``, see ``_force_opus_mono``). Our RX track
   emits exactly what the encoder wants: s16, 48 kHz, mono, 960 samples.
2. Calls ``opus_encode`` directly, bypassing the leaking wrapper.

Together that is 0.46 -> about 0.03 MB/min, i.e. ~2 MB/h per listener instead of
45. If libopus cannot be loaded, the PyAV path stays in place (mono, still half).

Two things that do NOT work, for the record: mono instead of stereo changes
nothing on its own, and rebuilding the codec context reclaims nothing while
costing the listener a 105 ms burst at full scale (the same reset on the decoder
side would put that click on the air, since that is the TX path).
"""
from __future__ import annotations

import ctypes
import logging
from typing import Optional

log = logging.getLogger("tmv71")

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960          # 20 ms, what aiortc packetises
CHANNELS = 1
BITRATE = 96000                  # what aiortc asks libavcodec for

_APPLICATION_VOIP = 2048
_SET_BITRATE_REQUEST = 4002
_MAX_PAYLOAD = 4000              # an Opus frame cannot exceed ~1275 B at 48k mono

_installed = False


class _Libopus:
    """One libopus encoder state, with the handle freed on collection."""

    _lib = None

    @classmethod
    def _load(cls):
        if cls._lib is None:
            lib = ctypes.CDLL("libopus.so.0")
            lib.opus_encoder_create.restype = ctypes.c_void_p
            lib.opus_encoder_create.argtypes = [
                ctypes.c_int32, ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_int)]
            lib.opus_encode.restype = ctypes.c_int32
            lib.opus_encode.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
            lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
            lib.opus_encoder_ctl.restype = ctypes.c_int
            lib.opus_strerror.restype = ctypes.c_char_p
            cls._lib = lib
        return cls._lib

    def __init__(self) -> None:
        lib = self._load()
        err = ctypes.c_int()
        handle = lib.opus_encoder_create(SAMPLE_RATE, CHANNELS,
                                         _APPLICATION_VOIP, ctypes.byref(err))
        if not handle or err.value != 0:
            raise RuntimeError(f"opus_encoder_create failed ({err.value})")
        self._handle = ctypes.c_void_p(handle)
        lib.opus_encoder_ctl(self._handle, ctypes.c_int(_SET_BITRATE_REQUEST),
                             ctypes.c_int32(BITRATE))
        self._buf = (ctypes.c_ubyte * _MAX_PAYLOAD)()

    def encode(self, pcm: bytes) -> bytes:
        """One 20 ms mono s16 block in, one Opus payload out."""
        lib = self._lib
        src = ctypes.cast(ctypes.c_char_p(pcm), ctypes.POINTER(ctypes.c_int16))
        n = lib.opus_encode(self._handle, src, SAMPLES_PER_FRAME,
                            self._buf, _MAX_PAYLOAD)
        if n < 0:
            raise RuntimeError(f"opus_encode: {lib.opus_strerror(n).decode()}")
        return bytes(self._buf[:n])

    def __del__(self) -> None:
        handle, lib = getattr(self, "_handle", None), type(self)._lib
        if handle and lib is not None:
            self._handle = None
            try:
                lib.opus_encoder_destroy(handle)
            except Exception:  # noqa: BLE001
                pass


def install() -> None:
    """Patch aiortc's Opus encoder. Safe to call more than once."""
    global _installed
    if _installed:
        return
    try:
        from av import AudioResampler
        from aiortc.codecs import opus as aiortc_opus
    except Exception as exc:  # noqa: BLE001
        log.warning("mono Opus encoder not installed: %s", exc)
        return

    try:
        _Libopus._load()
        direct = True
    except Exception as exc:  # noqa: BLE001
        # No libopus on this system: stay on PyAV, but still mono (half the
        # growth, since the resampler goes away either way).
        log.warning("libopus not loadable (%s) — staying on the PyAV encoder", exc)
        direct = False

    orig_init = aiortc_opus.OpusEncoder.__init__

    def __init__(self) -> None:
        orig_init(self)
        self.codec.layout = "mono"           # used only by the fallback path
        self._mono = _Libopus() if direct else None
        self._first_pts: Optional[int] = None
        self._next_ts = 0            # fallback for frames that carry no pts
        # kept for anything that is not already a 960-sample mono s16 frame
        self.resampler = AudioResampler(
            format="s16", layout="mono", rate=SAMPLE_RATE,
            frame_size=SAMPLES_PER_FRAME)

    def encode(self, frame, force_keyframe: bool = False):
        fits = (frame.format.name == "s16"
                and frame.layout.name == "mono"
                and frame.samples == SAMPLES_PER_FRAME)
        blocks = [frame] if fits else list(self.resampler.resample(frame))
        if not blocks:
            return [], None                   # resampler is still buffering
        pts = blocks[0].pts
        if pts is None:                      # no timing from upstream: count
            pts = self._next_ts
        if self._first_pts is None:
            self._first_pts = pts
        timestamp = pts - self._first_pts
        self._next_ts = pts + sum(b.samples for b in blocks)
        payloads = []
        for b in blocks:
            if self._mono is not None:
                # the plane buffer may be padded; libopus reads 960 samples
                payloads.append(self._mono.encode(
                    bytes(b.planes[0])[:SAMPLES_PER_FRAME * 2]))
            else:
                payloads += [bytes(p) for p in self.codec.encode(b)]
        return payloads, timestamp

    aiortc_opus.OpusEncoder.__init__ = __init__
    aiortc_opus.OpusEncoder.encode = encode
    _installed = True
    log.info("Opus encoder: mono, no resampler, %s",
             "libopus direct" if direct else "PyAV codec")

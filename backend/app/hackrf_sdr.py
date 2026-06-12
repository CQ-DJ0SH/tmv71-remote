"""HackRF live spectrum / waterfall source.

Drives a connected HackRF One to feed the browser waterfall. Two mutually
exclusive modes (the HackRF is a single-tenant device):

  * ``pan``   — panadapter: continuous IQ from ``hackrf_transfer``, FFT'd to a
                span centred on a frequency (optionally following the radio).
  * ``sweep`` — wideband power sweep from ``hackrf_sweep`` across a range.

A background thread produces one power-spectrum row at a time (dBFS) and hands
it to the asyncio loop, which fans it out to subscribed WebSocket clients.
Nothing here touches the radio or its serial port.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("hackrf")

# --- tunables ---------------------------------------------------------------
PAN_SAMPLE_RATE = 2_000_000      # HackRF minimum; panadapter span = sample rate
PAN_FFT = 2048                   # FFT size per frame (finer frequency resolution)
OUT_BINS = 512                   # spectrum bins sent to the browser (downsampled)
TARGET_FPS = 25                  # waterfall rows per second (panadapter)
DC_NOTCH_BINS = 2                # hide the HackRF centre DC spike

# gain limits (HackRF hardware steps)
LNA_MAX, LNA_STEP = 40, 8        # IF/LNA gain 0..40 step 8
VGA_MAX, VGA_STEP = 62, 2        # baseband VGA gain 0..62 step 2

FOLLOW_RETUNE_HZ = PAN_SAMPLE_RATE // 4   # re-centre once the radio drifts this far


def _clamp_gain(v: int, step: int, hi: int) -> int:
    return max(0, min(hi, (int(v) // step) * step))


class HackRFSpectrum:
    """Manage the HackRF subprocess and broadcast spectrum frames."""

    def __init__(self, freq_getter: Optional[Callable[[], Optional[int]]] = None):
        # freq_getter returns the radio's current control-band RX freq (Hz) or None
        self._freq_getter = freq_getter
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._procs: set = set()           # every spawned capture, so none leaks
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subs: set[asyncio.Queue] = set()

        # config / state
        self.available = shutil.which("hackrf_transfer") is not None
        self.running = False
        self.mode = "pan"                  # 'pan' | 'sweep'
        self.follow = True                 # panadapter centre tracks the radio
        self.center = 145_000_000          # panadapter centre (Hz)
        self.sweep_start = 144_000_000     # sweep range (Hz)
        self.sweep_stop = 146_000_000
        self.lna = 24
        self.vga = 20
        self.amp = False
        self.error: Optional[str] = None
        self._fps = 0.0

    # -- subscriptions (WebSocket clients) --------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _publish(self, frame: dict) -> None:
        # runs on the event loop thread
        for q in list(self._subs):
            if q.full():
                try: q.get_nowait()        # drop oldest, keep the waterfall live
                except asyncio.QueueEmpty: pass
            try: q.put_nowait(frame)
            except asyncio.QueueFull: pass

    def _emit(self, frame: dict) -> None:
        loop = self._loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._publish, frame)

    # -- status -----------------------------------------------------------
    def status(self) -> dict:
        return {
            "available": self.available, "running": self.running, "mode": self.mode,
            "follow": self.follow, "center": self.center, "span": PAN_SAMPLE_RATE,
            "sweep_start": self.sweep_start, "sweep_stop": self.sweep_stop,
            "lna": self.lna, "vga": self.vga, "amp": self.amp,
            "bins": OUT_BINS, "fps": round(self._fps, 1), "error": self.error,
        }

    # -- lifecycle --------------------------------------------------------
    async def start(self, **cfg) -> dict:
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._start_sync, loop, cfg)
        return self.status()

    async def stop(self) -> dict:
        await asyncio.to_thread(self._stop_sync)
        return self.status()

    async def configure(self, **cfg) -> dict:
        """Apply config; restart the capture if running and a hw param changed."""
        restart = self._apply_cfg(cfg)
        if self.running and restart:
            loop = asyncio.get_running_loop()
            await asyncio.to_thread(self._start_sync, loop, {})
        return self.status()

    def _apply_cfg(self, cfg: dict) -> bool:
        """Update fields from cfg. Returns True if the capture must restart."""
        restart = False
        if "mode" in cfg and cfg["mode"] in ("pan", "sweep") and cfg["mode"] != self.mode:
            self.mode = cfg["mode"]; restart = True
        if "follow" in cfg:
            self.follow = bool(cfg["follow"])
        if cfg.get("center"):
            c = int(cfg["center"])
            if c != self.center: self.center = c; restart = True
        if cfg.get("sweep_start"):
            self.sweep_start = int(cfg["sweep_start"]); restart = True
        if cfg.get("sweep_stop"):
            self.sweep_stop = int(cfg["sweep_stop"]); restart = True
        if "lna" in cfg:
            v = _clamp_gain(cfg["lna"], LNA_STEP, LNA_MAX)
            if v != self.lna: self.lna = v; restart = True
        if "vga" in cfg:
            v = _clamp_gain(cfg["vga"], VGA_STEP, VGA_MAX)
            if v != self.vga: self.vga = v; restart = True
        if "amp" in cfg:
            a = bool(cfg["amp"])
            if a != self.amp: self.amp = a; restart = True
        return restart

    def _start_sync(self, loop: asyncio.AbstractEventLoop, cfg: dict) -> None:
        if not self.available:
            self.error = "hackrf_transfer not found"; return
        self._apply_cfg(cfg)
        self._stop_sync()                  # ensure any previous capture is gone
        self._kill_strays()                # …and clear any leftover holding the device
        self._loop = loop
        self.error = None
        self._stop.clear()
        # follow: snap the centre to the radio before we spawn
        if self.mode == "pan" and self.follow:
            f = self._radio_freq()
            if f: self.center = f
        target = self._run_pan if self.mode == "pan" else self._run_sweep
        self._thread = threading.Thread(target=target, name="hackrf", daemon=True)
        self.running = True
        self._thread.start()
        log.info("hackrf %s started (center=%d lna=%d vga=%d amp=%d)",
                 self.mode, self.center, self.lna, self.vga, self.amp)

    def _kill(self, p) -> None:
        """Reliably stop a hackrf subprocess and reap it (so it can't keep the
        USB device claimed). Escalates SIGTERM → SIGKILL; always wait()s."""
        if not p:
            return
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    p.kill()
            p.wait(timeout=1.5)            # reap (avoids a defunct that holds the device)
        except Exception:  # noqa: BLE001
            pass
        self._procs.discard(p)

    @staticmethod
    def _kill_strays() -> None:
        """The HackRF is single-tenant (only this app uses it). Before a fresh
        start, hard-kill any leftover capture a previous run lost track of, so
        the device is guaranteed openable."""
        for name in ("hackrf_transfer", "hackrf_sweep"):
            try:
                subprocess.run(["pkill", "-9", "-x", name], timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.2)                   # let the kernel release the USB device

    def _stop_sync(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=4)            # the thread's finally kills + reaps its proc
        for p in list(self._procs):      # kill everything ever spawned (no lost handle)
            self._kill(p)
        self._kill(self._proc)
        self._proc = None
        self._thread = None
        self.running = False
        self._fps = 0.0

    def _radio_freq(self) -> Optional[int]:
        try:
            return self._freq_getter() if self._freq_getter else None
        except Exception:
            return None

    # -- panadapter (continuous IQ → FFT) ---------------------------------
    def _spawn_transfer(self) -> subprocess.Popen:
        cmd = ["hackrf_transfer", "-r", "-", "-f", str(self.center),
               "-s", str(PAN_SAMPLE_RATE), "-a", "1" if self.amp else "0",
               "-l", str(self.lna), "-g", str(self.vga)]
        p = None
        for _ in range(4):                      # retry transient "Resource busy"
            if self._stop.is_set():
                break
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, bufsize=0)
            self._procs.add(p)
            time.sleep(0.15)
            if p.poll() is None:
                return p                        # opened the device, streaming
            self._kill(p)                       # died at once (busy) → wait and retry
            time.sleep(0.35)
        return p

    def _run_pan(self) -> None:
        win = np.hanning(PAN_FFT).astype(np.float32)
        need = PAN_FFT * 2                      # bytes for one FFT block (int8 IQ)
        buf = bytearray()
        next_frame = time.monotonic()
        frame_dt = 1.0 / TARGET_FPS
        fps_t, fps_n = time.monotonic(), 0
        try:
            self._proc = self._spawn_transfer()
            stdout = self._proc.stdout if self._proc else None
            while not self._stop.is_set() and stdout is not None:
                chunk = stdout.read(65536)      # drain the pipe to avoid backpressure
                if not chunk:
                    if not self._proc or self._proc.poll() is not None:
                        if not self._stop.is_set():
                            self.error = "hackrf_transfer exited"
                        break
                    continue
                buf += chunk
                if len(buf) > need * 4:         # keep only the most recent samples
                    del buf[:-need * 2]
                now = time.monotonic()
                if now < next_frame or len(buf) < need:
                    continue
                next_frame = now + frame_dt
                block = np.frombuffer(bytes(buf[-need:]), dtype=np.int8).astype(np.float32)
                iq = (block[0::2] + 1j * block[1::2]) / 128.0
                spec = np.fft.fftshift(np.fft.fft(iq * win))
                psd = 20.0 * np.log10(np.abs(spec) + 1e-6)
                mid = len(psd) // 2             # notch the DC spike at centre
                psd[mid - DC_NOTCH_BINS: mid + DC_NOTCH_BINS + 1] = psd.min()
                row = self._downsample(psd, OUT_BINS)
                half = PAN_SAMPLE_RATE // 2
                self._emit({
                    "t": "pan", "center": self.center, "span": PAN_SAMPLE_RATE,
                    "f0": self.center - half, "f1": self.center + half,
                    "db": [round(float(x), 1) for x in row],
                })
                fps_n += 1
                if now - fps_t >= 1.0:
                    self._fps = fps_n / (now - fps_t); fps_t, fps_n = now, 0
                if self.follow:                 # re-centre on the radio when it drifts
                    f = self._radio_freq()
                    if f and abs(f - self.center) > FOLLOW_RETUNE_HZ:
                        self.center = f
                        self._kill(self._proc)  # stop + reap the old capture
                        self._proc = None
                        if self._stop.is_set():
                            break
                        time.sleep(0.12)        # let the USB device release
                        self._proc = self._spawn_transfer()
                        stdout = self._proc.stdout if self._proc else None
                        buf.clear()
        except Exception as exc:  # noqa: BLE001
            if not self._stop.is_set():
                self.error = f"hackrf_transfer failed: {exc}"
        finally:
            self._kill(self._proc)              # the thread always reaps its own proc
            self._proc = None
            self.running = False

    # -- sweep (hackrf_sweep across a range) ------------------------------
    def _run_sweep(self) -> None:
        f0 = min(self.sweep_start, self.sweep_stop)
        f1 = max(self.sweep_start, self.sweep_stop)
        f1 = max(f1, f0 + 1_000_000)
        lo, hi = f0 // 1_000_000, -(-f1 // 1_000_000)   # MHz, round hi up
        # FFT bin width so the requested range fills ~OUT_BINS buckets
        bin_hz = int(max(2_450, min(5_000_000, (f1 - f0) / OUT_BINS)))
        cmd = ["hackrf_sweep", "-f", f"{lo}:{hi}", "-w", str(bin_hz),
               "-l", str(self.lna), "-g", str(self.vga), "-a", "1" if self.amp else "0"]
        out_bin = (f1 - f0) / OUT_BINS
        acc = np.full(OUT_BINS, -120.0, dtype=np.float32)
        cur_ts = None
        fps_t, fps_n = time.monotonic(), 0

        def flush():
            nonlocal fps_n, fps_t
            self._emit({"t": "sweep", "center": (f0 + f1) // 2, "span": f1 - f0,
                        "f0": f0, "f1": f1, "db": [round(float(x), 1) for x in acc]})
            acc[:] = -120.0
            fps_n += 1
            now = time.monotonic()
            if now - fps_t >= 1.0:
                self._fps = fps_n / (now - fps_t); fps_t, fps_n = now, 0

        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, bufsize=1,
                                          universal_newlines=True)
            self._procs.add(self._proc)
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                parts = line.split(", ")
                if len(parts) < 7:
                    continue
                try:
                    hz_low = int(parts[2]); bin_w = float(parts[4])
                    vals = [float(x) for x in parts[6:]]
                except ValueError:
                    continue
                ts = parts[1]                  # all segments of one sweep share a timestamp
                if cur_ts is not None and ts != cur_ts:
                    flush()
                cur_ts = ts
                for i, v in enumerate(vals):   # map each FFT bin into the display range
                    freq = hz_low + i * bin_w
                    if f0 <= freq < f1:
                        idx = int((freq - f0) / out_bin)
                        if 0 <= idx < OUT_BINS and v > acc[idx]:
                            acc[idx] = v
            if self._proc and self._proc.poll() not in (None, 0) and not self._stop.is_set():
                self.error = "hackrf_sweep exited"
        except Exception as exc:  # noqa: BLE001
            if not self._stop.is_set():
                self.error = f"hackrf_sweep failed: {exc}"
        finally:
            self._kill(self._proc)
            self._proc = None
            self.running = False

    @staticmethod
    def _downsample(psd: np.ndarray, n: int) -> np.ndarray:
        if len(psd) == n:
            return psd
        if len(psd) > n:
            # peak-hold per bucket, vectorised (reduceat over bucket edges)
            edges = np.linspace(0, len(psd), n + 1).astype(int)[:-1]
            return np.maximum.reduceat(psd, edges).astype(np.float32)
        # fewer input bins than output → interpolate up for a smooth row
        xp = np.linspace(0, 1, len(psd))
        return np.interp(np.linspace(0, 1, n), xp, psd).astype(np.float32)

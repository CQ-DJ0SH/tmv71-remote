"""Classic 5-tone selective calling (Selektivruf): ZVEI / CCIR / EEA.

Encode a 5-digit code to an audio tone sequence (fed to the radio mic, keyed),
and decode such sequences from the RX audio. Consecutive equal digits use the
standard's repeat tone ("R"). Tones are contiguous (no gaps); a digit change is
always a frequency change, so the decoder segments cleanly on frequency.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 48000

# Tone tables (Hz). Index 0-9 = digits, "R" = repeat tone (multimon-ng values).
STANDARDS = {
    "zvei1": {"0": 2400, "1": 1060, "2": 1160, "3": 1270, "4": 1400, "5": 1530,
              "6": 1670, "7": 1830, "8": 2000, "9": 2200, "R": 2600},
    "zvei2": {"0": 2400, "1": 1060, "2": 1160, "3": 1270, "4": 1400, "5": 1530,
              "6": 1670, "7": 1830, "8": 2000, "9": 2200, "R": 970},
    "ccir":  {"0": 1981, "1": 1124, "2": 1197, "3": 1275, "4": 1358, "5": 1446,
              "6": 1540, "7": 1640, "8": 1747, "9": 1860, "R": 2110},
    "eea":   {"0": 1981, "1": 1124, "2": 1197, "3": 1275, "4": 1358, "5": 1446,
              "6": 1540, "7": 1640, "8": 1747, "9": 1860, "R": 2110},
}


def encode(code: str, standard: str = "zvei1", tone_ms: float = 70.0,
           fs: int = SAMPLE_RATE) -> np.ndarray:
    """5-tone sequence with repeat-tone substitution + soft start/end edges."""
    table = STANDARDS.get(standard, STANDARDS["zvei1"])
    digits = [c for c in code if c.isdigit()]
    if not digits:
        return np.zeros(0, dtype=np.int16)
    seq = []
    prev = None
    for d in digits:
        seq.append("R" if d == prev else d)
        prev = d
    n = int(tone_ms / 1000.0 * fs)
    out = np.zeros(n * len(seq), dtype=np.float32)
    phase = 0.0
    for i, sym in enumerate(seq):
        f = table[sym]
        t = np.arange(n)
        out[i * n:(i + 1) * n] = np.sin(phase + 2 * np.pi * f * t / fs)
        phase = (phase + 2 * np.pi * f * n / fs) % (2 * np.pi)
    r = max(1, int(0.004 * fs))                  # 4 ms edges to avoid clicks
    if len(out) > 2 * r:
        ramp = 0.5 * (1 - np.cos(np.pi * np.arange(r) / r))
        out[:r] *= ramp
        out[-r:] *= ramp[::-1]
    return np.clip(out * 0.5 * 32767, -32768, 32767).astype(np.int16)


class SelcallDecoder:
    """Streaming 5-tone decoder: Goertzel bank + run-length tone segmentation."""

    def __init__(self, standard: str = "zvei1", tone_ms: float = 70.0,
                 fs: int = SAMPLE_RATE):
        self.fs = fs
        self.table = STANDARDS.get(standard, STANDARDS["zvei1"])
        self.syms = list(self.table.keys())
        self.freqs = np.array([self.table[s] for s in self.syms], dtype=np.float64)
        self.win = max(8, int(fs * tone_ms / 1000.0 / 3))   # ~3 windows per tone
        t = np.arange(self.win)
        self._cos = np.cos(2 * np.pi * np.outer(self.freqs, t) / fs)
        self._sin = np.sin(2 * np.pi * np.outer(self.freqs, t) / fs)
        self.buf = np.zeros(0, dtype=np.float32)
        self.cur = None           # current dominant symbol
        self.run = 0              # windows in the current run
        self.min_run = 2          # windows needed to accept a tone
        self.gap = 0              # consecutive silent windows
        self.digits = ""
        self.prev_digit = None

    def _dominant(self, w: np.ndarray):
        p = (self._cos @ w) ** 2 + (self._sin @ w) ** 2
        k = int(np.argmax(p))
        total = float(p.sum()) + 1e-9
        # require the winner to dominate (a clean single tone) and be loud enough
        if p[k] / total < 0.55 or p[k] / self.win ** 2 < 1e-4:
            return None
        return self.syms[k]

    def _commit(self, sym: str) -> None:
        if sym == "R":
            if self.prev_digit is not None:
                self.digits += self.prev_digit
        else:
            self.digits += sym
            self.prev_digit = sym

    def _reset(self) -> None:
        self.digits = ""
        self.prev_digit = None
        self.cur = None
        self.run = 0

    def _emit_if_full(self, out: list) -> None:
        if len(self.digits) >= 5:
            out.append(self.digits[:5])
            self._reset()

    def feed(self, pcm: np.ndarray):
        self.buf = np.concatenate([self.buf, pcm.astype(np.float32) / 32768.0])
        out = []
        while len(self.buf) >= self.win:
            w = self.buf[:self.win]
            self.buf = self.buf[self.win:]
            sym = self._dominant(w)
            if sym is None:                          # silence / no clean tone
                self.gap += 1
                if self.cur is not None and self.run >= self.min_run:
                    self._commit(self.cur)
                    self._emit_if_full(out)
                self.cur = None
                self.run = 0
                if self.gap >= 3:                    # tone train ended -> drop partial
                    self._reset()
                continue
            self.gap = 0
            if sym == self.cur:
                self.run += 1
            else:                                    # tone boundary (freq change)
                if self.cur is not None and self.run >= self.min_run:
                    self._commit(self.cur)
                    self._emit_if_full(out)
                self.cur = sym
                self.run = 1
        return out


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    rng = np.random.default_rng(2)

    def noisy(sig, snr_db=25):
        s = sig.astype(np.float32) / 32768.0
        p = np.mean(s ** 2) or 1e-9
        n = rng.normal(0, (p / (10 ** (snr_db / 10))) ** 0.5, len(s))
        return np.clip((s + n) * 32768, -32768, 32767).astype(np.int16)

    for std in ("zvei1", "ccir"):
        for code in ("12345", "11233", "54321", "00100"):
            a = encode(code, std)
            # pad with silence so the decoder sees an end-of-train gap
            a = np.concatenate([a, np.zeros(2400, dtype=np.int16)])
            dec = SelcallDecoder(std)
            got = []
            for i in range(0, len(a), 4800):
                got += dec.feed(noisy(a, 25)[i:i + 4800])
            print(f"{std} in={code} out={got}")

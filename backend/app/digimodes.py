"""CW (Morse) and RTTY (Baudot/AFSK) encode + decode for the digimodes panel.

Over the FM TM-V71 these are MCW (a keyed audio tone) and AFSK RTTY (audio FSK
fed into the mic) — not native HF CW/RTTY, but they work for FM experiments.

Encoders return int16 mono @ ``fs``. Decoders are fed int16 blocks via ``feed``
and return any newly decoded text incrementally (streaming, stateful).
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 48000

# ---------------------------------------------------------------- Morse / CW
MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-",
    "+": ".-.-.", "-": "-....-", ":": "---...", "(": "-.--.", ")": "-.--.-",
    "@": ".--.-.", "'": ".----.", "\"": ".-..-.",
}
INV_MORSE = {v: k for k, v in MORSE.items()}


def cw_encode(text: str, wpm: float = 18.0, pitch: float = 700.0,
              fs: int = SAMPLE_RATE) -> np.ndarray:
    """Keyed sine (PARIS timing) with 5 ms raised-cosine edges (no clicks)."""
    dot = 1.2 / max(5.0, wpm)                       # seconds per dot
    r = max(1, int(0.005 * fs))

    def tone(dots: float) -> np.ndarray:
        n = int(dots * dot * fs)
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        t = np.arange(n) / fs
        env = np.ones(n, dtype=np.float32)
        if n > 2 * r:
            ramp = 0.5 * (1 - np.cos(np.pi * np.arange(r) / r))
            env[:r] = ramp
            env[-r:] = ramp[::-1]
        return (np.sin(2 * np.pi * pitch * t) * env).astype(np.float32)

    def gap(dots: float) -> np.ndarray:
        return np.zeros(max(0, int(dots * dot * fs)), dtype=np.float32)

    out = [gap(1)]
    for wi, word in enumerate(text.upper().split(" ")):
        if wi:
            out.append(gap(7))
        first = True
        for ch in word:
            code = MORSE.get(ch)
            if not code:
                continue
            if not first:
                out.append(gap(3))
            first = False
            for ei, el in enumerate(code):
                if ei:
                    out.append(gap(1))
                out.append(tone(1 if el == "." else 3))
    out.append(gap(3))
    sig = np.concatenate(out) if out else np.zeros(0, dtype=np.float32)
    return np.clip(sig * 0.6 * 32767, -32768, 32767).astype(np.int16)


class CWDecoder:
    """Streaming Morse decoder: Goertzel envelope at ``pitch`` + adaptive timing."""

    def __init__(self, fs: int = SAMPLE_RATE, pitch: float = 700.0, wpm: float = 18.0,
                 auto: bool = True):
        self.fs = fs
        self.auto = auto                              # adapt the dot length to the RX speed
        self.win = max(1, int(fs * 0.005))           # 5 ms non-overlapping windows
        self._t = np.arange(self.win)
        self._cos = np.cos(2 * np.pi * pitch * self._t / fs)
        self._sin = np.sin(2 * np.pi * pitch * self._t / fs)
        self.buf = np.zeros(0, dtype=np.float32)
        self.noise = 1e-3
        self.peak = 1e-2
        self.state = False
        self.run = 0                                  # windows in current state
        self.dot = max(1, int((1.2 / wpm) / 0.005))   # dot length in windows
        self.symbol = ""
        self._space_pending = False

    def _classify_mark(self, length: int) -> None:
        if length <= 0:
            return
        if length < 2 * self.dot:
            self.symbol += "."
            if self.auto:
                self.dot = max(1, int(0.7 * self.dot + 0.3 * length))
        else:
            self.symbol += "-"
            if self.auto:
                self.dot = max(1, int(0.7 * self.dot + 0.3 * max(1, length // 3)))

    def _flush(self) -> str:
        if not self.symbol:
            return ""
        ch = INV_MORSE.get(self.symbol, "")
        self.symbol = ""
        return ch

    def feed(self, pcm: np.ndarray) -> str:
        self.buf = np.concatenate([self.buf, pcm.astype(np.float32) / 32768.0])
        out = []
        while len(self.buf) >= self.win:
            w = self.buf[:self.win]
            self.buf = self.buf[self.win:]
            i = float(np.dot(w, self._cos))
            q = float(np.dot(w, self._sin))
            mag = (i * i + q * q) ** 0.5 / self.win
            thr = (self.noise + self.peak) / 2
            tone = mag > thr
            # adapt the floor / peak trackers
            if tone:
                self.peak = 0.9 * self.peak + 0.1 * mag
            else:
                self.noise = 0.9 * self.noise + 0.1 * mag
            self.peak = max(self.peak, self.noise * 4 + 1e-4)

            if tone != self.state:
                if self.state:                         # end of a mark
                    self._classify_mark(self.run)
                    self._space_pending = True
                else:                                  # end of a gap
                    if self.run >= 2 * self.dot:
                        ch = self._flush()
                        if ch:
                            out.append(ch)
                self.state = tone
                self.run = 1
            else:
                self.run += 1
                if not self.state and self._space_pending:
                    # flush the character once the inter-char gap is reached, and
                    # add a single space at the (longer) word gap
                    if self.symbol and self.run == 2 * self.dot:
                        ch = self._flush()
                        if ch:
                            out.append(ch)
                    if self.run == 6 * self.dot:
                        out.append(" ")
                        self._space_pending = False
        return "".join(out)


# ---------------------------------------------------------------- Baudot / RTTY
# ITA2: 5-bit codes (b1 first / as transmitted), '1' = mark, '0' = space.
_ITA2 = {
    "00011": ("A", "-"), "11001": ("B", "?"), "01110": ("C", ":"),
    "01001": ("D", "$"), "00001": ("E", "3"), "01101": ("F", "!"),
    "11010": ("G", "&"), "10100": ("H", "#"), "00110": ("I", "8"),
    "01011": ("J", "'"), "01111": ("K", "("), "10010": ("L", ")"),
    "11100": ("M", "."), "01100": ("N", ","), "11000": ("O", "9"),
    "10110": ("P", "0"), "10111": ("Q", "1"), "01010": ("R", "4"),
    "00101": ("S", "'"), "10000": ("T", "5"), "00111": ("U", "7"),
    "11110": ("V", ";"), "10011": ("W", "2"), "11101": ("X", "/"),
    "10101": ("Y", "6"), "10001": ("Z", "\""),
    "00100": (" ", " "), "01000": ("\r", "\r"), "00010": ("\n", "\n"),
    "00000": ("", ""),
}
_LTRS = "11111"
_FIGS = "11011"
_ENC_LTR = {v[0]: k for k, v in _ITA2.items() if v[0] and v[0] not in "\r\n"}
_ENC_FIG = {v[1]: k for k, v in _ITA2.items() if v[1] and v[1] not in "\r\n"}
_ENC_LTR[" "] = "00100"


def rtty_encode(text: str, baud: float = 45.45, shift: float = 170.0,
                mark: float = 2125.0, fs: int = SAMPLE_RATE) -> np.ndarray:
    """AFSK Baudot: 1 start (space), 5 data (b1 first), 1.5 stop (mark)."""
    space = mark + shift
    spb = fs / baud
    segs: list[tuple[float, int]] = []          # (freq, n_samples)

    def add(freq: float, nbits: float) -> None:
        segs.append((freq, int(round(nbits * spb))))

    def send_code(code: str) -> None:
        add(space, 1.0)                          # start
        for b in code:                           # data, b1 first
            add(mark if b == "1" else space, 1.0)
        add(mark, 1.5)                            # stop

    add(mark, 8.0)                               # idle lead-in (mark)
    state = "LTR"
    for ch in text.upper():
        if ch == "\n":
            send_code(_ITA2_inv("\r")); send_code(_ITA2_inv("\n")); continue
        in_ltr = ch in _ENC_LTR
        in_fig = ch in _ENC_FIG
        if not in_ltr and not in_fig:
            continue
        want = "LTR" if in_ltr else "FIG"
        # space exists in both; don't shift just for a space
        if ch != " " and want != state and not (in_ltr and in_fig):
            send_code(_LTRS if want == "LTR" else _FIGS)
            state = want
        code = _ENC_LTR[ch] if (in_ltr and (state == "LTR" or ch == " ")) else _ENC_FIG[ch]
        send_code(code)
    add(mark, 4.0)                               # idle tail

    total = sum(n for _, n in segs)
    out = np.zeros(total, dtype=np.float32)
    phase = 0.0
    i = 0
    for f, n in segs:
        t = np.arange(n)
        out[i:i + n] = np.sin(phase + 2 * np.pi * f * t / fs)
        phase = (phase + 2 * np.pi * f * n / fs) % (2 * np.pi)
        i += n
    return np.clip(out * 0.6 * 32767, -32768, 32767).astype(np.int16)


def _ITA2_inv(ch: str) -> str:
    for code, (lt, fg) in _ITA2.items():
        if lt == ch:
            return code
    return "00000"


class RTTYDecoder:
    """Streaming AFSK Baudot decoder (software UART over a mark/space slicer)."""

    def __init__(self, fs: int = SAMPLE_RATE, baud: float = 45.45,
                 shift: float = 170.0, mark: float = 2125.0):
        self.fs = fs
        self.spb = fs / baud
        self.mark = mark
        self.space = mark + shift
        self.win = max(8, int(self.spb * 0.8))
        self.hop = max(4, int(self.spb / 6))
        t = np.arange(self.win)
        self._mc = np.cos(2 * np.pi * mark * t / fs)
        self._ms = np.sin(2 * np.pi * mark * t / fs)
        self._sc = np.cos(2 * np.pi * self.space * t / fs)
        self._ss = np.sin(2 * np.pi * self.space * t / fs)
        self.buf = np.zeros(0, dtype=np.float32)
        self.base = 0                                # abs index of buf[0]
        self.cursor = self.win // 2 + 1              # abs scan pos (window must fit)
        self.shift = "LTR"

    def _bit(self, abs_idx: float) -> int | None:
        """1 = mark, 0 = space, None if not enough samples buffered."""
        start = int(abs_idx - self.win / 2) - self.base
        if start < 0 or start + self.win > len(self.buf):
            return None
        w = self.buf[start:start + self.win]
        mp = np.dot(w, self._mc) ** 2 + np.dot(w, self._ms) ** 2
        sp = np.dot(w, self._sc) ** 2 + np.dot(w, self._ss) ** 2
        return 1 if mp >= sp else 0

    def feed(self, pcm: np.ndarray) -> str:
        self.buf = np.concatenate([self.buf, pcm.astype(np.float32) / 32768.0])
        out = []
        last_end = self.base + len(self.buf)
        while self.cursor + int(7.5 * self.spb) < last_end:
            b = self._bit(self.cursor)
            if b is None:
                break
            if b == 1:                               # idle / mark — keep scanning
                self.cursor += self.hop
                continue
            # candidate start bit (space). confirm mid-start is still space.
            if self._bit(self.cursor + 0.5 * self.spb) != 0:
                self.cursor += self.hop
                continue
            bits = [self._bit(self.cursor + (1.5 + k) * self.spb) for k in range(5)]
            if any(x is None for x in bits):
                break
            code = "".join(str(x) for x in bits)
            if code == _LTRS:
                self.shift = "LTR"
            elif code == _FIGS:
                self.shift = "FIG"
            else:
                pair = _ITA2.get(code)
                if pair:
                    ch = pair[0] if self.shift == "LTR" else pair[1]
                    if ch:
                        out.append(ch)
            self.cursor += int(7.0 * self.spb)        # past the stop bit
        # trim consumed samples to keep the buffer bounded
        keep_from = self.cursor - int(2 * self.spb)
        if keep_from - self.base > self.fs:           # trim ~1 s+ of history
            drop = keep_from - self.base
            self.buf = self.buf[drop:]
            self.base += drop
        return "".join(out)


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    rng = np.random.default_rng(1)

    def noisy(sig, snr_db=20):
        s = sig.astype(np.float32) / 32768.0
        p = np.mean(s ** 2) or 1e-9
        n = rng.normal(0, (p / (10 ** (snr_db / 10))) ** 0.5, len(s))
        return np.clip((s + n) * 32768, -32768, 32767).astype(np.int16)

    msg = "CQ DE DJ0SH"
    a = cw_encode(msg, wpm=20)
    d = CWDecoder(pitch=700.0, wpm=20)
    got = "".join(d.feed(noisy(a, 25)[i:i + 4800]) for i in range(0, len(a), 4800))
    print(f"CW   in={msg!r}  out={got.strip()!r}")

    msg2 = "RYRY TEST DE DJ0SH 599"
    b = rtty_encode(msg2)
    r = RTTYDecoder()
    got2 = "".join(r.feed(noisy(b, 30)[i:i + 4800]) for i in range(0, len(b), 4800))
    print(f"RTTY in={msg2!r}  out={got2.strip()!r}")

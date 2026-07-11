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


# --------------------------------------------------------------------- POCSAG
# POCSAG (CCIR Radiopaging Code No. 1): 2-FSK NRZ at 512/1200/2400 baud. On an FM
# radio the NRZ bitstream directly frequency-modulates the carrier, so on the
# audio path we send a band-limited bipolar baseband (bit 0 = +level, bit 1 =
# -level) and slice it back on RX. Structure: a 576-bit 1010… preamble, then
# batches of a 32-bit sync word + 8 frames × 2 codewords; every codeword is
# BCH(31,21) with an even-parity bit (bit 0).
POCSAG_SYNC = 0x7CD215D8
POCSAG_IDLE = 0x7A89C197
POCSAG_IDLE31 = POCSAG_IDLE >> 1
POCSAG_PREAMBLE_BITS = 576
_BCH_GEN = 0b11101101001            # x^10+x^9+x^8+x^6+x^5+x^3+1
_POCSAG_NUM = "0123456789*U -)("    # numeric value 0..15 -> char


def _rev4(v: int) -> int:
    """Reverse the low 4 bits (POCSAG sends numeric nibbles LSB-first)."""
    return ((v & 1) << 3) | ((v & 2) << 1) | ((v & 4) >> 1) | ((v & 8) >> 3)


def _pocsag_syndrome(w31: int) -> int:
    """BCH remainder of a 31-bit codeword (0 = no detected error)."""
    reg = w31 & 0x7FFFFFFF
    for i in range(30, 9, -1):
        if reg & (1 << i):
            reg ^= _BCH_GEN << (i - 10)
    return reg & 0x3FF


def _pocsag_codeword(data21: int) -> int:
    """21 data bits (MSB = flag) -> full 32-bit codeword (BCH + even parity)."""
    c31 = ((data21 & 0x1FFFFF) << 10) | _pocsag_syndrome((data21 & 0x1FFFFF) << 10)
    return (c31 << 1) | (bin(c31).count("1") & 1)


def _bits_of(word: int, n: int = 32):
    return [(word >> (n - 1 - i)) & 1 for i in range(n)]


def _num_payloads(text: str):
    nib = []
    for ch in text.upper():
        v = _POCSAG_NUM.find(ch)
        nib.append(_rev4(v if v >= 0 else _POCSAG_NUM.find(" ")))
    while len(nib) % 5:
        nib.append(_rev4(_POCSAG_NUM.find(" ")))
    out = []
    for i in range(0, len(nib), 5):
        w = 0
        for r in nib[i:i + 5]:
            w = (w << 4) | r
        out.append(w & 0xFFFFF)
    return out


def _alpha_payloads(text: str):
    bits = []
    for ch in text:
        c = ord(ch) & 0x7F
        for b in range(7):                       # LSB first
            bits.append((c >> b) & 1)
    while len(bits) % 20:
        bits.append(0)
    out = []
    for i in range(0, len(bits), 20):
        w = 0
        for b in bits[i:i + 20]:
            w = (w << 1) | b                     # first stream bit -> field MSB
        out.append(w & 0xFFFFF)
    return out


def _pocsag_message_bits(address: int, function: int, text: str, alpha: bool):
    addr_cw = _pocsag_codeword(((address >> 3) & 0x3FFFF) << 2 | (function & 3))
    payloads = _alpha_payloads(text) if alpha else _num_payloads(text)
    seq = [addr_cw] + [_pocsag_codeword((1 << 20) | p) for p in payloads]
    words = [POCSAG_IDLE] * ((address & 7) * 2) + seq       # address in its frame
    while len(words) % 16:
        words.append(POCSAG_IDLE)
    bits = [1, 0] * (POCSAG_PREAMBLE_BITS // 2)
    for i in range(0, len(words), 16):
        bits += _bits_of(POCSAG_SYNC)
        for w in words[i:i + 16]:
            bits += _bits_of(w)
    return bits


def pocsag_encode(text: str, address: int = 1234567, baud: int = 1200,
                  function: int = 3, alpha: bool = False,
                  fs: int = SAMPLE_RATE) -> np.ndarray:
    """Encode a numeric/alphanumeric page as band-limited bipolar FSK baseband."""
    bits = _pocsag_message_bits(int(address), int(function), text or "", bool(alpha))
    spb = fs / float(baud)
    sig = np.zeros(int(round(len(bits) * spb)), dtype=np.float32)
    for i, b in enumerate(bits):
        a, z = int(round(i * spb)), int(round((i + 1) * spb))
        sig[a:z] = 1.0 if b == 0 else -1.0       # bit 0 -> +level
    k = max(1, int(spb / 3))                      # gentle low-pass (limit splatter)
    if k > 1:
        sig = np.convolve(sig, np.ones(k, np.float32) / k, mode="same")
    ramp = min(len(sig) // 2, int(0.005 * fs))
    if ramp:
        sig[:ramp] *= np.linspace(0, 1, ramp)
        sig[-ramp:] *= np.linspace(1, 0, ramp)
    return np.clip(sig * 24000, -32768, 32767).astype(np.int16)


class POCSAGDecoder:
    """Streaming POCSAG decoder: transition-tracked bit slicer -> sync search ->
    BCH-corrected codewords -> numeric/alphanumeric text. Handles either FSK
    polarity. ``feed`` returns any newly completed pages ("[RIC] text\\n")."""

    def __init__(self, fs: int = SAMPLE_RATE, baud: int = 1200, alpha: bool = False,
                 addr: int = 0, listen_all: bool = True):
        self.fs = fs
        self.spb = fs / float(baud)
        self.alpha = alpha
        self.addr = addr
        self.listen_all = listen_all
        self.dc = 0.0
        self.env = 0.0
        self.last_sign = 1
        self.idx = 0.0
        self.next_center = self.spb / 2.0
        self.reg = 0
        self.synced = False
        self.inv = False
        self.words: list = []
        self.bitcount = 0
        self.cur_active = False
        self.cur_ric = 0
        self.cur_func = 0
        self.msg = []

    def _flush(self) -> str:
        active, ric, func, payloads = self.cur_active, self.cur_ric, self.cur_func, self.msg
        self.cur_active, self.msg = False, []
        if not active or not payloads:
            return ""
        if not self.listen_all and ric != self.addr:      # filter to our RIC
            return ""
        if self.alpha:
            bits = []
            for w in payloads:
                bits += [(w >> b) & 1 for b in range(19, -1, -1)]
            chars = []
            for i in range(0, len(bits) - 6, 7):
                c = sum(bits[i + k] << k for k in range(7))
                if c == 0:
                    continue
                chars.append(chr(c) if 32 <= c < 127 else "")
            text = "".join(chars).rstrip()
        else:
            text = ""
            for w in payloads:
                for k in range(5):
                    text += _POCSAG_NUM[_rev4((w >> (16 - 4 * k)) & 0xF)]
            text = text.rstrip()
        if not text:
            return ""
        kind = "ALPHA" if self.alpha else "NUM"
        return f"RIC {ric:>7} · FUNC {'ABCD'[func & 3]} · {kind} · {text}\n"

    def _decode_batch(self, words) -> str:
        out = []
        for j, w in enumerate(words):
            w31 = w >> 1
            if _pocsag_syndrome(w31) != 0:               # single-bit BCH correction
                for i in range(31):
                    if _pocsag_syndrome(w31 ^ (1 << i)) == 0:
                        w31 ^= (1 << i)
                        break
                else:
                    continue                             # uncorrectable -> skip
            if w31 == POCSAG_IDLE31:
                out.append(self._flush())
            elif (w31 >> 30) & 1:                         # message codeword
                if self.cur_active:
                    self.msg.append((w31 >> 10) & 0xFFFFF)
            else:                                         # address codeword
                out.append(self._flush())
                self.cur_ric = (((w31 >> 12) & 0x3FFFF) << 3) | (j // 2)
                self.cur_func = (w31 >> 10) & 0x3
                self.cur_active, self.msg = True, []
        return "".join(out)

    def _push_bit(self, bit: int) -> str:
        self.reg = ((self.reg << 1) | bit) & 0xFFFFFFFF
        if not self.synced:
            if bin((self.reg ^ POCSAG_SYNC) & 0xFFFFFFFF).count("1") <= 2:
                self.synced, self.inv, self.words, self.bitcount = True, False, [], 0
            elif bin((self.reg ^ POCSAG_SYNC ^ 0xFFFFFFFF) & 0xFFFFFFFF).count("1") <= 2:
                self.synced, self.inv, self.words, self.bitcount = True, True, [], 0
            return ""
        self.bitcount += 1
        if self.bitcount < 32:
            return ""
        self.bitcount = 0
        self.words.append((self.reg ^ (0xFFFFFFFF if self.inv else 0)) & 0xFFFFFFFF)
        if len(self.words) < 16:
            return ""
        text = self._decode_batch(self.words)
        self.words, self.synced = [], False          # re-acquire sync for next batch
        return text

    def feed(self, pcm: np.ndarray) -> str:
        out = []
        for s in pcm.astype(np.float32):
            self.dc += 0.025 * (s - self.dc)         # ~1-bit baseline tracker (follows the
            v = s - self.dc                          # AC-coupled RX droop / baseline wander)
            a = v if v >= 0 else -v
            self.env = a if a > self.env else self.env * 0.9998   # slow peak follower (AGC)
            h = 0.30 * self.env                      # Schmitt hysteresis (reject noise flutter)
            if v > h:
                sign = 1
            elif v < -h:
                sign = -1
            else:
                sign = self.last_sign                # hold within the deadband
            self.idx += 1.0
            if sign != self.last_sign:               # data edge -> nudge phase to mid-bit
                self.next_center += 0.05 * ((self.idx + self.spb / 2.0) - self.next_center)
                self.last_sign = sign
            if self.idx >= self.next_center:
                self.next_center += self.spb
                t = self._push_bit(0 if sign > 0 else 1)   # +level -> bit 0
                if t:
                    out.append(t)
        if self.idx > 1e7:                           # keep the phase counter bounded
            self.idx -= 1e7
            self.next_center -= 1e7
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

    for baud in (512, 1200, 2400):
        for alpha, msg3 in ((False, "1234567890"), (True, "HELLO DJ0SH")):
            ric = 1234567
            p = pocsag_encode(msg3, address=ric, baud=baud, alpha=alpha)
            pd = POCSAGDecoder(baud=baud, alpha=alpha)
            got3 = "".join(pd.feed(noisy(p, 30)[i:i + 4800]) for i in range(0, len(p), 4800))
            got3 += pd.feed(noisy(p[:1], 30) * 0)     # (no-op flush guard)
            print(f"POCSAG {baud:>4} {'ALPHA' if alpha else 'NUM  '} "
                  f"in={msg3!r}  out={got3.strip()!r}")

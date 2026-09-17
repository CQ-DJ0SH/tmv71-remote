"""APRS receive: Bell 202 AFSK → AX.25 (HDLC) → APRS payload.

Three layers, each the classic one:

1. Demodulation. Two matched filters, one bit long, at 1200 Hz (mark) and
   2200 Hz (space); their magnitude difference is the soft bit stream. The RTTY
   decoder in digimodes.py uses the same correlator idea — but RTTY samples from
   a start bit, while AX.25 runs continuously, so the bit clock has to be
   recovered from the signal itself (a zero-crossing DPLL below).
2. Framing. NRZI (a transition means 0), HDLC flags 0x7E, bit de-stuffing, and
   the FCS. The CRC check is not optional: without it, noise regularly produces
   frames that look plausible, and every one of them would become a ghost
   station in the list.
3. APRS. AX.25 addresses, then the payload — uncompressed and compressed
   positions, MIC-E (which most mobile stations use), status, messages,
   objects.

The TM-V71 has no built-in TNC (that is the TM-D710), so this is the only way
to hear APRS with it. Feed it the flat 9600-baud discriminator output rather
than the speaker path: no de-emphasis, no squelch chopping the frame.
"""
from __future__ import annotations

import math
import time
from typing import Optional

import numpy as np

MARK_HZ = 1200.0
SPACE_HZ = 2200.0
BAUD = 1200.0
MIN_FRAME = 17          # 2 addresses (14) + control + PID + at least the FCS


def crc_x25(data: bytes) -> int:
    """CRC-16/X.25 — reflected 0x1021, init 0xFFFF, final XOR 0xFFFF."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _addr(raw: bytes) -> tuple[str, int, bool]:
    """One AX.25 address field: 6 shifted ASCII chars + SSID byte."""
    call = "".join(chr(b >> 1) for b in raw[:6]).strip()
    ssid = (raw[6] >> 1) & 0x0F
    repeated = bool(raw[6] & 0x80)      # H bit: this digipeater has used it
    return call, ssid, repeated


def _call(call: str, ssid: int) -> str:
    return f"{call}-{ssid}" if ssid else call


def _dm(v: float, lat: bool) -> str:
    """Decimal degrees as the usual ham notation (52.3125 -> 52°18.75'N)."""
    hemi = ("N" if v >= 0 else "S") if lat else ("E" if v >= 0 else "W")
    v = abs(v)
    d = int(v)
    return f"{d}°{(v - d) * 60:05.2f}'{hemi}"


class _Frame:
    """One decoded AX.25/APRS frame, in the shape the panel needs."""

    def __init__(self, src: str, dst: str, path: list[str], info: bytes):
        self.ts = time.time()
        self.src, self.dst, self.path, self.info = src, dst, path, info
        self.kind = "?"
        self.lat: Optional[float] = None
        self.lon: Optional[float] = None
        self.course: Optional[int] = None
        self.speed: Optional[float] = None        # km/h
        self.alt: Optional[float] = None          # m
        self.symbol = ""
        self.comment = ""
        self.target = ""                          # message addressee / object name
        self.text = ""                            # message text / status

    # --- rendering -------------------------------------------------------
    def line(self) -> str:
        head = f"{self.src}>{self.dst}"
        if self.path:
            head += "," + ",".join(self.path)
        bits = [head + ":"]
        if self.lat is not None:
            bits.append(f"{_dm(self.lat, True)} {_dm(self.lon, False)}")
        if self.course is not None and self.speed is not None:
            bits.append(f"{self.course:03d}° {self.speed:.0f} km/h")
        if self.alt is not None:
            bits.append(f"{self.alt:.0f} m")
        if self.target:
            bits.append(f"→{self.target}")
        if self.text:
            bits.append(f'"{self.text}"')
        if self.comment:
            bits.append(f'"{self.comment}"')
        return " ".join(bits)

    def debug(self) -> str:
        """The second line: what the panel shows as decoder detail."""
        raw = self.info.decode("ascii", "replace").rstrip()
        if len(raw) > 120:
            raw = raw[:117] + "…"
        sym = f" sym {self.symbol}" if self.symbol else ""
        return f"    ⤷ {self.kind}{sym} · {len(self.info)} B · {raw}"

    def as_dict(self) -> dict:
        return {"ts": self.ts, "src": self.src, "dst": self.dst,
                "path": self.path, "kind": self.kind,
                "lat": self.lat, "lon": self.lon, "course": self.course,
                "speed": self.speed, "alt": self.alt, "symbol": self.symbol,
                "comment": self.comment, "target": self.target, "text": self.text,
                "info": self.info.decode("ascii", "replace")}


# --- APRS payload ----------------------------------------------------------

def _parse_uncompressed(s: str, f: _Frame) -> bool:
    """DDMM.mmN/DDDMM.mmE$ — the plain position report."""
    if len(s) < 19:
        return False
    try:
        lat = int(s[0:2]) + float(s[2:7]) / 60.0
        if s[7] in "Ss":
            lat = -lat
        elif s[7] not in "Nn":
            return False
        lon = int(s[9:12]) + float(s[12:17]) / 60.0
        if s[17] in "Ww":
            lon = -lon
        elif s[17] not in "Ee":
            return False
    except ValueError:
        return False
    f.lat, f.lon = lat, lon
    f.symbol = s[8] + s[18]
    rest = s[19:]
    # course/speed "CSE/SPD" in knots, or an altitude comment
    if len(rest) >= 7 and rest[3] == "/" and rest[:3].isdigit() and rest[4:7].isdigit():
        f.course = int(rest[:3])
        f.speed = int(rest[4:7]) * 1.852
        rest = rest[7:]
    if "/A=" in rest:
        i = rest.index("/A=")
        try:
            f.alt = int(rest[i + 3:i + 9]) * 0.3048
            rest = rest[:i] + rest[i + 9:]
        except ValueError:
            pass
    f.comment = rest.strip()
    return True


def _parse_compressed(s: str, f: _Frame) -> bool:
    """Base-91 compressed position: /YYYYXXXX$cs T (13 chars)."""
    if len(s) < 13:
        return False
    y, x = s[1:5], s[5:9]
    if any(not (33 <= ord(c) <= 124) for c in y + x):
        return False
    yv = sum((ord(c) - 33) * 91 ** (3 - i) for i, c in enumerate(y))
    xv = sum((ord(c) - 33) * 91 ** (3 - i) for i, c in enumerate(x))
    f.lat = 90.0 - yv / 380926.0
    f.lon = -180.0 + xv / 190463.0
    f.symbol = s[0] + s[9]
    c, sp = s[10], s[11]
    if c != " " and 33 <= ord(c) <= 123:
        f.course = ((ord(c) - 33) * 4) % 360
        f.speed = (1.08 ** (ord(sp) - 33) - 1) * 1.852
    f.comment = s[13:].strip()
    return True


# MIC-E: the latitude is hidden in the DESTINATION callsign, the rest in the
# first bytes of the information field. Most mobile stations send this.
_MICE_DIGIT = {**{chr(0x30 + i): (str(i), 0, 0) for i in range(10)},
               **{chr(0x41 + i): (str(i), 1, 1) for i in range(10)},
               **{chr(0x50 + i): (str(i), 1, 0) for i in range(10)}}
_MICE_DIGIT.update({"K": (" ", 1, 1), "L": (" ", 0, 0), "Z": (" ", 1, 0)})


def _parse_mice(dst: str, s: str, f: _Frame) -> bool:
    if len(s) < 8 or len(dst) < 6:
        return False
    digits, ns, we, off = "", 0, 0, 0
    for i, ch in enumerate(dst[:6]):
        d = _MICE_DIGIT.get(ch)
        if d is None:
            return False
        digits += d[0]
        if i == 3:
            ns = d[1]                      # 1 = north
        elif i == 4:
            off = d[1]                     # longitude offset +100°
        elif i == 5:
            we = d[1]                      # 1 = west
    try:
        lat = int(digits[0:2]) + float(digits[2:4] + "." + digits[4:6]) / 60.0
    except ValueError:
        return False
    f.lat = lat if ns else -lat
    b = s.encode("latin-1") if isinstance(s, str) else s
    d28, m28, h28 = b[1] - 28, b[2] - 28, b[3] - 28
    deg = d28 + 100 if off else d28
    if 180 <= deg <= 189:
        deg -= 80
    elif 190 <= deg <= 199:
        deg -= 190
    minu = m28 - 60 if m28 >= 60 else m28
    lon = deg + (minu + (h28 / 100.0)) / 60.0
    f.lon = -lon if we else lon
    sp = (b[4] - 28) * 10
    dc = b[5] - 28
    sp += dc // 10
    course = (dc % 10) * 100 + (b[6] - 28)
    if sp >= 800:
        sp -= 800
    if course >= 400:
        course -= 400
    f.speed = sp * 1.852
    f.course = course
    f.symbol = chr(b[8]) + chr(b[7]) if len(b) > 8 else ""
    f.comment = b[9:].decode("ascii", "replace").strip()
    return True


def parse_payload(f: _Frame) -> None:
    """Fill the frame from its information field; kind says what was found."""
    if not f.info:
        f.kind = "leer"
        return
    s = f.info.decode("latin-1")
    t = s[0]
    body = s[1:]
    if t in ("`", "'", "\x1c", "\x1d"):
        f.kind = "MIC-E"
        if not _parse_mice(f.dst.split("-")[0], s, f):
            f.kind = "MIC-E?"
        return
    if t in ("!", "=", "/", "@"):
        # '/' and '@' carry a timestamp first (7 chars)
        pos = body[7:] if t in ("/", "@") else body
        f.kind = "Position"
        if pos[:1] in ("/", "\\") or (pos[:1].isalpha() and not pos[:2].isdigit()):
            if _parse_compressed(pos, f):
                f.kind = "Position (komprimiert)"
                return
        if not _parse_uncompressed(pos, f):
            f.kind = "Position?"
            f.comment = pos.strip()
        return
    if t == ">":
        f.kind, f.text = "Status", body.strip()
        return
    if t == ":":
        f.kind = "Nachricht"
        f.target = body[:9].strip()
        rest = body[9:]
        f.text = rest[1:].strip() if rest[:1] == ":" else rest.strip()
        return
    if t in (";", ")"):
        f.kind = "Objekt" if t == ";" else "Item"
        f.target = body[:9].strip()
        i = body.find("*")
        tail = body[10:] if t == ";" else body[body.find("!") + 1:]
        if not _parse_uncompressed(tail[7:] if t == ";" else tail, f):
            f.comment = tail.strip()
        return
    if t == "T":
        f.kind, f.comment = "Telemetrie", body.strip()
        return
    if t == "_":
        f.kind, f.comment = "Wetter", body.strip()
        return
    f.kind = f"unbekannt '{t}'"
    f.comment = body.strip()


class APRSDecoder:
    """Streaming Bell 202 / AX.25 decoder. feed(pcm) -> text for the panel."""

    BANNER = "APRS · 1200 Bd AFSK · Bell 202 · AX.25 · 144,800 MHz\n"

    def __init__(self, fs: int = 48000, verbose: bool = True):
        self.fs = fs
        self.sps = fs / BAUD                       # 40 samples per bit at 48 kHz
        n = int(round(self.sps))
        t = np.arange(n) / fs
        w = np.hanning(n)
        self._mk = (w * np.exp(-2j * np.pi * MARK_HZ * t))[::-1].conj()
        self._sp = (w * np.exp(-2j * np.pi * SPACE_HZ * t))[::-1].conj()
        self._tail = np.zeros(n - 1, dtype=np.float64)
        self._pos = self.sps / 2.0                 # next bit-centre, in samples
        self._last_sym = 1                         # for NRZI
        # BANNER is printed by the service when decoding starts, not here: this
        # class only ever sees audio, and with a closed squelch the first block
        # may not arrive for minutes — the line would be missing exactly when
        # someone is checking whether the mode is running at all.
        self.verbose = verbose        # print the per-frame decoder detail line
        # HDLC state
        self._sr = 0
        self._collect = False
        self._ones = 0
        self._bitbuf = 0
        self._nbits = 0
        self._frame = bytearray()
        # statistics for the panel
        self.frames = 0
        self.bad_crc = 0
        self.last: Optional[_Frame] = None
        self.stations: dict[str, dict] = {}

    # --- layer 1: AFSK -> soft bits ---------------------------------------
    def _soft(self, pcm: np.ndarray) -> np.ndarray:
        x = np.concatenate([self._tail, pcm.astype(np.float64)])
        self._tail = x[-(self._mk.size - 1):]
        m = np.abs(np.convolve(x, self._mk, mode="valid"))
        s = np.abs(np.convolve(x, self._sp, mode="valid"))
        return m - s

    def feed(self, pcm: np.ndarray) -> str:
        if pcm is None or not len(pcm):
            return ""
        diff = self._soft(pcm)
        sign = diff >= 0
        cross = np.flatnonzero(np.diff(sign.astype(np.int8)) != 0) + 1
        out: list[str] = []
        ci = 0
        half = self.sps / 2.0
        while self._pos < diff.size:
            # DPLL: every transition should sit half a bit from a bit centre
            while ci < cross.size and cross[ci] < self._pos:
                err = (self._pos - cross[ci]) - half
                err = (err + half) % self.sps - half
                self._pos -= err * 0.15            # loop gain: steady, not twitchy
                ci += 1
            idx = int(self._pos)
            if idx >= diff.size:
                break
            sym = 1 if diff[idx] >= 0 else 0
            bit = 1 if sym == self._last_sym else 0    # NRZI: no change = 1
            self._last_sym = sym
            line = self._hdlc(bit)
            if line:
                out.append(line)
            self._pos += self.sps
        self._pos -= diff.size
        return "".join(out)

    # --- layer 2: HDLC ----------------------------------------------------
    def _hdlc(self, b: int) -> str:
        self._sr = ((self._sr >> 1) | (b << 7)) & 0xFF
        if self._sr == 0x7E:                       # flag: ends one, starts next
            line = self._finish()
            self._collect, self._ones = True, 0
            self._bitbuf = self._nbits = 0
            self._frame = bytearray()
            return line
        if b:
            self._ones += 1
            if self._ones >= 7:                    # 7 ones = abort / idle line
                self._collect = False
                self._frame = bytearray()
                return ""
        else:
            if self._ones == 5:                    # stuffed zero: not data
                self._ones = 0
                return ""
            self._ones = 0
        if self._collect:
            self._bitbuf = (self._bitbuf >> 1) | (b << 7)
            self._nbits += 1
            if self._nbits == 8:
                self._frame.append(self._bitbuf)
                self._bitbuf = self._nbits = 0
                if len(self._frame) > 330:         # AX.25 maximum plus slack
                    self._collect = False
        return ""

    def _finish(self) -> str:
        data = bytes(self._frame)
        self._frame = bytearray()
        if len(data) < MIN_FRAME:
            return ""
        fcs = data[-2] | (data[-1] << 8)
        if crc_x25(data[:-2]) != fcs:
            self.bad_crc += 1
            return ""
        return self._decode(data[:-2])

    # --- layer 3: AX.25 + APRS -------------------------------------------
    def _decode(self, data: bytes) -> str:
        if len(data) < 15:
            return ""
        dst, dssid, _ = _addr(data[0:7])
        src, sssid, _ = _addr(data[7:14])
        path, i = [], 14
        if not data[13] & 0x01:                    # more addresses follow
            while i + 7 <= len(data) and len(path) < 8:
                c, sd, rep = _addr(data[i:i + 7])
                path.append(_call(c, sd) + ("*" if rep else ""))
                last = bool(data[i + 6] & 0x01)
                i += 7
                if last:
                    break
        if i + 2 > len(data):
            return ""
        ctrl, pid = data[i], data[i + 1]
        info = data[i + 2:]
        if ctrl != 0x03 or pid != 0xF0:            # not APRS UI/no-layer-3
            self.frames += 1
            return (f"{_call(src, sssid)}>{_call(dst, dssid)}: "
                    f"AX.25 ctrl {ctrl:02X} pid {pid:02X} ({len(info)} B)\n")
        f = _Frame(_call(src, sssid), _call(dst, dssid), path, info)
        parse_payload(f)
        self.frames += 1
        self.last = f
        st = self.stations.setdefault(f.src, {"first": f.ts, "count": 0})
        st.update(last=f.ts, kind=f.kind, lat=f.lat, lon=f.lon,
                  comment=f.comment or f.text)
        st["count"] += 1
        line = time.strftime("%H:%M:%S ") + f.line() + "\n"
        return line + (f.debug() + "\n" if self.verbose else "")

    def status(self) -> dict:
        return {"frames": self.frames, "bad_crc": self.bad_crc,
                "stations": len(self.stations)}


def locator_to_latlon(loc: str) -> tuple[float, float]:
    """Maidenhead locator -> centre of the square, in decimal degrees.

    Accepts 4, 6 or 8 characters. Precision is worth knowing before putting it
    on the air: 4 chars is a 1°×2° field (about 111×70 km), 6 chars a 2.5'×5'
    square (roughly 4.6 km), 8 chars about 200 m. For a beacon, use at least 6.
    """
    loc = (loc or "").strip().replace(" ", "")
    n = len(loc)
    if n not in (4, 6, 8) or not loc[0:2].isalpha() or not loc[2:4].isdigit():
        raise ValueError("Locator erwartet 4, 6 oder 8 Zeichen, z. B. JO62QM")
    if n >= 6 and not loc[4:6].isalpha():
        raise ValueError("Locator: Zeichen 5-6 müssen Buchstaben sein")
    if n == 8 and not loc[6:8].isdigit():
        raise ValueError("Locator: Zeichen 7-8 müssen Ziffern sein")
    u = loc.upper()
    lon = (ord(u[0]) - 65) * 20.0 - 180.0
    lat = (ord(u[1]) - 65) * 10.0 - 90.0
    lon += int(u[2]) * 2.0
    lat += int(u[3]) * 1.0
    step_lon, step_lat = 2.0, 1.0
    if n >= 6:
        lon += (ord(u[4]) - 65) * (2.0 / 24.0)
        lat += (ord(u[5]) - 65) * (1.0 / 24.0)
        step_lon, step_lat = 2.0 / 24.0, 1.0 / 24.0
    if n == 8:
        lon += int(u[6]) * (step_lon / 10.0)
        lat += int(u[7]) * (step_lat / 10.0)
        step_lon, step_lat = step_lon / 10.0, step_lat / 10.0
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Locator ergibt keine gültige Position")
    return lat + step_lat / 2.0, lon + step_lon / 2.0    # centre, not corner


# ---------------------------------------------------------------------------
# Transmit: own position beacon.
#
# The same three layers in reverse — APRS payload, AX.25 frame with FCS, Bell
# 202 AFSK. Nothing here digipeats or gates; it announces this station and
# nothing else, which is what a beacon is.
# ---------------------------------------------------------------------------

# Experimental/unregistered software uses an APZ tocall. Claiming a registered
# one would misreport which program is on the air.
TOCALL = "APZV71"


def _addr_bytes(call: str, last: bool) -> bytes:
    """One AX.25 address: CALL-SSID, shifted left, with the end-of-path bit."""
    call = call.strip().upper()
    rep = call.endswith("*")
    call = call.rstrip("*")
    base, _, ssid = call.partition("-")
    try:
        n = int(ssid or 0)
    except ValueError:
        n = 0
    if not 0 <= n <= 15 or not 1 <= len(base) <= 6 or not base.isalnum():
        raise ValueError(f"ungültiges Rufzeichen: {call}")
    b = bytearray(ord(c) << 1 for c in base.ljust(6))
    b.append((n << 1) | 0x60 | (1 if last else 0) | (0x80 if rep else 0))
    return bytes(b)


def build_frame(src: str, dst: str, path: list[str], info: bytes) -> bytes:
    """A UI frame (control 0x03, PID 0xF0) with its FCS appended."""
    f = bytearray(_addr_bytes(dst, False))
    f += _addr_bytes(src, not path)
    for i, p in enumerate(path):
        f += _addr_bytes(p, i == len(path) - 1)
    f += b"\x03\xf0" + info
    crc = crc_x25(bytes(f))
    return bytes(f) + bytes([crc & 0xFF, crc >> 8])


def position_report(lat: float, lon: float, symbol: str = "/-",
                    comment: str = "") -> bytes:
    """Uncompressed position without timestamp ('!'), the plainest thing that
    every APRS client understands. Symbol is table + code, e.g. '/-' house."""
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Position außerhalb des gültigen Bereichs")
    la, lo = abs(lat), abs(lon)
    lad, lam = int(la), (la - int(la)) * 60.0
    lod, lom = int(lo), (lo - int(lo)) * 60.0
    tab = symbol[0] if symbol else "/"
    code = symbol[1] if len(symbol) > 1 else "-"
    body = (f"{lad:02d}{lam:05.2f}{'N' if lat >= 0 else 'S'}{tab}"
            f"{lod:03d}{lom:05.2f}{'E' if lon >= 0 else 'W'}{code}")
    return ("!" + body + comment.strip()).encode("ascii", "replace")


def modulate(frame: bytes, fs: int = 48000, txdelay_ms: int = 300,
             tail_ms: int = 60, level: float = 0.5) -> np.ndarray:
    """Bell 202 AFSK for one frame, with a flag preamble.

    TXDELAY matters on the air: the transmitter needs time to come up and the
    receiving TNC needs flags to lock its clock onto before the frame starts.
    300 ms is the usual value; too short and the first bytes are lost, which
    looks exactly like a decoder fault at the far end.
    """
    bits: list[int] = []

    def push(data: bytes, stuff: bool) -> None:
        ones = 0
        for byte in data:
            for k in range(8):                     # AX.25 is LSB first
                b = (byte >> k) & 1
                bits.append(b)
                if stuff:
                    ones = ones + 1 if b else 0
                    if ones == 5:                  # never six ones inside data
                        bits.append(0)
                        ones = 0

    flags = max(1, int(txdelay_ms * BAUD / 8000))  # 8 bits per flag
    push(b"\x7e" * flags, False)
    push(frame, True)
    push(b"\x7e" * max(1, int(tail_ms * BAUD / 8000)), False)

    # NRZI: 0 flips the tone, 1 holds it
    sym = 1
    freqs = np.empty(len(bits), dtype=np.float64)
    for i, b in enumerate(bits):
        if not b:
            sym ^= 1
        freqs[i] = MARK_HZ if sym else SPACE_HZ
    # continuous phase: a phase jump at a symbol boundary splatters
    n = int(round(fs / BAUD))
    inc = 2.0 * np.pi * np.repeat(freqs, n) / fs
    phase = np.cumsum(inc)
    return np.clip(np.sin(phase) * level * 32767, -32768, 32767).astype(np.int16)


def beacon(call: str, lat: float, lon: float, symbol: str = "/-",
           comment: str = "", path: Optional[list[str]] = None,
           fs: int = 48000, txdelay_ms: int = 300) -> tuple[np.ndarray, str]:
    """Audio for one position beacon, plus the line describing what was sent."""
    path = [p for p in (path or []) if p.strip()]
    info = position_report(lat, lon, symbol, comment)
    frame = build_frame(call, TOCALL, path, info)
    pcm = modulate(frame, fs, txdelay_ms)
    head = f"{call.upper()}>{TOCALL}" + ("," + ",".join(path) if path else "")
    text = (f"{head}: {_dm(lat, True)} {_dm(lon, False)}"
            + (f' "{comment.strip()}"' if comment.strip() else ""))
    return pcm, text

"""Low-level driver for the Kenwood TM-V71(A) PC command protocol.

The radio speaks a simple ASCII request/response protocol over the serial port
(programming cable / FTDI bridge). Every command and every reply is terminated
by a carriage return (0x0D). On an unrecognised or rejected command the radio
answers with a single ``?``.

This module owns the serial port directly (we do *not* go through hamlib —
hamlib's TM-V71 backends are unreliable, and the documented protocol gives us
full access including per-channel memory programming via ME/MN).

Protocol reference: https://github.com/LA3QMA/TM-V71_TM-D710-Kenwood

The driver is synchronous and thread-safe (a single lock serialises access —
the radio cannot process concurrent commands). FastAPI calls it through
``asyncio.to_thread`` so the event loop is never blocked.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional

import serial


# --- Lookup tables (indices used by the FO/ME field format) -----------------

# Tuning step sizes in Hz, indexed by the radio's step field.
STEP_HZ = [5000, 6250, 28330, 10000, 12500, 15000, 20000, 25000,
           30000, 50000, 100000]


def align_step_index(freq_hz: int, preferred: int = 0) -> int:
    """Pick a ``STEP_HZ`` index whose step evenly divides ``freq_hz``.

    The TM-V71 rejects an ``ME``/``FO`` write whose frequency is not an integer
    multiple of the channel's tuning step (e.g. 123.780 MHz at a 12.5 kHz step
    is refused with ``?``). Keep the caller's ``preferred`` step when it already
    divides the frequency; otherwise return the *finest* step that does, so the
    exact frequency is preserved. Falls back to ``preferred`` if none fits.
    """
    if 0 <= preferred < len(STEP_HZ) and STEP_HZ[preferred] \
            and freq_hz % STEP_HZ[preferred] == 0:
        return preferred
    for i in sorted(range(len(STEP_HZ)), key=lambda i: STEP_HZ[i]):
        if STEP_HZ[i] and freq_hz % STEP_HZ[i] == 0:
            return i
    return preferred

# Standard Kenwood CTCSS tone table (Hz), 1-based as the radio reports it.
CTCSS_TONES = [
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9,
    186.2, 192.8, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8,
    250.3, 254.1,
]

# Standard DCS code table, 1-based as the radio reports it.
DCS_CODES = [
    23, 25, 26, 31, 32, 36, 43, 47, 51, 53, 54, 65, 71, 72, 73, 74,
    114, 115, 116, 122, 125, 131, 132, 134, 143, 145, 152, 155, 156, 162,
    165, 172, 174, 205, 212, 223, 225, 226, 243, 244, 245, 246, 251, 252,
    255, 261, 263, 265, 266, 271, 274, 306, 311, 315, 325, 331, 332, 343,
    346, 351, 356, 364, 365, 371, 411, 412, 413, 423, 431, 432, 445, 446,
    452, 454, 455, 462, 464, 465, 466, 503, 506, 516, 523, 526, 532, 546,
    565, 606, 612, 624, 627, 631, 632, 654, 662, 664, 703, 712, 723, 731,
    732, 734, 743, 754,
]

SHIFT_NONE, SHIFT_UP, SHIFT_DOWN = 0, 1, 2
MODE_FM, MODE_NFM, MODE_AM = 0, 1, 2  # data field "mode" in FO/ME

# Power levels: the radio reports 0=High, 1=Medium, 2=Low.
POWER_HIGH, POWER_MEDIUM, POWER_LOW = 0, 1, 2

BAND_A, BAND_B = 0, 1

# Per-band operating mode (VM command).
VFO_MODE, MR_MODE, CALL_MODE = 0, 1, 2

# External data band (MU menu, parameter 38 -> 0-based index 37):
#   0 = Band A, 1 = Band B, 2 = TX A / RX B, 3 = TX B / RX A
DATA_BAND_IDX = 37
# data band value -> the band whose RX audio reaches the data connector
DATA_BAND_RX = {0: 0, 1: 1, 2: 1, 3: 0}

# 1750 Hz tone hold (MU menu, parameter 24 -> 0-based index 23): radio menu 402.
TONE_1750_IDX = 23


class TMV71Error(Exception):
    """Raised when the radio rejects a command (``?``) or times out."""


@dataclass
class ChannelData:
    """Decoded FO (VFO) or ME (memory) frequency object.

    Field layout (FO has 13 fields, ME adds tx_freq + lockout for split/skip):
        index, rx_freq, step, shift, reverse, tone_on, ctcss_on, dcs_on,
        tone_idx, ctcss_idx, dcs_idx, offset, mode[, tx_freq, lockout, ...]
    """
    index: int            # band number (FO) or channel number (ME)
    rx_freq: int          # Hz
    step: int             # raw step index
    shift: int            # 0 none / 1 up / 2 down
    reverse: int
    tone_on: int
    ctcss_on: int
    dcs_on: int
    tone_idx: int
    ctcss_idx: int
    dcs_idx: int
    offset: int           # Hz
    mode: int             # 0 FM / 1 NFM / 2 AM
    tx_freq: Optional[int] = None   # ME only (odd split); 0 = none
    lockout: Optional[int] = None   # ME only (scan skip / lockout)

    # -- convenience views -------------------------------------------------
    @property
    def step_hz(self) -> int:
        return STEP_HZ[self.step] if 0 <= self.step < len(STEP_HZ) else 0

    @property
    def tone_hz(self) -> Optional[float]:
        i = self.tone_idx - 1
        return CTCSS_TONES[i] if 0 <= i < len(CTCSS_TONES) else None

    @property
    def ctcss_hz(self) -> Optional[float]:
        i = self.ctcss_idx - 1
        return CTCSS_TONES[i] if 0 <= i < len(CTCSS_TONES) else None

    @property
    def dcs_code(self) -> Optional[int]:
        i = self.dcs_idx
        return DCS_CODES[i] if 0 <= i < len(DCS_CODES) else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(step_hz=self.step_hz, tone_hz=self.tone_hz,
                 ctcss_hz=self.ctcss_hz, dcs_code=self.dcs_code)
        return d


def _parse_freq_object(fields: list[str], is_memory: bool) -> ChannelData:
    """Parse the comma-separated fields of an FO or ME reply (sans command)."""
    f = fields
    return ChannelData(
        index=int(f[0]),
        rx_freq=int(f[1]),
        step=int(f[2]),
        shift=int(f[3]),
        reverse=int(f[4]),
        tone_on=int(f[5]),
        ctcss_on=int(f[6]),
        dcs_on=int(f[7]),
        tone_idx=int(f[8]),
        ctcss_idx=int(f[9]),
        dcs_idx=int(f[10]),
        offset=int(f[11]),
        mode=int(f[12]),
        tx_freq=int(f[13]) if is_memory and len(f) > 13 else None,
        lockout=int(f[14]) if is_memory and len(f) > 14 else None,
    )


class TMV71:
    """Thread-safe synchronous driver for the Kenwood TM-V71(A)."""

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 57600,
                 timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.RLock()

    # -- connection --------------------------------------------------------
    def open(self) -> None:
        with self._lock:
            if self._serial and self._serial.is_open:
                return
            self._serial = serial.Serial(
                self.port, self.baudrate, timeout=self.timeout,
                write_timeout=self.timeout,
            )
            # Warm-up sync: the radio answers '?' to the very first command
            # after the port opens. Send a bare CR and discard the reply so the
            # next real command (e.g. ID) reads cleanly.
            try:
                self._serial.reset_input_buffer()
                self._serial.write(b"\r")
                self._serial.flush()
                time.sleep(0.15)
                self._serial.reset_input_buffer()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.close()
            self._serial = None

    @property
    def is_open(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    # -- raw transaction ---------------------------------------------------
    def transact(self, command: str, allow_n: bool = False) -> str:
        """Send one command and return the raw reply (without trailing CR).

        ``allow_n`` keeps an ``N`` reply instead of raising — used by action
        commands (e.g. DT) that acknowledge with ``N`` and return no data.

        Raises ``TMV71Error`` on ``?`` (rejected) or on timeout/empty reply.
        """
        with self._lock:
            if not self.is_open:
                self.open()
            ser = self._serial
            assert ser is not None
            ser.reset_input_buffer()
            ser.write(command.encode("ascii") + b"\r")
            ser.flush()
            raw = bytearray()
            deadline = time.monotonic() + max(self.timeout, 0.5) * 3
            while time.monotonic() < deadline:
                byte = ser.read(1)
                if not byte:
                    continue
                if byte == b"\r":
                    break
                raw += byte
            else:
                raise TMV71Error(f"timeout waiting for reply to {command!r}")
            reply = raw.decode("ascii", errors="replace")
            # '?' = command rejected; 'N' = negative / no data (e.g. an empty
            # memory channel). Both mean "no usable reply".
            if reply == "?" or (reply == "N" and not allow_n):
                raise TMV71Error(f"radio returned {reply!r} for {command!r}")
            return reply

    def _fields(self, reply: str) -> list[str]:
        """Split a reply like 'FO 0,0145...' into its comma fields.

        The reply begins with the 2-letter command and a space before the
        first field; we drop that prefix and return the remaining CSV fields.
        """
        head, _, rest = reply.partition(" ")
        return rest.split(",") if rest else []

    # -- identity / status -------------------------------------------------
    def get_model(self) -> str:
        reply = self.transact("ID")          # e.g. 'ID TM-V71'
        return reply.split(" ", 1)[1] if " " in reply else reply

    def get_firmware(self, unit: int = 0) -> Optional[str]:
        """FV p1 -> firmware version string.

        The reply looks like ``FV 0,1.00,2.10,A,1`` — the first field is the
        queried unit, the rest are the version fields, which we join back.
        """
        try:
            f = self._fields(self.transact(f"FV {unit}"))
        except TMV71Error:
            return None
        return ",".join(f[1:]) if len(f) > 1 else None

    def get_serial_number(self) -> Optional[str]:
        """AE -> serial number (+ model). Returns the raw value string."""
        try:
            reply = self.transact("AE")
        except TMV71Error:
            return None
        return reply.split(" ", 1)[1] if " " in reply else reply

    def get_radio_type(self) -> Optional[list[str]]:
        """TY -> radio type fields (market, mars, max-tx, crossband, skycommand)."""
        try:
            return self._fields(self.transact("TY"))
        except TMV71Error:
            return None

    # -- DTMF memory (DM): 10 channels (0-9), up to 16 digits each ----------
    def get_dtmf_memory(self, channel: int) -> str:
        """DM p1 -> stored DTMF code for channel 0-9 (trailing spaces stripped)."""
        try:
            f = self._fields(self.transact(f"DM {channel}"))
        except TMV71Error:
            return ""
        return f[1].rstrip() if len(f) > 1 else ""

    def set_dtmf_memory(self, channel: int, code: str) -> None:
        """DM p1,p2 -> store a DTMF code. Unused digits are padded with spaces."""
        code = code.strip()[:16].ljust(16)
        self.transact(f"DM {channel},{code}")

    def send_dtmf_digit(self, digit: str) -> None:
        """DT 0,p2 -> emit one DTMF tone. Radio must already be transmitting.
        Mapping: 0-9/A-D pass through, '*' -> E, '#' -> F."""
        p2 = {"*": "E", "#": "F"}.get(digit, digit.upper())
        if p2 in "0123456789ABCDEF":
            self.transact(f"DT 0,{p2}", allow_n=True)   # DT acks with 'N'

    # -- band / control band ----------------------------------------------
    def get_band_status(self) -> tuple[int, int]:
        """Return (control_band, ptt_band) from the BC command."""
        f = self._fields(self.transact("BC"))
        return int(f[0]), int(f[1])

    def set_control_band(self, control: int, ptt: Optional[int] = None) -> None:
        if ptt is None:
            ptt = control
        self.transact(f"BC {control},{ptt}")

    # -- menu (MU): external data band, 1750 Hz hold, ... ------------------
    def get_menu(self) -> list[str]:
        """All menu settings (MU) as a list of field strings (TM-V71: 42)."""
        return self._fields(self.transact("MU"))

    def get_menu_item(self, idx: int) -> int:
        """Read a single MU field by 0-based index."""
        m = self.get_menu()
        return int(m[idx]) if 0 <= idx < len(m) else 0

    def set_menu_item(self, idx: int, value: int) -> None:
        """Change one MU field by 0-based index, writing the menu back verbatim."""
        m = self.get_menu()
        if not 0 <= idx < len(m):
            raise TMV71Error(f"menu index {idx} out of range (len {len(m)})")
        m[idx] = str(int(value))
        self.transact("MU " + ",".join(m))

    def get_data_band(self) -> int:
        """External data band: 0=A, 1=B, 2=TX A/RX B, 3=TX B/RX A."""
        return self.get_menu_item(DATA_BAND_IDX)

    def set_data_band(self, band: int) -> None:
        """Change only the data-band field and write the menu back verbatim."""
        self.set_menu_item(DATA_BAND_IDX, band)

    def get_tone_1750(self) -> bool:
        """1750 Hz tone hold (menu 402): off/on."""
        return bool(self.get_menu_item(TONE_1750_IDX))

    def set_tone_1750(self, on: bool) -> None:
        """Toggle the 1750 Hz tone hold (menu 402)."""
        self.set_menu_item(TONE_1750_IDX, 1 if on else 0)

    # -- transmit power (PC) ----------------------------------------------
    def get_power(self, band: int) -> int:
        """PC <band> -> 0 High (50W) / 1 Mid (10W) / 2 Low (5W)."""
        f = self._fields(self.transact(f"PC {band}"))
        return int(f[1]) if len(f) > 1 else POWER_HIGH

    def set_power(self, band: int, level: int) -> None:
        self.transact(f"PC {band},{level}")

    # -- dual / single band display (DL) ----------------------------------
    def get_dl(self) -> int:
        """DL -> 0 dual band / 1 single band."""
        f = self._fields(self.transact("DL"))
        return int(f[0]) if f else 0

    def set_dl(self, single: bool) -> None:
        self.transact(f"DL {1 if single else 0}")

    # -- per-band operating mode (VFO / memory / call) --------------------
    def get_band_mode(self, band: int) -> int:
        """VM <band> -> 0 VFO / 1 memory / 2 call."""
        f = self._fields(self.transact(f"VM {band}"))
        return int(f[1]) if len(f) > 1 else VFO_MODE

    def set_band_mode(self, band: int, mode: int) -> None:
        self.transact(f"VM {band},{mode}")

    # -- VFO frequency object (live) --------------------------------------
    def get_vfo(self, band: int) -> ChannelData:
        reply = self.transact(f"FO {band}")
        return _parse_freq_object(self._fields(reply), is_memory=False)

    def set_vfo(self, ch: ChannelData) -> None:
        self.transact(self._format_freq_object("FO", ch, is_memory=False))

    def set_vfo_frequency(self, band: int, freq_hz: int,
                          ensure_vfo: bool = True) -> ChannelData:
        """Change only the RX frequency of a band's VFO and return the result.

        ``FO`` writes only take effect on the live display when the band is in
        VFO mode, so by default we switch the band to VFO mode first.
        """
        if ensure_vfo and self.get_band_mode(band) != VFO_MODE:
            self.set_band_mode(band, VFO_MODE)
        ch = self.get_vfo(band)
        # The radio refuses an FO write whose frequency isn't a multiple of the
        # tuning step (e.g. a HackRF waterfall double-click lands on an arbitrary
        # 100 Hz value). Keep the exact frequency when some step divides it,
        # otherwise snap it to the current step grid so the write is accepted.
        step_idx = align_step_index(freq_hz, ch.step)
        step = STEP_HZ[step_idx] if 0 <= step_idx < len(STEP_HZ) else 0
        if step and freq_hz % step:
            freq_hz = round(freq_hz / step) * step
        ch.rx_freq = freq_hz
        ch.step = step_idx
        self.set_vfo(ch)
        return self.get_vfo(band)

    # -- memory recall -----------------------------------------------------
    def recall_memory(self, band: int, channel: int) -> None:
        """Put a band into memory mode and select a stored channel."""
        self.set_band_mode(band, MR_MODE)
        self.transact(f"MR {band},{channel:03d}")

    # -- memory channels ---------------------------------------------------
    def get_memory(self, channel: int) -> Optional[ChannelData]:
        """Return decoded memory channel, or None if the channel is empty."""
        try:
            reply = self.transact(f"ME {channel:03d}")
        except TMV71Error:
            return None       # empty channels answer with '?' (N)
        return _parse_freq_object(self._fields(reply), is_memory=True)

    def set_memory(self, ch: ChannelData) -> None:
        self.transact(self._format_freq_object("ME", ch, is_memory=True))

    def get_memory_name(self, channel: int) -> str:
        try:
            f = self._fields(self.transact(f"MN {channel:03d}"))
        except TMV71Error:
            return ""
        return f[1] if len(f) > 1 else ""

    def set_memory_name(self, channel: int, name: str) -> None:
        self.transact(f"MN {channel:03d},{name[:8]}")

    def delete_memory(self, channel: int) -> None:
        # Clearing a channel: ME with an empty spec (single trailing comma)
        # deletes it (same approach CHIRP uses). Radio echoes 'ME <ch>'.
        self.transact(f"ME {channel:03d},")

    # -- microphone UP / DOWN keys (step control band one step) ------------
    def mic_up(self) -> None:
        self.transact("UP")

    def mic_down(self) -> None:
        self.transact("DW")

    # -- PTT / transmit ----------------------------------------------------
    def set_ptt(self, transmit: bool) -> None:
        self.transact("TX" if transmit else "RX")

    def get_squelch_open(self, band: int) -> bool:
        """BY <band> -> busy status; field 1 == '1' means squelch open."""
        f = self._fields(self.transact(f"BY {band}"))
        return len(f) > 1 and f[1] == "1"

    def get_squelch_level(self, band: int) -> int:
        """SQ <band> -> configured squelch threshold (hex 00..1F)."""
        f = self._fields(self.transact(f"SQ {band}"))
        return int(f[0], 16) if f else 0

    def set_squelch_level(self, band: int, level: int) -> None:
        """SQ <band>,<level> -> set squelch threshold (0..31 -> 00..1F)."""
        level = max(0, min(31, int(level)))
        self.transact(f"SQ {band},{level:02X}")

    def get_smeter(self, band: int) -> Optional[int]:
        """Signal strength.

        The TM-V71(A) does NOT implement the ``SM`` query that the TM-D710 has;
        it always answers ``?``. We therefore return None and fall back to the
        BY (busy / squelch-open) indicator in the status layer.
        """
        try:
            f = self._fields(self.transact(f"SM {band}"))
            return int(f[1]) if len(f) > 1 else None
        except TMV71Error:
            return None

    # -- formatting --------------------------------------------------------
    @staticmethod
    def _format_freq_object(cmd: str, c: ChannelData, is_memory: bool) -> str:
        parts = [
            f"{c.index:03d}" if is_memory else str(c.index),
            f"{c.rx_freq:010d}",
            str(c.step), str(c.shift), str(c.reverse),
            str(c.tone_on), str(c.ctcss_on), str(c.dcs_on),
            f"{c.tone_idx:02d}", f"{c.ctcss_idx:02d}", f"{c.dcs_idx:03d}",
            f"{c.offset:08d}", str(c.mode),
        ]
        if is_memory:
            parts += [f"{(c.tx_freq or 0):010d}", str(c.lockout or 0), "0"]
        return f"{cmd} " + ",".join(parts)

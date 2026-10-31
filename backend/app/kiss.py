"""KISS over TCP: the software TNC for external APRS and packet programs.

The TM-V71 has no TNC of its own; TM-V71 Remote's APRS modem (aprs.py) is one in
software. This server hands it to programs that speak KISS over TCP — Graywolf
(a `tcp-client` interface in TNC mode), YAAC, Xastir, APRSIS32, Pat — the way
Direwolf does on port 8001:

* every frame the modem receives with a good CRC goes to every client
* every data frame a client sends is queued for transmission; the service
  (main.py) waits for a clear channel, modulates and keys the radio

Only KISS port 0 exists. TXDELAY (command 1) is honoured; persistence and slot
time come from here, the other parameter commands are accepted and ignored.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_DATA, CMD_TXDELAY = 0x00, 0x01
MIN_FRAME = 15            # two addresses + control
MAX_FRAME = 330           # AX.25 maximum, as the decoder accepts it
QUEUE_MAX = 16            # frames waiting to go out; more are dropped
MAX_BUFFER = 64 * 1024    # unread bytes per client before it counts as dead

log = logging.getLogger("tmv71.kiss")


def escape(data: bytes) -> bytes:
    return (bytes(data).replace(bytes([FESC]), bytes([FESC, TFESC]))
            .replace(bytes([FEND]), bytes([FESC, TFEND])))


def encode(frame: bytes, port: int = 0) -> bytes:
    """One AX.25 frame (no FCS) as a KISS data frame."""
    return bytes([FEND, (port & 0x0F) << 4 | CMD_DATA]) + escape(frame) + bytes([FEND])


class Deframer:
    """Byte stream -> (port, command, payload) tuples."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._esc = False
        self._in = False

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        out = []
        for b in data:
            if b == FEND:
                if self._in and self._buf:
                    t = self._buf[0]
                    out.append((t >> 4, t & 0x0F, bytes(self._buf[1:])))
                self._buf = bytearray()
                self._esc = False
                self._in = True
                continue
            if not self._in:
                continue
            if self._esc:
                self._esc = False
                b = {TFEND: FEND, TFESC: FESC}.get(b)
                if b is None:              # protocol error: drop the frame
                    self._buf = bytearray()
                    self._in = False
                    continue
            elif b == FESC:
                self._esc = True
                continue
            self._buf.append(b)
            if len(self._buf) > MAX_FRAME + 64:
                self._buf = bytearray()
                self._in = False
        return out


class KissServer:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(QUEUE_MAX)
        self.txdelay_ms = 300
        self.allow_tx = True
        self.host = "0.0.0.0"
        self.port = 8001
        self.error: Optional[str] = None
        self.rx = 0                 # frames passed to the clients
        self.tx = 0                 # frames transmitted
        self.dropped = 0            # frames from clients not transmitted
        self._server: Optional[asyncio.base_events.Server] = None
        self._clients: dict[asyncio.StreamWriter, dict] = {}

    # --- server ----------------------------------------------------------
    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def active(self) -> bool:
        """Someone is connected: the modem has to decode for them."""
        return bool(self._clients)

    async def start(self, host: str, port: int) -> None:
        await self.stop()
        self.host, self.port = host, port
        try:
            self._server = await asyncio.start_server(self._handle, host, port)
            self.error = None
            log.info("KISS TCP server on %s:%d", host, port)
        except OSError as e:
            self._server = None
            self.error = f"port {port}: {e.strerror or e}"
            log.warning("KISS TCP server: %s", self.error)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        for w in list(self._clients):
            w.close()
        self._clients.clear()
        while not self.queue.empty():
            self.queue.get_nowait()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        info = {"addr": f"{peer[0]}:{peer[1]}", "since": time.time(), "in": 0}
        self._clients[writer] = info
        log.info("KISS client %s connected", info["addr"])
        de = Deframer()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for port, cmd, payload in de.feed(data):
                    self._command(port, cmd, payload, info)
        except (ConnectionError, OSError):
            pass
        finally:
            self._clients.pop(writer, None)
            writer.close()
            log.info("KISS client %s gone", info["addr"])

    def _command(self, port: int, cmd: int, payload: bytes, info: dict) -> None:
        if port == 0x0F and cmd == 0x0F:   # 0xFF: "leave KISS mode" — meaningless here
            return
        if port != 0:
            return
        if cmd == CMD_TXDELAY and payload:
            self.txdelay_ms = max(100, min(1000, payload[0] * 10))
            return
        if cmd != CMD_DATA:
            return
        info["in"] += 1
        if not self.allow_tx or not MIN_FRAME <= len(payload) <= MAX_FRAME:
            self.dropped += 1
            return
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped += 1

    # --- frames out to the clients ---------------------------------------
    def send(self, frame: bytes) -> None:
        if not self._clients:
            return
        data = encode(frame)
        self.rx += 1
        for w, info in list(self._clients.items()):
            try:
                if w.transport.get_write_buffer_size() > MAX_BUFFER:
                    raise ConnectionError("client does not read")
                w.write(data)
            except Exception:  # noqa: BLE001
                self._clients.pop(w, None)
                w.close()
                log.info("KISS client %s dropped", info["addr"])

    def status(self) -> dict:
        return {"listening": self.listening, "port": self.port, "error": self.error,
                "allow_tx": self.allow_tx, "txdelay_ms": self.txdelay_ms,
                "clients": [{"addr": i["addr"], "since": i["since"], "in": i["in"]}
                            for i in self._clients.values()],
                "rx": self.rx, "tx": self.tx, "dropped": self.dropped,
                "queued": self.queue.qsize()}

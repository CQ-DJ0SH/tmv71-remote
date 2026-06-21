"""Async service layer around the synchronous TMV71 driver.

All radio access goes through ``asyncio.to_thread`` so the FastAPI event loop
is never blocked. The driver itself serialises concurrent access with a lock,
so commands issued by REST handlers and the background poller interleave safely.

A background task polls the radio at ``settings.poll_interval`` and broadcasts a
``RadioStatus`` snapshot to every connected WebSocket client.
"""
from __future__ import annotations

import asyncio
import logging

from . import config
from .config import settings
from .models import BandState, DtmfMemory, RadioInfo, RadioStatus
from .state import ConnectionManager
from .tmv71 import (TMV71, TMV71Error, ChannelData, MR_MODE, VFO_MODE,
                    DATA_BAND_RX, DATA_BAND_IDX, MODE_AM,
                    align_step_index)

# Reserved memory channel that backs the Band-A air band (see toggle_airband_a).
AIRBAND_CH = 997
AIRBAND_MIN_HZ, AIRBAND_MAX_HZ = 118_000_000, 136_975_000

log = logging.getLogger("tmv71")

# Band-scan ranges: key -> (start_hz, end_hz, step_hz, mode, scratch_ch, via_memory).
# mode 0 = FM, 2 = AM. scratch_ch is a reserved memory channel.
#  - via_memory False: prime the VFO into the band once via scratch_ch (one flash
#    write, never deleted), then sweep by retuning the VFO — flash-friendly.
#  - via_memory True: sweep by rewriting + recalling scratch_ch for every step.
#    This is the only way to scan the air band (an RX-only AM segment the VFO
#    can't be moved into over CAT), at the cost of one ME flash write per step.
SCAN_BANDS = {
    "2m":   (144_000_000, 145_995_000, 12_500, 0, 998, False),
    "70cm": (430_000_000, 439_975_000, 25_000, 0, 999, False),
    "air":  (118_000_000, 136_975_000, 25_000, 2, 997, True),
}


class RadioService:
    def __init__(self, manager: ConnectionManager) -> None:
        self.radio = TMV71(settings.serial_port, settings.serial_baud,
                           settings.serial_timeout)
        self.manager = manager
        self.model: str | None = None
        self.transmitting: bool = False
        self.data_band: Optional[int] = None    # cached MU data band (lazy)
        # 1750 Hz tone call is generated in software on the mic path (the radio's
        # menu-402 hold does not key a tone on this unit), so it is *not* read
        # from the radio menu — it tracks the software tone generator instead.
        self.tone_1750: bool = False
        # set by main.py: pushes the on/off state to the audio tone generator
        self.tone_1750_sink = None
        self._status: RadioStatus = RadioStatus(connected=False)
        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Optional hook wired up by the audio bridge for PTT coupling.
        self.on_ptt = None  # async callable(transmit: bool) -> None
        # Optional hook run while still keyed on release (roger beep).
        self.on_unkey = None  # async callable() -> None
        # Band scan: level_provider() returns the live RX AF level in dBFS.
        self.level_provider = None
        self._scanning = False
        self._scan_cancel = False
        self._scan_task: asyncio.Task | None = None
        self._scan: dict = {"running": False, "band": None, "total": 0,
                            "index": 0, "points": [], "done": False,
                            "error": None}

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        try:
            await asyncio.to_thread(self.radio.open)
            self.model = await asyncio.to_thread(self.radio.get_model)
            log.info("Connected to radio: %s", self.model)
            await self._restore_squelch()
        except Exception as exc:  # noqa: BLE001
            log.error("Could not open radio: %s", exc)
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _restore_squelch(self) -> None:
        """Re-apply the persisted squelch level to each band (the radio loses it
        on a power cycle). No-op for bands with no saved value."""
        from .config import settings
        for band, level in ((0, settings.squelch_a), (1, settings.squelch_b)):
            if level is None:
                continue
            try:
                await asyncio.to_thread(self.radio.set_squelch_level, band, level)
            except Exception as exc:  # noqa: BLE001
                log.warning("restore squelch band %d failed: %s", band, exc)

    async def stop(self) -> None:
        self._stop.set()
        if self._poll_task:
            await asyncio.gather(self._poll_task, return_exceptions=True)
        await asyncio.to_thread(self.radio.close)

    # -- status ------------------------------------------------------------
    @property
    def status(self) -> RadioStatus:
        return self._status

    def _read_band(self, band: int) -> BandState:
        r = self.radio
        mode = r.get_band_mode(band)
        ch = r.get_vfo(band)
        mem_ch = mem_name = None
        if mode == MR_MODE:
            # In memory mode FO mirrors the active channel's data.
            try:
                f = r._fields(r.transact(f"MR {band}"))
                mem_ch = int(f[1]) if len(f) > 1 else None
                if mem_ch is not None:
                    mem_name = r.get_memory_name(mem_ch)
            except TMV71Error:
                pass
        try:
            power = r.get_power(band)
        except TMV71Error:
            power = None
        try:
            sql = r.get_squelch_level(band)
        except TMV71Error:
            sql = None
        return BandState(
            band=band, mode=mode, rx_freq=ch.rx_freq,
            shift=ch.shift, offset=ch.offset, fm_mode=ch.mode,
            tone_on=bool(ch.tone_on), ctcss_on=bool(ch.ctcss_on),
            dcs_on=bool(ch.dcs_on), tone_hz=ch.tone_hz, ctcss_hz=ch.ctcss_hz,
            dcs_code=ch.dcs_code, step_hz=ch.step_hz, power=power,
            squelch_level=sql, squelch_open=r.get_squelch_open(band),
            memory_channel=mem_ch, memory_name=mem_name,
        )

    def _read_status(self) -> RadioStatus:
        """Synchronous full read — runs in a worker thread."""
        if not self.radio.is_open:
            self.radio.open()
        control, ptt = self.radio.get_band_status()
        try:
            single = bool(self.radio.get_dl())
        except TMV71Error:
            single = False
        bands = [self._read_band(0), self._read_band(1)]
        if self.data_band is None:
            # read the MU menu once for the data band, then cache. (1750 Hz is
            # software-generated and intentionally not sourced from the menu.)
            try:
                menu = self.radio.get_menu()
                self.data_band = int(menu[DATA_BAND_IDX])
            except (TMV71Error, ValueError, IndexError):
                self.data_band = 0
        return RadioStatus(
            connected=True, model=self.model,
            control_band=control, ptt_band=ptt,
            data_band=self.data_band,
            audio_band=DATA_BAND_RX.get(self.data_band, 0),
            tone_1750=self.tone_1750,
            transmitting=self.transmitting, single_band=single, bands=bands,
        )

    async def refresh(self) -> RadioStatus:
        prev = self._status.connected
        try:
            self._status = await asyncio.to_thread(self._read_status)
        except Exception as exc:  # noqa: BLE001
            self._status = RadioStatus(connected=False, error=str(exc))
        # radio just came (back) online — re-apply the persisted squelch, which
        # the rig loses on a power cycle (GPIO/manual power-on, serial recovery)
        if self._status.connected and not prev:
            await self._restore_squelch()
        return self._status

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            if not self._scanning:          # the scan owns the serial link
                await self.refresh()
                await self.manager.broadcast(self._status.model_dump())
            try:
                await asyncio.wait_for(self._stop.wait(), settings.poll_interval)
            except asyncio.TimeoutError:
                pass

    # -- commands ----------------------------------------------------------
    def _in_airband(self, band: int) -> bool:
        """True while ``band`` sits on *any* memory channel inside the air band.

        That covers both the 997 scratch entry and the M50+ air-band presets, so
        tuning the dial never falls through to the VFO path (the VFO can't be
        moved into the air band over CAT and gets stuck there if forced)."""
        bs = self._status.bands[band] if band < len(self._status.bands) else None
        return bool(bs and bs.mode == MR_MODE
                    and AIRBAND_MIN_HZ <= (bs.rx_freq or 0) <= AIRBAND_MAX_HZ + 25_000)

    def _set_airband_freq(self, freq_hz: int) -> None:
        """Retune the air band by rewriting the 997 scratch channel and recalling it.

        The VFO can't be moved into the air band over CAT, so a normal ``FO``
        write would be rejected (and would strand Band A in the air band).
        Instead we always tune via the reserved 997 channel — recalling a preset
        (M50+) leaves it untouched; turning the dial lifts onto 997 at the new
        frequency. Either way Band A stays in memory mode inside the air band.
        """
        if not (AIRBAND_MIN_HZ <= freq_hz <= AIRBAND_MAX_HZ):
            raise TMV71Error(
                f"{freq_hz / 1e6:.3f} MHz is outside the air band "
                f"({AIRBAND_MIN_HZ // 10**6}–{AIRBAND_MAX_HZ / 1e6:.3f} MHz)")
        cur = self.radio.get_memory(AIRBAND_CH)
        if cur is None:
            cur = ChannelData(index=AIRBAND_CH, rx_freq=freq_hz, step=7, shift=0,
                              reverse=0, tone_on=0, ctcss_on=0, dcs_on=0,
                              tone_idx=1, ctcss_idx=1, dcs_idx=0, offset=0,
                              mode=MODE_AM, tx_freq=0, lockout=0)
        else:
            cur.rx_freq = freq_hz
            cur.mode = MODE_AM
        cur.step = align_step_index(freq_hz, cur.step)
        self.radio.set_memory(cur)
        self.radio.recall_memory(0, AIRBAND_CH)

    async def set_frequency(self, band: int, freq_hz: int) -> None:
        if band == 0 and self._in_airband(0):
            await asyncio.to_thread(self._set_airband_freq, freq_hz)
        else:
            await asyncio.to_thread(self.radio.set_vfo_frequency, band, freq_hz)
        await self.refresh()

    async def set_band_mode(self, band: int, mode: int) -> None:
        await asyncio.to_thread(self.radio.set_band_mode, band, mode)
        await self.refresh()

    def _update_vfo(self, u) -> None:
        # FO writes only take effect on the live display in VFO mode.
        if self.radio.get_band_mode(u.band) != VFO_MODE:
            self.radio.set_band_mode(u.band, VFO_MODE)
        ch = self.radio.get_vfo(u.band)
        if u.shift is not None: ch.shift = u.shift
        if u.offset is not None: ch.offset = u.offset
        if u.fm_mode is not None: ch.mode = u.fm_mode
        if u.tone_on is not None: ch.tone_on = int(u.tone_on)
        if u.ctcss_on is not None: ch.ctcss_on = int(u.ctcss_on)
        if u.dcs_on is not None: ch.dcs_on = int(u.dcs_on)
        if u.tone_idx is not None: ch.tone_idx = u.tone_idx
        if u.ctcss_idx is not None: ch.ctcss_idx = u.ctcss_idx
        if u.dcs_idx is not None: ch.dcs_idx = u.dcs_idx
        self.radio.set_vfo(ch)

    async def update_vfo(self, u) -> None:
        await asyncio.to_thread(self._update_vfo, u)
        await self.refresh()

    async def set_power(self, band: int, level: int) -> None:
        await asyncio.to_thread(self.radio.set_power, band, level)
        await self.refresh()

    async def set_squelch(self, band: int, level: int) -> None:
        await asyncio.to_thread(self.radio.set_squelch_level, band, level)
        await self.refresh()

    def _mic_step(self, band: int, up: bool) -> None:
        # UP/DW act on the control band, so briefly make `band` the control
        # band, step it, then restore the previous control/ptt so the audio
        # path is left unchanged.
        control, ptt = self.radio.get_band_status()
        step = self.radio.mic_up if up else self.radio.mic_down
        if control != band:
            self.radio.set_control_band(band)
            step()
            self.radio.set_control_band(control, ptt)
        else:
            step()

    async def mic_step(self, band: int, up: bool) -> None:
        await asyncio.to_thread(self._mic_step, band, up)
        await self.refresh()
        await self.manager.broadcast(self._status.model_dump())

    def _set_control_band(self, band: int) -> None:
        # Keep the current PTT band (BC has independent control/ptt fields), so
        # selecting CTRL doesn't move the transmit band.
        _control, ptt = self.radio.get_band_status()
        self.radio.set_control_band(band, ptt)

    async def set_control_band(self, band: int) -> None:
        await asyncio.to_thread(self._set_control_band, band)
        await self.refresh()

    def _set_data_band(self, band: int) -> None:
        self.radio.set_data_band(band)
        self.data_band = band

    async def set_data_band(self, band: int) -> RadioStatus:
        await asyncio.to_thread(self._set_data_band, band)
        await self.refresh()
        await self.manager.broadcast(self._status.model_dump())
        return self._status

    def _set_ptt_band(self, band: int) -> None:
        # BC <control>,<ptt>: set the PTT band, preserving the control band.
        control, _ptt = self.radio.get_band_status()
        self.radio.set_control_band(control, band)

    async def set_ptt_band(self, band: int) -> None:
        await asyncio.to_thread(self._set_ptt_band, band)
        await self.refresh()

    async def set_tone_1750(self, on: bool) -> RadioStatus:
        """1750 Hz repeater tone call, generated in software on the mic path.
        The radio's menu-402 hold does not key an actual tone on this unit, so
        we drive a software tone generator on the TX audio instead — no serial
        I/O. The tone only sounds while PTT is keyed."""
        if self.tone_1750_sink is not None:
            self.tone_1750_sink(on)
        self.tone_1750 = on
        # Build the status to return/broadcast in a local so a concurrent poll
        # refresh can't swap self._status out from under us across the await.
        status = self._status
        if status is not None and status.connected:
            status = status.model_copy(update={"tone_1750": on})
            self._status = status
        else:
            status = await self.refresh()
        await self.manager.broadcast(status.model_dump())
        return status

    def _set_band_display(self, single: bool, band) -> None:
        if single:
            if band is not None:
                self.radio.set_control_band(band)
            self.radio.set_dl(True)
        else:
            self.radio.set_dl(False)

    async def set_band_display(self, single: bool, band=None) -> None:
        await asyncio.to_thread(self._set_band_display, single, band)
        await self.refresh()
        await self.manager.broadcast(self._status.model_dump())

    async def recall_memory(self, band: int, channel: int) -> None:
        await asyncio.to_thread(self.radio.recall_memory, band, channel)
        await self.refresh()

    # -- air band (Band A) -------------------------------------------------
    async def toggle_airband_a(self) -> bool:
        """Toggle Band A between the air band and 2 m.

        On: recall reserved memory 997 (the air band is an RX-only AM segment the
        VFO can't be moved into over CAT, so it's a memory recall). 997 is seeded
        once with a default air-band AM frequency if empty (never overwritten) —
        edit it to your preferred channel. Off (already in the air band): prime
        Band A's VFO back into 2 m. Returns the new air-band state."""
        bs = self._status.bands[0] if self._status.bands else None
        in_air = bool(bs and bs.rx_freq and 118_000_000 <= bs.rx_freq <= 137_000_000)
        if in_air:
            # back to 2 m (VHF) — VFO via reserved channel 998 at a 12.5 kHz step
            await self._prime_band(0, 144_000_000, 145_995_000, 0, 998, step=4)
        else:
            # air band (UKW / aviation) — recall the 997 scratch channel. Seed it
            # if empty; otherwise leave the user's frequency alone but make sure
            # its tuning step actually divides that frequency and the mode is AM
            # (the radio refuses an ME write whose freq isn't a step multiple).
            cur = await asyncio.to_thread(self.radio.get_memory, AIRBAND_CH)
            if cur is None:
                # 5 kHz step (index 0): the universal air-band step — it divides
                # every standard channel (25 kHz spacing and the 8.33 kHz labels).
                cur = ChannelData(index=AIRBAND_CH, rx_freq=119_000_000, step=0,
                                  shift=0, reverse=0, tone_on=0, ctcss_on=0,
                                  dcs_on=0, tone_idx=1, ctcss_idx=1, dcs_idx=0,
                                  offset=0, mode=MODE_AM, tx_freq=0, lockout=0)
                await asyncio.to_thread(self.radio.set_memory, cur)
            else:
                aligned = align_step_index(cur.rx_freq, cur.step)
                if cur.step != aligned or cur.mode != MODE_AM:
                    cur.step = aligned
                    cur.mode = MODE_AM
                    await asyncio.to_thread(self.radio.set_memory, cur)
            await asyncio.to_thread(self.radio.recall_memory, 0, AIRBAND_CH)
        await self.refresh()
        await self.manager.broadcast(self._status.model_dump())
        return not in_air

    # -- band scan ---------------------------------------------------------
    def scan_status(self) -> dict:
        return self._scan

    async def start_scan(self, band_key: str) -> dict:
        if self._scanning:
            raise RuntimeError("scan already running")
        self._scanning = True
        self._scan_cancel = False
        if band_key == "mem":
            # memory-bank scan: channels 0..99, empty ones skipped, repeats until
            # stopped. Pure recalls/reads (no flash writes), so looping is safe.
            channels = list(range(0, 100))
            self._scan = {"running": True, "band": band_key, "kind": "mem",
                          "total": 0, "index": 0, "points": [], "done": False,
                          "error": None, "sweep": 0,
                          "ch_start": channels[0], "ch_end": channels[-1]}
            runner = self._run_scan_mem(channels)
        else:
            if band_key not in SCAN_BANDS:
                self._scanning = False
                raise ValueError(f"unknown band {band_key!r}")
            start, end, step, fm_mode, scratch_ch, via_memory = SCAN_BANDS[band_key]
            freqs = list(range(start, end + 1, step))
            self._scan = {"running": True, "band": band_key, "kind": "freq",
                          "total": len(freqs), "index": 0, "points": [],
                          "done": False, "error": None, "sweep": 0,
                          "start_hz": start, "end_hz": end, "step_hz": step}
            runner = (self._run_scan_memory(freqs, fm_mode, scratch_ch) if via_memory
                      else self._run_scan(freqs, fm_mode, scratch_ch))
        self._scan_task = asyncio.create_task(runner)
        return self._scan

    def stop_scan(self) -> None:
        self._scan_cancel = True

    async def _prime_band(self, band: int, start_hz: int, end_hz: int,
                          fm_mode: int, scratch_ch: int, step: int = 4) -> bool:
        """Move ``band``'s VFO into the band [start_hz..end_hz].

        The TM-V71 VFO only tunes within its currently selected band and FO
        can't cross a band edge — but a memory recall can. So the reserved
        ``scratch_ch`` holds a frequency in this band; recalling it and then
        switching to VFO makes the VFO inherit the band. The channel is written
        only if it isn't already in-band (flash-friendly: a one-time write) and
        is never deleted. Returns True if this receiver reaches the band."""
        try:
            cur = await asyncio.to_thread(self.radio.get_memory, scratch_ch)
            # rewrite the scratch channel if it isn't in-band OR its step differs
            # from the wanted one, so the recall puts the right step on the VFO
            in_band = cur is not None and start_hz <= cur.rx_freq <= end_hz
            if not in_band or cur.step != step:
                scratch = ChannelData(index=scratch_ch, rx_freq=start_hz, step=step,
                                      shift=0, reverse=0, tone_on=0, ctcss_on=0,
                                      dcs_on=0, tone_idx=1, ctcss_idx=1, dcs_idx=0,
                                      offset=0, mode=fm_mode, tx_freq=0, lockout=0)
                await asyncio.to_thread(self.radio.set_memory, scratch)
            await asyncio.to_thread(self.radio.recall_memory, band, scratch_ch)
            await asyncio.sleep(0.25)
            await asyncio.to_thread(self.radio.set_band_mode, band, VFO_MODE)
            await asyncio.sleep(0.25)
            ch = await asyncio.to_thread(self.radio.get_vfo, band)
            ch.mode = fm_mode
            ch.rx_freq = start_hz
            ch.step = step
            await asyncio.to_thread(self.radio.set_vfo, ch)   # 'N' -> band not covered
            return True
        except TMV71Error:
            return False

    async def _run_scan(self, freqs: list[int], fm_mode: int,
                        scratch_ch: int) -> None:
        start_hz, end_hz = freqs[0], freqs[-1]
        orig_control = orig_ptt = 0
        touched: dict[int, tuple] = {}    # band -> (saved_mode, saved_mem_ch)
        used_band = None
        try:
            orig_control, orig_ptt = await asyncio.to_thread(self.radio.get_band_status)
            # Try the control band first; if its receiver doesn't cover the band
            # (e.g. band A is VHF-only for a 70 cm scan), use the other one.
            for band in (orig_control, 1 - orig_control):
                if self._scan_cancel or self._stop.is_set():
                    break
                mode = await asyncio.to_thread(self.radio.get_band_mode, band)
                bs = self._status.bands[band] if band < len(self._status.bands) else None
                touched[band] = (mode, bs.memory_channel if bs else None)
                if band != orig_control:   # scan on its audio -> make it control
                    await asyncio.to_thread(self.radio.set_control_band, band, band)
                    await asyncio.sleep(0.2)
                if await self._prime_band(band, start_hz, end_hz, fm_mode, scratch_ch):
                    used_band = band
                    break

            if used_band is None:
                self._scan["error"] = "No receiver (band A/B) covers this band."
                return

            ch = await asyncio.to_thread(self.radio.get_vfo, used_band)
            ch.mode = fm_mode
            sweep = 0
            # repeat the sweep until the user stops it (VFO retune only — no
            # per-step flash writes, so looping is safe for the radio)
            while not (self._scan_cancel or self._stop.is_set()):
                self._scan["points"] = []
                self._scan["index"] = 0
                for i, f in enumerate(freqs):
                    if self._scan_cancel or self._stop.is_set():
                        break
                    ch.rx_freq = f
                    try:
                        await asyncio.to_thread(self.radio.set_vfo, ch)
                    except TMV71Error:
                        self._scan["points"].append({"f": f, "db": -90.0})
                        self._scan["index"] = i + 1
                        continue
                    await asyncio.sleep(0.17)        # retune + audio settle
                    db = float(self.level_provider()) if self.level_provider else -90.0
                    self._scan["points"].append({"f": f, "db": round(db, 1)})
                    self._scan["index"] = i + 1
                if self._scan_cancel or self._stop.is_set():
                    break
                sweep += 1
                self._scan["sweep"] = sweep      # hold the finished sweep briefly
                await asyncio.sleep(0.6)          # so clients can grab it + throttle
        except Exception as exc:  # noqa: BLE001
            self._scan["error"] = str(exc)
            log.error("band scan failed: %s", exc)
        finally:
            try:
                # restore every band we touched and the control band; the
                # scratch channel is left in place (never deleted/rewritten).
                for b, (mode, mem_ch) in touched.items():
                    if mode == MR_MODE and mem_ch is not None:
                        await asyncio.to_thread(self.radio.recall_memory, b, mem_ch)
                    elif mode is not None and mode != VFO_MODE:
                        await asyncio.to_thread(self.radio.set_band_mode, b, mode)
                await asyncio.to_thread(self.radio.set_control_band, orig_control, orig_ptt)
            except Exception as exc:  # noqa: BLE001
                log.error("scan restore failed: %s", exc)
            self._scan["running"] = False
            self._scan["done"] = True
            self._scanning = False
            await self.refresh()
            await self.manager.broadcast(self._status.model_dump())

    async def _run_scan_memory(self, freqs: list[int], mode: int,
                               scratch_ch: int) -> None:
        """Sweep a band the VFO can't reach (the air band) by rewriting and
        recalling ``scratch_ch`` for each frequency, measuring the RX AF level.

        The air band lives on band A (the VHF receiver), so the scan runs there.
        One ME flash write happens per step — that's inherent to memory-recall
        scanning; keep the range/step modest. The scratch channel is left in
        place (used by the air-band toggle too)."""
        band = 0                       # air band is on band A (VHF receiver)
        orig_control = orig_ptt = 0
        saved_mode = saved_mem = None
        try:
            orig_control, orig_ptt = await asyncio.to_thread(self.radio.get_band_status)
            saved_mode = await asyncio.to_thread(self.radio.get_band_mode, band)
            bs = self._status.bands[band] if band < len(self._status.bands) else None
            saved_mem = bs.memory_channel if bs else None
            if orig_control != band:   # scan on its audio -> make it control
                await asyncio.to_thread(self.radio.set_control_band, band, band)
                await asyncio.sleep(0.2)
            for i, f in enumerate(freqs):
                if self._scan_cancel or self._stop.is_set():
                    break
                scratch = ChannelData(index=scratch_ch, rx_freq=f, step=7, shift=0,
                                      reverse=0, tone_on=0, ctcss_on=0, dcs_on=0,
                                      tone_idx=1, ctcss_idx=1, dcs_idx=0, offset=0,
                                      mode=mode, tx_freq=0, lockout=0)
                try:
                    await asyncio.to_thread(self.radio.set_memory, scratch)
                    await asyncio.to_thread(self.radio.recall_memory, band, scratch_ch)
                except TMV71Error:
                    self._scan["points"].append({"f": f, "db": -90.0})
                    self._scan["index"] = i + 1
                    continue
                await asyncio.sleep(0.17)            # retune + audio settle
                db = float(self.level_provider()) if self.level_provider else -90.0
                self._scan["points"].append({"f": f, "db": round(db, 1)})
                self._scan["index"] = i + 1
        except Exception as exc:  # noqa: BLE001
            self._scan["error"] = str(exc)
            log.error("air-band scan failed: %s", exc)
        finally:
            try:
                # restore band A's mode/channel and the original control band
                if saved_mode == MR_MODE and saved_mem is not None:
                    await asyncio.to_thread(self.radio.recall_memory, band, saved_mem)
                elif saved_mode is not None and saved_mode != MR_MODE:
                    await asyncio.to_thread(self.radio.set_band_mode, band, saved_mode)
                await asyncio.to_thread(self.radio.set_control_band, orig_control, orig_ptt)
            except Exception as exc:  # noqa: BLE001
                log.error("air scan restore failed: %s", exc)
            self._scan["running"] = False
            self._scan["done"] = True
            self._scanning = False
            await self.refresh()
            await self.manager.broadcast(self._status.model_dump())

    async def _run_scan_mem(self, channels: list[int]) -> None:
        """Scan the occupied memory channels in ``channels`` (empty ones are
        skipped), measuring the RX AF level on each, and repeat until stopped.

        Runs on the control band: each step recalls a stored channel (a mode/
        channel selection, no flash write) and reads the AF level — so looping
        is safe for the radio. The occupied set is re-read every sweep so newly
        added/cleared channels show up."""
        band = orig_control = orig_ptt = 0
        saved_mode = saved_mem = None
        try:
            band, orig_ptt = await asyncio.to_thread(self.radio.get_band_status)
            orig_control = band
            saved_mode = await asyncio.to_thread(self.radio.get_band_mode, band)
            bs = self._status.bands[band] if band < len(self._status.bands) else None
            saved_mem = bs.memory_channel if bs else None
            sweep = 0
            while not (self._scan_cancel or self._stop.is_set()):
                # find occupied channels (skip empty), re-read each sweep
                occupied: list[tuple[int, int]] = []
                for ch in channels:
                    if self._scan_cancel or self._stop.is_set():
                        break
                    m = await asyncio.to_thread(self.radio.get_memory, ch)
                    if m is not None:
                        occupied.append((ch, m.rx_freq))
                if self._scan_cancel or self._stop.is_set():
                    break
                self._scan["total"] = len(occupied)
                self._scan["points"] = []
                self._scan["index"] = 0
                if not occupied:
                    self._scan["error"] = "No occupied channels in 0–99."
                    break
                for i, (ch, f) in enumerate(occupied):
                    if self._scan_cancel or self._stop.is_set():
                        break
                    try:
                        await asyncio.to_thread(self.radio.recall_memory, band, ch)
                    except TMV71Error:
                        self._scan["points"].append({"f": f, "db": -90.0, "ch": ch})
                        self._scan["index"] = i + 1
                        continue
                    await asyncio.sleep(0.17)        # recall + audio settle
                    db = float(self.level_provider()) if self.level_provider else -90.0
                    self._scan["points"].append({"f": f, "db": round(db, 1), "ch": ch})
                    self._scan["index"] = i + 1
                if self._scan_cancel or self._stop.is_set():
                    break
                sweep += 1
                self._scan["sweep"] = sweep          # hold the finished sweep briefly
                await asyncio.sleep(0.6)             # so clients can grab it + throttle
        except Exception as exc:  # noqa: BLE001
            self._scan["error"] = str(exc)
            log.error("memory scan failed: %s", exc)
        finally:
            try:
                if saved_mode == MR_MODE and saved_mem is not None:
                    await asyncio.to_thread(self.radio.recall_memory, band, saved_mem)
                elif saved_mode is not None and saved_mode != MR_MODE:
                    await asyncio.to_thread(self.radio.set_band_mode, band, saved_mode)
                await asyncio.to_thread(self.radio.set_control_band, orig_control, orig_ptt)
            except Exception as exc:  # noqa: BLE001
                log.error("memory scan restore failed: %s", exc)
            self._scan["running"] = False
            self._scan["done"] = True
            self._scanning = False
            await self.refresh()
            await self.manager.broadcast(self._status.model_dump())

    async def send_dtmf_memory(self, channel: int) -> str:
        """Transmit the stored DTMF code (channel 0-9): key TX, send each digit
        via the DT command, then unkey. The mic audio path stays closed so only
        the radio-generated DTMF tones go out. Returns the code sent."""
        code = await asyncio.to_thread(self.radio.get_dtmf_memory, channel)
        if not code:
            return ""
        await asyncio.to_thread(self.radio.set_ptt, True)
        self.transmitting = True
        self._status.transmitting = True
        await self.manager.broadcast(self._status.model_dump())
        try:
            await asyncio.sleep(0.5)         # let the transmitter come up before the DTMF tones
            for d in code:
                await asyncio.to_thread(self.radio.send_dtmf_digit, d)
                await asyncio.sleep(0.12)
        finally:
            await asyncio.to_thread(self.radio.set_ptt, False)
            self.transmitting = False
            self._status.transmitting = False
        await self.manager.broadcast(self._status.model_dump())
        return code

    # -- memory channels ---------------------------------------------------
    def _read_memory(self, channel: int):
        cd = self.radio.get_memory(channel)
        if cd is None:
            return None
        name = self.radio.get_memory_name(channel)
        from .memory import channeldata_to_model
        return channeldata_to_model(cd, name)

    async def get_memory(self, channel: int):
        return await asyncio.to_thread(self._read_memory, channel)

    def _list_memories(self, start: int, end: int):
        out = []
        for ch in range(start, end + 1):
            m = self._read_memory(ch)
            if m is not None:
                out.append(m)
        return out

    async def list_memories(self, start: int, end: int):
        return await asyncio.to_thread(self._list_memories, start, end)

    def _write_memory(self, m) -> None:
        from .memory import model_to_channeldata
        cd = model_to_channeldata(m)
        # The radio refuses an ME write whose frequency is not a multiple of the
        # channel step; snap the step to one that fits so the write succeeds.
        cd.step = align_step_index(cd.rx_freq, cd.step)
        self.radio.set_memory(cd)
        if m.name:
            self.radio.set_memory_name(m.channel, m.name)

    async def set_memory(self, m) -> None:
        await asyncio.to_thread(self._write_memory, m)

    async def delete_memory(self, channel: int) -> None:
        await asyncio.to_thread(self.radio.delete_memory, channel)

    # -- device info (ID / AE / FV / TY) -----------------------------------
    def _read_info(self) -> RadioInfo:
        r = self.radio
        if not r.is_open:
            r.open()

        def safe(fn, *a):
            try:
                return fn(*a)
            except Exception:  # noqa: BLE001
                return None

        ty = safe(r.get_radio_type) or []
        return RadioInfo(
            app_version=config.APP_VERSION,
            model=safe(r.get_model),
            serial_number=safe(r.get_serial_number),
            firmware=safe(r.get_firmware, 0),
            market=(ty[0] if ty else None),
            crossband=(ty[3] == "1") if len(ty) > 3 else None,
            radio_type=(",".join(ty) if ty else None),
        )

    async def get_info(self) -> RadioInfo:
        return await asyncio.to_thread(self._read_info)

    # -- DTMF memory (10 channels) -----------------------------------------
    async def list_dtmf(self) -> list[DtmfMemory]:
        def _read():
            return [DtmfMemory(channel=ch, code=self.radio.get_dtmf_memory(ch))
                    for ch in range(10)]
        return await asyncio.to_thread(_read)

    async def set_dtmf(self, channel: int, code: str) -> DtmfMemory:
        await asyncio.to_thread(self.radio.set_dtmf_memory, channel, code)
        stored = await asyncio.to_thread(self.radio.get_dtmf_memory, channel)
        return DtmfMemory(channel=channel, code=stored)

    # -- serial reconnect ---------------------------------------------------
    async def reconnect(self, port: str, baud: int) -> str:
        def _do():
            self.radio.close()
            self.radio.port = port
            self.radio.baudrate = baud
            self.radio.open()
            return self.radio.get_model()
        self.model = await asyncio.to_thread(_do)
        config.save_runtime(serial_port=port, serial_baud=baud)
        await self._restore_squelch()
        await self.refresh()
        await self.manager.broadcast(self._status.model_dump())
        return self.model

    async def set_ptt(self, transmit: bool) -> None:
        # Couple audio first on key-up so the path is ready before RF; on
        # key-down drop RF first, then release audio.
        if transmit:
            if self.on_ptt:
                await self.on_ptt(True)
            await asyncio.to_thread(self.radio.set_ptt, True)
            self.transmitting = True
        else:
            # roger beep plays while the radio is still keyed + audio gate open
            if self.on_unkey:
                await self.on_unkey()
            await asyncio.to_thread(self.radio.set_ptt, False)
            self.transmitting = False
            if self.on_ptt:
                await self.on_ptt(False)
        await self.refresh()
        await self.manager.broadcast(self._status.model_dump())

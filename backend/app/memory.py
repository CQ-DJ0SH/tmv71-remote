"""Memory-channel logic: conversion between driver/API models and CSV I/O.

The TM-V71 stores up to 1000 memory channels (0..999). Each channel is read /
written with the ``ME`` command (frequency object) plus ``MN`` (8-char name).
"""
from __future__ import annotations

import csv
import io
from typing import Iterable, Optional

from .models import MemoryChannel
from .tmv71 import ChannelData, CTCSS_TONES, DCS_CODES, STEP_HZ


# --- model conversion -------------------------------------------------------
def channeldata_to_model(cd: ChannelData, name: str = "") -> MemoryChannel:
    return MemoryChannel(
        channel=cd.index, name=name, rx_freq=cd.rx_freq,
        tx_freq=cd.tx_freq or 0, step=cd.step, shift=cd.shift,
        reverse=cd.reverse, tone_on=bool(cd.tone_on),
        ctcss_on=bool(cd.ctcss_on), dcs_on=bool(cd.dcs_on),
        tone_idx=cd.tone_idx, ctcss_idx=cd.ctcss_idx, dcs_idx=cd.dcs_idx,
        offset=cd.offset, fm_mode=cd.mode, lockout=cd.lockout or 0,
    )


def model_to_channeldata(m: MemoryChannel) -> ChannelData:
    return ChannelData(
        index=m.channel, rx_freq=m.rx_freq, step=m.step, shift=m.shift,
        reverse=m.reverse, tone_on=int(m.tone_on), ctcss_on=int(m.ctcss_on),
        dcs_on=int(m.dcs_on), tone_idx=m.tone_idx, ctcss_idx=m.ctcss_idx,
        dcs_idx=m.dcs_idx, offset=m.offset, mode=m.fm_mode,
        tx_freq=m.tx_freq, lockout=m.lockout,
    )


# --- helpers for human-readable CSV ----------------------------------------
_SHIFT_STR = {0: "", 1: "+", 2: "-"}
_SHIFT_REV = {v: k for k, v in _SHIFT_STR.items()}
_MODE_STR = {0: "FM", 1: "NFM", 2: "AM"}
_MODE_REV = {v: k for k, v in _MODE_STR.items()}

CSV_FIELDS = ["channel", "name", "rx_mhz", "shift", "offset_mhz", "mode",
              "step_khz", "tone_on", "tone_hz", "ctcss_on", "ctcss_hz",
              "dcs_on", "dcs_code", "lockout"]


def _tone_idx_from_hz(hz: float, default: int = 1) -> int:
    best = min(range(len(CTCSS_TONES)), key=lambda i: abs(CTCSS_TONES[i] - hz))
    return best + 1 if abs(CTCSS_TONES[best] - hz) < 1.0 else default


def _dcs_idx_from_code(code: int, default: int = 0) -> int:
    return DCS_CODES.index(code) if code in DCS_CODES else default


def memory_to_csv_row(m: MemoryChannel) -> dict:
    tone_i = m.tone_idx - 1
    ctcss_i = m.ctcss_idx - 1
    return {
        "channel": m.channel, "name": m.name,
        "rx_mhz": f"{m.rx_freq / 1e6:.5f}",
        "shift": _SHIFT_STR.get(m.shift, ""),
        "offset_mhz": f"{m.offset / 1e6:.3f}",
        "mode": _MODE_STR.get(m.fm_mode, "FM"),
        "step_khz": f"{STEP_HZ[m.step] / 1000:g}" if m.step < len(STEP_HZ) else "",
        "tone_on": int(m.tone_on),
        "tone_hz": CTCSS_TONES[tone_i] if 0 <= tone_i < len(CTCSS_TONES) else "",
        "ctcss_on": int(m.ctcss_on),
        "ctcss_hz": CTCSS_TONES[ctcss_i] if 0 <= ctcss_i < len(CTCSS_TONES) else "",
        "dcs_on": int(m.dcs_on),
        "dcs_code": DCS_CODES[m.dcs_idx] if m.dcs_idx < len(DCS_CODES) else "",
        "lockout": m.lockout,
    }


def csv_row_to_memory(row: dict) -> MemoryChannel:
    step_khz = float(row.get("step_khz") or 12.5)
    step = min(range(len(STEP_HZ)),
               key=lambda i: abs(STEP_HZ[i] - step_khz * 1000))
    return MemoryChannel(
        channel=int(row["channel"]),
        name=(row.get("name") or "")[:8],
        rx_freq=round(float(row["rx_mhz"]) * 1e6),
        offset=round(float(row.get("offset_mhz") or 0) * 1e6),
        shift=_SHIFT_REV.get((row.get("shift") or "").strip(), 0),
        fm_mode=_MODE_REV.get((row.get("mode") or "FM").upper(), 0),
        step=step,
        tone_on=str(row.get("tone_on", "")).strip() in ("1", "True", "true"),
        ctcss_on=str(row.get("ctcss_on", "")).strip() in ("1", "True", "true"),
        dcs_on=str(row.get("dcs_on", "")).strip() in ("1", "True", "true"),
        tone_idx=_tone_idx_from_hz(float(row["tone_hz"])) if row.get("tone_hz") else 1,
        ctcss_idx=_tone_idx_from_hz(float(row["ctcss_hz"])) if row.get("ctcss_hz") else 1,
        dcs_idx=_dcs_idx_from_code(int(row["dcs_code"])) if row.get("dcs_code") else 0,
        lockout=int(row.get("lockout") or 0),
    )


def export_csv(channels: Iterable[MemoryChannel]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for m in channels:
        writer.writerow(memory_to_csv_row(m))
    return buf.getvalue()


def import_csv(text: str) -> list[MemoryChannel]:
    reader = csv.DictReader(io.StringIO(text))
    out: list[MemoryChannel] = []
    for row in reader:
        if not (row.get("channel") and row.get("rx_mhz")):
            continue
        out.append(csv_row_to_memory(row))
    return out

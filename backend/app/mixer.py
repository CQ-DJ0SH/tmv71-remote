"""ALSA mixer access (via amixer) for the USB radio-audio card.

The USB sound interface that bridges the radio often ships with its playback
volume at 0 % (no TX/monitor output) or capture muted. Expose its simple mixer
controls so they can be adjusted from the web UI instead of the shell.
"""
from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger("tmv71.mixer")


def _card_index() -> int | None:
    """ALSA index of the USB-Audio card (the radio interface), or None."""
    try:
        with open("/proc/asound/cards", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return None
    # e.g. " 0 [D71            ]: USB-Audio - 7-1"
    for m in re.finditer(r"^\s*(\d+)\s+\[.*?\]:\s*(\S+)", txt, re.M):
        if m.group(2) == "USB-Audio":
            return int(m.group(1))
    return None


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=5)


def _parse(block: str) -> dict | None:
    nm = re.match(r"'([^']+)',(\d+)", block)
    if not nm:
        return None
    if "Limits: Playback" in block:
        kind = "playback"
    elif "Limits: Capture" in block:
        kind = "capture"
    else:
        return None                       # enum / switch-only control: skip
    pct = re.search(r"\[(\d+)%\]", block)
    sw = re.search(r"\[(on|off)\]", block)
    return {
        "name": nm.group(1),
        "kind": kind,
        "percent": int(pct.group(1)) if pct else None,
        "has_switch": bool(sw),
        "switch_on": (sw.group(1) == "on") if sw else True,
    }


def list_controls() -> dict:
    card = _card_index()
    if card is None:
        return {"card": None, "controls": []}
    r = _run(["amixer", "-c", str(card)])
    if r.returncode != 0:
        return {"card": card, "controls": []}
    controls = []
    for block in r.stdout.split("Simple mixer control "):
        c = _parse(block.strip())
        if c:
            controls.append(c)
    return {"card": card, "controls": controls}


def set_control(name: str, percent: int | None = None,
                switch_on: bool | None = None) -> dict:
    card = _card_index()
    if card is None:
        raise RuntimeError("no USB audio card found")
    args = ["amixer", "-c", str(card), "sset", name]
    if percent is not None:
        args.append(f"{max(0, min(100, int(percent)))}%")
    if switch_on is not None:
        args.append("unmute" if switch_on else "mute")
    r = _run(args)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "amixer failed")
    try:                                   # persist across reboots
        _run(["alsactl", "store", str(card)])
    except Exception:  # noqa: BLE001
        log.warning("alsactl store failed")
    return list_controls()

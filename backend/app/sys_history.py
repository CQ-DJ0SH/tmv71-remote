"""24-hour history of the host metrics shown in Settings ▸ Hardware.

The live figures answer "what is it doing now"; they cannot answer "has it been
climbing all night", which is the question both a thermal problem and a slow
memory growth actually raise. One sample a minute for a day is 1440 points —
small enough to keep in memory and to hand to the browser in one go.

It is written through to disk because the interesting cases span a restart: a
service that was killed by its memory ceiling, or a Pi that rebooted, is exactly
when the hour before matters. The file is append-only and trimmed when it grows
past a couple of days, so it stays a few tens of kilobytes.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque

log = logging.getLogger("tmv71")

STEP_S = 60                      # one sample a minute
KEEP = 24 * 60                   # ...for 24 h
_TRIM_AT = 3 * KEEP              # rewrite the file once it holds ~3 days
_FILE = os.path.join(os.path.dirname(__file__), "sys_history.csv")

# ts, temp °C ×10, load1 ×100, host used MB, service RSS MB — all integers, so
# a day of samples is a small file and a small JSON payload.
_FIELDS = 5

_ring: deque = deque(maxlen=KEEP)


def _row(metrics: dict) -> tuple[int, int, int, int, int]:
    temp = metrics.get("temp_c")
    load = (metrics.get("load") or [0])[0]
    return (
        int(time.time()),
        int(round((temp if temp is not None else -1) * 10)),
        int(round(load * 100)),
        int(metrics.get("mem_used", 0) / 1048576),
        int(metrics.get("app_rss", 0) / 1048576),
    )


def load() -> None:
    """Read the tail of the file into the ring at startup."""
    try:
        with open(_FILE, encoding="utf-8") as f:
            lines = f.readlines()[-KEEP:]
    except OSError:
        return
    cut = time.time() - KEEP * STEP_S
    for line in lines:
        parts = line.split(",")
        if len(parts) != _FIELDS:
            continue
        try:
            row = tuple(int(p) for p in parts)
        except ValueError:
            continue
        if row[0] >= cut:
            _ring.append(row)
    log.info("system history: %d samples restored", len(_ring))


def _trim() -> None:
    try:
        with open(_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _TRIM_AT:
            return
        with open(_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines[-KEEP:])
    except OSError as exc:
        log.debug("system history trim failed: %s", exc)


def add(metrics: dict) -> None:
    """Append one sample (called from the sampler task)."""
    row = _row(metrics)
    _ring.append(row)
    try:
        with open(_FILE, "a", encoding="utf-8") as f:
            f.write(",".join(str(v) for v in row) + "\n")
    except OSError as exc:
        log.debug("system history write failed: %s", exc)
        return
    if row[0] % (STEP_S * KEEP) < STEP_S:      # about once a day
        _trim()


def series(hours: float = 24.0) -> dict:
    """Samples of the last ``hours``, oldest first, as parallel integer lists.

    Columns rather than records: a day of five metrics is 7200 numbers, and
    [[ts,…],[temp,…]] costs a third of what [{"ts":…,"temp":…},…] does.
    """
    cut = time.time() - max(0.0, hours) * 3600
    rows = [r for r in _ring if r[0] >= cut]
    cols = list(zip(*rows)) if rows else [()] * _FIELDS
    return {
        "step_s": STEP_S,
        "n": len(rows),
        "ts": list(cols[0]),
        "temp_c": [None if v < 0 else v / 10.0 for v in cols[1]],
        "load1": [v / 100.0 for v in cols[2]],
        "mem_used_mb": list(cols[3]),
        "app_rss_mb": list(cols[4]),
    }

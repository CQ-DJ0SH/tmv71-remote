"""Raspberry Pi host metrics (CPU/mem/disk/temp) read straight from /proc and
/sys — no third-party dependency. Surfaced in the web settings ▸ Hardware tab."""
from __future__ import annotations

import os
import shutil
import time


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _meminfo() -> dict:
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                info[k.strip()] = int(parts[0]) * 1024      # kB -> bytes
            except ValueError:
                pass
    return info


def _cpu_times() -> tuple[int, int]:
    fields = _read("/proc/stat").splitlines()[0].split()[1:]
    vals = [int(x) for x in fields]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)      # idle + iowait
    return sum(vals), idle


def _cpu_percent(interval: float = 0.3) -> float:
    t0, i0 = _cpu_times()
    time.sleep(interval)
    t1, i1 = _cpu_times()
    dt, di = t1 - t0, i1 - i0
    return round(100.0 * (1 - di / dt), 1) if dt > 0 else 0.0


def _model() -> str:
    m = (_read("/proc/device-tree/model")
         or _read("/sys/firmware/devicetree/base/model"))
    return m.replace("\x00", "").strip() or "Unknown"


def _temp_c() -> float | None:
    t = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    try:
        return round(int(t) / 1000.0, 1)
    except ValueError:
        return None


def _freq_mhz() -> int | None:
    f = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").strip()
    try:
        return round(int(f) / 1000)                          # kHz -> MHz
    except ValueError:
        return None


def collect() -> dict:
    mem = _meminfo()
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    sw_total = mem.get("SwapTotal", 0)
    sw_free = mem.get("SwapFree", 0)
    try:
        du = shutil.disk_usage("/")
        disk_total, disk_used = du.total, du.used
    except OSError:
        disk_total = disk_used = 0
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = [0, 0, 0]
    up = _read("/proc/uptime").split()
    return {
        "model": _model(),
        "cores": os.cpu_count() or 0,
        "cpu_percent": _cpu_percent(),
        "load": load,
        "temp_c": _temp_c(),
        "freq_mhz": _freq_mhz(),
        "mem_total": total,
        "mem_used": total - avail,
        "mem_available": avail,
        "swap_total": sw_total,
        "swap_used": sw_total - sw_free,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "uptime_sec": float(up[0]) if up else 0.0,
    }

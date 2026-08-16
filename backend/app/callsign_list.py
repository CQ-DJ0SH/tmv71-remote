"""Official German amateur-radio callsign list (BNetzA *Rufzeichenliste*) used to
verify ASR-recognised callsigns and enrich them with the holder's name, town and
licence class — all straight from the register, no online lookup.

The list ships as a large PDF (~700 pages). Parsing it takes minutes, so it is
parsed **once** into a tab-separated cache (``CALL\tCLASS\tNAME\tCITY`` per line)
and only the cache is read at runtime. Neither the PDF nor the cache is committed
(both are gitignored) — the operator supplies the current PDF on the Pi.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("tmv71")

_CALL = r"D[A-R]\d[A-Z]{1,3}"
# each entry reads "CALL, CLASS, NAME; STREET, ZIP CITY" (address may wrap / be
# absent). Capture CALL, CLASS and the remainder up to the next callsign entry.
_ENTRY = re.compile(
    rf"({_CALL}),\s*([A-Z0-9]{{1,3}}),\s*(.*?)\s*(?=(?:{_CALL},)|Seite \d|$)", re.S)
_ZIP_CITY = re.compile(r"\b\d{5}\s+([^,;]+)")


def default_pdf_path() -> str:
    """Where the Rufzeichenliste PDF is expected (overridable in settings)."""
    return "/opt/rufzeichenliste_afu.pdf"


def cache_path() -> str:
    """Extracted cache, kept next to the models dir (gitignored)."""
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "models", "rufzeichenliste.txt"))


def _parse_rest(rest: str) -> tuple:
    """'Name; Street, ZIP City' -> (name, city). City empty when no address."""
    rest = re.sub(r"\s+", " ", rest).strip()
    if ";" in rest:
        name, addr = rest.split(";", 1)
    else:
        name, addr = rest, ""
    m = _ZIP_CITY.search(addr)
    return name.strip()[:60], (m.group(1).strip()[:40] if m else "")


def build_cache(pdf_path: str, cache: str) -> int:
    """Parse the PDF into a sorted TSV cache (slow). Returns the callsign count."""
    from pypdf import PdfReader                 # heavy; only needed when building
    reader = PdfReader(pdf_path)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    # complete set of assigned callsigns (permissive) + parsed details where possible
    calls = set(re.findall(rf"({_CALL}),", text))
    details: dict = {}
    for m in _ENTRY.finditer(text):
        name, city = _parse_rest(m.group(3))
        details[m.group(1)] = (m.group(2), name, city)
    os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for c in sorted(calls):
            kl, name, city = details.get(c, ("", "", ""))
            f.write("\t".join((c, kl, name, city)) + "\n")
    os.replace(tmp, cache)                      # atomic
    return len(calls)


def load(pdf_path: str = "", cache: str = "") -> dict:
    """Read the cache and return ``{call: {"class", "name", "city"}}``.

    Read-only: it never (re)builds — building is a multi-minute PDF parse and must
    not stall startup. Run the converter manually to (re)build after supplying a
    new PDF: ``python -m app.callsign_list``. If the cache is missing, returns an
    empty dict and the caller skips verification (fails open).
    """
    cache = cache or cache_path()
    try:
        if not os.path.exists(cache):
            log.warning("callsign list: no cache at %s — ASR verification disabled; "
                        "build it with `python -m app.callsign_list`", cache)
            return {}
        out: dict = {}
        with open(cache, encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if p and p[0]:
                    out[p[0]] = {"class": p[1] if len(p) > 1 else "",
                                 "name": p[2] if len(p) > 2 else "",
                                 "city": p[3] if len(p) > 3 else ""}
        log.info("callsign list: loaded %d callsigns", len(out))
        return out
    except Exception as exc:                    # noqa: BLE001
        log.warning("callsign list: load failed: %s", exc)
    return {}


if __name__ == "__main__":
    # Rebuild the cache from the PDF. Run to refresh the list after downloading a
    # newer Rufzeichenliste:
    #     backend/.venv/bin/python -m app.callsign_list [PDF] [CACHE]
    # (run from the backend/ dir, or with backend on PYTHONPATH). With no args it
    # uses the default PDF path and cache location.
    import sys
    import time
    logging.basicConfig(level=logging.INFO)
    pdf = sys.argv[1] if len(sys.argv) > 1 else default_pdf_path()
    dst = sys.argv[2] if len(sys.argv) > 2 else cache_path()
    if not os.path.exists(pdf):
        sys.exit(f"PDF not found: {pdf}")
    t0 = time.time()
    n = build_cache(pdf, dst)
    print(f"built {n} callsigns in {time.time() - t0:.1f}s -> {dst}")

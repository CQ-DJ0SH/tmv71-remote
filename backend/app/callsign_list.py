"""Official German amateur-radio callsign list (BNetzA *Rufzeichenliste*) used to
verify ASR-recognised callsigns — a recognised call is only reported if it is an
actually assigned callsign, which removes almost all false positives.

The list ships as a large PDF (~700 pages). Parsing it takes minutes, so it is
parsed **once** into a plain-text cache (one callsign per line) and only the
cache is read at runtime. Neither the PDF nor the cache is committed (both are
gitignored) — the operator supplies the current PDF on the Pi.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("tmv71")

# Each entry reads "CALLSIGN, <class>, <name>; <address>" — capture the leading
# German callsign (D + [A-R] + digit + 1..3 letters), anchored by the comma.
_CALL_RE = re.compile(r"(D[A-R]\d[A-Z]{1,3}),")


def default_pdf_path() -> str:
    """Where the Rufzeichenliste PDF is expected (overridable in settings)."""
    return "/opt/rufzeichenliste_afu.pdf"


def cache_path() -> str:
    """Extracted-callsign cache, kept next to the models dir (gitignored)."""
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "models", "rufzeichenliste.txt"))


def build_cache(pdf_path: str, cache: str) -> int:
    """Parse the PDF into a sorted callsign cache (slow). Returns the count."""
    from pypdf import PdfReader                 # heavy; only needed when building
    calls: set = set()
    reader = PdfReader(pdf_path)
    for page in reader.pages:
        calls.update(_CALL_RE.findall(page.extract_text() or ""))
    os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="ascii") as f:
        f.write("\n".join(sorted(calls)))
    os.replace(tmp, cache)                      # atomic
    return len(calls)


def load(pdf_path: str = "", cache: str = "") -> set:
    """Return the set of assigned callsigns.

    Reads the cache; (re)builds it from the PDF when the cache is missing or
    older than the PDF. On any failure returns an empty set, in which case the
    caller skips verification (fails open). NOTE: building blocks for minutes —
    call this from a worker thread, and pre-build the cache during setup.
    """
    pdf_path = pdf_path or default_pdf_path()
    cache = cache or cache_path()
    try:
        have_pdf = os.path.exists(pdf_path)
        have_cache = os.path.exists(cache)
        stale = have_cache and have_pdf and \
            os.path.getmtime(cache) < os.path.getmtime(pdf_path)
        if have_pdf and (not have_cache or stale):
            n = build_cache(pdf_path, cache)
            log.info("callsign list: built cache (%d callsigns) from %s", n, pdf_path)
            have_cache = True
        elif not have_pdf and not have_cache:
            log.warning("callsign list: no PDF at %s and no cache — "
                        "ASR verification disabled", pdf_path)
        if have_cache:
            with open(cache, encoding="ascii") as f:
                calls = set(f.read().split())
            log.info("callsign list: loaded %d callsigns", len(calls))
            return calls
    except Exception as exc:                    # noqa: BLE001
        log.warning("callsign list: load failed: %s", exc)
    return set()


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

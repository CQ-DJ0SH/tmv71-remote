"""Official German amateur-radio callsign list (BNetzA *Rufzeichenliste*) used to
verify ASR-recognised callsigns and enrich them with the holder's name, address
and licence class — all straight from the register, no online lookup.

The list ships as a large PDF (~700 pages). Parsing it takes minutes, so it is
parsed **once** into a tab-separated cache, one
``CALL\tCLASS\tNAME\tCITY\tSTREET\tZIP`` per line, and only the cache is read
at runtime. Neither the PDF nor the cache is committed
(both are gitignored) — the operator supplies the current PDF on the Pi.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("tmv71")

_CALL = r"D[A-R]\d[A-Z]{1,3}"
# each entry reads "CALL, CLASS, NAME; STREET, ZIP CITY" (address may wrap / be
# absent). Capture CALL, CLASS and the remainder up to the next callsign entry.
_ENTRY = re.compile(
    rf"({_CALL}),\s*([A-Z0-9]{{1,3}}),\s*(.*?)\s*(?=(?:{_CALL},)|Seite \d|$)", re.S)
_ZIP = re.compile(r"\b(\d{5})\s+")
# a column break hyphenates a word across lines ("Franz-\nSchneller-Str."), which
# the text extraction hands over as "Franz- Schneller-Str."; joining needs a
# letter on both sides so a spaced dash between words stays untouched
_WRAP = re.compile(r"(?<=[A-Za-zÄÖÜäöüß])- (?=[A-ZÄÖÜa-zäöüß])")


def default_pdf_path() -> str:
    """Where the Rufzeichenliste PDF is expected (overridable in settings)."""
    return "/opt/rufzeichenliste_afu.pdf"


def cache_path() -> str:
    """Extracted cache, kept next to the models dir (gitignored)."""
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "models", "rufzeichenliste.txt"))


def _parse_rest(rest: str) -> tuple:
    """'Name; Street, ZIP City' -> (name, street, zip, city); empty where absent.

    The register is not consistent about the separator: most entries put a
    semicolon between holder and address, a few hundred (mostly club and relay
    stations) only a comma. Splitting on the semicolon alone left the whole
    address sitting in the name field, so a comma before the postcode counts as
    a separator too — names themselves carry no comma.
    """
    rest = _WRAP.sub("-", re.sub(r"\s+", " ", rest).strip())
    zip_ = _ZIP.search(rest)
    if ";" in rest:
        name, addr = rest.split(";", 1)
    elif zip_ and "," in rest[:zip_.start()]:
        cut = rest.index(",")
        name, addr = rest[:cut], rest[cut + 1:]
    else:
        name, addr = rest, ""
    zip_ = _ZIP.search(addr)
    if zip_:                                    # street is what precedes the ZIP
        street = addr[:zip_.start()].strip(" ,")
        code = zip_.group(1)
        city = re.split(r"[,;]", addr[zip_.end():])[0].strip()
    else:
        street, code, city = addr.strip(" ,"), "", ""
    return name.strip()[:60], street[:60], code, city[:40]


def build_cache(pdf_path: str, cache: str) -> int:
    """Parse the PDF into a sorted TSV cache (slow). Returns the callsign count."""
    from pypdf import PdfReader                 # heavy; only needed when building
    reader = PdfReader(pdf_path)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    # complete set of assigned callsigns (permissive) + parsed details where possible
    calls = set(re.findall(rf"({_CALL}),", text))
    details: dict = {}
    for m in _ENTRY.finditer(text):
        name, street, code, city = _parse_rest(m.group(3))
        details[m.group(1)] = (m.group(2), name, city, street, code)
    os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for c in sorted(calls):
            kl, name, city, street, code = details.get(c, ("", "", "", "", ""))
            f.write("\t".join((c, kl, name, city, street, code)) + "\n")
    os.replace(tmp, cache)                      # atomic
    return len(calls)


def json_path(cache: str = "") -> str:
    """The JSON export sits next to the TSV cache (also gitignored)."""
    return os.path.splitext(cache or cache_path())[0] + ".json"


def export_json(cache: str = "", dst: str = "") -> int:
    """Write the cache out as JSON. Returns the number of callsigns.

    Built from the cache, not from the PDF: the slow part is the parse, and both
    files are meant to hold exactly the same reading of the same list. The
    header says which PDF and which run it came from, because the register is
    reissued every few weeks and two exports are otherwise indistinguishable.
    """
    import time

    cache = cache or cache_path()
    dst = dst or json_path(cache)
    calls = load("", cache)
    doc = {"source": os.path.basename(default_pdf_path()),
           "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "count": len(calls), "calls": calls}
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, dst)                        # atomic
    return len(calls)


def load(pdf_path: str = "", cache: str = "") -> dict:
    """Read the cache -> ``{call: {"class", "name", "city", "street", "zip"}}``.

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
                                 "city": p[3] if len(p) > 3 else "",
                                 "street": p[4] if len(p) > 4 else "",
                                 "zip": p[5] if len(p) > 5 else ""}
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
    # uses the default PDF path and cache location. Every run also writes the
    # same list as JSON next to the cache; --json-only skips the PDF parse and
    # re-exports from the cache that is already there.
    import sys
    import time
    logging.basicConfig(level=logging.INFO)
    argv = [a for a in sys.argv[1:] if a != "--json-only"]
    pdf = argv[0] if argv else default_pdf_path()
    dst = argv[1] if len(argv) > 1 else cache_path()
    t0 = time.time()
    if "--json-only" in sys.argv:
        n = export_json(dst)
        print(f"exported {n} callsigns in {time.time() - t0:.1f}s -> {json_path(dst)}")
        sys.exit(0)
    if not os.path.exists(pdf):
        sys.exit(f"PDF not found: {pdf}")
    n = build_cache(pdf, dst)
    print(f"built {n} callsigns in {time.time() - t0:.1f}s -> {dst}")
    export_json(dst)
    print(f"exported {n} callsigns -> {json_path(dst)}")

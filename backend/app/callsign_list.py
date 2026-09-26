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
from collections.abc import Mapping

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


# Where each register is expected on disk, and what its cache is called. The
# two sources could hardly be less alike — Germany publishes a 685-page PDF, the
# FCC a zip of pipe-separated tables — but both end up in the same cache, so
# everything above this module sees one list.
SOURCES = {
    "de": ("/opt/rufzeichenliste_afu.pdf", "rufzeichenliste.txt"),
    "us": ("/opt/l_amat.zip", "fcc-amateur.txt"),
}


def default_pdf_path(region: str = "de") -> str:
    """Where the register is expected (overridable in settings)."""
    return SOURCES.get(region, SOURCES["de"])[0]


def cache_path(region: str = "de") -> str:
    """Extracted cache, kept next to the models dir (gitignored)."""
    name = SOURCES.get(region, SOURCES["de"])[1]
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "models", name))


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
            # the seventh column is the state, which a German licence has not
            f.write("\t".join((c, kl, name, city, street, code, "")) + "\n")
    os.replace(tmp, cache)                      # atomic
    return len(calls)


def build_cache_fcc(zip_path: str, cache: str) -> int:
    """Parse the FCC ULS amateur dump (l_amat.zip) into the same cache.

    Three of its tables are needed and they are read in that order: HD says
    which licences are actually active (the dump carries every expired and
    cancelled one too — barely half of its records are live), AM carries the
    operator class, EN the name and address. All three are streamed line by
    line: together they are some 500 MB unpacked, and holding them would cost
    more memory than the Pi has to spare.

    Unlike the German PDF this needs no text extraction at all, so it takes
    seconds rather than a minute and a half.
    """
    import zipfile

    def rows(zf, name):
        with zf.open(name) as fh:
            for raw in fh:
                yield raw.decode("latin-1").rstrip("\r\n").split("|")

    live: dict = {}
    with zipfile.ZipFile(zip_path) as zf:
        for f in rows(zf, "HD.dat"):                 # 4 call, 5 licence status
            if len(f) > 5 and f[5] == "A" and f[4]:
                live[f[4]] = ["", "", "", "", "", ""]   # class,name,city,street,zip,state
        for f in rows(zf, "AM.dat"):                 # 5 operator class
            if len(f) > 5 and f[4] in live:
                live[f[4]][0] = f[5]
        for f in rows(zf, "EN.dat"):
            # 7 entity name, 8 first, 10 last, 15 street, 16 city, 17 state, 18 zip
            if len(f) <= 18 or f[4] not in live:
                continue
            rec = live[f[4]]
            person = " ".join(x for x in (f[8], f[10]) if x)
            rec[1] = _name_case(person or f[7])[:60]
            rec[2] = _name_case(f[16])[:40]
            rec[3] = _name_case(f[15])[:60]
            rec[4] = f[18][:5]                       # ZIP+4 is more than we show
            rec[5] = f[17][:2]
    os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for c in sorted(live):
            kl, name, city, street, code, state = live[c]
            fh.write("\t".join((c, kl, name, city, street, code, state)) + "\n")
    os.replace(tmp, cache)                          # atomic
    return len(live)


def _name_case(s: str) -> str:
    """FCC records are all upper case; a card full of capitals shouts.

    Title case with the exceptions that matter in names and addresses: Mc/Mac
    keep their inner capital, Roman numerals and the state-style abbreviations
    stay as they are, and a lone letter (middle initial, "N" in a street name)
    is left alone.
    """
    out = []
    for w in (s or "").strip().split():
        if len(w) <= 1 or w in ("II", "III", "IV", "JR", "SR", "NE", "NW",
                                "SE", "SW", "PO", "US", "USA"):
            out.append(w if len(w) > 1 else w)
        elif w.startswith("MC") and len(w) > 3:
            out.append("Mc" + w[2:].capitalize())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def json_path(cache: str = "") -> str:
    """The JSON export sits next to the TSV cache (also gitignored)."""
    return os.path.splitext(cache or cache_path())[0] + ".json"


def export_json(cache: str = "", dst: str = "", src: str = "") -> int:
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
    # the mapping unpacks a line at a time (see CallList), so the export is
    # built as a generator would be — one entry expanded, written, dropped
    doc = {"source": os.path.basename(src or default_pdf_path()),
           "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "count": len(calls), "calls": {c: calls[c] for c in sorted(calls)}}
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, dst)                        # atomic
    return len(calls)


class CallList(Mapping):
    """The register in memory: callsign -> {class, name, city, street, zip, state}.

    Each entry is kept as the one packed line it was read from and split only
    when someone asks for it. That is not premature thrift: the German register
    holds 70,000 callsigns, the American one 823,000, and a dict of dicts for
    the latter costs about half a gigabyte on a Pi that is also holding two
    Vosk models. Packed, the same list is a fraction of that, and a lookup
    happens a few times per over — never in the audio path.
    """

    __slots__ = ("_raw",)
    FIELDS = ("class", "name", "city", "street", "zip", "state")

    def __init__(self, raw: dict):
        self._raw = raw

    def __getitem__(self, call: str) -> dict:
        p = self._raw[call].split("\t")
        return {k: (p[i] if i < len(p) else "") for i, k in enumerate(self.FIELDS)}

    def __iter__(self):
        return iter(self._raw)

    def __len__(self) -> int:
        return len(self._raw)

    def __contains__(self, call) -> bool:       # the hot one: N-best rescoring
        return call in self._raw


def load(pdf_path: str = "", cache: str = "") -> Mapping:
    """Read the cache into a :class:`CallList`.

    Read-only: it never (re)builds — building is a multi-minute PDF parse and must
    not stall startup. Run the converter manually to (re)build after supplying a
    new register: ``python -m app.callsign_list``. If the cache is missing,
    returns an empty mapping and the caller skips verification (fails open).
    """
    cache = cache or cache_path()
    try:
        if not os.path.exists(cache):
            log.warning("callsign list: no cache at %s — ASR verification disabled; "
                        "build it with `python -m app.callsign_list`", cache)
            return {}
        raw: dict = {}
        with open(cache, encoding="utf-8") as f:
            for line in f:
                call, _, rest = line.rstrip("\n").partition("\t")
                if call:
                    raw[call] = rest
        log.info("callsign list: loaded %d callsigns", len(raw))
        return CallList(raw)
    except Exception as exc:                    # noqa: BLE001
        log.warning("callsign list: load failed: %s", exc)
    return {}


if __name__ == "__main__":
    # Rebuild the cache from the register. Run it after downloading a newer one:
    #     backend/.venv/bin/python -m app.callsign_list [--region de|us] [SRC] [CACHE]
    # (run from the backend/ dir, or with backend on PYTHONPATH). With no args it
    # uses the region's default source and cache location. The source decides how
    # it is read: a .zip is the FCC ULS dump, anything else the BNetzA PDF. Every
    # run also writes the same list as JSON next to the cache; --json-only skips
    # the parse and re-exports from the cache that is already there.
    import sys
    import time
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    reg = "de"
    if "--region" in args:
        i = args.index("--region")
        reg = args[i + 1] if i + 1 < len(args) else "de"
        del args[i:i + 2]
    json_only = "--json-only" in args
    args = [a for a in args if a != "--json-only"]
    t0 = time.time()
    if json_only:
        # re-export only: the one path this takes is the CACHE, not the register
        dst = args[0] if args else cache_path(reg)
        n = export_json(dst, src=default_pdf_path(reg))
        print(f"exported {n} callsigns in {time.time() - t0:.1f}s -> {json_path(dst)}")
        sys.exit(0)
    src = args[0] if args else default_pdf_path(reg)
    dst = args[1] if len(args) > 1 else cache_path(reg)
    if not os.path.exists(src):
        sys.exit(f"register not found: {src}")
    if src.lower().endswith(".zip"):
        n = build_cache_fcc(src, dst)
    else:
        n = build_cache(src, dst)
    print(f"built {n} callsigns in {time.time() - t0:.1f}s -> {dst}")
    export_json(dst, src=src)
    print(f"exported {n} callsigns -> {json_path(dst)}")

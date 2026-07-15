"""Off-air callsign recognition: grammar-constrained Vosk ASR on the RX audio.

German amateur callsigns are read out in the international phonetic alphabet with
German digits — "Delta Lima null Sierra Hotel" = DL0SH. We restrict Vosk's
vocabulary to exactly that word set (a grammar), which keeps recognition reliable
on noisy FM voice where free dictation would be useless. Recognised words are
mapped back to letters/digits and valid German callsigns (prefix D, i.e. DA–DR)
are pulled out of the resulting string.

The model (vosk-model-small-de-0.15) is imported lazily so the dependency is only
required when the feature is switched on.
"""
from __future__ import annotations

import json
import re
from typing import List, Tuple

import numpy as np

# Phonetic word -> letter/digit. Where the small German model lacks the canonical
# spelling we use an in-vocabulary alternate that sounds the same: foxtrot→foxtrott,
# xray→x, quebec→québec, juliett→julia/julius. All entries verified present in the
# model's lexicon (grammar build reports zero out-of-vocabulary words).
WORD_MAP = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E",
    "foxtrott": "F", "fox": "F", "golf": "G", "hotel": "H", "india": "I",
    "julia": "J", "julius": "J", "kilo": "K", "lima": "L", "mike": "M",
    "november": "N", "oscar": "O", "papa": "P", "québec": "Q", "romeo": "R",
    "sierra": "S", "tango": "T", "uniform": "U", "victor": "V", "viktor": "V",
    "whiskey": "W", "whisky": "W", "x": "X", "yankee": "Y", "zulu": "Z",
    # German spelling alphabet (Buchstabiertafel) as a fallback — hams mix both.
    # All verified present in the model except "xanthippe" (xaver covers X).
    "anton": "A", "berta": "B", "cäsar": "C", "caesar": "C", "dora": "D",
    "emil": "E", "friedrich": "F", "gustav": "G", "heinrich": "H", "ida": "I",
    "kaufmann": "K", "konrad": "K", "ludwig": "L", "martha": "M", "nordpol": "N",
    "otto": "O", "paula": "P", "quelle": "Q", "richard": "R", "samuel": "S",
    "siegfried": "S", "theodor": "T", "ulrich": "U", "wilhelm": "W", "xaver": "X",
    "ypsilon": "Y", "zacharias": "Z", "zeppelin": "Z",
    # German letter NAMES ("DB0SP" = de-be-null-es-pe). Least reliable layer —
    # short words, some homophones of common speech — but very common on FM.
    # F/V/X names ("ef/vau/iks") aren't in the model; phonetic/Buchstabier cover them.
    "a": "A", "be": "B", "ce": "C", "de": "D", "e": "E", "ge": "G", "ha": "H",
    "i": "I", "jot": "J", "ka": "K", "el": "L", "ell": "L", "em": "M", "emm": "M",
    "en": "N", "enn": "N", "o": "O", "pe": "P", "ku": "Q", "kuh": "Q", "er": "R",
    "err": "R", "es": "S", "te": "T", "u": "U", "we": "W", "weh": "W", "zett": "Z",
    # digits (German)
    "null": "0", "eins": "1", "zwei": "2", "drei": "3", "vier": "4",
    "fünf": "5", "sechs": "6", "sieben": "7", "acht": "8", "neun": "9",
}
# Grammar handed to Vosk: our word list plus "[unk]" so anything else in the
# transmission (rag-chew, reports) is absorbed instead of forced onto a phonetic
# word. ensure_ascii=False keeps "fünf"/"québec" as real UTF-8 (Vosk needs it).
GRAMMAR = json.dumps(list(WORD_MAP) + ["[unk]"], ensure_ascii=False)

# German amateur callsign: D + [A-R] + one digit + 1..3 letter suffix — never
# longer than 6 characters.
CALL_RE = re.compile(r"D[A-R][0-9][A-Z]{1,3}")

# A callsign is spelled as one contiguous group. Recognised letters carry no word
# boundary, so a call read out twice ("DN6YI DN6YI") would run together into
# DN6YIDN6YI and match wrongly. Split on a speech gap between words instead.
# Kept generous: splitting a slowly spelled call mid-way loses its last letters,
# whereas two calls running together are resolved by _tile() anyway.
GROUP_GAP_S = 0.9

TARGET_SR = 16000
_DECIM = 3               # 48 kHz radio audio -> 16 kHz for Vosk


def _design_lowpass(cutoff: float, fs: int, ntaps: int) -> np.ndarray:
    """Windowed-sinc anti-alias filter for the 48→16 kHz decimation."""
    m = (ntaps - 1) / 2.0
    k = np.arange(ntaps) - m
    h = np.sinc(2.0 * cutoff / fs * k) * np.hamming(ntaps)
    return (h / h.sum()).astype(np.float32)


# Cut just below the 16 kHz Nyquist (8 kHz); speech energy is well inside this.
_LP = _design_lowpass(7000.0, 48000, 15)


def downsample_48_to_16(x: np.ndarray) -> np.ndarray:
    """48 kHz int16 mono -> 16 kHz float32 mono (anti-aliased, decimate by 3)."""
    return np.convolve(x.astype(np.float32), _LP, mode="same")[::_DECIM]


# --- ASR pre-filter: restore the spectral balance the acoustic model expects ---
# The flat 9600/discriminator output carries NO de-emphasis (so it is too bright/
# hissy vs. the natural speech Vosk was trained on) and passes CTCSS/sub-audio +
# DC. We therefore (1) de-emphasise 75 µs — the single biggest win — and (2)
# high-pass ~250 Hz to drop CTCSS/DC. Both are folded into one linear-phase FIR,
# applied with overlap-save so it stays continuous across the 0.12 s chunks.
DEEMPH_TAU_S = 75e-6
HP_CUTOFF_HZ = 250.0


def _deemph_kernel(fs: int, tau: float, ntaps: int = 24) -> np.ndarray:
    """6 dB/oct de-emphasis as a normalised exponential FIR (−3 dB at 1/2πτ)."""
    a = 1.0 - np.exp(-1.0 / (tau * fs))
    h = a * (1.0 - a) ** np.arange(ntaps)
    return (h / h.sum()).astype(np.float32)


def _highpass_kernel(fs: int, fc: float, ntaps: int = 221) -> np.ndarray:
    """Windowed-sinc low-pass, spectrally inverted to a high-pass (CTCSS/DC)."""
    m = (ntaps - 1) / 2.0
    lp = np.sinc(2.0 * fc / fs * (np.arange(ntaps) - m)) * np.hamming(ntaps)
    lp /= lp.sum()
    hp = -lp
    hp[int(m)] += 1.0                     # spectral inversion: δ − lowpass
    return hp.astype(np.float32)


# de-emphasis ⊛ high-pass, precomputed at 16 kHz
_ASR_FIR = np.convolve(_deemph_kernel(TARGET_SR, DEEMPH_TAU_S),
                       _highpass_kernel(TARGET_SR, HP_CUTOFF_HZ)).astype(np.float32)


def normalize_call(call: str) -> str:
    """Upper-case, strip anything that isn't A–Z/0–9 (own-call comparison)."""
    return "".join(c for c in (call or "").upper() if c.isalnum())


class CallsignRecognizer:
    """Wraps a grammar-constrained Vosk recognizer and extracts callsigns.

    ``feed`` streams 48 kHz RX audio in; ``flush`` finalises the current
    utterance (call at end of an over). Both return ``[(callsign, confidence)]``.
    """

    def __init__(self, model_dir: str, own_call: str = "", min_conf: float = 0.55):
        from vosk import Model, KaldiRecognizer, SetLogLevel  # lazy dependency
        SetLogLevel(-1)                       # silence per-frame + grammar logs
        self._model = Model(model_dir)
        self._rec = KaldiRecognizer(self._model, TARGET_SR, GRAMMAR)
        self._rec.SetWords(True)              # per-word confidence for gating
        self.own = normalize_call(own_call)
        self.min_conf = min_conf
        self._fir_hist = np.zeros(len(_ASR_FIR) - 1, dtype=np.float32)   # overlap-save

    def set_own(self, own_call: str) -> None:
        self.own = normalize_call(own_call)

    @staticmethod
    def _tile(s: str):
        """Split `s` into back-to-back callsigns covering it completely, else None.

        Disambiguates a group spoken without a pause: greedy matching would read
        "DN6YIDN6YI" as DN6YID + leftovers, while the only tiling that consumes
        the whole run is DN6YI + DN6YI."""
        end = len(s)
        memo = {end: []}                       # position -> spans tiling s[pos:]

        def solve(i):
            if i in memo:
                return memo[i]
            res = None
            for k in (6, 5, 4):                # longest first; 4 = shortest legal
                if i + k <= end and CALL_RE.fullmatch(s[i:i + k]):
                    rest = solve(i + k)
                    if rest is not None:
                        res = [(i, i + k)] + rest
                        break
            memo[i] = res                      # memoised -> linear, never blows up
            return res

        return solve(0)

    def _extract(self, result: dict) -> List[Tuple[str, float]]:
        # (letter, confidence, start, end) for every word that maps to a letter/digit
        items = []
        for w in result.get("result") or []:
            ch = WORD_MAP.get(w.get("word", ""))
            if ch:
                items.append((ch, float(w.get("conf", 1.0)),
                              float(w.get("start", 0.0)), float(w.get("end", 0.0))))
        # group contiguous spelling; a pause starts a new group (see GROUP_GAP_S)
        groups: List[list] = []
        for it in items:
            if groups and it[2] - groups[-1][-1][3] <= GROUP_GAP_S:
                groups[-1].append(it)
            else:
                groups.append([it])
        out: List[Tuple[str, float]] = []
        for g in groups:
            s = "".join(i[0] for i in g)
            confs = [i[1] for i in g]
            # a clean tiling of the whole group wins; else scan for calls in it
            spans = self._tile(s)
            if spans is None:
                spans = [(m.start(), m.end()) for m in CALL_RE.finditer(s)]
            for a, b in spans:
                seg = confs[a:b]
                avg = sum(seg) / len(seg) if seg else 0.0
                call = s[a:b]
                if call == self.own or avg < self.min_conf:
                    continue
                out.append((call, avg))
        return out

    def _shape(self, x16: np.ndarray) -> np.ndarray:
        """De-emphasis + high-pass (overlap-save, continuous across chunks)."""
        buf = np.concatenate((self._fir_hist, x16))
        y = np.convolve(buf, _ASR_FIR, mode="valid")
        self._fir_hist = buf[-(len(_ASR_FIR) - 1):]
        return np.clip(y, -32768, 32767).astype(np.int16)

    def feed(self, pcm48: np.ndarray) -> List[Tuple[str, float]]:
        x16 = self._shape(downsample_48_to_16(pcm48))   # de-emphasis + high-pass
        if self._rec.AcceptWaveform(x16.tobytes()):
            return self._extract(json.loads(self._rec.Result()))
        return []

    def flush(self) -> List[Tuple[str, float]]:
        return self._extract(json.loads(self._rec.FinalResult()))

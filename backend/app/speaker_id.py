"""Speaker recognition for the ASR contact panel — attribute an over that
carries no spoken callsign.

The idea that makes this cheap: enrolment costs nothing. Every over in which
the callsign recognition DID hear a call is labelled audio for that station, so
the voice profiles build themselves while the panel is simply used. An over
without a callsign is then compared against those profiles.

Model: Vosk's own speaker model (vosk-model-spk-0.4, ~14 MB), the x-vector from
Kaldi's CallHome diarization recipe. It is trained on 8 kHz telephone speech —
narrowband, noisy, channel-varied — which is far closer to FM off a repeater
than a wideband (VoxCeleb) model would be.

Measured here on two off-air recordings of the same round (14 + 11 overs, four
stations present in both):

    same station, different recording   0.62 … 0.79 cosine
    different stations (117 pairs)      0.33 mean, 0.64 max
    closed-set identification           4 of 4 on rank 1 of 11 profiles

At threshold 0.55 with a 0.05 margin over the runner-up, 3 of the 4 known
stations were identified, none wrongly, and none of the 7 stations unknown to
the profile set was mistaken for a known one. The MARGIN does the real work: a
stranger's best match is flat (0.01–0.08 ahead of the second), a true match
stands clear (0.20–0.34). The threshold alone would have called DO3AO "DC7WQ".

Everything stays on the Pi. A voiceprint is biometric data; speakers.json is
gitignored, is never uploaded, and is dropped when its card is deleted.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import numpy as np

log = logging.getLogger("tmv71")

_STORE = os.path.join(os.path.dirname(__file__), "speakers.json")
_MODEL = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "models", "vosk-model-spk-0.4"))

# An x-vector needs speech to work with. Below ~3 s the measurement fell apart
# (d' 2.2, EER 12 % on 1.5–3 s halves) while whole overs of 4–7 s reached
# d' 3.8 — so short overs are skipped rather than guessed at.
MIN_SPEECH_S = 2.5
MAX_SEG_S = 30.0          # cap the buffer; nobody needs 30 s to be recognised
# Profiles keep adapting, but an old profile must not be swamped by one noisy
# over: the running mean is weighted, and the weight stops growing at 20 overs.
MAX_WEIGHT = 20


class SpeakerBook:
    """Voice profiles: one running-mean x-vector per callsign."""

    def __init__(self, asr_model_dir_getter):
        self._asr_dir = asr_model_dir_getter
        self._model = None                 # vosk.Model (acoustic, for the decoder)
        self._spk = None                   # vosk.SpkModel (x-vector extractor)
        self._lock = threading.Lock()      # embed() runs in a worker thread
        self._prof: dict = self._load()

    # --- persistence --------------------------------------------------------
    @staticmethod
    def _load() -> dict:
        try:
            with open(_STORE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            with open(_STORE, "w", encoding="utf-8") as f:
                json.dump(self._prof, f)
        except OSError as exc:
            log.warning("speaker profiles: save failed: %s", exc)

    # --- model --------------------------------------------------------------
    @property
    def available(self) -> bool:
        if not os.path.isdir(_MODEL):
            return False
        try:
            import vosk  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def load_models(self) -> None:
        """Blocking model load — call from a thread."""
        if self._spk is not None:
            return
        from vosk import Model, SpkModel, SetLogLevel
        SetLogLevel(-1)
        self._model = Model(self._asr_dir())
        self._spk = SpkModel(_MODEL)

    def embed(self, pcm48: np.ndarray) -> list | None:
        """x-vector of one over (48 kHz int16 mono), or None if it held no speech.

        A second, plain recognizer is needed: the callsign decoder runs with
        SetMaxAlternatives for its N-best rescoring, and in that mode Vosk
        returns only the alternatives list — no "spk" field. Measured cost of
        this extra pass on a Pi 5: ~1.3 s per 5 s over, once, after the over.
        """
        from vosk import KaldiRecognizer
        from .callsign_asr import downsample_48_to_16
        try:
            self.load_models()
            x = downsample_48_to_16(pcm48)
            # deliberately NOT the ASR's de-emphasis/high-pass conditioning: it
            # made no difference to separation (d' 2.18 vs 2.20) and the x-vector
            # model wants the signal as it comes.
            x16 = np.clip(x, -32768, 32767).astype(np.int16)
            with self._lock:
                rec = KaldiRecognizer(self._model, 16000)
                rec.SetSpkModel(self._spk)
                rec.AcceptWaveform(x16.tobytes())
                res = json.loads(rec.FinalResult())
        except Exception as exc:  # noqa: BLE001
            log.warning("speaker id: embedding failed: %s", exc)
            return None
        v = res.get("spk")
        return v if v else None

    # --- profiles -----------------------------------------------------------
    @staticmethod
    def _unit(v) -> np.ndarray:
        a = np.asarray(v, dtype=np.float64)
        n = np.linalg.norm(a)
        return a / n if n else a

    def enrol(self, call: str, vec: list) -> int:
        """Fold one labelled over into that station's profile. Returns the new
        number of overs behind it."""
        call = (call or "").upper()
        if not call or not vec:
            return 0
        cur = self._prof.get(call)
        v = self._unit(vec)
        if cur:
            n = min(int(cur.get("n", 1)), MAX_WEIGHT)
            mean = self._unit(np.asarray(cur["v"], dtype=np.float64) * n + v)
            n = int(cur.get("n", 1)) + 1
        else:
            mean, n = v, 1
        self._prof[call] = {"v": mean.tolist(), "n": n, "ts": int(time.time())}
        self._save()
        return n

    def match(self, vec: list, threshold: float, margin: float) -> dict:
        """Best profile for this over, with the runner-up that decides whether
        the answer may be trusted."""
        out = {"call": "", "score": 0.0, "second": "", "second_score": 0.0,
               "margin": 0.0, "accept": False, "profiles": len(self._prof)}
        if not vec or not self._prof:
            return out
        v = self._unit(vec)
        scores = sorted(((float(np.dot(v, self._unit(p["v"]))), c)
                         for c, p in self._prof.items()), reverse=True)
        out["call"], out["score"] = scores[0][1], round(scores[0][0], 3)
        if len(scores) > 1:
            out["second"], out["second_score"] = scores[1][1], round(scores[1][0], 3)
            out["margin"] = round(scores[0][0] - scores[1][0], 3)
        else:
            # a single profile has nothing to stand out from, so the margin test
            # cannot be applied — the threshold alone has to carry it
            out["margin"] = round(scores[0][0], 3)
        out["accept"] = bool(out["score"] >= threshold and out["margin"] >= margin)
        return out

    def rename(self, old: str, new: str) -> bool:
        """Follow a corrected callsign. Without this a mis-heard call would keep
        a voice profile under a name that never existed — and the real station
        would never accumulate one."""
        old, new = (old or "").upper(), (new or "").upper()
        p = self._prof.pop(old, None)
        if p is None:
            return False
        cur = self._prof.get(new)
        if cur:                                  # merge into an existing profile
            n = min(int(cur.get("n", 1)), MAX_WEIGHT)
            m = min(int(p.get("n", 1)), MAX_WEIGHT)
            mean = self._unit(np.asarray(cur["v"]) * n + np.asarray(p["v"]) * m)
            p = {"v": mean.tolist(), "n": int(cur.get("n", 1)) + int(p.get("n", 1)),
                 "ts": int(time.time())}
        self._prof[new] = p
        self._save()
        return True

    def forget(self, call: str) -> bool:
        if self._prof.pop((call or "").upper(), None) is None:
            return False
        self._save()
        return True

    def stats(self) -> dict:
        return {"profiles": len(self._prof),
                "calls": sorted((c, int(p.get("n", 1))) for c, p in self._prof.items())}

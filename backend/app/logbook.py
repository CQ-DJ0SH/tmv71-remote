"""Logbook integration: log QSOs to external services and read recent contacts.

Currently implements a Wavelog provider (locally installed Wavelog/Cloudlog
instance) over its JSON API, plus QRZ.com callsign lookup via the XML data API.

The design goal from the UI: the operator only enters callsign (+ optionally a
name); everything else — frequency, band, mode, date/time, own callsign — is
filled in automatically from the live radio state and the configured station
profile, and missing details (name, grid, …) are looked up via the provider.

No third-party HTTP client is available in the venv, so all outbound calls use
the stdlib ``urllib`` and run inside ``asyncio.to_thread`` from the endpoints.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .config import settings, save_runtime

_RECENT_FILE = os.path.join(os.path.dirname(__file__), "logbook.json")
_RECENT_MAX = 50


class LogError(Exception):
    """A logbook provider request failed (network / API error)."""


# --- helpers ----------------------------------------------------------------

# Amateur band edges (Hz) → ADIF band name. Covers the TM-V71's VHF/UHF range
# plus common HF bands so the same helper works if a different rig is attached.
_BANDS = [
    (1_800_000, 2_000_000, "160m"), (3_500_000, 4_000_000, "80m"),
    (5_250_000, 5_450_000, "60m"), (7_000_000, 7_300_000, "40m"),
    (10_100_000, 10_150_000, "30m"), (14_000_000, 14_350_000, "20m"),
    (18_068_000, 18_168_000, "17m"), (21_000_000, 21_450_000, "15m"),
    (24_890_000, 24_990_000, "12m"), (28_000_000, 29_700_000, "10m"),
    (50_000_000, 54_000_000, "6m"), (70_000_000, 71_000_000, "4m"),
    (144_000_000, 148_000_000, "2m"), (222_000_000, 225_000_000, "1.25m"),
    (430_000_000, 440_000_000, "70cm"), (902_000_000, 928_000_000, "33cm"),
    (1_240_000_000, 1_300_000_000, "23cm"),
]

_FM_MODE = {0: "FM", 1: "FM", 2: "AM"}   # rig fm_mode int (NFM logged as FM)


def band_for_hz(hz: int | None) -> str:
    if not hz:
        return ""
    for lo, hi, name in _BANDS:
        if lo <= hz <= hi:
            return name
    return ""


def _ssl_ctx() -> ssl.SSLContext:
    """Unverified TLS context — the Wavelog instance is a local/LAN server that
    typically uses a self-signed or internal certificate."""
    return ssl._create_unverified_context()


def _adif_field(name: str, value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s == "":
        return ""
    # ADIF data length is the byte count — matters for non-ASCII (umlauts etc.)
    return f"<{name.upper()}:{len(s.encode('utf-8'))}>{s}"


def build_adif(qso: dict, station_callsign: str = "", my_grid: str = "") -> str:
    """Build a single ADIF QSO record (terminated with <EOR>) from a QSO dict."""
    fields = [
        ("CALL", qso.get("call")),
        ("QSO_DATE", qso.get("qso_date")),
        ("TIME_ON", qso.get("time_on")),
        ("FREQ", qso.get("freq_mhz")),
        ("BAND", qso.get("band")),
        ("MODE", qso.get("mode")),
        ("RST_SENT", qso.get("rst_sent")),
        ("RST_RCVD", qso.get("rst_rcvd")),
        ("NAME", qso.get("name")),
        ("GRIDSQUARE", qso.get("gridsquare")),
        ("QTH", qso.get("qth")),
        ("COUNTRY", qso.get("country")),
        ("EMAIL", qso.get("email")),
        ("COMMENT", qso.get("comment")),
        ("STATION_CALLSIGN", station_callsign),
        ("MY_GRIDSQUARE", my_grid),
    ]
    return "".join(_adif_field(n, v) for n, v in fields) + "<EOR>"


# --- providers --------------------------------------------------------------

class LogProvider:
    """Base class for a logbook backend."""
    name = "base"
    label = "Base"

    def configured(self) -> bool:
        return False

    def log_qso(self, adif: str, power_w: float | None = None) -> dict:
        raise NotImplementedError

    def lookup(self, callsign: str, band: str = "", mode: str = "") -> dict:
        return {}

    def stats(self) -> dict:
        return {}

    def stations(self) -> list[dict]:
        return []

    def test(self) -> dict:
        return {"ok": False, "message": "not configured"}


class WavelogProvider(LogProvider):
    name = "wavelog"
    label = "Wavelog"

    def __init__(self, url: str = "", key: str = "", station_id: str = ""):
        self.url = (url or "").rstrip("/")
        self.key = key or ""
        self.station_id = str(station_id or "")

    def configured(self) -> bool:
        return bool(self.url and self.key)

    # Wavelog exposes its API at /index.php/api/<ep> and (with URL rewriting) at
    # /api/<ep>; try both so it works regardless of the install's setup.
    def _request(self, ep: str, payload: dict | None = None,
                 key_in_url: bool = False, timeout: float = 8.0):
        last_err = None
        for root in (f"{self.url}/index.php/api", f"{self.url}/api"):
            url = f"{root}/{ep}"
            if key_in_url:
                url = f"{url}/{self.key}"
            data = json.dumps(payload or {}).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=_ssl_ctx()) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return self._parse(raw), getattr(resp, "status", 200)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    last_err = e
                    continue
                raw = ""
                try:
                    raw = e.read().decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    pass
                parsed = self._parse(raw)
                if isinstance(parsed, dict):
                    parsed.setdefault("_http", e.code)
                    return parsed, e.code
                raise LogError(f"HTTP {e.code}: {raw[:160] or e.reason}")
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
        raise LogError(str(last_err) if last_err else "request failed")

    @staticmethod
    def _parse(raw: str):
        raw = (raw or "").strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {"message": raw}

    def test(self) -> dict:
        if not self.configured():
            return {"ok": False, "message": "URL and API key required"}
        try:
            body, code = self._request("version", {"key": self.key})
        except LogError as e:
            return {"ok": False, "message": str(e)}
        if isinstance(body, dict) and body.get("version"):
            return {"ok": True, "message": f"Wavelog {body['version']}",
                    "version": body.get("version")}
        # Older instances may lack /version — fall back to station_info.
        try:
            st, code = self._request("station_info", key_in_url=True)
        except LogError as e:
            return {"ok": False, "message": str(e)}
        if isinstance(st, list):
            return {"ok": True, "message": f"{len(st)} station profile(s)"}
        return {"ok": False, "message": f"unexpected response ({code})"}

    def stations(self) -> list[dict]:
        if not self.configured():
            return []
        try:
            body, _ = self._request("station_info", key_in_url=True)
        except LogError:
            return []
        out = []
        if isinstance(body, list):
            for s in body:
                out.append({
                    "id": str(s.get("station_id", "")),
                    "name": s.get("station_profile_name", ""),
                    "callsign": s.get("station_callsign", ""),
                    "grid": s.get("station_gridsquare", ""),
                    "active": str(s.get("station_active", "")) in ("1", "true"),
                })
        return out

    def stats_status(self) -> dict:
        """One statistics round-trip → reachability + the dashboard counts.
        A successful response means the instance is reachable (online); the raw
        body is returned so the UI can render whatever keys Wavelog sends."""
        if not self.configured():
            return {"online": False, "stats": {}}
        try:
            body, _ = self._request("statistics", key_in_url=True)
        except LogError:
            return {"online": False, "stats": {}}
        return {"online": True, "stats": body if isinstance(body, dict) else {}}

    def lookup(self, callsign: str, band: str = "", mode: str = "") -> dict:
        if not self.configured():
            return {}
        payload = {"key": self.key, "callsign": callsign}
        if band:
            payload["band"] = band
        if mode:
            payload["mode"] = mode
        try:
            body, _ = self._request("private_lookup", payload)
        except LogError as e:
            raise LogError(str(e))
        if not isinstance(body, dict):
            return {}
        dxcc = body.get("dxcc")
        if isinstance(dxcc, dict):
            dxcc = dxcc.get("name") or dxcc.get("entity")
        return {
            "callsign": body.get("callsign") or callsign,
            "name": body.get("name") or "",
            "gridsquare": body.get("gridsquare") or "",
            "dxcc": dxcc or "",
            "state": body.get("state") or "",
            "qth": body.get("location") or body.get("qth") or "",
            "worked_before": bool(body.get("call_worked")),
        }

    def log_qso(self, adif: str, power_w: float | None = None) -> dict:
        if not self.configured():
            return {"ok": False, "message": "Wavelog not configured"}
        payload = {"key": self.key, "station_profile_id": self.station_id,
                   "type": "adif", "string": adif}
        if power_w:
            payload["power"] = power_w
        try:
            body, code = self._request("qso", payload)
        except LogError as e:
            return {"ok": False, "message": str(e)}
        msg = ""
        if isinstance(body, dict):
            msg = (body.get("messages") or body.get("message")
                   or body.get("status") or "")
            if isinstance(msg, list):
                msg = "; ".join(str(m) for m in msg)
        ok = code in (200, 201) and "error" not in str(msg).lower()
        return {"ok": ok, "message": str(msg) or ("logged" if ok else "failed")}


class QrzProvider(LogProvider):
    """QRZ.com. Callsign lookup via the XML Data API (username/password →
    session key) is implemented; QSO upload via the logbook API key is planned."""
    name = "qrz"
    label = "QRZ.com"
    XML_URL = "https://xmldata.qrz.com/xml/current/"
    AGENT = "tmv71-remote 1.0"

    def __init__(self, api_key: str = "", username: str = "", password: str = ""):
        self.api_key = api_key or ""
        self.username = username or ""
        self.password = password or ""
        self._session = ""          # cached XML session key

    def configured(self) -> bool:
        return False   # QSO upload not implemented yet (so it isn't a log target)

    def lookup_ok(self) -> bool:
        return bool(self.username and self.password)

    @staticmethod
    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]   # strip XML namespace

    def _xml(self, params: dict, timeout: float = 8.0) -> dict:
        url = self.XML_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": self.AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            raise LogError(f"QRZ: {e}")
        out: dict[str, dict] = {}
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise LogError("QRZ: bad XML response")
        for sect in root:
            d = {self._local(c.tag): (c.text or "").strip() for c in sect}
            out.setdefault(self._local(sect.tag), {}).update(d)
        return out

    def _ensure_session(self) -> str:
        if self._session:
            return self._session
        data = self._xml({"username": self.username, "password": self.password,
                          "agent": self.AGENT})
        key = data.get("Session", {}).get("Key")
        if not key:
            raise LogError(data.get("Session", {}).get("Error") or "login failed")
        self._session = key
        return key

    def lookup(self, callsign: str, band: str = "", mode: str = "") -> dict:
        if not self.lookup_ok():
            return {}
        call = callsign.strip().upper()
        for attempt in (1, 2):
            key = self._ensure_session()
            data = self._xml({"s": key, "callsign": call})
            cs = data.get("Callsign", {})
            err = data.get("Session", {}).get("Error", "")
            if cs:
                fname, lname = cs.get("fname", ""), cs.get("name", "")
                return {
                    "callsign": cs.get("call", call),
                    "name": (fname + " " + lname).strip(),
                    "gridsquare": cs.get("grid", ""),
                    "email": cs.get("email", ""),
                    "qth": cs.get("addr2", ""),
                    "state": cs.get("state", ""),
                    "country": cs.get("country", ""),
                }
            # session expired → drop it and retry once
            if attempt == 1 and "session" in err.lower():
                self._session = ""
                continue
            if "not found" in err.lower():
                return {}
            raise LogError(err or "callsign not found")
        return {}

    def test(self) -> dict:
        if not self.lookup_ok():
            return {"ok": False, "message": "username and password required"}
        try:
            self._session = ""
            self._ensure_session()
            return {"ok": True, "message": "configured"}
        except LogError as e:
            return {"ok": False, "message": str(e)}


# --- manager ----------------------------------------------------------------

class LogBook:
    """Owns the providers, builds QSOs from live state, keeps a local recent log."""

    def __init__(self):
        self.wavelog = WavelogProvider(
            settings.wavelog_url, settings.wavelog_key, settings.wavelog_station_id)
        self.qrz = QrzProvider(
            settings.qrz_api_key, settings.qrz_username, settings.qrz_password)
        self._recent = self._load_recent()

    # --- config ---
    def config(self) -> dict:
        return {
            "wavelog_url": self.wavelog.url,
            "wavelog_key": self.wavelog.key,
            "wavelog_station_id": self.wavelog.station_id,
            "qrz_api_key": self.qrz.api_key,
            "qrz_username": self.qrz.username,
            "qrz_password": self.qrz.password,
            "wavelog_enabled": self.wavelog.configured(),
            "qrz_enabled": self.qrz.lookup_ok(),
        }

    def configure(self, **cfg) -> dict:
        if "wavelog_url" in cfg:
            self.wavelog.url = (cfg["wavelog_url"] or "").rstrip("/")
        if "wavelog_key" in cfg:
            self.wavelog.key = cfg["wavelog_key"] or ""
        if "wavelog_station_id" in cfg:
            self.wavelog.station_id = str(cfg["wavelog_station_id"] or "")
        if "qrz_api_key" in cfg:
            self.qrz.api_key = cfg["qrz_api_key"] or ""
        if "qrz_username" in cfg:
            self.qrz.username = cfg["qrz_username"] or ""
        if "qrz_password" in cfg:
            self.qrz.password = cfg["qrz_password"] or ""
        save_runtime(
            wavelog_url=self.wavelog.url, wavelog_key=self.wavelog.key,
            wavelog_station_id=self.wavelog.station_id,
            qrz_api_key=self.qrz.api_key, qrz_username=self.qrz.username,
            qrz_password=self.qrz.password)
        return self.config()

    # --- recent store ---
    def _load_recent(self) -> list:
        try:
            with open(_RECENT_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (FileNotFoundError, ValueError):
            return []

    def _save_recent(self) -> None:
        try:
            with open(_RECENT_FILE, "w", encoding="utf-8") as f:
                json.dump(self._recent[:_RECENT_MAX], f, indent=2)
        except OSError:
            pass

    def recent(self) -> list:
        return self._recent[:_RECENT_MAX]

    def delete_recent(self, ts: str) -> dict:
        before = len(self._recent)
        self._recent = [e for e in self._recent if e.get("ts") != ts]
        self._save_recent()
        return {"deleted": before - len(self._recent), "recent": self.recent()}

    def clear_recent(self) -> dict:
        self._recent = []
        self._save_recent()
        return {"recent": []}

    def stats_status(self) -> dict:
        return self.wavelog.stats_status()

    def lookup(self, callsign: str, band: str = "", mode: str = "") -> dict:
        """Merge a callsign lookup from QRZ.com (rich personal data: name, grid,
        e-mail) and Wavelog (worked-before / DXCC)."""
        call = callsign.strip().upper()
        result = {"callsign": call, "name": "", "gridsquare": "", "email": "",
                  "qth": "", "state": "", "country": "", "dxcc": "",
                  "worked_before": False, "sources": []}
        errors = []
        # QRZ first — preferred for name/grid/e-mail
        if self.qrz.lookup_ok():
            try:
                q = self.qrz.lookup(call, band, mode)
                if q:
                    for k in ("name", "gridsquare", "email", "qth", "state", "country"):
                        if q.get(k):
                            result[k] = q[k]
                    result["sources"].append("qrz")
            except LogError as e:
                errors.append(f"QRZ: {e}")
        # Wavelog — worked-before + DXCC, and fill any gaps
        if self.wavelog.configured():
            try:
                w = self.wavelog.lookup(call, band, mode)
                if w:
                    for k in ("name", "gridsquare", "state", "qth", "dxcc"):
                        if not result.get(k) and w.get(k):
                            result[k] = w[k]
                    result["worked_before"] = bool(w.get("worked_before"))
                    result["sources"].append("wavelog")
            except LogError as e:
                errors.append(f"Wavelog: {e}")
        if not result["sources"] and errors:
            raise LogError("; ".join(errors))
        return result

    # --- logging ---
    def log(self, *, callsign: str, freq_hz: int | None, mode: str,
            name: str = "", rst_sent: str = "59", rst_rcvd: str = "59",
            comment: str = "", gridsquare: str = "", email: str = "",
            qth: str = "", country: str = "", power_w: float | None = None,
            station_callsign: str = "", my_grid: str = "") -> dict:
        now = datetime.now(timezone.utc)
        call = callsign.strip().upper()
        if not call:
            raise LogError("callsign required")
        band = band_for_hz(freq_hz)
        freq_mhz = f"{freq_hz / 1e6:.5f}" if freq_hz else ""
        qso = {
            "call": call,
            "qso_date": now.strftime("%Y%m%d"),
            "time_on": now.strftime("%H%M%S"),
            "freq_mhz": freq_mhz,
            "band": band,
            "mode": mode or "FM",
            "rst_sent": rst_sent or "59",
            "rst_rcvd": rst_rcvd or "59",
            "name": name.strip(),
            "gridsquare": gridsquare.strip(),
            "email": email.strip(),
            "qth": qth.strip(),
            "country": country.strip(),
            "comment": comment.strip(),
        }
        adif = build_adif(qso, station_callsign=station_callsign, my_grid=my_grid)

        targets: dict[str, dict] = {}
        for prov in (self.wavelog, self.qrz):
            if prov.configured():
                try:
                    targets[prov.name] = prov.log_qso(adif, power_w=power_w)
                except Exception as e:  # noqa: BLE001
                    targets[prov.name] = {"ok": False, "message": str(e)}

        if not targets:
            raise LogError("no logbook configured — set up Wavelog in Settings")

        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "call": call, "name": qso["name"], "band": band,
            "freq_mhz": freq_mhz, "mode": qso["mode"],
            "rst_sent": qso["rst_sent"], "rst_rcvd": qso["rst_rcvd"],
            "gridsquare": qso["gridsquare"], "email": qso["email"],
            "qth": qso["qth"], "country": qso["country"],
            "comment": qso["comment"],
            "targets": {k: bool(v.get("ok")) for k, v in targets.items()},
        }
        self._recent.insert(0, entry)
        self._recent = self._recent[:_RECENT_MAX]
        self._save_recent()
        ok = any(v.get("ok") for v in targets.values())
        return {"ok": ok, "entry": entry, "targets": targets}


logbook = LogBook()

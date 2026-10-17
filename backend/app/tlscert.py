"""The TLS certificate a browser has to be told to trust, and where it lives.

The Pi serves HTTPS with a certificate of its own — the browser microphone only
works in a secure context, and no public CA will sign a LAN address — so every
new device shows a warning until that certificate is installed. An installed
PWA is worse off than a tab: it has no "proceed anyway" to click past.

What a device installs is the *issuer*, not the server certificate, whenever
there is one: the manual's own root CA signs the server certificate, and
trusting the CA once covers every renewal of the leaf below it. Only a plainly
self-signed server certificate (install.sh's default) is itself the thing to
install.

Everything here is read-only: the private keys beside these files are never
touched, and nothing is served that is not already public in the TLS handshake.
"""
from __future__ import annotations

import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID

from .config import settings

# certs/ beside the backend, as created by deploy/install.sh
_DEFAULT_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "certs"))

# the leaf, if uvicorn was not given an explicit path
_LEAF_NAMES = ("cert.pem", "cert.crt", "fullchain.pem", "server.pem")
# the issuer above it — install.sh's CA layout first
_CA_NAMES = ("ca/ca.crt", "ca/ca.pem", "ca.crt", "ca.pem", "rootCA.pem")


def _cert_dir() -> str:
    """Where the serving certificate lives (TMV71_SSL_CERTFILE wins)."""
    if settings.ssl_certfile:
        d = os.path.dirname(os.path.abspath(settings.ssl_certfile))
        if d:
            return d
    return _DEFAULT_DIR


def _leaf_path() -> str | None:
    if settings.ssl_certfile and os.path.isfile(settings.ssl_certfile):
        return os.path.abspath(settings.ssl_certfile)
    d = _cert_dir()
    for n in _LEAF_NAMES:
        p = os.path.join(d, n)
        if os.path.isfile(p):
            return p
    return None


def _load(path: str) -> x509.Certificate | None:
    try:
        with open(path, "rb") as fh:
            return x509.load_pem_x509_certificate(fh.read())
    except Exception:
        return None


def _cn(name: x509.Name) -> str:
    try:
        return str(name.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value)
    except Exception:
        return name.rfc4514_string()


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(
            x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _fingerprint(cert: x509.Certificate) -> str:
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def _names(cert: x509.Certificate) -> list[str]:
    """The SAN entries — the addresses this certificate is actually valid for."""
    try:
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    out: list[str] = []
    for n in san.get_values_for_type(x509.DNSName):
        out.append(str(n))
    for ip in san.get_values_for_type(x509.IPAddress):
        out.append(str(ip))
    return out


def resolve() -> tuple[str | None, x509.Certificate | None, str, str]:
    """The file a device should install: (path, cert, kind, reason).

    kind is "ca" for the issuer above the server certificate, "self" for a
    self-signed server certificate. Both empty with a reason when there is
    nothing worth offering — a certificate from a public CA needs no install,
    and a private CA whose own file is not on the Pi cannot be handed out.
    """
    leaf_p = _leaf_path()
    if not leaf_p:
        return None, None, "", "no certificate file found"
    leaf = _load(leaf_p)
    if leaf is None:
        return None, None, "", "certificate unreadable"

    if leaf.issuer == leaf.subject:
        return leaf_p, leaf, "self", ""

    d = _cert_dir()
    for n in _CA_NAMES:
        p = os.path.join(d, n)
        if not os.path.isfile(p):
            continue
        ca = _load(p)
        if ca is not None and _is_ca(ca) and ca.subject == leaf.issuer:
            return p, ca, "ca", ""
    return None, None, "", "issuer certificate not on the Pi"


def info() -> dict:
    """What the browser shows beside the download link."""
    path, cert, kind, reason = resolve()
    if not path or cert is None:
        return {"available": False, "kind": "", "reason": reason}
    leaf = _load(_leaf_path() or "")
    return {
        "available": True,
        "kind": kind,                        # "ca" | "self"
        "subject": _cn(cert.subject),
        "issuer": _cn(cert.issuer),
        "fingerprint": _fingerprint(cert),   # SHA-256, colon-separated
        "not_before": cert.not_valid_before_utc.strftime("%Y-%m-%d"),
        "not_after": cert.not_valid_after_utc.strftime("%Y-%m-%d"),
        "filename": filename(kind, cert),
        "names": _names(leaf) if leaf is not None else [],
    }


def filename(kind: str, cert: x509.Certificate) -> str:
    """A name the phone's file list makes sense of."""
    stem = "tmv71-remote-ca" if kind == "ca" else "tmv71-remote"
    return f"{stem}.crt"

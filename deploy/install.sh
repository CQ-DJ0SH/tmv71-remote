#!/usr/bin/env bash
# TM-V71 Remote — install helper. Run from the repo root: sudo deploy/install.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> System packages"
apt-get update -qq
# portaudio: sounddevice; swig + liblgpio-dev: build lgpio (GPIO power switch).
# Two-way browser audio (WebRTC/Opus) is handled in-process by aiortc (pip) —
# no Mumble server needed.
apt-get install -y portaudio19-dev python3-venv git swig liblgpio-dev

echo "==> Python venv + deps"
cd "$ROOT/backend"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> Self-signed TLS cert (browser mic needs HTTPS)"
IP="$(hostname -I | awk '{print $1}')"
mkdir -p "$ROOT/backend/certs"
if [ ! -f "$ROOT/backend/certs/cert.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$ROOT/backend/certs/key.pem" -out "$ROOT/backend/certs/cert.pem" \
    -subj "/CN=tmv71-remote" \
    -addext "subjectAltName=IP:${IP},DNS:localhost,IP:127.0.0.1"
fi

echo "==> Serial udev rule (stable /dev/tmv71)"
cp "$ROOT/deploy/99-tmv71-serial.rules" /etc/udev/rules.d/
udevadm control --reload && udevadm trigger || true

echo "==> systemd service"
cp "$ROOT/deploy/systemd/tmv71-remote.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tmv71-remote.service

echo "==> Done. Open https://<pi-ip>:8443/"
echo "    Audio: click VERBINDEN in the web UI and allow the microphone."

# TM-V71 Remote

Modern web-based remote control for the **Kenwood TM-V71(A/E)** dual-band FM
transceiver — comparable to RigPi, but built around a direct serial driver and a
clean, dependency-light stack. Full radio control in the browser, direct two-way
browser audio (WebRTC/Opus), and complete memory-channel management.

![TM-V71 Remote — dark theme](docs/preview.png)

*A light and a dark theme are both included — switch between them any time with
the theme toggle in the header (light is the default).*

![TM-V71 Remote — light theme](docs/preview-light.png)

> Built for a Raspberry Pi with the radio on `/dev/ttyUSB0` (FTDI programming
> cable) at 57600 baud and a USB sound interface wired to the radio's data /
> mic / speaker connections.

---

## Features

- **Full live control** — both bands (A/B): frequency, VFO/memory mode, repeater
  shift & offset, CTCSS/DCS tone, step, control-band selection, and **PTT over
  CAT** (no separate PTT line).
- **Memory channels (CHIRP-level)** — read, write, delete and rename any of the
  1000 channels, plus CSV import/export.
- **Live status** — frequency, mode, tone, squelch/busy push to the browser over
  a WebSocket; transmit state lights the whole UI.
- **Two-way audio (direct WebRTC)** — the radio's RX/TX audio is bridged
  straight to the browser over **WebRTC/Opus** via `aiortc` — no extra app
  or proxy. Listen in the browser; the mic is fed to the radio only
  while PTT is engaged.
- **No build step for the control UI** — the SPA is plain HTML/CSS/JS served
  directly by the backend. No Node toolchain required on the Pi.

## Why not hamlib?

hamlib's TM-V71 backends are unreliable in practice (model `2034` rejects the
reply termination, model `2035` hangs). The radio's documented PC command set,
however, works perfectly over a direct serial connection and exposes the radio's
*full* feature set — including per-channel memory programming, which hamlib does
not. So the backend owns the serial port directly (`backend/app/tmv71.py`).
Protocol reference: [LA3QMA/TM-V71_TM-D710-Kenwood](https://github.com/LA3QMA/TM-V71_TM-D710-Kenwood).

## Architecture

```
                          Raspberry Pi
 ┌───────────────────────────────────────────────────────────────┐
 │  /dev/ttyUSB0 (57600) ─ tmv71 driver ─┐                       │
 │                                       ▼                       │
 │   FastAPI backend ── REST + WebSocket (live status)           │
 │     • control (freq/mode/band/PTT)   • memory CRUD + CSV      │
 │     • PTT couples the audio bridge                            │
 │                                                               │
 │   USB sound ── aiortc WebRTC ◄──► browser (Opus, PTT-gated)   │
 │     (RX→browser track, browser mic→radio mic)                 │
 │                                                               │
 │   FastAPI serves the SPA at "/" and the WebRTC signalling at  │
 │   "/api/webrtc/offer" (SDP offer/answer, same origin/TLS)     │
 └───────────────────────────────────────────────────────────────┘
        ▲ LAN (HTTPS)
   Web browser (control + audio)   ·   later: Flutter app
```

## Requirements

- Raspberry Pi (tested on Debian 13 / aarch64), Python 3.11+
- Kenwood TM-V71(A/E) on a serial port (FTDI programming cable)
- A USB sound interface wired to the radio (data port or mic/speaker), full-duplex
- System packages: `portaudio19-dev` (sounddevice), `swig` + `liblgpio-dev`
  (build `lgpio` for the optional GPIO power switch). WebRTC/Opus audio is
  in-process via `aiortc` (pip) — no audio server needed.

### Python dependencies

Installed from [`backend/requirements.txt`](backend/requirements.txt):

| Package | Purpose |
| --- | --- |
| `fastapi`, `uvicorn[standard]` | REST + WebSocket server (HTTPS/TLS) |
| `pydantic`, `pydantic-settings` | request/response models, config |
| `pyserial` | serial CAT link to the radio |
| `python-multipart` | file uploads (logo, CSV import) |
| `aiortc` | **two-way browser audio over WebRTC/Opus** |
| `sounddevice` | USB sound-card I/O (PortAudio); `av`/`numpy` pulled in for frames |
| `numpy` | audio sample processing (levels, gain, tones) |
| `gpiozero`, `lgpio` | optional GPIO power switch (Raspberry Pi) |

## Radio setup

On the TM-V71(A/E), set the menu items:

- **519 (PC port baud rate) → 57600** — the CAT/serial rate this app uses
  (matches `TMV71_SERIAL_BAUD`).
- **518 (data speed) → 1200**.

Menu 518 does **not** just change input sensitivity — it switches the whole audio
path to/from the rear data connector. **1200** routes through the normal,
band-limited audio chain (RX after de-emphasis, TX with pre-emphasis and the
limiter), i.e. the same character as the speaker/mic. **9600** uses the flat,
wideband direct-FM path (straight off the FM discriminator on RX, flat modulation
on TX) and is only for true 9600-baud G3RUH FSK packet. For a USB sound interface
carrying voice and ordinary soundcard modes (FT8, SSTV, APRS-1200, Echolink) use
**1200** — 9600 sounds harsh/dull for voice and needs different levels. The
differing input sensitivity (≈0.4 Vpp vs ≈2 Vpp) is a consequence of this path
switch, not its purpose.

Accordingly, take the **RX audio** into the USB sound interface from the
**1200-baud** audio pin (`PR1`) on the rear data connector (mini-DIN), **not** the
9600-baud pin — it is filtered, line-level audio.

## Install

```bash
sudo apt-get install -y portaudio19-dev python3-venv swig liblgpio-dev

git clone https://github.com/CQ-DJ0SH/tmv71-remote.git
cd tmv71-remote/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Add the service user to the `dialout` (serial) and `audio` groups.

## Run

Browser microphone access (`getUserMedia`) requires a **secure context**, so the
server is run over **HTTPS**. Generate a self-signed certificate (use your Pi's
LAN IP in the SAN) and start uvicorn with TLS:

```bash
cd backend
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/key.pem -out certs/cert.pem -subj "/CN=tmv71-remote" \
  -addext "subjectAltName=IP:<pi-ip>,DNS:localhost,IP:127.0.0.1"

.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem
```

Open **`https://<pi-ip>:8000/`** and accept the self-signed certificate once.
This single process serves radio control, live status and the WebRTC audio
signalling — no extra services. For a reboot-proof setup see [`deploy/`](deploy/).

> Plain HTTP also works for control, but browser audio needs HTTPS (or a browser
> exception for the origin) because `getUserMedia` requires a secure context.

## Audio

Two-way audio is **direct WebRTC** between the browser and the backend (via
`aiortc`), using the **Opus** codec — no extra audio server or proxy. The backend bridges
the radio's USB sound device to a WebRTC peer: RX audio is sent to the browser,
and the browser microphone is fed to the radio's mic input **only while PTT is
engaged** (keyed from the web UI).

In the web UI, open the **AUDIO** panel, click **CONNECT** and allow the
microphone. Listen there; hold the large **PTT** button to transmit.

> ⚠️ Only transmit into a dummy load or with a valid amateur radio licence.

## Configuration

All settings are overridable via `TMV71_*` environment variables (see
[`backend/.env.example`](backend/.env.example)). Common ones:

| Variable | Default | Meaning |
|---|---|---|
| `TMV71_SERIAL_PORT` | `/dev/ttyUSB0` | radio serial device |
| `TMV71_SERIAL_BAUD` | `57600` | serial baud rate |
| `TMV71_PORT` | `8000` | HTTP port |
| `TMV71_AUDIO_DEVICE` | `NAD` | substring matched against the USB sound device |
| `TMV71_RX_GAIN` / `TMV71_TX_GAIN` | `1.0` | digital audio gain |
| `TMV71_AUDIO_ENABLED` | `true` | open the audio device / WebRTC bridge |
| `TMV71_GPIO_POWER_PIN` | _(unset)_ | BCM pin for the GPIO power relay |

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/status` | full radio status snapshot |
| `POST` | `/api/frequency` | `{band, freq_hz}` set VFO frequency |
| `POST` | `/api/band-mode` | `{band, mode}` 0=VFO 1=memory 2=call |
| `POST` | `/api/control-band` | `{control_band}` |
| `POST` | `/api/recall` | `{band, channel}` recall a memory channel |
| `POST` | `/api/ptt` | `{transmit}` key/unkey TX (gates mic→radio) |
| `POST` | `/api/power` | `{band, level}` TX power 0=50W/1=10W/2=5W |
| `POST` | `/api/squelch` | `{band, level}` squelch 0–31 |
| `POST` | `/api/band-display` | `{single, band}` dual/single band (DL) |
| `GET` | `/api/memories?start&end` | list populated channels |
| `GET`/`PUT`/`DELETE` | `/api/memories/{ch}` | read / write / delete a channel |
| `GET` | `/api/memories.csv` | CSV export |
| `POST` | `/api/memories/import` | CSV import (multipart) |
| `GET` | `/api/audio/status` | WebRTC audio bridge status + levels |
| `POST` | `/api/webrtc/offer` | WebRTC SDP offer → answer (browser audio) |
| `WS` | `/ws` | live status stream |

## Security

LAN-only by design. There is no authentication. Do **not** expose port 8000
directly to the internet — use a VPN (e.g. WireGuard/Tailscale) or a reverse
proxy with TLS + auth.

## Status & roadmap

- ✅ Serial control, memory management, live status, web UI
- ✅ Direct two-way browser audio over WebRTC/Opus (`aiortc`)
- ✅ GPIO power switch, single-band (DL), TX power, squelch, in-display S-meter
- ✅ systemd packaging (see [`deploy/`](deploy/))
- ⏳ Flutter Android app (reuses this REST + WebSocket API)

## Credits

- Kenwood PC protocol docs — [LA3QMA/TM-V71_TM-D710-Kenwood](https://github.com/LA3QMA/TM-V71_TM-D710-Kenwood)
- [aiortc](https://github.com/aiortc/aiortc) (WebRTC/Opus), [sounddevice](https://python-sounddevice.readthedocs.io/)
- Fonts: Saira, IBM Plex Mono, DSEG (7-segment)

## License

GNU GPL v3 — see [LICENSE](LICENSE).

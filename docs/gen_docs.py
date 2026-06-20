#!/usr/bin/env python3
"""Generate the project documentation PDFs (English + German) into docs/.

Pure-Python via fpdf2 (no LaTeX). Run:  .venv/bin/python docs/gen_docs.py
"""
import os
from fpdf import FPDF

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONTB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONTI = FONT   # DejaVu ships no Oblique here; reuse the regular face for "I"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

ACCENT = (16, 110, 78)        # muted green
DARK = (28, 39, 49)
GREY = (110, 122, 132)
CODEBG = (244, 246, 248)
VERSION = "3.1"


class Doc(FPDF):
    title_txt = ""

    def footer(self):
        self.set_y(-12)
        self.set_font("DV", "", 8)
        self.set_text_color(*GREY)
        self.cell(0, 8, self.title_txt, align="L")
        self.cell(0, 8, f"{self.page_no()}", align="R", new_x="LMARGIN", new_y="TOP")


def new_pdf(title):
    pdf = Doc(orientation="P", unit="mm", format="A4")
    pdf.title_txt = title
    pdf.add_font("DV", "", FONT)
    pdf.add_font("DV", "B", FONTB)
    pdf.add_font("DV", "I", FONTI)
    pdf.add_font("MN", "", MONO)
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, margin=16)
    return pdf


def cover(pdf, title, subtitle, lang):
    pdf.add_page()
    pdf.ln(40)
    img = os.path.join(HERE, "preview.png")
    pdf.set_font("DV", "B", 30)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 16, "TM-V71 Remote", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DV", "", 14)
    pdf.set_text_color(*DARK)
    pdf.cell(0, 10, title, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DV", "", 11)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 8, subtitle, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, f"Version {VERSION}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    if os.path.exists(img):
        try:
            pdf.image(img, x=33, w=144)
        except Exception:
            pass
    pdf.ln(6)
    pdf.set_font("DV", "I", 9)
    pdf.set_text_color(*GREY)
    note = ("Kenwood TM-V71(A/E) web remote + WebRTC audio + HackRF panadapter, "
            "for a Raspberry Pi.") if lang == "en" else \
           ("Web-Fernsteuerung für Kenwood TM-V71(A/E) mit WebRTC-Audio und "
            "HackRF-Panadapter, für den Raspberry Pi.")
    pdf.multi_cell(0, 5, note, align="C")


def render(pdf, blocks):
    for kind, *rest in blocks:
        if kind == "h1":
            pdf.add_page()
            pdf.set_font("DV", "B", 18)
            pdf.set_text_color(*ACCENT)
            pdf.multi_cell(0, 9, rest[0])
            pdf.set_draw_color(*ACCENT)
            pdf.set_line_width(0.4)
            y = pdf.get_y() + 1
            pdf.line(18, y, 192, y)
            pdf.ln(4)
        elif kind == "h2":
            pdf.ln(2)
            pdf.set_font("DV", "B", 13)
            pdf.set_text_color(*DARK)
            pdf.multi_cell(0, 7, rest[0])
            pdf.ln(1)
        elif kind == "p":
            pdf.set_font("DV", "", 10.5)
            pdf.set_text_color(*DARK)
            pdf.multi_cell(0, 5.6, rest[0])
            pdf.ln(1.5)
        elif kind == "ul":
            pdf.set_font("DV", "", 10.5)
            pdf.set_text_color(*DARK)
            for item in rest[0]:
                x = pdf.get_x()
                pdf.set_x(22)
                pdf.set_text_color(*ACCENT)
                pdf.cell(4, 5.4, "•")
                pdf.set_text_color(*DARK)
                pdf.multi_cell(0, 5.4, item)
                pdf.set_x(x)
            pdf.ln(1.5)
        elif kind == "code":
            pdf.set_font("MN", "", 8.7)
            pdf.set_fill_color(*CODEBG)
            pdf.set_text_color(40, 50, 58)
            pdf.multi_cell(0, 4.6, rest[0], fill=True, border=0)
            pdf.ln(2)
        elif kind == "img":
            path = os.path.join(HERE, rest[0])
            if os.path.exists(path):
                try:
                    pdf.image(path, w=rest[1] if len(rest) > 1 else 150)
                    pdf.ln(2)
                except Exception:
                    pass
        elif kind == "space":
            pdf.ln(rest[0] if rest else 3)


ARCH = (
    "                       Raspberry Pi\n"
    " /dev/ttyUSB0 (57600) -- tmv71 driver --+\n"
    "                                        v\n"
    "  FastAPI backend -- REST + WebSocket (live status)\n"
    "    - control (freq/mode/band/PTT)  - memory CRUD + CSV\n"
    "    - CW/RTTY + 5-tone selcall      - HackRF spectrum\n"
    "                                                       \n"
    "  USB sound -- aiortc WebRTC <-> browser (Opus, PTT)\n"
    "  FastAPI serves the SPA / PWA at \"/\" (HTTPS/TLS)\n"
    "        ^ LAN (HTTPS)\n"
    "   Browser  -  installable PWA (control + audio)\n"
)

API_CORE = (
    "GET  /api/status            live radio state\n"
    "POST /api/frequency         set VFO frequency\n"
    "POST /api/band-mode         VFO / memory / call\n"
    "POST /api/control-band      select control band\n"
    "POST /api/ptt               key / un-key (CAT)\n"
    "POST /api/ptt-band          select TX band\n"
    "POST /api/squelch /step     squelch level / step\n"
    "POST /api/vfo               shift/offset/tone/bw\n"
    "GET  /api/info /version     rig + app info\n"
    "WS   /ws                    live status stream\n"
)
API_MEM = (
    "GET    /api/memories?start&end   list channels\n"
    "GET    /api/memories/{ch}        one channel\n"
    "PUT    /api/memories/{ch}        write channel\n"
    "DELETE /api/memories/{ch}        clear channel\n"
    "GET    /api/memories.csv         export CSV\n"
    "POST   /api/memories/import      import CSV\n"
    "POST   /api/recall               recall to band\n"
)
API_AUDIO = (
    "POST /api/webrtc/offer      WebRTC SDP offer\n"
    "GET  /api/audio/status      RX/TX levels, flags\n"
    "GET  /api/audio/devices     list sound devices\n"
    "POST /api/audio/device      pick device\n"
    "POST /api/audio/gain        rx_gain / tx_gain\n"
    "POST /api/audio/buffer      tx buffer / ptt tail\n"
    "POST /api/audio/tones       roger/test/lowpass/mic-test\n"
    "GET/POST /api/audio/mixer   USB card mixer\n"
)
API_DIGI = (
    "GET  /api/digi              CW/RTTY status\n"
    "POST /api/digi/config       mode + parameters\n"
    "POST /api/digi/tx           encode + transmit\n"
    "WS   /ws/digi               decoded text stream\n"
    "GET  /api/selcall           5-tone status\n"
    "POST /api/selcall/config    standard / tone / own\n"
    "POST /api/selcall/tx        send a 5-tone call\n"
    "WS   /ws/selcall            decoded calls stream\n"
)
API_SDR = (
    "GET  /api/hackrf            SDR status\n"
    "POST /api/hackrf/start|stop|config\n"
    "WS   /ws/hackrf             spectrum/waterfall frames\n"
    "GET  /api/scan  POST /api/scan/start|stop  band scan\n"
)
API_SYS = (
    "GET/POST /api/power-switch   GPIO power\n"
    "POST /api/gpio-config        set GPIO pin\n"
    "POST /api/auto-power-off     idle auto-off\n"
    "GET/POST /api/serial-config  serial port/baud\n"
    "GET/POST /api/callsign /theme\n"
    "GET  /api/system             Pi host metrics\n"
    "GET/POST /api/update         GitHub self-update\n"
)

INSTALL = (
    "sudo apt-get install -y portaudio19-dev python3-venv swig liblgpio-dev\n"
    "git clone https://github.com/CQ-DJ0SH/tmv71-remote.git\n"
    "cd tmv71-remote/backend\n"
    "python3 -m venv .venv\n"
    ".venv/bin/pip install -r requirements.txt\n"
)
RUNTLS = (
    "cd backend && mkdir -p certs\n"
    "openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \\\n"
    "  -keyout certs/key.pem -out certs/cert.pem -subj \"/CN=tmv71-remote\" \\\n"
    "  -addext \"subjectAltName=IP:<pi-ip>,DNS:localhost,IP:127.0.0.1\"\n"
    ".venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8443 \\\n"
    "  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem\n"
)
CA = (
    "cd backend/certs && mkdir -p ca\n"
    "# 1) Root CA (10 years) — keep ca.key secret\n"
    "openssl genrsa -out ca/ca.key 4096\n"
    "openssl req -x509 -new -key ca/ca.key -sha256 -days 3650 \\\n"
    "  -subj \"/CN=TM-V71 Remote Root CA\" \\\n"
    "  -addext \"basicConstraints=critical,CA:TRUE,pathlen:0\" \\\n"
    "  -addext \"keyUsage=critical,keyCertSign,cRLSign\" -out ca/ca.crt\n"
    "# 2) server cert signed by the CA (list every name/IP in the SAN)\n"
    "openssl genrsa -out key.pem 2048\n"
    "openssl req -new -key key.pem -subj \"/CN=tm-v71.example.lan\" -out s.csr\n"
    "openssl x509 -req -in s.csr -CA ca/ca.crt -CAkey ca/ca.key \\\n"
    "  -CAcreateserial -days 825 -sha256 -extfile leaf.ext -out cert.pem\n"
    "sudo systemctl restart tmv71-remote.service\n"
)
ENVV = (
    "TMV71_SERIAL_PORT=/dev/ttyUSB0   TMV71_SERIAL_BAUD=57600\n"
    "TMV71_HOST=0.0.0.0               TMV71_PORT=8443\n"
    "TMV71_AUDIO_DEVICE=NAD           TMV71_AUDIO_ENABLED=true\n"
    "TMV71_GPIO_POWER_PIN=17          TMV71_CALLSIGN=DJ0SH\n"
    "TMV71_SSL_CERTFILE=...  TMV71_SSL_KEYFILE=...\n"
)

# ---------------------------------------------------------------- English
EN = [
    ("h1", "1  Overview"),
    ("p", "TM-V71 Remote is a modern, dependency-light web remote control for the "
          "Kenwood TM-V71(A/E) dual-band FM transceiver, built around a direct "
          "serial driver. It gives full radio control in the browser, two-way "
          "browser audio over WebRTC/Opus, complete memory-channel management, an "
          "optional HackRF panadapter, classic 5-tone selective calling, and a "
          "CW/RTTY digimodes decoder/encoder. It installs as a Progressive Web "
          "App (PWA) and is designed to run on a Raspberry Pi."),
    ("p", "Unlike hamlib, whose TM-V71 backends are unreliable, this project speaks "
          "the radio's documented PC command set directly and exposes the radio's "
          "full feature set, including per-channel memory programming."),
    ("h1", "2  Features"),
    ("ul", [
        "Full live control of both bands (A/B): frequency, VFO/memory mode, "
        "repeater shift & offset, CTCSS/DCS, step, control band, and PTT over CAT.",
        "Memory channels (CHIRP-level): read, write, delete, rename any of the "
        "1000 channels, plus CSV import/export.",
        "Live status pushed to the browser over a WebSocket; transmit lights the UI.",
        "Two-way audio: direct WebRTC/Opus between browser and backend via aiortc; "
        "the mic feeds the radio only while PTT is engaged.",
        "Optional HackRF One waterfall: real-time panadapter (auto-following the "
        "tuned frequency) or a wideband sweep.",
        "Classic 5-tone selective calling (ZVEI-1/2, CCIR, EEA): call, decode, and "
        "mute RX until your own ID is received.",
        "CW (Morse) and RTTY (Baudot/AFSK) decode + encode, over the FM audio path.",
        "Installable PWA with a mobile landscape swipe-deck layout.",
        "Resilient operation: the phone screen is kept awake, browser audio "
        "auto-reconnects after a network glitch, and a backend watchdog releases "
        "a latched PTT if every client disappears.",
        "GPIO power switch, auto power-off, TX power, squelch, in-display S-meter.",
        "Two themes (dark/light); no build step for the UI.",
    ]),
    ("h1", "3  Architecture"),
    ("code", ARCH),
    ("p", "The backend owns the serial port directly (backend/app/tmv71.py). One "
          "FastAPI process serves the SPA/PWA, the REST control endpoints, the "
          "live-status WebSocket, and the WebRTC audio signalling — no extra "
          "services. Audio is 48 kHz / 16-bit / mono internally (Opus' native "
          "rate)."),
    ("h1", "4  Requirements & Hardware"),
    ("ul", [
        "Raspberry Pi (tested on Debian 13 / aarch64), Python 3.11+.",
        "Kenwood TM-V71(A/E) on a serial port (FTDI programming cable), 57600 baud.",
        "A USB sound interface wired to the radio (data port or mic/speaker), "
        "full-duplex.",
        "System packages: portaudio19-dev, swig + liblgpio-dev (optional GPIO).",
        "Optional: a HackRF One plus the hackrf host tools for the waterfall.",
    ]),
    ("h1", "5  Installation"),
    ("code", INSTALL),
    ("p", "For a reboot-proof setup, install the systemd unit from the deploy/ "
          "directory. The service runs uvicorn with TLS on port 8443."),
    ("h1", "6  Running over HTTPS"),
    ("p", "Browser microphone access (getUserMedia) and the PWA service worker "
          "require a secure context, so the server runs over HTTPS. A quick "
          "self-signed certificate is enough on the desktop (accept the warning "
          "once); for installing the PWA on a phone you need a trusted certificate "
          "(see chapter 9)."),
    ("code", RUNTLS),
    ("p", "Open https://<pi-ip>:8443/ and accept the certificate once."),
    ("h1", "7  The Web Interface"),
    ("h2", "Band panels (VFO A / VFO B)"),
    ("p", "Each band shows the frequency on a 7-segment display with an S-meter "
          "(1 s peak-hold), plus controls for VFO/memory mode, CTRL/PTT band "
          "selection, TX power, squelch, repeater shift/offset, tone and "
          "bandwidth. The digit tuner lets you click bars above/below each digit "
          "to step the frequency; AIR Band tunes band A to the 118–137 MHz air "
          "band (receive-only)."),
    ("h2", "PTT & memory quick keys"),
    ("p", "Hold the large PTT button (or the space bar) to transmit; PTT-LOCK "
          "latches transmit. ROGER adds a beep on release; the 1750 Hz button "
          "arms a tone-call. The left column recalls memory channels 0–9 (the "
          "loaded channel's key glows); the right column sends DTMF memories. On "
          "mobile, mini RX/TX VU bars with peak-hold flank the button."),
    ("h2", "Audio (WebRTC/Opus)"),
    ("p", "Open the AUDIO panel, pick the audio band, click CONNECT and allow the "
          "microphone. RX and mic levels are shown live. Controls: RX/TX gain, a "
          "MIC TEST switch (meter the mic without keying — it records while on and "
          "replays your audio over RX when switched off, so you can hear how you "
          "sound, with no RF; the radio's RX is muted during the test), "
          "switchable TX and RX voice low-pass filters "
          "(≤ 3.5 kHz), a two-tone test, and TX timing (buffer / trail). The USB "
          "card mixer is in Settings > Audio."),
    ("h2", "HackRF waterfall"),
    ("p", "If a HackRF One is connected, this panel shows a live spectrum stacked "
          "over a waterfall: a panadapter centred on the tuned frequency "
          "(auto-following) or a wideband sweep. Receive-only; LNA/VGA gains and a "
          "display level are adjustable."),
    ("h2", "Selcall (classic 5-tone)"),
    ("p", "Send and decode classic selective calls (ZVEI-1/2, CCIR, EEA). Enter a "
          "5-digit CALL code and press CALL (keys PTT). Enter your own ID and press "
          "MUTE to silence RX until your ID is received — then it un-mutes "
          "automatically. Over FM this is AFSK; use a dummy load when setting up."),
    ("h2", "Digimodes (CW / RTTY)"),
    ("p", "Switch between CW (Morse) and RTTY (Baudot/AFSK). DECODE shows received "
          "text; type into the field and SEND to transmit (keys PTT). Parameters: "
          "CW WPM/pitch — with an optional AUTO mode that tracks the received "
          "speed — and RTTY baud/shift/mark. Over the FM radio this is MCW / "
          "AFSK — not native HF CW/RTTY."),
    ("h2", "Band scan"),
    ("p", "Sweep a VHF/UHF range or the memory bank and see an occupancy "
          "spectrum + waterfall. Double-click a channel to tune the control VFO "
          "to it."),
    ("h2", "Settings"),
    ("p", "Tabs: General (callsign, API backend URL, serial port/baud, GPIO power, "
          "auto power-off, logo, GitHub self-update, Root-CA download), Audio "
          "(device, USB mixer, voice filters, test tone, TX timing), Rig-Info, "
          "Rig-Memory, Rig-DTMF, and Pi-Hardware (host metrics)."),
    ("h1", "8  Mobile App (PWA)"),
    ("p", "The UI installs as a Progressive Web App: full-screen, with an "
          "app-shell service worker for instant launch. On phones the panels become "
          "a vertical swipe deck — swipe up/down, one panel per screen (this keeps "
          "the deck's scroll axis off the horizontal sliders, so they stay usable) "
          "— with the title bar as a slim vertical strip on the left and an icon "
          "tab rail on the right. Past the last panel is an info page listing the "
          "app version and browser/environment details. The "
          "app is forced to landscape; portrait shows a rotate hint. Install via the "
          "browser menu (Install / Add to Home Screen); on iOS use Safari > Share."),
    ("p", "While the app is open the phone screen is kept awake via the Screen Wake "
          "Lock API, so it won't dim or lock mid-QSO. The browser audio link "
          "reconnects automatically after a brief network interruption, and if the "
          "connection to the backend is lost while transmit is latched, the PTT is "
          "released (locally and by a backend watchdog) so the rig can never stay "
          "keyed unattended."),
    ("img", "pwa-ptt-dark.png", 150),
    ("h1", "9  Trusted Certificate (Root CA)"),
    ("p", "A self-signed certificate is fine on the desktop, but mobile browsers "
          "will not install the PWA or run the service worker without a trusted "
          "certificate. Create your own root CA, sign the server certificate with "
          "it, and trust the CA on the phone. A download link for the CA appears in "
          "Settings > General once it exists."),
    ("code", CA),
    ("p", "Install ca/ca.crt on the phone (Settings > Security > Install a "
          "certificate > CA certificate). The leaf is valid 825 days; re-issue it "
          "from the same CA without re-installing on phones. Keep ca/ca.key secret."),
    ("h1", "10  Configuration"),
    ("p", "Settings are read from environment variables (prefix TMV71_) or a .env "
          "file, with web-UI changes persisted to backend/app/runtime.json. Key "
          "variables:"),
    ("code", ENVV),
    ("h1", "11  REST & WebSocket API"),
    ("h2", "Control & status"), ("code", API_CORE),
    ("h2", "Memory channels"), ("code", API_MEM),
    ("h2", "Audio"), ("code", API_AUDIO),
    ("h2", "Digimodes & selcall"), ("code", API_DIGI),
    ("h2", "SDR & scan"), ("code", API_SDR),
    ("h2", "System & power"), ("code", API_SYS),
    ("h1", "12  Troubleshooting"),
    ("ul", [
        "No CAT / 'radio offline': check the serial port and baud in Settings; the "
        "FTDI cable must be on /dev/ttyUSB0 (or set the right port).",
        "No audio: ensure HTTPS, click CONNECT and allow the mic; check the USB "
        "card and its mixer levels (playback drives the radio mic on TX).",
        "PWA won't install / theme not switching on a phone: the certificate is "
        "not trusted — install the Root CA (chapter 9).",
        "RX filter only on one channel: reconnect audio (Disconnect/Connect) to "
        "renegotiate Opus to mono.",
        "Decoders need a clean signal; tune levels and (for RTTY) the mark tone.",
    ]),
    ("h1", "13  Security"),
    ("p", "LAN-only by design; there is no authentication. Do not expose the port "
          "directly to the internet — use a VPN (WireGuard/Tailscale) or a reverse "
          "proxy with TLS + auth. The Root CA private key never leaves the Pi."),
    ("h1", "14  Credits & License"),
    ("p", "Kenwood PC protocol docs: LA3QMA/TM-V71_TM-D710-Kenwood. Built with "
          "aiortc (WebRTC/Opus) and sounddevice. Fonts: Saira, IBM Plex Mono, "
          "DSEG (7-segment), Neuropol (title). See the repository for license "
          "details."),
]

# ---------------------------------------------------------------- German
DE = [
    ("h1", "1  Überblick"),
    ("p", "TM-V71 Remote ist eine moderne, schlanke Web-Fernsteuerung für den "
          "Kenwood TM-V71(A/E) Dualband-FM-Transceiver, aufgebaut auf einem "
          "direkten seriellen Treiber. Sie bietet volle Gerätesteuerung im Browser, "
          "Zwei-Wege-Audio über WebRTC/Opus, vollständige Speicherkanal-Verwaltung, "
          "einen optionalen HackRF-Panadapter, klassischen 5-Ton-Selektivruf sowie "
          "einen CW/RTTY-Decoder/Encoder. Sie lässt sich als Progressive Web App "
          "(PWA) installieren und ist für den Raspberry Pi ausgelegt."),
    ("p", "Anders als hamlib (dessen TM-V71-Backends unzuverlässig sind) spricht "
          "dieses Projekt den dokumentierten PC-Befehlssatz des Geräts direkt an "
          "und erschließt den vollen Funktionsumfang, inklusive der "
          "Speicherkanal-Programmierung."),
    ("h1", "2  Funktionen"),
    ("ul", [
        "Volle Live-Steuerung beider Bänder (A/B): Frequenz, VFO-/Speichermodus, "
        "Relais-Shift & Offset, CTCSS/DCS, Schrittweite, Steuerband, PTT über CAT.",
        "Speicherkanäle (CHIRP-Niveau): Lesen, Schreiben, Löschen, Umbenennen aller "
        "1000 Kanäle, plus CSV-Import/-Export.",
        "Live-Status per WebSocket an den Browser; beim Senden leuchtet die UI.",
        "Zwei-Wege-Audio: direktes WebRTC/Opus zwischen Browser und Backend via "
        "aiortc; das Mikrofon speist das Funkgerät nur bei gedrücktem PTT.",
        "Optionaler HackRF-One-Wasserfall: Echtzeit-Panadapter (folgt der "
        "Frequenz) oder Breitband-Sweep.",
        "Klassischer 5-Ton-Selektivruf (ZVEI-1/2, CCIR, EEA): rufen, dekodieren "
        "und RX stummschalten bis zum eigenen Ruf.",
        "CW (Morse) und RTTY (Baudot/AFSK) dekodieren + senden über den FM-Audioweg.",
        "Installierbare PWA mit mobilem Querformat-Swipe-Deck.",
        "Robuster Betrieb: der Handy-Bildschirm bleibt an, das Browser-Audio "
        "verbindet sich nach einer Netzstörung automatisch neu, und ein "
        "Backend-Watchdog beendet ein eingerastetes PTT, wenn alle Clients "
        "verschwinden.",
        "GPIO-Power-Schalter, Auto-Abschaltung, TX-Leistung, Squelch, S-Meter.",
        "Zwei Themes (dunkel/hell); kein Build-Schritt für die Oberfläche.",
    ]),
    ("h1", "3  Architektur"),
    ("code", ARCH),
    ("p", "Das Backend besitzt die serielle Schnittstelle direkt "
          "(backend/app/tmv71.py). Ein einziger FastAPI-Prozess liefert die "
          "SPA/PWA, die REST-Steuerendpunkte, den Live-Status-WebSocket und die "
          "WebRTC-Signalisierung — ohne Zusatzdienste. Audio ist intern "
          "48 kHz / 16 Bit / mono (Opus-Standardrate)."),
    ("h1", "4  Voraussetzungen & Hardware"),
    ("ul", [
        "Raspberry Pi (getestet auf Debian 13 / aarch64), Python 3.11+.",
        "Kenwood TM-V71(A/E) an einer seriellen Schnittstelle (FTDI-Kabel), "
        "57600 Baud.",
        "Ein USB-Audiointerface, am Funkgerät verdrahtet (Datenbuchse oder "
        "Mic/Speaker), vollduplex.",
        "Systempakete: portaudio19-dev, swig + liblgpio-dev (optional GPIO).",
        "Optional: ein HackRF One plus die hackrf-Hosttools für den Wasserfall.",
    ]),
    ("h1", "5  Installation"),
    ("code", INSTALL),
    ("p", "Für einen neustartfesten Betrieb die systemd-Unit aus dem Ordner "
          "deploy/ installieren. Der Dienst startet uvicorn mit TLS auf Port 8443."),
    ("h1", "6  Betrieb über HTTPS"),
    ("p", "Der Mikrofonzugriff des Browsers (getUserMedia) und der "
          "PWA-Service-Worker benötigen einen sicheren Kontext, daher läuft der "
          "Server über HTTPS. Ein schnelles selbstsigniertes Zertifikat genügt am "
          "Desktop (Warnung einmal bestätigen); für die PWA-Installation auf dem "
          "Handy ist ein vertrauenswürdiges Zertifikat nötig (Kapitel 9)."),
    ("code", RUNTLS),
    ("p", "https://<pi-ip>:8443/ öffnen und das Zertifikat einmal akzeptieren."),
    ("h1", "7  Die Weboberfläche"),
    ("h2", "Band-Panels (VFO A / VFO B)"),
    ("p", "Jedes Band zeigt die Frequenz auf einer 7-Segment-Anzeige mit S-Meter "
          "(1 s Peak-Hold) sowie Bedienelemente für VFO-/Speichermodus, CTRL-/"
          "PTT-Bandwahl, TX-Leistung, Squelch, Relais-Shift/Offset, Ton und "
          "Bandbreite. Über die Ziffern-Abstimmung lässt sich jede Stelle per "
          "Klick auf die Balken hoch/runter stellen; AIR Band stellt Band A auf "
          "das Flugfunkband 118–137 MHz (nur Empfang)."),
    ("h2", "PTT & Speicher-Schnelltasten"),
    ("p", "Den großen PTT-Knopf (oder die Leertaste) halten zum Senden; PTT-LOCK "
          "rastet den Sendebetrieb ein. ROGER fügt beim Loslassen einen Piep "
          "hinzu; die 1750-Hz-Taste schärft einen Tonruf. Die linke Spalte ruft "
          "die Speicherkanäle 0–9 ab (die Taste des geladenen Kanals leuchtet); "
          "die rechte Spalte sendet DTMF-Speicher. Auf dem Handy flankieren "
          "Mini-RX/TX-VU-Bars mit Peak-Hold den Knopf."),
    ("h2", "Audio (WebRTC/Opus)"),
    ("p", "Das AUDIO-Panel öffnen, das Audio-Band wählen, CONNECT klicken und das "
          "Mikrofon erlauben. RX- und Mic-Pegel werden live angezeigt. "
          "Bedienelemente: RX/TX-Gain, ein MIC-TEST-Schalter (Mic messen ohne zu "
          "tasten — nimmt im Betrieb auf und spielt die Aufnahme beim Ausschalten "
          "über RX zurück, sodass man sich selbst hört, ganz ohne HF; das "
          "RX-Rauschen des Funkgeräts ist während des Tests stumm), zuschaltbare "
          "TX- und RX-Sprachtiefpässe (≤ 3,5 kHz), ein Zweiton-Test sowie TX-Timing "
          "(Buffer/Trail). Der USB-Mixer liegt unter Einstellungen > Audio."),
    ("h2", "HackRF-Wasserfall"),
    ("p", "Ist ein HackRF One angeschlossen, zeigt dieses Panel ein Live-Spektrum "
          "über einem Wasserfall: ein Panadapter zentriert auf der Frequenz "
          "(folgt automatisch) oder ein Breitband-Sweep. Nur Empfang; LNA/VGA und "
          "ein Anzeigepegel sind einstellbar."),
    ("h2", "Selektivruf (klassisch, 5-Ton)"),
    ("p", "Klassische Selektivrufe senden und dekodieren (ZVEI-1/2, CCIR, EEA). "
          "Einen 5-stelligen CALL-Code eingeben und CALL drücken (tastet PTT). Den "
          "eigenen Code (MY ID) eingeben und MUTE drücken, um RX stumm zu schalten, "
          "bis der eigene Ruf empfangen wird — dann wird automatisch entstummt. "
          "Über FM ist das AFSK; zum Einstellen einen Dummy-Load verwenden."),
    ("h2", "Digimodes (CW / RTTY)"),
    ("p", "Umschalten zwischen CW (Morse) und RTTY (Baudot/AFSK). DECODE zeigt den "
          "empfangenen Text; in das Feld tippen und mit SEND senden (tastet PTT). "
          "Parameter: CW WpM/Tonhöhe — mit optionalem AUTO-Modus, der die "
          "empfangene Geschwindigkeit nachführt — sowie RTTY Baud/Shift/Mark. Über "
          "das FM-Gerät ist das MCW / AFSK — kein echtes HF-CW/RTTY."),
    ("h2", "Bandscan"),
    ("p", "Einen VHF/UHF-Bereich oder die Speicherbank absuchen und ein "
          "Belegungs-Spektrum + Wasserfall sehen. Ein Doppelklick auf einen Kanal "
          "stimmt den Steuer-VFO darauf ab."),
    ("h2", "Einstellungen"),
    ("p", "Reiter: Allgemein (Rufzeichen, API-Backend-URL, serieller Port/Baud, "
          "GPIO-Power, Auto-Abschaltung, Logo, GitHub-Update, Root-CA-Download), "
          "Audio (Gerät, USB-Mixer, Sprachfilter, Testton, TX-Timing), Rig-Info, "
          "Rig-Speicher, Rig-DTMF und Pi-Hardware (Host-Metriken)."),
    ("h1", "8  Mobile App (PWA)"),
    ("p", "Die Oberfläche installiert sich als Progressive Web App: Vollbild, mit "
          "App-Shell-Service-Worker für sofortigen Start. Auf dem Handy werden die "
          "Panels zu einem vertikalen Swipe-Deck — nach oben/unten wischen, ein "
          "Panel pro Bildschirm (so liegt die Scroll-Achse des Decks nicht auf den "
          "waagerechten Schiebereglern, die dadurch bedienbar bleiben) —, die "
          "Titelzeile zu einer schmalen vertikalen Leiste links und die Tab-Leiste "
          "rechts. Hinter dem letzten Panel folgt eine Info-Seite mit App-Version "
          "und Browser-/Umgebungsdaten. Die App wird ins Querformat gezwungen; im "
          "Hochformat erscheint "
          "ein Dreh-Hinweis. Installation über das Browser-Menü (Installieren / Zum "
          "Startbildschirm); unter iOS über Safari > Teilen."),
    ("p", "Solange die App geöffnet ist, bleibt der Handy-Bildschirm über die "
          "Screen-Wake-Lock-API wach und schaltet sich nicht mitten im QSO ab. Die "
          "Browser-Audioverbindung verbindet sich nach einer kurzen Netzstörung "
          "automatisch neu, und geht die Verbindung zum Backend bei eingerastetem "
          "Sendebetrieb verloren, wird das PTT beendet (lokal und durch einen "
          "Backend-Watchdog) — das Gerät kann so nie unbeaufsichtigt getastet "
          "bleiben."),
    ("img", "pwa-ptt-dark.png", 150),
    ("h1", "9  Vertrauenswürdiges Zertifikat (Root-CA)"),
    ("p", "Ein selbstsigniertes Zertifikat genügt am Desktop, aber mobile Browser "
          "installieren die PWA nicht und starten den Service-Worker nicht ohne "
          "vertrauenswürdiges Zertifikat. Eine eigene Root-CA erstellen, das "
          "Serverzertifikat damit signieren und die CA auf dem Handy als "
          "vertrauenswürdig installieren. Ein Download-Link erscheint unter "
          "Einstellungen > Allgemein, sobald die CA existiert."),
    ("code", CA),
    ("p", "ca/ca.crt auf dem Handy installieren (Einstellungen > Sicherheit > "
          "Zertifikat installieren > CA-Zertifikat). Das Leaf ist 825 Tage gültig "
          "und kann ohne Neu-Import aus derselben CA erneuert werden. ca/ca.key "
          "geheim halten."),
    ("h1", "10  Konfiguration"),
    ("p", "Einstellungen kommen aus Umgebungsvariablen (Präfix TMV71_) oder einer "
          ".env-Datei; Änderungen aus der Web-UI werden in "
          "backend/app/runtime.json gespeichert. Wichtige Variablen:"),
    ("code", ENVV),
    ("h1", "11  REST- & WebSocket-API"),
    ("h2", "Steuerung & Status"), ("code", API_CORE),
    ("h2", "Speicherkanäle"), ("code", API_MEM),
    ("h2", "Audio"), ("code", API_AUDIO),
    ("h2", "Digimodes & Selektivruf"), ("code", API_DIGI),
    ("h2", "SDR & Scan"), ("code", API_SDR),
    ("h2", "System & Power"), ("code", API_SYS),
    ("h1", "12  Fehlerbehebung"),
    ("ul", [
        "Kein CAT / 'radio offline': Port und Baud in den Einstellungen prüfen; "
        "das FTDI-Kabel muss auf /dev/ttyUSB0 liegen (oder Port korrekt setzen).",
        "Kein Audio: HTTPS sicherstellen, CONNECT klicken und Mic erlauben; "
        "USB-Karte und Mixer prüfen (Playback treibt das Funk-Mic beim Senden).",
        "PWA installiert nicht / Theme schaltet am Handy nicht: Zertifikat ist "
        "nicht vertrauenswürdig — Root-CA installieren (Kapitel 9).",
        "RX-Filter nur auf einem Kanal: Audio neu verbinden (Disconnect/Connect), "
        "damit Opus auf Mono neu ausgehandelt wird.",
        "Decoder brauchen ein sauberes Signal; Pegel und (bei RTTY) den Mark-Ton "
        "anpassen.",
    ]),
    ("h1", "13  Sicherheit"),
    ("p", "Nur fürs LAN konzipiert; es gibt keine Authentifizierung. Den Port "
          "nicht direkt ins Internet stellen — ein VPN (WireGuard/Tailscale) oder "
          "einen Reverse-Proxy mit TLS + Auth verwenden. Der private CA-Schlüssel "
          "verlässt den Pi nie."),
    ("h1", "14  Danksagung & Lizenz"),
    ("p", "Kenwood-PC-Protokoll-Doku: LA3QMA/TM-V71_TM-D710-Kenwood. Erstellt mit "
          "aiortc (WebRTC/Opus) und sounddevice. Schriften: Saira, IBM Plex Mono, "
          "DSEG (7-Segment), Neuropol (Titel). Lizenzdetails im Repository."),
]


def build(path, title, subtitle, lang, blocks):
    pdf = new_pdf(title)
    cover(pdf, title, subtitle, lang)
    render(pdf, blocks)
    pdf.output(path)
    print("wrote", path)


build(os.path.join(HERE, "Manual-EN.pdf"),
      "User & Technical Manual",
      "Kenwood TM-V71 web remote", "en", EN)
build(os.path.join(HERE, "Handbuch-DE.pdf"),
      "Benutzer- & Technikhandbuch",
      "Kenwood TM-V71 Web-Fernsteuerung", "de", DE)

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
VERSION = "3.2"


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
    "    - Wavelog logbook (QSO log)     - callsign lookup\n"
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
    "POST /api/audio/gain        rx/tx gain + TX AGC\n"
    "POST /api/audio/buffer      tx buffer / ptt tail\n"
    "POST /api/audio/tones       roger/test/mic/lowpass/de-emph/squelch\n"
    "POST /api/audio/record[/clear]  raw RX recorder\n"
    "GET  /api/audio/record.wav  download recording (WAV)\n"
    "GET/POST /api/audio/mixer   USB card mixer\n"
)
API_DIGI = (
    "GET  /api/digi              CW/RTTY/POCSAG status\n"
    "POST /api/digi/config       mode + parameters\n"
    "POST /api/digi/tx           encode + transmit\n"
    "POST /api/digi/decode-recording  decode the RX buffer\n"
    "WS   /ws/digi               decoded text stream\n"
    "GET/POST /api/asr/config    callsign recognition (Vosk)\n"
    "DELETE /api/asr/log/{call}  drop a misrecognised contact\n"
    "WS   /ws/callsign           recognised-callsign events\n"
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
API_LOG = (
    "GET/POST /api/log/config     logbook credentials (Wavelog + QRZ)\n"
    "POST /api/log/test           test the Wavelog connection\n"
    "POST /api/log/qrz/test       test the QRZ.com login\n"
    "GET  /api/log/stations       Wavelog station profiles\n"
    "POST /api/log/lookup         callsign lookup (QRZ + Wavelog)\n"
    "POST /api/log/qso            log a QSO\n"
    "GET  /api/log/recent         recent QSOs + online + Wavelog stats\n"
    "POST /api/log/recent/delete  delete one recent entry\n"
    "POST /api/log/recent/clear   clear the recent list\n"
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
    "TMV71_ASR_MODEL_DIR=...          TMV71_ASR_CALLLIST_PDF=...\n"
    "TMV71_SSL_CERTFILE=...  TMV71_SSL_KEYFILE=...\n"
)

# ---------------------------------------------------------------- English
EN = [
    ("h1", "1  Overview"),
    ("p", "TM-V71 Remote is a modern, dependency-light web remote control for the "
          "Kenwood TM-V71(A/E) dual-band FM transceiver, built around a direct "
          "serial driver. It gives full radio control in the browser, two-way "
          "browser audio over WebRTC/Opus, complete memory-channel management, an "
          "optional HackRF panadapter, classic 5-tone selective calling, a "
          "CW/RTTY/POCSAG digimodes decoder/encoder, a raw RX recorder, and "
          "optional off-air callsign recognition (Vosk). It installs as a "
          "Progressive Web App (PWA) and is designed to run on a Raspberry Pi."),
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
        "CW (Morse), RTTY (Baudot/AFSK) and POCSAG paging (512/1200/2400 baud, "
        "numeric + alphanumeric, BCH FEC) decode + encode over the FM audio path; "
        "CW auto mode tracks the received speed and tone pitch, plus a button to "
        "decode a captured RX buffer off-line.",
        "Audio processing: RX de-emphasis (for a flat 9600/discriminator feed), "
        "BUSY-gated software squelch, TX AGC, and voice low-pass filters.",
        "Raw RX recorder with WAV download (e.g. to build ASR training data).",
        "Off-air callsign recognition (optional, Vosk): detects spoken German "
        "callsigns, verifies them against the BNetzA list (name/town/class, or "
        "VOID if unassigned), shown in the title bar and a toast.",
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
        "Optional: vosk + the small German model (offline callsign recognition), "
        "and pypdf + the BNetzA Rufzeichenliste PDF (name/town/class + VOID check).",
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
    ("p", "Each band shows the frequency on a 7-segment display with two stacked "
          "meters under a shared S-scale: a real S-meter (S0–S9) in the active "
          "band's colour, and below it the AF level / mic-modulation bar "
          "(1 s peak-hold). Plus controls for VFO/memory mode, CTRL/PTT band "
          "selection, TX power, squelch (remembered per band across power "
          "cycles), repeater shift/offset, tone and "
          "bandwidth. The digit tuner lets you click bars above/below each digit "
          "to step the frequency; AIR Band tunes band A to the 118–137 MHz air "
          "band (receive-only)."),
    ("p", "The S-meter is derived from FM quieting: the high-band noise on the flat "
          "RX is inverse to signal strength (loud hiss = no signal, full quieting = "
          "strong signal), so it estimates the received signal even though the "
          "TM-V71 sends no numeric RSSI over CAT — only a binary BUSY status. It is "
          "a relative quieting estimate: a readable signal reads mid/upper scale, a "
          "strong local signal saturates at S9, and it snaps back to S0 the moment "
          "the carrier drops."),
    ("h2", "PTT & memory quick keys"),
    ("p", "Hold the large PTT button (or the space bar) to transmit; PTT-LOCK "
          "latches transmit. PTT and PTT-LOCK require connected audio (there is no "
          "mic otherwise) — they are disabled while audio is off and released "
          "automatically if audio disconnects. ROGER adds a two-tone "
          "(1000/1750 Hz) beep on "
          "release; while transmitting the button shows a count-up timer (MM:SS). "
          "The 1750 Hz button "
          "arms a tone-call. Memory quick keys recall channels 0–16 (M0–M9 in the "
          "left column, M10–M16 in the right; the loaded channel's key glows); "
          "below them the right column sends three DTMF memories (0–2). A status "
          "line shows per-band BUSY, the ASR state and the live RX/TX gain (in the "
          "PWA; the desktop shows the transmit hint). On mobile, mini RX/TX VU "
          "bars with peak-hold flank the button."),
    ("h2", "Audio (WebRTC/Opus)"),
    ("p", "Open the AUDIO panel, pick the RX band with the RX-A/RX-B switch, click "
          "CONNECT and allow the microphone. RX and mic levels are shown live, with "
          "the WebRTC RX/TX data rate in the graph corner. Controls: RX/TX gain "
          "(with a recommended-default tick), MIC (mic test — meters the mic "
          "without keying, records while on and replays your audio over RX when "
          "switched off; RX is muted during the test), AGC (automatic TX level), "
          "and a small recorder — ● REC / ▶ PLAY plus a WAV download of the raw, "
          "un-squelched RX feed (up to 60 min; e.g. to build ASR training data). "
          "TX timing (buffer / trail) and the USB card mixer are in Settings > "
          "Audio. The link auto-reconnects after a network glitch and is restored "
          "on the next launch."),
    ("p", "RX conditioning (Settings > Audio): a RX de-emphasis (adjustable time "
          "constant, on by default) restores natural voice tone when the audio "
          "comes from a flat discriminator / 9600-baud data output; a fixed ~180 Hz "
          "high-pass on the listen path removes the CTCSS/PL sub-audible tone "
          "(67–254 Hz) and DC hum that this flat output passes and de-emphasis "
          "would otherwise lift (audible as a low speaker hum), while leaving voice "
          "untouched; a BUSY-gated "
          "software squelch re-applies muting from the radio's own busy status for "
          "that always-open output; and TX/RX voice low-pass filters (≤ 3.5 kHz) "
          "tame hiss. The decoders always receive the un-squelched, un-filtered "
          "signal."),
    ("p", "Bluetooth headsets: transmit audio is captured from the phone's "
          "built-in microphone (not the headset's), so the headset stays on the "
          "A2DP profile and receive audio keeps coming through in good quality. "
          "Using the headset mic would force Android onto the mono HFP/SCO profile "
          "and, on many phones, leave RX stuck until Bluetooth is toggled."),
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
    ("h2", "Digimodes (CW / RTTY / POCSAG)"),
    ("p", "Switch between CW (Morse), RTTY (Baudot/AFSK) and POCSAG paging. DECODE "
          "shows received text; type into the field and SEND to transmit (keys "
          "PTT); the CW text input is forced upper case. Parameters: CW WPM/pitch "
          "— with an AUTO mode that tracks both the received speed and tone pitch "
          "(shown live on the sliders) and rejects voice/noise so it locks onto "
          "the CW even after a spoken ident; RTTY baud/shift/mark; and POCSAG baud "
          "(512/1200/2400), RIC, function and numeric/alphanumeric (auto-detected "
          "on RX) — e.g. monitor DAPNET on 439.9875 MHz with per-page "
          "RIC/FUNC/timestamp output. The REC button decodes the raw RX recorder "
          "buffer off-line in the current mode. Over the FM radio this is MCW / "
          "AFSK / FSK — not native HF modes."),
    ("h2", "Callsign recognition (Vosk)"),
    ("p", "An optional, offline speech-recognition pass on the RX audio that "
          "detects spoken German callsigns; enable it in Settings > Audio. A "
          "grammar-constrained Vosk model (ITU/NATO phonetic alphabet plus German "
          "digits — the German spelling alphabet and letter names were dropped as "
          "their short, homophone-prone words caused most false matches) stays "
          "usable on noisy "
          "FM voice; the recognised letters are assembled into a callsign, "
          "restricted to the real German BNetzA allocation blocks (always 5–6 "
          "characters) and verified against the BNetzA Rufzeichenliste. Accuracy is "
          "raised by N-best rescoring — Vosk returns several hypotheses per over and "
          "the best callsign across them is chosen, preferring an assigned one — and "
          "by repetition voting: a listed call is shown at once, while an unassigned "
          "(VOID) hit must be heard twice within a short window before it appears, "
          "suppressing one-off mishears (operators send their call 2–3× anyway). A "
          "hit appears in a framed "
          "field in the title bar (coloured to the RX band) and as a toast, "
          "enriched from the offline list with the holder's name, town and licence "
          "class (A/E/N); a call that is not assigned is still shown but flagged "
          "VOID. Your own callsign is ignored, it runs only while the squelch is "
          "open, and it can also grade the mic-test audio. The callsign list is "
          "built once from the PDF with a converter (python -m app.callsign_list); "
          "QRZ.com is used only for a manual lookup in the logbook, never by the "
          "ASR."),
    ("h2", "Band scan"),
    ("p", "Sweep a VHF/UHF range or the memory bank and see an occupancy "
          "spectrum + waterfall. Double-click a channel to tune the control VFO "
          "to it."),
    ("h2", "ASR contacts"),
    ("p", "A panel below the band scan that collects every recognised station as "
          "an index card, so the last overs are readable at a glance instead of "
          "as a scrolling log. Cards sit in a tray of empty slots, newest first. "
          "The last 200 entries are kept on the Pi and restored when the panel "
          "opens; CLEAR empties the view. In the mobile deck the panel has no "
          "tab — swipe to it past the band scan."),
    ("p", "Each card carries:"),
    ("ul", [
        "The callsign, in the largest and boldest type on the card — it is the "
        "identity of the contact. A zero is drawn slashed (DJØSH) so it cannot "
        "be misread as the letter O; that is display only, everything sent to "
        "the logbook keeps the plain 0.",
        "An avatar whose letters and colour are derived from the callsign "
        "itself (the characters after the region digit, plus a hue hashed from "
        "the whole call), so a station looks the same in every session without "
        "anything being stored.",
        "All three German licence classes A / E / N, with the holder's own one "
        "lit and the other two dimmed. If none is lit, the call is not in the "
        "BNetzA list.",
        "Name and town of the holder from the offline BNetzA list — never from "
        "QRZ.com, which the ASR does not query.",
        "Date and time of the last time the station was heard, pinned to the "
        "bottom edge of the card.",
    ]),
    ("p", "A station heard again does not get a second card. The existing one "
          "flashes, is marked, and counts up (×2, ×3 …) — but it stays where it "
          "is, so the tray does not reshuffle under you on every over. Exactly "
          "one card carries the red border at a time: the most recently heard. "
          "The ordering therefore follows first contact, not last mention."),
    ("p", "Hovering a card shows the recogniser detail that used to fill the log "
          "lines: word confidence (0.00–1.00, the mean over the individual "
          "spelled letters — a statement about the acoustics, not about whether "
          "the callsign is real), the S-value and receiving band at the moment "
          "of detection, the raw words Vosk actually heard, the rejected N-best "
          "candidates, and any 5/6-character correction that was applied."),
    ("p", "Two buttons per card: the play symbol logs the QSO straight to "
          "Wavelog with the name from the BNetzA list pre-filled, and turns teal "
          "once it has gone through so nothing is sent twice. The cross removes a "
          "misrecognised contact. That deletion happens on the Pi, not just in "
          "the browser: the card is dropped from the log buffer (otherwise it "
          "would return the next time the panel opens), every connected client "
          "loses it at once, and the callsign is released from the 90-second "
          "de-dupe window so a corrected reading can be reported immediately."),
    ("h2", "Logbook (Wavelog + QRZ.com)"),
    ("p", "Logs QSOs to a locally installed Wavelog instance. Enter just the "
          "callsign (and optionally a name) — frequency, band, mode, date/time and "
          "your own callsign are filled in automatically from the live control "
          "band and station profile. LOOKUP fetches the operator's name, grid, "
          "QTH, country and e-mail from QRZ.com (XML data API) and 'worked before' "
          "/ DXCC from Wavelog. LOG QSO sends the contact as ADIF. A green dot "
          "shows when Wavelog is reachable; the panel lists the most recent QSOs "
          "(with the looked-up details, deletable individually or via CLEAR) and "
          "Wavelog's QSO counts (today/month/year/total). Configure the Wavelog "
          "URL, API token and station profile, and the QRZ.com username/password, "
          "in Settings > Logging. Credentials are stored only on the Pi "
          "(runtime.json), never committed."),
    ("h2", "Settings"),
    ("p", "Tabs: General (callsign, API backend URL, serial port/baud, GPIO power, "
          "auto power-off, logo, GitHub self-update, Root-CA download), Audio "
          "(device, USB mixer, voice filters, test tone, TX timing), Rig-Info, "
          "Rig-Memory, Rig-DTMF, Logging (Wavelog + QRZ.com), and Pi-Hardware "
          "(host metrics)."),
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
    ("img", "pwa-ptt.png", 150),
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
    ("h2", "Logbook"), ("code", API_LOG),
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
          "einen optionalen HackRF-Panadapter, klassischen 5-Ton-Selektivruf, "
          "einen CW/RTTY/POCSAG-Decoder/Encoder, einen Roh-RX-Rekorder sowie "
          "optionale Offline-Rufzeichenerkennung (Vosk). Sie lässt sich als "
          "Progressive Web App (PWA) installieren und ist für den Raspberry Pi "
          "ausgelegt."),
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
        "CW (Morse), RTTY (Baudot/AFSK) und POCSAG-Paging (512/1200/2400 Baud, "
        "numerisch + alphanumerisch, BCH-FEC) dekodieren + senden über den "
        "FM-Audioweg; der CW-Auto-Modus führt Geschwindigkeit und Tonhöhe nach, "
        "plus eine Taste zum Offline-Dekodieren eines aufgenommenen RX-Puffers.",
        "Audioaufbereitung: RX-De-emphasis (für flachen 9600-/Diskriminator-"
        "Ausgang), BUSY-gesteuerte Software-Rauschsperre, TX-AGC und "
        "Sprach-Tiefpässe.",
        "Roh-RX-Rekorder mit WAV-Download (z. B. für ASR-Trainingsdaten).",
        "Offline-Rufzeichenerkennung (optional, Vosk): erkennt gesprochene deutsche "
        "Rufzeichen, prüft sie gegen die BNetzA-Liste (Name/Ort/Klasse bzw. VOID, "
        "wenn nicht zugeteilt), Anzeige in der Titelzeile und als Toast.",
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
        "Optional: vosk + das kleine deutsche Modell (Offline-Rufzeichen-"
        "erkennung) sowie pypdf + die BNetzA-Rufzeichenliste-PDF (Name/Ort/Klasse "
        "+ VOID-Prüfung).",
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
    ("p", "Jedes Band zeigt die Frequenz auf einer 7-Segment-Anzeige mit zwei "
          "übereinander liegenden Anzeigen unter einer gemeinsamen S-Skala: einem "
          "echten S-Meter (S0–S9) in der Farbe des aktiven Bandes und darunter dem "
          "NF-Pegel-/Mikrofon-Modulationsbalken (1 s Peak-Hold). Dazu Bedienelemente "
          "für VFO-/Speichermodus, CTRL-/"
          "PTT-Bandwahl, TX-Leistung, Squelch (pro Band über Aus-/Einschalten "
          "hinweg gespeichert), Relais-Shift/Offset, Ton und "
          "Bandbreite. Über die Ziffern-Abstimmung lässt sich jede Stelle per "
          "Klick auf die Balken hoch/runter stellen; AIR Band stellt Band A auf "
          "das Flugfunkband 118–137 MHz (nur Empfang)."),
    ("p", "Das S-Meter wird aus dem FM-Quieting abgeleitet: Das Rauschen im oberen "
          "Frequenzband des flachen RX-Signals ist umgekehrt proportional zur "
          "Signalstärke (lautes Rauschen = kein Signal, volle Rauschunterdrückung = "
          "starkes Signal). So lässt sich die Empfangsstärke schätzen, obwohl die "
          "TM-V71 über CAT keinen numerischen RSSI liefert — nur einen binären "
          "BUSY-Status. Es ist eine relative Quieting-Schätzung: ein verständliches "
          "Signal steht im mittleren/oberen Bereich, ein starkes Ortssignal sättigt "
          "bei S9, und bei Trägerverlust springt es sofort auf S0 zurück."),
    ("h2", "PTT & Speicher-Schnelltasten"),
    ("p", "Den großen PTT-Knopf (oder die Leertaste) halten zum Senden; PTT-LOCK "
          "rastet den Sendebetrieb ein. PTT und PTT-LOCK setzen verbundenes Audio "
          "voraus (sonst gibt es kein Mikrofon) — sie sind deaktiviert, solange "
          "Audio aus ist, und werden bei einer Audio-Trennung automatisch beendet. "
          "ROGER fügt beim Loslassen einen "
          "Zweiton-Piep (1000/1750 Hz) hinzu; während des Sendens zeigt der Knopf "
          "einen aufwärts laufenden Timer (MM:SS). Die 1750-Hz-Taste schärft einen "
          "Tonruf. Die Speicher-Schnelltasten rufen die Kanäle 0–16 ab (M0–M9 in "
          "der linken, M10–M16 in der rechten Spalte; die Taste des geladenen "
          "Kanals leuchtet); darunter sendet die rechte Spalte drei DTMF-Speicher "
          "(0–2). Eine Statuszeile zeigt BUSY je Band, den ASR-Zustand und den "
          "Live-RX/TX-Gain (in der PWA; am Desktop steht dort der Sende-Hinweis). "
          "Auf dem Handy flankieren Mini-RX/TX-VU-Bars mit Peak-Hold den Knopf."),
    ("h2", "Audio (WebRTC/Opus)"),
    ("p", "Das AUDIO-Panel öffnen, mit dem RX-A/RX-B-Schalter das Empfangsband "
          "wählen, CONNECT klicken und das Mikrofon erlauben. RX- und Mic-Pegel "
          "werden live angezeigt, dazu die WebRTC-RX/TX-Datenrate in der Graph-Ecke. "
          "Bedienelemente: RX/TX-Gain (mit Default-Markierung), MIC (Mic-Test — "
          "misst ohne zu tasten, nimmt im Betrieb auf und spielt beim Ausschalten "
          "über RX zurück; RX ist dabei stumm), AGC (automatischer TX-Pegel) sowie "
          "ein kleiner Rekorder — ● REC / ▶ PLAY plus WAV-Download des rohen, "
          "un-gesquelchten RX-Signals (bis 60 min; z. B. für ASR-Trainingsdaten). "
          "TX-Timing (Buffer/Trail) und der USB-Mixer liegen unter Einstellungen > "
          "Audio. Die Verbindung verbindet sich nach einer Netzstörung automatisch "
          "neu und wird beim nächsten Start wiederhergestellt."),
    ("p", "RX-Aufbereitung (Einstellungen > Audio): eine RX-De-emphasis "
          "(einstellbare Zeitkonstante, standardmäßig an) stellt den natürlichen "
          "Klang her, wenn das Audio vom flachen Diskriminator-/9600-Baud-Ausgang "
          "kommt; ein fester ~180-Hz-Hochpass im Hörpfad entfernt den "
          "CTCSS/PL-Subaudioton (67–254 Hz) und das Gleichspannungs-/Brummen, das "
          "dieser flache Ausgang durchlässt und die De-emphasis sonst anhebt "
          "(hörbar als tiefes Lautsprecherbrummen), ohne die Sprache anzutasten; "
          "eine BUSY-gesteuerte Software-Rauschsperre übernimmt für diesen "
          "daueroffenen Ausgang die Stummschaltung aus dem Busy-Status des Geräts; "
          "TX/RX-Sprachtiefpässe (≤ 3,5 kHz) zähmen Rauschen. Die Decoder erhalten "
          "stets das un-gesquelchte, ungefilterte Signal."),
    ("p", "Bluetooth-Headsets: Das Sende-Audio wird vom eingebauten Telefon-"
          "Mikrofon aufgenommen (nicht vom Headset-Mikro), damit das Headset im "
          "A2DP-Profil bleibt und der Empfang in guter Qualität durchkommt. Das "
          "Headset-Mikrofon würde Android auf das Mono-Profil HFP/SCO zwingen und "
          "RX auf vielen Handys hängen lassen, bis Bluetooth aus/an geschaltet "
          "wird."),
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
    ("h2", "Digimodes (CW / RTTY / POCSAG)"),
    ("p", "Umschalten zwischen CW (Morse), RTTY (Baudot/AFSK) und POCSAG-Paging. "
          "DECODE zeigt den empfangenen Text; in das Feld tippen und mit SEND "
          "senden (tastet PTT); die CW-Eingabe wird in Großbuchstaben erzwungen. "
          "Parameter: CW WpM/Tonhöhe — mit AUTO-Modus, der Geschwindigkeit und "
          "Tonhöhe nachführt (live auf den Slidern) und Sprache/Rauschen "
          "verwirft, sodass er auch nach einer Sprachansage auf das CW einrastet; "
          "RTTY Baud/Shift/Mark; sowie POCSAG-Baud (512/1200/2400), RIC, Funktion "
          "und numerisch/alphanumerisch (RX auto-erkannt) — z. B. DAPNET auf "
          "439,9875 MHz mit RIC/FUNC/Zeitstempel pro Meldung. Die REC-Taste "
          "dekodiert den Roh-RX-Puffer offline im aktuellen Modus. Über das "
          "FM-Gerät ist das MCW / AFSK / FSK — keine echten HF-Modes."),
    ("h2", "Rufzeichenerkennung (Vosk)"),
    ("p", "Eine optionale, Offline-Spracherkennung auf dem RX-Audio, die "
          "gesprochene deutsche Rufzeichen erkennt; einzuschalten unter "
          "Einstellungen > Audio. Ein grammatik-beschränktes Vosk-Modell "
          "(ITU/NATO-Buchstabieralphabet plus deutsche Ziffern — die deutsche "
          "Buchstabiertafel und die Buchstabennamen wurden entfernt, da ihre "
          "kurzen, homophon-anfälligen Wörter die meisten Falschtreffer "
          "verursachten) bleibt auf verrauschter FM-Sprache "
          "brauchbar; die erkannten Zeichen werden zu einem Rufzeichen gefügt, "
          "auf die realen deutschen BNetzA-Präfixblöcke beschränkt (immer 5–6 "
          "Zeichen) und gegen die BNetzA-Rufzeichenliste geprüft. Die Genauigkeit "
          "steigt durch N-Best-Rescoring — Vosk liefert mehrere Hypothesen pro "
          "Durchgang, und das beste Rufzeichen daraus wird gewählt, vorzugsweise "
          "ein zugeteiltes — sowie durch Wiederholungs-Voting: ein gelistetes "
          "Rufzeichen wird sofort angezeigt, ein nicht zugeteiltes (VOID) erst, "
          "wenn es zweimal in kurzer Zeit gehört wurde, was einmalige Fehlhörer "
          "unterdrückt (OMs geben ihr Call ohnehin 2–3×). Ein Treffer erscheint in "
          "einem umrahmten Feld in der Titelzeile (in Bandfarbe) und als Toast, "
          "angereichert aus der Offline-Liste mit Name, Ort und Klasse (A/E/N); "
          "ein nicht zugeteiltes Rufzeichen wird dennoch angezeigt, aber als VOID "
          "markiert. Das eigene Rufzeichen wird ignoriert, es läuft nur bei "
          "offener Rauschsperre und kann auch das Mic-Test-Audio auswerten. Die "
          "Rufzeichenliste wird einmalig per Converter aus der PDF erzeugt "
          "(python -m app.callsign_list); QRZ.com wird nur bei der manuellen "
          "Abfrage im Logbuch genutzt, nie von der ASR."),
    ("h2", "Bandscan"),
    ("p", "Einen VHF/UHF-Bereich oder die Speicherbank absuchen und ein "
          "Belegungs-Spektrum + Wasserfall sehen. Ein Doppelklick auf einen Kanal "
          "stimmt den Steuer-VFO darauf ab."),
    ("h2", "ASR-Kontakte"),
    ("p", "Ein Panel unter dem Bandscan, das jede erkannte Station als "
          "Karteikarte sammelt — die letzten Durchgänge sind so auf einen Blick "
          "lesbar statt als durchlaufendes Protokoll. Die Karten liegen in einem "
          "Kartenkasten aus leeren Fächern, die neueste vorn. Die letzten 200 "
          "Einträge hält der Pi und stellt sie beim Öffnen des Panels wieder "
          "her; CLEAR leert die Ansicht. Im mobilen Deck hat das Panel keinen "
          "Tab — dorthin hinter dem Bandscan wischen."),
    ("p", "Jede Karte trägt:"),
    ("ul", [
        "Das Rufzeichen, in der größten und fettesten Schrift der Karte — es "
        "ist die Identität des Kontakts. Die Null wird durchgestrichen "
        "dargestellt (DJØSH), damit sie nicht als Buchstabe O gelesen wird; das "
        "gilt nur für die Anzeige, ins Logbuch geht weiterhin die schlichte 0.",
        "Einen Avatar, dessen Buchstaben und Farbe aus dem Rufzeichen selbst "
        "abgeleitet sind (die Zeichen nach der Regionalziffer, dazu ein Farbton "
        "aus einem Hash des ganzen Rufzeichens). Eine Station sieht damit in "
        "jeder Sitzung gleich aus, ohne dass etwas gespeichert wird.",
        "Alle drei deutschen Lizenzklassen A / E / N, wobei nur die des "
        "Inhabers leuchtet und die beiden anderen gedimmt bleiben. Leuchtet "
        "keine, steht das Rufzeichen nicht in der BNetzA-Liste.",
        "Name und Ort des Inhabers aus der Offline-Liste der BNetzA — nie von "
        "QRZ.com, das die ASR nicht abfragt.",
        "Datum und Uhrzeit der letzten Nennung, fest an der Unterkante der "
        "Karte.",
    ]),
    ("p", "Eine erneut gehörte Station bekommt keine zweite Karte. Die "
          "vorhandene leuchtet auf, wird markiert und zählt hoch (×2, ×3 …) — "
          "sie bleibt aber an ihrem Platz, damit sich der Kartenkasten nicht bei "
          "jedem Durchgang unter dem Blick umsortiert. Genau eine Karte trägt "
          "die rote Umrandung: die zuletzt gehörte. Die Reihenfolge richtet sich "
          "damit nach dem Erstkontakt, nicht nach der letzten Nennung."),
    ("p", "Beim Überfahren einer Karte erscheinen die Erkennerdetails, die "
          "früher die Protokollzeilen füllten: die Wort-Konfidenz (0.00–1.00, "
          "Mittelwert über die einzeln buchstabierten Zeichen — eine Aussage "
          "über die Akustik, nicht darüber, ob es das Rufzeichen wirklich gibt), "
          "S-Wert und Empfangsband zum Zeitpunkt der Erkennung, der von Vosk "
          "tatsächlich gehörte Rohtext, die verworfenen N-Best-Kandidaten sowie "
          "eine gegebenenfalls angewandte 5/6-Zeichen-Korrektur."),
    ("p", "Zwei Knöpfe je Karte: Das Wiedergabesymbol trägt das QSO direkt ins "
          "Wavelog ein, mit dem Namen aus der BNetzA-Liste vorbelegt, und färbt "
          "sich nach erfolgreicher Übertragung türkis, damit nichts zweimal "
          "gesendet wird. Das Kreuz entfernt einen falsch erkannten Kontakt. "
          "Diese Löschung geschieht auf dem Pi, nicht nur im Browser: Die Karte "
          "fällt aus dem Protokollpuffer (sonst käme sie beim nächsten Öffnen "
          "des Panels zurück), alle verbundenen Clients verlieren sie "
          "gleichzeitig, und das Rufzeichen wird aus dem 90-Sekunden-"
          "Dedupe-Fenster entlassen, damit eine korrigierte Erkennung sofort "
          "wieder gemeldet werden darf."),
    ("h2", "Logbuch (Wavelog + QRZ.com)"),
    ("p", "Protokolliert QSOs in eine lokal installierte Wavelog-Instanz. Es "
          "genügt, das Rufzeichen (und optional einen Namen) einzugeben — Frequenz, "
          "Band, Modus, Datum/Uhrzeit und das eigene Rufzeichen werden automatisch "
          "aus dem aktuellen Steuerband und dem Stationsprofil ergänzt. LOOKUP holt "
          "Name, Locator, QTH, Land und E-Mail von QRZ.com (XML-Daten-API) sowie "
          "'schon gearbeitet' / DXCC von Wavelog. LOG QSO überträgt den Kontakt als "
          "ADIF. Ein grüner Punkt zeigt, ob Wavelog erreichbar ist; das Panel "
          "listet die letzten QSOs (mit den ermittelten Details, einzeln oder per "
          "CLEAR löschbar) sowie die QSO-Zähler von Wavelog (heute/Monat/Jahr/"
          "gesamt). Wavelog-URL, API-Token und Stationsprofil sowie QRZ.com-"
          "Benutzer/Passwort werden unter Einstellungen > Logging hinterlegt. "
          "Zugangsdaten liegen nur auf dem Pi (runtime.json) und werden nie "
          "committet."),
    ("h2", "Einstellungen"),
    ("p", "Reiter: Allgemein (Rufzeichen, API-Backend-URL, serieller Port/Baud, "
          "GPIO-Power, Auto-Abschaltung, Logo, GitHub-Update, Root-CA-Download), "
          "Audio (Gerät, USB-Mixer, Sprachfilter, Testton, TX-Timing), Rig-Info, "
          "Rig-Speicher, Rig-DTMF, Logging (Wavelog + QRZ.com) und Pi-Hardware "
          "(Host-Metriken)."),
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
    ("img", "pwa-ptt.png", 150),
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
    ("h2", "Logbuch"), ("code", API_LOG),
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

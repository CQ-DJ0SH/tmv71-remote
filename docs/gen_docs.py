#!/usr/bin/env python3
"""Generate the project documentation PDFs (English + German) into docs/.

Pure-Python via fpdf2 (no LaTeX). Run:  .venv/bin/python docs/gen_docs.py
"""
import os
import time

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


RULE = (206, 216, 222)        # hairlines for header/footer
HALF = 87                     # half the type area (174 mm), for L/R cells


class Doc(FPDF):
    """A4 with a running head: product left, current chapter right.

    The chapter caption is what makes a long manual navigable — on any page it
    says which of the fourteen chapters you are in. Cover and contents carry no
    furniture (running_head stays False there)."""
    title_txt = ""            # document title, shown in the footer
    chapter = ""              # current chapter, shown in the running head
    running_head = False      # off for the cover
    opening = False           # this page opens a chapter -> no caption

    def header(self):
        if self.page_no() <= 1 or not self.running_head:
            return
        self.set_y(11)
        self.set_font("DV", "", 7.6)
        self.set_text_color(*GREY)
        self.cell(HALF, 4, "TM-V71 REMOTE", align="L")
        # A page that opens a chapter carries no caption: the title stands right
        # below it in 22 pt, and repeating it is the classic redundancy.
        self.cell(HALF, 4, "" if self.opening else self.chapter,
                  align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.line(18, 16.4, 192, 16.4)
        self.set_y(22)

    def footer(self):
        if self.page_no() <= 1:      # the cover carries no furniture; the flag
            return                   # is already back on when this page closes
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.line(18, self.h - 15, 192, self.h - 15)
        self.set_y(-13)
        self.set_font("DV", "", 7.6)
        self.set_text_color(*GREY)
        self.cell(HALF, 5, f"{self.title_txt} · Version {VERSION}", align="L")
        self.cell(HALF, 5, f"{self.page_no()} / {{nb}}", align="R",
                  new_x="LMARGIN", new_y="TOP")


def new_pdf(title):
    pdf = Doc(orientation="P", unit="mm", format="A4")
    pdf.title_txt = title
    pdf.add_font("DV", "", FONT)
    pdf.add_font("DV", "B", FONTB)
    pdf.add_font("DV", "I", FONTI)
    pdf.add_font("MN", "", MONO)
    pdf.set_margins(18, 22, 18)          # top margin clears the running head
    pdf.set_auto_page_break(True, margin=20)
    pdf.alias_nb_pages()                 # "{nb}" in the footer = total pages
    pdf.set_title("TM-V71 Remote — " + title)
    pdf.set_author("CQ-DJ0SH")
    pdf.set_creator("gen_docs.py (fpdf2)")
    return pdf


def cover(pdf, title, subtitle, lang):
    """Title page: an accent band, the wordmark, what the thing is, and when
    this copy was generated. No running head — a cover carries no furniture."""
    pdf.running_head = False
    pdf.add_page()
    pdf.set_fill_color(*ACCENT)
    pdf.rect(0, 0, 210, 6, style="F")                 # band across the head
    pdf.ln(34)
    pdf.set_font("DV", "B", 32)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 17, "TM-V71 Remote", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.5)
    pdf.line(78, pdf.get_y() + 1, 132, pdf.get_y() + 1)
    pdf.ln(7)
    pdf.set_font("DV", "", 15)
    pdf.set_text_color(*DARK)
    pdf.cell(0, 9, title, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DV", "", 11)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 7, subtitle, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    img = os.path.join(HERE, "preview.png")
    if os.path.exists(img):
        try:
            pdf.image(img, x=33, w=144)
        except Exception:
            pass
    pdf.ln(7)
    pdf.set_font("DV", "I", 9.5)
    pdf.set_text_color(*GREY)
    note = ("Kenwood TM-V71(A/E) web remote + WebRTC audio + HackRF panadapter, "
            "for a Raspberry Pi.") if lang == "en" else \
           ("Web-Fernsteuerung für Kenwood TM-V71(A/E) mit WebRTC-Audio und "
            "HackRF-Panadapter, für den Raspberry Pi.")
    pdf.multi_cell(0, 5, note, align="C")
    # foot of the cover: version, date, source
    pdf.set_y(-32)
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.2)
    pdf.line(60, pdf.get_y(), 150, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("DV", "", 9)
    pdf.set_text_color(*DARK)
    stamp = ("Version %s · %s" % (VERSION, time.strftime("%d.%m.%Y"))) if lang == "de" \
            else ("Version %s · %s" % (VERSION, time.strftime("%Y-%m-%d")))
    pdf.cell(0, 5, stamp, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DV", "", 8)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 4, "github.com/CQ-DJ0SH/tmv71-remote", align="C",
             new_x="LMARGIN", new_y="NEXT")


def toc(pdf, outline, lang):
    """Contents, rendered into the placeholder reserved after the cover.

    Chapters in dark, sections indented and grey, page numbers flush right with
    a dotted leader — the reader can find a chapter without paging through."""
    pdf.set_font("DV", "B", 17)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 11, "Inhalt" if lang == "de" else "Contents",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.4)
    y = pdf.get_y() + 1
    pdf.line(18, y, 192, y)
    pdf.ln(6)
    for e in outline:
        chapter = e.level == 0
        pdf.set_font("DV", "B" if chapter else "", 10.5 if chapter else 9.5)
        pdf.set_text_color(*(DARK if chapter else GREY))
        if chapter:
            pdf.ln(1.6)
        x0 = 18 if chapter else 24
        pdf.set_x(x0)
        label = e.name
        num = str(e.page_number)
        wl = pdf.get_string_width(label)
        wn = pdf.get_string_width(num)
        pdf.cell(wl + 1, 5.4, label)
        # dotted leader, drawn rather than typed: dots that always land on the
        # same baseline and never wrap
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.2)
        pdf.set_dash_pattern(dash=0.4, gap=1.1)
        ly = pdf.get_y() + 3.6
        pdf.line(x0 + wl + 2, ly, 192 - wn - 2, ly)
        pdf.set_dash_pattern()
        pdf.set_x(192 - wn - 1)
        pdf.cell(wn + 1, 5.4, num, align="R", new_x="LMARGIN", new_y="NEXT")


# ---------------------------------------------------------------------------
# Block diagrams. Drawn with fpdf primitives rather than embedded as an image:
# they stay sharp at any zoom, cost no file, and the labels are searchable text.
# ---------------------------------------------------------------------------
BOX_BG = (240, 244, 247)
BOX_ED = (152, 168, 178)
ACC_BG = (226, 240, 234)
LINE = (96, 112, 122)


def _box(pdf, x, y, w, h, title, sub=(), accent=False):
    pdf.set_line_width(0.3)
    pdf.set_draw_color(*(ACCENT if accent else BOX_ED))
    pdf.set_fill_color(*(ACC_BG if accent else BOX_BG))
    pdf.rect(x, y, w, h, style="DF", round_corners=True, corner_radius=1.2)
    pdf.set_xy(x + 1, y + 1.3)
    pdf.set_font("DV", "B", 7)
    pdf.set_text_color(*(ACCENT if accent else DARK))
    pdf.multi_cell(w - 2, 3.1, title, align="C")
    if sub:
        pdf.set_x(x + 1)
        pdf.set_font("DV", "", 5.8)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(w - 2, 2.6, "\n".join(sub), align="C")


def _frame(pdf, x, y, w, h, label):
    """Dashed enclosure with a caption — one physical machine."""
    pdf.set_dash_pattern(dash=1.2, gap=1.2)
    pdf.set_draw_color(*BOX_ED)
    pdf.set_line_width(0.25)
    pdf.rect(x, y, w, h, style="D", round_corners=True, corner_radius=2)
    pdf.set_dash_pattern()
    pdf.set_xy(x + 2, y + 1)
    pdf.set_font("DV", "B", 6.4)
    pdf.set_text_color(*GREY)
    pdf.cell(w - 4, 3.4, label)


def _arrow(pdf, x1, y1, x2, y2, label="", both=False, above=True):
    import math
    pdf.set_draw_color(*LINE)
    pdf.set_fill_color(*LINE)
    pdf.set_line_width(0.3)
    pdf.line(x1, y1, x2, y2)
    a = math.atan2(y2 - y1, x2 - x1)
    for ax, ay, ang in (((x2, y2, a),) if not both
                        else ((x2, y2, a), (x1, y1, a + math.pi))):
        pdf.polygon([(ax, ay),
                     (ax - 1.9 * math.cos(ang) + 1.0 * math.sin(ang),
                      ay - 1.9 * math.sin(ang) - 1.0 * math.cos(ang)),
                     (ax - 1.9 * math.cos(ang) - 1.0 * math.sin(ang),
                      ay - 1.9 * math.sin(ang) + 1.0 * math.cos(ang))], style="F")
    if label:
        pdf.set_font("DV", "", 5.6)
        pdf.set_text_color(*GREY)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        pdf.set_xy(mx - 14, my - (3.2 if above else -0.6))
        pdf.cell(28, 2.6, label, align="C")


def _chain(pdf, y, x0, items, w, gap, h, accent=(), labels=()):
    """A left-to-right row of boxes joined by arrows. Returns their x spans."""
    xs = []
    x = x0
    for i, (title, sub) in enumerate(items):
        _box(pdf, x, y, w, h, title, sub, accent=(i in accent))
        xs.append((x, x + w))
        x += w + gap
    for i in range(len(items) - 1):
        lbl = labels[i] if i < len(labels) else ""
        _arrow(pdf, xs[i][1] + 0.5, y + h / 2, xs[i + 1][0] - 0.5, y + h / 2, lbl)
    return xs


def _diagram(pdf, height, fn):
    """Reserve the space, keep it on one page, then draw."""
    if pdf.get_y() + height > pdf.h - 18:
        pdf.add_page()
    pdf.set_auto_page_break(False)
    y0 = pdf.get_y() + 2
    fn(pdf, y0)
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_y(y0 + height)
    pdf.set_text_color(*DARK)


def _cap(pdf, x, y, w, text):
    pdf.set_xy(x, y)
    pdf.set_font("DV", "I", 6.6)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(w, 3, text, align="C")


def draw_system(pdf, lang):
    de = lang == "de"
    def go(pdf, y0):
        gx, gw = 58, 100
        _frame(pdf, gx, y0, gw, 92, "Raspberry Pi 5")
        bx, bw, bh, gap = gx + 4, gw - 8, 12, 2.6
        rows = [
            ("tmv71" + ("-Treiber" if de else " driver"),
             ["Serial 57600 Bd · Kenwood PC-" + ("Protokoll" if de else "protocol")]),
            ("Audio-Engine" if de else "Audio engine",
             ["sounddevice 48 kHz · aiortc/Opus",
              ("Squelch · S-Meter · Roger-Beep" if de
               else "squelch · S-meter · roger beep")]),
            ("Decoder & SDR",
             ["CW/RTTY/POCSAG · 5-" + ("Ton" if de else "tone") + " · HackRF"]),
            (("Rufzeichen-ASR" if de else "Callsign ASR"),
             ["Vosk " + ("Grammatik" if de else "grammar") + " + "
              + ("Sprechermodell" if de else "speaker model")]),
            (("Logbuch" if de else "Logbook"), ["Wavelog · QRZ.com"]),
            ("FastAPI + uvicorn",
             ["REST · WebSocket · WebRTC · PWA", "HTTPS 8443"]),
        ]
        ys = []
        y = y0 + 6
        for i, (t, sub) in enumerate(rows):
            _box(pdf, bx, y, bw, bh, t, sub, accent=(i in (3, 5)))
            ys.append(y)
            y += bh + gap
        # radio, left, spanning the serial and audio rows
        ry, rh = ys[0], ys[1] + bh - ys[0]
        _box(pdf, 18, ry, 34, rh, "Kenwood TM-V71",
             ["CAT (USB-Seriell)" if de else "CAT (USB serial)",
              ("Datenbuchse 9600" if de else "data port 9600"),
              ("Mic + PTT · GPIO-Netz" if de else "mic + PTT · GPIO power")])
        _arrow(pdf, 52.5, ys[0] + bh / 2, bx - 0.5, ys[0] + bh / 2, both=True)
        _arrow(pdf, 52.5, ys[1] + bh / 2, bx - 0.5, ys[1] + bh / 2, both=True)
        _box(pdf, 18, ys[2], 34, bh, "HackRF One",
             ["USB · " + ("Spektrum" if de else "spectrum")])
        _arrow(pdf, 52.5, ys[2] + bh / 2, bx - 0.5, ys[2] + bh / 2)
        # clients, right
        _box(pdf, 164, ys[5], 28, bh, "Browser / PWA",
             [("Bedienung · Audio" if de else "control · audio"), "WebRTC"])
        # no caption on this arrow either: 28 mm of centred text over a 5 mm
        # link lands on the frame and on both boxes
        _arrow(pdf, gx + gw + 0.5, ys[5] + bh / 2, 163.5, ys[5] + bh / 2, both=True)
        _box(pdf, 164, ys[4], 28, bh, "Wavelog", ["QRZ.com"])
        _arrow(pdf, gx + gw + 0.5, ys[4] + bh / 2, 163.5, ys[4] + bh / 2)
        _cap(pdf, 18, y0 + 94, 174,
             ("Alles läuft auf dem Pi; der Browser bekommt Steuerung, Status und "
              "Audio über eine einzige HTTPS-Adresse."
              if de else
              "Everything runs on the Pi; the browser gets control, status and "
              "audio over one HTTPS address."))
    _diagram(pdf, 100, go)


def draw_audio(pdf, lang):
    de = lang == "de"
    def go(pdf, y0):
        W, G, H = 30.8, 5, 14
        x0 = 18
        # --- RX playback -----------------------------------------------------
        pdf.set_xy(18, y0)
        pdf.set_font("DV", "B", 7.4)
        pdf.set_text_color(*DARK)
        pdf.cell(0, 4, "RX " + ("(Empfang)" if de else "(receive)"))
        y = y0 + 5
        rx = _chain(pdf, y, x0, [
            (("Datenbuchse" if de else "Data port"),
             ["TM-V71 · 9600",
              ("Diskriminator" if de else "discriminator")]),
            ("USB-" + ("Soundkarte" if de else "sound card"),
             ["48 kHz · 16 bit", "mono"]),
            ("Audio-Callback",
             ["webrtc_audio.py",
              ("Tiefpass · Abgriffe" if de else "low-pass · taps")]),
            (("Aufbereitung" if de else "Conditioning"),
             ["de-emph 75 µs · BUSY-" + ("Gate" if de else "gate"),
              ("Pegel → S-Meter" if de else "level → S-meter")]),
            ("aiortc · Opus",
             ["WebRTC → Browser",
              "MUTE / DROP"]),
        ], W, G, H)
        # --- taps ------------------------------------------------------------
        ty = y + H + 12
        cb = rx[2]                                    # the callback box feeds them
        cx = (cb[0] + cb[1]) / 2
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.3)
        pdf.line(cx, y + H, cx, ty - 6)               # drop from the callback
        pdf.line(x0 + W / 2, ty - 6, x0 + 4 * (W + G) + W / 2, ty - 6)   # bus
        pdf.set_font("DV", "", 5.6)
        pdf.set_text_color(*GREY)
        pdf.set_xy(cx + 2, ty - 10.2)
        pdf.cell(60, 2.6, ("Abgriffe — immer das rohe, un-gesquelchte Signal"
                           if de else "taps — always the raw, un-squelched signal"))
        taps = [
            (("Digimodes" if de else "Digimodes"), ["CW · RTTY · POCSAG"]),
            ("5-" + ("Ton-Selcall" if de else "tone selcall"), ["ZVEI/CCIR"]),
            (("Roh-Rekorder" if de else "Raw recorder"), ["WAV · 60 min"]),
            (("Rufzeichen-ASR" if de else "Callsign ASR"), ["Vosk"]),
            (("Stimmerkennung" if de else "Voice ID"), ["Vosk " + ("Sprecher" if de else "speaker")]),
        ]
        tw = 30.8
        for i, (t, sub) in enumerate(taps):
            tx = x0 + i * (tw + G)
            _box(pdf, tx, ty, tw, 10, t, sub, accent=(i >= 3))
            _arrow(pdf, tx + tw / 2, ty - 6, tx + tw / 2, ty - 0.5)
        # --- the Vosk chains --------------------------------------------------
        ay = ty + 16
        pdf.set_xy(18, ay - 5)
        pdf.set_font("DV", "B", 7.4)
        pdf.set_text_color(*ACCENT)
        pdf.cell(0, 4, ("Rufzeichenerkennung (Vosk, grammatikgebunden)"
                        if de else "Callsign recognition (Vosk, grammar-constrained)"))
        _chain(pdf, ay, x0, [
            (("0,25-s-Blöcke" if de else "0.25 s blocks"),
             ["48 kHz · " + ("Squelch offen" if de else "squelch open")]),
            ("48 → 16 kHz",
             ["FIR + " + ("Dezimation" if de else "decimation"),
              "de-emph · 250 Hz HP"]),
            ("Vosk KaldiRecognizer",
             [("Grammatik: Alphabet" if de else "grammar: alphabet"),
              ("+ Ziffern · N-best" if de else "+ digits · N-best")]),
            (("Prüfung & Votum" if de else "Verify & vote"),
             ["BNetzA-" + ("Liste" if de else "list"), "de-dupe 90 s"]),
            (("Panel / WebSocket" if de else "Panel / WebSocket"),
             ["/ws/callsign", ("Kontaktkarte" if de else "contact card")]),
        ], W, G, H, accent=(2,))
        # --- speaker branch ---------------------------------------------------
        sy = ay + H + 11
        pdf.set_xy(18, sy - 5)
        pdf.set_font("DV", "B", 7.4)
        pdf.set_text_color(*ACCENT)
        pdf.cell(0, 4, ("Stimmerkennung (Vosk-Sprechermodell)"
                        if de else "Voice ID (Vosk speaker model)"))
        _chain(pdf, sy, x0, [
            (("Über-Segmentierer" if de else "Over segmenter"),
             ["50 ms · −55 dBFS",
              ("Pause 0,6 s = Ende" if de else "0.6 s pause = end")]),
            ("Vosk SpkModel",
             ["x-vector · 128",
              ("8-kHz-Modell" if de else "8 kHz model")]),
            (("Profilbuch" if de else "Profile book"),
             ["speakers.json",
              ("Mittel je Rufzeichen" if de else "mean per callsign")]),
            (("Einlernen / Zuordnen" if de else "Enrol / match"),
             ["≥ 0,55 · " + ("Abstand" if de else "margin") + " ≥ 0,10"]),
            (("Panel / WebSocket" if de else "Panel / WebSocket"),
             ["/ws/callsign",
              ("Marke + Redezeit" if de else "mark + talk timer")]),
        ], W, G, H, accent=(1,))
        # --- TX ---------------------------------------------------------------
        xy = sy + H + 11
        pdf.set_xy(18, xy - 5)
        pdf.set_font("DV", "B", 7.4)
        pdf.set_text_color(*DARK)
        pdf.cell(0, 4, "TX " + ("(Senden)" if de else "(transmit)"))
        _chain(pdf, xy, x0, [
            (("Browser-Mikrofon" if de else "Browser microphone"),
             ["getUserMedia · Opus"]),
            ("aiortc",
             [("Dekodierung" if de else "decode") + " · 48 kHz"]),
            ("TX-" + ("Puffer" if de else "buffer"),
             [("Vorlauf + PTT-Nachlauf" if de else "lead-in + PTT tail"),
              ("Roger-Beep · AGC" if de else "roger beep · AGC")]),
            (("Mic-Leitung" if de else "Mic line"),
             ["USB-" + ("Soundkarte" if de else "sound card")]),
            ("Kenwood TM-V71", [("PTT über CAT" if de else "PTT over CAT")]),
        ], W, G, H)
        _cap(pdf, 18, xy + H + 3, 174,
             ("Die Decoder hängen immer am rohen Signal — Squelch, MUTE und DROP "
              "wirken nur auf das, was der Browser hört."
              if de else
              "The decoders always sit on the raw signal — squelch, MUTE and DROP "
              "only affect what the browser hears."))
    _diagram(pdf, 122, go)


def draw_wiring(pdf, lang):
    """Where the cables go: Pi -> USB sound card -> radio. The labels stay
    English in both languages — connector and pin names are what is printed on
    the radio and in its manual, and translating them would only invite a
    mis-wire."""
    de = lang == "de"

    def go(pdf, y0):
        def head(x, y, text):
            pdf.set_xy(x, y)
            pdf.set_font("DV", "B", 6.6)
            pdf.set_text_color(*ACCENT)
            pdf.cell(40, 3, text)
        # --- Pi, left, between the three signal rows -------------------------
        _box(pdf, 16, y0 + 24, 32, 26, "Raspberry Pi 5",
             ["tmv71-remote", "HTTPS 8443"], accent=True)
        # --- USB sound card + serial bridge ----------------------------------
        _box(pdf, 58, y0 + 10, 38, 44, "USB sound card",
             ["Sound Blaster Play! 3", "class compliant, 48 kHz", "",
              "OUT  3.5 mm TRS", "line level, ~ 1 V", "", "IN   3.5 mm"])
        _box(pdf, 58, y0 + 64, 38, 16, "USB-serial bridge",
             ["FTDI FT-X", "/dev/ttyUSB0"])
        # --- level interface, transmit path only -----------------------------
        head(104, y0 + 7, "TX  transmit")
        _box(pdf, 104, y0 + 12, 34, 22, "Isolation transformer + pad",
             ["1:1, 600 Ohm", "~ 40 dB down"])
        head(104, y0 + 44, "RX  receive")
        head(104, y0 + 64, "CAT  control")
        # --- the radio and its three sockets ---------------------------------
        _frame(pdf, 152, y0 + 4, 42, 78, "Kenwood TM-V71")
        _box(pdf, 154, y0 + 12, 38, 22, "MIC jack",
             ["8-pin modular, side of head", "pin 6 MIC, ~ 2 mV / 600 Ohm",
              "pin 5 MIC GND"])
        _box(pdf, 154, y0 + 40, 38, 22, "DATA jack",
             ["6-pin mini-DIN, rear", "pin 5 PR1, 1200 Bd out",
              "pin 2 DE (ground)"])
        _box(pdf, 154, y0 + 66, 38, 14, "PC port",
             ["8-pin mini-DIN, rear"])
        # --- the paths --------------------------------------------------------
        _arrow(pdf, 48.5, y0 + 33, 57.5, y0 + 33, "USB")
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.3)
        pdf.line(32, y0 + 50, 32, y0 + 72)          # down the left, then across
        _arrow(pdf, 32, y0 + 72, 57.5, y0 + 72, "USB")
        _arrow(pdf, 96.5, y0 + 23, 103.5, y0 + 23)
        _arrow(pdf, 138.5, y0 + 23, 153.5, y0 + 23)
        _arrow(pdf, 153.5, y0 + 51, 96.5, y0 + 51, "PR1 ~ 0.3 Vpp, fixed")
        _arrow(pdf, 96.5, y0 + 72, 153.5, y0 + 72, "CAT 57600 Bd + PTT")
        _cap(pdf, 16, y0 + 86, 178,
             ("TX geht in die Mikrofonbuchse, nicht in die Datenbuchse: Das Gerät "
              "legt das Sendeaudio nur dann auf die Datenbuchse, wenn eine "
              "HARDWARE-PTT tastet — hier wird die PTT über CAT getastet. RX "
              "kommt von PR1, dem 1200-Baud-Pin: gefiltert, mit festem Pegel und "
              "unabhängig vom Lautstärkeregler. Menü 518 (Data-Speed) auf 1200 "
              "und Menü 519 (PC-Port-Baud) auf 57600 stellen."
              if de else
              "TX goes into the mic jack, not into the DATA jack: the radio only "
              "routes transmit audio from the DATA jack while a HARDWARE PTT keys "
              "it, and this station keys PTT over CAT. RX is taken from PR1, the "
              "1200-baud pin — filtered, at a fixed level and untouched by the "
              "volume knob. Set menu 518 (data speed) to 1200 and menu 519 (PC "
              "port baud) to 57600."))
    _diagram(pdf, 100, go)


DIAGRAMS = {"system": draw_system, "audio": draw_audio,
            "wiring": draw_wiring}


def render(pdf, blocks):
    for kind, *rest in blocks:
        if kind == "h1":
            # both are set BEFORE the page break: header() runs while the new
            # page is created, so it would otherwise print the previous chapter
            pdf.chapter = rest[0]
            pdf.opening = True
            # a chapter starts on its own page — unless the current one is still
            # untouched, which is the case for the page the ToC placeholder left
            if pdf.get_y() > pdf.t_margin + 0.5:
                pdf.add_page()
            pdf.opening = False
            pdf.start_section(rest[0], 0)     # PDF outline + table of contents
            # the chapter number is set in the accent colour and the title in
            # dark, so the eye finds the number when leafing through
            num, _, name = rest[0].partition("  ")
            pdf.set_font("DV", "B", 22)
            pdf.set_text_color(*ACCENT)
            w = pdf.get_string_width(num + " ")
            pdf.cell(w, 11, num + " ")
            pdf.set_text_color(*DARK)
            # left-aligned, and continuation lines hang under the title rather
            # than under the number
            pdf.set_left_margin(18 + w)
            pdf.multi_cell(0, 11, name, align="L")
            pdf.set_left_margin(18)
            pdf.set_draw_color(*ACCENT)
            pdf.set_line_width(0.5)
            y = pdf.get_y() + 1.5
            pdf.line(18, y, 192, y)
            pdf.ln(6)
        elif kind == "h2":
            pdf.ln(3)
            # Keep the heading with what follows. Without this a section title
            # can land at the very bottom of a page — and worse, its accent tick
            # is drawn before the text flows, so the tick stayed behind on the
            # previous page while the heading moved to the next one.
            if pdf.get_y() > pdf.h - pdf.b_margin - 24:
                pdf.add_page()
            pdf.start_section(rest[0], 1)
            # a short accent tick marks the section without shouting
            y = pdf.get_y()
            pdf.set_fill_color(*ACCENT)
            pdf.rect(18, y + 1.4, 1.6, 4.6, style="F")
            pdf.set_x(22)
            pdf.set_font("DV", "B", 12.5)
            pdf.set_text_color(*DARK)
            pdf.multi_cell(0, 7, rest[0], align="L")
            pdf.ln(1.5)
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
        elif kind == "diagram":
            DIAGRAMS[rest[0]](pdf, rest[1])
        elif kind == "space":
            pdf.ln(rest[0] if rest else 3)



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
    "POST /api/audio/buffer      tx buffer / ptt tail / rx buffer\n"
    "POST /api/audio/tones       roger/test/mic/lowpass/de-emph/squelch\n"
    "POST /api/audio/record[/clear]  raw RX recorder\n"
    "GET  /api/audio/record.wav  download recording (WAV)\n"
    "GET/POST /api/audio/mixer   USB card mixer\n"
)
API_DIGI = (
    "GET  /api/digi              CW/RTTY/POCSAG/APRS status\n"
    "POST /api/digi/config       mode + parameters\n"
    "POST /api/digi/tx           encode + transmit\n"
    "POST /api/digi/decode-recording  decode the RX buffer\n"
    "WS   /ws/digi               decoded text stream\n"
    "GET/POST /api/asr/config    callsign recognition (Vosk)\n"
    "POST /api/asr/log           add a callsign by hand\n"
    "PATCH /api/asr/log/{call}   correct a callsign (profile follows)\n"
    "GET/POST /api/asr/speaker   voice ID: on/off, stage, limits\n"
    "DELETE /api/asr/log         empty the contact log (CLEAR ALL)\n"
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
    ("p", "Everything below runs on the Pi. The radio is reached over two "
          "independent paths — CAT commands on the serial line, and audio on the "
          "data port through a USB sound card — and the browser sees only the one "
          "HTTPS address the backend serves."),
    ("diagram", "system", "en"),
    ("h2", "Audio path"),
    ("p", "One capture callback owns the received audio. What the browser hears "
          "is conditioned and squelched; every decoder is fed from a tap taken "
          "BEFORE that, on the raw signal, which is why the S-meter, the "
          "recorder and both Vosk chains keep working while MUTE or DROP is on. "
          "Vosk appears twice on purpose: the grammar-constrained decoder reads "
          "spoken callsigns, and a second, plain recognizer with the speaker "
          "model turns a whole over into a voiceprint (the grammar decoder runs "
          "with N-best alternatives, and in that mode Vosk returns no x-vector)."),
    ("diagram", "audio", "en"),
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
    ("h2", "Wiring the USB sound card"),
    ("p", "The audio does not use one connector but two, and they sit on "
          "opposite sides of the radio: transmit audio goes into the mic jack on "
          "the side of the control head, received audio comes off the DATA jack "
          "on the rear panel. That split is not a preference, it follows from how "
          "the radio switches its paths — see the note under the diagram."),
    ("diagram", "wiring", "en"),
    ("ul", [
        "The transmit path needs to lose about 40 dB: the card puts out line "
        "level (~1 V), the mic input expects a few millivolts into 600 ohms. A "
        "1:1 transformer in that lead also breaks the ground loop between the "
        "Pi's supply and the radio, which is what an alternator whine or a "
        "steady hum on your transmitted audio usually is.",
        "PR1 (pin 5) carries the 1200-baud receive audio: filtered, at a fixed "
        "level, and independent of the volume knob — so what the Pi hears does "
        "not change when you turn the radio down. PR9 (pin 4) is the flat "
        "9600-baud output straight off the discriminator and is the wrong pin "
        "here unless you re-enable de-emphasis in software.",
        "PTT is keyed over CAT on the PC port, so no PTT line is wired. Nothing "
        "in the audio cabling can key the radio.",
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
          "automatically if audio disconnects. ROGER adds a descending three-tone "
          "beep (d6 · a5 · e5, i.e. 1175/880/659 Hz, 60 ms each with 30 ms gaps) "
          "on release, at the level set in the audio settings; "
          "while transmitting the button shows a count-up timer (MM:SS). "
          "The 1750 Hz button "
          "arms a tone-call. Memory quick keys recall channels 0–16 (M0–M9 in the "
          "left column, M10–M16 in the right; the loaded channel's key glows); "
          "below them the right column sends three DTMF memories (0–2). A status "
          "line shows per-band BUSY, the ASR state and the live RX/TX gain (in the "
          "PWA; the desktop shows the transmit hint). On mobile, mini RX/TX VU "
          "bars with peak-hold flank the button."),
    ("h2", "MUTE and DROP"),
    ("p", "Two buttons flank the \u201cPTT \u2192 BAND x\u201d caption and silence the "
          "received audio, differing only in when they stop:"),
    ("ul", [
        "MUTE (left) is the plain one: it stays until it is pressed again. The "
        "label reads MUTED while it is on.",
        "DROP (right) blanks the transmission in progress and lifts itself as "
        "soon as that station stops keying — for an interferer or a rag-chew you "
        "do not want to hear, without having to remember to un-mute afterwards.",
    ]),
    ("p", "Both glow in the colour of the transmit band while armed. Three "
          "details are worth knowing:"),
    ("ul", [
        "DROP releases on the falling edge of that band's BUSY signal, not merely "
        "because the channel is quiet: arming it on an idle channel keeps the "
        "audio muted until the end of the next transmission, rather than "
        "un-muting again on the next status update.",
        "DROP also lifts after three seconds without speech, even while BUSY "
        "is still up — the permanent carrier of a repeater would otherwise "
        "keep the audio muted indefinitely. The threshold is on the AF level, "
        "so a quiet carrier counts as silence while an over does not.",
        "Muting is done in the browser, on the audio element. The S-meter, the "
        "raw RX recorder and the callsign recognition keep receiving the signal — "
        "only what you hear is silenced.",
        "A three-tone chime confirms each change (e5 · a5 · d6 rising on release, "
        "the same triad reversed when muting), so the two are told apart without "
        "looking. It is generated in the browser and can therefore never be mixed "
        "into the transmitted audio, unlike the roger beep, which deliberately is.",
    ]),
    ("h2", "Audio (WebRTC/Opus)"),
    ("p", "Open the AUDIO panel, pick the RX band with the RX-A/RX-B switch, click "
          "CONNECT and allow the microphone. RX and mic levels are shown live, with "
          "the WebRTC RX/TX data rate in the graph corner. Controls: RX/TX gain "
          "(with a recommended-default tick), MIC (mic test — meters the mic "
          "without keying, records while on and replays your audio over RX when "
          "switched off; RX is muted during the test), AGC (automatic TX level), "
          "and a small recorder — ● REC / ▶ PLAY plus a WAV download of the raw, "
          "un-squelched RX feed (up to 60 min; e.g. to build ASR training data; the "
          "downloaded file is named with the date and time it was saved). "
          "Audio timing (TX buffer / TX trail / RX buffer) and the USB card mixer "
          "are in Settings > Audio. The link auto-reconnects after a network "
          "glitch and is restored on the next launch."),
    ("p", "The TX buffer bounds how much microphone audio may queue up for the "
          "radio. It has to be larger than the clusters the browser delivers in: "
          "measured here with a 40 ms cap, 2.3 s of silence were inserted for "
          "want of samples while 2.5 s were discarded at the cap — starving and "
          "overflowing at the same time, heard as dropouts. 150 ms is the "
          "default and the slider stops at 80 ms, because below that no setting "
          "can work. Half of it (40–80 ms) is collected as a cushion before "
          "play-out starts."),
    ("p", "TX trail is how long the radio stays keyed after the button is "
          "released, so the last syllables are not chopped: it covers the frames "
          "still in flight over WebRTC at that moment. Once that time is up the "
          "over is treated as finished — no further microphone audio is accepted, "
          "the silence at the end of it is dropped (below -40 dBFS, with 40 ms "
          "kept after the last sound so a soft final consonant survives), and "
          "only what was actually spoken plays out before the key goes up. This "
          "matters because the browser microphone streams continuously and is "
          "not gated by the PTT button: without that cut-off the queue never ran "
          "empty and every over was held open for the full timeout, half a "
          "second of room noise on the air."),
    ("p", "A digital transmission (CW, RTTY, POCSAG, APRS beacon) owns the mic "
          "line for as long as it is keyed. Queued microphone audio is discarded "
          "when it starts, nothing new is taken while it runs, and the key goes "
          "up the moment its last symbol has gone out — with no voice tail and "
          "no roger beep, which is a voice-mode habit that would only corrupt "
          "the end of the transmission."),
    ("p", "The RX buffer (Settings > Audio, 60 ms by default) is a jitter buffer "
          "between the sound card and the WebRTC track, and it is worth knowing "
          "why it exists. The card produces a 20 ms block on its own clock; the "
          "track emits a frame every 20 ms on the backend's clock. Two "
          "independent clocks sampling each other means that whenever one runs a "
          "few milliseconds early or late, a block goes out twice or is skipped "
          "— and every such seam is an audible click. Under load (the callsign "
          "recogniser runs continuously, and each over costs another ~1.3 s for "
          "its voiceprint) that is frequent enough to sound like crackle. Three "
          "20 ms blocks absorb it; the delay they add is not noticeable in a "
          "QSO. Raise it if the received audio still crackles, lower it for the "
          "shortest possible delay. Each listening browser gets its own queue, "
          "capped so a client that falls behind loses its oldest blocks instead "
          "of accumulating delay; a queue that runs dry refills to the target "
          "before playing out again, because limping along one block deep means "
          "the next hiccup clicks as well."),
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
    ("p", "TX modulation (Settings > Audio). Two stages can be switched into "
          "the transmit path, followed by a peak limiter that is always active "
          "behind them."),
    ("ul", [
        "COMPRESSOR (off by default) raises the AVERAGE modulation: quiet "
        "syllables and trailing words come up by as much as 9 dB while loud "
        "bursts are held back. Measured on a 1 kHz tone, an input range of "
        "−45…−1 dBFS leaves as −36…−9 dBFS, so 44 dB of speech dynamics become "
        "27 dB. It acts within a syllable — unlike the AGC, which rides the "
        "overall level over seconds; both may be on. Below −52 dBFS nothing is "
        "lifted, so the pauses between words keep their room noise in the "
        "background instead of being pumped up to speech level.",
        "PRE-EMPHASIS (on by default) lifts the treble by 6 dB/octave — the "
        "exact inverse of the RX de-emphasis, using the same time constant, and "
        "within 0.2 dB of the analogue curve (+4.6 dB at 3 kHz). Switch it on "
        "ONLY if the transmit audio goes into a FLAT input, i.e. the 9600-baud "
        "data port: the radio's mic input and its 1200-baud input pre-emphasise "
        "by themselves, and doing it twice sounds shrill. The voice low-pass "
        "runs automatically while it is on — without it the lift would send "
        "hiss above the voice band as well (+11 dB at 8 kHz, −44 dB with it).",
        "The LIMITER holds peaks at −1 dBFS whenever either stage is on. It is "
        "not decoration: the emphasis lifts treble before the band-limiting "
        "low-pass, so a hot microphone clipped in 16-bit — 1.9 % of samples at "
        "a −1 dBFS peak — and hard clipping there lands as broadband crackle "
        "INSIDE the voice band, where no filter can take it out again. The "
        "limiter reduces gain over 2.5 ms slices and ramps between them; "
        "measured across the chain, a 0 dBFS input leaves at −1.0 dBFS with not "
        "a single clipped sample.",
    ]),
    ("p", "Try either with MIC TEST: the replay runs through the same chain, so "
          "it is heard without transmitting."),
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
    ("h2", "Digimodes (CW / RTTY / POCSAG / APRS)"),
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
    ("h2", "APRS"),
    ("p", "The fourth digimode, in software: the TM-V71 has no built-in TNC — "
          "that is the TM-D710 — so the whole stack runs on the Pi. Feed it the "
          "flat 9600-baud data output rather than the speaker path: no "
          "de-emphasis, and no squelch chopping a frame in half."),
    ("ul", [
        "RECEIVE decodes Bell 202 AFSK (1200 baud, mark 1200 Hz, space "
        "2200 Hz) into AX.25 and then APRS: uncompressed and compressed "
        "positions, MIC-E — which most mobile stations send — status, messages, "
        "objects, telemetry and weather. Each frame becomes one line with "
        "source, path, position, course, speed and comment; DEBUG adds a second "
        "line with the frame type, symbol, length and the raw payload. The panel "
        "head counts frames, damaged frames, distinct stations and noise.",
        "The CRC check is not optional. Noise regularly produces bit patterns "
        "that look like a frame, and every one of them would be a ghost station "
        "in the list. Measured here: ten seconds of pure noise produced four "
        "such candidates and not one of them survived the check.",
        "A failed check is two very different things, so they are counted "
        "apart. A candidate whose address field still holds twelve plausible "
        "callsign characters was a real transmission, damaged on the way — a "
        "collision, a fade — and is counted as DAMAGED; with DEBUG on, the line "
        "names who was lost. Anything else is noise between two flags and is "
        "counted as such. Random bytes pass the address test with a probability "
        "around 8·10⁻¹¹, and sixty seconds of pure noise produced not one. "
        "Measured on the air: in 152 s, 17 frames received, 5 damaged, 108 "
        "noise candidates — so the raw CRC count said far more about the noise "
        "floor than about lost stations.",
        "TRANSMIT sends a position beacon — BEACON keys PTT, the text field is its "
        "optional comment. The position is the centre of the Maidenhead locator "
        "from Settings > General, so use six characters or more: four is a 1°×2° "
        "field, roughly 111×70 km. It is sent as an uncompressed position "
        "without timestamp, path WIDE1-1, destination APZV71 — the APZ prefix "
        "marks experimental software, and claiming a registered one would "
        "misreport which program is on the air. 300 ms of flags precede the "
        "frame: too short and the receiving TNC loses the first bytes, which "
        "looks like a decoder fault at the far end.",
        "There is no digipeater, no APRS-IS gateway and no automatic repeat. "
        "A beacon that transmits by itself every few minutes is an operating "
        "decision, not a default.",
    ]),
    ("p", "Verified by loop-back — every beacon was decoded again by this "
          "program's own receiver, position, symbol, path and comment intact, "
          "and still error-free at 6 dB signal-to-noise. MIC-E was checked "
          "against known coordinates in both hemispheres, including a "
          "three-digit longitude with its offset. Decoding costs 0.2 ms per "
          "20 ms of audio."),
    ("h2", "Contact columns beside the console"),
    ("p", "On a wide screen the console leaves a few hundred pixels unused on "
          "either side. They hold a copy of the contact cards — the newest in "
          "the left column, the next ones in the right — so the stations heard "
          "stay in view while the panel itself is scrolled away. The tray "
          "background and the empty places are the panel's own, and the free "
          "places are numbered straight through both columns, large and faint, "
          "so an empty one says how far the list reaches."),
    ("ul", [
        "The heads carry the panel's name and, on the left, the total number of "
        "cards — not what fits, so the figure says whether anything is out of "
        "sight. The feet carry the two readings worth having in view: the sum "
        "of all talk timers on the left, the VOICE MATCH lamp with the number "
        "of learned voices on the right.",
        "The copies are fully operable: delete, log to Wavelog, start and stop "
        "the talk timer, mark a card, correct a callsign, and the hover detail. "
        "Every click is routed to the original card and performed by ITS button "
        "— cloning does not copy event handlers, and giving the copies their "
        "own would mean every behaviour existed twice, with the certainty of "
        "drifting apart one day.",
        "One control in the title bar folds both columns away together; they "
        "are one display split over two columns, and leaving one open would "
        "look like a fault rather than a choice. The columns appear from "
        "1620 px of window width (1220 for the console plus room for both) and "
        "never in the mobile deck, which has no margins to put them in.",
    ]),
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
          "enriched from the offline list with the holder's name, address and "
          "licence class (A/E/N); a call that is not assigned is still shown but flagged "
          "VOID. Your own callsign is ignored, it runs only while the squelch is "
          "open, and it can also grade the mic-test audio. The callsign list is "
          "built once from the PDF with a converter (python -m app.callsign_list), "
          "which splits each entry into callsign, class, name, street, postcode "
          "and town, and writes the same list as JSON beside the cache for other "
          "tools (--json-only re-exports it in a second, without re-reading the "
          "PDF; the TSV stays what the service itself loads, being about 40% "
          "faster to read). The register is not consistent about its separator — most "
          "entries use a semicolon between holder and address, a few hundred (club "
          "and relay stations) only a comma — so a comma in front of the postcode "
          "counts as a separator too, and a word hyphenated across a column break "
          "is joined again. "
          "QRZ.com is used only for a manual lookup in the logbook, never by the "
          "ASR."),
    ("p", "Powering the radio down suspends the recognition — there is no RX "
          "audio to analyse — and switching it back on resumes it. Your setting "
          "is not overwritten by this, so detection does not quietly stay off "
          "after a power cycle. The ASR indicator and the detected-callsign field "
          "stay on screen throughout; only the LED changes: green and pulsing "
          "while listening, red while suspended, dim when switched off. The pulse "
          "is what says \u201clistening right now\u201d, so neither off-state pulses."),
    ("h2", "Band scan"),
    ("p", "Sweep a VHF/UHF range or the memory bank and see an occupancy "
          "spectrum + waterfall. Double-click a channel to tune the control VFO "
          "to it."),
    ("h2", "ASR contacts"),
    ("p", "A panel below the band scan that collects every recognised station as "
          "an index card, so the last overs are readable at a glance instead of "
          "as a scrolling log. Cards sit in a tray of empty slots, newest first. "
          "The last 200 entries are kept on the Pi and restored when the panel "
          "opens; CLEAR ALL empties the view. In the mobile deck the panel has no "
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
        "QRZ.com, which the ASR does not query. The town carries its postcode "
        "in front of it, as on an envelope; the street stays behind the card and "
        "travels to the logbook.",
        "The time the station was last heard and its talk timer, pinned to the "
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
    ("p", "Talk time. The clock face in the bottom row of a card is the "
          "start/stop button for that station's timer — and the count also runs "
          "by itself: it starts on the rising edge of BUSY and stops on the "
          "falling one, so an over is timed without touching anything. The clock "
          "always belongs to exactly one card, the marked one. Clicking a card "
          "moves the mark, and with it the timer, onto that contact — for when "
          "the recognition attributed an over to the wrong station. Every card "
          "keeps its own total: switching the focus banks the running stretch on "
          "the card it came from and picks up the new card's figure, so nothing "
          "is lost or counted twice. The display is fixed at hh:mm:ss, otherwise "
          "the button would change width as it counts."),
    ("p", "The panel head carries the sum of all timers (Σ) and five controls:"),
    ("ul", [
        "The input field takes a callsign by hand when the recognition misses "
        "one — upper-case letters and digits only, filtered while typing. LOG "
        "(or Enter) creates the card through the backend exactly like a "
        "recognised one, so it lands in the history and reaches every connected "
        "client. A message says whether the BNetzA list knew the call.",
        "MOD marks the focused card as net control. Its timer keeps running and "
        "stays readable on the card, but it is left out of every figure: neither "
        "the Σ total nor the ranking counts it, and it does not set the scale "
        "of the bars — without that, the station holding the round together "
        "would flatten every other bar. A flagged card wears a ring around its "
        "avatar. The flag lives in the browser (local storage), not on the Pi: it "
        "belongs to whoever is listening to this round, and it survives a reload.",
        "STATS opens the evaluation: every card by talk time, longest first, "
        "with callsign, name, a bar and the time. The bar is scaled against the "
        "longest over rather than against the sum. Moderators are listed below "
        "the ranking, without a bar and without a rank. COPY puts the list on the "
        "clipboard as plain text. The dialog keeps counting while a station is "
        "being heard, so it can stay open through an over.",
        "CLEAR ALL empties the log — in dark red, because it takes every card "
        "at once, on the Pi and in every open client, and asks before it does. "
        "The talk times go with them (they live in the browser); the learned "
        "voices do not — clearing a list is not the same statement as “this "
        "was wrong”, which is what the cross on a single card means.",
        "VOICE MATCH, at the right-hand end, switches the speaker recognition on "
        "and off (see below). Its lamp pulses green while it is listening; an "
        "outlined button means it only observes, a filled one that it acts.",
    ]),
    ("p", "Correcting a callsign: click the call on the card and type over it "
          "(Enter applies, Escape and clicking away cancel). The card keeps its "
          "talk time, its repeat count and its place, name and town are re-read "
          "for the corrected call, and the change reaches every open client. If "
          "the corrected call already has a card, the two are merged and their "
          "times added. The voice profile is renamed with it — otherwise a "
          "mis-heard call would keep collecting a voiceprint under a name that "
          "never existed while the real station never gets one."),
    ("p", "The interface is in English throughout — labels, hints, messages and "
          "error texts. This manual exists in German as well; the labels quoted "
          "in it are the English ones, because those are what is on screen."),
    ("h2", "Voice ID — an over without a callsign"),
    ("p", "Not every over carries a spoken callsign, and the talk timer then "
          "counts onto whoever was marked last. Speaker recognition closes that "
          "gap: the voiceprint of an over is compared against the stations whose "
          "callsign HAS been heard. Enrolment costs nothing — every recognised "
          "call labels its own audio, so the profiles build themselves while the "
          "panel is simply used."),
    ("ul", [
        "An over ends at the PAUSE in speech, not at the carrier. Through a "
        "repeater BUSY stays up for the whole QSO, so the falling edge never "
        "comes and every station would land in one segment with their voices "
        "averaged together. The pause boundary works in simplex too, where the "
        "carrier drop simply arrives first.",
        "Under about 3 s of speech nothing is guessed at: the over is skipped. "
        "An over in which two callsigns were heard is skipped as well, because "
        "labelling it would spoil both profiles.",
        "Two stages. Observe only decides and reports — the verdict appears "
        "above the card tray and in the card's hover detail, and nothing moves. "
        "Assign additionally moves the mark and the talk timer onto the "
        "recognised station; such a card is drawn with a dashed border so it is "
        "never confused with the solid red of \u201cjust heard saying its call\u201d. "
        "The stage is chosen in Settings > Audio > Voice ID.",
        "A card picked BY HAND always wins: while a manual selection holds, the "
        "voice moves neither the mark nor the timer. It is released when the "
        "next over begins, so the correction applies to the over it was made in. "
        "A callsign actually heard still moves the mark — that is evidence, not "
        "a guess.",
        "It needs the callsign recognition switched on (it listens to the same "
        "audio tap), and it costs about 1.3 s of one core per over, afterwards.",
    ]),
    ("p", "Measured on two off-air recordings of the same round (14 + 11 overs, "
          "four stations in both): the same station across recordings scored "
          "0.62–0.79 cosine, different stations 0.33 on average and 0.64 at "
          "worst. At a threshold of 0.55 with a 0.05 margin over the runner-up, "
          "3 of the 4 known stations were identified, none wrongly, and none of "
          "the 7 stations unknown to the profile set was mistaken for a known "
          "one. The margin does the real work: a stranger's best match is flat "
          "(0.01–0.08 ahead of the second), a true match stands clear "
          "(0.20–0.34). Treat it as an assistant that may abstain, not as proof "
          "— and never log a QSO on a voice match alone."),
    ("p", "The model is Vosk's own speaker model (vosk-model-spk-0.4, ~14 MB), "
          "an x-vector trained on 8 kHz telephone speech — narrowband, noisy, "
          "channel-varied, which is far closer to FM off a repeater than a "
          "wideband model would be. A voiceprint is biometric data: the profiles "
          "stay on the Pi in speakers.json (gitignored, never uploaded) and a "
          "profile is dropped with its card."),
    ("h2", "Logbook (Wavelog + QRZ.com)"),
    ("p", "Logs QSOs to a locally installed Wavelog instance. Enter just the "
          "callsign (and optionally a name) — frequency, band, mode, date/time and "
          "your own callsign are filled in automatically from the live control "
          "band and station profile. LOOKUP fetches the operator's name, grid, "
          "QTH, country and e-mail from QRZ.com (XML data API) and 'worked before' "
          "/ DXCC from Wavelog. The ADDRESS field beside QTH takes the holder's "
          "street and postcode; logging a contact straight from an ASR card fills "
          "it in from the BNetzA list, and it travels as the ADIF field ADDRESS "
          "while QTH keeps the plain town, so Wavelog's statistics stay clean. "
          "LOG QSO sends the contact as ADIF. A green dot "
          "shows when Wavelog is reachable; the panel lists the most recent QSOs "
          "(with the looked-up details, deletable individually or via CLEAR) and "
          "Wavelog's QSO counts (today/month/year/total). Configure the Wavelog "
          "URL, API token and station profile, and the QRZ.com username/password, "
          "in Settings > Logging. Credentials are stored only on the Pi "
          "(runtime.json), never committed."),
    ("h2", "Settings"),
    ("p", "Tabs: General (callsign, API backend URL, serial port/baud, GPIO power, "
          "auto power-off, logo, GitHub self-update, Root-CA download), Audio "
          "(device, USB mixer, voice filters, test tone, audio timing), Rig-Info, "
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
    ("p", "Alles Folgende läuft auf dem Pi. Das Funkgerät hängt über zwei "
          "getrennte Wege daran — CAT-Befehle auf der seriellen Leitung, Audio "
          "über die Datenbuchse und eine USB-Soundkarte —, und der Browser sieht "
          "nur die eine HTTPS-Adresse, die das Backend ausliefert."),
    ("diagram", "system", "de"),
    ("h2", "Audiopfad"),
    ("p", "Ein einziger Aufnahme-Callback verwaltet das Empfangssignal. Was der "
          "Browser hört, ist aufbereitet und gesquelcht; jeder Decoder bekommt "
          "seinen Abgriff DAVOR, vom rohen Signal — deshalb arbeiten S-Meter, "
          "Rekorder und beide Vosk-Ketten weiter, während MUTE oder DROP aktiv "
          "ist. Vosk steht mit Absicht zweimal im Bild: Der grammatikgebundene "
          "Dekoder liest gesprochene Rufzeichen, ein zweiter, schlichter "
          "Erkenner mit dem Sprechermodell macht aus einem ganzen Durchgang "
          "einen Stimmabdruck (der Grammatik-Dekoder läuft mit N-best-"
          "Alternativen, und in dieser Betriebsart liefert Vosk keinen "
          "X-Vektor)."),
    ("diagram", "audio", "de"),
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
    ("h2", "Anschluss der USB-Soundkarte"),
    ("p", "Das Audio nutzt nicht eine Buchse, sondern zwei — und die sitzen auf "
          "gegenüberliegenden Seiten des Geräts: Das Sendesignal geht in die "
          "Mikrofonbuchse an der Seite des Bedienteils, das Empfangssignal kommt "
          "aus der Datenbuchse auf der Rückseite. Diese Aufteilung ist keine "
          "Geschmacksfrage, sie folgt daraus, wie das Gerät seine Pfade "
          "umschaltet — siehe die Anmerkung unter dem Bild. Die Beschriftung des "
          "Schaltbilds bleibt englisch: So stehen die Buchsen- und Pinnamen im "
          "Handbuch des Funkgeräts."),
    ("diagram", "wiring", "de"),
    ("ul", [
        "Der Sendezweig muss rund 40 dB verlieren: Die Karte gibt Line-Pegel "
        "(~1 V) ab, der Mikrofoneingang erwartet wenige Millivolt an 600 Ohm. "
        "Ein 1:1-Übertrager in dieser Leitung trennt zugleich die Masseschleife "
        "zwischen Pi-Netzteil und Funkgerät — sie ist meist die Ursache, wenn "
        "das eigene Sendesignal brummt oder die Lichtmaschine mitpfeift.",
        "PR1 (Pin 5) führt das 1200-Baud-Empfangsaudio: gefiltert, mit festem "
        "Pegel und unabhängig vom Lautstärkeregler — was der Pi hört, ändert "
        "sich also nicht, wenn man das Gerät leiser dreht. PR9 (Pin 4) ist der "
        "flache 9600-Baud-Ausgang direkt vom Diskriminator und hier der falsche "
        "Pin, solange die De-Emphasis nicht in Software wieder zugeschaltet wird.",
        "PTT wird über CAT am PC-Port getastet, eine PTT-Leitung ist deshalb "
        "nicht verdrahtet. Nichts in der Audioverkabelung kann das Gerät tasten.",
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
          "absteigenden Dreiklang hinzu (d6 · a5 · e5, also 1175/880/659 Hz, je "
          "60 ms mit 30 ms Pause), in der in den Audio-Einstellungen gewählten "
          "Lautstärke; während des Sendens zeigt der Knopf "
          "einen aufwärts laufenden Timer (MM:SS). Die 1750-Hz-Taste schärft einen "
          "Tonruf. Die Speicher-Schnelltasten rufen die Kanäle 0–16 ab (M0–M9 in "
          "der linken, M10–M16 in der rechten Spalte; die Taste des geladenen "
          "Kanals leuchtet); darunter sendet die rechte Spalte drei DTMF-Speicher "
          "(0–2). Eine Statuszeile zeigt BUSY je Band, den ASR-Zustand und den "
          "Live-RX/TX-Gain (in der PWA; am Desktop steht dort der Sende-Hinweis). "
          "Auf dem Handy flankieren Mini-RX/TX-VU-Bars mit Peak-Hold den Knopf."),
    ("h2", "MUTE und DROP"),
    ("p", "Zwei Knöpfe flankieren den Schriftzug \u201ePTT \u2192 BAND x\u201c und "
          "schalten den Empfangston stumm; sie unterscheiden sich nur darin, wann "
          "sie wieder aufhören:"),
    ("ul", [
        "MUTE (links) ist der schlichte: Er bleibt, bis er erneut gedrückt wird. "
        "Die Beschriftung lautet dann MUTED.",
        "DROP (rechts) blendet die laufende Aussendung aus und hebt sich auf, "
        "sobald diese Station die Taste loslässt — für einen Störer oder ein "
        "Gespräch, das man nicht mithören möchte, ohne hinterher an das Aufheben "
        "denken zu müssen.",
    ]),
    ("p", "Beide leuchten im aktiven Zustand in der Farbe des Sendebands. Drei "
          "Punkte sind erwähnenswert:"),
    ("ul", [
        "DROP gibt auf der fallenden Flanke des BUSY-Signals frei, nicht schon "
        "deshalb, weil der Kanal gerade frei ist: Auf einem stillen Kanal "
        "aktiviert, bleibt der Ton bis zum Ende der nächsten Aussendung stumm, "
        "statt beim nächsten Statuswechsel sofort wieder aufzugehen.",
        "DROP gibt außerdem nach drei Sekunden ohne Sprache frei, auch wenn "
        "BUSY noch ansteht — der Dauerträger eines Relais hielte den Ton sonst "
        "unbegrenzt stumm. Die Schwelle liegt auf dem NF-Pegel: Ein stiller "
        "Träger gilt als Stille, eine Aussendung nicht.",
        "Stummgeschaltet wird im Browser, am Audio-Element. S-Meter, "
        "Roh-Rekorder und Rufzeichenerkennung bekommen das Signal weiterhin — "
        "still ist nur, was man hört.",
        "Ein Dreiklang bestätigt jeden Wechsel (e5 · a5 · d6 aufsteigend bei der "
        "Freigabe, derselbe Dreiklang rückwärts beim Stummschalten), sodass "
        "beides ohne Hinsehen zu unterscheiden ist. Er entsteht im Browser und "
        "kann deshalb nie ins Sendesignal geraten — anders als der Roger-Beep, "
        "der bewusst dorthin gehört.",
    ]),
    ("h2", "Audio (WebRTC/Opus)"),
    ("p", "Das AUDIO-Panel öffnen, mit dem RX-A/RX-B-Schalter das Empfangsband "
          "wählen, CONNECT klicken und das Mikrofon erlauben. RX- und Mic-Pegel "
          "werden live angezeigt, dazu die WebRTC-RX/TX-Datenrate in der Graph-Ecke. "
          "Bedienelemente: RX/TX-Gain (mit Default-Markierung), MIC (Mic-Test — "
          "misst ohne zu tasten, nimmt im Betrieb auf und spielt beim Ausschalten "
          "über RX zurück; RX ist dabei stumm), AGC (automatischer TX-Pegel) sowie "
          "ein kleiner Rekorder — ● REC / ▶ PLAY plus WAV-Download des rohen, "
          "un-gesquelchten RX-Signals (bis 60 min; z. B. für ASR-Trainingsdaten; der "
          "Dateiname des Downloads trägt Datum und Uhrzeit der Sicherung). "
          "Audio-Timing (TX-Buffer / TX-Trail / RX-Buffer) und der USB-Mixer "
          "liegen unter Einstellungen > Audio. Die Verbindung verbindet sich nach "
          "einer Netzstörung automatisch neu und wird beim nächsten Start "
          "wiederhergestellt."),
    ("p", "Der TX-Buffer begrenzt, wie viel Mikrofonaudio sich für das "
          "Funkgerät stauen darf. Er muss größer sein als die Schübe, in denen "
          "der Browser liefert: Hier gemessen wurden mit 40 ms Deckel 2,3 s "
          "Stille eingefügt, weil nichts da war, während gleichzeitig 2,5 s am "
          "Deckel verworfen wurden — verhungern und überlaufen zugleich, hörbar "
          "als Aussetzer. 150 ms sind die Voreinstellung, der Regler endet bei "
          "80 ms, weil darunter kein Wert funktionieren kann. Die Hälfte davon "
          "(40–80 ms) wird als Polster gesammelt, bevor die Wiedergabe "
          "beginnt."),
    ("p", "Der TX-Trail ist die Zeit, die das Funkgerät nach dem Loslassen der "
          "Taste noch getastet bleibt, damit die letzten Silben nicht "
          "abgeschnitten werden: Sie deckt die Rahmen ab, die in diesem Moment "
          "noch über WebRTC unterwegs sind. Danach gilt der Durchgang als "
          "beendet — es wird kein Mikrofonaudio mehr angenommen, die Stille am "
          "Ende wird verworfen (unter -40 dBFS, wobei nach dem letzten Laut "
          "40 ms stehen bleiben, damit ein leiser Endkonsonant erhalten bleibt), "
          "und nur das tatsächlich Gesprochene geht noch hinaus. Das ist "
          "nötig, weil das Browser-Mikrofon ununterbrochen sendet und nicht von "
          "der PTT-Taste abhängt: Ohne diesen Schnitt lief die Warteschlange nie "
          "leer, und jeder Durchgang blieb bis zum Maximum offen — eine halbe "
          "Sekunde Raumgeräusch auf der Frequenz."),
    ("p", "Eine digitale Aussendung (CW, RTTY, POCSAG, APRS-Bake) besitzt die "
          "Mikrofonleitung, solange sie getastet ist. Wartendes Mikrofonaudio "
          "wird zu Beginn verworfen, währenddessen nichts Neues angenommen, und "
          "die Taste geht auf, sobald das letzte Symbol draußen ist — ohne "
          "Sprachnachlauf und ohne Roger-Beep, der aus dem Sprechfunk stammt und "
          "das Ende der Aussendung nur verfälschen würde."),
    ("p", "Der RX-Buffer (Einstellungen > Audio, standardmäßig 60 ms) ist ein "
          "Jitterpuffer zwischen Soundkarte und WebRTC-Track — und es lohnt zu "
          "wissen, warum es ihn gibt. Die Karte liefert alle 20 ms einen Block "
          "auf ihrer eigenen Uhr; der Track gibt alle 20 ms einen Rahmen auf der "
          "Uhr des Backends aus. Zwei unabhängige Uhren, die einander abtasten: "
          "Läuft eine ein paar Millisekunden vor oder nach, geht ein Block "
          "zweimal hinaus oder fällt aus — und jede dieser Nahtstellen ist ein "
          "hörbarer Klick. Unter Last (die Rufzeichenerkennung läuft dauernd, je "
          "Durchgang kommen ~1,3 s für den Stimmabdruck dazu) passiert das oft "
          "genug, um als Knattern durchzugehen. Drei 20-ms-Blöcke fangen das ab; "
          "die zusätzliche Verzögerung fällt im QSO nicht auf. Höher stellen, "
          "wenn der Empfangston weiterhin knattert, niedriger für die kürzeste "
          "Verzögerung. Jeder zuhörende Browser bekommt seine eigene "
          "Warteschlange, gedeckelt, damit ein hängender Client seine ältesten "
          "Blöcke verliert statt Verzögerung anzuhäufen; eine leergelaufene "
          "Schlange füllt erst wieder auf die Solltiefe, denn einen Block tief "
          "weiterzuhumpeln hieße, dass der nächste Aussetzer ebenfalls klickt."),
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
    ("p", "TX-Modulation (Einstellungen > Audio). Zwei Stufen lassen sich in "
          "den Sendeweg schalten, dahinter arbeitet immer ein Spitzenbegrenzer."),
    ("ul", [
        "KOMPRESSOR (standardmäßig aus) hebt den MITTLEREN Hub: Leise Silben "
        "und auslaufende Wörter kommen um bis zu 9 dB herauf, laute Stellen "
        "werden zurückgehalten. Am 1-kHz-Ton gemessen wird aus einem "
        "Eingangsbereich von −45…−1 dBFS ein Ausgang von −36…−9 dBFS, aus 44 dB "
        "Sprachdynamik also 27 dB. Er wirkt innerhalb einer Silbe — anders als "
        "die AGC, die den Gesamtpegel über Sekunden nachführt; beides darf "
        "gleichzeitig an sein. Unter −52 dBFS wird nichts angehoben, damit das "
        "Raumrauschen in den Sprechpausen im Hintergrund bleibt statt auf "
        "Sprachpegel hochgezogen zu werden.",
        "PRE-EMPHASIS (standardmäßig an) hebt die Höhen um 6 dB pro Oktave an — "
        "die genaue Umkehrung der RX-De-Emphasis, mit derselben Zeitkonstante "
        "und auf 0,2 dB an der analogen Kurve (+4,6 dB bei 3 kHz). NUR "
        "einschalten, wenn das Sendeaudio in einen LINEAREN Eingang geht, also "
        "die 9600-Baud-Datenbuchse: Der Mikrofoneingang des Geräts und sein "
        "1200-Baud-Eingang machen die Anhebung selbst, doppelt klingt es "
        "schrill. Der Sprach-Tiefpass läuft dabei zwingend mit — ohne ihn ginge "
        "auch das Rauschen oberhalb des Sprachbands angehoben hinaus (+11 dB "
        "bei 8 kHz, mit Tiefpass −44 dB).",
        "Der BEGRENZER hält die Spitzen bei −1 dBFS, sobald eine der beiden "
        "Stufen an ist. Er ist kein Zierrat: Die Emphasis hebt die Höhen vor "
        "dem bandbegrenzenden Tiefpass an, ein heißes Mikrofon lief damit in "
        "die 16-Bit-Grenze — bei −1 dBFS Spitze 1,9 % aller Samples — und "
        "hartes Clipping landet dort als breitbandiges Knistern MITTEN im "
        "Sprachband, wo es kein Filter mehr herausholt. Der Begrenzer regelt in "
        "2,5-ms-Scheiben und verschleift dazwischen; über die ganze Kette "
        "gemessen verlässt ein Signal mit 0 dBFS die Kette mit −1,0 dBFS, ohne "
        "ein einziges geklipptes Sample.",
    ]),
    ("p", "Ausprobieren lässt sich beides mit MIC TEST: Die Wiedergabe läuft "
          "durch dieselbe Kette, man hört es also, ohne zu senden."),
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
    ("h2", "Digimodes (CW / RTTY / POCSAG / APRS)"),
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
    ("h2", "APRS"),
    ("p", "Die vierte Betriebsart, vollständig in Software: Der TM-V71 hat "
          "keinen eingebauten TNC — das ist der TM-D710 —, der ganze Stapel "
          "läuft also auf dem Pi. Zuführen sollte man den flachen "
          "9600-Baud-Datenausgang statt des Lautsprecherwegs: keine "
          "De-Emphasis, und keine Rauschsperre, die einen Rahmen zerhackt."),
    ("ul", [
        "EMPFANG dekodiert Bell-202-AFSK (1200 Baud, Mark 1200 Hz, Space "
        "2200 Hz) über AX.25 zu APRS: Positionen unkomprimiert und komprimiert, "
        "MIC-E — was die meisten Mobilstationen senden —, Status, Nachrichten, "
        "Objekte, Telemetrie und Wetter. Jeder Rahmen wird eine Zeile mit "
        "Quelle, Pfad, Position, Kurs, Tempo und Kommentar; DEBUG ergänzt eine "
        "zweite Zeile mit Rahmentyp, Symbol, Länge und roher Nutzlast. Die "
        "Titelzeile zählt Rahmen, beschädigte Rahmen, verschiedene Stationen "
        "und Rauschkandidaten.",
        "Die CRC-Prüfung ist nicht verhandelbar. Rauschen erzeugt regelmäßig "
        "Bitmuster, die wie ein Rahmen aussehen, und jedes davon wäre eine "
        "Geisterstation in der Liste. Hier gemessen: Zehn Sekunden reines "
        "Rauschen brachten vier solche Kandidaten hervor — keiner überstand die "
        "Prüfsumme.",
        "Eine fehlgeschlagene Prüfung sind zwei sehr verschiedene Dinge, "
        "deshalb werden sie getrennt gezählt. Ein Kandidat, dessen Adressfeld "
        "noch zwölf plausible Rufzeichen-Zeichen enthält, war eine echte "
        "Aussendung, die unterwegs beschädigt wurde — Kollision, Schwund — und "
        "zählt als BESCHÄDIGT; bei eingeschaltetem DEBUG nennt die Zeile, wer "
        "verloren ging. Alles andere ist Rauschen zwischen zwei Flaggen und "
        "wird als solches gezählt. Zufällige Bytes bestehen die Adressprüfung "
        "mit einer Wahrscheinlichkeit um 8·10⁻¹¹, und sechzig Sekunden reines "
        "Rauschen brachten keinen einzigen hervor. Auf dem Band gemessen: in "
        "152 s 17 empfangene Rahmen, 5 beschädigte, 108 Rauschkandidaten — die "
        "reine CRC-Zahl sagte also weit mehr über den Störpegel als über "
        "verlorene Stationen.",
        "SENDEN heißt Positionsbake: BEACON tastet PTT, das Textfeld ist der "
        "optionale Kommentar. Die Position ist die Mitte des "
        "Maidenhead-Locators aus Einstellungen > Allgemein, weshalb sechs "
        "Zeichen oder mehr nötig sind: Vier Zeichen sind ein 1°×2°-Feld, also "
        "rund 111×70 km. Gesendet wird eine unkomprimierte Position ohne "
        "Zeitstempel, Pfad WIDE1-1, Ziel APZV71 — das Kürzel APZ kennzeichnet "
        "experimentelle Software; eine registrierte Kennung zu verwenden würde "
        "falsch melden, welches Programm auf dem Band ist. Dem Rahmen gehen "
        "300 ms Flaggen voraus: zu wenig, und die Gegenstelle verliert die "
        "ersten Bytes, was dort wie ein Decoderfehler aussieht.",
        "Digipeater, APRS-IS-Gateway und automatische Wiederholung gibt es "
        "nicht. Eine Bake, die von selbst alle paar Minuten sendet, ist eine "
        "Betriebsentscheidung und keine Voreinstellung.",
    ]),
    ("p", "Nachgewiesen über die Rückschleife — jede Bake wurde vom eigenen "
          "Empfänger dieses Programms wieder gelesen, mit Position, Symbol, "
          "Pfad und Kommentar, und bis 6 dB Störabstand fehlerfrei. MIC-E wurde "
          "gegen bekannte Koordinaten auf beiden Halbkugeln geprüft, "
          "einschließlich dreistelliger Länge mit Offset. Das Dekodieren kostet "
          "0,2 ms je 20 ms Audio."),
    ("h2", "Kontaktspalten neben der Konsole"),
    ("p", "Auf einem breiten Bildschirm bleiben links und rechts der Konsole "
          "einige hundert Pixel ungenutzt. Dort steht eine Kopie der "
          "Kontaktkarten — die neuesten in der linken Spalte, die nächsten in "
          "der rechten —, sodass die gehörten Stationen im Blick bleiben, "
          "während das Panel selbst weggescrollt ist. Hintergrund und leere "
          "Fächer sind die des Panels, und die freien Plätze sind groß und "
          "blass durchnummeriert, über beide Spalten hinweg: Ein leeres Fach "
          "sagt damit auch, wie weit die Liste reicht."),
    ("ul", [
        "Die Köpfe tragen den Namen des Panels und links die Gesamtzahl der "
        "Karten — nicht die der sichtbaren, damit die Zahl verrät, ob etwas "
        "außer Sicht liegt. Die Füße tragen die beiden Werte, die man im Auge "
        "behalten will: links die Summe aller Redezeiten, rechts die "
        "VOICE-MATCH-Lampe mit der Zahl der gelernten Stimmen.",
        "Die Kopien sind voll bedienbar: löschen, ins Wavelog eintragen, "
        "Redezeit starten und stoppen, Karte markieren, Rufzeichen korrigieren "
        "und die Detailanzeige beim Überfahren. Jeder Klick wird an die "
        "Originalkarte weitergereicht und von DEREN Knopf ausgeführt — Klonen "
        "kopiert keine Ereignisbehandler, und eigene zu vergeben hieße, jedes "
        "Verhalten zweimal im Quelltext zu haben, mit der sicheren Aussicht, "
        "dass beide Fassungen eines Tages auseinanderlaufen.",
        "Ein Schalter in der Titelzeile klappt beide Spalten gemeinsam weg; sie "
        "sind eine Anzeige auf zwei Spalten verteilt, und eine offen und eine "
        "zu sähe nach Fehler aus statt nach Entscheidung. Die Spalten "
        "erscheinen ab 1620 px Fensterbreite (1220 für die Konsole plus Platz "
        "für beide) und nie im mobilen Deck, das keine Ränder dafür hat.",
    ]),
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
          "angereichert aus der Offline-Liste mit Name, Anschrift und Klasse (A/E/N); "
          "ein nicht zugeteiltes Rufzeichen wird dennoch angezeigt, aber als VOID "
          "markiert. Das eigene Rufzeichen wird ignoriert, es läuft nur bei "
          "offener Rauschsperre und kann auch das Mic-Test-Audio auswerten. Die "
          "Rufzeichenliste wird einmalig per Converter aus der PDF erzeugt "
          "(python -m app.callsign_list), der jeden Eintrag in Rufzeichen, Klasse, "
          "Name, Straße, PLZ und Ort zerlegt und dieselbe Liste zusätzlich als "
          "JSON neben den Cache schreibt, für andere Werkzeuge (--json-only "
          "erzeugt sie in einer Sekunde neu, ohne die PDF noch einmal zu lesen; "
          "geladen wird vom Dienst weiterhin die TSV-Fassung, sie ist rund 40 % "
          "schneller zu lesen). Das Register trennt nicht "
          "einheitlich — meist steht ein Semikolon zwischen Inhaber und Anschrift, "
          "bei einigen hundert Klub- und Relaisstationen nur ein Komma —, deshalb "
          "gilt auch ein Komma vor der Postleitzahl als Trenner, und ein am "
          "Spaltenumbruch getrenntes Wort wird wieder zusammengesetzt. "
          "QRZ.com wird nur bei der manuellen "
          "Abfrage im Logbuch genutzt, nie von der ASR."),
    ("p", "Wird das Funkgerät abgeschaltet, setzt die Erkennung aus — es gibt "
          "kein RX-Audio zu analysieren — und nimmt beim Einschalten wieder auf. "
          "Die gespeicherte Einstellung wird dabei nicht überschrieben, die "
          "Erkennung bleibt nach einem Aus- und Einschalten also nicht "
          "unbemerkt abgeschaltet. ASR-Anzeige und Rufzeichenfeld bleiben "
          "durchgehend sichtbar; nur die LED wechselt: grün pulsierend im "
          "Betrieb, rot während des Aussetzens, gedimmt bei ausgeschalteter "
          "Erkennung. Der Puls bedeutet \u201eh\u00f6rt gerade zu\u201c — deshalb pulst "
          "keiner der beiden Aus-Zustände."),
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
          "her; CLEAR ALL leert die Ansicht. Im mobilen Deck hat das Panel keinen "
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
        "QRZ.com, das die ASR nicht abfragt. Vor dem Ort steht die Postleitzahl "
        "wie auf einem Briefumschlag; die Straße bleibt hinter der Karte und "
        "wandert ins Logbuch.",
        "Die Uhrzeit der letzten Nennung und die Redezeit der Station, fest an "
        "der Unterkante der Karte.",
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
    ("p", "Redezeit. Die Uhr in der Fußzeile einer Karte ist der Start/Stopp-"
          "Knopf für den Zeitzähler dieser Station — und der Zähler läuft auch "
          "von allein: Er startet auf der steigenden Flanke von BUSY und hält "
          "auf der fallenden, ein Durchgang wird also ohne Zutun erfasst. Die "
          "Uhr gehört immer genau einer Karte, der markierten. Ein Klick auf "
          "eine Karte verschiebt die Markierung und mit ihr den Zähler dorthin "
          "— für den Fall, dass die Erkennung einen Durchgang der falschen "
          "Station zugeschrieben hat. Jede Karte führt ihre eigene Summe: Beim "
          "Fokuswechsel wird der laufende Abschnitt auf der bisherigen Karte "
          "verbucht und der Wert der neuen übernommen, es geht also nichts "
          "verloren und nichts wird doppelt gezählt. Die Anzeige steht fest auf "
          "hh:mm:ss, sonst änderte der Knopf im Zählen seine Breite."),
    ("p", "Die Titelzeile des Panels trägt die Summe aller Zähler (Σ) und fünf "
          "Bedienelemente:"),
    ("ul", [
        "Das Eingabefeld nimmt ein Rufzeichen von Hand auf, wenn die Erkennung "
        "eines verpasst — nur Großbuchstaben und Ziffern, bereits beim Tippen "
        "gefiltert. LOG (oder die Eingabetaste) legt die Karte über das Backend "
        "an, genau wie bei einer erkannten Station: Sie landet in der Historie "
        "und erreicht alle verbundenen Clients. Eine Meldung sagt, ob die "
        "BNetzA-Liste das Rufzeichen kannte.",
        "MOD kennzeichnet die markierte Karte als Moderator. Deren Zeit läuft "
        "weiter und bleibt auf der Karte ablesbar, wird aber nirgends gewertet: "
        "weder in der Σ-Summe noch in der Rangliste, und sie bestimmt auch nicht "
        "den Maßstab der Balken — sonst drückte die Station, die die Runde "
        "zusammenhält, alle anderen Balken flach. Eine gekennzeichnete Karte "
        "trägt einen Ring um den Avatar. Die Kennzeichnung liegt im Browser "
        "(Local Storage), nicht auf dem Pi: Sie gehört dem, der diese Runde "
        "mithört, und übersteht einen Reload.",
        "STATS öffnet die Auswertung: alle Karten nach Redezeit, die längste "
        "zuerst, mit Rufzeichen, Name, Balken und Zeit. Der Balken bezieht sich "
        "auf die längste Redezeit, nicht auf die Summe. Moderatoren stehen ohne "
        "Balken und ohne Rang unter der Rangliste. COPY legt die Liste als "
        "Klartext in die Zwischenablage. Der Dialog zählt weiter, solange eine "
        "Station zu hören ist, kann also über einen Durchgang hinweg offen "
        "bleiben.",
        "CLEAR ALL leert das Protokoll — in Dunkelrot, weil es alle Karten auf "
        "einmal betrifft, auf dem Pi und in jedem offenen Client, und deshalb "
        "vorher nachfragt. Die Redezeiten gehen mit (sie liegen im Browser), "
        "die gelernten Stimmen nicht: Eine Liste zu leeren ist nicht dieselbe "
        "Aussage wie „das war falsch“, die das ✕ auf einer einzelnen Karte "
        "trifft.",
        "VOICE MATCH am rechten Ende schaltet die Stimmerkennung ein und aus (siehe "
        "unten). Die Lampe pulst grün, solange sie zuhört; ein umrandeter Knopf "
        "bedeutet, dass sie nur beobachtet, ein gefüllter, dass sie eingreift.",
    ]),
    ("p", "Rufzeichen korrigieren: auf das Rufzeichen der Karte klicken und "
          "überschreiben (Eingabetaste übernimmt, Escape und Wegklicken "
          "verwerfen). Die Karte behält Redezeit, Zähler und Platz, Name und Ort "
          "werden für das korrigierte Rufzeichen neu gelesen, und die Änderung "
          "erreicht jeden offenen Client. Hat das korrigierte Rufzeichen schon "
          "eine Karte, werden beide verschmolzen und ihre Zeiten addiert. Das "
          "Stimmprofil zieht mit um — sonst sammelte ein verhörtes Rufzeichen "
          "weiter einen Stimmabdruck unter einem Namen, den es nie gab, während "
          "die wirkliche Station nie einen bekommt."),
    ("p", "Die Oberfläche ist durchgehend englisch — Beschriftungen, Hinweise, "
          "Meldungen und Fehlertexte. Dieses Handbuch bleibt deutsch; die darin "
          "genannten Beschriftungen sind die englischen, weil genau die auf dem "
          "Bildschirm stehen."),
    ("h2", "Stimmerkennung — ein Durchgang ohne Rufzeichen"),
    ("p", "Nicht in jedem Durchgang fällt ein Rufzeichen, und die Redezeit "
          "zählt dann auf die zuletzt markierte Karte. Die Sprechererkennung "
          "schließt diese Lücke: Der Stimmabdruck eines Durchgangs wird mit den "
          "Stationen verglichen, deren Rufzeichen schon einmal zu hören war. Das "
          "Einlernen kostet nichts — jeder erkannte Ruf beschriftet seine eigene "
          "Aufnahme, die Profile bauen sich also im laufenden Betrieb auf."),
    ("ul", [
        "Ein Durchgang endet an der SPRECHPAUSE, nicht am Träger. Über ein "
        "Relais bleibt BUSY die ganze Verbindung stehen, die fallende Flanke "
        "kommt also nie, und alle Stationen lägen in einem Segment mit zu Brei "
        "gemittelten Stimmen. Die Pausengrenze trägt auch im Simplexbetrieb, wo "
        "der Trägerabfall schlicht früher kommt.",
        "Unter etwa 3 s Sprache wird nichts geraten, der Durchgang entfällt. "
        "Ebenso ein Durchgang, in dem zwei Rufzeichen fielen — ihn zu "
        "beschriften würde beide Profile verderben.",
        "Zwei Stufen. „Observe“ entscheidet und meldet nur — das Urteil steht "
        "über dem Kartenkasten und in den Detailangaben der Karte, bewegt wird "
        "nichts. „Assign“ verschiebt zusätzlich Marke und Redezeit auf die "
        "erkannte Station; eine solche Karte wird gestrichelt umrandet und ist "
        "damit nie mit dem durchgezogenen Rot von „hat gerade sein Rufzeichen "
        "genannt“ zu verwechseln. Die Stufe wird unter Einstellungen > Audio > "
        "Voice ID gewählt.",
        "Eine VON HAND gewählte Kachel hat immer Vorrang: Solange sie gilt, "
        "verschiebt die Stimme weder Marke noch Timer. Sie wird mit dem Beginn "
        "des nächsten Durchgangs frei, die Korrektur gilt also für den "
        "Durchgang, in dem sie gemacht wurde. Ein tatsächlich gehörtes "
        "Rufzeichen verschiebt die Marke weiterhin — das ist Beleg, keine "
        "Vermutung.",
        "Sie setzt die eingeschaltete Rufzeichenerkennung voraus (sie hört am "
        "selben Audio-Abgriff mit) und kostet je Durchgang etwa 1,3 s auf einem "
        "Kern, im Anschluss.",
    ]),
    ("p", "Gemessen an zwei Mitschnitten derselben Runde (14 + 11 Durchgänge, "
          "vier Stationen in beiden): dieselbe Station über zwei Aufnahmen "
          "hinweg 0,62–0,79 Kosinus, verschiedene Stationen im Mittel 0,33 und "
          "im schlechtesten Fall 0,64. Bei Schwelle 0,55 mit 0,05 Abstand zum "
          "Zweitplatzierten wurden 3 der 4 bekannten Stationen erkannt, keine "
          "falsch, und keine der 7 dem Profilbestand fremden Stationen mit einer "
          "bekannten verwechselt. Der Abstand leistet dabei die eigentliche "
          "Arbeit: Bei einer fremden Stimme liegt der beste Treffer flach vorn "
          "(0,01–0,08), bei einer echten deutlich (0,20–0,34). Also ein "
          "Assistent, der sich enthalten darf — kein Beweis, und nie Grundlage "
          "für einen Logbucheintrag allein."),
    ("p", "Das Modell ist Vosks eigenes Sprechermodell (vosk-model-spk-0.4, "
          "~14 MB), ein X-Vektor, trainiert auf 8-kHz-Telefonsprache: "
          "schmalbandig, verrauscht, kanalvariabel — und damit dem FM-Signal "
          "über ein Relais viel näher als ein Breitbandmodell. Ein Stimmabdruck "
          "ist ein biometrisches Merkmal: Die Profile bleiben in speakers.json "
          "auf dem Pi (gitignored, kein Upload), und mit der Karte verschwindet "
          "auch das Profil."),
    ("h2", "Logbuch (Wavelog + QRZ.com)"),
    ("p", "Protokolliert QSOs in eine lokal installierte Wavelog-Instanz. Es "
          "genügt, das Rufzeichen (und optional einen Namen) einzugeben — Frequenz, "
          "Band, Modus, Datum/Uhrzeit und das eigene Rufzeichen werden automatisch "
          "aus dem aktuellen Steuerband und dem Stationsprofil ergänzt. LOOKUP holt "
          "Name, Locator, QTH, Land und E-Mail von QRZ.com (XML-Daten-API) sowie "
          "'schon gearbeitet' / DXCC von Wavelog. Das Feld ADDRESS neben QTH "
          "nimmt Straße und Postleitzahl des Inhabers auf; wird direkt aus einer "
          "ASR-Karte geloggt, füllt es sich aus der BNetzA-Liste. Es geht als "
          "ADIF-Feld ADDRESS mit, während in QTH nur der Ort steht, damit die "
          "Auswertungen in Wavelog sauber bleiben. LOG QSO überträgt den Kontakt als "
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
          "Audio (Gerät, USB-Mixer, Sprachfilter, Testton, Audio-Timing), Rig-Info, "
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
    pdf.running_head = True
    pdf.chapter = ""          # the contents page needs no caption either
    pdf.add_page()
    # The placeholder renders onto the CURRENT page and then adds `pages` more,
    # so after this call we already stand on a fresh page — chapter 1 must not
    # open another one (see the h1 branch in render()).
    pdf.insert_toc_placeholder(lambda p, outline: toc(p, outline, lang), pages=1)
    render(pdf, blocks)
    pdf.output(path)
    print("wrote", path)


build(os.path.join(HERE, "Manual-EN.pdf"),
      "User & Technical Manual",
      "Kenwood TM-V71 web remote", "en", EN)
build(os.path.join(HERE, "Handbuch-DE.pdf"),
      "Benutzer- & Technikhandbuch",
      "Kenwood TM-V71 Web-Fernsteuerung", "de", DE)

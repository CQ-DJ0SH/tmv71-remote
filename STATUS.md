# Status — 13. Oktober 2026, abends

Arbeitsstand der Speicheruntersuchung und der heute daraus gefolgten Änderungen.
Alle Zahlen sind auf diesem Pi 5 gemessen, nicht geschätzt.

## Kurzfassung

Der gemeldete Speicheranstieg (AA7US, Pi 4B mit 1 GB, lief über Nacht voll) ist
gefunden und benannt. Er hat **nichts** mit diesem Projektcode zu tun: er sitzt
im **libopus-Wrapper von libavcodec**, den PyAV mitbringt, und tritt nur auf,
solange ein Zuhörer verbunden ist.

| Zustand | Anstieg |
|---|---|
| niemand verbunden, Soundkarte läuft durch | **flach** — 10 h lang 361 MB ±1 MB |
| ein verbundener Zuhörer (vorher) | **+45 MB/h** |
| ein verbundener Zuhörer (nach dem Mono-Patch) | **+13 MB/h** |
| rechnerisch nach dem geplanten ctypes-Encoder | **≈ +2 MB/h** |

## Wie es eingekreist wurde

**Nicht** die Rufzeichenerkennung: mit abgeschalteter ASR (Modell und 69.904er
Register blieben geladen, nur die Audiozufuhr gestoppt) 13 Messpunkte im
Minutenabstand, +8,3 MB in 12 min = **41,3 MB/h** — unverändert gegenüber der
Messung mit ASR. Das deckt sich mit Johns Beobachtung, dass das Entfernen von
Voice ID nichts brachte.

**Nicht** erreichbare Objekte: eine nackte aiortc-Schleife in einem leeren
Prozess (kein Funkgerät, keine Soundkarte, keine Erkennung) wächst um
**0,43 MB/min** und gewinnt dabei in fünf Minuten **zwei** Python-Objekte.

**Nicht** der Allokator — drei Varianten, identische Steigung:

```
glibc Standard                      +2,44 MB / 5 min
glibc TRIM=128k MMAP=64k            +2,22 MB / 5 min
jemalloc 5.3 (LD_PRELOAD)           +2,22 MB / 5 min
```

Damit ist es keine Fragmentierung, sondern Speicher, der nie freigegeben wird.

**Bisektion des Sendepfads** (gleiche Arbeit, 50 Blöcke/s, nur die Schicht
darunter getauscht):

```
AudioFrame.from_ndarray allein      +0,19 MB, dann flach
+ AudioResampler.resample           +0,22 MB/min
+ libopus encode                    +0,45 MB/min
+ SRTP protect                      +0,45 MB/min   (unverändert)
```

**Der Codec ist es, nicht PyAV:**

```
libopus über PyAV                   +0,23 MB/min
  + PyAV-Log auf PANIC              +0,23 MB/min
  + PyAV-Logmitschnitt aus          +0,23 MB/min
pcm_s16le über PyAV                 +0,00 MB/min   (4 min bitgenau flach)
libopus direkt über ctypes          +0,03 MB/min
```

Gleiche Rahmen, gleiche PyAV-Maschinerie, gleiche Paketobjekte. Nur der Codec
getauscht — Leck weg. Und libopus selbst ist sauber. Also: **der Fehler liegt in
`libavcodec`s libopus-Wrapper** (PyAV 16.1.0, gebündeltes libavcodec 62.11.100).

## Was nicht hilft

| Versuch | Ergebnis |
|---|---|
| Mono statt Stereo kodieren (bei 20 ms) | +0,46 MB/min — **keine** Änderung |
| Codec-Kontext jede Minute neu bauen | +0,49 MB/min — **keine** Änderung |
| 60-ms-Rahmen statt 20 ms | +0,31 MB/min, aber +40 ms Latenz |
| jemalloc / glibc-Feinstellung | siehe oben, nichts |
| `gc.collect()` + `malloc_trim(0)` | holt nichts zurück, solange der Strom läuft |

**Zum Kontext-Erneuern im Besonderen** (gemessen, weil es naheliegt): Der
Encoder-Neubau ist zeitlich sauber — 300 Rahmen rein, 300 Pakete raus, kein
pts-Sprung. Hörbar ist er trotzdem: der durchlaufende Decoder des Zuhörers
bekommt **105 ms Schaden mit einer Spitze von 19.559 auf einem ±8000-Signal**,
1 ms nach dem Umschalten. Ein Decoder-Neubau — das ist der **TX**-Pfad,
Browser-Mikro → Funkgerät — kostet dasselbe: 104 ms, Vollaussteuerung, also ein
Knack, der auf Sendung gehen würde. Da es ohnehin keinen Speicher spart, ist es
doppelt erledigt.

## Was eingebaut ist

- **`50dd20b`** 24-h-Graph in Einstellungen ▸ Hardware: Temperatur, Load,
  Speicher (Host und Dienst) als drei gestapelte Panels, eine Probe pro Minute,
  nach `sys_history.csv` durchgeschrieben, damit die Stunde vor einem Neustart
  erhalten bleibt. 1h/6h/24h, Fadenkreuz mit Tooltip.
- **`4d45a69`** Arena-Begrenzung (`mallopt(M_ARENA_MAX, 2)`): glibc hatte dem
  Prozess **30 Arenen à 64 MB** über 34 Threads gegeben, 3,0 GB Adressraum.
  Danach: 0–1 Arena, 0,67 GB virtuell, RSS 611 → 318 MB. Senkt das **Niveau**,
  nicht die Steigung.
- **Mono-Opus direkt gegen libopus** (`opus_mono.py`):
  aiortcs Encoder schreibt `layout="stereo"` fest und resampelt unser Mono
  dorthin, obwohl das SDP `sprop-stereo=0` sagt. Unsere Rahmen sind schon
  s16/48k/960 — der Mono-Codec nimmt sie direkt. Dazu wird `opus_encode`
  unmittelbar aufgerufen, nicht über libavcodec. An der vollen Strecke mit
  echtem Empfänger: **+0,42 → +0,02 MB/min**, nach `gc`+`trim` unter dem
  Startwert, 46,5 Rahmen/s, keine Dekodierfehler. Ohne `libopus.so` fällt es auf
  den PyAV-Encoder zurück, weiter mono, also die Hälfte.
- **`c916aca`**, **`c5d3ba0`** Netzschalter: Der Prozessausstieg gibt die
  GPIO-Leitung frei, der Pin fällt auf Eingang, das Funkgerät verliert die
  Versorgung — jeder Backend-Neustart ließ es dunkel zurück (das war die Ursache
  von „Radio offline" heute). Zustand wird jetzt gemerkt und beim Start
  wiederhergestellt, mit 3 s Abfallzeit, weil ein Sekunden-Ausfall das Gerät
  hängen lässt (bestromt, aber auf CAT taub).
- **`ff5e8e2`** Modellname wird beim Reconnect nachgelesen, nicht nur beim Start.

## Reparatur: erledigt

Der ctypes-Encoder ist gebaut, geprüft und läuft seit 21:54 im Dienst
(`Opus encoder: mono, no resampler, libopus direct`). Geprüft wurde: Zeitstempel
schreiten in 960er-Schritten ab 0, Nutzlasten 241–385 Byte, Rundlauf über den
aiortc-Decoder ergibt 700 Hz bei 1,6 % Pegelabweichung, Rahmen abweichender
Länge gehen weiter über den Resampler, der Encoder-Handle wird sauber
freigegeben, und die volle Strecke mit echtem Empfänger wächst um 0,02 MB/min.

Damit: **~1 MB/h pro Zuhörer statt 45.**

Minimaler Reproduzierer für die Meldung nach oben (PyAV/FFmpeg):

```python
import fractions, av, numpy as np
from av import CodecContext
SR, BLOCK = 48000, 960
c = CodecContext.create("libopus", "w")       # "pcm_s16le" -> flach
c.format, c.layout, c.sample_rate = "s16", "mono", SR
c.time_base = fractions.Fraction(1, SR); c.bit_rate = 96000
arr = np.random.randint(-3000, 3000, (1, BLOCK)).astype(np.int16)
for i in range(60000):                        # 20 min Material
    f = av.AudioFrame.from_ndarray(arr, format="s16", layout="mono")
    f.sample_rate = SR; f.pts = i * BLOCK; f.time_base = c.time_base
    for _ in c.encode(f): pass
# RSS wächst linear um ~0,23 MB/min; mit pcm_s16le gar nicht
```

## Nachtbeobachtung

- Nächtlicher Neustart (`00 4 * * * /usr/sbin/reboot`) bleibt **aus**.
- `/usr/local/bin/tmv71-memlog` alle 10 min → `/var/log/tmv71-mem.log`, jetzt
  mit `peers=`, `arenas=`, `threads=` (RSS ohne die Zahl der Zuhörer daneben ist
  nicht deutbar).
- `/usr/local/bin/tmv71-memreport [Stunden]` trennt die Steigung mit und ohne
  Zuhörer und erkennt Neustarts an der zurückgesprungenen Laufzeit.
- Um 07:00 legt ein Cron-Eintrag die Nachtauswertung in
  `/var/log/tmv71-mem-report.txt`.
- Die Kurve hat heute Abend mehrere Schnitte durch meine Neustarts
  (Arena-Begrenzung, Graph-Sammler, Mono-Encoder). Sauber ist sie ab **21:33**.

## Offen

1. Die Nachtkurve gegenprüfen: mit dem Fix sollte eine Mithörsitzung die
   Steigung nicht mehr tragen.
2. Fehlermeldung an PyAV/FFmpeg mit dem Reproduzierer oben.
3. Kein GitHub-Issue zum Speicherthema angelegt.

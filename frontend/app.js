/* TM-V71 Remote — frontend controller (no build step, vanilla ES) */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let last = null;          // last RadioStatus
let memBase = 0;          // quick-key channel offset: 0 normally, 50 in the air band
let airbandRx = false;    // Band A in the air band → receive-only, TX blocked
let txActive = false;
let linkDown = false;     // backend status WebSocket is down (shown to the user)
let pttTimerStart = 0, pttTimerIv = null;   // PTT up-timer (counts up while TX)
function fmtMMSS(s) {
  const m = Math.floor(s / 60), ss = s % 60;
  return String(m).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
}
function startPttTimer() {
  pttTimerStart = Date.now();
  const el = document.getElementById("ptt-timer"); if (el) el.textContent = "00:00";
  const tick = () => {
    const ms = Date.now() - pttTimerStart;
    const e = document.getElementById("ptt-timer");
    if (e) e.textContent = fmtMMSS(Math.floor(ms / 1000));
    // clock sweep: one revolution per minute (6° per second)
    const ptt = document.getElementById("ptt");
    if (ptt) ptt.style.setProperty("--sweep", (((ms / 1000) % 60) * 6).toFixed(1));
  };
  tick();
  pttTimerIv = setInterval(tick, 120);
}
function stopPttTimer() {
  if (pttTimerIv) { clearInterval(pttTimerIv); pttTimerIv = null; }
  pttTimerStart = 0;
  const el = document.getElementById("ptt-timer"); if (el) el.textContent = "00:00";
  const ptt = document.getElementById("ptt"); if (ptt) ptt.style.setProperty("--sweep", "0");
}
let selcallMuted = false; // RX muted by the selcall "MUTE until call" button
let micTestActive = false; // RX muted while MIC TEST is on (silence radio noise)
let micTestTimer = null;   // auto-stop the mic test after a cap (see bindAudio)
let pttLock = false;      // latched (continuous) transmit
let lockConfirmed = false; // radio has actually reported TX since locking
let setPttLock = null;    // assigned in bindControls()
let memCache = {};        // channel -> MemoryChannel, for the editor
let edState = { channel: null, isNew: false };

// ---- API backend ----------------------------------------------------------
// Which backend this browser talks to. Empty = same origin (the host that
// served this page). Stored per-browser so the static UI can point at a
// remote backend. Trailing slashes are stripped.
function apiBase() {
  return (localStorage.getItem("tmv71.apiBase") || "").replace(/\/+$/, "");
}
// Absolute URL for an API path, honouring the configured backend.
function apiUrl(path) {
  return apiBase() + path;
}
// WebSocket URL for a path, honouring the configured backend (http→ws, https→wss).
function wsUrl(path) {
  const b = apiBase();
  if (b) return b.replace(/^http/, "ws") + path;
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;
}

// ---- helpers --------------------------------------------------------------
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(apiUrl(path), opt);
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch {}
    throw new Error(detail || `An error occurred: ${r.status}`);
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}

let toastTimer;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 2600);
}

// Themed confirmation dialog — drop-in async replacement for window.confirm().
// Returns a Promise<boolean>. Falls back to the native confirm if the markup
// isn't present. opts: {title, okText, cancelText, danger}.
function confirmDialog(message, opts = {}) {
  const m = $("#confirm-dialog");
  if (!m) return Promise.resolve(window.confirm(message));
  $("#confirm-title").textContent = opts.title || "Confirm";
  $("#confirm-msg").textContent = message;
  const okBtn = $("#confirm-ok"), cancelBtn = $("#confirm-cancel");
  okBtn.textContent = opts.okText || "OK";
  cancelBtn.textContent = opts.cancelText || "CANCEL";
  okBtn.className = opts.danger ? "btn-danger" : "btn-primary";
  return new Promise(resolve => {
    const close = val => {
      m.classList.remove("open");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      m.removeEventListener("mousedown", onBackdrop);
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onOk = () => close(true);
    const onCancel = () => close(false);
    const onBackdrop = e => { if (e.target === m) close(false); };
    const onKey = e => {
      if (e.key === "Escape") { e.preventDefault(); close(false); }
      else if (e.key === "Enter") { e.preventDefault(); close(true); }
    };
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    m.addEventListener("mousedown", onBackdrop);
    document.addEventListener("keydown", onKey);
    m.classList.add("open");
    okBtn.focus();
  });
}

const fmtMHz = (hz) => (hz / 1e6).toFixed(4);
const SHIFT = { 0: "SIMPLEX", 1: "SHIFT +", 2: "SHIFT −" };
const FMMODE = { 0: "FM", 1: "NFM", 2: "AM" };

// Mirror of the radio's lookup tables (see backend/app/tmv71.py).
const CTCSS_TONES = [67.0,69.3,71.9,74.4,77.0,79.7,82.5,85.4,88.5,91.5,94.8,97.4,100.0,103.5,107.2,110.9,114.8,118.8,123.0,127.3,131.8,136.5,141.3,146.2,151.4,156.7,162.2,167.9,173.8,179.9,186.2,192.8,203.5,206.5,210.7,218.1,225.7,229.1,233.6,241.8,250.3,254.1];
const DCS_CODES = [23,25,26,31,32,36,43,47,51,53,54,65,71,72,73,74,114,115,116,122,125,131,132,134,143,145,152,155,156,162,165,172,174,205,212,223,225,226,243,244,245,246,251,252,255,261,263,265,266,271,274,306,311,315,325,331,332,343,346,351,356,364,365,371,411,412,413,423,431,432,445,446,452,454,455,462,464,465,466,503,506,516,523,526,532,546,565,606,612,624,627,631,632,654,662,664,703,712,723,731,732,734,743,754];
const STEP_HZ = [5000,6250,28330,10000,12500,15000,20000,25000,30000,50000,100000];

const toneHz = (i) => CTCSS_TONES[i - 1];           // radio tone index is 1-based
const dcsCode = (i) => DCS_CODES[i];                // dcs index is 0-based

function shiftLabel(m) {
  if (m.shift === 1) return "+" + (m.offset / 1000) + "k";
  if (m.shift === 2) return "−" + (m.offset / 1000) + "k";
  return "—";
}
function toneLabel(m) {
  if (m.ctcss_on) return "C " + (toneHz(m.ctcss_idx)?.toFixed(1) ?? "?");
  if (m.tone_on) return "T " + (toneHz(m.tone_idx)?.toFixed(1) ?? "?");
  if (m.dcs_on) return "D" + String(dcsCode(m.dcs_idx) ?? "?").padStart(3, "0");
  return "—";
}

// ---- rendering ------------------------------------------------------------
function setSqlUi(band, val) {
  const v = Number(val);
  const lbl = document.getElementById(`sql-val-${band}`);
  if (lbl) lbl.textContent = v;
  const sl = document.getElementById(`sql-${band}`);
  if (sl) sl.style.setProperty("--sqlpct", Math.round(v / 31 * 100) + "%");
}

function renderBand(b) {
  const i = b.band;
  $(`#freq-${i}`).textContent = fmtMHz(b.rx_freq);

  // mirror the live frequency into the tuner digits unless the user is editing
  if (!dirty[i]) { editHz[i] = b.rx_freq; setSpinner(i, b.rx_freq); }

  $(`#step-${i}`).textContent = b.step_hz ? `${(b.step_hz / 1000).toString()} kHz` : "";
  $(`#fmode-${i}`).textContent = FMMODE[b.fm_mode] || "FM";   // AM auto in air band

  // air-band switch (Band A only) lit when A's frequency is in the air band
  if (i === 0) {
    const air = $("#air-0");
    if (air) air.classList.toggle("active", b.rx_freq >= 118e6 && b.rx_freq <= 137e6);
  }

  const mn = $(`#memname-${i}`);
  mn.textContent = (b.mode === 1 && b.memory_name)
    ? `M${String(b.memory_channel ?? 0).padStart(3, "0")} ${b.memory_name}` : "";

  const panel = $(`#band-${i}`);
  panel.classList.toggle("is-busy", !!b.squelch_open);

  // mode toggle active state
  $$(`.mode-toggle[data-band="${i}"] .mt`).forEach(btn =>
    btn.classList.toggle("active", Number(btn.dataset.mode) === b.mode));

  // transmit-power switch active state
  $$(`#band-${i} .pwr-seg .sg`).forEach(x =>
    x.classList.toggle("active", b.power != null && Number(x.dataset.pwr) === b.power));

  // squelch slider (skip while the user is dragging it)
  const sqlEl = document.getElementById(`sql-${i}`);
  if (sqlEl && document.activeElement !== sqlEl && b.squelch_level != null) {
    sqlEl.value = b.squelch_level;
    setSqlUi(i, b.squelch_level);
  }

  updateVfoParams(b);
}

function render(st) {
  last = st;
  $("#stat-radio").classList.toggle("online", st.connected);
  $("#stat-radio").classList.toggle("fault", !st.connected && !!st.error);
  document.body.classList.toggle("disconnected", !st.connected);

  (st.bands || []).forEach(renderBand);
  updateMemBase(st);
  // sweep in follow mode: re-centre the 20 MHz window when the radio drifts
  if (hrf.ws && hrf.mode === "sweep" && hrf.follow) {
    const cf = hrfControlFreq();
    if (cf && Math.abs(cf - hrf.sweepCenter) > 5e6) hrfReconfig();
  }

  // control band highlight + per-band TX lamp + single-band dimming
  $$(".band").forEach(p => {
    const band = Number(p.dataset.band);
    p.classList.toggle("is-ctrl", band === st.control_band);
    p.classList.toggle("is-ptt", band === st.ptt_band);
    p.classList.toggle("is-tx", !!st.transmitting && st.ptt_band === band);
    p.classList.toggle("is-off", !!st.single_band && band !== st.control_band);
  });
  // audio-band (data band) switch reflects the radio (data band 2/3 -> RX band)
  if (st.data_band != null) {
    const rxBand = (st.data_band === 1 || st.data_band === 2) ? 1 : 0;
    $$("#audio-band-seg .sg").forEach(b => b.classList.toggle("active", Number(b.dataset.band) === rxBand));
    // detected-callsign field follows the active RX band's colour
    const rc = $("#rx-call");
    if (rc) { rc.classList.toggle("band-a", rxBand === 0); rc.classList.toggle("band-b", rxBand === 1); }
  }
  const pttLabel = $("#ptt-band");
  pttLabel.textContent = `PTT → BAND ${st.ptt_band === 1 ? "B" : "A"}`;
  pttLabel.classList.toggle("is-b", st.ptt_band === 1);   // color = selected band
  document.body.classList.toggle("ptt-b", st.ptt_band === 1);
  document.body.classList.toggle("ctrl-b", st.control_band === 1);   // memory-key colour
  highlightActiveMem(st);

  // 1750 Hz tone hold (menu 402) — reflect the radio state on the side button
  const t1750 = $("#btn-1750");
  if (t1750 && st.tone_1750 != null) {
    t1750.classList.toggle("on", !!st.tone_1750);
    t1750.setAttribute("aria-pressed", String(!!st.tone_1750));
  }

  // transmit state
  txActive = !!st.transmitting;
  document.body.classList.toggle("transmitting", txActive);
  $("#ptt").classList.toggle("tx", txActive);
  // PTT up-timer: count up from 00:00 while transmitting (any source)
  if (txActive && !pttTimerIv) startPttTimer();
  else if (!txActive && pttTimerIv) stopPttTimer();
  // mute browser RX output whenever transmitting (covers PTT-lock, spacebar,
  // and radio-side TX, not just the key() button handler)
  // RX is NOT muted during TX in the browser: a half-duplex rig isn't receiving
  // while it transmits, and toggling the <audio> element upsets Bluetooth
  // routing. The mic is gated in the backend instead (only sent to the radio
  // while keyed). Selcall/mic-test muting stays.
  const rxEl = $("#rx-audio"); if (rxEl) rxEl.muted = selcallMuted || micTestActive;
  // keep the PTT-Lock honest: clear it only after TX was confirmed then dropped
  // on the radio side (e.g. time-out timer), never during the engage round-trip.
  if (pttLock && setPttLock) {
    if (txActive) lockConfirmed = true;
    else if (lockConfirmed) setPttLock(false);
  }

  updateSmeters();
}

// ---- audio level floor (shared by the S-meter and the topbar RX strip) ----
const VU_FLOOR = -54;     // dBFS at the bottom of a level bar

// ---- S-meter — RX: receive-audio level / TX: mic (modulation) level -------
const SM_SEGMENTS = 30;
const SM_PEAK_HOLD = 1000;                        // hold the peak marker (ms)
let lastRxDb = null, lastTxDb = null;             // Pi-measured RX / mic levels
const smPeak = [{ seg: 0, t: 0 }, { seg: 0, t: 0 }];  // per-band peak-hold state

function buildSmeter(band) {
  const el = document.getElementById(`sm-bar-${band}`);
  if (!el || el.childElementCount) return;
  for (let i = 0; i < SM_SEGMENTS; i++) {
    const s = document.createElement("span");
    s.className = "sm-seg";
    el.appendChild(s);
  }
}
function renderSmeter(band, frac, tx) {
  const bar = document.getElementById(`sm-bar-${band}`);
  const wrap = document.getElementById(`sm-${band}`);
  if (!bar) return;
  if (wrap) wrap.classList.toggle("tx", !!tx);
  const lit = Math.round(Math.max(0, Math.min(1, frac)) * SM_SEGMENTS);
  // peak hold: keep the highest segment lit for ~1 s, then let it fall a step
  // per update. The marker sits at the held peak even above the current level.
  const pk = smPeak[band], now = Date.now();
  if (lit >= pk.seg) { pk.seg = lit; pk.t = now; }
  else if (now - pk.t > SM_PEAK_HOLD) pk.seg = Math.max(lit, pk.seg - 1);
  const peakIdx = pk.seg - 1;
  const segs = bar.children;
  for (let i = 0; i < segs.length; i++) {
    let cls = "sm-seg" + (i < lit ? (tx ? " on-tx" : " on") : "");
    if (i === peakIdx) cls += tx ? " peak-tx" : " peak";
    segs[i].className = cls;
  }
}
function updateSmeters() {
  if (!last || !last.bands) return;
  last.bands.forEach(b => {
    if (last.transmitting && last.ptt_band === b.band) {
      // TX: show the live mic (modulation) level
      const f = (lastTxDb != null) ? (lastTxDb - VU_FLOOR) / (0 - VU_FLOOR) : 0;
      renderSmeter(b.band, f, true);
    } else {
      // RX: Pi audio comes from the data band's RX side only, so show the
      // measured AF level on that band; the other band stays empty. Driven by
      // rx_db directly (not squelch_open, which reads false on an open squelch
      // even while audio/noise is passing), so it tracks what you actually hear.
      const onAudio = b.band === (last.audio_band ?? last.control_band);
      const f = (onAudio && lastRxDb != null)
        ? Math.max(0, (lastRxDb - VU_FLOOR) / (0 - VU_FLOOR)) : 0;
      renderSmeter(b.band, f, false);
    }
  });
}

// mini PTT VU bars with 1 s peak-hold (lvl is 0..1; bar fills from the bottom)
const VU_HOLD = 1000;
const vuPeak = { rx: { v: 0, t: 0 }, tx: { v: 0, t: 0 } };
function vuUpdate(key, lvl, maskId, pkId) {
  const mask = $(maskId);
  if (!mask) return;
  const p = vuPeak[key], now = Date.now();
  if (lvl >= p.v) { p.v = lvl; p.t = now; }
  else if (now - p.t > VU_HOLD) p.v = Math.max(lvl, p.v - 0.05);   // decay after hold
  mask.style.height = (100 - lvl * 100) + "%";
  const pk = $(pkId);
  if (pk) pk.style.bottom = Math.max(0, Math.min(1, p.v)) * 100 + "%";
}

async function refreshAudio() {
  try {
    const a = await api("GET", "/api/audio/status");
    // audio panel LED + text
    const led = $("#audio-led"), txt = $("#audio-text");
    if (led) {
      const ok = a.enabled && a.connected;
      led.classList.toggle("on", ok);
      led.classList.toggle("off", a.enabled && !a.connected);
      txt.textContent = !a.enabled ? "disabled"
        : !a.connected ? (a.error || "not ready")
        : "USB Audio";
    }
    // feed the in-display S-meter with the live RX / mic audio levels
    lastRxDb = a.rx_db; lastTxDb = a.tx_db;
    updateSmeters();
    // mini VU bars flanking the PTT button (PWA): RX left, TX right, with 1 s
    // peak-hold. The mask covers the empty (top) part → fills upward from bottom.
    vuUpdate("rx", lvlNorm(a.rx_db), "#ptt-vu-rx-mask", "#ptt-vu-rx-pk");
    vuUpdate("tx", lvlNorm(a.tx_db), "#ptt-vu-tx-mask", "#ptt-vu-tx-pk");
    // ROGER toggle button in the PTT panel reflects the roger-beep state
    const rb = $("#btn-roger");
    if (rb) { rb.classList.toggle("on", !!a.roger_beep); rb.setAttribute("aria-pressed", String(!!a.roger_beep)); }
    // two-tone test active -> warning triangle in the PTT panel + reflect switch
    const warn = $("#ptt-warn"); if (warn) warn.hidden = !a.test_tone;
    const tt = $("#set-testtone"); if (tt && document.activeElement !== tt) tt.checked = !!a.test_tone;
    const mt = $("#audio-mictest"); if (mt && document.activeElement !== mt) mt.checked = !!a.mic_test;
    const rec = $("#mt-record"); if (rec) rec.hidden = !a.mic_test;   // recording indicator
    const pb = $("#mt-playback"); if (pb) pb.hidden = !a.echo_busy;   // replay indicator
    // scroll the level graph only while the browser audio is connected
    if (audioConnected()) pushLevel(a.rx_db, a.tx_db);
    else if (levelHist.length) { levelHist.length = 0; drawLevelGraph(); }
    updateAudioBytes();     // WebRTC RX/TX data rate readout (throttled inside)
    // mirror gain sliders unless the user is dragging them
    ["rx", "tx"].forEach(k => {
      const sl = document.getElementById(k + "-gain"), g = a[k + "_gain"];
      // while AUTO is on, show the live AGC factor in the (disabled) TX slider
      const v = (k === "tx" && a.tx_auto_gain && a.agc_gain != null) ? a.agc_gain : g;
      if (sl && v != null && document.activeElement !== sl) { sl.value = v; setGainUi(k, v); }
    });
    const ta = $("#tx-auto");
    if (ta && document.activeElement !== ta) {
      // don't let the shared backend override this app's saved toggle position;
      // reflect it only until the app has its own remembered preference
      if (localStorage.getItem(txAgcKey()) === null) ta.checked = !!a.tx_auto_gain;
      setTxAutoUi(!!a.tx_auto_gain);
    }
  } catch {}
}

// ---- scrolling RX/MIC level graph (newest on the right) -------------------
const GRAPH_MAX = 150;            // ~30 s of history at 200 ms/sample
const levelHist = [];             // {rx,tx} normalized to 0..1
const SAMPLE_MS = 200;            // status-poll cadence (see refreshAudio interval)
let lastSampleTs = 0;             // when the newest sample arrived (for smooth scroll)
let levelRAF = null, lastLvlDraw = 0, graphOnScreen = true;
const lvlNorm = db =>
  db == null ? 0 : Math.max(0, Math.min(1, (db - VU_FLOOR) / (0 - VU_FLOOR)));

function sizeLevelGraph() {
  const cv = $("#level-graph"); if (!cv) return;
  const r = cv.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.round(r.width * dpr));
  cv.height = Math.max(1, Math.round(r.height * dpr));
  drawLevelGraph();
}

function drawSeries(ctx, vals, xAt, H, stroke, fill, lw) {
  if (vals.length < 2) return;
  ctx.beginPath();
  vals.forEach((v, i) => { const x = xAt(i), y = H - v * H; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.lineTo(xAt(vals.length - 1), H); ctx.lineTo(xAt(0), H); ctx.closePath();
  ctx.fillStyle = fill; ctx.fill();
  ctx.beginPath();
  vals.forEach((v, i) => { const x = xAt(i), y = H - v * H; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = stroke; ctx.lineWidth = lw; ctx.lineJoin = "round"; ctx.stroke();
}

function drawLevelGraph(offset = 0) {
  const cv = $("#level-graph"); if (!cv) return;
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  const dpr = window.devicePixelRatio || 1;
  ctx.clearRect(0, 0, W, H);
  // dBFS scale: horizontal gridlines + labels (0 dB top … −54 floor bottom)
  ctx.font = `${Math.round(9 * dpr)}px ui-monospace, monospace`;
  ctx.textBaseline = "middle";
  [0, -20, -40].forEach(db => {
    const v = (db - VU_FLOOR) / (0 - VU_FLOOR);
    const y = Math.round(H - v * H) + 0.5;
    ctx.strokeStyle = "rgba(127,140,150,0.16)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.fillStyle = "rgba(127,140,150,0.75)";
    ctx.fillText(`${db}`, 4 * dpr, y + (db === 0 ? 7 * dpr : 0));
  });
  ctx.fillStyle = "rgba(127,140,150,0.5)";
  ctx.fillText("dBFS", 4 * dpr, H - 8 * dpr);
  // time grid: vertical divisions every 5 s across the ~30 s window
  // (newest sample at the right edge). Stronger line every 10 s.
  for (let t = 5; t < 30; t += 5) {
    const x = Math.round(W * (30 - t) / 30) + 0.5;
    const major = (t % 10 === 0);
    ctx.strokeStyle = major ? "rgba(127,140,150,0.22)" : "rgba(127,140,150,0.10)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }
  const n = levelHist.length; if (!n) return;
  const dx = W / (GRAPH_MAX - 1);
  // smooth scroll: shift everything left by up to one sample-width between polls
  // (offset 0→1), so motion is continuous instead of stepping 5×/s
  const shift = Math.max(0, Math.min(1, offset)) * dx;
  const xAt = i => W - (n - 1 - i) * dx - shift; // newest sample at the right edge
  const lw = Math.max(1.5, (window.devicePixelRatio || 1) * 1.3);
  drawSeries(ctx, levelHist.map(s => s.rx), xAt, H, "rgba(139,122,214,0.9)",  "rgba(139,122,214,0.16)", lw);   // RX = audio-panel violet
  drawSeries(ctx, levelHist.map(s => s.tx), xAt, H, "rgba(207,97,89,0.95)", "rgba(207,97,89,0.14)",  lw);   // muted MIC red
}

// WebRTC audio data-rate readout (bytes/s) shown in the graph corner. Computed
// from getStats() byte deltas; throttled to ~1 s so the rate is stable.
let _rtcPrev = null;      // {rx, tx, t}
function fmtRate(bps) {
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " MB/s";
  if (bps >= 1e3) return (bps / 1e3).toFixed(1) + " kB/s";
  return Math.round(bps) + " B/s";
}
async function updateAudioBytes() {
  const el = $("#graph-bytes"); if (!el) return;
  if (!audioPc || !audioConnected()) { el.textContent = ""; _rtcPrev = null; return; }
  const now = performance.now();
  if (_rtcPrev && now - _rtcPrev.t < 900) return;      // throttle to ~1 s
  let stats; try { stats = await audioPc.getStats(); } catch { return; }
  let rx = 0, tx = 0;
  stats.forEach(r => {
    if (r.type === "inbound-rtp" && r.kind === "audio") rx += r.bytesReceived || 0;
    if (r.type === "outbound-rtp" && r.kind === "audio") tx += r.bytesSent || 0;
  });
  if (_rtcPrev) {
    const dt = (now - _rtcPrev.t) / 1000;
    if (dt > 0) el.innerHTML =
      '<span class="gb-rx">↓ ' + fmtRate((rx - _rtcPrev.rx) / dt) + '</span> ' +
      '<span class="gb-tx">↑ ' + fmtRate((tx - _rtcPrev.tx) / dt) + '</span>';
  }
  _rtcPrev = { rx, tx, t: now };
}
function pushLevel(rxDb, txDb) {
  levelHist.push({ rx: lvlNorm(rxDb), tx: lvlNorm(txDb) });
  if (levelHist.length > GRAPH_MAX) levelHist.shift();
  lastSampleTs = performance.now();
  startLevelLoop();
}
// Smooth-scroll render loop — decoupled from the 200 ms data poll. Runs at ~30fps
// only while the graph is on-screen, audio is connected and the tab is visible;
// otherwise it stops so it costs nothing when you're not watching it.
function startLevelLoop() { if (levelRAF == null) levelRAF = requestAnimationFrame(levelTick); }
function levelTick(now) {
  if (!audioConnected() || document.hidden || !graphOnScreen) {
    levelRAF = null; drawLevelGraph(0); return;      // settle at the final position
  }
  if (now - lastLvlDraw >= 33) {                     // ~30 fps
    drawLevelGraph((now - lastSampleTs) / SAMPLE_MS);
    lastLvlDraw = now;
  }
  levelRAF = requestAnimationFrame(levelTick);
}

// ---- websocket ------------------------------------------------------------
function connectWS() {
  const ws = new WebSocket(wsUrl("/ws"));
  ws.onopen = () => {
    document.body.classList.remove("disconnected");
    if (linkDown) { linkDown = false; toast("Connection restored", "ok"); }
  };
  ws.onmessage = (e) => { try { render(JSON.parse(e.data)); } catch {} };
  ws.onclose = () => {
    document.body.classList.add("disconnected");
    // Surface the lost backend link to the user (once per outage), regardless of
    // whether transmit is keyed.
    if (!linkDown) { linkDown = true; toast("Connection lost — reconnecting…", "err"); }
    // Safety: a latched (continuous) PTT must not stay keyed when the link to
    // the backend drops. Release it locally and best-effort tell the radio; the
    // backend PTT watchdog covers the case where the radio is unreachable too.
    if (pttLock && setPttLock) { setPttLock(false); toast("Link lost — PTT released", "err"); }
    setTimeout(connectWS, 1500);
  };
  ws.onerror = () => ws.close();
  // keepalive
  ws._ka = setInterval(() => ws.readyState === 1 && ws.send("ping"), 15000);
  ws.addEventListener("close", () => clearInterval(ws._ka));
}

// ---- controls -------------------------------------------------------------
async function setFreqHz(band, hz) {
  if (!hz || hz < 1e6) return toast("Invalid frequency", "err");
  try {
    await api("POST", "/api/frequency", { band, freq_hz: hz });
    toast(`Band ${band ? "B" : "A"} → ${(hz / 1e6).toFixed(4)} MHz`, "ok");
  } catch (e) { toast("Frequenz: " + e.message, "err"); }
}

// ---- per-digit frequency tuner --------------------------------------------
// Hz value of each of the 7 digits: 100/10/1 MHz . 100/10/1 kHz / 100 Hz
const PLACES = [100000000, 10000000, 1000000, 100000, 10000, 1000, 100];
const editHz = [null, null];      // pending frequency per band
const dirty = [false, false];     // user has changed it but not applied yet

function buildSpinner(band) {
  const el = document.getElementById(`spin-${band}`);
  if (!el || el.childElementCount) return;
  PLACES.forEach((p, idx) => {
    if (idx === 3) {              // decimal point before the fractional digits
      const dot = document.createElement("span");
      dot.className = "fdot"; dot.textContent = "."; el.appendChild(dot);
    }
    // Each digit is a column: clickable bar above (+1), the digit, bar below (-1).
    const cell = document.createElement("div");
    cell.className = "dcell";

    const up = document.createElement("span");
    up.className = "dbar dbar-up"; up.dataset.idx = idx;
    up.setAttribute("aria-label", "increment digit");   // no title → no hover tooltip
    up.addEventListener("click", () => bump(band, idx, +1));
    cell.appendChild(up);

    const dig = document.createElement("input");
    dig.className = "dig"; dig.type = "text"; dig.inputMode = "numeric";
    dig.maxLength = 1; dig.dataset.idx = idx;
    dig.addEventListener("beforeinput", e => {
      if (e.data && /\D/.test(e.data)) e.preventDefault();   // block non-digits
    });
    dig.addEventListener("input", () => {
      dig.value = dig.value.replace(/\D/g, "").slice(-1);    // digits only, single char
      onDigitType(band);
      if (dig.value.length >= 1) {       // auto-advance to the next digit cell
        const next = el.querySelectorAll(".dig")[idx + 1];
        if (next) { next.focus(); next.select(); }
      }
    });
    dig.addEventListener("keydown", e => {
      if (e.key === "Enter") applySpinner(band);
      else if (e.key === "ArrowUp") { e.preventDefault(); bump(band, idx, +1); }
      else if (e.key === "ArrowDown") { e.preventDefault(); bump(band, idx, -1); }
      else if (e.key === "Backspace" && !dig.value) {   // step back when empty
        const prev = el.querySelectorAll(".dig")[idx - 1];
        if (prev) { prev.focus(); prev.select(); }
      }
    });
    cell.appendChild(dig);

    const dn = document.createElement("span");
    dn.className = "dbar dbar-dn"; dn.dataset.idx = idx;
    dn.setAttribute("aria-label", "decrement digit");   // no title → no hover tooltip
    dn.addEventListener("click", () => bump(band, idx, -1));
    cell.appendChild(dn);

    el.appendChild(cell);
  });
  const unit = document.createElement("span");
  unit.className = "spin-unit"; unit.textContent = "MHz"; el.appendChild(unit);
  // small apply button: commit the entered frequency to the radio
  const apply = document.createElement("button");
  apply.type = "button"; apply.className = "spin-apply"; apply.textContent = "✓";
  apply.title = "Apply frequency"; apply.setAttribute("aria-label", "Apply frequency");
  apply.addEventListener("click", () => applySpinner(band));
  el.appendChild(apply);
}

function digitsOf(hz) {
  const mhz = Math.floor(hz / 1e6), frac = Math.round((hz % 1e6) / 100);
  return (String(mhz).padStart(3, "0") + String(frac).padStart(4, "0"))
    .split("").map(Number);
}
function setSpinner(band, hz) {
  const el = document.getElementById(`spin-${band}`); if (!el) return;
  const d = digitsOf(hz);
  el.querySelectorAll(".dig").forEach((inp, i) => {
    if (document.activeElement !== inp) inp.value = d[i];
  });
}
function readSpinner(band) {
  const el = document.getElementById(`spin-${band}`);
  const s = [...el.querySelectorAll(".dig")]
    .map(i => (i.value.replace(/\D/g, "") || "0").slice(-1)).join("");
  return parseInt(s.slice(0, 3), 10) * 1e6 + parseInt(s.slice(3), 10) * 100;
}
function markDirty(band, on) {
  dirty[band] = on;
  document.getElementById(`spin-${band}`)?.classList.toggle("pending", on);
}
function bump(band, idx, dir) {
  if (editHz[band] == null) editHz[band] = last?.bands?.[band]?.rx_freq || 0;
  editHz[band] = Math.max(0, Math.min(999_999_900, editHz[band] + dir * PLACES[idx]));
  markDirty(band, true); setSpinner(band, editHz[band]);
}
function onDigitType(band) { editHz[band] = readSpinner(band); markDirty(band, true); }
async function applySpinner(band) {
  const hz = editHz[band] ?? readSpinner(band);
  await setFreqHz(band, hz);
  markDirty(band, false);
}

function bindControls() {
  // mic UP / DW keys — step the displayed band one step (Enter applies a freq)
  $$(".step-btn").forEach(btn => btn.addEventListener("click", async () => {
    const band = Number(btn.dataset.band), direction = btn.dataset.dir;
    try { await api("POST", "/api/step", { band, direction }); }
    catch (e) { toast("Step: " + e.message, "err"); }
  }));

  // mode toggle
  $$(".mode-toggle .mt").forEach(btn => btn.addEventListener("click", async () => {
    const band = Number(btn.closest(".mode-toggle").dataset.band);
    try { await api("POST", "/api/band-mode", { band, mode: Number(btn.dataset.mode) }); }
    catch (e) { toast("Mode: " + e.message, "err"); }
  }));

  // control band
  $$(".ctrl-band-btn").forEach(btn => btn.addEventListener("click", async () => {
    try { await api("POST", "/api/control-band", { control_band: Number(btn.dataset.band) }); }
    catch (e) { toast("CTRL: " + e.message, "err"); }
  }));

  // PTT (transmit) band — BC <control>,<ptt>
  $$(".ptt-band-btn").forEach(btn => btn.addEventListener("click", async () => {
    try { await api("POST", "/api/ptt-band", { ptt_band: Number(btn.dataset.band) }); }
    catch (e) { toast("PTT: " + e.message, "err"); }
  }));

  // air-band toggle (Band A): on → air band (memory 997), off → back to 2 m
  const airBtn = $("#air-0");
  if (airBtn) airBtn.addEventListener("click", async () => {
    try {
      const st = await api("POST", "/api/airband");
      render(st);
      const b0 = st.bands && st.bands[0];
      const inAir = b0 && b0.rx_freq >= 118e6 && b0.rx_freq <= 137e6;
      toast(inAir ? "Band A → air band (M997)" : "Band A → 2 m", "ok");
    } catch (e) { toast("AIR: " + e.message, "err"); }
  });

  // band power / single-band (DL): solo this band, or back to dual
  $$(".band-power").forEach(btn => btn.addEventListener("click", async () => {
    const band = Number(btn.dataset.band);
    const single = !!last?.single_band, ctrl = last?.control_band;
    // active band in single mode -> back to dual; otherwise solo this band
    const body = (single && band === ctrl) ? { single: false } : { single: true, band };
    try { await api("POST", "/api/band-display", body); }
    catch (e) { toast("Band: " + e.message, "err"); }
  }));

  // transmit power (50W / 10W / 5W)
  $$(".pwr-seg .sg").forEach(btn => btn.addEventListener("click", async () => {
    const band = Number(btn.closest(".band").dataset.band);
    try { await api("POST", "/api/power", { band, level: Number(btn.dataset.pwr) }); }
    catch (e) { toast("Leistung: " + e.message, "err"); }
  }));

  // squelch slider — live label while dragging, send on release
  $$(".sql-slider").forEach(sl => {
    const band = Number(sl.closest(".sql-row").dataset.band);
    sl.addEventListener("input", () => setSqlUi(band, sl.value));
    sl.addEventListener("change", async () => {
      try { await api("POST", "/api/squelch", { band, level: Number(sl.value) }); }
      catch (e) { toast("Squelch: " + e.message, "err"); }
    });
  });

  // PTT (pointer + spacebar), hold-to-talk; PTT-Lock latches continuous TX
  const ptt = $("#ptt");
  const lockBtn = $("#ptt-lock");
  let keying = false;
  const key = async (on) => {
    if (on && airbandRx) { toast("Air band is receive-only — TX disabled", "err"); return; }
    if (on && !audioConnected()) { toast("Connect audio first to transmit", "err"); return; }
    if (keying === on) return; keying = on;
    // (TX audio is wired to the radio's front mic input, so it modulates the PTT
    // band regardless of the data-band setting — no audio-band/PTT-band check.)
    // RX is not muted for TX (see render()); keep only selcall/mic-test muting
    const rx = $("#rx-audio"); if (rx) rx.muted = selcallMuted || micTestActive;
    try { await api("POST", "/api/ptt", { transmit: on }); }
    catch (e) { toast("PTT: " + e.message, "err"); }

    // 1750 Hz tone call is a one-shot: auto-disable the hold after releasing PTT
    if (!on) {
      const t1750 = $("#btn-1750");
      if (t1750 && t1750.classList.contains("on")) {
        t1750.classList.remove("on");
        t1750.setAttribute("aria-pressed", "false");
        try { const st = await api("POST", "/api/tone-1750", { on: false }); render(st); }
        catch (e) { toast("1750 Hz: " + e.message, "err"); }
      }
    }
  };
  // expose to updatePanels so a radio-side TX drop clears the latch
  setPttLock = (on) => {
    if (on && !audioConnected()) { toast("Connect audio first to transmit", "err"); return; }
    pttLock = on;
    lockConfirmed = false;
    lockBtn.classList.toggle("locked", on);
    lockBtn.setAttribute("aria-pressed", String(on));
    lockBtn.textContent = on ? "PTT-LOCK · AN" : "PTT-LOCK";
    key(on);
  };
  lockBtn.addEventListener("click", () => setPttLock(!pttLock));
  ptt.addEventListener("pointerdown", e => { e.preventDefault(); ptt.setPointerCapture(e.pointerId); key(true); });
  ptt.addEventListener("pointerup", () => { if (!pttLock) key(false); });
  ptt.addEventListener("pointercancel", () => { if (!pttLock) key(false); });
  ptt.addEventListener("lostpointercapture", () => { if (!pttLock) key(false); });
  window.addEventListener("keydown", e => {
    if (e.code === "Space" && !e.repeat && document.activeElement.tagName !== "INPUT") { e.preventDefault(); key(true); }
  });
  window.addEventListener("keyup", e => {
    if (e.code !== "Space" || document.activeElement.tagName === "INPUT") return;
    e.preventDefault();      // stop Space from "clicking" a focused button (e.g. VFO/MEMORY)
    if (!pttLock) key(false);
  });

  // ROGER — toggle the roger beep (mic beep on PTT release)
  const rogerBtn = $("#btn-roger");
  rogerBtn?.addEventListener("click", async () => {
    const on = !rogerBtn.classList.contains("on");
    rogerBtn.classList.toggle("on", on);                 // optimistic
    rogerBtn.setAttribute("aria-pressed", String(on));
    try { await api("POST", "/api/audio/tones", { roger_beep: on }); }
    catch (e) { rogerBtn.classList.toggle("on", !on); toast("Roger beep: " + e.message, "err"); }
  });
  // 1750 — toggle the 1750 Hz tone hold (radio menu 402)
  const tone1750Btn = $("#btn-1750");
  tone1750Btn?.addEventListener("click", async () => {
    const on = !tone1750Btn.classList.contains("on");
    tone1750Btn.classList.toggle("on", on);              // optimistic; render() confirms
    tone1750Btn.setAttribute("aria-pressed", String(on));
    try { const st = await api("POST", "/api/tone-1750", { on }); render(st); }
    catch (e) { tone1750Btn.classList.toggle("on", !on); toast("1750 Hz: " + e.message, "err"); }
  });
}

// ---- memory ---------------------------------------------------------------
async function loadMemories() {
  const start = Number($("#mem-start").value || 0);
  const end = Number($("#mem-end").value || 49);
  const body = $("#mem-body");
  body.innerHTML = `<tr><td colspan="8" class="empty">Loading channels ${start}–${end} …</td></tr>`;
  try {
    const rows = await api("GET", `/api/memories?start=${start}&end=${end}`);
    if (!rows.length) { body.innerHTML = `<tr><td colspan="8" class="empty">No occupied channels in ${start}–${end}.</td></tr>`; return; }
    body.innerHTML = "";
    memCache = {};
    for (const m of rows) {
      memCache[m.channel] = m;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="ch">${String(m.channel).padStart(3, "0")}</td>
        <td class="name">${m.name || ""}</td>
        <td>${fmtMHz(m.rx_freq)}</td>
        <td>${shiftLabel(m)}</td>
        <td>${toneLabel(m)}</td>
        <td>${FMMODE[m.fm_mode] || "FM"}</td>
        <td>${STEP_HZ[m.step] ? STEP_HZ[m.step] / 1000 + " kHz" : "—"}</td>
        <td class="row-actions">
          <button class="edit" data-ch="${m.channel}" title="Bearbeiten">✎</button>
          <button class="recall" data-ch="${m.channel}" title="Auf Band A">→A</button>
          <button class="recall-b" data-ch="${m.channel}" title="Auf Band B">→B</button>
          <button class="del" data-ch="${m.channel}" title="Delete">✕</button>
        </td>`;
      body.appendChild(tr);
    }
    $$(".edit", body).forEach(b => b.onclick = () => openEditor(Number(b.dataset.ch)));
    $$(".recall", body).forEach(b => b.onclick = () => recall(0, Number(b.dataset.ch)));
    $$(".recall-b", body).forEach(b => b.onclick = () => recall(1, Number(b.dataset.ch)));
    $$(".del", body).forEach(b => b.onclick = () => delMem(Number(b.dataset.ch)));
  } catch (e) { body.innerHTML = `<tr><td colspan="8" class="empty">Error: ${e.message}</td></tr>`; }
}

async function recall(band, ch) {
  try { await api("POST", "/api/recall", { band, channel: ch }); toast(`Channel ${ch} → Band ${band ? "B" : "A"}`, "ok"); }
  catch (e) { toast("Recall: " + e.message, "err"); }
}
async function delMem(ch) {
  if (!await confirmDialog(`Delete memory channel ${ch}?`,
        { title: "Delete channel", okText: "DELETE", danger: true })) return;
  try { await api("DELETE", `/api/memories/${ch}`); toast(`Channel ${ch} deleted`, "ok"); loadMemories(); loadQuickMemNames(); }
  catch (e) { toast("Delete: " + e.message, "err"); }
}

function bindMemory() {
  $("#mem-load").onclick = loadMemories;
  $("#mem-export").addEventListener("click", e => {
    e.target.href = apiUrl(`/api/memories.csv?start=${$("#mem-start").value || 0}&end=${$("#mem-end").value || 999}`);
  });
  $("#mem-import").addEventListener("change", async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try { const r = await fetch(apiUrl("/api/memories/import"), { method: "POST", body: fd }); const j = await r.json();
      toast(`${j.imported} channels imported`, "ok"); loadMemories(); loadQuickMemNames(); }
    catch (err) { toast("Import fehlgeschlagen", "err"); }
    e.target.value = "";
  });
}

// ---- memory editor --------------------------------------------------------
// The radio refuses a memory write whose frequency isn't a multiple of the
// channel step. Keep the chosen step if it fits, else pick the finest that does
// (so e.g. 123.780 MHz air-band channels save at a 5 kHz step instead of failing).
function alignStepIndex(freqHz, preferred) {
  if (STEP_HZ[preferred] && freqHz % STEP_HZ[preferred] === 0) return preferred;
  const order = STEP_HZ.map((s, i) => i).sort((a, b) => STEP_HZ[a] - STEP_HZ[b]);
  for (const i of order) if (STEP_HZ[i] && freqHz % STEP_HZ[i] === 0) return i;
  return preferred;
}
// True when a frequency in MHz falls in the (RX-only, AM) air band.
const isAirbandMHz = mhz => mhz >= 118 && mhz <= 137;

// Normalise the RX field so the MHz.kHz decimal point is always shown: e.g.
// "145" → "145.600"? no — "145" → "145.000", "123.78" → "123.780". Keeps finer
// precision (down to 10 Hz) but never drops below 3 decimals (kHz separator).
function fmtRxField(v) {
  const f = parseFloat(v);
  if (!isFinite(f)) return v;
  let s = f.toFixed(5).replace(/0+$/, "");
  if (s.endsWith(".")) s += "000";
  if ((s.split(".")[1] || "").length < 3) s = f.toFixed(3);
  return s;
}

function fillStepOptions(sel) {
  sel.innerHTML = STEP_HZ.map((s, i) =>
    `<option value="${i}">${s / 1000} kHz</option>`).join("");
}
function fillToneOptions(selected) {
  const type = $("#ed-tonetype").value, val = $("#ed-toneval");
  if (type === "dcs")
    val.innerHTML = DCS_CODES.map((c, i) =>
      `<option value="${i}">D${String(c).padStart(3, "0")}</option>`).join("");
  else if (type === "ctcss" || type === "tone")
    val.innerHTML = CTCSS_TONES.map((h, i) =>
      `<option value="${i + 1}">${h.toFixed(1)} Hz</option>`).join("");
  else val.innerHTML = "<option value=''>—</option>";
  val.disabled = (type === "none");
  if (selected != null) val.value = String(selected);
}

function openEditor(ch, isNew = false) {
  const m = (!isNew && memCache[ch]) ? memCache[ch] : null;
  edState = { channel: ch, isNew };
  $("#ed-title").textContent = isNew
    ? "New memory channel" : `Edit channel ${String(ch).padStart(3, "0")}`;
  const chIn = $("#ed-channel");
  chIn.value = ch ?? 0; chIn.disabled = !isNew;
  $("#ed-name").value = m?.name || "";
  $("#ed-rx").value = m ? fmtRxField(m.rx_freq / 1e6) : "";
  $("#ed-mode").value = String(m?.fm_mode ?? 0);
  fillStepOptions($("#ed-step")); $("#ed-step").value = String(m?.step ?? 4);
  $("#ed-shift").value = String(m?.shift ?? 0);
  $("#ed-offset").value = m ? (m.offset / 1000) : 0;
  let tt = "none", sel = null;
  if (m?.ctcss_on) { tt = "ctcss"; sel = m.ctcss_idx; }
  else if (m?.tone_on) { tt = "tone"; sel = m.tone_idx; }
  else if (m?.dcs_on) { tt = "dcs"; sel = m.dcs_idx; }
  $("#ed-tonetype").value = tt; fillToneOptions(sel);
  $("#editor").classList.add("open");
}
function closeEditor() { $("#editor").classList.remove("open"); }

async function saveEditor() {
  const ch = Number($("#ed-channel").value);
  if (ch < 0 || ch > 999) return toast("Channel number 0–999", "err");
  const rx = Math.round(parseFloat($("#ed-rx").value) * 1e6);
  if (!rx || rx < 1e6) return toast("Invalid RX frequency", "err");
  const tt = $("#ed-tonetype").value, tv = Number($("#ed-toneval").value) || 1;
  const chosenStep = Number($("#ed-step").value);
  const step = alignStepIndex(rx, chosenStep);
  if (step !== chosenStep) $("#ed-step").value = String(step);  // reflect the fix
  // air-band channels are AM by definition — force it (and reflect in the UI)
  const fm_mode = isAirbandMHz(rx / 1e6) ? 2 : Number($("#ed-mode").value);
  if (fm_mode === 2) $("#ed-mode").value = "2";
  const m = {
    channel: ch, name: $("#ed-name").value.slice(0, 8), rx_freq: rx, tx_freq: 0,
    step, shift: Number($("#ed-shift").value),
    reverse: 0, offset: Math.round(Number($("#ed-offset").value) * 1000),
    fm_mode,
    tone_on: tt === "tone", ctcss_on: tt === "ctcss", dcs_on: tt === "dcs",
    tone_idx: tt === "tone" ? tv : 1, ctcss_idx: tt === "ctcss" ? tv : 1,
    dcs_idx: tt === "dcs" ? tv : 0, lockout: 0,
  };
  try {
    await api("PUT", `/api/memories/${ch}`, m);
    const note = step !== chosenStep ? ` (step → ${STEP_HZ[step] / 1000} kHz)` : "";
    toast(`Channel ${ch} saved${note}`, "ok");
    closeEditor(); loadMemories(); loadQuickMemNames();
  } catch (e) { toast("Save: " + e.message, "err"); }
}

function bindEditor() {
  const rxIn = $("#ed-rx");
  // always render the MHz.kHz decimal separator once the field loses focus
  rxIn.addEventListener("blur", () => { if (rxIn.value.trim()) rxIn.value = fmtRxField(rxIn.value); });
  // live: an air-band frequency is RX-only AM — switch the mode select to AM
  // (and back off AM again if the frequency leaves the air band)
  rxIn.addEventListener("input", e => {
    // auto-insert the MHz.kHz separator once three digits (the MHz part) are
    // typed — but not while deleting, so backspacing past the dot still works.
    const del = e.inputType && e.inputType.startsWith("delete");
    if (!del && /^\d{3}$/.test(rxIn.value)) rxIn.value += ".";
    const sel = $("#ed-mode");
    if (isAirbandMHz(parseFloat(rxIn.value))) sel.value = "2";
    else if (sel.value === "2") sel.value = "0";
  });
  $("#ed-tonetype").addEventListener("change", () => fillToneOptions());
  $("#ed-save").addEventListener("click", saveEditor);
  $("#ed-cancel").addEventListener("click", closeEditor);
  $("#editor").addEventListener("click", e => { if (e.target.id === "editor") closeEditor(); });
  $("#mem-new").addEventListener("click", () => openEditor(Number($("#mem-start").value || 0), true));
  window.addEventListener("keydown", e => { if (e.key === "Escape") closeEditor(); });
}

// ---- audio panel (direct WebRTC <-> Pi, Opus) -----------------------------
// Keep a Bluetooth headset permanently on A2DP (good-quality stereo playback) so
// RX audio always reaches it. A2DP can't carry a microphone, and using the
// headset's mic would force Android onto the mono HFP/SCO profile (which also
// gets stuck until Bluetooth is toggled). So we capture TX audio from a
// NON-Bluetooth input — the phone's built-in mic — which never triggers SCO. The
// headset then stays on A2DP the whole time; the radio mic comes from the phone.
let audioPc = null, audioMic = null;
let audioWant = false;          // user wants audio on → auto-reconnect on drops
let audioReconnectT = null;     // pending reconnect timer
let audioReconnectDelay = 400;  // reconnect backoff (ms), reset on a good connect
let audioGraceT = null;         // grace timer: ride out a transient "disconnected"

// Pick an audio input that is NOT the Bluetooth headset (so the headset stays on
// A2DP). Returns a deviceId or null. Labels are only populated after mic
// permission has been granted.
async function pickNonBtMicId() {
  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const ins = devs.filter(d => d.kind === "audioinput" && d.deviceId && d.deviceId !== "default");
    const isBt = l => /bluetooth|headset|hands?-?free|hfp|sco|airpod|buds|wh-|wf-/i.test(l || "");
    const builtin = ins.find(d => /built|internal|phone|handset|mic|mikrofon/i.test(d.label) && !isBt(d.label));
    const nonBt = builtin || ins.find(d => d.label && !isBt(d.label));
    return nonBt ? nonBt.deviceId : null;
  } catch { return null; }
}
// Mic capture constraints depend on the platform — two conflicting needs:
//  • Mobile / installed PWA: a Bluetooth headset must stay on A2DP. The browser
//    enables echoCancellation by default, which switches the headset to mono
//    HFP/SCO (pulling RX playback off A2DP, onto the phone speaker). So we force
//    EC/NS/AGC OFF there to keep playback on the headset.
//  • Desktop (e.g. macOS): forcing those off makes the browser reconfigure the
//    input device and drop the level very low (across all browsers), so we leave
//    them at the browser default.
const isPwaMode = () => !!(window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
function micConstraintsBase() {
  const mobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "");
  return (isPwaMode() || mobile)
    ? { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
    : {};
}
// TX AGC toggle position is remembered per app (browser vs installed PWA). They
// share one localStorage (same origin), so key it by display mode to separate.
const txAgcKey = () => "tmv71.txAgc." + (isPwaMode() ? "pwa" : "browser");
// Capture the mic, preferring a non-Bluetooth (built-in) input.
async function captureBuiltinMic() {
  const base = micConstraintsBase();
  let id = await pickNonBtMicId();                 // works if permission persisted
  let stream = await navigator.mediaDevices.getUserMedia({
    audio: id ? { ...base, deviceId: { exact: id } } : base });
  if (!id) {
    // first run: labels are available now — switch to a non-BT mic if needed
    const id2 = await pickNonBtMicId();
    const cur = stream.getAudioTracks()[0]?.getSettings?.().deviceId;
    if (id2 && id2 !== cur) {
      stream.getTracks().forEach(tr => tr.stop());
      stream = await navigator.mediaDevices.getUserMedia({ audio: { ...base, deviceId: { exact: id2 } } });
    }
  }
  return stream;
}

function audioConnected() {
  return !!audioPc && ["connected", "completed"].includes(audioPc.connectionState);
}
function updateAudioToggle() {
  const t = $("#audio-toggle");
  if (t) {
    const reconnecting = audioWant && !audioPc;
    t.textContent = reconnecting ? "…" : audioPc ? "DISCONNECT" : "CONNECT";
    t.classList.toggle("connected", !!audioPc || reconnecting);
  }
  updatePttEnabled();
}
// PTT / PTT-LOCK only make sense with audio (the mic) connected: disable them
// when audio is off so transmit can't be keyed without modulation.
function updatePttEnabled() {
  const ok = audioConnected();
  ["#ptt", "#ptt-lock"].forEach(sel => {
    const el = document.querySelector(sel);
    if (el) { el.disabled = !ok; el.classList.toggle("tx-disabled", !ok); }
  });
}
// End any active transmit (held key or latched lock) when audio goes away.
function endTxOnAudioLoss() {
  if (pttLock && setPttLock) setPttLock(false);
  else if (txActive) api("POST", "/api/ptt", { transmit: false }).catch(() => {});
}
function teardownAudio() {
  if (audioGraceT) { clearTimeout(audioGraceT); audioGraceT = null; }
  if (audioPc) { try { audioPc.close(); } catch {} audioPc = null; }
  if (audioMic) { audioMic.getTracks().forEach(tr => tr.stop()); audioMic = null; }
  const a = $("#rx-audio"); if (a) a.srcObject = null;
}
function scheduleAudioReconnect() {
  if (audioReconnectT || !audioWant) return;
  const d = audioReconnectDelay;
  audioReconnectDelay = Math.min(audioReconnectDelay * 2, 4000);  // back off on repeated failures
  audioReconnectT = setTimeout(() => {
    audioReconnectT = null;
    if (audioWant && !audioPc) audioConnect();
  }, d);
}
// Unexpected drop (ICE failed/disconnected, negotiation error): tear the peer
// down and retry while the user still wants audio.
function audioDropped() {
  const was = audioWant;
  endTxOnAudioLoss();          // never keep transmitting without audio
  teardownAudio();
  updateAudioToggle();
  if (was) { toast("Audio lost — reconnecting…", "err"); scheduleAudioReconnect(); }
}
async function audioConnect() {
  audioWant = true;
  try { localStorage.setItem("tmv71.audioOn", "1"); } catch {}   // persist: auto-connect next launch
  const t = $("#audio-toggle");
  if (t) { t.disabled = true; t.textContent = "…"; }
  let mic;
  try {
    mic = await captureBuiltinMic();     // non-BT mic → headset stays on A2DP
  } catch (e) {
    // mic permission / device error — needs the user, so don't loop on it
    toast("Audio: " + e.message, "err"); audioDisconnect();
    if (t) t.disabled = false; return;
  }
  try {
    audioMic = mic;
    audioPc = new RTCPeerConnection();
    audioMic.getTracks().forEach(tr => audioPc.addTrack(tr, audioMic));
    audioPc.addEventListener("track", e => { $("#rx-audio").srcObject = e.streams[0]; });
    audioPc.addEventListener("connectionstatechange", () => {
      updateAudioToggle();
      if (!audioPc) return;
      const st = audioPc.connectionState;
      if (st === "connected") {
        audioReconnectDelay = 400;                       // healthy → reset backoff
        if (audioGraceT) { clearTimeout(audioGraceT); audioGraceT = null; }
      } else if (st === "failed" || st === "closed") {
        audioDropped();                                  // terminal → reconnect now
      } else if (st === "disconnected" && !audioGraceT) {
        // transient blip: give ICE a short moment to self-heal before tearing
        // down. If it recovers to "connected" the grace timer is cancelled above.
        audioGraceT = setTimeout(() => {
          audioGraceT = null;
          if (audioPc && audioPc.connectionState === "disconnected") audioDropped();
        }, 1200);
      }
    });
    const offer = await audioPc.createOffer({ offerToReceiveAudio: true });
    await audioPc.setLocalDescription(offer);
    await new Promise(res => {                       // non-trickle: gather ICE
      if (audioPc.iceGatheringState === "complete") return res();
      const h = () => { if (audioPc.iceGatheringState === "complete") { audioPc.removeEventListener("icegatheringstatechange", h); res(); } };
      audioPc.addEventListener("icegatheringstatechange", h);
      setTimeout(res, 1500);   // LAN host candidates gather fast; cap the wait short
    });
    const ans = await api("POST", "/api/webrtc/offer",
      { sdp: audioPc.localDescription.sdp, type: audioPc.localDescription.type });
    await audioPc.setRemoteDescription(ans);
    toast("Audio connected", "ok");
  } catch (e) {
    // backend/negotiation failure: retry (e.g. service still coming back up)
    toast("Audio: " + e.message, "err"); audioDropped();
  }
  if (t) t.disabled = false;
  updateAudioToggle();
}
function audioDisconnect() {       // user-initiated: stop and don't reconnect
  audioWant = false;
  endTxOnAudioLoss();              // end PTT/PTT-LOCK when audio goes away
  try { localStorage.setItem("tmv71.audioOn", "0"); } catch {}
  audioReconnectDelay = 400;
  if (audioReconnectT) { clearTimeout(audioReconnectT); audioReconnectT = null; }
  teardownAudio();
  updateAudioToggle();
}
function setGainUi(key, val) {
  const sl = document.getElementById(key + "-gain");
  const dec = sl && Number(sl.step) >= 1 ? 0 : 1;   // whole-step sliders show no decimal
  const el = document.getElementById(key + "-gain-val");
  if (el) el.textContent = Number(val).toFixed(dec) + "×";
  if (sl) {
    const pct = (sl.value - sl.min) / (sl.max - sl.min) * 100;
    sl.style.setProperty("--gpct", pct + "%");
  }
}
// TX auto-gain on → the manual TX slider is overridden; grey + disable it.
function setTxAutoUi(on) {
  const sl = $("#tx-gain"); if (sl) sl.disabled = on;
  const lbl = sl && sl.closest(".acg"); if (lbl) lbl.classList.toggle("is-auto", on);
}
async function loadAudioDevices() {
  const sel = $("#audio-device"); if (!sel) return;
  let d;
  try { d = await api("GET", "/api/audio/devices"); } catch { return; }
  sel.innerHTML = (d.devices || []).map(x => `<option value="${x.name}">${x.name}</option>`).join("");
  const cur = (d.current || "").toLowerCase();
  const m = [...sel.options].find(o => o.value.toLowerCase().includes(cur) || cur.includes(o.value.toLowerCase()));
  if (m) sel.value = m.value;
  else if (d.current) {
    const o = document.createElement("option");
    o.value = d.current; o.textContent = d.current + " (aktuell)";
    sel.appendChild(o); sel.value = d.current;
  }
}
// USB sound-card mixer (settings ▸ Audio): a slider (+ on/off) per control
async function loadMixer() {
  const box = $("#mixer-list"); if (!box) return;
  let d;
  try { d = await api("GET", "/api/audio/mixer"); }
  catch { box.innerHTML = '<p class="pane-hint">Mixer unavailable.</p>'; return; }
  if (!d.controls || !d.controls.length) {
    box.innerHTML = '<p class="pane-hint">No mixer controls — this USB device has no software volume.</p>';
    return;
  }
  // Friendly labels for the radio audio path: a capture control (or a "Mic"
  // monitor) is the radio RX into the Pi; the main playback output
  // (PCM/Speaker/Headphone) drives the radio mic (TX).
  const TX_NAMES = new Set(["PCM", "Speaker", "Headphone", "Master"]);
  const RX_NAMES = new Set(["Mic", "Capture"]);
  const mxLabel = c => (c.kind === "capture" || RX_NAMES.has(c.name)) ? "RX"
                     : (TX_NAMES.has(c.name) ? "TX" : c.name);
  box.innerHTML = "";
  d.controls.forEach(c => {
    const row = document.createElement("div");
    row.className = "mixer-row";
    const label = mxLabel(c);
    row.innerHTML =
      `<span class="mx-name">${label} · ${c.kind}</span>` +
      `<input type="range" class="mx-slider" min="0" max="100" step="1" value="${c.percent ?? 0}">` +
      `<span class="mx-val">${c.percent ?? 0}%</span>` +
      (c.has_switch ? `<label class="mx-mute"><input type="checkbox" class="mx-sw" ${c.switch_on ? "checked" : ""}>on</label>` : "");
    const sl = row.querySelector(".mx-slider"), val = row.querySelector(".mx-val"),
          sw = row.querySelector(".mx-sw");
    sl.style.setProperty("--gpct", (c.percent ?? 0) + "%");
    // recommended-default marker on the tick: RX (capture) 85 %, TX (playback) 33 %
    if (label === "RX") sl.style.setProperty("--def-pct", "85%");
    else if (label === "TX") sl.style.setProperty("--def-pct", "33%");
    sl.addEventListener("input", () => {
      val.textContent = sl.value + "%";
      sl.style.setProperty("--gpct", sl.value + "%");
    });
    sl.addEventListener("change", async () => {
      try { await api("POST", "/api/audio/mixer", { name: c.name, percent: Number(sl.value) }); }
      catch (e) { toast("Mixer: " + e.message, "err"); }
    });
    if (sw) sw.addEventListener("change", async () => {
      try { await api("POST", "/api/audio/mixer", { name: c.name, switch_on: sw.checked }); }
      catch (e) { toast("Mixer: " + e.message, "err"); }
    });
    box.appendChild(row);
  });
}

function setMsUi(id, val) {
  const el = document.getElementById(id + "-val");
  if (el) el.textContent = Math.round(Number(val)) + " ms";
  const sl = document.getElementById(id);
  if (sl) sl.style.setProperty("--gpct",
    (sl.value - sl.min) / (sl.max - sl.min) * 100 + "%");
}

async function loadTones() {
  try {
    const s = await api("GET", "/api/audio/status");
    // ROGER toggle lives in the PTT panel; reflect its persisted state
    const rb = $("#btn-roger");
    if (rb) { rb.classList.toggle("on", !!s.roger_beep); rb.setAttribute("aria-pressed", String(!!s.roger_beep)); }
    const tt = $("#set-testtone"); if (tt) tt.checked = !!s.test_tone;
    const lp = $("#set-tx-lowpass"); if (lp) lp.checked = !!s.tx_lowpass;
    const rlp = $("#set-rx-lowpass"); if (rlp) rlp.checked = !!s.rx_lowpass;
    const rde = $("#set-rx-deemph"); if (rde) rde.checked = !!s.rx_deemph;
    const rsq = $("#set-rx-squelch"); if (rsq) rsq.checked = !!s.rx_squelch;
    if (s.rx_deemph_us != null) { const sl = $("#deemph-us"); if (sl) { sl.value = s.rx_deemph_us; setDeemphUi(s.rx_deemph_us); } }
    if (s.tx_buffer_ms != null) { const sl = $("#tx-buffer"); if (sl) sl.value = s.tx_buffer_ms; setMsUi("tx-buffer", s.tx_buffer_ms); }
    if (s.ptt_tail_ms != null) { const sl = $("#ptt-tail"); if (sl) sl.value = s.ptt_tail_ms; setMsUi("ptt-tail", s.ptt_tail_ms); }
  } catch { /* leave as-is */ }
}

function bindAudio() {
  const t = $("#audio-toggle"); if (!t) return;
  // only run the smooth-scroll graph loop while its canvas is actually on-screen
  const gcv = $("#level-graph");
  if (gcv && "IntersectionObserver" in window) {
    new IntersectionObserver(es => {
      graphOnScreen = es[0].isIntersecting;
      if (graphOnScreen && audioConnected()) startLevelLoop();
    }, { threshold: 0.01 }).observe(gcv);
  }
  $$("#audio-band-seg .sg").forEach(b => b.addEventListener("click", async () => {
    const band = Number(b.dataset.band);
    try { const st = await api("POST", "/api/data-band", { band }); render(st); }
    catch (err) { toast("Audio band: " + err.message, "err"); }
  }));
  $("#set-testtone")?.addEventListener("change", async e => {
    try {
      await api("POST", "/api/audio/tones", { test_tone: e.target.checked });
      toast(e.target.checked ? "Two-tone test ON — key PTT" : "Two-tone test off", e.target.checked ? "ok" : "");
    } catch (err) { toast("Test tone: " + err.message, "err"); e.target.checked = !e.target.checked; }
  });
  $("#audio-mictest")?.addEventListener("change", async e => {
    const on = e.target.checked;
    if (on && !audioConnected())
      toast("Connect audio first to test the mic", "");
    // mute the radio RX (noise) while testing; un-mute on switch-off so the
    // echo replay is audible
    micTestActive = on;
    { const a = $("#rx-audio"); if (a) a.muted = on || selcallMuted; }
    // cap the test at 30 s — auto-switch off (which starts the replay)
    if (micTestTimer) { clearTimeout(micTestTimer); micTestTimer = null; }
    if (on) micTestTimer = setTimeout(() => {
      const cb = $("#audio-mictest");
      if (cb && cb.checked) { cb.checked = false; cb.dispatchEvent(new Event("change")); }
    }, 30000);
    try {
      const st = await api("POST", "/api/audio/tones", { mic_test: on });
      toast(on ? "Mic test ON (max 30 s) — speak; switch off to hear it back"
               : (st && st.echo_busy ? "Replaying your mic test…" : "Mic test off"),
            on || (st && st.echo_busy) ? "ok" : "");
    } catch (err) {
      toast("Mic test: " + err.message, "err");
      e.target.checked = !on; micTestActive = !on;
      const a = $("#rx-audio"); if (a) a.muted = (!on) || selcallMuted;
    }
  });
  $("#set-tx-lowpass")?.addEventListener("change", async e => {
    try {
      await api("POST", "/api/audio/tones", { tx_lowpass: e.target.checked });
      toast(e.target.checked ? "TX low-pass on" : "TX low-pass off", e.target.checked ? "ok" : "");
    } catch (err) { toast("TX low-pass: " + err.message, "err"); e.target.checked = !e.target.checked; }
  });
  $("#set-rx-lowpass")?.addEventListener("change", async e => {
    try {
      await api("POST", "/api/audio/tones", { rx_lowpass: e.target.checked });
      toast(e.target.checked ? "RX low-pass on" : "RX low-pass off", e.target.checked ? "ok" : "");
    } catch (err) { toast("RX low-pass: " + err.message, "err"); e.target.checked = !e.target.checked; }
  });
  $("#set-rx-deemph")?.addEventListener("change", async e => {
    try {
      await api("POST", "/api/audio/tones", { rx_deemph: e.target.checked });
      toast(e.target.checked ? "RX de-emphasis on" : "RX de-emphasis off", e.target.checked ? "ok" : "");
    } catch (err) { toast("RX de-emphasis: " + err.message, "err"); e.target.checked = !e.target.checked; }
  });
  $("#set-rx-squelch")?.addEventListener("change", async e => {
    try {
      await api("POST", "/api/audio/tones", { rx_squelch: e.target.checked });
      toast(e.target.checked ? "RX squelch on" : "RX squelch off", e.target.checked ? "ok" : "");
    } catch (err) { toast("RX squelch: " + err.message, "err"); e.target.checked = !e.target.checked; }
  });
  t.addEventListener("click", () => { (audioWant || audioPc) ? audioDisconnect() : audioConnect(); });

  $("#audio-device").addEventListener("change", async e => {
    try { await api("POST", "/api/audio/device", { device: e.target.value }); toast("Audio device switched", "ok"); }
    catch (err) { toast("Audio device: " + err.message, "err"); loadAudioDevices(); }
  });
  ["rx", "tx"].forEach(k => {
    const sl = document.getElementById(k + "-gain");
    sl.addEventListener("input", () => setGainUi(k, sl.value));
    sl.addEventListener("change", async () => {
      const body = {}; body[k + "_gain"] = Number(sl.value);
      try { await api("POST", "/api/audio/gain", body); }
      catch (e) { toast("Gain: " + e.message, "err"); }
    });
  });
  const txAuto = $("#tx-auto");
  if (txAuto) {
    // apply this app's remembered TX AGC position (backend is shared/global, so
    // pushing it here makes this app's preference take effect on launch)
    const stored = localStorage.getItem(txAgcKey());
    if (stored !== null) {
      const on = stored === "1";
      txAuto.checked = on; setTxAutoUi(on);
      api("POST", "/api/audio/gain", { tx_auto_gain: on }).catch(() => {});
    }
    txAuto.addEventListener("change", async () => {
      setTxAutoUi(txAuto.checked);
      try { localStorage.setItem(txAgcKey(), txAuto.checked ? "1" : "0"); } catch {}
      try { await api("POST", "/api/audio/gain", { tx_auto_gain: txAuto.checked }); }
      catch (e) { toast("Auto gain: " + e.message, "err"); txAuto.checked = !txAuto.checked; setTxAutoUi(txAuto.checked); }
    });
  }
  [["tx-buffer", "tx_buffer_ms"], ["ptt-tail", "ptt_tail_ms"]].forEach(([id, key]) => {
    const sl = document.getElementById(id); if (!sl) return;
    sl.addEventListener("input", () => setMsUi(id, sl.value));
    sl.addEventListener("change", async () => {
      const body = {}; body[key] = Number(sl.value);
      try { await api("POST", "/api/audio/buffer", body); }
      catch (e) { toast("TX timing: " + e.message, "err"); }
    });
  });
  const du = $("#deemph-us");
  if (du) {
    du.addEventListener("input", () => setDeemphUi(du.value));
    du.addEventListener("change", async () => {
      try { await api("POST", "/api/audio/tones", { rx_deemph_us: Number(du.value) }); }
      catch (e) { toast("De-emphasis: " + e.message, "err"); }
    });
  }
}
function setDeemphUi(val) {
  const el = $("#deemph-us-val"); if (el) el.textContent = Math.round(val) + " µs";
  const sl = $("#deemph-us");
  if (sl) sl.style.setProperty("--gpct", (sl.value - sl.min) / (sl.max - sl.min) * 100 + "%");
}

// ---- VFO live parameters (shift / bandwidth / CTCSS) ----------------------
function buildToneSelects() {
  $$(".tone-sel").forEach(sel => {
    sel.innerHTML = '<option value="">CTCSS off</option>' +
      CTCSS_TONES.map((h, i) => `<option value="${i + 1}">CTCSS ${h.toFixed(1)}</option>`).join("");
  });
  $$(".tone-tx-sel").forEach(sel => {
    sel.innerHTML = '<option value="">Tone off</option>' +
      CTCSS_TONES.map((h, i) => `<option value="${i + 1}">Tone ${h.toFixed(1)}</option>`).join("");
  });
}
async function sendVfo(band, fields) {
  try { await api("POST", "/api/vfo", { band, ...fields }); toast(`Band ${band ? "B" : "A"} updated`, "ok"); }
  catch (e) { toast("VFO: " + e.message, "err"); }
}
function updateVfoParams(b) {
  const p = document.querySelector(`.vfo-params[data-band="${b.band}"]`);
  if (!p) return;
  p.querySelectorAll(".shift-seg .sg").forEach(x =>
    x.classList.toggle("active", Number(x.dataset.shift) === b.shift));
  p.querySelectorAll(".bw-seg .sg").forEach(x =>
    x.classList.toggle("active", Number(x.dataset.mode) === b.fm_mode));
  const ts = p.querySelector(".tone-sel");
  if (ts && document.activeElement !== ts) {
    let v = "";
    if (b.ctcss_on && b.ctcss_hz) {
      const idx = CTCSS_TONES.findIndex(h => Math.abs(h - b.ctcss_hz) < 0.05);
      if (idx >= 0) v = String(idx + 1);
    }
    ts.value = v;
  }
  if (ts) ts.classList.toggle("active", !!ts.value);
  const tx = p.querySelector(".tone-tx-sel");
  if (tx && document.activeElement !== tx) {
    let v = "";
    if (b.tone_on && b.tone_hz) {
      const idx = CTCSS_TONES.findIndex(h => Math.abs(h - b.tone_hz) < 0.05);
      if (idx >= 0) v = String(idx + 1);
    }
    tx.value = v;
  }
  if (tx) tx.classList.toggle("active", !!tx.value);
  const off = p.querySelector(".offset-in");
  if (off && document.activeElement !== off) off.value = Math.round(b.offset / 1000);
}
function bindVfoParams() {
  buildToneSelects();
  $$(".shift-seg .sg").forEach(btn => btn.onclick = () =>
    sendVfo(Number(btn.closest(".vfo-params").dataset.band), { shift: Number(btn.dataset.shift) }));
  $$(".bw-seg .sg").forEach(btn => btn.onclick = () =>
    sendVfo(Number(btn.closest(".vfo-params").dataset.band), { fm_mode: Number(btn.dataset.mode) }));
  $$(".tone-sel").forEach(sel => sel.onchange = () => {
    sel.classList.toggle("active", !!sel.value);
    const band = Number(sel.closest(".vfo-params").dataset.band);
    sendVfo(band, sel.value
      ? { ctcss_on: true, ctcss_idx: Number(sel.value), tone_on: false, dcs_on: false }
      : { ctcss_on: false, tone_on: false, dcs_on: false });
  });
  // Tone (TX-only). Tone, CTCSS and DCS are mutually exclusive on the radio.
  $$(".tone-tx-sel").forEach(sel => sel.onchange = () => {
    sel.classList.toggle("active", !!sel.value);
    const band = Number(sel.closest(".vfo-params").dataset.band);
    sendVfo(band, sel.value
      ? { tone_on: true, tone_idx: Number(sel.value), ctcss_on: false, dcs_on: false }
      : { tone_on: false, ctcss_on: false, dcs_on: false });
  });
  $$(".offset-in").forEach(inp => {
    inp.addEventListener("input", () => { inp.value = inp.value.replace(/\D/g, ""); });
    inp.addEventListener("keydown", e => {
      if (e.key !== "Enter") return;
      const band = Number(inp.closest(".vfo-params").dataset.band);
      sendVfo(band, { offset: Number(inp.value || 0) * 1000 });
      inp.blur();
    });
  });
}

// ---- settings (tabbed: General / Info / Memory / DTMF) ----------------
function renderCallsign() {
  // render the cached value instantly, then refresh from the server (the
  // callsign is persisted server-side so it survives across browsers/devices).
  $("#callsign").textContent = localStorage.getItem("tmv71.callsign") || "";
}

async function syncCallsign() {
  try {
    const cs = (await api("GET", "/api/callsign")).callsign || "";
    localStorage.setItem("tmv71.callsign", cs);
    $("#callsign").textContent = cs;
  } catch { /* keep cached value if backend unreachable */ }
}

// Title-bar logo (stored server-side in a gitignored dir, never committed).
// Root-CA download (settings): only shown when the backend actually serves it
// (the file is local/gitignored — absent on a fresh clone until a CA is made).
async function refreshCaLink() {
  const field = $("#cert-field"), link = $("#ca-download");
  if (!field || !link) return;
  const url = apiUrl("/tm-v71-ca.crt");
  link.href = url;
  try { field.hidden = !(await fetch(url, { method: "HEAD" })).ok; }
  catch { field.hidden = true; }
}

async function refreshLogo() {
  let has = false;
  try { has = (await api("GET", "/api/branding")).has_logo; } catch {}
  const src = has ? apiUrl(`/api/branding/logo?t=${Date.now()}`) : "";
  const img = $("#brand-logo"), mark = $("#brand-mark"), prev = $("#logo-preview");
  if (img) { img.hidden = !has; if (has) img.src = src; }
  if (mark) mark.hidden = has;          // the logo replaces the ▚ glyph
  if (prev) prev.innerHTML = has ? `<img src="${src}" alt="Logo">` : "";
}

// GPIO power switch (relay on the radio's DC line)
let powerState = null;
function refreshPowerUi() {
  const btn = $("#power-switch");
  if (!btn || !powerState) return;
  const avail = !!powerState.available, on = powerState.on;
  btn.classList.toggle("unavailable", !avail);
  btn.classList.toggle("on", avail && on === true);
  btn.classList.toggle("off", avail && on === false);
  // Dim the band panels while the radio is powered off (GPIO relay open).
  document.body.classList.toggle("radio-off", avail && on === false);
  // Radio off -> also drop audio and the HackRF (the backend stops them too;
  // this keeps the UI in sync). Idempotent: only acts when something is active.
  if (avail && on === false) {
    if (audioWant || audioPc) audioDisconnect();
    if (hrf.on) { hrf.on = false; updateHrfPower(); hrfSync(); }
  }
  btn.title = !avail ? "GPIO-Pin in den Einstellungen festlegen"
    : on === true ? "Radio is ON — click to turn off"
    : on === false ? "Radio is OFF — click to turn on"
    : "Radio on/off (GPIO)";
  updateRadioHint();
}
async function refreshPower() {
  try { powerState = await api("GET", "/api/power-switch"); refreshPowerUi(); } catch {}
}
// After a GPIO power-on the radio needs a few seconds before it answers CAT.
// Wait until it's connected, then (re)label the M0-M9 quick keys — they're
// loaded once at startup, so otherwise they keep the dimmed "M<n>" placeholder
// that was read while the radio was still off.
// The radio's memory channel is captured/restored across power cycles by the
// backend (covers manual + auto power-off, shared across devices); here we just
// relabel the quick keys once CAT is back.
async function relabelQuickMemAfterBoot() {
  await new Promise(r => setTimeout(r, 1000));   // give it time before the first CAT query
  for (let i = 0; i < 15; i++) {
    let st;
    try { st = await api("GET", "/api/status"); } catch { st = null; }
    if (st?.connected) { await loadQuickMemNames(); return; }
    await new Promise(r => setTimeout(r, 2000));
  }
}
function bindPower() {
  $("#power-switch").addEventListener("click", async () => {
    if (!powerState?.available) {
      toast("GPIO-Pin in den Einstellungen festlegen", "err");
      $("#open-settings").click();
      return;
    }
    const turnOff = powerState.on === true;
    if (turnOff && !await confirmDialog("Really turn off the radio?",
          { title: "Turn off radio", okText: "TURN OFF", danger: true })) return;
    try {
      powerState = await api("POST", "/api/power-switch", { on: !turnOff });
      refreshPowerUi();
      toast(powerState.on ? "Radio on" : "Radio off", "ok");
      if (powerState.on) relabelQuickMemAfterBoot();   // wait for CAT, then relabel
    } catch (e) { toast("Power: " + e.message, "err"); }
  });
}

let serialCfg = null;       // last-known {port, baud} from the backend

function switchTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-pane").forEach(p => p.classList.toggle("active", p.dataset.pane === name));
  if (sysTimer) { clearInterval(sysTimer); sysTimer = null; }   // leaving hardware
  if (name === "info") loadInfo();
  if (name === "dtmf") loadDtmf();
  if (name === "audio") { loadAudioDevices(); loadMixer(); loadTones(); }
  if (name === "logging") loadLogConfig();
  if (name === "hardware") { loadSystem(); sysTimer = setInterval(loadSystem, 2000); }
}

async function loadSerialConfig() {
  try {
    const c = await api("GET", "/api/serial-config");
    serialCfg = { port: c.port, baud: c.baud };
    $("#set-port").value = c.port || "";
    $("#port-list").innerHTML = (c.available || [])
      .map(p => `<option value="${p.device}">${p.description || ""}</option>`).join("");
    const sel = $("#set-baud");
    const bauds = c.bauds && c.bauds.length ? c.bauds : [57600];
    if (!bauds.includes(c.baud)) bauds.push(c.baud);
    sel.innerHTML = bauds.sort((a, b) => a - b).map(b => `<option value="${b}">${b}</option>`).join("");
    sel.value = String(c.baud);
  } catch { /* radio offline: leave the fields as entered */ }
}

async function loadInfo() {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = (v ?? "") === "" ? "—" : v; };
  const yesno = v => v == null ? null : (v ? "yes" : "no");
  try {
    const i = await api("GET", "/api/info");
    set("inf-model", i.model); set("inf-serial", i.serial_number);
    set("inf-firmware", i.firmware);
    set("inf-market", i.market === "M" ? "M · EU" : i.market === "K" ? "K · US" : i.market);
    set("inf-crossband", yesno(i.crossband));
    set("inf-type", i.radio_type);
  } catch (e) { toast("Info: " + e.message, "err"); }
}

// ---- hardware tab (Raspberry Pi metrics, auto-refresh while open) ----
let sysTimer = null;
const fmtBytes = n => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};
const fmtUptime = s => {
  s = Math.floor(s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
    m = Math.floor(s % 3600 / 60);
  return (d ? `${d}d ` : "") + (d || h ? `${h}h ` : "") + `${m}m`;
};
async function loadSystem() {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  let s;
  try { s = await api("GET", "/api/system"); }
  catch (e) { toast("System: " + e.message, "err"); return; }
  const pct = (u, t) => t ? ` (${Math.round(u / t * 100)}%)` : "";
  set("sys-model", s.model);
  set("sys-cpu", `${s.cpu_percent}% · ${s.cores} cores${s.freq_mhz ? " @ " + s.freq_mhz + " MHz" : ""}`);
  set("sys-load", (s.load || []).join("  ·  "));
  set("sys-temp", s.temp_c != null ? `${s.temp_c} °C` : "—");
  set("sys-mem", `${fmtBytes(s.mem_used)} / ${fmtBytes(s.mem_total)}${pct(s.mem_used, s.mem_total)}`);
  set("sys-swap", s.swap_total ? `${fmtBytes(s.swap_used)} / ${fmtBytes(s.swap_total)}${pct(s.swap_used, s.swap_total)}` : "none");
  set("sys-disk", `${fmtBytes(s.disk_used)} / ${fmtBytes(s.disk_total)}${pct(s.disk_used, s.disk_total)}`);
  set("sys-uptime", fmtUptime(s.uptime_sec));
}

// ---- software update (git pull from GitHub) ----
async function loadUpdate() {
  const st = $("#update-status"), btn = $("#update-apply");
  if (!st) return;
  st.textContent = "checking…"; if (btn) btn.disabled = true;
  try {
    const u = await api("GET", "/api/update");
    if (!u.is_repo) { st.textContent = "not a git checkout"; return; }
    if (u.dirty) { st.textContent = `local changes present (${u.current}) — update blocked`; return; }
    if (u.behind > 0) {
      st.textContent = `${u.behind} update${u.behind > 1 ? "s" : ""} available · on ${u.current} (${u.branch})`;
      if (btn) { btn.disabled = false; btn.title = u.changes || ""; }
    } else {
      st.textContent = `up to date · ${u.current} (${u.branch})`;
    }
  } catch (e) { st.textContent = "check failed: " + e.message; }
}
async function applyUpdate() {
  const st = $("#update-status");
  if (!await confirmDialog("Pull the latest code from GitHub and restart the service?",
        { title: "Update & restart", okText: "UPDATE" })) return;
  st.textContent = "updating…";
  try {
    const r = await api("POST", "/api/update");
    if (r.ok) {
      toast("Updated — service restarting", "ok");
      st.textContent = "updated · service restarting…";
      setTimeout(() => location.reload(), 6000);
    } else {
      toast("Update failed", "err");
      st.textContent = (r.output || "update failed").slice(0, 200);
    }
  } catch (e) { toast("Update: " + e.message, "err"); st.textContent = "update failed: " + e.message; }
}

async function loadDtmf() {
  const list = $("#dtmf-list");
  let rows;
  try { rows = await api("GET", "/api/dtmf"); }
  catch (e) { list.innerHTML = `<p class="pane-hint">Error: ${e.message}</p>`; return; }
  list.innerHTML = "";
  rows.forEach(m => {
    const row = document.createElement("div");
    row.className = "dtmf-row";
    row.innerHTML =
      `<span class="dtmf-ch">${m.channel}</span>` +
      `<input maxlength="16" placeholder="—" aria-label="DTMF memory ${m.channel}">` +
      `<button class="dtmf-save" data-ch="${m.channel}">SPEICHERN</button>`;
    const inp = row.querySelector("input"), btn = row.querySelector(".dtmf-save");
    inp.value = m.code || "";
    inp.addEventListener("input", () => {
      inp.value = inp.value.toUpperCase().replace(/[^0-9A-D*#]/g, "").slice(0, 16);
      btn.classList.add("dirty");
    });
    inp.addEventListener("keydown", e => { if (e.key === "Enter") btn.click(); });
    btn.addEventListener("click", async () => {
      try {
        await api("PUT", `/api/dtmf/${m.channel}`, { channel: m.channel, code: inp.value });
        btn.classList.remove("dirty");
        toast(`DTMF ${m.channel} saved`, "ok");
      } catch (e) { toast("DTMF: " + e.message, "err"); }
    });
    list.appendChild(row);
  });
}

function bindSettings() {
  $("#open-settings").addEventListener("click", async () => {
    $("#set-apiurl").value = apiBase();
    $("#set-callsign").value = localStorage.getItem("tmv71.callsign") || "";
    try {
      const cs = (await api("GET", "/api/callsign")).callsign;
      if (cs !== undefined) $("#set-callsign").value = cs;
    } catch { /* fall back to cached value */ }
    $("#set-gpio").value = powerState?.pin ?? "";
    $("#set-apo-on").checked = !!powerState?.auto_off_enabled;
    $("#set-apo-sec").value = powerState?.auto_off_seconds ?? 60;
    switchTab("general");
    loadSerialConfig();
    refreshLogo();
    refreshCaLink();
    $("#settings").classList.add("open");
  });

  // logo: upload / Kenwood download / remove
  $("#logo-file").addEventListener("change", async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await fetch(apiUrl("/api/branding/logo"), { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
      await refreshLogo(); toast("Logo set", "ok");
    } catch (err) { toast("Logo upload: " + err.message, "err"); }
    e.target.value = "";
  });
  $("#logo-kenwood").addEventListener("click", async () => {
    try { await api("POST", "/api/branding/logo/kenwood"); await refreshLogo(); toast("Kenwood logo loaded", "ok"); }
    catch (e) { toast("Kenwood logo: " + e.message, "err"); }
  });
  $("#logo-remove").addEventListener("click", async () => {
    try { await api("DELETE", "/api/branding/logo"); await refreshLogo(); toast("Logo removed", "ok"); }
    catch (e) { toast("Entfernen: " + e.message, "err"); }
  });
  const close = () => {
    if (sysTimer) { clearInterval(sysTimer); sysTimer = null; }
    $("#settings").classList.remove("open");
  };
  $("#set-cancel").addEventListener("click", close);
  $("#set-cancel-audio")?.addEventListener("click", close);
  $("#set-close").addEventListener("click", close);
  $("#settings").addEventListener("click", e => { if (e.target.id === "settings") close(); });
  $$(".tab").forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#info-refresh").addEventListener("click", loadInfo);
  $("#sys-refresh")?.addEventListener("click", loadSystem);
  $("#update-check")?.addEventListener("click", loadUpdate);
  $("#update-apply")?.addEventListener("click", applyUpdate);
  window.addEventListener("keydown", e => { if (e.key === "Escape") close(); });

  $("#set-save").addEventListener("click", async () => {
    // API backend URL is a per-browser client setting. Changing which server
    // we talk to means re-establishing the WS / WebRTC / polling, so persist
    // it and reload rather than trying to live-migrate every connection.
    const apiRaw = $("#set-apiurl").value.trim().replace(/\/+$/, "");
    if (apiRaw !== apiBase()) {
      if (apiRaw) localStorage.setItem("tmv71.apiBase", apiRaw);
      else localStorage.removeItem("tmv71.apiBase");
      toast(apiRaw ? `Backend → ${apiRaw}` : "Backend → this server", "ok");
      setTimeout(() => location.reload(), 600);
      return;
    }
    const cs = $("#set-callsign").value.trim().toUpperCase();
    localStorage.setItem("tmv71.callsign", cs);
    try { await api("POST", "/api/callsign", { callsign: cs }); }
    catch (e) { toast("Callsign save: " + e.message, "err"); }
    renderCallsign();
    // GPIO power pin (server-side, persisted)
    const gpioRaw = $("#set-gpio").value.trim();
    const gpioPin = gpioRaw === "" ? null : Number(gpioRaw);
    if (gpioPin !== (powerState?.pin ?? null)) {
      try { powerState = await api("POST", "/api/gpio-config", { pin: gpioPin }); refreshPowerUi(); }
      catch (e) { toast("GPIO: " + e.message, "err"); }
    }
    // Auto-Power-Off (server-side timer)
    const apoOn = $("#set-apo-on").checked;
    const apoSec = Math.max(10, Number($("#set-apo-sec").value) || 60);
    if (apoOn !== !!(powerState?.auto_off_enabled) || apoSec !== (powerState?.auto_off_seconds ?? 60)) {
      try { powerState = await api("POST", "/api/auto-power-off", { enabled: apoOn, seconds: apoSec }); }
      catch (e) { toast("Auto power off: " + e.message, "err"); }
    }
    const port = $("#set-port").value.trim();
    const baud = Number($("#set-baud").value);
    if (serialCfg && port && (port !== serialCfg.port || baud !== serialCfg.baud)) {
      try {
        const r = await api("POST", "/api/serial-config", { port, baud });
        serialCfg = { port, baud };
        toast(r.connected ? `Connected: ${r.model || port}`
                          : `Port ${port} set — no response`, r.connected ? "ok" : "err");
      } catch (e) { toast("Port: " + e.message, "err"); }
    } else {
      toast("Settings saved", "ok");
    }
  });
}

// ---- clock ----------------------------------------------------------------
function tickClock() {
  const d = new Date();
  $("#clock").textContent = d.toLocaleTimeString("de-DE", { hour12: false }) + " UTC" +
    (d.getTimezoneOffset() <= 0 ? "+" : "−") + Math.abs(d.getTimezoneOffset() / 60);
}

// When Band A sits in the air band, the quick-memory keys switch from M0–M9 to
// M50–M59 (a dedicated bank of air-band presets). Re-label only when the base
// actually changes, so the live status stream doesn't trigger constant fetches.
function updateMemBase(st) {
  const b0 = (st.bands || [])[0];
  const inAir = !!b0 && b0.rx_freq >= 118e6 && b0.rx_freq <= 137e6;
  // The air band is receive-only: flag it, badge the panel, and let CSS border
  // the M-keys in Band A's colour while disabling PTT/DTMF/side keys.
  airbandRx = inAir;
  document.body.classList.toggle("airband", inAir);
  const hint = $(".ptt-hint");
  if (hint) hint.textContent = inAir ? "AIR BAND · RECEIVE ONLY" : "HOLD SPACEBAR TO TRANSMIT";
  const base = inAir ? 50 : 0;
  if (base === memBase) return;
  memBase = base;
  loadQuickMemNames();
}

// ---- quick keys: memory recall (M0-M9 / M50-M59 in air band) + DTMF (D0-D9) -
function bindQuickKeys() {
  const mem = $("#quick-mem"), dt = $("#quick-dtmf");
  if (!mem || !dt) return;
  for (let n = 0; n < 10; n++) {
    const m = document.createElement("button");
    m.className = "qbtn qmem empty"; m.dataset.ch = n; m.textContent = "M" + n;
    m.title = `Recall memory ${n} to the CTRL band`;
    m.addEventListener("click", async () => {
      const ch = memBase + n;
      const band = memBase ? 0 : (last?.control_band ?? 0);  // air-band presets → Band A
      try { await api("POST", "/api/recall", { band, channel: ch }); toast(`Memory ${ch} → Band ${band ? "B" : "A"}`, "ok"); }
      catch (e) { toast("M" + ch + ": " + e.message, "err"); }
    });
    mem.appendChild(m);

    const d = document.createElement("button");
    d.className = "qbtn qdtmf"; d.textContent = "DTMF" + n;
    d.title = `Send DTMF memory ${n} (DT, transmitter is keyed briefly)`;
    d.addEventListener("click", async () => {
      if (d.classList.contains("sending")) return;
      d.classList.add("sending");
      try {
        const r = await api("POST", `/api/dtmf/${n}/send`);
        toast(r.sent ? `DTMF ${n} sent: ${r.sent}` : `DTMF ${n} is empty`, r.sent ? "ok" : "err");
      } catch (e) { toast("DTMF " + n + ": " + e.message, "err"); }
      finally { d.classList.remove("sending"); }
    });
    dt.appendChild(d);
  }
  loadQuickMemNames();
}

// Make the quick-memory key whose channel is loaded on the active CTRL VFO glow.
// Only the control band counts (mode === 1 → memory_channel); match it against
// the visible bank (memBase+0 … memBase+9).
function highlightActiveMem(st = last) {
  const cb = (st?.bands || []).find(b => b.band === (st?.control_band ?? 0));
  const active = (cb && cb.mode === 1) ? cb.memory_channel : null;
  $$("#quick-mem .qbtn").forEach(b =>
    b.classList.toggle("active", active != null && memBase + Number(b.dataset.ch) === active));
}

// Label the quick keys with the stored memory name for the current bank
// (channels memBase+0 … memBase+9), or keep the "M<ch>" placeholder (dimmed)
// when the channel is empty — width stays fixed.
async function loadQuickMemNames() {
  const base = memBase;          // capture: memBase can change while the fetch is in flight
  let rows;
  try { rows = await api("GET", `/api/memories?start=${base}&end=${base + 9}`); }
  catch { return; }
  // A newer call for a different bank superseded us (e.g. base 0 fetched at boot
  // resolving after we've switched to the air-band M50 bank) — don't clobber it.
  if (base !== memBase) return;
  const byCh = {}; rows.forEach(r => { byCh[r.channel] = r; });
  $$("#quick-mem .qbtn").forEach(b => {
    const ch = base + Number(b.dataset.ch), m = byCh[ch];
    const name = (m && m.name && m.name.trim()) ? m.name.trim() : "";
    b.textContent = name || ("M" + ch);
    b.classList.toggle("empty", !name);
    b.title = name ? `Recall M${ch} · ${name}` : `Memory ${ch} (empty)`;
  });
  highlightActiveMem();
}

// ---- band scan (spectrum + waterfall) -------------------------------------
let scanBand = null, scanRunning = false, scanTimer = null, scanMeta = null;
const scanSweeps = [];                 // completed sweeps (newest first), each = normalized levels
let scanPoints = [];                   // live points of the current/last sweep
let lastWfSweep = -1;                  // last sweep id pushed to the waterfall (repeat mode)

// shape scanMeta from a /api/scan response (frequency sweep or memory bank)
function scanMetaFrom(s) {
  return s.kind === "mem"
    ? { kind: "mem", total: s.total || 0, ch_start: s.ch_start, ch_end: s.ch_end }
    : { kind: "freq", total: s.total, start_hz: s.start_hz, end_hz: s.end_hz, step_hz: s.step_hz };
}
// NF window: noise floor … strong signal. Higher NF level = stronger occupancy.
const SCAN_FLOOR = -45, SCAN_TOP = -10;
const scanNorm = db => Math.max(0, Math.min(1, (db - SCAN_FLOOR) / (SCAN_TOP - SCAN_FLOOR)));
const scanLevel = p => scanNorm(p.db);

// heat ramp low→high. Dark theme fades from near-black; the light theme starts
// light (so low/empty channels blend with the bright panel) → cyan → amber → red.
const SCAN_PAL_DARK  = [[12,18,24],[16,72,76],[63,224,208],[255,178,62],[255,70,58]];
const SCAN_PAL_LIGHT = [[226,231,234],[120,200,205],[34,165,170],[240,158,48],[222,52,42]];
function scanHeat(t) {
  t = Math.max(0, Math.min(1, t));
  const s = document.body.classList.contains("theme-light") ? SCAN_PAL_LIGHT : SCAN_PAL_DARK;
  const x = t * (s.length - 1), i = Math.floor(x), f = x - i;
  const a = s[i], b = s[Math.min(i + 1, s.length - 1)];
  return `rgb(${Math.round(a[0]+(b[0]-a[0])*f)},${Math.round(a[1]+(b[1]-a[1])*f)},${Math.round(a[2]+(b[2]-a[2])*f)})`;
}

function sizeScanCanvas() {
  const cv = $("#scan-canvas"); if (!cv) return;
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.round(r.width * dpr));
  cv.height = Math.max(1, Math.round(r.height * dpr));
  drawScan();
}

function drawScan() {
  const cv = $("#scan-canvas"); if (!cv) return;
  const ctx = cv.getContext("2d"), W = cv.width, H = cv.height;
  const dpr = window.devicePixelRatio || 1;
  ctx.clearRect(0, 0, W, H);
  // give the waterfall more room so several history rows are visible
  const specH = Math.round(H * 0.6), wfH = H - specH;
  const total = (scanMeta && scanMeta.total) ? scanMeta.total : (scanPoints.length || 1);
  // per-column slot; cap the drawn width so a handful of channels don't blow up
  // into giant bars — keep them at a normal size, centred in their slot.
  const slot = W / total, maxBar = 18 * dpr;
  // spectrum grid
  ctx.strokeStyle = "rgba(127,140,150,0.12)"; ctx.lineWidth = 1;
  for (let g = 1; g < 3; g++) { const y = Math.round(specH * g / 3) + 0.5; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  // spectrum bars (current sweep)
  if (scanPoints.length) {
    const barW = Math.max(1, Math.min(slot - 0.4, maxBar));
    scanPoints.forEach((p, i) => {
      const v = scanLevel(p), h = v * (specH - 4);
      ctx.fillStyle = scanHeat(v);
      ctx.fillRect(i * slot + (slot - barW) / 2, specH - h, barW, h);
    });
  }
  // separator
  ctx.strokeStyle = "rgba(127,140,150,0.28)"; ctx.beginPath(); ctx.moveTo(0, specH + 0.5); ctx.lineTo(W, specH + 0.5); ctx.stroke();
  // waterfall (completed sweeps, newest just under the separator)
  if (scanSweeps.length && total) {
    const rowH = Math.max(4, Math.round(5 * dpr)), maxRows = Math.floor(wfH / rowH);
    const cellW = Math.max(1, Math.min(slot + 0.4, maxBar));
    for (let r = 0; r < Math.min(scanSweeps.length, maxRows); r++) {
      const sweep = scanSweeps[r], y = specH + 1 + r * rowH;
      for (let i = 0; i < sweep.length; i++) { ctx.fillStyle = scanHeat(sweep[i]); ctx.fillRect(i * slot + (slot - cellW) / 2, y, cellW, rowH); }
    }
  }
}

function setScanAxis() {
  if (!scanMeta) return;
  if (scanMeta.kind === "mem") {
    $("#scan-f0").textContent = "M" + (scanMeta.ch_start ?? 0);
    $("#scan-fc").textContent = "memory bank";
    $("#scan-f1").textContent = "M" + (scanMeta.ch_end ?? 99);
    return;
  }
  const mhz = h => (h / 1e6).toFixed(3);
  $("#scan-f0").textContent = mhz(scanMeta.start_hz);
  $("#scan-fc").textContent = mhz((scanMeta.start_hz + scanMeta.end_hz) / 2);
  $("#scan-f1").textContent = mhz(scanMeta.end_hz);
}

function updateScanUi() {
  $$(".scan-band").forEach(x => {
    x.classList.toggle("active", x.dataset.band === scanBand);
    x.classList.toggle("running", scanRunning && x.dataset.band === scanBand);
  });
}

// Clear the graph: spectrum + waterfall history (leaves any running scan to
// repopulate it on the next sweep).
function clearScan() {
  scanPoints = []; scanSweeps.length = 0; lastWfSweep = -1;
  drawScan();
  if (!scanRunning) $("#scan-prog").textContent = "ready";
}

// Pressing a band button starts its scan; pressing again (while running) stops.
function onScanBand(b) {
  if (scanRunning) { stopScan(); return; }
  scanBand = b; startScan();
}

async function startScan() {
  if (!scanBand || scanRunning) return;
  try {
    const s = await api("POST", "/api/scan/start", { band: scanBand });
    scanMeta = scanMetaFrom(s);
    scanPoints = []; scanSweeps.length = 0; lastWfSweep = -1; setScanAxis();
    scanRunning = true; updateScanUi();
    pollScan();
  } catch (e) { toast("Scan: " + e.message, "err"); }
}

async function stopScan() { try { await api("POST", "/api/scan/stop"); } catch {} }

async function pollScan() {
  let s;
  try { s = await api("GET", "/api/scan"); }
  catch { scanTimer = setTimeout(pollScan, 600); return; }
  if (!scanMeta && (s.total || s.kind)) { scanMeta = scanMetaFrom(s); setScanAxis(); }
  if (scanMeta && s.total) scanMeta.total = s.total;   // mem total grows after sweep 1
  scanPoints = s.points || [];
  // while repeating, push one waterfall row per completed sweep
  if (s.running && s.total && s.index >= s.total && s.sweep && s.sweep !== lastWfSweep && scanPoints.length) {
    scanSweeps.unshift(scanPoints.map(scanLevel));
    if (scanSweeps.length > 60) scanSweeps.pop();
    lastWfSweep = s.sweep;
  }
  drawScan();
  const lp = scanPoints.length ? scanPoints[scanPoints.length - 1] : null;
  const lastTxt = lp ? (lp.ch != null ? `M${lp.ch} · ${fmtMHz(lp.f)} MHz` : `${fmtMHz(lp.f)} MHz`) : "";
  $("#scan-prog").textContent = s.running
    ? `running · ${s.index}/${s.total} · ${lastTxt}`
    : s.error ? "Error: " + s.error
    : s.done ? `done · ${s.total} channels` : "ready";
  if (s.running) { scanTimer = setTimeout(pollScan, 400); return; }
  // sweep ended (stopped/done/error) → add the final partial sweep as a row
  if (scanPoints.length && lastWfSweep !== (s.sweep || 0)) {
    scanSweeps.unshift(scanPoints.map(scanLevel));
    if (scanSweeps.length > 60) scanSweeps.pop();
  }
  // clear selection so the band button returns to its normal state.
  scanRunning = false; scanBand = null; updateScanUi(); drawScan();
}

// map a pointer X (client px) to the nearest scanned channel under it
function scanFreqAtX(clientX) {
  if (!scanMeta) return null;
  const cv = $("#scan-canvas"); if (!cv) return null;
  const r = cv.getBoundingClientRect();
  let frac = (clientX - r.left) / r.width;
  frac = Math.max(0, Math.min(1, frac));
  const total = scanMeta.total || 1;
  const idx = Math.max(0, Math.min(total - 1, Math.round(frac * (total - 1))));
  const p = scanPoints[idx];
  const freq = p ? p.f : (scanMeta.start_hz != null ? scanMeta.start_hz + idx * scanMeta.step_hz : 0);
  return { idx, freq, p, xCss: frac * r.width, gw: r.width };
}

function bindScan() {
  $$(".scan-band").forEach(b => b.addEventListener("click", () => onScanBand(b.dataset.band)));
  $("#scan-clear")?.addEventListener("click", clearScan);
  sizeScanCanvas();
  window.addEventListener("resize", sizeScanCanvas);
  // hover readout
  const cv = $("#scan-canvas"), cur = $("#scan-cur"), tip = $("#scan-tip");
  if (cv) {
    cv.addEventListener("mousemove", e => {
      const info = scanFreqAtX(e.clientX);
      if (!info) { cur.hidden = true; tip.hidden = true; return; }
      cur.hidden = false; cur.style.left = info.xCss + "px";
      const occ = info.p ? Math.round(scanNorm(info.p.db) * 100) + "%" : "—";
      tip.hidden = false;
      const fTxt = info.freq ? `${(info.freq / 1e6).toFixed(4)} MHz · ` : "";
      tip.textContent = (info.p && info.p.ch != null ? `M${info.p.ch} · ` : "") + `${fTxt}${occ}`;
      tip.style.left = Math.max(34, Math.min(info.gw - 34, info.xCss)) + "px";
    });
    cv.addEventListener("mouseleave", () => { cur.hidden = true; tip.hidden = true; });
    // double-click tunes the control VFO to the channel under the cursor
    cv.addEventListener("dblclick", e => {
      const info = scanFreqAtX(e.clientX);
      if (!info) return;
      setFreqHz(last?.control_band ?? 0, info.freq);
    });
  }
  // resume if a scan is already running server-side (e.g. after a page reload)
  api("GET", "/api/scan").then(s => {
    if (s && s.running) {
      scanBand = s.band;
      scanMeta = scanMetaFrom(s); lastWfSweep = -1;
      setScanAxis(); scanRunning = true; updateScanUi(); pollScan();
    }
  }).catch(() => {});
}

// ---- collapsible panels ----------------------------------------------------
function bindPanels() {
  $$(".panel-toggle").forEach(btn => {
    const section = btn.closest("section");
    if (!section) return;
    const setState = collapsed => {
      section.classList.toggle("collapsed", collapsed);
      btn.setAttribute("aria-expanded", String(!collapsed));
      btn.textContent = collapsed ? "▲" : "▼";   // down arrow when expanded
    };
    const saved = localStorage.getItem("tmv71.collapse." + section.id);
    if (saved != null) setState(saved === "1");
    btn.addEventListener("click", () => {
      const collapsed = !section.classList.contains("collapsed");
      setState(collapsed);
      localStorage.setItem("tmv71.collapse." + section.id, collapsed ? "1" : "0");
      if (section.id === "hackrf-zone") { if (!collapsed) sizeHrfCanvases(); hrfSync(); }
      else if (section.id === "log-zone") { if (!collapsed) loadLogRecent(); }
      else if (!collapsed) sizeScanCanvas();        // re-measure scan on expand
    });
  });
}

// ---- HackRF waterfall ------------------------------------------------------
const hrf = {
  on: false, mode: "pan", follow: true, lna: 24, vga: 20, amp: false,
  center: null, sweepStart: 144e6, sweepStop: 146e6, sweepCenter: 0,
  ws: null, meta: null, floorEMA: null,   // auto-tracked noise floor (dB)
  level: Number(localStorage.getItem("tmv71.hrf.level") || 50),  // display peak height
  peakHold: localStorage.getItem("tmv71.hrf.peak") !== "0",      // draw a max-hold line (default on)
  smooth: null, peak: null,               // per-bin EMA trace and decaying peak buffers
};
const HRF_PEAK = "#ffcf6a";    // peak-hold line — soft amber, distinct from the live green
const HRF_BLUE = "#3a9ff0";
const HRF_GREEN = "#6fd89a";   // HackRF spectrum trace above the separator (soft green)
// waterfall ramp low→high: blue "water" at the bottom, hot yellow→red for
// strong signals — deep navy → blue → cyan → yellow → red.
const HRF_PAL_DARK  = [[10,14,30],[26,60,150],[40,130,225],[70,205,225],[245,225,70],[220,55,40]];
const HRF_PAL_LIGHT = [[223,230,239],[120,165,222],[40,120,212],[40,178,205],[232,195,55],[208,48,38]];
function hrfHeatRGB(t) {
  t = Math.max(0, Math.min(1, t));
  const s = document.body.classList.contains("theme-light") ? HRF_PAL_LIGHT : HRF_PAL_DARK;
  const x = t * (s.length - 1), i = Math.floor(x), f = x - i;
  const a = s[i], b = s[Math.min(i + 1, s.length - 1)];
  return [Math.round(a[0]+(b[0]-a[0])*f), Math.round(a[1]+(b[1]-a[1])*f), Math.round(a[2]+(b[2]-a[2])*f)];
}
function hrfHeat(t) { const c = hrfHeatRGB(t); return `rgb(${c[0]},${c[1]},${c[2]})`; }

// "Visible" = the body is actually laid out. On desktop a collapsed panel hides
// its body (display:none → offsetHeight 0). In the PWA deck every panel keeps
// the .collapsed class but its body is force-shown, so the class is unreliable.
function hrfVisible() { const b = $("#hrf-body"); return !!(b && b.offsetParent !== null && b.offsetHeight > 0); }
function hrfShouldRun() { return hrf.on && hrfVisible(); }
function hrfStat(msg) { const e = $("#hrf-stat"); if (e) e.textContent = msg; }
function hrfMhz(h) { return (h / 1e6).toFixed(3); }

function hrfControlFreq() {
  try { const b = last?.control_band ?? 0; return last?.bands?.[b]?.rx_freq || null; }
  catch { return null; }
}
function hrfCfg() {
  const c = { mode: hrf.mode, follow: hrf.follow, lna: hrf.lna, vga: hrf.vga, amp: hrf.amp };
  if (hrf.mode === "pan" && !hrf.follow && hrf.center) c.center = hrf.center;
  if (hrf.mode === "sweep") {
    if (hrf.follow) {                       // 20 MHz window centred on the radio
      const cf = hrfControlFreq() || hrf.center || 145e6;
      hrf.sweepCenter = cf;
      c.sweep_start = cf - 10e6; c.sweep_stop = cf + 10e6;
    } else { c.sweep_start = hrf.sweepStart; c.sweep_stop = hrf.sweepStop; }
  }
  return c;
}

async function hrfSync() {
  if (hrfShouldRun() && !hrf.ws) {
    try { await api("POST", "/api/hackrf/start", hrfCfg()); }
    catch (e) { toast("HackRF: " + e.message, "err"); hrf.on = false; updateHrfPower(); return; }
    openHrfWs();
    hrfStat("starting…");
  } else if (!hrfShouldRun() && hrf.ws) {
    closeHrfWs();
    try { await api("POST", "/api/hackrf/stop"); } catch {}
    hrfStat("");
    const cl = $("#hrf-centerline"); if (cl) cl.hidden = true;
  }
}
async function hrfReconfig() {
  if (!hrfShouldRun()) return;
  try { applyHrfStatus(await api("POST", "/api/hackrf/config", hrfCfg())); }
  catch (e) { toast("HackRF: " + e.message, "err"); }
}

function openHrfWs() {
  closeHrfWs();
  const ws = new WebSocket(wsUrl("/ws/hackrf"));
  hrf.ws = ws;
  ws.onmessage = ev => { try { hrfFrame(JSON.parse(ev.data)); } catch {} };
  ws.onclose = () => { if (hrf.ws === ws) hrf.ws = null; };
  ws.onerror = () => {};
}
function closeHrfWs() { if (hrf.ws) { try { hrf.ws.close(); } catch {} hrf.ws = null; } }

function hrfFrame(m) {
  if (m.t === "status") { applyHrfStatus(m); return; }
  if (m.t !== "pan" && m.t !== "sweep") return;       // idle/ping
  hrf.meta = m;
  hrfUpdateFloor(m.db);
  drawHrfWaterfall(m.db);
  drawHrfSpectrum(m.db);
  setHrfAxis(m);
  updateHrfCenterline(m);
  hrfStat(`${m.t === "pan" ? "PAN" : "SWEEP"} · ${hrfMhz(m.center)} MHz`);
  if (m.t === "pan" && hrf.follow) {
    const inp = $("#hrf-center-in");
    if (inp && document.activeElement !== inp) inp.value = hrfMhz(m.center);
  }
}

function applyHrfStatus(s) {
  if (!s) return;
  if (s.center) hrf.center = s.center;
  if (s.error) hrfStat("⚠ " + s.error);
}

// Track the noise floor (slow EMA of a low percentile) so it sits at the bottom
// of the waterfall in any RF/gain condition.
function hrfUpdateFloor(db) {
  const s = [...db].sort((a, b) => a - b);
  const p = s[Math.floor(s.length * 0.10)];
  hrf.floorEMA = hrf.floorEMA == null ? p : hrf.floorEMA * 0.93 + p * 0.07;
}
// dB→0..1 with the auto floor at the bottom; the LEVEL slider sets the dB span
// above it (= peak height / contrast): more level → smaller span → taller peaks.
function hrfNorm() {
  const floor = hrf.floorEMA == null ? -60 : hrf.floorEMA;
  const dr = Math.max(8, 80 - (hrf.level / 100) * 70);   // level 0→80 dB … 100→10 dB
  return v => Math.max(0, Math.min(1, (v - floor) / dr));
}

function sizeHrfCanvases() {
  const dpr = window.devicePixelRatio || 1;
  ["#hrf-spectrum", "#hrf-wf"].forEach(sel => {
    const cv = $(sel); if (!cv) return;
    const r = cv.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width * dpr));
    const h = Math.max(1, Math.round(r.height * dpr));
    // Assigning width/height clears the canvas — only do it on a real size
    // change, so navigating away and back (re-measuring the visible panel)
    // keeps the accumulated waterfall instead of wiping it.
    if (cv.width !== w) cv.width = w;
    if (cv.height !== h) cv.height = h;
  });
}

let hrfRowCv = null;        // offscreen 1-row buffer (bin resolution)
function drawHrfWaterfall(db) {
  const cv = $("#hrf-wf"); if (!cv) return;
  const ctx = cv.getContext("2d"), W = cv.width;
  const dpr = window.devicePixelRatio || 1;
  // sweeps land ~once every several seconds, so draw them as fat rows that
  // visibly advance; the panadapter streams fast, so thin rows.
  const sweep = hrf.meta && hrf.meta.t === "sweep";
  const rowH = Math.max(1, Math.round((sweep ? 8 : 2) * dpr));
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(cv, 0, rowH);                          // crisp integer scroll down
  // paint the new row at bin resolution into an offscreen strip…
  const n = db.length, norm = hrfNorm();
  if (!hrfRowCv || hrfRowCv.width !== n) {
    hrfRowCv = document.createElement("canvas"); hrfRowCv.width = n; hrfRowCv.height = 1;
  }
  const rctx = hrfRowCv.getContext("2d"), img = rctx.createImageData(n, 1), d = img.data;
  for (let i = 0; i < n; i++) {
    const c = hrfHeatRGB(norm(db[i])), o = i * 4;
    d[o] = c[0]; d[o + 1] = c[1]; d[o + 2] = c[2]; d[o + 3] = 255;
  }
  rctx.putImageData(img, 0, 0);
  // …then stretch it across the canvas with interpolation → smooth, not blocky
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(hrfRowCv, 0, 0, n, 1, 0, 0, W, rowH);
}

function drawHrfSpectrum(db) {
  const cv = $("#hrf-spectrum"); if (!cv) return;
  const ctx = cv.getContext("2d"), W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = "rgba(127,140,150,0.12)"; ctx.lineWidth = 1;
  for (let g = 1; g < 3; g++) { const y = Math.round(H * g / 3) + 0.5; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
  const norm = hrfNorm(), n = db.length;
  const yOf = v => H - norm(v) * (H - 2);

  // Temporal smoothing calms the fast, jittery panadapter trace (EMA per bin).
  // Sweep frames are slow and already discrete, so leave those raw.
  const pan = hrf.meta && hrf.meta.t === "pan";
  let trace = db;
  if (pan) {
    if (!hrf.smooth || hrf.smooth.length !== n) hrf.smooth = Float32Array.from(db);
    else { const S = 0.62; for (let i = 0; i < n; i++) hrf.smooth[i] = hrf.smooth[i] * S + db[i] * (1 - S); }
    trace = hrf.smooth;
  } else hrf.smooth = null;

  // filled area + live line (from the smoothed trace)
  ctx.beginPath(); ctx.moveTo(0, H);
  for (let i = 0; i < n; i++) ctx.lineTo(i / (n - 1) * W, yOf(trace[i]));
  ctx.lineTo(W, H); ctx.closePath();
  ctx.fillStyle = "rgba(111,216,154,0.18)"; ctx.fill();
  ctx.beginPath(); ctx.moveTo(0, yOf(trace[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(i / (n - 1) * W, yOf(trace[i]));
  ctx.strokeStyle = HRF_GREEN; ctx.lineWidth = 1.2 * dprOf(); ctx.stroke();

  // optional peak hold: max of the raw level per bin, decaying slowly each frame
  if (hrf.peakHold) {
    if (!hrf.peak || hrf.peak.length !== n) hrf.peak = Float32Array.from(db);
    else { const DEC = 0.5; for (let i = 0; i < n; i++) hrf.peak[i] = db[i] > hrf.peak[i] ? db[i] : hrf.peak[i] - DEC; }
    ctx.beginPath(); ctx.moveTo(0, yOf(hrf.peak[0]));
    for (let i = 1; i < n; i++) ctx.lineTo(i / (n - 1) * W, yOf(hrf.peak[i]));
    ctx.strokeStyle = HRF_PEAK; ctx.lineWidth = 1 * dprOf(); ctx.stroke();
  } else hrf.peak = null;
}
function dprOf() { return window.devicePixelRatio || 1; }

function setHrfAxis(m) {
  $("#hrf-f0").textContent = hrfMhz(m.f0);
  $("#hrf-fc").textContent = hrfMhz(m.center);
  $("#hrf-f1").textContent = hrfMhz(m.f1);
}

// vertical marker at the currently tuned radio frequency (centre in follow mode)
function updateHrfCenterline(m) {
  const el = $("#hrf-centerline"); if (!el) return;
  const cf = hrfControlFreq();
  if (!cf || !m || m.f1 <= m.f0) { el.hidden = true; return; }
  const frac = (cf - m.f0) / (m.f1 - m.f0);
  if (frac < 0 || frac > 1) { el.hidden = true; return; }
  el.hidden = false; el.style.left = (frac * 100).toFixed(2) + "%";
}

function hrfFreqAtX(clientX) {
  const m = hrf.meta; if (!m) return null;
  const g = $(".hrf-graph"); if (!g) return null;
  const r = g.getBoundingClientRect();
  let frac = (clientX - r.left) / r.width;
  frac = Math.max(0, Math.min(1, frac));
  const n = m.db.length, idx = Math.max(0, Math.min(n - 1, Math.round(frac * (n - 1))));
  return { freq: Math.round(m.f0 + frac * (m.f1 - m.f0)), db: m.db[idx],
           xCss: frac * r.width, gw: r.width };
}

function updateHrfPower() {
  const b = $("#hrf-power"); if (!b) return;
  b.textContent = hrf.on ? "ON" : "OFF";
  b.setAttribute("aria-pressed", String(hrf.on));
  $("#hackrf-zone")?.classList.toggle("hrf-off", !hrf.on);   // dim the panel when off
}
function updateHrfModeUi() {
  $$(".hrf-m").forEach(x => x.classList.toggle("active", x.dataset.mode === hrf.mode));
  $$(".hrf-pan-only").forEach(x => x.hidden = hrf.mode !== "pan");
  $$(".hrf-sweep-only").forEach(x => x.hidden = hrf.mode !== "sweep");
  const ci = $("#hrf-center-in"); if (ci) ci.disabled = (hrf.mode === "pan" && hrf.follow);
}

function bindHackRF() {
  if (!$("#hackrf-zone")) return;
  // availability check — show "HackRF ready" when a device is connected,
  // otherwise disable the power button.
  api("GET", "/api/hackrf").then(s => {
    const present = s && s.available !== false && s.detected !== false;
    const ready = $("#hrf-ready");
    if (present) {
      if (ready) ready.hidden = false;
    } else {
      if (ready) ready.hidden = true;
      hrfStat(s && s.available === false ? "hackrf tools missing" : "no HackRF detected");
      $("#hrf-power").disabled = true;
    }
  }).catch(() => {});

  $("#hrf-power")?.addEventListener("click", () => { hrf.on = !hrf.on; updateHrfPower(); hrfSync(); });
  $$(".hrf-m").forEach(b => b.addEventListener("click", () => {
    hrf.mode = b.dataset.mode; updateHrfModeUi();
    if (hrfShouldRun()) hrfReconfig(); }));
  $("#hrf-follow")?.addEventListener("change", e => {
    hrf.follow = e.target.checked; updateHrfModeUi(); hrfReconfig(); });
  $("#hrf-amp")?.addEventListener("change", e => { hrf.amp = e.target.checked; hrfReconfig(); });
  const peakCb = $("#hrf-peak");
  if (peakCb) {
    peakCb.checked = hrf.peakHold;
    peakCb.addEventListener("change", e => {
      hrf.peakHold = e.target.checked;
      hrf.peak = null;                                  // restart the hold from the next frame
      localStorage.setItem("tmv71.hrf.peak", hrf.peakHold ? "1" : "0");
      if (hrf.meta) drawHrfSpectrum(hrf.meta.db);       // reflect immediately
    });
  }
  // colored track fill (--gpct) like the audio-panel sliders
  const fillRange = el => { if (!el) return; const lo = +el.min || 0, hi = +el.max || 100;
    el.style.setProperty("--gpct", ((el.value - lo) / (hi - lo) * 100) + "%"); };
  const lna = $("#hrf-lna"), vga = $("#hrf-vga");
  lna?.addEventListener("input", () => { $("#hrf-lna-v").textContent = lna.value; fillRange(lna); });
  vga?.addEventListener("input", () => { $("#hrf-vga-v").textContent = vga.value; fillRange(vga); });
  lna?.addEventListener("change", () => { hrf.lna = Number(lna.value); hrfReconfig(); });
  vga?.addEventListener("change", () => { hrf.vga = Number(vga.value); hrfReconfig(); });
  fillRange(lna); fillRange(vga);
  const lvl = $("#hrf-level");
  if (lvl) {
    lvl.value = hrf.level; $("#hrf-level-v").textContent = hrf.level; fillRange(lvl);
    lvl.addEventListener("input", () => {            // display-only: no radio restart
      hrf.level = Number(lvl.value);
      $("#hrf-level-v").textContent = lvl.value;
      fillRange(lvl);
      localStorage.setItem("tmv71.hrf.level", lvl.value);
      if (hrf.meta) drawHrfSpectrum(hrf.meta.db);    // live preview on the spectrum line
    });
  }
  $("#hrf-center-in")?.addEventListener("change", e => {
    const mhz = parseFloat(e.target.value);
    if (isFinite(mhz)) { hrf.center = Math.round(mhz * 1e6); hrfReconfig(); }
  });
  $("#hrf-range-in")?.addEventListener("change", e => {
    const m = e.target.value.match(/(\d+(?:\.\d+)?)\s*[-–:]\s*(\d+(?:\.\d+)?)/);
    if (m) { hrf.sweepStart = Math.round(parseFloat(m[1]) * 1e6); hrf.sweepStop = Math.round(parseFloat(m[2]) * 1e6); hrfReconfig(); }
  });
  // hover readout (frequency + dB) over the whole graph; double-click → VFO
  const cv = $(".hrf-graph"), cur = $("#hrf-cur"), tip = $("#hrf-tip");
  if (cv) {
    cv.addEventListener("mousemove", e => {
      const info = hrfFreqAtX(e.clientX);
      if (!info) { cur.hidden = tip.hidden = true; return; }
      cur.hidden = false; cur.style.left = info.xCss + "px";
      tip.hidden = false;
      tip.textContent = `${hrfMhz(info.freq)} MHz · ${Math.round(info.db)} dB`;
      tip.style.left = Math.max(48, Math.min(info.gw - 48, info.xCss)) + "px";
    });
    cv.addEventListener("mouseleave", () => { cur.hidden = tip.hidden = true; });
    cv.addEventListener("dblclick", e => {
      const info = hrfFreqAtX(e.clientX);
      if (!info) return;
      setFreqHz(last?.control_band ?? 0, info.freq);
      toast(`VFO → ${hrfMhz(info.freq)} MHz`, "ok");
    });
  }
  window.addEventListener("resize", sizeHrfCanvases);
  updateHrfPower(); updateHrfModeUi();
  if (hrfVisible()) sizeHrfCanvases();
}

// ---- theme (hell / dunkel) ------------------------------------------------
function applyTheme(light, persist = true) {
  document.body.classList.toggle("theme-light", light);
  // (color-scheme is declared statically as "light dark" in CSS so the WebView
  // never force-darkens; do NOT pin it to one value here.)
  const tc = document.querySelector('meta[name="theme-color"]');
  if (tc) tc.setAttribute("content", light ? "#f4f7fa" : "#0c1115");
  const btn = $("#theme-toggle");
  if (btn) {                              // show the mode you'd switch TO
    btn.textContent = light ? "☾" : "☀";
    btn.setAttribute("aria-pressed", String(light));
    btn.title = light ? "Switch to dark" : "Switch to light";
  }
  const name = light ? "light" : "dark";
  localStorage.setItem("tmv71.theme", name);   // local cache → no flash on reload
  // persist server-side so the choice survives across browsers/devices
  if (persist) api("POST", "/api/theme", { theme: name }).catch(() => {});
  // recolor the canvases that use a theme-dependent palette
  try { drawScan(); drawLevelGraph(); } catch {}
}
function bindTheme() {
  // apply the cached theme at once (avoids a flash), then sync the
  // server-persisted choice without re-saving it.
  applyTheme(localStorage.getItem("tmv71.theme") !== "dark", false);   // light is the default
  api("GET", "/api/theme").then(r => {
    if (r && (r.theme === "light" || r.theme === "dark")) applyTheme(r.theme === "light", false);
  }).catch(() => {});
  $("#theme-toggle").addEventListener("click",
    () => applyTheme(!document.body.classList.contains("theme-light")));
}

// ---- mobile panel deck (swipe + bottom tab bar) ---------------------------
// On phones the console becomes a horizontal scroll-snap "deck": one panel per
// screen. The bottom tab bar reflects and drives the current panel. Desktop is
// untouched (the deck CSS only applies under the mobile breakpoint).
function bindPanelDeck() {
  const deck = document.querySelector(".console");
  const tabs = $$(".panel-tabs .ptab");
  if (!deck || !tabs.length) return;
  const mq = window.matchMedia("(max-width:760px)");
  const targets = tabs.map(t => document.querySelector(t.dataset.target));

  tabs.forEach((t, i) => t.addEventListener("click", () => {
    const el = targets[i];
    if (el) deck.scrollTo({ top: el.offsetTop, behavior: "smooth" });
  }));

  const setActive = i => tabs.forEach((t, k) => t.classList.toggle("active", k === i));

  // a panel becoming visible: light its tab + re-measure its canvas (a canvas
  // laid out while its panel was off-screen can come up zero-sized)
  const onShown = el => {
    try {
      if (el.matches(".audio")) sizeLevelGraph();
      else if (el.matches(".hackrf-zone")) { sizeHrfCanvases(); hrfSync(); }
      else if (el.matches(".log-zone")) loadLogRecent();
      else if (el.matches(".scan-zone")) sizeScanCanvas();
    } catch {}
  };

  const io = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting && e.intersectionRatio >= 0.5) {
        const i = targets.indexOf(e.target);
        if (i >= 0) setActive(i);
        onShown(e.target);
      }
    });
  }, { root: deck, threshold: [0.5, 0.75] });

  let observing = false;
  const sync = () => {
    if (mq.matches && !observing) {
      targets.forEach(el => el && io.observe(el)); observing = true; setActive(0);
    } else if (!mq.matches && observing) {
      targets.forEach(el => el && io.unobserve(el)); observing = false;
    }
  };
  mq.addEventListener("change", sync);
  sync();
}

// Keep the phone screen awake while the app is open: the radio remote needs to
// stay visible (PTT, meters, waterfall). The wake lock is released by the
// browser whenever the page is hidden, so re-acquire it on visibility change.
let wakeLock = null;
async function requestWakeLock() {
  if (!("wakeLock" in navigator) || document.visibilityState !== "visible") return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => { wakeLock = null; });
  } catch { /* denied / unsupported / not focused — ignore */ }
}
function bindWakeLock() {
  requestWakeLock();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !wakeLock) requestWakeLock();
  });
}

// Best-effort lock to landscape. The manifest already forces it for the
// installed PWA; this covers the fullscreen case. Throws in a normal browser
// tab (no permission) — ignored; the CSS rotate hint handles portrait there.
function lockLandscape() {
  try { screen.orientation?.lock?.("landscape").catch(() => {}); } catch {}
}

// Fade the launch/splash screen once the app has booted.
function hideLaunch() {
  const l = $("#launch");
  if (!l || l.classList.contains("gone")) return;
  l.classList.add("gone");
  setTimeout(() => l.remove(), 600);
}

// Radio-off hint: shown when the GPIO power switch is available and the radio is
// off. Tapping it (not the ✕) powers the radio on. Dismiss is remembered until
// the radio next comes on.
function updateRadioHint() {
  const el = $("#radio-hint");
  if (!el) return;
  const off = !!(powerState && powerState.available && powerState.on === false);
  if (!off) el.dataset.dismissed = "";          // re-arm once it's on
  el.hidden = !off || el.dataset.dismissed === "1";
}
function bindRadioHint() {
  const el = $("#radio-hint");
  if (!el) return;
  // informational only — switching on is done with the power button, not here
  $("#radio-hint-x")?.addEventListener("click", () => {
    el.dataset.dismissed = "1"; el.hidden = true;
  });
}

// Hide/show the title bar (mobile) to give the panels full height. Persisted.
function bindHeaderHide() {
  const set = on => {
    document.body.classList.toggle("hdr-collapsed", on);
    localStorage.setItem("tmv71.hdrCollapsed", on ? "1" : "0");
  };
  $("#hdr-hide")?.addEventListener("click", () => set(true));
  $("#hdr-reveal")?.addEventListener("click", () => set(false));
  if (localStorage.getItem("tmv71.hdrCollapsed") === "1") set(true);
}

// ---- digimodes (CW / RTTY) ------------------------------------------------
function bindDigi() {
  const decode = $("#digi-decode");
  if (!decode) return;
  const post = async body => {
    try { return await api("POST", "/api/digi/config", body); }
    catch (e) { toast("Digi: " + e.message, "err"); }
  };
  const fill = el => {
    const pct = (el.value - el.min) / (el.max - el.min) * 100;
    el.style.setProperty("--gpct", pct + "%");
  };
  // mode toggle
  $$(".digi-mode").forEach(b => b.addEventListener("click", () => {
    const mode = b.dataset.mode;
    $$(".digi-mode").forEach(x => x.classList.toggle("active", x === b));
    $("#digi-params-cw").hidden = mode !== "cw";
    $("#digi-params-rtty").hidden = mode !== "rtty";
    $("#digi-params-pocsag").hidden = mode !== "pocsag";
    const dh = $(".digi-hint-pocsag"); if (dh) dh.hidden = mode !== "pocsag";
    post({ mode });
  }));
  // range params (value label + fill + server update on release)
  const range = (id, vid, suffix, key) => {
    const el = $(id), v = $(vid);
    if (!el) return;
    const upd = () => { v.textContent = el.value + suffix; fill(el); };
    el.addEventListener("input", upd);
    el.addEventListener("change", () => post({ [key]: Number(el.value) }));
    upd();
  };
  range("#digi-wpm", "#digi-wpm-v", "", "cw_wpm");
  range("#digi-pitch", "#digi-pitch-v", " Hz", "cw_pitch");
  $("#digi-auto")?.addEventListener("change", e => {
    post({ cw_auto: e.target.checked });
    toast(e.target.checked ? "CW auto-WPM on" : "CW auto-WPM off", "ok");
  });
  range("#digi-mark", "#digi-mark-v", " Hz", "rtty_mark");
  $("#digi-baud")?.addEventListener("change", e => post({ rtty_baud: Number(e.target.value) }));
  $("#digi-shift")?.addEventListener("change", e => post({ rtty_shift: Number(e.target.value) }));
  // POCSAG params
  $("#pocsag-baud")?.addEventListener("change", e => post({ pocsag_baud: Number(e.target.value) }));
  $("#pocsag-func")?.addEventListener("change", e => post({ pocsag_func: Number(e.target.value) }));
  $("#pocsag-alpha")?.addEventListener("change", e => post({ pocsag_alpha: e.target.checked }));
  $("#pocsag-all")?.addEventListener("change", e => {
    post({ pocsag_listen_all: e.target.checked });
    toast(e.target.checked ? "POCSAG: listen to all RICs" : "POCSAG: filter to RIC", "ok");
  });
  $("#pocsag-addr")?.addEventListener("change", e => {
    const v = Math.max(0, Math.min(2097151, parseInt(e.target.value, 10) || 0));
    e.target.value = String(v); post({ pocsag_addr: v });
  });
  // decoder on/off
  $("#digi-rx")?.addEventListener("change", e => {
    post({ rx: e.target.checked });
    toast(e.target.checked ? "Decoder on" : "Decoder off", e.target.checked ? "ok" : "");
  });
  $("#digi-clear")?.addEventListener("click", () => { decode.textContent = ""; });
  // transmit
  const sendBtn = $("#digi-send"), txt = $("#digi-text");
  const send = async () => {
    const t = txt.value.trim();
    if (!t) return;
    sendBtn.disabled = true; sendBtn.classList.add("tx"); sendBtn.textContent = "SENDING…";
    try { const r = await api("POST", "/api/digi/tx", { text: t }); if (r && r.sent) txt.value = ""; }
    catch (e) { toast("Send: " + e.message, "err"); }
    finally { sendBtn.disabled = false; sendBtn.classList.remove("tx"); sendBtn.textContent = "SEND"; }
  };
  sendBtn?.addEventListener("click", send);
  txt?.addEventListener("keydown", e => { if (e.key === "Enter") send(); });
  // reflect persisted server state
  api("GET", "/api/digi").then(s => {
    if (!s) return;
    $$(".digi-mode").forEach(x => x.classList.toggle("active", x.dataset.mode === s.mode));
    $("#digi-params-cw").hidden = s.mode !== "cw";
    $("#digi-params-rtty").hidden = s.mode !== "rtty";
    $("#digi-params-pocsag").hidden = s.mode !== "pocsag";
    { const dh = $(".digi-hint-pocsag"); if (dh) dh.hidden = s.mode !== "pocsag"; }
    const set = (id, val, vid, suffix) => {
      const el = $(id); if (!el) return;
      el.value = val; fill(el);
      if (vid) $(vid).textContent = val + (suffix || "");
    };
    set("#digi-wpm", s.cw_wpm, "#digi-wpm-v", "");
    set("#digi-pitch", s.cw_pitch, "#digi-pitch-v", " Hz");
    set("#digi-mark", s.rtty_mark, "#digi-mark-v", " Hz");
    if ($("#digi-baud")) $("#digi-baud").value = String(s.rtty_baud);
    if ($("#digi-shift")) $("#digi-shift").value = String(s.rtty_shift);
    if ($("#pocsag-baud") && s.pocsag_baud != null) $("#pocsag-baud").value = String(s.pocsag_baud);
    if ($("#pocsag-addr") && s.pocsag_addr != null) $("#pocsag-addr").value = String(s.pocsag_addr);
    if ($("#pocsag-func") && s.pocsag_func != null) $("#pocsag-func").value = String(s.pocsag_func);
    if ($("#pocsag-alpha")) $("#pocsag-alpha").checked = !!s.pocsag_alpha;
    if ($("#pocsag-all") && s.pocsag_listen_all != null) $("#pocsag-all").checked = !!s.pocsag_listen_all;
    if ($("#digi-rx")) $("#digi-rx").checked = !!s.rx;
    if ($("#digi-auto")) $("#digi-auto").checked = s.cw_auto !== false;
  }).catch(() => {});
  connectDigiWS(decode);
}

function connectDigiWS(decode) {
  let ws;
  try { ws = new WebSocket(wsUrl("/ws/digi")); } catch { return; }
  ws.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === "rx" && m.text) {
      let text = m.text;
      // POCSAG pages arrive as complete lines — prefix each with a timestamp
      if (document.querySelector(".digi-mode.active")?.dataset.mode === "pocsag") {
        const ts = new Date().toLocaleTimeString([], { hour12: false });
        text = text.split("\n").map(l => (l ? ts + "  " + l : l)).join("\n");
      }
      decode.textContent += text;
      if (decode.textContent.length > 4000) decode.textContent = decode.textContent.slice(-3000);
      decode.scrollTop = decode.scrollHeight;
    }
  };
  ws.onclose = () => setTimeout(() => connectDigiWS(decode), 2500);
  ws.onerror = () => { try { ws.close(); } catch {} };
}

// ---- selcall (classic 5-tone selective calling) ---------------------------
function bindSelcall() {
  const decode = $("#sel-decode");
  if (!decode) return;
  const post = async b => {
    try { return await api("POST", "/api/selcall/config", b); }
    catch (e) { toast("Selcall: " + e.message, "err"); }
  };
  const digitsOf = sel => $$(sel + " .sel-d").map(i => i.value).join("");
  const setDigits = (sel, str) => $$(sel + " .sel-d").forEach((el, i) => { el.value = str[i] || ""; });
  // auto-advancing 5-digit group
  const wire = (sel, onChange) => {
    const els = $$(sel + " .sel-d");
    els.forEach((el, i) => {
      el.addEventListener("input", () => {
        el.value = el.value.replace(/[^0-9]/g, "").slice(0, 1);
        if (el.value && i < els.length - 1) els[i + 1].focus();
        if (onChange) onChange();
      });
      el.addEventListener("keydown", e => {
        if (e.key === "Backspace" && !el.value && i > 0) els[i - 1].focus();
      });
    });
  };
  wire("#sel-code", () => post({ code: digitsOf("#sel-code") }));
  wire("#sel-own", () => post({ own: digitsOf("#sel-own") }));
  $("#sel-standard")?.addEventListener("change", e => post({ standard: e.target.value }));
  $("#sel-tone")?.addEventListener("change", e => post({ tone_ms: Number(e.target.value) }));
  $("#sel-rx")?.addEventListener("change", e => {
    post({ rx: e.target.checked });
    toast(e.target.checked ? "Selcall decode on" : "Selcall decode off", e.target.checked ? "ok" : "");
  });
  // CALL (transmit)
  const callBtn = $("#sel-call");
  callBtn?.addEventListener("click", async () => {
    const code = digitsOf("#sel-code");
    if (code.length < 5) { toast("Enter 5 digits", "err"); return; }
    callBtn.disabled = true; callBtn.classList.add("tx"); callBtn.textContent = "…";
    try { await api("POST", "/api/selcall/tx", { code }); toast("Called " + code, "ok"); }
    catch (e) { toast("Call: " + e.message, "err"); }
    finally { callBtn.disabled = false; callBtn.classList.remove("tx"); callBtn.textContent = "CALL"; }
  });
  // MUTE until call: mutes RX audio; releases when the own code (or any, if no
  // own set) is decoded. Arming also turns the decoder on.
  const muteBtn = $("#sel-mute");
  let muted = false;
  const setMute = on => {
    muted = on;
    selcallMuted = on;
    const a = $("#rx-audio"); if (a) a.muted = on || micTestActive;
    muteBtn.classList.toggle("armed", on);
    muteBtn.textContent = on ? "MUTED" : "MUTE";
    if (on && $("#sel-rx") && !$("#sel-rx").checked) { $("#sel-rx").checked = true; post({ rx: true }); }
  };
  muteBtn?.addEventListener("click", () => setMute(!muted));
  // reflect server state
  api("GET", "/api/selcall").then(s => {
    if (!s) return;
    if ($("#sel-standard")) $("#sel-standard").value = s.standard;
    if ($("#sel-tone")) $("#sel-tone").value = String(Math.round(s.tone_ms || 70));
    if ($("#sel-rx")) $("#sel-rx").checked = !!s.rx;
    if (s.own) setDigits("#sel-own", s.own);
    if (s.code) setDigits("#sel-code", s.code);
  }).catch(() => {});
  connectSelcallWS(decode, () => muted, setMute);
}

function connectSelcallWS(decode, isMuted, setMute) {
  let ws;
  try { ws = new WebSocket(wsUrl("/ws/selcall")); } catch { return; }
  ws.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === "call" && m.code) {
      const own = $$("#sel-own .sel-d").map(i => i.value).join("");
      const release = m.mine || own.length < 5;     // no own set -> any call releases
      const line = document.createElement("div");
      if (m.mine) line.className = "mine";
      line.textContent = (m.mine ? "► " : "") + m.code;
      decode.appendChild(line);
      decode.scrollTop = decode.scrollHeight;
      while (decode.childElementCount > 50) decode.removeChild(decode.firstChild);
      if (isMuted() && release) { setMute(false); toast("Call received: " + m.code, "ok"); }
    }
  };
  ws.onclose = () => setTimeout(() => connectSelcallWS(decode, isMuted, setMute), 2500);
  ws.onerror = () => { try { ws.close(); } catch {} };
}

// ---- callsign auto-detect (Vosk ASR) --------------------------------------
function reflectAsr(s) {
  if (!s) return;
  const tgl = $("#set-asr-callsign");
  if (tgl) { tgl.checked = !!s.enabled; tgl.disabled = !s.available; }
  const rc = $("#rx-call");            // field + ASR-active tag only while detection is on
  if (rc) { rc.hidden = !s.enabled; if (!s.enabled) rc.textContent = ""; }
  const live = $("#asr-live");
  if (live) live.hidden = !s.enabled;
  document.body.classList.toggle("asr-on", !!s.enabled);   // PWA: drop nameplate, rotate ASR
  const hint = $("#asr-hint");
  if (hint && !s.available)
    hint.textContent = "Vosk model not found on the Pi — install vosk-model-small-de-0.15 under models/ to enable callsign detection.";
}

function showRxCall(m) {
  const chip = $("#rx-call");
  if (!chip) return;
  chip.textContent = m.call;
  const info = [m.name, m.qth || m.state, m.country].filter(Boolean).join(" · ");
  chip.title = (info ? m.call + " — " + info : m.call + " (auto-detected)") + " · click to clear";
  chip.classList.remove("flash"); void chip.offsetWidth; chip.classList.add("flash");   // restart flash
}

function bindCallsign() {
  const chip = $("#rx-call");
  if (chip) chip.addEventListener("click", () => {
    chip.textContent = ""; chip.classList.remove("flash");
    chip.title = "Auto-detected callsign (none yet)";
  });
  const tgl = $("#set-asr-callsign");
  if (tgl) tgl.addEventListener("change", async e => {
    const on = e.target.checked;
    try {
      reflectAsr(await api("POST", "/api/asr/config", { enabled: on }));
      toast(on ? "Callsign detect on" : "Callsign detect off", on ? "ok" : "");
    } catch (err) {
      e.target.checked = !on;                         // revert on failure
      toast("Callsign detect: " + err.message, "err");
    }
  });
  connectCallsignWS();
}

function connectCallsignWS() {
  let ws;
  try { ws = new WebSocket(wsUrl("/ws/callsign")); } catch { return; }
  ws.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === "status") { reflectAsr(m); return; }
    if (m.t === "callsign" && m.call) {
      showRxCall(m);
      const info = [m.name, m.qth || m.state, m.country].filter(Boolean).join(" · ");
      toast(info ? `📻 ${m.call} · ${info}` : `📻 ${m.call} heard`, "ok");
    }
  };
  ws.onclose = () => setTimeout(connectCallsignWS, 2500);
  ws.onerror = () => { try { ws.close(); } catch {} };
}

// ---- logbook (Wavelog) ----------------------------------------------------
const logEsc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let logStationWanted = "";   // station id to preselect once profiles load
let lastLookup = null;       // cached lookup result {call, ...} for the log form

function renderLogRecent(data) {
  const onl = $("#log-online");
  if (onl) {
    onl.hidden = !data.enabled;            // only relevant once Wavelog is set up
    onl.classList.toggle("on", !!data.online);
    onl.title = data.online ? "Wavelog connected" : "Wavelog not reachable";
  }
  const sEl = $("#log-stats");
  if (sEl) {
    const s = data.stats || {};
    // Wavelog's statistics keys vary by version (e.g. Today / month_qsos /
    // year_qsos / total_qsos) — map the known dashboard counters, else show raw
    const pick = (...ks) => { for (const k of ks) if (s[k] != null) return s[k]; return null; };
    let entries = [
      ["Today", pick("Today", "today_qsos", "todays_qsos", "today")],
      ["Month", pick("Month", "month_qsos", "months_qsos")],
      ["Year", pick("Year", "year_qsos", "years_qsos")],
      ["Total", pick("Total", "total_qsos", "total")],
    ].filter(([, v]) => v != null);
    if (!entries.length)
      entries = Object.entries(s).filter(([, v]) => v != null && typeof v !== "object");
    const parts = entries.map(([l, v]) => `${logEsc(l)} <b>${logEsc(v)}</b>`);
    sEl.innerHTML = parts.join(" &nbsp;·&nbsp; ");
    sEl.hidden = !parts.length;
  }
  const r = data.recent || [];
  const clr = $("#log-clear"); if (clr) clr.hidden = !r.length;
  // most recent QSO, shown in the panel title bar
  const lastEl = $("#log-last");
  if (lastEl) {
    if (r.length) {
      const e = r[0];
      const ok = e.targets && Object.values(e.targets).some(Boolean);
      // no band here — it makes the title row wrap; only stat · call · name
      lastEl.innerHTML = `<span class="ll-stat ${ok ? "ok" : "err"}">${ok ? "✓" : "✕"}</span>` +
        `<span class="ll-call">${logEsc(e.call)}</span>` +
        (e.name ? `<span class="ll-meta">${logEsc(e.name)}</span>` : "");
      lastEl.hidden = false;
    } else { lastEl.innerHTML = ""; lastEl.hidden = true; }
  }
  const list = $("#log-recent"); if (!list) return;
  if (!r.length) { list.innerHTML = '<p class="log-empty">No logged QSOs yet.</p>'; return; }
  list.innerHTML = r.map(e => {
    const t = String(e.ts || "").replace("T", " ").slice(5, 16);   // MM-DD HH:MM
    const ok = e.targets && Object.values(e.targets).some(Boolean);
    const meta = [e.band, e.mode, e.freq_mhz ? `${e.freq_mhz} MHz` : ""]
      .filter(Boolean).join(" · ");
    const extra = [e.gridsquare, e.qth, e.email, e.comment].filter(Boolean).join(" · ");
    return `<div class="log-item">` +
      `<span class="li-when"><span class="li-dt">${logEsc(t)}</span>${meta ? " · " + logEsc(meta) : ""}</span>` +
      `<span class="li-extra" title="${logEsc(extra)}">${logEsc(extra)}</span>` +
      `<span class="li-name">${logEsc(e.name || "")}</span>` +
      `<span class="li-call">${logEsc(e.call)}</span>` +
      `<span class="li-stat ${ok ? "ok" : "err"}" title="${ok ? "logged" : "failed"}">${ok ? "✓" : "✕"}</span>` +
      `<button type="button" class="li-del" data-ts="${logEsc(e.ts || "")}" title="Delete this entry" aria-label="Delete">✕</button>` +
      `</div>`;
  }).join("");
}

async function loadLogRecent() {
  try { renderLogRecent(await api("GET", "/api/log/recent")); } catch {}
}

async function deleteLogEntry(ts) {
  if (!ts) return;
  try { await api("POST", "/api/log/recent/delete", { ts }); loadLogRecent(); }
  catch (e) { toast("Delete: " + e.message, "err"); }
}

async function clearLogRecent() {
  if (!await confirmDialog("Clear the recent list? (does not delete the QSOs in Wavelog)",
        { title: "Clear recent", okText: "CLEAR", danger: true })) return;
  try { await api("POST", "/api/log/recent/clear"); loadLogRecent(); }
  catch (e) { toast("Clear: " + e.message, "err"); }
}

async function logLookup() {
  const inp = $("#log-call"); if (!inp) return;
  const call = inp.value.trim().toUpperCase();
  if (!call) { toast("Enter a callsign", "err"); return; }
  inp.value = call;
  try {
    const r = await api("POST", "/api/log/lookup", { callsign: call });
    lastLookup = { ...r, call };          // cache for the QSO we're about to log
    if (r.name && !$("#log-name").value.trim()) $("#log-name").value = r.name;
    if (r.gridsquare && !$("#log-grid").value.trim()) $("#log-grid").value = r.gridsquare;
    const bits = [r.name, r.gridsquare, r.qth, r.country, r.email, r.dxcc]
      .filter(Boolean).join(" · ");
    toast(bits ? `${call}: ${bits}${r.worked_before ? " · worked before" : ""}`
               : `${call}: no data`, bits ? "ok" : "");
  } catch (e) { toast("Lookup: " + e.message, "err"); }
}

async function logQso() {
  const call = $("#log-call").value.trim().toUpperCase();
  if (!call) { toast("Enter a callsign", "err"); return; }
  const btn = $("#log-save"); btn.disabled = true;
  try {
    // make sure the lookup for this callsign has finished (a click on LOG QSO
    // can fire before the blur-triggered lookup resolved) so name/grid/extras
    // are actually captured in the logged entry
    if (!lastLookup || lastLookup.call !== call) {
      try { await logLookup(); } catch {}
    }
    const lk = (lastLookup && lastLookup.call === call) ? lastLookup : {};
    const r = await api("POST", "/api/log/qso", {
      callsign: call,
      name: $("#log-name").value.trim() || lk.name || "",
      rst_sent: $("#log-rst-s").value.trim() || "59",
      rst_rcvd: $("#log-rst-r").value.trim() || "59",
      gridsquare: $("#log-grid").value.trim() || lk.gridsquare || "",
      comment: $("#log-comment").value.trim(),
      email: lk.email || "", qth: lk.qth || "", country: lk.country || "",
    });
    if (r.ok) {
      toast(`Logged ${call}`, "ok");
      lastLookup = null;
      ["#log-call", "#log-name", "#log-grid", "#log-comment"].forEach(s => ($(s).value = ""));
      $("#log-rst-s").value = "59"; $("#log-rst-r").value = "59";
    } else {
      const msg = Object.values(r.targets || {}).map(t => t.message).filter(Boolean).join("; ");
      toast("Log failed: " + (msg || "unknown"), "err");
    }
    loadLogRecent();
  } catch (e) { toast("Log: " + e.message, "err"); }
  finally { btn.disabled = false; }
}

function logStatus(sel, msg, kind) {
  const e = $(sel); if (!e) return;
  e.textContent = msg; e.className = kind || "";
}

async function loadLogConfig() {
  try {
    const c = await api("GET", "/api/log/config");
    $("#log-wl-url").value = c.wavelog_url || "";
    $("#log-wl-key").value = c.wavelog_key || "";
    logStationWanted = c.wavelog_station_id || "";
    $("#log-qrz-user").value = c.qrz_username || "";
    $("#log-qrz-pass").value = c.qrz_password || "";
    $("#log-qrz-key").value = c.qrz_api_key || "";
    logStatus("#log-wl-status", c.wavelog_enabled ? "configured" : "not configured",
              c.wavelog_enabled ? "ok" : "");
    logStatus("#log-qrz-status", c.qrz_enabled ? "configured" : "not configured",
              c.qrz_enabled ? "ok" : "");
    if (c.wavelog_url && c.wavelog_key) loadLogStations();
  } catch (e) { toast("Logging config: " + e.message, "err"); }
}

async function loadLogStations() {
  const sel = $("#log-wl-station"); if (!sel) return;
  try {
    const { stations } = await api("GET", "/api/log/stations");
    sel.innerHTML = '<option value="">—</option>' + (stations || []).map(s =>
      `<option value="${logEsc(s.id)}">${logEsc(s.id)} · ${logEsc(s.callsign || s.name || "")}</option>`).join("");
    sel.value = logStationWanted || "";
  } catch {}
}

async function saveLogConfig(silent) {
  const body = {
    wavelog_url: $("#log-wl-url").value.trim(),
    wavelog_key: $("#log-wl-key").value.trim(),
    wavelog_station_id: $("#log-wl-station").value,
    qrz_username: $("#log-qrz-user").value.trim(),
    qrz_password: $("#log-qrz-pass").value,
    qrz_api_key: $("#log-qrz-key").value.trim(),
  };
  try {
    const c = await api("POST", "/api/log/config", body);
    if (!silent) toast("Logging settings saved", "ok");
    logStatus("#log-wl-status", c.wavelog_enabled ? "configured" : "not configured",
              c.wavelog_enabled ? "ok" : "");
    loadLogRecent();
    return c;
  } catch (e) { if (!silent) toast("Save: " + e.message, "err"); }
}

// test a provider connection with the values currently in the form
async function testProvider(endpoint, statusSel, onOk) {
  logStatus(statusSel, "testing…", "");
  await saveLogConfig(true);
  try {
    const r = await api("POST", endpoint);
    logStatus(statusSel, r.message || (r.ok ? "ok" : "failed"), r.ok ? "ok" : "err");
    if (r.ok && onOk) onOk();
  } catch (e) { logStatus(statusSel, e.message, "err"); }
}

const testLog = () => testProvider("/api/log/test", "#log-wl-status", loadLogStations);
const testQrz = () => testProvider("/api/log/qrz/test", "#log-qrz-status");

function bindLogbook() {
  $("#log-lookup")?.addEventListener("click", logLookup);
  $("#log-save")?.addEventListener("click", logQso);
  const call = $("#log-call");
  call?.addEventListener("change", () => { call.value = call.value.trim().toUpperCase(); });
  call?.addEventListener("blur", () => {
    if (call.value.trim() && !$("#log-name").value.trim()) logLookup();
  });
  call?.addEventListener("keydown", e => { if (e.key === "Enter") logQso(); });
  $("#log-recent")?.addEventListener("click", e => {
    const btn = e.target.closest(".li-del");
    if (btn) deleteLogEntry(btn.dataset.ts);
  });
  $("#log-clear")?.addEventListener("click", clearLogRecent);
  // up/down buttons scroll the recent list (touch decks where direct scroll is
  // awkward); step ~80% of the visible height so rows aren't skipped
  const recList = $("#log-recent");
  const recStep = dir => recList?.scrollBy({ top: dir * recList.clientHeight * 0.8, behavior: "smooth" });
  $("#log-recent-up")?.addEventListener("click", () => recStep(-1));
  $("#log-recent-dn")?.addEventListener("click", () => recStep(1));
  $("#log-wl-test")?.addEventListener("click", testLog);
  $("#log-qrz-test")?.addEventListener("click", testQrz);
  $("#log-wl-reload")?.addEventListener("click", () => { saveLogConfig(true).then(loadLogStations); });
  $("#log-save-cfg")?.addEventListener("click", () => saveLogConfig(false));
  $("#set-cancel-logging")?.addEventListener("click",
    () => $("#settings").classList.remove("open"));
  loadLogRecent();
}

// ---- boot -----------------------------------------------------------------
bindTheme();
bindPanelDeck();
bindHeaderHide();
bindRadioHint();
bindDigi();
bindSelcall();
bindCallsign();
lockLandscape();
bindWakeLock();
bindQuickKeys();
bindScan();
bindPanels();
bindHackRF();
bindControls();
bindMemory();
bindEditor();
bindAudio();
updatePttEnabled();   // PTT/PTT-LOCK start disabled until audio is connected
loadAudioDevices();
bindVfoParams();
bindSettings();
bindLogbook();
bindPower();
renderCallsign();
syncCallsign();
refreshLogo();
refreshPower();
setInterval(refreshPower, 2500);
connectWS();
buildSpinner(0);
buildSpinner(1);
buildSmeter(0);
buildSmeter(1);
// hide the splash once the first status round-trips (with a min display time),
// and a hard fallback so it never gets stuck if the backend is unreachable
api("GET", "/api/status").then(render).catch(() => {})
  .finally(() => setTimeout(hideLaunch, 700));
setTimeout(hideLaunch, 4000);
api("GET", "/api/version").then(v => {
  if (v?.version) $("#foot-version").textContent = v.version;
  if (v?.repo) $("#src-link").href = v.repo;
  if (v?.built) { const d = $("#foot-date"); d.textContent = "built " + v.built; d.hidden = false; }
  fillEnv();
}).catch(() => {});

// Footer info page (last deck slide): app + browser/environment details.
function fillEnv() {
  const el = $("#foot-env"); if (!el) return;
  const n = navigator;
  const dm = matchMedia("(display-mode: standalone)").matches ||
             matchMedia("(display-mode: fullscreen)").matches ? "installed (PWA)" : "browser tab";
  const rows = [
    ["App version", $("#foot-version")?.textContent || "—"],
    ["Backend", apiBase() || location.origin],
    ["Mode", dm],
    ["Secure context", window.isSecureContext ? "yes" : "no"],
    ["Online", n.onLine ? "yes" : "no"],
    ["Browser", n.userAgent],
    ["Platform", n.platform || "—"],
    ["Language", n.language || "—"],
    ["CPU cores", n.hardwareConcurrency || "—"],
    ["Viewport", `${innerWidth}×${innerHeight} @ ${devicePixelRatio || 1}x`],
    ["Screen", `${screen.width}×${screen.height}`],
    ["Orientation", screen.orientation?.type || "—"],
    ["Wake Lock", ("wakeLock" in n) ? (wakeLock ? "held" : "supported") : "unsupported"],
  ];
  el.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${logEsc(v)}</dd>`).join("");
}
fillEnv();
window.addEventListener("resize", fillEnv);
window.addEventListener("online", fillEnv);
window.addEventListener("offline", fillEnv);
screen.orientation?.addEventListener?.("change", fillEnv);
sizeLevelGraph();
window.addEventListener("resize", sizeLevelGraph);
refreshAudio();
setInterval(refreshAudio, 200);   // fast enough for a responsive VU meter
tickClock(); setInterval(tickClock, 1000);

// Persistent audio: auto-reconnect if it was on last session. RX playback may
// need a user gesture (autoplay policy) — resume it on the first tap.
if (localStorage.getItem("tmv71.audioOn") === "1") {
  audioConnect();
  // RX playback may need a user gesture (autoplay policy) — resume on first tap
  const kick = () => { const a = $("#rx-audio"); if (a) a.play().catch(() => {}); window.removeEventListener("pointerdown", kick); };
  window.addEventListener("pointerdown", kick, { once: true });
}

// decorative aircraft-panel corner screws (one set of four per panel)
function addPanelScrews() {
  document.querySelectorAll(".band, .ptt-zone, .audio, .scan-zone, .hackrf-zone, .selcall-zone, .digi-zone, .log-zone").forEach(p => {
    if (p.querySelector(".screw")) return;
    ["tl", "tr", "bl", "br"].forEach(pos => {
      const s = document.createElement("span");
      s.className = "screw screw-" + pos;
      p.appendChild(s);
    });
  });
}
addPanelScrews();

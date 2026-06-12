/* TM-V71 Remote — frontend controller (no build step, vanilla ES) */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let last = null;          // last RadioStatus
let memBase = 0;          // quick-key channel offset: 0 normally, 50 in the air band
let airbandRx = false;    // Band A in the air band → receive-only, TX blocked
let txActive = false;
let pttLock = false;      // latched (continuous) transmit
let lockConfirmed = false; // radio has actually reported TX since locking
let setPttLock = null;    // assigned in bindControls()
let memCache = {};        // channel -> MemoryChannel, for the editor
let edState = { channel: null, isNew: false };

// ---- helpers --------------------------------------------------------------
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
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

  // control band highlight + per-band TX lamp + single-band dimming
  $$(".band").forEach(p => {
    const band = Number(p.dataset.band);
    p.classList.toggle("is-ctrl", band === st.control_band);
    p.classList.toggle("is-ptt", band === st.ptt_band);
    p.classList.toggle("is-tx", !!st.transmitting && st.ptt_band === band);
    p.classList.toggle("is-off", !!st.single_band && band !== st.control_band);
  });
  // audio-band (data band) selector reflects the radio
  const abs = $("#audio-band-sel");
  if (abs && document.activeElement !== abs && st.data_band != null)
    abs.value = String(st.data_band);
  const pttLabel = $("#ptt-band");
  pttLabel.textContent = `PTT → BAND ${st.ptt_band === 1 ? "B" : "A"}`;
  pttLabel.classList.toggle("is-b", st.ptt_band === 1);   // color = selected band
  document.body.classList.toggle("ptt-b", st.ptt_band === 1);
  document.body.classList.toggle("ctrl-b", st.control_band === 1);   // memory-key colour

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
  // mute browser RX output whenever transmitting (covers PTT-lock, spacebar,
  // and radio-side TX, not just the key() button handler)
  const rxEl = $("#rx-audio"); if (rxEl) rxEl.muted = txActive;
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
let lastRxDb = null, lastTxDb = null;             // Pi-measured RX / mic levels

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
  const segs = bar.children;
  for (let i = 0; i < segs.length; i++)
    segs[i].className = "sm-seg" + (i < lit ? (tx ? " on-tx" : " on") : "");
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
    // ROGER toggle button in the PTT panel reflects the roger-beep state
    const rb = $("#btn-roger");
    if (rb) { rb.classList.toggle("on", !!a.roger_beep); rb.setAttribute("aria-pressed", String(!!a.roger_beep)); }
    // two-tone test active -> warning triangle in the PTT panel + reflect switch
    const warn = $("#ptt-warn"); if (warn) warn.hidden = !a.test_tone;
    const tt = $("#set-testtone"); if (tt && document.activeElement !== tt) tt.checked = !!a.test_tone;
    // scroll the level graph only while the browser audio is connected
    if (audioConnected()) pushLevel(a.rx_db, a.tx_db);
    else if (levelHist.length) { levelHist.length = 0; drawLevelGraph(); }
    // mirror gain sliders unless the user is dragging them
    ["rx", "tx"].forEach(k => {
      const sl = document.getElementById(k + "-gain"), g = a[k + "_gain"];
      if (sl && g != null && document.activeElement !== sl) { sl.value = g; setGainUi(k, g); }
    });
  } catch {}
}

// ---- scrolling RX/MIC level graph (newest on the right) -------------------
const GRAPH_MAX = 150;            // ~30 s of history at 200 ms/sample
const levelHist = [];             // {rx,tx} normalized to 0..1
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

function drawLevelGraph() {
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
  const xAt = i => W - (n - 1 - i) * dx;        // newest sample at the right edge
  const lw = Math.max(1.5, (window.devicePixelRatio || 1) * 1.3);
  drawSeries(ctx, levelHist.map(s => s.rx), xAt, H, "rgba(139,122,214,0.9)",  "rgba(139,122,214,0.16)", lw);   // RX = audio-panel violet
  drawSeries(ctx, levelHist.map(s => s.tx), xAt, H, "rgba(207,97,89,0.95)", "rgba(207,97,89,0.14)",  lw);   // muted MIC red
}

function pushLevel(rxDb, txDb) {
  levelHist.push({ rx: lvlNorm(rxDb), tx: lvlNorm(txDb) });
  if (levelHist.length > GRAPH_MAX) levelHist.shift();
  drawLevelGraph();
}

// ---- websocket ------------------------------------------------------------
function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (e) => { try { render(JSON.parse(e.data)); } catch {} };
  ws.onclose = () => { document.body.classList.add("disconnected"); setTimeout(connectWS, 1500); };
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
    el.appendChild(dig);
  });
  const unit = document.createElement("span");
  unit.className = "spin-unit"; unit.textContent = "MHz"; el.appendChild(unit);
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
  // TX side of the data band (which VFO the DATA-connector mic / PKD modulates):
  // data band 0/2 -> A, 1/3 -> B. Mic audio only goes out if this == PTT band.
  const DATA_TX_BAND = { 0: 0, 1: 1, 2: 0, 3: 1 };
  const key = async (on) => {
    if (on && airbandRx) { toast("Air band is receive-only — TX disabled", "err"); return; }
    if (keying === on) return; keying = on;
    // warn if the keyed band isn't where the USB-soundcard mic audio is routed:
    // the audio band (data band) TX side must match the PTT band, else nothing
    // is modulated on air. Warn only — keying is still allowed.
    if (on && last && last.data_band != null && last.ptt_band != null &&
        DATA_TX_BAND[last.data_band] !== last.ptt_band) {
      toast(`Audio band ≠ PTT band: mic is on band ${DATA_TX_BAND[last.data_band] ? "B" : "A"}, ` +
            `transmitting on band ${last.ptt_band ? "B" : "A"} — no modulation!`, "err");
    }
    // mute the browser RX output the instant we key so nothing trails on TX
    const rx = $("#rx-audio"); if (rx) rx.muted = on;
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
  window.addEventListener("keyup", e => { if (e.code === "Space" && !pttLock) key(false); });

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
  if (!confirm(`Delete memory channel ${ch}?`)) return;
  try { await api("DELETE", `/api/memories/${ch}`); toast(`Channel ${ch} deleted`, "ok"); loadMemories(); loadQuickMemNames(); }
  catch (e) { toast("Delete: " + e.message, "err"); }
}

function bindMemory() {
  $("#mem-load").onclick = loadMemories;
  $("#mem-export").addEventListener("click", e => {
    e.target.href = `/api/memories.csv?start=${$("#mem-start").value || 0}&end=${$("#mem-end").value || 999}`;
  });
  $("#mem-import").addEventListener("change", async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try { const r = await fetch("/api/memories/import", { method: "POST", body: fd }); const j = await r.json();
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
  $("#ed-offset").value = m ? (m.offset / 1000) : 600;
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
  rxIn.addEventListener("input", () => {
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
let audioPc = null, audioMic = null;
function audioConnected() {
  return !!audioPc && ["connected", "completed"].includes(audioPc.connectionState);
}
function updateAudioToggle() {
  const t = $("#audio-toggle"); if (!t) return;
  t.textContent = audioPc ? "DISCONNECT" : "CONNECT";
  t.classList.toggle("connected", !!audioPc);
}
async function audioConnect() {
  const t = $("#audio-toggle");
  t.disabled = true; t.textContent = "…";
  try {
    audioMic = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
    });
    audioPc = new RTCPeerConnection();
    audioMic.getTracks().forEach(tr => audioPc.addTrack(tr, audioMic));
    audioPc.addEventListener("track", e => { $("#rx-audio").srcObject = e.streams[0]; });
    audioPc.addEventListener("connectionstatechange", () => {
      updateAudioToggle();
      if (audioPc && ["failed", "closed", "disconnected"].includes(audioPc.connectionState)) audioDisconnect();
    });
    const offer = await audioPc.createOffer({ offerToReceiveAudio: true });
    await audioPc.setLocalDescription(offer);
    await new Promise(res => {                       // non-trickle: gather ICE
      if (audioPc.iceGatheringState === "complete") return res();
      const h = () => { if (audioPc.iceGatheringState === "complete") { audioPc.removeEventListener("icegatheringstatechange", h); res(); } };
      audioPc.addEventListener("icegatheringstatechange", h);
      setTimeout(res, 3000);
    });
    const ans = await api("POST", "/api/webrtc/offer",
      { sdp: audioPc.localDescription.sdp, type: audioPc.localDescription.type });
    await audioPc.setRemoteDescription(ans);
    toast("Audio connected", "ok");
  } catch (e) { toast("Audio: " + e.message, "err"); audioDisconnect(); }
  if (t) t.disabled = false;
  updateAudioToggle();
}
function audioDisconnect() {
  if (audioPc) { try { audioPc.close(); } catch {} audioPc = null; }
  if (audioMic) { audioMic.getTracks().forEach(tr => tr.stop()); audioMic = null; }
  const a = $("#rx-audio"); if (a) a.srcObject = null;
  updateAudioToggle();
}
function setGainUi(key, val) {
  const el = document.getElementById(key + "-gain-val");
  if (el) el.textContent = Number(val).toFixed(1) + "×";
  const sl = document.getElementById(key + "-gain");
  if (sl) {
    const pct = (sl.value - sl.min) / (sl.max - sl.min) * 100;
    sl.style.setProperty("--gpct", pct + "%");
  }
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
  box.innerHTML = "";
  d.controls.forEach(c => {
    const row = document.createElement("div");
    row.className = "mixer-row";
    row.innerHTML =
      `<span class="mx-name">${c.name} · ${c.kind}</span>` +
      `<input type="range" class="mx-slider" min="0" max="100" step="1" value="${c.percent ?? 0}">` +
      `<span class="mx-val">${c.percent ?? 0}%</span>` +
      (c.has_switch ? `<label class="mx-mute"><input type="checkbox" class="mx-sw" ${c.switch_on ? "checked" : ""}>on</label>` : "");
    const sl = row.querySelector(".mx-slider"), val = row.querySelector(".mx-val"),
          sw = row.querySelector(".mx-sw");
    sl.style.setProperty("--gpct", (c.percent ?? 0) + "%");
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
    if (s.tx_buffer_ms != null) { const sl = $("#tx-buffer"); if (sl) sl.value = s.tx_buffer_ms; setMsUi("tx-buffer", s.tx_buffer_ms); }
    if (s.ptt_tail_ms != null) { const sl = $("#ptt-tail"); if (sl) sl.value = s.ptt_tail_ms; setMsUi("ptt-tail", s.ptt_tail_ms); }
  } catch { /* leave as-is */ }
}

function bindAudio() {
  const t = $("#audio-toggle"); if (!t) return;
  $("#audio-band-sel")?.addEventListener("change", async e => {
    const band = Number(e.target.value);
    try { const st = await api("POST", "/api/data-band", { band }); render(st); }
    catch (err) { toast("Audio band: " + err.message, "err"); }
  });
  $("#set-testtone")?.addEventListener("change", async e => {
    try {
      await api("POST", "/api/audio/tones", { test_tone: e.target.checked });
      toast(e.target.checked ? "Two-tone test ON — key PTT" : "Two-tone test off", e.target.checked ? "ok" : "");
    } catch (err) { toast("Test tone: " + err.message, "err"); e.target.checked = !e.target.checked; }
  });
  t.addEventListener("click", () => { audioPc ? audioDisconnect() : audioConnect(); });

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
  [["tx-buffer", "tx_buffer_ms"], ["ptt-tail", "ptt_tail_ms"]].forEach(([id, key]) => {
    const sl = document.getElementById(id); if (!sl) return;
    sl.addEventListener("input", () => setMsUi(id, sl.value));
    sl.addEventListener("change", async () => {
      const body = {}; body[key] = Number(sl.value);
      try { await api("POST", "/api/audio/buffer", body); }
      catch (e) { toast("TX timing: " + e.message, "err"); }
    });
  });
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
async function refreshLogo() {
  let has = false;
  try { has = (await api("GET", "/api/branding")).has_logo; } catch {}
  const src = has ? `/api/branding/logo?t=${Date.now()}` : "";
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
  btn.title = !avail ? "GPIO-Pin in den Einstellungen festlegen"
    : on === true ? "Radio is ON — click to turn off"
    : on === false ? "Radio is OFF — click to turn on"
    : "Radio on/off (GPIO)";
}
async function refreshPower() {
  try { powerState = await api("GET", "/api/power-switch"); refreshPowerUi(); } catch {}
}
// After a GPIO power-on the radio needs a few seconds before it answers CAT.
// Wait until it's connected, then (re)label the M0-M9 quick keys — they're
// loaded once at startup, so otherwise they keep the dimmed "M<n>" placeholder
// that was read while the radio was still off.
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
    if (turnOff && !confirm("Really turn off the radio?")) return;
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
    if (i.app_version) $("#app-version").textContent = i.app_version;
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
  if (!confirm("Pull the latest code from GitHub and restart the service?")) return;
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
    $("#settings").classList.add("open");
  });

  // logo: upload / Kenwood download / remove
  $("#logo-file").addEventListener("change", async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await fetch("/api/branding/logo", { method: "POST", body: fd });
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

// Label the quick keys with the stored memory name for the current bank
// (channels memBase+0 … memBase+9), or keep the "M<ch>" placeholder (dimmed)
// when the channel is empty — width stays fixed.
async function loadQuickMemNames() {
  let rows;
  try { rows = await api("GET", `/api/memories?start=${memBase}&end=${memBase + 9}`); }
  catch { return; }
  const byCh = {}; rows.forEach(r => { byCh[r.channel] = r; });
  $$("#quick-mem .qbtn").forEach(b => {
    const ch = memBase + Number(b.dataset.ch), m = byCh[ch];
    const name = (m && m.name && m.name.trim()) ? m.name.trim() : "";
    b.textContent = name || ("M" + ch);
    b.classList.toggle("empty", !name);
    b.title = name ? `Recall M${ch} · ${name}` : `Memory ${ch} (empty)`;
  });
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

// ---- theme (hell / dunkel) ------------------------------------------------
function applyTheme(light) {
  document.body.classList.toggle("theme-light", light);
  const btn = $("#theme-toggle");
  if (btn) {                              // show the mode you'd switch TO
    btn.textContent = light ? "☾" : "☀";
    btn.setAttribute("aria-pressed", String(light));
    btn.title = light ? "Switch to dark" : "Switch to light";
  }
  localStorage.setItem("tmv71.theme", light ? "light" : "dark");
  // recolor the canvases that use a theme-dependent palette
  try { drawScan(); drawLevelGraph(); } catch {}
}
function bindTheme() {
  applyTheme(localStorage.getItem("tmv71.theme") !== "dark");   // light is the default
  $("#theme-toggle").addEventListener("click",
    () => applyTheme(!document.body.classList.contains("theme-light")));
}

// ---- boot -----------------------------------------------------------------
bindTheme();
bindQuickKeys();
bindScan();
bindControls();
bindMemory();
bindEditor();
bindAudio();
loadAudioDevices();
bindVfoParams();
bindSettings();
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
api("GET", "/api/status").then(render).catch(() => {});
sizeLevelGraph();
window.addEventListener("resize", sizeLevelGraph);
refreshAudio();
setInterval(refreshAudio, 200);   // fast enough for a responsive VU meter
tickClock(); setInterval(tickClock, 1000);

// decorative aircraft-panel corner screws (one set of four per panel)
function addPanelScrews() {
  document.querySelectorAll(".band, .ptt-zone, .audio, .scan-zone").forEach(p => {
    if (p.querySelector(".screw")) return;
    ["tl", "tr", "bl", "br"].forEach(pos => {
      const s = document.createElement("span");
      s.className = "screw screw-" + pos;
      p.appendChild(s);
    });
  });
}
addPanelScrews();

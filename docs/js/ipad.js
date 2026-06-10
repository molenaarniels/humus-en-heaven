// ─── Config ──────────────────────────────────────────────────
const LAT = 52.0907, LON = 5.1214;
const DATA_REFRESH_MS = 5 * 60 * 1000;   // 5 min
const THEME_TICK_MS   = 60 * 1000;
const STALE_HOURS     = 12;

// Same shape as weather_briefing.py
const WEEKDAY_BLOCKS = [
  { name: "Fietstocht",    sh: 6,  sm: 0,  eh: 6,  em: 30, days: [1, 3],       icon: "🚲" },
  { name: "KDV brengen",   sh: 8,  sm: 0,  eh: 9,  em: 0,  days: [0, 1, 3],    icon: "🧒" },
  { name: "Naar kantoor",  sh: 8,  sm: 0,  eh: 9,  em: 0,  days: [2],          icon: "🏢" },
  { name: "Naar huis",     sh: 16, sm: 30, eh: 17, em: 30, days: [0, 1, 2, 3], icon: "🏠" },
  { name: "Sport",         sh: 19, sm: 0,  eh: 20, em: 0,  days: [0, 2],       icon: "🏃" },
];
const PEUTER_BLOCKS = [
  { name: "Na fruit", sh: 9,  sm: 30, eh: 11, em: 30, days: null, icon: "🍓" },
  { name: "Na dutje", sh: 15, sm: 0,  eh: 17, em: 0,  days: null, icon: "💤" },
];
const PEUTER_DAYS = new Set([4, 5, 6]); // Fri, Sat, Sun (Mon=0)

// ─── WMO weather codes → glyph + label ───────────────────────
const WX = {
  0:  ["☀", "helder"],
  1:  ["🌤", "vooral zonnig"],
  2:  ["⛅", "halfbewolkt"],
  3:  ["☁", "bewolkt"],
  45: ["🌫", "mist"],
  48: ["🌫", "rijp"],
  51: ["🌦", "lichte motregen"],
  53: ["🌦", "motregen"],
  55: ["🌧", "stevige motregen"],
  61: ["🌦", "lichte regen"],
  63: ["🌧", "regen"],
  65: ["🌧", "zware regen"],
  71: ["🌨", "lichte sneeuw"],
  73: ["🌨", "sneeuw"],
  75: ["❄", "zware sneeuw"],
  80: ["🌦", "buien"],
  81: ["🌧", "stevige buien"],
  82: ["⛈", "hevige buien"],
  95: ["⛈", "onweer"],
  96: ["⛈", "onweer + hagel"],
  99: ["⛈", "zwaar onweer"],
};
function wxFor(code) { return WX[code] || ["·", "—"]; }

// ─── State ───────────────────────────────────────────────────
let sunrise = null, sunset = null; // Date or null
let lastHourly = null;             // most recent Open-Meteo hourly payload
const $ = (id) => document.getElementById(id);

// ─── Diag (visible only with ?debug or #debug) ───────────────
const DEBUG = /[?#&]debug/i.test(location.search + location.hash);
function logDiag(msg) {
  if (!DEBUG) return;
  const el = $("diag");
  if (!el) return;
  el.hidden = false;
  const t = new Date().toISOString().slice(11, 19);
  el.textContent = (el.textContent + `\n${t}  ${msg}`).split("\n").slice(-40).join("\n");
}
window.addEventListener("error", e => logDiag(`window.error: ${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener("unhandledrejection", e => logDiag(`unhandled: ${e.reason}`));

// ─── Clock + date ────────────────────────────────────────────
function pad(n) { return String(n).padStart(2, "0"); }
const DOW_NL  = ["zondag","maandag","dinsdag","woensdag","donderdag","vrijdag","zaterdag"];
const MONTH_NL = ["januari","februari","maart","april","mei","juni","juli","augustus","september","oktober","november","december"];
function tickClock() {
  const d = new Date();
  $("clock").textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  $("weekday").textContent = DOW_NL[d.getDay()];
  $("daydate").textContent = `${d.getDate()} ${MONTH_NL[d.getMonth()]} ${d.getFullYear()}`;
}

// ─── Theme auto-switch ───────────────────────────────────────
function applyTheme() {
  const now = new Date();
  let dark;
  if (sunrise && sunset) {
    dark = now < sunrise || now > sunset;
  } else {
    const h = now.getHours();
    dark = h < 6 || h >= 21;
  }
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

// ─── Stale check ─────────────────────────────────────────────
function markStaleIf(...timestamps) {
  const now = Date.now();
  const stale = timestamps.some(t => t && (now - new Date(t).getTime()) > STALE_HOURS * 3600 * 1000);
  $("stale-banner").classList.toggle("show", stale);
}

// ─── Data fetches ────────────────────────────────────────────
async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status} on ${url}`);
  return r.json();
}
const bust = () => `?t=${Date.now()}`;

function safe(label, fn) {
  try { fn(); }
  catch (e) {
    console.error(`[${label}]`, e);
    logDiag(`${label}: ${e.message || e}`);
  }
}

async function loadAll() {
  const [soil, mowing, windowData, om, sandbox, pollen] = await Promise.allSettled([
    fetchJSON("data.json" + bust()),
    fetchJSON("mowing_data.json" + bust()),
    fetchJSON("window_data.json" + bust()),
    fetchJSON(`https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}` +
      `&current=temperature_2m,apparent_temperature,relative_humidity_2m,weathercode` +
      `&hourly=temperature_2m,relative_humidity_2m,precipitation,precipitation_probability,uv_index,weathercode` +
      `&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset,weathercode,uv_index_max,precipitation_probability_max` +
      `&timezone=Europe%2FAmsterdam&forecast_days=1`),
    fetchJSON("sandbox_state.json" + bust()),
    fetchJSON(`https://air-quality-api.open-meteo.com/v1/air-quality?latitude=${LAT}&longitude=${LON}` +
      `&hourly=alder_pollen,birch_pollen,grass_pollen,mugwort_pollen,olive_pollen,ragweed_pollen` +
      `&timezone=Europe%2FAmsterdam&forecast_days=1`),
  ]);

  logDiag(`fetch: soil=${soil.status} mow=${mowing.status} win=${windowData.status} om=${om.status} sb=${sandbox.status} pln=${pollen.status}`);

  const wd = windowData.status === "fulfilled" ? windowData.value : null;
  if (om.status === "fulfilled") {
    safe("sun-times", () => {
      const d = om.value.daily;
      sunrise = new Date(d.sunrise[0]);
      sunset  = new Date(d.sunset[0]);
    });
    lastHourly = om.value.hourly || null;
    safe("hero",     () => renderHero(om.value, wd));
    safe("timeline", () => renderTimeline(om.value.hourly));
  } else {
    safe("hero", () => renderHero(null, wd));
  }
  if (soil.status       === "fulfilled") safe("soil",  () => renderSoil(soil.value));
  if (mowing.status     === "fulfilled") safe("mow",   () => renderMow(mowing.value));
  const omv = om.status === "fulfilled" ? om.value : null;
  if (windowData.status === "fulfilled") safe("rooms", () => renderRooms(windowData.value, omv));
  else safe("rooms", () => renderRooms(null, omv));
  safe("blocks", () => renderBlocks());
  safe("chips",  () => renderChips({
    windowData: windowData.status === "fulfilled" ? windowData.value : null,
    om:         om.status         === "fulfilled" ? om.value         : null,
    sandbox:    sandbox.status    === "fulfilled" ? sandbox.value    : null,
    pollen:     pollen.status     === "fulfilled" ? pollen.value     : null,
  }));
  markStaleIf(
    soil.status       === "fulfilled" ? soil.value.generated_at       : null,
    mowing.status     === "fulfilled" ? mowing.value.generated_at     : null,
    windowData.status === "fulfilled" ? windowData.value.generated_at : null,
  );
  applyTheme();
}

// ─── Hero ────────────────────────────────────────────────────
function renderHero(om, windowData) {
  const daily   = om?.daily;
  const current = om?.current;
  const code    = current?.weathercode ?? daily?.weathercode?.[0];
  const [glyph, label] = wxFor(code);

  // Big = perceived/feels-like (Open-Meteo current apparent_temperature).
  // Small italic = "geijkt" — the WU station value, bias-corrected by the
  // window advisor in window_data.outside_now. Fallback to Open-Meteo
  // current temperature_2m if the window advisor hasn't refreshed.
  const perceived = current?.apparent_temperature;
  const measured  = windowData?.outside_now ?? current?.temperature_2m;
  const measuredSrc = windowData?.outside_now != null ? windowData.outside_source : "om";

  $("wx-icon").textContent = glyph;
  $("wx-perceived").textContent = perceived != null ? Math.round(perceived) : "—";
  $("wx-measured").textContent  = measured  != null ? (Math.round(measured * 10) / 10).toFixed(1) + "°" : "—°";

  // Update the "gemeten" micro-label to show source (wu = station)
  const microEls = document.querySelectorAll(".wx-temp .wx-micro");
  if (microEls.length >= 2 && measuredSrc) {
    microEls[1].textContent = measuredSrc === "wu" ? "gemeten · station" : "gemeten";
  }

  const dMax = daily?.temperature_2m_max?.[0];
  const dMin = daily?.temperature_2m_min?.[0];
  const pop  = daily?.precipitation_probability_max?.[0];
  const uv   = daily?.uv_index_max?.[0];
  const bits = [label];
  if (dMax != null && dMin != null) bits.push(`↑${Math.round(dMax)}° ↓${Math.round(dMin)}°`);
  if (pop  != null) bits.push(`regen ${pop}%`);
  if (uv   != null) bits.push(`uv ${Math.round(uv)}`);
  $("wx-meta").textContent = bits.join(" · ");
}

// ─── Timeline SVG ────────────────────────────────────────────
// Resolve CSS custom properties to literal colors at render-time.
// SVG presentation attributes (fill/stroke) don't reliably resolve
// var(...) across browsers — and Safari is especially flaky inside
// elements added via innerHTML — so we substitute literals before render.
function colors() {
  const s = getComputedStyle(document.documentElement);
  const get = (name, fallback) => (s.getPropertyValue(name) || "").trim() || fallback;
  return {
    sun:  get("--sun",      "#c9956a"),
    rain: get("--rain",     "#7a8fa3"),
    ink:  get("--ink",      "#2a2520"),
    soft: get("--ink-soft", "#6b5d4a"),
    faint:get("--ink-faint","#b5a690"),
    hair: get("--ink",      "#2a2520"),
    moss: get("--moss",     "#5a6b3e"),
    clay: get("--clay",     "#b86b4a"),
    bg2:  get("--bg-2",     "#ebe2d2"),
  };
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function renderTimeline(hourly) {
  if (!hourly || !Array.isArray(hourly.time)) return;
  // Open-Meteo (with timezone=Europe/Amsterdam) returns naïve local strings
  // like "2026-06-02T13:00" — compare as strings to avoid TZ shifts.
  const now = new Date();
  const todayStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const idx = hourly.time
    .map((t, i) => (typeof t === "string" && t.slice(0, 10) === todayStr) ? i : -1)
    .filter(i => i >= 0);
  if (idx.length === 0) return;

  const C = colors();
  const temps = idx.map(i => hourly.temperature_2m[i]).filter(v => typeof v === "number");
  const rains = idx.map(i => hourly.precipitation[i] || 0);
  if (temps.length < 2) return;
  const nowH = now.getHours() + now.getMinutes() / 60;

  const W = 1000, H = 90;
  const m = { l: 22, r: 16, t: 8, b: 14 };
  const innerW = W - m.l - m.r;
  const innerH = H - m.t - m.b;

  const x = (h) => m.l + (h / 24) * innerW;

  let tMin = Math.min(...temps), tMax = Math.max(...temps);
  if (tMax - tMin < 4) { const mid = (tMin + tMax) / 2; tMin = mid - 2; tMax = mid + 2; }
  tMin -= 1; tMax += 1;
  const y = (t) => m.t + innerH - ((t - tMin) / (tMax - tMin)) * innerH;

  const rMax = Math.max(2, ...rains);
  const ry = (r) => (r / rMax) * (innerH * 0.55);

  const tempPath = temps.map((t, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(t).toFixed(1)}`).join(" ");
  const fillPath = tempPath + ` L${x(temps.length - 1).toFixed(1)},${H - m.b} L${x(0).toFixed(1)},${H - m.b}Z`;

  const barW = innerW / 24 * 0.7;
  const rainBars = rains.map((r, i) => {
    if (r < 0.1) return "";
    const bx = x(i + 0.5) - barW / 2;
    return `<rect x="${bx.toFixed(1)}" y="${m.t}" width="${barW.toFixed(1)}" height="${ry(r).toFixed(1)}" fill="${C.rain}" opacity="0.55" rx="1"/>`;
  }).join("");

  const grid = [6, 12, 18].map(h =>
    `<line x1="${x(h)}" y1="${m.t}" x2="${x(h)}" y2="${H - m.b}" stroke="${C.hair}" stroke-opacity="0.18" stroke-width="0.5" stroke-dasharray="2,3"/>`
  ).join("");

  const nowMarker = `
    <line x1="${x(nowH).toFixed(1)}" y1="${m.t - 2}" x2="${x(nowH).toFixed(1)}" y2="${H - m.b}" stroke="${C.ink}" stroke-width="1.2"/>
    <circle cx="${x(nowH).toFixed(1)}" cy="${y(temps[Math.min(temps.length - 1, Math.floor(nowH))]).toFixed(1)}" r="3" fill="${C.ink}"/>
  `;

  const iMax = temps.indexOf(Math.max(...temps));
  const iMin = temps.indexOf(Math.min(...temps));
  const labels = `
    <text x="${x(iMax).toFixed(1)}" y="${(y(temps[iMax]) - 4).toFixed(1)}" font-family="JetBrains Mono" font-size="9" fill="${C.soft}" text-anchor="middle">${Math.round(temps[iMax])}°</text>
    <text x="${x(iMin).toFixed(1)}" y="${(y(temps[iMin]) + 10).toFixed(1)}" font-family="JetBrains Mono" font-size="9" fill="${C.soft}" text-anchor="middle">${Math.round(temps[iMin])}°</text>
  `;

  // Build inner SVG fresh each call. We replace via outerHTML (NOT
  // innerHTML on <svg>) because iOS Safari sometimes parses children of
  // svg.innerHTML in HTML namespace — leaving them in the DOM but
  // invisible. outerHTML re-creates the <svg> from the parent's HTML
  // parser, which correctly puts children in SVG namespace.
  const innerSVG = `
    <defs>
      <linearGradient id="tempFill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${C.sun}" stop-opacity="0.28"/>
        <stop offset="100%" stop-color="${C.sun}" stop-opacity="0.02"/>
      </linearGradient>
    </defs>
    ${grid}
    ${rainBars}
    <path d="${fillPath}" fill="url(#tempFill)"/>
    <path d="${tempPath}" fill="none" stroke="${C.sun}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>
    ${labels}
    ${nowMarker}
  `;
  const oldEl = $("timeline");
  const cls = oldEl.getAttribute("class") || "";
  const vb  = oldEl.getAttribute("viewBox") || "0 0 1000 90";
  const pa  = oldEl.getAttribute("preserveAspectRatio") || "none";
  oldEl.outerHTML = `<svg xmlns="http://www.w3.org/2000/svg" id="timeline" class="${cls}" viewBox="${vb}" preserveAspectRatio="${pa}">${innerSVG}</svg>`;
}

// ─── Rooms ───────────────────────────────────────────────────
const ROOM_LABELS = {
  "Living room": "Woonkamer",
  "Ted": "Ted",
  "hotties": "Hotties",
  "office": "Werkkamer",
};
const ROOM_ORDER = ["Living room", "Ted", "hotties", "office"];
const ROOM_SHORT = {
  "Living room": "WOON",
  "Ted":         "TED",
  "hotties":     "HOT",
  "office":      "WERK",
};

// Outside temperature trend (°C/h), derived from Open-Meteo hourly.
// Two-hour forward slope of the local hourly forecast — simple, robust,
// and uses the same data source the timeline already loads.
function computeOutsideTrend(om) {
  if (!om?.hourly?.time || !om?.hourly?.temperature_2m) return 0;
  const now = new Date();
  const todayStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const curStr   = `${todayStr}T${pad(now.getHours())}:00`;
  const idx = om.hourly.time.indexOf(curStr);
  if (idx < 0) return 0;
  const last = om.hourly.temperature_2m.length - 1;
  const future = Math.min(idx + 2, last);
  if (future <= idx) return 0;
  const t0 = om.hourly.temperature_2m[idx];
  const t1 = om.hourly.temperature_2m[future];
  if (typeof t0 !== "number" || typeof t1 !== "number") return 0;
  return (t1 - t0) / (future - idx);  // °C per hour
}

// Outside humidity trend (%RH/h), derived from Open-Meteo hourly — the
// horizontal companion to computeOutsideTrend, so the buiten dot gets an
// angled vector consistent with its displayed (Open-Meteo) humidity.
function computeOutsideHumTrend(om) {
  if (!om?.hourly?.time || !om?.hourly?.relative_humidity_2m) return 0;
  const now = new Date();
  const todayStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const curStr   = `${todayStr}T${pad(now.getHours())}:00`;
  const idx = om.hourly.time.indexOf(curStr);
  if (idx < 0) return 0;
  const last = om.hourly.relative_humidity_2m.length - 1;
  const future = Math.min(idx + 2, last);
  if (future <= idx) return 0;
  const h0 = om.hourly.relative_humidity_2m[idx];
  const h1 = om.hourly.relative_humidity_2m[future];
  if (typeof h0 !== "number" || typeof h1 !== "number") return 0;
  return (h1 - h0) / (future - idx);  // %RH per hour
}

// Scatter plot: vocht (x) vs temp (y). One dot per room + outside.
// Vector = trend direction & magnitude: an angled arrow combining the
// temperature trend (vertical: warming → up) and the humidity trend
// (horizontal: drying → left, humidifying → right). Stable = no vector.
// Room humidity trend comes from window_data (hum_trend); buiten's from
// Open-Meteo. Rooms without hum_trend yet fall back to a vertical vector.
function renderRooms(wd, om) {
  const C = colors();
  const W = 600, H = 400;
  const m = { l: 46, r: 30, t: 22, b: 38 };
  const innerW = W - m.l - m.r;
  const innerH = H - m.t - m.b;

  // Gather data points
  const dots = [];
  const outTemp = wd?.outside_now ?? om?.current?.temperature_2m;
  const outHum  = om?.current?.relative_humidity_2m;
  const outTrend = computeOutsideTrend(om);
  const outHumTrend = computeOutsideHumTrend(om);
  if (outTemp != null && outHum != null) {
    dots.push({ key: "buiten", label: "BUITEN", temp: outTemp, hum: outHum, trend: outTrend, humTrend: outHumTrend, kind: "outside" });
  }
  for (const key of ROOM_ORDER) {
    const r = wd?.rooms?.[key];
    if (!r || r.inside == null || r.humidity == null) continue;
    dots.push({
      key, label: ROOM_SHORT[key] || key.toUpperCase(),
      temp: r.inside, hum: r.humidity,
      trend: typeof r.trend === "number" ? r.trend : 0,
      humTrend: typeof r.hum_trend === "number" ? r.hum_trend : 0,
      advice: r.advice, kind: "room",
    });
  }
  if (dots.length === 0) return;

  // Auto-scale with breathing room. Snap to whole numbers / 10% steps.
  const temps = dots.map(d => d.temp);
  const hums  = dots.map(d => d.hum);
  let tMin = Math.floor(Math.min(...temps) - 1);
  let tMax = Math.ceil (Math.max(...temps) + 1);
  if (tMax - tMin < 10) { const mid = (tMin + tMax) / 2; tMin = Math.floor(mid - 5); tMax = Math.ceil(mid + 5); }
  let hMin = Math.max(0,   Math.floor((Math.min(...hums) - 5) / 10) * 10);
  let hMax = Math.min(100, Math.ceil ((Math.max(...hums) + 5) / 10) * 10);
  if (hMax - hMin < 30) { const mid = (hMin + hMax) / 2; hMin = Math.max(0, Math.floor(mid - 15)); hMax = Math.min(100, Math.ceil(mid + 15)); }

  const X = (h) => m.l + ((h - hMin) / (hMax - hMin)) * innerW;
  const Y = (t) => m.t + (1 - (t - tMin) / (tMax - tMin)) * innerH;

  // Gridlines + tick labels (humidity every 10% on x, temp every 2 °C on y).
  const xTicks = [], yTicks = [];
  for (let h = Math.ceil(hMin / 10) * 10; h <= hMax; h += 10) xTicks.push(h);
  for (let t = Math.ceil(tMin / 2) * 2; t <= tMax; t += 2) yTicks.push(t);
  const grid = [
    ...xTicks.map(h => `<line x1="${X(h).toFixed(1)}" y1="${m.t}" x2="${X(h).toFixed(1)}" y2="${H - m.b}" stroke="${C.hair}" stroke-opacity="0.10" stroke-width="0.5"/>`),
    ...yTicks.map(t => `<line x1="${m.l}" y1="${Y(t).toFixed(1)}" x2="${W - m.r}" y2="${Y(t).toFixed(1)}" stroke="${C.hair}" stroke-opacity="0.10" stroke-width="0.5"/>`),
  ].join("");
  const xLabels = xTicks.map(h => `<text x="${X(h).toFixed(1)}" y="${H - m.b + 14}" font-family="JetBrains Mono" font-size="9" fill="${C.soft}" text-anchor="middle">${h}%</text>`).join("");
  const yLabels = yTicks.map(t => `<text x="${m.l - 6}" y="${(Y(t) + 3).toFixed(1)}" font-family="JetBrains Mono" font-size="9" fill="${C.soft}" text-anchor="end">${t}°</text>`).join("");

  const axes = `
    <line x1="${m.l}" y1="${m.t}" x2="${m.l}" y2="${H - m.b}" stroke="${C.hair}" stroke-opacity="0.28" stroke-width="0.8"/>
    <line x1="${m.l}" y1="${H - m.b}" x2="${W - m.r}" y2="${H - m.b}" stroke="${C.hair}" stroke-opacity="0.28" stroke-width="0.8"/>
  `;
  const titles = `
    <text x="${W - m.r}" y="${H - 4}" font-family="JetBrains Mono" font-size="9" letter-spacing="2" fill="${C.faint}" text-anchor="end">VOCHT →</text>
    <text x="${m.l}" y="${m.t - 8}" font-family="JetBrains Mono" font-size="9" letter-spacing="2" fill="${C.faint}" text-anchor="start">↑ TEMPERATUUR</text>
  `;

  // Dots + vectors
  // The vector projects the dot ~PROJ_H hours forward through the SAME axis
  // mapping as the dots (X/Y), so it points to where the room is actually
  // heading on this temp×vocht plot — angle and length stay consistent with
  // the plotted scale (no arbitrary per-axis pixel constants, no per-axis
  // gates that would swallow the small humidity trends that occur in practice).
  const PROJ_H = 2;              // hours projected forward
  const MIN_PX = 6;              // shorter pixel move than this → effectively stable, no arrow
  const VEC_MIN = 14, VEC_MAX = 70;

  // Pre-compute coordinates so we can pick label positions that don't
  // overlap their dot or each other badly.
  const placed = dots.map(d => ({ ...d, px: X(d.hum), py: Y(d.temp) }));

  const dotsSVG = placed.map((d, i) => {
    const color = d.kind === "outside"
      ? C.sun
      : (d.advice === "dicht" ? C.clay : C.moss);

    // Future position via X()/Y(): humidity trend moves it along x, temperature
    // trend along y (Y is inverted, so warming rises). One angled arrow toward it.
    let vx = X(d.hum + d.humTrend * PROJ_H) - d.px;
    let vy = Y(d.temp + d.trend   * PROJ_H) - d.py;
    const raw = Math.hypot(vx, vy);

    let vector = "";
    if (raw >= MIN_PX) {
      const s = clamp(raw, VEC_MIN, VEC_MAX) / raw;   // clamp length, keep direction
      const x2 = d.px + vx * s, y2 = d.py + vy * s;
      // Small arrowhead so the direction reads at a glance.
      const ang = Math.atan2(y2 - d.py, x2 - d.px), ah = 6, spread = 0.5;
      const ax1 = x2 - ah * Math.cos(ang - spread), ay1 = y2 - ah * Math.sin(ang - spread);
      const ax2 = x2 - ah * Math.cos(ang + spread), ay2 = y2 - ah * Math.sin(ang + spread);
      vector = `<line x1="${d.px.toFixed(1)}" y1="${d.py.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${color}" stroke-opacity="0.55" stroke-width="1.6" stroke-linecap="round"/>`
             + `<polyline points="${ax1.toFixed(1)},${ay1.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)} ${ax2.toFixed(1)},${ay2.toFixed(1)}" fill="none" stroke="${color}" stroke-opacity="0.55" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`;
    }

    const dot = d.kind === "outside"
      ? `<circle cx="${d.px.toFixed(1)}" cy="${d.py.toFixed(1)}" r="7" fill="none" stroke="${color}" stroke-width="2"/>`
      : `<circle cx="${d.px.toFixed(1)}" cy="${d.py.toFixed(1)}" r="6" fill="${color}" stroke="${C.bg2}" stroke-width="1.5"/>`;

    // Place the label opposite the arrow's horizontal direction to avoid sitting
    // on the vector; near an edge, force it inward so it can't clip.
    let labelLeft;
    if (d.px > W - m.r - 60)      labelLeft = true;
    else if (d.px < m.l + 60)     labelLeft = false;
    else                          labelLeft = vx > 0;
    const lx = labelLeft ? d.px - 11 : d.px + 11;
    const ly = d.py + 4;
    const anchor = labelLeft ? "end" : "start";
    const label = `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" font-family="JetBrains Mono" font-size="10" letter-spacing="2" fill="${C.ink}" text-anchor="${anchor}">${d.label}</text>`;

    return `${vector}${dot}${label}`;
  }).join("");

  const innerSVG = `${grid}${axes}${xLabels}${yLabels}${titles}${dotsSVG}`;
  const oldEl = $("rooms-scatter");
  if (!oldEl) return;
  const cls = oldEl.getAttribute("class") || "";
  const par = oldEl.getAttribute("preserveAspectRatio") || "xMidYMid meet";
  oldEl.outerHTML = `<svg xmlns="http://www.w3.org/2000/svg" id="rooms-scatter" class="${cls}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="${par}">${innerSVG}</svg>`;
}

// ─── Soil ────────────────────────────────────────────────────
// Gauge fill = beschikbaar water (100 - depletion). Full ring = full bucket.
function gaugeSVG(availablePct, state) {
  const r = 22, cx = 26, cy = 26;
  const circ = 2 * Math.PI * r;
  const filled = Math.max(0, Math.min(100, availablePct));
  const dash = (filled / 100) * circ;
  const s = getComputedStyle(document.documentElement);
  const lit = (name, fb) => (s.getPropertyValue(name) || "").trim() || fb;
  const colorMap = {
    dry:       lit("--dry",  "#b86b4a"),
    threshold: lit("--warn", "#c4823f"),
    moist:     lit("--moss", "#5a6b3e"),
    wet:       lit("--rain", "#7a8fa3"),
  };
  const ringColor = colorMap[state] || colorMap.moist;
  const trackColor = lit("--ink", "#2a2520");
  return `<svg width="52" height="52" viewBox="0 0 52 52" style="transform:rotate(-90deg)" xmlns="http://www.w3.org/2000/svg">
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${trackColor}" stroke-opacity="0.12" stroke-width="3"/>
    <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${ringColor}" stroke-width="3"
            stroke-dasharray="${dash.toFixed(2)} ${(circ - dash).toFixed(2)}" stroke-linecap="butt"/>
  </svg>`;
}
const STATE_NL = { dry: "droog", threshold: "drempel", moist: "vochtig", wet: "nat" };
function renderSoil(d) {
  const ls = d.lawn_status, ss = d.shrubs_status;
  const zones = [
    { key: "gras",     status: ls },
    { key: "struiken", status: ss },
  ];
  $("soil-row").innerHTML = zones.map(z => {
    const dep = z.status?.depletion_pct ?? 0;
    const avail = Math.max(0, Math.min(100, 100 - dep));
    const st  = z.status?.state || "moist";
    return `
      <div class="gauge">
        ${gaugeSVG(avail, st)}
        <div class="gauge-info">
          <div class="gauge-zone">${z.key}</div>
          <div class="gauge-pct">${Math.round(avail)}<span style="font-size:14px;color:var(--ink-soft)">%</span></div>
          <div class="gauge-state">${STATE_NL[st] || st}</div>
        </div>
      </div>`;
  }).join("");

  $("soil-next-text").textContent = soilNextText(ls, ss);
}
function fmtMm(v) {
  // proposal_mm == 0 is a legitimate value (no action) — keep it distinct
  // from missing data, which we still want to show as "?".
  if (typeof v !== "number") return "?";
  return Number.isInteger(v) ? `${v}` : v.toFixed(1);
}
function soilNextText(ls, ss) {
  const rank = { high: 3, medium: 2, low: 1, none: 0 };
  const candidates = [];
  if (ls) candidates.push({ zone: "gras",     ...ls });
  if (ss) candidates.push({ zone: "struiken", ...ss });
  candidates.sort((a, b) => (rank[b.priority] || 0) - (rank[a.priority] || 0));
  const top = candidates[0];
  if (!top || top.priority === "none") {
    const dts = candidates.map(c => c.days_to_stress).filter(d => d != null);
    if (dts.length) return `droogtegrens over ~${Math.min(...dts)} dagen`;
    return "geen actie nodig";
  }
  const mm = typeof top.proposal_mm === "number" ? top.proposal_mm : null;
  // priority low + 0 mm = light watch-list, no real recommendation
  if (top.priority === "low" && (mm === 0 || mm == null)) {
    const dts = candidates.map(c => c.days_to_stress).filter(d => d != null);
    return dts.length ? `in de gaten houden — grens over ~${Math.min(...dts)} dagen` : "in de gaten houden";
  }
  if (top.priority === "high")   return `nu ${fmtMm(mm)} mm op ${top.zone}`;
  if (top.priority === "medium") return `binnen 1–2 dagen ${fmtMm(mm)} mm op ${top.zone}`;
  if (top.priority === "low")    return `${top.zone}: ${fmtMm(mm)} mm binnen 3 dagen`;
  return "—";
}

// ─── Mow ─────────────────────────────────────────────────────
function renderMow(m) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const ready = m.ready;
  const dormant = m.dormant;
  const next = m.predicted_next_mow;
  const opt  = m.optimal_day;
  const rec  = m.recommended_length;
  let whenHTML = "—", heightHTML = "";

  if (dormant) {
    whenHTML = `<span class="big">winterrust</span>`;
  } else if (ready) {
    whenHTML = `<span class="big">vandaag</span>`;
  } else if (next) {
    const d = new Date(next + "T00:00:00");
    const days = Math.round((d - today) / 86400000);
    if (days <= 0)      whenHTML = `<span class="big">vandaag</span>`;
    else if (days === 1) whenHTML = `<span class="big">morgen</span>`;
    else                 whenHTML = `nog <span class="big">${days}</span> dagen`;
  } else {
    whenHTML = `<span class="big">groeit nog</span>`;
  }
  if (rec && rec.length_mm) {
    heightHTML = `<span class="mm">${rec.length_mm}mm</span>`;
  }
  $("mow-when").innerHTML = whenHTML;
  $("mow-height").innerHTML = heightHTML;
  // mow-reason is intentionally not rendered on the iPad dashboard —
  // the panel is condensed to share a row with the soil gauges. The
  // full reason text lives on the dedicated mowing dashboard.
  const reasonEl = $("mow-reason");
  if (reasonEl) {
    let reason = "";
    if (opt && opt.reason && !ready && !dormant) reason = `optimaal ${shortDate(opt.date)} — ${opt.reason}`;
    else if (rec && rec.reason) reason = rec.reason;
    reasonEl.textContent = reason;
  }
}
function shortDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  return `${d.getDate()} ${MONTH_NL[d.getMonth()].slice(0,3)}`;
}

// ─── Time blocks ─────────────────────────────────────────────
// Summarise weather for the hours the block overlaps: dominant
// (most-severe) WMO code as glyph, peak temp, peak precip-probability
// (omitted under 10%). Uses the cached Open-Meteo hourly payload, so
// renders empty until the first successful fetch.
function blockWeather(b) {
  const hourly = lastHourly;
  if (!hourly?.time) return "";
  const now = new Date();
  const todayStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  // Include every hourly slot that overlaps [start, end). A block that
  // ends at HH:00 doesn't need that hour; one that ends at HH:30 does.
  const lastHour = b.em > 0 ? b.eh : b.eh - 1;
  const temps = [], pops = [], codes = [];
  for (let h = b.sh; h <= lastHour; h++) {
    const idx = hourly.time.indexOf(`${todayStr}T${pad(h)}:00`);
    if (idx < 0) continue;
    const t = hourly.temperature_2m?.[idx];
    const p = hourly.precipitation_probability?.[idx];
    const c = hourly.weathercode?.[idx];
    if (typeof t === "number") temps.push(t);
    if (typeof p === "number") pops.push(p);
    if (typeof c === "number") codes.push(c);
  }
  if (!temps.length) return "";
  const tMax = Math.max(...temps);
  const pMax = pops.length ? Math.max(...pops) : null;
  // Higher WMO codes are roughly more severe — fine as a glyph picker.
  const code = codes.length ? Math.max(...codes) : null;
  const [glyph] = code != null ? wxFor(code) : ["·"];
  const parts = [`<span class="wx-g">${glyph}</span>`, `<span class="wx-t">${Math.round(tMax)}°</span>`];
  if (pMax != null && pMax >= 10) parts.push(`<span class="wx-p">${pMax}%</span>`);
  return parts.join("");
}

function renderBlocks() {
  const now = new Date();
  // weather_briefing uses Mon=0; JS getDay Sun=0
  const wb = (now.getDay() + 6) % 7;
  const blocks = PEUTER_DAYS.has(wb) ? PEUTER_BLOCKS : WEEKDAY_BLOCKS;
  const todays = blocks.filter(b => !b.days || b.days.includes(wb));
  if (todays.length === 0) {
    $("blocks-list").innerHTML = `<div class="dim" style="font-style:italic;font-size:14px;">geen blokken vandaag</div>`;
    return;
  }
  const nowMin = now.getHours() * 60 + now.getMinutes();
  $("blocks-list").innerHTML = todays.map(b => {
    const startMin = b.sh * 60 + b.sm;
    const endMin   = b.eh * 60 + b.em;
    const past = endMin < nowMin;
    const isNow = startMin <= nowMin && nowMin < endMin;
    const wx = blockWeather(b);
    return `
      <div class="block ${past ? "past" : ""} ${isNow ? "now" : ""}">
        <span class="block-time">${pad(b.sh)}:${pad(b.sm)}–${pad(b.eh)}:${pad(b.em)}</span>
        <span class="block-icon">${b.icon}</span>
        <span class="block-name">${b.name}</span>
        <span class="block-wx">${wx}</span>
      </div>`;
  }).join("");
}

// ─── Chips ───────────────────────────────────────────────────
const SANDBOX_LABELS = {
  open:     { txt: "open",     color: "var(--moss)" },
  dicht:    { txt: "dicht",    color: "var(--ink-soft)" },
  afgedekt: { txt: "afgedekt", color: "var(--rain)" },
};
const POLLEN_NL = {
  alder_pollen:   "els",
  birch_pollen:   "berk",
  grass_pollen:   "gras",
  mugwort_pollen: "bijvoet",
  olive_pollen:   "olijf",
  ragweed_pollen: "ambrosia",
};
function pollenLevel(v) {
  if (v == null || v < 20) return null;
  if (v < 50)  return "matig";
  if (v < 100) return "hoog";
  return "zeer hoog";
}
function pollenChipText(pollen) {
  // Returns { txt, color } or null if data unusable.
  if (!pollen?.hourly?.time) return null;
  const now = new Date();
  const todayStr = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const idx = pollen.hourly.time
    .map((t, i) => (typeof t === "string" && t.slice(0, 10) === todayStr) ? i : -1)
    .filter(i => i >= 0);
  if (!idx.length) return null;
  const active = [];
  let anyData = false;
  for (const [key, label] of Object.entries(POLLEN_NL)) {
    const series = pollen.hourly[key];
    if (!Array.isArray(series)) continue;
    const vals = idx.map(i => series[i]).filter(v => typeof v === "number");
    if (!vals.length) continue;
    anyData = true;
    const peak = Math.max(...vals);
    const lvl = pollenLevel(peak);
    if (lvl) active.push(`${label} ${lvl}`);
  }
  if (!anyData) return null;
  if (!active.length) return { txt: "rustig", color: "var(--ink-soft)" };
  return { txt: active.join(", "), color: "var(--warn)" };
}
function renderChips({ windowData, om, sandbox, pollen }) {
  const chips = [];
  if (sunset) {
    chips.push(`<span class="chip">zonsondergang <strong>${pad(sunset.getHours())}:${pad(sunset.getMinutes())}</strong></span>`);
  }
  if (sandbox?.status) {
    const meta = SANDBOX_LABELS[sandbox.status] || { txt: sandbox.status, color: "var(--ink-soft)" };
    chips.push(`<span class="chip" style="color:${meta.color}">zandbak <strong>${meta.txt}</strong></span>`);
  }
  const pln = pollenChipText(pollen);
  if (pln) chips.push(`<span class="chip" style="color:${pln.color}">pollen <strong>${pln.txt}</strong></span>`);
  if (windowData?.bias != null) {
    const b = Math.round(windowData.bias * 10) / 10;
    const sign = b > 0 ? "+" : "";
    chips.push(`<span class="chip">station bias <strong>${sign}${b}°</strong></span>`);
  }
  if (windowData?.warm_ahead) {
    chips.push(`<span class="chip" style="color:var(--clay)">warme dag voor de boeg</span>`);
  }
  if (om?.daily?.uv_index_max?.[0] >= 5) {
    chips.push(`<span class="chip" style="color:var(--warn)">uv hoog · zonbescherming</span>`);
  }
  $("chips").innerHTML = chips.join("");
}

// ─── Boot ────────────────────────────────────────────────────
tickClock();
setInterval(tickClock, 15 * 1000);
applyTheme();
setInterval(applyTheme, THEME_TICK_MS);
loadAll();
setInterval(loadAll, DATA_REFRESH_MS);
// Re-render time-blocks each minute so "now" highlight follows the clock
setInterval(() => safe("blocks-tick", () => renderBlocks()), 60 * 1000);

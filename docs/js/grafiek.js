// ===================== CONFIG =====================
// Volle-pagina scatter: elke kamer als spoor (afgelopen N uur) door de
// temperatuur×vocht-ruimte, met een vervagende lijn naar het verleden en een
// stip + trendpijl aan de kop. Leest dezelfde meetreeks als het raam-koeladvies
// (window_data.json — per kamer `history` + `outside_history`, kwartiercadans).
// COLORS-palet komt uit js/theme.js (geladen vóór dit script).
const ROOM_COLORS = [COLORS.clay, COLORS.rain, COLORS.sun, COLORS.mossLight];
const PROJ_H = 2;          // uur vooruit voor de richtingvector (zelfde als raam-scatter)
const MIN_ALPHA = 0.08;    // doorzichtigheid van het oudste spoorsegment (vervaagt → 1.0 nu)
const state = { data: null, chart: null, windowH: 24, hidden: new Set() };  // hidden = uitgevinkte kamers

document.getElementById("folio-mark").textContent = `Terroir de Utrecht · Est. ${new Date().getFullYear()} · Grafiek`;
document.getElementById("today-date").textContent = new Date().toLocaleDateString("nl-NL", { weekday:"long", day:"numeric", month:"long", year:"numeric" });
document.getElementById("refresh-btn").addEventListener("click", loadData);

// Venster-keuze (6/12/24/48u) — geen inline handlers (CSP), dus event-delegatie.
document.getElementById("window-select").addEventListener("click", (e) => {
  const btn = e.target.closest(".seg-btn");
  if (!btn) return;
  state.windowH = Number(btn.dataset.h);
  document.querySelectorAll("#window-select .seg-btn").forEach(b => b.classList.toggle("active", b === btn));
  if (state.data) drawScatter();
});

// Legenda-vinkjes (kamer aan/uit). Gedelegeerd op #content (blijft bestaan over re-renders heen),
// geen inline handlers (CSP). De legenda wordt niet hertekend bij togglen — alleen de grafiek.
document.getElementById("content").addEventListener("change", (e) => {
  const cb = e.target.closest('input[type="checkbox"][data-label]');
  if (!cb) return;
  if (cb.checked) state.hidden.delete(cb.dataset.label); else state.hidden.add(cb.dataset.label);
  const lab = cb.closest(".legend-toggle");
  if (lab) lab.style.opacity = cb.checked ? "" : "0.4";
  if (state.data) drawScatter();
});

// ===================== DATA =====================
async function loadData() {
  document.getElementById("banner-slot").innerHTML = "";
  document.getElementById("source-label").innerHTML = '<span class="pulse">⋯ data laden…</span>';
  try {
    const res = await fetch(`window_data.json?t=${Date.now()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
    render();
  } catch (e) {
    document.getElementById("banner-slot").innerHTML =
      `<div class="banner banner-error"><strong>Kan data niet laden:</strong> ${e.message}. Heeft de GitHub Action al gedraaid?</div>`;
    document.getElementById("source-label").textContent = "";
  }
}

function ageLabel(iso) {
  const ageH = (Date.now() - new Date(iso).getTime()) / 3.6e6;
  if (ageH < 1) return "net bijgewerkt";
  return `${Math.round(ageH)} uur geleden`;
}

// ===================== RENDER =====================
function render() {
  const d = state.data;
  const srcLabel = d.outside_source === "wu" ? "WU-station" : "Open-Meteo";
  document.getElementById("source-label").textContent =
    `Buiten via ${srcLabel} · ververst ${ageLabel(d.generated_at)} (${new Date(d.generated_at).toLocaleString("nl-NL")})`;

  const banners = [];
  const ageH = (Date.now() - new Date(d.generated_at).getTime()) / 3.6e6;
  if (ageH > 3) banners.push(`<div class="banner banner-warn">⚠ Data is ${Math.round(ageH)} uur oud — draait de Action nog?</div>`);
  document.getElementById("banner-slot").innerHTML = banners.join("");

  document.getElementById("content").innerHTML = `
    <div class="grid">
      <div class="specimen-card">
        <div class="corner-mark">Temperatuur × luchtvochtigheid · spoor van de afgelopen <span id="window-label">${state.windowH}</span> uur</div>
        <div class="chart-box"><canvas id="th-chart"></canvas></div>
        ${scatterLegendHTML(d)}
      </div>
    </div>
  `;

  drawScatter();
}

function ROOMS_ORDER(d) { return Object.keys(d.rooms); }

function scatterLegendHTML(d) {
  const toggles = ROOMS_ORDER(d).map((r,i) => legendToggle(r, ROOM_COLORS[i % ROOM_COLORS.length], "●"))
    .concat([legendToggle("buiten", COLORS.rain, "◆")]);
  const notes = [`spoor: vervaagt van toen → nu`, `→ trend (komende ~2u)`]
    .map(t => `<span style="color:var(--ink-soft)">${t}</span>`);
  return `<div id="legend" style="display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-family:'JetBrains Mono',monospace;font-size:10px;margin-top:10px;color:var(--ink-soft);">${toggles.join("")}${notes.join("")}</div>`;
}

// Eén legenda-vinkje (kamer/buiten aan-uit). Vinkstatus volgt state.hidden zodat hij over een
// refresh of venster-wissel heen bewaard blijft.
function legendToggle(label, color, glyph) {
  const off = state.hidden.has(label);
  return `<label class="legend-toggle" style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;${off ? "opacity:0.4;" : ""}">`
    + `<input type="checkbox" data-label="${label}"${off ? "" : " checked"} style="accent-color:${color};cursor:pointer;margin:0;">`
    + `<span style="color:${color}">${glyph} ${label}</span></label>`;
}

// hex (#rrggbb) → rgba met alpha, voor de vervagende spoorsegmenten.
function hexToRgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

// Bouw één spoor (chronologisch) uit een history-reeks, beperkt tot het gekozen venster.
function buildTrail(history) {
  const cutoff = Date.now() - state.windowH * 3.6e6;
  return (history || [])
    .filter(p => p.temp != null && p.hum != null && new Date(p.t).getTime() >= cutoff)
    .map(p => ({ x: p.hum, y: p.temp, t: p.t }));
}

// ===================== SCATTER MET SPOREN =====================
// Per kamer (en buiten) één lijn-dataset: vervagende segmenten van oud → nu, alle
// punten onzichtbaar behalve de kop (= nu) met een dikkere stip; een schuine
// annotatiepijl aan de kop toont de trend (dx = vocht-trend, dy = temp-trend).
function drawScatter() {
  const d = state.data, P = d.params;
  const wl = document.getElementById("window-label");
  if (wl) wl.textContent = state.windowH;

  const series = [];
  ROOMS_ORDER(d).forEach((name, ri) => {
    const r = d.rooms[name];
    if (!r || state.hidden.has(name)) return;
    let trail = buildTrail(r.history);
    // Geen historie binnen het venster maar wél een actuele meting → toon enkel de kop.
    if (!trail.length && r.inside != null && r.humidity != null)
      trail = [{ x: r.humidity, y: r.inside, t: d.as_of_local }];
    if (!trail.length) return;
    series.push({ label: name, color: ROOM_COLORS[ri % ROOM_COLORS.length], outside: false,
                  trail, dx: (r.hum_trend || 0) * PROJ_H, dy: (r.trend || 0) * PROJ_H });
  });
  let outTrail = state.hidden.has("buiten") ? [] : buildTrail(d.outside_history);
  if (!outTrail.length && !state.hidden.has("buiten") && d.outside_now != null && d.outside_humidity != null)
    outTrail = [{ x: d.outside_humidity, y: d.outside_now, t: d.as_of_local }];
  if (outTrail.length)
    series.push({ label: "buiten", color: COLORS.rain, outside: true, trail: outTrail,
                  dx: (d.outside_hum_trend || 0) * PROJ_H, dy: (d.outside_trend || 0) * PROJ_H });

  // As-bereik strak om alle spoorpunten, met marge.
  const allX = [], allY = [];
  series.forEach(s => s.trail.forEach(p => { allX.push(p.x); allY.push(p.y); }));
  const HUM_MARGIN = 4, TEMP_MARGIN = 0.6;
  const xLo = allX.length ? Math.min(...allX) : 50, xHi = allX.length ? Math.max(...allX) : 70;
  const yLo = allY.length ? Math.min(...allY) : 18, yHi = allY.length ? Math.max(...allY) : 26;
  const hMin = Math.max(0,   Math.floor(xLo - HUM_MARGIN));
  const hMax = Math.min(100, Math.ceil (xHi + HUM_MARGIN));
  const tMin = Math.floor(yLo - TEMP_MARGIN);
  const tMax = Math.ceil (yHi + TEMP_MARGIN);

  // Datasets: per spoor één vervagende lijn met alleen een kopstip.
  const datasets = series.map(s => {
    const n = s.trail.length;
    return {
      label: s.label,
      data: s.trail,
      showLine: true,
      tension: 0.3,
      borderColor: s.color,
      borderWidth: 2,
      segment: {
        // vervaag op basis van de positie van het segment-einde in het spoor
        borderColor: ctx => hexToRgba(s.color, MIN_ALPHA + (1 - MIN_ALPHA) * (n > 1 ? ctx.p1DataIndex / (n - 1) : 1)),
      },
      pointRadius: s.trail.map((_, i) => i === n - 1 ? (s.outside ? 7 : 6) : 0),
      pointHoverRadius: s.trail.map((_, i) => i === n - 1 ? (s.outside ? 8 : 7) : 0),
      pointBackgroundColor: s.color,
      pointBorderColor: s.outside ? COLORS.ink : s.color,
      pointBorderWidth: s.outside ? 1.5 : 1,
      pointStyle: s.trail.map((_, i) => i === n - 1 ? (s.outside ? "rectRot" : "circle") : "circle"),
    };
  });

  // Schuine trendpijl + naam-label aan de kop van elk spoor.
  const ann = {};
  series.forEach((s, i) => {
    const head = s.trail[s.trail.length - 1];
    const dx = Math.max(-20, Math.min(20, s.dx));   // vocht-trend, weergave-clamp
    const dy = Math.max(-3,  Math.min(3,  s.dy));   // temp-trend
    if (!(Math.abs(dx) < 0.5 && Math.abs(dy) < 0.05)) {
      ann["v" + i] = { type: "line", xMin: head.x, yMin: head.y, xMax: head.x + dx, yMax: head.y + dy,
        borderColor: s.color, borderWidth: 2,
        arrowHeads: { end: { display: true, borderColor: s.color, borderWidth: 2, length: 7, width: 6 } } };
    }
    ann["l" + i] = { type: "label", xValue: head.x, yValue: head.y, content: [s.label], yAdjust: -13,
      font: { family: "JetBrains Mono", size: 10, weight: 600 }, color: s.color, backgroundColor: "transparent" };
  });
  // Vocht-drempels van de beslislogica (verticaal op de vocht-as).
  ann.rhTarget = { type: "line", xMin: P.RH_COMFORT, xMax: P.RH_COMFORT, borderColor: COLORS.clay, borderWidth: 1, borderDash: [4,4],
    label: { content: `streef ${P.RH_COMFORT}%`, display: true, position: "start", font: { family: "JetBrains Mono", size: 9 }, color: COLORS.clay, backgroundColor: "transparent" } };
  ann.rhVeto = { type: "line", xMin: P.RH_HARD_CAP, xMax: P.RH_HARD_CAP, borderColor: COLORS.dry, borderWidth: 1, borderDash: [2,4],
    label: { content: `veto ${P.RH_HARD_CAP}%`, display: true, position: "end", font: { family: "JetBrains Mono", size: 9 }, color: COLORS.dry, backgroundColor: "transparent" } };

  state.chart?.destroy();
  const ctx = document.getElementById("th-chart").getContext("2d");
  state.chart = new Chart(ctx, {
    type: "scatter",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 400 },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: COLORS.parchment, titleColor: COLORS.ink, bodyColor: COLORS.ink,
          borderColor: COLORS.ink, borderWidth: 1,
          titleFont: { family: "JetBrains Mono", weight: 600, size: 11 },
          bodyFont: { family: "JetBrains Mono", size: 11 }, padding: 10,
          filter: item => item.raw && item.raw.t != null,
          callbacks: {
            title: items => items.length ? items[0].dataset.label : "",
            label: c => {
              const p = c.raw, when = p.t ? new Date(p.t).toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" }) : "";
              return `${p.y.toFixed(1)}° · ${Math.round(p.x)}% RH${when ? " · " + when : ""}`;
            },
          },
        },
        annotation: { annotations: ann },
      },
      scales: {
        x: { min: hMin, max: hMax, grid: { color: "#2a241b11" },
             ticks: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft },
             title: { display: true, text: "luchtvochtigheid %", font: { family: "JetBrains Mono", size: 10 }, color: COLORS.inkSoft } },
        y: { min: tMin, max: tMax, grid: { color: "#2a241b11" },
             ticks: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft },
             title: { display: true, text: "temperatuur °C", font: { family: "JetBrains Mono", size: 10 }, color: COLORS.inkSoft } },
      },
    },
  });
}

loadData();

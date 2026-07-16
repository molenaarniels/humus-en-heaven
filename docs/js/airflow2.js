// =======================================================================
// Ventilatie 2 (Project 12) — read-only dashboard van de tweede tweeling.
// Leest airflow2_data.json (+ airflow2_learned.json impliciet daarin) en
// haalt voor het vergelijk-paneel tweeling 1's airflow_learned.json erbij.
// Geen Gist-token, geen modal: meldingen doe je op het tweeling-1-dashboard.
// COLORS-palet komt uit js/theme.js (geladen vóór dit script).
// =======================================================================

const state = { data: null, twin1: null, tempChart: null, rhChart: null, rmseChart: null };

document.getElementById("folio-mark").textContent =
  `Terroir de Utrecht · Est. ${new Date().getFullYear()} · Ventilatie 2`;
document.getElementById("today-date").textContent =
  new Date().toLocaleDateString("nl-NL", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
document.getElementById("refresh-btn").addEventListener("click", loadData);

// ===================== DATA =====================
async function loadData() {
  document.getElementById("banner-slot").innerHTML = "";
  document.getElementById("source-label").innerHTML = '<span class="pulse">⋯ data laden…</span>';
  try {
    const res = await fetch(`airflow2_data.json?t=${Date.now()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
  } catch (e) {
    document.getElementById("banner-slot").innerHTML =
      `<div class="banner banner-error">Kon <code>airflow2_data.json</code> niet laden (${e.message}). De tweede tweeling draait elk kwartier mee in de ventilatie-workflow.</div>`;
    document.getElementById("source-label").textContent = "";
    document.getElementById("content").innerHTML = "";
    return;
  }
  // Tweeling 1's leercurve voor het vergelijk-paneel — optioneel: zonder blijft het
  // paneel gewoon leeg (b.v. vlak na een verse deploy).
  try {
    const res1 = await fetch(`airflow_learned.json?t=${Date.now()}`);
    state.twin1 = res1.ok ? await res1.json() : null;
  } catch { state.twin1 = null; }
  render();
}

// ===================== HELPERS =====================
function fmt(v, dec = 1, unit = "") {
  return v == null || Number.isNaN(v) ? "—" : (+v).toFixed(dec) + unit;
}
function errCls(err) { return err == null ? "" : (err >= 0 ? "err-pos" : "err-neg"); }

// Gemiddelde van een leercurve-veld over de laatste `days` dagen (alleen écht-geleerde/
// geëvalueerde punten: held/gepauzeerd telt niet als model-skill).
function recentMean(hist, days = 7, key = "rmse") {
  const cut = Date.now() - days * 864e5;
  const vals = (hist || [])
    .filter(p => p[key] != null && !p.held && !p.paused && new Date(p.t).getTime() >= cut)
    .map(p => p[key]);
  if (!vals.length) return null;
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}
function recentRmse(hist, days = 7) { return recentMean(hist, days, "rmse"); }

// ===================== RENDER =====================
function render() {
  const d = state.data;
  const w = d.weather || {};
  const learned = d.learned || {};
  const asOf = d.as_of_local ? new Date(d.as_of_local) : new Date();
  document.getElementById("source-label").textContent =
    `Bijgewerkt ${asOf.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" })} · model 2 + tado`;

  document.getElementById("banner-slot").innerHTML = d.paused
    ? `<div class="banner banner-warn">⏸️ Gepauzeerd — de tweede tweeling voorspelt door maar leert dit venster niet mee (zelfde pauze als tweeling 1; beheer op het <a class="link" href="airflow.html">Ventilatie-dashboard</a>).</div>`
    : (learned.held
      ? `<div class="banner banner-warn">🧭 Leren gepauzeerd — voorspelfout anomaal hoog; waarschijnlijk wijkt de gemelde raamstand af van de werkelijkheid. Corrigeer de standen op het <a class="link" href="airflow.html">Ventilatie-dashboard</a>.</div>`
      : "");

  let html = "";

  // — Strip: duel + leerstand + weer —
  html += `<div class="grid grid-2" style="padding-top:0;">`;
  html += duelPanel(learned);
  html += `<div class="cell">
      <div class="rule-label">Leerstand — tweeling 2</div>
      <div class="stat-row"><span class="lbl">RMSE (temp)</span><span>${fmt(learned.rmse, 3, " °C")}</span></div>
      <div class="stat-row"><span class="lbl">RMSE (vocht)</span><span>${fmt(learned.rmse_rh, 2, " %RH")}</span></div>
      <div class="stat-row"><span class="lbl">skill vs. persistentie</span><span>${fmt(learned.skill, 3)}</span></div>
      <div class="stat-row"><span class="lbl">regime</span><span>${learned.pinned ? "📌 vastgepind op het batch-anker" : "bootstrap — online lerend tot de eerste batch"}</span></div>
      <div class="stat-row"><span class="lbl">batch-anker</span><span>${learned.anchor_at ? new Date(learned.anchor_at).toLocaleDateString("nl-NL", { day: "numeric", month: "short" }) + ` <span style="color:var(--ink-soft)">(${learned.anchor_src || "?"})</span>` : "— nog geen batch-fit"}</span></div>
      <div class="stat-row"><span class="lbl">buiten nu</span><span>${fmt(w.outside_temp, 1, "°")} · ${fmt(w.outside_humidity, 0, "%")} · ${w.outside_source || "?"}</span></div>
      <div class="stat-row"><span class="lbl">wind</span><span>${fmt(w.wind_speed, 1, " m/s")} uit ${fmt(w.wind_dir, 0, "°")}</span></div>
    </div>`;
  html += `</div>`;

  // — Voorspeld vs. werkelijk: temperatuur + vocht —
  html += `<div class="grid" style="grid-template-columns:1fr;">
    <div class="cell"><div class="rule-label">Voorspeld (model 2) vs. werkelijk (tado) — temperatuur</div>
      <div class="chart-box"><canvas id="temp2-chart"></canvas></div></div>
    <div class="cell"><div class="rule-label">Voorspeld vs. werkelijk — luchtvochtigheid (het tweede leerkanaal)</div>
      <div class="chart-box short"><canvas id="rh2-chart"></canvas></div></div>
  </div>`;

  // — Kamerkaarten —
  html += `<div class="grid grid-rooms">` +
    Object.entries(d.rooms || {}).map(([rid, r]) => roomCard(rid, r)).join("") +
    `</div>`;

  // — Leercurve + geleerde params —
  html += `<div class="grid grid-2">
    <div class="cell"><div class="rule-label">Leercurve — tweeling 2 (klei) vs. tweeling 1 (grijs) · vocht (regen, rechteras) · ▲ = batch-anker</div>
      <div class="chart-box short"><canvas id="rmse2-chart"></canvas></div></div>
    <div class="cell"><div class="rule-label">Geleerde parameters (schalen × fysische basis)</div>${learnedTable(d)}</div>
  </div>`;

  // — Openingen (read-only) —
  html += `<div class="grid" style="grid-template-columns:1fr;"><div class="cell">
      <div class="rule-label">Gerapporteerde stand (gedeelde log — melden op het Ventilatie-dashboard)</div>
      <div class="chips" style="margin-top:8px;">${openingChips(d.openings)}</div>
    </div></div>`;

  document.getElementById("content").innerHTML = html;
  drawTempChart();
  drawRhChart();
  drawRmseChart();
}

function duelPanel(learned) {
  const t1 = state.data.twin1 || {};
  const hist2 = learned.rmse_history || [];
  const hist1 = (state.twin1 && state.twin1.rmse_history) || [];
  const r2 = recentRmse(hist2), r1 = recentRmse(hist1);
  let verdict = `<div class="duel" style="color:var(--ink-soft);">Nog te weinig gedeelde historie voor een eerlijke vergelijking — de curves groeien vanzelf naar elkaar toe.</div>`;
  if (r1 != null && r2 != null) {
    const pct = Math.round((1 - r2 / r1) * 100);
    verdict = pct >= 0
      ? `<div class="duel">Tweeling 2 is <span class="win">${pct}% nauwkeuriger</span> dan tweeling 1 over de laatste 7 dagen.</div>`
      : `<div class="duel">Tweeling 2 is <span class="lose">${-pct}% minder nauwkeurig</span> dan tweeling 1 over de laatste 7 dagen.</div>`;
  }
  const s2 = recentMean(hist2, 7, "skill"), s1 = recentMean(hist1, 7, "skill");
  return `<div class="cell">
      <div class="rule-label">Het duel — model 2 vs. model 1</div>
      <div style="display:flex;gap:26px;align-items:baseline;margin-top:8px;flex-wrap:wrap;">
        <div><div class="big-num">${fmt(r2, 2)}<span>°C</span></div><div class="rule-label">tweeling 2 · rmse 7d</div></div>
        <div><div class="big-num" style="color:var(--ink-soft);">${fmt(r1, 2)}<span>°C</span></div><div class="rule-label">tweeling 1 · rmse 7d</div></div>
      </div>
      ${verdict}
      <div class="stat-row"><span class="lbl">skill 7d (weer-genormaliseerd — eerlijkste maat)</span>
        <span>tweeling 2 ${fmt(s2, 2)} · tweeling 1 ${fmt(s1, 2)}</span></div>
      <div style="font-style:italic;color:var(--ink-soft);font-size:12px;margin-top:8px;">
        Zelfde kalibratievenster, zelfde tado-grond-waarheid, zelfde meldingen — alleen het model
        verschilt. Kanttekening: tweeling 1 fit elke 15 min op precies het venster waarop hij
        gescoord wordt (in-sample); tweeling 2 staat vastgepind en scoort out-of-sample — de
        RMSE-vergelijking vleit tweeling 1.
      </div>
    </div>`;
}

function roomCard(rid, r) {
  const chips = [];
  if (r.ac) chips.push(`<span class="chip-tag" style="color:var(--rain);">❄️ airco — niet gekalibreerd</span>`);
  if (r.heating) chips.push(`<span class="chip-tag" style="color:var(--clay);">🔥 verwarming — stook-samples uit de fit</span>`);
  if (r.paused) chips.push(`<span class="chip-tag">⏸️ gepauzeerd</span>`);
  let subz = "";
  if (r.subzones) {
    const parts = Object.entries(r.subzones).map(([sid, t]) => `${sid} ${fmt(t, 1)}°`);
    subz = `<div class="stat-row"><span class="lbl">verdiepingen</span><span>${parts.join(" · ")}</span></div>`;
  }
  return `<div class="cell">
      <div class="rule-label">${r.label || rid}</div>
      <div style="display:flex;gap:18px;align-items:baseline;margin-top:6px;">
        <div class="big-num">${fmt(r.actual_temp, 1)}<span>°</span></div>
        <div class="room-temp">model ${fmt(r.predicted_temp, 1)}°
          <span class="${errCls(r.error)}">(${r.error == null ? "—" : (r.error > 0 ? "+" : "") + fmt(r.error, 1)}°)</span></div>
      </div>
      <div class="stat-row"><span class="lbl">vocht</span><span>${fmt(r.humidity, 0, "%")} · model ${fmt(r.predicted_rh, 0, "%")}
        <span class="${errCls(r.rh_error)}">(${r.rh_error == null ? "—" : (r.rh_error > 0 ? "+" : "") + fmt(r.rh_error, 0)}%)</span></span></div>
      <div class="stat-row"><span class="lbl">knopen lucht/snel/diep</span><span>${fmt(r.predicted_air_temp, 1)}° / ${fmt(r.predicted_fast_temp, 1)}° / ${fmt(r.predicted_deep_temp, 1)}°</span></div>
      <div class="stat-row"><span class="lbl">verse lucht</span><span>${fmt(r.ach, 2)} ACH · zon ${fmt(r.solar_w, 0, " W")}</span></div>
      ${subz}
      ${chips.length ? `<div class="chips" style="margin-top:8px;">${chips.join("")}</div>` : ""}
    </div>`;
}

function openingChips(openings) {
  const entries = Object.entries(openings || {})
    .filter(([k]) => k !== "paused" && k !== "ac_room");
  if (!entries.length) return `<span style="font-style:italic;">nog geen meldingen</span>`;
  return entries.map(([k, v]) => {
    const s = String(v).toLowerCase();
    const open = !(s === "dicht" || s === "closed" || s === "0" || s === "false");
    return `<span class="chip-tag ${open ? "chip-open" : "chip-dicht"}">${k}: ${v}</span>`;
  }).join("");
}

function learnedTable(d) {
  const p = (d.learned && d.learned.params) || {};
  let s = `<table><thead><tr><th>globaal</th><th>waarde</th></tr></thead><tbody>`;
  ["cp_shelter_front", "cp_shelter_back", "vent_eff", "q_moist", "stair_exch"].forEach(k => {
    if (p[k] != null) s += `<tr><td>${k}</td><td class="num">${(+p[k]).toFixed(3)}</td></tr>`;
  });
  s += `</tbody></table><table style="margin-top:10px;"><thead><tr><th>kamer</th><th>C_a</th><th>C_snel</th><th>C_diep</th><th>zon</th><th>UA</th><th>w_buf</th></tr></thead><tbody>`;
  Object.entries(d.rooms || {}).forEach(([rid, r]) => {
    const rp = r.params || {};
    s += `<tr><td>${r.label || rid}</td><td>${fmt(rp.c_air, 2)}</td><td>${fmt(rp.c_fast, 2)}</td><td>${fmt(rp.c_deep, 2)}</td><td>${fmt(rp.solar_gain, 2)}</td><td>${fmt(rp.ua_env, 2)}</td><td>${fmt(rp.w_buf, 2)}</td></tr>`;
  });
  return s + `</tbody></table>`;
}

// ===================== CHARTS =====================
function drawTempChart() {
  const c = document.getElementById("temp2-chart"); if (!c) return;
  if (state.tempChart) state.tempChart.destroy();
  const palette = [COLORS.moss, COLORS.clay, COLORS.rain, COLORS.sun, COLORS.mossLight, COLORS.dry];
  const ds = []; let i = 0;
  Object.entries(state.data.rooms || {}).forEach(([rid, r]) => {
    const col = palette[i % palette.length]; i++;
    if (r.predicted_series && r.predicted_series.length)
      ds.push({ label: `${r.label || rid} (model 2)`, data: r.predicted_series.map(p => ({ x: p.t, y: p.temp })), borderColor: col, backgroundColor: col, borderWidth: 2, pointRadius: 0, tension: 0.25 });
    if (r.actual_series && r.actual_series.length)
      ds.push({ label: `${r.label || rid} (tado)`, data: r.actual_series.map(p => ({ x: p.t, y: p.temp })), borderColor: col, borderDash: [3, 3], borderWidth: 1.5, pointRadius: 0, tension: 0.25 });
  });
  state.tempChart = new Chart(c, { type: "line", data: { datasets: ds }, options: {
    responsive: true, maintainAspectRatio: false, interaction: { mode: "nearest", intersect: false },
    scales: { x: { type: "time", time: { unit: "hour", displayFormats: { hour: "HH:mm" } }, grid: { color: "#2a241b11" }, ticks: { font: { family: "JetBrains Mono", size: 10 }, color: COLORS.inkSoft } },
              y: { grid: { color: "#2a241b11" }, ticks: { font: { family: "JetBrains Mono", size: 10 }, color: COLORS.inkSoft, callback: v => v + "°" } } },
    plugins: { legend: { labels: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft, boxWidth: 18 } } }
  }});
}

function drawRhChart() {
  const c = document.getElementById("rh2-chart"); if (!c) return;
  if (state.rhChart) state.rhChart.destroy();
  const palette = [COLORS.moss, COLORS.clay, COLORS.rain, COLORS.sun, COLORS.mossLight, COLORS.dry];
  const ds = []; let i = 0;
  Object.entries(state.data.rooms || {}).forEach(([rid, r]) => {
    const col = palette[i % palette.length]; i++;
    if (r.predicted_rh_series && r.predicted_rh_series.length)
      ds.push({ label: `${r.label || rid} (model 2)`, data: r.predicted_rh_series.map(p => ({ x: p.t, y: p.rh })), borderColor: col, backgroundColor: col, borderWidth: 2, pointRadius: 0, tension: 0.25 });
    if (r.actual_rh_series && r.actual_rh_series.length)
      ds.push({ label: `${r.label || rid} (tado)`, data: r.actual_rh_series.map(p => ({ x: p.t, y: p.rh })), borderColor: col, borderDash: [3, 3], borderWidth: 1.5, pointRadius: 0, tension: 0.25 });
  });
  state.rhChart = new Chart(c, { type: "line", data: { datasets: ds }, options: {
    responsive: true, maintainAspectRatio: false, interaction: { mode: "nearest", intersect: false },
    scales: { x: { type: "time", time: { unit: "hour", displayFormats: { hour: "HH:mm" } }, grid: { color: "#2a241b11" }, ticks: { font: { family: "JetBrains Mono", size: 10 }, color: COLORS.inkSoft } },
              y: { grid: { color: "#2a241b11" }, ticks: { font: { family: "JetBrains Mono", size: 10 }, color: COLORS.inkSoft, callback: v => v + "%" } } },
    plugins: { legend: { labels: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft, boxWidth: 18 } } }
  }});
}

function drawRmseChart() {
  const c = document.getElementById("rmse2-chart"); if (!c) return;
  if (state.rmseChart) state.rmseChart.destroy();
  const hist2 = (state.data.learned && state.data.learned.rmse_history) || [];
  const hist1 = (state.twin1 && state.twin1.rmse_history) || [];
  const ds = [{
    label: "tweeling 2 · RMSE (°C)",
    data: hist2.map(p => ({ x: p.t, y: p.rmse, anchored: !!p.anchored })),
    borderColor: COLORS.clay, backgroundColor: COLORS.clay, borderWidth: 2, tension: 0.2,
    // ▲-markering op de punten waar een nieuw batch-anker is geadopteerd.
    pointRadius: hist2.map(p => p.anchored ? 4 : 0),
    pointStyle: hist2.map(p => p.anchored ? "triangle" : "circle"),
    pointBackgroundColor: COLORS.moss, pointBorderColor: COLORS.moss,
  }];
  if (hist1.length)
    ds.push({ label: "tweeling 1 · RMSE (°C)", data: hist1.map(p => ({ x: p.t, y: p.rmse })),
              borderColor: COLORS.inkSoft, backgroundColor: COLORS.inkSoft, borderWidth: 1.5,
              borderDash: [5, 4], pointRadius: 0, tension: 0.2 });
  if (hist2.some(p => p.rmse_rh != null))
    ds.push({ label: "tweeling 2 · RMSE vocht (%RH)", yAxisID: "y2",
              data: hist2.filter(p => p.rmse_rh != null).map(p => ({ x: p.t, y: p.rmse_rh })),
              borderColor: COLORS.rain, backgroundColor: COLORS.rain, borderWidth: 1.5,
              pointRadius: 0, tension: 0.2 });
  state.rmseChart = new Chart(c, { type: "line", data: { datasets: ds }, options: {
    responsive: true, maintainAspectRatio: false,
    scales: { x: { type: "time", time: { unit: "day" }, grid: { display: false }, ticks: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft } },
              y: { beginAtZero: true, grid: { color: "#2a241b11" }, ticks: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft, callback: v => v + "°" } },
              y2: { position: "right", beginAtZero: true, grid: { display: false }, ticks: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.rain, callback: v => v + "%" } } },
    plugins: { legend: { labels: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft, boxWidth: 18 } },
               tooltip: { callbacks: { afterLabel: (ctx) => (ctx.raw || {}).anchored ? "batch-anker geadopteerd" : undefined } } }
  }});
}

loadData();

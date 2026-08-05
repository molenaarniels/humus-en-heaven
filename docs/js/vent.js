// ===================== CONFIG =====================
// Ventilatie (nieuw) — het dashboard van de herbouwde tweeling (vent_twin.py).
// (CONFIG + Gist/token-logica komen uit js/shared.js; plattegrond + speeltuin uit
// js/speeltuin.js; COLORS-palet uit js/theme.js — alle drie vóór dit script geladen.)
const OPENINGS_FILE = "house_openings.json";
const AC_STATE_KEY = "ac_room";   // sleutel in de snapshot: kamer met de mobiele airco (of "")
const PAUSE_STATE_KEY = "paused"; // sleutel in de snapshot: huis-breed gepauzeerd? (bool)
const state = { data: null, forecast: null, tempChart: null, rmseChart: null, pending: {} };

document.getElementById("folio-mark").textContent = `Terroir de Utrecht · Est. ${new Date().getFullYear()} · Ventilatie`;
document.getElementById("today-date").textContent = new Date().toLocaleDateString("nl-NL", { weekday:"long", day:"numeric", month:"long", year:"numeric" });

// Toon de rapportage-UI alleen als er in déze browser een Gist-token is ingesteld (zoals
// op het bodem/gazon-dashboard). Het dashboard is publiek leesbaar, maar wijzigen vereist
// jouw token — die staat enkel lokaal.
if (!CONFIG.githubToken) {
  const rb = document.getElementById("report-btn");
  if (rb) rb.style.display = "none";
}

document.getElementById("refresh-btn").addEventListener("click", loadData);
document.getElementById("report-btn").addEventListener("click", openReport);
document.getElementById("report-cancel").addEventListener("click", () => toggleModal(false));
document.getElementById("report-save").addEventListener("click", saveReport);

// ===================== DATA =====================
async function loadData() {
  document.getElementById("banner-slot").innerHTML = "";
  document.getElementById("source-label").innerHTML = '<span class="pulse">⋯ data laden…</span>';
  try {
    const res = await fetch(bust("vent_data.json"));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
  } catch (e) {
    document.getElementById("banner-slot").innerHTML =
      `<div class="banner banner-error">Kon <code>vent_data.json</code> niet laden (${e.message}). Het model draait elk kwartier via GitHub Actions.</div>`;
    document.getElementById("source-label").textContent = "";
    document.getElementById("content").innerHTML = "";
    return;
  }
  // Vooruitblik-payload (weer-drivers + thermische params + het geankerde zaad). Apart
  // bestand met een eigen levensduur, en de pagina moet zónder ook werken: ontbreekt of
  // faalt hij, dan verliezen we de speeltuin-scenario's en de buitenlijn — niet meer.
  try {
    const res = await fetch(bust("vent_forecast.json"));
    state.forecast = res.ok ? await res.json() : null;
  } catch (e) {
    console.warn("vent_forecast.json niet geladen:", e);
    state.forecast = null;
  }
  // De VentCore-instantie draagt de zojuist vervangen drivers in zich — weggooien, anders
  // rekent een refresh met het vorige kwartier door. Het surrogaat zelf (0.4 MB, verandert
  // niet tussen runs) blijft wél gecachet.
  state.ventCore = null;
  // Renderfouten gescheiden van laadfouten: een chart-/render-exceptie mag de al
  // opgebouwde pagina (plattegrond, kaarten, speeltuin) niet wegvagen of zich als
  // "kon niet laden" vermommen.
  try {
    render();
  } catch (e) {
    document.getElementById("banner-slot").innerHTML =
      `<div class="banner banner-warn">Deel van de pagina kon niet renderen: ${e.message}</div>`;
  }
}

// ===================== RENDER =====================
function render() {
  const d = state.data;
  const w = d.weather || {};
  const asOf = d.as_of_local ? new Date(d.as_of_local) : new Date();
  document.getElementById("source-label").textContent =
    `Bijgewerkt ${asOf.toLocaleTimeString("nl-NL",{hour:"2-digit",minute:"2-digit"})} · bron: model + tado`;

  // — Pauze-badge: zichtbaar zolang het huis nu gepauzeerd is, onafhankelijk van de modal. —
  document.getElementById("banner-slot").innerHTML = d.paused
    ? `<div class="banner banner-warn">⏸️ Gepauzeerd sinds ${d.paused_since ? new Date(d.paused_since).toLocaleTimeString("nl-NL",{hour:"2-digit",minute:"2-digit"}) : "onbekend"} — de tweeling voorspelt door maar leert dit venster niet mee. Zet de pauze uit in de modal zodra de standen weer betrouwbaar te melden zijn.</div>`
    : "";

  const rmse = d.learned && d.learned.rmse != null ? d.learned.rmse : null;
  let html = "";

  // — Strip: weer + zon + wind + leer-RMSE (alleen de paused/held-banners — de rest van de
  //   oude machinerie-callouts bestaat in dit model niet meer) —
  html += `<div class="grid grid-2" style="padding-top:0;">`;
  html += `<div class="specimen-card"><div class="corner-mark">Buiten &amp; hemel</div>
    <div class="card-title">Wind, zon &amp; buitenlucht</div>
    <div class="chips">
      <span class="chip-strong num">${fmt(w.outside_temp)}°C</span> buiten <span class="ctl-sub">(${w.outside_source === "wu" ? "station" : "model"})</span>
      <span>·</span> <span class="num">${fmt(w.outside_humidity,0)}%</span> RV
      <span>·</span> wind <span class="num">${bftText(w.wind_speed)}</span> ${windArrow(w.wind_dir)} ${dirName(w.wind_dir)}
      <span>·</span> zon ${sunGlyph(w.sun_el)} az <span class="num">${fmt(w.sun_az,0)}°</span> h <span class="num">${fmt(w.sun_el,0)}°</span>
    </div>
    <div style="margin-top:12px;" class="chips">
      <span class="ctl-sub">Leerfout (RMSE)</span>
      <span class="chip-strong num">${rmse!=null?rmse.toFixed(2)+"°C":"—"}</span>
      <span class="ctl-sub">${learnTrendText(d)}</span>
      ${d.learned&&d.learned.skill!=null?`<span class="ctl-sub">· skill ${(+d.learned.skill).toFixed(2)}</span>`:""}
      ${fitExcludedNote(d)}
    </div>
    ${(d.learned&&d.learned.paused)?`<div style="margin-top:8px;color:var(--clay);font-style:italic;font-size:13px;border-left:3px solid var(--clay);padding-left:10px;">⏸ Leren gepauzeerd — het huis staat op pauze. Het model voorspelt door maar leert dit venster niet (zo blijft de geleerde fysica schoon).</div>`:''}
    ${(d.learned&&d.learned.held&&!d.learned.paused)?`<div style="margin-top:8px;color:var(--clay);font-style:italic;font-size:13px;border-left:3px solid var(--clay);padding-left:10px;">⏸ Leren gepauzeerd — de fout is anomaal hoog. Waarschijnlijk staat er iets open/dicht dat niet gemeld is; het model voorspelt door en hervat het leren vanzelf (uiterlijk na 24u).</div>`:''}
  </div>`;

  // — Plattegrond (in dezelfde 2-koloms strip als het weer, op brede schermen) —
  html += `</div>`;
  html += `<div class="grid" style="grid-template-columns:1fr;"><div class="specimen-card">
    <div class="corner-mark">Plattegrond · luchtstroom</div>
    <div class="card-title">Wie waait waarheen</div>
    <div style="overflow-x:auto;">${floorPlanSVG(d)}</div>
    <div class="chips" style="margin-top:8px;">
      <span style="color:var(--rain)">➜ instroom (koel)</span>
      <span style="color:var(--clay)">➜ uitstroom (warm)</span>
      <span style="color:var(--moss-light)">➜ tussen kamers</span>
      <span>deursymbool: doorgang + draairichting</span>
      <span>dikte &amp; snelheid ∝ debiet</span>
      <span class="ctl-sub">debieten/ACH: modelschatting — geijkt op temperatuur, niet op gemeten debiet</span>
    </div>
    <div class="chips" style="margin-top:6px;">
      <span style="color:var(--sun)">☀ zon erin</span>
      <span style="color:var(--rain)">❄ warmte eruit</span>
      <span style="color:var(--clay)">🔥 warmte erin (van buiten)</span>
      <span>·</span>
      <span>chip rechtsonder: trend —
        <span style="color:rgb(47,111,176)">afkoelend</span> ·
        <span style="color:rgb(150,144,130)">stabiel</span> ·
        <span style="color:rgb(214,51,42)">opwarmend</span></span>
    </div>
  </div></div>`;

  // — Temperatuur: voorspeld vs werkelijk —
  html += `<div class="grid" style="grid-template-columns:1fr;"><div class="specimen-card">
    <div class="corner-mark">Afgeleide temperaturen vs. werkelijkheid</div>
    <div class="card-title">Voorspeld (model) vs. gemeten (tado)</div>
    <div class="chart-box"><canvas id="temp-chart"></canvas></div>
    <div class="chips" style="margin-top:8px;">
      <span>— doorgetrokken: model</span><span>· · gestippeld: tado-meting</span>
      <span>24u terug · 12u vooruit</span>
      <span class="ctl-sub">rechts van "nu": voorspelling, geankerd op de laatste tado-meting</span>
      ${hiddenNote(d)}
    </div>
  </div></div>`;

  // — Kamerkaarten —
  html += `<div class="grid grid-rooms">`;
  Object.entries(d.rooms || {}).forEach(([rid, r]) => {
    const errCls = r.error==null ? "" : (r.error>=0 ? "err-pos" : "err-neg");
    // Per-raam zon-verdeling (tooltip op de zon-chip).
    const sunTip = Object.values(r.solar_by_window || {})
      .map(w => `${w.label} ${fmt(w.w,0)} W`).join(" · ").replace(/["<>]/g, "'");
    html += `<div class="specimen-card">
      <div class="corner-mark">${r.label || rid}</div>
      <div class="big-num">${fmt(r.predicted_temp)}<span>°C model</span></div>
      <div class="room-temp" style="margin-top:6px;">
        tado <span class="num">${fmt(r.actual_temp)}°C</span> ·
        fout <span class="num ${errCls}">${r.error==null?"—":(r.error>0?"+":"")+r.error.toFixed(1)+"°"}</span>
      </div>
      ${r.ac ? `<div class="ctl-sub" style="margin-top:6px;color:var(--clay);">❄️ airco aan — niet gekalibreerd (model heeft geen koel-term)</div>` : ""}
      ${r.heating ? `<div class="ctl-sub" style="margin-top:6px;color:var(--clay);">🔥 verwarming aan — niet gekalibreerd (model heeft geen verwarmingsterm)</div>` : ""}
      ${r.paused ? `<div class="ctl-sub" style="margin-top:6px;color:var(--clay);">⏸️ gepauzeerd — niet gekalibreerd (standen niet betrouwbaar gemeld)</div>` : ""}
      ${r.fit_excluded ? `<div class="ctl-sub" style="margin-top:6px;color:var(--clay);">🚿 niet gekalibreerd — douche/handbediende afzuiging; deze kamer telt niet mee in de leerfout en staat niet in de grafieken</div>` : ""}
      <div class="chips" style="margin-top:10px;">
        <span class="ctl-sub">ACH</span><span class="num">${fmt(r.ach,2)}</span>
        <span class="ctl-sub">zon in</span><span class="num"${sunTip?` title="${sunTip}"`:""}>${fmt(r.solar_w,0)} W</span>
        <span class="ctl-sub">RV</span><span class="num">${fmt(r.humidity,0)}%</span>
      </div>
      ${energyRow(r)}
      ${r.predicted_mass_temp!=null?`<div class="ctl-sub" style="margin-top:8px;">massaknoop (wanden) ${r.predicted_mass_temp.toFixed(1)}°C${(r.predicted_air_temp!=null&&r.sensor_outdoor_frac>0)?` · ware lucht ~${r.predicted_air_temp.toFixed(1)}°C (voeler op buitenmuur)`:""} · comfort ${r.comfort_low??"?"}–${r.comfort_high??"?"}°</div>`:""}
    </div>`;
  });
  html += `</div>`;

  // — Leerpaneel —
  html += `<div class="grid grid-2"><div class="specimen-card">
    <div class="corner-mark">Leercurve</div>
    <div class="card-title">Wordt de tweeling beter?</div>
    <div class="chart-box short"><canvas id="rmse-chart"></canvas></div>
  </div>
  <div class="specimen-card"><div class="corner-mark">Geleerde parameters</div>
    <div class="card-title">Wat het model leerde</div>
    ${learnedTable(d)}
  </div></div>`;

  // — Speeltuin: interactief luchtstroommodel (markup + logica in js/speeltuin.js) —
  html += sandboxCardHTML();

  document.getElementById("content").innerHTML = html;
  renderSandbox();     // vóór de charts: een chart-fout mag de speeltuin niet meenemen
  if (typeof Chart !== "undefined") {   // CDN niet geladen → pagina zonder grafieken i.p.v. leeg
    drawTempChart();
    drawRmseChart();
  }
}

// ===================== CHARTS =====================
const ROOM_PALETTE = [COLORS.moss, COLORS.clay, COLORS.rain, COLORS.sun, COLORS.mossLight, COLORS.dry];
const HOUR_MS = 3600e3;

function nowMs() {
  const t = state.data && state.data.as_of_local;
  const v = t ? new Date(t).getTime() : NaN;
  return isNaN(v) ? Date.now() : v;
}

// Gedeelde tijd-as voor beide temperatuurgrafieken: middernacht krijgt een sterke lijn +
// datumlabel, 06/12/18u een zwakke + "HH:00". Zonder die dag-ankers is een venster dat de
// nacht overspant nauwelijks te lezen.
const DAY_FMT = new Intl.DateTimeFormat("nl-NL", { weekday: "short", day: "numeric", month: "short" });
const FULL_FMT = new Intl.DateTimeFormat("nl-NL", { weekday: "short", hour: "2-digit", minute: "2-digit" });
function isMidnight(v) { const d = new Date(v); return d.getHours() === 0 && d.getMinutes() === 0; }
function ventTimeScale(xMin, xMax) {
  return {
    type: "time", min: xMin, max: xMax,
    time: { unit: "hour", stepSize: 3, displayFormats: { hour: "HH:mm" } },
    grid: { color: (c) => isMidnight(c.tick.value) ? "#2a241b40" : "#2a241b14",
            lineWidth: (c) => isMidnight(c.tick.value) ? 1.5 : 1 },
    ticks: { autoSkip: true, maxRotation: 0, font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft,
             callback: (v) => isMidnight(v) ? DAY_FMT.format(new Date(v))
                                            : new Date(v).toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" }) },
  };
}
function ventTooltip() {
  return {
    backgroundColor: COLORS.parchment, titleColor: COLORS.ink, bodyColor: COLORS.ink,
    borderColor: COLORS.ink, borderWidth: 1,
    titleFont: { family: "JetBrains Mono", weight: 600, size: 11 },
    bodyFont: { family: "JetBrains Mono", size: 11 }, padding: 10,
    callbacks: {
      title: (items) => items.length ? FULL_FMT.format(new Date(items[0].parsed.x)) : "",
      label: (c) => c.parsed.y == null ? null : `${c.dataset.label}: ${c.parsed.y.toFixed(1)}°`,
    },
  };
}
// Het toekomstvlak + de "nu"-lijn. Alles rechts van de lijn is voorspelling; dát is de
// enige visuele scheiding die de grafiek nodig heeft, dus de modellijn loopt gewoon door
// (een andere streepjesstijl per helft botst met de gestippelde meetlijn).
function futureAnnotations(now, xMax) {
  return {
    future: { type: "box", xMin: now, xMax: xMax, backgroundColor: "#2a241b08",
              borderWidth: 0, drawTime: "beforeDatasetsDraw" },
    now: { type: "line", xMin: now, xMax: now, borderColor: COLORS.ink, borderWidth: 1, borderDash: [2, 4],
           label: { content: "nu · vooruitblik →", display: true, position: "start",
                    font: { family: "JetBrains Mono", size: 9 }, color: COLORS.ink,
                    backgroundColor: "transparent" } },
  };
}

// Buitentemperatuur uit de vooruitblik-payload: verleden (`past`) + toekomst (`steps`).
// Puur context achter de kamerlijnen — de kamers zijn de boodschap.
function outsideSeries() {
  const f = state.forecast;
  if (!f) return [];
  return [...(f.past || []), ...(f.steps || [])]
    .filter(s => s.T_out != null)
    .map(s => ({ x: new Date(s.t).getTime(), y: s.T_out }));
}

function drawTempChart() {
  const c = document.getElementById("temp-chart"); if (!c) return;
  if (state.tempChart) state.tempChart.destroy();
  const now = nowMs();
  const xMin = now - 24 * HOUR_MS, xMax = now + 12 * HOUR_MS;
  const ds = [];
  const out = outsideSeries();
  if (out.length)
    ds.push({ label: "buiten", data: out, borderColor: COLORS.sand, borderWidth: 1.2,
              borderDash: [2, 3], pointRadius: 0, tension: 0.3, spanGaps: true, order: 9 });
  let i = 0;
  // `hidden` (house_model.json → vent_data.json) houdt kamers uit de grafiek die er geen
  // leesbaar verhaal in hebben: de badkamer (douche + handbediende afzuiging → pieken die
  // geen gemelde raamstand verklaart) en het trappenhuis (geen sensor → een lijn zonder
  // meting ernaast). Ze blijven in de plattegrond, op hun kaart en in de meldmodal.
  Object.entries(state.data.rooms || {}).forEach(([rid, r]) => {
    if (r.hidden) return;
    const col = ROOM_PALETTE[i % ROOM_PALETTE.length]; i++;
    // Eén doorlopende modellijn: het gekalibreerde verleden plus de op de tado-meting
    // geankerde 12u-vooruitblik. Dat zijn twee verschillende simulaties (zie
    // vent_forecast.py) maar één verhaal, en ze sluiten per constructie op elkaar aan.
    const model = [...(r.predicted_series || []), ...(r.forecast_series || [])]
      .map(p => ({ x: new Date(p.t).getTime(), y: p.temp }));
    if (model.length)
      ds.push({ label: `${r.label || rid} (model)`, data: model, borderColor: col,
                backgroundColor: col, borderWidth: 2, pointRadius: 0, tension: 0.25 });
    if (r.actual_series && r.actual_series.length)
      ds.push({ label: `${r.label || rid} (tado)`,
                data: r.actual_series.map(p => ({ x: new Date(p.t).getTime(), y: p.temp })),
                borderColor: col, borderDash: [3, 3], borderWidth: 1.5, pointRadius: 0, tension: 0.25 });
  });
  state.tempChart = new Chart(c, { type: "line", data: { datasets: ds }, options: {
    responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
    scales: { x: ventTimeScale(xMin, xMax),
              y: { grid: { color: "#2a241b11" }, ticks: { font: { family: "JetBrains Mono", size: 10 }, color: COLORS.inkSoft, callback: v => v + "°" } } },
    plugins: {
      legend: { labels: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft, boxWidth: 18 } },
      tooltip: ventTooltip(),
      annotation: { annotations: futureAnnotations(now, xMax) },
    },
  }});
}
function drawRmseChart() {
  const c = document.getElementById("rmse-chart"); if (!c) return;
  if (state.rmseChart) state.rmseChart.destroy();
  const hist = (state.data.learned && state.data.learned.rmse_history) || [];
  state.rmseChart = new Chart(c, { type:"line", data:{ datasets:[{ label:"RMSE (°C)",
      data:hist.map(p=>({x:p.t,y:p.rmse})),
      borderColor:COLORS.clay, backgroundColor:COLORS.clay, borderWidth:2, tension:0.2,
      pointRadius:0 }]}, options:{
    responsive:true, maintainAspectRatio:false,
    scales:{ x:{type:"time", time:{unit:"day"}, grid:{display:false}, ticks:{font:{family:"JetBrains Mono",size:9}, color:COLORS.inkSoft}},
             y:{beginAtZero:true, grid:{color:"#2a241b11"}, ticks:{font:{family:"JetBrains Mono",size:9}, color:COLORS.inkSoft, callback:v=>v+"°"}} },
    plugins:{ legend:{display:false} }
  }});
}
function learnedTable(d) {
  const p = (d.learned && d.learned.params) || {};
  let s = `<table><thead><tr><th>globaal</th><th>waarde</th></tr></thead><tbody>`;
  ["cp_shelter","vent_eff","ua_inter"].forEach(k => { if (p[k]!=null) s += `<tr><td>${k}</td><td class="num">${(+p[k]).toFixed(3)}</td></tr>`; });
  s += `</tbody></table><table style="margin-top:10px;"><thead><tr><th>kamer</th><th>C_air</th><th>zon</th><th>UA</th></tr></thead><tbody>`;
  Object.entries(d.rooms||{}).forEach(([rid,r]) => {
    const rp = p[rid]||{};
    s += `<tr><td>${r.label||rid}</td><td class="num">${num(rp.c_air)}</td><td class="num">${num(rp.solar_gain)}</td><td class="num">${num(rp.ua_env)}</td></tr>`;
  });
  return s + `</tbody></table>`;
}

// ===================== REPORTING MODAL =====================
// Voeg de gerapporteerde snapshots voorwaarts samen tot de huidige toestand per element
// (zelfde logica als openings_at in Python): elk element houdt zijn laatst-gezette waarde.
function accumulateLog(log) {
  const s = {};
  (log || []).slice().sort((a,b)=>(""+(a.t||"")).localeCompare(""+(b.t||"")))
    .forEach(e => Object.assign(s, e.states || {}));
  return s;
}

async function openReport() {
  const ctls = (state.data && state.data.controls) || [];
  if (!ctls.length) { alert("Nog geen elementen — vul house_model.json en laat het model één keer draaien."); return; }
  state.pending = {};
  // Tijdstempel-picker standaard op "nu" (lokale tijd) — onveranderd laten = nu; aanpassen om
  // een eerdere wijziging terug te dateren (zie saveReport).
  document.getElementById("report-when").value = localDatetimeValue(new Date());
  document.getElementById("ctl-list").innerHTML = '<div class="ctl-sub pulse">⋯ huidige stand laden…</div>';
  toggleModal(true);
  // Lees de ÉCHTE huidige toestand rechtstreeks uit de Gist-log, niet uit de (tot ~15 min
  // verouderde) server-gegenereerde snapshot. Valt terug op c.state bij geen token.
  let live = null;
  if (CONFIG.githubToken && CONFIG.gistId && CONFIG.gistId !== "__GIST_ID__") {
    try { live = accumulateLog(await fetchOpeningsLog()); }
    catch (e) { console.warn("Live Gist-status ophalen mislukt, val terug op dashboard:", e); }
  }
  const stateOf = (c) => (live && (c.id in live)) ? live[c.id] : c.state;
  // — Pauze-toggle vóór de AC-dropdown: de grijze-uit-stand moet al gezet zijn zodra de
  // rest van de modal rendert.
  buildPauseToggle(live);
  buildAcDropdown(live);
  const groups = { window:"Ramen", vent:"Roosters", shade:"Zonwering", door:"Deuren" };
  const opts = { window:["dicht","tilt","open"], vent:["dicht","open"], shade:["open","half","dicht"], door:["dicht","open"] };
  let html = live ? "" : `<div class="ctl-sub" style="color:var(--clay);margin-bottom:6px;">⚠ kon de live Gist-status niet lezen — toont de laatst bekende dashboard-stand</div>`;
  ["window","vent","shade","door"].forEach(kind => {
    const items = ctls.filter(c => c.kind===kind);
    if (!items.length) return;
    html += `<div class="grp-title">${groups[kind]}</div>`;
    items.forEach(c => {
      const cur = normState(stateOf(c), kind);
      state.pending[c.id] = cur;
      const sub = c.between ? c.between.join(" ↔ ") : (c.room||"");
      html += `<div class="ctl-row"><div><div class="ctl-label">${c.label}</div><div class="ctl-sub">${sub}</div></div>
        <div class="seg" data-id="${c.id}">${opts[kind].map(o =>
          `<button data-v="${o}" class="${cur===o?'active':''}">${o}</button>`).join("")}</div></div>`;
    });
  });
  document.getElementById("ctl-list").innerHTML = html;
  document.querySelectorAll("#ctl-list .seg button").forEach(b => b.addEventListener("click", e => {
    const seg = e.target.closest(".seg"); const id = seg.dataset.id;
    seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
    e.target.classList.add("active");
    state.pending[id] = e.target.dataset.v;
  }));
}
// Vul de pauze-toggle: "Normaal"/"Gepauzeerd" (huis-breed). Huidige stand uit de live
// Gist-log (anders het dashboard). Zet ook meteen de grijze-uit-stand op #ac-row + #ctl-list
// als de modal met een al-actieve pauze opent. Keuze → `paused` (bool) in state.pending.
function buildPauseToggle(live) {
  const seg = document.getElementById("pause-seg");
  if (!seg) return;
  const d = state.data || {};
  const liveHas = live && (PAUSE_STATE_KEY in live);
  const cur = liveHas ? !!live[PAUSE_STATE_KEY] : !!d.paused;
  state.pending[PAUSE_STATE_KEY] = cur;
  applyPauseGrayOut(cur);
  seg.querySelectorAll("button").forEach(b => {
    b.classList.toggle("active", (b.dataset.v === "true") === cur);
  });
  seg.querySelectorAll("button").forEach(b => b.addEventListener("click", e => {
    const v = e.target.dataset.v === "true";
    seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
    e.target.classList.add("active");
    state.pending[PAUSE_STATE_KEY] = v;
    applyPauseGrayOut(v);
  }));
}
// Grijs de AC-dropdown + het hele ramen/roosters/deuren-blok uit zodra gepauzeerd — alleen
// deze toggle en het tijdstip blijven bedienbaar (voor terugdateren).
function applyPauseGrayOut(paused) {
  const acRow = document.getElementById("ac-row");
  const ctlList = document.getElementById("ctl-list");
  if (acRow) acRow.classList.toggle("disabled", paused);
  if (ctlList) ctlList.classList.toggle("disabled", paused);
}
// Vul de airco-dropdown: "geen" + elke sensorkamer. Opties uit d.ac.rooms (server), met
// terugval op de sensorkamers uit house_meta. Keuze → `ac_room` ("" = geen) in state.pending.
function buildAcDropdown(live) {
  const sel = document.getElementById("ac-select");
  if (!sel) return;
  const d = state.data || {};
  let rooms = (d.ac && d.ac.rooms) || [];
  if (!rooms.length) {
    const meta = (d.house_meta && d.house_meta.rooms) || {};
    rooms = Object.entries(meta).filter(([, r]) => r.sensor)
                  .map(([id, r]) => ({ id, label: r.label || id }));
  }
  const liveHas = live && (AC_STATE_KEY in live);
  let cur = liveHas ? live[AC_STATE_KEY] : ((d.ac && d.ac.room) || "");
  cur = (cur == null ? "" : ("" + cur).trim().toLowerCase());
  if (["geen", "none", "off", "uit", "-"].includes(cur)) cur = "";
  sel.innerHTML = `<option value="">geen</option>` +
    rooms.map(r => `<option value="${r.id}">${r.label}</option>`).join("");
  sel.value = cur;
  state.pending[AC_STATE_KEY] = sel.value;       // "" = geen airco
  sel.onchange = () => { state.pending[AC_STATE_KEY] = sel.value; };
}

async function saveReport() {
  if (!ensureToken()) return;
  const btn = document.getElementById("report-save"); btn.textContent = "⋯ bewaren";
  try {
    const log = await fetchOpeningsLog();
    log.push({ t: reportTimestamp(), states: { ...state.pending } });
    // Houd de log behapbaar (laatste ~500 snapshots).
    const trimmed = log.slice(-500);
    await saveOpeningsLog(trimmed);
    await triggerWorkflow();
    toggleModal(false);
    document.getElementById("banner-slot").innerHTML =
      `<div class="banner banner-ok">Standen bewaard — het model leert er bij de volgende run van (kan een paar minuten duren).</div>`;
  } catch (e) {
    alert("Bewaren mislukt: " + e.message);
  } finally { btn.textContent = "Bewaar & leer"; }
}
// Lokale datum/tijd → de "YYYY-MM-DDTHH:mm" waarde die <input type="datetime-local"> verwacht.
function localDatetimeValue(d) {
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
// Tijdstempel voor de snapshot: de (lokale) picker-waarde omgezet naar UTC-ISO. Leeg of
// ongeldig → val terug op nu, zodat een ongewijzigde modal gewoon de huidige tijd gebruikt.
function reportTimestamp() {
  const v = (document.getElementById("report-when") || {}).value;
  if (v) {
    const d = new Date(v);          // datetime-local is lokale tijd → Date interpreteert lokaal
    if (!isNaN(d.getTime())) return d.toISOString();
  }
  return new Date().toISOString();
}
const ensureToken = ensureGistConfig;
async function fetchOpeningsLog() {
  const content = await gistReadFileContent(OPENINGS_FILE);
  if (!content) return [];
  try { return JSON.parse(content).log || []; } catch { return []; }
}
async function saveOpeningsLog(log) {
  await gistWriteFile(OPENINGS_FILE, JSON.stringify({ log }, null, 2));
}
const triggerWorkflow = () => dispatchWorkflow("vent-notify.yml");

// ===================== HELPERS =====================
function num(v) { return v==null ? "—" : (+v).toFixed(2); }
// Noem de weggelaten kamers expliciet onder de grafiek — een lijn die er zonder uitleg niet
// staat leest als een storing. Leeg als er niets verborgen is.
function hiddenNote(d) {
  const names = Object.entries(d.rooms || {}).filter(([, r]) => r.hidden)
    .map(([rid, r]) => r.label || rid);
  return names.length ? `<span class="ctl-sub">niet getoond: ${names.join(", ")}</span>` : "";
}
// Idem bij de leerfout: welke kamers zitten er níét in? Zonder dat erbij is de RMSE een
// getal over een onbekende verzameling kamers.
function fitExcludedNote(d) {
  const names = Object.entries(d.rooms || {}).filter(([, r]) => r.fit_excluded)
    .map(([rid, r]) => r.label || rid);
  return names.length ? `<span class="ctl-sub">· zonder ${names.join(", ")}</span>` : "";
}
function windArrow(deg) { if (deg==null) return ""; const a=["↓","↙","←","↖","↑","↗","→","↘"]; return a[Math.round(((deg%360)/45))%8]; }
function sunGlyph(el) { return el!=null && el>0 ? "☀" : "🌙"; }
function learnTrendText(d) {
  const h = (d.learned && d.learned.rmse_history) || [];
  if (h.length < 3) return "leert nog op…";
  const first = h[0].rmse, last = h[h.length-1].rmse;
  if (last < first - 0.05) return `↓ verbeterd t.o.v. start (${first.toFixed(2)}°)`;
  if (last > first + 0.05) return `↑ fout liep op (${first.toFixed(2)}°)`;
  return "stabiel";
}

loadData();

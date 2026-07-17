// ===================== CONFIG =====================
// (CONFIG + Gist/token-logica komen uit js/shared.js)
const OPENINGS_FILE = "house_openings.json";
const AC_STATE_KEY = "ac_room";   // sleutel in de snapshot: kamer met de mobiele airco (of "")
const PAUSE_STATE_KEY = "paused"; // sleutel in de snapshot: huis-breed gepauzeerd? (bool)
// COLORS-palet komt uit js/theme.js (geladen vóór dit script).
const state = { data: null, tempChart: null, rmseChart: null, pending: {} };

document.getElementById("folio-mark").textContent = `Terroir de Utrecht · Est. ${new Date().getFullYear()} · Ventilatie`;
document.getElementById("today-date").textContent = new Date().toLocaleDateString("nl-NL", { weekday:"long", day:"numeric", month:"long", year:"numeric" });

// Toon de rapportage-UI alleen als er in déze browser een Gist-token is ingesteld (zoals
// op het bodem/gazon-dashboard). Het dashboard is publiek leesbaar, maar wijzigen vereist
// jouw token — die staat enkel lokaal. Zo ziet een willekeurige bezoeker de bewerk-knop
// niet eens (minder misbruikgevoelig); schrijven blijft sowieso geblokkeerd zonder token.
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
    const res = await fetch(bust("airflow_data.json"));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
    render();
  } catch (e) {
    document.getElementById("banner-slot").innerHTML =
      `<div class="banner banner-error">Kon <code>airflow_data.json</code> niet laden (${e.message}). Het model draait elk kwartier via GitHub Actions.</div>`;
    document.getElementById("source-label").textContent = "";
    document.getElementById("content").innerHTML = "";
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

  // — Strip: weer + zon + wind + leer-RMSE —
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
    </div>
    ${(d.learned&&d.learned.paused)?`<div style="margin-top:8px;color:var(--clay);font-style:italic;font-size:13px;border-left:3px solid var(--clay);padding-left:10px;">⏸ Leren gepauzeerd — het huis staat op pauze. Het model voorspelt door maar leert dit venster niet (zo blijft de geleerde fysica schoon).</div>`:''}
    ${(d.learned&&d.learned.held&&!d.learned.paused)?`<div style="margin-top:8px;color:var(--clay);font-style:italic;font-size:13px;border-left:3px solid var(--clay);padding-left:10px;">⏸ Leren gepauzeerd — de fout is anomaal hoog${d.learned.baseline_rmse!=null?` (norm ~${d.learned.baseline_rmse.toFixed(2)}°)`:''}. Waarschijnlijk staat er iets open/dicht dat niet gemeld is; het model voorspelt door maar leert dit venster niet (zo blijft de geleerde fysica schoon).</div>`:''}
    ${(d.learned&&d.learned.solver_failures>0)?`<div style="margin-top:8px;color:var(--clay);font-style:italic;font-size:13px;border-left:3px solid var(--clay);padding-left:10px;">⚠ ${d.learned.solver_failures} substap(pen) met een bijna-singulier thermisch stelsel — de voorspelling bevroor daar even op de laatste goede waarde.</div>`:''}
    ${(d.learned&&d.learned.calib_span_h!=null&&d.learned.calib_samples>0&&d.learned.calib_span_h<(d.learned.calib_coverage_warn_h??24))?`<div style="margin-top:8px;color:var(--clay);font-style:italic;font-size:13px;border-left:3px solid var(--clay);padding-left:10px;">⚠ Dunne kalibratiedekking: ${d.learned.calib_samples} samples over ~${d.learned.calib_span_h}u (na AC-/verwarmings-/pauzefilters) — de fit leunt dit venster op weinig grond-waarheid.</div>`:''}
  </div>`;

  // — Suggestie —
  html += renderAdvice(d);
  html += `</div>`;

  // — Plattegrond —
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
    <div class="chips" style="margin-top:8px;"><span>— doorgetrokken: model</span><span>· · gestippeld: tado-meting</span></div>
  </div></div>`;

  // — Kamerkaarten —
  html += `<div class="grid grid-rooms">`;
  Object.entries(d.rooms || {}).forEach(([rid, r]) => {
    const adv = (r.actual_temp!=null && r.predicted_temp!=null);
    const errCls = r.error==null ? "" : (r.error>=0 ? "err-pos" : "err-neg");
    // Per-raam zon-verdeling (additief veld; oudere JSON zonder → geen tooltip).
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
  drawTempChart();
  drawRmseChart();
}

// ===================== ADVIES =====================
// Het advies als simpele verander-checklist: vergelijk de beste raamstand met de huidige
// gerapporteerde stand (d.controls) en toon alléén de verschillen. De volledige doelstand
// en de alternatieven zitten achter een <details>-uitklapper (native, CSP-veilig).
function shortLabel(s) {
  if (!s) return s || "";
  let cut = s.length;
  [" (", " — "].forEach(sep => { const i = s.indexOf(sep); if (i >= 0 && i < cut) cut = i; });
  return s.slice(0, cut);
}

// Doelstand per bedienbaar raam: keep_closed → alles dicht; anders uit instructions[]
// (bevat óók vast glas → filteren op de bedienbare set); oudere data zonder instructions
// → de winnaar uit ranked[]. null = geen advies beschikbaar.
function adviceTarget(d) {
  const sg = d.suggestion || {};
  const ops = (d.controls || []).filter(c => c.kind === "window");
  if (!ops.length) return null;
  const target = {};
  if (sg.keep_closed) { ops.forEach(c => target[c.id] = "dicht"); return target; }
  if (sg.instructions && sg.instructions.length) {
    const byId = {}; sg.instructions.forEach(i => byId[i.window] = i.action);
    ops.forEach(c => target[c.id] = byId[c.id] === "open" ? "open" : "dicht");
    return target;
  }
  if (sg.ranked && sg.ranked.length) {
    const open = new Set(sg.ranked[0].windows || []);
    ops.forEach(c => target[c.id] = open.has(c.id) ? "open" : "dicht");
    return target;
  }
  return null;
}

// Verschillen doel ↔ nu. Kiepstand telt als (deels) open: doel open + nu kiep → geen
// actie, wel een notitie; doel dicht + nu kiep → sluiten.
function adviceDeltas(d, target) {
  const deltas = [], notes = [];
  (d.controls || []).filter(c => c.kind === "window").forEach(c => {
    const cur = normState(c.state, "window"), want = target[c.id];
    if (want === "open" && cur === "dicht") deltas.push({ act: "open", c });
    else if (want === "open" && cur === "tilt") notes.push(`${shortLabel(c.label)} staat op kiep — telt als (deels) open`);
    else if (want === "dicht" && cur !== "dicht") deltas.push({ act: "sluit", c });
  });
  return { deltas, notes };
}

// De suggest()-score is ≈ watt nuttige koeling (1.2·cp·debiet·ΔT minus kleine straffen).
function effectText(score) {
  return (score != null && score > 0) ? `≈ ${Math.round(score)} W koeling` : "geen nuttige koeling";
}

function renderAdvice(d) {
  const sg = d.suggestion || {};
  let html = `<div class="specimen-card"><div class="corner-mark">Passief advies (handel niet verplicht)</div>
    <div class="card-title">Wat zou koeling geven?</div>
    <p class="adv-headline">${sg.headline || "—"}</p>`;
  const target = adviceTarget(d);
  if (!target) return html + `</div>`;
  const { deltas, notes } = adviceDeltas(d, target);
  if (deltas.length) {
    html += `<ul class="adv-checklist">` + deltas.map(dl =>
      `<li class="${dl.act === "open" ? "adv-open" : "adv-close"}">▸ ${dl.act === "open" ? "Open" : "Sluit"} <b>${shortLabel(dl.c.label)}</b> <span class="ctl-sub">${roomLabel(dl.c.room)}</span></li>`
    ).join("") + `</ul>`;
  } else {
    html += `<p class="adv-ok">✓ alles staat al goed</p>`;
  }
  notes.forEach(n => html += `<div class="ctl-sub" style="margin-top:4px;">◦ ${n}</div>`);

  html += `<details class="adv-more"><summary>Volledige stand &amp; alternatieven</summary>`;
  html += `<table><thead><tr><th>raam</th><th>doel</th><th>nu</th></tr></thead><tbody>`;
  (d.controls || []).filter(c => c.kind === "window").forEach(c => {
    const cur = normState(c.state, "window"), want = target[c.id];
    const diff = want !== cur && !(want === "open" && cur === "tilt");
    html += `<tr><td>${shortLabel(c.label)}</td>
      <td class="num" style="color:${want === "open" ? "var(--moss)" : "var(--ink-soft)"}">${want}</td>
      <td class="num" style="${diff ? "color:var(--clay);font-weight:600;" : ""}">${cur}</td></tr>`;
  });
  html += `</tbody></table>`;
  if (sg.ranked && sg.ranked.length) {
    const maxPos = Math.max(0, ...sg.ranked.map(r => r.score || 0));
    // Bij "alles dicht" hoort de dicht-optie bovenaan; alternatieven zonder nut krijgen geen balk.
    const rows = sg.keep_closed
      ? [...sg.ranked].sort((a, b) => ((b.windows || []).length === 0) - ((a.windows || []).length === 0))
      : sg.ranked;
    html += `<div class="grp-title">Alternatieven</div>`;
    rows.forEach(r => {
      const labs = (r.windows || []).map(wid => shortLabel(winLabel(wid))).join(" + ") || "alles dicht houden";
      const roomsTxt = (r.rooms || []).map(roomLabel).join(" · ");
      const bw = maxPos > 0 ? Math.round(100 * Math.max(0, r.score || 0) / maxPos) : 0;
      html += `<div class="ranked-row"><div style="flex:1;">
          <div class="ranked-windows">${labs}</div>
          ${roomsTxt ? `<div class="ranked-rooms">${roomsTxt}</div>` : ""}
          ${bw > 0 ? `<div class="ranked-bar" style="width:${bw}%;"></div>` : ""}
        </div><div class="ranked-effect">${effectText(r.score)}</div></div>`;
    });
  }
  html += `</details></div>`;
  return html;
}

// ===================== FLOOR PLAN → verhuisd naar js/speeltuin.js =====================
// (floorPlanSVG + teken-/trend-helpers zijn gedeeld met het Ventilatie (stabiel)-dashboard;
//  speeltuin.js wordt vóór dit script geladen, dus alle functies blijven globaal beschikbaar.)

// ===================== CHARTS =====================
function drawTempChart() {
  const c = document.getElementById("temp-chart"); if (!c) return;
  if (state.tempChart) state.tempChart.destroy();
  const palette = [COLORS.moss, COLORS.clay, COLORS.rain, COLORS.sun, COLORS.mossLight, COLORS.dry];
  const ds = []; let i=0;
  Object.entries(state.data.rooms||{}).forEach(([rid,r]) => {
    const col = palette[i%palette.length]; i++;
    if (r.predicted_series && r.predicted_series.length)
      ds.push({ label:`${r.label||rid} (model)`, data:r.predicted_series.map(p=>({x:p.t,y:p.temp})), borderColor:col, backgroundColor:col, borderWidth:2, pointRadius:0, tension:0.25 });
    if (r.actual_series && r.actual_series.length)
      ds.push({ label:`${r.label||rid} (tado)`, data:r.actual_series.map(p=>({x:p.t,y:p.temp})), borderColor:col, borderDash:[3,3], borderWidth:1.5, pointRadius:0, tension:0.25 });
  });
  state.tempChart = new Chart(c, { type:"line", data:{datasets:ds}, options:{
    responsive:true, maintainAspectRatio:false, interaction:{mode:"nearest",intersect:false},
    scales:{ x:{type:"time", time:{unit:"hour", displayFormats:{hour:"HH:mm"}}, grid:{color:"#2a241b11"}, ticks:{font:{family:"JetBrains Mono",size:10}, color:COLORS.inkSoft}},
             y:{grid:{color:"#2a241b11"}, ticks:{font:{family:"JetBrains Mono",size:10}, color:COLORS.inkSoft, callback:v=>v+"°"}} },
    plugins:{ legend:{labels:{font:{family:"JetBrains Mono",size:9}, color:COLORS.inkSoft, boxWidth:18}} }
  }});
}
function drawRmseChart() {
  const c = document.getElementById("rmse-chart"); if (!c) return;
  if (state.rmseChart) state.rmseChart.destroy();
  const hist = (state.data.learned && state.data.learned.rmse_history) || [];
  // Punten die achteraf tegen de gecorrigeerde openingen-log zijn herberekend (een te laat
  // gemelde/teruggedateerde raamwijziging) krijgen een moss-stip + tooltip met de oude waarde,
  // zodat zichtbaar is waar de curve model-skill toont i.p.v. een meld-fout.
  state.rmseChart = new Chart(c, { type:"line", data:{ datasets:[{ label:"RMSE (°C)",
      data:hist.map(p=>({x:p.t,y:p.rmse,recomputed:!!p.recomputed,logged:p.rmse_logged})),
      borderColor:COLORS.clay, backgroundColor:COLORS.clay, borderWidth:2, tension:0.2,
      pointRadius:hist.map(p=>p.recomputed?2.5:0),
      pointBackgroundColor:hist.map(p=>p.recomputed?COLORS.moss:COLORS.clay),
      pointBorderColor:hist.map(p=>p.recomputed?COLORS.moss:COLORS.clay) }]}, options:{
    responsive:true, maintainAspectRatio:false,
    scales:{ x:{type:"time", time:{unit:"day"}, grid:{display:false}, ticks:{font:{family:"JetBrains Mono",size:9}, color:COLORS.inkSoft}},
             y:{beginAtZero:true, grid:{color:"#2a241b11"}, ticks:{font:{family:"JetBrains Mono",size:9}, color:COLORS.inkSoft, callback:v=>v+"°"}} },
    plugins:{ legend:{display:false}, tooltip:{ callbacks:{ afterLabel:(ctx)=>{
      const r = ctx.raw||{}; if (!r.recomputed) return undefined;
      return r.logged!=null ? `herberekend (was ${(+r.logged).toFixed(2)}°)` : "herberekend"; }}} }
  }});
}
function learnedTable(d) {
  const p = (d.learned && d.learned.params) || {};
  let s = `<table><thead><tr><th>globaal</th><th>waarde</th></tr></thead><tbody>`;
  ["cp_shelter","cd","vent_eff"].forEach(k => { if (p[k]!=null) s += `<tr><td>${k}</td><td class="num">${(+p[k]).toFixed(3)}</td></tr>`; });
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
  // verouderde) server-gegenereerde snapshot. Zo toont de modal direct wat je laatst hebt
  // ingesteld, onafhankelijk van wanneer de Action draait. Valt terug op c.state bij geen token.
  let live = null;
  if (CONFIG.githubToken && CONFIG.gistId && CONFIG.gistId !== "__GIST_ID__") {
    try { live = accumulateLog(await fetchOpeningsLog()); }
    catch (e) { console.warn("Live Gist-status ophalen mislukt, val terug op dashboard:", e); }
  }
  const stateOf = (c) => (live && (c.id in live)) ? live[c.id] : c.state;
  // — Pauze-toggle: huis-breed "Normaal"/"Gepauzeerd". Grijst de AC-dropdown + het hele
  // ramen/roosters/deuren-blok uit zolang gepauzeerd (zie applyPauseGrayOut). Moet vóór
  // buildAcDropdown draaien, want de pauze-rij staat erboven en de grijze-uit-stand moet al
  // gezet zijn zodra de rest van de modal rendert.
  buildPauseToggle(live);
  // — Airco-dropdown: kies de kamer met de mobiele unit (of geen). Opties uit de server
  // (alleen sensorkamers); valt terug op de sensorkamers uit house_meta. Huidige stand uit de
  // live Gist-log, anders uit het dashboard. Wordt als `ac_room` in de snapshot meebewaard.
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
// Vul de pauze-toggle: "Normaal"/"Gepauzeerd" (seg-knoppen, geen dropdown — huis-breed, geen
// kamer-keuze). Huidige stand uit de live Gist-log (anders het dashboard). Zet ook meteen de
// grijze-uit-stand op #ac-row + #ctl-list als de modal met een al-actieve pauze opent. De keuze
// wordt als `paused` (bool) in state.pending meegeschreven.
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
// Grijs de AC-dropdown + het hele ramen/roosters/deuren-blok uit (pointer-events + opacity via
// .disabled) zodra gepauzeerd — alleen deze toggle en het tijdstip blijven bedienbaar, zodat je
// later kunt terugdateren om de écht opgetreden standen alsnog te melden.
function applyPauseGrayOut(paused) {
  const acRow = document.getElementById("ac-row");
  const ctlList = document.getElementById("ctl-list");
  if (acRow) acRow.classList.toggle("disabled", paused);
  if (ctlList) ctlList.classList.toggle("disabled", paused);
}
// Vul de airco-dropdown: "geen" + elke sensorkamer. Opties uit d.ac.rooms (server), met
// terugval op de sensorkamers in house_meta. Huidige keuze uit de live Gist-log (anders het
// dashboard). De keuze wordt als `ac_room` ("" = geen) in state.pending meegeschreven.
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
// Tijdstempel voor de snapshot: de (lokale) picker-waarde omgezet naar UTC-ISO — net als de oude
// `new Date().toISOString()`. Leeg of ongeldig → val terug op nu, zodat een ongewijzigde modal
// gewoon de huidige tijd gebruikt.
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
const triggerWorkflow = () => dispatchWorkflow("airflow-notify.yml");

// ===================== SPEELTUIN → verhuisd naar js/speeltuin.js =====================


// ===================== HELPERS =====================
function num(v) { return v==null ? "—" : (+v).toFixed(2); }
function winLabel(wid) { const m=state.data.house_meta||{}; return (m.windows&&m.windows[wid]&&m.windows[wid].label)||wid; }
function roomLabel(rid) { const m=state.data.house_meta||{}; const z=(m.rooms&&m.rooms[rid])||(m.junctions&&m.junctions[rid]); return (z&&z.label)||rid; }
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

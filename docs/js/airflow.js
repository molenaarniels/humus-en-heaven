// ===================== CONFIG =====================
// (CONFIG + Gist/token-logica komen uit js/shared.js)
const OPENINGS_FILE = "house_openings.json";
const COLORS = { parchment:"#f3ecd9", ink:"#2a241b", inkSoft:"#5c4f3c", moss:"#3d5a3a", mossLight:"#6b8562", clay:"#b8532a", sun:"#d4a017", dry:"#a0421a", rain:"#4a6b8a" };
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

  const rmse = d.learned && d.learned.rmse != null ? d.learned.rmse : null;
  let html = "";

  // — Strip: weer + zon + wind + leer-RMSE —
  html += `<div class="grid grid-2" style="padding-top:0;">`;
  html += `<div class="specimen-card"><div class="corner-mark">Buiten &amp; hemel</div>
    <div class="card-title">Wind, zon &amp; buitenlucht</div>
    <div class="chips">
      <span class="chip-strong num">${fmt(w.outside_temp)}°C</span> buiten
      <span>·</span> <span class="num">${fmt(w.outside_humidity,0)}%</span> RV
      <span>·</span> wind <span class="num">${bftText(w.wind_speed)}</span> ${windArrow(w.wind_dir)} ${dirName(w.wind_dir)}
      <span>·</span> zon ${sunGlyph(w.sun_el)} az <span class="num">${fmt(w.sun_az,0)}°</span> h <span class="num">${fmt(w.sun_el,0)}°</span>
    </div>
    <div style="margin-top:12px;" class="chips">
      <span class="ctl-sub">Leerfout (RMSE)</span>
      <span class="chip-strong num">${rmse!=null?rmse.toFixed(2)+"°C":"—"}</span>
      <span class="ctl-sub">${learnTrendText(d)}</span>
    </div>
    ${(d.learned&&d.learned.held)?`<div style="margin-top:8px;color:var(--clay);font-style:italic;font-size:13px;border-left:3px solid var(--clay);padding-left:10px;">⏸ Leren gepauzeerd — de fout is anomaal hoog${d.learned.baseline_rmse!=null?` (norm ~${d.learned.baseline_rmse.toFixed(2)}°)`:''}. Waarschijnlijk staat er iets open/dicht dat niet gemeld is; het model voorspelt door maar leert dit venster niet (zo blijft de geleerde fysica schoon).</div>`:''}
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
    html += `<div class="specimen-card">
      <div class="corner-mark">${r.label || rid}</div>
      <div class="big-num">${fmt(r.predicted_temp)}<span>°C model</span></div>
      <div class="room-temp" style="margin-top:6px;">
        tado <span class="num">${fmt(r.actual_temp)}°C</span> ·
        fout <span class="num ${errCls}">${r.error==null?"—":(r.error>0?"+":"")+r.error.toFixed(1)+"°"}</span>
      </div>
      <div class="chips" style="margin-top:10px;">
        <span class="ctl-sub">ACH</span><span class="num">${fmt(r.ach,2)}</span>
        <span class="ctl-sub">zon in</span><span class="num">${fmt(r.solar_w,0)} W</span>
        <span class="ctl-sub">RV</span><span class="num">${fmt(r.humidity,0)}%</span>
      </div>
      ${energyRow(r)}
      ${r.predicted_mass_temp!=null?`<div class="ctl-sub" style="margin-top:8px;">massaknoop (wanden) ${r.predicted_mass_temp.toFixed(1)}°C · comfort ${r.comfort_low??"?"}–${r.comfort_high??"?"}°</div>`:""}
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

  // — Speeltuin: interactief luchtstroommodel —
  html += `<div class="grid" style="grid-template-columns:1fr;"><div class="specimen-card" id="sandbox-card">
    <div class="corner-mark">Speeltuin · wat-als</div>
    <div class="card-title">Experimenteer met de luchtstroom</div>
    <p style="font-style:italic;color:var(--ink-soft);font-size:14px;margin:0 0 6px;">
      Zet hier ramen, roosters en deuren open of dicht (en draai aan wind &amp; buitentemperatuur) en
      zie meteen wat er met de luchtstroom gebeurt — zónder iets te bewaren of het echte model te raken.
      Hetzelfde luchtstroomnetwerk als hierboven, maar live in je browser. De kamertemperaturen blijven
      staan op de huidige meting; je ziet het ógenblikkelijke debiet (ventilatievoud &amp; koeling), geen
      heruitgerekende opwarming.</p>
    <div class="grid grid-2" style="padding:0;gap:20px;">
      <div>
        <div class="grp-title" style="margin-top:4px;">Omstandigheden</div>
        <div id="sandbox-env"></div>
        <div id="sandbox-controls" style="margin-top:6px;"></div>
        <button class="btn btn-secondary" id="sandbox-reset" style="margin-top:14px;">↺ Terug naar huidige stand</button>
      </div>
      <div>
        <div class="grp-title" style="margin-top:4px;">Resultaat</div>
        <div id="sandbox-out"></div>
        <div style="overflow-x:auto;margin-top:10px;" id="sandbox-plan"></div>
        <div class="chips" style="margin-top:8px;">
          <span style="color:var(--rain)">➜ instroom (koel)</span>
          <span style="color:var(--clay)">➜ uitstroom (warm)</span>
          <span style="color:var(--moss-light)">➜ tussen kamers</span>
          <span>dikte &amp; snelheid ∝ debiet</span>
        </div>
      </div>
    </div>
  </div></div>`;

  document.getElementById("content").innerHTML = html;
  drawTempChart();
  drawRmseChart();
  renderSandbox();
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

// ===================== FLOOR PLAN =====================
const FLOOR_NL = {0:"Begane grond", 1:"1e verdieping", 2:"2e verdieping", 3:"3e"};

function zoneFill(r) {
  if (!r || r.predicted_temp==null) return "rgba(92,79,60,0.06)";        // junctie / geen sensor
  const t=r.predicted_temp, hi=r.comfort_high, lo=r.comfort_low;
  if (hi!=null && t>hi) { const x=Math.min(1,(t-hi)/4.5); return `rgba(184,83,42,${(0.10+0.30*x).toFixed(2)})`; }
  if (lo!=null && t<lo) { const x=Math.min(1,(lo-t)/4.5); return `rgba(74,107,138,${(0.10+0.22*x).toFixed(2)})`; }
  return "rgba(61,90,58,0.13)";
}

// Trendkleur = richting van de temperatuurverandering (trend_c_per_h). Fel rood = opwarmend,
// fel blauw = afkoelend, grijs ertussenin (geen verandering). De vulling toont de toestand
// (warm/koel t.o.v. comfort); de trend-chip rechtsonder toont waar het naartoe gáát.
const TREND_FULL = 0.8;                       // °C/uur → volle verzadiging van de rand
const TREND_GRAY = [150,144,130], TREND_WARM = [214,51,42], TREND_COOL = [47,111,176];
function lerpColor(a, b, t) {
  const r=Math.round(a[0]+(b[0]-a[0])*t), g=Math.round(a[1]+(b[1]-a[1])*t), bl=Math.round(a[2]+(b[2]-a[2])*t);
  return `rgb(${r},${g},${bl})`;
}
function outlineColor(trend) {
  if (trend==null || isNaN(trend)) return null;               // geen sensor → standaard rand
  const x = Math.max(-1, Math.min(1, trend / TREND_FULL));
  return x>=0 ? lerpColor(TREND_GRAY, TREND_WARM, x) : lerpColor(TREND_GRAY, TREND_COOL, -x);
}
function rgbToRgba(c, a) { return c && c.startsWith("rgb(") ? c.replace("rgb(", "rgba(").replace(")", `,${a})`) : c; }
// Netto warmte-uitwisseling met buiten (W): schil-conductie + ventilatie. − = energie verlaat
// de kamer (koeling), + = de buitenlucht warmt de kamer op. De tegenhanger van de zonwinst.
function outsideNet(r) {
  if (r.env_w==null && r.vent_w==null) return null;
  return (r.env_w||0) + (r.vent_w||0);
}
function trendText(t) {
  if (t==null || isNaN(t)) return "—";
  const a = t>0.03 ? "↑" : (t<-0.03 ? "↓" : "→");
  return `${a} ${t>0?"+":""}${t.toFixed(2)}°/u`;
}
function energyRow(r) {
  const net = outsideNet(r);
  const oc = outlineColor(r.trend_c_per_h) || COLORS.inkSoft;
  const outTxt = net==null ? "—"
    : (net < 0 ? `<span style="color:${COLORS.rain}">❄ ${Math.round(-net)} W eruit</span>`
    : (net > 0 ? `<span style="color:${COLORS.clay}">🔥 ${Math.round(net)} W erin</span>` : "0 W"));
  return `<div class="chips" style="margin-top:6px;">
      <span class="ctl-sub">naar buiten</span><span class="num">${outTxt}</span>
      <span class="ctl-sub">richting</span><span class="num" style="color:${oc};font-weight:600;">${trendText(r.trend_c_per_h)}</span>
    </div>`;
}

// — Architectonische tekenhulpen —
// De plattegrond is een mini-architectenplan: muren als dubbele inktlijn met échte
// openingen erin; ramen/roosters/deuren als klassieke plansymbolen. Plaatsing komt uit
// house_model.json (plan_side + plan_pos per opening, additief); zonder die velden valt
// de plaatsing terug op gevel-azimut → buitenzijde + gelijkmatige verdeling.
const FP = { cw: 164, ch: 124, gap: 30, padL: 92, padT: 80, padR: 60, padB: 36 };
const WALL_T = 5;                              // muurband-dikte (px)
const PLAN_SIDES = ["top", "bottom", "left", "right"];

// — Plattegrond-oriëntatie: het plan is GEDRAAID (niet noord-boven). De rechterrand kijkt naar de
// straat/voorgevel (NW, ~309° — zie house_model.json _README), de linkerrand naar de tuin (ZO, 129°).
// De kompasroos + windpijl in het cartouche draaien hierin mee, zodat de richting klopt t.o.v. de
// kamers (pijl naar rechts = wind naar de straatgevel) i.p.v. een losse noord-boven-roos.
const PLAN_FRONT_AZ = 309;
const PLAN_ROT = ((90 - PLAN_FRONT_AZ) % 360 + 360) % 360;   // azimut → schermhoek (kloksgewijs vanaf boven)

function sideGeom(rc, side) {                  // beginpunt + tangent + buitennormaal + lengte
  switch (side) {
    case "top":    return { x0: rc.x,        y0: rc.y,        tx: 1, ty: 0, nx: 0,  ny: -1, len: rc.w };
    case "bottom": return { x0: rc.x,        y0: rc.y + rc.h, tx: 1, ty: 0, nx: 0,  ny: 1,  len: rc.w };
    case "left":   return { x0: rc.x,        y0: rc.y,        tx: 0, ty: 1, nx: -1, ny: 0,  len: rc.h };
    default:       return { x0: rc.x + rc.w, y0: rc.y,        tx: 0, ty: 1, nx: 1,  ny: 0,  len: rc.h };
  }
}
function sidePoint(rc, side, t) {
  const g = sideGeom(rc, side);
  return { x: g.x0 + g.tx*g.len*t, y: g.y0 + g.ty*g.len*t, nx: g.nx, ny: g.ny, tx: g.tx, ty: g.ty };
}
function glyphLen(el, kind) {                  // symboolbreedte langs de muur (px)
  if (kind === "vent") return 10;
  if (kind === "skylight") return 16;
  if (kind === "door") return Math.max(16, Math.min(34, 12 * (el.area_m2 || 1.5)));
  return Math.max(14, Math.min(56, 18 * Math.sqrt(el.area_m2 || 0.5)));
}

// Tegenover elkaar liggende randen van twee rastercellen (deuren verbinden over de kier).
function sharedEdge(rcA, rcB) {
  const g = FP.gap + 2;
  const vSpan = () => [Math.max(rcA.y, rcB.y), Math.min(rcA.y + rcA.h, rcB.y + rcB.h)];
  const hSpan = () => [Math.max(rcA.x, rcB.x), Math.min(rcA.x + rcA.w, rcB.x + rcB.w)];
  let d = rcB.x - (rcA.x + rcA.w);
  if (d > -1 && d <= g) { const [lo, hi] = vSpan(); if (hi > lo + 8) return { sideA: "right",  sideB: "left",   lo, hi, vertical: true }; }
  d = rcA.x - (rcB.x + rcB.w);
  if (d > -1 && d <= g) { const [lo, hi] = vSpan(); if (hi > lo + 8) return { sideA: "left",   sideB: "right",  lo, hi, vertical: true }; }
  d = rcB.y - (rcA.y + rcA.h);
  if (d > -1 && d <= g) { const [lo, hi] = hSpan(); if (hi > lo + 8) return { sideA: "bottom", sideB: "top",    lo, hi, vertical: false }; }
  d = rcA.y - (rcB.y + rcB.h);
  if (d > -1 && d <= g) { const [lo, hi] = hSpan(); if (hi > lo + 8) return { sideA: "top",    sideB: "bottom", lo, hi, vertical: false }; }
  return null;
}

// Alle openingen op de muren plaatsen: expliciet (plan_side/plan_pos) → fallback (azimut →
// buitenzijde) → gelijkmatig spreiden + minimale onderlinge afstand. Levert per opening het
// muurpunt (incl. normaal/tangent, voor glyph én stroompijl) en per kamer de muurgaten.
function placeOpenings(meta, zones, ids, rectOf, planH) {
  const occ = new Set();
  ids.forEach(id => { const [x, y] = zones[id].plan_xy; for (let r = 0; r < planH(id); r++) occ.add(`${x},${y + r}`); });
  const extSides = id => {
    const [x, y] = zones[id].plan_xy, h = planH(id), out = [];
    if (!occ.has(`${x + 1},${y}`)) out.push("right");
    if (!occ.has(`${x - 1},${y}`)) out.push("left");
    if (!occ.has(`${x},${y + h}`)) out.push("bottom");
    if (!occ.has(`${x},${y - 1}`)) out.push("top");
    return out.length ? out : ["top"];
  };

  const entries = [];
  const collect = (els, kind) => Object.entries(els || {}).forEach(([id, el]) => {
    if (!el.room || !zones[el.room] || !Array.isArray(zones[el.room].plan_xy)) return;
    const k = el.kind === "skylight" ? "skylight" : kind;
    entries.push({ id, el, kind: k, room: el.room,
                   side: PLAN_SIDES.includes(el.plan_side) ? el.plan_side : null,
                   t: el.plan_pos != null ? el.plan_pos : null, len: glyphLen(el, k) });
  });
  collect(meta.windows, "window");
  collect(meta.vents, "vent");

  // fallback: per kamer krijgt elke nog ongeplaatste gevel-azimut één buitenzijde
  ids.forEach(room => {
    const loose = entries.filter(e => e.room === room && !e.side);
    if (!loose.length) return;
    const sides = extSides(room); const azSide = {}; let si = 0;
    loose.forEach(e => {
      if (e.kind === "skylight") { e.side = "top"; return; }
      const az = String(e.el.facade_azimuth_deg ?? 0);
      if (!(az in azSide)) azSide[az] = sides[Math.min(si++, sides.length - 1)];
      e.side = azSide[az];
    });
  });

  // spreiden per (kamer, zijde): expliciete posities blijven, de rest gelijkmatig;
  // daarna minimale tussenafstand (halve glyphbreedtes + 6 px) afdwingen
  const groups = {};
  entries.forEach(e => (groups[`${e.room}|${e.side}`] = groups[`${e.room}|${e.side}`] || []).push(e));
  Object.values(groups).forEach(g => {
    const free = g.filter(e => e.t == null);
    free.forEach((e, i) => e.t = 0.12 + 0.76 * (i + 0.5) / free.length);
    const L = sideGeom(rectOf(g[0].room), g[0].side).len;
    g.sort((a, b) => a.t - b.t);
    for (let i = 1; i < g.length; i++) {
      const minPx = (g[i - 1].len + g[i].len) / 2 + 6;
      if ((g[i].t - g[i - 1].t) * L < minPx) g[i].t = g[i - 1].t + minPx / L;
    }
    g.forEach(e => e.t = Math.min(0.94, Math.max(0.06, e.t)));
  });

  const byId = {}, gaps = {};
  const addGap = (room, side, p0, p1) => {
    const bySide = gaps[room] = gaps[room] || {};
    (bySide[side] = bySide[side] || []).push([p0, p1]);
  };
  entries.forEach(e => {
    const rc = rectOf(e.room), L = sideGeom(rc, e.side).len, p = e.t * L;
    byId[e.id] = { kind: e.kind, room: e.room, side: e.side, t: e.t, len: e.len, pt: sidePoint(rc, e.side, e.t) };
    if (e.kind !== "skylight") addGap(e.room, e.side, p - e.len / 2, p + e.len / 2);
  });

  Object.entries(meta.doors || {}).forEach(([id, dr]) => {
    const [a, b] = dr.between || [];
    if (!zones[a] || !zones[b] || !Array.isArray(zones[a].plan_xy) || !Array.isArray(zones[b].plan_xy)) return;
    const se = sharedEdge(rectOf(a), rectOf(b));
    if (!se) return;
    const len = glyphLen(dr, "door");
    const c = se.lo + (dr.plan_pos != null ? dr.plan_pos : 0.5) * (se.hi - se.lo);
    const gA = sideGeom(rectOf(a), se.sideA), gB = sideGeom(rectOf(b), se.sideB);
    const tA = (c - (se.vertical ? gA.y0 : gA.x0)) / gA.len;
    const tB = (c - (se.vertical ? gB.y0 : gB.x0)) / gB.len;
    byId[id] = { kind: "door", a, b, len, fixed: !!dr.fixed,
                 ptA: sidePoint(rectOf(a), se.sideA, tA), ptB: sidePoint(rectOf(b), se.sideB, tB) };
    addGap(a, se.sideA, tA * gA.len - len / 2, tA * gA.len + len / 2);
    addGap(b, se.sideB, tB * gB.len - len / 2, tB * gB.len + len / 2);
  });

  return { byId, gaps };
}

function mergeIntervals(iv, len) {
  const cl = iv.map(([a, b]) => [Math.max(1, Math.min(a, b)), Math.min(len - 1, Math.max(a, b))])
               .filter(([a, b]) => b > a).sort((p, q) => p[0] - q[0]);
  const out = [];
  cl.forEach(([a, b]) => {
    if (out.length && a <= out[out.length - 1][1] + 1) out[out.length - 1][1] = Math.max(out[out.length - 1][1], b);
    else out.push([a, b]);
  });
  return out;
}

// Muur als dubbele inktlijn (buitenlijn op de celrand, binnenlijn WALL_T naar binnen),
// onderbroken bij openingen, met neggen (dagkant-streepjes) aan weerszijden van elk gat.
function wallBand(rc, sideGaps) {
  let s = "";
  PLAN_SIDES.forEach(side => {
    const g = sideGeom(rc, side);
    const at = (p, inset) => [g.x0 + g.tx * p - g.nx * inset, g.y0 + g.ty * p - g.ny * inset];
    const line = (p0, p1, inset, w) => {
      const [x1, y1] = at(p0, inset), [x2, y2] = at(p1, inset);
      return `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${COLORS.ink}" stroke-width="${w}"/>`;
    };
    const jamb = p => {
      const [x1, y1] = at(p, 0), [x2, y2] = at(p, WALL_T);
      return `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${COLORS.ink}" stroke-width="1"/>`;
    };
    const seg = (p0, p1) => {
      let t = line(p0, p1, 0, 1.1);
      const i0 = Math.max(p0, WALL_T), i1 = Math.min(p1, g.len - WALL_T);
      if (i1 - i0 > 0.5) t += line(i0, i1, WALL_T, 1.1);
      return t;
    };
    let prev = 0;
    mergeIntervals(sideGaps[side] || [], g.len).forEach(([a, b]) => {
      if (a - prev > 0.5) s += seg(prev, a);
      s += jamb(a) + jamb(b);
      prev = b;
    });
    if (g.len - prev > 0.5) s += seg(prev, g.len);
  });
  return s;
}

// Plansymbolen in het muurgat.
function windowGlyph(rc, side, t, len) {       // glas: dunne dubbele lijn in de muurband
  const g = sideGeom(rc, side), p = t * g.len, a = p - len / 2, b = p + len / 2;
  const at = (pp, inset) => [g.x0 + g.tx * pp - g.nx * inset, g.y0 + g.ty * pp - g.ny * inset];
  let s = "";
  [WALL_T * 0.32, WALL_T * 0.68].forEach(ins => {
    const [x1, y1] = at(a, ins), [x2, y2] = at(b, ins);
    s += `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${COLORS.ink}" stroke-width="0.9"/>`;
  });
  return s;
}
function ventGlyph(rc, side, t, len) {         // rooster: gestreepte sleuf + tikjes naar buiten
  const g = sideGeom(rc, side), p = t * g.len, a = p - len / 2, b = p + len / 2;
  const at = (pp, inset) => [g.x0 + g.tx * pp - g.nx * inset, g.y0 + g.ty * pp - g.ny * inset];
  const [x1, y1] = at(a, WALL_T / 2), [x2, y2] = at(b, WALL_T / 2);
  let s = `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${COLORS.inkSoft}" stroke-width="3" stroke-dasharray="2 1.6"/>`;
  [p - len / 4, p + len / 4].forEach(pp => {
    const [tx1, ty1] = at(pp, 0);
    s += `<line x1="${tx1.toFixed(1)}" y1="${ty1.toFixed(1)}" x2="${(tx1 + g.nx * 4).toFixed(1)}" y2="${(ty1 + g.ny * 4).toFixed(1)}" stroke="${COLORS.inkSoft}" stroke-width="1"/>`;
  });
  return s;
}
function skylightGlyph(rc, side, t) {          // plat dakraam: vierkantje met kruis óp de muur
  const p = sidePoint(rc, side, t), h = 8, x = p.x - h, y = p.y - h;
  return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="16" height="16" rx="2" fill="${COLORS.parchment}" stroke="${COLORS.ink}" stroke-width="1.1"/>`
    + `<line x1="${x.toFixed(1)}" y1="${y.toFixed(1)}" x2="${(x + 16).toFixed(1)}" y2="${(y + 16).toFixed(1)}" stroke="${COLORS.ink}" stroke-width="0.8" opacity="0.7"/>`
    + `<line x1="${(x + 16).toFixed(1)}" y1="${y.toFixed(1)}" x2="${x.toFixed(1)}" y2="${(y + 16).toFixed(1)}" stroke="${COLORS.ink}" stroke-width="0.8" opacity="0.7"/>`;
}
function doorGlyph(pl) {                       // doorgang over de rasterkier + draaisymbool
  const { ptA, ptB, len } = pl;
  const jamb = (pt, sgn) => [pt.x + pt.tx * sgn * len / 2, pt.y + pt.ty * sgn * len / 2];
  const [a1x, a1y] = jamb(ptA, -1), [a2x, a2y] = jamb(ptA, 1);
  const [b1x, b1y] = jamb(ptB, -1), [b2x, b2y] = jamb(ptB, 1);
  let s = `<line x1="${a1x.toFixed(1)}" y1="${a1y.toFixed(1)}" x2="${b1x.toFixed(1)}" y2="${b1y.toFixed(1)}" stroke="${COLORS.ink}" stroke-width="0.8" opacity="0.3"/>`
        + `<line x1="${a2x.toFixed(1)}" y1="${a2y.toFixed(1)}" x2="${b2x.toFixed(1)}" y2="${b2y.toFixed(1)}" stroke="${COLORS.ink}" stroke-width="0.8" opacity="0.3"/>`;
  if (!pl.fixed) {
    // deurblad vanaf de negge de kamer in + kwartcirkel (quadratisch benaderd)
    const inx = -ptA.nx, iny = -ptA.ny;
    const lx = a1x + inx * len, ly = a1y + iny * len;
    const cx = a1x + (ptA.tx + inx) * len, cy = a1y + (ptA.ty + iny) * len;
    s += `<line x1="${a1x.toFixed(1)}" y1="${a1y.toFixed(1)}" x2="${lx.toFixed(1)}" y2="${ly.toFixed(1)}" stroke="${COLORS.ink}" stroke-width="1" opacity="0.55"/>`;
    s += `<path d="M ${a2x.toFixed(1)} ${a2y.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${lx.toFixed(1)} ${ly.toFixed(1)}" fill="none" stroke="${COLORS.ink}" stroke-width="0.7" opacity="0.4" stroke-dasharray="2 2"/>`;
  }
  return s;
}

// Kamerinhoud op een vast tekstraster binnen de muren.
function roomContent(rc, r, z, stack, live) {
  const x = rc.x, y = rc.y, ix = x + WALL_T + 8;
  let s = `<rect x="${x + WALL_T}" y="${y + WALL_T}" width="${rc.w - 2 * WALL_T}" height="17" fill="#2a241b08"/>`;
  // geen verdieping-tag per kamer: de banden in de linkermarge tonen de verdieping al
  s += `<text x="${ix}" y="${y + WALL_T + 13}" font-size="11" font-family="'Cormorant Garamond',serif" font-style="italic" fill="${COLORS.ink}">${z.label || ""}</text>`;
  if (r.predicted_temp != null) {
    s += `<text x="${ix}" y="${y + 52}" font-size="22" font-weight="600" fill="${COLORS.ink}">${fmt(r.predicted_temp)}°</text>`;
    s += `<text x="${ix}" y="${y + 70}" font-size="9" fill="${COLORS.inkSoft}">model${r.actual_temp != null ? ' · tado ' + fmt(r.actual_temp) + '°' : ''}</text>`;
    s += `<text x="${ix}" y="${y + 86}" font-size="9" fill="${COLORS.inkSoft}">ACH ${fmt(r.ach, 1)}${r.humidity != null ? ' · RV ' + fmt(r.humidity, 0) + '%' : ''}</text>`;
    if (r.solar_w > 40) s += `<text x="${x + rc.w - WALL_T - 8}" y="${y + 52}" font-size="11" fill="${COLORS.sun}" text-anchor="end">☀ ${fmt(r.solar_w, 0)}W</text>`;
    if (live) {
      // Energie naar buiten (schil + ventilatie): − = warmte verlaat de kamer (koeling),
      // + = buitenlucht warmt 'm op. Drempel bewust laag (~8 W): in mild weer met dichte
      // ramen verliest een kamer maar tientallen W.
      const net = outsideNet(r);
      if (net != null && net < -8) s += `<text x="${ix}" y="${y + 103}" font-size="9" fill="${COLORS.rain}">❄ ${fmt(-net, 0)} W eruit</text>`;
      else if (net != null && net > 8) s += `<text x="${ix}" y="${y + 103}" font-size="9" fill="${COLORS.clay}">🔥 ${fmt(net, 0)} W erin</text>`;
      s += trendChip(rc, r.trend_c_per_h);
    }
  } else {
    s += `<text x="${ix}" y="${y + 54}" font-size="11" fill="${COLORS.inkSoft}" font-style="italic">geen sensor</text>`;
  }
  // verticale koker (trap): label in het lege middendeel, weg van energietekst en trend-chip
  if (stack) s += `<text x="${x + rc.w / 2}" y="${y + rc.h / 2}" font-size="9" fill="${COLORS.inkSoft}" text-anchor="middle" letter-spacing="1">↕ schoorsteen</text>`;
  return s;
}

// Trend als chip rechtsonder (verving de gekleurde kamerrand — de muur blijft inkt).
function trendChip(rc, trend) {
  if (trend == null || isNaN(trend)) return "";
  const oc = outlineColor(trend) || COLORS.inkSoft;
  const w = 58, h = 14, x = rc.x + rc.w - WALL_T - w - 4, y = rc.y + rc.h - WALL_T - h - 4;
  return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="7" fill="${rgbToRgba(oc, 0.13)}" stroke="${oc}" stroke-width="0.8"/>`
    + `<text x="${x + w / 2}" y="${y + 10}" font-size="8.5" font-weight="600" fill="${oc}" text-anchor="middle">${trendText(trend)}</text>`;
}

// Wind + zon in één omkaderd cartouche rechtsboven (i.p.v. los zwevende glyphs).
function cartouche(w, W) {
  const bw = 184, bh = 56, x0 = W - bw - 8, y0 = 6;
  let s = `<rect x="${x0}" y="${y0}" width="${bw}" height="${bh}" rx="4" fill="#2a241b06" stroke="${COLORS.ink}" stroke-width="0.8"/>`;
  const cx = x0 + 28, cy = y0 + bh / 2, R = 14;
  s += `<circle cx="${cx}" cy="${cy}" r="${R}" fill="none" stroke="${COLORS.inkSoft}" stroke-width="1" opacity="0.5"/>`;
  // Het plan is gedraaid (rechts = straat), niet noord-boven: zet de N-markering én de windpijl op
  // de gedraaide oriëntatie (PLAN_ROT), zodat de richting klopt t.o.v. de kamers.
  const nAng = PLAN_ROT * Math.PI / 180;
  s += `<line x1="${cx}" y1="${cy}" x2="${(cx + Math.sin(nAng) * R).toFixed(1)}" y2="${(cy - Math.cos(nAng) * R).toFixed(1)}" stroke="${COLORS.inkSoft}" stroke-width="0.8" opacity="0.55"/>`;
  s += `<text x="${(cx + Math.sin(nAng) * (R + 6)).toFixed(1)}" y="${(cy - Math.cos(nAng) * (R + 6) + 2.5).toFixed(1)}" font-size="6.5" fill="${COLORS.inkSoft}" text-anchor="middle">N</text>`;
  if (w.wind_dir != null) {
    const toRad = ((w.wind_dir + 180 + PLAN_ROT) % 360) * Math.PI / 180, R2 = R - 3;   // waarheen de wind waait, gedraaid naar het plan
    const tx = cx + Math.sin(toRad) * R2, ty = cy - Math.cos(toRad) * R2;
    const fx = cx - Math.sin(toRad) * R2, fy = cy + Math.cos(toRad) * R2;
    s += flowArrow(fx, fy, tx, ty, 2.2, Math.max(0.6, Math.min(2, 6 / Math.max(1, w.wind_speed || 1))), COLORS.rain);
  }
  const txx = x0 + 52;
  s += `<text x="${txx}" y="${y0 + 22}" font-size="9" fill="${COLORS.ink}">wind ${bftText(w.wind_speed)} ${dirName(w.wind_dir)}</text>`;
  if (w.sun_el != null && w.sun_el > 0) {
    s += `<text x="${txx}" y="${y0 + 40}" font-size="9" fill="${COLORS.inkSoft}">zon az ${fmt(w.sun_az, 0)}° · h ${fmt(w.sun_el, 0)}°</text>`;
    const sx = x0 + bw - 22, sy = y0 + bh / 2;
    s += `<circle cx="${sx}" cy="${sy}" r="13" fill="url(#sunhalo)"/>`;
    s += `<circle cx="${sx}" cy="${sy}" r="5" fill="${COLORS.sun}"/>`;
    for (let i = 0; i < 8; i++) {
      const a = i * Math.PI / 4;
      s += `<line x1="${(sx + Math.cos(a) * 7).toFixed(1)}" y1="${(sy + Math.sin(a) * 7).toFixed(1)}" x2="${(sx + Math.cos(a) * 10.5).toFixed(1)}" y2="${(sy + Math.sin(a) * 10.5).toFixed(1)}" stroke="${COLORS.sun}" stroke-width="1.3"/>`;
    }
  } else {
    s += `<text x="${txx}" y="${y0 + 40}" font-size="9" fill="${COLORS.inkSoft}">nacht</text>`;
    s += `<text x="${x0 + bw - 22}" y="${y0 + bh / 2 + 5}" font-size="13" text-anchor="middle">🌙</text>`;
  }
  return s;
}

// Gebogen stroompijl (kwadratische Bézier) — zelfde dash/duur-regels als flowArrow,
// pijlkop uit de eindtangent. Gebruikt om stromen dóór een muuropening te rijgen.
function flowArrowCurved(x0, y0, cx, cy, x1, y1, w, dur, col) {
  const ang = Math.atan2(y1 - cy, x1 - cx), ah = 6 + w;
  const head = `M ${x1} ${y1} L ${x1 - Math.cos(ang - 0.42) * ah} ${y1 - Math.sin(ang - 0.42) * ah} M ${x1} ${y1} L ${x1 - Math.cos(ang + 0.42) * ah} ${y1 - Math.sin(ang + 0.42) * ah}`;
  const dash = `${Math.max(4, w * 1.4).toFixed(0)} ${Math.max(7, w * 3).toFixed(0)}`;
  return `<path class="flowline airy" d="M ${x0} ${y0} Q ${cx} ${cy} ${x1} ${y1}" fill="none" stroke="${col}" stroke-width="${w.toFixed(1)}" stroke-linecap="round" stroke-dasharray="${dash}" style="animation-duration:${dur.toFixed(2)}s,3.5s"/>`
    + `<path class="airy" d="${head}" stroke="${col}" stroke-width="${w.toFixed(1)}" fill="none" stroke-linecap="round"/>`;
}

function floorPlanSVG(d, opts={}) {
  const live = opts.live !== false;           // false = speeltuin: temp/energie niet herrekend
  const meta = d.house_meta || {};
  const zones = Object.assign({}, meta.rooms||{}, meta.junctions||{});
  const ids = Object.keys(zones).filter(z => Array.isArray(zones[z].plan_xy));
  if (!ids.length) return '<div class="ctl-sub">Geen plattegrond — vul plan_xy in house_model.json.</div>';
  let minx=99,miny=99,maxx=-99,maxy=-99;
  ids.forEach(id => { const [x,y]=zones[id].plan_xy; minx=Math.min(minx,x); maxx=Math.max(maxx,x); miny=Math.min(miny,y); maxy=Math.max(maxy,y); });
  const { cw, ch, gap, padL, padT, padR, padB } = FP;
  const cols=maxx-minx+1, rowsN=maxy-miny+1;
  const W=padL+padR+cols*cw+(cols-1)*gap, H=padT+padB+rowsN*ch+(rowsN-1)*gap;
  const planH = id => zones[id].plan_h || 1;                       // hoeveel rijen hoog (trap = 3)
  const rectOf = id => { const x=padL+(zones[id].plan_xy[0]-minx)*(cw+gap), y=padT+(zones[id].plan_xy[1]-miny)*(ch+gap);
    return {x, y, w:cw, h:planH(id)*ch+(planH(id)-1)*gap}; };
  const ctr = id => { const r=rectOf(id); return [r.x+r.w/2, r.y+r.h/2]; };
  const border = (id, tx, ty) => { const r=rectOf(id);            // punt op de rand richting (tx,ty)
    return [Math.max(r.x, Math.min(r.x+r.w, tx)), Math.max(r.y, Math.min(r.y+r.h, ty))]; };
  const td  = id => (d.rooms||{})[id] || {};
  const placed = placeOpenings(meta, zones, ids, rectOf, planH);

  let defs = `<defs>
    <radialGradient id="sunhalo" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="${COLORS.sun}" stop-opacity="0.9"/><stop offset="100%" stop-color="${COLORS.sun}" stop-opacity="0"/></radialGradient>
  </defs>`;
  let s = `<svg class="fp-svg" viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;font-family:'JetBrains Mono',monospace;">${defs}`;

  // Verdieping-banden + labels links. Hoge, doorlopende zones (trap) tellen NIET mee voor
  // het verdieping-label van een rij, zodat de rij op hotties' hoogte "1e verdieping" toont.
  const floorsByRow = {};
  ids.forEach(id => { if (planH(id)>1) return; const yrow=zones[id].plan_xy[1]; floorsByRow[yrow]=zones[id].floor??0; });
  Object.keys(floorsByRow).forEach(yrow => {
    const y = padT+(yrow-miny)*(ch+gap)-gap/2;
    s += `<rect x="2" y="${y}" width="${W-4}" height="${ch+gap}" fill="${(+yrow)%2?'#2a241b04':'#2a241b00'}"/>`;
    s += `<text x="16" y="${y+(ch+gap)/2}" font-size="9" fill="${COLORS.inkSoft}" letter-spacing="1" transform="rotate(-90 16 ${y+(ch+gap)/2})" text-anchor="middle">${(FLOOR_NL[floorsByRow[yrow]]||'').toUpperCase()}</text>`;
  });

  // Zones: comfort-tint als "vloerafwerking" bínnen de muren; de muren als dubbele
  // architectenlijn met échte openingen erin (wallBand + glyphs hieronder).
  ids.forEach(id => {
    const r=td(id), z=zones[id], rc=rectOf(id);
    // "schoorsteen"-label alleen voor een echte verticale koker: een hoge cel die méér dan één
    // verdieping overspant (de trap). Een cel die alleen lager is doorgetrokken voor de
    // plattegrond-uitlijning (zoals de woonkamer, alles op de begane grond) is géén schoorsteen.
    const h=planH(id), [, py]=zones[id].plan_xy, fl=new Set();
    if (h>1) for (let rr=0; rr<h; rr++) if (floorsByRow[py+rr]!=null) fl.add(floorsByRow[py+rr]);
    const stack = fl.size>1;
    s += `<rect class="zone-rect" x="${rc.x+WALL_T}" y="${rc.y+WALL_T}" width="${rc.w-2*WALL_T}" height="${rc.h-2*WALL_T}" fill="${zoneFill(r)}"/>`;
    s += wallBand(rc, placed.gaps[id] || {});
    s += roomContent(rc, r, z, stack, live);
  });

  // Openingen: raam-/rooster-/dakraam-/deursymbolen in de muur.
  Object.values(placed.byId).forEach(pl => {
    if (pl.kind === "door") s += doorGlyph(pl);
    else if (pl.kind === "skylight") s += skylightGlyph(rectOf(pl.room), pl.side, pl.t);
    else if (pl.kind === "vent") s += ventGlyph(rectOf(pl.room), pl.side, pl.t, pl.len);
    else s += windowGlyph(rectOf(pl.room), pl.side, pl.t, pl.len);
  });

  // Stromen: geanimeerde, luchtige pijlen (dikte ∝ debiet, snelheid ∝ debiet) die door
  // de muuropening rijgen. Element zonder plaatsing → oude centrum-azimut-projectie.
  (d.flows||[]).forEach(f => {
    const q=Math.abs(f.flow_m3s); if (q<0.0025) return;     // verwaarloosbaar → niet tekenen
    const wd=Math.max(1.6, Math.min(8, q*15));
    const dur=Math.max(0.5, Math.min(2.6, 0.16/q));
    const pl = placed.byId[f.id];
    if (f.b==="outside") {
      const into = f.flow_m3s < 0, col = into?COLORS.rain:COLORS.clay;
      if (pl && pl.pt) {
        const p = pl.pt;
        const ox = p.x + p.nx*28 + p.tx*7, oy = p.y + p.ny*28 + p.ty*7;   // buiten
        const ixx = p.x - p.nx*19 - p.tx*6, iyy = p.y - p.ny*19 - p.ty*6; // binnen
        s += into ? flowArrowCurved(ox,oy,p.x,p.y,ixx,iyy,wd,dur,col)
                  : flowArrowCurved(ixx,iyy,p.x,p.y,ox,oy,wd,dur,col);
      } else {
        const win=(meta.windows||{})[f.id]||(meta.vents||{})[f.id]; const zid=(win&&win.room)||f.a; if(!zones[zid]) return;
        const rc=rectOf(zid);
        const az=((win?win.facade_azimuth_deg:0)||0)*Math.PI/180, ux=Math.sin(az), uy=-Math.cos(az);
        const edgeX=rc.x+rc.w/2+ux*(rc.w/2-4), edgeY=rc.y+rc.h/2+uy*(Math.min(rc.h,ch)/2-4);
        const outX=edgeX+ux*26, outY=edgeY+uy*26;
        s += into ? flowArrow(outX,outY,edgeX,edgeY,wd,dur,col) : flowArrow(edgeX,edgeY,outX,outY,wd,dur,col);
      }
    } else if (zones[f.a] && zones[f.b]) {
      const fwd = f.flow_m3s>=0;
      if (pl && pl.ptA) {
        const pa = pl.ptA, pb = pl.ptB;
        const ax = pa.x - pa.nx*18, ay = pa.y - pa.ny*18;   // binnen kamer a
        const bx = pb.x - pb.nx*18, by = pb.y - pb.ny*18;   // binnen kamer b
        const mx = (pa.x+pb.x)/2, my = (pa.y+pb.y)/2;       // midden in de deuropening
        s += fwd ? flowArrowCurved(ax,ay,mx,my,bx,by,wd,dur,COLORS.mossLight)
                 : flowArrowCurved(bx,by,mx,my,ax,ay,wd,dur,COLORS.mossLight);
      } else {
        const [ax,ay]=ctr(f.a),[bx,by]=ctr(f.b);
        const [pax,pay]=border(f.a,bx,by), [pbx,pby]=border(f.b,ax,ay);
        s += flowArrow(fwd?pax:pbx, fwd?pay:pby, fwd?pbx:pax, fwd?pby:pay, wd, dur, COLORS.mossLight);
      }
    }
  });

  s += cartouche(d.weather||{}, W);
  s += `</svg>`;
  return s;
}

function flowArrow(x1,y1,x2,y2,w,dur,col) {
  const ang=Math.atan2(y2-y1,x2-x1), ah=6+w;
  const head=`M ${x2} ${y2} L ${x2-Math.cos(ang-0.42)*ah} ${y2-Math.sin(ang-0.42)*ah} M ${x2} ${y2} L ${x2-Math.cos(ang+0.42)*ah} ${y2-Math.sin(ang+0.42)*ah}`;
  const dash=`${Math.max(4,w*1.4).toFixed(0)} ${Math.max(7,w*3).toFixed(0)}`;
  return `<line class="flowline airy" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${col}" stroke-width="${w.toFixed(1)}" stroke-linecap="round" stroke-dasharray="${dash}" style="animation-duration:${dur.toFixed(2)}s,3.5s"/>`+
    `<path class="airy" d="${head}" stroke="${col}" stroke-width="${w.toFixed(1)}" fill="none" stroke-linecap="round"/>`;
}

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
  state.rmseChart = new Chart(c, { type:"line", data:{ datasets:[{ label:"RMSE (°C)",
      data:hist.map(p=>({x:p.t,y:p.rmse})), borderColor:COLORS.clay, backgroundColor:COLORS.clay, borderWidth:2, pointRadius:0, tension:0.2 }]}, options:{
    responsive:true, maintainAspectRatio:false,
    scales:{ x:{type:"time", time:{unit:"day"}, grid:{display:false}, ticks:{font:{family:"JetBrains Mono",size:9}, color:COLORS.inkSoft}},
             y:{beginAtZero:true, grid:{color:"#2a241b11"}, ticks:{font:{family:"JetBrains Mono",size:9}, color:COLORS.inkSoft, callback:v=>v+"°"}} },
    plugins:{ legend:{display:false} }
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
function normState(v, kind) {
  const mid = kind==="shade" ? "half" : "tilt";
  if (typeof v === "number") return v>0.5 ? "open" : (v>0 ? mid : "dicht");
  v = (""+v).toLowerCase();
  if (v==="closed" || v==="toe") return "dicht";
  if (v==="kier") return mid;
  const allowed = kind==="shade" ? ["open","half","dicht"] : ["open","tilt","dicht"];
  if (allowed.includes(v)) return v;
  return kind==="window" ? "dicht" : "open";   // ramen default dicht; rest (vent/shade/door) open
}
function toggleModal(open) { document.getElementById("report-modal").classList.toggle("open", open); }

async function saveReport() {
  if (!ensureToken()) return;
  const btn = document.getElementById("report-save"); btn.textContent = "⋯ bewaren";
  try {
    const log = await fetchOpeningsLog();
    log.push({ t: new Date().toISOString(), states: { ...state.pending } });
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

// ===================== SPEELTUIN: INTERACTIEF LUCHTSTROOMMODEL =====================
// Een client-side port van het luchtstroomnetwerk uit airflow_model.py: identieke
// orifice-flow + Newton-druksolver, zodat je ramen/roosters/deuren kunt omzetten (en
// aan wind/buitentemp draaien) en meteen het effect op het debiet ziet — zonder
// server-rondgang en zonder iets op te slaan of het echte model te raken.
const SIM = { LEAK_AREA: 0.004, DP_LAM: 0.1, CP_AIR: 1005.0, G: 9.81, P_ATM: 101325.0, R_AIR: 287.05 };

function simConst(d) {
  const s = (d.house_meta && d.house_meta.sim) || {};
  if (s.leak_area != null) SIM.LEAK_AREA = s.leak_area;
  if (s.dp_lam != null) SIM.DP_LAM = s.dp_lam;
}
function airDensity(t) { return SIM.P_ATM / (SIM.R_AIR * (t + 273.15)); }
function cpCoeff(thetaDeg) { const t = thetaDeg * Math.PI / 180; return 0.475*Math.cos(t) + 0.3125*Math.cos(2*t) - 0.0875; }
function windPressure(facadeAz, height, windSpeed, windDir, shelter, rho) {
  const theta = Math.abs(((windDir - facadeAz + 180) % 360) - 180);
  const z = Math.max(1.5, height);
  const u = (windSpeed || 0) * Math.pow(z / 10, 0.30);
  return shelter * cpCoeff(theta) * 0.5 * rho * u * u;
}
function massflow(dP, Cd, area, rhoFrom, rhoTo) {
  if (area <= 0) return 0;
  const rho = dP >= 0 ? rhoFrom : rhoTo;
  const coef = Cd * area * Math.sqrt(2 * rho), a = Math.abs(dP);
  const m = a >= SIM.DP_LAM ? coef * Math.sqrt(a) : coef * Math.sqrt(SIM.DP_LAM) * (a / SIM.DP_LAM);
  return dP >= 0 ? m : -m;
}
function solveLinear(A, b) {            // Gauss-eliminatie met partieel pivoteren (== Python)
  const n = b.length, M = A.map((row, i) => row.concat([b[i]]));
  for (let col = 0; col < n; col++) {
    let piv = col;
    for (let r = col + 1; r < n; r++) if (Math.abs(M[r][col]) > Math.abs(M[piv][col])) piv = r;
    if (Math.abs(M[piv][col]) < 1e-12) return null;
    [M[col], M[piv]] = [M[piv], M[col]];
    const pv = M[col][col];
    for (let r = 0; r < n; r++) {
      if (r === col) continue;
      const f = M[r][col] / pv;
      if (f) for (let c = col; c <= n; c++) M[r][c] -= f * M[col][c];
    }
  }
  return M.map((row, i) => row[n] / row[i]);
}
function solveNetwork(zones, openings, zoneTemps, outsideTemp) {
  const idx = {}; zones.forEach((z, i) => idx[z] = i);
  const n = zones.length, rhoOut = airDensity(outsideTemp);
  const rhoZ = {}; zones.forEach(z => rhoZ[z] = airDensity(zoneTemps[z] ?? outsideTemp));
  function residual(P) {
    const res = new Array(n).fill(0);
    for (const op of openings) {
      const ia = idx[op.a], rhoA = rhoZ[op.a], PaEff = P[ia] - rhoA * SIM.G * op.z;
      let PbEff, rhoB;
      if (op.b === "outside") { PbEff = op.Pe - rhoOut * SIM.G * op.z; rhoB = rhoOut; }
      else { rhoB = rhoZ[op.b]; PbEff = P[idx[op.b]] - rhoB * SIM.G * op.z; }
      const md = massflow(PaEff - PbEff, op.Cd, op.area, rhoA, rhoB);
      res[ia] += md;
      if (op.b !== "outside") res[idx[op.b]] -= md;
    }
    return res;
  }
  const sse = r => r.reduce((s, v) => s + v * v, 0);
  let P = new Array(n).fill(0), r = residual(P);
  for (let it = 0; it < 40; it++) {
    if (Math.max(...r.map(Math.abs)) < 1e-6) break;
    const J = Array.from({ length: n }, () => new Array(n).fill(0)), eps = 0.02;
    for (let j = 0; j < n; j++) {
      P[j] += eps; const rp = residual(P); P[j] -= eps;
      for (let i = 0; i < n; i++) J[i][j] = (rp[i] - r[i]) / eps;
    }
    const delta = solveLinear(J, r.map(v => -v));
    if (!delta) break;
    const sse0 = sse(r); let alpha = 1.0, Ptry = P, rtry = r, ok = false;
    for (let ls = 0; ls < 24; ls++) {
      Ptry = P.map((p, j) => p + alpha * delta[j]); rtry = residual(Ptry);
      if (sse(rtry) < sse0) { ok = true; break; }
      alpha *= 0.5;
    }
    if (!ok) break;
    P = Ptry; r = rtry;
  }
  const flows = [], fresh = {}; zones.forEach(z => fresh[z] = 0);
  for (const op of openings) {
    const ia = idx[op.a], rhoA = rhoZ[op.a], PaEff = P[ia] - rhoA * SIM.G * op.z;
    let PbEff, rhoB;
    if (op.b === "outside") { PbEff = op.Pe - rhoOut * SIM.G * op.z; rhoB = rhoOut; }
    else { rhoB = rhoZ[op.b]; PbEff = P[idx[op.b]] - rhoB * SIM.G * op.z; }
    const md = massflow(PaEff - PbEff, op.Cd, op.area, rhoA, rhoB);
    flows.push(md / (md >= 0 ? rhoA : rhoB));
    if (op.b === "outside" && md < 0) fresh[op.a] += -md / rhoOut;
  }
  return { flows, fresh };
}
function openFracJS(value, el) {
  if (typeof value === "number") return Math.max(0, Math.min(1, value));
  const s = ("" + value).trim().toLowerCase();
  if (["open","1","true","ja"].includes(s)) return 1.0;
  if (["tilt","kier","kiep"].includes(s)) return el.tilt_frac ?? 0.15;
  return 0.0;   // dicht / closed / 0 / leeg
}
function defaultFracJS(el, kind) {
  if (el.default_state != null) return openFracJS(el.default_state, el);
  return ({ window: 0.0, vent: 1.0, door: 1.0 })[kind] ?? 0.0;
}
function buildOpeningsJS(meta, states, wind, params, outsideTemp) {
  const shelter = params.cp_shelter ?? 0.5, cd = params.cd ?? 0.62, rhoOut = airDensity(outsideTemp);
  const ws = wind.wind_speed || 0, wdir = wind.wind_dir || 0, ops = [];
  const zones = Object.keys(Object.assign({}, meta.rooms || {}, meta.junctions || {}));
  const ext = (id, el, kind) => {
    const frac = (id in states) ? openFracJS(states[id], el) : defaultFracJS(el, kind);
    const area = frac * (el.max_open_area_m2 ?? el.area_m2 ?? 0);
    if (area <= 0) return;
    const pe = windPressure(el.facade_azimuth_deg || 0, el.center_height_m ?? 1.5, ws, wdir, shelter, rhoOut);
    ops.push({ a: el.room, b: "outside", area, Cd: cd, z: el.center_height_m ?? 1.5, Pe: pe, id });
  };
  Object.entries(meta.windows || {}).forEach(([wid, w]) => ext(wid, w, "window"));
  Object.entries(meta.vents || {}).forEach(([vid, v]) => ext(vid, v, "vent"));
  Object.entries(meta.doors || {}).forEach(([did, dr]) => {
    const frac = (did in states) ? openFracJS(states[did], dr) : defaultFracJS(dr, "door");
    const area = frac * (dr.area_m2 ?? 0);
    if (area <= 0 || !dr.between) return;
    const [a, b] = dr.between;
    ops.push({ a, b, area, Cd: cd, z: dr.center_height_m ?? 1.0, Pe: 0, id: did });
  });
  zones.forEach(z => ops.push({ a: z, b: "outside", area: SIM.LEAK_AREA, Cd: cd, z: 1.5, Pe: 0, id: `_leak_${z}` }));
  return ops;
}

function renderSandbox() {
  const d = state.data; if (!d || !d.house_meta) return;
  simConst(d);
  const ctls = (d.controls || []).filter(c => ["window","vent","door"].includes(c.kind));
  if (!ctls.length) { document.getElementById("sandbox-card").style.display = "none"; return; }
  // Begintoestand = de huidige gerapporteerde stand (zelfde normalisatie als de modal).
  const states0 = {};
  ctls.forEach(c => states0[c.id] = normState(c.state, c.kind));
  const w = d.weather || {};
  state.sandbox = {
    states: states0,
    wind_speed: w.wind_speed ?? 3.0,
    wind_dir: w.wind_dir ?? 240,
    outside_temp: w.outside_temp ?? 18.0,
  };
  // Omgevings-sliders (wind + buitentemp) zodat je óók het weer kunt variëren.
  const env = document.getElementById("sandbox-env");
  env.innerHTML =
    sandboxSlider("wind_speed", "Windkracht", 0, 12, 1) +
    sandboxSlider("wind_dir", "Windrichting", 0, 360, 5) +
    sandboxSlider("outside_temp", "Buitentemperatuur", 8, 35, 0.5);
  env.querySelectorAll("input[type=range]").forEach(inp => inp.addEventListener("input", e => {
    const k = e.target.dataset.k, raw = parseFloat(e.target.value);
    // de windslider staat in Beaufort; het model rekent in m/s → terug naar de bandmidden-m/s
    state.sandbox[k] = k === "wind_speed" ? beaufortToMs(raw) : raw;
    const lab = document.getElementById("sbval-" + k);
    if (lab) lab.textContent = sandboxValTxt(k, state.sandbox[k]);
    sandboxRecompute();
  }));
  // Element-toggles, gegroepeerd zoals de modal.
  const groups = { window: "Ramen", vent: "Roosters", door: "Deuren" };
  const opts = { window: ["dicht","tilt","open"], vent: ["dicht","open"], door: ["dicht","open"] };
  let html = "";
  ["window","vent","door"].forEach(kind => {
    const items = ctls.filter(c => c.kind === kind);
    if (!items.length) return;
    html += `<div class="grp-title">${groups[kind]}</div>`;
    items.forEach(c => {
      const cur = states0[c.id];
      const sub = c.between ? c.between.join(" ↔ ") : (c.room || "");
      html += `<div class="ctl-row"><div><div class="ctl-label">${c.label}</div><div class="ctl-sub">${sub}</div></div>
        <div class="seg" data-id="${c.id}">${opts[kind].map(o =>
          `<button data-v="${o}" class="${cur===o?'active':''}">${o}</button>`).join("")}</div></div>`;
    });
  });
  const cc = document.getElementById("sandbox-controls");
  cc.innerHTML = html;
  cc.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", e => {
    const seg = e.target.closest(".seg"), id = seg.dataset.id;
    seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
    e.target.classList.add("active");
    state.sandbox.states[id] = e.target.dataset.v;
    sandboxRecompute();
  }));
  const rb = document.getElementById("sandbox-reset");
  rb.onclick = renderSandbox;
  sandboxRecompute();
}

function sandboxValTxt(k, v) {
  if (k === "wind_dir") return `${Math.round(v)}° ${dirName(v)}`;
  if (k === "wind_speed") return bftText(v);          // v in m/s → Beaufort
  return `${v.toFixed(1)}°C`;
}
function sandboxSlider(k, label, min, max, step) {
  const v = state.sandbox[k];
  const sliderVal = k === "wind_speed" ? beaufort(v) : v;   // windslider draait in Beaufort
  return `<div class="ctl-row"><div><div class="ctl-label">${label}</div>
      <div class="ctl-sub num" id="sbval-${k}">${sandboxValTxt(k, v)}</div></div>
    <input type="range" data-k="${k}" min="${min}" max="${max}" step="${step}" value="${sliderVal}" style="width:160px;max-width:48vw;accent-color:var(--moss);"></div>`;
}

function sandboxRecompute() {
  const d = state.data, meta = d.house_meta, sb = state.sandbox;
  const params = (d.learned && d.learned.params) || {};
  const rooms = meta.rooms || {}, junctions = meta.junctions || {};
  const zones = Object.keys(rooms).concat(Object.keys(junctions));
  // Zone-temperaturen: huidige model-temp per kamer; junctie/zonder sensor → buiten.
  const zoneTemps = {};
  zones.forEach(z => {
    const rt = (d.rooms || {})[z];
    zoneTemps[z] = (rt && rt.predicted_temp != null) ? rt.predicted_temp : sb.outside_temp;
  });
  const wind = { wind_speed: sb.wind_speed, wind_dir: sb.wind_dir };
  const ops = buildOpeningsJS(meta, sb.states, wind, params, sb.outside_temp);
  const net = solveNetwork(zones, ops, zoneTemps, sb.outside_temp);
  // Flow-objecten in dezelfde vorm als d.flows, zodat floorPlanSVG ze kan tekenen.
  const flows = [];
  ops.forEach((op, i) => { if (!op.id.startsWith("_leak_")) flows.push({ id: op.id, a: op.a, b: op.b, flow_m3s: Math.round(net.flows[i]*1e4)/1e4 }); });
  const simData = { house_meta: meta, rooms: d.rooms, flows,
    weather: { wind_dir: sb.wind_dir, wind_speed: sb.wind_speed,
               sun_az: (d.weather||{}).sun_az, sun_el: (d.weather||{}).sun_el } };
  document.getElementById("sandbox-plan").innerHTML = floorPlanSVG(simData, {live:false});
  // Uitlezing: per kamer ventilatievoud (ACH), verse buitenlucht (m³/u) en koeling (W).
  let totalFresh = 0, totalCool = 0, rows = "";
  Object.entries(rooms).forEach(([rid, rm]) => {
    const fresh = net.fresh[rid] || 0;             // m³/s verse buitenlucht in deze kamer
    const vol = rm.volume_m3 || 40;
    const ach = fresh * 3600 / vol;
    const coolW = 1.2 * SIM.CP_AIR * fresh * (zoneTemps[rid] - sb.outside_temp);
    totalFresh += fresh * 3600;
    if (coolW > 0) totalCool += coolW;
    const coolTxt = fresh < 1e-4 ? "—" : (coolW >= 0 ? "+" : "") + Math.round(coolW) + " W";
    const coolCls = coolW > 5 ? "err-neg" : (coolW < -5 ? "err-pos" : "");
    rows += `<tr><td>${rm.label || rid}</td><td class="num">${ach.toFixed(2)}</td>
      <td class="num">${Math.round(fresh*3600)}</td><td class="num ${coolCls}">${coolTxt}</td></tr>`;
  });
  const head = totalCool > 30
    ? `≈ <b class="num">${Math.round(totalCool)} W</b> nuttige koeling · <span class="num">${Math.round(totalFresh)}</span> m³/u verse lucht`
    : (totalFresh > 5
        ? `<span class="num">${Math.round(totalFresh)}</span> m³/u verse lucht — nauwelijks koeling bij deze stand`
        : `vrijwel dicht — <span class="num">${Math.round(totalFresh)}</span> m³/u verse lucht`);
  document.getElementById("sandbox-out").innerHTML =
    `<p style="font-style:italic;font-size:15px;margin:0 0 6px;">${head}</p>
     <table><thead><tr><th>kamer</th><th>ACH</th><th>verse m³/u</th><th>koeling</th></tr></thead>
     <tbody>${rows}</tbody></table>`;
}

// ===================== HELPERS =====================
function fmt(v, dec=1) { return (v==null || isNaN(v)) ? "—" : (+v).toFixed(dec); }
function num(v) { return v==null ? "—" : (+v).toFixed(2); }
function winLabel(wid) { const m=state.data.house_meta||{}; return (m.windows&&m.windows[wid]&&m.windows[wid].label)||wid; }
function roomLabel(rid) { const m=state.data.house_meta||{}; const z=(m.rooms&&m.rooms[rid])||(m.junctions&&m.junctions[rid]); return (z&&z.label)||rid; }
function dirName(deg) { if (deg==null) return ""; const dirs=["N","NO","O","ZO","Z","ZW","W","NW"]; return dirs[Math.round(((deg%360)/45))%8]; }
function windArrow(deg) { if (deg==null) return ""; const a=["↓","↙","←","↖","↑","↗","→","↘"]; return a[Math.round(((deg%360)/45))%8]; }
// Windsnelheid (m/s) → Beaufort (0–12) via de standaard WMO/KNMI-bovengrenzen, en terug naar een
// representatieve m/s (bandmidden) voor het model. bftText() is de losse weergavehelper ("X Bft").
const BEAUFORT_MAX_MS = [0.5, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7];
function beaufort(ms) {
  if (ms == null || isNaN(ms)) return null;
  let b = 0; while (b < BEAUFORT_MAX_MS.length && ms >= BEAUFORT_MAX_MS[b]) b++;
  return b;
}
function beaufortToMs(b) {
  const lo = b <= 0 ? 0 : BEAUFORT_MAX_MS[Math.min(b, BEAUFORT_MAX_MS.length) - 1];
  const hi = b < BEAUFORT_MAX_MS.length ? BEAUFORT_MAX_MS[b] : 36;
  return +((lo + hi) / 2).toFixed(1);
}
function bftText(ms) { const b = beaufort(ms); return b == null ? "—" : `${b} Bft`; }
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

// ===================== CONFIG =====================
// (CONFIG + Gist/token-logica komen uit js/shared.js)
const COLORS = { parchment:"#f3ecd9", ink:"#2a241b", inkSoft:"#5c4f3c", moss:"#3d5a3a", mossLight:"#6b8562", clay:"#b8532a", sun:"#d4a017", dry:"#a0421a" };
const NL_DAYS = ["maandag","dinsdag","woensdag","donderdag","vrijdag","zaterdag","zondag"];
const NL_MONTHS = ["","jan","feb","mrt","apr","mei","jun","jul","aug","sep","okt","nov","dec"];
const state = { data: null, chart: null, mowings: {}, pickedLen: 40 };

document.getElementById("folio-mark").textContent = `Terroir de Utrecht · Est. ${new Date().getFullYear()} · Gazon`;
document.getElementById("today-date").textContent = new Date().toLocaleDateString("nl-NL", { weekday:"long", day:"numeric", month:"long", year:"numeric" });
document.getElementById("mow-date").value = new Date().toISOString().slice(0,10);

document.getElementById("refresh-btn").addEventListener("click", loadData);
document.getElementById("mow-btn").addEventListener("click", openMowModal);
document.getElementById("mow-cancel").addEventListener("click", closeMowModal);
document.getElementById("mow-save").addEventListener("click", saveMow);
// Lengte-chips: listeners i.p.v. inline onclick (CSP zonder unsafe-inline).
document.querySelectorAll(".len-chip[data-len]").forEach(b =>
  b.addEventListener("click", () => pickLength(+b.dataset.len)));

// ===================== DATA =====================
async function loadData() {
  document.getElementById("banner-slot").innerHTML = "";
  document.getElementById("source-label").innerHTML = '<span class="pulse">⋯ data laden…</span>';
  try {
    const res = await fetch(bust("mowing_data.json"));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
    state.mowings = state.data.mowings || {};
    render();
  } catch (e) {
    document.getElementById("banner-slot").innerHTML =
      `<div class="banner banner-error"><strong>Kan data niet laden:</strong> ${e.message}. Heeft de GitHub Action al gedraaid?</div>`;
    document.getElementById("source-label").textContent = "";
  }
}

function fmtDate(iso) {
  const d = new Date(iso + "T00:00:00");
  return `${NL_DAYS[(d.getDay()+6)%7]} ${d.getDate()} ${NL_MONTHS[d.getMonth()+1]}`;
}
function shortLabel(iso) { const d = new Date(iso+"T00:00:00"); return `${d.getDate()}/${d.getMonth()+1}`; }

function daysUntil(iso) {
  if (!iso) return null;
  const a = new Date(state.data.today + "T00:00:00");
  const b = new Date(iso + "T00:00:00");
  return Math.round((b - a) / 86400000);
}

// ===================== RENDER =====================
function render() {
  const d = state.data;
  const src = d.source === "gdd_fallback" ? "vereenvoudigd model (geen bodemdata)" : "FAO-56 bodemmodel";
  document.getElementById("source-label").textContent =
    `Bron: ${src} · ververst ${new Date(d.generated_at).toLocaleString("nl-NL")}`;

  if (d.source === "gdd_fallback") {
    document.getElementById("banner-slot").innerHTML =
      `<div class="banner banner-warn">⚠ Vereenvoudigd groeimodel — bodemdata tijdelijk niet beschikbaar.</div>`;
  }

  const rec = d.recommended_length || {};
  const optimal = d.optimal_day;
  const n = daysUntil(d.predicted_next_mow);

  // Status badge + headline
  let badge, headline, sub;
  if (d.dormant) {
    badge = `<span class="badge badge-wait">Winterrust</span>`;
    headline = "Het gras groeit nauwelijks.";
    sub = "Geen maaibeurt nodig.";
  } else if (d.ready) {
    const over = optimal && optimal.overgrown;
    badge = `<span class="badge ${over ? "badge-over" : "badge-ready"}">${over ? "Te lang" : "Maairijp"}</span>`;
    headline = over ? "Het gras staat lang." : "Tijd om te maaien.";
    if (optimal && optimal.is_today) sub = `Vandaag is een goede maaidag (${optimal.reason}).`;
    else if (optimal) sub = `Beste maaidag: <b>${fmtDate(optimal.date)}</b> (${optimal.reason}).`;
    else sub = "Geen goede maaidag in de voorspelling — even afwachten.";
  } else {
    badge = `<span class="badge badge-wait">Nog niet</span>`;
    headline = (n != null && n >= 0) ? `Nog ~${n} ${n === 1 ? "dag" : "dagen"}.` : "Nog even groeien.";
    sub = d.predicted_next_mow ? `Verwachte maairijpheid rond <b>${fmtDate(d.predicted_next_mow)}</b>.` : "Verwachte datum nog onbekend.";
  }

  const lastMow = d.last_mow_assumed
    ? `${fmtDate(d.last_mow)} <span style="color:var(--ink-soft)">(aanname — log je eerste beurt)</span>`
    : `${fmtDate(d.last_mow)}${d.last_length_mm ? ` · ${d.last_length_mm}mm` : ""}`;

  const lenColor = rec.length_mm >= 50 ? "var(--moss)" : (rec.length_mm <= 30 ? "var(--clay)" : "var(--ink)");

  document.getElementById("content").innerHTML = `
    <div class="grid grid-2">
      <div class="specimen-card">
        <div class="corner-mark">Status</div>
        <div style="margin:8px 0 10px;">${badge}</div>
        <div class="card-title" style="margin-top:0;">${headline}</div>
        <p style="font-size:15px;color:var(--ink-soft);">${sub}</p>
        <div style="margin-top:16px;border-top:1px dashed #2a241b33;padding-top:12px;font-size:13px;">
          <div class="rule-label">Laatste maaibeurt</div>
          <div style="margin-top:2px;font-family:'JetBrains Mono',monospace;font-size:13px;">${lastMow}</div>
        </div>
        <div style="margin-top:12px;font-size:13px;">
          <div class="rule-label">Groei sinds maaien</div>
          <div style="margin-top:2px;font-family:'JetBrains Mono',monospace;">${d.accum_today.toFixed(1)} / ${d.params.READY_GU_effective.toFixed(1)} groei-eenheden${d.params.self_calibrated ? " <span style='color:var(--moss)'>(geleerd)</span>" : ""}</div>
        </div>
      </div>

      <div class="specimen-card">
        <div class="corner-mark">Advies maaihoogte</div>
        <div class="big-num" style="margin-top:10px;color:${lenColor};">${rec.length_mm || "—"}<span>mm</span></div>
        <p style="font-size:15px;color:var(--ink-soft);margin-top:10px;font-style:italic;">${rec.reason || ""}</p>
        ${d.last_length_mm && d.last_length_mm !== rec.length_mm
          ? `<p style="margin-top:10px;font-size:12px;color:var(--clay);font-family:'JetBrains Mono',monospace;">↑ vorige keer ${d.last_length_mm}mm — advies wijkt af</p>` : ""}
      </div>
    </div>

    <div class="grid">
      <div class="specimen-card">
        <div class="corner-mark">Grasgroei sinds laatste maaibeurt</div>
        <div class="chart-box"><canvas id="growth-chart"></canvas></div>
      </div>
    </div>

    <div class="grid">
      <div class="specimen-card">
        <div class="corner-mark">Maailogboek</div>
        <div id="history-table"></div>
      </div>
    </div>
  `;

  drawChart();
  renderHistory();
}

function drawChart() {
  const d = state.data;
  const series = d.series;
  const labels = series.map(s => shortLabel(s.date));
  const accum = series.map(s => s.accum);
  const todayIdx = series.findIndex(s => s.date === d.today);
  const thr = d.params.READY_GU_effective;

  const pointRadius = series.map(s => s.is_mow ? 5 : 0);
  const pointColor = series.map(s => s.is_mow ? COLORS.clay : "transparent");

  state.chart?.destroy();
  const ctx = document.getElementById("growth-chart").getContext("2d");
  state.chart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets: [{
      label: "Groei-accumulatie",
      data: accum,
      borderColor: COLORS.moss,
      backgroundColor: COLORS.moss + "22",
      borderWidth: 2, fill: true, tension: 0.3,
      pointRadius, pointBackgroundColor: pointColor, pointBorderColor: pointColor,
      segment: { borderDash: c => series[c.p1DataIndex]?.forecast ? [5,4] : undefined },
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: COLORS.parchment, titleColor: COLORS.ink, bodyColor: COLORS.ink,
          borderColor: COLORS.ink, borderWidth: 1,
          titleFont: { family: "JetBrains Mono", weight: 600, size: 11 },
          bodyFont: { family: "JetBrains Mono", size: 11 }, padding: 10,
          callbacks: {
            title: items => { const s = series[items[0].dataIndex]; return s.date + (s.forecast ? " (voorspelling)" : ""); },
            label: c => {
              const s = series[c.dataIndex];
              const lines = [`Groei: ${s.accum.toFixed(1)} eenheden`];
              if (s.is_mow) lines.push(`✂️ Gemaaid op ${s.length_mm}mm`);
              return lines;
            },
          },
        },
        annotation: { annotations: {
          thr: { type: "line", yMin: thr, yMax: thr, borderColor: COLORS.clay, borderWidth: 1.5, borderDash: [4,4],
                 label: { content: `Maairijp (${thr.toFixed(0)})`, display: true, position: "start", font: { family: "JetBrains Mono", size: 9 }, color: COLORS.clay, backgroundColor: "transparent" } },
          ...(todayIdx >= 0 ? { today: { type: "line", xMin: todayIdx, xMax: todayIdx, borderColor: COLORS.ink, borderWidth: 1, borderDash: [2,4],
                 label: { content: "vandaag →", display: true, position: "end", font: { family: "JetBrains Mono", size: 9 }, color: COLORS.ink, backgroundColor: "transparent" } } } : {}),
        }},
      },
      scales: {
        x: { grid: { color: "#2a241b11" }, ticks: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
        y: { beginAtZero: true, grid: { color: "#2a241b11" }, ticks: { font: { family: "JetBrains Mono", size: 9 }, color: COLORS.inkSoft },
             title: { display: true, text: "groei-eenheden", font: { family: "JetBrains Mono", size: 10 }, color: COLORS.inkSoft } },
      },
    },
  });
}

function renderHistory() {
  const el = document.getElementById("history-table");
  const dates = Object.keys(state.mowings).sort((a,b) => b.localeCompare(a));
  if (dates.length === 0) {
    el.innerHTML = `<p style="font-style:italic;color:var(--ink-soft);padding:8px 0;">Nog geen maaibeurten gelogd. Klik op "Ik heb gemaaid" om te beginnen.</p>`;
    return;
  }
  let rows = "";
  for (let i = 0; i < dates.length; i++) {
    const cur = dates[i], next = dates[i+1];
    let gap = "";
    if (next) gap = `${Math.round((new Date(cur) - new Date(next)) / 86400000)} dgn`;
    rows += `<tr><td>${fmtDate(cur)}</td><td>${state.mowings[cur].length_mm} mm</td><td style="color:var(--ink-soft)">${gap}</td></tr>`;
  }
  el.innerHTML = `<table><thead><tr><th>Datum</th><th>Hoogte</th><th>Interval</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// ===================== MODAL =====================
function pickLength(len) {
  state.pickedLen = len;
  document.querySelectorAll(".len-chip").forEach(c => c.classList.toggle("active", +c.dataset.len === len));
}

function openMowModal() {
  if (!ensureGistConfig()) return;
  document.getElementById("mow-modal").classList.add("open");
  updateMowLog();
}

function closeMowModal() {
  document.getElementById("mow-modal").classList.remove("open");
  document.getElementById("mow-status").textContent = "";
}

async function saveMow() {
  const date = document.getElementById("mow-date").value;
  const status = document.getElementById("mow-status");
  if (!date) { status.textContent = "⚠ Kies een datum."; status.style.color = "var(--dry)"; return; }

  status.innerHTML = '<span class="pulse">⋯ opslaan…</span>';
  status.style.color = "var(--ink-soft)";
  try {
    const mowings = await fetchGistMowings();
    mowings[date] = { length_mm: state.pickedLen };  // dubbele invoer zelfde dag → overschrijft
    await saveGistMowings(mowings);
    state.mowings = mowings;
    status.innerHTML = `✓ Opgeslagen: ${date} · ${state.pickedLen}mm`;
    status.style.color = "var(--moss)";
    updateMowLog();
    setTimeout(closeMowModal, 1800);
    await triggerWorkflow();
  } catch (e) {
    status.textContent = "✗ Fout: " + e.message;
    status.style.color = "var(--dry)";
  }
}

async function fetchGistMowings() {
  return await gistReadJSON("mowings.json", {});
}

async function saveGistMowings(data) {
  await gistWriteFile("mowings.json", JSON.stringify(data, null, 2));
}

const triggerWorkflow = () => dispatchWorkflow("mowing-notify.yml");

function updateMowLog() {
  const log = document.getElementById("mow-log");
  // Alleen strikte YYYY-MM-DD-keys renderen: de log komt uit de Gist en gaat
  // via innerHTML het DOM in — een misvormde key mag geen HTML/JS injecteren.
  const dates = Object.keys(state.mowings).filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d))
    .sort((a,b) => b.localeCompare(a)).slice(0, 8);
  if (dates.length === 0) {
    log.innerHTML = '<div style="color:var(--ink-soft);font-style:italic;padding:6px 0;">Nog niets gelogd.</div>';
    return;
  }
  log.innerHTML = dates.map(date =>
    `<div class="log-entry"><span>${date} · ${state.mowings[date].length_mm}mm</span><span class="rm" data-date="${date}">×</span></div>`
  ).join('');
}

// Gedelegeerde listener i.p.v. inline onclick met geïnterpoleerde data:
// data hoort nooit als code in een handler-string te belanden.
document.addEventListener("click", (e) => {
  const rm = e.target.closest("#mow-log .rm[data-date]");
  if (rm) removeMow(rm.dataset.date);
});

async function removeMow(date) {
  if (!confirm(`Maaibeurt van ${date} verwijderen?`)) return;
  const mowings = await fetchGistMowings();
  delete mowings[date];
  await saveGistMowings(mowings);
  state.mowings = mowings;
  updateMowLog();
  triggerWorkflow();
}

loadData();

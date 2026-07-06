const $ = id => document.getElementById(id);
let charts = {};

async function load() {
  let d;
  try {
    const r = await fetch(`insulation_data.json?t=${Date.now()}`);
    if (!r.ok) throw new Error(r.status);
    d = await r.json();
  } catch (e) {
    $("verdict-banner").innerHTML =
      `<div class="banner banner-error">Kon insulation_data.json niet laden — draai eerst insulation_analysis.py met minstens één kamer. (${e})</div>`;
    return;
  }
  render(d);
}

function render(d) {
  $("period").textContent = `${d.data_period.start} … ${d.data_period.end}`;
  $("source-label").textContent =
    `${d.rooms_covered.length} kamer(s) gedekt · ${new Date(d.generated_at).toLocaleString("nl-NL")}`;

  if (d.rooms_missing && d.rooms_missing.length) {
    $("missing-note").textContent =
      `${d.rooms_covered.length}/${d.rooms_covered.length + d.rooms_missing.length} kamers gedekt — nog geen export voor: ${d.rooms_missing.join(", ")}.`;
  }

  renderVerdict(d);
  renderCards(d);
  renderRankChart(d);
  renderTrendChart(d);
}

function renderVerdict(d) {
  const ranking = d.overall_ranking || [];
  if (!ranking.length) {
    $("verdict-banner").innerHTML =
      `<div class="banner banner-warn">Nog geen enkele kamer heeft genoeg 'verwarming-uit'-uren voor een betrouwbare schatting.</div>`;
    return;
  }
  const worst = ranking[ranking.length - 1];
  const room = d.rooms[worst];
  const msg = ranking.length === 1
    ? `Eén kamer geanalyseerd: <b>${room.geometry.label}</b> — ${room.narrative}`
    : `Minst geïsoleerd: <b>${room.geometry.label}</b> (UA ${room.ua_per_m2 != null ? room.ua_per_m2.toFixed(2) : "?"} W/K per m²). ${room.narrative}`;
  $("verdict-banner").innerHTML = `<div class="banner banner-warn"><span class="verdict" style="font-size:15px;font-style:italic;">${msg}</span></div>`;
}

function renderCards(d) {
  const el = $("room-cards");
  el.innerHTML = "";
  for (const rid of d.rooms_covered) {
    const room = d.rooms[rid];
    const card = document.createElement("div");
    card.className = "specimen-card";
    const rankBadge = room.rank ? `<span class="rank-badge">rang ${room.rank}/${room.rank_total}</span>` : "";
    card.innerHTML = `
      <div class="card-title">${room.geometry.label}${rankBadge}</div>
      <div class="big-num">${room.status === "ok" ? room.ua_w_per_k.toFixed(1) : "–"}<span>W/K</span></div>
      <div class="stat-row"><span class="lbl">Per m² buitengevel</span><span>${room.ua_per_m2 != null ? room.ua_per_m2.toFixed(2) + " W/K/m²" : "–"}</span></div>
      <div class="stat-row"><span class="lbl">Project 8 (48u online)</span><span>${room.online_compare ? room.online_compare.ua_total_w_per_k.toFixed(1) + " W/K" : "–"}</span></div>
      <div class="stat-row"><span class="lbl">Bruikbare uur-paren</span><span>${room.n_pairs} / ${room.n_hours_total}u</span></div>
      <div class="narrative">${room.narrative}</div>
    `;
    el.appendChild(card);
  }
}

function renderRankChart(d) {
  const ctx = $("rankChart");
  if (charts.rank) charts.rank.destroy();
  const rooms = (d.overall_ranking || []).map(rid => d.rooms[rid]);
  charts.rank = new Chart(ctx, {
    type: "bar",
    data: {
      labels: rooms.map(r => r.geometry.label),
      datasets: [{
        label: "UA per m² (W/K/m²)",
        data: rooms.map(r => r.ua_per_m2),
        backgroundColor: "#b8532a99",
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: { x: { title: { display: true, text: "W/K per m² buitengevel (lager = beter)" } } },
    },
  });
}

const TREND_COLORS = ["#b8532a", "#3d5a3a", "#4a6b8a", "#d4a017", "#2a241b"];

function renderTrendChart(d) {
  const ctx = $("trendChart");
  if (charts.trend) charts.trend.destroy();
  const datasets = d.rooms_covered.map((rid, i) => {
    const room = d.rooms[rid];
    return {
      label: room.geometry.label,
      data: (room.monthly_trend || []).map(m => ({ x: m.month, y: m.mean_delta_t })),
      borderColor: TREND_COLORS[i % TREND_COLORS.length],
      backgroundColor: "transparent",
      tension: 0.25,
    };
  });
  const allMonths = [...new Set(datasets.flatMap(ds => ds.data.map(p => p.x)))].sort();
  charts.trend = new Chart(ctx, {
    type: "line",
    data: { labels: allMonths, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { y: { title: { display: true, text: "gem. binnen − buiten (°C)" } } },
    },
  });
}

$("refresh-btn").addEventListener("click", load);
load();

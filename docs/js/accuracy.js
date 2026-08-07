const $ = id => document.getElementById(id);
const sign = v => (v >= 0 ? "+" : "") + v.toFixed(2);
const biasColor = v => v > 0.5 ? "#a0421a" : v < -0.5 ? "#4a6b8a" : "#6b8562";
let charts = {};

async function load() {
  let d;
  try {
    const r = await fetch(`accuracy_data.json?t=${Date.now()}`);
    if (!r.ok) throw new Error(r.status);
    d = await r.json();
  } catch (e) {
    $("verdict-banner").innerHTML =
      `<div class="banner banner-error">Kon accuracy_data.json niet laden — draai eerst de workflow “Weerstation-nauwkeurigheid”. (${e})</div>`;
    return;
  }
  render(d);
}

function render(d) {
  const o = d.overall, pm = d.sunny_afternoon, rest = d.rest;
  $("period").textContent = `${d.period.start} … ${d.period.end}`;
  // Het venster is een momentopname; het archief is waar de ijking op rust.
  const arc = d.archive;
  const arcTxt = arc && arc.n
    ? ` · archief ${arc.n} uren over ${arc.months.length} mnd (${arc.start} … ${arc.end})`
    : "";
  $("source-label").textContent =
    `${d.n_pairs} gekoppelde uren${arcTxt} · ${new Date(d.generated_at).toLocaleString("nl-NL")}`;

  renderRecalibration(d);
  renderReferences(d);
  renderNeighbours(d);

  $("overall-bias").innerHTML = `${sign(o.mean_bias)}<span>°C</span>`;
  $("overall-rmse").textContent = o.rmse.toFixed(2);
  $("overall-corr").textContent = o.corr ?? "–";
  $("overall-n").textContent = o.n;

  $("pm-bias").innerHTML = pm.n ? `${sign(pm.mean_bias)}<span>°C</span>` : "—";
  $("pm-rmse").textContent = pm.n ? pm.rmse.toFixed(2) : "–";
  $("pm-n").textContent = pm.n;
  $("rest-bias").textContent = rest.n ? sign(rest.mean_bias) + " °C" : "–";

  $("solar-slope").innerHTML = d.solar_slope_per_100 != null
    ? `${sign(d.solar_slope_per_100)}<span>°C</span>` : "—";
  $("wind-slope").textContent = d.wind_slope != null ? sign(d.wind_slope) : "–";

  // Verdict — interpreteer het patroon
  const night = d.diurnal.filter(x => (x.hour < 5 || x.hour > 22) && x.n)
                         .map(x => x.mean_bias);
  const nightBias = night.length ? night.reduce((a, b) => a + b, 0) / night.length : 0;
  let msg, cls;
  if (pm.n && pm.mean_bias > 1.0 && Math.abs(nightBias) < 0.5) {
    msg = `Sterke <b>stralingsfout</b>: het station leest op zonnige middagen ${sign(pm.mean_bias)} °C te warm, terwijl het 's nachts klopt (${sign(nightBias)} °C). Typisch een onvoldoende geventileerde stralingskap of een te zonnige plaatsing.`;
    cls = "banner-warn";
  } else if (Math.abs(o.mean_bias) > 0.8 && Math.abs(nightBias) > 0.6) {
    msg = `Vrijwel <b>constante afwijking</b> (dag én nacht ~${sign(nightBias)} °C). Dat lijkt op een ijk-offset eerder dan een stralingsfout.`;
    cls = "banner-warn";
  } else if (pm.n && pm.mean_bias > 0.5) {
    msg = `Milde stralingsfout op zonnige middagen (${sign(pm.mean_bias)} °C), 's nachts ${sign(nightBias)} °C. De zon-gevoeligheid is ${sign(d.solar_slope_per_100 ?? 0)} °C per 100 W/m².`;
    cls = "banner-ok";
  } else {
    msg = `Het station volgt het model goed (gem. ${sign(o.mean_bias)} °C, RMSE ${o.rmse.toFixed(2)}). Geen duidelijke stralingsfout.`;
    cls = "banner-ok";
  }
  $("verdict-banner").innerHTML = `<div class="banner ${cls}"><span class="verdict">${msg}</span></div>`;

  drawDiurnal(d);
  drawBins(d);
  drawCloud(d);
  drawScatter(d);
}

function destroy(k) { if (charts[k]) charts[k].destroy(); }

function drawDiurnal(d) {
  destroy("d");
  const rows = d.diurnal.filter(x => x.n);
  charts.d = new Chart($("diurnalChart"), {
    type: "bar",
    data: {
      labels: rows.map(x => x.hour + "u"),
      datasets: [{
        label: "afwijking °C",
        data: rows.map(x => x.mean_bias),
        backgroundColor: rows.map(x => biasColor(x.mean_bias)),
      }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { afterLabel: c => `n=${rows[c.dataIndex].n}, RMSE ${rows[c.dataIndex].rmse}` } },
        annotation: { annotations: { zero: { type: "line", yMin: 0, yMax: 0, borderColor: "#2a241b66", borderWidth: 1 } } },
      },
      scales: { y: { title: { display: true, text: "WU − model (°C)" } } },
    },
  });
}

function drawBins(d) {
  destroy("s");
  const rows = d.by_solar.filter(x => x.n);
  charts.s = new Chart($("solarChart"), {
    type: "bar",
    data: {
      labels: rows.map(x => x.label),
      datasets: [{ data: rows.map(x => x.mean_bias), backgroundColor: rows.map(x => biasColor(x.mean_bias)) }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { afterLabel: c => `n=${rows[c.dataIndex].n}` } } },
      scales: { x: { title: { display: true, text: "instraling W/m²" } }, y: { title: { display: true, text: "afwijking °C" } } },
    },
  });
}

function drawCloud(d) {
  destroy("c");
  const order = [["sunny", "zonnig"], ["partly", "half"], ["overcast", "bewolkt"]];
  const rows = order.map(([k, l]) => ({ l, ...d.by_cloud[k] })).filter(x => x.n);
  charts.c = new Chart($("cloudChart"), {
    type: "bar",
    data: {
      labels: rows.map(x => x.l),
      datasets: [{ data: rows.map(x => x.mean_bias), backgroundColor: rows.map(x => biasColor(x.mean_bias)) }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { afterLabel: c => `n=${rows[c.dataIndex].n}` } } },
      scales: { y: { title: { display: true, text: "afwijking °C" } } },
    },
  });
}

function drawScatter(d) {
  destroy("sc");
  // Bij voorkeur de eigen pyranometer: dát is de as waarop de correctie in
  // productie draait. Oudere artefacten dragen alleen de grid-instraling.
  const useWu = d.scatter.some(p => p.wu_solar != null);
  const key = useWu ? "wu_solar" : "solar";
  const pts = d.scatter.filter(p => p[key] != null);
  $("scatter-note").textContent = useWu
    ? "Elk punt = één uur, tegen de eigen pyranometer — de as waarop de correctie draait. De lijn is de uitgerolde helling."
    : "Elk punt = één uur, tegen de Open-Meteo-instraling (dit artefact draagt nog geen eigen pyranometer-kolom).";
  const maxSolar = Math.max(...pts.map(p => p[key]));
  const slope = d.recalibration ? d.recalibration.deployed : null;
  charts.sc = new Chart($("scatterChart"), {
    type: "scatter",
    data: {
      datasets: [{
        data: pts.map(p => ({ x: p[key], y: p.bias })),
        pointBackgroundColor: pts.map(p => {
          const t = Math.max(0, Math.min(1, (p.h - 4) / 14)); // 4u→18u
          return `hsl(${30 + 30 * t}, ${40 + 50 * t}%, ${30 + 25 * t}%)`;
        }),
        pointRadius: 3,
      }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => `${pts[c.dataIndex][key]} W/m² → ${sign(pts[c.dataIndex].bias)} °C (${pts[c.dataIndex].h}u)` } },
        annotation: {
          annotations: {
            zero: { type: "line", yMin: 0, yMax: 0, borderColor: "#2a241b66", borderWidth: 1 },
            // De uitgerolde correctie zelf, zodat je ziet wát er van de wolk wordt
            // afgetrokken in plaats van alleen dat er een wolk is.
            ...(slope ? {
              deployed: {
                type: "line", xMin: 0, yMin: 0, xMax: maxSolar, yMax: slope * maxSolar,
                borderColor: "#a0421a", borderWidth: 2, borderDash: [6, 4],
                label: { display: true, content: "uitgerolde correctie", position: "end",
                         font: { size: 10 }, backgroundColor: "#a0421acc" },
              },
            } : {}),
          },
        },
      },
      scales: {
        x: { title: { display: true, text: useWu ? "instraling W/m² (eigen pyranometer)" : "instraling W/m² (Open-Meteo)" } },
        y: { title: { display: true, text: "WU − referentie (°C)" } },
      },
    },
  });
}


// ── IJk-beslissing, referenties en buurstations ───────────────────────────────
// Deze drie panelen zijn de kern van een *ijk*-dashboard: mag de constante zoals
// hij is blijven, hoe hard is de referentie waar dat op rust, en is een afwijking
// die 's nachts blijft staan wel van ons station.

function renderRecalibration(d) {
  const rc = d.recalibration;
  const el = $("recal-verdict");
  if (!rc) { el.textContent = "Nog niet beoordeeld — draai de workflow opnieuw."; return; }
  if (rc.needed) {
    el.innerHTML = `De helling kan <b>herijkt</b> worden: <span class="num">${rc.deployed.toFixed(5)}</span> → `
      + `<span class="num">${rc.slope.toFixed(5)}</span>. Dat scheelt <b>${rc.delta_c.toFixed(2)} °C</b> `
      + `bij ${rc.reference_wm2.toFixed(0)} W/m² — een zonnige middag. Zet 'm met de hand in wu_bias.py.`;
    $("recal-evidence").textContent =
      `${rc.n} uren · ${rc.months.length} mnd · ref ${rc.reference} · driver ${rc.driver}`;
  } else {
    el.innerHTML = "De uitgerolde constante <b>houdt stand</b> tegen het protocol — "
      + "een herfitte helling wint niet out-of-sample. Niets te doen.";
    $("recal-evidence").textContent = "geen kandidaat haalt alle drie de poorten";
  }
  $("recal-deployed").textContent = `${rc.deployed.toFixed(5)} °C per W/m²`;
}

function renderReferences(d) {
  const refs = d.references || [];
  const t = $("ref-table");
  if (!refs.length) { t.innerHTML = ""; return; }
  const best = Math.min(...refs.map(r => r.sd));
  t.innerHTML =
    `<tr style="text-align:right;color:var(--ink-soft);">
       <th style="text-align:left;">referentie</th><th>n</th><th>bias</th><th>rmse</th><th>sd</th></tr>` +
    refs.map(r => {
      const mark = r.sd === best ? ' style="font-weight:600;"' : "";
      return `<tr${mark}><td style="text-align:left;padding:4px 0;">${r.label}</td>`
        + `<td style="text-align:right;">${r.n}</td>`
        + `<td style="text-align:right;">${sign(r.bias)}</td>`
        + `<td style="text-align:right;">${r.rmse.toFixed(2)}</td>`
        + `<td style="text-align:right;">${r.sd.toFixed(2)}</td></tr>`;
    }).join("");
}

function renderNeighbours(d) {
  const nb = d.neighbours;
  const verdictEl = $("neigh-verdict"), t = $("neigh-table");
  if (!nb) {
    verdictEl.textContent = "Geen buurstations geconfigureerd (secret WU_NEIGHBOUR_IDS).";
    t.innerHTML = "";
    return;
  }
  const kleur = { gedeeld: "#6b8562", uitzondering: "#a0421a", onbeslist: "#4a6b8a" }[nb.verdict];
  verdictEl.innerHTML = `<span style="color:${kleur};font-weight:600;">${nb.verdict.toUpperCase()}</span> — ${nb.explanation}`;

  const bins = (nb.ours.by_wind || []).map(b => `${b.lo}-${b.hi}`);
  const cel = v => v == null ? "–" : sign(v);
  const rij = (p, eigen) => {
    const st = eigen ? ' style="font-weight:600;"' : "";
    return `<tr${st}><td style="text-align:left;padding:4px 0;">${p.label}</td>`
      + `<td style="text-align:right;">${p.n}</td>`
      + `<td style="text-align:right;">${cel(p.night_bias)}</td>`
      + `<td style="text-align:right;">${cel(p.day_bias)}</td>`
      + `<td style="text-align:right;">${p.slope_per_100 == null ? "–" : sign(p.slope_per_100)}</td>`
      + (p.by_wind || []).map(b => `<td style="text-align:right;">${cel(b.bias)}</td>`).join("")
      + "</tr>";
  };
  t.innerHTML =
    `<tr style="text-align:right;color:var(--ink-soft);">
       <th style="text-align:left;">station</th><th>n</th><th>nacht</th><th>dag</th><th>°C/100W</th>`
    + bins.map(b => `<th>${b} km/h</th>`).join("") + "</tr>"
    + rij(nb.ours, true) + (nb.others || []).map(p => rij(p, false)).join("");
}

$("refresh-btn").addEventListener("click", load);
load();

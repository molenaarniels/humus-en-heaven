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
  $("source-label").textContent =
    `${d.n_pairs} gekoppelde uren · ref ${d.reference} · ${new Date(d.generated_at).toLocaleString("nl-NL")}`;

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
  const pts = d.scatter.filter(p => p.solar != null);
  charts.sc = new Chart($("scatterChart"), {
    type: "scatter",
    data: {
      datasets: [{
        data: pts.map(p => ({ x: p.solar, y: p.bias })),
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
        tooltip: { callbacks: { label: c => `${pts[c.dataIndex].solar} W/m² → ${sign(pts[c.dataIndex].bias)} °C (${pts[c.dataIndex].h}u)` } },
        annotation: { annotations: { zero: { type: "line", yMin: 0, yMax: 0, borderColor: "#2a241b66", borderWidth: 1 } } },
      },
      scales: {
        x: { title: { display: true, text: "instraling W/m²" } },
        y: { title: { display: true, text: "WU − model (°C)" } },
      },
    },
  });
}

$("refresh-btn").addEventListener("click", load);
load();

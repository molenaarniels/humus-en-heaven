// =============================================================================
// Het Model — interactieve uitleg
//
// Alle formules op deze pagina zijn een directe JS-port van soil_model.py.
// De waarden zijn illustratief; geen enkele input wordt gepersisteerd of
// teruggekoppeld naar het dashboard. Per ontwerp: geen fetch, geen
// localStorage, geen externe API-calls.
// =============================================================================

const COLORS = {
  parchment: "#f3ecd9", parchmentDeep: "#e8dec3",
  ink: "#2a241b", inkSoft: "#5c4f3c",
  moss: "#3d5a3a", mossLight: "#6b8562",
  clay: "#b8532a", sand: "#c9a978",
  rain: "#4a6b8a", sun: "#d4a017",
  dry: "#a0421a", wet: "#2d4a5c",
};

// --- Constants gemirrorde van soil_model.py ---------------------------------
const SOIL_FC = 0.20;
const SOIL_WP = 0.09;
const KC_MAX = 1.20;
const TEW = 18.0, REW = 8.0;
const UTRECHT_LAT = 52.0907;
const UTRECHT_ELEV = 5.0;

const ZONES = {
  lawn:   { Zr: 0.20, p: 0.40, fc: 0.95, fw: 1.00, C: 1.0, color: COLORS.mossLight },
  shrubs: { Zr: 0.50, p: 0.50, fc: 0.90, fw: 0.30, C: 1.5, color: COLORS.moss },
};

const KCB_SEASONAL = {
  lawn: [
    [1,0.36],[32,0.36],[60,0.59],[91,0.81],[121,0.86],[152,0.90],
    [182,0.90],[213,0.90],[244,0.77],[274,0.68],[305,0.45],[335,0.36],[365,0.36],
  ],
  shrubs: [
    [1,0.34],[32,0.34],[60,0.49],[91,0.69],[121,0.74],[152,0.83],
    [182,0.83],[213,0.83],[244,0.55],[274,0.46],[305,0.36],[335,0.34],[365,0.34],
  ],
};

function seasonalKcb(zone, doy) {
  const a = KCB_SEASONAL[zone];
  if (doy <= a[0][0]) return a[0][1];
  if (doy >= a[a.length-1][0]) return a[a.length-1][1];
  for (let i = 0; i < a.length - 1; i++) {
    const [d0,k0] = a[i], [d1,k1] = a[i+1];
    if (doy >= d0 && doy <= d1) {
      const t = (doy - d0) / (d1 - d0);
      return k0 + t * (k1 - k0);
    }
  }
  return 0.75;
}

function tempFactor(t) {
  if (t <= 5.0) return 0.0;
  if (t <= 8.0) return (t - 5.0) / 3.0;
  return 1.0;
}

// FAO-56 Penman-Monteith — port van penman_monteith_et0() in soil_model.py.
function penmanMonteith(Tmax, Tmin, RHmean, u2, Rs, elev, latRad, doy) {
  const Tmean = (Tmax + Tmin) / 2;
  const P = 101.3 * Math.pow((293 - 0.0065 * elev) / 293, 5.26);
  const gamma = 0.000665 * P;
  const delta = (4098 * (0.6108 * Math.exp((17.27 * Tmean) / (Tmean + 237.3)))) /
                Math.pow(Tmean + 237.3, 2);
  const eTmax = 0.6108 * Math.exp((17.27 * Tmax) / (Tmax + 237.3));
  const eTmin = 0.6108 * Math.exp((17.27 * Tmin) / (Tmin + 237.3));
  const es = (eTmax + eTmin) / 2;
  const ea = es * RHmean / 100;
  const dr = 1 + 0.033 * Math.cos(2 * Math.PI * doy / 365);
  const decl = 0.409 * Math.sin(2 * Math.PI * doy / 365 - 1.39);
  const ws = Math.acos(-Math.tan(latRad) * Math.tan(decl));
  const Ra = (24 * 60 / Math.PI) * 0.082 * dr * (
    ws * Math.sin(latRad) * Math.sin(decl) +
    Math.cos(latRad) * Math.cos(decl) * Math.sin(ws)
  );
  const Rso = (0.75 + 2e-5 * elev) * Ra;
  const Rns = (1 - 0.23) * Rs;
  const sigma = 4.903e-9;
  const Rnl = sigma * (
    (Math.pow(Tmax + 273.16, 4) + Math.pow(Tmin + 273.16, 4)) / 2
  ) * (0.34 - 0.14 * Math.sqrt(Math.max(ea, 0))) * (
    1.35 * Math.min(Math.max(Rs / Rso, 0.3), 1) - 0.35
  );
  const Rn = Rns - Rnl;
  const num = 0.408 * delta * Rn + gamma * (900 / (Tmean + 273)) * u2 * (es - ea);
  const den = delta + gamma * (1 + 0.34 * u2);
  return Math.max(num / den, 0);
}

// =============================================================================
// § 2 — ET₀ rekenmachine
// =============================================================================
function updateEt0() {
  const Tmax = parseFloat(document.getElementById("et0-tmax").value);
  const Tmin = Math.min(parseFloat(document.getElementById("et0-tmin").value), Tmax);
  const RH   = parseFloat(document.getElementById("et0-rh").value);
  const u2   = parseFloat(document.getElementById("et0-u2").value);
  const Rs   = parseFloat(document.getElementById("et0-rs").value);
  const doy  = parseInt(document.getElementById("et0-doy").value);
  document.getElementById("et0-tmax-val").textContent = Tmax.toFixed(1);
  document.getElementById("et0-tmin-val").textContent = Tmin.toFixed(1);
  document.getElementById("et0-rh-val").textContent = RH.toFixed(0);
  document.getElementById("et0-u2-val").textContent = u2.toFixed(1);
  document.getElementById("et0-rs-val").textContent = Rs.toFixed(1);
  document.getElementById("et0-doy-val").textContent = doy;
  const et0 = penmanMonteith(Tmax, Tmin, RH, u2, Rs, UTRECHT_ELEV, UTRECHT_LAT * Math.PI / 180, doy);
  document.getElementById("et0-out").textContent = et0.toFixed(2);
  let cap;
  if (et0 < 1.0) cap = "Heel weinig verdamping — winter of bewolkte koude dag.";
  else if (et0 < 2.5) cap = "Bescheiden ETo — voor- of najaar of bewolkt.";
  else if (et0 < 4.0) cap = "Normale lente/zomer-dag.";
  else if (et0 < 6.0) cap = "Stevige zomerdag met zon en droge lucht.";
  else cap = "Hittegolfomstandigheden — dit is fors.";
  document.getElementById("et0-caption").textContent = cap;
}
["et0-tmax","et0-tmin","et0-rh","et0-u2","et0-rs","et0-doy"].forEach(id => {
  document.getElementById(id).addEventListener("input", updateEt0);
});
updateEt0();

// =============================================================================
// § 3 — Kcb jaarchart
// =============================================================================
(function buildKcbChart() {
  const days = Array.from({length: 365}, (_, i) => i + 1);
  const lawn = days.map(d => seasonalKcb("lawn", d));
  const shrubs = days.map(d => seasonalKcb("shrubs", d));
  const today = new Date();
  const start = new Date(today.getFullYear(), 0, 0);
  const todayDoy = Math.floor((today - start) / 86400000);
  const monthLabels = ["Jan","Feb","Mrt","Apr","Mei","Jun","Jul","Aug","Sep","Okt","Nov","Dec"];
  const monthStarts = [1,32,60,91,121,152,182,213,244,274,305,335];
  new Chart(document.getElementById("kcb-chart"), {
    type: "line",
    data: {
      labels: days,
      datasets: [
        { label: "Lawn", data: lawn, borderColor: COLORS.mossLight, borderWidth: 2, pointRadius: 0, tension: 0.2 },
        { label: "Shrubs", data: shrubs, borderColor: COLORS.moss, borderWidth: 2, pointRadius: 0, tension: 0.2 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        annotation: {
          annotations: {
            today: {
              type: "line", xMin: todayDoy, xMax: todayDoy,
              borderColor: COLORS.clay, borderWidth: 1.5, borderDash: [4,4],
              label: { display: true, content: "vandaag", position: "start", backgroundColor: "transparent", color: COLORS.clay, font: { family: "JetBrains Mono", size: 9 } }
            }
          }
        },
        tooltip: {
          callbacks: { title: (items) => `Dag ${items[0].label}` }
        }
      },
      scales: {
        x: {
          ticks: {
            color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 },
            callback: function(val) {
              const d = this.getLabelForValue(val);
              const idx = monthStarts.indexOf(parseInt(d));
              return idx >= 0 ? monthLabels[idx] : "";
            },
            maxRotation: 0, autoSkip: false,
          },
          grid: { display: false }
        },
        y: {
          min: 0, max: 1.0,
          ticks: { color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 } },
          grid: { color: "#2a241b11" },
          title: { display: true, text: "Kcb", color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 } }
        }
      }
    }
  });
})();

// =============================================================================
// § 4 — Temp factor
// =============================================================================
let tfChart;
(function buildTfChart() {
  const xs = [];
  const ys = [];
  for (let t = -2; t <= 15; t += 0.25) { xs.push(t.toFixed(2)); ys.push(tempFactor(t)); }
  tfChart = new Chart(document.getElementById("tf-chart"), {
    type: "line",
    data: {
      labels: xs,
      datasets: [
        { label: "temp_factor", data: ys, borderColor: COLORS.wet, borderWidth: 2, pointRadius: 0, tension: 0.0, fill: { target: "origin", above: COLORS.wet + "22" } },
        { label: "huidige", data: xs.map(x => parseFloat(x) === 10 ? 1.0 : null), borderColor: COLORS.clay, backgroundColor: COLORS.clay, pointRadius: 0 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        annotation: {
          annotations: {
            lo: { type: "line", xMin: "5.00", xMax: "5.00", borderColor: COLORS.dry, borderWidth: 1, borderDash: [3,3] },
            hi: { type: "line", xMin: "8.00", xMax: "8.00", borderColor: COLORS.moss, borderWidth: 1, borderDash: [3,3] },
            cursor: { type: "point", xValue: "10.00", yValue: 1.0, radius: 5, backgroundColor: COLORS.clay, borderColor: COLORS.clay }
          }
        },
        tooltip: { callbacks: { title: (i) => `Tmean ${i[0].label} °C` } }
      },
      scales: {
        x: {
          ticks: {
            color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 },
            callback: function(val) { const v = parseFloat(this.getLabelForValue(val)); return Number.isInteger(v) && v % 5 === 0 ? v + "°" : ""; },
            maxRotation: 0, autoSkip: false,
          },
          grid: { display: false }
        },
        y: { min: 0, max: 1.05, ticks: { color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 } }, grid: { color: "#2a241b11" } }
      }
    }
  });
})();
function updateTf() {
  const t = parseFloat(document.getElementById("tf-t").value);
  document.getElementById("tf-t-val").textContent = t.toFixed(1);
  const f = tempFactor(t);
  document.getElementById("tf-out").textContent = f.toFixed(2);
  let cap;
  if (f === 0) cap = "Volledig gedempt — geen verdamping in het model.";
  else if (f < 0.5) cap = "Sterk gedempt — vroeg voorjaar, planten kruipen op gang.";
  else if (f < 1.0) cap = "Geleidelijke opstart.";
  else cap = "Volledig actief — transpiratie wordt niet gedempt.";
  document.getElementById("tf-caption").textContent = cap;
  // cursor on chart
  const ann = tfChart.options.plugins.annotation.annotations.cursor;
  ann.xValue = t.toFixed(2);
  ann.yValue = f;
  tfChart.update("none");
}
document.getElementById("tf-t").addEventListener("input", updateTf);
updateTf();

// =============================================================================
// § 5 — Interceptie
// =============================================================================
let intChart;
(function buildIntChart() {
  const xs = [];
  const ysLawn = [];
  const ysShrubs = [];
  for (let p = 0; p <= 20; p += 0.2) {
    xs.push(p.toFixed(1));
    ysLawn.push(ZONES.lawn.C * (1 - Math.exp(-p / ZONES.lawn.C)));
    ysShrubs.push(ZONES.shrubs.C * (1 - Math.exp(-p / ZONES.shrubs.C)));
  }
  intChart = new Chart(document.getElementById("int-chart"), {
    type: "line",
    data: {
      labels: xs,
      datasets: [
        { label: "Gras", data: ysLawn, borderColor: COLORS.mossLight, borderWidth: 2, pointRadius: 0, tension: 0.0 },
        { label: "Struiken", data: ysShrubs, borderColor: COLORS.moss, borderWidth: 2, pointRadius: 0, tension: 0.0 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "bottom", labels: { color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 }, boxWidth: 12 }
        },
        annotation: {
          annotations: {
            cursor: { type: "line", xMin: "3.0", xMax: "3.0", borderColor: COLORS.clay, borderWidth: 1, borderDash: [3,3] }
          }
        },
        tooltip: { callbacks: { title: (i) => `P = ${i[0].label} mm`, label: (i) => `${i.dataset.label}: ${i.raw.toFixed(2)} mm onderschept` } }
      },
      scales: {
        x: {
          ticks: { color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 }, callback: function(val) { const v = parseFloat(this.getLabelForValue(val)); return Number.isInteger(v) && v % 5 === 0 ? v + " mm" : ""; }, maxRotation: 0, autoSkip: false },
          grid: { display: false },
          title: { display: true, text: "Neerslag P", color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 } }
        },
        y: { min: 0, max: 2.0, ticks: { color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 } }, grid: { color: "#2a241b11" }, title: { display: true, text: "I (mm onderschept)", color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 } } }
      }
    }
  });
})();
function updateInt() {
  const P = parseFloat(document.getElementById("int-p").value);
  document.getElementById("int-p-val").textContent = P.toFixed(1);
  const il = ZONES.lawn.C * (1 - Math.exp(-P / ZONES.lawn.C));
  const is = ZONES.shrubs.C * (1 - Math.exp(-P / ZONES.shrubs.C));
  document.getElementById("int-lawn-i").textContent = il.toFixed(2) + " mm";
  document.getElementById("int-lawn-t").textContent = (P - il).toFixed(2) + " mm";
  document.getElementById("int-shrubs-i").textContent = is.toFixed(2) + " mm";
  document.getElementById("int-shrubs-t").textContent = (P - is).toFixed(2) + " mm";
  intChart.options.plugins.annotation.annotations.cursor.xMin = P.toFixed(1);
  intChart.options.plugins.annotation.annotations.cursor.xMax = P.toFixed(1);
  intChart.update("none");
}
document.getElementById("int-p").addEventListener("input", updateInt);
updateInt();

// =============================================================================
// § 6 — Twee buckets simulator
// =============================================================================
const bucket = {
  zone: "lawn",
  day: 0,
  water: 0,      // mm boven WP in wortelzone
  De: REW,       // depletie oppervlaktelaag (mm)
};
function bucketReset() {
  bucket.zone = bucket.zone || "lawn";
  bucket.day = 0;
  // Start: 30% depletie (zelfde als seed default in soil_model.py:219)
  const z = ZONES[bucket.zone];
  const AWC = (SOIL_FC - SOIL_WP) * z.Zr * 1000;
  bucket.water = AWC * 0.7;
  bucket.De = REW;
  bucketRender();
}
function bucketRender() {
  const z = ZONES[bucket.zone];
  const AWC = (SOIL_FC - SOIL_WP) * z.Zr * 1000;
  const RAW = AWC * z.p;
  const depletion = Math.max(0, AWC - bucket.water);
  const depPct = AWC > 0 ? depletion / AWC * 100 : 0;
  const theta = SOIL_WP + bucket.water / (z.Zr * 1000);

  document.getElementById("bk-day").textContent = bucket.day;
  document.getElementById("bk-theta").textContent = theta.toFixed(3);
  document.getElementById("bk-dep").textContent = depPct.toFixed(0);
  document.getElementById("bk-awc").textContent = AWC.toFixed(1);
  document.getElementById("bk-raw").textContent = RAW.toFixed(1);
  document.getElementById("bk-de").textContent = bucket.De.toFixed(1);

  // SVG fills
  // Surface bar: blue height proportional to (TEW - De) / TEW
  const surfFrac = Math.max(0, Math.min(1, (TEW - bucket.De) / TEW));
  const surfFill = document.getElementById("bk-surf-fill");
  const surfH = 36 * surfFrac;
  surfFill.setAttribute("y", (22 + (36 - surfH)).toFixed(1));
  surfFill.setAttribute("height", surfH.toFixed(1));

  // Root bar: green height proportional to water / AWC (top of root box = FC, bottom = WP)
  const rootFrac = AWC > 0 ? Math.max(0, Math.min(1, bucket.water / AWC)) : 0;
  const rootFill = document.getElementById("bk-root-fill");
  const rootH = 158 * rootFrac;
  rootFill.setAttribute("y", (258 - rootH).toFixed(1));
  rootFill.setAttribute("height", rootH.toFixed(1));

  // p·AWC line position (depletion = p·AWC reached when water = AWC·(1-p))
  const pLineY = 100 + 158 * z.p;
  document.getElementById("bk-p-line").setAttribute("y1", pLineY.toFixed(1));
  document.getElementById("bk-p-line").setAttribute("y2", pLineY.toFixed(1));
}

function bucketApplyDay(rain, irrig, ET0) {
  const z = ZONES[bucket.zone];
  const AWC = (SOIL_FC - SOIL_WP) * z.Zr * 1000;
  const RAW = AWC * z.p;
  const doy = (new Date().getDate() < 1 ? 1 : Math.floor((new Date() - new Date(new Date().getFullYear(),0,0)) / 86400000)) || 172;
  const kcb = seasonalKcb(bucket.zone, doy);
  // temp_factor = 1 voor de simulator (we simuleren een actieve dag);
  // de koude-drempel-knop staat in § 4.
  const tf = 1.0;
  const kcbEff = kcb * tf;

  // Interceptie van regen
  const rainNet = rain > 0 ? rain - z.C * (1 - Math.exp(-rain / z.C)) : 0;
  const irr = irrig || 0;
  const wetting = rainNet + irr;

  // De daalt door wetting
  bucket.De = Math.max(0, bucket.De - wetting);

  // few
  let fwEff;
  if (wetting > 0 && rainNet > 0) fwEff = 1.0;
  else if (irr > 0) fwEff = z.fw;
  else fwEff = 1.0;
  const few = Math.max(0.01, Math.min(1.0 - z.fc, fwEff));

  // Kr
  let Kr;
  if (bucket.De <= REW) Kr = 1.0;
  else if (bucket.De >= TEW) Kr = 0.0;
  else Kr = (TEW - bucket.De) / (TEW - REW);
  const Ke = Math.max(0.0, Math.min(Kr * (KC_MAX - kcbEff), few * KC_MAX)) * tf;

  // Ks + T
  const depletion = Math.max(0, AWC - bucket.water);
  const Ks = depletion <= RAW ? 1.0 : Math.max(0, (AWC - depletion) / (AWC - RAW));
  const T = kcbEff * Ks * ET0;

  // E begrensd door beschikbaar in oppervlak
  const Eavail = Math.max(0, TEW - bucket.De);
  const E = Math.min(Ke * ET0, Eavail);
  bucket.De = Math.min(TEW, bucket.De + E);

  // Update wortelzone: wetting in, E + T uit
  bucket.water += wetting - T - E;
  let drain = 0;
  if (bucket.water > AWC) { drain = bucket.water - AWC; bucket.water = AWC; }
  if (bucket.water < 0) bucket.water = 0;

  bucket.day += 1;

  // Update side-readouts
  document.getElementById("bk-kcb").textContent = kcb.toFixed(2);
  document.getElementById("bk-ks").textContent = Ks.toFixed(2);
  document.getElementById("bk-e").textContent = E.toFixed(2);
  document.getElementById("bk-t").textContent = T.toFixed(2);
  document.getElementById("bk-etc").textContent = (E + T).toFixed(2);
  document.getElementById("bk-drain").textContent = drain.toFixed(2);

  bucketRender();
}

document.querySelectorAll("[data-bucket-zone]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-bucket-zone]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    bucket.zone = btn.dataset.bucketZone;
    bucketReset();
  });
});
document.getElementById("b-rain-2").addEventListener("click", () => bucketApplyDay(2, 0, 0));
document.getElementById("b-rain-10").addEventListener("click", () => bucketApplyDay(10, 0, 0));
document.getElementById("b-irrig").addEventListener("click", () => bucketApplyDay(0, 8, 0));
document.getElementById("b-day").addEventListener("click", () => bucketApplyDay(0, 0, 4));
document.getElementById("b-reset").addEventListener("click", bucketReset);
bucketReset();

// =============================================================================
// § 7 — Ks stress chart
// =============================================================================
(function buildKsChart() {
  const xs = [];
  const ysLawn = [];
  const ysShrubs = [];
  for (let d = 0; d <= 100; d += 1) {
    xs.push(d);
    const dep = d / 100;
    for (const [arr, p] of [[ysLawn, ZONES.lawn.p], [ysShrubs, ZONES.shrubs.p]]) {
      if (dep <= p) arr.push(1.0);
      else arr.push(Math.max(0, (1 - dep) / (1 - p)));
    }
  }
  new Chart(document.getElementById("ks-chart"), {
    type: "line",
    data: {
      labels: xs,
      datasets: [
        { label: "Gras", data: ysLawn, borderColor: COLORS.mossLight, borderWidth: 2, pointRadius: 0, tension: 0.0 },
        { label: "Struiken", data: ysShrubs, borderColor: COLORS.moss, borderWidth: 2, pointRadius: 0, tension: 0.0 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        annotation: {
          annotations: {
            pLawn: { type: "line", xMin: 40, xMax: 40, borderColor: COLORS.mossLight, borderWidth: 1, borderDash: [3,3], label: { display: true, content: "p=0.40", position: "start", color: COLORS.mossLight, backgroundColor: "transparent", font: { family: "JetBrains Mono", size: 9 } } },
            pShrubs: { type: "line", xMin: 50, xMax: 50, borderColor: COLORS.moss, borderWidth: 1, borderDash: [3,3], label: { display: true, content: "p=0.50", position: "end", color: COLORS.moss, backgroundColor: "transparent", font: { family: "JetBrains Mono", size: 9 } } },
          }
        },
        tooltip: { callbacks: { title: (i) => `Depletie ${i[0].label}%` } }
      },
      scales: {
        x: {
          ticks: { color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 }, callback: (v) => v % 20 === 0 ? v + "%" : "", maxRotation: 0, autoSkip: false },
          grid: { display: false },
          title: { display: true, text: "Depletie als % van AWC", color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 } }
        },
        y: { min: 0, max: 1.05, ticks: { color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 } }, grid: { color: "#2a241b11" }, title: { display: true, text: "Ks", color: COLORS.inkSoft, font: { family: "JetBrains Mono", size: 10 } } }
      }
    }
  });
})();

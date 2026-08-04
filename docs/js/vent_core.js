// ===================== VENT CORE — de thermische kern in de browser =====================
// De helft van vent_physics.py die client-side móét draaien, omdat het de helft is die van
// de raam- en deurstanden afhangt. Alles wat dat níét doet — zonnegeometrie, hoekafhankelijke
// glastransmissie, beschaduwing, dak-instraling, het buur- en bodemanker — is al door
// vent_forecast.driver_export uitgerekend en komt binnen als vent_forecast.json.
//
// De dure meerzone-Newton-druksolver is vervangen door het gedistilleerde surrogaat
// (js/surrogate.json): een MLP getraind op ~1 miljoen UNIFORM GETROKKEN raamstanden uit de
// solver zelf. Uniform getrokken, want dát is het experiment dat het echte huis nooit kan
// draaien — de openingen-log heeft ~90 aan/uit-events per element en is in béíde richtingen
// geconfound (terrasdeuren gaan open als het warm is, het keukenkiepraam juist dicht op de
// heetste dagen). Een model dat op die log getraind is, leert "terrasdeur open → kamer
// warmer", precies andersom, en de speeltuin is per definitie een counterfactual over exact
// die elementen. Gedistilleerd uit de solver erft het surrogaat wél de causale structuur.
//
// Wat hier dus overblijft:
//     standen → surrogaat → fresh/mix → ventilatie- + deurgeleiding
//            → trap-stratificatie + drijvende counterflow
//            → 2-knoops RC-assemblage → 14×14 stelsel per substap
//
// Juistheid staat vast via de golden-vector: tools/export_driver_timeline.py legt de exacte
// Ta/Tm-baan vast die vp.simulate voor een vaste tijdlijn + zaad produceert, en
// `node tools/test_golden.js` eist die tot ~1e-9 terug. Zonder die test kan een echte
// portfout zich in het foutbudget van het surrogaat verstoppen — in de offline sessie
// gebeurde dat ook: een verdwaalde NUL-byte in een template-literal op drie van de zes
// plekken die zone-paar-sleutels bouwen, goed voor 9.8e-2 °C en volstrekt onzichtbaar in de
// broncode én in een diff. Vandaar `pairKey`: één helper, dus de twee kanten kúnnen het niet
// oneens zijn over het sleutelformaat.
//
// Plain globals, geen modules (zoals speeltuin.js) — de CSP staat geen inline scripts toe en
// het project heeft bewust geen build step. Onderaan staat een CommonJS-export zodat de
// golden-test dit bestand ongewijzigd in node kan laden.

/** Canonieke sleutel voor een zone-paar. Álles wat zo'n sleutel bouwt of opzoekt moet hier
 *  doorheen — zie de NUL-byte-anekdote in de kop. */
function pairKey(a, b) { return `${a}|${b}`; }

// ---------------------------------------------------------------- lineaire algebra

/** Gauss-eliminatie met partieel pivoteren. Mirror van vp.solve_linear, inclusief de
 *  bijna-singuliere bail-out (null), die de aanroeper als "bevries deze stap" behandelt.
 *  Bewust NIET `solveLinear` genoemd: speeltuin.js definieert die naam al globaal en twee
 *  top-level functiedeclaraties met dezelfde naam overschrijven elkaar stilzwijgend. */
function rcSolveLinear(A, b) {
  const n = b.length;
  const M = A.map((row, i) => [...row, b[i]]);
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
  return Array.from({ length: n }, (_, i) => M[i][n] / M[i][i]);
}

// ------------------------------------------------------------------- surrogaat

/** IEEE754 half → double. Oudere engines hebben geen DataView-float16, dus met de hand. */
function f16ToF32(h) {
  const s = (h & 0x8000) >> 15, e = (h & 0x7c00) >> 10, f = h & 0x03ff;
  if (e === 0) return (s ? -1 : 1) * Math.pow(2, -14) * (f / 1024);
  if (e === 0x1f) return f ? NaN : (s ? -Infinity : Infinity);
  return (s ? -1 : 1) * Math.pow(2, e - 15) * (1 + f / 1024);
}

/** Gewichten zijn óf gewone geneste arrays óf base64-float16 (zie --fp16 in de trainer).
 *  De Python-runtime doet dezelfde decode, dus houd de twee gelijk. */
function unpackF16(a) {
  if (!a || !a.__f16) return a;
  const bin = atob(a.__f16);
  const buf = new ArrayBuffer(bin.length);
  const u8 = new Uint8Array(buf);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  const f16 = new Uint16Array(buf);
  const out = new Float64Array(f16.length);
  for (let i = 0; i < f16.length; i++) out[i] = f16ToF32(f16[i]);
  if (a.shape.length === 1) return Array.from(out);
  const [rows, cols] = a.shape;
  const m = [];
  for (let r = 0; r < rows; r++) m.push(Array.from(out.subarray(r * cols, (r + 1) * cols)));
  return m;
}

class Surrogate {
  constructor(w, meta) {
    this.W = w.layers.map((L) => unpackF16(L.W));
    this.b = w.layers.map((L) => unpackF16(L.b));
    this.xMu = unpackF16(w.x_mu);
    this.xSd = unpackF16(w.x_sd);
    this.yScale = unpackF16(w.y_scale);
    this.outCols = w.output_cols;
    this.inCols = w.input_cols;
    this.meta = meta;
    this.zones = meta.zones;
    // De elementvolgorde in de invoervector ligt vast in `input_cols` van de exporter en
    // moet exact matchen met vent_forecast.operable_elements — anders schuiven de kolommen.
    this.elemIds = w.input_cols.filter((c) => c.startsWith('open_')).map((c) => c.slice(5));
    this.elemById = new Map(meta.elements.map((e) => [e.id, e]));
    // Het surrogaat geeft gesorteerde paarnamen; door_mix-sleutels volgen de eigen
    // `between`-volgorde van elke deur.
    this.mixCols = [];
    w.output_cols.forEach((c, j) => {
      if (!c.startsWith('mix_')) return;
      const [a, b] = c.slice(4).split('__');
      const d = meta.doors.find((x) => (x.a === a && x.b === b) || (x.a === b && x.b === a));
      this.mixCols.push([j, d ? [d.a, d.b] : [a, b]]);
    });
  }

  /** De openingsfractie die build_openings voor dit element zou gebruiken. */
  fracOf(states, id) {
    const e = this.elemById.get(id);
    if (!e) return 0;
    const s = states[id];
    if (s === undefined || s === null || s === '') return e.default;
    return s in e.frac ? e.frac[s] : e.default;
  }

  predict(states, windSpeed, windDir, tOut, zoneTemps, cpShelter) {
    const n = this.elemIds.length;
    const x = new Float64Array(this.xMu.length);
    for (let i = 0; i < n; i++) x[i] = this.fracOf(states, this.elemIds[i]);
    const wd = (windDir * Math.PI) / 180;
    x[n] = windSpeed; x[n + 1] = Math.sin(wd); x[n + 2] = Math.cos(wd);
    x[n + 3] = tOut; x[n + 4] = cpShelter;
    for (let i = 0; i < this.zones.length; i++) {
      const z = this.zones[i];
      x[n + 5 + i] = zoneTemps[z] !== undefined ? zoneTemps[z] : tOut;
    }
    let h = Array.from(x, (v, i) => (v - this.xMu[i]) / this.xSd[i]);
    for (let l = 0; l < this.W.length; l++) {
      const W = this.W[l], bb = this.b[l];
      const out = new Array(W.length);
      for (let r = 0; r < W.length; r++) {
        const row = W[r];
        let acc = bb[r];
        for (let c = 0; c < row.length; c++) acc += row[c] * h[c];
        out[r] = acc;
      }
      // SiLU op alle lagen behalve de uitvoer.
      if (l < this.W.length - 1) for (let r = 0; r < out.length; r++) out[r] = out[r] / (1 + Math.exp(-out[r]));
      h = out;
    }
    const y = h.map((v, j) => Math.sinh(v) * this.yScale[j]);
    const fresh = {};
    this.zones.forEach((z, i) => { fresh[z] = Math.max(0, y[i]); });
    const mix = new Map();
    for (const [j, key] of this.mixCols) {
      const v = Math.max(0, y[j]);
      if (v > 0) { const k = pairKey(key[0], key[1]); mix.set(k, (mix.get(k) || 0) + v); }
    }
    return { fresh, mix };
  }
}

// ----------------------------------------------------------------- eenzijdige ventilatie

/**
 * de Gids & Phaff (1982), per kamer, in gesloten vorm.
 *
 * Bewust GEEN onderdeel van het surrogaat. `effective_fresh` is max(netto netwerkdebiet,
 * eenzijdig), en die max() is een discontinuïteit: een gladde MLP smeert 'm uit, waardoor het
 * surrogaat het in 18 % van de gevallen met de solver oneens was over de RICHTING van een
 * opening — precies de fout die de speeltuin onbruikbaar zou maken. Aan beide kanten exact
 * rekenen bracht dat van 82 % naar 95.7 % overeenstemming én verbeterde de end-to-end-fout
 * (0.035 → 0.021 °C). Er is geen reden om een gesloten formule te benaderen.
 */
function singleSidedFresh(meta, sur, states, windSpeed, zoneTemps, tOut) {
  const c = meta.consts, out = {};
  for (const w of meta.ss_windows || []) {
    const area = sur.fracOf(states, w.id) * w.max_open_area_m2;
    if (area <= 0) continue;
    const tIn = zoneTemps[w.room];
    const dt = tIn === undefined || tIn === null ? 0 : Math.abs(tIn - tOut);
    const v = Math.sqrt(c.SS_C1 * (windSpeed || 0) ** 2
                        + c.SS_C2 * Math.max(0, w.open_height_m) * dt + c.SS_C3);
    out[w.room] = (out[w.room] || 0) + 0.5 * area * v;
  }
  return out;
}

// ------------------------------------------------------------------ stratificatie

/** Kleinste-kwadraten-helling van temperatuur t.o.v. deurhoogte, geklemd op [0, max].
 *  Mirror van vp.stair_gradient — een inversie (kouder bovenin) wordt niet doorgegeven. */
function stairGradient(points, maxGrad) {
  const pts = points.filter((p) => p[1] !== null && p[1] !== undefined);
  if (new Set(pts.map((p) => p[0])).size < 2) return 0;
  const n = pts.length;
  const mz = pts.reduce((s, p) => s + p[0], 0) / n;
  const mt = pts.reduce((s, p) => s + p[1], 0) / n;
  const den = pts.reduce((s, p) => s + (p[0] - mz) ** 2, 0);
  if (den <= 0) return 0;
  const slope = pts.reduce((s, p) => s + (p[0] - mz) * (p[1] - mt), 0) / den;
  return Math.max(0, Math.min(maxGrad, slope));
}

/** Brown–Solvason tweerichtings-uitwisseling door een open binnendeur (één richting). */
function buoyantDoorExchange(area, tA, tB, C, G, heightM) {
  if (area <= 0 || tA == null || tB == null) return 0;
  const dt = Math.abs(tA - tB);
  if (dt <= 0) return 0;
  return C * area * Math.sqrt((G * heightM * dt) / (273.15 + 0.5 * (tA + tB)));
}

// ------------------------------------------------------------------------ de kern

class VentCore {
  constructor(meta, surrogate) {
    this.m = meta;
    this.sur = surrogate;
    this.zones = meta.zones;
    this.zi = new Map(this.zones.map((z, i) => [z, i]));
  }

  /** Deur-openingsoppervlakken per zonepaar, uit de huidige standen. */
  doorAreas(states) {
    const out = new Map();
    for (const d of this.m.doors) {
      const frac = this.sur.fracOf(states, d.id);
      const area = frac * d.area_m2;
      if (area > 0) {
        const k = pairKey(d.a, d.b);
        out.set(k, (out.get(k) || 0) + area);
      }
    }
    return out;
  }

  /**
   * Integreer de tijdlijn. `statesFor(step, i)` geeft de elementstanden op die stap — de
   * speeltuin overschrijft 'm om een scenario toe te passen. `airflowFor(i)` laat een
   * aanroeper bekende fresh/mix injecteren (de golden-test spuit die van de Python-solver
   * erin, om RC-portfouten van surrogaatfout te scheiden).
   * Geeft { series: {zone: [°C]}, sensor: {kamer: [°C]}, ach: {zone: [1/u]}, Ta, Tm }.
   */
  run(steps, seedTa, seedTm, statesFor, airflowFor, onAssemble) {
    const m = this.m, zones = this.zones, zi = this.zi, n = zones.length;
    const rhoCp = m.rho_cp, veff = m.vent_eff;
    const Ta = { ...seedTa }, Tm = { ...seedTm };
    const series = {}, achOut = {};
    for (const z of zones) { series[z] = []; achOut[z] = []; }

    for (let si = 0; si < steps.length; si++) {
      const step = steps[si];
      const states = statesFor ? statesFor(step, si) : step.states;
      const tOut = step.T_out;

      // Geïnjecteerde waarden zijn al effective_fresh, dus de single-sided max() geldt
      // alleen voor het ruwe netto-debiet van het surrogaat.
      let fresh, mix;
      if (airflowFor) {
        ({ fresh, mix } = airflowFor(si));
      } else {
        ({ fresh, mix } = this.sur.predict(
          states, step.wind_speed, step.wind_dir, tOut, Ta, m.cp_shelter));
        const ss = singleSidedFresh(m, this.sur, states, step.wind_speed, Ta, tOut);
        for (const z in ss) fresh[z] = Math.max(fresh[z] || 0, ss[z]);
      }

      // Advectieve geleidingen. De drijvende counterflow komt er ZONDER vent_eff bij —
      // het is een orifice-verschijnsel, geen netto advectie (zie de noot in vent_physics).
      const gdoor = new Map();
      for (const [k, q] of mix) gdoor.set(k, rhoCp * q * veff);

      const stratTerms = [];
      if (m.strat && Object.keys(m.strat).length) {
        const dArea = this.doorAreas(states);
        for (const [sid, info] of Object.entries(m.strat)) {
          const openOthers = Object.keys(info.doors).filter(
            (o) => dArea.has(pairKey(sid, o)) || dArea.has(pairKey(o, sid)));
          // In een voorspelling zijn er geen metingen, dus vp._gamma_temps valt terug op de
          // gesimuleerde luchtknopen — precies wat we hier hebben.
          const gamma = stairGradient(
            openOthers.filter((o) => Ta[o] !== undefined).map((o) => [info.doors[o], Ta[o]]),
            m.consts.STAIR_STRAT_MAX_GRAD);
          for (const other of openOthers) {
            const zh = info.doors[other];
            const area = (dArea.get(pairKey(sid, other)) || 0) + (dArea.get(pairKey(other, sid)) || 0);
            const qEx = buoyantDoorExchange(
              area, Ta[sid] + gamma * (zh - info.z_mean),
              Ta[other] !== undefined ? Ta[other] : tOut,
              m.consts.BUOY_EXCH_C, m.consts.G, m.consts.DOOR_HEIGHT_M);
            if (qEx > 0) {
              const k = gdoor.has(pairKey(sid, other)) ? pairKey(sid, other) : pairKey(other, sid);
              gdoor.set(k, (gdoor.get(k) || 0) + rhoCp * qEx);
            }
            const g = (gdoor.get(pairKey(sid, other)) || 0) + (gdoor.get(pairKey(other, sid)) || 0);
            if (g !== 0 && zi.has(other)) {
              stratTerms.push([zi.get(other), zi.get(sid), g * gamma * (zh - info.z_mean)]);
            }
          }
        }
      }

      const gvent = {};
      for (const z of zones) gvent[z] = rhoCp * (fresh[z] || 0) * veff;

      const nsub = Math.max(1, Math.ceil(step.dt / m.substep_s));
      const h = step.dt / nsub;
      for (let s = 0; s < nsub; s++) {
        const A = Array.from({ length: 2 * n }, () => new Array(2 * n).fill(0));
        const b = new Array(2 * n).fill(0);
        for (const z of zones) {
          const k = zi.get(z), ia = 2 * k, im = 2 * k + 1, pa = m.par[z];
          const qSolar = (step.irr[z] || 0) * pa.solar;
          const uaParty = pa.UA_party || 0;
          const qInt = (pa.Q_int_base || 0) * step.int_profile;
          A[ia][ia] += pa.C_a / h + gvent[z] + pa.UA_env + pa.H_am + uaParty;
          A[ia][im] += -pa.H_am;
          b[ia] += pa.C_a / h * Ta[z] + gvent[z] * tOut + pa.UA_env * tOut
                 + pa.f_air * qSolar + uaParty * step.nb_now + qInt;
          const uaRoof = pa.UA_roof || 0, uaGround = pa.UA_ground || 0;
          A[im][im] += pa.C_m / h + pa.H_am + pa.UA_mass + uaRoof + uaGround;
          A[im][ia] += -pa.H_am;
          b[im] += pa.C_m / h * Tm[z] + pa.UA_mass * tOut
                 + (1 - pa.f_air) * qSolar + uaRoof * step.t_solair[z] + uaGround * m.ground_temp;
        }
        const couple = [...gdoor.entries()].map(([k, g]) => [k.split('|'), g])
          .concat(m.ginter.map((e) => [[e.a, e.b], e.g]));
        for (const [[za, zb], g] of couple) {
          if (!zi.has(za) || !zi.has(zb)) continue;
          const ka = zi.get(za), kb = zi.get(zb);
          A[2 * ka][2 * ka] += g; A[2 * ka][2 * kb] += -g;
          A[2 * kb][2 * kb] += g; A[2 * kb][2 * ka] += -g;
        }
        for (const [ko, ks, val] of stratTerms) { b[2 * ko] += val; b[2 * ks] -= val; }
        if (onAssemble) onAssemble(si, s, A, b);
        const x = rcSolveLinear(A, b);
        if (x === null) break;      // bijna-singulier: bevries deze stap, zoals Python doet
        for (const z of zones) { const k = zi.get(z); Ta[z] = x[2 * k]; Tm[z] = x[2 * k + 1]; }
      }
      for (const z of zones) {
        series[z].push(Ta[z]);
        achOut[z].push(((fresh[z] || 0) * 3600) / (this.volOf(z) || 1));
      }
    }

    // Sensorruimte: wat de tado-voeler zou lezen (buitenmuur-bias), zodat de speeltuinlijn
    // op dezelfde schaal staat als de gemeten reeks ernaast.
    const sensor = {};
    for (const rid of m.sensor_rooms) {
      const frac = m.sensor_outdoor_frac[rid] || 0;
      sensor[rid] = series[rid].map((v, i) => (frac ? (1 - frac) * v + frac * steps[i].T_out : v));
    }
    return { series, sensor, ach: achOut, Ta, Tm };
  }

  volOf(z) {
    return (this.m.volumes && this.m.volumes[z]) || 1;
  }
}

// CommonJS-export voor tools/test_golden.js (node). In de browser is dit blok inert en
// blijven de bovenstaande declaraties gewoon globals.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { pairKey, rcSolveLinear, Surrogate, VentCore,
                     singleSidedFresh, stairGradient, buoyantDoorExchange };
}

/**
 * Golden-vector-test: de JS-kern moet vp.simulate reproduceren.
 *
 * Drie poorten. De eerste twee zijn hard en isoleren PORT-getrouwheid; de derde is
 * informatief. Dat onderscheid is niet academisch: door port- en surrogaatfout op één hoop
 * te gooien kon in de offline sessie een echte bug (een verdwaalde NUL-byte in een
 * template-literal die zone-paar-sleutels bouwde, 9.8e-2 °C) comfortabel onder de tolerantie
 * van het surrogaat blijven zitten, en werd later een pech-oorsprong voor een regressie
 * aangezien.
 *
 *   1. RC-port exact      Python's eigen per-stap fresh/mix geïnjecteerd, dus het ENIGE
 *                         wat getest wordt is de RC-assemblage, de stratificatie, de
 *                         drijvende counterflow en de sensorbias. Aan beide kanten dezelfde
 *                         rekensom ⇒ moet tot ~1e-9 kloppen. Harde poort.
 *
 *   2. Surrogaat-port     JS-surrogaat vs PYTHON-surrogaat (gedistilleerd nettodebiet +
 *                         exacte eenzijdige term + max). Óók dezelfde rekensom, dus óók
 *                         ~1e-9. Dít valideert de JS-surrogaat- en eenzijdige-implementatie.
 *                         Harde poort.
 *
 *   3. Surrogaat-fout     surrogaat vs de échte Newton-solver op déze ene oorsprong.
 *                         Gerapporteerd, ruim begrensd. De gezaghebbende meting is
 *                         tools/surrogate_backtest.py over tientallen oorsprongen; de staart
 *                         van één venster is geen regressiesignaal.
 *
 * Draaien:  python tools/export_driver_timeline.py   # eenmalig / na een fysica-wijziging
 *           node tools/test_golden.js
 */
const { readFileSync, existsSync } = require('fs');
const { join } = require('path');
const { VentCore, Surrogate, pairKey } = require('../docs/js/vent_core.js');

const here = __dirname;
const goldenDir = join(here, 'golden');
const tlPath = join(goldenDir, 'driver_timeline.json');
const gPath = join(goldenDir, 'golden.json');

if (!existsSync(tlPath) || !existsSync(gPath)) {
  console.log(`SKIP: geen golden-vector in ${goldenDir}.`);
  console.log('      Genereer hem met: python tools/export_driver_timeline.py');
  process.exit(0);
}

const meta = JSON.parse(readFileSync(tlPath, 'utf8'));
const golden = JSON.parse(readFileSync(gPath, 'utf8'));
const weights = JSON.parse(readFileSync(join(here, '../docs/js/surrogate.json'), 'utf8'));

const core = new VentCore(meta, new Surrogate(weights, meta));
const nExpect = meta.steps.length * meta.sensor_rooms.length;
let failed = false;

// ---- poort 1: RC-port exact ------------------------------------------------------
if (golden.solver_airflow && golden.solver_airflow.length === meta.steps.length) {
  const inject = (i) => {
    const r = golden.solver_airflow[i];
    const mix = new Map();
    for (const e of r.mix || []) mix.set(pairKey(e.a, e.b), e.q);
    return { fresh: r.fresh, mix };
  };
  const exact = core.run(meta.steps, golden.seed_Ta, golden.seed_Tm, null, inject);
  let worst = 0, room = '-', n = 0;
  for (const rid of meta.sensor_rooms) {
    const js = exact.sensor[rid], py = golden.expected_sensor[rid];
    for (let i = 0; i < py.length; i++) {
      const d = Math.abs(js[i] - py[i]);
      n++;
      if (d > worst) { worst = d; room = rid; }
    }
  }
  console.log(`\n[1] RC-port exact (Python-luchtstroom geïnjecteerd): ${worst.toExponential(2)} °C (${room}), ${n} punten`);
  // Een test die niets vergelijkt mag nooit slagen melden.
  if (n < nExpect) {
    console.log(`    FAIL: maar ${n} punten vergeleken, verwacht ${nExpect} — de golden-vector is incompleet.`);
    failed = true;
  } else if (worst > 1e-6) {
    console.log('    FAIL: de RC-port wijkt af van vent_physics.');
    failed = true;
  } else {
    console.log('    PASS: RC-assemblage, stratificatie en sensorbias zijn bit-getrouw.');
  }
} else {
  console.log('\n[1] OVERGESLAGEN: geen solver_airflow in golden.json — export_driver_timeline.py opnieuw draaien');
  failed = true;
}

// ---- poort 2: JS-surrogaat == Python-surrogaat -----------------------------------
if (golden.expected_sensor_surrogate) {
  const s = core.run(meta.steps, golden.seed_Ta, golden.seed_Tm, null);
  let worst = 0, room = '-', n = 0;
  for (const rid of meta.sensor_rooms) {
    const js = s.sensor[rid], py = golden.expected_sensor_surrogate[rid];
    for (let i = 0; i < py.length; i++) {
      const d = Math.abs(js[i] - py[i]);
      n++;
      if (d > worst) { worst = d; room = rid; }
    }
  }
  console.log(`\n[2] JS-surrogaat vs PYTHON-surrogaat: ${worst.toExponential(2)} °C (${room}), ${n} punten`);
  if (n < nExpect) {
    console.log(`    FAIL: maar ${n} punten vergeleken.`);
    failed = true;
  } else if (worst > 1e-6) {
    console.log('    FAIL: de JS-surrogaat-/eenzijdige-port wijkt af van Python.');
    failed = true;
  } else {
    console.log('    PASS: identiek aan de Python-surrogaatruntime.');
  }
} else {
  console.log('\n[2] OVERGESLAGEN: geen expected_sensor_surrogate — export_driver_timeline.py opnieuw draaien');
  failed = true;
}

// ---- poort 3: informatief — surrogaat vs de echte solver op deze ene oorsprong ----
const t0 = Date.now();
const out = core.run(meta.steps, golden.seed_Ta, golden.seed_Tm, null);
const ms = Date.now() - t0;

const LIM = { mean: 0.15, p95: 0.40, max: 0.60 };
console.log('\n[3] surrogaat vs de echte Newton-solver, DEZE oorsprong (informatief)');
console.log(`${'kamer'.padEnd(10)}${'gem'.padStart(9)}${'p95'.padStart(9)}${'max'.padStart(9)}`);
console.log('-'.repeat(37));
for (const rid of meta.sensor_rooms) {
  const js = out.sensor[rid], py = golden.expected_sensor[rid];
  const d = py.map((v, i) => Math.abs(js[i] - v)).sort((a, b) => a - b);
  const mean = d.reduce((s, v) => s + v, 0) / d.length;
  const p95 = d[Math.floor(0.95 * (d.length - 1))];
  const max = d[d.length - 1];
  const bad = mean > LIM.mean || p95 > LIM.p95 || max > LIM.max;
  if (bad) failed = true;
  console.log(`${rid.padEnd(10)}${mean.toFixed(4).padStart(9)}${p95.toFixed(4).padStart(9)}` +
              `${max.toFixed(4).padStart(9)}${bad ? '  <-- boven de limiet' : ''}`);
}
console.log('-'.repeat(37));
console.log(`${'limiet'.padEnd(10)}${LIM.mean.toFixed(4).padStart(9)}` +
            `${LIM.p95.toFixed(4).padStart(9)}${LIM.max.toFixed(4).padStart(9)}`);
console.log(`\n${meta.steps.length} stappen in ${ms} ms (${(ms / meta.steps.length).toFixed(2)} ms/stap)`);

console.log(failed ? '\nFAILED' : '\nPASS');
process.exit(failed ? 1 : 0);

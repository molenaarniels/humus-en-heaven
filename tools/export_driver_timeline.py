#!/usr/bin/env python3
"""Exporteer een golden-vector voor de browserkern — het contract tussen Python en JS.

De productie-payload (`docs/vent_forecast.json`) schrijft `vent_twin.py` elke kwartierrun
zelf, via `vent_forecast.driver_export`. Dít gereedschap doet twee dingen die daar niet
horen omdat ze alleen bij het ontwikkelen nodig zijn:

  1. het bouwt dezelfde payload op een HISTORISCHE oorsprong uit de maand-shards, zodat de
     test niet van live weer afhangt en morgen hetzelfde antwoord geeft;
  2. het legt de exacte Ta/Tm-baan vast die `vp.simulate` voor die tijdlijn + dat zaad
     produceert — plus de per-stap `fresh`/`mix` van de solver, én de baan van de
     PYTHON-surrogaatroute.

Die drie referenties maken drie verschillende tests mogelijk, en dat onderscheid is het punt
(zie tools/test_golden.js): met de solver-luchtstroom geïnjecteerd test je uitsluitend de
RC-port, met de Python-surrogaatbaan uitsluitend de JS-surrogaatport, en pas daarna is het
verschil met de échte solver de fout van het surrogaat zelf. Ze op één hoop gooien liet in
de offline sessie een echte portfout (9.8e-2 °C) ongemerkt binnen het foutbudget van het
surrogaat zitten.

Vereist numpy voor de Python-surrogaatroute (zie requirements-tools.txt).

Gebruik:
    python tools/export_driver_timeline.py --hours 12 --out tools/golden
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vent_forecast as vfc    # noqa: E402
import vent_io as vio          # noqa: E402
import vent_physics as vp      # noqa: E402
from horizon_backtest import Measured   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=vfc.FORECAST_H)
    ap.add_argument("--origin", default=None,
                    help="ISO-datetime; default = een dicht bemeten punt midden in het record")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "golden"))
    args = ap.parse_args()

    house = vio.load_house()
    ds = vio.load_dataset(house)
    params = vio.merged_params(house, dict(vio.load_learned(), physics_rev=vio.PHYSICS_REV))
    meas = Measured(ds, 10.0)
    rooms = [r for r, c in house.get("rooms", {}).items() if c.get("from_window_data")]

    ts = sorted(t for s in meas.raw.values() for t, _ in s)
    t0 = (datetime.fromisoformat(args.origin) if args.origin
          else (ts[0] + timedelta(days=30)).replace(minute=0, second=0, microsecond=0))

    rows = ds["weather_rows"]
    ctx = vio.make_context(house, {"hourly": rows}, t0)
    tl = vio.build_timeline(house, {"hourly": rows, "current": {}}, ds["log"],
                            t0, vio.WARMUP_H, ctx, beam_iam=True, end_h=args.hours)
    warm = [s for s in tl if s["t"] <= t0]
    fore = [s for s in tl if s["t"] >= t0]
    if len(warm) < 4 or len(fore) < 2:
        raise SystemExit(f"[golden] te weinig tijdlijn rond {t0} — kies een andere --origin")

    # Zaai de aanloop exact zoals de backtest, zodat de golden-vector het echte pad aflegt.
    seed0 = {}
    for rid in rooms:
        v = meas.at(rid, warm[0]["t"])
        if v is not None:
            seed0[rid] = vp.air_from_sensor(house, rid, v, warm[0]["T_out"])
    for z in vfc.zone_list(house):
        seed0.setdefault(z, warm[0]["T_out"])
    ws = vp.simulate(house, params, warm, seed0, ctx, snapshot_t=t0)

    measured_now = {rid: meas.at(rid, t0) for rid in rooms}
    measured_now = {k: v for k, v in measured_now.items() if v is not None}
    seed, tm, anchored = vfc.anchor_seed(house, ws, measured_now, fore[0]["T_out"])
    if not anchored:
        raise SystemExit(f"[golden] geen meting op {t0} om op te ankeren")

    meta = vfc.driver_export(house, params, ctx, fore, seed, tm, now=t0)

    # Golden: de exacte baan die vp.simulate voor deze tijdlijn + zaad produceert, plús de
    # per-stap fresh/mix van de solver. Die injecteren in de JS-kern scheidt de twee
    # foutbronnen; zonder dat kan een portfout zich in het surrogaat-budget verstoppen.
    rec = []
    _ef, _dm = vp.effective_fresh, vp.door_mix

    def rec_ef(fresh, house_, states, weather, zone_temps, outside_temp):
        f = _ef(fresh, house_, states, weather, zone_temps, outside_temp)
        rec.append({"fresh": dict(f)})
        return f

    def rec_dm(house_, flows, openings):
        m_ = _dm(house_, flows, openings)
        if rec:
            rec[-1]["mix"] = [{"a": a, "b": b, "q": q} for (a, b), q in m_.items()]
        return m_

    vp.effective_fresh, vp.door_mix = rec_ef, rec_dm
    try:
        sim = vp.simulate(house, params, fore, seed, ctx, tm_seed=tm)
    finally:
        vp.effective_fresh, vp.door_mix = _ef, _dm
    golden = {
        "origin": t0.isoformat(),
        "seed_Ta": seed, "seed_Tm": tm,
        "expected_sensor": {rid: [v for _, v in vp._to_sensor_series(
            house, fore, rid, sim["series"].get(rid, []))] for rid in rooms},
        "final_Ta": sim["Ta"], "final_Tm": sim["Tm"],
        "n_steps": len(fore),
        "solver_airflow": rec,
    }

    # En de PYTHON-SURROGAATroute op dezelfde oorsprong. De golden-test moet bewijzen dat de
    # JS-port getrouw is, niet de nauwkeurigheid van het surrogaat hermeten (dat doet
    # tools/surrogate_backtest.py over tientallen oorsprongen). JS-surrogaat tegen
    # PYTHON-surrogaat isoleert portfouten exact; tegen de echte solver zou het alleen de
    # surrogaatfout van díé ene oorsprong opnieuw rapporteren en pech-oorsprongen als
    # regressie markeren.
    import surrogate_runtime
    restore = surrogate_runtime.install(house, params.get("cp_shelter", 0.2))
    try:
        sim_sur = vp.simulate(house, params, fore, seed, ctx, tm_seed=tm)
    finally:
        restore()
    golden["expected_sensor_surrogate"] = {
        rid: [v for _, v in vp._to_sensor_series(house, fore, rid,
                                                 sim_sur["series"].get(rid, []))]
        for rid in rooms}

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "driver_timeline.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    with open(os.path.join(args.out, "golden.json"), "w", encoding="utf-8") as f:
        json.dump(golden, f, indent=1)
    size = os.path.getsize(os.path.join(args.out, "driver_timeline.json")) / 1e3
    print(f"oorsprong {t0}   stappen {len(fore)}   zones {len(meta['zones'])}")
    print(f"geschreven: {args.out}/driver_timeline.json ({size:.0f} kB) + golden.json")
    for rid in rooms:
        e = golden["expected_sensor"][rid]
        print(f"  {rid:9} {e[0]:6.2f} -> {e[-1]:6.2f} °C")


if __name__ == "__main__":
    main()

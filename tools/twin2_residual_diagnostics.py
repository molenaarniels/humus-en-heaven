#!/usr/bin/env python3
"""Residu-diagnose voor Ventilatie 2 (Project 12) — waar zit de fout, en waardoor?

Twee onafhankelijke doorsnijdingen die samen de eenzijdige-ventilatie-diagnose van
augustus 2026 dragen (zie AIRFLOW2_ASSESSMENT.md):

1. `--residuals` — profileert de held-out-residuen van een gefit parameterset over kamer,
   uur, instraling, binnen-buiten-verschil én **de gemelde raamstand**. De sleutelvondst:
   de fout zit vrijwel volledig in de raam-open-toestand en heeft daar een wind-helling
   (te warm bij weinig wind, te koud bij veel wind), terwijl de raam-dicht-fout vlak is
   over álle windsnelheden.

2. `--ventilation` — zet het netwerk-verseluchtdebiet van één kamer naast de empirische
   de Gids & Phaff-correlatie voor eenzijdige ventilatie. Het drukwerk-netwerk rekent
   alleen het *netto* debiet; de pulserende/buoyante uitwisseling dóór één opening bestaat
   er niet in. Zelfde blinde vlek die `am.buoyant_door_exchange` (Brown–Solvason) voor
   bínnendeuren repareert — voor buitenramen ontbreekt het equivalent.

Gebruik:
    python tools/twin2_residual_diagnostics.py --residuals --params <campagne-arm.json>
    python tools/twin2_residual_diagnostics.py --ventilation [--room hotties]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import airflow_model as am        # noqa: E402
import airflow2_model as a2       # noqa: E402

# Kamer → (buitenraam, deur naar de koker). Voedt de raamstand-doorsnijding.
ROOM_WINDOW = {"hotties": ("hotties_window", "hotties_stair"),
               "office": ("office_window", "office_stair"),
               "ted": ("ted_small_window", "ted_stair"),
               "living": ("living_french", "living_hall")}
# de Gids & Phaff (1982), eenzijdige ventilatie: Q = (A/2)·√(C1·U² + C2·H·ΔT + C3).
DG_C1, DG_C2, DG_C3 = 0.001, 0.0035, 0.01


def _house():
    house = am.load_house()
    loc = house.get("location", {})
    am._LAT = loc.get("lat", am._LAT)
    am._LON = loc.get("lon", am._LON)
    return house


def _stat(rows: list[dict]) -> str:
    r = [x["res"] for x in rows]
    if not r:
        return "n=0"
    return (f"n={len(r):5} bias={st.mean(r):+6.3f} "
            f"rmse={math.sqrt(sum(v * v for v in r) / len(r)):5.3f}")


def residuals(params_path: str, offset: int) -> None:
    house = _house()
    params = json.load(open(params_path, encoding="utf-8"))["params"]
    wins = a2.prepare_windows(house, a2.load_dataset(house), window_d=5.0, stride_d=5.0)
    held = [w for i, w in enumerate(wins) if i % 3 == offset]
    print(f"held-out: {[w['end'].date().isoformat() for w in held]}")
    recs = []
    for w in held:
        with a2.window_anchors(w):
            sim = a2.simulate2(house, params, w["timeline"], w["seed"],
                               calib_only_rooms=set(w["actual"]) | set(w["rh"]),
                               seed_w=w["seed_w"], tm_seed=w.get("tm_seed"))
            states = [(s["t"], s["states"]) for s in w["timeline"]]
            wind = [(s["t"], s["weather"].get("wind_speed") or 0.0) for s in w["timeline"]]
            solar = [(s["t"], (s["weather"].get("direct") or 0.0)
                      + (s["weather"].get("diffuse") or 0.0)) for s in w["timeline"]]
            tout = [(s["t"], s["T_out"]) for s in w["timeline"]]
            for rid, samples in w["actual"].items():
                pred = sim["series"].get(rid) or []
                if not pred:
                    continue
                pred = am._to_sensor_series(house, w["timeline"], rid, pred)
                wid = (ROOM_WINDOW.get(rid) or (None, None))[0]
                for ts, val in samples:
                    stt = min(states, key=lambda kv: abs((kv[0] - ts).total_seconds()))[1]
                    recs.append({
                        "room": rid, "t": ts, "res": am._interp(pred, ts) - val,
                        "open": None if wid is None else (stt.get(wid) not in ("dicht", None)),
                        "wind": am._interp(wind, ts), "sol": am._interp(solar, ts),
                        "dt": val - am._interp(tout, ts)})
    print(f"\n{len(recs)} residuen\n")
    print("=== per kamer ===")
    for rid in sorted({x["room"] for x in recs}):
        print(f"  {rid:9} {_stat([x for x in recs if x['room'] == rid])}")
    print("\n=== per binnen-buiten-verschil (gemeten binnen − buiten) ===")
    for lo, hi in ((-99, -3), (-3, -1), (-1, 1), (1, 3), (3, 99)):
        print(f"  {lo:+3} .. {hi:+3} K  {_stat([x for x in recs if lo <= x['dt'] < hi])}")
    print("\n=== per instraling ===")
    for lo, hi in ((0, 1), (1, 100), (100, 300), (300, 600), (600, 2000)):
        print(f"  {lo:4}-{hi:<5} W/m²  {_stat([x for x in recs if lo <= x['sol'] < hi])}")
    print("\n=== raamstand × wind (dé doorsnijding) ===")
    for lab, val in (("OPEN ", True), ("DICHT", False)):
        for lo, hi in ((0, 1.5), (1.5, 3), (3, 5), (5, 20)):
            rows = [x for x in recs if x["open"] is val and lo <= x["wind"] < hi]
            if rows:
                print(f"  raam {lab} {lo:>4}-{hi:<4} m/s  {_stat(rows)}")
    print("\n=== per kamer × raamstand ===")
    for rid in sorted({x["room"] for x in recs}):
        for lab, val in (("OPEN ", True), ("DICHT", False)):
            rows = [x for x in recs if x["room"] == rid and x["open"] is val]
            if rows:
                print(f"  {rid:9} raam {lab} {_stat(rows)}")


def ventilation(room: str) -> None:
    house = _house()
    params = am.merged_params(house, json.load(open("docs/airflow_learned.json",
                                                    encoding="utf-8")))
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    wid = ROOM_WINDOW[room][0]
    w = house["windows"][wid]
    area = w["max_open_area_m2"]
    height = math.sqrt(max(w.get("area_m2", 1.0), 1e-6))   # ruwe openings-hoogte
    vol = house["rooms"][room]["volume_m3"]

    def net(wind, t_out, t_in):
        states = {k: "dicht" for k in list(house["windows"]) + list(house["doors"])}
        for v in house["vents"]:
            states[v] = "open"
        states[wid] = "open"
        temps = {z: t_in for z in zones}
        ops = am.build_openings(house, states,
                                {"wind_speed": wind, "wind_dir": 309.0, "T_out": t_out},
                                params, temps, t_out)
        q = am.solve_network(zones, ops, temps, t_out)["fresh"].get(room, 0.0)
        return q, q * 3600 / vol

    def dg(wind, dt):
        q = 0.5 * area * math.sqrt(DG_C1 * wind ** 2 + DG_C2 * height * abs(dt) + DG_C3)
        return q, q * 3600 / vol

    print(f"{room}: {vol} m³, openend raamvlak {area} m², opening ~{height:.1f} m hoog\n")
    print(f"{'wind':>5} {'ΔT':>5} | {'netwerk m³/s':>13} {'ACH':>7} | "
          f"{'deGids m³/s':>12} {'ACH':>7} | {'onderschatting':>14}")
    for wind in (0.5, 1.0, 2.0, 3.0, 4.0, 6.0):
        qm, am_ = net(wind, 20.0, 23.0)
        qd, ad = dg(wind, 3.0)
        print(f"{wind:5.1f} {3.0:5.1f} | {qm:13.4f} {am_:7.2f} | {qd:12.4f} {ad:7.2f} | "
              f"{ad / max(am_, 1e-9):13.1f}×")
    lo, hi = net(0.5, 20.0, 23.0)[1], net(6.0, 20.0, 23.0)[1]
    dlo, dhi = dg(0.5, 3.0)[1], dg(6.0, 3.0)[1]
    print("\nwind-helling 0.5 → 6 m/s:")
    print(f"   netwerk  {lo:7.2f} → {hi:7.2f} ACH   ({hi / max(lo, 1e-9):.0f}×)")
    print(f"   deGids   {dlo:7.2f} → {dhi:7.2f} ACH   ({dhi / dlo:.1f}× — vrijwel vlak)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Tweeling 2 — residu- en ventilatiediagnose")
    ap.add_argument("--residuals", action="store_true")
    ap.add_argument("--ventilation", action="store_true")
    ap.add_argument("--params", help="campagne-resultaat-JSON met een `params`-blok")
    ap.add_argument("--holdout-offset", type=int, default=2)
    ap.add_argument("--room", default="hotties")
    args = ap.parse_args()
    if args.residuals:
        if not args.params:
            ap.error("--residuals vereist --params")
        residuals(args.params, args.holdout_offset)
    elif args.ventilation:
        ventilation(args.room)
    else:
        ap.error("kies --residuals of --ventilation")
    return 0


if __name__ == "__main__":
    sys.exit(main())

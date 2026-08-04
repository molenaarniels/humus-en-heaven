#!/usr/bin/env python3
"""End-to-end acceptance test for the airflow surrogate.

Held-out accuracy against solver samples is only a proxy. What matters is whether
the surrogate, running *inside* the thermal simulation, reproduces the forecasts
the real solver produces. Errors there can cancel (the RC time constants damp a
lot of airflow noise) or compound (a biased ACH shifts every step in the same
direction), and only this test tells you which.

Runs the identical rolling-origin backtest twice on the same origins — once with
`vp.solve_network`, once with the surrogate patched in — and compares per room.

Acceptance: the surrogate-vs-solver *disagreement* should be small next to the
model's own error against reality (~0.6-0.9 °C). If swapping the solver moves the
forecast by a comparable amount, the surrogate is not faithful enough to carry
the playground.

Usage:
    python tools/surrogate_backtest.py
    python tools/surrogate_backtest.py --stride-h 6
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from collections import defaultdict
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import vent_io as vio                      # noqa: E402
from horizon_backtest import Measured, run_origin   # noqa: E402
import surrogate_runtime                   # noqa: E402

_G: dict = {}


def _init(house, ds, params, meas, rooms, horizon_h, weights):
    _G.update(house=house, ds=ds, params=params, meas=meas, rooms=rooms,
              horizon_h=horizon_h)
    if weights:
        surrogate_runtime.install(house, params.get("cp_shelter", 0.2), weights)


def _work(t0):
    try:
        return t0, run_origin(_G["house"], _G["ds"], _G["params"], _G["meas"],
                              t0, _G["horizon_h"], _G["rooms"], "shift")
    except Exception:
        return t0, None


def run(house, ds, params, meas, rooms, origins, horizon_h, jobs, weights):
    import multiprocessing as mp
    res = {}
    t0 = time.time()
    if jobs > 1:
        with mp.Pool(jobs, initializer=_init,
                     initargs=(house, ds, params, meas, rooms, horizon_h, weights)) as p:
            for k, r in p.imap_unordered(_work, origins, chunksize=4):
                if r:
                    res[k] = r
    else:
        _init(house, ds, params, meas, rooms, horizon_h, weights)
        for o in origins:
            k, r = _work(o)
            if r:
                res[k] = r
    return res, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", default=surrogate_runtime.DEFAULT_WEIGHTS)
    ap.add_argument("--horizon-h", type=float, default=12.0)
    ap.add_argument("--stride-h", type=float, default=9.0)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    args = ap.parse_args()

    house = vio.load_house()
    ds = vio.load_dataset(house)
    # Stempel de huidige revisie op de geleerde staat: anders reset merged_params bij een
    # PHYSICS_REV-bump naar de priors en meet je het surrogaat op parameters die de
    # productie nooit draait.
    params = vio.merged_params(house, dict(vio.load_learned(), physics_rev=vio.PHYSICS_REV))
    meas = Measured(ds, 10.0)
    rooms = [r for r, c in house.get("rooms", {}).items() if c.get("from_window_data")]

    ts = sorted(t for s in meas.raw.values() for t, _ in s)
    origins, t = [], ts[0] + timedelta(hours=vio.WARMUP_H)
    last = ts[-1] - timedelta(hours=args.horizon_h)
    while t <= last:
        origins.append(t)
        t += timedelta(hours=args.stride_h)
    print(f"origins {len(origins)}  horizon {args.horizon_h}h  jobs {args.jobs}\n")

    solver, t_solver = run(house, ds, params, meas, rooms, origins,
                           args.horizon_h, args.jobs, None)
    print(f"solver    : {len(solver)} origins in {t_solver:.1f}s")
    surro, t_sur = run(house, ds, params, meas, rooms, origins,
                       args.horizon_h, args.jobs, args.weights)
    print(f"surrogate : {len(surro)} origins in {t_sur:.1f}s  "
          f"({t_solver / max(t_sur, 1e-9):.1f}x speedup)\n")

    # vs reality, and vs each other
    err_s = defaultdict(list)
    err_u = defaultdict(list)
    diff = defaultdict(list)
    for k in set(solver) & set(surro):
        for rid in rooms:
            a = solver[k].get(rid, {})
            b = surro[k].get(rid, {})
            for hm in set(a) & set(b):
                ps, _, act = a[hm]
                pu, _, _ = b[hm]
                err_s[rid].append(ps - act)
                err_u[rid].append(pu - act)
                diff[rid].append(pu - ps)

    def rmse(v):
        return math.sqrt(sum(x * x for x in v) / len(v)) if v else float("nan")

    print("=" * 78)
    print(f"{'room':10}{'solver RMSE':>13}{'surrogate':>12}{'delta':>9}"
          f"{'|surro-solver|':>16}{'p95':>8}")
    print("-" * 78)
    for rid in rooms:
        d = [abs(x) for x in diff[rid]]
        d.sort()
        print(f"{rid:10}{rmse(err_s[rid]):13.3f}{rmse(err_u[rid]):12.3f}"
              f"{rmse(err_u[rid]) - rmse(err_s[rid]):+9.3f}"
              f"{sum(d) / len(d):16.3f}{d[int(.95 * len(d))]:8.3f}")
    alls = [x for v in err_s.values() for x in v]
    allu = [x for v in err_u.values() for x in v]
    alld = sorted(abs(x) for v in diff.values() for x in v)
    print("-" * 78)
    print(f"{'POOLED':10}{rmse(alls):13.3f}{rmse(allu):12.3f}"
          f"{rmse(allu) - rmse(alls):+9.3f}{sum(alld) / len(alld):16.3f}"
          f"{alld[int(.95 * len(alld))]:8.3f}")

    md = sum(alld) / len(alld)
    print(f"\nmean |surrogate - solver| = {md:.3f} °C against a model error of "
          f"{rmse(alls):.3f} °C  ({100 * md / rmse(alls):.0f}%)")
    if md > 0.15 * rmse(alls):
        print("  !! NOT accepted — swapping the solver moves the forecast too much.")
    else:
        print("  accepted: the surrogate is faithful inside the thermal loop.")


if __name__ == "__main__":
    main()

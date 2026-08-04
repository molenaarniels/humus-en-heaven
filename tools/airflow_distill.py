#!/usr/bin/env python3
"""Distil the multi-zone airflow solver into a dataset a small NN can learn.

Why distil rather than learn from history (PLAN.md §2.2): the observational
record has ~90 on/off events per element, confounded in both directions (french
doors open when it's warm, kitchen tilt shut when it's hot). No model trained on
it can answer "what if I opened this window?" correctly. But `solve_network` is a
deterministic function — we can evaluate it on **uniformly sampled, independent**
window states, the exact experiment the real house can never run. The surrogate
then inherits the solver's causal structure instead of the log's correlations.

The distilled mapping is ONLY the expensive, iterative part of the airflow chain:

    (open fractions, wind speed/dir, T_out, zone temps, cp_shelter)
        → build_openings → solve_network → net fresh per zone + door_mix

The single-sided term (`vp.single_sided_fresh`) is deliberately NOT distilled: it is
a closed-form correlation, and `effective_fresh`'s `max(net, single_sided)` is a
discontinuity that a smooth network cannot represent (see the comment in
`one_sample`). The consumer computes it exactly and takes the max itself.

Sampling deliberately mixes three regimes so the surrogate is accurate both where
the house actually operates and across the whole space the playground can reach:

  uniform  every operable element independently open/closed/ajar  — unconfounded
  sparse   only 1-3 elements open                                  — the common real case
  replay   states drawn from the actual openings log               — the operating point

Outputs a .npz of inputs/targets plus a JSON spec pinning column order, so the
trainer and the JS runtime agree on the layout.

Usage:
    python tools/airflow_distill.py --n 400000 --out data/surrogate/airflow.npz
    python tools/airflow_distill.py --n 2000 --jobs 1 --out /tmp/smoke.npz
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import vent_io as vio          # noqa: E402
import vent_physics as vp      # noqa: E402
# Eén bron voor de kolomvolgorde: dezelfde helpers die vent_forecast.driver_export gebruikt
# om de browser-payload te bouwen, en die surrogate_runtime tegen `input_cols` verifieert.
# Zouden deze drie ooit uiteenlopen, dan schuiven de surrogaat-kolommen stilzwijgend op.
from vent_forecast import door_pairs, operable_elements, zone_list   # noqa: E402
from surrogate_runtime import _frac_of                              # noqa: E402

# Ranges deliberately wider than the observed record (t_out 9.2-36.3 °C, wind
# 0.15-6.98 m/s) so the playground can explore beyond this summer without the
# surrogate extrapolating blindly.
T_OUT_RANGE = (-5.0, 40.0)
T_ZONE_RANGE = (12.0, 34.0)
WIND_RANGE = (0.0, 14.0)
CP_SHELTER_RANGE = (0.10, 0.60)   # learned value is 0.214




def sample_states(elems, rng, mode, replay_pool):
    """Return {element_id: 'open'|'dicht'|'half'} — the string form vent_io uses."""
    if mode == "replay" and replay_pool:
        base = dict(rng.choice(replay_pool))
        # jitter a couple of elements so replay still covers nearby counterfactuals
        for eid, _ in rng.sample(elems, k=min(3, len(elems))):
            base[eid] = rng.choice(["open", "dicht"])
        return base
    if mode == "sparse":
        st = {eid: "dicht" for eid, _ in elems}
        for eid, _ in rng.sample(elems, k=rng.randint(1, 3)):
            st[eid] = rng.choice(["open", "open", "half"])
        return st
    return {eid: rng.choice(["open", "dicht", "half"]) for eid, _ in elems}


def one_sample(house, elems, zones, dpairs, rng, replay_pool):
    mode = rng.choices(["uniform", "sparse", "replay"], weights=[0.5, 0.3, 0.2])[0]
    states = sample_states(elems, rng, mode, replay_pool)

    t_out = rng.uniform(*T_OUT_RANGE)
    wind_s = rng.uniform(*WIND_RANGE)
    wind_d = rng.uniform(0.0, 360.0)
    cp = rng.uniform(*CP_SHELTER_RANGE)
    # Zone temps: a common house level plus per-zone spread, so stack effects are
    # exercised (a uniform house has no buoyancy signal at all).
    base_t = rng.uniform(*T_ZONE_RANGE)
    ztemps = {z: min(T_ZONE_RANGE[1], max(T_ZONE_RANGE[0], base_t + rng.gauss(0, 1.8)))
              for z in zones}

    weather = {"wind_speed": wind_s, "wind_dir": wind_d}
    params = {"cp_shelter": cp}
    ops = vp.build_openings(house, states, weather, params, ztemps, t_out)
    net = vp.solve_network(zones, ops, ztemps, t_out)
    # Target the RAW network net flow, NOT effective_fresh.
    #
    # effective_fresh is max(net, single_sided) — a discontinuity. A smooth MLP smears
    # it, and `tools/test_invariants.py` measured the cost: the single-sided branch wins
    # in 21.5% of zone-samples, and the surrogate then disagreed with the solver about
    # the DIRECTION of an opening's effect in 18% of cases (unchanged from a 0.11 MB to
    # a 6.9 MB model, so structural rather than capacity).
    #
    # single_sided_fresh is closed-form (de Gids & Phaff: area, height, wind, dT) and
    # needs no solver, so there is no reason to approximate it. Only the Newton solve is
    # distilled; the consumer computes the single-sided term exactly and applies the
    # max() analytically.
    fresh = net["fresh"]
    mix = vp.door_mix(house, net["flows"], ops)

    # Mirror build_openings exactly: an element absent from `states` falls back to its
    # own default fraction. Replay snapshots don't carry every element, so reading
    # states[eid] directly both crashes and — worse — would record a different opening
    # fraction than the solver actually used.
    x = [_frac_of(house, states, eid, kind) for eid, kind in elems]
    x += [wind_s, np.sin(np.deg2rad(wind_d)), np.cos(np.deg2rad(wind_d)), t_out, cp]
    x += [ztemps[z] for z in zones]
    y = [max(0.0, fresh.get(z, 0.0)) for z in zones]   # raw network net flow
    y += [mix.get(p, mix.get((p[1], p[0]), 0.0)) for p in dpairs]
    return x, y




_G: dict = {}


def _init(seed_base):
    house = vio.load_house()
    _G.update(house=house, elems=operable_elements(house), zones=zone_list(house),
              dpairs=door_pairs(house), seed_base=seed_base)
    ds = vio.load_dataset(house)
    pool = []
    for snap in ds["log"]:
        st = snap.get("states") or {}
        if st:
            pool.append({k: v for k, v in st.items() if isinstance(v, str) and v})
    _G["replay"] = pool


def _chunk(job):
    idx, n = job
    rng = random.Random(_G["seed_base"] + idx)
    xs, ys, fails = [], [], []
    for _ in range(n):
        try:
            x, y = one_sample(_G["house"], _G["elems"], _G["zones"], _G["dpairs"],
                              rng, _G["replay"])
        except Exception as exc:                      # never silently thin the dataset:
            fails.append(f"{type(exc).__name__}: {exc}")   # dropped samples would bias it
            continue
        xs.append(x)
        ys.append(y)
    return (np.asarray(xs, dtype=np.float32), np.asarray(ys, dtype=np.float32), fails)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=400_000)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--seed", type=int, default=20260804)
    ap.add_argument("--out", default="data/surrogate/airflow.npz")
    args = ap.parse_args()

    house = vio.load_house()
    elems, zones, dpairs = operable_elements(house), zone_list(house), door_pairs(house)
    print(f"operable elements : {len(elems)}")
    print(f"zones             : {zones}")
    print(f"door pairs        : {dpairs}")
    print(f"input dim         : {len(elems) + 5 + len(zones)}")
    print(f"output dim        : {len(zones) + len(dpairs)}")
    print(f"samples           : {args.n}  jobs {args.jobs}\n")

    per = 2000
    jobs = [(i, min(per, args.n - i * per)) for i in range((args.n + per - 1) // per)]
    xs, ys, failures = [], [], []
    if args.jobs > 1:
        import multiprocessing as mp
        with mp.Pool(args.jobs, initializer=_init, initargs=(args.seed,)) as pool:
            for i, (x, y, f) in enumerate(pool.imap_unordered(_chunk, jobs)):
                if len(x):
                    xs.append(x)
                    ys.append(y)
                failures.extend(f)
                if (i + 1) % 20 == 0:
                    print(f"  {sum(len(a) for a in xs)} samples", flush=True)
    else:
        _init(args.seed)
        for j in jobs:
            x, y, f = _chunk(j)
            if len(x):
                xs.append(x)
                ys.append(y)
            failures.extend(f)

    X, Y = np.concatenate(xs), np.concatenate(ys)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    np.savez_compressed(args.out, X=X, Y=Y)
    spec = {
        "input_cols": [f"open_{e}" for e, _ in elems]
                      + ["wind_speed", "wind_dir_sin", "wind_dir_cos", "t_out", "cp_shelter"]
                      + [f"tz_{z}" for z in zones],
        "output_cols": [f"fresh_{z}" for z in zones]
                       + [f"mix_{a}__{b}" for a, b in dpairs],
        "n": int(len(X)),
        "ranges": {"t_out": T_OUT_RANGE, "t_zone": T_ZONE_RANGE,
                   "wind": WIND_RANGE, "cp_shelter": CP_SHELTER_RANGE},
    }
    with open(os.path.splitext(args.out)[0] + "_spec.json", "w") as f:
        json.dump(spec, f, indent=2)

    if failures:
        from collections import Counter
        c = Counter(failures)
        print(f"\n!! {len(failures)} samples failed ({len(failures) / args.n:.1%}) —"
              f" dropped samples bias the surrogate, so this must be ~0:")
        for msg, k in c.most_common(5):
            print(f"   {k:6}  {msg}")
    else:
        print("\nno failed samples")

    print(f"\nwrote {args.out}  X{X.shape} Y{Y.shape}")
    print(f"\n{'output':28}{'mean':>10}{'p50':>10}{'p95':>10}{'max':>10}{'frac>0':>9}")
    for i, c in enumerate(spec["output_cols"]):
        col = Y[:, i]
        print(f"{c:28}{col.mean():10.4f}{np.median(col):10.4f}"
              f"{np.percentile(col, 95):10.4f}{col.max():10.4f}{(col > 1e-6).mean():9.3f}")


if __name__ == "__main__":
    main()

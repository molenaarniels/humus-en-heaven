#!/usr/bin/env python3
"""Physical invariants the playground must never violate.

Accuracy is not trustworthiness. The twin scores 0.72 °C at 12h and the JS core is
bit-faithful, but nothing yet stops it giving a physically absurd answer to a
*counterfactual* — and counterfactuals are the whole point of the playground.

The invariants below are deliberately only the ones that are actually true. Writing
this file found that two of my first four expectations were wrong, and the wrongness
is itself worth recording:

  A  sealed house      everything shut -> modelled fresh air is EXACTLY zero. Each
                       zone has a single leak opening to outside, so mass balance
                       forces net flow to nil; those leaks exist to keep the pressure
                       solve non-singular, not to ventilate.
                       CONSEQUENCE: the model has no background-infiltration term at
                       all. A shut house exchanges no air in this model, and real
                       infiltration is absorbed into the learned `ua_env`. That is a
                       modelling choice worth knowing when reading playground output,
                       not a bug.
                       (First version asserted "small and strictly positive". Wrong.)

  B  short-circuiting  opening one more element CAN reduce whole-house fresh air —
                       measured on the real solver in ~2 % of random configurations,
                       with drops up to 78 %. Opening a second window can convert a
                       strong single-sided exchange into a weaker net through-flow.
                       So monotonicity is NOT an invariant and is not asserted.
                       What IS asserted: the surrogate agrees with the solver on the
                       DIRECTION of an opening's effect. If the physics says this makes
                       the house airier, the playground must not say the opposite.
                       (First version asserted monotonicity. Wrong.)

  C  cooling direction with T_out below room temperature and no sun, opening an
                       exterior element cannot WARM the room it belongs to, nor the
                       house mean. This is the one the UX depends on, and it is exactly
                       the failure mode the confounded observational data would have
                       produced ("french doors open => warmer room"). Asserted hard.

  D  robustness        how an unrecognised state string is read. Informational.

A, B and C run against BOTH the real Newton solver and the distilled surrogate, and
report separately — if the solver violates something, the invariant is wrong; if only
the surrogate does, the playground would mislead.

Usage:
    python tools/test_invariants.py
    python tools/test_invariants.py --n-configs 300 --verbose
"""
from __future__ import annotations

import argparse
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import vent_io as vio          # noqa: E402
import vent_physics as vp      # noqa: E402
from vent_forecast import operable_elements, zone_list     # noqa: E402
import surrogate_runtime       # noqa: E402

WEIGHTS = surrogate_runtime.DEFAULT_WEIGHTS   # overschrijfbaar met --weights


# ------------------------------------------------------------------ airflow paths

def airflow_solver(house, states, weather, ztemps, t_out, cp):
    ops = vp.build_openings(house, states, weather, {"cp_shelter": cp}, ztemps, t_out)
    net = vp.solve_network(zone_list(house), ops, ztemps, t_out)
    return vp.effective_fresh(net["fresh"], house, states, weather, ztemps, t_out)


def make_surrogate(house):
    sur = surrogate_runtime.SurrogateAirflow(WEIGHTS, house)

    def f(house_, states, weather, ztemps, t_out, cp):
        fresh, _ = sur.predict(states, weather, ztemps, t_out, cp)
        return fresh
    return f


# ---------------------------------------------------------------------- helpers

def all_shut(elems):
    return {eid: "dicht" for eid, _ in elems}


def house_total(fresh):
    return sum(max(0.0, v) for v in fresh.values())


def rand_conditions(rng):
    return {"weather": {"wind_speed": rng.uniform(0.0, 10.0),
                        "wind_dir": rng.uniform(0, 360)},
            "t_out": rng.uniform(-2.0, 34.0), "cp": 0.214}


def rand_temps(rng, zones):
    b = rng.uniform(16.0, 28.0)
    return {z: b + rng.gauss(0, 1.5) for z in zones}


# ---------------------------------------------------------------- the invariants

def inv_a_sealed(house, elems, zones, flow, rng, n, vols):
    worst = 0.0
    for _ in range(n):
        c = rand_conditions(rng)
        fresh = flow(house, all_shut(elems), c["weather"], rand_temps(rng, zones),
                     c["t_out"], c["cp"])
        for z in zones:
            worst = max(worst, abs(fresh.get(z, 0.0)) * 3600.0 / vols[z])
    return worst


def inv_b_agreement(house, elems, zones, f_sol, f_sur, rng, n):
    agree = tested = sol_drops = sur_drops = skipped = 0
    worst = None
    for _ in range(n):
        c = rand_conditions(rng)
        zt = rand_temps(rng, zones)
        base = {eid: rng.choice(["open", "dicht", "dicht"]) for eid, _ in elems}
        shut = [e for e, _ in elems if base[e] == "dicht"]
        if not shut:
            continue
        eid = rng.choice(shut)
        opened = dict(base, **{eid: "open"})
        a0 = house_total(f_sol(house, base, c["weather"], zt, c["t_out"], c["cp"]))
        a1 = house_total(f_sol(house, opened, c["weather"], zt, c["t_out"], c["cp"]))
        b0 = house_total(f_sur(house, base, c["weather"], zt, c["t_out"], c["cp"]))
        b1 = house_total(f_sur(house, opened, c["weather"], zt, c["t_out"], c["cp"]))
        ds, du = a1 - a0, b1 - b0
        tested += 1
        sol_drops += ds < 0
        sur_drops += du < 0
        # Near zero the sign is noise on both sides and has no user-visible meaning.
        if abs(ds) < 0.01 * max(a0, 1e-6):
            skipped += 1
            continue
        if (ds > 0) == (du > 0):
            agree += 1
        elif worst is None or abs(ds) > abs(worst[1]):
            worst = (eid, ds, du)
    judged = tested - skipped
    return {"tested": tested, "judged": judged, "agree": agree, "skipped": skipped,
            "sol_drops": sol_drops, "sur_drops": sur_drops, "worst": worst}


def synthetic_timeline(house, zones, hours, t_out, states, wind=3.0):
    """A controlled night: no sun, constant cool outside. Isolates ventilation."""
    from datetime import datetime, timedelta, timezone
    t = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
    return [{"t": t + timedelta(minutes=15 * i), "dt": 900.0, "T_out": t_out,
             "irr": {z: 0.0 for z in zones}, "irr_roof": {z: 0.0 for z in zones},
             "sun_el": -10.0, "states": dict(states),
             "weather": {"wind_speed": wind, "wind_dir": 200.0, "gust": wind * 1.5}}
            for i in range(int(hours * 4))]


def inv_c_cooling(house, elems, zones, params, ctx, hours, t_out, t_room, tol):
    seed = {z: t_room for z in zones}
    base_states = all_shut(elems)
    base = vp.simulate(house, params,
                       synthetic_timeline(house, zones, hours, t_out, base_states),
                       seed, ctx, tm_seed=dict(seed))
    rows = []
    for eid, kind in elems:
        if kind == "door":
            continue
        e = house.get("windows", {}).get(eid) or house.get("vents", {}).get(eid)
        room = e.get("room")
        if room not in zones:
            continue
        sim = vp.simulate(house, params,
                          synthetic_timeline(house, zones, hours, t_out,
                                             dict(base_states, **{eid: "open"})),
                          seed, ctx, tm_seed=dict(seed))
        d_room = sim["Ta"][room] - base["Ta"][room]
        d_house = (sum(sim["Ta"][z] for z in zones)
                   - sum(base["Ta"][z] for z in zones)) / len(zones)
        rows.append((eid, room, d_room, d_house))
    return rows, [r for r in rows if r[2] > tol or r[3] > tol]


def inv_d_unknown(house, elems):
    bad = []
    for eid, kind in elems:
        e = (house.get("windows", {}).get(eid) or house.get("vents", {}).get(eid)
             or house.get("doors", {}).get(eid))
        d, got = vp._default_frac(e, kind), vp._open_frac("gibberish", e)
        if abs(got - d) > 1e-12:
            bad.append((eid, got, d))
    return bad


# ------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-configs", type=int, default=200)
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--t-out", type=float, default=12.0)
    ap.add_argument("--t-room", type=float, default=24.0)
    ap.add_argument("--tol-c", type=float, default=1e-6)
    ap.add_argument("--weights", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    global WEIGHTS
    if args.weights:
        WEIGHTS = args.weights
    house = vio.load_house()
    elems, zones = operable_elements(house), zone_list(house)
    vols = {**{r: c.get("volume_m3", 1.0) for r, c in house["rooms"].items()},
            **{j: c.get("volume_m3", 1.0) for j, c in house.get("junctions", {}).items()}}
    # Stempel de huidige revisie op de geleerde staat: anders reset merged_params bij een
    # PHYSICS_REV-bump naar de priors en meet je het surrogaat op parameters die de
    # productie nooit draait.
    params = vio.merged_params(house, dict(vio.load_learned(), physics_rev=vio.PHYSICS_REV))
    # Neutralise every non-ventilation path so C isolates ventilation.
    ctx = vio.RunContext(lat=house.get("lat", 52.09), lon=house.get("lon", 5.12),
                         neighbor_temp=args.t_room, ground_temp=args.t_room)
    f_sur = make_surrogate(house)
    failed = []
    print(f"elements {len(elems)}   zones {len(zones)}   configs {args.n_configs}\n")

    print("=" * 76)
    print("A  sealed house -> modelled fresh air is exactly zero")
    print("=" * 76)
    # The solver stops at NET_TOL, so its net flow is ~0 to convergence tolerance,
    # not exactly 0. 0.01 ACH is ~30x below any real ventilation rate.
    for name, flow, lim in (("solver", airflow_solver, 0.01), ("surrogate", f_sur, 0.05)):
        w = inv_a_sealed(house, elems, zones, flow, random.Random(7),
                         max(1, args.n_configs // 4), vols)
        ok = w <= lim
        print(f"  {name:10} worst {w:.4f} ACH   limit {lim}   {'ok' if ok else 'FAIL'}")
        if not ok:
            failed.append(f"A/{name}")
    print("  NB: no background-infiltration term exists — a shut house exchanges no air")
    print("      in this model. Real infiltration is absorbed into the learned ua_env.")

    print("\n" + "=" * 76)
    print("B  surrogate agrees with the solver on the DIRECTION of an opening's effect")
    print("=" * 76)
    r = inv_b_agreement(house, elems, zones, airflow_solver, f_sur,
                        random.Random(11), args.n_configs)
    rate = r["agree"] / max(r["judged"], 1)
    print(f"  agreement {r['agree']}/{r['judged']} ({rate:.1%})   "
          f"({r['skipped']} near-zero cases skipped)")
    print(f"  opening REDUCED total fresh air: solver {r['sol_drops']}/{r['tested']}, "
          f"surrogate {r['sur_drops']}/{r['tested']}  <- short-circuiting is real physics")
    if r["worst"]:
        eid, ds, du = r["worst"]
        print(f"  worst disagreement: {eid}  solver {ds:+.4f} m3/s, surrogate {du:+.4f}")
    if rate < 0.90:
        failed.append("B/agreement")
        print("  FAIL: too often points the opposite way to the physics.")
    else:
        print("  ok")

    print("\n" + "=" * 76)
    print(f"C  T_out={args.t_out}°C < T_room={args.t_room}°C, no sun: opening cannot warm")
    print("=" * 76)
    for name in ("solver", "surrogate"):
        restore = (surrogate_runtime.install(house, params.get("cp_shelter", 0.2), WEIGHTS)
                   if name == "surrogate" else None)
        try:
            rows, viol = inv_c_cooling(house, elems, zones, params, ctx,
                                       args.hours, args.t_out, args.t_room, args.tol_c)
        finally:
            if restore:
                restore()
        rows.sort(key=lambda x: x[2])
        print(f"  {name}: {len(rows)} exterior elements, {len(viol)} violations")
        if args.verbose or viol:
            print(f"    {'element':24}{'room':9}{'dT room':>10}{'dT house':>10}")
            for eid, room, dr, dh in (viol or rows[:3]):
                flag = "  <-- VIOLATION" if (dr > args.tol_c or dh > args.tol_c) else ""
                print(f"    {eid:24}{room:9}{dr:10.4f}{dh:10.4f}{flag}")
        if viol:
            failed.append(f"C/{name}")

    print("\n" + "=" * 76)
    print("D  robustness: how an UNRECOGNISED state string is read")
    print("=" * 76)
    bad = inv_d_unknown(house, elems)
    print(f"  {len(bad)}/{len(elems)} elements read an unknown string as SHUT while their")
    print("  documented default is open. Not a physics bug (absent and unrecognised are")
    print("  different cases) but a real footgun: a typo silently closes an always-open")
    print("  trickle vent. The UI must only emit strings from the exported `frac` map.")
    for eid, got, d in bad[:5]:
        print(f"    {eid:24} unknown -> {got:.3f}, default {d:.3f}")
    vocab_bad = [eid for eid, _ in elems
                 for e in [house.get("windows", {}).get(eid)
                           or house.get("vents", {}).get(eid)
                           or house.get("doors", {}).get(eid)]
                 if not (vp._open_frac("open", e) >= vp._open_frac("half", e)
                         >= vp._open_frac("dicht", e))]
    print(f"\n  vocabulary ordering open >= half >= dicht: "
          f"{len(elems) - len(vocab_bad)}/{len(elems)} ok")
    if vocab_bad:
        print(f"    violated by: {vocab_bad}")
        failed.append("D/vocab")

    print("\n" + "=" * 76)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        sys.exit(1)
    print("all invariants hold")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Held-out evaluatie van de anker-stap in Teds nachtvoorspelling (Project 10).

Waarom dit hulpje bestaat: de anker-stap (`night_forecast.anchor_now` +
`anchor_mass_now`) is veruit de grootste foutbron in die voorspelling — groter
dan de fysica-parameters — en hij is *stil*. Elke variant draait gewoon door;
alleen de nachtcurve staat er structureel naast. De keuze tussen de schatters
hoort dus op gemeten held-out-bewijs te rusten, niet op smaak, en dit script is
wat die getallen produceert (ze staan geciteerd in `anchor_mass_now`).

Methode — replay van het PRODUCTIEPAD over de maand-shards:
- Eén virtuele "nu" per avond op EVENING (18:45, het orchestrator-doel), over de
  hele shard-span; een avond doet mee als zowel de 24u aanloop als de nacht erna
  gedekt is.
- Fase 1 = exact `night_forecast.main()`: `build_timeline` (24u warmup, end_h=0,
  om_bias-driver, beam_iam) + `apply_shade_routine`, geseed op de oudste meting
  in het venster, `simulate(snapshot_t=vnow)` → de blinde `Ta_now`/`Tm_now`.
- Dan de **arm**: de anker-variant die we scoren.
- Fase 2 = de forecast-sim tot morgen 08:00, geseed op het anker. Bewust op de
  **échte gerapporteerde log** (géén scenario-forcering): zo is het residu aan
  het anker toe te schrijven en niet aan een raamstand-aanname.
- Score = voorspeld − gemeten op de echte tado-samples, per horizon-emmer.
  Dezelfde hygiëne als de fit: AC-kamer, vannacht gestookte kamers en
  huis-brede-pauze-nachten vallen af.

Het weer komt voor beide fasen uit de shard-rijen (archief-grade, dus geen
forecast-fout in de vergelijking) — identiek over alle armen, dus het weegt de
armen niet tegen elkaar. Deterministisch: geen RNG, geen netwerk.

Gebruik:
    python tools/night_anchor_experiment.py [--arms a,b,...] [--room ted]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import night_forecast as nf              # noqa: E402
import vent_io as vio                    # noqa: E402
import vent_physics as vp                # noqa: E402
from shared_const import TZ              # noqa: E402

EVENING = (18, 45)          # lokaal uur/minuut van de virtuele nu (orchestrator-doel)
MATCH_TOL_S = 450           # sample ↔ rasterpunt: hoogstens een halve stap ernaast
# Horizon-emmers (label, ondergrens, bovengrens) in uren vanaf de virtuele nu.
BUCKETS = [("h≈1", 0.5, 1.5), ("h≤3", 0.0, 3.0), ("h 3–8", 3.0, 8.0),
           ("h>8", 8.0, None), ("heel", None, None)]


# ── Armen ────────────────────────────────────────────────────────────────────────────
# Elke arm: (ta_sim, tm_sim, actual, now) -> (ta_seed, tm_seed) voor de forecast-sim.

def arm_geen_anker(ta_sim, tm_sim, actual, now):
    """Vrijloop: de warmup-toestand gaat ongecorrigeerd de nacht in."""
    return dict(ta_sim), dict(tm_sim)


def arm_alleen_lucht(ta_sim, tm_sim, actual, now):
    """Alleen de luchtknoop ijken — de massa houdt de warmup-drift vast en trekt via
    H_am de zojuist geijkte lucht er binnen een paar uur weer doorheen."""
    return nf.anchor_now(ta_sim, actual, now), dict(tm_sim)


def arm_gedempt_gemiddelde(ta_sim, tm_sim, actual, now):
    """De massa uit de metingen schatten (EWMA), los van de sim — het gedrag vóór
    de starre schuif."""
    return nf.anchor_now(ta_sim, actual, now), nf.anchor_mass_now(tm_sim, actual, now)


def arm_starre_schuif(ta_sim, tm_sim, actual, now):
    """Massa over dezelfde δ als de lucht, zónder terugval waar het luchtanker niet
    vuurde (dán blijft de massa dus op zijn weggedreven warmup-waarde staan)."""
    ta = nf.anchor_now(ta_sim, actual, now)
    d = nf.anchor_delta(ta_sim, ta)
    return ta, {rid: v + d.get(rid, 0.0) for rid, v in tm_sim.items()}


def arm_productie(ta_sim, tm_sim, actual, now):
    """Wat `night_forecast.main()` daadwerkelijk doet: starre schuif waar het
    luchtanker vuurde, gedempt gemiddelde als terugval."""
    ta = nf.anchor_now(ta_sim, actual, now)
    return ta, nf.anchor_mass_now(tm_sim, actual, now,
                                  air_delta=nf.anchor_delta(ta_sim, ta))


def arm_tm_is_ta(ta_sim, tm_sim, actual, now):
    """De massa gelijkstellen aan de geijkte lucht — past beter, maar fysisch een
    overschatting die enkel de resterende koud-bias wegstreept (zie anchor_mass_now)."""
    ta = nf.anchor_now(ta_sim, actual, now)
    d = nf.anchor_delta(ta_sim, ta)
    return ta, {rid: (ta[rid] if rid in d else v) for rid, v in tm_sim.items()}


ARMS = {
    "geen_anker": arm_geen_anker,
    "alleen_lucht": arm_alleen_lucht,
    "gedempt_gemiddelde": arm_gedempt_gemiddelde,
    "starre_schuif": arm_starre_schuif,
    "productie": arm_productie,
    "tm_is_ta": arm_tm_is_ta,
}


# ── Replay ───────────────────────────────────────────────────────────────────────────

def _slice(actual: dict, since: datetime, until: datetime) -> dict:
    out = {}
    for rid, samples in actual.items():
        s = [(t, v) for t, v in samples if since <= t <= until]
        if s:
            out[rid] = s
    return out


def _weather(rows: list[dict], until: datetime | None = None) -> dict:
    """Shard-weer in fetch_weather-vorm; `until` kapt de toekomstkennis eraf."""
    return {"hourly": [r for r in rows if until is None or r["dt"] <= until], "current": {}}


def _rmse(v: list[float]) -> float | None:
    return (sum(x * x for x in v) / len(v)) ** 0.5 if v else None


def evenings(rows, actual_all, t0, t1) -> list[datetime]:
    out, d = [], t0.date()
    while d <= t1.date():
        v = datetime.combine(d, datetime.min.time(), TZ).replace(hour=EVENING[0],
                                                                minute=EVENING[1])
        if (v - timedelta(hours=nf.WARMUP_H)) >= t0 and (v + timedelta(hours=14)) <= t1 \
           and v >= rows[0]["dt"] + timedelta(hours=nf.WARMUP_H + 1):
            out.append(v)
        d += timedelta(days=1)
    return out


def run(arms: dict) -> list[dict]:
    """Per bruikbare avond: per arm per kamer de (horizon_h, fout)-paren."""
    house = vio.load_house()
    dataset = vio.load_dataset(house)
    om_learned = vio.om_learned_from(vio.load_window_data())
    params = vio.merged_params(house, vio.load_learned())
    rows, log, actual_all = dataset["weather_rows"], dataset["log"], dataset["actual"]
    heat_on = dataset.get("heat_on", {})
    all_ts = sorted(t for s in actual_all.values() for t, _ in s)
    if not all_ts:
        raise SystemExit("[anker] geen kamersamples in de shards — eerst backfillen?")
    t0, t1 = all_ts[0], all_ts[-1]
    print(f"[anker] shard-span {t0:%Y-%m-%d} .. {t1:%Y-%m-%d}")
    ac, pch = vio.ac_changes(log), vio.pause_changes(log)

    nights = []
    for vnow in evenings(rows, actual_all, t0, t1):
        past = _weather(rows, vnow)
        ctx = vio.make_context(house, past, vnow)
        warm_tl = vio.build_timeline(house, past, log, vnow, nf.WARMUP_H, ctx,
                                     beam_iam=True, end_h=0.0, om_learned=om_learned)
        if not warm_tl:
            continue
        warm_tl = nf.apply_shade_routine(warm_tl)
        actual = _slice(actual_all, vnow - timedelta(hours=nf.WARMUP_H), vnow)
        if not actual:
            continue
        seed = {rid: s[0][1] for rid, s in actual.items()}
        for rid in house.get("rooms", {}):
            seed.setdefault(rid, warm_tl[0]["T_out"])
        warm = vp.simulate(house, params, warm_tl, seed, ctx, snapshot_t=vnow)
        ta_sim = dict(warm.get("Ta_now", warm["Ta"]))
        tm_sim = dict(warm.get("Tm_now", warm["Tm"]))

        end_h = nf.hours_until_morning(vnow)
        end_t = vnow + timedelta(hours=end_h)
        fc_tl = vio.build_timeline(house, _weather(rows), log, vnow, 0.0, ctx,
                                   beam_iam=True, end_h=end_h, om_learned=om_learned)
        if not fc_tl or fc_tl[-1]["t"] < end_t - timedelta(minutes=30):
            continue
        fc_tl = nf.apply_shade_routine(fc_tl)

        if any(s <= vnow <= e for s, e in vio.paused_intervals(pch, end_t)):
            continue
        truth = _slice(actual_all, vnow + timedelta(minutes=1), end_t)
        ac_room = vio.ac_room_at(ac, vnow)
        drop = {ac_room} if ac_room else set()
        for rid in truth:
            if any(vnow <= t <= end_t for t in heat_on.get(rid, ())):
                drop.add(rid)
        truth = {r: s for r, s in truth.items() if r not in drop}
        if not truth:
            continue

        entry = {"t": vnow, "fired": set(nf.anchor_delta(ta_sim,
                                                         nf.anchor_now(ta_sim, actual, vnow))),
                 "arms": {}}
        for name, fn in arms.items():
            ta, tm = fn(ta_sim, tm_sim, actual, vnow)
            sim = vp.simulate(house, params, fc_tl, ta, ctx, tm_seed=tm)
            per = {}
            for rid, samples in truth.items():
                series = sim["series"].get(rid) or []
                if not series:
                    continue
                errs = []
                for t, meas in samples:
                    ts, pred = min(series, key=lambda x: abs((x[0] - t).total_seconds()))
                    if abs((ts - t).total_seconds()) <= MATCH_TOL_S:
                        errs.append(((t - vnow).total_seconds() / 3600.0, pred - meas))
                if errs:
                    per[rid] = errs
            entry["arms"][name] = per
        nights.append(entry)
    return nights


# ── Rapport ──────────────────────────────────────────────────────────────────────────

def _pool(nights, arm, room=None, fired=None):
    out = []
    for n in nights:
        for rid, errs in n["arms"][arm].items():
            if room is not None and rid != room:
                continue
            if fired is not None and (rid in n["fired"]) != fired:
                continue
            out.extend(errs)
    return out


def report(nights: list[dict], arms: dict, room: str) -> None:
    print(f"[anker] {len(nights)} bruikbare nachten; luchtanker vuurde voor {room} op "
          f"{sum(1 for n in nights if room in n['fired'])}/{len(nights)} avonden\n")

    for scope, kw in ((room, {"room": room}), ("alle kamers", {})):
        print(f"── RMSE/bias per horizon — {scope} " + "─" * 30)
        print(f"{'arm':<20}" + "".join(f"{b[0]:>16}" for b in BUCKETS))
        for a in arms:
            pairs = _pool(nights, a, **kw)
            line = f"{a:<20}"
            for _, lo, hi in BUCKETS:
                e = [d for h, d in pairs
                     if (lo is None or h >= lo) and (hi is None or h < hi)]
                line += f"{_rmse(e):>9.3f}/{sum(e)/len(e):+.2f}" if e else f"{'-':>16}"
            print(line)
        print(f"{'(n samples)':<20}{len(_pool(nights, next(iter(arms)), **kw)):>16}\n")

    ref = "gedempt_gemiddelde"
    if ref in arms:
        print(f"── Gepaard per nacht t.o.v. {ref} (nacht-RMSE) " + "─" * 20)
        for scope, kw in ((room, {"room": room}), ("alle kamers", {})):
            print(f"  scope={scope}")
            for a in arms:
                if a == ref:
                    continue
                diffs = []
                for n in nights:
                    x = _rmse([d for _, d in _pool([n], a, **kw)])
                    y = _rmse([d for _, d in _pool([n], ref, **kw)])
                    if x is not None and y is not None:
                        diffs.append(x - y)
                if len(diffs) < 2:
                    continue
                m = sum(diffs) / len(diffs)
                sd = (sum((q - m) ** 2 for q in diffs) / (len(diffs) - 1)) ** 0.5
                se = sd / len(diffs) ** 0.5 or float("inf")
                print(f"    {a:<20} Δ {m:+.3f} ± {se:.3f} (t={m/se:+.1f}) "
                      f"beter op {sum(1 for q in diffs if q < 0)}/{len(diffs)}")
            print()

    # De kern van de terugval-keuze: de starre schuif wint waar het luchtanker vuurde en
    # stort in waar het niet vuurde. Splits de kamer-nachten dus expliciet.
    print("── Gesplitst op wél/niet gevuurd luchtanker (RMSE, alle kamers) " + "─" * 3)
    print(f"{'arm':<20}{'wél gevuurd':>14}{'niet gevuurd':>15}")
    for a in arms:
        ja = [d for _, d in _pool(nights, a, fired=True)]
        nee = [d for _, d in _pool(nights, a, fired=False)]
        print(f"{a:<20}" + (f"{_rmse(ja):>14.3f}" if ja else f"{'-':>14}")
              + (f"{_rmse(nee):>15.3f}" if nee else f"{'-':>15}"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arms", default=",".join(ARMS),
                    help=f"komma-lijst uit: {', '.join(ARMS)}")
    ap.add_argument("--room", default=nf.ROOM_ID, help="kamer voor de losse kolom")
    args = ap.parse_args()
    arms = {a: ARMS[a] for a in args.arms.split(",") if a in ARMS}
    if not arms:
        raise SystemExit(f"[anker] geen geldige armen in {args.arms!r}")
    nights = run(arms)
    if not nights:
        raise SystemExit("[anker] geen bruikbare nachten — shards te dun?")
    report(nights, arms, args.room)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Held-out evaluatiecampagne voor de herbouwde ventilatie-tweeling (Project 13).

Opvolger van tools/twin2_experiment.py, met dezelfde bewezen methodologie maar
zonder de twin-2-machinerie (batch-fit, RH-kanaal, anker-warm-start):

- **Niet-overlappende vensters** van --days dagen (stride == venster — overlap
  lekt held-out-data naar de training), achterwaarts verankerd op het jongste
  shard-sample zodat het meest recente venster altijd meedoet.
- **K-vouws-rotaties** (default 3, --rotations): rotatie r houdt de vensters
  i % K == r apart; over alle rotaties samen is élk venster precies één keer
  held-out.
- **Training per arm per rotatie** = replay van de PRODUCTIE-onlinefit over de
  train-vensters, chronologisch (het tools/vent_seed.py-patroon): virtuele nu
  per --step-h, per stap vent_io.build_timeline (48u venster + 24u warmup,
  om_bias-driver uit de checkout, beam_iam) + de AC/verwarmings/pauze-filters +
  vent_fit.calibrate met productie-defaults, params voorwaarts gedragen. Het
  kalibratie-slice wordt op de vensterstart geklemd zodat samples van een held
  venster nooit in een fit lekken.
- **Evaluatie per held venster**: 5d-vrijloop (primair; pooled residuen, geseed
  uit de eerste samples) + hergeseede 48u-segmenten (co-primair — dít spiegelt
  wat het dashboard scoort) + de zon-/middag-plakken + per-kamer + het
  gerailde-params-rapport (vent_fit.railed_params). De §6.2-campagnevraag is
  PRIMAIR of het vloer-railen van ua_env/ua_party/f_air (secundair
  vent_eff/solar_gain/c_mass) ontspant mét de eenzijdige-ventilatieterm
  t.o.v. zónder (arm `no_single_sided`).

Elke arm draait als eigen procés (--jobs breed): de term-uit-arm patcht een
vent_physics-module-attribuut (`single_sided_fresh`; `effective_fresh` resolvet
'm via de module-globals op call-time), en dat mag een andere arm niet zien.
Deterministisch: geen RNG — de A/A-armen jitteren de startvector alternerend
±2% op param-index (hun held-out-verschil ís de ruisvloer), en de
rapport-stempel komt uit --stamp (default: het jongste shard-sample, NIET de
wandklok).

Gebruik:
    python tools/vent_experiment.py [--arms baseline,no_single_sided,aa_1,aa_2]
                                    [--days 5] [--step-h 6] [--rotations 3]
                                    [--jobs 2] [--refresh-weather] [--stamp S]
    python tools/vent_experiment.py --arm baseline --stamp S    # één arm (worker)

Rapport: tabel op stdout + JSON in tools/out/vent_experiment_<stamp>.json.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import timedelta

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
for _p in (_ROOT, _TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vent_fit as vf                                  # noqa: E402
import vent_io as vio                                  # noqa: E402
import vent_physics as vp                              # noqa: E402
from vent_seed import _slice_actual, _weather_view     # noqa: E402 — het replay-patroon

WINDOW_D = 5.0            # dagen per venster; stride == venster (lek-vrije holdout)
DEFAULT_ROTATIONS = 3
DEFAULT_STEP_H = 6.0      # uur tussen twee virtuele nu's in de train-replay
SEG_OFFSETS_H = (0.0, 48.0)   # 48u-segmenten binnen elk held venster
SEG_WARMUP_H = 24.0
COVERAGE_FRAC = 0.5       # kamer <50% dekking in een held venster → uit diens scoring
# Zon-/middag-plakken van de held-out-residuen (overgenomen van de twin-2-campagne):
# de fout van de tweeling correleert sterk met de instraling, dus een arm die alléén
# het etmaalgemiddelde verbetert lost het eigenlijke probleem niet op.
SUNNY_WM2 = 300.0         # globale horizontale instraling waarboven een sample "zonnig" heet
MIDDAY_H = (10, 16)       # lokale uren [start, eind) voor de middag-plak
# §6.2: de primaire uitkomst is het rail-gedrag van deze parameters.
KEY_FLOOR_PARAMS = ("ua_env", "ua_party", "f_air")          # floor-railen = kernuitkomst
KEY_SECONDARY_PARAMS = ("vent_eff", "solar_gain", "c_mass")  # secundair (elke kant)

OUT_DIR = os.path.join(_TOOLS, "out")

# ── Armen ────────────────────────────────────────────────────────────────────────────
# Uitbreidbaar register: één dict-entry per toekomstige term-toggle; de sleutel wordt in
# apply_config afgehandeld (onbekende sleutels zijn bewust een no-op, zodat een nieuwe
# arm-config nooit een oude worker laat crashen).
ARMS: dict[str, dict] = {
    # vergelijker: de huidige productie-fysica, ongewijzigd.
    "baseline": {},
    # ablation: de de Gids & Phaff-eenzijdige-ventilatieterm uit — meet of het
    # kern-floor-railen (ua_env/ua_party/f_air) mét de term ontspant.
    "no_single_sided": {"no_single_sided": True},
    # A/A-ruisvloer: baseline met deterministische alternerende ±2%-start-jitter.
    "aa_1": {"jitter": 0.02},
    "aa_2": {"jitter": -0.02},
}


def _no_single_sided(*_a, **_k) -> dict:
    """Vervanger voor vent_physics.single_sided_fresh in de term-uit-arm: geen enkele
    kamer krijgt een eenzijdige bijdrage, dus effective_fresh reduceert tot het pure
    netwerk-nettodebiet (het pre-rev-5-gedrag)."""
    return {}


def apply_config(cfg: dict) -> None:
    """Zet de variant-haakjes (proces-lokaal). `effective_fresh` roept
    `single_sided_fresh` via de module-globals van vent_physics aan, dus een
    attribuut-patch werkt op call-time — vent_physics zelf blijft ongewijzigd.
    Afwezige/onbekende sleutels laten de productie-defaults staan."""
    if cfg.get("no_single_sided"):
        vp.single_sided_fresh = _no_single_sided


def jitter_params(params: dict, j: float, house: dict | None = None) -> dict:
    """Deterministische start-jitter (geen RNG): elke parameter ±`j` fractioneel,
    alternerend van teken per param-index — het twin-2-campagnepatroon. Geklemd op
    BOUNDS. De A/A-armen meten hiermee de gevoeligheid van de hele campagne voor de
    startvector; hun held-out-verschil ís de ruisvloer."""
    rooms = sorted(rid for rid in params if isinstance(params[rid], dict))
    keys = vf._param_keys(rooms)
    vec = vf.params_to_vec(params, keys)
    vec = [v * (1.0 + j * (1 if i % 2 == 0 else -1)) for i, v in enumerate(vec)]
    return vf.vec_to_params(vf._clamp_to_bounds(vec, keys, house), keys, params, house)


def start_params_for(house: dict, cfg: dict) -> dict:
    """Startvector: kale priors (er is geen batch-anker meer om vanaf te warm-starten —
    de replay ís de productie-leerroute), met de A/A-jitter erbovenop."""
    start = vio.default_params(house)
    j = cfg.get("jitter")
    return jitter_params(start, j, house) if j else start


# ── Venster-geometrie ────────────────────────────────────────────────────────────────

def experiment_windows(dataset: dict, actual_f: dict, window_d: float) -> list[tuple]:
    """Niet-overlappende (start, eind)-vensters van `window_d` dagen, achterwaarts
    verankerd op het jongste sample (het meest recente venster doet altijd mee) en
    chronologisch teruggegeven. Stride == venster: géén overlap, dus een held venster
    deelt geen enkel sample met een train-venster. Vensters zonder (gefilterde)
    samples vallen af."""
    ts_all = [t for s in dataset["actual"].values() for t, _ in s]
    if not ts_all:
        return []
    t_min, t_max = min(ts_all), max(ts_all)
    ends = []
    end = t_max
    first_end = t_min + timedelta(days=window_d)
    while end >= first_end:
        ends.append(end)
        end -= timedelta(days=window_d)
    ends.reverse()
    return [(e - timedelta(days=window_d), e) for e in ends
            if _slice_actual(actual_f, e - timedelta(days=window_d), e)]


def held_indices(n: int, rotations: int) -> list[list[int]]:
    """K-vouws-verdeling: rotatie r houdt de vensters i met i % rotations == r apart.
    Over alle rotaties samen is élk venster precies één keer held-out."""
    return [[i for i in range(n) if i % rotations == r] for r in range(rotations)]


def prefilter(dataset: dict) -> dict:
    """De productie-hygiënefilters (verwarming / airco / pauze) één keer over de hele
    dataset. Historisch equivalent aan het per-stap filteren van de runner: de log is
    hier teruggedateerd compleet, dus de live AC-guard vervalt (guard_h=0) — het
    prepare_windows-precedent van de twin-2-campagne."""
    ts_all = [t for s in dataset["actual"].values() for t, _ in s]
    if not ts_all:
        return {}
    t_max = max(ts_all)
    log = dataset["log"]
    act, _ = vf.filter_heating_samples(dataset["actual"], dataset.get("heat_on", {}))
    act, _ = vf.filter_ac_samples(act, vio.ac_changes(log), None, t_max, guard_h=0.0)
    act, _ = vf.filter_paused_samples(act, vio.paused_intervals(vio.pause_changes(log), t_max))
    return act


# ── Training: replay van de productie-onlinefit ──────────────────────────────────────

def train_replay(house: dict, dataset: dict, actual_f: dict, train: list[tuple],
                 step_h: float, om_learned: dict | None, params: dict) -> tuple[dict, int]:
    """Replay de productie-onlinefit over de train-vensters, chronologisch (het
    tools/vent_seed.py-patroon): per virtuele nu een verse RunContext
    (vio.make_context — geen module-globals), build_timeline + calibrate met
    productie-defaults, params voorwaarts gedragen. Seeds/massaknoop/γ-basis komen
    uit de RUWE samples en de fit draait op de GEFILTERDE — exact als de runner.
    Het kalibratie-slice klemt op de vensterstart zodat held-out samples nooit in
    een fit lekken (kost: een korter slice op de eerste stappen van elk venster).
    Geeft (eindparams, #gelukte fit-stappen)."""
    window_h = vio.CALIB_WINDOW_H + vio.WARMUP_H
    log = dataset["log"]
    steps = 0
    for w_start, w_end in train:
        n_vnows = int((w_end - w_start).total_seconds() / 3600.0 / step_h)
        for k in range(1, n_vnows + 1):
            vnow = w_start + timedelta(hours=step_h * k)
            weather = _weather_view(dataset["weather_rows"], vnow)
            rows = weather["hourly"]
            if not rows or (vnow - rows[0]["dt"]).total_seconds() / 3600.0 < window_h:
                continue
            since = max(vnow - timedelta(hours=vio.CALIB_WINDOW_H), w_start)
            raw = _slice_actual(dataset["actual"], since, vnow)
            actual = _slice_actual(actual_f, since, vnow)
            if not raw or not actual:
                continue
            ctx = vio.make_context(house, weather, vnow)
            seed = {rid: s[0][1] for rid, s in raw.items()}
            tm_seed = {rid: sum(v for _, v in s) / len(s) for rid, s in raw.items()}
            measured = {rid: list(s) for rid, s in raw.items()}
            timeline = vio.build_timeline(house, weather, log, vnow, window_h, ctx,
                                          beam_iam=True, end_h=0.0, om_learned=om_learned)
            if not timeline:
                continue
            for rid in house.get("rooms", {}):
                seed.setdefault(rid, timeline[0]["T_out"])
            solar = [r["shortwave"] for r in rows[-int(vio.CALIB_WINDOW_H):]
                     if r.get("shortwave") is not None]
            solar_mean = round(sum(solar) / len(solar)) if solar else None
            params, _ = vf.calibrate(house, params, timeline, seed, ctx, actual,
                                     solar_mean=solar_mean, tm_seed=tm_seed,
                                     measured=measured)
            steps += 1
    return params, steps


# ── Held-out-evaluatie ───────────────────────────────────────────────────────────────

def _room_ok(samples: list, window_d: float) -> bool:
    return len(samples) >= COVERAGE_FRAC * window_d * 24 * 4


def _solar_series(timeline: list[dict]) -> list[tuple]:
    """(t, globale horizontale instraling W/m²) per tijdlijnstap — Open-Meteo's
    `direct` staat al op het horizontale vlak, dus direct + diffuus ≈ GHI. Voedt de
    zon-plak van de held-out-evaluatie."""
    return [(s["t"], (s["weather"].get("direct") or 0.0) + (s["weather"].get("diffuse") or 0.0))
            for s in timeline]


def evaluate(house: dict, dataset: dict, actual_f: dict, held: list[tuple],
             params: dict, om_learned: dict | None) -> dict:
    """Held-out-evaluatie: vrijloop over het volle venster (pooled residuen, geseed uit
    de eerste samples — óók de massaknoop: het productie-venstergemiddelde zou held-out-
    toekomst in de beginconditie lekken) + hergeseede 48u-segmenten + zon-/middag-plakken
    + per-kamer. `measured` (de koker-γ-regressiebasis) wordt bewust NIET meegegeven:
    dat zíjn de held metingen. Kamers onder de dekkingsdrempel vallen uit de scoring."""
    rows = dataset["weather_rows"]
    log = dataset["log"]
    pools = {"5d": [0.0, 0], "48h": [0.0, 0], "sunny": [0.0, 0], "midday": [0.0, 0]}
    per_room: dict[str, list] = {}
    per_window: dict[str, dict] = {}
    for w_start, w_end in held:
        days = (w_end - w_start).total_seconds() / 86400.0
        act = {rid: s for rid, s in _slice_actual(actual_f, w_start, w_end).items()
               if _room_ok(s, days)}
        if not act:
            continue
        # Venster-eigen ankers (buur + bodem): make_context kijkt alleen naar historie
        # ≤ w_end, dus de volle weer-rijen zijn hier veilig (drivers zijn exogeen).
        ctx = vio.make_context(house, {"hourly": rows}, w_end)
        window_h = (w_end - w_start).total_seconds() / 3600.0 + SEG_WARMUP_H
        timeline = vio.build_timeline(house, {"hourly": rows, "current": {}}, log, w_end,
                                      window_h, ctx, beam_iam=True, end_h=0.0,
                                      om_learned=om_learned)
        if not timeline:
            continue
        solar = _solar_series(timeline)
        seed = {rid: s[0][1] for rid, s in act.items()}
        for rid in house.get("rooms", {}):
            seed.setdefault(rid, timeline[0]["T_out"])
        sim = vp.simulate(house, params, timeline, seed, ctx,
                          calib_only_rooms=set(act), tm_seed=dict(seed))
        w_res: list[float] = []
        for rid, samples in act.items():
            pred = sim["series"].get(rid, [])
            if not pred:
                continue
            pred = vp._to_sensor_series(house, timeline, rid, pred)
            res = [vp._interp(pred, ts) - val for ts, val in samples]
            w_res += res
            rp = per_room.setdefault(rid, [0.0, 0])
            rp[0] += sum(r * r for r in res)
            rp[1] += len(res)
            for (ts, _), r in zip(samples, res):
                if vp._interp(solar, ts) >= SUNNY_WM2:
                    pools["sunny"][0] += r * r
                    pools["sunny"][1] += 1
                if MIDDAY_H[0] <= ts.hour < MIDDAY_H[1]:
                    pools["midday"][0] += r * r
                    pools["midday"][1] += 1
        pools["5d"][0] += sum(r * r for r in w_res)
        pools["5d"][1] += len(w_res)
        per_window[w_end.date().isoformat()] = {
            "rmse": round(vf.rmse(w_res), 3) if w_res else None, "n": len(w_res)}
        # 48u-segmenten: hergeseed op offsets binnen het venster (dashboard-protocol:
        # ~48u-vrijloop na een korte aanloop, seed = eerste sample in het segment).
        for off_h in SEG_OFFSETS_H:
            seg_start = w_start + timedelta(hours=off_h)
            seg_end = seg_start + timedelta(hours=48.0)
            if seg_end > w_end:
                continue
            warm_start = seg_start - timedelta(hours=SEG_WARMUP_H)
            tl_seg = [s for s in timeline if warm_start <= s["t"] <= seg_end]
            if not tl_seg:
                continue
            seed_seg = {}
            for rid, samples in act.items():
                inseg = [v for ts, v in samples if seg_start <= ts <= seg_end]
                if inseg:
                    seed_seg[rid] = inseg[0]
            for rid in house.get("rooms", {}):
                seed_seg.setdefault(rid, tl_seg[0]["T_out"])
            sim_seg = vp.simulate(house, params, tl_seg, seed_seg, ctx,
                                  calib_only_rooms=set(act), tm_seed=dict(seed_seg))
            for rid, samples in act.items():
                pred = sim_seg["series"].get(rid, [])
                if not pred:
                    continue
                pred = vp._to_sensor_series(house, tl_seg, rid, pred)
                res48 = [vp._interp(pred, ts) - val for ts, val in samples
                         if seg_start <= ts <= seg_end]
                pools["48h"][0] += sum(r * r for r in res48)
                pools["48h"][1] += len(res48)
    out: dict = {"pools": pools, "per_room": per_room, "per_window": per_window}
    for key, label in (("5d", "rmse_5d"), ("48h", "rmse_48h"),
                       ("sunny", "rmse_sunny"), ("midday", "rmse_midday")):
        ss, n = pools[key]
        out[label] = round(math.sqrt(ss / n), 4) if n else None
        out[f"n_{key}"] = n
    return out


# ── Aggregatie + rails ───────────────────────────────────────────────────────────────

def _rail_name(entry: str) -> tuple[str, str]:
    """'scope.param@floor' → (param, kant)."""
    left, _, side = entry.partition("@")
    _, _, name = left.rpartition(".")
    return name, side


def summarize(rots: list[dict]) -> dict:
    """Pool de rotatie-resultaten van één arm: elke venster-residu telt precies één keer
    (elk venster is één keer held-out), dus de gecombineerde RMSE is de wortel van de
    gepoolde kwadraatsom. Rails: geteld over de eind-params van álle rotaties, met de
    §6.2-kernparams apart uitgelicht."""
    pools: dict[str, list] = {}
    per_room: dict[str, list] = {}
    per_window: dict[str, dict] = {}
    rails_all: list[str] = []
    for rr in rots:
        ev = rr["eval"]
        for key, (ss, n) in ev["pools"].items():
            a = pools.setdefault(key, [0.0, 0])
            a[0] += ss
            a[1] += n
        for rid, (ss, n) in ev["per_room"].items():
            a = per_room.setdefault(rid, [0.0, 0])
            a[0] += ss
            a[1] += n
        per_window.update(ev["per_window"])
        rails_all += rr["railed"]
    out: dict = {}
    for key, label in (("5d", "rmse_5d"), ("48h", "rmse_48h"),
                       ("sunny", "rmse_sunny"), ("midday", "rmse_midday")):
        ss, n = pools.get(key, (0.0, 0))
        out[label] = round(math.sqrt(ss / n), 4) if n else None
        out[f"n_{key}"] = n
    out["per_room"] = {rid: (round(math.sqrt(ss / n), 3) if n else None)
                       for rid, (ss, n) in sorted(per_room.items())}
    out["per_window"] = per_window
    out["railed_total"] = len(rails_all)
    out["railed_key_floor"] = sum(1 for r in rails_all
                                  if _rail_name(r)[0] in KEY_FLOOR_PARAMS
                                  and _rail_name(r)[1] == "floor")
    out["railed_secondary"] = sum(1 for r in rails_all
                                  if _rail_name(r)[0] in KEY_SECONDARY_PARAMS)
    out["railed_union"] = sorted(set(rails_all))
    return out


# ── Eén arm (worker) ─────────────────────────────────────────────────────────────────

def run_arm(arm: str, days: float, step_h: float, rotations: int) -> dict:
    cfg = ARMS[arm]
    t0 = time.time()
    apply_config(cfg)
    house = vio.load_house()
    dataset = vio.load_dataset(house)
    om_learned = vio.om_learned_from(vio.load_window_data())
    actual_f = prefilter(dataset)
    wins = experiment_windows(dataset, actual_f, days)
    if not wins:
        raise SystemExit(f"[{arm}] geen vensters — shards te dun?")
    print(f"[{arm}] {len(wins)} vensters: {[e.date().isoformat() for _, e in wins]}")
    rots = []
    for r, held_idx in enumerate(held_indices(len(wins), rotations)):
        train = [w for i, w in enumerate(wins) if i not in set(held_idx)]
        held = [wins[i] for i in held_idx]
        params = start_params_for(house, cfg)
        params, steps = train_replay(house, dataset, actual_f, train, step_h,
                                     om_learned, params)
        ev = evaluate(house, dataset, actual_f, held, params, om_learned)
        rails = vf.railed_params(params, house=house)
        print(f"[{arm}] rotatie {r}: held {held_idx}, {steps} fit-stappen → "
              f"5d {ev['rmse_5d']}°C · 48u {ev['rmse_48h']}°C · zon {ev['rmse_sunny']}°C"
              + (f"; gerailed: {', '.join(rails)}" if rails else ""))
        rots.append({"rotation": r, "held_idx": held_idx, "train_steps": steps,
                     "railed": rails, "eval": ev, "params": params})
    return {"arm": arm, "cfg": cfg, "rotations": rotations,
            "windows": [e.date().isoformat() for _, e in wins],
            "rot": rots, "summary": summarize(rots),
            "wall_s": round(time.time() - t0, 1)}


# ── Orchestratie + rapport ───────────────────────────────────────────────────────────

def arm_result_path(stamp: str, arm: str) -> str:
    return os.path.join(OUT_DIR, f"vent_experiment_{stamp}.{arm}.json")


def report_path(stamp: str) -> str:
    return os.path.join(OUT_DIR, f"vent_experiment_{stamp}.json")


def default_stamp(dataset: dict) -> str:
    """Deterministische rapport-stempel: het jongste shard-sample, niet de wandklok —
    dezelfde dataset geeft altijd dezelfde uitvoerbestanden."""
    ts_all = [t for s in dataset.get("actual", {}).values() for t, _ in s]
    if not ts_all:
        return "leeg"
    return max(ts_all).strftime("%Y%m%dT%H%M")


def run_all(arms: list[str], stamp: str, args) -> dict:
    """Elke arm als eigen proces (de term-patch is proces-lokaal), `--jobs` breed."""
    pending = list(arms)
    running: list[tuple[str, subprocess.Popen]] = []
    while pending or running:
        while pending and len(running) < max(1, args.jobs):
            arm = pending.pop(0)
            cmd = [sys.executable, os.path.abspath(__file__), "--arm", arm,
                   "--stamp", stamp, "--days", str(args.days),
                   "--step-h", str(args.step_h), "--rotations", str(args.rotations)]
            p = subprocess.Popen(cmd, cwd=_ROOT)
            running.append((arm, p))
            print(f"[all] gestart: {arm} (pid {p.pid}); {len(pending)} in wachtrij")
        time.sleep(5)
        for arm, p in running[:]:
            if p.poll() is not None:
                running.remove((arm, p))
                print(f"[all] klaar: {arm} (exit {p.returncode})")
    results = {}
    for arm in arms:
        path = arm_result_path(stamp, arm)
        if not os.path.exists(path):
            print(f"[all] WAARSCHUWING: geen resultaat voor {arm} ({path}) — "
                  f"uit het rapport gelaten.")
            continue
        with open(path, encoding="utf-8") as f:
            results[arm] = json.load(f)
    return results


def _cell(v, w: int = 7, nd: int = 3) -> str:
    return format(v, f">{w}.{nd}f") if isinstance(v, (int, float)) else format("—", f">{w}")


def _delta(summary: dict, base: dict | None, key: str, w: int = 7, nd: int = 3) -> str:
    if base is None or summary is base:
        return format(0.0, f">+{w}.{nd}f")
    a_v, b_v = summary.get(key), base.get(key)
    if a_v is None or b_v is None:
        return format("—", f">{w}")
    return format(a_v - b_v, f">+{w}.{nd}f")


def _noise(results: dict, key: str):
    aa = [results[a]["summary"] for a in ("aa_1", "aa_2") if a in results]
    if len(aa) != 2 or aa[0].get(key) is None or aa[1].get(key) is None:
        return None
    return round(abs(aa[0][key] - aa[1][key]), 4)


def render_report(results: dict, stamp: str, rotations: int, window_d: float) -> str:
    """Tabel arms × metrics (incl. rail-tellingen) + de A/A-ruisvloer per plak."""
    lines = []
    base = results.get("baseline", {}).get("summary")
    n5, n48 = _noise(results, "rmse_5d"), _noise(results, "rmse_48h")
    nsun, nmid = _noise(results, "rmse_sunny"), _noise(results, "rmse_midday")

    def _nf(v):
        return v if v is not None else "n.b."

    lines.append(f"\n== vent-experiment {stamp} — {rotations} rotaties × {window_d:g}d-"
                 f"vensters, elk venster precies één keer held-out ==")
    lines.append(f"   A/A-ruisvloer: 5d {_nf(n5)} · 48u {_nf(n48)} · zon {_nf(nsun)} · "
                 f"mid {_nf(nmid)} °C")
    lines.append("   winst telt pas boven de ruisvloer op 5d ÉN de zon-plak; primaire "
                 "uitkomst (§6.2) = ontspant het kern-floor-railen "
                 f"({'/'.join(KEY_FLOOR_PARAMS)})?")
    hdr = (f"{'arm':18} {'5d°C':>7} {'Δ5d':>8} {'48u°C':>7} {'Δ48u':>8} {'zon°C':>7} "
           f"{'Δzon':>8} {'mid°C':>7} {'Δmid':>8} {'kern-rail':>9} {'rails':>6}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for arm in sorted(results, key=lambda a: results[a]["summary"].get("rmse_5d") or 9e9):
        s = results[arm]["summary"]
        lines.append(
            f"{arm:18} {_cell(s.get('rmse_5d'))} {_delta(s, base, 'rmse_5d', 7)} "
            f"{_cell(s.get('rmse_48h'))} {_delta(s, base, 'rmse_48h', 7)} "
            f"{_cell(s.get('rmse_sunny'))} {_delta(s, base, 'rmse_sunny', 7)} "
            f"{_cell(s.get('rmse_midday'))} {_delta(s, base, 'rmse_midday', 7)} "
            f"{s.get('railed_key_floor', 0):>9} {s.get('railed_total', 0):>6}")
    for arm in sorted(results):
        u = results[arm]["summary"].get("railed_union") or []
        if u:
            lines.append(f"   {arm}: gerailed {', '.join(u)}")
    rooms = sorted({rid for r in results.values()
                    for rid in (r["summary"].get("per_room") or {})})
    if rooms:
        lines.append("\nper-kamer RMSE (vrijloop, °C):")
        lines.append(f"{'arm':18} " + " ".join(format(rid, '>9') for rid in rooms))
        for arm in sorted(results):
            pr = results[arm]["summary"].get("per_room") or {}
            lines.append(f"{arm:18} " + " ".join(_cell(pr.get(rid), 9) for rid in rooms))
    return "\n".join(lines)


def refresh_weather() -> None:
    """Ververs het shard-weer uit het Open-Meteo-archief over de dataset-spanwijdte.
    Netwerkfout → waarschuwing en dóór op het bestaande shard-weer (de sandbox/CI
    blokkeert de archief-host; de campagne is verder volledig offline)."""
    house = vio.load_house()
    dataset = vio.load_dataset(house)
    ts_all = [t for s in dataset["actual"].values() for t, _ in s]
    if not ts_all:
        print("[experiment] geen samples — weer-verversing overgeslagen.")
        return
    start = (min(ts_all) - timedelta(hours=vio.CALIB_WINDOW_H + vio.WARMUP_H)).date()
    end = max(ts_all).date()
    lat, lon = vio.house_location(house)
    try:
        rows = vio.fetch_weather_archive(lat, lon, start, end)
        n = vio.refresh_shard_weather(rows, overwrite=True)
        print(f"[experiment] shard-weer ververst: {n} nieuwe uur-rijen ({start} → {end}).")
    except Exception as e:  # noqa: BLE001 — best-effort; de campagne kan zonder
        print(f"[experiment] WAARSCHUWING: archief-verversing mislukt ({e}) — "
              f"campagne draait op het bestaande shard-weer.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Ventilatie-tweeling — held-out-experimentcampagne")
    ap.add_argument("--arm", choices=sorted(ARMS), help="één arm draaien (worker-modus)")
    ap.add_argument("--arms", default=None, help="komma-lijst van armen (default alle)")
    ap.add_argument("--days", type=float, default=WINDOW_D,
                    help=f"vensterlengte in dagen (default {WINDOW_D:g}; stride == venster)")
    ap.add_argument("--step-h", type=float, default=DEFAULT_STEP_H,
                    help=f"uren tussen virtuele nu's in de train-replay (default {DEFAULT_STEP_H:g})")
    ap.add_argument("--rotations", type=int, default=DEFAULT_ROTATIONS,
                    help=f"K-vouws-rotaties (default {DEFAULT_ROTATIONS})")
    ap.add_argument("--jobs", type=int, default=2, help="processen over armen (default 2)")
    ap.add_argument("--stamp", default=None,
                    help="rapport-stempel (default: jongste shard-sample — deterministisch)")
    ap.add_argument("--refresh-weather", action="store_true",
                    help="shard-weer eerst verversen uit het Open-Meteo-archief (netwerk; "
                         "faalt zacht naar het bestaande shard-weer)")
    args = ap.parse_args()

    if args.refresh_weather:
        refresh_weather()

    stamp = args.stamp
    if stamp is None:
        stamp = default_stamp(vio.load_dataset(vio.load_house()))
    os.makedirs(OUT_DIR, exist_ok=True)

    if args.arm:
        result = run_arm(args.arm, args.days, args.step_h, args.rotations)
        path = arm_result_path(stamp, args.arm)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1, default=str)
        print(f"[{args.arm}] → {path}")
        return 0

    arms = args.arms.split(",") if args.arms else list(ARMS)
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        ap.error(f"onbekende arm(en): {', '.join(unknown)}")
    results = run_all(arms, stamp, args)
    if not results:
        print("[experiment] geen enkel arm-resultaat — niets te rapporteren.")
        return 1
    report = render_report(results, stamp, args.rotations, args.days)
    print(report)
    out = {"stamp": stamp, "rotations": args.rotations, "window_d": args.days,
           "step_h": args.step_h, "arms": results}
    with open(report_path(stamp), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n[experiment] JSON → {report_path(stamp)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Train/holdout-experimentcampagne voor Ventilatie 2 (Project 12) — "train & set".

Toetst model-varianten (armen) wetenschappelijk op de volle gedolven historie:
niet-overlappende 5-daagse vensters (stride == venster — overlap lekt held-out-data
naar de training), warm-start vanaf het huidige batch-anker, een VAST epoch-budget
per arm (tijdbudget zou armen met verschillende sim-kosten confounden), en evaluatie
op held-out vensters: 5-daags vrijlopend (primair, pooled residuen) + re-seeded
48u-segmenten (co-primair — dít is wat het dashboard scoort) + de zon-/middag-plakken
(`rmse_sunny`/`rmse_midday` — de fout van beide tweelingen correleert sterk met de
instraling, dus een arm die alléén het etmaalgemiddelde verbetert lost niets op)
+ RH + per-kamer.

Armen configureren de variant-haakjes in airflow2_model (NEIGHBOR_TRANSFORM,
TD_SEED_MODE, BEDROOM_NIGHT_ROOMS, RH_RES_WEIGHT, REG_WEIGHT2_BY_PARAM) — élke arm
draait als eigen procés (de hergebruikte am-helpers muteren module-globalen zoals
am._NEIGHBOR_TEMP; threads zouden racen). Volledig offline: de shards bevatten het
weer. Deterministisch (geen RNG); de A/A-armen jitteren de startvector met een
alternerend ±2%-patroon — hun held-out-ΔRMSE is de ruisvloer van de campagne.

Gebruik:
    python tools/twin2_experiment.py --all --out /pad/naar/resultaten [--jobs 3]
    python tools/twin2_experiment.py --arm neighbor_damped_mid --out DIR [--epochs 3]
                                     [--holdout-offset 2]
    python tools/twin2_experiment.py --report --out DIR
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import airflow_model as am          # noqa: E402
import airflow2_model as a2         # noqa: E402

WINDOW_D = 5.0
STRIDE_D = 5.0            # == WINDOW_D: géén overlap → lek-vrije holdout
DEFAULT_EPOCHS = 3
LAM0 = 1e-3               # start-demping; ronde ≥2 zet dit hoog (--lam0) tegen train-overfit
DEFAULT_OFFSET = 2        # held-out indices {2, 5, 8}: mild juni / na-hittegolf / warm juli
SEG_OFFSETS_H = (0.0, 48.0)   # 48u-segmenten binnen elk held venster
SEG_WARMUP_H = 24.0
COVERAGE_FRAC = 0.5       # kamer <50% dekking in een held venster → uit diens scoring
# Zon-/middag-plakken van de held-out-residuen. De fout van beide tweelingen correleert
# sterk met de instraling (r = +0.49 over tweeling 1's volle leercurve, +0.77 over de
# laatste 48u), dus een arm die het gemiddelde verbetert maar de zonnige uren niet raakt
# lost het eigenlijke probleem niet op — deze plakken maken dat zichtbaar i.p.v. het in
# het etmaalgemiddelde te laten verdwijnen.
SUNNY_WM2 = 300.0         # globale horizontale instraling waarboven een sample "zonnig" heet
MIDDAY_H = (10, 16)       # lokale uren [start, eind) voor de middag-plak

# ── Armen ────────────────────────────────────────────────────────────────────────────
# Elke arm: hypothese in één regel; cfg = variant-haakjes + start-transformaties.
ARMS: dict[str, dict] = {
    # vergelijker: zelfde anker, zelfde epochs, zelfde train-vensters — géén variant.
    "baseline": {},
    # primair: zomer-anker te heet én verkeerd getimed aan de venster-rand.
    "neighbor_damped_mid": {"neighbor_transform": "damped", "neighbor_mode": "mid",
                            "reset_ua_party": True},
    # primair, alternatieve vorm: onderscheidt "niveau clipt" van "helling te steil".
    "neighbor_cap": {"neighbor_transform": "cap23", "reset_ua_party": True},
    # het hete anker vervuilt de diepe-massa-beginconditie (τ~dagen; 24u warmup spoelt niet).
    "td_seed_own": {"td_seed": "own"},
    # slaapkamers: interne warmte + vocht 's nachts (fase-, niet magnitude-fout).
    "bedroom_night_gain": {"bedrooms": ["ted", "hotties"]},
    # ablation: verdient de koker-split zich terug via de deur-gekoppelde kamers?
    "no_subzones": {"strip_subzones": True},
    # diepe-knoop-equilibratie: 48u i.p.v. 24u sim-only aanloop per venster.
    "warmup_48": {"warmup_h": 48.0},
    # misgespecificeerd vochtmodel vervormt de temp-fit → RH-gewicht halveren.
    "rh_weight_05": {"rh_weight": 0.05},
    # resterende per-param-ridges: vangnetten die met 7 weken batch-data bias zijn.
    "ridge_light": {"ridge": {}},
    # A/A-ruisvloer: baseline met alternerende ±2%-start-jitter (deterministisch).
    "aa_jitter_1": {"jitter": 0.02},
    "aa_jitter_2": {"jitter": -0.02},
    # Param-compatibele buur-varianten (géén ua_party-reset): meten het anker-effect
    # via de kamers die nú party-koppeling hebben (living/hotties) — voor de
    # 0-epoch-configtest, waar een reset juist ruis zou toevoegen.
    "neighbor_damped_nr": {"neighbor_transform": "damped", "neighbor_mode": "mid"},
    "neighbor_cap_nr": {"neighbor_transform": "cap23"},
    # nacht-verlaagd buur-anker: buren koelen 's nachts óók af; het vaste 23°C-plafond
    # "stookte" 's nachts door (de nachtelijke warm-bias, juli 2026). Param-compatibel
    # (geen reset) → geschikt voor de 0-epoch-configtest, net als de _nr-armen.
    "neighbor_cap_night": {"neighbor_transform": "cap23_night"},
    # ── Zon-invoer (augustus 2026) ───────────────────────────────────────────────────
    # Deze armen zijn NIET param-compatibel: ze veranderen de grootte van de zon-drive,
    # die het huidige anker in `solar_gain`/`ua_roof` heeft geabsorbeerd. Warm-starten
    # vanaf dat anker maakt ze vals-negatief, dus alle vier starten op kale priors — en
    # daarom is `baseline_bare` (en niet `baseline`) hun vergelijker.
    # eerlijke vergelijker voor de bare-priors-armen: kale priors, geen variant.
    "baseline_bare": {"bare_priors": True},
    # Open-Meteo's `direct_radiation` is horizontaal, niet loodrecht — de beam werd met
    # sin(zonshoogte) te laag gelezen (1.4× hoge zon, 2.5× lage zon).
    "solar_dni": {"bare_priors": True, "direct_is_horizontal": True},
    # de verduisteringsgordijnen zijn BINNENgordijnen: factor 0.12 is een verduisterings-
    # fractie, geen zonwarmte-fractie (de geabsorbeerde energie zit binnen het glas).
    "shade_internal": {"bare_priors": True, "shade_factor": {
        "ted_window": 0.7, "hotties_window": 0.7, "office_window": 0.7}},
    # samen: (1) verhoogt de instraling, (2) verlaagt het vermeden aandeel — ze werken
    # deels tegen elkaar in, dus de combinatie moet apart gemeten worden.
    "solar_dni_shade": {"bare_priors": True, "direct_is_horizontal": True,
                        "shade_factor": {"ted_window": 0.7, "hotties_window": 0.7,
                                         "office_window": 0.7}},
    # A/A-ruisvloer ín het bare-priors-regime (de warm-start-jitter-armen meten een andere
    # ruisvloer: daar zit de start op het anker, hier op de priors).
    "aa_bare_1": {"bare_priors": True, "jitter": 0.02},
    "aa_bare_2": {"bare_priors": True, "jitter": -0.02},
}

# Armen die op kale priors starten en dus tegen `baseline_bare` vergeleken moeten worden.
BARE_BASELINE = "baseline_bare"


def apply_config(cfg: dict) -> None:
    """Zet de variant-haakjes in airflow2_model (proces-lokaal). Afwezige sleutels
    laten de módule-defaults staan — "baseline" betekent dus altijd "de huidige
    productie-configuratie", ook nadat een campagne-winnaar default is geworden."""
    a2.NEIGHBOR_TRANSFORM = cfg.get("neighbor_transform", a2.NEIGHBOR_TRANSFORM)
    a2.TD_SEED_MODE = cfg.get("td_seed", a2.TD_SEED_MODE)
    a2.BEDROOM_NIGHT_ROOMS = set(cfg.get("bedrooms", a2.BEDROOM_NIGHT_ROOMS))
    if "rh_weight" in cfg:
        a2.RH_RES_WEIGHT = cfg["rh_weight"]
    if "ridge" in cfg:
        a2.REG_WEIGHT2_BY_PARAM = dict(cfg["ridge"])
    # Zon-conventie: leeft in airflow_model (gedeeld door béíde tweelingen), niet in a2.
    am.DIRECT_IS_HORIZONTAL = cfg.get("direct_is_horizontal", am.DIRECT_IS_HORIZONTAL)


def load_house_for(cfg: dict) -> dict:
    house = am.load_house()
    if cfg.get("strip_subzones"):
        for r in house.get("rooms", {}).values():
            r.pop("subzones", None)
    for wid, factor in (cfg.get("shade_factor") or {}).items():
        sh = house.get("windows", {}).get(wid, {}).get("shade")
        if isinstance(sh, dict) and "factor" in sh:      # alleen het simpele scherm-type
            sh["factor"] = float(factor)
    loc = house.get("location", {})
    am._LAT = loc.get("lat", am._LAT)
    am._LON = loc.get("lon", am._LON)
    return house


def start_params_for(house: dict, cfg: dict) -> dict:
    """Warm-start: het huidige batch-anker; buur-armen resetten ua_party naar prior
    (het anker zit op de 0-rail gefit onder de óude fysica — anders vals-negatief);
    A/A-armen jitteren de vector alternerend ±2% (deterministisch, geklemd).

    `bare_priors`: negeer het anker volledig en start op de kale priors. Nodig voor armen
    die de *drive* veranderen (de zon-conventie, de gordijnfactor): het anker heeft die
    fout in `solar_gain`/`ua_roof` geabsorbeerd, dus warm-starten daarvandaan meet de oude
    compensatie mee i.p.v. de variant. Zelfde keuze als bij de om_bias-adoptie."""
    if cfg.get("bare_priors"):
        start = a2.default_params2(house)
    else:
        start = a2.batch_start_params(house, a2.load_batch()) or a2.default_params2(house)
    if cfg.get("reset_ua_party"):
        for rp in start.values():
            if isinstance(rp, dict) and "ua_party" in rp:
                rp["ua_party"] = a2.PRIORS2["ua_party"]
    j = cfg.get("jitter")
    if j:
        rooms = [rid for rid in start if isinstance(start.get(rid), dict)]
        keys = a2._param_keys2(sorted(rooms))
        vec = a2.params_to_vec2(start, keys)
        vec = [v * (1.0 + j * (1 if i % 2 == 0 else -1)) for i, v in enumerate(vec)]
        start = a2.vec_to_params2(a2._clamp_to_bounds2(vec, keys), keys, start)
    return start


def _room_ok(samples: list, window_d: float) -> bool:
    return len(samples) >= COVERAGE_FRAC * window_d * 24 * 4


def _solar_series(timeline: list[dict]) -> list[tuple]:
    """(t, globale horizontale instraling W/m²) per tijdlijnstap — `direct` staat bij
    Open-Meteo al op het horizontale vlak, dus direct + diffuus ≈ GHI. Voedt de
    zon-plak van de held-out-evaluatie."""
    return [(s["t"], (s["weather"].get("direct") or 0.0) + (s["weather"].get("diffuse") or 0.0))
            for s in timeline]


def evaluate(house: dict, wins_held: list[dict], params: dict) -> dict:
    """Held-out-evaluatie: 5d-vrijloop (pooled residuen) + 48u-segmenten + de zon-/middag-
    plakken + per-kamer/per-venster. Kamers onder de dekkingsdrempel vallen uit de scoring
    van dat venster."""
    rt_all, rr_all, rt_48 = [], [], []
    rt_sunny, rt_midday = [], []
    per_window, per_room = {}, {}
    for w in wins_held:
        # Zelfde venster-ankers (buur + bodem) als de fit gebruikte — anders zou de
        # evaluatie op een andere randvoorwaarde draaien dan de training.
        with a2.window_anchors(w):
            solar = _solar_series(w["timeline"])
            act = {rid: s for rid, s in w["actual"].items()
                   if _room_ok(s, (w["end"] - w["start"]).total_seconds() / 86400.0)}
            rh = {rid: s for rid, s in w["rh"].items() if rid in act}
            sim = a2.simulate2(house, params, w["timeline"], w["seed"],
                               calib_only_rooms=set(act) | set(rh),
                               seed_w=w["seed_w"], tm_seed=w.get("tm_seed"))
            w_res = []
            for rid, samples in act.items():
                pred = sim["series"].get(rid, [])
                if not pred:
                    continue
                pred = am._to_sensor_series(house, w["timeline"], rid, pred)
                res = [am._interp(pred, ts) - val for ts, val in samples]
                w_res += res
                per_room.setdefault(rid, []).extend(res)
                for (ts, _), r in zip(samples, res):
                    if am._interp(solar, ts) >= SUNNY_WM2:
                        rt_sunny.append(r)
                    if MIDDAY_H[0] <= ts.hour < MIDDAY_H[1]:
                        rt_midday.append(r)
            for rid, samples in rh.items():
                pred = sim["series_rh"].get(rid, [])
                if pred:
                    rr_all += [am._interp(pred, ts) - val for ts, val in samples]
            rt_all += w_res
            per_window[w["end"].date().isoformat()] = {
                "rmse": round(am.rmse(w_res), 3) if w_res else None, "n": len(w_res)}
            # 48u-segmenten: re-seeded op offsets binnen het venster (dashboard-protocol:
            # ~48u-vrijloop na een korte aanloop, seed = eerste actual in het segment).
            for off_h in SEG_OFFSETS_H:
                seg_start = w["start"] + timedelta(hours=off_h)
                seg_end = seg_start + timedelta(hours=48.0)
                if seg_end > w["end"]:
                    continue
                warm_start = seg_start - timedelta(hours=SEG_WARMUP_H)
                tl_seg = [s for s in w["timeline"] if warm_start <= s["t"] <= seg_end]
                if not tl_seg:
                    continue
                seed_seg = {}
                for rid, samples in act.items():
                    inseg = [v for ts, v in samples if seg_start <= ts <= seg_end]
                    if inseg:
                        seed_seg[rid] = inseg[0]
                for rid in house.get("rooms", {}):
                    seed_seg.setdefault(rid, tl_seg[0]["T_out"])
                tm_seg = dict(seed_seg) if a2.TD_SEED_MODE == "own" else None
                sim_seg = a2.simulate2(house, params, tl_seg, seed_seg,
                                       calib_only_rooms=set(act), tm_seed=tm_seg)
                for rid, samples in act.items():
                    pred = sim_seg["series"].get(rid, [])
                    if not pred:
                        continue
                    pred = am._to_sensor_series(house, tl_seg, rid, pred)
                    rt_48 += [am._interp(pred, ts) - val for ts, val in samples
                              if seg_start <= ts <= seg_end]
    return {
        "rmse_5d": round(am.rmse(rt_all), 4) if rt_all else None,
        "rmse_rh_5d": round(am.rmse(rr_all), 2) if rr_all else None,
        "rmse_48h": round(am.rmse(rt_48), 4) if rt_48 else None,
        "rmse_sunny": round(am.rmse(rt_sunny), 4) if rt_sunny else None,
        "rmse_midday": round(am.rmse(rt_midday), 4) if rt_midday else None,
        "n_5d": len(rt_all), "n_48h": len(rt_48),
        "n_sunny": len(rt_sunny), "n_midday": len(rt_midday),
        "per_window": per_window,
        "per_room": {rid: round(am.rmse(res), 3) for rid, res in sorted(per_room.items())},
    }


def run_arm(arm: str, out_dir: str, epochs: int, offset: int) -> None:
    cfg = ARMS[arm]
    t0 = time.time()
    apply_config(cfg)
    house = load_house_for(cfg)
    dataset = a2.load_dataset(house)
    wins = a2.prepare_windows(house, dataset, window_d=WINDOW_D, stride_d=STRIDE_D,
                              warmup_h=cfg.get("warmup_h"),
                              neighbor_mode=cfg.get("neighbor_mode", "end"))
    held_idx = sorted(i for i in range(len(wins)) if i % 3 == offset)
    train = [w for i, w in enumerate(wins) if i not in set(held_idx)]
    held = [wins[i] for i in held_idx]
    print(f"[{arm}] {len(wins)} vensters ({[w['end'].date().isoformat() for w in wins]}); "
          f"held: {held_idx}")
    start = start_params_for(house, cfg)
    eval0 = evaluate(house, held, start)     # pure-config-effect, vóór de refit
    params, stats = a2.batch_fit(house, train, max_epochs=epochs, start_params=start,
                                 lam0=LAM0)
    ev = evaluate(house, held, params)
    result = {"arm": arm, "cfg": cfg, "epochs_budget": epochs, "holdout_offset": offset,
              "held_idx": held_idx, "n_train": len(train),
              "fit": {k: stats[k] for k in ("epochs", "accepted", "converged",
                                            "rmse_batch", "rmse_rh_batch", "samples")},
              "eval0": eval0, "eval": ev,
              "railed": a2.railed_params2(params), "params": params,
              "wall_s": round(time.time() - t0, 1)}
    os.makedirs(out_dir, exist_ok=True)
    path = result_path(out_dir, arm, offset)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1, default=str)
    print(f"[{arm}] klaar in {result['wall_s']}s → 5d {ev['rmse_5d']}°C · "
          f"zon {ev['rmse_sunny']}°C · mid {ev['rmse_midday']}°C · "
          f"48u {ev['rmse_48h']}°C · RH {ev['rmse_rh_5d']}% → {path}")


def result_path(out_dir: str, arm: str, offset: int) -> str:
    return os.path.join(out_dir, f"{arm}.off{offset}.json")


def run_all(out_dir: str, epochs: int, offset: int, jobs: int, arms: list[str],
            force: bool = False) -> None:
    """Orchestrator: elke arm als eigen proces (module-globalen!), `jobs`-breed.

    Hervatbaar: een arm met een bestaand resultaatbestand wordt overgeslagen (`--force`
    om tóch over te doen). Een campagne-arm kost uren, en de omgeving kan midden in een
    golf herstarten — zonder deze poort kost elke herstart de hele golf opnieuw, terwijl
    de al afgeronde armen gewoon op schijf staan."""
    pending = []
    for arm in arms:
        if not force and os.path.exists(result_path(out_dir, arm, offset)):
            print(f"[all] overgeslagen (bestaat al): {arm}.off{offset}")
            continue
        pending.append(arm)
    if not pending:
        print(f"[all] niets te doen voor offset {offset} — alle armen staan er al.")
        return
    running: list[tuple[str, subprocess.Popen]] = []
    while pending or running:
        while pending and len(running) < jobs:
            arm = pending.pop(0)
            p = subprocess.Popen([sys.executable, os.path.abspath(__file__),
                                  "--arm", arm, "--out", out_dir,
                                  "--epochs", str(epochs), "--holdout-offset", str(offset),
                                  "--lam0", str(LAM0)],
                                 cwd=_ROOT)
            running.append((arm, p))
            print(f"[all] gestart: {arm} (pid {p.pid}); {len(pending)} in wachtrij")
        time.sleep(10)
        for arm, p in running[:]:
            if p.poll() is not None:
                running.remove((arm, p))
                print(f"[all] klaar: {arm} (exit {p.returncode})")
    print("[all] alle armen klaar.")


def report(out_dir: str, offset: int) -> None:
    rows = []
    for name in ARMS:
        path = os.path.join(out_dir, f"{name}.off{offset}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            rows.append(json.load(f))
    # Vergelijker + A/A-paar volgen het regime van de campagne: bare-priors-armen horen
    # tegen `baseline_bare` en de `aa_bare_*`-ruisvloer, want een warm-start vanaf het anker
    # is voor hen geen faire tegenhanger (zie `start_params_for`).
    bare = any(r["arm"] == BARE_BASELINE for r in rows)
    base = next((r for r in rows if r["arm"] == (BARE_BASELINE if bare else "baseline")), None)
    aa_prefix = "aa_bare" if bare else "aa_jitter"
    aa = [r for r in rows if r["arm"].startswith(aa_prefix)]

    def _noise(key: str):
        if len(aa) != 2 or aa[0]["eval"].get(key) is None or aa[1]["eval"].get(key) is None:
            return None
        return round(abs(aa[0]["eval"][key] - aa[1]["eval"][key]), 4)

    def _cell(v, w=7, nd=3):
        return format(v, f">{w}.{nd}f") if isinstance(v, (int, float)) else format("—", f">{w}")

    def _delta(r, b, key, w=7, nd=3):
        if not base or r is base:
            return format(0.0, f">+{w}.{nd}f")
        a_v, b_v = r["eval"].get(key), b.get(key)
        if a_v is None or b_v is None:
            return format("—", f">{w}")
        return format(a_v - b_v, f">+{w}.{nd}f")

    n5, nsun, nmid = _noise("rmse_5d"), _noise("rmse_sunny"), _noise("rmse_midday")
    print(f"\n== screening (holdout-offset {offset}) — A/A-ruisvloer: "
          f"5d {n5 if n5 is not None else 'n.b.'} · zon {nsun if nsun is not None else 'n.b.'} · "
          f"mid {nmid if nmid is not None else 'n.b.'} °C ==")
    print("   een arm telt pas als winst als hij de ruisvloer op 5d ÉN op de zon-plak "
          "overtreft en geen enkel held venster verslechtert.")
    hdr = (f"{'arm':20} {'5d°C':>7} {'Δ5d':>7} {'zon°C':>7} {'Δzon':>7} {'mid°C':>7} "
           f"{'Δmid':>7} {'48u°C':>7} {'Δ48u':>7} {'RH%':>6} {'ΔRH':>6} {'ergste-vnst':>11}")
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(rows, key=lambda r: r["eval"]["rmse_5d"] or 9e9):
        ev, b = r["eval"], (base or {}).get("eval", {})
        worst = None
        if base and r is not base:
            worst = max((ev["per_window"][k]["rmse"] or 0) - (b["per_window"].get(k, {}).get("rmse") or 0)
                        for k in ev["per_window"])
        worst_s = "n.b." if worst is None else format(worst, "+.3f")
        gap = (r["fit"]["rmse_batch"] - ev["rmse_5d"]) if r["fit"]["rmse_batch"] else None
        gap_s = "n.b." if gap is None else format(-gap, "+.3f")
        print(f"{r['arm']:20} {_cell(ev['rmse_5d'])} {_delta(r, b, 'rmse_5d')} "
              f"{_cell(ev.get('rmse_sunny'))} {_delta(r, b, 'rmse_sunny')} "
              f"{_cell(ev.get('rmse_midday'))} {_delta(r, b, 'rmse_midday')} "
              f"{_cell(ev['rmse_48h'])} {_delta(r, b, 'rmse_48h')} "
              f"{_cell(ev['rmse_rh_5d'], 6, 2)} {_delta(r, b, 'rmse_rh_5d', 6, 2)} "
              f"{worst_s:>11} acc={r['fit'].get('accepted', '?')} overfit-gap={gap_s}")
    if base:
        ev = base["eval"]
        print(f"\nzon-plak dekking: {ev.get('n_sunny', 0)} van {ev.get('n_5d', 0)} residuen "
              f"(> {SUNNY_WM2:.0f} W/m²); middag-plak {ev.get('n_midday', 0)} "
              f"({MIDDAY_H[0]:02d}–{MIDDAY_H[1]:02d}u lokaal).")


def main() -> int:
    ap = argparse.ArgumentParser(description="Ventilatie 2 — experimentcampagne")
    ap.add_argument("--arm", choices=sorted(ARMS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    ap.add_argument("--holdout-offset", type=int, default=DEFAULT_OFFSET)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--lam0", type=float, default=None,
                    help="start-demping voor de fits (default 1e-3; ronde ≥2: ~1.0)")
    ap.add_argument("--arms", default=None,
                    help="komma-lijst (voor --all), default alle ARMS")
    ap.add_argument("--force", action="store_true",
                    help="(voor --all) armen met een bestaand resultaat tóch opnieuw draaien")
    args = ap.parse_args()
    global LAM0
    if args.lam0 is not None:
        LAM0 = args.lam0
    if args.report:
        report(args.out, args.holdout_offset)
    elif args.all:
        arms = args.arms.split(",") if args.arms else list(ARMS)
        run_all(args.out, args.epochs, args.holdout_offset, args.jobs, arms, args.force)
    elif args.arm:
        run_arm(args.arm, args.out, args.epochs, args.holdout_offset)
    else:
        ap.error("kies --arm, --all of --report")
    return 0


if __name__ == "__main__":
    sys.exit(main())

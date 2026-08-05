#!/usr/bin/env python3
"""
vent_seed.py — parameter-seeding van de ventilatie-tweeling uit de maand-shards.

Speelt de laatste ~SEED_DAYS dagen shard-historie af door de PRODUCTIE-onlinefit:
een virtuele "nu" schuift in stappen van STEP_H uur vooruit, en op elke stap draait
exact wat de kwartierrunner draait — build_timeline (48u venster + 24u warmup, met
de geleerde om_bias-driver), de AC/verwarmings/pauze-filters en vent_fit.calibrate,
LEARN_RATE-nudgend vanaf de vorige stap. ~20 online-runs samengeperst in één
offline pass; deterministisch, géén nieuwe fit-machinerie.

Waarom: een verse deploy (of een PHYSICS_REV-bump) start dan met ingeleerde params
in docs/vent_learned.json i.p.v. live vanaf de priors te moeten leren — precies de
reset-schok die bij de voorgangers de anomalie-poort liet dichtslaan. Draai dit
hulpje dus óók bij elke toekomstige revisie-bump, vóór de merge.

Acceptatiepoort (het herbouwplan §8.2): eind-RMSE ≤ MAX_SEED_RMSE én geen gerailde
params — anders exit 1 en wordt er niets geschreven (een falende seed is per
definitie een port-/fysicafout, geen reden om 'm te verzwijgen).

Gebruik:
  python tools/vent_seed.py [--days 5] [--step-h 6] [--no-refresh] [--dry-run]

--no-refresh slaat de shard-weer-verversing (Open-Meteo-archief, netwerk) over;
zonder verversing draait de replay op het forecast-grade weer dat de kwartierloop
appendde. Schrijft docs/vent_learned.json (VENT_LEARNED_PATH) met een additieve
`seed_src`-stempel; rmse_history blijft leeg — de leercurve toont alléén echte
productieruns.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vent_fit as vf                    # noqa: E402
import vent_io as vio                    # noqa: E402
from shared_const import TZ              # noqa: E402
from vent_physics import PER_ROOM_PARAMS, GLOBAL_PARAMS  # noqa: E402

MAX_SEED_RMSE = 0.9   # °C — acceptatiepoort op de laatste replay-stap


def _slice_actual(actual: dict, since: datetime, until: datetime) -> dict:
    out = {}
    for rid, samples in actual.items():
        s = [(t, v) for t, v in samples if since <= t <= until]
        if s:
            out[rid] = s
    return out


def _weather_view(rows: list[dict], until: datetime) -> dict:
    """Shard-weer in fetch_weather-vorm, afgekapt op de virtuele nu (geen toekomstkennis)."""
    return {"hourly": [r for r in rows if r["dt"] <= until], "current": {}}


def replay(house: dict, dataset: dict, days: float, step_h: float,
           om_learned: dict | None, params: dict,
           eval_only: bool = False) -> tuple[dict, list[dict]]:
    """De eigenlijke replay. Geeft (eindparams, stap-log) terug."""
    all_ts = [t for s in dataset["actual"].values() for t, _ in s]
    if not all_ts:
        raise SystemExit("[seed] geen kamersamples in de shards — eerst backfillen?")
    t_end = max(all_ts)
    n_steps = max(1, int(round(days * 24.0 / step_h)))
    vnows = [t_end - timedelta(hours=step_h * (n_steps - 1 - i)) for i in range(n_steps)]
    log = dataset["log"]
    window_h = vio.CALIB_WINDOW_H + vio.WARMUP_H

    steps: list[dict] = []
    for vnow in vnows:
        weather = _weather_view(dataset["weather_rows"], vnow)
        rows = weather["hourly"]
        if not rows or (vnow - rows[0]["dt"]).total_seconds() / 3600.0 < window_h:
            print(f"[seed] {vnow:%m-%d %H:%M} — te weinig weer-historie, stap overgeslagen")
            continue
        ctx = vio.make_context(house, weather, vnow)
        since = vnow - vio._timedelta_h(vio.CALIB_WINDOW_H)
        actual = _slice_actual(dataset["actual"], since, vnow)
        if not actual:
            print(f"[seed] {vnow:%m-%d %H:%M} — geen samples in het venster, stap overgeslagen")
            continue
        seed = {rid: s[0][1] for rid, s in actual.items()}
        tm_seed = {rid: sum(v for _, v in s) / len(s) for rid, s in actual.items()}
        gamma_measured = {rid: list(s) for rid, s in actual.items()}

        # Dezelfde hygiëne-filters als de runner (uitgesloten kamers / AC / verwarming / pauze).
        # De structurele uitsluiting hoort hier expliciet bij: seedt de tool op een ándere
        # verzameling kamers dan de productie-fit, dan keurt de acceptatiepoort (RMSE ≤ 0.9)
        # een getal dat de runner nooit rapporteert.
        actual, _ = vf.filter_excluded_rooms(actual, house)
        acc = vio.ac_changes(log)
        actual, _ = vf.filter_ac_samples(actual, acc, vio.ac_room_at(acc, vnow), vnow)
        actual, _ = vf.filter_heating_samples(actual, dataset.get("heat_on", {}))
        pch = vio.pause_changes(log)
        actual, _ = vf.filter_paused_samples(actual, vio.paused_intervals(pch, vnow))
        if not actual:
            print(f"[seed] {vnow:%m-%d %H:%M} — alles weggefilterd (uitgesloten/AC/stook/pauze), "
                  "overgeslagen")
            continue

        timeline = vio.build_timeline(house, weather, log, vnow, window_h, ctx,
                                      beam_iam=True, end_h=0.0, om_learned=om_learned)
        if not timeline:
            continue
        for rid in house.get("rooms", {}):
            seed.setdefault(rid, timeline[0]["T_out"])
        solar = [r["shortwave"] for r in rows[-int(vio.CALIB_WINDOW_H):]
                 if r.get("shortwave") is not None]
        solar_mean = round(sum(solar) / len(solar)) if solar else None

        if eval_only:
            step_rmse = vf.rmse(vf._residuals(house, params, timeline, seed, ctx, actual,
                                              set(actual.keys()),
                                              tm_seed=tm_seed, measured=gamma_measured))
        else:
            params, step_rmse = vf.calibrate(house, params, timeline, seed, ctx, actual,
                                             solar_mean=solar_mean,
                                             tm_seed=tm_seed, measured=gamma_measured)
        rails = vf.railed_params(params, house=house)
        n = sum(len(s) for s in actual.values())
        steps.append({"t": vnow.isoformat(), "rmse": round(step_rmse, 3),
                      "samples": n, "railed": rails})
        print(f"[seed] {vnow:%m-%d %H:%M} — RMSE {step_rmse:.3f} °C over {n} samples"
              + (f"; gerailed: {', '.join(rails)}" if rails else ""))
    return params, steps


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--days", type=float, default=5.0, help="replay-venster (dagen, default 5)")
    ap.add_argument("--step-h", type=float, default=6.0, help="stapgrootte (uren, default 6)")
    ap.add_argument("--no-refresh", action="store_true",
                    help="shard-weer-verversing (archief, netwerk) overslaan")
    ap.add_argument("--eval-only", action="store_true",
                    help="niet fitten, alleen de priors op het laatste venster scoren")
    ap.add_argument("--dry-run", action="store_true", help="niets schrijven")
    args = ap.parse_args()

    house = vio.load_house()
    lat, lon = vio.house_location(house)

    if not args.no_refresh:
        # Verversing op verbruiksmoment (herbouwplan U7): archief-kwaliteit weer voor
        # precies het venster dat we gaan gebruiken — de rol van de gepensioneerde
        # wekelijkse batch, zonder cron.
        end = datetime.now(TZ).date()
        start = end - timedelta(days=int(args.days) + 5)
        try:
            rows = vio.fetch_weather_archive(lat, lon, start, end)
            n = vio.refresh_shard_weather(rows, overwrite=True)
            print(f"[seed] shard-weer ververst uit het archief: {n} uur-rijen bijgewerkt")
        except Exception as e:  # noqa: BLE001 — verversing is best-effort, replay kan door
            print(f"[seed] archief-verversing mislukt ({e}) — replay op bestaand shard-weer")

    dataset = vio.load_dataset(house)
    om_learned = vio.om_learned_from(vio.load_window_data())
    if om_learned:
        print(f"[seed] om_bias-driver: nacht {om_learned.get('night'):+.2f}°C, "
              f"dag {om_learned.get('day'):+.2f}°C")

    p0 = vio.default_params(house)
    params, steps = replay(house, dataset, args.days if not args.eval_only else 0.1,
                           args.step_h, om_learned, p0, eval_only=args.eval_only)
    if not steps:
        raise SystemExit("[seed] geen enkele replay-stap gelukt — shards te dun?")

    final = steps[-1]
    # De poort kijkt alleen naar échte BOUNDS-saturatie. Een parameter die op een BEWUSTE
    # huismodel-grens ligt (`param_bounds`, achtervoegsel "(model)") is geen klacht maar de
    # constraint die doet wat hij moet doen — `living.c_mass` hoort daar te liggen, want de
    # nowcast-doelfunctie waar de fit op draait wíl daar juist onderdoor (zie house_model.json).
    hard_rails = [r for r in final["railed"] if not vf.is_model_rail(r)]
    model_rails = [r for r in final["railed"] if vf.is_model_rail(r)]
    ok = final["rmse"] <= MAX_SEED_RMSE and not hard_rails
    print(f"[seed] eind-RMSE {final['rmse']:.3f} °C (poort ≤ {MAX_SEED_RMSE}), "
          f"gerailed: {hard_rails or 'geen'}"
          + (f" (+ huismodel-grens: {', '.join(model_rails)})" if model_rails else "")
          + f" → {'GESLAAGD' if ok else 'AFGEKEURD'}")
    if not ok:
        raise SystemExit(1)

    # Sanity: het volledige parameter-oppervlak aanwezig (merged_params-vorm).
    assert all(g in params for g in GLOBAL_PARAMS)
    assert all(k in params[rid] for rid in house.get("rooms", {}) for k in PER_ROOM_PARAMS)

    span = f"{steps[0]['t'][:10]}..{final['t'][:10]}"
    out = {"updated_at": datetime.now(TZ).isoformat(),
           "model_version": vio.model_version(),
           "physics_rev": vio.PHYSICS_REV,
           "params": params,
           "rmse": final["rmse"],
           "skill": None,
           "railed": model_rails,
           "rmse_history": [],     # leercurve toont alléén echte productieruns
           "anomaly": {},
           "seed_src": f"shard-replay {span}" + (" (eval-only)" if args.eval_only else "")}
    if args.dry_run:
        print("[seed] DRY-RUN — niets geschreven.")
        return
    with open(vio.LEARNED_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[seed] geschreven: {vio.LEARNED_FILE} (seed_src: {out['seed_src']})")


if __name__ == "__main__":
    main()

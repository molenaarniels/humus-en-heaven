#!/usr/bin/env python3
"""Meet de lucht-proxy voor bodemtemperatuur tegen meerdere schouderseizoenen.

`soil_model.temp_factor` draait op een 5-daags loopgemiddelde van de lucht-Tmean
als stand-in voor de bodemtemperatuur. Die keuze is nooit getoetst. Het
ERA5-archief draagt bodemtemperatuur jaren terug, dus dat kan nu — en met
meerdere winters tegelijk in plaats van wachten op de volgende.

    python tools/soil_temp_probe.py [--years 4] [--depth root|shallow]

Leest alleen Open-Meteo (geen key, geen secret) en schrijft niets. Zie
`soil_temp_proxy.py` voor waaróm de seizoensgrens-verschuiving de hoofdmaat is
en niet de RMSE.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import soil_temp_proxy as stp  # noqa: E402
from shared_const import local_today  # noqa: E402
from soil_model import SOIL_TEMP_WINDOW, fetch_open_meteo_archive  # noqa: E402

WINDOWS = (1, 2, 3, 5, 7, 10, 14, 21)
DEPTH_FIELD = {"shallow": "Tsoil_shallow", "root": "Tsoil_root"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=4)
    ap.add_argument("--depth", choices=sorted(DEPTH_FIELD), default="root",
                    help="root = 7–28 cm (graswortelzone), shallow = 0–7 cm")
    args = ap.parse_args()

    end = local_today() - timedelta(days=7)          # ERA5 loopt ~5 dagen achter
    start = end.replace(year=end.year - args.years)
    print(f"[probe] ERA5-archief {start} t/m {end} ({args.years} jaar), "
          f"diepte {args.depth} ({DEPTH_FIELD[args.depth]})")

    rows = fetch_open_meteo_archive(start.isoformat(), end.isoformat())
    field = DEPTH_FIELD[args.depth]
    dates = [r["date"] for r in rows]
    air = [r.get("Tmean") for r in rows]
    soil = [r.get(field) for r in rows]
    have = sum(1 for s in soil if s is not None)
    print(f"[probe] {len(rows)} dagen, {have} met bodemtemperatuur")
    if have < 365:
        print("[probe] LET OP: minder dan een jaar bodemtemperatuur — het archief "
              "levert dit veld mogelijk niet zo ver terug.")
    if not have:
        return

    results = stp.evaluate_windows(dates, air, soil, WINDOWS)

    print(f"\n{'venster':>8s} {'rmse':>7s} {'bias':>7s} {'corr':>6s} "
          f"{'dagen oneens':>13s} {'med |versch.|':>14s} {'gem':>7s}")
    for r in results:
        f, d = r["fit"], r["disagreement"]
        mark = "  ← huidig" if r["window"] == SOIL_TEMP_WINDOW else ""
        med = "—" if r["median_abs_shift"] is None else f"{r['median_abs_shift']:.1f} d"
        gem = "—" if r["mean_abs_shift"] is None else f"{r['mean_abs_shift']:.1f}"
        print(f"{r['window']:6d} d {f['rmse']:7.2f} {f['bias']:+7.2f} "
              f"{f['corr']:6.3f} {d['n_disagree']:9d}/{d['n']:<4d} {med:>14s} {gem:>7s}{mark}")

    huidig = next(r for r in results if r["window"] == SOIL_TEMP_WINDOW)
    print(f"\nSeizoensgrenzen bij het huidige venster van {SOIL_TEMP_WINDOW} dagen "
          f"(drempel {stp.TEMP_FACTOR_FULL_C:.0f} °C, proxy − bodem):")
    print(f"  {'jaar':>6s} {'seizoen':>8s} {'proxy':>12s} {'bodem':>12s} {'verschil':>9s}")
    for s in huidig["shifts"]:
        print(f"  {s['year']:6d} {s['season']:>8s} {str(s['proxy']):>12s} "
              f"{str(s['truth']):>12s} {s['shift_days']:+8d} d")

    # Niet-monotonie over de vensters is het teken dat de verschuivingsmaat door
    # losse uitbijters wordt gedreven i.p.v. door een echt optimum; dan is elke
    # "winnaar" ruis. Zie de noot bij `best_window`.
    meds = [r["median_abs_shift"] for r in results if r["median_abs_shift"] is not None]
    gems = [r["mean_abs_shift"] for r in results if r["mean_abs_shift"] is not None]
    def _wisselingen(xs):
        richting = [b - a for a, b in zip(xs, xs[1:])]
        return sum(1 for a, b in zip(richting, richting[1:]) if a * b < 0)

    beste = stp.best_window(results)
    if beste:
        venster, shift = beste
        huidig_med = huidig["median_abs_shift"]
        print(f"\n[uitkomst] kleinste MEDIANE verschuiving: {venster} dagen ({shift:.1f} d) — "
              f"huidig {SOIL_TEMP_WINDOW} dagen geeft {huidig_med:.1f} d")
        print(f"           richtingswisselingen over de vensters: mediaan {_wisselingen(meds)}, "
              f"gemiddelde {_wisselingen(gems)} (veel = uitbijter-gedreven, geen optimum)")
        if venster == SOIL_TEMP_WINDOW or huidig_med - shift < 1.0:
            print("           Geen reden om het venster te wijzigen: het verschil met de "
                  "bestaande keuze\n           is kleiner dan een dag op een grens die zelf "
                  "op dagen nauwkeurig is.")
        else:
            print(f"           Kandidaat-winst: {huidig_med - shift:.1f} dag op de mediaan. "
                  "Toets dit op meer jaren\n           vóór je temp_factor aanraakt — het werkt "
                  "door in Project 5.")
    print("\nLet op: ERA5-bodemtemperatuur is zelf een model in een standaard-"
          "bodemkolom,\nniet deze klei-verrijkte zandgrond. Dit weegt de tráágheid van de "
          "proxy,\nniet de absolute waarde — een uitkomst hoort in een vensterlengte te "
          "landen,\nniet in 'vervang de driver'.")


if __name__ == "__main__":
    main()

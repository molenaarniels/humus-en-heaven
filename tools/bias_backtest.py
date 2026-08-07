#!/usr/bin/env python3
"""Weeg kandidaat-modelvormen voor de stralingsbiascorrectie tegen de uitgerolde
constante — out-of-sample, met ruisvloer en een vooraf vastgelegde acceptatieregel.

Dit is de maat waar een wijziging aan `wu_bias.SOLAR_BIAS_SLOPE` (of aan de
modelvórm eromheen) op beoordeeld hoort te worden. De redenering staat in
`bias_eval.py`; dit script is alleen de runner en het rapport.

    python tools/bias_backtest.py                        # ERA5-referentie, grid-driver
    python tools/bias_backtest.py --driver wu_solar      # de as waarop productie draait
    python tools/bias_backtest.py --reference knmi       # zodra de KNMI-kolom er is
    python tools/bias_backtest.py --daytime              # alleen uren met zon

Leest uitsluitend `data/station_history/` en schrijft niets.
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bias_eval as be  # noqa: E402
from station_accuracy import load_archive  # noqa: E402


def _weighted(folds) -> float | None:
    n = sum(f[2] for f in folds)
    if not n:
        return None
    return math.sqrt(sum(f[1] ** 2 * f[2] for f in folds) / n)


def _fmt(value, spec=".3f") -> str:
    return "—" if value is None else format(value, spec)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", default="om",
                    help="kolom waartegen de bias gemeten wordt (om = ERA5, knmi)")
    ap.add_argument("--driver", default="solar",
                    help="instralingskolom (solar = Open-Meteo-grid, wu_solar = eigen pyranometer)")
    ap.add_argument("--daytime", action="store_true", help="alleen uren met zon")
    ap.add_argument("--k", type=int, default=6, help="aantal folds in de dag-geblokte CV")
    args = ap.parse_args()

    archive = load_archive()
    rows = be.prepare(archive, reference=args.reference, driver=args.driver)
    if args.daytime:
        rows = be.daytime(rows)
    if not rows:
        print(f"[backtest] geen bruikbare rijen voor reference={args.reference} "
              f"driver={args.driver} — draait het archief al met die kolom?")
        return

    days = sorted({r["day"] for r in rows})
    months = sorted({r["month"] for r in rows})
    print(f"[backtest] referentie={args.reference} driver={args.driver} "
          f"{'daguren' if args.daytime else 'alle uren'}")
    print(f"[backtest] {len(rows)} uren over {len(days)} dagen, {len(months)} maanden "
          f"({months[0]} t/m {months[-1]}) uit {len(archive)} gearchiveerde uren\n")

    results = be.evaluate(rows, k=args.k)
    print(f"{'model':28s} {'n':>5s} {'CV':>7s} {'skill':>7s} {'A/A':>6s} {'forward':>8s}  oordeel")
    for res in results:
        fwd = _weighted(res["forward"])
        p, n_days, past = res["budget"]
        verdict, reasons = res["oordeel"]
        if verdict is None:
            mark = "lat" if res["naam"] == be.DEPLOYED else "—"
        else:
            mark = "AANGENOMEN" if verdict else "verworpen"
        budget_flag = "" if past else f"  ⚠ {p} params op {n_days} dagen"
        print(f"{res['naam']:28s} {res['n']:5d} {_fmt(res['cv']):>7s} "
              f"{_fmt(res['skill_nul'], '+.3f'):>7s} {_fmt(res['aa'], '.3f'):>6s} "
              f"{_fmt(fwd):>8s}  {mark}{budget_flag}")
        for reason in reasons:
            print(f"{'':28s} {'':5s} {'':7s} {'':7s} {'':6s} {'':8s}    · {reason}")

    if len(months) < 3:
        print("\n[backtest] LET OP: minder dan 3 maanden archief — forward-chaining heeft "
              "dan hooguit één of twee folds en de acceptatieregel is navenant zwak.")

    lat = next((r for r in results if r["naam"] == be.DEPLOYED), None)
    nul = next((r for r in results if not r["features"] or r["features"] == []), None)
    if lat and nul and lat["cv"] and nul["cv"]:
        print(f"\n[backtest] de uitgerolde constante wint {nul['cv'] - lat['cv']:+.3f} °C op "
              f"'geen correctie'. Dát is wat er op het spel staat; elke modelvorm hierboven "
              f"vecht om de rest.")
    print("[backtest] 'skill' is t.o.v. geen correctie. Een kandidaat wordt alleen "
          "AANGENOMEN als hij op forward-chaining wint, in élke fold wint, én de winst "
          "boven de A/A-ruisvloer ligt.")


if __name__ == "__main__":
    main()

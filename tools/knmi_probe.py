#!/usr/bin/env python3
"""Verkenner voor de KNMI-referentie (fase 1) — draait handmatig, schrijft niets.

Twee taken, in deze volgorde:

1. **Vorm vaststellen.** De parser in `knmi_ref.py` is geschreven tegen de
   gedocumenteerde vorm van de scriptservice, niet tegen een gemeten respons —
   de ontwikkelomgeving mag knmi.nl niet bereiken. Deze probe print de eerste
   ruwe records letterlijk, zodat de eerste Actions-run de waarheid laat zien
   in plaats van dat een aanname stilletjes de productiepad in glijdt.

2. **De referentie wegen.** Voor de uren die al in `data/station_history/`
   staan: is `WU − KNMI` strakker dan `WU − ERA5`? Dat is de hele reden voor
   fase 1 — een scherpere referentie verlaagt de vloer waar de helling tegenaan
   gefit wordt. Zonder overlap slaat dit deel zichzelf over.

    python tools/knmi_probe.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]

Standaardvenster: de laatste 7 volle dagen.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import knmi_ref  # noqa: E402
from shared_const import local_today  # noqa: E402
from station_accuracy import load_archive  # noqa: E402


def _stats(pairs: list[tuple[float, float]]) -> dict:
    diffs = [a - b for a, b in pairs]
    n = len(diffs)
    mean = sum(diffs) / n
    return {"n": n, "bias": mean, "rmse": math.sqrt(sum(d * d for d in diffs) / n),
            "sd": math.sqrt(sum((d - mean) ** 2 for d in diffs) / n)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--station", type=int, default=knmi_ref.DE_BILT)
    args = ap.parse_args()

    end = args.end or (local_today() - timedelta(days=1)).isoformat()
    start = args.start or (local_today() - timedelta(days=7)).isoformat()

    params_preview = {"start": start, "end": end, "stns": args.station, "vars": knmi_ref.VARS}
    print(f"[probe] {knmi_ref.ENDPOINT} {params_preview}")

    payload = knmi_ref.get_json(knmi_ref.ENDPOINT, {
        "start": start.replace("-", "") + "01", "end": end.replace("-", "") + "24",
        "stns": str(args.station), "vars": knmi_ref.VARS, "fmt": "json"},
        timeout=30, label="knmi-probe")
    records = payload if isinstance(payload, list) else payload.get("data", [])
    print(f"[probe] {len(records)} ruwe records; type={type(payload).__name__}")
    print("[probe] eerste records, letterlijk:")
    for rec in records[:3]:
        print("   ", json.dumps(rec, ensure_ascii=False))

    hours = knmi_ref.parse_hourly(records)
    print(f"[probe] {len(hours)} uren geparsed")
    for key in sorted(hours)[:3]:
        print("   ", key, hours[key])
    if not hours:
        print("[probe] LET OP: niets geparsed — vergelijk de ruwe records hierboven met "
              "knmi_ref.parse_hourly (veldnamen, uur-conventie, eenheden)")
        return

    archive = {row["t"]: row for row in load_archive()}
    common = sorted(set(archive) & set(hours))
    if not common:
        print("[probe] geen overlap met data/station_history — sla de vergelijking over")
        return

    knmi = _stats([(archive[k]["wu"], hours[k]["temp"]) for k in common])
    era5 = _stats([(archive[k]["wu"], archive[k]["om"]) for k in common])
    print(f"\n[probe] {len(common)} gemeenschappelijke uren")
    print(f"{'referentie':12s} {'n':>5s} {'bias':>7s} {'rmse':>7s} {'sd':>7s}")
    for label, s in (("KNMI 260", knmi), ("ERA5", era5)):
        print(f"{label:12s} {s['n']:5d} {s['bias']:+7.2f} {s['rmse']:7.2f} {s['sd']:7.2f}")
    print("\nLagere sd = strakkere referentie = lagere vloer voor de hellingfit.\n"
          "Een groot verschil in *bias* is geen kwaliteitsoordeel (dat is deels echt "
          "microklimaat over 4,1 km); het gaat om de spreiding.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Coherentie-toets: delen de buur-PWS'en onze warme, windstille nacht?

Dit is de meting die de fase-3-beslissing blokkeert. Zie `neighbour_pws.py` voor
het waarom; kort: tegen KNMI leest ons station 's nachts +0,56 °C te warm terwijl
er geen straling is, en dat is óf het microklimaat van de buurt (dan moet de
correctie eraf blijven) óf onze plaatsing (dan is een interceptterm terecht). Eén
referentie op 4,1 km kan die twee niet scheiden; buren op enkele honderden meters
wel, omdat ze ons microklimaat delen maar een andere kap hebben.

    python tools/neighbour_probe.py [--days 30]

Vereist `WU_STATION_ID`, `WU_API_KEY` en `WU_NEIGHBOUR_IDS` (komma-gescheiden).
Leest alleen; schrijft en committeert niets. Station-id's worden nooit geprint.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import knmi_ref  # noqa: E402
import neighbour_pws as npws  # noqa: E402
from shared_const import local_today  # noqa: E402
from station_accuracy import fetch_wu_hourly  # noqa: E402


def _row(label: str, prof: dict) -> str:
    def fmt(value, spec="+.2f"):
        return "—" if value is None else format(value, spec)
    wind = "  ".join(f"{fmt(b['bias']):>6s}" for b in prof["by_wind"])
    return (f"{label:10s} {prof['n']:5d} {fmt(prof['night_bias']):>7s} "
            f"{fmt(prof['day_bias']):>7s} {fmt(prof['slope_per_100'], '+.3f'):>8s}   {wind}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    station_id, api_key = os.environ.get("WU_STATION_ID"), os.environ.get("WU_API_KEY")
    buren = npws.neighbour_ids()
    if not (station_id and api_key):
        raise SystemExit("WU_STATION_ID/WU_API_KEY ontbreken")
    if not buren:
        raise SystemExit("WU_NEIGHBOUR_IDS ontbreekt — zet de buur-station-id's "
                         "komma-gescheiden in dat secret (nooit in de repo)")

    end, start = local_today() - timedelta(days=1), local_today() - timedelta(days=args.days)
    print(f"[probe] {args.days} dagen ({start} t/m {end}), {len(buren)} buren, "
          f"KNMI {knmi_ref.DE_BILT} als gemeenschappelijk frame")

    ref = knmi_ref.fetch_hourly(start.isoformat(), end.isoformat())
    if not ref:
        raise SystemExit("geen KNMI-uren — zonder frame valt er niets te vergelijken")

    ons = npws.profile(npws.pair_with_reference(
        fetch_wu_hourly(station_id, api_key, args.days), ref))
    buurprofielen = []
    for i, sid in enumerate(buren, 1):
        rows = npws.pair_with_reference(fetch_wu_hourly(sid, api_key, args.days), ref)
        if not rows:
            print(f"[probe] buur {i}: geen gekoppelde uren — overgeslagen")
            continue
        buurprofielen.append(npws.profile(rows))

    kop = "  ".join(f"{lo:.0f}-{hi:.0f}".rjust(6) for lo, hi in npws.WIND_BINS)
    print(f"\n{'station':10s} {'n':>5s} {'nacht':>7s} {'dag':>7s} {'°C/100W':>8s}   "
          f"nachtbias per wind (km/h): {kop}")
    print(_row("ONS", ons))
    for i, prof in enumerate(buurprofielen, 1):
        print(_row(f"buur {i}", prof))

    code, uitleg = npws.verdict(ons, buurprofielen)
    sd_nacht = npws.spread(buurprofielen, "night_bias")
    sd_zon = npws.spread(buurprofielen, "slope_per_100")
    print(f"\n[spreiding tussen de buren] nacht sd={sd_nacht if sd_nacht is None else round(sd_nacht, 2)} °C, "
          f"zonhelling sd={sd_zon if sd_zon is None else round(sd_zon, 3)} °C/100W/m²")
    print(f"[oordeel] {code.upper()}: {uitleg}")
    print("\nLees de zonhelling ernaast: dát is de term waarvan we zéker weten dat "
          "een kap hem kan veroorzaken.\nSteken wij daar ook bovenuit, dan is de "
          "plaatsing sowieso een aandachtspunt — los van de nachtvraag.")


if __name__ == "__main__":
    main()

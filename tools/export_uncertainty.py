#!/usr/bin/env python3
"""Turn the rolling-origin backtest into a per-horizon uncertainty band, plus the
training envelope the out-of-distribution guard needs.

A point estimate is the wrong answer to the question the playground exists to
answer. "Will the bedroom be under 20 °C by 07:00?" needs a band; a single line
implies a precision the model does not have.

The band is **empirical**: for each (room, horizon hour), the distribution of
`predicted - measured` over every scored origin in `horizon_backtest.py`. No
distributional assumption, and it automatically widens with horizon and for the
rooms that are genuinely harder.

Two honest limits, both carried into the output so the UI can state them:

  * **Model error only.** The backtest replays hindcast weather as if it were a
    perfect forecast, so the band excludes forecast error entirely. Real bands are
    wider by an unmeasured amount.
  * **Observed states only.** The residuals come from window states the house
    actually occupied. Applying them to a counterfactual assumes the model's error
    is similar there, which is an assumption, not a measurement.

The envelope is the range of conditions the model was ever validated over. Anything
outside it — most importantly any outdoor temperature below the summer record's
minimum, i.e. the entire heating season — is extrapolation and must be marked.

Usage:
    python tools/horizon_backtest.py --horizon-h 12 --stride-h 3 --out tools/out/h12.json
    python tools/export_uncertainty.py --backtest tools/out/h12.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import vent_io as vio          # noqa: E402


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (pos - lo) * (sorted_vals[hi] - sorted_vals[lo])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backtest", default="tools/out/h12.json")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(HERE), "docs", "js",
                                                  "uncertainty.json"))
    ap.add_argument("--min-n", type=int, default=40,
                    help="minimum residuals before a (room, hour) cell is trusted")
    args = ap.parse_args()

    with open(args.backtest, encoding="utf-8") as f:
        bt = json.load(f)

    # residuals[room][horizon_hour] = [pred - actual, ...]
    res = defaultdict(lambda: defaultdict(list))
    for _t0, rooms in bt["origins"].items():
        for rid, per in rooms.items():
            for h_min, v in per.items():
                pred, _free, act = v
                if pred is None or act is None:
                    continue
                hour = max(1, -(-int(h_min) // 60))     # ceil to the hour, like the scorer
                res[rid][hour].append(pred - act)

    bands = {}
    for rid, per in res.items():
        bands[rid] = {}
        for hour, vals in sorted(per.items()):
            vals.sort()
            bands[rid][str(hour)] = {
                "n": len(vals),
                "p10": round(quantile(vals, 0.10), 4),
                "p50": round(quantile(vals, 0.50), 4),
                "p90": round(quantile(vals, 0.90), 4),
                "trusted": len(vals) >= args.min_n,
            }

    # ---- training envelope ---------------------------------------------------
    house = vio.load_house()
    ds = vio.load_dataset(house)
    rows = [r for r in ds["weather_rows"] if r.get("T_out") is not None]
    t_out = [r["T_out"] for r in rows]
    wind = [r.get("wind_speed") or 0.0 for r in rows]
    ts = sorted(t for s in ds["actual"].values() for t, _ in s)

    envelope = {
        "t_out_min": round(min(t_out), 1), "t_out_max": round(max(t_out), 1),
        "wind_max": round(max(wind), 1),
        "record_start": ts[0].isoformat(), "record_end": ts[-1].isoformat(),
        "days": round((ts[-1] - ts[0]).total_seconds() / 86400, 1),
        # The single most important limitation, stated as data rather than prose.
        "heating_season_observed": False,
        "note": ("Summer only. Samples where tado reported active heating are excluded "
                 "from calibration, and the record contains no heating season at all, so "
                 "any scenario below t_out_min is extrapolation."),
    }

    out = {
        "source": os.path.basename(args.backtest),
        "physics_rev": vio.PHYSICS_REV,
        "band": "empirical p10/p50/p90 of (predicted - measured), per room per horizon hour",
        "excludes_forecast_error": True,
        "measured_on_observed_states_only": True,
        "envelope": envelope,
        "bands": bands,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)

    print(f"envelope: t_out {envelope['t_out_min']}–{envelope['t_out_max']} °C, "
          f"wind ≤ {envelope['wind_max']} m/s, {envelope['days']} days, no heating season")
    print(f"\n{'room':9}" + "".join(f"{'h' + str(h):>16}" for h in (1, 4, 8, 12)))
    for rid in sorted(bands):
        line = f"{rid:9}"
        for h in (1, 4, 8, 12):
            c = bands[rid].get(str(h))
            line += f"{c['p10']:+.2f} / {c['p90']:+.2f}".rjust(16) if c else "-".rjust(16)
        print(line)
    print("\n(p10/p90 of predicted - measured; the band on a forecast is pred-p90 … pred-p10)")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

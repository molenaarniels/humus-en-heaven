#!/usr/bin/env python3
"""Read-only diagnostiek voor de ventilatie-tweeling (Project 8).

Consumeert de gecommitte artefacten — `docs/airflow_learned.json` (leercurve + params +
checkpoint) en `docs/airflow_data.json` (huidige per-kamer-toestand) — en print een
markdown-rapport dat de assessment onderbouwt:

  1. Regime-curve   — RMSE & skill vs. dag-max/zon, uit `rmse_history` (wanneer faalt het?).
  2. Saturatie      — elke geleerde param vs. zijn BOUNDS; floor/ceiling-rails gemarkeerd.
  3. Residu-ontleding — voorspeld−werkelijk per kamer, per uur-van-de-dag, naast de
                        gepubliceerde energie-termen (solar/env/vent/party/internal_w) →
                        scheidt zon-gedreven bias (middag) van envelope (volgt buiten) van
                        ventilatie (vroege ochtend → niet-gemelde nachtventilatie).
  4. Airco          — gerapporteerde AC-kamer + uit-de-fit-gelaten samples.

Géén model-mutatie, géén netwerk, géén third-party deps. Draai:  python tools/airflow_diagnostics.py
Optioneel:  python tools/airflow_diagnostics.py --out AIRFLOW_ASSESSMENT_DATA.md
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime

# Repo-root op het pad zodat de bounds/priors uit de bron-of-waarheid komen (geen duplicatie).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from airflow_model import (  # noqa: E402
    BOUNDS, GLOBAL_PARAMS, PER_ROOM_PARAMS, PRIORS,
)

LEARNED_PATH = os.getenv("AIRFLOW_LEARNED_PATH", "docs/airflow_learned.json")
DATA_PATH = os.getenv("AIRFLOW_DATA_PATH", "docs/airflow_data.json")

RAIL_TOL = 0.02   # binnen deze fractie van de band-breedte → 'geraild' op die grens
ENERGY_KEYS = ["solar_w", "env_w", "vent_w", "party_w", "internal_w"]


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_dt(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def interp(series: list[dict], ts: datetime) -> float | None:
    """Lineaire interpolatie van een [{t,temp}]-reeks op tijdstip ts (None bij lege reeks)."""
    pts = [(parse_dt(p["t"]), p.get("temp")) for p in series]
    pts = [(t, v) for t, v in pts if t is not None and v is not None]
    if not pts:
        return None
    if ts <= pts[0][0]:
        return pts[0][1]
    if ts >= pts[-1][0]:
        return pts[-1][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= ts <= t1:
            f = (ts - t0).total_seconds() / max(1.0, (t1 - t0).total_seconds())
            return v0 + f * (v1 - v0)
    return pts[-1][1]


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _fmt(v, nd: int = 2) -> str:
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"


# ── 1. Regime-curve ───────────────────────────────────────────────────────────────────
def regime_report(learned: dict) -> str:
    hist = [h for h in learned.get("rmse_history", [])
            if h.get("rmse") is not None and not h.get("held")
            and h.get("rmse") == h.get("rmse")]
    if not hist:
        return "### 1. Regime-curve\n\n_Geen leercurve-historie._\n"
    bins = [("≤25", -1e9, 25.0), ("25–28", 25.0, 28.0), ("28–31", 28.0, 31.0),
            ("31–34", 31.0, 34.0), (">34", 34.0, 1e9)]
    lines = ["### 1. Regime-curve — fout vs. weer\n",
             "| dag-max (°C) | n | RMSE (gem) | skill (gem) | zon-gem (W/m²) |",
             "|---|---|---|---|---|"]
    no_wx = []
    for label, lo, hi in bins:
        rows = [h for h in hist
                if (h.get("wx", {}).get("tmax") is not None
                    and lo <= h["wx"]["tmax"] < hi)]
        if not rows:
            continue
        rmse_m = _mean([h["rmse"] for h in rows])
        skill_m = _mean([h["skill"] for h in rows if h.get("skill") is not None])
        solar_m = _mean([h["wx"]["solar_mean"] for h in rows
                         if h.get("wx", {}).get("solar_mean") is not None])
        lines.append(f"| {label} | {len(rows)} | {_fmt(rmse_m)} | {_fmt(skill_m)} "
                     f"| {_fmt(solar_m, 0)} |")
    no_wx = [h for h in hist if h.get("wx", {}).get("tmax") is None]
    if no_wx:
        rmse_m = _mean([h["rmse"] for h in no_wx])
        lines.append(f"| (geen wx-stempel) | {len(no_wx)} | {_fmt(rmse_m)} | — | — |")
    # Headline-correlatie tmax↔rmse over alle gestempelde punten.
    pairs = [(h["wx"]["tmax"], h["rmse"]) for h in hist
             if h.get("wx", {}).get("tmax") is not None]
    if len(pairs) >= 3:
        lines.append("\n" + _corr_line("RMSE", "dag-max temp", pairs))
    spairs = [(h["wx"]["solar_mean"], h["rmse"]) for h in hist
              if h.get("wx", {}).get("solar_mean") is not None]
    if len(spairs) >= 3:
        lines.append(_corr_line("RMSE", "zon-gemiddelde", spairs))
    return "\n".join(lines) + "\n"


def _corr_line(yname: str, xname: str, pairs: list[tuple]) -> str:
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = _mean(xs), _mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx <= 0 or sy <= 0:
        return ""
    r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)
    return f"- Pearson-correlatie **{yname} ↔ {xname}**: r = {r:+.2f} (n={len(pairs)})"


# ── 2. Saturatie (bound-railing) ───────────────────────────────────────────────────────
def _rail_flag(value: float, bounds: tuple) -> str:
    lo, hi = bounds
    rng = hi - lo
    if rng <= 0:
        return ""
    if value - lo <= RAIL_TOL * rng:
        return "⚠ FLOOR"
    if hi - value <= RAIL_TOL * rng:
        return "⚠ CEIL"
    return ""


def saturation_report(learned: dict) -> str:
    params = learned.get("params", {})
    lines = ["### 2. Saturatie — geleerde params vs. grenzen\n",
             "| scope | param | waarde | prior | bounds | rail |",
             "|---|---|---|---|---|---|"]
    rails = 0
    for name in GLOBAL_PARAMS:
        if name not in params:
            continue
        b = BOUNDS.get(name)
        flag = _rail_flag(params[name], b) if b else ""
        rails += bool(flag)
        lines.append(f"| global | `{name}` | {_fmt(params[name], 3)} | "
                     f"{_fmt(PRIORS.get(name), 2)} | {b} | {flag} |")
    for rid, rp in params.items():
        if not isinstance(rp, dict):
            continue
        for name in PER_ROOM_PARAMS:
            if name not in rp:
                continue
            b = BOUNDS.get(name)
            flag = _rail_flag(rp[name], b) if b else ""
            if not flag:
                continue   # toon alleen de gerailde per-kamer-params; rest is ruis
            rails += 1
            lines.append(f"| {rid} | `{name}` | {_fmt(rp[name], 3)} | "
                         f"{_fmt(PRIORS.get(name), 2)} | {b} | {flag} |")
    lines.append(f"\n**{rails} gerailde parameter(s).** Veel floor-rails op de warmte-in-kanalen "
                 "(solar_gain/ua_env/q_int/f_air/cp_shelter) = de optimizer duwt élk warmte-kanaal "
                 "naar minimum en komt nóg niet koel genoeg → structureel tekort of te-warme prior.")
    return "\n".join(lines) + "\n"


# ── 3. Residu-ontleding per kamer ──────────────────────────────────────────────────────
def residual_report(data: dict) -> str:
    rooms = data.get("rooms", {})
    lines = ["### 3. Residu-ontleding per kamer (voorspeld − werkelijk)\n",
             "Positief = model te warm. Energie-termen zijn de gepubliceerde momentwaarden (W).\n",
             "| kamer | nu err | bias (gem) | RMSE | solar_w | env_w | vent_w | party_w | int_w |",
             "|---|---|---|---|---|---|---|---|---|"]
    hourly_all: dict[int, list[float]] = {}
    for rid, r in rooms.items():
        pred = r.get("predicted_series", [])
        act = r.get("actual_series", [])
        res = []
        for a in act:
            ts = parse_dt(a.get("t"))
            av = a.get("temp")
            if ts is None or av is None:
                continue
            pv = interp(pred, ts)
            if pv is None:
                continue
            res.append((ts, pv - av))
        bias = _mean([d for _, d in res])
        rmse_v = math.sqrt(_mean([d * d for _, d in res])) if res else None
        for ts, d in res:
            hourly_all.setdefault(ts.hour, []).append(d)
        lines.append(
            f"| {rid} | {_fmt(r.get('error'))} | {_fmt(bias)} | {_fmt(rmse_v)} "
            f"| {_fmt(r.get('solar_w'), 0)} | {_fmt(r.get('env_w'), 0)} "
            f"| {_fmt(r.get('vent_w'), 0)} | {_fmt(r.get('party_w'), 0)} "
            f"| {_fmt(r.get('internal_w'), 0)} |")
    # Bias per uur-van-de-dag, over alle kamers samen → discrimineert de oorzaak.
    if hourly_all:
        lines += ["\n**Gemiddelde bias per uur-van-de-dag (alle kamers):**\n",
                  "| uur | n | bias (°C) |", "|---|---|---|"]
        for h in sorted(hourly_all):
            vals = hourly_all[h]
            lines.append(f"| {h:02d} | {len(vals)} | {_fmt(_mean(vals))} |")
        lines.append("\n_Middag-piek → zon over-gedreven; volgt-buiten → envelope; "
                     "vroege-ochtend-piek → niet-gemelde nachtventilatie._")
    return "\n".join(lines) + "\n"


# ── 4. Airco ───────────────────────────────────────────────────────────────────────────
def ac_report(data: dict) -> str:
    ac = data.get("ac", {}) or {}
    room = ac.get("room")
    rooms = data.get("rooms", {})
    lines = ["### 4. Airco-contaminatie\n",
             f"- AC-kamer nu: **{room or '—'}**"]
    if room and room in rooms:
        r = rooms[room]
        excl = r.get("ac_excluded_samples", 0)
        lines.append(f"- `ac_excluded_samples` voor {room}: **{excl}**")
        lines.append(f"- {room}: gemeten {_fmt(r.get('actual_temp'))} °C, "
                     f"voorspeld {_fmt(r.get('predicted_temp'))} °C, "
                     f"comfort-band {_fmt(r.get('comfort_low'))}–{_fmt(r.get('comfort_high'))} °C")
        if excl == 0 and r.get("actual_temp") is not None and r.get("comfort_low") is not None \
                and r["actual_temp"] < r["comfort_low"]:
            lines.append("- ⚠ AC-kamer leest **onder** zijn comfort-vloer terwijl er **0** samples "
                         "uit de fit vielen → de AC-koude vervuilt de fit (retroactief-only "
                         "exclusie ving de niet-teruggedateerde melding niet).")
    return "\n".join(lines) + "\n"


def build_report(learned: dict, data: dict) -> str:
    gen = data.get("generated_at", "?")
    wx = data.get("weather", {})
    head = [f"# Ventilatie-tweeling — diagnostiek ({gen})\n",
            f"- buiten: {_fmt(wx.get('outside_temp'))} °C ({wx.get('outside_source', '?')}), "
            f"buur-anker {_fmt(wx.get('neighbor_temp'))} °C",
            f"- huidige RMSE: {_fmt(learned.get('rmse'), 3)} °C; "
            f"checkpoint: skill {learned.get('checkpoint', {}).get('skill')}, "
            f"RMSE {learned.get('checkpoint', {}).get('rmse')}\n"]
    return "\n".join(head) + "\n".join([
        regime_report(learned),
        saturation_report(learned),
        residual_report(data),
        ac_report(data),
    ])


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only diagnostiek voor de ventilatie-tweeling.")
    ap.add_argument("--out", help="schrijf het markdown-rapport ook naar dit bestand")
    ap.add_argument("--learned", default=LEARNED_PATH)
    ap.add_argument("--data", default=DATA_PATH)
    args = ap.parse_args()

    learned = load_json(args.learned)
    data = load_json(args.data)
    report = build_report(learned, data)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n[diagnostiek] geschreven naar {args.out}")


if __name__ == "__main__":
    main()

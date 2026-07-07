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
from datetime import datetime, timedelta, timezone

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


def room_residuals(r: dict) -> list[tuple[datetime, float]]:
    """(t, voorspeld − werkelijk) per actual-sample van één kamer-rij uit airflow_data.json
    (voorspeld lineair geïnterpoleerd). Gedeeld door de residu-ontleding en de zon-decompositie."""
    pred = r.get("predicted_series", [])
    res = []
    for a in r.get("actual_series", []):
        ts = parse_dt(a.get("t"))
        av = a.get("temp")
        if ts is None or av is None:
            continue
        pv = interp(pred, ts)
        if pv is None:
            continue
        res.append((ts, pv - av))
    return res


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _fmt(v, nd: int = 2) -> str:
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "—"


# ── 1. Regime-curve ───────────────────────────────────────────────────────────────────
RECENT_REGIME_H = 48.0   # tweede regimetabel: alleen de laatste 48u (post-convergentie-blik)


def _regime_table(hist: list[dict], title: str) -> list[str]:
    """Bin-tabel + correlaties voor een (al gefilterde) puntenlijst."""
    bins = [("≤25", -1e9, 25.0), ("25–28", 25.0, 28.0), ("28–31", 28.0, 31.0),
            ("31–34", 31.0, 34.0), (">34", 34.0, 1e9)]
    lines = [f"**{title}**\n",
             "| dag-max (°C) | n | RMSE (gem) | skill (gem) | zon-gem (W/m²) |",
             "|---|---|---|---|---|"]
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
    pairs = [(h["wx"]["tmax"], h["rmse"]) for h in hist
             if h.get("wx", {}).get("tmax") is not None]
    if len(pairs) >= 3:
        lines.append("\n" + _corr_line("RMSE", "dag-max temp", pairs))
    spairs = [(h["wx"]["solar_mean"], h["rmse"]) for h in hist
              if h.get("wx", {}).get("solar_mean") is not None]
    if len(spairs) >= 3:
        lines.append(_corr_line("RMSE", "zon-gemiddelde", spairs))
    return lines


def regime_report(learned: dict, since: datetime | None = None) -> str:
    # `fell_back`-punten vallen áltijd uit de bins: op zo'n run zijn de checkpoint-params
    # teruggezet en meet het punt de fallback, niet het lopende leren.
    hist = [h for h in learned.get("rmse_history", [])
            if h.get("rmse") is not None and not h.get("held") and not h.get("fell_back")
            and h.get("rmse") == h.get("rmse")]
    if since is not None:
        hist = [h for h in hist
                if (parse_dt(h.get("t")) or since) >= since]
    if not hist:
        return "### 1. Regime-curve\n\n_Geen leercurve-historie._\n"
    lines = ["### 1. Regime-curve — fout vs. weer\n"]
    lines += _regime_table(hist, "Volledig venster" + (f" (vanaf {since:%Y-%m-%d %H:%M})"
                                                       if since else ""))
    # Tweede blik: alleen de laatste 48u, geankerd op het nieuwste punt. De juli-assessment
    # vond dat het volle venster de leerfase mee-bint (koele dagen ↔ nog-niet-geconvergeerd →
    # schijncorrelatie fout↔weer); deze tabel toont het regime-beeld zónder die confound.
    anchor = max((t for t in (parse_dt(h.get("t")) for h in hist) if t is not None),
                 default=None)
    if anchor is not None:
        cut = anchor - timedelta(hours=RECENT_REGIME_H)
        recent = [h for h in hist if (parse_dt(h.get("t")) or cut) >= cut]
        if recent and len(recent) < len(hist):
            lines.append("")
            lines += _regime_table(recent, f"Laatste {RECENT_REGIME_H:.0f}u (post-convergentie)")
    return "\n".join(lines) + "\n"


def _pearson(pairs: list[tuple]) -> float | None:
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = _mean(xs), _mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx <= 0 or sy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _corr_line(yname: str, xname: str, pairs: list[tuple]) -> str:
    r = _pearson(pairs)
    if r is None:
        return ""
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
    hourly_room: dict[str, dict[int, list[float]]] = {}
    for rid, r in rooms.items():
        res = room_residuals(r)
        bias = _mean([d for _, d in res])
        rmse_v = math.sqrt(_mean([d * d for _, d in res])) if res else None
        for ts, d in res:
            hourly_all.setdefault(ts.hour, []).append(d)
            hourly_room.setdefault(rid, {}).setdefault(ts.hour, []).append(d)
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
    # Per-kamer bias-per-uur-matrix: lokaliseert een dip/piek (bv. dageraad-onderkoeling) —
    # zit hij in de dak-kamers (office/stair, ROOF_SKY_COOLING) of huisbreed?
    room_ids = [rid for rid in rooms if hourly_room.get(rid)]
    if room_ids:
        lines += ["\n**Bias per uur, per kamer (°C):**\n",
                  "| uur | " + " | ".join(room_ids) + " |",
                  "|---|" + "---|" * len(room_ids)]
        for h in sorted(hourly_all):
            cells = [_fmt(_mean(hourly_room[rid].get(h, []))) for rid in room_ids]
            lines.append(f"| {h:02d} | " + " | ".join(cells) + " |")
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


# ── 5. Zon-decompositie per raam (opt-in, gebruikt netwerk) ────────────────────────────
def solar_decomp_report(data: dict, room_id: str = "living") -> str:
    """Correleert het uurlijkse residu van één kamer met de getransmitteerde instraling per
    raam van die kamer, herberekend over het actual-venster met de pure helpers
    (`per_window_solar`/`facade_irradiance`) + `house_model.json`. Historische per-raam-zon
    wordt níét gepersisteerd (`solar_by_window` is een nu-snapshot), dus de drivers komen
    van een verse Open-Meteo-fetch (`fetch_weather`, past_days dekt het ~48u-venster) —
    dít is de enige netwerk-afhankelijke sectie en draait alleen onder `--solar-decomp`.
    Benadering: de zonwering-standen zijn de HUIDIGE gerapporteerde standen (de log-historie
    vergt Gist-secrets); voor een welk-raam-drijft-het-residu-diagnose is dat ruim genoeg."""
    from airflow_model import (
        _LAT, _LON, _interp_hourly, fetch_weather, load_house, per_window_solar, sun_position,
    )
    room = data.get("rooms", {}).get(room_id)
    if not room:
        return f"### 5. Zon-decompositie\n\n_Kamer `{room_id}` niet in airflow_data.json._\n"
    res = room_residuals(room)
    if len(res) < 8:
        return f"### 5. Zon-decompositie\n\n_Te weinig residu-samples voor `{room_id}`._\n"
    house = load_house()
    states = data.get("openings", {}) or {}
    windows = {wid: w for wid, w in house.get("windows", {}).items()
               if w.get("room") == room_id}
    if not windows:
        return f"### 5. Zon-decompositie\n\n_Geen ramen voor `{room_id}` in house_model.json._\n"
    try:
        rows = fetch_weather()["hourly"]
    except Exception as e:                                    # noqa: BLE001 — diagnose, geen runner
        return ("### 5. Zon-decompositie\n\n_Open-Meteo niet bereikbaar "
                f"({type(e).__name__}) — deze sectie vergt netwerk._\n")
    per_win: dict[str, list[float]] = {wid: [] for wid in windows}
    for ts, _ in res:
        s_az, s_el = sun_position(_LAT, _LON, ts.astimezone(timezone.utc))
        direct = _interp_hourly(rows, ts, "direct")
        diffuse = _interp_hourly(rows, ts, "diffuse")
        pw = per_window_solar(house, states, s_az, s_el, direct, diffuse, beam_iam=True)
        for wid in windows:
            per_win[wid].append(pw.get(wid, 0.0))
    resid = [d for _, d in res]
    lines = [f"### 5. Zon-decompositie — residu `{room_id}` vs. instraling per raam\n",
             "Correlatie residu↔W per raam over het actual-venster (huidige zonwering-standen; "
             "positief = raam-instraling loopt mee met de te-warm-fout).\n",
             "| raam | label | corr | gem. W (dag) | piekuur W | piekuur residu |",
             "|---|---|---|---|---|---|"]
    # Piekuur van het residu (uur met de hoogste gemiddelde bias) als referentie.
    hr_res: dict[int, list[float]] = {}
    for ts, d in res:
        hr_res.setdefault(ts.hour, []).append(d)
    peak_res = max(hr_res, key=lambda h: _mean(hr_res[h])) if hr_res else None
    for wid, w in windows.items():
        watts = per_win[wid]
        day = [(ts, v) for (ts, _), v in zip(res, watts) if v > 1.0]
        hr_w: dict[int, list[float]] = {}
        for ts, v in day:
            hr_w.setdefault(ts.hour, []).append(v)
        peak_w = max(hr_w, key=lambda h: _mean(hr_w[h])) if hr_w else None
        r = _pearson(list(zip(watts, resid)))
        r_txt = f"{r:+.2f}" if r is not None else "—"
        lines.append(f"| `{wid}` | {w.get('label', '')} | {r_txt} "
                     f"| {_fmt(_mean([v for _, v in day]) or 0.0, 0)} "
                     f"| {peak_w if peak_w is not None else '—'} "
                     f"| {peak_res if peak_res is not None else '—'} |")
    lines.append("\n_Het raam waarvan corr én piekuur het residu-piekuur matchen is de "
                 "hoofdverdachte voor de over-gedreven zonwinst (horizon-mask/overstek/g-waarde "
                 "in house_model.json nalopen)._")
    return "\n".join(lines) + "\n"


def build_report(learned: dict, data: dict, since: datetime | None = None,
                 solar_room: str | None = None) -> str:
    gen = data.get("generated_at", "?")
    wx = data.get("weather", {})
    head = [f"# Ventilatie-tweeling — diagnostiek ({gen})\n",
            f"- buiten: {_fmt(wx.get('outside_temp'))} °C ({wx.get('outside_source', '?')}), "
            f"buur-anker {_fmt(wx.get('neighbor_temp'))} °C",
            f"- huidige RMSE: {_fmt(learned.get('rmse'), 3)} °C; "
            f"checkpoint: skill {learned.get('checkpoint', {}).get('skill')}, "
            f"RMSE {learned.get('checkpoint', {}).get('rmse')}\n"]
    sections = [
        regime_report(learned, since=since),
        saturation_report(learned),
        residual_report(data),
        ac_report(data),
    ]
    if solar_room:
        sections.append(solar_decomp_report(data, solar_room))
    return "\n".join(head) + "\n".join(sections)


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only diagnostiek voor de ventilatie-tweeling.")
    ap.add_argument("--out", help="schrijf het markdown-rapport ook naar dit bestand")
    ap.add_argument("--learned", default=LEARNED_PATH)
    ap.add_argument("--data", default=DATA_PATH)
    ap.add_argument("--since", help="alleen leercurve-punten vanaf dit ISO-tijdstip "
                                    "(filtert de leerfase uit de regime-tabel)")
    ap.add_argument("--solar-decomp", nargs="?", const="living", metavar="KAMER",
                    help="zon-decompositie per raam voor deze kamer (default living); "
                         "haalt Open-Meteo op — de enige netwerk-afhankelijke sectie")
    args = ap.parse_args()

    since = parse_dt(args.since) if args.since else None
    if args.since and since is None:
        ap.error(f"--since: ongeldig ISO-tijdstip: {args.since!r}")
    if since is not None and since.tzinfo is None:
        from shared_const import TZ   # kaal ISO-tijdstip → lokale tijd (punten zijn tz-aware)
        since = since.replace(tzinfo=TZ)
    learned = load_json(args.learned)
    data = load_json(args.data)
    report = build_report(learned, data, since=since, solar_room=args.solar_decomp)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n[diagnostiek] geschreven naar {args.out}")


if __name__ == "__main__":
    main()

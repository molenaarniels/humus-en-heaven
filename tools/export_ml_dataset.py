#!/usr/bin/env python3
"""Exporteer een platte, ML-klare dataset voor de ventilatie-tweeling.

Doel: alles wat je nodig hebt om lokaal (bv. op een MacBook, in een notebook)
zélf een model te trainen en met de "perfecte" structuur/parameters te
experimenteren — zonder de repo-fysica of secrets. Puur uit de gecommitte
maand-shards (`data/twin2_history/*.json`), dus **volledig offline** (de shards
bevatten het weer) en zonder API-sleutels.

Wat het doet
------------
1. `airflow2_model.load_dataset(house)` — voegt alle shards samen tot de
   uitgelijnde grond-waarheid: per kamer de tado-temp/RH-samples, de stook-
   tijdstippen, de weer-rijen en de samengevoegde openingen-log.
2. `airflow_model.build_timeline(...)` — bouwt één 15-minuten-raster van drivers
   over de hele spanwijdte: buitentemp, wind, per-kamer instraling *door het
   glas*, dak-instraling, zonstand en de (voorwaarts-ingevulde) raam/deur/
   rooster-standen op elk moment.
3. Voegt per (tijdstip, kamer) de gemeten temp/RH toe als **doelwaarde**
   (nearest-sample binnen `--join-tol-min`), plus de stook/airco/pauze-vlaggen
   waarmee de tweeling die samples juist uit de kalibratie laat vallen — zodat
   jij dezelfde filters kunt reproduceren.
4. Optioneel (`--baseline`, standaard aan): draait de bestaande grey-box-
   fysica (tweeling 1 = 2-knoops, tweeling 2 = 3-knoops) opnieuw over
   niet-overlappende 5-daagse vensters (24u warmup, hergeseed uit de metingen)
   en schrijft hun voorspelling per rij weg als `pred_twin1_c`/`pred_twin2_c`.
   Zo heb je meteen een fysica-baseline om te verslaan, of om residu-leren op
   te doen ("leer alleen de fout van het fysica-model").

Uitvoer (in `--out`, standaard `data/ml/`):
  - `ventilation_long.csv`   — één rij per (tijdstip, kamer); handig voor
                               per-kamer- en pooled-modellen.
  - `ventilation_wide.csv`   — één rij per tijdstip; kamertemps als kolommen;
                               handig voor state-space/RC-modellen.
  - `ventilation_long.parquet` / `ventilation_wide.parquet` — idem, compact +
                               getypeerd (alleen als pandas + pyarrow aanwezig).
  - `schema.json`            — data-dictionary (kolom → betekenis + eenheid).

Gebruik:
    python tools/export_ml_dataset.py                 # alles, incl. baseline
    python tools/export_ml_dataset.py --no-baseline   # alleen data (sneller)
    python tools/export_ml_dataset.py --out /pad --history-dir data/twin2_history
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import airflow_model as am          # noqa: E402
import airflow2_model as a2         # noqa: E402
from shared_const import TZ         # noqa: E402

# Kolommen die per tijdstip gedeeld zijn (identiek voor elke kamer op dat moment).
SHARED_META = ["t", "t_epoch"]
WEATHER_COLS = ["t_out_c", "rh_out", "wind_speed_ms", "wind_dir_deg", "gust_ms",
                "precip_mm", "solar_direct_wm2", "solar_diffuse_wm2",
                "solar_global_wm2", "sun_az_deg", "sun_el_deg", "neighbor_anchor_c"]
HOUSE_STATE_COLS = ["paused", "ac_room", "ac_here"]
# Per-kamer kolommen (in long: platte kolommen; in wide: met `__<kamer>`-suffix).
TARGET_COLS = ["temp_c", "humidity"]
ROOM_DRIVER_COLS = ["solar_glass_w", "roof_irr_w", "heating"]
BASELINE_COLS = ["pred_twin1_c", "pred_twin2_c"]


def _openness(val, elem: dict) -> float:
    """Zet een gerapporteerde stand om naar een 0..1-openingsfractie voor het
    model. `tilt`/kier → het element-eigen `tilt_frac` (default 0.3)."""
    if val is None:
        return 0.0
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    if isinstance(val, (int, float)):
        return max(0.0, min(1.0, float(val)))
    s = str(val).strip().lower()
    if s in ("open", "1", "true", "on", "aan", "ja"):
        return 1.0
    if s in ("dicht", "closed", "close", "0", "false", "off", "uit", "nee"):
        return 0.0
    if s in ("tilt", "kier", "gekiept", "kiep", "kiepstand"):
        return float(elem.get("tilt_frac", 0.3) or 0.3)
    return 0.0


def _element_meta(house: dict) -> dict:
    """Alle bedienbare elementen (ramen + roosters + deuren) → hun meta, gekeyd
    op element-id (zoals in de openingen-log)."""
    meta = {}
    for group in ("windows", "vents", "doors"):
        for eid, elem in (house.get(group) or {}).items():
            meta[eid] = elem
    return meta


def _nearest(samples: list, when: datetime, tol_s: float):
    """Nearest (dt, waarde) uit een op tijd gesorteerde lijst binnen `tol_s`
    seconden, of (None, None). `samples` = [(datetime, value), ...]."""
    if not samples:
        return None, None
    times = [s[0] for s in samples]
    i = bisect.bisect_left(times, when)
    best, best_d = None, None
    for j in (i - 1, i):
        if 0 <= j < len(samples):
            d = abs((samples[j][0] - when).total_seconds())
            if best_d is None or d < best_d:
                best, best_d = samples[j], d
    if best is None or best_d > tol_s:
        return None, None
    return best


def _iso_min(dt: datetime) -> str:
    """Stabiele minuut-sleutel om de master-timeline en de baseline-sims op te
    joinen (beide liggen op hetzelfde 15-min-rooster, verankerd op t_max)."""
    return dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


def _sensor_series(house, timeline, rid, series):
    """Fysica-voorspelling naar sensor-ruimte (buitenmuur-voeler-debias) zodat
    ze vergelijkbaar is met de gemeten tado-temp — het runner-recept."""
    return dict(_iso_pairs(am._to_sensor_series(house, timeline, rid, series)))


def _iso_pairs(series):
    return [(_iso_min(t), v) for t, v in series]


def build_long_rows(house: dict, ds: dict, *, join_tol_min: float,
                    baseline: bool, window_d: float | None = None):
    """Bouw de long-format rijen (één per (tijdstip, kamer)) + de kolom-metadata.

    Itereert de niet-overlappende 5-daagse vensters van `prepare_windows` (stride
    == venster → elk rooster-tijdstip in precies één gescoord venster). Uit
    ELKZELFDE venster-timeline komen zowel de features als — bij `baseline` — de
    fysica-voorspellingen, zodat ze per constructie exact op hetzelfde 15-min-
    tijdstip liggen. De warmup-aanloop (< venster-start) dient enkel om de trage
    massaknoop te laten inregelen en levert geen rij.

    Rebindt `am._NEIGHBOR_TEMP` per venster/model exact als de fit (`_win_res`):
    tweeling 1 met zijn eigen `neighbor_temp_estimate`, tweeling 2 met het
    `neighbor_anchor` dat `prepare_windows` al berekende.

    Geeft terug: (rows, room_ids, element_ids)."""
    actual = ds["actual"]
    actual_rh = ds["actual_rh"]
    heat_on = ds["heat_on"]
    rows_wx = ds["weather_rows"]
    room_ids = [rid for rid, r in house.get("rooms", {}).items()
                if r.get("from_window_data")]
    elem_meta = _element_meta(house)

    window_d = a2.BATCH_WINDOW_D if window_d is None else window_d
    wins = a2.prepare_windows(house, ds, window_d=window_d, stride_d=window_d)
    if not wins:
        return [], room_ids, sorted(elem_meta)

    # Stabiele element-kolommen: unie van de huis-elementen en alles wat ooit in
    # de log stond (m.u.v. de speciale niet-element-sleutels).
    specials = {am.AC_STATE_KEY, am.PAUSE_STATE_KEY}
    logged = {k for w in wins for step in w["timeline"]
              for k in step["states"] if k not in specials}
    element_ids = sorted(set(elem_meta) | logged)

    if baseline:
        p1 = am.merged_params(house, am.load_learned())
        p2 = a2.merged_params2(house, a2.load_learned2())

    tol_s = join_tol_min * 60.0
    rows = []
    seen_t = set()
    old_nb = am._NEIGHBOR_TEMP
    try:
        for w in wins:
            tl, seed, w_start, w_end = w["timeline"], w["seed"], w["start"], w["end"]
            pred1, pred2 = {}, {}
            if baseline:
                am._NEIGHBOR_TEMP = am.neighbor_temp_estimate(rows_wx, w_end)
                sim1 = am.simulate(house, p1, tl, seed, tm_seed=w.get("tm_seed"))
                pred1 = {rid: _sensor_series(house, tl, rid, ser)
                         for rid, ser in sim1["series"].items()}
                am._NEIGHBOR_TEMP = w["neighbor"]
                sim2 = a2.simulate2(house, p2, tl, seed, seed_w=w.get("seed_w"),
                                    tm_seed=w.get("tm_seed"))
                pred2 = {rid: _sensor_series(house, tl, rid, ser)
                         for rid, ser in sim2["series"].items()}

            for step in tl:
                t = step["t"]
                if t < w_start or t > w_end:
                    continue                       # sla de warmup-aanloop over
                iso = _iso_min(t)
                if iso in seen_t:
                    continue                       # venster-grens: één keer per tijdstip
                seen_t.add(iso)
                st = step["states"]
                wx = step.get("weather", {})
                direct = wx.get("direct") or 0.0
                diffuse = wx.get("diffuse") or 0.0
                ac_room = st.get(am.AC_STATE_KEY, "") or ""
                paused = 1 if _openness(st.get(am.PAUSE_STATE_KEY), {}) >= 0.5 else 0

                shared = {
                    "t": iso,
                    "t_epoch": int(t.timestamp()),
                    "t_out_c": _round(step.get("T_out")),
                    "rh_out": _round(wx.get("rh"), 1),
                    "wind_speed_ms": _round(wx.get("wind_speed"), 3),
                    "wind_dir_deg": _round(wx.get("wind_dir"), 1),
                    "gust_ms": _round(wx.get("gust"), 3),
                    "precip_mm": _round(wx.get("precip"), 3),
                    "solar_direct_wm2": _round(direct, 1),
                    "solar_diffuse_wm2": _round(diffuse, 1),
                    "solar_global_wm2": _round(direct + diffuse, 1),
                    "sun_az_deg": _round(step.get("sun_az"), 2),
                    "sun_el_deg": _round(step.get("sun_el"), 2),
                    "neighbor_anchor_c": _round(a2.neighbor_anchor(rows_wx, t), 2),
                    "paused": paused,
                    "ac_room": ac_room,
                }
                for eid in element_ids:
                    shared[f"open_{eid}"] = _round(
                        _openness(st.get(eid), elem_meta.get(eid, {})), 3)

                for rid in room_ids:
                    temp_hit = _nearest(actual.get(rid, []), t, tol_s)
                    rh_hit = _nearest(actual_rh.get(rid, []), t, tol_s)
                    temp_dt, temp_v = temp_hit
                    heating = 1 if (temp_dt is not None
                                    and temp_dt in heat_on.get(rid, set())) else 0
                    row = dict(shared)
                    row["room"] = rid
                    row["ac_here"] = 1 if ac_room == rid else 0
                    row["temp_c"] = _round(temp_v, 2)
                    row["humidity"] = _round(rh_hit[1], 1)
                    row["heating"] = heating
                    row["solar_glass_w"] = _round((step.get("irr") or {}).get(rid), 1)
                    row["roof_irr_w"] = _round((step.get("irr_roof") or {}).get(rid, 0.0), 1)
                    if baseline:
                        row["pred_twin1_c"] = pred1.get(rid, {}).get(iso)
                        row["pred_twin2_c"] = pred2.get(rid, {}).get(iso)
                    rows.append(row)
    finally:
        am._NEIGHBOR_TEMP = old_nb
    rows.sort(key=lambda r: (r["t"], r["room"]))
    return rows, room_ids, element_ids


def _round(v, n=2):
    return None if v is None else round(float(v), n)


def long_columns(element_ids, baseline: bool):
    cols = SHARED_META + ["room"] + TARGET_COLS + ["heating", "ac_here"] \
        + [c for c in HOUSE_STATE_COLS if c != "ac_here"] \
        + ["solar_glass_w", "roof_irr_w"] + WEATHER_COLS \
        + [f"open_{e}" for e in element_ids]
    if baseline:
        cols += BASELINE_COLS
    # Dedup met behoud van volgorde.
    seen, out = set(), []
    for c in cols:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def wide_columns(room_ids, element_ids, baseline: bool):
    per_room = TARGET_COLS + ROOM_DRIVER_COLS + ["ac_here"]
    if baseline:
        per_room += BASELINE_COLS
    cols = SHARED_META + WEATHER_COLS + ["paused", "ac_room"] \
        + [f"open_{e}" for e in element_ids]
    for rid in room_ids:
        cols += [f"{c}__{rid}" for c in per_room]
    return cols


def to_wide(long_rows, room_ids, element_ids, baseline: bool):
    """Pivotteer de long-rijen naar één rij per tijdstip (kamertemps als
    kolommen). Gedeelde kolommen worden één keer overgenomen."""
    per_room = TARGET_COLS + ROOM_DRIVER_COLS + ["ac_here"] \
        + (BASELINE_COLS if baseline else [])
    shared_keys = SHARED_META + WEATHER_COLS + ["paused", "ac_room"] \
        + [f"open_{e}" for e in element_ids]
    by_t = {}
    for r in long_rows:
        w = by_t.setdefault(r["t"], {k: r.get(k) for k in shared_keys})
        for c in per_room:
            w[f"{c}__{r['room']}"] = r.get(c)
    return [by_t[t] for t in sorted(by_t)]


def write_csv(path, rows, columns):
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow(r)


def try_write_parquet(out_dir, long_rows, long_cols, wide_rows, wide_cols):
    """Best-effort Parquet (compact + getypeerd). Alleen als pandas + pyarrow
    aanwezig zijn — anders vriendelijk overslaan (CSV + schema blijven het
    stdlib-product)."""
    try:
        import pandas as pd  # noqa: PLC0415
        import pyarrow       # noqa: F401,PLC0415
    except ImportError:
        print("[export] pandas/pyarrow niet gevonden → Parquet overgeslagen "
              "(pip install pandas pyarrow voor .parquet).")
        return
    pd.DataFrame(long_rows, columns=long_cols).to_parquet(
        os.path.join(out_dir, "ventilation_long.parquet"), index=False)
    pd.DataFrame(wide_rows, columns=wide_cols).to_parquet(
        os.path.join(out_dir, "ventilation_wide.parquet"), index=False)
    print("[export] Parquet geschreven (long + wide).")


def build_schema(room_ids, element_ids, baseline: bool, elem_meta: dict):
    """Data-dictionary: kolom → {betekenis, eenheid}."""
    d = {
        "t": {"desc": "tijdstip (lokaal, Europe/Amsterdam), 15-min raster", "unit": "ISO8601"},
        "t_epoch": {"desc": "tijdstip als unix-seconden", "unit": "s"},
        "room": {"desc": "kamer-id (alleen long-format)", "unit": "id"},
        "temp_c": {"desc": "GEMETEN kamertemp (tado) — het doelwit", "unit": "°C"},
        "humidity": {"desc": "gemeten relatieve vochtigheid (tado)", "unit": "%RH"},
        "heating": {"desc": "tado meldt actieve verwarming in deze kamer op dit moment "
                            "(de tweeling laat zulke samples uit de kalibratie vallen)",
                    "unit": "0/1"},
        "paused": {"desc": "huis-breed handmatig gepauzeerd (log onbetrouwbaar)", "unit": "0/1"},
        "ac_room": {"desc": "kamer met de mobiele airco op dit moment ('' = geen)", "unit": "id"},
        "ac_here": {"desc": "de mobiele airco staat in DEZE kamer", "unit": "0/1"},
        "solar_glass_w": {"desc": "instraling door het glas in deze kamer (som over de ramen, "
                                  "hoek-afhankelijke transmissie)", "unit": "W"},
        "roof_irr_w": {"desc": "horizontale dak-instraling (alleen bovenverdieping-kamers)",
                       "unit": "W/m²"},
        "t_out_c": {"desc": "buitentemp (Open-Meteo, per 15-min geïnterpoleerd)", "unit": "°C"},
        "rh_out": {"desc": "buiten relatieve vochtigheid", "unit": "%RH"},
        "wind_speed_ms": {"desc": "windsnelheid op 10 m", "unit": "m/s"},
        "wind_dir_deg": {"desc": "windrichting (uit)", "unit": "° vanaf noord"},
        "gust_ms": {"desc": "windstoot", "unit": "m/s"},
        "precip_mm": {"desc": "neerslag in het uur", "unit": "mm"},
        "solar_direct_wm2": {"desc": "directe (beam) instraling op horizontaal vlak", "unit": "W/m²"},
        "solar_diffuse_wm2": {"desc": "diffuse instraling op horizontaal vlak", "unit": "W/m²"},
        "solar_global_wm2": {"desc": "globaal = direct + diffuse", "unit": "W/m²"},
        "sun_az_deg": {"desc": "zon-azimut (met de klok mee vanaf noord)", "unit": "°"},
        "sun_el_deg": {"desc": "zon-elevatie boven de horizon", "unit": "°"},
        "neighbor_anchor_c": {"desc": "party-muur-buur-anker (gekapt/nacht-verlaagd, tweeling 2)",
                              "unit": "°C"},
    }
    for eid in element_ids:
        e = elem_meta.get(eid, {})
        if not e:
            kind = "element"
        elif "between" in e:
            kind = "door"
        elif e.get("glass_m2") is not None:
            kind = "window"
        else:
            kind = "vent"
        d[f"open_{eid}"] = {
            "desc": f"openingsfractie van {e.get('label', eid)} "
                    f"(0=dicht, 1=open, kier={e.get('tilt_frac', 0.3)})",
            "unit": "0..1", "kind": kind}
    if baseline:
        d["pred_twin1_c"] = {"desc": "voorspelling grey-box tweeling 1 (2-knoops RC), "
                                     "sensor-ruimte, hergeseed per 5-daags venster + 24u warmup — "
                                     "ruwe fysica, GEEN online tarrering", "unit": "°C"}
        d["pred_twin2_c"] = {"desc": "voorspelling grey-box tweeling 2 (3-knoops RC + vocht), "
                                     "sensor-ruimte, idem — de baseline om te verslaan / "
                                     "residu-doel", "unit": "°C"}
    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "rooms": room_ids,
        "elements": element_ids,
        "notes": [
            "Volledig offline gebouwd uit data/twin2_history/*.json (geen secrets, geen API).",
            "temp_c/humidity zijn de GEMETEN doelwaarden; leeg = geen sample binnen join-tol.",
            "Filter net als de tweeling: laat rijen met heating==1, ac_here==1 of paused==1 vallen "
            "wil je een schone fit op de ventilatie-fysica.",
            "pred_twin*_c zijn optioneel (--baseline) en dienen als fysica-baseline / residu-doel.",
        ],
        "columns": d,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Exporteer ML-dataset voor de ventilatie-tweeling.")
    ap.add_argument("--out", default=os.path.join(_ROOT, "data", "ml"),
                    help="uitvoermap (default data/ml)")
    ap.add_argument("--history-dir", default=None,
                    help="shard-map (default airflow2_model.HISTORY_DIR / data/twin2_history)")
    ap.add_argument("--join-tol-min", type=float, default=10.0,
                    help="max. minuten tussen rooster-tijdstip en gemeten sample (default 10)")
    ap.add_argument("--window-days", type=float, default=None,
                    help="lengte van het hergeseed-venster voor de baseline-sims "
                         "(default 5; kleiner = vaker hergeseed, kortere-horizon-skill)")
    bl = ap.add_mutually_exclusive_group()
    bl.add_argument("--baseline", dest="baseline", action="store_true", default=True,
                    help="voeg tweeling-1/2-voorspellingen toe (default aan)")
    bl.add_argument("--no-baseline", dest="baseline", action="store_false",
                    help="alleen data, geen fysica-baseline (sneller)")
    args = ap.parse_args()

    if args.history_dir:
        a2.HISTORY_DIR = args.history_dir

    house = am.load_house()
    loc = house.get("location", {})
    am._LAT = loc.get("lat", am._LAT)
    am._LON = loc.get("lon", am._LON)

    ds = a2.load_dataset(house)
    n_samples = sum(len(s) for s in ds["actual"].values())
    print(f"[export] {n_samples} tado-samples, {len(ds['weather_rows'])} weer-uren, "
          f"{len(ds['log'])} log-snapshots uit {a2.HISTORY_DIR}.")
    if not n_samples:
        print("[export] geen data in de shards — niets te exporteren.")
        return 1

    long_rows, room_ids, element_ids = build_long_rows(
        house, ds, join_tol_min=args.join_tol_min, baseline=args.baseline,
        window_d=args.window_days)
    wide_rows = to_wide(long_rows, room_ids, element_ids, args.baseline)
    long_cols = long_columns(element_ids, args.baseline)
    wide_cols = wide_columns(room_ids, element_ids, args.baseline)

    os.makedirs(args.out, exist_ok=True)
    write_csv(os.path.join(args.out, "ventilation_long.csv"), long_rows, long_cols)
    write_csv(os.path.join(args.out, "ventilation_wide.csv"), wide_rows, wide_cols)
    schema = build_schema(room_ids, element_ids, args.baseline, _element_meta(house))
    with open(os.path.join(args.out, "schema.json"), "w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False, indent=2)
    try_write_parquet(args.out, long_rows, long_cols, wide_rows, wide_cols)

    labeled = sum(1 for r in long_rows if r.get("temp_c") is not None)
    print(f"[export] {len(long_rows)} long-rijen ({labeled} met doel-temp), "
          f"{len(wide_rows)} wide-rijen, {len(room_ids)} kamers, "
          f"{len(element_ids)} elementen → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

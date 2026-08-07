#!/usr/bin/env python3
"""Weerstation-nauwkeurigheidsanalyse (Project 7 — diagnostiek).

Vergelijkt het WU PWS *uurgemiddelde* temperatuur tegen Open-Meteo ERA5
(reanalyse-archief) voor dezelfde locatie en periode, en stratificeert de
afwijking (bias = WU − model) naar tijd-van-dag, bewolking/zon en wind. Doel:
kwantificeren *of* en *wanneer* het station te warm leest — de klassieke
stralings-/plaatsingsfout op zonnige (laat-)middagen.

Methodologie (zie ook CLAUDE.md):
  - Referentie = Open-Meteo ERA5. Dit is een grid-schaal model, GEEN absolute
    waarheid. Een ruwe WU−model-kloof mengt (a) sensorfout en (b) écht lokaal
    microklimaat. We scheiden die door te kijken hóe de kloof zich gedraagt:
      * stralingsfout  → schaalt met instraling, erger bij weinig wind,
                          verdwijnt 's nachts (zon-gedreven).
      * plaatsing/thermische massa → loopt achter, houdt aan in de vroege
                          avond (muren/bestrating stralen na) → "eind middag".
      * constante offset → dag én nacht even groot (ijking).

Read-only t.o.v. alle andere projecten: importeert niets, raakt data.json /
window_data.json / de Gist niet aan. Schrijft alleen docs/accuracy_data.json
(dashboard-artefact). Draait via manual-dispatch.

VEILIGHEID: WU_STATION_ID is een secret en wordt NOOIT naar de (publieke)
JSON of logs geschreven.
"""

from __future__ import annotations

import glob
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

import bias_eval
import knmi_ref
import neighbour_pws
from http_util import get_json
from notify import sanitize_error, send_telegram
from shared_const import LATITUDE as UTRECHT_LAT, LONGITUDE as UTRECHT_LON, TZ, local_today
from wu_bias import SOLAR_BIAS_SLOPE

UTC = timezone.utc

# Stratificatie-grenzen
AFTERNOON = range(13, 19)        # lokale uren 13:00–18:59 ("eind middag")
SUNNY_CLOUD_MAX = 25.0           # bewolking < 25% telt als "zonnig"
OVERCAST_CLOUD_MIN = 75.0        # > 75% telt als "bewolkt"
SOLAR_BINS = [(0, 1), (1, 200), (200, 400), (400, 600), (600, 10000)]
WIND_BINS = [(0, 5), (5, 10), (10, 20), (20, 1000)]   # km/h @10m

OUT_PATH = "docs/accuracy_data.json"
SCATTER_CAP = 1500               # max punten in JSON (dashboard scatter)

# Maand-shards met de gekoppelde uren (zelfde patroon als data/twin2_history).
# accuracy_data.json is een *momentopname* van het laatste venster en wordt elke
# run overschreven; modelvorm-vragen (helling per seizoen, wind-/geometrieterm,
# validatie op een vooruit-geschoven venster) hebben juist een groeiende reeks
# nodig. Zonder archief gooit elke run de vorige steekproef weg.
HISTORY_DIR = os.getenv("STATION_HISTORY_DIR", "data/station_history")
SHARD_SCHEMA = 1


# =============================================================================
# FETCH
# =============================================================================

def fetch_wu_hourly(station_id: str, api_key: str, days: int) -> Dict[str, dict]:
    """WU PWS uur-historie, per dag opgehaald. Returnt {utc_hour_key: {...}}
    met utc_hour_key = 'YYYY-MM-DDTHH'. Velden: temp (°C), wind (km/h @10m),
    rh (%), solar (W/m², kan None zijn als station geen zonsensor heeft)."""
    today = local_today()
    out: Dict[str, dict] = {}
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        url = (
            "https://api.weather.com/v2/pws/history/hourly"
            f"?stationId={station_id}&format=json&units=m"
            f"&date={d.strftime('%Y%m%d')}&numericPrecision=decimal&apiKey={api_key}"
        )
        # Kleine retry per dag (buiten http_util: de apiKey zit in de URL-string).
        # Zonder retry laat een transiente 5xx die dag stilletjes uit de kalibratie-
        # steekproef vallen — en dit script ís de kalibratiebron voor wu_bias.
        r = None
        for attempt, pause in ((1, 2), (2, 5), (3, 0)):
            try:
                r = requests.get(url, timeout=20)
                if r.status_code == 200:
                    break
                print(f"[WU] {d} status {r.status_code} (poging {attempt})")
            except Exception as e:
                # sanitize_error: de WU-URL bevat apiKey — nooit rauw printen.
                print(f"[WU] {d} poging {attempt} failed: {sanitize_error(e)}")
                r = None
            if pause:
                time.sleep(pause)
        if r is None or r.status_code != 200:
            continue
        try:
            for obs in r.json().get("observations", []):
                ts = obs.get("obsTimeUtc")
                if not ts:
                    continue
                key = ts[:13]                       # 'YYYY-MM-DDTHH'
                m = obs.get("metric", {}) or {}
                temp = m.get("tempAvg")
                if temp is None:
                    continue
                out[key] = {
                    "temp": temp,
                    "wind": obs.get("windspeedAvg"),
                    "rh": obs.get("humidityAvg"),
                    "solar": obs.get("solarRadiationHigh"),
                }
        except Exception as e:
            # sanitize_error: de WU-URL bevat apiKey — nooit rauw printen.
            print(f"[WU] {d} failed: {sanitize_error(e)}")
    print(f"[WU] {len(out)} uurwaarnemingen over {days} dagen")
    return out


def fetch_om_archive_hourly(start: str, end: str) -> Dict[str, dict]:
    """Open-Meteo ERA5-archief, uurlijks, in UTC. Returnt {utc_hour_key: {...}}
    met temp (°C), solar (shortwave W/m²), cloud (%), wind (km/h @10m)."""
    params = {
        "latitude": UTRECHT_LAT,
        "longitude": UTRECHT_LON,
        "start_date": start,
        "end_date": end,
        "hourly": "temperature_2m,shortwave_radiation,cloud_cover,wind_speed_10m",
        "timezone": "UTC",
    }
    j = get_json("https://archive-api.open-meteo.com/v1/archive", params,
                 timeout=30, label="om-archive")
    h = j.get("hourly", {})
    times = h.get("time", [])
    temp = h.get("temperature_2m", [])
    solar = h.get("shortwave_radiation", [])
    cloud = h.get("cloud_cover", [])
    wind = h.get("wind_speed_10m", [])
    out: Dict[str, dict] = {}
    for i, t in enumerate(times):
        tv = temp[i] if i < len(temp) else None
        if tv is None:
            continue                                # ERA5T-gat (laatste ~5 dgn)
        out[t[:13]] = {
            "temp": tv,
            "solar": solar[i] if i < len(solar) else None,
            "cloud": cloud[i] if i < len(cloud) else None,
            "wind": wind[i] if i < len(wind) else None,
        }
    print(f"[OM] {len(out)} uurwaarnemingen ({start} … {end})")
    return out


# =============================================================================
# PAIR
# =============================================================================

def pair_hours(wu: Dict[str, dict], om: Dict[str, dict]) -> List[dict]:
    """Koppelt op gemeenschappelijk UTC-uur. bias = WU − model."""
    rows = []
    for key in sorted(set(wu) & set(om)):
        w, o = wu[key], om[key]
        local = datetime.fromisoformat(key + ":00:00").replace(tzinfo=UTC).astimezone(TZ)
        rows.append({
            "t": key,
            "hour": local.hour,
            "wu": w["temp"],
            "om": o["temp"],
            "bias": w["temp"] - o["temp"],
            "solar": o.get("solar"),         # Open-Meteo (grid-schaal) instraling
            "wu_solar": w.get("solar"),      # WU eigen pyranometer (lokaal, co-located)
            "cloud": o.get("cloud"),
            "wind": o.get("wind"),
            "rh": w.get("rh"),               # alleen voor het archief, niet gestratificeerd
        })
    return rows


# =============================================================================
# ARCHIEF (maand-shards — de groeiende kalibratie-/validatieset)
# =============================================================================

ARCHIVE_FIELDS = ("wu", "om", "knmi", "solar", "wu_solar", "cloud", "wind", "rh")


def _shard_path(month: str) -> str:
    return os.path.join(HISTORY_DIR, f"{month}.json")


def load_shard(month: str) -> dict:
    try:
        with open(_shard_path(month), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"schema": SHARD_SCHEMA, "month": month,
                "reference": "open-meteo ERA5 archive", "rows": []}


def append_archive(rows: List[dict]) -> Tuple[int, int]:
    """Voeg de gekoppelde uren toe aan `data/station_history/<YYYY-MM>.json`.

    Idempotent op het UTC-uur: een tweede run over hetzelfde venster werkt de rij
    bij i.p.v. te stapelen (ERA5 herziet zijn archief soms). `bias` wordt niet
    opgeslagen — die is `wu − om` en zou bij een herziening uit de pas kunnen gaan
    lopen met zijn eigen operanden. Geeft (nieuw, bijgewerkt) terug.

    VEILIGHEID: hier gaat alleen meetdata in, nooit WU_STATION_ID.
    """
    by_month: Dict[str, dict] = {}
    fresh = updated = 0
    for r in rows:
        month = r["t"][:7]
        if month not in by_month:
            by_month[month] = load_shard(month)
        shard = by_month[month]
        index = {row["t"]: row for row in shard["rows"]}
        row = {"t": r["t"], **{k: r.get(k) for k in ARCHIVE_FIELDS}}
        if r["t"] in index:
            if index[r["t"]] != row:
                index[r["t"]].update(row)
                updated += 1
        else:
            shard["rows"].append(row)
            fresh += 1
    os.makedirs(HISTORY_DIR, exist_ok=True)
    for shard in by_month.values():
        shard["rows"].sort(key=lambda row: row["t"])
        with open(_shard_path(shard["month"]), "w", encoding="utf-8") as f:
            json.dump(shard, f, ensure_ascii=False, separators=(",", ":"))
    return fresh, updated


def load_archive() -> List[dict]:
    """Alle gearchiveerde uren over alle shards, op tijd gesorteerd, met `bias`
    en `hour` (lokaal) opnieuw afgeleid — de vorm die `analyse()` verwacht."""
    out: Dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                shard = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for row in shard.get("rows", []):
            if row.get("wu") is None or row.get("om") is None:
                continue
            local = datetime.fromisoformat(row["t"] + ":00:00").replace(tzinfo=UTC).astimezone(TZ)
            out[row["t"]] = {**row, "bias": row["wu"] - row["om"], "hour": local.hour}
    return [out[k] for k in sorted(out)]


# =============================================================================
# ANALYSE
# =============================================================================

def attach_knmi(rows: List[dict], knmi_hours: Dict[str, dict]) -> int:
    """Hang de KNMI-referentietemperatuur aan de gekoppelde uren (`knmi`-kolom).

    Additief en niet-fataal: ontbreekt de bron, dan blijft `knmi` None en draait
    alles ongewijzigd door op ERA5. De stratificatie hieronder blijft bewust op
    ERA5 staan — dit veld gaat het archief in zodat `bias_eval`/`tools/
    bias_backtest.py` er tegen kan scoren (`--reference knmi`).

    Gemeten op de eerste 1437 gekoppelde uren (apr–jun 2026): dezelfde gemiddelde
    bias als ERA5 (+0,88 °C, twee onafhankelijke referenties die het eens zijn),
    maar sd 1,15 vs 1,42 — ~0,27 °C van wat als stationsruis werd gefit, was
    ERA5's eigen gridfout.
    """
    hit = 0
    for row in rows:
        temp = (knmi_hours.get(row["t"]) or {}).get("temp")
        row["knmi"] = temp
        hit += temp is not None
    return hit


def reference_stats(rows: List[dict]) -> List[dict]:
    """Bias-statistiek van het station tegen élke beschikbare referentie.

    De **sd** is de kolom die telt, niet de bias: een verschil in gemiddelde is
    over 4,1 km deels écht microklimaat, maar de spreiding is de ruisvloer waar
    elke hellingfit tegenaan loopt. KNMI scoorde daar 1,15 vs ERA5's 1,42.
    """
    out = []
    for key, label in (("om", "Open-Meteo ERA5"), ("knmi", "KNMI De Bilt (260)")):
        diffs = [r["wu"] - r[key] for r in rows
                 if r.get("wu") is not None and r.get(key) is not None]
        if not diffs:
            continue
        n = len(diffs)
        mean = sum(diffs) / n
        out.append({
            "key": key, "label": label, "n": n, "bias": round(mean, 3),
            "rmse": round(math.sqrt(sum(d * d for d in diffs) / n), 3),
            "sd": round(math.sqrt(sum((d - mean) ** 2 for d in diffs) / n), 3),
        })
    return out


def archive_summary(rows: List[dict]) -> dict:
    """Omvang van de gróeiende reeks — het gepubliceerde venster is maar een
    momentopname, en het dashboard hoort te tonen waar het bewijs op rust."""
    if not rows:
        return {"n": 0, "months": [], "start": None, "end": None}
    months = sorted({r["t"][:7] for r in rows})
    return {"n": len(rows), "months": months,
            "start": rows[0]["t"][:10], "end": rows[-1]["t"][:10],
            "n_knmi": sum(1 for r in rows if r.get("knmi") is not None),
            "n_wu_solar": sum(1 for r in rows if r.get("wu_solar") is not None)}


NEIGHBOUR_DAYS = 30      # eigen venster; zie de rate-limit-noot in neighbour_coherence


def neighbour_coherence(api_key: str, start: str, end: str, days: int,
                        reference: Dict[str, dict]) -> Optional[dict]:
    """Draait de buur-PWS-coherentietoets mee in de maandelijkse run.

    Waarom hier en niet in een losse probe: het antwoord ("is de warme, windstille
    nacht van ons of van de buurt?") bepaalt of de correctie een interceptterm mág
    hebben, en dat is een blijvende vraag die elk seizoen opnieuw gesteld hoort te
    worden — 's winters kan een hitte-eiland zich anders gedragen. Meeliften op de
    maandcron geeft die herhaling gratis; een handmatige probe zou het bij één
    zomermeting laten.

    Niet-fataal en volledig optioneel: zonder `WU_NEIGHBOUR_IDS` gebeurt er niets.
    VEILIGHEID: station-id's worden nooit in het artefact of de log geschreven.
    """
    ids = neighbour_pws.neighbour_ids()
    if not (ids and reference):
        return None
    # Eigen, korter venster. `fetch_wu_hourly` doet één call per dag per station, dus
    # de coherentietoets vermenigvuldigt het WU-verkeer met (1 + aantal buren): een
    # 120-daagse run met drie buren is 480 calls. Dat blijkt in de praktijk te lopen
    # (de eerste zo'n run was in ~2 min klaar, geen 429's) — dit is dus een
    # voorzorg, geen reparatie van een waargenomen storing. Maar 480 calls voor een
    # vraag die met 30 dagen ruim beantwoord is, is verspilling die bij een groeiend
    # archief alleen maar toeneemt: de 45-daagse eerste meting gaf 0,4 °C verschil
    # tussen ons en de warmste buur, ver boven de ruis. Het lange venster blijft
    # onverkort gelden voor het archief zelf.
    window = min(days, NEIGHBOUR_DAYS)
    ours = neighbour_pws.profile(neighbour_pws.pair_with_reference(
        fetch_wu_hourly(os.environ.get("WU_STATION_ID", ""), api_key, window), reference))
    others = []
    for idx, sid in enumerate(ids, 1):
        paired = neighbour_pws.pair_with_reference(fetch_wu_hourly(sid, api_key, window),
                                                   reference)
        if not paired:
            print(f"[buur] buur {idx}: geen gekoppelde uren — overgeslagen")
            continue
        others.append({"label": f"buur {idx}", **neighbour_pws.profile(paired)})
    if not others:
        return None
    code, uitleg = neighbour_pws.verdict(ours, others)
    print(f"[buur] {code.upper()}: {uitleg}")
    return {"verdict": code, "explanation": uitleg,
            "window": {"start": start, "end": end, "days": window},
            "ours": {"label": "ons station", **ours}, "others": others,
            "spread_night": neighbour_pws.spread(others, "night_bias"),
            "spread_slope": neighbour_pws.spread(others, "slope_per_100")}


def _agg(rows: List[dict]) -> dict:
    """Mean bias, RMSE, n, en (waar zinnig) correlatie WU↔model."""
    biases = [r["bias"] for r in rows]
    n = len(biases)
    if n == 0:
        return {"n": 0, "mean_bias": None, "rmse": None}
    mean = sum(biases) / n
    rmse = math.sqrt(sum(b * b for b in biases) / n)
    out = {"n": n, "mean_bias": round(mean, 2), "rmse": round(rmse, 2)}
    if n >= 3:
        out["corr"] = round(_pearson([r["wu"] for r in rows],
                                      [r["om"] for r in rows]), 3)
    return out


def _pearson(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def _slope(rows: List[dict], xkey: str) -> Optional[float]:
    """Least-squares helling van bias t.o.v. xkey (None-waarden overgeslagen)."""
    pts = [(r[xkey], r["bias"]) for r in rows if r.get(xkey) is not None]
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx == 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    return sxy / sxx


def _bias_corr(rows: List[dict], xkey: str) -> Tuple[Optional[float], int]:
    """Pearson-correlatie tussen xkey en bias (None-waarden overgeslagen).
    Returnt (r, n). Hogere |r| = strakker verband → betere correctie-driver."""
    pts = [(r[xkey], r["bias"]) for r in rows if r.get(xkey) is not None]
    n = len(pts)
    if n < 3:
        return None, n
    return _pearson([p[0] for p in pts], [p[1] for p in pts]), n


def _recommend_slope(om_slope: Optional[float], om_corr: Optional[float],
                     wu_slope: Optional[float], wu_corr: Optional[float]) -> Optional[dict]:
    """Kies de correctie-driver: het WU-station (lokaal, co-located) als zijn
    bias↔instraling-correlatie minstens zo strak is als Open-Meteo, anders OM.
    Returnt {driver, slope_per_wm2, slope_per_100} om in wu_bias.py te zetten."""
    have_wu = wu_slope is not None and wu_corr is not None
    have_om = om_slope is not None and om_corr is not None
    if have_wu and (not have_om or abs(wu_corr) >= abs(om_corr)):
        driver, slope = "wu", wu_slope
    elif have_om:
        driver, slope = "om", om_slope
    elif have_wu:
        driver, slope = "wu", wu_slope
    else:
        return None
    return {"driver": driver, "slope_per_wm2": round(slope, 5),
            "slope_per_100": round(slope * 100, 3)}


def analyse(rows: List[dict]) -> dict:
    diurnal = []
    for hr in range(24):
        diurnal.append({"hour": hr, **_agg([r for r in rows if r["hour"] == hr])})

    def cloud_subset(lo, hi):
        return [r for r in rows if r.get("cloud") is not None and lo <= r["cloud"] < hi]

    by_cloud = {
        "sunny":    _agg(cloud_subset(0, SUNNY_CLOUD_MAX)),
        "partly":   _agg(cloud_subset(SUNNY_CLOUD_MAX, OVERCAST_CLOUD_MIN)),
        "overcast": _agg(cloud_subset(OVERCAST_CLOUD_MIN, 1000)),
    }
    by_solar = [{"label": f"{lo}–{hi if hi < 9999 else '∞'}", "lo": lo, "hi": hi,
                 **_agg([r for r in rows if r.get("solar") is not None and lo <= r["solar"] < hi])}
                for lo, hi in SOLAR_BINS]
    by_wind = [{"label": f"{lo}–{hi if hi < 999 else '∞'}", "lo": lo, "hi": hi,
                **_agg([r for r in rows if r.get("wind") is not None and lo <= r["wind"] < hi])}
               for lo, hi in WIND_BINS]

    sunny_pm = [r for r in rows if r["hour"] in AFTERNOON
                and r.get("cloud") is not None and r["cloud"] < SUNNY_CLOUD_MAX]
    rest = [r for r in rows if r not in sunny_pm]

    solar_slope = _slope(rows, "solar")
    wind_slope = _slope(rows, "wind")

    # Twee kandidaat-drivers voor de temperatuur-correctie: Open-Meteo (grid)
    # vs. het WU-station zelf (lokale, co-located pyranometer). De helling is
    # alleen geldig t.o.v. de as waarop ze gefit is, dus we fitten beide en
    # vergelijken de correlatie — de strakste driver wint (zie wu_bias.py).
    wu_solar_slope = _slope(rows, "wu_solar")
    solar_corr, solar_corr_n = _bias_corr(rows, "solar")
    wu_solar_corr, wu_solar_corr_n = _bias_corr(rows, "wu_solar")

    # scatter (gedownsampled) voor het dashboard. `wu_solar` is additief meegenomen:
    # de scatter droeg alleen de Open-Meteo-instraling, terwijl de correctie in
    # productie op de WU-pyranometer draait — elke modelvorm-analyse op dit artefact
    # toetste dus een andere as dan er gebruikt wordt.
    step = max(1, len(rows) // SCATTER_CAP)
    scatter = [{"t": r["t"], "h": r["hour"], "bias": round(r["bias"], 2),
                "solar": r["solar"], "wu_solar": r.get("wu_solar"),
                "cloud": r["cloud"], "wind": r["wind"],
                "wu": round(r["wu"], 1), "om": round(r["om"], 1)}
               for r in rows[::step]]

    return {
        "overall": _agg(rows),
        "diurnal": diurnal,
        "by_cloud": by_cloud,
        "by_solar": by_solar,
        "by_wind": by_wind,
        "sunny_afternoon": {**_agg(sunny_pm), "window": "13–19u, <25% bewolking"},
        "rest": _agg(rest),
        "solar_slope_per_100": round(solar_slope * 100, 3) if solar_slope is not None else None,
        "wind_slope": round(wind_slope, 3) if wind_slope is not None else None,
        "wu_solar_slope_per_100": round(wu_solar_slope * 100, 3) if wu_solar_slope is not None else None,
        "solar_bias_corr": round(solar_corr, 3) if solar_corr is not None else None,
        "wu_solar_bias_corr": round(wu_solar_corr, 3) if wu_solar_corr is not None else None,
        "wu_solar_n": wu_solar_corr_n,
        # Aanbevolen coëfficiënt voor wu_bias.SOLAR_BIAS_SLOPE (°C per W/m²):
        # de WU-driver als die minstens zo strak is als Open-Meteo, anders OM.
        "recommended_slope": _recommend_slope(solar_slope, solar_corr,
                                              wu_solar_slope, wu_solar_corr),
        "scatter": scatter,
    }


# =============================================================================
# REPORT (stdout — zichtbaar in de Action-log)
# =============================================================================

def build_report(stats: dict) -> str:
    o = stats["overall"]
    pm = stats["sunny_afternoon"]
    rest = stats["rest"]
    L = []
    L.append("═" * 56)
    L.append("  WEERSTATION-NAUWKEURIGHEID  (WU − Open-Meteo ERA5)")
    L.append("═" * 56)
    L.append(f"  Gekoppelde uren : {o['n']}")
    L.append(f"  Gem. afwijking  : {o['mean_bias']:+.2f} °C   (RMSE {o['rmse']:.2f})")
    if "corr" in o:
        L.append(f"  Correlatie      : r = {o['corr']}")
    L.append("")
    L.append(f"  ➤ ZONNIGE EIND-MIDDAG ({pm['window']})")
    if pm["n"]:
        L.append(f"      {pm['mean_bias']:+.2f} °C over {pm['n']} uren  (RMSE {pm['rmse']:.2f})")
    else:
        L.append("      (geen waarnemingen in dit venster)")
    if rest["n"]:
        L.append(f"      rest van de tijd: {rest['mean_bias']:+.2f} °C over {rest['n']} uren")
    L.append("")
    L.append("  BIAS PER UUR (lokaal)")
    for d in stats["diurnal"]:
        if not d["n"]:
            continue
        bar = "█" * min(20, int(abs(d["mean_bias"]) / 0.25))
        L.append(f"   {d['hour']:02d}u  {d['mean_bias']:+5.2f}  {bar}  (n={d['n']})")
    L.append("")
    L.append("  BIAS vs BEWOLKING")
    for k, lbl in (("sunny", "zonnig  <25%"), ("partly", "half  25–75%"),
                   ("overcast", "bewolkt >75%")):
        c = stats["by_cloud"][k]
        if c["n"]:
            L.append(f"   {lbl:14s} {c['mean_bias']:+.2f} °C  (n={c['n']})")
    L.append("")
    L.append("  BIAS vs INSTRALING (W/m²)")
    for b in stats["by_solar"]:
        if b["n"]:
            L.append(f"   {b['label']:>9s}  {b['mean_bias']:+.2f} °C  (n={b['n']})")
    if stats["solar_slope_per_100"] is not None:
        L.append(f"   → helling: {stats['solar_slope_per_100']:+.3f} °C per 100 W/m²")
    L.append("")
    L.append("  BIAS vs WIND (km/h @10m)")
    for b in stats["by_wind"]:
        if b["n"]:
            L.append(f"   {b['label']:>8s}  {b['mean_bias']:+.2f} °C  (n={b['n']})")
    if stats["wind_slope"] is not None:
        L.append(f"   → helling: {stats['wind_slope']:+.3f} °C per km/h")
    L.append("")
    L.append("  CORRECTIE-DRIVER  (welke instraling stuurt de bias het strakst?)")
    om_c, wu_c = stats.get("solar_bias_corr"), stats.get("wu_solar_bias_corr")
    om_s, wu_s = stats.get("solar_slope_per_100"), stats.get("wu_solar_slope_per_100")
    L.append(f"   Open-Meteo : helling {om_s if om_s is not None else '—'} °C/100W/m²"
             f"   corr(bias) {om_c if om_c is not None else '—'}")
    L.append(f"   WU-station : helling {wu_s if wu_s is not None else '—'} °C/100W/m²"
             f"   corr(bias) {wu_c if wu_c is not None else '—'}  (n={stats.get('wu_solar_n', 0)})")
    rec = stats.get("recommended_slope")
    if rec:
        L.append(f"   → aanbevolen driver: {rec['driver'].upper()}  "
                 f"({rec['slope_per_100']:+.3f} °C/100W/m²)")
        L.append(f"   → zet  SOLAR_BIAS_SLOPE = {rec['slope_per_wm2']}  in wu_bias.py")
    L.append("═" * 56)
    return "\n".join(L)


# =============================================================================
# HERIJKINGSSIGNAAL (fase 4)
# =============================================================================

# Drempel voor "de helling is meetbaar verschoven", uitgedrukt als het
# temperatuurverschil dat de oude en de nieuwe helling geven bij REFERENCE_WM2 —
# een zonnige middag. Een helling in °C per W/m² zegt niks in het bericht; "op een
# zonnige middag scheelt dit 0,4 °C" wel.
REFERENCE_WM2 = 600.0
DRIFT_ALERT_C = 0.30


def recalibration_signal(rows: List[dict]) -> Optional[dict]:
    """Is er *bewijs* dat `wu_bias.SOLAR_BIAS_SLOPE` herijkt moet worden?

    Bewust niet "is de fit veranderd" — dat is hij altijd. De poort is het
    evaluatieprotocol (`bias_eval`): de heringestelde helling moet de uitgerolde
    constante verslaan op forward-chaining, in élke fold, én boven de A/A-ruisvloer
    uitkomen. Pas dan is er iets te melden. Zo wordt de maandelijkse run een
    uitzonderingsmelding in plaats van een tikkende teller.

    Referentie en driver worden gekozen op wat het archief draagt: KNMI boven ERA5
    (19% minder spreiding, zie de KNMI-notitie) en de eigen pyranometer boven het
    Open-Meteo-grid (dat is de as waarop de correctie in productie draait).
    Geeft None als er niets te melden is.
    """
    reference = "knmi" if any(r.get("knmi") is not None for r in rows) else "om"
    driver = "wu_solar" if any(r.get("wu_solar") is not None for r in rows) else "solar"
    prepared = bias_eval.prepare(rows, reference=reference, driver=driver)
    features = bias_eval.MODELS["helling (huidige vorm)"]
    sub = bias_eval.usable(prepared, features)
    if not sub:
        return None
    folds = bias_eval.forward_chain(sub, features)
    incumbent = [(m, bias_eval.rmse([r for r in sub if r["month"] == m],
                                     bias_eval.deployed_predictor), n) for m, _, n in folds]
    ok, reasons = bias_eval.accept(folds, incumbent, bias_eval.aa_floor(sub, features))
    slope = bias_eval.fit(sub, features)[0]
    delta_c = abs(slope - SOLAR_BIAS_SLOPE) * REFERENCE_WM2
    if not ok or delta_c < DRIFT_ALERT_C:
        return None
    return {"slope": slope, "deployed": SOLAR_BIAS_SLOPE, "delta_c": delta_c,
            "reference": reference, "driver": driver, "n": len(sub),
            "months": sorted({r["month"] for r in sub}), "reasons": reasons}


def protocol_report(rows: List[dict], signal: Optional[dict]) -> str:
    """Het protocol-oordeel in de Action-log — elke run, ook als er niets te melden is."""
    reference = "knmi" if any(r.get("knmi") is not None for r in rows) else "om"
    driver = "wu_solar" if any(r.get("wu_solar") is not None for r in rows) else "solar"
    lines = [f"\nEVALUATIEPROTOCOL (referentie={reference}, driver={driver})",
             "-" * 62]
    if signal:
        lines += [f"⚠️  De helling kan herijkt worden: {SOLAR_BIAS_SLOPE:.5f} → {signal['slope']:.5f}",
                  f"    ({signal['delta_c']:.2f} °C verschil bij {REFERENCE_WM2:.0f} W/m²; "
                  f"haalt alle poorten op {len(signal['months'])} maanden)",
                  "    Zet de waarde met de hand in wu_bias.py — zie de kalibratienoot daar."]
    else:
        lines.append("Geen herijking nodig: de uitgerolde constante houdt stand tegen "
                     "het protocol.")
    lines.append("Draai `python tools/bias_backtest.py` voor de volledige tabel.")
    return "\n".join(lines)


def drift_message(signal: dict) -> str:
    """Telegram-tekst voor een gedragen herijking. Noemt de °C op een zonnige
    middag i.p.v. de kale helling — dát is wat het verschil betekent."""
    return "\n".join([
        "🌡️ *Weerstation: helling kan herijkt*",
        f"`SOLAR_BIAS_SLOPE` {signal['deployed']:.5f} → *{signal['slope']:.5f}*",
        f"Scheelt {signal['delta_c']:.2f} °C bij {REFERENCE_WM2:.0f} W/m² (zonnige middag).",
        f"Bewijs: {signal['n']} uren over {len(signal['months'])} maanden, "
        f"referentie {signal['reference']}, driver {signal['driver']} — "
        f"haalt forward-chaining, elke fold én de ruisvloer.",
        "Zet 'm met de hand in `wu_bias.py`.",
    ])


def telegram_summary(stats: dict) -> str:
    o, pm = stats["overall"], stats["sunny_afternoon"]
    lines = ["🌡️ *Weerstation-check* (WU − ERA5)",
             f"Gem. afwijking: *{o['mean_bias']:+.2f} °C* (RMSE {o['rmse']:.2f}, n={o['n']})"]
    if pm["n"]:
        lines.append(f"Zonnige eind-middag: *{pm['mean_bias']:+.2f} °C* (n={pm['n']})")
    if stats["solar_slope_per_100"] is not None:
        lines.append(f"Per 100 W/m² zon: {stats['solar_slope_per_100']:+.2f} °C")
    return "\n".join(lines)


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    station_id = os.environ.get("WU_STATION_ID")
    api_key = os.environ.get("WU_API_KEY")
    if not station_id or not api_key:
        raise SystemExit("WU_STATION_ID en WU_API_KEY zijn vereist.")
    days = int(os.environ.get("ACCURACY_DAYS", "30"))

    today = local_today()
    start, end = today - timedelta(days=days), today - timedelta(days=1)

    wu = fetch_wu_hourly(station_id, api_key, days)
    om = fetch_om_archive_hourly(start.isoformat(), end.isoformat())
    rows = pair_hours(wu, om)
    if not rows:
        raise SystemExit("Geen gekoppelde uren — controleer station-id/periode "
                         "(ERA5 loopt ~5 dagen achter).")

    # Tweede referentie (KNMI De Bilt, 4,1 km) — niet-fataal: de diagnose blijft
    # op ERA5 draaien als de scriptservice hapert. Zie knmi_ref.py voor het waarom.
    knmi_hours: Dict[str, dict] = {}
    try:
        knmi_hours = knmi_ref.fetch_hourly(start.isoformat(), end.isoformat())
        hit = attach_knmi(rows, knmi_hours)
        print(f"[KNMI] {hit}/{len(rows)} gekoppelde uren hebben een KNMI-referentie")
    except Exception as e:
        attach_knmi(rows, {})
        print(f"[KNMI] referentie niet opgehaald ({sanitize_error(e)}) — alleen ERA5")

    # Buur-coherentie liftt mee op dezelfde KNMI-uren (geen tweede fetch) en is
    # optioneel: zonder WU_NEIGHBOUR_IDS gebeurt er niets.
    try:
        neighbours = neighbour_coherence(api_key, start.isoformat(), end.isoformat(),
                                         days, knmi_hours)
    except Exception as e:
        neighbours = None
        print(f"[buur] coherentietoets overgeslagen ({sanitize_error(e)})")

    fresh, updated = append_archive(rows)
    print(f"[archief] {HISTORY_DIR}: {fresh} nieuwe uren, {updated} bijgewerkt "
          f"({len(load_archive())} totaal)")

    # Herijkingssignaal: draait op het hele archief (niet alleen dit venster) en
    # meldt alleen als het protocol de herijking dráágt — zie recalibration_signal.
    archive = load_archive()
    signal = recalibration_signal(archive)

    stats = analyse(rows)
    report = build_report(stats)
    print(report)
    print(protocol_report(rows, signal))

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "station_accuracy",
        "reference": "open-meteo ERA5 archive",
        "location": {"lat": UTRECHT_LAT, "lon": UTRECHT_LON, "name": "Utrecht Oost"},
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "n_pairs": len(rows),
        # Additief (aug 2026): het dashboard toonde alleen het laatste venster tegen
        # ERA5, terwijl de ijking inmiddels op een groeiend archief, een tweede
        # referentie en een expliciet acceptatieprotocol rust.
        "references": reference_stats(rows),
        "archive": archive_summary(archive),
        "recalibration": {
            "needed": bool(signal),
            "deployed": SOLAR_BIAS_SLOPE,
            "reference_wm2": REFERENCE_WM2,
            **({k: signal[k] for k in ("slope", "delta_c", "reference", "driver", "n", "months")}
               if signal else {}),
        },
        "neighbours": neighbours,
        **stats,
    }
    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[OUT] {OUT_PATH} geschreven ({len(rows)} paren)")

    if os.environ.get("DRY_RUN") == "1":
        return
    if os.environ.get("SEND_TELEGRAM") == "1":
        send_telegram(telegram_summary(stats), parse_mode="Markdown")
    elif signal:
        # Uitzonderingsmelding: de maandelijkse cron draait zonder `notify`, dus
        # zwijgt normaal. Alleen als het protocol een herijking draagt gaat er een
        # bericht uit — anders zou de constante opnieuw stilletjes verouderen
        # (0.00421 stond maanden ná de laatste aanbeveling nog in wu_bias.py).
        send_telegram(drift_message(signal), parse_mode="Markdown")


if __name__ == "__main__":
    main()

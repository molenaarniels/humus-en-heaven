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

import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

from http_util import get_json
from notify import sanitize_error, send_telegram
from shared_const import LATITUDE as UTRECHT_LAT, LONGITUDE as UTRECHT_LON, TZ, local_today

UTC = timezone.utc

# Stratificatie-grenzen
AFTERNOON = range(13, 19)        # lokale uren 13:00–18:59 ("eind middag")
SUNNY_CLOUD_MAX = 25.0           # bewolking < 25% telt als "zonnig"
OVERCAST_CLOUD_MIN = 75.0        # > 75% telt als "bewolkt"
SOLAR_BINS = [(0, 1), (1, 200), (200, 400), (400, 600), (600, 10000)]
WIND_BINS = [(0, 5), (5, 10), (10, 20), (20, 1000)]   # km/h @10m

OUT_PATH = "docs/accuracy_data.json"
SCATTER_CAP = 1500               # max punten in JSON (dashboard scatter)


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
        try:
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                print(f"[WU] {d} status {r.status_code}")
                continue
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
# PAIR + STATS
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
        })
    return rows


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

    # scatter (gedownsampled) voor het dashboard
    step = max(1, len(rows) // SCATTER_CAP)
    scatter = [{"t": r["t"], "h": r["hour"], "bias": round(r["bias"], 2),
                "solar": r["solar"], "cloud": r["cloud"], "wind": r["wind"],
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

    stats = analyse(rows)
    report = build_report(stats)
    print(report)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "station_accuracy",
        "reference": "open-meteo ERA5 archive",
        "location": {"lat": UTRECHT_LAT, "lon": UTRECHT_LON, "name": "Utrecht Oost"},
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "n_pairs": len(rows),
        **stats,
    }
    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[OUT] {OUT_PATH} geschreven ({len(rows)} paren)")

    if os.environ.get("SEND_TELEGRAM") == "1" and os.environ.get("DRY_RUN") != "1":
        send_telegram(telegram_summary(stats), parse_mode="Markdown")


if __name__ == "__main__":
    main()

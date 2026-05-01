"""
Soil moisture model — FAO-56 Penman-Monteith + water balance.
Shared between the GitHub Action and the data-builder for the static site.
"""
import calendar as _calendar
import math
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional

import requests

# --- Locatie & bodem ---
UTRECHT_LAT = 52.0907
UTRECHT_LON = 5.1214
UTRECHT_ELEV = 5.0

SOIL_FC = 0.20  # ophoogzand met kleicomponent, Utrecht Oost (Schildersbuurt)
SOIL_WP = 0.09

ZONES = {
    "lawn":   {"name": "Lawn",   "Zr": 0.20},
    "shrubs": {"name": "Shrubs", "Zr": 0.42},
}

# Seizoensgebonden Kc per zone (FAO-56, gecalibreerd voor Nederland).
# Format: lijst van (dag_van_jaar, Kc) ankerpunten.
# Tussenliggende waarden worden lineair geïnterpoleerd.
# Gebaseerd op FAO-56 Tabel 12 + KNMI klimaatdata voor Utrecht.
KC_SEASONAL = {
    # Graszone: enkelvoudige curve, maandelijkse ankerpunten.
    # Bronnen: FAO-56 Tabel 12, KNMI klimaatdata Utrecht, veldcalibratie.
    "lawn": [
        (  1, 0.40),  # jan: winterrust
        ( 32, 0.40),  # feb: winterrust
        ( 60, 0.65),  # mrt: herstelgroei
        ( 91, 0.90),  # apr: actieve groei
        (121, 0.95),  # mei: vol groeiseizoen
        (152, 1.00),  # jun: max verdamping
        (182, 1.00),  # jul: vol seizoen
        (213, 1.00),  # aug: vol seizoen
        (244, 0.85),  # sep: lichte afname
        (274, 0.75),  # okt: groei neemt af
        (305, 0.50),  # nov: bijna winterrust
        (335, 0.40),  # dec: winterrust
        (365, 0.40),  # eind dec
    ],
    # Plantenzone: gewogen mix van fruitbomen (5%), vaste planten (75%), kale grond (20%).
    # Kc_zone = 0.05*Kc_bomen + 0.75*Kc_vast + 0.20*Kc_kaal
    "shrubs": [
        (  1, 0.35),  # jan: winterrust mix
        ( 32, 0.35),  # feb: winterrust mix
        ( 60, 0.58),  # mrt: uitlopen bomen + vaste planten
        ( 91, 0.82),  # apr: blad in ontwikkeling
        (121, 0.87),  # mei: vol blad
        (152, 0.98),  # jun: max seizoen
        (182, 0.98),  # jul: vol seizoen
        (213, 0.98),  # aug: vol seizoen
        (244, 0.64),  # sep: blad verkleurt, terugval
        (274, 0.56),  # okt: blad valt
        (305, 0.38),  # nov: kale takken
        (335, 0.35),  # dec: winterrust mix
        (365, 0.35),  # eind dec
    ],
}


def seasonal_kc(zone_key: str, doy: int) -> float:
    """Geeft de Kc-waarde voor een zone op dag `doy` via lineaire interpolatie."""
    anchors = KC_SEASONAL[zone_key]
    if doy <= anchors[0][0]:
        return anchors[0][1]
    if doy >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        d0, k0 = anchors[i]
        d1, k1 = anchors[i + 1]
        if d0 <= doy <= d1:
            t = (doy - d0) / (d1 - d0)
            return round(k0 + t * (k1 - k0), 3)
    return 0.75  # fallback


# =============================================================================
# WETENSCHAPPELIJK MODEL
# =============================================================================

def penman_monteith_et0(Tmax, Tmin, RHmean, u2, Rs, elev, lat_rad, doy):
    Tmean = (Tmax + Tmin) / 2
    P = 101.3 * ((293 - 0.0065 * elev) / 293) ** 5.26
    gamma = 0.000665 * P
    delta = (4098 * (0.6108 * math.exp((17.27 * Tmean) / (Tmean + 237.3)))) / \
            (Tmean + 237.3) ** 2
    eTmax = 0.6108 * math.exp((17.27 * Tmax) / (Tmax + 237.3))
    eTmin = 0.6108 * math.exp((17.27 * Tmin) / (Tmin + 237.3))
    es = (eTmax + eTmin) / 2
    ea = es * RHmean / 100
    dr = 1 + 0.033 * math.cos(2 * math.pi * doy / 365)
    decl = 0.409 * math.sin(2 * math.pi * doy / 365 - 1.39)
    ws = math.acos(-math.tan(lat_rad) * math.tan(decl))
    Ra = (24 * 60 / math.pi) * 0.082 * dr * (
        ws * math.sin(lat_rad) * math.sin(decl) +
        math.cos(lat_rad) * math.cos(decl) * math.sin(ws)
    )
    Rso = (0.75 + 2e-5 * elev) * Ra
    Rns = (1 - 0.23) * Rs
    sigma = 4.903e-9
    Rnl = sigma * (
        ((Tmax + 273.16) ** 4 + (Tmin + 273.16) ** 4) / 2
    ) * (0.34 - 0.14 * math.sqrt(max(ea, 0))) * (
        1.35 * min(max(Rs / Rso, 0.3), 1) - 0.35
    )
    Rn = Rns - Rnl
    num = 0.408 * delta * Rn + gamma * (900 / (Tmean + 273)) * u2 * (es - ea)
    den = delta + gamma * (1 + 0.34 * u2)
    return max(num / den, 0)


def run_water_balance(series: List[Dict], zone: Dict, zone_key: str,
                      irrigations: Optional[Dict[str, float]] = None) -> List[Dict]:
    """Single-bucket balance. irrigations = {"YYYY-MM-DD": mm_gegeven}.
    Gebruikt seizoensgebonden Kc per dag via seasonal_kc()."""
    irrigations = irrigations or {}
    AWC_max = (SOIL_FC - SOIL_WP) * zone["Zr"] * 1000
    start_theta = SOIL_FC - (SOIL_FC - SOIL_WP) * 0.3
    water = (start_theta - SOIL_WP) * zone["Zr"] * 1000
    out = []
    for d in series:
        doy = datetime.fromisoformat(d["date"]).timetuple().tm_yday
        kc = seasonal_kc(zone_key, doy)

        # Bodemtemperatuur drempel (FAO-56 §3.3):
        # Onder 5°C stopt plantengroei nagenoeg volledig — geen verdamping.
        # Tussen 5°C en 8°C lineaire overgang (voorkomt harde knip in de grafiek).
        # Tmean als proxy voor bodemtemperatuur — vertraagd maar voldoende
        # nauwkeurig voor dagelijkse beslissingen.
        tmean = d.get("Tmean") or ((d.get("Tmax", 10) + d.get("Tmin", 0)) / 2)
        if tmean <= 5.0:
            temp_factor = 0.0
        elif tmean <= 8.0:
            temp_factor = (tmean - 5.0) / 3.0  # 0→1 tussen 5 en 8°C
        else:
            temp_factor = 1.0
        kc = round(kc * temp_factor, 3)

        ETc = d["ET0"] * kc
        depletion = max(0, AWC_max - water)
        RAW = AWC_max * 0.5
        Ks = 1 if depletion <= RAW else max(0, (AWC_max - depletion) / (AWC_max - RAW))
        actual_ET = ETc * Ks
        rain = d.get("precip") or 0
        irrig = irrigations.get(f"{d['date']}_{zone_key}", irrigations.get(d["date"], 0))
        water += rain + irrig - actual_ET
        drainage = 0
        if water > AWC_max:
            drainage = water - AWC_max
            water = AWC_max
        if water < 0:
            water = 0
        theta = SOIL_WP + water / (zone["Zr"] * 1000)
        depletion_pct = (AWC_max - water) / AWC_max * 100
        out.append({
            "theta": round(theta, 4),
            "depletion_pct": round(depletion_pct, 1),
            "ETc": round(actual_ET, 2),
            "Kc": kc,
            "drainage": round(drainage, 2),
            "irrigation": irrig,
        })
    return out


# =============================================================================
# DATA FETCHING
# =============================================================================

def fetch_open_meteo(days_past: int = 30, days_forecast: int = 7) -> List[Dict]:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={UTRECHT_LAT}&longitude={UTRECHT_LON}"
        f"&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
        f"relative_humidity_2m_mean,wind_speed_10m_mean,precipitation_sum,"
        f"shortwave_radiation_sum,et0_fao_evapotranspiration"
        f"&past_days={days_past}&forecast_days={days_forecast}"
        f"&timezone=Europe%2FAmsterdam"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    j = r.json()
    d = j["daily"]
    today = date.today().isoformat()
    return [
        {
            "date": t,
            "Tmax": d["temperature_2m_max"][i],
            "Tmin": d["temperature_2m_min"][i],
            "Tmean": d["temperature_2m_mean"][i],
            "RHmean": d["relative_humidity_2m_mean"][i],
            "u2": (d["wind_speed_10m_mean"][i] or 0) / 3.6,
            "Rs": d["shortwave_radiation_sum"][i],
            "precip": d["precipitation_sum"][i] or 0,
            "forecast": t > today,
        }
        for i, t in enumerate(d["time"])
    ]


def fetch_wunderground(station_id: str, api_key: str, days: int = 30) -> List[Dict]:
    """Haalt WU PWS history. Probeert range-call eerst, dan per-dag fallback."""
    today = date.today()
    start = today - timedelta(days=days)
    end = today - timedelta(days=1)
    url = (
        "https://api.weather.com/v2/pws/history/daily"
        f"?stationId={station_id}&format=json&units=m"
        f"&startDate={start.strftime('%Y%m%d')}&endDate={end.strftime('%Y%m%d')}"
        f"&numericPrecision=decimal&apiKey={api_key}"
    )
    results = []
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            for obs in r.json().get("observations", []):
                m = obs.get("metric", {})
                ts = obs.get("obsTimeUtc") or obs.get("obsTimeLocal", "")
                day = ts[:10] if ts else None
                if not day:
                    continue
                results.append({
                    "date": day,
                    "Tmax": m.get("tempHigh"),
                    "Tmin": m.get("tempLow"),
                    "Tmean": m.get("tempAvg"),
                    "RHmean": obs.get("humidityAvg"),
                    "u2": (obs.get("windspeedAvg") or 0) / 3.6,
                    "precip": m.get("precipTotal"),
                })
            if results:
                return results
    except Exception as e:
        print(f"[WU] range call failed: {e}")

    print("[WU] falling back to per-day...")
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        url = (
            "https://api.weather.com/v2/pws/history/daily"
            f"?stationId={station_id}&format=json&units=m"
            f"&date={d.strftime('%Y%m%d')}&numericPrecision=decimal&apiKey={api_key}"
        )
        try:
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                continue
            obs_list = r.json().get("observations", [])
            if not obs_list:
                continue
            obs = obs_list[0]
            m = obs.get("metric", {})
            results.append({
                "date": d.isoformat(),
                "Tmax": m.get("tempHigh"),
                "Tmin": m.get("tempLow"),
                "Tmean": m.get("tempAvg"),
                "RHmean": obs.get("humidityAvg"),
                "u2": (obs.get("windspeedAvg") or 0) / 3.6,
                "precip": m.get("precipTotal"),
            })
        except Exception as e:
            print(f"[WU] {d} failed: {e}")
    return results


# =============================================================================
# COMBINED
# =============================================================================

def _apply_et0_and_balance(series: List[Dict],
                           irrigations: Optional[Dict[str, float]] = None) -> List[Dict]:
    """Compute ET0 + water balance in-place on a series of day dicts. Returns the series."""
    lat_rad = math.radians(UTRECHT_LAT)
    for d in series:
        doy = datetime.fromisoformat(d["date"]).timetuple().tm_yday
        try:
            d["ET0"] = round(penman_monteith_et0(
                Tmax=d["Tmax"], Tmin=d["Tmin"], RHmean=d["RHmean"],
                u2=d["u2"], Rs=d["Rs"], elev=UTRECHT_ELEV,
                lat_rad=lat_rad, doy=doy,
            ), 2)
        except Exception:
            d["ET0"] = None
    for d in series:
        if d["ET0"] is None:
            vals = [x["ET0"] for x in series if x["ET0"] is not None]
            d["ET0"] = sum(vals) / len(vals) if vals else 2.0

    balances = {k: run_water_balance(series, z, k, irrigations) for k, z in ZONES.items()}
    for i, d in enumerate(series):
        for k, bal in balances.items():
            s = bal[i]
            d[f"{k}_theta"] = s["theta"]
            d[f"{k}_depletion"] = s["depletion_pct"]
            d[f"{k}_ETc"] = s["ETc"]
            d[f"{k}_Kc"] = s["Kc"]
            d[f"{k}_irrigation"] = s["irrigation"]
    return series


def build_full_dataset(station_id: Optional[str], api_key: Optional[str],
                       irrigations: Optional[Dict[str, float]] = None,
                       days_past: int = 35) -> Dict:
    """Haalt data, merge WU met Open-Meteo, run ET0 + water balance per zone."""
    om = fetch_open_meteo(days_past=days_past, days_forecast=7)
    wu_days = 0
    source_note = "Open-Meteo (Utrecht reanalysis + forecast)"

    if station_id and api_key:
        try:
            wu = fetch_wunderground(station_id, api_key, days=days_past)
            wu_by_date = {d["date"]: d for d in wu}
            for d in om:
                w = wu_by_date.get(d["date"])
                if not w:
                    continue
                for f in ("Tmax", "Tmin", "Tmean", "RHmean", "u2", "precip"):
                    if w.get(f) is not None:
                        d[f] = w[f]
                d["hasWU"] = True
            wu_days = sum(1 for d in om if d.get("hasWU"))
            if wu_days > 0:
                source_note = f"Wunderground {station_id} ({wu_days}d) + Open-Meteo solar"
        except Exception as e:
            print(f"[WARN] WU merge failed: {e}")

    _apply_et0_and_balance(om, irrigations)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": source_note,
        "wu_days": wu_days,
        "soil": {"FC": SOIL_FC, "WP": SOIL_WP},
        "zones": ZONES,
        "irrigation_rates": IRRIGATION_RATES,
        "location": {"lat": UTRECHT_LAT, "lon": UTRECHT_LON, "name": "Utrecht Oost"},
        "days": om,
    }


def fetch_open_meteo_archive(start_date: str, end_date: str) -> List[Dict]:
    """Haalt historische data op via de Open-Meteo archive API (ERA5 reanalysis)."""
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={UTRECHT_LAT}&longitude={UTRECHT_LON}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
        f"relative_humidity_2m_mean,wind_speed_10m_mean,precipitation_sum,"
        f"shortwave_radiation_sum"
        f"&timezone=Europe%2FAmsterdam"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    j = r.json()
    d = j["daily"]
    return [
        {
            "date": t,
            "Tmax": d["temperature_2m_max"][i],
            "Tmin": d["temperature_2m_min"][i],
            "Tmean": d["temperature_2m_mean"][i],
            "RHmean": d["relative_humidity_2m_mean"][i],
            "u2": (d["wind_speed_10m_mean"][i] or 0) / 3.6,
            "Rs": d["shortwave_radiation_sum"][i],
            "precip": d["precipitation_sum"][i] or 0,
            "forecast": False,
        }
        for i, t in enumerate(d["time"])
    ]


def build_monthly_totals_from_days(days: List[Dict]) -> Dict[str, Dict]:
    """Aggregeert voltooide kalendermaanden uit een verwerkte dagenlijst.

    Geeft alleen maanden terug die volledig aanwezig zijn in `days` (alle
    kalenderdagen aanwezig) én die voor vandaag zijn afgelopen. De huidige
    maand wordt nooit bevroren.
    """
    today = date.today().isoformat()
    raw: Dict[str, Dict] = {}
    for d in days:
        if d.get("forecast") or d["date"] >= today:
            continue
        ym = d["date"][:7]
        if ym not in raw:
            raw[ym] = {
                "rain": 0.0, "irrigation": 0.0,
                "ETc_lawn": 0.0, "ETc_shrubs": 0.0, "days": 0,
            }
        raw[ym]["rain"] += d.get("precip") or 0
        raw[ym]["irrigation"] += (d.get("lawn_irrigation") or 0) + (d.get("shrubs_irrigation") or 0)
        raw[ym]["ETc_lawn"] += d.get("lawn_ETc") or 0
        raw[ym]["ETc_shrubs"] += d.get("shrubs_ETc") or 0
        raw[ym]["days"] += 1

    result: Dict[str, Dict] = {}
    for ym, v in raw.items():
        year, month_num = int(ym[:4]), int(ym[5:7])
        days_in_month = _calendar.monthrange(year, month_num)[1]
        last_day = f"{ym}-{days_in_month:02d}"
        # Only freeze months that are complete AND fully past
        if last_day >= today or v["days"] < days_in_month:
            continue
        result[ym] = {
            "rain": round(v["rain"], 1),
            "irrigation": round(v["irrigation"], 1),
            "ETc_lawn": round(v["ETc_lawn"], 1),
            "ETc_shrubs": round(v["ETc_shrubs"], 1),
        }
    return result


# Irrigatiesnelheden per zone (mm per minuut).
# Druppelslang struiken: 2 mm/uur = 0.0333 mm/min
# Sproeier gras:        20 mm/uur = 0.3333 mm/min
IRRIGATION_RATES = {
    "lawn":   20 / 60,   # mm per minuut
    "shrubs":  2 / 60,   # mm per minuut
}


def irrigation_proposal_mm(zone: str, depletion_pct: float,
                            soil: Dict, zone_info: Dict) -> float:
    """Berekent hoeveel mm water nodig is om terug op 90% veldcapaciteit te komen.
    90% ipv 100% om ruimte te laten voor regen zonder oppervlakkige afvoer."""
    AWC_max = (soil["FC"] - soil["WP"]) * zone_info["Zr"] * 1000  # mm
    current_water = AWC_max * (1 - depletion_pct / 100)
    target_water = AWC_max * 0.90
    needed = max(0, target_water - current_water)
    return round(needed, 1)


def mm_to_minutes(zone: str, mm: float) -> int:
    """Rekent mm om naar minuten op basis van irrigatiesnelheid."""
    rate = IRRIGATION_RATES.get(zone, 0.333)
    if rate <= 0:
        return 0
    return math.ceil(mm / rate)


def assess_status(data: Dict, zone: str = "lawn") -> Dict:
    """Bepaalt of water geven nodig is, inclusief irrigatievoorstel in mm en minuten."""
    days = data["days"]
    soil = data["soil"]
    zone_info = data["zones"][zone]
    today_idx = next((i for i, d in enumerate(days) if d["forecast"]), len(days))
    current_idx = max(0, today_idx - 1)
    current = days[current_idx]
    future = days[current_idx + 1:]
    dep = current[f"{zone}_depletion"]

    if dep > 70:
        state = "dry"
    elif dep > 50:
        state = "threshold"
    elif dep < 20:
        state = "wet"
    else:
        state = "moist"

    days_to_stress = None
    for i, d in enumerate(future):
        if d[f"{zone}_depletion"] > 50:
            days_to_stress = i + 1
            break
    rain7 = sum((d.get("precip") or 0) for d in future)

    # Irrigatievoorstel
    proposal_mm = irrigation_proposal_mm(zone, dep, soil, zone_info)
    proposal_min = mm_to_minutes(zone, proposal_mm)

    if state == "dry":
        recommendation = "URGENT: water vandaag — bodem in stress."
        priority = "high"
    elif state == "threshold":
        if rain7 >= 8:
            recommendation = f"Nog niet water geven — {rain7:.1f} mm regen verwacht (7d)."
            priority = "low"
            proposal_mm = 0
            proposal_min = 0
        else:
            recommendation = "Water geven binnen 1–2 dagen."
            priority = "medium"
    elif state == "moist":
        if days_to_stress and days_to_stress <= 3 and rain7 < 5:
            recommendation = f"Let op — stress-grens over ~{days_to_stress} dagen."
            priority = "low"
        else:
            recommendation = "Geen actie nodig."
            priority = "none"
        proposal_mm = 0
        proposal_min = 0
    else:
        recommendation = "Bodem goed verzadigd."
        priority = "none"
        proposal_mm = 0
        proposal_min = 0

    return {
        "state": state,
        "priority": priority,
        "depletion_pct": dep,
        "days_to_stress": days_to_stress,
        "rain7_mm": round(rain7, 1),
        "recommendation": recommendation,
        "proposal_mm": proposal_mm,
        "proposal_min": proposal_min,
        "irrigation_rate_mm_per_min": IRRIGATION_RATES.get(zone, 0),
        "current": current,
        "zone": zone,
    }

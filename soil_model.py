"""
Soil moisture model — FAO-56 Penman-Monteith + water balance.
Shared between the GitHub Action and the data-builder for the static site.
"""
import calendar as _calendar
import math
from datetime import date, datetime, timedelta, timezone
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo

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

# Per-zone depletie-fractie p (FAO-56 Tabel 22): readily-available water
# = p × AWC. Boven die drempel begint stress (Ks daalt). Cool-season gras
# heeft p ≈ 0.40; gemengde sierbeplanting houdt 0.50 aan.
P_DEPLETION = {"lawn": 0.40, "shrubs": 0.50}

# Interceptie per regenbui (mm): water dat op blad/canopy blijft hangen
# en verdampt voordat het de bodem bereikt. Alleen toegepast als rain > 2 mm
# (lichte motregen draagt verwaarloosbaar bij; gewogen door event-grootte
# zou correcter zijn, maar de impact op dagschaal is klein).
INTERCEPTION = {"lawn": 1.0, "shrubs": 1.5}

# Rolling window (dagen) voor bodemtemperatuur-proxy. Air-Tmean is een te
# snelle proxy voor bodem; de bodem ijlt na, dus een 5-daagse loopgemiddelde
# vermijdt dat een enkele koude dag verdamping volledig naar 0 trekt.
SOIL_TEMP_WINDOW = 5

# Both Open-Meteo en Wunderground PWS leveren wind op 10 m hoogte.
# FAO-56 Penman-Monteith vereist u2 (2 m). Eq. 47 log-law correctie:
#   u2 = u_z * 4.87 / ln(67.8 * z - 5.42)
WIND_MEASUREMENT_HEIGHT = 10.0
WIND_2M_FACTOR = 4.87 / math.log(67.8 * WIND_MEASUREMENT_HEIGHT - 5.42)

# Seizoensgebonden Kcb (basal transpiration coefficient) per zone.
# FAO-56 hoofdstuk 7 (dual-Kc): Kc = Kcb + Ke, waarbij Kcb de
# transpiratie van het gewas representeert en Ke de directe verdamping
# van het bodemoppervlak na bevochtiging. De curves hier zijn de
# bestaande seizoensankerpunten, geïnterpreteerd als Kcb — de waarden
# zijn al dichtbij FAO-56 Tabel 17 (cool-season turf Kcb_mid ≈ 0.90;
# gemengde sierbeplanting Kcb_mid 0.85–1.00).
# Format: lijst van (dag_van_jaar, Kcb) ankerpunten; lineair geïnterpoleerd.
KCB_SEASONAL = {
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
    # Kcb_zone = 0.05*Kcb_bomen + 0.75*Kcb_vast + 0.20*Kcb_kaal
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

# Oppervlaktelaag voor Ke (FAO-56 Tabel 19, voor sandy-loam aangepast aan
# klei-versterkte zandgrond Utrecht):
#   TEW  = totaal verdampbaar water in topbodem (~0.10 m)
#   REW  = readily-evaporable water (geen Kr-reductie tot deze grens)
#   Ze   = informatieve laagdikte (TEW is al in mm uitgedrukt)
SURFACE_LAYER = {"TEW": 18.0, "REW": 8.0, "Ze": 0.10}

# Bovengrens voor de effectieve Kc (Kcb + Ke). FAO-56 Eq. 72 stelt
# Kc_max ≈ 1.20 voor sub-humide klimaat met u2 ~ 2 m/s. We negeren de
# RHmin/u2-afhankelijke fine-tune en gebruiken één constante.
KC_MAX = 1.20

# Grondbedekking fc (fractie van bodem onder canopy). Lawn is bijna
# gesloten in het groeiseizoen; sierbeplanting is een mix met deels
# kale grond/mulch — gemiddeld ~50%.
GROUND_COVER = {"lawn": 0.95, "shrubs": 0.50}

# Bevochtigingsfractie fw bij irrigatie. Sproeier op gazon dekt
# de hele zone; druppelslang bij struiken raakt slechts een smalle
# strook (~30%). Regen wordt altijd fw=1.0 verondersteld.
WETTING_FRACTION = {"lawn": 1.0, "shrubs": 0.30}


def seasonal_kcb(zone_key: str, doy: int) -> float:
    """Lineair geïnterpoleerde basal Kcb voor zone op dag `doy`."""
    anchors = KCB_SEASONAL[zone_key]
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


# Back-compat alias zodat externe importeerders (tests etc.) niet breken.
seasonal_kc = seasonal_kcb
KC_SEASONAL = KCB_SEASONAL


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
                      irrigations: Optional[Dict[str, float]] = None,
                      seed_theta: Optional[float] = None) -> List[Dict]:
    """FAO-56 dual-Kc waterbalans (Kcb + Ke) met twee buckets:

    * `water` — diepe wortelzone (mm beschikbaar boven WP). Daaruit komt
      transpiratie T = Kcb · Ks · temp_factor · ET0 (gewasstress via Ks).
    * `De` — depletie van de oppervlaktelaag (mm). Daaruit komt directe
      bodemevaporatie E = Ke · ET0 (Ke door Kr-droogcurve gemoduleerd).

    Regen/irrigatie vult eerst de oppervlaktelaag tot TEW; overschot
    infiltreert naar de diepe bucket. ETc-output blijft = E + T zodat
    bestaande dashboard-velden compatibel blijven.

    `seed_theta` is start-θ (v/v) van de wortelzone; `De` start altijd
    op REW (matig vochtig oppervlak) tenzij FC volledig wordt geraakt.
    """
    irrigations = irrigations or {}
    AWC_max = (SOIL_FC - SOIL_WP) * zone["Zr"] * 1000
    if seed_theta is not None:
        seed_clamped = max(SOIL_WP, min(SOIL_FC, seed_theta))
        water = (seed_clamped - SOIL_WP) * zone["Zr"] * 1000
    else:
        start_theta = SOIL_FC - (SOIL_FC - SOIL_WP) * 0.3
        water = (start_theta - SOIL_WP) * zone["Zr"] * 1000

    # Voor "vandaag" zit in d["precip"] alleen de regen die al daadwerkelijk
    # gevallen is. Voor toekomstige dagen telt de voorspelde regen wél mee.
    p_zone = P_DEPLETION.get(zone_key, 0.50)
    interception_mm = INTERCEPTION.get(zone_key, 0.0)
    fc_zone = GROUND_COVER.get(zone_key, 0.50)
    fw_irrig = WETTING_FRACTION.get(zone_key, 1.0)
    TEW = SURFACE_LAYER["TEW"]
    REW = SURFACE_LAYER["REW"]

    # Oppervlaktelaag start op REW (overgang van Kr=1 naar Kr<1); de eerste
    # paar regendagen drukken dit snel naar 0 (verzadigd oppervlak).
    De = REW
    tmean_window: List[float] = []
    out = []
    for d in series:
        doy = datetime.fromisoformat(d["date"]).timetuple().tm_yday
        kcb = seasonal_kcb(zone_key, doy)

        # Bodemtemperatuur drempel (FAO-56 §3.3): air-Tmean ijlt na in
        # bodem; we gebruiken SOIL_TEMP_WINDOW-daagse loopgemiddelde.
        tmean_today = d.get("Tmean")
        if tmean_today is None:
            tmean_today = (d.get("Tmax", 10) + d.get("Tmin", 0)) / 2
        tmean_window.append(tmean_today)
        if len(tmean_window) > SOIL_TEMP_WINDOW:
            tmean_window.pop(0)
        tmean_eff = sum(tmean_window) / len(tmean_window)
        if tmean_eff <= 5.0:
            temp_factor = 0.0
        elif tmean_eff <= 8.0:
            temp_factor = (tmean_eff - 5.0) / 3.0
        else:
            temp_factor = 1.0
        kcb_eff = round(kcb * temp_factor, 3)

        ET0 = d["ET0"] or 0

        rain_raw = d.get("precip") or 0
        if rain_raw > 2.0:
            intercepted = min(interception_mm, rain_raw)
        else:
            intercepted = 0.0
        rain = rain_raw - intercepted
        irrig = irrigations.get(f"{d['date']}_{zone_key}", irrigations.get(d["date"], 0))

        # FAO-56 §7.4.5: De en Dr zijn parallelle boekhoudingen van
        # hetzelfde fysieke water in respectievelijk de bovenste Ze m
        # (oppervlaktelaag, gebruikt om Ke te moduleren) en de volle Zr m
        # (wortelzone, bepaalt θ en stress). Regen + irrigatie wordt van
        # beide buckets afgetrokken (Eq. 77 en Eq. 85); ETc = E + T komt
        # bij beide weer toe — E uit de oppervlaktelaag, T uit de diepe
        # zone. Overschot in De boven 0 (saturatie) verlaat de oppervlakte-
        # laag als deep-percolation maar blijft binnen de wortelzone.
        wetting = rain + irrig
        De = max(0.0, De - wetting)

        # few = fractie blootgesteld én bevochtigd oppervlak (FAO-56 Eq. 75).
        if wetting > 0 and rain > 0:
            fw_eff = 1.0  # regendag domineert de bevochtigingsfractie
        elif irrig > 0:
            fw_eff = fw_irrig
        else:
            fw_eff = 1.0  # zonder bevochtiging is few irrelevant (E ≈ 0)
        few = max(0.01, min(1.0 - fc_zone, fw_eff))

        # Kr-droogcurve (FAO-56 Eq. 74).
        if De <= REW:
            Kr = 1.0
        elif De >= TEW:
            Kr = 0.0
        else:
            Kr = (TEW - De) / (TEW - REW)
        # Ke (FAO-56 Eq. 71). temp_factor demt ook E onder 5–8 °C.
        Ke = max(0.0, min(Kr * (KC_MAX - kcb_eff), few * KC_MAX)) * temp_factor

        # Stress en transpiratie uit de diepe bucket.
        depletion = max(0, AWC_max - water)
        RAW = AWC_max * p_zone
        Ks = 1 if depletion <= RAW else max(0, (AWC_max - depletion) / (AWC_max - RAW))
        T = kcb_eff * Ks * ET0

        # Surface evap mag niet meer dan beschikbaar in oppervlaktelaag.
        E_max_available = max(0.0, TEW - De)
        E = min(Ke * ET0, E_max_available)

        # Update oppervlaktelaag met evaporatie.
        De = min(TEW, De + E)

        # Update wortelzone: regen/irrigatie komt erin, E + T gaan eruit.
        # Beide verliezen tellen omdat de oppervlaktelaag binnen de wortel-
        # zone valt (Ze < Zr); water dat als E verdampt verlaat dus ook Dr.
        water += wetting - T - E
        drainage = 0.0
        if water > AWC_max:
            drainage = water - AWC_max
            water = AWC_max
        if water < 0:
            water = 0

        theta = SOIL_WP + water / (zone["Zr"] * 1000)
        depletion_pct = (AWC_max - water) / AWC_max * 100
        ETc_total = E + T
        Kc_eff = (ETc_total / ET0) if ET0 > 0 else (kcb_eff + Ke)

        out.append({
            "theta": round(theta, 4),
            "depletion_pct": round(depletion_pct, 1),
            "ETc": round(ETc_total, 2),
            "Kc": round(Kc_eff, 3),
            "Kcb": kcb_eff,
            "Ke": round(Ke, 3),
            "E": round(E, 2),
            "T": round(T, 2),
            "De": round(De, 2),
            "few": round(few, 3),
            "drainage": round(drainage, 2),
            "irrigation": irrig,
            "interception": round(intercepted, 2),
        })
    return out


# =============================================================================
# DATA FETCHING
# =============================================================================

def _today_rain_so_far(hourly: Optional[Dict], today_str: str) -> float:
    """Som de uurlijkse neerslag van vandaag tot het huidige tijdstip
    (Europe/Amsterdam). Geeft 0 als er nog geen uur voorbij is, of als
    hourly-data ontbreekt. Open-Meteo levert tijden in Amsterdam tz."""
    if not hourly or "time" not in hourly or "precipitation" not in hourly:
        return 0.0
    now_ams = datetime.now(ZoneInfo("Europe/Amsterdam"))
    cutoff = now_ams.strftime("%Y-%m-%dT%H:00")
    total = 0.0
    for t, p in zip(hourly["time"], hourly["precipitation"]):
        if t.startswith(today_str) and t < cutoff:
            total += p or 0
    return round(total, 2)


def fetch_open_meteo(days_past: int = 30, days_forecast: int = 7) -> List[Dict]:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={UTRECHT_LAT}&longitude={UTRECHT_LON}"
        f"&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
        f"relative_humidity_2m_mean,wind_speed_10m_mean,precipitation_sum,"
        f"shortwave_radiation_sum,et0_fao_evapotranspiration"
        f"&hourly=precipitation"
        f"&past_days={days_past}&forecast_days={days_forecast}"
        f"&timezone=Europe%2FAmsterdam"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    j = r.json()
    d = j["daily"]
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
    # Voor "vandaag" gebruiken we alleen de regen die al daadwerkelijk is
    # gevallen (uurlijks). De daily-sum mengt gemeten + voorspeld; dat zou
    # de balans laten lijken alsof de voorspelde regen al verwerkt is.
    rain_today_so_far = _today_rain_so_far(j.get("hourly"), today)
    return [
        {
            "date": t,
            "Tmax": d["temperature_2m_max"][i],
            "Tmin": d["temperature_2m_min"][i],
            "Tmean": d["temperature_2m_mean"][i],
            "RHmean": d["relative_humidity_2m_mean"][i],
            "u2": (d["wind_speed_10m_mean"][i] or 0) / 3.6 * WIND_2M_FACTOR,
            "Rs": d["shortwave_radiation_sum"][i],
            "precip": rain_today_so_far if t == today else (d["precipitation_sum"][i] or 0),
            "ET0_om": d.get("et0_fao_evapotranspiration", [None] * len(d["time"]))[i],
            "forecast": t > today,
        }
        for i, t in enumerate(d["time"])
    ]


def fetch_wunderground_current(station_id: str, api_key: str) -> Optional[Dict]:
    """Haalt de meest recente WU observatie. Geeft today's accumulatieve
    `precipTotal` (sinds middernacht station-tijd). Andere velden blijven
    leeg zodat de merge alleen precip overschrijft — Tmax/Tmin/RHmean/u2
    voor today blijven van Open-Meteo komen (daily aggregaten)."""
    url = (
        "https://api.weather.com/v2/pws/observations/current"
        f"?stationId={station_id}&format=json&units=m"
        f"&numericPrecision=decimal&apiKey={api_key}"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        obs_list = r.json().get("observations", [])
        if not obs_list:
            return None
        obs = obs_list[0]
        m = obs.get("metric", {})
        precip = m.get("precipTotal")
        if precip is None:
            return None
        # obsTimeLocal is "YYYY-MM-DD HH:MM:SS" in station-tz (Amsterdam).
        day = (obs.get("obsTimeLocal") or "")[:10]
        if not day:
            day = (obs.get("obsTimeUtc") or "")[:10]
        if not day:
            return None
        return {
            "date": day,
            "Tmax": None, "Tmin": None, "Tmean": None,
            "RHmean": None, "u2": None,
            "precip": precip,
        }
    except Exception as e:
        print(f"[WU] current call failed: {e}")
        return None


def fetch_wunderground(station_id: str, api_key: str, days: int = 30) -> List[Dict]:
    """Haalt WU PWS history (afgesloten dagen) + current observatie voor
    today's accumulatieve precip. Probeert range-call eerst, dan per-dag
    fallback voor history."""
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    start = today - timedelta(days=days)
    end = today - timedelta(days=1)
    url = (
        "https://api.weather.com/v2/pws/history/daily"
        f"?stationId={station_id}&format=json&units=m"
        f"&startDate={start.strftime('%Y%m%d')}&endDate={end.strftime('%Y%m%d')}"
        f"&numericPrecision=decimal&apiKey={api_key}"
    )
    results = []
    history_ok = False
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
                    "u2": (obs.get("windspeedAvg") or 0) / 3.6 * WIND_2M_FACTOR,
                    "precip": m.get("precipTotal"),
                })
            history_ok = bool(results)
    except Exception as e:
        print(f"[WU] range call failed: {e}")

    if not history_ok:
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
                    "u2": (obs.get("windspeedAvg") or 0) / 3.6 * WIND_2M_FACTOR,
                    "precip": m.get("precipTotal"),
                })
            except Exception as e:
                print(f"[WU] {d} failed: {e}")

    # Today's accumulatieve regen via current-endpoint. Overschrijft via de
    # merge alleen `precip` (andere velden zijn None) — Tmax/Tmin/etc. voor
    # today blijven van Open-Meteo komen.
    today_obs = fetch_wunderground_current(station_id, api_key)
    if today_obs and today_obs["date"] == today.isoformat():
        results.append(today_obs)
        print(f"[WU] today: precip={today_obs['precip']} mm tot nu")
    return results


# =============================================================================
# COMBINED
# =============================================================================

def apply_et0_and_balance(series: List[Dict],
                          irrigations: Optional[Dict[str, float]] = None,
                          seed_theta: Optional[Dict[str, float]] = None) -> List[Dict]:
    """Compute ET0 + water balance in-place on a series of day dicts. Returns the series.

    `seed_theta` is optioneel een dict zoals {"lawn": 0.14, "shrubs": 0.16}
    met de start-θ (v/v) per zone voor de eerste dag. Als afwezig: FAO-56
    30%-uitputting heuristiek.
    """
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

    seed_theta = seed_theta or {}
    balances = {
        k: run_water_balance(series, z, k, irrigations, seed_theta=seed_theta.get(k))
        for k, z in ZONES.items()
    }
    for i, d in enumerate(series):
        for k, bal in balances.items():
            s = bal[i]
            d[f"{k}_theta"] = s["theta"]
            d[f"{k}_depletion"] = s["depletion_pct"]
            d[f"{k}_ETc"] = s["ETc"]
            d[f"{k}_Kc"] = s["Kc"]
            d[f"{k}_irrigation"] = s["irrigation"]
            d[f"{k}_drainage"] = s["drainage"]
            d[f"{k}_interception"] = s["interception"]
            d[f"{k}_Kcb"] = s["Kcb"]
            d[f"{k}_Ke"] = s["Ke"]
            d[f"{k}_E"] = s["E"]
            d[f"{k}_T"] = s["T"]
            d[f"{k}_De"] = s["De"]
            d[f"{k}_few"] = s["few"]
    return series


def build_full_dataset(station_id: Optional[str], api_key: Optional[str],
                       irrigations: Optional[Dict[str, float]] = None,
                       days_past: int = 35,
                       seed_theta: Optional[Dict[str, float]] = None) -> Dict:
    """Haalt data, merge WU met Open-Meteo, run ET0 + water balance per zone.

    `seed_theta` is optioneel een dict {"lawn": θ, "shrubs": θ} dat dient
    als startwaarde voor de waterbalans op dag 0 van het venster. Bedoeld
    om state continuïteit te geven tussen opeenvolgende runs.
    """
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

    apply_et0_and_balance(om, irrigations, seed_theta=seed_theta)

    # State-carry-over: laatste niet-forecast dag dient als seed voor de
    # volgende run. Zo convergeert het 35-daagse warmup-venster naar een
    # consistente initiële conditie in plaats van elke run vanaf 30%-
    # uitputting te starten.
    theta_end: Dict[str, Optional[float]] = {}
    theta_end_date: Optional[str] = None
    for d in reversed(om):
        if d.get("forecast"):
            continue
        if d.get("lawn_theta") is None:
            continue
        theta_end_date = d["date"]
        for k in ZONES.keys():
            theta_end[k] = d.get(f"{k}_theta")
        break

    return {
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "source": source_note,
        "wu_days": wu_days,
        "soil": {"FC": SOIL_FC, "WP": SOIL_WP},
        "zones": ZONES,
        "irrigation_rates": IRRIGATION_RATES,
        "location": {"lat": UTRECHT_LAT, "lon": UTRECHT_LON, "name": "Utrecht Oost"},
        "seed_source": "previous_run" if seed_theta else "default_30pct",
        "theta_end": {"as_of": theta_end_date, **theta_end},
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
            "u2": (d["wind_speed_10m_mean"][i] or 0) / 3.6 * WIND_2M_FACTOR,
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
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
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

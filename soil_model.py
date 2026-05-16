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
    # Gewogen rooting depth voor gemengde sierbeplanting + 25% volwassen
    # fruitbomen. Volwassen vrucht- en sierboomwortels reiken in zandgrond
    # vaak 0.6–1.0 m; gewogen met perennials (~0.4 m) en hedge (~0.4 m)
    # komt het effectieve diepteveld neer op ~0.50 m.
    "shrubs": {"name": "Shrubs", "Zr": 0.50},
}

# Per-zone depletie-fractie p (FAO-56 Tabel 22): readily-available water
# = p × AWC. Boven die drempel begint stress (Ks daalt). Cool-season gras
# heeft p ≈ 0.40; gemengde sierbeplanting houdt 0.50 aan.
P_DEPLETION = {"lawn": 0.40, "shrubs": 0.50}

# Interceptiecapaciteit C (mm): maximale hoeveelheid water die op
# blad/canopy blijft hangen voordat het de bodem bereikt. De effectieve
# interceptie per dag volgt de canopy-saturation-curve (Liu 1997, ook
# bekend als de exponentiële vorm van het Rutter-model):
#     I = C · (1 − exp(−P / C))
# Voor kleine events (P ≪ C) schaalt I ongeveer lineair met P en wordt
# een groot deel van de regen onderschept; bij grote events (P ≫ C)
# verzadigt de canopy en passeert het overschot naar de bodem. Dit
# vervangt een eerder hard 2 mm drempelmodel dat bij 2.0–3.0 mm events
# 50–75% van de regen onderschepte — fysisch niet houdbaar.
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
# van het bodemoppervlaktelaag na bevochtiging.
#
# Kalibratie: de oorspronkelijke "lumped" Kc-curves voor deze tuin gaven
# in de praktijk realistische irrigatie-aanbevelingen. Bij overgang naar
# dual-Kc bouwt Ke daarboven op (lawn few≈0.05 → Ke≈0.06; shrubs few≈0.50
# → Ke≈0.20 gemiddeld). Om de oude lange-termijn effectieve Kc te
# behouden zijn de basal-anchors hier neerwaarts geschaald:
#   • lawn   × 0.90 (mid 1.00 → 0.90; ruwweg ouwe Kc minus Ke_avg≈0.06)
#   • shrubs × 0.816 (mid 0.98 → 0.80; ouwe Kc minus Ke_avg≈0.20)
# Waarden blijven binnen FAO-56 Tabel 17 bandbreedte (cool-season turf
# Kcb_mid 0.85-0.95; sierbeplanting/mixed cover 0.75-0.85).
# Format: lijst van (dag_van_jaar, Kcb) ankerpunten; lineair geïnterpoleerd.
KCB_SEASONAL = {
    "lawn": [
        (  1, 0.36),  # jan: winterrust
        ( 32, 0.36),  # feb: winterrust
        ( 60, 0.59),  # mrt: herstelgroei
        ( 91, 0.81),  # apr: actieve groei
        (121, 0.86),  # mei: vol groeiseizoen
        (152, 0.90),  # jun: max transpiratie
        (182, 0.90),  # jul: vol seizoen
        (213, 0.90),  # aug: vol seizoen
        (244, 0.77),  # sep: lichte afname
        (274, 0.68),  # okt: groei neemt af
        (305, 0.45),  # nov: bijna winterrust
        (335, 0.36),  # dec: winterrust
        (365, 0.36),  # eind dec
    ],
    # Plantenzone: gewogen mix van volwassen fruitbomen (25%), evergreen lage
    # haag (buxus/taxus, 10%), vaste planten/groundcover (60%), mulch/kale
    # grond (5%). Mid-seizoen Kcb ≈ 0.83 volgt uit 0.25·0.95 (FAO-56 Tbl 17
    # deciduous fruit, active cover) + 0.10·0.70 (clipped evergreen hedge) +
    # 0.60·0.85 (perennials in bloei) ≈ 0.82, na dual-Kc Ke-aftrek ~0.83.
    # Winter ondergrens 0.34 omdat de evergreen haag blijft transpireren.
    "shrubs": [
        (  1, 0.34),  # jan: winterrust + evergreen haag actief
        ( 32, 0.34),  # feb: winterrust + evergreen haag actief
        ( 60, 0.49),  # mrt: uitlopen bomen + vaste planten
        ( 91, 0.69),  # apr: blad in ontwikkeling
        (121, 0.74),  # mei: vol blad
        (152, 0.83),  # jun: max seizoen
        (182, 0.83),  # jul: vol seizoen
        (213, 0.83),  # aug: vol seizoen
        (244, 0.55),  # sep: blad verkleurt, terugval
        (274, 0.46),  # okt: blad valt, evergreen haag compenseert deels
        (305, 0.36),  # nov: kale takken, alleen haag + groundcover
        (335, 0.34),  # dec: winterrust + evergreen haag actief
        (365, 0.34),  # eind dec
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

# Grondbedekking fc (fractie van bodem die beschermd is tegen directe
# zoninstraling). Voor Ke beperkt dit `few = 1 − fc`: alleen de blootgestelde
# bare-soil fractie draagt bij aan oppervlakteverdamping. Lawn is bijna
# gesloten in het groeiseizoen. De plantenzone in deze tuin is dicht
# beplant: fruitbomen + evergreen haag + groundcover (geranium, alchemilla)
# bedekken de bodem vrijwel volledig, met mulch in de paar resterende
# hoekjes — visueel ~0% blootgestelde grond. FAO-56 Eq. 76 staat toe `fc`
# te interpreteren als effectieve bedekking inclusief mulch (zie ch. 11).
GROUND_COVER = {"lawn": 0.95, "shrubs": 0.90}

# Bevochtigingsfractie fw bij irrigatie. Sproeier op gazon dekt
# de hele zone; druppelslang bij struiken raakt slechts een smalle
# strook (~30%). Regen wordt altijd fw=1.0 verondersteld.
WETTING_FRACTION = {"lawn": 1.0, "shrubs": 0.30}

# Open-Meteo bodemvochtlagen (m³/m³). Forecast-endpoint levert NWP-model
# lagen, archive-endpoint levert ERA5 reanalyse-lagen. We bewaren ze in
# `data.json` als validatie-overlay — het FAO-56 model blijft de bron
# voor irrigatiebeslissingen. Bounds in meter.
OM_SM_LAYERS_FORECAST = [
    ("soil_moisture_0_to_1cm",   0.00, 0.01),
    ("soil_moisture_1_to_3cm",   0.01, 0.03),
    ("soil_moisture_3_to_9cm",   0.03, 0.09),
    ("soil_moisture_9_to_27cm",  0.09, 0.27),
    ("soil_moisture_27_to_81cm", 0.27, 0.81),
]
OM_SM_LAYERS_ARCHIVE = [
    ("soil_moisture_0_to_7cm",    0.00, 0.07),
    ("soil_moisture_7_to_28cm",   0.07, 0.28),
    ("soil_moisture_28_to_100cm", 0.28, 1.00),
]


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


def _depth_weighted_sm(layer_means: Dict[str, Optional[float]],
                       layer_bounds, Zr: float) -> Optional[float]:
    """Weegt per-laag bodemvocht (m³/m³) over de wortelzone [0, Zr] m.

    `layer_bounds` is een lijst van (key, top, bot) in meter. Elke laag
    krijgt een gewicht dat gelijk is aan de overlap tussen [top, bot] en
    [0, Zr], gedeeld door Zr. Lagen geheel buiten de wortelzone wegen 0.
    Geeft None terug als geen enkele laag beschikbaar is."""
    total_weight = 0.0
    total = 0.0
    for key, top, bot in layer_bounds:
        v = layer_means.get(key)
        if v is None:
            continue
        overlap = max(0.0, min(bot, Zr) - max(top, 0.0))
        if overlap <= 0:
            continue
        w = overlap / Zr
        total += v * w
        total_weight += w
    if total_weight <= 0:
        return None
    # Niet alle lagen aanwezig? Schaal terug naar het gewicht dat we wél
    # hadden — beter een schatting uit beschikbare lagen dan None.
    return round(total / total_weight, 4)


def effective_forecast_rain(days: List[Dict], zone_key: str,
                             horizon: int = 7) -> Dict:
    """Verwacht netto regen voor de komende `horizon` voorspeldagen.

    Per dag:
      1. raw = d["precip"]
      2. expected = raw × probability/100 (default 100% als ontbreekt)
      3. intercepted via canopy-saturation curve (zie run_water_balance)
      4. net = max(0, expected − intercepted)

    Returns dict met som en per-dag componenten zodat de beslissings-
    logica en het dashboard dezelfde feiten zien."""
    interception_mm = INTERCEPTION.get(zone_key, 0.0)
    forecast_days = [d for d in days if d.get("forecast")][:horizon]
    raw_sum = 0.0
    expected_sum = 0.0
    net_sum = 0.0
    intercepted_sum = 0.0
    for d in forecast_days:
        p_raw = d.get("precip") or 0.0
        prob_pct = d.get("precip_prob")
        prob = (prob_pct / 100.0) if prob_pct is not None else 1.0
        expected = p_raw * prob
        if expected > 0 and interception_mm > 0:
            intercepted = interception_mm * (1 - math.exp(-expected / interception_mm))
        else:
            intercepted = 0.0
        net = max(0.0, expected - intercepted)
        raw_sum += p_raw
        expected_sum += expected
        net_sum += net
        intercepted_sum += intercepted
    return {
        "horizon": len(forecast_days),
        "raw_mm": round(raw_sum, 2),
        "expected_mm": round(expected_sum, 2),
        "intercepted_mm": round(intercepted_sum, 2),
        "net_mm": round(net_sum, 2),
    }


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
        if rain_raw > 0 and interception_mm > 0:
            intercepted = interception_mm * (1 - math.exp(-rain_raw / interception_mm))
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


def _hourly_daily_means(hourly: Optional[Dict], var_keys: List[str]) -> Dict[str, Dict[str, float]]:
    """Aggregeert per-uur Open-Meteo variabelen naar dagelijkse gemiddeldes.

    Returns: {var_key: {"YYYY-MM-DD": mean, ...}}. Mist-uren tellen niet
    mee; dagen zonder enkele waarde komen niet voor in de output."""
    if not hourly or "time" not in hourly:
        return {k: {} for k in var_keys}
    out: Dict[str, Dict[str, List[float]]] = {k: {} for k in var_keys}
    for i, t in enumerate(hourly["time"]):
        day = t[:10]
        for k in var_keys:
            arr = hourly.get(k)
            if not arr or i >= len(arr):
                continue
            v = arr[i]
            if v is None:
                continue
            out[k].setdefault(day, []).append(v)
    return {k: {day: sum(vals) / len(vals) for day, vals in days.items() if vals}
            for k, days in out.items()}


def _per_zone_sm(layer_means_by_day: Dict[str, Dict[str, float]],
                 layer_bounds, day: str) -> Dict[str, Optional[float]]:
    """Bouwt {zone_key: weighted_sm} voor één dag uit per-laag-per-dag means."""
    per_layer = {key: layer_means_by_day.get(key, {}).get(day)
                 for key, _, _ in layer_bounds}
    return {
        zone_key: _depth_weighted_sm(per_layer, layer_bounds, z["Zr"])
        for zone_key, z in ZONES.items()
    }


def fetch_open_meteo(days_past: int = 30, days_forecast: int = 7) -> List[Dict]:
    sm_layer_keys = [k for k, _, _ in OM_SM_LAYERS_FORECAST]
    hourly_vars = ["precipitation"] + sm_layer_keys
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={UTRECHT_LAT}&longitude={UTRECHT_LON}"
        f"&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
        f"relative_humidity_2m_mean,wind_speed_10m_mean,precipitation_sum,"
        f"precipitation_probability_max,"
        f"shortwave_radiation_sum,et0_fao_evapotranspiration"
        f"&hourly={','.join(hourly_vars)}"
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
    sm_daily = _hourly_daily_means(j.get("hourly"), sm_layer_keys)
    precip_prob = d.get("precipitation_probability_max", [None] * len(d["time"]))
    et0_om = d.get("et0_fao_evapotranspiration", [None] * len(d["time"]))
    out = []
    for i, t in enumerate(d["time"]):
        per_zone = _per_zone_sm(sm_daily, OM_SM_LAYERS_FORECAST, t)
        row = {
            "date": t,
            "Tmax": d["temperature_2m_max"][i],
            "Tmin": d["temperature_2m_min"][i],
            "Tmean": d["temperature_2m_mean"][i],
            "RHmean": d["relative_humidity_2m_mean"][i],
            "u2": (d["wind_speed_10m_mean"][i] or 0) / 3.6 * WIND_2M_FACTOR,
            "Rs": d["shortwave_radiation_sum"][i],
            "precip": rain_today_so_far if t == today else (d["precipitation_sum"][i] or 0),
            "ET0_om": et0_om[i],
            "precip_prob": precip_prob[i] if i < len(precip_prob) else None,
            "era5_theta_lawn": per_zone.get("lawn"),
            "era5_theta_shrubs": per_zone.get("shrubs"),
            "forecast": t > today,
        }
        out.append(row)
    return out


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
    sm_layer_keys = [k for k, _, _ in OM_SM_LAYERS_ARCHIVE]
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={UTRECHT_LAT}&longitude={UTRECHT_LON}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
        f"relative_humidity_2m_mean,wind_speed_10m_mean,precipitation_sum,"
        f"shortwave_radiation_sum"
        f"&hourly={','.join(sm_layer_keys)}"
        f"&timezone=Europe%2FAmsterdam"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    j = r.json()
    d = j["daily"]
    sm_daily = _hourly_daily_means(j.get("hourly"), sm_layer_keys)
    out = []
    for i, t in enumerate(d["time"]):
        per_zone = _per_zone_sm(sm_daily, OM_SM_LAYERS_ARCHIVE, t)
        out.append({
            "date": t,
            "Tmax": d["temperature_2m_max"][i],
            "Tmin": d["temperature_2m_min"][i],
            "Tmean": d["temperature_2m_mean"][i],
            "RHmean": d["relative_humidity_2m_mean"][i],
            "u2": (d["wind_speed_10m_mean"][i] or 0) / 3.6 * WIND_2M_FACTOR,
            "Rs": d["shortwave_radiation_sum"][i],
            "precip": d["precipitation_sum"][i] or 0,
            "era5_theta_lawn": per_zone.get("lawn"),
            "era5_theta_shrubs": per_zone.get("shrubs"),
            "forecast": False,
        })
    return out


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
    """Bepaalt of water geven nodig is, inclusief irrigatievoorstel in mm en minuten.

    Voor de skip-watering beslissing gebruiken we *effectieve* regen
    (probability-gewogen × na canopy-interceptie), niet de ruwe daily
    precipitation_sum — een 7d voorspelling van 8 mm verdeeld als
    1 mm/dag bij 50% kans levert netto bijna geen water in de wortelzone."""
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

    # Effectieve regen (probability × interceptie) over 3d en 7d.
    eff3 = effective_forecast_rain(days, zone, horizon=3)
    eff7 = effective_forecast_rain(days, zone, horizon=7)
    eff_3d = eff3["net_mm"]
    eff_7d = eff7["net_mm"]

    # Huidige deficit in mm (mate van uitputting onder veldcapaciteit).
    AWC_max = (soil["FC"] - soil["WP"]) * zone_info["Zr"] * 1000
    deficit_mm = AWC_max * (dep / 100.0)

    # Irrigatievoorstel
    proposal_mm = irrigation_proposal_mm(zone, dep, soil, zone_info)
    proposal_min = mm_to_minutes(zone, proposal_mm)

    if state == "dry":
        recommendation = "URGENT: water vandaag — bodem in stress."
        priority = "high"
    elif state == "threshold":
        # Skip-rule deficit-relatief: alleen overslaan als 3d eff regen
        # >= 60% van deficit én 7d eff regen >= 90% van deficit. Kleine
        # buien op droge bodem triggeren dus toch een irrigatie.
        skip = (eff_3d >= 0.6 * deficit_mm) and (eff_7d >= 0.9 * deficit_mm)
        if skip:
            recommendation = (
                f"Nog niet water geven — {eff_7d:.1f} mm effectieve regen "
                f"verwacht (7d, dekt deficit {deficit_mm:.1f} mm)."
            )
            priority = "low"
            proposal_mm = 0
            proposal_min = 0
        else:
            recommendation = "Water geven binnen 1–2 dagen."
            priority = "medium"
    elif state == "moist":
        if days_to_stress and days_to_stress <= 3 and eff_3d < 3:
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
        "deficit_mm": round(deficit_mm, 1),
        "days_to_stress": days_to_stress,
        "rain7_mm": round(rain7, 1),
        "eff_rain_3d_mm": eff_3d,
        "eff_rain_7d_mm": eff_7d,
        "eff_rain_intercepted_7d_mm": eff7["intercepted_mm"],
        "recommendation": recommendation,
        "proposal_mm": proposal_mm,
        "proposal_min": proposal_min,
        "irrigation_rate_mm_per_min": IRRIGATION_RATES.get(zone, 0),
        "current": current,
        "zone": zone,
    }

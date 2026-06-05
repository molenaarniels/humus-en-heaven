#!/usr/bin/env python3
"""
airflow_model.py — Ventilatie-digital-twin (Project 8).

Het omgekeerde van Project 6 (de tado raam-koeladvies). Daar vertelt het model
wélke ramen je moet openen en handel jíj. Hier vertel jíj het model welke ramen,
roosters en deuren open/dicht staan, en het model:

  1. voorspelt per kamer de temperatuur ("afgeleide temperaturen") met een
     2-knoops RC-thermisch model gevoed door een meerzone-luchtstroomnetwerk
     (wind + schoorsteeneffect) en zoninstraling dóór het glas;
  2. vergelijkt die voorspelling met de échte tado-temperaturen (read-only uit
     docs/window_data.json) en toont de fout;
  3. kalibreert zijn eigen parameters elke run zodat de fout krimpt — een
     digital twin die beter wordt naarmate hij langer draait.

Daarnaast een *passief* suggestie-luik ("wat zou je openen voor de meeste koeling")
— puur ter info, je hoeft er niet naar te handelen. Géén Telegram in deze versie.

Volledig geïsoleerd: leest docs/window_data.json alleen-lezen (zoals het maaiproject
docs/data.json leest), haalt zélf wind + zon + buitenweer bij Open-Meteo, en niets
anders hangt van dit project af. Geen tado-auth → geen conflict met de roterende
tado-token van Project 6.

Bronnen / env:
  GIST_ID, GIST_TOKEN        — leest de gerapporteerde raam/rooster/deur-log
                               (`house_openings.json`) read-only uit de niet-geheime Gist
  WU_STATION_ID, WU_API_KEY  — optionele buiten-nu-verfijning (biascorrectie)
  DRY_RUN=1                  — schrijf artefacten maar doe verder niets bijzonders

Pure stdlib + requests, geen numpy.
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# Optionele, pure helpers uit naburige modules (géén netwerk/zijeffect bij import).
from wu_bias import correct_temp
from window_advisor import convert_rh, RH_HARD_CAP, RH_COMFORT, ROOM_COMFORT

TZ = ZoneInfo("Europe/Amsterdam")

# ── Bestanden ─────────────────────────────────────────────────────────────────────
HOUSE_FILE     = os.getenv("HOUSE_MODEL_PATH", "house_model.json")
WINDOW_DATA    = os.getenv("WINDOW_DATA_PATH", "docs/window_data.json")
DASHBOARD_FILE = os.getenv("AIRFLOW_DATA_PATH", "docs/airflow_data.json")
LEARNED_FILE   = os.getenv("AIRFLOW_LEARNED_PATH", "docs/airflow_learned.json")
OPENINGS_FILE  = "house_openings.json"   # in de Gist (niet-geheim)

# ── Fysische constanten ─────────────────────────────────────────────────────────────
CP_AIR = 1005.0    # J/(kg·K)
G      = 9.81       # m/s²
P_ATM  = 101325.0   # Pa
R_AIR  = 287.05     # J/(kg·K)

# Kalibratievenster + integratie.
CALIB_WINDOW_H = 30.0    # uur historie waarover we de fout minimaliseren
SUBSTEP_S      = 300.0   # interne tijdstap (s) voor de Euler-integratie (stabiliteit)
RMSE_HISTORY_KEEP = 240  # rollend venster aan kalibratie-RMSE's (leercurve)
LEARN_RATE     = 0.5     # online: schuif deze fractie naar het nieuwe optimum per run

# Leakage (infiltratie) per kamer: een kleine, altijd aanwezige lek naar buiten. Houdt
# het luchtstroomnetwerk goed geconditioneerd (een verder dichte kamer is niet singulier)
# en is fysisch reëel (kieren). m² effectief lekoppervlak.
LEAK_AREA = 0.004

# ── Prior-parameters (vertrekpunt vóór het leren) ───────────────────────────────────
# Alles is een dimensieloze schaal × een fysische basis, zodat het leren rond 1.0 speelt
# en geclamped blijft in een fysiek plausibele band.
PRIORS = {
    "cp_shelter":  0.5,   # wind-Cp-amplitude × dit (stedelijk/beschut < 1)
    "cd":          0.62,  # ontladingscoëfficiënt van de openingen
    "vent_eff":    1.0,   # globale ventilatie-effectiviteit (advectieve menging)
    # per-kamer schalen (× de fysische basis afgeleid uit volume/wandoppervlak):
    "c_air":       1.0,   # luchtknoop-warmtecapaciteit
    "c_mass":      1.0,   # massaknoop (wanden/meubels)
    "h_am":        1.0,   # lucht↔massa-koppeling
    "ua_env":      1.0,   # schil-conductie (lucht-gekoppeld deel)
    "ua_mass":     1.0,   # schil-conductie naar de massaknoop
    "solar_gain":  1.0,   # zonwinst dóór het glas
}
# Clamp-banden voor de leerbare schalen (ondergrens, bovengrens).
BOUNDS = {
    "cp_shelter": (0.1, 1.2), "cd": (0.3, 0.9), "vent_eff": (0.3, 2.0),
    "c_air": (0.3, 4.0), "c_mass": (0.2, 6.0), "h_am": (0.2, 5.0),
    "ua_env": (0.2, 5.0), "ua_mass": (0.2, 5.0), "solar_gain": (0.0, 3.0),
}
# Welke parameters per kamer leren (h_am/ua_mass blijven op hun prior — minder vrijheid,
# stabieler leren). De rest is globaal.
PER_ROOM_PARAMS = ["c_air", "c_mass", "ua_env", "solar_gain"]
GLOBAL_PARAMS   = ["cp_shelter", "cd", "vent_eff"]


# ════════════════════════════════════════════════════════════════════════════════════
#  Lineaire algebra — kleine dichte Gauss-eliminatie (geen numpy)
# ════════════════════════════════════════════════════════════════════════════════════

def solve_linear(A: list[list[float]], b: list[float]) -> list[float] | None:
    """Los A·x = b op met partieel pivoteren. None bij (bijna-)singulier."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(n):
            if r == col:
                continue
            f = M[r][col] / pv
            if f:
                for c in range(col, n + 1):
                    M[r][c] -= f * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


# ════════════════════════════════════════════════════════════════════════════════════
#  Zonpositie — NOAA-algoritme (puur stdlib math)
# ════════════════════════════════════════════════════════════════════════════════════

def sun_position(lat: float, lon: float, when_utc: datetime) -> tuple[float, float]:
    """(azimut °, elevatie °) van de zon. Azimut met de klok mee vanaf noord (0=N,
    90=O, 180=Z, 270=W); elevatie boven de horizon (negatief = onder). NOAA."""
    if when_utc.tzinfo is None:
        when_utc = when_utc.replace(tzinfo=timezone.utc)
    u = when_utc.astimezone(timezone.utc)
    doy = u.timetuple().tm_yday
    hour = u.hour + u.minute / 60.0 + u.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (doy - 1 + (hour - 12.0) / 24.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    time_offset = eqtime + 4.0 * lon                  # minuten
    tst = (hour * 60.0 + time_offset) % 1440.0        # echte zonnetijd, minuten
    ha = math.radians(tst / 4.0 - 180.0)              # uurhoek, rad
    lat_r = math.radians(lat)
    cos_zen = (math.sin(lat_r) * math.sin(decl)
               + math.cos(lat_r) * math.cos(decl) * math.cos(ha))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zenith = math.acos(cos_zen)
    el = 90.0 - math.degrees(zenith)
    sin_zen = math.sin(zenith)
    if sin_zen < 1e-6:
        return 180.0, el
    cos_az = (math.sin(lat_r) * cos_zen - math.sin(decl)) / (math.cos(lat_r) * sin_zen)
    cos_az = max(-1.0, min(1.0, cos_az))
    az_core = math.degrees(math.acos(cos_az))
    az = (az_core + 180.0) % 360.0 if math.degrees(ha) > 0 else (540.0 - az_core) % 360.0
    return az, el


def facade_irradiance(facade_az: float, sun_az: float, sun_el: float,
                      direct: float, diffuse: float, tilt_deg: float = 90.0) -> float:
    """Instraling (W/m²) op een vlak met azimut `facade_az` en helling `tilt_deg` vanaf
    horizontaal (90 = verticaal raam, 0 = plat dakraam/skylight). Directe component via de
    invalshoek op het hellende vlak; diffuus via de hemelkoepel-viewfactor (1+cos β)/2."""
    beta = math.radians(tilt_deg)
    sky_view = (1.0 + math.cos(beta)) / 2.0          # diffuse view factor (0.5 verticaal, 1.0 plat)
    if sun_el <= 0:
        return max(0.0, (diffuse or 0.0) * sky_view)
    zen = math.radians(90.0 - sun_el)
    daz = math.radians(((sun_az - facade_az + 180.0) % 360.0) - 180.0)
    # cos(invalshoek) op een vlak met helling β: standaard zon-op-vlak-formule.
    cos_inc = math.cos(zen) * math.cos(beta) + math.sin(zen) * math.sin(beta) * math.cos(daz)
    direct_on = max(0.0, (direct or 0.0) * max(0.0, cos_inc))
    return direct_on + (diffuse or 0.0) * sky_view


# ════════════════════════════════════════════════════════════════════════════════════
#  Wind — Cp-druk per gevel
# ════════════════════════════════════════════════════════════════════════════════════

def cp_coefficient(theta_deg: float) -> float:
    """Surface-averaged druk-coëfficiënt voor een laagbouwgevel als functie van de
    invalshoek θ (0° = wind recht op de gevel → loef; 180° = lij). Twee-cosinus-fit
    op tabel-Cp's: loef ≈ +0.7, zijgevel ≈ −0.4, lij ≈ −0.25. Nog ongeschaald door
    de (leerbare) beschuttingsfactor."""
    t = math.radians(theta_deg)
    return 0.475 * math.cos(t) + 0.3125 * math.cos(2 * t) - 0.0875


def wind_pressure(facade_az: float, height: float, wind_speed: float,
                  wind_dir: float, shelter: float, rho: float) -> float:
    """Externe winddruk (Pa) op een opening: Cp·½ρU_lokaal². `wind_dir` = richting
    waar de wind vandaan komt (meteorologisch). U_lokaal via een power-law-profiel
    naar de openingshoogte (stedelijke ruwheid)."""
    theta = abs(((wind_dir - facade_az + 180.0) % 360.0) - 180.0)
    z = max(1.5, height)
    u_local = (wind_speed or 0.0) * (z / 10.0) ** 0.30
    return shelter * cp_coefficient(theta) * 0.5 * rho * u_local * u_local


# ════════════════════════════════════════════════════════════════════════════════════
#  Luchtdichtheid + orifice-flow
# ════════════════════════════════════════════════════════════════════════════════════

def air_density(temp_c: float) -> float:
    return P_ATM / (R_AIR * (temp_c + 273.15))


# Zonwering-transmissie per `shading`-label (fractie zon die het glas haalt): geen, een
# balkon/overstek erboven, lamella ~1/3 dicht, diep beschaduwd (b.v. onder een terras,
# alleen ochtendzon), binnenzonwering/lamella dicht, of een buitenscherm uitgerold.
SHADING_FACTOR = {"none": 1.0, "overhang": 0.7, "lamella": 0.67, "deep": 0.4,
                  "blind": 0.35, "shade": 0.2}

# Crossover-drukval (Pa) tussen het laminaire (lineaire) en turbulente (√) regime. De
# orifice-wet Q∝√|ΔP| heeft een oneindige helling bij ΔP=0, wat de Newton-Jacobiaan
# slecht conditioneert (een grote opening egaliseert de druk → ΔP≈0). Onder DP_LAM gaan
# we lineair over met aansluitende waarde+helling → eindige Jacobiaan, stabiele convergentie
# (standaard CONTAM/AIRNET-aanpak). Fysisch ook reëel: bij heel kleine ΔP is de stroming
# laminair, niet turbulent.
DP_LAM = 0.1


def _massflow(dP: float, Cd: float, area: float, rho_from: float, rho_to: float) -> float:
    """Massadebiet (kg/s) door een opening, positief = uít de 'from'-zone. dP = druk
    aan de from-kant min de to-kant (Pa). Twee-regime (laminair onder DP_LAM)."""
    if area <= 0.0:
        return 0.0
    rho = rho_from if dP >= 0.0 else rho_to
    coef = Cd * area * math.sqrt(2.0 * rho)      # mdot = coef·√|ΔP| in het turbulente regime
    a = abs(dP)
    if a >= DP_LAM:
        m = coef * math.sqrt(a)
    else:
        m = coef * math.sqrt(DP_LAM) * (a / DP_LAM)   # lineair, aansluitend op DP_LAM
    return m if dP >= 0.0 else -m


# ════════════════════════════════════════════════════════════════════════════════════
#  Luchtstroomnetwerk — los de zone-drukken op zodat massa behouden blijft
# ════════════════════════════════════════════════════════════════════════════════════

def solve_network(zones: list[str], openings: list[dict], zone_temps: dict[str, float],
                  outside_temp: float, P_init: list[float] | None = None) -> dict:
    """Los het meerzone-netwerk op. `openings` is een lijst dicts:
        {"a": zone, "b": zone|"outside", "area": m², "Cd": -, "z": m, "Pe": Pa}
    Pe is de externe winddruk aan de buitenkant (alleen voor exterieuropeningen; 0 voor
    interne deuren). `P_init` = warme-start-drukken (vorige stap) → minder iteraties.
    Geeft terug: {"flows": [m³/s a→b per opening], "fresh": {zone: m³/s verse buitenlucht
    in}, "pressures": {zone: Pa}, "P": [Pa per zone]}."""
    idx = {z: i for i, z in enumerate(zones)}
    n = len(zones)
    rho_out = air_density(outside_temp)
    rho_z = {z: air_density(zone_temps.get(z, outside_temp)) for z in zones}

    def residual(P: list[float]) -> list[float]:
        res = [0.0] * n
        for op in openings:
            a = op["a"]
            ia = idx[a]
            z = op["z"]
            rho_a = rho_z[a]
            Pa_eff = P[ia] - rho_a * G * z
            if op["b"] == "outside":
                Pb_eff = op["Pe"] - rho_out * G * z
                rho_b = rho_out
            else:
                ib = idx[op["b"]]
                rho_b = rho_z[op["b"]]
                Pb_eff = P[ib] - rho_b * G * z
            md = _massflow(Pa_eff - Pb_eff, op["Cd"], op["area"], rho_a, rho_b)
            res[ia] += md
            if op["b"] != "outside":
                res[idx[op["b"]]] -= md
        return res

    def sse(r):
        return sum(v * v for v in r)

    P = list(P_init) if P_init and len(P_init) == n else [0.0] * n
    r = residual(P)
    for _ in range(40):
        if max(abs(v) for v in r) < 1e-6:
            break
        # Numerieke Jacobiaan.
        J = [[0.0] * n for _ in range(n)]
        eps = 0.02
        for j in range(n):
            P[j] += eps
            rp = residual(P)
            P[j] -= eps
            for i in range(n):
                J[i][j] = (rp[i] - r[i]) / eps
        delta = solve_linear(J, [-v for v in r])
        if delta is None:
            break
        # Backtracking line search: neem de grootste stapfractie die de residu-norm
        # daadwerkelijk verkleint. Zonder dit kan de Newton-iteratie tussen twee
        # toestanden oscilleren (sterke deurkoppeling + de √-niet-lineariteit) en nooit
        # convergeren.
        sse0 = sse(r)
        alpha = 1.0
        r_try = r
        for _ in range(24):
            P_try = [P[j] + alpha * delta[j] for j in range(n)]
            r_try = residual(P_try)
            if sse(r_try) < sse0:
                break
            alpha *= 0.5
        else:
            break   # geen verbeterende stap meer → klaar
        P = P_try
        r = r_try

    # Debieten + verse-lucht-aanvoer reconstrueren.
    flows = []
    fresh = {z: 0.0 for z in zones}
    for op in openings:
        a = op["a"]
        ia = idx[a]
        z = op["z"]
        rho_a = rho_z[a]
        Pa_eff = P[ia] - rho_a * G * z
        if op["b"] == "outside":
            Pb_eff = op["Pe"] - rho_out * G * z
            rho_b = rho_out
        else:
            rho_b = rho_z[op["b"]]
            Pb_eff = P[idx[op["b"]]] - rho_b * G * z
        md = _massflow(Pa_eff - Pb_eff, op["Cd"], op["area"], rho_a, rho_b)
        q_vol = md / (rho_a if md >= 0 else rho_b)   # m³/s, positief a→b
        flows.append(q_vol)
        if op["b"] == "outside" and md < 0:          # buitenlucht stroomt zone a in
            fresh[a] += -md / rho_out
        elif op["b"] != "outside":
            # interne deur: telt niet als 'verse' lucht, maar wel voor menging (apart).
            pass
    return {"flows": flows, "fresh": fresh,
            "pressures": {z: P[idx[z]] for z in zones}, "P": P}


# ════════════════════════════════════════════════════════════════════════════════════
#  Openingen — lognaar-toestand reconstrueren + effectief oppervlak
# ════════════════════════════════════════════════════════════════════════════════════

def _open_frac(value, element: dict) -> float:
    """Zet een gerapporteerde waarde (getal 0..1, of "open"/"tilt"/"closed"/"dicht")
    om naar een open-fractie."""
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    s = str(value).strip().lower()
    if s in ("open", "1", "true", "ja"):
        return 1.0
    if s in ("tilt", "kier", "kiep"):
        return float(element.get("tilt_frac", 0.15))
    if s in ("closed", "dicht", "0", "false", "nee"):
        return 0.0
    return 0.0


def openings_at(log: list[dict], when: datetime) -> dict:
    """Actieve gerapporteerde toestanden op tijdstip `when`: de laatste snapshot met
    tijdstempel ≤ when. Lege dict als er niets vóór `when` is gelogd."""
    best, best_t = {}, None
    for entry in log:
        try:
            t = datetime.fromisoformat(entry["t"])
        except (ValueError, TypeError, KeyError):
            continue
        if t <= when and (best_t is None or t > best_t):
            best, best_t = entry.get("states", {}) or {}, t
    return best


def _default_frac(element: dict, kind: str) -> float:
    """Basistoestand vóór enige rapportage: ramen dicht, binnendeuren open, roosters
    op trickle-stand (tenzij het huismodel een `default_state` geeft)."""
    if "default_state" in element:
        return _open_frac(element["default_state"], element)
    return {"window": 0.0, "vent": 1.0, "door": 1.0}.get(kind, 0.0)


def build_openings(house: dict, states: dict, weather: dict, params: dict,
                   zone_temps: dict, outside_temp: float) -> list[dict]:
    """Bouw de openingenlijst voor het netwerk uit de huidige toestanden + wind."""
    shelter = params["cp_shelter"]
    cd = params["cd"]
    rho_out = air_density(outside_temp)
    wind_s, wind_d = weather.get("wind_speed", 0.0), weather.get("wind_dir", 0.0)
    ops = []

    def ext(elem_id, elem, kind):
        frac = _open_frac(states[elem_id], elem) if elem_id in states else _default_frac(elem, kind)
        area = frac * elem.get("max_open_area_m2", elem.get("area_m2", 0.0))
        if area <= 0:
            return
        pe = wind_pressure(elem.get("facade_azimuth_deg", 0.0),
                           elem.get("center_height_m", 1.5), wind_s, wind_d, shelter, rho_out)
        ops.append({"a": elem["room"], "b": "outside", "area": area, "Cd": cd,
                    "z": elem.get("center_height_m", 1.5), "Pe": pe, "id": elem_id})

    for wid, w in house.get("windows", {}).items():
        ext(wid, w, "window")
    for vid, v in house.get("vents", {}).items():
        ext(vid, v, "vent")
    for did, d in house.get("doors", {}).items():
        frac = _open_frac(states[did], d) if did in states else _default_frac(d, "door")
        area = frac * d.get("area_m2", 0.0)
        if area <= 0:
            continue
        a, b = d["between"]
        ops.append({"a": a, "b": b, "area": area, "Cd": cd,
                    "z": d.get("center_height_m", 1.0), "Pe": 0.0, "id": did})
    # Per-zone infiltratielek (altijd aanwezig, klein) → het netwerk blijft welgesteld,
    # óók als een zone helemaal dicht zit (b.v. badkamer: deur dicht + afzuiging uit) —
    # anders wordt die knoop singulier en ontspoort de hele drukoplossing.
    for zone in list(house.get("rooms", {})) + list(house.get("junctions", {})):
        ops.append({"a": zone, "b": "outside", "area": LEAK_AREA, "Cd": cd,
                    "z": 1.5, "Pe": 0.0, "id": f"_leak_{zone}"})
    return ops


def door_mix(house: dict, flows: list[float], openings: list[dict]) -> dict:
    """Per (kamer→kamer) het absolute volumedebiet door open binnendeuren (m³/s),
    voor de advectieve thermische menging."""
    mix = {}
    for op, q in zip(openings, flows):
        if op["b"] == "outside":
            continue
        a, b = op["a"], op["b"]
        mix.setdefault((a, b), 0.0)
        mix[(a, b)] += abs(q)
    return mix


# ════════════════════════════════════════════════════════════════════════════════════
#  2-knoops RC-thermisch model
# ════════════════════════════════════════════════════════════════════════════════════

def room_base_capacitances(room: dict) -> tuple[float, float, float]:
    """Fysische basis (C_air, C_mass, exterieur-UA) uit geometrie, vóór de leer-schalen.
    C_air ≈ luchtmassa×cp (× factor voor meubilair-lucht); C_mass ≈ wandmassa; UA uit
    schiloppervlak."""
    vol = room.get("volume_m3", 40.0)
    wall = room.get("exterior_wall_m2", 0.4 * vol)
    c_air = vol * 1.2 * CP_AIR * 3.0          # ×3: effectieve binnenlucht + lichte inboedel
    c_mass = wall * 90000.0                     # J/K per m² wand (baksteen/pleister, ~slow)
    ua = wall * 1.0                             # W/K (matig geïsoleerde schil)
    return c_air, c_mass, ua


def _zone_thermal_params(house: dict, params: dict) -> dict:
    """Per zone (kamer én junctie) de geschaalde thermische parameters. Kamers krijgen
    geometrie + geleerde schalen; junct, gang/overloop) generieke defaults (geen zonwinst)."""
    par = {}
    for rid, r in house.get("rooms", {}).items():
        c_air0, c_mass0, ua0 = room_base_capacitances(r)
        p = params.get(rid, {})
        par[rid] = {
            "C_a": c_air0 * p.get("c_air", 1.0),
            "C_m": c_mass0 * p.get("c_mass", 1.0),
            "H_am": ua0 * 8.0 * p.get("h_am", 1.0),
            "UA_env": ua0 * 0.5 * p.get("ua_env", 1.0),
            "UA_mass": ua0 * 0.5 * p.get("ua_mass", 1.0),
            "solar": p.get("solar_gain", 1.0), "f_air": 0.4,
        }
    for jid, j in house.get("junctions", {}).items():
        vol = j.get("volume_m3", 15.0)
        c_air0 = vol * 1.2 * CP_AIR * 3.0
        par[jid] = {"C_a": c_air0, "C_m": c_air0 * 2.0, "H_am": 15.0,
                    "UA_env": 3.0, "UA_mass": 1.0, "solar": 0.0, "f_air": 1.0}
    return par


def simulate(house: dict, params: dict, timeline: list[dict],
             seed: dict, calib_only_rooms: set | None = None) -> dict:
    """Integreer het 2-knoops thermische model over `timeline` (lijst stappen met drivers).
    Elke stap: {"t", "T_out", "irr": {room: W}, "states", "weather", "dt"}. `seed` =
    {zone: T_start °C}. Geeft per sensorkamer de voorspelde luchttemp-reeks terug.

    De integratie is *impliciet* (backward Euler): per substap wordt het gekoppelde
    lineaire stelsel voor alle lucht- + massaknopen ineens opgelost (solve_linear). Dat
    is onvoorwaardelijk stabiel — sterke deur-/ventilatiekoppeling laat de expliciete
    Euler anders ontsporen."""
    rooms = house.get("rooms", {})
    zones = list(rooms.keys()) + list(house.get("junctions", {}).keys())
    par = _zone_thermal_params(house, params)
    veff = params.get("vent_eff", 1.0)
    rho_cp = 1.2 * CP_AIR

    Ta = {z: seed.get(z, timeline[0]["T_out"]) for z in zones}
    Tm = dict(Ta)
    out = {rid: [] for rid in rooms if (calib_only_rooms is None or rid in calib_only_rooms)}

    n = len(zones)
    zi = {z: k for k, z in enumerate(zones)}
    P_warm = None

    for step in timeline:
        T_out = step["T_out"]
        ops = build_openings(house, step["states"], step["weather"], params, Ta, T_out)
        net = solve_network(zones, ops, Ta, T_out, P_init=P_warm)
        P_warm = net["P"]
        fresh = net["fresh"]
        mix = door_mix(house, net["flows"], ops)
        # Advectieve geleiding per (zone↔zone) deur (W/K) en per zone naar buiten (vent).
        gdoor = {key: rho_cp * qm * veff for key, qm in mix.items()}
        gvent = {z: rho_cp * fresh.get(z, 0.0) * veff for z in zones}

        nsub = max(1, int(math.ceil(step["dt"] / SUBSTEP_S)))
        h = step["dt"] / nsub
        for _ in range(nsub):
            # Bouw het 2N-stelsel A·x = b, x = [Ta_z0, Tm_z0, Ta_z1, Tm_z1, ...].
            A = [[0.0] * (2 * n) for _ in range(2 * n)]
            b = [0.0] * (2 * n)
            for z in zones:
                k = zi[z]
                ia, im = 2 * k, 2 * k + 1
                pa = par[z]
                q_solar = step["irr"].get(z, 0.0) * pa["solar"]
                # Luchtknoop.
                A[ia][ia] += pa["C_a"] / h + gvent[z] + pa["UA_env"] + pa["H_am"]
                A[ia][im] += -pa["H_am"]
                b[ia] += pa["C_a"] / h * Ta[z] + gvent[z] * T_out + pa["UA_env"] * T_out + pa["f_air"] * q_solar
                # Massaknoop.
                A[im][im] += pa["C_m"] / h + pa["H_am"] + pa["UA_mass"]
                A[im][ia] += -pa["H_am"]
                b[im] += pa["C_m"] / h * Tm[z] + pa["UA_mass"] * T_out + (1.0 - pa["f_air"]) * q_solar
            # Deur-koppeling (advectief, impliciet).
            for (za, zb), g in gdoor.items():
                ka, kb = zi[za], zi[zb]
                A[2 * ka][2 * ka] += g
                A[2 * ka][2 * kb] += -g
                A[2 * kb][2 * kb] += g
                A[2 * kb][2 * ka] += -g
            x = solve_linear(A, b)
            if x is None:
                break
            for z in zones:
                k = zi[z]
                Ta[z] = x[2 * k]
                Tm[z] = x[2 * k + 1]
        for rid in out:
            out[rid].append((step["t"], Ta[rid]))
    return {"series": out, "Ta": dict(Ta), "Tm": dict(Tm)}


# ════════════════════════════════════════════════════════════════════════════════════
#  Kalibratie — gedempte Gauss-Newton op de leerbare schalen
# ════════════════════════════════════════════════════════════════════════════════════

def _param_keys(rooms: list[str]) -> list[tuple]:
    keys = [("global", g) for g in GLOBAL_PARAMS]
    for rid in rooms:
        for p in PER_ROOM_PARAMS:
            keys.append((rid, p))
    return keys


def params_to_vec(params: dict, keys: list[tuple]) -> list[float]:
    out = []
    for scope, name in keys:
        out.append(params[name] if scope == "global" else params[scope][name])
    return out


def vec_to_params(vec: list[float], keys: list[tuple], base: dict) -> dict:
    p = json.loads(json.dumps(base))   # diepe kopie
    for v, (scope, name) in zip(vec, keys):
        lo, hi = BOUNDS[name]
        v = max(lo, min(hi, v))
        if scope == "global":
            p[name] = v
        else:
            p.setdefault(scope, {})[name] = v
    return p


def _residuals(house, params, timeline, seed, actual, rooms_set) -> list[float]:
    """Voorspeld − werkelijk op elk meetmoment (lineair geïnterpoleerd op de
    voorspelde reeks)."""
    sim = simulate(house, params, timeline, seed, calib_only_rooms=rooms_set)
    res = []
    for rid, samples in actual.items():
        pred = sim["series"].get(rid, [])
        if not pred:
            continue
        for ts, val in samples:
            res.append(_interp(pred, ts) - val)
    return res


def _interp(series: list[tuple], ts: datetime) -> float:
    """Lineaire interpolatie van (t, waarde)-reeks op tijdstip ts."""
    if ts <= series[0][0]:
        return series[0][1]
    if ts >= series[-1][0]:
        return series[-1][1]
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if t0 <= ts <= t1:
            f = (ts - t0).total_seconds() / max(1.0, (t1 - t0).total_seconds())
            return v0 + f * (v1 - v0)
    return series[-1][1]


def rmse(res: list[float]) -> float:
    return math.sqrt(sum(r * r for r in res) / len(res)) if res else float("nan")


def calibrate(house, params, timeline, seed, actual, max_iter: int = 5,
              time_budget_s: float = 25.0) -> tuple[dict, float]:
    """Minimaliseer Σ(voorspeld−werkelijk)² over het venster met gedempte Gauss-Newton.
    Online: schuif maar LEARN_RATE naar het optimum (stabiel, convergeert over runs).
    Geeft (nieuwe params, RMSE-na)."""
    rooms = [rid for rid in actual if actual[rid]]
    if not rooms:
        return params, float("nan")
    rooms_set = set(rooms)
    keys = _param_keys(rooms)
    x = params_to_vec(params, keys)
    base = params

    r0 = _residuals(house, params, timeline, seed, actual, rooms_set)
    best_rmse = rmse(r0)
    if not r0:
        return params, float("nan")

    t_start = time.time()
    lam = 1e-3
    for _ in range(max_iter):
        if time.time() - t_start > time_budget_s:
            break
        p_cur = vec_to_params(x, keys, base)
        r = _residuals(house, p_cur, timeline, seed, actual, rooms_set)
        if not r:
            break
        m = len(r)
        # Jacobiaan via voorwaartse differentie (één simulatie per parameter).
        J = [[0.0] * len(keys) for _ in range(m)]
        for j in range(len(keys)):
            dx = max(1e-3, abs(x[j]) * 0.05)
            xj = x[:]
            xj[j] += dx
            rj = _residuals(house, vec_to_params(xj, keys, base), timeline, seed, actual, rooms_set)
            if len(rj) != m:
                continue
            for i in range(m):
                J[i][j] = (rj[i] - r[i]) / dx
        # Normaalvergelijkingen (JᵀJ + λI) δ = −Jᵀr.
        nk = len(keys)
        JtJ = [[sum(J[i][a] * J[i][b] for i in range(m)) for b in range(nk)] for a in range(nk)]
        for a in range(nk):
            JtJ[a][a] += lam * (JtJ[a][a] + 1.0)
        Jtr = [sum(J[i][a] * r[i] for i in range(m)) for a in range(nk)]
        delta = solve_linear(JtJ, [-v for v in Jtr])
        if delta is None:
            break
        x_new = [x[j] + delta[j] for j in range(nk)]
        new_rmse = rmse(_residuals(house, vec_to_params(x_new, keys, base), timeline, seed, actual, rooms_set))
        if math.isnan(new_rmse) or new_rmse >= best_rmse:
            lam *= 4.0                      # geen verbetering → meer demping
            if lam > 1e6:
                break
            continue
        lam = max(1e-4, lam / 3.0)
        x = x_new
        best_rmse = new_rmse

    # Online: schuif LEARN_RATE van oud → nieuw zodat één run niet wild uitslaat.
    x_old = params_to_vec(params, keys)
    x_blend = [x_old[j] + LEARN_RATE * (x[j] - x_old[j]) for j in range(len(keys))]
    new_params = vec_to_params(x_blend, keys, base)
    final_rmse = rmse(_residuals(house, new_params, timeline, seed, actual, rooms_set))
    return new_params, final_rmse


# ════════════════════════════════════════════════════════════════════════════════════
#  Passieve suggestie — welke ramen geven nú de meeste koeling
# ════════════════════════════════════════════════════════════════════════════════════

def suggest(house: dict, params: dict, weather: dict, room_now: dict,
            outside_temp: float, outside_rh: float, outside_rh_temp: float) -> dict:
    """Brute-force raam/rooster-combinaties (≤256) en rangschik op nuttige koeling nu,
    met comfort- en vochtbegrenzing. Geeft het beste open-zetje als instructies per
    raam + de top-combinaties. Puur adviserend — er wordt niet naar gehandeld."""
    rooms = list(house.get("rooms", {}).keys())
    zones = rooms + list(house.get("junctions", {}).keys())
    # Alléén de te openen ramen meenemen: een vast raam "openen" doet niets (oppervlak 0),
    # dus zou alleen schijn-variatie in de suggesties geven.
    windows = [(wid, w) for wid, w in house.get("windows", {}).items()
               if w.get("max_open_area_m2", 0.0) > 0.0]
    vents = house.get("vents", {})
    if len(windows) > 10:
        windows = windows[:10]   # houd de enumeratie behapbaar (2^n)

    zone_temps = {z: room_now.get(_wd_key(house, z), outside_temp) for z in zones}

    def comfort(room_id):
        wd = _wd_key(house, room_id)
        return ROOM_COMFORT.get(wd, (19.5, 23.5))

    def score(states):
        ops = build_openings(house, states, weather, params, zone_temps, outside_temp)
        net = solve_network(zones, ops, zone_temps, outside_temp)
        total = 0.0
        for room_id in rooms:
            t_in = room_now.get(_wd_key(house, room_id))
            if t_in is None:
                continue
            fresh = net["fresh"].get(room_id, 0.0)
            low, high = comfort(room_id)
            # Vocht-veto: nooit lucht binnenhalen die de kamer voorbij RH_HARD_CAP duwt.
            vent_rh = convert_rh(outside_rh, outside_rh_temp, t_in)
            if fresh > 1e-4 and vent_rh is not None and vent_rh >= RH_HARD_CAP:
                total -= 50.0 * fresh
                continue
            cooling = 1.2 * CP_AIR * fresh * (t_in - outside_temp)
            if t_in > high and outside_temp < t_in:
                total += max(0.0, cooling)
            elif t_in <= low and fresh > 1e-4:
                total -= 0.5 * 1.2 * CP_AIR * fresh * max(0.0, t_in - outside_temp)  # niet doorkoelen
        # Kleine vaste kost per open raam: breekt gelijkspel richting zo wéinig mogelijk
        # ramen, zodat thermisch-neutrale ramen (buiten even warm/koeler dan binnen, geen
        # netto koeling) dícht blijven i.p.v. "gratis" mee te openen. Verwaarloosbaar t.o.v.
        # echte koeling (honderden W).
        n_open = sum(1 for wid, _ in windows if _open_frac(states.get(wid, 0), {}) > 0.5)
        total -= 0.5 * n_open
        # Veiligheid: ontmoedig wijd open bij harde wind/regen.
        if weather.get("gust", 0.0) > 14.0 or weather.get("precip", 0.0) > 0.3:
            total -= 5.0 * n_open
        return total

    # Vaste basis voor roosters/deuren (trickle / open).
    base_states = {}
    for vid in vents:
        base_states[vid] = "open"

    best, ranked = None, []
    for combo in range(1 << len(windows)):
        states = dict(base_states)
        for bit, (wid, _) in enumerate(windows):
            states[wid] = "open" if combo & (1 << bit) else "closed"
        s = score(states)
        open_ids = [wid for bit, (wid, _) in enumerate(windows) if combo & (1 << bit)]
        ranked.append({"windows": open_ids, "score": round(s, 1)})
        if best is None or s > best["score"]:
            best = {"windows": open_ids, "score": round(s, 1), "states": states}
    ranked.sort(key=lambda r: -r["score"])

    # Diverse top-K: filter bijna-identieke combinaties (verschillen in ≤1 raam) eruit, zodat
    # de lijst écht andere strategieën toont i.p.v. vier varianten op hetzelfde. Voeg de
    # "alles dicht"-basislijn toe als ijkpunt. Geef per combinatie de betrokken kamers mee.
    diverse = []
    for cand in ranked:
        cs = set(cand["windows"])
        if cs and any(len(cs ^ set(d["windows"])) <= 1 for d in diverse):
            continue
        cand = dict(cand)
        cand["rooms"] = sorted({house["windows"][w]["room"] for w in cand["windows"]})
        diverse.append(cand)
        if len(diverse) >= 5:
            break
    if not any(not d["windows"] for d in diverse):
        diverse.append({"windows": [], "rooms": [], "score": round(score(dict(base_states)), 1)})

    instructions = []
    for wid, w in house.get("windows", {}).items():
        do_open = best is not None and wid in best["windows"]
        instructions.append({
            "window": wid, "room": w.get("room"),
            "action": "open" if do_open else "dicht",
            "label": w.get("label", wid),
        })
    keep_closed = best is None or best["score"] <= 0.1
    return {"instructions": instructions, "ranked": diverse,
            "keep_closed": keep_closed,
            "headline": _suggest_headline(house, best, keep_closed)}


def _suggest_headline(house, best, keep_closed) -> str:
    if keep_closed or not best or not best["windows"]:
        return "Alles dicht houden — buiten geeft nu geen nuttige koeling."
    labels = [house["windows"][w].get("label", w) for w in best["windows"]]
    return "Voor de meeste koeling: open " + " + ".join(labels) + "."


def _wd_key(house: dict, room_id: str) -> str:
    return house.get("rooms", {}).get(room_id, {}).get("from_window_data", room_id)


# ════════════════════════════════════════════════════════════════════════════════════
#  I/O — huismodel, window_data.json, Gist-openingenlog, geleerde params, weer
# ════════════════════════════════════════════════════════════════════════════════════

def load_house() -> dict:
    with open(HOUSE_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_window_data() -> dict:
    try:
        with open(WINDOW_DATA, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"[window_data] {WINDOW_DATA} ontbreekt/onleesbaar — kamers leeg.")
        return {}


def load_learned() -> dict:
    try:
        with open(LEARNED_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def default_params(house: dict) -> dict:
    p = {k: PRIORS[k] for k in GLOBAL_PARAMS}
    for rid in house.get("rooms", {}):
        p[rid] = {k: PRIORS[k] for k in PER_ROOM_PARAMS}
    return p


def load_openings_log() -> list[dict]:
    gist_id = os.getenv("GIST_ID")
    token = os.getenv("GIST_TOKEN")
    if not gist_id or not token:
        print("[openings] geen GIST_ID/GIST_TOKEN — lege log.")
        return []
    try:
        r = requests.get(f"https://api.github.com/gists/{gist_id}",
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/vnd.github+json"}, timeout=20)
        r.raise_for_status()
        content = (r.json().get("files", {}).get(OPENINGS_FILE, {}) or {}).get("content")
        if not content:
            return []
        return json.loads(content).get("log", [])
    except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
        print(f"[openings] log lezen mislukt: {e}")
        return []


def fetch_weather() -> dict:
    """Open-Meteo: verleden (voor het leren) + forecast, met wind incl. richting en de
    zoncomponenten voor de instraling door het glas."""
    lat, lon = _LAT, _LON
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ("temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,"
                   "wind_direction_10m,wind_gusts_10m,shortwave_radiation,"
                   "direct_radiation,diffuse_radiation"),
        "current": ("temperature_2m,relative_humidity_2m,wind_speed_10m,"
                    "wind_direction_10m,wind_gusts_10m,shortwave_radiation,direct_radiation"),
        "wind_speed_unit": "ms",
        "timezone": "Europe/Amsterdam",
        "past_days": 3, "forecast_days": 2,
    }
    data = None
    for attempt, delay in [(1, 0), (2, 3), (3, 8)]:
        if delay:
            time.sleep(delay)
        try:
            r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=25)
            r.raise_for_status()
            data = r.json()
            break
        except requests.exceptions.RequestException as e:
            print(f"[open-meteo] poging {attempt}/3 mislukt: {e}")
            if attempt == 3:
                raise
    h = data.get("hourly", {})
    times = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in h.get("time", [])]
    rows = []
    for i, t in enumerate(times):
        rows.append({
            "dt": t,
            "T_out": _get(h, "temperature_2m", i),
            "rh": _get(h, "relative_humidity_2m", i),
            "precip": _get(h, "precipitation", i) or 0.0,
            "wind_speed": _get(h, "wind_speed_10m", i) or 0.0,
            "wind_dir": _get(h, "wind_direction_10m", i) or 0.0,
            "gust": _get(h, "wind_gusts_10m", i) or 0.0,
            "shortwave": _get(h, "shortwave_radiation", i) or 0.0,
            "direct": _get(h, "direct_radiation", i) or 0.0,
            "diffuse": _get(h, "diffuse_radiation", i) or 0.0,
        })
    cur = data.get("current", {}) or {}
    return {"hourly": rows, "current": cur}


def _get(h: dict, key: str, i: int):
    arr = h.get(key) or []
    return arr[i] if i < len(arr) else None


# ════════════════════════════════════════════════════════════════════════════════════
#  Timeline-opbouw + dashboard
# ════════════════════════════════════════════════════════════════════════════════════

def build_timeline(house: dict, weather: dict, log: list[dict], now: datetime,
                   window_h: float) -> list[dict]:
    """Bouw een 15-minuten-raster van drivers over het kalibratievenster t/m nu,
    plus een korte vooruitblik. Per stap: T_out, per-kamer instraling (door het glas),
    wind, en de gerapporteerde openingen-toestand op dat moment."""
    rows = [r for r in weather["hourly"] if r["T_out"] is not None]
    if not rows:
        return []
    start = now - _timedelta_h(window_h)
    grid = []
    t = start
    end = now + _timedelta_h(2.0)   # korte vooruitblik voor de afgeleide-temp-projectie
    lat, lon = _LAT, _LON
    while t <= end:
        T_out = _interp_hourly(rows, t, "T_out")
        wx = {k: _interp_hourly(rows, t, k) for k in
              ("wind_speed", "wind_dir", "gust", "precip", "direct", "diffuse", "rh")}
        sun_az, sun_el = sun_position(lat, lon, t.astimezone(timezone.utc))
        irr = {}
        for rid, room in house.get("rooms", {}).items():
            tot = 0.0
            for wid, w in house.get("windows", {}).items():
                if w.get("room") != rid:
                    continue
                shade = SHADING_FACTOR.get(w.get("shading", "none"), 1.0)
                I = facade_irradiance(w.get("facade_azimuth_deg", 0.0), sun_az, sun_el,
                                      wx["direct"], wx["diffuse"], w.get("tilt_deg", 90.0))
                tot += 0.7 * shade * I * w.get("glass_m2", 0.6 * w.get("area_m2", 1.0))
            irr[rid] = tot
        grid.append({"t": t, "T_out": T_out, "irr": irr, "states": openings_at(log, t),
                     "weather": wx, "dt": 900.0, "sun_az": sun_az, "sun_el": sun_el})
        t = t + _timedelta_h(0.25)
    return grid


def collect_actual(house: dict, wd: dict, since: datetime) -> dict:
    """Per sensorkamer de werkelijke tado-temp-samples (t, °C) vanaf `since`, uit de
    history in window_data.json (+ de huidige meting)."""
    actual = {}
    for rid, room in house.get("rooms", {}).items():
        wd_key = room.get("from_window_data")
        if not wd_key or wd_key not in wd.get("rooms", {}):
            continue
        rd = wd["rooms"][wd_key]
        samples = []
        for s in rd.get("history", []):
            try:
                ts = datetime.fromisoformat(s["t"])
            except (ValueError, TypeError, KeyError):
                continue
            if ts >= since and s.get("temp") is not None:
                samples.append((ts, s["temp"]))
        samples.sort()
        if samples:
            actual[rid] = samples
    return actual


def build_dashboard(house, params, weather, wd, timeline, sim, sugg, learned,
                    actual, now, rmse_now) -> dict:
    """Stel docs/airflow_data.json samen (additief schema)."""
    cur = weather["current"]
    sun_az, sun_el = sun_position(_LAT, _LON, now.astimezone(timezone.utc))
    out_t = cur.get("temperature_2m")
    out_rh = cur.get("relative_humidity_2m")

    # Huidige openingen-toestand (laatste snapshot).
    states_now = openings_at(load_openings_log_cached(), now) if False else timeline[-1]["states"] if timeline else {}

    rooms_out = {}
    last_step = timeline[-1] if timeline else None
    for rid, room in house.get("rooms", {}).items():
        wd_key = room.get("from_window_data")
        rd = wd.get("rooms", {}).get(wd_key, {}) if wd_key else {}
        pred_series = sim["series"].get(rid, [])
        pred_now = sim["Ta"].get(rid)
        act_now = rd.get("inside")
        err = (pred_now - act_now) if (pred_now is not None and act_now is not None) else None
        fresh_ach = None
        if last_step is not None:
            ops = build_openings(house, last_step["states"], last_step["weather"], params,
                                 sim["Ta"], last_step["T_out"])
            net = solve_network(list(house["rooms"]) + list(house.get("junctions", {})),
                                ops, sim["Ta"], last_step["T_out"])
            vol = room.get("volume_m3", 40.0)
            fresh_ach = round(net["fresh"].get(rid, 0.0) * 3600.0 / vol, 2)
        rooms_out[rid] = {
            "label": room.get("label", rid),
            "from_window_data": wd_key,
            "actual_temp": act_now,
            "predicted_temp": round(pred_now, 2) if pred_now is not None else None,
            "predicted_mass_temp": round(sim["Tm"].get(rid), 2) if sim["Tm"].get(rid) is not None else None,
            "error": round(err, 2) if err is not None else None,
            "humidity": rd.get("humidity"),
            "ach": fresh_ach,
            "solar_w": round(last_step["irr"].get(rid, 0.0), 0) if last_step else None,
            "comfort_low": ROOM_COMFORT.get(wd_key, (None, None))[0] if wd_key else None,
            "comfort_high": ROOM_COMFORT.get(wd_key, (None, None))[1] if wd_key else None,
            "predicted_series": [{"t": t.isoformat(), "temp": round(v, 2)} for t, v in pred_series],
            "actual_series": [{"t": t.isoformat(), "temp": v} for t, v in actual.get(rid, [])],
            "params": params.get(rid, {}),
        }

    rmse_hist = (learned.get("rmse_history") or [])[:]
    if rmse_now == rmse_now:   # niet-NaN
        rmse_hist.append({"t": now.isoformat(), "rmse": round(rmse_now, 3)})
    rmse_hist = rmse_hist[-RMSE_HISTORY_KEEP:]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_local": now.isoformat(),
        "source": "airflow_model",
        "weather": {
            "outside_temp": out_t, "outside_humidity": out_rh,
            "wind_speed": cur.get("wind_speed_10m"), "wind_dir": cur.get("wind_direction_10m"),
            "gust": cur.get("wind_gusts_10m"), "shortwave": cur.get("shortwave_radiation"),
            "sun_az": round(sun_az, 1), "sun_el": round(sun_el, 1),
        },
        "openings": states_now,
        "controls": _controls(house, states_now),
        "rooms": rooms_out,
        "flows": _flow_summary(house, params, sim, timeline),
        "suggestion": sugg,
        "learned": {"params": params, "rmse": round(rmse_now, 3) if rmse_now == rmse_now else None,
                    "rmse_history": rmse_hist},
        "house_meta": {
            "rooms": {rid: {"plan_xy": r.get("plan_xy"), "label": r.get("label", rid),
                            "floor": r.get("floor", 0), "sensor": bool(r.get("from_window_data"))}
                      for rid, r in house.get("rooms", {}).items()},
            "junctions": {jid: {"plan_xy": j.get("plan_xy"), "label": j.get("label", jid),
                                "floor": j.get("floor", 0), "sensor": False}
                          for jid, j in house.get("junctions", {}).items()},
            "windows": {wid: {"room": w.get("room"), "facade_azimuth_deg": w.get("facade_azimuth_deg"),
                              "kind": "skylight" if w.get("tilt_deg", 90) < 45 else "window",
                              "label": w.get("label", wid)}
                        for wid, w in house.get("windows", {}).items()},
            "doors": {did: {"between": d.get("between"), "label": d.get("label", did)}
                      for did, d in house.get("doors", {}).items()},
        },
    }


def _controls(house: dict, states_now: dict) -> list[dict]:
    """Lijst bedienbare elementen (ramen, roosters, deuren) met hun huidige gerapporteerde
    stand — de bron voor de 'Stel ramen/roosters in'-modal op het dashboard."""
    out = []
    for wid, w in house.get("windows", {}).items():
        if w.get("max_open_area_m2", 0.0) <= 0.0:
            continue   # vast glas — niet bedienbaar, hoort niet in de modal
        out.append({"id": wid, "kind": "window", "label": w.get("label", wid),
                    "room": w.get("room"), "state": states_now.get(wid, "dicht")})
    for vid, v in house.get("vents", {}).items():
        out.append({"id": vid, "kind": "vent", "label": v.get("label", vid),
                    "room": v.get("room"), "state": states_now.get(vid, "open")})
    for did, d in house.get("doors", {}).items():
        if d.get("fixed"):
            continue   # permanente doorgang (geen deur) → niet bedienbaar, niet tonen
        out.append({"id": did, "kind": "door", "label": d.get("label", did),
                    "between": d.get("between"),
                    "state": states_now.get(did, d.get("default_state", "open"))})
    return out


def _flow_summary(house, params, sim, timeline) -> list[dict]:
    if not timeline:
        return []
    last = timeline[-1]
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    ops = build_openings(house, last["states"], last["weather"], params, sim["Ta"], last["T_out"])
    net = solve_network(zones, ops, sim["Ta"], last["T_out"])
    out = []
    for op, q in zip(ops, net["flows"]):
        if op["id"].startswith("_leak_"):
            continue
        out.append({"id": op["id"], "a": op["a"], "b": op["b"],
                    "flow_m3s": round(q, 4), "area": round(op["area"], 3)})
    return out


# Caching-stub (de log wordt één keer geladen in main en doorgegeven).
_OPENINGS_CACHE: list[dict] = []
def load_openings_log_cached() -> list[dict]:
    return _OPENINGS_CACHE


# kleine datetime-helpers (vermijd timedelta-import-ruis bovenin)
from datetime import timedelta as _td
def _timedelta_h(h: float):
    return _td(hours=h)


def _interp_hourly(rows: list[dict], t: datetime, key: str) -> float:
    """Lineaire interpolatie van een uurlijkse driver-reeks op tijdstip t."""
    if t <= rows[0]["dt"]:
        return rows[0].get(key) or 0.0
    if t >= rows[-1]["dt"]:
        return rows[-1].get(key) or 0.0
    for r0, r1 in zip(rows, rows[1:]):
        if r0["dt"] <= t <= r1["dt"]:
            v0, v1 = r0.get(key) or 0.0, r1.get(key) or 0.0
            f = (t - r0["dt"]).total_seconds() / max(1.0, (r1["dt"] - r0["dt"]).total_seconds())
            return v0 + f * (v1 - v0)
    return rows[-1].get(key) or 0.0


# ════════════════════════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════════════════════════

_LAT = 52.0907
_LON = 5.1214


def main():
    global _LAT, _LON, _OPENINGS_CACHE
    now = datetime.now(TZ)
    print(f"[airflow_model] Start — {now.isoformat()}")

    house = load_house()
    loc = house.get("location", {})
    _LAT = loc.get("lat", _LAT)
    _LON = loc.get("lon", _LON)

    wd = load_window_data()
    weather = fetch_weather()
    log = load_openings_log()
    _OPENINGS_CACHE = log

    learned = load_learned()
    params = learned.get("params") or default_params(house)
    # Zorg dat nieuwe kamers/keys een prior krijgen (additief, robuust).
    base = default_params(house)
    for g in GLOBAL_PARAMS:
        params.setdefault(g, base[g])
    for rid in house.get("rooms", {}):
        params.setdefault(rid, base[rid])
        for k in PER_ROOM_PARAMS:
            params[rid].setdefault(k, PRIORS[k])

    since = now - _timedelta_h(CALIB_WINDOW_H)
    actual = collect_actual(house, wd, since)
    timeline = build_timeline(house, weather, log, now, CALIB_WINDOW_H)
    if not timeline:
        print("[airflow_model] Geen weerdata → kan niet simuleren. Stop.")
        sys.exit(1)

    # Seed de luchtknoop op de eerste werkelijke meting per kamer (anders buiten).
    seed = {}
    for rid, samples in actual.items():
        seed[rid] = samples[0][1]
    for rid in house.get("rooms", {}):
        seed.setdefault(rid, timeline[0]["T_out"])

    # Leren (alleen als er werkelijke samples zijn om tegen te ijken).
    rmse_now = float("nan")
    if actual:
        print(f"[leren] {sum(len(v) for v in actual.values())} samples over "
              f"{len(actual)} kamers in het venster.")
        params, rmse_now = calibrate(house, params, timeline, seed, actual)
        print(f"[leren] RMSE na kalibratie: {rmse_now:.3f} °C")
    else:
        print("[leren] geen werkelijke kamertemps in het venster → alleen voorspellen.")

    # Voorspelling met de (geleerde) params over het volledige venster + vooruitblik.
    sim = simulate(house, params, timeline, seed,
                   calib_only_rooms=set(house.get("rooms", {}).keys()))

    # Passieve suggestie op basis van nú.
    cur = weather["current"]
    out_t = cur.get("temperature_2m", timeline[-1]["T_out"])
    out_rh = cur.get("relative_humidity_2m")
    room_now = {room.get("from_window_data"): wd.get("rooms", {}).get(room.get("from_window_data"), {}).get("inside")
                for room in house.get("rooms", {}).values()}
    wx_now = {"wind_speed": cur.get("wind_speed_10m", 0.0), "wind_dir": cur.get("wind_direction_10m", 0.0),
              "gust": cur.get("wind_gusts_10m", 0.0), "precip": 0.0}
    sugg = suggest(house, params, wx_now, room_now, out_t, out_rh, out_t)
    print(f"[suggestie] {sugg['headline']}")

    dash = build_dashboard(house, params, weather, wd, timeline, sim, sugg, learned,
                           actual, now, rmse_now)
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(dash, f, ensure_ascii=False, indent=2)
    with open(LEARNED_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now.isoformat(), "params": params,
                   "rmse": round(rmse_now, 3) if rmse_now == rmse_now else None,
                   "rmse_history": dash["learned"]["rmse_history"]},
                  f, ensure_ascii=False, indent=2)
    print(f"[airflow_model] Geschreven: {DASHBOARD_FILE} + {LEARNED_FILE}. Klaar.")


if __name__ == "__main__":
    main()

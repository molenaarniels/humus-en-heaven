#!/usr/bin/env python3
"""
airflow2_model.py — Ventilatie 2 (Project 12): de tweede, hogere-fideliteit digital twin.

Zelfde inputs als Project 8 (openingen-log, weer, tado-temps), een rijker model,
eigen artefacten + dashboard (docs/airflow2.html) — een puur voorspel-experiment
naast tweeling 1, dat diens staat nooit aanraakt. Géén Telegram, géén suggestie-
motor: tweeling 1 blijft de operationele twin; deze meet zich eraan.

De fideliteit-upgrades t.o.v. tweeling 1 (elk gekozen omdat er een dataraam voor is):
  • 3-knoops RC per kamer: lucht (snel) / inboedel+binnenschil (uren) / diepe massa
    (dagen). De batch-fit over wéken data maakt de twee massatijdconstanten
    identificeerbaar die een 48u-venster niet kan scheiden; zware ridge op
    c_deep/h_fd als vangnet.
  • Vocht als mede-geobserveerde tracer: per zone de absolute vochtigheid w (kg/kg),
    geadvecteerd met dezelfde luchtstromen + een EMPD-lite bufferknoop per kamer.
    tado levert per kamer óók RH (window_data-history `hum`) → ~verdubbelt de
    observatiekanalen en bindt de luchtverversing direct (vocht is een tracer van
    de luchtuitwisseling) — de zwakste degeneratie van tweeling 1 (`vent_eff`).
    Bewust GEEN latente-warmte-koppeling in de energiebalans (tweede-orde binnen).
  • Koker-subzones: het trappenhuis krijgt per verdieping een eigen thermische
    sub-knoop (house_model.json `subzones`, additief) — één drukknoop blijft
    (solve_network ongewijzigd), elke verdiepingsdeur advecteert tegen zíjn
    verdieping, verticale buoyancy-uitwisseling koppelt de lagen. Waar tweeling 1
    bewust bij een display-gradiënt bleef, probeert tweeling 2 de echte split —
    dat is precies het experiment.
  • Wind: Swami–Chandra-laagbouw-Cp + twee beschuttingsfactoren (voor/achter,
    `front_azimuth_deg` in house_model.json, per element te overschrijven met
    `exposure`) i.p.v. één globale.
  • Beam-IAM (scherende-hoek-glas-transmissie) staat áltijd aan.
  • Batch-kalibratie over de volle gedolven historie (data/twin2_history/, zie
    tools/twin2_backfill.py): het batch-optimum wordt het ridge-ANKER voor het
    online leren — het kwartierleren zweeft eromheen i.p.v. rond de kale priors.

Bewust NIET overgenomen van tweeling 1 (gedocumenteerde keuzes):
  • het checkpoint/auto-fallback-mechaniek (veroorzaakte daar tweemaal een
    fallback-lus; het batch-anker vervult de vangnet-rol),
  • backfill_rmse_history (v1: tweeling 1's curve blijft de geheelde referentie),
  • een leerbare `cd`/sensor-fractie en een AC-/verwarmingsterm (dezelfde
    exclusie-filters als tweeling 1 worden hergebruikt).

Draait mee in dezelfde kwartierloop (airflow-notify.yml) ná tweeling 1;
`--batch` draait de volle-historie-fit (eigen workflow, wekelijks/handmatig).

Bronnen / env: als airflow_model.py (GIST_*, WU_*, DRY_RUN); eigen artefact-paden
via AIRFLOW2_DATA_PATH / AIRFLOW2_LEARNED_PATH / AIRFLOW2_BATCH_PATH /
TWIN2_HISTORY_DIR (test-overrides, weekjournaal-patroon).

Pure stdlib + requests; hergebruikt de pure helpers van airflow_model read-only
(het Project 9/10-patroon) — importeert nooit diens main-pad, schrijft nooit
diens artefacten.
"""

import argparse
import glob
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import airflow_model as am
import shared_const
from http_util import get_json
from notify import run_guarded
from shared_const import utc_now_iso
from window_advisor import ROOM_COMFORT, fetch_wu_current_temp
from wu_bias import correct_temp

TZ = shared_const.TZ

# ── Bestanden (env-overrides voor tests, weekjournaal-patroon) ─────────────────────
DASHBOARD2_FILE = os.getenv("AIRFLOW2_DATA_PATH", "docs/airflow2_data.json")
LEARNED2_FILE   = os.getenv("AIRFLOW2_LEARNED_PATH", "docs/airflow2_learned.json")
BATCH_FILE      = os.getenv("AIRFLOW2_BATCH_PATH", "docs/airflow2_batch.json")
HISTORY_DIR     = os.getenv("TWIN2_HISTORY_DIR", "data/twin2_history")

# Eigen fysica-revisie (mirror van am.PHYSICS_REV): bumpen wanneer een wijziging de
# betekenis van geléérde parameters verschuift. Bij een mismatch reset merged_params2
# de globalen naar hun prior en wordt een ouder batch-anker genegeerd.
PHYSICS2_REV = 1

# ── Kalibratie ─────────────────────────────────────────────────────────────────────
CALIB_WINDOW_H = am.CALIB_WINDOW_H   # zelfde venster/cadans als tweeling 1 → curves vergelijkbaar
WARMUP_H       = am.WARMUP_H
LEARN_RATE2    = 0.5
LEARN_TIME_BUDGET_S = 60.0
REG_WEIGHT2    = 3.0
# Zwaardere ankers op de zwak-identificeerbare nieuwkomers: de trage massaknoop
# (c_deep/h_fd — pas de batch-fit over weken maakt ze echt zichtbaar), de vochtbuffer
# (w_buf) en de koker-uitwisseling (stair_exch, geen sensor in de koker). solar_gain
# houdt tweeling 1's anti-collapse-anker (+ de fysieke vloer in BOUNDS2).
REG_WEIGHT2_BY_PARAM = {"solar_gain": 6.0, "c_deep": 8.0, "h_fd": 8.0,
                        "w_buf": 8.0, "stair_exch": 8.0}
# RH-residuen tellen mee in de fit, geschaald naar °C-equivalent: ~10%RH fout weegt
# als ~1°C. Groot genoeg om vent_eff te binden, klein genoeg dat de temp-fit domineert.
RH_RES_WEIGHT = 0.10

# ── Batch-fit ──────────────────────────────────────────────────────────────────────
BATCH_WINDOW_D  = 5.0    # dagen residu-venster per batch-window
BATCH_STRIDE_D  = 3.0    # dagen stap tussen window-starts (overlap → gladdere fit)
BATCH_WARMUP_H  = 24.0   # sim-only aanloop per window (massaknoop-equilibratie)
BATCH_TIME_BUDGET_S = float(os.getenv("BATCH_TIME_BUDGET_S", "4200"))
BATCH_MAX_EPOCHS = 40
# Convergentie-stop: een geaccepteerde stap die de kosten relatief minder dan dit
# verbetert telt als "uitgeconvergeerd" — de resterende budget-tijd is dan verspild.
BATCH_CONVERGED_RTOL = 1e-3

# ── Vocht (tracer + EMPD-lite buffer) ──────────────────────────────────────────────
P_ATM_KPA        = 101.325
MOIST_GAIN_GH_M3 = 1.0   # g/h vochtproductie per m³ kamervolume bij profiel 1.0 (koken,
                         # douchen, mensen — q_moist schaalt; dag/nacht via hetzelfde
                         # internal_gain_profile als de interne warmtelast)
BUF_CAP_FACTOR   = 5.0   # buffercapaciteit ≈ dit × de luchtmassa (hygroscopische inboedel)
BUF_TAU_H        = 6.0   # uur — koppel-tijdconstante lucht↔buffer (w_buf schaalt de capaciteit)

# ── Koker-subzones ─────────────────────────────────────────────────────────────────
VERT_EXCH_STABLE_MS = 0.003  # m/s effectieve uitwisselsnelheid over het kokerdoorsnede-vlak
                             # bij stabiele gelaagdheid (koud onder, warm boven)
VERT_EXCH_C         = 0.10   # Brown–Solvason-achtige constante bij instabiel (onder warmer)

# Cp-referentie-amplitude voor de Swami–Chandra-loefgevel (vóór de beschutting).
CP_SC_REF = 0.6

# Fractie van de tweeling-1-massabasis die naar de snelle knoop gaat (inboedel +
# binnenschil); de rest is de diepe structurele massa. Vaste split — c_fast/c_deep
# leren de groottes zelf bij.
FAST_MASS_FRAC = 0.30

# ── Prior-parameters + banden (mirror van tweeling 1's filosofie) ────────────────────
PRIORS2 = {
    "cp_shelter_front": 0.5, "cp_shelter_back": 0.5,
    "vent_eff": 1.0,
    "q_moist": 1.0,       # globale vochtproductie-schaal
    "stair_exch": 1.0,    # verticale koker-uitwisseling-schaal
    "c_air": 1.0, "c_fast": 1.0, "c_deep": 1.0,
    "h_af": 1.0, "h_fd": 1.0,
    "ua_env": 1.0, "solar_gain": 1.0, "ua_party": 1.0,
    "q_int": 1.0, "ua_roof": 1.0,
    "f_air": 0.4,         # absolute fractie zonwinst → luchtknoop (rest → snelle massa)
    "w_buf": 1.0,         # vochtbuffer-capaciteit-schaal per kamer
}
BOUNDS2 = {
    "cp_shelter_front": (0.1, 1.2), "cp_shelter_back": (0.1, 1.2),
    "vent_eff": (0.1, 2.0),
    "q_moist": (0.0, 4.0), "stair_exch": (0.1, 4.0),
    "c_air": (0.3, 8.0), "c_fast": (0.2, 8.0), "c_deep": (0.2, 10.0),
    "h_af": (0.2, 5.0), "h_fd": (0.05, 5.0),
    "ua_env": (0.2, 5.0), "solar_gain": (0.25, 3.0),   # zelfde fysieke zon-vloer als tweeling 1
    "ua_party": (0.0, 6.0), "q_int": (0.0, 4.0), "ua_roof": (0.0, 4.0),
    "f_air": (0.1, 0.9), "w_buf": (0.2, 5.0),
}
GLOBAL_PARAMS2   = ["cp_shelter_front", "cp_shelter_back", "vent_eff", "q_moist", "stair_exch"]
PER_ROOM_PARAMS2 = ["c_air", "c_fast", "c_deep", "h_af", "h_fd", "ua_env",
                    "solar_gain", "ua_party", "q_int", "ua_roof", "f_air", "w_buf"]


def reg_weight2(name: str) -> float:
    return REG_WEIGHT2_BY_PARAM.get(name, REG_WEIGHT2)


def railed_params2(params: dict, tol: float = am.RAIL_TOL) -> list[str]:
    """Saturatie-tell (mirror van am.railed_params, op de eigen BOUNDS2-tabellen)."""
    out = []

    def _flag(scope, name, value):
        b = BOUNDS2.get(name)
        if b is None or not isinstance(value, (int, float)):
            return
        lo, hi = b
        rng = hi - lo
        if rng <= 0:
            return
        if value - lo <= tol * rng:
            out.append(f"{scope}.{name}@floor")
        elif hi - value <= tol * rng:
            out.append(f"{scope}.{name}@ceil")

    for name in GLOBAL_PARAMS2:
        if name in params:
            _flag("global", name, params[name])
    for rid, rp in params.items():
        if not isinstance(rp, dict):
            continue
        for name in PER_ROOM_PARAMS2:
            if name in rp:
                _flag(rid, name, rp[name])
    return out


# ════════════════════════════════════════════════════════════════════════════════════
#  Psychrometrie — absolute vochtigheid w (kg/kg) ↔ RH
# ════════════════════════════════════════════════════════════════════════════════════

def _es_kpa(temp_c: float) -> float:
    """Saturatiedampdruk (kPa), Magnus/Tetens — zelfde formule als window_advisor._es."""
    return 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))


def w_from_rh(rh: float | None, temp_c: float | None) -> float | None:
    """Absolute vochtigheid (kg water / kg droge lucht) uit RH (%) + temp (°C)."""
    if rh is None or temp_c is None:
        return None
    e = max(0.0, min(1.0, rh / 100.0)) * _es_kpa(temp_c)
    return 0.622 * e / max(1e-6, P_ATM_KPA - e)


def rh_from_w(w: float | None, temp_c: float | None) -> float | None:
    """RH (%) uit absolute vochtigheid w + temp; geklemd op [0, 100]."""
    if w is None or temp_c is None:
        return None
    e = w * P_ATM_KPA / (0.622 + w)
    return max(0.0, min(100.0, e / _es_kpa(temp_c) * 100.0))


# ════════════════════════════════════════════════════════════════════════════════════
#  Wind — Swami–Chandra-Cp + voor/achter-beschutting
# ════════════════════════════════════════════════════════════════════════════════════

def cp_swami_chandra(theta_deg: float) -> float:
    """Surface-averaged Cp voor een verticale laagbouwgevel, Swami & Chandra (1988)
    laagbouw-correlatie met zijverhouding 1 (G = ln S = 0): een gladde, gevalideerde
    curve i.p.v. tweeling 1's twee-cosinus-fit. Loef ≈ +0.6, zijgevel ≈ −0.44,
    lij ≈ −0.36 (vóór beschutting)."""
    t = math.radians(abs(((theta_deg + 180.0) % 360.0) - 180.0))
    val = (1.248 - 0.703 * math.sin(t / 2.0) - 1.175 * math.sin(t) ** 2
           + 0.769 * math.cos(t / 2.0) + 0.717 * math.cos(t / 2.0) ** 2)
    return CP_SC_REF * math.log(max(1e-3, val))


def cp_tilted2(theta_deg: float, tilt_deg: float) -> float:
    """Als am.cp_tilted, maar met de Swami–Chandra-muurcurve; het (bijna) platte dak
    houdt tweeling 1's alom-zuiging-profiel (am.cp_roof)."""
    w_wall = max(0.0, min(1.0, tilt_deg / 90.0))
    return w_wall * cp_swami_chandra(theta_deg) + (1.0 - w_wall) * am.cp_roof(theta_deg)


def element_exposure(elem: dict, front_az: float) -> str:
    """'front' (straatzijde) of 'back' (tuinzijde) voor een exterieur element: de
    expliciete `exposure`-override, anders per gevel-azimut (±90° van de voorgevel)."""
    exp = elem.get("exposure")
    if exp in ("front", "back"):
        return exp
    daz = abs(((elem.get("facade_azimuth_deg", 0.0) - front_az + 180.0) % 360.0) - 180.0)
    return "front" if daz <= 90.0 else "back"


def wind_pressure2(facade_az: float, wind_speed: float, wind_dir: float,
                   shelter: float, rho: float, tilt_deg: float = 90.0) -> float:
    """Externe winddruk (Pa): Cp(Swami–Chandra)·½ρU². Dynamische druk op één
    referentiehoogte per gevel (am.WIND_REF_Z — de tweeling-1-fix tegen de
    zelfde-gevel-lus geldt hier onverkort)."""
    theta = abs(((wind_dir - facade_az + 180.0) % 360.0) - 180.0)
    u_local = (wind_speed or 0.0) * (am.WIND_REF_Z / 10.0) ** 0.30
    return shelter * cp_tilted2(theta, tilt_deg) * 0.5 * rho * u_local * u_local


def build_openings2(house: dict, states: dict, weather: dict, params: dict,
                    zone_temps: dict, outside_temp: float) -> list[dict]:
    """Openingenlijst voor het netwerk — bewuste dunne kopie van am.build_openings
    (zie daar voor de conventies) met twee verschillen: Swami–Chandra-Cp en een
    beschuttingsfactor per gevel-zijde (cp_shelter_front/back via element_exposure)."""
    front_az = house.get("front_azimuth_deg", 309.0)
    cd = am.CD
    rho_out = am.air_density(outside_temp)
    wind_s, wind_d = weather.get("wind_speed", 0.0), weather.get("wind_dir", 0.0)
    ops = []

    def ext(elem_id, elem, kind):
        frac = (am._open_frac(states[elem_id], elem) if elem_id in states
                else am._default_frac(elem, kind))
        area = frac * elem.get("max_open_area_m2", elem.get("area_m2", 0.0)) * am._eff_open_area(elem)
        if area <= 0:
            return
        shelter = params.get("cp_shelter_front" if element_exposure(elem, front_az) == "front"
                             else "cp_shelter_back", 0.5)
        pe = wind_pressure2(elem.get("facade_azimuth_deg", 0.0), wind_s, wind_d,
                            shelter, rho_out, elem.get("tilt_deg", 90.0))
        ops.append({"a": elem["room"], "b": "outside", "area": area, "Cd": cd,
                    "z": elem.get("center_height_m", 1.5), "Pe": pe, "id": elem_id})

    for wid, w in house.get("windows", {}).items():
        ext(wid, w, "window")
    for vid, v in house.get("vents", {}).items():
        ext(vid, v, "vent")
    for did, d in house.get("doors", {}).items():
        frac = am._open_frac(states[did], d) if did in states else am._default_frac(d, "door")
        area = frac * d.get("area_m2", 0.0)
        if area <= 0:
            continue
        a, b = d["between"]
        ops.append({"a": a, "b": b, "area": area, "Cd": cd,
                    "z": d.get("center_height_m", 1.0), "Pe": 0.0, "id": did})
    for zone in list(house.get("rooms", {})) + list(house.get("junctions", {})):
        ops.append({"a": zone, "b": "outside", "area": am.LEAK_AREA, "Cd": cd,
                    "z": 1.5, "Pe": 0.0, "id": f"_leak_{zone}"})
    return ops


# ════════════════════════════════════════════════════════════════════════════════════
#  Koker-subzones — metadata uit house_model.json
# ════════════════════════════════════════════════════════════════════════════════════

def _sub_index_for_height(subs: list[dict], z: float) -> int:
    """Sub-knoop-index waarin hoogte `z` valt (subs gesorteerd op z_lo)."""
    for i, s in enumerate(subs):
        if z < s["z_hi"]:
            return i
    return len(subs) - 1


def subzone_meta(house: dict) -> dict:
    """Zones met een `subzones`-lijst (≥2) → hun per-verdieping-metadata: genormaliseerde
    sub-knopen (id, z_lo/z_hi, volumefractie) + per verbonden deur de sub-knoop-index
    (expliciete `subzone`-sleutel op de deur, anders op deurhoogte). Zonder de lijst →
    afwezig → simulate2 gedraagt zich exact één-knoops (getest)."""
    out = {}
    for rid, r in house.get("rooms", {}).items():
        subs = r.get("subzones")
        if not subs or len(subs) < 2:
            continue
        total = sum(s.get("volume_frac", 1.0 / len(subs)) for s in subs) or 1.0
        norm = sorted(({"id": s.get("id", str(i)), "z_lo": float(s.get("z_lo", 0.0)),
                        "z_hi": float(s.get("z_hi", 0.0)),
                        "frac": s.get("volume_frac", 1.0 / len(subs)) / total}
                       for i, s in enumerate(subs)), key=lambda s: s["z_lo"])
        doors = {}
        for did, d in house.get("doors", {}).items():
            pair = d.get("between", [])
            if len(pair) != 2 or rid not in pair:
                continue
            sz = d.get("subzone")
            idx = next((i for i, s in enumerate(norm) if s["id"] == sz), None)
            if idx is None:
                idx = _sub_index_for_height(norm, d.get("center_height_m", 1.0))
            doors[did] = idx
        height = max(s["z_hi"] for s in norm) - min(s["z_lo"] for s in norm)
        area_h = r.get("volume_m3", 26.0) / max(1.0, height)   # horizontale kokerdoorsnede
        out[rid] = {"subs": norm, "doors": doors, "area_h": area_h}
    return out


def vertical_exchange(area_h: float, t_lower: float, t_upper: float,
                      dz: float, scale: float) -> float:
    """Buoyancy-gedreven verticale uitwisseling (m³/s) tussen twee gestapelde
    sub-knopen: een kleine altijd-aanwezige menging (trapverkeer, kieren) plus een
    Brown–Solvason-achtige term wanneer de ónderste warmer is (instabiel → convectie).
    Stabiele gelaagdheid (boven warmer) houdt alleen de basis-menging — zo kan warmte
    bovenin poolen maar mengt een verwarmde begane grond wél omhoog."""
    q = VERT_EXCH_STABLE_MS * area_h
    if t_lower > t_upper:
        t_mean_k = 273.15 + 0.5 * (t_lower + t_upper)
        q += VERT_EXCH_C * area_h * math.sqrt(am.G * max(0.1, dz) * (t_lower - t_upper) / t_mean_k)
    return scale * q


# ════════════════════════════════════════════════════════════════════════════════════
#  3-knoops RC + vocht-tracer — simulate2
# ════════════════════════════════════════════════════════════════════════════════════

def _zone_thermal_params2(house: dict, params: dict) -> dict:
    """Per zone de geschaalde 3-knoops parameters. De tweeling-1-massabasis wordt
    FAST_MASS_FRAC/rest gesplitst over snel/diep; verder dezelfde fysische bases
    (am.room_base_capacitances) zodat de geleerde schalen vergelijkbaar blijven."""
    par = {}
    for rid, r in house.get("rooms", {}).items():
        c_air0, c_mass0, ua0 = am.room_base_capacitances(r)
        vol = r.get("volume_m3", 40.0)
        p = params.get(rid, {})
        par[rid] = {
            "C_a": c_air0 * p.get("c_air", 1.0),
            "C_f": FAST_MASS_FRAC * c_mass0 * p.get("c_fast", 1.0),
            "C_d": (1.0 - FAST_MASS_FRAC) * c_mass0 * p.get("c_deep", 1.0),
            "H_af": ua0 * 8.0 * p.get("h_af", 1.0),
            "H_fd": ua0 * 2.0 * p.get("h_fd", 1.0),
            "UA_env": ua0 * 0.5 * p.get("ua_env", 1.0),
            # Diepe massa ziet buiten door de muur — vaste helft, mirror van tweeling 1's
            # niet-leerbare ua_mass (minder vrijheid, stabieler).
            "UA_deep_out": ua0 * 0.5,
            "solar": p.get("solar_gain", 1.0), "f_air": p.get("f_air", 0.4),
            "UA_party": ua0 * p.get("ua_party", 1.0),
            "Q_int_base": vol * am.INTERNAL_GAIN_WM3 * p.get("q_int", 1.0),
            "UA_roof": r.get("roof_m2", 0.0) * am.ROOF_U * p.get("ua_roof", 1.0),
            "vol": vol, "w_buf": p.get("w_buf", 1.0),
        }
    for jid, j in house.get("junctions", {}).items():
        vol = j.get("volume_m3", 15.0)
        c_air0 = vol * 1.2 * am.CP_AIR * 3.0
        par[jid] = {"C_a": c_air0, "C_f": c_air0, "C_d": c_air0 * 2.0,
                    "H_af": 15.0, "H_fd": 5.0, "UA_env": 3.0, "UA_deep_out": 1.0,
                    "solar": 0.0, "f_air": 1.0, "UA_party": 0.0, "Q_int_base": 0.0,
                    "UA_roof": 0.0, "vol": vol, "w_buf": 1.0}
    return par


def simulate2(house: dict, params: dict, timeline: list[dict], seed: dict,
              calib_only_rooms: set | None = None, snapshot_t: datetime | None = None,
              tm_seed: dict | None = None, seed_w: dict | None = None) -> dict:
    """Integreer het 3-knoops thermische model + de vocht-tracer over `timeline`
    (zelfde rijvorm als am.build_timeline levert). Impliciet (backward Euler), één
    gekoppelde lineaire solve per substap voor de temps en één kleine voor het vocht
    (het vocht advecteert met de al-opgeloste luchtstromen — ontkoppeld van de
    temp-solve, zelf wél impliciet).

    Kokerzones met `subzones` krijgen per verdieping een eigen lúchtknoop (één druk-
    knoop, één snelle + één diepe massaknoop blijven); hun gerapporteerde temp is het
    volumegemiddelde. Zonder `subzones` exact één-knoops-gedrag.

    `seed`/`tm_seed` als bij am.simulate (tm_seed zet hier de díépe knoop); `seed_w` =
    {zone: w-beginwaarde} (anders de buiten-w op t0). De party-muur-anker­temperatuur
    is am._NEIGHBOR_TEMP — de aanroeper (main/night-achtige paden) moet die rebinden."""
    rooms = house.get("rooms", {})
    zones = list(rooms.keys()) + list(house.get("junctions", {}).keys())
    par = _zone_thermal_params2(house, params)
    veff = params.get("vent_eff", 1.0)
    exch_scale = params.get("stair_exch", 1.0)
    q_moist_scale = params.get("q_moist", 1.0)
    rho_cp = 1.2 * am.CP_AIR
    rho_a = 1.2                       # kg/m³ voor de vocht-massabalans
    subm = subzone_meta(house)

    # Indexering: per zone n_sub luchtknopen + 1 snelle + 1 diepe massaknoop.
    air_idx: dict[str, list[int]] = {}
    f_idx: dict[str, int] = {}
    d_idx: dict[str, int] = {}
    k = 0
    for z in zones:
        n_sub = len(subm[z]["subs"]) if z in subm else 1
        air_idx[z] = list(range(k, k + n_sub))
        k += n_sub
        f_idx[z] = k
        d_idx[z] = k + 1
        k += 2
    ntot = k

    t0_out = timeline[0]["T_out"]
    w_out0 = w_from_rh(timeline[0].get("weather", {}).get("rh"), t0_out) or 0.008
    Ta = {z: [seed.get(z, t0_out)] * len(air_idx[z]) for z in zones}
    Tf = {z: seed.get(z, t0_out) for z in zones}
    Td = {z: (tm_seed[z] if tm_seed is not None and tm_seed.get(z) is not None
              else 0.5 * (seed.get(z, t0_out) + am._NEIGHBOR_TEMP)) for z in zones}
    Wz = {z: (seed_w[z] if seed_w is not None and seed_w.get(z) is not None else w_out0)
          for z in zones}
    Wbuf = dict(Wz)

    def zmean(z):
        return sum(Ta[z][i] * subm[z]["subs"][i]["frac"] for i in range(len(Ta[z]))) \
            if z in subm else Ta[z][0]

    out = {rid: [] for rid in rooms if (calib_only_rooms is None or rid in calib_only_rooms)}
    out_rh = {rid: [] for rid in out}
    P_warm = None
    snap = None
    solver_failures = 0

    for step in timeline:
        T_out = step["T_out"]
        w_out = w_from_rh(step.get("weather", {}).get("rh"), T_out)
        if w_out is None:
            w_out = w_out0
        temps_mean = {z: zmean(z) for z in zones}
        ops = build_openings2(house, step["states"], step["weather"], params, temps_mean, T_out)
        net = am.solve_network(zones, ops, temps_mean, T_out, P_init=P_warm)
        P_warm = net["P"]

        # Verse buitenlucht per sub-knoop (exterieure instroom toegewezen op de hoogte
        # van de opening) + deur-uitwisseling per (sub-)knoop-paar.
        fresh_sub = {z: [0.0] * len(air_idx[z]) for z in zones}
        fresh_zone = {z: 0.0 for z in zones}
        gpairs: dict[tuple, float] = {}      # (node_i, node_j) → W/K (lucht↔lucht)
        mpairs: dict[tuple, float] = {}      # zone-paar → kg/s (vocht, zone-niveau)

        def _sub_of(z, op):
            if z not in subm:
                return 0
            did = op.get("id")
            idx = subm[z]["doors"].get(did)
            return idx if idx is not None else _sub_index_for_height(subm[z]["subs"], op.get("z", 1.0))

        for op, q in zip(ops, net["flows"]):
            a, b = op["a"], op["b"]
            if b == "outside":
                if q < 0.0:                      # buitenlucht stroomt a in
                    si = _sub_of(a, op)
                    fresh_sub[a][si] += -q
                    fresh_zone[a] += -q
                continue
            q_abs = abs(q)
            ia = air_idx[a][_sub_of(a, op)]
            ib = air_idx[b][_sub_of(b, op)]
            g = rho_cp * q_abs * veff
            key = (min(ia, ib), max(ia, ib))
            gpairs[key] = gpairs.get(key, 0.0) + g
            mk = (min(a, b), max(a, b))
            mpairs[mk] = mpairs.get(mk, 0.0) + rho_a * q_abs * veff
            # Brown–Solvason-counterflow op deuren van subzone-kokers: tegen de échte
            # sub-knoop-temp op deurhoogte (geen γ-gradiënt meer nodig). Buiten × vent_eff
            # om — fysieke orifice-term, zelfde argument als tweeling 1.
            sub_side = a if a in subm else (b if b in subm else None)
            if sub_side is not None:
                other = b if sub_side == a else a
                t_sub = Ta[sub_side][_sub_of(sub_side, op)]
                t_oth = Ta[other][_sub_of(other, op)] if other in subm else Ta[other][0]
                q_ex = am.buoyant_door_exchange(op["area"], t_sub, t_oth)
                if q_ex > 0.0:
                    gpairs[key] = gpairs.get(key, 0.0) + rho_cp * q_ex
                    mpairs[mk] = mpairs.get(mk, 0.0) + rho_a * q_ex

        # Verticale uitwisseling tussen gestapelde sub-knopen.
        for z, info in subm.items():
            subs = info["subs"]
            for i in range(len(subs) - 1):
                dz = 0.5 * (subs[i]["z_hi"] - subs[i]["z_lo"] + subs[i + 1]["z_hi"] - subs[i + 1]["z_lo"])
                q_v = vertical_exchange(info["area_h"], Ta[z][i], Ta[z][i + 1], dz, exch_scale)
                key = (air_idx[z][i], air_idx[z][i + 1])
                gpairs[key] = gpairs.get(key, 0.0) + rho_cp * q_v

        nsub_steps = max(1, int(math.ceil(step["dt"] / am.SUBSTEP_S)))
        h = step["dt"] / nsub_steps
        irr_roof = step.get("irr_roof", {})
        night = step.get("sun_el", 90.0) <= 0.0
        profile = am.internal_gain_profile(step["t"])
        nb = am._NEIGHBOR_TEMP

        for _ in range(nsub_steps):
            A = [[0.0] * ntot for _ in range(ntot)]
            bvec = [0.0] * ntot
            for z in zones:
                pa = par[z]
                q_solar = step["irr"].get(z, 0.0) * pa["solar"]
                q_int = pa["Q_int_base"] * profile
                subs = subm[z]["subs"] if z in subm else [{"frac": 1.0}]
                fi, di = f_idx[z], d_idx[z]
                for si, sub in enumerate(subs):
                    ai = air_idx[z][si]
                    frac = sub["frac"]
                    gv = rho_cp * fresh_sub[z][si] * veff
                    # Zonwinst-luchtdeel: subzone-kokers krijgen 'm op de bovenste knoop
                    # (het enige koker-glas is de skylight bovenin — een per-raam-split
                    # per sub-knoop zou de timeline-bouw moeten verbouwen, bewust niet).
                    q_sol_air = pa["f_air"] * q_solar * (1.0 if (z not in subm or si == len(subs) - 1) else 0.0)
                    A[ai][ai] += (pa["C_a"] * frac / h + gv + pa["UA_env"] * frac
                                  + pa["H_af"] * frac + pa["UA_party"] * frac)
                    A[ai][fi] += -pa["H_af"] * frac
                    bvec[ai] += (pa["C_a"] * frac / h * Ta[z][si] + gv * T_out
                                 + pa["UA_env"] * frac * T_out + q_sol_air
                                 + pa["UA_party"] * frac * nb + q_int * frac)
                # Snelle massaknoop (inboedel/binnenschil): lucht↔snel + snel↔diep + zonrest.
                A[fi][fi] += pa["C_f"] / h + pa["H_af"] + pa["H_fd"]
                for si, sub in enumerate(subs):
                    A[fi][air_idx[z][si]] += -pa["H_af"] * sub["frac"]
                A[fi][di] += -pa["H_fd"]
                bvec[fi] += pa["C_f"] / h * Tf[z] + (1.0 - pa["f_air"]) * q_solar
                # Diepe massaknoop: snel↔diep + muur-naar-buiten + dak-sol-air.
                t_solair = T_out + am.ROOF_SOLAR_GAIN * irr_roof.get(z, 0.0) \
                    - (am.ROOF_SKY_COOLING if night else 0.0)
                A[di][di] += pa["C_d"] / h + pa["H_fd"] + pa["UA_deep_out"] + pa["UA_roof"]
                A[di][fi] += -pa["H_fd"]
                bvec[di] += (pa["C_d"] / h * Td[z] + pa["UA_deep_out"] * T_out
                             + pa["UA_roof"] * t_solair)
            for (i, j), g in gpairs.items():
                A[i][i] += g
                A[i][j] += -g
                A[j][j] += g
                A[j][i] += -g
            x = solve_linear2(A, bvec)
            if x is None:
                solver_failures += 1
                break
            for z in zones:
                for si in range(len(Ta[z])):
                    Ta[z][si] = x[air_idx[z][si]]
                Tf[z] = x[f_idx[z]]
                Td[z] = x[d_idx[z]]

            # Vocht-tracer (zone-niveau, één w-knoop per zone + één bufferknoop):
            # advectie met dezelfde debieten, EMPD-lite buffer, interne productie.
            nz = len(zones)
            zi = {z: i for i, z in enumerate(zones)}
            Aw = [[0.0] * (2 * nz) for _ in range(2 * nz)]
            bw = [0.0] * (2 * nz)
            for z in zones:
                pa = par[z]
                iw, ib = 2 * zi[z], 2 * zi[z] + 1
                m_air = pa["vol"] * rho_a
                m_fresh = rho_a * fresh_zone[z] * veff
                k_buf = m_air / (BUF_TAU_H * 3600.0)
                c_buf = BUF_CAP_FACTOR * m_air * pa["w_buf"]
                s_moist = pa["vol"] * MOIST_GAIN_GH_M3 * q_moist_scale * profile / 3.6e6  # kg/s
                Aw[iw][iw] += m_air / h + m_fresh + k_buf
                Aw[iw][ib] += -k_buf
                bw[iw] += m_air / h * Wz[z] + m_fresh * w_out + s_moist
                Aw[ib][ib] += c_buf / h + k_buf
                Aw[ib][iw] += -k_buf
                bw[ib] += c_buf / h * Wbuf[z]
            for (a, b), m_ex in mpairs.items():
                ia_, ib_ = 2 * zi[a], 2 * zi[b]
                Aw[ia_][ia_] += m_ex
                Aw[ia_][ib_] += -m_ex
                Aw[ib_][ib_] += m_ex
                Aw[ib_][ia_] += -m_ex
            xw = solve_linear2(Aw, bw)
            if xw is not None:
                for z in zones:
                    Wz[z] = max(0.0, xw[2 * zi[z]])
                    Wbuf[z] = max(0.0, xw[2 * zi[z] + 1])

        for rid in out:
            out[rid].append((step["t"], zmean(rid)))
            out_rh[rid].append((step["t"], rh_from_w(Wz[rid], zmean(rid))))
        if snapshot_t is not None and snap is None and step["t"] >= snapshot_t:
            snap = ({z: zmean(z) for z in zones}, dict(Tf), dict(Td), dict(Wz),
                    {z: {subm[z]["subs"][i]["id"]: round(Ta[z][i], 2) for i in range(len(Ta[z]))}
                     for z in subm})

    final = ({z: zmean(z) for z in zones}, dict(Tf), dict(Td), dict(Wz),
             {z: {subm[z]["subs"][i]["id"]: round(Ta[z][i], 2) for i in range(len(Ta[z]))}
              for z in subm})
    ta_now, tf_now, td_now, w_now, sub_now = snap if snap is not None else final
    return {"series": out, "series_rh": out_rh,
            "Ta": final[0], "Tf": final[1], "Td": final[2], "W": final[3],
            "Ta_now": ta_now, "Tf_now": tf_now, "Td_now": td_now, "W_now": w_now,
            "sub_now": sub_now, "solver_failures": solver_failures}


def solve_linear2(A: list[list[float]], b: list[float]) -> list[float] | None:
    """Dunne alias op am.solve_linear (één implementatie), hier benoemd zodat de
    hot-path in simulate2 goedkoop te patchen/instrumenteren is in tests."""
    return am.solve_linear(A, b)


# ════════════════════════════════════════════════════════════════════════════════════
#  Kalibratie — Gauss-Newton op temp- + RH-residuen, ridge naar het (batch-)anker
# ════════════════════════════════════════════════════════════════════════════════════

def _param_keys2(rooms: list[str]) -> list[tuple]:
    keys = [("global", g) for g in GLOBAL_PARAMS2]
    for rid in rooms:
        for p in PER_ROOM_PARAMS2:
            keys.append((rid, p))
    return keys


def params_to_vec2(params: dict, keys: list[tuple]) -> list[float]:
    return [params[name] if scope == "global" else params[scope][name]
            for scope, name in keys]


def vec_to_params2(vec: list[float], keys: list[tuple], base: dict) -> dict:
    p = json.loads(json.dumps(base))
    for v, (scope, name) in zip(vec, keys):
        lo, hi = BOUNDS2[name]
        v = max(lo, min(hi, v))
        if scope == "global":
            p[name] = v
        else:
            p.setdefault(scope, {})[name] = v
    return p


def _clamp_to_bounds2(vec: list[float], keys: list[tuple]) -> list[float]:
    return [max(BOUNDS2[name][0], min(BOUNDS2[name][1], v))
            for v, (_, name) in zip(vec, keys)]


def _residuals2_timed(house, params, timeline, seed, actual, actual_rh,
                      rooms_set, seed_w=None) -> list[tuple]:
    """(meetmoment, residu) — temp-residuen in sensor-ruimte (°C) gevolgd door de
    RH-residuen × RH_RES_WEIGHT (°C-equivalent), in vaste dict-volgorde zodat de
    recency-gewichten over iteraties uitgelijnd blijven."""
    sim = simulate2(house, params, timeline, seed, calib_only_rooms=rooms_set, seed_w=seed_w)
    out = []
    for rid, samples in actual.items():
        pred = sim["series"].get(rid, [])
        if not pred:
            continue
        pred = am._to_sensor_series(house, timeline, rid, pred)
        for ts, val in samples:
            out.append((ts, am._interp(pred, ts) - val))
    for rid, samples in (actual_rh or {}).items():
        pred = sim["series_rh"].get(rid, [])
        if not pred:
            continue
        for ts, val in samples:
            out.append((ts, RH_RES_WEIGHT * (am._interp(pred, ts) - val)))
    return out


def _residuals2(house, params, timeline, seed, actual, actual_rh, rooms_set, seed_w=None):
    return [r for _, r in _residuals2_timed(house, params, timeline, seed,
                                            actual, actual_rh, rooms_set, seed_w)]


def rmse_split2(house, params, timeline, seed, actual, actual_rh, seed_w=None) -> tuple[float, float]:
    """(temp-RMSE °C, RH-RMSE %) met één simulatie — de temp-RMSE is de maat die met
    tweeling 1 vergeleken wordt; de RH-RMSE is het eigen tweede kanaal (ongeschaald)."""
    sim = simulate2(house, params, timeline, seed,
                    calib_only_rooms=set(actual) | set(actual_rh or {}), seed_w=seed_w)
    rt = []
    for rid, samples in actual.items():
        pred = sim["series"].get(rid, [])
        if not pred:
            continue
        pred = am._to_sensor_series(house, timeline, rid, pred)
        rt += [am._interp(pred, ts) - val for ts, val in samples]
    rr = []
    for rid, samples in (actual_rh or {}).items():
        pred = sim["series_rh"].get(rid, [])
        if not pred:
            continue
        rr += [am._interp(pred, ts) - val for ts, val in samples]
    return am.rmse(rt), am.rmse(rr)


def calibrate2(house, params, timeline, seed, actual, actual_rh, anchor: dict | None = None,
               max_iter: int = 4, time_budget_s: float = LEARN_TIME_BUDGET_S,
               learn_rate: float = LEARN_RATE2, seed_w=None) -> tuple[dict, float, float]:
    """Gedempte Gauss-Newton (mirror van am.calibrate) op de gecombineerde temp+RH-
    residuen, met Tikhonov-ridge naar `anchor` (het batch-optimum als dat er is,
    anders de priors) en Huber×recency-weging. Online: schuif `learn_rate` naar het
    optimum. Geeft (nieuwe params, temp-RMSE, RH-RMSE)."""
    rooms = [rid for rid in actual if actual[rid]]
    if not rooms:
        return params, float("nan"), float("nan")
    rooms_set = set(rooms) | {rid for rid in (actual_rh or {}) if actual_rh[rid]}
    keys = _param_keys2(rooms)
    x = params_to_vec2(params, keys)
    base = params
    anchor_full = anchor or {}
    prior_vec = []
    for scope, name in keys:
        if scope == "global":
            prior_vec.append(anchor_full.get(name, PRIORS2[name]))
        else:
            prior_vec.append((anchor_full.get(scope) or {}).get(name, PRIORS2[name]))
    reg_vec = [reg_weight2(name) for _, name in keys]

    def _total_cost(res, xv):
        pen = sum(reg_vec[k] * (xv[k] - prior_vec[k]) ** 2 for k in range(len(keys)))
        return am._wcost(res, tw) + pen

    r0_timed = _residuals2_timed(house, params, timeline, seed, actual, actual_rh,
                                 rooms_set, seed_w)
    if not r0_timed:
        return params, float("nan"), float("nan")
    r0 = [v for _, v in r0_timed]
    tw = am._recency_weights([t for t, _ in r0_timed])
    best_cost = _total_cost(r0, x)

    t_start = time.time()
    lam = 1e-3
    for _ in range(max_iter):
        if time.time() - t_start > time_budget_s:
            break
        p_cur = vec_to_params2(x, keys, base)
        r = _residuals2(house, p_cur, timeline, seed, actual, actual_rh, rooms_set, seed_w)
        if not r:
            break
        m = len(r)
        hw = am._huber_weights(r)
        twv = tw if len(tw) == m else [1.0] * m
        w = [hw[i] * twv[i] for i in range(m)]
        J = [[0.0] * len(keys) for _ in range(m)]
        aborted = False
        for j in range(len(keys)):
            if time.time() - t_start > time_budget_s:
                aborted = True   # halverwege de Jacobiaan → deze iteratie niet meer afmaken
                break
            dx = max(1e-3, abs(x[j]) * 0.05)
            xj = x[:]
            xj[j] += dx
            rj = _residuals2(house, vec_to_params2(xj, keys, base), timeline, seed,
                             actual, actual_rh, rooms_set, seed_w)
            if len(rj) != m:
                continue
            for i in range(m):
                J[i][j] = (rj[i] - r[i]) / dx
        if aborted:
            break
        nk = len(keys)
        JtJ = [[sum(w[i] * J[i][a] * J[i][b] for i in range(m)) for b in range(nk)]
               for a in range(nk)]
        Jtr = [sum(w[i] * J[i][a] * r[i] for i in range(m)) for a in range(nk)]
        for a in range(nk):
            JtJ[a][a] += reg_vec[a]
            Jtr[a] += reg_vec[a] * (x[a] - prior_vec[a])
        for a in range(nk):
            JtJ[a][a] += lam * (JtJ[a][a] + 1.0)
        delta = am.solve_linear(JtJ, [-v for v in Jtr])
        if delta is None:
            break
        x_new = _clamp_to_bounds2([x[j] + delta[j] for j in range(nk)], keys)
        new_cost = _total_cost(
            _residuals2(house, vec_to_params2(x_new, keys, base), timeline, seed,
                        actual, actual_rh, rooms_set, seed_w), x_new)
        if math.isnan(new_cost) or new_cost >= best_cost:
            lam *= 4.0
            if lam > 1e6:
                break
            continue
        lam = max(1e-4, lam / 3.0)
        x = x_new
        best_cost = new_cost

    x_old = params_to_vec2(params, keys)
    x_blend = [x_old[j] + learn_rate * (x[j] - x_old[j]) for j in range(len(keys))]
    new_params = vec_to_params2(x_blend, keys, base)
    rmse_t, rmse_rh = rmse_split2(house, new_params, timeline, seed, actual, actual_rh, seed_w)
    if rmse_t != rmse_t:
        return params, am.rmse([v for _, v in r0_timed[:len(r0)]]), float("nan")
    return new_params, rmse_t, rmse_rh


# ════════════════════════════════════════════════════════════════════════════════════
#  I/O — geleerde staat, batch-anker, historie-shards
# ════════════════════════════════════════════════════════════════════════════════════

def load_learned2() -> dict:
    try:
        with open(LEARNED2_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_batch() -> dict:
    try:
        with open(BATCH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def default_params2(house: dict) -> dict:
    p = {k: PRIORS2[k] for k in GLOBAL_PARAMS2}
    for rid in house.get("rooms", {}):
        p[rid] = {k: PRIORS2[k] for k in PER_ROOM_PARAMS2}
    return p


def merged_params2(house: dict, learned: dict) -> dict:
    """Geleerde params + priors voor nieuwe kamers/keys; bij een PHYSICS2_REV-mismatch
    gaan alleen de globalen terug naar hun prior (mirror van am.merged_params)."""
    params = learned.get("params") or default_params2(house)
    base = default_params2(house)
    if bool(learned.get("params")) and learned.get("physics2_rev") != PHYSICS2_REV:
        for g in GLOBAL_PARAMS2:
            params[g] = base[g]
    for g in GLOBAL_PARAMS2:
        params.setdefault(g, base[g])
    for rid in house.get("rooms", {}):
        params.setdefault(rid, dict(base[rid]))
        for kk in PER_ROOM_PARAMS2:
            params[rid].setdefault(kk, PRIORS2[kk])
    return params


def anchor_from_batch(house: dict, batch: dict) -> tuple[dict | None, dict]:
    """Het vastgepinde parameterstel uit het batch-anker: de batch-params (aangevuld
    met priors voor nieuwe kamers/keys) + de anker-stempels — of (None, lege stempels)
    bij geen/rev-vreemd anker (→ de kwartierrun valt terug op bootstrap-leren).

    Dit ís het regime van tweeling 2: minder adaptief, maar in een goede staat. De
    kwartierrun drift niet online rond het anker (dat was tweeling 1's regime opnieuw)
    maar pint de params exact op het batch-optimum; adaptatie loopt uitsluitend via de
    wekelijkse, cumulatieve (warm-gestarte) batch-herfit over de volle historie."""
    if not batch.get("params") or batch.get("physics2_rev") != PHYSICS2_REV:
        return None, {"anchor_at": None, "anchor_src": None}
    params = merged_params2(house, {"params": json.loads(json.dumps(batch["params"])),
                                    "physics2_rev": PHYSICS2_REV})
    return params, {"anchor_at": batch.get("fitted_at"),
                    "anchor_src": batch.get("model_version")}


def collect_actual_rh(house: dict, wd: dict, since: datetime) -> dict:
    """Per sensorkamer de werkelijke tado-RH-samples (t, %) vanaf `since` — zelfde
    history-wandeling als am.collect_actual, veld `hum` i.p.v. `temp`."""
    actual = {}
    for rid, room in house.get("rooms", {}).items():
        wd_key = room.get("from_window_data")
        if not wd_key or wd_key not in wd.get("rooms", {}):
            continue
        samples = []
        for s in wd["rooms"][wd_key].get("history", []):
            try:
                ts = datetime.fromisoformat(s["t"])
            except (ValueError, TypeError, KeyError):
                continue
            if ts >= since and s.get("hum") is not None:
                samples.append((ts, float(s["hum"])))
        samples.sort()
        if samples:
            actual[rid] = samples
    return actual


# ── Historie-shards (data/twin2_history/<YYYY-MM>.json) ──────────────────────────────
# Kolom-vorm per kamer (ts epoch-s, temp ×10 int, hum %, heat 0/1) + de weer-rijen
# (fetch_weather-vorm, door de batch/backfill ververst) + de openingen-snapshots.
# De kwartierrun appende alléén verse kamer-samples + log-snapshots (bytes per run);
# het weer wordt bewust NIET per run geappend — de batch haalt het uit het archief.

def _shard_path(month: str) -> str:
    return os.path.join(HISTORY_DIR, f"{month}.json")


def _load_shard(month: str) -> dict:
    try:
        with open(_shard_path(month), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"schema": 1, "month": month, "rooms": {}, "weather": [], "openings": []}


def _write_shard(shard: dict) -> None:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(_shard_path(shard["month"]), "w", encoding="utf-8") as f:
        json.dump(shard, f, ensure_ascii=False, separators=(",", ":"))


def append_history_shard(wd: dict, log: list[dict], now: datetime) -> int:
    """Append verse tado-samples (+ nieuwe openingen-snapshots) aan de maand-shard(s).
    Idempotent: alleen samples/snapshots nieuwer dan het laatst opgeslagen per kamer/log
    worden toegevoegd — een tweede aanroep is een no-op. Snapshots beschermen zo blijvend
    tegen de browser-trim van de Gist-log (laatste ~500). Geeft #toegevoegde samples."""
    added = 0
    by_month: dict[str, dict] = {}

    def shard_for(ts: datetime) -> dict:
        month = ts.strftime("%Y-%m")
        if month not in by_month:
            by_month[month] = _load_shard(month)
        return by_month[month]

    for name, rd in (wd.get("rooms", {}) or {}).items():
        for s in rd.get("history", []):
            try:
                ts = datetime.fromisoformat(s["t"])
            except (ValueError, TypeError, KeyError):
                continue
            if s.get("temp") is None:
                continue
            shard = shard_for(ts)
            slot = shard["rooms"].setdefault(name, {"ts": [], "temp": [], "hum": [], "heat": []})
            epoch = int(ts.timestamp())
            if slot["ts"] and epoch <= slot["ts"][-1]:
                continue
            slot["ts"].append(epoch)
            slot["temp"].append(int(round(s["temp"] * 10)))
            slot["hum"].append(int(round(s["hum"])) if s.get("hum") is not None else None)
            slot["heat"].append(1 if s.get("heat") else 0)
            added += 1
    for entry in log or []:
        try:
            ts = datetime.fromisoformat(entry["t"])
        except (ValueError, TypeError, KeyError):
            continue
        shard = shard_for(ts)
        known = {e.get("t") for e in shard["openings"]}
        if entry.get("t") not in known:
            shard["openings"].append({"t": entry["t"], "states": entry.get("states", {}) or {}})
    for shard in by_month.values():
        shard["openings"].sort(key=lambda e: e.get("t") or "")
        _write_shard(shard)
    return added


def load_dataset(house: dict) -> dict:
    """Merge alle maand-shards tot één trainingsset: per kamer-id de (t, temp)- en
    (t, RH)-samples (gededupliceerd op tijdstip), de stook-tijdstippen, de weer-rijen
    (fetch_weather-vorm) en de samengevoegde openingen-log."""
    name_to_rid = {r.get("from_window_data"): rid
                   for rid, r in house.get("rooms", {}).items() if r.get("from_window_data")}
    samples: dict[str, dict[int, tuple]] = {}
    weather_rows: dict[str, dict] = {}
    log_by_t: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                shard = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for name, cols in (shard.get("rooms") or {}).items():
            rid = name_to_rid.get(name)
            if not rid:
                continue
            slot = samples.setdefault(rid, {})
            ts_arr = cols.get("ts") or []
            for i, epoch in enumerate(ts_arr):
                temp = (cols.get("temp") or [None] * len(ts_arr))[i]
                hum = (cols.get("hum") or [None] * len(ts_arr))[i]
                heat = (cols.get("heat") or [0] * len(ts_arr))[i]
                if temp is None:
                    continue
                slot[epoch] = (temp / 10.0, hum, bool(heat))
        for row in shard.get("weather") or []:
            if row.get("dt"):
                weather_rows[row["dt"]] = row
        for entry in shard.get("openings") or []:
            if entry.get("t"):
                log_by_t[entry["t"]] = entry
    actual, actual_rh, heat_on = {}, {}, {}
    for rid, slot in samples.items():
        t_list = sorted(slot)
        actual[rid] = [(datetime.fromtimestamp(e, TZ), slot[e][0]) for e in t_list]
        rh_list = [(datetime.fromtimestamp(e, TZ), float(slot[e][1]))
                   for e in t_list if slot[e][1] is not None]
        if rh_list:
            actual_rh[rid] = rh_list
        hs = {datetime.fromtimestamp(e, TZ) for e in t_list if slot[e][2]}
        if hs:
            heat_on[rid] = hs
    rows = []
    for iso in sorted(weather_rows):
        r = dict(weather_rows[iso])
        r["dt"] = datetime.fromisoformat(iso)
        rows.append(r)
    log = [log_by_t[t] for t in sorted(log_by_t)]
    return {"actual": actual, "actual_rh": actual_rh, "heat_on": heat_on,
            "weather_rows": rows, "log": log}


def fetch_weather_archive(start: date, end: date) -> list[dict]:
    """Historische uur-drivers in exact de fetch_weather-rijvorm: Open-Meteo-archief
    (ERA5, ~2–5 dagen achterstand) + de forecast-API met past_days voor de verse
    staart. Dezelfde variabelen als am.fetch_weather zodat am.build_timeline ze
    ongewijzigd consumeert."""
    hourly_vars = ("temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,"
                   "wind_direction_10m,wind_gusts_10m,shortwave_radiation,"
                   "direct_radiation,diffuse_radiation")

    def _rows(data: dict) -> list[dict]:
        h = data.get("hourly", {})
        times = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in h.get("time", [])]
        out = []
        for i, t in enumerate(times):
            out.append({"dt": t,
                        "T_out": am._get(h, "temperature_2m", i),
                        "rh": am._get(h, "relative_humidity_2m", i),
                        "precip": am._get(h, "precipitation", i) or 0.0,
                        "wind_speed": am._get(h, "wind_speed_10m", i) or 0.0,
                        "wind_dir": am._get(h, "wind_direction_10m", i) or 0.0,
                        "gust": am._get(h, "wind_gusts_10m", i) or 0.0,
                        "shortwave": am._get(h, "shortwave_radiation", i) or 0.0,
                        "direct": am._get(h, "direct_radiation", i) or 0.0,
                        "diffuse": am._get(h, "diffuse_radiation", i) or 0.0})
        return out

    base = {"latitude": am._LAT, "longitude": am._LON, "hourly": hourly_vars,
            "wind_speed_unit": "ms", "timezone": "Europe/Amsterdam"}
    rows = _rows(get_json("https://archive-api.open-meteo.com/v1/archive",
                          {**base, "start_date": start.isoformat(), "end_date": end.isoformat()},
                          timeout=45, label="open-meteo-archief"))
    rows = [r for r in rows if r["T_out"] is not None]
    last = rows[-1]["dt"].date() if rows else (start - timedelta(days=1))
    if last < end:
        # ERA5-staart ontbreekt → vul bij uit de forecast-API (past_days ≤ 92).
        need_days = min(92, (date.today() - last).days + 1)
        fresh = _rows(get_json("https://api.open-meteo.com/v1/forecast",
                               {**base, "past_days": need_days, "forecast_days": 1},
                               timeout=30, label="open-meteo-staart"))
        cutoff = rows[-1]["dt"] if rows else None
        rows += [r for r in fresh if r["T_out"] is not None
                 and (cutoff is None or r["dt"] > cutoff)]
    return rows


def refresh_shard_weather(rows: list[dict]) -> None:
    """Schrijf de (archief-)weer-rijen terug in hun maand-shards — de batch/backfill
    is de enige schrijver van het shard-weer (de kwartierrun appendt bewust geen weer)."""
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        by_month.setdefault(r["dt"].strftime("%Y-%m"), []).append(r)
    for month, mrows in by_month.items():
        shard = _load_shard(month)
        shard["weather"] = [{**r, "dt": r["dt"].isoformat()} for r in mrows]
        _write_shard(shard)


# ════════════════════════════════════════════════════════════════════════════════════
#  Batch-fit — volle-historie-kalibratie (het ridge-anker voor het online leren)
# ════════════════════════════════════════════════════════════════════════════════════

def batch_windows(t_min: datetime, t_max: datetime) -> list[tuple]:
    """(venster-eind)-lijst: residu-vensters van BATCH_WINDOW_D dagen, stride
    BATCH_STRIDE_D, achterwaarts vanaf t_max zodat het meest recente venster altijd
    meedoet. Elk venster krijgt in de fit BATCH_WARMUP_H sim-only aanloop."""
    ends = []
    end = t_max
    first_end = t_min + timedelta(days=BATCH_WINDOW_D)
    while end >= first_end:
        ends.append(end)
        end = end - timedelta(days=BATCH_STRIDE_D)
    ends.reverse()
    return [(e - timedelta(days=BATCH_WINDOW_D), e) for e in ends]


def _slice_actual(actual: dict, start: datetime, end: datetime) -> dict:
    out = {}
    for rid, samples in actual.items():
        win = [(t, v) for t, v in samples if start <= t <= end]
        if win:
            out[rid] = win
    return out


def batch_fit(house: dict, dataset: dict, time_budget_s: float = BATCH_TIME_BUDGET_S,
              start_params: dict | None = None) -> tuple[dict, dict]:
    """Mini-batch Gauss-Newton over de volle gedolven historie: JᵀWJ/JᵀWr worden per
    epoch over álle vensters geaccumuleerd (Huber-gewogen, géén recency — batch weegt
    de hele historie gelijk), één gedempte stap per epoch, ridge naar de kale PRIORS2.
    AC-/pauze-perioden komen uit de gereconstrueerde log, stook-samples uit de per-
    sample heat-vlaggen. Geeft (params, stats)."""
    log = dataset["log"]
    rows = dataset["weather_rows"]
    actual_all = dataset["actual"]
    rh_all = dataset["actual_rh"]
    ts_all = [t for s in actual_all.values() for t, _ in s]
    if not ts_all or not rows:
        return start_params or default_params2(house), {"windows": 0, "epochs": 0,
                                                        "samples": 0, "rmse_batch": None,
                                                        "rmse_rh_batch": None}
    t_min, t_max = min(ts_all), max(ts_all)

    # Historische filters: stook (per sample), AC + pauze (uit de log).
    acc = am.ac_changes(log)
    p_intervals = am.paused_intervals(am.pause_changes(log), t_max)
    heat_on = dataset["heat_on"]

    def _filtered(d):
        d, _ = am.filter_heating_samples(d, heat_on)
        d, _ = am.filter_ac_samples(d, acc, None, t_max, guard_h=0.0)
        d, _ = am.filter_paused_samples(d, p_intervals)
        return d

    actual_all = _filtered(actual_all)
    rh_all = _filtered(rh_all)

    # Vensters voorbereiden (timeline + slices + seeds), eenmalig vóór de epochs.
    wins = []
    for w_start, w_end in batch_windows(t_min, t_max):
        act = _slice_actual(actual_all, w_start, w_end)
        if not act:
            continue
        rh = _slice_actual(rh_all, w_start, w_end)
        window_h = BATCH_WINDOW_D * 24.0 + BATCH_WARMUP_H
        timeline = am.build_timeline(house, {"hourly": rows}, log, w_end,
                                     window_h, beam_iam=True, end_h=0.0)
        if not timeline:
            continue
        seed = {rid: s[0][1] for rid, s in act.items()}
        for rid in house.get("rooms", {}):
            seed.setdefault(rid, timeline[0]["T_out"])
        seed_w = {rid: w_from_rh(s[0][1], seed.get(rid, 20.0)) for rid, s in rh.items()}
        nb = am.neighbor_temp_estimate(rows, w_end)
        wins.append({"timeline": timeline, "actual": act, "rh": rh,
                     "seed": seed, "seed_w": seed_w, "neighbor": nb})
    if not wins:
        return start_params or default_params2(house), {"windows": 0, "epochs": 0,
                                                        "samples": 0, "rmse_batch": None,
                                                        "rmse_rh_batch": None}

    rooms = sorted({rid for w in wins for rid in w["actual"]})
    keys = _param_keys2(rooms)
    params = start_params or default_params2(house)
    x = params_to_vec2(params, keys)
    prior_vec = [PRIORS2[name] for _, name in keys]
    reg_vec = [reg_weight2(name) for _, name in keys]
    nk = len(keys)

    def _win_res(w, p):
        old_nb = am._NEIGHBOR_TEMP
        am._NEIGHBOR_TEMP = w["neighbor"]
        try:
            return _residuals2(house, p, w["timeline"], w["seed"], w["actual"],
                               w["rh"], set(w["actual"]) | set(w["rh"]), w["seed_w"])
        finally:
            am._NEIGHBOR_TEMP = old_nb

    def _total_cost(xv):
        p = vec_to_params2(xv, keys, params)
        cost = 0.0
        for w in wins:
            cost += am._wcost(_win_res(w, p))
        cost += sum(reg_vec[k2] * (xv[k2] - prior_vec[k2]) ** 2 for k2 in range(nk))
        return cost

    t_start = time.time()
    lam = 1e-3
    best_cost = _total_cost(x)
    epochs = 0
    converged = False
    for _ in range(BATCH_MAX_EPOCHS):
        if time.time() - t_start > time_budget_s:
            break
        JtJ = [[0.0] * nk for _ in range(nk)]
        Jtr = [0.0] * nk
        aborted = False
        p_cur = vec_to_params2(x, keys, params)
        for w in wins:
            if time.time() - t_start > time_budget_s:
                aborted = True
                break
            r = _win_res(w, p_cur)
            if not r:
                continue
            m = len(r)
            hw = am._huber_weights(r)
            J = [[0.0] * nk for _ in range(m)]
            for j in range(nk):
                if time.time() - t_start > time_budget_s:
                    aborted = True
                    break
                dx = max(1e-3, abs(x[j]) * 0.05)
                xj = x[:]
                xj[j] += dx
                rj = _win_res(w, vec_to_params2(xj, keys, params))
                if len(rj) != m:
                    continue
                for i in range(m):
                    J[i][j] = (rj[i] - r[i]) / dx
            if aborted:
                break
            for a in range(nk):
                for b in range(a, nk):
                    v = sum(hw[i] * J[i][a] * J[i][b] for i in range(m))
                    JtJ[a][b] += v
                    if a != b:
                        JtJ[b][a] += v
                Jtr[a] += sum(hw[i] * J[i][a] * r[i] for i in range(m))
        if aborted:
            break
        for a in range(nk):
            JtJ[a][a] += reg_vec[a]
            Jtr[a] += reg_vec[a] * (x[a] - prior_vec[a])
            JtJ[a][a] += lam * (JtJ[a][a] + 1.0)
        delta = am.solve_linear(JtJ, [-v for v in Jtr])
        if delta is None:
            break
        x_new = _clamp_to_bounds2([x[j] + delta[j] for j in range(nk)], keys)
        new_cost = _total_cost(x_new)
        epochs += 1
        if math.isnan(new_cost) or new_cost >= best_cost:
            lam *= 4.0
            if lam > 1e6:
                break
            continue
        improved = (best_cost - new_cost) / max(best_cost, 1e-9)
        lam = max(1e-4, lam / 3.0)
        x = x_new
        best_cost = new_cost
        if improved < BATCH_CONVERGED_RTOL:
            converged = True   # geaccepteerde stap zonder materiële winst → klaar
            break

    params = vec_to_params2(x, keys, params)
    # Eind-RMSE over alle vensters met de definitieve params.
    rt_all, rr_all = [], []
    for w in wins:
        old_nb = am._NEIGHBOR_TEMP
        am._NEIGHBOR_TEMP = w["neighbor"]
        try:
            sim = simulate2(house, params, w["timeline"], w["seed"],
                            calib_only_rooms=set(w["actual"]) | set(w["rh"]),
                            seed_w=w["seed_w"])
        finally:
            am._NEIGHBOR_TEMP = old_nb
        for rid, samples in w["actual"].items():
            pred = sim["series"].get(rid, [])
            if not pred:
                continue
            pred = am._to_sensor_series(house, w["timeline"], rid, pred)
            rt_all += [am._interp(pred, ts) - val for ts, val in samples]
        for rid, samples in w["rh"].items():
            pred = sim["series_rh"].get(rid, [])
            if not pred:
                continue
            rr_all += [am._interp(pred, ts) - val for ts, val in samples]
    rmse_b, rmse_rh_b = am.rmse(rt_all), am.rmse(rr_all)
    stats = {"windows": len(wins), "epochs": epochs, "converged": converged,
             "samples": len(rt_all),
             "rmse_batch": round(rmse_b, 3) if rmse_b == rmse_b else None,
             "rmse_rh_batch": round(rmse_rh_b, 2) if rmse_rh_b == rmse_rh_b else None,
             "span": {"start": t_min.isoformat(), "end": t_max.isoformat()}}
    return params, stats


def batch_start_params(house: dict, prev: dict) -> dict | None:
    """Warm-start-params voor de batch: het vorige anker (aangevuld met priors voor
    nieuwe kamers/keys), of None bij geen/rev-vreemd anker (→ kale priors)."""
    if prev.get("params") and prev.get("physics2_rev") == PHYSICS2_REV:
        return merged_params2(house, {"params": prev["params"],
                                      "physics2_rev": PHYSICS2_REV})
    return None


def batch_main():
    """`python airflow2_model.py --batch`: ververs het shard-weer uit het archief,
    fit over de volle historie en schrijf het batch-anker (docs/airflow2_batch.json).
    De kwartierrun pint zich op zijn volgende iteratie op het anker vast (anchor_from_batch)."""
    now = datetime.now(TZ)
    print(f"[batch] Start — {now.isoformat()}")
    house = am.load_house()
    loc = house.get("location", {})
    am._LAT = loc.get("lat", am._LAT)
    am._LON = loc.get("lon", am._LON)

    dataset = load_dataset(house)
    ts_all = [t for s in dataset["actual"].values() for t, _ in s]
    if not ts_all:
        print(f"[batch] geen trainingsdata in {HISTORY_DIR} — draai eerst de backfill "
              "(tools/twin2_backfill.py / airflow2-backfill.yml). Stop.")
        return
    t_min, t_max = min(ts_all), max(ts_all)
    print(f"[batch] dataset: {sum(len(s) for s in dataset['actual'].values())} samples, "
          f"{len(dataset['actual'])} kamers, {t_min.date()} → {t_max.date()}")

    # Weer verversen over de dataset-spanwijdte (incl. warmup-aanloop) en terugschrijven.
    rows = fetch_weather_archive((t_min - timedelta(hours=BATCH_WARMUP_H + 24)).date(),
                                 t_max.date())
    refresh_shard_weather(rows)
    dataset["weather_rows"] = rows
    print(f"[batch] weer ververst: {len(rows)} uur-rijen.")

    # Warm-start vanaf het vorige batch-anker (rev-passend): één epoch over alle
    # vensters kost ~15–20 min, dus het budget laat maar ~2 gedempte stappen per run
    # toe — vanaf de kale priors zou de wekelijkse batch daardoor nóóit verder dan
    # 2 stappen convergeren. Doorstarten op het vorige optimum maakt de batches
    # cumulatief (mirror van het online leren dat over runs convergeert). De ridge
    # blijft naar de kale priors trekken, dus een fossiel kan niet wegdriften.
    prev = load_batch()
    start_params = batch_start_params(house, prev)
    if start_params is not None:
        print(f"[batch] warm-start vanaf het vorige anker (gefit {prev.get('fitted_at')}).")
    # Budget expliciet op call-time doorgeven (een def-time default zou de module-
    # constante bevriezen — env-override/test-monkeypatch werkte dan niet).
    params, stats = batch_fit(house, dataset, time_budget_s=BATCH_TIME_BUDGET_S,
                              start_params=start_params)
    rails = railed_params2(params)
    if rails:
        print(f"[batch][saturatie] params op hun grens: {', '.join(rails)}")
    result = {"fitted_at": now.isoformat(), "model_version": am.model_version(),
              "physics2_rev": PHYSICS2_REV, "params": params, "railed": rails, **stats}
    os.makedirs(os.path.dirname(BATCH_FILE) or ".", exist_ok=True)
    with open(BATCH_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[batch] klaar: {stats['windows']} vensters, {stats['epochs']} epochs, "
          f"RMSE {stats['rmse_batch']}°C / RH {stats['rmse_rh_batch']}% → {BATCH_FILE}")


# ════════════════════════════════════════════════════════════════════════════════════
#  Dashboard + main (kwartierrun)
# ════════════════════════════════════════════════════════════════════════════════════

def _twin1_snapshot() -> dict | None:
    """Compacte tweeling-1-stand (uit diens learned-artefact) voor het vergelijk-paneel."""
    try:
        with open(am.LEARNED_FILE, encoding="utf-8") as f:
            l1 = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    hist = l1.get("rmse_history") or []
    last = hist[-1] if hist else {}
    # Bewust géén volledige rmse_history hier: het dashboard haalt tweeling 1's
    # learned-artefact zelf op voor de vergelijk-curve (anders dupliceert elke run
    # ~1000 punten in dit artefact).
    return {"rmse": l1.get("rmse"), "skill": last.get("skill"),
            "updated_at": l1.get("updated_at")}


def build_dashboard2(house, params, weather, wd, timeline, sim, learned, actual,
                     actual_rh, now, rmse_now, rmse_rh_now, log, learning_held=False,
                     baseline=None, skill=None, rmse_baseline=None, wx=None,
                     ac_room=None, ac_excluded=None, heat_now=None, heat_excluded=None,
                     paused_now=False, paused_since=None, pause_excluded=None,
                     anchor_stamps=None, pinned=False) -> dict:
    """Stel docs/airflow2_data.json samen (additief schema, veldnamen mirroren
    airflow_data.json waar het kan zodat de frontend-logica herbruikbaar blijft)."""
    cur = weather["current"]
    sun_az, sun_el = am.sun_position(am._LAT, am._LON, now.astimezone(timezone.utc))
    states_now = am.openings_at(log, now)
    now_step = None
    for s in (timeline or []):
        if s["t"] <= now:
            now_step = s
        else:
            break
    if now_step is None and timeline:
        now_step = timeline[-1]
    ta_all = sim.get("Ta_now", {})
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    net_now = None
    if now_step is not None:
        ops = build_openings2(house, now_step["states"], now_step["weather"], params,
                              ta_all, now_step["T_out"])
        net_now = am.solve_network(zones, ops, ta_all, now_step["T_out"])

    horizon = now - timedelta(hours=CALIB_WINDOW_H)
    rooms_out = {}
    for rid, room in house.get("rooms", {}).items():
        wd_key = room.get("from_window_data")
        rd = wd.get("rooms", {}).get(wd_key, {}) if wd_key else {}
        pred_series = sim["series"].get(rid, [])
        sens_series = am._to_sensor_series(house, timeline, rid, pred_series)
        rh_series = sim["series_rh"].get(rid, [])
        ta_now = ta_all.get(rid)
        frac = room.get("sensor_outdoor_frac", 0.0)
        t_out_now = now_step["T_out"] if now_step else None
        pred_now = am._sensor_temp(ta_now, t_out_now, frac)
        act_now = rd.get("inside")
        err = (pred_now - act_now) if (pred_now is not None and act_now is not None) else None
        pred_rh_now = rh_from_w(sim.get("W_now", {}).get(rid), ta_now)
        act_rh_now = rd.get("humidity")
        rh_err = (pred_rh_now - act_rh_now) if (pred_rh_now is not None
                                                and act_rh_now is not None) else None
        ach = None
        if net_now is not None:
            vol = room.get("volume_m3", 40.0)
            ach = round(net_now["fresh"].get(rid, 0.0) * 3600.0 / vol, 2)
        rooms_out[rid] = {
            "label": room.get("label", rid),
            "from_window_data": wd_key,
            "actual_temp": act_now,
            "predicted_temp": round(pred_now, 2) if pred_now is not None else None,
            "predicted_air_temp": round(ta_now, 2) if ta_now is not None else None,
            "predicted_fast_temp": (round(sim.get("Tf_now", {}).get(rid), 2)
                                    if sim.get("Tf_now", {}).get(rid) is not None else None),
            "predicted_deep_temp": (round(sim.get("Td_now", {}).get(rid), 2)
                                    if sim.get("Td_now", {}).get(rid) is not None else None),
            "sensor_outdoor_frac": frac,
            "error": round(err, 2) if err is not None else None,
            "humidity": act_rh_now,
            "predicted_rh": round(pred_rh_now, 1) if pred_rh_now is not None else None,
            "rh_error": round(rh_err, 1) if rh_err is not None else None,
            "ach": ach,
            "solar_w": round(now_step["irr"].get(rid, 0.0), 0) if now_step else None,
            "comfort_low": ROOM_COMFORT.get(wd_key, (None, None))[0] if wd_key else None,
            "comfort_high": ROOM_COMFORT.get(wd_key, (None, None))[1] if wd_key else None,
            "ac": (rid == ac_room),
            "ac_excluded_samples": (ac_excluded or {}).get(rid, 0),
            "heating": bool((heat_now or {}).get(rid)),
            "heat_excluded_samples": (heat_excluded or {}).get(rid, 0),
            "paused": bool(paused_now),
            "pause_excluded_samples": (pause_excluded or {}).get(rid, 0),
            "subzones": sim.get("sub_now", {}).get(rid),
            "predicted_series": [{"t": t.isoformat(), "temp": round(v, 2)}
                                 for t, v in sens_series if t >= horizon],
            "actual_series": [{"t": t.isoformat(), "temp": v} for t, v in actual.get(rid, [])],
            "predicted_rh_series": [{"t": t.isoformat(), "rh": round(v, 1)}
                                    for t, v in rh_series if v is not None and t >= horizon],
            "actual_rh_series": [{"t": t.isoformat(), "rh": v}
                                 for t, v in (actual_rh or {}).get(rid, [])],
            "params": params.get(rid, {}),
        }

    rmse_hist = (learned.get("rmse_history") or [])[:]
    if rmse_now == rmse_now:
        entry = {"t": now.isoformat(), "rmse": round(rmse_now, 3),
                 "held": bool(learning_held), "paused": bool(paused_now),
                 "version": am.model_version()}
        if rmse_rh_now == rmse_rh_now:
            entry["rmse_rh"] = round(rmse_rh_now, 2)
        if skill is not None:
            entry["skill"] = skill
        if rmse_baseline is not None and rmse_baseline == rmse_baseline:
            entry["rmse_naive"] = round(rmse_baseline, 3)
        if wx:
            entry["wx"] = wx
        if pinned:
            entry["pinned"] = True     # vastgepind op het anker — pure model-kwaliteit
        if (anchor_stamps or {}).get("anchor_at") and \
                anchor_stamps.get("anchor_at") != learned.get("anchor_at"):
            entry["anchored"] = True   # markeer het punt waarop een (nieuw) batch-anker landde
        rmse_hist.append(entry)
    rmse_hist = am.thin_rmse_history(rmse_hist, now)

    stamps = anchor_stamps or {}
    return {
        "generated_at": utc_now_iso(),
        "as_of_local": now.isoformat(),
        "source": "airflow2_model",
        "model_version": am.model_version(),
        "physics2_rev": PHYSICS2_REV,
        "weather": {
            "outside_temp": cur.get("temperature_2m"),
            "outside_humidity": cur.get("relative_humidity_2m"),
            "outside_source": cur.get("outside_source", "open-meteo"),
            "wind_speed": cur.get("wind_speed_10m"), "wind_dir": cur.get("wind_direction_10m"),
            "gust": cur.get("wind_gusts_10m"), "shortwave": cur.get("shortwave_radiation"),
            "sun_az": round(sun_az, 1), "sun_el": round(sun_el, 1),
            "neighbor_temp": round(am._NEIGHBOR_TEMP, 1),
            "wu_solar_scale": (round(weather.get("wu_solar_scale"), 2)
                               if weather.get("wu_solar_scale") is not None else None),
        },
        "openings": states_now,
        "paused": bool(paused_now),
        "paused_since": paused_since.isoformat() if paused_since else None,
        "ac": {"room": ac_room},
        "rooms": rooms_out,
        "learned": {"params": params,
                    "rmse": round(rmse_now, 3) if rmse_now == rmse_now else None,
                    "rmse_rh": round(rmse_rh_now, 2) if rmse_rh_now == rmse_rh_now else None,
                    "rmse_naive": (round(rmse_baseline, 3)
                                   if (rmse_baseline is not None and rmse_baseline == rmse_baseline)
                                   else None),
                    "skill": skill,
                    "rmse_history": rmse_hist,
                    "held": bool(learning_held), "paused": bool(paused_now),
                    "pinned": bool(pinned),
                    "solver_failures": sim.get("solver_failures", 0),
                    "baseline_rmse": round(baseline, 3) if baseline is not None else None,
                    "anchor_at": stamps.get("anchor_at"),
                    "anchor_src": stamps.get("anchor_src"),
                    "wx": wx or None},
        "twin1": _twin1_snapshot(),
    }


def main():
    now = datetime.now(TZ)
    print(f"[airflow2] Start — {now.isoformat()}")

    house = am.load_house()
    loc = house.get("location", {})
    # De hergebruikte am-helpers (build_timeline, sun_position, dashboard-paden) lezen
    # module-globalen — dezelfde rebind-stap als night_forecast.py (getest).
    am._LAT = loc.get("lat", am._LAT)
    am._LON = loc.get("lon", am._LON)

    wd = am.load_window_data()
    weather = am.fetch_weather()
    # Buiten-nu verfijnen met het eigen WU-station + de zon-herschaling van de glas-
    # drive — exact het tweeling-1-recept (zie am.main voor de volledige toelichting).
    cur = weather.get("current", {}) or {}
    wu_temp, wu_solar, wu_humid = fetch_wu_current_temp()
    wu_solar_scale = None
    if wu_temp is not None:
        solar_now = wu_solar if wu_solar is not None else cur.get("shortwave_radiation")
        cur["temperature_2m"] = round(correct_temp(wu_temp, solar_now), 1)
        cur["outside_source"] = "wu"
        if wu_humid is not None:
            cur["relative_humidity_2m"] = wu_humid
        om_solar_now = cur.get("shortwave_radiation")
        if (wu_solar is not None and om_solar_now and om_solar_now >= am.WU_SOLAR_MIN_WM2
                and wu_solar >= am.WU_SOLAR_MIN_WM2):
            wu_solar_scale = min(am.WU_SOLAR_SCALE_MAX,
                                 max(am.WU_SOLAR_SCALE_MIN, wu_solar / om_solar_now))
    else:
        cur["outside_source"] = "open-meteo"
    weather["current"] = cur
    weather["wu_solar_scale"] = wu_solar_scale

    log = am.load_openings_log()
    am._OPENINGS_CACHE = log
    am._NEIGHBOR_TEMP = am.neighbor_temp_estimate(weather.get("hourly", []), now)

    learned = load_learned2()
    batch = load_batch()
    # Vastgepind regime: is er een rev-passend batch-anker, dan zíjn de params het
    # anker — geen online drift (zie anchor_from_batch). Zonder anker (verse deploy)
    # bootstrap-leert de kwartierrun zoals tweeling 1, tot de eerste batch landt.
    anchor_params, anchor_stamps = anchor_from_batch(house, batch)
    pinned = anchor_params is not None
    params = anchor_params if pinned else merged_params2(house, learned)
    if pinned:
        print(f"[anker] vastgepind op het batch-anker (gefit {anchor_stamps['anchor_at']}, "
              f"rmse_batch {batch.get('rmse_batch')}°C, {batch.get('epochs')} epochs).")

    since = now - timedelta(hours=CALIB_WINDOW_H)
    actual = am.collect_actual(house, wd, since)
    actual_rh = collect_actual_rh(house, wd, since)
    seed_src = {rid: s[0][1] for rid, s in actual.items() if s}
    seed_w_src = {rid: w_from_rh(s[0][1], seed_src.get(rid, 20.0))
                  for rid, s in actual_rh.items() if s}

    # Dezelfde exclusie-filters als tweeling 1 (AC / verwarming / pauze), toegepast op
    # béíde kanalen zodat een uitgesloten kamer ook niet via haar RH mee-leert.
    acc = am.ac_changes(log)
    ac_room_now = am.ac_room_at(acc, now)
    actual, ac_excluded = am.filter_ac_samples(actual, acc, ac_room_now, now)
    actual_rh, _ = am.filter_ac_samples(actual_rh, acc, ac_room_now, now)
    heat_on = am.collect_heating_on(house, wd, since)
    heat_now = am.heating_now(house, wd)
    actual, heat_excluded = am.filter_heating_samples(actual, heat_on)
    actual_rh, _ = am.filter_heating_samples(actual_rh, heat_on)
    pchanges = am.pause_changes(log)
    p_intervals = am.paused_intervals(pchanges, now)
    paused_now = am.paused_at(pchanges, now)
    paused_since = p_intervals[-1][0] if paused_now and p_intervals else None
    actual, pause_excluded = am.filter_paused_samples(actual, p_intervals)
    actual_rh, _ = am.filter_paused_samples(actual_rh, p_intervals)

    timeline = am.build_timeline(house, weather, log, now, CALIB_WINDOW_H + WARMUP_H,
                                 wu_solar_scale=wu_solar_scale, beam_iam=True)
    if not timeline:
        print("[airflow2] Geen weerdata → kan niet simuleren. Stop.")
        sys.exit(1)

    seed = dict(seed_src)
    for rid in house.get("rooms", {}):
        seed.setdefault(rid, timeline[0]["T_out"])

    wx_summary = am.window_weather_summary(weather, now, CALIB_WINDOW_H)

    rmse_now = float("nan")
    rmse_rh_now = float("nan")
    learning_held = False
    baseline = None
    if actual:
        print(f"[eval] {sum(len(v) for v in actual.values())} temp- + "
              f"{sum(len(v) for v in actual_rh.values())} RH-samples over "
              f"{len(actual)} kamers.")
        if pinned:
            # Vastgepind: alleen evalueren — de fout op dit venster is pure model-
            # kwaliteit (geen fit die 'm net heeft gladgestreken). Geen anomalie-poort
            # nodig: er valt niets te beschermen, en tweeling 1 nudget al voor de
            # gedeelde openingen-log.
            rmse_now, rmse_rh_now = rmse_split2(house, params, timeline, seed,
                                                actual, actual_rh, seed_w_src)
            print(f"[eval] RMSE op het anker: {rmse_now:.3f}°C / RH {rmse_rh_now:.2f}%")
        else:
            rmse_cur, rmse_rh_cur = rmse_split2(house, params, timeline, seed,
                                                actual, actual_rh, seed_w_src)
            # Bootstrap-leren (nog geen anker): anomalie-poort met tweeling-1-semantiek
            # (log↔werkelijkheid verdacht → params bevriezen, wél blijven voorspellen).
            anomaly_held, baseline = am.should_hold_learning(rmse_cur,
                                                             learned.get("rmse_history", []))
            learning_held = anomaly_held or paused_now
            if learning_held:
                rmse_now, rmse_rh_now = rmse_cur, rmse_rh_cur
                why = "gepauzeerd" if paused_now else f"anomaal (norm {baseline:.2f}°C)"
                print(f"[leren] vastgehouden — {why}; RMSE {rmse_cur:.2f}°C.")
            else:
                params, rmse_now, rmse_rh_now = calibrate2(house, params, timeline, seed,
                                                           actual, actual_rh,
                                                           seed_w=seed_w_src)
                print(f"[leren] RMSE na bootstrap-kalibratie: {rmse_now:.3f}°C / "
                      f"RH {rmse_rh_now:.2f}%")
                rails = railed_params2(params)
                if rails:
                    print(f"[saturatie] params op hun grens: {', '.join(rails)}")
    else:
        print("[eval] geen werkelijke kamertemps in het venster → alleen voorspellen.")
    learning_held = learning_held or (paused_now and not pinned)

    rmse_baseline = am.naive_rmse(actual) if actual else float("nan")
    skill = am.skill_score(rmse_now, rmse_baseline)

    sim = simulate2(house, params, timeline, seed,
                    calib_only_rooms=set(house.get("rooms", {}).keys()),
                    snapshot_t=now, seed_w=seed_w_src)
    if sim.get("solver_failures"):
        print(f"[sim] {sim['solver_failures']} bijna-singuliere substap(pen).")

    # Trainingsset laten meegroeien: verse tado-samples + nieuwe log-snapshots naar de
    # maand-shard (bytes per run; het weer ververst de batch uit het archief).
    try:
        n_added = append_history_shard(wd, log, now)
        if n_added:
            print(f"[historie] {n_added} samples geappend aan {HISTORY_DIR}.")
    except OSError as e:
        print(f"[historie] append overgeslagen: {e}")

    dash = build_dashboard2(house, params, weather, wd, timeline, sim, learned,
                            actual, actual_rh, now, rmse_now, rmse_rh_now, log,
                            learning_held=learning_held, baseline=baseline, skill=skill,
                            rmse_baseline=rmse_baseline, wx=wx_summary,
                            ac_room=ac_room_now, ac_excluded=ac_excluded,
                            heat_now=heat_now, heat_excluded=heat_excluded,
                            paused_now=paused_now, paused_since=paused_since,
                            pause_excluded=pause_excluded, anchor_stamps=anchor_stamps,
                            pinned=pinned)
    os.makedirs(os.path.dirname(DASHBOARD2_FILE) or ".", exist_ok=True)
    with open(DASHBOARD2_FILE, "w", encoding="utf-8") as f:
        json.dump(dash, f, ensure_ascii=False, indent=2)
    with open(LEARNED2_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now.isoformat(), "model_version": am.model_version(),
                   "physics2_rev": PHYSICS2_REV,
                   "params": params,
                   "rmse": round(rmse_now, 3) if rmse_now == rmse_now else None,
                   "rmse_rh": round(rmse_rh_now, 2) if rmse_rh_now == rmse_rh_now else None,
                   "railed": railed_params2(params),
                   "pinned": pinned,
                   "anchor_at": anchor_stamps.get("anchor_at"),
                   "anchor_src": anchor_stamps.get("anchor_src"),
                   "rmse_history": dash["learned"]["rmse_history"]},
                  f, ensure_ascii=False, indent=2)
    print(f"[airflow2] Geschreven: {DASHBOARD2_FILE} + {LEARNED2_FILE}. Klaar.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Ventilatie 2 — tweede digital twin")
    ap.add_argument("--batch", action="store_true",
                    help="volle-historie-batch-fit (schrijft het ridge-anker)")
    args = ap.parse_args()
    if args.batch:
        # Wekelijkse/handmatige batch: één-shot, direct alerten bij een echte crash.
        run_guarded(batch_main, "airflow-twin-2-batch")
    else:
        # Kwartierloop: zelfde drempel als tweeling 1 — pas alerten bij ~1,5u storing.
        run_guarded(main, "airflow-twin-2", fail_threshold=6)

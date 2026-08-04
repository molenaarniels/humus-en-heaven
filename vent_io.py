#!/usr/bin/env python3
"""
vent_io.py — data-loodgieterij van de ventilatie-tweeling (Project 13).

Loaders (huisgeometrie, window_data, geleerde staat incl. de PHYSICS_REV-poort),
openingen-log-reconstructie (incl. de speciale `ac_room`/`paused`-sleutels),
Open-Meteo-fetch + WU-buiten-nu-verfijning, de driver-tijdlijn (build_timeline,
met de geleerde om_bias-correctie als enige plek waar de buitentemperatuur de
fysica binnenkomt), en de maand-shards (data/twin2_history — de trainingsset
voor offline evaluatie en het seeden, geërfd van tweeling 2).

Geen muteerbare module-globals: make_context() bouwt de RunContext die alle
fysica-aanroepen expliciet meekrijgen (vent_physics).

Env: GIST_ID/GIST_TOKEN (openingen-log, read-only), WU_STATION_ID/WU_API_KEY
(buiten-nu-verfijning), VENT_DATA_PATH/VENT_LEARNED_PATH/VENT_HISTORY_DIR/
HOUSE_MODEL_PATH/WINDOW_DATA_PATH (test-overrides).
"""

import glob
import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone

import shared_const
import om_bias
from gist_io import read_json as gist_read_json
from http_util import get_json
from wu_bias import correct_temp
from window_advisor import convert_rh, fetch_wu_current_temp

import vent_physics as vp
from vent_physics import (
    RunContext,
    SOLAR_SUBSTEPS, WU_SOLAR_SCALE_DECAY_H,
    WU_SOLAR_SCALE_MIN, WU_SOLAR_SCALE_MAX, WU_SOLAR_MIN_WM2,
    PRIORS, PER_ROOM_PARAMS, GLOBAL_PARAMS,
    clamp_model_bounds, facade_irradiance, per_window_solar, sun_position,
)

HOUSE_FILE     = os.getenv("HOUSE_MODEL_PATH", "house_model.json")
WINDOW_DATA    = os.getenv("WINDOW_DATA_PATH", "docs/window_data.json")
DASHBOARD_FILE = os.getenv("VENT_DATA_PATH", "docs/vent_data.json")
LEARNED_FILE   = os.getenv("VENT_LEARNED_PATH", "docs/vent_learned.json")
# Browser-payload van de 12u-vooruitblik (weer-only drivers + thermische params + het
# geankerde zaad) — de speeltuin rekent daarmee raamstand-scenario's lokaal door. Apart van
# vent_data.json: dát is een slank dashboard-schema en dit is een andere consument.
FORECAST_FILE  = os.getenv("VENT_FORECAST_PATH", "docs/vent_forecast.json")
OPENINGS_FILE  = "house_openings.json"

# Fysica-revisie van de tweeling. Bij een mismatch met de opgeslagen learned-staat reset
# merged_params ALLE params naar hun priors (de oude waren compensaties voor de oude
# fysica) en slaat de runner de anomalie-poort die ene run over; daarna her-seeden met
# tools/vent_seed.py zodat de reset niet live vanaf priors hoeft te leren. Rev 6 = de
# lijn van airflow_model rev 5 (eenzijdige ventilatie + DNI-conventie + binnengordijn-
# factor), voortgezet als startpunt van de herbouw (Project 13).
# Rev 7 (aug 2026): de kruipruimte-verankering (`GROUND_AIR_COUPLING` 0.5 → 1.0) + de
# per-kamer parametervloer (`param_bounds`, living.c_mass ≥ 0.595). De geleerde params
# absorbeerden de te koude kruipruimte — een staande put van ~250 W onder living — dus ze
# moeten terug naar hun priors i.p.v. die compensatie de nieuwe fysica in te dragen. Zie
# tools/horizon_backtest.py voor de meting en AIRFLOW3_ASSESSMENT.md voor de motivering;
# her-seed na deploy met tools/vent_seed.py.
PHYSICS_REV = 7

TZ = shared_const.TZ

# Speciale sleutel in een openingen-log-snapshot: in wélke kamer de (ene, mobiele) airco staat
# — een room-id, of "" / "geen" = geen airco. Voorwaarts geaccumuleerd zoals elke andere stand.
# Bewust géén element-id: build_openings kent 'm niet → raakt het luchtstroomnetwerk nooit. Het
# model heeft géén actieve-koel-term, dus de AC-kamer wordt alleen uit de KALIBRATIE gelaten
# (zie main); ze blijft wél voorspeld + getoond.
AC_STATE_KEY = "ac_room"

# Speciale sleutel in een openingen-log-snapshot: is het huis nu gepauzeerd (huis-breed, geen
# room-id)? True zolang iemand anders — niet de betrouwbare melder — thuis kan zijn; niemand
# meldt dan de raam/rooster/deur-standen betrouwbaar. Voorwaarts geaccumuleerd zoals ac_room.
# Anders dan AC_STATE_KEY raakt dit ALLE kamers tegelijk (het is geen kamer-uitsluiting maar een
# leer-gate — zie main), en anders dan de AC-guard is géén apart guard-venster nodig: een nog-
# actieve pauze is per definitie open-eindig tot nu (zie paused_intervals) en sluit recente
# samples dus vanzelf uit.
PAUSE_STATE_KEY = "paused"

# Kalibratievenster + integratie.
CALIB_WINDOW_H = 48.0    # uur historie waarover we de fout minimaliseren (~2 dag-nacht-cycli;

                         # window_data houdt ~48u tado-historie → traag-veranderende termen
                         # ua_party/q_int/c_mass worden identificeerbaar i.p.v. naar hun grens
                         # te driften op een half-daags venster)
WARMUP_H       = 24.0    # uur sim-only aanloop vóór het residu-venster: de trage massaknoop

                         # equilibreert zodat zijn beginwaarde geen vrije laagfrequente bias is.
                         # Drivers reiken ver genoeg terug via Open-Meteo past_days; residuen
                         # tellen alleen waar tado-samples bestaan (≤CALIB_WINDOW_H terug)
CALIB_COVERAGE_WARN_H = 24.0   # effectieve grond-waarheid-spanwijdte (na AC-/verwarmings-/

def openings_at(log: list[dict], when: datetime) -> dict:
    """Actieve gerapporteerde toestand per element op tijdstip `when`, voorwaarts
    geaccumuleerd: elk element houdt zijn laatst-gezette waarde tot het opnieuw gemeld
    wordt. Zo kun je kleine, losse wijzigingen melden (één raam) zonder de rest te
    herhalen, en weerspiegelt de toestand wat écht open/dicht staat. Lege dict als er
    niets vóór `when` is gelogd."""
    entries = []
    for entry in log:
        try:
            t = datetime.fromisoformat(entry["t"])
        except (ValueError, TypeError, KeyError):
            continue
        if t <= when:
            entries.append((t, entry.get("states", {}) or {}))
    entries.sort(key=lambda e: e[0])
    state: dict = {}
    for _, st in entries:
        state.update(st)
    return state

def _norm_ac_room(value) -> str | None:
    """Normaliseer de gerapporteerde AC-kamer naar een room-id, of None (geen airco)."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("", "geen", "none", "off", "uit", "-"):
        return None
    return s

def ac_changes(log: list[dict]) -> list[tuple]:
    """Chronologische (tijdstip, room-id|None) AC-toewijzingen uit de openingen-log: elk
    snapshot dat de `ac_room`-sleutel zet. Voorwaarts uit te lezen met `ac_room_at`."""
    out = []
    for entry in log:
        st = entry.get("states", {}) or {}
        if AC_STATE_KEY not in st:
            continue
        try:
            t = datetime.fromisoformat(entry["t"])
        except (ValueError, TypeError, KeyError):
            continue
        out.append((t, _norm_ac_room(st[AC_STATE_KEY])))
    out.sort(key=lambda c: c[0])
    return out

def ac_room_at(changes: list[tuple], when: datetime) -> str | None:
    """Welke kamer de airco had op tijdstip `when` (voorwaarts geaccumuleerd), of None."""
    room = None
    for t, r in changes:
        if t <= when:
            room = r
        else:
            break
    return room

def _norm_paused(value) -> bool:
    """Normaliseer de gerapporteerde pauze-stand naar een bool."""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    return s in ("true", "1", "paused", "gepauzeerd", "aan", "ja")

def pause_changes(log: list[dict]) -> list[tuple]:
    """Chronologische (tijdstip, paused-bool) wijzigingen uit de openingen-log: elk snapshot
    dat de `paused`-sleutel zet. Voorwaarts uit te lezen met `paused_at`."""
    out = []
    for entry in log:
        st = entry.get("states", {}) or {}
        if PAUSE_STATE_KEY not in st:
            continue
        try:
            t = datetime.fromisoformat(entry["t"])
        except (ValueError, TypeError, KeyError):
            continue
        out.append((t, _norm_paused(st[PAUSE_STATE_KEY])))
    out.sort(key=lambda c: c[0])
    return out

def paused_at(changes: list[tuple], when: datetime) -> bool:
    """Was het huis gepauzeerd op tijdstip `when` (voorwaarts geaccumuleerd)? Vóór de eerste
    melding: niet gepauzeerd (False)."""
    val = False
    for t, v in changes:
        if t <= when:
            val = v
        else:
            break
    return val

def paused_intervals(changes: list[tuple], now: datetime) -> list[tuple]:
    """Zet de boolean-wisselingen om in (start, eind)-tupels waarin het huis gepauzeerd was. Een
    nog-actieve pauze (geen latere 'uit'-melding) loopt open-eindig door tot `now` — dat sluit
    recente samples vanzelf uit, zónder apart guard-venster zoals bij de airco (die guard bestaat
    juist omdát een 'staat nu hier'-melding niet met een interval-eind samenvalt; hier IS
    'nu, nog actief' letterlijk het interval-eind)."""
    intervals = []
    start = None
    for t, v in changes:
        if v and start is None:
            start = t
        elif not v and start is not None:
            intervals.append((start, t))
            start = None
    if start is not None:
        intervals.append((start, now))
    return intervals

def collect_heating_on(house: dict, wd: dict, since: datetime) -> dict[str, set]:
    """Per sensorkamer de tijdstippen (datetime) sinds `since` waarop tado meldde dat er
    gestookt werd (`heat`-vlag in de window_data.json-history). Leeg → geen stook-samples.
    Leest dezelfde history als `collect_actual`, zodat de tijdstippen exact matchen."""
    out: dict[str, set] = {}
    for rid, room in house.get("rooms", {}).items():
        wd_key = room.get("from_window_data")
        if not wd_key or wd_key not in wd.get("rooms", {}):
            continue
        on = set()
        for s in wd["rooms"][wd_key].get("history", []):
            try:
                ts = datetime.fromisoformat(s["t"])
            except (ValueError, TypeError, KeyError):
                continue
            if ts >= since and s.get("temp") is not None and s.get("heat"):
                on.add(ts)
        if on:
            out[rid] = on
    return out

def heating_now(house: dict, wd: dict) -> dict[str, bool]:
    """Per sensorkamer of tado nú meldt dat er gestookt wordt (`heating`-vlag op de kamer in
    window_data.json) — voor de dashboard-chip + de 'niet-gekalibreerd'-melding."""
    out: dict[str, bool] = {}
    for rid, room in house.get("rooms", {}).items():
        wd_key = room.get("from_window_data")
        if wd_key and wd_key in wd.get("rooms", {}):
            out[rid] = bool(wd["rooms"][wd_key].get("heating"))
    return out

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

_MODEL_VERSION = None

def model_version() -> str:
    """Korte code-versie (git short-SHA) van de draaiende runner, zodat elk RMSE-punt aan een
    codeversie te koppelen is — "heeft iteratie N de fout echt verbeterd?" wordt dan een
    correlatie op de data zelf, geen git-archeologie. Prefereert `GITHUB_SHA` (gezet in de
    Action), valt terug op `git rev-parse`, dan 'unknown'. Gecachet per proces."""
    global _MODEL_VERSION
    if _MODEL_VERSION is not None:
        return _MODEL_VERSION
    sha = os.getenv("GITHUB_SHA")
    if sha:
        _MODEL_VERSION = sha[:7]
        return _MODEL_VERSION
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        _MODEL_VERSION = out.stdout.strip() if (out.returncode == 0 and out.stdout.strip()) else "unknown"
    except (OSError, subprocess.SubprocessError):
        _MODEL_VERSION = "unknown"
    return _MODEL_VERSION

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

def om_learned_from(wd: dict) -> dict | None:
    """De geleerde Open-Meteo-modelbias uit het raamadviseur-artefact (`om_bias`, zie
    `om_bias.py`) — de enige plek waar hij geleerd én gepersisteerd wordt, zodat beide
    tweelingen, de batch-fit en de nachtvoorspelling dezelfde correctie gebruiken.

    Nog niets geleerd (verse deploy, te weinig geverifieerde punten) → None, en dan
    gedraagt `build_timeline` zich exact als vóór deze correctie."""
    ob = (wd or {}).get("om_bias") or {}
    if not ob.get("night") and not ob.get("day"):
        return None
    return ob

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

def physics_rev_migration_needed(learned: dict) -> bool:
    """Is er geleerde staat van een oudere fysica-revisie? (Een lege/nieuwe staat hoeft
    niets te migreren — de defaults zijn al de priors.)"""
    return bool(learned.get("params")) and learned.get("physics_rev") != PHYSICS_REV

def merged_params(house: dict, learned: dict) -> dict:
    """Geleerde params aangevuld met priors voor nieuwe kamers/keys (additief, robuust).
    Gedeeld door main() en night_forecast.py zodat de merge-logica niet dubbel bestaat."""
    params = learned.get("params") or default_params(house)
    # Fossiel uit de tijd dat `cd` leerbaar was: de code rekent met de vaste CD-constante en
    # niets leest params["cd"] — strip 'm hier zodat hij niet eeuwig in de artefacten meerijdt.
    params.pop("cd", None)
    base = default_params(house)
    # Fysica-revisie-migratie (zie PHYSICS_REV): geleerde staat van een oudere revisie → álles
    # terug naar de priors. Hier (en niet alleen in main) zodat óók night_forecast.py nooit
    # oude-fysica-params op de nieuwe fysica loslaat.
    if physics_rev_migration_needed(learned):
        params = base
    for g in GLOBAL_PARAMS:
        params.setdefault(g, base[g])
    for rid in house.get("rooms", {}):
        params.setdefault(rid, base[rid])
        for k in PER_ROOM_PARAMS:
            params[rid].setdefault(k, PRIORS[k])
            # Klem op de per-kamer versmalde band uit house_model.json (`param_bounds`).
            # Dit is de poort waar een huismodel-vloer daadwerkelijk landt: de fit klemt
            # zelf al, maar geleerde staat van vóór de vloer (of een seed uit
            # tools/vent_seed.py) komt hier binnen en moet meteen worden opgetild —
            # anders draait de eerste run nog op de oude, te lage waarde.
            params[rid][k] = clamp_model_bounds(house, rid, k, params[rid][k])
    return params

def load_openings_log() -> list[dict]:
    gist_id = os.getenv("GIST_ID")
    token = os.getenv("GIST_TOKEN")
    if not gist_id or not token:
        print("[openings] geen GIST_ID/GIST_TOKEN — lege log.")
        return []
    data = gist_read_json(gist_id, OPENINGS_FILE, token=token,
                          default={}, label="openings")
    return data.get("log", []) if isinstance(data, dict) else []

def fetch_weather(lat: float, lon: float) -> dict:
    """Open-Meteo: verleden (voor het leren) + forecast, met wind incl. richting en de
    zoncomponenten voor de instraling door het glas. Coördinaten expliciet
    (house_location) — geen module-globals."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ("temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,"
                   "wind_direction_10m,wind_gusts_10m,shortwave_radiation,"
                   "direct_radiation,diffuse_radiation"),
        "current": ("temperature_2m,relative_humidity_2m,wind_speed_10m,"
                    "wind_direction_10m,wind_gusts_10m,shortwave_radiation,direct_radiation"),
        "wind_speed_unit": "ms",
        "timezone": "Europe/Amsterdam",
        # Genoeg verleden voor het residu-venster (CALIB_WINDOW_H) plus de sim-only WARMUP_H
        # aanloop van de massaknoop, met marge.
        "past_days": 4, "forecast_days": 2,
    }
    data = get_json("https://api.open-meteo.com/v1/forecast", params,
                    timeout=25, label="open-meteo")
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

def wu_solar_scale_factor(k: float | None, age_h: float,
                          decay_h: float = WU_SOLAR_SCALE_DECAY_H) -> float:
    """Per-stap herschaalfactor voor de instraling: blend tussen de WU/OM-zon-ratio `k` (geldig op
    nu) en 1.0 (pure OM) naar sample-leeftijd. `age_h` = uren vóór nu; **negatief = vooruit**.
    `k`=None → 1.0 (WU ontbrak; no-op).

    De uitdoving is SYMMETRISCH (`abs(age_h)`): `k` is een momentopname van de verhouding tussen
    wat de eigen pyranometer meet en wat het grid modelleert, en die verhouding veroudert
    vooruit net zo hard als achteruit. Zolang de vooruitblik 2 uur was, was dit cosmetisch (hij
    voedde alleen de trendpijl); met een 12-uurs voorspelling zou de bewolkingsverhouding van dít
    moment anders de hele nacht en de ochtend erna blijven gelden. Voor het verleden verandert er
    niets."""
    if k is None:
        return 1.0
    w = 1.0 if decay_h <= 0 else 1.0 - abs(age_h) / decay_h
    w = min(1.0, max(0.0, w))
    return 1.0 + (k - 1.0) * w

def build_timeline(house: dict, weather: dict, log: list[dict], now: datetime,
                   window_h: float, ctx: RunContext, *,
                   wu_solar_scale: float | None = None,
                   beam_iam: bool = False, end_h: float = 2.0,
                   om_learned: dict | None = None) -> list[dict]:
    """Bouw een 15-minuten-raster van drivers over het kalibratievenster t/m nu,
    plus een vooruitblik van `end_h` uur (default de korte 2u voor de afgeleide-
    temp-projectie; night_forecast.py rekt hem op tot morgenochtend). Per stap:
    T_out, per-kamer instraling (door het glas), wind, en de gerapporteerde
    openingen-toestand op dat moment.

    `om_learned` = de geleerde Open-Meteo-modelbias (zie `om_bias.py`). Open-Meteo leest
    op onze locatie structureel te warm, 's nachts het sterkst (+1,4 °C tussen 22–07u),
    en dít is het enige punt waar de buitentemperatuur de fysica binnenkomt — voor béíde
    tweelingen, hun batch-fit én de nachtvoorspelling. Zonder correctie absorbeert de
    kalibratie die modelfout in de kamer-params (twin 2's tarrering maakte 'm zichtbaar:
    een systematische warm-bias per kamer die 's nachts het grootst is), waarna élke
    voorspelling ermee besmet is. None → geen correctie, exact het oude gedrag.

    De **vochtigheid schuift mee**: bij een temperatuurcorrectie blijft de absolute
    dampinhoud behouden, dus de RH bij de gecorrigeerde temperatuur is hoger
    (`convert_rh`, dezelfde Magnus-redenering als `vent_rh` in de raamadviseur). Alleen
    de temperatuur corrigeren zou de tweeling stiekem uitdrogen — en twin 2 laat de
    RH-residuen meewegen in zijn fit."""
    rows = [r for r in weather["hourly"] if r["T_out"] is not None]
    if not rows:
        return []
    start = now - _timedelta_h(window_h)
    grid = []
    t = start
    end = now + _timedelta_h(end_h)
    lat, lon = ctx.lat, ctx.lon
    while t <= end:
        T_out = _interp_hourly(rows, t, "T_out")
        wx = {k: _interp_hourly(rows, t, k) for k in
              ("wind_speed", "wind_dir", "gust", "precip", "direct", "diffuse", "rh")}
        if om_learned and T_out is not None:
            # Modelbias eraf; de RH volgt bij behoud van dampinhoud (zie docstring).
            T_corr = T_out + om_bias.correction_for(t, om_learned)
            wx["rh"] = convert_rh(wx["rh"], T_out, T_corr)
            T_out = T_corr
        st = openings_at(log, t)            # gerapporteerde toestand op dit moment (incl. zonwering)
        # Representatieve zonpositie op het stap-midden (voor de rij/dashboard; `irr` hieronder
        # is een tijdsgemiddelde over de stap, geen momentopname).
        sun_az, sun_el = sun_position(lat, lon, (t + _timedelta_h(0.125)).astimezone(timezone.utc))
        # Tijdsgemiddelde instraling over [t, t+0.25h] via de midden-regel op SOLAR_SUBSTEPS
        # subintervallen: dempt de geometrie-aliasing van de snel-draaiende lage avondzon.
        irr = {rid: 0.0 for rid in house.get("rooms", {})}
        # Horizontale dak-instraling (W/m², onbeschaduwd, opake conductie — de absorptie zit in
        # ROOF_SOLAR_GAIN, niet in een glas-transmissie) voor de bovenste-verdieping-kamers.
        roof_rooms = [rid for rid, r in house.get("rooms", {}).items() if r.get("roof_m2", 0.0) > 0.0]
        irr_roof = {rid: 0.0 for rid in roof_rooms}
        # WU/OM-herschaling (stap 1): sterkst rond nu, uitdovend naar 1.0 verder terug. None → 1.0.
        sc = wu_solar_scale_factor(wu_solar_scale, (now - t).total_seconds() / 3600.0)
        for j in range(SOLAR_SUBSTEPS):
            ts = t + _timedelta_h(0.25 * (j + 0.5) / SOLAR_SUBSTEPS)
            s_az, s_el = sun_position(lat, lon, ts.astimezone(timezone.utc))
            s_direct = _interp_hourly(rows, ts, "direct")
            s_diffuse = _interp_hourly(rows, ts, "diffuse")
            if sc != 1.0:
                s_direct = (s_direct or 0.0) * sc
                s_diffuse = (s_diffuse or 0.0) * sc
            pw = per_window_solar(house, st, s_az, s_el, s_direct, s_diffuse, beam_iam)
            tot_room = {rid: 0.0 for rid in irr}
            for wid, w in house.get("windows", {}).items():
                rid = w.get("room")
                if rid in tot_room:
                    tot_room[rid] += pw[wid]
            for rid in irr:
                irr[rid] += tot_room[rid] / SOLAR_SUBSTEPS
            if roof_rooms:
                # Plat dak (tilt 0) → azimut-onafhankelijk; één waarde voor alle dak-kamers.
                roof_i = facade_irradiance(0.0, s_az, s_el, s_direct, s_diffuse, 0.0)
                for rid in roof_rooms:
                    irr_roof[rid] += roof_i / SOLAR_SUBSTEPS
        grid.append({"t": t, "T_out": T_out, "irr": irr, "irr_roof": irr_roof, "states": st,
                     "weather": wx, "dt": 900.0, "sun_az": sun_az, "sun_el": sun_el})
        t = t + _timedelta_h(0.25)
    return grid

def _timedelta_h(h: float):
    return timedelta(hours=h)

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


# ── RunContext-opbouw ────────────────────────────────────────────────────────────────

def house_location(house: dict) -> tuple[float, float]:
    """Locatie uit het huismodel, met de gedeelde Utrecht-Oost-constante als fallback."""
    loc = house.get("location", {}) or {}
    return (float(loc.get("lat", shared_const.LATITUDE)),
            float(loc.get("lon", shared_const.LONGITUDE)))


def make_context(house: dict, weather: dict, now: datetime) -> RunContext:
    """Bouw de run-context: locatie uit het huismodel plus de twee trage ankers uit de
    weer-historie. Buur-anker met het zomerplafond erop (simulate legt daar per tijdstap
    de nachtcap overheen via neighbor_at); bodem-anker ~30-daags gedempt."""
    hourly = (weather or {}).get("hourly", [])
    lat, lon = house_location(house)
    return RunContext(
        lat=lat, lon=lon,
        neighbor_temp=min(vp.NEIGHBOR_SUMMER_CAP,
                          vp.neighbor_temp_estimate(hourly, now)),
        ground_temp=vp.ground_temp_estimate(hourly, now))


# ── WU-buiten-nu-verfijning ──────────────────────────────────────────────────────────

def refine_outside_now(weather: dict) -> tuple[float | None, float | None]:
    """Verfijn de buiten-nu-uitlezing met het eigen WU-station (zoals soil/window): de
    station-temp + RH zijn lokaler dan het Open-Meteo-grid. De temp krijgt de wu_bias-
    stralingscorrectie (driver = eigen pyranometer, Open-Meteo-zon als fallback).
    Bewust NIET van WU overgenomen: wind (het station meet die onbetrouwbaar) en de
    zon-instraling voor de glasfysica (die heeft de direct/diffuus-split nodig die WU
    niet levert; WU-zon dient enkel als bias-driver + glas-drive-herschaling). Alleen
    de "nu"-uitlezing wordt verfijnd — de historische timeline die de kalibratie voedt
    blijft Open-Meteo (WU levert geen uur-historie; de ground truth zijn de tado-temps).

    Muteert `weather` in place (current + wu_solar_scale) en geeft
    (wu_solar_scale, out_rh_temp) terug — de rauwe temp bij de gebruikte RH (één
    consistent Magnus-sensorpaar voor convert_rh)."""
    cur = weather.get("current", {}) or {}
    out_rh_temp = cur.get("temperature_2m")   # rauwe temp bij de gebruikte RH (één sensorpaar)
    wu_temp, wu_solar, wu_humid = fetch_wu_current_temp()
    wu_solar_scale = None   # WU/OM glas-drive-herschaling; None → pure Open-Meteo
    if wu_temp is not None:
        solar_now = wu_solar if wu_solar is not None else cur.get("shortwave_radiation")
        src = "wu" if wu_solar is not None else "om"
        cur["temperature_2m"] = round(correct_temp(wu_temp, solar_now), 1)
        cur["outside_source"] = "wu"
        out_rh_temp = wu_temp                 # rauwe WU-temp hoort bij de WU-RH (Magnus-paar)
        if wu_humid is not None:
            cur["relative_humidity_2m"] = wu_humid
        print(f"[buiten] WU: {wu_temp}°C → gecorrigeerd {cur['temperature_2m']}°C "
              f"(zon {solar_now} W/m², bron {src}); RH {wu_humid}%")
        # WU-gemeten-zon herschaling van de glas-drive rond nu: k = WU_global/OM_global.
        om_solar_now = cur.get("shortwave_radiation")
        if (wu_solar is not None and om_solar_now and om_solar_now >= WU_SOLAR_MIN_WM2
                and wu_solar >= WU_SOLAR_MIN_WM2):
            wu_solar_scale = min(WU_SOLAR_SCALE_MAX,
                                 max(WU_SOLAR_SCALE_MIN, wu_solar / om_solar_now))
            print(f"[zon] WU/OM glas-drive-herschaling k={wu_solar_scale:.2f} "
                  f"(WU {wu_solar}, OM {om_solar_now} W/m²)")
    else:
        cur["outside_source"] = "open-meteo"
        print("[buiten] WU niet beschikbaar → Open-Meteo buiten-nu.")
    weather["current"] = cur
    weather["wu_solar_scale"] = wu_solar_scale
    return wu_solar_scale, out_rh_temp


# ── Maand-shards (trainingsset; geërfd van tweeling 2) ──────────────────────────────

HISTORY_DIR     = os.getenv("VENT_HISTORY_DIR", "data/twin2_history")

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

def fetch_weather_archive(lat: float, lon: float, start: date, end: date) -> list[dict]:
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
                        "T_out": _get(h, "temperature_2m", i),
                        "rh": _get(h, "relative_humidity_2m", i),
                        "precip": _get(h, "precipitation", i) or 0.0,
                        "wind_speed": _get(h, "wind_speed_10m", i) or 0.0,
                        "wind_dir": _get(h, "wind_direction_10m", i) or 0.0,
                        "gust": _get(h, "wind_gusts_10m", i) or 0.0,
                        "shortwave": _get(h, "shortwave_radiation", i) or 0.0,
                        "direct": _get(h, "direct_radiation", i) or 0.0,
                        "diffuse": _get(h, "diffuse_radiation", i) or 0.0})
        return out

    base = {"latitude": lat, "longitude": lon, "hourly": hourly_vars,
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

def refresh_shard_weather(rows: list[dict], overwrite: bool = True) -> int:
    """Merge weer-rijen in hun maand-shards, gesleuteld op `dt`. Geeft #nieuwe uur-rijen.

    **Mergen, niet vervangen** — dit was een echte gat-bron: de oude versie zette
    `shard["weather"]` gelijk aan précies de aangeleverde rijen, dus een fetch met een
    smaller bereik (of een half-mislukte staart-fallback) wíste de rest van die maand.
    Gecombineerd met "alleen de wekelijkse batch schrijft weer" leverde dat een shard op
    waarvan het weer op 2026-07-16 stopte terwijl de kamerdata tot 07-28 liep — de hele
    hete twee weken viel buiten elke fit (gediagnosticeerd juli 2026). Mergen maakt het
    shard-weer monotoon groeiend en immuun voor een smaller venster of een door de
    `-X theirs`-merge van de kwartierloop geklobberde batch-commit.

    `overwrite=True` (batch/backfill): het ERA5-archief wint van een eerder ingevulde
    forecast-staart. `overwrite=False` (kwartierrun): alléén gaten vullen, zodat een
    archief-rij nooit terug-degradeert naar een forecast-waarde."""
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        by_month.setdefault(r["dt"].strftime("%Y-%m"), []).append(r)
    added = 0
    for month, mrows in by_month.items():
        shard = _load_shard(month)
        merged = {r["dt"]: r for r in shard.get("weather") or [] if r.get("dt")}
        for r in mrows:
            iso = r["dt"].isoformat()
            if iso in merged and not overwrite:
                continue
            added += iso not in merged
            merged[iso] = {**r, "dt": iso}
        shard["weather"] = [merged[k] for k in sorted(merged)]
        _write_shard(shard)
    return added

def append_shard_weather(weather: dict, now: datetime) -> int:
    """Vul het shard-weer bij uit de drivers die de kwartierrun tóch al ophaalt
    (`am.fetch_weather` levert `past_days=4` aan verleden). Geeft #nieuwe uur-rijen.

    Alléén verstreken uren: de forecast-staart is gemodelleerde toekomst en hoort niet
    als grondwaarheid in de trainingsset. Alléén gaten vullen (`overwrite=False`), dus
    waar de batch het ERA5-archief al heeft neergezet blijft dat leidend.

    Waarom de kwartierrun dit óók doet terwijl het weer "van de batch" is: met één
    wekelijkse schrijver is elk gemist of geklobberd batch-venster een blijvend gat in
    de trainingsset. Met een rollende 4-daagse overlap elke 15 minuten kan zo'n gat niet
    meer ontstaan, en het kost bytes per run — hetzelfde argument als voor de
    kamer-samples in `append_history_shard`."""
    rows = [r for r in (weather.get("hourly") or [])
            if r.get("dt") is not None and r["dt"] <= now and r.get("T_out") is not None]
    return refresh_shard_weather(rows, overwrite=False) if rows else 0

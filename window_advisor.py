#!/usr/bin/env python3
"""
window_advisor.py — Tado raam-koeladvies via Telegram (Project 6).

Doel: op warme zomerdagen het huis koelen door ramen te openen wanneer het
buiten kouder is dan in een kamer, en te sluiten zodra het buiten warmer wordt
(warmte-instroom). Staat er een warme dag aan te komen, dan begint dat koelen
al bij het comfortabele minimum (`low`) i.p.v. te wachten tot de kamer al te
warm is — zoveel mogelijk warmte "eruit tanken" vóórdat het heet wordt. De
roosters regelen de continue luchtkwaliteit, maar binnen een actieve
warme-dag-run heeft frisse lucht ook op zichzelf een kleine, thermisch-neutrale
voorkeur (open i.p.v. dicht) als er verder niets — koeling, oververhitting, noch
vocht — op het spel staat; op een koele/onderdrukte dag blijft dit puur thermisch.

Bronnen:
  - tado  → binnentemperatuur + luchtvochtigheid per zone (kamer)
  - Weather Underground PWS → echte buitentemperatuur nú
  - Open-Meteo hourly → vooruitblik (koele dag onderdrukken, "open weer rond HH:00")
  - Telegram (privé-chat: raam-advies + operationele alerts) → bezorging

Auth: tado gebruikt sinds maart 2025 de OAuth2 device-code flow. Refresh tokens
roteren (elke refresh herroept de vorige). De roterende token leeft in een
*secret* Gist (TADO_GIST_ID via GIST_TOKEN, bestand `tado_token.json`) en wordt
elke run meteen teruggeschreven. De eenmalige autorisatie doe je met
`tado_auth_bootstrap.py`.

Cadans: per-kamer toestandsmachine (zoals sandbox_notify). Eén check per kwartier,
maar alléén een bericht wanneer een kamer van advies wisselt (open ↔ dicht).

DRY_RUN=1 → print het bericht i.p.v. te versturen (token + state worden nog wél
weggeschreven; de token-rotatie mág niet overgeslagen worden).
"""

import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta

import gist_io
from http_util import get_json
from notify import run_guarded, sanitize_error, send_telegram

import requests

import shared_const
from shared_const import utc_now_iso
from wu_bias import correct_temp

# ── Kamers in scope (tado-zonenamen, hoofdletterongevoelig gematcht) ───────────
# ROOMS = de kamers waarvoor we koeladvies geven (raam open/dicht + Telegram).
ROOMS = ["Living room", "Ted", "hotties", "office"]
# SENSOR_ROOMS = álle tado-zones die we uitlezen en in window_data.json publiceren, óók de
# raamloze badkamer ("Shower") die er enkel als sensor bij zit: geen koeladvies (geen raam om
# te openen), maar wél gepubliceerd zodat de ventilatie-tweeling (Project 8) haar temp +
# verwarmingsstatus kan gebruiken. Advies/Telegram blijft strikt op ROOMS.
SENSOR_ROOMS = ROOMS + ["Shower"]

# ── Beslis-parameters (hysterese om flapping te voorkomen) ─────────────────────
COMFORT_HIGH = 23.5   # °C — standaard-comfortgrens (fallback voor kamers buiten ROOM_COMFORT)
OPEN_MARGIN  = 1.5    # °C — open als buiten ≥ deze marge kouder is dan binnen
CLOSE_MARGIN = 0.5    # °C — sluit als buiten tot binnen ± deze marge stijgt
WARM_DAY_MAX = 22.0   # °C — onder deze verwachte dag-max: geen koeladvies
LOOKAHEAD_H  = 12     # uur — vooruitblik voor "open weer rond HH:00"
SMOOTH_WINDOW_H = 0.75  # uur — mediaanvenster op de buitentemp vóór decide() (anti-flapping)
MIN_CLOSE_H  = 1.0    # uur — sluit een open raam niet voor een warmte-instroom die binnen
                      #       dit venster alweer voorbij is (kort momentje ≠ moeite waard)
SOLAR_AVG_WINDOW_MIN = 45  # min — middelingsvenster op de WU-pyranometer vóór de
                      #       stralingsbiascorrectie (zie fetch_wu_recent_solar)

# ── Per-kamer comfortband (low, high) in °C ────────────────────────────────────
# Bóven `high` is de kamer te warm → koelen (raam open als buiten kouder is). Ónder
# `low` is de kamer koel genoeg → sluiten om niet door te koelen. Daartussen een dode
# band: het advies blijft staan (geen flapping). Kamers die hier niet in staan vallen
# terug op (COMFORT_HIGH, COMFORT_HIGH) → het oude gedrag met één drempel.
ROOM_COMFORT = {
    "Living room": (19.5, 22.0),
    "Ted":         (17.0, 18.0),
    "hotties":     (16.0, 18.0),
    "office":      (20.0, 22.0),
}

# ── Vocht-comfort (ventilatie balanceert temperatuur én luchtvochtigheid) ─────────
# Op een vochtig huis met een hekel aan warmte wringt puur thermisch koelen op muffe
# zomerdagen: een raam open om te koelen haalt dan klamme lucht binnen. We projecteren
# met `vent_rh` (convert_rh) wat de kamer-RH wórdt als je ventileert, en laten dat de
# open-drempel verschuiven (muf = strenger, droog = soepeler), met een hard veto-plafond.
RH_COMFORT       = 60.0   # % — streef geprojecteerde kamer-RH na ventileren
RH_HARD_CAP      = 72.0   # % — daarboven nooit openen (te muf), forceert dicht
RH_TEMP_K        = 0.15   # °C per %RH afwijking van RH_COMFORT (de straf spant zo bijna de
                          #      hele 0–2°C op over de 60→72%-band, dus streef→veto)
RH_PENALTY_MAX   = 2.0    # °C — max muf-straf: open-drempel omhoog
RH_BONUS_MAX     = 0.5    # °C — max droog-bonus: open-drempel omlaag (asymmetrisch & klein,
                          #      zodat het gedrag op gewone droge dagen vrijwel onveranderd blijft)
RH_DRYOUT_MIN    = 65.0   # % — binnen-RH waarboven droge buitenlucht mag openen
RH_DRYOUT_MARGIN = 8.0    # % — buitenlucht moet ≥ deze marge droger (vent_rh ≤ RH − marge)


def comfort_band(room: str) -> tuple[float, float]:
    """(low, high) comfortgrenzen voor een kamer; fallback op de globale COMFORT_HIGH."""
    return ROOM_COMFORT.get(room, (COMFORT_HIGH, COMFORT_HIGH))


# ── Vocht: buiten-RH omrekenen naar kamertemperatuur (ventilatie/schimmel) ────────

def _es(temp_c: float) -> float:
    """Saturatiedampdruk (kPa) via Magnus/Tetens (FAO-56 Eq. 11)."""
    return 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))


def convert_rh(rh_out: float | None, t_out: float | None,
               t_in: float | None) -> float | None:
    """Zet de buiten-RH (%) bij buitentemp `t_out` om naar de RH (%) die diezelfde
    lucht zou hebben bij kamertemp `t_in` — de absolute dampdruk blijft behouden
    (alleen het saturatiepunt schuift met de temperatuur). Dit is de RH die de kamer
    benadert als je ventileert met buitenlucht: ligt 'm onder de huidige binnen-RH,
    dan droogt ventileren de kamer (minder schimmelrisico). `rh_out`/`t_out` moeten
    een consistent sensorpaar zijn (rauwe meting, niet de biascorrectie). None bij
    ontbrekende invoer."""
    if rh_out is None or t_out is None or t_in is None:
        return None
    e_actual = (rh_out / 100.0) * _es(t_out)   # werkelijke dampdruk buiten (kPa)
    rh_in = e_actual / _es(t_in) * 100.0
    return max(0.0, min(100.0, rh_in))

# ── Dashboard-voorspelling (heuristiek, geen thermisch huismodel) ──────────────
BIAS_DECAY_H    = 12    # uur — stationscorrectie dooft lineair uit over dit venster
TREND_WINDOW_H  = 2.0   # uur — historie-venster voor de binnentemp-trend (kort → volgt "nu")
GAP_BREAK_MIN   = 40    # min — een gat groter dan dit breekt het trend-venster: de fit gebruikt
                        # alleen de meest recente aaneengesloten reeks, zodat een gepauzeerde of
                        # herstarte loop (stale samples vóór het gat) de helling niet vervuilt/omdraait
TREND_MAX_SLOPE = 1.5   # °C/uur — clamp op de geschatte trend
RH_TREND_MAX    = 15.0  # %RH/uur — clamp op de vochttrend (richtingvector op het scatterplot)
TREND_CAP_H     = 4     # uur — trend wordt max. zoveel uur vooruit geprojecteerd, dan vlak
PREDICT_HORIZON_H = 18  # uur — hoe ver vooruit we naar een open-moment zoeken (rest van de dag)
PREDICT_STEP_MIN  = 15  # min — raster waarop open/dicht-momenten worden gevonden (interpoleert
                        # tussen de uurlijkse forecast-punten, zodat tijden niet op het hele uur plakken)
HISTORY_KEEP    = 192   # samples — rollend venster aan binnen/buiten-metingen (~2 dagen bij kwartiercadans)

# ── Locatie (Utrecht) ──────────────────────────────────────────────────────────
LATITUDE  = shared_const.LATITUDE
LONGITUDE = shared_const.LONGITUDE
TZ        = shared_const.TZ

# ── tado endpoints ──────────────────────────────────────────────────────────────
# Publieke device-flow client-id van de tado-app (algemeen bekend, geen geheim).
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
TADO_TOKEN_URL = "https://login.tado.com/oauth2/token"
TADO_API       = "https://my.tado.com/api/v2"

# ── Gist-opslag ──────────────────────────────────────────────────────────────────
TOKEN_FILE = "tado_token.json"     # {"refresh_token": "..."}
STATE_FILE = "window_state.json"   # {"rooms": {...}, "last_updated": ..., "last_notification": ...}

# ── Dashboard-artefact (publiek, gecommit door de Action; géén geheim) ───────────
DASHBOARD_FILE = os.path.join("docs", "window_data.json")


# ── Gist I/O ─────────────────────────────────────────────────────────────────────

def _gist_env():
    gist_id = os.environ["TADO_GIST_ID"]
    token   = os.environ["GIST_TOKEN"]
    return gist_id, token


def gist_read_file(filename: str) -> str | None:
    # Raist bewust bij netwerk-/HTTP-fouten: zonder token-file geen run.
    gist_id, token = _gist_env()
    return gist_io.read_file(gist_id, filename, token=token, timeout=20)


def gist_write_files(files: dict[str, str]) -> None:
    gist_id, token = _gist_env()
    payload = {"files": {fn: {"content": content} for fn, content in files.items()}}
    r = requests.patch(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        json=payload,
        timeout=20,
    )
    r.raise_for_status()


# ── tado auth (refresh-token flow, met rotatie-persistentie) ──────────────────────

# Retry-schema voor het persisteren van de geroteerde refresh token. tado herroept
# de oude token bij rotatie, dus één mislukte Gist-PATCH = keten gebroken =
# handmatige re-bootstrap. De PATCH is idempotent → agressief retryen mag.
TOKEN_PERSIST_DELAYS = (2, 4, 8, 16, 32)  # s — ~6 pogingen, ~62s worst case

# Retry-schema voor de tado-requests zelf (token-refresh + per-zone calls). tado's
# API heeft af en toe een kortstondige ReadTimeout/verbindingshikje (geobserveerd);
# zonder retry doet dat de hele kwartier-iteratie stranden vóórdat het dashboard
# ook maar geschreven is (main() faalt al vóór write_dashboard()). Kort en klein
# (twee pogingen na de eerste) zodat een échte, langdurige tado-storing niet een
# hele iteratie laat verzuipen in wachttijd — dat geval blijft gewoon een mislukte
# iteratie (de volgende kwartier-tick probeert opnieuw). Alleen transiënte
# netwerkfouten (timeout/verbinding) retryen; een HTTP-foutstatus (401 etc.) is
# geen netwerkhikje en moet meteen zichtbaar falen.
TADO_RETRY_DELAYS = (3, 8)  # s — ~3 pogingen, ~11s extra worst case per call


def _tado_request(method, url: str, **kwargs):
    """`method` (bv. `requests.get`/`requests.post`) met retry op transiënte
    timeouts/verbindingsfouten — zie TADO_RETRY_DELAYS hierboven."""
    last_err: Exception | None = None
    for attempt, delay in enumerate((0, *TADO_RETRY_DELAYS), start=1):
        if delay:
            time.sleep(delay)
        try:
            return method(url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            print(f"[tado] poging {attempt} mislukt ({sanitize_error(e)})"
                  + (", retry" if attempt <= len(TADO_RETRY_DELAYS) else ""))
    raise last_err


def _persist_rotated_token(new_refresh: str) -> None:
    """Schrijf de geroteerde refresh token naar de Gist, met retry/backoff.

    Bij definitief falen: Telegram-alert (de keten is dan mogelijk gebroken) en
    doorgaan — de access token van déze run is nog geldig, dus advies + dashboard
    kloppen nog. NOOIT de token zelf printen of doorsturen (publieke logs)."""
    last_err: Exception | None = None
    for attempt, delay in enumerate((0, *TOKEN_PERSIST_DELAYS), start=1):
        if delay:
            time.sleep(delay)
        try:
            gist_write_files({TOKEN_FILE: json.dumps({"refresh_token": new_refresh}, indent=2)})
            if attempt > 1:
                print(f"[tado] token-persist gelukt op poging {attempt}")
            return
        except Exception as e:
            last_err = e
            print(f"[tado] token-persist poging {attempt} mislukt: {sanitize_error(e)}")
    send_telegram(
        "⚠️ *tado window advisor*: kon de geroteerde refresh token niet opslaan — "
        "token-keten mogelijk gebroken. Re-run `tado_auth_bootstrap.py` als de "
        "volgende runs op 401 stuklopen.\n"
        f"Laatste fout: {sanitize_error(last_err)}",
        parse_mode="Markdown",
    )


def get_access_token() -> str:
    """Wissel de opgeslagen refresh token in voor een access token en schrijf de
    (geroteerde) refresh token meteen terug naar de Gist."""
    raw = gist_read_file(TOKEN_FILE)
    if not raw:
        print(
            f"[tado] Geen `{TOKEN_FILE}` in de Gist. Draai eerst eenmalig "
            "`python tado_auth_bootstrap.py` om te autoriseren.",
            file=sys.stderr,
        )
        sys.exit(1)
    refresh_token = json.loads(raw).get("refresh_token")
    if not refresh_token:
        print(f"[tado] `{TOKEN_FILE}` bevat geen refresh_token.", file=sys.stderr)
        sys.exit(1)

    r = _tado_request(
        requests.post,
        TADO_TOKEN_URL,
        data={
            "client_id":     TADO_CLIENT_ID,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    if r.status_code != 200:
        # Geen token loggen; wel statuscode zodat een verbroken keten zichtbaar is.
        print(
            f"[tado] Refresh mislukt (HTTP {r.status_code}). Mogelijk is de "
            "token-keten verbroken — draai `tado_auth_bootstrap.py` opnieuw.",
            file=sys.stderr,
        )
        sys.exit(1)
    tok = r.json()

    # Rotatie: bewaar de nieuwe refresh token onmiddellijk (de oude is herroepen).
    new_refresh = tok.get("refresh_token", refresh_token)
    _persist_rotated_token(new_refresh)

    return tok["access_token"]


def _tado_get(path: str, access_token: str) -> dict:
    r = _tado_request(
        requests.get,
        f"{TADO_API}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def parse_heating(state: dict) -> tuple[bool, float | None]:
    """Lees de verwarmingsstatus uit een tado zone-`/state`-respons: (aan?, vermogen%).

    Driver = het gemeten verwarmingsvermogen `activityDataPoints.heatingPower.percentage`
    (0..100) — dat is de directe "wordt er nú actief verwarmd"-uitlezing. `aan` = vermogen > 0,
    óf (bij een zone zonder heatingPower-datapunt) `setting.power == "ON"` mét een setpoint. Het
    model heeft geen verwarmingsterm, dus deze vlag laat de tweeling die kamer uit de kalibratie
    houden zolang er gestookt wordt. None-vermogen bij een zone die het datapunt niet levert."""
    adp = state.get("activityDataPoints", {}) or {}
    power_pct = (adp.get("heatingPower") or {}).get("percentage")
    if power_pct is not None:
        return power_pct > 0.0, power_pct
    # Geen gemeten vermogen → val terug op de aan/uit-stand van de thermostaat.
    setting = state.get("setting", {}) or {}
    on = str(setting.get("power", "")).upper() == "ON" and setting.get("temperature") is not None
    return on, None


def fetch_room_temps(access_token: str) -> dict[str, dict]:
    """Geeft per kamer in SENSOR_ROOMS: {"inside": °C, "humidity": %, "heating": bool,
    "heating_power": %|None} — de badkamer ("Shower") zit er als sensor-only kamer bij."""
    home_id = _tado_get("/me", access_token)["homes"][0]["id"]
    zones   = _tado_get(f"/homes/{home_id}/zones", access_token)

    wanted = {name.lower(): name for name in SENSOR_ROOMS}
    out: dict[str, dict] = {}
    for z in zones:
        canonical = wanted.get((z.get("name") or "").lower())
        if not canonical:
            continue
        state = _tado_get(f"/homes/{home_id}/zones/{z['id']}/state", access_token)
        sdp   = state.get("sensorDataPoints", {}) or {}
        inside = (sdp.get("insideTemperature") or {}).get("celsius")
        humid  = (sdp.get("humidity") or {}).get("percentage")
        heating, heat_pct = parse_heating(state)
        out[canonical] = {"inside": inside, "humidity": humid,
                          "heating": heating, "heating_power": heat_pct}

    missing = [r for r in SENSOR_ROOMS if r not in out]
    if missing:
        print(f"[tado] Niet gevonden als zone: {missing}", file=sys.stderr)
    return out


# ── Buitentemperatuur: WU nú, Open-Meteo als fallback + vooruitblik ───────────────

def fetch_wu_current_temp() -> tuple[float | None, float | None, float | None]:
    """Huidige buitentemperatuur (metric.temp) + instraling (solarRadiation,
    W/m², top-level) + relatieve luchtvochtigheid (humidity, %, top-level) van het
    eigen WU-station. De instraling drijft de stralingsbiascorrectie (zie wu_bias.py);
    de RH wordt (met de rauwe temp) gebruikt om de buiten-RH naar kamertemperatuur om
    te rekenen. Returnt (temp, solar, humidity); elk None als onbeschikbaar."""
    station_id = os.environ.get("WU_STATION_ID")
    api_key    = os.environ.get("WU_API_KEY")
    if not (station_id and api_key):
        return None, None, None
    url = (
        "https://api.weather.com/v2/pws/observations/current"
        f"?stationId={station_id}&format=json&units=m"
        f"&numericPrecision=decimal&apiKey={api_key}"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None, None, None
        obs_list = r.json().get("observations", [])
        if not obs_list:
            return None, None, None
        obs = obs_list[0]
        return ((obs.get("metric", {}) or {}).get("temp"),
                obs.get("solarRadiation"), obs.get("humidity"))
    except Exception as e:
        # sanitize_error: de WU-URL bevat apiKey — nooit rauw printen.
        print(f"[WU] current call failed: {sanitize_error(e)}")
        return None, None, None


def fetch_wu_recent_solar(now: datetime, window_min: float = SOLAR_AVG_WINDOW_MIN) -> float | None:
    """Mediaan van de WU-pyranometer over de laatste `window_min` minuten, via het
    raw (~5-min) `history/all`-endpoint voor vandaag.

    Instraling schommelt écht binnen seconden (passerende bewolking) — dat is geen
    sensorruis maar een fysiek feit. Het stralingskap-opwarmeffect dat de bias
    veroorzaakt heeft juist thermische traagheid over meerdere minuten, dus een
    enkel instant-sample (de `current`-call, `fetch_wu_current_temp`) importeerde
    zo'n voorbijgaande wolkenschaduw 1-op-1 in de stralingsbiascorrectie — de
    piekige "gecorrigeerde" buitentemp tussen 12–15u op dagen met wisselende
    bewolking (gediagnosticeerd juli 2026). Middelen over een venster benadert de
    kap-traagheid i.p.v. het instant-moment. None bij falen/geen recente data —
    de aanroeper valt dan terug op de instant-waarde."""
    station_id = os.environ.get("WU_STATION_ID")
    api_key    = os.environ.get("WU_API_KEY")
    if not (station_id and api_key):
        return None
    url = (
        "https://api.weather.com/v2/pws/history/all"
        f"?stationId={station_id}&format=json&units=m"
        f"&date={now.strftime('%Y%m%d')}&numericPrecision=decimal&apiKey={api_key}"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            # Diagnostisch: history/all vereist mogelijk een ander entitlement dan
            # de current/hourly/daily-endpoints die elders al werken — de status hier
            # onderscheidt dat van "geen data vandaag" (leeg maar 200).
            print(f"[WU] history/all status {r.status_code} — val terug op lokale mediaan")
            return None
        obs_list = r.json().get("observations", [])
    except Exception as e:
        # sanitize_error: de WU-URL bevat apiKey — nooit rauw printen.
        print(f"[WU] history/all call failed: {sanitize_error(e)}")
        return None
    cutoff = now - timedelta(minutes=window_min)
    vals = []
    for obs in obs_list:
        ts = obs.get("obsTimeUtc")
        solar = obs.get("solarRadiation")
        if not ts or solar is None:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if cutoff <= t <= now:
            vals.append(solar)
    return statistics.median(vals) if vals else None


def _parse_local(t: str) -> datetime:
    """Open-Meteo geeft met timezone=Europe/Amsterdam naïeve lokale tijden terug; maak
    ze tijdzone-bewust zodat ze veilig met `datetime.now(TZ)` te vergelijken zijn."""
    dt = datetime.fromisoformat(t)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt


def fetch_open_meteo() -> dict:
    """Huidige temp + uurlijkse forecast (vandaag + morgen).

    Drie pogingen met korte backoff: Open-Meteo's free tier heeft incidentele
    5xx-hiccups (geobserveerd: ~17% kans op een 502 binnen een ~5u-venster). Open-Meteo
    is een harde dependency — zonder forecast geen dashboard, geen warm-day-gate, geen
    open-tijd-voorspelling — dus één tikkie mag niet de hele iteratie laten sneuvelen.
    """
    params = {
        "latitude":     LATITUDE,
        "longitude":    LONGITUDE,
        "current":      "temperature_2m,shortwave_radiation,relative_humidity_2m",
        "hourly":       "temperature_2m",
        "timezone":     "Europe/Amsterdam",
        "forecast_days": 2,
    }
    data = get_json("https://api.open-meteo.com/v1/forecast", params,
                    timeout=20, label="open-meteo")
    cur = data.get("current") or {}
    current = cur.get("temperature_2m")
    h = data.get("hourly", {})
    rows = [
        {"dt": _parse_local(t), "temp": temp}
        for t, temp in zip(h.get("time", []), h.get("temperature_2m", []))
    ]
    # `current_solar` = fallback-driver voor de biascorrectie als de WU-pyranometer ontbreekt.
    # `current_humidity` = fallback voor de RH-omrekening als het WU-station geen RH geeft.
    return {"current": current, "current_solar": cur.get("shortwave_radiation"),
            "current_humidity": cur.get("relative_humidity_2m"), "hourly": rows}


def day_max_temp(hourly: list[dict], today) -> float | None:
    temps = [r["temp"] for r in hourly if r["dt"].date() == today and r["temp"] is not None]
    return max(temps) if temps else None


def upcoming_max_temp(hourly: list[dict], now: datetime, horizon_h: int = 24) -> float | None:
    """Hoogste verwachte temperatuur in de komende `horizon_h` uur. Vooruitkijkend (niet
    op kalenderdag) zodat 'staat er een warme dag aan?' ook diep in de nacht klopt — de
    warme piek kan dan later vandaag óf morgen liggen."""
    cutoff = horizon_h * 3600
    temps = [
        r["temp"] for r in hourly
        if r["temp"] is not None and 0.0 <= (r["dt"] - now).total_seconds() <= cutoff
    ]
    return max(temps) if temps else None


def next_reopen(hourly: list[dict], inside: float, now: datetime) -> datetime | None:
    """Eerste forecast-uur binnen LOOKAHEAD_H waarop buiten weer onder
    (binnen - OPEN_MARGIN) zakt → datetime, anders None."""
    for r in hourly:
        if r["dt"] <= now or r["temp"] is None:
            continue
        if (r["dt"] - now).total_seconds() > LOOKAHEAD_H * 3600:
            break
        if r["temp"] <= inside - OPEN_MARGIN:
            return r["dt"]
    return None


def reopen_hour(hourly: list[dict], inside: float, now: datetime) -> str | None:
    """Idem als next_reopen, maar als 'HH:MM'-string (voor de Telegram-hint)."""
    rt = next_reopen(hourly, inside, now)
    return rt.strftime("%H:%M") if rt else None


def reopen_is_brief(hourly: list[dict], inside: float | None, now: datetime) -> bool:
    """True als buiten binnen MIN_CLOSE_H alweer onder de open-drempel zakt — een kort,
    voorbijgaand warmte-instroommomentje dat een open raam sluiten niet de moeite waard
    maakt. Voorwaarde voor de 'laat-maar-openstaan'-onderdrukking in decide()."""
    if inside is None:
        return False
    rt = next_reopen(hourly, inside, now)
    if rt is None:
        return False
    return (rt - now).total_seconds() <= MIN_CLOSE_H * 3600


# ── Slimme combinatie: forecast geijkt op het eigen station + binnentrend ─────────

def correct_forecast(hourly: list[dict], bias: float, now: datetime) -> list[dict]:
    """Ijk de Open-Meteo forecast op het WU-station. `bias` = (WU_nu − model_nu) is de
    lokale microklimaat/kalibratie-offset; we tellen 'm bij de forecast op maar laten 'm
    lineair uitdoven over BIAS_DECAY_H (dichtbij = stationsanker, ver weg = ruw model).
    Geeft per uur {"dt", "out_raw", "out_corr"}."""
    out = []
    for r in hourly:
        raw = r["temp"]
        if raw is None:
            out.append({"dt": r["dt"], "out_raw": None, "out_corr": None})
            continue
        h_ahead = max(0.0, (r["dt"] - now).total_seconds() / 3600.0)
        decay = max(0.0, 1.0 - h_ahead / BIAS_DECAY_H)
        out.append({"dt": r["dt"], "out_raw": raw, "out_corr": raw + bias * decay})
    return out


def room_trend(history: list[dict], now: datetime,
               key: str = "temp", clamp: float = TREND_MAX_SLOPE) -> float | None:
    """Helling (per uur) van een serie over de laatste TREND_WINDOW_H uur, via
    kleinste-kwadraten over de samples. None bij te weinig historie. Geclamped op
    ±`clamp` zodat één rare meting de projectie niet laat ontsporen. `key` kiest de
    grootheid: "temp" (°C/uur, default) of "hum" (%RH/uur, gebruik clamp=RH_TREND_MAX)."""
    pts = []  # (uren_geleden_negatief, waarde)
    for s in history:
        try:
            t = datetime.fromisoformat(s["t"])
        except (ValueError, TypeError, KeyError):
            continue
        val = s.get(key)
        if val is None:
            continue
        dt_h = (t - now).total_seconds() / 3600.0  # ≤ 0 voor verleden
        if dt_h < -TREND_WINDOW_H or dt_h > 0.01:
            continue
        pts.append((dt_h, val))
    # Houd alleen de meest recente aaneengesloten reeks: loop vanaf het nieuwste sample
    # terug en stop zodra het gat naar het volgende oudere sample > GAP_BREAK_MIN is.
    # Zo vervuilt een gepauzeerde/herstarte loop (stale samples vóór het gat) de fit niet.
    if pts:
        pts.sort(key=lambda p: p[0])  # oplopend dt_h: oudste → nieuwste
        gap_h = GAP_BREAK_MIN / 60.0
        contiguous = [pts[-1]]
        for prev_dt, prev_val in reversed(pts[:-1]):
            if contiguous[-1][0] - prev_dt > gap_h:
                break
            contiguous.append((prev_dt, prev_val))
        pts = contiguous
    if len(pts) < 2:
        return None
    n = len(pts)
    sx = sum(x for x, _ in pts)
    sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts)
    sxy = sum(x * y for x, y in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    slope = (n * sxy - sx * sy) / denom
    return max(-clamp, min(clamp, slope))


def project_inside(inside_now: float, slope: float | None, hours_ahead: float) -> float:
    """Projecteer de binnentemperatuur vooruit. De huidige trend houdt hooguit
    TREND_CAP_H uur aan en vlakt dan af (heuristiek, geen thermisch model)."""
    if slope is None:
        return inside_now
    return inside_now + slope * min(hours_ahead, TREND_CAP_H)


def _interp_out_corr(pts: list[tuple[datetime, float]], t: datetime) -> float:
    """Lineair geïnterpoleerde geijkte buitentemp op tijdstip `t` uit de uurlijkse
    (dt, out_corr)-punten (oplopend gesorteerd). Buiten het bereik → dichtstbijzijnde
    eindpunt (geen extrapolatie)."""
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            span = (t1 - t0).total_seconds()
            if span <= 0:
                return v0
            f = (t - t0).total_seconds() / span
            return v0 + f * (v1 - v0)
    return pts[-1][1]


def predict_open_intervals(forecast_corr: list[dict], inside_now: float | None,
                            slope: float | None, now: datetime,
                            high: float, currently_open: bool = False) -> tuple[list[dict], list]:
    """Vind de momenten waarop raam-open zinvol is: geprojecteerde binnentemp > `high`
    (kamer-comfortgrens) én buiten-geijkt ≤ binnen − OPEN_MARGIN.

    De open/dicht-momenten worden gezocht op een fijn PREDICT_STEP_MIN-raster (met
    lineaire interpolatie van de geijkte buitentemp tussen de uurlijkse forecast-punten),
    zodat de tijden niet op het hele uur plakken maar op kwartieren vallen. Geeft
    (open_intervals, proj) terug; `proj` blijft de geprojecteerde binnentemp *per
    forecast-uur* (uitgelijnd op forecast_corr, voor de dashboard-grafiek), maar alleen
    binnen `TREND_CAP_H` — voorbij dat punt vlakt `project_inside()` toch af (de trend
    wordt niet oneindig doorgetrokken), en een urenlange horizontale staart tot het einde
    van `PREDICT_HORIZON_H` oogt als een voorspelling terwijl het puur de laatst bekende
    waarde herhaalt. De grafieklijn stopt dus na `TREND_CAP_H` in plaats van nutteloos
    plat door te lopen tot diep de volgende ochtend.

    `currently_open`: het raam staat ook nú al open (dode band, koelte tanken, ontvochtigen
    of frisse lucht kunnen dat laten gelden zónder dat de strikte `is_open`-drempel hierboven
    op dit moment geldt — die kent alleen de "cool"-conditie). Zonder deze correctie begon het
    eerste segment pas bij de eerstvolgende toekomstige drempeloverschrijding, en toonde de
    tijdlijn een gat vóór dat moment terwijl de kamer al open stond. `currently_open` forceert
    het allereerste (in-bereik) rasterpunt open, zodat het segment bij "nu" begint."""
    proj: list = []
    for r in forecast_corr:
        h_ahead = (r["dt"] - now).total_seconds() / 3600.0
        if inside_now is None or h_ahead < -0.5 or h_ahead > TREND_CAP_H:
            proj.append(None)
        else:
            proj.append(round(project_inside(inside_now, slope, max(0.0, h_ahead)), 1))

    # intervals: op fijn raster tussen de uurpunten.
    pts = [(r["dt"], r["out_corr"]) for r in forecast_corr if r["out_corr"] is not None]
    if inside_now is None or len(pts) < 2:
        return [], proj

    intervals: list[dict] = []
    cur_start: datetime | None = None
    prev_t: datetime | None = None

    def _close(end_dt: datetime) -> None:
        intervals.append({
            "start":   cur_start.strftime("%H:%M"),
            "end":     end_dt.strftime("%H:%M"),
            "start_h": round((cur_start - now).total_seconds() / 3600.0, 2),
            "end_h":   round((end_dt - now).total_seconds() / 3600.0, 2),
        })

    step = timedelta(minutes=PREDICT_STEP_MIN)
    t = pts[0][0]  # forecast-uur → raster valt op :00/:15/:30/:45
    force_open_now = currently_open
    is_open = False
    while t <= pts[-1][0]:
        h_ahead = (t - now).total_seconds() / 3600.0
        if h_ahead < -0.5 or h_ahead > PREDICT_HORIZON_H:
            if cur_start is not None and prev_t is not None:
                _close(prev_t)
                cur_start = None
                is_open = False
            t += step
            continue
        oc = _interp_out_corr(pts, t)
        ip = project_inside(inside_now, slope, max(0.0, h_ahead))
        # Open-trigger (nieuw opent) en blijf-open-trigger (warmte-instroom sluit) zijn
        # asymmetrisch, net als bij decide(): OPEN_MARGIN > CLOSE_MARGIN. Zonder dat
        # onderscheid sloot een geforceerd-open segment vrijwel meteen weer zodra de
        # binnentemp terugzakte in de dode band (open_trigger werd dan symmetrisch ook
        # als sluit-signaal gebruikt) — precies het geval waarin `currently_open` net
        # bedoeld is om het segment te láten staan.
        open_trigger = ip > high and oc <= ip - OPEN_MARGIN
        close_trigger = oc >= ip - CLOSE_MARGIN  # warmte-instroom
        if force_open_now:
            is_open = True  # kamer staat al open — laat het segment bij "nu" beginnen
            force_open_now = False
        elif is_open:
            is_open = not close_trigger
        else:
            is_open = open_trigger
        if is_open and cur_start is None:
            cur_start = t
        elif not is_open and cur_start is not None:
            _close(t)
            cur_start = None
        prev_t = t
        t += step
    if cur_start is not None and prev_t is not None:
        _close(prev_t)
    return intervals, proj


def open_status_tail(intervals: list[dict]) -> str:
    """' tot ~HH:MM[, weer open rond HH:MM]' uit de eerste twee voorspelde segmenten van
    `predict_open_intervals`, of '' zonder segmenten. Wordt gebruikt om elke "kamer is nu
    open"-statustekst (Nu open/Blijft open, met of zonder reden-suffix) dezelfde sluit-/
    heropentijd te laten tonen als de tijdlijn eronder — anders belooft de tekst "blijft
    open" terwijl dezelfde `intervals` een tussentijdse sluiting laten zien."""
    if not intervals:
        return ""
    end = intervals[0]["end"]
    reopen = intervals[1]["start"] if len(intervals) > 1 else None
    return f" tot ~{end}, weer open rond {reopen}" if reopen else f" tot ~{end}"


# ── Buitentemp gladstrijken vóór de beslissing (anti-flapping) ────────────────────

def smoothed_outside(history: list[dict], now: datetime,
                     current: float | None) -> float | None:
    """Mediaan van de buitentemp over de laatste SMOOTH_WINDOW_H uur, inclusief de
    huidige meting. Een mediaan negeert losse uitschieters — bv. een korte, hevige
    regenbui die de buitentemp door verdampingskoeling even scherp laat duiken en
    daarna weer herstelt — zodat één voorbijgaande meting het advies niet over de
    hysterese-band trekt en laat flippen (je wilt sowieso geen raam open tijdens een
    bui). `current` None → None; te weinig historie → gewoon de huidige meting."""
    if current is None:
        return None
    vals = [current]
    cutoff = SMOOTH_WINDOW_H * 3600
    for s in (history or []):
        try:
            t = datetime.fromisoformat(s["t"])
        except (ValueError, TypeError, KeyError):
            continue
        temp = s.get("temp")
        if temp is None:
            continue
        age = (now - t).total_seconds()
        if 0.0 <= age <= cutoff:
            vals.append(temp)
    vals.sort()
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def smoothed_solar(history: list[dict], now: datetime,
                   current: float | None,
                   window_min: float = SOLAR_AVG_WINDOW_MIN) -> float | None:
    """Mediaan van de eigen (per-run gepersisteerde) `solar`-samples in `history` over
    de laatste `window_min` minuten, inclusief de huidige meting — de lokale fallback
    voor `fetch_wu_recent_solar()` wanneer het `history/all`-endpoint faalt (bv. geen
    entitlement op dat WU-productniveau). Werkt met wat we al elke 15 min ophalen en
    bewaren, dus geen extra API-call nodig; bouwt vanzelf op na de deploy die `solar`
    additief aan `outside_history` toevoegt (oudere samples zonder dat veld tellen
    simpelweg niet mee). `current` None → None."""
    if current is None:
        return None
    vals = [current]
    cutoff = window_min * 60.0
    for s in (history or []):
        try:
            t = datetime.fromisoformat(s["t"])
        except (ValueError, TypeError, KeyError):
            continue
        solar = s.get("solar")
        if solar is None:
            continue
        age = (now - t).total_seconds()
        if 0.0 <= age <= cutoff:
            vals.append(solar)
    vals.sort()
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


# ── Beslislogica per kamer (met dode band voor hysterese) ─────────────────────────

def humidity_offset(vent_rh: float | None) -> float:
    """°C-correctie op de open-drempel op basis van de geprojecteerde kamer-RH na
    ventileren (`vent_rh`): + = muf (strenger openen), − = droog (soepeler openen),
    begrensd. Asymmetrisch geclamped — de muf-straf mag groter zijn dan de droog-bonus,
    zodat het gedrag op gewone droge dagen vrijwel onveranderd blijft."""
    if vent_rh is None:
        return 0.0
    raw = RH_TEMP_K * (vent_rh - RH_COMFORT)
    return max(-RH_BONUS_MAX, min(RH_PENALTY_MAX, raw))


def open_reason(inside: float | None, outside: float | None, low: float, high: float,
                vent_rh: float | None = None, humidity: float | None = None,
                bank_cooling: bool = False, fresh_air_ok: bool = False) -> str | None:
    """Wil de kamer nú open, vóór hysterese — en waaróm? Eén bron van waarheid voor
    zowel decide() als het dashboard-`open_now`/`open_reason`/Telegram-bericht, zodat
    advies, dashboard en bericht niet uit elkaar lopen. Retourneert de reden
    ("cool" | "bank" | "dryout" | "fresh_air") of None (geen open-wens)."""
    if inside is None or outside is None:
        return None
    if vent_rh is not None and vent_rh >= RH_HARD_CAP:
        return None  # veto: nooit ventileren naar te muffe lucht
    off = humidity_offset(vent_rh)
    if inside > high + off and outside <= inside - OPEN_MARGIN:
        return "cool"  # koelen — vocht schuift de open-drempel (muf hoger, droog lager)
    # Koelte tanken: staat er een warme dag aan te komen, dan wachten we niet tot de kamer
    # al te warm ís (`high`) — we beginnen al te koelen zodra ze op haar comfortabele
    # minimum zit (`low`) en buiten kouder is, om zoveel mogelijk warmte "eruit te halen"
    # vóórdat het heet wordt. Eenmaal open houdt de dode band/koelte-tank-logica in decide()
    # het raam ook onder `low` open, zolang buiten kouder blijft.
    if bank_cooling and inside > low + off and outside <= inside - OPEN_MARGIN:
        return "bank"
    # Ontvochtig-trigger: een muffe (niet per se warme) kamer mag open als de buitenlucht
    # duidelijk droger is en er geen warmte instroomt — drogen zonder het huis op te warmen.
    if (humidity is not None and vent_rh is not None
            and humidity >= RH_DRYOUT_MIN
            and vent_rh <= humidity - RH_DRYOUT_MARGIN
            and inside > low and outside <= inside):
        return "dryout"
    # Frisse-lucht-trigger: niets thermisch op het spel (buiten warmt niet op, kamer niet
    # onder haar comfortabele minimum) én de lucht is niet muf → dan heeft frisse lucht op
    # zichzelf waarde, ook al is er geen koel- of ontvochtig-noodzaak. Alleen binnen een
    # actieve (niet-onderdrukte) warme-dag-run — `fresh_air_ok` is de aanroeper's gate,
    # zodat een koele/onderdrukte dag hier niet alsnog open van wordt geadviseerd.
    if (fresh_air_ok and inside > low
            and vent_rh is not None and vent_rh <= RH_COMFORT
            and outside <= inside):
        return "fresh_air"
    return None


def open_desire(inside: float | None, outside: float | None, low: float, high: float,
                vent_rh: float | None = None, humidity: float | None = None,
                bank_cooling: bool = False, fresh_air_ok: bool = False) -> bool:
    """Bool-vorm van open_reason() — het echte beslispunt voor decide()."""
    return open_reason(inside, outside, low, high, vent_rh, humidity,
                       bank_cooling, fresh_air_ok) is not None


def decide(inside: float | None, outside: float | None, prev: str,
           low: float, high: float, bank_cooling: bool = False,
           vent_rh: float | None = None, humidity: float | None = None,
           reopen_soon: bool = False, fresh_air_ok: bool = False) -> str:
    if inside is None or outside is None:
        return prev  # geen meting → advies niet wijzigen
    if vent_rh is not None and vent_rh >= RH_HARD_CAP:
        return "dicht"  # te muf → dicht, ook een open raam (klam/schimmel weegt zwaarder)
    if open_desire(inside, outside, low, high, vent_rh, humidity, bank_cooling, fresh_air_ok):
        return "open"
    if outside >= inside - CLOSE_MARGIN:
        # Warmte-instroom: buiten heeft de kamer ingehaald → normaal sluiten. Maar staat
        # het raam nú open en zakt buiten binnen MIN_CLOSE_H alweer onder de open-drempel,
        # dan is even sluiten de moeite niet — laat het raam staan (geen geflapper voor
        # een kort warmte-momentje). Een muf-veto hierboven gaat hier wél vóór.
        if reopen_soon and prev == "open":
            return prev
        return "dicht"
    # Kamer is koel genoeg (≤ `low`, de onderkant van de comfortband). Normaal sluiten we
    # dan om niet door te koelen, maar staat er een warme dag aan te komen, dan blijven we
    # 's nachts koelte "tanken" zolang het buiten kouder blijft.
    if inside <= low and not bank_cooling:
        return "dicht"
    return prev  # dode band (low < binnen ≤ high) of koelte tanken → huidig advies vasthouden


# ── State I/O (in dezelfde secret Gist) ────────────────────────────────────────────

def load_state() -> dict:
    raw = gist_read_file(STATE_FILE)
    defaults = {"rooms": {}, "last_updated": None, "last_notification": None}
    if not raw:
        return defaults
    try:
        return {**defaults, **json.loads(raw)}
    except json.JSONDecodeError:
        return defaults


def save_state(state: dict) -> None:
    state["last_updated"] = datetime.now(TZ).isoformat()
    gist_write_files({STATE_FILE: json.dumps(state, indent=2, ensure_ascii=False)})


# ── Dashboard-artefact (docs/window_data.json) ─────────────────────────────────────

def read_prev_dashboard() -> dict:
    """Lees het vorige dashboard-bestand uit de checkout (voor historie-continuïteit)."""
    try:
        with open(DASHBOARD_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _append_trim(history: list, sample: dict) -> list:
    """Voeg een sample toe en houd de laatste HISTORY_KEEP over (chronologisch)."""
    hist = [h for h in (history or []) if h.get("temp") is not None]
    hist.append(sample)
    return hist[-HISTORY_KEEP:]


def write_dashboard(payload: dict) -> None:
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[dashboard] Geschreven → {DASHBOARD_FILE}")


def _room_dashboard_row(room: str, rooms_data: dict, prev_room_dash: dict,
                        now: datetime, ctx: dict) -> dict:
    """Bouw één kamer-rij voor het dashboard-artefact.

    `ctx` bundelt de run-brede context die elke kamer deelt: `od` (beslis-temp),
    `prev_rooms` (vorige adviezen), `warm_ahead`, `gated`, het buiten-RH-sensorpaar, en de
    forecast (`fc` + ruwe `hourly`)."""
    d = rooms_data.get(room) or {}
    inside   = d.get("inside")
    humidity = d.get("humidity")
    heating  = bool(d.get("heating"))
    od = ctx["od"]

    prev_hist = (prev_room_dash.get(room) or {}).get("history", [])
    hist = prev_hist
    if inside is not None:
        sample = {"t": now.isoformat(), "temp": round(inside, 1)}
        if humidity is not None:
            sample["hum"] = round(humidity)  # binnen-RH → vochttrend op het scatterplot
        # Verwarmingsvlag per sample (additief; afwezig = niet stoken). De ventilatie-tweeling
        # laat de samples waarin gestookt werd uit haar kalibratie vallen — het thermische model
        # heeft geen verwarmingsterm, dus een gestookte kamer leest warmer dan de fysica verklaart.
        if heating:
            sample["heat"] = 1
        hist = _append_trim(prev_hist, sample)

    low, high = comfort_band(room)
    slope  = room_trend(hist, now)
    hum_slope = room_trend(hist, now, "hum", RH_TREND_MAX)
    vent_rh = convert_rh(ctx["outside_rh"], ctx["outside_rh_temp"], inside)
    rh_off = humidity_offset(vent_rh)
    reopen_soon = reopen_is_brief(ctx["hourly"], inside, now)
    # Frisse lucht mag alleen meewegen binnen een actieve (niet-onderdrukte) warme-dag-run —
    # op een gated (koele) dag blijft dit puur thermisch, zoals de gate zelf al belooft.
    fresh_air_ok = not ctx["gated"]
    advice = decide(inside, od, ctx["prev_rooms"].get(room, "dicht"),
                    low, high, bank_cooling=ctx["warm_ahead"],
                    vent_rh=vent_rh, humidity=humidity, reopen_soon=reopen_soon,
                    fresh_air_ok=fresh_air_ok)
    # De vooruit-voorspeller verschuift mee met de huidige vochtstraf (per-uur RH-forecast
    # valt buiten scope → huidige offset als statische benadering) én met koelte-tanken
    # (drempel `low` i.p.v. `high` zodra warm_ahead) — dezelfde drempel als open_reason()
    # hieronder ziet, anders lopen de voorspelde tijden en de nu-status uit elkaar.
    open_threshold = (low if ctx["warm_ahead"] else high) + rh_off
    intervals, proj = predict_open_intervals(ctx["fc"], inside, slope, now, open_threshold,
                                             currently_open=(advice == "open"))

    reason = open_reason(inside, od, low, high, vent_rh, humidity,
                         bank_cooling=ctx["warm_ahead"], fresh_air_ok=fresh_air_ok)
    open_now = reason is not None
    predicted_open = intervals[0]["start"] if intervals else None

    rh_veto = vent_rh is not None and vent_rh >= RH_HARD_CAP
    dryout = reason == "dryout"

    # Zolang advice=="open" is currently_open=True hierboven meegegeven, dus intervals[0]
    # is altíjd het lopende open-segment (begint bij "nu") — elke "kamer is nu open"-tekst
    # hieronder moet dus dezelfde sluit-/heropentijd tonen als de tijdlijn eronder.
    tail = open_status_tail(intervals)

    if inside is None:
        status_text = "Geen meting"
    elif reason == "bank":
        status_text = f"Nu open{tail} — koelte tanken"
    elif reason == "fresh_air":
        status_text = f"Nu open{tail} — frisse lucht"
    elif open_now:
        status_text = f"Nu open{tail}"
    elif advice == "open":
        # Hysterese (dode band of koelte tanken) houdt het raam open, ook al zou een
        # verse herberekening nú niet meer actief openen — laat dat niet tegenspreken.
        status_text = f"Blijft open{tail}" if tail else "Blijft open"
    elif predicted_open:
        status_text = f"Open rond {predicted_open}"
    else:
        status_text = "Vandaag dicht houden"

    return {
        "inside":         round(inside, 1) if inside is not None else None,
        "humidity":       round(humidity) if humidity is not None else None,
        # Verwarmingsstatus nu (additief): vlag + gemeten vermogen%. Voedt de tweeling-uitsluiting
        # en kan op het dashboard getoond worden. Afwezig in oudere JSON → geen verwarming.
        "heating":        heating,
        "heating_power":  round(d.get("heating_power")) if d.get("heating_power") is not None else None,
        "vent_rh":        round(vent_rh) if vent_rh is not None else None,
        "rh_offset":      round(rh_off, 2),
        "rh_veto":        rh_veto,
        "dryout":         dryout,
        "open_reason":    reason,
        "advice":         advice,
        "comfort_low":    low,
        "comfort_high":   high,
        "trend":          round(slope, 2) if slope is not None else None,
        "hum_trend":      round(hum_slope, 2) if hum_slope is not None else None,
        "open_now":       open_now,
        "predicted_open": predicted_open,
        "open_intervals": intervals,
        "status_text":    status_text,
        "history":        hist,
        "proj":           proj,
    }


def build_dashboard(now: datetime, rooms_data: dict, om: dict, outside: float | None,
                    outside_source: str, dmax: float | None, prev_rooms: dict,
                    gated: bool, gate_reason: str, warm_ahead: bool = False,
                    outside_rh: float | None = None,
                    outside_rh_temp: float | None = None,
                    outside_decide: float | None = None,
                    wu_solar: float | None = None) -> dict:
    """Stel het publieke dashboard-artefact samen: stationsgeijkte forecast, per-kamer
    binnentrend + voorspelde open-momenten, en rollende historie voor de grafieken.

    `outside_rh`/`outside_rh_temp` zijn een consistent buiten-sensorpaar (rauwe temp +
    RH); per kamer rekenen we daaruit de RH om naar de kamertemperatuur (`vent_rh`).

    `outside`= de actuele (gecorrigeerde) buitenmeting — voor bias, historie en de
    `outside_now`-uitlezing. `outside_decide`= diezelfde temp ná het mediaanfilter
    (anti-flapping); daarmee beslissen we (`decide`/`open_now`). None → val terug op
    `outside`."""
    prev = read_prev_dashboard()
    prev_room_dash = prev.get("rooms", {})
    om_now = om.get("current")
    od = outside_decide if outside_decide is not None else outside  # beslis-temp (gladgestreken)

    # Stationscorrectie: alleen ijken als de WU-meting beschikbaar is.
    bias = 0.0
    if outside_source == "wu" and outside is not None and om_now is not None:
        bias = round(outside - om_now, 2)

    fc = correct_forecast(om["hourly"], bias, now)
    forecast = [
        {"dt": r["dt"].isoformat(),
         "out_raw":  round(r["out_raw"], 1)  if r["out_raw"]  is not None else None,
         "out_corr": round(r["out_corr"], 1) if r["out_corr"] is not None else None,
         "is_future": r["dt"] >= now}
        for r in fc
    ]

    out_hist = prev.get("outside_history", [])
    if outside is not None:
        # `temp` = de gebruikte buitentemp (WU-station, of Open-Meteo als fallback). We
        # bewaren óók de ruwe Open-Meteo-waarde van datzelfde uur (`om`), zodat het
        # dashboard station vs. model kan terugkijken en de divergentie zichtbaar maakt
        # (bijv. een zonnige avond waarop het station te warm meet).
        sample = {"t": now.isoformat(), "temp": round(outside, 1)}
        if om_now is not None:
            sample["om"] = round(om_now, 1)
        if outside_rh is not None:
            sample["hum"] = round(outside_rh)  # gemeten buiten-RH → vochttrend op het scatterplot
        if wu_solar is not None:
            # Rauwe WU-pyranometerstand (vóór de biascorrectie) — additief, voedt de
            # lokale mediaan-fallback (smoothed_solar) voor als history/all faalt.
            sample["solar"] = round(wu_solar, 1)
        out_hist = _append_trim(out_hist, sample)
    outside_slope = room_trend(out_hist, now)
    outside_hum_slope = room_trend(out_hist, now, "hum", RH_TREND_MAX)

    warm_day = dmax is not None and dmax >= WARM_DAY_MAX

    ctx = {
        "od": od, "prev_rooms": prev_rooms, "warm_ahead": warm_ahead, "gated": gated,
        "outside_rh": outside_rh, "outside_rh_temp": outside_rh_temp,
        "fc": fc, "hourly": om["hourly"],
    }
    rooms_out = {room: _room_dashboard_row(room, rooms_data, prev_room_dash, now, ctx)
                 for room in SENSOR_ROOMS}

    return {
        "generated_at":   utc_now_iso(),
        "as_of_local":    now.isoformat(),
        "source":         "window_advisor",
        "gated":          gated,
        "gate_reason":    gate_reason,
        "outside_now":    round(outside, 1) if outside is not None else None,
        "outside_smoothed": round(od, 1) if od is not None else None,
        "outside_source": outside_source,
        "outside_humidity": round(outside_rh) if outside_rh is not None else None,
        "om_now":         round(om_now, 1) if om_now is not None else None,
        "outside_trend":  round(outside_slope, 2) if outside_slope is not None else None,
        "outside_hum_trend": round(outside_hum_slope, 2) if outside_hum_slope is not None else None,
        "bias":           bias,
        "day_max":        round(dmax, 1) if dmax is not None else None,
        "warm_day":       warm_day,
        "warm_ahead":     warm_ahead,
        "params": {
            "COMFORT_HIGH": COMFORT_HIGH, "OPEN_MARGIN": OPEN_MARGIN,
            "CLOSE_MARGIN": CLOSE_MARGIN, "WARM_DAY_MAX": WARM_DAY_MAX,
            "LOOKAHEAD_H": LOOKAHEAD_H,
            "RH_COMFORT": RH_COMFORT, "RH_HARD_CAP": RH_HARD_CAP,
            "ROOM_COMFORT": {r: {"low": lo, "high": hi}
                             for r, (lo, hi) in ROOM_COMFORT.items()},
        },
        "outside_history": out_hist,
        "forecast":        forecast,
        "rooms":           rooms_out,
    }


# ── Entrypoint ──────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(TZ)
    print(f"[window_advisor] Start — {now.isoformat()}")

    access_token = get_access_token()
    rooms_data   = fetch_room_temps(access_token)

    om              = fetch_open_meteo()
    outside, wu_solar, wu_humid = fetch_wu_current_temp()
    # Consistent buiten-sensorpaar voor de RH→kamer-omrekening: rauwe temp + bijbehorende
    # RH (géén biascorrectie — RH en temp moeten van dezelfde meting komen). WU-paar bij
    # voorkeur; valt terug op het Open-Meteo-paar als het station geen RH levert.
    if outside is not None and wu_humid is not None:
        outside_rh, outside_rh_temp = wu_humid, outside
    else:
        outside_rh, outside_rh_temp = om.get("current_humidity"), om.get("current")
    # Vooraf ophalen (i.p.v. pas ná de correctie) zodat de lokale solar-mediaan hieronder
    # 'm ook kan gebruiken — dezelfde gepersisteerde historie als de anti-flapping-mediaan
    # verderop.
    prev_hist = read_prev_dashboard().get("outside_history", [])

    if outside is None:
        outside = om["current"]
        outside_source = "open-meteo"
        print(f"[buiten] WU niet beschikbaar → Open-Meteo: {outside}°C")
    else:
        outside_source = "wu"
        # Stralingsbiascorrectie (zie wu_bias.py): het WU-station leest in de zon
        # te warm. Driver bij voorkeur de mediaan van de eigen pyranometer over de
        # laatste SOLAR_AVG_WINDOW_MIN, via het `history/all`-endpoint
        # (fetch_wu_recent_solar) — een enkel instant-sample importeerde voorbijgaande
        # wolkenschaduw 1-op-1 in de correctie (piekige gecorrigeerde temp rond 12-15u
        # op wisselend bewolkte dagen). Dat endpoint bleek in de praktijk altijd te
        # falen (elke iteratie viel terug op de instant-waarde, gediagnosticeerd juli
        # 2026 — vermoedelijk een ander WU-productniveau dan current/hourly/daily),
        # dus een lokale mediaan (smoothed_solar) over onze eigen 15-minuten-historie
        # zit er tussenin: geen extra endpoint-afhankelijkheid, wel dezelfde demping.
        # Laatste redmiddel: de instant-WU-waarde, dan Open-Meteo. Dit schoont meteen
        # de microklimaat-bias-blend, decide() en outside_history op.
        raw = outside
        solar_recent = fetch_wu_recent_solar(now)
        solar_local  = smoothed_solar(prev_hist, now, wu_solar)
        if solar_recent is not None:
            solar_now, src = solar_recent, "wu_hist_api"
        elif solar_local is not None:
            solar_now, src = solar_local, "wu_hist_local"
        elif wu_solar is not None:
            solar_now, src = wu_solar, "wu_now"
        else:
            solar_now, src = om.get("current_solar"), "om"
        outside = round(correct_temp(outside, solar_now), 1)
        print(f"[buiten] WU: {raw}°C → gecorrigeerd {outside}°C "
              f"(zon {solar_now} W/m², bron {src})")

    # Anti-flapping: streek de actuele buitentemp glad met de mediaan over de laatste
    # SMOOTH_WINDOW_H aan metingen vóórdat we erop beslissen. Korte, hevige regenbuien
    # laten de buitentemp door verdampingskoeling soms >2°C duiken tussen kwartiermetingen
    # — meer dan de hysterese-band — om daarna weer te herstellen, wat het advies liet
    # flippen (en je wilt sowieso geen raam open in een bui). De mediaan negeert zo'n losse
    # dip. De ruwe `outside` blijft de uitlezing/historie/bias voeden; `outside_decide` is
    # wat decide() ziet. `prev_hist` is hierboven al opgehaald (ook gebruikt voor de
    # solar-mediaan).
    outside_decide = smoothed_outside(prev_hist, now, outside)
    if outside_decide is not None and outside is not None and abs(outside_decide - outside) >= 0.1:
        print(f"[buiten] beslis-temp (mediaan {SMOOTH_WINDOW_H}u): {outside_decide:.1f}°C")

    today  = now.date()
    dmax   = day_max_temp(om["hourly"], today)
    # Komt er een warme dag aan (komende 24u)? Zo ja, dan tanken we 's nachts koelte:
    # ramen blijven open ook als de kamer onder comfort zakt, zolang het buiten kouder is.
    upcoming_max = upcoming_max_temp(om["hourly"], now)
    warm_ahead   = upcoming_max is not None and upcoming_max >= WARM_DAY_MAX
    state  = load_state()
    prev_rooms = state.get("rooms", {})

    # Koeladvies is alleen zinvol op warme dagen. Onder de drempel én geen warme
    # kamer → niets doen (change-only voorkomt verder ruis).
    # Alleen de advies-kamers (ROOMS) mogen de gate opheffen — de sensor-only badkamer telt niet
    # mee (geen raam om te openen; een warme douche mag geen koeladvies-run forceren).
    warm_room = any(
        ((rooms_data.get(room) or {}).get("inside") is not None
         and rooms_data[room]["inside"] > comfort_band(room)[1])
        for room in ROOMS
    )
    gated = (dmax is None or dmax < WARM_DAY_MAX) and not warm_room
    gate_reason = "koele dag — advies onderdrukt" if gated else "warme dag"

    # Dashboard altijd verversen (ook op onderdrukte dagen, zodat de historie en de
    # "vandaag dicht houden"-status zichtbaar blijven). Telegram blijft ongewijzigd.
    write_dashboard(build_dashboard(
        now, rooms_data, om, outside, outside_source, dmax, prev_rooms, gated, gate_reason,
        warm_ahead, outside_rh=outside_rh, outside_rh_temp=outside_rh_temp,
        outside_decide=outside_decide, wu_solar=wu_solar))

    if gated:
        print(f"[gate] Koele dag (max {dmax}°C) en geen warme kamer → geen advies.")
        # State niet wijzigen; ramen blijven op hun laatste advies staan.
        return

    changes: list[tuple[str, str]] = []   # (room, new_advice)
    new_rooms = dict(prev_rooms)
    rh_reason: dict[str, str] = {}        # room → "veto" (dicht ondanks dat koelen zou kunnen)
    reason_by_room: dict[str, str] = {}   # room → "cool"|"bank"|"dryout"|"fresh_air" (waarom open)
    rh_vent: dict[str, float] = {}        # room → geprojecteerde vent_rh (voor het bericht)
    for room in ROOMS:
        d = rooms_data.get(room)
        inside = d["inside"] if d else None
        humidity = d.get("humidity") if d else None
        vent_rh = convert_rh(outside_rh, outside_rh_temp, inside)
        if vent_rh is not None:
            rh_vent[room] = vent_rh
        prev   = prev_rooms.get(room, "dicht")
        low, high = comfort_band(room)
        reopen_soon = reopen_is_brief(om["hourly"], inside, now)
        # fresh_air_ok=True: dit blok draait alleen binnen een niet-onderdrukte run (de gate
        # hierboven heeft koele dagen al afgevangen met een vroege return).
        new    = decide(inside, outside_decide, prev, low, high, bank_cooling=warm_ahead,
                        vent_rh=vent_rh, humidity=humidity, reopen_soon=reopen_soon,
                        fresh_air_ok=True)
        new_rooms[room] = new
        # Reden onthouden voor het bericht: veto (te muf → dicht), of wáárom een open-advies
        # opkwam (cool/bank/dryout/fresh_air) — zelfde open_reason() als decide() zag.
        if vent_rh is not None and vent_rh >= RH_HARD_CAP:
            rh_reason[room] = "veto"
        else:
            reason = open_reason(inside, outside_decide, low, high, vent_rh, humidity,
                                 bank_cooling=warm_ahead, fresh_air_ok=True)
            if reason is not None:
                reason_by_room[room] = reason
        ins = f"{inside:.1f}" if inside is not None else "?"
        out = f"{outside_decide:.1f}" if outside_decide is not None else "?"
        bank = " [koelte tanken]" if warm_ahead else ""
        rh_s = f" vent_rh {vent_rh:.0f}%" if vent_rh is not None else ""
        print(f"[kamer] {room}: binnen {ins}° buiten {out}°{rh_s} | {prev} → {new}{bank}")
        if new != prev:
            changes.append((room, new))

    state["rooms"] = new_rooms

    if not changes:
        print("[bericht] Geen wijzigingen → niets te versturen.")
        save_state(state)
        return

    lines = [f"🪟 *Raam-koeladvies* ({now.strftime('%H:%M')})"]
    for room, advice in changes:
        d   = rooms_data.get(room) or {}
        ins = d.get("inside")
        ins_s = f"{ins:.1f}" if ins is not None else "?"
        # Toon de gladgestreken beslis-temp: dat is de waarde waarop het advies stoelt.
        out_s = f"{outside_decide:.1f}" if outside_decide is not None else "?"
        if advice == "open":
            suffix_by_reason = {
                "dryout":    " — ontvochtigen (buiten droger)",
                "bank":      " — koelte tanken voor de warme dag",
                "fresh_air": " — frisse lucht",
            }
            extra = suffix_by_reason.get(reason_by_room.get(room), "")
            lines.append(f"🟢 Open: *{room}* (binnen {ins_s}°, buiten {out_s}°){extra}")
        elif rh_reason.get(room) == "veto":
            # Te muffe buitenlucht: dicht ondanks dat koelen thermisch zou kunnen.
            vr = rh_vent.get(room)
            vr_s = f" (RH ~{vr:.0f}%)" if vr is not None else ""
            lines.append(f"🔴 Sluit: *{room}* (binnen {ins_s}°) — buiten te muf{vr_s}")
        else:
            # De "buiten zakt rond HH weer onder binnen"-hint slaat alleen ergens op als
            # buiten nú boven de heropen-drempel zit (warmte-instroom). Zit het al onder
            # die drempel, dan is er niets om op te wachten en zou de hint misleiden.
            hint = None
            if (ins is not None and outside_decide is not None
                    and outside_decide > ins - OPEN_MARGIN):
                hint = reopen_hour(om["hourly"], ins, now)
            suffix = f" — buiten zakt rond {hint} weer onder binnen" if hint else ""
            lines.append(f"🔴 Sluit: *{room}* (binnen {ins_s}°, buiten {out_s}°){suffix}")
    message = "\n".join(lines)

    print(message)
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1, niet verzonden.")
    else:
        # Alles van dit project gaat naar de privé-chat (TELEGRAM_CHAT_ID, de
        # send_telegram default): zowel het raam-advies als de operationele
        # alerts (token-persist, run_guarded crash) — niets naar de groep.
        send_telegram(message, parse_mode="Markdown")
        state["last_notification"] = now.isoformat()
        print("Verzonden naar Telegram.")

    save_state(state)
    print("[window_advisor] Klaar")


if __name__ == "__main__":
    # fail_threshold=6: kwartierloop — pas alerten bij ~1,5 uur aanhoudende
    # storing. Een gemiste iteratie is onschuldig (de volgende haalt bij);
    # alleen een echte outage is een bericht waard.
    run_guarded(main, "window-advisor", fail_threshold=6)

#!/usr/bin/env python3
"""
window_advisor.py — Tado raam-koeladvies via Telegram (Project 6).

Doel: op warme zomerdagen het huis koelen door ramen te openen wanneer het
buiten kouder is dan in een kamer, en te sluiten zodra het buiten warmer wordt
(warmte-instroom). Ventilatie regelt de luchtkwaliteit al — dit is puur een
thermische beslissing, per kamer.

Bronnen:
  - tado  → binnentemperatuur + luchtvochtigheid per zone (kamer)
  - Weather Underground PWS → echte buitentemperatuur nú
  - Open-Meteo hourly → vooruitblik (koele dag onderdrukken, "open weer rond HH:00")
  - Telegram (weerbriefing-groep) → bezorging

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
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from notify import send_telegram

import requests

from wu_bias import correct_temp

# ── Kamers in scope (tado-zonenamen, hoofdletterongevoelig gematcht) ───────────
ROOMS = ["Living room", "Ted", "hotties", "office"]

# ── Beslis-parameters (hysterese om flapping te voorkomen) ─────────────────────
COMFORT_HIGH = 23.5   # °C — standaard-comfortgrens (fallback voor kamers buiten ROOM_COMFORT)
OPEN_MARGIN  = 1.5    # °C — open als buiten ≥ deze marge kouder is dan binnen
CLOSE_MARGIN = 0.5    # °C — sluit als buiten tot binnen ± deze marge stijgt
WARM_DAY_MAX = 22.0   # °C — onder deze verwachte dag-max: geen koeladvies
LOOKAHEAD_H  = 12     # uur — vooruitblik voor "open weer rond HH:00"
SMOOTH_WINDOW_H = 0.75  # uur — mediaanvenster op de buitentemp vóór decide() (anti-flapping)

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
TREND_WINDOW_H  = 4.0   # uur — historie-venster voor de binnentemp-trend
TREND_MAX_SLOPE = 1.5   # °C/uur — clamp op de geschatte trend
TREND_CAP_H     = 4     # uur — trend wordt max. zoveel uur vooruit geprojecteerd, dan vlak
PREDICT_HORIZON_H = 18  # uur — hoe ver vooruit we naar een open-moment zoeken (rest van de dag)
HISTORY_KEEP    = 192   # samples — rollend venster aan binnen/buiten-metingen (~2 dagen bij kwartiercadans)

# ── Locatie (Utrecht) ──────────────────────────────────────────────────────────
LATITUDE  = 52.0907
LONGITUDE = 5.1214
TZ        = ZoneInfo("Europe/Amsterdam")

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
    gist_id, token = _gist_env()
    r = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=20,
    )
    r.raise_for_status()
    files = r.json().get("files", {})
    if filename not in files:
        return None
    return files[filename].get("content")


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

    r = requests.post(
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
    gist_write_files({TOKEN_FILE: json.dumps({"refresh_token": new_refresh}, indent=2)})

    return tok["access_token"]


def _tado_get(path: str, access_token: str) -> dict:
    r = requests.get(
        f"{TADO_API}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def fetch_room_temps(access_token: str) -> dict[str, dict]:
    """Geeft per kamer in ROOMS: {"inside": °C, "humidity": %}."""
    home_id = _tado_get("/me", access_token)["homes"][0]["id"]
    zones   = _tado_get(f"/homes/{home_id}/zones", access_token)

    wanted = {name.lower(): name for name in ROOMS}
    out: dict[str, dict] = {}
    for z in zones:
        canonical = wanted.get((z.get("name") or "").lower())
        if not canonical:
            continue
        state = _tado_get(f"/homes/{home_id}/zones/{z['id']}/state", access_token)
        sdp   = state.get("sensorDataPoints", {}) or {}
        inside = (sdp.get("insideTemperature") or {}).get("celsius")
        humid  = (sdp.get("humidity") or {}).get("percentage")
        out[canonical] = {"inside": inside, "humidity": humid}

    missing = [r for r in ROOMS if r not in out]
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
        print(f"[WU] current call failed: {e}")
        return None, None, None


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
    data = None
    for attempt, delay in [(1, 0), (2, 3), (3, 8)]:
        if delay:
            time.sleep(delay)
        try:
            r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            break
        except requests.exceptions.RequestException as e:
            print(f"[open-meteo] poging {attempt}/3 mislukt: {e}")
            if attempt == 3:
                raise
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


def reopen_hour(hourly: list[dict], inside: float, now: datetime) -> str | None:
    """Eerste uur binnen LOOKAHEAD_H waarop buiten weer onder (binnen - OPEN_MARGIN)
    zakt → 'HH:00', anders None."""
    for r in hourly:
        if r["dt"] <= now or r["temp"] is None:
            continue
        if (r["dt"] - now).total_seconds() > LOOKAHEAD_H * 3600:
            break
        if r["temp"] <= inside - OPEN_MARGIN:
            return r["dt"].strftime("%H:%M")
    return None


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


def room_trend(history: list[dict], now: datetime) -> float | None:
    """Helling (°C/uur) van de binnentemperatuur over de laatste TREND_WINDOW_H uur,
    via kleinste-kwadraten over de samples. None bij te weinig historie. Geclamped op
    ±TREND_MAX_SLOPE zodat één rare meting de projectie niet laat ontsporen."""
    pts = []  # (uren_geleden_negatief, temp)
    for s in history:
        try:
            t = datetime.fromisoformat(s["t"])
        except (ValueError, TypeError, KeyError):
            continue
        temp = s.get("temp")
        if temp is None:
            continue
        dt_h = (t - now).total_seconds() / 3600.0  # ≤ 0 voor verleden
        if dt_h < -TREND_WINDOW_H or dt_h > 0.01:
            continue
        pts.append((dt_h, temp))
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
    return max(-TREND_MAX_SLOPE, min(TREND_MAX_SLOPE, slope))


def project_inside(inside_now: float, slope: float | None, hours_ahead: float) -> float:
    """Projecteer de binnentemperatuur vooruit. De huidige trend houdt hooguit
    TREND_CAP_H uur aan en vlakt dan af (heuristiek, geen thermisch model)."""
    if slope is None:
        return inside_now
    return inside_now + slope * min(hours_ahead, TREND_CAP_H)


def predict_open_intervals(forecast_corr: list[dict], inside_now: float | None,
                            slope: float | None, now: datetime,
                            high: float) -> tuple[list[dict], list]:
    """Loop de geijkte forecast af (tot PREDICT_HORIZON_H) en vind de uren waarop het
    raam-open zinvol is: geprojecteerde binnentemp > `high` (kamer-comfortgrens) én
    buiten-geijkt ≤ binnen − OPEN_MARGIN. Geeft (open_intervals, proj) waarbij proj de
    geprojecteerde binnentemp per forecast-uur is (uitgelijnd op forecast_corr)."""
    proj: list = []
    intervals: list[dict] = []
    cur_start: datetime | None = None
    prev_dt: datetime | None = None

    def _close(end_dt: datetime) -> None:
        intervals.append({
            "start":   cur_start.strftime("%H:%M"),
            "end":     end_dt.strftime("%H:%M"),
            "start_h": round((cur_start - now).total_seconds() / 3600.0, 2),
            "end_h":   round((end_dt - now).total_seconds() / 3600.0, 2),
        })

    for r in forecast_corr:
        dt, oc = r["dt"], r["out_corr"]
        h_ahead = (dt - now).total_seconds() / 3600.0
        if inside_now is None or h_ahead < -0.5 or h_ahead > PREDICT_HORIZON_H:
            proj.append(None)
            # buiten het zoekvenster een lopend interval afsluiten
            if cur_start is not None and prev_dt is not None:
                _close(prev_dt)
                cur_start = None
            continue
        ip = project_inside(inside_now, slope, max(0.0, h_ahead))
        proj.append(round(ip, 1))
        is_open = oc is not None and ip > high and oc <= ip - OPEN_MARGIN
        if is_open and cur_start is None:
            cur_start = dt
        elif not is_open and cur_start is not None:
            _close(dt)
            cur_start = None
        prev_dt = dt
    if cur_start is not None and prev_dt is not None:
        _close(prev_dt)
    return intervals, proj


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


def open_desire(inside: float | None, outside: float | None, low: float, high: float,
                vent_rh: float | None = None, humidity: float | None = None) -> bool:
    """Wil de kamer nú open (koelen óf ontvochtigen), vóór hysterese? Eén bron van
    waarheid voor zowel decide() als het dashboard-`open_now`, zodat advies en dashboard
    niet uit elkaar lopen."""
    if inside is None or outside is None:
        return False
    if vent_rh is not None and vent_rh >= RH_HARD_CAP:
        return False  # veto: nooit ventileren naar te muffe lucht
    if inside > high + humidity_offset(vent_rh) and outside <= inside - OPEN_MARGIN:
        return True  # koelen — vocht schuift de open-drempel (muf hoger, droog lager)
    # Ontvochtig-trigger: een muffe (niet per se warme) kamer mag open als de buitenlucht
    # duidelijk droger is en er geen warmte instroomt — drogen zonder het huis op te warmen.
    if (humidity is not None and vent_rh is not None
            and humidity >= RH_DRYOUT_MIN
            and vent_rh <= humidity - RH_DRYOUT_MARGIN
            and inside > low and outside <= inside):
        return True
    return False


def decide(inside: float | None, outside: float | None, prev: str,
           low: float, high: float, bank_cooling: bool = False,
           vent_rh: float | None = None, humidity: float | None = None) -> str:
    if inside is None or outside is None:
        return prev  # geen meting → advies niet wijzigen
    if vent_rh is not None and vent_rh >= RH_HARD_CAP:
        return "dicht"  # te muf → dicht, ook een open raam (klam/schimmel weegt zwaarder)
    if open_desire(inside, outside, low, high, vent_rh, humidity):
        return "open"
    if outside >= inside - CLOSE_MARGIN:
        return "dicht"  # warmte-instroom: buiten heeft de kamer ingehaald → sluiten
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


def build_dashboard(now: datetime, rooms_data: dict, om: dict, outside: float | None,
                    outside_source: str, dmax: float | None, prev_rooms: dict,
                    gated: bool, gate_reason: str, warm_ahead: bool = False,
                    outside_rh: float | None = None,
                    outside_rh_temp: float | None = None,
                    outside_decide: float | None = None) -> dict:
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
        out_hist = _append_trim(out_hist, sample)
    outside_slope = room_trend(out_hist, now)

    warm_day = dmax is not None and dmax >= WARM_DAY_MAX

    rooms_out: dict[str, dict] = {}
    for room in ROOMS:
        d = rooms_data.get(room) or {}
        inside   = d.get("inside")
        humidity = d.get("humidity")

        prev_hist = (prev_room_dash.get(room) or {}).get("history", [])
        hist = prev_hist
        if inside is not None:
            hist = _append_trim(prev_hist, {"t": now.isoformat(), "temp": round(inside, 1)})

        low, high = comfort_band(room)
        slope  = room_trend(hist, now)
        vent_rh = convert_rh(outside_rh, outside_rh_temp, inside)
        rh_off = humidity_offset(vent_rh)
        advice = decide(inside, od, prev_rooms.get(room, "dicht"),
                        low, high, bank_cooling=warm_ahead,
                        vent_rh=vent_rh, humidity=humidity)
        # De vooruit-voorspeller verschuift de open-drempel mee met de huidige vochtstraf
        # (per-uur RH-forecast valt buiten scope → huidige offset als statische benadering).
        intervals, proj = predict_open_intervals(fc, inside, slope, now, high + rh_off)

        open_now = open_desire(inside, od, low, high, vent_rh, humidity)
        predicted_open = intervals[0]["start"] if intervals else None

        rh_veto = vent_rh is not None and vent_rh >= RH_HARD_CAP
        dryout = bool(open_now and not (inside is not None and od is not None
                      and inside > high + rh_off and od <= inside - OPEN_MARGIN))

        if inside is None:
            status_text = "Geen meting"
        elif open_now:
            end = intervals[0]["end"] if intervals else None
            status_text = f"Nu open tot ~{end}" if end else "Nu open"
        elif predicted_open:
            status_text = f"Open rond {predicted_open}"
        else:
            status_text = "Vandaag dicht houden"

        rooms_out[room] = {
            "inside":         round(inside, 1) if inside is not None else None,
            "humidity":       round(humidity) if humidity is not None else None,
            "vent_rh":        round(vent_rh) if vent_rh is not None else None,
            "rh_offset":      round(rh_off, 2),
            "rh_veto":        rh_veto,
            "dryout":         dryout,
            "advice":         advice,
            "comfort_low":    low,
            "comfort_high":   high,
            "trend":          round(slope, 2) if slope is not None else None,
            "open_now":       open_now,
            "predicted_open": predicted_open,
            "open_intervals": intervals,
            "status_text":    status_text,
            "history":        hist,
            "proj":           proj,
        }

    return {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
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
    if outside is None:
        outside = om["current"]
        outside_source = "open-meteo"
        print(f"[buiten] WU niet beschikbaar → Open-Meteo: {outside}°C")
    else:
        outside_source = "wu"
        # Stralingsbiascorrectie (zie wu_bias.py): het WU-station leest in de zon
        # te warm. Driver = eigen pyranometer, met Open-Meteo als fallback. Dit
        # schoont meteen de microklimaat-bias-blend, decide() en outside_history op.
        raw = outside
        solar_now = wu_solar if wu_solar is not None else om.get("current_solar")
        src = "wu" if wu_solar is not None else "om"
        outside = round(correct_temp(outside, solar_now), 1)
        print(f"[buiten] WU: {raw}°C → gecorrigeerd {outside}°C "
              f"(zon {solar_now} W/m², bron {src})")

    # Anti-flapping: streek de actuele buitentemp glad met de mediaan over de laatste
    # SMOOTH_WINDOW_H aan metingen vóórdat we erop beslissen. Korte, hevige regenbuien
    # laten de buitentemp door verdampingskoeling soms >2°C duiken tussen kwartiermetingen
    # — meer dan de hysterese-band — om daarna weer te herstellen, wat het advies liet
    # flippen (en je wilt sowieso geen raam open in een bui). De mediaan negeert zo'n losse
    # dip. De ruwe `outside` blijft de uitlezing/historie/bias voeden; `outside_decide` is
    # wat decide() ziet.
    prev_hist = read_prev_dashboard().get("outside_history", [])
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
    warm_room = any(
        (d["inside"] is not None and d["inside"] > comfort_band(room)[1])
        for room, d in rooms_data.items()
    )
    gated = (dmax is None or dmax < WARM_DAY_MAX) and not warm_room
    gate_reason = "koele dag — advies onderdrukt" if gated else "warme dag"

    # Dashboard altijd verversen (ook op onderdrukte dagen, zodat de historie en de
    # "vandaag dicht houden"-status zichtbaar blijven). Telegram blijft ongewijzigd.
    write_dashboard(build_dashboard(
        now, rooms_data, om, outside, outside_source, dmax, prev_rooms, gated, gate_reason,
        warm_ahead, outside_rh=outside_rh, outside_rh_temp=outside_rh_temp,
        outside_decide=outside_decide))

    if gated:
        print(f"[gate] Koele dag (max {dmax}°C) en geen warme kamer → geen advies.")
        # State niet wijzigen; ramen blijven op hun laatste advies staan.
        return

    changes: list[tuple[str, str]] = []   # (room, new_advice)
    new_rooms = dict(prev_rooms)
    rh_reason: dict[str, str] = {}        # room → "veto" | "dryout" (waarom vocht meespeelt)
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
        new    = decide(inside, outside_decide, prev, low, high, bank_cooling=warm_ahead,
                        vent_rh=vent_rh, humidity=humidity)
        new_rooms[room] = new
        # Reden onthouden voor het bericht: veto (te muf → dicht) of ontvochtigen (open
        # zonder dat de kamer thermisch warm genoeg was).
        if vent_rh is not None and vent_rh >= RH_HARD_CAP:
            rh_reason[room] = "veto"
        elif new == "open" and not (inside is not None and outside_decide is not None
                and inside > high + humidity_offset(vent_rh)
                and outside_decide <= inside - OPEN_MARGIN):
            rh_reason[room] = "dryout"
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
            dry = " — ontvochtigen (buiten droger)" if rh_reason.get(room) == "dryout" else ""
            lines.append(f"🟢 Open: *{room}* (binnen {ins_s}°, buiten {out_s}°){dry}")
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
        send_telegram(message, chat_id=os.getenv("TELEGRAM_CHAT_GROUP_ID"),
                      parse_mode="Markdown")
        state["last_notification"] = now.isoformat()
        print("Verzonden naar Telegram.")

    save_state(state)
    print("[window_advisor] Klaar")


if __name__ == "__main__":
    main()

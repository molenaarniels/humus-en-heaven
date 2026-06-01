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

Cadans: per-kamer toestandsmachine (zoals sandbox_notify). Eén check per uur,
maar alléén een bericht wanneer een kamer van advies wisselt (open ↔ dicht).

DRY_RUN=1 → print het bericht i.p.v. te versturen (token + state worden nog wél
weggeschreven; de token-rotatie mág niet overgeslagen worden).
"""

import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ── Kamers in scope (tado-zonenamen, hoofdletterongevoelig gematcht) ───────────
ROOMS = ["Living room", "Ted", "hotties", "office"]

# ── Beslis-parameters (hysterese om flapping te voorkomen) ─────────────────────
COMFORT_HIGH = 23.5   # °C — standaard-comfortgrens (fallback voor kamers buiten ROOM_COMFORT)
OPEN_MARGIN  = 1.5    # °C — open als buiten ≥ deze marge kouder is dan binnen
CLOSE_MARGIN = 0.5    # °C — sluit als buiten tot binnen ± deze marge stijgt
WARM_DAY_MAX = 22.0   # °C — onder deze verwachte dag-max: geen koeladvies
LOOKAHEAD_H  = 12     # uur — vooruitblik voor "open weer rond HH:00"

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


def comfort_band(room: str) -> tuple[float, float]:
    """(low, high) comfortgrenzen voor een kamer; fallback op de globale COMFORT_HIGH."""
    return ROOM_COMFORT.get(room, (COMFORT_HIGH, COMFORT_HIGH))

# ── Dashboard-voorspelling (heuristiek, geen thermisch huismodel) ──────────────
BIAS_DECAY_H    = 12    # uur — stationscorrectie dooft lineair uit over dit venster
TREND_WINDOW_H  = 4.0   # uur — historie-venster voor de binnentemp-trend
TREND_MAX_SLOPE = 1.5   # °C/uur — clamp op de geschatte trend
TREND_CAP_H     = 4     # uur — trend wordt max. zoveel uur vooruit geprojecteerd, dan vlak
PREDICT_HORIZON_H = 18  # uur — hoe ver vooruit we naar een open-moment zoeken (rest van de dag)
HISTORY_KEEP    = 48    # samples — rollend venster aan binnen/buiten-metingen (~2 dagen uurlijks)

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

def fetch_wu_current_temp() -> float | None:
    """Huidige buitentemperatuur van het eigen WU-station (metric.temp)."""
    station_id = os.environ.get("WU_STATION_ID")
    api_key    = os.environ.get("WU_API_KEY")
    if not (station_id and api_key):
        return None
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
        return (obs_list[0].get("metric", {}) or {}).get("temp")
    except Exception as e:
        print(f"[WU] current call failed: {e}")
        return None


def _parse_local(t: str) -> datetime:
    """Open-Meteo geeft met timezone=Europe/Amsterdam naïeve lokale tijden terug; maak
    ze tijdzone-bewust zodat ze veilig met `datetime.now(TZ)` te vergelijken zijn."""
    dt = datetime.fromisoformat(t)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt


def fetch_open_meteo() -> dict:
    """Huidige temp + uurlijkse forecast (vandaag + morgen)."""
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":     LATITUDE,
            "longitude":    LONGITUDE,
            "current":      "temperature_2m",
            "hourly":       "temperature_2m",
            "timezone":     "Europe/Amsterdam",
            "forecast_days": 2,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    current = (data.get("current") or {}).get("temperature_2m")
    h = data.get("hourly", {})
    rows = [
        {"dt": _parse_local(t), "temp": temp}
        for t, temp in zip(h.get("time", []), h.get("temperature_2m", []))
    ]
    return {"current": current, "hourly": rows}


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


# ── Beslislogica per kamer (met dode band voor hysterese) ─────────────────────────

def decide(inside: float | None, outside: float | None, prev: str,
           low: float, high: float, bank_cooling: bool = False) -> str:
    if inside is None or outside is None:
        return prev  # geen meting → advies niet wijzigen
    if inside > high and outside <= inside - OPEN_MARGIN:
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
                    gated: bool, gate_reason: str, warm_ahead: bool = False) -> dict:
    """Stel het publieke dashboard-artefact samen: stationsgeijkte forecast, per-kamer
    binnentrend + voorspelde open-momenten, en rollende historie voor de grafieken."""
    prev = read_prev_dashboard()
    prev_room_dash = prev.get("rooms", {})
    om_now = om.get("current")

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
        out_hist = _append_trim(out_hist, {"t": now.isoformat(), "temp": round(outside, 1)})
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
        advice = decide(inside, outside, prev_rooms.get(room, "dicht"),
                        low, high, bank_cooling=warm_ahead)
        intervals, proj = predict_open_intervals(fc, inside, slope, now, high)

        open_now = (inside is not None and outside is not None
                    and inside > high and outside <= inside - OPEN_MARGIN)
        predicted_open = intervals[0]["start"] if intervals else None

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
        "outside_source": outside_source,
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
            "ROOM_COMFORT": {r: {"low": lo, "high": hi}
                             for r, (lo, hi) in ROOM_COMFORT.items()},
        },
        "outside_history": out_hist,
        "forecast":        forecast,
        "rooms":           rooms_out,
    }


# ── Telegram (weerbriefing-groep) ─────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    token   = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_GROUP_ID"]
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }, timeout=20)
    r.raise_for_status()


# ── Entrypoint ──────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(TZ)
    print(f"[window_advisor] Start — {now.isoformat()}")

    access_token = get_access_token()
    rooms_data   = fetch_room_temps(access_token)

    om       = fetch_open_meteo()
    outside  = fetch_wu_current_temp()
    if outside is None:
        outside = om["current"]
        outside_source = "open-meteo"
        print(f"[buiten] WU niet beschikbaar → Open-Meteo: {outside}°C")
    else:
        outside_source = "wu"
        print(f"[buiten] WU: {outside}°C")

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
        warm_ahead))

    if gated:
        print(f"[gate] Koele dag (max {dmax}°C) en geen warme kamer → geen advies.")
        # State niet wijzigen; ramen blijven op hun laatste advies staan.
        return

    changes: list[tuple[str, str]] = []   # (room, new_advice)
    new_rooms = dict(prev_rooms)
    for room in ROOMS:
        d = rooms_data.get(room)
        inside = d["inside"] if d else None
        prev   = prev_rooms.get(room, "dicht")
        low, high = comfort_band(room)
        new    = decide(inside, outside, prev, low, high, bank_cooling=warm_ahead)
        new_rooms[room] = new
        ins = f"{inside:.1f}" if inside is not None else "?"
        out = f"{outside:.1f}" if outside is not None else "?"
        bank = " [koelte tanken]" if warm_ahead else ""
        print(f"[kamer] {room}: binnen {ins}° buiten {out}° | {prev} → {new}{bank}")
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
        out_s = f"{outside:.1f}" if outside is not None else "?"
        if advice == "open":
            lines.append(f"🟢 Open: *{room}* (binnen {ins_s}°, buiten {out_s}°)")
        else:
            # De "buiten zakt rond HH weer onder binnen"-hint slaat alleen ergens op als
            # buiten nú boven de heropen-drempel zit (warmte-instroom). Zit het al onder
            # die drempel, dan is er niets om op te wachten en zou de hint misleiden.
            hint = None
            if (ins is not None and outside is not None
                    and outside > ins - OPEN_MARGIN):
                hint = reopen_hour(om["hourly"], ins, now)
            suffix = f" — buiten zakt rond {hint} weer onder binnen" if hint else ""
            lines.append(f"🔴 Sluit: *{room}* (binnen {ins_s}°, buiten {out_s}°){suffix}")
    message = "\n".join(lines)

    print(message)
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1, niet verzonden.")
    else:
        send_telegram(message)
        state["last_notification"] = now.isoformat()
        print("Verzonden naar Telegram.")

    save_state(state)
    print("[window_advisor] Klaar")


if __name__ == "__main__":
    main()

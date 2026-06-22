#!/usr/bin/env python3
"""
mowing_advisor.py — Grasmaai-adviseur via Telegram + dashboard.

Vijfde, onafhankelijke pijler naast het Humus & Heaven systeem. Vertelt wanneer
het gazon weer toe is aan een maaibeurt en welke maaihoogte (30/40/50 mm) je het
beste kunt instellen, gegeven het weer.

Werking:
  1. Lees de maai-log uit GitHub Gist (`mowings.json`, geschreven vanuit het
     dashboard). Vorm: {"YYYY-MM-DD": {"length_mm": 40}}.
  2. Lees de dagelijkse uitvoer van het bodemproject (`docs/data.json`) — alleen
     LEZEN. Daaruit komt per dag `lawn_T` (actuele transpiratie), die seizoen,
     kou, droogte en verdampingsvraag al via FAO-56 in zich draagt.
  3. Bouw een groei-accumulatie sinds de laatste maaibeurt (groei-eenheden =
     lawn_T × hitte-demping, want bij hitte transpireert gras wel maar groeit het
     nauwelijks).
  4. Bepaal of het gazon "maairijp" is, kies de beste droge dag in de
     voorspelling, en adviseer een maaihoogte.
  5. Stuur een Telegram-bericht (alleen als zinvol — geen spam) en schrijf
     `docs/mowing_data.json` voor het dashboard.

Onafhankelijkheid: dit script importeert NOOIT `soil_model` en schrijft NOOIT
`docs/data.json`. Het consumeert het gepubliceerde artefact read-only, net als
elke andere dashboard-lezer.

Env vars (GitHub Secrets / vars):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (optioneel — anders alleen printen)
  GIST_ID, GH_TOKEN | GITHUB_TOKEN       (maai-log; read-only)
  DASHBOARD_URL                          (optioneel; voor dashboard-link)
  DRY_RUN=1                              (print zonder te verzenden)
  SOIL_DATA_PATH, MOWING_DATA_PATH, MOWING_STATE_FILE  (override voor tests)
"""

import json
import os
from datetime import date, datetime, timedelta, timezone


import shared_const
from shared_const import parse_date, utc_now_iso
from gist_io import read_json as gist_read_json
from http_util import get_json
from notify import run_guarded, send_telegram

# =============================================================================
# Configuratie — alle tunables staan hier bovenaan.
# =============================================================================

# --- Groeimodel ---
HEAT_OPT_C = 24.0    # Tmax t/m deze waarde: geen hitte-demping op groei
HEAT_MAX_C = 35.0    # Tmax vanaf deze waarde: groei bijna stil (hittestress)
HEAT_FLOOR = 0.25    # restgroei-fractie bij/boven HEAT_MAX_C
GDD_BASE = 6.0       # basistemperatuur voor GDD-fallback (koel-seizoensgras)

# --- Maairijp-drempel ---
READY_GU = 11.0            # geaccumuleerde groei-eenheden = "tijd om te maaien"
LEAD_GU = 2.5              # zoveel GU vóór de drempel al een "bijna maairijp"-seintje
SELF_CALIBRATE = True      # leer de drempel uit het eigen maairitme
CALIBRATE_MIN_MOWS = 4     # minstens dit aantal intervallen voor we zelf-kalibreren
CALIBRATE_CLAMP = (8.0, 24.0)  # geleerde drempel blijft binnen deze band
DORMANT_GU_PER_DAY = 0.4   # 7-daags gemiddelde GU hieronder → winterrust, geen porren

# --- Beste-dag selectie over de voorspelling ---
DRY_PRECIP_MM = 1.0    # dag telt als "droog genoeg" onder deze neerslag
WET_PRECIP_MM = 3.0    # zware regen → nooit maaien
WET_PRECIP_PROB = 60   # hoge buienkans (%) bij lichte regen → toch overslaan
FROST_TMIN_C = 1.0     # Tmin hieronder: vorst, niet maaien
HEAT_TMAX_C = 30.0     # Tmax hierboven: te heet om te maaien (schroei/stress)
OVERGROWTH_FACTOR = 1.5  # accum >= READY_GU × dit → te lang, ⅓-regel-risico

# --- Maaihoogte-advies (30 / 40 / 50 mm) ---
LEN_TALL = 50
LEN_MID = 40
LEN_SHORT = 30
HOT_TMAX_C = 27.0           # "hete dag" in het maaihoogte-advies
HOT_DAYS_NEEDED = 2         # zoveel hete dagen in het venster → hoog maaien
DROUGHT_DEPLETION_PCT = 55.0  # lawn_depletion hierboven → hoog maaien (wortels)
COOL_TMAX_C = 22.0          # alle dagen koeler dan dit → kort mag
TIDY_DEPLETION_PCT = 35.0   # en vochtig genoeg → kort mag
GROWTH_MONTHS = range(4, 11)  # apr t/m okt: actief groeiseizoen
BOLT_MONTHS = range(5, 7)   # mei–juni: zaadpluim-seizoen (koel-seizoensgras / straatgras)
LENGTH_WINDOW_DAYS = 5      # vooruitkijk-venster voor het hoogte-advies

# --- Notificatie-cadans ---
RENUDGE_DAYS = 3  # nog steeds niet gemaaid → na zoveel dagen nog eens porren

# --- Koude start ---
COLD_START_ASSUMED_INTERVAL_DAYS = 10  # geen maaibeurt ooit gelogd → aanname

# --- Paden (overschrijfbaar voor tests) ---
SOIL_DATA_PATH = os.getenv("SOIL_DATA_PATH", "docs/data.json")
MOWING_DATA_PATH = os.getenv("MOWING_DATA_PATH", "docs/mowing_data.json")
STATE_FILE = os.getenv("MOWING_STATE_FILE", "mowing_state.json")
SOIL_MAX_AGE_H = 36.0

LATITUDE = shared_const.LATITUDE
LONGITUDE = shared_const.LONGITUDE

NL_DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
NL_MONTHS = ["", "jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


# =============================================================================
# Maai-log (GitHub Gist, read-only) — spiegelt load_irrigations_from_gist.
# =============================================================================

def load_mowings_from_gist() -> dict:
    """Haalt maai-log uit GitHub Gist. Vorm: {"YYYY-MM-DD": {"length_mm": int}}."""
    gist_id = os.getenv("GIST_ID")
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not gist_id:
        print("[mowings] geen GIST_ID, overslaan")
        return {}
    raw = gist_read_json(gist_id, "mowings.json", token=token,
                         default={}, label="mowings")

    out = {}
    for key, val in raw.items():
        if key.startswith("_"):  # _meta e.d. overslaan
            continue
        try:
            datetime.strptime(key, "%Y-%m-%d")
        except ValueError:
            continue
        # Tolerant: {"length_mm": 40} of een kale int.
        if isinstance(val, dict) and val.get("length_mm") is not None:
            out[key] = {"length_mm": int(val["length_mm"])}
        elif isinstance(val, (int, float)):
            out[key] = {"length_mm": int(val)}
    print(f"[mowings] {len(out)} maaibeurten geladen")
    return out


# =============================================================================
# Bodemdata lezen (read-only) + GDD-fallback.
# =============================================================================

def load_soil_days() -> tuple[list[dict], str] | None:
    """Lees docs/data.json read-only. Geeft (days, generated_at) of None terug.

    Geeft None als het bestand ontbreekt, onleesbaar is, of als `lawn_T` niet op
    alle dagen aanwezig is (dan valt main terug op de GDD-modus).
    """
    try:
        with open(SOIL_DATA_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[soil] kon {SOIL_DATA_PATH} niet lezen: {e}")
        return None
    days = data.get("days") or []
    if not days:
        return None
    if any(d.get("lawn_T") is None for d in days):
        print("[soil] lawn_T ontbreekt op één of meer dagen → fallback")
        return None
    return days, data.get("generated_at", "")


def is_stale(generated_at: str, max_age_h: float = SOIL_MAX_AGE_H) -> bool:
    if not generated_at:
        return True
    try:
        ts = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    return age_h > max_age_h


def fetch_gdd_fallback() -> list[dict]:
    """Lichtgewicht Open-Meteo dag-forecast voor de GDD-only fallback-modus.

    Spiegelt de vorm van sandbox_notify.fetch_forecast(), met Tmean erbij.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "daily": [
            "temperature_2m_max", "temperature_2m_min", "temperature_2m_mean",
            "precipitation_sum", "precipitation_probability_max",
        ],
        "past_days": 21,
        "forecast_days": 7,
        "timezone": "Europe/Amsterdam",
    }
    d = get_json(url, params, timeout=10, label="open-meteo")["daily"]
    today = date.today().isoformat()
    days = []
    for i, day in enumerate(d["time"]):
        days.append({
            "date": day,
            "Tmax": d["temperature_2m_max"][i],
            "Tmin": d["temperature_2m_min"][i],
            "Tmean": d["temperature_2m_mean"][i],
            "precip": d["precipitation_sum"][i] or 0.0,
            "precip_prob": d["precipitation_probability_max"][i] or 0,
            "forecast": day > today,
        })
    return days


# =============================================================================
# Groeimodel.
# =============================================================================

def heat_derate(tmax) -> float:
    if tmax is None:
        return 1.0
    if tmax <= HEAT_OPT_C:
        return 1.0
    if tmax >= HEAT_MAX_C:
        return HEAT_FLOOR
    frac = (tmax - HEAT_OPT_C) / (HEAT_MAX_C - HEAT_OPT_C)
    return 1.0 - frac * (1.0 - HEAT_FLOOR)


def daily_growth_unit(day: dict, source: str) -> float:
    """Groei-eenheid voor één dag."""
    if source == "soil":
        lt = day.get("lawn_T")
        if lt is None:
            return 0.0
        return max(0.0, lt) * heat_derate(day.get("Tmax"))
    # gdd_fallback
    tm = day.get("Tmean")
    if tm is None:
        tmax = day.get("Tmax") or 10.0
        tmin = day.get("Tmin") or 0.0
        tm = (tmax + tmin) / 2.0
    return max(0.0, tm - GDD_BASE)


def last_mow_date(mowings: dict, today: date) -> tuple[date, bool]:
    """Laatste gelogde maaidatum, of een aanname bij koude start.

    Geeft (datum, assumed) terug.
    """
    past = [parse_date(d)
            for d in mowings if parse_date(d) <= today]
    if past:
        return max(past), False
    return today - timedelta(days=COLD_START_ASSUMED_INTERVAL_DAYS), True


def build_growth_series(days: list[dict], mowings: dict, source: str,
                        reset_dates: set[str]) -> list[dict]:
    """Per dag: {date, gu, accum, forecast, is_mow, length_mm}.

    De accumulator wordt op elke reset-datum (echte of aangenomen maaibeurt) op 0
    gezet; de maaidag zelf draagt niets bij aan de volgende beurt.
    """
    series = []
    accum = 0.0
    for d in days:
        gu = daily_growth_unit(d, source)
        is_real_mow = d["date"] in mowings
        if d["date"] in reset_dates:
            accum = 0.0
        else:
            accum += gu
        series.append({
            "date": d["date"],
            "gu": round(gu, 3),
            "accum": round(accum, 2),
            "forecast": bool(d.get("forecast")),
            "is_mow": is_real_mow,
            "length_mm": mowings[d["date"]]["length_mm"] if is_real_mow else None,
        })
    return series


def effective_threshold(mowings: dict, days: list[dict], source: str) -> tuple[float, bool]:
    """Bepaal de maairijp-drempel; leer hem uit het eigen ritme indien mogelijk."""
    if not SELF_CALIBRATE:
        return READY_GU, False
    mow_dates = sorted(parse_date(d) for d in mowings)
    if len(mow_dates) < CALIBRATE_MIN_MOWS + 1:
        return READY_GU, False

    gu_by_date = {d["date"]: daily_growth_unit(d, source) for d in days}
    intervals = []
    for prev, cur in zip(mow_dates, mow_dates[1:]):
        total = 0.0
        ok = False
        step = prev + timedelta(days=1)
        while step <= cur:
            key = step.isoformat()
            if key in gu_by_date:
                total += gu_by_date[key]
                ok = True
            step += timedelta(days=1)
        if ok and total > 0:
            intervals.append(total)
    if len(intervals) < CALIBRATE_MIN_MOWS:
        return READY_GU, False

    intervals.sort()
    n = len(intervals)
    med = intervals[n // 2] if n % 2 else (intervals[n // 2 - 1] + intervals[n // 2]) / 2.0
    lo, hi = CALIBRATE_CLAMP
    learned = max(lo, min(hi, med))
    return round(learned, 2), True


def is_dormant(series: list[dict], today_idx: int) -> bool:
    start = max(0, today_idx - 6)
    window = series[start:today_idx + 1]
    if not window:
        return False
    mean_gu = sum(s["gu"] for s in window) / len(window)
    return mean_gu < DORMANT_GU_PER_DAY


# =============================================================================
# Beste dag + voorspelling + maaihoogte.
# =============================================================================

def _day_excluded(day: dict) -> bool:
    precip = day.get("precip") or 0.0
    prob = day.get("precip_prob") or 0
    tmin = day.get("Tmin")
    tmax = day.get("Tmax")
    if precip >= WET_PRECIP_MM:
        return True
    if precip >= DRY_PRECIP_MM and prob >= WET_PRECIP_PROB:
        return True
    if tmin is not None and tmin < FROST_TMIN_C:
        return True
    if tmax is not None and tmax > HEAT_TMAX_C:
        return True
    return False


def describe_day(day: dict) -> str:
    parts = []
    precip = day.get("precip") or 0.0
    parts.append("droog" if precip < DRY_PRECIP_MM else f"{precip:.0f}mm regen")
    if day.get("Tmax") is not None:
        parts.append(f"{day['Tmax']:.0f}°C")
    return ", ".join(parts)


def pick_optimal_day(days: list[dict], series: list[dict], today_idx: int,
                     threshold: float) -> dict | None:
    """Eerste geschikte (droge, niet te koude/hete) dag vanaf vandaag."""
    for j in range(today_idx, len(days)):
        if _day_excluded(days[j]):
            continue
        accum = series[j]["accum"]
        return {
            "date": days[j]["date"],
            "reason": describe_day(days[j]),
            "overgrown": accum >= threshold * OVERGROWTH_FACTOR,
            "is_today": j == today_idx,
        }
    return None


def predict_ready_date(series: list[dict], today_idx: int, threshold: float) -> str | None:
    for j in range(today_idx, len(series)):
        if series[j]["accum"] >= threshold:
            return series[j]["date"]
    return None


def recommend_length(days: list[dict], today_idx: int, today: date,
                     overgrown: bool = False,
                     last_length_mm: int | None = None) -> dict:
    """Adviseer maaihoogte 30/40/50 mm via een prioriteit-cascade (top-down).

    Prioriteit: (1) diepe wortels, (2) zaadpluimen voorkomen, (3) strak gazon.
    Een hogere prioriteit wint altijd van een lagere:
      P1a  hitte/droogte op komst → 50mm (hoge canopy koelt/beschaduwt de bodem,
           houdt de wortels diep en in leven).
      P1b  ⅓-regel / anti-scalp: een te lang gewoekerd gazon nooit korter dan de
           vorige beurt maaien — meer dan ⅓ van de spriet eraf remt de wortelgroei
           dagen-tot-weken (de grootste wortelkiller in huistuinen).
      P2   zaadpluim-seizoen (mei–juni) → 40mm: regelmatige gematigde beurten maaien
           de bloeistengels weg; te hoog laten staan laat ze de maaier ontsnappen.
      P3   strak gazon → 30mm, maar alléén als het koel, vochtig en groeizaam is en
           het de wortels niets kost (niet in zaadpluim-seizoen, niet overgroeid).
      default → 40mm (veilige, wortelvriendelijke middenstand).
    """
    window = days[today_idx:today_idx + LENGTH_WINDOW_DAYS]
    tmaxes = [d["Tmax"] for d in window if d.get("Tmax") is not None]
    depls = [d["lawn_depletion"] for d in window if d.get("lawn_depletion") is not None]
    hot_days = sum(1 for t in tmaxes if t >= HOT_TMAX_C)
    max_depl = max(depls) if depls else None

    # P1a — wortelbescherming onder hitte/droogte.
    if hot_days >= HOT_DAYS_NEEDED or (max_depl is not None and max_depl >= DROUGHT_DEPLETION_PCT):
        return {"length_mm": LEN_TALL,
                "reason": "warmte/droogte op komst, hoog maaien beschermt de wortels en de bodem"}

    # P1b — ⅓-regel: te lang gras niet scalperen.
    if overgrown and last_length_mm is not None:
        floor = max(LEN_MID, last_length_mm)
        return {"length_mm": floor,
                "reason": "gras staat lang — hou je aan de ⅓-regel (niet korter dan de vorige beurt) "
                          "zodat de wortels niet schrikken; maai desnoods in twee rondes"}

    # P2 — zaadpluim-seizoen: gematigd kort houden.
    if today.month in BOLT_MONTHS:
        return {"length_mm": LEN_MID,
                "reason": "zaadpluim-seizoen — een gematigde hoogte maait de bloeistengels weg vóór ze rijpen"}

    # P3 — strak gazon, alleen als het de wortels niets kost.
    if (tmaxes and all(t < COOL_TMAX_C for t in tmaxes)
            and (max_depl is None or max_depl < TIDY_DEPLETION_PCT)
            and today.month in GROWTH_MONTHS):
        return {"length_mm": LEN_SHORT,
                "reason": "koel en vochtig, het gras groeit rustig — kort mag voor een strak gazon"}

    return {"length_mm": LEN_MID,
            "reason": "gemengd weer — veilige middenstand voor gazon én wortels"}


# =============================================================================
# State I/O — minimale bookkeeping (spiegelt sandbox_state.json).
# =============================================================================

def load_state() -> dict:
    defaults = {"last_notified_date": None, "last_notified_kind": None,
                "last_seen_mow_date": None}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return {**defaults, **json.load(f)}
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def save_state(state: dict) -> None:
    state["last_updated"] = utc_now_iso()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print(f"[state] opgeslagen: {state}")


# =============================================================================
# Berichtopbouw.
# =============================================================================

def _format_date_nl(d: date) -> str:
    return f"{NL_DAYS[d.weekday()]} {d.day} {NL_MONTHS[d.month]}"


def _dashboard_link() -> str:
    dash = os.getenv("DASHBOARD_URL")
    if not dash:
        return ""
    if dash.endswith("index.html"):
        url = dash[:-len("index.html")] + "mowing.html"
    elif dash.endswith("/"):
        url = dash + "mowing.html"
    else:
        url = dash.rstrip("/") + "/mowing.html"
    return f'\n\n<a href="{url}">→ Open maai-dashboard</a>'


def build_message(kind: str, last_mow: date, today: date, today_day: dict,
                  optimal: dict | None, length: dict, source: str,
                  predicted: str | None = None) -> str:
    banner = ""
    if source == "gdd_fallback":
        banner = "⚠️ <i>Vereenvoudigd groeimodel — bodemdata tijdelijk niet beschikbaar.</i>\n\n"

    len_line = (f"📏 Zet de maaier op <b>{length['length_mm']}mm</b> — "
                f"{length['reason']}.")
    days_since = (today - last_mow).days

    if kind == "ready_today":
        body = (
            "✂️ <b>Tijd om te maaien!</b>\n"
            f"Het gras is sinds {_format_date_nl(last_mow)} flink gegroeid. "
            f"Vandaag {describe_day(today_day)} — prima maaidag.\n"
            f"{len_line}"
        )
    elif kind == "ready_wet":
        opt = optimal
        body = (
            "✂️ <b>Het gras is maairijp</b>, maar vandaag is geen goede maaidag "
            f"({describe_day(today_day)}).\n"
            f"🌤️ Beste dag: <b>{_format_date_nl(parse_date(opt['date']))}</b> "
            f"({opt['reason']}).\n"
            f"{len_line}"
        )
    elif kind == "soon":
        when = ""
        if optimal:
            when = (f"\n🌤️ Eerstvolgende goede maaidag: "
                    f"<b>{_format_date_nl(parse_date(optimal['date']))}</b> "
                    f"({optimal['reason']}).")
        around = ""
        if predicted:
            around = f" (rond {_format_date_nl(parse_date(predicted))})"
        body = (
            "🌱 <b>Het gras is bijna maairijp.</b>\n"
            f"Nog een paar groeidagen te gaan{around}. Plan alvast een droge "
            "maaidag, dan sta je niet ineens voor zaadpluimen."
            f"{when}\n"
            f"{len_line}"
        )
    elif kind == "overgrown":
        when = ""
        if optimal:
            when = (f"\n🌤️ Eerstvolgende goede dag: "
                    f"<b>{_format_date_nl(parse_date(optimal['date']))}</b> "
                    f"({optimal['reason']}).")
        body = (
            f"⚠️ <b>Gras staat lang</b> — al {days_since} dagen niet gemaaid.\n"
            "Maai niet alles in één keer (⅓-regel): zet de maaier hoog of maai in twee rondes."
            f"{when}\n"
            f"{len_line}"
        )
    else:
        return ""

    return banner + body + _dashboard_link()


# =============================================================================
# Hoofdlogica.
# =============================================================================

def find_today_index(days: list[dict], today: date) -> int:
    iso = today.isoformat()
    for i, d in enumerate(days):
        if d["date"] == iso:
            return i
    le = [i for i, d in enumerate(days) if d["date"] <= iso]
    return le[-1] if le else len(days) - 1


def run() -> dict:
    """Voert de hele analyse uit en geeft een resultaat-dict terug (voor main + tests)."""
    today = date.today()
    mowings = load_mowings_from_gist()

    soil = load_soil_days()
    if soil and not is_stale(soil[1]):
        days, generated_at = soil
        source = "soil"
    else:
        if soil:
            print("[soil] data verouderd → GDD-fallback")
        days = fetch_gdd_fallback()
        generated_at = ""
        source = "gdd_fallback"

    today_idx = find_today_index(days, today)
    last_mow, assumed = last_mow_date(mowings, today)

    reset_dates = set(mowings.keys())
    if assumed:
        reset_dates.add(last_mow.isoformat())

    series = build_growth_series(days, mowings, source, reset_dates)
    threshold, calibrated = effective_threshold(mowings, days, source)

    accum_today = series[today_idx]["accum"]
    dormant = is_dormant(series, today_idx)
    ready = accum_today >= threshold and not dormant
    # "Bijna maairijp": binnen LEAD_GU van de drempel, maar nog net niet rijp.
    # Geeft een dag of twee voorsprong om een droge maaidag te kiezen vóór er
    # zaadpluimen komen.
    almost = (not ready) and (not dormant) and accum_today >= threshold - LEAD_GU
    optimal = pick_optimal_day(days, series, today_idx, threshold)
    predicted = predict_ready_date(series, today_idx, threshold)
    overgrown = accum_today >= threshold * OVERGROWTH_FACTOR
    last_length_mm = mowings[max(mowings)]["length_mm"] if mowings else None
    length = recommend_length(days, today_idx, today, overgrown, last_length_mm)

    # --- Bepaal het soort bericht ---
    if dormant or assumed:
        # Winterrust of koude start (nog geen echte maaibeurt): geen Telegram.
        kind = "none"
    elif ready and overgrown:
        kind = "overgrown"
    elif ready and optimal and optimal["is_today"]:
        kind = "ready_today"
    elif ready and optimal:
        kind = "ready_wet"
    elif almost and optimal:
        kind = "soon"
    else:
        kind = "none"

    return {
        "today": today, "days": days, "series": series, "today_idx": today_idx,
        "source": source, "generated_at": generated_at, "mowings": mowings,
        "last_mow": last_mow, "assumed": assumed, "threshold": threshold,
        "calibrated": calibrated, "accum_today": accum_today, "dormant": dormant,
        "ready": ready, "almost": almost, "optimal": optimal, "predicted": predicted,
        "length": length, "kind": kind,
    }


def write_mowing_data_json(res: dict) -> None:
    today = res["today"]
    last_mow = res["last_mow"]
    last_length = None
    if res["mowings"]:
        last_key = max(res["mowings"])
        last_length = res["mowings"][last_key]["length_mm"]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "data.json" if res["source"] == "soil" else "gdd_fallback",
        "params": {
            "READY_GU": READY_GU,
            "READY_GU_effective": res["threshold"],
            "LEAD_GU": LEAD_GU,
            "self_calibrated": res["calibrated"],
            "HEAT_OPT_C": HEAT_OPT_C,
            "HEAT_MAX_C": HEAT_MAX_C,
            "OVERGROWTH_FACTOR": OVERGROWTH_FACTOR,
        },
        "mowings": res["mowings"],
        "last_mow": last_mow.isoformat(),
        "last_mow_assumed": res["assumed"],
        "last_length_mm": last_length,
        "today": today.isoformat(),
        "accum_today": round(res["accum_today"], 2),
        "series": res["series"],
        "ready": res["ready"],
        "almost": res["almost"],
        "dormant": res["dormant"],
        "predicted_next_mow": res["predicted"],
        "optimal_day": res["optimal"],
        "recommended_length": res["length"],
        "kind": res["kind"],
    }
    os.makedirs(os.path.dirname(MOWING_DATA_PATH) or ".", exist_ok=True)
    with open(MOWING_DATA_PATH, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"[data] {MOWING_DATA_PATH} geschreven ({os.path.getsize(MOWING_DATA_PATH)} bytes)")


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    res = run()
    today = res["today"]

    print(f"→ bron={res['source']} accum={res['accum_today']:.1f} "
          f"drempel={res['threshold']:.1f} (gekalibreerd={res['calibrated']}) "
          f"maairijp={res['ready']} winterrust={res['dormant']} kind={res['kind']}")
    print(f"   laatste maaibeurt: {res['last_mow']} (aanname={res['assumed']}) · "
          f"advies hoogte: {res['length']['length_mm']}mm · "
          f"volgende beurt ~ {res['predicted']}")

    # Bericht opbouwen
    msg = ""
    if res["kind"] != "none":
        msg = build_message(res["kind"], res["last_mow"], today,
                            res["days"][res["today_idx"]], res["optimal"],
                            res["length"], res["source"], res["predicted"])

    # Dashboard-data altijd wegschrijven
    write_mowing_data_json(res)

    # --- Suppressie / cadans ---
    state = load_state()
    cur_real_mow = max(res["mowings"]).__str__() if res["mowings"] else None
    if state.get("last_seen_mow_date") != cur_real_mow:
        # Nieuwe maaibeurt gelogd → notificatiegeheugen resetten
        state["last_notified_kind"] = None
        state["last_notified_date"] = None

    should_send = False
    if res["kind"] != "none":
        if state.get("last_notified_date") == today.isoformat():
            should_send = False  # max één bericht per dag
        elif res["kind"] != state.get("last_notified_kind"):
            should_send = True
        elif state.get("last_notified_date"):
            try:
                prev = parse_date(state["last_notified_date"])
                should_send = (today - prev).days >= RENUDGE_DAYS
            except ValueError:
                should_send = True
        else:
            should_send = True

    if msg:
        print("\n--- Telegram ---\n" + msg + "\n----------------")

    if should_send:
        if dry_run:
            print("DRY_RUN=1, niet verzonden.")
        else:
            if send_telegram(msg):
                state["last_notified_date"] = today.isoformat()
                state["last_notified_kind"] = res["kind"]
    else:
        print("→ geen notificatie (gesuppresseerd of niets te melden)")

    state["last_seen_mow_date"] = cur_real_mow
    if not dry_run:
        save_state(state)
    print("✓ klaar")


if __name__ == "__main__":
    # fail_threshold=2: de workflow doet bij falen één herkansing na 10 min
    # (zelfde job, dus zelfde RUNNER_TEMP-teller) — alleen een aanhoudende
    # storing alert.
    run_guarded(main, "Grasmaai-adviseur", fail_threshold=2)

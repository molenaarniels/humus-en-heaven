"""
Daily weather briefing via Telegram.

Fetches hourly forecast from Open-Meteo and sends a personalized
briefing covering specific time blocks plus daily UV exposure windows.

Weekdays Mon–Thu: commute (bike Tue/Thu only), school run, return home,
sport (Mon/Wed). Peuter days Fri–Sun: morning outing + post-nap window.

When the configured location is not "home" (>10km from Utrecht),
only UV info is reported.
"""

import os
import math
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from notify import run_guarded, send_telegram

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOCATION = {
    "lat": 52.0907,
    "lon": 5.1214,
    "label": "Utrecht",
    "timezone": "Europe/Amsterdam",
}

HOME_COORDS = (52.0907, 5.1214)
HOME_RADIUS_KM = 10.0

# Blocks for regular weekdays (Mon–Thu). weekdays: 0=Mon … 6=Sun
WEEKDAY_BLOCKS = [
    ("Fietstocht",     6,  0,  6, 30, {1, 3},          "🚲"),
    ("KDV brengen",    8,  0,  9,  0, {0, 1, 3},       "🧒"),
    ("Naar kantoor",   8,  0,  9,  0, {2},             "🏢"),
    ("Naar huis",     16, 30, 17, 30, {0, 1, 2, 3},    "🏠"),
    ("Sport (vrouw)", 19,  0, 20,  0, {0, 2},           "🏃"),
]

# Blocks for peuter days (Fri–Sun)
PEUTER_BLOCKS = [
    ("Na fruit",   9, 30, 11, 30, None, "🍓"),
    ("Na dutje",  15,  0, 17,  0, None, "💤"),
]

PEUTER_DAYS = {4, 5, 6}  # Friday, Saturday, Sunday

UV_MODERATE = 3.0
UV_HIGH     = 5.0

FROST_THRESHOLD_C = 0.0
HEAT_THRESHOLD_C  = 27.0

WIND_GUST_ALERT_MS = 14.0  # Beaufort 7 — krachtige wind
WIND_GUST_STORM_MS = 20.0  # Beaufort 9 — storm

CLOUD_TREND_DELTA_PCT = 25.0

POLLEN_LABELS_NL = {
    "alder_pollen":   "els",
    "birch_pollen":   "berk",
    "grass_pollen":   "gras",
    "mugwort_pollen": "bijvoet",
    "olive_pollen":   "olijf",
    "ragweed_pollen": "ambrosia",
}
POLLEN_API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def is_home(location):
    d = haversine_km(location["lat"], location["lon"], *HOME_COORDS)
    return d <= HOME_RADIUS_KM


def fetch_forecast(location):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":  location["lat"],
        "longitude": location["lon"],
        "hourly": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "precipitation",
            "precipitation_probability",
            "uv_index",
            "uv_index_clear_sky",
            "cloud_cover",
            "direct_radiation",
            "wind_speed_10m",
            "wind_gusts_10m",
        ]),
        "timezone":     location["timezone"],
        "forecast_days": 1,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def cloud_corrected_uv(uv_clear_sky, cloud_cover_pct):
    """Reduce clear-sky UV by a cloud modification factor.

    Josefsson & Landelius (2000): CMF = 1 - 0.75 * (cloud_fraction)^3.4.
    Open-Meteo's own `uv_index` under-corrects for cloud cover, so we
    derive the value ourselves from `uv_index_clear_sky` + `cloud_cover`.
    """
    if uv_clear_sky is None or cloud_cover_pct is None:
        return uv_clear_sky
    cc = max(0.0, min(100.0, cloud_cover_pct)) / 100.0
    cmf = 1.0 - 0.75 * (cc ** 3.4)
    return uv_clear_sky * cmf


def parse_hourly(forecast):
    h = forecast["hourly"]
    n = len(h["time"])
    uv_clear_series = h.get("uv_index_clear_sky", [None] * n)
    cloud_series    = h.get("cloud_cover",        [None] * n)
    gust_series     = h.get("wind_gusts_10m",     [None] * n)
    rows = []
    for i, t in enumerate(h["time"]):
        dt = datetime.fromisoformat(t)
        uv_clear = uv_clear_series[i]
        cloud    = cloud_series[i]
        uv = cloud_corrected_uv(uv_clear, cloud) if uv_clear is not None else h["uv_index"][i]
        rows.append({
            "dt": dt,
            "temp":       h["temperature_2m"][i],
            "feels":      h["apparent_temperature"][i],
            "precip":     h["precipitation"][i],
            "pop":        h["precipitation_probability"][i],
            "uv":         uv,
            "cloud":      cloud,
            "direct_rad": h["direct_radiation"][i],
            "wind_spd":   h["wind_speed_10m"][i],
            "wind_gust":  gust_series[i],
        })
    return rows


def weather_glyph(precip_mm, pop_pct, cloud_pct):
    """Pick a weather emoji from precip + probability + cloud cover."""
    if precip_mm >= 1.0 or pop_pct >= 60:
        return "🌧️"
    if precip_mm >= 0.2:
        return "🌦️"
    if cloud_pct is not None and cloud_pct >= 70:
        return "☁️"
    return "☀️"


def hours_in_window(rows, target_date, start_h, start_m, end_h, end_m):
    result = []
    for r in rows:
        if r["dt"].date() != target_date:
            continue
        bucket_start_min = r["dt"].hour * 60
        bucket_end_min   = bucket_start_min + 60
        win_start_min    = start_h * 60 + start_m
        win_end_min      = end_h * 60 + end_m
        if bucket_start_min < win_end_min and bucket_end_min > win_start_min:
            result.append(r)
    return result


def summarize_block(label, hours):
    if not hours:
        return label + ": geen data"

    temps  = [h["temp"]   for h in hours]
    feels  = [h["feels"]  for h in hours]
    precip = sum(h["precip"] for h in hours)
    pop    = max(h["pop"]    for h in hours)
    clouds = [h["cloud"] for h in hours if h.get("cloud") is not None]
    cloud_avg = sum(clouds) / len(clouds) if clouds else None
    glyph  = weather_glyph(precip, pop, cloud_avg)

    def fmt_range(vals, unit="C"):
        lo, hi = min(vals), max(vals)
        if abs(hi - lo) < 0.5:
            return str(round(lo)) + unit
        return str(round(lo)) + "-" + str(round(hi)) + unit

    if precip < 0.05 and pop >= 30:
        precip_str = "miezer"
    else:
        precip_str = ("%.1f" % precip) + " mm"

    direct_rads = [h["direct_rad"] for h in hours if h.get("direct_rad") is not None]
    wind_spds   = [h["wind_spd"]   for h in hours if h.get("wind_spd")   is not None]
    if direct_rads:
        mean_dir  = sum(direct_rads) / len(direct_rads)
        mean_wind = sum(wind_spds) / len(wind_spds) if wind_spds else 0.0
        solar_bonus = round(mean_dir * 0.012 * max(0.0, 1 - mean_wind / 10))
    else:
        solar_bonus = 0

    if solar_bonus >= 3:
        feels_str = "gevoel " + fmt_range(feels) + ", +" + str(solar_bonus) + "° in zon"
    else:
        feels_str = "gevoel " + fmt_range(feels)

    gusts = [h["wind_gust"] for h in hours if h.get("wind_gust") is not None]
    max_gust = max(gusts) if gusts else 0.0
    if max_gust >= WIND_GUST_STORM_MS:
        wind_line = "\n   🌪️ Storm: windvlagen tot " + str(round(max_gust)) + " m/s"
    elif max_gust >= WIND_GUST_ALERT_MS:
        wind_line = "\n   💨 Wind: vlagen tot " + str(round(max_gust)) + " m/s"
    else:
        wind_line = ""

    return (
        label + " " + glyph + "\n"
        "   Temp " + fmt_range(temps) + " (" + feels_str + ")"
        "  Regen " + str(round(pop)) + "% / " + precip_str
        + wind_line
    )

def uv_windows(rows, target_date, threshold):
    """
    Find ranges where UV >= threshold on target_date, with sub-hour
    precision via linear interpolation between hourly values.

    Open-Meteo's uv_index is the instantaneous value AT the hour stamp,
    so we interpolate between consecutive hours to find the exact
    threshold crossing time. Returns list of (start_str, end_str) tuples
    rounded to nearest 30 minutes.
    """
    day_rows = [r for r in rows if r["dt"].date() == target_date and r["uv"] is not None]
    day_rows.sort(key=lambda r: r["dt"])
    if not day_rows:
        return []

    def crossing_minute(t1, v1, t2, v2, thr):
        if v2 == v1:
            return t1 * 60
        frac = (thr - v1) / (v2 - v1)
        frac = max(0.0, min(1.0, frac))
        return round((t1 + frac * (t2 - t1)) * 60)

    def fmt(minute_of_day):
        h, m = divmod(minute_of_day, 60)
        m_rounded = 30 * round(m / 30)
        if m_rounded == 60:
            h += 1
            m_rounded = 0
        return f"{h:02d}:{m_rounded:02d}"

    raw = []  # collect raw start/end minutes first
    cur_start_min = None
    for i in range(len(day_rows)):
        r = day_rows[i]
        v = r["uv"]
        h = r["dt"].hour
        prev = day_rows[i-1] if i > 0 else None
        if v >= threshold and cur_start_min is None:
            if prev is not None and prev["uv"] < threshold:
                cur_start_min = crossing_minute(prev["dt"].hour, prev["uv"], h, v, threshold)
            else:
                cur_start_min = h * 60
        elif v < threshold and cur_start_min is not None:
            end_min = crossing_minute(prev["dt"].hour, prev["uv"], h, v, threshold)
            raw.append((cur_start_min, end_min))
            cur_start_min = None
    if cur_start_min is not None:
        raw.append((cur_start_min, day_rows[-1]["dt"].hour * 60))

    # Drop windows shorter than 15 min (barely touches threshold)
    raw = [(s, e) for s, e in raw if e - s >= 15]
    return [(fmt(s), fmt(e)) for s, e in raw]

def format_uv_section(rows, target_date):
    mod_windows  = uv_windows(rows, target_date, UV_MODERATE)
    high_windows = uv_windows(rows, target_date, UV_HIGH)

    if not mod_windows:
        return "🕶️ UV: geen risico vandaag (overal <3)"

    def fmt(ws):
        return ", ".join(a + "-" + b for a, b in ws)

    lines = ["🧴 UV >=3: " + fmt(mod_windows)]
    if high_windows:
        lines.append("⛱️ UV >=5: " + fmt(high_windows))
    else:
        lines.append("⛱️ UV >=5: niet vandaag")
    return "\n".join(lines)


def format_extremes_banner(rows, target_date):
    """Banner for frost or heat across today's hourly temps."""
    temps = [r["temp"] for r in rows if r["dt"].date() == target_date and r.get("temp") is not None]
    if not temps:
        return None
    tmin, tmax = min(temps), max(temps)
    if tmin < FROST_THRESHOLD_C:
        return "❄️ Vorst verwacht: min " + str(round(tmin)) + "°C"
    if tmax > HEAT_THRESHOLD_C:
        return "🔥 Hitte verwacht: max " + str(round(tmax)) + "°C"
    return None


def format_cloud_trend(rows, target_date):
    """Single-line cloud trend across daylight hours, only if shift is meaningful."""
    morning, afternoon = [], []
    for r in rows:
        if r["dt"].date() != target_date:
            continue
        if r.get("cloud") is None:
            continue
        h = r["dt"].hour
        if 6 <= h < 12:
            morning.append(r["cloud"])
        elif 12 <= h < 20:
            afternoon.append(r["cloud"])
    if not morning or not afternoon:
        return None
    delta = (sum(afternoon) / len(afternoon)) - (sum(morning) / len(morning))
    if delta <= -CLOUD_TREND_DELTA_PCT:
        return "☀️ Trend: opklarend in de middag"
    if delta >= CLOUD_TREND_DELTA_PCT:
        return "☁️ Trend: bewolking neemt toe"
    return None


def fetch_pollen(location):
    """Fetch today's hourly pollen forecast. Returns None on failure (briefing-resilient)."""
    params = {
        "latitude":  location["lat"],
        "longitude": location["lon"],
        "hourly":    ",".join(POLLEN_LABELS_NL.keys()),
        "timezone":  location["timezone"],
        "forecast_days": 1,
    }
    try:
        r = requests.get(POLLEN_API_URL, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def _pollen_level(value):
    if value < 20:
        return None
    if value < 50:
        return "matig"
    if value < 100:
        return "hoog"
    return "zeer hoog"


def format_pollen_section(pollen_json, target_date):
    if not pollen_json or "hourly" not in pollen_json:
        return None
    h = pollen_json["hourly"]
    times = h.get("time", [])
    day_idx = [i for i, t in enumerate(times) if datetime.fromisoformat(t).date() == target_date]
    if not day_idx:
        return None
    parts = []
    for key, label in POLLEN_LABELS_NL.items():
        series = h.get(key)
        if not series:
            continue
        vals = [series[i] for i in day_idx if series[i] is not None]
        if not vals:
            continue
        peak = max(vals)
        level = _pollen_level(peak)
        if level is None:
            continue
        parts.append(label + " " + level + " (" + str(round(peak)) + ")")
    if not parts:
        return None
    return "🌼 Pollen: " + ", ".join(parts)


def build_message(location, forecast, today, pollen=None):
    rows = parse_hourly(forecast)
    home = is_home(location)
    weekday = today.weekday()

    header = "*Weerbericht " + today.strftime("%a %d %b") + " - " + location["label"] + "*"
    if not home:
        header += " (vakantie)"

    parts = [header, ""]

    if home:
        banner = format_extremes_banner(rows, today)
        if banner:
            parts.append(banner)
            parts.append("")

        blocks = PEUTER_BLOCKS if weekday in PEUTER_DAYS else WEEKDAY_BLOCKS
        for label, sh, sm, eh, em, days, icon in blocks:
            if days is not None and weekday not in days:
                continue
            block_hours = hours_in_window(rows, today, sh, sm, eh, em)
            window_label = icon + " " + label + " " + ("%02d:%02d" % (sh, sm)) + "-" + ("%02d:%02d" % (eh, em))
            parts.append(summarize_block(window_label, block_hours))
            parts.append("")

        trend = format_cloud_trend(rows, today)
        if trend:
            parts.append(trend)
            parts.append("")
    else:
        parts.append("_(buiten Utrecht - alleen UV-info)_")
        parts.append("")

    parts.append(format_uv_section(rows, today))

    if home:
        pollen_line = format_pollen_section(pollen, today)
        if pollen_line:
            parts.append(pollen_line)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tz = ZoneInfo(LOCATION["timezone"])
    today = datetime.now(tz).date()

    forecast = fetch_forecast(LOCATION)
    pollen   = fetch_pollen(LOCATION) if is_home(LOCATION) else None
    message  = build_message(LOCATION, forecast, today, pollen=pollen)

    print(message)
    print("---")

    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1, niet verzonden.")
        return

    send_telegram(message, chat_id=os.getenv("TELEGRAM_CHAT_GROUP_ID"),
                  parse_mode="Markdown")
    print("Verzonden naar Telegram.")


if __name__ == "__main__":
    run_guarded(main, "weerbriefing", chat_id=os.getenv("TELEGRAM_CHAT_GROUP_ID"))

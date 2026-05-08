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
import sys
import math
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo

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
    ("KDV brengen",    8,  0,  9,  0, {0, 1, 2, 3},    "🧒"),
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
            "cloud_cover",
        ]),
        "timezone":     location["timezone"],
        "forecast_days": 1,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def parse_hourly(forecast):
    h = forecast["hourly"]
    rows = []
    for i, t in enumerate(h["time"]):
        dt = datetime.fromisoformat(t)
        rows.append({
            "dt": dt,
            "temp":   h["temperature_2m"][i],
            "feels":  h["apparent_temperature"][i],
            "precip": h["precipitation"][i],
            "pop":    h["precipitation_probability"][i],
            "uv":     h["uv_index"][i],
            "cloud":  h.get("cloud_cover", [None] * len(h["time"]))[i],
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

    return (
        label + " " + glyph + "\n"
        "   Temp " + fmt_range(temps) + " (gevoel " + fmt_range(feels) + ")"
        "  Regen " + str(round(pop)) + "% / " + ("%.1f" % precip) + " mm"
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

    lines = ["🕶️ UV >=3: " + fmt(mod_windows)]
    if high_windows:
        lines.append("🧴 UV >=5: " + fmt(high_windows))
    else:
        lines.append("🧴 UV >=5: niet vandaag")
    return "\n".join(lines)



def build_message(location, forecast, today):
    rows = parse_hourly(forecast)
    home = is_home(location)
    weekday = today.weekday()

    header = "*Weerbericht " + today.strftime("%a %d %b") + " - " + location["label"] + "*"
    if not home:
        header += " (vakantie)"

    parts = [header, ""]

    if home:
        blocks = PEUTER_BLOCKS if weekday in PEUTER_DAYS else WEEKDAY_BLOCKS
        for label, sh, sm, eh, em, days, icon in blocks:
            if days is not None and weekday not in days:
                continue
            block_hours = hours_in_window(rows, today, sh, sm, eh, em)
            window_label = icon + " " + label + " " + ("%02d:%02d" % (sh, sm)) + "-" + ("%02d:%02d" % (eh, em))
            parts.append(summarize_block(window_label, block_hours))
            parts.append("")
    else:
        parts.append("_(buiten Utrecht - alleen UV-info)_")
        parts.append("")

    parts.append(format_uv_section(rows, today))

    return "\n".join(parts)


def send_telegram(message):
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    tz = ZoneInfo(LOCATION["timezone"])
    today = datetime.now(tz).date()

    forecast = fetch_forecast(LOCATION)
    message  = build_message(LOCATION, forecast, today)

    print(message)
    print("---")

    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1, niet verzonden.")
        return

    send_telegram(message)
    print("Verzonden naar Telegram.")


if __name__ == "__main__":
    sys.exit(main())

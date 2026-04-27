“””
Daily weather briefing via Telegram.

Fetches hourly forecast from Open-Meteo and sends a personalized
briefing covering specific time blocks (commute, school run, return,
sport on Mon/Wed) plus daily UV exposure windows.

When the configured location is not “home” (>10km from Utrecht),
only UV info is reported.
“””

import os
import sys
import math
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo

# —————————————————————————

# Config

# —————————————————————————

# Change these when traveling. Timezone must match the location.

LOCATION = {
“lat”: 52.0907,
“lon”: 5.1214,
“label”: “Utrecht”,
“timezone”: “Europe/Amsterdam”,
}

# “Home” reference point for the thuis/vakantie check.

HOME_COORDS = (52.0907, 5.1214)  # Utrecht
HOME_RADIUS_KM = 10.0

# Time blocks: (label, start_hour, start_minute, end_hour, end_minute, weekdays)

# weekdays: None = every day, or set of weekday ints (Mon=0 … Sun=6)

TIME_BLOCKS = [
(“🚴 Fietstocht”,       6, 0,  6, 30, None),
(“👶 KDV brengen”,      8, 0,  9,  0, None),
(“🏠 Naar huis”,       16, 30, 17, 30, None),
(“🏃 Sport (vrouw)”,   19, 0, 20,  0, {0, 2}),  # Mon & Wed
]

# UV thresholds

UV_MODERATE = 3.0   # “use sunscreen” level
UV_HIGH     = 5.0   # “be careful” level

# —————————————————————————

# Helpers

# —————————————————————————

def haversine_km(lat1, lon1, lat2, lon2):
“”“Great-circle distance in km between two lat/lon points.”””
R = 6371.0
p1, p2 = math.radians(lat1), math.radians(lat2)
dp = math.radians(lat2 - lat1)
dl = math.radians(lon2 - lon1)
a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
return 2 * R * math.asin(math.sqrt(a))

def is_home(location):
d = haversine_km(location[“lat”], location[“lon”], *HOME_COORDS)
return d <= HOME_RADIUS_KM

def fetch_forecast(location):
“”“Fetch hourly forecast for today from Open-Meteo.”””
url = “https://api.open-meteo.com/v1/forecast”
params = {
“latitude”:  location[“lat”],
“longitude”: location[“lon”],
“hourly”: “,”.join([
“temperature_2m”,
“apparent_temperature”,
“precipitation”,
“precipitation_probability”,
“uv_index”,
]),
“timezone”:     location[“timezone”],
“forecast_days”: 1,
}
r = requests.get(url, params=params, timeout=20)
r.raise_for_status()
return r.json()

def parse_hourly(forecast):
“””
Convert Open-Meteo hourly arrays into a list of dicts keyed by datetime.
Open-Meteo returns times in the requested timezone (naive ISO strings).
“””
h = forecast[“hourly”]
rows = []
for i, t in enumerate(h[“time”]):
dt = datetime.fromisoformat(t)  # naive, in location tz
rows.append({
“dt”: dt,
“temp”:   h[“temperature_2m”][i],
“feels”:  h[“apparent_temperature”][i],
“precip”: h[“precipitation”][i],
“pop”:    h[“precipitation_probability”][i],
“uv”:     h[“uv_index”][i],
})
return rows

def hours_in_window(rows, target_date, start_h, start_m, end_h, end_m):
“””
Return all hourly rows whose timestamp falls in [start, end) on target_date.

```
Open-Meteo hourly data is timestamped at the start of each hour.
A row at 06:00 represents the 06:00–07:00 interval. So for a window
06:00–06:30 we include the 06:00 row (it covers that half hour).
For 08:00–09:00 we include the 08:00 row.
For 16:30–17:30 we include the 16:00 and 17:00 rows (both partly overlap).
"""
result = []
for r in rows:
    if r["dt"].date() != target_date:
        continue
    # Hourly bucket: this row covers [hour, hour+1)
    bucket_start_min = r["dt"].hour * 60
    bucket_end_min   = bucket_start_min + 60
    win_start_min    = start_h * 60 + start_m
    win_end_min      = end_h * 60 + end_m
    # Overlap test
    if bucket_start_min < win_end_min and bucket_end_min > win_start_min:
        result.append(r)
return result
```

def summarize_block(label, hours):
“”“Format one time-block line for the message.”””
if not hours:
return f”{label}: geen data”

```
temps  = [h["temp"]   for h in hours]
feels  = [h["feels"]  for h in hours]
precip = sum(h["precip"] for h in hours)
pop    = max(h["pop"]    for h in hours)

# If the window spans multiple hours, show range; else single value
def fmt_range(vals, unit="°C"):
    lo, hi = min(vals), max(vals)
    if abs(hi - lo) < 0.5:
        return f"{round(lo)}{unit}"
    return f"{round(lo)}–{round(hi)}{unit}"

return (
    f"{label}\n"
    f"   🌡 {fmt_range(temps)} (gevoel {fmt_range(feels)})"
    f"  ☔ {round(pop)}% / {precip:.1f} mm"
)
```

def uv_windows(rows, target_date, threshold):
“””
Find contiguous ranges (HH:MM–HH:MM) where UV >= threshold on target_date.
Returns a list of (start_str, end_str) tuples. End time is hour+1
because each row represents a 1-hour bucket.
“””
day_rows = [r for r in rows if r[“dt”].date() == target_date]
day_rows.sort(key=lambda r: r[“dt”])

```
windows = []
cur_start = None
cur_last  = None

for r in day_rows:
    if r["uv"] is not None and r["uv"] >= threshold:
        if cur_start is None:
            cur_start = r["dt"].hour
        cur_last = r["dt"].hour
    else:
        if cur_start is not None:
            windows.append((cur_start, cur_last + 1))
            cur_start = None
            cur_last  = None
if cur_start is not None:
    windows.append((cur_start, cur_last + 1))

return [(f"{s:02d}:00", f"{e:02d}:00") for s, e in windows]
```

def format_uv_section(rows, target_date):
mod_windows  = uv_windows(rows, target_date, UV_MODERATE)
high_windows = uv_windows(rows, target_date, UV_HIGH)

```
if not mod_windows:
    return "☀️ UV: geen risico vandaag (overal <3)"

def fmt(ws):
    return ", ".join(f"{a}–{b}" for a, b in ws)

lines = [f"☀️ UV ≥3: {fmt(mod_windows)}"]
if high_windows:
    lines.append(f"⚠️ UV ≥5: {fmt(high_windows)}")
else:
    lines.append("⚠️ UV ≥5: niet vandaag")
return "\n".join(lines)
```

def build_message(location, forecast, today):
rows = parse_hourly(forecast)
home = is_home(location)
weekday = today.weekday()  # Mon=0

```
header = f"*Weerbericht {today.strftime('%a %d %b')} — {location['label']}*"
if not home:
    header += " ✈️"

parts = [header, ""]

if home:
    for label, sh, sm, eh, em, days in TIME_BLOCKS:
        if days is not None and weekday not in days:
            continue
        block_hours = hours_in_window(rows, today, sh, sm, eh, em)
        window_label = f"{label} {sh:02d}:{sm:02d}–{eh:02d}:{em:02d}"
        parts.append(summarize_block(window_label, block_hours))
        parts.append("")
else:
    parts.append("_(buiten Utrecht — alleen UV-info)_")
    parts.append("")

parts.append(format_uv_section(rows, today))

return "\n".join(parts)
```

def send_telegram(message):
token   = os.environ[“TELEGRAM_BOT_TOKEN”]
chat_id = os.environ[“TELEGRAM_CHAT_ID”]
url = f”https://api.telegram.org/bot{token}/sendMessage”
r = requests.post(url, json={
“chat_id”: chat_id,
“text”: message,
“parse_mode”: “Markdown”,
“disable_web_page_preview”: True,
}, timeout=20)
r.raise_for_status()

# —————————————————————————

# Main

# —————————————————————————

def main():
tz = ZoneInfo(LOCATION[“timezone”])
today = datetime.now(tz).date()

```
forecast = fetch_forecast(LOCATION)
message  = build_message(LOCATION, forecast, today)

print(message)
print("---")

if os.environ.get("DRY_RUN") == "1":
    print("DRY_RUN=1, niet verzonden.")
    return

send_telegram(message)
print("Verzonden naar Telegram.")
```

if **name** == “**main**”:
sys.exit(main())

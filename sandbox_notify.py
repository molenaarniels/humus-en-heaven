#!/usr/bin/env python3
"""
sandbox_notify.py — Zandbak notificaties via Telegram
Losse module naast het bestaande Humus & Heaven systeem.

Logica:
- Ochtend (07:00): stuur bericht als luchten de moeite waard is
- Avond (20:00): stuur bericht op basis van huidige status + neerslagverwachting

Status-waarden:
  "open"      — zandbak is gelucht/open
  "dicht"     — zandbak is dicht (tegen katten), maar niet afgedekt
  "afgedekt"  — zandbak is afgedekt met dekzeil (tegen regen)

Beslislogica ochtend:
  - Geen bericht als status == "afgedekt" EN regen verwacht
  - Bericht "luchten!" als regen_kans < drempel (bijv. < 30%) EN min_temp > 5
  - Geen bericht als slecht weer verwacht (te veel regen kans)

Beslislogica avond:
  - Status "open": bericht als regen verwacht → "afdekken" of "sluiten"
  - Status "dicht": bericht als regen verwacht → "afdekken"
  - Status "afgedekt" EN regen verwacht: geen bericht
  - Status "afgedekt" EN geen regen (komende dagen): bericht "kan morgen gelucht"

Regen-drempel: precipitatie_kans >= RAIN_PROB_THRESHOLD (%) of totaal >= RAIN_MM_THRESHOLD

Afdek-advies avond: afdekken als regen verwacht komende nacht/ochtend
                    sluiten (dicht) als alleen kattenbescherming nodig
"""

import json
import os
import sys
import requests
from datetime import datetime, date, timedelta, timezone

# ── Configuratie ──────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE         = os.environ.get("SANDBOX_STATE_FILE", "sandbox_state.json")

# Utrecht locatie
LATITUDE  = 52.0907
LONGITUDE = 5.1214

# Drempels
RAIN_PROB_THRESHOLD = 30   # % kans op neerslag → beschouwen als "regen verwacht"
RAIN_MM_THRESHOLD   = 1.0  # mm totaal neerslag die dag → ook telt als "regen"
MIN_TEMP_LUCHTEN    = 7    # °C minimumtemperatuur om luchten zinvol te achten

# ── State I/O ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Laad zandbak-status uit JSON bestand."""
    defaults = {
        "status": "dicht",          # open | dicht | afgedekt
        "last_updated": None,
        "last_notification": None,
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {**defaults, **data}
    return defaults


def save_state(state: dict) -> None:
    """Sla zandbak-status op naar JSON bestand."""
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print(f"[state] Opgeslagen: {state}")


# ── Weersdata (Open-Meteo) ────────────────────────────────────────────────────

def fetch_forecast() -> list[dict]:
    """
    Haal 3-daagse forecast op via Open-Meteo (geen API key nodig).
    Geeft lijst van dicts met: date, precip_mm, precip_prob_max, tmin, tmax
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":           LATITUDE,
        "longitude":          LONGITUDE,
        "daily":              [
            "precipitation_sum",
            "precipitation_probability_max",
            "temperature_2m_min",
            "temperature_2m_max",
        ],
        "forecast_days":      4,
        "timezone":           "Europe/Amsterdam",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()["daily"]

    days = []
    for i, d in enumerate(data["time"]):
        days.append({
            "date":            d,
            "precip_mm":       data["precipitation_sum"][i] or 0.0,
            "precip_prob_max": data["precipitation_probability_max"][i] or 0,
            "tmin":            data["temperature_2m_min"][i] or 0.0,
            "tmax":            data["temperature_2m_max"][i] or 0.0,
        })
    return days


def is_rain_expected(day: dict) -> bool:
    """Geeft True als er significante regen verwacht wordt op die dag."""
    return (
        day["precip_prob_max"] >= RAIN_PROB_THRESHOLD
        or day["precip_mm"] >= RAIN_MM_THRESHOLD
    )


def first_dry_day(forecast: list[dict], from_index: int = 1) -> str | None:
    """
    Zoek eerste droge dag na vandaag.
    Geeft de datum terug als string, of None als geen droge dag in forecast.
    """
    for day in forecast[from_index:]:
        if not is_rain_expected(day):
            return day["date"]
    return None


# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    """Verstuur bericht via Telegram Bot API."""
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    resp = requests.post(url, data=data, timeout=10)
    if resp.ok:
        print(f"[telegram] Verzonden: {message[:80]}...")
        return True
    else:
        print(f"[telegram] Fout: {resp.status_code} {resp.text}", file=sys.stderr)
        return False


# ── Ochtendlogica (07:00) ─────────────────────────────────────────────────────

def morning_check(state: dict, forecast: list[dict]) -> tuple[str | None, dict]:
    """
    Bepaal ochtendmelding. Geeft (bericht | None, nieuwe_state).
    """
    today   = forecast[0]
    status  = state["status"]
    regen_vandaag = is_rain_expected(today)
    warm_genoeg   = today["tmax"] >= MIN_TEMP_LUCHTEN

    print(f"[ochtend] status={status} regen_vandaag={regen_vandaag} "
          f"precip_kans={today['precip_prob_max']}% precip_mm={today['precip_mm']:.1f} "
          f"tmax={today['tmax']}°C")

    # Afgedekt + regen → niks doen, alles klopt
    if status == "afgedekt" and regen_vandaag:
        print("[ochtend] Afgedekt + regen → geen bericht")
        return None, state

    # Luchten is de moeite waard
    if not regen_vandaag and warm_genoeg:
        if status == "afgedekt":
            msg = (
                "🏖️ <b>Zandbak kan vandaag gelucht worden!</b>\n"
                f"Droog ({today['precip_prob_max']}% kans, {today['precip_mm']:.0f}mm) "
                f"en {today['tmax']:.0f}°C. Dekzeil eraf!\n"
                "→ Vergeet niet vanavond te checken of hij dicht moet."
            )
        elif status == "dicht":
            msg = (
                "☀️ <b>Zandbak kan gelucht worden vandaag!</b>\n"
                f"Geen regen verwacht ({today['precip_prob_max']}% kans) "
                f"en {today['tmax']:.0f}°C.\n"
                "→ Deksel eraf, laat hem lekker luchten."
            )
        else:
            # Al open → geen bericht nodig
            print("[ochtend] Al open, geen bericht")
            return None, state
        return msg, state

    # Regen verwacht maar zandbak staat open → attentie
    if regen_vandaag and status == "open":
        msg = (
            "🌧️ <b>Regen verwacht vandaag — zandbak staat open!</b>\n"
            f"{today['precip_prob_max']}% kans, {today['precip_mm']:.0f}mm verwacht.\n"
            "→ Afdekken of sluiten?"
        )
        return msg, state

    print("[ochtend] Geen actie nodig")
    return None, state


# ── Avondlogica (20:00) ───────────────────────────────────────────────────────

def evening_check(state: dict, forecast: list[dict]) -> tuple[str | None, dict]:
    """
    Bepaal avondmelding. Geeft (bericht | None, nieuwe_state).
    Kijkt naar morgen + overmorgen voor neerslag.
    """
    morgen       = forecast[1] if len(forecast) > 1 else None
    overmorgen   = forecast[2] if len(forecast) > 2 else None
    status       = state["status"]

    regen_morgen      = is_rain_expected(morgen) if morgen else False
    regen_overmorgen  = is_rain_expected(overmorgen) if overmorgen else False
    regen_nabij       = regen_morgen  # primaire trigger is morgen

    print(f"[avond] status={status} regen_morgen={regen_morgen} "
          f"({morgen['precip_prob_max'] if morgen else '-'}%) "
          f"regen_overmorgen={regen_overmorgen}")

    # ── Zandbak staat open ──
    if status == "open":
        if regen_nabij:
            # Langere regenperiode? Dan afdekken. Anders sluiten (makkelijker morgen open)
            if regen_overmorgen:
                droge_dag = first_dry_day(forecast, from_index=1)
                droge_str = (
                    f"Eerste droge dag: {_format_date(droge_dag)}"
                    if droge_dag else "Geen droge dag in zicht"
                )
                msg = (
                    "🌧️ <b>Zandbak afdekken vanavond!</b>\n"
                    f"Morgen {morgen['precip_prob_max']}% regen "
                    f"({morgen['precip_mm']:.0f}mm), en ook overmorgen neerslag.\n"
                    f"{droge_str}.\n"
                    "→ Dekzeil erop."
                )
            else:
                msg = (
                    "🌦️ <b>Zandbak sluiten vanavond</b>\n"
                    f"Morgen {morgen['precip_prob_max']}% kans op regen, "
                    f"maar overmorgen is het droog.\n"
                    "→ Deksel erop (makkelijk morgenochtend weer open)."
                )
        else:
            # Droog → herinner om te sluiten tegen katten
            msg = (
                "🐱 <b>Zandbak sluiten tegen de katten</b>\n"
                "Morgen droog, dus morgen gewoon weer openzetten.\n"
                "→ Deksel erop voor de nacht."
            )
        return msg, state

    # ── Zandbak staat dicht (niet afgedekt) ──
    if status == "dicht":
        if regen_nabij:
            if regen_overmorgen:
                droge_dag = first_dry_day(forecast, from_index=1)
                droge_str = (
                    f"Eerste droge dag: {_format_date(droge_dag)}"
                    if droge_dag else "Geen droge dag in zicht"
                )
                msg = (
                    "⚠️ <b>Zandbak afdekken!</b>\n"
                    f"Morgen {morgen['precip_prob_max']}% regen ({morgen['precip_mm']:.0f}mm) "
                    f"én overmorgen. Deksel alleen is niet genoeg.\n"
                    f"{droge_str}.\n"
                    "→ Dekzeil erop."
                )
            else:
                # Één regendag, daarna droog → deksel is voldoende als hij al dicht is
                print("[avond] Dicht + alleen morgen regen → deksel voldoende, geen bericht")
                return None, state
        else:
            # Droog → geen bericht nodig
            print("[avond] Dicht + droog → geen bericht")
            return None, state
        return msg, state

    # ── Zandbak is afgedekt ──
    if status == "afgedekt":
        if regen_nabij:
            # Alles is goed, geen actie
            print("[avond] Afgedekt + regen → geen bericht")
            return None, state
        else:
            # Droog morgen → meld dat het morgen gelucht kan worden
            msg = (
                "🌤️ <b>Zandbak kan morgen gelucht worden!</b>\n"
                f"Morgen droog ({morgen['precip_prob_max'] if morgen else '?'}% kans) "
                f"en {morgen['tmax'] if morgen else '?'}°C.\n"
                "→ Dekzeil eraf morgenochtend."
            )
            return msg, state

    print(f"[avond] Onbekende status '{status}', geen actie")
    return None, state


# ── Hulpfuncties ──────────────────────────────────────────────────────────────

def _format_date(d: str | None) -> str:
    """Formateer YYYY-MM-DD naar bijv. 'vrijdag 25 apr'."""
    if not d:
        return "onbekend"
    dt = datetime.strptime(d, "%Y-%m-%d")
    nl_days   = ["maandag","dinsdag","woensdag","donderdag","vrijdag","zaterdag","zondag"]
    nl_months = ["","jan","feb","mrt","apr","mei","jun","jul","aug","sep","okt","nov","dec"]
    return f"{nl_days[dt.weekday()]} {dt.day} {nl_months[dt.month]}"


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("morning", "evening"):
        print("Gebruik: python sandbox_notify.py [morning|evening]", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[1]
    print(f"[sandbox_notify] Start — mode={mode} tijd={datetime.now(timezone.utc).isoformat()}")

    state    = load_state()
    forecast = fetch_forecast()

    print(f"[forecast] Vandaag: {forecast[0]}")
    if len(forecast) > 1:
        print(f"[forecast] Morgen:  {forecast[1]}")

    if mode == "morning":
        bericht, nieuwe_state = morning_check(state, forecast)
    else:
        bericht, nieuwe_state = evening_check(state, forecast)

    if bericht:
        send_telegram(bericht)
        nieuwe_state["last_notification"] = datetime.now(timezone.utc).isoformat()

    save_state(nieuwe_state)
    print("[sandbox_notify] Klaar")


if __name__ == "__main__":
    main()

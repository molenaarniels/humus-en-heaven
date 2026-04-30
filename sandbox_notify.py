#!/usr/bin/env python3
"""
sandbox_notify.py — Zandbak notificaties via Telegram
Losse module naast het bestaande Humus & Heaven systeem.

Status-waarden:
  "open"      — zandbak is gelucht/open
  "dicht"     — zandbak is dicht (tegen katten), droog verwacht, morgen weer open
  "afgedekt"  — zandbak is afgedekt met dekzeil (regen verwacht)

Avondlogica:
  - Regen verwacht (morgen of later) → altijd AFDEKKEN
  - Alleen droog verwacht → SLUITEN (dicht), morgen ochtend bericht om te luchten
  - Afgedekt + regen → geen bericht

Ochtendlogica:
  - Droog + warm → bericht om te luchten (zowel vanuit "dicht" als "afgedekt")
  - Al open + droog → geen bericht
  - Open + regen vandaag → waarschuw

State wordt automatisch bijgewerkt na elk advies.
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone

# ── Configuratie ──────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE         = os.environ.get("SANDBOX_STATE_FILE", "sandbox_state.json")

LATITUDE  = 52.0907
LONGITUDE = 5.1214

RAIN_PROB_THRESHOLD = 30   # % kans → "regen verwacht"
RAIN_MM_THRESHOLD   = 1.0  # mm → ook "regen verwacht"
MIN_TEMP_LUCHTEN    = 7    # °C tmax minimaal nodig om luchten zinvol te achten

# ── State I/O ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    defaults = {"status": "dicht", "last_updated": None, "last_notification": None}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {**defaults, **data}
    return defaults


def save_state(state: dict) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print(f"[state] Opgeslagen: status={state['status']}")


# ── Weersdata (Open-Meteo) ────────────────────────────────────────────────────

def fetch_forecast() -> list[dict]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":      LATITUDE,
        "longitude":     LONGITUDE,
        "daily": [
            "precipitation_sum",
            "precipitation_probability_max",
            "temperature_2m_min",
            "temperature_2m_max",
        ],
        "forecast_days": 4,
        "timezone":      "Europe/Amsterdam",
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
    return (
        day["precip_prob_max"] >= RAIN_PROB_THRESHOLD
        or day["precip_mm"] >= RAIN_MM_THRESHOLD
    )


def first_dry_day(forecast: list[dict], from_index: int = 1) -> str | None:
    for day in forecast[from_index:]:
        if not is_rain_expected(day):
            return day["date"]
    return None


# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    resp = requests.post(url, data=data, timeout=10)
    if resp.ok:
        print(f"[telegram] Verzonden: {message[:80]}...")
        return True
    else:
        print(f"[telegram] Fout: {resp.status_code} {resp.text}", file=sys.stderr)
        return False


# ── Ochtendlogica (07:00) ─────────────────────────────────────────────────────

def morning_check(state: dict, forecast: list[dict]) -> tuple[str | None, dict]:
    today  = forecast[0]
    status = state["status"]
    regen_vandaag = is_rain_expected(today)
    warm_genoeg   = today["tmax"] >= MIN_TEMP_LUCHTEN

    print(f"[ochtend] status={status} regen_vandaag={regen_vandaag} "
          f"precip_kans={today['precip_prob_max']}% precip_mm={today['precip_mm']:.1f} "
          f"tmax={today['tmax']}°C")

    # Al open + droog → niks te doen
    if status == "open" and not regen_vandaag:
        print("[ochtend] Al open en droog → geen bericht")
        return None, state

    # Afgedekt + regen vandaag → alles klopt, geen actie
    if status == "afgedekt" and regen_vandaag:
        print("[ochtend] Afgedekt + regen → geen bericht")
        return None, state

    # Droog + warm genoeg → luchten (vanuit dicht of afgedekt)
    if not regen_vandaag and warm_genoeg:
        if status == "afgedekt":
            msg = (
                "🏖️ <b>Zandbak kan vandaag gelucht worden!</b>\n"
                f"Droog ({today['precip_prob_max']}% kans, {today['precip_mm']:.0f}mm) "
                f"en {today['tmax']:.0f}°C. Dekzeil eraf!\n"
                "→ Vergeet vanavond niet te checken of hij dicht moet."
            )
        else:  # dicht
            msg = (
                "☀️ <b>Zandbak kan gelucht worden vandaag!</b>\n"
                f"Geen regen verwacht ({today['precip_prob_max']}% kans) "
                f"en {today['tmax']:.0f}°C.\n"
                "→ Deksel eraf, laat hem lekker luchten."
            )
        return msg, {**state, "status": "open"}

    # Open of dicht + regen vandaag → waarschuw (deksel alleen is niet genoeg)
    if status in ("open", "dicht") and regen_vandaag:
        intro = (
            "🌧️ <b>Regen verwacht vandaag — zandbak staat open!</b>"
            if status == "open"
            else "🌧️ <b>Regen verwacht vandaag — zandbak alleen dicht, niet afgedekt!</b>"
        )
        msg = (
            f"{intro}\n"
            f"{today['precip_prob_max']}% kans, {today['precip_mm']:.0f}mm verwacht.\n"
            "→ Afdekken!"
        )
        return msg, state

    # Afgedekt + droog maar te koud → geen bericht
    if status == "afgedekt" and not regen_vandaag and not warm_genoeg:
        print(f"[ochtend] Afgedekt + droog maar te koud ({today['tmax']}°C) → geen bericht")
        return None, state

    print("[ochtend] Geen actie nodig")
    return None, state


# ── Avondlogica (20:00) ───────────────────────────────────────────────────────

def evening_check(state: dict, forecast: list[dict]) -> tuple[str | None, dict]:
    today      = forecast[0]
    raw_morgen = forecast[1] if len(forecast) > 1 else None
    overmorgen = forecast[2] if len(forecast) > 2 else None
    status     = state["status"]

    # Forecast soms < 2 dagen (API-hiccup, late run): val terug op vandaag voor
    # tmax/regen-velden zodat downstream-formatters geen '?' tonen, en zet een
    # waarschuwing bovenaan het bericht.
    limited_forecast = raw_morgen is None
    morgen = raw_morgen if raw_morgen is not None else today

    regen_morgen     = is_rain_expected(raw_morgen) if raw_morgen else False
    regen_overmorgen = is_rain_expected(overmorgen) if overmorgen else False
    regen_nabij      = regen_morgen or regen_overmorgen  # enige regen → afdekken

    print(f"[avond] status={status} regen_morgen={regen_morgen} "
          f"({morgen['precip_prob_max']}%) "
          f"regen_overmorgen={regen_overmorgen} regen_nabij={regen_nabij} "
          f"limited_forecast={limited_forecast}")

    def _wrap(msg: str | None) -> str | None:
        if msg is None or not limited_forecast:
            return msg
        return "⚠️ <i>Beperkte forecast (alleen vandaag beschikbaar)</i>\n" + msg

    # ── Open ──
    if status == "open":
        if regen_morgen:
            droge_dag = first_dry_day(forecast, from_index=1)
            droge_str = f"Eerste droge dag: {_format_date(droge_dag)}" if droge_dag else "Geen droge dag in zicht"
            regen_desc = (
                f"Morgen {morgen['precip_prob_max']}% regen ({morgen['precip_mm']:.0f}mm)"
                + (", en ook overmorgen." if regen_overmorgen else ".")
            )
            msg = (
                f"🌧️ <b>Zandbak afdekken vanavond!</b>\n"
                f"{regen_desc}\n"
                f"{droge_str}.\n"
                "→ Dekzeil erop."
            )
            return _wrap(msg), {**state, "status": "afgedekt"}
        else:
            # Droog morgen → sluiten tegen katten; morgenochtend krijg je bericht om te openen
            msg = (
                "🐱 <b>Zandbak sluiten tegen de katten</b>\n"
                "Morgen droog, je krijgt morgenochtend een berichtje om hem te openen.\n"
                "→ Deksel erop voor de nacht."
            )
            return _wrap(msg), {**state, "status": "dicht"}

    # ── Dicht ──
    if status == "dicht":
        if regen_morgen:
            droge_dag = first_dry_day(forecast, from_index=1)
            droge_str = f"Eerste droge dag: {_format_date(droge_dag)}" if droge_dag else "Geen droge dag in zicht"
            regen_desc = (
                f"Morgen {morgen['precip_prob_max']}% regen ({morgen['precip_mm']:.0f}mm)"
                + (", en ook overmorgen." if regen_overmorgen else ".")
            )
            msg = (
                f"⚠️ <b>Zandbak afdekken!</b>\n"
                f"{regen_desc} Deksel alleen is niet genoeg.\n"
                f"{droge_str}.\n"
                "→ Dekzeil erop."
            )
            return _wrap(msg), {**state, "status": "afgedekt"}
        else:
            # Droog morgen → geen actie (morgen ochtend triggert luchten)
            print("[avond] Dicht + droog morgen → geen bericht, ochtend triggert luchten")
            return None, state

    # ── Afgedekt ──
    if status == "afgedekt":
        if regen_nabij:
            # Alles klopt
            print("[avond] Afgedekt + regen → geen bericht")
            return None, state
        else:
            # Droog morgen → aankondiging, ochtendrun stuurt het echte bericht
            msg = (
                "🌤️ <b>Morgen kan de zandbak gelucht worden!</b>\n"
                f"Morgen droog ({morgen['precip_prob_max']}% kans) "
                f"en {morgen['tmax']}°C.\n"
                "→ Je krijgt morgenochtend een berichtje."
            )
            # State blijft "afgedekt" — ochtendrun zet hem op open na het bericht
            return _wrap(msg), state

    print(f"[avond] Onbekende status '{status}', geen actie")
    return None, state


# ── Hulpfuncties ──────────────────────────────────────────────────────────────

def _format_date(d: str | None) -> str:
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

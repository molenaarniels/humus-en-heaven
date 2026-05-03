"""
Daily check & notify script.

Runs in GitHub Actions every morning. Doet:
  1. Haalt WU + Open-Meteo data
  2. Haalt irrigatie-log uit GitHub Gist
  3. Rekent soil water balance uit voor lawn + shrubs
  4. Als water geven nodig → stuurt Telegram + e-mail
  5. Schrijft verse data.json terug (voor de GitHub Pages dashboard)

Benodigde env vars (in GitHub Secrets):
  WU_STATION_ID, WU_API_KEY
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID      (optioneel)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_TO  (optioneel)
  GIST_ID, GITHUB_TOKEN                     (voor irrigatie-log)
"""

import json
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

from soil_model import (apply_et0_and_balance, assess_status, build_full_dataset,
                        build_monthly_totals_from_days, fetch_open_meteo_archive)

# =============================================================================

def load_existing_monthly_totals() -> dict:
    """Laad bestaande maandtotalen uit docs/data.json (koude opslag)."""
    try:
        with open("docs/data.json") as f:
            return json.load(f).get("monthly_totals", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def bootstrap_monthly_totals(irrigations_raw: dict) -> dict:
    """Eenmalige bootstrap: haal ~13 maanden historische data op en bevries voltooide maanden."""
    today = date.today()
    first_of_month = today.replace(day=1)
    # Einddatum = laatste dag van vorige maand (archive API heeft lag, voltooide maanden zijn veilig)
    end_date = (first_of_month - timedelta(days=1)).isoformat()
    # Startdatum = 1e dag van de maand 13 maanden geleden
    start = first_of_month
    for _ in range(13):
        start = (start - timedelta(days=1)).replace(day=1)
    start_date = start.isoformat()

    print(f"[bootstrap] Ophalen historische data: {start_date} → {end_date}")
    try:
        series = fetch_open_meteo_archive(start_date, end_date)
    except Exception as e:
        print(f"[bootstrap] archive fetch mislukt: {e}")
        return {}

    apply_et0_and_balance(series, irrigations_raw)
    totals = build_monthly_totals_from_days(series)
    print(f"[bootstrap] {len(totals)} maanden bevroren: {sorted(totals.keys())}")
    return totals


def load_irrigations_from_gist() -> dict:
    """Haalt irrigatie-log uit GitHub Gist. Format: {"YYYY-MM-DD": mm}."""
    gist_id = os.getenv("GIST_ID")
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not gist_id:
        print("[irrigations] geen GIST_ID, overslaan")
        return {}
    headers = {"Authorization": f"token {token}"} if token else {}
    try:
        r = requests.get(f"https://api.github.com/gists/{gist_id}",
                         headers=headers, timeout=10)
        r.raise_for_status()
        files = r.json().get("files", {})
        content = files.get("irrigations.json", {}).get("content", "{}")
        data = json.loads(content)
        print(f"[irrigations] {len(data)} events geladen")
        return data
    except Exception as e:
        print(f"[irrigations] kon niet laden: {e}")
        return {}


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram] geen creds, overslaan")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        r.raise_for_status()
        print("[telegram] ✓ verzonden")
        return True
    except Exception as e:
        print(f"[telegram] fout: {e}")
        return False


def send_email(subject: str, body_html: str) -> bool:
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pw   = os.getenv("SMTP_PASS")
    to   = os.getenv("EMAIL_TO")
    if not all([host, user, pw, to]):
        print("[email] geen creds, overslaan")
        return False
    try:
        msg = MIMEText(body_html, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        print("[email] ✓ verzonden")
        return True
    except Exception as e:
        print(f"[email] fout: {e}")
        return False


def format_telegram(status_lawn: dict, status_shrubs: dict,
                    generated_at: str) -> str:
    icons = {"high": "🚨", "medium": "💧", "low": "⏳", "none": "✓"}

    def zone_line(label: str, status: dict) -> list[str]:
        icon = icons[status["priority"]]
        available_pct = 100 - status["depletion_pct"]
        lines = [
            f"{icon} <b>{label}</b>: {status['recommendation']}",
            f"   Beschikbaar water: <code>{available_pct:.0f}%</code>",
        ]
        if status["proposal_min"] > 0:
            lines.append(
                f"   💧 Advies: <b>{status['proposal_min']} min</b>"
                f" (= {status['proposal_mm']:.1f} mm)"
            )
        return lines

    lawn_lines = zone_line("Gras (sproeier)", status_lawn)
    shrub_lines = zone_line("Struiken (druppelslang)", status_shrubs)

    # Samengevat irrigatie-advies als beide water nodig hebben
    both_need = status_lawn["proposal_min"] > 0 and status_shrubs["proposal_min"] > 0
    one_needs = status_lawn["proposal_min"] > 0 or status_shrubs["proposal_min"] > 0

    lines = [
        "<b>Humus &amp; Heaven · dagcheck</b>",
        f"<i>{datetime.now().strftime('%A %d %B %Y')}</i>",
        "",
        *lawn_lines,
        "",
        *shrub_lines,
        "",
        f"🌧️ Regen komende 7d: <b>{status_lawn['rain7_mm']:.1f} mm</b>",
    ]

    if one_needs:
        lines += [""]
        if both_need:
            lines.append(
                f"⏱ Gras: <b>{status_lawn['proposal_min']} min</b> · "
                f"Struiken: <b>{status_shrubs['proposal_min']} min</b>"
            )
        elif status_lawn["proposal_min"] > 0:
            lines.append(f"⏱ Gras: <b>{status_lawn['proposal_min']} min</b>")
        else:
            lines.append(f"⏱ Struiken: <b>{status_shrubs['proposal_min']} min</b>")

    dash = os.getenv("DASHBOARD_URL")
    if dash:
        lines += ["", f'<a href="{dash}">→ Open dashboard</a>']
    return "\n".join(lines)


def format_email(status_lawn: dict, status_shrubs: dict) -> tuple:
    icons = {"high": "🚨", "medium": "💧", "low": "⏳", "none": "✓"}
    highest = status_lawn if {"high":3,"medium":2,"low":1,"none":0}[status_lawn["priority"]] \
        >= {"high":3,"medium":2,"low":1,"none":0}[status_shrubs["priority"]] else status_shrubs
    subject = f"{icons[highest['priority']]} Humus &amp; Heaven — {highest['recommendation']}"
    dash = os.getenv("DASHBOARD_URL", "#")
    body = f"""
<html><body style="font-family:Georgia,serif;color:#2a241b;background:#f3ecd9;padding:32px;">
  <h1 style="font-style:italic;font-weight:500;">Humus &amp; Heaven</h1>
  <p style="color:#5c4f3c;font-style:italic;">
    Dagcheck voor {datetime.now().strftime('%A %d %B %Y')}
  </p>
  <table cellpadding="12" style="border-collapse:collapse;margin-top:16px;">
    <tr style="border-bottom:1px solid #2a241b33;">
      <td><b>Gras</b><br><small>15 cm wortelzone</small></td>
      <td>{icons[status_lawn['priority']]} {status_lawn['recommendation']}<br>
          <small style="color:#5c4f3c;">Beschikbaar water {100 - status_lawn['depletion_pct']:.0f}%</small></td>
    </tr>
    <tr>
      <td><b>Struiken</b><br><small>40 cm wortelzone</small></td>
      <td>{icons[status_shrubs['priority']]} {status_shrubs['recommendation']}<br>
          <small style="color:#5c4f3c;">Beschikbaar water {100 - status_shrubs['depletion_pct']:.0f}%</small></td>
    </tr>
  </table>
  <p style="margin-top:24px;">
    🌧️ Regen komende 7 dagen: <b>{status_lawn['rain7_mm']:.1f} mm</b>
  </p>
  <p><a href="{dash}" style="color:#3d5a3a;">→ Open dashboard</a></p>
  <hr style="border:none;border-top:1px dashed #2a241b66;margin-top:32px;">
  <p style="font-size:11px;color:#5c4f3c;font-style:italic;">
    FAO-56 Penman-Monteith ET₀ + single-bucket water balance voor zandgrond Utrecht Oost.
  </p>
</body></html>
    """.strip()
    return subject, body


def main():
    station = os.getenv("WU_STATION_ID", "")
    key = os.getenv("WU_API_KEY", "")
    if not station or not key:
        print("⚠ WU_STATION_ID of WU_API_KEY niet gezet!")
        # Ga toch door met Open-Meteo only
    force = os.getenv("FORCE_NOTIFY", "").lower() in ("1", "true", "yes")

    print(f"→ Irrigaties laden uit Gist...")
    irrigations_raw = load_irrigations_from_gist()

    # Bootstrap maandtotalen als er nog geen koude opslag is
    existing_monthly = load_existing_monthly_totals()
    if not existing_monthly:
        print("→ Geen maandtotalen gevonden — eenmalige bootstrap...")
        existing_monthly = bootstrap_monthly_totals(irrigations_raw)

    print(f"→ Data bouwen (WU={bool(station)}, Open-Meteo forecast, 35 warme dagen)...")
    data = build_full_dataset(station, key, irrigations=irrigations_raw, days_past=35)
    data["irrigations"] = irrigations_raw

    # Bevries voltooide maanden uit het warme venster en merge met koude opslag
    new_frozen = build_monthly_totals_from_days(data["days"])
    merged_monthly = {**existing_monthly, **new_frozen}  # nieuwe data wint bij overlap
    data["monthly_totals"] = merged_monthly
    print(f"→ Maandtotalen: {len(merged_monthly)} maanden ({', '.join(sorted(merged_monthly.keys()))})")

    status_lawn = assess_status(data, "lawn")
    status_shrubs = assess_status(data, "shrubs")
    print(f"   Gras: {status_lawn['priority']} — {status_lawn['recommendation']}")
    print(f"   Struiken: {status_shrubs['priority']} — {status_shrubs['recommendation']}")

    # Publish a trimmed status per zone so the dashboard reads the same facts
    # the Telegram message does, instead of re-deriving thresholds in JS.
    # `recommendation` stays Telegram-only (long-form); the dashboard renders
    # its own short tile text from these facts.
    _DASHBOARD_FIELDS = ("state", "priority", "depletion_pct", "days_to_stress",
                         "rain7_mm", "proposal_mm", "proposal_min")
    data["lawn_status"]   = {k: status_lawn[k]   for k in _DASHBOARD_FIELDS}
    data["shrubs_status"] = {k: status_shrubs[k] for k in _DASHBOARD_FIELDS}

    # Data.json wegschrijven voor dashboard
    out = Path("docs/data.json")
    out.parent.mkdir(exist_ok=True)
    with open(out, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"→ {out} geschreven ({out.stat().st_size} bytes)")

    # Notificatie-logica: alleen sturen bij medium/high tenzij force
    priorities = {status_lawn["priority"], status_shrubs["priority"]}
    should_notify = force or ("high" in priorities) or ("medium" in priorities)

    if should_notify:
        print("→ Notificatie nodig, versturen...")
        tg_text = format_telegram(status_lawn, status_shrubs, data["generated_at"])
        send_telegram(tg_text)
        # subject, body = format_email(status_lawn, status_shrubs)
        # send_email(subject, body)
    else:
        print("→ Geen notificatie nodig (alles rustig)")

    print("✓ klaar")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        # Stuur failure Telegram
        try:
            send_telegram(f"⚠ <b>Humus &amp; Heaven</b> check mislukt:\n<code>{e}</code>")
        except Exception:
            pass
        sys.exit(1)

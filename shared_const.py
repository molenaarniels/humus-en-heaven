"""Gedeelde locatie-constanten (Utrecht Oost) — één bron voor alle pipelines.

Alleen stdlib. De per-project modules her-binden deze namen aan hun eigen
lokale aliassen (UTRECHT_LAT, _LAT, …) zodat call-sites en tests ongewijzigd
blijven; dit bestand is uitsluitend de bron van de getallen.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

LATITUDE = 52.0907
LONGITUDE = 5.1214
TZ = ZoneInfo("Europe/Amsterdam")

# Nederlandse dag-/maandnamen voor Telegram-berichten. De runner heeft geen
# nl_NL-locale, dus strftime('%A %B') zou Engelse namen geven — vandaar met
# de hand. Maanden zijn 1-gebaseerd (index 0 is bewust leeg).
NL_DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
NL_MONTHS = ["", "jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def utc_now_iso() -> str:
    """Huidige UTC-tijd als ISO-string met `+00:00`-offset (aware).

    Identiek aan `datetime.now(timezone.utc).isoformat()` — puur om de
    boilerplate op de vele `generated_at`/`last_updated`-sites te bundelen.
    De Z-gesuffixte dashboard-varianten blijven bewust hun eigen vorm houden.
    """
    return datetime.now(timezone.utc).isoformat()


def local_today() -> date:
    """Vandaag in Europe/Amsterdam (`datetime.now(TZ).date()`)."""
    return datetime.now(TZ).date()


def parse_date(s: str) -> date:
    """Parse een `YYYY-MM-DD`-string naar een `date` (`strptime(...).date()`)."""
    return datetime.strptime(s, "%Y-%m-%d").date()


def format_date_nl(d: date) -> str:
    """`date` → 'maandag 6 jul' — de gedeelde Nederlandse datumweergave."""
    return f"{NL_DAYS[d.weekday()]} {d.day} {NL_MONTHS[d.month]}"

"""Dunne, gedeelde transport-helper: GET → JSON met retry/backoff.

Eén implementatie voor de zes Open-Meteo-fetchplekken (soil, briefing,
zandbak, maaien, window, airflow). Incidentele 5xx-hiccups kostten gemeten
~17% van de loop-iteraties zonder retry (zie window_advisor); dit is dat
bewezen retry-gedrag, met gesanitizede foutlogs. Het retry-venster is
verbreed van ~11s naar ~100s (5 pogingen, backoff tot 60s): korte
TLS-reset/timeout-bursts van Open-Meteo worden zo uitgezeten in plaats van
een hele run te laten crashen (en alerten).

Bewust alleen transport: parameter-sets, parsing en fallback-gedrag blijven
bij de aanroepers — de helper abstraheert het hoe, niet het wat. Excepties
propageren na de laatste poging, zodat bestaande fallback-paden (WU-merge,
loop-workflows, run_guarded) blijven werken. WU-fetches blijven bewust
buiten deze helper (apiKey in de URL-string + eigen partial-failure-logica).
"""

import time

import requests

from notify import sanitize_error


def get_json(url: str, params: dict | None = None, *, timeout: int = 20,
             attempts: int = 5, delays: tuple[float, ...] = (3, 8, 30, 60),
             label: str = "http") -> dict:
    """GET de URL en geef de JSON-body terug; retry met backoff bij falen.

    `delays[i]` is de wachttijd vóór poging i+2 (de laatste delay herhaalt als
    er meer pogingen dan delays zijn). Na de laatste mislukte poging raist de
    onderliggende requests-exceptie."""
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            time.sleep(delays[min(attempt - 2, len(delays) - 1)])
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"[{label}] poging {attempt}/{attempts} mislukt: {sanitize_error(e)}")
            if attempt == attempts:
                raise

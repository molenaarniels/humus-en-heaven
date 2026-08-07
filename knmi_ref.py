"""KNMI De Bilt als tweede referentie voor de stationsdiagnose (Project 7).

WAAROM
------
De bias van het eigen WU-station wordt tot nu toe gemeten tegen Open-Meteo ERA5:
een *reanalyse op gridschaal*, geen waarneming. Die referentie draagt zelf ~1.2 °C
RMSE aan gridfout + echt microklimaat, en dát is de vloer waar alle modelvorm-
vragen (schuift de helling met het seizoen? helpt een windterm?) tegenaan lopen —
op die ruis is elk verschil tussen modelvormen ~1%.

KNMI-station **De Bilt (260) ligt 4,1 km van de tuin**: een officieel gesiteerde,
geventileerde hut met een gekalibreerde voeler. Vier kilometer echt microklimaat
is een véél kleinere fout dan een gridcel plus modelfout, dus dezelfde steekproef
levert een scherper antwoord. ERA5 blijft ernaast staan: een correctie die tegen
twee ónafhankelijke referenties standhoudt, is de sterkste overfit-bewaking die
we kunnen krijgen — sterker dan welk CV-schema ook op één referentie.

DE BRON
-------
`daggegevens.knmi.nl/klimatologie/uurgegevens` — de klassieke KNMI-scriptservice.
**Geen API-key, geen nieuwe dependency, jaren historie.** Dat maakt hem meteen
toepasbaar op de uren die al in `data/station_history/` staan, niet alleen op wat
er vanaf nu binnenkomt.

Wat hij níet kan: sub-uurlijk. De vraag of de ~0,8 °C kwartierruis op zonnige
dagen échte lucht is of onze stralingskap, heeft een 5–10-minutenreferentie nodig
(buur-PWS'en of het KNMI Data Platform) — zie de fase-1-notitie.

TIJDCONVENTIE (het subtiele stuk — hier zit de valkuil)
-------------------------------------------------------
De scriptservice geeft per rij een `date` (dag) plus `hour` **1..24 in UT**, waarbij
uur `h` het tijdvak (h−1, h] beslaat en de temperatuur de waarneming *aan het eind*
van dat vak is. Een rij (2026-08-01, hour=1) is dus de meting van 01:00 UT.

Onze sleutel is `YYYY-MM-DDTHH` in UTC, zoals `fetch_om_archive_hourly` die maakt
uit `hourly.time` — óók een momentopname op het hele uur. KNMI `hour=h` mapt
daarmee op `date + h uur` (h=24 rolt naar 00 UT van de volgende dag).

LET OP, en dit is een bestaande scheefheid die deze module niet zelf oplost: het
WU-uur in `station_accuracy.fetch_wu_hourly` is een *emmer* (`obsTimeUtc[:13]`) met
`tempAvg` als gemiddelde óver dat uur, terwijl beide referenties een momentopname
op het hele uur zijn — een halfuur-verschuiving die er altijd al in zat. Met twee
referenties is dat voor het eerst meetbaar in plaats van aanneembaar: als beide
strakker correleren op een verschoven sleutel, dan is dát het bewijs. Vandaar
`HOUR_SHIFT`, expliciet en testbaar, i.p.v. impliciet in een indexberekening.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

from http_util import get_json

UTC = timezone.utc

ENDPOINT = "https://www.daggegevens.knmi.nl/klimatologie/uurgegevens"
DE_BILT = 260                 # KNMI-klimatologienummer (WMO 06260), 4,1 km
VARS = "T:U:FH:Q:N"           # temp, RV, uurgemiddelde wind, globale straling, bewolking
HOUR_SHIFT = 0                # uren t.o.v. de afgeleide UTC-sleutel; zie de tijdconventie

# Eenheden van de scriptservice → SI/onze conventie.
# T  0.1 °C            → °C
# FH 0.1 m/s (10 m)    → m/s, en daarna → km/h (de rest van Project 7 rekent in km/h)
# Q  J/cm² over het uur → W/m² gemiddeld: 1 J/cm² = 10 000 J/m², gedeeld door 3600 s
J_CM2_TO_WM2 = 10_000.0 / 3600.0
MS_TO_KMH = 3.6
OCTANT_OBSCURED = 9           # 9 = "bovenlucht onzichtbaar" → geen bewolkingsgetal


def _num(value) -> Optional[float]:
    """KNMI levert lege velden als '' of None (en soms als string-getal)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _key(day: date, hour: int) -> str:
    """KNMI (dag, uur 1..24 UT) → onze UTC-sleutel `YYYY-MM-DDTHH`."""
    stamp = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(hours=hour + HOUR_SHIFT)
    return stamp.strftime("%Y-%m-%dT%H")


def parse_hourly(records: Iterable[dict]) -> Dict[str, dict]:
    """Scriptservice-records → {utc_hour_key: {temp, rh, wind, solar, cloud}}.

    Pure functie (geen netwerk) zodat de vorm te testen is zonder de API. Rijen
    zonder bruikbare temperatuur vallen af — dat is het veld waar alles om draait;
    de rest mag ontbreken.
    """
    out: Dict[str, dict] = {}
    for rec in records or []:
        raw_day = rec.get("date") or rec.get("YYYYMMDD")
        hour = _num(rec.get("hour") or rec.get("HH"))
        temp = _num(rec.get("T"))
        if raw_day is None or hour is None or temp is None:
            continue
        try:
            day = datetime.fromisoformat(str(raw_day).replace("Z", "+00:00")).date()
        except ValueError:
            try:
                day = datetime.strptime(str(raw_day), "%Y%m%d").date()
            except ValueError:
                continue
        solar = _num(rec.get("Q"))
        wind = _num(rec.get("FH"))
        cloud = _num(rec.get("N"))
        out[_key(day, int(hour))] = {
            "temp": temp / 10.0,
            "rh": _num(rec.get("U")),
            "wind": wind / 10.0 * MS_TO_KMH if wind is not None else None,
            "solar": solar * J_CM2_TO_WM2 if solar is not None else None,
            # Octanten (0–8) → % voor vergelijkbaarheid met Open-Meteo's cloud_cover;
            # 9 betekent "bovenlucht onzichtbaar" en is geen bedekkingsgraad.
            "cloud": cloud / 8.0 * 100.0 if cloud is not None and cloud != OCTANT_OBSCURED else None,
        }
    return out


def fetch_hourly(start: str, end: str, station: int = DE_BILT) -> Dict[str, dict]:
    """Uurwaarnemingen van `station` over [start, end] (ISO-datums, inclusief).

    Geen API-key. Faalt niet-fataal: bij een lege of onbruikbare respons komt er
    een leeg dict terug en logt de aanroeper verder — de ERA5-referentie moet
    blijven werken als het KNMI-pad hapert.
    """
    params = {
        "start": start.replace("-", "") + "01",
        "end": end.replace("-", "") + "24",
        "stns": str(station),
        "vars": VARS,
        "fmt": "json",
    }
    payload = get_json(ENDPOINT, params, timeout=30, label="knmi")
    records = payload if isinstance(payload, list) else payload.get("data", [])
    hours = parse_hourly(records)
    print(f"[KNMI] station {station}: {len(records)} records → {len(hours)} bruikbare uren "
          f"({start} t/m {end})")
    return hours

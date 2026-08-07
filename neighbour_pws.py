"""Buur-PWS'en als coherentie-toets voor de stationsbias (fase 1, route A).

DE VRAAG DIE DIT BEANTWOORDT
---------------------------
Tegen KNMI De Bilt leest ons station bij *nul* instraling +0,56 °C te warm
(+1,08 windstil, −0,14 boven 15 km/h). Er is dan geen straling, dus dit kan geen
stralingsfout van de kap zijn. Twee verklaringen passen even goed op die 4,1 km:

  (a) **microklimaat** — de tuin ligt in stedelijk gebied en is 's nachts echt
      warmer dan het De Bilt-terrein, het sterkst bij windstil weer (klassiek
      hitte-eiland-gedrag, dat met wind wegmengt);
  (b) **plaatsing** — de schuurmuur naast de kap straalt 's nachts na, óók
      wind-geventileerd.

Ze zijn niet te scheiden met één referentie op 4 km, en **meer seizoenen lossen
dat niet op**: het is geen precisieprobleem maar een identificeerbaarheids-
probleem. Wat het wél breekt is een referentie die ons microklimaat déélt maar
een andere kap heeft — buurstations op enkele honderden meters.

  · Zien de buren dezelfde warme nacht → (a), regionaal. Dan is die +1 °C écht de
    temperatuur hier, moet de correctie er afblijven, en blijft `wu_bias` een
    zuivere zon-term.
  · Zien alleen wij hem → (b), onze plaatsing. Dan is een interceptterm terecht
    en wordt de fase-3-beslissing een andere.

De buren zijn goedkope sensoren met hun eigen fouten, en dat geeft niet: dit is
een *coherentie*-toets, geen ijking. We vragen niet wie gelijk heeft, we vragen
of het signaal gedeeld wordt.

OPZET
-----
KNMI levert het gemeenschappelijke frame — referentietemperatuur, instraling én
wind — zodat elk station alleen zijn eigen temperatuur inbrengt. Anders zou je
per station een andere anemometer op een andere hoogte in de stratificatie
stoppen en meet je vooral het verschil tussen windmeters.

VEILIGHEID: de station-id's zijn net zo goed locatiegegevens als `WU_STATION_ID`
en staan daarom in het secret `WU_NEIGHBOUR_IDS` (komma-gescheiden). Ze worden
nooit gelogd — het rapport nummert ze als "buur 1", "buur 2", …
"""

from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

NIGHT_SOLAR_MAX = 1.0        # W/m² — hieronder is er geen straling om te vangen
DAY_SOLAR_MIN = 20.0
WIND_BINS: Sequence[Tuple[float, float]] = ((0, 5), (5, 10), (10, 15), (15, 100))
MIN_BIN = 15                 # minder waarnemingen dan dit → geen getal tonen


def neighbour_ids() -> List[str]:
    """Station-id's uit het secret; lege lijst als het niet gezet is."""
    raw = os.environ.get("WU_NEIGHBOUR_IDS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def pair_with_reference(station: Dict[str, dict],
                        reference: Dict[str, dict]) -> List[dict]:
    """Koppel de uurtemperaturen van één station aan het KNMI-frame.

    `station` is de vorm van `station_accuracy.fetch_wu_hourly`, `reference` die
    van `knmi_ref.fetch_hourly`. Instraling en wind komen uit de referentie —
    zie de opzet-notitie: één gemeenschappelijk frame voor alle stations.
    """
    rows = []
    for key in sorted(set(station) & set(reference)):
        temp, ref = station[key].get("temp"), reference[key].get("temp")
        if temp is None or ref is None:
            continue
        rows.append({"t": key, "bias": temp - ref,
                     "solar": reference[key].get("solar"),
                     "wind": reference[key].get("wind")})
    return rows


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def night_by_wind(rows: Sequence[dict],
                  bins: Sequence[Tuple[float, float]] = WIND_BINS) -> List[dict]:
    """Nachtelijke bias per windklasse — het profiel waar de hele vraag om draait.

    Hitte-eiland én een nastralende muur dempen allebei met wind, dus de *vorm*
    onderscheidt ze niet; het gaat erom of de buren dezelfde vorm laten zien.
    """
    night = [r for r in rows
             if r["solar"] is not None and r["solar"] <= NIGHT_SOLAR_MAX
             and r["wind"] is not None]
    out = []
    for lo, hi in bins:
        sub = [r["bias"] for r in night if lo <= r["wind"] < hi]
        out.append({"lo": lo, "hi": hi, "n": len(sub),
                    "bias": _mean(sub) if len(sub) >= MIN_BIN else None})
    return out


def solar_slope(rows: Sequence[dict]) -> Optional[float]:
    """Kleinste-kwadraten helling van de bias t.o.v. instraling, °C per W/m².

    Dit is de zon-gedreven term — de enige waarvan we zeker weten dát het een
    kapfout kan zijn. Een station dat er hier fors bovenuit steekt heeft een
    slechter geventileerde of zonniger geplaatste kap.
    """
    day = [r for r in rows if r["solar"] is not None and r["solar"] > DAY_SOLAR_MIN]
    if len(day) < MIN_BIN:
        return None
    xs = [r["solar"] for r in day]
    ys = [r["bias"] for r in day]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def profile(rows: Sequence[dict]) -> dict:
    """Samenvatting per station: n, nachtbias (totaal + per windklasse), dagbias
    en de zonhelling."""
    night = [r["bias"] for r in rows
             if r["solar"] is not None and r["solar"] <= NIGHT_SOLAR_MAX]
    day = [r["bias"] for r in rows
           if r["solar"] is not None and r["solar"] > DAY_SOLAR_MIN]
    slope = solar_slope(rows)
    return {"n": len(rows), "n_night": len(night), "n_day": len(day),
            "night_bias": _mean(night), "day_bias": _mean(day),
            "slope_per_100": slope * 100 if slope is not None else None,
            "by_wind": night_by_wind(rows)}


def verdict(ours: dict, neighbours: Sequence[dict],
            margin: float = 0.5) -> Tuple[str, str]:
    """Deelt de buurt onze warme nacht, of staan wij alleen?

    `margin` (°C) is hoeveel wij boven het buur-*maximum* moeten uitkomen voordat
    we onszelf een uitzondering noemen. Bewust het maximum en niet het gemiddelde:
    met drie buren op verschillende plekken is de spreiding groot, en één afwijkende
    buur mag ons niet vrijpleiten. Geeft (code, uitleg) — codes: `gedeeld`,
    `uitzondering`, `onbeslist`.
    """
    hun = [p["night_bias"] for p in neighbours if p["night_bias"] is not None]
    if ours.get("night_bias") is None or not hun:
        return "onbeslist", "te weinig nachtelijke waarnemingen om te vergelijken"
    hoogste, gemiddelde = max(hun), sum(hun) / len(hun)
    if ours["night_bias"] > hoogste + margin:
        return "uitzondering", (
            f"onze nachtbias {ours['night_bias']:+.2f} °C ligt {ours['night_bias'] - hoogste:.2f} °C "
            f"boven de warmste buur ({hoogste:+.2f}) — dit wijst op onze plaatsing, "
            f"niet op de buurt. Een interceptterm in wu_bias is dan verdedigbaar.")
    return "gedeeld", (
        f"onze nachtbias {ours['night_bias']:+.2f} °C ligt binnen de buurt "
        f"(gemiddeld {gemiddelde:+.2f}, warmste {hoogste:+.2f}) — dit is microklimaat, "
        f"géén sensorfout. De correctie moet een zuivere zon-term blijven.")


def spread(profiles: Sequence[dict], key: str) -> Optional[float]:
    """Spreiding (sd) over de stations op één maat — hoe eensgezind de buurt is."""
    vals = [p[key] for p in profiles if p.get(key) is not None]
    if len(vals) < 2:
        return None
    gem = sum(vals) / len(vals)
    return math.sqrt(sum((v - gem) ** 2 for v in vals) / len(vals))

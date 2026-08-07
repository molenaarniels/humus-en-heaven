"""Klopt de lucht-proxy voor bodemtemperatuur, en is 5 dagen het juiste venster?

DE AANNAME DIE HIER GETOETST WORDT
----------------------------------
`soil_model.temp_factor` is de enige plek waar bodemtemperatuur het bodemmodel
binnenkomt, en hij krijgt geen bodemtemperatuur te zien: hij draait op een
`SOIL_TEMP_WINDOW`-daags loopgemiddelde van de *lucht*-Tmean. Dat is een bewuste
demping — de bodem ijlt na op de lucht — maar of die demping de goede vorm en de
goede traagheid heeft, is nooit gemeten. Er was simpelweg niets om tegen te leggen.

Sinds aug 2026 wel: Open-Meteo levert bodemtemperatuur in dezelfde call, en het
ERA5-archief draagt 'm jaren terug. Deze module is de weegschaal.

WAT ER GEMETEN WORDT (en waarom niet alleen RMSE)
--------------------------------------------------
Een RMSE tussen proxy en bodemtemperatuur is bijzaak. `temp_factor` is een
*drempelfunctie*: boven 8 °C staat hij op 1.0 en maakt geen enkel verschil meer.
Wat telt is dus wannéér de reeks door die drempel gaat, want dát verschuift het
begin en het eind van het groeiseizoen — en daarmee het eerste maai-advies en de
dormancy-guard van Project 5. Vandaar drie maten, in volgorde van belang:

  1. `season_marks` — de datum waarop de reeks de drempel duurzaam passeert,
     in de lente omhoog en in het najaar omlaag. Het verschil in dágen tussen
     proxy en bodem is het getal waar een beslissing aan hangt.
  2. `factor_disagreement` — op hoeveel dagen per jaar `temp_factor` er materieel
     naast zit. Nul buiten de schouderseizoenen, per constructie.
  3. `fit_stats` — RMSE/bias/correlatie, als achtergrond.

BELANGRIJK VOORBEHOUD
---------------------
Open-Meteo's bodemtemperatuur is zélf een model, op gridschaal, in een
standaard-bodemkolom — niet de klei-verrijkte zandgrond onder struiken van deze
tuin. Deze meting kan dus wél zeggen of de proxy de goede *traagheid en vorm*
heeft, en niet of de absolute waarde klopt. Een uitkomst hoort dan ook te landen
in een beter vensterlengte, niet in "vervang de driver door de modelwaarde".

Zuivere functies over gewone lijsten; geen netwerk. `tools/soil_temp_probe.py`
is de runner eromheen.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from shared_const import parse_date
from soil_model import TEMP_FACTOR_FULL_C, temp_factor

# Een drempelpassage telt pas als de reeks er `PERSIST_DAYS` aaneengesloten dagen
# aan de nieuwe kant van blijft. Zonder die eis levert één zachte februaridag een
# "start van het groeiseizoen" op en meet je geklapper i.p.v. een seizoen.
PERSIST_DAYS = 5
# Verschil in temp_factor waarboven we van een materieel meningsverschil spreken.
# 0.05 op een schaal van 0..1 — kleiner dan dat verdwijnt in de afronding van Kcb.
FACTOR_TOL = 0.05


def rolling_mean(values: Sequence[Optional[float]], window: int) -> List[Optional[float]]:
    """Loopgemiddelde over de laatste `window` waarden, exact zoals
    `run_water_balance` het opbouwt: de eerste dagen middelen over minder dagen
    i.p.v. None te geven, want zo start het model ook."""
    out: List[Optional[float]] = []
    buf: List[float] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        buf.append(v)
        if len(buf) > window:
            buf.pop(0)
        out.append(sum(buf) / len(buf))
    return out


def fit_stats(pred: Sequence[Optional[float]],
              truth: Sequence[Optional[float]]) -> Optional[dict]:
    """RMSE/bias/correlatie tussen twee reeksen, None-veilig."""
    pairs = [(p, t) for p, t in zip(pred, truth) if p is not None and t is not None]
    if len(pairs) < 3:
        return None
    n = len(pairs)
    diffs = [p - t for p, t in pairs]
    bias = sum(diffs) / n
    rmse = math.sqrt(sum(d * d for d in diffs) / n)
    mp = sum(p for p, _ in pairs) / n
    mt = sum(t for _, t in pairs) / n
    sxx = sum((p - mp) ** 2 for p, _ in pairs)
    syy = sum((t - mt) ** 2 for _, t in pairs)
    sxy = sum((p - mp) * (t - mt) for p, t in pairs)
    corr = sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else None
    return {"n": n, "bias": bias, "rmse": rmse, "corr": corr}


def season_marks(dates: Sequence[str], values: Sequence[Optional[float]],
                 threshold: float = TEMP_FACTOR_FULL_C,
                 persist: int = PERSIST_DAYS) -> Dict[int, Dict[str, Optional[date]]]:
    """Per jaar de dag waarop de reeks de drempel duurzaam passeert.

    `spring` = eerste dag die ≥ drempel is én dat `persist` dagen volhoudt;
    `autumn` = eerste dag ná de zomer die < drempel is en dat volhoudt. Dit is de
    maat die er voor het maai-advies toe doet: hij zegt wanneer het groeiseizoen
    begint en eindigt, niet hoe goed twee krommes op elkaar lijken.
    """
    out: Dict[int, Dict[str, Optional[date]]] = {}
    series = [(parse_date(d), v) for d, v in zip(dates, values) if v is not None]
    for idx, (day, _) in enumerate(series):
        rest = series[idx:idx + persist]
        if idx == 0 or len(rest) < persist:
            continue
        # Een *passage* vereist een vorige dag aan de andere kant. Zonder die eis
        # meldt een archief dat toevallig in de zomer begint zijn eerste dag als
        # "start van het groeiseizoen" — geen kruising maar een startwaarde.
        vorige = series[idx - 1][1]
        year = out.setdefault(day.year, {"spring": None, "autumn": None})
        if (year["spring"] is None and day.month <= 7 and vorige < threshold
                and all(v >= threshold for _, v in rest)):
            year["spring"] = day
        if (year["autumn"] is None and day.month >= 8 and vorige >= threshold
                and all(v < threshold for _, v in rest)):
            year["autumn"] = day
    return out


def mark_shift(proxy: Dict[int, Dict[str, Optional[date]]],
               truth: Dict[int, Dict[str, Optional[date]]]) -> List[dict]:
    """Verschil in dagen per jaar en seizoen (proxy − bodem).

    Positief = de proxy loopt achter (zegt later 'lente' / later 'winter').
    """
    rows = []
    for year in sorted(set(proxy) & set(truth)):
        for season in ("spring", "autumn"):
            p, t = proxy[year].get(season), truth[year].get(season)
            if p and t:
                rows.append({"year": year, "season": season, "proxy": p,
                             "truth": t, "shift_days": (p - t).days})
    return rows


def factor_disagreement(proxy: Sequence[Optional[float]],
                        truth: Sequence[Optional[float]],
                        tol: float = FACTOR_TOL) -> dict:
    """Op hoeveel dagen wijkt de resulterende `temp_factor` materieel af?

    Dit is de vertaling naar modeltaal: twee reeksen mogen 3 °C uit elkaar liggen
    zonder één beslissing te veranderen, zolang ze allebei boven 8 °C zitten.
    """
    pairs = [(p, t) for p, t in zip(proxy, truth) if p is not None and t is not None]
    diffs = [temp_factor(p) - temp_factor(t) for p, t in pairs]
    materieel = [d for d in diffs if abs(d) > tol]
    return {"n": len(pairs), "n_disagree": len(materieel),
            "mean_abs": (sum(abs(d) for d in materieel) / len(materieel)) if materieel else 0.0,
            "max_abs": max((abs(d) for d in diffs), default=0.0),
            "proxy_hoger": sum(1 for d in materieel if d > 0),
            "proxy_lager": sum(1 for d in materieel if d < 0)}


def evaluate_windows(dates: Sequence[str], air: Sequence[Optional[float]],
                     soil: Sequence[Optional[float]],
                     windows: Sequence[int]) -> List[dict]:
    """Draai elk kandidaat-venster door alle drie de maten."""
    truth_marks = season_marks(dates, soil)
    rows = []
    for w in windows:
        proxy = rolling_mean(air, w)
        shifts = mark_shift(season_marks(dates, proxy), truth_marks)
        absolute = sorted(abs(s["shift_days"]) for s in shifts)
        rows.append({"window": w, "fit": fit_stats(proxy, soil),
                     "disagreement": factor_disagreement(proxy, soil),
                     "shifts": shifts,
                     "mean_abs_shift": (sum(absolute) / len(absolute)) if absolute else None,
                     "median_abs_shift": _median(absolute)})
    return rows


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def best_window(rows: Sequence[dict]) -> Optional[Tuple[int, float]]:
    """Het venster met de kleinste **mediane** verschuiving van de seizoensgrenzen.

    Bewust niet de laagste RMSE: boven 8 °C verandert RMSE geen enkele beslissing.
    En bewust de mediaan en niet het gemiddelde — dat laatste stond hier eerst en
    gaf een misleidend antwoord. In een zachte winter zakt de bodem op 7–28 cm
    nauwelijks onder 8 °C, waardoor "eerste duurzame passage" met maanden kan
    springen (jan 2023: bodem 4 januari, proxy 17 maart, 72 dagen verschil). Eén
    zo'n jaar domineert een gemiddelde over tien seizoensgrenzen volledig, en het
    resultaat verraadde zichzelf doordat de reeks over de vensters niet-monotoon
    werd — 7 dagen 17.1, 14 dagen 2.9, 21 dagen 5.9. Dat is ruis met een winnaar,
    geen optimum. Zelfde les als de acceptatieregel in `bias_eval`: één uitbijter
    mag geen conclusie dragen.
    """
    scored = [(r["window"], r["median_abs_shift"]) for r in rows
              if r.get("median_abs_shift") is not None]
    return min(scored, key=lambda x: x[1]) if scored else None

"""Evaluatieprotocol voor de stralingsbiascorrectie (fase 2).

WAAROM DIT BESTAAT
------------------
`SOLAR_BIAS_SLOPE` is één constante die op één seizoen is gefit, in-sample is
beoordeeld en met de hand wordt overgeschreven. Elke vervolgvraag — helpt een
windterm, schuift de helling met het seizoen, is een intercept beter — is tot nu
toe beantwoord door een fit te draaien en naar de RMSE te kijken. Dat is precies
de manier waarop je jezelf een verbetering van 1% aanpraat die op de volgende
maand omdraait: met ~60 onafhankelijke dagen en een referentie die zelf ~1,2 °C
ruis draagt, is bijna elk verschil tussen modelvormen ruis.

Deze module is het tegengif, en volgt bewust het patroon dat Project 13 al goed
doet (`rmse_naive`/`skill` in vent_learned, de A/A-ruisvloer van
`tools/vent_experiment.py`): altijd een nulmodel én het uitgerolde model ernaast,
altijd out-of-sample, en een acceptatieregel die vóór het kijken vastligt.

DE VIER POORTEN
---------------
1. **Nulmodel + uitgerold model als ondergrens.** "RMSE 1,19" zegt niets; "slaat
   geen correctie met 0,21 °C en de uitgerolde constante met 0,02 °C" wel.
2. **Forward-chaining, niet alleen k-fold.** De constante wordt één keer gefit en
   daarna maanden gebruikt. Trainen op maand 1..n en toetsen op n+1 is dus de
   eerlijke nabootsing; dag-geblokte CV is een ondergrens (opeenvolgende uren zijn
   sterk gecorreleerd, dus een willekeurige split lekt).
3. **A/A-ruisvloer.** Dezelfde modelvorm, alleen een andere willekeurige
   dag→fold-toewijzing, geeft al spreiding op de CV-uitkomst. Een winst kleiner
   dan die spreiding is geen winst.
4. **Parameterbudget.** ~60 onafhankelijke dagen dragen geen vijf vrije
   parameters. `param_budget()` rekent het voor i.p.v. het aan te voelen.

De acceptatieregel (`accept`) eist alle drie: beter op forward-chaining, beter in
*elke* fold (niet gemiddeld), en een winst boven de ruisvloer.

Zuivere functies over gewone dicts — geen netwerk, geen numpy, geen artefact.
`tools/bias_backtest.py` is de runner eromheen.
"""

from __future__ import annotations

import math
import random
from collections import OrderedDict
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from wu_bias import SOLAR_BIAS_SLOPE

# Kandidaat-modelvormen. De sleutel is de naam in het rapport, de waarde de
# featurelijst; "1" is het intercept. Bewust klein gehouden — zie param_budget().
MODELS: "OrderedDict[str, List[str]]" = OrderedDict([
    ("nul (geen correctie)", []),
    ("helling (huidige vorm)", ["solar"]),
    ("intercept + helling", ["1", "solar"]),
    ("+ wind", ["1", "solar", "wind"]),
    ("+ zon vorig uur", ["1", "solar", "prev_solar"]),
    ("+ zon x bewolking", ["1", "solar", "solar_cloud"]),
    ("alles", ["1", "solar", "wind", "prev_solar", "solar_cloud"]),
])

DEPLOYED = "uitgerold (SOLAR_BIAS_SLOPE)"
DAY_SOLAR_MIN = 20.0     # W/m² — grens tussen "nacht" en "daguur"
PARAM_DAYS_PER_FREE = 20  # vuistregel: ≥20 onafhankelijke dagen per vrije parameter


# ── Voorbereiding ─────────────────────────────────────────────────────────────

def prepare(rows: Iterable[dict], reference: str = "om",
            driver: str = "solar") -> List[dict]:
    """Archiefrijen → evaluatierijen met `bias`, de driver en afgeleide features.

    `reference` is de kolom waartegen de bias gemeten wordt (`om` = ERA5,
    `knmi` zodra die er is) en `driver` de instralingskolom (`solar` = het
    Open-Meteo-grid, `wu_solar` = de eigen pyranometer, de as waarop de
    correctie in productie draait). Rijen zonder alle drie vallen af — beter een
    kleinere, eerlijke steekproef dan geïmputeerde nullen in de fit.

    `prev_solar` (instraling van het vórige klokuur) is de traagheidsproxy: is de
    stralingskap traag, dan hangt de bias óók aan het uur ervoor. Alleen gevuld
    als dat uur echt aansluit, anders valt de rij uit de modellen die 'm gebruiken.
    """
    out: List[dict] = []
    prev: Optional[dict] = None
    for row in sorted(rows, key=lambda r: r["t"]):
        wu, ref, sol = row.get("wu"), row.get(reference), row.get(driver)
        if wu is None or ref is None or sol is None:
            prev = None
            continue
        item = {
            "t": row["t"], "day": row["t"][:10], "month": row["t"][:7],
            "bias": wu - ref, "solar": sol, "1": 1.0,
            "wind": row.get("wind"), "cloud": row.get("cloud"),
            "prev_solar": prev["solar"] if prev and _adjacent(prev["t"], row["t"]) else None,
        }
        item["solar_cloud"] = (sol * item["cloud"] / 100.0) if item["cloud"] is not None else None
        out.append(item)
        prev = item
    return out


def _adjacent(earlier: str, later: str) -> bool:
    """Sluit `later` direct aan op `earlier` (één klokuur, zelfde dag of dagrand)?"""
    from datetime import datetime, timedelta
    fmt = "%Y-%m-%dT%H"
    return datetime.strptime(earlier, fmt) + timedelta(hours=1) == datetime.strptime(later, fmt)


def usable(rows: Sequence[dict], features: Sequence[str]) -> List[dict]:
    """Rijen waarin elke feature van dit model aanwezig is."""
    return [r for r in rows if all(r.get(f) is not None for f in features)]


def daytime(rows: Sequence[dict]) -> List[dict]:
    return [r for r in rows if r["solar"] > DAY_SOLAR_MIN]


# ── Fit + score ───────────────────────────────────────────────────────────────

def fit(rows: Sequence[dict], features: Sequence[str]) -> List[float]:
    """Kleinste kwadraten via de normaalvergelijkingen (Gauss met partiële pivot).

    Handgeschreven i.p.v. numpy: dit draait in CI en in de Actions-runner, waar
    numpy bewust geen dependency is (zie de grondregels). Bij ≤5 parameters is
    dat prima; groter dan dat hoort dit model sowieso niet te worden.
    """
    p = len(features)
    if p == 0:
        return []
    M = [[sum(r[a] * r[b] for r in rows) for b in features] +
         [sum(r[a] * r["bias"] for r in rows)] for a in features]
    for c in range(p):
        piv = max(range(c, p), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        if abs(M[c][c]) < 1e-12:
            M[c][c] = 1e-12          # singulier (bv. een constante kolom) → geen deling door 0
        for r in range(p):
            if r == c:
                continue
            f = M[r][c] / M[c][c]
            for k in range(c, p + 1):
                M[r][k] -= f * M[c][k]
    return [M[i][p] / M[i][i] for i in range(p)]


def predict(row: dict, features: Sequence[str], coef: Sequence[float]) -> float:
    return sum(c * row[f] for c, f in zip(coef, features))


def rmse(rows: Sequence[dict], predictor: Callable[[dict], float]) -> Optional[float]:
    if not rows:
        return None
    return math.sqrt(sum((r["bias"] - predictor(r)) ** 2 for r in rows) / len(rows))


def deployed_predictor(row: dict) -> float:
    """Het model zoals het nú in productie draait: een vaste helling door de
    oorsprong. Bewust *niet* meegefit — dit is de lat, geen kandidaat."""
    return SOLAR_BIAS_SLOPE * max(0.0, row["solar"])


def skill(candidate: Optional[float], baseline: Optional[float]) -> Optional[float]:
    """1 − RMSE/baseline: >0 is beter dan de baseline, <0 slechter."""
    if candidate is None or not baseline:
        return None
    return 1.0 - candidate / baseline


# ── Validatieschema's ─────────────────────────────────────────────────────────

def forward_chain(rows: Sequence[dict], features: Sequence[str],
                  min_train_months: int = 1) -> List[Tuple[str, float, int]]:
    """Train op maand 1..n, toets op n+1. Geeft [(maand, rmse, n_test)].

    Dit is hoe de constante daadwerkelijk gebruikt wordt: één keer gefit, daarna
    maanden vooruit toegepast. Een modelvorm die hier niet wint, wint niet.
    """
    months = sorted({r["month"] for r in rows})
    out = []
    for i in range(min_train_months, len(months)):
        train = [r for r in rows if r["month"] < months[i]]
        test = [r for r in rows if r["month"] == months[i]]
        if not train or not test:
            continue
        coef = fit(train, features)
        score = rmse(test, lambda r, c=coef: predict(r, features, c))
        out.append((months[i], score, len(test)))
    return out


def day_blocked_cv(rows: Sequence[dict], features: Sequence[str],
                   k: int = 6, seed: int = 0) -> Optional[float]:
    """k-fold met hele dagen in of uit — opeenvolgende uren zijn te sterk
    gecorreleerd voor een willekeurige rij-split (dat lekt de testset in)."""
    days = sorted({r["day"] for r in rows})
    if len(days) < k:
        return None
    rng = random.Random(seed)
    shuffled = days[:]
    rng.shuffle(shuffled)
    folds = [set(shuffled[i::k]) for i in range(k)]
    total, n = 0.0, 0
    for test_days in folds:
        train = [r for r in rows if r["day"] not in test_days]
        test = [r for r in rows if r["day"] in test_days]
        if not train or not test:
            continue
        coef = fit(train, features)
        score = rmse(test, lambda r, c=coef: predict(r, features, c))
        total += score * score * len(test)
        n += len(test)
    return math.sqrt(total / n) if n else None


def aa_floor(rows: Sequence[dict], features: Sequence[str],
             k: int = 6, seeds: Sequence[int] = (1, 2, 3, 4, 5)) -> Optional[float]:
    """Ruisvloer: spreiding van de CV-uitkomst bij *dezelfde* modelvorm en alleen
    een andere dag→fold-toewijzing. Alles onder deze marge is meetruis, geen
    verbetering. Zelfde rol als de A/A-run in `tools/vent_experiment.py`."""
    scores = [s for s in (day_blocked_cv(rows, features, k=k, seed=s) for s in seeds)
              if s is not None]
    return max(scores) - min(scores) if len(scores) >= 2 else None


def param_budget(rows: Sequence[dict], features: Sequence[str]) -> Tuple[int, int, bool]:
    """(vrije parameters, onafhankelijke dagen, past het). Uren zijn geen
    onafhankelijke waarnemingen — dagen benaderen dat veel beter."""
    p = len(features)
    days = len({r["day"] for r in rows})
    return p, days, p == 0 or days >= p * PARAM_DAYS_PER_FREE


# ── Acceptatieregel (vóór het kijken vastgelegd) ──────────────────────────────

def accept(candidate_folds: Sequence[Tuple[str, float, int]],
           incumbent_folds: Sequence[Tuple[str, float, int]],
           floor: Optional[float]) -> Tuple[bool, List[str]]:
    """Mag deze kandidaat de uitgerolde vorm vervangen?

    Drie eisen, alle drie verplicht:
      1. beter op forward-chaining (gewogen over de folds);
      2. beter in *elke* fold — een gemiddelde verbetering die uit één maand komt
         is een seizoenstoevalligheid, niet een betere modelvorm;
      3. de winst is groter dan de A/A-ruisvloer.
    Geeft (oordeel, redenen) — de redenen zijn de rapportageregels.
    """
    reasons: List[str] = []
    if not candidate_folds or not incumbent_folds:
        return False, ["geen forward-chaining-folds (te weinig maanden in het archief)"]
    paired = [(c, i) for c, i in zip(candidate_folds, incumbent_folds) if c[0] == i[0]]
    if not paired:
        return False, ["kandidaat en lat delen geen enkele testmaand"]

    def weighted(folds):
        n = sum(f[2] for f in folds)
        return math.sqrt(sum(f[1] ** 2 * f[2] for f in folds) / n)

    cand, inc = weighted([c for c, _ in paired]), weighted([i for _, i in paired])
    gain = inc - cand
    if gain <= 0:
        reasons.append(f"niet beter op forward-chaining ({cand:.3f} vs {inc:.3f})")
    verloren = [c[0] for c, i in paired if c[1] >= i[1]]
    if verloren:
        reasons.append(f"verliest in {len(verloren)}/{len(paired)} folds ({', '.join(verloren)})")
    if floor is not None and gain <= floor:
        reasons.append(f"winst {gain:+.3f} °C ligt binnen de ruisvloer ({floor:.3f} °C)")
    return (not reasons), reasons


def evaluate(rows: Sequence[dict], models: "OrderedDict[str, List[str]]" = MODELS,
             k: int = 6) -> List[dict]:
    """Draai alle modellen door alle poorten. Geeft één dict per model."""
    incumbent = [(m, rmse([r for r in rows if r["month"] == m], deployed_predictor), n)
                 for m, _, n in forward_chain(rows, ["solar"])]
    null_rmse = rmse(rows, lambda r: 0.0)
    results = [{
        "naam": DEPLOYED, "features": ["(vast)"], "n": len(rows),
        "cv": rmse(rows, deployed_predictor), "aa": None,
        "forward": incumbent, "skill_nul": skill(rmse(rows, deployed_predictor), null_rmse),
        "budget": (0, len({r["day"] for r in rows}), True), "oordeel": (None, []),
    }]
    for naam, features in models.items():
        sub = usable(rows, features)
        if not sub:
            continue
        cv = day_blocked_cv(sub, features, k=k) if features else rmse(sub, lambda r: 0.0)
        folds = forward_chain(sub, features) if features else []
        floor = aa_floor(sub, features, k=k) if features else None
        results.append({
            "naam": naam, "features": list(features), "n": len(sub),
            "cv": cv, "aa": floor, "forward": folds,
            "skill_nul": skill(cv, rmse(sub, lambda r: 0.0)),
            "budget": param_budget(sub, features),
            "oordeel": accept(folds, incumbent, floor) if features else (None, []),
        })
    return results

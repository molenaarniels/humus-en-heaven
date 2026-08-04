#!/usr/bin/env python3
"""
vent_fit.py — het leren van de ventilatie-tweeling (Project 13).

Eén eenvoudig online-regime, geport uit airflow_model.py: per run een gedempte
Gauss-Newton-stap (calibrate) op de residuen van het 48u-venster, met Huber-
gewichten, recency-weging en een Tikhonov-ridge richting de priors — en verder
NIETS. Het checkpoint/fallback-complex, de batch-verankering en de leercurve-
backfill van de voorgangers zijn bewust niet meegekomen: elk groot incident
(de fallback-lus ×2, de curve-churn, het 56-uurs vastgelopen anker, de
tarrering) kwam uit die vangnetten, niet uit de fysica — zie
AIRFLOW_ASSESSMENT.md / AIRFLOW2_ASSESSMENT.md. Offline validatie loopt via
tools/vent_experiment.py op de maand-shards.

De anomalie-poort is er nog wél (log↔werkelijkheid-mismatch → leren pauzeren +
Telegram-nudge), maar deadlock-proof herbouwd als `anomaly_step`: een hold
duurt maximaal ANOMALY_MAX_HOLD_H, daarna hervat het leren met een cooloff —
de zelfvoedende hold die tweeling 2 bij zijn pensionering bevroor (de baseline
sloot held-punten uit, dus de poort kon zichzelf nooit meer openen) kan niet
terugkomen.
"""

import json
import math
import time
from datetime import datetime, timedelta

from vent_io import CALIB_WINDOW_H, _timedelta_h, ac_room_at
from vent_physics import (
    BOUNDS, GLOBAL_PARAMS, PER_ROOM_PARAMS, PRIORS, RAIL_TOL, RunContext,
    _interp, _to_sensor_series, clamp_param, param_bounds, simulate, solve_linear,
)

RMSE_HISTORY_KEEP = 1000  # hard vangnet op het aantal leercurve-punten (na uitdunning ~384)

RMSE_HISTORY_DAYS = 10.0  # leeftijdslimiet van de leercurve. De uitdunning (thin_rmse_history)

LEARN_RATE     = 0.5     # online: schuif deze fractie naar het nieuwe optimum per run

# Tikhonov-regularisatie: trek elke leerbare schaal zacht naar zijn prior (1.0, cp_shelter 0.5)
# zodat zwak-identificeerbare parameters (milde-weer-degeneratie tussen ua_env/ua_party/q_int)
# naar hun prior teruggetrokken worden i.p.v. naar hun grens te driften. Levenberg-λ dempt
# alleen de stapgrootte, niet de absolute drift; deze ridge wel. Klein t.o.v. de data-Hessiaan
# zodat sterk-bepaalde parameters (grote ∂fout/∂param) vrij blijven en alleen platte richtingen
# naar de prior gaan.
REG_WEIGHT     = 3.0

# Per-parameter ridge-override (sterker dan REG_WEIGHT). `solar_gain` wil op een mild/bewolkt
# venster (of bij een iets te ruim ingeschatte zon-magnitude) naar ~0 zakken — dan kan de twin
# géén zonwinst meer voorstellen, juist op de hete zonnige dagen waarvoor hij bedoeld is. Een
# sterkere ankering naar de prior (1.0) houdt 'm overeind tenzij de data écht het tegendeel
# bewijst; alle andere parameters houden REG_WEIGHT.
REG_WEIGHT_BY_PARAM = {"solar_gain": 6.0}

# Regime-bewuste ridge voor `solar_gain`. Het extra-sterke anker (6.0) is bedoeld voor MILDE /
# BEWOLKTE vensters waar de zon-magnitude onidentificeerbaar is en de fit 'm anders naar ~0 laat
# zakken ("zon-uit"). Op een HETE ZONNIGE dag is dat juist averechts: dan bewíjst de data de
# zon-magnitude (afternoon-piek-residu, RMSE↔dag-max-correlatie ~0.7) en wil ze solar_gain lager,
# maar dezelfde 6.0-ridge trekt 'm weer naar de prior (1.0) → een te-warme compromiswaarde, juist
# op de dag waarvoor de twin bestaat. Daarom ramp het anker terug van 6.0 → REG_WEIGHT zodra het
# venster-zon-gemiddelde van LOW → HIGH loopt: anti-collapse-bescherming blijft op bewolkte
# vensters, en op zonnige vensters wordt de data vertrouwd. Géén effect als solar_mean onbekend is
# (backwards-compatibel: dan geldt de oude 6.0).
SOLAR_RIDGE_LOW_WM2  = 150.0  # ≤ dit zon-gemiddelde (W/m²): vol anker (onidentificeerbaar)

SOLAR_RIDGE_HIGH_WM2 = 300.0  # ≥ dit: terug naar REG_WEIGHT (zon-magnitude identificeerbaar)

# Recency-weging van de residuen in de fit. Het 48u-kalibratievenster (CALIB_WINDOW_H) kan een
# REGIME-WISSEL overspannen (mild → hittegolf); een ongewogen kleinste-kwadraten-fit middelt dan
# twee regimes en het compromis overschat het hete eind. Een exponentieel tijdsgewicht laat het
# HUIDIGE regime de fit-richting domineren zónder het venster te verkorten (trage termen
# ua_party/c_mass blijven identificeerbaar over de volle 48u). Raakt alléén de fit-richting, NIET
# de gerapporteerde RMSE/skill (die blijft de ongewogen fout, vergelijkbaar over de leercurve).
RECENCY_HALFLIFE_H = 18.0  # uur: een sample van deze leeftijd weegt half t.o.v. het nieuwste

def reg_weight(name: str, solar_mean: float | None = None) -> float:
    """Ridge-gewicht voor parameter `name`: de per-parameter-override of de globale REG_WEIGHT.
    Voor `solar_gain` ramp het extra anker (6.0) regime-bewust terug naar REG_WEIGHT naarmate het
    venster-zon-gemiddelde `solar_mean` (W/m²) van SOLAR_RIDGE_LOW → HIGH loopt — sterk anker bij
    weinig zon (anti-collapse), zwak anker bij veel zon (vertrouw de data). `solar_mean=None` →
    ongewijzigd gedrag."""
    w = REG_WEIGHT_BY_PARAM.get(name, REG_WEIGHT)
    if name == "solar_gain" and solar_mean is not None and w > REG_WEIGHT:
        lo, hi = SOLAR_RIDGE_LOW_WM2, SOLAR_RIDGE_HIGH_WM2
        frac = 0.0 if hi <= lo else min(1.0, max(0.0, (solar_mean - lo) / (hi - lo)))
        w += frac * (REG_WEIGHT - w)   # 6.0 (frac=0) → REG_WEIGHT (frac=1)
    return w

def railed_params(params: dict, tol: float = RAIL_TOL,
                  house: dict | None = None) -> list[str]:
    """Welke geleerde params op (≈) hun grens zitten — de 'saturatie-tell' dat een fysisch
    kanaal naar zijn extreem geduwd wordt (vaak een structureel tekort of een te-warme prior).
    Geeft een lijst `'scope.param@floor|ceil'` voor logging/dashboard. Massaal floor-railen op
    de warmte-in-kanalen = de optimizer komt niet koel genoeg. Puur diagnostisch, geen mutatie.

    `house` (optioneel) laat de per-kamer versmalde band meetellen (zie `param_bounds`), zodat
    een parameter die op zijn HUISMODEL-vloer zit óók zichtbaar wordt i.p.v. ruim binnen de
    globale `BOUNDS` te lijken. Zo'n grens krijgt het achtervoegsel `(model)` —
    `living.c_mass@floor(model)` — want hij betekent iets ánders: een `BOUNDS`-rail is een
    saturatie-KLACHT (de fysica wil ergens heen waar ze niet mag), een huismodel-rail is de
    constraint die dóét waarvoor hij is aangebracht. `is_model_rail` scheidt de twee voor
    poorten die alleen op het eerste mogen afgaan (tools/vent_seed.py)."""
    out = []

    def _flag(scope: str, name: str, value) -> None:
        if name not in BOUNDS or not isinstance(value, (int, float)):
            return
        lo, hi = param_bounds(house, scope, name)
        glo, ghi = BOUNDS[name]
        rng = hi - lo
        if rng <= 0:
            return
        if value - lo <= tol * rng:
            out.append(f"{scope}.{name}@floor" + ("" if lo <= glo else "(model)"))
        elif hi - value <= tol * rng:
            out.append(f"{scope}.{name}@ceil" + ("" if hi >= ghi else "(model)"))

    for name in GLOBAL_PARAMS:
        if name in params:
            _flag("global", name, params[name])
    for rid, rp in params.items():
        if not isinstance(rp, dict):
            continue
        for name in PER_ROOM_PARAMS:
            if name in rp:
                _flag(rid, name, rp[name])
    return out

def is_model_rail(flag: str) -> bool:
    """Komt deze `railed_params`-vlag van een bewuste huismodel-grens (`param_bounds`)?"""
    return flag.endswith("(model)")

# ── Robuust leren: bescherming tegen niet-gerapporteerde raamwijzigingen ─────────────
# Als de werkelijke openingen afwijken van de gerapporteerde log (b.v. iemand zet thuis
# een raam open terwijl jij weg bent), wordt de voorspelfout anomaal hoog: het model
# verklaart de werkelijkheid dan met de verkeerde aanname en zou de fysica-parameters
# scheeftrekken. Dan PAUZEREN we het leren (parameters vasthouden) — we voorspellen nog
# wél, zodat de divergentie zichtbaar blijft, maar leren niet van een venster waarvan de
# log waarschijnlijk niet klopt. Het venster rolt vanzelf weg (CALIB_WINDOW_H), dus zodra
# de log weer klopt herstelt het leren. Binnen een venster dempt een Huber-verlies losse
# uitschieters (een enkele rare sensor-sample domineert de fit niet).
ANOMALY_MIN_HISTORY = 12    # zoveel eerdere RMSE-punten nodig vóór we een norm vertrouwen

ANOMALY_FACTOR      = 2.2   # fout > dit × de mediane recente RMSE → leren pauzeren

ANOMALY_FLOOR       = 1.5   # °C — pauzeer nooit op kleine ruis; pas boven deze absolute fout

ANOMALY_BASE_N      = 48    # mediaan over de laatste zoveel RMSE-punten = de norm

HUBER_DELTA         = 1.5   # °C — residuen hierboven worden lineair (i.p.v. kwadratisch) gewogen

# De anomalie-poort pauzeerde het leren wél, maar niemand hoorde het: de dashboard-banner is
# er alleen voor wie toevallig kijkt, dus een niet-gemelde raamwijziging kon dágen fout blijven
# staan terwijl de tweeling op de verkeerde aanname doorvoorspelde (assessment 10 juli 2026,
# besluit gebruiker: nudge toegevoegd). Daarom gaat er bij een anomalie-pauze een Telegram-nudge
# naar de privé-chat ("klopt de raamstand nog?"). Cooldown-gedrag: één nudge per episode-start,
# hooguit elke ANOMALY_NUDGE_COOLDOWN_H herhaald zolang de anomalie aanhoudt; herstelt het leren,
# dan wordt de stempel gewist zodat een vólgende episode meteen weer nudget. De handmatige
# huis-brede pauze nudget bewust níét (die is zelf gekozen — je weet het al). Stempel
# De nudge-stempel leeft als `anomaly.nudged_at` in vent_learned.json (de artefact-commit is de state).
ANOMALY_NUDGE_COOLDOWN_H = 6.0

def should_nudge_anomaly(last_nudge_iso: str | None, now: datetime) -> bool:
    """Mag er nu een anomalie-nudge uit? True bij geen (of onleesbare) eerdere stempel,
    of wanneer de cooldown verstreken is. Puur — de aanroeper beslist óf er een anomalie is."""
    if not last_nudge_iso:
        return True
    try:
        last = datetime.fromisoformat(last_nudge_iso)
    except (ValueError, TypeError):
        return True
    return (now - last) >= timedelta(hours=ANOMALY_NUDGE_COOLDOWN_H)

def learning_baseline(history: list[dict]) -> float | None:
    """Mediane recente kalibratie-RMSE als 'normale' fout. Alléén écht-geleerde runs tellen
    mee — gepauzeerde (held) runs worden genegeerd, anders zou een langdurige anomalie de
    norm langzaam optrekken en zichzelf un-gaten. None bij te weinig historie."""
    vals = [h.get("rmse") for h in history
            if h.get("rmse") is not None and not h.get("held")
            and h.get("rmse") == h.get("rmse")]
    if len(vals) < ANOMALY_MIN_HISTORY:
        return None
    recent = sorted(vals[-ANOMALY_BASE_N:])
    n = len(recent)
    return recent[n // 2] if n % 2 else 0.5 * (recent[n // 2 - 1] + recent[n // 2])

# De ontsnappingsklep op de anomalie-poort. De oude poort (should_hold_learning) was
# zelfvoedend: learning_baseline negeert held-punten (terecht — anders trekt een anomalie
# de norm zelf omhoog), maar daardoor kon een stápsprong in de RMSE (revisie-reset,
# sensorverplaatsing) de hold voor onbepaalde tijd verankeren: er kwamen nooit meer
# niet-held-punten bij om de norm te laten meebewegen. Tweeling 2 stond er bij zijn
# pensionering precies zo bij. Nu: een hold duurt maximaal ANOMALY_MAX_HOLD_H, daarna
# hervat het leren en is de poort ANOMALY_REARM_H ontwapend (cooloff) zodat de baseline
# ~96 kwartier-punten krijgt om het nieuwe regime te absorberen. Slechtste geval = het
# gedrag van vóór de poort (door de mismatch heen leren), na een beschermende pauze.
ANOMALY_MAX_HOLD_H = 24.0   # maximale aaneengesloten hold-duur
ANOMALY_REARM_H = 24.0      # cooloff ná een ontsnapping: poort ontwapend, baseline leert bij


def _iso_or_none(v):
    try:
        return datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def anomaly_step(rmse_cur: float | None, history: list[dict], anomaly_state: dict | None,
                 now: datetime, physics_migrated: bool = False) -> tuple[bool, float | None, dict]:
    """Eén stap van de anomalie-poort. Geeft (hold, baseline, nieuwe_state) terug; de state
    ({held_since, cooloff_until, nudged_at, escaped_at}, ISO-strings) wordt door de runner in
    vent_learned.json gepersisteerd. `hold=True` → deze run niet leren (wel simuleren/tonen).

    - Revisie-migratie: nooit holden én meteen een cooloff — de reset-sprong is verwacht,
      erdoorheen leren is het doel (her-seeden via tools/vent_seed.py maakt 'm klein).
    - Escape valve: een hold ouder dan ANOMALY_MAX_HOLD_H eindigt zichzelf (escaped_at wordt
      gestempeld voor de "leren hervat"-nudge-variant) en ontwapent de poort tijdelijk.
    - Normale run: episode-administratie wordt gewist zodat een volgende anomalie weer
      meteen mag nudgen."""
    st = dict(anomaly_state or {})
    st.pop("escaped_at", None)   # transient: alleen de run waarin de klep vuurt draagt 'm
    baseline = learning_baseline(history)

    if physics_migrated:
        st["held_since"] = None
        st["cooloff_until"] = (now + timedelta(hours=ANOMALY_REARM_H)).isoformat()
        return False, baseline, st

    cooloff = _iso_or_none(st.get("cooloff_until"))
    if cooloff is not None and now < cooloff:
        st["held_since"] = None
        return False, baseline, st

    anomalous = (baseline is not None and rmse_cur is not None and rmse_cur == rmse_cur
                 and rmse_cur > max(ANOMALY_FLOOR, baseline * ANOMALY_FACTOR))
    if not anomalous:
        st["held_since"] = None
        st["nudged_at"] = None
        if cooloff is not None and now >= cooloff:
            st["cooloff_until"] = None
        return False, baseline, st

    held_since = _iso_or_none(st.get("held_since")) or now
    st["held_since"] = held_since.isoformat()
    if now - held_since < timedelta(hours=ANOMALY_MAX_HOLD_H):
        return True, baseline, st
    # Escape valve: lang genoeg gepauzeerd — hervat het leren en ontwapen de poort.
    st["held_since"] = None
    st["cooloff_until"] = (now + timedelta(hours=ANOMALY_REARM_H)).isoformat()
    st["escaped_at"] = now.isoformat()
    return False, baseline, st


def _huber_weights(residuals: list[float], delta: float = HUBER_DELTA) -> list[float]:
    """Huber-gewicht per residu: 1 binnen ±delta, daarbuiten delta/|r| (<1) zodat een
    uitschieter de kleinste-kwadraten-fit niet domineert."""
    return [1.0 if abs(r) <= delta else delta / abs(r) for r in residuals]

# De AC-exclusie was retroactief-only: ze liet alleen samples vallen waarvan de log op dát moment
# `ac_room==kamer` zei. Meld je de airco "staat nu hier" zónder terug te dateren, dan bleef de
# koeling die al vóór de melding liep ín de fit (de AC-kamer leest dan kouder dan de fysica kan en
# trekt zowel haar eigen params als de gedeelde globalen scheef). Daarom laten we, zodra een kamer
# nú de airco heeft, óók haar samples van de laatste AC_GUARD_H uur vallen — ongeacht de log-timing.
AC_GUARD_H = 6.0

def filter_ac_samples(actual: dict, acc: list[tuple], ac_room_now: str | None,
                      now: datetime, guard_h: float = AC_GUARD_H) -> tuple[dict, dict]:
    """Laat de tado-samples van de AC-kamer uit `actual` vallen — het model heeft géén actieve-
    koel-term, dus die kamer leest kouder dan de fysica kan en zou de fit (kamer-params + gedeelde
    globalen + RMSE/leercurve) vervuilen. Een sample valt weg als (a) de log op dát moment
    `ac_room==kamer` zei, óf (b) de kamer NÚ de airco heeft en het sample binnen het laatste
    `guard_h`-uur ligt — die guard vangt een 'staat nu hier'-melding die niet teruggedateerd is
    (de koeling liep al vóór de melding). Geeft (gefilterde actual-kopie, {kamer: #weggelaten}).
    Lege `acc` → ongewijzigd. De AC-kamer wordt nog wél gesimuleerd + getoond (seed uit seed_src)."""
    if not acc:
        return actual, {}
    guard_since = now - _timedelta_h(guard_h) if guard_h and guard_h > 0 else None
    out: dict = {}
    excluded: dict = {}
    for rid, samples in actual.items():
        kept = [(ts, v) for ts, v in samples
                if not (ac_room_at(acc, ts) == rid
                        or (ac_room_now == rid and guard_since is not None and ts >= guard_since))]
        dropped = len(samples) - len(kept)
        if dropped:
            excluded[rid] = dropped
        if kept:
            out[rid] = kept
    return out, excluded

def filter_heating_samples(actual: dict, heat_on: dict[str, set]) -> tuple[dict, dict]:
    """Laat per kamer de tado-samples vallen waarop er volgens de log gestookt werd. Geeft
    (gefilterde actual-kopie, {kamer: #weggelaten}). Leeg `heat_on` → ongewijzigd. Een kamer
    die volledig wegvalt (heel het venster gestookt) verdwijnt uit `actual` (niet gekalibreerd),
    net als de airco-kamer — ze wordt nog wél gesimuleerd + getoond (seed uit seed_src)."""
    if not heat_on:
        return actual, {}
    out: dict = {}
    excluded: dict = {}
    for rid, samples in actual.items():
        on = heat_on.get(rid)
        if not on:
            out[rid] = samples
            continue
        kept = [(ts, v) for ts, v in samples if ts not in on]
        dropped = len(samples) - len(kept)
        if dropped:
            excluded[rid] = dropped
        if kept:
            out[rid] = kept
    return out, excluded

def filter_paused_samples(actual: dict, intervals: list[tuple]) -> tuple[dict, dict]:
    """Laat, huis-breed, de tado-samples vallen die binnen een gepauzeerd interval liggen —
    niemand betrouwbaars meldt dan de raam/rooster/deur-stand, dus fitten daarop zou een
    onbekende open/dicht-aanname in de kalibratie brengen. Anders dan filter_ac_samples/
    filter_heating_samples (per kamer) raakt dit ALLE kamers evenveel. Geeft (gefilterde
    actual-kopie, {kamer: #weggelaten}) — zelfde vorm als de AC/verwarmings-filters, voor
    schema-consistentie. Lege `intervals` → ongewijzigd (backwards-compatibel). Een kamer wier
    hele venster binnen een pauze valt, verdwijnt uit `actual` (net als bij AC/verwarming) — ze
    wordt nog wél gesimuleerd + getoond (seed uit seed_src)."""
    if not intervals:
        return actual, {}
    out: dict = {}
    excluded: dict = {}
    for rid, samples in actual.items():
        kept = [(ts, v) for ts, v in samples
                if not any(start <= ts <= end for start, end in intervals)]
        dropped = len(samples) - len(kept)
        if dropped:
            excluded[rid] = dropped
        if kept:
            out[rid] = kept
    return out, excluded

def coupled_sensorless_zones(house: dict, rooms: list[str]) -> list[str]:
    """Zones zónder eigen meting die wél via een deur aan ≥1 gemeten kamer hangen.

    De trap is zo'n zone: geen tado-sensor, dus hij viel buiten de parametervector en zijn
    params bevroren voor altijd op de priors — inclusief een kaal-dak-`ROOF_U` op 10 m²,
    goed voor een fantoom-verwarming van honderden watts pal boven de twee slechtst
    voorspelde kamers. Onmeetbaar is niet hetzelfde als ononderscheidbaar: de schacht is
    sterk gekoppeld aan office, hotties en ted, dus zijn parameters zijn wel degelijk
    identificeerbaar dóór die kamers heen. Ze meeleren is beter dan ze op een gok vastzetten;
    de zwaardere ridge (zie `reg_weight`) houdt de zwak-bepaalde richtingen bij hun prior."""
    measured = set(rooms)
    if not measured:
        return []
    out = []
    # Alléén `rooms`: junctions (de gang) krijgen in `_zone_thermal_params` generieke vaste
    # waarden en lezen hun params niet, dus die meeleren zou een nul-gradiënt-richting aan de
    # vector toevoegen — kosten zonder informatie.
    for zid in house.get("rooms", {}):
        if zid in measured:
            continue
        for d in house.get("doors", {}).values():
            pair = d.get("between") or []
            if len(pair) == 2 and zid in pair and (set(pair) - {zid}) & measured:
                out.append(zid)
                break
    return out

# Zwaardere ridge voor de meeleerde sensorloze zones: hun params zijn alléén indirect (via de
# gekoppelde kamers) bepaald, dus de zwak-geïnformeerde richtingen moeten steviger naar hun
# prior getrokken worden dan bij een kamer met eigen meetreeks.
REG_WEIGHT_SENSORLESS = 3.0 * REG_WEIGHT

def _param_keys(rooms: list[str], sensorless: list[str] | None = None) -> list[tuple]:
    keys = [("global", g) for g in GLOBAL_PARAMS]
    for rid in list(rooms) + list(sensorless or []):
        for p in PER_ROOM_PARAMS:
            keys.append((rid, p))
    return keys

def params_to_vec(params: dict, keys: list[tuple]) -> list[float]:
    out = []
    for scope, name in keys:
        out.append(params[name] if scope == "global" else params[scope][name])
    return out

def vec_to_params(vec: list[float], keys: list[tuple], base: dict,
                  house: dict | None = None) -> dict:
    p = json.loads(json.dumps(base))   # diepe kopie
    for v, (scope, name) in zip(vec, keys):
        v = clamp_param(house, scope, name, v)
        if scope == "global":
            p[name] = v
        else:
            p.setdefault(scope, {})[name] = v
    return p

def _clamp_to_bounds(vec: list[float], keys: list[tuple],
                     house: dict | None = None) -> list[float]:
    """Klem een solver-state-vector op de BOUNDS van zijn parameters (incl. de per-kamer
    versmalling uit house_model.json — zie `param_bounds`). vec_to_params projecteert al bij
    élke evaluatie, maar de Gauss-Newton-iterate zelf kon buiten de band accumuleren — dan
    rekent de ridge-anker-term (x−prior) op een fantoompunt dat nooit geëvalueerd wordt."""
    return [clamp_param(house, scope, name, v) for v, (scope, name) in zip(vec, keys)]

def _residuals_timed(house, params, timeline, seed, ctx: RunContext, actual, rooms_set,
                     tm_seed=None, measured=None) -> list[tuple]:
    """(meetmoment, voorspeld−werkelijk) op elk sample — als `_residuals` maar mét de tijdstempel
    behouden zodat de fit een recency-weging kan toepassen. Voorspelling eerst naar sensor-ruimte
    (buitenmuur-bias) zodat tegen de werkelijk gemeten — gebiasde — tado-temp vergeleken wordt.
    `tm_seed` (optioneel): per-zone massaknoop-beginwaarde (bv. het venster-gemiddelde van de
    gemeten luchttemp) i.p.v. de warme buur-blend — zie simulate().
    `measured` (optioneel): regressie-basis voor de koker-gradiënt, doorgegeven aan simulate()."""
    sim = simulate(house, params, timeline, seed, ctx, calib_only_rooms=rooms_set,
                   tm_seed=tm_seed, measured=measured)
    out = []
    for rid, samples in actual.items():
        pred = sim["series"].get(rid, [])
        if not pred:
            continue
        pred = _to_sensor_series(house, timeline, rid, pred)
        for ts, val in samples:
            out.append((ts, _interp(pred, ts) - val))
    return out

def _residuals(house, params, timeline, seed, ctx: RunContext, actual, rooms_set,
               tm_seed=None, measured=None) -> list[float]:
    """Voorspeld − werkelijk op elk meetmoment (lineair geïnterpoleerd op de
    voorspelde reeks). De voorspelling wordt eerst naar sensor-ruimte gemapt (buitenmuur-
    bias) zodat de fit tegen de werkelijk gemeten — gebiasde — tado-temp vergelijkt.
    `tm_seed` wordt doorgegeven aan simulate() (massaknoop-beginwaarde)."""
    return [r for _, r in _residuals_timed(house, params, timeline, seed, ctx, actual, rooms_set,
                                           tm_seed=tm_seed, measured=measured)]

def _recency_weights(times: list, half_life_h: float | None = None) -> list[float]:
    """Exponentieel tijdsgewicht per sample: 1.0 voor het nieuwste, halverend per `half_life_h`
    oudere uren — `2^(−Δt/half_life)`. Zo domineert het huidige regime de fit-richting bij een
    regime-wissel binnen het venster. `half_life_h ≤ 0` → uniform (uitgeschakeld). `None` →
    de module-constante RECENCY_HALFLIFE_H (op call-time gelezen → configureerbaar/testbaar)."""
    hl = RECENCY_HALFLIFE_H if half_life_h is None else half_life_h
    if not times or hl is None or hl <= 0:
        return [1.0] * len(times)
    ref = max(times)
    out = []
    for t in times:
        dt_h = (ref - t).total_seconds() / 3600.0
        out.append(2.0 ** (-dt_h / hl))
    return out

def rmse(res: list[float]) -> float:
    return math.sqrt(sum(r * r for r in res) / len(res)) if res else float("nan")

def naive_rmse(actual: dict) -> float:
    """RMSE van een persistentie-baseline: elke kamer blijft op zijn eerste sample in het
    venster. De 'doet-niets'-voorspeller waar het model tegen moet winnen — over exact dezelfde
    meetmomenten als de model-RMSE, zodat de skill een eerlijke ratio is. NaN bij geen samples."""
    sq, n = 0.0, 0
    for samples in actual.values():
        if not samples:
            continue
        base = samples[0][1]
        for _, temp in samples:
            sq += (base - temp) ** 2
            n += 1
    return math.sqrt(sq / n) if n else float("nan")

def skill_score(rmse_model: float, rmse_baseline: float) -> float | None:
    """1 − RMSE_model / RMSE_persistentie (afgerond, geklemd op ≤1). >0 = beter dan 'kamer
    beweegt niet', 0 = even goed, <0 = slechter. Weer-genormaliseerd: op een dag met veel
    temperatuurbeweging is de baseline óók slecht, dus een hoge RMSE drukt de skill niet
    vanzelf. None als de baseline onbruikbaar is (vlakke dag → ~0 noemer, of NaN)."""
    if (rmse_baseline is None or rmse_baseline != rmse_baseline or rmse_baseline < 1e-6
            or rmse_model is None or rmse_model != rmse_model):
        return None
    return round(min(1.0, 1.0 - rmse_model / rmse_baseline), 3)

def _window_naive(actual: dict, start: datetime, end: datetime) -> float:
    """Persistentie-baseline-RMSE over een DEEL-venster [start, end] — als naive_rmse maar
    begrensd in de tijd, zodat een herberekend leercurve-punt zijn eigen venster-baseline (en dus
    skill) krijgt. NaN bij geen samples in het venster."""
    sq, n = 0.0, 0
    for samples in actual.values():
        win = [v for (t, v) in samples if start <= t <= end]
        if not win:
            continue
        base = win[0]
        for v in win:
            sq += (base - v) ** 2
            n += 1
    return math.sqrt(sq / n) if n else float("nan")

def timed_residuals_from_sim(house, timeline, sim, actual: dict) -> list:
    """(meetmoment, voorspeld−werkelijk) per sensorkamer uit een ál-uitgevoerde simulatie `sim` —
    als _residuals maar mét de tijdstempel behouden én zónder opnieuw te simuleren, zodat een
    deel-venster-RMSE per leercurve-punt te snijden valt. Chronologisch gesorteerd."""
    out = []
    for rid, samples in actual.items():
        pred = sim.get("series", {}).get(rid, [])
        if not pred:
            continue
        pred = _to_sensor_series(house, timeline, rid, pred)
        for ts, val in samples:
            out.append((ts, _interp(pred, ts) - val))
    out.sort(key=lambda e: e[0])
    return out

def thin_rmse_history(hist: list[dict], now: datetime) -> list[dict]:
    """Leeftijds-gebaseerde uitdunning van de leercurve i.p.v. de oude count-slice.

    - Punten binnen CALIB_WINDOW_H blijven op volle kwartier-resolutie: dat is precies het
      venster waarbinnen backfill_rmse_history nog herberekent, dus daar mag niets verdwijnen.
    - Oudere punten worden uitgedund naar één per klok-uur. Binnen een uur-bucket wint de
      laatste níet-held/níet-gepauzeerde vertegenwoordiger (het weekjournaal filtert op
      "live" punten), anders de laatste sowieso.
    - Punten ouder dan RMSE_HISTORY_DAYS vervallen; RMSE_HISTORY_KEEP blijft als hard vangnet.

    Deterministisch en idempotent: een al-uitgedunde historie komt ongewijzigd terug
    (buckets bevatten dan al één punt). Muteert de invoer niet."""
    recent_cut = now - _timedelta_h(CALIB_WINDOW_H)
    age_cut = now - _timedelta_h(RMSE_HISTORY_DAYS * 24.0)
    recent: list[dict] = []
    buckets: dict[datetime, dict] = {}
    order: list[datetime] = []
    for p in hist:
        try:
            t = datetime.fromisoformat(p["t"])
        except (ValueError, TypeError, KeyError):
            continue
        if t < age_cut or t > now:
            continue
        if t >= recent_cut:
            recent.append(p)
            continue
        key = t.replace(minute=0, second=0, microsecond=0)
        best = buckets.get(key)
        if best is None:
            order.append(key)
            buckets[key] = p
            continue
        cand_live = not p.get("held") and not p.get("paused")
        best_live = not best.get("held") and not best.get("paused")
        if cand_live or not best_live:   # een live punt verdringt alles; non-live alleen non-live
            buckets[key] = p
    out = [buckets[k] for k in order] + recent
    return out[-RMSE_HISTORY_KEEP:]

def _wcost(res: list[float], extra_w: list[float] | None = None) -> float:
    """Huber-gewogen som van kwadraten — het doel dat de kalibratie minimaliseert (een
    paar uitschieters wegen lineair i.p.v. kwadratisch mee). `extra_w` (b.v. recency-gewichten)
    schaalt elke term extra, consistent met de normaalvergelijkingen in `calibrate`."""
    w = _huber_weights(res)
    if extra_w is not None:
        return sum(extra_w[i] * w[i] * res[i] * res[i] for i in range(len(res)))
    return sum(w[i] * res[i] * res[i] for i in range(len(res)))

def calibrate(house, params, timeline, seed, ctx: RunContext, actual, max_iter: int = 5,
              time_budget_s: float = 40.0, solar_mean: float | None = None,
              tm_seed=None, measured=None) -> tuple[dict, float]:
    """Minimaliseer Σ(voorspeld−werkelijk)² + Tikhonov-ridge naar de priors over het venster
    met gedempte Gauss-Newton. Online: schuif maar LEARN_RATE naar het optimum (stabiel,
    convergeert over runs). `solar_mean` (venster-zon-gemiddelde, W/m²) maakt het solar_gain-anker
    regime-bewust (zie reg_weight). `tm_seed` (per-zone massaknoop-beginwaarde) wordt aan élke
    interne simulatie doorgegeven zodat de fit dezelfde massa-startwaarde ziet als de eind-run.
    Geeft (nieuwe params, RMSE-na)."""
    rooms = [rid for rid in actual if actual[rid]]
    if not rooms:
        return params, float("nan")
    rooms_set = set(rooms)
    # Sensorloze maar wél gekoppelde zones (de trap) leren mee — zie coupled_sensorless_zones.
    sensorless = coupled_sensorless_zones(house, rooms)
    keys = _param_keys(rooms, sensorless)
    x = params_to_vec(params, keys)
    base = params
    prior_vec = [PRIORS[name] for _, name in keys]   # ridge-anker per parameter
    # Regime-bewust ridge-gewicht; sensorloze zones krijgen de zwaardere variant.
    sl = set(sensorless)
    reg_vec = [REG_WEIGHT_SENSORLESS if scope in sl else reg_weight(name, solar_mean)
               for scope, name in keys]

    def _total_cost(res: list[float], xv: list[float]) -> float:
        """Huber-datakosten (mét recency-weging) + Tikhonov-ridge naar de priors. Dezelfde schaal
        als de normaalvergelijkingen (de factor 2 valt consistent aan beide kanten weg)."""
        pen = sum(reg_vec[k] * (xv[k] - prior_vec[k]) ** 2 for k in range(len(keys)))
        return _wcost(res, tw) + pen

    # Eén timed-residu-call vooraf: levert zowel r0 als de sample-tijdstempels. De residu-volgorde
    # is identiek aan elke latere `_residuals`-call (zelfde actual/rooms_set), dus de recency-
    # gewichten `tw` blijven uitgelijnd door de hele fit.
    r0_timed = _residuals_timed(house, params, timeline, seed, ctx, actual, rooms_set,
                                tm_seed=tm_seed, measured=measured)
    if not r0_timed:
        return params, float("nan")
    r0 = [v for _, v in r0_timed]
    tw = _recency_weights([t for t, _ in r0_timed])   # huidig regime weegt zwaarder
    best_cost = _total_cost(r0, x)   # Huber-data×recency + ridge sturen accept/reject

    t_start = time.time()
    lam = 1e-3
    for _ in range(max_iter):
        if time.time() - t_start > time_budget_s:
            break
        p_cur = vec_to_params(x, keys, base, house)
        r = _residuals(house, p_cur, timeline, seed, ctx, actual, rooms_set, tm_seed=tm_seed,
                       measured=measured)
        if not r:
            break
        m = len(r)
        hw = _huber_weights(r)      # demp uitschieters (rare samples / deels-verkeerde log)
        # Recency × Huber: het huidige regime weegt zwaarder (zie _recency_weights). tw is vooraf
        # uitgelijnd op de residu-volgorde; bij een afwijkende lengte (zelden) val terug op uniform.
        twv = tw if len(tw) == m else [1.0] * m
        w = [hw[i] * twv[i] for i in range(m)]
        # Jacobiaan via voorwaartse differentie (één simulatie per parameter).
        J = [[0.0] * len(keys) for _ in range(m)]
        for j in range(len(keys)):
            dx = max(1e-3, abs(x[j]) * 0.05)
            xj = x[:]
            xj[j] += dx
            rj = _residuals(house, vec_to_params(xj, keys, base, house), timeline, seed, ctx, actual, rooms_set,
                            tm_seed=tm_seed)
            if len(rj) != m:
                continue
            for i in range(m):
                J[i][j] = (rj[i] - r[i]) / dx
        # Gewogen normaalvergelijkingen (JᵀWJ + λI) δ = −JᵀWr (W = recency × Huber).
        nk = len(keys)
        JtJ = [[sum(w[i] * J[i][a] * J[i][b] for i in range(m)) for b in range(nk)] for a in range(nk)]
        Jtr = [sum(w[i] * J[i][a] * r[i] for i in range(m)) for a in range(nk)]
        # Tikhonov-ridge naar de priors: Gauss-Newton-Hessiaan (+ridge op de diagonaal) en
        # gradient (+ridge·(x−prior)) van de penalty. Een platte (zwak-bepaalde) richting heeft
        # een kleine data-Hessiaan, dus de ridge domineert en trekt 'm naar de prior; een
        # sterk-bepaalde richting heeft een grote data-Hessiaan en blijft vrij. Het ridge-gewicht
        # is per parameter (reg_vec): `solar_gain` krijgt een sterkere ankering (zie reg_weight).
        for a in range(nk):
            JtJ[a][a] += reg_vec[a]
            Jtr[a] += reg_vec[a] * (x[a] - prior_vec[a])
        # Levenberg-demping op de (geregulariseerde) diagonaal.
        for a in range(nk):
            JtJ[a][a] += lam * (JtJ[a][a] + 1.0)
        delta = solve_linear(JtJ, [-v for v in Jtr])
        if delta is None:
            break
        # Klem de iterate zélf op BOUNDS (niet alleen de vec_to_params-projectie): anders kan x
        # buiten de band zwerven terwijl de ridge-anker-term (x−prior) op dat fantoompunt rekent.
        x_new = _clamp_to_bounds([x[j] + delta[j] for j in range(nk)], keys, house)
        new_cost = _total_cost(
            _residuals(house, vec_to_params(x_new, keys, base, house), timeline, seed, ctx, actual, rooms_set,
                       tm_seed=tm_seed), x_new)
        if math.isnan(new_cost) or new_cost >= best_cost:
            lam *= 4.0                      # geen verbetering → meer demping
            if lam > 1e6:
                break
            continue
        lam = max(1e-4, lam / 3.0)
        x = x_new
        best_cost = new_cost

    # Online: schuif LEARN_RATE van oud → nieuw zodat één run niet wild uitslaat.
    x_old = params_to_vec(params, keys)
    x_blend = [x_old[j] + LEARN_RATE * (x[j] - x_old[j]) for j in range(len(keys))]
    new_params = vec_to_params(x_blend, keys, base, house)
    final_rmse = rmse(_residuals(house, new_params, timeline, seed, ctx, actual, rooms_set, tm_seed=tm_seed))
    if final_rmse != final_rmse:   # NaN: vertrouw de geblende params niet — geef de oude terug
        return params, rmse(r0)   # (kan zelf NaN zijn → downstream vangt de niet-NaN-poort dat af)
    return new_params, final_rmse

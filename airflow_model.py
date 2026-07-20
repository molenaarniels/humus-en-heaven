#!/usr/bin/env python3
"""
airflow_model.py — Ventilatie-digital-twin (Project 8).

Het omgekeerde van Project 6 (de tado raam-koeladvies). Daar vertelt het model
wélke ramen je moet openen en handel jíj. Hier vertel jíj het model welke ramen,
roosters en deuren open/dicht staan, en het model:

  1. voorspelt per kamer de temperatuur ("afgeleide temperaturen") met een
     2-knoops RC-thermisch model gevoed door een meerzone-luchtstroomnetwerk
     (wind + schoorsteeneffect) en zoninstraling dóór het glas;
  2. vergelijkt die voorspelling met de échte tado-temperaturen (read-only uit
     docs/window_data.json) en toont de fout;
  3. kalibreert zijn eigen parameters elke run zodat de fout krimpt — een
     digital twin die beter wordt naarmate hij langer draait.

Daarnaast een *passief* suggestie-luik ("wat zou je openen voor de meeste koeling")
— puur ter info, je hoeft er niet naar te handelen. Géén Telegram in deze versie.

Volledig geïsoleerd: leest docs/window_data.json alleen-lezen (zoals het maaiproject
docs/data.json leest), haalt zélf wind + zon + buitenweer bij Open-Meteo, en niets
anders hangt van dit project af. Geen tado-auth → geen conflict met de roterende
tado-token van Project 6.

De buiten-nu temp + RH worden — net als soil/window — verfijnd met het eigen WU-station
(lokaler dan het Open-Meteo-grid), met de wu_bias-stralingscorrectie op de temp. Wind en
de zon-instraling voor de glasfysica blijven Open-Meteo (WU meet wind onbetrouwbaar; de
fysica heeft de direct/diffuus-split nodig die WU niet levert).

Bronnen / env:
  GIST_ID, GIST_TOKEN        — leest de gerapporteerde raam/rooster/deur-log
                               (`house_openings.json`) read-only uit de niet-geheime Gist
  WU_STATION_ID, WU_API_KEY  — buiten-nu temp + RH (biasgecorrigeerd) van het eigen station
  DRY_RUN=1                  — schrijf artefacten maar doe verder niets bijzonders

Pure stdlib + requests, geen numpy.
"""

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone


# Optionele, pure helpers uit naburige modules (géén netwerk/zijeffect bij import).
import shared_const
from shared_const import utc_now_iso
from gist_io import read_json as gist_read_json
from http_util import get_json
from notify import run_guarded, send_telegram
from wu_bias import correct_temp
from window_advisor import (convert_rh, RH_HARD_CAP, ROOM_COMFORT,
                            fetch_wu_current_temp)

TZ = shared_const.TZ

# ── Bestanden ─────────────────────────────────────────────────────────────────────
HOUSE_FILE     = os.getenv("HOUSE_MODEL_PATH", "house_model.json")
WINDOW_DATA    = os.getenv("WINDOW_DATA_PATH", "docs/window_data.json")
DASHBOARD_FILE = os.getenv("AIRFLOW_DATA_PATH", "docs/airflow_data.json")
LEARNED_FILE   = os.getenv("AIRFLOW_LEARNED_PATH", "docs/airflow_learned.json")
OPENINGS_FILE  = "house_openings.json"   # in de Gist (niet-geheim)
# Speciale sleutel in een openingen-log-snapshot: in wélke kamer de (ene, mobiele) airco staat
# — een room-id, of "" / "geen" = geen airco. Voorwaarts geaccumuleerd zoals elke andere stand.
# Bewust géén element-id: build_openings kent 'm niet → raakt het luchtstroomnetwerk nooit. Het
# model heeft géén actieve-koel-term, dus de AC-kamer wordt alleen uit de KALIBRATIE gelaten
# (zie main); ze blijft wél voorspeld + getoond.
AC_STATE_KEY = "ac_room"
# De AC-exclusie was retroactief-only: ze liet alleen samples vallen waarvan de log op dát moment
# `ac_room==kamer` zei. Meld je de airco "staat nu hier" zónder terug te dateren, dan bleef de
# koeling die al vóór de melding liep ín de fit (de AC-kamer leest dan kouder dan de fysica kan en
# trekt zowel haar eigen params als de gedeelde globalen scheef). Daarom laten we, zodra een kamer
# nú de airco heeft, óók haar samples van de laatste AC_GUARD_H uur vallen — ongeacht de log-timing.
AC_GUARD_H = 6.0
# Speciale sleutel in een openingen-log-snapshot: is het huis nu gepauzeerd (huis-breed, geen
# room-id)? True zolang iemand anders — niet de betrouwbare melder — thuis kan zijn; niemand
# meldt dan de raam/rooster/deur-standen betrouwbaar. Voorwaarts geaccumuleerd zoals ac_room.
# Anders dan AC_STATE_KEY raakt dit ALLE kamers tegelijk (het is geen kamer-uitsluiting maar een
# leer-gate — zie main), en anders dan de AC-guard is géén apart guard-venster nodig: een nog-
# actieve pauze is per definitie open-eindig tot nu (zie paused_intervals) en sluit recente
# samples dus vanzelf uit.
PAUSE_STATE_KEY = "paused"

# ── Fysische constanten ─────────────────────────────────────────────────────────────
CP_AIR = 1005.0    # J/(kg·K)
G      = 9.81       # m/s²
P_ATM  = 101325.0   # Pa
R_AIR  = 287.05     # J/(kg·K)

# Kalibratievenster + integratie.
CALIB_WINDOW_H = 48.0    # uur historie waarover we de fout minimaliseren (~2 dag-nacht-cycli;
                         # window_data houdt ~48u tado-historie → traag-veranderende termen
                         # ua_party/q_int/c_mass worden identificeerbaar i.p.v. naar hun grens
                         # te driften op een half-daags venster)
WARMUP_H       = 24.0    # uur sim-only aanloop vóór het residu-venster: de trage massaknoop
                         # equilibreert zodat zijn beginwaarde geen vrije laagfrequente bias is.
                         # Drivers reiken ver genoeg terug via Open-Meteo past_days; residuen
                         # tellen alleen waar tado-samples bestaan (≤CALIB_WINDOW_H terug)
CALIB_COVERAGE_WARN_H = 24.0   # effectieve grond-waarheid-spanwijdte (na AC-/verwarmings-/
                               # pauze-filters) waaronder het dashboard waarschuwt: het venster
                               # is nominaal 48u vol, maar de filters kunnen er stilletjes veel
                               # minder van overlaten — dan leunt de fit op te weinig data
SUBSTEP_S      = 300.0   # interne tijdstap (s) voor de Euler-integratie (stabiliteit)
SOLAR_SUBSTEPS = 3       # sub-samples per 15-min stap voor het tijdsgemiddelde van de instraling:
                         # de lage avondzon draait snel door het NW-gevelvlak (cos-invalshoek-knik),
                         # waardoor één punt-sample per stap aliast tot zichtbare wiebel in de
                         # voorspelde temp. Middel de flux over [t, t+dt] op de subinterval-middens
                         # (300s ≈ SUBSTEP_S) — fysisch de juiste grootheid (gemiddelde flux over de
                         # stap), niet een momentopname. Behoudt de dag-energie; dempt enkel de
                         # hoogfrequente aliasing.
RMSE_HISTORY_KEEP = 1000  # hard vangnet op het aantal leercurve-punten (na uitdunning ~384)
RMSE_HISTORY_DAYS = 10.0  # leeftijdslimiet van de leercurve. De uitdunning (thin_rmse_history)
                          # bewaart het CALIB_WINDOW_H-venster op volle kwartier-resolutie
                          # (backfill_rmse_history herberekent alléén daarbinnen) en dunt oudere
                          # punten uit naar uurcadans — zo kijkt het weekjournaal (RMSE_LOOKBACK_D
                          # 6.5 d) écht een week terug i.p.v. op een 2.5-daagse count-slice te
                          # stranden, zonder dat de artefacten meegroeien met de looptijd.
# Achteraf-herstel van de leercurve. Een leercurve-punt werd berekend met de openingen-log zoals
# díé toen luidde; meld je een raamwijziging te laat (of date je 'm terug), dan zat de oude fout
# door een verkeerde open/dicht-aanname verhoogd in de curve — model-skill verward met meld-fouten.
# Daarom herberekenen we de RMSE/skill van de punten die nog binnen het herstel-horizon
# (CALIB_WINDOW_H) vallen tegen de HUIDIG gecorrigeerde log + params — maar alléén de punten
# waarvan de log in hun venster daadwerkelijk is gewijzigd (vingerafdruk-poort `log_fp`, zie
# openings_fingerprint/backfill_rmse_history); zonder log-correctie bevriest een punt, hoe anders
# de huidige sim ook uitvalt. Punten ouder dan de horizon bevriezen sowieso: hun tado-grond-waarheid
# is uit het ~48u window_data-buffer gerold en valt niet meer te herberekenen — een harde
# datalimiet, geen keuze.
RMSE_BACKFILL_MIN_SAMPLES = 8     # te weinig overlap met de bewaarde actuals → punt ongemoeid laten
RMSE_BACKFILL_DELTA       = 0.25  # °C — ook bij een log-wijziging alleen overschrijven als de
                                  # waarde ≥ dit verschuift (correctie zonder materieel effect
                                  # raakt het punt niet aan)
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


# ── Fysica-revisie ───────────────────────────────────────────────────────────────────
# Bumpen wanneer een fysica-wijziging de betekenis van GELEERDE parameters verschuift, zodat
# oude geleerde waarden niet stilzwijgend op de nieuwe fysica worden losgelaten. Rev 2 =
# de wind-referentiehoogte-fix + effectief-openingsoppervlak (WIND_REF_Z/EFF_OPEN_AREA, juli
# 2026): cp_shelter (0.10, gevloerd) en vent_eff (0.43) stonden op waarden die louter de oude
# zelfde-gevel-lus compenseerden — onder de nieuwe fysica zijn dat fossielen. Bij een
# rev-mismatch reset merged_params alléén die twee globalen naar hun prior (kamer-params
# blijven staan: hun betekenis is niet verschoven) en laat main() het checkpoint + de
# anomalie-poort van die ene run vallen (beide zijn op de oude fysica geijkt).
PHYSICS_REV = 2

# ── Beste-params-checkpoint + auto-fallback ──────────────────────────────────────────
# Online leren met gepersisteerde params kan een slechte excursie — een rare/niet-gemelde
# opening-log, een code-deploy die de fit verslechtert, een anomaal venster — ín de opgeslagen
# toestand bakken, zónder weg terug. Daarom houden we een checkpoint bij van de béste tot nu
# toe geziene params en vallen we daarop terug als de kwaliteit langdurig wegzakt.
#
# Het kwaliteitssignaal is de SKILL t.o.v. een persistentie-baseline (zie `skill_score`),
# NIET de rauwe RMSE. De skill is weer-genormaliseerd: op een zwoele zonnige dag is élke
# voorspeller slechter (de kamer beweegt veel), dus de rauwe RMSE stijgt dan vanzelf — dat mag
# géén fallback triggeren. Alleen een echte modelregressie (skill zakt) doet dat. Bijkomend
# voordeel: een checkpoint wordt zo bij voorkeur vastgelegd op een informatief (zwaar) venster
# i.p.v. op een makkelijk mild venster, waar bijna elke param past.
CKPT_MIN_SKILL_GAIN = 0.01   # checkpoint pas bijwerken als de skill ≥ dit beter is (ruismarge)
FALLBACK_SKILL_DROP = 0.15   # skill ≥ dit ónder de checkpoint-skill telt als 'verslechterd'
FALLBACK_AFTER      = 8       # zoveel opeenvolgende verslechterde runs (~2u) → terugvallen
# Een fallback wordt vóór adoptie tegen de werkelijkheid gehouden (main): de checkpoint-params
# moeten op het HUIDIGE venster ook echt beter passen dan de zojuist geleerde params. Zonder die
# check ontstond een fallback-lus: een verouderd optimum (fossiel van een oudere code-versie,
# skill geoogst op een gunstig/informatief weer-venster) bleef onbereikbaar in kalm weer — skill
# is daar structureel negatief omdat persistentie op een vlak venster bijna perfect is — zodat
# elke ~FALLBACK_AFTER runs de fossiele params werden teruggezet (RMSE 0.6 → 1.9°C), het online
# leren ze in een uur terugkalibreerde, en de cyclus opnieuw begon (gediagnosticeerd juli 2026).
# Een verworpen fallback her-zetelt het checkpoint op de huidige params (reseat_checkpoint):
# de lat ligt weer op een haalbaar niveau en een écht regressie-vangnet blijft bestaan.
# Een GEACCEPTEERDE fallback her-vloert de lat óók (accept_fallback_checkpoint): de checkpoint-
# params passen dan wel beter dan wat net geleerd is, maar de opgeslagen skill-lat is een
# high-water-mark van één gunstig venster. Zonder her-vloeren blijft de lat op dat niveau staan,
# halen normale vensters 'm structureel niet (skill is venster-afhankelijk, hoe goed de params
# ook zijn) en wordt het leren elke ~FALLBACK_AFTER runs opnieuw naar het checkpoint teruggetrokken
# — een permanente jojo i.p.v. een vangnet (gediagnosticeerd 9–10 juli 2026: lat 0.813 geoogst op
# één venster, daarna 7 opeenvolgende "degraded" runs met geaccepteerde fallbacks elke ~2u). De
# her-gevloerde lat is wat de teruggezette params op het HUIDIGE venster halen: haalbaar, en elke
# echte verbetering zet er weer een nieuw optimum bovenop.


def checkpoint_step(ckpt: dict, params: dict, skill: float | None,
                    rmse_now: float, version: str, now_iso: str) -> tuple[dict, dict, bool]:
    """Beslis over het beste-params-checkpoint op basis van de (weer-genormaliseerde) skill.
    Geeft `(params, checkpoint, fell_back)`:
      • nieuw/eerste skill-optimum → leg de huidige params vast (geen fallback);
      • skill ≥ FALLBACK_SKILL_DROP ónder het optimum, FALLBACK_AFTER runs lang → geef de
        checkpoint-params terug en zet `fell_back=True` (de aanroeper hermerged + herberekent
        dan de RMSE met de teruggezette params);
      • anders ongewijzigd, teller gereset.
    Puur: doet zelf geen simulatie. Roep alléén aan op écht-geleerde runs (niet `held`)."""
    ckpt = dict(ckpt or {})
    best_skill = ckpt.get("skill")
    if skill is not None and (best_skill is None or skill >= best_skill + CKPT_MIN_SKILL_GAIN):
        return params, {"params": json.loads(json.dumps(params)), "skill": skill,
                        "rmse": round(rmse_now, 3), "version": version, "t": now_iso,
                        "degraded_runs": 0, "last_fallback": ckpt.get("last_fallback")}, False
    if (skill is not None and best_skill is not None
            and skill <= best_skill - FALLBACK_SKILL_DROP):
        ckpt["degraded_runs"] = int(ckpt.get("degraded_runs", 0)) + 1
        if ckpt["degraded_runs"] >= FALLBACK_AFTER and ckpt.get("params"):
            ckpt["degraded_runs"] = 0
            ckpt["last_fallback"] = now_iso
            return json.loads(json.dumps(ckpt["params"])), ckpt, True
        return params, ckpt, False
    ckpt["degraded_runs"] = 0
    return params, ckpt, False


def reseat_checkpoint(params: dict, skill, rmse_now: float, version: str, now_iso: str,
                      last_fallback=None) -> dict:
    """Nieuw checkpoint op de HUIDIGE params — de uitkomst van een verworpen fallback: het
    opgeslagen optimum bleek een fossiel (past op het huidige venster slechter dan wat net
    geleerd is), dus de skill-lat wordt terug op een haalbaar niveau gelegd. `reseated` stempelt
    dit (additief) zodat een her-zeteling op het dashboard te onderscheiden is van een gewoon
    nieuw optimum; `last_fallback` behoudt de laatste écht uitgevoerde fallback."""
    return {"params": json.loads(json.dumps(params)), "skill": skill,
            "rmse": round(rmse_now, 3) if rmse_now == rmse_now else None,
            "version": version, "t": now_iso, "degraded_runs": 0,
            "last_fallback": last_fallback, "reseated": now_iso}


def accept_fallback_checkpoint(ckpt: dict, skill, rmse_now: float, now_iso: str) -> dict:
    """Her-vloer de skill-lat na een GEACCEPTEERDE fallback: de checkpoint-params blijven staan
    (die zijn zojuist teruggezet en passen aantoonbaar beter), maar de opgeslagen skill/rmse
    worden wat die params op het HUIDIGE venster halen. De oude lat was een high-water-mark van
    één gunstig venster; onaangepast blijft elke normale run "degraded" en jojo't het leren elke
    ~FALLBACK_AFTER runs terug naar het checkpoint (zie de toelichting bij FALLBACK_AFTER).
    `refloored` stempelt dit (additief) zodat het op het dashboard te onderscheiden is van een
    gewoon nieuw optimum of een her-zeteling na een verworpen fallback."""
    ckpt = dict(ckpt or {})
    ckpt["skill"] = skill
    ckpt["rmse"] = round(rmse_now, 3) if rmse_now == rmse_now else None
    ckpt["refloored"] = now_iso
    return ckpt


def calib_coverage(actual: dict) -> dict:
    """Effectieve grond-waarheid-dekking van deze kalibratierun: hoeveel tado-samples, over
    hoeveel kamers, en welke tijdspanne — gemeten NA de AC-/verwarmings-/pauze-filters. Het
    venster is nominaal CALIB_WINDOW_H vol, maar de filters kunnen er stilletjes veel minder
    van overlaten (winter: veel gestookte samples); dan leunt de fit op weinig data en hoort
    het dashboard dat te tonen (drempel CALIB_COVERAGE_WARN_H)."""
    ts = [t for s in (actual or {}).values() for t, _ in s]
    span_h = (max(ts) - min(ts)).total_seconds() / 3600.0 if len(ts) >= 2 else 0.0
    return {"calib_samples": sum(len(s) for s in (actual or {}).values()),
            "calib_rooms": len(actual or {}),
            "calib_span_h": round(span_h, 1)}


def window_weather_summary(weather: dict, now: datetime, window_h: float) -> dict:
    """Compacte weer-context van het residu-venster: dag-max buitentemp + zon-piek/-gemiddelde.
    Stempelt elk leercurve-punt zodat een RMSE-stijging aan het wéér te koppelen is (een hete
    zonnige dag) i.p.v. blind als modelregressie gelezen te worden. Lege dict bij geen historie."""
    since = now - _timedelta_h(window_h)
    rows = [r for r in weather.get("hourly", [])
            if r.get("dt") is not None and since <= r["dt"] <= now]
    temps = [r["T_out"] for r in rows if r.get("T_out") is not None]
    solar = [r["shortwave"] for r in rows if r.get("shortwave") is not None]
    out: dict = {}
    if temps:
        out["tmax"] = round(max(temps), 1)
    if solar:
        out["solar_peak"] = round(max(solar))
        out["solar_mean"] = round(sum(solar) / len(solar))
    return out


# ── Tussenwoning-fysica: buren + interne warmtelast ─────────────────────────────────
# Dit is een jaren-'20 rijtjeshuis (tussenwoning): woningscheidende (party) muren grenzen
# aan álle kamers aan verwarmde buren die ~jaarrond op kamertemperatuur zitten. Die muren
# trekken elke kamer naar NEIGHBOR_TEMP i.p.v. naar de (koude) buitenlucht — een grote,
# bijna-constante warmtebuffer die het model zónder deze term structureel mist (waardoor
# het de kamers te koud voorspelt en de kalibratie álle knoppen naar hun grens duwt om
# warmte vast te houden). De geleerde per-kamer `ua_party` vangt de geleiding-grootte op.
#
# NEIGHBOR_TEMP is niet langer een vaste constante: de buren (jaren-'20 tussenwoning, zónder
# airco) stoken 's winters tot ~kamertemp maar zweven 's zomers mee met buiten — op een
# hittegolf zitten ze eerder op 24–26°C dan op 20°C. Een vaste 20°C zou de party-muren de
# kamers dan onterecht naar beneden trékken, juist als de twin telt. We schatten daarom per run
# een traag, gedempt buur-anker (`neighbor_temp_estimate`): de winter-stookvloer, opgetild met
# het 3-daags buitengemiddelde. De módule-default blijft 20.0 (back-compat voor directe
# simulate-tests); main() herbindt `_NEIGHBOR_TEMP` aan de run-schatting, net als _LAT/_LON.
NEIGHBOR_TEMP = 20.0
NEIGHBOR_WINTER_FLOOR = 19.5   # °C — buren stoken 's winters minstens tot ~deze temp
_NEIGHBOR_TEMP = NEIGHBOR_TEMP  # run-gebonden buur-anker; herbonden in main()

# Interne warmtelast (mensen, koken, apparaten, verlichting): nominale dichtheid (W/m³
# kamervolume) × de geleerde per-kamer `q_int` × een dag/nacht-profiel. Overdag (wakker)
# vol, 's nachts gedempt — slapende lichamen + sluimerverbruik zijn niet nul.
INTERNAL_GAIN_WM3        = 1.5   # W per m³ kamervolume bij profiel = 1.0 (prior; q_int schaalt)
INTERNAL_DAY_START       = 7     # lokaal uur: profiel → dag (wakker)
INTERNAL_NIGHT_START     = 23    # lokaal uur: profiel → nacht (slapend)
INTERNAL_NIGHT_FRACTION  = 0.5   # nacht-aandeel van de dag-last
INTERNAL_RAMP_H          = 1.0   # uur — duur van de soepele dag/nacht-overgang (geen sprong)


def neighbor_temp_estimate(rows: list[dict], now: datetime, lookback_h: float = 72.0) -> float:
    """Traag, gedempt buur-anker voor de party-muren: de winter-stookvloer, opgetild met het
    gemiddelde buitentemp over de laatste `lookback_h` (default 3 dagen). 's Winters domineert
    de stookvloer (~19.5°C); 's zomers volgt het anker het buitengemiddelde mee omhoog
    (hittegolf → ~24°C) zodat de party-muren de kamers niet onterecht naar 20°C koelen.
    Valt terug op NEIGHBOR_TEMP als er geen bruikbare historie is."""
    since = now - timedelta(hours=lookback_h)
    temps = [r["T_out"] for r in rows
             if r.get("T_out") is not None and since <= r["dt"] <= now]
    if not temps:
        return NEIGHBOR_TEMP
    return max(NEIGHBOR_WINTER_FLOOR, sum(temps) / len(temps))


def _ramp(x: float, center: float, width: float) -> float:
    """Stijgende lineaire ramp 0→1 over [center−width/2, center+width/2], daarbuiten geklemd."""
    if width <= 0:
        return 1.0 if x >= center else 0.0
    return max(0.0, min(1.0, (x - center) / width + 0.5))


def internal_gain_profile(t) -> float:
    """Dag/nacht-schaalfactor (0..1) voor de interne warmtelast op tijdstip `t` (lokaal):
    wakker (INTERNAL_DAY_START..NIGHT_START) → 1.0, slapend → INTERNAL_NIGHT_FRACTION, met een
    soepele ~INTERNAL_RAMP_H overgang i.p.v. een harde sprong (een stap injecteert een knik in
    de voorspelde temp precies op 07/23u, die als residu terugkomt en de diurnale RMSE-swing
    voedt). Robuust tegen een t zonder .hour (→ 1.0)."""
    try:
        hr = t.hour + t.minute / 60.0
    except AttributeError:
        return 1.0
    awake = min(_ramp(hr, INTERNAL_DAY_START, INTERNAL_RAMP_H),
                1.0 - _ramp(hr, INTERNAL_NIGHT_START, INTERNAL_RAMP_H))
    return INTERNAL_NIGHT_FRACTION + (1.0 - INTERNAL_NIGHT_FRACTION) * awake

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
# `anomaly_nudge_at` leeft in airflow_learned.json (additief; de artefact-commit is de state).
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


def anomaly_nudge_text(rmse_cur: float, baseline: float | None) -> str:
    """Het nudge-bericht: fout vs norm + de vraag die de datakwaliteit herstelt. HTML-parse
    (send_telegram-default); dashboardlink alleen als DASHBOARD_URL gezet is."""
    norm = f" (norm ~{baseline:.2f}°C)" if baseline is not None else ""
    lines = [
        "🧭 <b>Tweeling: leren gepauzeerd</b> — voorspelfout anomaal hoog: "
        f"<b>{rmse_cur:.2f}°C</b>{norm}.",
        "Waarschijnlijk staat er een raam/rooster/deur anders dan gemeld. "
        "Klopt de raamstand nog? Meld de werkelijke standen (desnoods teruggedateerd) "
        "in de dashboard-modal — de leercurve herstelt zich dan automatisch.",
    ]
    dash = os.getenv("DASHBOARD_URL")
    if dash:
        lines += [f'<a href="{dash.rstrip("/")}/airflow.html">→ Open het tweeling-dashboard</a>']
    return "\n".join(lines)


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


def should_hold_learning(rmse_cur: float, history: list[dict]) -> tuple[bool, float | None]:
    """(pauzeer?, norm). Pauzeer het leren als de huidige fout veel groter is dan de
    recente norm én absoluut betekenisvol — het sterke teken dat de opening-log niet met
    de werkelijkheid overeenkomt. Te weinig historie → nooit pauzeren (eerst bootstrappen)."""
    base = learning_baseline(history)
    if base is None or rmse_cur != rmse_cur:
        return False, base
    return rmse_cur > max(ANOMALY_FLOOR, base * ANOMALY_FACTOR), base


def _huber_weights(residuals: list[float], delta: float = HUBER_DELTA) -> list[float]:
    """Huber-gewicht per residu: 1 binnen ±delta, daarbuiten delta/|r| (<1) zodat een
    uitschieter de kleinste-kwadraten-fit niet domineert."""
    return [1.0 if abs(r) <= delta else delta / abs(r) for r in residuals]

# Leakage (infiltratie) per kamer: een kleine, altijd aanwezige lek naar buiten. Houdt
# het luchtstroomnetwerk goed geconditioneerd (een verder dichte kamer is niet singulier)
# en is fysisch reëel (kieren). m² effectief lekoppervlak.
LEAK_AREA = 0.004

# ── Dak (zolder/bovenste verdieping) — sol-air-term ─────────────────────────────────
# De bovenste verdieping (office, trap) wisselt warmte uit via een groot dakvlak met een
# sterke middag-zonlast én 's nachts hemel-stralingskoeling — een gerichte, tijd-variërende
# driver die een platte schil-UA niet kan vatten (waardoor `office.ua_env` op zijn grens
# satureerde). We modelleren 'm als een sol-air-koppeling op de massaknoop: het dak "ziet" een
# effectieve buitentemp T_solair = T_out + ROOF_SOLAR_GAIN·I_horizontaal − ROOF_SKY_COOLING
# ('s nachts). Alleen actief voor kamers met `roof_m2 > 0`; de geleerde `ua_roof` schaalt de
# grootte. Bewust grof: priors die de kalibratie verder bijstelt.
ROOF_U          = 1.5   # W/(m²·K) — dak-schil-conductie-basis (1920s, deels na-geïsoleerd)
ROOF_SOLAR_GAIN = 0.025  # °C per W/m² horizontale instraling (≈ donkere dakabsorptie / h_out)
ROOF_SKY_COOLING = 3.0   # °C — nachtelijke hemel-stralings-depressie ('s nachts, helder)

# ── Zonnige-dag-nauwkeurigheid (stappen 1 & 2) ──────────────────────────────────────
# STAP 1 — WU-gemeten-zon herschaling van de Open-Meteo glas-drive. Open-Meteo levert een gladde
# uur-instraling zonder de wolk-transiënten die op zonnige dagen juist de meeste variatie geven; het
# co-gelegen WU-pyranometer (al opgehaald voor de bias-correctie) vángt die bursts wel. We schalen de
# OM direct+diffuus met k = WU_global/OM_global (behoudt de direct/diffuus-split) op de recente
# stappen rond nu, lineair uitdovend naar 1.0 (pure OM) verder terug — dit project heeft geen WU
# uur-historie, alleen de nu-meting (mirror van de window-advisor BIAS_DECAY_H). No-op als WU ontbreekt.
WU_SOLAR_SCALE_DECAY_H = 3.0   # uur: het WU/OM-herschaal-gewicht dooft lineair naar 0 over dit venster
WU_SOLAR_SCALE_MIN = 0.3       # klem: WU kan bij gebroken bewolking laag/hoog uitschieten
WU_SOLAR_SCALE_MAX = 1.5
WU_SOLAR_MIN_WM2 = 20.0        # onder dit zonniveau: herschaling irrelevant (nacht/schemer) → k=1
# STAP 2 — hoek-afhankelijke glas-transmissie. GLASS_TRANSMITTANCE (0.7) is de transmissie bij
# loodrechte inval; echte beglazing laat bij scherende invalshoeken (de lage NW-avondzon op de
# straatgevel) veel minder door. Standaard ASHRAE-incidentiehoek-modifier Kτα = 1 − b0·(1/cosθ − 1),
# genormaliseerd op 1 bij loodrechte inval en geklemd op [0,1]; b0≈0.1 voor heldere beglazing. Alleen
# op de beam-component (diffuus houdt de vlakke transmissie). Achter een vlag zodat de default
# ongewijzigd blijft (facade_irradiance zonder beam_iam).
GLASS_IAM_B0 = 0.10
# Glas-zonwinst in build_timeline: transmissie bij loodrechte inval (dubbel glas,
# SHGC-achtig) en de fractie van het raamkozijn die glas is wanneer `glass_m2`
# niet expliciet in house_model.json staat. Zelfde status als GLASS_IAM_B0:
# gedocumenteerde fysische priors, bewust niet leerbaar.
GLASS_TRANSMITTANCE = 0.7
GLASS_AREA_FRACTION = 0.6

# ── Trappenhuis-stratificatie (stap 3) ──────────────────────────────────────────────
# De koker is één goedgemengde knoop, maar fysisch pool warme lucht bovenin. We houden de enkele
# knoop (kalibratie/airflow) maar leggen een begrensde, NIET-leerbare verticale gradiënt γ (°C/m) op —
# een gedocumenteerde prior zoals ROOF_SOLAR_GAIN, geen vrije parameter (een vrije γ zou degenereren
# met de deur-advectie). γ schaalt met de verticale temp-spreiding van de gekoppelde kamers (warme
# kamer boven, koele onder → drijft de stratificatie) en voedt (a) de weergave (top/onder) en (b) de
# advectieve deur-koppeling: elke verdiepingsdeur mengt tegen T_koker + γ·(z − z_mid) i.p.v. de
# vlakke gemiddelde-temp. Opt-in via "stratify": true op de zone in house_model.json → default
# (afwezig) volledig ongewijzigd.
# γ is GEEN afgestemde constante meer maar de kleinste-kwadraten-HELLING van de kamertemp t.o.v.
# deurhoogte door de gekoppelde kamers (ted 1.0m, hotties 3.9m, office 7.0m) — die kamers grenzen
# met een OPEN deur aan de koker en zijn dus een directe proxy-meting van het verticale profiel
# (gevalideerd: 142 tado-punten gaven een gemeten helling ~0.15 °C/m, piek ~0.67 op de zonnige
# stretch). Zo ijkt de gradiënt zichzelf op de kamers en hoeft er geen K/zon-constante geraden te
# worden — de zon zit al ín de kamertemps (office loopt warm in de middag → de helling steilt
# vanzelf). Alleen deuren die op dat moment OPEN zijn tellen mee (dichte deur = ontkoppeld); <2
# open verdiepingen → vlak. Enkel de klem blijft een prior.
STAIR_STRAT_MAX_GRAD = 0.7   # °C/m — klem (gemeten kamer-helling piekte op ~0.67; ~8m koker → ~5°C
                             # top-onder-verschil). Rail tegen office's eigen zonwinst die de helling
                             # op de zonnigste momenten kunstmatig zou opblazen.
# Tweerichtings-deuruitwisseling (Brown–Solvason). Het netwerk rekent alleen het NETTO-debiet door
# een deur, maar een open binnendeur tussen een warme koker en een koelere kamer draagt een grote
# buoyancy-gedreven counterflow (warm bovenlangs eruit, koel onderlangs erin) — óók bij netto nul.
# Zonder die term kon de koker-knoop ~2°C boven een open-deur-kamer blijven zweven (top-weergave
# 28.4° naast office 24.3° — fysisch onmogelijk over een open deur). Q_ex = C·A·√(g·H·ΔT/T̄) per
# open koker-deur, uitgewisseld tegen de koker-lucht op déúrhoogte (γ-offset, consistent met de
# stratificatie). Deur dicht → geen term → de skylight-/dakwarmte poolt bovenin (de "pocket").
# Bewust alleen op de deuren van stratify-zones (scope/risico). Gaat als eigen geleiding in
# gdoor, BUITEN de geleerde × vent_eff om: dit is een fysieke orifice-term (zelfde argument als
# de vaste `cd`), en de netto-advectie-efficiency erover heen schalen dempte de pinning ~3× —
# de sensorloze koker bleef daardoor ~1°C ónder zijn open-deur-kamers hangen (onderkant kouder
# dan ted bij open deur, het spiegelbeeld van de 28.4°-float die deze term juist moest fixen).
BUOY_EXCH_C = 0.14      # ≈ Cd/3 met doorway-Cd ~0.42 (Brown–Solvason interzonale convectie)
DOOR_HEIGHT_M = 2.0     # m — verticale maat van een binnendeur (drijft de stack in de deuropening)
# Zon-kroon voor de top-weergave: de skylight-/dak-zon landt bovenin de koker; met de office-deur
# open wordt dat grotendeels weggemengd (counterflow), maar de bovenste ~1.7m (bóven de hoogste
# deur) blijft op een felle middag een paar graden warmer dan de γ-lijn. Display-term (geen
# fysica-knoop), ∝ horizontale dak-instraling, geklemd — 's avonds (irr≈0) automatisch 0, dus de
# avond-inconsistentie kan er niet door terugkomen.
STAIR_CROWN_K = 0.004    # °C per W/m² horizontale dak-instraling
STAIR_CROWN_MAX = 4.0    # °C — klem

# ── Prior-parameters (vertrekpunt vóór het leren) ───────────────────────────────────
# Alles is een dimensieloze schaal × een fysische basis, zodat het leren rond 1.0 speelt
# en geclamped blijft in een fysiek plausibele band.
PRIORS = {
    "cp_shelter":  0.5,   # wind-Cp-amplitude × dit (stedelijk/beschut < 1)
    "cd":          0.62,  # ontladingscoëfficiënt van de openingen
    "vent_eff":    1.0,   # globale ventilatie-effectiviteit (advectieve menging)
    # per-kamer schalen (× de fysische basis afgeleid uit volume/wandoppervlak):
    "c_air":       1.0,   # luchtknoop-warmtecapaciteit
    "c_mass":      1.0,   # massaknoop (wanden/meubels)
    "h_am":        1.0,   # lucht↔massa-koppeling
    "ua_env":      1.0,   # schil-conductie (lucht-gekoppeld deel)
    "ua_mass":     1.0,   # schil-conductie naar de massaknoop
    "solar_gain":  1.0,   # zonwinst dóór het glas
    "ua_party":    1.0,   # geleiding naar de buur (woningscheidende muren → NEIGHBOR_TEMP)
    "q_int":       1.0,   # interne warmtelast (mensen/koken/apparaten), dag/nacht-profiel
    "ua_roof":     1.0,   # dak-sol-air-koppeling (alleen actief bij roof_m2 > 0)
    # f_air is géén dimensieloze schaal maar een absolute fractie (0..1): het deel van de
    # zonwinst dat direct op de snelle luchtknoop landt i.p.v. op de trage massaknoop. Leerbaar
    # zodat het model de midday-piek-timing kan vinden (te hoog → spikes die de fit dan met een
    # lage solar_gain probeerde te onderdrukken).
    "f_air":       0.4,   # fractie zonwinst → luchtknoop (rest → massaknoop)
}
# Clamp-banden voor de leerbare schalen (ondergrens, bovengrens).
# `vent_eff`-ondergrens verlaagd 0.3→0.1: nu `cd` op zijn fysische waarde vastligt (zie CD),
#   moet `vent_eff` de écht-lage meng-koppeling van dit huis kunnen bereiken zónder te railen.
# `solar_gain`-ondergrens opgetild 0.0→0.25: een fysieke vloer (er komt áltijd wat zon binnen)
#   zodat de twin nooit volledig "zon-uit" leert op een mild/bewolkt venster.
BOUNDS = {
    "cp_shelter": (0.1, 1.2), "cd": (0.3, 0.9), "vent_eff": (0.1, 2.0),
    "c_air": (0.3, 8.0), "c_mass": (0.2, 10.0), "h_am": (0.2, 5.0),
    "ua_env": (0.2, 5.0), "ua_mass": (0.2, 5.0), "solar_gain": (0.25, 3.0),
    "ua_party": (0.0, 6.0), "q_int": (0.0, 4.0), "ua_roof": (0.0, 4.0),
    "f_air": (0.1, 0.9),   # absolute fractie zonwinst → luchtknoop (fysiek 0..1, marge gehouden)
}
# Welke parameters per kamer leren. `h_am` (lucht↔massa-koppeling) leert mee zodat `c_air`
# niet langer de énige knop is die bepaalt hoe snel de luchtknoop de drivers volgt — zonder een
# vrije `h_am` satureerde `c_air` op zijn bovengrens in álle kamers (degeneratie). `ua_roof`
# leert mee maar heeft basis 0 (→ geen effect, nul-gradient, ridge parkeert 'm op de prior) voor
# kamers zónder `roof_m2`. `f_air` (zon-split lucht/massa) leert mee zodat het model de midday-piek-
# timing kan vinden i.p.v. die met een te lage `solar_gain` weg te drukken. `ua_mass` blijft op
# zijn prior (minder vrijheid, stabieler).
PER_ROOM_PARAMS = ["c_air", "c_mass", "h_am", "ua_env", "solar_gain", "ua_party", "q_int", "ua_roof", "f_air"]
# `cd` is geen leerbare globale parameter meer: het is een fysische orifice-constante (~0.62) die
# óók de volumetrische ACH/flows (dashboard + suggest) zet. De thermische fit railde 'm naar zijn
# vloer (degenereert met `vent_eff` in de meng-koppeling ∝ cd·vent_eff), wat de getoonde airflow
# corrumpeert. Nu vast op CD; `vent_eff` draagt de meng-koppeling alleen.
GLOBAL_PARAMS   = ["cp_shelter", "vent_eff"]
CD = PRIORS["cd"]   # vaste ontladingscoëfficiënt (niet geleerd)

RAIL_TOL = 0.02   # binnen deze fractie van de band-breedte → de param zit 'op zijn grens'


def railed_params(params: dict, tol: float = RAIL_TOL) -> list[str]:
    """Welke geleerde params op (≈) hun BOUNDS-grens zitten — de 'saturatie-tell' dat een
    fysisch kanaal naar zijn extreem geduwd wordt (vaak een structureel tekort of een te-warme
    prior). Geeft een lijst `'scope.param@floor|ceil'` voor logging/dashboard. Massaal floor-railen
    op de warmte-in-kanalen = de optimizer komt niet koel genoeg. Puur diagnostisch, geen mutatie."""
    out = []

    def _flag(scope: str, name: str, value) -> None:
        b = BOUNDS.get(name)
        if b is None or not isinstance(value, (int, float)):
            return
        lo, hi = b
        rng = hi - lo
        if rng <= 0:
            return
        if value - lo <= tol * rng:
            out.append(f"{scope}.{name}@floor")
        elif hi - value <= tol * rng:
            out.append(f"{scope}.{name}@ceil")

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


# ════════════════════════════════════════════════════════════════════════════════════
#  Lineaire algebra — kleine dichte Gauss-eliminatie (geen numpy)
# ════════════════════════════════════════════════════════════════════════════════════

def solve_linear(A: list[list[float]], b: list[float]) -> list[float] | None:
    """Los A·x = b op met partieel pivoteren. None bij (bijna-)singulier."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(n):
            if r == col:
                continue
            f = M[r][col] / pv
            if f:
                for c in range(col, n + 1):
                    M[r][c] -= f * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


# ════════════════════════════════════════════════════════════════════════════════════
#  Zonpositie — NOAA-algoritme (puur stdlib math)
# ════════════════════════════════════════════════════════════════════════════════════

def sun_position(lat: float, lon: float, when_utc: datetime) -> tuple[float, float]:
    """(azimut °, elevatie °) van de zon. Azimut met de klok mee vanaf noord (0=N,
    90=O, 180=Z, 270=W); elevatie boven de horizon (negatief = onder). NOAA."""
    if when_utc.tzinfo is None:
        when_utc = when_utc.replace(tzinfo=timezone.utc)
    u = when_utc.astimezone(timezone.utc)
    doy = u.timetuple().tm_yday
    hour = u.hour + u.minute / 60.0 + u.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (doy - 1 + (hour - 12.0) / 24.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    time_offset = eqtime + 4.0 * lon                  # minuten
    tst = (hour * 60.0 + time_offset) % 1440.0        # echte zonnetijd, minuten
    ha = math.radians(tst / 4.0 - 180.0)              # uurhoek, rad
    lat_r = math.radians(lat)
    cos_zen = (math.sin(lat_r) * math.sin(decl)
               + math.cos(lat_r) * math.cos(decl) * math.cos(ha))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zenith = math.acos(cos_zen)
    el = 90.0 - math.degrees(zenith)
    sin_zen = math.sin(zenith)
    if sin_zen < 1e-6:
        return 180.0, el
    cos_az = (math.sin(lat_r) * cos_zen - math.sin(decl)) / (math.cos(lat_r) * sin_zen)
    cos_az = max(-1.0, min(1.0, cos_az))
    az_core = math.degrees(math.acos(cos_az))
    az = (az_core + 180.0) % 360.0 if math.degrees(ha) > 0 else (540.0 - az_core) % 360.0
    return az, el


def beam_iam_factor(cos_inc: float, b0: float = GLASS_IAM_B0) -> float:
    """ASHRAE-incidentiehoek-modifier voor de beam-glas-transmissie: Kτα = 1 − b0·(1/cosθ − 1),
    genormaliseerd op 1 bij loodrechte inval (cos=1) en geklemd op [0,1]. cos_inc ≤ 0 (zon achter
    het vlak) → 0. Vangt de scherende-hoek-terugval die de vlakke 0.7-transmissie mist."""
    if cos_inc <= 1e-6:
        return 0.0
    return max(0.0, min(1.0, 1.0 - b0 * (1.0 / cos_inc - 1.0)))


def horizon_diffuse_reduction(horizon_deg: float) -> float:
    """Fractie van de diffuse hemelkoepel-viewfactor die een gevel-obstakel (`horizon_elevation_deg`,
    b.v. overburen of een boom) wegneemt van een verticaal raam — het stuk dat eerder bewust
    "tweede-orde" bleef (zie `facade_irradiance`). Exact afgeleid voor een isotrope hemel op een
    verticaal vlak: de invalshoek-cosinus is cos(θ) = cos(e)·cos(a) (e = elevatie, a = azimut t.o.v.
    de gevelnormaal), dus de bijdrage per hemel-band weegt cos²(e) — een verticale muur "ziet"
    verhoudingsgewijs veel van de lage hemel vlak boven de horizon en niets van het zenit. Blokkeer je
    alles onder elevatie h, dan resteert F(h) = 0.5 − h/π − sin(2h)/(2π) (uit ∫cos²(e)de over [h,π/2],
    genormaliseerd op de onbeschaduwde F(0) = 0.5 = (1+cosβ)/2 bij β=90°); de teruggenomen fractie
    t.o.v. die onbeschaduwde 0.5 is dus 2h/π + sin(2h)/π. Alleen toegepast op verticale ramen (de
    enige die vandaag `horizon_elevation_deg` zetten); voor een ijl/doorlatend obstakel (een boom met
    gaten in de kroon, i.p.v. een dichte overburen-gevel) is dit een bovengrens."""
    h = math.radians(max(0.0, min(90.0, horizon_deg)))
    return max(0.0, min(1.0, 2.0 * h / math.pi + math.sin(2.0 * h) / math.pi))


def facade_irradiance(facade_az: float, sun_az: float, sun_el: float,
                      direct: float, diffuse: float, tilt_deg: float = 90.0,
                      diffuse_only: bool = False, horizon_deg: float = 0.0,
                      beam_iam: bool = False) -> float:
    """Instraling (W/m²) op een vlak met azimut `facade_az` en helling `tilt_deg` vanaf
    horizontaal (90 = verticaal raam, 0 = plat dakraam/skylight). Directe component via de
    invalshoek op het hellende vlak; diffuus via de hemelkoepel-viewfactor (1+cos β)/2.

    `diffuse_only`: het raam wordt door een vast obstakel (b.v. een huis ervóór + zonwering)
    permanent uit de directe zonnestraal gehouden, maar ziet nog wél de diffuse hemel. Dan
    valt de beam-term volledig weg en blijft alleen de diffuse view-factor over — anders dan
    een `shading`/`shade`-factor, die juist béíde componenten gelijk dempt.

    `horizon_deg`: schijnbare elevatie (°) van een obstakel vóór de gevel — de gelijk-hoge
    overburen aan de NW-straatzijde, plus (voor de laagste ramen) een boom. Staat de zon
    lager dan deze hoek, dan is de directe straal geblokkeerd en blijft enkel diffuus over
    (dezelfde beam-uit-tak als `diffuse_only`, maar elevatie-afhankelijk i.p.v. permanent).
    De diffuse view-factor wordt ook verlaagd voor het weggenomen hemeldeel (`horizon_diffuse_reduction`)
    — zónder die correctie bleef een laag raam achter een dichte boom/overburen een onrealistisch
    groot diffuus zonvermogen behouden, ook zodra de zon onder de horizon-elevatie zakte (de
    massaknoop-warmbias van juli 2026, Ted's kamer). De boom-hoogte zelf is een seizoens-/
    azimut-benadering — verfijn op de echte straat."""
    beta = math.radians(tilt_deg)
    sky_view = (1.0 + math.cos(beta)) / 2.0          # diffuse view factor (0.5 verticaal, 1.0 plat)
    if horizon_deg > 0.0:
        sky_view *= (1.0 - horizon_diffuse_reduction(horizon_deg))
    diff_on = (diffuse or 0.0) * sky_view
    if diffuse_only or sun_el <= horizon_deg:
        return max(0.0, diff_on)
    zen = math.radians(90.0 - sun_el)
    daz = math.radians(((sun_az - facade_az + 180.0) % 360.0) - 180.0)
    # cos(invalshoek) op een vlak met helling β: standaard zon-op-vlak-formule.
    cos_inc = math.cos(zen) * math.cos(beta) + math.sin(zen) * math.sin(beta) * math.cos(daz)
    direct_on = max(0.0, (direct or 0.0) * max(0.0, cos_inc))
    if beam_iam:
        direct_on *= beam_iam_factor(cos_inc)   # scherende-hoek-transmissie-terugval (stap 2)
    return direct_on + diff_on


# ════════════════════════════════════════════════════════════════════════════════════
#  Wind — Cp-druk per gevel
# ════════════════════════════════════════════════════════════════════════════════════

# Referentiehoogte (m) voor de wind-dynamische druk: de nokhoogte (≈ het trap-skylight, 8.7 m).
# Surface-averaged Cp-tabellen zijn genormaliseerd op ÉÉN referentie-winddruk op gebouwhoogte —
# CONTAM/AIRNET rekenen dan ook één winddruk per gevel. De oude code evalueerde het power-law-
# profiel op de hoogte van élke opening afzonderlijk, waardoor twee openingen op DEZELFDE gevel
# (zelfde Cp) een ΔPe ∝ wind² kregen puur uit hun hoogteverschil: een kunstmatige dwarsstroom-lus
# hotties-raam → koker → kantoor-raam van ~0.27 m³/s bij 3 m/s (~27 ACH; 0.57 bij 6.2 m/s — de
# "ACH 50" uit de juli-assessments). De kalibratie vocht daar alleen maar tegen: cp_shelter op
# zijn vloer (0.10) en vent_eff omlaag om de valse instroom thermisch te dempen — en de tweeling
# blies intussen warme buitenlucht in hotties (de +3°C-fout van 10 juli). Motor van de fix: de
# dynamische druk op één referentiehoogte per gevel; het hóógteverschil blijft wél meedoen in de
# stack-term (Pa_eff = P − ρ·g·z), die fysisch echt is. Gediagnosticeerd + gekwantificeerd in de
# assessment van 10 juli 2026 (zie AIRFLOW_ASSESSMENT.md).
WIND_REF_Z = 8.7

# Effectief-openingsoppervlak per openings-type (fractie van max_open_area_m2 die aerodynamisch
# meedoet, bovenop de vaste Cd). Een wijd open draairaam is zelden het volle kozijngat: de
# openstaande vleugel staat in de stroombaan en de contractie is sterker dan het kale Cd-getal
# (metingen op zij-/onderhangende ramen: effectieve Cd·A grofweg 30–60% van het kozijngat; met
# CD 0.62 → factor 0.5 ≈ effectief 0.31·A, midden in die band). Een (buiten)deur opent vrijwel
# vol (0.9); een kiepraam-stand zit al in `tilt_frac`, dus daar géén extra korting (1.0).
# Per element te overschrijven met `eff_open_frac` in house_model.json (additief).
EFF_OPEN_AREA = {"casement": 0.5, "door": 0.9, "tilt": 1.0}


def _eff_open_area(elem: dict) -> float:
    """Effectief-oppervlak-factor voor een exterieure opening: expliciete `eff_open_frac`
    van het element, anders de `open_type`-default uit EFF_OPEN_AREA, anders 1.0 (roosters
    e.d.: hun doorsnede ís al het effectieve gat)."""
    return float(elem.get("eff_open_frac", EFF_OPEN_AREA.get(elem.get("open_type"), 1.0)))


def cp_coefficient(theta_deg: float) -> float:
    """Surface-averaged druk-coëfficiënt voor een verticale laagbouwgevel als functie
    van de invalshoek θ (0° = wind recht op de gevel → loef; 180° = lij). Twee-cosinus-fit
    op tabel-Cp's: loef ≈ +0.7, zijgevel ≈ −0.4, lij ≈ −0.25. Nog ongeschaald door
    de (leerbare) beschuttingsfactor."""
    t = math.radians(theta_deg)
    return 0.475 * math.cos(t) + 0.3125 * math.cos(2 * t) - 0.0875


def cp_roof(theta_deg: float) -> float:
    """Cp voor een (bijna) plat dakvlak. Anders dan een verticale gevel staat een laag-
    hellend dak op álle windrichtingen onder ónderdruk (zuiging): de wind versnelt
    eroverheen → Bernoulli-onderdruk over het hele vlak, met de loefrand iets minder
    negatief dan de lijrand. Er is géén loef-overdruklob zoals bij een muur. Milde
    richtingsafhankelijkheid rond een surface-averaged ~−0.6 (laagbouw-daktabellen);
    de magnitude wordt verder geschaald door de leerbare `cp_shelter`."""
    return -0.6 + 0.1 * math.cos(math.radians(theta_deg))


def cp_tilted(theta_deg: float, tilt_deg: float) -> float:
    """Cp voor een opening met willekeurige helling: lineair gemengd tussen het muur-
    profiel (tilt 90° = verticaal raam) en het dakprofiel (tilt 0° = plat dakraam). Zo
    krijgt een (bijna) plat dakraam de fysisch juiste alom-zuiging i.p.v. de muur-loeflob
    te lenen — terwijl een gewoon verticaal raam (default tilt 90°) exact het oude gedrag
    houdt. NB: de werkelijke Cp van een dakluik schuift met hóé ver het opengaat (kier →
    dak-achtig, wijd open → de opstaande klep wordt muur-achtiger); dat tweede-orde-effect
    modelleren we niet — dit luik gaat alleen op een kier (~5 cm), dus dak-achtig is juist."""
    w_wall = max(0.0, min(1.0, tilt_deg / 90.0))
    return w_wall * cp_coefficient(theta_deg) + (1.0 - w_wall) * cp_roof(theta_deg)


def wind_pressure(facade_az: float, height: float, wind_speed: float,
                  wind_dir: float, shelter: float, rho: float,
                  tilt_deg: float = 90.0) -> float:
    """Externe winddruk (Pa) op een opening: Cp·½ρU_lokaal². `wind_dir` = richting
    waar de wind vandaan komt (meteorologisch). U_lokaal via een power-law-profiel
    naar de openingshoogte (stedelijke ruwheid). `tilt_deg` (90 = verticaal raam,
    0 = plat dakraam) kiest het Cp-profiel via `cp_tilted`: een (bijna) plat dakvlak
    is op álle windrichtingen zuiging, niet de loef-overdruk van een muur."""
    theta = abs(((wind_dir - facade_az + 180.0) % 360.0) - 180.0)
    z = max(1.5, height)
    u_local = (wind_speed or 0.0) * (z / 10.0) ** 0.30
    return shelter * cp_tilted(theta, tilt_deg) * 0.5 * rho * u_local * u_local


# ════════════════════════════════════════════════════════════════════════════════════
#  Luchtdichtheid + orifice-flow
# ════════════════════════════════════════════════════════════════════════════════════

def air_density(temp_c: float) -> float:
    return P_ATM / (R_AIR * (temp_c + 273.15))


# Statische, áltijd-aanwezige zonwering per `shading`-label (fractie zon die het glas
# haalt): geen, een balkon/overstek erboven, een vaste lichte dubbel-papieren lamella die
# ~1/3 van het raam bedekt en weinig verduistert (translucent → ~0.9: de 2/3 vrije glas
# laat alles door, de bedekte 1/3 nog het meeste), diep beschaduwd (b.v. onder een terras,
# alleen ochtendzon), of een binnenzonwering. Een bedíénbare zonwering (gordijn/scherm)
# staat hier los van en wordt er multiplicatief overheen gelegd (zie _shade_factor) — de
# twee lagen werken tegelijk op hetzelfde raam.
SHADING_FACTOR = {"none": 1.0, "overhang": 0.7, "lamella": 0.9, "deep": 0.4,
                  "blind": 0.35, "shade": 0.2}


def _shade_factor(wid: str, w: dict, states: dict) -> float:
    """Zon-transmissiefractie door een raam = de statische, altijd-aanwezige zonwering
    (`shading`, b.v. een vaste lamella of overstek) × de bedienbare zonwering (`shade`)
    die je meldt. De twee lagen vermenigvuldigen, zodat een raam met zówel een vaste
    lamella áls een bedienbare zonwering ze allebei tegelijk meetelt. Een niet-gemelde
    bedienbare zonwering geldt als z'n default-stand (voor het simpele type = open, ×1.0)
    — zo geeft 'niet gemeld' dezelfde transmissie als de defaultstand (geen sprong).

    Twee `shade`-typen:
    - **simpel scherm/gordijn** (`factor`): open ×1.0, dicht ×factor, half ertussenin —
      vaste opaciteit, je trekt 'm dicht of open (b.v. Teds verduisteringsgordijn).
    - **coverage-lamella** (`coverage` + `paper`): vaste papier-opaciteit, variabele
      dékking. De gemelde stand kiest een dekkingsfractie (b.v. open 0.30 / half 0.50 /
      dicht 1.00); transmissie = 1 − dekking·(1 − papier). Het onbedekte glas laat alles
      door, alleen het bedekte deel dempt (b.v. de woonkamer-lamella die je tot 30/50/100%
      uittrekt)."""
    base = SHADING_FACTOR.get(w.get("shading", "none"), 1.0)
    sh = w.get("shade")
    if not sh:
        return base
    rep = states.get(wid + "_shade")
    cov = sh.get("coverage")
    if cov:
        paper = float(sh.get("paper", 0.7))
        key = str(rep).strip().lower() if rep is not None else sh.get("default", "open")
        frac = cov.get(key)
        if frac is None:
            try:                                    # losse dekkingsfractie 0..1 toegestaan
                frac = max(0.0, min(1.0, float(key)))
            except (TypeError, ValueError):
                frac = cov.get(sh.get("default", "open"), 0.0)
        return base * (1.0 - float(frac) * (1.0 - paper))
    mult = 1.0
    if rep is not None:
        s = str(rep).strip().lower()
        if s in ("half", "kier"):
            mult = 0.5 * (1.0 + float(sh.get("factor", 0.2)))
        elif s in ("dicht", "closed", "toe", "1", "true", "ja"):
            mult = float(sh.get("factor", 0.2))
        # open/0/false/nee (of onbekend) → mult blijft 1.0
    return base * mult

# Crossover-drukval (Pa) tussen het laminaire (lineaire) en turbulente (√) regime. De
# orifice-wet Q∝√|ΔP| heeft een oneindige helling bij ΔP=0, wat de Newton-Jacobiaan
# slecht conditioneert (een grote opening egaliseert de druk → ΔP≈0). Onder DP_LAM gaan
# we lineair over met aansluitende waarde+helling → eindige Jacobiaan, stabiele convergentie
# (standaard CONTAM/AIRNET-aanpak). Fysisch ook reëel: bij heel kleine ΔP is de stroming
# laminair, niet turbulent.
DP_LAM = 0.1


def _massflow(dP: float, Cd: float, area: float, rho_from: float, rho_to: float) -> float:
    """Massadebiet (kg/s) door een opening, positief = uít de 'from'-zone. dP = druk
    aan de from-kant min de to-kant (Pa). Twee-regime (laminair onder DP_LAM)."""
    if area <= 0.0:
        return 0.0
    rho = rho_from if dP >= 0.0 else rho_to
    coef = Cd * area * math.sqrt(2.0 * rho)      # mdot = coef·√|ΔP| in het turbulente regime
    a = abs(dP)
    if a >= DP_LAM:
        m = coef * math.sqrt(a)
    else:
        m = coef * math.sqrt(DP_LAM) * (a / DP_LAM)   # lineair, aansluitend op DP_LAM
    return m if dP >= 0.0 else -m


# ════════════════════════════════════════════════════════════════════════════════════
#  Luchtstroomnetwerk — los de zone-drukken op zodat massa behouden blijft
# ════════════════════════════════════════════════════════════════════════════════════

def solve_network(zones: list[str], openings: list[dict], zone_temps: dict[str, float],
                  outside_temp: float, P_init: list[float] | None = None) -> dict:
    """Los het meerzone-netwerk op. `openings` is een lijst dicts:
        {"a": zone, "b": zone|"outside", "area": m², "Cd": -, "z": m, "Pe": Pa}
    Pe is de externe winddruk aan de buitenkant (alleen voor exterieuropeningen; 0 voor
    interne deuren). `P_init` = warme-start-drukken (vorige stap) → minder iteraties.
    Geeft terug: {"flows": [m³/s a→b per opening], "fresh": {zone: m³/s verse buitenlucht
    in}, "pressures": {zone: Pa}, "P": [Pa per zone]}."""
    idx = {z: i for i, z in enumerate(zones)}
    n = len(zones)
    rho_out = air_density(outside_temp)
    rho_z = {z: air_density(zone_temps.get(z, outside_temp)) for z in zones}

    def residual(P: list[float]) -> list[float]:
        res = [0.0] * n
        for op in openings:
            a = op["a"]
            ia = idx[a]
            z = op["z"]
            rho_a = rho_z[a]
            Pa_eff = P[ia] - rho_a * G * z
            if op["b"] == "outside":
                Pb_eff = op["Pe"] - rho_out * G * z
                rho_b = rho_out
            else:
                ib = idx[op["b"]]
                rho_b = rho_z[op["b"]]
                Pb_eff = P[ib] - rho_b * G * z
            md = _massflow(Pa_eff - Pb_eff, op["Cd"], op["area"], rho_a, rho_b)
            res[ia] += md
            if op["b"] != "outside":
                res[idx[op["b"]]] -= md
        return res

    def sse(r):
        return sum(v * v for v in r)

    P = list(P_init) if P_init and len(P_init) == n else [0.0] * n
    r = residual(P)
    for _ in range(40):
        if max(abs(v) for v in r) < 1e-6:
            break
        # Numerieke Jacobiaan.
        J = [[0.0] * n for _ in range(n)]
        eps = 0.02
        for j in range(n):
            P[j] += eps
            rp = residual(P)
            P[j] -= eps
            for i in range(n):
                J[i][j] = (rp[i] - r[i]) / eps
        delta = solve_linear(J, [-v for v in r])
        if delta is None:
            break
        # Backtracking line search: neem de grootste stapfractie die de residu-norm
        # daadwerkelijk verkleint. Zonder dit kan de Newton-iteratie tussen twee
        # toestanden oscilleren (sterke deurkoppeling + de √-niet-lineariteit) en nooit
        # convergeren.
        sse0 = sse(r)
        alpha = 1.0
        r_try = r
        for _ in range(24):
            P_try = [P[j] + alpha * delta[j] for j in range(n)]
            r_try = residual(P_try)
            if sse(r_try) < sse0:
                break
            alpha *= 0.5
        else:
            break   # geen verbeterende stap meer → klaar
        P = P_try
        r = r_try

    # Debieten + verse-lucht-aanvoer reconstrueren.
    flows = []
    fresh = {z: 0.0 for z in zones}
    for op in openings:
        a = op["a"]
        ia = idx[a]
        z = op["z"]
        rho_a = rho_z[a]
        Pa_eff = P[ia] - rho_a * G * z
        if op["b"] == "outside":
            Pb_eff = op["Pe"] - rho_out * G * z
            rho_b = rho_out
        else:
            rho_b = rho_z[op["b"]]
            Pb_eff = P[idx[op["b"]]] - rho_b * G * z
        md = _massflow(Pa_eff - Pb_eff, op["Cd"], op["area"], rho_a, rho_b)
        q_vol = md / (rho_a if md >= 0 else rho_b)   # m³/s, positief a→b
        flows.append(q_vol)
        if op["b"] == "outside" and md < 0:          # buitenlucht stroomt zone a in
            fresh[a] += -md / rho_out
        elif op["b"] != "outside":
            # interne deur: telt niet als 'verse' lucht, maar wel voor menging (apart).
            pass
    return {"flows": flows, "fresh": fresh,
            "pressures": {z: P[idx[z]] for z in zones}, "P": P}


# ════════════════════════════════════════════════════════════════════════════════════
#  Openingen — lognaar-toestand reconstrueren + effectief oppervlak
# ════════════════════════════════════════════════════════════════════════════════════

def _open_frac(value, element: dict) -> float:
    """Zet een gerapporteerde waarde (getal 0..1, of "open"/"tilt"/"closed"/"dicht")
    om naar een open-fractie."""
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    s = str(value).strip().lower()
    if s in ("open", "1", "true", "ja"):
        return 1.0
    if s in ("tilt", "kier", "kiep"):
        return float(element.get("tilt_frac", 0.15))
    if s in ("closed", "dicht", "0", "false", "nee"):
        return 0.0
    return 0.0


def openings_at(log: list[dict], when: datetime) -> dict:
    """Actieve gerapporteerde toestand per element op tijdstip `when`, voorwaarts
    geaccumuleerd: elk element houdt zijn laatst-gezette waarde tot het opnieuw gemeld
    wordt. Zo kun je kleine, losse wijzigingen melden (één raam) zonder de rest te
    herhalen, en weerspiegelt de toestand wat écht open/dicht staat. Lege dict als er
    niets vóór `when` is gelogd."""
    entries = []
    for entry in log:
        try:
            t = datetime.fromisoformat(entry["t"])
        except (ValueError, TypeError, KeyError):
            continue
        if t <= when:
            entries.append((t, entry.get("states", {}) or {}))
    entries.sort(key=lambda e: e[0])
    state: dict = {}
    for _, st in entries:
        state.update(st)
    return state


def openings_fingerprint(log: list[dict], start: datetime, end: datetime) -> str:
    """Deterministische vingerafdruk van de openingen-log zoals die het venster
    [start, end] raakt: de voorwaarts-geaccumuleerde toestand op `start` (een oudere
    log-edit die de beginstand wijzigt telt dus mee) + alle snapshots ín het venster.
    Verandert alléén wanneer een log-correctie de sim over dit venster écht anders
    maakt — de poort waarop `backfill_rmse_history` een leercurve-punt herberekent.
    Sim-drift (params, weerdata, WU-herschaling) raakt de vingerafdruk níét."""
    entries = []
    for entry in log:
        try:
            t = datetime.fromisoformat(entry["t"])
        except (ValueError, TypeError, KeyError):
            continue
        if start < t <= end:
            entries.append((t.isoformat(), entry.get("states", {}) or {}))
    entries.sort(key=lambda e: e[0])
    payload = json.dumps([openings_at(log, start), entries], sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _norm_ac_room(value) -> str | None:
    """Normaliseer de gerapporteerde AC-kamer naar een room-id, of None (geen airco)."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("", "geen", "none", "off", "uit", "-"):
        return None
    return s


def ac_changes(log: list[dict]) -> list[tuple]:
    """Chronologische (tijdstip, room-id|None) AC-toewijzingen uit de openingen-log: elk
    snapshot dat de `ac_room`-sleutel zet. Voorwaarts uit te lezen met `ac_room_at`."""
    out = []
    for entry in log:
        st = entry.get("states", {}) or {}
        if AC_STATE_KEY not in st:
            continue
        try:
            t = datetime.fromisoformat(entry["t"])
        except (ValueError, TypeError, KeyError):
            continue
        out.append((t, _norm_ac_room(st[AC_STATE_KEY])))
    out.sort(key=lambda c: c[0])
    return out


def ac_room_at(changes: list[tuple], when: datetime) -> str | None:
    """Welke kamer de airco had op tijdstip `when` (voorwaarts geaccumuleerd), of None."""
    room = None
    for t, r in changes:
        if t <= when:
            room = r
        else:
            break
    return room


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


# ── Verwarmings-uitsluiting (auto, gedreven door de tado-verwarmingsvlag) ─────────────
# Spiegelbeeld van de airco-uitsluiting, maar dán warm: het thermische model heeft géén
# verwarmingsterm, dus een kamer die actief gestookt wordt (b.v. Ted's kamer op een avond
# met een driftbui, of gewoon de cv 's winters) leest wármer dan de fysica kan verklaren.
# Fitten op die warmte zou de kamer-params + de gedeelde globalen + RMSE/leercurve vervuilen.
#
# Anders dan de airco (die je handmatig in de modal meldt) komt deze status rechtstreeks uit
# de tado-API: Project 6 schrijft per history-sample in window_data.json een `heat`-vlag (en
# de huidige `heating`-status per kamer). De uitsluiting is dus PER SAMPLE en exact getimed —
# géén guard-venster nodig zoals bij de airco. In de winter, wanneer er doorgaans gestookt
# wordt, vallen die samples vanzelf weg en kalibreert de tweeling schoon op de stook-vrije
# momenten; buiten het stookseizoen verandert er niets.


def collect_heating_on(house: dict, wd: dict, since: datetime) -> dict[str, set]:
    """Per sensorkamer de tijdstippen (datetime) sinds `since` waarop tado meldde dat er
    gestookt werd (`heat`-vlag in de window_data.json-history). Leeg → geen stook-samples.
    Leest dezelfde history als `collect_actual`, zodat de tijdstippen exact matchen."""
    out: dict[str, set] = {}
    for rid, room in house.get("rooms", {}).items():
        wd_key = room.get("from_window_data")
        if not wd_key or wd_key not in wd.get("rooms", {}):
            continue
        on = set()
        for s in wd["rooms"][wd_key].get("history", []):
            try:
                ts = datetime.fromisoformat(s["t"])
            except (ValueError, TypeError, KeyError):
                continue
            if ts >= since and s.get("temp") is not None and s.get("heat"):
                on.add(ts)
        if on:
            out[rid] = on
    return out


def heating_now(house: dict, wd: dict) -> dict[str, bool]:
    """Per sensorkamer of tado nú meldt dat er gestookt wordt (`heating`-vlag op de kamer in
    window_data.json) — voor de dashboard-chip + de 'niet-gekalibreerd'-melding."""
    out: dict[str, bool] = {}
    for rid, room in house.get("rooms", {}).items():
        wd_key = room.get("from_window_data")
        if wd_key and wd_key in wd.get("rooms", {}):
            out[rid] = bool(wd["rooms"][wd_key].get("heating"))
    return out


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


# ── Pauze-uitsluiting (huis-breed, handmatig via de modal) ───────────────────────────
# Spiegelbeeld van de AC-/verwarmings-uitsluiting, maar huis-breed i.p.v. per kamer: als iemand
# anders thuis is en de ramen/roosters/deuren bedient zonder dat te melden, is de openingen-log
# voor de hele duur onbetrouwbaar — niet slechts voor één kamer. In plaats van een kamer-
# uitsluiting is dit dus een LEER-GATE (naast de bestaande anomalie-poort `should_hold_learning`
# in main): calibrate()/checkpoint_step/backfill_rmse_history slaan een gepauzeerd venster over,
# maar de voorspelling (simulate()) blijft gewoon draaien — de fout wordt nog getoond, alleen
# niet gebruikt om te leren. Geleerde params worden dus nooit gereset of gedegradeerd tijdens
# een pauze: ze bevriezen exact waar ze stonden en leren verder vanaf dat punt zodra hervat.


def _norm_paused(value) -> bool:
    """Normaliseer de gerapporteerde pauze-stand naar een bool."""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    return s in ("true", "1", "paused", "gepauzeerd", "aan", "ja")


def pause_changes(log: list[dict]) -> list[tuple]:
    """Chronologische (tijdstip, paused-bool) wijzigingen uit de openingen-log: elk snapshot
    dat de `paused`-sleutel zet. Voorwaarts uit te lezen met `paused_at`."""
    out = []
    for entry in log:
        st = entry.get("states", {}) or {}
        if PAUSE_STATE_KEY not in st:
            continue
        try:
            t = datetime.fromisoformat(entry["t"])
        except (ValueError, TypeError, KeyError):
            continue
        out.append((t, _norm_paused(st[PAUSE_STATE_KEY])))
    out.sort(key=lambda c: c[0])
    return out


def paused_at(changes: list[tuple], when: datetime) -> bool:
    """Was het huis gepauzeerd op tijdstip `when` (voorwaarts geaccumuleerd)? Vóór de eerste
    melding: niet gepauzeerd (False)."""
    val = False
    for t, v in changes:
        if t <= when:
            val = v
        else:
            break
    return val


def paused_intervals(changes: list[tuple], now: datetime) -> list[tuple]:
    """Zet de boolean-wisselingen om in (start, eind)-tupels waarin het huis gepauzeerd was. Een
    nog-actieve pauze (geen latere 'uit'-melding) loopt open-eindig door tot `now` — dat sluit
    recente samples vanzelf uit, zónder apart guard-venster zoals bij de airco (die guard bestaat
    juist omdát een 'staat nu hier'-melding niet met een interval-eind samenvalt; hier IS
    'nu, nog actief' letterlijk het interval-eind)."""
    intervals = []
    start = None
    for t, v in changes:
        if v and start is None:
            start = t
        elif not v and start is not None:
            intervals.append((start, t))
            start = None
    if start is not None:
        intervals.append((start, now))
    return intervals


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


def _default_frac(element: dict, kind: str) -> float:
    """Basistoestand vóór enige rapportage: ramen dicht, binnendeuren open, roosters
    op trickle-stand (tenzij het huismodel een `default_state` geeft)."""
    if "default_state" in element:
        return _open_frac(element["default_state"], element)
    return {"window": 0.0, "vent": 1.0, "door": 1.0}.get(kind, 0.0)


def build_openings(house: dict, states: dict, weather: dict, params: dict,
                   zone_temps: dict, outside_temp: float) -> list[dict]:
    """Bouw de openingenlijst voor het netwerk uit de huidige toestanden + wind."""
    shelter = params["cp_shelter"]
    cd = CD                       # vaste fysische ontladingscoëfficiënt (niet geleerd)
    rho_out = air_density(outside_temp)
    wind_s, wind_d = weather.get("wind_speed", 0.0), weather.get("wind_dir", 0.0)
    ops = []

    def ext(elem_id, elem, kind):
        frac = _open_frac(states[elem_id], elem) if elem_id in states else _default_frac(elem, kind)
        area = frac * elem.get("max_open_area_m2", elem.get("area_m2", 0.0)) * _eff_open_area(elem)
        if area <= 0:
            return
        # Dynamische druk op WIND_REF_Z (één referentie-winddruk per gevel, CONTAM-conventie) —
        # het element-hoogteverschil doet alleen mee in de stack-term via `z` hieronder. Zie de
        # toelichting bij WIND_REF_Z: per-opening-hoogte gaf een kunstmatige zelfde-gevel-lus.
        pe = wind_pressure(elem.get("facade_azimuth_deg", 0.0),
                           WIND_REF_Z, wind_s, wind_d, shelter, rho_out,
                           elem.get("tilt_deg", 90.0))
        ops.append({"a": elem["room"], "b": "outside", "area": area, "Cd": cd,
                    "z": elem.get("center_height_m", 1.5), "Pe": pe, "id": elem_id})

    for wid, w in house.get("windows", {}).items():
        ext(wid, w, "window")
    for vid, v in house.get("vents", {}).items():
        ext(vid, v, "vent")
    for did, d in house.get("doors", {}).items():
        frac = _open_frac(states[did], d) if did in states else _default_frac(d, "door")
        area = frac * d.get("area_m2", 0.0)
        if area <= 0:
            continue
        a, b = d["between"]
        ops.append({"a": a, "b": b, "area": area, "Cd": cd,
                    "z": d.get("center_height_m", 1.0), "Pe": 0.0, "id": did})
    # Per-zone infiltratielek (altijd aanwezig, klein) → het netwerk blijft welgesteld,
    # óók als een zone helemaal dicht zit (b.v. badkamer: deur dicht + afzuiging uit) —
    # anders wordt die knoop singulier en ontspoort de hele drukoplossing.
    for zone in list(house.get("rooms", {})) + list(house.get("junctions", {})):
        ops.append({"a": zone, "b": "outside", "area": LEAK_AREA, "Cd": cd,
                    "z": 1.5, "Pe": 0.0, "id": f"_leak_{zone}"})
    return ops


def door_mix(house: dict, flows: list[float], openings: list[dict]) -> dict:
    """Per (kamer→kamer) het absolute volumedebiet door open binnendeuren (m³/s),
    voor de advectieve thermische menging."""
    mix = {}
    for op, q in zip(openings, flows):
        if op["b"] == "outside":
            continue
        a, b = op["a"], op["b"]
        mix.setdefault((a, b), 0.0)
        mix[(a, b)] += abs(q)
    return mix


def stair_gradient(points: list, max_grad: float = STAIR_STRAT_MAX_GRAD) -> float:
    """Verticale temperatuurgradiënt γ (°C/m, ≥0) als de kleinste-kwadraten-HELLING van kamertemp
    t.o.v. deurhoogte door de gekoppelde (open-deur) kamers — de kamers zijn de proxy-meting van
    het koker-profiel, dus γ ijkt zich op de data i.p.v. op een geraden constante. `points` =
    lijst (hoogte_m, temp_°C). <2 punten op verschillende hoogtes → 0 (vlak). Geklemd op
    [0, max_grad]; een inversie (top koeler → negatieve helling) wordt niet doorgezet."""
    pts = [(z, t) for z, t in points if t is not None]
    if len({z for z, _ in pts}) < 2:
        return 0.0
    n = len(pts)
    mz = sum(z for z, _ in pts) / n
    mt = sum(t for _, t in pts) / n
    den = sum((z - mz) ** 2 for z, _ in pts)
    if den <= 0.0:
        return 0.0
    slope = sum((z - mz) * (t - mt) for z, t in pts) / den
    return max(0.0, min(max_grad, slope))


def buoyant_door_exchange(area_m2: float, t_a: float, t_b: float,
                          height_m: float = DOOR_HEIGHT_M) -> float:
    """Tweerichtings-uitwisselingsdebiet (m³/s, één kant van de counterflow) door een open
    binnendeur op temperatuurverschil (Brown–Solvason): Q = C·A·√(g·H·ΔT/T̄). Nul bij een dichte
    deur (area 0) of gelijke temperaturen. Dit is de menging die het netwerk-nettodebiet mist."""
    if area_m2 <= 0.0 or t_a is None or t_b is None:
        return 0.0
    dt = abs(t_a - t_b)
    if dt <= 0.0:
        return 0.0
    t_mean_k = 273.15 + 0.5 * (t_a + t_b)
    return BUOY_EXCH_C * area_m2 * math.sqrt(G * height_m * dt / t_mean_k)


def stair_crown(irr_roof_wm2: float) -> float:
    """Zon-kroon (°C) bovenop de γ-lijn voor de tóp-weergave van een gestratificeerde koker —
    de skylight-/dak-zon die bovenin landt en boven de hoogste deur niet weggemengd wordt."""
    return max(0.0, min(STAIR_CROWN_MAX, STAIR_CROWN_K * (irr_roof_wm2 or 0.0)))


def _stratify_zones(house: dict) -> dict:
    """Zones met "stratify": true → hun verticale-koker-metadata voor de stratificatie-gradiënt:
    `z_mean` (advectie-referentiehoogte, = gemiddelde deurhoogte), de gekoppelde `doors`
    {buurzone: hoogte}, en de koker-extent `z_lo`/`z_hi` (laagste/hoogste opening) voor de
    top/onder-weergave. Zonder de vlag (of zonder deuren) → afwezig, dus geen effect."""
    out = {}
    for zid, r in house.get("rooms", {}).items():
        if not r.get("stratify"):
            continue
        doors = {}
        for d in house.get("doors", {}).values():
            pair = d.get("between", [])
            if len(pair) == 2 and zid in pair:
                other = pair[0] if pair[1] == zid else pair[1]
                doors[other] = d.get("center_height_m", 0.0)
        if len(doors) < 2:
            continue
        heights = list(doors.values())
        for coll in ("windows", "vents"):
            for w in house.get(coll, {}).values():
                if w.get("room") == zid:
                    heights.append(w.get("center_height_m", 0.0))
        out[zid] = {"z_mean": sum(doors.values()) / len(doors), "doors": doors,
                    "z_lo": min(heights), "z_hi": max(heights)}
    return out


def _stair_gamma(info: dict, temps: dict, open_others: set | None = None) -> float:
    """De verticale gradiënt γ (°C/m) voor één koker: de kleinste-kwadraten-helling van de
    gekoppelde kamertemps t.o.v. hun deurhoogte. `temps` = actuele zone-luchttemps. `open_others`
    = de kamers waarvan de koker-deur NÚ open staat (None = alle deuren meetellen); een dichte deur
    ontkoppelt die kamer, dus die valt uit de regressie."""
    pts = [(zh, temps[o]) for o, zh in info["doors"].items()
           if o in temps and (open_others is None or o in open_others)]
    return stair_gradient(pts)


# ════════════════════════════════════════════════════════════════════════════════════
#  2-knoops RC-thermisch model
# ════════════════════════════════════════════════════════════════════════════════════

def room_base_capacitances(room: dict) -> tuple[float, float, float]:
    """Fysische basis (C_air, C_mass, exterieur-UA) uit geometrie, vóór de leer-schalen.
    C_air ≈ luchtmassa×cp (× factor voor meubilair-lucht); C_mass ≈ wandmassa; UA uit
    schiloppervlak."""
    vol = room.get("volume_m3", 40.0)
    wall = room.get("exterior_wall_m2", 0.4 * vol)
    roof = room.get("roof_m2", 0.0)             # bovenste verdieping: dakvlak (anders 0)
    c_air = vol * 1.2 * CP_AIR * 3.0          # ×3: effectieve binnenlucht + lichte inboedel
    c_mass = (wall + roof) * 90000.0            # J/K per m² schil (baksteen/pleister + dak, ~slow)
    ua = wall * 1.0                             # W/K (matig geïsoleerde gevel; dak via UA_roof)
    return c_air, c_mass, ua


def _zone_thermal_params(house: dict, params: dict) -> dict:
    """Per zone (kamer én junctie) de geschaalde thermische parameters. Kamers krijgen
    geometrie + geleerde schalen; junct, gang/overloop) generieke defaults (geen zonwinst)."""
    par = {}
    for rid, r in house.get("rooms", {}).items():
        c_air0, c_mass0, ua0 = room_base_capacitances(r)
        vol = r.get("volume_m3", 40.0)
        p = params.get(rid, {})
        par[rid] = {
            "C_a": c_air0 * p.get("c_air", 1.0),
            "C_m": c_mass0 * p.get("c_mass", 1.0),
            "H_am": ua0 * 8.0 * p.get("h_am", 1.0),
            "UA_env": ua0 * 0.5 * p.get("ua_env", 1.0),
            "UA_mass": ua0 * 0.5 * p.get("ua_mass", 1.0),
            "solar": p.get("solar_gain", 1.0), "f_air": p.get("f_air", 0.4),
            # Buur-geleiding via de woningscheidende muren → NEIGHBOR_TEMP. Basis ~ schil-UA
            # (party-muur-oppervlak is van dezelfde orde als de gevel); de geleerde schaal
            # vangt de werkelijke grootte. Een hoekkamer met minder buren leert 'm lager.
            "UA_party": ua0 * p.get("ua_party", 1.0),
            # Interne warmtelast (W) bij profiel = 1.0; het dag/nacht-profiel schaalt 'm per stap.
            "Q_int_base": vol * INTERNAL_GAIN_WM3 * p.get("q_int", 1.0),
            # Dak-sol-air-koppeling (W/K) op de massaknoop. Basis 0 (→ inactief) tenzij de kamer
            # een `roof_m2` heeft; de geleerde `ua_roof` schaalt de grootte.
            "UA_roof": r.get("roof_m2", 0.0) * ROOF_U * p.get("ua_roof", 1.0),
        }
    for jid, j in house.get("junctions", {}).items():
        vol = j.get("volume_m3", 15.0)
        c_air0 = vol * 1.2 * CP_AIR * 3.0
        par[jid] = {"C_a": c_air0, "C_m": c_air0 * 2.0, "H_am": 15.0,
                    "UA_env": 3.0, "UA_mass": 1.0, "solar": 0.0, "f_air": 1.0,
                    "UA_party": 0.0, "Q_int_base": 0.0, "UA_roof": 0.0}
    return par


def simulate(house: dict, params: dict, timeline: list[dict],
             seed: dict, calib_only_rooms: set | None = None,
             snapshot_t: datetime | None = None,
             tm_seed: dict | None = None) -> dict:
    """Integreer het 2-knoops thermische model over `timeline` (lijst stappen met drivers).
    Elke stap: {"t", "T_out", "irr": {room: W}, "states", "weather", "dt"}. `seed` =
    {zone: T_start °C}. Geeft per sensorkamer de voorspelde luchttemp-reeks terug.

    `snapshot_t` (optioneel): legt de volledige zone-toestand (álle zones, incl. junctions)
    vast op het eerste tijdstip ≥ `snapshot_t` — `Ta_now`/`Tm_now`. Zo kan het dashboard de
    snapshot (ACH, flows, voorspelde temp) op "nu" tonen i.p.v. op de eind-/vooruitblikstap.

    `tm_seed` (optioneel): expliciete beginwaarde voor de massaknoop per zone, voor een
    caller die 'm al kent (bv. uit een eerdere simulate()-aanloop via `Tm_now`) i.p.v. de
    standaard warme blend hieronder — puur additief, `None`/ontbrekende zone → ongewijzigd
    gedrag.

    De integratie is *impliciet* (backward Euler): per substap wordt het gekoppelde
    lineaire stelsel voor alle lucht- + massaknopen ineens opgelost (solve_linear). Dat
    is onvoorwaardelijk stabiel — sterke deur-/ventilatiekoppeling laat de expliciete
    Euler anders ontsporen."""
    rooms = house.get("rooms", {})
    zones = list(rooms.keys()) + list(house.get("junctions", {}).keys())
    par = _zone_thermal_params(house, params)
    veff = params.get("vent_eff", 1.0)
    rho_cp = 1.2 * CP_AIR
    strat = _stratify_zones(house)   # verticale-koker-zones (opt-in via "stratify"); leeg → geen effect

    Ta = {z: seed.get(z, timeline[0]["T_out"]) for z in zones}
    # Massaknoop richting een warme blend (NEIGHBOR_TEMP) i.p.v. = luchtknoop: met de sim-only
    # WARMUP_H aanloop equilibreert hij ruim vóór het residu-venster (massa-tijdconstante ~uren),
    # zodat zijn beginwaarde geen vrije laagfrequente bias meer is die de fit scheeftrekt.
    # `tm_seed` overschrijft dit per-zone wanneer een caller de al-geëvolueerde massatemp
    # heeft (zie docstring); anders ongewijzigd de warme blend.
    Tm = {z: (tm_seed[z] if tm_seed is not None and tm_seed.get(z) is not None
              else 0.5 * (Ta[z] + _NEIGHBOR_TEMP))
          for z in zones}
    out = {rid: [] for rid in rooms if (calib_only_rooms is None or rid in calib_only_rooms)}

    n = len(zones)
    zi = {z: k for k, z in enumerate(zones)}
    P_warm = None
    Ta_snap = Tm_snap = None
    solver_failures = 0   # bijna-singuliere thermische stelsels (zie de substap-break hieronder)

    for step in timeline:
        T_out = step["T_out"]
        ops = build_openings(house, step["states"], step["weather"], params, Ta, T_out)
        net = solve_network(zones, ops, Ta, T_out, P_init=P_warm)
        P_warm = net["P"]
        fresh = net["fresh"]
        mix = door_mix(house, net["flows"], ops)
        # Trappenhuis-stratificatie: γ = kleinste-kwadraten-helling door de OPEN-deur-kamers
        # (proxy-meting van het profiel), plus de Brown–Solvason-counterflow per open koker-deur —
        # de tweerichtings-menging die het netto-netwerkdebiet mist. De counterflow is een fysiek
        # orifice-verschijnsel (zelfde argument als de vaste `cd`) en gaat dus BUITEN de geleerde
        # × vent_eff om — die efficiency hoort bij de netto-advectie; erover heen schalen dempte
        # de pinning ~3× en liet de sensorloze koker onder zijn open-deur-kamers zweven. Deur
        # dicht → geen term → warmte poolt bovenin. Leeg `strat` (geen opt-in) → alles ongewijzigd.
        strat_step = {}
        ex_mix = {}
        if strat:
            door_area = {}
            for op in ops:
                if op.get("b") != "outside":
                    k = (op["a"], op["b"])
                    door_area[k] = door_area.get(k, 0.0) + op["area"]
            for sid, info in strat.items():
                open_others = {o for o in info["doors"]
                               if (sid, o) in door_area or (o, sid) in door_area}
                gamma = _stair_gamma(info, Ta, open_others)
                strat_step[sid] = (gamma, open_others)
                for other in open_others:
                    zh = info["doors"][other]
                    area = door_area.get((sid, other), 0.0) + door_area.get((other, sid), 0.0)
                    q_ex = buoyant_door_exchange(area, Ta[sid] + gamma * (zh - info["z_mean"]),
                                                 Ta.get(other, T_out))
                    if q_ex > 0.0:
                        key = (sid, other) if (sid, other) in mix else (other, sid)
                        ex_mix[key] = ex_mix.get(key, 0.0) + q_ex
        # Advectieve geleiding per (zone↔zone) deur (W/K) en per zone naar buiten (vent);
        # de buoyancy-counterflow komt er ongedempt (zonder × vent_eff) bovenop.
        gdoor = {key: rho_cp * qm * veff for key, qm in mix.items()}
        for key, q_ex in ex_mix.items():
            gdoor[key] = gdoor.get(key, 0.0) + rho_cp * q_ex
        gvent = {z: rho_cp * fresh.get(z, 0.0) * veff for z in zones}
        # Het γ-hoogte-offset als energie-behoudende symmetrische bron in b[] (+ kamer, − koker):
        # elke verdiepingsdeur mengt tegen T_koker + γ·(z − z_mid) i.p.v. het vlakke gemiddelde.
        strat_terms = []
        for sid, (gamma, open_others) in strat_step.items():
            if gamma == 0.0:
                continue
            info = strat[sid]
            for other in open_others:
                if other not in zi:
                    continue
                zh = info["doors"][other]
                g = gdoor.get((sid, other), 0.0) + gdoor.get((other, sid), 0.0)
                if g == 0.0:
                    continue
                strat_terms.append((zi[other], zi[sid], g * gamma * (zh - info["z_mean"])))

        nsub = max(1, int(math.ceil(step["dt"] / SUBSTEP_S)))
        h = step["dt"] / nsub
        # Dak-sol-air-effectieve buitentemp per kamer (alleen relevant waar UA_roof > 0): de
        # horizontale instraling tilt 'm overdag op, 's nachts (zon onder de horizon) trekt de
        # heldere-hemel-straling 'm omlaag. Per stap (de instraling is al stap-gemiddeld).
        irr_roof = step.get("irr_roof", {})
        night = step.get("sun_el", 90.0) <= 0.0
        t_solair = {z: T_out + ROOF_SOLAR_GAIN * irr_roof.get(z, 0.0)
                    - (ROOF_SKY_COOLING if night else 0.0) for z in zones}
        for _ in range(nsub):
            # Bouw het 2N-stelsel A·x = b, x = [Ta_z0, Tm_z0, Ta_z1, Tm_z1, ...].
            A = [[0.0] * (2 * n) for _ in range(2 * n)]
            b = [0.0] * (2 * n)
            for z in zones:
                k = zi[z]
                ia, im = 2 * k, 2 * k + 1
                pa = par[z]
                q_solar = step["irr"].get(z, 0.0) * pa["solar"]
                # Buur-geleiding (party walls → NEIGHBOR_TEMP) en interne warmtelast (W,
                # dag/nacht-profiel). Beide werken op de luchtknoop: de buur als een vaste
                # warme rand, de interne last als bron.
                ua_party = pa.get("UA_party", 0.0)
                q_int = pa.get("Q_int_base", 0.0) * internal_gain_profile(step["t"])
                # Luchtknoop.
                A[ia][ia] += pa["C_a"] / h + gvent[z] + pa["UA_env"] + pa["H_am"] + ua_party
                A[ia][im] += -pa["H_am"]
                b[ia] += (pa["C_a"] / h * Ta[z] + gvent[z] * T_out + pa["UA_env"] * T_out
                          + pa["f_air"] * q_solar + ua_party * _NEIGHBOR_TEMP + q_int)
                # Massaknoop (+ dak-sol-air-koppeling naar de effectieve dak-buitentemp).
                ua_roof = pa.get("UA_roof", 0.0)
                A[im][im] += pa["C_m"] / h + pa["H_am"] + pa["UA_mass"] + ua_roof
                A[im][ia] += -pa["H_am"]
                b[im] += (pa["C_m"] / h * Tm[z] + pa["UA_mass"] * T_out
                          + (1.0 - pa["f_air"]) * q_solar + ua_roof * t_solair[z])
            # Deur-koppeling (advectief, impliciet).
            for (za, zb), g in gdoor.items():
                ka, kb = zi[za], zi[zb]
                A[2 * ka][2 * ka] += g
                A[2 * ka][2 * kb] += -g
                A[2 * kb][2 * kb] += g
                A[2 * kb][2 * ka] += -g
            # Stratificatie-hoogte-offset als symmetrische bron (kamer +, koker −): behoudt energie
            # en laat A ongemoeid (dus stabiel). Leeg als geen koker gestratificeerd is.
            for ko, ks, val in strat_terms:
                b[2 * ko] += val
                b[2 * ks] -= val
            x = solve_linear(A, b)
            if x is None:
                # Bijna-singulier thermisch stelsel: Ta/Tm bevriezen deze stap op hun laatste
                # goede waarde. Dat is de juiste noodgreep, maar mag niet stil blijven — de
                # teller wordt gepubliceerd (learned.solver_failures) zodat een structureel
                # conditioneringsprobleem zichtbaar is i.p.v. een geruisloos bevroren curve.
                solver_failures += 1
                break
            for z in zones:
                k = zi[z]
                Ta[z] = x[2 * k]
                Tm[z] = x[2 * k + 1]
        for rid in out:
            out[rid].append((step["t"], Ta[rid]))
        if snapshot_t is not None and Ta_snap is None and step["t"] >= snapshot_t:
            Ta_snap, Tm_snap = dict(Ta), dict(Tm)
    return {"series": out, "Ta": dict(Ta), "Tm": dict(Tm),
            "Ta_now": Ta_snap if Ta_snap is not None else dict(Ta),
            "Tm_now": Tm_snap if Tm_snap is not None else dict(Tm),
            "solver_failures": solver_failures}


# ════════════════════════════════════════════════════════════════════════════════════
#  Kalibratie — gedempte Gauss-Newton op de leerbare schalen
# ════════════════════════════════════════════════════════════════════════════════════

def _param_keys(rooms: list[str]) -> list[tuple]:
    keys = [("global", g) for g in GLOBAL_PARAMS]
    for rid in rooms:
        for p in PER_ROOM_PARAMS:
            keys.append((rid, p))
    return keys


def params_to_vec(params: dict, keys: list[tuple]) -> list[float]:
    out = []
    for scope, name in keys:
        out.append(params[name] if scope == "global" else params[scope][name])
    return out


def vec_to_params(vec: list[float], keys: list[tuple], base: dict) -> dict:
    p = json.loads(json.dumps(base))   # diepe kopie
    for v, (scope, name) in zip(vec, keys):
        lo, hi = BOUNDS[name]
        v = max(lo, min(hi, v))
        if scope == "global":
            p[name] = v
        else:
            p.setdefault(scope, {})[name] = v
    return p


def _clamp_to_bounds(vec: list[float], keys: list[tuple]) -> list[float]:
    """Klem een solver-state-vector op de BOUNDS van zijn parameters. vec_to_params projecteert
    al bij élke evaluatie, maar de Gauss-Newton-iterate zelf kon buiten de band accumuleren —
    dan rekent de ridge-anker-term (x−prior) op een fantoompunt dat nooit geëvalueerd wordt."""
    return [max(BOUNDS[name][0], min(BOUNDS[name][1], v)) for v, (_, name) in zip(vec, keys)]


def _sensor_temp(ta: float | None, t_out: float | None, frac: float) -> float | None:
    """Wat een sensor leest die (deels) op de buitenmuur zit: een blend van de échte
    luchttemp `ta` en de buitentemp `t_out`. Een tado-voeler vlak op de exterieurmuur leest
    een fractie `frac` richting de (koude/warme) wand-/buitenkant i.p.v. zuiver de kamerlucht
    (zie `sensor_outdoor_frac` per kamer in house_model.json). frac=0 → ongewijzigd.

    Dit is een meet-laag, niet de fysica: het laat de luchtknoop de wáre kamertemp blijven
    terwijl de fit tegen de gebiasde sensor vergelijkt — zo hoeft de kalibratie ua_env niet
    meer te maximaliseren om een naar-buiten-lekkende sensor na te bootsen. Mirror van
    `wu_bias`: een vaste, gedocumenteerde constante, géén leerbare parameter (zou anders
    degenereren met ua_env)."""
    if ta is None or not frac or t_out is None:
        return ta
    return (1.0 - frac) * ta + frac * t_out


def _to_sensor_series(house, timeline, rid, pred: list[tuple]) -> list[tuple]:
    """Map een voorspelde luchttemp-reeks (t, Ta) naar wat de sensor van die kamer zou
    lezen, per stap met de bijbehorende buitentemp. No-op voor kamers zonder bias."""
    frac = house.get("rooms", {}).get(rid, {}).get("sensor_outdoor_frac", 0.0)
    if not frac:
        return pred
    tout = {s["t"]: s["T_out"] for s in timeline}
    return [(t, _sensor_temp(v, tout.get(t), frac)) for t, v in pred]


def _series_trend(series: list[tuple], since: datetime | None = None) -> float | None:
    """Kleinste-kwadraten-helling (°C/uur) van een (t, temp)-reeks. Met `since` enkel de
    punten vanaf dat moment — zo geeft de voorspelde reeks (die tot now+2u doorloopt) de
    vóóruit geprojecteerde richting: + = opwarmend, − = afkoelend. None bij <2 punten."""
    pts = [(t, v) for t, v in series if v is not None and (since is None or t >= since)]
    if len(pts) < 2:
        return None
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 3600.0 for t, _ in pts]
    ys = [float(v) for _, v in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return num / den


def _residuals_timed(house, params, timeline, seed, actual, rooms_set) -> list[tuple]:
    """(meetmoment, voorspeld−werkelijk) op elk sample — als `_residuals` maar mét de tijdstempel
    behouden zodat de fit een recency-weging kan toepassen. Voorspelling eerst naar sensor-ruimte
    (buitenmuur-bias) zodat tegen de werkelijk gemeten — gebiasde — tado-temp vergeleken wordt."""
    sim = simulate(house, params, timeline, seed, calib_only_rooms=rooms_set)
    out = []
    for rid, samples in actual.items():
        pred = sim["series"].get(rid, [])
        if not pred:
            continue
        pred = _to_sensor_series(house, timeline, rid, pred)
        for ts, val in samples:
            out.append((ts, _interp(pred, ts) - val))
    return out


def _residuals(house, params, timeline, seed, actual, rooms_set) -> list[float]:
    """Voorspeld − werkelijk op elk meetmoment (lineair geïnterpoleerd op de
    voorspelde reeks). De voorspelling wordt eerst naar sensor-ruimte gemapt (buitenmuur-
    bias) zodat de fit tegen de werkelijk gemeten — gebiasde — tado-temp vergelijkt."""
    return [r for _, r in _residuals_timed(house, params, timeline, seed, actual, rooms_set)]


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


def _interp(series: list[tuple], ts: datetime) -> float:
    """Lineaire interpolatie van (t, waarde)-reeks op tijdstip ts."""
    if ts <= series[0][0]:
        return series[0][1]
    if ts >= series[-1][0]:
        return series[-1][1]
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if t0 <= ts <= t1:
            f = (ts - t0).total_seconds() / max(1.0, (t1 - t0).total_seconds())
            return v0 + f * (v1 - v0)
    return series[-1][1]


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


def backfill_rmse_history(history: list, timed_res: list, actual: dict, now: datetime,
                          log: list[dict]) -> int:
    """Herbereken in-place de RMSE/skill van elk leercurve-punt dat nog binnen het herstel-horizon
    (t ≥ now − CALIB_WINDOW_H) valt, tegen de HUIDIG gecorrigeerde openingen-log + params (via de
    al-uitgevoerde simulatie in `timed_res`). Zo verdwijnt de door een verkeerde open/dicht-aanname
    opgeblazen fout van een achteraf rechtgezette/teruggedateerde melding uit de curve — de leercurve
    toont dan model-skill i.p.v. meld-fouten.

    De poort is de openingen-log zélf, niet de sim-waarde: elk punt draagt een vingerafdruk
    (`log_fp`, `openings_fingerprint` over zijn eigen venster) en wordt alléén herberekend
    wanneer die verschuift — d.w.z. wanneer een melding in dat venster is teruggedateerd,
    rechtgezet of toegevoegd. Zonder log-wijziging bevriest het punt, hoe anders de huidige
    simulatie ook uitpakt. (De eerdere Δ≥0.25°C-poort vergeleek sim-waarden en liet zo één
    run met een slechte fit — transiënte input-hapering, mislukte kalibratiestap — de hele
    zichtbare curve naar zijn eigen foutniveau herschrijven, elke run opnieuw: churn i.p.v.
    heling. De Δ-drempel blijft als tweede poort zodat een log-correctie zonder materieel
    effect het punt niet aanraakt.)

    Punten zónder `log_fp` (van vóór deze poort): één keer terug naar hun als-gelogde waarde
    (`rmse_logged`, als een eerdere poort-loze herberekening ze had overschreven) en dan
    gestempeld — heelt de door de churn-bug vervuilde curve bij deploy. Een lege log (nog
    geen meldingen, óf een transiënte Gist-leesfout) → alles ongemoeid: juist dan is de sim
    op default-standen gebouwd en zou herberekenen de curve vergiftigen. De oorspronkelijk
    gelogde waarde blijft één keer bewaard (`rmse_logged`), herberekende punten worden
    gemarkeerd (`recomputed`). Punten ouder dan de horizon bevriezen: hun tado-grond-waarheid
    is uit het ~48u window_data-buffer gerold. Geeft het aantal gewijzigde punten terug."""
    if not timed_res or not log:
        return 0
    horizon = now - _timedelta_h(CALIB_WINDOW_H)
    earliest = timed_res[0][0]
    changed = 0
    for p in history:
        try:
            t_p = datetime.fromisoformat(p["t"])
        except (ValueError, TypeError, KeyError):
            continue
        if t_p < horizon or t_p > now:
            continue
        fp = openings_fingerprint(log, t_p - _timedelta_h(CALIB_WINDOW_H), t_p)
        stored_fp = p.get("log_fp")
        if stored_fp is None:
            # Punt van vóór de vingerafdruk-poort: herstel eenmalig de als-gelogde waarde
            # (een poort-loze herberekening kan 'm met een willekeurige latere sim hebben
            # overschreven) en bevries 'm op de huidige log-stand.
            if p.get("recomputed") and p.get("rmse_logged") is not None:
                p["rmse"] = p["rmse_logged"]
                sk = skill_score(p["rmse"], p.get("rmse_naive"))
                if sk is not None:
                    p["skill"] = sk
                p.pop("recomputed", None)
                changed += 1
            p["log_fp"] = fp
            continue
        if stored_fp == fp:
            continue                     # log ongewijzigd in dit venster → punt bevroren
        p["log_fp"] = fp                 # log-correctie gezien; hoe dan ook niet opnieuw evalueren
        start = max(t_p - _timedelta_h(CALIB_WINDOW_H), earliest)
        res_win = [r for (t, r) in timed_res if start <= t <= t_p]
        if len(res_win) < RMSE_BACKFILL_MIN_SAMPLES:
            continue
        new_rmse = rmse(res_win)
        if new_rmse != new_rmse:                                   # NaN-guard
            continue
        if abs(new_rmse - (p.get("rmse") if p.get("rmse") is not None else new_rmse)) < RMSE_BACKFILL_DELTA:
            continue
        p.setdefault("rmse_logged", p.get("rmse"))
        p["rmse"] = round(new_rmse, 3)
        new_naive = _window_naive(actual, start, t_p)
        if new_naive == new_naive:
            p["rmse_naive"] = round(new_naive, 3)
            sk = skill_score(new_rmse, new_naive)
            if sk is not None:
                p["skill"] = sk
        p["recomputed"] = True
        changed += 1
    return changed


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


def calibrate(house, params, timeline, seed, actual, max_iter: int = 5,
              time_budget_s: float = 40.0, solar_mean: float | None = None) -> tuple[dict, float]:
    """Minimaliseer Σ(voorspeld−werkelijk)² + Tikhonov-ridge naar de priors over het venster
    met gedempte Gauss-Newton. Online: schuif maar LEARN_RATE naar het optimum (stabiel,
    convergeert over runs). `solar_mean` (venster-zon-gemiddelde, W/m²) maakt het solar_gain-anker
    regime-bewust (zie reg_weight). Geeft (nieuwe params, RMSE-na)."""
    rooms = [rid for rid in actual if actual[rid]]
    if not rooms:
        return params, float("nan")
    rooms_set = set(rooms)
    keys = _param_keys(rooms)
    x = params_to_vec(params, keys)
    base = params
    prior_vec = [PRIORS[name] for _, name in keys]   # ridge-anker per parameter
    reg_vec = [reg_weight(name, solar_mean) for _, name in keys]  # regime-bewust ridge-gewicht

    def _total_cost(res: list[float], xv: list[float]) -> float:
        """Huber-datakosten (mét recency-weging) + Tikhonov-ridge naar de priors. Dezelfde schaal
        als de normaalvergelijkingen (de factor 2 valt consistent aan beide kanten weg)."""
        pen = sum(reg_vec[k] * (xv[k] - prior_vec[k]) ** 2 for k in range(len(keys)))
        return _wcost(res, tw) + pen

    # Eén timed-residu-call vooraf: levert zowel r0 als de sample-tijdstempels. De residu-volgorde
    # is identiek aan elke latere `_residuals`-call (zelfde actual/rooms_set), dus de recency-
    # gewichten `tw` blijven uitgelijnd door de hele fit.
    r0_timed = _residuals_timed(house, params, timeline, seed, actual, rooms_set)
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
        p_cur = vec_to_params(x, keys, base)
        r = _residuals(house, p_cur, timeline, seed, actual, rooms_set)
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
            rj = _residuals(house, vec_to_params(xj, keys, base), timeline, seed, actual, rooms_set)
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
        x_new = _clamp_to_bounds([x[j] + delta[j] for j in range(nk)], keys)
        new_cost = _total_cost(
            _residuals(house, vec_to_params(x_new, keys, base), timeline, seed, actual, rooms_set), x_new)
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
    new_params = vec_to_params(x_blend, keys, base)
    final_rmse = rmse(_residuals(house, new_params, timeline, seed, actual, rooms_set))
    if final_rmse != final_rmse:   # NaN: vertrouw de geblende params niet — geef de oude terug
        return params, rmse(r0)   # (kan zelf NaN zijn → downstream vangt de niet-NaN-poort dat af)
    return new_params, final_rmse


# ════════════════════════════════════════════════════════════════════════════════════
#  Passieve suggestie — welke ramen geven nú de meeste koeling
# ════════════════════════════════════════════════════════════════════════════════════

def suggest(house: dict, params: dict, weather: dict, room_now: dict,
            outside_temp: float, outside_rh: float, outside_rh_temp: float) -> dict:
    """Brute-force raam/rooster-combinaties (≤256) en rangschik op nuttige koeling nu,
    met comfort- en vochtbegrenzing. Geeft het beste open-zetje als instructies per
    raam + de top-combinaties. Puur adviserend — er wordt niet naar gehandeld."""
    rooms = list(house.get("rooms", {}).keys())
    zones = rooms + list(house.get("junctions", {}).keys())
    # Alléén de te openen ramen meenemen: een vast raam "openen" doet niets (oppervlak 0),
    # dus zou alleen schijn-variatie in de suggesties geven.
    windows = [(wid, w) for wid, w in house.get("windows", {}).items()
               if w.get("max_open_area_m2", 0.0) > 0.0]
    vents = house.get("vents", {})
    if len(windows) > 10:
        windows = windows[:10]   # houd de enumeratie behapbaar (2^n)

    # None-veilig: een sensorkamer kan in room_now staan mét waarde None (tado-uitval, of een
    # window_data.json die de kamer nog niet kent — b.v. een net toegevoegde sensor terwijl de
    # loop-checkout nog een oude window_data heeft). `.get(key, fallback)` valt dan NIET terug
    # (de key bestaat), en een None-temp crasht air_density() in solve_network.
    zone_temps = {}
    for z in zones:
        t_z = room_now.get(_wd_key(house, z))
        zone_temps[z] = t_z if t_z is not None else outside_temp

    def comfort(room_id):
        wd = _wd_key(house, room_id)
        return ROOM_COMFORT.get(wd, (19.5, 23.5))

    def score(states):
        ops = build_openings(house, states, weather, params, zone_temps, outside_temp)
        net = solve_network(zones, ops, zone_temps, outside_temp)
        total = 0.0
        for room_id in rooms:
            t_in = room_now.get(_wd_key(house, room_id))
            if t_in is None:
                continue
            fresh = net["fresh"].get(room_id, 0.0)
            low, high = comfort(room_id)
            # Vocht-veto: nooit lucht binnenhalen die de kamer voorbij RH_HARD_CAP duwt.
            vent_rh = convert_rh(outside_rh, outside_rh_temp, t_in)
            if fresh > 1e-4 and vent_rh is not None and vent_rh >= RH_HARD_CAP:
                total -= 50.0 * fresh
                continue
            cooling = 1.2 * CP_AIR * fresh * (t_in - outside_temp)
            if t_in > high and outside_temp < t_in:
                total += max(0.0, cooling)
            elif t_in <= low and fresh > 1e-4:
                total -= 0.5 * 1.2 * CP_AIR * fresh * max(0.0, t_in - outside_temp)  # niet doorkoelen
        # Kleine vaste kost per open raam: breekt gelijkspel richting zo wéinig mogelijk
        # ramen, zodat thermisch-neutrale ramen (buiten even warm/koeler dan binnen, geen
        # netto koeling) dícht blijven i.p.v. "gratis" mee te openen. Verwaarloosbaar t.o.v.
        # echte koeling (honderden W).
        n_open = sum(1 for wid, _ in windows if _open_frac(states.get(wid, 0), {}) > 0.5)
        total -= 0.5 * n_open
        # Veiligheid: ontmoedig wijd open bij harde wind/regen.
        if weather.get("gust", 0.0) > 14.0 or weather.get("precip", 0.0) > 0.3:
            total -= 5.0 * n_open
        return total

    # Vaste basis voor roosters/deuren (trickle / open).
    base_states = {}
    for vid in vents:
        base_states[vid] = "open"

    best, ranked = None, []
    for combo in range(1 << len(windows)):
        states = dict(base_states)
        for bit, (wid, _) in enumerate(windows):
            states[wid] = "open" if combo & (1 << bit) else "closed"
        s = score(states)
        open_ids = [wid for bit, (wid, _) in enumerate(windows) if combo & (1 << bit)]
        ranked.append({"windows": open_ids, "score": round(s, 1)})
        if best is None or s > best["score"]:
            best = {"windows": open_ids, "score": round(s, 1), "states": states}
    ranked.sort(key=lambda r: -r["score"])

    # Diverse top-K: filter bijna-identieke combinaties (verschillen in ≤1 raam) eruit, zodat
    # de lijst écht andere strategieën toont i.p.v. vier varianten op hetzelfde. Voeg de
    # "alles dicht"-basislijn toe als ijkpunt. Geef per combinatie de betrokken kamers mee.
    diverse = []
    for cand in ranked:
        cs = set(cand["windows"])
        if cs and any(len(cs ^ set(d["windows"])) <= 1 for d in diverse):
            continue
        cand = dict(cand)
        cand["rooms"] = sorted({house["windows"][w]["room"] for w in cand["windows"]})
        diverse.append(cand)
        if len(diverse) >= 5:
            break
    if not any(not d["windows"] for d in diverse):
        diverse.append({"windows": [], "rooms": [], "score": round(score(dict(base_states)), 1)})

    instructions = []
    for wid, w in house.get("windows", {}).items():
        do_open = best is not None and wid in best["windows"]
        instructions.append({
            "window": wid, "room": w.get("room"),
            "action": "open" if do_open else "dicht",
            "label": w.get("label", wid),
        })
    keep_closed = best is None or best["score"] <= 0.1
    return {"instructions": instructions, "ranked": diverse,
            "keep_closed": keep_closed,
            "headline": _suggest_headline(house, best, keep_closed)}


def _suggest_headline(house, best, keep_closed) -> str:
    if keep_closed or not best or not best["windows"]:
        return "Alles dicht houden — buiten geeft nu geen nuttige koeling."
    labels = [house["windows"][w].get("label", w) for w in best["windows"]]
    return "Voor de meeste koeling: open " + " + ".join(labels) + "."


def _wd_key(house: dict, room_id: str) -> str:
    return house.get("rooms", {}).get(room_id, {}).get("from_window_data", room_id)


# ════════════════════════════════════════════════════════════════════════════════════
#  I/O — huismodel, window_data.json, Gist-openingenlog, geleerde params, weer
# ════════════════════════════════════════════════════════════════════════════════════

_MODEL_VERSION = None


def model_version() -> str:
    """Korte code-versie (git short-SHA) van de draaiende runner, zodat elk RMSE-punt aan een
    codeversie te koppelen is — "heeft iteratie N de fout echt verbeterd?" wordt dan een
    correlatie op de data zelf, geen git-archeologie. Prefereert `GITHUB_SHA` (gezet in de
    Action), valt terug op `git rev-parse`, dan 'unknown'. Gecachet per proces."""
    global _MODEL_VERSION
    if _MODEL_VERSION is not None:
        return _MODEL_VERSION
    sha = os.getenv("GITHUB_SHA")
    if sha:
        _MODEL_VERSION = sha[:7]
        return _MODEL_VERSION
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        _MODEL_VERSION = out.stdout.strip() if (out.returncode == 0 and out.stdout.strip()) else "unknown"
    except (OSError, subprocess.SubprocessError):
        _MODEL_VERSION = "unknown"
    return _MODEL_VERSION


def load_house() -> dict:
    with open(HOUSE_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_window_data() -> dict:
    try:
        with open(WINDOW_DATA, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"[window_data] {WINDOW_DATA} ontbreekt/onleesbaar — kamers leeg.")
        return {}


def load_learned() -> dict:
    try:
        with open(LEARNED_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def default_params(house: dict) -> dict:
    p = {k: PRIORS[k] for k in GLOBAL_PARAMS}
    for rid in house.get("rooms", {}):
        p[rid] = {k: PRIORS[k] for k in PER_ROOM_PARAMS}
    return p


def physics_rev_migration_needed(learned: dict) -> bool:
    """Is er geleerde staat van een oudere fysica-revisie? (Een lege/nieuwe staat hoeft
    niets te migreren — de defaults zijn al de priors.)"""
    return bool(learned.get("params")) and learned.get("physics_rev") != PHYSICS_REV


def merged_params(house: dict, learned: dict) -> dict:
    """Geleerde params aangevuld met priors voor nieuwe kamers/keys (additief, robuust).
    Gedeeld door main() en night_forecast.py zodat de merge-logica niet dubbel bestaat."""
    params = learned.get("params") or default_params(house)
    # Fossiel uit de tijd dat `cd` leerbaar was: de code rekent met de vaste CD-constante en
    # niets leest params["cd"] — strip 'm hier zodat hij niet eeuwig in de artefacten meerijdt.
    params.pop("cd", None)
    base = default_params(house)
    # Fysica-revisie-migratie (zie PHYSICS_REV): geleerde staat van een oudere revisie →
    # alleen de globalen terug naar hun prior. Hier (en niet alleen in main) zodat óók
    # night_forecast.py nooit oude-fysica-globalen op de nieuwe fysica loslaat.
    if physics_rev_migration_needed(learned):
        for g in GLOBAL_PARAMS:
            params[g] = base[g]
    for g in GLOBAL_PARAMS:
        params.setdefault(g, base[g])
    for rid in house.get("rooms", {}):
        params.setdefault(rid, base[rid])
        for k in PER_ROOM_PARAMS:
            params[rid].setdefault(k, PRIORS[k])
    return params


def load_openings_log() -> list[dict]:
    gist_id = os.getenv("GIST_ID")
    token = os.getenv("GIST_TOKEN")
    if not gist_id or not token:
        print("[openings] geen GIST_ID/GIST_TOKEN — lege log.")
        return []
    data = gist_read_json(gist_id, OPENINGS_FILE, token=token,
                          default={}, label="openings")
    return data.get("log", []) if isinstance(data, dict) else []


def fetch_weather() -> dict:
    """Open-Meteo: verleden (voor het leren) + forecast, met wind incl. richting en de
    zoncomponenten voor de instraling door het glas."""
    lat, lon = _LAT, _LON
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ("temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,"
                   "wind_direction_10m,wind_gusts_10m,shortwave_radiation,"
                   "direct_radiation,diffuse_radiation"),
        "current": ("temperature_2m,relative_humidity_2m,wind_speed_10m,"
                    "wind_direction_10m,wind_gusts_10m,shortwave_radiation,direct_radiation"),
        "wind_speed_unit": "ms",
        "timezone": "Europe/Amsterdam",
        # Genoeg verleden voor het residu-venster (CALIB_WINDOW_H) plus de sim-only WARMUP_H
        # aanloop van de massaknoop, met marge.
        "past_days": 4, "forecast_days": 2,
    }
    data = get_json("https://api.open-meteo.com/v1/forecast", params,
                    timeout=25, label="open-meteo")
    h = data.get("hourly", {})
    times = [datetime.fromisoformat(t).replace(tzinfo=TZ) for t in h.get("time", [])]
    rows = []
    for i, t in enumerate(times):
        rows.append({
            "dt": t,
            "T_out": _get(h, "temperature_2m", i),
            "rh": _get(h, "relative_humidity_2m", i),
            "precip": _get(h, "precipitation", i) or 0.0,
            "wind_speed": _get(h, "wind_speed_10m", i) or 0.0,
            "wind_dir": _get(h, "wind_direction_10m", i) or 0.0,
            "gust": _get(h, "wind_gusts_10m", i) or 0.0,
            "shortwave": _get(h, "shortwave_radiation", i) or 0.0,
            "direct": _get(h, "direct_radiation", i) or 0.0,
            "diffuse": _get(h, "diffuse_radiation", i) or 0.0,
        })
    cur = data.get("current", {}) or {}
    return {"hourly": rows, "current": cur}


def _get(h: dict, key: str, i: int):
    arr = h.get(key) or []
    return arr[i] if i < len(arr) else None


# ════════════════════════════════════════════════════════════════════════════════════
#  Timeline-opbouw + dashboard
# ════════════════════════════════════════════════════════════════════════════════════

def wu_solar_scale_factor(k: float | None, age_h: float,
                          decay_h: float = WU_SOLAR_SCALE_DECAY_H) -> float:
    """Per-stap herschaalfactor voor de instraling: blend tussen de WU/OM-zon-ratio `k` (geldig op
    nu) en 1.0 (pure OM) naar sample-leeftijd. `age_h` = uren vóór nu (negatief = de 2u-vooruitblik,
    krijgt vol `k`). `k`=None → 1.0 (WU ontbrak; no-op)."""
    if k is None:
        return 1.0
    w = 1.0 if decay_h <= 0 else 1.0 - age_h / decay_h
    w = min(1.0, max(0.0, w))
    return 1.0 + (k - 1.0) * w


def per_window_solar(house: dict, states: dict, sun_az: float, sun_el: float,
                     direct: float, diffuse: float, beam_iam: bool = False) -> dict[str, float]:
    """W getransmitteerd door elk raam (τ·shading·shade·I·glas_m²), key = raam-id.
    Pure momentopname op één zonpositie; de per-kamer `irr` in build_timeline is de
    som hiervan per kamer. Hergebruikt door het zonwering-advies (shade_advisor.py)
    en het additieve dashboard-veld `solar_by_window`."""
    out = {}
    for wid, w in house.get("windows", {}).items():
        shade = _shade_factor(wid, w, states)
        # I = invallende straling op het glas (W/m²) — fysica-symbool.
        I = facade_irradiance(w.get("facade_azimuth_deg", 0.0), sun_az, sun_el,  # noqa: E741
                              direct, diffuse, w.get("tilt_deg", 90.0),
                              bool(w.get("diffuse_only", False)),
                              w.get("horizon_elevation_deg", 0.0), beam_iam)
        out[wid] = GLASS_TRANSMITTANCE * shade * I * w.get(
            "glass_m2", GLASS_AREA_FRACTION * w.get("area_m2", 1.0))
    return out


def build_timeline(house: dict, weather: dict, log: list[dict], now: datetime,
                   window_h: float, wu_solar_scale: float | None = None,
                   beam_iam: bool = False, end_h: float = 2.0) -> list[dict]:
    """Bouw een 15-minuten-raster van drivers over het kalibratievenster t/m nu,
    plus een vooruitblik van `end_h` uur (default de korte 2u voor de afgeleide-
    temp-projectie; night_forecast.py rekt hem op tot morgenochtend). Per stap:
    T_out, per-kamer instraling (door het glas), wind, en de gerapporteerde
    openingen-toestand op dat moment."""
    rows = [r for r in weather["hourly"] if r["T_out"] is not None]
    if not rows:
        return []
    start = now - _timedelta_h(window_h)
    grid = []
    t = start
    end = now + _timedelta_h(end_h)
    lat, lon = _LAT, _LON
    while t <= end:
        T_out = _interp_hourly(rows, t, "T_out")
        wx = {k: _interp_hourly(rows, t, k) for k in
              ("wind_speed", "wind_dir", "gust", "precip", "direct", "diffuse", "rh")}
        st = openings_at(log, t)            # gerapporteerde toestand op dit moment (incl. zonwering)
        # Representatieve zonpositie op het stap-midden (voor de rij/dashboard; `irr` hieronder
        # is een tijdsgemiddelde over de stap, geen momentopname).
        sun_az, sun_el = sun_position(lat, lon, (t + _timedelta_h(0.125)).astimezone(timezone.utc))
        # Tijdsgemiddelde instraling over [t, t+0.25h] via de midden-regel op SOLAR_SUBSTEPS
        # subintervallen: dempt de geometrie-aliasing van de snel-draaiende lage avondzon.
        irr = {rid: 0.0 for rid in house.get("rooms", {})}
        # Horizontale dak-instraling (W/m², onbeschaduwd, opake conductie — de absorptie zit in
        # ROOF_SOLAR_GAIN, niet in een glas-transmissie) voor de bovenste-verdieping-kamers.
        roof_rooms = [rid for rid, r in house.get("rooms", {}).items() if r.get("roof_m2", 0.0) > 0.0]
        irr_roof = {rid: 0.0 for rid in roof_rooms}
        # WU/OM-herschaling (stap 1): sterkst rond nu, uitdovend naar 1.0 verder terug. None → 1.0.
        sc = wu_solar_scale_factor(wu_solar_scale, (now - t).total_seconds() / 3600.0)
        for j in range(SOLAR_SUBSTEPS):
            ts = t + _timedelta_h(0.25 * (j + 0.5) / SOLAR_SUBSTEPS)
            s_az, s_el = sun_position(lat, lon, ts.astimezone(timezone.utc))
            s_direct = _interp_hourly(rows, ts, "direct")
            s_diffuse = _interp_hourly(rows, ts, "diffuse")
            if sc != 1.0:
                s_direct = (s_direct or 0.0) * sc
                s_diffuse = (s_diffuse or 0.0) * sc
            pw = per_window_solar(house, st, s_az, s_el, s_direct, s_diffuse, beam_iam)
            tot_room = {rid: 0.0 for rid in irr}
            for wid, w in house.get("windows", {}).items():
                rid = w.get("room")
                if rid in tot_room:
                    tot_room[rid] += pw[wid]
            for rid in irr:
                irr[rid] += tot_room[rid] / SOLAR_SUBSTEPS
            if roof_rooms:
                # Plat dak (tilt 0) → azimut-onafhankelijk; één waarde voor alle dak-kamers.
                roof_i = facade_irradiance(0.0, s_az, s_el, s_direct, s_diffuse, 0.0)
                for rid in roof_rooms:
                    irr_roof[rid] += roof_i / SOLAR_SUBSTEPS
        grid.append({"t": t, "T_out": T_out, "irr": irr, "irr_roof": irr_roof, "states": st,
                     "weather": wx, "dt": 900.0, "sun_az": sun_az, "sun_el": sun_el})
        t = t + _timedelta_h(0.25)
    return grid


def collect_actual(house: dict, wd: dict, since: datetime) -> dict:
    """Per sensorkamer de werkelijke tado-temp-samples (t, °C) vanaf `since`, uit de
    history in window_data.json (+ de huidige meting)."""
    actual = {}
    for rid, room in house.get("rooms", {}).items():
        wd_key = room.get("from_window_data")
        if not wd_key or wd_key not in wd.get("rooms", {}):
            continue
        rd = wd["rooms"][wd_key]
        samples = []
        for s in rd.get("history", []):
            try:
                ts = datetime.fromisoformat(s["t"])
            except (ValueError, TypeError, KeyError):
                continue
            if ts >= since and s.get("temp") is not None:
                samples.append((ts, s["temp"]))
        samples.sort()
        if samples:
            actual[rid] = samples
    return actual


def _room_dashboard_row(rid, room, house, params, wd, sim, timeline,
                        actual, now, ctx) -> dict:
    """Bouw één kamer-rij voor airflow_data.json.

    `ctx` bundelt de run-brede grootheden die elke kamer deelt: de zone-thermische
    params (`zpar`), de op-nu vastgelegde lucht-/massatemps (`ta_all`/`tm_all`), de
    laatste timeline-stap ≤ now (`now_step`), het interne-gain-profiel nu, en de
    ventilatie-/dichtheidsconstanten (`veff`/`rho_cp`)."""
    zpar = ctx["zpar"]
    ta_all = ctx["ta_all"]
    tm_all = ctx["tm_all"]
    now_step = ctx["now_step"]
    wd_key = room.get("from_window_data")
    rd = wd.get("rooms", {}).get(wd_key, {}) if wd_key else {}
    pred_series = sim["series"].get(rid, [])
    ta_now = ta_all.get(rid)               # échte (debiased) luchttemp van het model, op nu
    frac = room.get("sensor_outdoor_frac", 0.0)
    t_out_now = now_step["T_out"] if now_step else None
    pred_now = _sensor_temp(ta_now, t_out_now, frac)   # wat de sensor leest → vergelijkbaar met tado
    act_now = rd.get("inside")
    err = (pred_now - act_now) if (pred_now is not None and act_now is not None) else None
    fresh_ach = None
    env_w = vent_w = None        # warmtestroom naar/uit buiten (W, + = winst, − = verlies)
    if now_step is not None:
        ops = build_openings(house, now_step["states"], now_step["weather"], params,
                             ta_all, now_step["T_out"])
        net = solve_network(list(house["rooms"]) + list(house.get("junctions", {})),
                            ops, ta_all, now_step["T_out"])
        vol = room.get("volume_m3", 40.0)
        fresh_m3s = net["fresh"].get(rid, 0.0)
        fresh_ach = round(fresh_m3s * 3600.0 / vol, 2)
        # De twee "naar buiten"-termen, de tegenhanger van de zonwinst: schil-conductie
        # en ventilatie-uitwisseling met de buitenlucht. Negatief = energie verlaat de
        # kamer (koeling/warmteverlies). Zelfde tekens als in simulate()'s luchtknoop.
        if ta_now is not None and t_out_now is not None:
            ua_env = zpar.get(rid, {}).get("UA_env", 0.0)
            env_w = round(ua_env * (t_out_now - ta_now), 0)
            vent_w = round(ctx["rho_cp"] * ctx["veff"] * fresh_m3s * (t_out_now - ta_now), 0)
    sens_series = _to_sensor_series(house, timeline, rid, pred_series)
    trend = _series_trend(sens_series, since=now)
    # Verticale-koker-stratificatie (stap 3): additieve top/onder-temp + gradiënt voor de weergave
    # (de koker blijft één knoop; `predicted_air_temp` is het koker-gemiddelde). Alleen bij "stratify".
    strat_extra = {}
    strat_info = ctx.get("strat", {}).get(rid)
    if strat_info and ta_now is not None and t_out_now is not None and now_step is not None:
        strat_ops = build_openings(house, now_step["states"], now_step["weather"], params,
                                   ta_all, t_out_now)
        open_pairs = {(op["a"], op["b"]) for op in strat_ops if op.get("b") != "outside"}
        open_others = {o for o in strat_info["doors"]
                       if (rid, o) in open_pairs or (o, rid) in open_pairs}
        gamma = _stair_gamma(strat_info, ta_all, open_others)
        crown = stair_crown(now_step.get("irr_roof", {}).get(rid, 0.0))
        # Pin-fout per open deur: koker-lucht op deurhoogte minus de kamer-lucht (− = koker leest
        # kouder dan de kamer op die verdieping). Diagnostiek voor hoe strak de counterflow de
        # koker aan zijn open-deur-kamers pint; hoort ~0 te zijn bij open deuren. Additief veld.
        pin_err = {o: round(ta_now + gamma * (strat_info["doors"][o] - strat_info["z_mean"])
                            - ta_all[o], 2)
                   for o in sorted(open_others) if ta_all.get(o) is not None}
        strat_extra = {
            "stair_gradient_c_per_m": round(gamma, 3),
            "stair_pin_error_c": pin_err,
            # Zon-kroon: extra °C bovenop de γ-lijn voor de bovenste meters (boven de hoogste
            # deur), gedreven door de dak-/skylight-instraling nu; 's avonds 0. Additief veld.
            "stair_crown_c": round(crown, 2),
            "predicted_temp_top": round(ta_now + gamma * (strat_info["z_hi"] - strat_info["z_mean"])
                                        + crown, 2),
            "predicted_temp_bottom": round(ta_now + gamma * (strat_info["z_lo"] - strat_info["z_mean"]), 2),
        }
    return {
        "label": room.get("label", rid),
        "from_window_data": wd_key,
        "actual_temp": act_now,
        "predicted_temp": round(pred_now, 2) if pred_now is not None else None,
        # Debiased wáre luchttemp (vóór de buitenmuur-sensorbias); == predicted_temp als frac=0.
        "predicted_air_temp": round(ta_now, 2) if ta_now is not None else None,
        "sensor_outdoor_frac": frac,
        "predicted_mass_temp": round(tm_all.get(rid), 2) if tm_all.get(rid) is not None else None,
        "error": round(err, 2) if err is not None else None,
        "humidity": rd.get("humidity"),
        "ach": fresh_ach,
        "solar_w": round(now_step["irr"].get(rid, 0.0), 0) if now_step else None,
        # Additief: de verdeling van de zonwinst over de ramen van deze kamer (W, snapshot
        # op nu) — voedt de raam-tooltip op de kamerkaart en maakt zichtbaar wélk raam de
        # winst binnenlaat (en dus welke zonwering helpt).
        "solar_by_window": {wid: {"label": w.get("label", wid),
                                  "w": round(ctx.get("pw_now", {}).get(wid, 0.0), 0)}
                            for wid, w in house.get("windows", {}).items()
                            if w.get("room") == rid},
        # Energie naar buiten (W, signed): schil-conductie + ventilatie-uitwisseling.
        # − = de kamer verliest warmte (koelt af) langs deze weg; tegenhanger van solar_w.
        "env_w": env_w,
        "vent_w": vent_w,
        # Richting van de temperatuurverandering (°C/uur): + opwarmend, − afkoelend.
        "trend_c_per_h": round(trend, 2) if trend is not None else None,
        # Buur-warmtestroom (W, + = nettowinst uit de buren) en interne last (W) — de
        # twee tussenwoning-termen, voor inzicht op het dashboard.
        "party_w": (round(zpar.get(rid, {}).get("UA_party", 0.0) * (NEIGHBOR_TEMP - ta_now), 0)
                    if ta_now is not None else None),
        "internal_w": round(zpar.get(rid, {}).get("Q_int_base", 0.0) * ctx["int_profile_now"], 0),
        "comfort_low": ROOM_COMFORT.get(wd_key, (None, None))[0] if wd_key else None,
        "comfort_high": ROOM_COMFORT.get(wd_key, (None, None))[1] if wd_key else None,
        # Airco: staat de mobiele unit nu in deze kamer (→ uit de kalibratie gelaten), en hoeveel
        # samples zijn deze run om die reden uit de fit gevallen. Voor de "niet-gekalibreerd"-chip.
        "ac": (rid == ctx.get("ac_room")),
        "ac_excluded_samples": ctx.get("ac_excluded", {}).get(rid, 0),
        # Verwarming: staat de tado-verwarming nu aan in deze kamer (→ uit de kalibratie gelaten),
        # en hoeveel samples zijn deze run om die reden uit de fit gevallen. Voor de stook-chip.
        "heating": bool(ctx.get("heat_now", {}).get(rid)),
        "heat_excluded_samples": ctx.get("heat_excluded", {}).get(rid, 0),
        # Pauze: is het huis nu gepauzeerd (huis-breed → gelijk voor elke kamer, i.t.t. ac/
        # heating), en hoeveel samples zijn deze kamer deze run om die reden uit de fit
        # gevallen. Voor de "niet-gekalibreerd (gepauzeerd)"-chip.
        "paused": bool(ctx.get("paused_now")),
        "pause_excluded_samples": ctx.get("pause_excluded", {}).get(rid, 0),
        # Toon alleen het residu-venster (de WARMUP_H aanloop is sim-only opwarming van de
        # massaknoop, niet bedoeld als zichtbare voorspelling) + de 2u-vooruitblik.
        "predicted_series": [{"t": t.isoformat(), "temp": round(v, 2)}
                             for t, v in sens_series
                             if t >= now - _timedelta_h(CALIB_WINDOW_H)],
        "actual_series": [{"t": t.isoformat(), "temp": v} for t, v in actual.get(rid, [])],
        "params": params.get(rid, {}),
        **strat_extra,
    }


def build_dashboard(house, params, weather, wd, timeline, sim, sugg, learned,
                    actual, now, rmse_now, learning_held=False, baseline=None,
                    skill=None, rmse_baseline=None, wx=None, checkpoint=None,
                    fell_back=False, ac_room=None, ac_excluded=None,
                    heat_now=None, heat_excluded=None,
                    paused_now=False, paused_since=None, pause_excluded=None) -> dict:
    """Stel docs/airflow_data.json samen (additief schema)."""
    cur = weather["current"]
    sun_az, sun_el = sun_position(_LAT, _LON, now.astimezone(timezone.utc))
    out_t = cur.get("temperature_2m")
    out_rh = cur.get("relative_humidity_2m")

    # Huidige openingen-toestand (laatste snapshot).
    # Huidige (voorwaarts geaccumuleerde) toestand per element — dit voedt de modal zodat
    # die toont wat écht open/dicht staat i.p.v. de defaults.
    states_now = openings_at(load_openings_log_cached(), now)

    # De snapshot (kamertabellen + plattegrond) toont "nu", niet de 2u-vooruitblik: kies de
    # laatste timeline-stap ≤ now en pak de bijbehorende, op now vastgelegde zone-temps
    # (sim["Ta_now"]). De vooruitblik blijft alleen voor de projectie-reeks + trend.
    last_step = timeline[-1] if timeline else None
    now_step = None
    for s in (timeline or []):
        if s["t"] <= now:
            now_step = s
        else:
            break
    if now_step is None:
        now_step = last_step
    ta_all = sim.get("Ta_now", sim["Ta"])
    tm_all = sim.get("Tm_now", sim["Tm"])
    # Per-raam zon-doorval op nu (W, additief veld voor de raam-tooltip op de kamerkaart).
    # Momentopname op de stap-zonpositie — telt dus niet exact op tot het tijdsgemiddelde
    # (en evt. WU-herschaalde) `solar_w` van de stap; het is de verdeling, niet de boekhouding.
    pw_now = {}
    if now_step is not None:
        wx_now = now_step.get("weather", {})
        pw_now = per_window_solar(house, now_step["states"], now_step["sun_az"],
                                  now_step["sun_el"], wx_now.get("direct") or 0.0,
                                  wx_now.get("diffuse") or 0.0, beam_iam=True)
    ctx = {
        "zpar": _zone_thermal_params(house, params),
        "ta_all": ta_all, "tm_all": tm_all, "now_step": now_step,
        "int_profile_now": internal_gain_profile(now),
        "veff": params.get("vent_eff", 1.0), "rho_cp": 1.2 * CP_AIR,
        "ac_room": ac_room, "ac_excluded": ac_excluded or {},
        "heat_now": heat_now or {}, "heat_excluded": heat_excluded or {},
        "paused_now": paused_now, "pause_excluded": pause_excluded or {},
        "strat": _stratify_zones(house),   # verticale-koker-metadata voor de top/onder-weergave
        "pw_now": pw_now,                  # per-raam zon-doorval nu → solar_by_window
    }
    rooms_out = {rid: _room_dashboard_row(rid, room, house, params, wd, sim, timeline,
                                          actual, now, ctx)
                 for rid, room in house.get("rooms", {}).items()}

    rmse_hist = (learned.get("rmse_history") or [])[:]
    openings_log = load_openings_log_cached()
    # Heel de leercurve achteraf: herbereken in-horizon punten tegen de gecorrigeerde openingen-log
    # (een teruggedateerde/rechtgezette melding haalt zo de oude, vervuilde fout uit de curve).
    # Hergebruikt deze run-simulatie `sim` → geen extra simulatie. Punten buiten de horizon (oudere
    # grond-waarheid uit het buffer gerold) blijven ongemoeid, net als álles op een held-run: de
    # anomalie-poort zegt dan juist dat log en werkelijkheid vermoedelijk niet kloppen, dus die
    # verdachte sim mag de historie niet herschrijven.
    if actual and not learning_held:
        n_fixed = backfill_rmse_history(
            rmse_hist, timed_residuals_from_sim(house, timeline, sim, actual), actual, now,
            openings_log)
        if n_fixed:
            print(f"[leercurve] {n_fixed} punt(en) herberekend tegen de gecorrigeerde openingen-log.")
    if rmse_now == rmse_now:   # niet-NaN
        entry = {"t": now.isoformat(), "rmse": round(rmse_now, 3),
                 "held": bool(learning_held), "paused": bool(paused_now),
                 "version": model_version()}
        # Log-vingerafdruk over het eigen venster: de poort waarop backfill_rmse_history dit
        # punt later herberekent (alléén als de openingen-log in dat venster verandert).
        if openings_log:
            entry["log_fp"] = openings_fingerprint(
                openings_log, now - _timedelta_h(CALIB_WINDOW_H), now)
        # Additieve context per punt: weer-genormaliseerde skill + persistentie-baseline +
        # weer-samenvatting, zodat de leercurve te lezen is als "model vs. weer".
        if skill is not None:
            entry["skill"] = skill
        if rmse_baseline is not None and rmse_baseline == rmse_baseline:
            entry["rmse_naive"] = round(rmse_baseline, 3)
        if wx:
            entry["wx"] = wx
        if fell_back:
            entry["fell_back"] = True
        rmse_hist.append(entry)
    rmse_hist = thin_rmse_history(rmse_hist, now)

    return {
        "generated_at": utc_now_iso(),
        "as_of_local": now.isoformat(),
        "source": "airflow_model",
        "model_version": model_version(),
        "weather": {
            "outside_temp": out_t, "outside_humidity": out_rh,
            # Bron van de buiten-nu temp/RH: "wu" (eigen station, biasgecorrigeerd) of
            # "open-meteo" (grid-fallback). Wind/instraling komen altijd van Open-Meteo.
            "outside_source": cur.get("outside_source", "open-meteo"),
            "wind_speed": cur.get("wind_speed_10m"), "wind_dir": cur.get("wind_direction_10m"),
            "gust": cur.get("wind_gusts_10m"), "shortwave": cur.get("shortwave_radiation"),
            "sun_az": round(sun_az, 1), "sun_el": round(sun_el, 1),
            "neighbor_temp": round(_NEIGHBOR_TEMP, 1),
            # WU/OM glas-drive-herschaling die deze run op de recente stappen is toegepast (stap 1);
            # null = pure Open-Meteo (WU-zon ontbrak of te laag). Additief.
            "wu_solar_scale": (round(weather.get("wu_solar_scale"), 2)
                               if weather.get("wu_solar_scale") is not None else None),
        },
        "openings": states_now,
        "controls": _controls(house, states_now),
        # Pauze: is het huis nu gepauzeerd (huis-breed, via de modal-toggle), en sinds wanneer
        # (ISO, null als niet gepauzeerd) — voedt de "⏸️ Gepauzeerd sinds HH:MM"-banner.
        "paused": bool(paused_now),
        "paused_since": paused_since.isoformat() if paused_since else None,
        # Airco: in welke kamer de ene mobiele unit nu staat (of None) + de keuzelijst voor de
        # modal-dropdown (alleen sensorkamers — alleen díé worden gekalibreerd, dus alleen daar
        # doet de airco-uitsluiting ertoe).
        "ac": {"room": ac_room,
               "rooms": [{"id": rid, "label": r.get("label", rid)}
                         for rid, r in house.get("rooms", {}).items()
                         if r.get("from_window_data")]},
        "rooms": rooms_out,
        "flows": _flow_summary(house, params, sim, timeline, now),
        "suggestion": sugg,
        "learned": {"params": params, "rmse": round(rmse_now, 3) if rmse_now == rmse_now else None,
                    "rmse_naive": round(rmse_baseline, 3)
                    if (rmse_baseline is not None and rmse_baseline == rmse_baseline) else None,
                    "skill": skill,
                    "rmse_history": rmse_hist, "held": bool(learning_held),
                    "paused": bool(paused_now),
                    # Observability (additief): bijna-singuliere thermische substappen deze
                    # sim + de effectieve grond-waarheid-dekking na de filters (zie
                    # calib_coverage) met de waarschuwingsdrempel voor het dashboard.
                    "solver_failures": sim.get("solver_failures", 0),
                    **calib_coverage(actual),
                    "calib_coverage_warn_h": CALIB_COVERAGE_WARN_H,
                    "baseline_rmse": round(baseline, 3) if baseline is not None else None,
                    "wx": wx or None,
                    "checkpoint": {k: (checkpoint or {}).get(k)
                                   for k in ("skill", "rmse", "version", "t",
                                             "degraded_runs", "last_fallback", "reseated",
                                             "refloored")}
                    if checkpoint else None,
                    "fell_back": bool(fell_back)},
        # Volledige geometrie (additief) zodat de browser-speeltuin (airflow.html) hetzelfde
        # luchtstroomnetwerk lokaal kan oplossen — gedeeld met tweeling 2 via house_meta_out.
        "house_meta": house_meta_out(house),
    }


def house_meta_out(house: dict) -> dict:
    """Volledige geometrie voor de browser-speeltuin: openingsoppervlakken, hoogtes,
    roosters/deuren en de sim-constanten die Python gebruikt. Gedeeld door beide
    tweeling-dashboards (tweeling 2 voegt er additief zijn Cp-metadata aan toe)."""
    return {
        "rooms": {rid: {"plan_xy": r.get("plan_xy"), "label": r.get("label", rid),
                        "floor": r.get("floor", 0), "plan_h": r.get("plan_h", 1),
                        "volume_m3": r.get("volume_m3"),
                        "sensor": bool(r.get("from_window_data"))}
                  for rid, r in house.get("rooms", {}).items()},
        "junctions": {jid: {"plan_xy": j.get("plan_xy"), "label": j.get("label", jid),
                            "floor": j.get("floor", 0), "volume_m3": j.get("volume_m3"),
                            "sensor": False}
                      for jid, j in house.get("junctions", {}).items()},
        "windows": {wid: {"room": w.get("room"), "facade_azimuth_deg": w.get("facade_azimuth_deg"),
                          "kind": "skylight" if w.get("tilt_deg", 90) < 45 else "window",
                          "area_m2": w.get("area_m2"), "glass_m2": w.get("glass_m2"),
                          "max_open_area_m2": w.get("max_open_area_m2", 0.0),
                          "center_height_m": w.get("center_height_m"),
                          "tilt_frac": w.get("tilt_frac"), "tilt_deg": w.get("tilt_deg"),
                          # Opgelost effectief-oppervlak (open_type/override) voor de
                          # JS-speeltuin, zodat die dezelfde korting rekent (additief).
                          "eff_open_frac": _eff_open_area(w),
                          "plan_side": w.get("plan_side"), "plan_pos": w.get("plan_pos"),
                          "label": w.get("label", wid)}
                    for wid, w in house.get("windows", {}).items()},
        "vents": {vid: {"room": v.get("room"), "facade_azimuth_deg": v.get("facade_azimuth_deg"),
                        "area_m2": v.get("area_m2"), "max_open_area_m2": v.get("max_open_area_m2"),
                        "eff_open_frac": _eff_open_area(v),
                        "center_height_m": v.get("center_height_m"),
                        "default_state": v.get("default_state", "open"),
                        "plan_side": v.get("plan_side"), "plan_pos": v.get("plan_pos"),
                        "label": v.get("label", vid)}
                  for vid, v in house.get("vents", {}).items()},
        "doors": {did: {"between": d.get("between"), "label": d.get("label", did),
                        "area_m2": d.get("area_m2"), "center_height_m": d.get("center_height_m"),
                        "default_state": d.get("default_state", "open"),
                        "plan_pos": d.get("plan_pos"),
                        "fixed": bool(d.get("fixed"))}
                  for did, d in house.get("doors", {}).items()},
        "sim": {"leak_area": LEAK_AREA, "dp_lam": DP_LAM, "wind_ref_z": WIND_REF_Z},
    }


def _controls(house: dict, states_now: dict) -> list[dict]:
    """Lijst bedienbare elementen (ramen, roosters, deuren) met hun huidige gerapporteerde
    stand — de bron voor de 'Stel ramen/roosters in'-modal op het dashboard."""
    out = []
    for wid, w in house.get("windows", {}).items():
        if w.get("max_open_area_m2", 0.0) <= 0.0:
            continue   # vast glas — niet bedienbaar, hoort niet in de modal
        out.append({"id": wid, "kind": "window", "label": w.get("label", wid),
                    "room": w.get("room"), "state": states_now.get(wid, "dicht")})
    for vid, v in house.get("vents", {}).items():
        out.append({"id": vid, "kind": "vent", "label": v.get("label", vid),
                    "room": v.get("room"), "state": states_now.get(vid, "open")})
    # Bedienbare zonwering (lamella, buitenscherm, verduisteringsgordijn): wanneer dicht
    # gemeld, dempt het de zoninstraling door dat raam.
    for wid, w in house.get("windows", {}).items():
        sh = w.get("shade")
        if not sh:
            continue
        out.append({"id": wid + "_shade", "kind": "shade",
                    "label": f"{sh.get('label', 'zonwering')} — {w.get('label', wid)}",
                    "room": w.get("room"), "state": states_now.get(wid + "_shade", "open")})
    for did, d in house.get("doors", {}).items():
        if d.get("fixed"):
            continue   # permanente doorgang (geen deur) → niet bedienbaar, niet tonen
        out.append({"id": did, "kind": "door", "label": d.get("label", did),
                    "between": d.get("between"),
                    "state": states_now.get(did, d.get("default_state", "open"))})
    return out


def _flow_summary(house, params, sim, timeline, now) -> list[dict]:
    if not timeline:
        return []
    # Plattegrond toont "nu": de laatste stap ≤ now + de op now vastgelegde zone-temps.
    step = timeline[-1]
    for s in timeline:
        if s["t"] <= now:
            step = s
        else:
            break
    ta = sim.get("Ta_now", sim["Ta"])
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    ops = build_openings(house, step["states"], step["weather"], params, ta, step["T_out"])
    net = solve_network(zones, ops, ta, step["T_out"])
    out = []
    for op, q in zip(ops, net["flows"]):
        if op["id"].startswith("_leak_"):
            continue
        out.append({"id": op["id"], "a": op["a"], "b": op["b"],
                    "flow_m3s": round(q, 4), "area": round(op["area"], 3)})
    return out


# Caching-stub (de log wordt één keer geladen in main en doorgegeven).
_OPENINGS_CACHE: list[dict] = []
def load_openings_log_cached() -> list[dict]:
    return _OPENINGS_CACHE


def _timedelta_h(h: float):
    return timedelta(hours=h)


def _interp_hourly(rows: list[dict], t: datetime, key: str) -> float:
    """Lineaire interpolatie van een uurlijkse driver-reeks op tijdstip t."""
    if t <= rows[0]["dt"]:
        return rows[0].get(key) or 0.0
    if t >= rows[-1]["dt"]:
        return rows[-1].get(key) or 0.0
    for r0, r1 in zip(rows, rows[1:]):
        if r0["dt"] <= t <= r1["dt"]:
            v0, v1 = r0.get(key) or 0.0, r1.get(key) or 0.0
            f = (t - r0["dt"]).total_seconds() / max(1.0, (r1["dt"] - r0["dt"]).total_seconds())
            return v0 + f * (v1 - v0)
    return rows[-1].get(key) or 0.0


# ════════════════════════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════════════════════════

# Defaults; de `location` in house_model.json overschrijft deze in main().
_LAT = shared_const.LATITUDE
_LON = shared_const.LONGITUDE


def main():
    global _LAT, _LON, _OPENINGS_CACHE, _NEIGHBOR_TEMP
    now = datetime.now(TZ)
    print(f"[airflow_model] Start — {now.isoformat()}")

    house = load_house()
    loc = house.get("location", {})
    _LAT = loc.get("lat", _LAT)
    _LON = loc.get("lon", _LON)

    wd = load_window_data()
    weather = fetch_weather()
    # Buiten-nu verfijnen met het eigen WU-station (zoals soil/window): de station-temp
    # + RH zijn lokaler dan het Open-Meteo-grid (dat te warm leek op het dashboard). De
    # temp krijgt de wu_bias-stralingscorrectie (driver = eigen pyranometer, Open-Meteo-
    # zon als fallback). Bewust NIET overgenomen van WU: wind (snelheid/richting — het
    # station meet die onbetrouwbaar, blijft Open-Meteo) en de zon-instraling voor de
    # glasfysica (die heeft de direct/diffuus-split nodig die WU niet levert; WU-zon dient
    # hier enkel als bias-driver). Alleen de "nu"-uitlezing wordt verfijnd — de historische
    # timeline die de kalibratie voedt blijft Open-Meteo (WU levert geen uur-historie en de
    # ground truth zijn de tado-kamertemps).
    cur = weather.get("current", {}) or {}
    out_rh_temp = cur.get("temperature_2m")   # rauwe temp bij de gebruikte RH (één sensorpaar)
    wu_temp, wu_solar, wu_humid = fetch_wu_current_temp()
    wu_solar_scale = None   # WU/OM glas-drive-herschaling (stap 1); None → pure Open-Meteo
    if wu_temp is not None:
        solar_now = wu_solar if wu_solar is not None else cur.get("shortwave_radiation")
        src = "wu" if wu_solar is not None else "om"
        cur["temperature_2m"] = round(correct_temp(wu_temp, solar_now), 1)
        cur["outside_source"] = "wu"
        out_rh_temp = wu_temp                 # rauwe WU-temp hoort bij de WU-RH (Magnus-paar)
        if wu_humid is not None:
            cur["relative_humidity_2m"] = wu_humid
        print(f"[buiten] WU: {wu_temp}°C → gecorrigeerd {cur['temperature_2m']}°C "
              f"(zon {solar_now} W/m², bron {src}); RH {wu_humid}%")
        # WU-gemeten-zon herschaling van de glas-drive rond nu: k = WU_global/OM_global (stap 1).
        om_solar_now = cur.get("shortwave_radiation")
        if (wu_solar is not None and om_solar_now and om_solar_now >= WU_SOLAR_MIN_WM2
                and wu_solar >= WU_SOLAR_MIN_WM2):
            wu_solar_scale = min(WU_SOLAR_SCALE_MAX,
                                 max(WU_SOLAR_SCALE_MIN, wu_solar / om_solar_now))
            print(f"[zon] WU/OM glas-drive-herschaling k={wu_solar_scale:.2f} "
                  f"(WU {wu_solar}, OM {om_solar_now} W/m²)")
    else:
        cur["outside_source"] = "open-meteo"
        print("[buiten] WU niet beschikbaar → Open-Meteo buiten-nu.")
    weather["current"] = cur
    weather["wu_solar_scale"] = wu_solar_scale
    log = load_openings_log()
    _OPENINGS_CACHE = log

    # Buur-anker voor de party-muren: traag, gedempt (zie neighbor_temp_estimate).
    _NEIGHBOR_TEMP = neighbor_temp_estimate(weather.get("hourly", []), now)
    print(f"[buren] party-muur-anker (NEIGHBOR_TEMP) = {_NEIGHBOR_TEMP:.1f} °C")

    learned = load_learned()
    # Fysica-revisie-migratie (zie PHYSICS_REV): merged_params reset de globalen; hier vervalt
    # daarnaast het checkpoint (params + skill-lat zijn op de oude fysica geoogst — een fossiel
    # per definitie) en wordt de anomalie-poort deze ene run overgeslagen (haar RMSE-norm komt
    # uit de oude-fysica-leercurve).
    physics_migrated = physics_rev_migration_needed(learned)
    if physics_migrated:
        learned = dict(learned)
        learned.pop("checkpoint", None)
        print(f"[fysica] revisie {learned.get('physics_rev', 1)} → {PHYSICS_REV}: "
              "cp_shelter/vent_eff terug naar hun prior, checkpoint vervallen, "
              "anomalie-poort deze run overgeslagen.")
    params = merged_params(house, learned)

    since = now - _timedelta_h(CALIB_WINDOW_H)
    actual = collect_actual(house, wd, since)
    # Seed-bron vóór de AC-filter: de eerste (oudste) sample per kamer blijft de seed, óók als de
    # AC-filter latere samples wegneemt — zo blijft de voorspelling van de AC-kamer geankerd.
    seed_src = {rid: s[0][1] for rid, s in actual.items() if s}

    # Mobiele airco: het model heeft géén actieve-koel-term, dus de kamer waar de airco staat leest
    # kouder dan de fysica kan verklaren. Fitten op die AC-koude zou de kamer-params + RMSE/leercurve
    # + de gedeelde globale params vervuilen. Daarom laten we de samples van de AC-kamer — alléén voor
    # de uren dat de airco er volgens de log stond — uit `actual` vallen: niet gekalibreerd, maar nog
    # wél gesimuleerd en op het dashboard getoond (voorspeld-warm vs. gemeten-koud is eerlijk). De
    # overige kamers blijven schoon. (Tweede-orde: de AC-kamer-knoop loopt zélf warm door en kan via
    # de deur-advectie de gekoppelde kamers licht beïnvloeden — geaccepteerd; het alternatief is een
    # volledige AC-koel-term, bewust niet gekozen.)
    acc = ac_changes(log)
    ac_room_now = ac_room_at(acc, now)
    actual, ac_excluded = filter_ac_samples(actual, acc, ac_room_now, now)
    if ac_excluded:
        print(f"[airco] kamer nu = {ac_room_now or '—'}; samples uit de fit gelaten: {ac_excluded}")

    # Verwarming: spiegelbeeld van de airco, maar auto-gedreven door de tado-verwarmingsvlag in
    # window_data.json (Project 6 schrijft 'm per sample). Het model heeft geen verwarmingsterm,
    # dus een gestookte kamer leest te warm → laat die samples per stuk uit de fit vallen. Geen
    # guard nodig (de vlag is exact getimed). Kamer blijft gesimuleerd + getoond (seed uit seed_src).
    heat_on = collect_heating_on(house, wd, since)
    heat_now = heating_now(house, wd)
    actual, heat_excluded = filter_heating_samples(actual, heat_on)
    if heat_excluded:
        rooms_heating = [rid for rid, on in heat_now.items() if on]
        print(f"[verwarming] kamers nu aan = {rooms_heating or '—'}; "
              f"samples uit de fit gelaten: {heat_excluded}")

    # Pauze: huis-breed "niemand betrouwbaar meldt de standen nu" (iemand anders is thuis).
    # Spiegelbeeld van AC/verwarming maar huis-breed i.p.v. per kamer — zie filter_paused_samples.
    pchanges = pause_changes(log)
    p_intervals = paused_intervals(pchanges, now)
    paused_now = paused_at(pchanges, now)
    paused_since = p_intervals[-1][0] if paused_now and p_intervals else None
    actual, pause_excluded = filter_paused_samples(actual, p_intervals)
    if pause_excluded:
        print(f"[pauze] gepauzeerd nu = {paused_now}; samples uit de fit gelaten: {pause_excluded}")

    # Timeline reikt WARMUP_H vóór het residu-venster (sim-only massaknoop-aanloop); residuen
    # tellen alleen waar tado-samples bestaan (binnen CALIB_WINDOW_H, zie collect_actual).
    timeline = build_timeline(house, weather, log, now, CALIB_WINDOW_H + WARMUP_H,
                              wu_solar_scale=wu_solar_scale, beam_iam=True)
    if not timeline:
        print("[airflow_model] Geen weerdata → kan niet simuleren. Stop.")
        sys.exit(1)

    # Seed de luchtknoop op de eerste werkelijke meting per kamer (anders buiten); seed_src
    # behoudt de AC-kamer-seed ook al is die uit `actual` gefilterd.
    seed = {}
    for rid, t0 in seed_src.items():
        seed[rid] = t0
    for rid in house.get("rooms", {}):
        seed.setdefault(rid, timeline[0]["T_out"])

    # Weer-context van het venster (stempelt de leercurve) + maakt het solar_gain-anker
    # regime-bewust in de kalibratie (zie reg_weight): op een zonnig venster wordt de
    # zon-magnitude vertrouwd, op een bewolkt venster sterk verankerd.
    wx_summary = window_weather_summary(weather, now, CALIB_WINDOW_H)

    # Leren (alleen als er werkelijke samples zijn om tegen te ijken).
    rmse_now = float("nan")
    learning_held = False
    baseline = None
    anomaly_nudge_at = learned.get("anomaly_nudge_at")   # zie ANOMALY_NUDGE_COOLDOWN_H
    if actual:
        print(f"[leren] {sum(len(v) for v in actual.values())} samples over "
              f"{len(actual)} kamers in het venster.")
        # Anomalie-poort: hoe goed voorspellen de húidige parameters? Is die fout veel
        # groter dan normaal, dan klopt de opening-log vermoedelijk niet met de
        # werkelijkheid → leren pauzeren zodat de fysica niet scheefgetrokken wordt.
        rmse_cur = rmse(_residuals(house, params, timeline, seed, actual, set(actual.keys())))
        if physics_migrated:
            # De anomalie-norm komt uit de oude-fysica-leercurve; met net gereset-globalen zou
            # een vals "anomaal" de éérste leer-run op de nieuwe fysica blokkeren.
            anomaly_held, baseline = False, None
        else:
            anomaly_held, baseline = should_hold_learning(rmse_cur,
                                                          learned.get("rmse_history", []))
        learning_held = anomaly_held or paused_now
        if learning_held:
            rmse_now = rmse_cur
            if paused_now:
                print(f"[pauze] gepauzeerd → parameters vastgehouden (RMSE {rmse_cur:.2f}°C); "
                      "niemand betrouwbaars meldt nu de standen, dit venster leert niet mee.")
            if anomaly_held:
                print(f"[leren] fout anomaal hoog ({rmse_cur:.2f}°C vs norm {baseline:.2f}°C) → "
                      "parameters vastgehouden; de opening-log klopt mogelijk niet met de "
                      "werkelijkheid. Voorspellen gaat door, leren is gepauzeerd.")
                # Nudge naar de privé-chat: de poort beschermt de fysica, maar alleen een
                # mens kan de log corrigeren — en die moet het dan wel hóren. Cooldown +
                # episode-reset: zie ANOMALY_NUDGE_COOLDOWN_H. send_telegram raist nooit.
                if should_nudge_anomaly(anomaly_nudge_at, now):
                    msg = anomaly_nudge_text(rmse_cur, baseline)
                    if os.getenv("DRY_RUN") == "1":
                        print(f"[nudge] DRY_RUN — zou sturen:\n{msg}")
                    else:
                        send_telegram(msg)
                    anomaly_nudge_at = now.isoformat()
        else:
            params, rmse_now = calibrate(house, params, timeline, seed, actual,
                                         solar_mean=wx_summary.get("solar_mean"))
            print(f"[leren] RMSE na kalibratie: {rmse_now:.3f} °C")
            rails = railed_params(params)
            if rails:
                print(f"[saturatie] params op hun grens: {', '.join(rails)}")
        if not anomaly_held:
            anomaly_nudge_at = None   # episode voorbij → een vólgende anomalie nudget meteen
    else:
        print("[leren] geen werkelijke kamertemps in het venster → alleen voorspellen.")
    # Defensief: als het héle kalibratievenster gepauzeerd was, kan `actual` volledig leeg zijn
    # (filter_paused_samples liet dan elke kamer vallen) → de if-tak hierboven werd overgeslagen
    # en `learning_held` bleef False. Zet 'm alsnog zodat checkpoint/backfill/dashboard een
    # gepauzeerd huis nooit als "geleerd" behandelen.
    learning_held = learning_held or paused_now

    # Weer-genormaliseerde skill t.o.v. een persistentie-baseline — de apples-to-apples
    # kwaliteitsmaat die niet met de weersmoeilijkheid meebeweegt.
    rmse_baseline = naive_rmse(actual) if actual else float("nan")
    skill = skill_score(rmse_now, rmse_baseline)

    # Beste-params-checkpoint + auto-fallback (zie checkpoint_step). Alléén op écht-geleerde
    # runs: een gepauzeerde (held) run veranderde de params niet en moet niet als verslechtering
    # tellen. Bij een fallback hermergen we priors voor nieuwe keys, herberekenen we de RMSE en
    # houden we het resultaat tegen de werkelijkheid: passen de checkpoint-params op het huidige
    # venster NIET beter dan wat net geleerd is, dan is het optimum een fossiel → fallback
    # verwerpen en het checkpoint her-zetelen op de huidige params (breekt de fallback-lus,
    # zie de toelichting bij FALLBACK_AFTER).
    ckpt = learned.get("checkpoint") or {}
    fell_back = False
    if actual and not learning_held and rmse_now == rmse_now:
        learned_params = params
        prev_fallback = ckpt.get("last_fallback")
        params, ckpt, fell_back = checkpoint_step(ckpt, params, skill, rmse_now,
                                                  model_version(), now.isoformat())
        if fell_back:
            for rid in house.get("rooms", {}):
                params.setdefault(rid, default_params(house)[rid])
                for k in PER_ROOM_PARAMS:
                    params[rid].setdefault(k, PRIORS[k])
            rmse_fb = rmse(_residuals(house, params, timeline, seed, actual, set(actual.keys())))
            if rmse_fb == rmse_fb and rmse_fb <= rmse_now:
                rmse_now = rmse_fb
                skill = skill_score(rmse_now, rmse_baseline)
                # Her-vloer de skill-lat op wat de teruggezette params NU halen — anders blijft
                # de oude high-water-mark staan en jojo't het leren hier elke ~FALLBACK_AFTER
                # runs opnieuw naartoe (zie accept_fallback_checkpoint).
                ckpt = accept_fallback_checkpoint(ckpt, skill, rmse_now, now.isoformat())
                print(f"[checkpoint] {FALLBACK_AFTER} runs verslechterd → teruggevallen op de "
                      f"checkpoint-params (RMSE nu {rmse_now:.3f} °C, skill {skill}; "
                      f"skill-lat her-gevloerd).")
            else:
                params = learned_params
                fell_back = False
                ckpt = reseat_checkpoint(learned_params, skill, rmse_now, model_version(),
                                         now.isoformat(), last_fallback=prev_fallback)
                print(f"[checkpoint] fallback verworpen: checkpoint-params passen slechter op "
                      f"het huidige venster ({rmse_fb:.3f} vs {rmse_now:.3f} °C) — verouderd "
                      f"optimum; checkpoint her-gezeteld op de huidige params (skill {skill}).")
        elif ckpt.get("t") == now.isoformat():
            print(f"[checkpoint] nieuw skill-optimum vastgelegd (skill {skill}, "
                  f"RMSE {rmse_now:.3f} °C).")

    # Voorspelling met de (geleerde) params over het volledige venster + vooruitblik.
    sim = simulate(house, params, timeline, seed,
                   calib_only_rooms=set(house.get("rooms", {}).keys()),
                   snapshot_t=now)
    if sim.get("solver_failures"):
        print(f"[sim] {sim['solver_failures']} substap(pen) met een bijna-singulier thermisch "
              "stelsel — Ta/Tm die stap bevroren (zie learned.solver_failures).")

    # Passieve suggestie op basis van nú.
    cur = weather["current"]
    out_t = cur.get("temperature_2m", timeline[-1]["T_out"])
    out_rh = cur.get("relative_humidity_2m")
    room_now = {room.get("from_window_data"): wd.get("rooms", {}).get(room.get("from_window_data"), {}).get("inside")
                for room in house.get("rooms", {}).values()}
    wx_now = {"wind_speed": cur.get("wind_speed_10m", 0.0), "wind_dir": cur.get("wind_direction_10m", 0.0),
              "gust": cur.get("wind_gusts_10m", 0.0), "precip": 0.0}
    # out_rh_temp = de rauwe temp horend bij out_rh (WU-paar indien beschikbaar) voor convert_rh.
    sugg = suggest(house, params, wx_now, room_now, out_t, out_rh, out_rh_temp)
    print(f"[suggestie] {sugg['headline']}")

    dash = build_dashboard(house, params, weather, wd, timeline, sim, sugg, learned,
                           actual, now, rmse_now, learning_held=learning_held, baseline=baseline,
                           skill=skill, rmse_baseline=rmse_baseline, wx=wx_summary,
                           checkpoint=ckpt, fell_back=fell_back,
                           ac_room=ac_room_now, ac_excluded=ac_excluded,
                           heat_now=heat_now, heat_excluded=heat_excluded,
                           paused_now=paused_now, paused_since=paused_since,
                           pause_excluded=pause_excluded)
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(dash, f, ensure_ascii=False, indent=2)
    with open(LEARNED_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now.isoformat(), "model_version": model_version(),
                   "physics_rev": PHYSICS_REV,   # zie PHYSICS_REV: poort voor de globalen-migratie
                   # Cooldown-stempel van de anomalie-nudge (additief; None buiten een episode).
                   "anomaly_nudge_at": anomaly_nudge_at,
                   "params": params,
                   "rmse": round(rmse_now, 3) if rmse_now == rmse_now else None,
                   "railed": railed_params(params),   # saturatie-tell (additief, diagnostisch)
                   "rmse_history": dash["learned"]["rmse_history"],
                   "checkpoint": ckpt},
                  f, ensure_ascii=False, indent=2)
    print(f"[airflow_model] Geschreven: {DASHBOARD_FILE} + {LEARNED_FILE}. Klaar.")


if __name__ == "__main__":
    # fail_threshold=6: kwartierloop — pas alerten bij ~1,5 uur aanhoudende
    # storing. Een gemiste iteratie is onschuldig (de volgende haalt bij);
    # alleen een echte outage is een bericht waard.
    run_guarded(main, "airflow-twin", fail_threshold=6)

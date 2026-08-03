#!/usr/bin/env python3
"""
vent_physics.py — de fysicakern van de ventilatie-tweeling (Project 13).

Vrijwel woordelijke port van de gevalideerde fysica van airflow_model.py
(Project 8, RMSE 0,60 °C, nul gerailde params bij pensionering): zonnegeometrie
+ gevelinstraling (DNI-conventie), het meerzone-drukwerknetwerk (Newton met
gedempte herkansing), eenzijdige ventilatie, het 2-knoops RC-thermisch model
met tussenwoning-termen (buur-anker, bodemkoppeling, interne geleiding,
dak-sol-air), en de trap-stratificatie (γ op gemeten temps + Brown–Solvason-
tegenstroom). Zie AIRFLOW_ASSESSMENT.md / AIRFLOW2_ASSESSMENT.md voor de
meetgeschiedenis die deze termen draagt.

Puur: geen I/O, geen netwerk, en — anders dan zijn voorganger — géén muteerbare
module-globals. Alle run-gebonden ankers reizen via RunContext.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import shared_const

TZ = shared_const.TZ


@dataclass(frozen=True)
class RunContext:
    """Run-gebonden ankers — vervangt de oude module-globals (_LAT/_LON/
    _NEIGHBOR_TEMP/_GROUND_TEMP). Elke consument bouwt er precies één
    (vent_io.make_context) en geeft 'm expliciet dóór; vergeten is een
    TypeError, niet stilletjes-verkeerd (de night_forecast-les). Per-venster
    anker-wissels in tools: dataclasses.replace(ctx, neighbor_temp=...)."""

    lat: float
    lon: float
    neighbor_temp: float
    ground_temp: float

# ── Fysische constanten ─────────────────────────────────────────────────────────────
CP_AIR = 1005.0    # J/(kg·K)

G      = 9.81       # m/s²

P_ATM  = 101325.0   # Pa

R_AIR  = 287.05     # J/(kg·K)

                               # pauze-filters) waaronder het dashboard waarschuwt: het venster
                               # is nominaal 48u vol, maar de filters kunnen er stilletjes veel
                               # minder van overlaten — dan leunt de fit op te weinig data
SUBSTEP_S      = 300.0   # interne tijdstap (s) voor de Euler-integratie (stabiliteit)

SOLAR_SUBSTEPS = 3       # sub-samples per 15-min stap voor het tijdsgemiddelde van de instraling:

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
# het 3-daags buitengemiddelde. De runner zet die schatting in RunContext.neighbor_temp
# (make_context); NEIGHBOR_TEMP hieronder is enkel nog de default/vloerwaarde van die schatting.
NEIGHBOR_TEMP = 20.0

NEIGHBOR_WINTER_FLOOR = 19.5   # °C — buren stoken 's winters minstens tot ~deze temp

# Zomerplafond op het buur-anker, plus een lagere nachtcap. Zonder cap volgt het anker het
# 3-daags buitengemiddelde onbegrensd omhoog en wordt het in een hittegolf een 24–26°C
# WARMTEBRON tegen de party-muren — terwijl een binnenkant van een onbewoonde/niet-gekoelde
# buurwoning in de praktijk rond 23°C blijft steken, en 's nachts meekoelt. Overgenomen uit
# tweeling 2, waar beide varianten held-out zijn getoetst (juli 2026, 3-voudige 0-epoch-toets
# over 9 gepaarde vensters): `cap23` won −0.29°C op het hittegolf-venster en was inert op de
# 8 andere; `cap23_night` won of was exact gelijk op álle 9 (grootste winst 0.919 → 0.820).
# Tweeling 1 draaide tot nu toe zónder beide.
NEIGHBOR_SUMMER_CAP = 23.0     # °C — dagplafond op het anker

NEIGHBOR_NIGHT_CAP = 21.0      # °C — nachtplafond (23–07u), met gladde 1-uurs overgangen

# ── Bodemkoppeling (kruipruimte onder de begane grond) ───────────────────────────────
# Zonder deze term kan een kamer zijn warmte alléén kwijt aan buitenlucht (UA_env/UA_mass)
# of aan het buur-anker. Op een hete dag zijn dat allebei WARMTEBRONNEN — buiten 27.5°C,
# buur-anker 20°C, tegen kamers die in werkelijkheid op 21–23°C staan, dus 5–6°C ónder de
# buitentemp. Het model kon dat niveau simpelweg niet halen, en de optimizer had geen andere
# uitweg dan élk warmte-in-kanaal naar zijn ondergrens te duwen (solar_gain op zijn vloer,
# ua_env op zijn vloer) en werd het nóg steeds niet koel genoeg: de klassieke saturatie-tell
# van een ontbrekende term, niet van verkeerde afstelling (gediagnosticeerd juli 2026).
# De kruipruimte onder living/ted is die koude put. Hij is geventileerd — dus deels gekoppeld
# aan de buitenlucht — én aan de bodem, die op 1–2 m diepte rond het jaargemiddelde van de
# luchttemperatuur blijft. Vandaar een blend van beide i.p.v. één van de twee: een puur
# buiten-gedempt anker zou 's zomers te warm uitkomen (30-daags gemiddelde ~20°C in juli),
# een pure bodemtemperatuur te koud en seizoensdoof.
GROUND_U            = 0.6    # W/(m²K) — matig geïsoleerde houten vloer boven de kruipruimte

GROUND_LOOKBACK_H   = 720.0  # uur (~30 dagen) — dempingsvenster van het buitengemiddelde

GROUND_SOIL_ANCHOR  = 11.0   # °C — NL jaargemiddelde luchttemp ≈ diepe bodemtemp

GROUND_AIR_COUPLING = 0.5    # aandeel waarmee de kruipruimte het gedempte buiten volgt

GROUND_TEMP_MIN     = 6.0    # °C — klem, tegen een absurd anker bij korte/rare historie

GROUND_TEMP_MAX     = 20.0

GROUND_TEMP         = 15.0   # module-default (back-compat voor directe simulate-tests)

# ── Interne geleiding tussen zones (vloeren/plafonds + binnenwanden) ─────────────────
# Het huis had géén geleidende koppeling tussen kamers: de enige kamer↔kamer-weg was
# luchtadvectie door open deuren. In werkelijkheid zijn ted → hotties → office op elkaar
# gestapeld en zakt hun warmte door de vloeren omlaag (en uiteindelijk in de kruipruimte).
# Zonder die afvoer heeft de bovenste kamer geen uitweg en stapelt de fout zich op mét de
# hoogte — precies het gemeten patroon (ted +0.71, hotties +1.32, office +2.19 °C op een
# zonnige 27.5°C-middag). Een U-waarde is per definitie lucht-tot-lucht (de
# oppervlakte-overgangsweerstanden zitten erin), dus koppelen we LUCHT↔LUCHT, parallel aan
# de deur-advectie; de massa van de vloer zelf zit in `c_mass` van beide kamers.
INTERZONE_U = 0.7   # W/(m²K) — houten balklaag met stro ertussen (~R 1.3–1.4 incl. films)

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

def neighbor_night_cap(when) -> float:
    """Tijdsafhankelijk plafond op het buur-anker: NEIGHBOR_SUMMER_CAP overdag,
    NEIGHBOR_NIGHT_CAP 's nachts (23–07u), lineair overvloeiend in 22–23u en 07–08u —
    een harde sprong zou een knik in de voorspelde temp injecteren die als residu
    terugkomt (zelfde argument als bij `internal_gain_profile`)."""
    try:
        h = when.hour + when.minute / 60.0
    except AttributeError:
        return NEIGHBOR_SUMMER_CAP
    if h >= 23.0 or h < 7.0:
        return NEIGHBOR_NIGHT_CAP
    if 22.0 <= h < 23.0:
        return NEIGHBOR_SUMMER_CAP + (h - 22.0) * (NEIGHBOR_NIGHT_CAP - NEIGHBOR_SUMMER_CAP)
    if 7.0 <= h < 8.0:
        return NEIGHBOR_NIGHT_CAP + (h - 7.0) * (NEIGHBOR_SUMMER_CAP - NEIGHBOR_NIGHT_CAP)
    return NEIGHBOR_SUMMER_CAP

def neighbor_at(nb_base: float, when) -> float:
    """Het buur-anker op tijdstap `when`: de run-basiswaarde, geklemd op de nachtcap.
    Het anker is daarmee géén run-constante meer maar een trage dagcurve."""
    return min(nb_base, neighbor_night_cap(when))

def ground_temp_estimate(rows: list[dict], now: datetime,
                         lookback_h: float = GROUND_LOOKBACK_H) -> float:
    """Traag bodem-/kruipruimte-anker: de diepe bodemtemperatuur (≈ het jaargemiddelde van
    de luchttemp), voor `GROUND_AIR_COUPLING` opgetrokken naar het gemiddelde buiten over de
    laatste `lookback_h` (~30 dagen). Een geventileerde kruipruimte hangt tussen die twee in:
    puur bodem zou seizoensdoof zijn, puur (gedempt) buiten zou 's zomers te warm uitkomen.
    In een hete juli (30-daags gemiddelde ~20°C) geeft dit ~15.5°C, 's winters (~4°C) ~7.5°C.
    Geklemd op [GROUND_TEMP_MIN, GROUND_TEMP_MAX]; valt terug op GROUND_TEMP zonder historie.

    Alleen de temperatuur wordt hier geschat — hóé sterk de vloer eraan koppelt is de
    geleerde `ua_ground` per kamer, dus een matige schatting hier wordt door de fit
    opgevangen zolang de orde klopt."""
    since = now - timedelta(hours=lookback_h)
    temps = [r["T_out"] for r in rows
             if r.get("T_out") is not None and since <= r["dt"] <= now]
    if not temps:
        return GROUND_TEMP
    mean_out = sum(temps) / len(temps)
    t = GROUND_SOIL_ANCHOR + GROUND_AIR_COUPLING * (mean_out - GROUND_SOIL_ANCHOR)
    return max(GROUND_TEMP_MIN, min(GROUND_TEMP_MAX, t))

def interzone_conductances(house: dict, params: dict) -> dict:
    """(zone_a, zone_b) → geleiding W/K door de scheidende constructie (vloer/plafond tussen
    gestapelde kamers, binnenwand tussen buren op dezelfde verdieping).

    Lucht↔lucht: een U-waarde ís lucht-tot-lucht (de oppervlakte-overgangsweerstanden zitten
    erin), dus dit loopt parallel aan de deur-advectie en NIET via de massaknopen — die zouden
    de filmweerstanden dubbel tellen en een kunstmatige vertraging toevoegen. De massa van de
    vloer zelf zit al in `c_mass` van beide aangrenzende kamers.

    Eén globale geleerde schaal `ua_inter` i.p.v. een parameter per vlak: per-vlak-waarden zijn
    onderling degenereerbaar en tweeling 2 zit al op 65 parameters. Lege/afwezige lijst → geen
    koppeling → het model gedraagt zich exact als voorheen."""
    scale = params.get("ua_inter", 1.0)
    out: dict[tuple, float] = {}
    for e in house.get("interzone", []) or []:
        a, b = e.get("a"), e.get("b")
        if not a or not b or a == b:
            continue
        ua = e.get("area_m2", 0.0) * e.get("u", INTERZONE_U) * scale
        if ua > 0.0:
            key = (a, b) if a < b else (b, a)
            out[key] = out.get(key, 0.0) + ua
    return out

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

# Leakage (infiltratie) per kamer: een kleine, altijd aanwezige lek naar buiten. Houdt
# het luchtstroomnetwerk goed geconditioneerd (een verder dichte kamer is niet singulier)
# en is fysisch reëel (kieren). m² effectief lekoppervlak.
# Convergentie-eisen van het drukwerk-netwerk. NET_TOL is de max. toegestane massabalans-
# afwijking per zone (kg/s); NET_ALPHA_RETRY is de maximale Newton-stapfractie van de
# herkansing die een oscillerende iteratie breekt — zie de toelichting in solve_network.
NET_TOL = 1e-6

NET_ALPHA_RETRY = 0.5

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
    "ua_ground":   1.0,   # vloer → kruipruimte/bodem (alleen actief bij ground_m2 > 0)
    "ua_inter":    1.0,   # globale schaal op de interne vloer-/wandgeleiding tussen zones
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
    "ua_ground": (0.0, 4.0), "ua_inter": (0.0, 4.0),
    "f_air": (0.1, 0.9),   # absolute fractie zonwinst → luchtknoop (fysiek 0..1, marge gehouden)
}

# Welke parameters per kamer leren. `h_am` (lucht↔massa-koppeling) leert mee zodat `c_air`
# niet langer de énige knop is die bepaalt hoe snel de luchtknoop de drivers volgt — zonder een
# vrije `h_am` satureerde `c_air` op zijn bovengrens in álle kamers (degeneratie). `ua_roof`
# leert mee maar heeft basis 0 (→ geen effect, nul-gradient, ridge parkeert 'm op de prior) voor
# kamers zónder `roof_m2`. `f_air` (zon-split lucht/massa) leert mee zodat het model de midday-piek-
# timing kan vinden i.p.v. die met een te lage `solar_gain` weg te drukken. `ua_mass` blijft op
# zijn prior (minder vrijheid, stabieler).
# `ua_ground` leert mee met dezelfde nul-basis-logica als `ua_roof`: kamers zónder `ground_m2`
# hebben basis 0 → geen effect, nul-gradiënt, de ridge parkeert 'm op zijn prior.
PER_ROOM_PARAMS = ["c_air", "c_mass", "h_am", "ua_env", "solar_gain", "ua_party", "q_int",
                   "ua_roof", "ua_ground", "f_air"]

# `cd` is geen leerbare globale parameter meer: het is een fysische orifice-constante (~0.62) die
# óók de volumetrische ACH/flows (dashboard + suggest) zet. De thermische fit railde 'm naar zijn
# vloer (degenereert met `vent_eff` in de meng-koppeling ∝ cd·vent_eff), wat de getoonde airflow
# corrumpeert. Nu vast op CD; `vent_eff` draagt de meng-koppeling alleen.
GLOBAL_PARAMS   = ["cp_shelter", "vent_eff", "ua_inter"]

CD = PRIORS["cd"]   # vaste ontladingscoëfficiënt (niet geleerd)

RAIL_TOL = 0.02   # binnen deze fractie van de band-breedte → de param zit 'op zijn grens'

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

# Conventie van de `direct`-invoer. Open-Meteo's `direct_radiation` is de directe component
# op het HORIZONTALE vlak (GHI = diffuus + DNI·sin(zonshoogte)) — geverifieerd op de eigen
# shards: over 459 uren met instraling > 50 W/m² geldt `direct + diffuus == shortwave` tot op
# 0.00%. `facade_irradiance` behandelde 'm historisch als DNI (loodrecht op de straal) en
# vermenigvuldigde 'm direct met cos(invalshoek); dat leest structureel te laag, met een
# factor sin(zonshoogte) — 1.4× bij hoge zon, 2.5× bij lage zon, en op een plat dak geeft de
# functie dan 61–88% van `shortwave` waar ze exact `shortwave` hoort te geven. Omdat de fout
# met de zonstand meeschuift kan géén constante schaal (`solar_gain`, `ROOF_SOLAR_GAIN`) 'm
# opvangen — hij blijft als restfout-vorm zitten, precies op de zonnige middagen/avonden.
# Aan sinds fysica-rev 5 (augustus 2026). De held-out-campagne toonde géén meetbare
# RMSE-winst (≲0.02 °C, onder de A/A-ruisvloer van 0.032) maar ook géén regressie; de
# adoptiegrond is dus correctheid, niet prestatie — en P9 (zonwering) drempelt op absolute
# watts zónder leerbare schaal die de fout kan opvangen. De vlag blijft bestaan zodat
# tools/twin2_experiment.py het oude gedrag nog als arm kan draaien.
DIRECT_IS_HORIZONTAL = True

SIN_EL_FLOOR = math.sin(math.radians(3.0))   # klem op de 1/sin(el)-versterking vlak boven de horizon

MAX_DNI = 1100.0                             # W/m² — fysieke bovengrens (zonneconstante na atmosfeer)

def facade_irradiance(facade_az: float, sun_az: float, sun_el: float,
                      direct: float, diffuse: float, tilt_deg: float = 90.0,
                      diffuse_only: bool = False, horizon_deg: float = 0.0,
                      beam_iam: bool = False) -> float:
    """Instraling (W/m²) op een vlak met azimut `facade_az` en helling `tilt_deg` vanaf
    horizontaal (90 = verticaal raam, 0 = plat dakraam/skylight). Directe component via de
    invalshoek op het hellende vlak; diffuus via de hemelkoepel-viewfactor (1+cos β)/2.

    `direct` wordt als DNI (loodrecht op de straal) gebruikt; staat `DIRECT_IS_HORIZONTAL`
    aan, dan wordt de horizontale Open-Meteo-waarde éérst naar DNI omgerekend
    (`direct / sin(zonshoogte)`, geklemd) — zie de toelichting bij die vlag.

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
    beam = direct or 0.0
    if DIRECT_IS_HORIZONTAL:
        # horizontale directe component → DNI. De klemmen vangen de 1/sin(el)-singulariteit
        # vlak boven de horizon; daar is de beam sowieso verwaarloosbaar t.o.v. het diffuse.
        beam = min(beam / max(math.sin(math.radians(sun_el)), SIN_EL_FLOOR), MAX_DNI)
    direct_on = max(0.0, beam * max(0.0, cos_inc))
    if beam_iam:
        direct_on *= beam_iam_factor(cos_inc)   # scherende-hoek-transmissie-terugval (stap 2)
    return direct_on + diff_on

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

    def newton(P0: list[float], alpha_max: float = 1.0) -> tuple[list, list, bool]:
        """Gedempte Newton op de massabalans. Geeft (P, residu, geconvergeerd).
        `alpha_max` begrenst de stapfractie — zie de retry in solve_network."""
        P = list(P0)
        r = residual(P)
        for _ in range(40):
            if max(abs(v) for v in r) < NET_TOL:
                return P, r, True
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
            alpha = alpha_max
            r_try = r
            for _ in range(24):
                P_try = [P[j] + alpha * delta[j] for j in range(n)]
                r_try = residual(P_try)
                if sse(r_try) < sse0:
                    break
                alpha *= 0.5
            else:
                break   # geen verbeterende stap meer — de line search zit vast
            P = P_try
            r = r_try
        return P, r, max(abs(v) for v in r) < NET_TOL

    P0 = list(P_init) if P_init and len(P_init) == n else [0.0] * n
    P, r, converged = newton(P0)
    if not converged:
        # **Gedempte herkansing.** De volle Newton-stap kan gaan OSCILLEREN: bij ≥6 m/s
        # sprong de druk van hotties heen en weer tussen ~12.5 en ~0.4 Pa, terwijl de
        # line search elke stap accepteerde (de SSE dáált immers, maar minimaal: 3.12 → 2.40
        # in 20 iteraties). Na 40 iteraties gaf de solver dat stilzwijgend terug alsof het
        # een oplossing was — massabalans 1.4 kg/s tegen een tolerantie van 1e-6, goed voor
        # een fantoom-ventilatie van ~135 ACH waar het echte antwoord 1.5 ACH is
        # (gediagnosticeerd augustus 2026). Géén tweede wortel dus, maar één wortel plus een
        # afgekapte iteratie. Een stapfractie van ten hoogste NET_ALPHA_RETRY breekt de
        # oscillatie: getest van 5.5 tot 12 m/s convergeert hij dan in 17–19 iteraties.
        # De volle stap blijft de eerste poging, zodat de normale (verreweg meest
        # voorkomende) solve niets langzamer wordt.
        P, r, converged = newton(P0, alpha_max=NET_ALPHA_RETRY)

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
            "pressures": {z: P[idx[z]] for z in zones}, "P": P,
            # Eerlijk over de oplos-kwaliteit: een niet-geconvergeerde solve is géén geldige
            # drukverdeling en de debieten eruit zijn fysiek onzin (massa uit het niets).
            # Callers tellen dit mee i.p.v. het stilzwijgend te slikken.
            "converged": converged, "residual": max(abs(v) for v in r) if r else 0.0}

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

# ── Eenzijdige ventilatie (de Gids & Phaff 1982) ────────────────────────────────────
# Het drukwerk-netwerk lost per opening het NETTO debiet op. Staat er in een kamer één raam
# open zonder doorstroompad, dan is dat netto debiet vrijwel nul — terwijl er in werkelijkheid
# een forse tweerichtings-uitwisseling dóór diezelfde opening loopt, gedreven door buoyantie
# (warm eruit bovenlangs, koel erin onderlangs) en door turbulente winddruk-pulsatie. Exact
# dezelfde blinde vlek die `buoyant_door_exchange` voor bínnendeuren repareert.
#
# Gemeten (AIRFLOW2_ASSESSMENT.md §3–§5, augustus 2026, ná de solver-correctie): met één
# raam open leest het netwerk 0.58 ACH waar de correlatie 14.2 geeft — een 15–25×
# onderschatting bij weinig wind. (De eerder gerapporteerde "141 ACH bij 6 m/s / helling
# 243×" bleek een níet-geconvergeerde solve: geconvergeerd is het 1.49 ACH en is de echte
# netwerk-helling ~5×, zie §5.) De onderschatting bij lage wind blijft de gemeten
# kernfout, en de fit kan hem niet repareren: `cp_shelter`/`vent_eff` zijn lineaire
# vermenigvuldigers en vullen een ontbrékende uitwisselingsterm niet in — vandaar dat
# ua_env/ua_party/f_air collectief in hun vloer werden gedrukt.
#
# Q = (A/2)·√(C1·U² + C2·H·ΔT + C3), U op gevelhoogte, H = hoogte van de opening.
SS_C1, SS_C2, SS_C3 = 0.001, 0.0035, 0.01
SS_MIN_TILT_DEG = 45.0   # alleen (bijna-)verticale ramen; een plat dakraam is een ander regime

def single_sided_exchange(area_m2: float, height_m: float, wind_ms: float,
                          t_in: float | None, t_out: float) -> float:
    """Eenzijdig ventilatiedebiet (m³/s) door één open buitenraam — de Gids & Phaff (1982).
    Nul bij een dicht raam. Dit is de uitwisseling die het netto-netwerkdebiet mist."""
    if area_m2 <= 0.0:
        return 0.0
    dt = 0.0 if t_in is None else abs(t_in - t_out)
    v = math.sqrt(SS_C1 * (wind_ms or 0.0) ** 2 + SS_C2 * max(0.0, height_m) * dt + SS_C3)
    return 0.5 * area_m2 * v

def single_sided_fresh(house: dict, states: dict, weather: dict,
                       zone_temps: dict, outside_temp: float) -> dict[str, float]:
    """Per kamer de eenzijdige verse-lucht-bijdrage van haar open, (bijna-)verticale
    buitenramen (m³/s). Roosters blijven erbuiten: de correlatie is voor raamopeningen, en
    een spleet van 0.01 m² levert er sowieso verwaarloosbaar in."""
    wind = weather.get("wind_speed") or 0.0
    out: dict[str, float] = {}
    for wid, w in house.get("windows", {}).items():
        rid = w.get("room")
        if rid is None or w.get("tilt_deg", 90.0) < SS_MIN_TILT_DEG:
            continue
        frac = _open_frac(states[wid], w) if wid in states else _default_frac(w, "window")
        max_open = w.get("max_open_area_m2", 0.0)
        # Bewust ZONDER `_eff_open_area`: dat is de aerodynamische contractiefactor voor de
        # orifice-vergelijking, en de C-constanten van de Gids & Phaff zijn juist geijkt tegen
        # het geometrische openingsoppervlak van echte ramen. Beide toepassen zou de
        # contractie dubbel verdisconteren.
        area = frac * max_open
        if area <= 0.0:
            continue
        # Hoogte van het openende deel; bij gebrek aan een expliciete maat de wortel van het
        # openende vlak (vierkant-benadering) — de term gaat met √H, dus dit is mild.
        height = w.get("open_height_m") or math.sqrt(max(max_open, 1e-6))
        out[rid] = out.get(rid, 0.0) + single_sided_exchange(
            area, height, wind, zone_temps.get(rid), outside_temp)
    return out

def effective_fresh(fresh: dict, house: dict, states: dict, weather: dict,
                    zone_temps: dict, outside_temp: float) -> dict[str, float]:
    """Netto-netwerk-verselucht aangevuld met de eenzijdige term, per zone het **maximum**
    van de twee — géén som. Bij echte dwarsventilatie overtreft het netto debiet de
    pulserende uitwisseling en is het netwerk juist; zonder doorstroompad neemt de
    eenzijdige term het over. Zo wordt niets dubbel geteld.

    Bewust binnen de geleerde `vent_eff` (anders dan `buoyant_door_exchange`, dat er
    bewust buiten valt): dít is écht buitenlucht die de kamer in komt en met de kamerlucht
    moet mengen — precies waar `vent_eff` voor staat."""
    ss = single_sided_fresh(house, states, weather, zone_temps, outside_temp)
    if not ss:
        return fresh
    return {z: max(fresh.get(z, 0.0), ss.get(z, 0.0)) for z in set(fresh) | set(ss)}

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

def _measured_at(series: list[tuple], ts: datetime) -> float | None:
    """Gemeten waarde op `ts`, of None buiten de gemeten reeks. Anders dan `_interp`, dat aan
    de randen vlak extrapoleert — voor de koker-gradiënt is "geen meting" iets anders dan
    "de laatste meting blijft eeuwig gelden", want dan zou het profiel bevriezen zodra de
    tado-historie ophoudt (o.a. in het hele voorspel-venster)."""
    if not series or ts < series[0][0] or ts > series[-1][0]:
        return None
    return _interp(series, ts)

def _gamma_temps(measured: dict | None, Ta: dict, ts: datetime) -> dict:
    """De temperaturen waarop de koker-gradiënt geregresseerd wordt: de GEMETEN kamertemps
    waar die er zijn, anders de gesimuleerde luchtknoop.

    Waarom niet gewoon `Ta`: de docstring van `stair_gradient` zegt dat de kamers "de
    proxy-MÉTING van het koker-profiel" zijn, maar in de praktijk werd γ uit de gesimuleerde
    temps berekend, ín de stap-lus. Dat maakte er een positieve terugkoppeling van — een te
    warme voorspelling voor de bovenste kamer gaf een stéilere γ, en de bronterm
    `g·γ·(z − z_mean)` duwde daardoor nóg meer warmte in juist die kamer (tot ~245 W bij de
    klem γ=0.7). De lus verzadigde precies op zonnige middagen, wanneer de verticale spreiding
    het grootst is. Met de meting als regressiebasis is γ weer een waarneming in plaats van
    een versterker."""
    if not measured:
        return Ta
    out = dict(Ta)
    for rid, series in measured.items():
        v = _measured_at(series, ts)
        if v is not None:
            out[rid] = v
    return out

def _stair_gamma(info: dict, temps: dict, open_others: set | None = None) -> float:
    """De verticale gradiënt γ (°C/m) voor één koker: de kleinste-kwadraten-helling van de
    gekoppelde kamertemps t.o.v. hun deurhoogte. `temps` = actuele zone-luchttemps. `open_others`
    = de kamers waarvan de koker-deur NÚ open staat (None = alle deuren meetellen); een dichte deur
    ontkoppelt die kamer, dus die valt uit de regressie."""
    pts = [(zh, temps[o]) for o, zh in info["doors"].items()
           if o in temps and (open_others is None or o in open_others)]
    return stair_gradient(pts)

def room_base_capacitances(room: dict) -> tuple[float, float, float]:
    """Fysische basis (C_air, C_mass, exterieur-UA) uit geometrie, vóór de leer-schalen.
    C_air ≈ luchtmassa×cp (× factor voor meubilair-lucht); C_mass ≈ wandmassa; UA uit
    schiloppervlak.

    De massabasis telt naast gevel + dak óók de woningscheidende muren en de vloer/plafond-
    vlakken mee (`party_wall_m2`, `mass_floor_m2`; afwezig → 0 → exact het oude gedrag).
    Die zijn in een jaren-'20 tussenwoning juist de dominante massa — ze weglaten maakte de
    basis ~3× te klein, en omdat de massaknoop uitsluitend aan `T_out` hing betekende méér
    massa 's zomers méér opwarming, dus de fit verkleinde 'm nóg verder (office leerde
    c_mass 0.46). Het resultaat was een kamer die veel te snel op de zon reageert, terwijl
    de metingen juist een gedempte piek 2,5 uur ná de buitenpiek laten zien. Nominale basis;
    de geleerde `c_mass` zet de uiteindelijke grootte."""
    vol = room.get("volume_m3", 40.0)
    wall = room.get("exterior_wall_m2", 0.4 * vol)
    roof = room.get("roof_m2", 0.0)             # bovenste verdieping: dakvlak (anders 0)
    mass_area = (wall + roof
                 + room.get("party_wall_m2", 0.0)     # woningscheidende muren (baksteen)
                 + room.get("mass_floor_m2", 0.0))    # vloer + plafond (balklaag/beton)
    c_air = vol * 1.2 * CP_AIR * 3.0          # ×3: effectieve binnenlucht + lichte inboedel
    c_mass = mass_area * 90000.0                # J/K per m² schil (baksteen/pleister, ~slow)
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
            # een `roof_m2` heeft; de per-kamer `roof_u` overschrijft de default ROOF_U (een
            # geïsoleerd dak is een fáctor lager dan een kaal dak, geen schaal-nuance).
            "UA_roof": (r.get("roof_m2", 0.0) * r.get("roof_u", ROOF_U)
                        * p.get("ua_roof", 1.0)),
            # Vloer → kruipruimte/bodem (W/K) op de massaknoop. Basis 0 (→ inactief) tenzij de
            # kamer een `ground_m2` heeft; de geleerde `ua_ground` schaalt de grootte.
            "UA_ground": (r.get("ground_m2", 0.0) * r.get("ground_u", GROUND_U)
                          * p.get("ua_ground", 1.0)),
        }
    for jid, j in house.get("junctions", {}).items():
        vol = j.get("volume_m3", 15.0)
        c_air0 = vol * 1.2 * CP_AIR * 3.0
        par[jid] = {"C_a": c_air0, "C_m": c_air0 * 2.0, "H_am": 15.0,
                    "UA_env": 3.0, "UA_mass": 1.0, "solar": 0.0, "f_air": 1.0,
                    "UA_party": 0.0, "Q_int_base": 0.0, "UA_roof": 0.0,
                    # Junctie (gang/overloop) krijgt wél een bodemkoppeling als de
                    # huismodel-geometrie er een geeft — de gang ligt op dezelfde vloer.
                    "UA_ground": j.get("ground_m2", 0.0) * j.get("ground_u", GROUND_U)}
    return par

def simulate(house: dict, params: dict, timeline: list[dict],
             seed: dict, ctx: RunContext, *,
             calib_only_rooms: set | None = None,
             snapshot_t: datetime | None = None,
             tm_seed: dict | None = None,
             measured: dict | None = None) -> dict:
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

    `measured` (optioneel): {kamer: [(t, gemeten °C)]} — uitsluitend gebruikt als regressie-
    basis voor de koker-gradiënt γ (zie `_gamma_temps`), NIET om de sim ergens naartoe te
    sturen. Zonder deze reeksen valt γ terug op de gesimuleerde temps, wat het oude (en
    zelfversterkende) gedrag is.

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
    # Interne vloer-/wandgeleiding tussen zones (W/K). Constant over de tijdlijn — hangt
    # alleen van geometrie + de geleerde globale schaal af, niet van de standen of het weer.
    ginter = interzone_conductances(house, params)

    Ta = {z: seed.get(z, timeline[0]["T_out"]) for z in zones}
    # Massaknoop-startwaarde. `tm_seed` (per zone) is de voorkeur: main() geeft het venster-
    # gemiddelde van de gemeten luchttemp door — de trage massaknoop ís fysisch een gedempt
    # gemiddelde van de kamerlucht, dus dat is een op data geankerde start i.p.v. een gok.
    # Fallback (geen tm_seed voor deze zone, bv. een junctie of cold-start): een warme blend
    # richting NEIGHBOR_TEMP, die de sim-only WARMUP_H aanloop hoort uit te wassen — mits de
    # massa-tijdconstante ~uren blijft. Juist die aanname breekt wanneer c_mass/h_am de τ voorbij
    # 24u leren duwen; dán bleef de blend-warm-bias hangen en trok hij (via H_am) de luchtknoop
    # mee omhoog — de reden dat de venster-gemiddelde-seed nu de voorkeur is.
    Tm = {z: (tm_seed[z] if tm_seed is not None and tm_seed.get(z) is not None
              else 0.5 * (Ta[z] + ctx.neighbor_temp))
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
        # Netto-netwerkdebiet aangevuld met de eenzijdige uitwisseling per open buitenraam
        # (zie effective_fresh): zonder doorstroompad rekent het netwerk ~nul waar er in
        # werkelijkheid ~14 ACH loopt.
        fresh = effective_fresh(net["fresh"], house, step["states"], step["weather"],
                                Ta, T_out)
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
            gamma_temps = _gamma_temps(measured, Ta, step["t"])
            for sid, info in strat.items():
                open_others = {o for o in info["doors"]
                               if (sid, o) in door_area or (o, sid) in door_area}
                gamma = _stair_gamma(info, gamma_temps, open_others)
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
        # Buur-anker op deze tijdstap: de run-basiswaarde met de nachtcap erop (zie
        # neighbor_at) — 's nachts koelen de buren mee i.p.v. door te "stoken".
        nb_now = neighbor_at(ctx.neighbor_temp, step["t"])
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
                          + pa["f_air"] * q_solar + ua_party * nb_now + q_int)
                # Massaknoop (+ dak-sol-air-koppeling naar de effectieve dak-buitentemp,
                # + de vloerkoppeling naar de kruipruimte/bodem — de koude put die 's zomers
                # de enige echte warmte-afvoer is; zie GROUND_U).
                ua_roof = pa.get("UA_roof", 0.0)
                ua_ground = pa.get("UA_ground", 0.0)
                A[im][im] += (pa["C_m"] / h + pa["H_am"] + pa["UA_mass"]
                              + ua_roof + ua_ground)
                A[im][ia] += -pa["H_am"]
                b[im] += (pa["C_m"] / h * Tm[z] + pa["UA_mass"] * T_out
                          + (1.0 - pa["f_air"]) * q_solar + ua_roof * t_solair[z]
                          + ua_ground * ctx.ground_temp)
            # Deur-koppeling (advectief, impliciet) + de interne vloer-/wandgeleiding. Beide
            # koppelen LUCHT↔LUCHT en gaan dus in hetzelfde blok; ze blijven wél gescheiden
            # dicts, want de stratificatie-term hierboven mag alléén de deur-advectie zien.
            for (za, zb), g in list(gdoor.items()) + list(ginter.items()):
                if za not in zi or zb not in zi:
                    continue
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

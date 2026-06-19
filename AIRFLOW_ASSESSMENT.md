# Ventilatie-tweeling (Project 8) — assessment & herijking

_Datum: 2026-06-19. Databron: de gecommitte artefacten `docs/airflow_learned.json`
(331-punts leercurve + params + checkpoint) en `docs/airflow_data.json`, gekruist met de
kalibratiecode in `airflow_model.py`. Reproduceerbaar met `python tools/airflow_diagnostics.py`._

De ventilatie-tweeling is een zelfijkende grey-box-twin: een luchtstroomnetwerk + 2-knoops
RC-thermisch model dat per kamer de temperatuur voorspelt, de fout tegen de echte tado-temps
toont, en online ~40 parameters bijleert. Deze notitie beantwoordt: **hoe goed werkt hij, wanneer
niet, en wat is eraan gedaan** — alles datagedreven.

---

## 1. Hoe goed werkt hij

**Op milde dagen goed, en hij verslaat persistentie het hele venster door.**

| venster (lokaal) | dag-max | zon-gem | RMSE | skill |
|---|---|---|---|---|
| 16 jun, midden van de dag | 24.5 °C | ~225 | **0.49 °C** | — |
| 19 jun 01:45 (checkpoint, koele nacht) | laag | laag | **0.664 °C** | **0.78** |
| 19 jun 15:00–17:15 (hittegolf) | 34–35 °C | 286–310 | **1.08–1.28 °C** | **0.22–0.36** |

- Milde-dag-RMSE ≈ 0.5 °C is een goed resultaat voor een no-numpy grey-box-twin.
- **Skill blijft positief op de hete dag** (~0.22–0.36): hij wint van "morgen = nu" óók wanneer hij
  worstelt, want de kamers bewegen hard en persistentie doet het dan slechter.
- De **checkpoint + auto-fallback** werkt zoals bedoeld — vastgelegd op skill 0.78 / RMSE 0.664 om
  01:45, en een fallback om 16:45 toen de skill wegzakte.
- De **RMSE-backfill** is actief (honderden `recomputed`-punten): de leercurve wordt geheeld tegen
  de gecorrigeerde openingen-log.

## 2. Wanneer werkt hij niet

**Op de hete, zonnige dagen waarvoor de twin bestaat ontwikkelt hij een systematische warm-bias en
satureren zijn parameters.** De fout groeit monotoon met de hitte:

| dag-max (°C) | n | RMSE (gem) | zon-gem (W/m²) |
|---|---|---|---|
| ≤25 | 53 | 0.89 | 225 |
| 25–28 | 10 | 1.02 | 230 |
| 28–31 | 72 | 1.16 | 236 |
| 31–34 | 9 | 1.08 | 278 |
| >34 | 10 | 1.14 | 295 |

**Pearson-correlatie RMSE ↔ dag-max temp: r = +0.69** (n=154). De twin voorspelt elke kamer **te
warm**: huidige residu-bias (voorspeld − werkelijk, gemiddeld over het venster) living +1.02,
ted +1.56, hotties +0.68, office +0.62 °C.

**De saturatie-tell.** Tegelijk is bijna elk *warmte-in*-kanaal naar zijn vloer gerooid — de
optimizer duwt élk warmte-kanaal naar minimum en komt nóg niet koel genoeg:

| param | kamer(s) | waarde | vloer |
|---|---|---|---|
| `cp_shelter` | globaal | 0.10 | 0.1 (wind-ventilatie ≈ uit) |
| `solar_gain` | living, ted | 0.25 | 0.25 (beschermd; wil lager) |
| `ua_env` | hotties | 0.20 | 0.2 |
| `q_int` | ted, hotties | 0.00 | 0.0 |
| `f_air` | hotties, office | 0.10 | 0.1 |
| `ua_roof` | office | 0.00 | 0.0 |

## 3. Waarom — de bias-per-uur wijst de oorzaak aan

De diagnostiek ontleedt de gemiddelde bias per **uur-van-de-dag** (alle kamers samen). Dat is de
beslissende meting:

| uur | bias | uur | bias |
|---|---|---|---|
| 04–06 | +0.42…0.48 | 13 | +1.43 |
| 08–10 | +0.82…0.91 | 14 | +1.55 |
| 11 | +1.07 | **15** | **+1.78** |
| 12 | +1.25 | **16** | **+1.95** |

- **Piek om 15–16u, dal vóór zonsopkomst** → de fout is **zon-/warmte-gedreven**, bovenop een
  ~+0.5 °C nacht-basisoffset. Dit **sluit niet-gemelde nachtventilatie uit** als hoofdoorzaak (geen
  vroege-ochtend-piek).
- **Hoofdmechanismen** (in bewijs-volgorde):
  1. **De beschermende ridge vecht tegen de hete-dag-data.** `REG_WEIGHT=3.0`, `solar_gain` verdubbeld
     naar `6.0`, vloer `0.25`. Ingebouwd om "zon-uit"-collapse op *bewolkte* vensters te stoppen.
     Maar op een zonnig venster *is* de zon identificeerbaar en wil de data de warmte-in-params
     **onder** hun prior — de ridge trekt ze weer naar 1.0 → een te-warm compromis, juist op de dag
     waarvoor de twin bedoeld is. De bescherming werkt averechts.
  2. **Het 48u-kalibratievenster overspant een regime-wissel** (mild 16 jun → hittegolf 19 jun). Een
     ongewogen kleinste-kwadraten-fit middelt twee regimes en het compromis overschat het hete eind.
  3. **Ingestort luchtstroom-koppel** (`cp_shelter` op de vloer): wind draagt ~niets bij, dus
     gemelde-open ramen koelen in het model te weinig — secundair t.o.v. de zon-bias.
  4. **Airco** (office): in déze snapshot leest office juist warm (26.7 °C) met **14 samples**
     correct uit de fit gelaten — de exclusie werkt hier. Wél bleef er een **latent gat**: de
     exclusie was retroactief-only, dus een "airco staat nu hier"-melding zónder terugdatering
     vervuilde de recente uren.

**Oordeel:** een werkend *relatief* instrument (positieve skill, ~0.5 °C op milde dagen), maar op
hete zonnige dagen vertekenen de beschermingsmechanismen + het regime-blinde venster de geleerde
fysica tot een hardnekkige +1…+2 °C warm-bias.

---

## 4. Wat is eraan gedaan

### Diagnostiek (read-only, nieuw)
`tools/airflow_diagnostics.py` — leest de gecommitte artefacten en print de regime-curve, de
saturatie-tabel, de per-kamer residu-ontleding (incl. bias-per-uur) en de airco-check. Geen
model-mutatie; de bron-of-waarheid voor secties 1–3 hierboven.

### Veilige fixes
- **AC-guard-venster** (`filter_ac_samples`, `AC_GUARD_H=6.0`): een "airco nu in kamer X"-melding
  laat nu óók de samples van X uit de laatste 6 uur vallen, ongeacht log-timing — sluit het
  retroactief-only-gat. Uitgetrokken in een testbare pure functie.
- **Saturatie-logging**: de runner print elke run welke params op hun grens zitten en schrijft een
  additief `railed`-veld in `airflow_learned.json` (`railed_params()`), zodat rail-events zichtbaar
  zijn i.p.v. handmatig op te sporen.

### Fysica/kalibratie (regime-bewust)
- **Regime-bewuste solar_gain-ridge** (`reg_weight(name, solar_mean)`): het extra-sterke anker
  (6.0) ramp terug naar `REG_WEIGHT` (3.0) zodra het venster-zon-gemiddelde van
  `SOLAR_RIDGE_LOW_WM2` (150) → `SOLAR_RIDGE_HIGH_WM2` (300) loopt. Anti-collapse-bescherming blijft
  op bewolkte vensters; op zonnige vensters wordt de data vertrouwd. Standaardgedrag ongewijzigd als
  `solar_mean` onbekend is.
- **Recency-gewogen residuen** (`_recency_weights`, `RECENCY_HALFLIFE_H=18.0`): een exponentieel
  tijdsgewicht (`2^(−Δt/half-life)`) laat het huidige regime de fit-richting domineren bij een
  regime-wissel — zónder het venster te verkorten (trage termen blijven identificeerbaar). Raakt
  alléén de fit-richting, niet de gerapporteerde RMSE/skill (blijft vergelijkbaar over de leercurve).

Alle wijzigingen zijn additief en gedekt door nieuwe tests in `tests/test_airflow_model.py`
(regime-ridge-ramp, recency-decay, recency-volgt-recent-regime, AC-guard-venster, railed_params).
`ruff check .` en `python -m pytest` blijven groen (160 tests).

## 5. Open punten (bewust niet blind aangepast)

- **`cp_shelter` op de vloer / ingestort wind-koppel** — de bias-per-uur zegt dat de zon dominant is,
  niet de ventilatie, dus dit is als vervolgonderzoek gemarkeerd (stack/buoyancy-term-magnitude +
  `cd·vent_eff`-vs-`cp_shelter`-identificeerbaarheid op windstille hete dagen). Niet blind herijkt.
- **Reportage-nudge voor nachtventilatie** — de data ontkracht dit als hoofdoorzaak (geen
  ochtend-piek), dus gedeprioriteerd.
- **Validatie van de fysica-wijzigingen end-to-end** vereist meerdere live hete-dag-runs; de
  unit-tests bewijzen het mechanisme, de skill/`railed`-velden maken het effect over de komende runs
  zichtbaar.

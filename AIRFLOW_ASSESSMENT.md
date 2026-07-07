# Ventilatie-tweeling (Project 8) — assessment juli 2026

_Datum: 2026-07-07. Tweede assessment — opvolger van de assessment van 19 juni (commit
`62d84c59`; de kernbevindingen daarvan staan hieronder in §3 met hun status). Databron: de
gecommitte artefacten `docs/airflow_learned.json` (240-punts leercurve + params + checkpoint,
stand 2026-07-07 14:00 lokaal) en `docs/airflow_data.json`, gekruist met de kalibratiecode in
`airflow_model.py`. Reproduceerbaar met `python tools/airflow_diagnostics.py`._

**Oordeel in één alinea.** De twin is sinds juni fundamenteel gezonder geworden. De
juni-hoofdvondst — een systematische +1…+2 °C warm-bias die monotoon met de hitte groeide — is
op het waargenomen weer **opgelost en zelfs omgekeerd**: warme dagen fitten nu het best
(RMSE ≈ 0.41 °C), de leercurve convergeerde in drie dagen van 1.0 naar ~0.41 °C en de
stabiliteitsmechanieken (checkpoint, backfill, pauze, verwarmings-/AC-filter) doen aantoonbaar
hun werk. Wat overblijft is structureel en goed gelokaliseerd: de woonkamer-zonpad (enige kamer
met een echte residu-bias, `solar_gain` op zijn vloer), een dag-nacht-amplitudefout (±0.4 °C
te koud bij dageraad, te warm rond de middag), en het al in juni gemarkeerde
luchtstroom-magnitude-probleem (`cp_shelter` op de vloer, ACH ~50 in de bovenkamers). De échte
hittegolf-validatie moet nog komen — het venster zag maximaal ~28–29 °C.

---

## 1. Wat er sinds 19 juni is gebeurd

Een dichte wijzigingsgolf (16 verschillende `model_version`-stempels in de huidige 3-daagse
leercurve alleen al):

- **Fysica**: trappenhuis-stratificatie (zelfijkende verticale gradiënt uit de kamer-proxies),
  Brown–Solvason-deur-tegenstroom (koker gepind aan zijn open-deur-kamers), zonnekroon,
  pin-error-diagnostiek.
- **Kalibratie-hygiëne**: tado-verwarmingsstatus ingelezen en gestookte samples automatisch
  uit de fit; huis-brede pauzeknop; badkamer (`Shower`-sensor) gepromoveerd tot gekalibreerde
  kamer; checkpoint-fallback-lus gerepareerd (werkelijkheids-check + her-zeteling); leercurve-
  churn gerepareerd (backfill alleen nog bij échte log-wijziging, vingerafdruk-poort).
- **Robuustheid**: `suggest()` overleeft een None-kamertemp (6×-crash op 3 jul), Ted-nachtkoeling
  in de nachtvoorspelling gefixt.
- **Consumenten**: drie nieuwe pipelines lezen de twin read-only (zonwering-adviseur,
  Teds nachtvoorspelling, weekjournaal) — de twin is nu infrastructuur, niet alleen dashboard.

## 2. Hoe goed werkt hij nu

**De leercurve convergeert snel en het plateau ligt ruim onder de juni-niveaus.**

| dag | punten | RMSE (gem) | skill (gem) |
|---|---|---|---|
| 4 jul | 43 | 1.007 | 0.21 |
| 5 jul | 74 | 0.630 | 0.38 |
| 6 jul | 72 | 0.430 | 0.50 |
| 7 jul | 51 | **0.413** | **0.62** |

- Actueel: RMSE **0.431 °C**, skill **0.55** (persistentie-RMSE 0.96 → hij is ruim 2× beter dan
  "straks = nu"). Checkpoint-optimum: RMSE **0.398** / skill **0.684** (7 jul 03:15,
  `degraded_runs` 0). Ter vergelijking: juni scoorde 0.49–0.66 op milde dagen en 1.08–1.28 in de
  hittegolf.
- **Per kamer** (venster ≈ laatste 48u; bias = voorspeld − werkelijk):

| kamer | bias (gem) | RMSE | nu-fout | opmerking |
|---|---|---|---|---|
| living | **+0.37** | 0.47 | +0.13 | enige kamer met echte bias — zie §4.1 |
| ted | −0.01 | 0.26 | +0.49 | |
| hotties | +0.05 | 0.50 | +0.58 | |
| office | 0.00 | 0.58 | +0.62 | |
| bath | +0.02 | **0.24** | +0.40 | nieuw gekalibreerd; direct de beste fit |
| stair | — | — | — | sensorloos; gradiënt 0.21 °C/m, pin-fouten 0.08–0.51 |

- **De stabiliteitsmachinerie werkt.** In 240 punten: 5× `fell_back`, alle vóór 5 jul 21:45 —
  daarna geen enkele (de fallback-lus-fix houdt); `recomputed` 0 (de vingerafdruk-poort houdt de
  backfill stil zonder log-correctie); 21 punten gepauzeerd (6 jul 10:45–15:45) netjes buiten het
  leren gehouden; geen AC- of verwarmings-contaminatie in de huidige snapshot.
- **Ops**: pipeline live op kwartiercadans, 100 tests op de module, echte huisgeometrie.

## 3. Status van de juni-bevindingen

**3.1 Hete-dag-warm-bias — OPGELOST op het waargenomen weer, en omgekeerd.** Juni mat
r = **+0.69** tussen RMSE en dag-max (fout groeit met hitte). Nu, ná convergentie
(6–7 jul, niet-held, n=102):

| dag-max (°C) | n | RMSE (gem) | skill (gem) |
|---|---|---|---|
| ≤25 | 25 | 0.466 | 0.74 |
| 25–28 | 73 | **0.411** | 0.58 |
| >28 | 4 | **0.406** | 0.49 |

Correlatie RMSE ↔ zon-gemiddelde post-convergentie: r = **−0.52** — warme, zonnige uren fitten
nu het bést. (Let op: over de vólle 3-daagse curve staat r = +0.76, maar dat is een
leerfase-confound — de koele dagen vallen samen met de nog-niet-geconvergeerde start. De
juni-meting had die confound niet.) De regime-bewuste ridge, recency-weging, leerbare `f_air` en
WU-zon-herschaling hebben gedaan waarvoor ze zijn gebouwd. **Kanttekening:** het venster zag
maximaal ~28–29 °C (n=4 boven 28) — de échte hittegolf-stresstest staat nog uit.

**3.2 De saturatie-golf — grotendeels opgelost.** Juni telde rails op ~elk warmte-in-kanaal
in ~elke kamer (`q_int` op 0.0 in ted+hotties, `f_air` op de vloer in hotties+office,
`ua_env` op de vloer in hotties, `solar_gain` op de vloer in living+ted). Nu resteren er **4**
(zie §4) en zijn ted/hotties/office van hun zon- en q_int-vloeren losgekomen.

**3.3 `cp_shelter`/wind-koppel — NOG OPEN.** In juni als vervolgonderzoek gemarkeerd; staat nog
steeds op zijn vloer (0.10) en het probleem is nu beter zichtbaar (§4.3).

**3.4 Nachtventilatie-nudge — blijft gedeprioriteerd.** De bias-per-uur toont geen
vroege-ochtend-píek maar juist een dip (§4.2) — geen aanwijzing voor niet-gemelde nachtventilatie.

## 4. Wat nu niet goed werkt

**4.1 Woonkamer-zonpad (structureel kandidaat #1).** `living.solar_gain` staat op zijn
beschermde vloer **0.25** (prior 1.0) én living draagt de enige echte residu-bias (+0.37 °C,
slechtste kamer) — d.w.z. zelfs op mínimale zonwinst voorspelt het model de woonkamer te warm,
terwijl er in de middag-snapshot 707 W zon op de kamer wordt geboekt. De kalibratie kan dit niet
meer oplossen; de oorzaak zit vrijwel zeker in de geometrie/schaduw-inputs — denk aan de
horizon-mask van de tuindeuren (ZO), terras-overstek, boom- of buurpand-schaduw, of een te hoge
glas-transmissie. Dit is een `house_model.json`-onderzoek, geen parameter-tweak.

**4.2 Dag-nacht-amplitudefout.** De bias-per-uur (alle kamers, laatste ~48u) is geen vlakke
offset meer maar een slinger:

| uur | bias | uur | bias |
|---|---|---|---|
| 04 | −0.26 | 11 | **+0.38** |
| **05** | **−0.38** | 13 | +0.34 |
| 06 | −0.31 | 16–17 | +0.27…0.31 |
| 08 | +0.02 | 20–22 | +0.08…0.18 |

Het model koelt 's nachts te ver door en warmt overdag te ver op — een amplitude- i.p.v.
offset-fout. Verdachten: te sterke dak-/nachthemel-koeling (`ROOF_SKY_COOLING`), een te lichte
massaknoop (`c_mass`/`h_am`-identificeerbaarheid), en koppeling met §4.1 (te veel middag-zon
dwingt elders compensatie af). Netto middelt het weg (vandaar bias ≈ 0 per kamer behalve living),
maar het kost RMSE aan beide uiteinden van de dag.

**4.3 Luchtstroom-magnitude blijft onfysisch.** Met alles open en 6.2 m/s wind rekent het
netwerk **ACH ≈ 54 (hotties) en 50 (office)** — de lucht elke ~70 s ververst, niet plausibel.
`cp_shelter` staat op zijn vloer 0.10 en `vent_eff` op 0.26 (prior 1.0): de thermische fit
vecht tegen een overschat wind-debiet en heeft alle knoppen die hij daarvoor heeft al op minimum
gezet. Gevolg: de *thermische* voorspelling is er ondanks dit goed, maar de getoonde
volumetrische flows/ACH (dashboard-pijlen, speeltuin) zijn bij wind niet te vertrouwen — de twin
is een sterke relatieve ranker, geen liters-per-seconde-orakel, en dat moet óf gefixt óf
eerlijker gelabeld. Mogelijke richtingen: een effectieve-opening-factor (een "open" raam is
zelden het volle kozijnoppervlak), een ridge die `cp_shelter` aan zijn prior bindt met een
flow-plausibiliteits-term, of kalibratie op een windstil-vs-windig contrastvenster.

**4.4 Kleinere defecten.**
- **Leercurve-venster te kort voor het weekjournaal**: `RMSE_HISTORY_KEEP=240` ≈ 2.5 dag, maar
  het weekjournaal wil `RMSE_LOOKBACK_D=6.5` dagen terugkijken — de "week"-trend valt dus
  áltijd terug op het oudste punt (~2.5 d) en heet ten onrechte een weektrend. Fix: cap verhogen
  of oudere punten uitdunnen naar uurcadans.
- **`cd`-fossiel in `airflow_learned.json`**: de params dragen nog `cd: 0.3` uit de tijd dat hij
  leerbaar was; de code gebruikt de vaste `CD=0.62` en niets leest de opgeslagen waarde. Inert,
  maar verwarrend in elke param-dump. Bij de volgende persist strippen.
- **`office.ua_roof` ≈ 0.08** (prior 1.0): de dakterm die juist vóór de office is gebouwd, staat
  daar bijna uit. Óf het dak isoleert echt goed, óf de term degenereert met de raam-zon —
  in de gaten houden zodra er een felle dag-nacht-cyclus in het venster zit.
- **`bath.ua_env` op de vloer 0.205**: de nieuwste kamer heeft zijn envelope al aan de grens;
  met RMSE 0.24 geen acuut probleem, maar hetzelfde saturatie-signaal als altijd.
- **Terugkerende nachtgaten in de leercurve**: gaten tot ~3u rond 03:00–06:00 (5 jul 196 min,
  6 jul 166 min) — GitHub-cron is 's nachts onbetrouwbaar; de kicks vangen het later op maar de
  curve heeft gaten.
- **AUDIT.md-restpunten voor P8** (1 jul): R8 (eind-RMSE niet NaN-bewaakt; params niet in-solver
  geklemd, rails alleen gerapporteerd), M2 (~200-regel `main()` niet end-to-end getest), S2
  (workflow-least-privilege), M7/D4 (naamsbotsing + WU-wrapper-duplicatie met `window_advisor`).

## 5. Aanbevolen vervolg

**Quick wins (klein, laag risico):**
1. `RMSE_HISTORY_KEEP` verlengen of uitdunnen zodat het weekjournaal echt 6.5 dag terug kan.
2. `cd`-fossiel bij persist strippen.
3. Diagnostiek-regimetabel een post-convergentie-filter geven (de leerfase-confound van §3.1
   zit anders in elke toekomstige meting).
4. AUDIT R8: eind-RMSE NaN-guard + in-solver bound-clamp.

**Structureel (onderzoek eerst, dan pas code):**
1. **Woonkamer-zonpad** (§4.1): horizon-masks/overstek van de tuindeuren en het living-skylight
   in `house_model.json` naast de werkelijkheid leggen; pas daarna eventueel glas-params.
2. **Dageraad-onderkoeling** (§4.2): `ROOF_SKY_COOLING`-magnitude en de nacht-identificeerbaarheid
   van `c_mass`/`h_am` na een paar heldere nachten bekijken.
3. **Flow-magnitude** (§4.3): effectieve-opening-factor of flow-plausibiliteits-ridge; minimaal
   de dashboard-flows als *relatief* labelen zolang dit open staat.

**Geduld (data, geen code):**
- De échte hittegolf-validatie (venster zag ≤ ~29 °C; n=4 boven 28).
- Stookseizoen: het verwarmings-filter en de winter-identificeerbaarheid zijn pas dan te bewijzen.
- Elke week extra leercurve maakt de checkpoint/skill-vergelijking over weerregimes heen sterker.

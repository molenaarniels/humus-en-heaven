# Ventilatie-tweeling (Project 8) — assessment juli 2026 (derde)

_Datum: 2026-07-10. Derde assessment — opvolger van 7 juli (`62d84c59`-lijn; kernbevindingen
hieronder in §3 met status) en 19 juni. Databron: `docs/airflow_learned.json` (248-punts
leercurve, stand 10 jul 13:15 lokaal), `docs/airflow_data.json`, `tools/airflow_diagnostics.py`,
gekruist met de code. Anders dan de vorige twee assessments **levert deze ook meteen fixes mee**
(zelfde branch): de wind-referentiehoogte-fix + effectief-openingsoppervlak (fysica-rev 2), de
checkpoint-her-vloering, en observability/dashboard-eerlijkheid — zie §5._

**Oordeel in één alinea.** De 7-juli-conclusie "de twin is fundamenteel gezonder" hield één dag
stand: de warm-weer-fixes van juni bléven werken (warme zonnige uren fitten nu het bést), maar
zodra het venster dynamischer werd dreef de fout van het optimum ~0.38–0.43 terug naar
0.60–0.79 °C en kwam er een nieuw mechanisch defect bloot: een **checkpoint-jojo** (skill-lat
0.813 geoogst op één gunstig venster; 7 opeenvolgende geaccepteerde fallbacks trokken het leren
elke ~2u terug). Belangrijker: het al in juni gemarkeerde "luchtstroom-magnitude-probleem" bleek
**geen cosmetica maar de #1 thermische fout** — een modelleerfout in de winddruk (dynamische druk
op per-opening-hoogte i.p.v. één referentiedruk per gevel, tegen de CONTAM-conventie in) dreef
een kunstmatige zelfde-gevel-lus van ~27 ACH die warme buitenlucht in hotties blies (+3.0 °C
nu-fout) en cp_shelter/vent_eff/solar_gain collectief in de vloer-rails drukte. Beide defecten
zijn in deze branch gefixt; de her-convergentie (uren) en de zonpad-herkalibratie (daarna) zijn
het vervolg.

---

## 1. Wat er sinds 7 juli is gebeurd

- **De (milde) hittegolf-validatie kwam er** — het venster zag tot ~29 °C. De juni-fixes
  hielden: post-convergentie fitten warme/zonnige uren het bést (laatste 48u:
  r(RMSE↔dag-max) = **−0.62**, r(RMSE↔zon) = −0.32). De >30 °C-stresstest staat nog uit.
- **De leercurve dreef terug omhoog**: optimum 0.38–0.43 (6–7 jul) → 0.60–0.79 (8–10 jul),
  actueel **0.741 °C** (skill 0.47, naive 1.41). Grotendeels absolute-foutgroei in dynamischer
  weer (skill hield ~0.5), maar wél boven het checkpoint (0.673) — zie de jojo in §4.1.
- **De modelcode zelf was bevroren** (1 commit in de laatste 200): de drift is leerner + weer
  op vaste fysica, geen code-churn.
- De stabiliteitsmachinerie deed intussen zijn werk: de pauze van 6 jul netjes buiten het leren,
  vingerafdruk-poort stil (`recomputed` 0), geen AC-/verwarmings-contaminatie.

## 2. Hoe goed werkt hij nu

| moment | RMSE | skill | context |
|---|---|---|---|
| 4 jul 13:45 | 0.975 | 0.03 | start leercurve |
| 6 jul 15:45 | **0.381** | — | all-time minimum (makkelijk venster) |
| 7 jul (gem) | 0.413 | 0.62 | het 7-juli-assessment-plateau |
| 9 jul 00:00 | 0.788 | 0.58 | recente piek |
| 10 jul 13:15 | 0.741 | 0.47 | actueel; checkpoint 0.673/0.813 |

**Per kamer** (laatste ~48u; bias = voorspeld − werkelijk):

| kamer | nu-fout | bias (gem) | RMSE | opmerking |
|---|---|---|---|---|
| bath | +0.13 | −0.16 | **0.28** | beste fit |
| ted | +0.63 | +0.31 | 0.52 | |
| living | −0.79 | −0.16 | 0.54 | juli-7-bias (+0.37) weggetraind, nu iets te koud |
| office | +1.47 | −0.26 | 0.94 | dag-nacht-slinger: nacht −1.2, namiddag +0.8 |
| hotties | **+2.98** | **+0.96** | **1.11** | structureel te warm, hele dag — zie §4.2 |
| stair | — | — | — | sensorloos; gradiënt 0.29 °C/m, top 28.1/onder 23.6 |

**Uur-van-de-dag-bias (alle kamers):** dageraad −0.17 (05u), namiddag +0.43…+0.47 (16–18u) —
de dag-nacht-amplitudefout van 7 juli (§4.2 dáár) staat er nog, iets verschoven naar de avond.

## 3. Status van de 7-juli-bevindingen

**3.1 Woonkamer-zonpad — verschoven, niet opgelost.** `living.solar_gain` staat nog op de
vloer (0.25) maar de +0.37-bias is weggetraind (nu −0.16 gem). Het onderliggende
zon-magnitude-probleem is echter **geëscaleerd**: zie §4.3.

**3.2 Dag-nacht-amplitudefout — blijft.** Zelfde slinger (nacht te koud, middag/namiddag te
warm), nu het duidelijkst in office (−1.2 om 01u, +0.8 om 16u). Verdachten onveranderd
(`ROOF_SKY_COOLING`, massaknoop-identificeerbaarheid, koppeling met de zon-overdrive).

**3.3 Luchtstroom-magnitude — WORTELOORZAAK GEVONDEN + GEFIXT (deze branch).** De 7-juli-tekst
("de thermische voorspelling is er ondanks dit goed") bleek achterhaald: met hotties- én
office-raam open rekende het netwerk een 0.24 m³/s-lus (27 ACH) ín hotties, dóór de koker, úít
office — **beide ramen op dezelfde NW-gevel bij 3 m/s wind**. Oorzaak gekwantificeerd: het
power-law-windprofiel werd op de hoogte van élke opening afzonderlijk geëvalueerd, zodat twee
zelfde-gevel-openingen een ΔPe ∝ wind² kregen puur uit hun hoogteverschil — surface-averaged
Cp-tabellen zijn juist genormaliseerd op één referentiedruk per gevel (CONTAM/AIRNET). De
kalibratie vocht alleen maar tegen dit artefact: `cp_shelter` op de vloer (0.10), `vent_eff`
0.43, en de tweeling blies intussen 24°-buitenlucht in hotties (gemeten 21.7°, voorspeld 24.7°).
Fix: `WIND_REF_Z` 8.7 m (dynamische druk op nokhoogte; hoogteverschil blijft in de stack-term)
+ `EFF_OPEN_AREA` per openings-type (een wijd open draairaam ≠ het volle kozijngat; casement
×0.5). Gemeten op het echte huis: lus 0.24 → 0.02 m³/s, hotties-ACH 27 → ~3, windongevoelig
(zuivere stack-rest). Migratie via `PHYSICS_REV` 2: alleen cp_shelter/vent_eff terug naar prior,
checkpoint vervalt, anomalie-poort één run overgeslagen.

**3.4 Kleinere defecten van toen — allemaal gedaan** (verified): leercurve-venster
(`RMSE_HISTORY_KEEP` 1000/10d + uurcadans-uitdunning), `cd`-fossiel-strip, post-convergentie-
filter in de diagnostiek, AUDIT R8 (NaN-guard + in-solver clamp — stond nog ten onrechte op
"Open", nu geflipt).

## 4. Wat nu niet goed werkt (nieuwe bevindingen)

**4.1 Checkpoint-jojo — GEFIXT (deze branch).** De skill-lat is een high-water-mark: 0.813
geoogst op één informatief venster (9 jul 16:29). Normale vensters halen dat structureel niet
(skill is venster-afhankelijk), dus elke ~2u: 8 "degraded" runs → fallback → werkelijkheids-
check accepteert (de checkpoint-params pásten ook echt beter) → params teruggezet → **maar de
lat bleef op 0.813** → teller loopt meteen weer op. Zeven geaccepteerde fallbacks op 9–10 jul.
De reality-check-fix van begin juli ving alleen de vérworpen tak af. Fix:
`accept_fallback_checkpoint` her-vloert de lat na een geaccepteerde fallback op wat de
teruggezette params op het huidige venster halen (`refloored`-stempel).

**4.2 hotties is de structureel slechtste kamer** (+0.96 gem, +3.0 nu): grotendeels de valse
instroom-lus van §3.3 (fix moet dit dempen — hét validatiepunt van de komende dagen), maar
hotties was ook vóór de lus de meest rail-verzadigde kamer (solar_gain, h_am, q_int én f_air
op de vloer). Restverdenking: de geometrie-placeholders (volume 32 m³ en muur 12 m² identiek
aan ted/office — zelf-gelabeld "SCHATTINGEN") en de buitenmuur-voeler (`sensor_outdoor_frac`
0.15, identiek aan office).

**4.3 solar_gain op de vloer in ALLE gekalibreerde kamers** (7 juli: alleen living) +
`cp_shelter` gevloerd: de zon-/warmte-drive is globaal te heet en de optimizer klemt elk
warmte-kanaal. Deels gekoppeld aan §3.3 (valse ventilatie-koeling dwingt elders compensatie);
**daarom eerst de flow-fix laten her-convergeren en pás daarna het zonpad herijken** — beide
tegelijk aanpakken vernietigt de attributie. Blijft de vloer-rail ook ná her-convergentie, dan
is het glas-/schaduwpad aan de beurt: `GLASS_TRANSMITTANCE` 0.7 × `GLASS_AREA_FRACTION` 0.6,
horizon-masks (de boom bij Ted is een erkende grove benadering), overstek/terras-schaduw —
een `house_model.json`-onderzoek met meetwerk, geen parameter-tweak.

**4.4 Structurele beperkingen (geen regressies, wel plafonds):**
- **Grond-waarheid stopt op 48u** (`window_data` buffert 192 kwartier-samples): begrenst
  `CALIB_WINDOW_H`, het backfill-herstel-horizon én elke cross-regime-validatie. Richting: P8
  archiveert zijn eigen rollende actuals (additief artefact, ~14 d) — geen P6-wijziging nodig.
- **`suggest()` is puur ogenblikkelijk/steady-state**: geen zon, geen massaknoop, geen
  tijdintegratie. Upgrade-pad: kandidaten voor-sorteren met de huidige scorer en de top-K door
  een korte-horizon `simulate()` (1–2u) halen — de gekalibreerde twin bestaat al.
- **Niemand hoort de anomalie-poort — OPGELOST (deze branch, besluit gebruiker 10 jul).**
  Bij log↔werkelijkheid-mismatch pauzeerde het leren en meldde alléén het dashboard dat, dus
  de log bleef dagen fout staan tot iemand toevallig keek. Er gaat nu een Telegram-nudge naar
  de privé-chat ("klopt de raamstand nog?", fout vs norm + dashboardlink), één per
  episode-start, hooguit elke 6u herhaald (`ANOMALY_NUDGE_COOLDOWN_H`, stempel
  `anomaly_nudge_at` in `airflow_learned.json`). De handmatige pauze nudget bewust niet
  (zelf gekozen). Dit was een bewuste-ontwerp-conflict met CLAUDE.md's "geen Telegram voor
  P8"; CLAUDE.md is bijgewerkt: P8 stuurt alléén deze operationele nudge, geen
  advies-berichten.
- Kleiner: dichtheids-inconsistentie (vaste 1.2 kg/m³ in de advectie vs `air_density()` in het
  netwerk), nachthemel-koeling binair én wolken-blind, `ua_mass` permanent op zijn prior,
  consumer-koppeling via module-globals (`am._LAT/_LON/_NEIGHBOR_TEMP`) en `_private`-helpers
  (P9/P10), terugkerende nachtgaten (03–06u) in de leercurve precies waar de dageraadsfout zit.

## 5. In deze branch gefixt (validatie-instructies)

1. **Checkpoint-her-vloering** (`accept_fallback_checkpoint`) — verwacht: geen
   `fell_back`-clusters meer; `degraded_runs` blijft laag; lat volgt haalbare niveaus.
2. **Fysica-rev 2: `WIND_REF_Z` + `EFF_OPEN_AREA`** — verwacht na her-convergentie (uren, door
   de globalen-reset + checkpoint-vervaltijd): hotties-fout zakt substantieel; ACH-waarden
   plausibel (~0–6 i.p.v. 27–54); `cp_shelter` weg van zijn vloer; op termijn ook
   `solar_gain`-rails losser. **Valideer op de leercurve + §2-tabel over 2–3 dagen.**
3. **Observability**: `learned.solver_failures` (stille substap-freeze telt nu mee),
   `calib_samples`/`calib_span_h`/`calib_rooms` + dashboard-waarschuwing onder 24u effectieve
   dekking (gaat 's winters werken als het stook-filter samples wegneemt).
4. **Dashboard-eerlijkheid**: "ware lucht"-temp op buitenmuur-voeler-kamers; debieten/ACH
   expliciet gelabeld als op-temperatuur-geijkte modelschatting.
5. **Anomalie-nudge** (besluit gebruiker): Telegram naar de privé-chat zodra de anomalie-poort
   het leren pauzeert — verwacht: een niet-gemelde raamwijziging wordt binnen een kwartier
   gemeld i.p.v. per toeval ontdekt; de leercurve heelt daarna via de bestaande backfill.

## 6. Aanbevolen vervolg

**Nu (data afwachten, geen code):**
1. 2–3 dagen her-convergentie monitoren (§5.2) — pas daarna oordelen over het zonpad.
2. De échte >30 °C-stresstest en (later) het stookseizoen blijven de openstaande validaties.

**Daarna (onderzoek → code):**
1. **Zonpad-herijking** (§4.3) — alleen als solar_gain ná her-convergentie gevloerd blijft:
   eerst meetwerk aan schaduw/horizon/glas in `house_model.json`, dan pas constants.
2. **Eigen actuals-archief** (§4.4) — ontgrendelt langere vensters, backfill voorbij 48u en
   week-schaal-validatie.
3. **`suggest()` via korte-horizon-simulatie** (§4.4).
4. **Geometrie-huiswerk** (meten, geen code): echte volumes/muuroppervlakken voor
   ted/hotties/office (nu identieke placeholders), en de voeler-fracties.

**Expliciete CLAUDE.md-conflicten (bewuste ontwerpkeuzes die een besluit vergen, geen code
zonder dat besluit):**
- ~~**Anomalie-nudge via Telegram**~~ — **besloten en geïmplementeerd** (10 jul, zie §5.5);
  CLAUDE.md bijgewerkt.
- **AC-/verwarmingsterm in de fysica** — nu bewust opgelost via sample-uitsluiting; een simpele
  geleerde koel-/stook-wattage zou die kamers ín de kalibratie houden, maar CLAUDE.md
  documenteert het weglaten als bewuste keuze.
- **Openingen-log-verval/auto-correctie** — raakt de beschermde Gist-schrijflogica.

**Geduld (bewust niet doen):**
- Trap-subzone-split (geen sensor om tegen te valideren), leerbare `sensor_outdoor_frac`
  (degenereert met ua_env — vergt een fysiek sensor-experiment, geen code).

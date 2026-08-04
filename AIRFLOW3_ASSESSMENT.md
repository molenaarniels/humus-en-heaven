# Ventilatie-tweeling (Project 13) — assessment augustus 2026: de 12-uurs voorspelling

_Datum: 2026-08-04. Aanleiding: een offline werksessie op een export van deze repo leverde een
handoff met vier voorgestelde wijzigingen en een browserkern voor de speeltuin. Dit document is
de meting die ze in **deze** repo, op **deze** data, wél of niet rechtvaardigt — plus de twee
plekken waar het antwoord hier anders uitvalt dan offline._

**Oordeel in één alinea.** De maat die alles beoordeelt bestond niet en is nu de kern van de
zaak: `tools/horizon_backtest.py` scoort de fout op een *vaste horizon vanaf de toestand die je
nú kent*, en dat is iets heel anders dan de twee getallen die we hadden. De ~0,54 °C in
`vent_learned.json` is een **nowcast** over een 48-uurs kalibratievenster dat elk kwartier
opnieuw op verse tado-metingen wordt gezet; de ~1,1 °C in de ML-export is een **5-daagse
vrijloop**. Geen van beide beantwoordt "hoe warm is Teds kamer morgenochtend om 07:00". Op de
nieuwe maat scoort de tweeling **0,70 °C gepoold over 12 uur** (405 rollende oorsprongen), en
verslaat hij persistentie (1,20), gisteren-om-deze-tijd (1,25) en uur-van-de-dag-klimatologie
(1,83) in vier van de vijf kamers. De fout **plateaut** in plaats van te divergeren — h=12 is
0,85 tegen h=1 0,34 — dus een 12-uurs venster is haalbaar; dát is de rechtvaardiging om het
dashboard erop te bouwen.

Twee bevindingen wijken af van de offline handoff, en allebei omdat de handoff met een
momentopname van `vent_learned.json` van ~1 augustus werkte:

* **`living.c_mass` was al grotendeels zelf hersteld.** De handoff zag 0,372 — gerailed op de
  globale vloer, de láágste waarde van alle kamers terwijl living veruit de grootste is. Hier
  staat hij op **0,577**, vlak onder de aanbevolen 0,595. De online fit had de fout in twee
  maanden zelf grotendeels uitgelopen. De grens blijft toch staan, want de fit wíl er nog steeds
  onderdoor (zie §3).
* **De kruipruimte-correctie kan niet los van een parameter-reset beoordeeld worden.** Zie §2 —
  dit is de belangrijkste methodologische les van deze ronde.

---

## 1. De maat: `tools/horizon_backtest.py`

Per uitgiftetijd `t0` (rollende oorsprong, stap 3 uur):

1. aanloop over `[t0 − 24u, t0]` met `snapshot_t=t0`, zodat de trage massaknoop equilibreert;
2. **herankeren op `t0`** — de luchtknoop terug op de gemeten kamertemp (via `air_from_sensor`,
   want de sim-knoop is de wáre lucht en de tado-voeler leest gebiasd), en de massaknoop
   schuift met dezelfde delta mee;
3. voorspelpas over `[t0, t0 + 12u]`, gescoord op elke 15-minutenstap.

Baselines op exact hetzelfde raster: `persist`, `persist24`, `clim`, en — belangrijk —
`freerun`, dezelfde fysica zónder stap 2.

**Aanname: perfecte weersvoorspelling.** Het historische weer gaat erin alsóf het een forecast
was. Elk getal hier isoleert dus de *model*fout van de *weersvoorspellings*fout en is een
optimistische bovengrens op de live prestatie. Dat gat is niet gemeten en kán nu niet gemeten
worden: er is geen logboek van forecast-vs-werkelijk. Een ~30 regels Action die elke run de
48-uurs Open-Meteo-forecast wegschrijft zou het na 4–6 weken sluiten; dat is bewust niet in deze
ronde meegenomen, en het is de grootste openstaande onzekerheid van het hele dashboard.

### De uitgangsstand (revisie 6, geleerde params van 4 aug)

RMSE °C, gepoold over 0–12u, 405 oorsprongen:

| kamer | fysica | vrijloop | persist | persist24 | clim |
|---|---|---|---|---|---|
| living | 0,693 | 0,878 | 1,038 | 0,923 | 1,300 |
| ted | 0,539 | 1,170 | 0,650 | 0,773 | 1,449 |
| hotties | 0,912 | 1,243 | 1,731 | 1,804 | 2,392 |
| office | 0,730 | 1,156 | 1,445 | 1,530 | 2,357 |
| bath | 0,450 | 1,081 | **0,352** | 0,494 | 0,662 |
| **gepoold** | **0,699** | 1,120 | 1,204 | 1,250 | 1,832 |

Alleen **bath** verliest van persistentie — een raamloze kamer met 0,62 °C totale spreiding,
waar "het blijft zoals het is" moeilijk te verslaan is. Daar valt geen modelleerwerk te halen.

**Het herankeren is verreweg de grootste enkele winst: 1,120 → 0,699 gepoold.** Dat is de reden
dat de vooruitblik in `vent_forecast.py` een *tweede* simulatie is naast de kalibratieloop, in
plaats van de staart van de bestaande sim door te trekken.

Twee nuances bij dat getal, want het is verleidelijk om er te veel van te maken. De
`freerun`-kolom draait op een **ongekalibreerde** 24-uurs aanloop; de productie-sim begint 72 uur
terug op een gemeten zaad en is bovendien op het venster gefit, dus die staat op "nu" dichter bij
de werkelijkheid dan de vrijloop hier. De eerlijke maat voor wat het anker in productie opruimt
is precies de **nowcast-RMSE die het dashboard al rapporteert (~0,54 °C)** — dát is hoe ver de
kalibratiebaan op voorspelmoment naast de meting staat, en dat is de offset die de vooruitblik
anders over de volle 12 uur meesleept. Tegen een 12-uurs fout van ~0,70 °C is dat geen detail.

---

## 2. De kruipruimte-verankering — en waarom je hem niet los kunt meten

`GROUND_SOIL_ANCHOR` 11 °C met `GROUND_AIR_COUPLING` 0,5 zet de kruipruimte in juli op
**15,8 °C**. Een gevéntileerde kruipruimte onder een bewoond huis volgt in een zomer die
gemiddeld 19,7 °C doet veel dichter het gedempte buitengemiddelde (~20,6 °C bij koppeling 1,0).
De 11 °C-bodemwaarde is een **stookseizoen**-aanname die het hele jaar door werd toegepast: met
living op 23,0 °C over 55 m² vloer een staande put van ~250 W — overdag gemaskeerd door de zon,
dominant om 05:00.

Het bewijs is **geometrisch, niet numeriek**: de term schaalt met `ground_m2`, dus hij hóórt
exact de grond-gekoppelde kamers te verbeteren (living 55 m², ted 12, bath 7) en de twee zónder
grondkoppeling (hotties, office) onaangeroerd te laten. Dat patroon is het argument, niet de
gepoolde score.

### De meting, béíde armen her-geseed

`tools/vent_seed.py --days 5` gevolgd door `tools/horizon_backtest.py --horizon-h 12
--stride-h 3 --keep-learned`, 405 oorsprongen. Beide armen komen op dezelfde
**nowcast**-kwaliteit uit (eind-RMSE 0,540 vs 0,541) — precies wat je verwacht van twee fits op
dezelfde nowcast-doelfunctie, en meteen de reden dat die score als voorspelmaat niets zegt.
Op de 12-uurs horizon lopen ze wél uiteen:

| kamer | rev 6 (huidig) | rev 7 (kruipruimte + c_mass-vloer) | Δ |
|---|---|---|---|
| living | 0,669 | **0,573** | **−14 %** |
| bath | 0,420 | **0,363** | **−14 %** |
| ted | 0,517 | 0,523 | +1 % |
| hotties | 0,881 | 0,883 | +0 % |
| office | 0,740 | 0,735 | −1 % |
| **gepoold** | 0,682 | **0,660** | −3 % |

**De handtekening klopt.** De twee kamers zonder grondkoppeling bewegen niet (≤1 %); de winst
zit bij de twee grootste grond-gekoppelde kamers. `ted` is de uitzondering op de offline
verwachting (daar −19 %, hier +1 %) — met 12 m² vloer tegen living's 55 is het effect daar klein
genoeg om in de ruis te vallen, en het is in elk geval geen regressie.

En de diurnale handtekening — waar het allemaal om begon — wordt zichtbaar vlakker. Living's bias
per doel-uur (6–12u vooruit):

| uur | rev 6 | rev 7 |
|---|---|---|
| 03:00 | −0,86 | **−0,62** |
| 06:00 | −0,76 | **−0,45** |
| 12:00 | +0,68 | **+0,55** |
| 15:00 | +0,74 | **+0,35** |
| piek-tot-piek | 1,60 | **1,17** |

Dat is de "over-koelt 's nachts, overschiet 's middags"-cyclus die het meest kost op precies de
vraag waar het dashboard voor bestaat ("hoe warm is het morgenochtend?"), voor ~27 % ingelopen —
en het is de gecombineerde verdienste van de kruipruimte (het niveau) en de `c_mass`-vloer (de
amplitude).

Ter contrast: `INTERNAL_NIGHT_FRACTION` 0,5 → 1,8 scoorde gepoold nóg beter (0,587 vs 0,606) en
is verworpen — het is fysisch absurd (interne warmtelast 's nachts hóger dan overdag) en het
kocht living's winst door hotties van +0,27 naar +0,73 °C te duwen: de aggregatie repareren door
de ene kamer die al klopte te breken. Dezelfde handtekening deed de 30-parameter rollout-refit
sneuvelen (train-doelfunctie 0,80 → 0,58, held-out 0,533 → **0,591**). Beide staan bewust niet
in deze repo.

### De methodologische les

Een eerste meting draaide de nieuwe fysica op de *bestaande* geleerde parameters, en
rapporteerde living **0,693 → 0,741** — een regressie, met een groeiende middagbias (+0,85 →
+1,17 °C om 12:00). Dat is geen tegenspraak met de offline meting maar een voorspelbaar gevolg:
de online fit had twee maanden lang de te koude kruipruimte gecompenseerd (`living.ua_ground`
1,215, `ua_env` 1,327, `q_int` 1,077). De kruipruimte wármer maken bovenóp die compensaties telt
de warmte dubbel.

Dat is precies waarvoor `PHYSICS_REV` bestaat: bij een revisiebump reset `merged_params` alle
parameters naar hun priors en her-seedt `tools/vent_seed.py` ze onder de nieuwe fysica. **Een
fysica-wijziging is daarom alleen eerlijk te meten met beide armen her-geseed** — anders meet je
de parameter-reset, niet de fysica. `--keep-learned` op de backtest bestaat om dat verschil
expliciet te kunnen maken in plaats van er per ongeluk in te trappen.

Rev 6 → **7** dekt beide wijzigingen van deze ronde.

---

## 3. De per-kamer parametervloer (`param_bounds`)

Na de kruipruimte-correctie bleef offline een **diurnale oscillatie** staan: living −0,91 °C om
06:00 tegen +0,54 om 16:00 (6–13u vooruit), ~1,45 °C piek-tot-piek, die tot bijna nul middelt en
dus nóóit in een gepoolde RMSE opduikt. De richting is diagnostisch: de voorspelde dagcyclus is
te *groot* → de effectieve thermische traagheid te laag. En `living.c_mass` stond op de laagste
waarde van alle kamers terwijl living veruit de grootste is (150 m³, 55 m² vloer).

Een **globale** `c_mass`-vermenigvuldiger is getest en verworpen: hielp living (amplitude
1,433 → 1,084) maar verslechterde hotties (0,883 → **1,468**) en ted (0,588 → 0,860). Weer
dezelfde ruil. Living-only ×1,6 → 0,595 haalde het wél, met een vooraf vastgelegde bewaking dat
geen andere kamer meer dan 10 % mocht verslechteren.

**Waarom dit een constraint-wijziging is en geen waarde-wijziging.** `c_mass` zit in
`PER_ROOM_PARAMS`, dus de online Gauss-Newton-stap herleert hem elk kwartier — tegen de
**nowcast**-doelfunctie, en dát is de doelfunctie die hem in de eerste plaats omlaag duwde. Een
getal in `vent_learned.json` schrijven is binnen uren weg. De grens hoort dus in het model dat
hem rechtvaardigt: `house_model.json` → `rooms.living.param_bounds.c_mass.min`, gehonoreerd door
`vent_physics.param_bounds` en toegepast in de klem van `vent_fit` én in `merged_params`. De fit
blijft vrij bóven de vloer.

**Dat de fit er nog steeds onderdoor wil, is de meting die de vloer rechtvaardigt.** In de
her-seeding onder de nieuwe fysica eindigt `living.c_mass` pal op 0,595 — de nowcast wil lager,
de 12-uurs rollout niet. `railed_params` markeert zo'n grens daarom apart als `@floor(model)`:
een `BOUNDS`-rail is een klácht (de fysica wil ergens heen waar ze niet mag), een huismodel-rail
is de constraint die doet waarvoor hij is aangebracht. De acceptatiepoort van
`tools/vent_seed.py` gaat alleen op het eerste af — anders zou de seeding altijd afkeuren op
haar eigen ontwerp.

---

## 4. Wat er níét geland is

* **`tools/shipped_physics.py`.** Bestond alleen omdat de offline sessie de export niet mocht
  wijzigen. Hier zijn het twee echte edits; een runtime-overridelaag zou de correcties
  verstoppen voor wie `vent_physics.py` leest.
* **De rollout-doelfunctie-refit.** §2.
* **`INTERNAL_NIGHT_FRACTION` 1,8.** §2.
* **Elk ML-model van het raam→temperatuur-pad.** De openingen-log heeft ~90 aan/uit-events per
  element, in **béíde richtingen** geconfound (terrasdeuren open als het warm is, keukenkiepraam
  juist dicht op de heetste dagen). Zo'n model leert "terrasdeur open → kamer warmer", precies
  andersom — en de speeltuin is per definitie een counterfactual over exact die elementen.
  Daarom is het luchtstroom-surrogaat **gedistilleerd uit de solver** op uniform getrokken
  standen, niet geleerd uit historie.

---

## 5. De browserkern

`docs/js/vent_core.js` draait de helft van `vent_physics` die van de raamstand afhangt; de
andere helft (zonnegeometrie, glastransmissie, beschaduwing, dak-instraling, buur- en
bodemanker) rekent Python vooraf uit in `vent_forecast.driver_export` en verscheept het als
`docs/vent_forecast.json`. De Newton-druksolver is vervangen door het gedistilleerde surrogaat
(`docs/js/surrogate.json`, 0,39 MB, ~1 M uniform getrokken solver-samples).

Twee dingen die de offline sessie meet en die het waard zijn hier vast te leggen:

* **De eenzijdige-ventilatieterm hoort níét in het surrogaat.** `effective_fresh` is
  `max(netto netwerkdebiet, eenzijdig)`, en die `max()` is een discontinuïteit die een gladde
  MLP uitsmeert. Gevolg: het surrogaat was het in 18 % van de gevallen met de solver oneens over
  de **richting** van een opening — exact de fout die de speeltuin onbruikbaar maakt. Aan beide
  kanten exact rekenen (gesloten vorm, de Gids & Phaff) bracht dat van 82 % naar 95,7 %
  overeenstemming én verbeterde de end-to-end-fout (0,035 → 0,021 °C).
* **De golden-vector heeft drie poorten, niet één.** Poort 1 injecteert Pythons eigen
  per-stap `fresh`/`mix` en test dus uitsluitend de RC-port; poort 2 vergelijkt JS-surrogaat met
  **Python-surrogaat**; pas poort 3 is surrogaat-vs-solver, en die is informatief. Zonder die
  scheiding verstopte een echte portfout (een verdwaalde NUL-byte in een template-literal die
  zone-paar-sleutels bouwde, 9,8e-2 °C) zich comfortabel binnen het foutbudget van het
  surrogaat. Poort 1 en 2 staan in CI.

**Bekende zwakte, in de UI vermeld:** bij hoge ventilatie kunnen de twee kleinste zones (ted,
bath) een factor ~2 naast de solver-ACH zitten. `hotties` en `office` zijn exact, want hun
luchtstroom is eenzijdig gedomineerd en díé term is gesloten-vorm. De temperatuur is er veel
minder gevoelig voor (end-to-end 0,008 °C), maar de speeltuin tóónt ACH, dus het staat er.

---

## 6. Wat open blijft

* **`GROUND_TEMP_MAX` is bij koppeling 1,0 een zomerplafond geworden, geen vangrail — en dat
  is de eerstvolgende meting.** De klem staat op 20 °C en is er "tegen een absurd anker bij
  korte/rare historie". Bij koppeling 0,5 bond hij pas op een 30-daags buitengemiddelde van
  29 °C (in Nederland nooit); bij 1,0 bindt hij al op 20 °C. Over dit record kneep hij het
  bodemanker af in **86 van de 265** oorsprongen — een derde van de tijd, en juist tijdens de
  warme periodes waar de correctie het meest doet. De **−14 % op living hierboven is dus met een
  half toegepaste correctie gemeten**, niet met de volle.

  Bewust niet in deze ronde meegenomen: de klem verruimen is een tweede fysica-wijziging en die
  hoort dezelfde behandeling te krijgen als de eerste (her-seeden + backtest, ~50 min), niet een
  aanname erbovenop. `tests/test_vent_physics.py::test_ground_temp_max_is_een_vangrail_geen_zomerplafond`
  legt de redenering vast en faalt zodra iemand de koppeling verder verhoogt zonder de vangrail
  mee te schalen. De winterkant (`GROUND_TEMP_MIN` 6 °C) knijpt bij koppeling 1,0 net zo goed —
  daar is het waarschijnlijk juist wenselijk, maar even onbeproefd.
* **De weersvoorspellingsfout is ongemeten.** Zie §1. Grootste openstaande onzekerheid.
* **Geen stookseizoendata.** Het record loopt 2026-05-29 → 08-04, buiten 9,2–36,3 °C, en
  `heating==1`-samples zitten sowieso niet in de kalibratie. Winter is extrapolatie — en
  koppeling 1,0 maakt de kruipruimte 's winters júist kouder dan de oude verankering. De
  richting is fysisch juist, de grootte onbeproefd. Herijken zodra er stookdata is.
* **De onzekerheidsband is een ondergrens.** `tools/export_uncertainty.py` geeft de empirische
  p10/p50/p90 van (voorspeld − gemeten) per kamer per horizon-uur — modelfout alleen, gemeten op
  standen die het huis werkelijk had. Op een counterfactual toepassen is een aanname.
* **Geen achtergrond-infiltratieterm.** Een volledig dicht huis wisselt in dit model géén lucht
  uit (elke zone heeft één lekopening naar buiten, dus massabehoud dwingt het netto debiet op
  nul; die lekken bestaan om de druksolve niet-singulier te houden). Echte infiltratie is
  geabsorbeerd in de geleerde `ua_env`.
* **`office.f_air@floor` railt nog steeds** — het overgebleven er-ontbreekt-nog-iets-signaal uit
  de vorige ronde (dak-kamer, zon-naar-massa-split). Onaangeroerd.

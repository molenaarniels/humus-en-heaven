# Ventilatie 2 (Project 12) — assessment augustus 2026: eenzijdige ventilatie

> **Historisch document (aug 2026):** beschrijft de in de herbouw vervangen tweelingen (Projects 8/12). Het huidige ventilatie-project is Project 13 (vent_*.py) — zie CLAUDE.md. De meetresultaten hieronder blijven de onderbouwing van de geporteerde fysica.

_Datum: 2026-08-02. Databron: een held-out-campagne met `tools/twin2_experiment.py`
(10 runs, `--lam0 1.0 --epochs 14`, rotaties `off2` compleet + `off0` deels), de gefitte
parametersets daaruit, en `tools/twin2_residual_diagnostics.py`. Aanleiding: de vraag of het
kantoordak slecht geïsoleerd is (nee — 0,257 W/m²K klopt) en, in het verlengde daarvan, wat de
tweeling écht beter maakt._

**Oordeel in één alinea.** De twee kandidaat-correcties op de zon-invoer (DNI-conventie,
binnengordijn-warmtefactor) zijn allebei fysisch juist maar **thermisch marginaal**: over twee
rotaties begrensd op ≲0,06 °C, tegen een A/A-ruisvloer van 0,032 °C en een held-out-fout van
0,81–0,93 °C. Onderweg wees de campagne wél de échte hefboom aan, en die zit niet in de warmte
maar in de **lucht**: in álle tien runs — arm-onafhankelijk, `baseline_bare` incluis — railen
10–15 van de 84 parameters, **allemaal op hun vloer**. Die saturatie-tell blijkt te herleiden
tot één structurele blinde vlek: het drukwerk-netwerk kan **eenzijdige ventilatie** (een raam
open in een kamer) niet representeren. Het rekent alleen het *netto* debiet, terwijl de
pulserende/buoyante uitwisseling dóór één opening de dominante term is. Gevolg: bij weinig wind
onderschat het model de luchtverversing ~15–25×, bij veel wind schiet het juist door — een
wind-helling van **243×** waar de empirie er **1,4×** geeft.

---

## 1. Wat de campagne over de zon-invoer zegt

A/A-ruisvloer (`aa_bare_1` vs `aa_bare_2`, identieke config, ±2% startjitter): **0,0321 °C**
op 5d, 0,0356 op de zon-plak.

| arm | rotatie `off2` Δ5d | ×ruis | `off0` Δ5d |
|---|---|---|---|
| `shade_internal` | −0,061 | 1,9× | +0,004 |
| `solar_dni` | −0,016 | 0,5× | −0,002 |
| `solar_dni_shade` | −0,012 | 0,4× | +0,006 |

Geen consistente winst. Wél **geen regressie** — dat is de adoptiegrond die overblijft, en die
volstaat voor twee wijzigingen die op fysische correctheid staan (zie CLAUDE.md / de branch).

> **Methodologische waarschuwing.** Een eerste ronde draaide op de default `--lam0 1e-3` in
> plaats van de ~1.0 die `batch_fit`'s docstring voor de campagne voorschrijft. Vanaf kale
> priors groef de vrijwel ongedempte eerste GN-stap zich in de train-vensters in: A/A-ruisvloer
> **0,21 °C**, groter dan elk armeffect. Die ronde is weggegooid. En: één A/A-arm is géén
> ruisvloer — een tussenrapportage op basis van `baseline` vs. één jitter-arm gaf 0,0092 en
> overschatte de effecten ~3,5×.

## 2. De saturatie-tell

Gerailde parameters, over alle tien runs (84 params per run), **geen enkele op een plafond**:

| parameter | runs |
|---|---|
| `ted.ua_env`, `ted.ua_party`, `hotties.ua_party`, `office.f_air`, `bath.ua_env` | 10/10 |
| `living.f_air` | 9/10 |
| `ted.f_air` | 8/10 |
| `office.ua_env`, `office.w_buf`, `hotties.f_air` | 7/10 |
| `living.c_deep`, `office.c_deep`, `bath.c_deep` | 6/10 |

`ua_party` op **0,0** en `ua_env` op 0,05 (5% van de geometrische basis) betekent: de fit wil de
kamers van élke warme rand loskoppelen. `f_air` op zijn vloer in 4 van de 5 kamers betekent: alle
zonwarmte naar de massaknoop, want de luchtknoop reageert te snel. Dit is precies de tell die
CLAUDE.md bij fysica-rev 2 als afspraak vastlegde: *"BOUNDS2 is bewust NIET verruimd: na de
nieuwe termen hoort het railen vanzelf te ontspannen — gebeurt dat niet, dan is dát het signaal
dat er nog fysica ontbreekt."* Het ontspant niet.

## 3. Waar de fout werkelijk zit

`tools/twin2_residual_diagnostics.py --residuals`, held-out `off2`, 6792 residuen:

**Per binnen-buiten-verschil** — het model is te warm precies wanneer de kamer koeler is dan
buiten:

| binnen − buiten | bias | rmse |
|---|---|---|
| ≤ −3 K | **+0,325** | 0,870 |
| −3 … −1 K | +0,295 | 0,875 |
| −1 … +1 K | +0,123 | 0,810 |
| +1 … +3 K | +0,091 | 0,932 |
| ≥ +3 K | −0,010 | 0,946 |

**Per raamstand × wind** — dít is de doorsnijding die het verklaart:

| toestand | 0–1,5 m/s | 1,5–3 | 3–5 |
|---|---|---|---|
| raam **open** | bias **+0,49** (rmse 1,46) | +0,03 (1,08) | bias **−0,40** (0,96) |
| raam **dicht** | +0,22 (0,76) | +0,06 (0,78) | +0,08 (0,82) |

Raam dicht: vlak over álle windsnelheden — de thermische kern is gezond. Raam open: een
duidelijke wind-helling, te warm bij weinig wind, te koud bij veel wind. Hotties (grootste
openende vlak, 1,4 m²) is de extreemste: **rmse 1,60 met raam open 's nachts vs. 0,52 met raam
dicht** — zelfde kamer, zelfde parameters.

## 4. De oorzaak

`tools/twin2_residual_diagnostics.py --ventilation --room hotties` — netwerk-verselucht vs. de
empirische de Gids & Phaff-correlatie voor eenzijdige ventilatie
(`Q = (A/2)·√(C1·U² + C2·H·ΔT + C3)`, 1982):

| wind (ΔT = 3 K) | netwerk ACH | deGids ACH | onderschatting |
|---|---|---|---|
| 0,5 m/s | 0,58 | 14,20 | 24× |
| 2 m/s | 0,83 | 15,00 | 18× |
| 4 m/s | 1,20 | 17,30 | 15× |
| 6 m/s | 141,60 | 20,58 | 0,1× (7× overschat) |

Wind-helling 0,5 → 6 m/s: **netwerk 243×, empirie 1,4×**. De correlatie is vrijwel vlak omdat
buoyantie + turbulente pulsatie domineren; het netwerk heeft alléén winddruk en schaalt dus met
U². Dat verklaart beide takken van de residu-bias tegelijk, en het verklaart waarom de fit het
niet kan repareren: `cp_shelter_front/back` en `vent_eff` zijn **lineaire vermenigvuldigers** —
die kunnen een verkeerde *helling* niet rechttrekken, alleen het kruispunt verschuiven. Vandaar
dat de optimizer uitwijkt naar `ua_env`/`ua_party`/`f_air` en die in hun vloer drukt.

> **Correctie (zelfde dag, na §5).** De regel bij 6 m/s in de tabel hierboven — 141,60 ACH —
> is **géén fysica maar een mislukte solve**: de drukoplosser gaf daar een niet-oplossing terug
> (massabalans 1,4 kg/s tegen een tolerantie van 1e-6). De échte, geconvergeerde waarde is
> 1,49 ACH. Daarmee vervalt de "wind-helling 243×" uit deze paragraaf: het netwerk loopt in
> werkelijkheid van 0,58 ACH (0,5 m/s) naar 2,88 (15 m/s), een helling van ~5×. Wat **wél**
> overeind blijft is de onderschatting bij lage wind (15–25×, gemeten op geconvergeerde
> solves) — dat is en blijft de grond onder de eenzijdige-ventilatie-term. De
> "overschat bij veel wind"-tak van de diagnose was dus een solver-artefact; de −0,40 °C-bias
> bij 3–5 m/s heeft daarmee nog géén sluitende verklaring.

## 5. De drukoplosser gaf stilzwijgend niet-oplossingen terug

`solve_network`'s Newton-iteratie **oscilleerde**: de druk van hotties sprong heen en weer
tussen ~12,5 en ~0,4 Pa terwijl de line search elke stap accepteerde (de SSE dáált immers,
maar minimaal: 3,12 → 2,40 in 20 iteraties). Na 40 iteraties viel de lus uit de `for` en gaf
de functie de laatste drukvector terug **alsof het een oplossing was** — zonder enige
convergentiecontrole. Massabalans ~1,4 kg/s: ruim een kuub lucht per seconde uit het niets.

Niet twee wortels dus (de eerste lezing), en ook geen niet-lineaire fysica: één wortel plus
een afgekapte iteratie. Bewijs: dezelfde invoer die koud 135 ACH gaf, loste warm-gestart
vanaf de 5,5 m/s-oplossing keurig op 1,49 ACH.

**Omvang op échte data** — over alle twaalf shard-vensters (7501 netwerk-solves met de
werkelijk gemelde raamstanden):

| | niet-geconvergeerd |
|---|---|
| oude solver | **119 / 7501 (1,59 %)**, bij wind **0,7–6,1 m/s** |
| na de fix | 0 / 7501 |

Let op de ondergrens: 0,7 m/s. Het is dus **geen hoge-wind-probleem** maar een
configuratie-probleem — welke openingen open staan bepaalt of de iteratie gaat oscilleren.
En omdat `simulate` elke stap warm start vanaf de vorige, kan één mislukte solve de
volgende stappen meetrekken.

**De fix.** De volle Newton-stap blijft de eerste poging (kosten ongewijzigd voor de ~98 %
die gewoon convergeert); faalt hij, dan volgt één herkansing met de stapfractie geklemd op
`NET_ALPHA_RETRY` (0,5). Dat breekt de oscillatie: getest van 0,5 tot 15 m/s convergeert
hij dan in 17–19 iteraties. `solve_network` geeft nu ook `converged` + `residual` terug,
zodat een mislukte solve zichtbaar is i.p.v. stilzwijgend doorgegeven.

**Waarschijnlijk gevolg voor eerder werk:** 1,59 % vervuilde tijdstappen zaten in élke
kalibratie, ook in de campagne van §1 — een kandidaat-verklaring voor `converged: False` in
9 van de 10 fits en voor de A/A-ruisvloer. De campagne van §6.2 hoort dus **na** deze fix
opnieuw te draaien.

## 6. Status en vervolg

> **Update, zelfde dag:** punt 1 is **geïmplementeerd en uitgerold** (fysica-rev 5 / rev 4),
> gebundeld met de twee zon-correcties zodat er één herleer-cyclus nodig is i.p.v. twee.
> **Nog niet held-out gemeten** — punt 2 staat dus nog volledig open, en is nu de eerste
> prioriteit: als de term niet doet wat §3–4 voorspellen, hoort hij terug.

1. **Eenzijdige-ventilatie-term voor buitenramen.** Het patroon bestaat al in deze codebase:
   `am.buoyant_door_exchange` (Brown–Solvason) voegt voor bínnendeuren een bidirectionele
   buoyante uitwisseling toe — bewust *buiten* de geleerde `vent_eff`, omdat het een fysieke
   orifice-term is. Voor buitenramen ontbreekt het equivalent; de Gids & Phaff is de
   standaardformule. Toe te voegen als extra verse-lucht-bijdrage per buitenraam wanneer de
   kamer géén doorstroompad heeft, niet als vervanging van het netwerk.
2. **Meten (nu: achteraf) — de openstaande verplichting.** Met de bewezen campagne-instellingen (`--lam0 1.0 --epochs 14`,
   drie rotaties, béíde A/A-armen). Verwachting: het railen van `ua_env`/`ua_party`/`f_air`
   ontspant — dát is de primaire uitkomstmaat, niet alleen de RMSE.
3. ~~**De 6 m/s-uitschieter** apart nalopen~~ — gedaan, zie §5: het was een
   mislukte solve, inmiddels gefixt.
4. De zon-correcties zijn hiermee **niet** waardeloos: ze zijn fysisch juist en P9 (zonwering)
   consumeert absolute watts zonder leerbare schaal. Ze horen alleen niet als
   prestatieverbetering verkocht te worden.

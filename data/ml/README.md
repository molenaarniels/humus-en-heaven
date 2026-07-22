# Ventilatie-tweeling — ML-dataset

Een platte, model-agnostische dataset om **lokaal** (bv. op een MacBook, in een
notebook) zelf een model te trainen op de digitale tweeling van het huis:
voorspel per kamer de temperatuur (en vocht) uit het weer, de zonstand en de
gemelde raam-/deur-/roosterstanden — en experimenteer met de "perfecte"
modelstructuur en parameters.

De **bron** staat al in git: `data/twin2_history/<YYYY-MM>.json` (maand-shards,
elk kwartier bijgewerkt). Deze map bevat de *afgeleide, platte* export daarvan.

## Bouwen

```bash
# vanaf de repo-root (volledig offline — geen secrets, geen internet nodig):
python tools/export_ml_dataset.py                 # incl. fysica-baseline (~1 min)
python tools/export_ml_dataset.py --no-baseline   # alleen data (paar seconden)
```

Of download het kant-en-klaar via GitHub Actions → workflow **"ML-dataset
export"** → *Run workflow* → artefact `ventilation-ml-dataset` (bevat ook
`.parquet`).

Uitvoer (in `data/ml/`, niet in git — regenereert):

| bestand | inhoud |
|---|---|
| `ventilation_long.csv` / `.parquet`  | **één rij per (tijdstip, kamer)** — handig voor per-kamer- en pooled-modellen |
| `ventilation_wide.csv` / `.parquet`  | **één rij per tijdstip**, kamertemps als kolommen — handig voor state-space / RC / multi-output |
| `schema.json`                        | data-dictionary: elke kolom → betekenis + eenheid |

`.parquet` verschijnt alleen als `pandas` + `pyarrow` geïnstalleerd zijn
(`pip install pandas pyarrow`).

## Kolommen (kort)

Volledige beschrijving + eenheden staan in `schema.json`. Hoofdgroepen:

- **Doelwaarden** (wat je voorspelt): `temp_c` (gemeten tado-kamertemp, °C),
  `humidity` (%RH). Leeg wanneer er geen meting binnen `--join-tol-min` (10 min)
  van het rooster-tijdstip lag.
- **Weer** (gedeeld per tijdstip): `t_out_c`, `rh_out`, `wind_speed_ms`,
  `wind_dir_deg`, `gust_ms`, `precip_mm`, `solar_direct/diffuse/global_wm2`,
  `sun_az_deg`, `sun_el_deg`, `neighbor_anchor_c` (party-muur-buur-anker).
- **Per-kamer drivers**: `solar_glass_w` (instraling dóór het glas, som over de
  ramen), `roof_irr_w` (dak-instraling, alleen bovenverdieping).
- **Bedieningsstanden** (`open_<element>`, 0..1): elke raam/deur/rooster —
  `0`=dicht, `1`=open, kier = het element-eigen `tilt_frac`. Gedeeld per
  tijdstip (het huis is gekoppeld: een open deur tussen twee kamers hoort in
  beide modellen te zitten).
- **Uitsluit-vlaggen**: `heating` (tado stookt in deze kamer), `ac_here` (de
  mobiele airco staat hier), `ac_room` (welke kamer), `paused` (huis-breed
  handmatig gepauzeerd). De tweeling **laat deze samples uit de kalibratie
  vallen** omdat de fysica geen actieve verwarming/koeling kent — reproduceer
  dat door ze te filteren (zie hieronder).
- **Fysica-baseline** (optioneel, `--baseline`): `pred_twin1_c` (2-knoops RC),
  `pred_twin2_c` (3-knoops RC + vocht). De rúwe fysica-voorspelling in
  sensor-ruimte, hergeseed per niet-overlappend 5-daags venster met 24 u warmup
  (dezelfde manier waarop het dashboard scoort), **zonder** de online
  bias-correctie ("tarrering"). Dit is je lat om te verslaan, of het doel voor
  residu-leren.

In het **wide**-formaat krijgen de per-kamer-kolommen een `__<kamer>`-suffix
(bv. `temp_c__office`, `pred_twin2_c__ted`); de weer-/stand-kolommen staan er
één keer.

## Snel starten (pandas)

```python
import pandas as pd

df = pd.read_parquet("data/ml/ventilation_long.parquet")   # of read_csv(...)

# Schone trainingsset: alleen echte metingen, geen stook/airco/pauze-momenten
# (net als de tweeling zelf kalibreert).
clean = df[
    df["temp_c"].notna()
    & (df["heating"] == 0)
    & (df["ac_here"] == 0)
    & (df["paused"] == 0)
].copy()

# Hoe goed doet de bestaande grey-box het al? (de lat)
mae_twin2 = (clean["temp_c"] - clean["pred_twin2_c"]).abs().mean()
print(f"twin2 MAE = {mae_twin2:.2f} °C")
```

### Je eigen model trainen

```python
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

feat = [c for c in clean.columns if c.startswith("open_")] + [
    "t_out_c", "rh_out", "wind_speed_ms", "wind_dir_deg", "gust_ms",
    "solar_glass_w", "roof_irr_w", "sun_el_deg", "neighbor_anchor_c",
]
# tijd-split: train op het verleden, test op de laatste 20%
clean = clean.sort_values("t_epoch")
cut = int(len(clean) * 0.8)
tr, te = clean.iloc[:cut], clean.iloc[cut:]

m = HistGradientBoostingRegressor().fit(tr[feat], tr["temp_c"])
print("mijn MAE:", mean_absolute_error(te["temp_c"], m.predict(te[feat])))
```

### Residu-leren (leer alleen de fout van de fysica)

Vaak sterker dan from-scratch: laat het ML-model enkel de systematische
afwijking van de grey-box corrigeren.

```python
d = clean[clean["pred_twin2_c"].notna()].copy()
d["residual"] = d["temp_c"] - d["pred_twin2_c"]          # doel = de fysica-fout
# ... train op d[feat] → d["residual"], en tel de voorspelde residu bij
#     pred_twin2_c op. Meet weer tegen temp_c.
```

## Belangrijk

- **Tijd-splits, geen willekeurige shuffle**: opeenvolgende rijen zijn sterk
  gecorreleerd (15-min cadans). Split op `t_epoch` of gebruik
  `TimeSeriesSplit`, anders lek je toekomst naar het verleden.
- **Kamers zijn gekoppeld**: deuren/roosters verbinden kamers; een
  hele-huis- (wide) of pooled-model met de `open_*`-standen kan die koppeling
  leren die een per-kamer-model mist.
- **Groeit vanzelf**: de shards worden elk kwartier aangevuld — draai de export
  opnieuw na een `git pull` voor verse data. Mei 2026 is dun (pre-kwartier-
  commit-tijdperk); juni–juli zijn dicht.
- De export raakt **niets** in de pipelines aan; het is een read-only afgeleide
  van de gecommitte shards.

"""Open-Meteo's systematische temperatuurafwijking op onze locatie — zelflerend.

Het spiegelbeeld van `wu_bias.py`. Daar corrigeren we een *meetfout* van het eigen
station (stralingskap-opwarming) met een door Project 7 gekalibreerde constante; hier
corrigeren we een *modelfout* van Open-Meteo, en die constante kunnen we niet
vastleggen: hij verschuift met het seizoen en met het weertype. Daarom leert deze
module hem online uit de eigen historie.

**Wat er mis is.** Open-Meteo leest op onze locatie structureel te warm, en 's nachts
het sterkst. Gemeten over 25 dagen (~15.700 geverifieerde forecast-punten, juli 2026)
tegen het eigen WU-station: mediaan **+1,4 °C 's nachts** (22–07u) en **+0,8 °C
overdag**, bij élke vooruitblik. Dat is deels een echte modelfout en deels
microklimaat — een rastercel van kilometers middelt onze beschutte, begroeide tuin weg,
en juist de nachtelijke uitstralingsafkoeling is daar lokaal veel sterker dan het
raster laat zien. Voor het raamadvies maakt dat onderscheid niet uit: wat telt is de
temperatuur bij ónze ramen, en dat is precies wat het station meet.

**Waarom de bestaande stationsijking dit niet ving.** `window_advisor.correct_forecast`
ankerde de forecast al op het station, maar liet die correctie lineair uitdoven naar
**nul** over `BIAS_DECAY_H` (12u) — in de veronderstelling dat het ruwe model op termijn
zuiver is. Dat is het niet. Gevolg: het hele nachtvenster, dat vanaf een ochtend- of
middagrun 12–18u vooruit ligt, draaide op de ónvergeleken modelwaarde inclusief de volle
warme bias. Precies het venster waarin het raamadvies telt.

De ankerwaarde zélf blijkt maar ~6 uur informatief (correlatie met de werkelijke
modelfout: r=0,66 bij 0–3u vooruit, 0,27 bij 3–6u, ~0 daarna), dus de uitdoving is qua
vórm goed. Alleen de bodem klopte niet: uitdoven naar de geleerde climatologie in plaats
van naar nul.

**Meetmethode.** Niet de nowcast-fout (model-nu vs. station-nu) maar echte
*forecastverificatie*: elke run legt de forecast voor een uur dat `RECORD_LEAD_MIN_H`–
`RECORD_LEAD_MAX_H` vooruit ligt vast; zodra dat uur voorbij is wordt hij afgerekend
tegen wat het station toen daadwerkelijk mat. Dat is de eerlijke maat — de modelfout op
lange termijn is meetbaar groter dan de nowcast-fout (+1,4 vs. +1,0 's nachts), dus
leren op de nowcast zou structureel ondercorrigeren.

**Waarom nacht/dag en niet 24 uurvakken.** Op een holdout (eerste helft leren, tweede
helft toetsen) verslaat een gladgestreken 24-slots uurprofiel de ongecorrigeerde
forecast niet: het middagpiekje dat het uit de ene hittegolfweek leert, geldt de
volgende week niet meer (MAE 1,31 vs. 1,23 voor één constante). Twee emmers — nacht en
dag — generaliseren wél, en pakken het leeuwendeel van de winst.

**Gemeten effect** (holdout, tweede helft van 25 dagen, ~7.200 punten):

    schema                              MAE    RMSE  |  nacht MAE   RMSE
    huidig: anker → 0 over 12u          1,23   1,64  |     1,69     2,21
    anker → geleerde nacht/dag-bias     1,09   1,41  |     1,20     1,55

Dus ~30% minder fout in de nacht, en geen verslechtering overdag.

Zuivere functies over gewone dicts; de logboekopslag zelf leeft additief in
`docs/window_data.json` (veld `om_bias`), waar de aanroeper hem persisteert.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# ── Emmers ────────────────────────────────────────────────────────────────────
# Nacht loopt van NIGHT_START tot NIGHT_END (lokale tijd). De grens ligt bewust ruim:
# de bias is het grootst tussen middernacht en zonsopkomst, maar loopt aan beide kanten
# geleidelijk af, en een ruime emmer houdt de steekproef groot.
NIGHT_START = 22
NIGHT_END   = 7

# ── Leren ─────────────────────────────────────────────────────────────────────
LEARN_WINDOW_D  = 14    # dagen — geheugen van de geleerde bias. Korter (7d) reageert
                        # sneller op een weeromslag, langer (21d) is stabieler; op de
                        # holdout liggen 7–21d binnen 0,03 °C MAE van elkaar, dus 14d als
                        # middenweg: ruim genoeg voor een stabiele mediaan, kort genoeg om
                        # met het seizoen mee te schuiven.
MIN_SAMPLES     = 24    # per emmer — onder dit aantal geven we 0 terug (dus: gedraag je
                        # exact als vóór deze module). Ruwweg een dag aan nachturen.
MAX_BIAS        = 3.0   # °C — klem op de geleerde correctie. Een grotere systematische
                        # afwijking is geen bias meer maar een storing (station stuk,
                        # verkeerde locatie); dan liever ondercorrigeren dan het advies
                        # laten ontsporen.

# ── Verificatie ───────────────────────────────────────────────────────────────
RECORD_LEAD_MIN_H = 6.0   # uur — alleen forecasts mét échte vooruitblik afrekenen. Korter
RECORD_LEAD_MAX_H = 18.0  # dan dit zit het stationsanker er nog grotendeels op, en dan
                          # meet je je eigen correctie i.p.v. de modelfout.
MATCH_TOL_MIN     = 10    # min — hoe dicht een meting bij het hele uur moet liggen om als
                          # verificatie te tellen (historie is kwartierlijks).
PENDING_GIVE_UP_H = 30    # uur — een openstaande verificatie waarvoor nooit een meting
                          # kwam (station uit, loop gepauzeerd) vervalt hierna.

# ── Overgang ──────────────────────────────────────────────────────────────────
TRANSITION_H = 1.0  # uur — breedte van de gladde overgang tussen de emmers. We *leren*
                    # met een harde nacht/dag-knip (een sample hoort bij één emmer), maar
                    # *passen toe* met een lineaire vervloeiing rond de grenzen. Zonder
                    # dat maakte de correctie een sprong van ~1 °C tussen twee
                    # opeenvolgende forecast-uren, puur doordat de emmer omklapte — een
                    # kunstmatige knik in de curve waar `predict_open_intervals` zijn
                    # kruising op kan vastpakken ("open om 22:00" die alleen bestaat
                    # omdat de correctie daar een stap zet). Zelfde patroon als de
                    # 1-uurs overgangen van `neighbor_night_cap` in de tweelingen.


def is_night(dt: datetime) -> bool:
    """Valt dit tijdstip in de nachtemmer? (lokale klokuren, wikkelt over middernacht)"""
    return dt.hour >= NIGHT_START or dt.hour < NIGHT_END


def bucket(dt: datetime) -> str:
    return "night" if is_night(dt) else "day"


def _parse(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def record_forecast(log: dict, forecast: list[dict], now: datetime) -> dict:
    """Leg de forecast vast voor elk uur dat RECORD_LEAD_MIN_H..MAX_H vooruit ligt, zodat
    hij later afgerekend kan worden. Een uur dat al vastligt (openstaand óf al
    geverifieerd) wordt niet overschreven — zo houdt elk uur de vroegst vastgelegde
    forecast, en meten we een consistente vooruitblik in plaats van een mengelmoes.

    `forecast` = rijen met "dt" (datetime) en "temp" (ruwe modelwaarde, ongecorrigeerd).
    """
    log = dict(log or {})
    pending = list(log.get("pending") or [])
    seen = {p.get("t") for p in pending} | {e.get("t") for e in (log.get("errors") or [])}
    for r in forecast:
        dt, raw = r.get("dt"), r.get("temp")
        if dt is None or raw is None:
            continue
        lead_h = (dt - now).total_seconds() / 3600.0
        if not (RECORD_LEAD_MIN_H <= lead_h <= RECORD_LEAD_MAX_H):
            continue
        key = dt.isoformat()
        if key in seen:
            continue
        pending.append({"t": key, "fc": round(raw, 2)})
        seen.add(key)
    log["pending"] = pending
    return log


def verify_pending(log: dict, history: list[dict], now: datetime) -> dict:
    """Reken openstaande forecasts af tegen wat het station daarna daadwerkelijk mat.

    `history` = `outside_history` uit het dashboard-artefact. Alleen samples waarvan het
    **station** de bron is tellen mee: op een Open-Meteo-terugval is de "meting" ditzelfde
    model, en dan zou de fout per definitie ~0 zijn en de geleerde bias verwateren. Oudere
    samples zonder `src`-veld worden herkend aan `temp == om` (exact gelijk = terugval).
    """
    log = dict(log or {})
    meas = []
    for s in history or []:
        t, temp = _parse(s.get("t")), s.get("temp")
        if t is None or temp is None:
            continue
        src = s.get("src")
        if src is not None and src != "wu":
            continue
        if src is None and s.get("om") is not None and abs(temp - s["om"]) < 1e-9:
            continue  # oud sample zonder src: exacte gelijkheid verraadt de terugval
        meas.append((t, temp))
    meas.sort()

    errors = list(log.get("errors") or [])
    still_pending = []
    tol = MATCH_TOL_MIN * 60
    for p in log.get("pending") or []:
        t = _parse(p.get("t"))
        if t is None or p.get("fc") is None:
            continue
        if t > now:
            still_pending.append(p)
            continue
        best, best_d = None, tol + 1
        for mt, mv in meas:
            d = abs((mt - t).total_seconds())
            if d < best_d:
                best, best_d = mv, d
        if best is not None and best_d <= tol:
            errors.append({"t": p["t"], "e": round(p["fc"] - best, 2)})
        elif (now - t).total_seconds() / 3600.0 <= PENDING_GIVE_UP_H:
            still_pending.append(p)          # meting kan nog komen
        # anders: opgegeven, valt weg

    cutoff = now - timedelta(days=LEARN_WINDOW_D)
    errors = [e for e in errors if (_parse(e.get("t")) or cutoff) >= cutoff]
    errors.sort(key=lambda e: e["t"])
    log["errors"] = errors
    log["pending"] = still_pending
    return log


def _median(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def learn(log: dict) -> dict:
    """Vat het verificatielogboek samen tot de geleerde modelfout per emmer.

    Mediaan (niet gemiddelde): één onweersnacht met een uitschieter van 6 °C mag de
    correctie niet meeslepen. Positief = het model leest te warm.
    """
    buckets: dict[str, list[float]] = {"night": [], "day": []}
    for e in log.get("errors") or []:
        t = _parse(e.get("t"))
        if t is None or e.get("e") is None:
            continue
        buckets[bucket(t)].append(e["e"])
    out = {}
    for name, vals in buckets.items():
        if len(vals) >= MIN_SAMPLES:
            out[name] = round(max(-MAX_BIAS, min(MAX_BIAS, _median(vals))), 2)
        else:
            out[name] = 0.0
        out[f"n_{name}"] = len(vals)
    return out


def night_weight(dt: datetime) -> float:
    """Hoe "nacht" is dit tijdstip? 1,0 midden in de nacht, 0,0 midden op de dag, met een
    lineaire overgang van TRANSITION_H rond beide emmergrenzen (zie daar waarom)."""
    length = (NIGHT_END - NIGHT_START) % 24          # duur van de nachtemmer in uren
    u = ((dt.hour + dt.minute / 60.0) - NIGHT_START) % 24   # uren sinds het nachtbegin
    half = TRANSITION_H / 2.0
    rise = (u + half) / TRANSITION_H                 # inloop bij NIGHT_START
    fall = (length - u + half) / TRANSITION_H        # uitloop bij NIGHT_END
    return max(0.0, min(1.0, min(rise, fall)))


def bias_for(dt: datetime, learned: dict | None) -> float:
    """Geschatte modelfout (°C, + = model te warm) voor een tijdstip: de nacht- en
    dagwaarde vervloeid met `night_weight`. 0 als er nog niet genoeg geleerd is — dan
    gedraagt de aanroeper zich exact als vóór deze module."""
    if not learned:
        return 0.0
    def _val(key):
        v = learned.get(key)
        return float(v) if isinstance(v, (int, float)) else 0.0
    w = night_weight(dt)
    return w * _val("night") + (1.0 - w) * _val("day")


def correction_for(dt: datetime, learned: dict | None) -> float:
    """De correctie die bij de ruwe forecast opgeteld moet worden (tegengesteld teken aan
    `bias_for`: het model leest te warm, dus we trekken eraf)."""
    return -bias_for(dt, learned)

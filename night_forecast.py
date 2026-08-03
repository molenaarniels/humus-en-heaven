"""Teds nachtvoorspelling (Project 10) — hoe warm wordt de slaapkamer vannacht?

Draait 's avonds (orchestrator-doel 18:45, vóór peuter-bedtijd ~19:00) en
voorspelt met de gekalibreerde luchtstroom-tweeling (Project 8) hoe Teds kamer
de nacht doorkomt:

1. **Nachtcurve, in twee fasen** — een *aanloop*-sim van 24u (`WARMUP_H`,
   geseed op de oudste tado-temp in dat venster) laat de massaknoop
   equilibreren en re-simuleert het etmaal tot nu met de échte gerapporteerde
   log + het échte weer (geen scenario). Op "nu" wordt de luchtknoop-toestand
   gecorrigeerd met de meest recente échte tado-meting per kamer (mits vers
   genoeg — zie `ANCHOR_MAX_STALENESS_MIN`); de massaknoop wordt tegelijk op de
   gemeten kamerlucht geijkt (`anchor_mass_now` — gedempt gemiddelde, tau
   `MASS_ANCHOR_TAU_H`). Dat laatste is niet cosmetisch: de massa draagt veruit
   de meeste capaciteit, dus zolang die de warmup-drift vasthield trok hij de
   zojuist geijkte lucht er binnen een paar uur weer doorheen (9 held-out
   nachten: RMSE 1,46 → 0,86 °C). Pas dán start
   de *forecast*-sim (nu → morgen 08:00) — zo draagt de nacht-voorspelling
   geen ongecorrigeerde 24u-drift meer mee. De `end_h`-parameter van
   build_timeline rekt die tweede fase op; fetch_weather's forecast_days=2
   dekt de horizon ruim.
2. **Raam-scenario's** — drie forecast-sims vanaf nu: (a) `ted_small_window`
   dicht, `ted_stair` dicht (rooster blijft in alle scenario's ongemoeid
   open) — **dit is de aanname voor de échte nacht** — deur én raampje gaan
   's nachts standaard dicht, dus dit scenario is het hoofdbericht + de basis
   voor het slaapzakadvies; (b) `ted_small_window` open, `ted_stair` nog
   steeds dicht — zonder de deur geforceerd dicht te houden zou de sim 's
   nachts blijven meekoelen met het (stratifiërende, dak-gekoelde) trapgat
   terwijl de deur in werkelijkheid dicht gaat; (c) **alles open** —
   `ted_small_window` open én *elk* raam en *elke* deur in het huis open
   (incl. `ted_stair`) — het meest gunstige doorwaai-scenario. (b) en (c)
   zijn louter een informatieve vergelijking ("zou dit schelen"), geen advies
   om het raampje of de deuren ook echt open te zetten.
3. **Gordijnroutine (`apply_shade_routine`)** — het verduisteringsgordijn vóór
   `ted_window` (het grote vaste raam, een boom + overburen schaduwen het al
   deels — vandaar het lage horizon_elevation_deg — maar niet 's avonds vóór
   het dichtgaat) gaat elke avond om `SHADE_CLOSE_H` (19:00) dicht bij het
   slapengaan, ongeacht wat de openingen-log op dat moment toevallig meldt.
   Toegepast op **beide** fasen: zonder dit rekende de aanloopsim een avond
   met een in werkelijkheid al dicht gordijn alsnog als open door (fors
   avondzon-vermogen door een 3×1.5m raam), wat de niet-geankerde massaknoop
   een structurele warm-bias gaf die de forecast-fase optilde — een
   voorspelde nachtstijging die in de echte tado-metingen niet terugkwam
   (gediagnosticeerd juli 2026: 07:00 voorspeld ~1°C te warm, twee dagen op
   rij, terwijl de gemeten kamer al aan het dalen was).
4. **Tog/slaapzak-advies** — het nachtgemiddelde van het dicht-scenario door de
   standaard peuter-slaapzaktabel.

Bewust GEEN WU-verfijning (de sim is forecast-gedreven en de seed komt van
tado — het station voegt hier niets toe en zo blijven de WU-secrets uit deze
workflow) en geen dashboard-artefact (v1 is stateless; alleen Telegram).

Verzend-poort: in het zomerseizoen (mei–sep) elke avond; daarbuiten alleen als
de voorspelde nacht-max ≥ NIGHT_INTEREST_C (een warme najaarsnacht telt nog,
een stabiele gestookte winternacht niet). Gaat naar de **groepschat**
(`TELEGRAM_CHAT_GROUP_ID`), net als de weerbriefing — niet naar de privé-chat.
"""

import math
import os
from datetime import datetime, timedelta

import vent_io as vio
import vent_physics as vp
from notify import run_guarded, send_telegram
from shared_const import TZ, format_date_nl

ROOM_ID = "ted"                  # zone-id in house_model.json
WINDOW_ID = "ted_small_window"   # het openbare raampje voor het scenario
DOOR_ID = "ted_stair"            # deur naar het trapgat — 's nachts standaard dicht
WD_KEY = "Ted"                   # sleutel in window_data.json / ROOM_COMFORT
SHADE_ID = "ted_window_shade"    # verduisteringsgordijn vóór ted_window (het grote vaste raam)
SHADE_CLOSE_H = 19               # lokaal uur: gordijn gaat elke avond dicht (vaste bedtijdroutine)

NIGHT_START_H = 21               # nachtvenster: vanavond 21:00 → morgen 08:00
NIGHT_END_H = 8
MARKS = (23, 3, 7)               # uur-punten in het bericht
WARMUP_H = 24.0                  # aanloop-sim zodat de massaknoop equilibreert
ANCHOR_MAX_STALENESS_MIN = 30    # oudere actuele meting → niet vertrouwen, sim-waarde staat
MASS_ANCHOR_TAU_H = 8.0          # uur — dempingstijd waarmee de massaknoop op de gemeten
                                 # kamerlucht wordt geijkt (zie anchor_mass_now)
SEASON_MONTHS = range(5, 10)     # mei–sep: altijd sturen
NIGHT_INTEREST_C = 19.0          # daarbuiten: alleen bij een warme nacht

# Standaard peuter-slaapzaktabel op het voorspelde nachtgemiddelde:
# (ondergrens °C, tog, kleding) — eerste rij waarvan de grens gehaald wordt wint.
TOG_TABLE = [
    (24.0, "0.5 tog", "korte pyjama of alleen romper"),
    (21.0, "1.0 tog", "korte pyjama"),
    (18.0, "2.5 tog", "lange pyjama"),
    (16.0, "2.5 tog", "warme pyjama + romper"),
    (None, "3.5 tog", "warme pyjama + romper"),
]


# ── Pure hulpfuncties ─────────────────────────────────────────────────────────────────

def hours_until_morning(now: datetime, end_h: int = NIGHT_END_H) -> float:
    """Uren van nu tot morgen `end_h`:00 lokale tijd (de sim-horizon)."""
    target = (now + timedelta(days=1)).replace(hour=end_h, minute=0,
                                               second=0, microsecond=0)
    return (target - now).total_seconds() / 3600.0


def apply_shade_routine(timeline: list[dict], close_h: int = SHADE_CLOSE_H,
                        end_h: int = NIGHT_END_H) -> list[dict]:
    """Kopie van de timeline waarin `SHADE_ID` (Teds verduisteringsgordijn) van `close_h`
    lokale tijd tot `end_h` de volgende ochtend op 'dicht' staat — een vaste bedtijdroutine
    (elke avond dicht bij het slapengaan), niet afhankelijk van wat de openingen-log op dat
    moment toevallig meldt. Toegepast op zowel de aanloop- als de forecast-fase: zonder dit
    rekent de aanloopsim een avond met een in werkelijkheid al dicht gordijn alsnog als open
    door, wat de (niet-geankerde) massaknoop een structurele warm-bias meegeeft die de hele
    nachtvoorspelling verder optilt. Overdag/vóór `close_h` blijft de gemelde stand gelden."""
    return [({**step, "states": {**step["states"], SHADE_ID: "dicht"}}
             if step["t"].hour >= close_h or step["t"].hour < end_h else step)
            for step in timeline]


def scenario_timeline(timeline: list[dict], now: datetime, state: str) -> list[dict]:
    """Kopie van de timeline waarin het raampje vanaf nu op `state` staat én de deur
    naar het trapgat vanaf nu dicht (de aanname voor een échte nacht, in beide
    scenario's — alleen het raampje varieert); het verleden (de gemelde log) blijft
    onaangeroerd, het origineel wordt niet gemuteerd."""
    return [({**step, "states": {**step["states"], WINDOW_ID: state, DOOR_ID: "dicht"}}
             if step["t"] >= now else step)
            for step in timeline]


def all_open_timeline(timeline: list[dict], house: dict, now: datetime) -> list[dict]:
    """Kopie van de timeline waarin vanaf nu élk raam én élke deur in het huis open
    staat (incl. `ted_stair`) — het meest gunstige doorwaai-scenario, puur informatief
    (geen advies om alles ook echt open te zetten). Ramen zonder bewegend deel
    (`max_open_area_m2` 0) blijven fysiek dicht, ook al krijgen ze de "open"-state — de
    sim rekent er dan gewoon geen oppervlak voor. Het verleden (de gemelde log) blijft
    onaangeroerd, het origineel wordt niet gemuteerd."""
    override = {eid: "open" for eid in list(house.get("windows", {}))
               + list(house.get("doors", {}))}
    return [({**step, "states": {**step["states"], **override}}
             if step["t"] >= now else step)
            for step in timeline]


def anchor_now(ta_now: dict, actual: dict, now: datetime,
              max_staleness_min: float = ANCHOR_MAX_STALENESS_MIN) -> dict:
    """Corrigeer de blind-gesimuleerde "nu"-luchttemp per zone (`ta_now`) met de meest
    recente échte tado-meting uit `actual` (collect_actual-vorm: {zone: [(t, °C), ...]},
    oplopend gesorteerd), mits die meting binnen `max_staleness_min` van `now` valt.
    Oudere of ontbrekende metingen laten de gesimuleerde waarde ongemoeid (fail open).
    Kopie — `ta_now` wordt niet gemuteerd."""
    corrected = dict(ta_now)
    for rid, samples in actual.items():
        if not samples:
            continue
        ts, temp = samples[-1]
        if (now - ts).total_seconds() / 60.0 <= max_staleness_min:
            corrected[rid] = temp
    return corrected


def anchor_mass_now(tm_now: dict, actual: dict, now: datetime,
                    tau_h: float = MASS_ANCHOR_TAU_H) -> dict:
    """Ijk de massaknoop op de gemeten kamerlucht, net zoals `anchor_now` dat voor de
    luchtknoop doet.

    De 24u-warmup is een blinde simulatie en drijft weg (gemeten: de luchtknoop stond er
    bij aankomst gemiddeld 0,83 °C naast). `anchor_now` repareert de lucht, maar de massa
    hield die drift vast — en omdat de massaknoop veruit de meeste capaciteit heeft, trok
    hij de geijkte lucht er binnen een paar uur weer doorheen. Dat maakte de fout een
    bijna zuivere offset over de hele nacht.

    Schatting = **exponentieel gewogen gemiddelde van de gemeten luchttemp** over het
    warmup-venster (tijdconstante `tau_h`): de trage massaknoop ís fysisch een gedempt
    gemiddelde van de kamerlucht (dezelfde motivatie waarmee `airflow_model.main()` zijn
    `tm_seed` uit de metingen haalt), en recent weegt zwaarder dan de rand van het venster.

    Bewust **niet** simpelweg `tm = ta` (de instantane geijkte lucht). Dat past op de
    held-out nachten nóg beter (RMSE 0,64 vs 0,86), maar het is fysisch een overschatting:
    om 18:45 loopt de massa juist áchter op de lucht na een warme dag. Dat het beter past,
    komt doordat een te warme massa de resterende koud-bias van het model wegstreept — en
    twee fouten tegen elkaar wegstrepen is precies wat we hier aan het opruimen zijn.

    Kamers zonder metingen houden hun gesimuleerde waarde (fail open, zoals `anchor_now`).
    Kopie — `tm_now` wordt niet gemuteerd."""
    out = dict(tm_now or {})
    for rid, samples in (actual or {}).items():
        num = den = 0.0
        for ts, temp in samples:
            age_h = (now - ts).total_seconds() / 3600.0
            if age_h < 0:
                continue
            w = math.exp(-age_h / tau_h)
            num += w * temp
            den += w
        if den > 0:
            out[rid] = num / den
    return out


def night_stats(series: list[tuple], now: datetime) -> dict | None:
    """Nachtstatistiek uit een sim-serie [(t, °C)]: temps op de MARKS-uren
    (dichtstbijzijnde rasterpunt), min/max/gemiddelde over het nachtvenster."""
    start = now.replace(hour=NIGHT_START_H, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=1)).replace(hour=NIGHT_END_H, minute=0,
                                            second=0, microsecond=0)
    night = [(t, v) for t, v in series if start <= t <= end]
    if not night:
        return None
    temps = [v for _, v in night]
    stats = {"min": min(temps), "max": max(temps),
             "mean": sum(temps) / len(temps), "marks": {}}
    for hh in MARKS:
        base = now if hh >= NIGHT_START_H else now + timedelta(days=1)
        mark = base.replace(hour=hh, minute=0, second=0, microsecond=0)
        t, v = min(night, key=lambda s: abs((s[0] - mark).total_seconds()))
        if abs((t - mark).total_seconds()) <= 1800:   # ≤ een half uur ernaast
            stats["marks"][hh] = v
    return stats


def tog_advice(night_mean: float) -> tuple[str, str]:
    for floor, tog, clothing in TOG_TABLE:
        if floor is None or night_mean >= floor:
            return tog, clothing
    return TOG_TABLE[-1][1], TOG_TABLE[-1][2]   # pragma: no cover — vangnet


def build_message(now: datetime, inside_now: float | None, out_min: float | None,
                  closed_stats: dict, open_stats: dict, all_open_stats: dict,
                  reported_open: bool) -> str:
    """Het avondbericht. `closed_stats` = het hoofdscenario (deur + raampje
    dicht, rooster open — de aanname voor een echte nacht); `open_stats`
    (raampje open, deur dicht) en `all_open_stats` (raampje + alle ramen/deuren
    in het huis open) zijn puur informatieve vergelijkingen. `reported_open` =
    de huidige gemelde raampje-stand (waarschuwt als die van de aanname
    afwijkt)."""
    d = format_date_nl(now.date())
    lines = [f"🌙 <b>Teds nacht</b> — {d}"]
    ctx = []
    if inside_now is not None:
        ctx.append(f"Nu {inside_now:.1f}° binnen")
    if out_min is not None:
        ctx.append(f"buiten koelt naar {out_min:.0f}°")
    if ctx:
        lines.append(" · ".join(ctx))

    afwijking = (" (raampje staat nu open — voorspelling gaat uit van dicht)"
                if reported_open else "")
    marks = closed_stats["marks"]
    mark_txt = " · ".join(f"{hh:02d}:00 <b>{marks[hh]:.1f}°</b>"
                          for hh in MARKS if hh in marks)
    lines.append(f"\nVoorspelling (deur + raampje dicht, rooster open){afwijking}:")
    lines.append(f"{mark_txt}  (min {closed_stats['min']:.1f}°)")

    o7 = open_stats["marks"].get(7)
    c7 = closed_stats["marks"].get(7)
    if o7 is not None and c7 is not None:
        delta = o7 - c7
        lines.append(f"\n🪟 Raampje ook open zou <b>{delta:+.1f}°</b> schelen om 07:00 "
                     f"({o7:.1f}° i.p.v. {c7:.1f}°)")

    a7 = all_open_stats["marks"].get(7)
    if a7 is not None and c7 is not None:
        delta_all = a7 - c7
        lines.append(f"🏠 Alles open (heel huis) zou <b>{delta_all:+.1f}°</b> schelen om 07:00 "
                     f"({a7:.1f}° i.p.v. {c7:.1f}°)")

    tog, clothing = tog_advice(closed_stats["mean"])
    lines.append(f"\n👶 Slaapzak: <b>{tog} + {clothing}</b> "
                 f"(nachtgemiddeld ~{closed_stats['mean']:.0f}°, deur + raampje dicht)")
    return "\n".join(lines)


def should_send(now: datetime, night_max: float) -> bool:
    if now.month in SEASON_MONTHS:
        return True
    return night_max >= NIGHT_INTEREST_C


# ── Runner ────────────────────────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.now(TZ)
    print(f"[teds-nacht] Start — {now.isoformat()}")

    house = vio.load_house()
    weather = vio.fetch_weather(*vio.house_location(house))
    # Run-context: locatie + buur-/bodem-anker, expliciet doorgegeven (RunContext) — het
    # module-global-rebinden waar dit bestand de naamgever van was ("de night_forecast-les")
    # bestaat in de herbouw niet meer; vergeten is nu een TypeError. make_context legt óók
    # het zomerplafond (NEIGHBOR_SUMMER_CAP) op het buur-anker — het oude rebinden hier
    # deed dat niet, waardoor deze voorspelling op hittegolfdagen met een te warm anker
    # rekende; nu exact dezelfde gekapte curve als de tweeling zelf (cap23_night).
    ctx = vio.make_context(house, weather, now)
    print(f"[buren] party-muur-anker = {ctx.neighbor_temp:.1f} °C · "
          f"bodem-anker = {ctx.ground_temp:.1f} °C")

    log = vio.load_openings_log()
    params = vio.merged_params(house, vio.load_learned())
    wd = vio.load_window_data()

    # ── Fase 1: aanloop (nu−24u → nu), werkelijk weer + werkelijke log, geen scenario ──
    # Laat de massaknoop equilibreren en re-simuleert het etmaal tot nu; `end_h=0.0` zodat
    # het raster exact op `now` eindigt (start = now−WARMUP_H, stap 0.25u → altijd exact).
    om_learned = vio.om_learned_from(wd)
    warmup_tl = vio.build_timeline(house, weather, log, now, WARMUP_H, ctx,
                                   beam_iam=True, end_h=0.0, om_learned=om_learned)
    if not warmup_tl:
        print("[teds-nacht] Geen weerdata → stop.")
        raise SystemExit(1)
    warmup_tl = apply_shade_routine(warmup_tl)

    actual = vio.collect_actual(house, wd, now - timedelta(hours=WARMUP_H))
    warmup_seed = {rid: s[0][1] for rid, s in actual.items() if s}   # oudste meting in het venster
    for rid in house.get("rooms", {}):
        warmup_seed.setdefault(rid, warmup_tl[0]["T_out"])

    warmup_sim = vp.simulate(house, params, warmup_tl, warmup_seed, ctx, snapshot_t=now)
    ta_now = dict(warmup_sim.get("Ta_now", warmup_sim["Ta"]))
    tm_now = warmup_sim.get("Tm_now", warmup_sim["Tm"])

    # ── Anker-correctie: vervang de blind gesimuleerde "nu"-luchttemp door de meest
    # recente échte tado-meting per kamer, mits vers genoeg — anders (stale/ontbrekend)
    # blijft de gesimuleerde waarde staan (fail open, zoals elders in de repo).
    corrected = anchor_now(ta_now, actual, now)
    deltas = {rid: round(v - ta_now[rid], 2) for rid, v in corrected.items()
             if abs(v - ta_now[rid]) > 0.01}
    if deltas:
        print(f"[teds-nacht] anker-correctie (sim → actueel): {deltas}")
    ta_now = corrected

    # …en dezelfde behandeling voor de MASSAknoop. Zonder dit ijkten we alleen de lucht,
    # terwijl de massa 24u aan warmup-drift meedroeg — en de massa heeft veruit de meeste
    # capaciteit, dus die trok de zojuist geijkte lucht binnen een paar uur weer mee omlaag.
    # Dat was veruit de grootste foutbron in deze voorspelling: op 9 held-out nachten
    # RMSE 1,46 → 0,86 °C en de nachtbias van −1,43 naar −0,83. Het verklaart ook waarom
    # de fysica-parameters niets uithaalden (c_mass ×4 verschoof de bias 0,08°): het verlies
    # ging niet naar buiten maar naar een te koude interne massa.
    tm_now = anchor_mass_now(tm_now, actual, now)

    # ── Fase 2: forecast (nu → morgen 08:00), scenario-geforceerd, geseed op het anker ──
    end_h = hours_until_morning(now)
    fcst_tl = vio.build_timeline(house, weather, log, now, 0.0, ctx,
                                 beam_iam=True, end_h=end_h, om_learned=om_learned)
    if not fcst_tl:
        print("[teds-nacht] Geen forecast-data → stop.")
        raise SystemExit(1)
    fcst_tl = apply_shade_routine(fcst_tl)
    print(f"[teds-nacht] forecast t/m {fcst_tl[-1]['t'].isoformat()} (end_h={end_h:.1f})")

    stats = {}
    for state in ("open", "dicht"):
        sim = vp.simulate(house, params, scenario_timeline(fcst_tl, now, state),
                          ta_now, ctx, tm_seed=tm_now)
        stats[state] = night_stats(sim["series"].get(ROOM_ID, []), now)
    sim_all = vp.simulate(house, params, all_open_timeline(fcst_tl, house, now),
                          ta_now, ctx, tm_seed=tm_now)
    stats["all_open"] = night_stats(sim_all["series"].get(ROOM_ID, []), now)
    if not stats["open"] or not stats["dicht"] or not stats["all_open"]:
        print("[teds-nacht] Geen nachtvenster in de sim-serie → stop.")
        raise SystemExit(1)

    closed_stats = stats["dicht"]
    open_stats = stats["open"]
    all_open_stats = stats["all_open"]

    # Huidige gemelde raampje-stand (voor de afwijking-kopregel).
    w = house["windows"][WINDOW_ID]
    rep = vio.openings_at(log, now).get(WINDOW_ID)
    frac = vp._open_frac(rep, w) if rep is not None else vp._default_frac(w, "window")
    reported_open = frac > 0.0

    inside_now = (wd.get("rooms", {}).get(WD_KEY, {}) or {}).get("inside")
    night_out = [s["T_out"] for s in fcst_tl
                 if s["t"] >= now and s.get("T_out") is not None]
    out_min = min(night_out) if night_out else None

    night_max = max(stats["open"]["max"], stats["dicht"]["max"], stats["all_open"]["max"])
    if not should_send(now, night_max):
        print(f"[teds-nacht] buiten seizoen en koele nacht (max {night_max:.1f}°) — stil.")
        return

    msg = build_message(now, inside_now, out_min, closed_stats, open_stats, all_open_stats,
                        reported_open)
    print(msg)
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1, niet verzonden.")
        return
    send_telegram(msg, chat_id=os.getenv("TELEGRAM_CHAT_GROUP_ID"))


if __name__ == "__main__":
    run_guarded(main, "teds-nacht", chat_id=os.getenv("TELEGRAM_CHAT_GROUP_ID"),
               fail_threshold=2)

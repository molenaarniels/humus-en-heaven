"""Teds nachtvoorspelling (Project 10) — hoe warm wordt de slaapkamer vannacht?

Draait 's avonds (orchestrator-doel 18:45, vóór peuter-bedtijd ~19:00) en
voorspelt met de gekalibreerde luchtstroom-tweeling (Project 8) hoe Teds kamer
de nacht doorkomt:

1. **Nachtcurve** — simulate() over de forecast tot morgen 08:00, geseed op de
   werkelijke tado-temps (window_data.json) met 24u aanloop voor de massaknoop
   (het main()-patroon van de tweeling). De `end_h`-parameter van build_timeline
   rekt de vooruitblik op; fetch_weather's forecast_days=2 dekt de horizon ruim.
2. **Raam-scenario** — twee sims: `ted_small_window` open vs dicht vanaf nu
   (toekomstige timeline-stappen krijgen een overschreven states-dict; het
   verleden houdt de gemelde log; `ted_vent` (het rooster) blijft in beide
   ongemoeid open). **Dicht is de aanname voor de echte nacht** — deur en
   raampje gaan 's nachts standaard dicht, alleen het rooster staat open — dus
   dat scenario is het hoofdbericht + de basis voor het slaapzakadvies. Open
   blijft louter een informatieve vergelijking ("zou dit schelen"), geen advies
   om het raampje ook echt open te zetten.
3. **Tog/slaapzak-advies** — het nachtgemiddelde van het dicht-scenario door de
   standaard peuter-slaapzaktabel.

Bewust GEEN WU-verfijning (de sim is forecast-gedreven en de seed komt van
tado — het station voegt hier niets toe en zo blijven de WU-secrets uit deze
workflow) en geen dashboard-artefact (v1 is stateless; alleen Telegram).

Verzend-poort: in het zomerseizoen (mei–sep) elke avond; daarbuiten alleen als
de voorspelde nacht-max ≥ NIGHT_INTEREST_C (een warme najaarsnacht telt nog,
een stabiele gestookte winternacht niet). Gaat naar de **groepschat**
(`TELEGRAM_CHAT_GROUP_ID`), net als de weerbriefing — niet naar de privé-chat.
"""

import os
from datetime import datetime, timedelta

import airflow_model as am
from notify import run_guarded, send_telegram
from shared_const import TZ, format_date_nl

ROOM_ID = "ted"                  # zone-id in house_model.json
WINDOW_ID = "ted_small_window"   # het openbare raampje voor het scenario
WD_KEY = "Ted"                   # sleutel in window_data.json / ROOM_COMFORT

NIGHT_START_H = 21               # nachtvenster: vanavond 21:00 → morgen 08:00
NIGHT_END_H = 8
MARKS = (23, 3, 7)               # uur-punten in het bericht
WARMUP_H = 24.0                  # sim-aanloop zodat de massaknoop equilibreert
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


def scenario_timeline(timeline: list[dict], now: datetime, state: str) -> list[dict]:
    """Kopie van de timeline waarin het raampje vanaf nu op `state` staat; het
    verleden (de gemelde log) blijft onaangeroerd, het origineel wordt niet gemuteerd."""
    return [({**step, "states": {**step["states"], WINDOW_ID: state}}
             if step["t"] >= now else step)
            for step in timeline]


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
                  closed_stats: dict, open_stats: dict,
                  reported_open: bool) -> str:
    """Het avondbericht. `closed_stats` = het hoofdscenario (deur + raampje
    dicht, rooster open — de aanname voor een echte nacht); `open_stats` is
    puur een informatieve vergelijking. `reported_open` = de huidige gemelde
    raampje-stand (waarschuwt als die van de aanname afwijkt)."""
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

    house = am.load_house()
    loc = house.get("location", {})
    am._LAT = loc.get("lat", am._LAT)
    am._LON = loc.get("lon", am._LON)

    weather = am.fetch_weather()
    # Buur-anker rebinden — simulate() leest de module-global (main()-patroon);
    # zonder deze regel blijft het party-muur-anker op de statische default staan.
    am._NEIGHBOR_TEMP = am.neighbor_temp_estimate(weather.get("hourly", []), now)
    print(f"[buren] party-muur-anker = {am._NEIGHBOR_TEMP:.1f} °C")

    log = am.load_openings_log()
    params = am.merged_params(house, am.load_learned())

    end_h = hours_until_morning(now)
    timeline = am.build_timeline(house, weather, log, now, WARMUP_H,
                                 beam_iam=True, end_h=end_h)
    if not timeline:
        print("[teds-nacht] Geen weerdata → stop.")
        raise SystemExit(1)
    print(f"[teds-nacht] timeline t/m {timeline[-1]['t'].isoformat()} (end_h={end_h:.1f})")

    # Seed op de werkelijke tado-temps (het main()-patroon), rest op buiten-temp.
    wd = am.load_window_data()
    actual = am.collect_actual(house, wd, now - timedelta(hours=WARMUP_H))
    seed = {rid: s[0][1] for rid, s in actual.items() if s}
    for rid in house.get("rooms", {}):
        seed.setdefault(rid, timeline[0]["T_out"])

    stats = {}
    for state in ("open", "dicht"):
        sim = am.simulate(house, params, scenario_timeline(timeline, now, state), seed)
        stats[state] = night_stats(sim["series"].get(ROOM_ID, []), now)
    if not stats["open"] or not stats["dicht"]:
        print("[teds-nacht] Geen nachtvenster in de sim-serie → stop.")
        raise SystemExit(1)

    closed_stats = stats["dicht"]
    open_stats = stats["open"]

    # Huidige gemelde raampje-stand (voor de afwijking-kopregel).
    w = house["windows"][WINDOW_ID]
    rep = am.openings_at(log, now).get(WINDOW_ID)
    frac = am._open_frac(rep, w) if rep is not None else am._default_frac(w, "window")
    reported_open = frac > 0.0

    inside_now = (wd.get("rooms", {}).get(WD_KEY, {}) or {}).get("inside")
    night_out = [s["T_out"] for s in timeline
                 if s["t"] >= now and s.get("T_out") is not None]
    out_min = min(night_out) if night_out else None

    night_max = max(stats["open"]["max"], stats["dicht"]["max"])
    if not should_send(now, night_max):
        print(f"[teds-nacht] buiten seizoen en koele nacht (max {night_max:.1f}°) — stil.")
        return

    msg = build_message(now, inside_now, out_min, closed_stats, open_stats,
                        reported_open)
    print(msg)
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1, niet verzonden.")
        return
    send_telegram(msg, chat_id=os.getenv("TELEGRAM_CHAT_GROUP_ID"))


if __name__ == "__main__":
    run_guarded(main, "teds-nacht", chat_id=os.getenv("TELEGRAM_CHAT_GROUP_ID"),
               fail_threshold=2)

"""Weekjournaal (Project 11) — zondagavond-digest over alle pijplijnen.

Eén Telegram-bericht per week (orchestrator-doel zondag 20:00) dat de week
samenvat uit de al-gepubliceerde artefacten in docs/ — puur aggregatie, geen
nieuwe datavergaring:

- 🌱 tuinwater  — regen vs. beregening vs. verdamping + actuele uitputting (data.json)
- 🌾 maaien     — maaibeurten deze week, groei vs. drempel, volgende maaidag (mowing_data.json)
- 🧠 tweeling   — RMSE nu vs. een week terug + skill (airflow_learned.json)
- 🌤️ weer       — Tmax-range, weekneerslag, warmste dag (data.json)
- 📡 station    — bias-samenvatting, alleen als de accuracy-check vers genoeg is

Elke sectie is een pure functie die None teruggeeft als haar artefact ontbreekt
of onbruikbaar is — het journaal degradeert per sectie i.p.v. te falen. Alles
None → geen bericht. Stateless: de guard-job in de workflow dedupt, er is geen
state-bestand. Artefacten worden uit de lokale checkout gelezen (zoals
mowing_advisor data.json leest), met env-pad-overrides voor tests.
"""

import json
import os
from datetime import datetime, timedelta

from notify import run_guarded, send_telegram
from shared_const import TZ, format_date_nl, parse_date

SOIL_DATA_PATH = os.getenv("SOIL_DATA_PATH", "docs/data.json")
MOWING_DATA_PATH = os.getenv("MOWING_DATA_PATH", "docs/mowing_data.json")
LEARNED_PATH = os.getenv("AIRFLOW_LEARNED_PATH", "docs/airflow_learned.json")
ACCURACY_DATA_PATH = os.getenv("ACCURACY_DATA_PATH", "docs/accuracy_data.json")

WEEK_DAYS = 7            # het venster: vandaag −6 t/m vandaag
STATION_MAX_AGE_D = 30.0  # accuracy-check is manual-dispatch → ouder dan dit = sectie weg
RMSE_LOOKBACK_D = 6.5    # "een week geleden"-punt in rmse_history
MAX_LEN = 4000           # defensief onder de Telegram-limiet (4096) blijven

STATE_NL = {"wet": "nat", "moist": "vochtig", "threshold": "op de drempel",
            "dry": "droog"}


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _week_days(soil: dict, today) -> list[dict]:
    """De niet-forecast dagen in het weekvenster [vandaag−6, vandaag]."""
    start = today - timedelta(days=WEEK_DAYS - 1)
    out = []
    for d in soil.get("days", []):
        if d.get("forecast"):
            continue
        try:
            dd = parse_date(d["date"])
        except (KeyError, ValueError):
            continue
        if start <= dd <= today:
            out.append(d)
    return out


# ── Secties (elk: data → regel(s) of None) ────────────────────────────────────────────

def garden_section(soil: dict | None, today) -> str | None:
    if not soil:
        return None
    days = _week_days(soil, today)
    if not days:
        return None
    rain = sum(d.get("precip") or 0.0 for d in days)
    et0 = sum(d.get("ET0") or 0.0 for d in days)
    irr_lawn = sum(d.get("lawn_irrigation") or 0.0 for d in days)
    irr_shrubs = sum(d.get("shrubs_irrigation") or 0.0 for d in days)
    line = (f"🌱 <b>Tuinwater</b>: {rain:.1f} mm regen · "
            f"{irr_lawn:.0f} mm gesproeid (gazon) + {irr_shrubs:.0f} mm (borders) · "
            f"ET {et0:.0f} mm")
    parts = []
    for key, naam in (("lawn_status", "gazon"), ("shrubs_status", "borders")):
        st = soil.get(key) or {}
        if st.get("depletion_pct") is not None:
            state = STATE_NL.get(st.get("state"), st.get("state") or "?")
            parts.append(f"{naam} {st['depletion_pct']:.0f}% uitputting ({state})")
    if parts:
        line += "\n   " + " · ".join(parts)
    return line


def mowing_section(mow: dict | None, today) -> str | None:
    if not mow:
        return None
    start = today - timedelta(days=WEEK_DAYS - 1)
    week_mows = []
    for ds, info in (mow.get("mowings") or {}).items():
        try:
            dd = parse_date(ds)
        except ValueError:
            continue
        if start <= dd <= today:
            week_mows.append((dd, (info or {}).get("length_mm")))
    week_mows.sort()
    if week_mows:
        laatste = week_mows[-1]
        mtxt = (f"{len(week_mows)}× gemaaid (laatst {format_date_nl(laatste[0])}"
                + (f", {laatste[1]} mm)" if laatste[1] else ")"))
    else:
        mtxt = "niet gemaaid"
    accum = mow.get("accum_today")
    thr = (mow.get("params") or {}).get("READY_GU_effective")
    groei = (f"groei {accum:.1f}/{thr:.1f} GU"
             if accum is not None and thr is not None else None)
    if mow.get("dormant"):
        status = "gras slaapt (winterrust)"
    elif mow.get("ready"):
        status = "maairijp!"
    elif mow.get("predicted_next_mow"):
        try:
            status = f"volgende ~{format_date_nl(parse_date(mow['predicted_next_mow']))}"
        except ValueError:
            status = None
    else:
        status = None
    bits = [mtxt] + [b for b in (groei, status) if b]
    return "🌾 <b>Maaien</b>: " + " · ".join(bits)


def twin_section(learned: dict | None, now: datetime) -> str | None:
    hist = (learned or {}).get("rmse_history") or []
    live = [p for p in hist if not p.get("held") and p.get("rmse") is not None]
    if not live:
        return None
    cur = live[-1]
    cutoff = now - timedelta(days=RMSE_LOOKBACK_D)
    week_ago = None
    for p in live:
        try:
            if datetime.fromisoformat(p["t"]) <= cutoff:
                week_ago = p
        except (KeyError, ValueError):
            continue
    if week_ago is None:
        week_ago = live[0]
    line = f"🧠 <b>Tweeling</b>: RMSE {cur['rmse']:.2f}°"
    if week_ago is not cur and week_ago.get("rmse") is not None:
        arrow = "↘" if cur["rmse"] <= week_ago["rmse"] else "↗"
        line += f" (was {week_ago['rmse']:.2f}° {arrow})"
    if cur.get("skill") is not None:
        line += f" · skill {cur['skill']:.2f}"
    return line


def weather_section(soil: dict | None, today) -> str | None:
    if not soil:
        return None
    days = _week_days(soil, today)
    tmaxes = [(d.get("Tmax_corr") if d.get("Tmax_corr") is not None else d.get("Tmax"), d)
              for d in days]
    tmaxes = [(t, d) for t, d in tmaxes if t is not None]
    if not tmaxes:
        return None
    lo = min(t for t, _ in tmaxes)
    hi, hi_day = max(tmaxes, key=lambda x: x[0])
    rain = sum(d.get("precip") or 0.0 for d in days)
    line = f"🌤️ <b>Weer</b>: Tmax {lo:.0f}–{hi:.0f}° · {rain:.1f} mm regen"
    try:
        line += f" · warmste dag {format_date_nl(parse_date(hi_day['date']))}"
    except (KeyError, ValueError):
        pass
    return line


def station_section(acc: dict | None, now: datetime) -> str | None:
    if not acc:
        return None
    try:
        gen = datetime.fromisoformat(acc["generated_at"])
    except (KeyError, ValueError):
        return None
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=now.tzinfo)
    if (now - gen).total_seconds() > STATION_MAX_AGE_D * 86400:
        return None   # verouderde check → liever stil dan misleidend
    overall = acc.get("overall") or {}
    if overall.get("mean_bias") is None:
        return None
    return (f"📡 <b>Station</b>: bias {overall['mean_bias']:+.1f}° over "
            f"{overall.get('n', '?')} uurparen (check {format_date_nl(gen.date())})")


def build_message(sections: list[str | None], today) -> str | None:
    live = [s for s in sections if s]
    if not live:
        return None
    msg = f"📒 <b>Weekjournaal</b> — week t/m {format_date_nl(today)}\n\n" + "\n".join(live)
    if len(msg) > MAX_LEN:   # defensief: ruim onder de Telegram-limiet blijven
        msg = msg[:MAX_LEN] + "…"
    return msg


def main() -> None:
    now = datetime.now(TZ)
    today = now.date()
    print(f"[weekjournaal] Start — {now.isoformat()}")

    soil = _load(SOIL_DATA_PATH)
    mow = _load(MOWING_DATA_PATH)
    learned = _load(LEARNED_PATH)
    acc = _load(ACCURACY_DATA_PATH)

    msg = build_message([
        garden_section(soil, today),
        mowing_section(mow, today),
        twin_section(learned, now),
        weather_section(soil, today),
        station_section(acc, now),
    ], today)
    if msg is None:
        print("[weekjournaal] Geen enkele sectie beschikbaar — geen bericht.")
        return
    print(msg)
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1, niet verzonden.")
        return
    send_telegram(msg)


if __name__ == "__main__":
    run_guarded(main, "weekjournaal")

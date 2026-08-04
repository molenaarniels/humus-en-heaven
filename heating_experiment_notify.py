"""Wekelijks verwarmingsexperiment: welke ochtend-opstookarm draait deze week.

Vervangt de dagelijkse willekeurige nachtsetpoint-suggestie. Twee redenen:

1. **Het nachtsetpoint is geen lever meer.** De UA van het hele huis naar
   buiten is 163 W/K; lager dan 16 °C zetten kost niets omdat het setpoint
   dan nooit bindt, en 1 K warmer aanhouden kost ~€32 per seizoen (150
   nachten × 8 u, 92% ketelrendement, €1,45/m³). De enige realistische
   uitkomst van dat experiment is de bevestiging dat je het niet omhoog moet
   zetten — een eenrichtingsresultaat.
2. **De ochtend-opstook is dat wél.** Een harde blast om 06:00 duwt aanvoer-
   én retourtemperatuur omhoog en kan een condenserende ketel uit
   condensatie trekken; een zachtere, vroegere ramp houdt de retour laag en
   het rendement hoog. Dat is een echt effect van enkele procenten, het is
   onzichtbaar zonder te meten, en de fysica geeft het antwoord niet al weg.

**Waarom wekelijks en niet in blokken van twee maanden.** Twee maandblokken
verschillen per *seizoen*, niet per instelling: januari is kouder dan
november, dus de arm die in januari valt ziet er slechter uit om de verkeerde
reden. Bij wekelijks alterneren zien beide armen hetzelfde weerbereik.

**Waarom de eerste dag afvalt.** Met τ ≈ 23 u draagt het huis zijn toestand
mee over de omschakeling heen: één dag settelt ~65%, twee dagen ~92%. De
omschakeling gebeurt maandagavond, dus dinsdagochtend valt af en woensdag is
de eerste schone ochtend.

De armkeuze is **puur uit de datum afgeleid** (pariteit van het maandag-
ordinaal), niet uit de state. Een gemiste maandag, een handmatige dispatch of
de zomerstop kan de alternatie dus niet uit de pas laten lopen; de state is
enkel het logboek voor de latere analyse (armen koppelen aan gasverbruik +
graaddagen gebeurt buiten dit script).
"""

import json
import os
from datetime import date, timedelta

from notify import run_guarded, send_telegram
from shared_const import format_date_nl, local_today, utc_now_iso

EXPERIMENT = "morning_recovery"

# Vaste starttijd van het comfortblok in de `hard`-arm.
HARD_START = "06:00"

# Het nachtsetpoint is in dit experiment een *constante*, geen variabele — als
# het meeschommelt is de vergelijking geen zuivere vergelijking meer.
NIGHT_SETPOINT = 16.0

ARMS = {
    "early": {
        "label": "slimme start",
        "detail": "zet tado's early start (voorverwarmen) AAN voor de woonkamer — "
        "de ketel mag zelf vroeger en zachter naar het comfortblok toe werken.",
    },
    "hard": {
        "label": "harde start",
        "detail": "zet tado's early start (voorverwarmen) UIT — het comfortblok "
        f"begint hard om {HARD_START}.",
    },
}

# Buiten het stookseizoen valt er niets op te stoken, dus ook niets te meten.
# De alternatie is datum-afgeleid, dus de zomerstop verschuift hem niet.
SEASON_MONTHS = {10, 11, 12, 1, 2, 3, 4}

# Ruim twee winters aan weken; het logboek is klein maar hoeft niet oneindig.
MAX_WEEKS = 200

DEFAULT_STATE_FILE = "heating_experiment_state.json"


# ---------------------------------------------------------------- armkeuze


def monday_of(d: date) -> date:
    """De maandag van de week waar `d` in valt."""
    return d - timedelta(days=d.weekday())


def arm_for_week(d: date) -> str:
    """De arm voor de week van `d` — strikt alternerend, puur uit de datum.

    Pariteit op het *ordinaal van de maandag* (niet op het ISO-weeknummer):
    opeenvolgende maandagen liggen exact 7 dagen uit elkaar, dus `// 7` telt
    gegarandeerd met 1 op. Op het ISO-weeknummer zou een jaar met 53 weken
    twee gelijke armen achter elkaar geven op de jaargrens.
    """
    return "early" if (monday_of(d).toordinal() // 7) % 2 == 0 else "hard"


def week_label(monday: date) -> str:
    """ISO-weeklabel, bv. `2026-W45`."""
    year, week, _ = monday.isocalendar()
    return f"{year}-W{week:02d}"


def analysis_window(monday: date) -> tuple[date, date]:
    """(eerste, laatste) ochtend die voor deze arm meetelt.

    De omschakeling gebeurt maandagavond; de settle-periode van 24 u loopt dan
    tot dinsdagavond, dus dinsdagochtend valt af en woensdag is de eerste
    schone ochtend. De arm loopt door tot het bericht van de vólgende maandag,
    dus die maandagochtend telt nog mee.
    """
    return monday + timedelta(days=2), monday + timedelta(days=7)


def week_entry(monday: date, arm: str) -> dict:
    """Het logboekrecord voor deze week — volledig uit de datum afgeleid."""
    start, end = analysis_window(monday)
    return {
        "week": week_label(monday),
        "arm": arm,
        "switched_at": monday.isoformat(),
        "analyse_from": start.isoformat(),
        "analyse_through": end.isoformat(),
    }


# ---------------------------------------------------------------- logboek


def state_path() -> str:
    return os.environ.get("HEATING_STATE_FILE", DEFAULT_STATE_FILE)


def load_state() -> dict:
    path = state_path()
    if not os.path.exists(path):
        return {"experiment": EXPERIMENT, "weeks": []}
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("experiment", EXPERIMENT)
    state.setdefault("weeks", [])
    return state


def previous_arm(state: dict, monday: date) -> str | None:
    """De arm die vorige week draaide volgens het logboek, of `None`.

    Bewust uit het logboek en niet uit `other_arm()`: bij een koude start (of
    na de zomerstop) is er géén vorige week, en dan is "vorige week draaide de
    harde start" een verzonnen bewering over een week die het experiment niet
    gedraaid heeft.
    """
    label = week_label(monday - timedelta(days=7))
    for w in state.get("weeks", []):
        if w.get("week") == label:
            return w.get("arm")
    return None


def record_week(state: dict, entry: dict) -> dict:
    """Voeg `entry` toe of werk de bestaande week bij (pure functie).

    Upsert op weeklabel, zodat een herhaalde dispatch in dezelfde week geen
    dubbele regel oplevert — de orchestrator en de fallback-cron kunnen elkaar
    overlappen, en de armkeuze is toch datum-afgeleid.
    """
    weeks = [w for w in state.get("weeks", []) if w.get("week") != entry["week"]]
    weeks.append(entry)
    weeks.sort(key=lambda w: w["switched_at"])
    return {**state, "experiment": EXPERIMENT, "weeks": weeks[-MAX_WEEKS:]}


def save_state(state: dict) -> None:
    state = {**state, "last_updated": utc_now_iso()}
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------- bericht


def build_message(monday: date, arm: str, prev_arm: str | None) -> str:
    start, end = analysis_window(monday)
    drop = monday + timedelta(days=1)
    this = ARMS[arm]
    vorige = (
        f"Vorige week draaide de {ARMS[prev_arm]['label']}."
        if prev_arm
        else "Eerste week van het experiment — vanaf nu wisselt het elke maandag."
    )

    return "\n".join(
        [
            f"🔥 *Verwarmingsexperiment — week {week_label(monday)}*",
            "",
            f"Deze week: *{this['label']}*",
            f"→ {this['detail']}",
            "",
            vorige,
            f"Meetdagen: {format_date_nl(start)} t/m {format_date_nl(end)} — "
            f"{format_date_nl(drop)} valt af (het huis draagt na de omschakeling "
            "nog ~24 u zijn oude toestand mee).",
            "",
            f"Nachtsetpoint blijft vast op {NIGHT_SETPOINT:.1f}°C — dat is niet "
            "meer wat we testen.",
        ]
    )


def main():
    today = local_today()
    monday = monday_of(today)
    arm = arm_for_week(today)

    if today.month not in SEASON_MONTHS and os.environ.get("FORCE_SEND") != "1":
        print(f"Buiten het stookseizoen (maand {today.month}) — geen bericht, geen weekregistratie.")
        return

    state = load_state()
    message = build_message(monday, arm, previous_arm(state, monday))
    print(message)

    if os.environ.get("DRY_RUN") == "1":
        # Een testdispatch mag het experimentlogboek niet vervuilen; de
        # armkeuze is datum-afgeleid, dus er valt hier niets te bewaren.
        print("DRY_RUN=1, niet verzonden en state niet weggeschreven.")
        return

    save_state(record_week(state, week_entry(monday, arm)))
    print(f"Week {week_label(monday)} geregistreerd in {state_path()} (arm: {arm}).")

    send_telegram(message, parse_mode="Markdown")
    print("Verzonden naar Telegram.")


if __name__ == "__main__":
    run_guarded(main, "verwarming")

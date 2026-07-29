#!/usr/bin/env python3
"""Eenmalige (her-runbare) seeding van het om_bias-verificatielogboek (Project 6).

`om_bias.py` leert de systematische warm-bias van Open-Meteo uit *forecastverificatie*:
elke kwartierrun legt de forecast voor een uur 6–18u vooruit vast en rekent 'm later af
tegen wat het WU-station toen daadwerkelijk mat. Vanaf nul duurt dat dagen — de
nachtemmer haalt `MIN_SAMPLES` pas na ~4 dagen, en tot dan corrigeert het raamadvies
niet en draaien de tweelingen op de ónbiaste driver.

Dat is niet nodig: de gegevens bestáán al. `docs/window_data.json` wordt elk kwartier
gecommit en draagt per commit zowel de forecast (`forecast[].out_raw`, de ruwe
Open-Meteo-waarde) als een rollend ~48u venster gemeten stationstemperaturen
(`outside_history[]`). Precies de twee kanten van de verificatie. Dit script speelt die
git-historie door **dezelfde productie-functies** (`record_forecast`/`verify_pending`)
en schrijft het resulterende logboek in `docs/window_data.json`, zodat de eerstvolgende
run met een volledig gevuld leerboek start.

Waarom seeden en niet gewoon wachten: zonder seed springt de correctie op dag ~4 van 0
naar ~1,5 °C in één keer — een **stap** in de driver van beide tweelingen, precies waar
twin 1's checkpoint-mechaniek een regressie in ziet (de fallback-lus van juli 2026) en
twin 2 vastgepind staat en 'm niet kan volgen. Met een seed valt die stap samen met de
`PHYSICS_REV`-bump, en start alles schoon in hetzelfde regime.

Zelfde patroon als `tools/twin2_backfill.py`: git-mining van gecommitte artefacten,
idempotent (uren worden op tijdstip gededupliceerd — her-draaien vult alleen aan).

LET OP: vereist een niet-shallow clone (`git fetch --unshallow` lokaal; `fetch-depth: 0`
in een workflow) — een shallow checkout ziet alleen de laatste commits.

    python tools/om_bias_backfill.py [--since 2026-06-20] [--repo .] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

# Repo-root op het pad (zelfde patroon als twin2_backfill.py).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import om_bias  # noqa: E402

WINDOW_DATA_PATH = "docs/window_data.json"


# ── Git-laag (dun, subprocess — twin2_backfill-patroon) ───────────────────────────────

def _git(args: list[str], repo: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, check=True).stdout


def is_shallow(repo: str) -> bool:
    return _git(["rev-parse", "--is-shallow-repository"], repo).strip() == "true"


def list_commits(path: str, repo: str, since: str | None = None) -> list[str]:
    """Chronologische sha's van alle first-parent-commits die `path` raakten."""
    args = ["log", "--first-parent", "--reverse", "--format=%H"]
    if since:
        args.append(f"--since={since}")
    args += ["--", path]
    return _git(args, repo).split()


def show_json(sha: str, path: str, repo: str) -> dict | None:
    try:
        return json.loads(_git(["show", f"{sha}:{path}"], repo))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


# ── Replay ────────────────────────────────────────────────────────────────────────────

def replay(shas: list[str], repo: str) -> tuple[dict, int, datetime | None]:
    """Speel elke gecommitte versie door record_forecast/verify_pending — exact de
    volgorde en de functies die de kwartierrun zelf gebruikt, zodat het geseede logboek
    niet van een organisch gegroeid logboek te onderscheiden is."""
    log: dict = {}
    used = 0
    last: datetime | None = None
    for sha in shas:
        d = show_json(sha, WINDOW_DATA_PATH, repo)
        if not d:
            continue
        try:
            now = datetime.fromisoformat(d["as_of_local"])
        except (KeyError, ValueError, TypeError):
            continue
        fc = [{"dt": datetime.fromisoformat(r["dt"]), "temp": r.get("out_raw")}
              for r in d.get("forecast", []) if r.get("dt")]
        log = om_bias.verify_pending(log, d.get("outside_history", []), now)
        log = om_bias.record_forecast(log, fc, now)
        used += 1
        last = now
    return log, used, last


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--since", default=None,
                    help="git --since (default: alles; het leervenster knipt zelf af)")
    ap.add_argument("--dry-run", action="store_true",
                    help="alleen rapporteren, window_data.json niet aanraken")
    args = ap.parse_args()

    if is_shallow(args.repo):
        print("FOUT: shallow clone — de git-historie ontbreekt. Draai eerst\n"
              "      git fetch --unshallow   (of gebruik fetch-depth: 0 in de workflow)")
        return 1

    shas = list_commits(WINDOW_DATA_PATH, args.repo, args.since)
    if not shas:
        print(f"FOUT: geen commits gevonden die {WINDOW_DATA_PATH} raken.")
        return 1
    print(f"[backfill] {len(shas)} commits van {WINDOW_DATA_PATH} gevonden.")

    log, used, last = replay(shas, args.repo)
    learned = om_bias.learn(log)
    errs, pend = log.get("errors", []), log.get("pending", [])
    print(f"[backfill] {used} versies afgespeeld → {len(errs)} geverifieerde punten "
          f"binnen het {om_bias.LEARN_WINDOW_D}-daagse venster, {len(pend)} openstaand.")
    print(f"[backfill] geleerd: nacht {learned['night']:+.2f}°C (n={learned['n_night']}), "
          f"dag {learned['day']:+.2f}°C (n={learned['n_day']})")

    for name in ("night", "day"):
        if learned[f"n_{name}"] < om_bias.MIN_SAMPLES:
            print(f"[backfill] LET OP: emmer '{name}' onder MIN_SAMPLES "
                  f"({learned[f'n_{name}']} < {om_bias.MIN_SAMPLES}) → correctie blijft 0 "
                  f"tot de live runs 'm aanvullen.")

    if args.dry_run:
        print("[backfill] --dry-run → niets weggeschreven.")
        return 0

    path = os.path.join(args.repo, WINDOW_DATA_PATH)
    try:
        with open(path, encoding="utf-8") as f:
            wd = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"FOUT: {WINDOW_DATA_PATH} niet leesbaar ({e}).")
        return 1

    # Alleen het om_bias-blok aanraken; de rest van het artefact blijft bit-voor-bit staan
    # (het wordt door de kwartierrun beheerd — zie de "never edit manually"-grondregel).
    wd["om_bias"] = {**learned,
                     "updated_at": last.isoformat() if last else None,
                     "window_d": om_bias.LEARN_WINDOW_D,
                     "pending": pend, "errors": errs}
    # Zelfde serialisatie als window_advisor.write_dashboard, anders geeft de eerstvolgende
    # run een diff over het hele bestand i.p.v. alleen dit blok.
    with open(path, "w", encoding="utf-8") as f:
        json.dump(wd, f, indent=2, ensure_ascii=False)
    print(f"[backfill] om_bias-blok geschreven in {WINDOW_DATA_PATH} "
          f"({os.path.getsize(path) / 1024:.0f} kB). Committen en pushen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

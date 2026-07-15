#!/usr/bin/env python3
"""Eenmalige (her-runbare) historie-backfill voor Ventilatie 2 (Project 12).

Reconstrueert de trainingsset `data/twin2_history/<YYYY-MM>.json` uit drie bronnen:

  1. **tado-temps + RH**: de git-historie van `docs/window_data.json` — dat artefact
     wordt sinds ~eind mei 2026 elk kwartier gecommit en draagt per commit een rollend
     ~2,6-daags history-buffer per kamer. Eén commit per kalenderdag volstaat dus voor
     een gatenloze reeks (≥1,5 dag overlap tussen opeenvolgende dagen).
  2. **openingen-log**: de Gist `house_openings.json` (browser-getrimd op ~500
     snapshots — vermoedelijk de hele levensduur) is primair; voor een eventueel
     ouder gat worden de per-run `openings`-standen uit gecommitte
     `docs/airflow_data.json`-versies bemonsterd (~1/uur) en tot volledige-stand-
     snapshots gesynthetiseerd (alleen waar de stand verandert — veilig onder de
     voorwaartse accumulatie van openings_at).
  3. **weer**: het Open-Meteo-archief (Project 7-patroon) via
     airflow2_model.fetch_weather_archive — geen git-mining nodig.

LET OP: vereist een niet-shallow clone (`git fetch --unshallow` lokaal;
`fetch-depth: 0` in de workflow) — een shallow checkout ziet alleen de laatste
commits en het script stopt dan met een duidelijke melding.

Idempotent: samples/snapshots worden op tijdstip gededupliceerd tegen wat al in de
shards staat; her-draaien vult alleen gaten aan. Draai:

    python tools/twin2_backfill.py [--since 2026-05-01] [--out data/twin2_history]
                                   [--skip-weather] [--repo .]

Env: GIST_ID/GIST_TOKEN (openingen-log; zonder = alleen de airflow_data-mining).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

# Repo-root op het pad (zelfde patroon als airflow_diagnostics.py).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import airflow_model as am          # noqa: E402
import airflow2_model as a2         # noqa: E402

TZ = am.TZ
WINDOW_DATA_PATH = "docs/window_data.json"
AIRFLOW_DATA_PATH = "docs/airflow_data.json"
AIRFLOW_SAMPLE_MIN = 55             # minuten — bemonster airflow_data-commits ~1/uur
GAP_WARN_H = 2.0                    # dekking-rapport: gat groter dan dit uur melden


# ── Git-laag (dun, subprocess) ────────────────────────────────────────────────────────

def _git(args: list[str], repo: str) -> str:
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {out.stderr.strip()[:200]}")
    return out.stdout


def is_shallow(repo: str) -> bool:
    return _git(["rev-parse", "--is-shallow-repository"], repo).strip() == "true"


def list_commits(path: str, repo: str, since: str | None = None) -> list[tuple[str, datetime]]:
    """Chronologische (sha, commit-tijd) van alle first-parent-commits die `path` raakten."""
    args = ["log", "--first-parent", "--reverse", "--format=%H|%cI"]
    if since:
        args.append(f"--since={since}")
    args += ["--", path]
    out = []
    for line in _git(args, repo).splitlines():
        try:
            sha, iso = line.split("|", 1)
            out.append((sha, datetime.fromisoformat(iso)))
        except ValueError:
            continue
    return out


def git_show_json(sha: str, path: str, repo: str) -> dict | None:
    try:
        return json.loads(_git(["show", f"{sha}:{path}"], repo))
    except (RuntimeError, json.JSONDecodeError):
        return None


# ── Pure kern (getest zonder git) ─────────────────────────────────────────────────────

def pick_daily(commits: list[tuple[str, datetime]]) -> list[tuple[str, datetime]]:
    """Eén commit per lokale kalenderdag: de láátste van die dag. Het rollende
    ~2,6-daagse buffer per commit geeft dan ≥1,5 dag overlap → geen gaten."""
    by_day: dict = {}
    for sha, dt in commits:
        by_day[dt.astimezone(TZ).date()] = (sha, dt)
    return [by_day[d] for d in sorted(by_day)]


def sample_hourly(commits: list[tuple[str, datetime]],
                  every_min: int = AIRFLOW_SAMPLE_MIN) -> list[tuple[str, datetime]]:
    """Dun een kwartier-cadans-commitreeks uit naar ~één per uur."""
    out: list[tuple[str, datetime]] = []
    for sha, dt in commits:
        if not out or (dt - out[-1][1]) >= timedelta(minutes=every_min):
            out.append((sha, dt))
    return out


def extract_room_samples(wd: dict) -> dict[str, dict[int, tuple]]:
    """{kamernaam: {epoch: (temp, hum, heat)}} uit één window_data.json-versie."""
    out: dict[str, dict[int, tuple]] = {}
    for name, rd in (wd.get("rooms", {}) or {}).items():
        slot = out.setdefault(name, {})
        for s in rd.get("history", []) or []:
            try:
                ts = datetime.fromisoformat(s["t"])
            except (ValueError, TypeError, KeyError):
                continue
            if s.get("temp") is None:
                continue
            slot[int(ts.timestamp())] = (float(s["temp"]),
                                         s.get("hum"), bool(s.get("heat")))
    return out


def merge_samples(into: dict[str, dict[int, tuple]], new: dict[str, dict[int, tuple]]) -> int:
    """Union per (kamer, epoch); bestaande waarden winnen (idempotent). Geeft #nieuw."""
    added = 0
    for name, slot in new.items():
        dst = into.setdefault(name, {})
        for epoch, tup in slot.items():
            if epoch not in dst:
                dst[epoch] = tup
                added += 1
    return added


def airflow_snapshot_states(dash: dict) -> dict | None:
    """De volledige-stand-snapshot uit één airflow_data.json-versie: de voorwaarts-
    geaccumuleerde `openings` + de speciale sleutels (paused, ac_room)."""
    if not isinstance(dash, dict) or "openings" not in dash:
        return None
    states = dict(dash.get("openings") or {})
    states[am.PAUSE_STATE_KEY] = bool(dash.get("paused"))
    states[am.AC_STATE_KEY] = (dash.get("ac") or {}).get("room") or ""
    return states


def reconstruct_openings(gist_log: list[dict],
                         mined: list[tuple[str, dict]]) -> list[dict]:
    """Gist-log primair; de gedolven (iso-t, volledige-stand)-paren vullen alléén de
    periode vóór de vroegste Gist-snapshot, en alleen waar de stand écht verandert
    (volledige-stand-snapshots zijn veilig onder openings_at's accumulatie)."""
    gist = sorted((e for e in gist_log if e.get("t")), key=lambda e: e["t"])
    earliest = gist[0]["t"] if gist else None
    synth = []
    prev = None
    for iso_t, states in sorted(mined):
        if earliest is not None and iso_t >= earliest:
            break
        if states != prev:
            synth.append({"t": iso_t, "states": states})
            prev = states
    return synth + gist


def coverage_report(samples: dict[str, dict[int, tuple]]) -> list[str]:
    """Menselijk leesbaar dekking-rapport per kamer: spanwijdte, samples/dag, gaten."""
    lines = []
    for name in sorted(samples):
        epochs = sorted(samples[name])
        if not epochs:
            continue
        t0 = datetime.fromtimestamp(epochs[0], TZ)
        t1 = datetime.fromtimestamp(epochs[-1], TZ)
        days = max(1.0, (epochs[-1] - epochs[0]) / 86400.0)
        gaps = []
        for a, b in zip(epochs, epochs[1:]):
            if (b - a) > GAP_WARN_H * 3600:
                gaps.append(f"{datetime.fromtimestamp(a, TZ):%m-%d %H:%M}→"
                            f"{datetime.fromtimestamp(b, TZ):%m-%d %H:%M}")
        lines.append(f"  {name:<14} {len(epochs):>6} samples  {t0:%Y-%m-%d} → {t1:%Y-%m-%d}"
                     f"  (~{len(epochs) / days:.0f}/dag)"
                     + (f"  gaten>{GAP_WARN_H:.0f}u: {len(gaps)} ({'; '.join(gaps[:4])}"
                        + ("…" if len(gaps) > 4 else "") + ")" if gaps else ""))
    return lines


# ── Shard-merge (schrijft via airflow2_model's shard-I/O) ─────────────────────────────

def merge_into_shards(samples: dict[str, dict[int, tuple]], openings: list[dict]) -> int:
    """Voeg de gedolven samples + log toe aan de maand-shards (dedupe op tijdstip;
    idempotent). Geeft het aantal nieuw toegevoegde samples."""
    by_month: dict[str, dict] = {}

    def shard_for(month: str) -> dict:
        if month not in by_month:
            by_month[month] = a2._load_shard(month)
        return by_month[month]

    added = 0
    known: dict[tuple, set] = {}   # (maand, kamer) → al-aanwezige epochs
    for name, slot in samples.items():
        for epoch in sorted(slot):
            month = datetime.fromtimestamp(epoch, TZ).strftime("%Y-%m")
            shard = shard_for(month)
            cols = shard["rooms"].setdefault(name, {"ts": [], "temp": [], "hum": [], "heat": []})
            seen = known.setdefault((month, name), set(cols["ts"]))
            if epoch in seen:
                continue
            seen.add(epoch)
            temp, hum, heat = slot[epoch]
            cols["ts"].append(epoch)
            cols["temp"].append(int(round(temp * 10)))
            cols["hum"].append(int(round(hum)) if hum is not None else None)
            cols["heat"].append(1 if heat else 0)
            added += 1
    # Kolommen op tijd sorteren (merge kan out-of-order appends geven).
    for shard in by_month.values():
        for cols in shard["rooms"].values():
            order = sorted(range(len(cols["ts"])), key=lambda i: cols["ts"][i])
            for key in ("ts", "temp", "hum", "heat"):
                cols[key] = [cols[key][i] for i in order]
    for entry in openings:
        try:
            month = datetime.fromisoformat(entry["t"]).strftime("%Y-%m")
        except (ValueError, TypeError, KeyError):
            continue
        shard = shard_for(month)
        known_t = {e.get("t") for e in shard["openings"]}
        if entry["t"] not in known_t:
            shard["openings"].append({"t": entry["t"], "states": entry.get("states", {}) or {}})
    for shard in by_month.values():
        shard["openings"].sort(key=lambda e: e.get("t") or "")
        a2._write_shard(shard)
    return added


# ── Main ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Ventilatie 2 — historie-backfill uit git")
    ap.add_argument("--since", default=None, help="vroegste datum (YYYY-MM-DD), default alles")
    ap.add_argument("--out", default=None, help=f"shard-map (default {a2.HISTORY_DIR})")
    ap.add_argument("--repo", default=".", help="pad naar de git-checkout")
    ap.add_argument("--skip-weather", action="store_true",
                    help="sla de Open-Meteo-archief-verversing over")
    args = ap.parse_args()
    if args.out:
        a2.HISTORY_DIR = args.out

    if is_shallow(args.repo):
        print("FOUT: shallow clone — de git-historie ontbreekt. Draai eerst\n"
              "  git fetch --unshallow\n"
              "of gebruik fetch-depth: 0 in de workflow-checkout.")
        return 1

    house = am.load_house()
    loc = house.get("location", {})
    am._LAT = loc.get("lat", am._LAT)
    am._LON = loc.get("lon", am._LON)

    # 1. tado-samples uit de dagelijkse window_data-commits.
    commits = pick_daily(list_commits(WINDOW_DATA_PATH, args.repo, args.since))
    print(f"[backfill] {len(commits)} dag-commits van {WINDOW_DATA_PATH}.")
    samples: dict[str, dict[int, tuple]] = {}
    for i, (sha, _dt) in enumerate(commits):
        wd = git_show_json(sha, WINDOW_DATA_PATH, args.repo)
        if not wd:
            continue
        merge_samples(samples, extract_room_samples(wd))
        if (i + 1) % 10 == 0:
            print(f"[backfill] … {i + 1}/{len(commits)} commits verwerkt.")

    # 2. Openingen: Gist-log primair, airflow_data-mining als gap-filler ervóór.
    gist_log = am.load_openings_log()
    earliest_gist = min((e["t"] for e in gist_log if e.get("t")), default=None)
    all_epochs = [e for s in samples.values() for e in s]
    data_start = (datetime.fromtimestamp(min(all_epochs), TZ).isoformat()
                  if all_epochs else None)
    mined: list[tuple[str, dict]] = []
    if data_start and (earliest_gist is None or earliest_gist > data_start):
        af_commits = sample_hourly(list_commits(AIRFLOW_DATA_PATH, args.repo, args.since))
        af_commits = [(sha, dt) for sha, dt in af_commits
                      if earliest_gist is None or dt.astimezone(TZ).isoformat() < earliest_gist]
        print(f"[backfill] openingen-gap vóór de Gist-log: {len(af_commits)} "
              f"airflow_data-commits bemonsteren (~1/uur).")
        for sha, dt in af_commits:
            dash = git_show_json(sha, AIRFLOW_DATA_PATH, args.repo)
            states = airflow_snapshot_states(dash) if dash else None
            if states is not None:
                mined.append((dash.get("as_of_local") or dt.astimezone(TZ).isoformat(), states))
    log = reconstruct_openings(gist_log, mined)
    print(f"[backfill] openingen-log: {len(gist_log)} Gist-snapshots + "
          f"{len(log) - len(gist_log)} gesynthetiseerd uit airflow_data.")

    added = merge_into_shards(samples, log)
    print(f"[backfill] {added} nieuwe samples naar {a2.HISTORY_DIR}.")

    # 3. Weer over de volle spanwijdte (incl. batch-warmup-aanloop).
    if not args.skip_weather and all_epochs:
        t_min = datetime.fromtimestamp(min(all_epochs), TZ)
        t_max = datetime.fromtimestamp(max(all_epochs), TZ)
        rows = a2.fetch_weather_archive(
            (t_min - timedelta(hours=a2.BATCH_WARMUP_H + 24)).date(), t_max.date())
        a2.refresh_shard_weather(rows)
        print(f"[backfill] weer ververst: {len(rows)} uur-rijen "
              f"({t_min.date()} → {t_max.date()}).")

    print("[backfill] dekking:")
    for line in coverage_report(samples):
        print(line)
    print("[backfill] klaar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

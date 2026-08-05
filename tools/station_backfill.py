#!/usr/bin/env python3
"""Zaai het stations-archief (data/station_history) met de uren die al in
docs/accuracy_data.json staan.

`station_accuracy.py` haalt per run alleen de laatste `ACCURACY_DAYS` op en
overschreef zijn artefact; alle eerder gekoppelde uren bestonden daardoor nog
maar op één plek: de `scatter` in het gepubliceerde dashboard-JSON. Deze tool
schrijft die het archief in, zodat de reeks bij de volgende run niet bij nul
begint. Rerunnable en idempotent (append_archive dedupliceert op het UTC-uur).

Beperking: de scatter is van vóór de `wu_solar`-export, dus die rijen dragen
geen WU-pyranometer en geen RH — een latere run over dezelfde periode vult ze
alsnog aan. De scatter is bovendien gedownsampled zodra er meer dan
SCATTER_CAP paren zijn (bij de huidige 1438 is step=1, dus compleet).

    python tools/station_backfill.py [pad/naar/accuracy_data.json]
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from station_accuracy import HISTORY_DIR, append_archive, load_archive  # noqa: E402


def rows_from_artifact(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    out = []
    for point in payload.get("scatter", []):
        if point.get("wu") is None or point.get("om") is None:
            continue
        out.append({"t": point["t"], "wu": point["wu"], "om": point["om"],
                    "solar": point.get("solar"), "wu_solar": point.get("wu_solar"),
                    "cloud": point.get("cloud"), "wind": point.get("wind"),
                    "rh": point.get("rh")})
    return out


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "docs/accuracy_data.json"
    rows = rows_from_artifact(path)
    fresh, updated = append_archive(rows)
    print(f"[backfill] {path}: {len(rows)} paren gelezen → "
          f"{fresh} nieuw, {updated} bijgewerkt in {HISTORY_DIR} "
          f"({len(load_archive())} uren totaal)")


if __name__ == "__main__":
    main()

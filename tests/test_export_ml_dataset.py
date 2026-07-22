"""Checks voor de ML-dataset-export (`tools/export_ml_dataset.py`): de pure
omzet-helpers (openingsfractie, nearest-join, kolom-dedup, long→wide-pivot) en
één integratie-run tegen een synthetische shard — alles zónder netwerk of
secrets, met de fysica-baseline uit (snel)."""
import importlib
import json
import os
import sys
from datetime import datetime, timedelta

import airflow_model as am
import airflow2_model as a2


def _ex():
    tools_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    return importlib.import_module("export_ml_dataset")


def _t(day, hour=12, minute=0):
    return datetime(2026, 6, day, hour, minute, tzinfo=am.TZ)


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_openness_mapping():
    ex = _ex()
    assert ex._openness("open", {}) == 1.0
    assert ex._openness("dicht", {}) == 0.0
    assert ex._openness(None, {}) == 0.0
    assert ex._openness("tilt", {"tilt_frac": 0.25}) == 0.25
    assert ex._openness("tilt", {}) == 0.3            # default kier
    assert ex._openness(0.7, {}) == 0.7               # al numeriek
    assert ex._openness(True, {}) == 1.0
    assert ex._openness(5, {}) == 1.0                 # geklemd naar 1


def test_nearest_within_and_outside_tolerance():
    ex = _ex()
    samples = [(_t(1, 12, 0), 20.0), (_t(1, 12, 15), 21.0), (_t(1, 12, 30), 22.0)]
    assert ex._nearest(samples, _t(1, 12, 16), 600) == (_t(1, 12, 15), 21.0)
    # 12:07:30 ligt 7.5 min van beide buren → binnen 10 min, pak de dichtstbijzijnde
    assert ex._nearest(samples, _t(1, 12, 8), 600)[1] == 21.0
    # buiten tolerantie
    assert ex._nearest(samples, _t(1, 13, 0), 600) == (None, None)
    assert ex._nearest([], _t(1, 12, 0), 600) == (None, None)


def test_long_columns_dedup_and_baseline_toggle():
    ex = _ex()
    cols = ex.long_columns(["a", "b"], baseline=True)
    assert len(cols) == len(set(cols))                # geen dubbelen
    assert cols[:3] == ["t", "t_epoch", "room"]
    assert "temp_c" in cols and "pred_twin1_c" in cols and "pred_twin2_c" in cols
    assert "open_a" in cols and "open_b" in cols
    assert "pred_twin1_c" not in ex.long_columns(["a"], baseline=False)


def test_to_wide_pivots_rooms_into_columns():
    ex = _ex()
    long_rows = [
        {"t": "2026-06-01T12:00", "t_epoch": 1, "room": "living", "temp_c": 22.0,
         "humidity": 50, "solar_glass_w": 100, "roof_irr_w": 0, "heating": 0,
         "ac_here": 0, "paused": 0, "ac_room": "", "t_out_c": 18.0},
        {"t": "2026-06-01T12:00", "t_epoch": 1, "room": "office", "temp_c": 24.0,
         "humidity": 45, "solar_glass_w": 200, "roof_irr_w": 50, "heating": 0,
         "ac_here": 0, "paused": 0, "ac_room": "", "t_out_c": 18.0},
    ]
    wide = ex.to_wide(long_rows, ["living", "office"], [], baseline=False)
    assert len(wide) == 1
    row = wide[0]
    assert row["temp_c__living"] == 22.0
    assert row["temp_c__office"] == 24.0
    assert row["t_out_c"] == 18.0                     # gedeeld, één keer
    assert "room" not in row


# ── Integratie tegen een synthetische shard ─────────────────────────────────

def _write_synthetic_shard(path):
    """~3 dagen tado + uur-weer + één openingen-snapshot; genoeg voor 1-daagse
    hergeseed-vensters. Samples op exacte 15-min-markeringen zodat ze op het
    rooster joinen."""
    start = _t(10, 0, 0)
    rooms = {}
    for name in ("Living room", "office"):
        ts, temp, hum, heat = [], [], [], []
        for i in range(3 * 96):                       # 3 dagen × 96 kwartieren
            t = start + timedelta(minutes=15 * i)
            ts.append(int(t.timestamp()))
            temp.append(int(round((21.0 + 2.0 * ((i // 48) % 2)) * 10)))
            hum.append(50)
            heat.append(0)
        rooms[name] = {"ts": ts, "temp": temp, "hum": hum, "heat": heat}
    weather = []
    wstart = start - timedelta(hours=30)              # dekt de 48u venster-aanloop
    for h in range(24 * 5):
        t = wstart + timedelta(hours=h)
        weather.append({
            "dt": t.isoformat(), "T_out": 17.0, "rh": 60, "precip": 0.0,
            "wind_speed": 3.0, "wind_dir": 200, "gust": 6.0,
            "shortwave": 0.0, "direct": 0.0, "diffuse": 0.0})
    openings = [{"t": (start - timedelta(hours=1)).isoformat(),
                 "states": {"living_french": "dicht", "office_window": "open"}}]
    shard = {"schema": 1, "month": "2026-06", "rooms": rooms,
             "weather": weather, "openings": openings}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(shard, f)


def test_build_long_rows_end_to_end(tmp_path, monkeypatch):
    ex = _ex()
    monkeypatch.setattr(a2, "HISTORY_DIR", str(tmp_path))
    _write_synthetic_shard(os.path.join(tmp_path, "2026-06.json"))

    house = am.load_house()
    ds = a2.load_dataset(house)
    assert ds["actual"]                                # rooms herkend via from_window_data

    rows, room_ids, element_ids = ex.build_long_rows(
        house, ds, join_tol_min=10.0, baseline=False, window_d=1.0)

    assert rows, "verwacht rijen uit de synthetische shard"
    assert "living" in room_ids and "office" in room_ids
    # de gelogde elementen komen in de kolommen terecht
    assert "living_french" in element_ids and "office_window" in element_ids
    # doelwaarden aanwezig voor de kamers mét data
    living = [r for r in rows if r["room"] == "living" and r["temp_c"] is not None]
    assert living
    r = living[0]
    assert set(["t", "t_epoch", "temp_c", "humidity", "t_out_c",
                "open_office_window", "solar_glass_w"]).issubset(r)
    assert r["open_office_window"] == 1.0             # gelogd open
    assert r["open_living_french"] == 0.0             # gelogd dicht
    assert "pred_twin1_c" not in r                     # baseline uit

    # long → wide pivot blijft consistent
    wide = ex.to_wide(rows, room_ids, element_ids, baseline=False)
    assert wide and "temp_c__living" in wide[0]

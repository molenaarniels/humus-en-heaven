"""Pure-functie-checks voor de Ventilatie 2-backfill (`tools/twin2_backfill.py`):
commit-selectie, sample-extractie/merge-idempotentie, openingen-reconstructie
(Gist primair, gedolven snapshots alleen vóór het Gist-begin en alleen bij een
standswijziging) en de shard-merge — alles zónder git of netwerk."""
import importlib
import json
import os
import sys
from datetime import datetime, timedelta

import airflow_model as am
import airflow2_model as a2


def _bf():
    tools_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    return importlib.import_module("twin2_backfill")


def _t(day, hour=12, minute=0):
    return datetime(2026, 6, day, hour, minute, tzinfo=am.TZ)


# ── Commit-selectie ──────────────────────────────────────────────────────────────────

def test_pick_daily_last_of_each_day():
    bf = _bf()
    commits = [("a", _t(1, 8)), ("b", _t(1, 23)), ("c", _t(2, 0, 15)), ("d", _t(2, 9))]
    picked = bf.pick_daily(commits)
    assert [sha for sha, _ in picked] == ["b", "d"]


def test_sample_hourly_thins_quarter_cadence():
    bf = _bf()
    commits = [(f"c{i}", _t(1, 0) + timedelta(minutes=15 * i)) for i in range(16)]  # 4 uur
    picked = bf.sample_hourly(commits)
    assert len(picked) == 4
    assert picked[0][0] == "c0"


# ── Sample-extractie + merge ─────────────────────────────────────────────────────────

def _wd(t0, n=4, name="Living room"):
    return {"rooms": {name: {"history": [
        {"t": (t0 + timedelta(minutes=15 * i)).isoformat(),
         "temp": 20.0 + i, "hum": 40 + i, "heat": i % 2} for i in range(n)]}}}


def test_extract_and_merge_idempotent():
    bf = _bf()
    t0 = _t(5)
    s1 = bf.extract_room_samples(_wd(t0))
    assert len(s1["Living room"]) == 4
    merged: dict = {}
    assert bf.merge_samples(merged, s1) == 4
    assert bf.merge_samples(merged, s1) == 0          # tweede keer: niets nieuws
    # Overlappend buffer (zelfde tijden + 2 nieuwe) → alleen de 2 nieuwe erbij.
    s2 = bf.extract_room_samples(_wd(t0 + timedelta(minutes=30), n=4))
    assert bf.merge_samples(merged, s2) == 2
    epoch0 = int(t0.timestamp())
    assert merged["Living room"][epoch0] == (20.0, 40, False)


# ── Openingen-reconstructie ──────────────────────────────────────────────────────────

def test_airflow_snapshot_states_includes_special_keys():
    bf = _bf()
    dash = {"openings": {"a_win": "open"}, "paused": True, "ac": {"room": "ted"}}
    st = bf.airflow_snapshot_states(dash)
    assert st["a_win"] == "open"
    assert st[am.PAUSE_STATE_KEY] is True
    assert st[am.AC_STATE_KEY] == "ted"
    assert bf.airflow_snapshot_states({"weather": {}}) is None   # geen openings-veld


def test_reconstruct_openings_gist_primary_mined_gapfiller():
    bf = _bf()
    gist = [{"t": _t(10).isoformat(), "states": {"a_win": "open"}}]
    mined = [
        (_t(8).isoformat(), {"a_win": "dicht"}),
        (_t(8, 14).isoformat(), {"a_win": "dicht"}),   # ongewijzigd → geen snapshot
        (_t(9).isoformat(), {"a_win": "tilt"}),
        (_t(11).isoformat(), {"a_win": "dicht"}),      # ná het Gist-begin → genegeerd
    ]
    log = bf.reconstruct_openings(gist, mined)
    assert [e["t"] for e in log] == [_t(8).isoformat(), _t(9).isoformat(), _t(10).isoformat()]
    # openings_at accumuleert correct over de gemengde log.
    assert am.openings_at(log, _t(9, 18))["a_win"] == "tilt"
    assert am.openings_at(log, _t(10, 18))["a_win"] == "open"


def test_reconstruct_openings_empty_gist_uses_all_mined():
    bf = _bf()
    mined = [(_t(8).isoformat(), {"x": 1}), (_t(9).isoformat(), {"x": 2})]
    log = bf.reconstruct_openings([], mined)
    assert len(log) == 2


# ── Shard-merge ──────────────────────────────────────────────────────────────────────

def test_merge_into_shards_dedupes_and_sorts(tmp_path, monkeypatch):
    bf = _bf()
    monkeypatch.setattr(a2, "HISTORY_DIR", str(tmp_path))
    t0 = _t(5)
    samples = bf.extract_room_samples(_wd(t0))
    log = [{"t": t0.isoformat(), "states": {"a_win": "open"}}]
    assert bf.merge_into_shards(samples, log) == 4
    assert bf.merge_into_shards(samples, log) == 0    # idempotent
    shard = json.load(open(tmp_path / "2026-06.json"))
    assert shard["rooms"]["Living room"]["ts"] == sorted(shard["rooms"]["Living room"]["ts"])
    assert len(shard["openings"]) == 1
    # De shard is direct consumeerbaar door load_dataset.
    house = {"rooms": {"a": {"from_window_data": "Living room"}}}
    ds = a2.load_dataset(house)
    assert len(ds["actual"]["a"]) == 4
    assert ds["log"][0]["states"] == {"a_win": "open"}


def test_coverage_report_flags_gap():
    bf = _bf()
    t0 = _t(5)
    slot = {int((t0 + timedelta(minutes=15 * i)).timestamp()): (20.0, 50, False)
            for i in range(4)}
    far = t0 + timedelta(hours=10)
    slot[int(far.timestamp())] = (21.0, 50, False)
    lines = bf.coverage_report({"Living room": slot})
    assert len(lines) == 1
    assert "gaten" in lines[0]

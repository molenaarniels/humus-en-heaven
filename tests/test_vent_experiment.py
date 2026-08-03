"""Tests voor de held-out-experimentcampagne (tools/vent_experiment.py) — de opvolger
van tools/twin2_experiment.py voor de herbouwde tweeling (Project 13): lek-vrije
venster-geometrie (elk venster precies één keer held-out), de zon-/middag-plakken,
de deterministische A/A-jitter, het uitbreidbare arm-register, de rapport-weergave
en — nieuw — de term-uit-patch die de eenzijdige ventilatie aantoonbaar uitschakelt.
Alles zónder netwerk of shards."""
from __future__ import annotations

import importlib.util
import math
import os
from datetime import datetime, timedelta

import pytest

import vent_fit as vf
import vent_io as vio
import vent_physics as vp

TZ = vp.TZ

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "vent_experiment", os.path.join(_ROOT, "tools", "vent_experiment.py"))
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)


def _dataset(days: int = 16) -> dict:
    t0 = datetime(2026, 6, 1, 0, 0, tzinfo=TZ)
    samples = [(t0 + timedelta(hours=h), 21.0) for h in range(24 * days)]
    return {"actual": {"a": samples}, "actual_rh": {}, "heat_on": {},
            "weather_rows": [], "log": []}


# ── Venster-geometrie: geen overlap, elk venster precies één keer held-out ──────────

def test_windows_non_overlapping_and_each_held_out_once():
    ds = _dataset(16)
    wins = ex.experiment_windows(ds, ds["actual"], 5.0)
    assert len(wins) == 3
    for (s0, e0), (s1, _e1) in zip(wins, wins[1:]):
        assert e0 <= s1                                   # stride == venster: geen overlap
        assert e0 - s0 == timedelta(days=5)
    # achterwaarts verankerd: het jongste venster eindigt op het jongste sample
    assert wins[-1][1] == max(t for t, _ in ds["actual"]["a"])
    # K-vouws: over alle rotaties samen is élk venster precies één keer held-out
    held = ex.held_indices(len(wins), 3)
    flat = [i for r in held for i in r]
    assert sorted(flat) == list(range(len(wins)))
    assert len(flat) == len(set(flat))
    # ook bij n dat niet deelbaar is door K blijft de verdeling een partitie
    assert ex.held_indices(5, 3) == [[0, 3], [1, 4], [2]]


def test_windows_without_filtered_samples_fall_out():
    ds = _dataset(16)
    # filter alles weg → geen enkel venster overleeft
    assert ex.experiment_windows(ds, {}, 5.0) == []


# ── Zon-/middag-plakken (geport uit de twin-2-campagne) ─────────────────────────────

def test_solar_series_sums_direct_and_diffuse_and_tolerates_gaps():
    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=TZ)
    tl = [{"t": t0 + timedelta(minutes=15 * i),
           "weather": {"direct": 500.0, "diffuse": 120.0}} for i in range(4)]
    s = ex._solar_series(tl)
    assert [v for _, v in s] == [620.0] * 4
    # nacht / driver-gat: ontbrekende componenten tellen als 0 en crashen niet
    tl2 = [{"t": t0, "weather": {}},
           {"t": t0 + timedelta(minutes=15), "weather": {"direct": None, "diffuse": None}}]
    assert [v for _, v in ex._solar_series(tl2)] == [0.0, 0.0]


def test_sunny_and_midday_slices_are_disjoint_concepts():
    """`zon` selecteert op instraling (ook een heldere ochtend), `mid` puur op klok —
    zo is 'te warm door de zon' te scheiden van 'te warm rond het middaguur'."""
    assert ex.SUNNY_WM2 > 0.0
    assert 0 <= ex.MIDDAY_H[0] < ex.MIDDAY_H[1] <= 24


# ── Deterministische A/A-jitter ──────────────────────────────────────────────────────

def test_jitter_is_deterministic_with_alternating_sign_and_clamped():
    house = {"rooms": {"a": {}, "b": {}}}
    base = vio.default_params(house)
    j1 = ex.jitter_params(base, 0.02)
    assert j1 == ex.jitter_params(base, 0.02)          # geen RNG: twee keer identiek
    rooms = sorted(rid for rid in base if isinstance(base[rid], dict))
    keys = vf._param_keys(rooms)
    v0 = vf.params_to_vec(base, keys)
    v1 = vf.params_to_vec(j1, keys)
    for i, (a, b) in enumerate(zip(v0, v1)):
        lo, hi = vp.BOUNDS[keys[i][1]]
        expected = min(hi, max(lo, a * (1.0 + 0.02 * (1 if i % 2 == 0 else -1))))
        assert b == pytest.approx(expected)
    assert v1[0] > v0[0] and v1[1] < v0[1]             # tekens alterneren echt


# ── Arm-register ─────────────────────────────────────────────────────────────────────

def test_arm_registry_lookup_and_extensibility():
    assert ex.ARMS["baseline"] == {}
    assert ex.ARMS["no_single_sided"] == {"no_single_sided": True}
    # de twee A/A-armen jitteren gespiegeld — hun verschil is de ruisvloer
    assert ex.ARMS["aa_1"]["jitter"] == -ex.ARMS["aa_2"]["jitter"]
    # uitbreidbaar: een toekomstige toggle-sleutel is in apply_config een no-op,
    # zodat een nieuwe arm-entry nooit een bestaande worker laat crashen
    orig = vp.single_sided_fresh
    try:
        ex.apply_config({"toekomstige_term": True})
        assert vp.single_sided_fresh is orig
    finally:
        vp.single_sided_fresh = orig


# ── De term-uit-patch (kern van de §6.2-campagne) ───────────────────────────────────

def test_no_single_sided_patch_zeroes_the_term_in_effective_fresh():
    """`effective_fresh` resolvet `single_sided_fresh` via de module-globals op
    call-time; de arm-patch moet de eenzijdige bijdrage dus aantoonbaar op nul
    zetten zónder vent_physics zelf te wijzigen."""
    house = {"rooms": {"a": {}},
             "windows": {"w": {"room": "a", "max_open_area_m2": 0.5, "tilt_deg": 90.0}}}
    states = {"w": "open"}
    weather = {"wind_speed": 3.0}
    orig = vp.single_sided_fresh
    try:
        before = vp.effective_fresh({"a": 0.001}, house, states, weather, {"a": 24.0}, 18.0)
        assert before["a"] > 0.01        # de eenzijdige term domineert het nano-nettodebiet
        ex.apply_config({"no_single_sided": True})
        after = vp.effective_fresh({"a": 0.001}, house, states, weather, {"a": 24.0}, 18.0)
        assert after == {"a": 0.001}     # exact het netwerk-nettodebiet: term is uit
    finally:
        vp.single_sided_fresh = orig


# ── Determinisme van de rapport-stempel ──────────────────────────────────────────────

def test_default_stamp_comes_from_newest_shard_sample_not_wall_clock():
    t1 = datetime(2026, 7, 3, 12, 15, tzinfo=TZ)
    t2 = datetime(2026, 7, 9, 6, 45, tzinfo=TZ)
    ds = {"actual": {"a": [(t1, 20.0)], "b": [(t2, 21.0)]}}
    assert ex.default_stamp(ds) == "20260709T0645"
    assert ex.default_stamp({"actual": {}}) == "leeg"


# ── Rapport-weergave ─────────────────────────────────────────────────────────────────

def _summary(r5, r48, sun, mid, key_floor=0, total=0, union=None, per_room=None):
    return {"rmse_5d": r5, "rmse_48h": r48, "rmse_sunny": sun, "rmse_midday": mid,
            "n_5d": 100, "n_48h": 80, "n_sunny": 30, "n_midday": 25,
            "per_room": per_room or {}, "per_window": {},
            "railed_total": total, "railed_key_floor": key_floor,
            "railed_secondary": 0, "railed_union": union or []}


def test_report_renders_table_noise_floor_and_rail_counts():
    results = {
        "baseline": {"summary": _summary(1.00, 0.90, 1.40, 1.30, key_floor=3, total=5,
                                         union=["a.ua_env@floor"],
                                         per_room={"a": 0.9, "b": 1.1})},
        "no_single_sided": {"summary": _summary(1.10, 0.95, 1.60, 1.45,
                                                key_floor=6, total=8,
                                                per_room={"a": 1.0, "b": 1.2})},
        "aa_1": {"summary": _summary(1.01, 0.91, 1.42, 1.31)},
        "aa_2": {"summary": _summary(1.02, 0.92, 1.44, 1.33)},
    }
    rep = ex.render_report(results, "20260801T0000", 3, 5.0)
    assert "A/A-ruisvloer" in rep
    # de ruisvloer per plak apart — de zon-plak is luidruchtiger dan het etmaalgemiddelde
    assert "5d 0.01" in rep and "zon 0.02" in rep
    assert "zon°C" in rep and "mid°C" in rep and "kern-rail" in rep
    assert "+0.100" in rep                       # Δ5d van no_single_sided t.o.v. baseline
    assert "a.ua_env@floor" in rep               # de rail-detailregel
    assert "per-kamer RMSE" in rep


def test_report_survives_missing_metrics_and_absent_baseline():
    """Een arm zonder zonnige samples (None) toont '—' i.p.v. te crashen; zonder
    baseline-arm zijn er geen delta's maar rendert de tabel gewoon."""
    results = {"no_single_sided": {"summary": _summary(0.9, None, None, None)}}
    rep = ex.render_report(results, "s", 3, 5.0)
    assert "—" in rep
    assert "no_single_sided" in rep


# ── Contract van evaluate ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["rmse_sunny", "rmse_midday", "n_sunny", "n_midday",
                                 "rmse_5d", "rmse_48h"])
def test_evaluate_publishes_slice_keys_even_without_held_windows(key):
    """Contract: `evaluate` publiceert de metriek-sleutels altijd — anders zou het
    rapport per arm moeten raden of ze bestaan."""
    house = {"rooms": {}, "windows": {}, "doors": {}, "vents": {}}
    ds = {"actual": {}, "weather_rows": [], "log": [], "heat_on": {}}
    assert key in ex.evaluate(house, ds, {}, [], {}, None)


def test_summarize_pools_rotations_and_counts_key_floor_rails():
    ev1 = {"pools": {"5d": [4.0, 4], "48h": [1.0, 1], "sunny": [0.0, 0], "midday": [0.0, 0]},
           "per_room": {"a": [4.0, 4]}, "per_window": {"2026-06-06": {"rmse": 1.0, "n": 4}}}
    ev2 = {"pools": {"5d": [12.0, 4], "48h": [0.0, 0], "sunny": [2.0, 2], "midday": [0.0, 0]},
           "per_room": {"a": [12.0, 4]}, "per_window": {"2026-06-11": {"rmse": 1.7, "n": 4}}}
    rots = [{"eval": ev1, "railed": ["a.ua_env@floor", "global.vent_eff@ceil"]},
            {"eval": ev2, "railed": ["a.f_air@floor", "a.solar_gain@ceil",
                                     "a.ua_roof@ceil"]}]
    s = ex.summarize(rots)
    assert s["rmse_5d"] == pytest.approx(math.sqrt(16.0 / 8), abs=1e-4)
    assert s["n_5d"] == 8
    assert s["railed_total"] == 5
    # kern-floor: alléén ua_env/ua_party/f_air op hun VLOER (ua_roof@ceil telt niet mee)
    assert s["railed_key_floor"] == 2
    # secundair: vent_eff/solar_gain/c_mass, elke kant
    assert s["railed_secondary"] == 2
    assert set(s["per_window"]) == {"2026-06-06", "2026-06-11"}

"""Tests voor de experiment-harnas (tools/twin2_experiment.py) — specifiek de
zon-/middag-plakken die de acceptatiepoort voor elke modelwijziging zijn."""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta

import pytest

import airflow_model as am

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "twin2_experiment", os.path.join(_ROOT, "tools", "twin2_experiment.py"))
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)


def _timeline(t0: datetime, n: int, direct: float, diffuse: float) -> list[dict]:
    return [{"t": t0 + timedelta(minutes=15 * i),
             "weather": {"direct": direct, "diffuse": diffuse}} for i in range(n)]


def test_solar_series_sums_direct_and_diffuse():
    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=am.TZ)
    s = ex._solar_series(_timeline(t0, 4, 500.0, 120.0))
    assert [v for _, v in s] == [620.0] * 4
    assert [t for t, _ in s] == [t0 + timedelta(minutes=15 * i) for i in range(4)]


def test_solar_series_tolerates_missing_components():
    """Een tijdlijnstap zonder zon-componenten (nacht, of een gat in de drivers) telt
    als 0 W/m² en mag de plak niet laten crashen."""
    t0 = datetime(2026, 7, 10, 3, 0, tzinfo=am.TZ)
    tl = [{"t": t0, "weather": {}}, {"t": t0 + timedelta(minutes=15),
                                     "weather": {"direct": None, "diffuse": None}}]
    assert [v for _, v in ex._solar_series(tl)] == [0.0, 0.0]


def test_sunny_and_midday_thresholds_are_disjoint_concepts():
    """De twee plakken snijden bewust anders: `zon` selecteert op instraling (dus ook een
    heldere ochtend of late middag), `mid` puur op klok. Een zonnige dag scoort in beide,
    een bewolkte middag alléén in `mid` — zo is 'te warm door de zon' te scheiden van
    'te warm rond het middaguur'."""
    assert ex.SUNNY_WM2 > 0.0
    assert ex.MIDDAY_H[0] < ex.MIDDAY_H[1] <= 24


def _arm_result(arm: str, rmse5: float, sunny: float, midday: float) -> dict:
    return {"arm": arm, "cfg": {}, "epochs_budget": 0, "holdout_offset": 2,
            "held_idx": [2], "n_train": 2,
            "fit": {"epochs": 1, "accepted": 1, "converged": True,
                    "rmse_batch": 0.9, "rmse_rh_batch": 4.0, "samples": 100},
            "eval0": {}, "railed": [], "params": {}, "wall_s": 1.0,
            "eval": {"rmse_5d": rmse5, "rmse_rh_5d": 4.0, "rmse_48h": rmse5,
                     "rmse_sunny": sunny, "rmse_midday": midday,
                     "n_5d": 100, "n_48h": 80, "n_sunny": 30, "n_midday": 25,
                     "per_window": {"2026-07-10": {"rmse": rmse5, "n": 100}},
                     "per_room": {}}}


def test_report_renders_slices_and_noise_floor(tmp_path, capsys):
    for name, r5, sun, mid in [("baseline", 1.00, 1.40, 1.30),
                               ("aa_jitter_1", 1.01, 1.42, 1.31),
                               ("aa_jitter_2", 1.02, 1.44, 1.33)]:
        with open(tmp_path / f"{name}.off2.json", "w", encoding="utf-8") as f:
            json.dump(_arm_result(name, r5, sun, mid), f)
    ex.report(str(tmp_path), 2)
    out = capsys.readouterr().out
    assert "zon°C" in out and "mid°C" in out
    assert "A/A-ruisvloer" in out
    # De ruisvloer wordt per plak apart gerapporteerd — de zon-plak is luidruchtiger
    # dan het etmaalgemiddelde, dus één gedeelde drempel zou misleiden.
    assert "zon 0.02" in out and "mid 0.02" in out
    assert "zon-plak dekking: 30 van 100" in out


def test_report_survives_an_arm_without_sunny_samples(tmp_path, capsys):
    """Een held venster zonder zonnige uren (winter, of een volledig bewolkt venster)
    geeft rmse_sunny=None; de tabel moet dat als '—' tonen i.p.v. te crashen."""
    base = _arm_result("baseline", 1.0, 1.4, 1.3)
    other = _arm_result("neighbor_cap", 0.9, None, None)
    other["eval"]["n_sunny"] = 0
    for r in (base, other):
        with open(tmp_path / f"{r['arm']}.off2.json", "w", encoding="utf-8") as f:
            json.dump(r, f)
    ex.report(str(tmp_path), 2)
    out = capsys.readouterr().out
    assert "—" in out
    assert "neighbor_cap" in out


def test_report_handles_a_directory_with_no_results(tmp_path, capsys):
    ex.report(str(tmp_path), 2)
    assert "screening" in capsys.readouterr().out


@pytest.mark.parametrize("key", ["rmse_sunny", "rmse_midday", "n_sunny", "n_midday"])
def test_evaluate_returns_the_new_slice_keys(key):
    """Contract: `evaluate` publiceert de plak-sleutels altijd, ook als er geen held
    vensters zijn — anders zou `report` per arm moeten raden of ze bestaan."""
    house = {"rooms": {}, "windows": {}, "doors": {}, "vents": {}}
    assert key in ex.evaluate(house, [], {})

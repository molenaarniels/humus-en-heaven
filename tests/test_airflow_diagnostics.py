"""Tests voor tools/airflow_diagnostics.py — de rail-DRUK, die onderscheidt of de
optimizer tegen een grens beukt of er toevallig naast staat."""
from __future__ import annotations

import importlib.util
import os

import pytest

import airflow_model as am

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "airflow_diagnostics", os.path.join(_ROOT, "tools", "airflow_diagnostics.py"))
diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diag)


def test_pressure_is_zero_at_the_prior():
    """Een parameter waar de data niets over zegt wordt door de ridge exact op zijn prior
    geparkeerd — nul afstand, dus nul druk. Dát is het onschuldige geval."""
    assert diag._rail_pressure("ua_roof", am.PRIORS["ua_roof"],
                               am.BOUNDS["ua_roof"]) == pytest.approx(0.0)


def test_pressure_scales_with_distance_and_ridge_weight():
    lo, hi = am.BOUNDS["solar_gain"]
    prior = am.PRIORS["solar_gain"]
    at_floor = diag._rail_pressure("solar_gain", lo, (lo, hi))
    # solar_gain draagt de zware ridge (6.0) i.p.v. REG_WEIGHT — dezelfde afstand telt
    # daarom zwaarder dan bij een gewone parameter met dezelfde band.
    assert at_floor == pytest.approx(am.REG_WEIGHT_BY_PARAM["solar_gain"]
                                     * abs(lo - prior) / (hi - lo))
    halfway = diag._rail_pressure("solar_gain", (prior + lo) / 2.0, (lo, hi))
    assert 0.0 < halfway < at_floor


def test_pressure_follows_the_regime_aware_solar_ridge():
    """`solar_gain`'s ridge ramp't terug op een zonnig venster; de druk moet met hetzelfde
    gewicht rekenen als de fit die de waarde produceerde, anders leest een zonnig venster
    kunstmatig als 'harder duwen'."""
    lo, hi = am.BOUNDS["solar_gain"]
    dull = diag._rail_pressure("solar_gain", lo, (lo, hi), solar_mean=0.0)
    sunny = diag._rail_pressure("solar_gain", lo, (lo, hi),
                                solar_mean=am.SOLAR_RIDGE_HIGH_WM2 + 50.0)
    assert sunny < dull


def test_pressure_none_for_unknown_param_or_degenerate_bounds():
    assert diag._rail_pressure("niet_bestaand", 1.0, (0.0, 1.0)) is None
    assert diag._rail_pressure("ua_roof", 1.0, (2.0, 2.0)) is None


def test_pressure_cell_marks_only_above_threshold():
    assert "🔨" not in diag._pressure_cell(diag.PRESSURE_HIGH - 0.01)
    assert "🔨" in diag._pressure_cell(diag.PRESSURE_HIGH)
    assert diag._pressure_cell(None) == "—"


def _learned(params: dict) -> dict:
    return {"params": params, "rmse": 0.5, "rmse_history": []}


def test_saturation_report_separates_pinned_from_parked():
    """Twee kamers, beide met een param óp zijn vloer — maar `solar_gain`'s vloer ligt ver
    van zijn prior op een smalle band (hoge druk), terwijl `c_mass`'s vloer er dichtbij ligt
    op een zeer brede band (lage druk). Alleen de eerste is een probleem; dit onderscheid is
    precies waarvoor de druk-kolom bestaat."""
    md = diag.saturation_report(_learned({
        "cp_shelter": am.PRIORS["cp_shelter"], "vent_eff": am.PRIORS["vent_eff"],
        "duwt": {"solar_gain": am.BOUNDS["solar_gain"][0]},    # 0.25 vs prior 1.0, band 2.75
        "parkeert": {"c_mass": am.BOUNDS["c_mass"][0]},        # 0.20 vs prior 1.0, band 9.8
    }))
    assert "2 gerailde parameter(s), waarvan 1 onder hoge druk: duwt.solar_gain" in md
    assert "parkeert.c_mass" not in md.split("**Hoogste druk")[0].split("gerailde")[1]
    assert "Hoogste druk" in md


def test_saturation_report_no_rails_is_reported_cleanly():
    md = diag.saturation_report(_learned({
        "cp_shelter": am.PRIORS["cp_shelter"], "vent_eff": am.PRIORS["vent_eff"],
        "a": {"c_air": am.PRIORS["c_air"]},
    }))
    assert "0 gerailde parameter(s), waarvan 0 onder hoge druk." in md

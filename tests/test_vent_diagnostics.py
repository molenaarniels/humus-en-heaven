"""Tests voor tools/vent_diagnostics.py — de rail-DRUK, die onderscheidt of de
optimizer tegen een grens beukt of er toevallig naast staat, plus de
regime-/residu-rapportsecties (hierheen verhuisd uit test_airflow_model.py bij
de rename airflow_diagnostics → vent_diagnostics)."""
from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta

import pytest

import vent_fit as vf
import vent_physics as vp

TZ = vp.TZ

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "vent_diagnostics", os.path.join(_ROOT, "tools", "vent_diagnostics.py"))
diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diag)


def test_pressure_is_zero_at_the_prior():
    """Een parameter waar de data niets over zegt wordt door de ridge exact op zijn prior
    geparkeerd — nul afstand, dus nul druk. Dát is het onschuldige geval."""
    assert diag._rail_pressure("ua_roof", vp.PRIORS["ua_roof"],
                               vp.BOUNDS["ua_roof"]) == pytest.approx(0.0)


def test_pressure_scales_with_distance_and_ridge_weight():
    lo, hi = vp.BOUNDS["solar_gain"]
    prior = vp.PRIORS["solar_gain"]
    at_floor = diag._rail_pressure("solar_gain", lo, (lo, hi))
    # solar_gain draagt de zware ridge (6.0) i.p.v. REG_WEIGHT — dezelfde afstand telt
    # daarom zwaarder dan bij een gewone parameter met dezelfde band.
    assert at_floor == pytest.approx(vf.REG_WEIGHT_BY_PARAM["solar_gain"]
                                     * abs(lo - prior) / (hi - lo))
    halfway = diag._rail_pressure("solar_gain", (prior + lo) / 2.0, (lo, hi))
    assert 0.0 < halfway < at_floor


def test_pressure_follows_the_regime_aware_solar_ridge():
    """`solar_gain`'s ridge ramp't terug op een zonnig venster; de druk moet met hetzelfde
    gewicht rekenen als de fit die de waarde produceerde, anders leest een zonnig venster
    kunstmatig als 'harder duwen'."""
    lo, hi = vp.BOUNDS["solar_gain"]
    dull = diag._rail_pressure("solar_gain", lo, (lo, hi), solar_mean=0.0)
    sunny = diag._rail_pressure("solar_gain", lo, (lo, hi),
                                solar_mean=vf.SOLAR_RIDGE_HIGH_WM2 + 50.0)
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
        "cp_shelter": vp.PRIORS["cp_shelter"], "vent_eff": vp.PRIORS["vent_eff"],
        "duwt": {"solar_gain": vp.BOUNDS["solar_gain"][0]},    # 0.25 vs prior 1.0, band 2.75
        "parkeert": {"c_mass": vp.BOUNDS["c_mass"][0]},        # 0.20 vs prior 1.0, band 9.8
    }))
    assert "2 gerailde parameter(s), waarvan 1 onder hoge druk: duwt.solar_gain" in md
    assert "parkeert.c_mass" not in md.split("**Hoogste druk")[0].split("gerailde")[1]
    assert "Hoogste druk" in md


def test_saturation_report_no_rails_is_reported_cleanly():
    md = diag.saturation_report(_learned({
        "cp_shelter": vp.PRIORS["cp_shelter"], "vent_eff": vp.PRIORS["vent_eff"],
        "a": {"c_air": vp.PRIORS["c_air"]},
    }))
    assert "0 gerailde parameter(s), waarvan 0 onder hoge druk." in md


# ── Rapportsecties (verhuisd uit test_airflow_model.py) ──────────────────────────────

def test_diagnostics_regime_filters_since_fellback_and_adds_recent_table():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=TZ)
    hist = []
    for d in range(3, -1, -1):   # 4 dagen, elk 6 punten
        for h in range(6):
            t = now - timedelta(days=d, hours=h)
            hist.append({"t": t.isoformat(), "rmse": 1.0 if d >= 2 else 0.4,
                         "skill": 0.5, "wx": {"tmax": 26.0, "solar_mean": 250}})
    hist.append({"t": now.isoformat(), "rmse": 9.9, "fell_back": True,
                 "wx": {"tmax": 26.0, "solar_mean": 250}})
    rep = diag.regime_report({"rmse_history": hist})
    assert "Volledig venster" in rep
    assert "Laatste 48u" in rep                 # tweede (post-convergentie-)tabel
    assert "9.9" not in rep                     # fell_back-punt uit de bins
    # --since filtert de leerfase weg: alleen de 0.4-punten blijven over.
    rep2 = diag.regime_report({"rmse_history": hist}, since=now - timedelta(days=1, hours=12))
    assert "| 25–28 | 12 | 0.40 |" in rep2


def test_diagnostics_room_residuals_and_per_room_matrix():
    t0 = datetime(2026, 7, 6, 4, 0, tzinfo=TZ)
    mk = lambda h, v: {"t": (t0 + timedelta(hours=h)).isoformat(), "temp": v}  # noqa: E731
    room = {"predicted_series": [mk(0, 20.0), mk(2, 22.0)],
            "actual_series": [mk(0, 20.5), mk(1, 20.5), mk(2, 21.5)]}
    res = diag.room_residuals(room)
    assert [round(d, 2) for _, d in res] == [-0.5, 0.5, 0.5]   # geïnterpoleerd voorspeld
    rep = diag.residual_report({"rooms": {"a": room, "b": room}})
    assert "Bias per uur, per kamer" in rep
    assert "| uur | a | b |" in rep


def test_diagnostics_pearson():
    assert diag._pearson([(0, 0), (1, 1), (2, 2)]) == pytest.approx(1.0)
    assert diag._pearson([(0, 2), (1, 1), (2, 0)]) == pytest.approx(-1.0)
    assert diag._pearson([(1, 1), (1, 2), (1, 3)]) is None       # geen x-variantie
    assert diag._pearson([(0, 0)]) is None                       # te weinig punten


def test_diagnostics_solar_decomp_offline(monkeypatch):
    # De decompositie zelf (residu ↔ per-raam-W-correlatie) offline, met een nep-weerfetch:
    # het ZO-raam moet het middag-residu dragen, het noord-raam (diffuse_only) nauwelijks.
    # De tool leest weer/huis via vent_io (fetch_weather neemt nu expliciet lat/lon).
    import vent_io as vio
    t0 = datetime(2026, 7, 6, 6, 0, tzinfo=TZ)
    rows = [{"dt": t0 + timedelta(hours=h), "direct": 500.0, "diffuse": 100.0}
            for h in range(0, 40)]
    monkeypatch.setattr(vio, "fetch_weather", lambda lat, lon: {"hourly": rows})
    house = {"rooms": {"living": {}},
             "windows": {"zo": {"room": "living", "facade_azimuth_deg": 135.0,
                                "glass_m2": 2.0, "label": "tuindeuren"},
                         "n": {"room": "living", "facade_azimuth_deg": 0.0,
                               "glass_m2": 1.0, "diffuse_only": True, "label": "noord"}}}
    monkeypatch.setattr(vio, "load_house", lambda: house)
    mk = lambda h, v: {"t": (t0 + timedelta(hours=h)).isoformat(), "temp": v}  # noqa: E731
    # Residu piekt rond de middag (uren 4-8 na 06:00 = 10-14u): zon-gedreven fout.
    act = [mk(h, 22.0) for h in range(14)]
    pred = [mk(h, 22.0 + (1.0 if 4 <= h <= 8 else 0.0)) for h in range(14)]
    data = {"rooms": {"living": {"predicted_series": pred, "actual_series": act}},
            "openings": {}}
    rep = diag.solar_decomp_report(data, "living")
    assert "`zo`" in rep and "`n`" in rep and "tuindeuren" in rep
    import re
    corr = {m[0]: float(m[1]) for m in
            re.findall(r"\| `(\w+)` \| \w* \| ([+-][\d.]+|—)", rep) if m[1] != "—"}
    assert corr.get("zo", 0) > 0.3                    # ZO-raam correleert met het residu
    # Noord-raam: vlakke diffuse → geen W-variantie → geen (of veel zwakkere) correlatie.
    assert corr.get("n", 0.0) < corr["zo"]


def test_diagnostics_solar_decomp_survives_network_failure(monkeypatch):
    import vent_io as vio

    def boom(lat, lon):
        raise OSError("proxy dicht")

    monkeypatch.setattr(vio, "fetch_weather", boom)
    t0 = datetime(2026, 7, 6, 6, 0, tzinfo=TZ)
    mk = lambda h, v: {"t": (t0 + timedelta(hours=h)).isoformat(), "temp": v}  # noqa: E731
    series = [mk(h, 22.0) for h in range(10)]
    data = {"rooms": {"living": {"predicted_series": series, "actual_series": series}},
            "openings": {}}
    monkeypatch.setattr(vio, "load_house",
                        lambda: {"rooms": {"living": {}},
                                 "windows": {"w": {"room": "living", "glass_m2": 1.0}}})
    rep = diag.solar_decomp_report(data, "living")
    assert "niet bereikbaar" in rep                    # nette sectie i.p.v. traceback

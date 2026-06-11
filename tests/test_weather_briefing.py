"""Tests voor de pure logica van de weerbriefing (Project 2) + wu_bias.

UV-wolkencorrectie (Josefsson & Landelius CMF), uv_windows-interpolatie,
thuis-detectie en de WU-stralingsbiascorrectie. Geen netwerk.
"""

from datetime import datetime

import pytest

import weather_briefing as wb
import wu_bias
from weather_briefing import (cloud_corrected_uv, haversine_km, hours_in_window,
                              is_home, uv_windows)


# ── cloud_corrected_uv ─────────────────────────────────────────────────────────

def test_cmf_heldere_hemel_onveranderd():
    assert cloud_corrected_uv(6.0, 0.0) == pytest.approx(6.0)


def test_cmf_dichte_bewolking_kwart():
    # CMF = 1 − 0.75·(1.0)^3.4 = 0.25 bij 100% bewolking.
    assert cloud_corrected_uv(6.0, 100.0) == pytest.approx(1.5)


def test_cmf_none_passthrough_en_clamp():
    assert cloud_corrected_uv(None, 50.0) is None
    assert cloud_corrected_uv(6.0, None) == 6.0
    # Buiten bereik geklemd: >100% gedraagt zich als 100%.
    assert cloud_corrected_uv(6.0, 150.0) == cloud_corrected_uv(6.0, 100.0)


def test_cmf_monotoon_dalend_met_bewolking():
    uvs = [cloud_corrected_uv(6.0, cc) for cc in (0, 25, 50, 75, 100)]
    assert uvs == sorted(uvs, reverse=True)


# ── uv_windows ─────────────────────────────────────────────────────────────────

def _rows(target, uur_uv):
    return [{"dt": datetime(target.year, target.month, target.day, h), "uv": v}
            for h, v in uur_uv]


def test_uv_window_interpolatie_op_halfuur():
    d = datetime(2026, 6, 10).date()
    # 2→4 stijgt door 3 op 9:30; 4→2 zakt door 3 op 11:30.
    rows = _rows(d, [(8, 0.0), (9, 2.0), (10, 4.0), (11, 4.0), (12, 2.0), (13, 0.0)])
    assert uv_windows(rows, d, threshold=3.0) == [("09:30", "11:30")]


def test_uv_window_korter_dan_15min_vervalt():
    d = datetime(2026, 6, 10).date()
    rows = _rows(d, [(10, 2.0), (11, 3.05), (12, 2.0)])  # raakt de drempel maar net
    assert uv_windows(rows, d, threshold=3.0) == []


def test_uv_window_loopt_tot_einde_dag():
    d = datetime(2026, 6, 10).date()
    rows = _rows(d, [(14, 2.0), (15, 4.0), (16, 4.0)])
    windows = uv_windows(rows, d, threshold=3.0)
    assert len(windows) == 1
    assert windows[0][1] == "16:00"  # open venster sluit op de laatste sample


def test_uv_window_lege_dag():
    d = datetime(2026, 6, 10).date()
    assert uv_windows([], d, threshold=3.0) == []


# ── hours_in_window ────────────────────────────────────────────────────────────

def test_hours_in_window_overlap():
    d = datetime(2026, 6, 10).date()
    rows = _rows(d, [(7, 0), (8, 0), (9, 0), (10, 0)])
    # Venster 08:00–09:00 overlapt de buckets van 08:00 én niet die van 09:00+.
    sel = hours_in_window(rows, d, 8, 0, 9, 0)
    assert [r["dt"].hour for r in sel] == [8]
    # Venster 08:30–09:30 overlapt de buckets van 08:00 en 09:00.
    sel = hours_in_window(rows, d, 8, 30, 9, 30)
    assert [r["dt"].hour for r in sel] == [8, 9]


# ── is_home / haversine ────────────────────────────────────────────────────────

def test_is_home_op_thuislocatie():
    lat, lon = wb.HOME_COORDS
    assert is_home({"lat": lat, "lon": lon}) is True


def test_is_home_op_vakantie():
    lat, lon = wb.HOME_COORDS
    assert is_home({"lat": lat + 0.5, "lon": lon}) is False  # ~55 km noordelijker


def test_haversine_bekende_afstand():
    # Utrecht–Amsterdam hemelsbreed ≈ 35 km.
    assert haversine_km(52.0907, 5.1214, 52.3676, 4.9041) == pytest.approx(35.0, abs=3.0)


# ── wu_bias ────────────────────────────────────────────────────────────────────

def test_correct_temp_none_handling():
    assert wu_bias.correct_temp(None, 500.0) is None
    assert wu_bias.correct_temp(20.0, None) == 20.0


def test_correct_temp_trekt_zonsurplus_af():
    verwacht = 20.0 - wu_bias.SOLAR_BIAS_SLOPE * 500.0
    assert wu_bias.correct_temp(20.0, 500.0) == pytest.approx(verwacht)


def test_correct_temp_negatieve_instraling_geklemd():
    assert wu_bias.correct_temp(20.0, -50.0) == 20.0


def test_bias_estimate():
    assert wu_bias.bias_estimate(None) == 0.0
    assert wu_bias.bias_estimate(200.0) == pytest.approx(wu_bias.SOLAR_BIAS_SLOPE * 200.0)

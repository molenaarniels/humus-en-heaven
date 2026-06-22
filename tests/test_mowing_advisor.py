"""Tests voor de pure groei-/advieslogica van de grasmaai-adviseur (Project 5).

Geen netwerk of state: heat_derate, daily_growth_unit, build_growth_series,
effective_threshold, is_dormant, recommend_length, predict_ready_date.
Asserties tegen de moduleconstanten, niet tegen hardcoded tuningwaarden.
"""

from datetime import date, timedelta

import pytest

import mowing_advisor as ma
from mowing_advisor import (build_growth_series, build_message,
                            daily_growth_unit, effective_threshold, heat_derate,
                            is_dormant, predict_ready_date, recommend_length)


# ── heat_derate ────────────────────────────────────────────────────────────────

def test_heat_derate_stuksgewijs():
    assert heat_derate(None) == 1.0
    assert heat_derate(ma.HEAT_OPT_C) == 1.0
    assert heat_derate(ma.HEAT_OPT_C - 5) == 1.0
    assert heat_derate(ma.HEAT_MAX_C) == ma.HEAT_FLOOR
    assert heat_derate(ma.HEAT_MAX_C + 5) == ma.HEAT_FLOOR
    midden = (ma.HEAT_OPT_C + ma.HEAT_MAX_C) / 2
    assert heat_derate(midden) == pytest.approx((1.0 + ma.HEAT_FLOOR) / 2)


def test_heat_derate_monotoon_dalend():
    waarden = [heat_derate(t) for t in range(int(ma.HEAT_OPT_C), int(ma.HEAT_MAX_C) + 1)]
    assert waarden == sorted(waarden, reverse=True)


# ── daily_growth_unit ──────────────────────────────────────────────────────────

def test_gu_soil_volgt_lawn_t_met_hittedemping():
    assert daily_growth_unit({"lawn_T": 2.0, "Tmax": ma.HEAT_OPT_C}, "soil") == pytest.approx(2.0)
    assert daily_growth_unit({"lawn_T": 2.0, "Tmax": ma.HEAT_MAX_C}, "soil") == pytest.approx(2.0 * ma.HEAT_FLOOR)
    assert daily_growth_unit({"lawn_T": None}, "soil") == 0.0
    assert daily_growth_unit({"lawn_T": -0.5, "Tmax": 20}, "soil") == 0.0  # nooit negatief


def test_gu_gdd_fallback():
    assert daily_growth_unit({"Tmean": ma.GDD_BASE + 10}, "gdd") == pytest.approx(10.0)
    assert daily_growth_unit({"Tmean": ma.GDD_BASE - 2}, "gdd") == 0.0
    # Geen Tmean → (Tmax+Tmin)/2.
    assert daily_growth_unit({"Tmax": 20.0, "Tmin": 10.0}, "gdd") == pytest.approx(15.0 - ma.GDD_BASE)


# ── build_growth_series ────────────────────────────────────────────────────────

def _dagen(n, start=date(2026, 6, 1), lawn_t=1.0):
    return [{"date": (start + timedelta(days=i)).isoformat(),
             "lawn_T": lawn_t, "Tmax": 20.0, "forecast": False}
            for i in range(n)]


def test_accumulator_reset_op_maaidag():
    days = _dagen(5)
    maaidag = days[2]["date"]
    mowings = {maaidag: {"length_mm": 40}}
    serie = build_growth_series(days, mowings, "soil", reset_dates={maaidag})
    assert [s["accum"] for s in serie] == [1.0, 2.0, 0.0, 1.0, 2.0]
    assert serie[2]["is_mow"] is True
    assert serie[2]["length_mm"] == 40
    assert serie[3]["is_mow"] is False


# ── effective_threshold (zelfkalibratie) ───────────────────────────────────────

def test_drempel_default_bij_te_weinig_maaibeurten():
    days = _dagen(30)
    mowings = {days[0]["date"]: {"length_mm": 40}}
    assert effective_threshold(mowings, days, "soil") == (ma.READY_GU, False)


def _mows_elke(interval, aantal, days):
    return {days[i * interval]["date"]: {"length_mm": 40}
            for i in range(aantal)}


def test_drempel_leert_mediaan_van_intervallen():
    # gu=1.0/dag, maaibeurt elke 12 dagen → intervaltotalen ≈ 12 → geleerde drempel 12.
    days = _dagen(70)
    mowings = _mows_elke(12, ma.CALIBRATE_MIN_MOWS + 1, days)
    geleerd, gekalibreerd = effective_threshold(mowings, days, "soil")
    assert gekalibreerd is True
    assert geleerd == pytest.approx(12.0)


def test_drempel_clamp():
    lo, hi = ma.CALIBRATE_CLAMP
    # Heel lange intervallen (30 GU) → geklemd op de bovengrens.
    days = _dagen(160)
    mowings = _mows_elke(30, ma.CALIBRATE_MIN_MOWS + 1, days)
    geleerd, gekalibreerd = effective_threshold(mowings, days, "soil")
    assert gekalibreerd is True
    assert geleerd == hi


# ── is_dormant ─────────────────────────────────────────────────────────────────

def test_dormancy_op_7daags_gemiddelde():
    laag = [{"gu": ma.DORMANT_GU_PER_DAY / 2} for _ in range(10)]
    hoog = [{"gu": ma.DORMANT_GU_PER_DAY * 3} for _ in range(10)]
    assert is_dormant(laag, today_idx=9) is True
    assert is_dormant(hoog, today_idx=9) is False
    assert is_dormant([], today_idx=0) is False


# ── recommend_length ───────────────────────────────────────────────────────────

def _weerdagen(tmax, depletion, n=None):
    n = n or ma.LENGTH_WINDOW_DAYS
    return [{"Tmax": tmax, "lawn_depletion": depletion} for _ in range(n)]


def test_hoogte_hitte_op_komst():
    days = _weerdagen(ma.HOT_TMAX_C + 1, 20.0)
    advies = recommend_length(days, 0, date(2026, 6, 15))
    assert advies["length_mm"] == ma.LEN_TALL


def test_hoogte_droogte_op_komst():
    days = _weerdagen(ma.HOT_TMAX_C - 5, ma.DROUGHT_DEPLETION_PCT + 5)
    advies = recommend_length(days, 0, date(2026, 6, 15))
    assert advies["length_mm"] == ma.LEN_TALL


def test_hoogte_koel_vochtig_groeiseizoen_mag_kort():
    days = _weerdagen(ma.COOL_TMAX_C - 1, ma.TIDY_DEPLETION_PCT - 10)
    assert recommend_length(days, 0, date(2026, 6, 15))["length_mm"] == ma.LEN_SHORT
    # Zelfde weer buiten het groeiseizoen → niet kort.
    assert recommend_length(days, 0, date(2026, 1, 15))["length_mm"] == ma.LEN_MID


def test_hoogte_default_midden():
    days = _weerdagen((ma.COOL_TMAX_C + ma.HOT_TMAX_C) / 2, ma.TIDY_DEPLETION_PCT + 5)
    assert recommend_length(days, 0, date(2026, 6, 15))["length_mm"] == ma.LEN_MID


# ── predict_ready_date ─────────────────────────────────────────────────────────

def test_predict_ready_date():
    serie = [{"date": f"2026-06-{10 + i:02d}", "accum": float(i)} for i in range(6)]
    assert predict_ready_date(serie, 0, threshold=3.0) == "2026-06-13"
    assert predict_ready_date(serie, 0, threshold=99.0) is None


# ── "bijna maairijp" voorsprong (LEAD_GU) ───────────────────────────────────────

def test_lead_gu_is_zinvolle_voorsprong():
    # Een positieve voorsprong, kleiner dan de drempel zelf, anders heeft het
    # "bijna"-seintje geen betekenis.
    assert 0 < ma.LEAD_GU < ma.READY_GU


def test_soon_bericht_noemt_beste_dag_en_hoogte():
    optimal = {"date": "2026-06-22", "reason": "droog, 21°C",
               "overgrown": False, "is_today": False}
    length = {"length_mm": ma.LEN_MID, "reason": "veilige middenstand"}
    msg = build_message("soon", date(2026, 6, 14), date(2026, 6, 20),
                        {"precip": 0.0, "Tmax": 21.0}, optimal, length, "soil",
                        predicted="2026-06-23")
    assert "bijna maairijp" in msg.lower()
    assert "22 jun" in msg                    # eerstvolgende goede maaidag
    assert f"{ma.LEN_MID}mm" in msg           # hoogte-advies blijft meegestuurd

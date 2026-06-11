"""Tests voor de pure beslislogica van de raam-adviseur (Project 6).

Alleen functies zonder I/O: humidity_offset, convert_rh, open_desire, decide,
room_trend, next_reopen. Asserties pinnen de *vorm* van de logica, tegen de
moduleconstanten aan — niet tegen hardcoded tuningwaarden (die worden bewust
af en toe geretuned, vgl. tests/test_soil_model.py).
"""

from datetime import datetime, timedelta

import pytest

import window_advisor as wa
from window_advisor import (convert_rh, decide, humidity_offset, next_reopen,
                            open_desire, room_trend)

LOW, HIGH = 19.5, 22.0  # voorbeeldcomfortband (Living room-achtig)


# ── humidity_offset ────────────────────────────────────────────────────────────

def test_humidity_offset_neutraal():
    assert humidity_offset(None) == 0.0
    assert humidity_offset(wa.RH_COMFORT) == pytest.approx(0.0)


def test_humidity_offset_clamps_asymmetrisch():
    # Muf → straf, geclamped op RH_PENALTY_MAX; droog → kleine bonus, op RH_BONUS_MAX.
    assert humidity_offset(100.0) == pytest.approx(wa.RH_PENALTY_MAX)
    assert humidity_offset(0.0) == pytest.approx(-wa.RH_BONUS_MAX)
    assert wa.RH_PENALTY_MAX > wa.RH_BONUS_MAX  # bewust asymmetrisch


def test_humidity_offset_monotoon():
    assert humidity_offset(wa.RH_COMFORT + 5) > 0 > humidity_offset(wa.RH_COMFORT - 5)


# ── convert_rh (Magnus/Tetens) ─────────────────────────────────────────────────

def test_convert_rh_identiteit_bij_gelijke_temp():
    assert convert_rh(60.0, 18.0, 18.0) == pytest.approx(60.0)


def test_convert_rh_warmere_kamer_verlaagt_rh():
    # Zelfde absolute vocht, warmere lucht → lagere relatieve vochtigheid.
    assert convert_rh(80.0, 10.0, 20.0) < 80.0


def test_convert_rh_magnus_anker():
    # 10°C/100% naar 20°C ≈ 52–53% (verhouding verzadigingsdampdrukken).
    assert convert_rh(100.0, 10.0, 20.0) == pytest.approx(52.5, abs=1.0)


def test_convert_rh_none_propagatie_en_clamp():
    assert convert_rh(None, 10.0, 20.0) is None
    assert convert_rh(60.0, None, 20.0) is None
    assert convert_rh(60.0, 10.0, None) is None
    assert convert_rh(100.0, 30.0, 10.0) == 100.0  # koudere kamer: geklemd op 100


# ── open_desire ────────────────────────────────────────────────────────────────

def test_open_desire_koeltrigger_met_marge():
    inside = HIGH + 1.0
    assert open_desire(inside, inside - wa.OPEN_MARGIN, LOW, HIGH) is True
    # Buiten net niet koel genoeg → geen open.
    assert open_desire(inside, inside - wa.OPEN_MARGIN + 0.1, LOW, HIGH) is False


def test_open_desire_none_is_dicht():
    assert open_desire(None, 15.0, LOW, HIGH) is False
    assert open_desire(25.0, None, LOW, HIGH) is False


def test_open_desire_hard_veto():
    # Warm genoeg en buiten koel — maar te muf: nooit openen.
    assert open_desire(30.0, 20.0, LOW, HIGH, vent_rh=wa.RH_HARD_CAP) is False


def test_open_desire_muf_verschuift_drempel():
    # Binnen nét boven high: zonder vocht-info open, met muffe buitenlucht niet meer.
    inside = HIGH + 0.5
    outside = inside - wa.OPEN_MARGIN
    assert open_desire(inside, outside, LOW, HIGH) is True
    muf = wa.RH_COMFORT + 10  # straf > 0.5°C bij RH_TEMP_K 0.15
    assert open_desire(inside, outside, LOW, HIGH, vent_rh=muf) is False


def test_open_desire_ontvochtig_trigger():
    binnen_rh = wa.RH_DRYOUT_MIN + 5
    droog = binnen_rh - wa.RH_DRYOUT_MARGIN
    # Niet warm (binnen de band), maar muf binnen + duidelijk droger buiten → open.
    args = dict(inside=LOW + 1.0, outside=LOW + 0.5, low=LOW, high=HIGH)
    assert open_desire(**args, vent_rh=droog, humidity=binnen_rh) is True
    # Elke voorwaarde die wegvalt → geen open:
    assert open_desire(**args, vent_rh=droog, humidity=wa.RH_DRYOUT_MIN - 1) is False  # binnen niet muf
    assert open_desire(**args, vent_rh=binnen_rh - wa.RH_DRYOUT_MARGIN + 1,
                       humidity=binnen_rh) is False                                    # buiten niet droog genoeg
    assert open_desire(inside=LOW - 0.5, outside=LOW - 1.0, low=LOW, high=HIGH,
                       vent_rh=droog, humidity=binnen_rh) is False                     # zou overkoelen
    assert open_desire(inside=LOW + 1.0, outside=LOW + 2.0, low=LOW, high=HIGH,
                       vent_rh=droog, humidity=binnen_rh) is False                     # warmte-instroom


# ── decide ─────────────────────────────────────────────────────────────────────

def test_decide_geen_meting_houdt_advies():
    assert decide(None, 15.0, "open", LOW, HIGH) == "open"
    assert decide(21.0, None, "dicht", LOW, HIGH) == "dicht"


def test_decide_dode_band_houdt_advies():
    inside = (LOW + HIGH) / 2
    outside = inside - wa.OPEN_MARGIN - 1.0  # koel buiten, geen warmte-instroom
    assert decide(inside, outside, "open", LOW, HIGH) == "open"
    assert decide(inside, outside, "dicht", LOW, HIGH) == "dicht"


def test_decide_warmte_instroom_sluit():
    inside = (LOW + HIGH) / 2
    assert decide(inside, inside - wa.CLOSE_MARGIN, "open", LOW, HIGH) == "dicht"


def test_decide_overkoeling_vs_koelte_tanken():
    inside = LOW - 0.5
    outside = inside - wa.OPEN_MARGIN - 1.0
    assert decide(inside, outside, "open", LOW, HIGH, bank_cooling=False) == "dicht"
    # Warme dag op komst → koelte blijven tanken zolang buiten kouder is.
    assert decide(inside, outside, "open", LOW, HIGH, bank_cooling=True) == "open"


def test_decide_kort_warmtemoment_houdt_open_raam():
    inside = (LOW + HIGH) / 2
    outside = inside  # warmte-instroom
    assert decide(inside, outside, "open", LOW, HIGH, reopen_soon=True) == "open"
    # Een dicht raam blijft dicht — de onderdrukking geldt alleen voor open ramen.
    assert decide(inside, outside, "dicht", LOW, HIGH, reopen_soon=True) == "dicht"


def test_decide_hard_veto_gaat_voor():
    # Zelfs met reopen_soon en open raam: te muf → dicht.
    assert decide(30.0, 20.0, "open", LOW, HIGH, vent_rh=wa.RH_HARD_CAP,
                  reopen_soon=True) == "dicht"


# ── room_trend ─────────────────────────────────────────────────────────────────

def _history(now, values_per_hour):
    """[(uren_geleden, waarde)] → historielijst zoals in window_data.json."""
    return [{"t": (now - timedelta(hours=h)).isoformat(), "temp": v}
            for h, v in values_per_hour]


def test_room_trend_lineaire_helling():
    now = datetime(2026, 6, 10, 12, 0)
    hist = _history(now, [(2, 20.0), (1, 21.0), (0, 22.0)])
    assert room_trend(hist, now) == pytest.approx(1.0)


def test_room_trend_clamp_en_te_weinig_data():
    now = datetime(2026, 6, 10, 12, 0)
    steil = _history(now, [(1, 10.0), (0, 30.0)])  # +20°C/u
    assert room_trend(steil, now) == pytest.approx(wa.TREND_MAX_SLOPE)
    assert room_trend(_history(now, [(0, 21.0)]), now) is None
    assert room_trend([], now) is None


def test_room_trend_negeert_oude_samples():
    now = datetime(2026, 6, 10, 12, 0)
    # Sample ver buiten TREND_WINDOW_H telt niet mee → te weinig punten → None.
    hist = _history(now, [(wa.TREND_WINDOW_H + 5, 0.0), (0, 21.0)])
    assert room_trend(hist, now) is None


# ── next_reopen ────────────────────────────────────────────────────────────────

def test_next_reopen_eerste_koele_uur():
    now = datetime(2026, 6, 10, 18, 0)
    inside = 24.0
    drempel = inside - wa.OPEN_MARGIN
    hourly = [{"dt": now + timedelta(hours=h), "temp": t}
              for h, t in ((1, drempel + 2.0), (2, drempel + 0.5), (3, drempel - 0.5))]
    assert next_reopen(hourly, inside, now) == now + timedelta(hours=3)


def test_next_reopen_none_als_warm_blijft():
    now = datetime(2026, 6, 10, 18, 0)
    hourly = [{"dt": now + timedelta(hours=h), "temp": 30.0} for h in range(1, 6)]
    assert next_reopen(hourly, 24.0, now) is None

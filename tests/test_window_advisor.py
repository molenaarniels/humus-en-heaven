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
                            open_desire, open_reason, predict_open_intervals,
                            room_trend)

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


# ── parse_heating (tado-verwarmingsstatus) ─────────────────────────────────────
def test_parse_heating_uit_vermogen():
    # Gemeten verwarmingsvermogen is de primaire driver: > 0 → aan.
    assert wa.parse_heating({"activityDataPoints": {"heatingPower": {"percentage": 42.0}}}) == (True, 42.0)
    assert wa.parse_heating({"activityDataPoints": {"heatingPower": {"percentage": 0.0}}}) == (False, 0.0)


def test_parse_heating_fallback_op_power_stand():
    # Zonder heatingPower-datapunt: val terug op de aan/uit-stand + setpoint.
    st_on = {"setting": {"power": "ON", "temperature": {"celsius": 21.0}}}
    st_off = {"setting": {"power": "OFF"}}
    assert wa.parse_heating(st_on) == (True, None)
    assert wa.parse_heating(st_off) == (False, None)
    assert wa.parse_heating({}) == (False, None)


def test_shower_is_sensor_only_room():
    # De badkamer zit als sensor-only kamer in SENSOR_ROOMS maar niet in de advies-ROOMS
    # (raamloos → geen koeladvies/Telegram).
    assert "Shower" in wa.SENSOR_ROOMS
    assert "Shower" not in wa.ROOMS


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


def test_open_desire_koelte_tanken_drempel_op_low():
    # In de dode band (low < binnen ≤ high): zonder warme dag op komst geen nieuwe open-wens,
    # mét warme dag op komst wél — je wacht niet tot de kamer al te warm is.
    inside = (LOW + HIGH) / 2
    outside = inside - wa.OPEN_MARGIN - 1.0
    assert open_desire(inside, outside, LOW, HIGH, bank_cooling=False) is False
    assert open_desire(inside, outside, LOW, HIGH, bank_cooling=True) is True
    # Precies op `low` (niet erboven) tankt nog niet — anders zou het overkoelen.
    assert open_desire(LOW, outside, LOW, HIGH, bank_cooling=True) is False
    # Zonder dat buiten kouder is, ook met bank_cooling geen open-wens.
    assert open_desire(inside, inside, LOW, HIGH, bank_cooling=True) is False


def test_open_desire_frisse_lucht_alleen_met_fresh_air_ok():
    # Niets thermisch op het spel: binnen in de band, buiten niet warmer, droog genoeg.
    inside = (LOW + HIGH) / 2
    outside = inside - 0.1
    assert open_desire(inside, outside, LOW, HIGH, vent_rh=wa.RH_COMFORT - 5,
                       fresh_air_ok=True) is True
    # Standaard (geen expliciete opt-in) blijft dit uit — puur thermische call sites
    # veranderen niet vanzelf mee.
    assert open_desire(inside, outside, LOW, HIGH, vent_rh=wa.RH_COMFORT - 5) is False
    # Muf genoeg (> RH_COMFORT) → geen frisse-lucht-bonus.
    assert open_desire(inside, outside, LOW, HIGH, vent_rh=wa.RH_COMFORT + 5,
                       fresh_air_ok=True) is False
    # Zou overkoelen (op/onder low) → geen frisse-lucht-open.
    assert open_desire(LOW, outside, LOW, HIGH, vent_rh=wa.RH_COMFORT - 5,
                       fresh_air_ok=True) is False
    # Warmte-instroom (buiten warmer dan binnen) → geen frisse-lucht-open.
    assert open_desire(inside, inside + 0.5, LOW, HIGH, vent_rh=wa.RH_COMFORT - 5,
                       fresh_air_ok=True) is False
    # Onbekende vochtigheid → conservatief, geen open (kan mugginess niet uitsluiten).
    assert open_desire(inside, outside, LOW, HIGH, vent_rh=None, fresh_air_ok=True) is False


def test_open_reason_labels():
    inside = HIGH + 1.0
    assert open_reason(inside, inside - wa.OPEN_MARGIN, LOW, HIGH) == "cool"
    bandinside = (LOW + HIGH) / 2
    assert open_reason(bandinside, bandinside - wa.OPEN_MARGIN - 1.0, LOW, HIGH,
                       bank_cooling=True) == "bank"
    binnen_rh = wa.RH_DRYOUT_MIN + 5
    droog = binnen_rh - wa.RH_DRYOUT_MARGIN
    assert open_reason(LOW + 1.0, LOW + 0.5, LOW, HIGH, vent_rh=droog,
                       humidity=binnen_rh) == "dryout"
    assert open_reason(bandinside, bandinside - 0.1, LOW, HIGH,
                       vent_rh=wa.RH_COMFORT - 5, fresh_air_ok=True) == "fresh_air"
    assert open_reason(bandinside, bandinside + 1.0, LOW, HIGH) is None
    assert open_reason(30.0, 20.0, LOW, HIGH, vent_rh=wa.RH_HARD_CAP) is None  # veto


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


def test_decide_koelte_tanken_opent_al_vanaf_dicht():
    # Een dicht raam in de dode band opent proactief zodra er een warme dag aankomt —
    # niet pas als de kamer al boven `high` uitkomt.
    inside = (LOW + HIGH) / 2
    outside = inside - wa.OPEN_MARGIN - 1.0
    assert decide(inside, outside, "dicht", LOW, HIGH, bank_cooling=False) == "dicht"
    assert decide(inside, outside, "dicht", LOW, HIGH, bank_cooling=True) == "open"


def test_decide_frisse_lucht_opent_al_vanaf_dicht():
    # Geen thermische noodzaak, maar ook geen kosten → frisse lucht wint, mits opt-in.
    inside = (LOW + HIGH) / 2
    outside = inside - 0.1
    assert decide(inside, outside, "dicht", LOW, HIGH, vent_rh=wa.RH_COMFORT - 5,
                  fresh_air_ok=True) == "open"
    assert decide(inside, outside, "dicht", LOW, HIGH, vent_rh=wa.RH_COMFORT - 5,
                  fresh_air_ok=False) == "dicht"


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
    # Kwartiercadans (spacing ≤ GAP_BREAK_MIN), +1 °C/uur.
    hist = _history(now, [(0.5, 20.0), (0.25, 20.25), (0, 20.5)])
    assert room_trend(hist, now) == pytest.approx(1.0)


def test_room_trend_clamp_en_te_weinig_data():
    now = datetime(2026, 6, 10, 12, 0)
    steil = _history(now, [(0.25, 10.0), (0, 30.0)])  # +80°C/u → geclamped
    assert room_trend(steil, now) == pytest.approx(wa.TREND_MAX_SLOPE)
    assert room_trend(_history(now, [(0, 21.0)]), now) is None
    assert room_trend([], now) is None


def test_room_trend_negeert_oude_samples():
    now = datetime(2026, 6, 10, 12, 0)
    # Sample ver buiten TREND_WINDOW_H telt niet mee → te weinig punten → None.
    hist = _history(now, [(wa.TREND_WINDOW_H + 5, 0.0), (0, 21.0)])
    assert room_trend(hist, now) is None


def test_room_trend_overslaat_gat():
    now = datetime(2026, 6, 10, 12, 0)
    gap_h = wa.GAP_BREAK_MIN / 60.0
    # Recente aaneengesloten reeks (kwartier) die −1 °C/uur daalt, plus een stale,
    # veel koudere meting vóór een gat > GAP_BREAK_MIN: de fit moet die negeren.
    recent = [(0.5, 21.5), (0.25, 21.25), (0, 21.0)]      # −1 °C/uur
    stale = (0.5 + gap_h + 0.25, 10.0)                    # vóór het gat, nog binnen het venster
    assert room_trend(_history(now, [stale, *recent]), now) == pytest.approx(-1.0)
    # Eén enkel sample ná het gat → te weinig aaneengesloten punten → None.
    assert room_trend(_history(now, [stale, (0, 21.0)]), now) is None


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


# ── predict_open_intervals ─────────────────────────────────────────────────────

def _fc(now, temps):
    """Bouw een uurlijkse forecast_corr vanaf `now` (out_corr == out_raw)."""
    return [{"dt": now + timedelta(hours=h), "out_raw": t, "out_corr": t}
            for h, t in enumerate(temps)]


def test_predict_open_kwartier_granulariteit():
    # Binnen 24°, vlakke trend, buiten kruist tussen uur +1 (24.5) en +2 (23.0)
    # onder de open-drempel (inside − OPEN_MARGIN). De crossover valt binnen het uur,
    # dus de starttijd hoort op een kwartier te vallen, niet op het hele uur.
    now = datetime(2026, 6, 10, 18, 0)
    high = 22.0
    # inside 24 → open-drempel = 24 − OPEN_MARGIN (=22.5). Buiten kruist 'm tussen
    # uur +1 (23.0) en uur +2 (22.0), dus de crossover valt midden in het uur.
    fc = _fc(now, [26.0, 23.0, 22.0, 22.0])
    intervals, proj = predict_open_intervals(fc, inside_now=24.0, slope=0.0,
                                             now=now, high=high)
    assert intervals, "verwacht een open-interval"
    start = intervals[0]["start"]
    minute = int(start.split(":")[1])
    assert minute % wa.PREDICT_STEP_MIN == 0          # op het raster
    assert minute != 0                                # niet op het hele uur geplakt
    # proj blijft één waarde per forecast-uur (dashboard-grafiek).
    assert len(proj) == len(fc)


def test_predict_open_geen_crossover():
    now = datetime(2026, 6, 10, 18, 0)
    fc = _fc(now, [30.0, 30.0, 30.0])  # buiten blijft warmer dan binnen − marge
    intervals, _ = predict_open_intervals(fc, inside_now=24.0, slope=0.0,
                                          now=now, high=22.0)
    assert intervals == []


def test_predict_open_currently_open_begint_bij_nu():
    # Binnen zit onder `high` (bv. dode-band-hold: advies is "open", maar de strikte
    # koeldrempel wordt pas ver in de toekomst gehaald). Zonder `currently_open` toont de
    # tijdlijn dan een gat tot die verre crossover, terwijl het raam al open staat.
    now = datetime(2026, 6, 10, 18, 0)
    high = 22.0
    fc = _fc(now, [15.0, 15.0, 15.0, 15.0])
    intervals, _ = predict_open_intervals(fc, inside_now=20.0, slope=0.0,
                                          now=now, high=high, currently_open=False)
    assert intervals == []  # geen enkele crossover: binnen (20) haalt `high` (22) nooit

    intervals2, _ = predict_open_intervals(fc, inside_now=20.0, slope=0.0,
                                           now=now, high=high, currently_open=True)
    assert intervals2, "verwacht een segment dat bij nu begint, niet pas bij een verre crossover"
    assert intervals2[0]["start_h"] <= 0.0
    # Het segment moet blijven staan zolang er geen warmte-instroom is (buiten blijft
    # koel) — niet na één rasterstap alweer dichtklappen omdat de kamer terugzakt in de
    # dode band (dat was de eerdere, onvoldoende fix: alleen het eerste punt forceren
    # zonder een apart blijf-open-criterium).
    assert intervals2[0]["end_h"] > 1.0
    assert len(intervals2) == 1


def test_predict_open_currently_open_sluit_bij_warmte_instroom():
    # Ook geforceerd-open moet nog gewoon sluiten zodra buiten de kamer inhaalt.
    now = datetime(2026, 6, 10, 18, 0)
    high = 22.0
    fc = _fc(now, [15.0, 15.0, 25.0, 25.0])  # buiten warmt later op boven binnen
    intervals, _ = predict_open_intervals(fc, inside_now=20.0, slope=0.0,
                                          now=now, high=high, currently_open=True)
    assert intervals, "verwacht een open-segment dat bij nu begint"
    assert intervals[0]["start_h"] <= 0.0
    assert intervals[0]["end_h"] < 3.0  # sluit ergens tussen uur +1 (15°) en +2 (25°)

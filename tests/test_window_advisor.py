"""Tests voor de pure beslislogica van de raam-adviseur (Project 6).

Alleen functies zonder I/O: humidity_offset, convert_rh, open_desire, decide,
room_trend, next_reopen. Asserties pinnen de *vorm* van de logica, tegen de
moduleconstanten aan — niet tegen hardcoded tuningwaarden (die worden bewust
af en toe geretuned, vgl. tests/test_soil_model.py).
"""

from datetime import datetime, timedelta, timezone

import pytest

import om_bias
import window_advisor as wa
from window_advisor import (convert_rh, decide, humidity_offset, next_reopen,
                            open_desire, open_end_is_horizon, open_end_text,
                            open_reason, open_status_tail, predict_open_intervals,
                            room_trend, sustained_open_h, vent_rh_ahead)

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
    binnen = LOW + wa.SOFT_OPEN_MARGIN + 0.5
    # Niet warm (binnen de band), maar muf binnen + duidelijk droger buiten → open.
    args = dict(inside=binnen, outside=binnen - wa.SOFT_OPEN_MARGIN, low=LOW, high=HIGH)
    assert open_desire(**args, vent_rh=droog, humidity=binnen_rh) is True
    # Elke voorwaarde die wegvalt → geen open:
    assert open_desire(**args, vent_rh=droog, humidity=wa.RH_DRYOUT_MIN - 1) is False  # binnen niet muf
    assert open_desire(**args, vent_rh=binnen_rh - wa.RH_DRYOUT_MARGIN + 1,
                       humidity=binnen_rh) is False                                    # buiten niet droog genoeg
    assert open_desire(inside=LOW - 0.5, outside=LOW - 1.0 - wa.SOFT_OPEN_MARGIN,
                       low=LOW, high=HIGH,
                       vent_rh=droog, humidity=binnen_rh) is False                     # zou overkoelen
    assert open_desire(inside=binnen, outside=binnen + 1.0, low=LOW, high=HIGH,
                       vent_rh=droog, humidity=binnen_rh) is False                     # warmte-instroom
    # Net te weinig marge: dit is precies de band waarin decide() al zou sluiten. Openen
    # mág daar niet, anders flappert het advies elke kwartiertick heen en weer.
    assert open_desire(inside=binnen, outside=binnen - wa.SOFT_OPEN_MARGIN + 0.1,
                       low=LOW, high=HIGH,
                       vent_rh=droog, humidity=binnen_rh) is False


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
    # Niets thermisch op het spel: binnen in de band, buiten merkbaar koeler, lucht aangenaam.
    inside = (LOW + HIGH) / 2
    outside = inside - wa.OPEN_MARGIN
    fris = wa.RH_FRESH_MAX - 5
    assert open_desire(inside, outside, LOW, HIGH, vent_rh=fris, fresh_air_ok=True) is True
    # Standaard (geen expliciete opt-in) blijft dit uit — puur thermische call sites
    # veranderen niet vanzelf mee.
    assert open_desire(inside, outside, LOW, HIGH, vent_rh=fris) is False
    # Boven RH_FRESH_MAX → geen frisse-lucht-bonus, ook al blijft het onder RH_COMFORT:
    # frisse lucht is de reden zónder thermisch belang en mag dus het strengst zijn.
    assert open_desire(inside, outside, LOW, HIGH, vent_rh=wa.RH_FRESH_MAX + 1,
                       fresh_air_ok=True) is False
    # Zou overkoelen (op/onder low) → geen frisse-lucht-open.
    assert open_desire(LOW, outside, LOW, HIGH, vent_rh=fris, fresh_air_ok=True) is False
    # Warmte-instroom (buiten warmer dan binnen) → geen frisse-lucht-open.
    assert open_desire(inside, inside + 0.5, LOW, HIGH, vent_rh=fris,
                       fresh_air_ok=True) is False
    # Buiten wél koeler, maar te weinig om te openen: dit is de regel uit de screenshot
    # (binnen 22.8°, buiten 22.7°) die het advies elke kwartiertick liet omklappen.
    assert open_desire(inside, inside - 0.1, LOW, HIGH, vent_rh=fris,
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
    muf_binnen = LOW + wa.SOFT_OPEN_MARGIN + 0.5
    assert open_reason(muf_binnen, muf_binnen - wa.SOFT_OPEN_MARGIN, LOW, HIGH,
                       vent_rh=droog, humidity=binnen_rh) == "dryout"
    assert open_reason(bandinside, bandinside - wa.OPEN_MARGIN, LOW, HIGH,
                       vent_rh=wa.RH_FRESH_MAX - 5, fresh_air_ok=True) == "fresh_air"
    assert open_reason(bandinside, bandinside + 1.0, LOW, HIGH) is None
    assert open_reason(30.0, 20.0, LOW, HIGH, vent_rh=wa.RH_HARD_CAP) is None  # veto


def test_open_en_sluit_conditie_overlappen_nooit():
    """De regressietest voor het flapperen (augustus 2026).

    `decide()` sluit zodra `buiten >= binnen - CLOSE_MARGIN`. Mag een open-reden dáár
    ook waar zijn, dan wint open_desire() (het wordt eerst getoetst) en klapt het advies
    bij de kleinste wiebel in de buitentemp elke kwartiertick heen en weer. Deze test
    loopt het hele relevante (binnen, buiten)-vlak af en eist dat de twee elkaar
    nergens overlappen — voor élke combinatie van open-redenen.
    """
    overlap = []
    for i10 in range(150, 300):              # binnen 15.0 … 29.9 °C
        inside = i10 / 10.0
        for d10 in range(-30, 31):           # buiten − binnen: −3.0 … +3.0 °C
            outside = inside + d10 / 10.0
            sluit = outside >= inside - wa.CLOSE_MARGIN
            if not sluit:
                continue
            for vent_rh in (30.0, 50.0, 55.0, 59.0, 65.0, 71.0):
                for humidity in (None, 40.0, 70.0):
                    for bank in (False, True):
                        if open_desire(inside, outside, LOW, HIGH, vent_rh=vent_rh,
                                       humidity=humidity, bank_cooling=bank,
                                       fresh_air_ok=True):
                            overlap.append((inside, outside, vent_rh, humidity, bank))
    assert not overlap, f"open- én sluitconditie tegelijk waar, bv.: {overlap[:5]}"


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
    outside = inside - wa.OPEN_MARGIN
    assert decide(inside, outside, "dicht", LOW, HIGH, vent_rh=wa.RH_FRESH_MAX - 5,
                  fresh_air_ok=True) == "open"
    assert decide(inside, outside, "dicht", LOW, HIGH, vent_rh=wa.RH_FRESH_MAX - 5,
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


def test_room_trend_buiten_clamp_ruimer_dan_binnen():
    """De buitentrend gebruikt OUT_TREND_MAX, niet de binnen-clamp: buitenlucht heeft
    geen thermische massa en koelt op een heldere avond sneller af dan 1.5 °C/uur."""
    now = datetime(2026, 6, 10, 22, 30)
    # Werkelijk gemeten avondafkoeling (station, 28 juli 2026, 20:30–22:30): ~−2.6 °C/uur.
    avond = _history(now, [(2.0, 26.3), (1.75, 26.2), (1.5, 25.9), (1.25, 25.1), (1.0, 24.8),
                           (0.75, 23.7), (0.5, 22.7), (0.25, 22.0), (0, 21.6)])
    assert wa.OUT_TREND_MAX > wa.TREND_MAX_SLOPE
    # Met de binnen-clamp zou dit op de clamp blijven plakken...
    assert room_trend(avond, now) == pytest.approx(-wa.TREND_MAX_SLOPE)
    # ...met de buiten-clamp komt de echte helling eruit.
    buiten = room_trend(avond, now, clamp=wa.OUT_TREND_MAX)
    assert buiten == pytest.approx(-2.6, abs=0.2)
    # Een echt kapotte meting wordt nog steeds afgevangen.
    absurd = _history(now, [(0.25, 10.0), (0, 30.0)])  # +80 °C/uur
    assert room_trend(absurd, now, clamp=wa.OUT_TREND_MAX) == pytest.approx(wa.OUT_TREND_MAX)


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


def test_predict_open_proj_stopt_na_trend_cap():
    # proj volgt de trend tot TREND_CAP_H, en stopt dán — geen urenlange platte staart
    # tot PREDICT_HORIZON_H die niets meer voorstelt dan de laatst bekende waarde.
    now = datetime(2026, 6, 10, 18, 0)
    fc = _fc(now, [20.0] * (wa.PREDICT_HORIZON_H + 2))  # ruim voorbij de horizon
    _, proj = predict_open_intervals(fc, inside_now=24.0, slope=0.5,
                                     now=now, high=22.0)
    within_cap = [p for r, p in zip(fc, proj) if (r["dt"] - now).total_seconds() / 3600.0 <= wa.TREND_CAP_H]
    beyond_cap = [p for r, p in zip(fc, proj) if (r["dt"] - now).total_seconds() / 3600.0 > wa.TREND_CAP_H]
    assert all(p is not None for p in within_cap)
    assert all(p is None for p in beyond_cap)


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


def test_predict_open_currently_open_heropent_later():
    # Warmte-instroom sluit tijdelijk, daarna zakt buiten weer onder de open-drempel en
    # (met een licht stijgende binnentrend) komt binnen weer boven `high` → een tweede
    # segment. Dit is precies het "sluit om 18:00, weer open rond 20:30"-patroon.
    now = datetime(2026, 6, 10, 17, 0)
    high = 18.0
    fc = _fc(now, [10.0, 17.0, 17.0, 12.0, 12.0])  # uur 0..4: koel, warm-in, warm, koel, koel
    intervals, _ = predict_open_intervals(fc, inside_now=17.0, slope=0.3,
                                          now=now, high=high, currently_open=True)
    assert len(intervals) >= 2, "verwacht een sluiting gevolgd door een heropening"
    assert intervals[0]["start_h"] <= 0.0
    assert intervals[1]["start_h"] > intervals[0]["end_h"]


# ── open_status_tail ────────────────────────────────────────────────────────────

def test_open_status_tail_leeg_zonder_segmenten():
    assert open_status_tail([]) == ""


def test_open_status_tail_alleen_sluittijd():
    intervals = [{"start": "17:15", "end": "18:00", "start_h": 0.0, "end_h": 0.75}]
    assert open_status_tail(intervals) == " tot ~18:00"


def test_open_status_tail_met_heropening():
    # Dit is de tekst die naast de tijdlijn moet staan: "Blijft open"/"Nu open" mag niet
    # een langere open periode beloven dan de balk eronder (dezelfde `intervals`) toont.
    intervals = [
        {"start": "17:15", "end": "18:00", "start_h": 0.0, "end_h": 0.75},
        {"start": "20:30", "end": "23:00", "start_h": 3.25, "end_h": 5.75},
    ]
    assert open_status_tail(intervals) == " tot ~18:00, weer open rond 20:30"


# ── horizon-bewuste sluittekst + duur van het lopende segment ──────────────────

def _horizon_interval(start="18:45", start_h=5.5):
    """Een segment dat eindigt doordat de forecast ophoudt, niet door warmte-instroom."""
    return {"start": start, "end": "07:15", "start_h": start_h,
            "end_h": float(wa.PREDICT_HORIZON_H)}


def test_open_end_is_horizon_herkent_de_kijkvenstergrens():
    assert open_end_is_horizon(_horizon_interval()) is True
    assert open_end_is_horizon(
        {"start": "17:15", "end": "18:00", "start_h": 0.0, "end_h": 0.75}) is False
    assert open_end_is_horizon(None) is False


def test_open_end_text_zegt_hele_nacht_bij_horizon():
    # Op 1-8-2026 13:15 rapporteerden álle kamers end "07:15" = nu + PREDICT_HORIZON_H.
    # Dat is de rand van de forecast, geen voorspelde sluiting — dus geen kloktijd noemen.
    assert open_end_text([_horizon_interval()]) == "de hele nacht door"
    assert "07:15" not in open_status_tail([_horizon_interval()])


def test_open_end_text_noemt_een_echte_sluittijd():
    intervals = [{"start": "13:00", "end": "14:00", "start_h": -0.25, "end_h": 0.75}]
    assert open_end_text(intervals) == "tot ~14:00"
    assert open_end_text([]) == ""


def test_sustained_open_h_meet_alleen_een_lopend_segment():
    # Het lopende segment begint in het verleden (start_h ≤ 0); de duur telt vanaf nu.
    assert sustained_open_h(
        [{"start": "13:00", "end": "14:00", "start_h": -0.25, "end_h": 0.75}]) == 0.75
    # Precies het office-geval uit de screenshot: een open raam van een kwartier.
    assert sustained_open_h(
        [{"start": "13:00", "end": "13:15", "start_h": -0.25, "end_h": -0.0}]) == 0.0
    # Een segment dat pas later begint, loopt nu nog niet.
    assert sustained_open_h([_horizon_interval()]) is None
    assert sustained_open_h([]) is None


def test_sustained_open_h_poort_scheidt_blip_van_echt_venster():
    blip = [{"start": "13:00", "end": "13:15", "start_h": -0.25, "end_h": -0.0}]
    echt = [{"start": "13:00", "end": "20:00", "start_h": -0.25, "end_h": 6.75}]
    assert sustained_open_h(blip) < wa.MIN_OPEN_H
    assert sustained_open_h(echt) >= wa.MIN_OPEN_H


# ── vent_rh_ahead: vocht-vooruitblik over het kandidaat-open-venster ───────────

def test_vent_rh_ahead_pakt_de_ongunstigste_uur():
    now = datetime(2026, 8, 1, 18, 0)
    fc = [
        {"dt": now + timedelta(hours=0), "out_raw": 20.0, "out_corr": 20.0, "rh": 50.0},
        {"dt": now + timedelta(hours=1), "out_raw": 20.0, "out_corr": 20.0, "rh": 80.0},
        {"dt": now + timedelta(hours=5), "out_raw": 20.0, "out_corr": 20.0, "rh": 95.0},
    ]
    # Kamer op dezelfde temperatuur → vent_rh == buiten-RH; het klamme uur binnen het
    # venster telt, het uur ver daarbuiten niet.
    assert vent_rh_ahead(fc, 20.0, now, hours=2) == pytest.approx(80.0)
    assert vent_rh_ahead(fc, 20.0, now, hours=0.5) == pytest.approx(50.0)


def test_vent_rh_ahead_gebruikt_het_rauwe_temp_rh_paar():
    # out_corr wijkt sterk af van out_raw; convert_rh moet het consistente (raw, rh)-paar
    # gebruiken, anders verandert stilletjes de dampinhoud.
    now = datetime(2026, 8, 1, 18, 0)
    fc = [{"dt": now, "out_raw": 15.0, "out_corr": 25.0, "rh": 80.0}]
    verwacht = convert_rh(80.0, 15.0, 20.0)
    assert vent_rh_ahead(fc, 20.0, now, hours=1) == pytest.approx(verwacht)


def test_vent_rh_ahead_zonder_bruikbare_data():
    now = datetime(2026, 8, 1, 18, 0)
    assert vent_rh_ahead([], 20.0, now, hours=2) is None
    assert vent_rh_ahead([{"dt": now, "out_raw": 20.0, "rh": 50.0}], None, now, 2) is None
    # Ontbrekend RH-veld (oudere/kapotte forecast-rij) → None, niet crashen.
    assert vent_rh_ahead([{"dt": now, "out_raw": 20.0, "rh": None}], 20.0, now, 2) is None


def test_correct_forecast_draagt_rh_mee():
    now = datetime(2026, 8, 1, 12, 0)
    hourly = [{"dt": now, "temp": 20.0, "rh": 65.0},
              {"dt": now + timedelta(hours=1), "temp": None, "rh": 70.0}]
    out = wa.correct_forecast(hourly, 0.0, now, {})
    assert [r["rh"] for r in out] == [65.0, 70.0]
    # Bestaande velden blijven ongemoeid (additief).
    assert out[0]["out_raw"] == 20.0 and out[1]["out_corr"] is None


# ── correct_forecast: stationsanker + geleerde Open-Meteo-modelbias ────────────

def _hourly(now, temps):
    return [{"dt": now + timedelta(hours=i), "temp": t} for i, t in enumerate(temps)]


def test_correct_forecast_anker_geldt_volledig_op_nu():
    # Op lead 0 is de correctie precies het stationsanker, ongeacht wat er geleerd is.
    now = datetime(2026, 7, 29, 10, 15)
    fc = wa.correct_forecast(_hourly(now, [26.8]), bias=-1.5, now=now,
                             learned={"night": 1.4, "day": 0.8})
    assert fc[0]["out_corr"] == pytest.approx(26.8 - 1.5)


def test_correct_forecast_dooft_uit_naar_de_geleerde_bias_niet_naar_nul():
    # De kern van de fix: voorbij BIAS_DECAY_H bleef de ruwe modelwaarde staan, inclusief
    # de volle warme nachtbias. Nu landt hij op de geleerde climatologie.
    now = datetime(2026, 7, 29, 10, 15)
    ver = now + timedelta(hours=wa.BIAS_DECAY_H + 2)   # valt in de nacht
    assert om_bias.is_night(ver)
    fc = wa.correct_forecast([{"dt": ver, "temp": 27.5}], bias=-1.5, now=now,
                             learned={"night": 1.4, "day": 0.8})
    assert fc[0]["out_corr"] == pytest.approx(27.5 - 1.4)


def test_correct_forecast_kiest_de_emmer_van_het_geldige_uur():
    # Niet de emmer van "nu" maar die van het voorspelde uur bepaalt de correctie.
    now = datetime(2026, 7, 29, 10, 15)               # dag
    nacht = now + timedelta(hours=wa.BIAS_DECAY_H + 4)
    dag   = now + timedelta(hours=wa.BIAS_DECAY_H + 16)
    learned = {"night": 1.4, "day": 0.4}
    fc = wa.correct_forecast([{"dt": nacht, "temp": 25.0}, {"dt": dag, "temp": 25.0}],
                             bias=0.0, now=now, learned=learned)
    assert fc[0]["out_corr"] == pytest.approx(25.0 - 1.4)
    assert fc[1]["out_corr"] == pytest.approx(25.0 - 0.4)


def test_correct_forecast_zonder_geleerde_bias_is_het_oude_gedrag():
    # Terugvalgarantie: leeg leerboek → bit-voor-bit de oorspronkelijke uitdoving naar 0.
    now = datetime(2026, 7, 29, 10, 15)
    hourly = _hourly(now, [26.0, 26.5, 27.0, 27.5, 28.0])
    for learned in (None, {}, {"night": 0.0, "day": 0.0}):
        fc = wa.correct_forecast(hourly, bias=-1.5, now=now, learned=learned)
        for r, f in zip(hourly, fc):
            h = (r["dt"] - now).total_seconds() / 3600.0
            decay = max(0.0, 1.0 - h / wa.BIAS_DECAY_H)
            assert f["out_corr"] == pytest.approx(r["temp"] - 1.5 * decay)


def test_correct_forecast_verloopt_monotoon_van_anker_naar_climatologie():
    now = datetime(2026, 7, 29, 10, 15)
    learned = {"night": 1.0, "day": 1.0}          # emmer-onafhankelijk → puur de overgang
    hourly = _hourly(now, [25.0] * (wa.BIAS_DECAY_H + 3))
    corr = [f["out_corr"] for f in wa.correct_forecast(hourly, bias=-3.0, now=now,
                                                       learned=learned)]
    assert corr[0] == pytest.approx(22.0)         # anker: 25 − 3
    assert corr[-1] == pytest.approx(24.0)        # climatologie: 25 − 1
    assert all(b >= a - 1e-9 for a, b in zip(corr, corr[1:]))


def test_correct_forecast_laat_ontbrekende_uren_staan():
    now = datetime(2026, 7, 29, 10, 15)
    fc = wa.correct_forecast([{"dt": now, "temp": None}], bias=-1.5, now=now,
                             learned={"night": 1.4, "day": 0.8})
    assert fc[0]["out_raw"] is None and fc[0]["out_corr"] is None


# ── corrected_hourly: gate en next_reopen op dezelfde schaal ───────────────────

def test_corrected_hourly_geeft_de_geijkte_reeks_door():
    now = datetime(2026, 7, 29, 10, 15)
    fc = wa.correct_forecast(_hourly(now, [26.8, 27.0]), bias=-1.5, now=now, learned={})
    hc = wa.corrected_hourly(fc)
    assert [r["dt"] for r in hc] == [r["dt"] for r in fc]
    assert [r["temp"] for r in hc] == [r["out_corr"] for r in fc]


def test_gate_ziet_de_geijkte_dagmax():
    # day_max_temp draaide op de ruwe modelwaarde; met de correctie erop zakt de
    # gemeten dagmax mee — dit is wat de warme-dag-gate nu beoordeelt.
    now = datetime(2026, 7, 29, 10, 15)
    hourly = _hourly(now, [24.0, 25.0, 23.0])
    fc = wa.correct_forecast(hourly, bias=-1.5, now=now, learned={})
    assert wa.day_max_temp(wa.corrected_hourly(fc), now.date()) < wa.day_max_temp(hourly,
                                                                                 now.date())


def test_next_reopen_gebruikt_de_geijkte_reeks():
    # Een model dat te warm leest stelde de voorspelde heropening uit; op de geijkte
    # schaal valt hij eerder — en dat is de reeks die de hint en MIN_CLOSE_H nu zien.
    now = datetime(2026, 7, 29, 10, 15)
    hourly = _hourly(now, [24.0, 23.0, 22.0])
    inside = 23.0
    fc = wa.correct_forecast(hourly, bias=-2.0, now=now, learned={})
    ruw = next_reopen(hourly, inside, now)
    geijkt = next_reopen(wa.corrected_hourly(fc), inside, now)
    assert geijkt is not None
    assert ruw is None or geijkt < ruw


# ── station_bias ───────────────────────────────────────────────────────────────

def test_station_bias_zonder_wu_is_nul():
    # Zonder stationsmeting valt er niets te ijken — het model met zichzelf vergelijken
    # zou een schijnbias van 0 opleveren en de correctie stilletjes uitzetten.
    assert wa.station_bias("open-meteo", 26.8, 26.8) == 0.0
    assert wa.station_bias("wu", None, 26.8) == 0.0
    assert wa.station_bias("wu", 25.3, None) == 0.0


def test_station_bias_is_meting_min_model():
    assert wa.station_bias("wu", 25.3, 26.8) == pytest.approx(-1.5)


# ── Meldlaag: dagbudget, meldgeheugen, duur-poort en de urgente uitzondering ───

NOW = datetime(2026, 8, 1, 18, 45)
LANG = [{"start": "18:45", "end": "23:00", "start_h": -0.0, "end_h": 4.25}]
KORT = [{"start": "13:00", "end": "13:15", "start_h": -0.25, "end_h": -0.0}]


_DEFAULT = object()


def _decide_open(state, room="office", intervals=_DEFAULT, now=NOW, **kw):
    kw.setdefault("inside", 22.0)
    kw.setdefault("outside", 19.0)
    kw.setdefault("vent_rh", 50.0)
    iv = LANG if intervals is _DEFAULT else intervals
    return wa.notify_decision(state, room, "open", iv, "cool", now=now, **kw)


def _decide_dicht(state, room="office", now=NOW, **kw):
    kw.setdefault("inside", 22.0)
    kw.setdefault("outside", 22.0)
    kw.setdefault("vent_rh", 50.0)
    return wa.notify_decision(state, room, "dicht", [], None, now=now, **kw)


def test_roll_day_reset_bij_datumwissel():
    state = {"day": {"date": "2026-07-31", "plan_sent": True,
                     "rooms": {"office": {"open": 1, "close": 1, "urgent": 0,
                                          "last_urgent": None}}}}
    day = wa.roll_day(state, "2026-08-01")
    assert day["date"] == "2026-08-01"
    assert day["plan_sent"] is False and day["rooms"] == {}
    # Zelfde dag opnieuw → niets kwijt.
    day["rooms"]["office"] = {"open": 1, "close": 0, "urgent": 0, "last_urgent": None}
    assert wa.roll_day(state, "2026-08-01")["rooms"]["office"]["open"] == 1


def test_budget_staat_een_open_en_een_dicht_toe():
    state = {}
    d = _decide_open(state)
    assert d and d["kind"] == "open" and d["urgent"] is False
    wa.record_notification(state, "office", d, NOW)
    assert wa.notified_advice(state, "office") == "open"

    d2 = _decide_dicht(state, now=NOW + timedelta(hours=4))
    assert d2 and d2["kind"] == "dicht" and d2["urgent"] is False
    wa.record_notification(state, "office", d2, NOW + timedelta(hours=4))
    assert wa.notified_advice(state, "office") == "dicht"


def test_budget_blokkeert_het_tweede_open_bericht_dezelfde_dag():
    state = {}
    for _ in range(wa.MAX_OPEN_MSGS_PER_DAY):
        d = _decide_open(state)
        assert d is not None
        wa.record_notification(state, "office", d, NOW)
        # terug naar dicht melden, zodat alleen het budget nog tegenhoudt
        dd = _decide_dicht(state)
        wa.record_notification(state, "office", dd, NOW)
    assert _decide_open(state) is None


def test_budget_is_per_kamer_niet_per_huis():
    state = {}
    d = _decide_open(state, room="office")
    wa.record_notification(state, "office", d, NOW)
    assert _decide_open(state, room="Ted") is not None


def test_kort_venster_levert_geen_open_bericht():
    # Het office-geval uit de screenshot: voorspeld open van 13:00 tot 13:15.
    assert _decide_open({}, intervals=KORT) is None
    assert _decide_open({}, intervals=[]) is None


def test_geen_dicht_bericht_zonder_voorafgaand_open_bericht():
    """De val die onderdrukking introduceert.

    Als een open-advies onderdrukt is (te kort venster, budget op), dan mag de sluiting
    daarna géén "Sluit"-bericht opleveren — de ontvanger heeft dat raam nooit opengezet.
    """
    state = {}
    assert _decide_open(state, intervals=KORT) is None      # open onderdrukt
    assert _decide_dicht(state) is None                     # dus ook geen dicht
    assert wa.notified_advice(state, "office") == "dicht"


def test_urgent_breekt_door_het_dagmaximum():
    state = {}
    d = _decide_open(state)
    wa.record_notification(state, "office", d, NOW)
    # Dicht-budget opmaken met een gewone sluiting...
    dd = _decide_dicht(state)
    assert dd["urgent"] is False
    wa.record_notification(state, "office", dd, NOW)
    # ...daarna opnieuw open melden, en dan écht mis: hitte stroomt naar binnen.
    state["notified"]["office"] = {"state": "open", "at": NOW.isoformat()}
    later = NOW + timedelta(hours=1)
    urgent = wa.notify_decision(state, "office", "dicht", [], None,
                                inside=22.0, outside=22.0 + wa.URGENT_HEAT_C,
                                vent_rh=50.0, now=later)
    assert urgent and urgent["urgent"] is True and urgent["urgent_reason"] == "hitte"


def test_urgent_respecteert_cooldown_en_dagmaximum():
    # Vroeg op de dag beginnen: alle cooldown-sprongen hieronder moeten binnen dezelfde
    # kalenderdag blijven, anders reset het dagbudget en meet de test iets anders.
    start = datetime(2026, 8, 1, 7, 0)
    state = {"notified": {"office": {"state": "open", "at": start.isoformat()}},
             "day": {"date": start.date().isoformat(), "plan_sent": True,
                     "rooms": {"office": {"open": 1, "close": 1, "urgent": 0,
                                          "last_urgent": None}}}}
    heet = dict(inside=22.0, outside=22.0 + wa.URGENT_HEAT_C, vent_rh=50.0)
    d = wa.notify_decision(state, "office", "dicht", [], None, now=start, **heet)
    assert d["urgent"] is True
    wa.record_notification(state, "office", d, start)
    state["notified"]["office"] = {"state": "open", "at": start.isoformat()}

    # Binnen de cooldown → stil.
    kort_erna = start + timedelta(hours=wa.URGENT_COOLDOWN_H - 0.5)
    assert wa.notify_decision(state, "office", "dicht", [], None,
                              now=kort_erna, **heet) is None
    # Erbuiten mag het weer, tot het dagmaximum bereikt is.
    erna = start + timedelta(hours=wa.URGENT_COOLDOWN_H + 0.1)
    for _ in range(wa.MAX_URGENT_MSGS_PER_DAY - 1):
        d = wa.notify_decision(state, "office", "dicht", [], None, now=erna, **heet)
        assert d["urgent"] is True
        wa.record_notification(state, "office", d, erna)
        state["notified"]["office"] = {"state": "open", "at": erna.isoformat()}
        erna += timedelta(hours=wa.URGENT_COOLDOWN_H + 0.1)
    assert erna.date() == start.date(), "test moet binnen één kalenderdag blijven"
    assert wa.notify_decision(state, "office", "dicht", [], None,
                              now=erna, **heet) is None


def test_urgent_alleen_bij_veto_of_hitte_en_alleen_op_een_gemeld_open_raam():
    assert wa.urgent_reason(22.0, 22.0 + wa.URGENT_HEAT_C, 50.0, "open") == "hitte"
    assert wa.urgent_reason(22.0, 19.0, wa.RH_HARD_CAP, "open") == "muf"
    # Net niet heet genoeg, en niet muf genoeg → geen urgentie.
    assert wa.urgent_reason(22.0, 22.0 + wa.URGENT_HEAT_C - 0.1, 50.0, "open") is None
    assert wa.urgent_reason(22.0, 19.0, wa.RH_HARD_CAP - 1, "open") is None
    # Nooit gemeld dat het raam open stond → niets te redden, dus stil.
    assert wa.urgent_reason(22.0, 30.0, 95.0, "dicht") is None
    assert wa.urgent_reason(None, 30.0, 95.0, "open") is None


def test_dagwissel_geeft_weer_ruimte():
    state = {}
    d = _decide_open(state)
    wa.record_notification(state, "office", d, NOW)
    dd = _decide_dicht(state)
    wa.record_notification(state, "office", dd, NOW)
    assert _decide_open(state) is None
    morgen = NOW + timedelta(days=1)
    assert _decide_open(state, now=morgen) is not None


# ── Berichtteksten + dagplan ──────────────────────────────────────────────────

def test_open_bericht_noemt_reden_en_duur():
    d = {"kind": "open", "urgent": False, "reason": "bank", "urgent_reason": None}
    line = wa.room_message_line("office", d, 22.8, 21.0, LANG)
    assert "🟢 *office*" in line
    assert "binnen 22.8°, buiten 21.0°" in line
    assert wa.REASON_TEXT["bank"] in line
    assert "open laten tot ~23:00" in line


def test_open_bericht_belooft_geen_verzonnen_sluittijd():
    # Segment tot de forecast-horizon → geen kloktijd, wel een duidelijke instructie.
    d = {"kind": "open", "urgent": False, "reason": "cool", "urgent_reason": None}
    line = wa.room_message_line("Ted", d, 22.0, 19.0, [_horizon_interval()])
    assert "open laten de hele nacht door" in line
    assert "07:15" not in line


def test_dicht_bericht_noemt_heropening():
    d = {"kind": "dicht", "urgent": False, "reason": None, "urgent_reason": None}
    line = wa.room_message_line("office", d, 21.4, 22.3, [], reopen="18:45")
    assert "🔴 *office*" in line
    assert "weer open rond 18:45" in line


def test_urgent_bericht_benoemt_de_urgentie():
    muf = {"kind": "dicht", "urgent": True, "reason": None, "urgent_reason": "muf"}
    line = wa.room_message_line("hotties", muf, 21.0, 20.0, [], vent_rh=78.0)
    assert "schimmelrisico" in line and "RH ~78%" in line
    heet = {"kind": "dicht", "urgent": True, "reason": None, "urgent_reason": "hitte"}
    assert wa.URGENT_TEXT["hitte"] in wa.room_message_line("Ted", heet, 21.0, 24.0, [])


def test_advice_message_kop_volgt_de_inhoud():
    op = ("office", {"kind": "open", "urgent": False})
    dicht = ("Ted", {"kind": "dicht", "urgent": False})
    urgent = ("Ted", {"kind": "dicht", "urgent": True})
    assert "Ramen open" in wa.advice_message(NOW, [op], ["x"])
    assert "Ramen dicht" in wa.advice_message(NOW, [dicht], ["x"])
    assert "Raam-advies" in wa.advice_message(NOW, [op, dicht], ["x", "y"])
    assert wa.advice_message(NOW, [urgent], ["x"]).startswith("⚠️")
    assert "18:45" in wa.advice_message(NOW, [op], ["x"])


def test_build_day_plan_negeert_blips_en_sorteert_op_openingstijd():
    rooms = {
        "office": {"open_intervals": [
            {"start": "13:00", "end": "13:15", "start_h": -0.25, "end_h": -0.0},  # blip
            _horizon_interval("18:45", 5.5),
        ]},
        "Ted": {"open_intervals": [_horizon_interval("19:45", 6.5)]},
        "hotties": {"open_intervals": []},
        "Living room": {"open_intervals": [
            {"start": "17:45", "end": "23:00", "start_h": 4.5, "end_h": 9.75}]},
    }
    plan = wa.build_day_plan(rooms, NOW)
    # Kamers met een venster op volgorde van openen; de kamer zónder venster achteraan.
    assert [p["room"] for p in plan] == ["Living room", "office", "Ted", "hotties"]
    # De blip van 13:00–13:15 telt niet mee als het open-moment van office.
    assert plan[1]["windows"][0]["start"] == "18:45"


def test_build_day_plan_houdt_een_lopend_venster_altijd_in_het_plan():
    """Een raam dat nú openstaat verdwijnt niet uit het plan omdat het bijna dicht moet.

    De duur-poort weegt of een vóórspeld venster het openzetten waard is; op een lopend
    venster is dat de verkeerde vraag — dat raam staat al open en de sluittijd is het enige
    dat er nog te plannen valt. Toch poorten gaf op 3 augustus 2026 het omgekeerde van een
    plan: vier open kamers, sluittijden rond 09:15–09:30, en de twee met nog géén anderhalf
    uur te gaan (Living room, Ted) stonden in het bericht als "blijft vandaag dicht".
    """
    kort = {"start": "08:00", "end": "09:15", "start_h": -0.25, "end_h": 1.0}
    plan = wa.build_day_plan({"Ted": {"open_intervals": [kort]}}, NOW)
    ted = next(p for p in plan if p["room"] == "Ted")
    assert [w["start"] for w in ted["windows"]] == ["08:00"]
    assert ted["windows"][0]["running"] is True
    assert "*Ted* — staat al open, dicht rond 09:15" in wa.day_plan_message(plan, 33.7, NOW)
    # Hetzelfde korte venster in de toekomst is wél een blip: daar poort de duur-eis door.
    straks = {"start": "12:00", "end": "13:15", "start_h": 3.75, "end_h": 5.0}
    plan = wa.build_day_plan({"Ted": {"open_intervals": [straks]}}, NOW)
    assert next(p for p in plan if p["room"] == "Ted")["windows"] == []


def test_build_day_plan_houdt_elke_kamer_in_het_overzicht():
    """Een kamer zonder venster is óók informatie: 'die blijft vandaag dicht'.

    Vielen die kamers weg, dan noemde het plan alleen de kamers die toevallig een venster
    hadden en zei het over de rest niets.
    """
    plan = wa.build_day_plan({}, NOW)
    assert [p["room"] for p in plan] == list(wa.ROOMS)
    assert all(p["windows"] == [] for p in plan)


def test_build_day_plan_neemt_alle_vensters_mee_tot_het_maximum():
    """Het plan moet ook de sluiting en een tweede opening kunnen tonen."""
    ivs = [{"start": "08:45", "end": "10:45", "start_h": -0.25, "end_h": 2.0},
           {"start": "18:45", "end": "21:00", "start_h": 5.5, "end_h": 7.75},
           {"start": "22:00", "end": "23:45", "start_h": 8.75, "end_h": 10.5},
           {"start": "01:00", "end": "03:00", "start_h": 11.75, "end_h": 13.75}]
    plan = wa.build_day_plan({"Ted": {"open_intervals": ivs}}, NOW)
    vensters = plan[0]["windows"]
    assert len(vensters) == wa.MAX_PLAN_WINDOWS
    assert [w["start"] for w in vensters] == ["08:45", "18:45", "22:00"]
    assert [w["running"] for w in vensters] == [True, False, False]


def test_day_plan_message_noemt_openen_en_sluiten_per_kamer():
    plan = [
        {"room": "Ted", "start_h": -0.25, "windows": [
            {"start": "08:45", "end": "10:45", "start_h": -0.25, "end_h": 2.0,
             "horizon": False, "running": True},
            {"start": "18:45", "end": "21:00", "start_h": 5.5, "end_h": 7.75,
             "horizon": False, "running": False}]},
        {"room": "office", "start_h": 5.5, "windows": [
            {"start": "18:45", "end": "07:15", "start_h": 5.5,
             "end_h": float(wa.PREDICT_HORIZON_H), "horizon": True, "running": False}]},
        {"room": "hotties", "start_h": float("inf"), "windows": []},
    ]
    msg = wa.day_plan_message(plan, 24.1, NOW)
    assert "Raamplan" in msg and "24.1°" in msg
    # Een lopend venster is geen actie meer, maar de sluittijd is dat wél.
    assert "*Ted* — staat al open, dicht rond 10:45; daarna open 18:45–21:00" in msg
    # Tot de kijkvenstergrens → geen verzonnen kloktijd.
    assert "*office* — open vanaf 18:45, de hele nacht door" in msg
    assert "07:15" not in msg
    # Elke kamer staat erin, ook die zonder venster.
    assert "*hotties* — blijft vandaag dicht" in msg


def test_day_plan_message_zonder_venster():
    msg = wa.day_plan_message([], 23.0, NOW)
    assert "blijven de ramen dicht" in msg
    alles_dicht = [{"room": r, "windows": [], "start_h": float("inf")} for r in wa.ROOMS]
    assert "blijven de ramen dicht" in wa.day_plan_message(alles_dicht, 23.0, NOW)


def test_urgent_muf_volgt_de_veto_vlag_van_decide():
    """Het dashboard publiceert `vent_rh` afgerond; de veto zelf rekent onafgerond.

    Zonder doorgifte van die vlag kan de meldlaag op de grens net anders oordelen dan de
    beslissing die ze beschrijft.
    """
    net_onder = wa.RH_HARD_CAP - 0.4  # rondt af naar RH_HARD_CAP
    assert wa.urgent_reason(22.0, 21.0, net_onder, "open") is None
    assert wa.urgent_reason(22.0, 21.0, net_onder, "open", rh_veto=True) == "muf"
    # Andersom: afgerond ópheffend, maar decide() vetode niet → geen muf-label.
    assert wa.urgent_reason(22.0, 21.0, wa.RH_HARD_CAP, "open", rh_veto=False) is None


# ── WU history/all: veldnaam + zichtbare terugval ─────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


@pytest.fixture
def wu_env(monkeypatch):
    monkeypatch.setenv("WU_STATION_ID", "TESTSTATION")
    monkeypatch.setenv("WU_API_KEY", "testkey")


def _obs(minutes_ago, now, field="solarRadiationHigh", value=300.0):
    """WU-observatierecord met een UTC-stempel, zoals het endpoint ze levert."""
    t = (now - timedelta(minutes=minutes_ago)).astimezone(timezone.utc)
    return {"obsTimeUtc": t.strftime("%Y-%m-%dT%H:%M:%SZ"), field: value}


def test_recent_solar_leest_het_history_veld(wu_env, monkeypatch):
    """History-records dragen `solarRadiationHigh`; alleen het current-endpoint kent
    het kale `solarRadiation`. Die verwisseling liet dit endpoint een maand lang
    stilletjes niets opleveren (aug 2026)."""
    now = datetime.now(wa.TZ)
    obs = [_obs(m, now, value=v) for m, v in ((5, 100.0), (15, 500.0), (25, 300.0))]
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: _Resp({"observations": obs}))
    assert wa.fetch_wu_recent_solar(now) == 300.0


@pytest.mark.parametrize("field", ["solarRadiationAvg", "solarRadiation"])
def test_recent_solar_accepteert_ook_avg_en_kale_veldnaam(wu_env, monkeypatch, field):
    now = datetime.now(wa.TZ)
    obs = [_obs(5, now, field=field, value=222.0)]
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: _Resp({"observations": obs}))
    assert wa.fetch_wu_recent_solar(now) == 222.0


def test_recent_solar_negeert_samples_buiten_het_venster(wu_env, monkeypatch):
    now = datetime.now(wa.TZ)
    obs = [_obs(5, now, value=100.0), _obs(200, now, value=900.0)]
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: _Resp({"observations": obs}))
    assert wa.fetch_wu_recent_solar(now) == 100.0


@pytest.mark.parametrize("payload,verwacht", [
    ({"observations": []}, "0 records"),
    ({"observations": [{"obsTimeUtc": "2026-08-01T10:00:00Z", "tempAvg": 20.0}]},
     "0 met instraling"),
])
def test_recent_solar_meldt_waarom_hij_terugvalt(wu_env, monkeypatch, capsys, payload, verwacht):
    """Elke onbruikbare uitkomst moet een diagnose printen. De stille lege-tak is
    precies wat de veldnaam-bug een maand lang onzichtbaar hield."""
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: _Resp(payload))
    assert wa.fetch_wu_recent_solar(datetime.now(wa.TZ)) is None
    assert verwacht in capsys.readouterr().out


def test_recent_solar_meldt_non_200(wu_env, monkeypatch, capsys):
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: _Resp({}, status=403))
    assert wa.fetch_wu_recent_solar(datetime.now(wa.TZ)) is None
    assert "status 403" in capsys.readouterr().out


def test_workflow_checkout_pint_branch_tip(assert_checkout_pinned):
    assert_checkout_pinned("window-notify.yml")

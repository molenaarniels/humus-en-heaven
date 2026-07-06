"""Pure-logica-tests voor Teds nachtvoorspelling (`night_forecast.py`, Project 10).

Geen netwerk of Gist: de sim-integratietest draait op een mini-fixture-huis met
alleen de ted-zone; main() wordt getest met gemonkeypatchte weer/artefact-seams
(o.a. dat het _NEIGHBOR_TEMP-buur-anker écht herbonden wordt — de makkelijk te
vergeten stap bij extern simulate()-gebruik).
"""
import math
from datetime import datetime, timedelta

import pytest

import airflow_model as am
import night_forecast as nf
from shared_const import TZ

NOW = datetime(2026, 7, 2, 18, 45, tzinfo=TZ)

HOUSE = {
    "location": {"lat": 52.09, "lon": 5.12},
    "terrain": {},
    "rooms": {"ted": {"label": "Ted", "volume_m3": 32.0, "exterior_wall_m2": 12.0,
                      "floor": 0, "from_window_data": "Ted"},
             "stair": {"label": "Trap", "volume_m3": 20.0, "exterior_wall_m2": 8.0,
                       "floor": 1}},
    "junctions": {},
    "windows": {
        "ted_window": {"room": "ted", "facade_azimuth_deg": 309.0, "glass_m2": 4.5,
                       "max_open_area_m2": 0.0, "tilt_deg": 90.0,
                       "center_height_m": 1.5, "shading": "lamella",
                       "shade": {"factor": 0.12, "label": "Gordijn"}},
        "ted_small_window": {"room": "ted", "facade_azimuth_deg": 309.0,
                             "glass_m2": 0.16, "max_open_area_m2": 0.16,
                             "open_type": "casement", "tilt_frac": 0.35,
                             "tilt_deg": 90.0, "center_height_m": 1.8,
                             "shading": "none"},
    },
    "vents": {},
    "doors": {
        "ted_stair": {"between": ["ted", "stair"], "area_m2": 1.8,
                     "center_height_m": 1.0, "default_state": "open",
                     "label": "Ted ↔ trap"},
    },
}


def _rows(now=NOW, night_out=14.0, day_out=26.0):
    """Synthetische dag/nacht-cyclus rond `now` (2 dagen terug + 2 vooruit)."""
    t0 = (now - timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    rows = []
    for h in range(96):
        t = t0 + timedelta(hours=h)
        sun = max(0.0, math.sin(math.pi * (t.hour - 5.5) / 15.5))
        rows.append({"dt": t, "T_out": night_out + (day_out - night_out) * sun,
                     "rh": 55, "precip": 0.0, "wind_speed": 2.5, "wind_dir": 220.0,
                     "gust": 4.0, "shortwave": 800 * sun, "direct": 600 * sun,
                     "diffuse": 200 * sun})
    return rows


# ── Horizon + scenario-injectie ──────────────────────────────────────────────────────

def test_hours_until_morning():
    assert nf.hours_until_morning(NOW) == pytest.approx(13.25)
    laat = datetime(2026, 7, 2, 23, 30, tzinfo=TZ)
    assert nf.hours_until_morning(laat) == pytest.approx(8.5)


def test_timeline_reaches_morning():
    tl = am.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, window_h=24.0,
                           end_h=nf.hours_until_morning(NOW))
    morgen_745 = (NOW + timedelta(days=1)).replace(hour=7, minute=45)
    assert tl[-1]["t"] >= morgen_745


def test_scenario_injection_future_only():
    tl = am.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, window_h=4.0,
                           end_h=6.0)
    open_tl = nf.scenario_timeline(tl, NOW, "open")
    for orig, sc in zip(tl, open_tl):
        if sc["t"] >= NOW:
            assert sc["states"]["ted_small_window"] == "open"
            assert sc["states"]["ted_stair"] == "dicht"   # deur dicht in béíde scenario's
            assert sc is not orig                      # kopie, geen mutatie
        else:
            assert sc is orig                          # verleden blijft de log
    assert "ted_small_window" not in tl[-1]["states"]  # origineel ongemuteerd
    assert "ted_stair" not in tl[-1]["states"]


def test_scenario_forces_door_closed_in_both_states():
    tl = am.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, window_h=4.0,
                           end_h=6.0)
    for state in ("open", "dicht"):
        sc = nf.scenario_timeline(tl, NOW, state)
        for step in sc:
            if step["t"] >= NOW:
                assert step["states"]["ted_stair"] == "dicht"


def test_closed_door_retains_more_heat_overnight():
    # Kille trap (14°, zoals de koudere schacht 's nachts); ted start warm. Met de deur
    # geforceerd dicht (het echte gedrag) moet ted minder afkoelen dan een controle-run
    # waarin de deur openblijft.
    tl = am.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, window_h=24.0,
                           end_h=nf.hours_until_morning(NOW))
    params = am.default_params(HOUSE)
    seed = {"ted": 24.0, "stair": 14.0}

    door_closed_tl = nf.scenario_timeline(tl, NOW, "dicht")
    door_open_tl = [({**step, "states": {**step["states"], nf.WINDOW_ID: "dicht"}}
                     if step["t"] >= NOW else step) for step in tl]

    sim_closed = am.simulate(HOUSE, params, door_closed_tl, seed)
    sim_open_door = am.simulate(HOUSE, params, door_open_tl, seed)
    stats_closed = nf.night_stats(sim_closed["series"]["ted"], NOW)
    stats_open_door = nf.night_stats(sim_open_door["series"]["ted"], NOW)

    assert stats_closed["min"] > stats_open_door["min"]
    assert stats_closed["mean"] > stats_open_door["mean"]


def test_open_window_cools_more_overnight():
    # Buiten 14° 's nachts, kamer start 24°: het open raampje moet om 07:00
    # (en op z'n minst qua nacht-min) kouder uitkomen dan dicht.
    tl = am.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, window_h=24.0,
                           end_h=nf.hours_until_morning(NOW))
    params = am.default_params(HOUSE)
    seed = {"ted": 24.0}
    stats = {}
    for state in ("open", "dicht"):
        sim = am.simulate(HOUSE, params, nf.scenario_timeline(tl, NOW, state), seed)
        stats[state] = nf.night_stats(sim["series"]["ted"], NOW)
    assert stats["open"]["marks"][7] < stats["dicht"]["marks"][7]
    assert stats["open"]["min"] <= stats["dicht"]["min"]


# ── Anker-correctie (Fix 2: 24u-drift terugzetten op de laatste actuele meting) ──────

def test_anchor_now_prefers_fresh_actual():
    ta_now = {"ted": 20.0, "stair": 15.0}
    actual = {"ted": [(NOW - timedelta(minutes=10), 22.5)]}
    corrected = nf.anchor_now(ta_now, actual, NOW)
    assert corrected["ted"] == 22.5
    assert corrected["stair"] == 15.0     # geen actual voor stair → ongemoeid
    assert ta_now["ted"] == 20.0          # input niet gemuteerd


def test_anchor_now_uses_newest_sample():
    ta_now = {"ted": 20.0}
    actual = {"ted": [(NOW - timedelta(minutes=20), 21.0), (NOW - timedelta(minutes=5), 23.0)]}
    assert nf.anchor_now(ta_now, actual, NOW)["ted"] == 23.0


def test_anchor_now_ignores_stale_actual():
    ta_now = {"ted": 20.0}
    stale = {"ted": [(NOW - timedelta(minutes=45), 22.5)]}   # > ANCHOR_MAX_STALENESS_MIN
    assert nf.anchor_now(ta_now, stale, NOW)["ted"] == 20.0


def test_anchor_now_staleness_boundary():
    ta_now = {"ted": 20.0}
    just_fresh = {"ted": [(NOW - timedelta(minutes=30), 22.5)]}
    assert nf.anchor_now(ta_now, just_fresh, NOW)["ted"] == 22.5
    just_stale = {"ted": [(NOW - timedelta(minutes=30, seconds=1), 22.5)]}
    assert nf.anchor_now(ta_now, just_stale, NOW)["ted"] == 20.0


def test_anchor_now_empty_samples_noop():
    ta_now = {"ted": 20.0}
    assert nf.anchor_now(ta_now, {"ted": []}, NOW) == {"ted": 20.0}


def test_main_applies_anchor_correction(monkeypatch, capsys):
    now = datetime.now(TZ)
    rows = _rows(now)
    history = [{"t": (now - timedelta(hours=h)).isoformat(), "temp": 24.0}
              for h in range(24, 0, -4)]
    # meest recente meting wijkt duidelijk af van wat de blinde 24u-warmup zou opleveren
    history.append({"t": (now - timedelta(minutes=5)).isoformat(), "temp": 30.0})
    wd = {"rooms": {"Ted": {"history": history, "inside": 30.0}}}
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(am, "load_house", lambda: HOUSE)
    monkeypatch.setattr(am, "fetch_weather", lambda: {"hourly": rows, "current": {}})
    monkeypatch.setattr(am, "load_openings_log", lambda: [])
    monkeypatch.setattr(am, "load_learned", dict)
    monkeypatch.setattr(am, "load_window_data", lambda: wd)
    nf.main()
    out = capsys.readouterr().out
    assert "anker-correctie" in out


# ── Nachtstatistiek + advies + tog ───────────────────────────────────────────────────

def _series(now=NOW, start_t=23.0, end_t=19.0):
    """Lineair dalende kamertemp 19:00 → 08:00 (15-min raster)."""
    t0 = now.replace(hour=19, minute=0)
    steps = int(13 * 4) + 1
    return [(t0 + timedelta(minutes=15 * i),
             start_t + (end_t - start_t) * i / (steps - 1)) for i in range(steps)]


def test_night_stats_extraction():
    st = nf.night_stats(_series(), NOW)
    assert set(st["marks"]) == {23, 3, 7}
    assert st["marks"][23] > st["marks"][3] > st["marks"][7]   # daalt de nacht door
    assert st["min"] == pytest.approx(min(v for _, v in _series()
                                          if v is not None), abs=0.5)
    assert st["max"] <= 23.0
    assert nf.night_stats([], NOW) is None


def test_tog_table_boundaries():
    assert nf.tog_advice(24.0) == ("0.5 tog", "korte pyjama of alleen romper")
    assert nf.tog_advice(21.0) == ("1.0 tog", "korte pyjama")
    assert nf.tog_advice(18.0) == ("2.5 tog", "lange pyjama")
    assert nf.tog_advice(17.9) == ("2.5 tog", "warme pyjama + romper")
    assert nf.tog_advice(15.0) == ("3.5 tog", "warme pyjama + romper")


def test_season_gate():
    maart = datetime(2026, 3, 10, 18, 45, tzinfo=TZ)
    juni = datetime(2026, 6, 10, 18, 45, tzinfo=TZ)
    assert nf.should_send(juni, night_max=15.0)          # zomerseizoen: altijd
    assert not nf.should_send(maart, night_max=15.0)     # koude voorjaarsnacht: stil
    assert nf.should_send(maart, night_max=20.0)         # warme uitschieter: wél


def test_message_format():
    closed = {"min": 18.5, "max": 21.0, "mean": 19.5,
             "marks": {23: 20.5, 3: 19.5, 7: 18.8}}
    open_ = {"min": 17.0, "max": 20.5, "mean": 18.3,
             "marks": {23: 20.0, 3: 18.5, 7: 17.5}}
    msg = nf.build_message(NOW, 22.5, 14.2, closed, open_, reported_open=True)
    assert "Teds nacht" in msg
    assert "raampje dicht" in msg and "voorspelling gaat uit van dicht" in msg
    assert "23:00" in msg and "07:00" in msg
    assert "-1.3°" in msg                       # open zou 18.8 → 17.5 = -1.3° schelen
    assert "2.5 tog" in msg                     # nachtgemiddeld (dicht) 19.5° → 18–21-band
    assert len(msg) < 4096


def test_message_format_matches_reported_stand():
    closed = {"min": 18.5, "max": 21.0, "mean": 19.5,
             "marks": {23: 20.5, 3: 19.5, 7: 18.8}}
    open_ = {"min": 17.0, "max": 20.5, "mean": 18.3,
             "marks": {23: 20.0, 3: 18.5, 7: 17.5}}
    msg = nf.build_message(NOW, 22.5, 14.2, closed, open_, reported_open=False)
    assert "voorspelling gaat uit van dicht" not in msg


# ── main(): buur-anker-rebind via de gemockte seams ──────────────────────────────────

def test_main_rebinds_neighbor_temp(monkeypatch, capsys):
    rows = _rows(datetime.now(TZ))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(am, "load_house", lambda: HOUSE)
    monkeypatch.setattr(am, "fetch_weather", lambda: {"hourly": rows, "current": {}})
    monkeypatch.setattr(am, "load_openings_log", lambda: [])
    monkeypatch.setattr(am, "load_learned", dict)
    monkeypatch.setattr(am, "load_window_data", dict)
    am._NEIGHBOR_TEMP = -99.0                            # sentinel
    nf.main()
    expected = am.neighbor_temp_estimate(rows, datetime.now(TZ))
    assert am._NEIGHBOR_TEMP == pytest.approx(expected, abs=0.2)
    out = capsys.readouterr().out
    assert "Teds nacht" in out or "stil" in out          # bericht of seizoenspoort

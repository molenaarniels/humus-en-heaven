"""Pure-logica-tests voor Teds nachtvoorspelling (`night_forecast.py`, Project 10).

Geen netwerk of Gist: de sim-integratietest draait op een mini-fixture-huis met
alleen de ted-zone; main() wordt getest met gemonkeypatchte weer/artefact-seams
(o.a. dat de RunContext-ankers — buur + bodem — écht via make_context/build_timeline
doorgegeven worden; het oude module-global-rebinden bestaat in de herbouw niet meer).
"""
import math
from datetime import datetime, timedelta

import pytest

import night_forecast as nf
import vent_io as vio
import vent_physics as vp
from shared_const import TZ

NOW = datetime(2026, 7, 2, 18, 45, tzinfo=TZ)

# Reproduceert exact het oude module-default-gedrag (_LAT/_LON/_NEIGHBOR_TEMP/_GROUND_TEMP)
# voor de directe build_timeline/simulate-tests; main() bouwt zijn eigen ctx via make_context.
CTX = vp.RunContext(lat=52.09, lon=5.12,
                    neighbor_temp=vp.NEIGHBOR_TEMP, ground_temp=vp.GROUND_TEMP)

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
    tl = vio.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, 24.0, CTX,
                            end_h=nf.hours_until_morning(NOW))
    morgen_745 = (NOW + timedelta(days=1)).replace(hour=7, minute=45)
    assert tl[-1]["t"] >= morgen_745


def test_scenario_injection_future_only():
    tl = vio.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, 4.0, CTX,
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
    tl = vio.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, 4.0, CTX,
                            end_h=6.0)
    for state in ("open", "dicht"):
        sc = nf.scenario_timeline(tl, NOW, state)
        for step in sc:
            if step["t"] >= NOW:
                assert step["states"]["ted_stair"] == "dicht"


def test_all_open_timeline_opens_every_window_and_door():
    tl = vio.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, 4.0, CTX,
                            end_h=6.0)
    all_open = nf.all_open_timeline(tl, HOUSE, NOW)
    for orig, sc in zip(tl, all_open):
        if sc["t"] >= NOW:
            for wid in HOUSE["windows"]:
                assert sc["states"][wid] == "open"
            for did in HOUSE["doors"]:
                assert sc["states"][did] == "open"
            assert sc is not orig                      # kopie, geen mutatie
        else:
            assert sc is orig                          # verleden blijft de log
    assert "ted_stair" not in tl[-1]["states"]           # origineel ongemuteerd


def test_closed_door_retains_more_heat_overnight():
    # Kille trap (14°, zoals de koudere schacht 's nachts); ted start warm. Met de deur
    # geforceerd dicht (het echte gedrag) moet ted minder afkoelen dan een controle-run
    # waarin de deur openblijft.
    tl = vio.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, 24.0, CTX,
                            end_h=nf.hours_until_morning(NOW))
    params = vio.default_params(HOUSE)
    seed = {"ted": 24.0, "stair": 14.0}

    door_closed_tl = nf.scenario_timeline(tl, NOW, "dicht")
    door_open_tl = [({**step, "states": {**step["states"], nf.WINDOW_ID: "dicht"}}
                     if step["t"] >= NOW else step) for step in tl]

    sim_closed = vp.simulate(HOUSE, params, door_closed_tl, seed, CTX)
    sim_open_door = vp.simulate(HOUSE, params, door_open_tl, seed, CTX)
    stats_closed = nf.night_stats(sim_closed["series"]["ted"], NOW)
    stats_open_door = nf.night_stats(sim_open_door["series"]["ted"], NOW)

    assert stats_closed["min"] > stats_open_door["min"]
    assert stats_closed["mean"] > stats_open_door["mean"]


def test_open_window_cools_more_overnight():
    # Buiten 14° 's nachts, kamer start 24°: het open raampje moet om 07:00
    # (en op z'n minst qua nacht-min) kouder uitkomen dan dicht.
    tl = vio.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, 24.0, CTX,
                            end_h=nf.hours_until_morning(NOW))
    params = vio.default_params(HOUSE)
    seed = {"ted": 24.0}
    stats = {}
    for state in ("open", "dicht"):
        sim = vp.simulate(HOUSE, params, nf.scenario_timeline(tl, NOW, state), seed, CTX)
        stats[state] = nf.night_stats(sim["series"]["ted"], NOW)
    assert stats["open"]["marks"][7] < stats["dicht"]["marks"][7]
    assert stats["open"]["min"] <= stats["dicht"]["min"]


def test_all_open_cools_at_least_as_much_as_window_only():
    # Alles open (incl. de trapdeur naar de koelere schacht) mag 's nachts niet
    # minder afkoelen dan alleen het raampje open (deur dicht).
    tl = vio.build_timeline(HOUSE, {"hourly": _rows()}, [], NOW, 24.0, CTX,
                            end_h=nf.hours_until_morning(NOW))
    params = vio.default_params(HOUSE)
    seed = {"ted": 24.0, "stair": 24.0}
    sim_open = vp.simulate(HOUSE, params, nf.scenario_timeline(tl, NOW, "open"), seed, CTX)
    sim_all = vp.simulate(HOUSE, params, nf.all_open_timeline(tl, HOUSE, NOW), seed, CTX)
    stats_open = nf.night_stats(sim_open["series"]["ted"], NOW)
    stats_all = nf.night_stats(sim_all["series"]["ted"], NOW)
    assert stats_all["marks"][7] <= stats_open["marks"][7]


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
    monkeypatch.setattr(vio, "load_house", lambda: HOUSE)
    monkeypatch.setattr(vio, "fetch_weather", lambda lat, lon: {"hourly": rows, "current": {}})
    monkeypatch.setattr(vio, "load_openings_log", lambda: [])
    monkeypatch.setattr(vio, "load_learned", dict)
    monkeypatch.setattr(vio, "load_window_data", lambda: wd)
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
    all_open = {"min": 16.2, "max": 20.0, "mean": 17.6,
               "marks": {23: 19.5, 3: 17.8, 7: 16.5}}
    msg = nf.build_message(NOW, 22.5, 14.2, closed, open_, all_open, reported_open=True)
    assert "Teds nacht" in msg
    assert "raampje dicht" in msg and "voorspelling gaat uit van dicht" in msg
    assert "23:00" in msg and "07:00" in msg
    assert "-1.3°" in msg                       # open zou 18.8 → 17.5 = -1.3° schelen
    assert "-2.3°" in msg                       # alles open zou 18.8 → 16.5 = -2.3° schelen
    assert "Alles open" in msg
    assert "2.5 tog" in msg                     # nachtgemiddeld (dicht) 19.5° → 18–21-band
    assert len(msg) < 4096


def test_message_format_matches_reported_stand():
    closed = {"min": 18.5, "max": 21.0, "mean": 19.5,
             "marks": {23: 20.5, 3: 19.5, 7: 18.8}}
    open_ = {"min": 17.0, "max": 20.5, "mean": 18.3,
             "marks": {23: 20.0, 3: 18.5, 7: 17.5}}
    all_open = {"min": 16.2, "max": 20.0, "mean": 17.6,
               "marks": {23: 19.5, 3: 17.8, 7: 16.5}}
    msg = nf.build_message(NOW, 22.5, 14.2, closed, open_, all_open, reported_open=False)
    assert "voorspelling gaat uit van dicht" not in msg


# ── main(): RunContext-ankers via de gemockte seams ──────────────────────────────────

def test_main_geeft_de_ctx_ankers_door(monkeypatch, capsys):
    """Herschreven _NEIGHBOR_TEMP-rebind-test: main() bouwt één RunContext (make_context)
    en geeft die aan élke build_timeline mee. Bewuste gedragswijziging t.o.v. het oude
    rebinden: make_context legt óók het zomerplafond (NEIGHBOR_SUMMER_CAP) op het
    buur-anker — hittegolf-rows horen dus op de kap uit te komen, niet op de rauwe
    schatting. Het bodem-anker (ground_temp) rijdt in dezelfde ctx mee."""
    now = datetime.now(TZ)
    rows = _rows(now, night_out=22.0, day_out=34.0)      # hittegolf: 3-daags gemiddelde > kap
    seen = []
    orig = vio.build_timeline

    def spy(*a, **kw):
        seen.append(a[5] if len(a) > 5 else kw.get("ctx"))
        return orig(*a, **kw)

    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(vio, "load_house", lambda: HOUSE)
    monkeypatch.setattr(vio, "fetch_weather", lambda lat, lon: {"hourly": rows, "current": {}})
    monkeypatch.setattr(vio, "load_openings_log", lambda: [])
    monkeypatch.setattr(vio, "load_learned", dict)
    monkeypatch.setattr(vio, "load_window_data", dict)
    monkeypatch.setattr(vio, "build_timeline", spy)
    nf.main()
    assert seen and all(ctx is seen[0] for ctx in seen)  # één ctx voor warmup én forecast
    ctx = seen[0]
    raw = vp.neighbor_temp_estimate(rows, now)
    assert raw > vp.NEIGHBOR_SUMMER_CAP                  # de kap doet er in deze fixture echt toe
    assert ctx.neighbor_temp == pytest.approx(
        min(vp.NEIGHBOR_SUMMER_CAP, raw), abs=0.2)
    assert ctx.ground_temp == pytest.approx(vp.ground_temp_estimate(rows, now), abs=0.2)
    assert (ctx.lat, ctx.lon) == (52.09, 5.12)           # locatie uit het huismodel
    out = capsys.readouterr().out
    assert "Teds nacht" in out or "stil" in out          # bericht of seizoenspoort


# ── Open-Meteo-modelbias op de driver ──────────────────────────────────────────

def test_main_geeft_de_geleerde_om_bias_door_aan_build_timeline(monkeypatch, capsys):
    """Spiegel van de ctx-ankers-test: de driver-correctie moet écht doorgegeven
    worden. Vergeten = Teds voorspelling draait stil op de te warme nachtdriver, wat
    juist de reden was om 'm te bouwen — en niets zou dat zichtbaar maken."""
    rows = _rows(datetime.now(TZ))
    ob = {"night": 1.4, "day": 0.5, "n_night": 120, "n_day": 200}
    seen = []
    orig = vio.build_timeline

    def spy(*a, **kw):
        seen.append(kw.get("om_learned"))
        return orig(*a, **kw)

    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(vio, "load_house", lambda: HOUSE)
    monkeypatch.setattr(vio, "fetch_weather", lambda lat, lon: {"hourly": rows, "current": {}})
    monkeypatch.setattr(vio, "load_openings_log", lambda: [])
    monkeypatch.setattr(vio, "load_learned", dict)
    monkeypatch.setattr(vio, "load_window_data", lambda: {"om_bias": ob})
    monkeypatch.setattr(vio, "build_timeline", spy)
    nf.main()
    assert seen, "build_timeline is niet aangeroepen"
    assert all(s == ob for s in seen), (
        f"niet elke timeline kreeg de correctie mee: {seen}")


# ── anchor_mass_now: de massaknoop mee-ijken ───────────────────────────────────

def test_anchor_mass_now_ijkt_op_de_metingen():
    # De massaknoop stond op een weggedreven warmup-waarde; met metingen eromheen
    # moet hij naar het gedempte gemiddelde van die metingen toe.
    now = datetime(2026, 7, 20, 18, 45, tzinfo=TZ)
    actual = {"ted": [(now - timedelta(hours=h), 22.0) for h in range(24, 0, -1)]}
    out = nf.anchor_mass_now({"ted": 18.0}, actual, now)
    assert out["ted"] == pytest.approx(22.0, abs=0.05)


def test_anchor_mass_now_weegt_recent_zwaarder():
    # Gedempt gemiddelde: het recente verleden telt zwaarder dan de rand van het venster.
    now = datetime(2026, 7, 20, 18, 45, tzinfo=TZ)
    actual = {"ted": [(now - timedelta(hours=20), 18.0),
                      (now - timedelta(hours=1), 24.0)]}
    out = nf.anchor_mass_now({"ted": 15.0}, actual, now)
    assert out["ted"] > 21.0, "recente 24° moet zwaarder wegen dan de oude 18°"
    assert out["ted"] < 24.0


def test_anchor_mass_now_laat_kamers_zonder_metingen_staan():
    # Fail open, zoals anchor_now: geen meting → gesimuleerde waarde blijft.
    now = datetime(2026, 7, 20, 18, 45, tzinfo=TZ)
    out = nf.anchor_mass_now({"ted": 19.0, "stair": 20.5}, {"ted": [(now, 22.0)]}, now)
    assert out["stair"] == 20.5
    assert out["ted"] == pytest.approx(22.0)


def test_anchor_mass_now_muteert_de_invoer_niet():
    now = datetime(2026, 7, 20, 18, 45, tzinfo=TZ)
    tm = {"ted": 19.0}
    nf.anchor_mass_now(tm, {"ted": [(now, 23.0)]}, now)
    assert tm == {"ted": 19.0}


def test_anchor_mass_now_negeert_toekomstige_samples():
    now = datetime(2026, 7, 20, 18, 45, tzinfo=TZ)
    actual = {"ted": [(now + timedelta(hours=2), 30.0), (now - timedelta(hours=1), 21.0)]}
    out = nf.anchor_mass_now({"ted": 19.0}, actual, now)
    assert out["ted"] == pytest.approx(21.0, abs=0.01)


def test_anchor_mass_now_leeg_is_een_no_op():
    now = datetime(2026, 7, 20, 18, 45, tzinfo=TZ)
    assert nf.anchor_mass_now({"ted": 19.0}, {}, now) == {"ted": 19.0}
    assert nf.anchor_mass_now({"ted": 19.0}, {"ted": []}, now) == {"ted": 19.0}


def test_main_ijkt_de_massaknoop_mee(monkeypatch):
    """Wiring-test in het ctx-ankers-patroon: vergeten de massaknoop te ijken is
    stil en onzichtbaar — de voorspelling blijft draaien, alleen structureel te koud."""
    rows = _rows(datetime.now(TZ))
    gezien = []
    orig = vp.simulate

    def spy(house, params, timeline, seed, ctx, **kw):
        gezien.append(kw.get("tm_seed"))
        return orig(house, params, timeline, seed, ctx, **kw)

    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(vio, "load_house", lambda: HOUSE)
    monkeypatch.setattr(vio, "fetch_weather", lambda lat, lon: {"hourly": rows, "current": {}})
    monkeypatch.setattr(vio, "load_openings_log", lambda: [])
    monkeypatch.setattr(vio, "load_learned", dict)
    # Echte tado-historie zodat collect_actual samples oplevert: een kamer die de hele
    # dag stabiel 23° was. Zonder de fix erft de forecast de weggedrifte warmup-massa
    # (die van het koude weer in `rows` komt); mét de fix staat hij op ~23°.
    t0 = datetime.now(TZ)
    wd = {"rooms": {"Ted": {"history": [
        {"t": (t0 - timedelta(hours=h)).isoformat(), "temp": 23.0}
        for h in range(int(nf.WARMUP_H), 0, -1)]}}}
    monkeypatch.setattr(vio, "load_window_data", lambda: wd)
    monkeypatch.setattr(vp, "simulate", spy)
    nf.main()
    fcst = [s for s in gezien[1:] if s]
    assert fcst, "forecast-sim kreeg geen massaknoop mee"
    assert fcst[0].get("ted") == pytest.approx(23.0, abs=0.3), (
        f"massaknoop niet op de metingen geijkt (kreeg {fcst[0].get('ted')}) — "
        "de warmup-drift lekt de forecast in")

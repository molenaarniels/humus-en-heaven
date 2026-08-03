"""Pariteit oud→nieuw: de vent_*-modules zijn een woordelijke port van airflow_model.py.

Zelfde rekenkunde, alleen ander loodgieterswerk (RunContext i.p.v. module-globals) — dus
op identieke input horen de uitkomsten tot op 1e-9 gelijk te zijn. Elke afwijking is per
definitie een port-fout. Dit bestand is Fase-A/B-steigerwerk: het sneuvelt in Fase C
samen met airflow_model.py (zie het herbouwplan).
"""

from datetime import datetime, timedelta, timezone

import pytest

import airflow_model as am
import vent_fit as vf
import vent_io as vio
import vent_physics as vp

TZ = vp.TZ
NOW = datetime(2026, 8, 1, 15, 0, tzinfo=TZ)
TOL = 1e-9


@pytest.fixture()
def house():
    return vio.load_house()


@pytest.fixture()
def ctx(house, weather):
    return vio.make_context(house, weather, NOW)


@pytest.fixture()
def weather():
    """Deterministische weer-fixture in fetch_weather-vorm: 80u terug + 6u vooruit."""
    rows = []
    start = NOW - timedelta(hours=80)
    for i in range(87):
        t = start + timedelta(hours=i)
        h = t.hour
        sun = max(0.0, 700.0 * (1 - abs(h - 13.5) / 7.5)) if 6 <= h <= 21 else 0.0
        rows.append({
            "dt": t,
            "T_out": 18.0 + 8.0 * max(0.0, 1 - abs(h - 15) / 9.0) + 0.01 * (i % 7),
            "rh": 55.0 + (i % 11),
            "precip": 0.0,
            "wind_speed": 2.0 + 0.1 * (i % 5),
            "wind_dir": (200 + 3 * i) % 360,
            "gust": 4.0,
            "shortwave": sun,
            "direct": 0.6 * sun,
            "diffuse": 0.4 * sun,
        })
    cur = {"temperature_2m": 26.4, "relative_humidity_2m": 52.0,
           "wind_speed_10m": 2.4, "wind_direction_10m": 250.0,
           "wind_gusts_10m": 5.0, "shortwave_radiation": 430.0,
           "outside_source": "open-meteo"}
    return {"hourly": rows, "current": cur, "wu_solar_scale": None}


@pytest.fixture()
def log():
    """Openingen-log met raam/deur/rooster-mutaties + ac/pauze-sleutels in het venster."""
    t0 = (NOW - timedelta(hours=40)).isoformat()
    t1 = (NOW - timedelta(hours=20)).isoformat()
    t2 = (NOW - timedelta(hours=6)).isoformat()
    return [
        {"t": t0, "states": {"ted_small": "open", "ted_stair": "closed"}},
        {"t": t1, "states": {"ted_small": "closed", "office_roof": "tilt",
                             "ac_room": "", "paused": False}},
        {"t": t2, "states": {"living_garden_door": "open", "hotties_stair": "open"}},
    ]


def _sync_am_globals(house, ctx):
    """Zet de oude module-globals exact op de ctx-waarden (de oude main()-stappen)."""
    am._LAT, am._LON = ctx.lat, ctx.lon
    am._NEIGHBOR_TEMP = ctx.neighbor_temp
    am._GROUND_TEMP = ctx.ground_temp


def _timelines(house, weather, log, ctx, om_learned=None):
    _sync_am_globals(house, ctx)
    hours = am.CALIB_WINDOW_H + am.WARMUP_H
    tl_old = am.build_timeline(house, weather, log, NOW, hours,
                               wu_solar_scale=1.1, beam_iam=True, om_learned=om_learned)
    tl_new = vio.build_timeline(house, weather, log, NOW, hours, ctx,
                                wu_solar_scale=1.1, beam_iam=True, om_learned=om_learned)
    return tl_old, tl_new


def _seed(house, tl):
    return {rid: tl[0]["T_out"] + 2.0 + 0.1 * k
            for k, rid in enumerate(house.get("rooms", {}))}


def _synth_actual(house, sim_series):
    """Kunstmatige metingen: de oude sim-serie ±een deterministische offset, om het uur."""
    out = {}
    for rid, series in sim_series.items():
        pts = [(t, v + 0.4 - 0.05 * (i % 5)) for i, (t, v) in enumerate(series)
               if t.minute == 0 and t <= NOW]
        if pts:
            out[rid] = pts[::2]
    return out


def test_context_ankers_gelijk_aan_oude_schattingen(house, weather, ctx):
    verwacht_nb = min(am.NEIGHBOR_SUMMER_CAP,
                      am.neighbor_temp_estimate(weather["hourly"], NOW))
    assert ctx.neighbor_temp == pytest.approx(verwacht_nb, abs=TOL)
    assert ctx.ground_temp == pytest.approx(
        am.ground_temp_estimate(weather["hourly"], NOW), abs=TOL)


def test_zonnestand_en_gevelinstraling_pariteit(ctx):
    for h in (6, 9, 12, 15, 18, 21):
        t = NOW.replace(hour=h).astimezone(timezone.utc)
        az_o, el_o = am.sun_position(ctx.lat, ctx.lon, t)
        az_n, el_n = vp.sun_position(ctx.lat, ctx.lon, t)
        assert az_n == pytest.approx(az_o, abs=TOL)
        assert el_n == pytest.approx(el_o, abs=TOL)
        for facade in (0, 90, 219, 309):
            for tilt in (90.0, 15.0, 0.0):
                io_ = am.facade_irradiance(400.0, 180.0, az_o, el_o, facade,
                                           tilt_deg=tilt, beam_iam=True)
                in_ = vp.facade_irradiance(400.0, 180.0, az_n, el_n, facade,
                                           tilt_deg=tilt, beam_iam=True)
                assert in_ == pytest.approx(io_, abs=TOL)


def test_openingen_en_netwerk_pariteit(house, weather, log, ctx):
    states = am.openings_at(log, NOW)
    assert vio.openings_at(log, NOW) == states
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    ta = {z: 24.0 + 0.3 * k for k, z in enumerate(zones)}
    wx = {"wind_speed": 3.2, "wind_dir": 250.0, "gust": 5.0, "precip": 0.0}
    params = am.default_params(house)
    ops_o = am.build_openings(house, states, wx, params, ta, 21.0)
    ops_n = vp.build_openings(house, states, wx, params, ta, 21.0)
    assert len(ops_o) == len(ops_n)
    for a, b in zip(ops_o, ops_n):
        assert a.keys() == b.keys()
        for k in a:
            if isinstance(a[k], float):
                assert b[k] == pytest.approx(a[k], abs=TOL), (a["id"], k)
            else:
                assert a[k] == b[k]
    net_o = am.solve_network(zones, ops_o, ta, 21.0)
    net_n = vp.solve_network(zones, ops_n, ta, 21.0)
    assert net_n["converged"] == net_o["converged"]
    for qo, qn in zip(net_o["flows"], net_n["flows"]):
        assert qn == pytest.approx(qo, abs=TOL)
    for z in zones:
        assert net_n["fresh"].get(z, 0.0) == pytest.approx(net_o["fresh"].get(z, 0.0), abs=TOL)
    ef_o = am.effective_fresh(net_o["fresh"], house, states, wx, ta, 21.0)
    ef_n = vp.effective_fresh(net_n["fresh"], house, states, wx, ta, 21.0)
    for z in set(ef_o) | set(ef_n):
        assert ef_n.get(z, 0.0) == pytest.approx(ef_o.get(z, 0.0), abs=TOL)
    assert vp.buoyant_door_exchange(1.6, 26.0, 22.5) == pytest.approx(
        am.buoyant_door_exchange(1.6, 26.0, 22.5), abs=TOL)


def test_timeline_pariteit(house, weather, log, ctx):
    tl_old, tl_new = _timelines(house, weather, log, ctx)
    assert len(tl_old) == len(tl_new) > 0
    for so, sn in zip(tl_old, tl_new):
        assert sn["t"] == so["t"]
        assert sn["T_out"] == pytest.approx(so["T_out"], abs=TOL)
        assert sn["states"] == so["states"]
        assert set(sn["irr"]) == set(so["irr"])
        for rid in so["irr"]:
            assert sn["irr"][rid] == pytest.approx(so["irr"][rid], abs=TOL)


def test_timeline_pariteit_met_om_bias(house, weather, log, ctx):
    om_learned = {"night": 1.4, "day": 0.6, "n_night": 120, "n_day": 200}
    tl_old, tl_new = _timelines(house, weather, log, ctx, om_learned=om_learned)
    for so, sn in zip(tl_old, tl_new):
        assert sn["T_out"] == pytest.approx(so["T_out"], abs=TOL)
        assert sn["weather"].get("rh") == pytest.approx(so["weather"].get("rh") or 0.0,
                                                        abs=TOL)


def test_simulate_pariteit(house, weather, log, ctx):
    tl_old, tl_new = _timelines(house, weather, log, ctx)
    seed = _seed(house, tl_old)
    tm_seed = {rid: seed[rid] - 0.8 for rid in seed}
    measured = {rid: [(NOW - timedelta(hours=k), 23.0 + 0.2 * k) for k in range(6)]
                for rid in house.get("rooms", {})}
    sim_o = am.simulate(house, am.default_params(house), tl_old, seed,
                        snapshot_t=NOW, tm_seed=tm_seed, measured=measured)
    sim_n = vp.simulate(house, vio.default_params(house), tl_new, seed, ctx,
                        snapshot_t=NOW, tm_seed=tm_seed, measured=measured)
    assert sim_n.get("solver_failures", 0) == sim_o.get("solver_failures", 0)
    assert set(sim_n["series"]) == set(sim_o["series"])
    for rid in sim_o["series"]:
        so, sn = sim_o["series"][rid], sim_n["series"][rid]
        assert len(so) == len(sn)
        for (to, vo), (tn, vn) in zip(so, sn):
            assert tn == to
            assert vn == pytest.approx(vo, abs=TOL), rid
    for z in sim_o["Ta_now"]:
        assert sim_n["Ta_now"][z] == pytest.approx(sim_o["Ta_now"][z], abs=TOL)
        assert sim_n["Tm_now"][z] == pytest.approx(sim_o["Tm_now"][z], abs=TOL)


def test_calibrate_een_stap_pariteit(house, weather, log, ctx):
    tl_old, tl_new = _timelines(house, weather, log, ctx)
    seed = _seed(house, tl_old)
    params = am.default_params(house)
    sim_o = am.simulate(house, params, tl_old, seed)
    actual = _synth_actual(house, sim_o["series"])
    assert actual, "fixture hoort samples op te leveren"
    r_o = am._residuals(house, params, tl_old, seed, actual, set(actual))
    r_n = vf._residuals(house, params, tl_new, seed, ctx, actual, set(actual))
    assert len(r_o) == len(r_n) > 0
    for a, b in zip(r_o, r_n):
        assert b == pytest.approx(a, abs=TOL)
    p_o, rmse_o = am.calibrate(house, params, tl_old, seed, actual,
                               max_iter=2, time_budget_s=1e9, solar_mean=260.0)
    p_n, rmse_n = vf.calibrate(house, params, tl_new, seed, ctx, actual,
                               max_iter=2, time_budget_s=1e9, solar_mean=260.0)
    assert rmse_n == pytest.approx(rmse_o, abs=TOL)
    for g in vp.GLOBAL_PARAMS:
        assert p_n[g] == pytest.approx(p_o[g], abs=TOL), g
    for rid in house.get("rooms", {}):
        for k in vp.PER_ROOM_PARAMS:
            assert p_n[rid][k] == pytest.approx(p_o[rid][k], abs=TOL), (rid, k)

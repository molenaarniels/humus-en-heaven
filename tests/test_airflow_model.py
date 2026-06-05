"""Regressie-vangnet voor de ventilatie-digital-twin (`airflow_model.py`, Project 8).

Pure-functie-checks op de fysica-kern — géén netwerk, géén Gist, géén bestanden:
  1. Zonpositie tegen bekende referentiewaarden.
  2. Cp-druk: teken + symmetrie.
  3. Luchtstroomnetwerk: massabehoud + een analytische cross-ventilatie-case.
  4. 2-knoops RC-model: relaxeert naar de juiste evenwichtstemp; de massaknoop loopt
     achter op de luchtknoop.
  5. Kalibratie herstelt bekende parameters uit synthetische data (RMSE daalt).
  6. openings_at: tijdlijn-reconstructie uit een meervoudige log.
  7. suggest: "alles dicht" als het buiten warmer is dan elke kamer.
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

import airflow_model as am


# ── 1. Zonpositie ────────────────────────────────────────────────────────────────────

def test_sun_position_summer_noon_utrecht():
    # 21 juni, ware zonnemiddag ~11:42 UTC voor Utrecht (lon 5.12°O: 12:00 − 20½min −
    # eqtime). Op ware middag staat de zon pal zuid (az≈180) en op maximale hoogte
    # (≈90−(lat−23.44)=61°).
    az, el = am.sun_position(52.09, 5.12, datetime(2026, 6, 21, 11, 42, tzinfo=timezone.utc))
    assert abs(az - 180.0) < 4.0
    assert abs(el - 61.3) < 2.0


def test_sun_position_night_below_horizon():
    # Middernacht UTC in juni → zon onder de horizon (negatieve elevatie).
    _, el = am.sun_position(52.09, 5.12, datetime(2026, 6, 21, 0, 0, tzinfo=timezone.utc))
    assert el < 0.0


def test_sun_morning_east_afternoon_west():
    az_morning, _ = am.sun_position(52.09, 5.12, datetime(2026, 6, 21, 5, 0, tzinfo=timezone.utc))
    az_afternoon, _ = am.sun_position(52.09, 5.12, datetime(2026, 6, 21, 16, 0, tzinfo=timezone.utc))
    assert az_morning < 180.0      # ochtend: zon in het oosten
    assert az_afternoon > 180.0    # middag/avond: zon in het westen


# ── 2. Cp-druk ───────────────────────────────────────────────────────────────────────

def test_cp_sign():
    assert am.cp_coefficient(0) > 0.5      # loef: positieve druk
    assert am.cp_coefficient(90) < 0.0     # zijgevel: onderdruk
    assert am.cp_coefficient(180) < 0.0    # lij: onderdruk


def test_cp_symmetry():
    for theta in (10, 45, 70, 120):
        assert am.cp_coefficient(theta) == pytest.approx(am.cp_coefficient(-theta), abs=1e-9)


# ── 3. Luchtstroomnetwerk ────────────────────────────────────────────────────────────

def test_crossvent_mass_balance_and_analytic():
    # Eén kamer, twee ramen: loef (+Pe) en lij (−Pe). De instroom moet de uitstroom
    # exact compenseren, en het debiet moet de orifice-wet volgen.
    Pe = 5.0
    Cd, A = 0.62, 0.5
    ops = [
        {"a": "room", "b": "outside", "area": A, "Cd": Cd, "z": 1.5, "Pe": +Pe, "id": "w1"},
        {"a": "room", "b": "outside", "area": A, "Cd": Cd, "z": 1.5, "Pe": -Pe, "id": "w2"},
    ]
    net = am.solve_network(["room"], ops, {"room": 22.0}, 22.0)
    # Volumebehoud (gelijke dichtheid binnen/buiten hier): in = uit.
    assert net["flows"][0] == pytest.approx(-net["flows"][1], abs=1e-3)
    # Symmetrie → kamerdruk ≈ 0 → ΔP per raam ≈ Pe.
    rho = am.air_density(22.0)
    q_expected = Cd * A * math.sqrt(2.0 * Pe / rho)
    assert abs(net["flows"][1]) == pytest.approx(q_expected, rel=0.02)


def test_sealed_zone_does_not_break_solve():
    # Een volledig dichte zone (geen open opening) mag de hele drukoplossing niet singulier
    # maken — de per-zone infiltratielek houdt 'm welgesteld.
    house = _toy_house()
    house["junctions"]["sealed"] = {"volume_m3": 8}     # nergens mee verbonden
    house["rooms"]["a"]["from_window_data"] = "Living room"
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    params = am.default_params(house)
    zt = {z: 24.0 for z in zones}
    ops = am.build_openings(house, {"a_win": "open"}, {"wind_speed": 4.0, "wind_dir": 200.0},
                            params, zt, 18.0)
    net = am.solve_network(zones, ops, zt, 18.0)
    assert all(math.isfinite(v) for v in net["pressures"].values())
    res = _node_residual(zones, ops, net["pressures"], zt, 18.0)
    assert max(abs(v) for v in res) < 1e-4


def test_network_node_mass_balance_multizone():
    # Twee kamers + een deur, wind + schoorsteen: in elke interne knoop moet de
    # netto massa nul zijn (behoudswet).
    house = _toy_house()
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    params = am.default_params(house)
    states = {"a_win": "open", "b_win": "open"}
    zt = {"a": 26.0, "b": 24.0, "hall": 25.0}
    ops = am.build_openings(house, states, {"wind_speed": 4.0, "wind_dir": 200.0},
                            params, zt, 18.0)
    net = am.solve_network(zones, ops, zt, 18.0)
    # Reconstrueer netto massa per interne knoop uit de drukken.
    res = _node_residual(zones, ops, net["pressures"], zt, 18.0)
    assert max(abs(v) for v in res) < 1e-4


# ── 4. 2-knoops RC-model ─────────────────────────────────────────────────────────────

def test_rc_relaxes_to_outside_no_solar():
    # Geen zon, dichte ramen, constante buitentemp → binnen relaxeert náár buiten. Het
    # gebouw heeft een grote thermische tijdconstante (~dag), dus geef het ruim de tijd.
    house = _toy_house()
    params = am.default_params(house)
    T_out = 18.0
    tl = _const_timeline(T_out, hours=240, irr=0.0)
    seed = {z: 26.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    sim = am.simulate(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    for rid in house["rooms"]:
        assert sim["Ta"][rid] == pytest.approx(T_out, abs=0.4)
        # Monotone afkoeling: eindtemp ligt tussen buiten en de startwaarde.
        assert T_out <= sim["Ta"][rid] < 26.0


def test_rc_mass_node_lags_air_node():
    # Een sprong in de buitentemp: de luchtknoop reageert sneller dan de massaknoop,
    # dus na korte tijd ligt T_air dichter bij buiten dan T_mass.
    house = _toy_house()
    params = am.default_params(house)
    tl = _const_timeline(30.0, hours=3, irr=0.0)   # warm buiten, koud gestart
    seed = {z: 18.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    sim = am.simulate(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    rid = next(iter(house["rooms"]))
    assert sim["Ta"][rid] > sim["Tm"][rid]          # lucht warmde sneller op dan massa
    assert seed[rid] < sim["Tm"][rid] < sim["Ta"][rid] < 30.0


# ── 5. Kalibratie ────────────────────────────────────────────────────────────────────

def test_calibrate_reduces_error():
    # Het leren moet de voorspelfout aantoonbaar verkleinen. We toetsen de *fout* (robuust),
    # niet of individuele parameters exact teruggevonden worden — over één korte venster met
    # alle parameters vrij is dat onderbepaald (meerdere combinaties passen even goed). De
    # parameter-convergentie naar de waarheid komt over meerdere runs (online leren).
    house = _toy_house()
    tl = _varying_timeline(hours=24)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    truth = am.default_params(house)
    for rid in house["rooms"]:
        truth[rid]["c_air"] = 2.0
        truth[rid]["solar_gain"] = 1.7
    sim_truth = am.simulate(house, truth, tl, seed, calib_only_rooms=set(house["rooms"]))
    actual = {rid: sim_truth["series"][rid][::4] for rid in house["rooms"]}  # elk uur

    p0 = am.default_params(house)
    rmse0 = am.rmse(am._residuals(house, p0, tl, seed, actual, set(house["rooms"])))
    p1, rmse1 = am.calibrate(house, p0, tl, seed, actual, max_iter=6, time_budget_s=20)
    assert rmse1 < rmse0 * 0.7                       # minstens 30% beter
    # Herhaald leren blijft verbeteren (of stabiel): tweede ronde niet slechter.
    _, rmse2 = am.calibrate(house, p1, tl, seed, actual, max_iter=6, time_budget_s=20)
    assert rmse2 <= rmse1 + 1e-6


# ── 6. openings_at ───────────────────────────────────────────────────────────────────

def test_openings_at_timeline():
    t0 = datetime(2026, 6, 1, 8, 0, tzinfo=am.TZ)
    log = [
        {"t": t0.isoformat(), "states": {"w": "open"}},
        {"t": (t0 + timedelta(hours=4)).isoformat(), "states": {"w": "closed"}},
    ]
    # Vóór de eerste log: leeg.
    assert am.openings_at(log, t0 - timedelta(hours=1)) == {}
    # Tussen log 1 en 2: eerste snapshot geldt.
    assert am.openings_at(log, t0 + timedelta(hours=2))["w"] == "open"
    # Na log 2: tweede snapshot geldt.
    assert am.openings_at(log, t0 + timedelta(hours=6))["w"] == "closed"


def test_open_frac_mapping():
    elem = {"tilt_frac": 0.2}
    assert am._open_frac("open", elem) == 1.0
    assert am._open_frac("dicht", elem) == 0.0
    assert am._open_frac("tilt", elem) == 0.2
    assert am._open_frac(0.5, elem) == 0.5


# ── 7. suggest ───────────────────────────────────────────────────────────────────────

def test_suggest_keep_closed_when_outside_hot():
    house = _toy_house()
    params = am.default_params(house)
    # Buiten 30°C, kamers 24°C → openen kan niet koelen → alles dicht.
    room_now = {house["rooms"][r]["from_window_data"]: 24.0 for r in house["rooms"]}
    sugg = am.suggest(house, params, {"wind_speed": 3.0, "wind_dir": 200.0, "gust": 5.0, "precip": 0.0},
                      room_now, 30.0, 50.0, 30.0)
    assert sugg["keep_closed"] is True
    assert all(i["action"] == "dicht" for i in sugg["instructions"])


def test_suggest_opens_when_outside_cooler():
    house = _toy_house()
    params = am.default_params(house)
    # Buiten 16°C, kamers warm 27°C → openen koelt → minstens één raam open.
    room_now = {house["rooms"][r]["from_window_data"]: 27.0 for r in house["rooms"]}
    sugg = am.suggest(house, params, {"wind_speed": 3.0, "wind_dir": 200.0, "gust": 5.0, "precip": 0.0},
                      room_now, 16.0, 55.0, 16.0)
    assert sugg["keep_closed"] is False
    assert any(i["action"] == "open" for i in sugg["instructions"])


# ════════════════════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════════════════════

def _toy_house() -> dict:
    return {
        "location": {"lat": 52.09, "lon": 5.12},
        "rooms": {
            "a": {"from_window_data": "Living room", "volume_m3": 50, "exterior_wall_m2": 16, "plan_xy": [1, 1]},
            "b": {"from_window_data": "office", "volume_m3": 30, "exterior_wall_m2": 10, "plan_xy": [2, 1]},
        },
        "junctions": {"hall": {"volume_m3": 12}},
        "windows": {
            "a_win": {"room": "a", "facade_azimuth_deg": 180, "area_m2": 1.5, "glass_m2": 1.2,
                      "max_open_area_m2": 0.6, "tilt_frac": 0.15, "center_height_m": 1.5},
            "b_win": {"room": "b", "facade_azimuth_deg": 0, "area_m2": 1.2, "glass_m2": 1.0,
                      "max_open_area_m2": 0.5, "tilt_frac": 0.15, "center_height_m": 1.5},
        },
        "vents": {},
        "doors": {
            "a_hall": {"between": ["a", "hall"], "area_m2": 1.8, "center_height_m": 1.0, "default_state": "open"},
            "b_hall": {"between": ["b", "hall"], "area_m2": 1.6, "center_height_m": 1.0, "default_state": "open"},
        },
    }


def _const_timeline(T_out: float, hours: int, irr: float) -> list[dict]:
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=am.TZ)
    rooms = ["a", "b"]
    grid = []
    for i in range(hours * 4 + 1):
        t = t0 + timedelta(minutes=15 * i)
        grid.append({"t": t, "T_out": T_out, "irr": {r: irr for r in rooms}, "states": {},
                     "weather": {"wind_speed": 1.0, "wind_dir": 200.0, "gust": 2.0, "precip": 0.0,
                                 "direct": 0.0, "diffuse": 0.0, "rh": 60}, "dt": 900.0})
    return grid


def _varying_timeline(hours: int) -> list[dict]:
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=am.TZ)
    grid = []
    for i in range(hours * 4 + 1):
        t = t0 + timedelta(minutes=15 * i)
        hr = t.hour + t.minute / 60.0
        T_out = 16.0 + 6.0 * math.sin((hr - 9) / 24 * 2 * math.pi)
        s = max(0.0, math.sin((hr - 6) / 12 * math.pi)) * 400 if 6 < hr < 18 else 0.0
        st = {"a_win": "open"} if 12 < hr < 16 else {}
        grid.append({"t": t, "T_out": T_out, "irr": {"a": s, "b": s * 0.3}, "states": st,
                     "weather": {"wind_speed": 3.0, "wind_dir": 210.0, "gust": 5.0, "precip": 0.0,
                                 "direct": 0.0, "diffuse": 0.0, "rh": 55}, "dt": 900.0})
    return grid


def _node_residual(zones, ops, pressures, zt, T_out):
    """Netto massadebiet per interne knoop, gegeven opgeloste drukken (voor de balanscheck)."""
    idx = {z: i for i, z in enumerate(zones)}
    P = [pressures[z] for z in zones]
    rho_out = am.air_density(T_out)
    rho_z = {z: am.air_density(zt.get(z, T_out)) for z in zones}
    res = [0.0] * len(zones)
    for op in ops:
        ia = idx[op["a"]]
        z = op["z"]
        ra = rho_z[op["a"]]
        Pa = P[ia] - ra * am.G * z
        if op["b"] == "outside":
            Pb = op["Pe"] - rho_out * am.G * z
            rb = rho_out
        else:
            rb = rho_z[op["b"]]
            Pb = P[idx[op["b"]]] - rb * am.G * z
        md = am._massflow(Pa - Pb, op["Cd"], op["area"], ra, rb)
        res[ia] += md
        if op["b"] != "outside":
            res[idx[op["b"]]] -= md
    return res

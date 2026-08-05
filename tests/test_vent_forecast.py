"""Regressie-vangnet voor `vent_forecast.py` — de 12-uurs vooruitblik + de browser-payload.

Drie dingen die stuk kúnnen zonder dat iets anders piept:

  1. **Het herankeren.** De hele winst van de vooruitblik zit erin dat hij op de gemeten
     toestand start i.p.v. op de gesimuleerde, en dat de massaknoop mét de luchtknoop
     meeschuift. Zou die tweede helft ooit sneuvelen (of stilletjes `tm_mode="warm"`
     worden), dan trekt de h_am-koppeling de lucht binnen twee stappen terug naar de
     sim-waarde en verandert er verder niets zichtbaars — precies het soort regressie dat
     een schema-test niet vangt.
  2. **De sensor-inversie.** Zaaien met een sensorwaarde en scoren in sensorruimte past de
     buitenmuur-blend twee keer toe. `air_from_sensor` moet exact de omkering van
     `_sensor_temp` zijn.
  3. **Het kolomcontract van de payload.** De elementvolgorde in `driver_export` zet de
     invoerkolommen van het luchtstroom-surrogaat; schuift die, dan rekent de speeltuin
     stil met de verkeerde ramen.
"""
from datetime import datetime, timedelta

import pytest

import vent_forecast as vfc
import vent_io as vio
import vent_physics as vp

TZ = vio.TZ
NOW = datetime(2026, 8, 1, 15, 0, tzinfo=TZ)


def _weather_fixture() -> dict:
    rows = []
    start = NOW - timedelta(hours=80)
    for i in range(100):
        t = start + timedelta(hours=i)
        h = t.hour
        sun = max(0.0, 700.0 * (1 - abs(h - 13.5) / 7.5)) if 6 <= h <= 21 else 0.0
        rows.append({
            "dt": t,
            "T_out": 18.0 + 8.0 * max(0.0, 1 - abs(h - 15) / 9.0) + 0.01 * (i % 7),
            "rh": 55.0 + (i % 11), "precip": 0.0,
            "wind_speed": 2.0 + 0.1 * (i % 5), "wind_dir": (200 + 3 * i) % 360,
            "gust": 4.0, "shortwave": sun, "direct": 0.6 * sun, "diffuse": 0.4 * sun,
        })
    return {"hourly": rows, "current": {}, "wu_solar_scale": None}


@pytest.fixture(scope="module")
def setup():
    house = vio.load_house()
    weather = _weather_fixture()
    ctx = vio.make_context(house, weather, NOW)
    params = vio.default_params(house)
    timeline = vio.build_timeline(house, weather, [], NOW, 24.0, ctx,
                                  end_h=vfc.FORECAST_H)
    seed = {rid: 23.0 + 0.2 * k for k, rid in enumerate(house["rooms"])}
    sim = vp.simulate(house, params, timeline, seed, ctx,
                      calib_only_rooms=set(house["rooms"]), snapshot_t=NOW)
    return {"house": house, "ctx": ctx, "params": params, "timeline": timeline, "sim": sim}


# ── 1. Sensor-inversie ───────────────────────────────────────────────────────────────

def test_air_from_sensor_is_de_omkering_van_sensor_temp():
    house = vio.load_house()
    # hotties heeft sensor_outdoor_frac 0.15; living niet.
    frac = house["rooms"]["hotties"]["sensor_outdoor_frac"]
    assert frac > 0
    ta, t_out = 24.0, 15.0
    ts = vp._sensor_temp(ta, t_out, frac)
    assert ts != ta                                    # de blend doet écht iets
    assert vp.air_from_sensor(house, "hotties", ts, t_out) == pytest.approx(ta)


def test_air_from_sensor_is_noop_zonder_bias():
    house = vio.load_house()
    assert vp.air_from_sensor(house, "living", 22.5, 15.0) == pytest.approx(22.5)
    # Ontbrekende buitentemp → niets te inverteren, geef de meting ongewijzigd terug.
    assert vp.air_from_sensor(house, "hotties", 22.5, None) == pytest.approx(22.5)


# ── 2. Herankeren: lucht op de meting, massa schuift mee ─────────────────────────────

def test_anchor_seed_verschuift_de_massaknoop_mee(setup):
    house, sim = setup["house"], setup["sim"]
    ta0 = sim["Ta_now"]["ted"]
    tm0 = sim["Tm_now"]["ted"]
    meting = ta0 + 1.5                                 # geen sensorbias op ted → 1-op-1
    seed, tm, n = vfc.anchor_seed(house, sim, {"ted": meting}, 18.0)
    assert n == 1
    assert seed["ted"] == pytest.approx(meting)
    # De (Ta − Tm)-differentie codeert of de kamer op- of ontlaadt en blijft behouden.
    assert tm["ted"] - seed["ted"] == pytest.approx(tm0 - ta0)
    assert tm["ted"] == pytest.approx(tm0 + 1.5)


def test_anchor_seed_warm_modus_laat_de_massa_staan(setup):
    house, sim = setup["house"], setup["sim"]
    tm0 = sim["Tm_now"]["ted"]
    _seed, tm, _n = vfc.anchor_seed(house, sim, {"ted": sim["Ta_now"]["ted"] + 1.5},
                                    18.0, tm_mode="warm")
    assert tm["ted"] == pytest.approx(tm0)             # de naïeve ablatie, bewust bereikbaar


def test_anchor_seed_laat_ongemeten_zones_met_rust(setup):
    house, sim = setup["house"], setup["sim"]
    seed, tm, n = vfc.anchor_seed(house, sim, {"ted": 25.0}, 18.0)
    assert n == 1
    for z in ("living", "hotties", "stair", "hall"):
        if z in sim["Ta_now"]:
            assert seed[z] == pytest.approx(sim["Ta_now"][z])
            assert tm[z] == pytest.approx(sim["Tm_now"][z])


def test_anchor_seed_inverteert_de_sensorbias(setup):
    """Een gebiasde kamer moet de LUCHT-waarde als zaad krijgen, niet de sensorwaarde —
    anders gaat de blend er in `_to_sensor_series` nog een keer overheen."""
    house, sim = setup["house"], setup["sim"]
    seed, _tm, _n = vfc.anchor_seed(house, sim, {"hotties": 24.0}, 15.0)
    assert seed["hotties"] == pytest.approx(vp.air_from_sensor(house, "hotties", 24.0, 15.0))
    assert seed["hotties"] > 24.0                      # buiten kouder → lucht wármer dan de voeler


# ── 3. De vooruitblik zelf ──────────────────────────────────────────────────────────

def test_forecast_start_op_de_meting_en_loopt_tot_de_horizon(setup):
    house, sim = setup["house"], setup["sim"]
    meting = sim["Ta_now"]["ted"] + 2.0
    fc = vfc.forecast(house, setup["params"], setup["timeline"], sim, NOW, setup["ctx"],
                      {"ted": meting})
    assert fc["anchored"] == 1
    assert fc["steps"][0]["t"] >= NOW
    span_h = (fc["steps"][-1]["t"] - fc["steps"][0]["t"]).total_seconds() / 3600.0
    assert span_h == pytest.approx(vfc.FORECAST_H, abs=0.3)
    # De vooruitblik vertrekt vanaf de MÉTING, niet vanaf de sim-toestand. Het eerste punt
    # draagt al één integratiestap (zie de labelconventie in vent_forecast.forecast), dus het
    # ligt niet exact op het anker — maar het moet er duidelijk dichter bij liggen dan bij de
    # ongeankerde sim-waarde, anders doet het herankeren niets.
    t0, v0 = fc["series"]["ted"][0]
    assert t0 == fc["steps"][0]["t"]
    sim_waarde = sim["Ta_now"]["ted"]
    assert abs(v0 - meting) < abs(v0 - sim_waarde)
    assert abs(v0 - meting) < 0.5 * abs(meting - sim_waarde)


def test_forecast_zonder_toekomst_geeft_lege_reeksen(setup):
    house = setup["house"]
    verleden = [s for s in setup["timeline"] if s["t"] < NOW]
    fc = vfc.forecast(house, setup["params"], verleden, setup["sim"], NOW, setup["ctx"], {})
    assert fc["series"] == {} and fc["steps"] == [] and fc["anchored"] == 0


def test_latest_actual_negeert_verouderde_metingen():
    vers = {"ted": [(NOW - timedelta(minutes=20), 21.0), (NOW - timedelta(minutes=5), 22.0)]}
    oud = {"ted": [(NOW - timedelta(hours=6), 21.0)]}
    assert vfc.latest_actual(vers, NOW) == {"ted": 22.0}     # nieuwste sample wint
    assert vfc.latest_actual(oud, NOW) == {}                 # te oud → geen anker
    assert vfc.latest_actual({}, NOW) == {}


# ── 4. De browser-payload ───────────────────────────────────────────────────────────

def test_driver_export_schema(setup):
    house, sim = setup["house"], setup["sim"]
    fore = [s for s in setup["timeline"] if s["t"] >= NOW]
    meta = vfc.driver_export(house, setup["params"], setup["ctx"], fore,
                             sim["Ta_now"], sim["Tm_now"], now=NOW,
                             past_steps=[s for s in setup["timeline"] if s["t"] < NOW][-12:],
                             actual_series={"ted": [(NOW, 22.4)]})
    verwacht = {"zones", "sensor_rooms", "room_labels", "volumes", "elements", "doors",
                "strat", "consts", "ss_windows", "sensor_outdoor_frac", "par", "ginter",
                "ground_temp", "neighbor_temp", "vent_eff", "cp_shelter", "rho_cp",
                "substep_s", "t0", "seed_Ta", "seed_Tm", "steps", "past", "actual"}
    assert verwacht <= set(meta)
    assert meta["t0"] == NOW.isoformat()
    assert meta["zones"] == list(house["rooms"]) + list(house.get("junctions", {}))
    assert set(meta["sensor_rooms"]) == {"living", "ted", "hotties", "office", "bath"}
    # `hidden_rooms` staat NAAST sensor_rooms: die lijst is het kolomcontract van de
    # JS-kern + de golden-vector, dus een weergavekeuze mag er niet in snijden. bath staat
    # dus in béíde — ze wordt volledig meegesimuleerd, alleen niet getekend.
    assert set(meta["hidden_rooms"]) == {"bath", "stair"}
    assert "bath" in meta["sensor_rooms"]
    assert meta["past"] and meta["actual"]["ted"]
    # Elke stap draagt precies wat de JS-kern niet zelf kan uitrekenen.
    step = meta["steps"][0]
    assert {"t", "dt", "T_out", "irr", "t_solair", "nb_now", "int_profile",
            "wind_speed", "wind_dir", "states"} <= set(step)
    assert set(step["irr"]) == set(meta["zones"])
    assert set(step["t_solair"]) == set(meta["zones"])
    # Alleen string-standen: `ac_room`/`paused` zijn boekhouding, geen openingen.
    assert all(isinstance(v, str) for v in step["states"].values())
    # De thermische params per zone, in de vorm die vent_core.js indexeert.
    for z in meta["zones"]:
        assert {"C_a", "C_m", "H_am", "UA_env", "UA_mass", "solar", "f_air"} <= set(meta["par"][z])


def test_driver_export_elementvolgorde_is_het_surrogaat_contract(setup):
    """De volgorde van `operable_elements` zet de `input_cols` van het surrogaat. Schuift
    die, dan leest het netwerk de verkeerde ramen — zonder dat er iets omvalt."""
    house = setup["house"]
    ids = [eid for eid, _ in vfc.operable_elements(house)]
    # ramen → roosters → deuren, elk in house_model-volgorde.
    verwacht = ([w for w, c in house["windows"].items() if c.get("max_open_area_m2", 0) > 0]
                + [v for v, c in house["vents"].items()
                   if c.get("max_open_area_m2", c.get("area_m2", 0)) > 0]
                + [d for d, c in house["doors"].items() if c.get("area_m2", 0) > 0])
    assert ids == verwacht
    # Vast glas hoort er niet in (kan geen lucht doorlaten).
    vast = [w for w, c in house["windows"].items() if c.get("max_open_area_m2", 0) <= 0]
    assert not (set(vast) & set(ids))


def test_driver_export_element_fracties_komen_uit_de_echte_parser(setup):
    house = setup["house"]
    fore = [s for s in setup["timeline"] if s["t"] >= NOW]
    meta = vfc.driver_export(house, setup["params"], setup["ctx"], fore,
                             setup["sim"]["Ta_now"], setup["sim"]["Tm_now"], now=NOW)
    for e in meta["elements"]:
        elem = (house.get("windows", {}).get(e["id"]) or house.get("vents", {}).get(e["id"])
                or house.get("doors", {}).get(e["id"]))
        assert e["frac"]["dicht"] == pytest.approx(vp._open_frac("dicht", elem))
        assert e["frac"]["open"] == pytest.approx(vp._open_frac("open", elem))
        assert e["default"] == pytest.approx(vp._default_frac(elem, e["kind"]))

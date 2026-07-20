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


def test_cp_roof_always_suction():
    # Een (bijna) plat dak staat op élke windrichting onder onderdruk (geen loeflob).
    for theta in range(0, 361, 15):
        assert am.cp_roof(theta) < 0.0
    # Loefrand iets minder negatief dan de lijrand.
    assert am.cp_roof(0) > am.cp_roof(180)


def test_cp_tilted_endpoints():
    # tilt 90° (verticaal, default) → exact het muurprofiel: backward-compatible.
    for theta in (0, 45, 90, 180):
        assert am.cp_tilted(theta, 90.0) == pytest.approx(am.cp_coefficient(theta), abs=1e-12)
        assert am.cp_tilted(theta, 0.0) == pytest.approx(am.cp_roof(theta), abs=1e-12)
    # Een plat dakraam op de loef heeft géén overdruk meer (muur wél).
    assert am.cp_coefficient(0) > 0.5
    assert am.cp_tilted(0, 0.0) < 0.0


def test_facade_irradiance_default_unchanged():
    # Zonder diffuse_only-argument blijft de instraling identiek (backward-compatible).
    i_default = am.facade_irradiance(219.0, 219.0, 45.0, 700.0, 150.0, 90.0)
    i_explicit = am.facade_irradiance(219.0, 219.0, 45.0, 700.0, 150.0, 90.0, False)
    assert i_default == pytest.approx(i_explicit, abs=1e-12)
    assert i_default > 150.0  # bevat de directe beam-bijdrage


def test_facade_irradiance_diffuse_only_drops_beam():
    # Zon recht op een ZW-raam: normaal véél directe instraling, diffuse_only laat alleen
    # de hemel-viewfactor over (geen beam) — het huis ervóór schermt de directe zon af.
    full = am.facade_irradiance(219.0, 219.0, 45.0, 700.0, 150.0, 90.0, False)
    diff = am.facade_irradiance(219.0, 219.0, 45.0, 700.0, 150.0, 90.0, True)
    assert diff == pytest.approx(150.0 * 0.5, abs=1e-9)   # verticaal → sky_view 0.5
    assert diff < full
    # 's Nachts (zon onder horizon) is er sowieso geen beam → beide gelijk.
    night_full = am.facade_irradiance(219.0, 219.0, -5.0, 0.0, 80.0, 90.0, False)
    night_diff = am.facade_irradiance(219.0, 219.0, -5.0, 0.0, 80.0, 90.0, True)
    assert night_full == pytest.approx(night_diff, abs=1e-12)


def test_facade_irradiance_horizon_blocks_low_sun():
    # Overburen (+ boom) vóór de NW-gevel: staat de zon lager dan de obstakel-elevatie, dan
    # valt de directe beam weg en blijft enkel het (voor het obstakel gereduceerde) diffuus
    # over — als diffuse_only, maar elevatie-afhankelijk i.p.v. permanent.
    az = 309.0
    above = am.facade_irradiance(az, az, 20.0, 700.0, 120.0, 90.0, False, 14.0)  # 20° > obstakel
    below = am.facade_irradiance(az, az, 8.0, 700.0, 120.0, 90.0, False, 14.0)   # 8° < obstakel
    reduced_sky_view = 0.5 * (1.0 - am.horizon_diffuse_reduction(14.0))
    assert below == pytest.approx(120.0 * reduced_sky_view, abs=1e-9)
    assert below < 120.0 * 0.5   # het obstakel neemt ook een deel van de diffuse hemel weg
    assert below < above
    # horizon_deg default 0 → identiek aan geen-obstakel, en die ziet de 8°-zon nog wél als beam.
    no_obstacle = am.facade_irradiance(az, az, 8.0, 700.0, 120.0, 90.0, False)
    assert no_obstacle == pytest.approx(
        am.facade_irradiance(az, az, 8.0, 700.0, 120.0, 90.0, False, 0.0), abs=1e-12)
    assert no_obstacle > below


def test_horizon_diffuse_reduction_bounds():
    # Geen obstakel → geen reductie; recht-op-de-gevel-hoog obstakel (90°) → volledige blokkade.
    assert am.horizon_diffuse_reduction(0.0) == pytest.approx(0.0, abs=1e-12)
    assert am.horizon_diffuse_reduction(90.0) == pytest.approx(1.0, abs=1e-9)
    # Monotoon stijgend: een hoger obstakel neemt nooit minder hemel weg.
    fracs = [am.horizon_diffuse_reduction(h) for h in (0.0, 14.0, 28.0, 42.0, 60.0, 90.0)]
    assert fracs == sorted(fracs)


def test_build_timeline_averages_solar_over_step():
    # build_timeline middelt de instraling over elke 15-min stap (SOLAR_SUBSTEPS sub-samples op
    # de midden-regel) i.p.v. één punt-sample aan de stap-rand → dempt de avond-aliasing van de
    # snel-draaiende lage NW-zon. Verifieer dat de geraster-irr exact dat sub-sample-gemiddelde is.
    house = {
        "rooms": {"r": {}},
        "windows": {"w": {"room": "r", "facade_azimuth_deg": 309.0,
                          "glass_m2": 1.0, "tilt_deg": 90.0}},
    }
    base = datetime(2026, 6, 14, 17, 0, tzinfo=timezone.utc)
    rows = [{"dt": base + timedelta(hours=h), "T_out": 16.0, "direct": 600.0, "diffuse": 100.0,
             "wind_speed": 3.0, "wind_dir": 309.0, "gust": 5.0, "precip": 0.0, "rh": 60.0}
            for h in range(0, 5)]
    now = base + timedelta(hours=2)        # 19:00 UTC — lage avondzon in het NW
    grid = am.build_timeline(house, {"hourly": rows}, [], now, window_h=1.0)

    t = now
    step = next(s for s in grid if s["t"] == t)
    expected = 0.0
    for j in range(am.SOLAR_SUBSTEPS):
        ts = t + timedelta(hours=0.25 * (j + 0.5) / am.SOLAR_SUBSTEPS)
        s_az, s_el = am.sun_position(am._LAT, am._LON, ts.astimezone(timezone.utc))
        expected += 0.7 * am.facade_irradiance(309.0, s_az, s_el, 600.0, 100.0, 90.0, False, 0.0)
    expected /= am.SOLAR_SUBSTEPS
    assert step["irr"]["r"] == pytest.approx(expected, abs=1e-9)
    # De rij bewaart de representatieve zon op het stap-midden (voor het dashboard).
    mid_az, mid_el = am.sun_position(am._LAT, am._LON,
                                     (t + timedelta(hours=0.125)).astimezone(timezone.utc))
    assert step["sun_el"] == pytest.approx(mid_el, abs=1e-9)
    assert step["sun_az"] == pytest.approx(mid_az, abs=1e-9)


def test_wind_pressure_default_unchanged():
    # Zonder tilt_deg-argument blijft de druk identiek aan het verticale-muur-gedrag.
    rho = am.air_density(20.0)
    p_default = am.wind_pressure(309.0, 4.3, 5.0, 194.0, 0.5, rho)
    p_vertical = am.wind_pressure(309.0, 4.3, 5.0, 194.0, 0.5, rho, tilt_deg=90.0)
    assert p_default == pytest.approx(p_vertical, abs=1e-12)
    # Plat dakraam op dezelfde plek → zuiging (negatief), ongeacht of de muur dat zou zijn.
    p_roof = am.wind_pressure(309.0, 4.3, 5.0, 194.0, 0.5, rho, tilt_deg=0.0)
    assert p_roof < 0.0


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


# ── 3b. Wind-referentiehoogte + effectief openingsoppervlak (fysica-rev 2) ───────────

def _same_facade_house() -> dict:
    """Toy-huis met beide ramen op DEZELFDE gevel maar op verschillende hoogte — de
    configuratie die vóór de WIND_REF_Z-fix een kunstmatige dwarsstroom-lus dreef."""
    house = _toy_house()
    for wid, z in (("a_win", 1.5), ("b_win", 7.1)):
        house["windows"][wid]["facade_azimuth_deg"] = 309
        house["windows"][wid]["center_height_m"] = z
    return house


def test_same_facade_wind_pressure_equal():
    # Twee openingen op dezelfde gevel (zelfde Cp) horen dezelfde winddruk te krijgen,
    # ongeacht hun hoogte: de dynamische druk staat op WIND_REF_Z (CONTAM: één winddruk
    # per gevel); het hoogteverschil hoort alleen in de stack-term, niet in Pe.
    house = _same_facade_house()
    params = am.default_params(house)
    zt = {"a": 22.0, "b": 22.0, "hall": 22.0}
    ops = am.build_openings(house, {"a_win": "open", "b_win": "open"},
                            {"wind_speed": 6.2, "wind_dir": 194.0}, params, zt, 22.0)
    pe = {op["id"]: op["Pe"] for op in ops}
    assert pe["a_win"] == pytest.approx(pe["b_win"], abs=1e-9)
    assert pe["a_win"] != 0.0                          # er stáát wel winddruk op de gevel


def test_same_facade_no_wind_loop_at_equal_temps():
    # Regressie (10 juli 2026): gelijke temperaturen binnen/buiten (geen stack) + harde wind
    # op één gevel mag GEEN doorstroom-lus raam→deur→deur→raam drijven. Vóór de fix gaf het
    # per-opening-hoogte-machtsprofiel ΔPe ∝ wind² tussen de twee zelfde-gevel-ramen
    # (~0.2+ m³/s door de deuren); nu is de gevel-Pe per definitie gelijk en resteert er
    # alleen lek-schaal-ruis.
    house = _same_facade_house()
    zones = list(house["rooms"]) + list(house["junctions"])
    params = am.default_params(house)
    zt = {z: 22.0 for z in zones}
    ops = am.build_openings(house, {"a_win": "open", "b_win": "open"},
                            {"wind_speed": 6.2, "wind_dir": 39.0}, params, zt, 22.0)
    net = am.solve_network(zones, ops, zt, 22.0)
    door_q = {op["id"]: q for op, q in zip(ops, net["flows"])
              if op["id"] in ("a_hall", "b_hall")}
    assert all(abs(q) < 0.05 for q in door_q.values())


def test_cross_facade_flow_survives():
    # De fix mag échte dwarsventilatie (loef → lij) niet doden: tegenoverliggende gevels
    # houden hun Cp-contrast en drijven een stevige doorstroom.
    house = _toy_house()                                # a_win az 180, b_win az 0
    zones = list(house["rooms"]) + list(house["junctions"])
    params = am.default_params(house)
    zt = {z: 22.0 for z in zones}
    ops = am.build_openings(house, {"a_win": "open", "b_win": "open"},
                            {"wind_speed": 6.0, "wind_dir": 180.0}, params, zt, 22.0)
    net = am.solve_network(zones, ops, zt, 22.0)
    q = {op["id"]: v for op, v in zip(ops, net["flows"]) if op["id"] in ("a_win", "b_win")}
    assert q["a_win"] < -0.2                            # loef: instroom (negatief = binnenwaarts)
    assert q["b_win"] > 0.2                             # lij: uitstroom


def test_effective_open_area_casement():
    # Een wijd open draairaam is niet het volle kozijngat: open_type "casement" → ×0.5;
    # een expliciete per-element `eff_open_frac` overschrijft de type-default; zonder
    # open_type (roosters, toy-ramen) verandert er niets (×1.0).
    house = _toy_house()
    house["windows"]["a_win"]["open_type"] = "casement"
    params = am.default_params(house)
    zt = {"a": 22.0, "b": 22.0, "hall": 22.0}
    wx = {"wind_speed": 0.0, "wind_dir": 0.0}
    ops = am.build_openings(house, {"a_win": "open", "b_win": "open"}, wx, params, zt, 20.0)
    area = {op["id"]: op["area"] for op in ops}
    assert area["a_win"] == pytest.approx(0.6 * 0.5)   # casement-korting
    assert area["b_win"] == pytest.approx(0.5)          # geen open_type → ongewijzigd
    house["windows"]["a_win"]["eff_open_frac"] = 0.8    # expliciete override wint
    ops = am.build_openings(house, {"a_win": "open"}, wx, params, zt, 20.0)
    area = {op["id"]: op["area"] for op in ops}
    assert area["a_win"] == pytest.approx(0.6 * 0.8)


def test_physics_rev_migration_resets_globals():
    # Geleerde staat van een oudere fysica-revisie: alléén de globalen (die de oude
    # zelfde-gevel-lus compenseerden) terug naar hun prior; kamer-params blijven staan.
    house = {"rooms": {"a": {}}}
    old = {"params": {"cp_shelter": 0.1, "vent_eff": 0.43, "a": {"c_air": 1.2}}}
    assert am.physics_rev_migration_needed(old) is True
    p = am.merged_params(house, old)
    assert p["cp_shelter"] == am.PRIORS["cp_shelter"]
    assert p["vent_eff"] == am.PRIORS["vent_eff"]
    assert p["a"]["c_air"] == 1.2                      # kamer-params onaangeroerd
    cur = {"params": {"cp_shelter": 0.9, "a": {"c_air": 1.2}}, "physics_rev": am.PHYSICS_REV}
    assert am.physics_rev_migration_needed(cur) is False
    assert am.merged_params(house, cur)["cp_shelter"] == 0.9
    assert am.physics_rev_migration_needed({}) is False   # lege staat: niets te migreren


# ── 4. 2-knoops RC-model ─────────────────────────────────────────────────────────────

def test_rc_relaxes_to_outside_no_solar():
    # Geen zon, dichte ramen, constante buitentemp → binnen relaxeert náár buiten. Het
    # gebouw heeft een grote thermische tijdconstante (~dag), dus geef het ruim de tijd.
    # We toetsen hier de pure schil-relaxatie, dus zónder de tussenwoning-warmtebronnen
    # (buren + interne last) — díe tillen de evenwichtstemp op en worden apart getoetst in
    # test_tussenwoning_terms_lift_above_outside.
    house = _toy_house()
    params = am.default_params(house)
    for rid in house["rooms"]:
        params[rid]["ua_party"] = 0.0
        params[rid]["q_int"] = 0.0
    T_out = 18.0
    tl = _const_timeline(T_out, hours=240, irr=0.0)
    seed = {z: 26.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    sim = am.simulate(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    for rid in house["rooms"]:
        assert sim["Ta"][rid] == pytest.approx(T_out, abs=0.4)
        # Monotone afkoeling: eindtemp ligt tussen buiten en de startwaarde.
        assert T_out <= sim["Ta"][rid] < 26.0


def test_tussenwoning_terms_lift_above_outside():
    # Mét de tussenwoning-termen aan (buren op NEIGHBOR_TEMP + interne last) landt een kamer
    # met dichte ramen en zónder zon bóven de koudere buitentemp — naar de buren toe plus de
    # interne last. Dit is het structurele verschil dat de koude-bias verhielp.
    house = _toy_house()
    T_out = 16.0
    tl = _const_timeline(T_out, hours=240, irr=0.0)
    seed = {z: T_out for z in list(house["rooms"]) + list(house.get("junctions", {}))}

    p_on = am.default_params(house)                       # priors: ua_party=1, q_int=1
    sim_on = am.simulate(house, p_on, tl, seed, calib_only_rooms=set(house["rooms"]))

    p_off = am.default_params(house)
    for rid in house["rooms"]:
        p_off[rid]["ua_party"] = 0.0
        p_off[rid]["q_int"] = 0.0
    sim_off = am.simulate(house, p_off, tl, seed, calib_only_rooms=set(house["rooms"]))

    for rid in house["rooms"]:
        # Zonder termen: terug naar buiten. Mét termen: aantoonbaar opgetild, boven buiten
        # maar niet absurd boven de buurtemp.
        assert sim_off["Ta"][rid] == pytest.approx(T_out, abs=0.4)
        assert sim_on["Ta"][rid] > T_out + 0.5
        assert sim_on["Ta"][rid] > sim_off["Ta"][rid] + 1.0
        assert sim_on["Ta"][rid] <= am.NEIGHBOR_TEMP + 3.0


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


def test_h_am_is_learnable():
    # `h_am` (lucht↔massa-koppeling) leert nu per kamer mee, zodat `c_air` niet de enige knop
    # is voor de respons-snelheid van de luchtknoop (anders satureerde `c_air` op zijn grens).
    assert "h_am" in am.PER_ROOM_PARAMS
    assert ("a", "h_am") in am._param_keys(["a"])


def test_regularization_pulls_insensitive_param_to_prior():
    # De Tikhonov-ridge naar de priors moet een zwak-/niet-identificeerbare richting terug naar
    # zijn prior trekken i.p.v. 'm op een grens te laten staan. Met irr=0 heeft `solar_gain`
    # géén effect op de simulatie (nul gradient): zónder regularisatie zou hij op zijn
    # bovengrens (3.0) blijven plakken; mét regularisatie zakt hij richting de prior (1.0).
    house = _toy_house()
    tl = _const_timeline(14.0, hours=12, irr=0.0)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    truth = am.default_params(house)
    sim = am.simulate(house, truth, tl, seed, calib_only_rooms=set(house["rooms"]))
    actual = {rid: sim["series"][rid][::4] for rid in house["rooms"]}

    start = am.default_params(house)
    for rid in house["rooms"]:
        start[rid]["solar_gain"] = am.BOUNDS["solar_gain"][1]   # gerailed op de bovengrens
    new, _ = am.calibrate(house, start, tl, seed, actual, max_iter=4, time_budget_s=5)
    for rid in house["rooms"]:
        assert new[rid]["solar_gain"] < start[rid]["solar_gain"] - 0.4   # aantoonbaar losgetrokken
        assert new[rid]["solar_gain"] < 2.5                              # richting de prior (1.0)


def test_sensor_outdoor_bias_blend():
    # Een sensor op de buitenmuur leest een lineaire blend richting buiten. frac=0 en
    # ontbrekende waarden zijn no-ops (mirror van wu_bias: vaste meetcorrectie).
    assert am._sensor_temp(22.0, 12.0, 0.0) == 22.0
    assert am._sensor_temp(22.0, 12.0, 0.25) == pytest.approx(0.75 * 22 + 0.25 * 12)
    assert am._sensor_temp(None, 12.0, 0.25) is None
    assert am._sensor_temp(22.0, None, 0.25) == 22.0


def test_sensor_bias_shifts_residuals_not_physics():
    # De buitenmuur-bias is een meet-laag, geen fysica: de luchtknoop blijft identiek, maar
    # de vergelijking met de gemeten temp verschuift richting buiten. Bij koud buiten leest
    # de gebiasde voorspelling structureel lager dan de wáre luchttemp → negatieve residuen,
    # terwijl een ongebiasde kamer exact blijft.
    house = _toy_house()
    tl = _const_timeline(12.0, hours=12, irr=0.0)          # koud buiten
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 22.0 for z in zones}
    params = am.default_params(house)
    sim = am.simulate(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    # "Werkelijk" = de wáre luchtknoop (alsof de sensor perfect zou zijn).
    actual = {rid: sim["series"][rid][::4] for rid in house["rooms"]}

    # Ongebiasde kamer 'a' blijft exact; de fysica is onveranderd.
    res_a = am._residuals(house, params, tl, seed, {"a": actual["a"]}, {"a"})
    assert max(abs(r) for r in res_a) < 1e-6

    # Bias op 'b' verandert sim["Ta"] níét, maar trekt de vergeleken voorspelling omlaag.
    house["rooms"]["b"]["sensor_outdoor_frac"] = 0.2
    sim2 = am.simulate(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    assert sim2["Ta"]["b"] == pytest.approx(sim["Ta"]["b"], abs=1e-9)   # fysica intact
    res_b = am._residuals(house, params, tl, seed, {"b": actual["b"]}, {"b"})
    assert all(r < 0 for r in res_b)                       # systematisch te laag
    assert min(res_b) < -0.5


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


def test_openings_at_accumulates_incremental():
    # Kleine, losse meldingen: een element houdt zijn laatst-gezette waarde ook als een
    # latere snapshot 'm niet herhaalt (zo kun je één raam wijzigen zonder de rest op te geven).
    t0 = datetime(2026, 6, 1, 8, 0, tzinfo=am.TZ)
    log = [
        {"t": t0.isoformat(), "states": {"a": "open", "b": "dicht"}},
        {"t": (t0 + timedelta(hours=1)).isoformat(), "states": {"b": "tilt"}},   # alleen b
    ]
    st = am.openings_at(log, t0 + timedelta(hours=2))
    assert st["a"] == "open"      # behouden uit de eerdere snapshot
    assert st["b"] == "tilt"      # bijgewerkt door de latere


def test_ac_room_at_forward_fill():
    # De airco-kamer wordt voorwaarts geaccumuleerd uit de log (net als openings_at): vóór de
    # eerste melding geen airco, daarna de laatst-gezette kamer; "geen"/"" zet 'm weer uit.
    t0 = datetime(2026, 6, 1, 8, 0, tzinfo=am.TZ)
    log = [
        {"t": t0.isoformat(), "states": {"w": "open", am.AC_STATE_KEY: "office"}},
        {"t": (t0 + timedelta(hours=3)).isoformat(), "states": {am.AC_STATE_KEY: "living"}},
        {"t": (t0 + timedelta(hours=6)).isoformat(), "states": {am.AC_STATE_KEY: "geen"}},
    ]
    chg = am.ac_changes(log)
    assert am.ac_room_at(chg, t0 - timedelta(hours=1)) is None   # vóór de eerste melding
    assert am.ac_room_at(chg, t0 + timedelta(hours=1)) == "office"
    assert am.ac_room_at(chg, t0 + timedelta(hours=4)) == "living"
    assert am.ac_room_at(chg, t0 + timedelta(hours=7)) is None   # "geen" → uit
    # Een log zonder enige ac_room-sleutel levert geen wijzigingen → altijd None.
    assert am.ac_changes([{"t": t0.isoformat(), "states": {"w": "open"}}]) == []


def test_norm_ac_room():
    assert am._norm_ac_room("office") == "office"
    assert am._norm_ac_room("  Office ") == "office"   # genormaliseerd (trim + lower)
    assert am._norm_ac_room("") is None
    assert am._norm_ac_room("geen") is None
    assert am._norm_ac_room(None) is None


def test_shade_factor_override():
    w = {"shading": "none", "shade": {"factor": 0.15}}
    assert am._shade_factor("sky", w, {}) == 1.0                       # geen melding → static none
    assert am._shade_factor("sky", w, {"sky_shade": "dicht"}) == 0.15  # dicht → scherm-factor
    assert am._shade_factor("sky", w, {"sky_shade": "open"}) == 1.0    # open overschrijft
    assert am._shade_factor("sky", w, {"sky_shade": "half"}) == pytest.approx(0.5 * (1 + 0.15))
    # Raam zonder bedienbare zonwering valt terug op de statische `shading`.
    assert am._shade_factor("x", {"shading": "lamella"}, {}) == am.SHADING_FACTOR["lamella"]


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


def test_suggest_survives_none_room_temp():
    # Een sensorkamer kan in room_now staan mét waarde None: tado-uitval, of een
    # window_data.json die de kamer nog niet kent (net-toegevoegde sensor terwijl de
    # loop-checkout nog oude data heeft — de 6×-crash van 2026-07-03). room_now.get(key,
    # fallback) valt dan niet terug (de key bestáát) en None crashte air_density() in
    # solve_network. De zone-temp moet op de buitentemp terugvallen, niet crashen.
    house = _toy_house()
    params = am.default_params(house)
    room_now = {house["rooms"][r]["from_window_data"]: 27.0 for r in house["rooms"]}
    room_now[next(iter(room_now))] = None
    sugg = am.suggest(house, params, {"wind_speed": 3.0, "wind_dir": 200.0, "gust": 5.0, "precip": 0.0},
                      room_now, 16.0, 55.0, 16.0)
    assert sugg["headline"]   # geen crash; er komt gewoon een advies uit


# ── 8. Robuust leren: anomalie-poort + Huber ─────────────────────────────────────────

def _hist(*rmses):
    return [{"t": "2026-06-01T00:00:00+02:00", "rmse": v} for v in rmses]


def test_no_hold_without_enough_history():
    # Te weinig historie → nooit pauzeren (eerst bootstrappen, ook bij hoge fout).
    hold, base = am.should_hold_learning(9.0, _hist(0.5, 0.5, 0.5))
    assert hold is False
    assert base is None


def test_hold_when_error_anomalous():
    # Lange historie rond 0.5°C, nu plots 3°C → ver boven 2.2× norm én boven de vloer.
    hist = _hist(*([0.5] * 30))
    hold, base = am.should_hold_learning(3.0, hist)
    assert hold is True
    assert base == pytest.approx(0.5, abs=1e-9)


def test_no_hold_when_error_normal():
    hist = _hist(*([0.5] * 30))
    assert am.should_hold_learning(0.7, hist)[0] is False     # binnen de norm
    # Hoog t.o.v. norm maar absoluut klein (< ANOMALY_FLOOR) → niet pauzeren.
    assert am.should_hold_learning(1.2, _hist(*([0.2] * 30)))[0] is False


def test_held_runs_excluded_from_baseline():
    # Een langdurige anomalie (gepauzeerde runs met hoge RMSE) mag de norm niet optrekken:
    # de baseline blijft op de goede runs hangen, dus het blijft pauzeren tot de log weer
    # klopt.
    hist = _hist(*([0.5] * 20)) + [{"t": "x", "rmse": 12.0, "held": True} for _ in range(20)]
    hold, base = am.should_hold_learning(5.0, hist)
    assert base == pytest.approx(0.5, abs=1e-9)
    assert hold is True


def test_huber_weights_downweight_outliers():
    w = am._huber_weights([0.0, 1.0, 1.5, 3.0], delta=1.5)
    assert w[0] == 1.0 and w[1] == 1.0 and w[2] == 1.0      # binnen ±delta
    assert w[3] == pytest.approx(1.5 / 3.0)                  # uitschieter gedempt
    # Gewogen kosten zijn lager dan de pure kwadraatsom bij een uitschieter.
    res = [0.2, -0.3, 5.0]
    assert am._wcost(res) < sum(r * r for r in res)


def test_calibrate_robust_to_single_outlier():
    # Eén grof verkeerde sample mag de fit niet kapen: de geleerde params blijven dicht
    # bij die uit schone data.
    house = _toy_house()
    tl = _varying_timeline(hours=24)
    seed = {z: 20.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    truth = am.default_params(house)
    for rid in house["rooms"]:
        truth[rid]["solar_gain"] = 1.6
    sim = am.simulate(house, truth, tl, seed, calib_only_rooms=set(house["rooms"]))
    clean = {rid: sim["series"][rid][::4] for rid in house["rooms"]}
    # Injecteer één absurde sample in kamer 'a'.
    poisoned = {rid: list(s) for rid, s in clean.items()}
    t_bad, _ = poisoned["a"][3]
    poisoned["a"][3] = (t_bad, 80.0)
    p_clean, _ = am.calibrate(house, am.default_params(house), tl, seed, clean, max_iter=5, time_budget_s=20)
    p_pois, _ = am.calibrate(house, am.default_params(house), tl, seed, poisoned, max_iter=5, time_budget_s=20)
    # De geleerde solar_gain mag door de uitschieter niet ver wegschieten.
    assert abs(p_pois["a"]["solar_gain"] - p_clean["a"]["solar_gain"]) < 0.4


# ── 8. cd vastgezet, vent_eff vrijgemaakt ────────────────────────────────────────────

def test_cd_is_fixed_not_learnable():
    # `cd` is geen leerbare globale parameter meer (railde naar zijn vloer + corrumpeerde de
    # getoonde ACH/flows). Het is nu de vaste fysische constante CD; `vent_eff` draagt de
    # meng-koppeling, met een verlaagde ondergrens zodat hij niet alsnog railt.
    assert "cd" not in am.GLOBAL_PARAMS
    assert am.CD == am.PRIORS["cd"]
    assert am.BOUNDS["vent_eff"][0] == 0.1
    # default_params bevat geen `cd` meer in de geleerde vector.
    assert "cd" not in am.default_params(_toy_house())


def test_build_openings_uses_fixed_cd():
    # build_openings negeert een (verouderde, gerailde) `cd` in params en gebruikt altijd CD.
    house = _toy_house()
    params = am.default_params(house)
    params["cd"] = 0.30   # stale/gerailde waarde uit oude airflow_learned.json
    zt = {z: 22.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    ops = am.build_openings(house, {"a_win": "open"}, {"wind_speed": 4.0, "wind_dir": 200.0},
                            params, zt, 18.0)
    assert ops and all(op["Cd"] == am.CD for op in ops)


# ── 9. Dynamisch buur-anker (party-muren) ────────────────────────────────────────────

def test_neighbor_temp_estimate_winter_floor_and_summer_track():
    now = datetime(2026, 1, 15, 12, 0, tzinfo=am.TZ)
    winter = [{"dt": now - timedelta(hours=h), "T_out": 3.0} for h in range(72)]
    # 's Winters domineert de stookvloer.
    assert am.neighbor_temp_estimate(winter, now) == pytest.approx(am.NEIGHBOR_WINTER_FLOOR)
    # 's Zomers volgt het anker het 3-daags buitengemiddelde mee omhoog.
    summer = [{"dt": now - timedelta(hours=h), "T_out": 24.0} for h in range(72)]
    assert am.neighbor_temp_estimate(summer, now) == pytest.approx(24.0)
    # Geen bruikbare historie → terugval op de module-default.
    assert am.neighbor_temp_estimate([], now) == pytest.approx(am.NEIGHBOR_TEMP)


def test_simulate_honours_neighbor_temp_global():
    # Een warmer buur-anker tilt een dichte, zon-loze kamer (via de party-muren) hoger op.
    house = _toy_house()
    params = am.default_params(house)
    for rid in house["rooms"]:
        params[rid]["q_int"] = 0.0          # isoleer de party-term
    tl = _const_timeline(16.0, hours=240, irr=0.0)
    seed = {z: 16.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    saved = am._NEIGHBOR_TEMP
    try:
        am._NEIGHBOR_TEMP = 20.0
        cool = am.simulate(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
        am._NEIGHBOR_TEMP = 26.0
        warm = am.simulate(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    finally:
        am._NEIGHBOR_TEMP = saved
    for rid in house["rooms"]:
        assert warm["Ta"][rid] > cool["Ta"][rid] + 1.0


def test_simulate_tm_seed_overrides_default_blend():
    # Zonder tm_seed start Tm op de warme blend 0.5*(Ta+_NEIGHBOR_TEMP); met tm_seed moet
    # die per-zone beginwaarde overschreven worden. Eén korte stap (massa-tijdconstante ~uren)
    # zodat het startverschil nog grotendeels intact is in de output.
    house = _toy_house()
    params = am.default_params(house)
    tl = _const_timeline(20.0, hours=0, irr=0.0)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    saved = am._NEIGHBOR_TEMP
    try:
        am._NEIGHBOR_TEMP = 20.0    # default Tm-blend = 20.0, gelijk aan Ta
        default_sim = am.simulate(house, params, tl, seed)
        full_tm_seed = {z: 5.0 for z in zones}
        seeded_sim = am.simulate(house, params, tl, seed, tm_seed=full_tm_seed)
        partial_sim = am.simulate(house, params, tl, seed, tm_seed={"a": 5.0})
    finally:
        am._NEIGHBOR_TEMP = saved

    for z in zones:
        assert seeded_sim["Tm"][z] < default_sim["Tm"][z] - 10.0
    # ontbrekende zone in tm_seed valt terug op het standaardgedrag (additief, geen breuk)
    assert partial_sim["Tm"]["b"] == pytest.approx(default_sim["Tm"]["b"])
    assert partial_sim["Tm"]["a"] == pytest.approx(seeded_sim["Tm"]["a"])


# ── 10. solar_gain beschermd tegen instorten ─────────────────────────────────────────

def test_solar_gain_floor_and_per_param_ridge():
    assert am.BOUNDS["solar_gain"][0] == 0.25                 # fysieke vloer
    assert am.reg_weight("solar_gain") > am.reg_weight("ua_env")  # sterkere ankering
    assert am.reg_weight("ua_env") == am.REG_WEIGHT


def test_solar_gain_cannot_collapse_to_zero():
    # Zelfs als de data naar een lage zonwinst trekt, houdt de vloer (+ ridge) solar_gain
    # ≥ 0.25 — de twin blijft enige zonrespons houden voor de hete zonnige dagen.
    house = _toy_house()
    tl = _varying_timeline(hours=24)
    seed = {z: 20.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    truth = am.default_params(house)
    for rid in house["rooms"]:
        truth[rid]["solar_gain"] = 0.0   # "waarheid" zonder zonwinst (geclamped naar de vloer)
    sim = am.simulate(house, truth, tl, seed, calib_only_rooms=set(house["rooms"]))
    actual = {rid: sim["series"][rid][::4] for rid in house["rooms"]}
    new, _ = am.calibrate(house, am.default_params(house), tl, seed, actual, max_iter=5, time_budget_s=20)
    for rid in house["rooms"]:
        assert new[rid]["solar_gain"] >= 0.25


# ── 10b. Regime-bewuste ridge + recency-weging ───────────────────────────────────────
def test_reg_weight_regime_aware_solar_gain():
    # Het extra-sterke solar_gain-anker (6.0) ramp terug naar REG_WEIGHT (3.0) zodra het venster
    # zonniger wordt: bewolkt → vol anker (anti-collapse), zonnig → vertrouw de data.
    base = am.REG_WEIGHT_BY_PARAM["solar_gain"]
    assert am.reg_weight("solar_gain") == base                              # geen solar_mean → oud
    assert am.reg_weight("solar_gain", am.SOLAR_RIDGE_LOW_WM2 - 10) == base   # bewolkt → vol anker
    assert am.reg_weight("solar_gain", am.SOLAR_RIDGE_HIGH_WM2 + 10) == am.REG_WEIGHT  # zonnig → relax
    mid = am.reg_weight("solar_gain", 0.5 * (am.SOLAR_RIDGE_LOW_WM2 + am.SOLAR_RIDGE_HIGH_WM2))
    assert am.REG_WEIGHT < mid < base                                        # monotone tussenwaarde
    # Andere params zijn niet regime-afhankelijk.
    assert am.reg_weight("ua_env", 400) == am.REG_WEIGHT


def test_recency_weights_decay():
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=am.TZ)
    times = [t0, t0 + timedelta(hours=18), t0 + timedelta(hours=36)]   # oud → nieuw
    w = am._recency_weights(times, half_life_h=18.0)
    assert w[-1] == pytest.approx(1.0)        # nieuwste = referentie
    assert w[1] == pytest.approx(0.5)         # één half-life ouder
    assert w[0] == pytest.approx(0.25)        # twee half-lives ouder
    # Uitgeschakeld → uniform.
    assert am._recency_weights(times, half_life_h=0) == [1.0, 1.0, 1.0]
    assert am._recency_weights([]) == []


def test_recency_weighting_favors_recent_regime(monkeypatch):
    # Bij een regime-wissel in het venster moet de fit het HUIDIGE (recente) regime beter volgen.
    # We bouwen 'actual' uit een truth-sim maar verlagen de recente helft met een offset (koeler
    # regime); met recency-weging horen de recente residuen kleiner te zijn dan zonder weging.
    house = _toy_house()
    tl = _varying_timeline(hours=48)
    seed = {z: 20.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    truth = am.default_params(house)
    sim = am.simulate(house, truth, tl, seed, calib_only_rooms=set(house["rooms"]))
    mid = tl[0]["t"] + timedelta(hours=24)
    delta = 2.0
    actual = {rid: [(t, (v - delta if t >= mid else v)) for t, v in sim["series"][rid][::4]]
              for rid in house["rooms"]}

    def recent_rmse(params):
        res = [r for t, r in am._residuals_timed(house, params, tl, seed, actual, set(house["rooms"]))
               if t >= mid]
        return math.sqrt(sum(r * r for r in res) / len(res))

    monkeypatch.setattr(am, "RECENCY_HALFLIFE_H", 0.0)      # uniform
    p_uniform, _ = am.calibrate(house, am.default_params(house), tl, seed, actual, max_iter=6, time_budget_s=20)
    monkeypatch.setattr(am, "RECENCY_HALFLIFE_H", 6.0)      # sterke recency
    p_recent, _ = am.calibrate(house, am.default_params(house), tl, seed, actual, max_iter=6, time_budget_s=20)
    assert recent_rmse(p_recent) < recent_rmse(p_uniform)


# ── 10c. AC-guard-venster (niet-teruggedateerde airco-melding) ────────────────────────
def test_filter_ac_samples_guard_window():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=am.TZ)
    # Eén log-entry zet de airco 'nu' in kamer 'b' (geen terugdatering): zónder guard zou de
    # AC-koude van de uren vóór de melding ín de fit blijven. De guard laat 'b's recente samples vallen.
    acc = [(now, "b")]
    actual = {
        "a": [(now - timedelta(hours=h), 22.0) for h in range(10, 0, -1)],
        "b": [(now - timedelta(hours=h), 18.0) for h in range(10, 0, -1)],
    }
    filt, excl = am.filter_ac_samples(actual, acc, "b", now, guard_h=6.0)
    assert "a" not in excl                       # andere kamer ongemoeid
    assert len(filt["a"]) == len(actual["a"])
    assert excl["b"] == 6                          # h=1..6 binnen de guard vallen weg
    assert all(t < now - timedelta(hours=6) for t, _ in filt["b"])
    # Lege log → niets gefilterd (backwards-compatibel).
    filt2, excl2 = am.filter_ac_samples(actual, [], None, now)
    assert excl2 == {} and filt2["b"] == actual["b"]


# ── 10d. Verwarmings-uitsluiting (auto, uit de tado heat-vlag) ───────────────────────
def _heat_house():
    return {
        "location": {"lat": 52.09, "lon": 5.12},
        "rooms": {"ted": {"from_window_data": "Ted", "volume_m3": 30, "exterior_wall_m2": 10,
                          "plan_xy": [0, 0]},
                  "bath": {"from_window_data": "Shower", "volume_m3": 18, "exterior_wall_m2": 6,
                           "plan_xy": [1, 0]}},
        "junctions": {}, "windows": {}, "vents": {}, "doors": {},
    }


def test_collect_heating_on_en_filter():
    now = datetime(2026, 1, 15, 22, 0, tzinfo=am.TZ)
    since = now - timedelta(hours=5)
    ts = [now - timedelta(hours=h) for h in range(4, -1, -1)]   # 5 samples, elk uur
    # Ted stookt op de laatste 2 samples; de badkamer nooit.
    wd = {"rooms": {
        "Ted": {"heating": True, "history": [
            {"t": ts[0].isoformat(), "temp": 18.0},
            {"t": ts[1].isoformat(), "temp": 18.5},
            {"t": ts[2].isoformat(), "temp": 19.0},
            {"t": ts[3].isoformat(), "temp": 20.5, "heat": 1},
            {"t": ts[4].isoformat(), "temp": 21.5, "heat": 1},
        ]},
        "Shower": {"heating": False, "history": [
            {"t": ts[i].isoformat(), "temp": 22.0} for i in range(5)]},
    }}
    house = _heat_house()
    heat_on = am.collect_heating_on(house, wd, since)
    assert heat_on["ted"] == {ts[3], ts[4]}
    assert "bath" not in heat_on                       # badkamer stookte niet
    assert am.heating_now(house, wd) == {"ted": True, "bath": False}

    actual = {"ted": [(t, 0.0) for t in ts], "bath": [(t, 0.0) for t in ts]}
    filt, excl = am.filter_heating_samples(actual, heat_on)
    assert excl == {"ted": 2}                           # twee stook-samples weg
    assert len(filt["ted"]) == 3 and len(filt["bath"]) == 5
    assert all(t not in heat_on["ted"] for t, _ in filt["ted"])
    # Lege heat_on → ongewijzigd (backwards-compatibel met oude JSON zonder heat-vlag).
    filt2, excl2 = am.filter_heating_samples(actual, {})
    assert excl2 == {} and filt2 == actual


def test_filter_heating_drops_room_when_all_heated():
    now = datetime(2026, 1, 15, 22, 0, tzinfo=am.TZ)
    ts = [now - timedelta(hours=h) for h in range(3, -1, -1)]
    actual = {"ted": [(t, 21.0) for t in ts]}
    heat_on = {"ted": set(ts)}                          # heel het venster gestookt
    filt, excl = am.filter_heating_samples(actual, heat_on)
    assert "ted" not in filt                            # kamer valt uit de kalibratie
    assert excl["ted"] == 4


# ── 10e. Pauze-uitsluiting (huis-breed, handmatig via de modal) ──────────────────────
def test_norm_paused():
    assert am._norm_paused(True) is True
    assert am._norm_paused(False) is False
    assert am._norm_paused("true") is True
    assert am._norm_paused("gepauzeerd") is True
    assert am._norm_paused("") is False
    assert am._norm_paused("uit") is False


def test_pause_changes_and_paused_at():
    t0 = datetime(2026, 6, 1, 8, 0, tzinfo=am.TZ)
    log = [
        {"t": t0.isoformat(), "states": {"w": "open", am.PAUSE_STATE_KEY: True}},
        {"t": (t0 + timedelta(hours=2)).isoformat(), "states": {am.PAUSE_STATE_KEY: False}},
        {"t": (t0 + timedelta(hours=5)).isoformat(), "states": {am.PAUSE_STATE_KEY: True}},
    ]
    chg = am.pause_changes(log)
    assert am.paused_at(chg, t0 - timedelta(hours=1)) is False   # vóór eerste melding
    assert am.paused_at(chg, t0 + timedelta(hours=1)) is True
    assert am.paused_at(chg, t0 + timedelta(hours=3)) is False
    assert am.paused_at(chg, t0 + timedelta(hours=6)) is True
    # Log zonder paused-sleutel → geen wijzigingen.
    assert am.pause_changes([{"t": t0.isoformat(), "states": {"w": "open"}}]) == []


def test_paused_intervals_open_ended_when_still_active():
    t0 = datetime(2026, 6, 1, 8, 0, tzinfo=am.TZ)
    now = t0 + timedelta(hours=6)
    chg = [(t0, True), (t0 + timedelta(hours=2), False), (t0 + timedelta(hours=5), True)]
    intervals = am.paused_intervals(chg, now)
    assert intervals == [(t0, t0 + timedelta(hours=2)), (t0 + timedelta(hours=5), now)]
    # Nooit gepauzeerd → geen intervallen.
    assert am.paused_intervals([], now) == []
    # Volledig afgesloten (geen actieve pauze op `now`) → laatste interval sluit netjes af,
    # geen extra open-eindig interval.
    chg2 = [(t0, True), (t0 + timedelta(hours=2), False)]
    assert am.paused_intervals(chg2, now) == [(t0, t0 + timedelta(hours=2))]


def test_filter_paused_samples_drops_all_rooms_house_wide():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=am.TZ)
    intervals = [(now - timedelta(hours=4), now - timedelta(hours=1))]
    actual = {
        "a": [(now - timedelta(hours=h), 22.0) for h in range(6, 0, -1)],
        "b": [(now - timedelta(hours=h), 23.0) for h in range(6, 0, -1)],
    }
    filt, excl = am.filter_paused_samples(actual, intervals)
    # Uren 4,3,2,1 vallen binnen het interval → 4 samples weg per kamer, huis-breed.
    assert excl == {"a": 4, "b": 4}
    assert len(filt["a"]) == 2 and len(filt["b"]) == 2
    assert all(t < now - timedelta(hours=4) or t > now - timedelta(hours=1) for t, _ in filt["a"])
    # Lege intervals → ongewijzigd (backwards-compatibel).
    filt2, excl2 = am.filter_paused_samples(actual, [])
    assert excl2 == {} and filt2 == actual


def test_filter_paused_samples_drops_room_entirely_when_fully_paused():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=am.TZ)
    ts = [now - timedelta(hours=h) for h in range(3, -1, -1)]
    actual = {"a": [(t, 21.0) for t in ts]}
    intervals = [(ts[0], now)]   # heel het venster gepauzeerd
    filt, excl = am.filter_paused_samples(actual, intervals)
    assert "a" not in filt
    assert excl["a"] == 4


def test_openings_fingerprint_changes_on_paused_correction():
    t0 = datetime(2026, 6, 1, 8, 0, tzinfo=am.TZ)
    log = [{"t": t0.isoformat(), "states": {"w": "open"}}]
    log_corrected = [{"t": t0.isoformat(), "states": {"w": "open", am.PAUSE_STATE_KEY: True}}]
    fp1 = am.openings_fingerprint(log, t0 - timedelta(hours=1), t0 + timedelta(hours=1))
    fp2 = am.openings_fingerprint(log_corrected, t0 - timedelta(hours=1), t0 + timedelta(hours=1))
    assert fp1 != fp2


def test_railed_params_flags_bounds():
    params = am.default_params(_toy_house())
    params["cp_shelter"] = am.BOUNDS["cp_shelter"][0]      # op de vloer
    params["a"]["solar_gain"] = am.BOUNDS["solar_gain"][1]  # op het plafond
    params["a"]["ua_env"] = 1.0                            # midden → niet gerailed
    flags = am.railed_params(params)
    assert "global.cp_shelter@floor" in flags
    assert "a.solar_gain@ceil" in flags
    assert not any(f.startswith("a.ua_env") for f in flags)


# ── 11. Dak-sol-air-term (bovenste verdieping) ───────────────────────────────────────

def _roof_house(roof_m2: float) -> dict:
    return {
        "location": {"lat": 52.09, "lon": 5.12},
        "rooms": {"r": {"from_window_data": "office", "volume_m3": 30,
                        "exterior_wall_m2": 10, "roof_m2": roof_m2}},
        "junctions": {}, "windows": {}, "vents": {}, "doors": {},
    }


def _roof_timeline(T_out: float, hours: int, irr_roof: float, sun_el: float) -> list[dict]:
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=am.TZ)
    grid = []
    for i in range(hours * 4 + 1):
        t = t0 + timedelta(minutes=15 * i)
        grid.append({"t": t, "T_out": T_out, "irr": {"r": 0.0}, "irr_roof": {"r": irr_roof},
                     "sun_el": sun_el, "states": {},
                     "weather": {"wind_speed": 1.0, "wind_dir": 200.0, "gust": 2.0, "precip": 0.0,
                                 "direct": 0.0, "diffuse": 0.0, "rh": 60}, "dt": 900.0})
    return grid


def test_roof_term_warms_by_day_cools_by_night():
    params_with = am.default_params(_roof_house(20.0))
    params_no = am.default_params(_roof_house(0.0))
    seed = {"r": 20.0}
    # Overdag: zon op het dak → de dak-kamer warmer dan zonder dak.
    day_with = am.simulate(_roof_house(20.0), params_with,
                           _roof_timeline(20.0, 48, 700.0, 45.0), seed, calib_only_rooms={"r"})
    day_no = am.simulate(_roof_house(0.0), params_no,
                         _roof_timeline(20.0, 48, 700.0, 45.0), seed, calib_only_rooms={"r"})
    assert day_with["Ta"]["r"] > day_no["Ta"]["r"] + 0.3
    # 's Nachts: hemel-stralingskoeling → de dak-kamer kouder dan zonder dak.
    night_with = am.simulate(_roof_house(20.0), params_with,
                             _roof_timeline(20.0, 48, 0.0, -5.0), seed, calib_only_rooms={"r"})
    night_no = am.simulate(_roof_house(0.0), params_no,
                           _roof_timeline(20.0, 48, 0.0, -5.0), seed, calib_only_rooms={"r"})
    assert night_with["Ta"]["r"] < night_no["Ta"]["r"] - 0.1


def test_roofless_room_unchanged_by_roof_term():
    # Een kamer zónder roof_m2 (UA_roof basis 0) is identiek aan het oude gedrag, ongeacht
    # of er een irr_roof in de stap staat.
    house = _roof_house(0.0)
    params = am.default_params(house)
    seed = {"r": 18.0}
    a = am.simulate(house, params, _roof_timeline(24.0, 12, 800.0, 50.0), seed, calib_only_rooms={"r"})
    b = am.simulate(house, params, _roof_timeline(24.0, 12, 0.0, 50.0), seed, calib_only_rooms={"r"})
    assert a["Ta"]["r"] == pytest.approx(b["Ta"]["r"], abs=1e-12)


# ── 12. f_air leerbaar (zon-split lucht/massa) ───────────────────────────────────────

def test_f_air_is_learnable():
    assert "f_air" in am.PER_ROOM_PARAMS
    assert ("a", "f_air") in am._param_keys(["a"])
    assert am.BOUNDS["f_air"] == (0.1, 0.9)
    assert am.default_params(_toy_house())["a"]["f_air"] == am.PRIORS["f_air"]


def test_f_air_splits_solar_air_vs_mass():
    # Meer zon naar de luchtknoop (hoger f_air) → de luchtknoop reageert op korte termijn
    # sterker op een zonpuls dan met een lage f_air (die de zon vooral in de trage massa stopt).
    house = {
        "location": {"lat": 52.09, "lon": 5.12},
        "rooms": {"r": {"volume_m3": 30, "exterior_wall_m2": 10}},
        "junctions": {}, "windows": {}, "vents": {}, "doors": {},
    }
    t0 = datetime(2026, 6, 15, 12, 0, tzinfo=am.TZ)
    tl = [{"t": t0 + timedelta(minutes=15 * i), "T_out": 18.0, "irr": {"r": 1500.0},
           "states": {}, "weather": {"wind_speed": 0.5, "wind_dir": 200.0, "gust": 1.0,
                                     "precip": 0.0, "direct": 0.0, "diffuse": 0.0, "rh": 50},
           "dt": 900.0} for i in range(9)]   # ~2u zonpuls
    seed = {"r": 18.0}
    lo = am.default_params(house)
    lo["r"]["f_air"] = 0.2
    hi = am.default_params(house)
    hi["r"]["f_air"] = 0.8
    sim_lo = am.simulate(house, lo, tl, seed, calib_only_rooms={"r"})
    sim_hi = am.simulate(house, hi, tl, seed, calib_only_rooms={"r"})
    assert sim_hi["Ta"]["r"] > sim_lo["Ta"]["r"] + 0.2


# ── 13. model_version-stempel (RMSE ↔ codeversie) ────────────────────────────────────

def test_model_version_prefers_github_sha(monkeypatch):
    monkeypatch.setattr(am, "_MODEL_VERSION", None)
    monkeypatch.setenv("GITHUB_SHA", "abcdef1234567890")
    assert am.model_version() == "abcdef1"   # short-SHA (7 tekens)


def test_model_version_always_nonempty(monkeypatch):
    monkeypatch.setattr(am, "_MODEL_VERSION", None)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    v = am.model_version()
    assert isinstance(v, str) and v   # git short-SHA of 'unknown', nooit leeg


# ── 14. Weer-genormaliseerde skill + persistentie-baseline ───────────────────────────

def test_naive_rmse_persistence_baseline():
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=am.TZ)
    actual = {"a": [(t0, 20.0), (t0, 22.0), (t0, 18.0)]}   # baseline = eerste sample (20)
    assert am.naive_rmse(actual) == pytest.approx(math.sqrt((0 + 4 + 4) / 3))
    assert am.naive_rmse({}) != am.naive_rmse({})          # geen samples → NaN


def test_skill_score_normalizes_and_clamps():
    assert am.skill_score(0.5, 1.0) == pytest.approx(0.5)   # half zo goed als persistentie
    assert am.skill_score(0.0, 1.0) == pytest.approx(1.0)   # perfect, geklemd op 1
    assert am.skill_score(2.0, 1.0) == pytest.approx(-1.0)  # slechter dan persistentie
    assert am.skill_score(0.5, 0.0) is None                 # vlakke dag → onbruikbare baseline
    assert am.skill_score(float("nan"), 1.0) is None


def test_window_weather_summary_stamps_window_only():
    now = datetime(2026, 6, 17, 12, 0, tzinfo=am.TZ)
    rows = [{"dt": now - timedelta(hours=h), "T_out": 20.0 + h % 5, "shortwave": 100.0 * (h % 4)}
            for h in range(0, 30)]
    rows.append({"dt": now - timedelta(hours=72), "T_out": 99.0, "shortwave": 9999.0})  # buiten venster
    wx = am.window_weather_summary({"hourly": rows}, now, window_h=24.0)
    assert wx["tmax"] <= 24.0 and wx["tmax"] != 99.0        # de oude rij telt niet mee
    assert 0 <= wx["solar_mean"] <= wx["solar_peak"] <= 300


# ── 15. Beste-params-checkpoint + auto-fallback ──────────────────────────────────────

def test_checkpoint_captures_first_and_better_optimum():
    p1 = {"living": {"c_air": 1.0}}
    _, ckpt, fb = am.checkpoint_step({}, p1, skill=0.5, rmse_now=0.4, version="v1", now_iso="t1")
    assert fb is False and ckpt["skill"] == 0.5 and ckpt["params"] == p1
    # Een duidelijk betere skill legt de nieuwe params vast.
    p2 = {"living": {"c_air": 2.0}}
    _, ckpt2, fb2 = am.checkpoint_step(ckpt, p2, skill=0.7, rmse_now=0.3, version="v1", now_iso="t2")
    assert fb2 is False and ckpt2["skill"] == 0.7 and ckpt2["params"] == p2


def test_checkpoint_within_margin_resets_counter():
    ckpt = {"params": {"living": {"c_air": 1.0}}, "skill": 0.5, "degraded_runs": 3}
    params, out, fb = am.checkpoint_step(ckpt, {"living": {"c_air": 9.0}},
                                         skill=0.45, rmse_now=0.5, version="v1", now_iso="t")
    assert fb is False and out["degraded_runs"] == 0       # binnen marge → teller terug op 0
    assert out["skill"] == 0.5                              # optimum onveranderd
    assert params == {"living": {"c_air": 9.0}}            # huidige params behouden


def test_checkpoint_falls_back_after_sustained_degradation():
    best = {"living": {"c_air": 1.0}}
    ckpt = {"params": best, "skill": 0.6, "degraded_runs": am.FALLBACK_AFTER - 1}
    bad = {"living": {"c_air": 9.0}}
    params, out, fb = am.checkpoint_step(ckpt, bad, skill=0.30, rmse_now=1.2,
                                         version="v2", now_iso="t9")
    assert fb is True                                      # aanhoudend verslechterd → terugval
    assert params == best and params is not best           # diepe kopie van de checkpoint-params
    assert out["degraded_runs"] == 0 and out["last_fallback"] == "t9"


def test_checkpoint_degradation_accumulates_before_fallback():
    ckpt = {"params": {"living": {"c_air": 1.0}}, "skill": 0.6, "degraded_runs": 0}
    params, out, fb = am.checkpoint_step(ckpt, {"living": {"c_air": 9.0}},
                                         skill=0.30, rmse_now=1.0, version="v2", now_iso="t1")
    assert fb is False and out["degraded_runs"] == 1       # nog niet: telt eerst op
    assert params == {"living": {"c_air": 9.0}}


def test_reseat_checkpoint_shape():
    cur = {"living": {"c_air": 1.2}}
    out = am.reseat_checkpoint(cur, skill=-0.9, rmse_now=0.58, version="v3", now_iso="t5",
                               last_fallback="t4")
    assert out["params"] == cur and out["params"] is not cur          # diepe kopie
    assert out["skill"] == -0.9 and out["rmse"] == 0.58
    assert out["degraded_runs"] == 0
    assert out["reseated"] == "t5" and out["t"] == "t5"
    assert out["last_fallback"] == "t4"                                # laatste échte fallback blijft
    nan_out = am.reseat_checkpoint(cur, skill=None, rmse_now=float("nan"), version="v3",
                                   now_iso="t5")
    assert nan_out["rmse"] is None


def test_reseated_checkpoint_breaks_fallback_loop():
    """De fallback-lus (regressie): een fossiel optimum (skill geoogst op een gunstig venster /
    oudere code-versie) blijft in kalm weer onbereikbaar, dus zonder her-zeteling valt elke
    FALLBACK_AFTER runs dezelfde fossiele-params-terugzetting. Na een verworpen fallback +
    reseat ligt de lat op het huidige niveau en degradeert er niets meer."""
    fossil = {"params": {"living": {"c_air": 9.9}}, "skill": 0.8, "degraded_runs": 0}
    cur = {"living": {"c_air": 1.2}}
    ckpt = fossil
    for i in range(am.FALLBACK_AFTER):
        params, ckpt, fb = am.checkpoint_step(ckpt, cur, skill=-1.0, rmse_now=0.6,
                                              version="v3", now_iso=f"t{i}")
    assert fb is True                                       # de lus zoals in het wild
    # main() verwerpt 'm (fossiel past slechter op het huidige venster) en her-zetelt:
    ckpt = am.reseat_checkpoint(cur, skill=-1.0, rmse_now=0.6, version="v3", now_iso="t9")
    # Zelfde kalm-weer-skill erna: binnen de marge van het her-gezetelde optimum → geen
    # degradatie meer, geen nieuwe fallback — de lus is gebroken.
    for i in range(am.FALLBACK_AFTER + 2):
        params, ckpt, fb = am.checkpoint_step(ckpt, cur, skill=-1.0, rmse_now=0.6,
                                              version="v3", now_iso=f"u{i}")
        assert fb is False
    assert ckpt["degraded_runs"] == 0
    # Een écht betere run daarna legt gewoon weer een nieuw optimum vast.
    params, ckpt, fb = am.checkpoint_step(ckpt, cur, skill=0.2, rmse_now=0.4,
                                          version="v3", now_iso="u99")
    assert fb is False and ckpt["skill"] == 0.2 and ckpt["t"] == "u99"


def test_accepted_fallback_reseats_skill_bar():
    """De geaccepteerde-fallback-jojo (regressie, 9–10 juli 2026): een op één gunstig venster
    geoogste skill-lat (0.813) blijft na een geaccepteerde fallback staan, normale vensters
    halen 'm nooit → elke FALLBACK_AFTER runs opnieuw een fallback. Na her-vloeren ligt de lat
    op wat de teruggezette params NU halen en degradeert dezelfde skill niet meer."""
    best = {"living": {"c_air": 1.0}}
    ckpt = {"params": best, "skill": 0.813, "degraded_runs": am.FALLBACK_AFTER - 1}
    params, ckpt, fb = am.checkpoint_step(ckpt, {"living": {"c_air": 2.0}}, skill=0.47,
                                          rmse_now=0.74, version="v1", now_iso="t1")
    assert fb is True
    # main() accepteert (checkpoint-params passen beter) en her-vloert de lat:
    ckpt = am.accept_fallback_checkpoint(ckpt, skill=0.47, rmse_now=0.71, now_iso="t1")
    assert ckpt["skill"] == 0.47 and ckpt["rmse"] == 0.71
    assert ckpt["refloored"] == "t1"
    assert ckpt["params"] == best                           # params blijven het checkpoint
    assert ckpt["last_fallback"] == "t1"                     # de échte fallback blijft gestempeld
    # Zelfde skill in de runs erna: binnen de marge → geen jojo meer.
    for i in range(am.FALLBACK_AFTER + 2):
        params, ckpt, fb = am.checkpoint_step(ckpt, {"living": {"c_air": 2.0}}, skill=0.47,
                                              rmse_now=0.74, version="v1", now_iso=f"u{i}")
        assert fb is False
    assert ckpt["degraded_runs"] == 0
    # Een écht beter venster legt daarna gewoon weer een nieuw optimum vast.
    _, ckpt, fb = am.checkpoint_step(ckpt, {"living": {"c_air": 3.0}}, skill=0.60,
                                     rmse_now=0.5, version="v1", now_iso="u99")
    assert fb is False and ckpt["skill"] == 0.60


def test_accept_fallback_keeps_nan_rmse_none():
    ckpt = {"params": {"living": {"c_air": 1.0}}, "skill": 0.8, "degraded_runs": 0}
    out = am.accept_fallback_checkpoint(ckpt, skill=None, rmse_now=float("nan"), now_iso="t2")
    assert out["rmse"] is None and out["skill"] is None
    assert out["refloored"] == "t2"
    assert ckpt["skill"] == 0.8                              # input niet gemuteerd


# ── Observability: solver-failures + kalibratiedekking ──────────────────────────────

def test_simulate_flags_solver_failure(monkeypatch):
    # Een bijna-singulier thermisch stelsel bevriest de substap stil op de laatste goede
    # waarde — dat mág, maar moet geteld worden (learned.solver_failures) i.p.v. geruisloos.
    house = _toy_house()
    params = am.default_params(house)
    tl = _const_timeline(20.0, hours=2, irr=0.0)
    seed = {z: 22.0 for z in list(house["rooms"]) + list(house["junctions"])}
    ok = am.simulate(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    assert ok["solver_failures"] == 0                        # normaal: geen enkele
    monkeypatch.setattr(am, "solve_linear", lambda A, b: None)
    sim = am.simulate(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    assert sim["solver_failures"] > 0
    for rid in house["rooms"]:                               # bevroren op de seed, niet NaN
        assert sim["Ta"][rid] == pytest.approx(22.0)


def test_should_nudge_anomaly_cooldown():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=am.TZ)
    assert am.should_nudge_anomaly(None, now) is True                    # episode-start
    fresh = (now - timedelta(hours=1)).isoformat()
    assert am.should_nudge_anomaly(fresh, now) is False                  # binnen cooldown
    old = (now - timedelta(hours=am.ANOMALY_NUDGE_COOLDOWN_H)).isoformat()
    assert am.should_nudge_anomaly(old, now) is True                     # cooldown verstreken
    assert am.should_nudge_anomaly("kapot", now) is True                 # onleesbare stempel


def test_anomaly_nudge_text_contents(monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    txt = am.anomaly_nudge_text(1.92, 0.65)
    assert "1.92" in txt and "0.65" in txt
    assert "raam" in txt.lower()                        # de vraag die de log herstelt
    assert "airflow.html" not in txt                    # geen URL zonder DASHBOARD_URL
    monkeypatch.setenv("DASHBOARD_URL", "https://x.test/dash/")
    txt = am.anomaly_nudge_text(1.92, None)
    assert "https://x.test/dash/airflow.html" in txt
    assert "norm" not in txt                            # geen norm-tekst zonder baseline


def test_calib_coverage_reports_effective_ground_truth():
    t0 = datetime(2026, 7, 10, 0, 0, tzinfo=am.TZ)
    actual = {"a": [(t0 + timedelta(hours=h), 20.0) for h in range(13)],
              "b": [(t0, 21.0)]}
    cov = am.calib_coverage(actual)
    assert cov["calib_samples"] == 14 and cov["calib_rooms"] == 2
    assert cov["calib_span_h"] == pytest.approx(12.0)
    assert am.calib_coverage({}) == {"calib_samples": 0, "calib_rooms": 0,
                                     "calib_span_h": 0.0}


# ── Leercurve achteraf herstellen tegen de gecorrigeerde openingen-log ───────────────
def test_window_naive_baseline_is_time_bounded():
    t0 = datetime(2026, 6, 18, 12, 0, tzinfo=am.TZ)
    actual = {"a": [(t0 + timedelta(hours=h), 20.0 + h) for h in range(6)]}   # 20..25
    # Deel-venster [t0+2u, t0+4u] → samples 22,23,24; baseline = 22 → rmse √((0+1+4)/3).
    val = am._window_naive(actual, t0 + timedelta(hours=2), t0 + timedelta(hours=4))
    assert val == pytest.approx(math.sqrt((0 + 1 + 4) / 3))
    leeg = am._window_naive(actual, t0 + timedelta(hours=10), t0 + timedelta(hours=12))
    assert leeg != leeg                                                      # geen samples → NaN


def _backfill_fixture(now):
    """Gedeelde ingrediënten: kleine (0.1°C) gecorrigeerde residuen + actuals over ~48u,
    en een openingen-log met één snapshot ruim vóór het venster."""
    timed_res = [(now - timedelta(hours=h), 0.1) for h in range(48, -1, -1)]
    actual = {"a": [(now - timedelta(hours=h), 20.0 + (h % 3)) for h in range(48, -1, -1)]}
    log = [{"t": (now - timedelta(days=10)).isoformat(), "states": {"w1": "closed"}}]
    return timed_res, actual, log


def test_backfill_recomputes_in_horizon_points_on_log_change():
    now = datetime(2026, 6, 18, 18, 0, tzinfo=am.TZ)
    timed_res, actual, log = _backfill_fixture(now)
    history = [
        {"t": (now - timedelta(hours=6)).isoformat(), "rmse": 2.0,     # in horizon, vervuild
         "log_fp": "oude-log"},
        {"t": (now - timedelta(hours=50)).isoformat(), "rmse": 2.0,    # buiten horizon → bevroren
         "log_fp": "oude-log"},
        {"t": (now - timedelta(hours=3)).isoformat(), "rmse": 0.11,    # in horizon, al goed → ongemoeid
         "log_fp": "oude-log"},
    ]
    changed = am.backfill_rmse_history(history, timed_res, actual, now, log)
    assert changed == 1
    p_fixed, p_frozen, p_ok = history
    assert p_fixed["rmse"] == pytest.approx(0.1, abs=1e-6)              # naar de echte model-skill
    assert p_fixed["rmse_logged"] == 2.0 and p_fixed["recomputed"] is True
    assert p_frozen["rmse"] == 2.0 and "recomputed" not in p_frozen     # grond-waarheid weg → bevroren
    assert p_frozen["log_fp"] == "oude-log"                             # buiten horizon: niet gestempeld
    assert p_ok["rmse"] == 0.11 and "recomputed" not in p_ok            # < DELTA → geen gechurn
    assert p_ok["log_fp"] != "oude-log"                                 # maar wél opnieuw gestempeld


def test_backfill_freezes_points_when_log_unchanged():
    """De churn-bug (regressie): één run met een slechte sim mag de curve NIET naar zijn
    eigen foutniveau herschrijven zolang de openingen-log niet veranderde."""
    now = datetime(2026, 6, 18, 18, 0, tzinfo=am.TZ)
    _, actual, log = _backfill_fixture(now)
    # Sim van déze run is 2°C mis (transiënte input-hapering / mislukte kalibratiestap)…
    timed_res = [(now - timedelta(hours=h), 2.0) for h in range(48, -1, -1)]
    t_p = now - timedelta(hours=6)
    fp = am.openings_fingerprint(log, t_p - timedelta(hours=am.CALIB_WINDOW_H), t_p)
    history = [{"t": t_p.isoformat(), "rmse": 0.6, "skill": 0.4, "log_fp": fp}]
    # …maar de log is ongewijzigd → het punt blijft exact staan.
    assert am.backfill_rmse_history(history, timed_res, actual, now, log) == 0
    assert history[0]["rmse"] == 0.6 and history[0]["skill"] == 0.4
    assert "recomputed" not in history[0] and "rmse_logged" not in history[0]


def test_backfill_heals_legacy_recomputed_points_once():
    """Punten van vóór de vingerafdruk-poort die door de churn-bug zijn overschreven:
    eenmalig terug naar de als-gelogde waarde, daarna gestempeld en bevroren."""
    now = datetime(2026, 6, 18, 18, 0, tzinfo=am.TZ)
    timed_res, actual, log = _backfill_fixture(now)
    t_p = now - timedelta(hours=6)
    history = [
        {"t": t_p.isoformat(), "rmse": 1.9, "rmse_logged": 0.6, "rmse_naive": 1.2,
         "recomputed": True},                                          # vervuild → herstellen
        {"t": (now - timedelta(hours=3)).isoformat(), "rmse": 0.55},   # nooit aangeraakt → stempel
    ]
    changed = am.backfill_rmse_history(history, timed_res, actual, now, log)
    assert changed == 1
    p_heal, p_stamp = history
    assert p_heal["rmse"] == 0.6 and "recomputed" not in p_heal
    assert p_heal["skill"] == pytest.approx(1 - 0.6 / 1.2, abs=1e-3)   # skill mee-hersteld
    assert p_heal["log_fp"] and p_stamp["log_fp"]                       # beide bevroren op de log-stand
    assert p_stamp["rmse"] == 0.55
    # Tweede run, zelfde log: alles blijft nu staan (geen dubbele heal, geen churn).
    assert am.backfill_rmse_history(history, timed_res, actual, now, log) == 0
    assert p_heal["rmse"] == 0.6


def test_backfill_noop_without_residuals_or_log():
    now = datetime(2026, 6, 18, 18, 0, tzinfo=am.TZ)
    timed_res, actual, log = _backfill_fixture(now)
    history = [{"t": (now - timedelta(hours=1)).isoformat(), "rmse": 1.0}]
    assert am.backfill_rmse_history(history, [], {}, now, log) == 0
    # Lege log (nog geen meldingen óf transiënte Gist-leesfout): sim op default-standen
    # → historie volledig ongemoeid, ook geen stempel.
    assert am.backfill_rmse_history(history, timed_res, actual, now, []) == 0
    assert history[0]["rmse"] == 1.0 and "recomputed" not in history[0]
    assert "log_fp" not in history[0]


def test_openings_fingerprint_tracks_only_window_relevant_changes():
    now = datetime(2026, 6, 18, 18, 0, tzinfo=am.TZ)
    start, end = now - timedelta(hours=48), now
    log = [{"t": (now - timedelta(days=10)).isoformat(), "states": {"w1": "closed"}}]
    fp0 = am.openings_fingerprint(log, start, end)
    assert fp0 == am.openings_fingerprint(list(log), start, end)       # deterministisch
    # Teruggedateerde melding ín het venster → andere vingerafdruk.
    binnen = log + [{"t": (now - timedelta(hours=12)).isoformat(), "states": {"w1": "open"}}]
    assert am.openings_fingerprint(binnen, start, end) != fp0
    # Melding ná het venster raakt dit venster niet.
    erna = log + [{"t": (now + timedelta(hours=1)).isoformat(), "states": {"w1": "open"}}]
    assert am.openings_fingerprint(erna, start, end) == fp0
    # Oudere log-edit die de beginstand wijzigt telt wél mee (openings_at op start).
    ervoor = [{"t": (now - timedelta(days=10)).isoformat(), "states": {"w1": "open"}}]
    assert am.openings_fingerprint(ervoor, start, end) != fp0


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


# ════════════════════════════════════════════════════════════════════════════════════
#  Zonnige-dag-nauwkeurigheid (stap 1 WU-zon-herschaling + stap 2 hoek-transmissie)
# ════════════════════════════════════════════════════════════════════════════════════

def test_beam_iam_factor_grazing_dropoff():
    # Loodrechte inval (cos=1) → factor 1 (default 0.7-transmissie ongewijzigd). Scherende hoek
    # → < 1. cos ≤ 0 (zon achter het vlak) → 0. Monotoon dalend naar de horizon.
    assert am.beam_iam_factor(1.0) == pytest.approx(1.0)
    assert am.beam_iam_factor(0.0) == 0.0
    assert am.beam_iam_factor(-0.3) == 0.0
    import math as _m
    f60 = am.beam_iam_factor(_m.cos(_m.radians(60)))   # 1/cos=2 → 1 − b0
    assert f60 == pytest.approx(1.0 - am.GLASS_IAM_B0, abs=1e-9)
    f75 = am.beam_iam_factor(_m.cos(_m.radians(75)))
    assert 0.0 <= f75 < f60 < 1.0


def test_facade_irradiance_beam_iam_only_touches_beam():
    # beam_iam dempt enkel de directe component; op een scherende hoek < de default, en nooit
    # de diffuse view-factor. Default (vlag uit) blijft byte-identiek.
    az = 309.0
    plain = am.facade_irradiance(az, az + 60.0, 15.0, 700.0, 120.0, 90.0, False, 0.0)
    iam = am.facade_irradiance(az, az + 60.0, 15.0, 700.0, 120.0, 90.0, False, 0.0, True)
    assert iam < plain                                   # scherende avondzon → minder transmissie
    # Alleen-diffuus (zon onder de horizon): beam-vlag verandert niets (geen beam).
    night_plain = am.facade_irradiance(az, az, -5.0, 0.0, 80.0, 90.0, False, 0.0)
    night_iam = am.facade_irradiance(az, az, -5.0, 0.0, 80.0, 90.0, False, 0.0, True)
    assert night_iam == pytest.approx(night_plain, abs=1e-12)


def test_wu_solar_scale_factor_decays_to_one():
    # Op nu (age 0) → vol k; ouder dan het venster → 1.0 (pure OM); vooruitblik (age<0) → vol k.
    assert am.wu_solar_scale_factor(1.4, 0.0) == pytest.approx(1.4)
    assert am.wu_solar_scale_factor(1.4, am.WU_SOLAR_SCALE_DECAY_H) == pytest.approx(1.0)
    assert am.wu_solar_scale_factor(1.4, 2 * am.WU_SOLAR_SCALE_DECAY_H) == pytest.approx(1.0)
    assert am.wu_solar_scale_factor(1.4, -1.0) == pytest.approx(1.4)          # vooruitblik
    mid = am.wu_solar_scale_factor(1.4, am.WU_SOLAR_SCALE_DECAY_H / 2)
    assert 1.0 < mid < 1.4
    assert am.wu_solar_scale_factor(None, 0.0) == 1.0                          # WU ontbrak → no-op


def test_build_timeline_applies_wu_solar_scale():
    # Met een WU/OM-schaal wordt de instraling op de nu-stap ~k× de ongeschaalde waarde (de OM
    # direct+diffuus schalen mee, split behouden). Zonder schaal identiek aan de basis.
    house = {"rooms": {"r": {}},
             "windows": {"w": {"room": "r", "facade_azimuth_deg": 309.0,
                               "glass_m2": 1.0, "tilt_deg": 90.0}}}
    base = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    rows = [{"dt": base + timedelta(hours=h), "T_out": 20.0, "direct": 600.0, "diffuse": 100.0,
             "wind_speed": 3.0, "wind_dir": 309.0, "gust": 5.0, "precip": 0.0, "rh": 50.0}
            for h in range(0, 4)]
    now = base + timedelta(hours=1)
    plain = am.build_timeline(house, {"hourly": rows}, [], now, 1.0)
    scaled = am.build_timeline(house, {"hourly": rows}, [], now, 1.0, wu_solar_scale=1.5)
    sp = next(s for s in plain if s["t"] == now)["irr"]["r"]
    ss = next(s for s in scaled if s["t"] == now)["irr"]["r"]
    assert sp > 0.0
    assert ss == pytest.approx(1.5 * sp, rel=1e-9)       # nu-stap: vol k


# ════════════════════════════════════════════════════════════════════════════════════
#  Trappenhuis-stratificatie (stap 3)
# ════════════════════════════════════════════════════════════════════════════════════

def test_stair_gradient_slope_and_bounds():
    # γ = kleinste-kwadraten-helling van temp t.o.v. hoogte door de kamer-punten. Warm boven →
    # positieve helling; inversie (top koeler) → 0; <2 hoogtes → 0; steile helling → geklemd.
    assert am.stair_gradient([(1.0, 22.0), (7.0, 24.4)]) == pytest.approx(0.4)   # 2.4°C / 6m
    assert am.stair_gradient([(1.0, 24.0), (7.0, 22.0)]) == 0.0                  # inversie
    assert am.stair_gradient([(3.0, 23.0)]) == 0.0                              # 1 punt
    assert am.stair_gradient([]) == 0.0
    assert am.stair_gradient([(2.0, 20.0), (2.0, 25.0)]) == 0.0                 # zelfde hoogte
    assert am.stair_gradient([(1.0, 20.0), (7.0, 100.0)]) == am.STAIR_STRAT_MAX_GRAD  # geklemd
    # Drie punten: helling via kleinste kwadraten (hier exact lineair → 0.5 °C/m).
    assert am.stair_gradient([(1.0, 22.0), (4.0, 23.5), (7.0, 25.0)]) == pytest.approx(0.5)


def _strat_house():
    return {
        "rooms": {
            "top": {"volume_m3": 32, "exterior_wall_m2": 12},
            "bot": {"volume_m3": 32, "exterior_wall_m2": 12},
            "shaft": {"volume_m3": 26, "exterior_wall_m2": 6, "stratify": True},
        },
        "doors": {
            "bot_shaft": {"between": ["bot", "shaft"], "area_m2": 1.8, "center_height_m": 1.0,
                          "default_state": "open"},
            "top_shaft": {"between": ["top", "shaft"], "area_m2": 1.8, "center_height_m": 7.0,
                          "default_state": "open"},
        },
    }


def test_stratify_zones_metadata():
    info = am._stratify_zones(_strat_house())
    assert set(info) == {"shaft"}
    z = info["shaft"]
    assert z["doors"] == {"bot": 1.0, "top": 7.0}
    assert z["z_mean"] == pytest.approx(4.0)             # (1 + 7) / 2
    assert z["z_lo"] == 1.0 and z["z_hi"] == 7.0
    # Zonder de vlag → afwezig (default ongewijzigd).
    house_off = _strat_house()
    house_off["rooms"]["shaft"].pop("stratify")
    assert am._stratify_zones(house_off) == {}


def test_stair_gamma_room_slope_and_door_filter():
    info = am._stratify_zones(_strat_house())["shaft"]
    temps = {"top": 25.0, "bot": 22.0}                   # (7m,25) (1m,22) → 3°C / 6m = 0.5 (< klem)
    # Alle deuren open → helling door beide kamers.
    assert am._stair_gamma(info, temps) == pytest.approx(am.stair_gradient([(1.0, 22.0), (7.0, 25.0)]))
    assert am._stair_gamma(info, temps) == pytest.approx(3.0 / 6.0)
    # Eén deur dicht → <2 open kamers → vlak (die kamer is ontkoppeld, geen proxy).
    assert am._stair_gamma(info, temps, open_others={"top"}) == 0.0
    # Ontbrekende kamertemp valt eveneens uit de regressie.
    assert am._stair_gamma(info, {"top": 25.0}) == 0.0


def _strat_timeline(irr_by_room: dict, hours: int = 24, states: dict | None = None) -> list[dict]:
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=am.TZ)
    tl = []
    for i in range(hours * 4 + 1):
        t = t0 + timedelta(minutes=15 * i)
        tl.append({"t": t, "T_out": 20.0, "irr": dict(irr_by_room), "states": dict(states or {}),
                   "weather": {"wind_speed": 3.0, "wind_dir": 200.0, "gust": 5.0,
                   "precip": 0.0, "direct": 0.0, "diffuse": 0.0, "rh": 55}, "dt": 900.0})
    return tl


def test_stratification_shifts_floor_coupling():
    # Zon alleen op de bovenkamer → top warmer dan onder → verticale spreiding in de koker.
    # Met stratificatie mengt de bovendeur tegen de warmere koker-top (→ bovenkamer blijft warmer)
    # en de onderdeur tegen de koelere koker-onder (→ onderkamer koeler). Zonder de vlag identiek.
    # BUOY_EXCH_C tijdelijk op 0: dit test het γ-offset-mechanisme geïsoleerd van de counterflow.
    house = _strat_house()
    params = am.default_params(house)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    tl = _strat_timeline({"top": 500.0, "bot": 0.0, "shaft": 0.0})
    seed = {z: 20.0 for z in zones}
    saved = am.BUOY_EXCH_C
    try:
        am.BUOY_EXCH_C = 0.0
        on = am.simulate(house, params, tl, seed, calib_only_rooms={"top", "bot"})
        house_off = _strat_house()
        house_off["rooms"]["shaft"].pop("stratify")
        off = am.simulate(house_off, params, tl, seed, calib_only_rooms={"top", "bot"})
    finally:
        am.BUOY_EXCH_C = saved
    assert on["Ta"]["top"] > off["Ta"]["top"] + 1e-3
    assert on["Ta"]["bot"] < off["Ta"]["bot"] - 1e-3
    # Het koker-gemiddelde blijft ~behouden (energie-behoudende symmetrische bron, geen netto bron).
    assert on["Ta"]["shaft"] == pytest.approx(off["Ta"]["shaft"], abs=0.3)


def test_buoyant_door_exchange_basics():
    # Dichte deur (area 0) of gelijke temps → 0. Groeit met ΔT (√-wet) en met de deuroppervlakte.
    assert am.buoyant_door_exchange(0.0, 26.0, 22.0) == 0.0
    assert am.buoyant_door_exchange(1.8, 24.0, 24.0) == 0.0
    q2 = am.buoyant_door_exchange(1.8, 25.0, 23.0)       # ΔT 2, T̄ 24
    q8 = am.buoyant_door_exchange(1.8, 28.0, 20.0)       # ΔT 8, zelfde T̄ 24
    assert q2 > 0.0
    assert q8 == pytest.approx(q2 * 2.0, rel=1e-6)       # ΔT 8 vs 2 → √4 = 2×
    assert am.buoyant_door_exchange(3.6, 25.0, 23.0) == pytest.approx(2.0 * q2, rel=1e-6)
    # Grootte-orde: ~2°C over een 1.8 m² deur → zo'n 0.05–0.15 m³/s (honderden m³/h) — de
    # menging die het netto-netwerkdebiet mist.
    assert 0.05 < q2 < 0.15


def test_counterflow_pins_shaft_to_open_door_rooms():
    # Zon op de koker zelf. Deuren OPEN → de counterflow mengt de koker naar de kamers (gat
    # klein). Deuren DICHT → geen uitwisseling → de warmte poolt in de koker (gat groot) —
    # "office-deur dicht → de warmte gaat daarheen".
    house = _strat_house()
    params = am.default_params(house)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    irr = {"top": 0.0, "bot": 0.0, "shaft": 400.0}
    open_tl = _strat_timeline(irr)
    closed_tl = _strat_timeline(irr, states={"top_shaft": "dicht", "bot_shaft": "dicht"})
    op = am.simulate(house, params, open_tl, seed, calib_only_rooms={"top", "bot"})
    cl = am.simulate(house, params, closed_tl, seed, calib_only_rooms={"top", "bot"})
    gap_open = op["Ta"]["shaft"] - max(op["Ta"]["top"], op["Ta"]["bot"])
    gap_closed = cl["Ta"]["shaft"] - max(cl["Ta"]["top"], cl["Ta"]["bot"])
    assert gap_closed > gap_open + 1.0     # dicht → warmte poolt; open → weggemengd
    assert gap_open < 4.0                  # open deur → geen groot zwevend gat meer


def test_stair_crown_solar_display():
    # 's Avonds (irr 0) → 0, zodat de avond-inconsistentie niet terug kan komen; middagzon →
    # een paar graden, geklemd op de max.
    assert am.stair_crown(0.0) == 0.0
    assert am.stair_crown(None) == 0.0
    mid = am.stair_crown(500.0)
    assert mid == pytest.approx(am.STAIR_CROWN_K * 500.0)
    assert 1.0 < mid < am.STAIR_CROWN_MAX
    assert am.stair_crown(1e6) == am.STAIR_CROWN_MAX


def test_stair_gamma_steeper_when_top_hotter():
    # De kamers zíjn de proxy-meting: hoe warmer de bovenkamer t.o.v. de onderkamer, hoe steiler γ
    # (de zon zit al ín de kamertemp — geen aparte zon-constante meer nodig).
    info = am._stratify_zones(_strat_house())["shaft"]
    mild = am._stair_gamma(info, {"top": 23.0, "bot": 22.0})
    hot = am._stair_gamma(info, {"top": 26.0, "bot": 22.0})
    assert hot > mild > 0.0


def test_counterflow_bypasses_vent_eff():
    # De counterflow is een fysieke orifice-term (zelfde argument als de vaste CD) en gaat
    # BUITEN de geleerde × vent_eff om: een laag geleerde meng-efficiëntie mag de koker-pinning
    # niet mee-dempen (dat liet de sensorloze koker ~1°C ónder zijn open-deur-kamers hangen).
    house = _strat_house()
    zones = list(house["rooms"])
    seed = {z: 20.0 for z in zones}
    irr = {"top": 0.0, "bot": 0.0, "shaft": 400.0}
    tl = _strat_timeline(irr)
    p_hi = am.default_params(house)
    p_hi["vent_eff"] = 1.0
    p_lo = am.default_params(house)
    p_lo["vent_eff"] = 0.1
    hi = am.simulate(house, p_hi, tl, seed, calib_only_rooms={"top", "bot"})
    lo = am.simulate(house, p_lo, tl, seed, calib_only_rooms={"top", "bot"})
    gap_hi = hi["Ta"]["shaft"] - max(hi["Ta"]["top"], hi["Ta"]["bot"])
    gap_lo = lo["Ta"]["shaft"] - max(lo["Ta"]["top"], lo["Ta"]["bot"])
    # Ook met vent_eff op zijn ondergrens blijft de koker aan de open-deur-kamers gepind …
    assert gap_lo < 4.0
    # … en de pinning-sterkte hangt er nauwelijks van af (vóór de fix schaalde het gat ~×10 mee).
    assert abs(gap_lo - gap_hi) < 1.0


def _strat_dashboard_row(states=None, irr_roof=0.0, ta_all=None, rid="shaft"):
    # Minimale, deterministische aanroep van _room_dashboard_row: ta_all wordt met de hand
    # gezet zodat γ/pin-fout/top/onder exact narekenbaar zijn.
    house = _strat_house()
    params = am.default_params(house)
    tl = _strat_timeline({"top": 0.0, "bot": 0.0, "shaft": 0.0}, hours=1, states=states)
    now_step = tl[-1]
    now_step["irr_roof"] = {"shaft": irr_roof}
    ta = ta_all or {"shaft": 23.0, "top": 25.0, "bot": 22.0}
    ctx = {
        "zpar": am._zone_thermal_params(house, params),
        "ta_all": ta, "tm_all": {}, "now_step": now_step,
        "int_profile_now": 1.0, "veff": 1.0, "rho_cp": 1.2 * am.CP_AIR,
        "strat": am._stratify_zones(house), "pw_now": {},
    }
    return am._room_dashboard_row(rid, house["rooms"][rid], house, params,
                                  {}, {"series": {}}, tl, {}, now_step["t"], ctx)


def test_stair_pin_error_and_display_split():
    # ta 23/25/22 op deurhoogtes 7/1, z_mean 4, z_lo 1, z_hi 7 → γ exact 0.5 °C/m.
    row = _strat_dashboard_row()
    assert row["stair_gradient_c_per_m"] == pytest.approx(0.5)
    assert row["stair_crown_c"] == 0.0
    # Weergave-split pivoteert op z_mean; kroon (0) alleen op top.
    assert row["predicted_temp_top"] == pytest.approx(23.0 + 0.5 * (7.0 - 4.0))
    assert row["predicted_temp_bottom"] == pytest.approx(23.0 + 0.5 * (1.0 - 4.0))
    # Pin-fout = koker-op-deurhoogte − kamer, per open deur (− = koker leest kouder).
    assert row["stair_pin_error_c"] == {"bot": pytest.approx(-0.5), "top": pytest.approx(-0.5)}


def test_stair_crown_only_on_top_display():
    # Dak-instraling → kroon alleen bovenop de γ-lijn; de onderkant blijft ongemoeid.
    row = _strat_dashboard_row(irr_roof=500.0)
    crown = am.stair_crown(500.0)
    assert row["stair_crown_c"] == pytest.approx(round(crown, 2))
    assert row["predicted_temp_top"] == pytest.approx(23.0 + 0.5 * 3.0 + crown, abs=0.01)
    assert row["predicted_temp_bottom"] == pytest.approx(23.0 - 0.5 * 3.0)


def test_stair_pin_error_empty_when_doors_closed():
    # Dichte deuren → geen open kamers → γ 0, geen pin-fouten, top == onder == gemiddelde.
    row = _strat_dashboard_row(states={"top_shaft": "dicht", "bot_shaft": "dicht"})
    assert row["stair_gradient_c_per_m"] == 0.0
    assert row["stair_pin_error_c"] == {}
    assert row["predicted_temp_top"] == pytest.approx(23.0)
    assert row["predicted_temp_bottom"] == pytest.approx(23.0)


def test_no_strat_fields_on_plain_room():
    # Niet-stratify kamers krijgen de strat-velden niet (additief, alleen op de koker).
    row = _strat_dashboard_row(rid="top")
    assert "stair_pin_error_c" not in row
    assert "predicted_temp_top" not in row


# ── 8. Groundwork P9/P10: end_h + per_window_solar + merged_params ──────────────────

def _pw_house():
    return {
        "rooms": {"r": {}},
        "windows": {
            "w1": {"room": "r", "facade_azimuth_deg": 309.0, "glass_m2": 1.0,
                   "tilt_deg": 90.0},
            "w2": {"room": "r", "facade_azimuth_deg": 129.0, "glass_m2": 2.0,
                   "tilt_deg": 90.0, "shading": "lamella",
                   "shade": {"factor": 0.12, "label": "Gordijn"}},
        },
    }


def _pw_rows(base):
    return [{"dt": base + timedelta(hours=h), "T_out": 16.0, "direct": 600.0,
             "diffuse": 100.0, "wind_speed": 3.0, "wind_dir": 309.0, "gust": 5.0,
             "precip": 0.0, "rh": 60.0} for h in range(0, 5)]


def test_build_timeline_end_h_default_unchanged():
    # Default end_h=2.0 → het raster eindigt exact op now+2u (het oude gedrag);
    # een expliciete end_h rekt de vooruitblik op zonder iets anders te raken.
    house = _pw_house()
    base = datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc)
    now = base + timedelta(hours=2)
    weather = {"hourly": _pw_rows(base)}
    grid_default = am.build_timeline(house, weather, [], now, window_h=1.0)
    assert grid_default[-1]["t"] == now + timedelta(hours=2)
    grid_long = am.build_timeline(house, weather, [], now, window_h=1.0, end_h=13.0)
    assert grid_long[-1]["t"] == now + timedelta(hours=13)
    # Het overlappende deel is identiek (zelfde drivers, zelfde middeling).
    for s_def, s_long in zip(grid_default, grid_long):
        assert s_def["t"] == s_long["t"]
        assert s_def["irr"]["r"] == pytest.approx(s_long["irr"]["r"], abs=1e-12)


def test_per_window_solar_matches_room_irr():
    # De per-kamer irr in build_timeline is exact het substap-gemiddelde van de
    # per-raam-sommen — de extractie veranderde de boekhouding niet.
    house = _pw_house()
    base = datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc)
    now = base + timedelta(hours=2)
    grid = am.build_timeline(house, {"hourly": _pw_rows(base)}, [], now, window_h=0.5)
    step = next(s for s in grid if s["t"] == now)
    expected = 0.0
    for j in range(am.SOLAR_SUBSTEPS):
        ts = now + timedelta(hours=0.25 * (j + 0.5) / am.SOLAR_SUBSTEPS)
        s_az, s_el = am.sun_position(am._LAT, am._LON, ts.astimezone(timezone.utc))
        pw = am.per_window_solar(house, {}, s_az, s_el, 600.0, 100.0)
        expected += (pw["w1"] + pw["w2"]) / am.SOLAR_SUBSTEPS
    assert step["irr"]["r"] == pytest.approx(expected, abs=1e-9)


def test_per_window_solar_respects_shade_state():
    # Een dicht gemelde bedienbare zonwering schaalt dat raam met z'n factor
    # (× de statische shading); het andere raam blijft ongemoeid.
    house = _pw_house()
    open_pw = am.per_window_solar(house, {}, 129.0, 40.0, 600.0, 100.0)
    closed_pw = am.per_window_solar(house, {"w2_shade": "dicht"}, 129.0, 40.0,
                                    600.0, 100.0)
    assert closed_pw["w2"] == pytest.approx(open_pw["w2"] * 0.12, rel=1e-9)
    assert closed_pw["w1"] == pytest.approx(open_pw["w1"], abs=1e-12)
    # Statische lamella (0.9) zit er in beide standen overheen.
    bare = dict(house["windows"]["w2"])
    bare.pop("shade")
    bare.pop("shading")
    house_bare = {"rooms": {"r": {}}, "windows": {"w2": bare}}
    bare_pw = am.per_window_solar(house_bare, {}, 129.0, 40.0, 600.0, 100.0)
    assert open_pw["w2"] == pytest.approx(bare_pw["w2"] * 0.9, rel=1e-9)


def test_merged_params_fills_new_rooms_and_keys():
    house = {"rooms": {"a": {}, "b": {}}}
    learned = {"params": {"cp_shelter": 0.42, "a": {"c_air": 123.0}},
               "physics_rev": am.PHYSICS_REV}
    p = am.merged_params(house, learned)
    assert p["cp_shelter"] == 0.42                     # geleerd blijft staan
    assert p["a"]["c_air"] == 123.0
    for k in am.PER_ROOM_PARAMS:                        # ontbrekende keys → prior
        assert k in p["a"] and k in p["b"]
    assert p["b"]["c_air"] == am.PRIORS["c_air"]        # nieuwe kamer → priors
    for g in am.GLOBAL_PARAMS:
        assert g in p


# ── Leercurve-uitdunning (thin_rmse_history) + R8-guards ─────────────────────────────

def _lp(t, **kw):
    """Leercurve-punt met tijdstip t (aware datetime) en optionele extra velden."""
    return {"t": t.isoformat(), "rmse": kw.pop("rmse", 0.5), **kw}


def test_thin_rmse_history_keeps_recent_full_resolution():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=am.TZ)
    hist = [_lp(now - timedelta(minutes=15 * i))
            for i in range(int(am.CALIB_WINDOW_H * 4) - 1, -1, -1)]
    assert am.thin_rmse_history(hist, now) == hist   # alles binnen het venster → onaangeroerd


def test_thin_rmse_history_thins_old_to_hourly_and_drops_ancient():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=am.TZ)
    old = (now - timedelta(days=4)).replace(minute=0, second=0, microsecond=0)
    hist = [_lp(now - timedelta(days=am.RMSE_HISTORY_DAYS, hours=1))]   # te oud → weg
    for h in (0, 1):
        hist += [_lp(old + timedelta(hours=h, minutes=m)) for m in (0, 15, 30, 45)]
    out = am.thin_rmse_history(hist, now)
    assert len(out) == 2                              # één per klok-uur
    assert all(datetime.fromisoformat(p["t"]).minute == 45 for p in out)  # laatste wint
    assert [p["t"] for p in out] == sorted(p["t"] for p in out)           # chronologisch


def test_thin_rmse_history_prefers_live_representative():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=am.TZ)
    h0 = (now - timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    hist = [_lp(h0, rmse=0.4),                                        # live, eerst
            _lp(h0 + timedelta(minutes=15), rmse=0.6, held=True),     # later maar held
            _lp(h0 + timedelta(minutes=30), rmse=0.9, paused=True)]   # laatst maar paused
    out = am.thin_rmse_history(hist, now)
    assert len(out) == 1 and out[0]["rmse"] == 0.4    # live verdringt latere held/paused
    # Bucket zónder live punt → gewoon de laatste.
    hist2 = [_lp(h0, rmse=0.4, held=True), _lp(h0 + timedelta(minutes=15), rmse=0.6, held=True)]
    assert am.thin_rmse_history(hist2, now)[0]["rmse"] == 0.6


def test_thin_rmse_history_idempotent_and_capped(monkeypatch):
    now = datetime(2026, 7, 7, 12, 0, tzinfo=am.TZ)
    hist = [_lp(now - timedelta(hours=h, minutes=m))
            for h in range(int(am.RMSE_HISTORY_DAYS * 24) - 1, -1, -1) for m in (30, 15, 0)]
    once = am.thin_rmse_history(hist, now)
    assert am.thin_rmse_history(once, now) == once    # idempotent
    assert len(once) < len(hist)
    monkeypatch.setattr(am, "RMSE_HISTORY_KEEP", 5)   # hard vangnet blijft werken
    assert len(am.thin_rmse_history(hist, now)) == 5


def test_thin_rmse_history_feeds_weekjournaal_lookback():
    # De uitgedunde curve moet het weekjournaal een écht ≥6.5-daags vergelijkpunt geven
    # (voorheen strandde de "week"-trend op een 2.5-daagse count-slice).
    import weekjournaal as wj
    now = datetime(2026, 7, 7, 12, 0, tzinfo=am.TZ)
    hist, t = [], now - timedelta(days=am.RMSE_HISTORY_DAYS)
    while t <= now:
        old = t < now - timedelta(days=5)
        hist.append({"t": t.isoformat(), "rmse": 0.8 if old else 0.5, "skill": 0.4})
        t += timedelta(minutes=15)
    out = am.thin_rmse_history(hist, now)
    s = wj.twin_section({"rmse_history": out}, now)
    assert s and "was 0.80°" in s                      # vergelijkpunt ligt ≥6.5 d terug


def test_merged_params_strips_cd_fossil():
    house = {"rooms": {"a": {}}}
    learned = {"params": {"cd": 0.30, "cp_shelter": 0.4, "a": {"c_air": 1.2}},
               "physics_rev": am.PHYSICS_REV}
    p = am.merged_params(house, learned)
    assert "cd" not in p                               # fossiel gestript
    assert p["cp_shelter"] == 0.4                      # geleerde waarden onaangeroerd


def test_clamp_to_bounds_clamps_solver_state():
    keys = [("global", "cp_shelter"), ("a", "solar_gain")]
    lo_cp, hi_cp = am.BOUNDS["cp_shelter"]
    lo_sg, hi_sg = am.BOUNDS["solar_gain"]
    assert am._clamp_to_bounds([-5.0, 99.0], keys) == [lo_cp, hi_sg]
    mid = [(lo_cp + hi_cp) / 2.0, (lo_sg + hi_sg) / 2.0]
    assert am._clamp_to_bounds(mid, keys) == mid       # binnen de band → ongewijzigd


def test_calibrate_nan_final_rmse_returns_old_params(monkeypatch):
    # R8: een NaN in de geblende eind-RMSE mag nooit geblende params de persist-keten in
    # duwen — calibrate valt dan terug op de ingevoerde params + de pre-solve-RMSE.
    house = _toy_house()
    tl = _varying_timeline(hours=6)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    sim = am.simulate(house, am.default_params(house), tl, seed,
                      calib_only_rooms=set(house["rooms"]))
    actual = {rid: sim["series"][rid][::4] for rid in house["rooms"]}
    p0 = am.default_params(house)
    calls = []

    def fake_rmse(res):
        calls.append(1)
        return float("nan") if len(calls) == 1 else 0.123   # 1e = final blend, 2e = r0-fallback

    monkeypatch.setattr(am, "rmse", fake_rmse)
    p1, r1 = am.calibrate(house, p0, tl, seed, actual, max_iter=0, time_budget_s=5)
    assert p1 is p0
    assert r1 == 0.123


# ── Diagnostiek-tool (tools/airflow_diagnostics.py) ──────────────────────────────────

def _diag():
    import importlib
    import os
    import sys as _sys
    tools_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
    if tools_dir not in _sys.path:
        _sys.path.insert(0, tools_dir)
    return importlib.import_module("airflow_diagnostics")


def test_diagnostics_regime_filters_since_fellback_and_adds_recent_table():
    dg = _diag()
    now = datetime(2026, 7, 7, 12, 0, tzinfo=am.TZ)
    hist = []
    for d in range(3, -1, -1):   # 4 dagen, elk 6 punten
        for h in range(6):
            t = now - timedelta(days=d, hours=h)
            hist.append({"t": t.isoformat(), "rmse": 1.0 if d >= 2 else 0.4,
                         "skill": 0.5, "wx": {"tmax": 26.0, "solar_mean": 250}})
    hist.append({"t": now.isoformat(), "rmse": 9.9, "fell_back": True,
                 "wx": {"tmax": 26.0, "solar_mean": 250}})
    rep = dg.regime_report({"rmse_history": hist})
    assert "Volledig venster" in rep
    assert "Laatste 48u" in rep                 # tweede (post-convergentie-)tabel
    assert "9.9" not in rep                     # fell_back-punt uit de bins
    # --since filtert de leerfase weg: alleen de 0.4-punten blijven over.
    rep2 = dg.regime_report({"rmse_history": hist}, since=now - timedelta(days=1, hours=12))
    assert "| 25–28 | 12 | 0.40 |" in rep2


def test_diagnostics_room_residuals_and_per_room_matrix():
    dg = _diag()
    t0 = datetime(2026, 7, 6, 4, 0, tzinfo=am.TZ)
    mk = lambda h, v: {"t": (t0 + timedelta(hours=h)).isoformat(), "temp": v}  # noqa: E731
    room = {"predicted_series": [mk(0, 20.0), mk(2, 22.0)],
            "actual_series": [mk(0, 20.5), mk(1, 20.5), mk(2, 21.5)]}
    res = dg.room_residuals(room)
    assert [round(d, 2) for _, d in res] == [-0.5, 0.5, 0.5]   # geïnterpoleerd voorspeld
    rep = dg.residual_report({"rooms": {"a": room, "b": room}})
    assert "Bias per uur, per kamer" in rep
    assert "| uur | a | b |" in rep


def test_diagnostics_pearson():
    dg = _diag()
    assert dg._pearson([(0, 0), (1, 1), (2, 2)]) == pytest.approx(1.0)
    assert dg._pearson([(0, 2), (1, 1), (2, 0)]) == pytest.approx(-1.0)
    assert dg._pearson([(1, 1), (1, 2), (1, 3)]) is None       # geen x-variantie
    assert dg._pearson([(0, 0)]) is None                       # te weinig punten


def test_diagnostics_solar_decomp_offline(monkeypatch):
    # De decompositie zelf (residu ↔ per-raam-W-correlatie) offline, met een nep-weerfetch:
    # het ZO-raam moet het middag-residu dragen, het noord-raam (diffuse_only) nauwelijks.
    dg = _diag()
    t0 = datetime(2026, 7, 6, 6, 0, tzinfo=am.TZ)
    rows = [{"dt": t0 + timedelta(hours=h), "direct": 500.0, "diffuse": 100.0}
            for h in range(0, 40)]
    monkeypatch.setattr(am, "fetch_weather", lambda: {"hourly": rows})
    house = {"rooms": {"living": {}},
             "windows": {"zo": {"room": "living", "facade_azimuth_deg": 135.0,
                                "glass_m2": 2.0, "label": "tuindeuren"},
                         "n": {"room": "living", "facade_azimuth_deg": 0.0,
                               "glass_m2": 1.0, "diffuse_only": True, "label": "noord"}}}
    monkeypatch.setattr(am, "load_house", lambda: house)
    mk = lambda h, v: {"t": (t0 + timedelta(hours=h)).isoformat(), "temp": v}  # noqa: E731
    # Residu piekt rond de middag (uren 4-8 na 06:00 = 10-14u): zon-gedreven fout.
    act = [mk(h, 22.0) for h in range(14)]
    pred = [mk(h, 22.0 + (1.0 if 4 <= h <= 8 else 0.0)) for h in range(14)]
    data = {"rooms": {"living": {"predicted_series": pred, "actual_series": act}},
            "openings": {}}
    rep = dg.solar_decomp_report(data, "living")
    assert "`zo`" in rep and "`n`" in rep and "tuindeuren" in rep
    import re
    corr = {m[0]: float(m[1]) for m in
            re.findall(r"\| `(\w+)` \| \w* \| ([+-][\d.]+|—)", rep) if m[1] != "—"}
    assert corr.get("zo", 0) > 0.3                    # ZO-raam correleert met het residu
    # Noord-raam: vlakke diffuse → geen W-variantie → geen (of veel zwakkere) correlatie.
    assert corr.get("n", 0.0) < corr["zo"]


def test_diagnostics_solar_decomp_survives_network_failure(monkeypatch):
    dg = _diag()

    def boom():
        raise OSError("proxy dicht")

    monkeypatch.setattr(am, "fetch_weather", boom)
    t0 = datetime(2026, 7, 6, 6, 0, tzinfo=am.TZ)
    mk = lambda h, v: {"t": (t0 + timedelta(hours=h)).isoformat(), "temp": v}  # noqa: E731
    series = [mk(h, 22.0) for h in range(10)]
    data = {"rooms": {"living": {"predicted_series": series, "actual_series": series}},
            "openings": {}}
    monkeypatch.setattr(am, "load_house",
                        lambda: {"rooms": {"living": {}},
                                 "windows": {"w": {"room": "living", "glass_m2": 1.0}}})
    rep = dg.solar_decomp_report(data, "living")
    assert "niet bereikbaar" in rep                    # nette sectie i.p.v. traceback

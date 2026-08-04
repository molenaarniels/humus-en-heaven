"""Regressie-vangnet voor de fysicakern van de ventilatie-tweeling (`vent_physics.py`, Project 13).

Port van de overlevende fysica-secties uit tests/test_airflow_model.py, aangepast aan de
nieuwe API: geen muteerbare module-globals meer — alle run-gebonden ankers reizen via
RunContext (ctx als verplicht 5e positioneel argument van simulate()).

Pure-functie-checks — géén netwerk, géén Gist, géén bestanden:
  1. Zonpositie tegen bekende referentiewaarden + gevelinstraling (DNI-conventie, beam-IAM,
     horizon-obstakel).
  2. Cp-druk: teken + symmetrie + dak-zuiging; WIND_REF_Z + effectief openingsoppervlak.
  3. Luchtstroomnetwerk: massabehoud, een analytische cross-ventilatie-case, de gedempte
     herkansing bij oscillatie, eenzijdige ventilatie.
  4. 2-knoops RC-model: relaxeert naar de juiste evenwichtstemp; tussenwoning-termen;
     dak/bodem/interzone; tm_seed; trap-stratificatie.
"""
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import vent_io as vio
import vent_physics as vp

TZ = vp.TZ

# Reproduceert exact het oude module-default-gedrag (_LAT/_LON/_NEIGHBOR_TEMP/_GROUND_TEMP).
CTX = vp.RunContext(lat=52.09, lon=5.12,
                    neighbor_temp=vp.NEIGHBOR_TEMP, ground_temp=vp.GROUND_TEMP)


# ── 1. Zonpositie ────────────────────────────────────────────────────────────────────

def test_sun_position_summer_noon_utrecht():
    # 21 juni, ware zonnemiddag ~11:42 UTC voor Utrecht (lon 5.12°O: 12:00 − 20½min −
    # eqtime). Op ware middag staat de zon pal zuid (az≈180) en op maximale hoogte
    # (≈90−(lat−23.44)=61°).
    az, el = vp.sun_position(52.09, 5.12, datetime(2026, 6, 21, 11, 42, tzinfo=timezone.utc))
    assert abs(az - 180.0) < 4.0
    assert abs(el - 61.3) < 2.0


def test_sun_position_night_below_horizon():
    # Middernacht UTC in juni → zon onder de horizon (negatieve elevatie).
    _, el = vp.sun_position(52.09, 5.12, datetime(2026, 6, 21, 0, 0, tzinfo=timezone.utc))
    assert el < 0.0


def test_sun_morning_east_afternoon_west():
    az_morning, _ = vp.sun_position(52.09, 5.12, datetime(2026, 6, 21, 5, 0, tzinfo=timezone.utc))
    az_afternoon, _ = vp.sun_position(52.09, 5.12, datetime(2026, 6, 21, 16, 0, tzinfo=timezone.utc))
    assert az_morning < 180.0      # ochtend: zon in het oosten
    assert az_afternoon > 180.0    # middag/avond: zon in het westen


# ── 2. Cp-druk ───────────────────────────────────────────────────────────────────────

def test_cp_sign():
    assert vp.cp_coefficient(0) > 0.5      # loef: positieve druk
    assert vp.cp_coefficient(90) < 0.0     # zijgevel: onderdruk
    assert vp.cp_coefficient(180) < 0.0    # lij: onderdruk


def test_cp_symmetry():
    for theta in (10, 45, 70, 120):
        assert vp.cp_coefficient(theta) == pytest.approx(vp.cp_coefficient(-theta), abs=1e-9)


def test_cp_roof_always_suction():
    # Een (bijna) plat dak staat op élke windrichting onder onderdruk (geen loeflob).
    for theta in range(0, 361, 15):
        assert vp.cp_roof(theta) < 0.0
    # Loefrand iets minder negatief dan de lijrand.
    assert vp.cp_roof(0) > vp.cp_roof(180)


def test_cp_tilted_endpoints():
    # tilt 90° (verticaal, default) → exact het muurprofiel: backward-compatible.
    for theta in (0, 45, 90, 180):
        assert vp.cp_tilted(theta, 90.0) == pytest.approx(vp.cp_coefficient(theta), abs=1e-12)
        assert vp.cp_tilted(theta, 0.0) == pytest.approx(vp.cp_roof(theta), abs=1e-12)
    # Een plat dakraam op de loef heeft géén overdruk meer (muur wél).
    assert vp.cp_coefficient(0) > 0.5
    assert vp.cp_tilted(0, 0.0) < 0.0


def test_facade_irradiance_default_unchanged():
    # Zonder diffuse_only-argument blijft de instraling identiek (backward-compatible).
    i_default = vp.facade_irradiance(219.0, 219.0, 45.0, 700.0, 150.0, 90.0)
    i_explicit = vp.facade_irradiance(219.0, 219.0, 45.0, 700.0, 150.0, 90.0, False)
    assert i_default == pytest.approx(i_explicit, abs=1e-12)
    assert i_default > 150.0  # bevat de directe beam-bijdrage


def test_facade_irradiance_diffuse_only_drops_beam():
    # Zon recht op een ZW-raam: normaal véél directe instraling, diffuse_only laat alleen
    # de hemel-viewfactor over (geen beam) — het huis ervóór schermt de directe zon af.
    full = vp.facade_irradiance(219.0, 219.0, 45.0, 700.0, 150.0, 90.0, False)
    diff = vp.facade_irradiance(219.0, 219.0, 45.0, 700.0, 150.0, 90.0, True)
    assert diff == pytest.approx(150.0 * 0.5, abs=1e-9)   # verticaal → sky_view 0.5
    assert diff < full
    # 's Nachts (zon onder horizon) is er sowieso geen beam → beide gelijk.
    night_full = vp.facade_irradiance(219.0, 219.0, -5.0, 0.0, 80.0, 90.0, False)
    night_diff = vp.facade_irradiance(219.0, 219.0, -5.0, 0.0, 80.0, 90.0, True)
    assert night_full == pytest.approx(night_diff, abs=1e-12)


def test_facade_irradiance_horizon_blocks_low_sun():
    # Overburen (+ boom) vóór de NW-gevel: staat de zon lager dan de obstakel-elevatie, dan
    # valt de directe beam weg en blijft enkel het (voor het obstakel gereduceerde) diffuus
    # over — als diffuse_only, maar elevatie-afhankelijk i.p.v. permanent.
    az = 309.0
    above = vp.facade_irradiance(az, az, 20.0, 700.0, 120.0, 90.0, False, 14.0)  # 20° > obstakel
    below = vp.facade_irradiance(az, az, 8.0, 700.0, 120.0, 90.0, False, 14.0)   # 8° < obstakel
    reduced_sky_view = 0.5 * (1.0 - vp.horizon_diffuse_reduction(14.0))
    assert below == pytest.approx(120.0 * reduced_sky_view, abs=1e-9)
    assert below < 120.0 * 0.5   # het obstakel neemt ook een deel van de diffuse hemel weg
    assert below < above
    # horizon_deg default 0 → identiek aan geen-obstakel, en die ziet de 8°-zon nog wél als beam.
    no_obstacle = vp.facade_irradiance(az, az, 8.0, 700.0, 120.0, 90.0, False)
    assert no_obstacle == pytest.approx(
        vp.facade_irradiance(az, az, 8.0, 700.0, 120.0, 90.0, False, 0.0), abs=1e-12)
    assert no_obstacle > below


def test_facade_irradiance_flat_plane_equals_ghi_when_direct_is_horizontal(monkeypatch):
    # De invariant die de `direct`-conventie écht vastlegt: Open-Meteo levert de directe
    # component op het HORIZONTALE vlak (GHI = diffuus + DNI·sin(zonshoogte)), dus een plat
    # vlak (tilt 0, sky_view 1.0) moet exact `direct + diffuus` teruggeven. Met de vlag uit
    # doet de functie `direct × cos(zenit)` en leest ze structureel te laag — dat is precies
    # de fout die deze vlag adresseert.
    monkeypatch.setattr(vp, "DIRECT_IS_HORIZONTAL", True)
    diffuse = 150.0
    for el in (5.0, 10.0, 21.3, 30.0, 45.0, 56.0, 80.0):
        direct_h = 600.0 * math.sin(math.radians(el))     # horizontale directe component
        got = vp.facade_irradiance(0.0, 180.0, el, direct_h, diffuse, tilt_deg=0.0)
        assert got == pytest.approx(direct_h + diffuse, abs=1e-9)


def test_facade_irradiance_direct_is_horizontal_only_lifts_the_beam(monkeypatch):
    az, el, direct_h, diffuse = 309.0, 21.3, 223.0, 143.0
    monkeypatch.setattr(vp, "DIRECT_IS_HORIZONTAL", False)   # de oude conventie, expliciet
    off = vp.facade_irradiance(az, az - 36.0, el, direct_h, diffuse, 90.0, False, 14.0)
    monkeypatch.setattr(vp, "DIRECT_IS_HORIZONTAL", True)
    on = vp.facade_irradiance(az, az - 36.0, el, direct_h, diffuse, 90.0, False, 14.0)
    # Lage zon → de 1/sin(el)-correctie tilt de beam fors op (hier ~2.4×).
    assert on > 2.0 * off
    # Het diffuse deel blijft exact gelijk: het verschil zit volledig in de beam.
    sky = 0.5 * (1.0 - vp.horizon_diffuse_reduction(14.0))
    assert (on - diffuse * sky) / (off - diffuse * sky) == pytest.approx(
        1.0 / math.sin(math.radians(el)), rel=1e-9)
    # Geen beam (zon onder het obstakel, of 's nachts) → de vlag verandert niets.
    for sun_el, dh in ((8.0, 223.0), (-5.0, 0.0)):
        monkeypatch.setattr(vp, "DIRECT_IS_HORIZONTAL", False)
        a = vp.facade_irradiance(az, az, sun_el, dh, diffuse, 90.0, False, 14.0)
        monkeypatch.setattr(vp, "DIRECT_IS_HORIZONTAL", True)
        assert vp.facade_irradiance(az, az, sun_el, dh, diffuse, 90.0, False,
                                    14.0) == pytest.approx(a, abs=1e-12)


def test_facade_irradiance_direct_is_horizontal_clamped_near_horizon(monkeypatch):
    # 1/sin(el) is singulier op de horizon; SIN_EL_FLOOR + MAX_DNI houden 'm eindig en
    # fysiek. Vlak boven de horizon is de beam sowieso verwaarloosbaar t.o.v. het diffuus.
    monkeypatch.setattr(vp, "DIRECT_IS_HORIZONTAL", True)
    val = vp.facade_irradiance(180.0, 180.0, 0.05, 5.0, 40.0, 90.0)
    assert math.isfinite(val)
    assert val <= vp.MAX_DNI + 40.0
    # Een absurd hoge horizontale beam bij lage zon blijft onder de DNI-bovengrens.
    high = vp.facade_irradiance(180.0, 180.0, 2.0, 900.0, 0.0, 90.0)
    assert high <= vp.MAX_DNI


def _hotties_network(wind, p_init=None):
    """Het echte huis met alléén het hotties-raam open — de configuratie waarop de
    drukoplosser bij ≥6 m/s ging oscilleren en een niet-oplossing teruggaf."""
    house = vio.load_house()
    params = vio.merged_params(house, {})
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    states = {k: "dicht" for k in list(house["windows"]) + list(house["doors"])}
    for v in house["vents"]:
        states[v] = "open"
    states["hotties_window"] = "open"
    temps = {z: 23.0 for z in zones}
    ops = vp.build_openings(house, states, {"wind_speed": wind, "wind_dir": 309.0,
                                            "T_out": 20.0}, params, temps, 20.0)
    net = vp.solve_network(zones, ops, temps, 20.0, P_init=p_init)
    vol = house["rooms"]["hotties"]["volume_m3"]
    return net, net["fresh"]["hotties"] * 3600.0 / vol


def test_solve_network_converges_at_high_wind():
    # Regressie: bij ≥6 m/s oscilleerde de volle Newton-stap (druk heen en weer tussen
    # ~12.5 en ~0.4 Pa) en gaf de solver na 40 iteraties een massabalans van ~1.4 kg/s terug
    # alsóf het een oplossing was — goed voor een fantoom-ventilatie van ~135 ACH.
    for wind in (0.5, 3.0, 5.5, 6.0, 8.0, 12.0, 15.0):
        net, ach = _hotties_network(wind)
        assert net["converged"], f"niet geconvergeerd bij {wind} m/s"
        assert net["residual"] < vp.NET_TOL
        assert 0.0 < ach < 20.0, f"onfysieke ventilatie {ach:.1f} ACH bij {wind} m/s"


def test_solve_network_no_discontinuity_across_wind():
    # De ventilatie mag met de wind meestijgen, maar niet springen: vóór de fix ging het
    # van 1.41 ACH (5.5 m/s) naar 135.08 (6.0 m/s).
    achs = [_hotties_network(w)[1] for w in (4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0)]
    for a, b in zip(achs, achs[1:]):
        assert b >= a - 1e-6              # monotoon stijgend in wind
        assert b < a * 1.5                # en zonder sprong


def test_solve_network_result_is_start_independent():
    # Dezelfde invoer moet hetzelfde antwoord geven, ongeacht de warme start. Vóór de fix
    # gaf 6 m/s 135 ACH koud maar 1.5 ACH warm-gestart vanaf de 5.5-oplossing.
    net55, ach55 = _hotties_network(5.5)
    net60, ach60 = _hotties_network(6.0)
    assert _hotties_network(6.0, p_init=net55["P"])[1] == pytest.approx(ach60, abs=1e-3)
    assert _hotties_network(5.5, p_init=net60["P"])[1] == pytest.approx(ach55, abs=1e-3)


def test_single_sided_exchange_matches_de_gids_phaff():
    # Eenzijdige ventilatie: Q = (A/2)·√(C1·U² + C2·H·ΔT + C3). Nul bij een dicht raam,
    # groeit met ΔT en met wind, en is véél vlakker in wind dan de netto-netwerkstroom
    # (die ∝ U² gaat) — dát is de hele reden dat de term bestaat.
    assert vp.single_sided_exchange(0.0, 1.2, 3.0, 23.0, 20.0) == 0.0
    q_still = vp.single_sided_exchange(1.4, 1.2, 0.0, 23.0, 20.0)
    assert q_still > 0.0                       # buoyantie alleen is al een echte stroom
    assert vp.single_sided_exchange(1.4, 1.2, 3.0, 23.0, 20.0) > q_still      # wind helpt
    assert vp.single_sided_exchange(1.4, 1.2, 0.0, 26.0, 20.0) > q_still      # ΔT helpt
    # geen temperatuurverschil én geen wind → alleen de C3-restterm, maar niet nul
    assert vp.single_sided_exchange(1.4, 1.2, 0.0, 20.0, 20.0) == pytest.approx(
        0.5 * 1.4 * math.sqrt(vp.SS_C3), rel=1e-9)
    # wind-helling: 0.5 → 6 m/s mag hooguit een factor ~2 schelen (empirisch ~1.4×)
    lo = vp.single_sided_exchange(1.4, 1.2, 0.5, 23.0, 20.0)
    hi = vp.single_sided_exchange(1.4, 1.2, 6.0, 23.0, 20.0)
    assert 1.0 < hi / lo < 2.0


def test_single_sided_fresh_only_open_vertical_windows():
    house = vio.load_house()
    # Dicht raam → geen bijdrage; open raam → een substantiële, maar eindige stroom.
    assert vp.single_sided_fresh(house, {"hotties_window": "dicht"}, {"wind_speed": 2.0},
                                 {"hotties": 23.0}, 20.0).get("hotties", 0.0) == 0.0
    ss = vp.single_sided_fresh(house, {"hotties_window": "open"}, {"wind_speed": 2.0},
                               {"hotties": 23.0}, 20.0)
    ach = ss["hotties"] * 3600.0 / house["rooms"]["hotties"]["volume_m3"]
    assert 5.0 < ach < 30.0        # eenzijdige ventilatie door een open raam ≈ 10–20 ACH
    # Platte dakramen vallen buiten de correlatie (ander regime) → geen koker-bijdrage.
    assert "stair" not in vp.single_sided_fresh(
        house, {"stair_skylight": "open"}, {"wind_speed": 2.0}, {"stair": 23.0}, 20.0)


def test_effective_fresh_takes_max_never_sum():
    house = vio.load_house()
    states, weather = {"hotties_window": "open"}, {"wind_speed": 2.0}
    temps = {"hotties": 23.0}
    ss = vp.single_sided_fresh(house, states, weather, temps, 20.0)["hotties"]
    # Netto klein → de eenzijdige term neemt het over (geen som).
    out = vp.effective_fresh({"hotties": 0.001}, house, states, weather, temps, 20.0)
    assert out["hotties"] == pytest.approx(ss)
    # Netto groot (echte dwarsventilatie) → het netwerk blijft leidend.
    big = vp.effective_fresh({"hotties": 10.0}, house, states, weather, temps, 20.0)
    assert big["hotties"] == pytest.approx(10.0)


def test_horizon_diffuse_reduction_bounds():
    # Geen obstakel → geen reductie; recht-op-de-gevel-hoog obstakel (90°) → volledige blokkade.
    assert vp.horizon_diffuse_reduction(0.0) == pytest.approx(0.0, abs=1e-12)
    assert vp.horizon_diffuse_reduction(90.0) == pytest.approx(1.0, abs=1e-9)
    # Monotoon stijgend: een hoger obstakel neemt nooit minder hemel weg.
    fracs = [vp.horizon_diffuse_reduction(h) for h in (0.0, 14.0, 28.0, 42.0, 60.0, 90.0)]
    assert fracs == sorted(fracs)


def test_wind_pressure_default_unchanged():
    # Zonder tilt_deg-argument blijft de druk identiek aan het verticale-muur-gedrag.
    rho = vp.air_density(20.0)
    p_default = vp.wind_pressure(309.0, 4.3, 5.0, 194.0, 0.5, rho)
    p_vertical = vp.wind_pressure(309.0, 4.3, 5.0, 194.0, 0.5, rho, tilt_deg=90.0)
    assert p_default == pytest.approx(p_vertical, abs=1e-12)
    # Plat dakraam op dezelfde plek → zuiging (negatief), ongeacht of de muur dat zou zijn.
    p_roof = vp.wind_pressure(309.0, 4.3, 5.0, 194.0, 0.5, rho, tilt_deg=0.0)
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
    net = vp.solve_network(["room"], ops, {"room": 22.0}, 22.0)
    # Volumebehoud (gelijke dichtheid binnen/buiten hier): in = uit.
    assert net["flows"][0] == pytest.approx(-net["flows"][1], abs=1e-3)
    # Symmetrie → kamerdruk ≈ 0 → ΔP per raam ≈ Pe.
    rho = vp.air_density(22.0)
    q_expected = Cd * A * math.sqrt(2.0 * Pe / rho)
    assert abs(net["flows"][1]) == pytest.approx(q_expected, rel=0.02)


def test_sealed_zone_does_not_break_solve():
    # Een volledig dichte zone (geen open opening) mag de hele drukoplossing niet singulier
    # maken — de per-zone infiltratielek houdt 'm welgesteld.
    house = _toy_house()
    house["junctions"]["sealed"] = {"volume_m3": 8}     # nergens mee verbonden
    house["rooms"]["a"]["from_window_data"] = "Living room"
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    params = vio.default_params(house)
    zt = {z: 24.0 for z in zones}
    ops = vp.build_openings(house, {"a_win": "open"}, {"wind_speed": 4.0, "wind_dir": 200.0},
                            params, zt, 18.0)
    net = vp.solve_network(zones, ops, zt, 18.0)
    assert all(math.isfinite(v) for v in net["pressures"].values())
    res = _node_residual(zones, ops, net["pressures"], zt, 18.0)
    assert max(abs(v) for v in res) < 1e-4


def test_network_node_mass_balance_multizone():
    # Twee kamers + een deur, wind + schoorsteen: in elke interne knoop moet de
    # netto massa nul zijn (behoudswet).
    house = _toy_house()
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    params = vio.default_params(house)
    states = {"a_win": "open", "b_win": "open"}
    zt = {"a": 26.0, "b": 24.0, "hall": 25.0}
    ops = vp.build_openings(house, states, {"wind_speed": 4.0, "wind_dir": 200.0},
                            params, zt, 18.0)
    net = vp.solve_network(zones, ops, zt, 18.0)
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
    params = vio.default_params(house)
    zt = {"a": 22.0, "b": 22.0, "hall": 22.0}
    ops = vp.build_openings(house, {"a_win": "open", "b_win": "open"},
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
    params = vio.default_params(house)
    zt = {z: 22.0 for z in zones}
    ops = vp.build_openings(house, {"a_win": "open", "b_win": "open"},
                            {"wind_speed": 6.2, "wind_dir": 39.0}, params, zt, 22.0)
    net = vp.solve_network(zones, ops, zt, 22.0)
    door_q = {op["id"]: q for op, q in zip(ops, net["flows"])
              if op["id"] in ("a_hall", "b_hall")}
    assert all(abs(q) < 0.05 for q in door_q.values())


def test_cross_facade_flow_survives():
    # De fix mag échte dwarsventilatie (loef → lij) niet doden: tegenoverliggende gevels
    # houden hun Cp-contrast en drijven een stevige doorstroom.
    house = _toy_house()                                # a_win az 180, b_win az 0
    zones = list(house["rooms"]) + list(house["junctions"])
    params = vio.default_params(house)
    zt = {z: 22.0 for z in zones}
    ops = vp.build_openings(house, {"a_win": "open", "b_win": "open"},
                            {"wind_speed": 6.0, "wind_dir": 180.0}, params, zt, 22.0)
    net = vp.solve_network(zones, ops, zt, 22.0)
    q = {op["id"]: v for op, v in zip(ops, net["flows"]) if op["id"] in ("a_win", "b_win")}
    assert q["a_win"] < -0.2                            # loef: instroom (negatief = binnenwaarts)
    assert q["b_win"] > 0.2                             # lij: uitstroom


def test_effective_open_area_casement():
    # Een wijd open draairaam is niet het volle kozijngat: open_type "casement" → ×0.5;
    # een expliciete per-element `eff_open_frac` overschrijft de type-default; zonder
    # open_type (roosters, toy-ramen) verandert er niets (×1.0).
    house = _toy_house()
    house["windows"]["a_win"]["open_type"] = "casement"
    params = vio.default_params(house)
    zt = {"a": 22.0, "b": 22.0, "hall": 22.0}
    wx = {"wind_speed": 0.0, "wind_dir": 0.0}
    ops = vp.build_openings(house, {"a_win": "open", "b_win": "open"}, wx, params, zt, 20.0)
    area = {op["id"]: op["area"] for op in ops}
    assert area["a_win"] == pytest.approx(0.6 * 0.5)   # casement-korting
    assert area["b_win"] == pytest.approx(0.5)          # geen open_type → ongewijzigd
    house["windows"]["a_win"]["eff_open_frac"] = 0.8    # expliciete override wint
    ops = vp.build_openings(house, {"a_win": "open"}, wx, params, zt, 20.0)
    area = {op["id"]: op["area"] for op in ops}
    assert area["a_win"] == pytest.approx(0.6 * 0.8)


# ── 4. 2-knoops RC-model ─────────────────────────────────────────────────────────────

def test_rc_relaxes_to_outside_no_solar():
    # Geen zon, dichte ramen, constante buitentemp → binnen relaxeert náár buiten. Het
    # gebouw heeft een grote thermische tijdconstante (~dag), dus geef het ruim de tijd.
    # We toetsen hier de pure schil-relaxatie, dus zónder de tussenwoning-warmtebronnen
    # (buren + interne last) — díe tillen de evenwichtstemp op en worden apart getoetst in
    # test_tussenwoning_terms_lift_above_outside.
    house = _toy_house()
    params = vio.default_params(house)
    for rid in house["rooms"]:
        params[rid]["ua_party"] = 0.0
        params[rid]["q_int"] = 0.0
    T_out = 18.0
    tl = _const_timeline(T_out, hours=240, irr=0.0)
    seed = {z: 26.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    sim = vp.simulate(house, params, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
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

    p_on = vio.default_params(house)                      # priors: ua_party=1, q_int=1
    sim_on = vp.simulate(house, p_on, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))

    p_off = vio.default_params(house)
    for rid in house["rooms"]:
        p_off[rid]["ua_party"] = 0.0
        p_off[rid]["q_int"] = 0.0
    sim_off = vp.simulate(house, p_off, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))

    for rid in house["rooms"]:
        # Zonder termen: terug naar buiten. Mét termen: aantoonbaar opgetild, boven buiten
        # maar niet absurd boven de buurtemp.
        assert sim_off["Ta"][rid] == pytest.approx(T_out, abs=0.4)
        assert sim_on["Ta"][rid] > T_out + 0.5
        assert sim_on["Ta"][rid] > sim_off["Ta"][rid] + 1.0
        assert sim_on["Ta"][rid] <= vp.NEIGHBOR_TEMP + 3.0


def test_rc_mass_node_lags_air_node():
    # Een sprong in de buitentemp: de luchtknoop reageert sneller dan de massaknoop,
    # dus na korte tijd ligt T_air dichter bij buiten dan T_mass.
    house = _toy_house()
    params = vio.default_params(house)
    tl = _const_timeline(30.0, hours=3, irr=0.0)   # warm buiten, koud gestart
    seed = {z: 18.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    sim = vp.simulate(house, params, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    rid = next(iter(house["rooms"]))
    assert sim["Ta"][rid] > sim["Tm"][rid]          # lucht warmde sneller op dan massa
    assert seed[rid] < sim["Tm"][rid] < sim["Ta"][rid] < 30.0


def test_sensor_outdoor_bias_blend():
    # Een sensor op de buitenmuur leest een lineaire blend richting buiten. frac=0 en
    # ontbrekende waarden zijn no-ops (mirror van wu_bias: vaste meetcorrectie).
    assert vp._sensor_temp(22.0, 12.0, 0.0) == 22.0
    assert vp._sensor_temp(22.0, 12.0, 0.25) == pytest.approx(0.75 * 22 + 0.25 * 12)
    assert vp._sensor_temp(None, 12.0, 0.25) is None
    assert vp._sensor_temp(22.0, None, 0.25) == 22.0


# ── 5. cd vastgezet, vent_eff vrijgemaakt ────────────────────────────────────────────

def test_cd_is_fixed_not_learnable():
    # `cd` is geen leerbare globale parameter meer (railde naar zijn vloer + corrumpeerde de
    # getoonde ACH/flows). Het is nu de vaste fysische constante CD; `vent_eff` draagt de
    # meng-koppeling, met een verlaagde ondergrens zodat hij niet alsnog railt.
    assert "cd" not in vp.GLOBAL_PARAMS
    assert vp.CD == vp.PRIORS["cd"]
    assert vp.BOUNDS["vent_eff"][0] == 0.1
    # default_params bevat geen `cd` meer in de geleerde vector.
    assert "cd" not in vio.default_params(_toy_house())


def test_build_openings_uses_fixed_cd():
    # build_openings negeert een (verouderde, gerailde) `cd` in params en gebruikt altijd CD.
    house = _toy_house()
    params = vio.default_params(house)
    params["cd"] = 0.30   # stale/gerailde waarde uit oude learned-artefacten
    zt = {z: 22.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    ops = vp.build_openings(house, {"a_win": "open"}, {"wind_speed": 4.0, "wind_dir": 200.0},
                            params, zt, 18.0)
    assert ops and all(op["Cd"] == vp.CD for op in ops)


# ── 5b. Interne geleiding + bodemkoppeling (fysica-rev 3) ────────────────────────────

def test_room_base_capacitances_extra_mass_is_additive():
    """De nieuwe massavlakken tellen mee, maar een huismodel zónder die velden houdt
    exact zijn oude capaciteit — anders zou elke bestaande simulate-test stil verschuiven."""
    plain = {"volume_m3": 32, "exterior_wall_m2": 12, "roof_m2": 14}
    _, c_mass_plain, _ = vp.room_base_capacitances(plain)
    assert c_mass_plain == pytest.approx((12 + 14) * 90000.0)
    rich = {**plain, "party_wall_m2": 10, "mass_floor_m2": 24}
    _, c_mass_rich, _ = vp.room_base_capacitances(rich)
    assert c_mass_rich == pytest.approx((12 + 14 + 10 + 24) * 90000.0)
    # De UA blijft puur gevel: de nieuwe vlakken zijn massa, geen extra schilverlies.
    assert vp.room_base_capacitances(rich)[2] == vp.room_base_capacitances(plain)[2]


def _ground_raw(mean_out: float) -> float:
    """De ongeklemde blend, uitgedrukt in de constanten — zodat deze test niet opnieuw
    breekt als de koppeling herijkt wordt, maar de VORM wél blijft vastliggen."""
    return vp.GROUND_SOIL_ANCHOR + vp.GROUND_AIR_COUPLING * (mean_out - vp.GROUND_SOIL_ANCHOR)


def test_ground_temp_estimate_blends_soil_and_damped_outside():
    now = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
    mild = [{"dt": now - timedelta(hours=h), "T_out": 17.0} for h in range(720)]
    assert vp.ground_temp_estimate(mild, now) == pytest.approx(_ground_raw(17.0))
    koel = [{"dt": now - timedelta(hours=h), "T_out": 9.0} for h in range(720)]
    assert vp.ground_temp_estimate(koel, now) == pytest.approx(_ground_raw(9.0))
    # De kruipruimte volgt het gedempte buiten maar loopt er nooit vóórbij: bij koppeling ≤ 1
    # blijft hij tussen het bodemanker en het buitengemiddelde in (binnen de klemmen).
    for mean_out in (9.0, 12.0, 17.0):
        g = vp.ground_temp_estimate(
            [{"dt": now - timedelta(hours=h), "T_out": mean_out} for h in range(720)], now)
        lo, hi = sorted((vp.GROUND_SOIL_ANCHOR, mean_out))
        assert max(lo, vp.GROUND_TEMP_MIN) - 1e-9 <= g <= min(hi, vp.GROUND_TEMP_MAX) + 1e-9
    # Winterkant van dezelfde vangrail: bij koppeling 1.0 zou het anker een 30-daags
    # buitengemiddelde van 3 °C helemaal volgen; GROUND_TEMP_MIN houdt 'm op 6 °C. Dat is
    # onbeproefd terrein (geen stookseizoendata) — zie het winter-voorbehoud bij de constante.
    ijskoud = [{"dt": now - timedelta(hours=h), "T_out": 3.0} for h in range(720)]
    assert vp.ground_temp_estimate(ijskoud, now) == pytest.approx(vp.GROUND_TEMP_MIN)
    # Klemmen + terugval zonder historie.
    absurd = [{"dt": now - timedelta(hours=h), "T_out": 60.0} for h in range(720)]
    assert vp.ground_temp_estimate(absurd, now) == pytest.approx(vp.GROUND_TEMP_MAX)
    assert vp.ground_temp_estimate([], now) == pytest.approx(vp.GROUND_TEMP)


def test_ground_temp_max_is_een_vangrail_geen_zomerplafond():
    """Waakhond op een gemeten neveneffect van `GROUND_AIR_COUPLING` 0.5 → 1.0.

    `GROUND_TEMP_MAX` is bedoeld als vangrail tegen een absurd anker bij korte/rare historie.
    Bij koppeling 0.5 bond hij pas op een 30-daags buitengemiddelde van 29 °C — in Nederland
    nooit. Bij koppeling 1.0 bindt hij al op 20 °C, oftewel een doodgewone warme zomermaand:
    over het record 2026-05→08 kneep hij het bodemanker in **86 van de 265** oorsprongen af.

    Dat is gemeten in plaats van aangenomen: met de klem los (26 °C) gaat de 12u-fout van 0.660
    naar 0.662 gepoold — geen winst, en zonder per-kamer-handtekening, omdat het geleerde
    `ua_ground` het niveau van het anker gewoon absorbeert. De klem blijft dus staan.

    Deze test faalt zodra iemand de koppeling verder verhoogt zónder de vangrail mee te schalen:
    dan verschuift het bindpunt naar een nóg gewoner buitengemiddelde en is de meting hierboven
    niet meer van toepassing — er hoort dan eerst een nieuwe bij (tools/horizon_backtest.py).
    Zie AIRFLOW3_ASSESSMENT.md §6."""
    binding_mean = ((vp.GROUND_TEMP_MAX - vp.GROUND_SOIL_ANCHOR) / vp.GROUND_AIR_COUPLING
                    + vp.GROUND_SOIL_ANCHOR)
    assert binding_mean >= 20.0, (
        f"de klem bindt al bij een 30-daags buitengemiddelde van {binding_mean:.1f} °C — "
        "dat is een zomerplafond, geen vangrail")


def test_ground_term_is_inert_without_ground_m2():
    """Opt-in via de huismodel-geometrie: geen `ground_m2` → UA_ground 0 → nul-gradiënt,
    dus de ridge parkeert `ua_ground` op zijn prior en de sim is bit-identiek."""
    house = _toy_house()
    params = vio.default_params(house)
    zp = vp._zone_thermal_params(house, params)
    assert all(z.get("UA_ground", 0.0) == 0.0 for z in zp.values())
    tl = _const_timeline(16.0, hours=48, irr=0.0)
    seed = {z: 22.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    base = vp.simulate(house, params, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    params_hi = vio.default_params(house)
    for rid in house["rooms"]:
        params_hi[rid]["ua_ground"] = 4.0        # maximaal, maar zonder oppervlak: geen effect
    same = vp.simulate(house, params_hi, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    for rid in house["rooms"]:
        assert same["Ta"][rid] == pytest.approx(base["Ta"][rid])


def test_ground_coupling_pulls_a_hot_room_below_outside():
    """De kern van fysica-rev 3: mét een kruipruimte kan een kamer op een hete dag ónder de
    buitentemp uitkomen. Zonder die koude put kon het model dat niet — élke weg naar buiten
    was een warmtebron — en moest de fit élk warmte-in-kanaal naar zijn ondergrens duwen."""
    house = _toy_house()
    house["rooms"]["a"]["ground_m2"] = 20        # kruipruimte onder kamer a
    tl = _const_timeline(28.0, hours=240, irr=0.0)
    seed = {z: 28.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    params = vio.default_params(house)
    for rid in house["rooms"]:
        params[rid]["q_int"] = 0.0               # isoleer de bodem-term van de interne last
    ctx = replace(CTX, neighbor_temp=28.0, ground_temp=15.0)
    sim = vp.simulate(house, params, tl, seed, ctx, calib_only_rooms=set(house["rooms"]))
    assert sim["Ta"]["a"] < 28.0 - 1.0           # kamer mét kruipruimte zakt onder buiten
    assert sim["Ta"]["b"] > sim["Ta"]["a"]       # kamer zónder blijft warmer


def test_interzone_conductances_geometry_and_scale():
    house = {"interzone": [{"a": "office", "b": "hotties", "area_m2": 12, "u": 0.7},
                           {"a": "hotties", "b": "ted", "area_m2": 10, "u": 0.5}]}
    g = vp.interzone_conductances(house, {"ua_inter": 1.0})
    assert g[("hotties", "office")] == pytest.approx(12 * 0.7)   # sleutel alfabetisch geordend
    assert g[("hotties", "ted")] == pytest.approx(10 * 0.5)
    # De globale schaal werkt op álle vlakken tegelijk.
    g2 = vp.interzone_conductances(house, {"ua_inter": 2.0})
    assert g2[("hotties", "office")] == pytest.approx(2 * 12 * 0.7)
    # Geen lijst → geen koppeling (het model gedraagt zich exact als vóór rev 3).
    assert vp.interzone_conductances({}, {}) == {}
    # Onzin-vlakken worden stilzwijgend genegeerd i.p.v. de sim te laten crashen.
    junk = {"interzone": [{"a": "x"}, {"a": "x", "b": "x", "area_m2": 5},
                          {"a": "x", "b": "y", "area_m2": 0}]}
    assert vp.interzone_conductances(junk, {}) == {}


def test_interzone_conduction_drains_heat_between_stacked_rooms():
    """Het gestapelde-kamers-patroon: zonder vloergeleiding heeft de bovenste kamer geen
    afvoer en loopt hij weg van zijn koelere buur; mét geleiding trekken ze naar elkaar toe.
    Dit is de term die de met-de-hoogte-oplopende fout (ted +0.71 → office +2.19) verklaart."""
    house = _toy_house()
    del house["doors"]["b_hall"]                 # sluit de advectieve omweg af: puur geleiding
    tl = _const_timeline(20.0, hours=120, irr=0.0)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    params = vio.default_params(house)
    params["b"]["q_int"] = 6.0                   # kamer b loopt warm (proxy voor de zonlast)
    ctx = replace(CTX, neighbor_temp=20.0)
    loose = vp.simulate(house, params, tl, seed, ctx, calib_only_rooms=set(house["rooms"]))
    house["interzone"] = [{"a": "a", "b": "b", "area_m2": 12, "u": 0.7}]
    tight = vp.simulate(house, params, tl, seed, ctx, calib_only_rooms=set(house["rooms"]))
    # De warme kamer koelt af doordat hij nu in zijn buur kan lozen...
    assert tight["Ta"]["b"] < loose["Ta"]["b"]
    # ...en die buur warmt navenant op: energie verdwijnt niet, hij verplaatst.
    assert tight["Ta"]["a"] > loose["Ta"]["a"]


# ── 6. Dynamisch buur-anker (party-muren) ────────────────────────────────────────────

def test_neighbor_temp_estimate_winter_floor_and_summer_track():
    now = datetime(2026, 1, 15, 12, 0, tzinfo=TZ)
    winter = [{"dt": now - timedelta(hours=h), "T_out": 3.0} for h in range(72)]
    # 's Winters domineert de stookvloer.
    assert vp.neighbor_temp_estimate(winter, now) == pytest.approx(vp.NEIGHBOR_WINTER_FLOOR)
    # 's Zomers volgt het anker het 3-daags buitengemiddelde mee omhoog.
    summer = [{"dt": now - timedelta(hours=h), "T_out": 24.0} for h in range(72)]
    assert vp.neighbor_temp_estimate(summer, now) == pytest.approx(24.0)
    # Geen bruikbare historie → terugval op de module-default.
    assert vp.neighbor_temp_estimate([], now) == pytest.approx(vp.NEIGHBOR_TEMP)


def test_neighbor_night_cap_profile():
    """Dagplafond overdag, lager 's nachts, met gladde 1-uurs overgangen — een harde sprong
    zou een knik in de voorspelde temp injecteren die als residu terugkomt."""
    def cap(h, m=0):
        return vp.neighbor_night_cap(datetime(2026, 7, 15, h, m, tzinfo=TZ))
    assert cap(14) == pytest.approx(vp.NEIGHBOR_SUMMER_CAP)
    assert cap(3) == pytest.approx(vp.NEIGHBOR_NIGHT_CAP)
    assert cap(2) == pytest.approx(vp.NEIGHBOR_NIGHT_CAP)
    mid = 0.5 * (vp.NEIGHBOR_SUMMER_CAP + vp.NEIGHBOR_NIGHT_CAP)
    assert cap(22, 30) == pytest.approx(mid)      # halverwege de avondovergang
    assert cap(7, 30) == pytest.approx(mid)       # halverwege de ochtendovergang
    assert cap(21) > cap(23) and cap(8) == pytest.approx(vp.NEIGHBOR_SUMMER_CAP)
    # Robuust tegen een `when` zonder klok (mirror van internal_gain_profile).
    assert vp.neighbor_night_cap(object()) == pytest.approx(vp.NEIGHBOR_SUMMER_CAP)


def test_neighbor_at_only_ever_caps():
    """Het anker mag door de cap alleen omláág — een koud winteranker blijft ongemoeid."""
    night = datetime(2026, 7, 15, 3, 0, tzinfo=TZ)
    assert vp.neighbor_at(26.0, night) == pytest.approx(vp.NEIGHBOR_NIGHT_CAP)
    assert vp.neighbor_at(19.5, night) == pytest.approx(19.5)
    day = datetime(2026, 7, 15, 14, 0, tzinfo=TZ)
    assert vp.neighbor_at(26.0, day) == pytest.approx(vp.NEIGHBOR_SUMMER_CAP)


def test_simulate_applies_the_night_cap_to_the_party_anchor(monkeypatch):
    """Overgenomen uit tweeling 2 (held-out getoetst): met een hoog anker moet de kamer
    's nachts kóéler uitkomen dan met een anker dat de klok niet kent. Zonder cap bleef het
    3-daags-gemiddelde-anker in een hittegolf de hele nacht doorstoken."""
    house = _toy_house()
    params = vio.default_params(house)
    for rid in house["rooms"]:
        params[rid]["q_int"] = 0.0               # isoleer de party-term
    tl = _const_timeline(18.0, hours=120, irr=0.0)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 18.0 for z in zones}
    saved_cap = vp.NEIGHBOR_NIGHT_CAP
    ctx = replace(CTX, neighbor_temp=vp.NEIGHBOR_SUMMER_CAP)   # hittegolf-anker, al op het dagplafond
    monkeypatch.setattr(vp, "NEIGHBOR_NIGHT_CAP", vp.NEIGHBOR_SUMMER_CAP)  # cap uit (oud gedrag)
    no_cap = vp.simulate(house, params, tl, seed, ctx, calib_only_rooms=set(house["rooms"]))
    monkeypatch.setattr(vp, "NEIGHBOR_NIGHT_CAP", saved_cap)               # cap aan
    capped = vp.simulate(house, params, tl, seed, ctx, calib_only_rooms=set(house["rooms"]))
    # De sim eindigt om 00:00 — midden in de nachtcap, dus daar hoort het verschil te staan.
    for rid in house["rooms"]:
        assert capped["Ta"][rid] < no_cap["Ta"][rid]


def test_simulate_honours_neighbor_temp_ctx():
    # Een warmer buur-anker (via ctx.neighbor_temp) tilt een dichte, zon-loze kamer (via de
    # party-muren) hoger op.
    house = _toy_house()
    params = vio.default_params(house)
    for rid in house["rooms"]:
        params[rid]["q_int"] = 0.0          # isoleer de party-term
    tl = _const_timeline(16.0, hours=240, irr=0.0)
    seed = {z: 16.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    cool = vp.simulate(house, params, tl, seed, replace(CTX, neighbor_temp=20.0),
                       calib_only_rooms=set(house["rooms"]))
    warm = vp.simulate(house, params, tl, seed, replace(CTX, neighbor_temp=26.0),
                       calib_only_rooms=set(house["rooms"]))
    for rid in house["rooms"]:
        assert warm["Ta"][rid] > cool["Ta"][rid] + 1.0


def test_simulate_tm_seed_overrides_default_blend():
    # Zonder tm_seed start Tm op de warme blend 0.5*(Ta+ctx.neighbor_temp); met tm_seed moet
    # die per-zone beginwaarde overschreven worden. Eén korte stap (massa-tijdconstante ~uren)
    # zodat het startverschil nog grotendeels intact is in de output.
    house = _toy_house()
    params = vio.default_params(house)
    tl = _const_timeline(20.0, hours=0, irr=0.0)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    ctx = replace(CTX, neighbor_temp=20.0)   # default Tm-blend = 20.0, gelijk aan Ta
    default_sim = vp.simulate(house, params, tl, seed, ctx)
    full_tm_seed = {z: 5.0 for z in zones}
    seeded_sim = vp.simulate(house, params, tl, seed, ctx, tm_seed=full_tm_seed)
    partial_sim = vp.simulate(house, params, tl, seed, ctx, tm_seed={"a": 5.0})

    for z in zones:
        assert seeded_sim["Tm"][z] < default_sim["Tm"][z] - 10.0
    # ontbrekende zone in tm_seed valt terug op het standaardgedrag (additief, geen breuk)
    assert partial_sim["Tm"]["b"] == pytest.approx(default_sim["Tm"]["b"])
    assert partial_sim["Tm"]["a"] == pytest.approx(seeded_sim["Tm"]["a"])


# ── 7. Dak-sol-air-term (bovenste verdieping) ────────────────────────────────────────

def _roof_house(roof_m2: float) -> dict:
    return {
        "location": {"lat": 52.09, "lon": 5.12},
        "rooms": {"r": {"from_window_data": "office", "volume_m3": 30,
                        "exterior_wall_m2": 10, "roof_m2": roof_m2}},
        "junctions": {}, "windows": {}, "vents": {}, "doors": {},
    }


def _roof_timeline(T_out: float, hours: int, irr_roof: float, sun_el: float) -> list[dict]:
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    grid = []
    for i in range(hours * 4 + 1):
        t = t0 + timedelta(minutes=15 * i)
        grid.append({"t": t, "T_out": T_out, "irr": {"r": 0.0}, "irr_roof": {"r": irr_roof},
                     "sun_el": sun_el, "states": {},
                     "weather": {"wind_speed": 1.0, "wind_dir": 200.0, "gust": 2.0, "precip": 0.0,
                                 "direct": 0.0, "diffuse": 0.0, "rh": 60}, "dt": 900.0})
    return grid


def test_roof_term_warms_by_day_cools_by_night():
    params_with = vio.default_params(_roof_house(20.0))
    params_no = vio.default_params(_roof_house(0.0))
    seed = {"r": 20.0}
    # Overdag: zon op het dak → de dak-kamer warmer dan zonder dak.
    day_with = vp.simulate(_roof_house(20.0), params_with,
                           _roof_timeline(20.0, 48, 700.0, 45.0), seed, CTX, calib_only_rooms={"r"})
    day_no = vp.simulate(_roof_house(0.0), params_no,
                         _roof_timeline(20.0, 48, 700.0, 45.0), seed, CTX, calib_only_rooms={"r"})
    assert day_with["Ta"]["r"] > day_no["Ta"]["r"] + 0.3
    # 's Nachts: hemel-stralingskoeling → de dak-kamer kouder dan zonder dak.
    night_with = vp.simulate(_roof_house(20.0), params_with,
                             _roof_timeline(20.0, 48, 0.0, -5.0), seed, CTX, calib_only_rooms={"r"})
    night_no = vp.simulate(_roof_house(0.0), params_no,
                           _roof_timeline(20.0, 48, 0.0, -5.0), seed, CTX, calib_only_rooms={"r"})
    assert night_with["Ta"]["r"] < night_no["Ta"]["r"] - 0.1


def test_roofless_room_unchanged_by_roof_term():
    # Een kamer zónder roof_m2 (UA_roof basis 0) is identiek aan het oude gedrag, ongeacht
    # of er een irr_roof in de stap staat.
    house = _roof_house(0.0)
    params = vio.default_params(house)
    seed = {"r": 18.0}
    a = vp.simulate(house, params, _roof_timeline(24.0, 12, 800.0, 50.0), seed, CTX,
                    calib_only_rooms={"r"})
    b = vp.simulate(house, params, _roof_timeline(24.0, 12, 0.0, 50.0), seed, CTX,
                    calib_only_rooms={"r"})
    assert a["Ta"]["r"] == pytest.approx(b["Ta"]["r"], abs=1e-12)


# ── 8. f_air (zon-split lucht/massa) ─────────────────────────────────────────────────

def test_f_air_splits_solar_air_vs_mass():
    # Meer zon naar de luchtknoop (hoger f_air) → de luchtknoop reageert op korte termijn
    # sterker op een zonpuls dan met een lage f_air (die de zon vooral in de trage massa stopt).
    house = {
        "location": {"lat": 52.09, "lon": 5.12},
        "rooms": {"r": {"volume_m3": 30, "exterior_wall_m2": 10}},
        "junctions": {}, "windows": {}, "vents": {}, "doors": {},
    }
    t0 = datetime(2026, 6, 15, 12, 0, tzinfo=TZ)
    tl = [{"t": t0 + timedelta(minutes=15 * i), "T_out": 18.0, "irr": {"r": 1500.0},
           "states": {}, "weather": {"wind_speed": 0.5, "wind_dir": 200.0, "gust": 1.0,
                                     "precip": 0.0, "direct": 0.0, "diffuse": 0.0, "rh": 50},
           "dt": 900.0} for i in range(9)]   # ~2u zonpuls
    seed = {"r": 18.0}
    lo = vio.default_params(house)
    lo["r"]["f_air"] = 0.2
    hi = vio.default_params(house)
    hi["r"]["f_air"] = 0.8
    sim_lo = vp.simulate(house, lo, tl, seed, CTX, calib_only_rooms={"r"})
    sim_hi = vp.simulate(house, hi, tl, seed, CTX, calib_only_rooms={"r"})
    assert sim_hi["Ta"]["r"] > sim_lo["Ta"]["r"] + 0.2


# ── Observability: solver-failures ───────────────────────────────────────────────────

def test_simulate_flags_solver_failure(monkeypatch):
    # Een bijna-singulier thermisch stelsel bevriest de substap stil op de laatste goede
    # waarde — dat mág, maar moet geteld worden (learned.solver_failures) i.p.v. geruisloos.
    house = _toy_house()
    params = vio.default_params(house)
    tl = _const_timeline(20.0, hours=2, irr=0.0)
    seed = {z: 22.0 for z in list(house["rooms"]) + list(house["junctions"])}
    ok = vp.simulate(house, params, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    assert ok["solver_failures"] == 0                        # normaal: geen enkele
    monkeypatch.setattr(vp, "solve_linear", lambda A, b: None)
    sim = vp.simulate(house, params, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    assert sim["solver_failures"] > 0
    for rid in house["rooms"]:                               # bevroren op de seed, niet NaN
        assert sim["Ta"][rid] == pytest.approx(22.0)


# ════════════════════════════════════════════════════════════════════════════════════
#  Zonnige-dag-nauwkeurigheid (WU-zon-herschaling stap 2: hoek-transmissie)
# ════════════════════════════════════════════════════════════════════════════════════

def test_beam_iam_factor_grazing_dropoff():
    # Loodrechte inval (cos=1) → factor 1 (default 0.7-transmissie ongewijzigd). Scherende hoek
    # → < 1. cos ≤ 0 (zon achter het vlak) → 0. Monotoon dalend naar de horizon.
    assert vp.beam_iam_factor(1.0) == pytest.approx(1.0)
    assert vp.beam_iam_factor(0.0) == 0.0
    assert vp.beam_iam_factor(-0.3) == 0.0
    import math as _m
    f60 = vp.beam_iam_factor(_m.cos(_m.radians(60)))   # 1/cos=2 → 1 − b0
    assert f60 == pytest.approx(1.0 - vp.GLASS_IAM_B0, abs=1e-9)
    f75 = vp.beam_iam_factor(_m.cos(_m.radians(75)))
    assert 0.0 <= f75 < f60 < 1.0


def test_facade_irradiance_beam_iam_only_touches_beam():
    # beam_iam dempt enkel de directe component; op een scherende hoek < de default, en nooit
    # de diffuse view-factor. Default (vlag uit) blijft byte-identiek.
    az = 309.0
    plain = vp.facade_irradiance(az, az + 60.0, 15.0, 700.0, 120.0, 90.0, False, 0.0)
    iam = vp.facade_irradiance(az, az + 60.0, 15.0, 700.0, 120.0, 90.0, False, 0.0, True)
    assert iam < plain                                   # scherende avondzon → minder transmissie
    # Alleen-diffuus (zon onder de horizon): beam-vlag verandert niets (geen beam).
    night_plain = vp.facade_irradiance(az, az, -5.0, 0.0, 80.0, 90.0, False, 0.0)
    night_iam = vp.facade_irradiance(az, az, -5.0, 0.0, 80.0, 90.0, False, 0.0, True)
    assert night_iam == pytest.approx(night_plain, abs=1e-12)


# ════════════════════════════════════════════════════════════════════════════════════
#  Trappenhuis-stratificatie
# ════════════════════════════════════════════════════════════════════════════════════

def test_stair_gradient_slope_and_bounds():
    # γ = kleinste-kwadraten-helling van temp t.o.v. hoogte door de kamer-punten. Warm boven →
    # positieve helling; inversie (top koeler) → 0; <2 hoogtes → 0; steile helling → geklemd.
    assert vp.stair_gradient([(1.0, 22.0), (7.0, 24.4)]) == pytest.approx(0.4)   # 2.4°C / 6m
    assert vp.stair_gradient([(1.0, 24.0), (7.0, 22.0)]) == 0.0                  # inversie
    assert vp.stair_gradient([(3.0, 23.0)]) == 0.0                              # 1 punt
    assert vp.stair_gradient([]) == 0.0
    assert vp.stair_gradient([(2.0, 20.0), (2.0, 25.0)]) == 0.0                 # zelfde hoogte
    assert vp.stair_gradient([(1.0, 20.0), (7.0, 100.0)]) == vp.STAIR_STRAT_MAX_GRAD  # geklemd
    # Drie punten: helling via kleinste kwadraten (hier exact lineair → 0.5 °C/m).
    assert vp.stair_gradient([(1.0, 22.0), (4.0, 23.5), (7.0, 25.0)]) == pytest.approx(0.5)


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
    info = vp._stratify_zones(_strat_house())
    assert set(info) == {"shaft"}
    z = info["shaft"]
    assert z["doors"] == {"bot": 1.0, "top": 7.0}
    assert z["z_mean"] == pytest.approx(4.0)             # (1 + 7) / 2
    assert z["z_lo"] == 1.0 and z["z_hi"] == 7.0
    # Zonder de vlag → afwezig (default ongewijzigd).
    house_off = _strat_house()
    house_off["rooms"]["shaft"].pop("stratify")
    assert vp._stratify_zones(house_off) == {}


def test_stair_gamma_room_slope_and_door_filter():
    info = vp._stratify_zones(_strat_house())["shaft"]
    temps = {"top": 25.0, "bot": 22.0}                   # (7m,25) (1m,22) → 3°C / 6m = 0.5 (< klem)
    # Alle deuren open → helling door beide kamers.
    assert vp._stair_gamma(info, temps) == pytest.approx(vp.stair_gradient([(1.0, 22.0), (7.0, 25.0)]))
    assert vp._stair_gamma(info, temps) == pytest.approx(3.0 / 6.0)
    # Eén deur dicht → <2 open kamers → vlak (die kamer is ontkoppeld, geen proxy).
    assert vp._stair_gamma(info, temps, open_others={"top"}) == 0.0
    # Ontbrekende kamertemp valt eveneens uit de regressie.
    assert vp._stair_gamma(info, {"top": 25.0}) == 0.0


def test_measured_at_returns_none_outside_the_series():
    """Anders dan `_interp` (dat vlak extrapoleert) moet buiten het meetvenster expliciet
    'geen meting' terugkomen — anders zou het koker-profiel bevriezen op de laatste meting
    zodra de tado-historie ophoudt, inclusief het hele voorspel-venster."""
    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=TZ)
    ser = [(t0, 20.0), (t0 + timedelta(hours=2), 24.0)]
    assert vp._measured_at(ser, t0 + timedelta(hours=1)) == pytest.approx(22.0)
    assert vp._measured_at(ser, t0) == pytest.approx(20.0)
    assert vp._measured_at(ser, t0 - timedelta(minutes=1)) is None
    assert vp._measured_at(ser, t0 + timedelta(hours=3)) is None
    assert vp._measured_at([], t0) is None


def test_gamma_temps_prefers_measurement_and_falls_back():
    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=TZ)
    Ta = {"top": 30.0, "bot": 30.0, "shaft": 30.0}
    measured = {"top": [(t0, 25.0), (t0 + timedelta(hours=2), 25.0)]}
    got = vp._gamma_temps(measured, Ta, t0 + timedelta(hours=1))
    assert got["top"] == pytest.approx(25.0)      # meting wint
    assert got["bot"] == pytest.approx(30.0)      # geen meting → gesimuleerd
    # Buiten het meetvenster valt álles terug op de simulatie.
    assert vp._gamma_temps(measured, Ta, t0 + timedelta(hours=5))["top"] == pytest.approx(30.0)
    # Geen metingen → exact het oude gedrag (dezelfde dict).
    assert vp._gamma_temps(None, Ta, t0) is Ta


def test_gamma_no_longer_feeds_back_on_its_own_prediction():
    """De kern van de koker-fix. γ werd uit de GESIMULEERDE temps berekend, ín de stap-lus:
    een te warme voorspelling voor de bovenste kamer gaf een steilere γ, en de bronterm
    duwde daardoor nóg meer warmte in juist die kamer — een lus die op zonnige middagen
    verzadigde. Met de meting als regressiebasis mag een oplopende voorspelling de gradiënt
    niet meer opdrijven."""
    house = _strat_house()
    params = vio.default_params(house)
    params["top"]["q_int"] = 8.0            # laat de bovenste kamer weglopen (proxy zonlast)
    tl = _strat_timeline({"top": 0.0, "bot": 0.0, "shaft": 0.0}, hours=48)
    zones = list(house["rooms"])
    seed = {z: 22.0 for z in zones}
    ctx = replace(CTX, neighbor_temp=20.0)
    loop = vp.simulate(house, params, tl, seed, ctx, calib_only_rooms=set(house["rooms"]))
    # Metingen die zeggen: in werkelijkheid is er nauwelijks een verticale gradiënt.
    flat = {rid: [(s["t"], 22.0) for s in tl] for rid in ("top", "bot")}
    pinned = vp.simulate(house, params, tl, seed, ctx, calib_only_rooms=set(house["rooms"]),
                         measured=flat)
    # Zonder metingen versterkt de lus de warme kamer; met een vlakke gemeten gradiënt niet.
    assert pinned["Ta"]["top"] < loop["Ta"]["top"]


def _strat_timeline(irr_by_room: dict, hours: int = 24, states: dict | None = None) -> list[dict]:
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    tl = []
    for i in range(hours * 4 + 1):
        t = t0 + timedelta(minutes=15 * i)
        tl.append({"t": t, "T_out": 20.0, "irr": dict(irr_by_room), "states": dict(states or {}),
                   "weather": {"wind_speed": 3.0, "wind_dir": 200.0, "gust": 5.0,
                   "precip": 0.0, "direct": 0.0, "diffuse": 0.0, "rh": 55}, "dt": 900.0})
    return tl


def test_stratification_shifts_floor_coupling(monkeypatch):
    # Zon alleen op de bovenkamer → top warmer dan onder → verticale spreiding in de koker.
    # Met stratificatie mengt de bovendeur tegen de warmere koker-top (→ bovenkamer blijft warmer)
    # en de onderdeur tegen de koelere koker-onder (→ onderkamer koeler). Zonder de vlag identiek.
    # BUOY_EXCH_C tijdelijk op 0: dit test het γ-offset-mechanisme geïsoleerd van de counterflow.
    house = _strat_house()
    params = vio.default_params(house)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    tl = _strat_timeline({"top": 500.0, "bot": 0.0, "shaft": 0.0})
    seed = {z: 20.0 for z in zones}
    monkeypatch.setattr(vp, "BUOY_EXCH_C", 0.0)
    on = vp.simulate(house, params, tl, seed, CTX, calib_only_rooms={"top", "bot"})
    house_off = _strat_house()
    house_off["rooms"]["shaft"].pop("stratify")
    off = vp.simulate(house_off, params, tl, seed, CTX, calib_only_rooms={"top", "bot"})
    assert on["Ta"]["top"] > off["Ta"]["top"] + 1e-3
    assert on["Ta"]["bot"] < off["Ta"]["bot"] - 1e-3
    # Het koker-gemiddelde blijft ~behouden (energie-behoudende symmetrische bron, geen netto bron).
    assert on["Ta"]["shaft"] == pytest.approx(off["Ta"]["shaft"], abs=0.3)


def test_buoyant_door_exchange_basics():
    # Dichte deur (area 0) of gelijke temps → 0. Groeit met ΔT (√-wet) en met de deuroppervlakte.
    assert vp.buoyant_door_exchange(0.0, 26.0, 22.0) == 0.0
    assert vp.buoyant_door_exchange(1.8, 24.0, 24.0) == 0.0
    q2 = vp.buoyant_door_exchange(1.8, 25.0, 23.0)       # ΔT 2, T̄ 24
    q8 = vp.buoyant_door_exchange(1.8, 28.0, 20.0)       # ΔT 8, zelfde T̄ 24
    assert q2 > 0.0
    assert q8 == pytest.approx(q2 * 2.0, rel=1e-6)       # ΔT 8 vs 2 → √4 = 2×
    assert vp.buoyant_door_exchange(3.6, 25.0, 23.0) == pytest.approx(2.0 * q2, rel=1e-6)
    # Grootte-orde: ~2°C over een 1.8 m² deur → zo'n 0.05–0.15 m³/s (honderden m³/h) — de
    # menging die het netto-netwerkdebiet mist.
    assert 0.05 < q2 < 0.15


def test_counterflow_pins_shaft_to_open_door_rooms():
    # Zon op de koker zelf. Deuren OPEN → de counterflow mengt de koker naar de kamers (gat
    # klein). Deuren DICHT → geen uitwisseling → de warmte poolt in de koker (gat groot) —
    # "office-deur dicht → de warmte gaat daarheen".
    house = _strat_house()
    params = vio.default_params(house)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    irr = {"top": 0.0, "bot": 0.0, "shaft": 400.0}
    open_tl = _strat_timeline(irr)
    closed_tl = _strat_timeline(irr, states={"top_shaft": "dicht", "bot_shaft": "dicht"})
    op = vp.simulate(house, params, open_tl, seed, CTX, calib_only_rooms={"top", "bot"})
    cl = vp.simulate(house, params, closed_tl, seed, CTX, calib_only_rooms={"top", "bot"})
    gap_open = op["Ta"]["shaft"] - max(op["Ta"]["top"], op["Ta"]["bot"])
    gap_closed = cl["Ta"]["shaft"] - max(cl["Ta"]["top"], cl["Ta"]["bot"])
    assert gap_closed > gap_open + 1.0     # dicht → warmte poolt; open → weggemengd
    assert gap_open < 4.0                  # open deur → geen groot zwevend gat meer


def test_stair_crown_solar_display():
    # 's Avonds (irr 0) → 0, zodat de avond-inconsistentie niet terug kan komen; middagzon →
    # een paar graden, geklemd op de max.
    assert vp.stair_crown(0.0) == 0.0
    assert vp.stair_crown(None) == 0.0
    mid = vp.stair_crown(500.0)
    assert mid == pytest.approx(vp.STAIR_CROWN_K * 500.0)
    assert 1.0 < mid < vp.STAIR_CROWN_MAX
    assert vp.stair_crown(1e6) == vp.STAIR_CROWN_MAX


def test_stair_gamma_steeper_when_top_hotter():
    # De kamers zíjn de proxy-meting: hoe warmer de bovenkamer t.o.v. de onderkamer, hoe steiler γ
    # (de zon zit al ín de kamertemp — geen aparte zon-constante meer nodig).
    info = vp._stratify_zones(_strat_house())["shaft"]
    mild = vp._stair_gamma(info, {"top": 23.0, "bot": 22.0})
    hot = vp._stair_gamma(info, {"top": 26.0, "bot": 22.0})
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
    p_hi = vio.default_params(house)
    p_hi["vent_eff"] = 1.0
    p_lo = vio.default_params(house)
    p_lo["vent_eff"] = 0.1
    hi = vp.simulate(house, p_hi, tl, seed, CTX, calib_only_rooms={"top", "bot"})
    lo = vp.simulate(house, p_lo, tl, seed, CTX, calib_only_rooms={"top", "bot"})
    gap_hi = hi["Ta"]["shaft"] - max(hi["Ta"]["top"], hi["Ta"]["bot"])
    gap_lo = lo["Ta"]["shaft"] - max(lo["Ta"]["top"], lo["Ta"]["bot"])
    # Ook met vent_eff op zijn ondergrens blijft de koker aan de open-deur-kamers gepind …
    assert gap_lo < 4.0
    # … en de pinning-sterkte hangt er nauwelijks van af (vóór de fix schaalde het gat ~×10 mee).
    assert abs(gap_lo - gap_hi) < 1.0


# ── 9. per_window_solar + zonwering + open-fractie ───────────────────────────────────

def test_shade_factor_override():
    w = {"shading": "none", "shade": {"factor": 0.15}}
    assert vp._shade_factor("sky", w, {}) == 1.0                       # geen melding → static none
    assert vp._shade_factor("sky", w, {"sky_shade": "dicht"}) == 0.15  # dicht → scherm-factor
    assert vp._shade_factor("sky", w, {"sky_shade": "open"}) == 1.0    # open overschrijft
    assert vp._shade_factor("sky", w, {"sky_shade": "half"}) == pytest.approx(0.5 * (1 + 0.15))
    # Raam zonder bedienbare zonwering valt terug op de statische `shading`.
    assert vp._shade_factor("x", {"shading": "lamella"}, {}) == vp.SHADING_FACTOR["lamella"]


def test_open_frac_mapping():
    elem = {"tilt_frac": 0.2}
    assert vp._open_frac("open", elem) == 1.0
    assert vp._open_frac("dicht", elem) == 0.0
    assert vp._open_frac("tilt", elem) == 0.2
    assert vp._open_frac(0.5, elem) == 0.5


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


def test_per_window_solar_matches_room_irr():
    # De per-kamer irr in build_timeline is exact het substap-gemiddelde van de
    # per-raam-sommen — de extractie veranderde de boekhouding niet.
    house = _pw_house()
    base = datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc)
    now = base + timedelta(hours=2)
    grid = vio.build_timeline(house, {"hourly": _pw_rows(base)}, [], now, 0.5, CTX)
    step = next(s for s in grid if s["t"] == now)
    expected = 0.0
    for j in range(vp.SOLAR_SUBSTEPS):
        ts = now + timedelta(hours=0.25 * (j + 0.5) / vp.SOLAR_SUBSTEPS)
        s_az, s_el = vp.sun_position(CTX.lat, CTX.lon, ts.astimezone(timezone.utc))
        pw = vp.per_window_solar(house, {}, s_az, s_el, 600.0, 100.0)
        expected += (pw["w1"] + pw["w2"]) / vp.SOLAR_SUBSTEPS
    assert step["irr"]["r"] == pytest.approx(expected, abs=1e-9)


def test_per_window_solar_respects_shade_state():
    # Een dicht gemelde bedienbare zonwering schaalt dat raam met z'n factor
    # (× de statische shading); het andere raam blijft ongemoeid.
    house = _pw_house()
    open_pw = vp.per_window_solar(house, {}, 129.0, 40.0, 600.0, 100.0)
    closed_pw = vp.per_window_solar(house, {"w2_shade": "dicht"}, 129.0, 40.0,
                                    600.0, 100.0)
    assert closed_pw["w2"] == pytest.approx(open_pw["w2"] * 0.12, rel=1e-9)
    assert closed_pw["w1"] == pytest.approx(open_pw["w1"], abs=1e-12)
    # Statische lamella (0.9) zit er in beide standen overheen.
    bare = dict(house["windows"]["w2"])
    bare.pop("shade")
    bare.pop("shading")
    house_bare = {"rooms": {"r": {}}, "windows": {"w2": bare}}
    bare_pw = vp.per_window_solar(house_bare, {}, 129.0, 40.0, 600.0, 100.0)
    assert open_pw["w2"] == pytest.approx(bare_pw["w2"] * 0.9, rel=1e-9)


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
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    rooms = ["a", "b"]
    grid = []
    for i in range(hours * 4 + 1):
        t = t0 + timedelta(minutes=15 * i)
        grid.append({"t": t, "T_out": T_out, "irr": {r: irr for r in rooms}, "states": {},
                     "weather": {"wind_speed": 1.0, "wind_dir": 200.0, "gust": 2.0, "precip": 0.0,
                                 "direct": 0.0, "diffuse": 0.0, "rh": 60}, "dt": 900.0})
    return grid


def _node_residual(zones, ops, pressures, zt, T_out):
    """Netto massadebiet per interne knoop, gegeven opgeloste drukken (voor de balanscheck)."""
    idx = {z: i for i, z in enumerate(zones)}
    P = [pressures[z] for z in zones]
    rho_out = vp.air_density(T_out)
    rho_z = {z: vp.air_density(zt.get(z, T_out)) for z in zones}
    res = [0.0] * len(zones)
    for op in ops:
        ia = idx[op["a"]]
        z = op["z"]
        ra = rho_z[op["a"]]
        Pa = P[ia] - ra * vp.G * z
        if op["b"] == "outside":
            Pb = op["Pe"] - rho_out * vp.G * z
            rb = rho_out
        else:
            rb = rho_z[op["b"]]
            Pb = P[idx[op["b"]]] - rb * vp.G * z
        md = vp._massflow(Pa - Pb, op["Cd"], op["area"], ra, rb)
        res[ia] += md
        if op["b"] != "outside":
            res[idx[op["b"]]] -= md
    return res

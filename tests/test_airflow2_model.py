"""Regressie-vangnet voor Ventilatie 2 (`airflow2_model.py`, Project 12).

Pure-functie-checks — géén netwerk, géén Gist, géén echte artefacten:
  1. Swami–Chandra-Cp: teken/symmetrie; voor/achter-beschutting per element.
  2. Psychrometrie: w↔RH round-trip + klemmen.
  3. simulate2 (3-knoops RC + vocht): relaxatie, knoop-tijdconstante-ordening
     (lucht < snel < diep), koelen door een open raam, vocht-tracer-convergentie,
     koker-subzones (volumegemiddelde, zon-bovenin, geen-subzones = één-knoops).
  4. calibrate2 herstelt de richting van een bekende parameter-verstoring; het
     RH-kanaal telt mee.
  5. Batch-anker: adoptie/blend, physics2-rev-poort, merged_params2-migratie.
  6. Historie-shards: append-idempotentie, round-trip via load_dataset,
     openingen-dedupe; batch_windows-geometrie.
  7. De am-module-globalen (rebind-afhankelijkheid): simulate2 leest
     am._NEIGHBOR_TEMP — het makkelijk-te-vergeten koppelpunt (night_forecast-les).
"""
import json
import math
from datetime import datetime, timedelta

import pytest

import airflow_model as am
import airflow2_model as a2


# ── Fixtures ─────────────────────────────────────────────────────────────────────────

def _toy_house() -> dict:
    return {
        "location": {"lat": 52.09, "lon": 5.12},
        "front_azimuth_deg": 309,
        "rooms": {
            "a": {"from_window_data": "Living room", "volume_m3": 50, "exterior_wall_m2": 16},
            "b": {"from_window_data": "office", "volume_m3": 30, "exterior_wall_m2": 10},
        },
        "junctions": {"hall": {"volume_m3": 12}},
        "windows": {
            "a_win": {"room": "a", "facade_azimuth_deg": 129, "area_m2": 1.5, "glass_m2": 1.2,
                      "max_open_area_m2": 0.6, "tilt_frac": 0.15, "center_height_m": 1.5},
            "b_win": {"room": "b", "facade_azimuth_deg": 309, "area_m2": 1.2, "glass_m2": 1.0,
                      "max_open_area_m2": 0.5, "tilt_frac": 0.15, "center_height_m": 1.5},
        },
        "vents": {},
        "doors": {
            "a_hall": {"between": ["a", "hall"], "area_m2": 1.8, "center_height_m": 1.0,
                       "default_state": "open"},
            "b_hall": {"between": ["b", "hall"], "area_m2": 1.6, "center_height_m": 1.0,
                       "default_state": "open"},
        },
    }


def _stair_house() -> dict:
    """Toy-huis + een koker-zone 's' met drie verdieping-subzones en twee deuren
    (onder vanuit a, boven vanuit b)."""
    house = _toy_house()
    house["rooms"]["s"] = {
        "volume_m3": 26, "exterior_wall_m2": 6,
        "subzones": [{"id": "bg", "z_lo": 0.0, "z_hi": 3.0, "volume_frac": 0.34},
                     {"id": "1e", "z_lo": 3.0, "z_hi": 6.0, "volume_frac": 0.33},
                     {"id": "2e", "z_lo": 6.0, "z_hi": 9.0, "volume_frac": 0.33}],
    }
    house["doors"]["a_s"] = {"between": ["a", "s"], "area_m2": 1.8, "center_height_m": 1.0,
                             "default_state": "open", "subzone": "bg"}
    house["doors"]["b_s"] = {"between": ["b", "s"], "area_m2": 1.8, "center_height_m": 7.0,
                             "default_state": "open", "subzone": "2e"}
    return house


def _tl(T_out: float, hours: float, irr: dict | None = None, states: dict | None = None,
        rh: float = 60.0, wind: float = 1.0) -> list[dict]:
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=am.TZ)
    grid = []
    for i in range(int(hours * 4) + 1):
        t = t0 + timedelta(minutes=15 * i)
        grid.append({"t": t, "T_out": T_out, "irr": dict(irr or {}), "irr_roof": {},
                     "states": dict(states or {}),
                     "weather": {"wind_speed": wind, "wind_dir": 200.0, "gust": 2.0,
                                 "precip": 0.0, "direct": 0.0, "diffuse": 0.0, "rh": rh},
                     "dt": 900.0, "sun_az": 180.0, "sun_el": -10.0})
    return grid


def _no_house_terms(params, house):
    """Zet de tussenwoning-warmtebronnen uit voor pure relaxatie-tests."""
    for rid in house["rooms"]:
        params[rid]["ua_party"] = 0.0
        params[rid]["q_int"] = 0.0
    return params


# ── 1. Swami–Chandra-Cp + beschutting ────────────────────────────────────────────────

def test_cp_sc_signs():
    assert a2.cp_swami_chandra(0) > 0.5       # loef: overdruk
    assert a2.cp_swami_chandra(90) < 0.0      # zijgevel: zuiging
    assert a2.cp_swami_chandra(180) < 0.0     # lij: zuiging
    assert a2.cp_swami_chandra(0) > abs(a2.cp_swami_chandra(180))  # loeflob domineert


def test_cp_sc_symmetry_and_continuity():
    for theta in (10, 45, 70, 120, 170):
        assert a2.cp_swami_chandra(theta) == pytest.approx(a2.cp_swami_chandra(-theta), abs=1e-9)
        assert a2.cp_swami_chandra(theta) == pytest.approx(a2.cp_swami_chandra(360 - theta), abs=1e-9)


def test_cp_tilted2_roof_suction():
    # Plat dak houdt tweeling 1's alom-zuiging; verticaal = de S&C-muurcurve.
    assert a2.cp_tilted2(0, 0.0) < 0.0
    assert a2.cp_tilted2(0, 90.0) == pytest.approx(a2.cp_swami_chandra(0), abs=1e-12)


def test_element_exposure_default_and_override():
    assert a2.element_exposure({"facade_azimuth_deg": 309}, 309) == "front"
    assert a2.element_exposure({"facade_azimuth_deg": 129}, 309) == "back"
    assert a2.element_exposure({"facade_azimuth_deg": 129, "exposure": "front"}, 309) == "front"


def test_build_openings2_two_shelters():
    # De achtergevel-beschutting raakt alléén de achtergevel-elementen (en andersom).
    house = _toy_house()
    params = a2.default_params2(house)
    params["cp_shelter_front"] = 1.0
    params["cp_shelter_back"] = 0.5
    zt = {"a": 22.0, "b": 22.0, "hall": 22.0}
    wx = {"wind_speed": 6.0, "wind_dir": 309.0}
    ops = {op["id"]: op for op in a2.build_openings2(
        house, {"a_win": "open", "b_win": "open"}, wx, params, zt, 20.0)}
    pe_front = ops["b_win"]["Pe"]          # gevel 309 = front
    pe_back = ops["a_win"]["Pe"]           # gevel 129 = back
    params["cp_shelter_back"] = 1.0        # verdubbel de achter-beschutting
    ops2 = {op["id"]: op for op in a2.build_openings2(
        house, {"a_win": "open", "b_win": "open"}, wx, params, zt, 20.0)}
    assert ops2["a_win"]["Pe"] == pytest.approx(2.0 * pe_back, rel=1e-9)
    assert ops2["b_win"]["Pe"] == pytest.approx(pe_front, rel=1e-9)   # front onaangeroerd


# ── 2. Psychrometrie ─────────────────────────────────────────────────────────────────

def test_w_rh_roundtrip():
    for rh, t in ((30.0, 15.0), (55.0, 21.0), (85.0, 28.0)):
        w = a2.w_from_rh(rh, t)
        assert a2.rh_from_w(w, t) == pytest.approx(rh, abs=0.01)


def test_rh_from_w_clamps_and_none():
    assert a2.rh_from_w(0.5, 20.0) == 100.0     # oververzadigd → klem
    assert a2.rh_from_w(None, 20.0) is None
    assert a2.w_from_rh(None, 20.0) is None
    assert a2.w_from_rh(50.0, None) is None


def test_w_from_rh_warmer_air_holds_more():
    # Zelfde RH bij hogere temp = meer absolute vocht.
    assert a2.w_from_rh(60.0, 28.0) > a2.w_from_rh(60.0, 16.0)


# ── 3. simulate2 — 3-knoops RC + vocht ───────────────────────────────────────────────

def test_sim2_relaxes_to_outside():
    house = _toy_house()
    params = _no_house_terms(a2.default_params2(house), house)
    T_out = 18.0
    tl = _tl(T_out, hours=240)
    seed = {z: 26.0 for z in list(house["rooms"]) + list(house["junctions"])}
    sim = a2.simulate2(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    for rid in house["rooms"]:
        assert abs(sim["series"][rid][-1][1] - T_out) < 1.0


def test_sim2_node_timescale_ordering():
    # Na een stap in de buitentemp beweegt de luchtknoop het snelst, de snelle massa
    # daarna, de diepe massa het traagst — de kern van het 3-knoops ontwerp.
    house = _toy_house()
    params = _no_house_terms(a2.default_params2(house), house)
    tl = _tl(10.0, hours=6)
    seed = {z: 26.0 for z in list(house["rooms"]) + list(house["junctions"])}
    sim = a2.simulate2(house, params, tl, seed, tm_seed={z: 26.0 for z in house["rooms"]})
    # tm_seed zet de diepe knoop; snelle knoop start op seed (26).
    drop_air = 26.0 - sim["Ta"]["a"]
    drop_fast = 26.0 - sim["Tf"]["a"]
    drop_deep = 26.0 - sim["Td"]["a"]
    assert drop_air > drop_fast > drop_deep > 0.0


def test_sim2_open_windows_cool_faster():
    # Cross-ventilatie (beide gevels open): één enkel raam in een verder dichte schil
    # is massabalans-gelimiteerd (nauwelijks nétto flow — correcte orifice-fysica),
    # dus de vergelijking opent er twee.
    house = _toy_house()
    params = _no_house_terms(a2.default_params2(house), house)
    seed = {z: 27.0 for z in list(house["rooms"]) + list(house["junctions"])}
    tl_closed = _tl(15.0, hours=6, wind=3.0)
    tl_open = _tl(15.0, hours=6, states={"a_win": "open", "b_win": "open"}, wind=3.0)
    t_closed = a2.simulate2(house, params, tl_closed, seed)["series"]["a"][-1][1]
    t_open = a2.simulate2(house, params, tl_open, seed)["series"]["a"][-1][1]
    assert t_open < t_closed - 0.5


def test_sim2_moisture_converges_to_outside():
    # Raam open, stevige wind, geen interne vochtproductie → de kamer-w kruipt naar de
    # buiten-w; de RH-uitvoer blijft binnen [0, 100].
    house = _toy_house()
    params = _no_house_terms(a2.default_params2(house), house)
    params["q_moist"] = 0.0
    tl = _tl(18.0, hours=48, states={"a_win": "open", "b_win": "open"}, rh=60.0, wind=4.0)
    seed = {z: 18.0 for z in list(house["rooms"]) + list(house["junctions"])}
    w_hi = {z: 0.014 for z in seed}     # start muf (~100% RH bij 18°C), buffer ook vol
    sim = a2.simulate2(house, params, tl, seed, seed_w=w_hi)
    w_out = a2.w_from_rh(60.0, 18.0)
    # De EMPD-buffer draint met τ ≈ BUF_CAP_FACTOR·BUF_TAU_H (~30u), dus na 48u hoort
    # ≥70% van het vochtoverschot weggespoeld te zijn — niet 100%.
    assert sim["W"]["a"] - w_out < 0.30 * (0.014 - w_out)
    assert sim["W"]["a"] < 0.014
    for _, rh in sim["series_rh"]["a"]:
        assert 0.0 <= rh <= 100.0


def test_sim2_moisture_source_raises_rh():
    # Interne vochtproductie aan vs uit: mét bron eindigt de kamer natter.
    house = _toy_house()
    params = _no_house_terms(a2.default_params2(house), house)
    tl = _tl(18.0, hours=24)
    seed = {z: 18.0 for z in list(house["rooms"]) + list(house["junctions"])}
    params["q_moist"] = 0.0
    w_dry = a2.simulate2(house, params, tl, seed)["W"]["a"]
    params["q_moist"] = 2.0
    w_wet = a2.simulate2(house, params, tl, seed)["W"]["a"]
    assert w_wet > w_dry


def test_sim2_no_subzones_reports_no_sub_now():
    house = _toy_house()
    sim = a2.simulate2(house, a2.default_params2(house), _tl(18.0, hours=2),
                       {z: 20.0 for z in list(house["rooms"]) + list(house["junctions"])})
    assert sim["sub_now"] == {}


def test_sim2_subzone_volume_mean():
    # De gerapporteerde kokertemp is het volumegemiddelde van de sub-knopen.
    house = _stair_house()
    params = a2.default_params2(house)
    tl = _tl(18.0, hours=12, irr={"s": 250.0})
    zones = list(house["rooms"]) + list(house["junctions"])
    sim = a2.simulate2(house, params, tl, {z: 20.0 for z in zones}, snapshot_t=tl[-1]["t"])
    subs = sim["sub_now"]["s"]
    fracs = {s["id"]: s["frac"] for s in a2.subzone_meta(house)["s"]["subs"]}
    mean = sum(subs[sid] * fracs[sid] for sid in subs)
    assert sim["Ta_now"]["s"] == pytest.approx(mean, abs=0.05)


def test_sim2_subzone_solar_heats_top():
    # Koker-zon (skylight) landt op de bovenste sub-knoop; stabiele gelaagdheid mengt
    # maar zwak → boven warmer dan onder. Deuren dicht zodat de kamers niet bijmengen.
    house = _stair_house()
    params = _no_house_terms(a2.default_params2(house), house)
    states = {"a_s": "dicht", "b_s": "dicht", "a_hall": "dicht", "b_hall": "dicht"}
    tl = _tl(18.0, hours=8, irr={"s": 300.0}, states=states)
    zones = list(house["rooms"]) + list(house["junctions"])
    sim = a2.simulate2(house, params, tl, {z: 18.0 for z in zones})
    subs = sim["sub_now"]["s"]
    assert subs["2e"] > subs["1e"] >= subs["bg"]


def test_sim2_subzone_unstable_mixes_up():
    # Warme ónderlaag (instabiel) mengt omhoog: het boven-onder-verschil krimpt duidelijk
    # sneller dan bij de stabiele (boven-warm) spiegelconfiguratie.
    house = _stair_house()
    meta = a2.subzone_meta(house)["s"]
    q_unstable = a2.vertical_exchange(meta["area_h"], 26.0, 20.0, 3.0, 1.0)
    q_stable = a2.vertical_exchange(meta["area_h"], 20.0, 26.0, 3.0, 1.0)
    assert q_unstable > 3.0 * q_stable


def test_sim2_reads_neighbor_global():
    # simulate2 leest am._NEIGHBOR_TEMP (het rebind-koppelpunt): een warme buur trekt
    # een kamer met party-muren omhoog t.o.v. een koude buur.
    house = _toy_house()
    params = a2.default_params2(house)
    tl = _tl(10.0, hours=48)
    seed = {z: 15.0 for z in list(house["rooms"]) + list(house["junctions"])}
    old = am._NEIGHBOR_TEMP
    try:
        am._NEIGHBOR_TEMP = 24.0
        t_warm = a2.simulate2(house, params, tl, seed)["series"]["a"][-1][1]
        am._NEIGHBOR_TEMP = 14.0
        t_cold = a2.simulate2(house, params, tl, seed)["series"]["a"][-1][1]
    finally:
        am._NEIGHBOR_TEMP = old
    assert t_warm > t_cold + 0.5


# ── 4. calibrate2 ────────────────────────────────────────────────────────────────────

def _varying_tl(hours: int) -> list[dict]:
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=am.TZ)
    grid = []
    for i in range(hours * 4 + 1):
        t = t0 + timedelta(minutes=15 * i)
        hr = t.hour + t.minute / 60.0
        T_out = 16.0 + 6.0 * math.sin((hr - 9) / 24 * 2 * math.pi)
        s = max(0.0, math.sin((hr - 6) / 12 * math.pi)) * 350 if 6 < hr < 18 else 0.0
        grid.append({"t": t, "T_out": T_out, "irr": {"a": s, "b": 0.3 * s}, "irr_roof": {},
                     "states": {}, "weather": {"wind_speed": 3.0, "wind_dir": 210.0,
                                               "gust": 5.0, "precip": 0.0, "direct": 0.0,
                                               "diffuse": 0.0, "rh": 55.0},
                     "dt": 900.0, "sun_az": 180.0, "sun_el": 20.0})
    return grid


def _synthetic_samples(house, params, tl, seed, every_n=4):
    sim = a2.simulate2(house, params, tl, seed, calib_only_rooms=set(house["rooms"]))
    actual = {rid: [(t, v) for t, v in series[::every_n]]
              for rid, series in sim["series"].items()}
    actual_rh = {rid: [(t, v) for t, v in series[::every_n]]
                 for rid, series in sim["series_rh"].items()}
    return actual, actual_rh


def test_calibrate2_moves_toward_truth():
    # Data gegenereerd met een hogere schil-UA voor kamer a: de fit beweegt ua_env
    # in de goede richting én de RMSE daalt t.o.v. de prior-voorspelling.
    house = _toy_house()
    truth = a2.default_params2(house)
    truth["a"]["ua_env"] = 2.4
    tl = _varying_tl(30)
    seed = {z: 19.0 for z in list(house["rooms"]) + list(house["junctions"])}
    actual, actual_rh = _synthetic_samples(house, truth, tl, seed)
    start = a2.default_params2(house)
    rmse0, _ = a2.rmse_split2(house, start, tl, seed, actual, actual_rh)
    fitted, rmse1, rmse_rh1 = a2.calibrate2(house, start, tl, seed, actual, actual_rh,
                                            max_iter=3, time_budget_s=90.0, learn_rate=1.0)
    assert rmse1 < rmse0
    assert fitted["a"]["ua_env"] > start["a"]["ua_env"] + 0.05
    assert rmse_rh1 == rmse_rh1     # RH-kanaal levert een echte (niet-NaN) fout


def test_calibrate2_no_samples_is_noop():
    house = _toy_house()
    params = a2.default_params2(house)
    p2, r, rrh = a2.calibrate2(house, params, _tl(18.0, 2), {"a": 20.0}, {}, {})
    assert p2 is params and r != r and rrh != rrh


def test_calibrate2_anchor_is_ridge_target():
    # Zonder informatieve data (vlak weer, geen samples-variatie) trekt de ridge de
    # params richting het anker i.p.v. de kale priors.
    house = _toy_house()
    params = a2.default_params2(house)
    anchor = a2.default_params2(house)
    anchor["a"]["c_air"] = 3.0
    tl = _tl(20.0, hours=8)
    seed = {z: 20.0 for z in list(house["rooms"]) + list(house["junctions"])}
    actual = {"a": [(tl[8]["t"], 20.0), (tl[16]["t"], 20.0), (tl[24]["t"], 20.0)]}
    fitted, _, _ = a2.calibrate2(house, params, tl, seed, actual, {}, anchor=anchor,
                                 max_iter=2, time_budget_s=60.0, learn_rate=1.0)
    assert fitted["a"]["c_air"] > 1.3   # onderweg naar het 3.0-anker, niet op prior 1.0


# ── 5. Batch-anker + params-migratie ─────────────────────────────────────────────────

def test_maybe_adopt_anchor_blends_once():
    house = _toy_house()
    params = a2.default_params2(house)
    params["vent_eff"] = 0.4
    batch = {"params": {**a2.default_params2(house), "vent_eff": 1.2},
             "physics2_rev": a2.PHYSICS2_REV, "fitted_at": "2026-07-14T03:00:00+02:00",
             "model_version": "abc1234"}
    p1, anchor, stamps = a2.maybe_adopt_anchor(params, {}, batch)
    assert anchor is batch["params"]
    assert stamps["anchor_at"] == "2026-07-14T03:00:00+02:00"
    assert p1["vent_eff"] == pytest.approx(0.4 + a2.ANCHOR_BLEND * (1.2 - 0.4))
    # Zelfde anker al geadopteerd (anchor_at gelijk) → geen tweede blend.
    learned = {"anchor_at": "2026-07-14T03:00:00+02:00"}
    p2, anchor2, stamps2 = a2.maybe_adopt_anchor(p1, learned, batch)
    assert p2["vent_eff"] == pytest.approx(p1["vent_eff"])
    assert anchor2 is batch["params"]     # blijft wél het ridge-anker


def test_maybe_adopt_anchor_rev_gate():
    house = _toy_house()
    params = a2.default_params2(house)
    batch = {"params": {"vent_eff": 1.5}, "physics2_rev": a2.PHYSICS2_REV - 1,
             "fitted_at": "2026-07-14T03:00:00+02:00"}
    p, anchor, stamps = a2.maybe_adopt_anchor(params, {}, batch)
    assert anchor is None and p is params
    assert stamps["anchor_at"] is None


def test_merged_params2_rev_migration_resets_globals():
    house = {"rooms": {"a": {}}}
    old = {"params": {"cp_shelter_front": 0.11, "vent_eff": 0.3, "a": {"c_air": 1.7}},
           "physics2_rev": a2.PHYSICS2_REV - 1}
    p = a2.merged_params2(house, old)
    assert p["cp_shelter_front"] == a2.PRIORS2["cp_shelter_front"]
    assert p["vent_eff"] == a2.PRIORS2["vent_eff"]
    assert p["a"]["c_air"] == 1.7                     # kamer-params blijven
    assert p["a"]["c_fast"] == a2.PRIORS2["c_fast"]   # nieuwe keys → prior


def test_railed_params2_flags_bounds():
    house = _toy_house()
    p = a2.default_params2(house)
    p["vent_eff"] = a2.BOUNDS2["vent_eff"][0]
    p["a"]["c_deep"] = a2.BOUNDS2["c_deep"][1]
    rails = a2.railed_params2(p)
    assert "global.vent_eff@floor" in rails
    assert "a.c_deep@ceil" in rails


# ── 6. Historie-shards + batch-vensters ──────────────────────────────────────────────

def _wd_fixture(t0: datetime, n: int = 8) -> dict:
    hist = [{"t": (t0 + timedelta(minutes=15 * i)).isoformat(),
             "temp": 21.0 + 0.1 * i, "hum": 50 + i, "heat": 1 if i == 2 else 0}
            for i in range(n)]
    return {"rooms": {"Living room": {"history": hist, "inside": 22.0, "humidity": 55}}}


def test_append_history_shard_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(a2, "HISTORY_DIR", str(tmp_path))
    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=am.TZ)
    wd = _wd_fixture(t0)
    log = [{"t": t0.isoformat(), "states": {"a_win": "open"}}]
    n1 = a2.append_history_shard(wd, log, t0)
    assert n1 == 8
    n2 = a2.append_history_shard(wd, log, t0)     # tweede aanroep: no-op
    assert n2 == 0
    shard = json.load(open(tmp_path / "2026-07.json"))
    assert len(shard["rooms"]["Living room"]["ts"]) == 8
    assert len(shard["openings"]) == 1            # snapshot niet gedupliceerd


def test_append_history_shard_month_split(tmp_path, monkeypatch):
    monkeypatch.setattr(a2, "HISTORY_DIR", str(tmp_path))
    t0 = datetime(2026, 6, 30, 23, 30, tzinfo=am.TZ)   # samples rollen over de maandgrens
    wd = _wd_fixture(t0, n=6)
    a2.append_history_shard(wd, [], t0 + timedelta(hours=2))
    juni = json.load(open(tmp_path / "2026-06.json"))
    juli = json.load(open(tmp_path / "2026-07.json"))
    assert len(juni["rooms"]["Living room"]["ts"]) + \
        len(juli["rooms"]["Living room"]["ts"]) == 6


def test_load_dataset_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(a2, "HISTORY_DIR", str(tmp_path))
    house = _toy_house()
    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=am.TZ)
    a2.append_history_shard(_wd_fixture(t0), [{"t": t0.isoformat(), "states": {"x": 1}}], t0)
    ds = a2.load_dataset(house)
    assert len(ds["actual"]["a"]) == 8               # 'Living room' → kamer-id 'a'
    assert ds["actual"]["a"][0][1] == pytest.approx(21.0)
    assert len(ds["actual_rh"]["a"]) == 8
    assert len(ds["heat_on"]["a"]) == 1              # de ene heat-vlag
    assert ds["log"][0]["states"] == {"x": 1}
    assert ds["weather_rows"] == []                  # weer komt pas van de batch/backfill


def test_batch_windows_geometry():
    t0 = datetime(2026, 5, 20, 0, 0, tzinfo=am.TZ)
    t1 = t0 + timedelta(days=20)
    wins = a2.batch_windows(t0, t1)
    assert wins[-1][1] == t1                          # meest recente venster eindigt op t_max
    for start, end in wins:
        assert (end - start).days == a2.BATCH_WINDOW_D
        assert start >= t0 - timedelta(days=a2.BATCH_WINDOW_D)
    # stride tussen opeenvolgende eindes.
    assert (wins[1][1] - wins[0][1]).days == a2.BATCH_STRIDE_D


def test_collect_actual_rh_reads_hum():
    house = _toy_house()
    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=am.TZ)
    wd = _wd_fixture(t0)
    got = a2.collect_actual_rh(house, wd, t0 - timedelta(hours=1))
    assert got["a"][0][1] == 50.0
    assert len(got["a"]) == 8


def test_filters_apply_to_rh_channel():
    # Dezelfde exclusie-filters werken op de RH-dict (zelfde (t, waarde)-vorm): een
    # gestookt sample valt óók uit het vochtkanaal.
    t0 = datetime(2026, 7, 10, 12, 0, tzinfo=am.TZ)
    rh = {"a": [(t0, 50.0), (t0 + timedelta(minutes=15), 51.0)]}
    heat_on = {"a": {t0}}
    filtered, dropped = am.filter_heating_samples(rh, heat_on)
    assert dropped == {"a": 1}
    assert filtered["a"] == [(t0 + timedelta(minutes=15), 51.0)]


def test_batch_start_params_warm_start_and_rev_gate():
    # Wekelijkse batch start dóór op het vorige anker (cumulatieve convergentie —
    # het budget laat maar ~2 epochs per run toe); een rev-vreemd of leeg anker
    # valt terug op de kale priors (None).
    house = _toy_house()
    prev = {"params": {**a2.default_params2(house), "vent_eff": 0.7},
            "physics2_rev": a2.PHYSICS2_REV}
    start = a2.batch_start_params(house, prev)
    assert start["vent_eff"] == 0.7
    assert start["a"]["c_fast"] == a2.PRIORS2["c_fast"]   # nieuwe keys aangevuld
    assert a2.batch_start_params(house, {}) is None
    assert a2.batch_start_params(house, {"params": {"vent_eff": 0.7},
                                         "physics2_rev": a2.PHYSICS2_REV - 1}) is None

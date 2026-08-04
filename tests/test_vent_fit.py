"""Regressie-vangnet voor het leren van de ventilatie-tweeling (`vent_fit.py`, Project 13).

Port van de overlevende fit-secties uit tests/test_airflow_model.py, aangepast aan de
nieuwe API (RunContext als 5e positioneel argument van _residuals/calibrate), plus NIEUWE
tests voor de deadlock-proof anomalie-poort `anomaly_step` (max-hold-klep + cooloff — de
zelfvoedende hold die tweeling 2 bevroor kan niet terugkomen).

  1. Kalibratie herstelt bekende parameters uit synthetische data (RMSE daalt); ridge naar
     de priors; Huber-robuustheid; recency-weging; tm_seed-doorvoer; NaN-vangrail.
  2. reg_weight (regime-bewust solar_gain-anker) + railed_params + sensorloze-koker-keys.
  3. De drie sample-filters (airco / verwarming / pauze).
  4. Leercurve-uitdunning (thin_rmse_history) + naive_rmse/skill_score.
  5. Anomalie-poort: learning_baseline, anomaly_step (hold/escape/cooloff), nudge-cooldown.
"""
import math
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

import vent_fit as vf
import vent_io as vio
import vent_physics as vp

TZ = vp.TZ

# Reproduceert exact het oude module-default-gedrag (_LAT/_LON/_NEIGHBOR_TEMP/_GROUND_TEMP).
CTX = vp.RunContext(lat=52.09, lon=5.12,
                    neighbor_temp=vp.NEIGHBOR_TEMP, ground_temp=vp.GROUND_TEMP)


# ── 1. Kalibratie ────────────────────────────────────────────────────────────────────

def test_calibrate_reduces_error():
    # Het leren moet de voorspelfout aantoonbaar verkleinen. We toetsen de *fout* (robuust),
    # niet of individuele parameters exact teruggevonden worden — over één korte venster met
    # alle parameters vrij is dat onderbepaald (meerdere combinaties passen even goed). De
    # parameter-convergentie naar de waarheid komt over meerdere runs (online leren).
    house = _toy_house()
    tl = _varying_timeline(hours=24)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    truth = vio.default_params(house)
    for rid in house["rooms"]:
        truth[rid]["c_air"] = 2.0
        truth[rid]["solar_gain"] = 1.7
    sim_truth = vp.simulate(house, truth, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    actual = {rid: sim_truth["series"][rid][::4] for rid in house["rooms"]}  # elk uur

    p0 = vio.default_params(house)
    rmse0 = vf.rmse(vf._residuals(house, p0, tl, seed, CTX, actual, set(house["rooms"])))
    p1, rmse1 = vf.calibrate(house, p0, tl, seed, CTX, actual, max_iter=6, time_budget_s=20)
    assert rmse1 < rmse0 * 0.7                       # minstens 30% beter
    # Herhaald leren blijft verbeteren (of stabiel): tweede ronde niet slechter.
    _, rmse2 = vf.calibrate(house, p1, tl, seed, CTX, actual, max_iter=6, time_budget_s=20)
    assert rmse2 <= rmse1 + 1e-6


def test_h_am_is_learnable():
    # `h_am` (lucht↔massa-koppeling) leert nu per kamer mee, zodat `c_air` niet de enige knop
    # is voor de respons-snelheid van de luchtknoop (anders satureerde `c_air` op zijn grens).
    assert "h_am" in vp.PER_ROOM_PARAMS
    assert ("a", "h_am") in vf._param_keys(["a"])


def test_f_air_is_learnable():
    assert "f_air" in vp.PER_ROOM_PARAMS
    assert ("a", "f_air") in vf._param_keys(["a"])
    assert vp.BOUNDS["f_air"] == (0.1, 0.9)
    assert vio.default_params(_toy_house())["a"]["f_air"] == vp.PRIORS["f_air"]


def test_regularization_pulls_insensitive_param_to_prior():
    # De Tikhonov-ridge naar de priors moet een zwak-/niet-identificeerbare richting terug naar
    # zijn prior trekken i.p.v. 'm op een grens te laten staan. Met irr=0 heeft `solar_gain`
    # géén effect op de simulatie (nul gradient): zónder regularisatie zou hij op zijn
    # bovengrens (3.0) blijven plakken; mét regularisatie zakt hij richting de prior (1.0).
    house = _toy_house()
    tl = _const_timeline(14.0, hours=12, irr=0.0)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    truth = vio.default_params(house)
    sim = vp.simulate(house, truth, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    actual = {rid: sim["series"][rid][::4] for rid in house["rooms"]}

    start = vio.default_params(house)
    for rid in house["rooms"]:
        start[rid]["solar_gain"] = vp.BOUNDS["solar_gain"][1]   # gerailed op de bovengrens
    new, _ = vf.calibrate(house, start, tl, seed, CTX, actual, max_iter=4, time_budget_s=5)
    for rid in house["rooms"]:
        assert new[rid]["solar_gain"] < start[rid]["solar_gain"] - 0.4   # aantoonbaar losgetrokken
        assert new[rid]["solar_gain"] < 2.5                              # richting de prior (1.0)


def test_sensor_bias_shifts_residuals_not_physics():
    # De buitenmuur-bias is een meet-laag, geen fysica: de luchtknoop blijft identiek, maar
    # de vergelijking met de gemeten temp verschuift richting buiten. Bij koud buiten leest
    # de gebiasde voorspelling structureel lager dan de wáre luchttemp → negatieve residuen,
    # terwijl een ongebiasde kamer exact blijft.
    house = _toy_house()
    tl = _const_timeline(12.0, hours=12, irr=0.0)          # koud buiten
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 22.0 for z in zones}
    params = vio.default_params(house)
    sim = vp.simulate(house, params, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    # "Werkelijk" = de wáre luchtknoop (alsof de sensor perfect zou zijn).
    actual = {rid: sim["series"][rid][::4] for rid in house["rooms"]}

    # Ongebiasde kamer 'a' blijft exact; de fysica is onveranderd.
    res_a = vf._residuals(house, params, tl, seed, CTX, {"a": actual["a"]}, {"a"})
    assert max(abs(r) for r in res_a) < 1e-6

    # Bias op 'b' verandert sim["Ta"] níét, maar trekt de vergeleken voorspelling omlaag.
    house["rooms"]["b"]["sensor_outdoor_frac"] = 0.2
    sim2 = vp.simulate(house, params, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    assert sim2["Ta"]["b"] == pytest.approx(sim["Ta"]["b"], abs=1e-9)   # fysica intact
    res_b = vf._residuals(house, params, tl, seed, CTX, {"b": actual["b"]}, {"b"})
    assert all(r < 0 for r in res_b)                       # systematisch te laag
    assert min(res_b) < -0.5


def test_huber_weights_downweight_outliers():
    w = vf._huber_weights([0.0, 1.0, 1.5, 3.0], delta=1.5)
    assert w[0] == 1.0 and w[1] == 1.0 and w[2] == 1.0      # binnen ±delta
    assert w[3] == pytest.approx(1.5 / 3.0)                  # uitschieter gedempt
    # Gewogen kosten zijn lager dan de pure kwadraatsom bij een uitschieter.
    res = [0.2, -0.3, 5.0]
    assert vf._wcost(res) < sum(r * r for r in res)


def test_calibrate_robust_to_single_outlier():
    # Eén grof verkeerde sample mag de fit niet kapen: de geleerde params blijven dicht
    # bij die uit schone data.
    house = _toy_house()
    tl = _varying_timeline(hours=24)
    seed = {z: 20.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    truth = vio.default_params(house)
    for rid in house["rooms"]:
        truth[rid]["solar_gain"] = 1.6
    sim = vp.simulate(house, truth, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    clean = {rid: sim["series"][rid][::4] for rid in house["rooms"]}
    # Injecteer één absurde sample in kamer 'a'.
    poisoned = {rid: list(s) for rid, s in clean.items()}
    t_bad, _ = poisoned["a"][3]
    poisoned["a"][3] = (t_bad, 80.0)
    p_clean, _ = vf.calibrate(house, vio.default_params(house), tl, seed, CTX, clean,
                              max_iter=5, time_budget_s=20)
    p_pois, _ = vf.calibrate(house, vio.default_params(house), tl, seed, CTX, poisoned,
                             max_iter=5, time_budget_s=20)
    # De geleerde solar_gain mag door de uitschieter niet ver wegschieten.
    assert abs(p_pois["a"]["solar_gain"] - p_clean["a"]["solar_gain"]) < 0.4


# ── 2. tm_seed-doorvoer (massaknoop-startwaarde) ─────────────────────────────────────

def test_residuals_forward_tm_seed_to_simulate():
    # _residuals(_timed) geven tm_seed door aan simulate(), zodat de fit dezelfde massaknoop-
    # startwaarde ziet als de eind-run. Korte tijdlijn zodat het startverschil nog in de samples
    # doorwerkt; via H_am koppelt een warme/koude massa terug op de voorspelde luchttemp.
    house = _toy_house()
    params = vio.default_params(house)
    tl = _const_timeline(20.0, hours=3, irr=0.0)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    times = [tl[i]["t"] for i in (4, 8, 12)]
    actual = {rid: [(t, 20.0) for t in times] for rid in house["rooms"]}
    rooms_set = set(house["rooms"])
    warm = vf._residuals(house, params, tl, seed, CTX, actual, rooms_set,
                         tm_seed={z: 30.0 for z in zones})
    cold = vf._residuals(house, params, tl, seed, CTX, actual, rooms_set,
                         tm_seed={z: 10.0 for z in zones})
    assert len(warm) == len(cold) == len(times) * len(house["rooms"])
    assert sum(warm) > sum(cold)   # warme massa → warmere voorspelling → grotere residuen
    # geen tm_seed → terugval op de warme blend (default gedrag, geen breuk)
    default = vf._residuals(house, params, tl, seed, CTX, actual, rooms_set)
    assert len(default) == len(warm)


def test_calibrate_forwards_tm_seed():
    # calibrate() reikt tm_seed door tot élke interne simulatie (r0, Jacobiaan, accept-check,
    # eind-RMSE): een warme vs. gematchte massa-seed levert een andere eind-RMSE.
    house = _toy_house()
    params = vio.default_params(house)
    tl = _const_timeline(20.0, hours=3, irr=0.0)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    times = [tl[i]["t"] for i in (4, 8, 12)]
    actual = {rid: [(t, 20.0) for t in times] for rid in house["rooms"]}
    _, rmse_warm = vf.calibrate(house, params, tl, seed, CTX, actual,
                                tm_seed={z: 30.0 for z in zones})
    _, rmse_match = vf.calibrate(house, params, tl, seed, CTX, actual,
                                 tm_seed={z: 20.0 for z in zones})
    assert rmse_warm == rmse_warm and rmse_match == rmse_match   # beide eindig
    assert rmse_warm > rmse_match   # de warme massa-bias overleeft één online kalibratiestap


def test_window_mean_tm_seed_beats_warm_blend_on_slow_mass():
    # De kern van de fix: bij een trage massaknoop (grote c_mass, zwakke h_am) wast de 24u warmup
    # de warme buur-blend NIET uit → de massa blijft te warm en trekt via H_am de lucht mee omhoog.
    # Een op de gemeten historie geankerde tm_seed (venster-gemiddelde) vermijdt die bias. ua_party
    # + q_int op 0 zodat het contrast puur de massa-seed is (geen party/interne warmte-confound).
    house = _toy_house()
    params = vio.default_params(house)
    for rid in house["rooms"]:
        params[rid]["c_mass"] = 10.0   # trage massa → τ ≫ 24u → warmup equilibreert niet
        params[rid]["h_am"] = 0.3
        params[rid]["ua_party"] = 0.0
        params[rid]["q_int"] = 0.0
    tl = _const_timeline(20.0, hours=24, irr=0.0)   # huis in evenwicht met 20°C buiten
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    times = [tl[i]["t"] for i in range(len(tl) // 2, len(tl), 8)]
    actual = {rid: [(t, 20.0) for t in times] for rid in house["rooms"]}
    rooms_set = set(house["rooms"])
    ctx = replace(CTX, neighbor_temp=26.0)   # zomers buur-anker → warme blend seed = 0.5·(20+26) = 23°C
    blend = vf._residuals(house, params, tl, seed, ctx, actual, rooms_set)        # geen tm_seed
    seeded = vf._residuals(house, params, tl, seed, ctx, actual, rooms_set,
                           tm_seed={z: 20.0 for z in zones})                       # venster-mean
    assert vf.rmse(seeded) < vf.rmse(blend)   # data-geankerde seed → lagere fout
    assert sum(blend) > sum(seeded)           # de blend is systematisch te warm (positieve bias)


# ── 3. solar_gain beschermd tegen instorten + regime-bewuste ridge ───────────────────

def test_solar_gain_floor_and_per_param_ridge():
    assert vp.BOUNDS["solar_gain"][0] == 0.25                 # fysieke vloer
    assert vf.reg_weight("solar_gain") > vf.reg_weight("ua_env")  # sterkere ankering
    assert vf.reg_weight("ua_env") == vf.REG_WEIGHT


def test_solar_gain_cannot_collapse_to_zero():
    # Zelfs als de data naar een lage zonwinst trekt, houdt de vloer (+ ridge) solar_gain
    # ≥ 0.25 — de twin blijft enige zonrespons houden voor de hete zonnige dagen.
    house = _toy_house()
    tl = _varying_timeline(hours=24)
    seed = {z: 20.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    truth = vio.default_params(house)
    for rid in house["rooms"]:
        truth[rid]["solar_gain"] = 0.0   # "waarheid" zonder zonwinst (geclamped naar de vloer)
    sim = vp.simulate(house, truth, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    actual = {rid: sim["series"][rid][::4] for rid in house["rooms"]}
    new, _ = vf.calibrate(house, vio.default_params(house), tl, seed, CTX, actual,
                          max_iter=5, time_budget_s=20)
    for rid in house["rooms"]:
        assert new[rid]["solar_gain"] >= 0.25


def test_reg_weight_regime_aware_solar_gain():
    # Het extra-sterke solar_gain-anker (6.0) ramp terug naar REG_WEIGHT (3.0) zodra het venster
    # zonniger wordt: bewolkt → vol anker (anti-collapse), zonnig → vertrouw de data.
    base = vf.REG_WEIGHT_BY_PARAM["solar_gain"]
    assert vf.reg_weight("solar_gain") == base                              # geen solar_mean → oud
    assert vf.reg_weight("solar_gain", vf.SOLAR_RIDGE_LOW_WM2 - 10) == base   # bewolkt → vol anker
    assert vf.reg_weight("solar_gain", vf.SOLAR_RIDGE_HIGH_WM2 + 10) == vf.REG_WEIGHT  # zonnig → relax
    mid = vf.reg_weight("solar_gain", 0.5 * (vf.SOLAR_RIDGE_LOW_WM2 + vf.SOLAR_RIDGE_HIGH_WM2))
    assert vf.REG_WEIGHT < mid < base                                        # monotone tussenwaarde
    # Andere params zijn niet regime-afhankelijk.
    assert vf.reg_weight("ua_env", 400) == vf.REG_WEIGHT


# ── 4. Recency-weging ────────────────────────────────────────────────────────────────

def test_recency_weights_decay():
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    times = [t0, t0 + timedelta(hours=18), t0 + timedelta(hours=36)]   # oud → nieuw
    w = vf._recency_weights(times, half_life_h=18.0)
    assert w[-1] == pytest.approx(1.0)        # nieuwste = referentie
    assert w[1] == pytest.approx(0.5)         # één half-life ouder
    assert w[0] == pytest.approx(0.25)        # twee half-lives ouder
    # Uitgeschakeld → uniform.
    assert vf._recency_weights(times, half_life_h=0) == [1.0, 1.0, 1.0]
    assert vf._recency_weights([]) == []


def test_recency_weighting_favors_recent_regime(monkeypatch):
    # Bij een regime-wissel in het venster moet de fit het HUIDIGE (recente) regime beter volgen.
    # We bouwen 'actual' uit een truth-sim maar verlagen de recente helft met een offset (koeler
    # regime); met recency-weging horen de recente residuen kleiner te zijn dan zonder weging.
    house = _toy_house()
    tl = _varying_timeline(hours=48)
    seed = {z: 20.0 for z in list(house["rooms"]) + list(house.get("junctions", {}))}
    truth = vio.default_params(house)
    sim = vp.simulate(house, truth, tl, seed, CTX, calib_only_rooms=set(house["rooms"]))
    mid = tl[0]["t"] + timedelta(hours=24)
    delta = 2.0
    actual = {rid: [(t, (v - delta if t >= mid else v)) for t, v in sim["series"][rid][::4]]
              for rid in house["rooms"]}

    def recent_rmse(params):
        res = [r for t, r in vf._residuals_timed(house, params, tl, seed, CTX, actual,
                                                 set(house["rooms"]))
               if t >= mid]
        return math.sqrt(sum(r * r for r in res) / len(res))

    monkeypatch.setattr(vf, "RECENCY_HALFLIFE_H", 0.0)      # uniform
    p_uniform, _ = vf.calibrate(house, vio.default_params(house), tl, seed, CTX, actual,
                                max_iter=6, time_budget_s=20)
    monkeypatch.setattr(vf, "RECENCY_HALFLIFE_H", 6.0)      # sterke recency
    p_recent, _ = vf.calibrate(house, vio.default_params(house), tl, seed, CTX, actual,
                               max_iter=6, time_budget_s=20)
    assert recent_rmse(p_recent) < recent_rmse(p_uniform)


# ── 5. AC-guard-venster (niet-teruggedateerde airco-melding) ─────────────────────────

def test_filter_ac_samples_guard_window():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=TZ)
    # Eén log-entry zet de airco 'nu' in kamer 'b' (geen terugdatering): zónder guard zou de
    # AC-koude van de uren vóór de melding ín de fit blijven. De guard laat 'b's recente samples vallen.
    acc = [(now, "b")]
    actual = {
        "a": [(now - timedelta(hours=h), 22.0) for h in range(10, 0, -1)],
        "b": [(now - timedelta(hours=h), 18.0) for h in range(10, 0, -1)],
    }
    filt, excl = vf.filter_ac_samples(actual, acc, "b", now, guard_h=6.0)
    assert "a" not in excl                       # andere kamer ongemoeid
    assert len(filt["a"]) == len(actual["a"])
    assert excl["b"] == 6                          # h=1..6 binnen de guard vallen weg
    assert all(t < now - timedelta(hours=6) for t, _ in filt["b"])
    # Lege log → niets gefilterd (backwards-compatibel).
    filt2, excl2 = vf.filter_ac_samples(actual, [], None, now)
    assert excl2 == {} and filt2["b"] == actual["b"]


# ── 6. Verwarmings-uitsluiting (auto, uit de tado heat-vlag) ─────────────────────────

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
    now = datetime(2026, 1, 15, 22, 0, tzinfo=TZ)
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
    heat_on = vio.collect_heating_on(house, wd, since)
    assert heat_on["ted"] == {ts[3], ts[4]}
    assert "bath" not in heat_on                       # badkamer stookte niet
    assert vio.heating_now(house, wd) == {"ted": True, "bath": False}

    actual = {"ted": [(t, 0.0) for t in ts], "bath": [(t, 0.0) for t in ts]}
    filt, excl = vf.filter_heating_samples(actual, heat_on)
    assert excl == {"ted": 2}                           # twee stook-samples weg
    assert len(filt["ted"]) == 3 and len(filt["bath"]) == 5
    assert all(t not in heat_on["ted"] for t, _ in filt["ted"])
    # Lege heat_on → ongewijzigd (backwards-compatibel met oude JSON zonder heat-vlag).
    filt2, excl2 = vf.filter_heating_samples(actual, {})
    assert excl2 == {} and filt2 == actual


def test_filter_heating_drops_room_when_all_heated():
    now = datetime(2026, 1, 15, 22, 0, tzinfo=TZ)
    ts = [now - timedelta(hours=h) for h in range(3, -1, -1)]
    actual = {"ted": [(t, 21.0) for t in ts]}
    heat_on = {"ted": set(ts)}                          # heel het venster gestookt
    filt, excl = vf.filter_heating_samples(actual, heat_on)
    assert "ted" not in filt                            # kamer valt uit de kalibratie
    assert excl["ted"] == 4


# ── 7. Pauze-uitsluiting (huis-breed, handmatig via de modal) ────────────────────────

def test_filter_paused_samples_drops_all_rooms_house_wide():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=TZ)
    intervals = [(now - timedelta(hours=4), now - timedelta(hours=1))]
    actual = {
        "a": [(now - timedelta(hours=h), 22.0) for h in range(6, 0, -1)],
        "b": [(now - timedelta(hours=h), 23.0) for h in range(6, 0, -1)],
    }
    filt, excl = vf.filter_paused_samples(actual, intervals)
    # Uren 4,3,2,1 vallen binnen het interval → 4 samples weg per kamer, huis-breed.
    assert excl == {"a": 4, "b": 4}
    assert len(filt["a"]) == 2 and len(filt["b"]) == 2
    assert all(t < now - timedelta(hours=4) or t > now - timedelta(hours=1) for t, _ in filt["a"])
    # Lege intervals → ongewijzigd (backwards-compatibel).
    filt2, excl2 = vf.filter_paused_samples(actual, [])
    assert excl2 == {} and filt2 == actual


def test_filter_paused_samples_drops_room_entirely_when_fully_paused():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=TZ)
    ts = [now - timedelta(hours=h) for h in range(3, -1, -1)]
    actual = {"a": [(t, 21.0) for t in ts]}
    intervals = [(ts[0], now)]   # heel het venster gepauzeerd
    filt, excl = vf.filter_paused_samples(actual, intervals)
    assert "a" not in filt
    assert excl["a"] == 4


# ── 8. railed_params + sensorloze koker in de parametervector ────────────────────────

def test_railed_params_flags_bounds():
    params = vio.default_params(_toy_house())
    params["cp_shelter"] = vp.BOUNDS["cp_shelter"][0]      # op de vloer
    params["a"]["solar_gain"] = vp.BOUNDS["solar_gain"][1]  # op het plafond
    params["a"]["ua_env"] = 1.0                            # midden → niet gerailed
    flags = vf.railed_params(params)
    assert "global.cp_shelter@floor" in flags
    assert "a.solar_gain@ceil" in flags
    assert not any(f.startswith("a.ua_env") for f in flags)


# ── 8b. Per-kamer parametergrenzen uit het huismodel (`param_bounds`) ────────────────

def test_param_bounds_versmalt_alleen_per_kamer():
    house = {"rooms": {"a": {"param_bounds": {"c_mass": {"min": 0.6}}}, "b": {}}}
    glo, ghi = vp.BOUNDS["c_mass"]
    assert vp.param_bounds(house, "a", "c_mass") == (0.6, ghi)
    assert vp.param_bounds(house, "b", "c_mass") == (glo, ghi)   # andere kamer: onaangeroerd
    assert vp.param_bounds(house, "a", "c_air") == vp.BOUNDS["c_air"]   # andere param: idem
    assert vp.param_bounds(house, "global", "vent_eff") == vp.BOUNDS["vent_eff"]
    assert vp.param_bounds(None, "a", "c_mass") == (glo, ghi)    # geen huis → globaal


def test_param_bounds_kan_nooit_buiten_de_fysieke_band_stappen():
    """Een override VERSMALT; hij mag de band niet oprekken en niet omkeren — anders zou een
    typefout in house_model.json de fysieke klem stilzwijgend uitschakelen."""
    glo, ghi = vp.BOUNDS["c_mass"]
    ruim = {"rooms": {"a": {"param_bounds": {"c_mass": {"min": -99, "max": 999}}}}}
    assert vp.param_bounds(ruim, "a", "c_mass") == (glo, ghi)
    omgekeerd = {"rooms": {"a": {"param_bounds": {"c_mass": {"min": 5.0, "max": 1.0}}}}}
    assert vp.param_bounds(omgekeerd, "a", "c_mass") == (glo, ghi)   # onzin → val terug


def test_clamp_model_bounds_raakt_de_globale_band_niet():
    """merged_params gebruikt deze variant: de bewuste kamer-vloer moet meteen gelden, maar
    een geleerde waarde mag verder ongemoeid doorgegeven worden."""
    house = {"rooms": {"a": {"param_bounds": {"c_mass": {"min": 0.6}}}}}
    assert vp.clamp_model_bounds(house, "a", "c_mass", 0.3) == pytest.approx(0.6)
    assert vp.clamp_model_bounds(house, "a", "c_mass", 1.4) == pytest.approx(1.4)
    # Buiten de globale BOUNDS blijft het getal staan — dat is de taak van de fit.
    boven = vp.BOUNDS["c_mass"][1] + 5.0
    assert vp.clamp_model_bounds(house, "a", "c_mass", boven) == pytest.approx(boven)


def test_railed_params_onderscheidt_huismodel_grens_van_saturatie():
    """Twee heel verschillende signalen, en poorten mogen alleen op het eerste afgaan: een
    BOUNDS-rail is een klacht (de fysica wil ergens heen waar ze niet mag), een
    huismodel-rail is de constraint die precies doet waarvoor hij is aangebracht."""
    house = {"rooms": {"a": {"param_bounds": {"c_mass": {"min": 0.6}}}}}
    params = {"a": {"c_mass": 0.6, "solar_gain": vp.BOUNDS["solar_gain"][0]}}
    flags = vf.railed_params(params, house=house)
    assert "a.c_mass@floor(model)" in flags
    assert "a.solar_gain@floor" in flags
    assert vf.is_model_rail("a.c_mass@floor(model)")
    assert not vf.is_model_rail("a.solar_gain@floor")


def test_huismodel_draagt_de_living_c_mass_vloer():
    """Vangnet tegen stil verlies: zonder deze vloer leert de nowcast-fit living's
    thermische massa terug naar de globale ondergrens en groeit de voorspelde dagcyclus
    weer — zie de motivering in house_model.json."""
    house = vio.load_house()
    lo, _hi = vp.param_bounds(house, "living", "c_mass")
    assert lo == pytest.approx(0.595)
    # De vloer moet ook echt in de ingelezen params landen (merged_params-poort).
    learned = {"params": {"living": {"c_mass": 0.30}}, "physics_rev": vio.PHYSICS_REV}
    assert vio.merged_params(house, learned)["living"]["c_mass"] == pytest.approx(0.595)


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


def test_coupled_sensorless_zones_finds_the_shaft_only():
    house = _strat_house()
    # 'shaft' heeft geen meting maar hangt via open deuren aan twee gemeten kamers.
    assert vf.coupled_sensorless_zones(house, ["top", "bot"]) == ["shaft"]
    # Een gemeten zone hoort er nooit in.
    assert vf.coupled_sensorless_zones(house, ["top", "bot", "shaft"]) == []
    # Zonder ook maar één meting valt er niets te identificeren.
    assert vf.coupled_sensorless_zones(house, []) == []
    # Een sensorloze zone zónder deur naar een gemeten kamer blijft buiten de vector.
    lonely = _strat_house()
    lonely["rooms"]["cellar"] = {"volume_m3": 20, "exterior_wall_m2": 8}
    assert "cellar" not in vf.coupled_sensorless_zones(lonely, ["top", "bot"])


def test_param_keys_includes_the_sensorless_shaft():
    plain = vf._param_keys(["top", "bot"])
    assert ("shaft", "ua_roof") not in plain
    with_shaft = vf._param_keys(["top", "bot"], ["shaft"])
    assert ("shaft", "ua_roof") in with_shaft
    assert ("shaft", "c_mass") in with_shaft
    # De globalen blijven precies één keer vooraan staan.
    assert with_shaft[:len(vp.GLOBAL_PARAMS)] == [("global", g) for g in vp.GLOBAL_PARAMS]
    assert len(with_shaft) == len(plain) + len(vp.PER_ROOM_PARAMS)


# ── 9. Weer-genormaliseerde skill + persistentie-baseline ────────────────────────────

def test_naive_rmse_persistence_baseline():
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
    actual = {"a": [(t0, 20.0), (t0, 22.0), (t0, 18.0)]}   # baseline = eerste sample (20)
    assert vf.naive_rmse(actual) == pytest.approx(math.sqrt((0 + 4 + 4) / 3))
    assert vf.naive_rmse({}) != vf.naive_rmse({})          # geen samples → NaN


def test_skill_score_normalizes_and_clamps():
    assert vf.skill_score(0.5, 1.0) == pytest.approx(0.5)   # half zo goed als persistentie
    assert vf.skill_score(0.0, 1.0) == pytest.approx(1.0)   # perfect, geklemd op 1
    assert vf.skill_score(2.0, 1.0) == pytest.approx(-1.0)  # slechter dan persistentie
    assert vf.skill_score(0.5, 0.0) is None                 # vlakke dag → onbruikbare baseline
    assert vf.skill_score(float("nan"), 1.0) is None


# ── 10. Leercurve-uitdunning (thin_rmse_history) ─────────────────────────────────────

def _lp(t, **kw):
    """Leercurve-punt met tijdstip t (aware datetime) en optionele extra velden."""
    return {"t": t.isoformat(), "rmse": kw.pop("rmse", 0.5), **kw}


def test_thin_rmse_history_keeps_recent_full_resolution():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=TZ)
    hist = [_lp(now - timedelta(minutes=15 * i))
            for i in range(int(vf.CALIB_WINDOW_H * 4) - 1, -1, -1)]
    assert vf.thin_rmse_history(hist, now) == hist   # alles binnen het venster → onaangeroerd


def test_thin_rmse_history_thins_old_to_hourly_and_drops_ancient():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=TZ)
    old = (now - timedelta(days=4)).replace(minute=0, second=0, microsecond=0)
    hist = [_lp(now - timedelta(days=vf.RMSE_HISTORY_DAYS, hours=1))]   # te oud → weg
    for h in (0, 1):
        hist += [_lp(old + timedelta(hours=h, minutes=m)) for m in (0, 15, 30, 45)]
    out = vf.thin_rmse_history(hist, now)
    assert len(out) == 2                              # één per klok-uur
    assert all(datetime.fromisoformat(p["t"]).minute == 45 for p in out)  # laatste wint
    assert [p["t"] for p in out] == sorted(p["t"] for p in out)           # chronologisch


def test_thin_rmse_history_prefers_live_representative():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=TZ)
    h0 = (now - timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    hist = [_lp(h0, rmse=0.4),                                        # live, eerst
            _lp(h0 + timedelta(minutes=15), rmse=0.6, held=True),     # later maar held
            _lp(h0 + timedelta(minutes=30), rmse=0.9, paused=True)]   # laatst maar paused
    out = vf.thin_rmse_history(hist, now)
    assert len(out) == 1 and out[0]["rmse"] == 0.4    # live verdringt latere held/paused
    # Bucket zónder live punt → gewoon de laatste.
    hist2 = [_lp(h0, rmse=0.4, held=True), _lp(h0 + timedelta(minutes=15), rmse=0.6, held=True)]
    assert vf.thin_rmse_history(hist2, now)[0]["rmse"] == 0.6


def test_thin_rmse_history_idempotent_and_capped(monkeypatch):
    now = datetime(2026, 7, 7, 12, 0, tzinfo=TZ)
    hist = [_lp(now - timedelta(hours=h, minutes=m))
            for h in range(int(vf.RMSE_HISTORY_DAYS * 24) - 1, -1, -1) for m in (30, 15, 0)]
    once = vf.thin_rmse_history(hist, now)
    assert vf.thin_rmse_history(once, now) == once    # idempotent
    assert len(once) < len(hist)
    monkeypatch.setattr(vf, "RMSE_HISTORY_KEEP", 5)   # hard vangnet blijft werken
    assert len(vf.thin_rmse_history(hist, now)) == 5


def test_thin_rmse_history_feeds_weekjournaal_lookback():
    # De uitgedunde curve moet het weekjournaal een écht ≥6.5-daags vergelijkpunt geven
    # (voorheen strandde de "week"-trend op een 2.5-daagse count-slice).
    import weekjournaal as wj
    now = datetime(2026, 7, 7, 12, 0, tzinfo=TZ)
    hist, t = [], now - timedelta(days=vf.RMSE_HISTORY_DAYS)
    while t <= now:
        old = t < now - timedelta(days=5)
        hist.append({"t": t.isoformat(), "rmse": 0.8 if old else 0.5, "skill": 0.4})
        t += timedelta(minutes=15)
    out = vf.thin_rmse_history(hist, now)
    s = wj.twin_section({"rmse_history": out}, now)
    assert s and "was 0.80°" in s                      # vergelijkpunt ligt ≥6.5 d terug


# ── 11. Solver-state-klem + NaN-vangrail ─────────────────────────────────────────────

def test_clamp_to_bounds_clamps_solver_state():
    keys = [("global", "cp_shelter"), ("a", "solar_gain")]
    lo_cp, hi_cp = vp.BOUNDS["cp_shelter"]
    lo_sg, hi_sg = vp.BOUNDS["solar_gain"]
    assert vf._clamp_to_bounds([-5.0, 99.0], keys) == [lo_cp, hi_sg]
    mid = [(lo_cp + hi_cp) / 2.0, (lo_sg + hi_sg) / 2.0]
    assert vf._clamp_to_bounds(mid, keys) == mid       # binnen de band → ongewijzigd


def test_calibrate_nan_final_rmse_returns_old_params(monkeypatch):
    # R8: een NaN in de geblende eind-RMSE mag nooit geblende params de persist-keten in
    # duwen — calibrate valt dan terug op de ingevoerde params + de pre-solve-RMSE.
    house = _toy_house()
    tl = _varying_timeline(hours=6)
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    seed = {z: 20.0 for z in zones}
    sim = vp.simulate(house, vio.default_params(house), tl, seed, CTX,
                      calib_only_rooms=set(house["rooms"]))
    actual = {rid: sim["series"][rid][::4] for rid in house["rooms"]}
    p0 = vio.default_params(house)
    calls = []

    def fake_rmse(res):
        calls.append(1)
        return float("nan") if len(calls) == 1 else 0.123   # 1e = final blend, 2e = r0-fallback

    monkeypatch.setattr(vf, "rmse", fake_rmse)
    p1, r1 = vf.calibrate(house, p0, tl, seed, CTX, actual, max_iter=0, time_budget_s=5)
    assert p1 is p0
    assert r1 == 0.123


# ── 12. Anomalie-poort: baseline + anomaly_step + nudge-cooldown ─────────────────────

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=TZ)


def _hist(*rmses):
    return [{"t": "2026-06-01T00:00:00+02:00", "rmse": v} for v in rmses]


def test_learning_baseline_needs_enough_history():
    # Te weinig historie → geen norm (eerst bootstrappen, ook bij hoge fout).
    assert vf.learning_baseline(_hist(0.5, 0.5, 0.5)) is None


def test_learning_baseline_excludes_held_runs():
    # Een langdurige anomalie (gepauzeerde runs met hoge RMSE) mag de norm niet optrekken:
    # de baseline blijft op de goede runs hangen, dus het blijft pauzeren tot de log weer
    # klopt.
    hist = _hist(*([0.5] * 20)) + [{"t": "x", "rmse": 12.0, "held": True} for _ in range(20)]
    assert vf.learning_baseline(hist) == pytest.approx(0.5, abs=1e-9)
    hold, base, _ = vf.anomaly_step(5.0, hist, None, NOW)
    assert base == pytest.approx(0.5, abs=1e-9)
    assert hold is True


def test_anomaly_step_no_hold_without_enough_history():
    # (a) Te weinig historie → nooit pauzeren (eerst bootstrappen, ook bij hoge fout).
    hold, base, st = vf.anomaly_step(9.0, _hist(0.5, 0.5, 0.5), None, NOW)
    assert hold is False
    assert base is None
    assert st["held_since"] is None


def test_anomaly_step_holds_when_error_anomalous():
    # (b) Lange historie rond 0.5°C, nu plots 3°C → ver boven 2.2× norm én boven de vloer.
    hist = _hist(*([0.5] * 30))
    hold, base, st = vf.anomaly_step(3.0, hist, None, NOW)
    assert hold is True
    assert base == pytest.approx(0.5, abs=1e-9)
    assert st["held_since"] == NOW.isoformat()          # episode start nú


def test_anomaly_step_no_hold_when_error_normal():
    hist = _hist(*([0.5] * 30))
    assert vf.anomaly_step(0.7, hist, None, NOW)[0] is False     # binnen de norm
    # Hoog t.o.v. norm maar absoluut klein (< ANOMALY_FLOOR) → niet pauzeren.
    assert vf.anomaly_step(1.2, _hist(*([0.2] * 30)), None, NOW)[0] is False


def test_anomaly_step_hold_persists_under_max_hold():
    # (c) Een lopende hold jonger dan ANOMALY_MAX_HOLD_H blijft holden; held_since (de
    # episode-start) wordt gedragen, niet opnieuw gestempeld. nudged_at blijft staan
    # zolang de episode loopt (de cooldown-administratie van de nudge).
    hist = _hist(*([0.5] * 30))
    since = (NOW - timedelta(hours=vf.ANOMALY_MAX_HOLD_H - 1)).isoformat()
    nudged = (NOW - timedelta(hours=2)).isoformat()
    hold, _, st = vf.anomaly_step(3.0, hist, {"held_since": since, "nudged_at": nudged}, NOW)
    assert hold is True
    assert st["held_since"] == since
    assert st["nudged_at"] == nudged


def test_anomaly_step_escape_valve_after_max_hold():
    # (d) Op ≥ ANOMALY_MAX_HOLD_H aaneengesloten hold vuurt de klep: leren hervat (hold
    # False), escaped_at gestempeld op nú, en de poort is ANOMALY_REARM_H ontwapend.
    hist = _hist(*([0.5] * 30))
    since = (NOW - timedelta(hours=vf.ANOMALY_MAX_HOLD_H)).isoformat()
    hold, _, st = vf.anomaly_step(3.0, hist, {"held_since": since}, NOW)
    assert hold is False
    assert st["held_since"] is None
    assert st["escaped_at"] == NOW.isoformat()
    assert st["cooloff_until"] == (NOW + timedelta(hours=vf.ANOMALY_REARM_H)).isoformat()


def test_anomaly_step_cooloff_never_holds():
    # (e) Binnen de cooloff holdt de poort nooit, hoe anomaal de fout ook is — de baseline
    # moet eerst het nieuwe regime kunnen absorberen. escaped_at is transient: alleen de
    # run waarin de klep vuurde draagt 'm.
    hist = _hist(*([0.5] * 30))
    st_in = {"cooloff_until": (NOW + timedelta(hours=1)).isoformat(),
             "escaped_at": (NOW - timedelta(hours=1)).isoformat()}
    hold, _, st = vf.anomaly_step(9.0, hist, st_in, NOW)
    assert hold is False
    assert st["held_since"] is None
    assert "escaped_at" not in st


def test_anomaly_step_cooloff_expiry_clears_state():
    # (f) Ná de cooloff, met een normale fout, wordt de episode-administratie gewist
    # (held_since/nudged_at/cooloff_until) — en een látere anomalie mag weer gewoon holden.
    hist = _hist(*([0.5] * 30))
    st_in = {"cooloff_until": (NOW - timedelta(hours=1)).isoformat(),
             "nudged_at": (NOW - timedelta(hours=30)).isoformat()}
    hold, _, st = vf.anomaly_step(0.6, hist, st_in, NOW)
    assert hold is False
    assert st["held_since"] is None and st["nudged_at"] is None
    assert st["cooloff_until"] is None
    later = NOW + timedelta(hours=2)
    hold2, _, st2 = vf.anomaly_step(3.0, hist, st, later)
    assert hold2 is True
    assert st2["held_since"] == later.isoformat()


def test_anomaly_step_physics_migration_no_hold_with_cooloff():
    # (g) Een revisie-migratie holdt nooit (de reset-sprong is verwacht — erdoorheen leren
    # is het doel) en ontwapent de poort meteen met een cooloff.
    hist = _hist(*([0.5] * 30))
    hold, base, st = vf.anomaly_step(9.0, hist, {"held_since": NOW.isoformat()}, NOW,
                                     physics_migrated=True)
    assert hold is False
    assert base == pytest.approx(0.5, abs=1e-9)
    assert st["held_since"] is None
    assert st["cooloff_until"] == (NOW + timedelta(hours=vf.ANOMALY_REARM_H)).isoformat()


def test_anomaly_step_normal_run_clears_nudged_at():
    # (h) Een normale run wist de episode-administratie, zodat een vólgende anomalie
    # meteen weer mag nudgen.
    hist = _hist(*([0.5] * 30))
    st_in = {"held_since": (NOW - timedelta(hours=2)).isoformat(),
             "nudged_at": (NOW - timedelta(hours=2)).isoformat()}
    hold, _, st = vf.anomaly_step(0.6, hist, st_in, NOW)
    assert hold is False
    assert st["nudged_at"] is None and st["held_since"] is None


def test_should_nudge_anomaly_cooldown():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=TZ)
    assert vf.should_nudge_anomaly(None, now) is True                    # episode-start
    fresh = (now - timedelta(hours=1)).isoformat()
    assert vf.should_nudge_anomaly(fresh, now) is False                  # binnen cooldown
    old = (now - timedelta(hours=vf.ANOMALY_NUDGE_COOLDOWN_H)).isoformat()
    assert vf.should_nudge_anomaly(old, now) is True                     # cooldown verstreken
    assert vf.should_nudge_anomaly("kapot", now) is True                 # onleesbare stempel


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


def _varying_timeline(hours: int) -> list[dict]:
    t0 = datetime(2026, 6, 15, 0, 0, tzinfo=TZ)
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

"""Tests voor insulation_analysis.py (Project 12).

Pure-functie-checks, geen netwerk, geen echte tado-export:
  1. parse_tado_cache / resample_hourly: dayReport-vorm → uurgemiddelden +
     de heating_off-maskering (elk 15-min-sample in het uur moet "NONE" zijn).
  2. fit_room_ua herstelt een bekende UA uit synthetische vrije-uitloop-data.
  3. Geometrie-narratief (_facade_label, room_geometry_summary, online_ua_estimate)
     tegen zowel een gecontroleerd synthetisch huis als het echte house_model.json.
"""
import json
import math

import airflow_model as am
import insulation_analysis as ia


def _rel_err(actual, expected):
    return abs(actual - expected) / abs(expected)


# ── 1. Parsen van de tado dayReport-vorm ────────────────────────────────────────────

def _day_report(date_from: str, temps: list[tuple], heat_intervals: list[tuple]) -> dict:
    """Bouwt één dayReport-entry zoals in de echte export: `temps` = [(iso_ts, celsius), ...],
    `heat_intervals` = [(from_iso, to_iso, value), ...]."""
    return {
        "zoneType": "HEATING",
        "measuredData": {
            "measuringDeviceConnected": {"dataIntervals": [
                {"from": date_from, "to": date_from, "value": True}
            ]},
            "insideTemperature": {"dataPoints": [
                {"timestamp": ts, "value": {"celsius": c, "fahrenheit": c * 1.8 + 32}}
                for ts, c in temps
            ]},
            "humidity": {"dataPoints": [
                {"timestamp": ts, "value": 0.45} for ts, _ in temps
            ]},
        },
        "callForHeat": {"dataIntervals": [
            {"from": f, "to": t, "value": v} for f, t, v in heat_intervals
        ]},
    }


def test_parse_tado_cache_reads_temp_and_call_for_heat(tmp_path):
    day = _day_report(
        "2025-01-01T00:00:00.000Z",
        temps=[("2025-01-01T00:00:00.000Z", 19.0), ("2025-01-01T01:00:00.000Z", 18.5)],
        heat_intervals=[("2025-01-01T00:00:00.000Z", "2025-01-01T02:00:00.000Z", "NONE")],
    )
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"2025-01-01": day}), encoding="utf-8")

    samples = ia.parse_tado_cache(str(path))
    assert len(samples) == 2
    assert samples[0]["T_in"] == 19.0
    assert samples[0]["call_for_heat"] == "NONE"
    assert samples[0]["connected"] is True


def test_resample_hourly_marks_heating_off_only_when_every_sample_is_none():
    samples = [
        {"t": am.datetime(2025, 1, 1, 0, 0, tzinfo=am.timezone.utc), "T_in": 19.0,
         "humidity": 45.0, "call_for_heat": "NONE", "connected": True},
        {"t": am.datetime(2025, 1, 1, 0, 15, tzinfo=am.timezone.utc), "T_in": 18.9,
         "humidity": 45.0, "call_for_heat": "NONE", "connected": True},
        {"t": am.datetime(2025, 1, 1, 1, 0, tzinfo=am.timezone.utc), "T_in": 18.8,
         "humidity": 45.0, "call_for_heat": "NONE", "connected": True},
        {"t": am.datetime(2025, 1, 1, 1, 15, tzinfo=am.timezone.utc), "T_in": 19.2,
         "humidity": 45.0, "call_for_heat": "MEDIUM", "connected": True},
    ]
    hourly = ia.resample_hourly(samples)
    assert hourly["2025-01-01T00"]["heating_off"] is True
    assert hourly["2025-01-01T01"]["heating_off"] is False   # één MEDIUM-sample "besmet" het uur


def test_resample_hourly_off_when_device_disconnected():
    samples = [
        {"t": am.datetime(2025, 1, 1, 0, 0, tzinfo=am.timezone.utc), "T_in": 19.0,
         "humidity": None, "call_for_heat": "NONE", "connected": False},
    ]
    hourly = ia.resample_hourly(samples)
    assert hourly["2025-01-01T00"]["heating_off"] is False


# ── 2. Vrije-uitloop-regressie herstelt een bekende UA ──────────────────────────────

def _synthetic_room():
    # vol=40 m3, wall=12 m2, roof=0 → c_eff vastgelegd via airflow_model's eigen formule.
    return {"volume_m3": 40.0, "exterior_wall_m2": 12.0, "floor": 0}


def test_fit_room_ua_recovers_known_ua():
    house = {"rooms": {"testroom": _synthetic_room()}, "windows": {}}
    c_air0, c_mass0, _ = am.room_base_capacitances(house["rooms"]["testroom"])
    c_eff = c_air0 + c_mass0
    ua_true = 15.0                          # W/K
    k_true = ua_true * 3600.0 / c_eff        # 1/h

    n_hours = 240
    tado_hourly, weather_hourly, solar_hourly = {}, {}, {}
    t_in = 19.0
    for i in range(n_hours):
        key = f"2025-01-{1 + i // 24:02d}T{i % 24:02d}"
        t_out = 5.0 + 10.0 * math.sin(2 * math.pi * i / 24.0)
        solar = max(0.0, 400.0 * math.sin(2 * math.pi * (i % 24 - 6) / 24.0))
        weather_hourly[key] = {"T_out": t_out}
        solar_hourly[key] = solar
        tado_hourly[key] = {"T_in": t_in, "heating_off": True, "n_samples": 4}
        t_in = t_in + k_true * (t_out - t_in)   # expliciete Euler-stap, dt=1h, geen zonwinst

    fit = ia.fit_room_ua(house, "testroom", tado_hourly, weather_hourly, solar_hourly)

    assert fit["status"] == "ok"
    assert fit["n_pairs"] > ia.MIN_PAIRS
    assert not fit["solar_dropped"]
    assert _rel_err(fit["k_per_h"], k_true) < 0.15
    assert _rel_err(fit["ua_w_per_k"], ua_true) < 0.15


def test_fit_room_ua_falls_back_when_solar_column_is_degenerate():
    # Geen enkele variatie in de zon-input (bv. een raamloze kamer of een weerbron
    # zonder instraling) → de 3-variabelen-fit is singulier. Moet terugvallen op
    # k + constante i.p.v. helemaal te falen (zie insulation_analysis.py::fit_room_ua).
    house = {"rooms": {"testroom": _synthetic_room()}, "windows": {}}
    c_air0, c_mass0, _ = am.room_base_capacitances(house["rooms"]["testroom"])
    c_eff = c_air0 + c_mass0
    ua_true = 10.0
    k_true = ua_true * 3600.0 / c_eff

    n_hours = 240
    tado_hourly, weather_hourly, solar_hourly = {}, {}, {}
    t_in = 19.0
    for i in range(n_hours):
        key = f"2025-01-{1 + i // 24:02d}T{i % 24:02d}"
        t_out = 5.0 + 10.0 * math.sin(2 * math.pi * i / 24.0)
        weather_hourly[key] = {"T_out": t_out}
        solar_hourly[key] = 0.0                 # geen variatie → singuliere zon-kolom
        tado_hourly[key] = {"T_in": t_in, "heating_off": True, "n_samples": 4}
        t_in = t_in + k_true * (t_out - t_in)

    fit = ia.fit_room_ua(house, "testroom", tado_hourly, weather_hourly, solar_hourly)
    assert fit["status"] == "ok"
    assert fit["solar_dropped"] is True
    assert fit["solar_coef"] == 0.0
    assert _rel_err(fit["k_per_h"], k_true) < 0.15


def test_is_night_local_matches_known_local_hours():
    # Winter (Europe/Amsterdam = UTC+1): 22:00 UTC = 23:00 lokaal (nacht-start).
    assert ia._is_night_local(am.datetime(2025, 1, 15, 22, 0, tzinfo=am.timezone.utc)) is True
    # 04:00 UTC = 05:00 lokaal (nog nacht, < NIGHT_END_H).
    assert ia._is_night_local(am.datetime(2025, 1, 15, 4, 0, tzinfo=am.timezone.utc)) is True
    # 05:00 UTC = 06:00 lokaal (net geen nacht meer, NIGHT_END_H is exclusief).
    assert ia._is_night_local(am.datetime(2025, 1, 15, 5, 0, tzinfo=am.timezone.utc)) is False
    # 11:00 UTC = 12:00 lokaal (middag, overduidelijk geen nacht).
    assert ia._is_night_local(am.datetime(2025, 1, 15, 11, 0, tzinfo=am.timezone.utc)) is False
    # Zomer (UTC+2, DST): 21:00 UTC = 23:00 lokaal.
    assert ia._is_night_local(am.datetime(2025, 7, 15, 21, 0, tzinfo=am.timezone.utc)) is True
    assert ia._is_night_local(am.datetime(2025, 7, 15, 12, 0, tzinfo=am.timezone.utc)) is False


def test_build_pairs_night_only_keeps_only_night_hour_pairs():
    start = am.datetime(2025, 1, 1, 0, 0, tzinfo=am.timezone.utc)
    keys, tado_hourly, weather_hourly, solar_hourly = [], {}, {}, {}
    for i in range(72):                       # 3 volle dagen
        t = start + am.timedelta(hours=i)
        key = t.strftime("%Y-%m-%dT%H")
        keys.append(key)
        tado_hourly[key] = {"T_in": 19.0, "heating_off": True, "n_samples": 4}
        weather_hourly[key] = {"T_out": 8.0}
        solar_hourly[key] = 0.0

    all_pairs = ia._build_pairs(keys, tado_hourly, weather_hourly, solar_hourly, night_only=False)
    night_pairs = ia._build_pairs(keys, tado_hourly, weather_hourly, solar_hourly, night_only=True)
    assert len(night_pairs) < len(all_pairs)
    # 7 nachtelijke uren (23,0..5) per etmaal → 6 opeenvolgende paren per nacht, 3 nachten
    # in dit bereik zijn (deels) gedekt; in elk geval > 0 en ruim onder het dag-totaal.
    assert 0 < len(night_pairs) <= 6 * 3


def test_fit_room_ua_night_only_recovers_known_ua():
    house = {"rooms": {"testroom": _synthetic_room()}, "windows": {}}
    c_air0, c_mass0, _ = am.room_base_capacitances(house["rooms"]["testroom"])
    c_eff = c_air0 + c_mass0
    ua_true = 12.0
    k_true = ua_true * 3600.0 / c_eff

    start = am.datetime(2025, 1, 1, 0, 0, tzinfo=am.timezone.utc)
    tado_hourly, weather_hourly, solar_hourly = {}, {}, {}
    t_in = 19.0
    n_hours = 24 * 30                          # 30 dagen
    for i in range(n_hours):
        t = start + am.timedelta(hours=i)
        key = t.strftime("%Y-%m-%dT%H")
        t_out = 5.0 + 10.0 * math.sin(2 * math.pi * i / 24.0)
        weather_hourly[key] = {"T_out": t_out}
        solar_hourly[key] = max(0.0, 400.0 * math.sin(2 * math.pi * (i % 24 - 6) / 24.0))
        tado_hourly[key] = {"T_in": t_in, "heating_off": True, "n_samples": 4}
        t_in = t_in + k_true * (t_out - t_in)   # geen zonwinst in de synthetische fysica

    night_fit = ia.fit_room_ua(house, "testroom", tado_hourly, weather_hourly, solar_hourly,
                               night_only=True)
    assert night_fit["status"] == "ok"
    assert night_fit["solar_dropped"] is True   # zon is 's nachts per definitie ~0 → ontaard
    assert night_fit["n_pairs"] > ia.MIN_PAIRS
    assert _rel_err(night_fit["k_per_h"], k_true) < 0.15
    assert _rel_err(night_fit["ua_w_per_k"], ua_true) < 0.15


def test_fit_room_ua_insufficient_data_reports_status():
    house = {"rooms": {"testroom": _synthetic_room()}, "windows": {}}
    tado_hourly = {"2025-01-01T00": {"T_in": 19.0, "heating_off": True, "n_samples": 4}}
    weather_hourly = {"2025-01-01T00": {"T_out": 5.0}}
    fit = ia.fit_room_ua(house, "testroom", tado_hourly, weather_hourly, {})
    assert fit["status"] == "insufficient_data"


def test_fit_room_ua_excludes_heating_on_pairs():
    house = {"rooms": {"testroom": _synthetic_room()}, "windows": {}}
    # Twee opeenvolgende uren, allebei stokend (heating_off False) — mag niet meetellen,
    # ook al is het delta-T-signaal aanwezig.
    tado_hourly = {
        "2025-01-01T00": {"T_in": 15.0, "heating_off": False, "n_samples": 4},
        "2025-01-01T01": {"T_in": 20.0, "heating_off": False, "n_samples": 4},
    }
    weather_hourly = {
        "2025-01-01T00": {"T_out": 5.0},
        "2025-01-01T01": {"T_out": 5.0},
    }
    fit = ia.fit_room_ua(house, "testroom", tado_hourly, weather_hourly, {})
    assert fit["status"] == "insufficient_data"
    assert fit["n_pairs"] == 0


# ── 3. Geometrie-narratief ───────────────────────────────────────────────────────────

def test_facade_label_recognises_known_orientations():
    assert "straatzijde" in ia._facade_label(309.0)
    assert "tuinzijde" in ia._facade_label(129.0)
    assert "zijgevel" in ia._facade_label(219.0)
    assert ia._facade_label(45.0) == "45°"


def test_room_geometry_summary_on_real_house():
    house = am.load_house()
    geom = ia.room_geometry_summary(house, "living")
    assert geom["exterior_wall_m2"] == 46
    assert geom["window_area_m2"] > 0
    assert geom["window_wall_ratio"] > 0
    assert geom["has_roof"] is False


def test_room_geometry_summary_flags_roof_room():
    house = am.load_house()
    geom = ia.room_geometry_summary(house, "office")
    assert geom["has_roof"] is True
    assert geom["roof_m2"] == 14


def test_online_ua_estimate_uses_priors_when_no_learning():
    house = am.load_house()
    params = am.default_params(house)
    est = ia.online_ua_estimate(house, params, "office")
    # Bij prior-schalen (alles 1.0) is ua_total > 0 zodra roof_m2 > 0.
    assert est["ua_roof_w_per_k"] > 0
    assert est["ua_total_w_per_k"] == round(est["ua_env_w_per_k"] + est["ua_roof_w_per_k"], 2)


def test_build_narrative_reports_insufficient_data():
    msg = ia.build_narrative({"status": "insufficient_data", "n_pairs": 3},
                             {"window_wall_ratio": None, "has_roof": False}, None, None, 0)
    assert "Te weinig" in msg


def test_build_narrative_prefers_night_fit_for_online_comparison():
    fit = {"status": "ok", "ua_w_per_k": 9.7, "ua_per_m2": 0.21, "solar_dropped": False}
    night_fit = {"status": "ok", "ua_w_per_k": 20.0, "n_pairs": 500}
    geom = {"window_wall_ratio": 0.4, "dominant_facade_label": "tuinzijde", "has_roof": False}
    online_cmp = {"ua_total_w_per_k": 23.3}
    msg = ia.build_narrative(fit, geom, online_cmp, 1, 1, night_fit=night_fit)
    assert "nachtfit" in msg
    assert "Komt overeen" in msg          # 20.0 / 23.3 valt binnen de 0.7-1.4 band
    assert "'s Nachts" in msg


def test_build_narrative_falls_back_to_day_fit_without_night_fit():
    fit = {"status": "ok", "ua_w_per_k": 9.7, "ua_per_m2": 0.21, "solar_dropped": False}
    geom = {"window_wall_ratio": 0.4, "dominant_facade_label": "tuinzijde", "has_roof": False}
    online_cmp = {"ua_total_w_per_k": 23.3}
    msg = ia.build_narrative(fit, geom, online_cmp, 1, 1,
                             night_fit={"status": "insufficient_data", "n_pairs": 5})
    assert "jaarfit" in msg
    assert "Wijkt af" in msg              # 9.7 / 23.3 valt buiten de 0.7-1.4 band

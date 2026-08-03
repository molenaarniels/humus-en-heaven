"""Regressie-vangnet voor `vent_twin.py` — de runner/dashboard-bouwer van de herbouwde
ventilatie-tweeling (Project 13).

Kern: het slanke vent_data.json-schema is het contract waar de dashboard-JS op leunt —
elk veld dat de vijf features lezen moet er zijn (en de bewust geschrapte velden juist
níet). Verder: de leercurve groeit met precies één punt per run, _controls filtert vast
glas/vaste deuren, de nudge-teksten, en window_weather_summary.

main() doet netwerk-I/O en wordt hier bewust nooit aangeroepen — build_dashboard en de
pure helpers zijn de testbare laag.
"""
from datetime import datetime, timedelta

import pytest

import vent_fit as vf
import vent_io as vio
import vent_physics as vp
import vent_twin as vt

TZ = vio.TZ
NOW = datetime(2026, 8, 1, 15, 0, tzinfo=TZ)


# ── Fixtures: klein-maar-geldig dashboard-bouwsel op het échte huismodel ─────────────

def _weather_fixture() -> dict:
    """Deterministisch weer in fetch_weather-vorm: 80u terug + 6u vooruit (zelfde stijl
    als tests/test_vent_parity.py)."""
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
           "wind_gusts_10m": 5.0, "shortwave_radiation": 430.0}
    return {"hourly": rows, "current": cur, "wu_solar_scale": None}


@pytest.fixture(scope="module")
def setup():
    house = vio.load_house()
    weather = _weather_fixture()
    ctx = vio.make_context(house, weather, NOW)
    params = vio.default_params(house)
    timeline = vio.build_timeline(house, weather, [], NOW, 6.0, ctx)
    seed = {rid: 23.0 + 0.2 * k for k, rid in enumerate(house["rooms"])}
    sim = vp.simulate(house, params, timeline, seed, ctx,
                      calib_only_rooms=set(house["rooms"]), snapshot_t=NOW)
    wd = {"rooms": {"Ted": {"inside": 22.4, "humidity": 55}}}
    actual = {"ted": [(NOW - timedelta(hours=1), 22.0), (NOW, 22.4)]}
    return {"house": house, "weather": weather, "ctx": ctx, "params": params,
            "timeline": timeline, "sim": sim, "wd": wd, "actual": actual}


def _dash(s, **kw):
    args = dict(learned={}, rmse_now=0.42, skill=0.5, rmse_baseline=0.6,
                ac_room="office", heat_now={"ted": True},
                paused_now=False, paused_since=None, log=[])
    args.update(kw)
    return vt.build_dashboard(s["house"], s["params"], s["weather"], s["wd"],
                              s["timeline"], s["sim"], args.pop("learned"),
                              s["actual"], NOW, args.pop("rmse_now"), s["ctx"],
                              args.pop("log"), **args)


@pytest.fixture(scope="module")
def dash(setup):
    return _dash(setup)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ── 1. Schema-volledigheid: het contract waar de dashboard-JS op leunt ───────────────

def test_dashboard_schema_toplevel(dash):
    verwacht = {"generated_at", "as_of_local", "source", "model_version", "weather",
                "openings", "controls", "paused", "paused_since", "ac", "rooms",
                "flows", "learned", "house_meta"}
    assert verwacht <= set(dash)
    assert dash["source"] == "vent_twin"
    assert isinstance(dash["generated_at"], str) and dash["generated_at"]
    assert dash["as_of_local"] == NOW.isoformat()
    assert isinstance(dash["model_version"], str) and dash["model_version"]
    assert dash["paused"] is False
    assert dash["paused_since"] is None
    assert dash["openings"] == {}                     # lege log → geen gemelde standen
    assert isinstance(dash["controls"], list) and dash["controls"]
    # ac-blok: huidige kamer + de keuzelijst voor de modal (alleen sensorkamers).
    assert dash["ac"]["room"] == "office"
    ac_ids = {r["id"] for r in dash["ac"]["rooms"]}
    assert ac_ids == {"living", "ted", "hotties", "office", "bath"}   # géén stair/hall
    assert all({"id", "label"} <= set(r) for r in dash["ac"]["rooms"])


def test_dashboard_schema_weather_blok(dash):
    w = dash["weather"]
    verwacht = {"outside_temp", "outside_humidity", "outside_source", "wind_speed",
                "wind_dir", "gust", "sun_az", "sun_el", "neighbor_temp", "ground_temp",
                "wu_solar_scale"}
    assert verwacht <= set(w)
    assert w["outside_temp"] == pytest.approx(26.4)
    assert w["outside_humidity"] == pytest.approx(52.0)
    assert w["outside_source"] == "open-meteo"        # geen WU-verfijning in de fixture
    assert _num(w["wind_speed"]) and _num(w["wind_dir"]) and _num(w["gust"])
    assert _num(w["sun_az"]) and _num(w["sun_el"])
    assert _num(w["neighbor_temp"]) and _num(w["ground_temp"])
    assert w["wu_solar_scale"] is None                # fixture zonder WU-zon


def test_dashboard_schema_kamers(setup, dash):
    room_keys = {"label", "actual_temp", "predicted_temp", "predicted_air_temp",
                 "sensor_outdoor_frac", "predicted_mass_temp", "error", "humidity",
                 "ach", "solar_w", "solar_by_window", "env_w", "vent_w",
                 "trend_c_per_h", "comfort_low", "comfort_high", "ac", "heating",
                 "paused", "predicted_series", "actual_series", "params"}
    geschrapt = {"from_window_data", "party_w", "ground_w", "internal_w",
                 "ac_excluded_samples"}
    assert set(dash["rooms"]) == set(setup["house"]["rooms"])
    for rid, room in dash["rooms"].items():
        assert room_keys <= set(room), rid
        assert not geschrapt & set(room), rid          # bewust geschrapte velden blijven weg
        assert isinstance(room["label"], str)
        assert _num(room["predicted_temp"]) and _num(room["predicted_air_temp"])
        assert _num(room["predicted_mass_temp"]) and _num(room["ach"])
        assert _num(room["solar_w"]) and _num(room["env_w"]) and _num(room["vent_w"])
        assert _num(room["trend_c_per_h"]) and _num(room["sensor_outdoor_frac"])
        assert isinstance(room["ac"], bool) and isinstance(room["heating"], bool)
        assert isinstance(room["paused"], bool)
        assert isinstance(room["solar_by_window"], dict)
        for wentry in room["solar_by_window"].values():
            assert {"label", "w"} <= set(wentry)
        assert isinstance(room["predicted_series"], list) and room["predicted_series"]
        assert {"t", "temp"} <= set(room["predicted_series"][0])
        assert isinstance(room["actual_series"], list)
        assert isinstance(room["params"], dict)
    # De chips volgen de doorgegeven staten.
    assert dash["rooms"]["office"]["ac"] is True and dash["rooms"]["ted"]["ac"] is False
    assert dash["rooms"]["ted"]["heating"] is True
    assert dash["rooms"]["living"]["heating"] is False
    # Ted heeft wd-data → actual_temp/humidity/error gevuld; comfortband uit ROOM_COMFORT.
    ted = dash["rooms"]["ted"]
    assert ted["actual_temp"] == pytest.approx(22.4)
    assert ted["humidity"] == 55
    assert _num(ted["error"])
    assert ted["comfort_low"] == pytest.approx(17.0)
    assert ted["comfort_high"] == pytest.approx(18.0)
    assert len(ted["actual_series"]) == 2


def test_dashboard_schema_koker_stratificatie(dash):
    strat_keys = {"stair_gradient_c_per_m", "stair_crown_c", "predicted_temp_top",
                  "predicted_temp_bottom", "stair_pin_error_c", "stair_gamma_w"}
    stair = dash["rooms"]["stair"]
    assert strat_keys <= set(stair)
    assert _num(stair["stair_gradient_c_per_m"]) and stair["stair_gradient_c_per_m"] >= 0.0
    assert _num(stair["stair_crown_c"]) and stair["stair_crown_c"] >= 0.0
    assert _num(stair["predicted_temp_top"]) and _num(stair["predicted_temp_bottom"])
    assert stair["predicted_temp_top"] >= stair["predicted_temp_bottom"]
    assert isinstance(stair["stair_pin_error_c"], dict)
    assert isinstance(stair["stair_gamma_w"], dict)
    # Een gewone kamer draagt de koker-velden níet.
    assert not strat_keys & set(dash["rooms"]["living"])


def test_dashboard_schema_learned_blok(dash):
    ln = dash["learned"]
    verwacht = {"params", "rmse", "rmse_naive", "skill", "railed", "rmse_history",
                "held", "paused"}
    assert verwacht <= set(ln)
    assert ln["rmse"] == pytest.approx(0.42)
    assert ln["rmse_naive"] == pytest.approx(0.6)
    assert ln["skill"] == 0.5
    assert isinstance(ln["railed"], list)
    assert isinstance(ln["rmse_history"], list) and len(ln["rmse_history"]) == 1
    assert ln["held"] is False and ln["paused"] is False
    assert isinstance(ln["params"], dict)


def test_dashboard_schema_house_meta_en_flows(setup, dash):
    hm = dash["house_meta"]
    assert {"rooms", "junctions", "windows", "vents", "doors", "sim"} <= set(hm)
    assert set(hm["rooms"]) == set(setup["house"]["rooms"])
    assert set(hm["junctions"]) == set(setup["house"]["junctions"])
    assert set(hm["windows"]) == set(setup["house"]["windows"])
    assert set(hm["vents"]) == set(setup["house"]["vents"])
    assert set(hm["doors"]) == set(setup["house"]["doors"])
    assert hm["sim"] == {"leak_area": vp.LEAK_AREA, "dp_lam": vp.DP_LAM,
                         "wind_ref_z": vp.WIND_REF_Z}
    # Speeltuin-geometrie: elk raam/rooster draagt zijn opgeloste effectieve-openingsfactor.
    assert all("eff_open_frac" in w for w in hm["windows"].values())
    assert all("eff_open_frac" in v for v in hm["vents"].values())
    # Flows: het slanke per-opening-schema, zónder area-veld en zónder de infiltratielekken.
    assert isinstance(dash["flows"], list) and dash["flows"]
    for f in dash["flows"]:
        assert set(f) == {"id", "a", "b", "flow_m3s"}
        assert "area" not in f
        assert not f["id"].startswith("_leak_")
        assert _num(f["flow_m3s"])


# ── 2. Leercurve: precies één punt per run, geen punt op NaN ─────────────────────────

def test_rmse_history_groeit_met_precies_een_punt(setup):
    oud = {"t": (NOW - timedelta(hours=1)).isoformat(), "rmse": 0.5,
           "held": False, "paused": False, "version": "abc1234"}
    d = _dash(setup, learned={"rmse_history": [oud]}, rmse_now=0.7)
    hist = d["learned"]["rmse_history"]
    assert len(hist) == 2
    assert hist[0] == oud                              # bestaande punten onaangeroerd
    nieuw = hist[-1]
    # Kernvelden altijd aanwezig; skill/rmse_naive/wx zijn additieve stempels (voeden
    # vent_diagnostics' regime-analyse) en mogen erbij zitten wanneer ze bekend zijn.
    assert {"t", "rmse", "held", "paused", "version"} <= set(nieuw)
    assert set(nieuw) <= {"t", "rmse", "held", "paused", "version",
                          "skill", "rmse_naive", "wx"}
    assert nieuw["t"] == NOW.isoformat()
    assert nieuw["rmse"] == pytest.approx(0.7)
    assert nieuw["held"] is False and nieuw["paused"] is False
    assert isinstance(nieuw["version"], str) and nieuw["version"]


def test_rmse_history_nan_appends_niets(setup):
    oud = {"t": (NOW - timedelta(hours=1)).isoformat(), "rmse": 0.5,
           "held": False, "paused": False, "version": "abc1234"}
    d = _dash(setup, learned={"rmse_history": [oud]}, rmse_now=float("nan"),
              rmse_baseline=float("nan"), skill=None)
    assert d["learned"]["rmse_history"] == [oud]       # geen punt op NaN
    assert d["learned"]["rmse"] is None                # en de kop-RMSE is eerlijk leeg
    assert d["learned"]["rmse_naive"] is None


def test_paused_run_stempelt_overal(setup):
    since = NOW - timedelta(hours=2)
    d = _dash(setup, rmse_now=0.9, paused_now=True, paused_since=since,
              learning_held=True)
    assert d["paused"] is True
    assert d["paused_since"] == since.isoformat()
    assert d["learned"]["paused"] is True and d["learned"]["held"] is True
    assert all(r["paused"] is True for r in d["rooms"].values())
    punt = d["learned"]["rmse_history"][-1]
    assert punt["paused"] is True and punt["held"] is True


# ── 3. _controls: wat de modal mag bedienen ──────────────────────────────────────────

def test_controls_filtert_vast_glas_en_vaste_deuren(setup):
    house = setup["house"]
    controls = vt._controls(house, {})
    per_kind = {}
    for c in controls:
        assert {"id", "kind", "label", "state"} <= set(c)
        per_kind.setdefault(c["kind"], set()).add(c["id"])
    # Vast glas (max_open_area_m2 ≤ 0) is niet bedienbaar → geen window-control.
    assert "living_skylight" not in per_kind["window"]
    assert "ted_window" not in per_kind["window"]
    assert "ted_small_window" in per_kind["window"]
    vaste_ramen = {wid for wid, w in house["windows"].items()
                   if w.get("max_open_area_m2", 0.0) <= 0.0}
    assert not vaste_ramen & per_kind["window"]
    # Alle roosters zijn bedienbaar.
    assert per_kind["vent"] == set(house["vents"])
    # De vaste doorgang (fixed) is geen deur → niet tonen.
    assert "hall_stair" not in per_kind["door"]
    assert per_kind["door"] == {did for did, d in house["doors"].items() if not d.get("fixed")}
    # Default-standen zonder meldingen: raam dicht, rooster open.
    by_id = {c["id"]: c for c in controls}
    assert by_id["ted_small_window"]["state"] == "dicht"
    assert next(c["state"] for c in controls if c["kind"] == "vent") == "open"


def test_controls_zonwering_met_suffix(setup):
    house = setup["house"]
    controls = vt._controls(house, {"office_window_shade": "dicht"})
    shades = {c["id"]: c for c in controls if c["kind"] == "shade"}
    verwacht = {wid + "_shade" for wid, w in house["windows"].items() if w.get("shade")}
    assert set(shades) == verwacht
    assert all(sid.endswith("_shade") for sid in shades)
    # Ook zonwering op vast glas is bedienbaar (het glas niet, het scherm wél).
    assert "living_skylight_shade" in shades
    # Gemelde stand komt door; niet-gemelde valt terug op "open".
    assert shades["office_window_shade"]["state"] == "dicht"
    assert shades["ted_window_shade"]["state"] == "open"


# ── 4. Nudge-teksten ─────────────────────────────────────────────────────────────────

def test_anomaly_nudge_text_contents(monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    txt = vt.anomaly_nudge_text(1.92, 0.65)
    assert "1.92" in txt and "0.65" in txt
    assert "raam" in txt.lower()                        # de vraag die de log herstelt
    assert "vent.html" not in txt                       # geen URL zonder DASHBOARD_URL
    monkeypatch.setenv("DASHBOARD_URL", "https://x.test/dash/")
    txt = vt.anomaly_nudge_text(1.92, None)
    assert "https://x.test/dash/vent.html" in txt
    assert "norm" not in txt                            # geen norm-tekst zonder baseline


def test_anomaly_resume_text_contents():
    txt = vt.anomaly_resume_text(0.9)
    assert "0.90" in txt                                # de actuele fout, twee decimalen
    assert f"{vf.ANOMALY_MAX_HOLD_H:.0f}" in txt        # de ontsnappings-drempel
    assert "hervat" in txt.lower()


# ── 5. window_weather_summary ────────────────────────────────────────────────────────

def test_window_weather_summary_stamps_window_only():
    now = datetime(2026, 6, 17, 12, 0, tzinfo=TZ)
    rows = [{"dt": now - timedelta(hours=h), "T_out": 20.0 + h % 5, "shortwave": 100.0 * (h % 4)}
            for h in range(0, 30)]
    rows.append({"dt": now - timedelta(hours=72), "T_out": 99.0, "shortwave": 9999.0})  # buiten venster
    wx = vt.window_weather_summary({"hourly": rows}, now, window_h=24.0)
    assert wx["tmax"] <= 24.0 and wx["tmax"] != 99.0        # de oude rij telt niet mee
    assert 0 <= wx["solar_mean"] <= wx["solar_peak"] <= 300


def test_window_weather_summary_leeg():
    now = datetime(2026, 6, 17, 12, 0, tzinfo=TZ)
    assert vt.window_weather_summary({"hourly": []}, now, window_h=24.0) == {}
    # Alleen rijen buiten het venster (of zonder dt) → ook leeg.
    rows = [{"dt": now - timedelta(hours=100), "T_out": 20.0, "shortwave": 50.0},
            {"dt": None, "T_out": 21.0, "shortwave": 60.0}]
    assert vt.window_weather_summary({"hourly": rows}, now, window_h=24.0) == {}

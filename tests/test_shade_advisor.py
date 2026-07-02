"""Pure-logica-tests voor de zonwering-adviseur (`shade_advisor.py`, Project 9).

Geen netwerk, geen Gist: synthetische zonnige-dag-drivers + een fixture-huis met
de twee shade-typen (simpel scherm + coverage-lamella). De reminder-stand wordt
getest met gemonkeypatchte weer/log-seams en een tmp-state-bestand.
"""
import json
import math
from datetime import datetime, timedelta

import pytest

import airflow_model as am
import shade_advisor as sa
from shared_const import TZ

LAT, LON = 52.09, 5.12

HOUSE = {
    "location": {"lat": LAT, "lon": LON},
    "rooms": {"r": {}},
    "windows": {
        "sky": {"room": "r", "label": "Serre-dakraam (test) — buitenscherm",
                "facade_azimuth_deg": 129.0, "glass_m2": 3.0, "tilt_deg": 15.0,
                "shading": "none",
                "shade": {"factor": 0.15, "label": "Buitenscherm"}},
        "lam": {"room": "r", "label": "Straatraam (lamella)",
                "facade_azimuth_deg": 309.0, "glass_m2": 6.0, "tilt_deg": 90.0,
                "shading": "none",
                "shade": {"coverage": {"open": 0.3, "half": 0.5, "dicht": 1.0},
                          "paper": 0.7, "default": "open", "label": "Paper-lamella"}},
        "plain": {"room": "r", "label": "Vast raam", "facade_azimuth_deg": 129.0,
                  "glass_m2": 1.0, "tilt_deg": 90.0, "shading": "none"},
    },
}


def _sunny_rows(now, t_max=27.0):
    """Synthetische heldere dag rond `now`: sinus-instraling, dag-max instelbaar."""
    t0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for h in range(48):
        t = t0 + timedelta(hours=h)
        sun = max(0.0, math.sin(math.pi * (t.hour - 5.5) / 15.5))
        rows.append({"dt": t, "T_out": (t_max - 9.0) + 9.0 * sun, "rh": 50,
                     "precip": 0.0, "wind_speed": 2.0, "wind_dir": 200.0, "gust": 3.0,
                     "shortwave": 850 * sun, "direct": 650 * sun, "diffuse": 200 * sun})
    return rows


NOW = datetime(2026, 7, 2, 8, 15, tzinfo=TZ)
NOON = datetime(2026, 7, 2, 13, 0, tzinfo=TZ)


# ── Vermijdbare winst per shade-type ─────────────────────────────────────────────────

def test_avoidable_delta_simple_factor():
    # Simpel scherm (factor 0.15): dicht laat 15% door → vermijdbaar = 85% van open.
    open_pw = am.per_window_solar(HOUSE, {}, *am.sun_position(LAT, LON, NOON),
                                  600.0, 150.0, beam_iam=True)
    dw = sa._instant_delta(HOUSE, {}, "sky", NOON, LAT, LON, 600.0, 150.0)
    assert open_pw["sky"] > 100.0                 # er staat echt zon op
    assert dw == pytest.approx(0.85 * open_pw["sky"], rel=1e-9)


def test_avoidable_delta_coverage_lamella():
    # Coverage-lamella: huidige stand "open" (dekking 0.3, papier 0.7) laat
    # 1−0.3·0.3 = 0.91 door; dicht 1−1.0·0.3 = 0.70 → Δfractie 0.21 van kaal glas,
    # oftewel 0.21/0.91 van de húidige doorval (dicht-vs-huidige-stand, niet vs kaal).
    s_az, s_el = am.sun_position(LAT, LON, datetime(2026, 7, 2, 18, 30, tzinfo=TZ))
    cur = am.per_window_solar(HOUSE, {}, s_az, s_el, 400.0, 150.0, beam_iam=True)
    dw = sa._instant_delta(HOUSE, {}, "lam",
                           datetime(2026, 7, 2, 18, 30, tzinfo=TZ), LAT, LON,
                           400.0, 150.0)
    assert cur["lam"] > 50.0
    assert dw == pytest.approx(cur["lam"] * 0.21 / 0.91, rel=1e-9)


def test_close_interval_with_hysteresis():
    t0 = NOW
    vals = [50, 100, 160, 200, 120, 90, 70, 40]
    series = [(t0 + timedelta(minutes=15 * i), float(v)) for i, v in enumerate(vals)]
    span = sa.close_interval(series)
    assert span["start"] == t0 + timedelta(minutes=30)   # eerste ≥150
    assert span["end"] == t0 + timedelta(minutes=75)     # laatste ≥80 (hysterese)
    assert span["wh"] == pytest.approx((160 + 200 + 120 + 90) * 0.25)


def test_close_interval_picks_biggest_peak():
    t0 = NOW
    vals = [200, 60, 60, 300, 300, 60]                   # twee pieken, tweede groter
    series = [(t0 + timedelta(minutes=15 * i), float(v)) for i, v in enumerate(vals)]
    span = sa.close_interval(series)
    assert span["start"] == t0 + timedelta(minutes=45)


# ── Dagplan ──────────────────────────────────────────────────────────────────────────

def test_plan_on_warm_sunny_day():
    plan = sa.build_plan(HOUSE, _sunny_rows(NOW), {}, NOW, LAT, LON)
    assert plan["warm_day"]
    ids = [w["id"] for w in plan["windows"]]
    assert "sky" in ids                                  # ZO-dakraam met veel glas
    assert "plain" not in ids                            # geen bedienbare zonwering
    sky = next(w for w in plan["windows"] if w["id"] == "sky")
    assert sky["delta_wh"] >= sa.SHADE_MIN_DELTA_WH
    assert sky["close"] < sky["open"]
    # Berichtformaat: labels + tijden + kWh, ruim onder de Telegram-limiet.
    msg = sa.plan_message(plan, NOW)
    assert "Serre-dakraam" in msg and "buitenscherm" in msg
    assert "kWh" in msg and "herinnering" in msg
    assert len(msg) < 4096


def test_cold_day_gates_plan():
    plan = sa.build_plan(HOUSE, _sunny_rows(NOW, t_max=18.0), {}, NOW, LAT, LON)
    assert not plan["warm_day"]
    assert plan["windows"] == []


def test_already_closed_shade_excluded():
    states = {"sky_shade": "dicht"}
    plan = sa.build_plan(HOUSE, _sunny_rows(NOW), states, NOW, LAT, LON)
    assert "sky" not in [w["id"] for w in plan["windows"]]


# ── Reminder-stand: idempotentie + materialisatie ────────────────────────────────────

@pytest.fixture
def state_env(tmp_path, monkeypatch):
    path = tmp_path / "shade_state.json"
    monkeypatch.setattr(sa, "STATE_FILE", str(path))
    sent = []
    monkeypatch.setattr(sa, "_send", lambda msg: sent.append(msg))
    return path, sent


def _write_state(path, **over):
    state = {"date": NOW.date().isoformat(), "plan_sent": True,
             "reminder_at": "09:30", "reminder_sent": False,
             "windows": [{"id": "sky", "label": "Serre-dakraam",
                          "shade_label": "buitenscherm", "room": "r",
                          "close": "09:30", "open": "19:00", "delta_wh": 2100}]}
    state.update(over)
    path.write_text(json.dumps(state))
    return state


def test_reminder_noop_before_time_and_when_sent(state_env, monkeypatch):
    path, sent = state_env
    _write_state(path)
    sa.run_reminder(NOW)                                 # 08:15 < 09:30 → wachten
    assert sent == [] and not sa.load_state()["reminder_sent"]
    _write_state(path, reminder_sent=True)
    sa.run_reminder(NOON)                                # al gestuurd → no-op
    assert sent == []


def test_reminder_materialization_hold_then_send(state_env, monkeypatch):
    path, sent = state_env
    _write_state(path)
    monkeypatch.setattr(am, "load_house", lambda: HOUSE)
    monkeypatch.setattr(am, "load_openings_log", lambda: [])
    # Bewolkt: geen instraling → uitstel, niet als verzonden gemarkeerd.
    monkeypatch.setattr(am, "fetch_weather", lambda: {
        "hourly": [], "current": {"direct_radiation": 0.0, "shortwave_radiation": 10.0}})
    sa.run_reminder(NOON)
    assert sent == [] and not sa.load_state()["reminder_sent"]
    # Zon breekt door: voorspelde last materialiseert → één bericht + sent-vlag.
    monkeypatch.setattr(am, "fetch_weather", lambda: {
        "hourly": [], "current": {"direct_radiation": 600.0, "shortwave_radiation": 800.0}})
    sa.run_reminder(NOON)
    assert len(sent) == 1 and "Serre-dakraam" in sent[0]
    assert sa.load_state()["reminder_sent"]
    sa.run_reminder(NOON)                                # idempotent
    assert len(sent) == 1


def test_reminder_gives_up_after_window(state_env, monkeypatch):
    path, sent = state_env
    _write_state(path, windows=[{"id": "sky", "label": "Serre-dakraam",
                                 "shade_label": "buitenscherm", "room": "r",
                                 "close": "09:30", "open": "11:00", "delta_wh": 900}])
    monkeypatch.setattr(am, "load_house", lambda: HOUSE)
    sa.run_reminder(NOON)                                # 13:00 > open 11:00 → opgeven
    assert sent == [] and sa.load_state()["reminder_sent"]


def test_state_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    monkeypatch.setattr(sa, "STATE_FILE", str(path))
    sa.save_state({"date": "2026-07-02", "plan_sent": True})
    assert sa.load_state() == {"date": "2026-07-02", "plan_sent": True}
    monkeypatch.setattr(sa, "STATE_FILE", str(tmp_path / "afwezig.json"))
    assert sa.load_state() == {}

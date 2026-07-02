"""Pure-logica-tests voor het Weekjournaal (`weekjournaal.py`, Project 11).

Geen netwerk of bestanden (op env-pad-overrides via tmp_path na): elke sectie
krijgt een minimale inline-fixture en moet zelfstandig degraderen (None) als
haar artefact ontbreekt of onbruikbaar is.
"""
from datetime import date, datetime, timedelta

import weekjournaal as wj
from shared_const import TZ

TODAY = date(2026, 7, 5)                 # een zondag
NOW = datetime(2026, 7, 5, 20, 0, tzinfo=TZ)


def _soil():
    days = []
    for i in range(10):                  # 10 dagen, jongste = TODAY
        d = TODAY - timedelta(days=9 - i)
        days.append({"date": d.isoformat(), "forecast": False, "precip": 1.0,
                     "ET0": 3.0, "lawn_irrigation": 2.0, "shrubs_irrigation": 1.0,
                     "Tmax": 20.0 + i, "Tmax_corr": None})
    # Forecast-dagen mogen nooit meetellen.
    days.append({"date": (TODAY + timedelta(days=1)).isoformat(), "forecast": True,
                 "precip": 99.0, "ET0": 99.0, "Tmax": 99.0})
    return {"days": days,
            "lawn_status": {"state": "wet", "depletion_pct": 16.8},
            "shrubs_status": {"state": "moist", "depletion_pct": 28.7}}


def test_garden_section_week_window():
    s = wj.garden_section(_soil(), TODAY)
    assert "7.0 mm regen" in s           # 7 dagen × 1.0, forecast-dag uitgesloten
    assert "14 mm gesproeid" in s and "(gazon)" in s
    assert "ET 21 mm" in s
    assert "nat" in s and "vochtig" in s
    assert wj.garden_section(None, TODAY) is None
    assert wj.garden_section({"days": []}, TODAY) is None


def test_weather_section_forecast_excluded():
    s = wj.weather_section(_soil(), TODAY)
    # Week = laatste 7 niet-forecast dagen (Tmax 23..29); de 99°-forecast telt niet.
    assert "Tmax 23–29°" in s
    assert "7.0 mm regen" in s
    assert "zondag 5 jul" in s           # warmste dag = de jongste (Tmax 29)
    assert wj.weather_section(None, TODAY) is None


def test_mowing_section():
    mow = {"mowings": {"2026-06-20": {"length_mm": 40},
                       "2026-07-03": {"length_mm": 50}},
           "accum_today": 13.7, "params": {"READY_GU_effective": 19.4},
           "ready": False, "dormant": False, "predicted_next_mow": "2026-07-08"}
    s = wj.mowing_section(mow, TODAY)
    assert "1× gemaaid" in s             # alleen 3 jul valt in de week
    assert "50 mm" in s
    assert "13.7/19.4 GU" in s
    assert "woensdag 8 jul" in s
    # Maairijp verdringt de voorspelling; dormant verdringt alles.
    s2 = wj.mowing_section({**mow, "ready": True}, TODAY)
    assert "maairijp" in s2
    s3 = wj.mowing_section({**mow, "dormant": True}, TODAY)
    assert "winterrust" in s3
    assert wj.mowing_section(None, TODAY) is None


def test_twin_section_week_ago_pick():
    hist = [
        {"t": (NOW - timedelta(days=9)).isoformat(), "rmse": 0.90, "skill": 0.2},
        {"t": (NOW - timedelta(days=7)).isoformat(), "rmse": 0.80, "skill": 0.3},
        {"t": (NOW - timedelta(days=2)).isoformat(), "rmse": 9.99, "held": True},
        {"t": NOW.isoformat(), "rmse": 0.62, "skill": 0.50},
    ]
    s = wj.twin_section({"rmse_history": hist}, NOW)
    assert "RMSE 0.62°" in s
    assert "was 0.80°" in s              # laatste punt ≤ now−6.5d; held-punt genegeerd
    assert "↘" in s and "skill 0.50" in s
    # Korte historie → vergelijk met het oudste punt.
    s2 = wj.twin_section({"rmse_history": hist[-1:]}, NOW)
    assert s2 == "🧠 <b>Tweeling</b>: RMSE 0.62° · skill 0.50"
    assert wj.twin_section({"rmse_history": []}, NOW) is None
    assert wj.twin_section(None, NOW) is None


def test_station_section_staleness():
    fresh = {"generated_at": (NOW - timedelta(days=5)).isoformat(),
             "overall": {"mean_bias": 0.88, "n": 1438}}
    s = wj.station_section(fresh, NOW)
    assert "bias +0.9°" in s and "1438" in s
    stale = {**fresh, "generated_at": (NOW - timedelta(days=40)).isoformat()}
    assert wj.station_section(stale, NOW) is None
    assert wj.station_section(None, NOW) is None
    assert wj.station_section({"overall": {}}, NOW) is None


def test_build_message_skips_when_empty_and_caps_length():
    assert wj.build_message([None, None], TODAY) is None
    msg = wj.build_message(["🌱 regel", None, "🧠 regel"], TODAY)
    assert msg.startswith("📒 <b>Weekjournaal</b> — week t/m zondag 5 jul")
    assert msg.count("regel") == 2
    lang = wj.build_message(["x" * 5000], TODAY)
    assert len(lang) <= wj.MAX_LEN + 1   # afgekapt + ellipsis
    assert lang.endswith("…")


def test_missing_artifact_skips_section(tmp_path, monkeypatch):
    # main() met alle paden naar niet-bestaande bestanden → geen bericht, geen crash.
    for var in ("SOIL_DATA_PATH", "MOWING_DATA_PATH", "LEARNED_PATH",
                "ACCURACY_DATA_PATH"):
        monkeypatch.setattr(wj, var, str(tmp_path / "afwezig.json"))
    monkeypatch.setenv("DRY_RUN", "1")
    sent = []
    monkeypatch.setattr(wj, "send_telegram", lambda msg: sent.append(msg))
    wj.main()
    assert sent == []


def test_load_graceful(tmp_path):
    bad = tmp_path / "kapot.json"
    bad.write_text("{niet json")
    assert wj._load(str(bad)) is None
    assert wj._load(str(tmp_path / "afwezig.json")) is None
    ok = tmp_path / "ok.json"
    ok.write_text('{"a": 1}')
    assert wj._load(str(ok)) == {"a": 1}

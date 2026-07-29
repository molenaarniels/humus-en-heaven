"""Tests voor om_bias — de zelflerende Open-Meteo-modelbias (Project 6).

Zuivere functies, geen netwerk en geen bestanden: emmerindeling, het vastleggen en
afrekenen van forecasts, en het samenvatten tot een geleerde nacht/dag-bias.
Asserties pinnen de *vorm* van de logica tegen de moduleconstanten aan — niet tegen
hardcoded tuningwaarden (vgl. tests/test_window_advisor.py).
"""

from datetime import datetime, timedelta

import pytest

import om_bias
from om_bias import (bias_for, bucket, correction_for, is_night, learn,
                     night_weight, record_forecast, verify_pending)

NOW = datetime(2026, 7, 29, 10, 15)


def hour(h, day=29):
    return datetime(2026, 7, day, h, 0)


# ── emmers ─────────────────────────────────────────────────────────────────────

def test_nachtemmer_wikkelt_over_middernacht():
    assert is_night(hour(23)) and is_night(hour(0)) and is_night(hour(3))
    assert not is_night(hour(12)) and not is_night(hour(18))


def test_emmergrenzen_sluiten_op_elkaar_aan():
    # Precies bij NIGHT_END begint de dag, precies bij NIGHT_START de nacht.
    assert not is_night(hour(om_bias.NIGHT_END))
    assert is_night(hour(om_bias.NIGHT_END - 1))
    assert is_night(hour(om_bias.NIGHT_START))
    assert not is_night(hour(om_bias.NIGHT_START - 1))


def test_bucket_geeft_de_sleutelnaam():
    assert bucket(hour(2)) == "night"
    assert bucket(hour(14)) == "day"


# ── record_forecast ────────────────────────────────────────────────────────────

def _fcrows(now, leads, temp=25.0):
    return [{"dt": now + timedelta(hours=h), "temp": temp} for h in leads]


def test_legt_alleen_uren_binnen_de_leadband_vast():
    rows = _fcrows(NOW, [1, om_bias.RECORD_LEAD_MIN_H, 12, om_bias.RECORD_LEAD_MAX_H, 24])
    log = record_forecast({}, rows, NOW)
    leads = sorted(round((datetime.fromisoformat(p["t"]) - NOW).total_seconds() / 3600.0)
                   for p in log["pending"])
    assert leads == [round(om_bias.RECORD_LEAD_MIN_H), 12, round(om_bias.RECORD_LEAD_MAX_H)]


def test_kortere_vooruitblik_telt_niet_mee():
    # Binnen de leadband van het stationsanker meet je je eigen correctie, niet de
    # modelfout — die uren mogen niet in het logboek belanden.
    log = record_forecast({}, _fcrows(NOW, [0, 1, 3]), NOW)
    assert log["pending"] == []


def test_bestaand_uur_wordt_niet_overschreven():
    # Elk uur houdt de vróégst vastgelegde forecast → consistente vooruitblik.
    log = record_forecast({}, _fcrows(NOW, [18], temp=25.0), NOW)
    later = NOW + timedelta(hours=6)
    log = record_forecast(log, [{"dt": NOW + timedelta(hours=18), "temp": 30.0}], later)
    assert len(log["pending"]) == 1
    assert log["pending"][0]["fc"] == 25.0


def test_al_geverifieerd_uur_komt_niet_terug():
    log = {"pending": [], "errors": [{"t": hour(20).isoformat(), "e": 1.0}]}
    log = record_forecast(log, [{"dt": hour(20), "temp": 25.0}], hour(20) - timedelta(hours=12))
    assert log["pending"] == []


def test_ontbrekende_temp_wordt_overgeslagen():
    log = record_forecast({}, [{"dt": NOW + timedelta(hours=12), "temp": None}], NOW)
    assert log["pending"] == []


# ── verify_pending ─────────────────────────────────────────────────────────────

def test_rekent_af_tegen_de_stationsmeting():
    valid = hour(2)
    log = {"pending": [{"t": valid.isoformat(), "fc": 27.5}]}
    hist = [{"t": valid.isoformat(), "temp": 24.5, "om": 27.5, "src": "wu"}]
    out = verify_pending(log, hist, valid + timedelta(hours=1))
    assert out["pending"] == []
    assert out["errors"] == [{"t": valid.isoformat(), "e": 3.0}]  # model 3° te warm


def test_toekomstig_uur_blijft_openstaan():
    valid = NOW + timedelta(hours=12)
    log = {"pending": [{"t": valid.isoformat(), "fc": 27.5}]}
    out = verify_pending(log, [], NOW)
    assert len(out["pending"]) == 1 and out["errors"] == []


def test_open_meteo_terugval_telt_niet_als_meting():
    # Op een terugval ís de "meting" ditzelfde model → fout per definitie 0, wat de
    # geleerde bias zou verwateren. Zulke samples mogen niet verifiëren.
    valid = hour(2)
    log = {"pending": [{"t": valid.isoformat(), "fc": 27.5}]}
    hist = [{"t": valid.isoformat(), "temp": 27.5, "om": 27.5, "src": "open-meteo"}]
    out = verify_pending(log, hist, valid + timedelta(hours=1))
    assert out["errors"] == []
    assert len(out["pending"]) == 1  # blijft openstaan, meting kan nog komen


def test_oud_sample_zonder_src_herkend_aan_exacte_gelijkheid():
    valid = hour(2)
    log = {"pending": [{"t": valid.isoformat(), "fc": 27.5}]}
    hist = [{"t": valid.isoformat(), "temp": 27.5, "om": 27.5}]  # geen src-veld
    out = verify_pending(log, hist, valid + timedelta(hours=1))
    assert out["errors"] == []


def test_meting_buiten_de_tolerantie_verifieert_niet():
    valid = hour(2)
    ver = valid + timedelta(minutes=om_bias.MATCH_TOL_MIN + 5)
    log = {"pending": [{"t": valid.isoformat(), "fc": 27.5}]}
    hist = [{"t": ver.isoformat(), "temp": 24.5, "om": 27.0, "src": "wu"}]
    out = verify_pending(log, hist, valid + timedelta(hours=1))
    assert out["errors"] == []


def test_dichtstbijzijnde_meting_wint():
    valid = hour(2)
    log = {"pending": [{"t": valid.isoformat(), "fc": 27.5}]}
    hist = [
        {"t": (valid - timedelta(minutes=8)).isoformat(), "temp": 20.0, "om": 27.0, "src": "wu"},
        {"t": (valid + timedelta(minutes=2)).isoformat(), "temp": 24.5, "om": 27.0, "src": "wu"},
    ]
    out = verify_pending(log, hist, valid + timedelta(hours=1))
    assert out["errors"][0]["e"] == 3.0


def test_openstaande_verificatie_vervalt_na_de_opgeeftermijn():
    valid = hour(2)
    log = {"pending": [{"t": valid.isoformat(), "fc": 27.5}]}
    out = verify_pending(log, [], valid + timedelta(hours=om_bias.PENDING_GIVE_UP_H + 1))
    assert out["pending"] == [] and out["errors"] == []


def test_fouten_ouder_dan_het_leervenster_vallen_weg():
    oud = NOW - timedelta(days=om_bias.LEARN_WINDOW_D + 1)
    log = {"pending": [], "errors": [{"t": oud.isoformat(), "e": 5.0},
                                     {"t": NOW.isoformat(), "e": 1.0}]}
    out = verify_pending(log, [], NOW)
    assert [e["e"] for e in out["errors"]] == [1.0]


def test_verify_muteert_de_invoer_niet():
    valid = hour(2)
    log = {"pending": [{"t": valid.isoformat(), "fc": 27.5}], "errors": []}
    hist = [{"t": valid.isoformat(), "temp": 24.5, "om": 27.5, "src": "wu"}]
    verify_pending(log, hist, valid + timedelta(hours=1))
    assert len(log["pending"]) == 1 and log["errors"] == []


# ── learn ──────────────────────────────────────────────────────────────────────

def _errs(h, n, e, day=29):
    return [{"t": (hour(h, day) + timedelta(minutes=i)).isoformat(), "e": e}
            for i in range(n)]


def test_te_weinig_samples_geeft_nul():
    # Onder de drempel gedraagt alles zich exact als vóór deze module.
    out = learn({"errors": _errs(2, om_bias.MIN_SAMPLES - 1, 2.0)})
    assert out["night"] == 0.0
    assert out["n_night"] == om_bias.MIN_SAMPLES - 1


def test_genoeg_samples_geeft_de_mediaan():
    out = learn({"errors": _errs(2, om_bias.MIN_SAMPLES, 1.4)})
    assert out["night"] == pytest.approx(1.4)
    assert out["day"] == 0.0          # andere emmer blijft onaangeroerd


def test_uitschieter_sleept_de_mediaan_niet_mee():
    errs = _errs(2, om_bias.MIN_SAMPLES, 1.0) + _errs(3, 3, 40.0)
    assert learn({"errors": errs})["night"] == pytest.approx(1.0)


def test_emmers_worden_apart_geleerd():
    out = learn({"errors": _errs(2, om_bias.MIN_SAMPLES, 1.5)
                          + _errs(14, om_bias.MIN_SAMPLES, 0.5)})
    assert out["night"] == pytest.approx(1.5)
    assert out["day"] == pytest.approx(0.5)


def test_geleerde_bias_wordt_geklemd():
    out = learn({"errors": _errs(2, om_bias.MIN_SAMPLES, om_bias.MAX_BIAS + 10)})
    assert out["night"] == pytest.approx(om_bias.MAX_BIAS)


def test_leeg_logboek_is_neutraal():
    out = learn({})
    assert out["night"] == 0.0 and out["day"] == 0.0


# ── bias_for / correction_for ──────────────────────────────────────────────────

def test_zonder_geleerde_waarden_geen_correctie():
    assert bias_for(hour(2), None) == 0.0
    assert correction_for(hour(2), {}) == 0.0


def test_correctie_is_tegengesteld_aan_de_bias():
    learned = {"night": 1.4, "day": 0.8}
    assert bias_for(hour(2), learned) == pytest.approx(1.4)
    assert correction_for(hour(2), learned) == pytest.approx(-1.4)  # te warm → eraf
    assert correction_for(hour(14), learned) == pytest.approx(-0.8)


# ── night_weight: gladde emmerovergang ─────────────────────────────────────────

def test_night_weight_vol_in_de_kern():
    assert night_weight(hour(2)) == 1.0
    assert night_weight(hour(14)) == 0.0


def test_night_weight_halveert_precies_op_de_grens():
    assert night_weight(hour(om_bias.NIGHT_START)) == pytest.approx(0.5)
    assert night_weight(hour(om_bias.NIGHT_END)) == pytest.approx(0.5)


def test_night_weight_loopt_monotoon_door_de_overgang():
    # Rond NIGHT_START moet het gewicht netjes van 0 naar 1 lopen, zonder sprong.
    start = om_bias.NIGHT_START
    ws = [night_weight(datetime(2026, 7, 29, start - 1, m)) for m in (0, 30)]
    ws += [night_weight(datetime(2026, 7, 29, start, m)) for m in (0, 30)]
    ws += [night_weight(datetime(2026, 7, 29, start + 1, 0))]
    assert ws[0] == 0.0 and ws[-1] == 1.0
    assert all(b >= a for a, b in zip(ws, ws[1:]))


def test_night_weight_blijft_binnen_bereik():
    for h in range(24):
        for m in (0, 15, 30, 45):
            assert 0.0 <= night_weight(datetime(2026, 7, 29, h, m)) <= 1.0


def test_bias_vervloeit_tussen_de_emmers():
    # Op de grens is de correctie het gemiddelde van beide emmers — geen sprong van ~1°C
    # tussen twee opeenvolgende forecast-uren.
    learned = {"night": 1.5, "day": 0.5}
    assert bias_for(hour(om_bias.NIGHT_START), learned) == pytest.approx(1.0)


def test_niet_numerieke_waarde_is_neutraal():
    assert bias_for(hour(2), {"night": None}) == 0.0


# ── integratie: één rondje leren ───────────────────────────────────────────────

def test_volledige_cyclus_vastleggen_afrekenen_leren():
    """Leg forecasts vast, laat de tijd verstrijken, reken ze af tegen metingen die
    consequent 1,5° koeler zijn, en zie de geleerde nachtbias uitkomen op +1,5."""
    log, hist = {}, []
    issue = datetime(2026, 7, 20, 6, 0)
    valid_hours = []
    for d in range(om_bias.MIN_SAMPLES):           # genoeg nachturen verzamelen
        v = issue + timedelta(hours=12 + d)
        log = record_forecast(log, [{"dt": v, "temp": 25.0}], v - timedelta(hours=12))
        if is_night(v):
            valid_hours.append(v)
        hist.append({"t": v.isoformat(), "temp": 23.5, "om": 25.0, "src": "wu"})
    end = issue + timedelta(hours=12 + om_bias.MIN_SAMPLES)
    log = verify_pending(log, hist, end)
    out = learn(log)
    assert out["n_night"] == len(valid_hours)
    if len(valid_hours) >= om_bias.MIN_SAMPLES:
        assert out["night"] == pytest.approx(1.5)

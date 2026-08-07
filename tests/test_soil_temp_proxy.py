"""Weegschaal voor de bodemtemperatuur-proxy (`soil_temp_proxy.py`).

De hoofdmaat is bewust niet de RMSE maar de verschuiving van de seizoensgrens:
`temp_factor` is een drempelfunctie die boven 8 °C niets meer doet, dus twee
reeksen mogen daar ver uit elkaar liggen zonder één beslissing te veranderen.
Deze tests pinnen dat onderscheid vast.
"""
from datetime import date

import pytest

import soil_temp_proxy as stp
from soil_model import SOIL_TEMP_WINDOW, temp_factor


def _reeks(waarden, start="2026-01-01"):
    from datetime import timedelta
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(len(waarden))], list(waarden)


# ── rolling_mean ──────────────────────────────────────────────────────────────

def test_rolling_mean_start_met_minder_dagen():
    """Zoals `run_water_balance` het opbouwt: de eerste dagen middelen over wat er
    is, i.p.v. None. Anders zou het model zijn eigen eerste dagen niet kunnen."""
    assert stp.rolling_mean([10, 20, 30], 5) == [10.0, 15.0, 20.0]


def test_rolling_mean_schuift_het_venster():
    assert stp.rolling_mean([0, 0, 0, 0, 0, 10], 5)[-1] == pytest.approx(2.0)


def test_rolling_mean_laat_gaten_staan():
    assert stp.rolling_mean([10, None, 20], 3) == [10.0, None, 15.0]


# ── Seizoensgrenzen ───────────────────────────────────────────────────────────

def test_season_marks_vindt_de_duurzame_lentepassage():
    dates, vals = _reeks([5] * 10 + [9] * 20, start="2026-02-01")
    marks = stp.season_marks(dates, vals, threshold=8.0)
    assert marks[2026]["spring"] == date(2026, 2, 11)


def test_een_losse_warme_dag_telt_niet_als_lente():
    """Zonder persistentie-eis maakt één zachte februaridag een groeiseizoen."""
    dates, vals = _reeks([5] * 10 + [9] + [5] * 19, start="2026-02-01")
    assert stp.season_marks(dates, vals, threshold=8.0)[2026]["spring"] is None


def test_najaarspassage_telt_alleen_na_de_zomer():
    dates, vals = _reeks([12] * 10 + [4] * 20, start="2026-09-01")
    marks = stp.season_marks(dates, vals, threshold=8.0)
    assert marks[2026]["autumn"] == date(2026, 9, 11)
    assert marks[2026]["spring"] is None       # september is geen lente


def test_mark_shift_telt_dagen_en_tekent_de_richting():
    proxy = {2026: {"spring": date(2026, 3, 20), "autumn": date(2026, 11, 5)}}
    bodem = {2026: {"spring": date(2026, 3, 15), "autumn": date(2026, 11, 12)}}
    rows = {r["season"]: r["shift_days"] for r in stp.mark_shift(proxy, bodem)}
    assert rows["spring"] == +5      # proxy loopt achter in de lente
    assert rows["autumn"] == -7      # en loopt vóór in het najaar


def test_mark_shift_slaat_ontbrekende_seizoenen_over():
    proxy = {2026: {"spring": date(2026, 3, 20), "autumn": None}}
    bodem = {2026: {"spring": None, "autumn": date(2026, 11, 12)}}
    assert stp.mark_shift(proxy, bodem) == []


# ── Vertaling naar modeltaal ──────────────────────────────────────────────────

def test_disagreement_negeert_verschillen_boven_de_drempel():
    """15 vs 25 °C is 10 graden verschil en nul verschil in temp_factor — precies
    waarom RMSE hier de verkeerde maat is."""
    out = stp.factor_disagreement([15.0] * 20, [25.0] * 20)
    assert out["n_disagree"] == 0 and out["max_abs"] == 0.0


def test_disagreement_telt_wel_in_de_schouderband():
    out = stp.factor_disagreement([5.0] * 10, [8.0] * 10)
    assert out["n_disagree"] == 10
    assert out["mean_abs"] == pytest.approx(1.0)
    assert out["proxy_lager"] == 10 and out["proxy_hoger"] == 0


def test_disagreement_gebruikt_dezelfde_drempels_als_het_model():
    """Geen kopie van de 5/8 °C-grenzen: de module importeert `temp_factor`."""
    assert stp.temp_factor is temp_factor


# ── Samenhang ─────────────────────────────────────────────────────────────────

def test_evaluate_windows_scoort_elk_venster():
    dates, air = _reeks([2 + i * 0.2 for i in range(200)], start="2026-01-01")
    soil = [v - 1.0 for v in air]
    rows = stp.evaluate_windows(dates, air, soil, (1, 5, 14))
    assert [r["window"] for r in rows] == [1, 5, 14]
    assert all(r["fit"]["n"] == 200 for r in rows)


def test_best_window_kiest_op_verschuiving_niet_op_rmse():
    """Een venster met een lagere RMSE maar een grotere seizoensverschuiving mag
    niet winnen — de verschuiving is wat een maai-advies verzet."""
    rows = [{"window": 3, "median_abs_shift": 4.0}, {"window": 9, "median_abs_shift": 1.5}]
    assert stp.best_window(rows) == (9, 1.5)
    assert stp.best_window([{"window": 3, "median_abs_shift": None}]) is None


def test_een_tragere_bodem_vraagt_een_langer_venster():
    """Bouw een bodemreeks die de lucht met vertraging volgt en controleer dat de
    weegschaal dát ook aanwijst — de kern van wat de probe moet kunnen zien."""
    import math
    n = 730
    # Cosinus met het minimum op 1 januari: kruisingen vallen zo half maart en
    # half oktober, net als hier — een sinus vanaf 1 januari legt ze in juli en
    # december en dan vindt `season_marks` terecht niets.
    dates, air = _reeks([10 - 9 * math.cos(2 * math.pi * i / 365) for i in range(n)])
    traag = stp.rolling_mean(air, 14)          # bodem = sterk gedempte lucht
    rows = stp.evaluate_windows(dates, air, traag, (1, 5, 14, 21))
    beste = stp.best_window(rows)
    assert beste is not None and beste[0] >= 5     # niet het kortste venster


def test_huidig_venster_zit_in_de_kandidaten():
    from tools import soil_temp_probe  # noqa: F401
    import importlib
    mod = importlib.import_module("tools.soil_temp_probe")
    assert SOIL_TEMP_WINDOW in mod.WINDOWS


def test_een_reeks_die_warm_begint_meldt_geen_lentepassage():
    """Een archief dat in de zomer begint stond op dag 1 al boven de drempel. Dat
    is een startwaarde, geen kruising — en zonder deze eis zou elk meerjarig
    venster een verzonnen seizoensgrens op zijn eerste dag krijgen."""
    dates, vals = _reeks([12] * 30, start="2026-06-01")
    assert stp.season_marks(dates, vals, threshold=8.0)[2026]["spring"] is None


def test_koud_beginnende_reeks_meldt_geen_najaarspassage():
    dates, vals = _reeks([2] * 30, start="2026-09-01")
    assert stp.season_marks(dates, vals, threshold=8.0)[2026]["autumn"] is None


def test_best_window_is_bestand_tegen_een_uitbijterjaar():
    """Eén zachte winter waarin de bodem nauwelijks onder 8 °C zakt kan de
    seizoensgrens met maanden verschuiven (gemeten jan 2023: 72 dagen). Op een
    gemiddelde domineert zo'n jaar tien andere grenzen; op de mediaan niet."""
    rows = [
        {"window": 5, "mean_abs_shift": 9.8, "median_abs_shift": 1.5},
        {"window": 14, "mean_abs_shift": 2.9, "median_abs_shift": 4.0},
    ]
    assert stp.best_window(rows) == (5, 1.5)      # niet 14 op het gemiddelde


def test_evaluate_windows_geeft_beide_maten():
    dates, air = _reeks([2 + i * 0.1 for i in range(400)])
    soil = [v - 0.5 for v in air]
    row = stp.evaluate_windows(dates, air, soil, (5,))[0]
    assert "median_abs_shift" in row and "mean_abs_shift" in row

"""Het evaluatieprotocol voor de stralingsbiascorrectie (`bias_eval.py`, fase 2).

Deze tests pinnen de *poorten* vast, niet de uitkomsten: dat een fit klopt op een
exact bekende relatie, dat dagen niet over folds lekken, dat de acceptatieregel
een kandidaat afwijst zodra één van de drie eisen faalt. Als die poorten stuk
gaan, praat het rapport ons een verbetering aan die er niet is — precies het
failliet dat dit protocol moet voorkomen.
"""
import random

import pytest

import bias_eval as be
from wu_bias import SOLAR_BIAS_SLOPE


def _rows(n=240, slope=0.004, intercept=0.0, noise=0.0, seed=0, start_month=4):
    """Synthetische uren met een exact bekende bias-relatie.

    `noise` is een amplitude via een geseede RNG — bewust niet een herhalend
    patroon: met een periode die 24 deelt is elke dag identiek, valt er tussen
    fold-indelingen niets te verschillen en meet de A/A-vloer per definitie 0.
    """
    rng = random.Random(seed)
    out = []
    for i in range(n):
        day = i // 24
        month = start_month + day // 30
        solar = (i % 24) * 40.0
        bias = intercept + slope * solar + (rng.uniform(-noise, noise) if noise else 0.0)
        out.append({"t": f"2026-{month:02d}-{day % 30 + 1:02d}T{i % 24:02d}",
                    "wu": 20.0 + bias, "om": 20.0, "solar": solar,
                    "wind": 5.0 + (i % 7), "cloud": (i * 13) % 101, "rh": 60})
    return out


# ── prepare ───────────────────────────────────────────────────────────────────

def test_prepare_leidt_bias_en_features_af():
    row = be.prepare(_rows(2))[1]
    assert row["bias"] == pytest.approx(0.004 * 40.0)
    assert row["1"] == 1.0
    assert row["solar_cloud"] == pytest.approx(40.0 * (13 % 101) / 100.0)


def test_prepare_kiest_de_referentiekolom():
    rows = [{"t": "2026-04-01T12", "wu": 22.0, "om": 20.0, "knmi": 21.0, "solar": 100.0}]
    assert be.prepare(rows, reference="om")[0]["bias"] == pytest.approx(2.0)
    assert be.prepare(rows, reference="knmi")[0]["bias"] == pytest.approx(1.0)


def test_prepare_slaat_rijen_zonder_driver_over():
    """De teruggevulde archiefrijen dragen geen `wu_solar`. Liever een kleinere
    eerlijke steekproef dan geïmputeerde nullen die de helling naar beneden trekken."""
    rows = [{"t": "2026-04-01T12", "wu": 22.0, "om": 20.0, "solar": 100.0, "wu_solar": None}]
    assert be.prepare(rows, driver="wu_solar") == []
    assert len(be.prepare(rows, driver="solar")) == 1


def test_prev_solar_alleen_bij_een_echt_aansluitend_uur():
    rows = [{"t": "2026-04-01T10", "wu": 20.0, "om": 20.0, "solar": 100.0},
            {"t": "2026-04-01T11", "wu": 20.0, "om": 20.0, "solar": 200.0},
            {"t": "2026-04-01T15", "wu": 20.0, "om": 20.0, "solar": 300.0}]
    prepared = be.prepare(rows)
    assert prepared[0]["prev_solar"] is None      # eerste rij
    assert prepared[1]["prev_solar"] == 100.0     # sluit aan
    assert prepared[2]["prev_solar"] is None      # gat van 4 uur


def test_prev_solar_over_de_dagrand():
    rows = [{"t": "2026-04-01T23", "wu": 20.0, "om": 20.0, "solar": 5.0},
            {"t": "2026-04-02T00", "wu": 20.0, "om": 20.0, "solar": 0.0}]
    assert be.prepare(rows)[1]["prev_solar"] == 5.0


# ── fit ───────────────────────────────────────────────────────────────────────

def test_fit_vindt_een_exacte_relatie_terug():
    rows = be.prepare(_rows(slope=0.0037, intercept=0.42))
    coef = be.fit(rows, ["1", "solar"])
    assert coef[0] == pytest.approx(0.42, abs=1e-6)
    assert coef[1] == pytest.approx(0.0037, abs=1e-9)


def test_fit_overleeft_een_singuliere_kolom():
    """Een constante feature naast het intercept maakt de normaalmatrix singulier.
    Dat mag geen ZeroDivisionError geven — het rapport moet blijven draaien."""
    rows = be.prepare(_rows(48))
    for r in rows:
        r["dubbel"] = 1.0
    assert len(be.fit(rows, ["1", "dubbel", "solar"])) == 3


def test_leeg_model_is_geen_correctie():
    assert be.fit(be.prepare(_rows(24)), []) == []


# ── Validatieschema's ─────────────────────────────────────────────────────────

def test_forward_chain_traint_alleen_op_het_verleden():
    rows = be.prepare(_rows(24 * 90))
    folds = be.forward_chain(rows, ["solar"])
    maanden = sorted({r["month"] for r in rows})
    assert [f[0] for f in folds] == maanden[1:]      # eerste maand is alleen training
    assert all(f[1] == pytest.approx(0.0, abs=1e-9) for f in folds)   # ruisloos → perfect


def test_day_blocked_cv_laat_geen_dag_lekken(monkeypatch):
    """Uren van één dag zijn sterk gecorreleerd; belanden ze in train én test, dan
    ziet elk model er beter uit dan het is."""
    gezien = []
    origineel = be.fit

    def spion(rows, features):
        gezien.append({r["day"] for r in rows})
        return origineel(rows, features)

    monkeypatch.setattr(be, "fit", spion)
    rows = be.prepare(_rows(24 * 12))
    be.day_blocked_cv(rows, ["solar"], k=3)
    alle_dagen = {r["day"] for r in rows}
    for traindagen in gezien:
        testdagen = alle_dagen - traindagen
        assert testdagen and not (traindagen & testdagen)


def test_cv_is_deterministisch_per_seed():
    rows = be.prepare(_rows(24 * 30, noise=0.4))
    assert be.day_blocked_cv(rows, ["solar"], seed=7) == be.day_blocked_cv(rows, ["solar"], seed=7)
    assert be.day_blocked_cv(rows, ["solar"], seed=7) != be.day_blocked_cv(rows, ["solar"], seed=8)


def test_aa_ruisvloer_is_nul_op_ruisloze_data():
    """Zonder ruis fit elk fold hetzelfde model → geen spreiding. Met ruis wel:
    dát is de marge waaronder een 'verbetering' niets betekent."""
    schoon = be.prepare(_rows(24 * 30))
    rumoerig = be.prepare(_rows(24 * 30, noise=0.8))
    assert be.aa_floor(schoon, ["1", "solar"]) == pytest.approx(0.0, abs=1e-9)
    assert be.aa_floor(rumoerig, ["1", "solar"]) > 0.0


def test_param_budget_telt_dagen_niet_uren():
    rows = be.prepare(_rows(24 * 10))                # 240 uren, 10 dagen
    p, dagen, past = be.param_budget(rows, ["1", "solar"])
    assert (p, dagen) == (2, 10) and not past        # 10 dagen dragen geen 2 params
    assert be.param_budget(be.prepare(_rows(24 * 60)), ["1", "solar"])[2]


# ── Acceptatieregel ───────────────────────────────────────────────────────────

BETER = [("2026-05", 1.00, 100), ("2026-06", 1.00, 100)]
LAT = [("2026-05", 1.50, 100), ("2026-06", 1.50, 100)]


def test_accept_neemt_een_echte_verbetering_aan():
    ok, redenen = be.accept(BETER, LAT, floor=0.01)
    assert ok and redenen == []


def test_accept_verwerpt_winst_binnen_de_ruisvloer():
    ok, redenen = be.accept(BETER, LAT, floor=0.9)
    assert not ok and any("ruisvloer" in r for r in redenen)


def test_accept_verwerpt_een_gemiddelde_die_uit_een_fold_komt():
    """Groot winnen in juni en licht verliezen in mei is een seizoenstoevalligheid,
    geen betere modelvorm — ook al is het gemiddelde gunstig."""
    wisselvallig = [("2026-05", 1.55, 100), ("2026-06", 0.50, 100)]
    ok, redenen = be.accept(wisselvallig, LAT, floor=0.01)
    assert not ok and any("verliest in 1/2 folds" in r for r in redenen)


def test_accept_verwerpt_zonder_folds():
    ok, redenen = be.accept([], LAT, floor=0.01)
    assert not ok and "te weinig maanden" in redenen[0]


def test_accept_vergelijkt_alleen_gedeelde_maanden():
    """Een model dat een feature mist (bv. prev_solar) verliest rijen en dus soms
    een hele testmaand. Dan mag er niet stilzwijgend een andere maand tegenover
    de lat komen te staan."""
    ok, redenen = be.accept([("2026-06", 1.0, 100)], [("2026-07", 1.5, 100)], floor=0.01)
    assert not ok and "geen enkele testmaand" in redenen[0]


# ── Samenhang ─────────────────────────────────────────────────────────────────

def test_deployed_predictor_is_de_uitgerolde_constante():
    assert be.deployed_predictor({"solar": 500.0}) == pytest.approx(SOLAR_BIAS_SLOPE * 500.0)
    assert be.deployed_predictor({"solar": -10.0}) == 0.0   # nooit een negatieve correctie


def test_evaluate_zet_de_lat_bovenaan_en_scoort_elk_model():
    resultaten = be.evaluate(be.prepare(_rows(24 * 90, noise=0.4)))
    assert resultaten[0]["naam"] == be.DEPLOYED
    assert {r["naam"] for r in resultaten} >= set(be.MODELS)
    for res in resultaten:
        assert res["cv"] is not None


def test_skill_is_relatief_aan_het_nulmodel():
    assert be.skill(0.5, 1.0) == pytest.approx(0.5)
    assert be.skill(1.5, 1.0) == pytest.approx(-0.5)
    assert be.skill(None, 1.0) is None and be.skill(1.0, 0.0) is None

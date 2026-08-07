"""Coherentie-toets met buur-PWS'en (`neighbour_pws.py`, fase 1 route A).

De toets moet één ding goed doen: onderscheiden of onze warme nacht gedeeld wordt
door de buurt of niet. Deze tests pinnen dat oordeel vast, plus de randgevallen
waarin het géén uitspraak mág doen.
"""
import pytest

import neighbour_pws as npws


def _hours(n=240, bias=0.0, night_bias=None, slope=0.0, wind=8.0):
    """Uren met een bekend dag/nacht-profiel; zon volgt een driehoek per etmaal."""
    rows = []
    for i in range(n):
        uur = i % 24
        solar = max(0.0, (12 - abs(uur - 13)) * 60.0)
        nacht = solar <= npws.NIGHT_SOLAR_MAX
        rows.append({"t": f"2026-08-{i // 24 + 1:02d}T{uur:02d}",
                     "bias": (night_bias if (nacht and night_bias is not None) else bias)
                             + slope * solar,
                     "solar": solar, "wind": wind})
    return rows


# ── Koppeling ─────────────────────────────────────────────────────────────────

def test_pair_gebruikt_het_referentieframe_voor_zon_en_wind():
    """Elk station brengt alléén zijn temperatuur in — anders vergelijk je
    vooral anemometers op verschillende hoogtes."""
    station = {"2026-08-01T12": {"temp": 22.0, "wind": 99.0, "solar": 999.0}}
    ref = {"2026-08-01T12": {"temp": 20.0, "wind": 7.0, "solar": 400.0}}
    row = npws.pair_with_reference(station, ref)[0]
    assert row["bias"] == pytest.approx(2.0)
    assert row["wind"] == 7.0 and row["solar"] == 400.0


def test_pair_slaat_uren_zonder_beide_metingen_over():
    station = {"a": {"temp": 20.0}, "b": {"temp": None}, "c": {"temp": 21.0}}
    ref = {"a": {"temp": 19.0}, "b": {"temp": 19.0}, "c": {"temp": None}}
    assert [r["t"] for r in npws.pair_with_reference(station, ref)] == ["a"]


# ── Profiel ───────────────────────────────────────────────────────────────────

def test_profiel_scheidt_nacht_en_dag():
    prof = npws.profile(_hours(bias=0.2, night_bias=1.1))
    assert prof["night_bias"] == pytest.approx(1.1)
    assert prof["day_bias"] == pytest.approx(0.2)
    assert prof["n_night"] > 0 and prof["n_day"] > 0


def test_zonhelling_wordt_teruggevonden():
    prof = npws.profile(_hours(slope=0.004))
    assert prof["slope_per_100"] == pytest.approx(0.4, abs=1e-6)


def test_nachtprofiel_per_windklasse():
    rows = _hours(night_bias=1.2, wind=2.0) + _hours(night_bias=0.1, wind=18.0)
    bins = {(b["lo"], b["hi"]): b for b in npws.night_by_wind(rows)}
    assert bins[(0, 5)]["bias"] == pytest.approx(1.2)
    assert bins[(15, 100)]["bias"] == pytest.approx(0.1)
    assert bins[(5, 10)]["n"] == 0 and bins[(5, 10)]["bias"] is None


def test_dunne_windklasse_geeft_geen_getal():
    """Een handvol uren in een windklasse is geen gemiddelde waard — dat leest
    als een meting terwijl het ruis is."""
    mager = [r for r in _hours(48, night_bias=1.0, wind=2.0)
             if r["solar"] <= npws.NIGHT_SOLAR_MAX][:npws.MIN_BIN - 1]
    assert npws.night_by_wind(mager)[0]["bias"] is None


def test_zonhelling_none_bij_te_weinig_daguren():
    assert npws.solar_slope(_hours(24)[:5]) is None


# ── Het oordeel ───────────────────────────────────────────────────────────────

def test_gedeelde_warme_nacht_is_microklimaat():
    ons = npws.profile(_hours(night_bias=1.0))
    buren = [npws.profile(_hours(night_bias=b)) for b in (0.9, 1.1, 0.8)]
    code, uitleg = npws.verdict(ons, buren)
    assert code == "gedeeld" and "microklimaat" in uitleg


def test_alleen_wij_warm_wijst_op_plaatsing():
    ons = npws.profile(_hours(night_bias=1.6))
    buren = [npws.profile(_hours(night_bias=b)) for b in (0.3, 0.5, 0.2)]
    code, uitleg = npws.verdict(ons, buren)
    assert code == "uitzondering" and "plaatsing" in uitleg


def test_oordeel_meet_tegen_de_warmste_buur_niet_het_gemiddelde():
    """Eén afwijkend warme buur mag ons niet vrijpleiten — en andersom mag één
    koude buur ons niet veroordelen. Vandaar het maximum als lat."""
    ons = npws.profile(_hours(night_bias=1.2))
    met_warme_buur = [npws.profile(_hours(night_bias=b)) for b in (0.1, 0.1, 1.0)]
    assert npws.verdict(ons, met_warme_buur)[0] == "gedeeld"
    zonder = [npws.profile(_hours(night_bias=b)) for b in (0.1, 0.1, 0.2)]
    assert npws.verdict(ons, zonder)[0] == "uitzondering"


def test_krappe_marge_blijft_gedeeld():
    ons = npws.profile(_hours(night_bias=1.0))
    buren = [npws.profile(_hours(night_bias=0.7))]
    assert npws.verdict(ons, buren)[0] == "gedeeld"        # 0.3 < marge 0.5


def test_zonder_buren_geen_uitspraak():
    """Liever `onbeslist` dan een oordeel op één station — dit is precies het
    soort meting waar een halve conclusie erger is dan geen."""
    ons = npws.profile(_hours(night_bias=1.0))
    assert npws.verdict(ons, [])[0] == "onbeslist"
    assert npws.verdict({"night_bias": None}, [{"night_bias": 0.5}])[0] == "onbeslist"


def test_spreiding_over_de_buren():
    buren = [{"night_bias": 0.5}, {"night_bias": 0.9}]
    assert npws.spread(buren, "night_bias") == pytest.approx(0.2)
    assert npws.spread([{"night_bias": 0.5}], "night_bias") is None


# ── Secret-hygiëne ────────────────────────────────────────────────────────────

def test_buur_ids_komen_uit_het_secret(monkeypatch):
    monkeypatch.setenv("WU_NEIGHBOUR_IDS", " IAAA1 , IBBB2,,ICCC3 ")
    assert npws.neighbour_ids() == ["IAAA1", "IBBB2", "ICCC3"]
    monkeypatch.delenv("WU_NEIGHBOUR_IDS")
    assert npws.neighbour_ids() == []


def test_geen_station_ids_in_de_broncode():
    """Buur-id's zijn net zo goed locatiegegevens als WU_STATION_ID; ze horen in
    een secret, niet in een publieke repo."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for path in (root / "neighbour_pws.py", root / "tools" / "neighbour_probe.py",
                 root / ".github" / "workflows" / "neighbour-probe.yml"):
        tekst = path.read_text(encoding="utf-8")
        assert "IUTRE" not in tekst.upper()


def test_workflow_probe_logt_geen_ids(assert_checkout_pinned):
    import pathlib
    wf = (pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
          / "neighbour-probe.yml").read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in wf      # leest alleen
    assert "echo" not in wf                            # nooit een secret uitprinten
    assert_checkout_pinned("neighbour-probe.yml")

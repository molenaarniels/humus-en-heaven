"""Project 7 — archief, scatter-export en het herijkingssignaal."""
import json
import pathlib

import pytest

import station_accuracy as sa


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "HISTORY_DIR", str(tmp_path))
    return tmp_path


def _row(t, wu=20.0, om=19.0, **kw):
    return {"t": t, "wu": wu, "om": om, "solar": 400.0, "wu_solar": 512.0,
            "cloud": 20, "wind": 8.0, "rh": 61, **kw}


def test_archief_is_idempotent_en_werkt_bij(archive):
    """Een tweede run over hetzelfde venster mag niet stapelen — dat is precies wat
    er gebeurt als het venster elke dispatch overlapt (ACCURACY_DAYS=30, handmatig
    gedraaid). Wijzigt ERA5 zijn archief achteraf, dan wint de nieuwe waarde."""
    rows = [_row("2026-08-01T10"), _row("2026-08-01T11")]
    assert sa.append_archive(rows) == (2, 0)
    assert sa.append_archive(rows) == (0, 0)

    herzien = [_row("2026-08-01T10", om=19.4)]
    assert sa.append_archive(herzien) == (0, 1)
    bewaard = sa.load_archive()
    assert len(bewaard) == 2
    assert bewaard[0]["om"] == 19.4


def test_archief_splitst_per_maand_en_sorteert(archive):
    sa.append_archive([_row("2026-09-02T05"), _row("2026-08-31T23"), _row("2026-08-01T00")])
    assert sorted(p.name for p in archive.iterdir()) == ["2026-08.json", "2026-09.json"]
    aug = json.loads((archive / "2026-08.json").read_text())
    assert [r["t"] for r in aug["rows"]] == ["2026-08-01T00", "2026-08-31T23"]
    assert [r["t"] for r in sa.load_archive()] == ["2026-08-01T00", "2026-08-31T23", "2026-09-02T05"]


def test_archief_leidt_bias_en_lokaal_uur_af(archive):
    """`bias` wordt bewust niet opgeslagen maar afgeleid, zodat hij niet uit de pas
    kan lopen met zijn operanden na een ERA5-herziening. Het uur is lokaal (het
    archief zelf staat in UTC)."""
    sa.append_archive([_row("2026-08-01T10", wu=21.5, om=19.5)])
    row = sa.load_archive()[0]
    assert row["bias"] == pytest.approx(2.0)
    assert row["hour"] == 12          # UTC+2 in augustus
    assert "bias" not in json.loads((archive / "2026-08.json").read_text())["rows"][0]


def test_archief_slaat_onbruikbare_rijen_over(archive):
    sa.append_archive([_row("2026-08-01T10", wu=None), _row("2026-08-01T11")])
    assert [r["t"] for r in sa.load_archive()] == ["2026-08-01T11"]


def test_kapot_shard_blokkeert_het_archief_niet(archive):
    (archive / "2026-08.json").write_text("{niet eens json")
    assert sa.load_archive() == []
    assert sa.append_archive([_row("2026-08-01T10")]) == (1, 0)


def test_scatter_draagt_de_wu_pyranometer():
    """De scatter droeg alleen de Open-Meteo-instraling, terwijl de correctie in
    productie op de WU-pyranometer draait — elke modelvorm-analyse op dit artefact
    toetste dus een andere as dan er gebruikt wordt."""
    rows = [{"t": f"2026-08-01T{h:02d}", "hour": h, "wu": 20.0 + h, "om": 19.0 + h,
             "bias": 1.0, "solar": 100.0 * h, "wu_solar": 110.0 * h,
             "cloud": 20, "wind": 8.0, "rh": 60} for h in range(24)]
    scatter = sa.analyse(rows)["scatter"]
    assert len(scatter) == 24
    assert [p["wu_solar"] for p in scatter] == [110.0 * h for h in range(24)]


def test_workflow_checkout_pint_branch_tip(assert_checkout_pinned):
    assert_checkout_pinned("station-accuracy.yml")


# ── Tweede referentie (KNMI) ─────────────────────────────────────────────────

def test_knmi_wordt_additief_aangehangen(archive):
    rows = [{"t": "2026-08-01T10", "wu": 21.0, "om": 20.0},
            {"t": "2026-08-01T11", "wu": 22.0, "om": 20.5}]
    hit = sa.attach_knmi(rows, {"2026-08-01T10": {"temp": 20.4}})
    assert hit == 1
    assert rows[0]["knmi"] == 20.4
    assert rows[1]["knmi"] is None          # geen KNMI-uur → expliciet leeg, geen gat


def test_archief_bewaart_de_knmi_kolom(archive):
    rows = [_row("2026-08-01T10", knmi=20.4)]
    sa.append_archive(rows)
    assert sa.load_archive()[0]["knmi"] == 20.4


def test_ontbrekende_knmi_bron_laat_alles_op_era5_draaien(archive):
    """De diagnose mag niet omvallen als de scriptservice hapert — ERA5 blijft
    de referentie waarop gestratificeerd wordt."""
    rows = [{"t": "2026-08-01T10", "wu": 21.0, "om": 20.0, "hour": 12,
             "bias": 1.0, "solar": 300.0, "wu_solar": 320.0, "cloud": 20, "wind": 8.0}]
    assert sa.attach_knmi(rows, {}) == 0
    assert rows[0]["knmi"] is None
    assert sa.analyse(rows)["overall"]["n"] == 1


# ── Herijkingssignaal (fase 4) ───────────────────────────────────────────────

def _reeks(maanden=("2026-04", "2026-05", "2026-06", "2026-07"), slope=0.00421, dagen=25):
    """Uren met een exact bekende helling, over meerdere maanden (forward-chaining
    heeft minstens twee maanden nodig)."""
    rows = []
    for maand in maanden:
        for dag in range(1, dagen + 1):
            for uur in range(24):
                solar = max(0.0, (12 - abs(uur - 13)) * 60.0)
                rows.append({"t": f"{maand}-{dag:02d}T{uur:02d}",
                             "wu": 20.0 + slope * solar, "om": 20.0, "knmi": 20.0,
                             "solar": solar, "wu_solar": solar,
                             "cloud": 20, "wind": 8.0, "rh": 60})
    return rows


def test_geen_signaal_als_de_uitgerolde_helling_klopt():
    """De maandelijkse run moet zwijgen zolang er niets te melden is — anders is
    het een tikkende teller in plaats van een uitzonderingsmelding."""
    assert sa.recalibration_signal(_reeks(slope=sa.SOLAR_BIAS_SLOPE)) is None


def test_signaal_bij_een_gedragen_verschuiving():
    verschoven = sa.SOLAR_BIAS_SLOPE + 0.0015      # ~0.9 °C bij 600 W/m²
    signal = sa.recalibration_signal(_reeks(slope=verschoven))
    assert signal is not None
    assert signal["slope"] == pytest.approx(verschoven, abs=1e-5)
    assert signal["delta_c"] == pytest.approx(0.0015 * sa.REFERENCE_WM2, rel=0.05)
    assert signal["reference"] == "knmi" and signal["driver"] == "wu_solar"


def test_kleine_verschuiving_haalt_de_drempel_niet():
    """Statistisch overtuigend maar praktisch verwaarloosbaar is géén melding:
    de drempel staat in °C op een zonnige middag, niet in helling-eenheden."""
    klein = sa.SOLAR_BIAS_SLOPE + (sa.DRIFT_ALERT_C / sa.REFERENCE_WM2) * 0.5
    assert sa.recalibration_signal(_reeks(slope=klein)) is None


def test_signaal_valt_terug_op_era5_en_gridzon():
    """Een archief van vóór de KNMI-/wu_solar-kolommen moet gewoon werken."""
    rows = _reeks(slope=sa.SOLAR_BIAS_SLOPE + 0.0015)
    for r in rows:
        r["knmi"] = r["wu_solar"] = None
    signal = sa.recalibration_signal(rows)
    assert signal and signal["reference"] == "om" and signal["driver"] == "solar"


def test_signaal_is_none_op_een_leeg_archief():
    assert sa.recalibration_signal([]) is None


def test_drift_message_noemt_graden_en_bewijs():
    signal = sa.recalibration_signal(_reeks(slope=sa.SOLAR_BIAS_SLOPE + 0.0015))
    msg = sa.drift_message(signal)
    assert "SOLAR_BIAS_SLOPE" in msg and "wu_bias.py" in msg
    assert f"{sa.REFERENCE_WM2:.0f} W/m²" in msg
    assert "forward-chaining" in msg


def test_protocol_report_zegt_ook_iets_als_er_niets_te_melden_is():
    rows = _reeks(slope=sa.SOLAR_BIAS_SLOPE)
    assert "Geen herijking nodig" in sa.protocol_report(rows, None)
    signal = sa.recalibration_signal(_reeks(slope=sa.SOLAR_BIAS_SLOPE + 0.0015))
    assert "kan herijkt worden" in sa.protocol_report(rows, signal)


def test_workflow_draait_maandelijks_en_heeft_een_dagen_fallback():
    """Zonder fallback krijgt de cron een lege ACCURACY_DAYS (inputs bestaan daar
    niet) en crasht int('') de run."""
    wf = (pathlib.Path(__file__).resolve().parents[1]
          / ".github" / "workflows" / "station-accuracy.yml").read_text(encoding="utf-8")
    assert "schedule:" in wf and "cron:" in wf
    assert "timezone: \"Europe/Amsterdam\"" in wf
    assert "inputs.days || " in wf

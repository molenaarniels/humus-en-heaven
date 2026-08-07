"""KNMI-referentie (fase 1): eenheden, tijdconventie en robuustheid.

De parser is geschreven tegen de *gedocumenteerde* vorm van de KNMI-scriptservice
— de ontwikkelomgeving mag knmi.nl niet bereiken, dus deze tests pinnen de aanname
vast in plaats van de waarneming. `tools/knmi_probe.py` levert de meting; wijkt de
echte respons af, dan is dit het bestand dat mee moet veranderen.
"""
import pytest

import knmi_ref


def _rec(day="2026-08-01", hour=13, **kw):
    return {"station_code": 260, "date": day, "hour": hour,
            "T": 213, "U": 61, "FH": 40, "Q": 180, "N": 4, **kw}


# ── Eenheden ──────────────────────────────────────────────────────────────────

def test_eenheden_worden_omgerekend():
    row = knmi_ref.parse_hourly([_rec()])["2026-08-01T13"]
    assert row["temp"] == pytest.approx(21.3)          # 0.1 °C → °C
    assert row["rh"] == 61                             # % blijft %
    assert row["wind"] == pytest.approx(4.0 * 3.6)     # 0.1 m/s → km/h (Project 7 rekent in km/h)
    # Q is J/cm² over het hele uur, niet een momentaan vermogen: 180 J/cm² =
    # 1.8e6 J/m² over 3600 s = 500 W/m² gemiddeld.
    assert row["solar"] == pytest.approx(500.0)
    assert row["cloud"] == pytest.approx(50.0)         # 4 octanten → 50%


def test_octant_9_is_geen_bedekkingsgraad():
    """9 = 'bovenlucht onzichtbaar' (mist/zware neerslag), geen 112% bewolking."""
    assert knmi_ref.parse_hourly([_rec(N=9)])["2026-08-01T13"]["cloud"] is None
    assert knmi_ref.parse_hourly([_rec(N=0)])["2026-08-01T13"]["cloud"] == 0.0
    assert knmi_ref.parse_hourly([_rec(N=8)])["2026-08-01T13"]["cloud"] == 100.0


# ── Tijdconventie ─────────────────────────────────────────────────────────────

def test_uur_1_is_01_uur_utc():
    """KNMI-uur h beslaat (h−1, h] UT met de meting aan het eind; onze sleutel is
    dezelfde momentopname-op-het-hele-uur als `fetch_om_archive_hourly` maakt."""
    assert list(knmi_ref.parse_hourly([_rec(hour=1)])) == ["2026-08-01T01"]


def test_uur_24_rolt_naar_de_volgende_dag():
    """De klassieke valkuil: uur 24 bestaat en is middernacht van de dag erná.
    Zonder deze rol landt elke laatste rij van de dag op een niet-bestaande 24e uur
    of overschrijft hij uur 00 van dezelfde dag."""
    assert list(knmi_ref.parse_hourly([_rec(hour=24)])) == ["2026-08-02T00"]


def test_hour_shift_is_expliciet_en_werkt(monkeypatch):
    """De WU-emmer (`tempAvg` over het uur) en beide referenties (momentopname op
    het hele uur) staan een half uur uit elkaar — een scheefheid die er altijd al
    in zat. `HOUR_SHIFT` maakt het toetsbaar in plaats van aanneembaar."""
    monkeypatch.setattr(knmi_ref, "HOUR_SHIFT", -1)
    assert list(knmi_ref.parse_hourly([_rec(hour=13)])) == ["2026-08-01T12"]


def test_compacte_datumnotatie_wordt_ook_gelezen():
    assert list(knmi_ref.parse_hourly([_rec(day="20260801", hour=6)])) == ["2026-08-01T06"]
    assert list(knmi_ref.parse_hourly([{"YYYYMMDD": "20260801", "HH": 6, "T": 150}])) \
        == ["2026-08-01T06"]


# ── Robuustheid ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kapot", [
    {"T": None}, {"T": ""}, {"hour": None}, {"date": None}, {"date": "geen-datum"},
])
def test_onbruikbare_rijen_vallen_af(kapot):
    assert knmi_ref.parse_hourly([_rec(**kapot)]) == {}


def test_ontbrekende_nevenvelden_zijn_geen_probleem():
    """Temperatuur is het veld waar alles om draait; de rest mag ontbreken —
    anders kost één uitgevallen stralingssensor de hele referentie."""
    row = knmi_ref.parse_hourly([{"date": "2026-08-01", "hour": 5, "T": 150}])["2026-08-01T05"]
    assert row["temp"] == pytest.approx(15.0)
    assert row["rh"] is None and row["wind"] is None and row["solar"] is None


def test_lege_invoer():
    assert knmi_ref.parse_hourly([]) == {} and knmi_ref.parse_hourly(None) == {}


def test_stringgetallen_worden_geaccepteerd():
    """De scriptservice levert getallen soms als string (fmt-afhankelijk)."""
    row = knmi_ref.parse_hourly([_rec(T="213", Q="180")])["2026-08-01T13"]
    assert row["temp"] == pytest.approx(21.3) and row["solar"] == pytest.approx(500.0)


# ── Fetch-laag (zonder netwerk) ───────────────────────────────────────────────

def test_fetch_bouwt_het_uurvenster_en_parset(monkeypatch):
    gezien = {}

    def fake_get_json(url, params, **kw):
        gezien.update(url=url, params=params)
        return [_rec(hour=h) for h in (1, 2)]

    monkeypatch.setattr(knmi_ref, "get_json", fake_get_json)
    hours = knmi_ref.fetch_hourly("2026-08-01", "2026-08-03")
    assert gezien["params"]["start"] == "2026080101"    # eerste uur van de startdag
    assert gezien["params"]["end"] == "2026080324"      # laatste uur van de einddag
    assert gezien["params"]["stns"] == str(knmi_ref.DE_BILT)
    assert sorted(hours) == ["2026-08-01T01", "2026-08-01T02"]


def test_fetch_accepteert_dict_respons(monkeypatch):
    """Sommige KNMI-endpoints wikkelen de rijen in {"data": [...]}."""
    monkeypatch.setattr(knmi_ref, "get_json", lambda *a, **k: {"data": [_rec()]})
    assert list(knmi_ref.fetch_hourly("2026-08-01", "2026-08-01")) == ["2026-08-01T13"]


def test_de_bilt_is_het_juiste_station():
    """260 = De Bilt in KNMI's klimatologienummering (WMO 06260), 4,1 km van de tuin.
    Niet 310 (Vlissingen) — die verwisseling zwerft door online voorbeelden."""
    assert knmi_ref.DE_BILT == 260

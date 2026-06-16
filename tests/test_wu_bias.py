"""Tests voor wu_bias — de zon-gedreven WU-temperatuurcorrectie.

Pure functies, geen netwerk: correct_temp (bron-agnostische aftrek) en
bias_estimate (diagnostisch veld). Geijkt door Project 7.
"""

import wu_bias
from wu_bias import SOLAR_BIAS_SLOPE, bias_estimate, correct_temp


def test_none_temp_geeft_none():
    assert correct_temp(None, 700) is None


def test_none_instraling_laat_temp_ongewijzigd():
    # Geen instralingsdriver beschikbaar → geen correctie (geen gok).
    assert correct_temp(24.0, None) == 24.0


def test_nacht_basislijn_blijft_staan():
    # 's Nachts (0 W/m²) verdwijnt het surplus, dus geen aftrek.
    assert correct_temp(12.0, 0.0) == 12.0


def test_negatieve_instraling_geklemd_op_nul():
    # Negatieve instraling is onzin → geklemd, dus geen correctie.
    assert correct_temp(20.0, -50.0) == 20.0


def test_zon_trekt_surplus_af():
    assert correct_temp(25.0, 700.0) == 25.0 - SOLAR_BIAS_SLOPE * 700.0


def test_monotoon_in_instraling():
    # Meer zon → sterkere (warme) bias → lagere gecorrigeerde temp.
    laag = correct_temp(25.0, 200.0)
    hoog = correct_temp(25.0, 800.0)
    assert hoog < laag < 25.0


def test_correct_temp_is_temp_minus_bias_estimate():
    # De twee functies moeten consistent zijn (zelfde helling, zelfde clamp).
    for solar in (0.0, 150.0, 640.0, 1000.0):
        assert correct_temp(22.0, solar) == 22.0 - bias_estimate(solar)


def test_bias_estimate_nul_en_none():
    assert bias_estimate(0.0) == 0.0
    assert bias_estimate(None) == 0.0
    assert bias_estimate(-100.0) == 0.0


def test_bias_estimate_schaalt_lineair():
    assert bias_estimate(200.0) == SOLAR_BIAS_SLOPE * 200.0
    # Verdubbel de instraling → verdubbel de geschatte bias.
    assert bias_estimate(400.0) == 2 * bias_estimate(200.0)


def test_slope_is_positief_en_klein():
    # Vangnet tegen een per-ongeluk omgekeerd teken of verkeerde schaal bij
    # herkalibratie (°C per W/m², zou ~0.004 moeten zijn).
    assert 0.0 < wu_bias.SOLAR_BIAS_SLOPE < 0.05

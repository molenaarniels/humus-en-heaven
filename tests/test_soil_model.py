"""Regression safety net for the soil science core (`soil_model.py`).

Two kinds of checks, both aimed at catching *silent drift* in the FAO-56
implementation if the formulas are ever edited:

1. A textbook anchor — ET0 reproduced against the published FAO-56 Example 17.
2. Physical invariants of the dual-Kc water balance (θ bounds, monotonic
   drought depletion, refill-to-field-capacity, root-zone mass balance).

These intentionally do NOT touch the network, the Gist, or data.json — they
exercise the pure model functions with synthetic inputs only.
"""
import math
from datetime import date, timedelta

import pytest

from soil_model import (
    KC_MAX,
    SOIL_FC,
    SOIL_WP,
    ZONES,
    penman_monteith_et0,
    run_water_balance,
)

EPS = 1e-9


# ---------------------------------------------------------------------------
# ET0 — FAO-56 Penman-Monteith
# ---------------------------------------------------------------------------

def test_et0_matches_fao56_example_17():
    """FAO-56 (Allen et al. 1998) Example 17 — daily ETo for a location at
    50°48'N, 100 m elevation, 6 July (doy 187).

    Published inputs: Tmax 21.5 °C, Tmin 12.3 °C, RHmax 84 % / RHmin 63 %
    (→ ea 1.409 kPa, es 1.997 kPa), wind 10 km/h at 10 m (→ u2 2.078 m/s),
    Rs 22.07 MJ/m²/day. Published result: ETo ≈ 3.9 mm/day (3.88 with the
    unrounded intermediates, which is what we reproduce).
    """
    Tmax, Tmin = 21.5, 12.3
    # The function recomputes es from Tmax/Tmin; back out the RHmean that
    # reproduces the example's ea = 1.409 kPa.
    e_tmax = 0.6108 * math.exp(17.27 * Tmax / (Tmax + 237.3))
    e_tmin = 0.6108 * math.exp(17.27 * Tmin / (Tmin + 237.3))
    es = (e_tmax + e_tmin) / 2
    RHmean = 1.409 / es * 100

    et0 = penman_monteith_et0(
        Tmax=Tmax, Tmin=Tmin, RHmean=RHmean,
        u2=2.078, Rs=22.07, elev=100, lat_rad=math.radians(50.80), doy=187,
    )
    assert et0 == pytest.approx(3.88, abs=0.05)


def test_et0_never_negative_on_cold_dark_day():
    """`max(num/den, 0)` guard: a cold, low-radiation winter day where net
    longwave loss exceeds shortwave gain must clamp to 0, never go negative."""
    et0 = penman_monteith_et0(
        Tmax=2.0, Tmin=-5.0, RHmean=92, u2=1.0, Rs=1.0,
        elev=5, lat_rad=math.radians(52.1), doy=15,
    )
    assert et0 >= 0.0


# ---------------------------------------------------------------------------
# Water balance — synthetic series helpers
# ---------------------------------------------------------------------------

def make_series(n, et0, precip, *, start="2026-05-01", tmean=18.0):
    """Build an `n`-day series. `et0`/`precip` may be scalars or per-day lists.
    tmean=18 keeps temp_factor=1 so cold suppression doesn't confound tests."""
    d0 = date.fromisoformat(start)
    series = []
    for i in range(n):
        e = et0[i] if isinstance(et0, list) else et0
        p = precip[i] if isinstance(precip, list) else precip
        series.append({
            "date": (d0 + timedelta(days=i)).isoformat(),
            "ET0": e, "precip": p, "Tmean": tmean,
        })
    return series


def water_from_theta(theta, zone):
    return (theta - SOIL_WP) * zone["Zr"] * 1000


@pytest.mark.parametrize("zone_key", ["lawn", "shrubs"])
def test_theta_stays_within_physical_bounds(zone_key):
    """θ must never leave [WP, FC] and depletion must stay in [0, 100] %,
    across a long run of mixed wet/dry/hot days."""
    zone = ZONES[zone_key]
    et0 = [2.0, 5.0, 0.5, 6.0, 3.0] * 18           # varied demand, 90 days
    precip = [0, 0, 25, 0, 0, 0, 80, 0, 0, 0] * 9  # occasional big events
    out = run_water_balance(make_series(90, et0, precip), zone, zone_key)
    for row in out:
        assert SOIL_WP - EPS <= row["theta"] <= SOIL_FC + EPS
        assert -EPS <= row["depletion_pct"] <= 100 + EPS


@pytest.mark.parametrize("zone_key", ["lawn", "shrubs"])
def test_drought_depletes_monotonically(zone_key):
    """No rain + steady demand → θ is non-increasing every day and ends drier
    than it started, bottoming out at (but never below) wilting point."""
    zone = ZONES[zone_key]
    out = run_water_balance(make_series(40, et0=5.0, precip=0), zone, zone_key,
                            seed_theta=SOIL_FC)  # start full
    thetas = [r["theta"] for r in out]
    for prev, cur in zip(thetas, thetas[1:]):
        assert cur <= prev + EPS
    assert thetas[-1] < thetas[0]
    assert thetas[-1] >= SOIL_WP - EPS


@pytest.mark.parametrize("zone_key", ["lawn", "shrubs"])
def test_heavy_rain_refills_to_field_capacity_and_drains(zone_key):
    """A large rain event on dry soil pushes θ up to field capacity and the
    excess above FC leaves as drainage."""
    zone = ZONES[zone_key]
    out = run_water_balance(make_series(2, et0=2.0, precip=[0, 120]), zone,
                            zone_key, seed_theta=SOIL_WP + 0.005)
    wet_day = out[1]
    assert wet_day["theta"] == pytest.approx(SOIL_FC, abs=1e-3)
    assert wet_day["drainage"] > 0


@pytest.mark.parametrize("zone_key", ["lawn", "shrubs"])
def test_rootzone_mass_balance(zone_key):
    """Conservation: Δ(root-zone water) == wetting − T − E − drainage each day
    (wetting = precip − interception + irrigation), except on days the bucket
    hits the wilting-point floor (where the true loss is truncated)."""
    zone = ZONES[zone_key]
    et0 = [3.0] * 30
    precip = [0, 0, 12, 0, 0, 6, 0, 0, 0, 20] * 3  # interior-only moderate
    out = run_water_balance(make_series(30, et0, precip), zone, zone_key,
                            seed_theta=SOIL_FC - 0.02)
    for prev, cur, day in zip(out, out[1:], make_series(30, et0, precip)[1:]):
        if cur["theta"] <= SOIL_WP + 1e-4:
            continue  # floor clamp truncates the loss; balance won't close
        dw = water_from_theta(cur["theta"], zone) - water_from_theta(prev["theta"], zone)
        wetting = (day["precip"] - cur["interception"]) + cur["irrigation"]
        expected = wetting - cur["T"] - cur["E"] - cur["drainage"]
        assert dw == pytest.approx(expected, abs=0.1)


@pytest.mark.parametrize("seed", [0.50, 0.0, -1.0])
def test_seed_theta_is_clamped_into_valid_range(seed):
    """An out-of-range carry-over seed (e.g. corrupt state) must be clamped to
    [WP, FC] rather than producing an impossible starting θ."""
    zone = ZONES["lawn"]
    out = run_water_balance(make_series(1, et0=0.0, precip=0), zone, "lawn",
                            seed_theta=seed)
    assert SOIL_WP - EPS <= out[0]["theta"] <= SOIL_FC + EPS


def test_ke_bounded_by_kc_max():
    """Surface-evaporation coefficient Ke must never exceed Kc_max (FAO-56
    Eq. 72 ceiling) on any day of a wet/dry cycle."""
    zone = ZONES["lawn"]
    precip = [0, 0, 0, 15, 0, 0, 0, 0] * 4
    out = run_water_balance(make_series(32, et0=4.0, precip=precip), zone, "lawn")
    for row in out:
        assert row["Ke"] <= KC_MAX + EPS

#!/usr/bin/env python3
"""
vent_forecast.py — de 12-uurs vooruitblik van de ventilatie-tweeling (Project 13).

Twee dingen, allebei zuivere functies over gewone dicts:

1. **De geankerde voorspelling** (`anchor_seed` + `forecast`). De kalibratie-sim start
   72 uur terug (WARMUP_H + CALIB_WINDOW_H) en integreert vooruit; op "nu" staat hij dus
   op een *gesimuleerde* toestand, niet op de gemeten. Voor een nowcast maakt dat weinig
   uit — de fit trekt de baan naar de metingen toe — maar een 12u-voorspelling erft die
   restafwijking als een bijna zuivere offset over de hele horizon. De voorspelling
   herankert daarom op "nu": de luchtknoop gaat naar de laatste tado-meting (via
   `air_from_sensor`, want de sim-knoop is de wáre lucht en de sensor leest gebiasd) en
   **de massaknoop schuift met dezelfde delta mee**.

   Die tweede helft is het punt. De (Ta − Tm)-differentie codeert de thermische toestand —
   laadt de kamer op of ontlaadt hij — en die is het waard om te houden; alleen het
   absolute niveau is scheef. Alleen de lucht herankeren laat de massaknoop tot ~0.6 °C
   inconsistent staan, waarna de sterke `h_am`-koppeling de luchtknoop binnen één of twee
   stappen weer van de waarneming af trekt (h=1 RMSE 1.76 → 0.48 in het offline harnas).
   Dezelfde redenering als `night_forecast.anchor_mass_now`, maar dáár als gedempt
   gemiddelde over de metingen; hier als delta-verschuiving, omdat de kalibratie-sim zijn
   massaknoop al uit de metingen zaait (`tm_seed`) en dus alleen zijn *drift* mist.

2. **De browser-payload** (`driver_export`). De helft van `vent_physics` die NIET van de
   raamstand afhangt — zonnegeometrie, hoekafhankelijke glastransmissie, beschaduwing,
   dak-instraling, het buur- en bodemanker — is een functie van tijd en weer alleen. Die
   rekenen we hier één keer uit en verschepen we als tijdlijn; de speeltuin hoeft dan bij
   een toggle alleen nog `standen → luchtstroom → RC-stelsel` te herrekenen. Dat is wat de
   JS-kern (`docs/js/vent_core.js`) doet, en de golden-vector (`tools/export_driver_timeline.py`)
   pint 'm vast op deze Python.

Geen I/O, geen netwerk: `vent_twin.py` en `tools/export_driver_timeline.py` voeren de
loaders aan en schrijven het resultaat weg.
"""

from __future__ import annotations

import math

import vent_physics as vp
from vent_physics import RunContext, air_from_sensor

# Vooruitblik van de tweeling (uur). 12u dekt "hoe warm is het vannacht / morgenochtend?"
# — de vraag waarvoor het dashboard bestaat. Verder vooruit kán (de fout plateaut: 24u is
# maar ~8 % slechter dan 12u) maar leunt zwaarder op de weersvoorspelling, die in geen
# enkel getal hier verrekend is (zie de aanname bij tools/horizon_backtest.py).
FORECAST_H = 12.0

# Hoeveel VERLEDEN de grafieken tonen. Het dashboard zet de 12u-vooruitblik tegen een etmaal
# historie (één volledige dag-nachtcyclus als context); de speeltuin toont alleen de directe
# aanloop, want daar gaat het om het verschil tussen scenario's, niet om de geschiedenis.
DASHBOARD_PAST_H = 24.0
PLAYGROUND_PAST_H = 3.0

# Hoe oud een tado-meting maximaal mag zijn om als anker te dienen. Verder terug is het geen
# "wat weet ik nú" meer; dan is de gesimuleerde toestand eerlijker. Zelfde grootte-orde als
# night_forecast's staleness-grens.
ANCHOR_MAX_AGE_H = 1.0

def latest_actual(actual: dict, now, max_age_h: float = ANCHOR_MAX_AGE_H) -> dict:
    """{kamer: laatst gemeten °C} voor metingen niet ouder dan `max_age_h`. Kamers zonder
    verse meting ontbreken — die houden hun gesimuleerde waarde (fail open)."""
    out = {}
    for rid, samples in (actual or {}).items():
        for t, v in reversed(samples):
            if v is None:
                continue
            age = (now - t).total_seconds() / 3600.0
            if 0.0 <= age <= max_age_h:
                out[rid] = v
            break
    return out

def anchor_seed(house: dict, sim: dict, measured_now: dict, t_out0: float | None,
                tm_mode: str = "shift") -> tuple[dict, dict, int]:
    """Bouw het (Ta, Tm)-zaad voor de vooruitblik door op de meting op "nu" te herankeren.

    `sim` = de uitvoer van de kalibratie-`simulate()` mét `snapshot_t=now` (levert
    `Ta_now`/`Tm_now`). `measured_now` = {kamer: gemeten sensor-°C}. `tm_mode="warm"` laat de
    massaknoop op zijn sim-waarde staan — de naïeve variant, alleen als ablatie bedoeld.

    Geeft `(seed_Ta, seed_Tm, n_verankerd)`. Zones zonder meting (junctions, kamers zonder
    verse sample) houden hun sim-waarde.
    """
    ta_now = sim.get("Ta_now") or sim.get("Ta") or {}
    tm_now = sim.get("Tm_now") or sim.get("Tm") or {}
    seed, tm = dict(ta_now), dict(tm_now)
    anchored = 0
    for rid, v in (measured_now or {}).items():
        if rid not in house.get("rooms", {}):
            continue
        ta_new = air_from_sensor(house, rid, v, t_out0)
        if ta_new is None:
            continue
        delta = ta_new - ta_now.get(rid, ta_new)
        seed[rid] = ta_new
        if tm_mode == "shift" and rid in tm:
            tm[rid] = tm[rid] + delta
        anchored += 1
    return seed, tm, anchored

def forecast(house: dict, params: dict, timeline: list[dict], sim: dict, now,
             ctx: RunContext, measured_now: dict, *, measured: dict | None = None,
             tm_mode: str = "shift") -> dict:
    """Simuleer [now, einde van `timeline`] vanaf de geankerde toestand.

    Geeft `{"steps": [...], "series": {kamer: [(t, sensor-°C)]}, "air": {kamer: [(t, Ta)]},
    "seed_Ta": {...}, "seed_Tm": {...}, "anchored": n}`. Lege dicts als de tijdlijn geen
    toekomst bevat.
    """
    fore = [s for s in timeline if s["t"] >= now]
    if len(fore) < 2:
        return {"steps": [], "series": {}, "air": {}, "seed_Ta": {}, "seed_Tm": {},
                "anchored": 0}
    seed, tm, anchored = anchor_seed(house, sim, measured_now, fore[0]["T_out"], tm_mode)
    fc = vp.simulate(house, params, fore, seed, ctx, tm_seed=tm, measured=measured)
    # NB de labelconventie van `simulate`: een punt draagt het tijdstip van het BEGIN van de
    # stap maar de temperatuur ná die stap — een vaste 15-minuten-verschuiving die door de hele
    # tweeling loopt (de fit interpoleert zijn residuen op dezelfde reeksen, dus hij is erin
    # verdisconteerd). Het zaad hier apart vooraan plakken zou een tweede punt op fore[0]["t"]
    # zetten en die conventie doorbreken; de reeks begint dus gewoon op `now` met de waarde één
    # stap ná het anker.
    series = {rid: vp._to_sensor_series(house, fore, rid, s)
              for rid, s in fc["series"].items()}
    return {"steps": fore, "series": series, "air": fc["series"],
            "seed_Ta": seed, "seed_Tm": tm, "anchored": anchored,
            "solver_failures": fc.get("solver_failures", 0)}

# ── De browser-payload ────────────────────────────────────────────────────────────────

def operable_elements(house: dict) -> list[tuple]:
    """Elementen die écht lucht kunnen doorlaten — vast glas (max_open_area 0) valt af.
    De VOLGORDE is een contract: hij zet de kolomvolgorde van de luchtstroom-surrogaat vast
    (`docs/js/surrogate.json`, `input_cols`). Wijzigt de geometrie hier, dan moet het
    surrogaat opnieuw gedistilleerd worden — `tools/surrogate_backtest.py` slaat alarm."""
    out = []
    for wid, w in house.get("windows", {}).items():
        if w.get("max_open_area_m2", 0.0) > 0:
            out.append((wid, "window"))
    for vid, v in house.get("vents", {}).items():
        if v.get("max_open_area_m2", v.get("area_m2", 0.0)) > 0:
            out.append((vid, "vent"))
    for did, d in house.get("doors", {}).items():
        if d.get("area_m2", 0.0) > 0:
            out.append((did, "door"))
    return out

def zone_list(house: dict) -> list[str]:
    return list(house.get("rooms", {})) + list(house.get("junctions", {}))

def door_pairs(house: dict) -> list[tuple]:
    return [tuple(sorted(d["between"])) for d in house.get("doors", {}).values()
            if d.get("area_m2", 0.0) > 0]

def _driver_step(house: dict, zones: list[str], ctx: RunContext, s: dict) -> dict:
    """Eén tijdstap gereduceerd tot wat de browser niet zelf kan uitrekenen.

    **Niets hier wordt afgerond.** Elk getal in deze dict gaat rechtstreeks het RC-stelsel in,
    en de golden-vector eist dat de JS-kern `vp.simulate` tot ~1e-9 °C reproduceert. Afronden
    op 3 decimalen (een voor de hand liggende bytebesparing) tilt de portfout naar ~6e-5 °C —
    ruim onder wat je ziet, maar precies genoeg om de poort waardeloos te maken: die kan dan
    geen écht portverschil meer onderscheiden van afrondingsruis. De paar tientallen kB zijn
    dat niet waard.
    """
    night = s.get("sun_el", 90.0) <= 0.0
    irr_roof = s.get("irr_roof", {})
    return {
        "t": s["t"].isoformat(),
        "dt": s["dt"],
        "T_out": s["T_out"],
        "rh": (s.get("weather") or {}).get("rh"),
        "sun_el": s.get("sun_el", 0.0),
        "sun_az": s.get("sun_az", 0.0),
        "irr": {z: s["irr"].get(z, 0.0) for z in zones},
        # Sol-air-temperatuur van het dak: buitentemp + zonabsorptie − nachtelijke
        # hemelstraling. Vooraf gerekend omdat de browser de dak-instraling niet kent.
        "t_solair": {z: (s["T_out"] + vp.ROOF_SOLAR_GAIN * irr_roof.get(z, 0.0)
                         - (vp.ROOF_SKY_COOLING if night else 0.0))
                     for z in zones},
        "nb_now": vp.neighbor_at(ctx.neighbor_temp, s["t"]),
        "int_profile": vp.internal_gain_profile(s["t"]),
        "wind_speed": (s.get("weather") or {}).get("wind_speed", 0.0),
        "wind_dir": (s.get("weather") or {}).get("wind_dir", 0.0),
        # Alleen de string-standen: `ac_room`/`paused` zijn boekhouding, geen openingen.
        "states": {k: v for k, v in s["states"].items() if isinstance(v, str)},
    }

def driver_export(house: dict, params: dict, ctx: RunContext, steps: list[dict],
                  seed_ta: dict, seed_tm: dict, *, now=None,
                  past_steps: list[dict] | None = None,
                  actual_series: dict | None = None) -> dict:
    """Alles wat `docs/js/vent_core.js` nodig heeft om de tweeling in de browser te draaien.

    `steps` = de toekomst-stappen (vanaf nu), `past_steps` = de getoonde aanloop ervóór
    (alleen als drivers; de browser simuleert die niet opnieuw). `actual_series` =
    {kamer: [(t, °C)]} gemeten historie voor de context-lijn in de grafiek.
    """
    zones = zone_list(house)
    par = vp._zone_thermal_params(house, params)
    ginter = vp.interzone_conductances(house, params)

    elem_spec = []
    for eid, kind in operable_elements(house):
        e = (house.get("windows", {}).get(eid) or house.get("vents", {}).get(eid)
             or house.get("doors", {}).get(eid))
        elem_spec.append({
            "id": eid, "kind": kind,
            # De numerieke fractie per stand-string, vooraf opgelost door de échte
            # `_open_frac`/`_default_frac` — zo hoeft de browser vent_io's standen-parser
            # niet te porten en kan hij er ook niet van afdrijven.
            "frac": {st: vp._open_frac(st, e) for st in ("open", "dicht", "half", "kier", "tilt")},
            "default": vp._default_frac(e, kind),
            "room": e.get("room"), "between": e.get("between"),
            "label": e.get("label", eid),
        })

    doors = [{"id": did, "a": d["between"][0], "b": d["between"][1],
              "area_m2": d.get("area_m2", 0.0),
              "center_height_m": d.get("center_height_m", 0.0)}
             for did, d in house.get("doors", {}).items() if d.get("area_m2", 0.0) > 0]

    sensor_rooms = [rid for rid, r in house.get("rooms", {}).items()
                    if r.get("from_window_data")]

    return {
        "zones": zones,
        "sensor_rooms": sensor_rooms,
        "room_labels": {rid: r.get("label", rid) for rid, r in house.get("rooms", {}).items()},
        "volumes": {z: (house.get("rooms", {}).get(z)
                        or house.get("junctions", {}).get(z, {})).get("volume_m3", 1.0)
                    for z in zones},
        "elements": elem_spec,
        "doors": doors,
        "strat": vp._stratify_zones(house),
        "consts": {
            "BUOY_EXCH_C": vp.BUOY_EXCH_C, "G": vp.G,
            "DOOR_HEIGHT_M": vp.DOOR_HEIGHT_M,
            "STAIR_STRAT_MAX_GRAD": vp.STAIR_STRAT_MAX_GRAD,
            # de Gids & Phaff eenzijdige-ventilatie-correlatie. Exact in JS gerekend i.p.v.
            # gedistilleerd: hij is gesloten-vorm, en `effective_fresh`'s
            # max(netto, eenzijdig) is een discontinuïteit die een gladde MLP niet kán
            # weergeven — het surrogaat was het daardoor in 18 % van de gevallen oneens met
            # de solver over de RICHTING van een opening (invariant B, 82 % → 95.7 % na de
            # splitsing).
            "SS_C1": vp.SS_C1, "SS_C2": vp.SS_C2, "SS_C3": vp.SS_C3,
            "SS_MIN_TILT_DEG": vp.SS_MIN_TILT_DEG,
        },
        # Ramen die voor de eenzijdige term in aanmerking komen, met alles wat de formule wil.
        "ss_windows": [
            {"id": wid, "room": w.get("room"),
             "max_open_area_m2": w.get("max_open_area_m2", 0.0),
             "open_height_m": (w.get("open_height_m")
                               or math.sqrt(max(w.get("max_open_area_m2", 0.0), 1e-6)))}
            for wid, w in house.get("windows", {}).items()
            if w.get("room") and w.get("tilt_deg", 90.0) >= vp.SS_MIN_TILT_DEG
            and w.get("max_open_area_m2", 0.0) > 0
        ],
        "sensor_outdoor_frac": {rid: r.get("sensor_outdoor_frac", 0.0)
                                for rid, r in house.get("rooms", {}).items()},
        "par": par,
        "ginter": [{"a": a, "b": b, "g": g} for (a, b), g in ginter.items()],
        "ground_temp": ctx.ground_temp,
        "neighbor_temp": ctx.neighbor_temp,
        "vent_eff": params.get("vent_eff", 1.0),
        "cp_shelter": params.get("cp_shelter", vp.PRIORS["cp_shelter"]),
        "rho_cp": 1.2 * vp.CP_AIR,
        "substep_s": vp.SUBSTEP_S,
        "t0": now.isoformat() if now is not None else (steps[0]["t"].isoformat() if steps else None),
        # De toestand op voorspelmoment: luchtknoop uit de meting, massaknoop mee-verschoven.
        "seed_Ta": dict(seed_ta),
        "seed_Tm": dict(seed_tm),
        "steps": [_driver_step(house, zones, ctx, s) for s in steps],
        # Aanloop: alleen buitentemp + de gemeten kamertemps, zodat de speeltuingrafiek
        # context heeft zonder dat de browser het verleden hoeft na te rekenen.
        "past": [{"t": s["t"].isoformat(), "T_out": s["T_out"]} for s in (past_steps or [])],
        "actual": {rid: [{"t": t.isoformat(), "temp": v} for t, v in s]
                   for rid, s in (actual_series or {}).items()},
    }

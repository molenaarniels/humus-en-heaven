#!/usr/bin/env python3
"""Draai het gedistilleerde luchtstroom-surrogaat in plaats van de Newton-druksolver.

Dit is de referentie-implementatie van wat de JS-runtime doet, en hij bestaat zodat het
surrogaat **end-to-end** te testen is — binnen de echte thermische simulatie — in plaats van
alleen tegen achtergehouden solver-samples. Die twee zijn niet hetzelfde: per-sample-fout
middelt grotendeels uit over een rollout, omdat de RC-tijdconstanten luchtstroomruis dempen.

Elke tijdstap in `vp.simulate` doet:

    ops   = build_openings(...)        # geometrie + winddruk — goedkoop, geen iteratie
    net   = solve_network(...)         # gedempte Newton met numerieke Jacobiaan — duur
    fresh = effective_fresh(...)       # + de eenzijdige term
    mix   = door_mix(...)

`build_openings` blijft (goedkoop, en `ops` wordt stroomafwaarts hergebruikt voor de
koker-deuroppervlakken — bovendien port hij triviaal naar JS, er zit geen solver in).
Het surrogaat vervangt de andere drie.

`install()` monkey-patcht die drie. `effective_fresh` is waar het surrogaat daadwerkelijk
draait, want dán zijn al zijn invoeren in scope; het door_mix-resultaat wordt daar gecachet
en doorgegeven aan de gepatchte `door_mix`, die simulate meteen erna aanroept.

Vereist numpy — dit is offline gereedschap, geen runtime-afhankelijkheid van de pipelines
(zie requirements-tools.txt). De browser heeft niets nodig: docs/js/vent_core.js doet
dezelfde rekensom in kaal JS.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vent_physics as vp                                      # noqa: E402
from vent_forecast import operable_elements, zone_list         # noqa: E402

DEFAULT_WEIGHTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "docs", "js", "surrogate.json")


def _unpack(a):
    """Gewichten zijn óf gewone geneste lijsten óf base64-float16 (zie --fp16). De JS-runtime
    doet exact dezelfde decode; houd de twee in de pas."""
    if isinstance(a, dict) and "__f16" in a:
        import base64
        buf = np.frombuffer(base64.b64decode(a["__f16"]), dtype=np.float16)
        return buf.reshape(a["shape"]).astype(np.float64)
    return np.asarray(a, dtype=np.float64)


def _frac_of(house, states, eid, kind):
    """De openingsfractie die `build_openings` voor dit element zal gebruiken. Een element dat
    níét in `states` staat valt terug op zijn eigen default — replay-snapshots dragen niet elk
    element, en states[eid] rechtstreeks lezen zou een ándere fractie opschrijven dan de solver
    gebruikte."""
    elem = (house.get("windows", {}).get(eid) or house.get("vents", {}).get(eid)
            or house.get("doors", {}).get(eid))
    if eid in states:
        return vp._open_frac(states[eid], elem)
    return vp._default_frac(elem, kind)


class SurrogateAirflow:
    def __init__(self, weights_path, house):
        with open(weights_path, encoding="utf-8") as f:
            w = json.load(f)
        self.W = [_unpack(L["W"]) for L in w["layers"]]
        self.b = [_unpack(L["b"]) for L in w["layers"]]
        self.x_mu = _unpack(w["x_mu"])
        self.x_sd = _unpack(w["x_sd"])
        self.y_scale = _unpack(w["y_scale"])
        self.out_cols = w["output_cols"]
        self.house = house
        self.elems = operable_elements(house)
        self.zones = zone_list(house)
        self.n_zone = len(self.zones)

        expect = [f"open_{e}" for e, _ in self.elems] + \
                 ["wind_speed", "wind_dir_sin", "wind_dir_cos", "t_out", "cp_shelter"] + \
                 [f"tz_{z}" for z in self.zones]
        if w["input_cols"] != expect:
            raise SystemExit(
                "[surrogaat] kolomvolgorde wijkt af van house_model.json — de geometrie is "
                "veranderd sinds het surrogaat gedistilleerd werd. Her-distilleren met "
                "tools/airflow_distill.py + tools/train_surrogate.py.")

        # Map de gesorteerde deurpaar-kolommen van het surrogaat terug op de eigen
        # `between`-volgorde van elke deur — dát is de sleutelconventie die `door_mix`
        # oplevert en die de stratificatiecode stroomafwaarts verwacht.
        self.pair_key = {}
        for d in house.get("doors", {}).values():
            if d.get("area_m2", 0.0) > 0:
                a, b = d["between"]
                self.pair_key[tuple(sorted((a, b)))] = (a, b)
        self.mix_cols = []
        for j, c in enumerate(self.out_cols):
            if c.startswith("mix_"):
                a, b = c[len("mix_"):].split("__")
                self.mix_cols.append((j, self.pair_key.get((a, b), (a, b))))

    def _forward(self, x):
        h = (x - self.x_mu) / self.x_sd
        for i, (W, b) in enumerate(zip(self.W, self.b)):
            h = h @ W.T + b
            if i < len(self.W) - 1:
                h = h / (1.0 + np.exp(-h))      # SiLU
        return np.sinh(h) * self.y_scale

    def predict_net(self, states, weather, zone_temps, t_out, cp_shelter):
        """Alleen de surrogaat-uitvoer: het gedistilleerde Newton-nettodebiet, vóór de
        eenzijdige term erin gevouwen wordt."""
        return self._predict_raw(states, weather, zone_temps, t_out, cp_shelter)

    def predict(self, states, weather, zone_temps, t_out, cp_shelter):
        """Volledig `effective_fresh`-equivalent: max(gedistilleerd nettodebiet, EXACTE
        eenzijdige term). De eenzijdige term komt van de échte `vp.single_sided_fresh`, niet
        uit het netwerk — hij is gesloten-vorm, en de max() ertegen is een discontinuïteit die
        een gladde MLP niet kan weergeven (zie airflow_distill.py)."""
        fresh, mix = self._predict_raw(states, weather, zone_temps, t_out, cp_shelter)
        ss = vp.single_sided_fresh(self.house, states, weather, zone_temps, t_out)
        for z, v in (ss or {}).items():
            fresh[z] = max(fresh.get(z, 0.0), v)
        return fresh, mix

    def _predict_raw(self, states, weather, zone_temps, t_out, cp_shelter):
        ws = weather.get("wind_speed", 0.0) or 0.0
        wd = np.deg2rad(weather.get("wind_dir", 0.0) or 0.0)
        x = np.empty(len(self.elems) + 5 + self.n_zone)
        for i, (eid, kind) in enumerate(self.elems):
            x[i] = _frac_of(self.house, states, eid, kind)
        k = len(self.elems)
        x[k:k + 5] = (ws, np.sin(wd), np.cos(wd), t_out, cp_shelter)
        for i, z in enumerate(self.zones):
            x[k + 5 + i] = zone_temps.get(z, t_out)
        y = self._forward(x)
        fresh = {z: max(0.0, float(y[i])) for i, z in enumerate(self.zones)}
        mix = {}
        for j, key in self.mix_cols:
            v = max(0.0, float(y[j]))
            if v > 0:
                mix[key] = mix.get(key, 0.0) + v
        return fresh, mix


def install(house, cp_shelter, weights_path=DEFAULT_WEIGHTS):
    """Patch vp.solve_network / effective_fresh / door_mix. Geeft een restore() terug."""
    sur = SurrogateAirflow(weights_path, house)
    orig = (vp.solve_network, vp.effective_fresh, vp.door_mix)
    cache = {}

    def solve_network(zones, openings, zone_temps, outside_temp, P_init=None):
        # Het surrogaat neemt de solve over; geef de vorm terug die simulate verwacht. `P`
        # gaat er alleen als warme start weer in, dus None volstaat.
        return {"flows": [0.0] * len(openings), "fresh": {}, "pressures": {}, "P": None}

    def effective_fresh(fresh, house_, states, weather, zone_temps, outside_temp):
        f, m = sur.predict(states, weather, zone_temps, outside_temp, cp_shelter)
        cache["mix"] = m
        return f

    def door_mix(house_, flows, openings):
        return cache.get("mix", {})

    vp.solve_network = solve_network
    vp.effective_fresh = effective_fresh
    vp.door_mix = door_mix

    def restore():
        vp.solve_network, vp.effective_fresh, vp.door_mix = orig

    return restore

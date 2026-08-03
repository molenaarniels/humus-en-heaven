#!/usr/bin/env python3
"""
vent_twin.py — de runner van de ventilatie-tweeling (Project 13).

Elke kwartier-iteratie (airflow-notify.yml): weer + tado-temps + openingen-log
inlezen, één gedempte online-kalibratiestap (vent_fit.calibrate, tenzij de
anomalie-poort of de huis-pauze het venster wantrouwt), de voorspelling
simuleren (vent_physics.simulate) en het dashboard-artefact schrijven
(docs/vent_data.json + docs/vent_learned.json), plus de verse samples aan de
maand-shards appenden voor offline evaluatie/seeding.

Bewust NIET meegekomen uit de voorgangers (zie de plan-/assessment-docs):
suggest() (adviesluik), het checkpoint/fallback-complex, de leercurve-backfill
en de batch-verankering — elk groot incident kwam uit die vangnetten, niet uit
de fysica. Telegram: alléén de anomalie-nudge ("klopt de raamstand nog?") en
run_guarded's crash-alert.

Env: DRY_RUN=1 → nudges printen i.p.v. sturen (artefacten worden wél
geschreven); verder de secrets/paden van vent_io.
"""

import json
import os
import sys
from datetime import datetime, timezone

from notify import run_guarded, send_telegram
from shared_const import utc_now_iso

import vent_fit as vf
from vent_fit import (
    anomaly_step, calibrate, filter_ac_samples, filter_heating_samples,
    filter_paused_samples, naive_rmse, railed_params, rmse, should_nudge_anomaly,
    skill_score, thin_rmse_history, _residuals,
)
from vent_io import (
    CALIB_WINDOW_H, DASHBOARD_FILE, LEARNED_FILE, PHYSICS_REV, TZ, WARMUP_H,
    _timedelta_h, ac_changes, ac_room_at, append_history_shard,
    append_shard_weather, build_timeline, collect_actual, collect_heating_on,
    fetch_weather, heating_now, house_location, load_house, load_learned,
    load_openings_log, load_window_data, make_context, merged_params,
    model_version, om_learned_from, openings_at, pause_changes, paused_at,
    paused_intervals, physics_rev_migration_needed, refine_outside_now,
)
from vent_physics import (
    CP_AIR, DP_LAM, LEAK_AREA, RunContext, WIND_REF_Z, _eff_open_area,
    _gamma_temps, _sensor_temp, _series_trend, _stair_gamma, _stratify_zones,
    _to_sensor_series, _zone_thermal_params, buoyant_door_exchange,
    build_openings, effective_fresh, per_window_solar, simulate, solve_network,
    stair_crown, sun_position,
)

# Kamer-comfortbanden: read-only meegelift uit de raam-adviseur (zelfde tado-kamernamen),
# puur voor de comfortlijn op de kamerkaart.
from window_advisor import ROOM_COMFORT


def window_weather_summary(weather: dict, now: datetime, window_h: float) -> dict:
    """Compacte weer-context van het residu-venster: dag-max buitentemp + zon-piek/-gemiddelde.
    `solar_mean` maakt het solar_gain-anker regime-bewust in de kalibratie (zie reg_weight);
    de rest is log-context. Lege dict bij geen historie."""
    since = now - _timedelta_h(window_h)
    rows = [r for r in weather.get("hourly", [])
            if r.get("dt") is not None and since <= r["dt"] <= now]
    temps = [r["T_out"] for r in rows if r.get("T_out") is not None]
    solar = [r["shortwave"] for r in rows if r.get("shortwave") is not None]
    out: dict = {}
    if temps:
        out["tmax"] = round(max(temps), 1)
    if solar:
        out["solar_peak"] = round(max(solar))
        out["solar_mean"] = round(sum(solar) / len(solar))
    return out


def anomaly_nudge_text(rmse_cur: float, baseline: float | None) -> str:
    """Het nudge-bericht: fout vs norm + de vraag die de datakwaliteit herstelt. HTML-parse
    (send_telegram-default); dashboardlink alleen als DASHBOARD_URL gezet is."""
    norm = f" (norm ~{baseline:.2f}°C)" if baseline is not None else ""
    lines = [
        "🧭 <b>Tweeling: leren gepauzeerd</b> — voorspelfout anomaal hoog: "
        f"<b>{rmse_cur:.2f}°C</b>{norm}.",
        "Waarschijnlijk staat er een raam/rooster/deur anders dan gemeld. "
        "Klopt de raamstand nog? Meld de werkelijke standen (desnoods teruggedateerd) "
        "in de dashboard-modal.",
    ]
    dash = os.getenv("DASHBOARD_URL")
    if dash:
        lines += [f'<a href="{dash.rstrip("/")}/vent.html">→ Open het tweeling-dashboard</a>']
    return "\n".join(lines)


def anomaly_resume_text(rmse_cur: float) -> str:
    """De ontsnappingsklep-variant: na ANOMALY_MAX_HOLD_H hervat het leren zichzelf en
    wordt de afwijking de nieuwe norm — dat hoort de mens wél te horen, één keer."""
    return ("🧭 <b>Tweeling: leren hervat</b> — de anomalie hield langer aan dan "
            f"{vf.ANOMALY_MAX_HOLD_H:.0f} uur (fout nu {rmse_cur:.2f}°C). Het model leert "
            "vanaf nu door en absorbeert de nieuwe situatie als norm. Klopte de raamstand "
            "toch niet? Corrigeer 'm (desnoods teruggedateerd) in de dashboard-modal.")


def _room_dashboard_row(rid, room, house, params, wd, sim, timeline,
                        actual, now, ctx: RunContext, bundle) -> dict:
    """Bouw één kamer-rij voor vent_data.json.

    `bundle` bundelt de run-brede grootheden die elke kamer deelt: de zone-thermische
    params (`zpar`), de op-nu vastgelegde lucht-/massatemps (`ta_all`/`tm_all`), de
    laatste timeline-stap ≤ now (`now_step`), en de ventilatie-/dichtheidsconstanten
    (`veff`/`rho_cp`)."""
    zpar = bundle["zpar"]
    ta_all = bundle["ta_all"]
    tm_all = bundle["tm_all"]
    now_step = bundle["now_step"]
    wd_key = room.get("from_window_data")
    rd = wd.get("rooms", {}).get(wd_key, {}) if wd_key else {}
    pred_series = sim["series"].get(rid, [])
    ta_now = ta_all.get(rid)               # échte (debiased) luchttemp van het model, op nu
    frac = room.get("sensor_outdoor_frac", 0.0)
    t_out_now = now_step["T_out"] if now_step else None
    pred_now = _sensor_temp(ta_now, t_out_now, frac)   # wat de sensor leest → vergelijkbaar met tado
    act_now = rd.get("inside")
    err = (pred_now - act_now) if (pred_now is not None and act_now is not None) else None
    fresh_ach = None
    env_w = vent_w = None        # warmtestroom naar/uit buiten (W, + = winst, − = verlies)
    if now_step is not None:
        ops = build_openings(house, now_step["states"], now_step["weather"], params,
                             ta_all, now_step["T_out"])
        net = solve_network(list(house["rooms"]) + list(house.get("junctions", {})),
                            ops, ta_all, now_step["T_out"])
        vol = room.get("volume_m3", 40.0)
        fresh_m3s = effective_fresh(net["fresh"], house, now_step["states"],
                                    now_step["weather"], ta_all,
                                    now_step["T_out"]).get(rid, 0.0)
        fresh_ach = round(fresh_m3s * 3600.0 / vol, 2)
        # De twee "naar buiten"-termen, de tegenhanger van de zonwinst: schil-conductie
        # en ventilatie-uitwisseling met de buitenlucht. Negatief = energie verlaat de
        # kamer (koeling/warmteverlies). Zelfde tekens als in simulate()'s luchtknoop.
        if ta_now is not None and t_out_now is not None:
            ua_env = zpar.get(rid, {}).get("UA_env", 0.0)
            env_w = round(ua_env * (t_out_now - ta_now), 0)
            vent_w = round(bundle["rho_cp"] * bundle["veff"] * fresh_m3s * (t_out_now - ta_now), 0)
    sens_series = _to_sensor_series(house, timeline, rid, pred_series)
    trend = _series_trend(sens_series, since=now)
    # Verticale-koker-stratificatie: additieve top/onder-temp + gradiënt voor de weergave
    # (de koker blijft één knoop; `predicted_air_temp` is het koker-gemiddelde). Alleen bij "stratify".
    strat_extra = {}
    strat_info = bundle.get("strat", {}).get(rid)
    if strat_info and ta_now is not None and t_out_now is not None and now_step is not None:
        strat_ops = build_openings(house, now_step["states"], now_step["weather"], params,
                                   ta_all, t_out_now)
        open_pairs = {(op["a"], op["b"]) for op in strat_ops if op.get("b") != "outside"}
        open_others = {o for o in strat_info["doors"]
                       if (rid, o) in open_pairs or (o, rid) in open_pairs}
        # Zelfde regressie-basis als de fysica: gemeten waar beschikbaar (zie _gamma_temps),
        # anders de gesimuleerde luchtknoop. Anders zou het dashboard een andere γ tonen dan
        # de sim gebruikte.
        gamma_temps = _gamma_temps(bundle.get("gamma_measured"), ta_all, now_step["t"])
        gamma = _stair_gamma(strat_info, gamma_temps, open_others)
        crown = stair_crown(now_step.get("irr_roof", {}).get(rid, 0.0))
        # Pin-fout per open deur: koker-lucht op deurhoogte minus de kamer-lucht (− = koker leest
        # kouder dan de kamer op die verdieping). Diagnostiek voor hoe strak de counterflow de
        # koker aan zijn open-deur-kamers pint; hoort ~0 te zijn bij open deuren. Additief veld.
        pin_err = {o: round(ta_now + gamma * (strat_info["doors"][o] - strat_info["z_mean"])
                            - ta_all[o], 2)
                   for o in sorted(open_others) if ta_all.get(o) is not None}
        # Vermogen dat de γ-hoogte-offset per open deur ín die kamer duwt (W, + = de kamer
        # krijgt warmte uit de koker) — gerekend over de Brown–Solvason-counterflow, zodat de
        # grootte van die term controleerbaar blijft i.p.v. impliciet.
        door_area_now = {}
        for op in strat_ops:
            if op.get("b") != "outside":
                pair = (op["a"], op["b"])
                door_area_now[pair] = door_area_now.get(pair, 0.0) + op["area"]
        gamma_w = {}
        for o in sorted(open_others):
            if ta_all.get(o) is None:
                continue
            zh = strat_info["doors"][o]
            area = door_area_now.get((rid, o), 0.0) + door_area_now.get((o, rid), 0.0)
            q_ex = buoyant_door_exchange(area, ta_now + gamma * (zh - strat_info["z_mean"]),
                                         ta_all[o])
            gamma_w[o] = round(1.2 * CP_AIR * q_ex * gamma * (zh - strat_info["z_mean"]), 0)
        strat_extra = {
            "stair_gradient_c_per_m": round(gamma, 3),
            "stair_pin_error_c": pin_err,
            "stair_gamma_w": gamma_w,
            # Zon-kroon: extra °C bovenop de γ-lijn voor de bovenste meters (boven de hoogste
            # deur), gedreven door de dak-/skylight-instraling nu; 's avonds 0.
            "stair_crown_c": round(crown, 2),
            "predicted_temp_top": round(ta_now + gamma * (strat_info["z_hi"] - strat_info["z_mean"])
                                        + crown, 2),
            "predicted_temp_bottom": round(ta_now + gamma * (strat_info["z_lo"] - strat_info["z_mean"]), 2),
        }
    return {
        "label": room.get("label", rid),
        "actual_temp": act_now,
        "predicted_temp": round(pred_now, 2) if pred_now is not None else None,
        # Debiased wáre luchttemp (vóór de buitenmuur-sensorbias); == predicted_temp als frac=0.
        "predicted_air_temp": round(ta_now, 2) if ta_now is not None else None,
        "sensor_outdoor_frac": frac,
        "predicted_mass_temp": round(tm_all.get(rid), 2) if tm_all.get(rid) is not None else None,
        "error": round(err, 2) if err is not None else None,
        "humidity": rd.get("humidity"),
        "ach": fresh_ach,
        "solar_w": round(now_step["irr"].get(rid, 0.0), 0) if now_step else None,
        # De verdeling van de zonwinst over de ramen van deze kamer (W, snapshot op nu) —
        # voedt de raam-tooltip en maakt zichtbaar wélk raam de winst binnenlaat.
        "solar_by_window": {wid: {"label": w.get("label", wid),
                                  "w": round(bundle.get("pw_now", {}).get(wid, 0.0), 0)}
                            for wid, w in house.get("windows", {}).items()
                            if w.get("room") == rid},
        # Energie naar buiten (W, signed): schil-conductie + ventilatie-uitwisseling.
        # − = de kamer verliest warmte (koelt af) langs deze weg; tegenhanger van solar_w.
        "env_w": env_w,
        "vent_w": vent_w,
        # Richting van de temperatuurverandering (°C/uur): + opwarmend, − afkoelend.
        "trend_c_per_h": round(trend, 2) if trend is not None else None,
        "comfort_low": ROOM_COMFORT.get(wd_key, (None, None))[0] if wd_key else None,
        "comfort_high": ROOM_COMFORT.get(wd_key, (None, None))[1] if wd_key else None,
        # Chips: airco in deze kamer / tado-verwarming aan / huis-breed gepauzeerd — in alle
        # drie de gevallen is (een deel van) dit venster niet op deze kamer gekalibreerd.
        "ac": (rid == bundle.get("ac_room")),
        "heating": bool(bundle.get("heat_now", {}).get(rid)),
        "paused": bool(bundle.get("paused_now")),
        # Toon alleen het residu-venster (de WARMUP_H aanloop is sim-only opwarming van de
        # massaknoop, niet bedoeld als zichtbare voorspelling) + de 2u-vooruitblik.
        "predicted_series": [{"t": t.isoformat(), "temp": round(v, 2)}
                             for t, v in sens_series
                             if t >= now - _timedelta_h(CALIB_WINDOW_H)],
        "actual_series": [{"t": t.isoformat(), "temp": v} for t, v in actual.get(rid, [])],
        "params": params.get(rid, {}),
        **strat_extra,
    }


def build_dashboard(house, params, weather, wd, timeline, sim, learned,
                    actual, now, rmse_now, ctx: RunContext, log,
                    learning_held=False, skill=None, rmse_baseline=None,
                    ac_room=None, heat_now=None,
                    paused_now=False, paused_since=None,
                    gamma_measured=None, wx=None) -> dict:
    """Stel docs/vent_data.json samen — het slanke schema uit het herbouwplan (§4):
    precies de velden die de vijf dashboard-features lezen, additief vanaf dag één."""
    cur = weather["current"]
    sun_az, sun_el = sun_position(ctx.lat, ctx.lon, now.astimezone(timezone.utc))

    # Huidige (voorwaarts geaccumuleerde) toestand per element — voedt de modal zodat
    # die toont wat écht open/dicht staat i.p.v. de defaults.
    states_now = openings_at(log, now)

    # De snapshot (kamerkaarten + plattegrond) toont "nu", niet de 2u-vooruitblik: kies de
    # laatste timeline-stap ≤ now en pak de bijbehorende, op now vastgelegde zone-temps.
    last_step = timeline[-1] if timeline else None
    now_step = None
    for s in (timeline or []):
        if s["t"] <= now:
            now_step = s
        else:
            break
    if now_step is None:
        now_step = last_step
    ta_all = sim.get("Ta_now", sim["Ta"])
    tm_all = sim.get("Tm_now", sim["Tm"])
    # Per-raam zon-doorval op nu (W, voor de raam-tooltip). Momentopname op de stap-zonpositie —
    # telt dus niet exact op tot het tijdsgemiddelde `solar_w`; het is de verdeling, niet de boekhouding.
    pw_now = {}
    if now_step is not None:
        wx_now = now_step.get("weather", {})
        pw_now = per_window_solar(house, now_step["states"], now_step["sun_az"],
                                  now_step["sun_el"], wx_now.get("direct") or 0.0,
                                  wx_now.get("diffuse") or 0.0, beam_iam=True)
    bundle = {
        "zpar": _zone_thermal_params(house, params),
        "ta_all": ta_all, "tm_all": tm_all, "now_step": now_step,
        "veff": params.get("vent_eff", 1.0), "rho_cp": 1.2 * CP_AIR,
        "ac_room": ac_room, "heat_now": heat_now or {},
        "paused_now": paused_now,
        "strat": _stratify_zones(house),   # verticale-koker-metadata voor de top/onder-weergave
        "gamma_measured": gamma_measured,  # zelfde γ-basis als de sim (zie _gamma_temps)
        "pw_now": pw_now,
    }
    rooms_out = {rid: _room_dashboard_row(rid, room, house, params, wd, sim, timeline,
                                          actual, now, ctx, bundle)
                 for rid, room in house.get("rooms", {}).items()}

    # Leercurve: alleen aangroeien + uitdunnen. Geen backfill/vingerafdrukken meer — een
    # log-correctie verbetert vanzelf de vólgende punten; de oude curve-herschrijfmachinerie
    # was zelf een incidentbron (zie het herbouwplan).
    rmse_hist = (learned.get("rmse_history") or [])[:]
    if rmse_now == rmse_now:   # niet-NaN
        entry = {"t": now.isoformat(), "rmse": round(rmse_now, 3),
                 "held": bool(learning_held), "paused": bool(paused_now),
                 "version": model_version()}
        # Additieve context per punt: weer-genormaliseerde skill + persistentie-baseline +
        # weer-samenvatting, zodat de leercurve (en vent_diagnostics) te lezen is als
        # "model vs. weer" i.p.v. een RMSE-stijging blind als regressie te lezen.
        if skill is not None:
            entry["skill"] = skill
        if rmse_baseline is not None and rmse_baseline == rmse_baseline:
            entry["rmse_naive"] = round(rmse_baseline, 3)
        if wx:
            entry["wx"] = wx
        rmse_hist.append(entry)
    rmse_hist = thin_rmse_history(rmse_hist, now)

    return {
        "generated_at": utc_now_iso(),
        "as_of_local": now.isoformat(),
        "source": "vent_twin",
        "model_version": model_version(),
        "weather": {
            "outside_temp": cur.get("temperature_2m"),
            "outside_humidity": cur.get("relative_humidity_2m"),
            # Bron van de buiten-nu temp/RH: "wu" (eigen station, biasgecorrigeerd) of
            # "open-meteo" (grid-fallback). Wind/instraling komen altijd van Open-Meteo.
            "outside_source": cur.get("outside_source", "open-meteo"),
            "wind_speed": cur.get("wind_speed_10m"), "wind_dir": cur.get("wind_direction_10m"),
            "gust": cur.get("wind_gusts_10m"), "shortwave": cur.get("shortwave_radiation"),
            "sun_az": round(sun_az, 1), "sun_el": round(sun_el, 1),
            "neighbor_temp": round(ctx.neighbor_temp, 1),
            "ground_temp": round(ctx.ground_temp, 1),
            # WU/OM glas-drive-herschaling die deze run op de recente stappen is toegepast;
            # null = pure Open-Meteo (WU-zon ontbrak of te laag).
            "wu_solar_scale": (round(weather.get("wu_solar_scale"), 2)
                               if weather.get("wu_solar_scale") is not None else None),
        },
        "openings": states_now,
        "controls": _controls(house, states_now),
        # Pauze: is het huis nu gepauzeerd (huis-breed, via de modal-toggle), en sinds wanneer
        # (ISO, null als niet gepauzeerd) — voedt de "⏸️ Gepauzeerd sinds HH:MM"-banner.
        "paused": bool(paused_now),
        "paused_since": paused_since.isoformat() if paused_since else None,
        # Airco: in welke kamer de ene mobiele unit nu staat (of None) + de keuzelijst voor de
        # modal-dropdown (alleen sensorkamers — alleen díé worden gekalibreerd).
        "ac": {"room": ac_room,
               "rooms": [{"id": rid, "label": r.get("label", rid)}
                         for rid, r in house.get("rooms", {}).items()
                         if r.get("from_window_data")]},
        "rooms": rooms_out,
        "flows": _flow_summary(house, params, sim, timeline, now),
        "learned": {"params": params,
                    "rmse": round(rmse_now, 3) if rmse_now == rmse_now else None,
                    "rmse_naive": round(rmse_baseline, 3)
                    if (rmse_baseline is not None and rmse_baseline == rmse_baseline) else None,
                    "skill": skill,
                    "railed": railed_params(params),
                    "rmse_history": rmse_hist,
                    "held": bool(learning_held),
                    "paused": bool(paused_now)},
        # Volledige geometrie zodat de browser-speeltuin hetzelfde luchtstroomnetwerk
        # lokaal kan oplossen.
        "house_meta": house_meta_out(house),
    }


def house_meta_out(house: dict) -> dict:
    """Volledige geometrie voor de browser-speeltuin: openingsoppervlakken, hoogtes,
    roosters/deuren en de sim-constanten die Python gebruikt."""
    return {
        "rooms": {rid: {"plan_xy": r.get("plan_xy"), "label": r.get("label", rid),
                        "floor": r.get("floor", 0), "plan_h": r.get("plan_h", 1),
                        "volume_m3": r.get("volume_m3"),
                        "sensor": bool(r.get("from_window_data"))}
                  for rid, r in house.get("rooms", {}).items()},
        "junctions": {jid: {"plan_xy": j.get("plan_xy"), "label": j.get("label", jid),
                            "floor": j.get("floor", 0), "volume_m3": j.get("volume_m3"),
                            "sensor": False}
                      for jid, j in house.get("junctions", {}).items()},
        "windows": {wid: {"room": w.get("room"), "facade_azimuth_deg": w.get("facade_azimuth_deg"),
                          "kind": "skylight" if w.get("tilt_deg", 90) < 45 else "window",
                          "area_m2": w.get("area_m2"), "glass_m2": w.get("glass_m2"),
                          "max_open_area_m2": w.get("max_open_area_m2", 0.0),
                          "center_height_m": w.get("center_height_m"),
                          "tilt_frac": w.get("tilt_frac"), "tilt_deg": w.get("tilt_deg"),
                          # Opgelost effectief-oppervlak (open_type/override) voor de
                          # JS-speeltuin, zodat die dezelfde korting rekent.
                          "eff_open_frac": _eff_open_area(w),
                          "plan_side": w.get("plan_side"), "plan_pos": w.get("plan_pos"),
                          "label": w.get("label", wid)}
                    for wid, w in house.get("windows", {}).items()},
        "vents": {vid: {"room": v.get("room"), "facade_azimuth_deg": v.get("facade_azimuth_deg"),
                        "area_m2": v.get("area_m2"), "max_open_area_m2": v.get("max_open_area_m2"),
                        "eff_open_frac": _eff_open_area(v),
                        "center_height_m": v.get("center_height_m"),
                        "default_state": v.get("default_state", "open"),
                        "plan_side": v.get("plan_side"), "plan_pos": v.get("plan_pos"),
                        "label": v.get("label", vid)}
                  for vid, v in house.get("vents", {}).items()},
        "doors": {did: {"between": d.get("between"), "label": d.get("label", did),
                        "area_m2": d.get("area_m2"), "center_height_m": d.get("center_height_m"),
                        "default_state": d.get("default_state", "open"),
                        "plan_pos": d.get("plan_pos"),
                        "fixed": bool(d.get("fixed"))}
                  for did, d in house.get("doors", {}).items()},
        "sim": {"leak_area": LEAK_AREA, "dp_lam": DP_LAM, "wind_ref_z": WIND_REF_Z},
    }


def _controls(house: dict, states_now: dict) -> list[dict]:
    """Lijst bedienbare elementen (ramen, roosters, zonwering, deuren) met hun huidige
    gerapporteerde stand — de bron voor de 'Stel ramen/roosters in'-modal."""
    out = []
    for wid, w in house.get("windows", {}).items():
        if w.get("max_open_area_m2", 0.0) <= 0.0:
            continue   # vast glas — niet bedienbaar, hoort niet in de modal
        out.append({"id": wid, "kind": "window", "label": w.get("label", wid),
                    "room": w.get("room"), "state": states_now.get(wid, "dicht")})
    for vid, v in house.get("vents", {}).items():
        out.append({"id": vid, "kind": "vent", "label": v.get("label", vid),
                    "room": v.get("room"), "state": states_now.get(vid, "open")})
    # Bedienbare zonwering (lamella, buitenscherm, verduisteringsgordijn): wanneer dicht
    # gemeld, dempt het de zoninstraling door dat raam.
    for wid, w in house.get("windows", {}).items():
        sh = w.get("shade")
        if not sh:
            continue
        out.append({"id": wid + "_shade", "kind": "shade",
                    "label": f"{sh.get('label', 'zonwering')} — {w.get('label', wid)}",
                    "room": w.get("room"), "state": states_now.get(wid + "_shade", "open")})
    for did, d in house.get("doors", {}).items():
        if d.get("fixed"):
            continue   # permanente doorgang (geen deur) → niet bedienbaar, niet tonen
        out.append({"id": did, "kind": "door", "label": d.get("label", did),
                    "between": d.get("between"),
                    "state": states_now.get(did, d.get("default_state", "open"))})
    return out


def _flow_summary(house, params, sim, timeline, now) -> list[dict]:
    if not timeline:
        return []
    # Plattegrond toont "nu": de laatste stap ≤ now + de op now vastgelegde zone-temps.
    step = timeline[-1]
    for s in timeline:
        if s["t"] <= now:
            step = s
        else:
            break
    ta = sim.get("Ta_now", sim["Ta"])
    zones = list(house["rooms"]) + list(house.get("junctions", {}))
    ops = build_openings(house, step["states"], step["weather"], params, ta, step["T_out"])
    net = solve_network(zones, ops, ta, step["T_out"])
    out = []
    for op, q in zip(ops, net["flows"]):
        if op["id"].startswith("_leak_"):
            continue
        out.append({"id": op["id"], "a": op["a"], "b": op["b"],
                    "flow_m3s": round(q, 4)})
    return out


def main():
    now = datetime.now(TZ)
    print(f"[vent_twin] Start — {now.isoformat()}")

    house = load_house()
    wd = load_window_data()
    weather = fetch_weather(*house_location(house))
    wu_solar_scale, _out_rh_temp = refine_outside_now(weather)
    log = load_openings_log()

    # Run-context: locatie + de twee trage ankers (buur/bodem) — expliciet, geen globals.
    ctx = make_context(house, weather, now)
    print(f"[buren] party-muur-anker = {ctx.neighbor_temp:.1f} °C · "
          f"bodem-anker = {ctx.ground_temp:.1f} °C")

    learned = load_learned()
    # Fysica-revisie-migratie (zie PHYSICS_REV): merged_params reset alle params naar de
    # priors; de anomalie-poort krijgt een cooloff (anomaly_step) i.p.v. een valse hold.
    physics_migrated = physics_rev_migration_needed(learned)
    if physics_migrated:
        print(f"[fysica] revisie {learned.get('physics_rev', 1)} → {PHYSICS_REV}: params "
              "terug naar hun priors; her-seeden kan met tools/vent_seed.py.")
    params = merged_params(house, learned)

    since = now - _timedelta_h(CALIB_WINDOW_H)
    actual = collect_actual(house, wd, since)
    # Seed-bron vóór de filters: de eerste (oudste) sample per kamer blijft de seed, óók als
    # een filter latere samples wegneemt — zo blijft de voorspelling van die kamer geankerd.
    seed_src = {rid: s[0][1] for rid, s in actual.items() if s}
    # Massaknoop-seed = venster-gemiddelde van de gemeten luchttemp (de trage massaknoop ís
    # fysisch een gedempt gemiddelde van de kamerlucht). Vóór de filters berekend.
    tm_seed_src = {rid: sum(t for _, t in s) / len(s) for rid, s in actual.items() if s}
    # Regressie-basis voor de koker-gradiënt γ: bewust de ÓNGEFILTERDE metingen — de filters
    # houden vervuilde samples uit de FIT, maar γ is een waarneming, geen fit-doel.
    gamma_measured = {rid: list(s) for rid, s in actual.items() if s}

    # Airco-kamer uit de kalibratie (geen actieve-koel-term in de fysica).
    acc = ac_changes(log)
    ac_room_now = ac_room_at(acc, now)
    actual, ac_excluded = filter_ac_samples(actual, acc, ac_room_now, now)
    if ac_excluded:
        print(f"[airco] kamer nu = {ac_room_now or '—'}; samples uit de fit gelaten: {ac_excluded}")

    # Gestookte samples uit de kalibratie (geen verwarmingsterm; tado-vlag is exact getimed).
    heat_on = collect_heating_on(house, wd, since)
    heat_now = heating_now(house, wd)
    actual, heat_excluded = filter_heating_samples(actual, heat_on)
    if heat_excluded:
        rooms_heating = [rid for rid, on in heat_now.items() if on]
        print(f"[verwarming] kamers nu aan = {rooms_heating or '—'}; "
              f"samples uit de fit gelaten: {heat_excluded}")

    # Huis-brede pauze: "niemand meldt de standen nu betrouwbaar".
    pchanges = pause_changes(log)
    p_intervals = paused_intervals(pchanges, now)
    paused_now = paused_at(pchanges, now)
    paused_since = p_intervals[-1][0] if paused_now and p_intervals else None
    actual, pause_excluded = filter_paused_samples(actual, p_intervals)
    if pause_excluded:
        print(f"[pauze] gepauzeerd nu = {paused_now}; samples uit de fit gelaten: {pause_excluded}")

    # Timeline reikt WARMUP_H vóór het residu-venster (sim-only massaknoop-aanloop).
    om_learned = om_learned_from(wd)
    if om_learned:
        print(f"[om-bias] driver gecorrigeerd: nacht {om_learned.get('night'):+.2f}°C, "
              f"dag {om_learned.get('day'):+.2f}°C")
    timeline = build_timeline(house, weather, log, now, CALIB_WINDOW_H + WARMUP_H, ctx,
                              wu_solar_scale=wu_solar_scale, beam_iam=True,
                              om_learned=om_learned)
    if not timeline:
        print("[vent_twin] Geen weerdata → kan niet simuleren. Stop.")
        sys.exit(1)

    # Seed de luchtknoop op de eerste werkelijke meting per kamer (anders buiten).
    seed = dict(seed_src)
    for rid in house.get("rooms", {}):
        seed.setdefault(rid, timeline[0]["T_out"])

    wx_summary = window_weather_summary(weather, now, CALIB_WINDOW_H)

    # Leren (alleen als er werkelijke samples zijn om tegen te ijken).
    rmse_now = float("nan")
    learning_held = False
    baseline = None
    anomaly_state = dict(learned.get("anomaly") or {})
    if actual:
        print(f"[leren] {sum(len(v) for v in actual.values())} samples over "
              f"{len(actual)} kamers in het venster.")
        # Anomalie-poort: hoe goed voorspellen de húidige parameters? Veel slechter dan
        # normaal → de openingen-log klopt vermoedelijk niet met de werkelijkheid.
        rmse_cur = rmse(_residuals(house, params, timeline, seed, ctx, actual,
                                   set(actual.keys()),
                                   tm_seed=tm_seed_src, measured=gamma_measured))
        anomaly_held, baseline, anomaly_state = anomaly_step(
            rmse_cur, learned.get("rmse_history", []), anomaly_state, now,
            physics_migrated=physics_migrated)
        if anomaly_state.get("escaped_at") == now.isoformat():
            # Ontsnappingsklep gevuurd: hold duurde langer dan ANOMALY_MAX_HOLD_H → leren
            # hervat en de poort is tijdelijk ontwapend. Eén keer melden.
            msg = anomaly_resume_text(rmse_cur)
            if os.getenv("DRY_RUN") == "1":
                print(f"[nudge] DRY_RUN — zou sturen:\n{msg}")
            else:
                send_telegram(msg)
        learning_held = anomaly_held or paused_now
        if learning_held:
            rmse_now = rmse_cur
            if paused_now:
                print(f"[pauze] gepauzeerd → parameters vastgehouden (RMSE {rmse_cur:.2f}°C); "
                      "dit venster leert niet mee.")
            if anomaly_held:
                print(f"[leren] fout anomaal hoog ({rmse_cur:.2f}°C vs norm {baseline:.2f}°C) → "
                      "parameters vastgehouden; de openingen-log klopt mogelijk niet. "
                      "Voorspellen gaat door, leren is gepauzeerd.")
                # Nudge naar de privé-chat: alleen een mens kan de log corrigeren — en die
                # moet het dan wel hóren. Cooldown + episode-reset via anomaly_state.
                if should_nudge_anomaly(anomaly_state.get("nudged_at"), now):
                    msg = anomaly_nudge_text(rmse_cur, baseline)
                    if os.getenv("DRY_RUN") == "1":
                        print(f"[nudge] DRY_RUN — zou sturen:\n{msg}")
                    else:
                        send_telegram(msg)
                    anomaly_state["nudged_at"] = now.isoformat()
        else:
            params, rmse_now = calibrate(house, params, timeline, seed, ctx, actual,
                                         solar_mean=wx_summary.get("solar_mean"),
                                         tm_seed=tm_seed_src, measured=gamma_measured)
            print(f"[leren] RMSE na kalibratie: {rmse_now:.3f} °C")
            rails = railed_params(params)
            if rails:
                print(f"[saturatie] params op hun grens: {', '.join(rails)}")
    else:
        print("[leren] geen werkelijke kamertemps in het venster → alleen voorspellen.")
    # Defensief: was het héle venster gepauzeerd, dan is `actual` leeg en werd de if-tak
    # overgeslagen — een gepauzeerd huis mag nooit als "geleerd" geboekt worden.
    learning_held = learning_held or paused_now

    # Weer-genormaliseerde skill t.o.v. een persistentie-baseline (alleen een readout —
    # er poort níéts meer op skill, dat was de fallback-lus-les).
    rmse_baseline = naive_rmse(actual) if actual else float("nan")
    skill = skill_score(rmse_now, rmse_baseline)

    # Voorspelling met de (geleerde) params over het volledige venster + vooruitblik.
    sim = simulate(house, params, timeline, seed, ctx,
                   calib_only_rooms=set(house.get("rooms", {}).keys()),
                   snapshot_t=now, tm_seed=tm_seed_src, measured=gamma_measured)
    if sim.get("solver_failures"):
        print(f"[sim] {sim['solver_failures']} substap(pen) met een bijna-singulier thermisch "
              "stelsel — Ta/Tm die stap bevroren.")

    dash = build_dashboard(house, params, weather, wd, timeline, sim, learned,
                           actual, now, rmse_now, ctx, log,
                           learning_held=learning_held, skill=skill,
                           rmse_baseline=rmse_baseline,
                           ac_room=ac_room_now, heat_now=heat_now,
                           paused_now=paused_now, paused_since=paused_since,
                           gamma_measured=gamma_measured, wx=wx_summary)
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(dash, f, ensure_ascii=False, indent=2)
    learned_out = {"updated_at": now.isoformat(), "model_version": model_version(),
                   "physics_rev": PHYSICS_REV,
                   "params": params,
                   "rmse": round(rmse_now, 3) if rmse_now == rmse_now else None,
                   "skill": skill,
                   "railed": railed_params(params),
                   "rmse_history": dash["learned"]["rmse_history"],
                   # Anomalie-poort-state (held_since/cooloff_until/nudged_at/escaped_at).
                   "anomaly": anomaly_state}
    # seed_src-stempel van tools/vent_seed.py meenemen zolang die relevant is.
    if learned.get("seed_src") and not learned.get("rmse_history"):
        learned_out["seed_src"] = learned["seed_src"]
    with open(LEARNED_FILE, "w", encoding="utf-8") as f:
        json.dump(learned_out, f, ensure_ascii=False, indent=2)

    # Shards: verse tado-samples + log-snapshots + verstreken weer-uren appenden zodat de
    # offline evaluatie-/seed-set blijft aangroeien (merge-op-tijdstip, idempotent).
    try:
        n_s = append_history_shard(wd, log, now)
        n_w = append_shard_weather(weather, now)
        if n_s or n_w:
            print(f"[shards] +{n_s} kamersamples, +{n_w} weer-uren.")
    except Exception as e:  # noqa: BLE001 — shard-append mag de run nooit laten falen
        print(f"[shards] append overgeslagen: {e}")

    print(f"[vent_twin] Geschreven: {DASHBOARD_FILE} + {LEARNED_FILE}. Klaar.")


if __name__ == "__main__":
    run_guarded(main, "vent-twin", fail_threshold=6)

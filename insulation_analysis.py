#!/usr/bin/env python3
"""Isolatie-analyse (Project 12) — per-kamer isolatiekwaliteit uit een jaar tado-historie.

Schat per kamer een warmteverlies-coëfficiënt (UA, W/K) door alleen te kijken naar
uren waarin `callForHeat == "NONE"` — de kamer koelt/warmt dan vrij uit, zónder dat
we het onbekende radiatorvermogen hoeven te kennen. Een lineaire "vrije-uitloop"-
regressie (Newton's afkoelingswet, plus een zonwinst-term):

    dT_in/dt ≈ k · (T_out − T_in) + s · I_zon,kamer + c

gefit over een heel jaar (tado-export × Open-Meteo ERA5-archief) i.p.v. Project 8's
rollende 48u-venster — dat venster kan sommige kanalen (zie `office.ua_roof` in
`docs/airflow_learned.json`, tegen zijn grens gerailed) niet betrouwbaar identificeren;
een jaar seizoensvariatie in ΔT en zon wél.

Hergebruikt Project 8's *pure* helpers read-only (`load_house`, `sun_position`,
`facade_irradiance`, `per_window_solar`, `room_base_capacitances`, `solve_linear`,
`load_learned`, `merged_params`) — geen netwerk-/Gauss-Newton-kalibratie, dat is voor
een jaar aan data expliciet te duur gebleken (zie CLAUDE.md Project 12).

GEEN GitHub Action — dit is een handmatig, incidenteel script. De ruwe tado-export
bevat (impliciet, via de temperatuur-/stookpatronen) thuis/weg-informatie; dit is een
PUBLIEKE repo, dus de ruwe export wordt NOOIT gelezen van, of geschreven naar, een pad
in de git working tree. Alleen geaggregeerde/afgeleide resultaten (`docs/insulation_data.json`)
worden weggeschreven — geen per-uur of per-dag tijdreeks die het thuis/weg-patroon zou
verraden.

Gebruik:
    python insulation_analysis.py --input living=/pad/naar/tado_cache_living.json \
                                   --input ted=/pad/naar/tado_cache_ted.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from airflow_model import (
    ROOF_U,
    load_house,
    load_learned,
    merged_params,
    per_window_solar,
    room_base_capacitances,
    solve_linear,
    sun_position,
)
from http_util import get_json
from shared_const import LATITUDE, LONGITUDE, utc_now_iso

UTC = timezone.utc
OUT_PATH = os.getenv("INSULATION_DATA_PATH", "docs/insulation_data.json")

# Ruw-uur-op-uur-verschil groter dan dit is vrijwel zeker een sensor-hik of DST-sprong,
# geen echte fysica — verwijderd uit de fit i.p.v. de regressie te laten vervuilen.
MAX_PLAUSIBLE_DT_PER_H = 2.5
# Minder dan dit aantal "verwarming-uit"-uur-paren → te weinig signaal, geen schatting.
MIN_PAIRS = 100

FACADE_STREET = 309.0   # NW — weinig middagzon
FACADE_GARDEN = 129.0   # ZO — de meeste zon
FACADE_SIDE = 219.0     # ZW


# ════════════════════════════════════════════════════════════════════════════════════
#  Parsen van de tado dayReport-export (lokaal bestand, NOOIT gecommit)
# ════════════════════════════════════════════════════════════════════════════════════

def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _call_for_heat_lookup(intervals: list[dict], ts: datetime) -> str | None:
    for iv in intervals:
        if _parse_ts(iv["from"]) <= ts < _parse_ts(iv["to"]):
            return iv.get("value")
    return None


def _connected_lookup(intervals: list[dict], ts: datetime) -> bool:
    for iv in intervals:
        if _parse_ts(iv["from"]) <= ts < _parse_ts(iv["to"]):
            return bool(iv.get("value"))
    return True   # geen dekking gevonden → aanname: verbonden (de doorsnee-dag is 1 interval "true")


def parse_tado_cache(path: str) -> list[dict]:
    """Leest één tado dayReport-cache (365 dagen, key = datum) en levert een
    tijd-gesorteerde lijst {"t", "T_in", "humidity", "call_for_heat", "connected"}.
    Leest bewust NIET de "settings"/"stripes" (stook-setpoint/thuis-weg) — alleen wat
    de vrije-uitloop-fit nodig heeft."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    samples = []
    for date_key in sorted(data.keys()):
        day = data[date_key]
        md = day.get("measuredData", {})
        temp_points = md.get("insideTemperature", {}).get("dataPoints", [])
        hum_points = md.get("humidity", {}).get("dataPoints", [])
        hum_by_ts = {p["timestamp"]: p["value"] for p in hum_points}
        heat_intervals = day.get("callForHeat", {}).get("dataIntervals", [])
        conn_intervals = md.get("measuringDeviceConnected", {}).get("dataIntervals", [])

        for p in temp_points:
            ts = _parse_ts(p["timestamp"])
            samples.append({
                "t": ts,
                "T_in": p["value"]["celsius"],
                "humidity": hum_by_ts.get(p["timestamp"]),
                "call_for_heat": _call_for_heat_lookup(heat_intervals, ts),
                "connected": _connected_lookup(conn_intervals, ts),
            })
    samples.sort(key=lambda s: s["t"])
    return samples


def resample_hourly(samples: list[dict]) -> dict[str, dict]:
    """15-min tado-samples → uurgemiddelden, key = UTC-uur 'YYYY-MM-DDTHH' (matcht
    Open-Meteo's uur-sleutel). `heating_off` = elk 15-min-sample in dat uur had
    callForHeat == "NONE" én het apparaat was verbonden — conservatief: één
    onbekende/andere waarde in het uur maakt het hele uur "niet vrij uitlopend"."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        buckets[s["t"].strftime("%Y-%m-%dT%H")].append(s)

    out = {}
    for key, group in buckets.items():
        temps = [g["T_in"] for g in group if g["T_in"] is not None]
        if not temps:
            continue
        calls = [g["call_for_heat"] for g in group]
        connected = all(g["connected"] for g in group)
        known_calls = [c for c in calls if c is not None]
        heating_off = bool(known_calls) and connected and all(c == "NONE" for c in known_calls)
        hums = [g["humidity"] for g in group if g["humidity"] is not None]
        out[key] = {
            "T_in": sum(temps) / len(temps),
            "humidity": (sum(hums) / len(hums)) if hums else None,
            "heating_off": heating_off,
            "n_samples": len(group),
        }
    return out


# ════════════════════════════════════════════════════════════════════════════════════
#  Weer (Open-Meteo ERA5-archief — één call, willekeurige periode, zie station_accuracy.py)
# ════════════════════════════════════════════════════════════════════════════════════

def _at(h: dict, key: str, i: int):
    arr = h.get(key) or []
    return arr[i] if i < len(arr) else None


def fetch_om_archive_hourly(start: str, end: str) -> dict[str, dict]:
    """Open-Meteo ERA5-archief, uurlijks UTC, temp + directe/diffuse instraling +
    bewolking + wind. Zelfde endpoint als station_accuracy.py (Project 7), hier
    uitgebreid met direct/diffuse voor de per-raam-zoninstraling."""
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "start_date": start,
        "end_date": end,
        "hourly": ("temperature_2m,direct_radiation,diffuse_radiation,"
                   "shortwave_radiation,cloud_cover,wind_speed_10m"),
        "timezone": "UTC",
    }
    j = get_json("https://archive-api.open-meteo.com/v1/archive", params,
                 timeout=60, label="om-archive-insulation")
    h = j.get("hourly", {})
    times = h.get("time", [])
    out: dict[str, dict] = {}
    for i, t in enumerate(times):
        temp = _at(h, "temperature_2m", i)
        if temp is None:
            continue                      # ERA5T-gat (laatste ~5 dgn)
        out[t[:13]] = {
            "T_out": temp,
            "direct": _at(h, "direct_radiation", i) or 0.0,
            "diffuse": _at(h, "diffuse_radiation", i) or 0.0,
            "cloud": _at(h, "cloud_cover", i),
            "wind": _at(h, "wind_speed_10m", i),
        }
    print(f"[OM] {len(out)} uurwaarnemingen ({start} … {end})")
    return out


# ════════════════════════════════════════════════════════════════════════════════════
#  Per-kamer zoninstraling (hergebruikt Project 8's pure fysica, read-only)
# ════════════════════════════════════════════════════════════════════════════════════

def solar_series_per_room(house: dict, weather: dict) -> dict[str, dict[str, float]]:
    """{room_id: {uur_key: getransmitteerde zonne-W in die kamer}}. Zonder een
    jaarlang-bewaarde openingen-log wordt elk raam op zijn *standaard* zonwerings-
    stand behandeld (states={}) — een benadering; de structurele UA-verschillen
    tussen kamers zijn veel groter dan wat een gemiddelde zonwerings-aanname scheelt."""
    windows_by_room: dict[str, list[str]] = defaultdict(list)
    for wid, w in house.get("windows", {}).items():
        windows_by_room[w.get("room")].append(wid)

    out: dict[str, dict[str, float]] = {rid: {} for rid in house.get("rooms", {})}
    for key, wx in weather.items():
        dt = datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=UTC)
        sun_az, sun_el = sun_position(LATITUDE, LONGITUDE, dt)
        per_win = per_window_solar(house, {}, sun_az, sun_el, wx["direct"], wx["diffuse"])
        for rid, wids in windows_by_room.items():
            if rid in out:
                out[rid][key] = sum(per_win.get(wid, 0.0) for wid in wids)
    return out


# ════════════════════════════════════════════════════════════════════════════════════
#  Vrije-uitloop-regressie: dT_in/dt = k·(T_out−T_in) + s·I_zon + c
# ════════════════════════════════════════════════════════════════════════════════════

def fit_room_ua(house: dict, room_id: str, tado_hourly: dict[str, dict],
                weather_hourly: dict[str, dict], solar_hourly: dict[str, float]) -> dict:
    room = house["rooms"][room_id]
    keys = sorted(set(tado_hourly) & set(weather_hourly))

    pairs = []
    for i in range(len(keys) - 1):
        k0, k1 = keys[i], keys[i + 1]
        t0 = datetime.strptime(k0, "%Y-%m-%dT%H").replace(tzinfo=UTC)
        t1 = datetime.strptime(k1, "%Y-%m-%dT%H").replace(tzinfo=UTC)
        if t1 - t0 != timedelta(hours=1):
            continue
        r0, r1 = tado_hourly[k0], tado_hourly[k1]
        if not (r0["heating_off"] and r1["heating_off"]):
            continue
        w0, w1 = weather_hourly[k0], weather_hourly[k1]
        dT = r1["T_in"] - r0["T_in"]              # °C over exact 1h → °C/h
        if abs(dT) > MAX_PLAUSIBLE_DT_PER_H:
            continue
        tin_avg = (r0["T_in"] + r1["T_in"]) / 2.0
        tout_avg = (w0["T_out"] + w1["T_out"]) / 2.0
        i_avg = (solar_hourly.get(k0, 0.0) + solar_hourly.get(k1, 0.0)) / 2.0
        pairs.append((tout_avg - tin_avg, i_avg, dT))

    n = len(pairs)
    if n < MIN_PAIRS:
        return {"status": "insufficient_data", "n_pairs": n, "n_hours_total": len(keys)}

    # Probeer eerst k + zonwinst + constante (3 onbekenden); een raam-loze kamer of een
    # venster zonder zonvariantie in de meegegeven reeks maakt de zon-kolom ontaard
    # (singulier) — val dan terug op k + constante (2 onbekenden) i.p.v. helemaal te
    # falen: de robuuste primaire grootheid (k, dus UA) heeft de zonterm niet nodig.
    sol = _solve_ols(pairs, use_solar=True)
    solar_dropped = False
    if sol is None:
        sol = _solve_ols(pairs, use_solar=False)
        solar_dropped = True
    if sol is None:
        return {"status": "singular_fit", "n_pairs": n, "n_hours_total": len(keys)}

    if solar_dropped:
        k_per_h, const_term = sol
        solar_coef = 0.0
        resid = [y - (k_per_h * x1 + const_term) for x1, _x2, y in pairs]
    else:
        k_per_h, solar_coef, const_term = sol
        resid = [y - (k_per_h * x1 + solar_coef * x2 + const_term) for x1, x2, y in pairs]
    rmse_fit = (sum(r * r for r in resid) / n) ** 0.5

    c_air0, c_mass0, _ = room_base_capacitances(room)
    c_eff = c_air0 + c_mass0
    ua_w_per_k = k_per_h / 3600.0 * c_eff
    wall_m2 = room.get("exterior_wall_m2", 0.0)
    ua_per_m2 = ua_w_per_k / wall_m2 if wall_m2 else None

    return {
        "status": "ok", "n_pairs": n, "n_hours_total": len(keys),
        "k_per_h": round(k_per_h, 5), "solar_coef": round(solar_coef, 4),
        "solar_dropped": solar_dropped,
        "const_term": round(const_term, 4), "rmse_fit_c_per_h": round(rmse_fit, 3),
        "ua_w_per_k": round(ua_w_per_k, 2),
        "ua_per_m2": round(ua_per_m2, 3) if ua_per_m2 is not None else None,
        "c_effective_j_per_k": round(c_eff, 0), "exterior_wall_m2": wall_m2,
    }


def _solve_ols(pairs: list[tuple], use_solar: bool) -> list[float] | None:
    """Normaalvergelijkingen voor dT = k·x1 (+ s·x2) + c, opgelost via solve_linear
    (3×3, of 2×2 zonder de zonterm). None bij een (bijna-)singuliere zon-kolom."""
    ncol = 3 if use_solar else 2
    xtx = [[0.0] * ncol for _ in range(ncol)]
    xty = [0.0] * ncol
    for x1, x2, y in pairs:
        row = (x1, x2, 1.0) if use_solar else (x1, 1.0)
        for a in range(ncol):
            xty[a] += row[a] * y
            for b in range(ncol):
                xtx[a][b] += row[a] * row[b]
    return solve_linear(xtx, xty)


# ════════════════════════════════════════════════════════════════════════════════════
#  Geometrie-narratief + cross-check tegen Project 8's online kalibratie
# ════════════════════════════════════════════════════════════════════════════════════

def _facade_label(az: float) -> str:
    if abs(az - FACADE_STREET) < 20:
        return "straatzijde (NW, weinig middagzon)"
    if abs(az - FACADE_GARDEN) < 20:
        return "tuinzijde (ZO, meeste zon)"
    if abs(az - FACADE_SIDE) < 20:
        return "zijgevel (ZW)"
    return f"{az:.0f}°"


def room_geometry_summary(house: dict, room_id: str) -> dict:
    room = house["rooms"][room_id]
    windows = [w for w in house.get("windows", {}).values() if w.get("room") == room_id]
    window_area = sum(w.get("area_m2", 0.0) for w in windows)
    wall_m2 = room.get("exterior_wall_m2", 0.0)
    facade_area: dict[float, float] = defaultdict(float)
    for w in windows:
        facade_area[w.get("facade_azimuth_deg", 0.0)] += w.get("area_m2", 0.0)
    dominant_az = max(facade_area, key=facade_area.get) if facade_area else None
    return {
        "label": room.get("label", room_id),
        "window_area_m2": round(window_area, 2),
        "exterior_wall_m2": wall_m2,
        "window_wall_ratio": round(window_area / wall_m2, 3) if wall_m2 else None,
        "dominant_facade_deg": dominant_az,
        "dominant_facade_label": _facade_label(dominant_az) if dominant_az is not None else "geen ramen",
        "floor": room.get("floor"),
        "has_roof": bool(room.get("roof_m2")),
        "roof_m2": room.get("roof_m2", 0.0),
    }


def online_ua_estimate(house: dict, params: dict, room_id: str) -> dict:
    """UA (W/K) zoals Project 8's doorlopende 48u-kalibratie 'm nú ziet — voor
    de agreement-check, niet om te herschrijven (read-only)."""
    room = house["rooms"][room_id]
    _, _, ua0 = room_base_capacitances(room)
    p = params.get(room_id, {})
    ua_env = ua0 * 0.5 * p.get("ua_env", 1.0)
    ua_roof = room.get("roof_m2", 0.0) * ROOF_U * p.get("ua_roof", 1.0)
    return {"ua_env_w_per_k": round(ua_env, 2), "ua_roof_w_per_k": round(ua_roof, 2),
            "ua_total_w_per_k": round(ua_env + ua_roof, 2)}


def build_narrative(fit: dict, geom: dict, online_cmp: dict | None,
                    rank: int | None, total_rooms: int) -> str:
    if fit["status"] != "ok":
        return (f"Te weinig 'verwarming-uit'-uren dit jaar ({fit.get('n_pairs', 0)} paren) "
                "voor een betrouwbare schatting.")
    parts = [
        f"UA ≈ {fit['ua_w_per_k']:.1f} W/K"
        + (f" ({fit['ua_per_m2']:.2f} W/K per m² buitengevel)" if fit['ua_per_m2'] is not None else "")
        + (f", rang {rank}/{total_rooms} (1 = best geïsoleerd)." if rank else ".")
    ]
    if geom["window_wall_ratio"] is not None:
        parts.append(f"Raam/gevel-verhouding {geom['window_wall_ratio']:.0%} op de "
                     f"{geom['dominant_facade_label']}.")
    if geom["has_roof"]:
        parts.append("Bovenste verdieping met dakvlak — een deel van het verlies kan via "
                     "het dak lopen, niet alleen de muren.")
    if fit.get("solar_dropped"):
        parts.append("Zonwinst-term kon niet apart worden geschat (te weinig variatie in de "
                     "meegegeven instraling) — UA hier is de robuustere schatting zonder die term.")
    if online_cmp and online_cmp["ua_total_w_per_k"]:
        ratio = fit["ua_w_per_k"] / online_cmp["ua_total_w_per_k"]
        if 0.7 <= ratio <= 1.4:
            parts.append("Komt overeen met Project 8's doorlopende kalibratie.")
        else:
            parts.append(f"Wijkt af van Project 8's 48u-kalibratie (jaarschatting {ratio:.1f}× zo hoog) "
                         "— geen van beide is per definitie de waarheid: Project 8's korte venster kan "
                         "ua_env/ua_party/q_int onderling verwarren (zie de railed-parameters-diagnose), "
                         "maar deze jaarfit ziet ook alleen (T_uit − T_in) en niet de buurwarmte apart "
                         "— als de buurtemperatuur meeschaalt met het buitenweer, kan die warmte-instroom "
                         "hier deels in de isolatieschatting terechtkomen. Beschouw dit als twee "
                         "onafhankelijke, elk onvolmaakte metingen, niet als een uitspraak welke wint.")
    return " ".join(parts)


# ════════════════════════════════════════════════════════════════════════════════════
#  Maand-aggregaat (grof — géén dag/uur-detail, zie CLAUDE.md privacy-afspraak)
# ════════════════════════════════════════════════════════════════════════════════════

def monthly_trend(tado_hourly: dict[str, dict], weather_hourly: dict[str, dict],
                  ua_w_per_k: float | None) -> list[dict]:
    months: dict[str, list[float]] = defaultdict(list)
    for key in sorted(set(tado_hourly) & set(weather_hourly)):
        month = key[:7]
        delta = weather_hourly[key]["T_out"] - tado_hourly[key]["T_in"]
        months[month].append(delta)
    out = []
    for month in sorted(months):
        deltas = months[month]
        mean_delta = sum(deltas) / len(deltas)
        row = {"month": month, "n_hours": len(deltas), "mean_delta_t": round(mean_delta, 2)}
        if ua_w_per_k is not None:
            row["mean_loss_w"] = round(-ua_w_per_k * mean_delta, 1)
        out.append(row)
    return out


# ════════════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════════════

def analyse_rooms(house: dict, inputs: dict[str, str]) -> dict:
    """`inputs` = {room_id: pad-naar-lokale-tado-cache}. Nooit gecommit; de aanroeper
    (CLI/main) is verantwoordelijk voor paden buiten de git working tree."""
    parsed = {rid: parse_tado_cache(path) for rid, path in inputs.items()}
    hourly = {rid: resample_hourly(samples) for rid, samples in parsed.items()}

    all_keys = [k for h in hourly.values() for k in h]
    if not all_keys:
        raise SystemExit("Geen bruikbare tado-samples gevonden in de opgegeven bestanden.")
    start = min(all_keys)[:10]
    end = max(all_keys)[:10]

    weather = fetch_om_archive_hourly(start, end)
    solar = solar_series_per_room(house, weather)

    learned = load_learned()
    params = merged_params(house, learned)

    fits = {}
    for rid in inputs:
        fits[rid] = fit_room_ua(house, rid, hourly[rid], weather, solar.get(rid, {}))

    ok_rooms = sorted((r for r in fits if fits[r]["status"] == "ok"),
                      key=lambda r: fits[r]["ua_per_m2"] if fits[r]["ua_per_m2"] is not None else 1e9)
    rank_of = {rid: i + 1 for i, rid in enumerate(ok_rooms)}

    rooms_out = {}
    for rid in inputs:
        geom = room_geometry_summary(house, rid)
        online_cmp = online_ua_estimate(house, params, rid)
        fit = fits[rid]
        rooms_out[rid] = {
            **fit,
            "geometry": geom,
            "online_compare": online_cmp,
            "rank": rank_of.get(rid),
            "rank_total": len(ok_rooms),
            "narrative": build_narrative(fit, geom, online_cmp, rank_of.get(rid), len(ok_rooms)),
            "monthly_trend": monthly_trend(hourly[rid], weather,
                                           fit["ua_w_per_k"] if fit["status"] == "ok" else None),
        }

    # Alleen kamers mét een tado-sensor tellen mee voor de dekkingsgraad — "stair" heeft
    # er structureel geen (zie house_model.json) en kan dus nooit "gedekt" raken.
    sensored_rooms = {rid for rid, r in house.get("rooms", {}).items() if r.get("from_window_data")}
    return {
        "generated_at": utc_now_iso(),
        "source": "insulation_analysis",
        "method": ("vrije-uitloop-regressie (callForHeat==NONE): dT_in/dt = k·(T_out−T_in) "
                  "+ s·I_zon + c, gefit over tado-dayReport-export vs Open-Meteo ERA5-archief"),
        "data_period": {"start": start, "end": end},
        "rooms_covered": sorted(inputs),
        "rooms_missing": sorted(sensored_rooms - set(inputs)),
        "rooms": rooms_out,
        "overall_ranking": ok_rooms,
        "caveats": [
            "Zoninstraling gebruikt elk raam z'n standaard-zonweringsstand — er is geen "
            "jaarlang-bewaarde openingen-/zonweringslog om de werkelijke stand per uur te kennen.",
            "UA in W/K is afgeleid via een geometrie-gebaseerde warmtecapaciteit-aanname "
            "(dezelfde als Project 8's prior); de k-waarde (1/h) zelf is de robuustere, "
            "aannamevrije vergelijking tussen kamers.",
            "De trap-zone heeft geen tado-sensor en wordt hier nooit meegenomen.",
            "Deze regressie kent geen aparte buurwarmte-term (Project 8's ua_party): als de "
            "buurtemperatuur meeschaalt met het buitenweer, kan een deel van die warmte-instroom "
            "hier abusievelijk als 'isolatie' worden gemeten. Zie een grote afwijking van Project 8's "
            "kalibratie daarom als twee onvolmaakte metingen, niet als een tie-breaker.",
        ],
    }


def _parse_input_arg(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"verwacht 'kamer=/pad/naar/bestand.json', kreeg {spec!r}")
    room, path = spec.split("=", 1)
    return room, path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", action="append", required=True, type=_parse_input_arg,
                    metavar="kamer=/pad/naar/tado_cache.json",
                    help="Herhaalbaar: één per kamer. Kamer-id's zoals in house_model.json "
                         "(living, ted, hotties, office, bath).")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    inputs = dict(args.input)
    house = load_house()
    unknown = set(inputs) - set(house.get("rooms", {}))
    if unknown:
        raise SystemExit(f"Onbekende kamer-id('s): {sorted(unknown)} — zie house_model.json.")

    result = analyse_rooms(house, inputs)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"[OUT] {args.out} geschreven ({len(inputs)} kamer(s): {sorted(inputs)})")
    for rid in sorted(inputs):
        print(f"  {rid}: {result['rooms'][rid]['narrative']}")


if __name__ == "__main__":
    main()

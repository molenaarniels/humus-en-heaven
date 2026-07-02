"""Zonwering-adviseur (Project 9) — wanneer welke bedienbare zonwering dicht?

De luchtstroom-tweeling (Project 8) kent de zonwinst per raam door het glas
(gevel-azimut, invalshoek, statische `shading` × bedienbare `shade` in
house_model.json) maar adviseert er niet over. Dit script vult dat gat met
twee standen (env `SHADE_MODE`, zoals het zandbak-ochtend/avond-patroon):

- **plan** (orchestrator-doel 08:15): bouw voor vandaag een 15-min-raster van de
  *vermijdbare* zonwinst per bedienbaar-beschaduwd raam — doorgelaten W in de
  huidige gemelde stand minus volledig dicht — en stuur één dagplan-bericht op
  dagen dat het uitmaakt (warme dag én ≥1 raam met genoeg vermijdbare Wh).
  Schrijft `shade_state.json` (datum, plan, reminder-tijd) — de orchestrator
  leest daar de reminder-tijd uit.
- **reminder** (orchestrator, zodra `reminder_at` bereikt): één herinnering op
  het eerste dicht-moment, alléén als de voorspelde zonlast er nu ook echt is
  (materialisatie-check op de actuele Open-Meteo-instraling; bewolkt → uitstel,
  na het geplande open-moment → stil opgeven). Idempotent: dubbele dispatches
  zijn no-ops op de state.

Puur zon-geometrie + forecast — géén thermische simulatie, geen tado. Leest de
openingen-log (met de gemelde zonwering-standen) read-only uit de Gist, net als
Project 8. Telegram naar de privé-chat.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import airflow_model as am
from notify import run_guarded, sanitize_error, send_telegram
from shared_const import TZ, format_date_nl

# ── Constantes ────────────────────────────────────────────────────────────────────────
SHADE_WARM_DAY_C = 22.0        # dag-max-poort (spiegel van window_advisor.WARM_DAY_MAX):
                               # onder deze forecast-max is zonwinst gratis verwarming → stil
SHADE_MIN_DELTA_WH = 500.0     # per raam: minimale vermijdbare dag-winst (Wh) voor een plan-regel
SHADE_CLOSE_WM = 150.0         # momentane vermijdbare W → "vanaf hier dicht"
SHADE_OPEN_WM = 80.0           # hysterese: weer open onder deze W (voorkomt flapperen)
SHADE_MATERIALIZE_FRAC = 0.6   # reminder alleen als ≥ dit deel van de voorspelde ΔW er nu is
GRID_MIN = 15                  # rasterstap (minuten), zoals de tweeling-timeline
DAY_START_H, DAY_END_H = 6, 22  # plan-venster (lokale uren) — buiten dit venster staat de zon
                                # te laag om zonwering-advies te rechtvaardigen

STATE_FILE = os.getenv("SHADE_STATE_FILE", "shade_state.json")


# ── Pure hulpfuncties ─────────────────────────────────────────────────────────────────

def operable_shade_windows(house: dict) -> dict:
    """Alle ramen met een bedienbare `shade`-laag (runtime afgeleid — een nieuwe
    zonwering in house_model.json doet automatisch mee)."""
    return {wid: w for wid, w in house.get("windows", {}).items() if w.get("shade")}


def _shorten(lbl: str) -> str:
    """Label vóór de eerste toelichting (haakjes/streepje) — kort genoeg voor Telegram."""
    return lbl.split(" (")[0].split(" — ")[0].strip()


def short_label(w: dict, wid: str) -> str:
    """Korte raam-naam voor het bericht."""
    return _shorten(w.get("label", wid))


def closed_states(states: dict, shade_wids) -> dict:
    """Kopie van de gemelde toestand met álle bedienbare zonweringen op dicht
    (coverage-lamella: dekking 1.0 — zelfde sleutelwaarde `dicht`)."""
    st = dict(states)
    for wid in shade_wids:
        st[wid + "_shade"] = "dicht"
    return st


def avoidable_series(house: dict, rows: list[dict], states_now: dict,
                     day_grid: list[datetime], lat: float, lon: float) -> dict:
    """Per bedienbaar-beschaduwd raam de vermijdbare zonwinst ΔW(t) over het raster:
    doorgelaten W in de huidige gemelde stand minus volledig dicht. Een al-dicht
    gemelde zonwering geeft vanzelf ΔW≈0 en valt onder de drempels weg."""
    shades = operable_shade_windows(house)
    st_closed = closed_states(states_now, shades)
    out = {wid: [] for wid in shades}
    step = timedelta(minutes=GRID_MIN)
    for t in day_grid:
        ts = t + step / 2   # stap-midden, zoals de tweeling-timeline
        s_az, s_el = am.sun_position(lat, lon, ts.astimezone(timezone.utc))
        direct = am._interp_hourly(rows, ts, "direct")
        diffuse = am._interp_hourly(rows, ts, "diffuse")
        pw_cur = am.per_window_solar(house, states_now, s_az, s_el, direct, diffuse,
                                     beam_iam=True)
        pw_closed = am.per_window_solar(house, st_closed, s_az, s_el, direct, diffuse,
                                        beam_iam=True)
        for wid in shades:
            out[wid].append((t, max(0.0, pw_cur[wid] - pw_closed[wid])))
    return out


def close_interval(series: list[tuple], close_wm: float = SHADE_CLOSE_WM,
                   open_wm: float = SHADE_OPEN_WM) -> dict | None:
    """Beste dicht-interval met hysterese: in bij ΔW ≥ close_wm, uit onder open_wm.
    Bij meerdere pieken wint het span met de grootste geïntegreerde winst. Geeft
    {"start", "end", "wh"} (Wh over het span) of None."""
    spans = []
    cur = None
    for t, dw in series:
        if cur is None:
            if dw >= close_wm:
                cur = {"start": t, "end": t, "wh": 0.0}
        if cur is not None:
            if dw < open_wm:
                spans.append(cur)
                cur = None
            else:
                cur["end"] = t
                cur["wh"] += dw * (GRID_MIN / 60.0)
    if cur is not None:
        spans.append(cur)
    if not spans:
        return None
    return max(spans, key=lambda s: s["wh"])


def day_max_temp(rows: list[dict], today) -> float | None:
    temps = [r["T_out"] for r in rows
             if r.get("T_out") is not None and r["dt"].astimezone(TZ).date() == today]
    return max(temps) if temps else None


def build_plan(house: dict, rows: list[dict], states_now: dict, now: datetime,
               lat: float, lon: float) -> dict:
    """Het dagplan: per bedienbaar-beschaduwd raam het dicht-interval + de
    vermijdbare winst, plus de dag-poort. Puur — geen IO."""
    today = now.date()
    start = now.replace(hour=DAY_START_H, minute=0, second=0, microsecond=0)
    end = now.replace(hour=DAY_END_H, minute=0, second=0, microsecond=0)
    grid = []
    t = start
    while t <= end:
        grid.append(t)
        t += timedelta(minutes=GRID_MIN)

    dmax = day_max_temp(rows, today)
    warm = dmax is not None and dmax >= SHADE_WARM_DAY_C

    windows = []
    if warm:
        series = avoidable_series(house, rows, states_now, grid, lat, lon)
        for wid, ser in series.items():
            total_wh = sum(dw for _, dw in ser) * (GRID_MIN / 60.0)
            if total_wh < SHADE_MIN_DELTA_WH:
                continue
            span = close_interval(ser)
            if span is None:
                continue
            w = house["windows"][wid]
            windows.append({
                "id": wid,
                "label": short_label(w, wid),
                "shade_label": _shorten((w.get("shade") or {}).get("label", "zonwering")),
                "room": w.get("room"),
                "close": max(span["start"], now).strftime("%H:%M"),
                "open": span["end"].strftime("%H:%M"),
                "delta_wh": round(span["wh"]),
            })
        windows.sort(key=lambda x: x["close"])
    return {"date": today.isoformat(), "day_max": dmax, "warm_day": warm,
            "windows": windows}


def plan_message(plan: dict, now: datetime) -> str:
    lines = [f"🕶️ <b>Zonwering-plan — {format_date_nl(now.date())}</b>"
             + (f" (max {plan['day_max']:.0f}°)" if plan.get("day_max") is not None else "")]
    for w in plan["windows"]:
        kwh = w["delta_wh"] / 1000.0
        lines.append(f"☀️ <b>{w['label']}</b> ({w['shade_label'].lower()}): "
                     f"dicht {w['close']}–{w['open']} · scheelt ~{kwh:.1f} kWh")
    first = plan["windows"][0]
    lines.append(f"\nEerste actie rond {first['close']} — je krijgt dan één herinnering.")
    return "\n".join(lines)


def reminder_message(entry: dict, dw_now: float) -> str:
    return (f"🕶️ <b>Nu dichtdoen:</b> {entry['shade_label'].lower()} — "
            f"{entry['label']}: er komt nu ~{dw_now:.0f} W zon doorheen "
            f"(tot ~{entry['open']}).")


# ── State-IO (klein, zoals sandbox_state.json) ────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _send(msg: str) -> None:
    print(msg)
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN=1, niet verzonden.")
        return
    send_telegram(msg)


# ── Standen ───────────────────────────────────────────────────────────────────────────

def run_plan(now: datetime) -> None:
    house = am.load_house()
    loc = house.get("location", {})
    lat = loc.get("lat", am._LAT)
    lon = loc.get("lon", am._LON)
    am._LAT, am._LON = lat, lon   # fetch_weather leest de module-globals (main()-patroon)

    weather = am.fetch_weather()
    log = am.load_openings_log()
    states_now = am.openings_at(log, now)

    plan = build_plan(house, weather["hourly"], states_now, now, lat, lon)
    state = {"date": plan["date"], "plan_sent": False, "reminder_at": None,
             "reminder_sent": False, "windows": plan["windows"]}
    if plan["windows"]:
        _send(plan_message(plan, now))
        state["plan_sent"] = True
        state["reminder_at"] = plan["windows"][0]["close"]
    else:
        reason = ("koele dag" if not plan["warm_day"] else "te weinig vermijdbare zonwinst")
        print(f"[zonwering] geen plan vandaag ({reason}, "
              f"dag-max {plan['day_max']}) — stil.")
    save_state(state)


def _instant_delta(house: dict, states_now: dict, wid: str, now: datetime,
                   lat: float, lon: float, direct: float, diffuse: float) -> float:
    """Momentane vermijdbare ΔW voor één raam op de actuele instraling."""
    s_az, s_el = am.sun_position(lat, lon, now.astimezone(timezone.utc))
    shades = operable_shade_windows(house)
    pw_cur = am.per_window_solar(house, states_now, s_az, s_el, direct, diffuse,
                                 beam_iam=True)
    pw_closed = am.per_window_solar(house, closed_states(states_now, shades),
                                    s_az, s_el, direct, diffuse, beam_iam=True)
    return max(0.0, pw_cur[wid] - pw_closed[wid])


def run_reminder(now: datetime) -> None:
    state = load_state()
    if (state.get("date") != now.date().isoformat() or not state.get("plan_sent")
            or state.get("reminder_sent") or not state.get("reminder_at")):
        print("[zonwering] geen openstaande reminder — no-op.")
        return
    hh, mm = state["reminder_at"].split(":")
    if now < now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0):
        print(f"[zonwering] reminder pas om {state['reminder_at']} — no-op.")
        return

    entry = state["windows"][0]
    house = am.load_house()
    loc = house.get("location", {})
    lat = loc.get("lat", am._LAT)
    lon = loc.get("lon", am._LON)

    # Na het geplande open-moment heeft herinneren geen zin meer → stil opgeven.
    oh, om_ = entry["open"].split(":")
    if now >= now.replace(hour=int(oh), minute=int(om_), second=0, microsecond=0):
        print("[zonwering] dicht-venster al voorbij — reminder stilletjes opgegeven.")
        state["reminder_sent"] = True
        save_state(state)
        return

    # Materialisatie-check: is de voorspelde zonlast er nu ook echt? De actuele
    # Open-Meteo-instraling levert direct; diffuus = globaal − direct (beide horizontaal).
    am._LAT, am._LON = lat, lon
    dw_now = None
    try:
        cur = am.fetch_weather().get("current", {}) or {}
        direct = cur.get("direct_radiation")
        sw = cur.get("shortwave_radiation")
        if direct is not None and sw is not None:
            log = am.load_openings_log()
            states_now = am.openings_at(log, now)
            dw_now = _instant_delta(house, states_now, entry["id"], now, lat, lon,
                                    direct, max(0.0, sw - direct))
    except Exception as e:  # transient — volgende dispatch probeert opnieuw
        print(f"[zonwering] materialisatie-check faalde ({sanitize_error(e)}) — uitstel.")
        return
    if dw_now is None:
        print("[zonwering] geen actuele instraling beschikbaar — uitstel.")
        return
    if dw_now < SHADE_CLOSE_WM * SHADE_MATERIALIZE_FRAC:
        print(f"[zonwering] zonlast materialiseert (nog) niet "
              f"(ΔW nu {dw_now:.0f} W < {SHADE_CLOSE_WM * SHADE_MATERIALIZE_FRAC:.0f} W) — uitstel.")
        return

    _send(reminder_message(entry, dw_now))
    state["reminder_sent"] = True
    save_state(state)


def main() -> None:
    now = datetime.now(TZ)
    mode = os.getenv("SHADE_MODE", "plan").strip().lower()
    print(f"[zonwering] Start — {now.isoformat()} — mode={mode}")
    if mode == "reminder":
        run_reminder(now)
    else:
        run_plan(now)


if __name__ == "__main__":
    run_guarded(main, "zonwering", fail_threshold=2)

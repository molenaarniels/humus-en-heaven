# Code Quality Audit — Humus & Heaven

**Date:** 2026-07-01 · **Scope:** all 8 pipelines, shared modules, workflows, frontend (docs/), tests.
**Dimensions:** maintainability · security · reliability · duplication/inefficiency.

Line numbers refer to the state *before* the fixes listed in [Fixed in this audit](#fixed-in-this-audit).

---

## Executive summary

| Dimension | Verdict |
|---|---|
| **Security** | **Strong.** No `pull_request`/`pull_request_target` triggers anywhere; disciplined `sanitize_error` usage at every credential-bearing call site; CSP `script-src` without `unsafe-inline` genuinely holds on all 8 dashboard pages; SRI hashes present and byte-identical across pages; no secret/station-ID leakage in any committed JSON artifact; localStorage `gh_token` is only ever sent to `api.github.com`. |
| **Reliability** | **Good, with a few concentrated risks.** Retry/backoff is centralized (`http_util`) and used at all Open-Meteo sites; state-file corruption is handled gracefully everywhere. The top risk is the tado token-rotation chain (single point of failure, no cross-run recovery). A handful of unguarded partial-API-response paths existed (fixed below). |
| **Maintainability** | **Mixed.** Constants are exemplary — documented with rationale and calibration provenance. But the three biggest modules (`airflow_model.py` 2487 lines, `soil_model.py` 1096, `window_advisor.py` 1013) mix model, I/O and formatting; `airflow_model.main` is ~200 lines. Test coverage is strong on physics/pure helpers, weak on decision layers. |
| **Duplication** | **The biggest debt.** Repeated Open-Meteo fetch boilerplate (×4), Dutch date formatting (×2, fixed), Gist-log loaders (×2), state I/O (×2), workflow YAML scaffolding (dedup-guard ×5, quarter-hour loop ×2), and substantial frontend copy-paste (palette ×4 — fixed to ×2, tooltip config ×9, `loadData()` ×5, scatter logic ×2). |

---

## Per-project scorecard

| Project | Maint. | Security | Reliab. | Dupl. | Notes |
|---|---|---|---|---|---|
| 1 Soil moisture | B | A | A− | B | Physics well-tested vs FAO-56; `assess_status` (decision layer) untested; 1096-line module |
| 2 Weather briefing | B+ | A | B+ | B | `parse_hourly` crashed on partial responses (fixed); solar-bonus magic numbers |
| 3 Sandbox | B | A | B+ | B | Whole state machine was untested (fixed); `forecast[0]` guard added |
| 4 Heating | A | A | A | A | 38 lines, nothing to flag |
| 5 Mowing advisor | B+ | A | B+ | B | `date.today()` timezone bug (fixed); all-or-nothing `lawn_T` fallback |
| 6 Window advisor | B | A− | B− | B− | Token rotation SPOF (top finding); rotation path untested; `main()` ~153 lines |
| 7 Station accuracy | B− | A | B | B+ | No test file at all; WU fetch had no retry (fixed) — and it's the calibration source |
| 8 Airflow twin | B− | A | B+ | B− | 2487 lines, `main()` ~200 lines; NaN guards partial; exemplary constant documentation |
| Shared modules | A | A | A | A | `notify`/`http_util`/`gist_io`/`wu_bias` small, tested, well-designed |
| Workflows | B− | B+ | B | C+ | No fork-PR vectors; heavy YAML copy-paste; push-race between the three 15-min loops |
| Frontend | B− | A− | B+ | C+ | CSP/SRI solid; heavy cross-page duplication; `airflow.js` 1289-line monolith |

---

## Findings

### Reliability

| # | Sev | Where | Finding | Status |
|---|---|---|---|---|
| R1 | **High** | `window_advisor.py:182-206` | tado token rotation has no cross-run recovery: tado revokes the old refresh token on exchange; the new one is persisted only to the Gist (6 retries, ~62 s). If the Gist is unreachable that whole window, the valid token dies with the process → chain broken → manual `tado_auth_bootstrap.py`. A local-file backup retried next iteration would make this self-healing. | Open (Gist-write logic is ground-rule protected — needs an explicit decision) |
| R2 | Med | `mowing_advisor.py:198,576` | `date.today()` is runner-UTC, not Amsterdam — near-midnight runs (and DST edges) shift `today`, `today_idx` and reset-date logic by a day. | **Fixed** → `local_today()` |
| R3 | Med | `check_and_notify.py:163` | Naive `datetime.now()` + locale-dependent `%A %d %B %Y` → English day/month names in the Dutch message (no `nl_NL` locale on runners) and possibly yesterday's date in the evening. | **Fixed** → `local_today()` + shared `format_date_nl` |
| R4 | Med | `sandbox_notify.py:111,174` | `morning_check`/`evening_check` deref `forecast[0]`; an empty `daily` array (API hiccup) → IndexError. `len < 2` was handled, `len == 0` was not. | **Fixed** — empty-forecast guard in `main()`, state untouched |
| R5 | Med | `weather_briefing.py:147-154` | `parse_hourly` indexed required hourly keys without guards — a partial Open-Meteo response lost the whole day's briefing instead of degrading. | **Fixed** — pad/skip + neutral defaults |
| R6 | Med | `station_accuracy.py:73` | WU per-day history GET had no retry: a transient 5xx silently shrank the calibration sample — and this script is the calibration source for `wu_bias.SOLAR_BIAS_SLOPE`. | **Fixed** — 3-attempt backoff (bespoke; WU stays outside `http_util` per convention) |
| R7 | Med | `window-notify.yml:84-92`, `airflow-notify.yml`, daily workflows | Three loops push to `main` every 15 min in *separate* concurrency groups. The loops tolerate a lost `pull --rebase`/push race (retried next iteration), but daily jobs hard-fail on it (`daily-check.yml:113`, `mowing-notify.yml:103`, `sandbox-notify.yml:181`). | Open — consider a shared concurrency group or a push-retry step for daily jobs |
| R8 | Low | `airflow_model.py:1524-1618` | `calibrate` guards NaN per GN step, but the final blended `rmse` is unguarded until a later `rmse_now == rmse_now` check; params are not clamped to `BOUNDS` inside the solver (rails only reported). | Open |
| R9 | Low | `mowing_advisor.py:161` | `load_soil_days` rejects the entire dataset if *any* day lacks `lawn_T` — one malformed day forces GDD-fallback even when 34/35 days are fine. | Open |
| R10 | Low | repo-wide | Three different `generated_at` formats (`+00:00`, `.replace(tzinfo=None)+"Z"`, `.replace("+00:00","Z")`). Documented as intentional, but every consumer must handle all three. | Open (schema is additive-only; normalize opportunistically) |

### Security

The posture is stronger than typical for a public automation repo. Verified positives:

- **No `pull_request`/`pull_request_target` triggers** in any of the 11 workflows (`tests.yml` is push-only) — the classic fork-PR secret-exfiltration vector is closed.
- **`sanitize_error` used at every credential-bearing exception site** (`window_advisor.py:313`, `station_accuracy.py:94`, `http_util.py:41`, `notify.py:101-103`); the token-persist path never prints the token.
- **Frontend:** no inline `<script>`/`onclick=` anywhere → `script-src` without `unsafe-inline` genuinely holds; SRI hashes byte-identical across pages; `connect-src` correctly scoped per page (writers → `api.github.com`, read-only → `'self'`, `model.html` → `'none'`); no token/gist-id/station-id in any committed `docs/*.json`; write pages warn against classic PATs.

| # | Sev | Where | Finding | Status |
|---|---|---|---|---|
| S1 | Low | `soil_model.py:763`, `mowing_advisor.py:156`, `sandbox_notify.py:54` | Three raw `{e}` prints violated the sanitize-everything convention. No active leak (local-file/math errors), but a future refactor routing a credentialed error through them would leak silently. | **Fixed** — routed through `sanitize_error` |
| S2 | Low | `window-notify.yml:24`, `airflow-notify.yml:20` | The two loop jobs hold `contents: write` for the whole job (incl. `pip install` + network parsing). The daily workflows correctly split guard (read) from commit (write) jobs. | Open — least-privilege polish |
| S3 | Low | `station-accuracy.yml:45` | Free-form `workflow_dispatch` input `inputs.days` reaches `int()` unvalidated (via `env:`, so no shell injection; collaborator-only dispatch). `override_status` is re-validated in Python — good pattern to copy. | Open |
| S4 | Info | all workflows | `actions/checkout@v5`/`setup-python@v6` are tag-pinned, not SHA-pinned. **Compliant with the repo's own policy** (SHA required only outside the `actions/` namespace); full-SHA pinning remains the stricter hardening option. | Policy-compliant |
| S5 | Info | all dashboards | `style-src 'unsafe-inline'` + values interpolated into `innerHTML` (room names, `status_text`) without escaping. Low risk while every data source is first-party (own pipelines + own Gist), but it is the one CSP hole. | Open — escape-on-insert helper would close it |

### Maintainability

| # | Sev | Where | Finding | Status |
|---|---|---|---|---|
| M1 | **High** | tests | Untested decision layers: `sandbox_notify` state machine, `check_and_notify` (no test file), `soil_model.assess_status` (irrigation go/no-go), `window_advisor` token rotation + fetch/dashboard paths, **all** of `station_accuracy` (produces the calibration constant). Physics/pure helpers are well covered (177 tests). | **Partially fixed** — added `test_sandbox_notify.py` (19 tests) + `test_check_and_notify.py` (11 tests); `assess_status`, rotation and `station_accuracy` still open |
| M2 | Med | `airflow_model.py` (2487), `soil_model.py` (1096), `window_advisor.py` (1013), `docs/js/airflow.js` (1289) | God-modules; `airflow_model.main` ~200 lines (house load → fetch → calibrate → simulate → suggest → dashboard → write, inline), `window_advisor.main` ~153. High-complexity untested entry points. | Open — split fetch/model/report when next touched |
| M3 | Med | `check_and_notify.py:111-134,205-239` | ~75 lines of dead email code (`send_email`, `format_email`, smtplib imports) reachable only from commented-out call sites. | **Fixed** — removed |
| M4 | Low | `airflow_model.py:1913` | Magic `0.7` (glass transmittance) and `0.6` (glass fraction) inline in the solar-gain hot path, undocumented — unlike every constant around them. | **Fixed** — `GLASS_TRANSMITTANCE`/`GLASS_AREA_FRACTION` |
| M5 | Low | `weather_briefing.py:213` | `solar_bonus = mean_dir * 0.012 * (1 - mean_wind/10)` — undocumented factors, the outlier in an otherwise well-documented file. | Open |
| M6 | Low | `soil_model.py:952` | `IRRIGATION_RATES` defined mid-file after first reference point; `seasonal_kcb:174` has an unreachable fallback `return 0.75`. | Open |
| M7 | Low | `window_advisor.py:689` / `airflow_model.py:1949` | Two different functions both named `_room_dashboard_row` in sibling modules — name collision invites confusion. | Open |

### Duplication / inefficiency

| # | Sev | Where | Finding | Status |
|---|---|---|---|---|
| D1 | Med | `weather_briefing:96`, `sandbox_notify:67`, `mowing_advisor:180`, `soil_model` | Open-Meteo fetch boilerplate ×4: hardcoded base URL + the `for i, t in enumerate(d["time"])` unpack pattern. `http_util` shares transport; the params-build/unpack layer was never factored. | Open — small `open_meteo.py` helper or base-URL constant in `shared_const` |
| D2 | Med | `sandbox_notify:269-275`, `mowing_advisor:107-108,483` | Dutch day/month lists + formatter duplicated. | **Fixed** — single `shared_const.format_date_nl` |
| D3 | Med | `check_and_notify:98` / `mowing_advisor:115`; `sandbox_notify:46/58` / `mowing_advisor:460/472` | Near-identical Gist-log loaders (incl. the `GH_TOKEN or GITHUB_TOKEN` lookup) and load/save_state pairs. | Open |
| D4 | Med | `window_advisor.py:874-883` ↔ `airflow_model.py:2310-2321` | The WU outside-now refinement wrapper (solar-driver pick + `correct_temp` + `src` tag) is copy-pasted; only `fetch_wu_current_temp` itself is shared. | Open — factor a `refine_outside()` next to `wu_bias` |
| D5 | Med | workflows | Dedup-guard job ×5 (daily-check, weather-briefing, sandbox, heating, mowing — only the workflow filename differs); quarter-hour loop scaffold ×2 + cadence block ×3; checkout/setup-python/pip ×10. ~150+ removable lines via a reusable workflow / composite action. | Open |
| D6 | Med | `docs/` | Palette ×4 (`shared.css`, `index.html`, `model.html`, `theme.js`); Chart.js tooltip-style object ×9; `loadData()` fetch+banner ×5; `window.js` ↔ `grafiek.js` scatter fork (colors, legend, trend-arrow clamps); Gist CRUD + delegated-remove `index.js` ↔ `mowing.js`; modal CSS ×2. | **Partially fixed** — palette now ×2 (shared.css + documented theme.js mirror); rest open |
| D7 | Low | `soil_model.py:236,355` | Canopy-interception curve `I = C·(1−exp(−P/C))` computed in two places — a calibration change must be made twice. | Open |
| D8 | Low | `docs/js/accuracy.js:3,86,148` | Only charting page not using `theme.js` — hardcoded hex that will drift from the palette. | Open |
| D9 | Info | `docs/ipad.*`, `docs/sandbox_state.json` | ipad's isolation (own theme, no CDN, own fetch layer) is deliberate and documented; `docs/sandbox_state.json` is a deliberate copy (`sandbox-notify.yml:161` `cp` for ipad.js). **Not** duplication debt. | By design |

---

## Fixed in this audit

All behavior-preserving or strictly-hardening; no FAO-56 formulas, soil parameters, Gist-write logic or `data.json` schema touched. `ruff` + full test suite green (211 passed, 30 tests added).

1. **Timezone + Dutch dates** — `shared_const` gains `NL_DAYS`/`NL_MONTHS`/`format_date_nl()`; `check_and_notify` now stamps `local_today()` with Dutch names (was naive UTC + English locale); `sandbox_notify`/`mowing_advisor` reuse the shared formatter; `mowing_advisor` + `check_and_notify.bootstrap_monthly_totals` use `local_today()` instead of `date.today()`.
2. **Dead code removed** — `check_and_notify` email path (`send_email`, `format_email`, smtplib/MIMEText imports, commented call sites; ~75 lines).
3. **`sanitize_error` convention** — the 3 remaining raw `{e}` prints now sanitized.
4. **Partial-data guards** — `sandbox_notify.main` handles an empty forecast (state untouched); `weather_briefing.parse_hourly` pads short/missing series, skips temp-less hours, defaults the rest neutrally.
5. **`station_accuracy.fetch_wu_hourly`** — 3-attempt retry with backoff (bespoke, apiKey never printed).
6. **`airflow_model`** — named `GLASS_TRANSMITTANCE`/`GLASS_AREA_FRACTION` constants (numerically identical).
7. **Frontend** — `index.js` tooltip null-guards; palette single-sourced in `shared.css` (page `:root` blocks removed, `--wet` migrated, `theme.js` documented as the JS mirror; `?v=2` cache-bust).
8. **Tests** — `tests/test_sandbox_notify.py` (full state-machine matrix + empty-forecast guard), `tests/test_check_and_notify.py` (θ-seed clamp/corruption, Telegram formatting incl. Dutch-date assertion).

## Recommendations backlog (highest value first)

1. **R1 — token-rotation local backup** (small, but touches the protected Gist-write path → do deliberately): write the freshly rotated token to a runner-local file before the Gist persist and retry persisting it next iteration.
2. **Tests for `soil_model.assess_status`** — the irrigation go/no-go heuristics (DRY_GUARD, SKIP_HORIZON) are the highest-value remaining untested logic. Then `station_accuracy` (`_pearson`, `_slope`, `_recommend_slope`, `pair_hours`) and the token-rotation path (mock Gist + tado).
3. **Reusable workflow / composite action** for the dedup-guard job (×5) and the quarter-hour loop scaffold (×2) — removes the bulk of the YAML and makes the loop semantics single-sourced.
4. **`refine_outside()` helper** shared by `window_advisor` + `airflow_model` (D4), and an Open-Meteo params/unpack helper (D1).
5. **Frontend shared helpers** — `chartTooltip()` + `baseAxis()` + `loadData()` in `shared.js`/`theme.js`; port `accuracy.js` onto `theme.js`; merge the `window.js`/`grafiek.js` scatter into one module.
6. **Split `airflow_model.main`/`window_advisor.main`** into fetch → model → report functions when next touched (M2) — enables testing the entry points.
7. **Least-privilege polish** — split the two loop jobs' `contents: write` like the daily workflows (S2); validate `inputs.days` in Python like `override_status` (S3).
8. **HTML-escape helper** for `innerHTML` interpolations (S5) — cheap insurance if any data source ever stops being first-party.

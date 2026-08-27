# HANDOFF — Momentum Strategy Lab V2 (2026-08-27)

## Current live state (VPS: root@147.93.56.10, key `~/.ssh/robotcripto_momentum_deploy`)

- **robotcripto-momentum-strategy-lab**: ACTIVE, running ALONE (baseline stopped per user
  request), SHADOW_MODE=true, REAL_ORDERS=0. Dashboard: `http://147.93.56.10:8802/`.
- **robotcripto-momentum** (baseline bot): STOPPED (`systemctl stop`), NOT to be restarted
  during this observation window per explicit user instruction. Its data is intact and
  backed up (see below) — nothing in `momentum/` was modified.
- **Arbitrage bot** (robotcripto-engine/dashboard/shadow/altcoin-scanner): all 4 ACTIVE,
  untouched throughout.
- DB: `/opt/robotcripto-momentum/db/strategy_lab.db`, dataset tagged `dataset_version="v2"`,
  `dataset_phase="RESEARCH"` (walk-forward, see `strategy_lab/walk_forward.py`).
- Backups (non-destructive, all under `/opt/robotcripto-momentum/db/backups/`):
  `momentum_backup_<ts>.db` (+schema+config) = baseline's full state before stopping,
  integrity-checked OK. `strategy_lab_v1_devtest_<ts>.db` and `strategy_lab_v2_*_<ts>.db` =
  earlier dev/test runs (some under since-fixed bugs, some under since-retuned compute
  settings) — kept, not part of the `v2` dataset used for the eventual comparison.

## Architecture

- `momentum/` = existing baseline bot. **Untouched this entire session** (verified via
  `git status momentum/` empty and an import-check test). Rule 0: never modify.
- `strategy_lab/` = new, fully separate package. Own adapters/StateStore/Universe/health/
  ledger/dashboard, importing only stateless/pure pieces from `momentum/` (engines, data
  types, ShadowBroker, exchange adapters) — no shared runtime state with the baseline.
  - `app.py` — entrypoint/orchestration (stage loop, per-symbol strategy fan-out).
  - `strategies/` — 5 independent strategies: `baseline_momentum_starting` (control, wraps
    the baseline's own starting-engine formula), `persistent_micro_trend`,
    `impulse_pullback_reacceleration` (3-phase state machine, priority strategy),
    `breakout_retest_continuation`, `cross_exchange_lead_lag`.
  - `meta_engine.py` — agreement/conflict cohort consolidation (analysis layer, not a gate).
  - `execution.py` — realistic shadow fills + 5 explicit latency variants (reuses
    `momentum.shadow.broker.ShadowBroker`).
  - `exit_lab.py` — 11 exit policies (4 fixed horizons + 7 named) replayed as counterfactuals
    against the same real forward price path; one policy closes the real ledger trade.
  - `fast_entry_lab.py` — 11 confirmation-window observations per actionable signal, no
    look-ahead (only resolved once real time has elapsed).
  - `missed_move_analyzer.py` / `false_positive_analyzer.py` — post-hoc only, never feed
    back into live decisions.
  - `dashboard/` — own FastAPI app, port 8802. New **Live Signal Feed** panel
    (`/api/live_signals`) shows every detected movement incl. rejected ones: strategy,
    symbol, direction, velocity_10s, acceleration_10s, persistence_score (NULL except for
    PERSISTENT_MICRO_TREND), phase/IPR-state (NULL except for IPR), exhaustion_risk,
    expected_net_edge_pct, accepted, reject_reason.
  - `safety/isolation_guard.py` + `tests/test_strategy_lab_isolation.py` — own AST scan
    proving no real-order capability, independent of the baseline's equivalent guard.
- `config/strategy_lab_config.yaml` — all Lab config. `db/strategy_lab_schema.sql` — own
  schema (separate SQLite file, never touches `momentum.db`).
- `deploy/robotcripto-momentum-strategy-lab.service` — own systemd unit, own dedicated
  user `robotcripto-momentum-lab` (member of `robotcripto-momentum` group for `db/` write
  access — see Known quirks below).

## Compute-budget tuning (important — do not casually "turn back up")

The 2-core VPS is much slower per-core than local dev hardware; local timing was **not**
predictive of VPS behavior. Two more generous configs each drifted into sustained
`degraded_mode` after several minutes even with the baseline fully stopped:
- 45 symbols/cycle, 25 max open trades, 2s cycle → sustained degraded.
- 15 symbols/cycle, 10 max open trades, 3s cycle → still drifted degraded after ~10 min.

**Current, VPS-verified-stable values** (`config/strategy_lab_config.yaml`):
`stage.cycle_interval_s: 3`, `compute_budget.max_symbols_full_pass: 10`,
`exit_lab.max_open_trades: 6`, `exit_lab.fixed_horizons_s: [5, 15, 30, 60]`.
Verified over a 5-minute window: degraded on 1/14 samples, open_trades held exactly at the
cap (no runaway), no errors. A single later spot-check showed one more degraded reading —
consistent with the established "occasional brief spike, self-correcting" pattern (not a
new incident): `open_trade_count` was still exactly at the cap, not growing past it.
**If resuming work here: do not raise these limits without re-verifying live on the VPS**
(not locally) over several minutes, the same way this round was tuned.

None of these are strategy-quality thresholds (min_score, exhaustion veto, net-edge
minimum) — those were never touched, per explicit instruction not to inflate trade count.

## Incidents this session (both fixed, both worth knowing about)

1. **Baseline degraded by the Lab's uncapped resource use** (while baseline was still
   running, before the user asked to stop it): `exit_lab.py` had no cap on concurrently
   tracked trades and no memoization of per-trade engine calls → cycle time hit 9-19s,
   baseline's own `degraded_mode` flipped true, WS reconnect storms followed. Fixed via a
   hard `max_open_trades` cap + per-`(exchange,symbol)` memoization in `ExitLab.tick()`.
   Baseline was restarted (standard recovery, no code touched) and confirmed fully healthy
   before this pivot to the current baseline-stopped test.
2. **"Already open" duplicate-entry bug**: strategies had no guard against re-entering the
   same (strategy, symbol, direction) every cycle while a move continued → 15 duplicate
   trades on one symbol in 70s. Fixed via `ExitLab.has_open_trade()`, checked in `app.py`'s
   entry gate.

## Tests

49/49 pass (`pytest -q` from repo root, venv at `.venv/`): 33 pre-existing baseline tests
(untouched, still green) + 16 new Lab tests, including UP/DOWN symmetry, the IPR 3-phase
cycle, exhaustion veto, breakout/retest, cross-exchange no-look-ahead, fees/spread/
slippage/latency, MFE/MAE, ledger isolation, baseline-immutability (import-graph check),
process isolation (systemd unit diff), and no-real-order-capability.

## Unresolved / open items

- **Baseline RSS/CPU growth**: while investigating (before the pivot), the baseline bot
  independently grew from ~150MB→1.3GB RSS and pegged near 100% CPU within about an hour of
  a fresh restart, with no Lab running — this looks like a pre-existing baseline behavior,
  not something to fix here (rule 0: never touch `momentum/`). Worth mentioning to the user
  if/when the baseline is restarted; not investigated further (out of scope).
- **Occasional Lab degraded blips**: not eliminated, just bounded (self-corrects, capped
  open-trade count). If a longer observation shows these growing more frequent/severe,
  cut `max_symbols_full_pass`/`max_open_trades` further using the same live-VPS-tuning
  method — do not guess from local timing.
- **INSUFFICIENT SAMPLE discipline**: `strategy_kpis`/`agreement_cohorts` dashboard rows
  already mark `status="INSUFFICIENT_SAMPLE"` below n=20 trades. Honor this in any report;
  do not compute expectancy/profit-factor claims below that bar.

## Next actions (when resuming, likely a new session)

1. Do **not** re-poll the VPS repeatedly to "check in" — query status/logs once per
   session in a single batched SSH call, per the user's cost-control instruction.
2. Let the Lab keep accumulating `dataset_version="v2"` data for the requested multi-hour
   window (user said "continue several hours", not bounded further as of this writing).
3. When the user asks for the comparison report: compute baseline KPIs from the **backed
   up** `momentum_backup_<ts>.db` (`shadow_trades` table: net_pnl, fees_paid, slippage_pct,
   r_multiple, mfe_pct, mae_pct) vs. Lab KPIs from `strategy_lab.db` filtered to
   `dataset_version='v2'` (via `/api/strategy_kpis`, `/api/agreement_cohorts`, or direct
   SQL) — TRUE_NET_PNL, expectancy, profit factor, max drawdown, trades/hour, MFE/MAE,
   missed moves, sample size. Report `INSUFFICIENT SAMPLE` for anything under n=20 rather
   than concluding.
4. Restarting the baseline bot is the user's call, not automatic — do not restart it
   unless asked.

"""MOMENTUM STRATEGY LAB V2 entrypoint (spec sections 0-20). A fully separate
asyncio process/service from momentum/app.py (the baseline bot): its own
adapters instances, own StateStore, own Universe/health/ledger/dashboard. It
imports read-only, proven pieces from `momentum` (exchange adapters, engines,
ShadowBroker, StateStore) but shares no runtime object, no DB connection, no
service, and no data with the baseline process - rule 0.

SAFETY: SHADOW_MODE=true, REAL_ORDERS=0, structurally enforced the same way as
the baseline (see strategy_lab/safety/isolation_guard.py + this file's own
assert). No authenticated exchange client exists anywhere in this tree.
"""
from __future__ import annotations

import asyncio
import logging
import time

import uvicorn

from momentum.compute_budget import ComputeBudget
from momentum.data.state import StateStore
from momentum.engines import (
    acceleration, breakout, exhaustion, late_entry, orderbook_imbalance, orderflow, regime,
    velocity, volatility_expansion, volume as volume_engine,
)
from momentum.engines.cross_exchange import LeadLagTracker, compute as cross_exchange_compute
from momentum.engines.types import EngineScore
from momentum.exchanges.base import ExchangeAdapter
from momentum.exchanges.binance import BinanceAdapter
from momentum.exchanges.bybit import BybitAdapter
from momentum.exchanges.health import HealthRegistry
from momentum.exchanges.okx import OkxAdapter
from momentum.exchanges.universe import Universe

from strategy_lab.config import LabConfig, load_config
from strategy_lab.dashboard.api import create_app
from strategy_lab.execution import ShadowExecutionEngine
from strategy_lab.exit_lab import ExitLab
from strategy_lab.fast_entry_lab import FastEntryLab
from strategy_lab.ledger import LabLedger
from strategy_lab.market_bus import MarketEvent, build_market_event
from strategy_lab.meta_engine import compute_meta
from strategy_lab.missed_move_analyzer import MissedMoveAnalyzer
from strategy_lab.runtime import LabRuntime
from strategy_lab.strategies import baseline_momentum_starting, breakout_retest_continuation, persistent_micro_trend
from strategy_lab.strategies.base import StrategySignal
from strategy_lab.strategies.cross_exchange_lead_lag import CrossExchangeLeadLagTracker
from strategy_lab.strategies.impulse_pullback_reacceleration import ImpulsePullbackReaccelerationTracker
from strategy_lab.walk_forward import WalkForwardTag

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("strategy_lab.app")

EXCHANGE_PRIORITY = ("binance", "bybit", "okx")
CROSS_EXCHANGE_STATS_REFRESH_EVERY_N_CYCLES = 30   # ~60s at a 2s cycle interval


def _pick_primary(states_by_exchange: dict) -> tuple[str | None, object | None]:
    for ex in EXCHANGE_PRIORITY:
        st = states_by_exchange.get(ex)
        if st is not None and st.price_now() is not None:
            return ex, st
    return None, None


def _fast_score(store: StateStore, exchanges: list[str], symbol: str, now: float) -> float:
    """Cheap, single-horizon ranking score - just enough to decide which
    symbols earn a full 5-strategy pass this cycle (section 18's compute
    budget discipline), mirroring the baseline bot's Stage A pattern."""
    best = 0.0
    for ex in exchanges:
        st = store.get(ex, symbol)
        if st is None or st.is_stale(now):
            continue
        v = st.velocity_pct(now, 10)
        if v is not None and abs(v) > abs(best):
            best = v
    return best


def _weighted_engine_scores(state, now: float) -> dict[str, EngineScore]:
    return {
        "velocity": velocity.compute(state, now),
        "acceleration": acceleration.compute(state, now),
        "volume": volume_engine.compute(state, now),
        "orderflow": orderflow.compute(state, now),
        "orderbook_imbalance": orderbook_imbalance.compute(state, now),
        "breakout": breakout.compute(state, now),
        "volatility_expansion": volatility_expansion.compute(state, now),
    }


def build_adapter(exchange: str, cfg: LabConfig, health: HealthRegistry) -> ExchangeAdapter:
    per_conn = cfg.universe["ws_symbols_per_connection"][exchange]
    quote = cfg.universe["quote_asset"]
    h = health.get_or_create(exchange)
    if exchange == "binance":
        return BinanceAdapter(quote_asset=quote, ws_symbols_per_connection=per_conn, health=h)
    if exchange == "bybit":
        return BybitAdapter(quote_asset=quote, ws_symbols_per_connection=per_conn, health=h)
    if exchange == "okx":
        return OkxAdapter(quote_asset=quote, ws_symbols_per_connection=per_conn, health=h)
    raise ValueError(f"unknown exchange: {exchange}")


async def _dispatch_handlers(store: StateStore, exchange: str):
    async def on_trade(trade):
        store.get_or_create(exchange, trade.symbol).on_trade(trade)

    async def on_book_ticker(bt):
        store.get_or_create(exchange, bt.symbol).on_book_ticker(bt)

    async def on_depth(depth):
        store.get_or_create(exchange, depth.symbol).on_depth(depth)

    return on_trade, on_book_ticker, on_depth


async def _stream_exchange_supervisor(exchange: str, adapter: ExchangeAdapter, runtime: LabRuntime,
                                       on_trade, on_book_ticker, on_depth) -> None:
    while True:
        symbols = runtime.tracked_symbols_by_exchange.get(exchange, [])
        if not symbols:
            await asyncio.sleep(15)
            continue
        try:
            await adapter.stream_market_data(symbols, on_trade, on_book_ticker, on_depth)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s: lab stream_market_data supervisor crashed, restarting in 10s", exchange)
        await asyncio.sleep(10)


async def universe_refresh_loop(cfg: LabConfig, runtime: LabRuntime) -> None:
    while True:
        await asyncio.sleep(cfg.universe["refresh_interval_s"])
        for exchange, universe in runtime.universe_by_exchange.items():
            try:
                symbols = await universe.refresh()
                runtime.universe_size_by_exchange[exchange] = len(symbols)
                for sym in symbols:
                    f = universe.get_filter(sym)
                    if f:
                        await runtime.ledger.upsert_symbol(sym, exchange, f.base_asset, f.quote_asset,
                                                             f.tick_size, f.step_size, f.min_notional,
                                                             f.quote_volume_24h)
                if symbols and not runtime.tracked_symbols_by_exchange.get(exchange):
                    runtime.tracked_symbols_by_exchange[exchange] = symbols
                    logger.info("%s: lab universe recovered, %d symbols now tracked", exchange, len(symbols))
            except Exception:
                logger.exception("lab universe refresh failed for %s", exchange)


async def _open_shadow_trade(cfg: LabConfig, runtime: LabRuntime, execution: ShadowExecutionEngine,
                              exit_lab: ExitLab, signal_id: int, sig: StrategySignal, states_by_exchange: dict,
                              agreement_count: int, regime_label: str | None, wf_tag: WalkForwardTag,
                              risk_cfg: dict, now: float) -> None:
    state = states_by_exchange.get(sig.exchange)
    if state is None:
        return
    vol_pct = state.realized_vol(now, 60) or 0.2
    stop_distance_pct = max(0.15, vol_pct * 1.5)
    equity = risk_cfg["paper_account_equity"]
    risk_pct = risk_cfg["default_risk_pct"]
    risk_amount = equity * risk_pct
    price = sig.price
    if price <= 0:
        return
    size = risk_amount / (stop_distance_pct / 100.0 * price)

    universe = runtime.universe_by_exchange.get(sig.exchange)
    sym_filter = universe.get_filter(sig.symbol) if universe else None
    size = execution.apply_filters(sym_filter, size, price)
    if not size or size <= 0:
        return

    entry_fill = execution.simulate_entry(state, sig.direction, size, sig.exchange)
    if entry_fill is None:
        return

    stop_price = price * (1 - stop_distance_pct / 100.0) if sig.direction == "UP" else price * (1 + stop_distance_pct / 100.0)

    trade_id = await runtime.ledger.insert_trade(
        signal_id=signal_id, strategy=sig.strategy, symbol=sig.symbol, exchange=sig.exchange,
        direction=sig.direction, entry_price=entry_fill.avg_price, stop_price=stop_price, size=size,
        risk_pct=risk_pct, risk_amount=risk_amount,
        confirmation_window_s=cfg.fast_entry_lab_cfg["primary_confirmation_s"],
        entry_latency_ms=entry_fill.latency_ms, entry_fee=entry_fill.fee, entry_slippage_pct=entry_fill.slippage_pct,
        agreement_count=agreement_count, regime_label=regime_label, dataset_phase=wf_tag.phase,
        dataset_version=wf_tag.version,
    )
    v10 = state.velocity_pct(now, 10) or 0.0
    exit_lab.open_trade(trade_id, sig.strategy, sig.symbol, sig.exchange, sig.direction, now, entry_fill.avg_price,
                         stop_distance_pct, size, v10, wf_tag.phase, wf_tag.version)
    runtime.open_trade_count = exit_lab.open_count
    logger.info("LAB SHADOW ENTRY [%s] %s %s @ %.8f size=%.6f agreement=%d", sig.strategy, sig.symbol,
                sig.direction, entry_fill.avg_price, size, agreement_count)


async def stage_loop(cfg: LabConfig, store: StateStore, runtime: LabRuntime, execution: ShadowExecutionEngine,
                      exit_lab: ExitLab, fast_entry_lab: FastEntryLab, missed_move_analyzer: MissedMoveAnalyzer,
                      ipr_tracker: ImpulsePullbackReaccelerationTracker,
                      lead_lag_strategy: CrossExchangeLeadLagTracker, wf_tag: WalkForwardTag) -> None:
    stage_cfg = cfg.stage
    exchanges = cfg.exchanges
    cb_cfg = cfg.compute_budget_cfg
    strategies_cfg = cfg.strategies_cfg
    common_cfg = {
        "exhaustion_veto": cfg.exhaustion_cfg["veto_threshold"],
        "late_entry_veto": cfg.late_entry_cfg["veto_threshold"],
        "min_net_edge_pct": cfg.execution_cfg["min_expected_net_edge_pct"],
    }
    actionable_threshold = cfg.meta_engine_cfg["actionable_score_threshold"]
    taker_fee_bps_by_exchange = cfg.execution_cfg["taker_fee_bps_by_exchange"]
    risk_cfg = cfg.execution_cfg["risk"]
    compute_budget = ComputeBudget()
    baseline_cross_tracker = LeadLagTracker()   # feeds the 4 direction-scoring strategies' cross_result only

    cycle_count = 0
    degraded_mode = False   # set from the *previous* cycle's duration - see bottom of loop

    while True:
        t0 = time.time()
        now = time.time()
        market_events_this_cycle = 0

        canonical_symbols = runtime.tracked_symbols
        fast_scores = {sym: _fast_score(store, exchanges, sym, now) for sym in canonical_symbols}
        ranked = sorted(fast_scores.items(), key=lambda kv: abs(kv[1]), reverse=True)

        regime_ctx = regime.compute(store, "binance", now, canonical_symbols)
        regime_label = regime_ctx.regime_label

        above_floor = [(s, sc) for s, sc in ranked if abs(sc) >= stage_cfg["min_abs_fast_score"]]
        max_full_pass = cb_cfg["max_symbols_full_pass"]
        if degraded_mode:
            # section 18: shed load on the weakest candidates first, same
            # pattern as the baseline bot - never on the strongest movers
            max_full_pass = max(1, int(max_full_pass * cb_cfg["degraded_promote_fraction"]))
        active_ipr = ipr_tracker.active_symbols()   # never shed - an orphaned episode is worse than a slow cycle
        full_pass_set = list(dict.fromkeys([s for s, _ in above_floor[:max_full_pass]] + active_ipr))

        for symbol in full_pass_set:
            states_by_exchange = {}
            for ex in exchanges:
                st = store.get(ex, symbol)
                if st is not None and not st.is_stale(now):
                    states_by_exchange[ex] = st
            if not states_by_exchange:
                continue

            event: MarketEvent = build_market_event(symbol, states_by_exchange, now)
            market_events_this_cycle += 1

            primary_ex, primary_state = _pick_primary(states_by_exchange)
            if primary_ex is None:
                continue

            engine_scores = _weighted_engine_scores(primary_state, now)
            cross_result = cross_exchange_compute(symbol, states_by_exchange, now, baseline_cross_tracker)
            exh = exhaustion.compute(primary_state, now)
            late = late_entry.compute(primary_state, now)
            exh_tuple = (exh.up_risk, exh.down_risk)
            late_tuple = (late.up_risk, late.down_risk)
            # market-event-level readings for the live signal feed (dashboard section 17) -
            # the same for every strategy on this symbol/cycle, reused from engine_scores
            # already computed above rather than calling the engines again.
            velocity_10s = primary_state.velocity_pct(now, 10)
            acceleration_10s = engine_scores["acceleration"].details.get("acceleration")

            fee_primary = taker_fee_bps_by_exchange.get(primary_ex, 10.0)

            candidates: list[StrategySignal] = []
            for sig in (
                baseline_momentum_starting.compute(event, primary_ex, primary_state, engine_scores, cross_result,
                                                     exh_tuple, late_tuple, strategies_cfg["baseline_momentum_starting"],
                                                     common_cfg, fee_primary, now),
                persistent_micro_trend.compute(event, primary_ex, primary_state, engine_scores, cross_result,
                                                 exh_tuple, late_tuple, strategies_cfg["persistent_micro_trend"],
                                                 common_cfg, fee_primary, now),
                ipr_tracker.compute(event, primary_ex, primary_state, engine_scores, cross_result, exh_tuple,
                                     late_tuple, common_cfg, fee_primary, now),
                breakout_retest_continuation.compute(event, primary_ex, primary_state, engine_scores, cross_result,
                                                       exh_tuple, late_tuple, strategies_cfg["breakout_retest_continuation"],
                                                       common_cfg, fee_primary, now),
                lead_lag_strategy.compute(event, common_cfg, taker_fee_bps_by_exchange, now),
            ):
                if sig is not None:
                    candidates.append(sig)
            if not candidates:
                continue

            meta = compute_meta(candidates, actionable_threshold)
            for sig in candidates:
                mr = meta.get(sig.direction)
                agreement_count = mr.agreement_count if mr else (1 if sig.score >= actionable_threshold else 0)
                conflict_count = mr.conflict_count if mr else 0
                meta_strength = mr.meta_signal_strength if mr else sig.score
                sig_state = states_by_exchange.get(sig.exchange, primary_state)

                signal_id = await runtime.ledger.insert_signal(
                    market_event_id=event.market_event_id, strategy=sig.strategy, symbol=sig.symbol,
                    exchange=sig.exchange, direction=sig.direction, price=sig.price, score=sig.score,
                    phase=sig.phase, velocity_10s=velocity_10s, acceleration_10s=acceleration_10s,
                    persistence_score=(sig.score if sig.strategy == "PERSISTENT_MICRO_TREND" else None),
                    spread_bps=sig_state.spread_bps_now(), exhaustion_risk=sig.exhaustion_risk,
                    late_entry_risk=sig.late_entry_risk, regime_label=regime_label, meta_signal_strength=meta_strength,
                    agreement_count=agreement_count, conflict_count=conflict_count,
                    expected_move_pct=sig.expected_move_pct, expected_cost_pct=sig.expected_cost_pct,
                    expected_net_edge_pct=sig.expected_net_edge_pct, accepted=sig.accepted,
                    reject_reason=sig.reject_reason, dataset_phase=wf_tag.phase, dataset_version=wf_tag.version,
                    details=sig.details,
                )

                if sig.reject_reason in (None, "net_edge_not_positive"):
                    fast_entry_lab.register(signal_id, sig.strategy, sig.symbol, sig.exchange, sig.direction,
                                             now, sig.price)

                if (sig.accepted and not exit_lab.is_at_capacity
                        and not exit_lab.has_open_trade(sig.strategy, sig.symbol, sig.direction)):
                    await _open_shadow_trade(cfg, runtime, execution, exit_lab, signal_id, sig, states_by_exchange,
                                              agreement_count, regime_label, wf_tag, risk_cfg, now)

        await missed_move_analyzer.tick(store, runtime.tracked_symbols_by_exchange, now)
        await lead_lag_strategy.tick_pending_observations(store, runtime.ledger, now)
        await fast_entry_lab.tick(store, runtime.ledger, now)
        await exit_lab.tick(store, now)
        runtime.open_trade_count = exit_lab.open_count

        if ranked:
            hottest_symbol, hottest_score = ranked[0]
            for ex in EXCHANGE_PRIORITY:
                hstate = store.get(ex, hottest_symbol)
                if hstate is not None:
                    runtime.live_market_snapshot = {
                        "symbol": hottest_symbol, "exchange": ex,
                        "direction": "UP" if hottest_score > 0 else "DOWN",
                        "velocity_10s": hstate.velocity_pct(now, 10), "price": hstate.price_now(),
                        "regime_label": regime_label,
                    }
                    break
        runtime.strategy_agreement_snapshot = {"actionable_threshold": actionable_threshold}

        cycle_count += 1
        if cycle_count % CROSS_EXCHANGE_STATS_REFRESH_EVERY_N_CYCLES == 0:
            await lead_lag_strategy.refresh_stats_cache(runtime.ledger, exchanges)

        elapsed = time.time() - t0
        duration_ms = elapsed * 1000.0
        cpu_pct, rss_mb = compute_budget.sample()
        event_loop_lag_ms = max(0.0, (elapsed - stage_cfg["cycle_interval_s"]) * 1000.0)
        degraded_mode = elapsed > (stage_cfg["cycle_interval_s"] * cb_cfg["degrade_threshold_ratio"])
        runtime.last_compute_budget = {
            "cpu_percent": cpu_pct, "rss_mb": rss_mb, "event_loop_lag_ms": event_loop_lag_ms,
            "degraded_mode": degraded_mode, "cycle_duration_ms": duration_ms,
        }
        runtime.market_events_total += market_events_this_cycle
        await runtime.ledger.record_engine_run(len(canonical_symbols), len(full_pass_set), duration_ms, cpu_pct,
                                                rss_mb, event_loop_lag_ms, degraded_mode, market_events_this_cycle)

        await asyncio.sleep(max(0.1, stage_cfg["cycle_interval_s"] - elapsed))


async def main() -> None:
    cfg = load_config()
    assert cfg.shadow_mode is True and cfg.real_orders == 0, "SAFETY: refusing to start outside shadow mode"

    wf_tag = WalkForwardTag.from_config(cfg.walk_forward_cfg)

    ledger = LabLedger(cfg.db_path, cfg.schema_path)
    await ledger.init()
    await ledger.log_config_version(wf_tag.version, wf_tag.phase)

    health = HealthRegistry()
    adapters = {ex: build_adapter(ex, cfg, health) for ex in cfg.exchanges}
    universes = {
        ex: Universe(adapters[ex], cfg.universe["min_quote_volume_24h"])
        for ex in cfg.exchanges
    }

    async def _discover_one(ex: str, universe: Universe) -> tuple[str, list[str]]:
        try:
            symbols = await universe.refresh()
        except Exception:
            logger.exception("%s: lab initial universe discovery failed, starting with 0 symbols (will retry)", ex)
            return ex, []
        for sym in symbols:
            f = universe.get_filter(sym)
            await ledger.upsert_symbol(sym, ex, f.base_asset, f.quote_asset, f.tick_size, f.step_size,
                                        f.min_notional, f.quote_volume_24h)
        return ex, symbols

    discovery_results = await asyncio.gather(*(_discover_one(ex, u) for ex, u in universes.items()))

    tracked_by_exchange: dict[str, list[str]] = {}
    universe_size_by_exchange: dict[str, int] = {}
    for ex, symbols in discovery_results:
        universe_size_by_exchange[ex] = len(symbols)
        tracked_by_exchange[ex] = symbols

    store = StateStore()
    runtime = LabRuntime(
        shadow_mode=cfg.shadow_mode, real_orders=cfg.real_orders, exchanges=cfg.exchanges,
        start_time=time.time(), ledger=ledger, universe_by_exchange=universes, health=health,
        dataset_phase=wf_tag.phase, dataset_version=wf_tag.version,
        tracked_symbols_by_exchange=tracked_by_exchange, universe_size_by_exchange=universe_size_by_exchange,
    )

    execution = ShadowExecutionEngine(cfg.execution_cfg)
    exit_lab = ExitLab(cfg.exit_lab_cfg, {
        "exhaustion_veto": cfg.exhaustion_cfg["veto_threshold"], "late_entry_veto": cfg.late_entry_cfg["veto_threshold"],
    }, execution, ledger)
    fast_entry_lab = FastEntryLab(cfg.fast_entry_lab_cfg, execution)
    missed_move_analyzer = MissedMoveAnalyzer(ledger)
    ipr_tracker = ImpulsePullbackReaccelerationTracker(cfg.strategies_cfg["impulse_pullback_reacceleration"])
    lead_lag_strategy = CrossExchangeLeadLagTracker(cfg.strategies_cfg["cross_exchange_lead_lag"])

    stream_tasks = []
    for ex, adapter in adapters.items():
        on_trade, on_book_ticker, on_depth = await _dispatch_handlers(store, ex)
        stream_tasks.append(_stream_exchange_supervisor(ex, adapter, runtime, on_trade, on_book_ticker, on_depth))

    dash_cfg = cfg.dashboard_cfg
    fastapi_app = create_app(runtime)
    server = uvicorn.Server(uvicorn.Config(fastapi_app, host=dash_cfg["host"], port=dash_cfg["port"],
                                            log_level="warning"))

    logger.info("MOMENTUM STRATEGY LAB V2 starting: SHADOW_MODE=%s REAL_ORDERS=%d exchanges=%s universe=%s "
                "dataset_phase=%s dataset_version=%s", cfg.shadow_mode, cfg.real_orders, cfg.exchanges,
                universe_size_by_exchange, wf_tag.phase, wf_tag.version)

    await asyncio.gather(
        *stream_tasks,
        universe_refresh_loop(cfg, runtime),
        stage_loop(cfg, store, runtime, execution, exit_lab, fast_entry_lab, missed_move_analyzer, ipr_tracker,
                   lead_lag_strategy, wf_tag),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())

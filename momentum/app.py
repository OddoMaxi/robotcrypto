"""Entrypoint: wires Stage A discovery -> live multi-exchange data -> Stage B
engines (incl. cross-exchange confirmation) -> master ranker -> entry/risk ->
exchange-quality best-execution pick -> shadow broker -> ledger -> digital
twin / early-mover tracking, plus the dashboard server. asyncio-native, single
process.

SAFETY: SHADOW_MODE/REAL_ORDERS are validated in momentum.config.load_config
and asserted again here. Nothing downstream of this file can place a real
order (see momentum/safety and tests/test_isolation.py).
"""
from __future__ import annotations

import asyncio
import logging
import time

import uvicorn

from momentum.compute_budget import ComputeBudget
from momentum.config import Config, load_config
from momentum.dashboard.api import create_app
from momentum.data.state import StateStore, SymbolState
from momentum.engines import (
    acceleration, breakout, cross_exchange, exchange_quality,
    exhaustion as exhaustion_engine, fast_movers as fast_movers_engine, late_entry,
    multi_timeframe, orderbook_imbalance, orderflow,
    regime as regime_engine, stablecoin, starting as starting_engine,
    velocity, volatility_expansion, volume as volume_engine,
)
from momentum.engines.cross_exchange import LeadLagTracker
from momentum.entry import entry_engine
from momentum.exchanges.base import ExchangeAdapter
from momentum.exchanges.binance import BinanceAdapter
from momentum.exchanges.bybit import BybitAdapter
from momentum.exchanges.health import HealthRegistry
from momentum.exchanges.okx import OkxAdapter
from momentum.exchanges.universe import Universe
from momentum.exits import exit_engine
from momentum.ranker import master_ranker
from momentum.risk import risk_engine
from momentum.runtime import AppRuntime
from momentum.shadow.broker import ShadowBroker
from momentum.shadow.digital_twin import DigitalTwin
from momentum.shadow.early_mover import EarlyMoverTracker
from momentum.shadow.ledger import Ledger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("momentum.app")

REGIME_ANCHOR_SYMBOLS = ("BTCUSDT", "ETHUSDT")
EXCHANGE_PRIORITY = ("binance", "bybit", "okx")  # deterministic pick when multiple exchanges have data

# V1.1 mission 2: Tier 1 broad radar horizons - down to 1s so a symbol that's
# only just started moving gets flagged well before a 30s/1m window would show it.
RADAR_HORIZONS_S = (1, 3, 5, 10, 15, 30, 60, 180, 300)
RADAR_HORIZON_WEIGHTS = (1.5, 1.4, 1.3, 1.2, 1.1, 1.0, 0.8, 0.6, 0.5)

# The watchlist tier can be a much larger population than the momentum universe
# (hundreds of long-tail pairs) and is monitoring-only - never traded - so it's
# scanned far less often and with a single cheap horizon rather than the full
# weighted radar every 2s.
WATCHLIST_SCAN_EVERY_N_CYCLES = 10
WATCHLIST_VELOCITY_HORIZON_S = 10

WEIGHTED_ENGINES = {
    "velocity": velocity, "acceleration": acceleration, "volume": volume_engine,
    "orderflow": orderflow, "orderbook_imbalance": orderbook_imbalance, "breakout": breakout,
    "multi_timeframe": multi_timeframe, "volatility_expansion": volatility_expansion,
}

ADAPTER_CLASSES = {"binance": BinanceAdapter, "bybit": BybitAdapter, "okx": OkxAdapter}


class RuntimeTradeState:
    __slots__ = ("open_trade", "remaining_size", "realized_pnl", "realized_fees", "prev_confidence")

    def __init__(self, open_trade: exit_engine.OpenTrade, remaining_size: float):
        self.open_trade = open_trade
        self.remaining_size = remaining_size
        self.realized_pnl = 0.0
        self.realized_fees = 0.0
        self.prev_confidence: float | None = None


def build_adapter(exchange: str, cfg: Config, health: HealthRegistry) -> ExchangeAdapter:
    cls = ADAPTER_CLASSES[exchange]
    ws_map = cfg.universe["ws_symbols_per_connection"]
    per_conn = ws_map.get(exchange, 40) if isinstance(ws_map, dict) else ws_map
    return cls(quote_asset=cfg.universe["quote_asset"], ws_symbols_per_connection=per_conn,
               health=health.get_or_create(exchange))


def _reference_exchange(exchanges: list[str]) -> str:
    for ex in EXCHANGE_PRIORITY:
        if ex in exchanges:
            return ex
    return exchanges[0]


def _fast_score_one_exchange(store: StateStore, exchange: str, symbol: str, now: float, stage_a_cfg: dict) -> float:
    """TIER 1 BROAD RADAR: cheap, multi-horizon (1s..5m), run against every
    liquid symbol every cycle. Shorter horizons are weighted more heavily so a
    symbol that's *just* starting to move gets flagged immediately, not only
    once a slower 30s/1m window would show it too."""
    state = store.get(exchange, symbol)
    if state is None:
        return 0.0
    velocities = [state.velocity_pct(now, h) for h in RADAR_HORIZONS_S]
    pairs = [(v, w) for v, w in zip(velocities, RADAR_HORIZON_WEIGHTS) if v is not None]
    if not pairs:
        return 0.0
    weighted = sum(v * w for v, w in pairs) / sum(w for _, w in pairs)

    baseline = state.total_volume(now, 300) / 300
    recent = state.total_volume(now, stage_a_cfg["volume_horizon_s"]) / stage_a_cfg["volume_horizon_s"]
    vol_mult = min(3.0, max(1.0, (recent / baseline) if baseline > 0 else 1.0))
    return weighted * vol_mult


def _apply_load_shedding(promoted_symbols: set[str], fast_scores: dict[str, float], degraded_mode: bool,
                          degraded_promote_fraction: float) -> set[str]:
    """Mission 13: if the previous cycle ran long, only the strongest candidates
    (by |fast score|) keep the full Stage B suite this cycle - never the other
    way around. Anchors (BTC/ETH, needed for regime) are always kept regardless."""
    if not degraded_mode or len(promoted_symbols) <= 1:
        return promoted_symbols
    anchors = set(REGIME_ANCHOR_SYMBOLS) & promoted_symbols
    rest = sorted(promoted_symbols - anchors, key=lambda s: abs(fast_scores.get(s, 0.0)), reverse=True)
    keep_n = max(1, int(len(rest) * degraded_promote_fraction))
    return anchors | set(rest[:keep_n])


def _passes_liquidity_gate(state: SymbolState, now: float, universe_cfg: dict) -> bool:
    """Mission 1: eliminate illiquid/wide-spread pairs before deep (Stage B)
    analysis, using live book data (the REST 24h-volume filter alone can't see
    live spread/depth, and volume can look fine while the book is thin)."""
    spread_bps = state.spread_bps_now()
    if spread_bps is None or spread_bps > universe_cfg["max_spread_bps_for_deep_analysis"]:
        return False
    price = state.price_now()
    depth = state.latest_depth
    if price is None or depth is None:
        return False
    depth_notional = (depth.bid_depth + depth.ask_depth) * price
    return depth_notional >= universe_cfg["min_depth_notional_usd"]


def _combined_fast_score(store: StateStore, exchanges: list[str], symbol: str, now: float, stage_a_cfg: dict) -> float:
    scores = [_fast_score_one_exchange(store, ex, symbol, now, stage_a_cfg) for ex in exchanges]
    return max(scores, key=abs, default=0.0)


def _states_by_exchange(store: StateStore, exchanges: list[str], symbol: str, now: float,
                         max_age_s: float) -> dict[str, SymbolState]:
    result = {}
    for ex in exchanges:
        st = store.get(ex, symbol)
        if st is not None and not st.is_stale(now, max_age_s):
            result[ex] = st
    return result


def _pick_primary(states_by_exchange: dict[str, SymbolState]) -> tuple[str, SymbolState] | tuple[None, None]:
    for ex in EXCHANGE_PRIORITY:
        if ex in states_by_exchange:
            return ex, states_by_exchange[ex]
    if states_by_exchange:
        ex = next(iter(states_by_exchange))
        return ex, states_by_exchange[ex]
    return None, None


def _run_stage_b_engines(state: SymbolState, now: float) -> dict:
    return {name: mod.compute(state, now) for name, mod in WEIGHTED_ENGINES.items()}


def _serialize_engine_scores(engine_scores: dict) -> dict:
    """Handles every engine's dataclass shape uniformly (EngineScore's up/down,
    Exhaustion/LateEntry's up_risk/down_risk, FastMoverScore's direction/fast_score/
    returns) so new engines don't need special-casing here or at every call site."""
    out = {}
    for k, v in engine_scores.items():
        d = dict(getattr(v, "details", {}) or {})
        for attr in ("up", "down", "up_risk", "down_risk", "fast_score", "direction", "returns"):
            if hasattr(v, attr):
                d[attr] = getattr(v, attr)
        out[k] = d
    return out


def _candidate_view(symbol: str, primary_exchange: str, price: float, rank: dict, exhaustion,
                     entry_up, entry_down, quality_up, quality_down,
                     prev_fast: float, cur_fast: float, state: SymbolState,
                     states_by_exchange: dict[str, SymbolState], cross_details: dict,
                     starting: starting_engine.StartingScore, late: late_entry.LateEntryScore,
                     fast_mover, regime_label: str, tier: str) -> dict:
    dominant = "up" if rank["UP"].confidence >= rank["DOWN"].confidence else "down"
    entry = entry_up if dominant == "up" else entry_down
    now = time.time()
    baseline = state.total_volume(now, 300) / 300
    recent = state.total_volume(now, 10) / 10
    exchanges_status = {
        ex: {"price": st.price_now(), "velocity_10s": st.velocity_pct(now, 10)}
        for ex, st in states_by_exchange.items()
    }
    return {
        "symbol": symbol,
        "primary_exchange": primary_exchange,
        "price": price,
        "tier": tier,
        "regime": regime_label,
        "up": {
            "confidence": rank["UP"].confidence, "classification": rank["UP"].classification,
            "exhaustion_risk": exhaustion.up_risk, "late_entry_risk": late.up_risk,
            "starting_score": starting.up,
        },
        "down": {
            "confidence": rank["DOWN"].confidence, "classification": rank["DOWN"].classification,
            "exhaustion_risk": exhaustion.down_risk, "late_entry_risk": late.down_risk,
            "starting_score": starting.down,
        },
        "entry_quality": entry.entry_quality,
        "entry_type": entry.entry_type,
        "score_evolution": cur_fast - prev_fast,
        "velocity_10s": state.velocity_pct(now, 10),
        "velocity_30s": state.velocity_pct(now, 30),
        "velocity_60s": state.velocity_pct(now, 60),
        "velocity_300s": state.velocity_pct(now, 300),
        "volume_ratio": (recent / baseline) if baseline > 0 else 0.0,
        "exchanges": exchanges_status,
        "leading_exchange": cross_details.get("leading_exchange"),
        "cross_exchange_classification": cross_details.get("classification"),
        "best_execution_up": quality_up.best_exchange if quality_up else None,
        "best_execution_down": quality_down.best_exchange if quality_down else None,
        "fast_score": fast_mover.fast_score if fast_mover else None,
        "fast_direction": fast_mover.direction if fast_mover else None,
        "fast_returns": fast_mover.returns if fast_mover else None,
    }


async def _dispatch_handlers(store: StateStore, exchange: str):
    async def on_trade(trade):
        store.get_or_create(exchange, trade.symbol).on_trade(trade)

    async def on_book_ticker(bt):
        store.get_or_create(exchange, bt.symbol).on_book_ticker(bt)

    async def on_depth(depth):
        store.get_or_create(exchange, depth.symbol).on_depth(depth)

    return on_trade, on_book_ticker, on_depth


REJECTED_SIGNAL_LOG_COOLDOWN_S = 30.0  # avoid flooding the ledger with a repeat WATCH/BUILDING row every cycle


async def stage_ab_loop(cfg: Config, store: StateStore, runtime: AppRuntime,
                         broker: ShadowBroker, open_trades: dict[int, RuntimeTradeState]):
    stage_a_cfg = cfg.stage_a
    exchanges = cfg.exchanges
    reference_exchange = _reference_exchange(exchanges)
    stale_max_age_s = cfg.shadow_cfg["stale_data_max_age_s"]
    early_mover_cfg = cfg.early_mover_cfg
    universe_cfg = cfg.universe
    cb_cfg = cfg.compute_budget_cfg
    compute_budget = ComputeBudget()

    prev_fast_scores: dict[str, float] = {}
    last_signal_log: dict[tuple[str, str], tuple[float, str]] = {}
    lead_lag_tracker = LeadLagTracker()
    degraded_mode = False  # set from the *previous* cycle's duration - see bottom of loop
    cycle_count = 0

    while True:
        t0 = time.time()
        now = time.time()

        # TIER 1 - BROAD RADAR: cheap multi-horizon scan over every momentum
        # symbol (stablecoins excluded, see runtime.momentum_symbols).
        canonical_symbols = runtime.momentum_symbols
        fast_scores = {sym: _combined_fast_score(store, exchanges, sym, now, stage_a_cfg) for sym in canonical_symbols}
        ranked_up = sorted(fast_scores.items(), key=lambda kv: kv[1], reverse=True)
        ranked_down = sorted(fast_scores.items(), key=lambda kv: kv[1])

        n = cfg.universe["stage_b_promote_count"]
        min_abs = stage_a_cfg["min_abs_fast_score"]
        # TIER 2 - HOT WATCHLIST: anomalies promoted for deep analysis.
        promoted_symbols = set()
        for sym, score in ranked_up[:n]:
            if abs(score) >= min_abs:
                promoted_symbols.add(sym)
        for sym, score in ranked_down[:n]:
            if abs(score) >= min_abs:
                promoted_symbols.add(sym)
        promoted_symbols |= set(REGIME_ANCHOR_SYMBOLS) & set(canonical_symbols)

        promoted_symbols = _apply_load_shedding(
            promoted_symbols, fast_scores, degraded_mode, cb_cfg["degraded_promote_fraction"],
        )

        regime = regime_engine.compute(store, reference_exchange, now,
                                        runtime.momentum_symbols_by_exchange.get(reference_exchange, []))

        new_promoted = {}
        for sym in promoted_symbols:
            states_by_exchange = _states_by_exchange(store, exchanges, sym, now, stale_max_age_s)
            primary_ex, primary_state = _pick_primary(states_by_exchange)
            if primary_state is None:
                continue
            # mission 1: illiquid/wide-spread pairs are eliminated right before
            # deep analysis, using live book data - Stage A fast-scoring above is
            # cheap enough to run on them, but they never reach Stage B.
            if not _passes_liquidity_gate(primary_state, now, universe_cfg):
                continue
            price = primary_state.price_now()
            if price is None:
                continue

            # TIER 3 - DEEP MULTI-ENGINE ANALYSIS.
            engine_scores = _run_stage_b_engines(primary_state, now)
            engine_scores["cross_exchange"] = cross_exchange.compute(sym, states_by_exchange, now, lead_lag_tracker)
            exhaustion = exhaustion_engine.compute(primary_state, now)
            rank = master_ranker.rank(engine_scores, exhaustion, regime, cfg.engine_weights, cfg.ranker_cfg)

            starting = starting_engine.compute(primary_state, now, engine_scores, engine_scores["cross_exchange"])
            late = late_entry.compute(primary_state, now)
            engine_scores["starting"] = starting
            engine_scores["late_entry"] = late

            fee_bps = broker.taker_fee_bps(primary_ex)
            reward_risk_mult = cfg.risk_cfg["reward_risk_target_multiple"]
            entry_up = entry_engine.compute("UP", primary_state, now, rank["UP"], engine_scores, cfg.entry_cfg,
                                             fee_bps, reward_risk_mult, late)
            entry_down = entry_engine.compute("DOWN", primary_state, now, rank["DOWN"], engine_scores, cfg.entry_cfg,
                                               fee_bps, reward_risk_mult, late)

            quality_up = exchange_quality.compute("UP", states_by_exchange, now, cfg.shadow_cfg["taker_fee_bps_by_exchange"])
            quality_down = exchange_quality.compute("DOWN", states_by_exchange, now, cfg.shadow_cfg["taker_fee_bps_by_exchange"])

            fast_mover = fast_movers_engine.compute(
                primary_state, now, engine_scores, exhaustion,
                max(late.up_risk, late.down_risk), engine_scores["cross_exchange"],
            )

            cx_details = engine_scores["cross_exchange"].details
            leading = cx_details.get("leading_exchange")
            new_confirmation = cx_details.get("new_confirmation")
            if leading and new_confirmation and new_confirmation != leading:
                lead_time = cx_details.get("lead_time_ms", {}).get(new_confirmation)
                if lead_time is not None:
                    await runtime.ledger.insert_leader_lag_event(
                        symbol=sym, leading_exchange=leading, following_exchange=new_confirmation,
                        lead_time_ms=lead_time,
                    )

            prev_fast = prev_fast_scores.get(sym, 0.0)
            cur_fast = fast_scores.get(sym, 0.0)
            tier = "TRADE_CANDIDATE" if (
                rank["UP"].classification in ("CONFIRMED", "STRONG", "HIGH_CONVICTION")
                or rank["DOWN"].classification in ("CONFIRMED", "STRONG", "HIGH_CONVICTION")
            ) else "HOT_WATCHLIST"
            new_promoted[sym] = _candidate_view(sym, primary_ex, price, rank, exhaustion, entry_up, entry_down,
                                                 quality_up, quality_down, prev_fast, cur_fast, primary_state,
                                                 states_by_exchange, cx_details, starting, late, fast_mover,
                                                 regime.regime_label, tier)

            for direction, rres, entry, quality, starting_score in (
                ("UP", rank["UP"], entry_up, quality_up, starting.up),
                ("DOWN", rank["DOWN"], entry_down, quality_down, starting.down),
            ):
                runtime.early_mover_tracker.update_confidence(sym, direction, rres.confidence, now)
                await runtime.early_mover_tracker.maybe_register(
                    sym, primary_ex, direction, price, rres.confidence, now,
                    early_mover_cfg["confidence_threshold"],
                    starting_score=starting_score,
                    fast_score=fast_mover.fast_score if fast_mover else None,
                    regime_label=regime.regime_label, cx_details=cx_details, state=primary_state,
                )

                if rres.classification == "IGNORE":
                    continue
                shadow_only = direction == "DOWN"  # mission 8: DOWN is always shadow-only

                accept = (
                    rres.classification in ("CONFIRMED", "STRONG", "HIGH_CONVICTION")
                    and entry.reject_reason is None
                )
                reject_reason = entry.reject_reason
                if rres.classification == "EXHAUSTED":
                    accept = False
                    reject_reason = reject_reason or "exhausted"

                already_open = any(
                    ts.open_trade.symbol == sym and ts.open_trade.direction == direction
                    for ts in open_trades.values()
                )
                if already_open:
                    accept = False
                    reject_reason = reject_reason or "already_open"

                # Accepted signals always get logged (they own a shadow trade's foreign
                # key). Rejected ones are de-duped/cooldown-limited so a symbol parked at
                # WATCH/BUILDING for hours doesn't write a row every 2s over a 24h+ run -
                # we still capture every *episode* and every classification change.
                key = (sym, direction)
                last_ts, last_class = last_signal_log.get(key, (0.0, None))
                should_log = accept or (now - last_ts > REJECTED_SIGNAL_LOG_COOLDOWN_S) or (last_class != rres.classification)
                if not should_log:
                    continue

                signal_id = await runtime.ledger.insert_signal(
                    symbol=sym, exchange=primary_ex, direction=direction, price=price,
                    spread_bps=primary_state.spread_bps_now(),
                    engine_scores=_serialize_engine_scores(engine_scores),
                    momentum_confidence=rres.confidence, exhaustion_risk=rres.exhaustion_risk,
                    classification=rres.classification, entry_quality=entry.entry_quality,
                    entry_type=entry.entry_type, accepted=accept, reject_reason=reject_reason,
                    shadow_only=shadow_only,
                )
                last_signal_log[key] = (now, rres.classification)
                runtime.digital_twin.track(signal_id, sym, primary_ex, direction, price)

                if accept:
                    best_ex = quality.best_exchange if quality else primary_ex
                    exec_state = states_by_exchange.get(best_ex, primary_state)
                    await _open_shadow_trade(cfg, runtime, broker, open_trades, signal_id, sym, direction,
                                              exec_state, best_ex, entry, price)

        runtime.promoted = new_promoted
        prev_fast_scores = fast_scores
        runtime.last_stage_a_scanned = len(canonical_symbols)

        # Sub-threshold WATCHLIST tier: Tier-1-only visibility (price + fast
        # score), structurally never promoted to Stage B/trading - these
        # symbols never enter `promoted_symbols` above. This population can be
        # much larger than the momentum universe (hundreds of long-tail pairs),
        # so it's scanned far less often and with a single cheap horizon - it's
        # a monitoring panel, not a trading path, so 20s-stale numbers are fine
        # and keeping it on the 2s momentum cadence was needlessly expensive.
        cycle_count += 1
        if cycle_count % WATCHLIST_SCAN_EVERY_N_CYCLES == 0:
            watchlist_snapshot = {}
            for sym in runtime.watchlist_symbols:
                states_by_exchange = _states_by_exchange(store, exchanges, sym, now, stale_max_age_s)
                primary_ex, primary_state = _pick_primary(states_by_exchange)
                if primary_state is None:
                    continue
                price = primary_state.price_now()
                if price is None:
                    continue
                watchlist_snapshot[sym] = {
                    "symbol": sym, "primary_exchange": primary_ex, "price": price,
                    "fast_score": primary_state.velocity_pct(now, WATCHLIST_VELOCITY_HORIZON_S),
                    "velocity_10s": primary_state.velocity_pct(now, 10),
                    "exchanges": list(states_by_exchange.keys()),
                }
            runtime.watchlist_snapshot = watchlist_snapshot

        elapsed = time.time() - t0
        duration_ms = elapsed * 1000
        cpu_pct, rss_mb = compute_budget.sample()
        event_loop_lag_ms = max(0.0, (elapsed - stage_a_cfg["cycle_interval_s"]) * 1000.0)
        degraded_mode = elapsed > (stage_a_cfg["cycle_interval_s"] * cb_cfg["degrade_threshold_ratio"])
        runtime.last_compute_budget = {
            "cpu_percent": cpu_pct, "rss_mb": rss_mb, "event_loop_lag_ms": event_loop_lag_ms,
            "degraded_mode": degraded_mode, "cycle_duration_ms": duration_ms,
        }
        await runtime.ledger.record_engine_run(len(canonical_symbols), len(new_promoted), duration_ms,
                                                cpu_percent=cpu_pct, rss_mb=rss_mb,
                                                event_loop_lag_ms=event_loop_lag_ms, degraded_mode=degraded_mode)

        await asyncio.sleep(max(0.1, stage_a_cfg["cycle_interval_s"] - elapsed))


async def _open_shadow_trade(cfg, runtime: AppRuntime, broker, open_trades, signal_id, symbol, direction,
                              exec_state: SymbolState, exchange: str, entry, reference_price: float):
    # risk/stop levels are sized off the reference price where the signal was
    # scored; the actual fill happens on `exchange` (mission 9's best-execution
    # pick), which for liquid pairs stays within basis points of the reference.
    plan = risk_engine.compute(direction, reference_price, entry, cfg.risk_cfg)
    scenario = plan.default_scenario
    universe = runtime.universe_by_exchange[exchange]
    sym_filter = universe.get_filter(symbol)
    size = broker.apply_filters(sym_filter, scenario.size, reference_price)
    if not size:
        logger.info("%s %s @ %s: shadow entry skipped, failed exchange filters", symbol, direction, exchange)
        return

    fill = broker.simulate_entry(exec_state, direction, size, exchange)
    if fill is None:
        logger.warning("%s %s @ %s: no book data available for shadow fill", symbol, direction, exchange)
        return

    trade_id = await runtime.ledger.insert_shadow_trade(
        signal_id=signal_id, symbol=symbol, exchange=exchange, direction=direction,
        entry_price=fill.avg_price, invalidation_price=plan.invalidation_price, stop_price=plan.stop_price,
        size=fill.filled_size, risk_pct=scenario.risk_pct, risk_amount=scenario.risk_amount,
        entry_type=entry.entry_type, fees_paid=fill.fee, slippage_pct=fill.slippage_pct,
        latency_ms=fill.latency_ms,
    )
    r_unit = abs(fill.avg_price - plan.invalidation_price)
    open_trade = exit_engine.OpenTrade(
        id=trade_id, symbol=symbol, exchange=exchange, direction=direction, entry_price=fill.avg_price,
        stop_price=plan.stop_price, r_unit=r_unit, trailing_state="INITIAL", mfe_pct=0.0, mae_pct=0.0,
    )
    open_trades[trade_id] = RuntimeTradeState(open_trade, fill.filled_size)
    logger.info("SHADOW ENTRY %s %s @ %s %.6f size=%.6f (%s)", symbol, direction, exchange, fill.avg_price,
                fill.filled_size, entry.entry_type)


async def exit_loop(cfg: Config, store: StateStore, runtime: AppRuntime, broker: ShadowBroker,
                     open_trades: dict[int, RuntimeTradeState]):
    exits_cfg = cfg.exits_cfg
    while True:
        for trade_id in list(open_trades.keys()):
            rts = open_trades.get(trade_id)
            if rts is None:
                continue
            ot = rts.open_trade
            state = store.get(ot.exchange, ot.symbol)
            price = state.price_now() if state else None
            if state is None or price is None:
                continue

            candidate = runtime.promoted.get(ot.symbol)
            current_confidence = candidate[ot.direction.lower()]["confidence"] if candidate else 0.0

            decision = exit_engine.evaluate(ot, price, current_confidence, rts.prev_confidence, exits_cfg)
            rts.prev_confidence = current_confidence

            if decision.partial_tp_triggered and not ot.partial_taken:
                partial_size = rts.remaining_size * exits_cfg["partial_take_profit_fraction"]
                fill = broker.simulate_exit(state, ot.direction, partial_size, ot.exchange)
                if fill:
                    sign = 1 if ot.direction == "UP" else -1
                    pnl = sign * (fill.avg_price - ot.entry_price) * fill.filled_size - fill.fee
                    rts.realized_pnl += pnl
                    rts.realized_fees += fill.fee
                    rts.remaining_size -= fill.filled_size
                    ot.partial_taken = True
                    logger.info("PARTIAL TP %s %s: %.6f @ %.6f pnl=%.4f", ot.symbol, ot.direction,
                                fill.filled_size, fill.avg_price, pnl)

            ot.stop_price = decision.new_stop_price
            ot.trailing_state = decision.new_trailing_state
            ot.mfe_pct = decision.new_mfe_pct
            ot.mae_pct = decision.new_mae_pct
            await runtime.ledger.update_open_trade(
                trade_id, stop_price=ot.stop_price, trailing_state=ot.trailing_state,
                mfe_pct=ot.mfe_pct, mae_pct=ot.mae_pct,
            )

            if decision.exit:
                fill = broker.simulate_exit(state, ot.direction, rts.remaining_size, ot.exchange)
                if fill is None:
                    continue  # retry next tick
                sign = 1 if ot.direction == "UP" else -1
                pnl = sign * (fill.avg_price - ot.entry_price) * fill.filled_size - fill.fee
                total_net_pnl = rts.realized_pnl + pnl
                await runtime.ledger.close_trade(
                    trade_id, exit_price=fill.avg_price, exit_reason=decision.exit_reason,
                    fees_paid=fill.fee, slippage_pct=fill.slippage_pct, net_pnl=total_net_pnl,
                    r_multiple=decision.r_multiple,
                )
                del open_trades[trade_id]
                logger.info("SHADOW EXIT %s %s @ %s %.6f reason=%s pnl=%.4f", ot.symbol, ot.direction, ot.exchange,
                            fill.avg_price, decision.exit_reason, total_net_pnl)

        await asyncio.sleep(1.0)


async def digital_twin_loop(store: StateStore, digital_twin: DigitalTwin, early_mover_tracker: EarlyMoverTracker):
    while True:
        await digital_twin.tick(store)
        await early_mover_tracker.tick(store)
        await asyncio.sleep(1.0)


def _split_stablecoins(symbols: list[str], quote_asset: str) -> tuple[list[str], list[str]]:
    """mission 9: stablecoin pairs are separated out before anything else sees
    them - the momentum universe never includes them."""
    stable = [s for s in symbols if stablecoin.is_stablecoin_pair(s, quote_asset)]
    return symbols, stable


async def universe_refresh_loop(cfg: Config, runtime: AppRuntime):
    while True:
        await asyncio.sleep(cfg.universe["refresh_interval_s"])
        for exchange, universe in runtime.universe_by_exchange.items():
            try:
                symbols = await universe.refresh()
                runtime.universe_size_by_exchange[exchange] = len(symbols)
                for sym in symbols + universe.watchlist_symbols:
                    f = universe.get_filter(sym)
                    if f:
                        await runtime.ledger.upsert_symbol(sym, exchange, f.base_asset, f.quote_asset,
                                                            f.tick_size, f.step_size, f.min_notional,
                                                            f.quote_volume_24h)
                # the watchlist tier can refresh in place each cycle (visibility-only,
                # no live WS subscription risk either way since it's already tracked or not)
                runtime.watchlist_symbols_by_exchange[exchange] = universe.watchlist_symbols
                # only (re)populate tracked symbols if this exchange had none yet - an
                # exchange that's already streaming keeps its existing subscription set
                # until a restart, per the documented V1 limitation (see stage_ab_loop docstring)
                if symbols and not runtime.tracked_symbols_by_exchange.get(exchange):
                    all_symbols, stable_symbols = _split_stablecoins(symbols, cfg.universe["quote_asset"])
                    runtime.tracked_symbols_by_exchange[exchange] = list(
                        dict.fromkeys(all_symbols + universe.watchlist_symbols + list(REGIME_ANCHOR_SYMBOLS))
                    )
                    runtime.stablecoin_symbols_by_exchange[exchange] = stable_symbols
                    logger.info("%s: universe recovered, %d symbols now tracked (%d stablecoin, %d watchlist)",
                                exchange, len(symbols), len(stable_symbols), len(universe.watchlist_symbols))
            except Exception:
                logger.exception("universe refresh failed for %s", exchange)


async def _stream_exchange_supervisor(exchange: str, adapter: ExchangeAdapter, runtime: AppRuntime,
                                       on_trade, on_book_ticker, on_depth) -> None:
    """Wraps stream_market_data so an exchange with 0 symbols at startup (failed
    initial discovery) keeps retrying once universe_refresh_loop repopulates it,
    instead of silently never connecting for the rest of the process lifetime."""
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
            logger.exception("%s: stream_market_data supervisor caught an unexpected crash, restarting in 10s", exchange)
        await asyncio.sleep(10)


STABLECOIN_MONITOR_INTERVAL_S = 5.0
STABLECOIN_EVENT_COOLDOWN_S = 60.0


async def stablecoin_monitor_loop(store: StateStore, runtime: AppRuntime) -> None:
    """mission 9: a completely separate, lightweight monitor - never feeds the
    momentum ranker, never mixes stablecoin PnL/signals into the normal
    dataset. Runs independently of Stage A/B so a busy momentum cycle never
    delays a depeg detection."""
    last_logged: dict[tuple[str, str, str], float] = {}
    while True:
        now = time.time()
        snapshot = {}
        for exchange, symbols in runtime.stablecoin_symbols_by_exchange.items():
            for sym in symbols:
                state = store.get(exchange, sym)
                if state is None or state.is_stale(now):
                    continue
                check = stablecoin.compute(state, now)
                if check is None:
                    continue
                snapshot[f"{exchange}:{sym}"] = {
                    "symbol": sym, "exchange": exchange, "price": check.price,
                    "deviation_pct": check.deviation_pct, "anomalies": check.anomalies,
                }
                for anomaly in check.anomalies:
                    key = (sym, exchange, anomaly["type"])
                    if now - last_logged.get(key, 0.0) < STABLECOIN_EVENT_COOLDOWN_S:
                        continue
                    last_logged[key] = now
                    await runtime.ledger.insert_stablecoin_event(
                        symbol=sym, exchange=exchange, price=check.price, deviation_pct=check.deviation_pct,
                        anomaly_type=anomaly["type"], severity=anomaly["severity"],
                    )
                    logger.warning("STABLECOIN ANOMALY %s @ %s: %s (severity=%.0f)",
                                    sym, exchange, anomaly["type"], anomaly["severity"])
        runtime.stablecoin_snapshot = snapshot
        await asyncio.sleep(STABLECOIN_MONITOR_INTERVAL_S)


async def restore_open_trades(runtime: AppRuntime, open_trades: dict[int, RuntimeTradeState]) -> None:
    rows = await runtime.ledger.get_open_trades()
    for row in rows:
        r_unit = abs(row["entry_price"] - row["invalidation_price"])
        ot = exit_engine.OpenTrade(
            id=row["id"], symbol=row["symbol"], exchange=row["exchange"], direction=row["direction"],
            entry_price=row["entry_price"], stop_price=row["stop_price"], r_unit=r_unit,
            trailing_state=row["trailing_state"] or "INITIAL",
            mfe_pct=row["mfe_pct"] or 0.0, mae_pct=row["mae_pct"] or 0.0,
            partial_taken=(row["trailing_state"] == "PARTIAL_TAKEN"),
        )
        open_trades[row["id"]] = RuntimeTradeState(ot, row["size"])
    if rows:
        logger.info("restored %d open shadow trade(s) from ledger", len(rows))


async def main():
    cfg = load_config()
    assert cfg.shadow_mode is True and cfg.real_orders == 0, "SAFETY: refusing to start outside shadow mode"

    ledger = Ledger(cfg.db_path, cfg.schema_path)
    await ledger.init()

    health = HealthRegistry()
    adapters = {ex: build_adapter(ex, cfg, health) for ex in cfg.exchanges}
    universes = {
        ex: Universe(adapters[ex], cfg.universe["min_quote_volume_24h"],
                     cfg.universe.get("watchlist_min_quote_volume_24h"))
        for ex in cfg.exchanges
    }

    async def _discover_one(ex: str, universe: Universe) -> tuple[str, list[str], list[str]]:
        # mission 14: one exchange's data being unreachable at startup (geo-block,
        # transient outage, ...) must never take the other exchanges - or the whole
        # engine - down with it. It just starts with 0 symbols there; the periodic
        # universe_refresh_loop keeps retrying. Run concurrently so one slow/blocked
        # exchange doesn't also delay the others' startup.
        try:
            symbols = await universe.refresh()
        except Exception:
            logger.exception("%s: initial universe discovery failed, starting with 0 symbols (will retry)", ex)
            return ex, [], []
        for sym in symbols + universe.watchlist_symbols:
            f = universe.get_filter(sym)
            await ledger.upsert_symbol(sym, ex, f.base_asset, f.quote_asset, f.tick_size, f.step_size,
                                        f.min_notional, f.quote_volume_24h)
        return ex, symbols, universe.watchlist_symbols

    discovery_results = await asyncio.gather(*(_discover_one(ex, u) for ex, u in universes.items()))

    tracked_by_exchange: dict[str, list[str]] = {}
    stablecoin_by_exchange: dict[str, list[str]] = {}
    watchlist_by_exchange: dict[str, list[str]] = {}
    universe_size_by_exchange: dict[str, int] = {}
    for ex, symbols, watchlist_symbols in discovery_results:
        universe_size_by_exchange[ex] = len(symbols)
        if symbols or watchlist_symbols:
            all_symbols, stable_symbols = _split_stablecoins(symbols, cfg.universe["quote_asset"])
            tracked_by_exchange[ex] = list(
                dict.fromkeys(all_symbols + watchlist_symbols + list(REGIME_ANCHOR_SYMBOLS))
            )
            stablecoin_by_exchange[ex] = stable_symbols
            watchlist_by_exchange[ex] = watchlist_symbols
        else:
            tracked_by_exchange[ex] = []
            stablecoin_by_exchange[ex] = []
            watchlist_by_exchange[ex] = []

    store = StateStore()
    digital_twin = DigitalTwin(cfg.twin_horizons_s, ledger)
    early_mover_tracker = EarlyMoverTracker(cfg.twin_horizons_s, ledger, cfg.early_mover_cfg["cooldown_s"])
    runtime = AppRuntime(
        shadow_mode=cfg.shadow_mode, real_orders=cfg.real_orders, exchanges=cfg.exchanges,
        start_time=time.time(), ledger=ledger, digital_twin=digital_twin, early_mover_tracker=early_mover_tracker,
        universe_by_exchange=universes, health=health,
        tracked_symbols_by_exchange=tracked_by_exchange, stablecoin_symbols_by_exchange=stablecoin_by_exchange,
        watchlist_symbols_by_exchange=watchlist_by_exchange,
        universe_size_by_exchange=universe_size_by_exchange,
    )

    broker = ShadowBroker(cfg.shadow_cfg)
    open_trades: dict[int, RuntimeTradeState] = {}
    await restore_open_trades(runtime, open_trades)

    stream_tasks = []
    for ex, adapter in adapters.items():
        on_trade, on_book_ticker, on_depth = await _dispatch_handlers(store, ex)
        stream_tasks.append(_stream_exchange_supervisor(ex, adapter, runtime, on_trade, on_book_ticker, on_depth))

    dash_cfg = cfg.dashboard_cfg
    fastapi_app = create_app(runtime)
    server = uvicorn.Server(uvicorn.Config(fastapi_app, host=dash_cfg["host"], port=dash_cfg["port"],
                                            log_level="warning"))

    logger.info("MOMENTUM ENGINE starting: SHADOW_MODE=%s REAL_ORDERS=%d exchanges=%s universe=%s",
                cfg.shadow_mode, cfg.real_orders, cfg.exchanges, universe_size_by_exchange)

    await asyncio.gather(
        *stream_tasks,
        stage_ab_loop(cfg, store, runtime, broker, open_trades),
        exit_loop(cfg, store, runtime, broker, open_trades),
        digital_twin_loop(store, digital_twin, early_mover_tracker),
        universe_refresh_loop(cfg, runtime),
        stablecoin_monitor_loop(store, runtime),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())

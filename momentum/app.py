"""Entrypoint: wires Stage A discovery -> live data -> Stage B engines -> master
ranker -> entry/risk -> shadow broker -> ledger -> digital twin, plus the
dashboard server. asyncio-native, single process.

SAFETY: SHADOW_MODE/REAL_ORDERS are validated in momentum.config.load_config
and asserted again here. Nothing downstream of this file can place a real
order (see momentum/safety and tests/test_isolation.py).
"""
from __future__ import annotations

import asyncio
import logging
import time

import uvicorn

from momentum.config import Config, load_config
from momentum.dashboard.api import create_app
from momentum.data.state import StateStore
from momentum.engines import (
    acceleration, breakout, exhaustion as exhaustion_engine, multi_timeframe,
    orderbook_imbalance, orderflow, regime as regime_engine, velocity,
    volatility_expansion, volume as volume_engine,
)
from momentum.entry import entry_engine
from momentum.exchanges.binance import BinanceAdapter
from momentum.exchanges.universe import Universe
from momentum.exits import exit_engine
from momentum.ranker import master_ranker
from momentum.risk import risk_engine
from momentum.runtime import AppRuntime
from momentum.shadow.broker import ShadowBroker
from momentum.shadow.digital_twin import DigitalTwin
from momentum.shadow.ledger import Ledger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("momentum.app")

REGIME_ANCHOR_SYMBOLS = ("BTCUSDT", "ETHUSDT")

WEIGHTED_ENGINES = {
    "velocity": velocity, "acceleration": acceleration, "volume": volume_engine,
    "orderflow": orderflow, "orderbook_imbalance": orderbook_imbalance, "breakout": breakout,
    "multi_timeframe": multi_timeframe, "volatility_expansion": volatility_expansion,
}


class RuntimeTradeState:
    __slots__ = ("open_trade", "remaining_size", "realized_pnl", "realized_fees", "prev_confidence")

    def __init__(self, open_trade: exit_engine.OpenTrade, remaining_size: float):
        self.open_trade = open_trade
        self.remaining_size = remaining_size
        self.realized_pnl = 0.0
        self.realized_fees = 0.0
        self.prev_confidence: float | None = None


def _fast_score(store: StateStore, exchange: str, symbol: str, now: float, stage_a_cfg: dict) -> float:
    state = store.get(exchange, symbol)
    if state is None:
        return 0.0
    v = state.velocity_pct(now, stage_a_cfg["velocity_horizon_s"])
    if v is None:
        return 0.0
    baseline = state.total_volume(now, 300) / 300
    recent = state.total_volume(now, stage_a_cfg["volume_horizon_s"]) / stage_a_cfg["volume_horizon_s"]
    vol_mult = min(3.0, max(1.0, (recent / baseline) if baseline > 0 else 1.0))
    return v * vol_mult


def _run_stage_b_engines(state, now: float) -> dict:
    return {name: mod.compute(state, now) for name, mod in WEIGHTED_ENGINES.items()}


def _candidate_view(symbol: str, price: float, engine_scores: dict, exhaustion, rank: dict,
                     entry_up, entry_down, prev_fast: float, cur_fast: float, state) -> dict:
    dominant = "up" if rank["UP"].confidence >= rank["DOWN"].confidence else "down"
    entry = entry_up if dominant == "up" else entry_down
    now = time.time()
    baseline = state.total_volume(now, 300) / 300
    recent = state.total_volume(now, 10) / 10
    return {
        "symbol": symbol,
        "price": price,
        "up": {
            "confidence": rank["UP"].confidence, "classification": rank["UP"].classification,
            "exhaustion_risk": exhaustion.up_risk,
        },
        "down": {
            "confidence": rank["DOWN"].confidence, "classification": rank["DOWN"].classification,
            "exhaustion_risk": exhaustion.down_risk,
        },
        "entry_quality": entry.entry_quality,
        "entry_type": entry.entry_type,
        "score_evolution": cur_fast - prev_fast,
        "velocity_10s": state.velocity_pct(now, 10),
        "velocity_30s": state.velocity_pct(now, 30),
        "velocity_60s": state.velocity_pct(now, 60),
        "velocity_300s": state.velocity_pct(now, 300),
        "volume_ratio": (recent / baseline) if baseline > 0 else 0.0,
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


async def stage_ab_loop(cfg: Config, store: StateStore, universe: Universe, runtime: AppRuntime,
                         broker: ShadowBroker, open_trades: dict[int, RuntimeTradeState]):
    stage_a_cfg = cfg.stage_a
    prev_fast_scores: dict[str, float] = {}
    last_signal_log: dict[tuple[str, str], tuple[float, str]] = {}

    while True:
        t0 = time.time()
        now = time.time()

        fast_scores = {sym: _fast_score(store, "binance", sym, now, stage_a_cfg) for sym in runtime.tracked_symbols}
        ranked_up = sorted(fast_scores.items(), key=lambda kv: kv[1], reverse=True)
        ranked_down = sorted(fast_scores.items(), key=lambda kv: kv[1])

        n = cfg.universe["stage_b_promote_count"]
        min_abs = stage_a_cfg["min_abs_fast_score"]
        promoted_symbols = set()
        for sym, score in ranked_up[:n]:
            if abs(score) >= min_abs:
                promoted_symbols.add(sym)
        for sym, score in ranked_down[:n]:
            if abs(score) >= min_abs:
                promoted_symbols.add(sym)
        promoted_symbols |= set(REGIME_ANCHOR_SYMBOLS) & set(runtime.tracked_symbols)

        regime = regime_engine.compute(store, "binance", now, runtime.tracked_symbols)

        new_promoted = {}
        for sym in promoted_symbols:
            state = store.get("binance", sym)
            if state is None or state.is_stale(now):
                continue
            price = state.price_now()
            if price is None:
                continue

            engine_scores = _run_stage_b_engines(state, now)
            exhaustion = exhaustion_engine.compute(state, now)
            rank = master_ranker.rank(engine_scores, exhaustion, regime, cfg.engine_weights, cfg.ranker_cfg)

            entry_up = entry_engine.compute("UP", state, now, rank["UP"], engine_scores, cfg.entry_cfg,
                                             cfg.shadow_cfg, cfg.risk_cfg["reward_risk_target_multiple"])
            entry_down = entry_engine.compute("DOWN", state, now, rank["DOWN"], engine_scores, cfg.entry_cfg,
                                               cfg.shadow_cfg, cfg.risk_cfg["reward_risk_target_multiple"])

            prev_fast = prev_fast_scores.get(sym, 0.0)
            cur_fast = fast_scores.get(sym, 0.0)
            new_promoted[sym] = _candidate_view(sym, price, engine_scores, exhaustion, rank,
                                                 entry_up, entry_down, prev_fast, cur_fast, state)

            for direction, rres, entry in (("UP", rank["UP"], entry_up), ("DOWN", rank["DOWN"], entry_down)):
                if rres.classification == "IGNORE":
                    continue
                shadow_only = direction == "DOWN"  # section 8/23: DOWN is always shadow-only

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
                    symbol=sym, exchange="binance", direction=direction, price=price,
                    spread_bps=state.spread_bps_now(), engine_scores={k: v.details | {"up": v.up, "down": v.down}
                                                                       for k, v in engine_scores.items()},
                    momentum_confidence=rres.confidence, exhaustion_risk=rres.exhaustion_risk,
                    classification=rres.classification, entry_quality=entry.entry_quality,
                    entry_type=entry.entry_type, accepted=accept, reject_reason=reject_reason,
                    shadow_only=shadow_only,
                )
                last_signal_log[key] = (now, rres.classification)
                runtime.digital_twin.track(signal_id, sym, "binance", direction, price)

                if accept:
                    await _open_shadow_trade(cfg, runtime, broker, universe, open_trades, signal_id, sym,
                                              direction, state, entry, price)

        runtime.promoted = new_promoted
        prev_fast_scores = fast_scores
        runtime.last_stage_a_scanned = len(runtime.tracked_symbols)

        duration_ms = (time.time() - t0) * 1000
        await runtime.ledger.record_engine_run(len(runtime.tracked_symbols), len(new_promoted), duration_ms)

        elapsed = time.time() - t0
        await asyncio.sleep(max(0.1, stage_a_cfg["cycle_interval_s"] - elapsed))


async def _open_shadow_trade(cfg, runtime, broker, universe, open_trades, signal_id, symbol, direction,
                              state, entry, price):
    plan = risk_engine.compute(direction, price, entry, cfg.risk_cfg)
    scenario = plan.default_scenario
    sym_filter = universe.get_filter(symbol)
    size = broker.apply_filters(sym_filter, scenario.size, price)
    if not size:
        logger.info("%s %s: shadow entry skipped, failed exchange filters", symbol, direction)
        return

    fill = broker.simulate_entry(state, direction, size)
    if fill is None:
        logger.warning("%s %s: no book data available for shadow fill", symbol, direction)
        return

    trade_id = await runtime.ledger.insert_shadow_trade(
        signal_id=signal_id, symbol=symbol, exchange="binance", direction=direction,
        entry_price=fill.avg_price, invalidation_price=plan.invalidation_price, stop_price=plan.stop_price,
        size=fill.filled_size, risk_pct=scenario.risk_pct, risk_amount=scenario.risk_amount,
        entry_type=entry.entry_type, fees_paid=fill.fee, slippage_pct=fill.slippage_pct,
        latency_ms=fill.latency_ms,
    )
    r_unit = abs(fill.avg_price - plan.invalidation_price)
    open_trade = exit_engine.OpenTrade(
        id=trade_id, symbol=symbol, direction=direction, entry_price=fill.avg_price,
        stop_price=plan.stop_price, r_unit=r_unit, trailing_state="INITIAL", mfe_pct=0.0, mae_pct=0.0,
    )
    open_trades[trade_id] = RuntimeTradeState(open_trade, fill.filled_size)
    logger.info("SHADOW ENTRY %s %s @ %.6f size=%.6f (%s)", symbol, direction, fill.avg_price, fill.filled_size,
                entry.entry_type)


async def exit_loop(cfg: Config, store: StateStore, runtime: AppRuntime, broker: ShadowBroker,
                     open_trades: dict[int, RuntimeTradeState]):
    exits_cfg = cfg.exits_cfg
    while True:
        for trade_id in list(open_trades.keys()):
            rts = open_trades.get(trade_id)
            if rts is None:
                continue
            ot = rts.open_trade
            state = store.get("binance", ot.symbol)
            price = state.price_now() if state else None
            if state is None or price is None:
                continue

            candidate = runtime.promoted.get(ot.symbol)
            current_confidence = candidate[ot.direction.lower()]["confidence"] if candidate else 0.0

            decision = exit_engine.evaluate(ot, price, current_confidence, rts.prev_confidence, exits_cfg)
            rts.prev_confidence = current_confidence

            if decision.partial_tp_triggered and not ot.partial_taken:
                partial_size = rts.remaining_size * exits_cfg["partial_take_profit_fraction"]
                fill = broker.simulate_exit(state, ot.direction, partial_size)
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
                fill = broker.simulate_exit(state, ot.direction, rts.remaining_size)
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
                logger.info("SHADOW EXIT %s %s @ %.6f reason=%s pnl=%.4f", ot.symbol, ot.direction,
                            fill.avg_price, decision.exit_reason, total_net_pnl)

        await asyncio.sleep(1.0)


async def digital_twin_loop(store: StateStore, digital_twin: DigitalTwin):
    while True:
        await digital_twin.tick(store)
        await asyncio.sleep(1.0)


async def universe_refresh_loop(cfg: Config, universe: Universe, runtime: AppRuntime):
    while True:
        await asyncio.sleep(cfg.universe["refresh_interval_s"])
        try:
            symbols = await universe.refresh()
            runtime.universe_size = len(symbols)
            for sym in symbols:
                f = universe.get_filter(sym)
                if f:
                    await runtime.ledger.upsert_symbol(sym, "binance", f.base_asset, f.quote_asset,
                                                        f.tick_size, f.step_size, f.min_notional, f.quote_volume_24h)
        except Exception:
            logger.exception("universe refresh failed")


async def restore_open_trades(runtime: AppRuntime, open_trades: dict[int, RuntimeTradeState]) -> None:
    rows = await runtime.ledger.get_open_trades()
    for row in rows:
        r_unit = abs(row["entry_price"] - row["invalidation_price"])
        ot = exit_engine.OpenTrade(
            id=row["id"], symbol=row["symbol"], direction=row["direction"], entry_price=row["entry_price"],
            stop_price=row["stop_price"], r_unit=r_unit, trailing_state=row["trailing_state"] or "INITIAL",
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

    adapter = BinanceAdapter(quote_asset=cfg.universe["quote_asset"],
                              ws_symbols_per_connection=cfg.universe["ws_symbols_per_connection"])
    universe = Universe(adapter, cfg.universe["min_quote_volume_24h"])
    symbols = await universe.refresh()
    for sym in symbols:
        f = universe.get_filter(sym)
        await ledger.upsert_symbol(sym, "binance", f.base_asset, f.quote_asset, f.tick_size, f.step_size,
                                    f.min_notional, f.quote_volume_24h)

    tracked = list(dict.fromkeys(symbols + list(REGIME_ANCHOR_SYMBOLS)))

    store = StateStore()
    digital_twin = DigitalTwin(cfg.twin_horizons_s, ledger)
    runtime = AppRuntime(
        shadow_mode=cfg.shadow_mode, real_orders=cfg.real_orders, exchanges=cfg.exchanges,
        start_time=time.time(), ledger=ledger, digital_twin=digital_twin, universe=universe,
        tracked_symbols=tracked, universe_size=len(symbols),
    )

    broker = ShadowBroker(cfg.shadow_cfg)
    open_trades: dict[int, RuntimeTradeState] = {}
    await restore_open_trades(runtime, open_trades)

    on_trade, on_book_ticker, on_depth = await _dispatch_handlers(store, "binance")

    dash_cfg = cfg.dashboard_cfg
    fastapi_app = create_app(runtime)
    server = uvicorn.Server(uvicorn.Config(fastapi_app, host=dash_cfg["host"], port=dash_cfg["port"],
                                            log_level="warning"))

    logger.info("MOMENTUM ENGINE starting: SHADOW_MODE=%s REAL_ORDERS=%d universe=%d tracked=%d",
                cfg.shadow_mode, cfg.real_orders, len(symbols), len(tracked))

    await asyncio.gather(
        adapter.stream_market_data(tracked, on_trade, on_book_ticker, on_depth),
        stage_ab_loop(cfg, store, universe, runtime, broker, open_trades),
        exit_loop(cfg, store, runtime, broker, open_trades),
        digital_twin_loop(store, digital_twin),
        universe_refresh_loop(cfg, universe, runtime),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())

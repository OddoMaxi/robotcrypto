"""BASELINE_MOMENTUM_STARTING (spec section 2: "inchangee"). A thin wrapper
around momentum.engines.starting.compute() - the exact same formula the
existing Momentum Bot uses - run here on the Lab's own independent data feed
and ledger. This is the CONTROL strategy every CHALLENGER (the other four) is
measured against; it must not diverge from the baseline's own logic in any way.
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines import starting
from momentum.engines.types import EngineScore
from strategy_lab.market_bus import MarketEvent
from strategy_lab.strategies.base import StrategySignal, estimate_round_trip_cost_pct, exhaustion_veto

NAME = "BASELINE_MOMENTUM_STARTING"


def compute(event: MarketEvent, primary_ex: str, state: SymbolState, engine_scores: dict[str, EngineScore],
            cross_result: EngineScore | None, exhaustion_risk: tuple[float, float],
            late_entry_risk: tuple[float, float], strategy_cfg: dict, common_cfg: dict,
            taker_fee_bps: float, now: float) -> StrategySignal | None:
    price = state.price_now()
    if price is None:
        return None

    result = starting.compute(state, now, engine_scores, cross_result)
    direction = "UP" if result.up >= result.down else "DOWN"
    score = result.up if direction == "UP" else result.down
    exh = exhaustion_risk[0] if direction == "UP" else exhaustion_risk[1]
    late = late_entry_risk[0] if direction == "UP" else late_entry_risk[1]

    signal = StrategySignal(
        strategy=NAME, symbol=event.symbol, exchange=primary_ex, direction=direction, price=price,
        score=score, exhaustion_risk=exh, late_entry_risk=late, details=result.details,
    )

    veto = exhaustion_veto(exh, late, common_cfg["exhaustion_veto"], common_cfg["late_entry_veto"])
    if veto:
        signal.reject_reason = veto
        return signal
    if score < strategy_cfg["min_score"]:
        signal.reject_reason = "score_below_threshold"
        return signal

    vol_pct = state.realized_vol(now, 60) or 0.2
    signal.expected_move_pct = vol_pct
    signal.expected_cost_pct = estimate_round_trip_cost_pct(state, taker_fee_bps, now)
    signal.accepted = signal.expected_net_edge_pct > common_cfg["min_net_edge_pct"]
    if not signal.accepted:
        signal.reject_reason = "net_edge_not_positive"
    return signal

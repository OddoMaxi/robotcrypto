"""PERSISTENT_MICRO_TREND (spec section 3). Detects a move that HOLDS across a
ladder of short horizons rather than a single noisy final delta: directional
persistence, low reversal frequency, and confirming volume/flow/book/vol-
expansion/spread/cross-exchange context. PERSISTENCE_SCORE is a fraction of
criteria met, 0-100 - never a probability. UP/DOWN symmetric throughout.
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore
from strategy_lab.market_bus import MarketEvent
from strategy_lab.strategies.base import StrategySignal, estimate_round_trip_cost_pct, exhaustion_veto

NAME = "PERSISTENT_MICRO_TREND"
HEALTHY_SPREAD_BPS = 20.0
CROSS_EXCHANGE_FLOOR = 10.0


def _persistence_components(state: SymbolState, now: float, horizons_s: list[float]) -> dict | None:
    returns = {h: state.velocity_pct(now, h) for h in horizons_s}
    valid = [(h, v) for h, v in returns.items() if v is not None]
    if len(valid) < 4:
        return None
    vals = [v for _, v in valid]
    n = len(vals)
    persistence_up = sum(1 for v in vals if v > 0) / n
    persistence_down = sum(1 for v in vals if v < 0) / n
    signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in vals]
    comparable = [s for s in signs if s != 0]
    flips = sum(1 for i in range(1, len(comparable)) if comparable[i] != comparable[i - 1])
    reversal_frequency = (flips / (len(comparable) - 1)) if len(comparable) > 1 else 0.0
    return {
        "returns": returns, "persistence_up": persistence_up, "persistence_down": persistence_down,
        "reversal_frequency": reversal_frequency, "n": n,
    }


def _impulse_retracement_ratio(state: SymbolState, now: float, lookback_s: float, direction: str) -> float | None:
    price = state.price_now()
    peak = state.local_high(now, lookback_s)
    low = state.local_low(now, lookback_s)
    if price is None or peak is None or low is None or peak <= low:
        return None
    impulse = peak - low
    retracement = (peak - price) if direction == "UP" else (price - low)
    return max(0.0, retracement) / impulse


def compute(event: MarketEvent, primary_ex: str, state: SymbolState, engine_scores: dict[str, EngineScore],
            cross_result: EngineScore | None, exhaustion_risk: tuple[float, float],
            late_entry_risk: tuple[float, float], strategy_cfg: dict, common_cfg: dict,
            taker_fee_bps: float, now: float) -> StrategySignal | None:
    price = state.price_now()
    if price is None:
        return None

    horizons = strategy_cfg["horizons_s"]
    comp = _persistence_components(state, now, horizons)
    if comp is None:
        return None

    vol = engine_scores.get("volume")
    of = engine_scores.get("orderflow")
    ob = engine_scores.get("orderbook_imbalance")
    ve = engine_scores.get("volatility_expansion")
    spread_bps = state.spread_bps_now()
    healthy_spread = spread_bps is not None and spread_bps < HEALTHY_SPREAD_BPS
    cross_up = cross_result.up if cross_result else 0.0
    cross_down = cross_result.down if cross_result else 0.0

    direction = "UP" if comp["persistence_up"] >= comp["persistence_down"] else "DOWN"
    persistence = comp["persistence_up"] if direction == "UP" else comp["persistence_down"]
    ir_ratio = _impulse_retracement_ratio(state, now, max(horizons), direction)

    up_side = direction == "UP"
    components = {
        "persistence": persistence,
        "low_reversal": 1.0 - comp["reversal_frequency"],
        "volume_confirm": 1.0 if vol and (vol.up if up_side else vol.down) > 15 else 0.0,
        "flow_confirm": 1.0 if of and (of.up if up_side else of.down) > 15 else 0.0,
        "book_confirm": 1.0 if ob and (ob.up if up_side else ob.down) > 15 else 0.0,
        "vol_expansion": 1.0 if ve and (ve.up if up_side else ve.down) > 15 else 0.0,
        "healthy_spread": 1.0 if healthy_spread else 0.0,
        "cross_exchange": 1.0 if (cross_up if up_side else cross_down) > CROSS_EXCHANGE_FLOOR else 0.0,
    }
    score = sum(components.values()) / len(components) * 100.0

    exh = exhaustion_risk[0] if up_side else exhaustion_risk[1]
    late = late_entry_risk[0] if up_side else late_entry_risk[1]

    signal = StrategySignal(
        strategy=NAME, symbol=event.symbol, exchange=primary_ex, direction=direction, price=price,
        score=score, exhaustion_risk=exh, late_entry_risk=late,
        details={"components": components, "returns": comp["returns"],
                 "reversal_frequency": comp["reversal_frequency"], "impulse_retracement_ratio": ir_ratio},
    )

    veto = exhaustion_veto(exh, late, common_cfg["exhaustion_veto"], common_cfg["late_entry_veto"])
    if veto:
        signal.reject_reason = veto
        return signal
    if score < strategy_cfg["min_score"]:
        signal.reject_reason = "score_below_threshold"
        return signal

    longest = max(horizons)
    raw_move = state.velocity_pct(now, longest) or 0.0
    signal.expected_move_pct = abs(raw_move) * (1.0 - min(1.0, comp["reversal_frequency"]))
    signal.expected_cost_pct = estimate_round_trip_cost_pct(state, taker_fee_bps, now)
    signal.accepted = signal.expected_net_edge_pct > common_cfg["min_net_edge_pct"]
    if not signal.accepted:
        signal.reject_reason = "net_edge_not_positive"
    return signal

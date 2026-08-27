"""BREAKOUT -> RETEST -> CONTINUATION (spec section 6). Finds a micro
structural level (the price extreme *before* a recent breakout, not just the
raw rolling high/low which would already include the breakout itself), checks
that a breakout beyond it happened, and scores the quality of price coming
back to retest that level: how precisely it held, whether volume/flow/depth
confirm a healthy (not distributive) retest, and whether price is now being
rejected away from the level again in the breakout direction. UP/DOWN
symmetric (resistance-breakout-retest vs support-breakout-retest).
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore
from strategy_lab.market_bus import MarketEvent
from strategy_lab.strategies.base import StrategySignal, estimate_round_trip_cost_pct, exhaustion_veto

NAME = "BREAKOUT_RETEST_CONTINUATION"
RETEST_RECENT_S = 20.0        # "now" window used both for the live retest check and the breakout check
HEALTHY_SPREAD_BPS = 25.0


def _older_extreme(state: SymbolState, now: float, total_s: float, recent_s: float, want_max: bool) -> float | None:
    """Extreme of the price BEFORE the most recent `recent_s` window - i.e. the
    structural level as it stood prior to any breakout that may have just
    happened, not contaminated by the breakout move itself."""
    pts = state.price_buf.window(now, total_s)
    older = [p.value for p in pts if p.ts <= now - recent_s]
    if not older:
        return None
    return max(older) if want_max else min(older)


def compute(event: MarketEvent, primary_ex: str, state: SymbolState, engine_scores: dict[str, EngineScore],
            cross_result: EngineScore | None, exhaustion_risk: tuple[float, float],
            late_entry_risk: tuple[float, float], strategy_cfg: dict, common_cfg: dict,
            taker_fee_bps: float, now: float) -> StrategySignal | None:
    price = state.price_now()
    if price is None:
        return None

    lookback = strategy_cfg["level_lookback_s"]
    tolerance = strategy_cfg["retest_tolerance_pct"]
    min_magnitude = strategy_cfg["min_breakout_magnitude_pct"]

    level_up = _older_extreme(state, now, lookback, RETEST_RECENT_S, want_max=True)
    level_down = _older_extreme(state, now, lookback, RETEST_RECENT_S, want_max=False)
    recent_high = state.local_high(now, RETEST_RECENT_S)
    recent_low = state.local_low(now, RETEST_RECENT_S)
    if level_up is None or level_down is None or recent_high is None or recent_low is None or level_up <= 0:
        return None

    breakout_magnitude_up_pct = (recent_high - level_up) / level_up * 100.0
    breakout_magnitude_down_pct = (level_down - recent_low) / level_down * 100.0 if level_down > 0 else 0.0
    broke_up = breakout_magnitude_up_pct >= min_magnitude
    broke_down = breakout_magnitude_down_pct >= min_magnitude

    if not broke_up and not broke_down:
        return None
    # if both sides technically qualify (whipsaw), prefer the larger, more recent move
    up = broke_up if broke_up != broke_down else breakout_magnitude_up_pct >= breakout_magnitude_down_pct
    direction = "UP" if up else "DOWN"
    level = level_up if up else level_down
    breakout_magnitude_pct = breakout_magnitude_up_pct if up else breakout_magnitude_down_pct

    distance_from_level_pct = abs(price - level) / level * 100.0
    is_retesting = distance_from_level_pct <= tolerance
    holding_level = (price >= level * (1 - tolerance / 100.0)) if up else (price <= level * (1 + tolerance / 100.0))
    retest_quality = max(0.0, 1.0 - distance_from_level_pct / tolerance) if tolerance > 0 else 0.0

    v5 = state.velocity_pct(now, 5) or 0.0
    rejection_strength = max(0.0, v5) if up else max(0.0, -v5)

    vol = engine_scores.get("volume")
    of = engine_scores.get("orderflow")
    ob = engine_scores.get("orderbook_imbalance")
    spread_bps = state.spread_bps_now()
    healthy_spread = spread_bps is not None and spread_bps < HEALTHY_SPREAD_BPS
    cross_confirm = bool(cross_result and (cross_result.up if up else cross_result.down) > 10.0)

    checks = {
        "is_retesting": is_retesting,
        "holding_level": holding_level,
        "rejection_strength": rejection_strength > 0.03,
        "volume_confirm": bool(vol and (vol.up if up else vol.down) > 10),
        "flow_confirm": bool(of and (of.up if up else of.down) > 10),
        "book_confirm": bool(ob and (ob.up if up else ob.down) > 10),
        "healthy_spread": healthy_spread,
        "cross_exchange_confirms": cross_confirm,
    }
    score = sum(1 for c in checks.values() if c) / len(checks) * 100.0

    exh = exhaustion_risk[0] if up else exhaustion_risk[1]
    late = late_entry_risk[0] if up else late_entry_risk[1]
    signal = StrategySignal(
        strategy=NAME, symbol=event.symbol, exchange=primary_ex, direction=direction, price=price,
        score=score, exhaustion_risk=exh, late_entry_risk=late,
        details={"checks": checks, "level": level, "breakout_magnitude_pct": breakout_magnitude_pct,
                 "distance_from_level_pct": distance_from_level_pct, "retest_quality": retest_quality,
                 "rejection_strength": rejection_strength},
    )

    if not is_retesting or not holding_level:
        signal.reject_reason = "not_at_retest"
        return signal
    veto = exhaustion_veto(exh, late, common_cfg["exhaustion_veto"], common_cfg["late_entry_veto"])
    if veto:
        signal.reject_reason = veto
        return signal
    if score < strategy_cfg["min_score"]:
        signal.reject_reason = "score_below_threshold"
        return signal

    signal.expected_move_pct = breakout_magnitude_pct * 0.5   # conservative: expects to reclaim half the prior breakout leg
    signal.expected_cost_pct = estimate_round_trip_cost_pct(state, taker_fee_bps, now)
    signal.accepted = signal.expected_net_edge_pct > common_cfg["min_net_edge_pct"]
    if not signal.accepted:
        signal.reject_reason = "net_edge_not_positive"
    return signal

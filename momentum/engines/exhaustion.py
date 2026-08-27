"""EXHAUSTION / PUMP PROTECTION ENGINE (spec section 6). CRITICAL: this is what
stops the bot from being the buyer of an already-finished pump. A symbol can
score 95 on momentum and still be vetoed here.

Cross-exchange divergence is not computable with Binance-only data in this
slice; its weight is documented and contributes 0 until Bybit/OKX are added -
it does not silently inflate or deflate the score.
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.acceleration import compute as compute_acceleration
from momentum.engines.types import ExhaustionScore

VWAP_WINDOW_S = 300
DIST_FULL_SCORE_PCT = 2.0        # % distance from VWAP that maps to max risk contribution
SPREAD_WIDEN_RATIO_FULL = 2.0     # recent/older spread ratio mapping to max risk contribution
REJECTION_WINDOW_S = 15


def _component_weights():
    return {
        "distance": 0.25,
        "deceleration": 0.20,
        "volume_climax": 0.15,
        "spread_widening": 0.15,
        "liquidity_disappearing": 0.15,
        "wick_rejection": 0.10,
        "cross_exchange_divergence": 0.0,  # stub - documented, not computable yet
    }


def compute(state: SymbolState, now: float) -> ExhaustionScore:
    price = state.price_now()
    vwap = state.vwap(now, VWAP_WINDOW_S)
    w = _component_weights()

    if price is None or vwap is None or vwap == 0:
        return ExhaustionScore(0.0, 0.0, {"reason": "insufficient_history"})

    dist_pct = (price - vwap) / vwap * 100.0
    dist_up_risk = min(100.0, max(0.0, dist_pct) / DIST_FULL_SCORE_PCT * 100.0)
    dist_down_risk = min(100.0, max(0.0, -dist_pct) / DIST_FULL_SCORE_PCT * 100.0)

    accel = compute_acceleration(state, now)
    v10 = state.velocity_pct(now, 10) or 0.0
    decel_up_risk = 100.0 if (v10 > 0 and accel.details.get("acceleration", 0) < 0) else 0.0
    decel_down_risk = 100.0 if (v10 < 0 and accel.details.get("acceleration", 0) > 0) else 0.0

    rate_now = state.total_volume(now, 10) / 10
    rate_prior = state.total_volume(now, 20) / 20  # includes the now window, still a fair "was it higher" proxy
    climax_risk = 100.0 if (rate_prior > 0 and rate_now < rate_prior * 0.6) else 0.0

    spread_recent = state.avg_spread_bps(now, 5)
    spread_older = state.avg_spread_bps(now, 60)
    spread_widen_risk = 0.0
    if spread_recent is not None and spread_older and spread_older > 0:
        ratio = spread_recent / spread_older
        spread_widen_risk = min(100.0, max(0.0, (ratio - 1.0) / (SPREAD_WIDEN_RATIO_FULL - 1.0) * 100.0))

    bid_recent = state.avg_bid_depth(now, 10)
    bid_older = state.avg_bid_depth(now, 60)
    ask_recent = state.avg_ask_depth(now, 10)
    ask_older = state.avg_ask_depth(now, 60)
    bid_disappearing_risk = 0.0
    ask_disappearing_risk = 0.0
    if bid_recent is not None and bid_older and bid_older > 0:
        bid_disappearing_risk = max(0.0, 1.0 - bid_recent / bid_older) * 100.0
    if ask_recent is not None and ask_older and ask_older > 0:
        ask_disappearing_risk = max(0.0, 1.0 - ask_recent / ask_older) * 100.0

    local_high_15 = state.local_high(now, REJECTION_WINDOW_S)
    local_low_15 = state.local_low(now, REJECTION_WINDOW_S)
    wick_up_risk = 0.0
    wick_down_risk = 0.0
    if local_high_15 and local_high_15 > 0:
        wick_up_risk = max(0.0, (local_high_15 - price) / local_high_15 * 100.0) * 20.0  # scale small pullback into visible risk
    if local_low_15 and local_low_15 > 0:
        wick_down_risk = max(0.0, (price - local_low_15) / local_low_15 * 100.0) * 20.0

    up_risk = (
        dist_up_risk * w["distance"]
        + decel_up_risk * w["deceleration"]
        + climax_risk * w["volume_climax"]
        + spread_widen_risk * w["spread_widening"]
        + bid_disappearing_risk * w["liquidity_disappearing"]
        + min(100.0, wick_up_risk) * w["wick_rejection"]
    )
    down_risk = (
        dist_down_risk * w["distance"]
        + decel_down_risk * w["deceleration"]
        + climax_risk * w["volume_climax"]
        + spread_widen_risk * w["spread_widening"]
        + ask_disappearing_risk * w["liquidity_disappearing"]
        + min(100.0, wick_down_risk) * w["wick_rejection"]
    )

    return ExhaustionScore(
        up_risk=up_risk,
        down_risk=down_risk,
        details={
            "dist_pct": dist_pct, "decel_up_risk": decel_up_risk, "decel_down_risk": decel_down_risk,
            "climax_risk": climax_risk, "spread_widen_risk": spread_widen_risk,
            "bid_disappearing_risk": bid_disappearing_risk, "ask_disappearing_risk": ask_disappearing_risk,
            "wick_up_risk": wick_up_risk, "wick_down_risk": wick_down_risk,
            "cross_exchange_divergence": "N/A (binance-only slice)",
        },
    )

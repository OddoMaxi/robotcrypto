"""LATE_ENTRY_RISK (V1.1 mission 5). Complements exhaustion.py, which asks "does
this look like it's rolling over right now" (microstructure). This asks "has
price already traveled too far from where this specific impulse started to be
a good NEW entry" - a distance-since-impulse-start measure, decelerating and
divergence checks add to the risk. High volatility alone is never treated as a
buy signal here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.data.state import SymbolState

IMPULSE_LOOKBACK_S = 120.0     # how far back we look for "where this impulse started"
FULL_RISK_DISTANCE_PCT = 2.0    # % traveled since impulse start that maps to max distance risk
DECELERATION_PENALTY = 1.3


@dataclass(slots=True)
class LateEntryScore:
    up_risk: float
    down_risk: float
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        self.up_risk = max(0.0, min(100.0, self.up_risk))
        self.down_risk = max(0.0, min(100.0, self.down_risk))


def compute(state: SymbolState, now: float) -> LateEntryScore:
    price = state.price_now()
    impulse_low = state.local_low(now, IMPULSE_LOOKBACK_S)
    impulse_high = state.local_high(now, IMPULSE_LOOKBACK_S)
    if price is None or impulse_low is None or impulse_high is None:
        return LateEntryScore(0.0, 0.0, {"reason": "insufficient_history"})

    move_up_pct = (price - impulse_low) / impulse_low * 100.0 if impulse_low > 0 else 0.0
    move_down_pct = (impulse_high - price) / impulse_high * 100.0 if impulse_high > 0 else 0.0

    v10 = state.velocity_pct(now, 10) or 0.0
    v30 = state.velocity_pct(now, 30) or 0.0
    decelerating_up = v30 > 0 and v10 < v30 * 0.5
    decelerating_down = v30 < 0 and v10 > v30 * 0.5

    buy_recent = state.buy_volume(now, 10)
    sell_recent = state.sell_volume(now, 10)
    total_recent = buy_recent + sell_recent
    buy_ratio_recent = (buy_recent / total_recent) if total_recent > 0 else 0.5
    # price/volume divergence: price still up but aggressive buying no longer dominant
    divergence_up = move_up_pct > 0.3 and buy_ratio_recent < 0.5
    divergence_down = move_down_pct > 0.3 and buy_ratio_recent > 0.5

    up_risk = min(100.0, move_up_pct / FULL_RISK_DISTANCE_PCT * 100.0)
    down_risk = min(100.0, move_down_pct / FULL_RISK_DISTANCE_PCT * 100.0)
    if decelerating_up:
        up_risk *= DECELERATION_PENALTY
    if decelerating_down:
        down_risk *= DECELERATION_PENALTY
    if divergence_up:
        up_risk = min(100.0, up_risk + 15.0)
    if divergence_down:
        down_risk = min(100.0, down_risk + 15.0)

    return LateEntryScore(
        up_risk=up_risk, down_risk=down_risk,
        details={
            "move_up_pct": move_up_pct, "move_down_pct": move_down_pct,
            "decelerating_up": decelerating_up, "decelerating_down": decelerating_down,
            "divergence_up": divergence_up, "divergence_down": divergence_down,
        },
    )

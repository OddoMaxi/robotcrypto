"""PRICE ACCELERATION ENGINE (spec section 5).

Measures the change in velocity itself: compares the %/s rate over the most
recent 10s window against the %/s rate over the preceding 10-30s window. We
specifically want velocity>0 AND acceleration>0 for UP (and the mirror for
DOWN) - a fast move that is already decelerating is not what this engine
rewards, that's exhaustion's job.
"""
from __future__ import annotations

from momentum.engines.types import EngineScore
from momentum.data.state import SymbolState

FULL_SCORE_ACCEL = 0.03  # %/s^2-ish delta that maps to a raw score of 100


def compute(state: SymbolState, now: float) -> EngineScore:
    p_now = state.price_now()
    p_10 = state.price_buf.value_n_seconds_ago(now, 10)
    p_30 = state.price_buf.value_n_seconds_ago(now, 30)
    if p_now is None or p_10 is None or p_30 is None or p_10 == 0 or p_30 == 0:
        return EngineScore(0.0, 0.0, {"reason": "insufficient_history"})

    rate_recent = (p_now - p_10) / p_10 * 100.0 / 10.0     # %/s over last 10s
    rate_prior = (p_10 - p_30) / p_30 * 100.0 / 20.0        # %/s over the 10-30s-ago window

    acceleration = rate_recent - rate_prior

    velocity_up = rate_recent > 0
    velocity_down = rate_recent < 0

    up = 0.0
    down = 0.0
    if velocity_up and acceleration > 0:
        up = acceleration / FULL_SCORE_ACCEL * 100.0
    if velocity_down and acceleration < 0:
        down = -acceleration / FULL_SCORE_ACCEL * 100.0

    return EngineScore(
        up=up,
        down=down,
        details={"rate_recent": rate_recent, "rate_prior": rate_prior, "acceleration": acceleration},
    )

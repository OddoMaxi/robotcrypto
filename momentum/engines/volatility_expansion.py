"""VOLATILITY EXPANSION ENGINE (spec section 5).

Detects compression -> expansion: recent realized vol jumping well above the
5-minute baseline. This engine has no inherent direction of its own, so its
score is attributed to whichever side the concurrent short-term velocity
already favors (it amplifies a move that's expanding, it doesn't invent one).
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore

RECENT_S = 30
BASELINE_S = 300
FULL_SCORE_RATIO = 2.5


def compute(state: SymbolState, now: float) -> EngineScore:
    recent_vol = state.realized_vol(now, RECENT_S)
    baseline_vol = state.realized_vol(now, BASELINE_S)
    v10 = state.velocity_pct(now, 10)

    if recent_vol is None or baseline_vol is None or baseline_vol <= 0 or v10 is None:
        return EngineScore(0.0, 0.0, {"reason": "insufficient_history"})

    ratio = recent_vol / baseline_vol
    score = min(100.0, max(0.0, (ratio - 1.0) / (FULL_SCORE_RATIO - 1.0) * 100.0))

    up = score if v10 > 0 else 0.0
    down = score if v10 < 0 else 0.0

    return EngineScore(
        up=up, down=down,
        details={"recent_vol": recent_vol, "baseline_vol": baseline_vol, "ratio": ratio},
    )

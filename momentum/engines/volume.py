"""VOLUME ACCELERATION ENGINE (spec section 5).

Current volume rate vs a 5-minute baseline, split by aggressive buy vs sell
volume so a volume spike gets attributed to the direction it actually supports.
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore

RECENT_S = 10
BASELINE_S = 300
FULL_SCORE_RATIO = 4.0  # recent-rate/baseline-rate of 4x maps to raw score 100


def compute(state: SymbolState, now: float) -> EngineScore:
    baseline_rate = state.total_volume(now, BASELINE_S) / BASELINE_S
    recent_rate = state.total_volume(now, RECENT_S) / RECENT_S

    if baseline_rate <= 0:
        ratio = 0.0
    else:
        ratio = recent_rate / baseline_rate

    ratio_score = min(100.0, ratio / FULL_SCORE_RATIO * 100.0)

    buy_vol = state.buy_volume(now, 30)
    sell_vol = state.sell_volume(now, 30)
    total = buy_vol + sell_vol
    buy_ratio = (buy_vol / total) if total > 0 else 0.5

    # bias the ratio-driven score toward whichever side dominates
    up = ratio_score * max(0.0, (buy_ratio - 0.5) * 2)
    down = ratio_score * max(0.0, (0.5 - buy_ratio) * 2)

    return EngineScore(
        up=up,
        down=down,
        details={"ratio": ratio, "buy_ratio": buy_ratio, "recent_rate": recent_rate, "baseline_rate": baseline_rate},
    )

"""ORDER FLOW ENGINE (spec section 5).

Looks at aggressive buy/sell dominance across several short sub-windows and
rewards persistence - a buy-side skew that holds across 5s/10s/20s is worth
more than a single noisy print.
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore

SUBWINDOWS_S = (5, 10, 20)
DOMINANCE_THRESHOLD = 0.55  # buy_ratio above this counts as "buy dominant" for persistence


def _buy_ratio(state: SymbolState, now: float, window_s: int) -> float | None:
    buy = state.buy_volume(now, window_s)
    sell = state.sell_volume(now, window_s)
    total = buy + sell
    if total <= 0:
        return None
    return buy / total


def compute(state: SymbolState, now: float) -> EngineScore:
    ratios = [_buy_ratio(state, now, w) for w in SUBWINDOWS_S]
    valid = [r for r in ratios if r is not None]
    if not valid:
        return EngineScore(0.0, 0.0, {"reason": "no_flow"})

    persistence_up = sum(1 for r in valid if r >= DOMINANCE_THRESHOLD)
    persistence_down = sum(1 for r in valid if r <= (1 - DOMINANCE_THRESHOLD))

    primary = valid[0]  # shortest window = most current read
    deviation = (primary - 0.5) * 2  # -1..1

    up = max(0.0, deviation) * 100.0 * (persistence_up / len(valid))
    down = max(0.0, -deviation) * 100.0 * (persistence_down / len(valid))

    return EngineScore(
        up=up,
        down=down,
        details={"ratios": dict(zip(SUBWINDOWS_S, ratios)), "persistence_up": persistence_up,
                 "persistence_down": persistence_down},
    )

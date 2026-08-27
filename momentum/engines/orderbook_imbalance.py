"""ORDER BOOK IMBALANCE ENGINE (spec section 5).

Reads real depth (top-5 levels): bid/ask imbalance plus whether liquidity on
one side is thinning out (disappearing bids = bearish, disappearing asks =
less resistance to an upmove).
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore

RECENT_S = 10
OLDER_S = 60


def compute(state: SymbolState, now: float) -> EngineScore:
    imbalance = state.imbalance_now()
    if imbalance is None:
        return EngineScore(0.0, 0.0, {"reason": "no_depth"})

    ask_recent = state.avg_ask_depth(now, RECENT_S)
    ask_older = state.avg_ask_depth(now, OLDER_S)
    bid_recent = state.avg_bid_depth(now, RECENT_S)
    bid_older = state.avg_bid_depth(now, OLDER_S)

    ask_shrinking = 0.0
    bid_shrinking = 0.0
    if ask_recent is not None and ask_older and ask_older > 0:
        ask_shrinking = max(0.0, 1.0 - ask_recent / ask_older)  # 0..1
    if bid_recent is not None and bid_older and bid_older > 0:
        bid_shrinking = max(0.0, 1.0 - bid_recent / bid_older)

    imbalance_up = max(0.0, imbalance) * 100.0
    imbalance_down = max(0.0, -imbalance) * 100.0

    up = imbalance_up * 0.6 + ask_shrinking * 100.0 * 0.4
    down = imbalance_down * 0.6 + bid_shrinking * 100.0 * 0.4

    return EngineScore(
        up=up,
        down=down,
        details={"imbalance": imbalance, "ask_shrinking": ask_shrinking, "bid_shrinking": bid_shrinking},
    )

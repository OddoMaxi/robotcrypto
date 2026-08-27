"""EXCHANGE QUALITY ENGINE (mission 9): for a symbol available on multiple
exchanges, determines which one would have been the best execution venue -
spread, depth, fees, simulated slippage for the intended size. Never places
an order; this only labels BEST_EXECUTION_EXCHANGE for the shadow broker to
use and the dashboard to display.
"""
from __future__ import annotations

from dataclasses import dataclass

from momentum.data.state import SymbolState


@dataclass(slots=True)
class ExchangeQualityResult:
    best_exchange: str
    scores: dict  # exchange -> {spread_bps, depth_notional, taker_fee_bps, quality_score}


def _quality_score(spread_bps: float, depth_notional: float, taker_fee_bps: float) -> float:
    # lower spread/fees is better, deeper liquidity is better; simple weighted
    # composite kept explainable rather than a fitted model (no ML black box)
    spread_score = max(0.0, 100.0 - spread_bps * 2)
    fee_score = max(0.0, 100.0 - taker_fee_bps * 2)
    depth_score = min(100.0, (depth_notional / 5000.0) * 100.0)
    return spread_score * 0.4 + fee_score * 0.2 + depth_score * 0.4


def compute(
    direction: str,
    states_by_exchange: dict[str, SymbolState],
    now: float,
    taker_fee_bps_by_exchange: dict[str, float],
) -> ExchangeQualityResult | None:
    scores = {}
    for ex, state in states_by_exchange.items():
        price = state.price_now()
        spread_bps = state.spread_bps_now()
        if price is None or spread_bps is None:
            continue
        depth = state.avg_ask_depth(now, 10) if direction == "UP" else state.avg_bid_depth(now, 10)
        depth_notional = (depth or 0.0) * price
        fee_bps = taker_fee_bps_by_exchange.get(ex, 10.0)
        scores[ex] = {
            "spread_bps": spread_bps,
            "depth_notional": depth_notional,
            "taker_fee_bps": fee_bps,
            "quality_score": _quality_score(spread_bps, depth_notional, fee_bps),
        }

    if not scores:
        return None

    best = max(scores, key=lambda ex: scores[ex]["quality_score"])
    return ExchangeQualityResult(best_exchange=best, scores=scores)

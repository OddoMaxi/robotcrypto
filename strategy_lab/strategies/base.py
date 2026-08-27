"""Common StrategySignal shape + shared cost-estimation helpers every strategy
uses to answer "is expected net value > 0 after spread, fees, slippage,
depth, latency" (section 1) before proposing a Shadow entry. 0 TRADE > BAD
TRADE: a strategy that can't clear this bar returns accepted=False.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.data.state import SymbolState


@dataclass(slots=True)
class StrategySignal:
    strategy: str
    symbol: str
    exchange: str
    direction: str            # "UP" | "DOWN"
    price: float
    score: float               # 0-100, strategy-specific, never presented as a probability
    phase: str | None = None    # e.g. IMPULSE/PULLBACK/REACCELERATION
    exhaustion_risk: float = 0.0
    late_entry_risk: float = 0.0
    expected_move_pct: float = 0.0
    expected_cost_pct: float = 0.0
    accepted: bool = False
    reject_reason: str | None = None
    details: dict = field(default_factory=dict)

    @property
    def expected_net_edge_pct(self) -> float:
        return self.expected_move_pct - self.expected_cost_pct


def estimate_round_trip_cost_pct(state: SymbolState, taker_fee_bps: float, now: float) -> float:
    """ENTRY_FEE + EXIT_FEE + SPREAD_COST + a depth-derived slippage estimate,
    expressed as a round-trip % of notional (section 11's cost side, computed
    up front so a strategy can veto itself before ever proposing a trade)."""
    spread_bps = state.spread_bps_now() or 0.0
    fee_pct = (taker_fee_bps / 100.0) * 2   # entry + exit, bps -> pct
    spread_cost_pct = (spread_bps / 100.0)  # crossing the spread once, round trip already reflected in taker fills
    bid_depth = state.avg_bid_depth(now, 10) or 0.0
    ask_depth = state.avg_ask_depth(now, 10) or 0.0
    thin_book_penalty_pct = 0.0
    if bid_depth <= 0 or ask_depth <= 0:
        thin_book_penalty_pct = 0.05   # no live depth read yet - be conservative, not zero-cost
    return fee_pct + spread_cost_pct + thin_book_penalty_pct


def exhaustion_veto(exhaustion_risk: float, late_entry_risk: float, exhaustion_threshold: float,
                     late_entry_threshold: float) -> str | None:
    """Section 5: a strong score can still become NO_TRADE. Shared so every
    strategy applies the exact same veto rule, not five slightly different ones."""
    if exhaustion_risk >= exhaustion_threshold:
        return "exhausted"
    if late_entry_risk >= late_entry_threshold:
        return "too_late"
    return None

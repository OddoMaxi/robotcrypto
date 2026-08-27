"""MOMENTUM_META_ENGINE (spec section 8). Consolidates every strategy's signal
for the same symbol+cycle into a per-direction read: how many strategies agree
(AGREEMENT_COUNT), how many actively disagree (CONFLICT_COUNT), a combined
META_SIGNAL_STRENGTH, and an aggregate EXPECTED_MOVE/EXPECTED_COST/
EXPECTED_NET_EDGE. This is a consolidation/analysis layer, not a trading gate -
each strategy still decides its own entry independently (see strategies/*.py);
the meta engine's job is to let AGREEMENT_COUNT cohorts (1/2/3/4+) be sliced
and their real expectancy measured later (see ledger.get_agreement_cohort_stats),
never to promise a probability from an unweighted vote count.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from strategy_lab.strategies.base import StrategySignal


@dataclass(slots=True)
class MetaResult:
    direction: str
    meta_signal_strength: float
    agreement_count: int
    conflict_count: int
    expected_move_pct: float
    expected_cost_pct: float
    contributing_strategies: list[str] = field(default_factory=list)

    @property
    def expected_net_edge_pct(self) -> float:
        return self.expected_move_pct - self.expected_cost_pct


def compute_meta(signals: list[StrategySignal], actionable_threshold: float) -> dict[str, MetaResult]:
    """One call per symbol+cycle, given every strategy's signal for that
    MARKET_EVENT_ID (missing signals just mean that strategy had nothing to
    say this cycle - it does not count against agreement OR conflict)."""
    out: dict[str, MetaResult] = {}
    for direction in ("UP", "DOWN"):
        opposite = "DOWN" if direction == "UP" else "UP"
        agreeing = [s for s in signals if s.direction == direction and s.score >= actionable_threshold]
        conflicting = [s for s in signals if s.direction == opposite and s.score >= actionable_threshold]
        if not agreeing:
            continue
        strength = sum(s.score for s in agreeing) / len(agreeing)
        expected_move = max((s.expected_move_pct for s in agreeing), default=0.0)
        expected_cost = max((s.expected_cost_pct for s in agreeing), default=0.0)
        out[direction] = MetaResult(
            direction=direction, meta_signal_strength=strength, agreement_count=len(agreeing),
            conflict_count=len(conflicting), expected_move_pct=expected_move, expected_cost_pct=expected_cost,
            contributing_strategies=[s.strategy for s in agreeing],
        )
    return out

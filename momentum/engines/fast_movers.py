"""FAST MOVERS ENGINE (V1.1 mission 4). A dynamic "what's moving right now"
ranking - not a 24h-performance leaderboard. Combines very short return
horizons with the already-computed engine outputs into one FAST_SCORE for
display/promotion visibility. This does NOT trigger trades on its own - the
master ranker + entry engine remain the only path to a shadow entry (see
mission 11): a high fast score with high exhaustion/late-entry risk still
gets penalized here exactly like everywhere else in this bot.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore, ExhaustionScore

RETURN_HORIZONS_S = (1, 3, 5, 10, 30, 60)
FULL_SCORE_RETURN_10S_PCT = 0.5  # a +0.5% move over 10s maps to a raw short-term score of 100


@dataclass(slots=True)
class FastMoverScore:
    direction: str  # "UP" or "DOWN"
    fast_score: float
    returns: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)


def compute(
    state: SymbolState,
    now: float,
    engine_scores: dict,
    exhaustion: ExhaustionScore,
    late_entry_risk: float,
    cross_result: EngineScore | None,
) -> FastMoverScore | None:
    returns = {f"{h}s": state.velocity_pct(now, h) for h in RETURN_HORIZONS_S}
    r10 = returns["10s"]
    if r10 is None:
        return None

    direction = "UP" if r10 >= 0 else "DOWN"
    attr = "up" if direction == "UP" else "down"

    short_term_score = min(100.0, abs(r10) / FULL_SCORE_RETURN_10S_PCT * 100.0)

    def _score(name: str) -> float:
        eng = engine_scores.get(name)
        return getattr(eng, attr, 0.0) if eng else 0.0

    cross_score = getattr(cross_result, attr, 0.0) if cross_result else 0.0
    risk = exhaustion.up_risk if direction == "UP" else exhaustion.down_risk

    composite = (
        short_term_score * 0.30
        + _score("velocity") * 0.15
        + _score("acceleration") * 0.15
        + _score("volume") * 0.10
        + _score("orderflow") * 0.10
        + _score("orderbook_imbalance") * 0.05
        + _score("volatility_expansion") * 0.05
        + cross_score * 0.10
    )
    # dampen (never boost) for exhaustion/late-entry risk - a fast mover that's
    # already exhausted or too-late-to-enter must not rank higher for it
    combined_risk = max(risk, late_entry_risk)
    composite *= max(0.3, 1.0 - combined_risk / 150.0)
    composite = max(0.0, min(100.0, composite))

    return FastMoverScore(
        direction=direction, fast_score=composite, returns=returns,
        details={"exhaustion_risk": risk, "late_entry_risk": late_entry_risk},
    )

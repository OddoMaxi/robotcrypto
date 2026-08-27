"""MASTER MOMENTUM RANKER (spec section 7).

Combines the independent engines into one MOMENTUM_CONFIDENCE per direction and
a classification bucket. No score->probability mapping is asserted here - "a
score of 90 is not a promise of 90% odds"; that mapping is left to the Digital
Twin's empirical outcome stats, computed later from observed results.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.engines.types import EngineScore, ExhaustionScore, RegimeContext

CLASSIFICATIONS_HIGH_TO_LOW = (
    "HIGH_CONVICTION", "STRONG", "CONFIRMED", "BUILDING", "WATCH", "IGNORE",
)

EXHAUSTION_CONFIDENCE_DAMPENING = 0.3  # fraction of exhaustion risk subtracted from confidence


@dataclass(slots=True)
class RankResult:
    direction: str  # "UP" or "DOWN"
    raw_confidence: float
    confidence: float
    exhaustion_risk: float
    classification: str
    contributions: dict = field(default_factory=dict)  # engine_name -> weighted contribution


def _classify(confidence: float, exhaustion_risk: float, ranker_cfg: dict) -> str:
    thresholds = ranker_cfg["classification_thresholds"]
    override_threshold = ranker_cfg["exhausted_override_threshold"]

    if exhaustion_risk >= override_threshold and confidence >= thresholds["watch"]:
        return "EXHAUSTED"

    if confidence >= thresholds["high_conviction"]:
        return "HIGH_CONVICTION"
    if confidence >= thresholds["strong"]:
        return "STRONG"
    if confidence >= thresholds["confirmed"]:
        return "CONFIRMED"
    if confidence >= thresholds["building"]:
        return "BUILDING"
    if confidence >= thresholds["watch"]:
        return "WATCH"
    return "IGNORE"


def _rank_direction(
    direction: str,
    engine_scores: dict[str, EngineScore],
    exhaustion_risk: float,
    bias: float,
    engine_weights: dict,
    ranker_cfg: dict,
) -> RankResult:
    contributions = {}
    raw = 0.0
    attr = "up" if direction == "UP" else "down"
    for name, weight in engine_weights.items():
        score = engine_scores.get(name)
        if score is None:
            continue
        val = getattr(score, attr)
        contribution = val * weight
        contributions[name] = contribution
        raw += contribution

    raw_confidence = max(0.0, min(100.0, raw * bias))
    confidence = max(0.0, min(100.0, raw_confidence - exhaustion_risk * EXHAUSTION_CONFIDENCE_DAMPENING))
    classification = _classify(confidence, exhaustion_risk, ranker_cfg)

    return RankResult(
        direction=direction,
        raw_confidence=raw_confidence,
        confidence=confidence,
        exhaustion_risk=exhaustion_risk,
        classification=classification,
        contributions=contributions,
    )


def rank(
    engine_scores: dict[str, EngineScore],
    exhaustion: ExhaustionScore,
    regime: RegimeContext,
    engine_weights: dict,
    ranker_cfg: dict,
) -> dict[str, RankResult]:
    up = _rank_direction("UP", engine_scores, exhaustion.up_risk, regime.bias_up, engine_weights, ranker_cfg)
    down = _rank_direction("DOWN", engine_scores, exhaustion.down_risk, regime.bias_down, engine_weights, ranker_cfg)
    return {"UP": up, "DOWN": down}

from momentum.engines.types import EngineScore, ExhaustionScore, RegimeContext
from momentum.ranker.master_ranker import rank

ENGINE_WEIGHTS = {
    "velocity": 0.15, "acceleration": 0.15, "volume": 0.15, "orderflow": 0.15,
    "orderbook_imbalance": 0.10, "breakout": 0.15, "multi_timeframe": 0.10,
    "volatility_expansion": 0.05,
}
RANKER_CFG = {
    "classification_thresholds": {
        "ignore": 40, "watch": 55, "building": 70, "confirmed": 82, "strong": 90, "high_conviction": 95,
    },
    "exhausted_override_threshold": 75,
}
NEUTRAL_REGIME = RegimeContext(None, None, None, None, 1.0, 1.0, 1.0)


def _all_engines(up: float, down: float) -> dict:
    return {name: EngineScore(up, down) for name in ENGINE_WEIGHTS}


def test_strong_up_move_classifies_high():
    scores = _all_engines(95.0, 0.0)
    exhaustion = ExhaustionScore(up_risk=5.0, down_risk=0.0)
    result = rank(scores, exhaustion, NEUTRAL_REGIME, ENGINE_WEIGHTS, RANKER_CFG)
    assert result["UP"].confidence > 80
    assert result["UP"].classification in ("STRONG", "HIGH_CONVICTION", "CONFIRMED")
    assert result["DOWN"].classification == "IGNORE"


def test_high_exhaustion_overrides_high_momentum():
    scores = _all_engines(95.0, 0.0)
    exhaustion = ExhaustionScore(up_risk=92.0, down_risk=0.0)
    result = rank(scores, exhaustion, NEUTRAL_REGIME, ENGINE_WEIGHTS, RANKER_CFG)
    assert result["UP"].classification == "EXHAUSTED"


def test_no_signal_is_ignore():
    scores = _all_engines(0.0, 0.0)
    exhaustion = ExhaustionScore(up_risk=0.0, down_risk=0.0)
    result = rank(scores, exhaustion, NEUTRAL_REGIME, ENGINE_WEIGHTS, RANKER_CFG)
    assert result["UP"].classification == "IGNORE"
    assert result["DOWN"].classification == "IGNORE"

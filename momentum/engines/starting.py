"""MOMENTUM STARTING ENGINE (V1.1 mission 3). Distinct from the master ranker:
this is a pattern-match over the *combination* that typically precedes a move,
not a re-weighted average of the same inputs. It answers "is this beginning to
happen" rather than "how strong is the move right now" - a symbol can score
low on the master ranker (move hasn't developed yet) while scoring high here.

STARTING_UP_SCORE / STARTING_DOWN_SCORE are the fraction of starting criteria
currently satisfied, 0-100 - never a probability of anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore

HEALTHY_SPREAD_BPS = 20.0
RESISTANCE_ATTACK_WINDOW_PCT = 0.3  # price within this % of local high/low counts as "attacking" it
CROSS_EXCHANGE_STARTING_FLOOR = 10.0


@dataclass(slots=True)
class StartingScore:
    up: float
    down: float
    details: dict = field(default_factory=dict)


def compute(state: SymbolState, now: float, engine_scores: dict, cross_result: EngineScore | None) -> StartingScore:
    v = engine_scores.get("velocity")
    a = engine_scores.get("acceleration")
    vol = engine_scores.get("volume")
    of = engine_scores.get("orderflow")
    ob = engine_scores.get("orderbook_imbalance")
    ve = engine_scores.get("volatility_expansion")
    bo = engine_scores.get("breakout")

    spread_bps = state.spread_bps_now()
    healthy_spread = spread_bps is not None and spread_bps < HEALTHY_SPREAD_BPS

    price = state.price_now()
    local_high = bo.details.get("local_high") if bo else None
    local_low = bo.details.get("local_low") if bo else None
    attacking_resistance = (
        price is not None and local_high and 0 <= (local_high - price) / local_high * 100 < RESISTANCE_ATTACK_WINDOW_PCT
    )
    attacking_support = (
        price is not None and local_low and 0 <= (price - local_low) / local_low * 100 < RESISTANCE_ATTACK_WINDOW_PCT
    )

    cross_up = cross_result.up if cross_result else 0.0
    cross_down = cross_result.down if cross_result else 0.0

    checks_up = {
        "velocity_building": bool(v and v.details.get("building_up")),
        "acceleration_positive": bool(a and a.up > 15),
        "volume_accelerating": bool(vol and vol.up > 15),
        "aggressive_buyers_rising": bool(of and of.details.get("persistence_up", 0) >= 2),
        "bid_imbalance_rising": bool(ob and ob.up > 15),
        "compression_then_expansion": bool(ve and ve.up > 15),
        "attacking_resistance": attacking_resistance,
        "healthy_spread": healthy_spread,
        "cross_exchange_starting": cross_up > CROSS_EXCHANGE_STARTING_FLOOR,
    }
    checks_down = {
        "velocity_building": bool(v and v.details.get("building_down")),
        "acceleration_positive": bool(a and a.down > 15),
        "volume_accelerating": bool(vol and vol.down > 15),
        "aggressive_sellers_rising": bool(of and of.details.get("persistence_down", 0) >= 2),
        "ask_imbalance_rising": bool(ob and ob.down > 15),
        "compression_then_expansion": bool(ve and ve.down > 15),
        "attacking_support": attacking_support,
        "healthy_spread": healthy_spread,
        "cross_exchange_starting": cross_down > CROSS_EXCHANGE_STARTING_FLOOR,
    }

    starting_up = sum(1 for v_ in checks_up.values() if v_) / len(checks_up) * 100.0
    starting_down = sum(1 for v_ in checks_down.values() if v_) / len(checks_down) * 100.0

    return StartingScore(
        up=starting_up, down=starting_down,
        details={"checks_up": checks_up, "checks_down": checks_down},
    )

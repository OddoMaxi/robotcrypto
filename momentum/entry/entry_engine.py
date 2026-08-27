"""ENTRY ENGINE (spec section 9).

Momentum confidence alone does not justify an entry. This scores the *quality*
of entering right now: spread, distance already run past the trigger level,
reward:risk net of fees, and liquidity depth. A move that is 95-confidence but
already 3% past its breakout level should not be bought at market.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.data.state import SymbolState
from momentum.engines.late_entry import LateEntryScore
from momentum.engines.types import EngineScore
from momentum.ranker.master_ranker import RankResult

_ENGINE_TO_ENTRY_TYPE = {
    "velocity": "EARLY_MOMENTUM",
    "acceleration": "EARLY_MOMENTUM",
    "breakout": "BREAKOUT",
    "volume": "VOLUME_EXPANSION",
    "volatility_expansion": "VOLUME_EXPANSION",
    "orderflow": "PULLBACK_CONTINUATION",
    "orderbook_imbalance": "PULLBACK_CONTINUATION",
    "multi_timeframe": "PULLBACK_CONTINUATION",
}

MIN_STOP_DISTANCE_PCT = 0.15
MIN_DEPTH_NOTIONAL_USD = 2000.0
# V1.1 mission 5: a new, additive gate - not a retuning of any existing threshold
# above. A fast mover that has already traveled too far from where its impulse
# started gets rejected as TOO_LATE, independent of its entry_quality score.
LATE_ENTRY_RISK_REJECT_THRESHOLD = 70.0


@dataclass(slots=True)
class EntryQuality:
    entry_quality: float
    entry_type: str
    distance_from_trigger_pct: float
    spread_bps: float
    stop_distance_pct: float
    target_distance_pct: float
    net_reward_risk: float
    reject_reason: str | None = None
    details: dict = field(default_factory=dict)


def _dominant_entry_type(rank_result: RankResult) -> str:
    if not rank_result.contributions:
        return "EARLY_MOMENTUM"
    dominant = max(rank_result.contributions, key=rank_result.contributions.get)
    return _ENGINE_TO_ENTRY_TYPE.get(dominant, "EARLY_MOMENTUM")


def _is_retest(distance_from_trigger_pct: float) -> bool:
    # small distance (price back near the trigger level after having broken it)
    return 0.0 <= distance_from_trigger_pct <= 0.15


def compute(
    direction: str,
    state: SymbolState,
    now: float,
    rank_result: RankResult,
    engine_scores: dict[str, EngineScore],
    entry_cfg: dict,
    taker_fee_bps: float,
    reward_risk_target_multiple: float,
    late: LateEntryScore,
) -> EntryQuality:
    price = state.price_now()
    spread_bps = state.spread_bps_now()
    if price is None or spread_bps is None:
        return EntryQuality(0.0, "EARLY_MOMENTUM", 0.0, 999.0, 0.0, 0.0, 0.0, reject_reason="no_market_data")

    vol_pct = state.realized_vol(now, 60) or 0.2
    stop_distance_pct = max(MIN_STOP_DISTANCE_PCT, vol_pct * 1.5)
    target_distance_pct = stop_distance_pct * reward_risk_target_multiple

    breakout_details = engine_scores.get("breakout").details if engine_scores.get("breakout") else {}
    trigger = breakout_details.get("local_high") if direction == "UP" else breakout_details.get("local_low")
    if trigger:
        distance_from_trigger_pct = (price - trigger) / trigger * 100.0 if direction == "UP" \
            else (trigger - price) / trigger * 100.0
    else:
        distance_from_trigger_pct = 0.0

    entry_type = _dominant_entry_type(rank_result)
    if entry_type == "BREAKOUT" and _is_retest(distance_from_trigger_pct):
        entry_type = "BREAKOUT_RETEST"

    fees_pct = (taker_fee_bps / 100.0) * 2  # round trip, bps->pct
    net_reward_pct = target_distance_pct - fees_pct
    net_reward_risk = net_reward_pct / stop_distance_pct if stop_distance_pct > 0 else 0.0

    max_distance = entry_cfg["max_distance_from_trigger_pct"]
    spread_score = max(0.0, min(100.0, 100.0 - spread_bps * 2))
    distance_score = max(0.0, min(100.0, 100.0 - (max(0.0, distance_from_trigger_pct) / max_distance) * 100.0))
    rr_score = max(0.0, min(100.0, net_reward_risk / reward_risk_target_multiple * 100.0))

    depth_side = state.avg_ask_depth(now, 10) if direction == "UP" else state.avg_bid_depth(now, 10)
    depth_notional = (depth_side or 0.0) * price
    depth_score = 100.0 if depth_notional >= MIN_DEPTH_NOTIONAL_USD else (depth_notional / MIN_DEPTH_NOTIONAL_USD) * 100.0

    late_entry_risk = late.up_risk if direction == "UP" else late.down_risk

    entry_quality = (spread_score + distance_score + rr_score + depth_score) / 4.0
    # dampen (never boost) entry quality for late-entry risk - same pattern as
    # exhaustion dampening ranker confidence, kept independent of it
    entry_quality *= max(0.3, 1.0 - late_entry_risk / 150.0)

    reject_reason = None
    if distance_from_trigger_pct > max_distance:
        reject_reason = "too_far_past_trigger"
    elif late_entry_risk >= LATE_ENTRY_RISK_REJECT_THRESHOLD:
        reject_reason = "too_late"
    elif entry_quality < entry_cfg["min_entry_quality"]:
        reject_reason = "entry_quality_below_threshold"

    return EntryQuality(
        entry_quality=entry_quality,
        entry_type=entry_type,
        distance_from_trigger_pct=distance_from_trigger_pct,
        spread_bps=spread_bps,
        stop_distance_pct=stop_distance_pct,
        target_distance_pct=target_distance_pct,
        net_reward_risk=net_reward_risk,
        reject_reason=reject_reason,
        details={
            "spread_score": spread_score, "distance_score": distance_score,
            "rr_score": rr_score, "depth_score": depth_score, "depth_notional": depth_notional,
            "trigger": trigger, "late_entry_risk": late_entry_risk,
        },
    )

"""CROSS-EXCHANGE CONFIRMATION ENGINE (spec missions 3-5).

For a symbol tracked on 2+ exchanges: independently scores each exchange's
direction/momentum/volume/order-flow, then measures how much they *agree* -
it does not average three prices into one signal. A move confirmed on every
exchange scores materially higher than the identical move seen on only one
(mission 5's ISOLATED_MOVE vs BROAD_MARKET_CONFIRMATION). Lead/lag between
exchanges (mission 4) is tracked as a per-symbol onset-time comparison and
persisted for later *statistical* aggregation - it is never asserted as a
causal "X always leads Y" relationship from a single observation.

Returns a standard EngineScore so it plugs into the master ranker exactly like
every other weighted engine (see ranker/master_ranker.py) - all the rich
CROSS_EXCHANGE_* fields live in `.details` and flow straight into the
signals ledger + dashboard trade-detail view without extra plumbing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from momentum.data.state import SymbolState
from momentum.engines import orderflow, velocity, volume as volume_engine
from momentum.engines.types import EngineScore

DIRECTION_NOISE_FLOOR_PCT = 0.03   # |velocity_10s| below this doesn't count as a "direction" at all
ONSET_THRESHOLD_PCT = 0.10         # |velocity_10s| that counts as "this exchange has started moving"
EPISODE_RESET_S = 20.0             # if an exchange stays below onset threshold this long, its episode ends


@dataclass
class _SymbolLeadLagState:
    direction: str | None = None
    onset_ts: dict[str, float] = field(default_factory=dict)
    below_since: dict[str, float] = field(default_factory=dict)


class LeadLagTracker:
    """Stateful, per-symbol onset-time tracker feeding mission 4. Kept separate
    from the stateless per-cycle scoring so the rest of cross_exchange.py stays
    easy to unit test with plain inputs."""

    def __init__(self):
        self._episodes: dict[str, _SymbolLeadLagState] = {}

    def update(self, symbol: str, velocities_10s: dict[str, float], now: float) -> dict:
        """Returns {'leading_exchange', 'lead_time_ms': {exchange: ms}, 'new_confirmation': exchange|None}"""
        active = {ex: v for ex, v in velocities_10s.items() if abs(v) >= ONSET_THRESHOLD_PCT}
        if not active:
            self._episodes.pop(symbol, None)
            return {"leading_exchange": None, "lead_time_ms": {}, "new_confirmation": None}

        dominant_dir = "UP" if sum(1 for v in active.values() if v > 0) >= sum(1 for v in active.values() if v < 0) else "DOWN"
        same_dir = {ex for ex, v in active.items() if (v > 0) == (dominant_dir == "UP")}

        ep = self._episodes.get(symbol)
        if ep is None or ep.direction != dominant_dir:
            ep = _SymbolLeadLagState(direction=dominant_dir)
            self._episodes[symbol] = ep

        new_confirmation = None
        for ex in same_dir:
            if ex not in ep.onset_ts:
                ep.onset_ts[ex] = now
                new_confirmation = ex
            ep.below_since.pop(ex, None)
        for ex in list(ep.onset_ts):
            if ex not in same_dir:
                ep.below_since.setdefault(ex, now)
                if now - ep.below_since[ex] > EPISODE_RESET_S:
                    ep.onset_ts.pop(ex, None)
                    ep.below_since.pop(ex, None)

        if not ep.onset_ts:
            return {"leading_exchange": None, "lead_time_ms": {}, "new_confirmation": None}

        leader = min(ep.onset_ts, key=ep.onset_ts.get)
        lead_time_ms = {ex: (ts - ep.onset_ts[leader]) * 1000.0 for ex, ts in ep.onset_ts.items()}
        return {"leading_exchange": leader, "lead_time_ms": lead_time_ms, "new_confirmation": new_confirmation}


def _direction(v: float | None) -> str:
    if v is None or abs(v) < DIRECTION_NOISE_FLOOR_PCT:
        return "FLAT"
    return "UP" if v > 0 else "DOWN"


def compute(
    symbol: str,
    states_by_exchange: dict[str, SymbolState],
    now: float,
    lead_lag_tracker: LeadLagTracker,
) -> EngineScore:
    if not states_by_exchange:
        return EngineScore(0.0, 0.0, {"reason": "no_exchange_data"})

    per_exchange: dict[str, dict] = {}
    velocities_10s: dict[str, float] = {}
    for ex, state in states_by_exchange.items():
        v_score = velocity.compute(state, now)
        of_score = orderflow.compute(state, now)
        vol_score = volume_engine.compute(state, now)
        v10 = state.velocity_pct(now, 10)
        if v10 is None:
            continue
        velocities_10s[ex] = v10
        per_exchange[ex] = {
            "direction": _direction(v10),
            "momentum_up": v_score.up,
            "momentum_down": v_score.down,
            "buy_ratio": of_score.details.get("ratios", {}).get(5),
            "volume_ratio": vol_score.details.get("ratio"),
        }

    n = len(per_exchange)
    if n == 0:
        return EngineScore(0.0, 0.0, {"reason": "no_valid_velocity"})

    if n == 1:
        # single exchange available - degrade gracefully (mission 14: 3->2->1),
        # but the ranker must know confirmation strength is minimal, not absent.
        only = next(iter(per_exchange.values()))
        up = only["momentum_up"] * 0.5
        down = only["momentum_down"] * 0.5
        return EngineScore(up, down, {
            "per_exchange": per_exchange, "n_exchanges": 1,
            "classification": "SINGLE_EXCHANGE_ONLY",
            "direction_agreement": None, "velocity_agreement": None,
            "volume_confirmation": None, "orderflow_confirmation": None,
            "leading_exchange": None, "lead_time_ms": {},
        })

    up_votes = sum(1 for d in per_exchange.values() if d["direction"] == "UP")
    down_votes = sum(1 for d in per_exchange.values() if d["direction"] == "DOWN")
    direction_agreement_up = up_votes / n
    direction_agreement_down = down_votes / n

    vels = list(velocities_10s.values())
    mean_v = sum(vels) / n
    spread = (max(vels) - min(vels)) if n > 1 else 0.0
    denom = max(abs(mean_v), DIRECTION_NOISE_FLOOR_PCT)
    velocity_agreement = max(0.0, 1.0 - min(1.0, spread / (denom * 4)))

    buy_ratios = [d["buy_ratio"] for d in per_exchange.values() if d["buy_ratio"] is not None]
    orderflow_confirmation = 0.0
    if buy_ratios:
        avg_buy_ratio = sum(buy_ratios) / len(buy_ratios)
        agree_count = sum(1 for r in buy_ratios if (r >= 0.5) == (avg_buy_ratio >= 0.5))
        orderflow_confirmation = agree_count / len(buy_ratios)

    vol_ratios = [d["volume_ratio"] for d in per_exchange.values() if d["volume_ratio"] is not None]
    volume_confirmation = 0.0
    if vol_ratios:
        rising = sum(1 for r in vol_ratios if r >= 1.2)
        volume_confirmation = rising / len(vol_ratios)

    lead_lag = lead_lag_tracker.update(symbol, velocities_10s, now)

    flat_votes = n - up_votes - down_votes
    is_broad_up = up_votes == n and orderflow_confirmation > 0.5 and volume_confirmation > 0.3
    is_broad_down = down_votes == n and orderflow_confirmation > 0.5 and volume_confirmation > 0.3
    # isolated = exactly one exchange moved and every other exchange is flat (not
    # merely "not the same direction" - an opposite-direction mover is disagreement
    # (MIXED), not isolation, and must not be scored as if the rest were quiet)
    is_isolated = (
        n >= 2
        and ((up_votes == 1 and down_votes == 0) or (down_votes == 1 and up_votes == 0))
        and flat_votes == n - 1
        and max(abs(v) for v in vels) > ONSET_THRESHOLD_PCT * 2
    )

    classification = "MIXED"
    if is_broad_up or is_broad_down:
        classification = "BROAD_MARKET_CONFIRMATION"
    elif is_isolated:
        classification = "ISOLATED_MOVE"

    base_confidence = (
        0.35 * max(direction_agreement_up, direction_agreement_down) * 100
        + 0.25 * velocity_agreement * 100
        + 0.20 * volume_confirmation * 100
        + 0.20 * orderflow_confirmation * 100
    )
    isolated_penalty = 0.5 if classification == "ISOLATED_MOVE" else 1.0
    confidence = base_confidence * isolated_penalty

    up = confidence if up_votes >= down_votes else confidence * 0.2
    down = confidence if down_votes > up_votes else confidence * 0.2

    return EngineScore(up, down, {
        "per_exchange": per_exchange,
        "n_exchanges": n,
        "direction_agreement_up": direction_agreement_up,
        "direction_agreement_down": direction_agreement_down,
        "velocity_agreement": velocity_agreement,
        "volume_confirmation": volume_confirmation,
        "orderflow_confirmation": orderflow_confirmation,
        "classification": classification,
        "leading_exchange": lead_lag["leading_exchange"],
        "lead_time_ms": lead_lag["lead_time_ms"],
        "new_confirmation": lead_lag["new_confirmation"],
    })

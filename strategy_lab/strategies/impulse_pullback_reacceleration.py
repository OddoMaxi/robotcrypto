"""IMPULSE -> PULLBACK -> RE-ACCELERATION (spec section 4, PRIORITY). A real
per-symbol/per-direction phase state machine, not a single-cycle score:

  PHASE A IMPULSE      - magnitude/velocity/acceleration/volume/flow/book
                          confirm a move is under way. Never enters here.
  PHASE B PULLBACK      - a *controlled* retracement (PULLBACK_RATIO within a
                          configured band) with healthy structure/volume/flow.
  PHASE C REACCELERATION - velocity/acceleration/flow/volume/book recovery plus
                          a micro-breakout of the pullback high/low. Only here
                          can a Shadow entry be proposed, and only if the
                          estimated net edge (after costs) is still positive.

UP and DOWN run through the identical mirrored logic - see `_direction_view`.
One tracker instance is shared across cycles (stateful by design); it is
strategy-local and never touches the baseline bot's state.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore
from strategy_lab.market_bus import MarketEvent
from strategy_lab.strategies.base import StrategySignal, estimate_round_trip_cost_pct, exhaustion_veto

NAME = "IMPULSE_PULLBACK_REACCELERATION"
IMPULSE_MIN_CONFIRMATIONS = 3          # of {velocity, acceleration, volume, flow, book} = 5
IMPULSE_MAX_AGE_S = 120.0              # give up waiting for a pullback after this long
PULLBACK_MAX_AGE_S = 90.0              # give up waiting for re-acceleration after this long
MIN_RETRACEMENT_PCT = 0.03             # noise floor before we call it a genuine pullback
IMPULSE_START_LOOKBACK_S = 60.0        # how far back we look to find where a just-detected impulse actually began


@dataclass
class _Episode:
    phase: str                 # IMPULSE | PULLBACK
    start_ts: float
    start_price: float
    peak_price: float          # most favorable price reached (impulse extreme)
    peak_ts: float
    pullback_start_ts: float | None = None
    pullback_extreme_price: float | None = None   # least favorable price during pullback


def _impulse_confirmations(v: EngineScore | None, a: EngineScore | None, vol: EngineScore | None,
                            of: EngineScore | None, ob: EngineScore | None, up: bool) -> int:
    def side(e):
        return (e.up if up else e.down) if e else 0.0
    return sum(1 for e in (v, a, vol, of, ob) if side(e) > 15)


class ImpulsePullbackReaccelerationTracker:
    def __init__(self, strategy_cfg: dict):
        self.cfg = strategy_cfg
        self._episodes: dict[str, _Episode] = {}  # key: f"{symbol}:{exchange}:{direction}"

    def _key(self, symbol: str, exchange: str, direction: str) -> str:
        return f"{symbol}:{exchange}:{direction}"

    def active_symbols(self) -> list[str]:
        """Symbols with a live IMPULSE/PULLBACK episode - these must keep
        getting a full evaluation pass every cycle even if their fast_score
        ranking has since faded, or a pullback thesis silently orphans."""
        return list({key.split(":")[0] for key in self._episodes})

    def compute(self, event: MarketEvent, primary_ex: str, state: SymbolState,
                engine_scores: dict[str, EngineScore], cross_result: EngineScore | None,
                exhaustion_risk: tuple[float, float], late_entry_risk: tuple[float, float],
                common_cfg: dict, taker_fee_bps: float, now: float) -> StrategySignal | None:
        price = state.price_now()
        if price is None:
            return None

        # evaluate both directions' episodes; return whichever produces the more
        # advanced/interesting signal this cycle (a REACCELERATION beats an IMPULSE)
        candidates = []
        for up in (True, False):
            sig = self._compute_direction(event, primary_ex, state, engine_scores, cross_result,
                                            exhaustion_risk, late_entry_risk, common_cfg, taker_fee_bps, now, up)
            if sig is not None:
                candidates.append(sig)
        if not candidates:
            return None
        phase_rank = {"REACCELERATION": 2, "PULLBACK": 1, "IMPULSE": 0}
        candidates.sort(key=lambda s: (phase_rank.get(s.phase, 0), s.score), reverse=True)
        return candidates[0]

    def _compute_direction(self, event, primary_ex, state, engine_scores, cross_result,
                            exhaustion_risk, late_entry_risk, common_cfg, taker_fee_bps, now, up: bool):
        direction = "UP" if up else "DOWN"
        key = self._key(event.symbol, primary_ex, direction)
        price = state.price_now()
        episode = self._episodes.get(key)

        v = engine_scores.get("velocity")
        a = engine_scores.get("acceleration")
        vol = engine_scores.get("volume")
        of = engine_scores.get("orderflow")
        ob = engine_scores.get("orderbook_imbalance")
        v10 = state.velocity_pct(now, 10) or 0.0

        if episode is None:
            confirmations = _impulse_confirmations(v, a, vol, of, ob, up)
            velocity_gate = v10 >= self.cfg["impulse_min_velocity_pct_10s"] if up \
                else v10 <= -self.cfg["impulse_min_velocity_pct_10s"]
            if not (velocity_gate and confirmations >= IMPULSE_MIN_CONFIRMATIONS):
                return None
            # this cycle is where the impulse was first *detected*, not necessarily
            # where it *started* (a 2s-cadence scan can only ever see it a little
            # late) - back-fill the real start from the recent extreme so
            # IMPULSE_MAGNITUDE/PULLBACK_RATIO aren't computed off a truncated leg
            start_price = (state.local_low(now, IMPULSE_START_LOOKBACK_S) if up
                           else state.local_high(now, IMPULSE_START_LOOKBACK_S)) or price
            episode = _Episode(phase="IMPULSE", start_ts=now, start_price=start_price, peak_price=price, peak_ts=now)
            self._episodes[key] = episode
            return self._impulse_signal(event, primary_ex, direction, price, confirmations, exhaustion_risk,
                                         late_entry_risk, up)

        # stale episode cleanup
        max_age = IMPULSE_MAX_AGE_S if episode.phase == "IMPULSE" else PULLBACK_MAX_AGE_S
        if now - episode.start_ts > max_age:
            del self._episodes[key]
            return None

        favorable = (price > episode.peak_price) if up else (price < episode.peak_price)
        if favorable:
            episode.peak_price = price
            episode.peak_ts = now

        if episode.phase == "IMPULSE":
            retracement_pct = ((episode.peak_price - price) / episode.peak_price * 100.0) if up \
                else ((price - episode.peak_price) / episode.peak_price * 100.0)
            if retracement_pct < MIN_RETRACEMENT_PCT:
                confirmations = _impulse_confirmations(v, a, vol, of, ob, up)
                return self._impulse_signal(event, primary_ex, direction, price, confirmations, exhaustion_risk,
                                             late_entry_risk, up)
            impulse_magnitude_pct = abs(episode.peak_price - episode.start_price) / episode.start_price * 100.0
            pullback_ratio = (retracement_pct / impulse_magnitude_pct) if impulse_magnitude_pct > 0 else 1.0
            if pullback_ratio > self.cfg["pullback_max_ratio"]:
                del self._episodes[key]  # thesis invalidated - gave back too much
                return None
            if pullback_ratio < self.cfg["pullback_min_ratio"]:
                confirmations = _impulse_confirmations(v, a, vol, of, ob, up)
                return self._impulse_signal(event, primary_ex, direction, price, confirmations, exhaustion_risk,
                                             late_entry_risk, up)
            episode.phase = "PULLBACK"
            episode.pullback_start_ts = now
            episode.pullback_extreme_price = price
            return self._pullback_signal(event, primary_ex, direction, price, episode, pullback_ratio,
                                          engine_scores, exhaustion_risk, late_entry_risk, up)

        # PHASE == PULLBACK
        broke_structure = (price < episode.start_price) if up else (price > episode.start_price)
        if broke_structure:
            del self._episodes[key]  # gave back the entire impulse - thesis dead
            return None
        # track the most adverse price reached so far during the pullback (used
        # both for PULLBACK_RATIO and to detect "recovering" in phase C below)
        more_adverse = (episode.pullback_extreme_price is None or price < episode.pullback_extreme_price) if up \
            else (episode.pullback_extreme_price is None or price > episode.pullback_extreme_price)
        if more_adverse:
            episode.pullback_extreme_price = price
        impulse_magnitude_pct = abs(episode.peak_price - episode.start_price) / episode.start_price * 100.0
        retracement_pct = abs(episode.peak_price - episode.pullback_extreme_price) / episode.peak_price * 100.0
        pullback_ratio = (retracement_pct / impulse_magnitude_pct) if impulse_magnitude_pct > 0 else 1.0

        recovered_extreme = episode.pullback_extreme_price
        micro_breakout = (price > episode.peak_price) if up else (price < episode.peak_price)
        recovery_confirmations = _impulse_confirmations(v, a, vol, of, ob, up)
        recovering = (price > recovered_extreme) if up else (price < recovered_extreme)

        checks = {
            "velocity_recovery": bool(v and (v.up if up else v.down) > 15),
            "acceleration_recovery": bool(a and (a.up if up else a.down) > 15),
            "flow_recovery": bool(of and (of.up if up else of.down) > 15),
            "volume_recovery": bool(vol and (vol.up if up else vol.down) > 15),
            "book_recovery": bool(ob and (ob.up if up else ob.down) > 15),
            "micro_breakout": micro_breakout,
            "recovering_from_extreme": recovering,
            "cross_exchange_confirms": bool(cross_result and (cross_result.up if up else cross_result.down) > 10.0),
        }
        reaccel_score = sum(1 for c in checks.values() if c) / len(checks) * 100.0

        if reaccel_score < self.cfg["reaccel_min_score"] or not recovering:
            return self._pullback_signal(event, primary_ex, direction, price, episode, pullback_ratio,
                                          engine_scores, exhaustion_risk, late_entry_risk, up)

        exh = exhaustion_risk[0] if up else exhaustion_risk[1]
        late = late_entry_risk[0] if up else late_entry_risk[1]
        signal = StrategySignal(
            strategy=NAME, symbol=event.symbol, exchange=primary_ex, direction=direction, price=price,
            score=reaccel_score, phase="REACCELERATION", exhaustion_risk=exh, late_entry_risk=late,
            details={"checks": checks, "pullback_ratio": pullback_ratio,
                     "impulse_magnitude_pct": impulse_magnitude_pct},
        )
        veto = exhaustion_veto(exh, late, common_cfg["exhaustion_veto"], common_cfg["late_entry_veto"])
        if veto:
            signal.reject_reason = veto
        else:
            # shallower pullback -> more confidence in continuation; dampened, never a promise
            continuation_factor = max(0.1, 1.0 - pullback_ratio)
            signal.expected_move_pct = impulse_magnitude_pct * continuation_factor * 0.5
            signal.expected_cost_pct = estimate_round_trip_cost_pct(state, taker_fee_bps, now)
            signal.accepted = signal.expected_net_edge_pct > common_cfg["min_net_edge_pct"]
            if not signal.accepted:
                signal.reject_reason = "net_edge_not_positive"

        del self._episodes[key]   # fired (or rejected) - require a fresh impulse before trying again
        return signal

    def _impulse_signal(self, event, primary_ex, direction, price, confirmations, exhaustion_risk,
                         late_entry_risk, up: bool) -> StrategySignal:
        exh = exhaustion_risk[0] if up else exhaustion_risk[1]
        late = late_entry_risk[0] if up else late_entry_risk[1]
        return StrategySignal(
            strategy=NAME, symbol=event.symbol, exchange=primary_ex, direction=direction, price=price,
            score=confirmations / 5.0 * 100.0, phase="IMPULSE", exhaustion_risk=exh, late_entry_risk=late,
            reject_reason="phase_a_no_entry", details={"confirmations": confirmations},
        )

    def _pullback_signal(self, event, primary_ex, direction, price, episode, pullback_ratio, engine_scores,
                          exhaustion_risk, late_entry_risk, up: bool) -> StrategySignal:
        exh = exhaustion_risk[0] if up else exhaustion_risk[1]
        late = late_entry_risk[0] if up else late_entry_risk[1]
        vol = engine_scores.get("volume")
        of = engine_scores.get("orderflow")
        # a healthy pullback shows fading (not expanding) volume and flow that
        # hasn't flipped hard against the original direction
        volume_declining = bool(vol and (vol.down if up else vol.up) < 20)
        flow_not_reversed = bool(of and (of.down if up else of.up) < 40)
        score = (int(volume_declining) + int(flow_not_reversed)) / 2.0 * 100.0
        return StrategySignal(
            strategy=NAME, symbol=event.symbol, exchange=primary_ex, direction=direction, price=price,
            score=score, phase="PULLBACK", exhaustion_risk=exh, late_entry_risk=late,
            reject_reason="phase_b_no_entry",
            details={"pullback_ratio": pullback_ratio, "volume_declining": volume_declining,
                     "flow_not_reversed": flow_not_reversed},
        )

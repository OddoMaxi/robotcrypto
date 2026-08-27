"""EARLY-MOVER DETECTOR (V1 missions 7-8, V1.1 missions 6-7). Distinct from the
Digital Twin: this fires on the *first* significant momentum anomaly for a
symbol+direction (T0), before it necessarily becomes a qualified signal or a
top gainer, and tracks forward returns, MFE/MAE (+ time-to-MFE/MAE and
time-to-+0.25%/+0.5%/+1%), the peak momentum score reached, and cross-exchange
propagation order captured at T0. EARLY_UP and EARLY_DOWN are tracked
identically (mission 8 symmetry); DOWN stays shadow-only throughout.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from momentum.data.state import StateStore
from momentum.shadow.ledger import Ledger

DEFAULT_COOLDOWN_S = 300.0  # don't re-fire the same symbol+direction more than once per 5min
THRESHOLD_TARGETS_PCT = {"time_to_025_s": 0.25, "time_to_050_s": 0.50, "time_to_100_s": 1.00}


@dataclass(slots=True)
class _Track:
    event_id: int
    symbol: str
    exchange: str
    direction: str
    t0: float
    ref_price: float
    remaining_horizons: list[int]
    max_confidence: float
    confirmation_price: float | None = None
    time_to_peak_s: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    time_to_mfe_s: float = 0.0
    time_to_mae_s: float = 0.0
    time_to_thresholds_s: dict = field(default_factory=dict)  # e.g. {"time_to_025_s": 4.2}


def _build_propagation_info(cx_details: dict, state, now: float) -> dict:
    """Uses the cross-exchange engine's onset timestamps (real, historical -
    not fabricated) to order exchanges by who confirmed first, and looks up
    the actual price at each onset from the retained price buffer."""
    onset_ts: dict = cx_details.get("onset_ts") or {}
    if len(onset_ts) < 2:
        return {}

    ordered = sorted(onset_ts.items(), key=lambda kv: kv[1])
    exchanges_in_order = [ex for ex, _ in ordered]
    leader_ts = ordered[0][1]
    second_ts = ordered[1][1] if len(ordered) > 1 else None
    third_ts = ordered[2][1] if len(ordered) > 2 else None

    price_at_leader = state.price_buf.value_at_or_before(leader_ts) if state else None
    price_at_second = state.price_buf.value_at_or_before(second_ts) if (state and second_ts) else None

    price_move_before = None
    if price_at_leader and price_at_second:
        price_move_before = (price_at_second - price_at_leader) / price_at_leader * 100.0

    return {
        "second_exchange": exchanges_in_order[1] if len(exchanges_in_order) > 1 else None,
        "third_exchange": exchanges_in_order[2] if len(exchanges_in_order) > 2 else None,
        "lead_time_ms": (second_ts - leader_ts) * 1000.0 if second_ts else None,
        "confirmation_delay_ms": (third_ts - second_ts) * 1000.0 if third_ts else None,
        "price_move_before_confirmation": price_move_before,
        "_confirmation_price": price_at_second,  # internal - used to compute price_move_after at finalize
    }


class EarlyMoverTracker:
    def __init__(self, horizons_s: list[int], ledger: Ledger, cooldown_s: float = DEFAULT_COOLDOWN_S):
        self.horizons_s = sorted(horizons_s)
        self.ledger = ledger
        self.cooldown_s = cooldown_s
        self._was_above: dict[tuple[str, str], bool] = {}
        self._last_registered: dict[tuple[str, str], float] = {}
        self._active: dict[int, _Track] = {}

    async def maybe_register(self, symbol: str, exchange: str, direction: str, price: float,
                              confidence: float, now: float, threshold: float, *,
                              starting_score: float | None = None, fast_score: float | None = None,
                              regime_label: str | None = None, cx_details: dict | None = None,
                              state=None) -> int | None:
        key = (symbol, direction)
        was_above = self._was_above.get(key, False)
        is_above = confidence >= threshold
        self._was_above[key] = is_above
        if not (is_above and not was_above):
            return None
        if now - self._last_registered.get(key, 0.0) < self.cooldown_s:
            return None

        self._last_registered[key] = now
        propagation = _build_propagation_info(cx_details or {}, state, now)

        event_id = await self.ledger.insert_early_mover_event(
            symbol=symbol, exchange=exchange, direction=direction, t0_price=price, t0_confidence=confidence,
            starting_score=starting_score, fast_score=fast_score, regime=regime_label,
            second_exchange=propagation.get("second_exchange"), third_exchange=propagation.get("third_exchange"),
            lead_time_ms=propagation.get("lead_time_ms"), confirmation_delay_ms=propagation.get("confirmation_delay_ms"),
            price_move_before_confirmation=propagation.get("price_move_before_confirmation"),
        )
        self._active[event_id] = _Track(
            event_id=event_id, symbol=symbol, exchange=exchange, direction=direction, t0=now,
            ref_price=price, remaining_horizons=list(self.horizons_s), max_confidence=confidence,
            confirmation_price=propagation.get("_confirmation_price"),
        )
        return event_id

    def update_confidence(self, symbol: str, direction: str, confidence: float, now: float) -> None:
        for tr in self._active.values():
            if tr.symbol == symbol and tr.direction == direction and confidence > tr.max_confidence:
                tr.max_confidence = confidence
                tr.time_to_peak_s = now - tr.t0

    async def tick(self, store: StateStore) -> None:
        now = time.time()
        finished = []
        for event_id, tr in self._active.items():
            state = store.get(tr.exchange, tr.symbol)
            price = state.price_now() if state else None
            if price is not None and tr.ref_price:
                sign = 1 if tr.direction == "UP" else -1
                pct = sign * (price - tr.ref_price) / tr.ref_price * 100.0
                elapsed = now - tr.t0

                if pct > tr.mfe_pct:
                    tr.mfe_pct = pct
                    tr.time_to_mfe_s = elapsed
                if pct < tr.mae_pct:
                    tr.mae_pct = pct
                    tr.time_to_mae_s = elapsed

                progress_update = {}
                for col, target_pct in THRESHOLD_TARGETS_PCT.items():
                    if col not in tr.time_to_thresholds_s and pct >= target_pct:
                        tr.time_to_thresholds_s[col] = elapsed
                        progress_update[col] = elapsed
                if progress_update:
                    await self.ledger.update_early_mover_progress(event_id, **progress_update)

                due = [h for h in tr.remaining_horizons if elapsed >= h]
                for h in due:
                    await self.ledger.insert_early_mover_return(event_id=event_id, horizon_s=h, pct_change=pct, ts=now)
                    tr.remaining_horizons.remove(h)

                if not tr.remaining_horizons and tr.confirmation_price:
                    price_move_after = (price - tr.confirmation_price) / tr.confirmation_price * 100.0
                    await self.ledger.update_early_mover_progress(
                        event_id, price_move_after_confirmation=price_move_after,
                    )

            if not tr.remaining_horizons:
                await self.ledger.finalize_early_mover_event(
                    event_id, max_confidence=tr.max_confidence, time_to_peak_s=tr.time_to_peak_s,
                    mfe_pct=tr.mfe_pct, mae_pct=tr.mae_pct,
                )
                finished.append(event_id)

        for event_id in finished:
            del self._active[event_id]

    @property
    def pending_count(self) -> int:
        return len(self._active)

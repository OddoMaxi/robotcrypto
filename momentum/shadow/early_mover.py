"""EARLY-MOVER DETECTOR (missions 7-8). Distinct from the Digital Twin: this
fires on the *first* significant momentum anomaly for a symbol+direction (T0),
before it necessarily becomes a qualified signal or a top gainer, and tracks
forward returns, MFE/MAE, the peak momentum score reached, and time-to-peak.
EARLY_UP and EARLY_DOWN are tracked identically (mission 8 symmetry); DOWN
stays shadow-only throughout, same as everywhere else in this bot.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from momentum.data.state import StateStore
from momentum.shadow.ledger import Ledger

DEFAULT_COOLDOWN_S = 300.0  # don't re-fire the same symbol+direction more than once per 5min


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
    time_to_peak_s: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0


class EarlyMoverTracker:
    def __init__(self, horizons_s: list[int], ledger: Ledger, cooldown_s: float = DEFAULT_COOLDOWN_S):
        self.horizons_s = sorted(horizons_s)
        self.ledger = ledger
        self.cooldown_s = cooldown_s
        self._was_above: dict[tuple[str, str], bool] = {}
        self._last_registered: dict[tuple[str, str], float] = {}
        self._active: dict[int, _Track] = {}

    async def maybe_register(self, symbol: str, exchange: str, direction: str, price: float,
                              confidence: float, now: float, threshold: float) -> int | None:
        key = (symbol, direction)
        was_above = self._was_above.get(key, False)
        is_above = confidence >= threshold
        self._was_above[key] = is_above
        if not (is_above and not was_above):
            return None
        if now - self._last_registered.get(key, 0.0) < self.cooldown_s:
            return None

        self._last_registered[key] = now
        event_id = await self.ledger.insert_early_mover_event(
            symbol=symbol, exchange=exchange, direction=direction, t0_price=price, t0_confidence=confidence,
        )
        self._active[event_id] = _Track(
            event_id=event_id, symbol=symbol, exchange=exchange, direction=direction, t0=now,
            ref_price=price, remaining_horizons=list(self.horizons_s), max_confidence=confidence,
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
                tr.mfe_pct = max(tr.mfe_pct, pct)
                tr.mae_pct = min(tr.mae_pct, pct)

                elapsed = now - tr.t0
                due = [h for h in tr.remaining_horizons if elapsed >= h]
                for h in due:
                    await self.ledger.insert_early_mover_return(event_id=event_id, horizon_s=h, pct_change=pct, ts=now)
                    tr.remaining_horizons.remove(h)

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

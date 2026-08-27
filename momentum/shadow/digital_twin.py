"""TICK-BY-TICK DIGITAL TWIN (spec section 14). For every signal - accepted,
rejected, UP or DOWN - track what price actually did afterward at fixed
horizons (5s..10min), recording MFE/MAE (direction-aware: favorable/adverse
relative to the signal's direction, not raw price direction). This is the raw
material for later empirically answering "what's P(+0.5% before -0.3%)?" -
V1 only records the observations; it does not yet compute those probabilities
(there isn't enough data on day one), but the KPI/report layer can once enough
signals have accumulated.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from momentum.data.state import StateStore
from momentum.shadow.ledger import Ledger


@dataclass(slots=True)
class _Tracker:
    signal_id: int
    symbol: str
    exchange: str
    direction: str
    ref_price: float
    start_ts: float
    remaining_horizons: list[int]
    mfe_pct: float = 0.0
    mae_pct: float = 0.0


class DigitalTwin:
    def __init__(self, horizons_s: list[int], ledger: Ledger):
        self.horizons_s = sorted(horizons_s)
        self.ledger = ledger
        self._trackers: list[_Tracker] = []

    def track(self, signal_id: int, symbol: str, exchange: str, direction: str, ref_price: float) -> None:
        self._trackers.append(
            _Tracker(
                signal_id=signal_id, symbol=symbol, exchange=exchange, direction=direction,
                ref_price=ref_price, start_ts=time.time(), remaining_horizons=list(self.horizons_s),
            )
        )

    async def tick(self, store: StateStore) -> None:
        now = time.time()
        still_pending: list[_Tracker] = []
        for t in self._trackers:
            state = store.get(t.exchange, t.symbol)
            price = state.price_now() if state else None
            if price is not None and t.ref_price:
                sign = 1 if t.direction == "UP" else -1
                pct_change = sign * (price - t.ref_price) / t.ref_price * 100.0
                t.mfe_pct = max(t.mfe_pct, pct_change)
                t.mae_pct = min(t.mae_pct, pct_change)

                elapsed = now - t.start_ts
                due = [h for h in t.remaining_horizons if elapsed >= h]
                for h in due:
                    await self.ledger.insert_twin_snapshot(
                        signal_id=t.signal_id, horizon_s=h, price=price, pct_change=pct_change,
                        mfe_pct=t.mfe_pct, mae_pct=t.mae_pct, ts=now,
                    )
                    t.remaining_horizons.remove(h)

            if t.remaining_horizons:
                still_pending.append(t)
        self._trackers = still_pending

    @property
    def pending_count(self) -> int:
        return len(self._trackers)

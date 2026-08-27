"""MARKET EVENT BUS -> NORMALIZED MULTI-EXCHANGE SNAPSHOT (spec section 2).

One MarketEvent is built per symbol per Stage cycle and handed, as the exact
same object, to every strategy - "un meme MARKET_EVENT_ID doit etre evalue par
toutes les strategies". Reuses momentum.data.normalized.build_snapshot (a pure
function, imported read-only) so the Lab's snapshot shape matches the proven
baseline definition instead of re-deriving a second one.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from momentum.data.normalized import NormalizedMarketSnapshot, build_snapshot
from momentum.data.state import SymbolState, StateStore

_counter = itertools.count(1)


@dataclass(slots=True)
class MarketEvent:
    market_event_id: str
    symbol: str
    ts: float
    states_by_exchange: dict[str, SymbolState]
    snapshots_by_exchange: dict[str, NormalizedMarketSnapshot] = field(default_factory=dict)

    def primary(self, priority: tuple[str, ...]) -> tuple[str, SymbolState] | tuple[None, None]:
        for ex in priority:
            st = self.states_by_exchange.get(ex)
            if st is not None and st.price_now() is not None:
                return ex, st
        return None, None


def build_market_event(symbol: str, states_by_exchange: dict[str, SymbolState], now: float) -> MarketEvent:
    snapshots = {}
    for ex, state in states_by_exchange.items():
        snap = build_snapshot(ex, symbol, state, now)
        if snap is not None:
            snapshots[ex] = snap
    return MarketEvent(
        market_event_id=f"{symbol}:{next(_counter)}",
        symbol=symbol,
        ts=now,
        states_by_exchange=states_by_exchange,
        snapshots_by_exchange=snapshots,
    )

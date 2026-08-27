"""SHADOW BROKER (spec section 13). Simulates realistic fills off the REAL order
book: best ask for a BUY, best bid for a SELL/short-cover, walking through book
depth levels for size, real taker fees, and a simulated execution latency that
fills against a slightly-stale (delayed) book snapshot rather than the instant
top-of-book.

SAFETY: this module has NO method that sends anything to an exchange. It only
ever reads from `SymbolState` (already-received public market data) and returns
a computed `FillResult`. There is no `place_order`, `submit_order`, `withdraw`,
or `transfer` anywhere in this file or the rest of the shadow/ tree - see
tests/safety/test_isolation.py.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from momentum.data.state import SymbolState
from momentum.exchanges.base import SymbolFilter


@dataclass(slots=True)
class FillResult:
    avg_price: float
    filled_size: float
    fee: float
    slippage_pct: float
    latency_ms: float


class ShadowBroker:
    def __init__(self, shadow_cfg: dict):
        self.fee_by_exchange: dict[str, float] = shadow_cfg["taker_fee_bps_by_exchange"]
        self.latency_range_ms = shadow_cfg["simulated_latency_ms"]

    def taker_fee_bps(self, exchange: str) -> float:
        return self.fee_by_exchange.get(exchange, 10.0)

    def _simulated_latency_ms(self) -> float:
        lo, hi = self.latency_range_ms
        return random.uniform(lo, hi)

    @staticmethod
    def _walk_book(levels: list[tuple[float, float]], size: float) -> tuple[float, float]:
        remaining = size
        cost = 0.0
        filled = 0.0
        for price, qty in levels:
            take = min(remaining, qty)
            if take <= 0:
                continue
            cost += take * price
            filled += take
            remaining -= take
            if remaining <= 1e-12:
                break
        if filled <= 0:
            return (levels[0][0] if levels else 0.0, 0.0)
        return cost / filled, filled

    def _fill(self, state: SymbolState, side: str, size: float, exchange: str) -> FillResult | None:
        """side: 'buy' walks the ask side, 'sell' walks the bid side."""
        latency_ms = self._simulated_latency_ms()
        snap = state.book_state_delayed(latency_ms)
        if snap is None or snap.book_ticker is None:
            return None

        if side == "buy":
            levels = list(snap.depth.asks) if snap.depth else []
            best = snap.book_ticker.best_ask
            if not levels:
                levels = [(best, snap.book_ticker.best_ask_qty)]
        else:
            levels = list(snap.depth.bids) if snap.depth else []
            best = snap.book_ticker.best_bid
            if not levels:
                levels = [(best, snap.book_ticker.best_bid_qty)]

        avg_price, filled = self._walk_book(levels, size)
        if filled <= 0 or best <= 0:
            return None

        slippage_pct = abs(avg_price - best) / best * 100.0
        fee = avg_price * filled * (self.taker_fee_bps(exchange) / 10_000)
        return FillResult(avg_price=avg_price, filled_size=filled, fee=fee, slippage_pct=slippage_pct, latency_ms=latency_ms)

    def simulate_entry(self, state: SymbolState, direction: str, size: float, exchange: str) -> FillResult | None:
        # UP -> long, buy at ask. DOWN -> shadow short, "sell" (short) at bid.
        return self._fill(state, "buy" if direction == "UP" else "sell", size, exchange)

    def simulate_exit(self, state: SymbolState, direction: str, size: float, exchange: str) -> FillResult | None:
        # closing a long -> sell at bid. closing a shadow short -> buy-to-cover at ask.
        return self._fill(state, "sell" if direction == "UP" else "buy", size, exchange)

    @staticmethod
    def apply_filters(sym_filter: SymbolFilter | None, size: float, price: float) -> float | None:
        """Round to step_size and enforce min_notional, like a real exchange would
        reject an order. Returns adjusted size, or None if it can't pass filters."""
        if sym_filter is None:
            return size
        step = sym_filter.step_size
        if step and step > 0:
            size = round(round(size / step) * step, 10)
        if size <= 0:
            return None
        if sym_filter.min_notional and size * price < sym_filter.min_notional:
            return None
        return size

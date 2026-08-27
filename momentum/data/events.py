"""Market data event types. Every event carries a precise (float, seconds since epoch,
sub-ms resolution via time.time()) timestamp so downstream engines can compute exact
horizon lookups rather than approximating from candle boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Trade:
    symbol: str
    ts: float          # local receive time, time.time()
    exch_ts: float      # exchange-reported trade time (ms -> s)
    price: float
    qty: float
    is_buyer_maker: bool  # True => taker was a seller (aggressive sell), False => aggressive buy


@dataclass(slots=True)
class BookTicker:
    symbol: str
    ts: float
    best_bid: float
    best_bid_qty: float
    best_ask: float
    best_ask_qty: float

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_bps(self) -> float:
        if self.mid <= 0:
            return 0.0
        return (self.best_ask - self.best_bid) / self.mid * 10_000


@dataclass(slots=True)
class DepthSnapshot:
    symbol: str
    ts: float
    bids: list[tuple[float, float]]  # [(price, qty), ...] best-first, top-5
    asks: list[tuple[float, float]]

    @property
    def bid_depth(self) -> float:
        return sum(qty for _, qty in self.bids)

    @property
    def ask_depth(self) -> float:
        return sum(qty for _, qty in self.asks)

    @property
    def imbalance(self) -> float:
        total = self.bid_depth + self.ask_depth
        if total <= 0:
            return 0.0
        return (self.bid_depth - self.ask_depth) / total

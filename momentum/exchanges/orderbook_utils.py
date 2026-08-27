"""Local order-book reconstruction for exchanges (Bybit) that push an initial
snapshot followed by incremental deltas, rather than a full top-N snapshot on
every update (Binance's depth5, OKX's books5 need no merging at all)."""
from __future__ import annotations


class LocalOrderBook:
    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply_snapshot(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
        self.bids = {p: q for p, q in bids if q > 0}
        self.asks = {p: q for p, q in asks if q > 0}

    def apply_delta(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
        for p, q in bids:
            if q <= 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = q
        for p, q in asks:
            if q <= 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = q

    def top(self, n: int = 5) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        bids = sorted(self.bids.items(), key=lambda x: -x[0])[:n]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:n]
        return bids, asks

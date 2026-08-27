"""Per-symbol rolling state built from the live event stream. This is the single
source of truth every engine reads from - engines never touch the network layer
directly.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from momentum.data.events import BookTicker, DepthSnapshot, Trade
from momentum.data.windows import TimeSeriesBuffer

MAX_HORIZON_S = 900.0  # 15m - longest horizon we ever look back
BOOK_HISTORY_MAX_S = 3.0  # generous vs simulated_latency_ms config


@dataclass
class BookSnap:
    ts: float
    book_ticker: BookTicker | None
    depth: DepthSnapshot | None


class SymbolState:
    def __init__(self, symbol: str, exchange: str):
        self.symbol = symbol
        self.exchange = exchange

        self.price_buf = TimeSeriesBuffer(MAX_HORIZON_S + 5)
        self.buy_vol_buf = TimeSeriesBuffer(MAX_HORIZON_S + 5)
        self.sell_vol_buf = TimeSeriesBuffer(MAX_HORIZON_S + 5)
        self.notional_buf = TimeSeriesBuffer(MAX_HORIZON_S + 5)  # price*qty, for VWAP
        self.qty_buf = TimeSeriesBuffer(MAX_HORIZON_S + 5)        # qty alone, for VWAP denom
        self.spread_buf = TimeSeriesBuffer(MAX_HORIZON_S + 5)
        self.imbalance_buf = TimeSeriesBuffer(MAX_HORIZON_S + 5)
        self.bid_depth_buf = TimeSeriesBuffer(MAX_HORIZON_S + 5)
        self.ask_depth_buf = TimeSeriesBuffer(MAX_HORIZON_S + 5)

        self.latest_book_ticker: BookTicker | None = None
        self.latest_depth: DepthSnapshot | None = None
        self._book_history: deque[BookSnap] = deque()

        self.last_update_ts: float = 0.0
        self.trade_count: int = 0

    # -- ingestion -----------------------------------------------------
    def on_trade(self, trade: Trade) -> None:
        self.price_buf.append(trade.ts, trade.price)
        notional = trade.price * trade.qty
        self.notional_buf.append(trade.ts, notional)
        self.qty_buf.append(trade.ts, trade.qty)
        if trade.is_buyer_maker:
            # taker was the seller -> aggressive sell
            self.sell_vol_buf.append(trade.ts, trade.qty)
        else:
            self.buy_vol_buf.append(trade.ts, trade.qty)
        self.last_update_ts = trade.ts
        self.trade_count += 1

    def on_book_ticker(self, bt: BookTicker) -> None:
        self.latest_book_ticker = bt
        self.spread_buf.append(bt.ts, bt.spread_bps)
        self.last_update_ts = bt.ts
        self._push_book_history(bt.ts)

    def on_depth(self, depth: DepthSnapshot) -> None:
        self.latest_depth = depth
        self.imbalance_buf.append(depth.ts, depth.imbalance)
        self.bid_depth_buf.append(depth.ts, depth.bid_depth)
        self.ask_depth_buf.append(depth.ts, depth.ask_depth)
        self.last_update_ts = depth.ts
        self._push_book_history(depth.ts)

    def _push_book_history(self, ts: float) -> None:
        # only record "complete" snapshots (both sides known) so a delayed-fill
        # lookup can never land on a partially-initialized snapshot
        if self.latest_book_ticker is None or self.latest_depth is None:
            return
        self._book_history.append(
            BookSnap(ts=ts, book_ticker=self.latest_book_ticker, depth=self.latest_depth)
        )
        cutoff = ts - BOOK_HISTORY_MAX_S
        while self._book_history and self._book_history[0].ts < cutoff:
            self._book_history.popleft()

    # -- queries ---------------------------------------------------------
    def price_now(self) -> float | None:
        if self.latest_book_ticker is not None:
            return self.latest_book_ticker.mid
        return self.price_buf.latest()

    def velocity_pct(self, now: float, horizon_s: float) -> float | None:
        p_now = self.price_now()
        p_then = self.price_buf.value_n_seconds_ago(now, horizon_s)
        if p_now is None or p_then is None or p_then == 0:
            return None
        return (p_now - p_then) / p_then * 100.0

    def buy_volume(self, now: float, horizon_s: float) -> float:
        return self.buy_vol_buf.sum_since(now, horizon_s)

    def sell_volume(self, now: float, horizon_s: float) -> float:
        return self.sell_vol_buf.sum_since(now, horizon_s)

    def total_volume(self, now: float, horizon_s: float) -> float:
        return self.buy_volume(now, horizon_s) + self.sell_volume(now, horizon_s)

    def vwap(self, now: float, horizon_s: float) -> float | None:
        notional = self.notional_buf.sum_since(now, horizon_s)
        qty = self.qty_buf.sum_since(now, horizon_s)
        if qty <= 0:
            return None
        return notional / qty

    def local_high(self, now: float, horizon_s: float) -> float | None:
        return self.price_buf.max_since(now, horizon_s)

    def local_low(self, now: float, horizon_s: float) -> float | None:
        return self.price_buf.min_since(now, horizon_s)

    def realized_vol(self, now: float, horizon_s: float) -> float | None:
        """Stdev of trade prices over the window, expressed as % of mean price."""
        pts = [p for p in self.price_buf.window(now, horizon_s)]
        if len(pts) < 3:
            return None
        vals = [p.value for p in pts]
        mean = sum(vals) / len(vals)
        if mean == 0:
            return None
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        return (var ** 0.5) / mean * 100.0

    def spread_bps_now(self) -> float | None:
        return self.spread_buf.latest()

    def avg_spread_bps(self, now: float, horizon_s: float) -> float | None:
        return self.spread_buf.avg_since(now, horizon_s)

    def imbalance_now(self) -> float | None:
        return self.imbalance_buf.latest()

    def avg_bid_depth(self, now: float, horizon_s: float) -> float | None:
        return self.bid_depth_buf.avg_since(now, horizon_s)

    def avg_ask_depth(self, now: float, horizon_s: float) -> float | None:
        return self.ask_depth_buf.avg_since(now, horizon_s)

    def book_state_delayed(self, latency_ms: float) -> BookSnap | None:
        """Return the book snapshot as it existed ~latency_ms in the past, for
        realistic (slightly stale) shadow fills. Falls back to the most recent
        snapshot if history doesn't go back far enough."""
        if not self._book_history:
            return None
        target = time.time() - (latency_ms / 1000.0)
        return min(self._book_history, key=lambda snap: abs(snap.ts - target))

    def is_stale(self, now: float, max_age_s: float = 30.0) -> bool:
        return (now - self.last_update_ts) > max_age_s if self.last_update_ts else True


class StateStore:
    """Holds every symbol's SymbolState, keyed by (exchange, symbol)."""

    def __init__(self):
        self._states: dict[tuple[str, str], SymbolState] = {}

    def get_or_create(self, exchange: str, symbol: str) -> SymbolState:
        key = (exchange, symbol)
        if key not in self._states:
            self._states[key] = SymbolState(symbol, exchange)
        return self._states[key]

    def get(self, exchange: str, symbol: str) -> SymbolState | None:
        return self._states.get((exchange, symbol))

    def all(self) -> list[SymbolState]:
        return list(self._states.values())

    def symbols(self, exchange: str) -> list[str]:
        return [s for (ex, s) in self._states if ex == exchange]

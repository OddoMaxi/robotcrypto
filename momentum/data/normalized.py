"""NormalizedMarketSnapshot (mission 2): the common read-model every downstream
consumer (cross-exchange engine, exchange-quality engine, dashboard) queries
instead of reaching into exchange-specific fields. The real normalization
already happens one layer down - every adapter (Binance/Bybit/OKX) translates
its native WS payloads into the same Trade/BookTicker/DepthSnapshot events and
the same canonical "BASEQUOTE" symbol string, so no exchange gets its data
reshaped to manufacture more signals than another.
"""
from __future__ import annotations

from dataclasses import dataclass

from momentum.data.state import SymbolState


@dataclass(slots=True)
class NormalizedMarketSnapshot:
    exchange: str
    symbol: str
    timestamp_exchange: float | None
    timestamp_local: float
    bid: float | None
    ask: float | None
    mid: float | None
    spread_bps: float | None
    last_price: float | None
    volume: float           # base-asset volume, trailing 60s
    quote_volume: float      # quote-asset volume, trailing 60s
    trades: int               # trade count, trailing 60s
    best_bid_qty: float | None
    best_ask_qty: float | None
    orderbook_depth: float     # bid_depth + ask_depth, top-N levels
    book_imbalance: float | None
    aggressive_buy_volume: float   # trailing 60s
    aggressive_sell_volume: float  # trailing 60s
    price_change_pct: float | None  # trailing 60s velocity, see velocity engine for finer horizons


VOLUME_WINDOW_S = 60.0


def build_snapshot(exchange: str, symbol: str, state: SymbolState, now: float) -> NormalizedMarketSnapshot | None:
    price = state.price_now()
    if price is None:
        return None

    bt = state.latest_book_ticker
    depth = state.latest_depth
    buy_vol = state.buy_volume(now, VOLUME_WINDOW_S)
    sell_vol = state.sell_volume(now, VOLUME_WINDOW_S)
    notional = state.notional_buf.sum_since(now, VOLUME_WINDOW_S)
    trade_count = len(state.price_buf.window(now, VOLUME_WINDOW_S))

    return NormalizedMarketSnapshot(
        exchange=exchange,
        symbol=symbol,
        timestamp_exchange=None,  # per-trade exch_ts isn't retained at the state level; local ts is authoritative here
        timestamp_local=now,
        bid=bt.best_bid if bt else None,
        ask=bt.best_ask if bt else None,
        mid=bt.mid if bt else price,
        spread_bps=bt.spread_bps if bt else None,
        last_price=price,
        volume=buy_vol + sell_vol,
        quote_volume=notional,
        trades=trade_count,
        best_bid_qty=bt.best_bid_qty if bt else None,
        best_ask_qty=bt.best_ask_qty if bt else None,
        orderbook_depth=(depth.bid_depth + depth.ask_depth) if depth else 0.0,
        book_imbalance=depth.imbalance if depth else None,
        aggressive_buy_volume=buy_vol,
        aggressive_sell_volume=sell_vol,
        price_change_pct=state.velocity_pct(now, VOLUME_WINDOW_S),
    )

"""Exchange normalization tests (mission 14): synthetic payloads matching each
exchange's documented WS format, verified against the common Trade/BookTicker/
DepthSnapshot events - without needing live network access (this sandbox's
network blocks the exchanges' main API domains, verified separately on the
deploy target)."""
import json

import pytest

from momentum.exchanges.bybit import BybitAdapter
from momentum.exchanges.okx import OkxAdapter
from momentum.exchanges.orderbook_utils import LocalOrderBook


class _Capture:
    def __init__(self):
        self.trades = []
        self.book_tickers = []
        self.depths = []

    async def on_trade(self, t):
        self.trades.append(t)

    async def on_book_ticker(self, bt):
        self.book_tickers.append(bt)

    async def on_depth(self, d):
        self.depths.append(d)


def test_okx_symbol_canonicalization_roundtrip():
    adapter = OkxAdapter(quote_asset="USDT")
    assert adapter._to_canonical("BTC-USDT") == "BTCUSDT"
    assert adapter._to_native("BTCUSDT") == "BTC-USDT"
    assert adapter._to_native(adapter._to_canonical("ETH-USDT")) == "ETH-USDT"


@pytest.mark.asyncio
async def test_okx_dispatch_trades_tickers_books5():
    adapter = OkxAdapter(quote_asset="USDT")
    cap = _Capture()

    trade_msg = json.dumps({
        "arg": {"channel": "trades", "instId": "BTC-USDT"},
        "data": [{"instId": "BTC-USDT", "tradeId": "1", "px": "100.5", "sz": "0.01", "side": "sell", "ts": "1000"}],
    })
    await adapter._dispatch(trade_msg, cap.on_trade, cap.on_book_ticker, cap.on_depth)
    assert len(cap.trades) == 1
    assert cap.trades[0].symbol == "BTCUSDT"
    assert cap.trades[0].is_buyer_maker is True  # side "sell" -> taker sold -> buyer was maker
    assert cap.trades[0].price == 100.5

    ticker_msg = json.dumps({
        "arg": {"channel": "tickers", "instId": "BTC-USDT"},
        "data": [{"instId": "BTC-USDT", "bidPx": "100.0", "bidSz": "2", "askPx": "100.2", "askSz": "3"}],
    })
    await adapter._dispatch(ticker_msg, cap.on_trade, cap.on_book_ticker, cap.on_depth)
    assert len(cap.book_tickers) == 1
    assert cap.book_tickers[0].symbol == "BTCUSDT"
    assert cap.book_tickers[0].best_bid == 100.0

    books_msg = json.dumps({
        "arg": {"channel": "books5", "instId": "BTC-USDT"},
        "data": [{"bids": [["100.0", "2", "0", "1"]], "asks": [["100.2", "3", "0", "1"]], "ts": "1000"}],
    })
    await adapter._dispatch(books_msg, cap.on_trade, cap.on_book_ticker, cap.on_depth)
    assert len(cap.depths) == 1
    assert cap.depths[0].symbol == "BTCUSDT"
    assert cap.depths[0].bids == [(100.0, 2.0)]


@pytest.mark.asyncio
async def test_bybit_dispatch_trade_and_ticker():
    adapter = BybitAdapter(quote_asset="USDT")
    cap = _Capture()

    trade_msg = json.dumps({
        "topic": "publicTrade.BTCUSDT",
        "data": [{"T": 1000, "s": "BTCUSDT", "S": "Buy", "v": "0.01", "p": "100.5"}],
    })
    await adapter._dispatch(trade_msg, cap.on_trade, cap.on_book_ticker, cap.on_depth)
    assert len(cap.trades) == 1
    assert cap.trades[0].is_buyer_maker is False  # side "Buy" -> aggressive buy

    ticker_msg = json.dumps({
        "topic": "tickers.BTCUSDT",
        "data": {"symbol": "BTCUSDT", "bid1Price": "100.0", "bid1Size": "1", "ask1Price": "100.2", "ask1Size": "2"},
    })
    await adapter._dispatch(ticker_msg, cap.on_trade, cap.on_book_ticker, cap.on_depth)
    assert cap.book_tickers[0].best_ask == 100.2

    non_market_msg = json.dumps({"success": True, "op": "subscribe"})
    await adapter._dispatch(non_market_msg, cap.on_trade, cap.on_book_ticker, cap.on_depth)
    assert len(cap.trades) == 1 and len(cap.book_tickers) == 1  # ack ignored, no crash


@pytest.mark.asyncio
async def test_bybit_orderbook_snapshot_then_delta():
    adapter = BybitAdapter(quote_asset="USDT")
    cap = _Capture()

    snapshot = json.dumps({
        "topic": "orderbook.50.BTCUSDT", "type": "snapshot",
        "data": {"s": "BTCUSDT", "b": [["100.0", "1"], ["99.9", "2"]], "a": [["100.1", "1"], ["100.2", "2"]]},
    })
    await adapter._dispatch(snapshot, cap.on_trade, cap.on_book_ticker, cap.on_depth)
    assert cap.depths[-1].bids[0] == (100.0, 1.0)

    delta = json.dumps({
        "topic": "orderbook.50.BTCUSDT", "type": "delta",
        "data": {"s": "BTCUSDT", "b": [["100.0", "0"]], "a": [["100.1", "5"]]},  # remove best bid, update best ask
    })
    await adapter._dispatch(delta, cap.on_trade, cap.on_book_ticker, cap.on_depth)
    bids, asks = cap.depths[-1].bids, cap.depths[-1].asks
    assert (100.0, 1.0) not in bids  # removed
    assert bids[0] == (99.9, 2.0)  # next best bid now on top
    assert asks[0] == (100.1, 5.0)  # updated qty


def test_local_order_book_merge_directly():
    book = LocalOrderBook()
    book.apply_snapshot(bids=[(10.0, 1.0), (9.0, 2.0)], asks=[(11.0, 1.0)])
    book.apply_delta(bids=[(10.0, 0.0)], asks=[(11.0, 3.0), (12.0, 1.0)])
    bids, asks = book.top(5)
    assert bids == [(9.0, 2.0)]
    assert asks == [(11.0, 3.0), (12.0, 1.0)]

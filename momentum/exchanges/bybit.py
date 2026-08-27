"""Bybit Spot adapter - PUBLIC market data only (see base.py SAFETY note).

Uses the public v5 REST (instruments-info/tickers, no auth) and public v5
WebSocket (publicTrade/tickers/orderbook.50, no auth, no API key ever read).
Symbols are canonicalized to Binance-style "BASEQUOTE" (Bybit already uses
that native format, so no translation is needed here, unlike OKX).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp
import websockets

from momentum.data.events import BookTicker, DepthSnapshot, Trade
from momentum.exchanges.base import BookTickerHandler, DepthHandler, SymbolFilter, TradeHandler
from momentum.exchanges.health import ExchangeHealth
from momentum.exchanges.orderbook_utils import LocalOrderBook

logger = logging.getLogger(__name__)

REST_BASE = "https://api.bybit.com"
WS_BASE = "wss://stream.bybit.com/v5/public/spot"
SUBSCRIBE_CHUNK = 10  # bybit's per-subscribe-request arg limit


class BybitAdapter:
    name = "bybit"

    def __init__(self, quote_asset: str = "USDT", ws_symbols_per_connection: int = 40,
                 health: ExchangeHealth | None = None):
        self.quote_asset = quote_asset
        self.ws_symbols_per_connection = ws_symbols_per_connection
        self.health = health or ExchangeHealth(exchange=self.name)
        self._books: dict[str, LocalOrderBook] = {}

    async def fetch_symbol_universe(self) -> list[SymbolFilter]:
        async with aiohttp.ClientSession() as session:
            instruments, tickers = await asyncio.gather(
                self._get_json(session, "/v5/market/instruments-info", {"category": "spot"}),
                self._get_json(session, "/v5/market/tickers", {"category": "spot"}),
            )

        ticker_by_symbol = {t["symbol"]: t for t in tickers["result"]["list"]}

        results: list[SymbolFilter] = []
        for s in instruments["result"]["list"]:
            if s.get("quoteCoin") != self.quote_asset:
                continue
            if s.get("status") != "Trading":
                continue
            lot = s.get("lotSizeFilter", {})
            price_filter = s.get("priceFilter", {})
            t = ticker_by_symbol.get(s["symbol"], {})
            try:
                quote_volume = float(t.get("turnover24h", 0.0))
            except (TypeError, ValueError):
                quote_volume = 0.0
            try:
                last_price = float(t.get("lastPrice", 0.0) or 0.0)
            except (TypeError, ValueError):
                last_price = 0.0

            results.append(
                SymbolFilter(
                    symbol=s["symbol"],
                    base_asset=s.get("baseCoin", ""),
                    quote_asset=s.get("quoteCoin", ""),
                    tick_size=float(price_filter.get("tickSize", 0) or 0),
                    step_size=float(lot.get("basePrecision", 0) or 0),
                    min_notional=float(lot.get("minOrderAmt", 0) or 0),
                    quote_volume_24h=quote_volume,
                    status=s.get("status", ""),
                    last_price=last_price,
                )
            )
        return results

    async def _get_json(self, session: aiohttp.ClientSession, path: str, params: dict) -> dict:
        async with session.get(REST_BASE + path, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def stream_market_data(
        self,
        symbols: list[str],
        on_trade: TradeHandler,
        on_book_ticker: BookTickerHandler,
        on_depth: DepthHandler,
    ) -> None:
        chunks = [
            symbols[i : i + self.ws_symbols_per_connection]
            for i in range(0, len(symbols), self.ws_symbols_per_connection)
        ]
        tasks = [
            asyncio.create_task(self._run_connection(chunk, on_trade, on_book_ticker, on_depth))
            for chunk in chunks
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    async def _run_connection(
        self,
        symbols: list[str],
        on_trade: TradeHandler,
        on_book_ticker: BookTickerHandler,
        on_depth: DepthHandler,
    ) -> None:
        args = []
        for sym in symbols:
            args += [f"publicTrade.{sym}", f"tickers.{sym}", f"orderbook.50.{sym}"]
        self.health.symbols_subscribed = max(self.health.symbols_subscribed, len(symbols))

        backoff = 1.0
        has_connected_before = False
        while True:
            t0 = self.health.on_connecting()
            try:
                async with websockets.connect(WS_BASE, ping_interval=20, ping_timeout=20) as ws:
                    for i in range(0, len(args), SUBSCRIBE_CHUNK):
                        await ws.send(json.dumps({"op": "subscribe", "args": args[i : i + SUBSCRIBE_CHUNK]}))
                    self.health.on_connected(t0, is_reconnect=has_connected_before)
                    has_connected_before = True
                    logger.info("bybit ws connected: %d symbols", len(symbols))
                    backoff = 1.0
                    async for raw in ws:
                        self.health.on_message()
                        await self._dispatch(raw, on_trade, on_book_ticker, on_depth)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health.on_disconnected()
                logger.exception("bybit ws connection dropped, reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _dispatch(
        self,
        raw: str,
        on_trade: TradeHandler,
        on_book_ticker: BookTickerHandler,
        on_depth: DepthHandler,
    ) -> None:
        msg = json.loads(raw)
        topic = msg.get("topic", "")
        if not topic:
            return  # subscribe ack / pong, not market data
        now = time.time()

        if topic.startswith("publicTrade."):
            for t in msg.get("data", []):
                trade = Trade(
                    symbol=t["s"], ts=now, exch_ts=float(t["T"]) / 1000.0,
                    price=float(t["p"]), qty=float(t["v"]), is_buyer_maker=(t["S"] == "Sell"),
                )
                await on_trade(trade)

        elif topic.startswith("tickers."):
            d = msg.get("data", {})
            symbol = d.get("symbol")
            bid = d.get("bid1Price")
            ask = d.get("ask1Price")
            if symbol and bid and ask:
                bt = BookTicker(
                    symbol=symbol, ts=now, best_bid=float(bid),
                    best_bid_qty=float(d.get("bid1Size", 0) or 0), best_ask=float(ask),
                    best_ask_qty=float(d.get("ask1Size", 0) or 0),
                )
                await on_book_ticker(bt)

        elif topic.startswith("orderbook."):
            d = msg.get("data", {})
            symbol = d.get("s")
            if not symbol:
                return
            book = self._books.setdefault(symbol, LocalOrderBook())
            bids = [(float(p), float(q)) for p, q in d.get("b", [])]
            asks = [(float(p), float(q)) for p, q in d.get("a", [])]
            if msg.get("type") == "snapshot":
                book.apply_snapshot(bids, asks)
            else:
                book.apply_delta(bids, asks)
            top_bids, top_asks = book.top(5)
            await on_depth(DepthSnapshot(symbol=symbol, ts=now, bids=top_bids, asks=top_asks))

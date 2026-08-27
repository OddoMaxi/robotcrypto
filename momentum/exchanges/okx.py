"""OKX Spot adapter - PUBLIC market data only (see base.py SAFETY note).

Uses the public v5 REST (instruments/tickers, no auth) and public v5
WebSocket (trades/tickers/books5, no auth, no API key ever read). OKX's
native symbol format is "BASE-QUOTE" (e.g. "BTC-USDT"); this adapter
canonicalizes to the Binance/Bybit-style "BASEQUOTE" so the rest of the
system (StateStore, cross-exchange matching) can key on one common symbol
string regardless of exchange.
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

logger = logging.getLogger(__name__)

REST_BASE = "https://www.okx.com"
WS_BASE = "wss://ws.okx.com:8443/ws/v5/public"


class OkxAdapter:
    name = "okx"

    def __init__(self, quote_asset: str = "USDT", ws_symbols_per_connection: int = 40,
                 health: ExchangeHealth | None = None):
        self.quote_asset = quote_asset
        self.ws_symbols_per_connection = ws_symbols_per_connection
        self.health = health or ExchangeHealth(exchange=self.name)

    def _to_canonical(self, inst_id: str) -> str:
        return inst_id.replace("-", "")

    def _to_native(self, canonical: str) -> str:
        base = canonical[: -len(self.quote_asset)]
        return f"{base}-{self.quote_asset}"

    async def fetch_symbol_universe(self) -> list[SymbolFilter]:
        async with aiohttp.ClientSession() as session:
            instruments, tickers = await asyncio.gather(
                self._get_json(session, "/api/v5/public/instruments", {"instType": "SPOT"}),
                self._get_json(session, "/api/v5/market/tickers", {"instType": "SPOT"}),
            )

        ticker_by_inst = {t["instId"]: t for t in tickers["data"]}

        results: list[SymbolFilter] = []
        for s in instruments["data"]:
            if s.get("quoteCcy") != self.quote_asset:
                continue
            if s.get("state") != "live":
                continue
            t = ticker_by_inst.get(s["instId"], {})
            try:
                quote_volume = float(t.get("volCcy24h", 0.0))
                last_price = float(t.get("last", 0.0) or 0.0)
            except (TypeError, ValueError):
                quote_volume, last_price = 0.0, 0.0
            min_sz = float(s.get("minSz", 0) or 0)

            results.append(
                SymbolFilter(
                    symbol=self._to_canonical(s["instId"]),
                    base_asset=s.get("baseCcy", ""),
                    quote_asset=s.get("quoteCcy", ""),
                    tick_size=float(s.get("tickSz", 0) or 0),
                    step_size=float(s.get("lotSz", 0) or 0),
                    min_notional=min_sz * last_price,  # OKX gives min size in base ccy, not quote
                    quote_volume_24h=quote_volume,
                    status=s.get("state", ""),
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
            inst_id = self._to_native(sym)
            args += [
                {"channel": "trades", "instId": inst_id},
                {"channel": "tickers", "instId": inst_id},
                {"channel": "books5", "instId": inst_id},
            ]
        self.health.symbols_subscribed = max(self.health.symbols_subscribed, len(symbols))

        backoff = 1.0
        has_connected_before = False
        while True:
            t0 = self.health.on_connecting()
            try:
                async with websockets.connect(WS_BASE, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    self.health.on_connected(t0, is_reconnect=has_connected_before)
                    has_connected_before = True
                    logger.info("okx ws connected: %d symbols", len(symbols))
                    backoff = 1.0
                    async for raw in ws:
                        self.health.on_message()
                        await self._dispatch(raw, on_trade, on_book_ticker, on_depth)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health.on_disconnected()
                logger.exception("okx ws connection dropped, reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _dispatch(
        self,
        raw: str,
        on_trade: TradeHandler,
        on_book_ticker: BookTickerHandler,
        on_depth: DepthHandler,
    ) -> None:
        if raw == "pong":
            return
        msg = json.loads(raw)
        arg = msg.get("arg", {})
        channel = arg.get("channel", "")
        if not channel:
            return  # subscribe ack / error, not market data
        now = time.time()
        data = msg.get("data", [])

        if channel == "trades":
            for t in data:
                trade = Trade(
                    symbol=self._to_canonical(t["instId"]), ts=now, exch_ts=float(t["ts"]) / 1000.0,
                    price=float(t["px"]), qty=float(t["sz"]), is_buyer_maker=(t["side"] == "sell"),
                )
                await on_trade(trade)

        elif channel == "tickers":
            for t in data:
                bid, ask = t.get("bidPx"), t.get("askPx")
                if not bid or not ask:
                    continue
                bt = BookTicker(
                    symbol=self._to_canonical(t["instId"]), ts=now, best_bid=float(bid),
                    best_bid_qty=float(t.get("bidSz", 0) or 0), best_ask=float(ask),
                    best_ask_qty=float(t.get("askSz", 0) or 0),
                )
                await on_book_ticker(bt)

        elif channel == "books5":
            for d in data:
                symbol = self._to_canonical(arg.get("instId", ""))
                depth = DepthSnapshot(
                    symbol=symbol, ts=now,
                    bids=[(float(p), float(q)) for p, q, *_ in d.get("bids", [])],
                    asks=[(float(p), float(q)) for p, q, *_ in d.get("asks", [])],
                )
                await on_depth(depth)

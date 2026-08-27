"""Binance Spot adapter - PUBLIC market data only (see base.py SAFETY note).

Uses REST for exchangeInfo/24h volume (symbol universe + filters) and combined
WebSocket streams (aggTrade, bookTicker, partial depth) for live data. No API
key is ever read, stored, or sent - there is nothing here that could take it.
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

logger = logging.getLogger(__name__)

# Binance's dedicated public market-data mirror. Same REST/WS payloads as the
# main api.binance.com/stream.binance.com hosts, but not subject to the
# trading API's regional/CloudFront blocking, since it serves market data only
# (no order endpoints exist here - consistent with this adapter's scope, see
# base.py SAFETY note). Kept as module-level constants so a future adapter
# variant could point elsewhere without touching call sites.
REST_BASE = "https://data-api.binance.vision"
WS_BASE = "wss://data-stream.binance.vision/stream"


class BinanceAdapter:
    name = "binance"

    def __init__(self, quote_asset: str = "USDT", ws_symbols_per_connection: int = 60):
        self.quote_asset = quote_asset
        self.ws_symbols_per_connection = ws_symbols_per_connection

    async def fetch_symbol_universe(self) -> list[SymbolFilter]:
        async with aiohttp.ClientSession() as session:
            exchange_info, tickers_24h = await asyncio.gather(
                self._get_json(session, "/api/v3/exchangeInfo"),
                self._get_json(session, "/api/v3/ticker/24hr"),
            )

        volume_by_symbol = {t["symbol"]: float(t["quoteVolume"]) for t in tickers_24h}

        results: list[SymbolFilter] = []
        for s in exchange_info["symbols"]:
            if s["quoteAsset"] != self.quote_asset:
                continue
            if not s.get("isSpotTradingAllowed", True):
                continue
            if s["status"] != "TRADING":
                continue

            tick_size = 0.0
            step_size = 0.0
            min_notional = 0.0
            for f in s["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    tick_size = float(f["tickSize"])
                elif f["filterType"] == "LOT_SIZE":
                    step_size = float(f["stepSize"])
                elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(f.get("minNotional") or f.get("minNotional", 0) or 0)

            results.append(
                SymbolFilter(
                    symbol=s["symbol"],
                    base_asset=s["baseAsset"],
                    quote_asset=s["quoteAsset"],
                    tick_size=tick_size,
                    step_size=step_size,
                    min_notional=min_notional,
                    quote_volume_24h=volume_by_symbol.get(s["symbol"], 0.0),
                    status=s["status"],
                )
            )
        return results

    async def _get_json(self, session: aiohttp.ClientSession, path: str) -> dict:
        async with session.get(REST_BASE + path, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
        streams = []
        for sym in symbols:
            low = sym.lower()
            streams += [f"{low}@aggTrade", f"{low}@bookTicker", f"{low}@depth5@100ms"]
        url = f"{WS_BASE}?streams={'/'.join(streams)}"

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("binance ws connected: %d symbols", len(symbols))
                    backoff = 1.0
                    async for raw in ws:
                        await self._dispatch(raw, on_trade, on_book_ticker, on_depth)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("binance ws connection dropped, reconnecting in %.1fs", backoff)
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
        stream = msg.get("stream", "")
        data = msg.get("data", {})
        now = time.time()

        if stream.endswith("@aggTrade"):
            trade = Trade(
                symbol=data["s"],
                ts=now,
                exch_ts=data["T"] / 1000.0,
                price=float(data["p"]),
                qty=float(data["q"]),
                is_buyer_maker=bool(data["m"]),
            )
            await on_trade(trade)
        elif stream.endswith("@bookTicker"):
            bt = BookTicker(
                symbol=data["s"],
                ts=now,
                best_bid=float(data["b"]),
                best_bid_qty=float(data["B"]),
                best_ask=float(data["a"]),
                best_ask_qty=float(data["A"]),
            )
            await on_book_ticker(bt)
        elif "@depth5" in stream:
            symbol = stream.split("@")[0].upper()
            depth = DepthSnapshot(
                symbol=symbol,
                ts=now,
                bids=[(float(p), float(q)) for p, q in data.get("bids", [])],
                asks=[(float(p), float(q)) for p, q in data.get("asks", [])],
            )
            await on_depth(depth)

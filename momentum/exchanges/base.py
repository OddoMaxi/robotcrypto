"""Exchange adapter contract.

SAFETY (section 24): this module tree defines ONLY public market-data access
(symbol metadata/filters + read-only WS streams). There is no authenticated
client, no API-secret handling, and no order-placement method anywhere here or
in any subclass. `tests/safety/test_isolation.py` enforces this by scanning the
whole `momentum` package for forbidden symbols (place_order, submit_order,
withdraw, transfer, create_order, cancel_order, ...). Adding a live trading
client is out of scope for V1 and must not happen inside this tree.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from momentum.data.events import BookTicker, DepthSnapshot, Trade


@dataclass(slots=True)
class SymbolFilter:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: float
    step_size: float
    min_notional: float
    quote_volume_24h: float
    status: str


TradeHandler = Callable[[Trade], Awaitable[None]]
BookTickerHandler = Callable[[BookTicker], Awaitable[None]]
DepthHandler = Callable[[DepthSnapshot], Awaitable[None]]


class ExchangeAdapter(Protocol):
    name: str

    async def fetch_symbol_universe(self) -> list[SymbolFilter]:
        """Public REST call: exchange info + 24h volume ranking. No auth."""
        ...

    async def stream_market_data(
        self,
        symbols: list[str],
        on_trade: TradeHandler,
        on_book_ticker: BookTickerHandler,
        on_depth: DepthHandler,
    ) -> None:
        """Public WS streams only (trade/aggTrade, bookTicker, partial depth).
        Runs until cancelled; reconnects internally on drop."""
        ...

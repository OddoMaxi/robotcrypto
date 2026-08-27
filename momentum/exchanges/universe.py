"""Stage A: build/refresh the tradable universe. This is the 'scan hundreds' half
of section 4 - a cheap REST-driven liquidity filter, refreshed periodically, that
decides which symbols are even worth a WebSocket subscription. The heavy per-tick
Stage A/B scoring happens downstream in the engines against live state, not here.
"""
from __future__ import annotations

import logging

from momentum.exchanges.base import ExchangeAdapter, SymbolFilter

logger = logging.getLogger(__name__)

# Defense-in-depth: each adapter already excludes non-trading symbols from its
# own REST response, but Universe re-checks status itself rather than trusting
# every adapter to always get this right (a delisted/suspended symbol must
# never reach Stage A/B just because one adapter's filtering changed).
TRADABLE_STATUSES = {"TRADING", "Trading", "live"}  # Binance, Bybit, OKX respectively


class Universe:
    def __init__(self, adapter: ExchangeAdapter, min_quote_volume_24h: float):
        self.adapter = adapter
        self.min_quote_volume_24h = min_quote_volume_24h
        self.filters_by_symbol: dict[str, SymbolFilter] = {}

    async def refresh(self) -> list[str]:
        all_symbols = await self.adapter.fetch_symbol_universe()
        liquid = [
            s for s in all_symbols
            if s.quote_volume_24h >= self.min_quote_volume_24h and s.status in TRADABLE_STATUSES
        ]
        liquid.sort(key=lambda s: s.quote_volume_24h, reverse=True)

        self.filters_by_symbol = {s.symbol: s for s in liquid}
        logger.info(
            "universe refreshed: %d/%d symbols pass min_quote_volume_24h=%.0f",
            len(liquid), len(all_symbols), self.min_quote_volume_24h,
        )
        return [s.symbol for s in liquid]

    def get_filter(self, symbol: str) -> SymbolFilter | None:
        return self.filters_by_symbol.get(symbol)

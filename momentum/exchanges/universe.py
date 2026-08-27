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
    def __init__(self, adapter: ExchangeAdapter, min_quote_volume_24h: float,
                 watchlist_min_quote_volume_24h: float | None = None):
        self.adapter = adapter
        self.min_quote_volume_24h = min_quote_volume_24h
        # Sub-threshold WATCHLIST tier (user-requested): symbols between this
        # floor and min_quote_volume_24h are tracked and shown on the dashboard
        # for visibility, but never enter Stage A/B promotion or trading - the
        # real min_quote_volume_24h gate above is untouched.
        self.watchlist_min_quote_volume_24h = watchlist_min_quote_volume_24h
        self.filters_by_symbol: dict[str, SymbolFilter] = {}
        self.watchlist_symbols: list[str] = []

    async def refresh(self) -> list[str]:
        all_symbols = await self.adapter.fetch_symbol_universe()
        tradable = [
            s for s in all_symbols
            if s.quote_volume_24h >= self.min_quote_volume_24h and s.status in TRADABLE_STATUSES
        ]
        tradable.sort(key=lambda s: s.quote_volume_24h, reverse=True)
        self.filters_by_symbol = {s.symbol: s for s in tradable}

        if self.watchlist_min_quote_volume_24h is not None:
            watchlist = [
                s for s in all_symbols
                if self.watchlist_min_quote_volume_24h <= s.quote_volume_24h < self.min_quote_volume_24h
                and s.status in TRADABLE_STATUSES
            ]
            watchlist.sort(key=lambda s: s.quote_volume_24h, reverse=True)
            self.watchlist_symbols = [s.symbol for s in watchlist]
            for s in watchlist:
                self.filters_by_symbol.setdefault(s.symbol, s)
        else:
            self.watchlist_symbols = []

        logger.info(
            "universe refreshed: %d/%d symbols pass min_quote_volume_24h=%.0f (+%d watchlist-tier)",
            len(tradable), len(all_symbols), self.min_quote_volume_24h, len(self.watchlist_symbols),
        )
        return [s.symbol for s in tradable]

    def get_filter(self, symbol: str) -> SymbolFilter | None:
        return self.filters_by_symbol.get(symbol)

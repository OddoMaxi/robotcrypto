"""Shared in-memory runtime state the dashboard reads from. Nothing here can
place an order - it's a read model built from SymbolState + Ledger."""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.exchanges.health import HealthRegistry
from momentum.exchanges.universe import Universe
from momentum.shadow.digital_twin import DigitalTwin
from momentum.shadow.early_mover import EarlyMoverTracker
from momentum.shadow.ledger import Ledger


@dataclass
class AppRuntime:
    shadow_mode: bool
    real_orders: int
    exchanges: list[str]
    start_time: float
    ledger: Ledger
    digital_twin: DigitalTwin
    early_mover_tracker: EarlyMoverTracker
    universe_by_exchange: dict[str, Universe]
    health: HealthRegistry
    tracked_symbols_by_exchange: dict[str, list[str]] = field(default_factory=dict)
    # V1.1 mission 9: stablecoin pairs still get live data (tracked_symbols_by_exchange
    # includes them, for WS subscription) but are excluded from momentum_symbols - the
    # Stage A/B loop never sees them, they only feed the separate stablecoin monitor.
    stablecoin_symbols_by_exchange: dict[str, list[str]] = field(default_factory=dict)
    # user-requested sub-threshold tier: real, liquid-enough-to-list pairs that
    # sit below min_quote_volume_24h but above watchlist_min_quote_volume_24h.
    # Dashboard visibility only, sourced from periodic REST 24h-ticker data
    # (see app.py _build_watchlist_snapshot) - deliberately NOT included in
    # tracked_symbols_by_exchange/WS subscriptions (a prior version did include
    # them there, which nearly tripled live WS message volume and was the root
    # cause of a CPU regression). Excluded from momentum_symbols exactly like
    # stablecoins either way, so they can never reach Stage A/B promotion or trading.
    watchlist_symbols_by_exchange: dict[str, list[str]] = field(default_factory=dict)
    universe_size_by_exchange: dict[str, int] = field(default_factory=dict)
    promoted: dict = field(default_factory=dict)   # canonical symbol -> dashboard candidate dict
    last_stage_a_scanned: int = 0
    last_compute_budget: dict = field(default_factory=dict)
    stablecoin_snapshot: dict = field(default_factory=dict)  # symbol -> latest StablecoinCheck-derived dict
    watchlist_snapshot: dict = field(default_factory=dict)   # symbol -> {price, quote_volume_24h, ...}, REST-only, no WS subscription

    @property
    def tracked_symbols(self) -> list[str]:
        """Union of every canonical symbol tracked (incl. stablecoins/watchlist) on any exchange."""
        seen: dict[str, None] = {}
        for symbols in self.tracked_symbols_by_exchange.values():
            for s in symbols:
                seen[s] = None
        return list(seen)

    @property
    def momentum_symbols_by_exchange(self) -> dict[str, list[str]]:
        out = {}
        for ex, symbols in self.tracked_symbols_by_exchange.items():
            excluded = set(self.stablecoin_symbols_by_exchange.get(ex, [])) | \
                set(self.watchlist_symbols_by_exchange.get(ex, []))
            out[ex] = [s for s in symbols if s not in excluded]
        return out

    @property
    def momentum_symbols(self) -> list[str]:
        """Union of every non-stablecoin canonical symbol - what Stage A/B actually scans."""
        seen: dict[str, None] = {}
        for symbols in self.momentum_symbols_by_exchange.values():
            for s in symbols:
                seen[s] = None
        return list(seen)

    @property
    def watchlist_symbols(self) -> list[str]:
        """Union of every sub-threshold watchlist-tier symbol across exchanges."""
        seen: dict[str, None] = {}
        for symbols in self.watchlist_symbols_by_exchange.values():
            for s in symbols:
                seen[s] = None
        return list(seen)

    @property
    def universe_size(self) -> int:
        return sum(self.universe_size_by_exchange.values())

    @property
    def common_symbols(self) -> list[str]:
        """Momentum symbols tracked on 2+ exchanges - the population the cross-exchange
        confirmation engine actually has something to confirm against."""
        counts: dict[str, int] = {}
        for symbols in self.momentum_symbols_by_exchange.values():
            for s in symbols:
                counts[s] = counts.get(s, 0) + 1
        return [s for s, c in counts.items() if c >= 2]

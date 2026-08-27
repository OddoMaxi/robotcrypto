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
    universe_size_by_exchange: dict[str, int] = field(default_factory=dict)
    promoted: dict = field(default_factory=dict)   # canonical symbol -> dashboard candidate dict
    last_stage_a_scanned: int = 0

    @property
    def tracked_symbols(self) -> list[str]:
        """Union of every canonical symbol tracked on any exchange."""
        seen: dict[str, None] = {}
        for symbols in self.tracked_symbols_by_exchange.values():
            for s in symbols:
                seen[s] = None
        return list(seen)

    @property
    def universe_size(self) -> int:
        return sum(self.universe_size_by_exchange.values())

    @property
    def common_symbols(self) -> list[str]:
        """Symbols tracked on 2+ exchanges - the population the cross-exchange
        confirmation engine actually has something to confirm against."""
        counts: dict[str, int] = {}
        for symbols in self.tracked_symbols_by_exchange.values():
            for s in symbols:
                counts[s] = counts.get(s, 0) + 1
        return [s for s, c in counts.items() if c >= 2]

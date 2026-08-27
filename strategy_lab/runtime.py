"""Shared in-memory runtime state the Lab dashboard reads from - same read-model
pattern as momentum/runtime.py, but its own instance/own process. Nothing here
can place an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.exchanges.health import HealthRegistry
from momentum.exchanges.universe import Universe

from strategy_lab.ledger import LabLedger


@dataclass
class LabRuntime:
    shadow_mode: bool
    real_orders: int
    exchanges: list[str]
    start_time: float
    ledger: LabLedger
    universe_by_exchange: dict[str, Universe]
    health: HealthRegistry
    dataset_phase: str
    dataset_version: str
    tracked_symbols_by_exchange: dict[str, list[str]] = field(default_factory=dict)
    universe_size_by_exchange: dict[str, int] = field(default_factory=dict)
    last_compute_budget: dict = field(default_factory=dict)
    market_events_total: int = 0
    live_market_snapshot: dict = field(default_factory=dict)   # "hottest symbol" panel
    strategy_agreement_snapshot: dict = field(default_factory=dict)
    open_trade_count: int = 0

    @property
    def tracked_symbols(self) -> list[str]:
        seen: dict[str, None] = {}
        for symbols in self.tracked_symbols_by_exchange.values():
            for s in symbols:
                seen[s] = None
        return list(seen)

    @property
    def universe_size(self) -> int:
        return sum(self.universe_size_by_exchange.values())

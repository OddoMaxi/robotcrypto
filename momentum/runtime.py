"""Shared in-memory runtime state the dashboard reads from. Nothing here can
place an order - it's a read model built from SymbolState + Ledger."""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.exchanges.universe import Universe
from momentum.shadow.digital_twin import DigitalTwin
from momentum.shadow.ledger import Ledger


@dataclass
class AppRuntime:
    shadow_mode: bool
    real_orders: int
    exchanges: list[str]
    start_time: float
    ledger: Ledger
    digital_twin: DigitalTwin
    universe: Universe
    tracked_symbols: list[str] = field(default_factory=list)
    universe_size: int = 0
    promoted: dict = field(default_factory=dict)   # symbol -> dashboard candidate dict
    last_stage_a_scanned: int = 0

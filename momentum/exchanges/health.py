"""Per-exchange WS health tracking, shared between an adapter and the
dashboard: connection state, reconnect count, last message age, connect
latency. Used for graceful degradation (mission 14: 3->2->1 exchanges) and
the dashboard's multi-exchange status panel.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ExchangeHealth:
    exchange: str
    connected: bool = False
    reconnect_count: int = 0
    last_message_ts: float = 0.0
    last_connect_latency_ms: float | None = None
    symbols_subscribed: int = 0

    def on_connecting(self) -> float:
        return time.time()

    def on_connected(self, connect_started_at: float, is_reconnect: bool = False) -> None:
        """`is_reconnect` must be tracked by the caller *per WS connection*, not
        globally per exchange - an exchange can have several parallel chunked
        connections (see binance.py), and one chunk's first-ever connect must
        never be counted as a reconnect just because a sibling chunk already
        connected onto this shared ExchangeHealth object."""
        self.last_connect_latency_ms = (time.time() - connect_started_at) * 1000.0
        if is_reconnect:
            self.reconnect_count += 1
        self.connected = True

    def on_disconnected(self) -> None:
        self.connected = False

    def on_message(self) -> None:
        self.last_message_ts = time.time()

    def data_freshness_s(self, now: float | None = None) -> float | None:
        if not self.last_message_ts:
            return None
        return (now or time.time()) - self.last_message_ts

    def to_dict(self) -> dict:
        now = time.time()
        return {
            "exchange": self.exchange,
            "connected": self.connected,
            "reconnect_count": self.reconnect_count,
            "connect_latency_ms": self.last_connect_latency_ms,
            "data_freshness_s": self.data_freshness_s(now),
            "symbols_subscribed": self.symbols_subscribed,
        }


class HealthRegistry:
    def __init__(self):
        self._health: dict[str, ExchangeHealth] = {}

    def get_or_create(self, exchange: str) -> ExchangeHealth:
        if exchange not in self._health:
            self._health[exchange] = ExchangeHealth(exchange=exchange)
        return self._health[exchange]

    def all(self) -> dict[str, ExchangeHealth]:
        return dict(self._health)

    def active_exchanges(self) -> list[str]:
        """Exchanges considered usable right now (connected, data not stale)."""
        return [
            ex for ex, h in self._health.items()
            if h.connected and (h.data_freshness_s() is None or h.data_freshness_s() < 30.0)
        ]

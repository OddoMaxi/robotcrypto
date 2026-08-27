"""MISSED MOVE ANALYZER (spec section 12). After a significant move that was
NOT captured by any strategy_trade, logs when it first became detectable
(the earliest strategy_signals row for that symbol+direction, if any) and why
it wasn't traded. Analytical only - this never rewrites the original decision,
it only reads back what was already logged at the time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from momentum.data.state import StateStore
from strategy_lab.ledger import LabLedger

MOVE_LOOKBACK_S = 60.0
MOVE_THRESHOLD_PCT = 1.0
COOLDOWN_S = 180.0


class MissedMoveAnalyzer:
    def __init__(self, ledger: LabLedger):
        self.ledger = ledger
        self._last_logged: dict[tuple[str, str], float] = {}

    async def tick(self, store: StateStore, symbols_by_exchange: dict[str, list[str]], now: float) -> None:
        for exchange, symbols in symbols_by_exchange.items():
            for symbol in symbols:
                state = store.get(exchange, symbol)
                if state is None:
                    continue
                move = state.velocity_pct(now, MOVE_LOOKBACK_S)
                if move is None or abs(move) < MOVE_THRESHOLD_PCT:
                    continue
                direction = "UP" if move > 0 else "DOWN"
                key = (symbol, direction)
                if now - self._last_logged.get(key, 0.0) < COOLDOWN_S:
                    continue

                window_start_iso = (datetime.now(timezone.utc) - timedelta(seconds=MOVE_LOOKBACK_S)).isoformat()
                if await self.ledger.has_recent_trade(symbol, direction, window_start_iso):
                    continue   # not missed - a strategy already captured this one

                self._last_logged[key] = now
                earliest = await self.ledger.get_earliest_signal_in_window(symbol, direction, window_start_iso)
                await self.ledger.insert_missed_move(
                    symbol=symbol, exchange=exchange, direction=direction, move_pct=move,
                    move_window_s=MOVE_LOOKBACK_S,
                    first_detectable_ts=earliest["ts"] if earliest else None,
                    first_detectable_scores=earliest["scores"] if earliest else {},
                    reject_reason=(earliest["reject_reason"] if earliest else "never_detected_by_any_strategy"),
                    what_happened_next={},   # reserved: a later pass can backfill continuation stats
                )

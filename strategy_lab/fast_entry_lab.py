"""FAST ENTRY LAB (spec section 9). For every actionable signal (one that
cleared the exhaustion/late-entry veto, whether or not it cleared the net-edge
gate), observes - once each confirmation window has actually elapsed in real
time, never before - whether the move was "still valid" (kept moving
favorably) and what the resulting net edge would have been. This answers the
confirmation-vs-late-entry tradeoff empirically, per strategy, with no
look-ahead: a window's row is only ever written after that much real time has
passed since the signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.data.state import StateStore
from strategy_lab.execution import ShadowExecutionEngine
from strategy_lab.ledger import LabLedger
from strategy_lab.strategies.base import estimate_round_trip_cost_pct


@dataclass
class _Pending:
    signal_id: int
    strategy: str
    symbol: str
    exchange: str
    direction: str
    t0: float
    t0_price: float
    windows_remaining: list[float] = field(default_factory=list)


class FastEntryLab:
    def __init__(self, cfg: dict, execution: ShadowExecutionEngine):
        self.windows_s: list[float] = sorted(float(w) for w in cfg["confirmation_windows_s"])
        self.execution = execution
        self._max_age_s = max(self.windows_s) + 30.0
        self._pending: list[_Pending] = []

    def register(self, signal_id: int, strategy: str, symbol: str, exchange: str, direction: str,
                 t0: float, t0_price: float) -> None:
        self._pending.append(_Pending(
            signal_id=signal_id, strategy=strategy, symbol=symbol, exchange=exchange, direction=direction,
            t0=t0, t0_price=t0_price, windows_remaining=list(self.windows_s),
        ))

    async def tick(self, store: StateStore, ledger: LabLedger, now: float) -> None:
        still_pending: list[_Pending] = []
        for item in self._pending:
            state = store.get(item.exchange, item.symbol)
            due = [w for w in item.windows_remaining if now - item.t0 >= w]
            for window_s in due:
                item.windows_remaining.remove(window_s)
                if state is None:
                    continue
                price_now = state.price_now()
                if price_now is None or item.t0_price <= 0:
                    continue
                move_pct = (price_now - item.t0_price) / item.t0_price * 100.0
                directional_move_pct = move_pct if item.direction == "UP" else -move_pct
                cost_pct = estimate_round_trip_cost_pct(state, self.execution.taker_fee_bps(item.exchange), now)
                await ledger.insert_confirmation_window_stat(
                    signal_id=item.signal_id, strategy=item.strategy, symbol=item.symbol,
                    direction=item.direction, window_s=window_s, still_valid=directional_move_pct > 0,
                    price_move_pct_at_window=directional_move_pct,
                    would_be_net_edge_pct=directional_move_pct - cost_pct,
                )
            if item.windows_remaining and now - item.t0 < self._max_age_s:
                still_pending.append(item)
        self._pending = still_pending

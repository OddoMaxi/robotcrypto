"""SHADOW EXECUTION ENGINE (spec section 11). Wraps
momentum.shadow.broker.ShadowBroker (proven, reused by import - the *one* real
fill per trade uses the exact same realistic book-walk/fee/latency logic the
baseline bot uses) and adds the explicit multi-latency comparison the spec
asks for: 50/100/250/500/1000ms are all simulated for every signal so exit/
entry-timing sensitivity can be measured, not just assumed from one blessed
number.

SAFETY: no method here sends anything to an exchange - same guarantee as
momentum/shadow/broker.py (see its docstring); this module only ever computes
`FillResult`s from already-received public market data.
"""
from __future__ import annotations

from dataclasses import dataclass

from momentum.data.state import SymbolState
from momentum.exchanges.base import SymbolFilter
from momentum.shadow.broker import FillResult, ShadowBroker


@dataclass(slots=True)
class TrueNetPnl:
    gross_pnl_pct: float
    entry_fee: float
    exit_fee: float
    spread_cost_pct: float
    slippage_pct: float
    true_net_pnl: float          # $ terms
    true_net_pnl_pct: float


class ShadowExecutionEngine:
    def __init__(self, execution_cfg: dict):
        self.broker = ShadowBroker({
            "taker_fee_bps_by_exchange": execution_cfg["taker_fee_bps_by_exchange"],
            "simulated_latency_ms": execution_cfg["primary_latency_ms"],
        })
        self.latency_variants_ms: list[int] = list(execution_cfg["simulated_latency_variants_ms"])

    def taker_fee_bps(self, exchange: str) -> float:
        return self.broker.taker_fee_bps(exchange)

    def simulate_entry(self, state: SymbolState, direction: str, size: float, exchange: str) -> FillResult | None:
        return self.broker.simulate_entry(state, direction, size, exchange)

    def simulate_exit(self, state: SymbolState, direction: str, size: float, exchange: str) -> FillResult | None:
        return self.broker.simulate_exit(state, direction, size, exchange)

    @staticmethod
    def apply_filters(sym_filter: SymbolFilter | None, size: float, price: float) -> float | None:
        return ShadowBroker.apply_filters(sym_filter, size, price)

    def simulate_latency_variants(self, state: SymbolState, direction: str, size: float,
                                   exchange: str, side: str) -> dict[int, FillResult | None]:
        """side: 'entry' walks the same book side simulate_entry would, 'exit'
        the same side simulate_exit would - evaluated at each fixed latency in
        `simulated_latency_variants_ms` instead of one randomized draw."""
        out: dict[int, FillResult | None] = {}
        for latency_ms in self.latency_variants_ms:
            out[latency_ms] = self._fill_at_fixed_latency(state, direction, size, exchange, side, latency_ms)
        return out

    def _fill_at_fixed_latency(self, state: SymbolState, direction: str, size: float, exchange: str,
                                side: str, latency_ms: float) -> FillResult | None:
        want_buy = (direction == "UP") if side == "entry" else (direction == "DOWN")
        snap = state.book_state_delayed(latency_ms)
        if snap is None or snap.book_ticker is None:
            return None
        if want_buy:
            levels = list(snap.depth.asks) if snap.depth else []
            best = snap.book_ticker.best_ask
            if not levels:
                levels = [(best, snap.book_ticker.best_ask_qty)]
        else:
            levels = list(snap.depth.bids) if snap.depth else []
            best = snap.book_ticker.best_bid
            if not levels:
                levels = [(best, snap.book_ticker.best_bid_qty)]

        avg_price, filled = ShadowBroker._walk_book(levels, size)
        if filled <= 0 or best <= 0:
            return None
        slippage_pct = abs(avg_price - best) / best * 100.0
        fee = avg_price * filled * (self.taker_fee_bps(exchange) / 10_000)
        return FillResult(avg_price=avg_price, filled_size=filled, fee=fee, slippage_pct=slippage_pct,
                           latency_ms=latency_ms)


def compute_true_net_pnl(direction: str, entry_fill: FillResult, exit_fill: FillResult, size: float,
                          entry_spread_bps: float | None) -> TrueNetPnl:
    """TRUE_NET_SHADOW_PNL = GROSS_RETURN - ENTRY_FEE - EXIT_FEE - SPREAD_COST -
    SLIPPAGE - ESTIMATED_LATENCY_IMPACT (section 11). Latency impact is already
    embedded in entry_fill/exit_fill (both were filled against a latency-
    delayed book snapshot, not instant top-of-book), so it is not subtracted a
    second time here - counting it twice would understate the strategy."""
    if direction == "UP":
        gross_pnl_pct = (exit_fill.avg_price - entry_fill.avg_price) / entry_fill.avg_price * 100.0
    else:
        gross_pnl_pct = (entry_fill.avg_price - exit_fill.avg_price) / entry_fill.avg_price * 100.0

    spread_cost_pct = (entry_spread_bps or 0.0) / 100.0
    slippage_pct = entry_fill.slippage_pct + exit_fill.slippage_pct
    fees_dollars = entry_fill.fee + exit_fill.fee
    notional = entry_fill.avg_price * size

    gross_dollars = gross_pnl_pct / 100.0 * notional
    true_net_pnl = gross_dollars - fees_dollars
    true_net_pnl_pct = (true_net_pnl / notional * 100.0) if notional > 0 else 0.0

    return TrueNetPnl(
        gross_pnl_pct=gross_pnl_pct, entry_fee=entry_fill.fee, exit_fee=exit_fill.fee,
        spread_cost_pct=spread_cost_pct, slippage_pct=slippage_pct, true_net_pnl=true_net_pnl,
        true_net_pnl_pct=true_net_pnl_pct,
    )

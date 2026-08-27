"""THESIS-BASED EXIT ENGINE / EXIT POLICY LAB (spec section 10).

For every real Shadow entry, ALL configured exit policies are evaluated in
parallel as counterfactual replays against the exact same real forward price
path (no separate capital or risk needed - Shadow only). One policy
(`production_exit_policy`, default THESIS_INVALIDATION_EXIT) closes the actual
ledger trade; every policy's own hypothetical exit is persisted to
exit_policy_results for honest comparison. MFE/MAE are tracked continuously
for every open trade regardless of which policy eventually exits it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.data.state import SymbolState, StateStore
from momentum.engines import exhaustion, late_entry, orderflow
from strategy_lab import false_positive_analyzer
from strategy_lab.execution import ShadowExecutionEngine, compute_true_net_pnl
from strategy_lab.ledger import LabLedger
from strategy_lab.strategies.base import exhaustion_veto


@dataclass
class _PolicyState:
    triggered: bool = False
    activated: bool = False
    peak_favorable_pct: float = 0.0


@dataclass
class TrackedTrade:
    trade_id: int
    strategy: str
    symbol: str
    exchange: str
    direction: str
    entry_ts: float
    entry_price: float
    stop_distance_pct: float
    size: float
    entry_velocity_10s: float
    dataset_phase: str
    dataset_version: str
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    policies: dict[str, _PolicyState] = field(default_factory=dict)
    production_closed: bool = False


def _favorable_pct(direction: str, entry_price: float, price: float) -> float:
    return (price - entry_price) / entry_price * 100.0 if direction == "UP" \
        else (entry_price - price) / entry_price * 100.0


class ExitLab:
    def __init__(self, exit_lab_cfg: dict, common_cfg: dict, execution: ShadowExecutionEngine, ledger: LabLedger):
        self.cfg = exit_lab_cfg
        self.common_cfg = common_cfg
        self.execution = execution
        self.ledger = ledger
        self._trades: dict[int, TrackedTrade] = {}
        self._fixed_policy_names = [f"EXIT_FIXED_{h}S" for h in exit_lab_cfg["fixed_horizons_s"]]

    def _policy_names(self) -> list[str]:
        return self._fixed_policy_names + [
            "TRAILING_EXIT", "MOMENTUM_DECAY_EXIT", "ORDER_FLOW_REVERSAL_EXIT",
            "EXHAUSTION_EXIT", "THESIS_INVALIDATION_EXIT", "BREAKEVEN_EXIT", "PARTIAL_TP_EXIT",
        ]

    def open_trade(self, trade_id: int, strategy: str, symbol: str, exchange: str, direction: str,
                    entry_ts: float, entry_price: float, stop_distance_pct: float, size: float,
                    entry_velocity_10s: float, dataset_phase: str, dataset_version: str) -> None:
        trade = TrackedTrade(
            trade_id=trade_id, strategy=strategy, symbol=symbol, exchange=exchange, direction=direction,
            entry_ts=entry_ts, entry_price=entry_price, stop_distance_pct=max(0.01, stop_distance_pct),
            size=size, entry_velocity_10s=entry_velocity_10s, dataset_phase=dataset_phase,
            dataset_version=dataset_version,
        )
        trade.policies = {name: _PolicyState() for name in self._policy_names()}
        self._trades[trade_id] = trade

    @property
    def open_count(self) -> int:
        return len(self._trades)

    def has_open_trade(self, strategy: str, symbol: str, direction: str) -> bool:
        """A strategy still tracking an open (strategy, symbol, direction)
        position must not open a second one on the same thesis every cycle
        the underlying move continues - production_closed tracks the real
        ledger trade specifically, since a trade can stay counterfactually
        tracked (for the other 13 exit policies) after the real one closed."""
        return any(
            t.strategy == strategy and t.symbol == symbol and t.direction == direction and not t.production_closed
            for t in self._trades.values()
        )

    async def tick(self, store: StateStore, now: float) -> None:
        done_ids = []
        for trade_id, trade in list(self._trades.items()):
            state = store.get(trade.exchange, trade.symbol)
            if state is None or state.price_now() is None:
                continue
            price = state.price_now()
            fav = _favorable_pct(trade.direction, trade.entry_price, price)
            trade.mfe_pct = max(trade.mfe_pct, fav)
            trade.mae_pct = min(trade.mae_pct, fav)

            for name in self._policy_names():
                ps = trade.policies[name]
                if ps.triggered:
                    continue
                if self._check(name, trade, ps, state, now, fav):
                    await self._resolve_policy(trade, name, state, now)
                    ps.triggered = True

            all_triggered = all(ps.triggered for ps in trade.policies.values())
            timed_out = (now - trade.entry_ts) >= self.cfg["max_tracking_s"]
            if timed_out:
                for name, ps in trade.policies.items():
                    if not ps.triggered:
                        await self._resolve_policy(trade, name, state, now, forced="lab_timeout")
                        ps.triggered = True
            if all_triggered or timed_out:
                done_ids.append(trade_id)
        for tid in done_ids:
            self._trades.pop(tid, None)

    def _check(self, name: str, trade: TrackedTrade, ps: _PolicyState, state: SymbolState, now: float,
               fav: float) -> bool:
        elapsed = now - trade.entry_ts
        r = fav / trade.stop_distance_pct if trade.stop_distance_pct > 0 else 0.0

        if name.startswith("EXIT_FIXED_"):
            horizon_s = float(name[len("EXIT_FIXED_"):-1])
            return elapsed >= horizon_s

        if name == "TRAILING_EXIT":
            if not ps.activated:
                if r >= self.cfg["trailing_activation_r"]:
                    ps.activated = True
                    ps.peak_favorable_pct = fav
                return False
            ps.peak_favorable_pct = max(ps.peak_favorable_pct, fav)
            return fav <= ps.peak_favorable_pct - trade.stop_distance_pct * 0.5

        if name == "MOMENTUM_DECAY_EXIT":
            v10 = state.velocity_pct(now, 10)
            if v10 is None:
                return False
            directional_v = v10 if trade.direction == "UP" else -v10
            directional_entry_v = trade.entry_velocity_10s if trade.direction == "UP" else -trade.entry_velocity_10s
            if directional_entry_v <= 0:
                return False
            return directional_v < directional_entry_v * self.cfg["momentum_decay_fraction"]

        if name == "ORDER_FLOW_REVERSAL_EXIT":
            of = orderflow.compute(state, now)
            buy_ratio = of.details.get("ratios", {}).get(5)
            if buy_ratio is None:
                return False
            threshold = self.cfg["orderflow_reversal_threshold"]
            return buy_ratio <= (1.0 - threshold) if trade.direction == "UP" else buy_ratio >= threshold

        if name == "EXHAUSTION_EXIT":
            ex = exhaustion.compute(state, now)
            risk = ex.up_risk if trade.direction == "UP" else ex.down_risk
            return risk >= self.cfg["exhaustion_exit_threshold"]

        if name == "THESIS_INVALIDATION_EXIT":
            ex = exhaustion.compute(state, now)
            le = late_entry.compute(state, now)
            exh_risk = ex.up_risk if trade.direction == "UP" else ex.down_risk
            late_risk = le.up_risk if trade.direction == "UP" else le.down_risk
            v10 = state.velocity_pct(now, 10) or 0.0
            direction_gone = (v10 <= 0) if trade.direction == "UP" else (v10 >= 0)
            veto = exhaustion_veto(exh_risk, late_risk, self.common_cfg["exhaustion_veto"],
                                    self.common_cfg["late_entry_veto"])
            return direction_gone and veto is not None

        if name == "BREAKEVEN_EXIT":
            if not ps.activated:
                if r >= self.cfg["breakeven_trigger_r"]:
                    ps.activated = True
                return False
            return fav <= 0.0

        if name == "PARTIAL_TP_EXIT":
            return r >= self.cfg["partial_tp_r"]

        return False

    async def _resolve_policy(self, trade: TrackedTrade, name: str, state: SymbolState, now: float,
                               forced: str | None = None) -> None:
        exit_fill = self.execution.simulate_exit(state, trade.direction, trade.size, trade.exchange)
        if exit_fill is None:
            return
        entry_fill_price = trade.entry_price  # already fee/slippage-adjusted at entry time
        gross_pnl_pct = _favorable_pct(trade.direction, entry_fill_price, exit_fill.avg_price)
        fee_pct = (self.execution.taker_fee_bps(trade.exchange) / 100.0)
        true_net_pnl_pct = gross_pnl_pct - fee_pct - exit_fill.slippage_pct
        hold_s = now - trade.entry_ts

        await self.ledger.insert_exit_policy_result(
            trade_id=trade.trade_id, policy=name, exit_price=exit_fill.avg_price, hold_s=hold_s,
            gross_pnl_pct=gross_pnl_pct, true_net_pnl_pct=true_net_pnl_pct,
            mfe_pct=trade.mfe_pct, mae_pct=trade.mae_pct,
        )

        if name == self.cfg["production_exit_policy"] and not trade.production_closed:
            trade.production_closed = True
            r_multiple = (gross_pnl_pct / trade.stop_distance_pct) if trade.stop_distance_pct > 0 else None
            notional = exit_fill.avg_price * trade.size
            true_net_pnl_dollars = true_net_pnl_pct / 100.0 * notional
            await self.ledger.close_trade(
                trade.trade_id, exit_policy=name, exit_price=exit_fill.avg_price,
                exit_reason=forced or "thesis_invalidated", exit_fee=exit_fill.fee,
                exit_slippage_pct=exit_fill.slippage_pct, exit_latency_ms=exit_fill.latency_ms,
                spread_cost_pct=None, gross_pnl_pct=gross_pnl_pct, true_net_pnl=true_net_pnl_dollars,
                true_net_pnl_pct=true_net_pnl_pct, r_multiple=r_multiple, mfe_pct=trade.mfe_pct,
                mae_pct=trade.mae_pct, hold_s=hold_s,
            )
            if true_net_pnl_dollars <= 0:
                await self._classify_false_positive(trade, gross_pnl_pct)

    async def _classify_false_positive(self, trade: TrackedTrade, gross_pnl_pct: float) -> None:
        """Section 13: post-trade only, analytical - never fed back into the
        live decision path or used to rewrite the (already-logged) entry."""
        classification = false_positive_analyzer.classify(
            trade.mfe_pct, trade.mae_pct, trade.stop_distance_pct, gross_pnl_pct
        )
        await self.ledger.insert_false_positive(
            trade_id=trade.trade_id, classification=classification,
            details={"mfe_pct": trade.mfe_pct, "mae_pct": trade.mae_pct, "gross_pnl_pct": gross_pnl_pct},
        )

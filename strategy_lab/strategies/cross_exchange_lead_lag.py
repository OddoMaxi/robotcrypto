"""CROSS_EXCHANGE_LEAD_LAG (spec section 7). Reuses
momentum.engines.cross_exchange.LeadLagTracker (its own instance - never
shared with the baseline bot's tracker) to detect, per symbol, which exchange
moves first. Never assumes a fixed leader: FIRST/SECOND/THIRD exchange and
LEAD_MS are measured fresh every episode.

The trade idea is explicit information lag: once one exchange confirms a
direction, bet the others follow. That bet is only ever proposed once
`lead_lag_stats_cache` shows enough sample size and a positive empirical
NET_EXPECTANCY_AFTER_COSTS for that exact (leader, follower) pair - otherwise
this emits an INSUFFICIENT_SAMPLE observation signal (still logged, never
traded). Nothing here asserts causality from a handful of episodes.
"""
from __future__ import annotations

from dataclasses import dataclass

from momentum.data.state import SymbolState
from momentum.engines import exhaustion, late_entry
from momentum.engines.cross_exchange import LeadLagTracker
from strategy_lab.ledger import LabLedger
from strategy_lab.market_bus import MarketEvent
from strategy_lab.strategies.base import StrategySignal, estimate_round_trip_cost_pct, exhaustion_veto

NAME = "CROSS_EXCHANGE_LEAD_LAG"
LEAD_LAG_EVAL_WINDOW_S = 20.0   # how long we wait before scoring a follower's actual outcome


@dataclass
class _PendingObservation:
    symbol: str
    direction: str
    leading_exchange: str
    following_exchange: str
    lead_time_ms: float
    t0: float
    t0_price: float
    t0_velocity_pct: float
    taker_fee_bps: float


class CrossExchangeLeadLagTracker:
    def __init__(self, strategy_cfg: dict):
        self.cfg = strategy_cfg
        self.lead_lag = LeadLagTracker()
        self.stats_cache: dict[tuple[str, str], dict] = {}
        self._pending: list[_PendingObservation] = []

    def compute(self, event: MarketEvent, common_cfg: dict, taker_fee_bps_by_exchange: dict[str, float],
                now: float) -> StrategySignal | None:
        velocities_10s = {}
        for ex, state in event.states_by_exchange.items():
            v10 = state.velocity_pct(now, 10)
            if v10 is not None:
                velocities_10s[ex] = v10
        if len(velocities_10s) < 2:
            return None

        result = self.lead_lag.update(event.symbol, velocities_10s, now)
        leader = result["leading_exchange"]
        if leader is None or result["new_confirmation"] != leader:
            return None   # only act at the moment the leader itself confirms - not on every tick of an ongoing episode

        direction = "UP" if velocities_10s[leader] > 0 else "DOWN"
        best_signal: StrategySignal | None = None
        for follower in event.states_by_exchange:
            if follower == leader or follower not in velocities_10s:
                continue
            follower_state = event.states_by_exchange[follower]
            follower_price = follower_state.price_now()
            if follower_price is None:
                continue

            self._pending.append(_PendingObservation(
                symbol=event.symbol, direction=direction, leading_exchange=leader, following_exchange=follower,
                lead_time_ms=0.0, t0=now, t0_price=follower_price, t0_velocity_pct=velocities_10s.get(follower, 0.0),
                taker_fee_bps=taker_fee_bps_by_exchange.get(follower, 10.0),
            ))

            stats = self.stats_cache.get((leader, follower), {"sample_size": 0})
            # only computed for an actual follower candidate at an actual leader
            # onset (rare) - not precomputed for every symbol/exchange every
            # cycle, which was pure waste for the vast majority of cycles where
            # no lead/lag episode is starting (a real compute-budget fix, see
            # git history for the incident this replaced)
            ex = exhaustion.compute(follower_state, now)
            le = late_entry.compute(follower_state, now)
            exh = ex.up_risk if direction == "UP" else ex.down_risk
            late = le.up_risk if direction == "UP" else le.down_risk

            signal = StrategySignal(
                strategy=NAME, symbol=event.symbol, exchange=follower, direction=direction, price=follower_price,
                score=min(100.0, (stats.get("sample_size", 0) / max(1, self.cfg["min_sample_size"])) * 100.0),
                exhaustion_risk=exh, late_entry_risk=late,
                details={"leading_exchange": leader, "following_exchange": follower,
                         "sample_size": stats.get("sample_size", 0),
                         "avg_net_expectancy_pct": stats.get("avg_net_expectancy_pct"),
                         "success_rate": stats.get("success_rate")},
            )

            if stats.get("sample_size", 0) < self.cfg["min_sample_size"]:
                signal.reject_reason = "insufficient_sample"
            else:
                veto = exhaustion_veto(exh, late, common_cfg["exhaustion_veto"], common_cfg["late_entry_veto"])
                if veto:
                    signal.reject_reason = veto
                elif not stats.get("avg_net_expectancy_pct") or stats["avg_net_expectancy_pct"] <= 0:
                    signal.reject_reason = "no_positive_empirical_edge"
                else:
                    signal.expected_move_pct = abs(stats["avg_net_expectancy_pct"]) + \
                        estimate_round_trip_cost_pct(follower_state, taker_fee_bps_by_exchange.get(follower, 10.0), now)
                    signal.expected_cost_pct = estimate_round_trip_cost_pct(
                        follower_state, taker_fee_bps_by_exchange.get(follower, 10.0), now)
                    signal.accepted = signal.expected_net_edge_pct > common_cfg["min_net_edge_pct"]
                    if not signal.accepted:
                        signal.reject_reason = "net_edge_not_positive"

            if best_signal is None or signal.score > best_signal.score:
                best_signal = signal
        return best_signal

    async def tick_pending_observations(self, store, ledger: LabLedger, now: float) -> None:
        """Resolve any observation whose evaluation window has elapsed: measure
        what the follower exchange's price actually did, net of round-trip
        costs, and persist it - this is what LEADER_SAMPLE_SIZE/SUCCESS_RATE
        (section 7) are built from. No look-ahead: only ever evaluated once
        LEAD_LAG_EVAL_WINDOW_S of real time has actually passed."""
        due = [p for p in self._pending if now - p.t0 >= LEAD_LAG_EVAL_WINDOW_S]
        if not due:
            return
        self._pending = [p for p in self._pending if now - p.t0 < LEAD_LAG_EVAL_WINDOW_S]
        for obs in due:
            state = store.get(obs.following_exchange, obs.symbol)
            price_now = state.price_now() if state else None
            if price_now is None or obs.t0_price <= 0:
                continue
            move_pct = (price_now - obs.t0_price) / obs.t0_price * 100.0
            directional_move_pct = move_pct if obs.direction == "UP" else -move_pct
            cost_pct = estimate_round_trip_cost_pct(state, obs.taker_fee_bps, now)
            net_expectancy_pct = directional_move_pct - cost_pct

            key = (obs.leading_exchange, obs.following_exchange)
            cached = self.stats_cache.get(key, {"sample_size": 0, "avg_net_expectancy_pct": 0.0})
            n = cached.get("sample_size", 0)
            prev_avg = cached.get("avg_net_expectancy_pct") or 0.0
            new_avg = (prev_avg * n + net_expectancy_pct) / (n + 1)
            self.stats_cache[key] = {
                "sample_size": n + 1, "avg_net_expectancy_pct": new_avg,
                "success_rate": cached.get("success_rate"),
            }

            await ledger.insert_lead_lag_observation(
                symbol=obs.symbol, direction=obs.direction, leading_exchange=obs.leading_exchange,
                following_exchange=obs.following_exchange, lead_time_ms=obs.lead_time_ms,
                price_propagation_pct=directional_move_pct, velocity_propagation_ratio=None,
                volume_propagation_ratio=None, follower_net_expectancy_pct=net_expectancy_pct,
            )

    async def refresh_stats_cache(self, ledger: LabLedger, exchanges: list[str]) -> None:
        for leader in exchanges:
            for follower in exchanges:
                if leader == follower:
                    continue
                self.stats_cache[(leader, follower)] = await ledger.get_lead_lag_stats(leader, follower)

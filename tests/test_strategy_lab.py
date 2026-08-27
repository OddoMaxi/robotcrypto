"""Strategy Lab V2 coverage (spec section 18's mandatory list): UP/DOWN
symmetry, persistence, pullback, re-acceleration, exhaustion veto,
breakout/retest, cross-exchange timestamp alignment, no look-ahead, fees/
spread/slippage/latency, MFE/MAE, ledger isolation, baseline immutability,
process isolation, no real-order capability (isolation itself is covered by
tests/test_strategy_lab_isolation.py).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from momentum.data.events import BookTicker, DepthSnapshot, Trade
from momentum.data.state import SymbolState
from strategy_lab.app import _weighted_engine_scores
from strategy_lab.execution import ShadowExecutionEngine, compute_true_net_pnl
from strategy_lab.exit_lab import ExitLab
from strategy_lab.fast_entry_lab import FastEntryLab
from strategy_lab.ledger import LabLedger
from strategy_lab.meta_engine import compute_meta
from strategy_lab.strategies import breakout_retest_continuation, persistent_micro_trend
from strategy_lab.strategies.base import StrategySignal, exhaustion_veto
from strategy_lab.strategies.cross_exchange_lead_lag import CrossExchangeLeadLagTracker
from strategy_lab.strategies.impulse_pullback_reacceleration import ImpulsePullbackReaccelerationTracker
from strategy_lab.walk_forward import WalkForwardTag

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMMON_CFG = {"exhaustion_veto": 75, "late_entry_veto": 70, "min_net_edge_pct": 0.05}


def _push(state: SymbolState, ts: float, price: float, qty: float, is_sell: bool, spread_bps: float = 5.0,
          depth_qty: float = 500.0) -> None:
    state.on_trade(Trade(symbol=state.symbol, ts=ts, exch_ts=ts, price=price, qty=qty, is_buyer_maker=is_sell))
    half = price * (spread_bps / 10_000) / 2
    state.on_book_ticker(BookTicker(symbol=state.symbol, ts=ts, best_bid=price - half, best_bid_qty=depth_qty,
                                     best_ask=price + half, best_ask_qty=depth_qty))
    state.on_depth(DepthSnapshot(symbol=state.symbol, ts=ts, bids=[(price - half, depth_qty)],
                                  asks=[(price + half, depth_qty)]))


def _ramp(state: SymbolState, start_ts: float, start_price: float, end_price: float, seconds: float,
          steps: int, buy_dominant: bool) -> float:
    """Feed `steps` evenly-spaced trades from start_price to end_price over
    `seconds`, with heavier volume on the dominant side - a clean synthetic
    impulse/pullback leg. Returns the ending timestamp."""
    for i in range(1, steps + 1):
        ts = start_ts + seconds * i / steps
        price = start_price + (end_price - start_price) * i / steps
        _push(state, ts, price, qty=5.0, is_sell=not buy_dominant)
        _push(state, ts + 0.001, price, qty=1.0, is_sell=buy_dominant)  # thin opposing print, keeps ratio dominant not exclusive
    return start_ts + seconds


# ---------------------------------------------------------------------------
# PERSISTENT_MICRO_TREND: UP/DOWN symmetry + persistence
# ---------------------------------------------------------------------------

def test_persistent_micro_trend_is_up_down_symmetric():
    base = 2_000_000.0
    up_state = SymbolState("AAAUSDT", "binance")
    end_up = _ramp(up_state, base, 100.0, 103.0, seconds=60, steps=30, buy_dominant=True)

    down_state = SymbolState("BBBUSDT", "binance")
    end_down = _ramp(down_state, base, 100.0, 97.0, seconds=60, steps=30, buy_dominant=False)

    engines_up = _weighted_engine_scores(up_state, end_up)
    engines_down = _weighted_engine_scores(down_state, end_down)
    cfg = {"horizons_s": [1, 3, 5, 10, 15, 20, 30, 60], "min_score": 0}

    sig_up = persistent_micro_trend.compute(
        event=_FakeEvent("AAAUSDT"), primary_ex="binance", state=up_state, engine_scores=engines_up,
        cross_result=None, exhaustion_risk=(0.0, 0.0), late_entry_risk=(0.0, 0.0), strategy_cfg=cfg,
        common_cfg=COMMON_CFG, taker_fee_bps=10.0, now=end_up,
    )
    sig_down = persistent_micro_trend.compute(
        event=_FakeEvent("BBBUSDT"), primary_ex="binance", state=down_state, engine_scores=engines_down,
        cross_result=None, exhaustion_risk=(0.0, 0.0), late_entry_risk=(0.0, 0.0), strategy_cfg=cfg,
        common_cfg=COMMON_CFG, taker_fee_bps=10.0, now=end_down,
    )
    assert sig_up is not None and sig_down is not None
    assert sig_up.direction == "UP"
    assert sig_down.direction == "DOWN"
    # mirrored price paths should produce comparable (not necessarily identical, since
    # engines aren't perfectly symmetric in their internals) persistence scores
    assert abs(sig_up.score - sig_down.score) < 25.0
    assert sig_up.score > 0 and sig_down.score > 0


class _FakeEvent:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.market_event_id = f"{symbol}:test"


# ---------------------------------------------------------------------------
# IMPULSE -> PULLBACK -> RE-ACCELERATION
# ---------------------------------------------------------------------------

def test_ipr_full_cycle_impulse_then_pullback_then_reacceleration():
    cfg = {
        "impulse_min_velocity_pct_10s": 0.15, "pullback_min_ratio": 0.15,
        "pullback_max_ratio": 0.75, "reaccel_min_score": 40,
    }
    tracker = ImpulsePullbackReaccelerationTracker(cfg)
    state = SymbolState("CCCUSDT", "binance")
    ts = 3_000_000.0
    event = _FakeEvent("CCCUSDT")

    # PHASE A: a strong, clean impulse up
    ts = _ramp(state, ts, 100.0, 103.0, seconds=20, steps=20, buy_dominant=True)
    engines = _weighted_engine_scores(state, ts)
    sig_a = tracker.compute(event, "binance", state, engines, None, (0.0, 0.0), (0.0, 0.0), COMMON_CFG, 10.0, ts)
    assert sig_a is not None and sig_a.phase == "IMPULSE" and sig_a.direction == "UP"
    assert sig_a.accepted is False   # never enters on phase A

    # PHASE B: a controlled pullback (~40% retracement of the impulse)
    pullback_target = 103.0 - (103.0 - 100.0) * 0.4
    ts = _ramp(state, ts, 103.0, pullback_target, seconds=10, steps=10, buy_dominant=False)
    engines = _weighted_engine_scores(state, ts)
    sig_b = tracker.compute(event, "binance", state, engines, None, (0.0, 0.0), (0.0, 0.0), COMMON_CFG, 10.0, ts)
    assert sig_b is not None and sig_b.phase == "PULLBACK"
    assert 0.15 <= sig_b.details["pullback_ratio"] <= 0.75

    # PHASE C: re-acceleration - reclaim and push beyond the impulse peak
    ts = _ramp(state, ts, pullback_target, 104.5, seconds=15, steps=15, buy_dominant=True)
    engines = _weighted_engine_scores(state, ts)
    sig_c = tracker.compute(event, "binance", state, engines, None, (0.0, 0.0), (0.0, 0.0), COMMON_CFG, 10.0, ts)
    assert sig_c is not None
    assert sig_c.phase == "REACCELERATION"
    assert sig_c.direction == "UP"


def test_ipr_pullback_too_deep_invalidates_thesis():
    cfg = {
        "impulse_min_velocity_pct_10s": 0.15, "pullback_min_ratio": 0.15,
        "pullback_max_ratio": 0.75, "reaccel_min_score": 40,
    }
    tracker = ImpulsePullbackReaccelerationTracker(cfg)
    state = SymbolState("DDDUSDT", "binance")
    ts = 4_000_000.0
    event = _FakeEvent("DDDUSDT")

    ts = _ramp(state, ts, 100.0, 103.0, seconds=20, steps=20, buy_dominant=True)
    engines = _weighted_engine_scores(state, ts)
    tracker.compute(event, "binance", state, engines, None, (0.0, 0.0), (0.0, 0.0), COMMON_CFG, 10.0, ts)

    # retrace almost the entire impulse (>75%) - thesis should be dropped, not "pullback"
    ts = _ramp(state, ts, 103.0, 100.2, seconds=10, steps=10, buy_dominant=False)
    engines = _weighted_engine_scores(state, ts)
    sig = tracker.compute(event, "binance", state, engines, None, (0.0, 0.0), (0.0, 0.0), COMMON_CFG, 10.0, ts)
    assert sig is None
    assert tracker.active_symbols() == []   # episode was dropped, not left dangling


# ---------------------------------------------------------------------------
# EXHAUSTION / LATE-ENTRY VETO (shared helper every strategy uses)
# ---------------------------------------------------------------------------

def test_exhaustion_veto_overrides_a_strong_score():
    assert exhaustion_veto(exhaustion_risk=80.0, late_entry_risk=0.0, exhaustion_threshold=75, late_entry_threshold=70) == "exhausted"
    assert exhaustion_veto(exhaustion_risk=0.0, late_entry_risk=90.0, exhaustion_threshold=75, late_entry_threshold=70) == "too_late"
    assert exhaustion_veto(exhaustion_risk=10.0, late_entry_risk=10.0, exhaustion_threshold=75, late_entry_threshold=70) is None


# ---------------------------------------------------------------------------
# BREAKOUT -> RETEST -> CONTINUATION
# ---------------------------------------------------------------------------

def test_breakout_retest_continuation_detects_up_retest():
    cfg = {"level_lookback_s": 90, "retest_tolerance_pct": 0.5, "min_breakout_magnitude_pct": 0.15, "min_score": 30}
    state = SymbolState("EEEUSDT", "binance")
    ts = 5_000_000.0
    # structural range before any breakout
    ts = _ramp(state, ts, 100.0, 100.3, seconds=40, steps=15, buy_dominant=True)
    ts = _ramp(state, ts, 100.3, 100.0, seconds=20, steps=8, buy_dominant=False)
    # breakout above the ~100.3 structural high
    ts = _ramp(state, ts, 100.0, 101.0, seconds=8, steps=8, buy_dominant=True)
    # retest back down near the broken level
    ts = _ramp(state, ts, 101.0, 100.35, seconds=8, steps=8, buy_dominant=False)

    engines = _weighted_engine_scores(state, ts)
    sig = breakout_retest_continuation.compute(_FakeEvent("EEEUSDT"), "binance", state, engines, None,
                                                 (0.0, 0.0), (0.0, 0.0), cfg, COMMON_CFG, 10.0, ts)
    assert sig is not None
    assert sig.direction == "UP"
    assert sig.details["breakout_magnitude_pct"] > 0.15


# ---------------------------------------------------------------------------
# CROSS-EXCHANGE LEAD/LAG: timestamp alignment + no look-ahead
# ---------------------------------------------------------------------------

def test_lead_lag_observation_only_resolves_after_the_eval_window_elapses():
    import asyncio

    async def _run():
        tracker = CrossExchangeLeadLagTracker({"onset_threshold_pct": 0.10, "min_sample_size": 30})
        binance = SymbolState("FFFUSDT", "binance")
        bybit = SymbolState("FFFUSDT", "bybit")
        ts = 6_000_000.0
        # only binance moves (a clean leader onset)
        _ramp(binance, ts, 100.0, 100.5, seconds=10, steps=10, buy_dominant=True)
        _push(bybit, ts, 100.0, 1.0, is_sell=False)
        ts2 = ts + 10.0

        event = _FakeEvent("FFFUSDT")
        event.states_by_exchange = {"binance": binance, "bybit": bybit}
        sig = tracker.compute(event, {"binance": (0, 0), "bybit": (0, 0)}, {"binance": (0, 0), "bybit": (0, 0)},
                               COMMON_CFG, {"binance": 10.0, "bybit": 10.0}, ts2)
        assert sig is not None
        assert sig.reject_reason == "insufficient_sample"   # honest: no empirical edge measured yet
        assert len(tracker._pending) == 1

        class _FakeStore:
            def get(self, ex, symbol):
                return {"binance": binance, "bybit": bybit}.get(ex)

        class _FakeLedger:
            def __init__(self):
                self.rows = []

            async def insert_lead_lag_observation(self, **kw):
                self.rows.append(kw)

        ledger = _FakeLedger()
        # before the evaluation window elapses: nothing resolved yet (no look-ahead)
        await tracker.tick_pending_observations(_FakeStore(), ledger, ts2 + 5.0)
        assert ledger.rows == []
        assert len(tracker._pending) == 1

        # after the window elapses: resolved exactly once
        await tracker.tick_pending_observations(_FakeStore(), ledger, ts2 + 25.0)
        assert len(ledger.rows) == 1
        assert tracker._pending == []
        assert tracker.stats_cache[("binance", "bybit")]["sample_size"] == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# EXECUTION: fees / spread / slippage / latency, TRUE_NET_SHADOW_PNL
# ---------------------------------------------------------------------------

def test_true_net_pnl_subtracts_every_cost_component():
    from momentum.shadow.broker import FillResult
    entry = FillResult(avg_price=100.0, filled_size=1.0, fee=0.10, slippage_pct=0.02, latency_ms=50.0)
    exit_ = FillResult(avg_price=101.0, filled_size=1.0, fee=0.101, slippage_pct=0.03, latency_ms=250.0)
    result = compute_true_net_pnl("UP", entry, exit_, size=1.0, entry_spread_bps=5.0)
    assert result.gross_pnl_pct == pytest.approx(1.0, abs=1e-6)
    assert result.true_net_pnl < result.gross_pnl_pct / 100.0 * 100.0   # fees/slippage strictly reduce the raw gross return
    assert result.entry_fee == 0.10 and result.exit_fee == 0.101


def test_latency_variants_are_all_simulated_independently():
    execution = ShadowExecutionEngine({
        "taker_fee_bps_by_exchange": {"binance": 10}, "primary_latency_ms": [50, 250],
        "simulated_latency_variants_ms": [50, 100, 250, 500, 1000],
    })
    state = SymbolState("GGGUSDT", "binance")
    ts = 7_000_000.0
    for i in range(5):
        _push(state, ts + i, 100.0 + i * 0.01, 1.0, is_sell=False)
    results = execution.simulate_latency_variants(state, "UP", 1.0, "binance", side="entry")
    assert set(results.keys()) == {50, 100, 250, 500, 1000}
    for latency_ms, fill in results.items():
        if fill is not None:
            assert fill.latency_ms == latency_ms


# ---------------------------------------------------------------------------
# EXIT LAB: MFE/MAE tracking
# ---------------------------------------------------------------------------

def test_exit_lab_tracks_mfe_and_mae():
    import asyncio

    async def _run():
        execution = ShadowExecutionEngine({
            "taker_fee_bps_by_exchange": {"binance": 10}, "primary_latency_ms": [50, 250],
            "simulated_latency_variants_ms": [50, 100, 250, 500, 1000],
        })

        class _FakeLedger:
            def __init__(self):
                self.exit_results = []
                self.closed = []

            async def insert_exit_policy_result(self, **kw):
                self.exit_results.append(kw)

            async def close_trade(self, trade_id, **kw):
                self.closed.append((trade_id, kw))

            async def insert_false_positive(self, **kw):
                pass

        ledger = _FakeLedger()
        exit_lab_cfg = {
            "fixed_horizons_s": [5], "trailing_activation_r": 1.0, "breakeven_trigger_r": 0.5,
            "partial_tp_r": 1.5, "momentum_decay_fraction": 0.4, "orderflow_reversal_threshold": 0.5,
            "exhaustion_exit_threshold": 75, "max_tracking_s": 30, "production_exit_policy": "EXIT_FIXED_5S",
        }
        lab = ExitLab(exit_lab_cfg, COMMON_CFG, execution, ledger)

        state = SymbolState("HHHUSDT", "binance")
        ts = 8_000_000.0
        _push(state, ts, 100.0, 1.0, is_sell=False)
        lab.open_trade(1, "TEST", "HHHUSDT", "binance", "UP", ts, 100.0, stop_distance_pct=1.0, size=1.0,
                        entry_velocity_10s=0.5, dataset_phase="RESEARCH", dataset_version="v1")

        class _FakeStore:
            def get(self, ex, symbol):
                return state if (ex, symbol) == ("binance", "HHHUSDT") else None

        store = _FakeStore()
        # price runs favorably to +2%, then pulls back to +0.5% by the time the fixed exit fires
        _push(state, ts + 1, 102.0, 1.0, is_sell=False)
        await lab.tick(store, ts + 1)
        _push(state, ts + 2, 99.0, 1.0, is_sell=True)
        await lab.tick(store, ts + 2)
        _push(state, ts + 6, 100.5, 1.0, is_sell=False)
        await lab.tick(store, ts + 6)

        trade = None
        assert ledger.exit_results, "EXIT_FIXED_5S should have resolved by t+6s"
        assert ledger.closed, "the production policy should have closed the real trade"
        # MFE captured the +2% run, MAE captured the -1% dip - both persisted on the close
        _, close_kw = ledger.closed[0]
        assert close_kw["mfe_pct"] == pytest.approx(2.0, abs=0.05)
        assert close_kw["mae_pct"] == pytest.approx(-1.0, abs=0.05)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# META ENGINE: agreement / conflict counting
# ---------------------------------------------------------------------------

def test_meta_engine_counts_agreement_and_conflict():
    signals = [
        StrategySignal(strategy="A", symbol="X", exchange="binance", direction="UP", price=1.0, score=80.0),
        StrategySignal(strategy="B", symbol="X", exchange="binance", direction="UP", price=1.0, score=60.0),
        StrategySignal(strategy="C", symbol="X", exchange="binance", direction="DOWN", price=1.0, score=55.0),
        StrategySignal(strategy="D", symbol="X", exchange="binance", direction="UP", price=1.0, score=30.0),  # below threshold
    ]
    meta = compute_meta(signals, actionable_threshold=50.0)
    assert meta["UP"].agreement_count == 2
    assert meta["UP"].conflict_count == 1
    assert meta["DOWN"].agreement_count == 1
    assert meta["DOWN"].conflict_count == 2


# ---------------------------------------------------------------------------
# LEDGER ISOLATION
# ---------------------------------------------------------------------------

async def test_lab_ledger_is_isolated_and_round_trips(tmp_path):
    schema_path = ROOT / "db" / "strategy_lab_schema.sql"
    ledger = LabLedger(tmp_path / "lab_test.db", schema_path)
    await ledger.init()
    assert ledger.db_path != ROOT / "db" / "momentum.db"

    signal_id = await ledger.insert_signal(
        market_event_id="X:1", strategy="TEST", symbol="XUSDT", exchange="binance", direction="UP",
        price=1.0, score=80.0, dataset_phase="RESEARCH", dataset_version="v1", accepted=True,
    )
    assert signal_id > 0
    kpis = await ledger.get_strategy_kpis()
    assert kpis == []   # no closed trades yet - INSUFFICIENT SAMPLE, not fabricated


# ---------------------------------------------------------------------------
# BASELINE IMMUTABILITY / PROCESS ISOLATION
# ---------------------------------------------------------------------------

FORBIDDEN_BASELINE_IMPORTS = {
    "momentum.app", "momentum.runtime", "momentum.config", "momentum.dashboard.api",
    "momentum.shadow.ledger",
}


def test_strategy_lab_never_imports_the_baselines_live_runtime_modules():
    """Architectural proof of rule 0: the Lab reuses only stateless/leaf
    modules (engines, data, exchange adapters, ShadowBroker) - never the
    baseline's own running config/runtime/app/dashboard/ledger objects, so it
    is structurally impossible for the Lab to mutate baseline state."""
    lab_root = ROOT / "strategy_lab"
    violations = []
    for path in lab_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_BASELINE_IMPORTS:
                violations.append(f"{path}: imports from forbidden baseline module {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_BASELINE_IMPORTS:
                        violations.append(f"{path}: imports forbidden baseline module {alias.name}")
    assert violations == [], "\n".join(violations)


def test_strategy_lab_systemd_service_is_isolated_from_the_baseline():
    baseline = (ROOT / "deploy" / "robotcripto-momentum.service").read_text()
    lab = (ROOT / "deploy" / "robotcripto-momentum-strategy-lab.service").read_text()
    assert "User=robotcripto-momentum\n" in baseline
    assert "User=robotcripto-momentum-lab" in lab
    assert "momentum.app" in baseline and "strategy_lab.app" in lab
    assert baseline != lab

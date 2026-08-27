"""SQLite-backed ledger for the Strategy Lab - its own database file
(db/strategy_lab.db), completely separate from db/momentum.db. Same
to_thread-wrapped single-writer pattern as momentum/shadow/ledger.py (proven
approach, reused by pattern, not by import - the two ledgers must never share
a connection or a lock).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
import threading
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LabLedger:
    def __init__(self, db_path: pathlib.Path, schema_path: pathlib.Path):
        self.db_path = db_path
        self.schema_path = schema_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        with open(self.schema_path) as f:
            conn.executescript(f.read())
        conn.commit()
        self._conn = conn

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- config version log (section 16) ---------------------------------
    async def log_config_version(self, version: str, phase: str) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT OR IGNORE INTO config_versions (ts, version, phase) VALUES (?, ?, ?)",
            (_now_iso(), version, phase),
        )

    # -- symbols -----------------------------------------------------------
    async def upsert_symbol(self, symbol: str, exchange: str, base_asset: str, quote_asset: str,
                             tick_size: float, step_size: float, min_notional: float,
                             quote_volume_24h: float) -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO lab_symbols (symbol, exchange, base_asset, quote_asset, tick_size, step_size,
                                         min_notional, quote_volume_24h, active, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(symbol, exchange) DO UPDATE SET
                   tick_size=excluded.tick_size, step_size=excluded.step_size,
                   min_notional=excluded.min_notional, quote_volume_24h=excluded.quote_volume_24h,
                   active=1, updated_at=excluded.updated_at""",
            (symbol, exchange, base_asset, quote_asset, tick_size, step_size, min_notional,
             quote_volume_24h, _now_iso()),
        )

    # -- strategy signals ---------------------------------------------------
    async def insert_signal(self, **kw) -> int:
        details = json.dumps(kw.pop("details", {}))
        cur = await asyncio.to_thread(
            self._exec,
            """INSERT INTO strategy_signals
               (ts, market_event_id, strategy, symbol, exchange, direction, price, score, phase,
                spread_bps, exhaustion_risk, late_entry_risk, regime_label, meta_signal_strength,
                agreement_count, conflict_count, expected_move_pct, expected_cost_pct,
                expected_net_edge_pct, accepted, reject_reason, dataset_phase, dataset_version, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now_iso(), kw["market_event_id"], kw["strategy"], kw["symbol"], kw["exchange"],
             kw["direction"], kw["price"], kw["score"], kw.get("phase"), kw.get("spread_bps"),
             kw.get("exhaustion_risk"), kw.get("late_entry_risk"), kw.get("regime_label"),
             kw.get("meta_signal_strength"), kw.get("agreement_count"), kw.get("conflict_count"),
             kw.get("expected_move_pct"), kw.get("expected_cost_pct"), kw.get("expected_net_edge_pct"),
             int(kw.get("accepted", False)), kw.get("reject_reason"), kw["dataset_phase"],
             kw["dataset_version"], details),
        )
        return cur.lastrowid

    # -- strategy trades ------------------------------------------------------
    async def insert_trade(self, **kw) -> int:
        cur = await asyncio.to_thread(
            self._exec,
            """INSERT INTO strategy_trades
               (signal_id, strategy, symbol, exchange, direction, entry_time, entry_price, stop_price,
                size, risk_pct, risk_amount, confirmation_window_s, entry_latency_ms, entry_fee,
                entry_slippage_pct, agreement_count, regime_label, dataset_phase, dataset_version, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
            (kw["signal_id"], kw["strategy"], kw["symbol"], kw["exchange"], kw["direction"], _now_iso(),
             kw["entry_price"], kw["stop_price"], kw["size"], kw["risk_pct"], kw["risk_amount"],
             kw.get("confirmation_window_s"), kw.get("entry_latency_ms"), kw.get("entry_fee"),
             kw.get("entry_slippage_pct"), kw.get("agreement_count"), kw.get("regime_label"),
             kw["dataset_phase"], kw["dataset_version"]),
        )
        return cur.lastrowid

    async def close_trade(self, trade_id: int, **kw) -> None:
        await asyncio.to_thread(
            self._exec,
            """UPDATE strategy_trades SET status='CLOSED', exit_policy=?, exit_time=?, exit_price=?,
                   exit_reason=?, exit_fee=?, exit_slippage_pct=?, exit_latency_ms=?, spread_cost_pct=?,
                   gross_pnl_pct=?, true_net_pnl=?, true_net_pnl_pct=?, r_multiple=?, mfe_pct=?,
                   mae_pct=?, hold_s=?
               WHERE id=?""",
            (kw["exit_policy"], _now_iso(), kw["exit_price"], kw["exit_reason"], kw["exit_fee"],
             kw["exit_slippage_pct"], kw.get("exit_latency_ms"), kw.get("spread_cost_pct"),
             kw["gross_pnl_pct"], kw["true_net_pnl"], kw["true_net_pnl_pct"], kw.get("r_multiple"),
             kw["mfe_pct"], kw["mae_pct"], kw["hold_s"], trade_id),
        )

    async def get_open_trade_ids(self) -> list[int]:
        rows = await asyncio.to_thread(self._query, "SELECT id FROM strategy_trades WHERE status='OPEN'")
        return [r["id"] for r in rows]

    # -- exit policy lab (section 10) ----------------------------------------
    async def insert_exit_policy_result(self, **kw) -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO exit_policy_results
               (trade_id, policy, exit_time, exit_price, hold_s, gross_pnl_pct, true_net_pnl_pct,
                mfe_pct, mae_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (kw["trade_id"], kw["policy"], _now_iso(), kw["exit_price"], kw["hold_s"],
             kw["gross_pnl_pct"], kw["true_net_pnl_pct"], kw["mfe_pct"], kw["mae_pct"]),
        )

    # -- fast entry lab (section 9) -------------------------------------------
    async def insert_confirmation_window_stat(self, **kw) -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO confirmation_window_stats
               (signal_id, strategy, symbol, direction, window_s, still_valid,
                price_move_pct_at_window, would_be_net_edge_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (kw["signal_id"], kw["strategy"], kw["symbol"], kw["direction"], kw["window_s"],
             int(kw["still_valid"]), kw["price_move_pct_at_window"], kw["would_be_net_edge_pct"]),
        )

    # -- missed move / false positive analyzers (sections 12/13) --------------
    async def get_earliest_signal_in_window(self, symbol: str, direction: str, since_iso: str) -> dict | None:
        rows = await asyncio.to_thread(
            self._query,
            """SELECT strategy, ts, score, reject_reason FROM strategy_signals
               WHERE symbol=? AND direction=? AND ts>=? ORDER BY ts ASC LIMIT 5""",
            (symbol, direction, since_iso),
        )
        if not rows:
            return None
        return {"strategy": rows[0]["strategy"], "ts": rows[0]["ts"],
                "scores": [{"strategy": r["strategy"], "score": r["score"], "reject_reason": r["reject_reason"]}
                           for r in rows], "reject_reason": rows[0]["reject_reason"]}

    async def has_recent_trade(self, symbol: str, direction: str, since_iso: str) -> bool:
        rows = await asyncio.to_thread(
            self._query,
            """SELECT 1 FROM strategy_trades WHERE symbol=? AND direction=?
               AND (status='OPEN' OR entry_time>=?) LIMIT 1""",
            (symbol, direction, since_iso),
        )
        return bool(rows)

    async def insert_missed_move(self, **kw) -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO missed_moves
               (ts, symbol, exchange, direction, move_pct, move_window_s, first_detectable_ts,
                first_detectable_scores, reject_reason, what_happened_next)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now_iso(), kw["symbol"], kw["exchange"], kw["direction"], kw["move_pct"],
             kw["move_window_s"], kw.get("first_detectable_ts"),
             json.dumps(kw.get("first_detectable_scores", {})), kw.get("reject_reason"),
             json.dumps(kw.get("what_happened_next", {}))),
        )

    async def insert_false_positive(self, **kw) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO false_positives (trade_id, ts, classification, details) VALUES (?, ?, ?, ?)",
            (kw["trade_id"], _now_iso(), kw["classification"], json.dumps(kw.get("details", {}))),
        )

    # -- cross-exchange lead/lag (section 7) -----------------------------------
    async def insert_lead_lag_observation(self, **kw) -> int:
        cur = await asyncio.to_thread(
            self._exec,
            """INSERT INTO lead_lag_observations
               (ts, symbol, direction, leading_exchange, following_exchange, lead_time_ms,
                price_propagation_pct, velocity_propagation_ratio, volume_propagation_ratio,
                follower_net_expectancy_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now_iso(), kw["symbol"], kw["direction"], kw["leading_exchange"], kw["following_exchange"],
             kw["lead_time_ms"], kw.get("price_propagation_pct"), kw.get("velocity_propagation_ratio"),
             kw.get("volume_propagation_ratio"), kw.get("follower_net_expectancy_pct")),
        )
        return cur.lastrowid

    async def get_lead_lag_stats(self, leading_exchange: str, following_exchange: str) -> dict:
        rows = await asyncio.to_thread(
            self._query,
            """SELECT follower_net_expectancy_pct FROM lead_lag_observations
               WHERE leading_exchange=? AND following_exchange=? AND follower_net_expectancy_pct IS NOT NULL""",
            (leading_exchange, following_exchange),
        )
        vals = [r["follower_net_expectancy_pct"] for r in rows]
        n = len(vals)
        if n == 0:
            return {"sample_size": 0, "avg_net_expectancy_pct": None, "success_rate": None}
        return {
            "sample_size": n,
            "avg_net_expectancy_pct": sum(vals) / n,
            "success_rate": sum(1 for v in vals if v > 0) / n,
        }

    # -- perf monitoring --------------------------------------------------------
    async def record_engine_run(self, symbols_scanned: int, symbols_full_pass: int, duration_ms: float,
                                 cpu_percent: float | None, rss_mb: float | None,
                                 event_loop_lag_ms: float | None, degraded_mode: bool,
                                 market_events: int) -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO lab_engine_runs (ts, symbols_scanned, symbols_full_pass, duration_ms,
                                             cpu_percent, rss_mb, event_loop_lag_ms, degraded_mode,
                                             market_events)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now_iso(), symbols_scanned, symbols_full_pass, duration_ms, cpu_percent, rss_mb,
             event_loop_lag_ms, int(degraded_mode), market_events),
        )

    # -- dashboard/report queries ------------------------------------------------
    async def get_strategy_kpis(self) -> list[dict]:
        rows = await asyncio.to_thread(
            self._query,
            """SELECT strategy,
                      COUNT(*) AS trades,
                      SUM(CASE WHEN true_net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN true_net_pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                      AVG(true_net_pnl) AS avg_net_pnl,
                      SUM(true_net_pnl) AS gross_pnl_sum,
                      AVG(CASE WHEN true_net_pnl > 0 THEN true_net_pnl END) AS avg_win,
                      AVG(CASE WHEN true_net_pnl <= 0 THEN true_net_pnl END) AS avg_loss,
                      AVG(hold_s) AS avg_hold_s,
                      AVG(mfe_pct) AS avg_mfe_pct,
                      AVG(mae_pct) AS avg_mae_pct
               FROM strategy_trades WHERE status='CLOSED' GROUP BY strategy""",
        )
        out = []
        for r in rows:
            d = dict(r)
            n = d["trades"] or 0
            wins = d["wins"] or 0
            gross_win = (d["avg_win"] or 0) * wins
            gross_loss = abs((d["avg_loss"] or 0) * (d["losses"] or 0))
            d["win_rate"] = (wins / n) if n else None
            d["expectancy"] = d["avg_net_pnl"]
            d["profit_factor"] = (gross_win / gross_loss) if gross_loss > 0 else None
            d["sample_size"] = n
            out.append(d)
        return out

    async def get_signal_counts_by_strategy(self) -> dict[str, int]:
        rows = await asyncio.to_thread(
            self._query, "SELECT strategy, COUNT(*) AS n FROM strategy_signals GROUP BY strategy"
        )
        return {r["strategy"]: r["n"] for r in rows}

    async def get_recent_trades(self, limit: int = 30) -> list[dict]:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM strategy_trades ORDER BY id DESC LIMIT ?", (limit,),
        )
        return [dict(r) for r in rows]

    async def get_confirmation_window_lab(self) -> list[dict]:
        rows = await asyncio.to_thread(
            self._query,
            """SELECT strategy, window_s,
                      COUNT(*) AS n,
                      AVG(CASE WHEN still_valid THEN 1.0 ELSE 0.0 END) AS still_valid_rate,
                      AVG(would_be_net_edge_pct) AS avg_would_be_net_edge_pct
               FROM confirmation_window_stats GROUP BY strategy, window_s ORDER BY strategy, window_s""",
        )
        return [dict(r) for r in rows]

    async def get_exit_policy_lab(self) -> list[dict]:
        rows = await asyncio.to_thread(
            self._query,
            """SELECT policy,
                      COUNT(*) AS n,
                      AVG(true_net_pnl_pct) AS avg_true_net_pnl_pct,
                      AVG(hold_s) AS avg_hold_s,
                      AVG(mfe_pct) AS avg_mfe_pct,
                      AVG(mae_pct) AS avg_mae_pct
               FROM exit_policy_results GROUP BY policy ORDER BY avg_true_net_pnl_pct DESC""",
        )
        return [dict(r) for r in rows]

    async def get_recent_missed_moves(self, limit: int = 20) -> list[dict]:
        rows = await asyncio.to_thread(self._query, "SELECT * FROM missed_moves ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    async def get_recent_false_positives(self, limit: int = 20) -> list[dict]:
        rows = await asyncio.to_thread(
            self._query,
            """SELECT fp.*, t.strategy, t.symbol FROM false_positives fp
               JOIN strategy_trades t ON t.id = fp.trade_id ORDER BY fp.id DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def get_agreement_cohort_stats(self) -> list[dict]:
        rows = await asyncio.to_thread(
            self._query,
            """SELECT t.agreement_count,
                      COUNT(*) AS trades,
                      AVG(t.true_net_pnl) AS expectancy,
                      SUM(CASE WHEN t.true_net_pnl > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS win_rate
               FROM strategy_trades t WHERE t.status='CLOSED' AND t.agreement_count IS NOT NULL
               GROUP BY t.agreement_count ORDER BY t.agreement_count""",
        )
        return [dict(r) for r in rows]

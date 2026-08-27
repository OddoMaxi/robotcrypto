"""SQLite-backed ledger: signals (including rejected ones, per section 17),
shadow trades, twin snapshots, engine run audit rows. Single writer lock since
sqlite3 connections aren't safely shared across threads without one; all calls
go through asyncio.to_thread so the event loop never blocks on disk I/O.
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


class Ledger:
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

    # -- symbols ---------------------------------------------------------
    async def upsert_symbol(self, symbol: str, exchange: str, base_asset: str, quote_asset: str,
                             tick_size: float, step_size: float, min_notional: float,
                             quote_volume_24h: float) -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO symbols (symbol, exchange, base_asset, quote_asset, tick_size, step_size,
                                     min_notional, quote_volume_24h, active, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(symbol, exchange) DO UPDATE SET
                   tick_size=excluded.tick_size, step_size=excluded.step_size,
                   min_notional=excluded.min_notional, quote_volume_24h=excluded.quote_volume_24h,
                   active=1, updated_at=excluded.updated_at""",
            (symbol, exchange, base_asset, quote_asset, tick_size, step_size, min_notional,
             quote_volume_24h, _now_iso()),
        )

    # -- signals -----------------------------------------------------------
    async def insert_signal(self, *, symbol: str, exchange: str, direction: str, price: float,
                             spread_bps: float | None, engine_scores: dict, momentum_confidence: float,
                             exhaustion_risk: float, classification: str, entry_quality: float | None,
                             entry_type: str | None, accepted: bool, reject_reason: str | None,
                             shadow_only: bool) -> int:
        cur = await asyncio.to_thread(
            self._exec,
            """INSERT INTO signals (ts, symbol, exchange, direction, price, spread_bps, engine_scores,
                                     momentum_confidence, exhaustion_risk, classification, entry_quality,
                                     entry_type, accepted, reject_reason, shadow_only)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now_iso(), symbol, exchange, direction, price, spread_bps, json.dumps(engine_scores),
             momentum_confidence, exhaustion_risk, classification, entry_quality, entry_type,
             int(accepted), reject_reason, int(shadow_only)),
        )
        return cur.lastrowid

    # -- shadow trades -------------------------------------------------------
    async def insert_shadow_trade(self, *, signal_id: int, symbol: str, exchange: str, direction: str,
                                   entry_price: float, invalidation_price: float, stop_price: float,
                                   size: float, risk_pct: float, risk_amount: float, entry_type: str,
                                   fees_paid: float, slippage_pct: float, latency_ms: float) -> int:
        cur = await asyncio.to_thread(
            self._exec,
            """INSERT INTO shadow_trades (signal_id, symbol, exchange, direction, entry_time, entry_price,
                                           invalidation_price, stop_price, size, risk_pct, risk_amount,
                                           entry_type, status, trailing_state, fees_paid, slippage_pct, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 'INITIAL', ?, ?, ?)""",
            (signal_id, symbol, exchange, direction, _now_iso(), entry_price, invalidation_price,
             stop_price, size, risk_pct, risk_amount, entry_type, fees_paid, slippage_pct, latency_ms),
        )
        return cur.lastrowid

    async def update_open_trade(self, trade_id: int, *, stop_price: float, trailing_state: str,
                                 mfe_pct: float, mae_pct: float) -> None:
        await asyncio.to_thread(
            self._exec,
            """UPDATE shadow_trades SET stop_price=?, trailing_state=?, mfe_pct=?, mae_pct=?
               WHERE id=?""",
            (stop_price, trailing_state, mfe_pct, mae_pct, trade_id),
        )

    async def close_trade(self, trade_id: int, *, exit_price: float, exit_reason: str, fees_paid: float,
                           slippage_pct: float, net_pnl: float, r_multiple: float) -> None:
        await asyncio.to_thread(
            self._exec,
            """UPDATE shadow_trades SET status='CLOSED', exit_time=?, exit_price=?, exit_reason=?,
                                        fees_paid=fees_paid+?, slippage_pct=?, net_pnl=?, r_multiple=?
               WHERE id=?""",
            (_now_iso(), exit_price, exit_reason, fees_paid, slippage_pct, net_pnl, r_multiple, trade_id),
        )

    async def get_open_trades(self) -> list[dict]:
        rows = await asyncio.to_thread(self._query, "SELECT * FROM shadow_trades WHERE status='OPEN'")
        return [dict(r) for r in rows]

    async def get_active_trades_view(self, limit: int = 50) -> list[dict]:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM shadow_trades WHERE status='OPEN' ORDER BY entry_time DESC LIMIT ?", (limit,),
        )
        return [dict(r) for r in rows]

    async def get_trade_detail(self, trade_id: int) -> dict | None:
        rows = await asyncio.to_thread(self._query, "SELECT * FROM shadow_trades WHERE id=?", (trade_id,))
        if not rows:
            return None
        trade = dict(rows[0])
        sig_rows = await asyncio.to_thread(self._query, "SELECT * FROM signals WHERE id=?", (trade["signal_id"],))
        trade["signal"] = dict(sig_rows[0]) if sig_rows else None
        if trade["signal"] and trade["signal"].get("engine_scores"):
            trade["signal"]["engine_scores"] = json.loads(trade["signal"]["engine_scores"])
        twin_rows = await asyncio.to_thread(
            self._query, "SELECT * FROM twin_snapshots WHERE signal_id=? ORDER BY horizon_s", (trade["signal_id"],)
        )
        trade["twin_snapshots"] = [dict(r) for r in twin_rows]
        return trade

    # -- digital twin --------------------------------------------------------
    async def insert_twin_snapshot(self, *, signal_id: int, horizon_s: int, price: float, pct_change: float,
                                    mfe_pct: float, mae_pct: float, ts: float) -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO twin_snapshots (signal_id, horizon_s, price, pct_change, mfe_pct, mae_pct, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, horizon_s, price, pct_change, mfe_pct, mae_pct, ts),
        )

    # -- engine run audit -----------------------------------------------------
    async def record_engine_run(self, symbols_scanned: int, symbols_promoted: int, duration_ms: float) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO engine_runs (ts, symbols_scanned, symbols_promoted, duration_ms) VALUES (?, ?, ?, ?)",
            (_now_iso(), symbols_scanned, symbols_promoted, duration_ms),
        )

    # -- reads for dashboard / KPIs -----------------------------------------
    async def get_recent_signals(self, limit: int = 50, classification: str | None = None) -> list[dict]:
        if classification:
            rows = await asyncio.to_thread(
                self._query,
                "SELECT * FROM signals WHERE classification=? ORDER BY ts DESC LIMIT ?",
                (classification, limit),
            )
        else:
            rows = await asyncio.to_thread(self._query, "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            if d.get("engine_scores"):
                d["engine_scores"] = json.loads(d["engine_scores"])
            out.append(d)
        return out

    async def get_kpis(self) -> dict:
        def _compute() -> dict:
            with self._lock:
                conn = self._conn
                total_signals = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
                qualified = conn.execute(
                    "SELECT COUNT(*) c FROM signals WHERE classification NOT IN ('IGNORE','WATCH','EXHAUSTED')"
                ).fetchone()["c"]
                trades = conn.execute("SELECT COUNT(*) c FROM shadow_trades").fetchone()["c"]
                closed = conn.execute(
                    "SELECT * FROM shadow_trades WHERE status='CLOSED'"
                ).fetchall()

            closed = [dict(r) for r in closed]
            n = len(closed)
            wins = [t for t in closed if (t["net_pnl"] or 0) > 0]
            losses = [t for t in closed if (t["net_pnl"] or 0) <= 0]
            win_rate = (len(wins) / n * 100.0) if n else 0.0
            avg_win = (sum(t["net_pnl"] for t in wins) / len(wins)) if wins else 0.0
            avg_loss = (sum(t["net_pnl"] for t in losses) / len(losses)) if losses else 0.0
            gross_profit = sum(t["net_pnl"] for t in wins) if wins else 0.0
            gross_loss = abs(sum(t["net_pnl"] for t in losses)) if losses else 0.0
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
            expectancy = (sum(t["net_pnl"] for t in closed) / n) if n else 0.0
            net_pnl = sum(t["net_pnl"] for t in closed) if closed else 0.0
            total_fees = sum(t["fees_paid"] or 0 for t in closed)
            avg_slippage = (sum(t["slippage_pct"] or 0 for t in closed) / n) if n else 0.0

            # simple running-equity max drawdown over closed trades in entry order
            equity = 0.0
            peak = 0.0
            max_dd = 0.0
            for t in sorted(closed, key=lambda x: x["entry_time"]):
                equity += t["net_pnl"] or 0.0
                peak = max(peak, equity)
                max_dd = max(max_dd, peak - equity)

            return {
                "total_signals": total_signals,
                "qualified_signals": qualified,
                "trades": trades,
                "closed_trades": n,
                "win_rate_pct": win_rate,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "expectancy": expectancy,
                "profit_factor": profit_factor,
                "max_drawdown": max_dd,
                "total_fees": total_fees,
                "avg_slippage_pct": avg_slippage,
                "net_pnl": net_pnl,
            }

        return await asyncio.to_thread(_compute)

    async def get_kpis_by_tag(self, tag_column: str) -> list[dict]:
        """Mission 8 strategy separation: KPIs grouped by entry_type or direction,
        never pooling PnL across groups. tag_column must be a known safe column
        name (not user input) - callers pass a literal, never external data."""
        assert tag_column in ("entry_type", "direction")

        def _compute() -> list[dict]:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT * FROM shadow_trades WHERE status='CLOSED' AND {tag_column} IS NOT NULL"
                ).fetchall()
            by_tag: dict[str, list[dict]] = {}
            for r in rows:
                d = dict(r)
                by_tag.setdefault(d[tag_column], []).append(d)

            out = []
            for tag, trades in by_tag.items():
                n = len(trades)
                wins = [t for t in trades if (t["net_pnl"] or 0) > 0]
                net_pnl = sum(t["net_pnl"] or 0 for t in trades)
                out.append({
                    "tag": tag, "trades": n,
                    "win_rate_pct": (len(wins) / n * 100.0) if n else 0.0,
                    "expectancy": (net_pnl / n) if n else 0.0,
                    "net_pnl": net_pnl,
                })
            return out

        return await asyncio.to_thread(_compute)

    # -- early movers (missions 7/8) -----------------------------------------
    async def insert_early_mover_event(self, *, symbol: str, exchange: str, direction: str,
                                        t0_price: float, t0_confidence: float) -> int:
        cur = await asyncio.to_thread(
            self._exec,
            """INSERT INTO early_mover_events (ts, symbol, exchange, direction, t0_price, t0_confidence,
                                                max_confidence, time_to_peak_s, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'TRACKING')""",
            (_now_iso(), symbol, exchange, direction, t0_price, t0_confidence, t0_confidence),
        )
        return cur.lastrowid

    async def insert_early_mover_return(self, *, event_id: int, horizon_s: int, pct_change: float, ts: float) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO early_mover_returns (event_id, horizon_s, pct_change, ts) VALUES (?, ?, ?, ?)",
            (event_id, horizon_s, pct_change, ts),
        )

    async def finalize_early_mover_event(self, event_id: int, *, max_confidence: float, time_to_peak_s: float,
                                          mfe_pct: float, mae_pct: float) -> None:
        await asyncio.to_thread(
            self._exec,
            """UPDATE early_mover_events SET status='DONE', max_confidence=?, time_to_peak_s=?,
                                              mfe_pct=?, mae_pct=? WHERE id=?""",
            (max_confidence, time_to_peak_s, mfe_pct, mae_pct, event_id),
        )

    async def get_recent_early_movers(self, direction: str | None = None, limit: int = 20) -> list[dict]:
        if direction:
            rows = await asyncio.to_thread(
                self._query,
                "SELECT * FROM early_mover_events WHERE direction=? ORDER BY ts DESC LIMIT ?",
                (direction, limit),
            )
        else:
            rows = await asyncio.to_thread(
                self._query, "SELECT * FROM early_mover_events ORDER BY ts DESC LIMIT ?", (limit,)
            )
        events = [dict(r) for r in rows]
        for e in events:
            returns = await asyncio.to_thread(
                self._query, "SELECT horizon_s, pct_change FROM early_mover_returns WHERE event_id=? ORDER BY horizon_s",
                (e["id"],),
            )
            e["returns"] = {r["horizon_s"]: r["pct_change"] for r in returns}
        return events

    # -- leader/lag (mission 4) -----------------------------------------------
    async def insert_leader_lag_event(self, *, symbol: str, leading_exchange: str, following_exchange: str,
                                       lead_time_ms: float) -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO leader_lag_events (ts, symbol, leading_exchange, following_exchange, lead_time_ms)
               VALUES (?, ?, ?, ?, ?)""",
            (_now_iso(), symbol, leading_exchange, following_exchange, lead_time_ms),
        )

    async def get_leader_lag_stats(self) -> list[dict]:
        """Statistical aggregation only (mission 4): how often, and by how much,
        each exchange has led another, across all observations so far."""
        def _compute() -> list[dict]:
            with self._lock:
                rows = self._conn.execute(
                    """SELECT leading_exchange, COUNT(*) as lead_count, AVG(lead_time_ms) as avg_lead_time_ms
                       FROM leader_lag_events GROUP BY leading_exchange ORDER BY lead_count DESC"""
                ).fetchall()
            return [dict(r) for r in rows]

        return await asyncio.to_thread(_compute)

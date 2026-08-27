"""Strategy Lab dashboard API (spec section 17) - own FastAPI app, own port
(8802 by default), own static page. Read-only: nothing here can trigger a
trade or touch an exchange. Does not import or extend momentum/dashboard/api.py
- a separate app entirely, so the baseline dashboard is never at risk of being
broken by a Lab change.
"""
from __future__ import annotations

import pathlib
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"


def create_app(runtime) -> FastAPI:
    app = FastAPI(title="Momentum Strategy Lab V2 Dashboard")

    @app.get("/api/status")
    async def status():
        return {
            "mode": "SHADOW" if runtime.shadow_mode else "LIVE",
            "real_orders": runtime.real_orders,
            "uptime_s": time.time() - runtime.start_time,
            "exchanges": runtime.exchanges,
            "universe_size": runtime.universe_size,
            "universe_size_by_exchange": runtime.universe_size_by_exchange,
            "symbols_tracked": len(runtime.tracked_symbols),
            "symbols_tracked_by_exchange": {ex: len(s) for ex, s in runtime.tracked_symbols_by_exchange.items()},
            "market_events_total": runtime.market_events_total,
            "open_trade_count": runtime.open_trade_count,
            "dataset_phase": runtime.dataset_phase,
            "dataset_version": runtime.dataset_version,
            "compute_budget": runtime.last_compute_budget,
        }

    @app.get("/api/exchanges")
    async def exchanges():
        return {"exchanges": [h.to_dict() for h in runtime.health.all().values()]}

    @app.get("/api/live_market")
    async def live_market():
        return runtime.live_market_snapshot

    @app.get("/api/strategy_kpis")
    async def strategy_kpis():
        kpis = await runtime.ledger.get_strategy_kpis()
        signal_counts = await runtime.ledger.get_signal_counts_by_strategy()
        by_strategy = {k["strategy"]: k for k in kpis}
        rows = []
        for strategy in sorted(set(list(signal_counts) + list(by_strategy))):
            k = by_strategy.get(strategy, {})
            n = k.get("sample_size", 0) or 0
            rows.append({
                "strategy": strategy,
                "signals": signal_counts.get(strategy, 0),
                "trades": n,
                "win_rate": k.get("win_rate"),
                "net_pnl": k.get("gross_pnl_sum"),
                "expectancy": k.get("expectancy"),
                "profit_factor": k.get("profit_factor"),
                "avg_hold_s": k.get("avg_hold_s"),
                "avg_mfe_pct": k.get("avg_mfe_pct"),
                "avg_mae_pct": k.get("avg_mae_pct"),
                "status": "INSUFFICIENT_SAMPLE" if n < 20 else "ACTIVE",
            })
        return {"strategies": rows}

    @app.get("/api/agreement_cohorts")
    async def agreement_cohorts():
        rows = await runtime.ledger.get_agreement_cohort_stats()
        for r in rows:
            r["status"] = "INSUFFICIENT_SAMPLE" if (r.get("trades") or 0) < 20 else "ACTIVE"
        return {"cohorts": rows}

    @app.get("/api/recent_trades")
    async def recent_trades(limit: int = 30):
        return {"trades": await runtime.ledger.get_recent_trades(limit)}

    @app.get("/api/missed_moves")
    async def missed_moves(limit: int = 20):
        return {"missed_moves": await runtime.ledger.get_recent_missed_moves(limit)}

    @app.get("/api/false_positives")
    async def false_positives(limit: int = 20):
        return {"false_positives": await runtime.ledger.get_recent_false_positives(limit)}

    @app.get("/api/confirmation_window_lab")
    async def confirmation_window_lab():
        return {"windows": await runtime.ledger.get_confirmation_window_lab()}

    @app.get("/api/exit_policy_lab")
    async def exit_policy_lab():
        return {"policies": await runtime.ledger.get_exit_policy_lab()}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    return app

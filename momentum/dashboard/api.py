"""Dashboard API (spec sections 19-21). Read-only: every endpoint here reads
already-computed runtime/ledger state - nothing in this module can trigger a
trade or touch an exchange.
"""
from __future__ import annotations

import pathlib
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"


def create_app(runtime) -> FastAPI:
    app = FastAPI(title="Momentum Engine Dashboard")

    @app.get("/api/status")
    async def status():
        return {
            "mode": "SHADOW" if runtime.shadow_mode else "LIVE",
            "real_orders": runtime.real_orders,
            "uptime_s": time.time() - runtime.start_time,
            "exchanges": runtime.exchanges,
            "universe_size": runtime.universe_size,
            "symbols_tracked": len(runtime.tracked_symbols),
            "symbols_promoted": len(runtime.promoted),
            "digital_twin_pending": runtime.digital_twin.pending_count,
            "last_stage_a_cycle_symbols": runtime.last_stage_a_scanned,
            "cross_exchange_confirmation": "N/A (binance-only slice)",
        }

    @app.get("/api/movers")
    async def movers():
        candidates = list(runtime.promoted.values())
        up_sorted = sorted(candidates, key=lambda c: c["up"]["confidence"], reverse=True)[:10]
        down_sorted = sorted(candidates, key=lambda c: c["down"]["confidence"], reverse=True)[:10]
        return {"up": up_sorted, "down": down_sorted}

    @app.get("/api/early_movers")
    async def early_movers():
        candidates = list(runtime.promoted.values())
        ranked = sorted(candidates, key=lambda c: c.get("score_evolution", 0), reverse=True)[:15]
        return {"early_movers": ranked}

    @app.get("/api/trades")
    async def trades():
        return {"trades": await runtime.ledger.get_active_trades_view()}

    @app.get("/api/trades/{trade_id}")
    async def trade_detail(trade_id: int):
        detail = await runtime.ledger.get_trade_detail(trade_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="trade not found")
        return detail

    @app.get("/api/kpis")
    async def kpis():
        return await runtime.ledger.get_kpis()

    @app.get("/api/signals")
    async def signals(classification: str | None = None, limit: int = 50):
        return {"signals": await runtime.ledger.get_recent_signals(limit=limit, classification=classification)}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    return app

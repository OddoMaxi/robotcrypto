"""MULTI-TIMEFRAME ENGINE (spec section 5).

Checks whether the short-term move has context behind it across 1m/3m/5m/15m,
without requiring all of them to agree - it's a weight of evidence, not a gate.
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore

CONTEXT_HORIZONS = (60, 180, 300, 900)  # 1m, 3m, 5m, 15m


def compute(state: SymbolState, now: float) -> EngineScore:
    vels = [state.velocity_pct(now, h) for h in CONTEXT_HORIZONS]
    valid = [v for v in vels if v is not None]
    if not valid:
        return EngineScore(0.0, 0.0, {"reason": "insufficient_history"})

    aligned_up = sum(1 for v in valid if v > 0)
    aligned_down = sum(1 for v in valid if v < 0)

    up = aligned_up / len(valid) * 100.0
    down = aligned_down / len(valid) * 100.0

    return EngineScore(
        up=up,
        down=down,
        details={"velocities": dict(zip(CONTEXT_HORIZONS, vels)),
                 "aligned_up": aligned_up, "aligned_down": aligned_down, "n": len(valid)},
    )

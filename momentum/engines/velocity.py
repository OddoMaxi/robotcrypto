"""PRICE VELOCITY ENGINE (spec section 5).

Detects the rate of price change across a short ladder of horizons and rewards
the accelerating pattern called out in the spec: +0.02% -> +0.08% -> +0.20% ->
+0.35%. A single-horizon move gets a base score; a monotonically building
sequence across horizons gets a bonus.
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore

HORIZONS = (5, 10, 15, 30)
FULL_SCORE_PCT = 0.6  # a +0.6% move over 30s maps to a raw score of 100


def compute(state: SymbolState, now: float) -> EngineScore:
    vels = {h: state.velocity_pct(now, h) for h in HORIZONS}
    if any(v is None for v in vels.values()):
        return EngineScore(0.0, 0.0, {"reason": "insufficient_history"})

    v5, v10, v15, v30 = (vels[h] for h in HORIZONS)

    up_raw = max(0.0, v30) / FULL_SCORE_PCT * 100.0
    down_raw = max(0.0, -v30) / FULL_SCORE_PCT * 100.0

    building_up = v5 <= v10 <= v15 <= v30 and v30 > 0
    building_down = v5 >= v10 >= v15 >= v30 and v30 < 0

    up = up_raw * (1.2 if building_up else 1.0)
    down = down_raw * (1.2 if building_down else 1.0)

    return EngineScore(
        up=up,
        down=down,
        details={"v5": v5, "v10": v10, "v15": v15, "v30": v30,
                 "building_up": building_up, "building_down": building_down},
    )

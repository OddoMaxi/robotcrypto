"""BREAKOUT ENGINE (spec section 5).

Detects a break of the recent local high/low or the VWAP volatility band, and
explicitly scores a volume-confirmed breakout higher than an unconfirmed one -
"a breakout without volume must not receive the same score as a confirmed one".
"""
from __future__ import annotations

from momentum.data.state import SymbolState
from momentum.engines.types import EngineScore

RANGE_S = 300          # 5m local high/low reference
VWAP_WINDOW_S = 300
VOLUME_CONFIRM_RATIO = 1.5  # recent/baseline volume rate needed for "confirmed"
BASE_SCORE = 55.0
CONFIRM_BONUS = 35.0


def compute(state: SymbolState, now: float) -> EngineScore:
    price = state.price_now()
    local_high = state.local_high(now, RANGE_S)
    local_low = state.local_low(now, RANGE_S)
    vwap = state.vwap(now, VWAP_WINDOW_S)
    vol = state.realized_vol(now, VWAP_WINDOW_S)

    if price is None or local_high is None or local_low is None:
        return EngineScore(0.0, 0.0, {"reason": "insufficient_history"})

    baseline_rate = state.total_volume(now, 300) / 300
    recent_rate = state.total_volume(now, 10) / 10
    volume_ratio = (recent_rate / baseline_rate) if baseline_rate > 0 else 0.0
    confirmed = volume_ratio >= VOLUME_CONFIRM_RATIO

    upper_band = (vwap * (1 + (vol or 0) / 100.0 * 2)) if vwap else None
    lower_band = (vwap * (1 - (vol or 0) / 100.0 * 2)) if vwap else None

    breakout_up = price > local_high or (upper_band is not None and price > upper_band)
    breakout_down = price < local_low or (lower_band is not None and price < lower_band)

    up = down = 0.0
    if breakout_up:
        up = BASE_SCORE + (CONFIRM_BONUS if confirmed else 0.0)
    if breakout_down:
        down = BASE_SCORE + (CONFIRM_BONUS if confirmed else 0.0)

    return EngineScore(
        up=up,
        down=down,
        details={
            "local_high": local_high, "local_low": local_low, "vwap": vwap,
            "volume_ratio": volume_ratio, "confirmed": confirmed,
            "breakout_up": breakout_up, "breakout_down": breakout_down,
        },
    )

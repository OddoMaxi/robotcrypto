from __future__ import annotations

from dataclasses import dataclass, field


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


@dataclass(slots=True)
class EngineScore:
    """Every directional engine returns this: an UP score and a DOWN score, each
    0-100, plus raw metrics for the dashboard/trade-detail 'why' view."""
    up: float
    down: float
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        self.up = _clamp(self.up)
        self.down = _clamp(self.down)


@dataclass(slots=True)
class ExhaustionScore:
    up_risk: float    # risk of buying an already-exhausted UP move
    down_risk: float  # risk of shorting an already-exhausted DOWN move
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        self.up_risk = _clamp(self.up_risk)
        self.down_risk = _clamp(self.down_risk)


@dataclass(slots=True)
class RegimeContext:
    btc_velocity_60s: float | None
    eth_velocity_60s: float | None
    breadth_pct_up: float | None       # % of tracked universe with positive 60s velocity
    breadth_pct_down: float | None
    cross_exchange_confirmed_fraction: float  # 1.0 stub in Binance-only V1 (see regime.py)
    bias_up: float    # multiplier applied to UP confidence, near 1.0 = neutral
    bias_down: float
    details: dict = field(default_factory=dict)

"""MARKET REGIME ENGINE (spec section 5, V1.1 mission 10).

Reads BTC/ETH's own velocity, breadth (what fraction of the tracked universe
is currently net up/down), and BTC's own volatility expansion to produce (a)
a small confidence bias multiplier and (b) a human-readable regime_label for
the dashboard/dataset. Neither is a hard filter - an altcoin can still show
genuine idiosyncratic momentum during a BROAD_DOWNTREND or CHOP regime; this
only shifts confidence, it never blocks a signal outright.

Real cross-exchange confirmation is its own weighted engine (see
cross_exchange.py) - `cross_exchange_confirmed_fraction` on RegimeContext is
legacy/unused from the V1 single-exchange slice and kept only for schema
stability.
"""
from __future__ import annotations

from momentum.data.state import StateStore
from momentum.engines.types import RegimeContext

BREADTH_HORIZON_S = 60
VOLATILITY_BASELINE_S = 300
MAX_BIAS_ADJUST = 0.15  # bias multipliers stay within [1-x, 1+x]


def _classify(btc_vel: float | None, breadth_up: float | None, breadth_down: float | None,
              btc_vol_recent: float | None, btc_vol_baseline: float | None) -> str:
    if btc_vel is None:
        return "UNKNOWN"

    if btc_vol_recent is not None and btc_vol_baseline and btc_vol_baseline > 0:
        if btc_vol_recent / btc_vol_baseline >= 2.5:
            if btc_vel <= -1.0 and (breadth_down or 0) >= 70:
                return "PANIC"
            return "VOLATILITY_EXPANSION"

    if btc_vel >= 0.2 and (breadth_up or 0) >= 55:
        return "BROAD_UPTREND"
    if btc_vel <= -0.2 and (breadth_down or 0) >= 55:
        return "BROAD_DOWNTREND"
    if abs(btc_vel) < 0.1 and breadth_up is not None and breadth_down is not None \
            and abs(breadth_up - breadth_down) < 15:
        return "CHOP"
    return "MARKET_RISK_ON" if btc_vel > 0 else "MARKET_RISK_OFF"


def compute(store: StateStore, exchange: str, now: float, tracked_symbols: list[str]) -> RegimeContext:
    btc = store.get(exchange, "BTCUSDT")
    eth = store.get(exchange, "ETHUSDT")
    btc_vel = btc.velocity_pct(now, BREADTH_HORIZON_S) if btc else None
    eth_vel = eth.velocity_pct(now, BREADTH_HORIZON_S) if eth else None
    btc_vol_recent = btc.realized_vol(now, BREADTH_HORIZON_S) if btc else None
    btc_vol_baseline = btc.realized_vol(now, VOLATILITY_BASELINE_S) if btc else None

    up_count = 0
    down_count = 0
    counted = 0
    for sym in tracked_symbols:
        st = store.get(exchange, sym)
        if st is None:
            continue
        v = st.velocity_pct(now, BREADTH_HORIZON_S)
        if v is None:
            continue
        counted += 1
        if v > 0:
            up_count += 1
        elif v < 0:
            down_count += 1

    breadth_up = (up_count / counted * 100.0) if counted else None
    breadth_down = (down_count / counted * 100.0) if counted else None

    # bias: reward UP candidates more when breadth/BTC/ETH are also up, and vice versa
    bias_up = 1.0
    bias_down = 1.0
    signals = [x for x in (btc_vel, eth_vel) if x is not None]
    if signals:
        avg_major = sum(signals) / len(signals)
        adjust = max(-MAX_BIAS_ADJUST, min(MAX_BIAS_ADJUST, avg_major / 1.0 * MAX_BIAS_ADJUST))
        bias_up = 1.0 + adjust
        bias_down = 1.0 - adjust

    regime_label = _classify(btc_vel, breadth_up, breadth_down, btc_vol_recent, btc_vol_baseline)

    return RegimeContext(
        btc_velocity_60s=btc_vel,
        eth_velocity_60s=eth_vel,
        breadth_pct_up=breadth_up,
        breadth_pct_down=breadth_down,
        cross_exchange_confirmed_fraction=1.0,
        bias_up=max(0.85, min(1.15, bias_up)),
        bias_down=max(0.85, min(1.15, bias_down)),
        regime_label=regime_label,
        details={"counted": counted, "btc_vol_recent": btc_vol_recent, "btc_vol_baseline": btc_vol_baseline},
    )

"""MARKET REGIME ENGINE (spec section 5) + CROSS-EXCHANGE CONFIRMATION stub
(section 5/22).

Reads BTC/ETH's own velocity and breadth (what fraction of the tracked universe
is currently net up/down) to produce a small bias multiplier - never a hard
filter - so an altcoin's move is read differently during a generalized rally
vs a violent BTC dump.

Cross-exchange confirmation and leader/lag detection (sections 5/22) require
Bybit/OKX data that this Binance-only slice doesn't have yet. The fields exist
on RegimeContext and are wired into the ranker, but `cross_exchange_confirmed_
fraction` is fixed at 1.0 ("1/1 exchanges confirm") and documented as a stub -
it is not measured, and must not be read as real multi-exchange confirmation
until the Bybit/OKX adapters are added.
"""
from __future__ import annotations

from momentum.data.state import StateStore
from momentum.engines.types import RegimeContext

BREADTH_HORIZON_S = 60
MAX_BIAS_ADJUST = 0.15  # bias multipliers stay within [1-x, 1+x]


def compute(store: StateStore, exchange: str, now: float, tracked_symbols: list[str]) -> RegimeContext:
    btc = store.get(exchange, "BTCUSDT")
    eth = store.get(exchange, "ETHUSDT")
    btc_vel = btc.velocity_pct(now, BREADTH_HORIZON_S) if btc else None
    eth_vel = eth.velocity_pct(now, BREADTH_HORIZON_S) if eth else None

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

    return RegimeContext(
        btc_velocity_60s=btc_vel,
        eth_velocity_60s=eth_vel,
        breadth_pct_up=breadth_up,
        breadth_pct_down=breadth_down,
        cross_exchange_confirmed_fraction=1.0,
        bias_up=max(0.85, min(1.15, bias_up)),
        bias_down=max(0.85, min(1.15, bias_down)),
        details={"note": "cross-exchange confirmation is a stub (binance-only slice)", "counted": counted},
    )

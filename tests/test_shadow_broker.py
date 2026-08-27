import time

from momentum.data.events import BookTicker, DepthSnapshot
from momentum.data.state import SymbolState
from momentum.shadow.broker import ShadowBroker

SHADOW_CFG = {"taker_fee_bps_by_exchange": {"binance": 10}, "simulated_latency_ms": [10, 20]}


def _state_with_book() -> SymbolState:
    state = SymbolState("BTCUSDT", "binance")
    now = time.time()
    state.on_book_ticker(BookTicker(symbol="BTCUSDT", ts=now, best_bid=99.9, best_bid_qty=1.0,
                                     best_ask=100.1, best_ask_qty=1.0))
    state.on_depth(DepthSnapshot(
        symbol="BTCUSDT", ts=now,
        bids=[(99.9, 1.0), (99.8, 2.0)],
        asks=[(100.1, 1.0), (100.2, 2.0)],
    ))
    return state


def test_entry_fill_walks_book_and_applies_fee():
    state = _state_with_book()
    broker = ShadowBroker(SHADOW_CFG)

    fill = broker.simulate_entry(state, "UP", size=1.5, exchange="binance")
    assert fill is not None
    assert fill.filled_size == 1.5
    # walks 1.0 @ 100.1 + 0.5 @ 100.2
    expected_avg = (1.0 * 100.1 + 0.5 * 100.2) / 1.5
    assert abs(fill.avg_price - expected_avg) < 1e-9
    assert fill.fee > 0
    assert fill.slippage_pct >= 0


def test_exit_short_covers_against_asks():
    state = _state_with_book()
    broker = ShadowBroker(SHADOW_CFG)

    fill = broker.simulate_exit(state, "DOWN", size=1.0, exchange="binance")
    assert fill is not None
    assert abs(fill.avg_price - 100.1) < 1e-9


def test_apply_filters_rejects_below_min_notional():
    from momentum.exchanges.base import SymbolFilter
    sf = SymbolFilter(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
                       tick_size=0.01, step_size=0.001, min_notional=10.0, quote_volume_24h=0, status="TRADING")
    assert ShadowBroker.apply_filters(sf, size=0.0001, price=100.0) is None
    assert ShadowBroker.apply_filters(sf, size=1.0, price=100.0) == 1.0

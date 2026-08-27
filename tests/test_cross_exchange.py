"""Mission 14 coverage: synchronized pump/dump, isolated pump, disagreement,
missing exchange (degradation), and basic lead/lag ordering."""
import time

import pytest

from momentum.data.events import BookTicker, Trade
from momentum.data.state import SymbolState
from momentum.engines.cross_exchange import LeadLagTracker, compute


def _state(now, exchange, direction_pct_10s, buy_ratio=0.8, symbol="BTCUSDT"):
    state = SymbolState(symbol, exchange)
    base_price = 100.0
    p1 = base_price * (1 + direction_pct_10s / 100.0)

    state.on_trade(Trade(symbol=symbol, ts=now - 300, exch_ts=now - 300, price=base_price, qty=1.0, is_buyer_maker=False))
    state.on_trade(Trade(symbol=symbol, ts=now - 10, exch_ts=now - 10, price=base_price, qty=1.0, is_buyer_maker=False))

    n = 10
    for i in range(n):
        ts = now - (9 - i) * 0.9
        is_buy = (i / n) < buy_ratio
        state.on_trade(Trade(symbol=symbol, ts=ts, exch_ts=ts, price=p1, qty=1.0, is_buyer_maker=not is_buy))

    state.on_book_ticker(BookTicker(symbol=symbol, ts=now, best_bid=p1 - 0.01, best_bid_qty=1.0,
                                     best_ask=p1 + 0.01, best_ask_qty=1.0))
    return state


def test_synchronized_pump_is_broad_market_confirmation():
    now = time.time()
    states = {
        "binance": _state(now, "binance", 0.5, buy_ratio=0.8),
        "bybit": _state(now, "bybit", 0.45, buy_ratio=0.8),
        "okx": _state(now, "okx", 0.55, buy_ratio=0.8),
    }
    result = compute("BTCUSDT", states, now, LeadLagTracker())
    assert result.details["classification"] == "BROAD_MARKET_CONFIRMATION"
    assert result.up > result.down
    assert result.up > 40


def test_isolated_pump_is_penalized():
    now = time.time()
    states = {
        "binance": _state(now, "binance", 1.5, buy_ratio=0.8),   # big isolated move
        "bybit": _state(now, "bybit", 0.01, buy_ratio=0.5),       # flat
        "okx": _state(now, "okx", 0.01, buy_ratio=0.5),           # flat
    }
    result = compute("BTCUSDT", states, now, LeadLagTracker())
    assert result.details["classification"] == "ISOLATED_MOVE"


def test_genuine_disagreement_is_mixed_not_isolated():
    now = time.time()
    states = {
        "binance": _state(now, "binance", 0.8, buy_ratio=0.8),    # up
        "bybit": _state(now, "bybit", -0.8, buy_ratio=0.2),        # down
        "okx": _state(now, "okx", 0.01, buy_ratio=0.5),            # flat
    }
    result = compute("BTCUSDT", states, now, LeadLagTracker())
    assert result.details["classification"] == "MIXED"


def test_single_exchange_degrades_gracefully():
    now = time.time()
    states = {"binance": _state(now, "binance", 0.8, buy_ratio=0.8)}
    result = compute("BTCUSDT", states, now, LeadLagTracker())
    assert result.details["classification"] == "SINGLE_EXCHANGE_ONLY"
    assert result.details["n_exchanges"] == 1
    assert result.up > 0


def test_no_exchange_data_is_safe():
    result = compute("BTCUSDT", {}, time.time(), LeadLagTracker())
    assert result.up == 0.0 and result.down == 0.0


def test_lead_lag_orders_by_onset_time():
    tracker = LeadLagTracker()
    t0 = 1000.0
    r1 = tracker.update("BTCUSDT", {"binance": 0.5, "bybit": 0.02, "okx": 0.02}, t0)
    assert r1["leading_exchange"] == "binance"

    r2 = tracker.update("BTCUSDT", {"binance": 0.5, "bybit": 0.4, "okx": 0.02}, t0 + 0.7)
    assert r2["leading_exchange"] == "binance"
    assert r2["lead_time_ms"]["bybit"] == pytest.approx(700.0)

    r3 = tracker.update("BTCUSDT", {"binance": 0.5, "bybit": 0.4, "okx": 0.45}, t0 + 1.4)
    assert r3["lead_time_ms"]["okx"] == pytest.approx(1400.0)

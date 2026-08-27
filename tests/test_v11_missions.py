"""V1.1 mission 16 coverage: dynamic universe screening, fast movers, momentum
starting, late-entry rejection, stablecoin isolation, and compute-budget
degradation (load shedding). Cross-exchange/leader-follower/shadow-realism/
isolation are covered in the other test files already.
"""
import time

from momentum.app import _apply_load_shedding, _passes_liquidity_gate
from momentum.data.events import BookTicker, DepthSnapshot, Trade
from momentum.data.state import SymbolState
from momentum.engines import fast_movers, late_entry, starting
from momentum.engines.stablecoin import is_stablecoin_pair
from momentum.engines.types import EngineScore, ExhaustionScore
from momentum.exchanges.base import SymbolFilter
from momentum.exchanges.universe import Universe


class _FakeAdapter:
    def __init__(self, filters):
        self._filters = filters

    async def fetch_symbol_universe(self):
        return self._filters


def _filter(symbol, volume, status="TRADING"):
    return SymbolFilter(symbol=symbol, base_asset=symbol[:-4], quote_asset="USDT",
                         tick_size=0.01, step_size=0.001, min_notional=5.0,
                         quote_volume_24h=volume, status=status)


async def test_universe_excludes_illiquid_and_non_trading_pairs():
    filters = [
        _filter("BTCUSDT", 50_000_000),          # liquid, trading -> kept
        _filter("NEWLISTUSDT", 10_000_000),        # a "new listing" that's liquid enough -> kept
        _filter("DUSTUSDT", 100),                   # illiquid -> excluded
        _filter("DELISTEDUSDT", 50_000_000, status="BREAK"),  # suspended/delisted -> excluded
    ]
    universe = Universe(_FakeAdapter(filters), min_quote_volume_24h=2_000_000)
    symbols = await universe.refresh()
    assert "BTCUSDT" in symbols
    assert "NEWLISTUSDT" in symbols
    assert "DUSTUSDT" not in symbols
    assert "DELISTEDUSDT" not in symbols


async def test_watchlist_tier_is_separate_from_tradable_and_never_overlaps():
    filters = [
        _filter("BTCUSDT", 50_000_000),   # above main threshold -> tradable
        _filter("LUNCUSDT", 1_200_000),    # below main (2M), above watchlist floor (300k) -> watchlist only
        _filter("RVNUSDT", 250_000),        # below both -> excluded entirely
        _filter("DELISTEDUSDT", 500_000, status="BREAK"),  # would qualify for watchlist by volume, but not status
    ]
    universe = Universe(_FakeAdapter(filters), min_quote_volume_24h=2_000_000,
                         watchlist_min_quote_volume_24h=300_000)
    tradable = await universe.refresh()

    assert tradable == ["BTCUSDT"]
    assert universe.watchlist_symbols == ["LUNCUSDT"]
    assert "RVNUSDT" not in tradable and "RVNUSDT" not in universe.watchlist_symbols
    assert "DELISTEDUSDT" not in universe.watchlist_symbols
    # a symbol never appears in both tiers at once
    assert not (set(tradable) & set(universe.watchlist_symbols))
    # get_filter still resolves watchlist-only symbols (needed for dashboard price/volume display)
    assert universe.get_filter("LUNCUSDT") is not None


async def test_watchlist_disabled_when_no_floor_configured():
    filters = [_filter("BTCUSDT", 50_000_000), _filter("LUNCUSDT", 1_200_000)]
    universe = Universe(_FakeAdapter(filters), min_quote_volume_24h=2_000_000)  # no watchlist floor
    await universe.refresh()
    assert universe.watchlist_symbols == []


def _state_with_book(spread_bps: float, depth_qty: float, symbol="BTCUSDT") -> SymbolState:
    state = SymbolState(symbol, "binance")
    now = time.time()
    mid = 100.0
    half_spread = mid * (spread_bps / 10_000) / 2
    state.on_book_ticker(BookTicker(symbol=symbol, ts=now, best_bid=mid - half_spread, best_bid_qty=depth_qty,
                                     best_ask=mid + half_spread, best_ask_qty=depth_qty))
    state.on_depth(DepthSnapshot(symbol=symbol, ts=now,
                                  bids=[(mid - half_spread, depth_qty)], asks=[(mid + half_spread, depth_qty)]))
    return state


def test_liquidity_gate_rejects_wide_spread_and_thin_depth():
    cfg = {"max_spread_bps_for_deep_analysis": 50, "min_depth_notional_usd": 500}
    now = time.time()

    healthy = _state_with_book(spread_bps=10, depth_qty=10)   # ~$1000 notional/side, tight spread
    assert _passes_liquidity_gate(healthy, now, cfg) is True

    wide_spread = _state_with_book(spread_bps=200, depth_qty=10)
    assert _passes_liquidity_gate(wide_spread, now, cfg) is False

    thin_depth = _state_with_book(spread_bps=10, depth_qty=0.001)
    assert _passes_liquidity_gate(thin_depth, now, cfg) is False


def test_stablecoin_pair_detection():
    assert is_stablecoin_pair("USDCUSDT", "USDT") is True
    assert is_stablecoin_pair("FDUSDUSDT", "USDT") is True
    assert is_stablecoin_pair("BTCUSDT", "USDT") is False


def test_stablecoin_monitor_flags_depeg():
    state = SymbolState("USDCUSDT", "binance")
    now = time.time()
    state.on_trade(Trade(symbol="USDCUSDT", ts=now - 60, exch_ts=now - 60, price=1.0, qty=1000, is_buyer_maker=False))
    state.on_book_ticker(BookTicker(symbol="USDCUSDT", ts=now, best_bid=0.985, best_bid_qty=1000,
                                     best_ask=0.986, best_ask_qty=1000))
    from momentum.engines import stablecoin
    check = stablecoin.compute(state, now)
    assert check is not None
    types = [a["type"] for a in check.anomalies]
    assert "DEPEG" in types


def _trending_state(now, direction_pct_10s=0.3, symbol="BTCUSDT") -> SymbolState:
    state = SymbolState(symbol, "binance")
    base = 100.0
    p1 = base * (1 + direction_pct_10s / 100.0)
    state.on_trade(Trade(symbol=symbol, ts=now - 300, exch_ts=now - 300, price=base, qty=1.0, is_buyer_maker=False))
    state.on_trade(Trade(symbol=symbol, ts=now - 10, exch_ts=now - 10, price=base, qty=1.0, is_buyer_maker=False))
    for i in range(10):
        ts = now - (9 - i) * 0.9
        state.on_trade(Trade(symbol=symbol, ts=ts, exch_ts=ts, price=p1, qty=1.0, is_buyer_maker=False))
    state.on_book_ticker(BookTicker(symbol=symbol, ts=now, best_bid=p1 - 0.01, best_bid_qty=5.0,
                                     best_ask=p1 + 0.01, best_ask_qty=5.0))
    state.on_depth(DepthSnapshot(symbol=symbol, ts=now, bids=[(p1 - 0.01, 5.0)], asks=[(p1 + 0.01, 5.0)]))
    return state


def test_fast_movers_detects_up_and_down():
    now = time.time()
    up_state = _trending_state(now, 0.6)
    down_state = _trending_state(now, -0.6)
    exhaustion = ExhaustionScore(up_risk=0.0, down_risk=0.0)
    empty_scores: dict = {}

    up_result = fast_movers.compute(up_state, now, empty_scores, exhaustion, 0.0, None)
    down_result = fast_movers.compute(down_state, now, empty_scores, exhaustion, 0.0, None)

    assert up_result is not None and up_result.direction == "UP" and up_result.fast_score > 0
    assert down_result is not None and down_result.direction == "DOWN" and down_result.fast_score > 0


def test_fast_movers_dampened_by_high_exhaustion():
    now = time.time()
    state = _trending_state(now, 0.6)
    empty_scores: dict = {}
    low_risk = fast_movers.compute(state, now, empty_scores, ExhaustionScore(0.0, 0.0), 0.0, None)
    high_risk = fast_movers.compute(state, now, empty_scores, ExhaustionScore(95.0, 0.0), 0.0, None)
    assert high_risk.fast_score < low_risk.fast_score


def test_starting_engine_rewards_building_combination():
    now = time.time()
    state = _trending_state(now, 0.4)
    engine_scores = {
        "velocity": EngineScore(60, 0, {"building_up": True, "building_down": False}),
        "acceleration": EngineScore(50, 0),
        "volume": EngineScore(40, 0),
        "orderflow": EngineScore(30, 0, {"persistence_up": 3, "persistence_down": 0}),
        "orderbook_imbalance": EngineScore(30, 0),
        "volatility_expansion": EngineScore(30, 0),
        "breakout": EngineScore(0, 0, {"local_high": None, "local_low": None}),
    }
    result = starting.compute(state, now, engine_scores, None)
    assert result.up > result.down
    assert result.up > 0


def test_late_entry_risk_flags_extended_move():
    now = time.time()
    # impulse_low far below current price -> large "distance traveled" for UP
    state = SymbolState("BTCUSDT", "binance")
    state.on_trade(Trade(symbol="BTCUSDT", ts=now - 100, exch_ts=now - 100, price=90.0, qty=1.0, is_buyer_maker=False))
    state.on_trade(Trade(symbol="BTCUSDT", ts=now - 5, exch_ts=now - 5, price=100.0, qty=1.0, is_buyer_maker=True))
    state.on_book_ticker(BookTicker(symbol="BTCUSDT", ts=now, best_bid=99.99, best_bid_qty=1.0,
                                     best_ask=100.01, best_ask_qty=1.0))
    result = late_entry.compute(state, now)
    assert result.up_risk > 50.0  # traveled from 90 -> 100 = ~11%, way past FULL_RISK_DISTANCE_PCT=2%


def test_load_shedding_keeps_only_strongest_when_degraded():
    scores = {"AAA": 90.0, "BBB": 10.0, "CCC": 50.0, "DDD": 5.0}
    promoted = set(scores.keys())

    not_degraded = _apply_load_shedding(promoted, scores, degraded_mode=False, degraded_promote_fraction=0.5)
    assert not_degraded == promoted

    degraded = _apply_load_shedding(promoted, scores, degraded_mode=True, degraded_promote_fraction=0.5)
    assert "AAA" in degraded  # strongest always kept
    assert "DDD" not in degraded  # weakest dropped first
    assert len(degraded) < len(promoted)


def test_load_shedding_never_drops_regime_anchors():
    from momentum.app import REGIME_ANCHOR_SYMBOLS
    scores = {s: 1.0 for s in REGIME_ANCHOR_SYMBOLS} | {"WEAKUSDT": 0.5}
    promoted = set(scores.keys())
    degraded = _apply_load_shedding(promoted, scores, degraded_mode=True, degraded_promote_fraction=0.1)
    for anchor in REGIME_ANCHOR_SYMBOLS:
        assert anchor in degraded

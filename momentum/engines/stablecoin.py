"""STABLECOIN_ANOMALY_MONITOR (V1.1 mission 9). Stablecoin pairs are excluded
from the normal momentum universe entirely (see app.py's universe split) and
watched here instead for depeg / abnormal volatility / abnormal volume. This
module never produces a momentum score and never feeds the ranker - it is a
parallel, separate monitor, exactly as specified. No additional real strategy
is attached to these detections in this mission.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.data.state import SymbolState

STABLECOIN_BASE_ASSETS = {
    "USDC", "FDUSD", "TUSD", "DAI", "USDP", "PYUSD", "USDD", "GUSD", "BUSD", "USDE", "EURT", "USTC",
}

DEPEG_THRESHOLD_PCT = 0.5
VOLATILITY_THRESHOLD_PCT = 0.15   # realized vol over 60s that's abnormal for a peg
VOLUME_RATIO_THRESHOLD = 3.0
PEG_PRICE = 1.0


def is_stablecoin_pair(symbol: str, quote_asset: str) -> bool:
    if not symbol.endswith(quote_asset):
        return False
    base = symbol[: -len(quote_asset)]
    return base in STABLECOIN_BASE_ASSETS


@dataclass(slots=True)
class StablecoinCheck:
    price: float
    deviation_pct: float
    anomalies: list = field(default_factory=list)  # [{"type", "value", "severity"}]


def compute(state: SymbolState, now: float) -> StablecoinCheck | None:
    price = state.price_now()
    if price is None:
        return None

    deviation_pct = (price - PEG_PRICE) / PEG_PRICE * 100.0
    vol60 = state.realized_vol(now, 60)
    baseline = state.total_volume(now, 300) / 300
    recent = state.total_volume(now, 30) / 30
    ratio = (recent / baseline) if baseline > 0 else 1.0

    anomalies = []
    if abs(deviation_pct) >= DEPEG_THRESHOLD_PCT:
        anomalies.append({
            "type": "DEPEG", "value": deviation_pct,
            "severity": min(100.0, abs(deviation_pct) / 2.0 * 100.0),
        })
    if vol60 is not None and vol60 >= VOLATILITY_THRESHOLD_PCT:
        anomalies.append({
            "type": "ABNORMAL_VOLATILITY", "value": vol60,
            "severity": min(100.0, vol60 / 1.0 * 100.0),
        })
    if ratio >= VOLUME_RATIO_THRESHOLD:
        anomalies.append({
            "type": "ABNORMAL_VOLUME", "value": ratio,
            "severity": min(100.0, (ratio - 1.0) / 9.0 * 100.0),
        })

    return StablecoinCheck(price=price, deviation_pct=deviation_pct, anomalies=anomalies)

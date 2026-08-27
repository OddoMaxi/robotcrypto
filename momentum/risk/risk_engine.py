"""RISK ENGINE (spec section 10). Paper-only in V1: every shadow trade knows its
entry/invalidation/stop/size/max-loss/expected-upside/R:R before it is "opened".
No all-in sizing - sizing is risk-per-trade (% of a paper account), never just
"capital available". Multiple risk-pct scenarios are computed and logged so the
Shadow can later show which one the empirical expectancy favors; none of them
is auto-applied to any future live account without separate authorization.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from momentum.entry.entry_engine import EntryQuality


@dataclass(slots=True)
class RiskScenario:
    risk_pct: float
    risk_amount: float
    size: float           # base-asset units
    size_notional: float  # quote-asset (USDT) value


@dataclass(slots=True)
class RiskPlan:
    entry_price: float
    invalidation_price: float
    stop_price: float
    target_price: float
    reward_risk: float
    default_risk_pct: float
    default_scenario: RiskScenario
    scenarios: list[RiskScenario] = field(default_factory=list)


def compute(direction: str, entry_price: float, entry_quality: EntryQuality, risk_cfg: dict) -> RiskPlan:
    stop_pct = entry_quality.stop_distance_pct / 100.0
    target_pct = entry_quality.target_distance_pct / 100.0

    if direction == "UP":
        invalidation_price = entry_price * (1 - stop_pct)
        target_price = entry_price * (1 + target_pct)
    else:
        invalidation_price = entry_price * (1 + stop_pct)
        target_price = entry_price * (1 - target_pct)

    stop_price = invalidation_price  # V1: stop == invalidation level, no separate buffer
    risk_per_unit = abs(entry_price - stop_price)
    reward_risk = (abs(target_price - entry_price) / risk_per_unit) if risk_per_unit > 0 else 0.0

    equity = risk_cfg["paper_account_equity"]
    scenarios = []
    for risk_pct in risk_cfg["risk_pct_scenarios"]:
        risk_amount = equity * risk_pct
        size = (risk_amount / risk_per_unit) if risk_per_unit > 0 else 0.0
        scenarios.append(RiskScenario(
            risk_pct=risk_pct, risk_amount=risk_amount, size=size, size_notional=size * entry_price,
        ))

    default_risk_pct = risk_cfg["default_risk_pct"]
    default_scenario = next(
        (s for s in scenarios if abs(s.risk_pct - default_risk_pct) < 1e-9),
        scenarios[0] if scenarios else RiskScenario(default_risk_pct, 0.0, 0.0, 0.0),
    )

    return RiskPlan(
        entry_price=entry_price,
        invalidation_price=invalidation_price,
        stop_price=stop_price,
        target_price=target_price,
        reward_risk=reward_risk,
        default_risk_pct=default_risk_pct,
        default_scenario=default_scenario,
        scenarios=scenarios,
    )

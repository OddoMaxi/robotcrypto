"""FAST FAILURE EXIT + PROFIT PROTECTION (spec sections 11-12).

A dynamic exit state machine per open (shadow) trade: INITIAL -> BREAKEVEN ->
TRAILING -> (PARTIAL_TAKEN) -> EXIT. Nothing here is a fixed "+0.5% = sell" -
trail tightness reacts to whether the ranker still likes the trade's direction,
and a fast-failure exit can cut a trade before its hard stop if momentum
clearly reverses while the trade isn't profitable yet.
"""
from __future__ import annotations

from dataclasses import dataclass

TIGHT_TRAIL_R = 0.5    # locked-in profit distance (in R) once momentum is decelerating
LOOSE_TRAIL_R = 1.2    # locked-in profit distance (in R) while momentum still strong
REVERSAL_CONFIDENCE_FLOOR = 30.0
DECELERATION_DROP_POINTS = 5.0


@dataclass(slots=True)
class OpenTrade:
    id: int
    symbol: str
    exchange: str    # execution venue (mission 9: may differ from where the signal was scored)
    direction: str  # "UP" or "DOWN"
    entry_price: float
    stop_price: float
    r_unit: float           # abs(entry_price - initial invalidation), in price units
    trailing_state: str      # "INITIAL" | "BREAKEVEN" | "TRAILING" | "PARTIAL_TAKEN"
    mfe_pct: float
    mae_pct: float
    partial_taken: bool = False


@dataclass(slots=True)
class ExitDecision:
    exit: bool
    exit_reason: str | None
    new_stop_price: float
    new_trailing_state: str
    new_mfe_pct: float
    new_mae_pct: float
    r_multiple: float
    partial_tp_triggered: bool


def evaluate(
    trade: OpenTrade,
    current_price: float,
    current_confidence: float,
    prev_confidence: float | None,
    exits_cfg: dict,
) -> ExitDecision:
    sign = 1 if trade.direction == "UP" else -1
    pnl_pct = sign * (current_price - trade.entry_price) / trade.entry_price * 100.0
    price_move = sign * (current_price - trade.entry_price)
    r_multiple = (price_move / trade.r_unit) if trade.r_unit > 0 else 0.0

    new_mfe = max(trade.mfe_pct, pnl_pct)
    new_mae = min(trade.mae_pct, pnl_pct)
    new_stop = trade.stop_price
    new_state = trade.trailing_state
    partial_tp_triggered = False

    # hard stop / invalidation
    stop_hit = (current_price <= trade.stop_price) if trade.direction == "UP" else (current_price >= trade.stop_price)
    if stop_hit:
        return ExitDecision(True, "stop_hit", trade.stop_price, new_state, new_mfe, new_mae, r_multiple, False)

    # fast failure: momentum clearly reversing before the trade is meaningfully profitable
    momentum_reversed = current_confidence < REVERSAL_CONFIDENCE_FLOOR
    if momentum_reversed and r_multiple < exits_cfg["trailing_activation_r"]:
        return ExitDecision(True, "fast_failure_momentum_reversed", new_stop, new_state, new_mfe, new_mae, r_multiple, False)

    # break-even
    if new_state == "INITIAL" and r_multiple >= exits_cfg["breakeven_trigger_r"]:
        new_state = "BREAKEVEN"
        candidate = trade.entry_price
        new_stop = max(new_stop, candidate) if trade.direction == "UP" else min(new_stop, candidate)

    # trailing (tightness reacts to whether momentum is still confirming)
    if r_multiple >= exits_cfg["trailing_activation_r"]:
        new_state = "TRAILING" if new_state != "PARTIAL_TAKEN" else new_state
        decelerating = prev_confidence is not None and current_confidence < prev_confidence - DECELERATION_DROP_POINTS
        trail_r = TIGHT_TRAIL_R if decelerating else LOOSE_TRAIL_R
        locked_r = max(0.0, r_multiple - trail_r)
        candidate = trade.entry_price + sign * locked_r * trade.r_unit
        new_stop = max(new_stop, candidate) if trade.direction == "UP" else min(new_stop, candidate)

    # partial take profit
    if not trade.partial_taken and r_multiple >= exits_cfg["partial_take_profit_r"]:
        partial_tp_triggered = True
        if new_state != "TRAILING":
            new_state = "PARTIAL_TAKEN"

    return ExitDecision(
        exit=False,
        exit_reason=None,
        new_stop_price=new_stop,
        new_trailing_state=new_state,
        new_mfe_pct=new_mfe,
        new_mae_pct=new_mae,
        r_multiple=r_multiple,
        partial_tp_triggered=partial_tp_triggered,
    )

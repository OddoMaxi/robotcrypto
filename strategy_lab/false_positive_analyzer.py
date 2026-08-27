"""FALSE POSITIVE ANALYZER (spec section 13). Classifies a losing trade after
the fact, from its recorded MFE/MAE/gross P&L only. Purely analytical - never
consulted by any strategy's entry/exit decision, only read by research.
"""
from __future__ import annotations

CLASSIFICATIONS = (
    "stopped_out_full_adverse_excursion",
    "gave_back_favorable_move",
    "chop_no_follow_through",
    "unclassified_loss",
)


def classify(mfe_pct: float, mae_pct: float, stop_distance_pct: float, gross_pnl_pct: float) -> str:
    if stop_distance_pct <= 0:
        return "unclassified_loss"
    if mae_pct <= -stop_distance_pct * 0.8:
        return "stopped_out_full_adverse_excursion"
    if mfe_pct > 0 and gross_pnl_pct < mfe_pct * 0.3:
        return "gave_back_favorable_move"
    if abs(mfe_pct) < stop_distance_pct * 0.2 and abs(mae_pct) < stop_distance_pct * 0.2:
        return "chop_no_follow_through"
    return "unclassified_loss"

"""RESEARCH / VALIDATION / OUT_OF_SAMPLE (spec section 16).

`phase`/`version` are read once from strategy_lab_config.yaml at startup - a
deliberate, manual, versioned human decision, never auto-promoted by the code.
Every signal/trade gets tagged with whatever is active at the moment it's
created (see WalkForwardTag.tag_signal/tag_trade below) and that tag is never
rewritten after the fact, even if the config is later changed - so a trade
logged under RESEARCH v1 stays tagged RESEARCH v1 forever, regardless of what
phase the Lab is in by the time anyone reads it back.

Forbidden by the spec and not implemented anywhere in this package: tuning
parameters to turn P&L green, dropping bad symbols after the fact, cherry-
picking good days, future leakage, optimizing and evaluating on the same
data, or forcing trade count.
"""
from __future__ import annotations

VALID_PHASES = {"RESEARCH", "VALIDATION", "OUT_OF_SAMPLE"}


class WalkForwardTag:
    def __init__(self, version: str, phase: str):
        if phase not in VALID_PHASES:
            raise ValueError(f"walk_forward.phase must be one of {VALID_PHASES}, got {phase!r}")
        self.version = version
        self.phase = phase

    @classmethod
    def from_config(cls, walk_forward_cfg: dict) -> "WalkForwardTag":
        return cls(version=str(walk_forward_cfg["version"]), phase=str(walk_forward_cfg["phase"]))

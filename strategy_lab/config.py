"""Typed config loading for the Strategy Lab. Deliberately its own module, not
an import from momentum/config.py - config.yaml (baseline) and
strategy_lab_config.yaml (this) are independent files with independent schemas,
and touching momentum/config.py at all is out of scope (rule 0).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "strategy_lab_config.yaml"


@dataclass(frozen=True)
class LabConfig:
    raw: dict = field(repr=False)

    @property
    def shadow_mode(self) -> bool:
        return bool(self.raw["mode"]["shadow_mode"])

    @property
    def real_orders(self) -> int:
        return int(self.raw["mode"]["real_orders"])

    @property
    def exchanges(self) -> list[str]:
        return list(self.raw["exchanges"])

    @property
    def universe(self) -> dict:
        return self.raw["universe"]

    @property
    def stage(self) -> dict:
        return self.raw["stage"]

    @property
    def compute_budget_cfg(self) -> dict:
        return self.raw["compute_budget"]

    @property
    def strategies_cfg(self) -> dict:
        return self.raw["strategies"]

    @property
    def exhaustion_cfg(self) -> dict:
        return self.raw["exhaustion"]

    @property
    def late_entry_cfg(self) -> dict:
        return self.raw["late_entry"]

    @property
    def meta_engine_cfg(self) -> dict:
        return self.raw["meta_engine"]

    @property
    def execution_cfg(self) -> dict:
        return self.raw["execution"]

    @property
    def exit_lab_cfg(self) -> dict:
        return self.raw["exit_lab"]

    @property
    def fast_entry_lab_cfg(self) -> dict:
        return self.raw["fast_entry_lab"]

    @property
    def walk_forward_cfg(self) -> dict:
        return self.raw["walk_forward"]

    @property
    def dashboard_cfg(self) -> dict:
        return self.raw["dashboard"]

    @property
    def db_path(self) -> pathlib.Path:
        return pathlib.Path(__file__).resolve().parent.parent / "db" / "strategy_lab.db"

    @property
    def schema_path(self) -> pathlib.Path:
        return pathlib.Path(__file__).resolve().parent.parent / "db" / "strategy_lab_schema.sql"


def load_config(path: pathlib.Path | None = None) -> LabConfig:
    path = path or DEFAULT_CONFIG_PATH
    with open(path) as f:
        raw = yaml.safe_load(f)

    if raw["mode"]["shadow_mode"] is not True:
        raise RuntimeError("SAFETY: mode.shadow_mode must be true. Refusing to start otherwise.")
    if int(raw["mode"]["real_orders"]) != 0:
        raise RuntimeError("SAFETY: mode.real_orders must be 0. Refusing to start otherwise.")

    return LabConfig(raw=raw)

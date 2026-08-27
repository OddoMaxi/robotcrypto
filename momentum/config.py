"""Typed config loading. SHADOW_MODE / REAL_ORDERS are validated hard here, but the
real safety guarantee is structural (see momentum/safety) - this is the visible flag
the spec requires, not the mechanism.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "config.yaml"


@dataclass(frozen=True)
class Config:
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
    def horizons_s(self) -> list[int]:
        return list(self.raw["horizons_s"])

    @property
    def twin_horizons_s(self) -> list[int]:
        return list(self.raw["twin_horizons_s"])

    @property
    def stage_a(self) -> dict:
        return self.raw["stage_a"]

    @property
    def engine_weights(self) -> dict:
        return self.raw["engines"]["weights"]

    @property
    def exhaustion_cfg(self) -> dict:
        return self.raw["engines"]["exhaustion"]

    @property
    def regime_cfg(self) -> dict:
        return self.raw["engines"]["regime"]

    @property
    def ranker_cfg(self) -> dict:
        return self.raw["ranker"]

    @property
    def entry_cfg(self) -> dict:
        return self.raw["entry"]

    @property
    def risk_cfg(self) -> dict:
        return self.raw["risk"]

    @property
    def exits_cfg(self) -> dict:
        return self.raw["exits"]

    @property
    def shadow_cfg(self) -> dict:
        return self.raw["shadow"]

    @property
    def dashboard_cfg(self) -> dict:
        return self.raw["dashboard"]

    @property
    def db_path(self) -> pathlib.Path:
        return pathlib.Path(__file__).resolve().parent.parent / "db" / "momentum.db"

    @property
    def schema_path(self) -> pathlib.Path:
        return pathlib.Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def load_config(path: pathlib.Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    with open(path) as f:
        raw = yaml.safe_load(f)

    if raw["mode"]["shadow_mode"] is not True:
        raise RuntimeError(
            "SAFETY: mode.shadow_mode must be true in V1. Refusing to start otherwise."
        )
    if int(raw["mode"]["real_orders"]) != 0:
        raise RuntimeError(
            "SAFETY: mode.real_orders must be 0 in V1. Refusing to start otherwise."
        )

    return Config(raw=raw)

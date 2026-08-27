"""Structural proof that the Strategy Lab has no real-order capability (spec
section 0/18) - mirrors tests/test_isolation.py's coverage of momentum/, run
independently against strategy_lab/ so neither package's guarantee depends on
the other.
"""
import pathlib

from strategy_lab.safety.isolation_guard import scan_package

STRATEGY_LAB_ROOT = pathlib.Path(__file__).resolve().parent.parent / "strategy_lab"


def test_strategy_lab_has_no_real_order_capability():
    violations = scan_package(STRATEGY_LAB_ROOT)
    assert violations == [], "forbidden trading-capable symbols found:\n" + "\n".join(violations)


def test_strategy_lab_config_forces_shadow_mode():
    from strategy_lab.config import load_config
    cfg = load_config()
    assert cfg.shadow_mode is True
    assert cfg.real_orders == 0


def test_strategy_lab_uses_its_own_db_and_port_not_the_baselines():
    from strategy_lab.config import load_config
    cfg = load_config()
    assert cfg.db_path.name == "strategy_lab.db"
    assert cfg.dashboard_cfg["port"] != 8801   # baseline's port - must never collide

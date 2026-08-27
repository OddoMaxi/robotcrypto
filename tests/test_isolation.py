import pathlib

from momentum.safety.isolation_guard import scan_package

MOMENTUM_ROOT = pathlib.Path(__file__).resolve().parent.parent / "momentum"


def test_no_live_order_capability_anywhere_in_momentum_package():
    violations = scan_package(MOMENTUM_ROOT)
    assert violations == [], "Live-order capability detected:\n" + "\n".join(violations)

"""Structural proof for spec section 0/18 ("no real-order capability"): scans
every .py file under the `strategy_lab` package for identifiers that would
indicate a live-order/withdrawal capability. Own copy of
momentum/safety/isolation_guard.py's approach (not an import - the two
packages must each independently prove they can't place an order, neither
depending on the other's guard staying correct) run via
tests/test_strategy_lab_isolation.py.
"""
from __future__ import annotations

import ast
import pathlib

FORBIDDEN_IDENTIFIERS = {
    "place_order", "submit_order", "create_order", "cancel_order",
    "withdraw", "transfer", "new_order", "order_create",
}

FORBIDDEN_IMPORT_HINTS = {
    "binance.client", "ccxt", "pybit", "okx", "okx.Trade", "okx.Account",
}


def _iter_py_files(root: pathlib.Path):
    for path in root.rglob("*.py"):
        yield path


def scan_package(root: pathlib.Path) -> list[str]:
    violations: list[str] = []
    for path in _iter_py_files(root):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as e:
            violations.append(f"{path}: failed to parse ({e})")
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in FORBIDDEN_IDENTIFIERS:
                    violations.append(f"{path}:{node.lineno}: defines forbidden function '{node.name}'")
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_IDENTIFIERS:
                violations.append(f"{path}:{node.lineno}: calls forbidden attribute '.{node.attr}'")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_IMPORT_HINTS:
                        violations.append(f"{path}:{node.lineno}: imports forbidden module '{alias.name}'")
            if isinstance(node, ast.ImportFrom):
                if node.module in FORBIDDEN_IMPORT_HINTS:
                    violations.append(f"{path}:{node.lineno}: imports from forbidden module '{node.module}'")

    return violations

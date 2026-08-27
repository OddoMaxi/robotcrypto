"""Structural proof for spec section 24: scans every .py file under the
`momentum` package for identifiers that would indicate a live-order/withdrawal
capability, and fails loudly if any is found. This is what actually enforces
"REAL ORDERS = 0" - not the config flag, which is just the visible switch.

Run via tests/test_isolation.py so it's part of the normal test suite and fails
the build the moment such a symbol is introduced.
"""
from __future__ import annotations

import ast
import pathlib

FORBIDDEN_IDENTIFIERS = {
    "place_order", "submit_order", "create_order", "cancel_order",
    "withdraw", "transfer", "new_order", "order_create",
}

FORBIDDEN_IMPORT_HINTS = {
    "binance.client",  # python-binance's authenticated trading client
    "ccxt",            # not used in V1; would be the path to a live trading client
    "pybit",            # Bybit's official SDK includes an authenticated trading client
    "okx",              # python-okx's Trade/Account modules are authenticated
    "okx.Trade",
    "okx.Account",
}


def _iter_py_files(root: pathlib.Path):
    for path in root.rglob("*.py"):
        yield path


def scan_package(root: pathlib.Path) -> list[str]:
    """Returns a list of human-readable violations; empty list means clean."""
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

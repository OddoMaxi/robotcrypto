"""Compute budget monitoring (V1.1 mission 13). The VPS has 2 cores shared with
the arbitrage bot - this samples this process's own CPU/RAM so the Stage A/B
loop can shed load intelligently under pressure: when a cycle runs long, only
the WEAKEST candidates (by Stage A fast score) get demoted to a lighter pass
next cycle. Strong candidates always keep the full engine suite - degrading
never means skipping exhaustion/late-entry/entry-quality checks on a real
contender, only on the marginal tail.
"""
from __future__ import annotations

import psutil


class ComputeBudget:
    def __init__(self):
        self._process = psutil.Process()
        self._process.cpu_percent()  # prime the internal counter; first real call is always 0.0

    def sample(self) -> tuple[float, float]:
        """Returns (cpu_percent, rss_mb) for this process since the last sample."""
        cpu = self._process.cpu_percent(interval=None)
        rss_mb = self._process.memory_info().rss / (1024 * 1024)
        return cpu, rss_mb

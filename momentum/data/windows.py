"""Generic time-indexed ring buffers used by SymbolState to answer
"what was X at time now-h" and "sum/avg of X over the last h seconds" without
re-deriving from candles. Everything here operates on wall-clock seconds.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(slots=True)
class _Point:
    ts: float
    value: float


class TimeSeriesBuffer:
    """Append-only (ts, value) buffer trimmed to `max_age_s`. Answers point-in-time
    and windowed-aggregate queries in O(n) over the retained window (small: seconds
    to ~15 minutes at trade/tick rate)."""

    def __init__(self, max_age_s: float):
        self.max_age_s = max_age_s
        self._buf: deque[_Point] = deque()

    def append(self, ts: float, value: float) -> None:
        self._buf.append(_Point(ts, value))
        self._trim(ts)

    def _trim(self, now: float) -> None:
        cutoff = now - self.max_age_s
        while self._buf and self._buf[0].ts < cutoff:
            self._buf.popleft()

    def latest(self) -> float | None:
        return self._buf[-1].value if self._buf else None

    def value_at_or_before(self, target_ts: float) -> float | None:
        """Most recent value with ts <= target_ts. Used for 'price N seconds ago'."""
        result = None
        for p in self._buf:
            if p.ts <= target_ts:
                result = p.value
            else:
                break
        return result

    def value_n_seconds_ago(self, now: float, seconds: float) -> float | None:
        target = now - seconds
        if not self._buf:
            return None
        # oldest point is the best estimate if the buffer doesn't go back far enough
        if self._buf[0].ts > target:
            return self._buf[0].value
        return self.value_at_or_before(target)

    def window(self, now: float, seconds: float) -> list[_Point]:
        cutoff = now - seconds
        return [p for p in self._buf if p.ts >= cutoff]

    def sum_since(self, now: float, seconds: float) -> float:
        return sum(p.value for p in self.window(now, seconds))

    def avg_since(self, now: float, seconds: float) -> float | None:
        pts = self.window(now, seconds)
        if not pts:
            return None
        return sum(p.value for p in pts) / len(pts)

    def max_since(self, now: float, seconds: float) -> float | None:
        pts = self.window(now, seconds)
        return max((p.value for p in pts), default=None)

    def min_since(self, now: float, seconds: float) -> float | None:
        pts = self.window(now, seconds)
        return min((p.value for p in pts), default=None)

    def stdev_since(self, now: float, seconds: float) -> float | None:
        pts = [p.value for p in self.window(now, seconds)]
        n = len(pts)
        if n < 2:
            return None
        mean = sum(pts) / n
        var = sum((v - mean) ** 2 for v in pts) / (n - 1)
        return var ** 0.5

    def __len__(self) -> int:
        return len(self._buf)

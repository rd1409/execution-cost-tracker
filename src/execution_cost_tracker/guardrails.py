"""Rate limiting and short-lived caching for the public quote endpoint.

Both live in memory, so on Vercel they apply per running instance: a burst
spread across several instances can exceed the configured rate. They stop the
easy abuse (one visitor hammering the button, a script looping) and absorb
repeated identical requests. For hard guarantees, add Vercel Firewall rate
limiting or a shared store (e.g. Redis) in front of this.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import Any, Callable, Hashable

Clock = Callable[[], float]


class RateLimited(Exception):
    """Too many requests; ``retry_after`` is seconds until one is allowed."""

    def __init__(self, scope: str, retry_after: float) -> None:
        self.scope = scope
        self.retry_after = max(1, int(retry_after + 0.999))
        who = "you" if scope == "client" else "everyone"
        super().__init__(f"Too many quote requests from {who}; try again in {self.retry_after}s")


class SlidingWindowLimiter:
    """At most ``limit`` events per ``window`` seconds, tracked per key."""

    def __init__(self, limit: int, window: float = 60.0, max_keys: int = 10_000, clock: Clock = time.monotonic):
        self.limit, self.window, self.max_keys, self.clock = limit, window, max_keys, clock
        self._events: OrderedDict[Hashable, deque[float]] = OrderedDict()

    def check(self, key: Hashable) -> float:
        """0 if an event is allowed now, else seconds to wait. Doesn't record."""
        now = self.clock()
        q = self._prune(key, now)
        if q is not None and len(q) >= self.limit:
            return q[0] + self.window - now
        return 0.0

    def record(self, key: Hashable) -> None:
        now = self.clock()
        q = self._prune(key, now)
        if q is None:
            q = self._events[key] = deque()
        q.append(now)
        self._events.move_to_end(key)
        while len(self._events) > self.max_keys:  # forget the least recently seen keys
            self._events.popitem(last=False)

    def _prune(self, key: Hashable, now: float) -> deque[float] | None:
        q = self._events.get(key)
        if q is None:
            return None
        while q and q[0] <= now - self.window:
            q.popleft()
        return q


class TTLCache:
    """A small time-limited cache. Entries expire ``ttl`` seconds after being set."""

    def __init__(self, ttl: float, max_entries: int = 500, clock: Clock = time.monotonic):
        self.ttl, self.max_entries, self.clock = ttl, max_entries, clock
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()

    def get(self, key: Hashable) -> Any | None:
        if self.ttl <= 0:
            return None
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires <= self.clock():
            del self._data[key]
            return None
        return value

    def age(self, key: Hashable) -> float | None:
        """Seconds since ``key`` was cached, or None if absent/expired."""
        if self.get(key) is None:
            return None
        return self.ttl - (self._data[key][0] - self.clock())

    def set(self, key: Hashable, value: Any) -> None:
        if self.ttl <= 0:
            return
        self._data[key] = (self.clock() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)


def client_key(headers: dict[str, str], fallback: str | None = None) -> str:
    """Best identifier for the caller: Vercel's client-IP headers, else the
    socket address. Header names are matched case-insensitively."""
    h = {k.lower(): v for k, v in headers.items()}
    real = h.get("x-real-ip", "").strip()
    if real:
        return real
    fwd = h.get("x-forwarded-for", "").split(",")[0].strip()
    if fwd:
        return fwd
    return fallback or "unknown"

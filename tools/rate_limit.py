"""A tiny, dependency-light per-key fixed-window rate limiter.

The public top.gg vote webhook (``cogs/system/webstats.py``) binds ``0.0.0.0``
so top.gg can reach it, which means internet scanners hit it too. A fixed-window
counter is the cheapest useful throttle: one integer window id plus one counter
per client key (an IP), no background timer, no per-request allocation churn.

Memory is bounded by delegating storage to :class:`tools.lru_cache.BoundedLRU`:
the map holds at most ``capacity`` keys and evicts the least-recently-used one
when a new key would overflow it. Eviction is always safe here - a dropped key
simply starts a fresh window on its next request, which at worst grants one
extra window's allowance to a client that had gone quiet long enough to be
evicted. Under a spoofed-source flood the cache churns rather than grows, so the
process footprint stays flat; genuine per-IP abuse from a stable source is
throttled. Network-level filtering (the operator's job) handles volumetric DDoS.
"""

from __future__ import annotations

import time
from typing import Callable

from tools.lru_cache import BoundedLRU


class FixedWindowRateLimiter:
    """Count requests per key inside fixed wall-clock windows.

    ``limit`` requests are allowed per ``window`` seconds per key. The window is
    derived from the clock (``int(now // window)``) rather than tracked per key,
    so a key that goes quiet needs no sweep - its next request lands in a new
    window and the counter resets lazily on read. ``capacity`` caps the number
    of distinct keys held at once (memory bound); ``clock`` is injectable so
    tests can drive time deterministically.
    """

    def __init__(
        self,
        *,
        limit: int,
        window: float,
        capacity: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window <= 0:
            raise ValueError("window must be > 0")
        self._limit = limit
        self._window = window
        self._clock = clock
        # value = [window_id, count, logged] - a small mutable list per key.
        self._buckets: BoundedLRU = BoundedLRU(capacity)

    def check(self, key) -> tuple[bool, bool]:
        """Record one request for ``key`` and decide whether to allow it.

        Returns ``(allowed, should_log)``. ``allowed`` is ``False`` once the key
        exceeds ``limit`` inside the current window. ``should_log`` is ``True``
        exactly once per key per window - on the first rejection - so an offender
        yields at most one log line per window instead of one per blocked
        request.
        """
        window_id = int(self._clock() // self._window)
        state = self._buckets.get(key)
        if state is None or state[0] != window_id:
            state = [window_id, 0, False]
        state[1] += 1
        self._buckets[key] = state  # (re)insert -> marks recent, evicts overflow

        if state[1] <= self._limit:
            return True, False
        should_log = not state[2]
        state[2] = True
        return False, should_log

    def __len__(self) -> int:
        """Number of distinct keys currently tracked (for tests/introspection)."""
        return len(self._buckets)

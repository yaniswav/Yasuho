"""Unit tests for ``tools.rate_limit.FixedWindowRateLimiter``.

Pure and deterministic: the limiter takes an injectable ``clock`` so every
window boundary is exercised without sleeping. No network, DB, or Discord.
"""

import pytest

from tools.rate_limit import FixedWindowRateLimiter


class _Clock:
    """A tiny controllable monotonic clock."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _limiter(clock, *, limit=3, window=60.0, capacity=16):
    return FixedWindowRateLimiter(
        limit=limit, window=window, capacity=capacity, clock=clock,
    )


def test_allows_up_to_limit_then_blocks():
    clock = _Clock(0.0)
    rl = _limiter(clock, limit=3)
    assert rl.check("ip") == (True, False)
    assert rl.check("ip") == (True, False)
    assert rl.check("ip") == (True, False)
    # 4th request in the same window is the first rejection -> block + log once.
    assert rl.check("ip") == (False, True)


def test_logs_only_once_per_window():
    clock = _Clock(0.0)
    rl = _limiter(clock, limit=1)
    assert rl.check("ip") == (True, False)
    assert rl.check("ip") == (False, True)   # first rejection logs
    assert rl.check("ip") == (False, False)  # subsequent rejections stay quiet
    assert rl.check("ip") == (False, False)


def test_window_reset_reallows_and_relogs():
    clock = _Clock(0.0)
    rl = _limiter(clock, limit=1, window=60.0)
    assert rl.check("ip") == (True, False)
    assert rl.check("ip") == (False, True)
    # Advance into the next window: counter and log flag reset lazily on read.
    clock.t = 60.0
    assert rl.check("ip") == (True, False)
    assert rl.check("ip") == (False, True)


def test_window_boundary_is_floor_division():
    clock = _Clock(59.9)
    rl = _limiter(clock, limit=1, window=60.0)
    assert rl.check("ip") == (True, False)
    assert rl.check("ip") == (False, True)  # still window 0
    clock.t = 60.0  # window 1 begins
    assert rl.check("ip") == (True, False)


def test_keys_are_isolated():
    clock = _Clock(0.0)
    rl = _limiter(clock, limit=1)
    assert rl.check("a") == (True, False)
    assert rl.check("a") == (False, True)
    # A different source has its own budget.
    assert rl.check("b") == (True, False)


def test_memory_is_bounded_by_capacity():
    clock = _Clock(0.0)
    rl = _limiter(clock, limit=1, capacity=8)
    for i in range(1000):
        rl.check(f"ip-{i}")
    assert len(rl) <= 8


def test_eviction_grants_a_fresh_window():
    clock = _Clock(0.0)
    rl = _limiter(clock, limit=1, capacity=2)
    assert rl.check("victim") == (True, False)
    assert rl.check("victim") == (False, True)  # victim now blocked
    # Push two other keys through to evict "victim" (LRU, capacity 2).
    rl.check("x")
    rl.check("y")
    # Re-seen after eviction: starts a clean window instead of staying blocked.
    assert rl.check("victim") == (True, False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0, "window": 60.0, "capacity": 4},
        {"limit": -1, "window": 60.0, "capacity": 4},
        {"limit": 5, "window": 0, "capacity": 4},
        {"limit": 5, "window": -1.0, "capacity": 4},
    ],
)
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        FixedWindowRateLimiter(**kwargs)


# --- stats(): lifetime counters for the bot-wide periodic load line --------


def test_stats_starts_at_zero():
    rl = _limiter(_Clock(0.0))
    assert rl.stats() == {"hits": 0, "rejections": 0, "tracked": 0}


def test_stats_counts_hits_and_rejections_lifetime():
    clock = _Clock(0.0)
    rl = _limiter(clock, limit=1)
    rl.check("a")  # hit
    rl.check("a")  # rejection
    rl.check("a")  # rejection (should_log fires once, but this still counts)
    rl.check("b")  # hit (different key, own budget)
    stats = rl.stats()
    assert stats["hits"] == 2
    assert stats["rejections"] == 2
    assert stats["tracked"] == 2


def test_stats_tracked_matches_len():
    clock = _Clock(0.0)
    rl = _limiter(clock, limit=5)
    rl.check("a")
    rl.check("b")
    rl.check("c")
    assert rl.stats()["tracked"] == len(rl) == 3

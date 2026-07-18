"""Unit tests for tools.lru_cache.BoundedLRU (pure, no bot/DB needed).

The settings cache leans on three properties of this primitive: it never grows
past its capacity, it keeps the most-recently-used entries (so hot ids survive a
flood of cold ones), and an evicted key is simply absent (the caller re-reads it
from the DB). These tests pin all three down, plus the ``setdefault`` guard that
tools.settings uses to survive a cold-cache write race.
"""

import pytest

from tools.lru_cache import BoundedLRU


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        BoundedLRU(0)
    with pytest.raises(ValueError):
        BoundedLRU(-1)


def test_stores_and_reads_back():
    cache = BoundedLRU(4)
    cache["a"] = 1
    assert "a" in cache
    assert cache["a"] == 1
    assert cache.get("a") == 1


def test_get_missing_returns_default_getitem_raises():
    cache = BoundedLRU(4)
    assert cache.get("nope") is None
    assert cache.get("nope", "fallback") == "fallback"
    with pytest.raises(KeyError):
        cache["nope"]


def test_eviction_drops_the_oldest_over_capacity():
    cache = BoundedLRU(2)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3  # over cap -> the oldest ("a") is evicted
    assert "a" not in cache
    assert "b" in cache
    assert "c" in cache
    assert len(cache) == 2


def test_getitem_refreshes_recency():
    cache = BoundedLRU(2)
    cache["a"] = 1
    cache["b"] = 2
    assert cache["a"] == 1  # touch "a" so "b" is now the oldest
    cache["c"] = 3          # evicts "b", not the just-used "a"
    assert "a" in cache
    assert "b" not in cache
    assert "c" in cache


def test_get_refreshes_recency():
    cache = BoundedLRU(2)
    cache["a"] = 1
    cache["b"] = 2
    assert cache.get("a") == 1  # use "a"
    cache["c"] = 3              # evicts the oldest ("b")
    assert "a" in cache
    assert "b" not in cache


def test_setitem_overwrite_refreshes_recency():
    cache = BoundedLRU(2)
    cache["a"] = 1
    cache["b"] = 2
    cache["a"] = 10  # rewrite "a" -> now most-recent; value updated
    cache["c"] = 3   # evicts "b"
    assert cache["a"] == 10
    assert "b" not in cache


def test_contains_is_a_peek_not_a_use():
    """A membership test must NOT keep a stale entry alive."""
    cache = BoundedLRU(2)
    cache["a"] = 1
    cache["b"] = 2
    assert ("a" in cache) is True  # peek only, does not refresh recency
    cache["c"] = 3                 # "a" is still the oldest -> evicted
    assert "a" not in cache
    assert "b" in cache
    assert "c" in cache


def test_setdefault_inserts_when_absent():
    cache = BoundedLRU(4)
    assert cache.setdefault("a", 1) == 1
    assert cache["a"] == 1


def test_setdefault_keeps_existing_value():
    cache = BoundedLRU(4)
    cache["a"] = 1
    assert cache.setdefault("a", 999) == 1  # never overwrites
    assert cache["a"] == 1


def test_setdefault_enforces_capacity():
    cache = BoundedLRU(2)
    cache.setdefault("a", 1)
    cache.setdefault("b", 2)
    cache.setdefault("c", 3)  # over cap -> oldest evicted
    assert len(cache) == 2
    assert "a" not in cache


def test_clear_empties_the_cache():
    cache = BoundedLRU(4)
    cache["a"] = 1
    cache["b"] = 2
    cache.clear()
    assert len(cache) == 0
    assert "a" not in cache


def test_discard_removes_only_requested_key_and_tolerates_missing():
    cache = BoundedLRU(4)
    cache["a"] = 1
    cache["b"] = 2

    cache.discard("a")
    cache.discard("missing")

    assert "a" not in cache
    assert cache["b"] == 2

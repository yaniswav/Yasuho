"""Tests for the per-user/guild settings cache (tools/settings.py).

The cache is authoritative for this single-process bot: writes go through set_*
which update both the in-memory cache and the DB. These tests cover the
read-modify-write merge behaviour and the cold-cache concurrency guard (a slow
fetch must not clobber a value a concurrent writer already cached).
"""

import asyncio

import pytest

from tools import settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    settings._cache.clear()
    yield
    settings._cache.clear()


async def test_get_returns_default_when_absent(fake_pool):
    fake_pool.fetchval_return = None
    assert await settings.get_guild(fake_pool, 1, "missing", "fallback") == "fallback"


async def test_set_then_get_roundtrips(fake_pool):
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, 1, "leveling_enabled", True)
    assert await settings.get_guild(fake_pool, 1, "leveling_enabled") is True


async def test_set_guild_merges_keys(fake_pool):
    # Two writes to the same guild must not clobber each other's keys.
    fake_pool.fetchval_return = None
    await settings.set_guild(fake_pool, 7, "a", 1)
    await settings.set_guild(fake_pool, 7, "b", 2)
    assert await settings.get_guild(fake_pool, 7, "a") == 1
    assert await settings.get_guild(fake_pool, 7, "b") == 2


async def test_user_and_guild_scopes_are_separate(fake_pool):
    fake_pool.fetchval_return = None
    await settings.set_user(fake_pool, 5, "locale", "fr")
    await settings.set_guild(fake_pool, 5, "locale", "ja")
    assert await settings.get_user(fake_pool, 5, "locale") == "fr"
    assert await settings.get_guild(fake_pool, 5, "locale") == "ja"


async def test_load_does_not_clobber_concurrent_write(fake_pool):
    """Regression: a slow cold-cache fetch must not overwrite a concurrent write.

    Without the setdefault guard in _load, the blocked fetch below would finish
    and reset the cache entry to {} (its stale DB read), losing the value a
    concurrent writer stored while the fetch was in flight.
    """
    settings._cache.clear()
    key = ("guild_settings", 42)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetchval(query, *args):
        reached.set()          # signal that the (cold) fetch has started
        await release.wait()   # block until the concurrent write has landed
        return None            # DB reports no row yet

    fake_pool.fetchval = slow_fetchval

    reader = asyncio.create_task(settings.get_guild(fake_pool, 42, "x"))
    await reached.wait()       # the reader is now parked inside the fetch

    # A concurrent writer populates the SAME entry directly in the cache.
    settings._cache[key] = {"x": "written"}

    release.set()              # let the fetch complete
    result = await reader

    assert settings._cache[key] == {"x": "written"}  # not clobbered by the fetch
    assert result == "written"

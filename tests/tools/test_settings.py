"""Tests for the per-user/guild settings cache (tools/settings.py).

The cache is authoritative for this single-process bot: writes go through set_*
which update both the in-memory cache and the DB. These tests cover the
read-modify-write merge behaviour and the cold-cache concurrency guard (a slow
fetch must not clobber a value a concurrent writer already cached).
"""

import asyncio
import json

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


async def test_cache_eviction_triggers_db_reread(fake_pool, monkeypatch):
    """An id evicted by the size cap is transparently re-read from the DB.

    Bounding the cache must preserve read semantics: an evicted id is not lost,
    it just costs one extra DB read on its next access. With a cap of 2, reading
    a third guild evicts the first; reading the first again re-fetches it.
    """
    fake_pool.fetchval_return = None  # every cold read sees an empty blob
    tiny = settings.SettingsCache(user_cap=2, guild_cap=2)
    monkeypatch.setattr(settings, "_cache", tiny)

    await settings.get_guild(fake_pool, 1, "x")  # cold read 1
    await settings.get_guild(fake_pool, 2, "x")  # cold read 2
    await settings.get_guild(fake_pool, 3, "x")  # cold read 3 -> evicts guild 1
    await settings.get_guild(fake_pool, 3, "x")  # cached hit, no read
    await settings.get_guild(fake_pool, 1, "x")  # evicted -> re-read (cold read 4)

    fetchvals = [c for c in fake_pool.calls if c[0] == "fetchval"]
    assert len(fetchvals) == 4
    # Guild 1 was fetched twice: once cold, once after eviction.
    assert len([c for c in fetchvals if c[2] == (1,)]) == 2


async def test_user_flood_does_not_evict_guild_blob(fake_pool, monkeypatch):
    """Scopes are capped independently: a flood of user ids spares guild blobs."""
    fake_pool.fetchval_return = None
    tiny = settings.SettingsCache(user_cap=2, guild_cap=2)
    monkeypatch.setattr(settings, "_cache", tiny)

    await settings.set_guild(fake_pool, 100, "leveling_enabled", True)
    for uid in range(10):  # far past the user cap
        await settings.get_user(fake_pool, uid, "x")

    reads_before = len([c for c in fake_pool.calls if c[0] == "fetchval"])
    # The guild blob is still cached: served without another DB read.
    assert await settings.get_guild(fake_pool, 100, "leveling_enabled") is True
    reads_after = len([c for c in fake_pool.calls if c[0] == "fetchval"])
    assert reads_after == reads_before


async def test_set_user_patches_single_key_via_jsonb_set(fake_pool):
    """set_user must write ONE key with jsonb_set, never a whole-blob overwrite.

    The whole-blob overwrite is the lost-update: tools/privacy.py writes the
    avatar-tracking flag out-of-band under an advisory lock, and a stale full
    blob written here would revert it. A per-key jsonb_set only touches the key
    we changed.
    """
    settings._cache.clear()
    await settings.set_user(fake_pool, 5, "help_expand", True)

    writes = [call for call in fake_pool.calls if call[0] == "execute"]
    assert writes, "set_user must issue a DB write"
    _method, query, args = writes[-1]
    assert "jsonb_set" in query
    assert "jsonb_build_object" in query
    # A whole-blob overwrite would set the entire column; that must be gone.
    assert "settings = $2::jsonb" not in query
    # Single parameterized statement keyed by (id, key, value).
    assert args == (5, "help_expand", json.dumps(True))


async def test_set_user_does_not_revert_out_of_band_sibling_key():
    """Regression: a set_user write must not clobber a concurrently-set sibling.

    Models the real row and jsonb_set semantics. A whole-blob write built from a
    stale cache would drop ``avatar_history_tracking``; the per-key patch keeps
    it.
    """

    class _JsonbPool:
        def __init__(self, row):
            self.row = row
            self.calls = []

        async def execute(self, query, *args):
            self.calls.append(("execute", query, args))
            _id, key, value = args
            self.row[key] = json.loads(value)  # emulate the per-key patch
            return "INSERT 0 1"

        async def fetchval(self, query, *args):
            self.calls.append(("fetchval", query, args))
            return json.dumps(self.row)  # authoritative post-write row

    settings._cache.clear()
    pool = _JsonbPool({"avatar_history_tracking": True})

    await settings.set_user(pool, 5, "help_expand", False)

    assert pool.row["avatar_history_tracking"] is True  # untouched sibling
    assert pool.row["help_expand"] is False


async def test_targeted_invalidation_forces_only_that_scope_to_reread(fake_pool):
    fake_pool.fetchval_return = None
    await settings.get_user(fake_pool, 5, "x")
    await settings.get_guild(fake_pool, 5, "x")

    settings.invalidate_user(5)
    settings.invalidate_guild(5)

    await settings.get_user(fake_pool, 5, "x")
    await settings.get_guild(fake_pool, 5, "x")
    fetchvals = [call for call in fake_pool.calls if call[0] == "fetchval"]
    assert len(fetchvals) == 4

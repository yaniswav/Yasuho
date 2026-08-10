"""The AutoMod ``automod``-table cache must not re-seat a value it lost.

``AutoMod.get_settings`` is a read-through cache with a NEGATIVE entry (``None``
means "this guild has no row"), and nothing ever re-reads a hit. So a cold read
still in flight when an invalidation lands used to put its pre-invalidation
snapshot back, and the stale ``antilink`` / ``antispam`` toggles then drove
``on_message`` until the next write to that guild.

The guard is the one ``tools/settings.py`` uses: a generation sampled before the
fetch and re-checked before seating. It lives INSIDE the cache object because the
invalidators are not in the cog - ``cogs/system/dashboard_sync.py`` reaches in
through ``getattr(cog, "_settings", None)`` and mutates the mapping directly
(``cache[gid] = row``, ``cache.clear()``), so hooking the mutation methods is
what covers them without their cooperation.

The tests below reproduce the exact interleaving (park the fetch, invalidate,
release) rather than calling the guard directly.
"""

import asyncio

from cogs.moderation import automod


class _GatedPool:
    """Pool whose fetchrow parks mid-flight so an invalidation can interleave."""

    def __init__(self, row, started, release):
        self.row = row
        self.started = started
        self.release = release
        self.fetches = 0

    async def fetchrow(self, query, *args):
        self.fetches += 1
        self.started.set()
        await self.release.wait()
        return self.row


class _Pool:
    """Pool that answers immediately, counting reads."""

    def __init__(self, row):
        self.row = row
        self.fetches = 0

    async def fetchrow(self, query, *args):
        self.fetches += 1
        return self.row


class _Bot:
    def __init__(self, pool):
        self.db_pool = pool


def _cog(pool):
    cog = automod.AutoMod.__new__(automod.AutoMod)
    cog.bot = _Bot(pool)
    cog._settings = automod._SettingsCache()
    return cog


_OFF = {"antilink": False, "antispam": False}
_ON = {"antilink": True, "antispam": False}


# ---------------------------------------------------------------------------
# The race the generation closes.
# ---------------------------------------------------------------------------


async def test_an_invalidation_during_a_cold_read_is_not_re_seated_stale():
    """Order the two: the dashboard write lands mid-fetch.

    ``_invalidate_automod`` re-reads the row and seats it under the guild id -
    the fresh value. Our fetch, issued before that write, must not overwrite it.
    """
    started, release = asyncio.Event(), asyncio.Event()
    cog = _cog(_GatedPool(_OFF, started, release))

    task = asyncio.create_task(cog.get_settings(100))
    await started.wait()

    cog._settings[100] = _ON  # exactly what the dashboard invalidator does

    release.set()
    row = await task

    assert cog._settings[100] == _ON  # the fresh value survived
    assert row == _OFF  # ... and this caller still got a real DB answer


async def test_a_resync_clear_during_a_cold_read_is_not_undone():
    """``_resync_automod`` empties the whole cache after a listen gap. A read in
    flight must not immediately put one entry back - that entry is exactly the
    one the gap made untrustworthy.
    """
    started, release = asyncio.Event(), asyncio.Event()
    cog = _cog(_GatedPool(_OFF, started, release))
    cog._settings[200] = _ON  # a warm entry, so clear() has something to drop

    task = asyncio.create_task(cog.get_settings(100))
    await started.wait()

    cog._settings.clear()

    release.set()
    row = await task

    assert 100 not in cog._settings
    assert row == _OFF


async def test_a_local_write_during_a_cold_read_is_not_re_seated_stale():
    """Same shape from inside the cog: ``_update_cache`` (behind
    ``set_custom_rule``) writes the new toggles, and a cold read that started
    before it must not revert them.
    """
    started, release = asyncio.Event(), asyncio.Event()
    cog = _cog(_GatedPool(_OFF, started, release))

    task = asyncio.create_task(cog.get_settings(100))
    await started.wait()

    cog._update_cache(100, antilink=True)

    release.set()
    await task

    assert cog._settings[100]["antilink"] is True


async def test_the_negative_entry_is_invalidatable_too():
    """A guild with no row caches ``None``; an invalidation mid-read must still
    win, or the guild stays "unconfigured" after the dashboard configures it.
    """
    started, release = asyncio.Event(), asyncio.Event()
    cog = _cog(_GatedPool(None, started, release))

    task = asyncio.create_task(cog.get_settings(100))
    await started.wait()

    cog._settings[100] = _ON

    release.set()
    await task

    assert cog._settings[100] == _ON


# ---------------------------------------------------------------------------
# ... without breaking the cache it guards.
# ---------------------------------------------------------------------------


async def test_an_uncontended_cold_read_still_seats():
    """The guard must not degrade the hot path into a read-every-message cache."""
    cog = _cog(_Pool(_ON))

    first = await cog.get_settings(100)
    second = await cog.get_settings(100)

    assert first == second == _ON
    assert cog.bot.db_pool.fetches == 1  # the second call was a hit


async def test_a_missing_row_is_negatively_cached():
    """``None`` is an answer, not a miss - it must be seated like any other."""
    cog = _cog(_Pool(None))

    assert await cog.get_settings(100) is None
    assert await cog.get_settings(100) is None
    assert cog.bot.db_pool.fetches == 1
    assert 100 in cog._settings


async def test_two_concurrent_misses_do_not_cancel_each_other():
    """Seating is not an invalidation. If it bumped the generation, one of two
    overlapping cold reads would always lose its seat and the cache would never
    settle under load.
    """
    started, release = asyncio.Event(), asyncio.Event()
    cog = _cog(_GatedPool(_ON, started, release))

    tasks = [asyncio.create_task(cog.get_settings(gid)) for gid in (100, 200)]
    await started.wait()
    release.set()
    await asyncio.gather(*tasks)

    assert cog._settings[100] == _ON
    assert cog._settings[200] == _ON


async def test_a_seat_never_clobbers_a_value_that_landed_first():
    """Mirrors ``tools/settings.py`` seating with ``setdefault``: whoever got
    there first cannot be older than us, so their value stays.
    """
    cache = automod._SettingsCache()
    generation = cache.generation
    cache.seat(100, _ON, generation)

    kept = cache.seat(100, _OFF, cache.generation)

    assert kept == _ON
    assert cache[100] == _ON


def test_every_mutating_entry_point_bumps_the_generation():
    """The dashboard reaches this cache through the plain mapping API, so each
    mutation method has to bump - a future invalidator using ``pop`` or
    ``update`` instead of ``__setitem__`` must not silently lose the guard.
    """
    cache = automod._SettingsCache()
    cache.seat(1, _ON, cache.generation)
    cache.seat(2, _ON, cache.generation)
    cache.seat(3, _ON, cache.generation)
    cache.seat(4, _ON, cache.generation)

    for mutate in (
        lambda c: c.__setitem__(1, _OFF),
        lambda c: c.__delitem__(1),
        lambda c: c.pop(2, None),
        lambda c: c.popitem(),
        lambda c: c.setdefault(9, _OFF),
        lambda c: c.update({10: _OFF}),
        lambda c: c.clear(),
    ):
        before = cache.generation
        mutate(cache)
        assert cache.generation > before, mutate


def test_the_cache_is_still_a_dict():
    """``cogs/system/dashboard_sync.py`` guards both AutoMod paths with
    ``isinstance(cache, dict)`` and skips silently otherwise - a cache that
    stopped being a dict would make every dashboard AutoMod write a no-op.
    """
    assert isinstance(automod._SettingsCache(), dict)

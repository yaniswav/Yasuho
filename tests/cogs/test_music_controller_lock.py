"""The per-guild controller lock must OUTLIVE the teardown that clears a guild.

``Music._send_controller`` serialises "delete the old controller, post exactly
one new one" under ``Music._controller_locks[guild_id]``. ``Music._clear`` (the
universal disconnect / stop / idle-teardown / restore-drop point) used to POP
that lock, which can happen while a poster is holding it across its awaits: the
holder is left guarding an object nobody else can see, the next poster's
``setdefault`` builds a FRESH lock, takes it uncontended and posts - two live
controllers, the exact race the lock exists to close.

The fix is to stop evicting: one uncontended ``asyncio.Lock`` per guild that has
ever played, the same unbounded-by-design trade the sibling per-guild lock maps
take (``_MUSIC_LOCKS``, ``_AUTOROOM_LOCKS``). These tests pin the object identity
AND the mutual exclusion it buys, so a future "reclaim the map" change has to
keep both.
"""

import asyncio

from cogs.music import music


class _Pool:
    """Pool stand-in: _clear's only DB call is a best-effort DELETE."""

    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))


class _Bot:
    def __init__(self):
        self.db_pool = _Pool()


class _Slot:
    """Stand-in for the effect-ceiling slot _clear releases."""

    def __init__(self):
        self.released = []

    def release(self, guild_id):
        self.released.append(guild_id)


class _Quotas:
    def __init__(self):
        self.filtered_players = _Slot()


class _Sessions:
    """Stand-in for the lyrics sessions / skip votes _clear winds down."""

    def __init__(self):
        self.stopped = []

    async def stop(self, guild_id):
        self.stopped.append(guild_id)

    async def clear(self, guild_id):
        self.stopped.append(guild_id)


def _cog():
    """A Music cog carrying only what ``_clear`` touches."""
    cog = music.Music.__new__(music.Music)
    cog.bot = _Bot()
    cog._controllers = {}
    cog._controller_locks = {}
    cog.quotas = _Quotas()
    cog.lyrics_sessions = _Sessions()
    cog.skip_votes = _Sessions()
    return cog


async def test_clear_keeps_the_very_same_controller_lock_object():
    """Same object before and after: identity is the whole guarantee here."""
    cog = _cog()
    lock = cog._controller_locks.setdefault(100, asyncio.Lock())

    await cog._clear(100)

    assert cog._controller_locks.get(100) is lock


async def test_clear_still_drops_the_controller_and_releases_the_slot():
    """Keeping the lock must not turn _clear into a no-op for everything else."""
    cog = _cog()
    cog._controllers[100] = object()
    cog._controller_locks.setdefault(100, asyncio.Lock())

    await cog._clear(100)

    assert 100 not in cog._controllers
    assert cog.quotas.filtered_players.released == [100]
    assert cog.lyrics_sessions.stopped == [100]
    assert cog.skip_votes.stopped == [100]
    assert cog.bot.db_pool.calls  # the persisted state is still dropped


async def test_a_clear_mid_post_cannot_let_a_second_poster_in():
    """The mutation-real one: a teardown landing while a poster holds the lock.

    With the pop, the second poster's ``setdefault`` handed back a brand-new
    unlocked Lock and it walked straight into the critical section while the
    first was still inside it. Now it waits, and only runs once the holder is
    out.
    """
    cog = _cog()
    entered = []

    async def poster():
        # Exactly how _send_controller reaches for the lock.
        async with cog._controller_locks.setdefault(100, asyncio.Lock()):
            entered.append("second")

    async with cog._controller_locks.setdefault(100, asyncio.Lock()):
        await cog._clear(100)  # disconnect lands mid-post
        task = asyncio.create_task(poster())
        for _ in range(5):  # every chance to sneak in
            await asyncio.sleep(0)
        assert entered == []

    await task
    assert entered == ["second"]

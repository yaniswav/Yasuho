"""Construction smoke test for the autoroom control components.

Regression guard for a production crash: the per-room control sub-components set
a reference to their owning view in __init__. It was named ``self.parent``, but
``discord.ui.Item.parent`` is a READ-ONLY property in Components V2, so building
any of them raised "AttributeError: property 'parent' ... has no setter" and
every room-control action failed. import-smoke only imports the module (the class
bodies); it does not instantiate, so it could not catch this. These tests build
the components and would fail again if a reserved discord.ui attribute name
(parent / view) were assigned in an __init__.

The second half of the file covers a different concern entirely: the derived
``_hub_index`` and the concurrency between its whole-index rebuild and the
per-guild writers that mutate it from three other modules.
"""

import asyncio

import pytest

from cogs.config import rooms


class _FakeOwner:
    """Stand-in for the RoomControlView the components reference."""


def test_slot_select_constructs():
    rooms._SlotSelect(_FakeOwner())


def test_member_action_select_constructs():
    rooms._MemberActionSelect(_FakeOwner(), [], "kick")


def test_room_sub_view_wraps_a_child():
    rooms._RoomSubView(rooms._SlotSelect(_FakeOwner()))


class _RaisingPool:
    """Pool whose only read fails, the way a restarting Postgres does."""

    async def fetch(self, *args):
        raise RuntimeError("connection reset")


class _Bot:
    def __init__(self, pool):
        self.db_pool = pool


async def test_cog_load_gives_up_on_both_halves_when_the_read_fails():
    """A failed settings read must not run the legacy migration.

    ``reload_hub_index`` returns the set of guilds that already carry an
    ``autorooms`` key, and ``_migrate_legacy`` treats every guild OUTSIDE that
    set as un-migrated - it writes default hubs over them. So handing the
    migration an empty set because the read failed would let one DB blip
    overwrite live hub lists with legacy defaults. cog_load returns instead, and
    the index stays exactly where it was (empty, at load).
    """
    cog = rooms.TemporaryRooms.__new__(rooms.TemporaryRooms)
    cog.bot = _Bot(_RaisingPool())
    cog._hub_index = {}
    migrated = []

    async def _never(configured):
        migrated.append(configured)

    cog._migrate_legacy = _never

    await rooms.TemporaryRooms.cog_load(cog)  # must not raise

    assert migrated == []
    assert cog._hub_index == {}


async def test_the_hub_rebuild_raises_rather_than_installing_an_empty_index():
    """The seam the dashboard resync depends on: a failed read is not an answer.

    ``on_voice_state_update`` reads ``_hub_index`` synchronously and an absent
    guild MEANS "no hubs here", so swallowing the error and assigning {} would
    kill join-to-create in every guild until a restart.
    """
    cog = rooms.TemporaryRooms.__new__(rooms.TemporaryRooms)
    cog.bot = _Bot(_RaisingPool())
    live = {100: {1111: {"hub_channel_id": 1111}}}
    cog._hub_index = live

    with pytest.raises(RuntimeError):
        await cog.reload_hub_index()

    assert cog._hub_index is live


# ---------------------------------------------------------------------------
# The whole-index rebuild vs the per-guild writers.
#
# ``reload_hub_index`` REBINDS ``_hub_index`` from a snapshot fetched moments
# earlier, while ``_index_guild`` (bot-side write, guild rejoin, dashboard
# autorooms invalidator) and ``tools/retention.py`` write the LIVE map for one
# guild. A per-guild write that landed between the fetch and the rebind used to
# be silently discarded, leaving the voice listener spinning up rooms from the
# config that write had just replaced.
# ---------------------------------------------------------------------------


def _hub(channel_id):
    """A hub dict shaped the way normalize_hubs leaves it."""
    return {
        "id": f"h{channel_id}",
        "label": "Ranked",
        "hub_channel_id": channel_id,
        "category_id": 42,
        "template": "{user}'s room",
        "user_limit": 0,
    }


def _row(guild_id, hubs):
    return {"guild_id": guild_id, "settings": {"autorooms": hubs}}


class _GatedPool:
    """Pool whose fetch parks mid-flight so a writer can interleave with it.

    ``started`` fires once the rebuild is inside its await (its snapshot of the
    live index is already taken); nothing comes back until ``release`` is set.
    """

    def __init__(self, rows, started, release):
        self.rows = rows
        self.started = started
        self.release = release

    async def fetch(self, *args):
        self.started.set()
        await self.release.wait()
        return self.rows


def _rebuilding_cog(pool):
    cog = rooms.TemporaryRooms.__new__(rooms.TemporaryRooms)
    cog.bot = _Bot(pool)
    cog._hub_index = {}
    return cog


async def test_a_hub_write_during_a_rebuild_is_not_lost():
    """Order the two: the write lands while the rebuild's fetch is in flight.

    The snapshot the rebuild is holding still says guild 100 points at hub 1111.
    The dashboard retargets it to 2222 (``_invalidate_autorooms`` re-reads the
    row, then calls ``_index_guild``) while the fetch is parked. The rebind must
    keep 2222 - it is strictly newer than anything the fetch could have seen.
    """
    started, release = asyncio.Event(), asyncio.Event()
    cog = _rebuilding_cog(_GatedPool([_row(100, [_hub(1111)])], started, release))
    cog._hub_index = {100: {1111: _hub(1111)}}

    task = asyncio.create_task(cog.reload_hub_index())
    await started.wait()

    cog._index_guild(100, [_hub(2222)])  # the concurrent per-guild write

    release.set()
    await task

    assert set(cog._hub_index[100]) == {2222}


async def test_a_hub_removal_during_a_rebuild_is_not_resurrected():
    """Same window, the other direction - and through a writer that is not
    ``_index_guild`` at all: ``tools/retention.py`` pops the guild straight off
    the map when its data is erased. A rebind from a snapshot taken before the
    erasure would put the hubs back.
    """
    started, release = asyncio.Event(), asyncio.Event()
    cog = _rebuilding_cog(_GatedPool([_row(100, [_hub(1111)])], started, release))
    cog._hub_index = {100: {1111: _hub(1111)}}

    task = asyncio.create_task(cog.reload_hub_index())
    await started.wait()

    cog._hub_index.pop(100, None)  # retention, mutating the live map directly

    release.set()
    await task

    assert 100 not in cog._hub_index


async def test_the_rebuild_still_rebuilds_every_untouched_guild():
    """The carry-over must not shrink to "keep whatever was live" - guilds no
    writer touched still come from the authoritative rows, including one that
    only exists there.
    """
    started, release = asyncio.Event(), asyncio.Event()
    rows = [_row(100, [_hub(1111)]), _row(200, [_hub(3333)])]
    cog = _rebuilding_cog(_GatedPool(rows, started, release))
    cog._hub_index = {100: {9999: _hub(9999)}}

    task = asyncio.create_task(cog.reload_hub_index())
    await started.wait()
    release.set()
    configured = await task

    assert set(cog._hub_index) == {100, 200}
    assert set(cog._hub_index[100]) == {1111}  # the stale 9999 is gone
    assert set(cog._hub_index[200]) == {3333}
    assert configured == {100, 200}


async def test_a_guild_configured_mid_rebuild_counts_as_configured():
    """``configured`` drives cog_load's legacy migration, which OVERWRITES every
    guild outside the set. A guild that gained its first hub while the fetch was
    parked is not in the snapshot, so it has to be added from the merged index.
    """
    started, release = asyncio.Event(), asyncio.Event()
    cog = _rebuilding_cog(_GatedPool([], started, release))

    task = asyncio.create_task(cog.reload_hub_index())
    await started.wait()

    cog._index_guild(300, [_hub(4444)])  # on_guild_join, mid-rebuild

    release.set()
    configured = await task

    assert set(cog._hub_index[300]) == {4444}
    assert 300 in configured


class _SequencedPool:
    """Pool handing each successive fetch the next (newer) database state."""

    def __init__(self, states):
        self.states = list(states)
        self.fetches = 0

    async def fetch(self, *args):
        self.fetches += 1
        await asyncio.sleep(0)  # a real round trip yields
        return self.states.pop(0)


async def test_two_overlapping_rebuilds_do_not_discard_the_newer_read():
    """What the lock buys on top of the diff.

    Unserialised, both rebuilds fetch at once, the first rebinds, and the second
    sees ALL of the first's freshly built entries as "changed" - it carries them
    over wholesale and throws away its own newer read. Held across fetch AND
    rebind, the second starts from the first's result and its read wins.
    """
    states = [[_row(100, [_hub(1111)])], [_row(100, [_hub(2222)])]]
    cog = _rebuilding_cog(_SequencedPool(states))

    await asyncio.gather(cog.reload_hub_index(), cog.reload_hub_index())

    assert cog.bot.db_pool.fetches == 2
    assert set(cog._hub_index[100]) == {2222}

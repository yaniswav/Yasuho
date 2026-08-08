"""Construction smoke test for the autoroom control components.

Regression guard for a production crash: the per-room control sub-components set
a reference to their owning view in __init__. It was named ``self.parent``, but
``discord.ui.Item.parent`` is a READ-ONLY property in Components V2, so building
any of them raised "AttributeError: property 'parent' ... has no setter" and
every room-control action failed. import-smoke only imports the module (the class
bodies); it does not instantiate, so it could not catch this. These tests build
the components and would fail again if a reserved discord.ui attribute name
(parent / view) were assigned in an __init__.
"""

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

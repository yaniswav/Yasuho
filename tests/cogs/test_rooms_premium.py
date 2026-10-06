"""Autoroom hubs under the premium resolver (M4a-3).

* ``TemporaryRooms._add_hub`` refuses creation at the EFFECTIVE max_hubs
  (FREE 5, Yasuho+ 10), resolved defensively, and names that effective max in
  its refusal.
* ``on_voice_state_update`` spawns NO room for an ARCHIVED hub (one past the
  guild's current effective cap) - silently, no message to the member, no
  mutation of ``_active`` - while a non-archived hub still spawns normally.
  Classification orders hubs by their position in the stored list (oldest
  first), with zero extra DB read.
* The per-hub room budget (``max_rooms``) is untouched by any of this.

No real Discord, no DB: ``TemporaryRooms.__new__`` builds a cog with its
in-memory maps live and its DB seams stubbed, the same pattern
tests/cogs/test_rooms_hub_lifecycle.py already uses.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import asyncio
import types
from collections import defaultdict
from unittest import mock

from cogs.config import rooms
from tools import premium
from tools.autoroom import MAX_HUBS

CATEGORY_ID = 900001
TRIGGER_ID = 900002
GUILD_ID = 100


def _hub(hub_id, hub_channel_id, label="Ranked", max_rooms=20):
    return {
        "id": hub_id,
        "label": label,
        "category_id": CATEGORY_ID,
        "hub_channel_id": hub_channel_id,
        "template": "{user}'s room",
        "user_limit": 0,
        "max_rooms": max_rooms,
        "private": False,
    }


class _Resolver:
    def __init__(self, limits):
        self._limits = limits

    def for_guild(self, guild_id):
        return self._limits


class _RaisingResolver:
    def for_guild(self, guild_id):
        raise RuntimeError("boom")


def _cog(hubs=(), premium_resolver=None):
    """A TemporaryRooms with its DB seams stubbed and its in-memory maps live."""
    cog = rooms.TemporaryRooms.__new__(rooms.TemporaryRooms)
    cog._hub_index = {}
    cog._active = defaultdict(set)
    cog._room_owners = {}
    cog._room_views = {}
    cog._locks = defaultdict(asyncio.Lock)
    cog.bot = types.SimpleNamespace(premium=premium_resolver)
    stored = [dict(hub) for hub in hubs]

    async def _load_hubs(guild_id):
        return [dict(hub) for hub in stored]

    async def _save_hubs(guild_id, new_hubs):
        return new_hubs

    cog._load_hubs = _load_hubs
    cog._save_hubs = _save_hubs
    return cog


# ---------------------------------------------------------------------------
# effective_max_hubs / resolver wiring
# ---------------------------------------------------------------------------


def test_effective_max_hubs_is_free_with_no_bot_attribute():
    cog = rooms.TemporaryRooms.__new__(rooms.TemporaryRooms)  # no .bot at all
    assert cog.effective_max_hubs(GUILD_ID) == MAX_HUBS
    assert cog.effective_max_hubs(GUILD_ID) == premium.FREE_MAX_HUBS


def test_effective_max_hubs_is_free_with_no_premium_attribute():
    cog = _cog()
    assert cog.effective_max_hubs(GUILD_ID) == MAX_HUBS


def test_effective_max_hubs_is_free_when_the_resolver_raises():
    cog = _cog(premium_resolver=_RaisingResolver())
    assert cog.effective_max_hubs(GUILD_ID) == MAX_HUBS


def test_effective_max_hubs_is_premium_for_a_premium_guild():
    cog = _cog(premium_resolver=_Resolver(premium.GUILD_PREMIUM))
    assert cog.effective_max_hubs(GUILD_ID) == premium.GUILD_PREMIUM.max_hubs
    assert cog.effective_max_hubs(GUILD_ID) == 10


# ---------------------------------------------------------------------------
# _add_hub: creation refused at the effective cap, message names it
# ---------------------------------------------------------------------------


async def test_add_hub_refuses_at_the_free_cap_and_names_it():
    hubs = [_hub(f"h{i}", 1000 + i) for i in range(MAX_HUBS)]
    cog = _cog(hubs)
    guild = types.SimpleNamespace(id=GUILD_ID, categories=[], channels=[])

    outcome = await cog._add_hub(
        guild,
        label="New",
        category_name="cat",
        hub_name="hub",
        template="{user}",
        user_limit=0,
    )

    assert str(MAX_HUBS) in outcome.message
    assert outcome.orphan_category_id is None


def test_premium_guild_is_not_capped_at_the_free_hub_count():
    """The same hub count that refuses a FREE guild must NOT refuse a
    premium one - the resolver wiring, not just the message, has to change
    behaviour. Exercises the exact guard ``_add_hub`` runs
    (``can_add_hub(hubs, max_hubs)``) without paying for real channel
    creation."""
    from tools.autoroom import can_add_hub

    hubs = [_hub(f"h{i}", 1000 + i) for i in range(MAX_HUBS)]
    cog = _cog(hubs, premium_resolver=_Resolver(premium.GUILD_PREMIUM))
    max_hubs = cog.effective_max_hubs(GUILD_ID)

    assert max_hubs == premium.GUILD_PREMIUM.max_hubs
    assert can_add_hub(hubs, MAX_HUBS) is False  # a FREE guild would be capped
    assert can_add_hub(hubs, max_hubs) is True  # a premium one is not


async def test_add_hub_refuses_at_the_premium_cap_and_names_it():
    hubs = [_hub(f"h{i}", 1000 + i) for i in range(premium.GUILD_PREMIUM.max_hubs)]
    cog = _cog(hubs, premium_resolver=_Resolver(premium.GUILD_PREMIUM))
    guild = types.SimpleNamespace(id=GUILD_ID, categories=[], channels=[])

    outcome = await cog._add_hub(
        guild,
        label="New",
        category_name="cat",
        hub_name="hub",
        template="{user}",
        user_limit=0,
    )

    assert str(premium.GUILD_PREMIUM.max_hubs) in outcome.message


# ---------------------------------------------------------------------------
# classify_hubs: ordering (oldest first, by append order)
# ---------------------------------------------------------------------------


def test_classify_hubs_keeps_the_oldest_active_under_a_downgrade():
    cog = _cog()
    hub1, hub2, hub3 = _hub("h1", 1), _hub("h2", 2), _hub("h3", 3)
    # Insertion order IS creation order (hubs.append in _add_hub).
    result = cog.classify_hubs([hub1, hub2, hub3], max_hubs=2)

    assert result.is_active("h1") is True
    assert result.is_active("h2") is True
    assert result.is_active("h3") is False  # the newest, archived


def test_classify_hubs_with_no_downgrade_keeps_everything_active():
    cog = _cog()
    hub1, hub2 = _hub("h1", 1), _hub("h2", 2)
    result = cog.classify_hubs([hub1, hub2], max_hubs=MAX_HUBS)
    assert result.is_active("h1") is True
    assert result.is_active("h2") is True


# ---------------------------------------------------------------------------
# on_voice_state_update: an archived hub spawns nothing, silently
# ---------------------------------------------------------------------------


def _member(guild):
    return types.SimpleNamespace(bot=False, guild=guild, id=7)


def _voice_state(channel_id):
    return types.SimpleNamespace(channel=types.SimpleNamespace(id=channel_id))


class _Cooldowns:
    """A Cooldowns stand-in that is never active - isolates this test from
    the real per-user debounce window."""

    def is_active(self, key):
        return False

    def touch(self, key):
        pass


async def test_archived_hub_spawns_no_room():
    hub1, hub2 = _hub("h1", 1001), _hub("h2", 1002)
    cog = _cog([hub1, hub2])
    cog._cooldowns = _Cooldowns()
    cog._hub_index[GUILD_ID] = {1001: hub1, 1002: hub2}
    cog._create_room = mock.AsyncMock()

    guild = types.SimpleNamespace(id=GUILD_ID)
    member = _member(guild)

    # hub2 is the newest (second in insertion order); cap it to 1 so only
    # hub1 stays active.
    with mock.patch.object(cog, "effective_max_hubs", return_value=1):
        await cog.on_voice_state_update(member, None, _voice_state(1002))

    cog._create_room.assert_not_awaited()


async def test_a_non_archived_hub_still_spawns_normally():
    """Counter-test: with the guild under its cap, joining the SAME hub type
    still creates a room - proving the silence above is conditional on
    archival, not unconditional."""
    hub1, hub2 = _hub("h1", 1001), _hub("h2", 1002)
    cog = _cog([hub1, hub2])
    cog._cooldowns = _Cooldowns()
    cog._hub_index[GUILD_ID] = {1001: hub1, 1002: hub2}
    cog._create_room = mock.AsyncMock()

    guild = types.SimpleNamespace(id=GUILD_ID)
    member = _member(guild)

    with mock.patch.object(cog, "effective_max_hubs", return_value=MAX_HUBS):
        await cog.on_voice_state_update(member, None, _voice_state(1002))

    cog._create_room.assert_awaited_once()


async def test_archived_hub_leaves_existing_rooms_and_budgets_untouched():
    """Existing spawned rooms (tracked in ``_active``) and the hub's own
    ``max_rooms`` budget are not touched by the archived no-spawn path - only
    the NEW spawn is withheld."""
    hub1, hub2 = _hub("h1", 1001), _hub("h2", 1002, max_rooms=15)
    cog = _cog([hub1, hub2])
    cog._cooldowns = _Cooldowns()
    cog._hub_index[GUILD_ID] = {1001: hub1, 1002: hub2}
    cog._create_room = mock.AsyncMock()
    # Pre-existing room on hub2, as if it spawned before a downgrade.
    cog._active[(GUILD_ID, "h2")] = {555666}

    guild = types.SimpleNamespace(id=GUILD_ID)
    member = _member(guild)

    with mock.patch.object(cog, "effective_max_hubs", return_value=1):
        await cog.on_voice_state_update(member, None, _voice_state(1002))

    cog._create_room.assert_not_awaited()
    assert cog._active[(GUILD_ID, "h2")] == {555666}  # untouched
    assert hub2["max_rooms"] == 15  # the global per-hub budget is unrelated


async def test_an_unindexed_hub_channel_is_a_plain_no_op():
    """Sanity: a voice join to a channel that is not any hub's trigger must
    still no-op before the archival check ever runs."""
    cog = _cog([])
    cog._cooldowns = _Cooldowns()
    cog._hub_index[GUILD_ID] = {}
    cog._create_room = mock.AsyncMock()
    guild = types.SimpleNamespace(id=GUILD_ID)

    await cog.on_voice_state_update(_member(guild), None, _voice_state(1002))

    cog._create_room.assert_not_awaited()


# ---------------------------------------------------------------------------
# _load_hubs / _save_hubs round-trip through the REAL normalize_hubs (not the
# stubbed seam the fixture above uses) - this is the regression coverage for
# the bug those stubs cannot see: normalize_hubs used to clamp to the FREE
# MAX_HUBS (5) regardless of the caller, so a Yasuho+ guild's 6th+ hub was
# silently dropped on every save and load even though its Discord channels
# already existed.
# ---------------------------------------------------------------------------


def _real_cog(premium_resolver=None):
    """A TemporaryRooms using the REAL _load_hubs/_save_hubs, backed by an
    in-memory stand-in for tools.settings.get_guild/set_guild."""
    cog = rooms.TemporaryRooms.__new__(rooms.TemporaryRooms)
    cog._hub_index = {}
    cog.bot = types.SimpleNamespace(db_pool=object(), premium=premium_resolver)
    return cog


async def test_save_then_load_keeps_more_than_five_hubs_for_a_premium_guild():
    store = {}

    async def fake_get_guild(pool, guild_id, key, default=None):
        return store.get((guild_id, key), default)

    async def fake_set_guild(pool, guild_id, key, value):
        store[(guild_id, key)] = value

    cog = _real_cog(premium_resolver=_Resolver(premium.GUILD_PREMIUM))
    hubs = [_hub(f"h{i}", 1000 + i) for i in range(7)]  # 7 > FREE MAX_HUBS (5)

    with mock.patch.object(rooms.settings, "get_guild", fake_get_guild), \
            mock.patch.object(rooms.settings, "set_guild", fake_set_guild):
        saved = await cog._save_hubs(GUILD_ID, hubs)
        assert len(saved) == 7  # not silently truncated to 5 on save

        loaded = await cog._load_hubs(GUILD_ID)
        assert len(loaded) == 7  # not silently truncated to 5 on the next load
        assert [h["id"] for h in loaded] == [f"h{i}" for i in range(7)]

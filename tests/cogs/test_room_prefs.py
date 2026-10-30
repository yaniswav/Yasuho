"""V5e-C: remembered temporary-room settings - merge, re-apply, save hooks.

Typography rule: ASCII '-' and '...' only.
"""

import types
from unittest import mock

import discord

from cogs.config import rooms
from cogs.config.rooms import TemporaryRooms
from tools import privacy, retention, roomprefs

HUB = {
    "id": "h1",
    "template": "{user}'s room",
    "user_limit": 5,
    "private": False,
    "max_rooms": 10,
}


# -- pure merge -----------------------------------------------------------------
def test_plan_room_merges_saved_settings_over_the_hub_defaults():
    assert roomprefs.plan_room(None, "Ann's room", 5, False) == ("Ann's room", 5, False, False)
    prefs = {"name": "Quiet", "user_limit": 0, "locked": True, "hidden": True}
    assert roomprefs.plan_room(prefs, "Ann's room", 5, False) == ("Quiet", 5, True, True)  # saved 0 (unlimited) is clamped to the hub limit
    # An unset field keeps the hub's value; a private hub beats a saved unlock.
    prefs = {"name": None, "user_limit": None, "locked": False, "hidden": None}
    assert roomprefs.plan_room(prefs, "Ann's room", 5, True) == ("Ann's room", 5, True, False)


def test_a_saved_user_limit_is_clamped_to_the_hubs_own_limit():
    # A Squad limit (8) saved elsewhere never exceeds a Duo hub (2); 0 means unlimited.
    assert roomprefs.plan_room({"user_limit": 8}, "n", 2, False)[1] == 2
    assert roomprefs.plan_room({"user_limit": 0}, "n", 2, False)[1] == 2
    assert roomprefs.plan_room({"user_limit": 1}, "n", 2, False)[1] == 1
    assert roomprefs.plan_room({"user_limit": 8}, "n", 0, False)[1] == 8  # hub has none


def test_clean_bounds_and_whitelist():
    assert roomprefs.clean("name", "  " + "x" * 300) == "x" * 100
    assert roomprefs.clean("user_limit", 500) == 99
    assert roomprefs.clean("user_limit", -3) == 0
    for bad in (lambda: roomprefs.clean("name", "   "), lambda: roomprefs.clean("bitrate", 1)):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


async def test_save_builds_its_columns_from_the_whitelist_only():
    pool = mock.AsyncMock()
    await roomprefs.save(pool, 1, 2, 77, name="Quiet", locked=True, evil="x; DROP TABLE y")
    sql, *args = pool.execute.await_args.args
    assert "evil" not in sql and "DROP" not in sql
    assert "name, locked" in sql and "WHERE NOT EXISTS" in sql and "room_prefs_off" in sql
    assert args == [1, 2, 77, "Quiet", True]
    pool.execute.reset_mock()
    await roomprefs.save(pool, 1, 2, 77, evil=1)
    pool.execute.assert_not_awaited()


# -- creation re-applies the saved settings ------------------------------------
class _Guild:
    def __init__(self):
        self.id = 1
        self.default_role = object()
        self.me = object()
        self.created = []
        self.channels = {}

    def get_channel(self, cid):
        return None

    async def create_voice_channel(self, name, **kwargs):
        self.created.append((name, kwargs))
        return types.SimpleNamespace(id=900 + len(self.created))


def _cog():
    bot = types.SimpleNamespace(db_pool=object(), get_channel=lambda cid: object())
    cog = TemporaryRooms(bot)
    cog._grant_room_owner = mock.AsyncMock()
    cog._post_room_controls = mock.AsyncMock()
    cog._cleanup_room = mock.AsyncMock()
    return cog


def _category(overwrites=None):
    category = mock.MagicMock(spec=discord.CategoryChannel)
    category.overwrites = overwrites or {}
    return category


class _Member:
    id = 42
    display_name = "Ann"

    def __init__(self):
        self.move_to = mock.AsyncMock()


def _member():
    return _Member()  # hashable, like discord.Member (an overwrite target)


async def _create(cog, guild, prefs, monkeypatch, hub=HUB):
    load = mock.AsyncMock(return_value=prefs)
    monkeypatch.setattr(rooms.roomprefs, "load", load)
    member = _member()
    member.guild = guild
    hub_channel = types.SimpleNamespace(id=777, category=_category())
    await cog._create_room(member, hub_channel, hub)
    return load


async def test_saved_settings_are_applied_when_the_member_creates_a_room(monkeypatch):
    guild = _Guild()
    prefs = {"name": "Quiet corner", "user_limit": 3, "locked": True, "hidden": True}
    load = await _create(_cog(), guild, prefs, monkeypatch)
    name, kwargs = guild.created[0]
    assert name == "Quiet corner" and kwargs["user_limit"] == 3
    everyone = kwargs["overwrites"][guild.default_role]
    assert everyone.connect is False and everyone.view_channel is False
    owner = next(
        v for k, v in kwargs["overwrites"].items() if k is not guild.default_role and k is not guild.me
    )
    assert owner.view_channel is True and owner.manage_channels is True
    load.assert_awaited_once()
    assert load.await_args.args[1:] == (1, 42, 777)  # keyed by the hub's trigger channel  # a single keyed read, only when a room is created


async def test_without_saved_settings_the_room_is_exactly_the_hub_default(monkeypatch):
    guild = _Guild()
    await _create(_cog(), guild, None, monkeypatch)
    name, kwargs = guild.created[0]
    assert name == "Ann's room" and kwargs["user_limit"] == 5 and "overwrites" not in kwargs


async def test_a_failing_read_falls_back_to_the_defaults(monkeypatch):
    guild = _Guild()
    cog = _cog()
    monkeypatch.setattr(rooms.roomprefs, "load", mock.AsyncMock(side_effect=RuntimeError("db")))
    member = _member()
    member.guild = guild
    hub_channel = types.SimpleNamespace(id=777, category=_category())
    await cog._create_room(member, hub_channel, HUB)
    assert guild.created[0][0] == "Ann's room"


async def test_a_private_hub_still_locks_and_does_not_hide_without_a_saved_hide(monkeypatch):
    guild = _Guild()
    await _create(_cog(), guild, None, monkeypatch, hub={**HUB, "private": True})
    everyone = guild.created[0][1]["overwrites"][guild.default_role]
    assert everyone.connect is False and everyone.view_channel is None


# -- the panel actions save --------------------------------------------------------
async def test_remember_room_saves_under_the_rooms_hub_and_never_raises(monkeypatch):
    cog = _cog()
    cog._hub_index = {1: {777: {"id": "h1"}}}
    cog._active[(1, "h1")].add(900)
    save = mock.AsyncMock()
    monkeypatch.setattr(rooms.roomprefs, "save", save)
    await cog.remember_room(1, 42, 900, name="x")
    save.assert_awaited_once_with(cog.bot.db_pool, 1, 42, 777, name="x")
    save.reset_mock()
    await cog.remember_room(1, 42, 555, name="x")  # not a tracked room: nothing saved
    save.assert_not_awaited()
    save.side_effect = RuntimeError("db")
    await cog.remember_room(1, 42, 900, name="x")  # swallowed (logged)


# -- the explicit overwrites ---------------------------------------------------------
def _ow(**kw):
    return discord.PermissionOverwrite(**kw)


def test_no_lock_or_hide_passes_no_overwrites_so_the_category_is_inherited():
    guild = _Guild()
    assert rooms.room_overwrites(guild, _category(), _member(), False, False) is None


def test_locked_room_without_a_category():
    guild, member = _Guild(), _member()
    ow = rooms.room_overwrites(guild, None, member, True, False)
    assert ow[guild.default_role].connect is False and ow[guild.default_role].view_channel is None
    assert ow[member].connect is True and ow[member].manage_channels is True
    me = ow[guild.me]
    assert (me.view_channel, me.connect, me.manage_channels, me.move_members) == (True,) * 4


def test_hidden_room_keeps_the_bot_and_the_owner_able_to_see_it():
    guild, member = _Guild(), _member()
    ow = rooms.room_overwrites(guild, None, member, False, True)
    assert ow[guild.default_role].view_channel is False and ow[guild.default_role].connect is None
    assert ow[member].view_channel is True
    assert ow[guild.me].view_channel is True


def test_a_staff_only_category_stays_staff_only():
    guild, member, staff = _Guild(), _member(), object()
    category = _category({guild.default_role: _ow(view_channel=False), staff: _ow(view_channel=True)})
    ow = rooms.room_overwrites(guild, category, member, True, False)
    assert ow[guild.default_role].view_channel is False  # the category's deny survives
    assert ow[guild.default_role].connect is False
    assert ow[staff].view_channel is True
    # The category's own objects are copies, never mutated.
    assert category.overwrites[guild.default_role].connect is None


async def test_a_private_hub_gets_the_same_merged_overwrites(monkeypatch):
    guild = _Guild()
    await _create(_cog(), guild, None, monkeypatch, hub={**HUB, "private": True})
    ow = guild.created[0][1]["overwrites"]
    assert ow[guild.default_role].connect is False
    assert ow[guild.me].move_members is True and ow[guild.me].view_channel is True


# -- privacy wiring --------------------------------------------------------------
def test_the_table_is_exported_erased_and_purged():
    assert ("room_prefs", "DELETE FROM room_prefs WHERE user_id = $1") in privacy.USER_DELETE_QUERIES
    assert "room_prefs_off" not in dict(privacy.USER_DELETE_QUERIES)  # the opt-out survives
    assert dict(retention.GUILD_DELETE_QUERIES)["room_prefs"] == "DELETE FROM room_prefs WHERE guild_id = $1"
    assert "FROM room_prefs" in retention.STORED_GUILD_IDS_QUERY

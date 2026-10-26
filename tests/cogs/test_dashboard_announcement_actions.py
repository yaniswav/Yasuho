"""Dashboard announcement executors: every write goes through the bot's store,
and every payload value is re-validated against the live guild."""

import datetime
import types

import discord

from cogs.system import dashboard_announcement_actions as actions


class _Store:
    def __init__(self):
        self.calls = []

    async def create(self, guild_id, channel_id, author_id, **kwargs):
        self.calls.append(("create", guild_id, channel_id, author_id, kwargs))
        return {"id": 7, "next_run_at": datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)}

    async def update(self, guild_id, announcement_id, **changes):
        self.calls.append(("update", guild_id, announcement_id, changes))
        return None if announcement_id == 404 else {"id": announcement_id, "next_run_at": None}

    async def set_enabled(self, guild_id, announcement_id, enabled):
        raise ValueError("One-shot time must be in the future")

    async def delete(self, guild_id, announcement_id):
        self.calls.append(("delete", guild_id, announcement_id))
        return announcement_id != 404


class _Channel(discord.TextChannel):
    def __init__(self, perms):
        self.id = 9
        self._perms = perms

    def permissions_for(self, _member):
        return self._perms


def _world(*, perms=None, mentionable=True, actor_can_mention=False):
    store = _Store()
    channel = _Channel(perms or discord.Permissions(view_channel=True, send_messages=True, embed_links=True))
    role = types.SimpleNamespace(id=5, mentionable=mentionable, is_default=lambda: False)
    guild = types.SimpleNamespace(
        id=3, me=object(),
        get_channel=lambda cid: channel if cid == 9 else None,
        get_role=lambda rid: role if rid == 5 else None,
    )
    engine = types.SimpleNamespace(store=store)
    bot = types.SimpleNamespace(get_guild=lambda gid: guild if gid == 3 else None,
                                get_cog=lambda name: engine)
    actor = types.SimpleNamespace(
        id=42, guild_permissions=discord.Permissions(mention_everyone=actor_can_mention)
    )
    return bot, store, actor


PAYLOAD = {"channel_id": "9", "local_time": "2030-01-06T21:00", "timezone": "Europe/Paris",
           "recurrence": "weekly", "embed": {"title": "Movie night"}, "role_id": "5"}


async def test_create_goes_through_the_store_with_the_actor_as_author():
    bot, store, actor = _world()
    result = await actions._exec_create(bot, 3, dict(PAYLOAD), actor)
    assert result == {"ok": True, "announcement_id": "7", "next_run_at": "2030-01-01T00:00:00+00:00"}
    (_kind, guild_id, channel_id, author_id, kwargs), = store.calls
    assert (guild_id, channel_id, author_id) == (3, 9, 42)
    assert kwargs["local_time"] == datetime.datetime(2030, 1, 6, 21, 0)
    assert kwargs["role_id"] == 5 and kwargs["recurrence"] == "weekly"


async def test_an_unmentionable_role_needs_an_actor_who_could_ping_it():
    bot, store, actor = _world(mentionable=False)
    assert (await actions._exec_create(bot, 3, dict(PAYLOAD), actor))["error"] == "role_not_mentionable"
    assert store.calls == []
    bot, store, actor = _world(mentionable=False, actor_can_mention=True)
    assert (await actions._exec_create(bot, 3, dict(PAYLOAD), actor))["ok"] is True


async def test_payload_values_are_revalidated():
    bot, _store, actor = _world()
    cases = {
        "channel_not_found": {**PAYLOAD, "channel_id": "123"},
        "role_not_found": {**PAYLOAD, "role_id": "6"},
        "bad_payload": {**PAYLOAD, "local_time": "tomorrow"},
    }
    for code, payload in cases.items():
        assert (await actions._exec_create(bot, 3, payload, actor))["error"] == code
    no_send = _world(perms=discord.Permissions(view_channel=True))[0]
    assert (await actions._exec_create(no_send, 3, dict(PAYLOAD), actor))["error"] == "missing_permissions"


async def test_update_delete_and_enable_map_store_answers_to_codes():
    bot, store, actor = _world()
    assert (await actions._exec_update(bot, 3, {"announcement_id": "404"}, actor))["error"] == "not_found"
    assert (await actions._exec_delete(bot, 3, {"announcement_id": "404"}))["error"] == "not_found"
    assert (await actions._exec_delete(bot, 3, {"announcement_id": "8"}))["ok"] is True
    enable = await actions._exec_set_enabled(bot, 3, {"announcement_id": "8", "enabled": True})
    assert enable["error"] == "invalid_announcement"
    assert (await actions._exec_set_enabled(bot, 3, {"announcement_id": "8", "enabled": "yes"}))["error"] == "bad_payload"

import datetime
import types

from cogs.system import events


def _cog(bot):
    cog = object.__new__(events.Events)
    cog.bot = bot
    return cog


async def test_guild_remove_schedules_grace_job_and_invalidates_caches(
    monkeypatch,
):
    scheduled = []
    invalidated = []
    purge_after = datetime.datetime(
        2030, 2, 1, tzinfo=datetime.timezone.utc
    )

    async def schedule(_pool, guild_id):
        scheduled.append(guild_id)
        return purge_after

    monkeypatch.setattr(events.retention, "schedule_guild_purge", schedule)
    monkeypatch.setattr(
        events.retention,
        "invalidate_guild_caches",
        lambda _bot, guild_id: invalidated.append(guild_id),
    )
    bot = types.SimpleNamespace(db_pool=object())

    await _cog(bot).on_guild_remove(types.SimpleNamespace(id=42))

    assert scheduled == [42]
    assert invalidated == [42]


async def test_guild_join_cancels_purge_and_restores_startup_caches(
    monkeypatch,
):
    cancelled = []
    refreshed = []
    indexed = []

    async def cancel(_pool, guild_id):
        cancelled.append(guild_id)
        return True

    class _Pool:
        async def fetchrow(self, query, guild_id):
            assert guild_id == 42
            return {"prefix": "!", "autorole": 10, "muterole": 11}

    monkeypatch.setattr(events.retention, "cancel_guild_purge", cancel)

    class _Leveling:
        async def refresh_guild_config(self, guild_id):
            refreshed.append(guild_id)

    class _Rooms:
        async def _load_hubs(self, guild_id):
            return [{"hub_channel_id": guild_id + 1}]

        def _index_guild(self, guild_id, hubs):
            indexed.append((guild_id, hubs))

    cogs = {"Leveling": _Leveling(), "TemporaryRooms": _Rooms()}
    bot = types.SimpleNamespace(
        db_pool=_Pool(),
        prefixes={},
        autoroles={},
        muteroles={},
        get_cog=cogs.get,
    )
    guild = types.SimpleNamespace(
        id=42,
        name="Guild",
        text_channels=[],
        system_channel=None,
        owner=None,
    )

    await _cog(bot).on_guild_join(guild)

    assert cancelled == [42]
    assert bot.prefixes == {42: "!"}
    assert bot.autoroles == {42: 10}
    assert bot.muteroles == {42: 11}
    assert refreshed == [42]
    assert indexed == [(42, [{"hub_channel_id": 43}])]

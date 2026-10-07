"""Tests for 24/7 music (Yasuho+ M4b, .claude/plans/monetisation/4-plan-retenu.md).

Two layers:

* ``cogs/music/always_on.py`` - the stored cache and the disconnect
  classifier - is pure/async-only and tested directly, no Discord objects.
* ``cogs/music/music.py``'s wiring (the idle sweeper, the empty-channel
  auto-leave, the restore/reconnect rejoin, the command group, the
  ``player_disconnect``/``guild_remove`` listeners) is tested against a
  ``Music`` instance built with ``object.__new__`` - the same "stand-in
  subclass, skip the real __init__" approach ``tests/cogs/
  test_music_playlists_premium.py`` and ``tests/cogs/
  test_rooms_hub_lifecycle.py`` already use, so the real ``_idle_check``
  tick, the real ``_restore_one`` empty-channel gate, and the real command
  callbacks run - not a reimplementation of them.

Every guard below carries a NEGATIVE CONTROL: the same setup with the one
condition under test flipped, proving the guard is guild/condition-specific
rather than a vacuous always-skip (see the project's own "a guard with no
negative control is unproven" rule).
"""

from __future__ import annotations

import time
import types
from datetime import datetime, timezone

import discord
import pytest
import sonolink

import cogs.music.music as music_mod
from cogs.music import always_on as ao
from cogs.music import failures, lyrics, voteskip
from cogs.music.player import Player
from tools import premium
from tools.quotas import QuotaRegistry

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _StubLimits:
    def __init__(self, music_247):
        self.music_247 = music_247


class _StubPremium:
    """``bot.premium``-shaped stub: one ``music_247`` verdict for every guild
    unless overridden per guild id."""

    def __init__(self, music_247=False, *, per_guild=None):
        self._default = music_247
        self._per_guild = per_guild or {}

    def for_guild(self, guild_id):
        return _StubLimits(self._per_guild.get(guild_id, self._default))


class _FakeMember:
    def __init__(self, member_id, *, bot=False, guild=None):
        self.id = member_id
        self.bot = bot
        self.guild = guild


class _FakeChannel(types.SimpleNamespace):
    """A plain stand-in channel - good enough for everything that only reads
    ``.guild``/``.members``/``.id`` and never needs a real discord.py
    isinstance check (the idle sweeper and the voice-state listener)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("id", 10)
        super().__init__(**kwargs)


class _Deleting:
    """Real-discord-type base for tests that DO need isinstance to pass
    (``_restore_one``, ``_bare_join_247``) - same pattern as
    tests/cogs/test_rooms_hub_lifecycle.py's ``_Voice``: subclass the real
    discord.py channel type but skip its ``__init__`` entirely."""

    def __init__(self, channel_id, *, guild=None, members=()):
        self.id = channel_id
        self.name = "voice"
        self.guild = guild
        self._members = list(members)


class _RealVoiceChannel(_Deleting, discord.VoiceChannel):
    # discord.py's own ``members`` is a read-only property derived from the
    # guild's voice states; we have no real guild, so it is overridden here
    # to read the plain list ``_Deleting.__init__`` stored instead.
    @property
    def members(self):
        return self._members


class _FakeGuild:
    def __init__(self, guild_id, *, channels=None, voice_client=None):
        self.id = guild_id
        self._channels = channels or {}
        self.voice_client = voice_client

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, member_id):
        return None


class _FakePlayer(Player):
    """Duck-typed stand-in satisfying ``isinstance(x, Player)`` - deliberately
    skips ``Player.__init__``/sonolink's own (a real voice/node connection),
    since every test here only needs the attributes the code under test
    actually reads."""

    def __init__(self, *, channel, current=None, paused=False, queued=()):
        # ``paused``/``current``/``queue`` are READ-ONLY properties on the
        # real sonolink Player (current and queue both derive from
        # ``self._queue``) - write their backing attributes instead.
        self._paused = paused
        self._queue = types.SimpleNamespace(tracks=list(queued), current_track=current)
        self.channel = channel
        self.home = channel
        self.controller = None
        self.idle_since = None
        self.dj = None
        self.disconnect_calls = 0

    async def disconnect(self, *, force=False):
        self.disconnect_calls += 1


class _FakeBot:
    def __init__(self, *, pool, premium_resolver, guilds=None):
        self.db_pool = pool
        self.premium = premium_resolver
        self._guilds = guilds or {}
        self.sl_client = types.SimpleNamespace(
            decode_tracks=self._decode_tracks
        )
        self.decode_calls = 0

    async def _decode_tracks(self, *args):
        self.decode_calls += 1
        return [None]  # "could not decode" -> caller clears and returns

    def get_guild(self, guild_id):
        return self._guilds.get(guild_id)


def _make_cog(bot):
    """A ``Music`` instance with every collaborator ``__init__`` sets, minus
    starting the real background loop (``_idle_check.start()``) - tests call
    ``Music._idle_check.coro(cog)`` directly instead, the listener equivalent
    of this suite's ``.callback(cog, ctx, ...)`` command pattern."""
    cog = object.__new__(music_mod.Music)
    cog.bot = bot
    cog._restored = False
    cog._controllers = {}
    cog._controller_locks = {}
    cog.quotas = QuotaRegistry()
    cog.lyrics_sessions = lyrics.LyricsSessions(cog.quotas.synced_lyrics)
    cog.skip_votes = voteskip.SkipVotes()
    cog.track_failures = failures.TrackFailureBursts()
    cog._last_quota_log = time.monotonic()
    cog.always_on = ao.AlwaysOnStore()
    cog._reconnect_rejoin_running = False
    return cog


@pytest.fixture
def cog(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(False))
    return _make_cog(bot)


# ---------------------------------------------------------------------------
# always_on.AlwaysOnStore - the cache
# ---------------------------------------------------------------------------


async def test_ensure_loaded_populates_cache_from_rows(fake_pool):
    fake_pool.fetch_return = [
        {"guild_id": 1, "channel_id": 10, "enabled_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"guild_id": 2, "channel_id": 20, "enabled_at": datetime(2026, 1, 2, tzinfo=UTC)},
    ]
    store = ao.AlwaysOnStore()
    await store.ensure_loaded(fake_pool)
    assert store.is_enabled(1) and store.is_enabled(2)
    assert store.channel_id(1) == 10
    assert store.channel_id(2) == 20
    assert store.ordered_guild_ids() == [1, 2]


async def test_ensure_loaded_is_one_shot(fake_pool):
    fake_pool.fetch_return = [
        {"guild_id": 1, "channel_id": 10, "enabled_at": datetime(2026, 1, 1, tzinfo=UTC)}
    ]
    store = ao.AlwaysOnStore()
    await store.ensure_loaded(fake_pool)
    fetch_calls_before = sum(1 for c in fake_pool.calls if c[0] == "fetch")
    await store.ensure_loaded(fake_pool)
    fetch_calls_after = sum(1 for c in fake_pool.calls if c[0] == "fetch")
    assert fetch_calls_before == fetch_calls_after == 1


def test_unconfigured_guild_reads_as_off():
    store = ao.AlwaysOnStore()
    assert not store.is_enabled(999)
    assert store.channel_id(999) is None
    assert not store.is_suspended(999)


async def test_enable_writes_db_and_updates_cache(fake_pool):
    store = ao.AlwaysOnStore()
    await store.enable(fake_pool, 1, 10)
    assert store.is_enabled(1)
    assert store.channel_id(1) == 10
    method, query, args = fake_pool.calls[-1]
    assert method == "execute"
    assert "music_247" in query
    assert args == (1, 10)


async def test_disable_deletes_db_and_evicts_cache(fake_pool):
    store = ao.AlwaysOnStore()
    await store.enable(fake_pool, 1, 10)
    await store.disable(fake_pool, 1)
    assert not store.is_enabled(1)
    method, query, _args = fake_pool.calls[-1]
    assert method == "execute"
    assert "DELETE" in query and "music_247" in query


def test_suspend_is_a_noop_for_an_unconfigured_guild():
    store = ao.AlwaysOnStore()
    assert store.suspend(1) is False
    assert not store.is_suspended(1)


async def test_suspend_and_lift_on_a_configured_guild(fake_pool):
    store = ao.AlwaysOnStore()
    await store.enable(fake_pool, 1, 10)
    assert store.suspend(1) is True
    assert store.is_suspended(1)
    store.lift_suspend(1)
    assert not store.is_suspended(1)


def test_lift_suspend_on_a_never_suspended_guild_is_a_noop():
    store = ao.AlwaysOnStore()
    store.lift_suspend(1)  # must not raise
    assert not store.is_suspended(1)


async def test_evict_drops_cache_with_no_db_write(fake_pool):
    store = ao.AlwaysOnStore()
    await store.enable(fake_pool, 1, 10)
    calls_before = len(fake_pool.calls)
    store.evict(1)
    assert not store.is_enabled(1)
    assert len(fake_pool.calls) == calls_before  # no new DB call


async def test_ordered_guild_ids_is_oldest_enabled_first(fake_pool):
    store = ao.AlwaysOnStore()
    await store.enable(fake_pool, 2, 20)
    await store.enable(fake_pool, 1, 10)
    # 2 was enabled before 1 in THIS process, so it stays first.
    assert store.ordered_guild_ids() == [2, 1]


# ---------------------------------------------------------------------------
# always_on.count_active_sessions
# ---------------------------------------------------------------------------


def _guild_channel_pair(guild_id, channel_id=1):
    guild = types.SimpleNamespace(id=guild_id)
    channel = _FakeChannel(guild=guild, id=channel_id)
    return guild, channel


async def test_count_active_sessions_counts_enabled_connected_guilds(fake_pool):
    store = ao.AlwaysOnStore()
    await store.enable(fake_pool, 1, 10)
    await store.enable(fake_pool, 2, 20)
    _, chan1 = _guild_channel_pair(1)
    _, chan2 = _guild_channel_pair(2)
    bot = types.SimpleNamespace(
        voice_clients=[_FakePlayer(channel=chan1), _FakePlayer(channel=chan2)]
    )
    assert ao.count_active_sessions(bot, store) == 2


async def test_count_active_sessions_excludes_a_suspended_guild(fake_pool):
    # NEGATIVE CONTROL for the count above: suspending one of the two
    # connected, enabled guilds must drop the count by exactly one - proving
    # the function actually reads suspension, not just "is enabled".
    store = ao.AlwaysOnStore()
    await store.enable(fake_pool, 1, 10)
    await store.enable(fake_pool, 2, 20)
    store.suspend(2)
    _, chan1 = _guild_channel_pair(1)
    _, chan2 = _guild_channel_pair(2)
    bot = types.SimpleNamespace(
        voice_clients=[_FakePlayer(channel=chan1), _FakePlayer(channel=chan2)]
    )
    assert ao.count_active_sessions(bot, store) == 1


async def test_count_active_sessions_ignores_a_non_247_player(fake_pool):
    store = ao.AlwaysOnStore()
    await store.enable(fake_pool, 1, 10)
    _, chan1 = _guild_channel_pair(1)
    _, chan2 = _guild_channel_pair(2)  # never enabled
    bot = types.SimpleNamespace(
        voice_clients=[_FakePlayer(channel=chan1), _FakePlayer(channel=chan2)]
    )
    assert ao.count_active_sessions(bot, store) == 1


# ---------------------------------------------------------------------------
# always_on.classify_player_disconnect
# ---------------------------------------------------------------------------


def _event(trigger, extra_data=None):
    return types.SimpleNamespace(trigger=trigger, extra_data=extra_data)


def test_classify_manual_is_own():
    event = _event(sonolink.DisconnectTriggerType.MANUAL)
    assert ao.classify_player_disconnect(event) == ao.OWN


def test_classify_error_with_close_code_is_external():
    ws_closed = types.SimpleNamespace(code=4014, reason="disconnected", by_remote=True)
    event = _event(sonolink.DisconnectTriggerType.ERROR, ws_closed)
    assert ao.classify_player_disconnect(event) == ao.EXTERNAL


def test_classify_error_with_an_exception_is_node_not_external():
    # NEGATIVE CONTROL: same ERROR trigger as the test above, but extra_data
    # has no `.code` - proving the EXTERNAL verdict depends on that attribute,
    # not on the trigger alone.
    event = _event(sonolink.DisconnectTriggerType.ERROR, TimeoutError("no session"))
    assert ao.classify_player_disconnect(event) == ao.NODE


def test_classify_unknown_trigger_is_unknown():
    event = _event(sonolink.DisconnectTriggerType.INACTIVITY)
    assert ao.classify_player_disconnect(event) == ao.UNKNOWN


# ---------------------------------------------------------------------------
# Music._is_247_active
# ---------------------------------------------------------------------------


async def test_is_247_active_true_when_enabled_entitled_not_suspended(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    assert c._is_247_active(1) is True


async def test_is_247_active_false_when_not_enabled(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    assert c._is_247_active(1) is False  # never enabled


async def test_is_247_active_false_when_not_entitled(fake_pool):
    # NEGATIVE CONTROL: configured but the subscription lapsed - "no job"
    # expiry rule (the plan): the setting is kept, but it reads as inactive.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(False))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    assert c._is_247_active(1) is False


async def test_is_247_active_false_when_suspended(fake_pool):
    # NEGATIVE CONTROL: configured AND entitled, but suspended - the
    # mechanism that stops an auto-rejoin loop after an external disconnect.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    c.always_on.suspend(1)
    assert c._is_247_active(1) is False


def test_is_247_active_false_for_none_guild_id():
    bot = _FakeBot(pool=None, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    assert c._is_247_active(None) is False


# ---------------------------------------------------------------------------
# The idle sweeper (_idle_check) skips a 24/7-active guild
# ---------------------------------------------------------------------------


async def test_idle_sweep_skips_a_247_active_guild(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)

    guild = types.SimpleNamespace(id=1)
    channel = _FakeChannel(guild=guild, members=[_FakeMember(1, bot=True)])  # empty of humans
    player = _FakePlayer(channel=channel)
    # Idle long past IDLE_TIMEOUT - without the 24/7 guard this would teardown.
    player.idle_since = time.monotonic() - music_mod.IDLE_TIMEOUT - 1
    bot.voice_clients = [player]

    await music_mod.Music._idle_check.coro(c)

    assert player.disconnect_calls == 0
    assert player.idle_since is None  # reset, not frozen


async def test_idle_sweep_still_disconnects_a_non_247_guild(fake_pool):
    # NEGATIVE CONTROL for the test above: same idle setup, 24/7 NOT active -
    # proves the skip is guild-specific, not a vacuous "never teardown".
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(False))
    c = _make_cog(bot)

    guild = types.SimpleNamespace(id=1)
    channel = _FakeChannel(guild=guild, members=[_FakeMember(1, bot=True)])
    player = _FakePlayer(channel=channel)
    player.idle_since = time.monotonic() - music_mod.IDLE_TIMEOUT - 1
    bot.voice_clients = [player]

    await music_mod.Music._idle_check.coro(c)

    assert player.disconnect_calls == 1


async def test_idle_sweep_resumes_normal_timeout_after_expiry(fake_pool):
    # The exact M4b expiry story: while 24/7 is active every tick keeps
    # resetting idle_since to None (see the test above), so the moment
    # entitlement lapses, idle_since is None - not a stale clock - and this
    # tick starts a FRESH IDLE_TIMEOUT countdown rather than disconnecting
    # instantly off a clock that had actually been running underneath.
    stub = _StubPremium(True)
    bot = _FakeBot(pool=fake_pool, premium_resolver=stub)
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)

    guild = types.SimpleNamespace(id=1)
    channel = _FakeChannel(guild=guild, members=[_FakeMember(1, bot=True)])
    player = _FakePlayer(channel=channel)
    player.idle_since = None  # continuously reset while 24/7 was active
    bot.voice_clients = [player]

    stub._default = False  # entitlement lapses
    await music_mod.Music._idle_check.coro(c)
    assert player.disconnect_calls == 0  # not disconnected instantly
    assert player.idle_since is not None  # the fresh clock started

    # A second tick, IDLE_TIMEOUT later with entitlement still lapsed, now
    # disconnects exactly like a normal (never-247) idle guild would.
    player.idle_since = time.monotonic() - music_mod.IDLE_TIMEOUT - 1
    await music_mod.Music._idle_check.coro(c)
    assert player.disconnect_calls == 1


# ---------------------------------------------------------------------------
# The empty-channel auto-leave (on_voice_state_update) skips a 24/7 guild
# ---------------------------------------------------------------------------


async def test_empty_channel_autoleave_skips_247_active_guild(fake_pool, monkeypatch):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)

    guild = _FakeGuild(1)
    bot_member = _FakeMember(99, bot=True)
    channel = _FakeChannel(guild=guild, members=[bot_member])
    player = _FakePlayer(channel=channel)
    guild.voice_client = player
    bot.user = bot_member

    async def _boom(*_a, **_kw):
        raise AssertionError("asyncio.sleep must not be reached for a 24/7 guild")

    monkeypatch.setattr(music_mod.asyncio, "sleep", _boom)

    leaving = _FakeMember(5, bot=False, guild=guild)
    before = types.SimpleNamespace(channel=channel)
    after = types.SimpleNamespace(channel=None)
    await c.on_voice_state_update(leaving, before, after)

    assert player.disconnect_calls == 0


async def test_empty_channel_autoleave_still_fires_for_a_non_247_guild(
    fake_pool, monkeypatch
):
    # NEGATIVE CONTROL: identical setup, 24/7 not configured - the sleep IS
    # reached (sped up here) and the normal auto-leave still runs.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(False))
    c = _make_cog(bot)

    guild = _FakeGuild(1)
    bot_member = _FakeMember(99, bot=True)
    channel = _FakeChannel(guild=guild, members=[bot_member])
    player = _FakePlayer(channel=channel)
    guild.voice_client = player
    bot.user = bot_member

    slept = []

    async def _fast_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(music_mod.asyncio, "sleep", _fast_sleep)

    leaving = _FakeMember(5, bot=False, guild=guild)
    before = types.SimpleNamespace(channel=channel)
    after = types.SimpleNamespace(channel=None)
    await c.on_voice_state_update(leaving, before, after)

    assert slept == [15]
    assert player.disconnect_calls == 1


# ---------------------------------------------------------------------------
# _restore_one's empty-channel exception for 24/7
# ---------------------------------------------------------------------------


def _restore_row(guild_id, channel_id):
    return {
        "guild_id": guild_id,
        "voice_channel_id": channel_id,
        "home_channel_id": None,
        "dj_id": None,
        "volume": 100,
        "loop_mode": 0,
        "position_ms": 0,
        "paused": False,
        "current_track": "ENCODED",
        "queue": [],
        "controller_message_id": None,
        "autoplay": True,
        "radio_genre": None,
        "effect": None,
    }


async def test_restore_one_proceeds_for_an_empty_channel_when_247_active(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)

    channel = _RealVoiceChannel(10, members=[_FakeMember(1, bot=True)])
    guild = _FakeGuild(1, channels={10: channel})
    channel.guild = guild
    bot._guilds = {1: guild}

    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = _restore_row(1, 10)
    row["updated_at"] = now

    await c._restore_one(row, now)

    assert bot.decode_calls == 1  # reached decode_tracks - gate did NOT block


async def test_restore_one_clears_an_empty_channel_when_not_247(fake_pool):
    # NEGATIVE CONTROL: same empty channel, 24/7 not configured - today's
    # behaviour (do not rejoin an empty room) is unchanged.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(False))
    c = _make_cog(bot)

    channel = _RealVoiceChannel(10, members=[_FakeMember(1, bot=True)])
    guild = _FakeGuild(1, channels={10: channel})
    channel.guild = guild
    bot._guilds = {1: guild}

    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = _restore_row(1, 10)
    row["updated_at"] = now

    await c._restore_one(row, now)

    assert bot.decode_calls == 0  # blocked at the empty-channel gate


# ---------------------------------------------------------------------------
# _bare_join_247 / _turn_off_247
# ---------------------------------------------------------------------------


async def test_bare_join_turns_off_247_when_channel_is_gone(fake_pool, caplog):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)  # channel 10 does not exist
    guild = _FakeGuild(1, channels={})
    bot._guilds = {1: guild}

    with caplog.at_level("WARNING"):
        await c._bare_join_247(1)

    assert not c.always_on.is_enabled(1)
    assert "MUSIC-247-OFF guild=1 reason=channel-deleted" in caplog.text


async def test_bare_join_turns_off_247_on_forbidden(fake_pool, caplog, monkeypatch):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    channel = _RealVoiceChannel(10)
    guild = _FakeGuild(1, channels={10: channel})
    bot._guilds = {1: guild}

    async def _refused(_channel):
        response = types.SimpleNamespace(status=403, reason="Forbidden")
        raise discord.Forbidden(response, "missing Connect")

    monkeypatch.setattr(music_mod, "connect_player", _refused)

    with caplog.at_level("WARNING"):
        await c._bare_join_247(1)

    assert not c.always_on.is_enabled(1)
    assert "MUSIC-247-OFF guild=1 reason=no-permission" in caplog.text


async def test_bare_join_succeeds_and_keeps_247_enabled(fake_pool, monkeypatch):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    channel = _RealVoiceChannel(10)
    guild = _FakeGuild(1, channels={10: channel})
    bot._guilds = {1: guild}

    joined = _FakePlayer(channel=channel)

    async def _connect(_channel):
        return joined

    monkeypatch.setattr(music_mod, "connect_player", _connect)

    await c._bare_join_247(1)

    assert c.always_on.is_enabled(1)  # still on - a successful join
    assert joined.home is channel


# ---------------------------------------------------------------------------
# _admit_and_join: the global ceiling
# ---------------------------------------------------------------------------


async def test_admit_and_join_skips_guilds_past_the_ceiling(fake_pool, monkeypatch, caplog):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    bot.voice_clients = []
    monkeypatch.setattr(ao, "MAX_247_SESSIONS", 1)

    for guild_id in (1, 2, 3):
        await c.always_on.enable(fake_pool, guild_id, 10)

    joined = []

    async def _fake_bare_join(guild_id, **_kw):
        joined.append(guild_id)

    monkeypatch.setattr(c, "_bare_join_247", _fake_bare_join)

    with caplog.at_level("WARNING"):
        await c._admit_and_join([1, 2, 3], {}, label="test")

    assert joined == [1]  # only the budget's worth, oldest-first order kept
    assert "MUSIC-247-CEILING skipped=2 admitted=1 max=1 label=test" in caplog.text


async def test_admit_and_join_admits_everything_under_the_ceiling(fake_pool, monkeypatch):
    # NEGATIVE CONTROL: raise the ceiling well above the candidate count -
    # nothing is skipped, proving the skip above was the ceiling, not a bug.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    bot.voice_clients = []
    monkeypatch.setattr(ao, "MAX_247_SESSIONS", 300)

    for guild_id in (1, 2, 3):
        await c.always_on.enable(fake_pool, guild_id, 10)

    joined = []

    async def _fake_bare_join(guild_id, **_kw):
        joined.append(guild_id)

    monkeypatch.setattr(c, "_bare_join_247", _fake_bare_join)

    await c._admit_and_join([1, 2, 3], {}, label="test")

    assert joined == [1, 2, 3]


# ---------------------------------------------------------------------------
# on_sonolink_player_disconnect
# ---------------------------------------------------------------------------


async def test_player_disconnect_external_suspends_when_channel_still_exists(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    channel = _RealVoiceChannel(10)
    guild = _FakeGuild(1, channels={10: channel})
    channel.guild = guild
    bot._guilds = {1: guild}

    player = _FakePlayer(channel=channel)
    ws_closed = types.SimpleNamespace(code=4014, reason="disconnected", by_remote=True)
    event = _event(sonolink.DisconnectTriggerType.ERROR, ws_closed)

    await c.on_sonolink_player_disconnect(player, event)

    assert c.always_on.is_suspended(1)
    assert c.always_on.is_enabled(1)  # suspended, not disabled


async def test_player_disconnect_external_turns_off_when_channel_is_gone(fake_pool, caplog):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)  # channel 10 does not exist
    guild = _FakeGuild(1, channels={})
    bot._guilds = {1: guild}

    channel = _FakeChannel(guild=guild, id=10)  # the player's OWN last-known channel
    player = _FakePlayer(channel=channel)
    ws_closed = types.SimpleNamespace(code=4014, reason="disconnected", by_remote=True)
    event = _event(sonolink.DisconnectTriggerType.ERROR, ws_closed)

    with caplog.at_level("WARNING"):
        await c.on_sonolink_player_disconnect(player, event)

    assert not c.always_on.is_enabled(1)
    assert "MUSIC-247-OFF guild=1 reason=channel-deleted" in caplog.text


async def test_player_disconnect_own_manual_does_not_suspend(fake_pool):
    # NEGATIVE CONTROL: OUR OWN disconnect (trigger MANUAL - every call this
    # codebase ever makes to player.disconnect()) must never suspend 24/7.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    channel = _RealVoiceChannel(10)
    guild = _FakeGuild(1, channels={10: channel})
    channel.guild = guild
    bot._guilds = {1: guild}
    player = _FakePlayer(channel=channel)
    event = _event(sonolink.DisconnectTriggerType.MANUAL)

    await c.on_sonolink_player_disconnect(player, event)

    assert not c.always_on.is_suspended(1)
    assert c.always_on.is_enabled(1)


async def test_player_disconnect_node_error_does_not_suspend(fake_pool):
    # NEGATIVE CONTROL: a Lavalink/session failure (NODE), not a human action -
    # recovery is the reconnect-rejoin pass, never a suspend.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    channel = _RealVoiceChannel(10)
    guild = _FakeGuild(1, channels={10: channel})
    channel.guild = guild
    bot._guilds = {1: guild}
    player = _FakePlayer(channel=channel)
    event = _event(sonolink.DisconnectTriggerType.ERROR, TimeoutError("session lost"))

    await c.on_sonolink_player_disconnect(player, event)

    assert not c.always_on.is_suspended(1)
    assert c.always_on.is_enabled(1)


async def test_player_disconnect_ignores_a_guild_without_247(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)  # never enabled for guild 1
    channel = _FakeChannel(guild=types.SimpleNamespace(id=1), id=10)
    player = _FakePlayer(channel=channel)
    ws_closed = types.SimpleNamespace(code=4014, reason="x", by_remote=True)
    event = _event(sonolink.DisconnectTriggerType.ERROR, ws_closed)

    await c.on_sonolink_player_disconnect(player, event)  # must not raise

    assert not c.always_on.is_suspended(1)


# ---------------------------------------------------------------------------
# /music alwayson enable|disable|status
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self, guild, author_id=1):
        self.guild = guild
        self.channel = types.SimpleNamespace(id=999)
        self.author = types.SimpleNamespace(id=author_id)
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def last_text(self):
        args, kwargs = self.sent[-1]
        return args[0] if args else kwargs.get("content", "")


async def test_enable_refuses_without_entitlement(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(False))
    c = _make_cog(bot)
    guild = _FakeGuild(1)
    ctx = _FakeCtx(guild)
    channel = _RealVoiceChannel(10, guild=guild)

    await music_mod.Music.alwayson_enable.callback(c, ctx, channel)

    assert "/premium" in ctx.last_text()
    assert not c.always_on.is_enabled(1)
    assert not any("music_247" in call[1] for call in fake_pool.calls if call[0] == "execute")


async def test_enable_stores_and_joins_when_entitled(fake_pool, monkeypatch):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    bot.voice_clients = []
    c = _make_cog(bot)
    guild = _FakeGuild(1)
    ctx = _FakeCtx(guild)
    channel = _RealVoiceChannel(10, guild=guild)

    joined = _FakePlayer(channel=channel)

    async def _connect(_channel):
        return joined

    monkeypatch.setattr(music_mod, "connect_player", _connect)

    await music_mod.Music.alwayson_enable.callback(c, ctx, channel)

    assert c.always_on.is_enabled(1)
    assert c.always_on.channel_id(1) == 10
    assert joined.home is ctx.channel


async def test_enable_refuses_past_the_global_ceiling(fake_pool, monkeypatch):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    bot.voice_clients = []
    c = _make_cog(bot)
    guild = _FakeGuild(1)
    ctx = _FakeCtx(guild)
    channel = _RealVoiceChannel(10, guild=guild)

    monkeypatch.setattr(ao, "count_active_sessions", lambda *_a, **_kw: ao.MAX_247_SESSIONS)

    await music_mod.Music.alwayson_enable.callback(c, ctx, channel)

    assert not c.always_on.is_enabled(1)
    assert "capacity" in ctx.last_text()


async def test_disable_turns_off_and_status_reflects_it(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    guild = _FakeGuild(1, channels={10: _RealVoiceChannel(10, guild=None)})
    await c.always_on.enable(fake_pool, 1, 10)
    ctx = _FakeCtx(guild)

    await music_mod.Music.alwayson_disable.callback(c, ctx)
    assert not c.always_on.is_enabled(1)

    ctx2 = _FakeCtx(guild)
    await c._send_alwayson_status(ctx2)
    assert "off" in ctx2.last_text().lower()


async def test_status_shows_inactive_needs_premium_when_lapsed(fake_pool):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(False))
    c = _make_cog(bot)
    channel = _RealVoiceChannel(10, guild=None)
    guild = _FakeGuild(1, channels={10: channel})
    await c.always_on.enable(fake_pool, 1, 10)
    ctx = _FakeCtx(guild)

    await c._send_alwayson_status(ctx)

    assert "Yasuho+" in ctx.last_text()
    assert "inactive" in ctx.last_text().lower()


# ---------------------------------------------------------------------------
# on_guild_remove
# ---------------------------------------------------------------------------


async def test_on_guild_remove_evicts_and_logs(fake_pool, caplog):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    guild = types.SimpleNamespace(id=1)

    with caplog.at_level("WARNING"):
        await c.on_guild_remove(guild)

    assert not c.always_on.is_enabled(1)
    assert "MUSIC-247-OFF guild=1 reason=bot-removed" in caplog.text


async def test_on_guild_remove_is_silent_for_a_guild_without_247(fake_pool, caplog):
    # NEGATIVE CONTROL: a guild that never had 24/7 produces no log line at
    # all - the listener only reacts to guilds it actually tracked.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    guild = types.SimpleNamespace(id=42)

    with caplog.at_level("WARNING"):
        await c.on_guild_remove(guild)

    assert "MUSIC-247-OFF" not in caplog.text


# ---------------------------------------------------------------------------
# tools/retention.py: the new table rides the usual guild-departure purge
# ---------------------------------------------------------------------------


def test_retention_deletes_music_247_on_guild_purge():
    from tools import retention

    tables = [name for name, _query in retention.GUILD_DELETE_QUERIES]
    assert "music_247" in tables
    query = dict(retention.GUILD_DELETE_QUERIES)["music_247"]
    assert "DELETE FROM music_247 WHERE guild_id = $1" == query


def test_retention_stored_guild_ids_includes_music_247():
    from tools import retention

    assert "music_247" in retention.STORED_GUILD_IDS_QUERY


# ---------------------------------------------------------------------------
# Drift guard: premium.py's catalog already carries music_247 (M4a); this
# lot must not have touched the catalog values, only consumed the field.
# ---------------------------------------------------------------------------


def test_premium_catalog_still_has_the_expected_free_and_premium_values():
    assert premium.GUILD_FREE.music_247 is False
    assert premium.GUILD_PREMIUM.music_247 is True


# ---------------------------------------------------------------------------
# Review fixes (M4b fresh review): the reconnect pass, permissions, messages
# ---------------------------------------------------------------------------


class _LeavingPlayer(_FakePlayer):
    """A player whose disconnect really leaves, like sonolink's does (its
    cleanup() drops the voice client, so guild.voice_client reads None)."""

    def __init__(self, *, guild, **kwargs):
        super().__init__(**kwargs)
        self._guild_ref = guild

    async def disconnect(self, *, force=False):
        self.disconnect_calls += 1
        self._guild_ref.voice_client = None


def _perms(*, view=True, connect=True):
    return types.SimpleNamespace(view_channel=view, connect=connect)


def _reconnect_setup(fake_pool, monkeypatch, *, rows, player_channel_id=10):
    """24/7 guild 1 configured on channel 10, currently connected (to
    ``player_channel_id``), entitled. Returns (cog, guild, player, connects)."""
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    bot.voice_clients = []
    c = _make_cog(bot)
    configured = _RealVoiceChannel(10)
    moved = _RealVoiceChannel(20)
    guild = _FakeGuild(1, channels={10: configured, 20: moved})
    configured.guild = moved.guild = guild
    bot._guilds = {1: guild}
    player = _LeavingPlayer(
        guild=guild, channel=guild.get_channel(player_channel_id)
    )
    guild.voice_client = player

    async def _rows(_pool):
        return rows

    monkeypatch.setattr(music_mod.music_state, "load_all_states", _rows)

    connects = []

    async def _connect(channel):
        connects.append(channel.id)
        joined = _FakePlayer(channel=channel)
        guild.voice_client = joined
        return joined

    monkeypatch.setattr(music_mod, "connect_player", _connect)
    return c, guild, player, connects


async def test_reconnect_pass_bare_joins_when_the_saved_row_is_stale(
    fake_pool, monkeypatch
):
    # THE common 24/7 case: the queue ended long ago, a natural queue end
    # never clears music_state, so the row is older than RESTORE_MAX_AGE.
    # _restore_one gives up on it - the pass must still bring the bot back,
    # it has just force-disconnected it.
    row = _restore_row(1, 10)
    row["updated_at"] = datetime(2000, 1, 1, tzinfo=UTC)
    c, guild, player, connects = _reconnect_setup(
        fake_pool, monkeypatch, rows=[row]
    )
    await c.always_on.enable(fake_pool, 1, 10)

    await c._rejoin_247_after_reconnect()

    assert player.disconnect_calls == 1
    assert connects == [10]
    assert isinstance(guild.voice_client, Player)


async def test_reconnect_pass_without_a_row_bare_joins(fake_pool, monkeypatch):
    # NEGATIVE CONTROL for the fallback: no row at all takes the plain bare
    # join path - one connect, not two (the fallback never doubles a join).
    c, guild, player, connects = _reconnect_setup(fake_pool, monkeypatch, rows=[])
    await c.always_on.enable(fake_pool, 1, 10)

    await c._rejoin_247_after_reconnect()

    assert connects == [10]


async def test_reconnect_pass_does_not_rejoin_a_non_247_guild(fake_pool, monkeypatch):
    # NEGATIVE CONTROL: the same connected guild without 24/7 is not touched.
    c, guild, player, connects = _reconnect_setup(fake_pool, monkeypatch, rows=[])

    await c._rejoin_247_after_reconnect()

    assert player.disconnect_calls == 0
    assert connects == []


async def test_reconnect_pass_keeps_a_moved_bot_where_it_was_put(
    fake_pool, monkeypatch
):
    # A moderator moved the bot from 10 to 20 this session. A node blink must
    # not drag it back to the configured channel.
    c, guild, player, connects = _reconnect_setup(
        fake_pool, monkeypatch, rows=[], player_channel_id=20
    )
    await c.always_on.enable(fake_pool, 1, 10)

    await c._rejoin_247_after_reconnect()

    assert connects == [20]


async def test_reconnect_pass_falls_back_to_configured_if_moved_room_is_gone(
    fake_pool, monkeypatch
):
    # NEGATIVE CONTROL: the room it was moved to no longer resolves - the
    # configured channel is used, not an auto-off.
    c, guild, player, connects = _reconnect_setup(
        fake_pool, monkeypatch, rows=[], player_channel_id=20
    )
    await c.always_on.enable(fake_pool, 1, 10)
    del guild._channels[20]

    await c._rejoin_247_after_reconnect()

    assert connects == [10]
    assert c.always_on.is_enabled(1)


async def test_reconnect_pass_reruns_when_node_ready_lands_mid_pass(
    fake_pool, monkeypatch
):
    c, guild, player, connects = _reconnect_setup(fake_pool, monkeypatch, rows=[])
    await c.always_on.enable(fake_pool, 1, 10)
    passes = []

    async def _pass():
        passes.append(1)
        if len(passes) == 1:
            # A second node_ready arrives while the first pass is running.
            await c._rejoin_247_after_reconnect()

    monkeypatch.setattr(c, "_rejoin_247_pass", _pass)

    await c._rejoin_247_after_reconnect()

    assert len(passes) == 2
    assert c._reconnect_rejoin_running is False


async def test_reconnect_pass_runs_once_without_an_overlapping_event(
    fake_pool, monkeypatch
):
    # NEGATIVE CONTROL for the rerun: no overlapping event, exactly one pass.
    c, guild, player, connects = _reconnect_setup(fake_pool, monkeypatch, rows=[])
    passes = []

    async def _pass():
        passes.append(1)

    monkeypatch.setattr(c, "_rejoin_247_pass", _pass)

    await c._rejoin_247_after_reconnect()

    assert len(passes) == 1


async def test_node_ready_with_a_resumed_session_skips_the_rejoin_pass(
    fake_pool, monkeypatch
):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    c._restored = True  # the startup restore already ran: this is a reconnect
    calls = []

    async def _noop(*_a, **_kw):
        return None

    async def _rejoin():
        calls.append(1)

    monkeypatch.setattr(music_mod.music_state, "save_session", _noop)
    monkeypatch.setattr(c, "_maybe_restore", _noop)
    monkeypatch.setattr(c, "_rejoin_247_after_reconnect", _rejoin)

    await c.on_sonolink_node_ready(types.SimpleNamespace(session_id="s", resumed=True))
    assert calls == []

    # NEGATIVE CONTROL: a fresh (non-resumed) session does run the pass.
    await c.on_sonolink_node_ready(types.SimpleNamespace(session_id="s", resumed=False))
    assert calls == [1]


async def test_bare_join_turns_off_when_the_bot_cannot_connect(
    fake_pool, caplog, monkeypatch
):
    # discord.py joins over the gateway: a missing Connect never raises
    # Forbidden, it only times out. The permission must be read up front.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    channel = _RealVoiceChannel(10)
    channel.permissions_for = lambda _member: _perms(connect=False)
    guild = _FakeGuild(1, channels={10: channel})
    guild.me = object()
    bot._guilds = {1: guild}

    async def _connect(_channel):
        raise AssertionError("must not try a join that can only time out")

    monkeypatch.setattr(music_mod, "connect_player", _connect)

    with caplog.at_level("WARNING"):
        await c._bare_join_247(1)

    assert not c.always_on.is_enabled(1)
    assert "MUSIC-247-OFF guild=1 reason=no-permission" in caplog.text


async def test_bare_join_proceeds_when_the_bot_can_connect(fake_pool, monkeypatch):
    # NEGATIVE CONTROL: same setup with Connect granted - the join happens.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    channel = _RealVoiceChannel(10)
    channel.permissions_for = lambda _member: _perms()
    guild = _FakeGuild(1, channels={10: channel})
    guild.me = object()
    bot._guilds = {1: guild}
    connects = []

    async def _connect(ch):
        connects.append(ch.id)
        return _FakePlayer(channel=ch)

    monkeypatch.setattr(music_mod, "connect_player", _connect)

    await c._bare_join_247(1)

    assert connects == [10]
    assert c.always_on.is_enabled(1)


async def test_bare_join_keeps_247_on_a_connect_timeout(fake_pool, monkeypatch):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)
    channel = _RealVoiceChannel(10)
    guild = _FakeGuild(1, channels={10: channel})
    bot._guilds = {1: guild}

    async def _timeout(_channel):
        raise ConnectionError("Connecting exceeded the 10.00 seconds timeout")

    monkeypatch.setattr(music_mod, "connect_player", _timeout)

    await c._bare_join_247(1)  # must not raise

    assert c.always_on.is_enabled(1)


async def test_enable_refuses_without_connect_and_stores_nothing(
    fake_pool, monkeypatch
):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    bot.voice_clients = []
    c = _make_cog(bot)
    guild = _FakeGuild(1)
    guild.me = object()
    ctx = _FakeCtx(guild)
    channel = _RealVoiceChannel(10, guild=guild)
    channel.permissions_for = lambda _member: _perms(connect=False)

    async def _connect(_channel):
        raise AssertionError("must not try a join that can only time out")

    monkeypatch.setattr(music_mod, "connect_player", _connect)

    await music_mod.Music.alwayson_enable.callback(c, ctx, channel)

    assert not c.always_on.is_enabled(1)
    assert "permission" in ctx.last_text()
    assert not any(
        "music_247" in call[1] for call in fake_pool.calls if call[0] == "execute"
    )


async def test_enable_answers_a_connect_timeout_and_keeps_the_setting(
    fake_pool, monkeypatch
):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    bot.voice_clients = []
    c = _make_cog(bot)
    guild = _FakeGuild(1)
    ctx = _FakeCtx(guild)
    channel = _RealVoiceChannel(10, guild=guild)

    async def _timeout(_channel):
        raise ConnectionError("Connecting exceeded the 10.00 seconds timeout")

    monkeypatch.setattr(music_mod, "connect_player", _timeout)

    await music_mod.Music.alwayson_enable.callback(c, ctx, channel)

    assert c.always_on.is_enabled(1)
    assert "could not join yet" in ctx.last_text()


async def _run_disconnect(c, guild, monkeypatch):
    player = _FakePlayer(channel=_FakeChannel(guild=guild, members=[]))

    async def _require(_ctx, **_kw):
        return player

    monkeypatch.setattr(c, "_require_player", _require)
    ctx = _FakeCtx(guild)
    await music_mod.Music.disconnect.callback(c, ctx)
    return ctx


async def test_disconnect_mentions_the_pause_only_while_247_is_active(
    fake_pool, monkeypatch
):
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)

    ctx = await _run_disconnect(c, _FakeGuild(1), monkeypatch)

    assert "24/7 is paused" in ctx.last_text()
    assert c.always_on.is_suspended(1)


async def test_disconnect_is_plain_for_a_lapsed_247_server(fake_pool, monkeypatch):
    # NEGATIVE CONTROL: the row is kept after Yasuho+ lapsed, but 24/7 is not
    # what kept the bot there - the free server gets the plain message.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(False))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)

    ctx = await _run_disconnect(c, _FakeGuild(1), monkeypatch)

    assert ctx.last_text() == "Disconnected from the voice channel."


async def _press_controller_disconnect(c, guild, monkeypatch, make_interaction):
    from cogs.music import views

    async def _allowed(*_a, **_kw):
        return True

    monkeypatch.setattr(views, "_ensure_can_control", _allowed)
    player = _FakePlayer(channel=_FakeChannel(guild=guild, members=[]))
    stopped = []

    async def _fail(_interaction):
        raise AssertionError("the disconnect button must not fail here")

    view = types.SimpleNamespace(
        cog=c,
        player=player,
        _disable_all=lambda: None,
        stop=lambda: stopped.append(1),
        _report_failure=_fail,
    )
    await views.MusicController._disconnect(view, make_interaction())
    assert player.disconnect_calls == 1
    assert stopped == [1]


async def test_controller_disconnect_button_pauses_247(
    fake_pool, monkeypatch, make_interaction
):
    # Same rule as /music disconnect: otherwise the next Lavalink reconnect
    # pass would pull the bot straight back into the room it was sent out of.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(True))
    c = _make_cog(bot)
    await c.always_on.enable(fake_pool, 1, 10)

    await _press_controller_disconnect(c, _FakeGuild(1), monkeypatch, make_interaction)

    assert c.always_on.is_suspended(1)
    assert not c._is_247_active(1)


async def test_controller_disconnect_button_without_247_suspends_nothing(
    fake_pool, monkeypatch, make_interaction
):
    # NEGATIVE CONTROL: a free server's button press leaves no suspension.
    bot = _FakeBot(pool=fake_pool, premium_resolver=_StubPremium(False))
    c = _make_cog(bot)

    await _press_controller_disconnect(c, _FakeGuild(1), monkeypatch, make_interaction)

    assert not c.always_on.is_suspended(1)

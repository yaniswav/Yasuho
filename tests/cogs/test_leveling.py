"""Unit tests for ``Leveling.level_for_xp`` (cogs/community/leveling.py).

``level_for_xp`` is the single source of truth for turning an XP total into a
level, and the ``rank`` command derives its progress bar from the inverse
``level**2 * 100`` threshold math. These tests pin down three properties:

* the origin case ``xp=0`` maps to level ``0``;
* the function is monotonic non-decreasing across a wide XP range (more XP can
  never lower your level);
* it agrees exactly with the ``cur_threshold``/``next_threshold`` arithmetic the
  ``rank`` command uses, so the card's "into level" / "span" figures stay sane.

The method is a ``@staticmethod`` with no dependencies, so it is called directly
on the class - no cog instance, bot, pool, or event loop required.
"""

import datetime
import json
import types

import discord
import pytest

from cogs.community.leveling import Leveling
from tools import leveling, settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """tools.settings caches user/guild blobs in a process-global singleton
    (see tests/tools/test_settings.py); the new level-up-reward tests below are
    the first in this module to exercise settings.get_user, so an entry left by
    an earlier test (or an earlier run in this same file) must never leak in."""
    settings._cache.clear()
    yield
    settings._cache.clear()


def test_zero_xp_is_level_zero():
    assert Leveling.level_for_xp(0) == 0


def test_monotonic_non_decreasing():
    """More XP must never yield a lower level across a broad range."""
    prev = Leveling.level_for_xp(0)
    for xp in range(0, 100_001):
        level = Leveling.level_for_xp(xp)
        assert level >= prev, f"level dropped at xp={xp}: {level} < {prev}"
        prev = level


def test_matches_rank_threshold_math():
    """Agrees with the ``level**2 * 100`` thresholds used by the rank command.

    For every level, entering XP (``cur_threshold``) and the last XP before the
    next level must both resolve to that level, while ``next_threshold`` rolls
    over to level+1 - exactly what the rank card relies on.
    """
    for level in range(0, 100):
        cur_threshold = level**2 * 100
        next_threshold = (level + 1) ** 2 * 100

        # The threshold math is the inverse of level_for_xp: the entry XP for a
        # level maps back to that same level.
        assert Leveling.level_for_xp(cur_threshold) == level

        # Anywhere inside the band [cur, next) stays on the current level...
        assert Leveling.level_for_xp(next_threshold - 1) == level
        # ...and crossing next_threshold advances exactly one level.
        assert Leveling.level_for_xp(next_threshold) == level + 1


def test_level_boundaries_are_exact():
    """Spot-check the first few hand-computed thresholds and their edges."""
    # (xp, expected level) - boundaries and one below each.
    cases = [
        (0, 0),
        (99, 0),
        (100, 1),
        (399, 1),
        (400, 2),
        (899, 2),
        (900, 3),
        (1599, 3),
        (1600, 4),
    ]
    for xp, expected in cases:
        assert Leveling.level_for_xp(xp) == expected, f"xp={xp}"


def test_is_staticmethod_callable_without_instance():
    """Guard the call contract the rank/levels/on_message paths depend on."""
    assert isinstance(
        Leveling.__dict__["level_for_xp"], staticmethod
    )
    # Callable straight off the class, returning a plain int.
    result = Leveling.level_for_xp(2500)
    assert result == 5
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# on_message hot-path gate (the leveling XP grant that runs for EVERY message)
# ---------------------------------------------------------------------------
#
# These drive on_message directly against fakes to prove the synchronous gate:
# a disabled guild and a command message must cost ZERO DB round-trips, while an
# organic message in an enabled guild earns XP exactly once per cooldown window.


class _FakeChannel:
    def __init__(self, channel_id=100, category_id=None):
        self.id = channel_id
        self.category_id = category_id
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


class _FakeMsgAuthor:
    def __init__(self, uid, is_bot=False, role_ids=(), display_name=None):
        self.id = uid
        self.bot = is_bot
        self.mention = f"<@{uid}>"
        self.display_name = display_name or f"user-{uid}"
        self.roles = [types.SimpleNamespace(id=rid) for rid in role_ids]
        self.dm_sends = []

    async def send(self, *args, **kwargs):
        self.dm_sends.append((args, kwargs))


class _FakeMessage:
    def __init__(
        self,
        content="hello",
        guild_id=1,
        author_id=2,
        is_bot=False,
        channel_id=100,
        category_id=None,
        role_ids=(),
        guild_channels=None,
    ):
        self.content = content
        if guild_id is not None:
            channels = dict(guild_channels or {})
            guild = types.SimpleNamespace(id=guild_id, name="guild")
            guild.get_channel = channels.get
            self.guild = guild
        else:
            self.guild = None
        self.author = _FakeMsgAuthor(author_id, is_bot, role_ids=role_ids)
        self.channel = _FakeChannel(channel_id, category_id)


def _make_bot(
    fake_pool, prefixes=None, default_prefix="?", bot_user_id=999, get_cog=None
):
    return types.SimpleNamespace(
        db_pool=fake_pool,
        prefixes=prefixes if prefixes is not None else {},
        default_prefix=default_prefix,
        user=types.SimpleNamespace(id=bot_user_id),
        # Cross-cog seam the reward-grant hook uses (bot.get_cog("LevelRewards")).
        # Defaults to "no such cog", matching a bot where LevelRewards never
        # loaded - the on_message path must tolerate that silently.
        get_cog=get_cog or (lambda name: None),
    )


def _enable(cog, guild_id=1, **overrides):
    """Arrange an enabled guild directly in the hot-path config map.

    Membership in cog._configs IS "leveling on for this guild"; overrides let a
    test pin a custom cooldown or xp band (e.g. xp_min=xp_max for a fixed gain).
    """
    cog._configs[guild_id] = leveling.LevelConfig(enabled=True, **overrides)


def _fetchval_calls(fake_pool):
    return [c for c in fake_pool.calls if c[0] == "fetchval"]


def _route_fetch(fake_pool, no_xp_rows=None, multiplier_rows=None):
    """A query-aware `fetch` stub: routes the no-xp snapshot query and the L4
    xp_multipliers query to their own configured rows. Needed as soon as ANY
    test exercises on_message, since a grant-eligible message loads BOTH
    snapshots (the single global `fetch_return` FakePool offers cannot serve
    two different row shapes in the same test)."""

    async def fetch(query, *args):
        fake_pool.calls.append(("fetch", query, args))
        if "level_no_xp" in query:
            return no_xp_rows or []
        if "xp_multipliers" in query:
            return multiplier_rows or []
        return []

    fake_pool.fetch = fetch


async def test_disabled_guild_grants_no_xp_and_hits_no_db(fake_pool):
    """The default (leveling off) must cost zero awaits and zero DB round-trips."""
    cog = Leveling(_make_bot(fake_pool))  # _configs empty by default
    msg = _FakeMessage(content="hello there", guild_id=1, author_id=2)
    await cog.on_message(msg)
    assert fake_pool.calls == []
    assert len(cog._cooldowns) == 0


async def test_bot_author_is_ignored(fake_pool):
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)
    msg = _FakeMessage(content="hello", guild_id=1, author_id=2, is_bot=True)
    await cog.on_message(msg)
    assert fake_pool.calls == []


async def test_direct_message_is_ignored(fake_pool):
    cog = Leveling(_make_bot(fake_pool))
    msg = _FakeMessage(content="hello", guild_id=None, author_id=2)
    await cog.on_message(msg)
    assert fake_pool.calls == []


async def test_command_message_grants_no_xp(fake_pool):
    """A prefix command must not earn XP nor even start the cooldown."""
    cog = Leveling(_make_bot(fake_pool, default_prefix="?"))
    _enable(cog, 1)
    msg = _FakeMessage(content="?rank", guild_id=1, author_id=2)
    await cog.on_message(msg)
    assert fake_pool.calls == []
    assert len(cog._cooldowns) == 0


async def test_guild_custom_prefix_blocks_xp(fake_pool):
    cog = Leveling(_make_bot(fake_pool, prefixes={1: "y!"}))
    _enable(cog, 1)
    msg = _FakeMessage(content="y!rank", guild_id=1, author_id=2)
    await cog.on_message(msg)
    assert fake_pool.calls == []


async def test_bot_mention_command_grants_no_xp(fake_pool):
    cog = Leveling(_make_bot(fake_pool, bot_user_id=999))
    _enable(cog, 1)
    msg = _FakeMessage(content="<@999> rank", guild_id=1, author_id=2)
    await cog.on_message(msg)
    assert fake_pool.calls == []


async def test_organic_message_grants_xp(fake_pool):
    fake_pool.fetchval_return = 11000  # mid-band: no level-up regardless of gain
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)
    msg = _FakeMessage(content="hello everyone", guild_id=1, author_id=2)
    await cog.on_message(msg)

    writes = _fetchval_calls(fake_pool)
    assert len(writes) == 1
    _method, query, args = writes[0]
    assert "INSERT INTO levels" in query
    guild_id, user_id, gain = args
    assert (guild_id, user_id) == (1, 2)
    assert 15 <= gain <= 25
    assert msg.channel.sends == []  # no level-up announce mid-band


async def test_grant_honours_per_guild_xp_range(fake_pool):
    """The XP gain is drawn from the guild's own xp band, not a hard-coded 15-25."""
    fake_pool.fetchval_return = 11000
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=7, xp_max=7)  # degenerate band -> gain is exactly 7
    await cog.on_message(_FakeMessage(content="hello", guild_id=1, author_id=2))
    (_method, _query, args), = _fetchval_calls(fake_pool)
    assert args[2] == 7


async def test_cooldown_blocks_second_message(fake_pool):
    fake_pool.fetchval_return = 11000
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)  # default 60s cooldown
    await cog.on_message(_FakeMessage(content="hi", guild_id=1, author_id=2))
    await cog.on_message(_FakeMessage(content="hey again", guild_id=1, author_id=2))
    assert len(_fetchval_calls(fake_pool)) == 1  # only the first earned XP


async def test_zero_cooldown_lets_every_message_earn(fake_pool):
    """A per-guild cooldown of 0 disables debouncing - proving the value is used."""
    fake_pool.fetchval_return = 11000
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, cooldown_seconds=0)
    await cog.on_message(_FakeMessage(content="one", guild_id=1, author_id=2))
    await cog.on_message(_FakeMessage(content="two", guild_id=1, author_id=2))
    assert len(_fetchval_calls(fake_pool)) == 2  # both earned: 0s window = no wait


# ---------------------------------------------------------------------------
# level_config load + toggle seam (the JSONB -> table read-through migration)
# ---------------------------------------------------------------------------


def _level_config_row(guild_id, **overrides):
    """A full level_config row as the DB would return it, defaults overridable."""
    row = {
        "guild_id": guild_id,
        "enabled": True,
        "cooldown_seconds": 60,
        "xp_min": 15,
        "xp_max": 25,
        "announce_mode": "channel",
        "announce_channel_id": None,
        "announce_template": None,
    }
    row.update(overrides)
    return row


async def test_cog_load_prefers_level_config_row_over_legacy_jsonb(fake_pool):
    """A level_config row wins over the legacy JSONB, and disabled rows stay off.

    Guild 10 has an enabled row with a custom cooldown AND a legacy JSONB true -
    the row must win (custom cooldown kept, not resurrected as a default). Guild 11
    has a disabled row -> off. Guild 20 has only the legacy bool -> default-enabled.
    """
    level_rows = [
        _level_config_row(10, cooldown_seconds=30),
        _level_config_row(11, enabled=False),
    ]
    legacy_rows = [{"guild_id": 20}, {"guild_id": 10}]  # 10 also in the legacy set

    async def fetch(query, *args):
        fake_pool.calls.append(("fetch", query, args))
        return level_rows if "level_config" in query else legacy_rows

    fake_pool.fetch = fetch
    cog = Leveling(_make_bot(fake_pool))
    await cog.cog_load()

    assert set(cog._configs) == {10, 20}
    assert cog._configs[10].cooldown_seconds == 30  # row wins over the JSONB default
    assert cog._configs[20] == leveling.LevelConfig(enabled=True)  # legacy fallback


async def test_cog_load_survives_db_error(fake_pool):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    fake_pool.fetch = boom
    cog = Leveling(_make_bot(fake_pool))
    await cog.cog_load()  # must not raise
    assert cog._configs == {}


async def test_set_enabled_on_writes_row_and_caches_config(fake_pool):
    fake_pool.fetchrow_return = _level_config_row(1)
    cog = Leveling(_make_bot(fake_pool))
    await cog.set_enabled(1, True)

    assert cog.is_enabled(1)
    writes = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert len(writes) == 1
    _method, query, args = writes[0]
    assert "INSERT INTO level_config" in query
    assert args == (1, True)


async def test_set_enabled_off_removes_from_cache(fake_pool):
    fake_pool.fetchrow_return = _level_config_row(1, enabled=False)
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)  # start enabled
    await cog.set_enabled(1, False)
    assert not cog.is_enabled(1)


def test_is_enabled_reflects_the_config_map(fake_pool):
    cog = Leveling(_make_bot(fake_pool))
    assert cog.is_enabled(5) is False
    _enable(cog, 5)
    assert cog.is_enabled(5) is True


# ---------------------------------------------------------------------------
# Level-reward announce integration (cross-cog seam: bot.get_cog("LevelRewards"))
# ---------------------------------------------------------------------------
#
# The pure add/remove decision math lives in tools/level_rewards.py and the
# cog-level role application in tests/cogs/test_level_rewards.py; these tests
# only pin the Leveling side of the seam: the reward cog is called on every
# level-up (never per message), roles are granted regardless of the announce
# opt-out, the announce message gains a suffix only when something was granted
# AND announcing is on, and a rewards-cog failure never breaks the announce.


class _FakeGrantedRole:
    def __init__(self, role_id):
        self.id = role_id
        self.mention = f"<@&{role_id}>"


class _FakeRewardsCog:
    """Stand-in for cogs.community.level_rewards.LevelRewards."""

    def __init__(self, granted=None, raises=None):
        self.granted = list(granted or [])
        self.raises = raises
        self.calls = []

    async def grant_for_levelup(self, guild, member, old_level, new_level):
        self.calls.append((guild.id, member.id, old_level, new_level))
        if self.raises is not None:
            raise self.raises
        return self.granted


def _route_fetchval(fake_pool, xp_value, user_settings_raw=None):
    """A query-aware fetchval stub: routes the XP upsert and the settings.get_user
    read to their own configured return values (the single global
    ``fetchval_return`` FakePool offers cannot serve both in the same test, since
    a level-up in on_message calls fetchval twice for two different purposes).
    """

    async def fetchval(query, *args):
        fake_pool.calls.append(("fetchval", query, args))
        if "INSERT INTO levels" in query:
            return xp_value
        if "user_settings" in query:
            return user_settings_raw
        return None

    fake_pool.fetchval = fetchval


async def test_levelup_grants_roles_and_appends_announce_suffix(fake_pool):
    reward_role = _FakeGrantedRole(55)
    rewards_cog = _FakeRewardsCog(granted=[reward_role])
    bot = _make_bot(
        fake_pool,
        get_cog=lambda name: rewards_cog if name == "LevelRewards" else None,
    )
    cog = Leveling(bot)
    _enable(cog, 1, xp_min=1, xp_max=1)  # deterministic +1 XP per message
    _route_fetchval(fake_pool, xp_value=10000)  # old=9999 (lvl 9) -> new=10000 (lvl 10)

    msg = _FakeMessage(content="hello", guild_id=1, author_id=2)
    await cog.on_message(msg)

    assert rewards_cog.calls == [(1, 2, 9, 10)]  # old_level, new_level passed through
    assert len(msg.channel.sends) == 1
    text = msg.channel.sends[0][0][0]
    assert "reached level **10**" in text
    assert "<@&55>" in text  # the granted role's mention is in the suffix

    # The reward-role mention must NOT mass-ping every holder of that role: the
    # send suppresses role/@everyone pings while keeping the leveler's own ping.
    allowed = msg.channel.sends[0][1]["allowed_mentions"]
    assert allowed.roles is False
    assert allowed.everyone is False
    assert allowed.users is True


async def test_levelup_opt_out_still_grants_roles_but_skips_announce(fake_pool):
    """The announce opt-out controls only the announce MESSAGE - reward roles
    are granted either way (grant_for_levelup runs outside the opt-out gate)."""
    reward_role = _FakeGrantedRole(55)
    rewards_cog = _FakeRewardsCog(granted=[reward_role])
    bot = _make_bot(
        fake_pool,
        get_cog=lambda name: rewards_cog if name == "LevelRewards" else None,
    )
    cog = Leveling(bot)
    _enable(cog, 1, xp_min=1, xp_max=1)
    _route_fetchval(
        fake_pool,
        xp_value=10000,
        user_settings_raw=json.dumps({"levelup_announce": False}),
    )

    msg = _FakeMessage(content="hello", guild_id=1, author_id=3)
    await cog.on_message(msg)

    assert rewards_cog.calls == [(1, 3, 9, 10)]  # still granted
    assert msg.channel.sends == []  # but nothing announced


async def test_levelup_without_rewards_cog_announces_plain(fake_pool):
    """No LevelRewards cog loaded: get_cog returns None, announce is unaffected."""
    bot = _make_bot(fake_pool)  # default get_cog -> always None
    cog = Leveling(bot)
    _enable(cog, 1, xp_min=1, xp_max=1)
    _route_fetchval(fake_pool, xp_value=10000)

    msg = _FakeMessage(content="hello", guild_id=1, author_id=4)
    await cog.on_message(msg)

    assert len(msg.channel.sends) == 1
    text = msg.channel.sends[0][0][0]
    assert "reached level **10**" in text
    assert "earned" not in text


async def test_rewards_cog_failure_does_not_break_the_announce(fake_pool):
    rewards_cog = _FakeRewardsCog(raises=RuntimeError("boom"))
    bot = _make_bot(
        fake_pool,
        get_cog=lambda name: rewards_cog if name == "LevelRewards" else None,
    )
    cog = Leveling(bot)
    _enable(cog, 1, xp_min=1, xp_max=1)
    _route_fetchval(fake_pool, xp_value=10000)

    msg = _FakeMessage(content="hello", guild_id=1, author_id=5)
    await cog.on_message(msg)  # must not raise

    assert len(msg.channel.sends) == 1
    text = msg.channel.sends[0][0][0]
    assert "reached level **10**" in text
    assert "earned" not in text  # the failed grant produced no roles to suffix


async def test_no_levelup_never_calls_the_rewards_cog(fake_pool):
    """Mid-band messages (no level crossed) must not touch the rewards seam at
    all - grants happen on level-up only, never per message (SCALE STORY)."""
    rewards_cog = _FakeRewardsCog(granted=[_FakeGrantedRole(55)])
    bot = _make_bot(
        fake_pool,
        get_cog=lambda name: rewards_cog if name == "LevelRewards" else None,
    )
    cog = Leveling(bot)
    _enable(cog, 1)
    fake_pool.fetchval_return = 11000  # mid-band, no level crossed (see above)

    msg = _FakeMessage(content="hello", guild_id=1, author_id=6)
    await cog.on_message(msg)

    assert rewards_cog.calls == []


# ---------------------------------------------------------------------------
# No-XP zones (L3) hot-path integration.
# ---------------------------------------------------------------------------
#
# The pure decision (is_no_xp_message) is covered in
# tests/tools/test_leveling_service.py; these tests pin the COG side of the
# seam: the snapshot is loaded from the DB at most ONCE per guild (a genuine
# cache, not a per-message query), a muted channel/category/role blocks the
# grant AND never even starts the cooldown, and refresh_no_xp_snapshot (the
# cross-cog hook cogs/community/level_config_ui.py calls after every write)
# makes a change visible on the very next message.


def _no_xp_fetch_calls(fake_pool):
    return [c for c in fake_pool.calls if c[0] == "fetch" and "level_no_xp" in c[1]]


async def test_no_xp_snapshot_is_loaded_once_then_cached(fake_pool):
    """A guild with no no-xp rows configured (fetch_return == [], the default)
    still costs exactly one `fetch` for its first message and zero for later
    ones - the cache holds the EMPTY snapshot, not a sentinel that re-queries."""
    fake_pool.fetchval_return = 11000  # mid-band, no level-up noise
    _route_fetch(fake_pool)
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    await cog.on_message(_FakeMessage(content="one", guild_id=1, author_id=2))
    await cog.on_message(_FakeMessage(content="two", guild_id=1, author_id=3))

    assert len(_no_xp_fetch_calls(fake_pool)) == 1
    assert 1 in cog._no_xp


async def test_no_xp_channel_blocks_grant_and_never_touches_cooldown(fake_pool):
    _route_fetch(fake_pool, no_xp_rows=[{"kind": "channel", "target_id": 100}])
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    msg = _FakeMessage(content="hi", guild_id=1, author_id=2, channel_id=100)
    await cog.on_message(msg)

    assert _fetchval_calls(fake_pool) == []  # no XP grant query at all
    assert len(cog._cooldowns) == 0  # the cooldown was never started either


async def test_no_xp_category_blocks_every_channel_inside_it(fake_pool):
    # a category id
    _route_fetch(fake_pool, no_xp_rows=[{"kind": "channel", "target_id": 50}])
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    msg = _FakeMessage(
        content="hi", guild_id=1, author_id=2, channel_id=999, category_id=50
    )
    await cog.on_message(msg)

    assert _fetchval_calls(fake_pool) == []


async def test_no_xp_role_blocks_grant(fake_pool):
    _route_fetch(fake_pool, no_xp_rows=[{"kind": "role", "target_id": 77}])
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    msg = _FakeMessage(content="hi", guild_id=1, author_id=2, role_ids=[77, 88])
    await cog.on_message(msg)

    assert _fetchval_calls(fake_pool) == []


async def test_no_xp_zone_does_not_affect_an_unrelated_channel(fake_pool):
    _route_fetch(fake_pool, no_xp_rows=[{"kind": "channel", "target_id": 100}])
    fake_pool.fetchval_return = 11000
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    msg = _FakeMessage(content="hi", guild_id=1, author_id=2, channel_id=200)
    await cog.on_message(msg)

    assert len(_fetchval_calls(fake_pool)) == 1  # earned XP normally


async def test_refresh_no_xp_snapshot_reloads_from_the_db(fake_pool):
    """The cross-cog hook: after a level_no_xp write, the caller re-reads the
    guild's rows and the NEW snapshot takes effect immediately, no restart."""
    _route_fetch(fake_pool)
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    await cog.on_message(_FakeMessage(guild_id=1, author_id=2, channel_id=100))
    assert 1 in cog._no_xp
    assert cog._no_xp[1].channels == frozenset()

    # A channel gets muted; the config UI cog calls this after the DB write.
    _route_fetch(fake_pool, no_xp_rows=[{"kind": "channel", "target_id": 100}])
    await cog.refresh_no_xp_snapshot(1)
    assert cog._no_xp[1].channels == frozenset({100})

    # The NEXT message in that channel is blocked without any further fetch.
    fetches_before = len(_no_xp_fetch_calls(fake_pool))
    msg = _FakeMessage(guild_id=1, author_id=3, channel_id=100)
    await cog.on_message(msg)
    assert len(_no_xp_fetch_calls(fake_pool)) == fetches_before  # cache hit
    assert msg.channel.sends == []


async def test_no_xp_empty_snapshot_short_circuits_before_the_membership_test(
    fake_pool, monkeypatch
):
    """HOT PATH allocation guard: a guild that configured NO zones must not even
    CALL is_no_xp_message (so the role-id generator is never built and
    Member.roles is never touched) - the `no_xp.channels or no_xp.roles` guard
    `and`-short-circuits first. This is the proxy for "zero allocations in the
    common case": the membership test, and the generator handed to it, are
    skipped entirely."""
    calls = []
    real = leveling.is_no_xp_message
    monkeypatch.setattr(
        leveling,
        "is_no_xp_message",
        lambda *a, **k: (calls.append(a), real(*a, **k))[1],
    )
    _route_fetch(fake_pool)  # no zones -> the EMPTY snapshot is cached
    fake_pool.fetchval_return = 11000
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    await cog.on_message(_FakeMessage(guild_id=1, author_id=2, role_ids=[7, 8]))

    assert calls == []  # short-circuited before is_no_xp_message
    assert len(_fetchval_calls(fake_pool)) == 1  # still earned XP normally


async def test_no_xp_nonempty_snapshot_does_run_the_membership_test(
    fake_pool, monkeypatch
):
    """The other side of the guard: a guild that DID configure a zone reaches
    is_no_xp_message (here with an unrelated role, so the message still earns)."""
    calls = []
    real = leveling.is_no_xp_message
    monkeypatch.setattr(
        leveling,
        "is_no_xp_message",
        lambda *a, **k: (calls.append(a), real(*a, **k))[1],
    )
    _route_fetch(fake_pool, no_xp_rows=[{"kind": "role", "target_id": 999}])
    fake_pool.fetchval_return = 11000
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    await cog.on_message(_FakeMessage(guild_id=1, author_id=2, role_ids=[7, 8]))

    assert len(calls) == 1  # the membership test ran (unrelated role -> allowed)
    assert len(_fetchval_calls(fake_pool)) == 1


# ---------------------------------------------------------------------------
# Announce control (L3): mode routing + the levelup_ping "no ping" flavour.
# ---------------------------------------------------------------------------
#
# resolve_announce_target/render_announce_template's pure decisions are
# covered in tests/tools/test_leveling_service.py; these drive
# _announce_levelup end to end through on_message against fakes for each
# route, proving the opt-out gate applies in every mode and levelup_ping
# swaps a mention for plain text without changing anything else.


def _route_fetchval_multi(fake_pool, xp_value, user_prefs):
    """Like _route_fetchval but serves DISTINCT per-user settings blobs, so a
    test can pin both levelup_announce and levelup_ping independently. Reads
    the level_config-less path: only the XP upsert and user_settings queries
    are routed (the cog never reads level_config directly - config is already
    in cog._configs for these tests, per _enable)."""

    async def fetchval(query, *args):
        fake_pool.calls.append(("fetchval", query, args))
        if "INSERT INTO levels" in query:
            return xp_value
        if "user_settings" in query:
            user_id = args[0]
            return json.dumps(user_prefs.get(user_id, {}))
        return None

    fake_pool.fetchval = fetchval


async def test_announce_mode_off_grants_roles_but_sends_nothing(fake_pool):
    reward_role = _FakeGrantedRole(55)
    rewards_cog = _FakeRewardsCog(granted=[reward_role])
    bot = _make_bot(
        fake_pool, get_cog=lambda name: rewards_cog if name == "LevelRewards" else None
    )
    cog = Leveling(bot)
    _enable(cog, 1, xp_min=1, xp_max=1, announce_mode="off")
    _route_fetchval(fake_pool, xp_value=10000)

    msg = _FakeMessage(guild_id=1, author_id=2)
    await cog.on_message(msg)

    assert rewards_cog.calls == [(1, 2, 9, 10)]  # roles still granted
    assert msg.channel.sends == []


async def test_announce_mode_dm_sends_to_the_member_not_the_channel(fake_pool):
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=1, xp_max=1, announce_mode="dm")
    _route_fetchval(fake_pool, xp_value=10000)

    msg = _FakeMessage(guild_id=1, author_id=2)
    await cog.on_message(msg)

    assert msg.channel.sends == []
    assert len(msg.author.dm_sends) == 1
    assert "reached level **10**" in msg.author.dm_sends[0][0][0]


async def test_announce_mode_dm_closed_dms_is_quiet(fake_pool):
    """discord.Forbidden on the DM (closed DMs) never breaks the level-up."""

    class _ClosedDMAuthor(_FakeMsgAuthor):
        async def send(self, *args, **kwargs):
            raise discord.Forbidden(
                types.SimpleNamespace(status=403, reason="Forbidden"), "Cannot send"
            )

    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=1, xp_max=1, announce_mode="dm")
    _route_fetchval(fake_pool, xp_value=10000)

    msg = _FakeMessage(guild_id=1, author_id=2)
    msg.author = _ClosedDMAuthor(2)
    await cog.on_message(msg)  # must not raise


async def test_announce_mode_fixed_sends_to_the_configured_channel(fake_pool):
    fixed_channel = _FakeChannel(channel_id=555)
    cog = Leveling(_make_bot(fake_pool))
    _enable(
        cog, 1, xp_min=1, xp_max=1, announce_mode="fixed", announce_channel_id=555
    )
    _route_fetchval(fake_pool, xp_value=10000)

    msg = _FakeMessage(guild_id=1, author_id=2, guild_channels={555: fixed_channel})
    await cog.on_message(msg)

    assert msg.channel.sends == []  # NOT the message's own channel
    assert len(fixed_channel.sends) == 1
    assert "reached level **10**" in fixed_channel.sends[0][0][0]


async def test_announce_mode_fixed_with_deleted_channel_is_quiet(fake_pool):
    """The fixed channel is missing from the guild cache (deleted) - no crash,
    no fallback send anywhere else (roles are still granted by the caller)."""
    cog = Leveling(_make_bot(fake_pool))
    _enable(
        cog, 1, xp_min=1, xp_max=1, announce_mode="fixed", announce_channel_id=555
    )
    _route_fetchval(fake_pool, xp_value=10000)

    msg = _FakeMessage(guild_id=1, author_id=2)  # no channel 555 registered
    await cog.on_message(msg)  # must not raise

    assert msg.channel.sends == []


async def test_announce_opt_out_applies_in_dm_mode_too(fake_pool):
    """The per-user opt-out is checked before the mode routing, so it silences
    every route, not just the default channel one."""
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=1, xp_max=1, announce_mode="dm")
    _route_fetchval(
        fake_pool, xp_value=10000, user_settings_raw=json.dumps({"levelup_announce": False})
    )

    msg = _FakeMessage(guild_id=1, author_id=2)
    await cog.on_message(msg)

    assert msg.channel.sends == []
    assert msg.author.dm_sends == []


async def test_announce_opt_out_applies_in_fixed_mode_too(fake_pool):
    """Fixed mode is gated by the SAME opt-out: an opted-out member gets nothing
    in the fixed channel, the origin channel, or a DM (roles still granted)."""
    fixed_channel = _FakeChannel(channel_id=555)
    cog = Leveling(_make_bot(fake_pool))
    _enable(
        cog, 1, xp_min=1, xp_max=1, announce_mode="fixed", announce_channel_id=555
    )
    _route_fetchval(
        fake_pool,
        xp_value=10000,
        user_settings_raw=json.dumps({"levelup_announce": False}),
    )

    msg = _FakeMessage(guild_id=1, author_id=2, guild_channels={555: fixed_channel})
    await cog.on_message(msg)

    assert fixed_channel.sends == []
    assert msg.channel.sends == []
    assert msg.author.dm_sends == []


async def test_levelup_ping_off_is_plain_text_in_dm_mode_too(fake_pool):
    """ping-off applies in every mode: a DM announce names the member without a
    mention just like the channel route does."""
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=1, xp_max=1, announce_mode="dm")
    _route_fetchval_multi(
        fake_pool, xp_value=10000, user_prefs={2: {"levelup_ping": False}}
    )

    msg = _FakeMessage(guild_id=1, author_id=2)
    await cog.on_message(msg)

    assert msg.channel.sends == []
    text = msg.author.dm_sends[0][0][0]
    assert msg.author.mention not in text
    assert msg.author.display_name in text


async def test_levelup_ping_off_names_the_member_without_mentioning_them(fake_pool):
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=1, xp_max=1)
    _route_fetchval_multi(
        fake_pool, xp_value=10000, user_prefs={2: {"levelup_ping": False}}
    )

    msg = _FakeMessage(guild_id=1, author_id=2)
    await cog.on_message(msg)

    text = msg.channel.sends[0][0][0]
    assert msg.author.mention not in text
    assert msg.author.display_name in text


async def test_levelup_ping_default_on_mentions_the_member(fake_pool):
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=1, xp_max=1)
    _route_fetchval(fake_pool, xp_value=10000)

    msg = _FakeMessage(guild_id=1, author_id=2)
    await cog.on_message(msg)

    assert msg.author.mention in msg.channel.sends[0][0][0]


async def test_custom_announce_template_is_rendered_with_the_roles_suffix(fake_pool):
    reward_role = _FakeGrantedRole(55)
    rewards_cog = _FakeRewardsCog(granted=[reward_role])
    bot = _make_bot(
        fake_pool, get_cog=lambda name: rewards_cog if name == "LevelRewards" else None
    )
    cog = Leveling(bot)
    _enable(
        cog,
        1,
        xp_min=1,
        xp_max=1,
        announce_template="Woo {user}, level {level} in {guild}!",
    )
    _route_fetchval(fake_pool, xp_value=10000)

    msg = _FakeMessage(guild_id=1, author_id=2)
    await cog.on_message(msg)

    text = msg.channel.sends[0][0][0]
    assert "Woo <@2>, level 10 in guild!" in text
    assert "and earned <@&55>" in text


# ---------------------------------------------------------------------------
# set_announce_mode / set_announce_template: the level_config writers used by
# cogs/community/level_config_ui.py. Mirrors set_enabled's own tests plus the
# `enabled`-seeded-from-legacy-JSONB upsert shape level_rewards_mode pins.
# ---------------------------------------------------------------------------


async def test_set_announce_mode_writes_row_and_caches_config(fake_pool):
    fake_pool.fetchrow_return = _level_config_row(
        1, announce_mode="dm", announce_channel_id=None
    )
    cog = Leveling(_make_bot(fake_pool))
    await cog.set_announce_mode(1, "dm")

    assert cog._configs[1].announce_mode == "dm"
    writes = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    assert len(writes) == 1
    _method, query, args = writes[0]
    assert "INSERT INTO level_config" in query
    assert "COALESCE" in query  # enabled seeded from legacy JSONB, never clobbered
    assert "enabled = " not in query.split("RETURNING")[0].split("DO UPDATE")[1]
    assert args == (1, "dm", None)


async def test_set_announce_mode_fixed_stores_the_channel_id(fake_pool):
    fake_pool.fetchrow_return = _level_config_row(
        1, announce_mode="fixed", announce_channel_id=999
    )
    cog = Leveling(_make_bot(fake_pool))
    await cog.set_announce_mode(1, "fixed", 999)

    assert cog._configs[1].announce_mode == "fixed"
    assert cog._configs[1].announce_channel_id == 999


async def test_set_announce_mode_disabled_row_drops_the_guild_from_the_cache(fake_pool):
    fake_pool.fetchrow_return = _level_config_row(1, enabled=False)
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)
    await cog.set_announce_mode(1, "channel")
    assert not cog.is_enabled(1)


async def test_set_announce_template_writes_row_and_caches_config(fake_pool):
    fake_pool.fetchrow_return = _level_config_row(
        1, announce_template="gg {user}"
    )
    cog = Leveling(_make_bot(fake_pool))
    await cog.set_announce_template(1, "gg {user}")

    assert cog._configs[1].announce_template == "gg {user}"
    writes = [c for c in fake_pool.calls if c[0] == "fetchrow"]
    _method, query, args = writes[0]
    assert "INSERT INTO level_config" in query
    assert args == (1, "gg {user}")


async def test_set_announce_template_none_resets_it(fake_pool):
    fake_pool.fetchrow_return = _level_config_row(1, announce_template=None)
    cog = Leveling(_make_bot(fake_pool))
    await cog.set_announce_template(1, None)
    assert cog._configs[1].announce_template is None


# ---------------------------------------------------------------------------
# Voice XP level-up routing (credit_voice_levelup): the seam the VoiceXP cog
# calls per credited member who crossed a level. It must behave IDENTICALLY to a
# message level-up - grant reward roles regardless of the announce opt-out, and
# announce per the guild's announce_mode - only here the origin "channel" is the
# VOICE channel's own text chat (the `channel` passed in).
# ---------------------------------------------------------------------------


def _voice_guild(guild_id=1, channels=None):
    channels = channels or {}
    return types.SimpleNamespace(
        id=guild_id, name="guild", get_channel=channels.get
    )


async def test_credit_voice_levelup_announces_to_the_voice_channel_and_grants(
    fake_pool,
):
    reward_role = _FakeGrantedRole(55)
    rewards_cog = _FakeRewardsCog(granted=[reward_role])
    bot = _make_bot(
        fake_pool,
        get_cog=lambda name: rewards_cog if name == "LevelRewards" else None,
    )
    cog = Leveling(bot)
    member = _FakeMsgAuthor(2)
    voice_channel = _FakeChannel(channel_id=777)  # a voice channel's text chat
    guild = _voice_guild(1)
    config = leveling.LevelConfig(enabled=True, announce_mode="channel")

    await cog.credit_voice_levelup(
        guild=guild,
        member=member,
        channel=voice_channel,
        config=config,
        old_xp=9975,  # level 9
        new_xp=10000,  # level 10 -> crossed
    )

    assert rewards_cog.calls == [(1, 2, 9, 10)]  # old_level, new_level passed
    assert len(voice_channel.sends) == 1
    text = voice_channel.sends[0][0][0]
    assert "reached level **10**" in text
    assert "<@&55>" in text  # granted role mention in the suffix
    # Role/@everyone pings stay suppressed (no mass ping of the reward role).
    allowed = voice_channel.sends[0][1]["allowed_mentions"]
    assert allowed.roles is False and allowed.everyone is False


async def test_credit_voice_levelup_no_threshold_crossed_is_a_noop(fake_pool):
    rewards_cog = _FakeRewardsCog(granted=[_FakeGrantedRole(55)])
    bot = _make_bot(
        fake_pool,
        get_cog=lambda name: rewards_cog if name == "LevelRewards" else None,
    )
    cog = Leveling(bot)
    member = _FakeMsgAuthor(2)
    voice_channel = _FakeChannel(channel_id=777)
    config = leveling.LevelConfig(enabled=True, announce_mode="channel")

    # Both totals sit inside level 10 -> not a level-up.
    await cog.credit_voice_levelup(
        guild=_voice_guild(1),
        member=member,
        channel=voice_channel,
        config=config,
        old_xp=10975,
        new_xp=11000,
    )

    assert rewards_cog.calls == []  # the reward seam is never touched
    assert voice_channel.sends == []  # nothing announced


async def test_credit_voice_levelup_respects_announce_off(fake_pool):
    """announce_mode 'off' still grants roles but sends nothing (parity with the
    message path)."""
    reward_role = _FakeGrantedRole(55)
    rewards_cog = _FakeRewardsCog(granted=[reward_role])
    bot = _make_bot(
        fake_pool,
        get_cog=lambda name: rewards_cog if name == "LevelRewards" else None,
    )
    cog = Leveling(bot)
    member = _FakeMsgAuthor(2)
    voice_channel = _FakeChannel(channel_id=777)
    config = leveling.LevelConfig(enabled=True, announce_mode="off")

    await cog.credit_voice_levelup(
        guild=_voice_guild(1),
        member=member,
        channel=voice_channel,
        config=config,
        old_xp=9975,
        new_xp=10000,
    )

    assert rewards_cog.calls == [(1, 2, 9, 10)]  # roles still granted
    assert voice_channel.sends == []  # but nothing announced


# ---------------------------------------------------------------------------
# XP multipliers (L4) hot-path integration.
# ---------------------------------------------------------------------------
#
# The pure stacking rule (compute_multiplier) is covered in
# tests/tools/test_leveling_service.py; these tests pin the COG side of the
# seam: the snapshot (xp_multipliers rows + the level_config event columns) is
# loaded at most ONCE per guild (a genuine cache), a boost scales the grant, a
# 0x boost skips the write entirely (but still starts the cooldown), and
# refresh_multiplier_snapshot (the cross-cog hook
# cogs/community/level_config_ui.py calls after every boost/event write) makes
# a change visible on the very next message.


def _multiplier_fetch_calls(fake_pool):
    return [c for c in fake_pool.calls if c[0] == "fetch" and "xp_multipliers" in c[1]]


def _event_fetchrow_calls(fake_pool):
    return [
        c for c in fake_pool.calls if c[0] == "fetchrow" and "event_factor" in c[1]
    ]


async def test_multiplier_snapshot_is_loaded_once_then_cached(fake_pool):
    """A guild with no boosts/event configured (the default empty rows/row)
    still costs exactly one xp_multipliers fetch + one event fetchrow for its
    first message, and zero for later ones."""
    fake_pool.fetchval_return = 11000  # mid-band, no level-up noise
    _route_fetch(fake_pool)
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    await cog.on_message(_FakeMessage(content="one", guild_id=1, author_id=2))
    await cog.on_message(_FakeMessage(content="two", guild_id=1, author_id=3))

    assert len(_multiplier_fetch_calls(fake_pool)) == 1
    assert len(_event_fetchrow_calls(fake_pool)) == 1
    assert 1 in cog._multipliers
    assert cog._multipliers[1].is_trivial


async def test_multiplier_global_boost_scales_the_grant(fake_pool):
    fake_pool.fetchval_return = 11000
    _route_fetch(
        fake_pool,
        multiplier_rows=[
            {"kind": "global", "target_id": 0, "factor": 2.0}
        ],
    )
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=10, xp_max=10)  # deterministic +10 XP before boost

    await cog.on_message(_FakeMessage(content="hi", guild_id=1, author_id=2))

    (_method, _query, args), = _fetchval_calls(fake_pool)
    assert args[2] == 20  # 10 base x 2.0 global boost


async def test_multiplier_channel_boost_beats_no_boost_elsewhere(fake_pool):
    fake_pool.fetchval_return = 11000
    _route_fetch(
        fake_pool,
        multiplier_rows=[{"kind": "channel", "target_id": 100, "factor": 3.0}],
    )
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=10, xp_max=10)

    msg = _FakeMessage(content="hi", guild_id=1, author_id=2, channel_id=100)
    await cog.on_message(msg)

    (_method, _query, args), = _fetchval_calls(fake_pool)
    assert args[2] == 30  # 10 base x 3.0 channel boost


async def test_multiplier_role_boost_uses_the_highest_matching_role(fake_pool):
    fake_pool.fetchval_return = 11000
    _route_fetch(
        fake_pool,
        multiplier_rows=[
            {"kind": "role", "target_id": 7, "factor": 2.0},
            {"kind": "role", "target_id": 8, "factor": 4.0},
        ],
    )
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=10, xp_max=10)

    msg = _FakeMessage(
        content="hi", guild_id=1, author_id=2, role_ids=[7, 8]
    )
    await cog.on_message(msg)

    (_method, _query, args), = _fetchval_calls(fake_pool)
    assert args[2] == 40  # highest of the two matching roles (4.0), not a product


async def test_multiplier_zero_factor_skips_the_write_but_starts_the_cooldown(fake_pool):
    _route_fetch(
        fake_pool,
        multiplier_rows=[{"kind": "global", "target_id": 0, "factor": 0.0}],
    )
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=10, xp_max=10)

    msg = _FakeMessage(content="hi", guild_id=1, author_id=2)
    await cog.on_message(msg)

    assert _fetchval_calls(fake_pool) == []  # no INSERT INTO levels at all
    assert len(cog._cooldowns) == 1  # but the cooldown DID start


async def test_multiplier_trivial_snapshot_short_circuits_compute_multiplier(
    fake_pool, monkeypatch
):
    """HOT PATH allocation guard: a guild with no multiplier configuration at
    all must not even CALL compute_multiplier (so the role-id generator is
    never built) - mirrors the no-xp empty-snapshot short circuit."""
    calls = []
    real = leveling.compute_multiplier
    monkeypatch.setattr(
        leveling,
        "compute_multiplier",
        lambda *a, **k: (calls.append(a), real(*a, **k))[1],
    )
    fake_pool.fetchval_return = 11000
    _route_fetch(fake_pool)  # no boosts, no event
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    await cog.on_message(_FakeMessage(guild_id=1, author_id=2, role_ids=[7, 8]))

    assert calls == []
    assert len(_fetchval_calls(fake_pool)) == 1  # still earned XP normally


async def test_multiplier_nontrivial_snapshot_does_call_compute_multiplier(
    fake_pool, monkeypatch
):
    calls = []
    real = leveling.compute_multiplier
    monkeypatch.setattr(
        leveling,
        "compute_multiplier",
        lambda *a, **k: (calls.append(a), real(*a, **k))[1],
    )
    fake_pool.fetchval_return = 11000
    _route_fetch(
        fake_pool, multiplier_rows=[{"kind": "global", "target_id": 0, "factor": 1.5}]
    )
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1)

    await cog.on_message(_FakeMessage(guild_id=1, author_id=2))

    assert len(calls) == 1


async def test_refresh_multiplier_snapshot_reloads_from_the_db(fake_pool):
    """The cross-cog hook: after an xp_multipliers/event write, the caller
    re-reads the guild's rows and the NEW snapshot takes effect immediately,
    no restart."""
    fake_pool.fetchval_return = 11000
    _route_fetch(fake_pool)
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=10, xp_max=10)

    await cog.on_message(_FakeMessage(guild_id=1, author_id=2))
    assert 1 in cog._multipliers
    assert cog._multipliers[1].is_trivial

    # A global boost gets added; the config UI cog calls this after the write.
    _route_fetch(
        fake_pool, multiplier_rows=[{"kind": "global", "target_id": 0, "factor": 2.0}]
    )
    await cog.refresh_multiplier_snapshot(1)
    assert cog._multipliers[1].global_factor == 2.0

    # The NEXT message picks up the boost without any further fetch.
    fetches_before = len(_multiplier_fetch_calls(fake_pool))
    msg = _FakeMessage(guild_id=1, author_id=3)
    await cog.on_message(msg)
    assert len(_multiplier_fetch_calls(fake_pool)) == fetches_before  # cache hit
    (_method, _query, args), = [
        c
        for c in _fetchval_calls(fake_pool)
        if c[2][1] == 3  # the second author's grant
    ]
    assert args[2] == 20  # 10 base x 2.0 boost


async def test_expired_event_is_lazily_nulled_and_ignored(fake_pool):
    """An event whose stored end time has already passed is treated as absent
    AND lazily nulled in level_config (no background timer needed)."""
    past = discord.utils.utcnow() - datetime.timedelta(hours=1)
    fake_pool.fetchval_return = 11000
    fake_pool.fetchrow_return = {"event_factor": 2.0, "event_ends_at": past}
    _route_fetch(fake_pool)
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=10, xp_max=10)

    await cog.on_message(_FakeMessage(guild_id=1, author_id=2))

    assert cog._multipliers[1].event_factor is None
    assert cog._multipliers[1].event_ends_at is None
    (_method, _query, args), = _fetchval_calls(fake_pool)
    assert args[2] == 10  # unaffected by the expired event

    nulls = [
        c
        for c in fake_pool.calls
        if c[0] == "execute" and "event_factor = NULL" in c[1]
    ]
    assert len(nulls) == 1
    assert nulls[0][2] == (1,)


async def test_active_event_boosts_the_grant(fake_pool):
    future = discord.utils.utcnow() + datetime.timedelta(hours=1)
    fake_pool.fetchval_return = 11000
    fake_pool.fetchrow_return = {"event_factor": 2.0, "event_ends_at": future}
    _route_fetch(fake_pool)
    cog = Leveling(_make_bot(fake_pool))
    _enable(cog, 1, xp_min=10, xp_max=10)

    await cog.on_message(_FakeMessage(guild_id=1, author_id=2))

    (_method, _query, args), = _fetchval_calls(fake_pool)
    assert args[2] == 20  # 10 base x 2.0 active event
    nulls = [
        c
        for c in fake_pool.calls
        if c[0] == "execute" and "event_factor = NULL" in c[1]
    ]
    assert nulls == []  # a still-active event is never nulled

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

import types

from cogs.community.leveling import Leveling
from tools import leveling


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
    def __init__(self):
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


class _FakeMsgAuthor:
    def __init__(self, uid, is_bot=False):
        self.id = uid
        self.bot = is_bot
        self.mention = f"<@{uid}>"


class _FakeMessage:
    def __init__(self, content="hello", guild_id=1, author_id=2, is_bot=False):
        self.content = content
        self.guild = (
            types.SimpleNamespace(id=guild_id) if guild_id is not None else None
        )
        self.author = _FakeMsgAuthor(author_id, is_bot)
        self.channel = _FakeChannel()


def _make_bot(fake_pool, prefixes=None, default_prefix="?", bot_user_id=999):
    return types.SimpleNamespace(
        db_pool=fake_pool,
        prefixes=prefixes if prefixes is not None else {},
        default_prefix=default_prefix,
        user=types.SimpleNamespace(id=bot_user_id),
    )


def _enable(cog, guild_id=1, **overrides):
    """Arrange an enabled guild directly in the hot-path config map.

    Membership in cog._configs IS "leveling on for this guild"; overrides let a
    test pin a custom cooldown or xp band (e.g. xp_min=xp_max for a fixed gain).
    """
    cog._configs[guild_id] = leveling.LevelConfig(enabled=True, **overrides)


def _fetchval_calls(fake_pool):
    return [c for c in fake_pool.calls if c[0] == "fetchval"]


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

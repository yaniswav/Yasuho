"""Unit tests for cogs.community.level_config_ui.LevelConfigUI (leveling L3).

The pure decisions (is_no_xp_message, validate_announce_template,
render_announce_template, resolve_announce_target) are covered in
tests/tools/test_leveling_service.py; these tests drive the COG-level
application against fakes: the race-safe cap-guarded INSERT (mirrors
level_rewards_add's own precedent), the duplicate-vs-maximum disambiguation on
a null insert, the cross-cog refresh_no_xp_snapshot push after every write, the
"exactly one of channel/role" validation, and the announce mode/template
commands' delegation to the Leveling cog (never a direct level_config write).
"""

import datetime
import types

import discord

from cogs.community.level_config_ui import LevelConfigUI
from tools import leveling

# ---------------------------------------------------------------------------
# Fakes: guild / channel / role shaped just enough for mentions + is_default().
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self, channel_id, name="general"):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"


class _FakeRole:
    def __init__(self, role_id, name="Muted", default=False):
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"
        self._default = default

    def is_default(self):
        return self._default


class _FakeGuild:
    def __init__(self, guild_id=1, name="guild", channels=(), roles=()):
        self.id = guild_id
        self.name = name
        self._channels = {c.id: c for c in channels}
        self._roles = {r.id: r for r in roles}

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_role(self, rid):
        return self._roles.get(rid)


class _Ctx:
    def __init__(self, guild=None, author_id=1):
        self.guild = guild or _FakeGuild()
        self.author = types.SimpleNamespace(
            id=author_id, mention=f"<@{author_id}>"
        )
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


class _FakeLevelingCog:
    """Stand-in for cogs.community.leveling.Leveling's cross-cog surface."""

    def __init__(self):
        self.refresh_calls = []
        self.multiplier_refresh_calls = []
        self.set_announce_mode_calls = []
        self.set_announce_template_calls = []
        self.set_voice_xp_enabled_calls = []
        self.set_voice_xp_rate_calls = []
        self.enabled = False  # what is_enabled reports back

    async def refresh_no_xp_snapshot(self, guild_id):
        self.refresh_calls.append(guild_id)

    async def refresh_multiplier_snapshot(self, guild_id):
        self.multiplier_refresh_calls.append(guild_id)

    async def set_announce_mode(self, guild_id, mode, channel_id=None):
        self.set_announce_mode_calls.append((guild_id, mode, channel_id))

    async def set_announce_template(self, guild_id, template):
        self.set_announce_template_calls.append((guild_id, template))

    async def set_voice_xp_enabled(self, guild_id, enabled):
        self.set_voice_xp_enabled_calls.append((guild_id, enabled))

    async def set_voice_xp_rate(self, guild_id, rate):
        self.set_voice_xp_rate_calls.append((guild_id, rate))

    def is_enabled(self, guild_id):
        return self.enabled


def _make_bot(fake_pool, leveling_cog=None):
    return types.SimpleNamespace(
        db_pool=fake_pool,
        get_cog=lambda name: leveling_cog if name == "Leveling" else None,
    )


# ---------------------------------------------------------------------------
# noxp add: "exactly one of channel or role"
# ---------------------------------------------------------------------------


async def test_noxp_add_rejects_neither_channel_nor_role(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    await cog.levelconfig_noxp_add.callback(cog, ctx, None, None)
    assert any("exactly one" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


async def test_noxp_add_rejects_both_channel_and_role(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    channel = _FakeChannel(10)
    role = _FakeRole(20)
    ctx = _Ctx()
    await cog.levelconfig_noxp_add.callback(cog, ctx, channel, role)
    assert any("exactly one" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


async def test_noxp_add_rejects_everyone_role(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    everyone = _FakeRole(1, name="@everyone", default=True)
    ctx = _Ctx()
    await cog.levelconfig_noxp_add.callback(cog, ctx, None, everyone)
    assert any("everyone" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# noxp add: the race-safe cap guard (mirrors level_rewards_add's precedent)
# ---------------------------------------------------------------------------


def _route_noxp_add(fake_pool, count=0, inserted="channel", exists=None):
    async def fetchval(query, *args):
        fake_pool.calls.append(("fetchval", query, args))
        if "INSERT INTO level_no_xp" in query:
            return inserted
        if query.lstrip().startswith("SELECT COUNT"):
            return count
        if query.lstrip().startswith("SELECT 1"):
            return exists
        return None

    fake_pool.fetchval = fetchval


async def test_noxp_add_insert_carries_the_atomic_cap_guard(fake_pool):
    channel = _FakeChannel(10)
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    _route_noxp_add(fake_pool, count=0, inserted="channel")

    await cog.levelconfig_noxp_add.callback(cog, ctx, channel, None)

    inserts = [c for c in fake_pool.calls if "INSERT INTO level_no_xp" in c[1]]
    assert len(inserts) == 1
    _method, query, args = inserts[0]
    assert "WHERE (SELECT COUNT(*) FROM level_no_xp WHERE guild_id = $1) < $4" in (
        " ".join(query.split())
    )
    assert args == (1, leveling.NO_XP_CHANNEL, 10, leveling.MAX_NO_XP_PER_GUILD)
    # The cross-cog seam fires exactly once, for this guild.
    assert leveling_cog.refresh_calls == [1]
    assert any("added" in c[1]["embed"].title.lower() for c in ctx.sends)


async def test_noxp_add_role_uses_the_role_kind(fake_pool):
    role = _FakeRole(77)
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    _route_noxp_add(fake_pool, count=0, inserted="role")

    await cog.levelconfig_noxp_add.callback(cog, ctx, None, role)

    inserts = [c for c in fake_pool.calls if "INSERT INTO level_no_xp" in c[1]]
    _method, _query, args = inserts[0]
    assert args[1] == leveling.NO_XP_ROLE
    assert args[2] == 77


async def test_noxp_add_pre_check_refuses_at_the_cap(fake_pool):
    channel = _FakeChannel(10)
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    _route_noxp_add(fake_pool, count=leveling.MAX_NO_XP_PER_GUILD)

    await cog.levelconfig_noxp_add.callback(cog, ctx, channel, None)

    assert any("maximum" in c[0][0] for c in ctx.sends)
    inserts = [c for c in fake_pool.calls if "INSERT INTO level_no_xp" in c[1]]
    assert inserts == []  # the pre-check short-circuits before the INSERT


async def test_noxp_add_null_insert_with_existing_entry_reports_duplicate(fake_pool):
    channel = _FakeChannel(10)
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    _route_noxp_add(fake_pool, count=1, inserted=None, exists=1)

    await cog.levelconfig_noxp_add.callback(cog, ctx, channel, None)

    assert any("already a no-XP zone" in c[0][0] for c in ctx.sends)


async def test_noxp_add_null_insert_from_a_lost_cap_race_reports_maximum(fake_pool):
    channel = _FakeChannel(10)
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    # Pre-check saw room, but the atomic INSERT still added nothing and the
    # row does not exist -> a concurrent add filled the last slot.
    _route_noxp_add(fake_pool, count=0, inserted=None, exists=None)

    await cog.levelconfig_noxp_add.callback(cog, ctx, channel, None)

    assert any("maximum" in c[0][0] for c in ctx.sends)


async def test_noxp_add_tolerates_a_missing_leveling_cog(fake_pool):
    """The cache push is best-effort - a missing Leveling cog never blocks the
    write itself (only the announce commands hard-refuse without it)."""
    channel = _FakeChannel(10)
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog=None))
    ctx = _Ctx()
    _route_noxp_add(fake_pool, count=0, inserted="channel")

    await cog.levelconfig_noxp_add.callback(cog, ctx, channel, None)

    assert len(ctx.sends) == 1  # succeeded, no crash


# ---------------------------------------------------------------------------
# noxp remove
# ---------------------------------------------------------------------------


async def test_noxp_remove_with_no_entries_configured(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    await cog.levelconfig_noxp_remove.callback(cog, ctx)
    assert any("no no-XP zones" in c[0][0] for c in ctx.sends)


async def test_noxp_remove_opens_a_picker_when_entries_exist(fake_pool):
    fake_pool.fetch_return = [{"kind": "channel", "target_id": 10}]
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    await cog.levelconfig_noxp_remove.callback(cog, ctx)
    assert len(ctx.sends) == 1
    _args, kwargs = ctx.sends[0]
    assert "view" in kwargs


async def test_noxp_remove_select_deletes_and_refreshes_cache(fake_pool, make_interaction):
    from cogs.community.level_config_ui import _RemoveNoXpSelect

    channel = _FakeChannel(10)
    guild = _FakeGuild(guild_id=1, channels=[channel])
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))

    select = _RemoveNoXpSelect(cog, guild, [(leveling.NO_XP_CHANNEL, 10)])
    select._values = [f"{leveling.NO_XP_CHANNEL}:10"]
    select._owner = types.SimpleNamespace(stop=lambda: None)

    interaction = make_interaction()
    await select.callback(interaction)

    deletes = [c for c in fake_pool.calls if c[0] == "execute"]
    assert len(deletes) == 1
    _method, query, args = deletes[0]
    assert "DELETE FROM level_no_xp" in query
    assert args == (1, leveling.NO_XP_CHANNEL, 10)
    assert leveling_cog.refresh_calls == [1]
    assert len(interaction.edits) == 1


# ---------------------------------------------------------------------------
# noxp list (just proves it renders without error, for both empty and
# populated cases - the content assembly is exercised, not asserted line by
# line, since the CV2 layout is presentational).
# ---------------------------------------------------------------------------


async def test_noxp_list_empty_does_not_crash(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    await cog.levelconfig_noxp_list.callback(cog, ctx)
    assert len(ctx.sends) == 1


async def test_noxp_list_with_entries_does_not_crash(fake_pool):
    channel = _FakeChannel(10)
    role = _FakeRole(20)
    fake_pool.fetch_return = [
        {"kind": "channel", "target_id": 10},
        {"kind": "role", "target_id": 20},
    ]
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx(guild=_FakeGuild(channels=[channel], roles=[role]))
    await cog.levelconfig_noxp_list.callback(cog, ctx)
    assert len(ctx.sends) == 1


# ---------------------------------------------------------------------------
# announce mode: delegates to the Leveling cog, never writes level_config
# directly.
# ---------------------------------------------------------------------------


async def test_announce_mode_off_needs_no_channel(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))

    await cog.levelconfig_announce_mode.callback(cog, ctx, "off", None)

    assert leveling_cog.set_announce_mode_calls == [(1, "off", None)]
    assert fake_pool.calls == []  # this cog never writes level_config itself


async def test_announce_mode_fixed_requires_a_channel(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()

    await cog.levelconfig_announce_mode.callback(cog, ctx, "fixed", None)

    assert leveling_cog.set_announce_mode_calls == []
    assert any("Give a channel" in c[0][0] for c in ctx.sends)


async def test_announce_mode_fixed_with_channel_passes_its_id(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=7))
    channel = _FakeChannel(555)

    await cog.levelconfig_announce_mode.callback(cog, ctx, "fixed", channel)

    assert leveling_cog.set_announce_mode_calls == [(7, "fixed", 555)]


async def test_announce_mode_channel_ignores_a_stray_channel_argument(fake_pool):
    """Non-fixed modes never persist a channel id, even if one was somehow
    passed (the slash command only exposes it for the fixed branch)."""
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    channel = _FakeChannel(555)

    await cog.levelconfig_announce_mode.callback(cog, ctx, "channel", channel)

    assert leveling_cog.set_announce_mode_calls == [(1, "channel", None)]


async def test_announce_mode_without_the_leveling_cog_refuses(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog=None))
    ctx = _Ctx()
    await cog.levelconfig_announce_mode.callback(cog, ctx, "off", None)
    assert any("isn't loaded" in c[0][0] for c in ctx.sends)


# ---------------------------------------------------------------------------
# announce template: validated at SET time, delegates to the Leveling cog.
# ---------------------------------------------------------------------------


async def test_announce_template_none_resets_it(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))

    await cog.levelconfig_announce_template.callback(cog, ctx, None)

    assert leveling_cog.set_announce_template_calls == [(1, None)]
    assert any("reset" in c[0][0] for c in ctx.sends)


async def test_announce_template_literal_reset_keyword_resets_it(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))

    await cog.levelconfig_announce_template.callback(cog, ctx, "  Reset  ")

    assert leveling_cog.set_announce_template_calls == [(1, None)]


async def test_announce_template_rejects_unknown_placeholder(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()

    await cog.levelconfig_announce_template.callback(
        cog, ctx, "{user} did {something}"
    )

    assert leveling_cog.set_announce_template_calls == []
    assert any("placeholders" in c[0][0] for c in ctx.sends)


async def test_announce_template_rejects_a_format_spec(fake_pool):
    """A format-spec abuse ("{level:>9999999}") is refused at SET time, so it
    never reaches the DB nor the render path - the cog surfaces the same
    placeholder error and delegates nothing."""
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()

    await cog.levelconfig_announce_template.callback(cog, ctx, "{level:>9999999}")

    assert leveling_cog.set_announce_template_calls == []
    assert any("placeholders" in c[0][0] for c in ctx.sends)


async def test_announce_template_rejects_a_conversion(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()

    await cog.levelconfig_announce_template.callback(cog, ctx, "{user!r}")

    assert leveling_cog.set_announce_template_calls == []
    assert any("placeholders" in c[0][0] for c in ctx.sends)


async def test_announce_template_rejects_empty(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()

    await cog.levelconfig_announce_template.callback(cog, ctx, "   ")

    assert leveling_cog.set_announce_template_calls == []
    assert any("empty" in c[0][0] for c in ctx.sends)


async def test_announce_template_sets_a_valid_custom_template_and_previews(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1, name="Test Guild"), author_id=42)

    await cog.levelconfig_announce_template.callback(
        cog, ctx, "gg {user}, level {level} in {guild}"
    )

    assert leveling_cog.set_announce_template_calls == [
        (1, "gg {user}, level {level} in {guild}")
    ]
    embed = ctx.sends[0][1]["embed"]
    assert isinstance(embed, discord.Embed)
    assert "<@42>" in embed.description
    assert "Test Guild" in embed.description


async def test_announce_template_without_the_leveling_cog_refuses(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog=None))
    ctx = _Ctx()
    await cog.levelconfig_announce_template.callback(cog, ctx, "gg {user}")
    assert any("isn't loaded" in c[0][0] for c in ctx.sends)


# ---------------------------------------------------------------------------
# overview (`/levelconfig` bare)
# ---------------------------------------------------------------------------


async def test_overview_does_not_crash_with_no_config_row(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    ctx.invoked_subcommand = None
    await cog.levelconfig.callback(cog, ctx)
    assert len(ctx.sends) == 1


# ---------------------------------------------------------------------------
# voicexp on/off/rate: delegation to the Leveling cog + rate validation.
# The cog never writes voice_xp columns directly - it always routes through
# Leveling.set_voice_xp_* so the hot-path config cache the VoiceXP sweep reads
# stays in step (mirrors the announce commands' delegation).
# ---------------------------------------------------------------------------


def _embed_descriptions(ctx):
    return [
        kwargs["embed"].description
        for _args, kwargs in ctx.sends
        if "embed" in kwargs
    ]


async def test_voicexp_on_delegates_and_confirms(fake_pool):
    leveling_cog = _FakeLevelingCog()
    leveling_cog.enabled = True  # server leveling is on -> no nudge
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    await cog.levelconfig_voicexp_on.callback(cog, ctx)
    assert leveling_cog.set_voice_xp_enabled_calls == [(1, True)]
    descs = _embed_descriptions(ctx)
    assert descs and all("leveling is off" not in d for d in descs)


async def test_voicexp_on_nudges_when_server_leveling_is_off(fake_pool):
    leveling_cog = _FakeLevelingCog()
    leveling_cog.enabled = False  # leveling off -> voice XP grants nothing yet
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    await cog.levelconfig_voicexp_on.callback(cog, ctx)
    assert leveling_cog.set_voice_xp_enabled_calls == [(1, True)]
    assert any("leveling is off" in d for d in _embed_descriptions(ctx))


async def test_voicexp_off_delegates(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    await cog.levelconfig_voicexp_off.callback(cog, ctx)
    assert leveling_cog.set_voice_xp_enabled_calls == [(1, False)]


async def test_voicexp_on_without_leveling_cog_is_a_friendly_refusal(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog=None))
    ctx = _Ctx()
    await cog.levelconfig_voicexp_on.callback(cog, ctx)
    assert any("isn't loaded" in c[0][0] for c in ctx.sends if c[0])


async def test_voicexp_rate_valid_delegates(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    await cog.levelconfig_voicexp_rate.callback(cog, ctx, 10)
    assert leveling_cog.set_voice_xp_rate_calls == [(1, 10)]


async def test_voicexp_rate_out_of_range_is_refused_without_a_write(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    for bad in (0, 61, -5):
        ctx = _Ctx()
        await cog.levelconfig_voicexp_rate.callback(cog, ctx, bad)
        assert leveling_cog.set_voice_xp_rate_calls == []  # never written
        assert any("between" in c[0][0] for c in ctx.sends if c[0]), bad


async def test_voicexp_rate_accepts_the_bounds(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    for good in (1, 60):
        await cog.levelconfig_voicexp_rate.callback(cog, _Ctx(), good)
    assert leveling_cog.set_voice_xp_rate_calls == [(1, 1), (1, 60)]


# ---------------------------------------------------------------------------
# XP boosts (L4): /levelconfig boost add/remove/list.
# ---------------------------------------------------------------------------


def _route_boost_add(fake_pool, inserted="global"):
    async def fetchval(query, *args):
        fake_pool.calls.append(("fetchval", query, args))
        if "INSERT INTO xp_multipliers" in query:
            return inserted
        return None

    fake_pool.fetchval = fetchval


async def test_boost_add_rejects_both_channel_and_role(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    channel = _FakeChannel(10)
    role = _FakeRole(20)
    ctx = _Ctx()
    await cog.levelconfig_boost_add.callback(cog, ctx, 2.0, channel, role)
    assert any("at most one" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


async def test_boost_add_rejects_everyone_role(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    everyone = _FakeRole(1, name="@everyone", default=True)
    ctx = _Ctx()
    await cog.levelconfig_boost_add.callback(cog, ctx, 2.0, None, everyone)
    assert any("everyone" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


async def test_boost_add_rejects_out_of_range_factor(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    await cog.levelconfig_boost_add.callback(cog, ctx, 5.1, None, None)
    assert any("between" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


async def test_boost_add_accepts_zero_factor(fake_pool):
    """0.0 is a valid, explicitly supported 'mute XP' factor - never refused."""
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    _route_boost_add(fake_pool, inserted="global")

    await cog.levelconfig_boost_add.callback(cog, ctx, 0.0, None, None)

    assert leveling_cog.multiplier_refresh_calls == [1]
    assert any("boost set" in c[1]["embed"].title.lower() for c in ctx.sends)


async def test_boost_add_neither_channel_nor_role_is_a_global_boost(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    _route_boost_add(fake_pool, inserted="global")

    await cog.levelconfig_boost_add.callback(cog, ctx, 2.0, None, None)

    inserts = [c for c in fake_pool.calls if "INSERT INTO xp_multipliers" in c[1]]
    _method, query, args = inserts[0]
    assert args[1] == leveling.MULTIPLIER_GLOBAL
    assert args[2] == leveling.GLOBAL_MULTIPLIER_TARGET_ID
    assert args[3] == 2.0
    assert leveling_cog.multiplier_refresh_calls == [1]


async def test_boost_add_channel_uses_the_channel_kind(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    channel = _FakeChannel(10)
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    _route_boost_add(fake_pool, inserted="channel")

    await cog.levelconfig_boost_add.callback(cog, ctx, 3.0, channel, None)

    inserts = [c for c in fake_pool.calls if "INSERT INTO xp_multipliers" in c[1]]
    _method, _query, args = inserts[0]
    assert args[1] == leveling.MULTIPLIER_CHANNEL
    assert args[2] == 10


async def test_boost_add_role_uses_the_role_kind(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    role = _FakeRole(77)
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    _route_boost_add(fake_pool, inserted="role")

    await cog.levelconfig_boost_add.callback(cog, ctx, 1.5, None, role)

    inserts = [c for c in fake_pool.calls if "INSERT INTO xp_multipliers" in c[1]]
    _method, _query, args = inserts[0]
    assert args[1] == leveling.MULTIPLIER_ROLE
    assert args[2] == 77


async def test_boost_add_insert_carries_the_race_safe_cap_and_update_guard(fake_pool):
    """The atomic INSERT allows a room-available OR already-existing target,
    and upserts the factor on conflict - so re-adding an existing boost edits
    it rather than erroring."""
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    _route_boost_add(fake_pool, inserted="global")

    await cog.levelconfig_boost_add.callback(cog, ctx, 2.0, None, None)

    inserts = [c for c in fake_pool.calls if "INSERT INTO xp_multipliers" in c[1]]
    _method, query, args = inserts[0]
    flat = " ".join(query.split())
    assert "WHERE (SELECT COUNT(*) FROM xp_multipliers WHERE guild_id = $1) < $5" in flat
    assert "OR EXISTS" in flat
    assert "DO UPDATE SET factor = EXCLUDED.factor" in flat
    assert args[4] == leveling.MAX_MULTIPLIERS_PER_GUILD


async def test_boost_add_null_insert_reports_the_maximum(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    _route_boost_add(fake_pool, inserted=None)  # blocked: new target, at cap

    await cog.levelconfig_boost_add.callback(cog, ctx, 2.0, None, None)

    assert any("maximum" in c[0][0] for c in ctx.sends)
    assert leveling_cog.multiplier_refresh_calls == []  # never refreshed


async def test_boost_remove_with_no_boosts_configured(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    await cog.levelconfig_boost_remove.callback(cog, ctx)
    assert any("no XP boosts" in c[0][0] for c in ctx.sends)


async def test_boost_remove_opens_a_picker_when_boosts_exist(fake_pool):
    fake_pool.fetch_return = [{"kind": "global", "target_id": 0, "factor": 2.0}]
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    await cog.levelconfig_boost_remove.callback(cog, ctx)
    assert len(ctx.sends) == 1
    _args, kwargs = ctx.sends[0]
    assert "view" in kwargs


async def test_boost_remove_select_deletes_and_refreshes_cache(fake_pool, make_interaction):
    from cogs.community.level_config_ui import _RemoveMultiplierSelect

    guild = _FakeGuild(guild_id=1)
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))

    select = _RemoveMultiplierSelect(
        cog, guild, [(leveling.MULTIPLIER_GLOBAL, leveling.GLOBAL_MULTIPLIER_TARGET_ID, 2.0)]
    )
    select._values = [f"{leveling.MULTIPLIER_GLOBAL}:{leveling.GLOBAL_MULTIPLIER_TARGET_ID}"]
    select._owner = types.SimpleNamespace(stop=lambda: None)

    interaction = make_interaction()
    await select.callback(interaction)

    deletes = [c for c in fake_pool.calls if c[0] == "execute"]
    assert len(deletes) == 1
    _method, query, args = deletes[0]
    assert "DELETE FROM xp_multipliers" in query
    assert args == (1, leveling.MULTIPLIER_GLOBAL, leveling.GLOBAL_MULTIPLIER_TARGET_ID)
    assert leveling_cog.multiplier_refresh_calls == [1]
    assert len(interaction.edits) == 1


async def test_boost_list_empty_does_not_crash(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    await cog.levelconfig_boost_list.callback(cog, ctx)
    assert len(ctx.sends) == 1


async def test_boost_list_with_entries_does_not_crash(fake_pool):
    channel = _FakeChannel(10)
    role = _FakeRole(20)
    fake_pool.fetch_return = [
        {"kind": "global", "target_id": 0, "factor": 2.0},
        {"kind": "channel", "target_id": 10, "factor": 1.5},
        {"kind": "role", "target_id": 20, "factor": 3.0},
    ]
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx(guild=_FakeGuild(channels=[channel], roles=[role]))
    await cog.levelconfig_boost_list.callback(cog, ctx)
    assert len(ctx.sends) == 1


# ---------------------------------------------------------------------------
# XP event (L4): /levelconfig event set/off.
# ---------------------------------------------------------------------------


async def test_event_set_rejects_out_of_range_factor(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    await cog.levelconfig_event_set.callback(cog, ctx, 5.1, "2h")
    assert any("between" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []
    assert leveling_cog.multiplier_refresh_calls == []


async def test_event_set_rejects_a_malformed_duration(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    await cog.levelconfig_event_set.callback(cog, ctx, 2.0, "not a duration")
    assert any("couldn't understand" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


async def test_event_set_rejects_a_too_long_duration(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    await cog.levelconfig_event_set.callback(cog, ctx, 2.0, "40d")  # > 14 days
    assert any("between" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


async def test_event_set_writes_the_upsert_and_refreshes_the_cache(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=7))

    await cog.levelconfig_event_set.callback(cog, ctx, 2.0, "2h")

    upserts = [c for c in fake_pool.calls if c[0] == "execute"]
    assert len(upserts) == 1
    _method, query, args = upserts[0]
    assert "INSERT INTO level_config" in query
    assert "COALESCE" in query  # enabled seeded from legacy JSONB, never clobbered
    assert "guild_settings" in query
    assert args[0] == 7
    assert args[1] == 2.0
    assert isinstance(args[2], datetime.datetime)
    assert leveling_cog.multiplier_refresh_calls == [7]
    assert any("event started" in c[1]["embed"].title.lower() for c in ctx.sends)


async def test_event_off_nulls_the_columns_and_refreshes_the_cache(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))

    await cog.levelconfig_event_off.callback(cog, ctx)

    upserts = [c for c in fake_pool.calls if c[0] == "execute"]
    assert len(upserts) == 1
    _method, _query, args = upserts[0]
    assert args == (1, None, None)
    assert leveling_cog.multiplier_refresh_calls == [1]
    assert any("stopped" in c[0][0] for c in ctx.sends)


async def test_event_status_shows_no_event_by_default(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool))
    ctx = _Ctx()
    ctx.invoked_subcommand = None
    await cog.levelconfig_event.callback(cog, ctx)
    embed = ctx.sends[0][1]["embed"]
    assert "No XP event running" in embed.description

"""Unit tests for cogs.community.leveling.level_config_ui.LevelConfigUI (leveling L3).

The pure decisions (is_no_xp_message, validate_announce_template,
render_announce_template, resolve_announce_target) are covered in
tests/cogs/test_leveling_engine.py; these tests drive the COG-level
application against fakes: the race-safe cap-guarded INSERT (mirrors
level_rewards_add's own precedent), the duplicate-vs-maximum disambiguation on
a null insert, the cross-cog refresh_no_xp_snapshot push after every write, the
"exactly one of channel/role" validation, and the announce mode/template
commands' delegation to the Leveling cog (never a direct level_config write).
"""

import datetime
import io
import types

import discord
import pytest

import cogs.community.leveling.level_config_ui as level_config_ui_module
from cogs.community.leveling import engine as leveling
from cogs.community.leveling import rank_card
from cogs.community.leveling.level_config_ui import LevelConfigUI, RankCardPanel
from tools import i18n


@pytest.fixture(autouse=True)
def _clear_preview_debounce():
    """The preview throttle is module state (one map for the process), so a
    click in one test would otherwise cool down the next one's."""
    level_config_ui_module._PREVIEW_DEBOUNCE._seen.clear()
    yield
    level_config_ui_module._PREVIEW_DEBOUNCE._seen.clear()


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
        self.deferred = False
        self.typing_depth = 0  # how deep we are inside `async with ctx.typing()`
        self.typed = False  # whether it was entered at all

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))

    async def defer(self, *args, **kwargs):
        self.deferred = True

    def typing(self, **kwargs):
        """The real Context.typing(): a typing indicator on the PREFIX path
        (where defer() is a no-op) and an already-answered defer on the slash
        one. Recorded so a test can assert the slow section runs inside it."""
        ctx = self

        class _Typing:
            async def __aenter__(self):
                ctx.typing_depth += 1
                ctx.typed = True

            async def __aexit__(self, *exc):
                ctx.typing_depth -= 1

        return _Typing()


class _FakeLevelingCog:
    """Stand-in for cogs.community.leveling.leveling.Leveling's cross-cog surface."""

    def __init__(self):
        self.refresh_calls = []
        self.multiplier_refresh_calls = []
        self.set_announce_mode_calls = []
        self.set_announce_template_calls = []
        self.set_voice_xp_enabled_calls = []
        self.set_voice_xp_rate_calls = []
        self.enabled = False  # what is_enabled reports back
        # RC2 seam: set_rank_background / set_rank_accent / clear_rank_card.
        self.set_rank_background_calls = []
        self.set_rank_background_error = None
        self.set_rank_accent_calls = []
        self.set_rank_accent_error = None
        self.clear_rank_card_calls = []
        self.rank_card_style = (None, False)  # (accent_rgb | None, has_background)

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

    async def set_rank_background(self, guild_id, data, content_type=None):
        self.set_rank_background_calls.append((guild_id, data, content_type))
        if self.set_rank_background_error is not None:
            raise self.set_rank_background_error
        return rank_card.STORED_FORMAT

    async def set_rank_accent(self, guild_id, value):
        self.set_rank_accent_calls.append((guild_id, value))
        if self.set_rank_accent_error is not None:
            raise self.set_rank_accent_error
        # Real validation, like the production seam - a bad hex must still
        # raise InvalidAccent from this fake, so panel/command tests exercise
        # the actual error-mapping path rather than a fake that never fails.
        return rank_card.validate_accent(value)

    async def clear_rank_card(self, guild_id, *, target=None):
        self.clear_rank_card_calls.append((guild_id, target))

    async def ensure_rank_card_style(self, guild_id):
        return self.rank_card_style

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
    from cogs.community.leveling.level_config_ui import _RemoveNoXpSelect

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
    from cogs.community.leveling.level_config_ui import _RemoveMultiplierSelect

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


# ---------------------------------------------------------------------------
# Rank card (RC2): /levelconfig card + /levelconfig card background
# ---------------------------------------------------------------------------


class _FakeAttachment:
    """Stand-in for discord.Attachment: the two metadata fields the command
    reads before ever downloading, plus a recording/failing ``read()``."""

    def __init__(
        self,
        size=100,
        content_type="image/png",
        data=b"pngdata",
        read_error=None,
        watch=None,
    ):
        self.size = size
        self.content_type = content_type
        self._data = data
        self._read_error = read_error
        self.read_calls = 0
        # An optional ctx to sample as the download starts, so a test can prove
        # the slow work really runs inside `async with ctx.typing()`.
        self._watch = watch
        self.typing_depth_on_read = None

    async def read(self):
        self.read_calls += 1
        if self._watch is not None:
            self.typing_depth_on_read = self._watch.typing_depth
        if self._read_error is not None:
            raise self._read_error
        return self._data


async def test_card_group_tolerates_a_missing_leveling_cog(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog=None))
    ctx = _Ctx()
    ctx.invoked_subcommand = None

    await cog.levelconfig_card.callback(cog, ctx)

    assert any("isn't loaded" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


async def test_card_panel_subcommand_opens_the_panel(fake_pool):
    """Discord cannot invoke a subcommand GROUP, so /levelconfig card panel is
    the panel's only slash form - it must open the same view the prefix group
    body does."""
    cog = LevelConfigUI(_make_bot(fake_pool, _FakeLevelingCog()))
    ctx = _Ctx(guild=_FakeGuild(guild_id=7))
    fake_pool.fetchrow_return = None

    await cog.levelconfig_card_panel.callback(cog, ctx)

    assert isinstance(ctx.sends[0][1]["view"], RankCardPanel)


async def test_card_background_tolerates_a_missing_leveling_cog(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog=None))
    ctx = _Ctx()
    attachment = _FakeAttachment()

    await cog.levelconfig_card_background.callback(cog, ctx, attachment)

    assert any("isn't loaded" in c[0][0] for c in ctx.sends)
    assert attachment.read_calls == 0
    assert ctx.deferred is False


async def test_card_background_rejects_an_oversized_attachment_without_downloading(
    fake_pool,
):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    attachment = _FakeAttachment(size=rank_card.MAX_SOURCE_BYTES + 1)

    await cog.levelconfig_card_background.callback(cog, ctx, attachment)

    assert any("too large" in c[0][0] for c in ctx.sends)
    assert attachment.read_calls == 0
    assert ctx.deferred is False  # refused before paying for the round-trip
    assert leveling_cog.set_rank_background_calls == []


async def test_card_background_reports_a_download_failure(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    # A bare HTTPException.__new__ (no real response/message) is enough to
    # exercise the except clause without constructing a real HTTP failure.
    attachment = _FakeAttachment(
        read_error=discord.HTTPException.__new__(discord.HTTPException)
    )

    await cog.levelconfig_card_background.callback(cog, ctx, attachment)

    assert ctx.deferred is True  # slow work was deferred before the download
    assert any("couldn't download" in c[0][0] for c in ctx.sends)
    assert leveling_cog.set_rank_background_calls == []


async def test_card_background_reports_a_non_discord_download_failure(fake_pool):
    """Attachment.read goes over the CDN through aiohttp, whose transport
    failures are NOT discord exceptions - they must still be reported, not
    escape as an unknown crash."""
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx()
    attachment = _FakeAttachment(read_error=OSError("connection reset"))

    await cog.levelconfig_card_background.callback(cog, ctx, attachment)

    assert any("couldn't download" in c[0][0] for c in ctx.sends)
    assert leveling_cog.set_rank_background_calls == []


def test_card_background_is_cooldown_guarded():
    """The one admin command that can queue an arbitrary decode into the
    BOT-WIDE 2-slot image semaphore must not be spammable."""
    buckets = LevelConfigUI.levelconfig_card_background._buckets
    assert buckets is not None and buckets._cooldown is not None
    assert buckets._cooldown.per == 10.0


async def test_card_background_runs_the_slow_work_inside_ctx_typing(fake_pool):
    """ctx.defer() is a no-op on the PREFIX path, so without ctx.typing() a
    ?levelconfig card background sits silent for the whole download + decode."""
    cog = LevelConfigUI(_make_bot(fake_pool, _FakeLevelingCog()))
    ctx = _Ctx(guild=_FakeGuild(guild_id=7))
    attachment = _FakeAttachment(watch=ctx)

    await cog.levelconfig_card_background.callback(cog, ctx, attachment)

    assert attachment.typing_depth_on_read == 1  # the download ran inside it
    assert ctx.typing_depth == 0  # and the block was exited


def test_card_background_help_quotes_the_enforced_cap():
    """The '8 MB' an admin reads in the slash help is DERIVED from
    rank_card.MAX_SOURCE_BYTES, so raising the cap can never leave the help
    quoting the old number."""
    assert level_config_ui_module.MAX_SOURCE_MB == (
        rank_card.MAX_SOURCE_BYTES // (1024 * 1024)
    )
    described = str(
        LevelConfigUI.levelconfig_card_background.app_command._params[
            "background"
        ].description
    )
    assert f"max {level_config_ui_module.MAX_SOURCE_MB} MB" in described


async def test_card_background_maps_a_typed_rank_card_error(fake_pool):
    leveling_cog = _FakeLevelingCog()
    leveling_cog.set_rank_background_error = rank_card.UnsupportedFormat("bmp")
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=7))
    attachment = _FakeAttachment()

    await cog.levelconfig_card_background.callback(cog, ctx, attachment)

    assert any(
        "PNG, JPEG, and WebP" in c[0][0] for c in ctx.sends
    )  # the mapped UnsupportedFormat message
    assert leveling_cog.set_rank_background_calls == [(7, b"pngdata", "image/png")]


async def test_card_background_success_defers_and_confirms(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=7))
    attachment = _FakeAttachment(data=b"real-png-bytes", content_type="image/png")

    await cog.levelconfig_card_background.callback(cog, ctx, attachment)

    assert ctx.deferred is True
    assert leveling_cog.set_rank_background_calls == [
        (7, b"real-png-bytes", "image/png")
    ]
    assert any(
        "background updated" in c[1]["embed"].title.lower() for c in ctx.sends
    )


# -- _send_card_panel: initial state from rank_cards ------------------------


async def test_send_card_panel_state_with_no_row_configured(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=7))
    fake_pool.fetchrow_return = None

    await cog._send_card_panel(ctx)

    view = ctx.sends[0][1]["view"]
    assert isinstance(view, RankCardPanel)
    assert view.state == {"accent": None, "has_background": False}


async def test_send_card_panel_state_with_a_configured_row(fake_pool):
    leveling_cog = _FakeLevelingCog()
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog))
    ctx = _Ctx(guild=_FakeGuild(guild_id=7))
    fake_pool.fetchrow_return = {
        "accent": 0x5865F2,
        "background_format": "webp",
        "has_background": True,
    }

    await cog._send_card_panel(ctx)

    view = ctx.sends[0][1]["view"]
    assert view.state == {"accent": 0x5865F2, "has_background": True}


async def test_send_card_panel_tolerates_a_missing_leveling_cog(fake_pool):
    cog = LevelConfigUI(_make_bot(fake_pool, leveling_cog=None))
    ctx = _Ctx()

    await cog._send_card_panel(ctx)

    assert any("isn't loaded" in c[0][0] for c in ctx.sends)
    assert fake_pool.calls == []


# ---------------------------------------------------------------------------
# RankCardPanel: layout, writes, preview.
# ---------------------------------------------------------------------------


def _panel(
    leveling_cog=None, guild=None, accent=None, has_background=False, pool=None
):
    return RankCardPanel(
        _make_bot(pool, leveling_cog),
        leveling_cog or _FakeLevelingCog(),
        guild or _FakeGuild(guild_id=7),
        1,
        {"accent": accent, "has_background": has_background},
    )


def test_card_panel_state_of_an_absent_row_is_the_stock_card():
    assert level_config_ui_module.card_panel_state(None) == {
        "accent": None,
        "has_background": False,
    }


def test_card_panel_state_reads_both_knobs_off_a_row():
    row = {"accent": 0x5865F2, "background_format": "webp", "has_background": True}
    assert level_config_ui_module.card_panel_state(row) == {
        "accent": 0x5865F2,
        "has_background": True,
    }


async def test_panel_build_reflects_no_customisation():
    panel = _panel()
    texts = [
        child.content
        for child in panel.walk_children()
        if isinstance(child, discord.ui.TextDisplay)
    ]
    joined = "\n".join(texts)
    assert "Not set" in joined
    assert "Default" in joined


async def test_panel_build_reflects_configured_background_and_accent():
    panel = _panel(accent=0x00FF00, has_background=True)
    texts = [
        child.content
        for child in panel.walk_children()
        if isinstance(child, discord.ui.TextDisplay)
    ]
    joined = "\n".join(texts)
    assert "880" in joined and "240" in joined
    assert "#00FF00" in joined
    container = panel.children[0]
    assert container.accent_colour == discord.Colour(0x00FF00)


async def test_show_background_instructions_replies_ephemeral(
    fake_pool, make_interaction
):
    panel = _panel(pool=fake_pool)
    interaction = make_interaction()
    # The panel's bound message must be a real stand-in with an awaitable
    # edit(): the hint answers the interaction first, so the refresh that
    # follows can only land through message.edit (refresh_layout's fallback).
    panel.message = interaction.message

    await panel.show_background_instructions(interaction)

    assert interaction.sent
    args, kwargs = interaction.sent[0]
    assert kwargs.get("ephemeral") is True
    assert "/levelconfig card background" in args[0]


async def test_show_background_instructions_refreshes_a_stale_panel(
    fake_pool, make_interaction
):
    """The upload lands through a SEPARATE command, so the panel can be stale
    by the time the admin clicks again - re-reading here is what stops "Reset
    background" being left disabled over a background that really is set."""
    panel = _panel(pool=fake_pool)
    interaction = make_interaction()
    panel.message = interaction.message
    fake_pool.fetchrow_return = {
        "accent": 0x5865F2,
        "background_format": "webp",
        "has_background": True,
    }

    await panel.show_background_instructions(interaction)

    assert panel.state == {"accent": 0x5865F2, "has_background": True}
    reset = [
        child
        for child in panel.walk_children()
        if isinstance(child, discord.ui.Button)
        and child.label == "Reset background"
    ]
    assert reset and reset[0].disabled is False
    # The rebuilt panel really went back to Discord: the hint already answered
    # the interaction, so refresh_layout falls through to message.edit, and
    # asserting the edit is what stops this test passing on a panel that was
    # only rebuilt in memory.
    assert interaction.message_edits
    _args, kwargs = interaction.message_edits[-1]
    assert kwargs["view"] is panel


async def test_show_background_instructions_survives_a_failed_refresh(
    fake_pool, make_interaction
):
    """A refresh failure must never cost the admin the instructions themselves."""

    async def _boom(query, *args):
        raise RuntimeError("db down")

    fake_pool.fetchrow = _boom
    panel = _panel(pool=fake_pool)
    interaction = make_interaction()
    panel.message = interaction.message

    await panel.show_background_instructions(interaction)

    assert interaction.sent  # the hint still landed
    assert panel.state == {"accent": None, "has_background": False}
    assert not interaction.message_edits  # nothing to refresh with


async def test_reset_background_calls_the_seam_and_rerenders(make_interaction):
    leveling_cog = _FakeLevelingCog()
    panel = _panel(leveling_cog=leveling_cog, has_background=True)
    interaction = make_interaction()
    panel.message = interaction.message  # a real stand-in: edit() is awaitable

    await panel.reset_background(interaction)

    assert leveling_cog.clear_rank_card_calls == [(7, "background")]
    assert panel.state["has_background"] is False
    assert interaction.edits  # the rebuilt panel went back to Discord


async def test_reset_accent_calls_the_seam_and_rerenders(make_interaction):
    leveling_cog = _FakeLevelingCog()
    panel = _panel(leveling_cog=leveling_cog, accent=0x5865F2)
    interaction = make_interaction()
    panel.message = interaction.message

    await panel.reset_accent(interaction)

    assert leveling_cog.clear_rank_card_calls == [(7, "accent")]
    assert panel.state["accent"] is None
    assert interaction.edits


async def test_set_accent_valid_hex_updates_state_and_rerenders(make_interaction):
    leveling_cog = _FakeLevelingCog()
    panel = _panel(leveling_cog=leveling_cog)
    interaction = make_interaction()
    panel.message = interaction.message

    await panel.set_accent(interaction, "#5865F2")

    assert leveling_cog.set_rank_accent_calls == [(7, "#5865F2")]
    assert panel.state["accent"] == 0x5865F2
    assert interaction.edits


async def test_set_accent_expands_the_three_digit_shorthand(make_interaction):
    """'#58F' expands like discord.Colour.from_str: '5'*2 '8'*2 'F'*2."""
    leveling_cog = _FakeLevelingCog()
    panel = _panel(leveling_cog=leveling_cog)
    interaction = make_interaction()
    panel.message = interaction.message

    await panel.set_accent(interaction, "#58F")

    assert panel.state["accent"] == 0x5588FF


async def test_set_accent_invalid_hex_notifies_failure_and_keeps_state(
    make_interaction,
):
    leveling_cog = _FakeLevelingCog()
    panel = _panel(leveling_cog=leveling_cog)
    interaction = make_interaction()
    panel.message = interaction.message

    await panel.set_accent(interaction, "not-a-colour")

    assert panel.state["accent"] is None
    assert interaction.sent  # an ephemeral refusal explaining the hex format
    args, kwargs = interaction.sent[0]
    assert kwargs.get("ephemeral") is True
    assert "hex colour" in args[0]


async def test_preview_defers_and_sends_an_ephemeral_file(make_interaction, monkeypatch):
    leveling_cog = _FakeLevelingCog()
    panel = _panel(leveling_cog=leveling_cog)
    buf = io.BytesIO(b"card-bytes")

    async def _fake_render(bot, leveling_cog_arg, guild, member):
        assert leveling_cog_arg is leveling_cog
        return buf

    monkeypatch.setattr(level_config_ui_module, "_render_card_preview", _fake_render)
    interaction = make_interaction()

    await panel.preview(interaction)

    assert interaction.defers  # deferred before the slow render
    # thinking=True is required on a component interaction: without it
    # discord.py sends deferred_message_update, which drops the ephemeral flag
    # and shows the clicker no loading state while the card renders.
    _defer_args, defer_kwargs = interaction.defers[0]
    assert defer_kwargs.get("ephemeral") is True
    assert defer_kwargs.get("thinking") is True
    assert len(interaction.followups) == 1
    _args, kwargs = interaction.followups[0]
    assert kwargs.get("ephemeral") is True
    assert kwargs["file"].filename == "rank_preview.png"


async def test_a_second_preview_click_inside_the_window_renders_nothing(
    make_interaction, monkeypatch
):
    """A button carries no commands.cooldown, so this debounce is the only
    thing standing between a held-down click and the BOT-WIDE 2-slot image
    semaphore. A refused click must cost a reply and nothing else: no defer, no
    render."""
    panel = _panel(leveling_cog=_FakeLevelingCog())
    renders = []

    async def _fake_render(bot, leveling_cog_arg, guild, member):
        renders.append(member)
        return io.BytesIO(b"card-bytes")

    monkeypatch.setattr(level_config_ui_module, "_render_card_preview", _fake_render)

    await panel.preview(make_interaction(user_id=1))
    second = make_interaction(user_id=1)
    await panel.preview(second)

    assert len(renders) == 1  # only the first click reached the pipeline
    assert not second.defers  # and the refusal never took an image slot
    assert second.sent
    _args, kwargs = second.sent[0]
    assert kwargs.get("ephemeral") is True


async def test_the_preview_debounce_is_per_user(make_interaction, monkeypatch):
    """The panel is author-gated, but the map is bot-wide: one admin's click
    must never cool down another guild's admin."""
    panel = _panel(leveling_cog=_FakeLevelingCog())
    renders = []

    async def _fake_render(bot, leveling_cog_arg, guild, member):
        renders.append(member)
        return io.BytesIO(b"card-bytes")

    monkeypatch.setattr(level_config_ui_module, "_render_card_preview", _fake_render)

    await panel.preview(make_interaction(user_id=1))
    await panel.preview(make_interaction(user_id=2))

    assert len(renders) == 2


async def test_a_refused_preview_click_does_not_extend_the_window(
    make_interaction, monkeypatch
):
    """The window is touched only on an ALLOWED click (the same discipline as
    music's _check_station_debounce), so hammering the button cannot push the
    next legitimate preview further and further away."""
    panel = _panel(leveling_cog=_FakeLevelingCog())

    async def _fake_render(bot, leveling_cog_arg, guild, member):
        return io.BytesIO(b"card-bytes")

    monkeypatch.setattr(level_config_ui_module, "_render_card_preview", _fake_render)
    debounce = level_config_ui_module._PREVIEW_DEBOUNCE

    await panel.preview(make_interaction(user_id=1))
    first_touch = debounce._seen[1]
    await panel.preview(make_interaction(user_id=1))  # refused

    assert debounce._seen[1] == first_touch


# -- _render_card_preview: the real pipeline, routed through run_image_job --


class _FakeAsset:
    def __init__(self, data=b"avatar-bytes"):
        self._data = data

    def replace(self, size=128):
        return self

    async def read(self):
        return self._data


class _FakeColour:
    def __init__(self, value=0):
        self.value = value

    def to_rgb(self):
        return (10, 20, 30)


class _FakeMember:
    def __init__(self, member_id=1, name="Yasuho"):
        self.id = member_id
        self.display_name = name
        self.display_avatar = _FakeAsset()
        self.colour = _FakeColour()


class _FakeLevelingCogForPreview:
    def __init__(self, guild_accent=None, has_background=False):
        self._guild_accent = guild_accent
        self._has_background = has_background
        self.style_calls = []

    async def ensure_rank_card_style(self, guild_id):
        self.style_calls.append(guild_id)
        return self._guild_accent, self._has_background

    @staticmethod
    def _render_rank_card(*args, **kwargs):
        return io.BytesIO(b"rendered-card")


async def test_render_card_preview_routes_through_run_image_job(fake_pool, monkeypatch):
    calls = []

    async def _fake_run_image_job(bot, function, *args, **kwargs):
        calls.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(
        level_config_ui_module.rendering, "run_image_job", _fake_run_image_job
    )
    fake_pool.fetchval_return = 0
    leveling_cog = _FakeLevelingCogForPreview()
    bot = _make_bot(fake_pool, leveling_cog)
    guild = _FakeGuild(guild_id=7)
    member = _FakeMember()

    buf = await level_config_ui_module._render_card_preview(
        bot, leveling_cog, guild, member
    )

    assert len(calls) == 1
    assert buf.getvalue() == b"rendered-card"
    assert leveling_cog.style_calls == [7]


async def test_render_card_preview_fetches_the_background_only_when_configured(
    fake_pool, monkeypatch
):
    async def _fake_run_image_job(bot, function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        level_config_ui_module.rendering, "run_image_job", _fake_run_image_job
    )
    fake_pool.fetchval_return = 0
    leveling_cog = _FakeLevelingCogForPreview(has_background=False)
    bot = _make_bot(fake_pool, leveling_cog)

    await level_config_ui_module._render_card_preview(
        bot, leveling_cog, _FakeGuild(guild_id=7), _FakeMember()
    )

    assert not any("FROM rank_cards" in c[1] for c in fake_pool.calls)


# ---------------------------------------------------------------------------
# Panel plumbing: author-gate + locale (shared AuthorLayoutView contract)
# ---------------------------------------------------------------------------


async def test_card_panel_rejects_a_non_author_interaction(make_interaction):
    panel = _panel()
    interaction = make_interaction(user_id=999)

    allowed = await panel.interaction_check(interaction)

    assert allowed is False
    assert interaction.sent
    assert "isn't for you" in interaction.sent[0][0][0]


async def test_card_panel_resolves_the_clicker_locale(make_interaction, monkeypatch):
    panel = _panel()
    interaction = make_interaction(user_id=1)
    calls = []

    async def _spy(interaction_arg):
        calls.append(interaction_arg)

    monkeypatch.setattr(i18n, "apply_interaction_locale", _spy)

    allowed = await panel.interaction_check(interaction)

    assert allowed is True
    assert calls == [interaction]

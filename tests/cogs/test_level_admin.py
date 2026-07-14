"""Unit tests for cogs.community.level_admin.LevelAdmin (the /levelconfig xp group, L5).

The pure value maths live in tests/tools/test_level_admin.py; these drive the
COG side against fakes: give/take/set write ONLY the lifetime levels row (never
xp_period) and route through the Leveling reward/announce seam with the right
old/new XP, out-of-range amounts are refused before any DB call, the reset
confirm actually deletes + reconciles, and the resetall modal wipes levels AND
xp_period only when the typed server name matches.
"""

import types

from cogs.community.level_admin import (
    LevelAdmin,
    _ResetAllModal,
    _ResetAllView,
    _ResetConfirmView,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMember:
    def __init__(self, uid):
        self.id = uid
        self.mention = f"<@{uid}>"
        self.display_name = f"user-{uid}"


class _FakeGuild:
    def __init__(self, guild_id=1, name="guild"):
        self.id = guild_id
        self.name = name


class _FakeChannel:
    def __init__(self, channel_id=100):
        self.id = channel_id


class _FakeCtx:
    def __init__(self, guild=None, channel=None, author_id=1):
        self.sends = []
        self.guild = guild or _FakeGuild()
        self.channel = channel or _FakeChannel()
        self.author = types.SimpleNamespace(id=author_id)
        self.invoked_subcommand = None

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=999)


class _FakeLevelingCog:
    """Records apply_admin_xp_change calls (the reward/announce seam)."""

    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    async def apply_admin_xp_change(self, *, guild, member, channel, old_xp, new_xp):
        self.calls.append(
            {
                "guild": guild,
                "member": member,
                "channel": channel,
                "old_xp": old_xp,
                "new_xp": new_xp,
            }
        )
        if self.raises is not None:
            raise self.raises


def _make_bot(fake_pool, leveling_cog=None):
    return types.SimpleNamespace(
        db_pool=fake_pool,
        get_cog=lambda name: leveling_cog if name == "Leveling" else None,
    )


def _level_writes(fake_pool):
    return [
        c
        for c in fake_pool.calls
        if c[0] == "execute" and "INSERT INTO levels" in c[1]
    ]


def _period_touches(fake_pool):
    return [c for c in fake_pool.calls if "xp_period" in c[1]]


# ---------------------------------------------------------------------------
# give / take / set: write levels, never xp_period, route through the seam
# ---------------------------------------------------------------------------


async def test_give_adds_writes_absolute_total_and_routes(fake_pool):
    fake_pool.fetchval_return = 100  # current xp
    lv = _FakeLevelingCog()
    cog = LevelAdmin(_make_bot(fake_pool, lv))
    ctx = _FakeCtx(guild=_FakeGuild(1))
    member = _FakeMember(2)

    await cog.cmd_give(ctx, member, 50)

    writes = _level_writes(fake_pool)
    assert len(writes) == 1
    assert writes[0][2] == (1, 2, 150)  # old 100 + 50
    assert _period_touches(fake_pool) == []  # admin edits never write xp_period
    assert lv.calls == [
        {
            "guild": ctx.guild,
            "member": member,
            "channel": ctx.channel,
            "old_xp": 100,
            "new_xp": 150,
        }
    ]
    assert "embed" in ctx.sends[0][1]


async def test_take_floors_at_zero(fake_pool):
    fake_pool.fetchval_return = 30
    lv = _FakeLevelingCog()
    cog = LevelAdmin(_make_bot(fake_pool, lv))
    ctx = _FakeCtx()
    member = _FakeMember(2)

    await cog.cmd_take(ctx, member, 100)  # would be -70

    assert _level_writes(fake_pool)[0][2] == (1, 2, 0)  # never negative
    assert lv.calls[0]["old_xp"] == 30
    assert lv.calls[0]["new_xp"] == 0


async def test_set_writes_the_exact_total(fake_pool):
    fake_pool.fetchval_return = 5000
    lv = _FakeLevelingCog()
    cog = LevelAdmin(_make_bot(fake_pool, lv))
    ctx = _FakeCtx()

    await cog.cmd_set(ctx, _FakeMember(2), 0)  # soft reset to 0

    assert _level_writes(fake_pool)[0][2] == (1, 2, 0)
    assert lv.calls[0]["old_xp"] == 5000 and lv.calls[0]["new_xp"] == 0


async def test_give_out_of_range_amount_is_refused_before_any_db(fake_pool):
    cog = LevelAdmin(_make_bot(fake_pool, _FakeLevelingCog()))
    ctx = _FakeCtx()

    await cog.cmd_give(ctx, _FakeMember(2), 0)  # below MIN 1

    assert fake_pool.calls == []  # no read, no write
    assert any("between" in c[0][0] for c in ctx.sends)


async def test_give_amount_over_max_is_refused(fake_pool):
    cog = LevelAdmin(_make_bot(fake_pool, _FakeLevelingCog()))
    ctx = _FakeCtx()

    await cog.cmd_give(ctx, _FakeMember(2), 1_000_001)

    assert fake_pool.calls == []


async def test_set_negative_is_refused(fake_pool):
    cog = LevelAdmin(_make_bot(fake_pool, _FakeLevelingCog()))
    ctx = _FakeCtx()

    await cog.cmd_set(ctx, _FakeMember(2), -1)

    assert fake_pool.calls == []


async def test_set_zero_is_allowed(fake_pool):
    """0 is IN range for set (a soft reset), so it does write."""
    fake_pool.fetchval_return = 42
    cog = LevelAdmin(_make_bot(fake_pool, _FakeLevelingCog()))
    ctx = _FakeCtx()

    await cog.cmd_set(ctx, _FakeMember(2), 0)

    assert len(_level_writes(fake_pool)) == 1


async def test_adjust_without_leveling_cog_still_writes(fake_pool):
    """A missing Leveling cog never breaks the XP write - only the routing."""
    fake_pool.fetchval_return = 100
    cog = LevelAdmin(_make_bot(fake_pool, leveling_cog=None))
    ctx = _FakeCtx()

    await cog.cmd_give(ctx, _FakeMember(2), 50)

    assert _level_writes(fake_pool)[0][2] == (1, 2, 150)
    assert "embed" in ctx.sends[0][1]


async def test_routing_failure_never_breaks_the_command(fake_pool):
    """apply_admin_xp_change blowing up is swallowed - the write already landed."""
    fake_pool.fetchval_return = 100
    lv = _FakeLevelingCog(raises=RuntimeError("boom"))
    cog = LevelAdmin(_make_bot(fake_pool, lv))
    ctx = _FakeCtx()

    await cog.cmd_give(ctx, _FakeMember(2), 50)  # must not raise

    assert len(_level_writes(fake_pool)) == 1
    assert ctx.sends  # still confirmed to the admin


# ---------------------------------------------------------------------------
# reset (single member): the confirm-view action deletes + reconciles
# ---------------------------------------------------------------------------


def _reset_interaction(make_interaction, guild, channel, user_id=1):
    interaction = make_interaction(user_id=user_id, guild_id=guild.id)
    interaction.guild = guild
    interaction.channel = channel
    return interaction


async def test_reset_confirm_deletes_row_and_routes_down(fake_pool, make_interaction):
    fake_pool.fetchval_return = 250  # they had 250 xp
    lv = _FakeLevelingCog()
    cog = LevelAdmin(_make_bot(fake_pool, lv))
    guild = _FakeGuild(1)
    channel = _FakeChannel()
    member = _FakeMember(2)
    view = _ResetConfirmView(cog, author_id=1, member=member)
    view.message = None
    interaction = _reset_interaction(make_interaction, guild, channel)

    # children[0] is Confirm, children[1] is Cancel (added in that order).
    await view.children[0].callback(interaction)

    deletes = [
        c for c in fake_pool.calls if c[0] == "execute" and "DELETE FROM levels" in c[1]
    ]
    assert len(deletes) == 1
    assert deletes[0][2] == (1, 2)
    assert _period_touches(fake_pool) == []  # single reset leaves period rows
    assert lv.calls == [
        {
            "guild": guild,
            "member": member,
            "channel": channel,
            "old_xp": 250,
            "new_xp": 0,
        }
    ]
    assert interaction.edits  # the panel was edited to a confirmation


async def test_reset_cancel_changes_nothing(fake_pool, make_interaction):
    cog = LevelAdmin(_make_bot(fake_pool, _FakeLevelingCog()))
    view = _ResetConfirmView(cog, author_id=1, member=_FakeMember(2))
    interaction = _reset_interaction(make_interaction, _FakeGuild(1), _FakeChannel())

    await view.children[1].callback(interaction)  # Cancel

    assert fake_pool.calls == []  # nothing deleted
    assert interaction.edits  # but the prompt was cleared


async def test_reset_confirm_without_leveling_cog_still_deletes(
    fake_pool, make_interaction
):
    fake_pool.fetchval_return = 10
    cog = LevelAdmin(_make_bot(fake_pool, leveling_cog=None))
    view = _ResetConfirmView(cog, author_id=1, member=_FakeMember(2))
    interaction = _reset_interaction(make_interaction, _FakeGuild(1), _FakeChannel())

    await view.children[0].callback(interaction)  # must not raise

    assert any("DELETE FROM levels" in c[1] for c in fake_pool.calls)


# ---------------------------------------------------------------------------
# resetall: the name-match modal gate wipes levels AND xp_period, or nothing
# ---------------------------------------------------------------------------


async def test_resetall_modal_matching_name_wipes_levels_and_periods(
    fake_pool, make_interaction
):
    fake_pool.fetchval_return = 7  # 7 member records
    cog = LevelAdmin(_make_bot(fake_pool, _FakeLevelingCog()))
    guild = _FakeGuild(1, name="My Server")
    owner = _ResetAllView(cog, author_id=1, guild=guild)
    owner.message = None
    modal = _ResetAllModal(cog, guild, owner)
    modal.name_input._value = "My Server"  # exact match
    interaction = make_interaction(user_id=1, guild_id=1)

    await modal.on_submit(interaction)

    deletes = [c for c in fake_pool.calls if c[0] == "execute"]
    tables = " ".join(c[1] for c in deletes)
    assert "DELETE FROM levels" in tables
    assert "DELETE FROM xp_period" in tables
    assert interaction.sent  # a confirmation was sent
    assert "7" in interaction.sent[0][0][0]  # wiped-count reported


async def test_resetall_modal_wrong_name_wipes_nothing(fake_pool, make_interaction):
    cog = LevelAdmin(_make_bot(fake_pool, _FakeLevelingCog()))
    guild = _FakeGuild(1, name="My Server")
    owner = _ResetAllView(cog, author_id=1, guild=guild)
    modal = _ResetAllModal(cog, guild, owner)
    modal.name_input._value = "wrong name"
    interaction = make_interaction(user_id=1, guild_id=1)

    await modal.on_submit(interaction)

    assert not any(c[0] == "execute" for c in fake_pool.calls)  # no DELETE at all
    assert interaction.sent  # a "does not match" refusal was sent


async def test_resetall_cancel_button_changes_nothing(fake_pool, make_interaction):
    cog = LevelAdmin(_make_bot(fake_pool, _FakeLevelingCog()))
    view = _ResetAllView(cog, author_id=1, guild=_FakeGuild(1))
    interaction = make_interaction(user_id=1, guild_id=1)

    # children[0] is the danger button, children[1] is Cancel.
    await view.children[1].callback(interaction)

    assert fake_pool.calls == []
    assert interaction.edits


async def test_perform_reset_all_returns_the_wiped_count(fake_pool):
    fake_pool.fetchval_return = 12
    cog = LevelAdmin(_make_bot(fake_pool, _FakeLevelingCog()))

    count = await cog._perform_reset_all(1)

    assert count == 12

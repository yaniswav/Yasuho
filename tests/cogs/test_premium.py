"""``?premium``: the owner-only hand-gifting surface (cogs/system/premium.py).

Prefix-only, no app_command - see the cog's own module docstring for why. These
tests cover, in order:

1. THE OWNER GATE. Every leaf command (``?premium`` itself and every
   subcommand ``walk_commands()`` finds) carries its own ``@commands.is_owner()``
   check, run for real against a fake ``bot.is_owner``. This is the structural
   guard tests/test_hybrid_gating_hygiene.py argues for in prose, applied here
   because this group is deliberately NOT hybrid (no app_command exists for it
   to protect, and cog_check already gates the prefix path on its own - the
   per-command decorator is the belt to that cog-wide suspenders).
2. Grant/revoke/list/check behaviour against the repo's ``fake_pool`` fixture.
3. The cache is only ever touched AFTER its matching database write succeeds.
4. Duration parsing (:func:`premium_cog._parse_duration_and_reason`), the
   pure function the grant commands share with no bot and no pool.

No Discord, no network: ``make_context`` (a plain ``commands.Context`` stand-
in) and ``fake_pool`` (an in-memory asyncpg pool stand-in) are both from
conftest.py.
"""

from __future__ import annotations

import datetime
import types

import pytest
from discord.ext import commands

from cogs.system import premium as premium_cog
from tools import premium

UTC = datetime.timezone.utc


def _bot(pool, *, owner_id=1):
    async def is_owner(user):
        return user.id == owner_id

    return types.SimpleNamespace(
        db_pool=pool,
        is_owner=is_owner,
        premium=premium.EntitlementCache(),
    )


def _cog(pool, *, owner_id=1):
    bot = _bot(pool, owner_id=owner_id)
    return premium_cog.Premium(bot), bot


# ---------------------------------------------------------------------------
# 1. The owner gate - every leaf, both the cog_check layer and the per-leaf one
# ---------------------------------------------------------------------------


def _all_commands(cog):
    """``?premium`` itself, plus every subcommand at every depth."""
    group = cog.premium_group
    return [group, *group.walk_commands()]


async def test_cog_check_delegates_to_bot_is_owner():
    cog, _bot = _cog(object())
    owner_ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=1))
    other_ctx = types.SimpleNamespace(author=types.SimpleNamespace(id=7))
    assert await cog.cog_check(owner_ctx) is True
    assert await cog.cog_check(other_ctx) is False


async def test_every_leaf_carries_its_own_owner_check():
    """Positive control: every command's checks refuse a non-owner.

    Discord.py's ``commands.is_owner()`` predicate raises ``NotOwner`` rather
    than returning False - see discord/ext/commands/core.py - so both shapes
    are accepted, but at least one check must refuse.
    """
    cog, _bot = _cog(object())
    owner_ctx = types.SimpleNamespace(bot=cog.bot, author=types.SimpleNamespace(id=1))
    other_ctx = types.SimpleNamespace(bot=cog.bot, author=types.SimpleNamespace(id=7))

    for command in _all_commands(cog):
        assert command.checks, f"{command.qualified_name} has no checks at all"
        refused = False
        for check in command.checks:
            try:
                ok = await check(other_ctx)
            except commands.CheckFailure:
                refused = True
                break
            if ok is False:
                refused = True
                break
        assert refused, f"{command.qualified_name} did not refuse a non-owner"

        # ...and the owner is let through by every one of its checks.
        for check in command.checks:
            assert await check(owner_ctx) is True, command.qualified_name


async def test_negative_control_a_leaf_missing_its_check_is_caught():
    """NEGATIVE CONTROL for the guard above: a command with NO checks at all
    (the exact shape of forgetting ``@commands.is_owner()`` on one subcommand)
    must fail :func:`test_every_leaf_carries_its_own_owner_check`'s assertion."""
    cog, _bot = _cog(object())
    fabricated = [cmd for cmd in _all_commands(cog) if cmd.checks] + [
        types.SimpleNamespace(qualified_name="premium fabricated", checks=[])
    ]
    with pytest.raises(AssertionError):
        for command in fabricated:
            assert command.checks, f"{command.qualified_name} has no checks at all"


def test_the_group_has_no_app_command_anywhere():
    """Prefix-only by design: zero slash-tree cost, so the owner surface can
    never need to compete for the global command budget."""
    cog, _bot = _cog(object())
    for command in _all_commands(cog):
        assert not hasattr(command, "app_command") or command.app_command is None


# ---------------------------------------------------------------------------
# 4. Duration parsing (pure, no bot/pool needed)
# ---------------------------------------------------------------------------


def test_duration_token_is_consumed_and_the_rest_is_the_reason():
    expires_at, reason = premium_cog._parse_duration_and_reason(
        "30d a gift for a friend"
    )
    assert expires_at is not None
    assert reason == "a gift for a friend"


def test_no_duration_token_leaves_everything_as_the_reason():
    expires_at, reason = premium_cog._parse_duration_and_reason(
        "just being generous today"
    )
    assert expires_at is None
    assert reason == "just being generous today"


def test_empty_rest_is_permanent_with_no_reason():
    expires_at, reason = premium_cog._parse_duration_and_reason("")
    assert (expires_at, reason) == (None, None)
    expires_at, reason = premium_cog._parse_duration_and_reason(None)
    assert (expires_at, reason) == (None, None)


def test_a_bare_duration_with_no_reason_leaves_reason_none():
    expires_at, reason = premium_cog._parse_duration_and_reason("7d")
    assert expires_at is not None
    assert reason is None


# ---------------------------------------------------------------------------
# 2+3. Grant / revoke / list / check, and the cache-after-write ordering
# ---------------------------------------------------------------------------


async def test_grant_server_writes_then_refreshes_the_guild_cache(
    fake_pool, make_context
):
    fake_pool.fetchrow_return = {"id": 9}
    fake_pool.fetch_return = [
        {
            "id": 9,
            "product": "yasuho_plus",
            "scope_type": "guild",
            "guild_id": 111,
            "user_id": None,
            "reason": "friend's server",
            "granted_by": 1,
            "granted_at": datetime.datetime(2026, 1, 1, tzinfo=UTC),
            "expires_at": None,
            "revoked_at": None,
            "revoked_by": None,
        }
    ]
    cog, bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_grant_server.callback(
        cog, ctx, 111, rest="a gift for a friend"
    )

    insert = next(c for c in fake_pool.calls if c[0] == "fetchrow")
    assert "INSERT INTO premium_grants" in insert[1]
    assert insert[2] == ("yasuho_plus", "guild", 111, None, "a gift for a friend", 1, None)
    assert bot.premium._guild_grants == {
        111: [premium._GrantSnapshot(grant_id=9, product="yasuho_plus", expires_at=None)]
    }
    assert ctx.sends  # a confirmation was sent
    assert "Granted" in ctx.sends[0][0][0] or "Granted" in str(ctx.sends[0][1])


async def test_grant_user_writes_then_refreshes_the_user_cache(fake_pool, make_context):
    fake_pool.fetchrow_return = {"id": 3}
    fake_pool.fetch_return = [
        {
            "id": 3,
            "product": "comfort_pack",
            "scope_type": "user",
            "guild_id": None,
            "user_id": 222,
            "reason": None,
            "granted_by": 1,
            "granted_at": datetime.datetime(2026, 1, 1, tzinfo=UTC),
            "expires_at": None,
            "revoked_at": None,
            "revoked_by": None,
        }
    ]
    cog, bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_grant_user.callback(cog, ctx, 222, rest="")

    assert bot.premium._user_grants == {
        222: [premium._GrantSnapshot(grant_id=3, product="comfort_pack", expires_at=None)]
    }


async def test_a_failed_grant_write_never_touches_the_cache(fake_pool, make_context, monkeypatch):
    """The ordering the M3a+ brief asks for: DB first, cache only on success."""

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(premium_cog.premium, "create_grant", _boom)
    cog, bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    with pytest.raises(RuntimeError):
        await cog.premium_grant_server.callback(cog, ctx, 111, rest="")

    assert bot.premium._guild_grants == {}
    assert ctx.sends == []


async def test_revoke_looks_up_the_scope_then_refreshes_only_that_scope(
    fake_pool, make_context
):
    fake_pool.fetchrow_return = {"scope_type": "guild", "guild_id": 111, "user_id": None}
    fake_pool.execute_return = "UPDATE 1"
    fake_pool.fetch_return = []  # no more active grants for guild 111
    cog, bot = _cog(fake_pool)
    # pre-existing cache entry
    bot.premium._guild_grants[111] = [
        premium._GrantSnapshot(grant_id=9, product="yasuho_plus", expires_at=None)
    ]
    ctx = make_context(author_id=1)

    await cog.premium_revoke.callback(cog, ctx, 9)

    execute_call = next(c for c in fake_pool.calls if c[0] == "execute")
    assert "SET revoked_at = now()" in execute_call[1]
    assert execute_call[2] == (9, 1)
    # The scope's cache entry was re-derived from the (now empty) DB result,
    # not left stale.
    assert bot.premium._guild_grants == {}
    assert ctx.sends


async def test_revoke_reports_a_no_op_without_touching_the_cache(
    fake_pool, make_context
):
    fake_pool.fetchrow_return = {"scope_type": "guild", "guild_id": 111, "user_id": None}
    fake_pool.execute_return = "UPDATE 0"  # already revoked / unknown id
    cog, bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_revoke.callback(cog, ctx, 999)

    assert not any(c[0] == "fetch" for c in fake_pool.calls)  # no refresh ran
    assert bot.premium._guild_grants == {}
    assert ctx.sends


async def test_list_rejects_an_unknown_scope_keyword(fake_pool, make_context):
    cog, _bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_list.callback(cog, ctx, "guild", 111)

    assert fake_pool.calls == []  # refused before touching the database
    assert ctx.sends


async def test_list_with_no_filter_lists_every_active_grant(fake_pool, make_context):
    fake_pool.fetch_return = [
        {
            "id": 1,
            "product": "yasuho_plus",
            "scope_type": "guild",
            "guild_id": 111,
            "user_id": None,
            "reason": None,
            "granted_by": 1,
            "granted_at": datetime.datetime(2026, 1, 1, tzinfo=UTC),
            "expires_at": None,
            "revoked_at": None,
            "revoked_by": None,
        }
    ]
    cog, _bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_list.callback(cog, ctx, None, None)

    _method, query, args = fake_pool.calls[0]
    assert "FROM premium_grants" in query
    assert args == ()  # no scope filter: every active grant
    assert ctx.sends


async def test_check_reports_free_when_nothing_is_active(fake_pool, make_context):
    fake_pool.fetch_return = []
    cog, _bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_check.callback(cog, ctx, "server", 111)

    assert ctx.sends
    embed = ctx.sends[0][1]["embed"]
    assert embed.fields[0].value == "Free"


# ---------------------------------------------------------------------------
# Argument validation: a zero/negative id can never be a real Discord
# snowflake, and must be refused before the database is ever touched.
# ---------------------------------------------------------------------------


async def test_grant_server_refuses_a_zero_or_negative_guild_id(fake_pool, make_context):
    cog, _bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_grant_server.callback(cog, ctx, 0, rest="")
    await cog.premium_grant_server.callback(cog, ctx, -5, rest="")

    assert fake_pool.calls == []  # refused before touching the database
    assert len(ctx.sends) == 2


async def test_grant_user_refuses_a_zero_or_negative_user_id(fake_pool, make_context):
    cog, _bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_grant_user.callback(cog, ctx, 0, rest="")
    await cog.premium_grant_user.callback(cog, ctx, -1, rest="")

    assert fake_pool.calls == []
    assert len(ctx.sends) == 2


async def test_grant_server_accepts_a_guild_the_bot_is_not_currently_in(
    fake_pool, make_context
):
    """DELIBERATE: no bot.get_guild membership check - see the command's own
    docstring for the pre-launch/preorder-gift reasoning this decides on."""
    fake_pool.fetchrow_return = {"id": 1}
    fake_pool.fetch_return = []
    cog, bot = _cog(fake_pool)
    assert not hasattr(bot, "get_guild")  # the stand-in bot has no guild cache at all
    ctx = make_context(author_id=1)

    await cog.premium_grant_server.callback(cog, ctx, 999999999999999999, rest="")

    assert any(c[0] == "fetchrow" for c in fake_pool.calls)
    assert ctx.sends


# ---------------------------------------------------------------------------
# Embed size budgets: _clip_reason / _join_within_budget
# ---------------------------------------------------------------------------


def test_clip_reason_leaves_a_short_reason_untouched():
    assert premium_cog._clip_reason("a gift for a friend") == "a gift for a friend"
    assert premium_cog._clip_reason(None) is None
    assert premium_cog._clip_reason("") == ""


def test_clip_reason_shortens_a_long_one_with_a_marker():
    reason = "x" * 500
    clipped = premium_cog._clip_reason(reason)
    assert len(clipped) == premium_cog._REASON_CLIP + 3
    assert clipped.endswith("...")


def test_join_within_budget_keeps_every_line_when_it_fits():
    lines = ["a", "b", "c"]
    assert premium_cog._join_within_budget(lines, 1000) == "a\nb\nc"


def test_join_within_budget_truncates_and_counts_the_rest():
    lines = ["x" * 100 for _ in range(20)]  # 2000+ chars, well past a small budget
    joined = premium_cog._join_within_budget(lines, 250)
    assert len(joined) <= 250 + 40  # the "+N more" marker line itself
    assert "more not shown" in joined


async def test_check_never_sends_a_field_value_past_discords_limit(
    fake_pool, make_context
):
    """A long reason, repeated across many active grants, must never make it
    past the 1024-character field-value budget - the regression this guard
    protects against (Discord's API rejecting the send outright)."""
    fake_pool.fetch_return = [
        {
            "id": i,
            "product": "yasuho_plus",
            "scope_type": "guild",
            "guild_id": 111,
            "user_id": None,
            "reason": "x" * 1900,  # near a Discord message's own 2000-char cap
            "granted_by": 1,
            "granted_at": datetime.datetime(2026, 1, 1, tzinfo=UTC),
            "expires_at": None,
            "revoked_at": None,
            "revoked_by": None,
        }
        for i in range(10)
    ]
    cog, _bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_check.callback(cog, ctx, "server", 111)

    embed = ctx.sends[0][1]["embed"]
    grants_field = next(f for f in embed.fields if f.name == "Owner grant(s)")
    assert len(grants_field.value) <= 1024


async def test_list_never_sends_a_description_past_discords_limit(
    fake_pool, make_context
):
    fake_pool.fetch_return = [
        {
            "id": i,
            "product": "yasuho_plus",
            "scope_type": "guild",
            "guild_id": 100000000000000000 + i,
            "user_id": None,
            "reason": "irrelevant to the list view",
            "granted_by": 1,
            "granted_at": datetime.datetime(2026, 1, 1, tzinfo=UTC),
            "expires_at": None,
            "revoked_at": None,
            "revoked_by": None,
        }
        for i in range(25)
    ]
    cog, _bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_list.callback(cog, ctx, None, None)

    embed = ctx.sends[0][1]["embed"]
    assert len(embed.description) <= 4096

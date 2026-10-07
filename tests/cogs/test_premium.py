"""``?premiumadmin``: the owner-only hand-gifting surface (cogs/system/premium.py).

Prefix-only, no app_command - see the cog's own module docstring for why. These
tests cover, in order:

1. THE OWNER GATE. Every leaf command (``?premiumadmin`` itself and every
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

5. M3b: the three ``on_entitlement_*`` gateway listeners
   (:meth:`Premium._handle_entitlement_event` and its three thin wrappers) -
   application filtering, write-then-refresh ordering, a failed write never
   touching the cache.
6. M3b: the periodic reconciliation loop
   (:meth:`Premium._reconcile_once`/:meth:`Premium._configured_skus`) and its
   ``@tasks.loop`` error handler - all driven directly (no real scheduler,
   same posture as tests/cogs/test_anilist_airing.py's ``_tick``-level
   tests), since ``cog_load`` (where the loop actually starts in production)
   is never invoked by ``_cog()`` below - see ``Premium.cog_load``'s own
   docstring for why that split exists.
"""

from __future__ import annotations

import asyncio
import datetime
import types

import discord
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
    """``?premiumadmin`` itself, plus every subcommand at every depth."""
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


# ---------------------------------------------------------------------------
# M3b: the ENTITLEMENT_* gateway listeners and the periodic reconciliation
# loop. These need a richer bot stand-in than _bot()/_cog() above (an
# ``application_id``, an ``entitlements()`` async iterator, a
# ``wait_until_ready``) - kept separate rather than widening _bot() itself,
# so every pre-existing ?premiumadmin command test above stays exactly as
# untouched by this lot as test_cache_mirror_registry.py's own rule insists
# a rename/widening like this should have to justify itself for.
# ---------------------------------------------------------------------------

APPLICATION_ID = 999


def _remote_entitlement(**overrides):
    row = dict(
        id=555,
        sku_id=111,
        guild_id=42,
        user_id=None,
        type=2,
        deleted=False,
        consumed=False,
        # A real purchase always has a start date; a missing one marks a
        # TEST entitlement (see _is_test_entitlement).
        starts_at=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc),
        ends_at=None,
        application_id=APPLICATION_ID,
    )
    row.update(overrides)
    return types.SimpleNamespace(**row)


def _m3b_bot(pool, *, application_id=APPLICATION_ID, stream_factory=None, owner_id=1):
    async def is_owner(user):
        return user.id == owner_id

    async def _empty_stream():
        for _ in ():
            yield _

    def entitlements(**kwargs):
        return stream_factory(**kwargs) if stream_factory is not None else _empty_stream()

    async def wait_until_ready():
        return None

    return types.SimpleNamespace(
        db_pool=pool,
        is_owner=is_owner,
        premium=premium.EntitlementCache(),
        application_id=application_id,
        entitlements=entitlements,
        wait_until_ready=wait_until_ready,
    )


def _m3b_cog(pool, **kwargs):
    bot = _m3b_bot(pool, **kwargs)
    return premium_cog.Premium(bot), bot


# -- on_entitlement_create/update/delete ------------------------------------


async def test_on_entitlement_create_writes_then_refreshes_the_guild_scope(fake_pool):
    fake_pool.fetch_return = [
        {"entitlement_id": 555, "sku_id": 111, "ends_at": None, "last_synced_at": None}
    ]
    cog, bot = _m3b_cog(fake_pool)

    await cog.on_entitlement_create(_remote_entitlement())

    insert = next(c for c in fake_pool.calls if c[0] == "execute")
    assert "premium_entitlements.deleted OR EXCLUDED.deleted" in insert[1]
    assert insert[2][0] == 555  # entitlement_id
    assert insert[2][6] is False  # deleted
    assert 42 in bot.premium._guild_skus
    assert bot.premium._guild_skus[42][0].entitlement_id == 555


async def test_on_entitlement_update_refreshes_the_user_scope(fake_pool):
    fake_pool.fetch_return = [
        {"entitlement_id": 2, "sku_id": 222, "ends_at": None, "last_synced_at": None}
    ]
    cog, bot = _m3b_cog(fake_pool)

    await cog.on_entitlement_update(
        _remote_entitlement(id=2, sku_id=222, guild_id=None, user_id=7)
    )

    assert 7 in bot.premium._user_skus


async def test_on_entitlement_delete_force_deletes_and_drops_the_scope(fake_pool):
    fake_pool.fetch_return = []  # nothing active left for this guild
    cog, bot = _m3b_cog(fake_pool)
    bot.premium._guild_skus[42] = [
        premium._EntitlementSnapshot(
            entitlement_id=555, sku_id=111, ends_at=None, last_synced_at=None
        )
    ]

    await cog.on_entitlement_delete(_remote_entitlement())

    insert = next(c for c in fake_pool.calls if c[0] == "execute")
    assert insert[2][6] is True  # deleted, forced True regardless of payload
    assert bot.premium._guild_skus == {}  # the scope was popped, not left stale


async def test_on_entitlement_delete_before_its_create_still_ends_up_deleted(fake_pool):
    """The out-of-order case end to end through the listeners themselves
    (tests/tools/test_premium.py covers the store function in isolation) -
    on_entitlement_delete arriving first, with no row existing yet, then the
    late create for the same id."""
    fake_pool.fetch_return = []
    cog, bot = _m3b_cog(fake_pool)

    await cog.on_entitlement_delete(_remote_entitlement())
    await cog.on_entitlement_create(_remote_entitlement(deleted=False))

    inserts = [c for c in fake_pool.calls if c[0] == "execute"]
    assert len(inserts) == 2
    # Both writes went through the OR-preserving query; the cache ends up
    # with nothing active for the guild either way (the DB row stays
    # deleted - see the tools-level test for the stored value itself).
    assert bot.premium._guild_skus == {}


async def test_a_foreign_application_entitlement_is_ignored_entirely(fake_pool):
    cog, bot = _m3b_cog(fake_pool)
    bot.premium._guild_skus[42] = [
        premium._EntitlementSnapshot(
            entitlement_id=1, sku_id=111, ends_at=None, last_synced_at=None
        )
    ]

    await cog.on_entitlement_create(
        _remote_entitlement(application_id=APPLICATION_ID + 1)
    )

    assert fake_pool.calls == []  # never written
    # the pre-existing cache entry is untouched (no refresh ran either)
    assert bot.premium._guild_skus == {
        42: [
            premium._EntitlementSnapshot(
                entitlement_id=1, sku_id=111, ends_at=None, last_synced_at=None
            )
        ]
    }


async def test_an_entitlement_with_no_application_id_known_is_ignored(fake_pool):
    """Fail-safe direction: an unknown application_id on EITHER side (ours or
    the entitlement's) is treated as foreign, never as "assume it's ours"."""
    cog, bot = _m3b_cog(fake_pool, application_id=None)

    await cog.on_entitlement_create(_remote_entitlement())

    assert fake_pool.calls == []


async def test_a_failed_entitlement_write_never_touches_the_cache(fake_pool, monkeypatch):
    """The same ordering the M3a+ grant commands already guarantee: DB
    write first, cache refresh only on success."""

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(premium_cog.premium, "upsert_entitlement_event", _boom)
    cog, bot = _m3b_cog(fake_pool)
    refreshed = []

    async def _spy(*_args, **_kwargs):
        refreshed.append(True)

    bot.premium.refresh_entitlement_scope = _spy

    await cog.on_entitlement_create(_remote_entitlement())  # must not raise

    assert refreshed == []


async def test_a_failed_cache_refresh_after_a_successful_write_is_logged_not_raised(
    fake_pool, caplog
):
    """The write is durable either way; only the in-memory refresh failed -
    this must be caught and logged, never propagated out of the listener."""

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("cache refresh exploded")

    cog, bot = _m3b_cog(fake_pool)
    bot.premium.refresh_entitlement_scope = _boom

    with caplog.at_level("ERROR", logger=premium_cog.log.name):
        await cog.on_entitlement_create(_remote_entitlement())  # must not raise

    assert any(c[0] == "execute" for c in fake_pool.calls)  # the write DID happen
    assert any("cache refresh failed" in r.message for r in caplog.records)


# -- periodic reconciliation -------------------------------------------------


async def test_configured_skus_returns_none_when_neither_sku_is_set(monkeypatch):
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", None)
    monkeypatch.setattr(premium_cog.premium, "COMFORT_PACK_SKU", None)
    cog, _bot = _m3b_cog(object())
    assert cog._configured_skus() is None


async def test_configured_skus_wraps_whichever_sku_is_set(monkeypatch):
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium_cog.premium, "COMFORT_PACK_SKU", 222)
    cog, _bot = _m3b_cog(object())
    skus = cog._configured_skus()
    assert {sku.id for sku in skus} == {111, 222}


async def test_reconcile_once_passes_the_same_sku_filter_to_reconcile(fake_pool, monkeypatch):
    """The exact bug tools.premium.reconcile's own "sku_ids" docstring
    paragraph warns against: ``bot.entitlements(skus=...)`` and the
    ``sku_ids`` this cog hands to ``premium.reconcile`` for its "missing"
    diff MUST name the same skus, or a configured SKU change would wrongly
    mark every row still carrying the OLD sku id deleted on the very next
    pass. Asserted here by capturing what ``_reconcile_once`` actually
    passes, rather than trusting the two call sites stay in sync by eye."""
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium_cog.premium, "COMFORT_PACK_SKU", 222)
    cog, bot = _m3b_cog(fake_pool)

    captured = {}

    async def _fake_reconcile(pool, entitlements, *, application_id, sku_ids=None):
        captured["sku_ids"] = sku_ids
        async for _ in entitlements:
            pass
        return {"seen": 0, "upserted": 0, "missing": 0}

    monkeypatch.setattr(premium_cog.premium, "reconcile", _fake_reconcile)

    await cog._reconcile_once()

    assert set(captured["sku_ids"]) == {sku.id for sku in cog._configured_skus()}


async def test_reconcile_once_sku_filter_is_none_when_no_sku_is_configured(fake_pool, monkeypatch):
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", None)
    monkeypatch.setattr(premium_cog.premium, "COMFORT_PACK_SKU", None)
    cog, bot = _m3b_cog(fake_pool)

    captured = {}

    async def _fake_reconcile(pool, entitlements, *, application_id, sku_ids=None):
        captured["sku_ids"] = sku_ids
        async for _ in entitlements:
            pass
        return {"seen": 0, "upserted": 0, "missing": 0}

    monkeypatch.setattr(premium_cog.premium, "reconcile", _fake_reconcile)

    await cog._reconcile_once()

    assert captured["sku_ids"] is None


async def test_reconcile_once_skips_entirely_without_an_application_id(fake_pool):
    cog, bot = _m3b_cog(fake_pool, application_id=None)
    called = []
    bot.entitlements = lambda **kwargs: called.append(kwargs) or _never_called()

    async def _never_called():
        for _ in ():
            yield _

    await cog._reconcile_once()

    assert called == []
    assert fake_pool.calls == []


async def test_reconcile_once_complete_pass_reloads_the_cache(fake_pool, monkeypatch):
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", 111)
    monkeypatch.setattr(premium_cog.premium, "COMFORT_PACK_SKU", None)

    class _Table:
        def __init__(self):
            self.rows = {}
            self.calls = []

        async def execute(self, query, *args):
            self.calls.append(("execute", query, args))
            self.rows[args[0]] = {
                "entitlement_id": args[0],
                "sku_id": args[1],
                "scope_type": args[2],
                "guild_id": args[3],
                "user_id": args[4],
                "deleted": args[6],
                "ends_at": args[9],
                "last_synced_at": None,
            }
            return "INSERT 0 1"

        async def fetch(self, query, *args):
            self.calls.append(("fetch", query, args))
            if "SELECT entitlement_id FROM premium_entitlements" in query:
                return [{"entitlement_id": rid} for rid in self.rows]
            if "FROM premium_entitlements WHERE deleted = FALSE" in query:
                return list(self.rows.values())
            if "FROM premium_grants" in query:
                return []
            raise AssertionError(f"unexpected query: {query}")

    table = _Table()

    async def _stream(**kwargs):
        yield _remote_entitlement()

    cog, bot = _m3b_cog(table, stream_factory=_stream)

    await cog._reconcile_once()

    assert 555 in table.rows
    assert bot.premium.is_guild_premium(42) is True  # the cache was reloaded


async def test_reconcile_once_aborted_pass_never_reloads_the_cache(monkeypatch):
    async def _stream(**kwargs):
        yield _remote_entitlement()
        raise RuntimeError("discord outage")

    class _Table:
        def __init__(self):
            self.calls = []

        async def execute(self, query, *args):
            self.calls.append(("execute", query, args))
            return "INSERT 0 1"

        async def fetch(self, query, *args, **kwargs):
            self.calls.append(("fetch", query, args))
            # M5's "has this id ever been recorded" read happens BEFORE the
            # listing loop even starts - harmless regardless of how the pass
            # later goes, unlike the "missing" diff query below, which really
            # must never run after an abort.
            if query == "SELECT entitlement_id FROM premium_entitlements":
                return []
            raise AssertionError("must not be reached after an aborted pass")

    cog, bot = _m3b_cog(_Table(), stream_factory=_stream)
    loaded = []
    bot.premium.load = lambda *_a, **_kw: loaded.append(True)

    await cog._reconcile_once()  # must not raise

    assert loaded == []


async def test_reconcile_entitlements_loop_tick_never_raises_on_failure(monkeypatch):
    """The tasks.loop wrapper swallows whatever _reconcile_once raises - same
    posture as cogs/anilist/airing.py's _poll_airing/_tick split."""
    cog, _bot = _m3b_cog(object())

    async def _boom():
        raise RuntimeError("unexpected")

    cog._reconcile_once = _boom

    await cog.reconcile_entitlements.coro(cog)  # must not raise


async def test_reconcile_error_handler_restarts_the_loop():
    cog, _bot = _m3b_cog(object())
    restarted = []
    cog.reconcile_entitlements.restart = lambda: restarted.append(True)

    await cog._reconcile_error(RuntimeError("loop crashed"))

    assert restarted == [True]


# -- cog_load / cog_unload wiring -------------------------------------------


async def test_cog_load_starts_the_reconciliation_task(fake_pool):
    cog, bot = _m3b_cog(fake_pool)
    assert cog.reconcile_entitlements.is_running() is False

    await cog.cog_load()

    assert cog.reconcile_entitlements.is_running() is True
    cog.cog_unload()


def test_cog_unload_before_cog_load_is_a_safe_no_op(fake_pool):
    """Direct construction (every test above, and the whole pre-existing
    ?premiumadmin suite) never calls cog_load - cog_unload must still be a no-op
    rather than raise, since discord.py calls it on extension teardown
    regardless of whether cog_load's task was ever started."""
    cog, _bot = _m3b_cog(fake_pool)
    cog.cog_unload()  # must not raise


# ---------------------------------------------------------------------------
# M5: DM the bot owner on a sale/refund.
#
# A richer bot stand-in than _m3b_bot: a resolved owner (``owner_id`` set, so
# _resolve_owner_ids never needs application_info), a fake "owner user" whose
# .send() records every text (or raises, for the failure tests), and
# get_guild always missing (so DM text falls back to a bare id - the cache-
# miss branch _sale_scope_desc takes). Delivery is a fire-and-forget
# background task (Premium._dispatch_sale_dm): every test here calls
# _flush_owner_dms(cog) right after the write, to await whatever task(s) that
# write scheduled before asserting on the result.
# ---------------------------------------------------------------------------


class _FakeOwnerUser:
    def __init__(self, user_id, *, send_raises=None):
        self.id = user_id
        self.sent = []
        self._send_raises = send_raises

    async def send(self, text):
        if self._send_raises is not None:
            raise self._send_raises
        self.sent.append(text)


def _dm_bot(pool, *, application_id=APPLICATION_ID, stream_factory=None, owner_id=1, send_raises=None):
    bot = _m3b_bot(
        pool, application_id=application_id, stream_factory=stream_factory, owner_id=owner_id
    )
    owner_user = _FakeOwnerUser(owner_id, send_raises=send_raises)
    bot.owner_id = owner_id
    bot.get_user = lambda uid: owner_user if uid == owner_id else None

    async def fetch_user(uid):
        return owner_user

    bot.fetch_user = fetch_user
    bot.get_guild = lambda guild_id: None  # always a cache miss - the id-only DM branch
    return bot, owner_user


def _dm_cog(pool, **kwargs):
    bot, owner_user = _dm_bot(pool, **kwargs)
    return premium_cog.Premium(bot), bot, owner_user


async def _flush_owner_dms(cog):
    """Await every owner-DM background task in flight right now (M5's
    fire-and-forget dispatch), so a test can assert on the result
    deterministically instead of racing the event loop."""
    pending = list(cog._pending_owner_dms)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_new_entitlement_create_sends_exactly_one_owner_dm(fake_pool, monkeypatch):
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", 111)
    fake_pool.fetchrow_return = None  # no prior row - genuinely new
    fake_pool.fetch_return = []
    cog, bot, owner = _dm_cog(fake_pool)

    await cog.on_entitlement_create(_remote_entitlement())
    await _flush_owner_dms(cog)

    assert len(owner.sent) == 1
    assert owner.sent[0].startswith("New sale: Yasuho+ for server 42 (42)")


async def test_the_same_create_event_processed_twice_sends_only_one_dm(fake_pool):
    """The event arrives, then (a gateway reconnect replay, or Discord
    simply delivering it twice) arrives again: the SECOND call's own
    before/after read now finds the row this module's own write already
    made durable the first time, so classify_entitlement_transition reads
    "not new" and sends nothing - no in-memory de-dup needed, the database
    IS the de-dup."""
    fake_pool.fetch_return = []
    cog, bot, owner = _dm_cog(fake_pool)

    fake_pool.fetchrow_return = None  # first delivery: no prior row
    await cog.on_entitlement_create(_remote_entitlement())
    await _flush_owner_dms(cog)

    fake_pool.fetchrow_return = {"deleted": False}  # second delivery: now it exists
    await cog.on_entitlement_create(_remote_entitlement())
    await _flush_owner_dms(cog)

    assert len(owner.sent) == 1


async def test_refund_sends_one_ended_dm_then_a_replayed_delete_sends_none(fake_pool, monkeypatch):
    monkeypatch.setattr(premium_cog.premium, "YASUHO_PLUS_SKU", 111)
    fake_pool.fetch_return = []
    cog, bot, owner = _dm_cog(fake_pool)

    fake_pool.fetchrow_return = {"deleted": False}  # it existed, active
    await cog.on_entitlement_update(_remote_entitlement(deleted=True))  # the refund
    await _flush_owner_dms(cog)

    assert len(owner.sent) == 1
    assert owner.sent[0].startswith("Ended (refund or cancellation): Yasuho+")

    fake_pool.fetchrow_return = {"deleted": True}  # already deleted - the replay
    await cog.on_entitlement_delete(_remote_entitlement())
    await _flush_owner_dms(cog)

    assert len(owner.sent) == 1  # unchanged - no second DM


async def test_out_of_order_delete_before_its_create_sends_no_dm(fake_pool):
    """classify_entitlement_transition's own "never granted" case: the
    DELETE lands first (no row yet), then the late CREATE still carries
    deleted=True (the OR-preserving write) - at no point does the owner see
    an entitlement that was ever actually active, so no DM at any point."""
    fake_pool.fetch_return = []
    cog, bot, owner = _dm_cog(fake_pool)

    fake_pool.fetchrow_return = None  # no row yet
    await cog.on_entitlement_delete(_remote_entitlement())
    await _flush_owner_dms(cog)

    fake_pool.fetchrow_return = {"deleted": True}  # the late create's own prior read
    await cog.on_entitlement_create(_remote_entitlement(deleted=False))
    await _flush_owner_dms(cog)

    assert owner.sent == []


async def test_test_mode_entitlement_dm_is_labelled_test(fake_pool):
    fake_pool.fetchrow_return = None
    fake_pool.fetch_return = []
    cog, bot, owner = _dm_cog(fake_pool)

    await cog.on_entitlement_create(
        _remote_entitlement(type=discord.EntitlementType.test_mode_purchase.value)
    )
    await _flush_owner_dms(cog)

    assert len(owner.sent) == 1
    assert owner.sent[0].startswith("TEST - New sale")


async def test_a_grant_never_dispatches_an_owner_dm(fake_pool, make_context):
    """?premiumadmin grant never touches premium_entitlements at all, so no
    transition is ever derived for it - confirmed here by checking the
    cog's own in-flight-DM set stays empty across a real grant write."""
    fake_pool.fetchrow_return = {"id": 9}
    fake_pool.fetch_return = []
    cog, _bot = _cog(fake_pool)
    ctx = make_context(author_id=1)

    await cog.premium_grant_server.callback(cog, ctx, 123, rest="")

    assert cog._pending_owner_dms == set()


async def test_dm_send_failure_is_logged_and_swallowed_not_raised(fake_pool, caplog):
    fake_pool.fetchrow_return = None
    fake_pool.fetch_return = []
    resp = types.SimpleNamespace(status=403, reason="Forbidden")
    cog, bot, owner = _dm_cog(
        fake_pool, send_raises=discord.Forbidden(resp, "Cannot send messages to this user")
    )

    with caplog.at_level("WARNING", logger=premium_cog.log.name):
        await cog.on_entitlement_create(_remote_entitlement())  # must not raise
        await _flush_owner_dms(cog)

    assert owner.sent == []
    assert any(
        "PREMIUM-OWNER-DM-FAILED reason=forbidden" in r.message for r in caplog.records
    )


async def test_an_unexpected_send_error_is_also_logged_and_swallowed(fake_pool, caplog):
    fake_pool.fetchrow_return = None
    fake_pool.fetch_return = []
    cog, bot, owner = _dm_cog(fake_pool, send_raises=RuntimeError("network is down"))

    with caplog.at_level("ERROR", logger=premium_cog.log.name):
        await cog.on_entitlement_create(_remote_entitlement())  # must not raise
        await _flush_owner_dms(cog)

    assert owner.sent == []
    assert any(
        "PREMIUM-OWNER-DM-FAILED reason=unexpected" in r.message for r in caplog.records
    )


# -- reconciliation-driven DMs: found-by-reconciliation / no DM on a re-read
# / storm collapse ------------------------------------------------------


class _ReconcileTable:
    """A minimal fake premium_entitlements store, just enough for
    ``tools.premium.reconcile`` plus ``bot.premium.load`` afterwards - the
    M5 reconciliation-DM tests only need write-then-read-back consistency,
    not the ordering/race guarantees tests/tools/test_premium.py's own
    ``_FakeEntitlementTable`` proves elsewhere."""

    def __init__(self, seed_rows=()):
        self.rows = {row["entitlement_id"]: dict(row) for row in seed_rows}
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if "SET deleted = TRUE" in query:
            (entitlement_id,) = args
            if entitlement_id not in self.rows:
                return "UPDATE 0"
            self.rows[entitlement_id]["deleted"] = True
            return "UPDATE 1"
        if len(args) == 11:
            args = args[:10]
        (
            entitlement_id, sku_id, scope_type, guild_id, user_id,
            entitlement_type, deleted, consumed, starts_at, ends_at,
        ) = args
        self.rows[entitlement_id] = {
            "entitlement_id": entitlement_id,
            "sku_id": sku_id,
            "scope_type": scope_type,
            "guild_id": guild_id,
            "user_id": user_id,
            "entitlement_type": entitlement_type,
            "deleted": deleted,
            "consumed": consumed,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "last_synced_at": None,
        }
        return "INSERT 0 1"

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if query == "SELECT entitlement_id FROM premium_entitlements":
            return [{"entitlement_id": rid} for rid in self.rows]
        if "FROM premium_grants" in query:
            return []
        if "FROM premium_entitlements" in query and "WHERE deleted = FALSE" in query:
            return [dict(row) for row in self.rows.values() if not row["deleted"]]
        raise AssertionError(f"unexpected query: {query}")


def _seeded_row(entitlement_id, **overrides):
    row = dict(
        entitlement_id=entitlement_id,
        sku_id=111,
        scope_type="guild",
        guild_id=entitlement_id * 10,
        user_id=None,
        entitlement_type=2,
        deleted=False,
        consumed=False,
        starts_at=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc),
        ends_at=None,
    )
    row.update(overrides)
    return row


async def test_reconciliation_rereading_an_existing_row_sends_no_dm():
    table = _ReconcileTable(seed_rows=[_seeded_row(1)])

    async def _stream(**kwargs):
        yield _remote_entitlement(id=1, guild_id=10)

    cog, bot, owner = _dm_cog(table, stream_factory=_stream)

    await cog._reconcile_once()
    await _flush_owner_dms(cog)

    assert owner.sent == []


async def test_reconciliation_finds_a_missed_purchase_and_sends_one_labelled_dm():
    """A row this pass has NEVER recorded before - the "missed gateway
    event" scenario - is one DM, clearly labelled so the owner knows it
    came from the safety net, not the real-time path."""
    table = _ReconcileTable()  # empty - this id was never recorded

    async def _stream(**kwargs):
        yield _remote_entitlement(id=1, guild_id=10)

    cog, bot, owner = _dm_cog(table, stream_factory=_stream)

    await cog._reconcile_once()
    await _flush_owner_dms(cog)

    assert len(owner.sent) == 1
    assert "(found by reconciliation)" in owner.sent[0]


async def test_reconciliation_storm_collapses_to_one_summary_dm():
    """More than RECONCILE_DM_STORM_THRESHOLD transitions in one pass (here:
    every one of them brand new) - ONE summary DM, never one per row."""
    table = _ReconcileTable()
    many = premium_cog.RECONCILE_DM_STORM_THRESHOLD + 1

    async def _stream(**kwargs):
        for index in range(many):
            yield _remote_entitlement(id=index + 1, guild_id=(index + 1) * 10)

    cog, bot, owner = _dm_cog(table, stream_factory=_stream)

    await cog._reconcile_once()
    await _flush_owner_dms(cog)

    assert len(owner.sent) == 1
    assert f"{many} changes" in owner.sent[0]
    assert "one per transition" not in owner.sent[0]  # sanity: not an accidental echo



def test_an_api_test_entitlement_is_labelled_test_by_its_missing_start():
    """?premiumadmin testbuy creates an entitlement with the SKU's ordinary
    type (here application subscription) and no starts_at: the DM must still
    say TEST."""
    row = {
        "sku_id": 111,
        "scope_type": "guild",
        "guild_id": 42,
        "user_id": None,
        "entitlement_type": discord.EntitlementType.application_subscription.value,
        "starts_at": None,
        "ends_at": None,
    }
    assert premium_cog._is_test_entitlement(row["entitlement_type"], None)
    assert not premium_cog._is_test_entitlement(
        row["entitlement_type"],
        datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc),
    )
    assert premium_cog._is_test_entitlement(
        discord.EntitlementType.test_mode_purchase,
        datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc),
    )

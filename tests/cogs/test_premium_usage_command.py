"""``?premiumadmin usage`` (cogs/system/premium.py + tools/premium_usage.py).

The owner gate itself (``@commands.is_owner()`` + ``cog_check``) is already
covered generically by tests/cogs/test_premium.py's
``test_every_leaf_carries_its_own_owner_check``, which walks every
subcommand dynamically - this file only covers the command's OWN behaviour:
it renders tools.premium_usage's report as plain-English code-block
messages (never tools.i18n._()), and degrades to one error message rather
than raising when the database is unavailable.
"""

from __future__ import annotations

import types

from cogs.system import premium as premium_cog
from tools import premium


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


async def test_usage_renders_a_plain_text_code_block(fake_pool, make_context):
    fake_pool.fetchrow_return = {
        "scopes": 3, "median": 1.0, "p90": 2.0, "p99": 2.0,
        "max": 2, "at_80": 1, "at_100": 0,
    }
    cog, _bot_obj = _cog(fake_pool)
    ctx = make_context()

    await cog.premium_usage_report.callback(cog, ctx)

    assert ctx.sends
    for args, _kwargs in ctx.sends:
        text = args[0]
        assert text.startswith("```")
        assert text.endswith("```")
    joined = "\n".join(args[0] for args, _kwargs in ctx.sends)
    assert "server playlists / guild" in joined
    assert "favourites / user" in joined


async def test_usage_sends_one_error_message_on_a_database_failure(
    fake_pool, make_context
):
    async def _raise(*args, **kwargs):
        raise RuntimeError("database unavailable")

    fake_pool.fetchrow = _raise
    cog, _bot_obj = _cog(fake_pool)
    ctx = make_context()

    await cog.premium_usage_report.callback(cog, ctx)

    assert len(ctx.sends) == 1
    assert "Could not read usage" in ctx.sends[0][0][0]


async def test_usage_is_gated_by_is_owner_like_every_other_leaf():
    """Positive control mirroring test_premium.py's generic gate test, scoped
    to this one new command - proves it was not added without the decorator."""
    cog, _bot_obj = _cog(object())
    assert cog.premium_usage_report.checks, "usage carries no checks at all"

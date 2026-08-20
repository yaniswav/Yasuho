"""A member without the permission is refused on BOTH invocation paths.

These drive the REAL command objects built by discord.py after ``add_cog``, and
call the two functions discord.py itself calls to decide whether an invocation
may proceed:

* prefix path - ``Command.can_run(ctx)``. This is exactly what ``Command.prepare``
  awaits before running a callback, and for a subcommand it is the ONLY check
  gate: ``HybridGroup`` forces ``invoke_without_command = True``, so
  ``Group.invoke`` skips the group's own ``prepare()`` and dispatches straight to
  ``ctx.invoked_subcommand.invoke(ctx)``.
* app path - ``HybridAppCommand._check_can_run(interaction)``, awaited by
  ``_invoke_with_namespace`` before the callback. For a subcommand it consults
  ``self.wrapped.checks``, i.e. the subcommand's own ext checks. The parent
  group's checks are never consulted on this path either.

Together those two facts are why a check on the parent group protects nothing,
and why every command below must carry its own. Removing any
``@commands.has_permissions`` decorator these pin makes the "member is refused"
cases fail; removing a ``@commands.guild_only`` makes the DM case fail.

The structural counterpart (a NEW subcommand landing without a check) lives in
tests/test_hybrid_gating_hygiene.py.
"""

import types

import discord
import pytest
from discord.ext import commands

from cogs.config.rooms import TemporaryRooms
from cogs.moderation.automod import AutoMod
from cogs.moderation.modlog import ModLog

# (qualified command name, the permission that must be required)
PRIVILEGED = [
    ("modlog set", "manage_guild"),
    ("modlog disable", "manage_guild"),
    ("modlog status", "manage_guild"),
    ("automod links", "manage_guild"),
    ("automod invites", "manage_guild"),
    ("automod spam", "manage_guild"),
    ("automod panel", "manage_guild"),
    ("autoroom list", "manage_channels"),
]


class _NullPool:
    """Enough of an asyncpg pool for the cogs' load-time reads to no-op.

    TemporaryRooms rebuilds its hub index on load; without this it logs a
    swallowed failure on every test. No check predicate touches the DB.
    """

    async def fetch(self, *args, **kwargs):
        return []

    async def fetchrow(self, *args, **kwargs):
        return None

    async def fetchval(self, *args, **kwargs):
        return None

    async def execute(self, *args, **kwargs):
        return "SELECT 0"


async def _bot():
    """A real Bot with the three cogs attached, exactly as production builds them."""
    bot = commands.Bot(command_prefix="?", intents=discord.Intents.none())
    bot.db_pool = _NullPool()
    await bot.add_cog(ModLog(bot))
    await bot.add_cog(AutoMod(bot))
    await bot.add_cog(TemporaryRooms(bot))
    return bot


def _ctx(bot, *, guild=True, **perms):
    """A Context stand-in carrying only what the check predicates read."""
    permissions = discord.Permissions.none()
    if perms:
        permissions.update(**perms)
    return types.SimpleNamespace(
        bot=bot,
        guild=types.SimpleNamespace(id=1) if guild else None,
        permissions=permissions,
        author=types.SimpleNamespace(id=1234),
        command=None,
        interaction=None,
    )


def _interaction(bot, ctx):
    """``_check_can_run`` reads exactly these two attributes off the interaction."""
    return types.SimpleNamespace(client=bot, _baton=ctx)


# ---------------------------------------------------------------------------
# The member without the permission: refused on both paths.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("qualified,permission", PRIVILEGED)
async def test_member_without_permission_is_refused_on_prefix_path(
    qualified, permission
):
    bot = await _bot()
    command = bot.get_command(qualified)
    assert command is not None, qualified

    with pytest.raises(commands.MissingPermissions):
        await command.can_run(_ctx(bot))


@pytest.mark.parametrize("qualified,permission", PRIVILEGED)
async def test_member_without_permission_is_refused_on_app_path(
    qualified, permission
):
    bot = await _bot()
    command = bot.get_command(qualified)
    app_command = command.app_command
    assert isinstance(app_command, discord.app_commands.Command), qualified

    ctx = _ctx(bot)
    with pytest.raises(commands.MissingPermissions):
        await app_command._check_can_run(_interaction(bot, ctx))


# ---------------------------------------------------------------------------
# The moderator who does hold it: still allowed on both paths.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("qualified,permission", PRIVILEGED)
async def test_moderator_passes_on_prefix_path(qualified, permission):
    bot = await _bot()
    command = bot.get_command(qualified)

    assert await command.can_run(_ctx(bot, **{permission: True})) is True


@pytest.mark.parametrize("qualified,permission", PRIVILEGED)
async def test_moderator_passes_on_app_path(qualified, permission):
    bot = await _bot()
    command = bot.get_command(qualified)
    ctx = _ctx(bot, **{permission: True})

    assert await command.app_command._check_can_run(_interaction(bot, ctx)) is True


# ---------------------------------------------------------------------------
# guild_only: these callbacks all dereference ctx.guild.id.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("qualified,permission", PRIVILEGED)
async def test_refused_outside_a_guild_even_holding_the_permission(
    qualified, permission
):
    """Pins @commands.guild_only on the subcommand itself.

    A DM never actually reports manage_guild, so this constructs the permission
    anyway: the point is that guild_only - not the luck of DM permissions - is
    what stops a callback that would AttributeError on ``ctx.guild.id``.
    """
    bot = await _bot()
    command = bot.get_command(qualified)

    with pytest.raises(commands.NoPrivateMessage):
        await command.can_run(_ctx(bot, guild=False, **{permission: True}))


# ---------------------------------------------------------------------------
# The mechanic that made all of the above necessary.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("group_name", ["modlog", "automod", "autoroom"])
async def test_hybrid_group_never_runs_its_own_checks_for_a_subcommand(group_name):
    """Document the discord.py behaviour this whole module defends against.

    ``invoke_without_command`` is forced True on every hybrid group, which is
    what makes ``Group.invoke`` skip the group's ``prepare()`` (its check gate)
    whenever a subcommand matched. If a future discord.py stopped forcing this,
    parent checks would start applying on the prefix path - good news, but the
    slash path would still ignore them, so the per-subcommand checks stay.
    """
    bot = await _bot()
    group = bot.get_command(group_name)

    assert isinstance(group, commands.HybridGroup)
    assert group.invoke_without_command is True
    # The group does carry its own checks; they simply do not reach subcommands.
    assert group.checks


async def test_subcommand_checks_are_independent_of_the_parent():
    """A subcommand's ``checks`` list does not inherit the parent's entries."""
    bot = await _bot()
    group = bot.get_command("modlog")
    subcommand = bot.get_command("modlog set")

    assert subcommand.checks
    assert not set(map(id, group.checks)) & set(map(id, subcommand.checks))

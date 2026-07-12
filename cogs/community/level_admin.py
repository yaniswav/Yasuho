"""Admin XP tools (leveling L5): the ``/xp`` group.

A Manage-Server admin adjusts a member's lifetime XP directly - ``give`` /
``take`` / ``set`` a member's total, ``reset`` one member (single confirm), or
``resetall`` for the whole guild (double confirm: a danger button THEN typing
the server name in a modal). Every mutation floors XP at 0, recomputes the
member's level, and routes a level change through the Leveling cog's reward +
announce seam (``apply_admin_xp_change``): a level UP grants reward roles and
announces like an organic level-up, a level DOWN reconciles roles (replace mode
recomputes the tier; stack mode keeps earned roles - the documented convention).

Scale: these are RARE, explicit human actions (an admin typing a command), never
a hot path, so each does a plain read-then-write with no caching - the SCALE
STORY is simply "there is nothing to scale here". The one guild-wide operation
(``resetall``) is a pair of indexed DELETEs behind a two-step confirmation, so it
cannot fire by accident. Admin edits deliberately do NOT touch xp_period (periods
track ORGANIC activity only - see schema.sql); only ``resetall`` wipes xp_period,
and only because it is nuking the whole guild's history on purpose.

Period rollups: an XP grant on the hot path writes both the lifetime `levels`
row AND the weekly/monthly xp_period rows; an admin adjustment writes ONLY
`levels`, so a boosted or docked total never distorts a period leaderboard.

Typography rule: ASCII '-' and '...' only. No em/en dashes or fancy ellipsis.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from tools import interactions, level_admin, leveling
from tools.formats import random_colour
from tools.i18n import _, ngettext
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Confirmation surfaces (reset: one button; resetall: button + name modal)
# ----------------------------------------------------------------------
class _ResetConfirmView(AuthorView):
    """Single Confirm/Cancel prompt for ``/xp reset`` (one member).

    Author-gated: only the admin who ran the command may confirm. Both buttons
    are built in ``__init__`` (which runs in the command's task, where the
    invoker's locale is set) so their labels localize, unlike a class-level
    ``@discord.ui.button`` decorator whose label is frozen at import.
    """

    def __init__(self, cog, author_id, member, *, timeout=60):
        super().__init__(
            author_id, timeout=timeout, deny_message="This prompt isn't for you."
        )
        self.cog = cog
        self.member = member
        confirm = discord.ui.Button(
            label=_("Reset XP"), style=discord.ButtonStyle.danger
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(
            label=_("Cancel"), style=discord.ButtonStyle.secondary
        )
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def _confirm(self, interaction):
        try:
            old_xp = await self.cog._perform_reset(
                interaction.guild, self.member, interaction.channel
            )
            await interaction.response.edit_message(
                content=_("Reset {member}'s XP - they had **{xp} XP**.").format(
                    member=self.member.mention, xp=old_xp
                ),
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("XP reset confirm failed")
            try:
                await interaction.response.edit_message(
                    content=_("Something went wrong."), embed=None, view=None
                )
            except discord.HTTPException:
                pass
        finally:
            self.stop()

    async def _cancel(self, interaction):
        await interaction.response.edit_message(
            content=_("Cancelled - no XP was changed."), embed=None, view=None
        )
        self.stop()


class _ResetAllModal(LocaleModal):
    """Second gate for ``/xp resetall``: the admin must type the server name.

    Opened from the danger button below (a component interaction, so this works
    for a prefix invocation too - the button click carries the interaction the
    modal needs). Reuses the pure name check so a fat-fingered name never wipes a
    guild's XP.
    """

    def __init__(self, cog, guild, owner):
        super().__init__(title=_("Confirm full XP reset"))
        self.cog = cog
        self.guild = guild
        self.owner = owner
        self.name_input = discord.ui.TextInput(
            label=_("Type the server name to confirm"),
            placeholder=guild.name[:100],
            required=True,
            max_length=100,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction):
        try:
            if not level_admin.confirm_name_matches(
                self.name_input.value, self.guild.name
            ):
                await interaction.response.send_message(
                    _("That doesn't match the server name - nothing was reset."),
                    ephemeral=True,
                )
                return

            count = await self.cog._perform_reset_all(self.guild.id)
            self.owner.stop()
            # Strip the now-spent buttons off the original panel (best effort).
            if self.owner.message is not None:
                try:
                    await self.owner.message.edit(view=None)
                except discord.HTTPException:
                    pass
            members = ngettext(
                "{count} member's XP", "{count} members' XP", count
            ).format(count=count)
            await interaction.response.send_message(
                _(
                    "Reset all XP in **{guild}**: wiped {members} and every "
                    "period leaderboard."
                ).format(guild=self.guild.name, members=members)
            )
        except Exception:
            log.exception("Full XP reset failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong resetting XP.")
            )


class _ResetAllView(AuthorView):
    """First gate for ``/xp resetall``: a danger button that opens the modal."""

    def __init__(self, cog, author_id, guild, *, timeout=60):
        super().__init__(
            author_id, timeout=timeout, deny_message="This prompt isn't for you."
        )
        self.cog = cog
        self.guild = guild
        reset = discord.ui.Button(
            label=_("Reset ALL XP"), style=discord.ButtonStyle.danger
        )
        reset.callback = self._open_modal
        cancel = discord.ui.Button(
            label=_("Cancel"), style=discord.ButtonStyle.secondary
        )
        cancel.callback = self._cancel
        self.add_item(reset)
        self.add_item(cancel)

    async def _open_modal(self, interaction):
        await interaction.response.send_modal(
            _ResetAllModal(self.cog, self.guild, self)
        )

    async def _cancel(self, interaction):
        await interaction.response.edit_message(
            content=_("Cancelled - no XP was changed."), embed=None, view=None
        )
        self.stop()


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class LevelAdmin(commands.Cog):
    """The /xp admin group: give, take, set, reset, resetall."""

    def __init__(self, bot):
        self.bot = bot

    # -- DB helpers ------------------------------------------------------
    async def _current_xp(self, guild_id, user_id):
        xp = await self.bot.db_pool.fetchval(
            "SELECT xp FROM levels WHERE guild_id = $1 AND user_id = $2;",
            guild_id,
            user_id,
        )
        return xp or 0

    async def _write_xp(self, guild_id, user_id, new_xp):
        """Upsert an ABSOLUTE XP total (the value is already floored/validated).

        Writes ONLY the lifetime `levels` row - never xp_period, so an admin
        adjustment never distorts a weekly/monthly leaderboard (periods track
        organic activity only).
        """
        await self.bot.db_pool.execute(
            """
            INSERT INTO levels (guild_id, user_id, xp)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = $3;
            """,
            guild_id,
            user_id,
            new_xp,
        )

    async def _route_change(self, ctx, member, old_xp, new_xp):
        """Route a committed XP change through the Leveling reward/announce seam.

        The XP write has already landed; this only reconciles roles and (on a
        level up) announces. A missing or failing Leveling cog must never undo
        the admin's action, so it is looked up defensively and wrapped.
        """
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            return
        try:
            await leveling_cog.apply_admin_xp_change(
                guild=ctx.guild,
                member=member,
                channel=ctx.channel,
                old_xp=old_xp,
                new_xp=new_xp,
            )
        except Exception:
            log.exception(
                "Admin XP reward/announce routing failed for %s", member.id
            )

    def _change_embed(self, member, old_xp, new_xp):
        """A confirmation embed showing the XP delta and any level movement."""
        old_level = leveling.level_for_xp(old_xp)
        new_level = leveling.level_for_xp(new_xp)
        embed = discord.Embed(title=_("XP updated"), colour=random_colour())
        embed.description = _(
            "{member} now has **{xp} XP** (was {old_xp})."
        ).format(member=member.mention, xp=new_xp, old_xp=old_xp)
        if new_level > old_level:
            embed.add_field(
                name=_("Level up"),
                value=_("Level {old} -> **{new}**").format(
                    old=old_level, new=new_level
                ),
            )
        elif new_level < old_level:
            embed.add_field(
                name=_("Level down"),
                value=_("Level {old} -> **{new}**").format(
                    old=old_level, new=new_level
                ),
            )
        else:
            embed.add_field(
                name=_("Level"),
                value=_("**{level}** (unchanged)").format(level=new_level),
            )
        return embed

    async def _adjust(self, ctx, member, action, amount):
        """Shared body for give/take/set: mutate, route, and confirm."""
        old_xp = await self._current_xp(ctx.guild.id, member.id)
        new_xp = level_admin.resolve_new_xp(action, old_xp, amount)
        await self._write_xp(ctx.guild.id, member.id, new_xp)
        await self._route_change(ctx, member, old_xp, new_xp)
        await ctx.send(
            embed=self._change_embed(member, old_xp, new_xp),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _perform_reset(self, guild, member, channel):
        """Zero one member's lifetime XP (the /xp reset confirm action).

        Deletes their `levels` row entirely (a true reset drops them off the
        board) and routes the level-DOWN reconcile so replace-mode tiers are
        recomputed. Leaves xp_period untouched: a single reset targets the
        lifetime total, not the member's organic period history. Returns the XP
        they held, for the confirmation message.
        """
        old_xp = await self._current_xp(guild.id, member.id)
        await self.bot.db_pool.execute(
            "DELETE FROM levels WHERE guild_id = $1 AND user_id = $2;",
            guild.id,
            member.id,
        )
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is not None:
            try:
                await leveling_cog.apply_admin_xp_change(
                    guild=guild,
                    member=member,
                    channel=channel,
                    old_xp=old_xp,
                    new_xp=0,
                )
            except Exception:
                log.exception(
                    "Admin XP reset routing failed for %s", member.id
                )
        return old_xp

    async def _perform_reset_all(self, guild_id):
        """Wipe EVERY member's lifetime XP AND every period rollup for a guild.

        The nuclear ``/xp resetall`` action (behind the two-step confirm). Two
        indexed DELETEs; roles are deliberately NOT reconciled here - a mass
        role sweep across the whole guild is a different, much heavier operation
        and out of scope for a reset (see the cog docstring / residual risks).
        Returns the number of member records wiped, for the confirmation.
        """
        count = await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) FROM levels WHERE guild_id = $1;", guild_id
        )
        await self.bot.db_pool.execute(
            "DELETE FROM levels WHERE guild_id = $1;", guild_id
        )
        await self.bot.db_pool.execute(
            "DELETE FROM xp_period WHERE guild_id = $1;", guild_id
        )
        return count or 0

    # -- command group ---------------------------------------------------
    @commands.hybrid_group(name="xp")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def xp(self, ctx):
        """Admin XP tools: give, take, set, or reset a member's XP."""
        if ctx.invoked_subcommand is None:
            embed = discord.Embed(
                title=_("Admin XP tools"),
                description=_(
                    "- `/xp give <member> <amount>` - add XP\n"
                    "- `/xp take <member> <amount>` - remove XP\n"
                    "- `/xp set <member> <amount>` - set an exact total\n"
                    "- `/xp reset <member>` - reset one member (confirm)\n"
                    "- `/xp resetall` - reset the whole server (double confirm)"
                ),
                colour=random_colour(),
            )
            await ctx.send(embed=embed)

    @xp.command(name="give")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        member="The member to give XP to.",
        amount="How much XP to add (1 to 1000000).",
    )
    async def xp_give(self, ctx, member: discord.Member, amount: int):
        """Give a member XP (adds to their current total)."""
        ok, _reason = level_admin.validate_adjust_amount(amount)
        if not ok:
            await ctx.send(
                _("The amount must be between {min} and {max}.").format(
                    min=level_admin.MIN_ADJUST_AMOUNT,
                    max=level_admin.MAX_ADJUST_AMOUNT,
                )
            )
            return
        await self._adjust(ctx, member, level_admin.GIVE, amount)

    @xp.command(name="take")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        member="The member to take XP from.",
        amount="How much XP to remove (1 to 1000000).",
    )
    async def xp_take(self, ctx, member: discord.Member, amount: int):
        """Take XP from a member (floors at 0)."""
        ok, _reason = level_admin.validate_adjust_amount(amount)
        if not ok:
            await ctx.send(
                _("The amount must be between {min} and {max}.").format(
                    min=level_admin.MIN_ADJUST_AMOUNT,
                    max=level_admin.MAX_ADJUST_AMOUNT,
                )
            )
            return
        await self._adjust(ctx, member, level_admin.TAKE, amount)

    @xp.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        member="The member whose XP to set.",
        amount="The exact XP total to set (0 to 10000000).",
    )
    async def xp_set(self, ctx, member: discord.Member, amount: int):
        """Set a member's XP to an exact total."""
        ok, _reason = level_admin.validate_set_xp(amount)
        if not ok:
            await ctx.send(
                _("The XP total must be between {min} and {max}.").format(
                    min=level_admin.MIN_SET_XP, max=level_admin.MAX_SET_XP
                )
            )
            return
        await self._adjust(ctx, member, level_admin.SET, amount)

    @xp.command(name="reset")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(member="The member whose XP to reset to 0.")
    async def xp_reset(self, ctx, member: discord.Member):
        """Reset one member's XP to 0 (asks for confirmation first)."""
        embed = discord.Embed(
            title=_("Reset this member's XP?"),
            description=_(
                "This resets {member}'s XP to **0** and removes them from the "
                "leaderboard. This can't be undone."
            ).format(member=member.mention),
            colour=random_colour(),
        )
        view = _ResetConfirmView(self, ctx.author.id, member)
        view.message = await ctx.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @xp.command(name="resetall")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def xp_resetall(self, ctx):
        """Reset EVERY member's XP for this server (double confirmation)."""
        embed = discord.Embed(
            title=_("Reset ALL XP in this server?"),
            description=_(
                "This wipes **every** member's XP and every weekly/monthly "
                "leaderboard for **{guild}**. This can't be undone.\n\n"
                "Press the button below, then type the server name to confirm."
            ).format(guild=ctx.guild.name),
            colour=random_colour(),
        )
        view = _ResetAllView(self, ctx.author.id, ctx.guild)
        view.message = await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(LevelAdmin(bot))

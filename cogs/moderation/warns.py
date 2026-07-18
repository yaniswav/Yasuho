"""Warn family: the warn/delwarn/warnings/warninfo command surface.

House concern-split of the moderation package. This module owns the warn-family
commands (``warn``, ``delwarn``, the ``warnings`` view/config group, and
``warninfo``), the escalation hook wiring the ``warn`` command fires, the
paginated ``WarningsView`` those commands render, and the per-command
persistence helper ``remove_warn_case``. It joins the existing
``cogs/moderation/warn_config.py`` (the ``/warnings config`` control panel and
the escalation presentation helpers) and ``tools/warn_escalation.py`` (the pure
policy engine) as the complete warns package.

Cog split: discord.py hybrid commands are cog-bound, so the warn family lives on
its own :class:`Warns` cog. ``cogs/moderation/moderation.py`` keeps the member
actions (kick/ban/mute/...), purge/clean and the case/history commands on the
:class:`~cogs.moderation.moderation.Moderation` cog and registers BOTH cogs from
its ``setup``; this module has no ``setup`` of its own. The commands' names,
permissions, descriptions and slash-tree positions are unchanged - they are root
commands, so a second cog does not move them in the tree.

Import direction is one-way: this module imports ``warn_config`` (and tools),
never the moderation cog, so there is no cycle. ``moderation.py`` re-exports
:class:`WarningsView` for backward compatibility with existing imports.

The ``_post_modlog`` helper is a deliberate verbatim duplicate of the same
stateless helper on the Moderation cog: the moved ``warn`` command calls
``self._post_modlog`` and must stay AST-verbatim, so the cog it now lives on
carries its own copy rather than reaching across cogs (which would rewrite the
command body). It is the minimal seam, not shared state.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the fancy
ellipsis anywhere in this file.
"""

import logging

import discord
from discord.ext import commands

from cogs.moderation.warn_config import (
    WarnConfigPanel,
    escalation_dm,
    escalation_failure_notice,
    escalation_summary,
)
from tools import modactions, warn_escalation
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)

WARN_HISTORY_CAP = 100


class WarningsView(AuthorView):
    """Author-restricted, paginated list of a member's warn-cases.

    A dropdown selects a warn on the current page and the danger button removes
    it (deletes the case row and decrements the member's ``warns_count``).
    """

    def __init__(self, cog, guild, member, warns, author_id, *, per_page=10, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.cog = cog
        self.guild = guild
        self.member = member
        self.warns = list(warns)  # asyncpg Records, newest first
        self.per_page = per_page
        self.index = 0
        self.selected = None

        self.select = discord.ui.Select(
            placeholder=_("Select a warn to remove..."), row=0
        )
        self.select.callback = self._on_select
        self.add_item(self.select)
        self._rebuild()

    @property
    def page_count(self):
        if not self.warns:
            return 1
        return (len(self.warns) + self.per_page - 1) // self.per_page

    def _page_slice(self):
        start = self.index * self.per_page
        return self.warns[start : start + self.per_page]

    def _mod_text(self, moderator_id):
        mod = self.guild.get_member(moderator_id)
        return mod.mention if mod else f"<@{moderator_id}>"

    def embed(self):
        embed = discord.Embed(
            title=_("Warnings - {member}").format(member=self.member),
            colour=modactions.action_colour("warn"),
        )
        embed.set_thumbnail(url=self.member.display_avatar.url)

        page = self._page_slice()
        if not page:
            embed.description = _("No warnings on record.")
        else:
            lines = []
            for warn in page:
                reason = warn["reason"] or _("*No reason provided*")
                when = discord.utils.format_dt(warn["created_at"], "R")
                lines.append(
                    _("**Case #{case}** - {reason}\nby {mod} - {when}").format(
                        case=warn["case_number"],
                        reason=reason,
                        mod=self._mod_text(warn["moderator_id"]),
                        when=when,
                    )
                )
            embed.description = "\n\n".join(lines)

        embed.set_footer(
            text=_("Page {current}/{total} - {count} warn(s)").format(
                current=self.index + 1,
                total=self.page_count,
                count=len(self.warns),
            )
        )
        return embed

    def _rebuild(self):
        """Refresh the select options and button states for the current page."""
        page = self._page_slice()
        options = []
        for warn in page:
            reason = warn["reason"] or _("No reason")
            options.append(
                discord.SelectOption(
                    label=_("Case #{case}").format(case=warn["case_number"]),
                    description=reason[:100],
                    value=str(warn["case_number"]),
                )
            )

        if options:
            self.select.options = options
            self.select.disabled = False
        else:
            self.select.options = [
                discord.SelectOption(label=_("No warnings"), value="none")
            ]
            self.select.disabled = True

        self.selected = None
        self.remove_warn.disabled = True
        self.prev_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= self.page_count - 1

    async def _on_select(self, interaction):
        try:
            self.selected = int(self.select.values[0])
            self.remove_warn.disabled = False
            for option in self.select.options:
                option.default = option.value == self.select.values[0]
            await interaction.response.edit_message(view=self)
        except Exception:
            log.exception("Warnings select failed")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        _("Couldn't select that warn, please try again."),
                        ephemeral=True,
                    )
                except Exception:
                    log.exception("Warnings select failed")

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def prev_page(self, interaction, button):
        await self._turn(interaction, self.index - 1)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction, button):
        await self._turn(interaction, self.index + 1)

    async def _turn(self, interaction, index):
        try:
            self.index = max(0, min(index, self.page_count - 1))
            self._rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)
        except Exception:
            log.exception("Warnings pagination failed")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        _("Couldn't turn the page, please try again."), ephemeral=True
                    )
                except Exception:
                    log.exception("Warnings pagination failed")

    @discord.ui.button(label="Remove warn", style=discord.ButtonStyle.danger, row=1)
    async def remove_warn(self, interaction, button):
        if self.selected is None:
            return await interaction.response.send_message(
                _("Pick a warn from the dropdown first."), ephemeral=True
            )

        try:
            await self.cog.remove_warn_case(
                self.guild.id, self.member.id, self.selected
            )

            removed = self.selected
            self.warns = [
                w for w in self.warns if w["case_number"] != removed
            ]
            if self.index >= self.page_count:
                self.index = self.page_count - 1
            self._rebuild()
            await interaction.response.edit_message(
                embed=self.embed(), view=self
            )
        except Exception:
            log.exception("Failed to remove warn case")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        _("Couldn't remove that warn, please try again."),
                        ephemeral=True,
                    )
                except Exception:
                    log.exception("Failed to remove warn case")


class Warns(commands.Cog):
    """Warnings and warn-escalation commands."""

    def __init__(self, bot):
        self.bot = bot

    async def _post_modlog(self, guild, embed):
        """Funnel a mod-action embed to the guild's configured mod-log channel."""
        await modactions.funnel_action(self.bot, guild, embed)

    async def remove_warn_case(self, guild_id, user_id, case_number):
        """Delete a member's warn-case row and clamp their warns_count at 0.

        Owns the persistence the warnings UI used to run inline: removes the
        single ``warn`` case and decrements the running ``warns_count`` (floored
        at 0 via ``GREATEST(..., 0)``).
        """
        removed, _remaining = await modactions.remove_warn_case(
            self.bot.db_pool, guild_id, user_id, case_number
        )
        return bool(removed)

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(member="The member to check.")
    async def warninfo(self, ctx, member: discord.Member = None):
        """Show how many warns a member currently has."""

        if member is None:
            return await ctx.send_help(ctx.command)

        query = """

        SELECT warns_count FROM warns
        WHERE guild_id = $1 AND user_id = $2;

        """

        fetch = await self.bot.db_pool.fetchval(query, ctx.guild.id, member.id)

        if not fetch:
            return await ctx.send(
                _("{member} has no warns.").format(member=member.mention)
            )

        await ctx.send(
            _("{member} has {count} warn(s)").format(
                member=member.mention, count=fetch
            )
        )

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(member="The member to warn.", reason="Why they're being warned.")
    async def warn(self, ctx, member: discord.Member = None, *, reason: str = None):
        """Warn a member (escalates per this server's warn rules)."""

        if member is None:
            return await ctx.send_help(ctx.command)

        # Every warn is recorded as its own case for history/auditing, while the
        # warns_count row is the running total the escalation policy keys on.
        # record_warn owns both writes atomically and is shared with AutoMod, so
        # both surfaces count identically. The policy is per-guild configurable
        # (defaulting to kick at 3 for an unconfigured server).
        num, new_count = await modactions.record_warn(
            self.bot.db_pool,
            ctx.guild.id,
            member.id,
            ctx.author.id,
            reason,
        )

        policy, _default = await modactions.load_escalation_policy(
            self.bot.db_pool, ctx.guild.id
        )
        rule = warn_escalation.action_for_count(policy, new_count)

        embed = modactions.case_embed(num, "warn", member, ctx.author, reason)

        # No rule fires at this exact count: record the warn and show the total.
        if rule is None:
            embed.add_field(name=_("Warns"), value=str(new_count), inline=False)
            await ctx.send(embed=embed)
            await self._post_modlog(ctx.guild, embed)
            return

        # A threshold was crossed - apply its action. The action rides this warn
        # case (no separate case row, exactly like the historical auto-kick); a
        # failure degrades to a clear notice while the warn stays recorded.
        ok = await modactions.apply_escalation_action(
            self.bot, ctx.guild, member, rule
        )
        embed.add_field(
            name=_("Auto-action"),
            value=escalation_summary(new_count, rule),
            inline=False,
        )
        await ctx.send(embed=embed)
        await self._post_modlog(ctx.guild, embed)

        if not ok:
            await ctx.send(
                escalation_failure_notice(member.mention, new_count, rule)
            )
            return

        try:
            await member.send(escalation_dm(ctx.guild.name, new_count, rule))
        except Exception:
            log.exception("Failed to DM member after warn escalation")

    @commands.hybrid_command(aliases=["rmwarn", "removewarn"])
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(
        member="The member to remove warns from.", num="How many warns to remove (default 1)."
    )
    async def delwarn(self, ctx, member: discord.Member = None, num: int = 1):
        """Remove a warn from a member."""

        if member is None:
            return await ctx.send_help(ctx.command)

        if num < 1:
            return await ctx.send(_("The number of warns must be at least 1."))

        removed, remaining = await modactions.remove_latest_warns(
            self.bot.db_pool, ctx.guild.id, member.id, num
        )
        if not removed:
            return await ctx.send(
                _("{member} has no warns!").format(member=member.mention)
            )

        if remaining == 0:
            return await ctx.send(
                _("Removed all warns for {member}.").format(member=member.mention)
            )

        await ctx.send(
            _("Removed {num} warn(s) for {member}. [{remaining} warns]").format(
                num=removed, member=member.mention, remaining=remaining
            )
        )

    @commands.hybrid_group(
        name="warnings",
        aliases=["warns"],
        fallback="view",
        invoke_without_command=True,
    )
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @discord.app_commands.describe(member="Whose warnings to browse (defaults to you).")
    async def warnings(self, ctx, member: discord.Member = None):
        """Interactively browse and remove a member's warnings."""

        member = member or ctx.author

        rows = await self.bot.db_pool.fetch(
            "SELECT case_number, reason, moderator_id, created_at FROM cases "
            "WHERE guild_id = $1 AND user_id = $2 AND action = 'warn' "
            "ORDER BY case_number DESC LIMIT $3;",
            ctx.guild.id,
            member.id,
            WARN_HISTORY_CAP,
        )

        view = WarningsView(self, ctx.guild, member, rows, ctx.author.id)
        view.message = await ctx.send(embed=view.embed(), view=view)

    @warnings.command(name="config")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def warnings_config(self, ctx):
        """Configure this server's warn escalation rules (threshold -> action)."""

        # manage_guild-gated (the browse fallback keeps its kick_members gate;
        # invoke_without_command=True means a subcommand runs only its own
        # checks, so this needs manage_guild only - see the command-tree notes).
        policy, _default = await modactions.load_escalation_policy(
            self.bot.db_pool, ctx.guild.id
        )
        state = {
            "policy": policy,
            "pending_action": warn_escalation.TIMEOUT,
        }
        view = WarnConfigPanel(self, ctx.guild, ctx.author.id, state)
        view.message = await ctx.send(view=view)

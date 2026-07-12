"""Level-up no-XP zones and announce control (leveling L3): the ``/levelconfig``
admin group.

Two independent knobs, both consumed by cogs/community/leveling.py's on_message
hot path:

* NO-XP ZONES (``level_no_xp``): channels/categories and roles where messages
  never earn XP. This cog owns the table (add/remove/list, capped at
  ``tools.leveling.MAX_NO_XP_PER_GUILD``) and, after every write, calls
  ``Leveling.refresh_no_xp_snapshot`` so the change is live on the very next
  message - no restart, no reliance on cache eviction.
* ANNOUNCE CONTROL (``level_config.announce_mode`` / ``announce_channel_id`` /
  ``announce_template``, columns L1 already added): where and how a level-up is
  announced. This cog never writes those columns directly - it always goes
  through ``Leveling.set_announce_mode`` / ``set_announce_template`` (the same
  cross-cog seam cogs/config/settings.py uses for the enabled toggle), because
  the Leveling cog's ``_configs`` hot-path cache must stay in step with the DB.

Cross-cog seam, matching the house pattern (cogs/community/level_rewards.py,
cogs/config/settings.py): looked up by name via ``bot.get_cog("Leveling")``,
guarded so a missing/failing Leveling cog degrades to a friendly refusal rather
than a crash - this cog owns no hot path itself.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

from __future__ import annotations

import logging
import typing

import discord
from discord.ext import commands

from tools import leveling
from tools.formats import random_colour
from tools.i18n import N_, _
from tools.views import AuthorView

log = logging.getLogger(__name__)

# Localized reasons for validate_announce_template's short failure codes (that
# module has no i18n dependency, like every other tools/*.py pure decision
# engine - this cog is where the codes become user-facing text).
_TEMPLATE_ERRORS = {
    "empty": N_("The message can't be empty."),
    "malformed": N_(
        "That message has a stray '{' or '}' - check the placeholders."
    ),
    "unknown_placeholder": N_(
        "Only these placeholders are allowed: {user}, {level}, {guild}."
    ),
}


def _template_error_message(reason):
    if reason == "too_long":
        return _("The message is too long (max {max} characters).").format(
            max=leveling.MAX_ANNOUNCE_TEMPLATE_LEN
        )
    return _(_TEMPLATE_ERRORS.get(reason, _TEMPLATE_ERRORS["malformed"]))


def _describe_announce_mode(config):
    """One-line, human description of the guild's current announce_mode."""
    if config.announce_mode == "off":
        return _("Off - level-ups are never announced.")
    if config.announce_mode == "dm":
        return _("DM - the member is messaged directly.")
    if config.announce_mode == "fixed":
        if config.announce_channel_id:
            return _("Fixed channel: <#{channel_id}>").format(
                channel_id=config.announce_channel_id
            )
        return _(
            "Fixed channel (none set yet - falls back to the message's own "
            "channel)."
        )
    return _("Channel - announced where the message was sent.")


def _describe_announce_template(config):
    if config.announce_template:
        return _('Custom: "{template}"').format(template=config.announce_template)
    return _('Default: "{template}"').format(
        template=leveling.DEFAULT_ANNOUNCE_TEMPLATE
    )


async def _fetch_config(pool, guild_id):
    """This guild's LevelConfig, read fresh (bypassing the enabled gate that
    resolve_config applies - an admin configuring no-xp zones or announce
    settings before ever turning leveling ON must still see/edit them)."""
    row = await pool.fetchrow(
        "SELECT enabled, cooldown_seconds, xp_min, xp_max, announce_mode, "
        "announce_channel_id, announce_template FROM level_config "
        "WHERE guild_id = $1;",
        guild_id,
    )
    return leveling.LevelConfig.from_row(row) if row is not None else leveling.LevelConfig()


# ----------------------------------------------------------------------
# Admin UX: "remove a no-xp entry" picker
# ----------------------------------------------------------------------
class _RemoveNoXpSelect(discord.ui.Select):
    """Lists every configured no-xp entry so the admin can pick one to delete.

    One option per entry; the cap (MAX_NO_XP_PER_GUILD == 50) is above
    Discord's 25-option select limit, so only the first 25 are offered here -
    an admin with more than 25 entries removes the rest in a follow-up call,
    the same soft limitation the level_rewards picker would hit past its own
    (lower) cap.
    """

    def __init__(self, cog, guild, rows):
        self.cog = cog
        self.guild = guild
        options = []
        for kind, target_id in rows[:25]:
            if kind == leveling.NO_XP_CHANNEL:
                obj = guild.get_channel(target_id)
                label = _("Channel/category")
                desc = obj.name if obj is not None else _("Unknown (deleted)")
            else:
                obj = guild.get_role(target_id)
                label = _("Role")
                desc = obj.name if obj is not None else _("Unknown (deleted)")
            options.append(
                discord.SelectOption(
                    label=f"{label}: {desc}"[:100],
                    value=f"{kind}:{target_id}",
                    description=desc[:100],
                )
            )
        super().__init__(
            placeholder=_("Pick a no-XP zone to remove..."),
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction):
        try:
            kind, target_id_str = self.values[0].split(":")
            target_id = int(target_id_str)
            await self.cog.bot.db_pool.execute(
                "DELETE FROM level_no_xp WHERE guild_id = $1 AND kind = $2 "
                "AND target_id = $3;",
                self.guild.id,
                kind,
                target_id,
            )
            await self.cog.refresh_no_xp_cache(self.guild.id)
            await interaction.response.edit_message(
                content=_("Removed that no-XP zone."),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("No-XP zone remove select failed")
            await interaction.response.edit_message(
                content=_("Something went wrong."), view=None
            )
        finally:
            # Terminal action: the message no longer carries a view, so stop the
            # timer too (mirrors _RemoveRewardSelect - see leveling L2).
            self._owner.stop()


class _RemoveNoXpView(AuthorView):
    def __init__(self, cog, guild, author_id, rows, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        select = _RemoveNoXpSelect(cog, guild, rows)
        select._owner = self
        self.add_item(select)


# ----------------------------------------------------------------------
# CV2 cards
# ----------------------------------------------------------------------
def _no_xp_lines(guild, rows):
    """(channel_lines, role_lines) - rendered mention lines for the no-xp
    lists, resolving deleted targets to a placeholder rather than a broken
    mention."""
    channel_lines = []
    role_lines = []
    for kind, target_id in rows:
        if kind == leveling.NO_XP_CHANNEL:
            channel = guild.get_channel(target_id)
            text = channel.mention if channel is not None else f"`{target_id}` " + _(
                "(deleted)"
            )
            channel_lines.append(f"- {text}")
        else:
            role = guild.get_role(target_id)
            text = role.mention if role is not None else f"`{target_id}` " + _(
                "(deleted)"
            )
            role_lines.append(f"- {text}")
    return channel_lines, role_lines


class NoXpListView(discord.ui.LayoutView):
    """Single-page Components V2 card: every configured no-xp channel/category
    and role for this guild."""

    def __init__(self, guild, rows, *, timeout=180):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(guild, rows)

    def _build(self, guild, rows):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "## " + _("No-XP zones | {guild}").format(guild=guild.name)
            )
        )
        container.add_item(discord.ui.Separator())

        if not rows:
            container.add_item(
                discord.ui.TextDisplay(
                    _(
                        "No no-XP zones configured yet. Use `/levelconfig noxp "
                        "add` to mute a channel, category, or role."
                    )
                )
            )
        else:
            channel_lines, role_lines = _no_xp_lines(guild, rows)
            if channel_lines:
                container.add_item(
                    discord.ui.TextDisplay(
                        _("**Channels & categories**\n{lines}").format(
                            lines="\n".join(channel_lines)
                        )
                    )
                )
            if role_lines:
                if channel_lines:
                    container.add_item(discord.ui.Separator())
                container.add_item(
                    discord.ui.TextDisplay(
                        _("**Roles**\n{lines}").format(lines="\n".join(role_lines))
                    )
                )
        self.add_item(container)


class LevelConfigOverviewView(discord.ui.LayoutView):
    """Single-page Components V2 landing card: no-xp zones + announce settings."""

    def __init__(self, guild, rows, config, *, timeout=180):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(guild, rows, config)

    def _build(self, guild, rows, config):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "## " + _("Level config | {guild}").format(guild=guild.name)
            )
        )
        container.add_item(discord.ui.Separator())

        channel_lines, role_lines = _no_xp_lines(guild, rows)
        no_xp_summary = (
            _("No no-XP zones configured. Use `/levelconfig noxp add`.")
            if not rows
            else "\n".join(channel_lines + role_lines)
        )
        container.add_item(
            discord.ui.TextDisplay(
                _("**No-XP zones ({count}/{max})**\n{summary}").format(
                    count=len(rows),
                    max=leveling.MAX_NO_XP_PER_GUILD,
                    summary=no_xp_summary,
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("**Announce mode**\n{mode}\n**Announce message**\n{template}").format(
                    mode=_describe_announce_mode(config),
                    template=_describe_announce_template(config),
                )
            )
        )
        self.add_item(container)


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class LevelConfigUI(commands.Cog):
    """No-XP zones + level-up announce control: the ``/levelconfig`` group."""

    def __init__(self, bot):
        self.bot = bot

    # -- cross-cog seam: keep the Leveling hot-path cache in step ------------
    async def refresh_no_xp_cache(self, guild_id):
        """Push the just-written level_no_xp rows into the Leveling cog's
        hot-path cache immediately (mirrors cogs/config/settings.py's
        ``bot.get_cog("Leveling").set_enabled`` call) - so the very next
        message in this guild sees the change, no restart needed. Tolerant of
        the Leveling cog not being loaded (never happens in production; keeps
        this cog testable in isolation)."""
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is not None:
            await leveling_cog.refresh_no_xp_snapshot(guild_id)

    # -- shared reads ----------------------------------------------------
    async def _fetch_no_xp_rows(self, guild_id):
        rows = await self.bot.db_pool.fetch(
            "SELECT kind, target_id FROM level_no_xp WHERE guild_id = $1;",
            guild_id,
        )
        return [(row["kind"], row["target_id"]) for row in rows]

    async def _send_overview(self, ctx):
        rows = await self._fetch_no_xp_rows(ctx.guild.id)
        config = await _fetch_config(self.bot.db_pool, ctx.guild.id)
        view = LevelConfigOverviewView(ctx.guild, rows, config)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _send_noxp_list(self, ctx):
        rows = await self._fetch_no_xp_rows(ctx.guild.id)
        view = NoXpListView(ctx.guild, rows)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    # -- command group -----------------------------------------------------
    @commands.hybrid_group(name="levelconfig", aliases=["lvlconfig"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig(self, ctx):
        """Manage no-XP zones and level-up announce settings."""
        if ctx.invoked_subcommand is None:
            await self._send_overview(ctx)

    # -- noxp subgroup -------------------------------------------------
    @levelconfig.group(name="noxp")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_noxp(self, ctx):
        """Manage channels/categories and roles that earn no XP."""
        if ctx.invoked_subcommand is None:
            await self._send_noxp_list(ctx)

    @levelconfig_noxp.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_noxp_add(
        self,
        ctx: commands.Context,
        channel: typing.Optional[
            typing.Union[discord.TextChannel, discord.CategoryChannel]
        ] = None,
        role: typing.Optional[discord.Role] = None,
    ):
        """Mute a channel/category OR a role from earning XP (give exactly one)."""
        if (channel is None) == (role is None):
            await ctx.send(
                _("Give exactly one of a channel/category or a role.")
            )
            return

        if role is not None and role.is_default():
            await ctx.send(_("You can't use @everyone as a no-XP zone."))
            return

        kind = leveling.NO_XP_CHANNEL if channel is not None else leveling.NO_XP_ROLE
        target = channel if channel is not None else role

        # Friendly fast-path refusal when the guild is already at the cap; the
        # WHERE guard inside the INSERT below is what enforces it RACE-SAFELY
        # (mirrors level_rewards_add's own atomic-cap precedent), so two admins
        # adding the 50th entry at once can never both win.
        count = await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) FROM level_no_xp WHERE guild_id = $1;", ctx.guild.id
        )
        if not leveling.can_add_no_xp_entry(count or 0):
            await ctx.send(
                _(
                    "This server already has the maximum of {max} no-XP "
                    "zones."
                ).format(max=leveling.MAX_NO_XP_PER_GUILD)
            )
            return

        inserted = await self.bot.db_pool.fetchval(
            """
            INSERT INTO level_no_xp (guild_id, kind, target_id)
            SELECT $1, $2, $3
            WHERE (SELECT COUNT(*) FROM level_no_xp WHERE guild_id = $1) < $4
            ON CONFLICT (guild_id, kind, target_id) DO NOTHING
            RETURNING kind;
            """,
            ctx.guild.id,
            kind,
            target.id,
            leveling.MAX_NO_XP_PER_GUILD,
        )
        if inserted is None:
            exists = await self.bot.db_pool.fetchval(
                "SELECT 1 FROM level_no_xp WHERE guild_id = $1 AND kind = $2 "
                "AND target_id = $3;",
                ctx.guild.id,
                kind,
                target.id,
            )
            if exists:
                await ctx.send(
                    _("{target} is already a no-XP zone.").format(
                        target=target.mention
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await ctx.send(
                    _(
                        "This server already has the maximum of {max} no-XP "
                        "zones."
                    ).format(max=leveling.MAX_NO_XP_PER_GUILD)
                )
            return

        await self.refresh_no_xp_cache(ctx.guild.id)

        embed = discord.Embed(
            title=_("No-XP zone added"),
            description=_("{target} will no longer earn XP.").format(
                target=target.mention
            ),
            colour=random_colour(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @levelconfig_noxp.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_noxp_remove(self, ctx):
        """Pick a no-XP zone to remove from a list of every one configured."""
        rows = await self._fetch_no_xp_rows(ctx.guild.id)
        if not rows:
            await ctx.send(_("This server has no no-XP zones configured yet."))
            return
        view = _RemoveNoXpView(self, ctx.guild, ctx.author.id, rows)
        view.message = await ctx.send(
            _("Pick a no-XP zone to remove:"), view=view
        )

    @levelconfig_noxp.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_noxp_list(self, ctx):
        """Show every no-XP channel/category and role configured."""
        await self._send_noxp_list(ctx)

    # -- announce subgroup -----------------------------------------------
    @levelconfig.group(name="announce")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_announce(self, ctx):
        """Manage where and how level-ups are announced."""
        if ctx.invoked_subcommand is None:
            await self._send_overview(ctx)

    @levelconfig_announce.command(name="mode")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_announce_mode(
        self,
        ctx: commands.Context,
        mode: typing.Literal["off", "channel", "dm", "fixed"],
        channel: typing.Optional[discord.TextChannel] = None,
    ):
        """Set how level-ups are announced (off / channel / dm / fixed)."""
        if mode == "fixed" and channel is None:
            await ctx.send(_("Give a channel when setting the fixed mode."))
            return

        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            await ctx.send(_("The leveling system isn't loaded right now."))
            return

        channel_id = channel.id if mode == "fixed" else None
        await leveling_cog.set_announce_mode(ctx.guild.id, mode, channel_id)

        config = leveling.LevelConfig(announce_mode=mode, announce_channel_id=channel_id)
        embed = discord.Embed(
            title=_("Announce mode updated"),
            description=_describe_announce_mode(config),
            colour=random_colour(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @levelconfig_announce.command(name="template")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_announce_template(
        self, ctx: commands.Context, text: typing.Optional[str] = None
    ):
        """Set a custom level-up message ({user} {level} {guild}), or reset it."""
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            await ctx.send(_("The leveling system isn't loaded right now."))
            return

        if text is None or text.strip().lower() == "reset":
            await leveling_cog.set_announce_template(ctx.guild.id, None)
            await ctx.send(
                _("The level-up message was reset to the default: \"{template}\"").format(
                    template=leveling.DEFAULT_ANNOUNCE_TEMPLATE
                )
            )
            return

        stripped = text.strip()
        ok, reason = leveling.validate_announce_template(stripped)
        if not ok:
            await ctx.send(_template_error_message(reason))
            return

        await leveling_cog.set_announce_template(ctx.guild.id, stripped)
        preview = leveling.render_announce_template(
            stripped,
            user_text=ctx.author.mention,
            level=5,
            guild_name=ctx.guild.name,
        )
        embed = discord.Embed(
            title=_("Level-up message updated"),
            description=_("Preview: {preview}").format(preview=preview),
            colour=random_colour(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(LevelConfigUI(bot))

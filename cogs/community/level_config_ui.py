"""Level-up no-XP zones, announce control, and XP multipliers (leveling L3+L4):
the ``/levelconfig`` admin group.

Independent knobs, all consumed by cogs/community/leveling.py's on_message hot
path (and, for voice XP and boosts/events, cogs/community/voice_xp.py's sweep):

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
* XP BOOSTS + EVENT (L4, ``xp_multipliers`` and level_config's ``event_factor``
  / ``event_ends_at``): boost or reduce XP globally, per channel/category, per
  role, or via a timed event. This cog owns both tables/columns directly (like
  level_no_xp) and, after every write, calls
  ``Leveling.refresh_multiplier_snapshot`` so the change is live on the very
  next message/sweep tick - no restart.

Cross-cog seam, matching the house pattern (cogs/community/level_rewards.py,
cogs/config/settings.py): looked up by name via ``bot.get_cog("Leveling")``,
guarded so a missing/failing Leveling cog degrades to a friendly refusal rather
than a crash - this cog owns no hot path itself.

Typography rule: ASCII '-' and '...' only. No em dashes, en dashes, or the
fancy ellipsis anywhere in this file (code, comments, docstrings, or strings).
"""

from __future__ import annotations

import datetime
import logging
import typing

import discord
from discord.ext import commands

from tools import leveling
from tools.formats import format_dt, random_colour
from tools.i18n import N_, _
from tools.views import AuthorView

try:
    # The house duration converter (tools/time.py), reused elsewhere (reminders,
    # rolemenus, announcements). Preferred whenever importable; a tiny pure
    # fallback parser (tools.leveling.parse_short_duration) covers the
    # (never-expected-in-production) case it is not - see the event command.
    from tools.time import ShortTime
except ImportError:  # pragma: no cover - defensive only
    ShortTime = None

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


def _describe_voice_xp(config):
    """One-line, human description of the guild's voice-XP setting."""
    if config.voice_xp_enabled:
        return _("On - {rate} XP per eligible minute in voice.").format(
            rate=config.voice_xp_per_minute
        )
    return _("Off - no XP for time spent in voice.")


# ----------------------------------------------------------------------
# XP multipliers (L4): boost/reduce XP globally, per channel/category, per
# role, plus a timed double-XP event.
# ----------------------------------------------------------------------

_MULTIPLIER_ERRORS = {
    "invalid": N_("The multiplier must be a plain number."),
}


def _multiplier_error_message(reason):
    if reason == "out_of_range":
        return _(
            "The multiplier must be between {min} and {max} (0 mutes XP "
            "entirely)."
        ).format(
            min=leveling.MIN_MULTIPLIER_FACTOR, max=leveling.MAX_MULTIPLIER_FACTOR
        )
    return _(_MULTIPLIER_ERRORS.get(reason, _MULTIPLIER_ERRORS["invalid"]))


def _duration_error_message(reason):
    if reason == "out_of_range":
        # MIN_EVENT_DURATION_SECONDS is a fixed 60s (1 minute) design constant
        # - see tools/leveling.py - so this is spelled out directly rather
        # than pluralized dynamically, matching the other bound messages in
        # this file (e.g. the voice-XP rate refusal).
        return _(
            "The event must last between 1 minute and {max_days} days (e.g. "
            "\"2h\" or \"3d\")."
        ).format(max_days=leveling.MAX_EVENT_DURATION_SECONDS // 86400)
    return _(
        "I couldn't understand that duration - try something like \"2h\" or "
        "\"3d\"."
    )


def _multiplier_lines(guild, rows):
    """(global_line_or_None, channel_lines, role_lines) rendered for the list
    card and the overview panel, resolving deleted targets to a placeholder
    rather than a broken mention (mirrors _no_xp_lines)."""
    global_line = None
    channel_lines = []
    role_lines = []
    for kind, target_id, factor in rows:
        if kind == leveling.MULTIPLIER_GLOBAL:
            global_line = _("Server-wide: **{factor}x**").format(factor=factor)
        elif kind == leveling.MULTIPLIER_CHANNEL:
            channel = guild.get_channel(target_id)
            text = (
                channel.mention
                if channel is not None
                else f"`{target_id}` " + _("(deleted)")
            )
            channel_lines.append(f"- {text}: **{factor}x**")
        else:
            role = guild.get_role(target_id)
            text = (
                role.mention if role is not None else f"`{target_id}` " + _("(deleted)")
            )
            role_lines.append(f"- {text}: **{factor}x**")
    return global_line, channel_lines, role_lines


def _describe_event(event_factor, event_ends_at):
    """One-line, human description of the guild's timed double-XP event.

    An already-expired stored row (``event_ends_at`` in the past) is
    described as "no event running" - the SAME "ignored at read time" rule
    tools.leveling.compute_multiplier applies, so the admin panel never
    shows a stale event as still active even in the short window before the
    next lazy-null refresh (cogs/community/leveling.py's
    refresh_multiplier_snapshot).
    """
    if (
        event_factor is None
        or event_ends_at is None
        or event_ends_at <= discord.utils.utcnow()
    ):
        return _("No XP event running. Use `/levelconfig event set` to start one.")
    return _("**{factor}x** XP until {when}.").format(
        factor=event_factor, when=format_dt(event_ends_at, "R")
    )


async def _fetch_config(pool, guild_id):
    """This guild's LevelConfig, read fresh (bypassing the enabled gate that
    resolve_config applies - an admin configuring no-xp zones or announce
    settings before ever turning leveling ON must still see/edit them)."""
    row = await pool.fetchrow(
        "SELECT enabled, cooldown_seconds, xp_min, xp_max, announce_mode, "
        "announce_channel_id, announce_template, voice_xp_enabled, "
        "voice_xp_per_minute FROM level_config WHERE guild_id = $1;",
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
# Admin UX: "remove a boost" picker (L4)
# ----------------------------------------------------------------------
class _RemoveMultiplierSelect(discord.ui.Select):
    """Lists every configured boost (global/channel/role) so the admin can
    pick one to delete. One option per row; the cap
    (MAX_MULTIPLIERS_PER_GUILD == 25) keeps this within Discord's own
    25-option select limit with no truncation needed - same precedent as
    _RemoveRewardSelect."""

    def __init__(self, cog, guild, rows):
        self.cog = cog
        self.guild = guild
        options = []
        for kind, target_id, factor in rows[:25]:
            if kind == leveling.MULTIPLIER_GLOBAL:
                label = _("Global boost")
                desc = _("Server-wide")
            elif kind == leveling.MULTIPLIER_CHANNEL:
                obj = guild.get_channel(target_id)
                label = _("Channel/category boost")
                desc = obj.name if obj is not None else _("Unknown (deleted)")
            else:
                obj = guild.get_role(target_id)
                label = _("Role boost")
                desc = obj.name if obj is not None else _("Unknown (deleted)")
            options.append(
                discord.SelectOption(
                    label=f"{label} ({factor}x)"[:100],
                    value=f"{kind}:{target_id}",
                    description=desc[:100],
                )
            )
        super().__init__(
            placeholder=_("Pick an XP boost to remove..."),
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
                "DELETE FROM xp_multipliers WHERE guild_id = $1 AND kind = $2 "
                "AND target_id = $3;",
                self.guild.id,
                kind,
                target_id,
            )
            await self.cog.refresh_multiplier_cache(self.guild.id)
            await interaction.response.edit_message(
                content=_("Removed that XP boost."),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("XP boost remove select failed")
            await interaction.response.edit_message(
                content=_("Something went wrong."), view=None
            )
        finally:
            # Terminal action: mirrors _RemoveNoXpSelect / _RemoveRewardSelect.
            self._owner.stop()


class _RemoveMultiplierView(AuthorView):
    def __init__(self, cog, guild, author_id, rows, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        select = _RemoveMultiplierSelect(cog, guild, rows)
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


class MultiplierListView(discord.ui.LayoutView):
    """Single-page Components V2 card: every configured XP boost plus the
    active timed event, for this guild."""

    def __init__(
        self, guild, rows, event_factor, event_ends_at, *, timeout=180
    ):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(guild, rows, event_factor, event_ends_at)

    def _build(self, guild, rows, event_factor, event_ends_at):
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(
            discord.ui.TextDisplay(
                "## " + _("XP boosts | {guild}").format(guild=guild.name)
            )
        )
        container.add_item(discord.ui.Separator())

        if not rows:
            container.add_item(
                discord.ui.TextDisplay(
                    _(
                        "No XP boosts configured yet. Use `/levelconfig boost "
                        "add` to boost or reduce XP globally, per channel, or "
                        "per role."
                    )
                )
            )
        else:
            global_line, channel_lines, role_lines = _multiplier_lines(guild, rows)
            if global_line:
                container.add_item(discord.ui.TextDisplay(global_line))
                if channel_lines or role_lines:
                    container.add_item(discord.ui.Separator())
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
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("**XP event**\n{event}").format(
                    event=_describe_event(event_factor, event_ends_at)
                )
            )
        )
        self.add_item(container)


class LevelConfigOverviewView(discord.ui.LayoutView):
    """Single-page Components V2 landing card: no-xp zones, announce settings,
    voice XP, XP boosts and the active timed event."""

    def __init__(
        self,
        guild,
        rows,
        config,
        multiplier_rows,
        event_factor,
        event_ends_at,
        *,
        timeout=180,
    ):
        super().__init__(timeout=timeout)
        self.message = None
        self._build(
            guild, rows, config, multiplier_rows, event_factor, event_ends_at
        )

    def _build(
        self, guild, rows, config, multiplier_rows, event_factor, event_ends_at
    ):
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
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("**Voice XP**\n{voice}").format(
                    voice=_describe_voice_xp(config)
                )
            )
        )
        container.add_item(discord.ui.Separator())
        global_line, channel_boost_lines, role_boost_lines = _multiplier_lines(
            guild, multiplier_rows
        )
        boost_summary = (
            _("No XP boosts configured. Use `/levelconfig boost add`.")
            if not multiplier_rows
            else "\n".join(
                ([global_line] if global_line else [])
                + channel_boost_lines
                + role_boost_lines
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                _("**XP boosts ({count}/{max})**\n{summary}").format(
                    count=len(multiplier_rows),
                    max=leveling.MAX_MULTIPLIERS_PER_GUILD,
                    summary=boost_summary,
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                _("**XP event**\n{event}").format(
                    event=_describe_event(event_factor, event_ends_at)
                )
            )
        )
        self.add_item(container)


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class LevelConfigUI(commands.Cog):
    """No-XP zones, level-up announce control, and XP boosts/events: the
    ``/levelconfig`` group."""

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

    async def refresh_multiplier_cache(self, guild_id):
        """The L4 sibling of refresh_no_xp_cache: pushes the just-written
        xp_multipliers rows OR level_config event columns into the Leveling
        cog's multiplier snapshot cache immediately. Called after every
        boost add/remove and every event set/off."""
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is not None:
            await leveling_cog.refresh_multiplier_snapshot(guild_id)

    # -- shared reads ----------------------------------------------------
    async def _fetch_no_xp_rows(self, guild_id):
        rows = await self.bot.db_pool.fetch(
            "SELECT kind, target_id FROM level_no_xp WHERE guild_id = $1;",
            guild_id,
        )
        return [(row["kind"], row["target_id"]) for row in rows]

    async def _fetch_multiplier_rows(self, guild_id):
        rows = await self.bot.db_pool.fetch(
            "SELECT kind, target_id, factor FROM xp_multipliers "
            "WHERE guild_id = $1;",
            guild_id,
        )
        return [(row["kind"], row["target_id"], row["factor"]) for row in rows]

    async def _fetch_event(self, guild_id):
        """(event_factor, event_ends_at) for a guild, or (None, None) when no
        level_config row exists yet (an admin may configure an event before
        ever turning leveling on, same as the no-xp/announce settings)."""
        row = await self.bot.db_pool.fetchrow(
            "SELECT event_factor, event_ends_at FROM level_config "
            "WHERE guild_id = $1;",
            guild_id,
        )
        if row is None:
            return None, None
        return row["event_factor"], row["event_ends_at"]

    async def _send_overview(self, ctx):
        rows = await self._fetch_no_xp_rows(ctx.guild.id)
        config = await _fetch_config(self.bot.db_pool, ctx.guild.id)
        multiplier_rows = await self._fetch_multiplier_rows(ctx.guild.id)
        event_factor, event_ends_at = await self._fetch_event(ctx.guild.id)
        view = LevelConfigOverviewView(
            ctx.guild, rows, config, multiplier_rows, event_factor, event_ends_at
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _send_noxp_list(self, ctx):
        rows = await self._fetch_no_xp_rows(ctx.guild.id)
        view = NoXpListView(ctx.guild, rows)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _send_boost_list(self, ctx):
        rows = await self._fetch_multiplier_rows(ctx.guild.id)
        event_factor, event_ends_at = await self._fetch_event(ctx.guild.id)
        view = MultiplierListView(ctx.guild, rows, event_factor, event_ends_at)
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
    @discord.app_commands.describe(
        channel="A channel/category that should earn no XP.",
        role="A role that should earn no XP.",
    )
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
    @discord.app_commands.describe(
        mode="off, channel, dm, or fixed.",
        channel="The channel to announce in (required for fixed mode).",
    )
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
    @discord.app_commands.describe(
        text="The message template, using {user} {level} {guild} (blank resets it)."
    )
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

    # -- voicexp subgroup ------------------------------------------------
    @levelconfig.group(name="voicexp")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_voicexp(self, ctx):
        """Manage XP earned for time spent in voice channels."""
        if ctx.invoked_subcommand is None:
            await self._send_overview(ctx)

    async def _apply_voice_xp_toggle(self, ctx, enabled):
        """Shared body of the on/off subcommands: delegate to the Leveling cog
        (so its hot-path config cache stays in step) and confirm, nudging the
        admin when leveling itself is off (voice XP grants nothing until it is
        on)."""
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            await ctx.send(_("The leveling system isn't loaded right now."))
            return
        await leveling_cog.set_voice_xp_enabled(ctx.guild.id, enabled)
        if enabled:
            title = _("Voice XP enabled")
            desc = _("Members now earn XP for time spent together in voice.")
            if not leveling_cog.is_enabled(ctx.guild.id):
                desc = desc + "\n" + _(
                    "Heads up: server leveling is off, so no voice XP is "
                    "granted until you turn leveling on."
                )
        else:
            title = _("Voice XP disabled")
            desc = _("Members no longer earn XP for time in voice.")
        embed = discord.Embed(title=title, description=desc, colour=random_colour())
        await ctx.send(embed=embed)

    @levelconfig_voicexp.command(name="on")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_voicexp_on(self, ctx):
        """Turn voice XP on: members earn XP for time in voice."""
        await self._apply_voice_xp_toggle(ctx, True)

    @levelconfig_voicexp.command(name="off")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_voicexp_off(self, ctx):
        """Turn voice XP off."""
        await self._apply_voice_xp_toggle(ctx, False)

    @levelconfig_voicexp.command(name="rate")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(rate="XP earned per eligible minute in voice (1-60).")
    async def levelconfig_voicexp_rate(self, ctx, rate: int):
        """Set how much XP a member earns per eligible minute in voice (1-60)."""
        if not leveling.validate_voice_xp_rate(rate)[0]:
            await ctx.send(
                _(
                    "The rate must be between {min} and {max} XP per minute."
                ).format(
                    min=leveling.MIN_VOICE_XP_PER_MINUTE,
                    max=leveling.MAX_VOICE_XP_PER_MINUTE,
                )
            )
            return
        leveling_cog = self.bot.get_cog("Leveling")
        if leveling_cog is None:
            await ctx.send(_("The leveling system isn't loaded right now."))
            return
        await leveling_cog.set_voice_xp_rate(ctx.guild.id, rate)
        embed = discord.Embed(
            title=_("Voice XP rate updated"),
            description=_(
                "Members now earn **{rate}** XP per eligible minute in voice."
            ).format(rate=rate),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)

    # -- boost subgroup (L4) -----------------------------------------------
    @levelconfig.group(name="boost")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_boost(self, ctx):
        """Manage XP boosts: global, per channel/category, or per role."""
        if ctx.invoked_subcommand is None:
            await self._send_boost_list(ctx)

    @levelconfig_boost.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        factor="The XP multiplier (0-5x).",
        channel="A channel/category to boost (omit for a role or server-wide boost).",
        role="A role to boost (omit for a channel or server-wide boost).",
    )
    async def levelconfig_boost_add(
        self,
        ctx: commands.Context,
        factor: float,
        channel: typing.Optional[
            typing.Union[discord.TextChannel, discord.CategoryChannel]
        ] = None,
        role: typing.Optional[discord.Role] = None,
    ):
        """Boost or reduce XP (0-5x). Give a channel/category OR a role, or
        neither for a server-wide boost. Re-running this on the same target
        just updates its factor."""
        if channel is not None and role is not None:
            await ctx.send(
                _(
                    "Give at most one of a channel/category or a role - "
                    "give neither for a server-wide boost."
                )
            )
            return
        if role is not None and role.is_default():
            await ctx.send(
                _(
                    "You can't target @everyone directly - leave both the "
                    "channel and role empty for a server-wide boost instead."
                )
            )
            return

        ok, reason = leveling.validate_multiplier_factor(factor)
        if not ok:
            await ctx.send(_multiplier_error_message(reason))
            return

        if channel is not None:
            kind, target_id, target_text = (
                leveling.MULTIPLIER_CHANNEL,
                channel.id,
                channel.mention,
            )
        elif role is not None:
            kind, target_id, target_text = (
                leveling.MULTIPLIER_ROLE,
                role.id,
                role.mention,
            )
        else:
            kind = leveling.MULTIPLIER_GLOBAL
            target_id = leveling.GLOBAL_MULTIPLIER_TARGET_ID
            target_text = _("the whole server")

        # Race-safe: an existing (guild, kind, target) row always upserts its
        # factor - adjusting a boost is never blocked by the cap. The cap
        # only ever refuses a genuinely NEW row once the guild already has
        # MAX_MULTIPLIERS_PER_GUILD configured, across every kind.
        inserted = await self.bot.db_pool.fetchval(
            """
            INSERT INTO xp_multipliers (guild_id, kind, target_id, factor)
            SELECT $1, $2, $3, $4
            WHERE (SELECT COUNT(*) FROM xp_multipliers WHERE guild_id = $1) < $5
               OR EXISTS (
                   SELECT 1 FROM xp_multipliers
                   WHERE guild_id = $1 AND kind = $2 AND target_id = $3
               )
            ON CONFLICT (guild_id, kind, target_id)
                DO UPDATE SET factor = EXCLUDED.factor
            RETURNING kind;
            """,
            ctx.guild.id,
            kind,
            target_id,
            factor,
            leveling.MAX_MULTIPLIERS_PER_GUILD,
        )
        if inserted is None:
            # A concurrent add filled the last slot between the pre-check and
            # the atomic INSERT (only reachable for a genuinely new target -
            # an existing target always matches the EXISTS branch and upserts).
            await ctx.send(
                _(
                    "This server already has the maximum of {max} XP boosts."
                ).format(max=leveling.MAX_MULTIPLIERS_PER_GUILD)
            )
            return

        await self.refresh_multiplier_cache(ctx.guild.id)

        embed = discord.Embed(
            title=_("XP boost set"),
            description=_(
                "{target} now has a **{factor}x** XP multiplier."
            ).format(target=target_text, factor=factor),
            colour=random_colour(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @levelconfig_boost.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_boost_remove(self, ctx):
        """Pick an XP boost to remove from a list of every one configured."""
        rows = await self._fetch_multiplier_rows(ctx.guild.id)
        if not rows:
            await ctx.send(_("This server has no XP boosts configured yet."))
            return
        view = _RemoveMultiplierView(self, ctx.guild, ctx.author.id, rows)
        view.message = await ctx.send(_("Pick an XP boost to remove:"), view=view)

    @levelconfig_boost.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_boost_list(self, ctx):
        """Show every XP boost configured for this server."""
        await self._send_boost_list(ctx)

    # -- event subgroup (L4) -------------------------------------------------
    @levelconfig.group(name="event")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_event(self, ctx):
        """Manage the timed double-XP (or reduced-XP) event."""
        if ctx.invoked_subcommand is None:
            event_factor, event_ends_at = await self._fetch_event(ctx.guild.id)
            embed = discord.Embed(
                title=_("XP event"),
                description=_describe_event(event_factor, event_ends_at),
                colour=random_colour(),
            )
            await ctx.send(embed=embed)

    async def _write_event(self, guild_id, factor, ends_at):
        """Upsert level_config's event columns, seeding ``enabled`` from the
        legacy JSONB flag on INSERT (never touching it on UPDATE) - the same
        precedent as level_rewards_mode/set_voice_xp_enabled, so starting/
        stopping an event for a guild that enabled leveling only through the
        legacy bool never masks that flag with a fresh FALSE row. Always
        refreshes the Leveling cog's multiplier snapshot afterwards."""
        await self.bot.db_pool.execute(
            """
            INSERT INTO level_config (guild_id, enabled, event_factor, event_ends_at)
            VALUES (
                $1,
                COALESCE(
                    (SELECT (settings->>'leveling_enabled')::boolean
                     FROM guild_settings WHERE guild_id = $1),
                    FALSE
                ),
                $2,
                $3
            )
            ON CONFLICT (guild_id) DO UPDATE
                SET event_factor = $2, event_ends_at = $3;
            """,
            guild_id,
            factor,
            ends_at,
        )
        await self.refresh_multiplier_cache(guild_id)

    @levelconfig_event.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        factor="The XP multiplier for the event (e.g. 2 for double XP).",
        duration="How long the event runs, e.g. 2h (max 14 days).",
    )
    async def levelconfig_event_set(
        self, ctx: commands.Context, factor: float, duration: str
    ):
        """Start a timed XP event - e.g. `/levelconfig event set 2 2h`
        doubles XP for 2 hours (max 14 days)."""
        ok, reason = leveling.validate_multiplier_factor(factor)
        if not ok:
            await ctx.send(_multiplier_error_message(reason))
            return

        now = discord.utils.utcnow()
        if ShortTime is not None:
            try:
                ends_at = ShortTime(duration, now=now).dt
            except commands.BadArgument:
                await ctx.send(_duration_error_message("malformed"))
                return
            seconds = (ends_at - now).total_seconds()
        else:  # pragma: no cover - defensive only, see the module import above
            seconds = leveling.parse_short_duration(duration)
            if seconds is None:
                await ctx.send(_duration_error_message("malformed"))
                return
            ends_at = now + datetime.timedelta(seconds=seconds)

        ok, reason = leveling.validate_event_duration(seconds)
        if not ok:
            await ctx.send(_duration_error_message(reason))
            return

        await self._write_event(ctx.guild.id, factor, ends_at)

        embed = discord.Embed(
            title=_("XP event started"),
            description=_describe_event(factor, ends_at),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)

    @levelconfig_event.command(name="off")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levelconfig_event_off(self, ctx):
        """Stop the active XP event (if any)."""
        await self._write_event(ctx.guild.id, None, None)
        await ctx.send(_("The XP event was stopped."))


async def setup(bot):
    await bot.add_cog(LevelConfigUI(bot))

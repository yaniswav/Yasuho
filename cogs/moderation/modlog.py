import asyncio
import logging

import discord
from discord.ext import commands

from tools import db, embed_creator, settings
from tools.i18n import _
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)

# Per-event embed colour (replaces the old random_colour() so the log reads at
# a glance: greens for "good", reds for bans, oranges/blurple for messages).
EVENT_COLOURS = {
    "join": 0x2ECC71,
    "leave": 0x95A5A6,
    "ban": 0xE74C3C,
    "unban": 0x2ECC71,
    "message_delete": 0xE67E22,
    "message_edit": 0x5865F2,
}

# Human labels for the panel's multi-select. Order defines the option order and
# the max_values (one per event key).
EVENT_LABELS = {
    "join": "Member joins",
    "leave": "Member leaves / kicks",
    "ban": "Member bans",
    "unban": "Member unbans",
    "message_delete": "Message deletions",
    "message_edit": "Message edits",
}

EVENT_KEYS = list(EVENT_LABELS)

# Embed titles for the shared member-event embed, keyed by the same action key
# used for EVENT_COLOURS.
MEMBER_EVENT_TITLES = {
    "join": "Member Joined",
    "leave": "Member Left / Kicked",
    "ban": "Member Banned",
    "unban": "Member Unbanned",
}


def _member_event_embed(user, action):
    """Build the shared join/leave/ban/unban embed for a member event.

    ``action`` is the event key (join/leave/ban/unban) used to look up both the
    title and the colour. Callers that need extra fields (e.g. join's account
    age) add them to the returned embed.
    """

    embed = discord.Embed(
        title=MEMBER_EVENT_TITLES[action],
        description=f"{user.mention} ({user})",
        colour=EVENT_COLOURS[action],
        timestamp=discord.utils.utcnow(),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=_("ID: {id}").format(id=user.id))
    return embed


# ----------------------------------------------------------------------
# Interactive control panel (discord.ui)
# ----------------------------------------------------------------------
class LogChannelSelect(discord.ui.ChannelSelect):
    """Pick the text channel that mod-log embeds are sent to."""

    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder=_("Select the log channel..."),
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction):
        try:
            channel = self.values[0]
            await self.panel.cog._set_channel(interaction.guild.id, channel.id)
            self.panel.channel_id = channel.id
            await self.panel._rerender(interaction)
        except Exception:
            log.exception("Mod-log panel channel select failed")
            await embed_creator.notify_failure(interaction)


class EventSelect(discord.ui.Select):
    """Multi-select of which server events should be logged."""

    def __init__(self, panel):
        self.panel = panel
        enabled = panel._enabled_set()
        options = [
            discord.SelectOption(
                label=EVENT_LABELS[key],
                value=key,
                default=key in enabled,
            )
            for key in EVENT_KEYS
        ]
        super().__init__(
            placeholder=_("Choose which events to log..."),
            min_values=0,
            max_values=len(EVENT_KEYS),  # all six event keys
            options=options,
        )

    async def callback(self, interaction):
        try:
            selected = list(self.values)
            await self.panel.cog._set_events(interaction.guild.id, selected)
            self.panel.events = selected
            await self.panel._rerender(interaction)
        except Exception:
            log.exception("Mod-log panel event select failed")
            await embed_creator.notify_failure(interaction)


class _DisableButton(discord.ui.Button):
    def __init__(self, panel):
        self.panel = panel
        # Not _()-wrapped: preserved verbatim from the pre-CV2 literal label.
        super().__init__(label="Disable logging", style=discord.ButtonStyle.danger)

    async def callback(self, interaction):
        try:
            await self.panel.cog._disable(interaction.guild.id)
            self.panel.channel_id = None
            await self.panel._rerender(interaction)
        except Exception:
            log.exception("Mod-log panel disable failed")
            await embed_creator.notify_failure(interaction)


# ----------------------------------------------------------------------
# Edit a LayoutView panel in place with view=-only (no embed/content)
# ----------------------------------------------------------------------
async def _refresh_layout(interaction, message, view):
    """Edit a LayoutView panel in place with ``view=`` only (no embed/content).

    A Components V2 message carries its content inside the view and Discord
    rejects an ``embed=`` on such an edit. Tries the live interaction edit
    first, then falls back to editing the stored message when the interaction
    was already answered (e.g. a deferred modal submit).
    """

    try:
        if not interaction.response.is_done():
            await interaction.response.edit_message(view=view)
            return
    except discord.HTTPException:
        pass
    if message is not None:
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass


class ModLogPanel(AuthorLayoutView):
    """Author-restricted control panel for the mod-log: channel + event toggles.

    A single Components V2 :class:`~discord.ui.Container` in the house style
    established by the settings/welcome/Twitch panels. The deny wording
    matches AuthorLayoutView's default ("This panel isn't for you.", the same
    wording the old AuthorView-based panel used explicitly), so it is left
    unset here.
    """

    def __init__(self, cog, author_id, *, channel_id, events, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.channel_id = channel_id
        # ``events`` is None (unset -> everything enabled) or an explicit list.
        self.events = events
        self._build()

    def _enabled_set(self):
        if self.events is None:
            return set(EVENT_KEYS)
        return set(self.events)

    def _build(self):
        """(Re)assemble the layout from the current channel/events state."""

        enabled = self._enabled_set()
        if self.channel_id:
            channel_value = f"<#{self.channel_id}>"
        else:
            channel_value = _("*Not set - logging is off.*")

        lines = [
            f"{'🟢' if key in enabled else '⚪'} {EVENT_LABELS[key]}"
            for key in EVENT_KEYS
        ]

        container = discord.ui.Container(accent_colour=0x5865F2)
        header_lines = [
            "### " + _("Mod-log settings"),
            _(
                "Choose where server events are logged and which events to "
                "record. Changes apply instantly."
            ),
        ]
        container.add_item(discord.ui.TextDisplay("\n".join(header_lines)))
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Log channel") + ":** " + channel_value
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "**" + _("Events") + "**\n" + "\n".join(lines)
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(LogChannelSelect(self)))
        container.add_item(discord.ui.ActionRow(EventSelect(self)))
        container.add_item(discord.ui.ActionRow(_DisableButton(self)))
        container.add_item(
            discord.ui.TextDisplay(
                "-# " + _("Only you can use these controls.")
            )
        )
        self.add_item(container)

    async def _rerender(self, interaction):
        """Re-render with a fresh panel so option defaults reflect new state."""

        new = ModLogPanel(
            self.cog,
            self.author_id,
            channel_id=self.channel_id,
            events=self.events,
        )
        new.message = self.message
        self.stop()
        await _refresh_layout(interaction, self.message, new)


class ModLog(commands.Cog):
    """Logs moderation actions and server events to a configured channel."""

    def __init__(self, bot):
        self.bot = bot
        self._recent_bans = set()
        # (guild_id, user_id, kind) keys for bot-initiated actions whose own
        # case embed is already posted by the moderation cog; the matching
        # listener skips its duplicate embed. Keys auto-expire after ~10s.
        self._suppressed = set()
        # guild_id -> channel_id | None (negative-cached: None means "looked up,
        # not configured", so unconfigured guilds never re-query).
        self._channels = {}

    def suppress(self, guild_id, user_id, kind):
        """Mark a bot-initiated action so its listener skips the duplicate embed.

        The moderation cog posts its own case embed for ban/kick/unban, so the
        matching gateway listener ('ban'/'unban'/'remove') would otherwise log
        the same action twice. The key auto-expires after ~10s in case the
        gateway event never arrives (e.g. the action failed).
        """
        key = (guild_id, user_id, kind)
        self._suppressed.add(key)
        asyncio.get_running_loop().call_later(
            10, self._suppressed.discard, key
        )

    # -- settings helpers (shared by panel + fallback subcommands) ------
    async def _set_channel(self, guild_id, channel_id):
        await db.upsert_guild_value(
            self.bot.db_pool, "modlog", "channel_id", guild_id, channel_id
        )
        self._channels[guild_id] = channel_id

    async def _disable(self, guild_id):
        await self.bot.db_pool.execute(
            "DELETE FROM modlog WHERE guild_id = $1;", guild_id
        )
        self._channels[guild_id] = None

    async def _get_events(self, guild_id):
        return await settings.get_guild(
            self.bot.db_pool, guild_id, "modlog_events", None
        )

    async def _set_events(self, guild_id, events):
        await settings.set_guild(
            self.bot.db_pool, guild_id, "modlog_events", events
        )

    async def _enabled(self, guild_id, key):
        """True if ``key`` should be logged. Unset settings = all enabled."""

        events = await self._get_events(guild_id)
        if events is None:
            return True
        return key in events

    # -- commands -------------------------------------------------------
    @commands.hybrid_group(name="modlog")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def modlog(self, ctx):
        """Open the moderation-log control panel."""

        if ctx.invoked_subcommand is not None:
            return

        # Warm the cache so we can render the current channel, then build the
        # panel from current state.
        await self.get_log_channel(ctx.guild)
        channel_id = self._channels.get(ctx.guild.id)
        events = await self._get_events(ctx.guild.id)
        view = ModLogPanel(
            self, ctx.author.id, channel_id=channel_id, events=events
        )
        view.message = await ctx.send(view=view)

    @modlog.command(name="set")
    @discord.app_commands.describe(channel="The channel to send moderation logs to.")
    async def modlog_set(self, ctx, channel: discord.TextChannel):
        """Set the channel where moderation logs are sent."""

        await self._set_channel(ctx.guild.id, channel.id)
        embed = discord.Embed(
            title=_("Mod log"),
            description=_("Mod-log channel set to {channel}.").format(
                channel=channel.mention
            ),
            colour=EVENT_COLOURS["join"],
        )
        await ctx.send(embed=embed)

    @modlog.command(name="disable")
    async def modlog_disable(self, ctx):
        """Disable moderation logging for this guild."""

        await self._disable(ctx.guild.id)
        embed = discord.Embed(
            title=_("Mod log"),
            description=_("Mod-log has been disabled for this guild."),
            colour=EVENT_COLOURS["leave"],
        )
        await ctx.send(embed=embed)

    # -- channel resolution + send funnel -------------------------------
    async def get_log_channel(self, guild):
        if guild is None:
            return None

        gid = guild.id
        if gid not in self._channels:
            query = "SELECT channel_id FROM modlog WHERE guild_id = $1;"
            self._channels[gid] = await self.bot.db_pool.fetchval(query, gid)

        cid = self._channels[gid]
        return guild.get_channel(cid) if cid else None

    async def post_action(self, guild, embed):
        """Send a pre-built embed to the guild's configured log channel.

        The single funnel the moderation and automod cogs use (via
        ``ml = self.bot.get_cog('ModLog'); await ml.post_action(...)``); this
        cog's own event listeners route through it too.
        """

        channel = await self.get_log_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except Exception:
            log.exception("Failed to send mod-log message")

    # -- event listeners ------------------------------------------------
    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        # Always record the ban so on_member_remove can dedup the resulting
        # leave, regardless of which events are enabled.
        key = (guild.id, user.id)
        self._recent_bans.add(key)
        asyncio.get_running_loop().call_later(
            5, self._recent_bans.discard, key
        )

        # A bot ban already posted its own case embed; skip the duplicate here
        # (the recent-ban dedup above still applies to the resulting leave).
        skey = (guild.id, user.id, "ban")
        if skey in self._suppressed:
            self._suppressed.discard(skey)
            return

        if not await self._enabled(guild.id, "ban"):
            return

        embed = _member_event_embed(user, "ban")
        await self.post_action(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        # A bot unban already posted its own case embed; skip the duplicate.
        skey = (guild.id, user.id, "unban")
        if skey in self._suppressed:
            self._suppressed.discard(skey)
            return

        if not await self._enabled(guild.id, "unban"):
            return

        embed = _member_event_embed(user, "unban")
        await self.post_action(guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if (member.guild.id, member.id) in self._recent_bans:
            return

        # A bot kick already posted its own case embed; skip the duplicate.
        skey = (member.guild.id, member.id, "remove")
        if skey in self._suppressed:
            self._suppressed.discard(skey)
            return

        if not await self._enabled(member.guild.id, "leave"):
            return

        embed = _member_event_embed(member, "leave")
        await self.post_action(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if not await self._enabled(member.guild.id, "join"):
            return

        embed = _member_event_embed(member, "join")
        embed.add_field(
            name=_("Account created"),
            value=discord.utils.format_dt(member.created_at, "R"),
        )
        await self.post_action(member.guild, embed)

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or message.guild is None or not message.content:
            return

        if not await self._enabled(message.guild.id, "message_delete"):
            return

        embed = discord.Embed(
            title=_("Message Deleted"),
            colour=EVENT_COLOURS["message_delete"],
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=message.author.display_avatar.url)
        embed.add_field(
            name=_("Author"), value=f"{message.author.mention} ({message.author})"
        )
        embed.add_field(name=_("Channel"), value=message.channel.mention)
        embed.add_field(
            name=_("Content"), value=message.content[:1024], inline=False
        )
        jump = getattr(message, "jump_url", None)
        if jump:
            embed.add_field(
                name=_("Jump"),
                value=_("[Go to location]({url})").format(url=jump),
                inline=False,
            )
        embed.set_footer(text=_("ID: {id}").format(id=message.author.id))
        await self.post_action(message.guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if (
            before.author.bot
            or before.guild is None
            or before.content == after.content
        ):
            return

        if not before.content and not after.content:
            return

        if not await self._enabled(before.guild.id, "message_edit"):
            return

        embed = discord.Embed(
            title=_("Message Edited"),
            colour=EVENT_COLOURS["message_edit"],
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=before.author.display_avatar.url)
        embed.add_field(
            name=_("Author"), value=f"{before.author.mention} ({before.author})"
        )
        embed.add_field(name=_("Channel"), value=before.channel.mention)
        embed.add_field(
            name=_("Before"), value=(before.content[:512] or "​"), inline=False
        )
        embed.add_field(
            name=_("After"), value=(after.content[:512] or "​"), inline=False
        )
        jump = getattr(after, "jump_url", None)
        if jump:
            embed.add_field(
                name=_("Jump"),
                value=_("[Go to message]({url})").format(url=jump),
                inline=False,
            )
        embed.set_footer(text=_("ID: {id}").format(id=before.author.id))
        await self.post_action(before.guild, embed)


async def setup(bot):
    await bot.add_cog(ModLog(bot))

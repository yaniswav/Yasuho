import asyncio
import logging

import discord
from discord.ext import commands

from tools import settings

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


# ----------------------------------------------------------------------
# Interactive control panel (discord.ui)
# ----------------------------------------------------------------------
class LogChannelSelect(discord.ui.ChannelSelect):
    """Pick the text channel that mod-log embeds are sent to."""

    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Select the log channel...",
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction):
        try:
            channel = self.values[0]
            await self.panel.cog._set_channel(interaction.guild.id, channel.id)
            self.panel.channel_id = channel.id
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Mod-log panel channel select failed")
            await self.panel._error(interaction)


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
            placeholder="Choose which events to log...",
            min_values=0,
            max_values=len(EVENT_KEYS),  # all six event keys
            options=options,
            row=1,
        )

    async def callback(self, interaction):
        try:
            selected = list(self.values)
            await self.panel.cog._set_events(interaction.guild.id, selected)
            self.panel.events = selected
            await self.panel._refresh(interaction)
        except Exception:
            log.exception("Mod-log panel event select failed")
            await self.panel._error(interaction)


class ModLogPanel(discord.ui.View):
    """Author-restricted control panel for the mod-log: channel + event toggles."""

    def __init__(self, cog, author_id, *, channel_id, events, timeout=180):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.channel_id = channel_id
        # ``events`` is None (unset -> everything enabled) or an explicit list.
        self.events = events
        self.message = None
        self.add_item(LogChannelSelect(self))
        self.add_item(EventSelect(self))

    def _enabled_set(self):
        if self.events is None:
            return set(EVENT_KEYS)
        return set(self.events)

    def build_embed(self):
        enabled = self._enabled_set()
        if self.channel_id:
            channel_value = f"<#{self.channel_id}>"
        else:
            channel_value = "*Not set - logging is off.*"

        lines = [
            f"{'🟢' if key in enabled else '⚪'} {EVENT_LABELS[key]}"
            for key in EVENT_KEYS
        ]

        embed = discord.Embed(
            title="Mod-log settings",
            description=(
                "Choose where server events are logged and which events to "
                "record. Changes apply instantly."
            ),
            colour=0x5865F2,
        )
        embed.add_field(name="Log channel", value=channel_value, inline=False)
        embed.add_field(name="Events", value="\n".join(lines), inline=False)
        embed.set_footer(text="Only you can use these controls.")
        return embed

    async def _refresh(self, interaction):
        """Re-render with a fresh panel so option defaults reflect new state."""

        new = ModLogPanel(
            self.cog,
            self.author_id,
            channel_id=self.channel_id,
            events=self.events,
        )
        new.message = self.message
        self.stop()
        await interaction.response.edit_message(
            embed=new.build_embed(), view=new
        )

    async def _error(self, interaction):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong.", ephemeral=True
                )
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Disable logging", style=discord.ButtonStyle.danger, row=2
    )
    async def disable_button(self, interaction, button):
        try:
            await self.cog._disable(interaction.guild.id)
            self.channel_id = None
            await self._refresh(interaction)
        except Exception:
            log.exception("Mod-log panel disable failed")
            await self._error(interaction)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This panel isn't for you.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ModLog(commands.Cog):
    """Logs moderation actions and server events to a configured channel."""

    def __init__(self, bot):
        self.bot = bot
        self._recent_bans = set()
        # guild_id -> channel_id | None (negative-cached: None means "looked up,
        # not configured", so unconfigured guilds never re-query).
        self._channels = {}

    # -- settings helpers (shared by panel + fallback subcommands) ------
    async def _set_channel(self, guild_id, channel_id):
        query = (
            "INSERT INTO modlog (guild_id, channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2;"
        )
        await self.bot.db_pool.execute(query, guild_id, channel_id)
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
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @modlog.command(name="set")
    async def modlog_set(self, ctx, channel: discord.TextChannel):
        """Set the channel where moderation logs are sent."""

        await self._set_channel(ctx.guild.id, channel.id)
        embed = discord.Embed(
            title="Mod log",
            description=f"Mod-log channel set to {channel.mention}.",
            colour=EVENT_COLOURS["join"],
        )
        await ctx.send(embed=embed)

    @modlog.command(name="disable")
    async def modlog_disable(self, ctx):
        """Disable moderation logging for this guild."""

        await self._disable(ctx.guild.id)
        embed = discord.Embed(
            title="Mod log",
            description="Mod-log has been disabled for this guild.",
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

        if not await self._enabled(guild.id, "ban"):
            return

        embed = discord.Embed(
            title="Member Banned",
            description=f"{user.mention} ({user})",
            colour=EVENT_COLOURS["ban"],
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"ID: {user.id}")
        await self.post_action(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        if not await self._enabled(guild.id, "unban"):
            return

        embed = discord.Embed(
            title="Member Unbanned",
            description=f"{user.mention} ({user})",
            colour=EVENT_COLOURS["unban"],
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"ID: {user.id}")
        await self.post_action(guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if (member.guild.id, member.id) in self._recent_bans:
            return

        if not await self._enabled(member.guild.id, "leave"):
            return

        embed = discord.Embed(
            title="Member Left / Kicked",
            description=f"{member.mention} ({member})",
            colour=EVENT_COLOURS["leave"],
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"ID: {member.id}")
        await self.post_action(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if not await self._enabled(member.guild.id, "join"):
            return

        embed = discord.Embed(
            title="Member Joined",
            description=f"{member.mention} ({member})",
            colour=EVENT_COLOURS["join"],
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="Account created",
            value=discord.utils.format_dt(member.created_at, "R"),
        )
        embed.set_footer(text=f"ID: {member.id}")
        await self.post_action(member.guild, embed)

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or message.guild is None or not message.content:
            return

        if not await self._enabled(message.guild.id, "message_delete"):
            return

        embed = discord.Embed(
            title="Message Deleted",
            colour=EVENT_COLOURS["message_delete"],
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=message.author.display_avatar.url)
        embed.add_field(
            name="Author", value=f"{message.author.mention} ({message.author})"
        )
        embed.add_field(name="Channel", value=message.channel.mention)
        embed.add_field(
            name="Content", value=message.content[:1024], inline=False
        )
        jump = getattr(message, "jump_url", None)
        if jump:
            embed.add_field(
                name="Jump", value=f"[Go to location]({jump})", inline=False
            )
        embed.set_footer(text=f"ID: {message.author.id}")
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
            title="Message Edited",
            colour=EVENT_COLOURS["message_edit"],
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=before.author.display_avatar.url)
        embed.add_field(
            name="Author", value=f"{before.author.mention} ({before.author})"
        )
        embed.add_field(name="Channel", value=before.channel.mention)
        embed.add_field(
            name="Before", value=(before.content[:512] or "​"), inline=False
        )
        embed.add_field(
            name="After", value=(after.content[:512] or "​"), inline=False
        )
        jump = getattr(after, "jump_url", None)
        if jump:
            embed.add_field(
                name="Jump", value=f"[Go to message]({jump})", inline=False
            )
        embed.set_footer(text=f"ID: {before.author.id}")
        await self.post_action(before.guild, embed)


async def setup(bot):
    await bot.add_cog(ModLog(bot))

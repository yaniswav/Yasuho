"""Announcement delivery through the shared Reminder timer dispatcher."""

import datetime
import logging

import discord
from discord.ext import commands

from .announcements_store import AnnouncementStore

log = logging.getLogger(__name__)


class ScheduledAnnouncements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = AnnouncementStore(bot)

    @commands.Cog.listener()
    async def on_announcement_timer_complete(self, extra):
        # Generic timers without a linked schedule retain their original event
        # dispatch; only this store's claimed snapshots belong to the engine.
        row = extra.get("announcement")
        if row is None:
            return
        if discord.utils.utcnow() - extra["scheduled_at"] > datetime.timedelta(hours=1):
            log.info("ANNOUNCE-SKIP-LATE id=%s guild=%s", row["id"], row["guild_id"])
            if row["recurrence"] == "none":
                log.info("ANNOUNCE-DISABLED id=%s reason=late-one-shot", row["id"])
            return
        try:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None or guild.me is None:
                raise ValueError("Guild unavailable")
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                channel = await self.bot.fetch_channel(row["channel_id"])
            channel_guild = getattr(channel, "guild", None)
            if channel_guild is None or channel_guild.id != guild.id or channel.type not in (
                discord.ChannelType.text, discord.ChannelType.news,
            ):
                raise ValueError("Announcement requires a guild text channel")
            permissions = channel.permissions_for(guild.me)
            if not permissions.view_channel or not permissions.send_messages:
                raise ValueError("Missing channel send permissions")
            content, embed, mentions = self.store.render(row)
            if embed is not None and not permissions.embed_links:
                raise ValueError("Missing embed_links permission")
            await channel.send(content, embed=embed, allowed_mentions=mentions)
        except (discord.HTTPException, ValueError) as exc:
            # Do not persist HTTP response bodies: they may contain message text.
            error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            result = await self.store.record_result(row, error)
            log.warning("ANNOUNCE-FAIL id=%s guild=%s reason=%s", row["id"], row["guild_id"], error)
            if result is not None and result["failures"] >= 3:
                log.warning("ANNOUNCE-DISABLED id=%s reason=consecutive-failures", row["id"])
            return
        await self.store.record_result(row)
        log.info("ANNOUNCE-SENT id=%s guild=%s", row["id"], row["guild_id"])

    # No on_guild_remove purge: like every other guild table, announcements
    # leave with tools/retention.py, so a quick re-invite keeps them. Until
    # then a departed guild's firings fail and disable after 3 tries.


async def setup(bot):
    await bot.add_cog(ScheduledAnnouncements(bot))

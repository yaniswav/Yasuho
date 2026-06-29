import asyncio
import logging

import discord
from discord.ext import commands

from tools.formats import random_colour

log = logging.getLogger(__name__)


class ModLog(commands.Cog):
    """Logs moderation actions and server events to a configured channel."""

    def __init__(self, bot):
        self.bot = bot
        self._recent_bans = set()

    @commands.hybrid_group(name="modlog")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def modlog(self, ctx):
        """Moderation log related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @modlog.command(name="set")
    async def modlog_set(self, ctx, channel: discord.TextChannel):
        """Set the channel where moderation logs are sent."""

        query = """
            INSERT INTO modlog (guild_id, channel_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, channel.id)
        embed = discord.Embed(
            title="Mod log", colour=random_colour()
        )
        embed.add_field(
            name="Mod log channel has been set to:", value=channel.mention
        )
        await ctx.send(embed=embed)

    @modlog.command(name="disable")
    async def modlog_disable(self, ctx):
        """Disable moderation logging for this guild."""

        query = """DELETE FROM modlog WHERE guild_id = $1;"""

        await self.bot.db_pool.execute(query, ctx.guild.id)
        embed = discord.Embed(
            title="Mod log", colour=random_colour()
        )
        embed.add_field(
            name="Mod log has been disabled for this guild.", value="​"
        )
        await ctx.send(embed=embed)

    async def get_log_channel(self, guild):
        if guild is None:
            return None

        query = """SELECT channel_id FROM modlog WHERE guild_id = $1;"""
        cid = await self.bot.db_pool.fetchval(query, guild.id)
        return guild.get_channel(cid) if cid else None

    async def send_log(self, guild, embed):
        ch = await self.get_log_channel(guild)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                log.exception("Failed to send mod log message")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        key = (guild.id, user.id)
        self._recent_bans.add(key)
        asyncio.get_running_loop().call_later(
            5, self._recent_bans.discard, key
        )
        embed = discord.Embed(
            title="Member Banned",
            description=f"{user.mention} ({user})",
            colour=random_colour(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"ID: {user.id}")
        await self.send_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        embed = discord.Embed(
            title="Member Unbanned",
            description=f"{user.mention} ({user})",
            colour=random_colour(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"ID: {user.id}")
        await self.send_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if (member.guild.id, member.id) in self._recent_bans:
            return

        embed = discord.Embed(
            title="Member Left / Kicked",
            description=f"{member.mention} ({member})",
            colour=random_colour(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"ID: {member.id}")
        await self.send_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        embed = discord.Embed(
            title="Member Joined",
            description=f"{member.mention} ({member})",
            colour=random_colour(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="Account created",
            value=discord.utils.format_dt(member.created_at, "R"),
        )
        embed.set_footer(text=f"ID: {member.id}")
        await self.send_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or message.guild is None or not message.content:
            return

        embed = discord.Embed(
            title="Message Deleted",
            colour=random_colour(),
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
        embed.set_footer(text=f"ID: {message.author.id}")
        await self.send_log(message.guild, embed)

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

        embed = discord.Embed(
            title="Message Edited",
            colour=random_colour(),
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
        embed.set_footer(text=f"ID: {before.author.id}")
        await self.send_log(before.guild, embed)


async def setup(bot):
    await bot.add_cog(ModLog(bot))

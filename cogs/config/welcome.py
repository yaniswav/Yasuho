import logging

import discord
from discord.ext import commands

from tools.formats import random_colour

log = logging.getLogger(__name__)


class Welcome(commands.Cog):
    """Greet new members with a configurable welcome message."""

    def __init__(self, bot):
        self.bot = bot

    def format_msg(self, template, member):
        return (
            template.replace("{user}", member.mention)
            .replace("{server}", member.guild.name)
            .replace("{count}", str(member.guild.member_count))
        )

    @commands.hybrid_group()
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome(self, ctx):
        """Welcome message related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @welcome.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome_set(self, ctx, channel: discord.TextChannel, *, message: str):
        """Set the welcome channel and message.

        Placeholders: {user}, {server}, {count}.
        """

        query = """
            INSERT INTO welcome
            (guild_id, channel_id, message)
            VALUES
            ($1, $2, $3)
            ON CONFLICT (guild_id) DO UPDATE SET channel_id = $2, message = $3;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, channel.id, message)
        embed = discord.Embed(
            title="Welcome message", colour=random_colour()
        )
        embed.add_field(name="Channel", value=channel.mention, inline=False)
        embed.add_field(name="Message", value=(message if len(message) <= 1024 else message[:1021] + "..."), inline=False)
        await ctx.send(embed=embed)

    @welcome.command(name="disable")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome_disable(self, ctx):
        """Disable the welcome message for your guild."""

        query = """DELETE FROM welcome WHERE guild_id = $1;"""

        await self.bot.db_pool.execute(query, ctx.guild.id)
        embed = discord.Embed(
            title="Welcome message", colour=random_colour()
        )
        embed.add_field(
            name="Disabled", value="Welcome messages have been turned off.", inline=False
        )
        await ctx.send(embed=embed)

    @welcome.command(name="test")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcome_test(self, ctx):
        """Preview the configured welcome message."""

        query = """SELECT channel_id, message FROM welcome WHERE guild_id = $1;"""

        row = await self.bot.db_pool.fetchrow(query, ctx.guild.id)
        if not row:
            await ctx.send("Welcome messages are not configured for this guild.")
            return

        await ctx.send(self.format_msg(row["message"], ctx.author))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if await self.bot.db_pool.fetchval("SELECT 1 FROM blbot WHERE member_id = $1;", member.id):
            return

        query = """SELECT channel_id, message FROM welcome WHERE guild_id = $1;"""

        row = await self.bot.db_pool.fetchrow(query, member.guild.id)
        if not row:
            return

        channel = member.guild.get_channel(row["channel_id"])
        if channel:
            try:
                await channel.send(self.format_msg(row["message"], member))
            except Exception:
                log.exception("Failed to send welcome message")


async def setup(bot):
    await bot.add_cog(Welcome(bot))

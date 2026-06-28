import logging

import discord
from discord.ext import commands

from tools.formats import random_colour

log = logging.getLogger(__name__)


class Settings(commands.Cog):
    """Server configuration commands (prefix and auto-role)."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group()
    @commands.guild_only()
    async def prefix(self, ctx):
        """Prefix related commands."""


        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @prefix.command(name="set")
    @commands.cooldown(1.0, 15.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def set_prefix(self, ctx, prefix: str):
        """Assign a Prefix to Yasuho for use in your guild."""

        query = """
            INSERT INTO prefixes
            (guild_id, prefix)
            VALUES
            ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET prefix = $3;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, prefix, prefix)
        self.bot.prefixes[ctx.guild.id] = prefix
        embed = discord.Embed(
            title="Server prefix", colour=random_colour()
        )
        embed.add_field(name="Prefix has been set to:", value=f"`{prefix}`")
        await ctx.send(embed=embed)

    @prefix.command(name="current", aliases=["list", "info"])
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def list_prefix(self, ctx):
        """List the available prefixes for your guild."""

        query = """

            SELECT prefix FROM prefixes
            WHERE guild_id = $1;

            """

        prefix = await self.bot.db_pool.fetchval(query, ctx.guild.id)
        embed = discord.Embed(
            title="Server prefix", colour=random_colour()
        )
        embed.add_field(name="Current server prefix", value=f"`{prefix}`")
        await ctx.send(embed=embed)

    @commands.hybrid_group(aliases=["auto-role"])
    @commands.guild_only()
    async def autorole(self, ctx):
        """Auto-role related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @autorole.command(name="set")
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def autorole_set(self, ctx, role: discord.Role):
        """Assign an auto role to your guild."""

        query = """
            INSERT INTO autorole
            (guild_id, role_id)
            VALUES
            ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET role_id = $3;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, role.id, role.id)
        embed = discord.Embed(
            title="Auto-role role", colour=random_colour()
        )
        embed.add_field(name="Auto-role has been set to:", value=f"<@&{role.id}>")
        await ctx.send(embed=embed)

    @autorole.command(name="remove")
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def autorole_rm(self, ctx):
        """Remove auto role from your guild."""

        query = """DELETE FROM autorole WHERE guild_id = $1 ;"""

        try:
            await self.bot.db_pool.execute(query, ctx.guild.id)
            embed = discord.Embed(
                title="Auto-role", colour=random_colour()
            )
            embed.add_field(
                name="Auto-role has been remove from the guild", value="\u200B"
            )
            await ctx.send(embed=embed)

        except Exception:
            log.exception("Failed to remove auto-role")

    @autorole.command(name="info", aliases=["current"])
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def autorole_info(self, ctx):
        """Auto-role of your guild."""

        query = """

            SELECT role_id FROM autorole
            WHERE guild_id = $1;

            """

        role = await self.bot.db_pool.fetchval(query, ctx.guild.id)

        if role is not None:

            embed = discord.Embed(
                title="Auto-role", colour=random_colour()
            )
            embed.add_field(name="Current auto-role", value=f"<@&{role}>")
            await ctx.send(embed=embed)

        else:
            embed = discord.Embed(
                title="Auto-role", colour=random_colour()
            )
            embed.add_field(name="Current auto-role", value="`None`")
            await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))

import discord
from discord.ext import commands
import asyncio
import random
import datetime

class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(pass_context=True)
    @commands.guild_only()
    async def prefix(self, ctx):
        """Prefix related commands."""

        query = """
        
            SELECT prefix FROM prefixes 
            WHERE guild_id = $1;
            
            """

        cprefix = await self.bot.db_pool.fetchval(query, ctx.guild.id)

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @prefix.command(name="set")
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
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
        self.bot.cache[ctx.guild.id] = prefix
        embed = discord.Embed(
            title="Server prefix", colour=random.randint(0x000000, 0xFFFFFF)
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

        cprefix = await self.bot.db_pool.fetchval(query, ctx.guild.id)
        embed = discord.Embed(
            title="Server prefix", colour=random.randint(0x000000, 0xFFFFFF)
        )
        embed.add_field(name="Current server prefix", value=f"`{cprefix}`")
        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))

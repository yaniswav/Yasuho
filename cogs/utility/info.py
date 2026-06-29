import logging

import discord
from discord.ext import commands

from tools.formats import random_colour

log = logging.getLogger(__name__)


class Info(commands.Cog):
    """Informational commands about users, the server and the bot."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="userinfo", aliases=["whois", "ui"])
    @commands.guild_only()
    async def userinfo(self, ctx, member: discord.Member = None):
        """Displays information about a member of the guild."""

        member = member or ctx.author

        embed = discord.Embed(
            title=f"User info - {member}",
            colour=random_colour(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Display name", value=member.display_name)
        embed.add_field(name="ID", value=member.id)
        embed.add_field(name="Mention", value=member.mention)
        embed.add_field(
            name="Account created",
            value=f"{discord.utils.format_dt(member.created_at, 'F')} "
            f"({discord.utils.format_dt(member.created_at, 'R')})",
            inline=False,
        )

        if member.joined_at is not None:
            embed.add_field(
                name="Joined server",
                value=discord.utils.format_dt(member.joined_at, "F"),
                inline=False,
            )

        embed.add_field(name="Top role", value=member.top_role.mention)
        embed.add_field(name="Role count", value=len(member.roles) - 1)
        embed.add_field(name="Is bot", value="Yes" if member.bot else "No")

        # Banners require a REST fetch; show it and opportunistically archive it.
        try:
            full = await self.bot.fetch_user(member.id)
            if full.banner:
                embed.set_image(url=full.banner.url)
            ah = self.bot.get_cog("AvatarHistory")
            if ah:
                await ah.capture_banner(member)
        except Exception:
            log.exception("failed to fetch/capture banner for %s", member.id)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="serverinfo", aliases=["guildinfo", "si"])
    @commands.guild_only()
    async def serverinfo(self, ctx):
        """Displays information about the current guild."""

        guild = ctx.guild

        text_channels = len(guild.text_channels)
        voice_channels = len(guild.voice_channels)

        embed = discord.Embed(
            title=f"Server info - {guild.name}",
            colour=random_colour(),
        )
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
        embed.add_field(name="Name", value=guild.name)
        embed.add_field(name="ID", value=guild.id)
        embed.add_field(
            name="Owner",
            value=guild.owner.mention if guild.owner else "Unknown",
        )
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(guild.created_at, "F"),
            inline=False,
        )
        embed.add_field(name="Members", value=guild.member_count)
        embed.add_field(name="Text channels", value=text_channels)
        embed.add_field(name="Voice channels", value=voice_channels)
        embed.add_field(name="Roles", value=len(guild.roles))
        embed.add_field(name="Boost tier", value=f"Tier {guild.premium_tier}")
        embed.add_field(name="Boosts", value=guild.premium_subscription_count)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="avatar", aliases=["av", "pfp"])
    async def avatar(self, ctx, member: discord.Member = None):
        """Displays the avatar of a member."""

        member = member or ctx.author

        embed = discord.Embed(
            title=f"Avatar - {member}",
            colour=random_colour(),
        )
        embed.set_image(url=member.display_avatar.url)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="ping")
    async def ping(self, ctx):
        """Shows the bot's websocket latency."""

        embed = discord.Embed(
            title="Pong!",
            description=f"Latency: **{round(self.bot.latency * 1000)} ms**",
            colour=random_colour(),
        )

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="botinfo", aliases=["about", "info"])
    async def botinfo(self, ctx):
        """Displays information about the bot."""

        total_users = sum(
            g.member_count for g in self.bot.guilds if g.member_count is not None
        )

        embed = discord.Embed(
            title="Bot info",
            colour=random_colour(),
        )
        embed.add_field(name="Servers", value=len(self.bot.guilds))
        embed.add_field(name="Users", value=total_users)
        embed.add_field(name="discord.py", value=discord.__version__)
        embed.add_field(
            name="Websocket latency",
            value=f"{round(self.bot.latency * 1000)} ms",
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Info(bot))

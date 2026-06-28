import logging
import re
import time

import discord
from discord.ext import commands

from tools.formats import random_colour

log = logging.getLogger(__name__)


class AutoMod(commands.Cog):
    """Automatic moderation: anti-link and anti-spam filtering."""

    url_re = re.compile(r"https?://\S+|discord\.gg/\S+", re.IGNORECASE)

    def __init__(self, bot):
        self.bot = bot
        self._spam = {}
        self._settings = {}

    @commands.hybrid_group(name="automod")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def automod(self, ctx):
        """Automatic moderation related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @automod.command(name="antilink")
    async def automod_antilink(self, ctx, mode: bool):
        """Enable or disable link filtering for this guild."""

        query = """
            INSERT INTO automod (guild_id, antilink)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET antilink = $2;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, mode)
        current = self._settings.get(ctx.guild.id)
        antispam = bool(current["antispam"]) if current else False
        self._settings[ctx.guild.id] = {"antilink": mode, "antispam": antispam}
        embed = discord.Embed(
            title="Auto-mod", colour=random_colour()
        )
        embed.add_field(
            name="Anti-link", value="Enabled" if mode else "Disabled"
        )
        await ctx.send(embed=embed)

    @automod.command(name="antispam")
    async def automod_antispam(self, ctx, mode: bool):
        """Enable or disable spam filtering for this guild."""

        query = """
            INSERT INTO automod (guild_id, antispam)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET antispam = $2;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, mode)
        current = self._settings.get(ctx.guild.id)
        antilink = bool(current["antilink"]) if current else False
        self._settings[ctx.guild.id] = {"antilink": antilink, "antispam": mode}
        embed = discord.Embed(
            title="Auto-mod", colour=random_colour()
        )
        embed.add_field(
            name="Anti-spam", value="Enabled" if mode else "Disabled"
        )
        await ctx.send(embed=embed)

    @automod.command(name="status")
    async def automod_status(self, ctx):
        """Show the current auto-mod settings for this guild."""

        s = await self.get_settings(ctx.guild.id)
        antilink = bool(s["antilink"]) if s else False
        antispam = bool(s["antispam"]) if s else False

        embed = discord.Embed(
            title="Auto-mod status", colour=random_colour()
        )
        embed.add_field(
            name="Anti-link", value="Enabled" if antilink else "Disabled"
        )
        embed.add_field(
            name="Anti-spam", value="Enabled" if antispam else "Disabled"
        )
        await ctx.send(embed=embed)

    async def get_settings(self, guild_id):
        if guild_id in self._settings:
            return self._settings[guild_id]

        query = """SELECT antilink, antispam FROM automod WHERE guild_id = $1;"""
        row = await self.bot.db_pool.fetchrow(query, guild_id)
        self._settings[guild_id] = row
        return row

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        if message.author.guild_permissions.manage_messages:
            return

        s = await self.get_settings(message.guild.id)
        if not s:
            return

        if s["antilink"] and self.url_re.search(message.content):
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention} links are not allowed here.",
                    delete_after=5,
                )
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                log.exception("Failed to handle link message")
            return

        if s["antispam"]:
            key = (message.guild.id, message.author.id)
            now = time.time()
            timestamps = self._spam.setdefault(key, [])
            timestamps.append(now)
            recent = [t for t in timestamps if now - t <= 5]
            if recent:
                self._spam[key] = recent
            else:
                self._spam.pop(key, None)
                return

            if len(recent) > 5:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"{message.author.mention} stop spamming.",
                        delete_after=5,
                    )
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    log.exception("Failed to handle spam message")


async def setup(bot):
    await bot.add_cog(AutoMod(bot))

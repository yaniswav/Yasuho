import logging
import urllib.parse

import aiohttp
import discord
from discord.ext import commands

from tools.formats import random_colour

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


class Utility(commands.Cog):
    """Handy utility commands."""

    def __init__(self, bot):
        self.bot = bot
        self._snipes = {}

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or not message.content:
            return
        self._snipes[message.channel.id] = (
            message.content,
            message.author,
            message.created_at,
        )

    @commands.hybrid_command()
    @commands.guild_only()
    async def snipe(self, ctx):
        """Show the last deleted message in this channel."""

        data = self._snipes.get(ctx.channel.id)
        if not data:
            return await ctx.send("Nothing to snipe.")

        content, author, when = data
        embed = discord.Embed(
            description=content,
            colour=random_colour(),
            timestamp=when,
        )
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    async def poll(self, ctx, *, question: str):
        """Create a simple yes/no poll."""

        if len(question) > 4000:
            return await ctx.send("Question is too long.")

        embed = discord.Embed(
            title="Poll",
            description=question,
            colour=random_colour(),
        )
        embed.set_footer(
            text=f"Asked by {ctx.author}", icon_url=ctx.author.display_avatar.url
        )
        m = await ctx.send(embed=embed)
        await m.add_reaction("\U0001F44D")
        await m.add_reaction("\U0001F44E")

    @commands.hybrid_command()
    async def quickpoll(self, ctx, *, args: str):
        """Create a multiple choice poll: quickpoll question | option1 | option2 ..."""

        parts = [p.strip() for p in args.split("|")]
        question, options = parts[0], parts[1:]
        if len(options) < 2 or len(options) > 10:
            return await ctx.send("Usage: quickpoll question | option1 | option2 ...")
        if any(len(o) > 240 for o in options) or len(question) > 4000:
            return await ctx.send("Question/options are too long.")
        if len(question) + sum(len(o) for o in options) > 5500:
            return await ctx.send("Poll text is too long.")

        letters = [
            "\U0001F1E6",
            "\U0001F1E7",
            "\U0001F1E8",
            "\U0001F1E9",
            "\U0001F1EA",
            "\U0001F1EB",
            "\U0001F1EC",
            "\U0001F1ED",
            "\U0001F1EE",
            "\U0001F1EF",
        ]

        embed = discord.Embed(
            title="Poll",
            description=question,
            colour=random_colour(),
        )
        for i, option in enumerate(options):
            embed.add_field(
                name=f"{letters[i]} {option}", value="​", inline=False
            )
        embed.set_footer(
            text=f"Asked by {ctx.author}", icon_url=ctx.author.display_avatar.url
        )

        m = await ctx.send(embed=embed)
        for i in range(len(options)):
            await m.add_reaction(letters[i])

    @commands.hybrid_command()
    async def translate(self, ctx, *, text: str):
        """Translate text to English (auto-detect source language)."""

        async with ctx.typing():
            try:
                url = (
                    "https://translate.googleapis.com/translate_a/single"
                    "?client=gtx&sl=auto&tl=en&dt=t&q="
                    + urllib.parse.quote(text)
                )
                async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as s:
                    async with s.get(url) as r:
                        data = await r.json()

                translated = "".join(seg[0] for seg in data[0])
                embed = discord.Embed(
                    description=translated,
                    colour=random_colour(),
                )
                embed.set_footer(text="auto -> en (unofficial)")
                await ctx.send(embed=embed)

            except Exception:
                log.exception("translation failed")
                await ctx.send("Translation failed.")


async def setup(bot):
    await bot.add_cog(Utility(bot))

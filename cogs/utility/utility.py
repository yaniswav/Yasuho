import datetime
import logging
import urllib.parse

import aiohttp
import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.http import TIMEOUT
from tools.i18n import _

log = logging.getLogger(__name__)


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
            return await ctx.send(_("Nothing to snipe."))

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
        """Create a native yes/no poll (runs for 24 hours)."""

        question = question.strip()
        if not question:
            return await ctx.send(_("Please give a question to ask."))
        if len(question) > 300:
            return await ctx.send(
                _("The poll question must be 300 characters or fewer.")
            )

        poll = discord.Poll(question=question, duration=datetime.timedelta(hours=24))
        poll.add_answer(text=_("Yes"), emoji="\U0001F44D")
        poll.add_answer(text=_("No"), emoji="\U0001F44E")

        try:
            await ctx.send(poll=poll)
        except (discord.HTTPException, ValueError):
            log.exception("Failed to send native poll")
            await ctx.send(_("I could not create that poll here."))

    @commands.hybrid_command()
    async def quickpoll(self, ctx, *, args: str):
        """Multiple-choice poll: quickpoll question | option 1 | option 2 ..."""

        parts = [p.strip() for p in args.split("|")]
        question = parts[0]
        options = [p for p in parts[1:] if p]

        if not question:
            return await ctx.send(
                _(
                    "Give a question and options: `quickpoll question | option 1 | option 2`"
                )
            )
        if len(options) < 2:
            return await ctx.send(_("A poll needs at least two options."))
        if len(options) > 10:
            return await ctx.send(_("A poll can have at most 10 options."))
        if len(question) > 300:
            return await ctx.send(_("The question must be 300 characters or fewer."))
        if any(len(option) > 55 for option in options):
            return await ctx.send(_("Each option must be 55 characters or fewer."))

        poll = discord.Poll(question=question, duration=datetime.timedelta(hours=24))
        for option in options:
            poll.add_answer(text=option)

        try:
            await ctx.send(poll=poll)
        except (discord.HTTPException, ValueError):
            log.exception("Failed to send native quickpoll")
            await ctx.send(_("I could not create that poll here."))

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
                async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                    async with s.get(url) as r:
                        data = await r.json()

                translated = "".join(seg[0] for seg in data[0])
                embed = discord.Embed(
                    description=translated,
                    colour=random_colour(),
                )
                embed.set_footer(text=_("auto -> en (unofficial)"))
                await ctx.send(embed=embed)

            except Exception:
                log.exception("translation failed")
                await ctx.send(_("Translation failed."))


async def setup(bot):
    await bot.add_cog(Utility(bot))

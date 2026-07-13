import logging
import urllib.parse

import aiohttp
import discord
import requests
import wikipedia
from discord.ext import commands

from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.http import TIMEOUT
from tools.i18n import _

log = logging.getLogger(__name__)


class _TimeoutRequests:
    """Thin proxy over the requests module that forces a default timeout on get.

    The wikipedia library calls requests.get() with no timeout (see its
    _wiki_request), so a hung upstream would tie up an executor thread forever.
    We swap the name the wikipedia module resolves to for this proxy, which
    forwards everything but caps get() - without touching the global requests
    module every other caller shares.
    """

    def __init__(self, timeout):
        self._timeout = timeout

    def get(self, *args, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return requests.get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(requests, name)


# Reuse the same cap as every aiohttp call (tools.http.TIMEOUT) as plain seconds.
wikipedia.wikipedia.requests = _TimeoutRequests(TIMEOUT.total)


class SearchWeb(commands.Cog):
    """Commands that search the web and external APIs."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(aliases=["wikipedia"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(query="The topic to search for.")
    async def wiki(self, ctx, *, query: str):
        """Search Wikipedia for a short summary of a topic."""

        async with ctx.typing():

            def _w():
                wikipedia.set_lang("en")
                return wikipedia.summary(query, sentences=5)

            try:
                summary = await self.bot.loop.run_in_executor(None, _w)
                embed = discord.Embed(
                    title=query,
                    description=summary,
                    colour=random_colour(),
                )
                await ctx.send(embed=embed)

            except wikipedia.exceptions.DisambiguationError as e:
                options = ", ".join(e.options[:5])
                embed = discord.Embed(
                    title=_("Disambiguation"),
                    description=_(
                        "That term is ambiguous. Did you mean: {options}?"
                    ).format(options=options),
                    colour=random_colour(),
                )
                await ctx.send(embed=embed)

            except wikipedia.exceptions.PageError:
                await ctx.send(_("No page found."))

            except Exception:
                log.exception("failed to fetch wikipedia summary")
                await ctx.send(_("Something went wrong while searching Wikipedia."))

    @commands.hybrid_command(aliases=["saucefinder", "imgsource"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(url="The image URL (omit to use your attached image).")
    async def imagesource(self, ctx, url: str = None):
        """Build a Google reverse image search link for an image URL or attachment."""

        if url is None and ctx.message.attachments:
            url = ctx.message.attachments[0].url

        if not url:
            return await ctx.send(_("Provide an image URL or attach an image."))

        link = "https://www.google.com/searchbyimage?image_url=" + urllib.parse.quote(
            url, safe=""
        )
        embed = discord.Embed(
            title=_("Reverse image search"),
            description=_("[Click here to search for the source]({link})").format(
                link=link
            ),
            colour=random_colour(),
        )
        embed.set_thumbnail(url=url)
        await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(username="The osu! username to look up.")
    async def osu(self, ctx, *, username: str):
        """Look up an osu! player's stats."""

        key = config_loader.get("APITokens", "osuKey", fallback=None)
        if not key:
            return await ctx.send(_("osu! is not configured."))

        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                    async with s.get(
                        "https://osu.ppy.sh/api/get_user",
                        params={"k": key, "u": username},
                    ) as r:
                        data = await r.json()

                if not data:
                    return await ctx.send(_("No osu! user found."))

                u = data[0]
                embed = discord.Embed(
                    title=_("osu! stats for {username}").format(
                        username=u["username"]
                    ),
                    colour=random_colour(),
                )
                embed.add_field(name=_("Rank"), value=u["pp_rank"])
                embed.add_field(name=_("PP"), value=u["pp_raw"])
                embed.add_field(name=_("Accuracy"), value=u["accuracy"])
                embed.add_field(name=_("Level"), value=u["level"])
                embed.add_field(name=_("Playcount"), value=u["playcount"])
                embed.add_field(name=_("Country"), value=u["country"])
                embed.set_thumbnail(url=f"https://a.ppy.sh/{u['user_id']}")
                await ctx.send(embed=embed)

            except Exception:
                log.exception("failed to fetch osu! user")
                await ctx.send(_("Something went wrong while fetching osu! data."))

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(username="The Minecraft username to look up.")
    async def minecraft(self, ctx, username: str):
        """Look up a Minecraft account and render its skin."""

        async with ctx.typing():
            try:
                safe_name = urllib.parse.quote(username, safe="")
                async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                    async with s.get(
                        f"https://api.mojang.com/users/profiles/minecraft/{safe_name}"
                    ) as r:
                        if r.status != 200:
                            return await ctx.send(_("No such Minecraft account."))
                        data = await r.json()

                uuid = data["id"]
                embed = discord.Embed(
                    title=_("Minecraft account: {name}").format(name=data["name"]),
                    colour=random_colour(),
                )
                embed.add_field(name=_("UUID"), value=uuid)
                embed.set_image(
                    url=f"https://crafatar.com/renders/body/{uuid}?overlay"
                )
                await ctx.send(embed=embed)

            except Exception:
                log.exception("failed to fetch minecraft account")
                await ctx.send(
                    _("Something went wrong while fetching Minecraft data.")
                )


async def setup(bot):
    await bot.add_cog(SearchWeb(bot))

import logging
import urllib.parse

import discord
import requests
import wikipedia
from discord.ext import commands

from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.http import TIMEOUT, get_session
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

    # ------------------------------------------------------------------
    # /lookup - the one "look something up somewhere else" group.
    #
    # Discord caps a bot at 100 GLOBAL top-level application commands and this
    # tree had grown to 101, so a cog was dying at load with
    # CommandLimitReached (cogs/utility/utility.py, taking /poll, /quickpoll,
    # /snipe and /translate down with it). A GROUP costs exactly ONE slot no
    # matter how many subcommands it holds, so folding these six lookups here
    # hands five slots back with nothing removed.
    #
    # Two of the six (apod, weather) have their bodies in the sibling Meta cog
    # and a hybrid subcommand cannot live in a different cog from its group
    # parent, so those two are thin wrappers that delegate through
    # bot.get_cog("Meta") - the same documented cross-cog seam /levelconfig xp
    # uses for LevelAdmin. See _require below.
    #
    # Prefix users lose nothing: the four commands owned here keep their names
    # and aliases as root prefix-only shims at the bottom of this cog, and
    # ?apod / ?weather stay root prefix-only commands in cogs/utility/meta.py.
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="lookup")
    async def lookup(self, ctx):
        """Look something up: Wikipedia, osu!, Minecraft, images, space, weather."""

        if ctx.invoked_subcommand is None:
            await ctx.send(
                _(
                    "Look something up with one of these:\n"
                    "- `{p}lookup wiki <topic>` - a Wikipedia summary\n"
                    "- `{p}lookup osu <username>` - osu! player stats\n"
                    "- `{p}lookup minecraft <username>` - a Minecraft account\n"
                    "- `{p}lookup imagesource <url>` - reverse image search\n"
                    "- `{p}lookup apod` - NASA's Astronomy Picture of the Day\n"
                    "- `{p}lookup weather <city>` - the current weather"
                ).format(p=ctx.clean_prefix)
            )

    async def _require(self, ctx, cog_name):
        """Return a sibling cog by name, or send a friendly refusal and None.

        The seam behind the folded /lookup apod and /lookup weather
        subcommands: their bodies live in the Meta cog (fine-grained by
        concern), and each wrapper delegates to a cmd_* method there. Looked up
        by name - the house cross-cog pattern - and guarded so a missing
        sibling degrades to a refusal rather than a crash (never happens in
        production; keeps this cog testable in isolation).
        """

        cog = self.bot.get_cog(cog_name)
        if cog is None:
            await ctx.send(_("That lookup isn't available right now."))
        return cog

    @lookup.command(name="wiki")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(query="The topic to search for.")
    async def lookup_wiki(self, ctx, *, query: str):
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

    @lookup.command(name="imagesource")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(url="The image URL (omit to use your attached image).")
    async def lookup_imagesource(self, ctx, url: str = None):
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

    @lookup.command(name="osu")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(username="The osu! username to look up.")
    async def lookup_osu(self, ctx, *, username: str):
        """Look up an osu! player's stats."""

        key = config_loader.get("APITokens", "osuKey", fallback=None)
        if not key:
            return await ctx.send(_("osu! is not configured."))

        async with ctx.typing():
            try:
                async with get_session(self.bot).get(
                    "https://osu.ppy.sh/api/get_user",
                    params={"k": key, "u": username},
                    timeout=TIMEOUT,
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

    @lookup.command(name="minecraft")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(username="The Minecraft username to look up.")
    async def lookup_minecraft(self, ctx, username: str):
        """Look up a Minecraft account and render its skin."""

        async with ctx.typing():
            try:
                safe_name = urllib.parse.quote(username, safe="")
                async with get_session(self.bot).get(
                    f"https://api.mojang.com/users/profiles/minecraft/{safe_name}",
                    timeout=TIMEOUT,
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

    # -- cross-cog subcommands: bodies live in cogs/utility/meta.py ---------
    # A hybrid subcommand must live in the SAME cog as its group parent, so
    # these two wrappers sit here while the work stays in Meta (the house
    # /levelconfig xp -> LevelAdmin pattern). ?apod and ?weather are still
    # plain root commands over there, so the prefix side is untouched.
    @lookup.command(name="apod")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def lookup_apod(self, ctx):
        """Show NASA's Astronomy Picture of the Day."""

        cog = await self._require(ctx, "Meta")
        if cog is not None:
            await cog.cmd_apod(ctx)

    @lookup.command(name="weather")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(city="The city to look up.")
    async def lookup_weather(self, ctx, *, city: str):
        """Show the current weather for a given city."""

        cog = await self._require(ctx, "Meta")
        if cog is not None:
            await cog.cmd_weather(ctx, city)

    # ------------------------------------------------------------------
    # Prefix compatibility shims for the commands folded into /lookup.
    #
    # commands.command (NOT hybrid): text-side only, so they register no
    # application command and cost none of the 100 global slash slots. Each
    # keeps the exact name, aliases, cooldown and short_doc of the standalone
    # command it replaces, so `?wiki`, `?wikipedia`, `?osu`, `?minecraft`,
    # `?imagesource`, `?saucefinder` and `?imgsource` are unchanged, and ?help
    # still lists them one per line.
    #
    # Each shim delegates through Command.__call__ (discord.py's documented
    # "call the callback directly" API) so the bodies live in one place only.
    # The cooldown is declared on the shim as well as on the subcommand: the
    # two are separate commands with separate buckets, so each surface needs
    # its own.
    # ------------------------------------------------------------------
    @commands.command(name="wiki", aliases=["wikipedia"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def wiki_prefix(self, ctx, *, query: str):
        """Search Wikipedia for a short summary of a topic."""

        await self.lookup_wiki(ctx, query=query)

    @commands.command(name="imagesource", aliases=["saucefinder", "imgsource"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def imagesource_prefix(self, ctx, url: str = None):
        """Build a Google reverse image search link for an image URL or attachment."""

        await self.lookup_imagesource(ctx, url)

    @commands.command(name="osu")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def osu_prefix(self, ctx, *, username: str):
        """Look up an osu! player's stats."""

        await self.lookup_osu(ctx, username=username)

    @commands.command(name="minecraft")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def minecraft_prefix(self, ctx, username: str):
        """Look up a Minecraft account and render its skin."""

        await self.lookup_minecraft(ctx, username)


async def setup(bot):
    await bot.add_cog(SearchWeb(bot))

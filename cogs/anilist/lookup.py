import datetime

import discord
from discord.ext import commands

from .account import AccountMixin
from .helpers import (
    SEASONS,
    _clean_description,
    _current_season,
    channel_allows_adult,
)
from .queries import CHARACTER_QUERY, STUDIO_QUERY
from tools.formats import random_colour
from tools.i18n import _


class CharacterCard(discord.ui.LayoutView):
    """A looked-up AniList character as a Components V2 card.

    Same family as :class:`~cogs.anilist.airing.AiringCard`: a
    :class:`~discord.ui.Container` accented with a random colour (the character
    query carries no cover colour to derive one from - matches the prior embed's
    ``random_colour()``), a ``###`` name heading plus the cleaned description, and
    a portrait :class:`~discord.ui.Thumbnail` accessory (its ``description`` alt
    text is the character name) when the API returned one. No ``isAdult`` field
    exists on a character, so unlike the media cards there is nothing to suppress.
    A trailing link button to the AniList page mirrors the prior embed's
    title-as-link.
    """

    def __init__(self, char, *, timeout=None):
        super().__init__(timeout=timeout)
        self._build(char or {})

    def _build(self, char):
        name = char.get("name") or {}
        full = name.get("full") or _("Unknown")
        native = name.get("native")
        title = f"{full} ({native})" if native else full
        url = char.get("siteUrl")
        description = _clean_description(char.get("description"))

        container = discord.ui.Container(accent_colour=random_colour())
        texts = [discord.ui.TextDisplay("### " + title)]
        if description:
            texts.append(discord.ui.TextDisplay(description))

        image = char.get("image") or {}
        thumb = image.get("large")
        if thumb:
            container.add_item(
                discord.ui.Section(
                    *texts,
                    accessory=discord.ui.Thumbnail(thumb, description=title[:256]),
                )
            )
        else:
            for text in texts:
                container.add_item(text)

        if url:
            container.add_item(discord.ui.Separator())
            action_row = discord.ui.ActionRow()
            action_row.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.link, label=_("AniList"), url=url
                )
            )
            container.add_item(action_row)

        self.add_item(container)


class StudioCard(discord.ui.LayoutView):
    """A looked-up AniList animation studio as a Components V2 card.

    Same family as :class:`CharacterCard`: a random-accented
    :class:`~discord.ui.Container` with a ``###`` name heading, the "Popular
    productions" field (when the studio has any) rendered as a bold label plus a
    bullet list - the established field->text conversion used elsewhere in this
    package - and a trailing link button to the AniList page. No thumbnail: the
    studio query carries no image field, so there never was a cover to show.
    """

    def __init__(self, studio, *, timeout=None):
        super().__init__(timeout=timeout)
        self._build(studio or {})

    def _build(self, studio):
        title = studio.get("name") or _("Unknown studio")
        url = studio.get("siteUrl")

        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay("### " + title))

        nodes = ((studio.get("media") or {}).get("nodes")) or []
        titles = [
            (n.get("title") or {}).get("romaji")
            for n in nodes
            if (n.get("title") or {}).get("romaji")
        ]
        if titles:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    "**"
                    + _("Popular productions")
                    + "**\n"
                    + "\n".join(f"- {t}" for t in titles[:10])
                )
            )

        if url:
            container.add_item(discord.ui.Separator())
            action_row = discord.ui.ActionRow()
            action_row.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.link, label=_("AniList"), url=url
                )
            )
            container.add_item(action_row)

        self.add_item(container)


class LookupMixin:
    """AniList lookup commands (no auth required).

    ``/anime`` and ``/manga`` stay top-level: they are the two people reach for
    constantly and the package's whole reason to exist. The five secondary
    lookups (trending, popular, seasonal, character, studio) were folded into
    the existing ``/anilist`` group, freeing five of the global slash slots.

    Why: Discord caps a bot at 100 GLOBAL top-level application commands. This
    tree had reached 101, so whichever cog happened to load last died with
    CommandLimitReached and its commands vanished from production. A group
    costs exactly ONE slot no matter how many subcommands it holds, and
    ``/anilist`` already existed - these five cost nothing at all now.

    This mixin is part of the SAME cog as ``AccountMixin`` (see
    cogs/anilist/__init__.py: both are bases of ``AniList``), so parenting
    subcommands onto ``AccountMixin.anilist`` is legal - a hybrid subcommand
    may not live in a different cog from its group parent. It is also exactly
    how airing.py, schedule.py and chapters.py already attach theirs.

    Every folded command keeps its prefix form: ``?trending``, ``?popular``,
    ``?seasonal``, ``?character`` and ``?studio`` are root prefix-only shims at
    the bottom of this mixin, and a prefix-only command registers no
    application command, so they cost no slots.
    """

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(search="The anime title to look up.")
    async def anime(self, ctx, *, search: str):
        """Look up an anime on AniList."""

        await self._media_lookup(ctx, search, "ANIME")

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(search="The manga title to look up.")
    async def manga(self, ctx, *, search: str):
        """Look up a manga on AniList."""

        await self._media_lookup(ctx, search, "MANGA")

    @AccountMixin.anilist.command(name="trending")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_trending(self, ctx):
        """Browse the anime trending on AniList right now."""

        await self._browse(
            ctx,
            {"sort": ["TRENDING_DESC"], "type": "ANIME"},
            "ANIME",
            _("Trending anime"),
        )

    @AccountMixin.anilist.command(name="popular")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_popular(self, ctx):
        """Browse the most popular anime on AniList."""

        await self._browse(
            ctx,
            {"sort": ["POPULARITY_DESC"], "type": "ANIME"},
            "ANIME",
            _("Popular anime"),
        )

    @AccountMixin.anilist.command(name="seasonal")
    @commands.cooldown(1, 10, commands.BucketType.user)
    @discord.app_commands.describe(
        season="WINTER, SPRING, SUMMER, or FALL (defaults to the current season).",
        year="The year to browse (defaults to the current year).",
    )
    async def anilist_seasonal(self, ctx, season: str = None, year: int = None):
        """Browse anime from a season (defaults to the current season)."""

        if season:
            season = season.upper()
            if season not in SEASONS:
                return await ctx.send(
                    _("Season must be one of: WINTER, SPRING, SUMMER, FALL.")
                )
            if year is None:
                year = datetime.datetime.now(datetime.timezone.utc).year
        else:
            current_season, current_year = _current_season()
            season = current_season
            if year is None:
                year = current_year

        allow_adult = channel_allows_adult(ctx.channel)
        async with ctx.typing():
            kwargs, view = await self._seasonal_payload(
                ctx.author.id, season, year, allow_adult
            )
            message = await ctx.send(**kwargs)
        if view is not None:
            view.message = message

    @AccountMixin.anilist.command(name="character")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(search="The character name to look up.")
    async def anilist_character(self, ctx, *, search: str):
        """Look up a character on AniList."""

        async with ctx.typing():
            data = await self._graphql(CHARACTER_QUERY, {"search": search})
            char = ((data or {}).get("data") or {}).get("Character")
            if not char:
                return await ctx.send(_("No character found."))

            await ctx.send(view=CharacterCard(char))

    @AccountMixin.anilist.command(name="studio")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @discord.app_commands.describe(search="The studio name to look up.")
    async def anilist_studio(self, ctx, *, search: str):
        """Look up an animation studio on AniList."""

        async with ctx.typing():
            data = await self._graphql(STUDIO_QUERY, {"search": search})
            studio = ((data or {}).get("data") or {}).get("Studio")
            if not studio:
                return await ctx.send(_("No studio found."))

            await ctx.send(view=StudioCard(studio))

    # ------------------------------------------------------------------
    # Prefix compatibility shims for the commands folded into /anilist.
    #
    # commands.command (NOT hybrid): text-side only, so they register no
    # application command and cost none of the 100 global slash slots. Each
    # keeps the exact name, cooldown and short_doc of the standalone command
    # it replaces, so `?trending`, `?popular`, `?seasonal`, `?character` and
    # `?studio` are unchanged and ?help still lists them one per line.
    #
    # Each shim delegates through Command.__call__ (discord.py's documented
    # "call the callback directly" API) so the bodies live in one place only.
    # The cooldown is declared on both surfaces: they are separate commands
    # with separate buckets, so each needs its own.
    # ------------------------------------------------------------------
    @commands.command(name="trending")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def trending_prefix(self, ctx):
        """Browse the anime trending on AniList right now."""

        await self.anilist_trending(ctx)

    @commands.command(name="popular")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def popular_prefix(self, ctx):
        """Browse the most popular anime on AniList."""

        await self.anilist_popular(ctx)

    @commands.command(name="seasonal")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def seasonal_prefix(self, ctx, season: str = None, year: int = None):
        """Browse anime from a season (defaults to the current season)."""

        await self.anilist_seasonal(ctx, season, year)

    @commands.command(name="character")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def character_prefix(self, ctx, *, search: str):
        """Look up a character on AniList."""

        await self.anilist_character(ctx, search=search)

    @commands.command(name="studio")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def studio_prefix(self, ctx, *, search: str):
        """Look up an animation studio on AniList."""

        await self.anilist_studio(ctx, search=search)

import datetime

import discord
from discord.ext import commands

from .components import SeasonView
from .helpers import SEASONS, _clean_description, _current_season
from .queries import CHARACTER_QUERY, PAGE_QUERY, STUDIO_QUERY
from tools.formats import random_colour
from tools.i18n import _


class LookupMixin:
    """AniList lookup commands (no auth required)."""

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anime(self, ctx, *, search: str):
        """Look up an anime on AniList."""

        await self._media_lookup(ctx, search, "ANIME")

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def manga(self, ctx, *, search: str):
        """Look up a manga on AniList."""

        await self._media_lookup(ctx, search, "MANGA")

    @commands.hybrid_command()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def trending(self, ctx):
        """Browse the anime trending on AniList right now."""

        await self._browse(
            ctx,
            {"sort": ["TRENDING_DESC"], "type": "ANIME"},
            "ANIME",
            _("Trending anime"),
        )

    @commands.hybrid_command()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def popular(self, ctx):
        """Browse the most popular anime on AniList."""

        await self._browse(
            ctx,
            {"sort": ["POPULARITY_DESC"], "type": "ANIME"},
            "ANIME",
            _("Popular anime"),
        )

    @commands.hybrid_command()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def seasonal(self, ctx, season: str = None, year: int = None):
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

        async with ctx.typing():
            data = await self._graphql(
                PAGE_QUERY,
                {
                    "sort": ["POPULARITY_DESC"],
                    "type": "ANIME",
                    "season": season,
                    "seasonYear": year,
                },
            )
            media = (
                ((data or {}).get("data") or {}).get("Page") or {}
            ).get("media") or []
            if not media:
                return await ctx.send(
                    _("No anime found for {season} {year}.").format(
                        season=season.title(), year=year
                    )
                )

            view = SeasonView(self, media, ctx.author.id, season, year)
            view.message = await ctx.send(
                content=_(
                    "**{season} {year} anime** - pick one for details:"
                ).format(season=season.title(), year=year),
                view=view,
            )

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def character(self, ctx, *, search: str):
        """Look up a character on AniList."""

        async with ctx.typing():
            data = await self._graphql(CHARACTER_QUERY, {"search": search})
            char = ((data or {}).get("data") or {}).get("Character")
            if not char:
                return await ctx.send(_("No character found."))

            name = char.get("name") or {}
            full = name.get("full") or _("Unknown")
            native = name.get("native")
            title = f"{full} ({native})" if native else full

            embed = discord.Embed(
                title=title,
                url=char.get("siteUrl"),
                description=_clean_description(char.get("description")),
                colour=random_colour(),
            )
            image = char.get("image") or {}
            if image.get("large"):
                embed.set_thumbnail(url=image["large"])
            await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def studio(self, ctx, *, search: str):
        """Look up an animation studio on AniList."""

        async with ctx.typing():
            data = await self._graphql(STUDIO_QUERY, {"search": search})
            studio = ((data or {}).get("data") or {}).get("Studio")
            if not studio:
                return await ctx.send(_("No studio found."))

            embed = discord.Embed(
                title=studio.get("name") or _("Unknown studio"),
                url=studio.get("siteUrl"),
                colour=random_colour(),
            )

            nodes = ((studio.get("media") or {}).get("nodes")) or []
            titles = [
                (n.get("title") or {}).get("romaji")
                for n in nodes
                if (n.get("title") or {}).get("romaji")
            ]
            if titles:
                embed.add_field(
                    name=_("Popular productions"),
                    value="\n".join(f"- {t}" for t in titles[:10]),
                    inline=False,
                )
            await ctx.send(embed=embed)

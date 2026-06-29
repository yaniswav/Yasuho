import logging

import discord
from discord import app_commands
from discord.ext import commands

from .components import LoginView
from .helpers import REDIRECT_URI, _parse_status, _profile_colour
from .queries import (
    AUTOCOMPLETE_QUERY,
    MEDIA_LIST_QUERY,
    USER_STATS_QUERY,
    VIEWER_QUERY,
)
from tools import crypto
from tools.formats import random_colour
from tools.paginator import Paginator, paginate_lines

log = logging.getLogger(__name__)


class AccountMixin:
    """AniList account group: OAuth PIN flow plus list editing."""

    # ------------------------------------------------------------------
    # Account group (OAuth PIN flow + list editing)
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="anilist")
    async def anilist(self, ctx):
        """Link your AniList account and edit your lists."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @anilist.command(name="login")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_login(self, ctx):
        """Start linking your AniList account."""

        if not self.client_id or not self.client_secret or not crypto.is_configured():
            return await ctx.send("AniList account linking is not configured.")

        authorize_url = (
            "https://anilist.co/api/v2/oauth/authorize?client_id="
            + self.client_id
            + "&redirect_uri="
            + REDIRECT_URI
            + "&response_type=code"
        )
        instructions = (
            "Authorize the bot here:\n"
            f"{authorize_url}\n\n"
            "Authorize, copy the code AniList shows you, then press "
            "**Enter code** below (or run `/anilist code <code>`)."
        )

        view = LoginView(self, ctx.author.id)

        try:
            view.message = await ctx.author.send(instructions, view=view)
        except discord.Forbidden:
            view.message = await ctx.send(instructions, view=view, ephemeral=True)
            return

        await ctx.send("Check your DMs.")

    @anilist.command(name="code")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_code(self, ctx, *, code: str):
        """Finish linking with the PIN code AniList gave you."""

        if not self.client_id or not self.client_secret or not crypto.is_configured():
            return await ctx.send("AniList account linking is not configured.")

        # Hide the PIN if it was posted in a guild text channel.
        if ctx.message is not None and ctx.guild is not None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        name = await self._exchange_code(ctx.author.id, code)
        if name is None:
            return await ctx.send(
                "That code did not work, try `/anilist login` again.",
                ephemeral=ctx.interaction is not None,
            )

        await ctx.send(
            f"Connected as {name}!", ephemeral=ctx.interaction is not None
        )

    @anilist.command(name="logout")
    async def anilist_logout(self, ctx):
        """Unlink your AniList account."""

        await self.bot.db_pool.execute(
            "DELETE FROM anilist_tokens WHERE user_id = $1;", ctx.author.id
        )
        await ctx.send(
            "Your AniList account has been unlinked.",
            ephemeral=ctx.interaction is not None,
        )

    @anilist.command(name="update")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_update(self, ctx, *, title: str):
        """Guided update: pick the title by clicking, then edit a pre-filled form."""

        await self._update_wizard(ctx, title)

    @anilist.command(name="status")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_status(self, ctx, status: str, *, title: str):
        """Set the status of a title on your list."""

        status = _parse_status(status)
        if status is None:
            return await ctx.send(
                "Status must be one of: Watching/Reading, Completed, "
                "Planning, Paused, Dropped, Repeating."
            )

        await self._edit_flow(ctx, title, "status", status)

    @anilist.command(name="score")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_score(self, ctx, score: float, *, title: str):
        """Score a title on your AniList list."""

        if score < 0:
            return await ctx.send("Score must be zero or a positive number.")

        await self._edit_flow(ctx, title, "score", score)

    @anilist_update.autocomplete("title")
    @anilist_status.autocomplete("title")
    @anilist_score.autocomplete("title")
    async def _title_autocomplete(self, interaction, current):
        """Live AniList search powering the update/status/score 'title' option.

        Returns ``id:<mediaId>`` sentinels as choice values so a numeric title
        (e.g. the anime "86") can never be mistaken for an id in the edit flow.
        """

        try:
            current = (current or "").strip()
            if len(current) < 2:
                return []

            data = await self._graphql(AUTOCOMPLETE_QUERY, {"search": current})
            media = (
                ((data or {}).get("data") or {}).get("Page") or {}
            ).get("media") or []

            choices = []
            for item in media[:25]:
                mtype = item.get("type") or "?"
                romaji = (item.get("title") or {}).get("romaji") or "Unknown"
                year = item.get("seasonYear") or "?"
                label = f"[{mtype}] {romaji} ({year})"
                choices.append(
                    app_commands.Choice(
                        name=label[:100], value=f"id:{item.get('id')}"
                    )
                )
            return choices
        except Exception:
            log.exception("AniList title autocomplete failed")
            return []

    @anilist.command(name="profile")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_profile(self, ctx, *, name: str = None):
        """Show AniList stats for a user (defaults to your linked account)."""

        async with ctx.typing():
            if name is None:
                token = await self._get_token(ctx.author.id)
                if not token:
                    return await ctx.send(
                        "Provide a name or link your account with "
                        "`/anilist login`."
                    )
                viewer = await self._graphql(VIEWER_QUERY, {}, token=token)
                name = (
                    ((viewer or {}).get("data") or {}).get("Viewer") or {}
                ).get("name")
                if not name:
                    return await ctx.send("Could not resolve your AniList account.")

            data = await self._graphql(USER_STATS_QUERY, {"name": name})
            user = ((data or {}).get("data") or {}).get("User")
            if not user:
                return await ctx.send("No AniList user found.")

            stats = user.get("statistics") or {}
            anime = stats.get("anime") or {}
            manga = stats.get("manga") or {}
            display_name = user.get("name") or name
            site_url = user.get("siteUrl")
            avatar = (user.get("avatar") or {}).get("large")

            embed = discord.Embed(
                url=site_url,
                colour=_profile_colour(
                    (user.get("options") or {}).get("profileColor")
                )
                or random_colour(),
            )
            embed.set_author(name=display_name, url=site_url, icon_url=avatar)
            if avatar:
                embed.set_thumbnail(url=avatar)
            if user.get("bannerImage"):
                embed.set_image(url=user["bannerImage"])

            days = (anime.get("minutesWatched") or 0) / 1440
            embed.add_field(
                name="📺 Anime",
                value=(
                    f"**{anime.get('count') or 0}** titles\n"
                    f"**{days:.1f}** days watched\n"
                    f"★ **{anime.get('meanScore') or 0}**/100"
                ),
            )
            embed.add_field(
                name="📚 Manga",
                value=(
                    f"**{manga.get('count') or 0}** titles\n"
                    f"**{manga.get('chaptersRead') or 0}** chapters\n"
                    f"★ **{manga.get('meanScore') or 0}**/100"
                ),
            )

            genres = anime.get("genres") or []
            top = ", ".join(g.get("genre") for g in genres[:6] if g.get("genre"))
            if top:
                embed.add_field(name="🎭 Top genres", value=top, inline=False)

            fav = user.get("favourites") or {}

            def _titles(section):
                nodes = (fav.get(section) or {}).get("nodes") or []
                return [
                    (n.get("title") or {}).get("romaji")
                    for n in nodes
                    if (n.get("title") or {}).get("romaji")
                ]

            fav_chars = [
                (n.get("name") or {}).get("full")
                for n in ((fav.get("characters") or {}).get("nodes") or [])
                if (n.get("name") or {}).get("full")
            ]
            fav_lines = []
            if _titles("anime"):
                fav_lines.append("📺 " + ", ".join(_titles("anime")))
            if _titles("manga"):
                fav_lines.append("📚 " + ", ".join(_titles("manga")))
            if fav_chars:
                fav_lines.append("👤 " + ", ".join(fav_chars))
            if fav_lines:
                embed.add_field(
                    name="⭐ Favourites", value="\n".join(fav_lines), inline=False
                )

            embed.set_footer(text="AniList")
            await ctx.send(embed=embed)

    @anilist.command(name="list")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_list(
        self, ctx, media_type: str = "anime", status: str = "CURRENT"
    ):
        """Show your anime/manga list, filtered by status (defaults to CURRENT)."""

        media_type = media_type.lower()
        if media_type not in ("anime", "manga"):
            return await ctx.send("Media type must be `anime` or `manga`.")

        status = _parse_status(status)
        if status is None:
            return await ctx.send(
                "Status must be one of: Watching/Reading, Completed, "
                "Planning, Paused, Dropped, Repeating."
            )

        token = await self._get_token(ctx.author.id)
        if not token:
            return await ctx.send("Link your account first with `/anilist login`.")

        gql_type = media_type.upper()
        unit = "chapters" if gql_type == "MANGA" else "episodes"

        async with ctx.typing():
            viewer = await self._graphql(VIEWER_QUERY, {}, token=token)
            user = ((viewer or {}).get("data") or {}).get("Viewer")
            if not user:
                return await ctx.send("Could not reach your AniList account.")

            data = await self._graphql(
                MEDIA_LIST_QUERY,
                {"userId": user["id"], "type": gql_type, "status": status},
                token=token,
            )
            collection = (
                ((data or {}).get("data") or {}).get("MediaListCollection") or {}
            )

            lines = []
            for lst in collection.get("lists") or []:
                for entry in lst.get("entries") or []:
                    media = entry.get("media") or {}
                    name = (media.get("title") or {}).get("romaji") or "Unknown"
                    total = (
                        media.get("chapters")
                        if gql_type == "MANGA"
                        else media.get("episodes")
                    ) or "?"
                    lines.append(
                        f"{name} - {entry.get('progress', 0)}/{total} {unit}"
                    )

            if not lines:
                return await ctx.send(
                    f"Nothing on your {status.title()} {media_type} list."
                )

        await Paginator(
            paginate_lines(lines, title=f"{status.title()} {media_type} list"),
            author_id=ctx.author.id,
        ).start(ctx)

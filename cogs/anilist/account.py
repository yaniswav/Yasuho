import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from .components import LoginView
from .helpers import REDIRECT_URI, _parse_status, _profile_colour
from .queries import (
    AUTOCOMPLETE_QUERY,
    LIST_ENTRIES_QUERY,
    USER_STATS_QUERY,
    VIEWER_QUERY,
)
from tools import crypto
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)


# --- List-first autocomplete cache ------------------------------------------
#
# The update/status/score "title" autocomplete prefers titles the user already
# tracks, so on a linked user's first keystroke it fetches their active
# (CURRENT) list once and caches it here, keyed by Discord user id. A stale
# cache is perfectly fine (this only feeds autocomplete), so entries live for a
# short TTL and the map is swept past a hard size cap - mirroring the bounded
# ``tools.cooldowns`` map. Times use ``time.monotonic()`` so a wall-clock change
# can never skew the window.
_LIST_CACHE_TTL = 60.0
_LIST_CACHE_SWEEP_AT = 500
# user_id -> (monotonic_ts, [(media_dict, search_lower), ...]).
_list_cache: dict = {}


def _list_cache_get(user_id, now):
    """Return the cached ``[(media, search)]`` for a user, or None if stale/absent."""

    hit = _list_cache.get(user_id)
    if hit is None:
        return None
    ts, entries = hit
    if now - ts >= _LIST_CACHE_TTL:
        return None
    return entries


def _list_cache_put(user_id, entries, now):
    """Cache a user's list entries, sweeping stale rows once past the size cap."""

    _list_cache[user_id] = (now, entries)
    if len(_list_cache) > _LIST_CACHE_SWEEP_AT:
        cutoff = now - _LIST_CACHE_TTL
        for key in [k for k, (ts, _e) in _list_cache.items() if ts < cutoff]:
            del _list_cache[key]


def _autocomplete_label(media):
    """The ``[TYPE] Romaji (Year)`` choice label shared by list and global hits."""

    mtype = media.get("type") or "?"
    romaji = (media.get("title") or {}).get("romaji") or "Unknown"
    year = media.get("seasonYear") or "?"
    return f"[{mtype}] {romaji} ({year})"


class AniListProfileView(discord.ui.LayoutView):
    """AniList profile rendered as a Components V2 layout.

    A coloured container pairs the user's avatar (as a Section thumbnail) with
    their name and anime/manga stats, then top genres, favourites and the banner
    image when AniList provides one. The view carries no interactive components,
    so it needs no author gating; it mirrors the proven MusicController layout.
    """

    # Keep the layout inside Discord's component budget: cap the favourite lists
    # so a heavy profile can never overflow a single TextDisplay/Container.
    _FAV_LIMIT = 8

    def __init__(self, user, *, timeout=None):
        super().__init__(timeout=timeout)
        self.user = user
        self._build()

    def _build(self):
        user = self.user
        stats = user.get("statistics") or {}
        anime = stats.get("anime") or {}
        manga = stats.get("manga") or {}
        display_name = user.get("name") or "?"
        site_url = user.get("siteUrl")
        avatar = (user.get("avatar") or {}).get("large")

        container = discord.ui.Container(
            accent_colour=_profile_colour(
                (user.get("options") or {}).get("profileColor")
            )
            or random_colour()
        )

        header = discord.ui.TextDisplay(
            "## [{name}]({url})".format(name=display_name, url=site_url)
            if site_url
            else "## {name}".format(name=display_name)
        )
        if avatar:
            container.add_item(
                discord.ui.Section(header, accessory=discord.ui.Thumbnail(avatar))
            )
        else:
            container.add_item(header)

        container.add_item(discord.ui.Separator())

        days = (anime.get("minutesWatched") or 0) / 1440
        container.add_item(
            discord.ui.TextDisplay(
                _(
                    "### 📺 Anime\n"
                    "**{count}** titles - **{days:.1f}** days watched - "
                    "★ **{score}**/100"
                ).format(
                    count=anime.get("count") or 0,
                    days=days,
                    score=anime.get("meanScore") or 0,
                )
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                _(
                    "### 📚 Manga\n"
                    "**{count}** titles - **{chapters}** chapters - "
                    "★ **{score}**/100"
                ).format(
                    count=manga.get("count") or 0,
                    chapters=manga.get("chaptersRead") or 0,
                    score=manga.get("meanScore") or 0,
                )
            )
        )

        genres = anime.get("genres") or []
        top = ", ".join(g.get("genre") for g in genres[:6] if g.get("genre"))
        if top:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    _("### 🎭 Top genres\n{genres}").format(genres=top)
                )
            )

        fav = user.get("favourites") or {}

        def _titles(section):
            nodes = (fav.get(section) or {}).get("nodes") or []
            titles = [
                (n.get("title") or {}).get("romaji")
                for n in nodes
                if (n.get("title") or {}).get("romaji")
            ]
            return titles[: self._FAV_LIMIT]

        fav_chars = [
            (n.get("name") or {}).get("full")
            for n in ((fav.get("characters") or {}).get("nodes") or [])
            if (n.get("name") or {}).get("full")
        ][: self._FAV_LIMIT]

        fav_lines = []
        if _titles("anime"):
            fav_lines.append("📺 " + ", ".join(_titles("anime")))
        if _titles("manga"):
            fav_lines.append("📚 " + ", ".join(_titles("manga")))
        if fav_chars:
            fav_lines.append("👤 " + ", ".join(fav_chars))
        if fav_lines:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    _("### ⭐ Favourites\n{lines}").format(
                        lines="\n".join(fav_lines)
                    )
                )
            )

        banner = user.get("bannerImage")
        if banner:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.MediaGallery(discord.MediaGalleryItem(banner))
            )

        container.add_item(discord.ui.TextDisplay(_("-# AniList")))

        self.add_item(container)


class AccountMixin:
    """AniList account group: OAuth PIN flow plus list editing."""

    # ------------------------------------------------------------------
    # Shared account helpers (reused by the commands and the /anilist hub)
    # ------------------------------------------------------------------
    def _login_available(self):
        """True when OAuth linking is fully configured (client + crypto key)."""

        return bool(self.client_id and self.client_secret and crypto.is_configured())

    def _login_instructions(self):
        """The authorize-and-paste-a-code instructions shown by the login flow."""

        authorize_url = (
            "https://anilist.co/api/v2/oauth/authorize?client_id="
            + self.client_id
            + "&redirect_uri="
            + REDIRECT_URI
            + "&response_type=code"
        )
        return _(
            "Authorize the bot here:\n"
            "{authorize_url}\n\n"
            "Authorize, copy the code AniList shows you, then press "
            "**Enter code** below (or run `/anilist code <code>`)."
        ).format(authorize_url=authorize_url)

    async def _profile_view(self, name):
        """Build the AniList profile payload for a resolved username.

        Returns ``(error, kwargs)``: exactly one is set. ``error`` is a localised
        string when the user cannot be found; ``kwargs`` is the LayoutView send
        payload otherwise. Shared by the ``profile`` command and the hub's My
        stats button.
        """

        data = await self._graphql(USER_STATS_QUERY, {"name": name})
        user = ((data or {}).get("data") or {}).get("User")
        if not user:
            return _("No AniList user found."), None
        return None, {
            "view": AniListProfileView(user),
            "allowed_mentions": discord.AllowedMentions.none(),
        }

    async def _profile_payload(self, user_id):
        """Build the invoker's OWN profile payload (the hub's My stats button).

        Returns ``(error, kwargs)`` like :meth:`_profile_view`; ``error`` also
        covers a missing/expired link and an unresolvable viewer.
        """

        token = await self._get_token(user_id)
        if not token:
            return _("Link your account first with `/anilist login`."), None
        viewer = await self._graphql(VIEWER_QUERY, {}, token=token)
        name = (
            ((viewer or {}).get("data") or {}).get("Viewer") or {}
        ).get("name")
        if not name:
            return _("Could not resolve your AniList account."), None
        return await self._profile_view(name)

    # ------------------------------------------------------------------
    # Account group (OAuth PIN flow + list editing)
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="anilist")
    async def anilist(self, ctx):
        """Link your AniList account and edit your lists."""

        if ctx.invoked_subcommand is None:
            await self._open_hub(ctx)

    @anilist.command(name="login")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_login(self, ctx):
        """Start linking your AniList account."""

        if not self._login_available():
            return await ctx.send(_("AniList account linking is not configured."))

        instructions = self._login_instructions()

        view = LoginView(self, ctx.author.id)

        try:
            view.message = await ctx.author.send(instructions, view=view)
        except discord.Forbidden:
            view.message = await ctx.send(instructions, view=view, ephemeral=True)
            return

        await ctx.send(_("Check your DMs."))

    @anilist.command(name="code")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def anilist_code(self, ctx, *, code: str):
        """Finish linking with the PIN code AniList gave you."""

        if not self._login_available():
            return await ctx.send(_("AniList account linking is not configured."))

        # Hide the PIN if it was posted in a guild text channel.
        if ctx.message is not None and ctx.guild is not None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        name = await self._exchange_code(ctx.author.id, code)
        if name is None:
            return await ctx.send(
                _("That code did not work, try `/anilist login` again."),
                ephemeral=ctx.interaction is not None,
            )

        await ctx.send(
            _("Connected as {name}!").format(name=name),
            ephemeral=ctx.interaction is not None,
        )

    @anilist.command(name="logout")
    async def anilist_logout(self, ctx):
        """Unlink your AniList account."""

        await self.bot.db_pool.execute(
            "DELETE FROM anilist_tokens WHERE user_id = $1;", ctx.author.id
        )
        await ctx.send(
            _("Your AniList account has been unlinked."),
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
                _(
                    "Status must be one of: Watching/Reading, Completed, "
                    "Planning, Paused, Dropped, Repeating."
                )
            )

        await self._edit_flow(ctx, title, "status", status)

    @anilist.command(name="score")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_score(self, ctx, score: float, *, title: str):
        """Score a title on your AniList list."""

        if score < 0:
            return await ctx.send(_("Score must be zero or a positive number."))

        await self._edit_flow(ctx, title, "score", score)

    async def _global_media(self, current):
        """Run the global cross-type search and return its raw media dicts.

        ``self._graphql`` already swallows network failures (returning None), so
        this degrades to an empty list rather than raising - the shared
        autocomplete callback treats that as "no global top-up".
        """

        data = await self._graphql(AUTOCOMPLETE_QUERY, {"search": current})
        return (
            ((data or {}).get("data") or {}).get("Page") or {}
        ).get("media") or []

    async def _current_list_entries(self, user_id, token):
        """Return (and cache) the linked user's active list as ``[(media, search)]``.

        One authed round-trip on a cache miss: resolve the viewer id, then fetch
        their CURRENT anime+manga entries in a single call. ``search`` is the
        lowercased romaji+english title for case-insensitive containment. A
        transient failure returns ``[]`` WITHOUT caching, so the callback simply
        falls back to global search and retries next keystroke; a genuinely empty
        list IS cached so we do not re-fetch it every keystroke. Never raises.
        """

        now = time.monotonic()
        cached = _list_cache_get(user_id, now)
        if cached is not None:
            return cached

        try:
            viewer = await self._graphql(VIEWER_QUERY, {}, token=token)
            vid = (
                ((viewer or {}).get("data") or {}).get("Viewer") or {}
            ).get("id")
            if not vid:
                return []

            data = await self._graphql(
                LIST_ENTRIES_QUERY, {"userId": vid, "status": "CURRENT"}, token=token
            )
            if not data or not data.get("data"):
                return []
            collection = (data.get("data") or {}).get("MediaListCollection") or {}

            entries = []
            seen = set()
            for lst in collection.get("lists") or []:
                for entry in lst.get("entries") or []:
                    media = entry.get("media") or {}
                    mid = media.get("id")
                    if mid is None or mid in seen:
                        continue
                    seen.add(mid)
                    title = media.get("title") or {}
                    search = " ".join(
                        p
                        for p in (title.get("romaji"), title.get("english"))
                        if p
                    ).lower()
                    entries.append((media, search))
            _list_cache_put(user_id, entries, now)
            return entries
        except Exception:
            log.exception("AniList list autocomplete fetch failed")
            return []

    async def _list_first_autocomplete(self, user_id, current, token):
        """List-first choices: the user's tracked titles, topped up with global.

        Their CURRENT entries come first (filtered by the typed text, or the head
        of the list when empty), keeping the ``id:<n>`` sentinel values so command
        resolution is untouched. If those alone fill 25 choices the global search
        is skipped entirely (one fewer API call). Otherwise the remainder is
        topped up with global results, deduped by media id.
        """

        entries = await self._current_list_entries(user_id, token)
        lowered = current.lower()
        matches = (
            [m for (m, s) in entries if lowered in s]
            if lowered
            else [m for (m, _s) in entries]
        )

        choices = []
        seen = set()
        for media in matches[:25]:
            choices.append(
                app_commands.Choice(
                    name=_autocomplete_label(media)[:100],
                    value=f"id:{media.get('id')}",
                )
            )
            seen.add(media.get("id"))

        # List matches already fill the menu, or the query is too short for a
        # meaningful global search: no global top-up.
        if len(choices) >= 25 or len(current) < 2:
            return choices

        for media in await self._global_media(current):
            if len(choices) >= 25:
                break
            mid = media.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            choices.append(
                app_commands.Choice(
                    name=_autocomplete_label(media)[:100], value=f"id:{mid}"
                )
            )
        return choices

    @anilist_update.autocomplete("title")
    @anilist_status.autocomplete("title")
    @anilist_score.autocomplete("title")
    async def _title_autocomplete(self, interaction, current):
        """List-first search powering the update/status/score 'title' option.

        Linked users see the titles they already track first (an ~95% hit rate on
        real updates), topped up with global search; unlinked users get the exact
        global-only behaviour as before. Every value is an ``id:<mediaId>``
        sentinel so a numeric title (e.g. the anime "86") can never be mistaken
        for an id in the edit flow. Any AniList failure degrades to global-only,
        then to no suggestions - the command still works without them.
        """

        try:
            current = (current or "").strip()
            token = await self._get_token(interaction.user.id)
            if token:
                return await self._list_first_autocomplete(
                    interaction.user.id, current, token
                )

            # Unlinked: byte-identical to the historical global-only behaviour.
            if len(current) < 2:
                return []
            return [
                app_commands.Choice(
                    name=_autocomplete_label(media)[:100],
                    value=f"id:{media.get('id')}",
                )
                for media in (await self._global_media(current))[:25]
            ]
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
                        _(
                            "Provide a name or link your account with "
                            "`/anilist login`."
                        )
                    )
                viewer = await self._graphql(VIEWER_QUERY, {}, token=token)
                name = (
                    ((viewer or {}).get("data") or {}).get("Viewer") or {}
                ).get("name")
                if not name:
                    return await ctx.send(
                        _("Could not resolve your AniList account.")
                    )

            error, kwargs = await self._profile_view(name)
            if error:
                return await ctx.send(error)

            # A LayoutView carries its own content; send it with no embed and
            # suppress mentions since TextDisplay resolves them (unlike an embed).
            await ctx.send(**kwargs)

    @anilist.command(name="list")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_list(
        self, ctx, media_type: str = "anime", status: str = "CURRENT"
    ):
        """Open your interactive list dashboard, filtered by status (CURRENT)."""

        media_type = media_type.lower()
        if media_type not in ("anime", "manga"):
            return await ctx.send(_("Media type must be `anime` or `manga`."))

        status = _parse_status(status)
        if status is None:
            return await ctx.send(
                _(
                    "Status must be one of: Watching/Reading, Completed, "
                    "Planning, Paused, Dropped, Repeating."
                )
            )

        async with ctx.typing():
            error, view = await self._collection_payload(
                ctx.author.id, media_type, status
            )
        if error:
            return await ctx.send(error)

        view.message = await ctx.send(view=view)

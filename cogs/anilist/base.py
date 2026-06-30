import datetime
import logging

import aiohttp
import discord

from .components import (
    EditSelectView,
    MediaView,
    OnListSelectView,
    ResultView,
    SeasonSelectView,
    TypeView,
)
from .helpers import (
    API_URL,
    REDIRECT_URI,
    TOKEN_URL,
    _media_title,
    _media_unit,
    _progress_max,
)
from .queries import (
    CANDIDATE_QUERY,
    ID_MEDIA_QUERY,
    MEDIA_ENTRY_QUERY,
    MEDIA_QUERY,
    PAGE_QUERY,
    SAVE_ENTRY_QUERY,
    SEARCH_ENTRY_QUERY,
    SEARCH_QUERY,
    VIEWER_QUERY,
)
from tools import crypto
from tools.config_loader import config_loader
from tools.i18n import _

log = logging.getLogger(__name__)

# Cap AniList HTTP calls so a slow or hung endpoint can't block an interaction.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


class AniListBase:
    """AniList lookups plus per-user account linking to edit your lists."""

    def __init__(self, bot):
        self.bot = bot

        try:
            self.client_id = config_loader.get("AniList", "clientId")
        except Exception:
            self.client_id = ""

        try:
            self.client_secret = config_loader.get("AniList", "clientSecret")
        except Exception:
            self.client_secret = ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _graphql(self, query, variables, token=None):
        """POST a GraphQL request to AniList. Returns the parsed JSON or None."""

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = "Bearer " + token

        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.post(
                    API_URL,
                    json={"query": query, "variables": variables},
                    headers=headers,
                ) as r:
                    return await r.json()
        except Exception:
            log.exception("AniList GraphQL request failed")
            return None

    async def _store_token(self, user_id, access_token, expires_in):
        """Persist the encrypted access token (never the plaintext)."""

        encrypted = crypto.encrypt(access_token)
        expires = None
        if expires_in:
            expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                seconds=expires_in
            )

        query = """
            INSERT INTO anilist_tokens (user_id, token, expires)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET token = $2, expires = $3;
            """
        await self.bot.db_pool.execute(query, user_id, encrypted, expires)

    async def _get_token(self, user_id):
        """Return the decrypted access token, or None if missing/expired."""

        query = "SELECT token, expires FROM anilist_tokens WHERE user_id = $1;"
        row = await self.bot.db_pool.fetchrow(query, user_id)
        if row is None:
            return None

        if row["expires"] and row["expires"] < datetime.datetime.now(
            datetime.timezone.utc
        ):
            return None

        return crypto.decrypt(row["token"])

    async def _viewer_entry(self, user_id, media_id):
        """Return ``(entry, logged_in)`` for the user's list entry on a media.

        ``entry`` is the authenticated viewer's ``mediaListEntry`` (or ``None``
        when the media is not on their list), and ``logged_in`` is ``True`` only
        when a valid token was found. The query is sent with the user's OAuth
        token so AniList resolves the entry per-viewer; the token is never
        logged.
        """

        if media_id is None:
            return None, False

        token = await self._get_token(user_id)
        if not token:
            return None, False

        data = await self._graphql(
            MEDIA_ENTRY_QUERY, {"id": media_id}, token=token
        )
        entry = (
            ((data or {}).get("data") or {}).get("Media") or {}
        ).get("mediaListEntry")
        return entry, True

    async def _exchange_code(self, user_id, code):
        """Exchange an OAuth PIN for a token and store it.

        Returns the AniList viewer name on success, or ``None`` on failure.
        The token and code are never logged or echoed.
        """

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": REDIRECT_URI,
            "code": (code or "").strip(),
        }

        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.post(TOKEN_URL, json=payload) as r:
                    data = await r.json()
        except Exception:
            log.exception("AniList token exchange failed")
            return None

        access_token = (data or {}).get("access_token")
        if not access_token:
            return None

        await self._store_token(user_id, access_token, data.get("expires_in"))

        viewer = await self._graphql(VIEWER_QUERY, {}, token=access_token)
        name = (((viewer or {}).get("data") or {}).get("Viewer") or {}).get("name")
        return name or _("AniList user")

    async def _search_candidates(self, title):
        """Return up to ~10 search candidates across both anime and manga.

        The lack of a type filter is deliberate: it lets the edit flow tell
        the user that, e.g., "Berserk" exists as both an anime and a manga.
        """

        data = await self._graphql(SEARCH_QUERY, {"search": title})
        page = ((data or {}).get("data") or {}).get("Page") or {}
        return page.get("media") or []

    async def _search_candidates_for_user(self, title, token):
        """Like :meth:`_search_candidates`, but each media is tagged with the
        viewer's own ``mediaListEntry`` (resolved via their token). A candidate
        is "on their list" when that entry is not ``None``. The token is never
        logged. Used only by the update wizard; status/score keep the anonymous
        :meth:`_search_candidates`.
        """

        data = await self._graphql(
            SEARCH_ENTRY_QUERY, {"search": title}, token=token
        )
        page = ((data or {}).get("data") or {}).get("Page") or {}
        return page.get("media") or []

    async def _reply(self, sender, content):
        """Send ``content`` via either a Context or an Interaction."""

        try:
            if isinstance(sender, discord.Interaction):
                if sender.response.is_done():
                    await sender.followup.send(content, ephemeral=True)
                else:
                    await sender.response.send_message(content, ephemeral=True)
            else:
                await sender.send(content)
        except Exception:
            log.exception("AniList reply failed")

    async def _apply_edit(self, sender, user_id, media, field, value):
        """Apply a single ``field`` edit to ``user_id``'s list entry for ``media``.

        ``field`` is one of ``progress``/``status``/``score``/``complete``.
        ``complete`` sets the status to COMPLETED and, when the total is known,
        the progress to it in a single mutation. ``sender`` may be a Context or
        an Interaction; the type-aware confirmation is routed accordingly
        (episode vs chapter).
        """

        token = await self._get_token(user_id)
        if not token:
            return await self._reply(
                sender, _("Link your account first with `/anilist login`.")
            )

        variables = {"mediaId": media.get("id")}
        if field == "progress":
            variables["progress"] = value
            variables["status"] = "CURRENT"
        elif field == "status":
            variables["status"] = value
        elif field == "score":
            variables["score"] = value
        elif field == "complete":
            variables["status"] = "COMPLETED"
            total = _progress_max(media)
            if total:
                variables["progress"] = total

        data = await self._graphql(SAVE_ENTRY_QUERY, variables, token=token)
        entry = ((data or {}).get("data") or {}).get("SaveMediaListEntry")
        if not entry:
            return await self._reply(sender, _("Could not update that entry."))

        name = ((entry.get("media") or {}).get("title") or {}).get(
            "romaji"
        ) or _media_title(media)

        if field == "progress":
            unit = _media_unit(media)
            message = _("Set **{name}** to {unit} {progress} ({status}).").format(
                name=name,
                unit=unit,
                progress=entry.get("progress"),
                status=entry.get("status"),
            )
        elif field == "status":
            message = _("Set **{name}** to {status}.").format(
                name=name, status=entry.get("status")
            )
        elif field == "complete":
            progress = entry.get("progress")
            if progress:
                unit = _media_unit(media, plural=True)
                message = _("Completed **{name}** ({progress} {unit}).").format(
                    name=name, progress=progress, unit=unit
                )
            else:
                message = _("Marked **{name}** as completed.").format(name=name)
        else:
            message = _("Scored **{name}** {score}.").format(
                name=name, score=entry.get("score")
            )

        await self._reply(sender, message)

    async def _media_by_id(self, media_id):
        """Fetch a lightweight media object by id (for the autocomplete path)."""

        data = await self._graphql(ID_MEDIA_QUERY, {"id": media_id})
        return ((data or {}).get("data") or {}).get("Media")

    async def _edit_flow(self, ctx, title, field, value):
        """Resolve ``title`` (disambiguating anime/manga) then apply an edit."""

        token = await self._get_token(ctx.author.id)
        if not token:
            return await ctx.send(
                _("Link your account first with `/anilist login`.")
            )

        # Slash autocomplete supplies an "id:<n>" sentinel (collision-safe vs a
        # numeric title like "86"): resolve it directly, skipping the search.
        if title.startswith("id:") and title[3:].isdigit():
            async with ctx.typing():
                media = await self._media_by_id(int(title[3:]))
            if not media:
                return await ctx.send(_("Could not load that title."))
            return await self._apply_edit(ctx, ctx.author.id, media, field, value)

        async with ctx.typing():
            candidates = await self._search_candidates(title)

        if not candidates:
            return await ctx.send(
                _("No result for **{title}**.").format(title=title)
            )

        if len(candidates) == 1:
            return await self._apply_edit(
                ctx, ctx.author.id, candidates[0], field, value
            )

        view = EditSelectView(self, candidates, ctx.author.id, field, value)
        view.message = await ctx.send(
            content=_(
                "Multiple matches for **{title}** - pick the right one:"
            ).format(title=title),
            view=view,
        )

    async def _open_media_editor(self, ctx, media_id, token, fallback=None):
        """Open the full MediaView editor for a resolved media (ctx-driven path).

        Used by the wizard's single-candidate / ``id:`` entry points, where a
        ``Context`` (not a component interaction) cannot send a modal. The
        MediaView carries the StatusSelect / +1 / Complete / Edit tools, and its
        Edit button opens the same pre-filled modal from a component interaction.
        """

        data = await self._graphql(MEDIA_QUERY, {"id": media_id})
        media = ((data or {}).get("data") or {}).get("Media") or fallback
        if not media:
            return await ctx.send(_("Could not load that title."))

        view = MediaView(self, media, ctx.author.id, token=token)
        view.message = await ctx.send(embed=view.overview_embed(), view=view)

    async def _update_wizard(self, ctx, title):
        """Guided ``update``, prioritising titles the user already tracks.

        update saves to the user's list, so a linked account is required.
        Routing:
          * exactly one matching entry on their list -> edit it directly;
          * several matching entries -> ask which one, scoped to their list;
          * nothing tracked -> the full add-a-new-entry wizard.
        """

        token = await self._get_token(ctx.author.id)
        if not token:
            return await ctx.send(
                _("Link your account first with `/anilist login`.")
            )

        # Autocomplete supplies an "id:<n>" sentinel: resolve straight to editor.
        if title.startswith("id:") and title[3:].isdigit():
            async with ctx.typing():
                return await self._open_media_editor(ctx, int(title[3:]), token)

        # Token-aware search so each candidate carries the viewer's own entry.
        async with ctx.typing():
            candidates = await self._search_candidates_for_user(title, token)

        if not candidates:
            return await ctx.send(
                _("No result for **{title}**.").format(title=title)
            )

        on_list = [c for c in candidates if c.get("mediaListEntry")]

        # Exactly one tracked entry -> edit it directly, no questions asked.
        if len(on_list) == 1:
            async with ctx.typing():
                return await self._open_media_editor(
                    ctx, on_list[0].get("id"), token, fallback=on_list[0]
                )

        # Several tracked entries -> pick among ONLY what they actually track.
        if len(on_list) > 1:
            view = OnListSelectView(self, on_list, ctx.author.id)
            view.message = await ctx.send(
                content=_(
                    "You track several titles matching **{title}** - which one?"
                ).format(title=title),
                view=view,
            )
            return

        # Nothing tracked -> full wizard over the global results to add an entry.
        if len(candidates) == 1:
            async with ctx.typing():
                return await self._open_media_editor(
                    ctx, candidates[0].get("id"), token, fallback=candidates[0]
                )

        types_present = []
        for media in candidates:
            mtype = media.get("type")
            if mtype and mtype not in types_present:
                types_present.append(mtype)

        if len(types_present) >= 2:
            view = TypeView(self, candidates, ctx.author.id)
            view.message = await ctx.send(
                content=_("**{title}** - is it an anime or a manga?").format(
                    title=title
                ),
                view=view,
            )
            return

        only_type = types_present[0] if types_present else None
        view = SeasonSelectView(self, candidates, ctx.author.id, only_type)
        view.message = await ctx.send(
            content=_("Pick the exact title to update:"), view=view
        )

    # ------------------------------------------------------------------
    # Lookup commands (no auth required)
    # ------------------------------------------------------------------
    async def _media_lookup(self, ctx, search, media_type):
        """Search AniList and present results via the interactive flow."""

        async with ctx.typing():
            data = await self._graphql(
                CANDIDATE_QUERY, {"search": search, "type": media_type}
            )
            candidates = (
                ((data or {}).get("data") or {}).get("Page") or {}
            ).get("media") or []
            if not candidates:
                return await ctx.send(_("No result."))

            token = await self._get_token(ctx.author.id)

            # A single match jumps straight to the full media view.
            if len(candidates) == 1:
                full = await self._graphql(
                    MEDIA_QUERY, {"id": candidates[0]["id"]}
                )
                media = ((full or {}).get("data") or {}).get("Media")
                if not media:
                    return await ctx.send(_("No result."))
                view = MediaView(self, media, ctx.author.id, token=token)
                view.message = await ctx.send(
                    embed=view.overview_embed(), view=view
                )
                return

            view = ResultView(self, candidates, ctx.author.id, media_type)
            view.message = await ctx.send(
                content=_(
                    "Found {count} results for **{search}** - pick one:"
                ).format(count=len(candidates), search=search),
                view=view,
            )

    async def _browse(self, ctx, variables, media_type, label):
        """Run a PAGE_QUERY browse and offer the results as a picker."""

        async with ctx.typing():
            data = await self._graphql(PAGE_QUERY, variables)
            media = (
                ((data or {}).get("data") or {}).get("Page") or {}
            ).get("media") or []
            if not media:
                return await ctx.send(_("No result."))

            view = ResultView(self, media, ctx.author.id, media_type)
            view.message = await ctx.send(
                content=_("**{label}** - pick one for details:").format(
                    label=label
                ),
                view=view,
            )

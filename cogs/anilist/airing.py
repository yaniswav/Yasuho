"""AniList airing tracker: opt-in, per-user new-episode DMs.

A user opts in with ``/anilist airing``; from then on the poller DMs them a
compact Components V2 card whenever a new episode of a title on their CURRENT
(Watching) AniList anime list airs, carrying a one-click **Seen** button that
bumps their AniList progress to that episode.

Two moving parts live here, both wired from the package ``__init__``:

* :class:`AiringMixin` - a base of the composed ``AniList`` cog that owns the
  ``/anilist airing`` opt-in toggle (it needs to be a subcommand of the shared
  ``anilist`` group, which discord.py only allows from the same cog).
* :class:`AniListAiring` - a standalone cog (added like ``AniListFeed``) that
  owns the poller, the per-user CURRENT-list cache, the schedule fetch, DM fan
  out and the persistent Seen button.

Token discipline. Poll-time reads are UNAUTHENTICATED: a public profile's
``MediaListCollection`` and ``Page.airingSchedules`` need no token. The only
token use is at opt-in (resolving the user's AniList id via ``VIEWER_QUERY``)
and on a Seen click (writing the clicker's own progress). Nothing here logs or
stores a token.

Cursor + anti-backfill. AniList only guarantees FUTURE airing data, so the
poller scans a SHORT trailing window ``(last_airing_at, now]`` sorted by TIME
ascending and never pre-announces. The cursor advances to the max ``airingAt``
processed; under page truncation it stops at the last fetched row so the
higher-``airingAt`` tail rides the next tick (see
:func:`tools.anilist_feed.advance_airing_cursor`). The very first run anchors the
cursor to ``now`` and posts nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
import discord
from discord.ext import commands, tasks

from .account import AccountMixin
from .feed import (
    CARD_ACCENT,
    REQUEST_SPACING,
    _authed_graphql,
    _AuthError,
    _check_debounce,
    _colour_from_media,
    _feed_ephemeral,
    _FetchError,
    _GoneError,
    _media_title,
    _parse_retry_after,
    _RateLimited,
    _resolve_token,
)
from .helpers import API_URL
from .queries import SAVE_ENTRY_QUERY, VIEWER_QUERY
from tools import anilist_feed as af
from tools import i18n
from tools.http import TIMEOUT
from tools.i18n import _

log = logging.getLogger(__name__)


# Poller cadence. Airing is not latency-critical - a new-episode DM a few minutes
# late is fine - so 600s (10 min) keeps the poll cheap and stays far under the
# (currently degraded 30/min) rate limit even with staggered list refreshes plus
# a few schedule pages in one tick.
POLL_SECONDS = 600

# AniList silently clamps perPage to 50 on Page.airingSchedules, so 50 is the
# real page size; paginate while a page comes back full.
PER_PAGE = 50

# Hard safety cap on airingSchedules pages fetched per tick. A tracked-media
# union only yields a handful of airings in a short trailing window, so this is a
# bound against a pathological burst, not a normal path; hitting it holds the
# cursor at the last fetched row (the tail rides the next tick).
MAX_SCHEDULE_PAGES = 5

# Per-user CURRENT-list cache TTL. A Watching list changes rarely relative to a
# 10-min tick, so a cached list is reused for ~30 min before a lazy refresh.
LIST_TTL = 1800.0
LIST_SWEEP_AT = 500

# At most this many stale per-user list refreshes per tick, so a large opt-in
# base cannot burst the rate limit: the rest refresh on later ticks (staggered).
MAX_LIST_REFRESHES_PER_TICK = 5


# --- GraphQL ----------------------------------------------------------------

# Airing schedules for a set of media in a trailing window. ``mediaId_in`` takes
# up to 10k ids; ``airingAt_greater`` / ``airingAt_lesser`` bound the window
# (unix seconds, strict greater); ``sort: TIME`` (the AiringSort enum) returns
# oldest-first so pagination and the cursor advance in airingAt order. Only the
# media fields the DM card needs are selected.
AIRING_SCHEDULE_QUERY = """
query ($mediaIds: [Int], $greater: Int, $lesser: Int, $page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    airingSchedules(
      mediaId_in: $mediaIds
      airingAt_greater: $greater
      airingAt_lesser: $lesser
      sort: TIME
    ) {
      id
      airingAt
      episode
      mediaId
      media {
        id
        title { romaji english userPreferred }
        coverImage { large medium color }
        isAdult
        siteUrl
        episodes
        format
      }
    }
  }
}
"""

# A user's public CURRENT anime list (media id + the viewer's progress). Readable
# UNAUTHENTICATED for public profiles, so the poller never needs a user token.
AIRING_LIST_QUERY = """
query ($userId: Int) {
  MediaListCollection(userId: $userId, type: ANIME, status: CURRENT) {
    lists {
      entries {
        mediaId
        progress
      }
    }
  }
}
"""

# The clicking viewer's own progress + the title, for the Seen button. Authed:
# ``mediaListEntry`` resolves per-viewer only when the request carries the user's
# token (the same per-viewer resolution the feed's Add button relies on).
SEEN_LOOKUP_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    id
    title { userPreferred romaji english }
    episodes
    mediaListEntry { status progress }
  }
}
"""

# Seen button custom_id template. The ``alf:seen:`` prefix is disjoint from the
# feed's ``alf:like`` / ``alf:reply`` / ``alf:add`` templates so discord.py's
# fullmatch dispatch can never cross-route; ``mid`` is the media id and ``ep`` the
# episode that just aired (both positive ints, well under the 100-char limit).
SEEN_TEMPLATE = r"alf:seen:(?P<mid>\d+):(?P<ep>\d+)"


def _title_markup(media):
    """A masked ``[title](url)`` link for the card line, or bare title with no url.

    Square brackets are stripped so a title like ``Re:Zero [Director's Cut]``
    cannot break the ``[...]`` markup. The bold lives in the card's msgid
    (``**{title}**``), so this returns the link only.
    """

    title = _media_title(media)
    safe = str(title).replace("[", "").replace("]", "")
    url = (media or {}).get("siteUrl")
    if url:
        return "[{title}]({url})".format(title=safe, url=url)
    return safe


# --- Pure helpers (unit-tested; no network, DB or Discord) -------------------


def plan_airing_channel_posts(aired, channel_media):
    """Pick the ``(guild_id, channel_id, media_id, episode)`` channel posts to make.

    ``aired`` is the list of aired schedule rows actually processed this tick, each
    a dict carrying ``media_id`` (int) and ``episode`` (int). ``channel_media`` maps
    a ``(guild_id, channel_id)`` feed key to the set of AniList media ids that feed
    explicitly SUBSCRIBES to (``anilist_channel_subs``, media_type ANIME).

    A feed channel gets ONE post per aired row whose media it subscribes to. Unlike
    the DM path (:func:`tools.anilist_feed.plan_airing_notifications`), this is NOT
    progress-gated: a guild post is for everyone in the channel, not tied to any one
    member's progress, so an airing of a subscribed title is posted regardless of
    who has watched what.

    Returns a flat list of ``(guild_id, channel_id, media_id, episode)`` tuples in a
    stable order - aired-row order, then feed key ascending - so delivery and the
    tests are deterministic. A row missing a media id or episode is skipped. Pure and
    total.
    """

    posts = []
    for row in aired:
        media_id = row.get("media_id")
        episode = row.get("episode")
        if media_id is None or episode is None:
            continue
        for key in sorted(channel_media):
            if media_id in channel_media[key]:
                guild_id, channel_id = key
                posts.append((guild_id, channel_id, media_id, episode))
    return posts


# --- Seen button ------------------------------------------------------------


async def _run_seen(interaction, media_id, episode):
    """Advance the clicker's AniList progress to ``episode`` for ``media_id``.

    Mirrors the feed action buttons: apply the invocation locale, gate on the
    shared per-user debounce, then resolve the clicker's token (this action
    WRITES, so a token is required). It looks up their current entry first and
    only advances when their progress is strictly below ``episode`` - progress is
    never regressed. The decrypted token stays a local; it is never logged or
    stored.
    """

    # Component callbacks run in their own task where the invocation locale was
    # never set: resolve it first so every _() below renders in the user's tongue.
    await i18n.apply_interaction_locale(interaction)
    if not await _check_debounce(interaction):
        return
    token = await _resolve_token(interaction)
    if token is None:
        return

    # Both round-trips can outlast the 3s window; defer, then follow up.
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except discord.HTTPException:
        pass

    # 1) Look up the viewer's current progress + the title, as themselves.
    try:
        data = await _authed_graphql(token, SEEN_LOOKUP_QUERY, {"id": media_id})
    except _RateLimited:
        return await _feed_ephemeral(
            interaction, _("AniList is rate limiting me right now - try again shortly.")
        )
    except _AuthError:
        return await _feed_ephemeral(
            interaction,
            _(
                "Your AniList link seems invalid now - re-link it with "
                "`/anilist login`."
            ),
        )
    except _GoneError:
        return await _feed_ephemeral(
            interaction, _("I couldn't find that title on AniList anymore.")
        )
    except _FetchError:
        return await _feed_ephemeral(
            interaction, _("I could not reach AniList - try again shortly.")
        )

    media = ((data or {}).get("data") or {}).get("Media") or {}
    if not media:
        return await _feed_ephemeral(
            interaction, _("I couldn't find that title on AniList anymore.")
        )
    title = _media_title(media)
    entry = media.get("mediaListEntry") or {}
    progress = entry.get("progress") or 0
    if progress >= episode:
        return await _feed_ephemeral(
            interaction,
            _("You are already at episode {progress} of **{title}**.").format(
                progress=progress, title=title
            ),
        )

    # 2) Advance progress to the aired episode as the clicking user. Passing only
    #    the progress leaves their status untouched (they are already Watching).
    try:
        saved = await _authed_graphql(
            token, SAVE_ENTRY_QUERY, {"mediaId": media_id, "progress": episode}
        )
    except _RateLimited:
        return await _feed_ephemeral(
            interaction, _("AniList is rate limiting me right now - try again shortly.")
        )
    except _AuthError:
        return await _feed_ephemeral(
            interaction,
            _(
                "Your AniList link seems invalid now - re-link it with "
                "`/anilist login`."
            ),
        )
    except _GoneError:
        return await _feed_ephemeral(
            interaction, _("I couldn't find that title on AniList anymore.")
        )
    except _FetchError:
        return await _feed_ephemeral(
            interaction, _("I could not reach AniList - try again shortly.")
        )

    if not ((saved or {}).get("data") or {}).get("SaveMediaListEntry"):
        return await _feed_ephemeral(
            interaction, _("I could not reach AniList - try again shortly.")
        )
    await _feed_ephemeral(
        interaction,
        _("Marked **{title}** as watched up to episode {episode}.").format(
            title=title, episode=episode
        ),
    )


class AiringSeenButton(
    discord.ui.DynamicItem[discord.ui.Button], template=SEEN_TEMPLATE
):
    """Persistent Seen button that advances the clicker's progress to the aired episode.

    A :class:`discord.ui.DynamicItem`, so the card is persistent (``timeout=None``)
    and the button keeps working forever - on DMs sent before a restart included -
    because dispatch matches the custom_id against the globally-registered template
    and rebuilds the item from the live message, never from a stored view. The
    media id and episode are the only state and ride inside the custom_id.
    """

    def __init__(self, media_id, episode):
        self.media_id = media_id
        self.episode = episode
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label=_("Seen"),
                emoji="\N{WHITE HEAVY CHECK MARK}",
                custom_id="alf:seen:{mid}:{ep}".format(mid=media_id, ep=episode),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["mid"]), int(match["ep"]))

    async def callback(self, interaction):
        await _run_seen(interaction, self.media_id, self.episode)


class AiringCard(discord.ui.LayoutView):
    """One just-aired episode as a compact Components V2 DM card.

    A cover-accented :class:`~discord.ui.Container` holds the aired-episode line
    (title link + "Episode N of X just aired." + a relative timestamp). The cover
    art is a :class:`~discord.ui.Thumbnail` accessory beside the text (its
    ``description`` alt text is the media title, for screen readers), OMITTED
    when the media is adult (the text stays, only the image is dropped). A trailing
    :class:`~discord.ui.ActionRow` carries the 'AniList' link button and the
    persistent :class:`AiringSeenButton`. Every field degrades independently, so a
    partial row dict never breaks delivery of the batch.
    """

    def __init__(self, row, *, timeout=None):
        super().__init__(timeout=timeout)
        try:
            self._build(row)
        except Exception:  # a card must never break the DM fan-out
            log.exception("AniList airing: failed to build a card")
            self._fallback()

    def _fallback(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=CARD_ACCENT)
        container.add_item(discord.ui.TextDisplay(_("A new episode just aired.")))
        self.add_item(container)

    def _build(self, row):
        media = row.get("media") or {}
        episode = row.get("episode")
        airing_at = row.get("airing_at")
        media_id = media.get("id") or row.get("media_id")
        url = media.get("siteUrl")

        container = discord.ui.Container(accent_colour=_colour_from_media(media))
        line = _("Episode {episode} of **{title}** just aired.").format(
            episode=episode, title=_title_markup(media)
        )
        texts = [
            discord.ui.TextDisplay("### " + _("New episode")),
            discord.ui.TextDisplay(line),
        ]
        if airing_at:
            texts.append(discord.ui.TextDisplay("-# <t:{ts}:R>".format(ts=int(airing_at))))

        cover = media.get("coverImage") or {}
        thumb = cover.get("large") or cover.get("medium")
        if thumb and not media.get("isAdult"):
            # A Section requires an accessory; only build one when we have a cover
            # to show (never for adult media), else degrade to plain text displays.
            container.add_item(
                discord.ui.Section(
                    *texts,
                    accessory=discord.ui.Thumbnail(
                        thumb, description=str(_media_title(media))[:256]
                    ),
                )
            )
        else:
            for text in texts:
                container.add_item(text)

        action_row = discord.ui.ActionRow()
        if url:
            action_row.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.link, label=_("AniList"), url=url
                )
            )
        if media_id is not None and episode is not None:
            action_row.add_item(AiringSeenButton(media_id, episode))
        if action_row.children:
            container.add_item(discord.ui.Separator())
            container.add_item(action_row)
        self.add_item(container)


# --- Opt-in command (a subcommand of the shared ``anilist`` group) -----------


class AiringMixin:
    """The ``/anilist airing`` opt-in toggle, mixed into the composed AniList cog.

    It has to live on the same cog as the ``anilist`` hybrid group (discord.py
    rejects a subcommand whose parent group is in a different cog), so it is a
    base of ``AniList`` rather than part of the standalone :class:`AniListAiring`
    poller cog. It reuses the base cog's ``_token_status`` / ``_graphql`` and
    talks to the same ``anilist_airing_optins`` table the poller reads.
    """

    @AccountMixin.anilist.command(name="airing")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_airing(self, ctx):
        """Toggle new-episode DMs for titles on your Watching anime list."""

        ephemeral = ctx.interaction is not None

        row = await self.bot.db_pool.fetchrow(
            "SELECT enabled FROM anilist_airing_optins WHERE user_id = $1;",
            ctx.author.id,
        )

        # Already opted in and on -> turn it off. Disabling always works.
        if row is not None and row["enabled"]:
            await self.bot.db_pool.execute(
                "UPDATE anilist_airing_optins SET enabled = FALSE WHERE user_id = $1;",
                ctx.author.id,
            )
            return await ctx.send(
                _(
                    "Airing alerts are now **off**. I will not DM you about new "
                    "episodes anymore - run this again to turn them back on."
                ),
                ephemeral=ephemeral,
            )

        # Enabling needs a linked account, to resolve and store their AniList id.
        status, token = await self._token_status(ctx.author.id)
        if status == "missing":
            return await ctx.send(
                _(
                    "Link your AniList account first with `/anilist login`, then "
                    "run this again to turn on airing alerts."
                ),
                ephemeral=ephemeral,
            )
        if status != "ok" or not token:
            return await ctx.send(
                _(
                    "Your AniList link is no longer valid - re-link it with "
                    "`/anilist login`."
                ),
                ephemeral=ephemeral,
            )

        async with ctx.typing():
            data = await self._graphql(VIEWER_QUERY, {}, token=token)
        viewer = ((data or {}).get("data") or {}).get("Viewer")
        if not viewer or viewer.get("id") is None:
            return await ctx.send(
                _("Could not resolve your AniList account - try again shortly."),
                ephemeral=ephemeral,
            )
        anilist_user_id = viewer["id"]

        await self.bot.db_pool.execute(
            "INSERT INTO anilist_airing_optins (user_id, anilist_user_id, enabled) "
            "VALUES ($1, $2, TRUE) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "anilist_user_id = EXCLUDED.anilist_user_id, enabled = TRUE;",
            ctx.author.id,
            anilist_user_id,
        )

        # Best-effort: peek at their PUBLIC Watching list (the poller reads it
        # unauthenticated) so we can warn if it is empty or private. A transient
        # read failure is treated as "unknown" - we neither warn nor falsely
        # reassure.
        note = ""
        airing_cog = self.bot.get_cog("AniListAiring")
        if airing_cog is not None:
            try:
                current = await airing_cog._fetch_public_list(anilist_user_id)
            except Exception:
                current = None
            if current is not None and not current:
                note = "\n" + _(
                    "-# Heads up: I could not see any titles on your Watching list. "
                    "Make sure your AniList anime list is set to public."
                )

        await ctx.send(
            _(
                "Airing alerts are now **on**. I will DM you when a new episode of "
                "a title on your **Watching** anime list airs, with a one-click "
                "Seen button.\n"
                "-# Your AniList anime list must be public for this to work."
            )
            + note,
            ephemeral=ephemeral,
        )


# --- Poller cog -------------------------------------------------------------


class AniListAiring(commands.Cog):
    """Opt-in airing tracker: DM a user when a tracked title's new episode airs."""

    def __init__(self, bot):
        self.bot = bot
        # Unix timestamp before which the poller stays quiet (429 embargo).
        self._embargo_until = 0
        # Per-tick request pacing flag (reset each tick in _tick).
        self._spaced = False
        # anilist_user_id -> (monotonic_ts, {media_id: progress}). Bounded cache,
        # swept past a hard size cap - the house pattern (see cogs/anilist/account).
        self._list_cache: dict = {}
        self._poll_airing.start()

    async def cog_load(self):
        # Register the Seen DynamicItem process-wide so its clicks dispatch on
        # EVERY airing DM, including ones sent before this start.
        try:
            self.bot.add_dynamic_items(AiringSeenButton)
        except Exception:
            log.exception("AniList airing: failed to register the Seen button")

    def cog_unload(self):
        self._poll_airing.cancel()
        try:
            self.bot.remove_dynamic_items(AiringSeenButton)
        except Exception:
            log.exception("AniList airing: failed to remove the Seen button")

    # ------------------------------------------------------------------
    # GraphQL plumbing (unauthenticated; one session per call)
    # ------------------------------------------------------------------
    async def _graphql(self, query, variables):
        """POST an UNAUTHENTICATED GraphQL request to AniList.

        Returns the parsed JSON. Raises :class:`_RateLimited` on a 429 (with the
        Retry-After seconds) and :class:`_FetchError` on any other network/HTTP
        failure or a GraphQL error with no usable data. Mirrors the feed poller's
        unauthenticated fetch exactly.
        """

        payload = {"query": query, "variables": variables}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                async with session.post(API_URL, json=payload, headers=headers) as r:
                    if r.status == 429:
                        raise _RateLimited(
                            _parse_retry_after(r.headers.get("Retry-After"))
                        )
                    try:
                        data = await r.json()
                    except Exception:
                        data = None
                    if data is None:
                        raise _FetchError("AniList HTTP %s with no JSON body" % r.status)
        except _RateLimited:
            raise
        except _FetchError:
            raise
        except Exception as exc:  # timeout / connection reset / ...
            raise _FetchError(str(exc)) from exc

        if isinstance(data, dict) and data.get("errors") and not data.get("data"):
            raise _FetchError("AniList GraphQL errors: " + str(data.get("errors"))[:200])
        return data

    async def _space(self):
        """Pace successive AniList requests within a tick under the rate limit.

        Sleeps :data:`REQUEST_SPACING` before every request except the first of
        the tick, so the whole burst (staggered list refreshes + schedule pages)
        cannot 429 itself. ``self._spaced`` is reset at the top of every tick.
        """

        if self._spaced:
            await asyncio.sleep(REQUEST_SPACING)
        self._spaced = True

    # ------------------------------------------------------------------
    # Per-user CURRENT-list cache (bounded, house pattern)
    # ------------------------------------------------------------------
    def _list_cache_get(self, anilist_user_id):
        """Return the cached ``{media_id: progress}`` for an AniList user, or None.

        Returns whatever is cached even when stale - staleness only drives the
        lazy refresh (:meth:`_list_is_stale`), never eviction from a read - so a
        list keeps contributing to the union until a refresh replaces it.
        """

        hit = self._list_cache.get(anilist_user_id)
        return None if hit is None else hit[1]

    def _list_is_stale(self, anilist_user_id, now):
        """True when a user's cached list is missing or older than the TTL."""

        hit = self._list_cache.get(anilist_user_id)
        if hit is None:
            return True
        return now - hit[0] >= LIST_TTL

    def _list_cache_put(self, anilist_user_id, entries, now):
        """Cache a user's list, sweeping stale rows once past the size cap."""

        self._list_cache[anilist_user_id] = (now, entries)
        if len(self._list_cache) > LIST_SWEEP_AT:
            cutoff = now - LIST_TTL
            for key in [
                k for k, (ts, _e) in self._list_cache.items() if ts < cutoff
            ]:
                del self._list_cache[key]

    async def _fetch_public_list(self, anilist_user_id):
        """Fetch a user's PUBLIC CURRENT anime list as ``{media_id: progress}``.

        Unauthenticated. A private profile returns a null ``MediaListCollection``,
        which maps to ``{}`` (cached, so we do not hammer it); progress is coerced
        to an int (0 when unset) so the pure planner never compares against None.
        May raise :class:`_RateLimited` / :class:`_FetchError` (handled upstream).
        """

        data = await self._graphql(AIRING_LIST_QUERY, {"userId": anilist_user_id})
        collection = ((data or {}).get("data") or {}).get("MediaListCollection")
        if collection is None:
            return {}
        out = {}
        for lst in collection.get("lists") or []:
            for entry in lst.get("entries") or []:
                mid = entry.get("mediaId")
                if mid is None:
                    continue
                out[mid] = entry.get("progress") or 0
        return out

    async def _refresh_lists(self, anilist_user_ids, now):
        """Refresh every MISSING list this tick, plus a throttled slice of stale ones.

        ``anilist_user_ids`` is every DM opt-in's AniList user (the channel fan-out
        no longer derives from followed users' lists - it reads explicit
        subscriptions instead). A never-cached (missing) list is ALWAYS refreshed
        before the schedules fetch. The airing cursor is global and advances over
        the tracked-media union, so leaving a tracked user out of the union would
        drag the cursor past that user's airings and drop those episodes for good -
        worst right after a restart, when every list is uncached. Genuinely
        stale-but-present lists keep serving their last-known entries meanwhile, so
        they stay throttled to :data:`MAX_LIST_REFRESHES_PER_TICK` per tick (the
        rest ride later ticks) and steady-state staleness cannot burst the rate
        limit. Requests are paced by :meth:`_space`. A single user's non-429 failure
        is skipped; a 429 propagates so the tick can set an embargo across the
        whole poll.
        """

        missing = []
        stale_present = []
        for aid in sorted(anilist_user_ids):
            if aid not in self._list_cache:
                missing.append(aid)
            elif self._list_is_stale(aid, now):
                stale_present.append(aid)

        for aid in missing + stale_present[:MAX_LIST_REFRESHES_PER_TICK]:
            await self._space()
            try:
                entries = await self._fetch_public_list(aid)
            except _FetchError as exc:
                log.warning(
                    "AniList airing: list refresh failed for user %s (%s)", aid, exc
                )
                continue
            self._list_cache_put(aid, entries, now)

    async def _fetch_schedules(self, media_ids, cursor, now):
        """Fetch aired schedules in ``(cursor, now]``, paginating on full pages.

        Returns ``(rows, fetched_airing_ats, capped)``. ``rows`` are normalized
        dicts (``media_id`` / ``episode`` / ``airing_at`` / ``media``).
        ``fetched_airing_ats`` and ``capped`` feed :func:`af.advance_airing_cursor`,
        which clamps the cursor below the last fetched second under truncation (a
        capped last page leaves an unfetched tail whose oldest row may share a
        second with the last fetched row). Requests are paced by :meth:`_space`.
        May raise :class:`_RateLimited` / :class:`_FetchError`.
        """

        rows = []
        airing_ats = []
        capped = False
        for page in range(1, MAX_SCHEDULE_PAGES + 1):
            await self._space()
            data = await self._graphql(
                AIRING_SCHEDULE_QUERY,
                {
                    "mediaIds": media_ids,
                    "greater": cursor,
                    "lesser": now,
                    "page": page,
                    "perPage": PER_PAGE,
                },
            )
            batch = (
                ((data or {}).get("data") or {}).get("Page") or {}
            ).get("airingSchedules") or []
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                mid = raw.get("mediaId")
                episode = raw.get("episode")
                airing_at = raw.get("airingAt")
                if mid is None or episode is None or airing_at is None:
                    continue
                rows.append(
                    {
                        "media_id": mid,
                        "episode": episode,
                        "airing_at": airing_at,
                        "media": raw.get("media") or {},
                    }
                )
                airing_ats.append(airing_at)
            if len(batch) < PER_PAGE:
                break
        else:
            capped = True
            log.warning(
                "AniList airing: schedule page cap (%s) reached; holding the "
                "cursor below the last fetched second so the tail rides the "
                "next tick",
                MAX_SCHEDULE_PAGES,
            )
        return rows, airing_ats, capped

    # ------------------------------------------------------------------
    # Database access
    # ------------------------------------------------------------------
    async def _load_optins(self):
        return await self.bot.db_pool.fetch(
            "SELECT user_id, anilist_user_id FROM anilist_airing_optins "
            "WHERE enabled = TRUE;"
        )

    async def _load_channel_subs(self):
        """Explicit ANIME title subscriptions of every enabled feed.

        The channel fan-out is driven ONLY by these rows now
        (``anilist_channel_subs``): a feed posts a subscribed title's new episodes
        in its channel, independently of the DM opt-ins and of who the feed
        follows. A disabled feed is excluded (its channel must stay quiet).
        """

        return await self.bot.db_pool.fetch(
            "SELECT s.guild_id, s.channel_id, s.media_id "
            "FROM anilist_channel_subs s "
            "JOIN anilist_feeds fe "
            "  ON fe.guild_id = s.guild_id AND fe.channel_id = s.channel_id "
            "WHERE fe.enabled = TRUE AND s.media_type = 'ANIME';"
        )

    async def _load_cursor(self):
        row = await self.bot.db_pool.fetchrow(
            "SELECT last_airing_at FROM anilist_airing_state WHERE id = 1;"
        )
        return row["last_airing_at"] if row is not None else 0

    async def _save_cursor(self, last_airing_at):
        await self.bot.db_pool.execute(
            "INSERT INTO anilist_airing_state (id, last_airing_at, updated_at) "
            "VALUES (1, $1, now()) "
            "ON CONFLICT (id) DO UPDATE SET "
            "last_airing_at = GREATEST("
            "  anilist_airing_state.last_airing_at, EXCLUDED.last_airing_at), "
            "updated_at = now();",
            last_airing_at,
        )

    async def _disable_optin(self, user_id):
        try:
            await self.bot.db_pool.execute(
                "UPDATE anilist_airing_optins SET enabled = FALSE WHERE user_id = $1;",
                user_id,
            )
        except Exception:
            log.exception("AniList airing: could not disable opt-in for %s", user_id)

    # ------------------------------------------------------------------
    # Poller
    # ------------------------------------------------------------------
    @tasks.loop(seconds=POLL_SECONDS)
    async def _poll_airing(self):
        # Fully wrapped: an unexpected error must never kill the loop.
        try:
            await self._tick()
        except Exception:
            log.exception("AniList airing: poll tick failed")

    @_poll_airing.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    @_poll_airing.error
    async def _poll_error(self, error):
        log.exception("AniList airing: poll loop crashed; restarting", exc_info=error)
        self._poll_airing.restart()

    async def _tick(self):
        now = int(time.time())
        if now < self._embargo_until:
            return  # still under a 429 backoff

        optins = await self._load_optins()
        channel_subs = await self._load_channel_subs()
        if not optins and not channel_subs:
            return  # nobody tracked -> no API call

        cursor = await self._load_cursor()

        # First run ever (cursor 0): anti-backfill. Anchor to now and post
        # nothing, so we never dump a backlog of already-aired episodes.
        if cursor == 0:
            await self._save_cursor(now)
            return

        self._spaced = False
        now_mono = time.monotonic()

        # a. Tracked AniList users = every DM opt-in (the channel fan-out is driven
        # by explicit subscriptions, not by followed users' lists). Refresh their
        # public Watching lists (missing in full, stale throttled) before the
        # schedules fetch: the cursor advances over the tracked-media union, so a
        # tracked user missing from the union would drag it past their airings (the
        # C4 lesson).
        tracked_users = {row["anilist_user_id"] for row in optins}
        try:
            await self._refresh_lists(tracked_users, now_mono)
        except _RateLimited as exc:
            self._embargo_until = now + exc.retry_after
            log.warning(
                "AniList airing: rate limited during list refresh, backing off %ss",
                exc.retry_after,
            )
            return

        # Build the tracked-media union plus the two fan-out maps. Every tracked
        # list is refreshed above before we reach here (missing ones in full), so a
        # user is skipped only when their list fetch failed this tick - never merely
        # because it had not been cached yet. DM recipients keep the per-user
        # {media_id: progress} the DM planner gates on.
        lists_by_user = {}
        union = set()
        for row in optins:
            cached = self._list_cache_get(row["anilist_user_id"])
            if cached is None:
                continue
            lists_by_user[row["user_id"]] = cached
            union.update(cached.keys())

        # Channel fan-out is driven ONLY by explicit subscriptions. Each subscribed
        # media id joins the union BEFORE the schedules fetch (the C4 invariant), so
        # the global cursor can never advance past a subscribed title's airing even
        # when no opted-in user watches it. Channel posts are NOT progress-gated - a
        # guild post is for everyone - so channel_media is a bare media-id set.
        channel_media = {}
        for row in channel_subs:
            key = (row["guild_id"], row["channel_id"])
            channel_media.setdefault(key, set()).add(row["media_id"])
            union.add(row["media_id"])

        if not union:
            return  # nothing tracked yet -> no schedules call

        # d. One airingSchedules query over the whole union, paginated.
        try:
            aired, fetched_ats, capped = await self._fetch_schedules(
                sorted(union), cursor, now
            )
        except _RateLimited as exc:
            self._embargo_until = now + exc.retry_after
            log.warning(
                "AniList airing: rate limited during schedules, backing off %ss",
                exc.retry_after,
            )
            return
        except _FetchError as exc:
            log.warning(
                "AniList airing: schedules fetch failed (%s); cursor held", exc
            )
            return

        new_cursor = af.advance_airing_cursor(cursor, fetched_ats, capped=capped)

        # e. Fan out: one progress-gated DM per (user, aired-row) the DM planner
        # selects, then one post per (feed channel, aired-row) whose feed
        # subscribes to the media (not progress-gated). Both index the same aired
        # rows.
        rows_by_key = {(r["media_id"], r["episode"]): r for r in aired}
        plan = af.plan_airing_notifications(aired, lists_by_user)
        if plan:
            await self._deliver(plan, rows_by_key)
        channel_posts = plan_airing_channel_posts(aired, channel_media)
        if channel_posts:
            await self._deliver_channel_posts(channel_posts, rows_by_key)

        # The cursor advances regardless of delivery outcome (a closed DM disables
        # the user, it does not re-queue the episode), so a stuck DM can never
        # re-notify forever. Delivery is fully per-target guarded, so it never
        # raises.
        if new_cursor != cursor:
            await self._save_cursor(new_cursor)

    async def _deliver(self, plan, rows_by_key):
        """DM each planned notification. Never raises (each DM is guarded)."""

        for user_id, media_id, episode in plan:
            row = rows_by_key.get((media_id, episode))
            if row is None:
                continue
            await self._deliver_one(user_id, row)

    async def _deliver_channel_posts(self, channel_posts, rows_by_key):
        """Post each planned channel card. Never raises (each post is guarded)."""

        for _guild_id, channel_id, media_id, episode in channel_posts:
            row = rows_by_key.get((media_id, episode))
            if row is None:
                continue
            await self._deliver_channel(channel_id, row)

    async def _deliver_one(self, user_id, row):
        """DM one user one airing card, disabling them on a closed-DM Forbidden."""

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                log.warning("AniList airing: could not resolve user %s", user_id)
                return

        # Resolve the recipient's own language and render the card inside it: the
        # LayoutView calls _() at construction, so build and send both run under
        # the locale context.
        loc = await i18n.resolve_locale(self.bot, user_id=user_id)
        try:
            with i18n.locale(loc):
                await user.send(view=AiringCard(row))
        except discord.Forbidden:
            log.info(
                "AniList airing: DMs closed for user %s; disabling their opt-in",
                user_id,
            )
            await self._disable_optin(user_id)
        except discord.HTTPException:
            log.warning("AniList airing: failed to DM user %s", user_id)
        except Exception:
            log.exception("AniList airing: unexpected error DMing user %s", user_id)

    async def _deliver_channel(self, channel_id, row):
        """Post one airing card once in a feed channel (guarded; never raises).

        The card is the same :class:`AiringCard` the DM path sends; its Seen button
        is per-clicker (it writes the CLICKER's own progress at click time), so it is
        safe to post publicly. Rendered in the guild's locale.
        """

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log.warning(
                "AniList airing: feed channel %s is unresolvable", channel_id
            )
            return

        loc = await i18n.resolve_guild_locale(
            self.bot, getattr(channel, "guild", None)
        )
        try:
            with i18n.locale(loc):
                await channel.send(
                    view=AiringCard(row),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.Forbidden, discord.NotFound):
            log.warning(
                "AniList airing: delivery to channel %s failed (forbidden/gone)",
                channel_id,
            )
        except discord.HTTPException:
            log.warning(
                "AniList airing: HTTP error posting to channel %s", channel_id
            )
        except Exception:
            log.exception(
                "AniList airing: unexpected error posting to channel %s", channel_id
            )

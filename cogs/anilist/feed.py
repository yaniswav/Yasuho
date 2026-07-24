"""AniList activity feed: the global poller and its management commands.

A guild may configure up to two feed channels; each feed follows a set of
AniList users and this cog mirrors their new activities (list progress + text
posts) into the channel. A single ``tasks.loop`` poller fetches new activities
in batches from AniList's public GraphQL API and fans them out to the channels
that want them, leaning on the pure helpers in ``tools.anilist_feed`` for all
filtering/routing/coalescing/markdown work.

Cursor + dedup. ``Page.activities`` has no ``id_greater`` argument, so the
poller cursors on TWO marks kept in ``anilist_feed_state``: ``last_created_at``
drives the server-side ``createdAt_greater`` filter (unix seconds), and
``last_activity_id`` is a client-side id high-water mark. Two activities can
share a createdAt second, so the createdAt filter alone can duplicate or skip at
the boundary; dropping ids ``<= last_activity_id`` is the real dedup. Both marks
only ever advance.

Rendering lives behind two methods - ``_render_activity`` / ``_render_digest`` -
which return ``channel.send`` kwargs. They now build polished Components V2
layouts (:class:`ActivityCard` / :class:`ActivityDigest`); the poller, cursor
and commands never touch rendering.
"""

from __future__ import annotations

import asyncio
import logging
import time
import typing
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

# --- Re-exports -------------------------------------------------------------
#
# feed.py is the package's public module: the poller/cursor machinery and the
# cog live here, and the interactive/render/action code moved to feed_views,
# feed_render and feed_delivery. Every symbol that used to live in this module is
# re-imported below so that every existing ``cogs.anilist.feed.<name>`` import
# path (airing.py, chapters.py and the test suite) keeps resolving to the exact
# same object (``feed.Name is feed_x.Name``). Names the cog itself uses at
# runtime carry no marker; the rest are pure re-exports flagged for the linter.
from .feed_delivery import (
    _ACTION_DEBOUNCE,  # noqa: F401
    _ADD_STATUS_WORDS,  # noqa: F401
    ADD_LOOKUP_QUERY,  # noqa: F401
    REPLY_MAX_LENGTH,  # noqa: F401
    SAVE_REPLY_MUTATION,  # noqa: F401
    TOGGLE_LIKE_MUTATION,  # noqa: F401
    _activity_url,  # noqa: F401
    _authed_graphql,  # noqa: F401
    _AuthError,  # noqa: F401
    _check_debounce,  # noqa: F401
    _ConfigureEntryView,  # noqa: F401
    _deny_feed_action,  # noqa: F401
    _feed_ephemeral,  # noqa: F401
    _FetchError,
    _GoneError,  # noqa: F401
    _media_title,  # noqa: F401
    _parse_retry_after,
    _RateLimited,
    _ReplyModal,  # noqa: F401
    _resolve_token,  # noqa: F401
    _run_add,  # noqa: F401
    _run_like,  # noqa: F401
    _run_reply,  # noqa: F401
    _status_word,  # noqa: F401
    _throttle_for,
)
from .feed_render import (
    _LIST_ACTION_TEMPLATES,  # noqa: F401
    ActivityCard,
    ActivityDigest,
    _bold_link,  # noqa: F401
    _card_subline,  # noqa: F401
    _colour_from_media,  # noqa: F401
    _list_action,  # noqa: F401
    _user_summary,  # noqa: F401
)
from .feed_views import (
    _TYPE_LABELS,  # noqa: F401
    ADD_TEMPLATE,  # noqa: F401
    ANILIST_BLUE,  # noqa: F401
    CARD_ACCENT,  # noqa: F401
    LIKE_TEMPLATE,  # noqa: F401
    PANEL_DISABLED,  # noqa: F401
    PANEL_ENABLED,  # noqa: F401
    REPLY_TEMPLATE,  # noqa: F401
    AddFollowModal,  # noqa: F401
    AniListFeedPanel,
    FeedAddButton,
    FeedLikeButton,
    FeedReplyButton,
    _FeedListView,
    _FeedNoticeView,
    _refresh_layout,  # noqa: F401
    _SubsManagerView,
)
from .helpers import API_URL
from .queries import SEARCH_QUERY, VIEWER_QUERY
from tools import anilist_feed as af
from tools import anilist_feed_coalesce as afc
from tools import i18n
from tools.http import TIMEOUT, get_session
from tools.i18n import _

log = logging.getLogger(__name__)


# Poller cadence. 120s stays far below the (currently degraded 30/min) rate
# limit even with several chunks per tick.
POLL_SECONDS = 120

# Activity types we ask AniList for. MESSAGE (private wall posts) is excluded
# server-side; it also has no ``user`` field, so a selection assuming user on
# every member would fail.
POLL_TYPES = ["TEXT", "ANIME_LIST", "MANGA_LIST"]

# AniList silently clamps perPage to 50, so 50 is the real page size.
PER_PAGE = 50

# Hard safety cap on pages fetched per user-chunk per tick, so a pathological
# burst cannot spiral into an unbounded fetch. When a chunk hits the cap it has
# an unfetched tail (higher ids), so the global cursor is held at that chunk's
# highest fetched id/createdAt and the remainder rides the next tick instead of
# being skipped by dedup or the createdAt filter.
MAX_PAGES_PER_CHUNK = 4

# Minimum gap between successive GraphQL requests within a tick. It paces a
# multi-chunk / multi-page burst under even the degraded 30/min budget (1 req
# per 2s) so a backlogged install cannot 429 itself mid-tick.
REQUEST_SPACING = 2.0

# Auto-disable a feed after this many consecutive delivery failures.
MAX_DELIVERY_FAILURES = 10

# Upper bound on dead coalescing-card rows swept per poll tick. The sweep rides
# the poll tick (no new timer) and this cap keeps it O(1)-ish: a huge backlog
# drains over several ticks instead of one unbounded DELETE.
COALESCE_PRUNE_BATCH = 500

# AniList's ``userId_in`` array filter accepts up to ~10k ids. The poller already
# chunks the followed-id union by ``PER_PAGE`` (50) per request, so no single
# request's ``userId_in`` ever approaches that cap - each chunk carries at most 50
# ids. This threshold is a pure operational signal: if the TOTAL followed union
# ever grows this large the request count (ceil(union / 50) chunks per tick) is
# worth an operator's attention long before the API filter itself is a concern.
IN_FILTER_WARN_AT = 9000


# --- GraphQL ----------------------------------------------------------------

# Batched activity fetch. Inline fragments keep the ListActivity / TextActivity
# selections apart (TextActivity has no media; MessageActivity - excluded via
# type_in - has no user). sort: ID returns ascending by id.
ACTIVITY_QUERY = """
query ($userIds: [Int], $types: [ActivityType], $createdAtGreater: Int, $page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    activities(
      userId_in: $userIds
      type_in: $types
      createdAt_greater: $createdAtGreater
      sort: ID
    ) {
      __typename
      ... on ListActivity {
        id
        type
        status
        progress
        createdAt
        siteUrl
        likeCount
        replyCount
        user { id name siteUrl avatar { large } }
        media {
          id
          title { romaji english userPreferred }
          coverImage { extraLarge large color }
          bannerImage
          format
          isAdult
          siteUrl
          episodes
          chapters
          type
        }
      }
      ... on TextActivity {
        id
        type
        text(asHtml: false)
        createdAt
        siteUrl
        likeCount
        replyCount
        user { id name siteUrl avatar { large } }
      }
    }
  }
}
"""

# Resolve a username to the AniList user for the follow command.
USER_SEARCH_QUERY = """
query ($name: String) {
  User(search: $name) {
    id
    name
    siteUrl
    avatar { large }
  }
}
"""


def _chunk(seq, size):
    """Yield successive ``size``-long slices of ``seq``."""

    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _chunk_boundary(raw_activities):
    """Highest fetched ``(id, created_at)`` in a page-capped chunk.

    Activities come back ascending by id, so the highest id marks the end of
    what we managed to fetch for this chunk; its ``createdAt`` is the safe
    createdAt boundary (nothing at or below it in this chunk is unfetched).
    Returns ``None`` when no entry carries a usable id.
    """

    best_id = None
    best_created = 0
    for raw in raw_activities:
        if not isinstance(raw, dict):
            continue
        aid = raw.get("id")
        if aid is None:
            continue
        if best_id is None or aid > best_id:
            best_id = aid
            best_created = raw.get("createdAt") or 0
    if best_id is None:
        return None
    return best_id, best_created


def _normalize(raw):
    """Flatten a raw GraphQL activity into the dict the helpers/render expect.

    Returns ``None`` for anything without an id (which cannot be cursored or
    deduped). ``is_adult`` is read from the media for list activities and is
    always ``False`` for text activities.
    """

    if not isinstance(raw, dict):
        return None
    aid = raw.get("id")
    if aid is None:
        return None

    user = raw.get("user") or {}
    avatar = user.get("avatar") or {}
    base = {
        "id": aid,
        "type": raw.get("type"),
        "kind": raw.get("__typename"),
        "user_id": user.get("id"),
        "user_name": user.get("name"),
        "user_url": user.get("siteUrl"),
        "user_avatar": avatar.get("large"),
        "created_at": raw.get("createdAt") or 0,
        "site_url": raw.get("siteUrl"),
        "like_count": raw.get("likeCount"),
        "reply_count": raw.get("replyCount"),
        "is_adult": False,
    }

    if base["kind"] == "ListActivity":
        media = raw.get("media") or {}
        base["media"] = media
        base["status"] = raw.get("status")
        base["progress"] = raw.get("progress")
        base["is_adult"] = bool(media.get("isAdult"))
    elif base["kind"] == "TextActivity":
        base["text"] = raw.get("text")

    return base


class AniListFeed(commands.Cog):
    """Mirror followed AniList users' activity into per-guild feed channels."""

    def __init__(self, bot):
        self.bot = bot
        # Unix timestamp before which the poller stays quiet (429 embargo).
        self._embargo_until = 0
        self._poll_feeds.start()

    async def cog_load(self):
        # Register the feed's Like / Reply DynamicItems process-wide so their
        # clicks dispatch on EVERY card, including ones posted before this start
        # (the whole point of DynamicItem - no per-message view is needed).
        try:
            self.bot.add_dynamic_items(FeedLikeButton, FeedReplyButton, FeedAddButton)
        except Exception:
            log.exception("AniList feed: failed to register the action buttons")

    def cog_unload(self):
        self._poll_feeds.cancel()
        # Drop the dynamic-item registration so a clean reload does not leave a
        # stale template behind (it is re-added by the next cog_load).
        try:
            self.bot.remove_dynamic_items(
                FeedLikeButton, FeedReplyButton, FeedAddButton
            )
        except Exception:
            log.exception("AniList feed: failed to remove the action buttons")

    # ------------------------------------------------------------------
    # GraphQL plumbing (one session per call, matching the codebase pattern)
    # ------------------------------------------------------------------
    async def _graphql(self, query, variables):
        """POST a GraphQL request to AniList.

        Returns the parsed JSON. Raises :class:`_RateLimited` on a 429 (with the
        Retry-After seconds) and :class:`_FetchError` on any other network/HTTP
        failure or a GraphQL error with no usable data.
        """

        payload = {"query": query, "variables": variables}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        try:
            async with get_session(self.bot).post(
                API_URL, json=payload, headers=headers, timeout=TIMEOUT
            ) as r:
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

        # A "not found" (e.g. unknown username) returns data with a null field
        # plus errors - that is a normal result the caller inspects. Only treat
        # errors with NO data payload as a hard fetch failure.
        if isinstance(data, dict) and data.get("errors") and not data.get("data"):
            raise _FetchError("AniList GraphQL errors: " + str(data.get("errors"))[:200])
        return data

    async def _fetch_activities(self, user_ids, last_created):
        """Fetch new activities for ``user_ids`` since ``last_created``.

        Chunks the ids by 50 and paginates each chunk while a page is full, up
        to ``MAX_PAGES_PER_CHUNK``. ``createdAt_greater`` is ``last_created - 1``
        so the boundary second is re-included (dedup then drops the already-seen
        ids client-side). Requests are spaced by ``REQUEST_SPACING`` so a large,
        backlogged install cannot burst past the rate limit mid-tick. May raise
        :class:`_RateLimited` / :class:`_FetchError`.

        Returns ``(activities, safe_boundary)``. ``safe_boundary`` is ``None``
        when every chunk drained within the page cap; otherwise it is the
        ``(id, created_at)`` mark the global cursor may safely advance to - the
        lowest highest-fetched id / createdAt across the chunks that hit the cap.
        Those chunks still have an unfetched tail (higher ids), so the caller
        must not advance past this boundary or the tail would be lost.
        """

        created_greater = max(0, last_created - 1)
        activities = []
        safe_id = None
        safe_created = None
        first = True
        for chunk in _chunk(user_ids, PER_PAGE):
            chunk_batch = []
            capped = True
            for page in range(1, MAX_PAGES_PER_CHUNK + 1):
                if not first:
                    await asyncio.sleep(REQUEST_SPACING)
                first = False
                data = await self._graphql(
                    ACTIVITY_QUERY,
                    {
                        "userIds": chunk,
                        "types": POLL_TYPES,
                        "createdAtGreater": created_greater,
                        "page": page,
                        "perPage": PER_PAGE,
                    },
                )
                batch = (
                    ((data or {}).get("data") or {}).get("Page") or {}
                ).get("activities") or []
                chunk_batch.extend(batch)
                if len(batch) < PER_PAGE:
                    capped = False
                    break
            activities.extend(chunk_batch)
            if capped:
                # Every page (including the last) was full: we hit the cap with
                # more waiting. Bound the global cursor at this chunk's highest
                # fetched id/createdAt so its unfetched tail rides the next tick
                # instead of being skipped by dedup or the createdAt filter.
                log.warning(
                    "AniList feed: page cap (%s) reached for a user chunk; "
                    "holding the cursor so the remainder rides the next tick",
                    MAX_PAGES_PER_CHUNK,
                )
                boundary = _chunk_boundary(chunk_batch)
                if boundary is not None:
                    bid, bcreated = boundary
                    if safe_id is None or bid < safe_id:
                        safe_id = bid
                    if safe_created is None or bcreated < safe_created:
                        safe_created = bcreated
        safe_boundary = None if safe_id is None else (safe_id, safe_created)
        return activities, safe_boundary

    # ------------------------------------------------------------------
    # Database access
    # ------------------------------------------------------------------
    async def _load_feeds(self):
        return await self.bot.db_pool.fetch(
            "SELECT guild_id, channel_id, types, fail_count "
            "FROM anilist_feeds WHERE enabled = TRUE;"
        )

    async def _load_follows(self):
        return await self.bot.db_pool.fetch(
            "SELECT f.guild_id, f.channel_id, f.anilist_user_id "
            "FROM anilist_follows f "
            "JOIN anilist_feeds fe "
            "  ON fe.guild_id = f.guild_id AND fe.channel_id = f.channel_id "
            "WHERE fe.enabled = TRUE;"
        )

    async def _load_state(self):
        row = await self.bot.db_pool.fetchrow(
            "SELECT last_activity_id, last_created_at "
            "FROM anilist_feed_state WHERE id = 1;"
        )
        if row is None:
            return 0, 0
        return row["last_activity_id"], row["last_created_at"]

    async def _save_state(self, last_id, last_created):
        await self.bot.db_pool.execute(
            "INSERT INTO anilist_feed_state "
            "(id, last_activity_id, last_created_at, updated_at) "
            "VALUES (1, $1, $2, now()) "
            "ON CONFLICT (id) DO UPDATE SET "
            "last_activity_id = GREATEST("
            "  anilist_feed_state.last_activity_id, EXCLUDED.last_activity_id), "
            "last_created_at = GREATEST("
            "  anilist_feed_state.last_created_at, EXCLUDED.last_created_at), "
            "updated_at = now();",
            last_id,
            last_created,
        )

    async def _record_failure(self, feed):
        """Bump a feed's fail_count and auto-disable it past the threshold."""

        try:
            row = await self.bot.db_pool.fetchrow(
                "UPDATE anilist_feeds "
                "SET fail_count = fail_count + 1, "
                "enabled = CASE WHEN fail_count + 1 >= $3 THEN FALSE ELSE enabled END "
                "WHERE guild_id = $1 AND channel_id = $2 "
                "RETURNING fail_count, enabled;",
                feed["guild_id"],
                feed["channel_id"],
                MAX_DELIVERY_FAILURES,
            )
        except Exception:
            log.exception("AniList feed: could not record a delivery failure")
            return
        if row is not None and not row["enabled"]:
            log.warning(
                "AniList feed disabled after %s failures: guild=%s channel=%s",
                row["fail_count"],
                feed["guild_id"],
                feed["channel_id"],
            )

    async def _reset_failure(self, feed):
        """Clear a feed's fail_count after a successful delivery."""

        try:
            await self.bot.db_pool.execute(
                "UPDATE anilist_feeds SET fail_count = 0 "
                "WHERE guild_id = $1 AND channel_id = $2 AND fail_count <> 0;",
                feed["guild_id"],
                feed["channel_id"],
            )
        except Exception:
            log.exception("AniList feed: could not reset fail_count")

    # ------------------------------------------------------------------
    # Poller
    # ------------------------------------------------------------------
    @tasks.loop(seconds=POLL_SECONDS)
    async def _poll_feeds(self):
        # Fully wrapped: an unexpected error must never kill the loop.
        try:
            await self._tick()
        except Exception:
            log.exception("AniList feed: poll tick failed")

    @_poll_feeds.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    @_poll_feeds.error
    async def _poll_error(self, error):
        log.exception("AniList feed: poll loop crashed; restarting", exc_info=error)
        self._poll_feeds.restart()

    async def _tick(self):
        now = int(time.time())
        if now < self._embargo_until:
            return  # still under a 429 backoff

        # Coalescing-card maintenance. Drop rows whose live card is certainly
        # dead (untouched past AGE_CAP + PRUNE_GRACE) so the table stays ~1 row
        # per active reading session. Bounded, index-served, DB-only and best
        # effort - it rides this tick (no new timer) and is NOT part of the
        # cursor/dedup machinery below, which stays byte-identical.
        await self._prune_coalesce_posts()

        feeds = await self._load_feeds()
        if not feeds:
            return

        follow_rows = await self._load_follows()
        follows_by_channel = {}
        followed_ids = set()
        for row in follow_rows:
            key = (row["guild_id"], row["channel_id"])
            follows_by_channel.setdefault(key, set()).add(row["anilist_user_id"])
            followed_ids.add(row["anilist_user_id"])
        if not followed_ids:
            return  # nobody followed anywhere -> no API call

        # Operational guard: the followed union drives the per-tick request count
        # (ceil(len / PER_PAGE) chunks) and, in the limit, AniList's ~10k userId_in
        # cap. Chunking keeps each request tiny, so this only ever warns; it never
        # drops a follow.
        if len(followed_ids) >= IN_FILTER_WARN_AT:
            log.warning(
                "AniList feed: %s followed users is approaching AniList's ~10k "
                "userId_in cap (%s chunk requests/tick); consider sharding feeds",
                len(followed_ids),
                -(-len(followed_ids) // PER_PAGE),
            )

        last_id, last_created = await self._load_state()

        # First run ever (both marks zero): anti-backfill. Anchor the createdAt
        # cursor to now and post nothing, so we never dump historical activity.
        if last_id == 0 and last_created == 0:
            await self._save_state(0, now)
            return

        try:
            raw, safe_boundary = await self._fetch_activities(
                sorted(followed_ids), last_created
            )
        except _RateLimited as exc:
            self._embargo_until = now + exc.retry_after
            log.warning(
                "AniList feed: rate limited, backing off for %ss", exc.retry_after
            )
            return
        except _FetchError as exc:
            # Abort cleanly; cursors are NOT advanced past unprocessed work.
            log.warning("AniList feed: fetch failed (%s); cursors held", exc)
            return

        normalized = [n for n in (_normalize(a) for a in raw) if n is not None]

        # Advance the high-water marks past everything we fetched (even the
        # dedup-filtered ones - they were fetched successfully).
        new_id, new_created = last_id, last_created
        for n in normalized:
            if n["id"] > new_id:
                new_id = n["id"]
            if n["created_at"] > new_created:
                new_created = n["created_at"]

        # A page-capped chunk has an unfetched tail (higher ids). Never let a
        # different chunk's fresher max drag the global marks past that chunk's
        # safe boundary, or its tail is skipped forever by dedup / the createdAt
        # filter. Everything above the boundary is held for the next tick.
        if safe_boundary is not None:
            safe_id, safe_created = safe_boundary
            new_id = max(last_id, min(new_id, safe_id))
            new_created = max(last_created, min(new_created, safe_created))

        # Real dedup: only ids strictly beyond the id mark are actually new, and
        # nothing beyond the safe boundary (>= new_id here) is delivered yet.
        fresh = [n for n in normalized if last_id < n["id"] <= new_id]
        if fresh:
            await self._dispatch(feeds, follows_by_channel, fresh)

        if new_id != last_id or new_created != last_created:
            await self._save_state(new_id, new_created)

    async def _dispatch(self, feeds, follows_by_channel, activities):
        """Route ``activities`` to the feeds and deliver each channel's share."""

        feed_dicts = []
        feed_by_channel = {}
        for feed in feeds:
            ids = follows_by_channel.get((feed["guild_id"], feed["channel_id"]))
            if not ids:
                continue
            feed_by_channel[feed["channel_id"]] = feed
            feed_dicts.append(
                {
                    "channel_id": feed["channel_id"],
                    "types": set(feed["types"] or ()),
                    "followed_ids": ids,
                    "allow_adult": self._allow_adult(feed["channel_id"]),
                }
            )

        routed = af.route_activities(activities, feed_dicts)
        for channel_id, items in routed.items():
            feed = feed_by_channel.get(channel_id)
            if feed is None:
                continue
            await self._deliver_channel(feed, channel_id, items)

    def _allow_adult(self, channel_id):
        """Whether the destination channel/thread allows adult activities.

        Threads delegate ``is_nsfw()`` to their parent; an unresolvable channel
        defaults to False (and is handled as a delivery failure at send time).
        """

        channel = self.bot.get_channel(channel_id)
        is_nsfw = getattr(channel, "is_nsfw", None)
        if not callable(is_nsfw):
            return False
        try:
            return bool(is_nsfw())
        except Exception:
            return False

    async def _deliver_channel(self, feed, channel_id, items):
        """Post one channel's activities, tracking delivery success/failure."""

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            await self._record_failure(feed)
            return

        # Resolve the destination guild's language once per channel and render
        # every card/digest inside it: the LayoutViews call _() at construction
        # time, so both the build and send must run under the locale context. A
        # Thread exposes .guild too; an unresolvable guild falls back to default.
        loc = await i18n.resolve_guild_locale(
            self.bot, getattr(channel, "guild", None)
        )
        try:
            with i18n.locale(loc):
                full, digest = af.plan_posts(items)
                for activity in full:
                    await self._deliver_card(feed["guild_id"], channel, activity)
                if digest:
                    # The digest (the busy-tick remainder) is never coalesced: it
                    # is a fresh, presentational summary and holds no record.
                    await channel.send(
                        allowed_mentions=discord.AllowedMentions.none(),
                        **self._render_digest(digest),
                    )
        except (discord.Forbidden, discord.NotFound):
            log.warning(
                "AniList feed: delivery to channel %s failed (forbidden/gone)",
                channel_id,
            )
            await self._record_failure(feed)
        except discord.HTTPException:
            # Count it: the cursor advances regardless of delivery, so a
            # sustained Discord outage would otherwise silently drop every batch.
            # Letting repeated HTTP failures accrue eventually auto-disables the
            # feed; a lone blip is cleared by the next success via _reset_failure.
            log.exception(
                "AniList feed: HTTP error delivering to channel %s", channel_id
            )
            await self._record_failure(feed)
        except Exception:
            log.exception(
                "AniList feed: unexpected delivery error for channel %s", channel_id
            )
        else:
            if feed["fail_count"]:
                await self._reset_failure(feed)

    # ------------------------------------------------------------------
    # Coalescing delivery. A reader who saves ch.50 then ch.54 on the same
    # manga emits two AniList activities where the second supersedes the first;
    # rather than post two cards we fold consecutive same-status progress
    # increments into ONE card, EDITED in place (a Discord edit is silent = zero
    # notification), within a session window. The pure decision lives in
    # tools.anilist_feed_coalesce; here is the thin I/O shell around it. Each
    # channel keeps its OWN record + message id, so fan-out edits independently.
    # ------------------------------------------------------------------
    async def _deliver_card(self, guild_id, channel, activity):
        """Deliver one full activity, coalescing consecutive list-progress saves.

        A coalescible list-progress activity folds into the live card for its
        ``(channel, user, media)`` slot when
        :func:`~tools.anilist_feed_coalesce.decide_delivery` says EDIT - a silent
        in-place edit whose freshly rebuilt :class:`ActivityCard` carries the
        LATEST activity id (so the Like/Reply/Add buttons act on it) - otherwise
        a fresh card is posted and (re)recorded. Text posts, progress-less list
        activities and any activity missing a user/media key never coalesce: they
        post fresh and hold no record. A 404 on the edit (the card was deleted in
        THIS channel) falls back to a fresh post here only, overwriting the stale
        record; other channels' rows are untouched.
        """

        user_id = activity.get("user_id")
        media_id = (activity.get("media") or {}).get("id")

        # Only a list-progress activity with a full (user, media) key can key a
        # record; everything else posts fresh and is never tracked.
        if not afc.is_coalescible(activity) or user_id is None or media_id is None:
            await channel.send(
                allowed_mentions=discord.AllowedMentions.none(),
                **self._render_activity(activity),
            )
            return

        record = await self._load_coalesce_record(channel.id, user_id, media_id)
        decision = afc.decide_delivery(
            activity, record, datetime.now(timezone.utc)
        )

        if decision.action == afc.EDIT:
            try:
                await channel.get_partial_message(decision.message_id).edit(
                    allowed_mentions=discord.AllowedMentions.none(),
                    **self._render_activity(activity),
                )
            except discord.NotFound:
                # The live card was deleted in THIS channel: fall through to a
                # fresh post + record overwrite. Other channels stay untouched.
                log.info(
                    "AniList feed: coalescing card %s gone in channel %s; "
                    "reposting",
                    decision.message_id,
                    channel.id,
                )
            else:
                await self._touch_coalesce_record(
                    channel.id, user_id, media_id, activity
                )
                return

        message = await channel.send(
            allowed_mentions=discord.AllowedMentions.none(),
            **self._render_activity(activity),
        )
        await self._record_coalesce_post(
            guild_id, channel.id, user_id, media_id, message.id, activity
        )

    async def _load_coalesce_record(self, channel_id, user_id, media_id):
        """Load the live coalescing record for a slot, or ``None``.

        Reads straight from ``anilist_feed_posts`` (never an in-memory cache), so
        an edit resumes across a restart: the message id lives in the table.
        """

        row = await self.bot.db_pool.fetchrow(
            "SELECT message_id, status, last_progress, created_at, updated_at "
            "FROM anilist_feed_posts "
            "WHERE channel_id = $1 AND user_id = $2 AND media_id = $3;",
            channel_id,
            user_id,
            media_id,
        )
        if row is None:
            return None
        return afc.CoalesceRecord(
            message_id=row["message_id"],
            status=row["status"],
            last_progress=row["last_progress"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _record_coalesce_post(
        self, guild_id, channel_id, user_id, media_id, message_id, activity
    ):
        """Upsert the coalescing row for a freshly posted card (both clocks reset).

        A new card starts a new session, so BOTH ``created_at`` (the AGE_CAP
        clock) and ``updated_at`` (the SESSION_GAP clock) are set to now, even
        when this overwrites a stale row for the same slot.
        """

        await self.bot.db_pool.execute(
            "INSERT INTO anilist_feed_posts "
            "(guild_id, channel_id, user_id, media_id, message_id, activity_id, "
            " last_progress, status, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), now()) "
            "ON CONFLICT (channel_id, user_id, media_id) DO UPDATE SET "
            "guild_id = EXCLUDED.guild_id, "
            "message_id = EXCLUDED.message_id, "
            "activity_id = EXCLUDED.activity_id, "
            "last_progress = EXCLUDED.last_progress, "
            "status = EXCLUDED.status, "
            "created_at = now(), "
            "updated_at = now();",
            guild_id,
            channel_id,
            user_id,
            media_id,
            message_id,
            activity.get("id"),
            activity.get("progress"),
            activity.get("status"),
        )

    async def _touch_coalesce_record(self, channel_id, user_id, media_id, activity):
        """Advance the coalescing row after a silent in-place edit.

        The card keeps its message and its first-post time (the AGE_CAP clock),
        so only ``activity_id`` / ``last_progress`` / ``status`` and the
        SESSION_GAP clock (``updated_at``) move forward.
        """

        await self.bot.db_pool.execute(
            "UPDATE anilist_feed_posts SET "
            "activity_id = $4, last_progress = $5, status = $6, updated_at = now() "
            "WHERE channel_id = $1 AND user_id = $2 AND media_id = $3;",
            channel_id,
            user_id,
            media_id,
            activity.get("id"),
            activity.get("progress"),
            activity.get("status"),
        )

    async def _prune_coalesce_posts(self):
        """Delete coalescing rows whose card is certainly dead (bounded sweep).

        A row is dead once its last edit (``updated_at``) is older than
        ``AGE_CAP + PRUNE_GRACE``: the session has lapsed (the card can no longer
        be edited) and an active card is touched at least every ``SESSION_GAP``.
        Rides the ``anilist_feed_posts_prune_idx`` and caps each sweep at
        :data:`COALESCE_PRUNE_BATCH` rows. Best-effort: a failure here must never
        disturb the poll.
        """

        dead_after = afc.AGE_CAP + afc.PRUNE_GRACE
        try:
            await self.bot.db_pool.execute(
                "DELETE FROM anilist_feed_posts WHERE ctid IN ("
                "  SELECT ctid FROM anilist_feed_posts "
                "  WHERE updated_at < now() - ($1 * interval '1 second') "
                "  ORDER BY updated_at LIMIT $2"
                ");",
                dead_after,
                COALESCE_PRUNE_BATCH,
            )
        except Exception:
            log.exception("AniList feed: coalescing-card prune failed")

    # ------------------------------------------------------------------
    # Rendering - the send-kwargs boundary. Both return a Components V2
    # LayoutView (ActivityCard / ActivityDigest); the layout craft lives in
    # those classes above, keeping this method boundary a one-liner.
    # ------------------------------------------------------------------
    def _render_activity(self, activity):
        """Render one activity into send kwargs (``dict(view=...)``)."""

        return {"view": ActivityCard(activity)}

    def _render_digest(self, items):
        """Render the coalesced remainder into send kwargs (``dict(view=...)``)."""

        return {"view": ActivityDigest(items)}

    # ------------------------------------------------------------------
    # Management commands
    # ------------------------------------------------------------------
    async def _feeds_for_guild(self, guild_id):
        return await self.bot.db_pool.fetch(
            "SELECT channel_id, types, self_add, enabled, fail_count "
            "FROM anilist_feeds WHERE guild_id = $1 ORDER BY created_at;",
            guild_id,
        )

    async def _follows_for_feed(self, guild_id, channel_id):
        return await self.bot.db_pool.fetch(
            "SELECT anilist_user_id, anilist_username FROM anilist_follows "
            "WHERE guild_id = $1 AND channel_id = $2 ORDER BY anilist_username;",
            guild_id,
            channel_id,
        )

    async def _create_feed(self, guild_id, channel_id):
        """Create a feed on ``channel_id``. Returns an error string, else None."""

        exists = await self.bot.db_pool.fetchval(
            "SELECT 1 FROM anilist_feeds WHERE guild_id = $1 AND channel_id = $2;",
            guild_id,
            channel_id,
        )
        if exists:
            return _("{channel} is already an AniList feed.").format(
                channel=f"<#{channel_id}>"
            )
        count = await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) FROM anilist_feeds WHERE guild_id = $1;", guild_id
        )
        if count >= af.MAX_FEEDS_PER_GUILD:
            return _(
                "This server already has the maximum of {max} feeds. Delete "
                "one first."
            ).format(max=af.MAX_FEEDS_PER_GUILD)
        await self.bot.db_pool.execute(
            "INSERT INTO anilist_feeds (guild_id, channel_id) VALUES ($1, $2);",
            guild_id,
            channel_id,
        )
        return None

    async def _move_feed(self, guild_id, old_channel_id, new_channel_id):
        """Move a feed (and its follows) to a new channel, in one transaction.

        ``channel_id`` is part of the primary key on both tables, so a move is
        implemented as delete+insert of the feed row plus an UPDATE of its
        follows' ``channel_id`` - all inside a single transaction so a failure
        partway through can never leave the feed split across two channels.
        Returns an error string, else None.
        """

        if old_channel_id == new_channel_id:
            return None
        exists = await self.bot.db_pool.fetchval(
            "SELECT 1 FROM anilist_feeds WHERE guild_id = $1 AND channel_id = $2;",
            guild_id,
            new_channel_id,
        )
        if exists:
            return _("{channel} is already an AniList feed.").format(
                channel=f"<#{new_channel_id}>"
            )
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT types, self_add, enabled, fail_count "
                    "FROM anilist_feeds WHERE guild_id = $1 AND channel_id = $2;",
                    guild_id,
                    old_channel_id,
                )
                if row is None:
                    return _("That feed no longer exists.")
                await conn.execute(
                    "UPDATE anilist_follows SET channel_id = $3 "
                    "WHERE guild_id = $1 AND channel_id = $2;",
                    guild_id,
                    old_channel_id,
                    new_channel_id,
                )
                await conn.execute(
                    "UPDATE anilist_channel_subs SET channel_id = $3 "
                    "WHERE guild_id = $1 AND channel_id = $2;",
                    guild_id,
                    old_channel_id,
                    new_channel_id,
                )
                await conn.execute(
                    "DELETE FROM anilist_feeds WHERE guild_id = $1 AND channel_id = $2;",
                    guild_id,
                    old_channel_id,
                )
                await conn.execute(
                    "INSERT INTO anilist_feeds "
                    "(guild_id, channel_id, types, self_add, enabled, fail_count) "
                    "VALUES ($1, $2, $3, $4, $5, $6);",
                    guild_id,
                    new_channel_id,
                    row["types"],
                    row["self_add"],
                    row["enabled"],
                    row["fail_count"],
                )
        return None

    async def _set_types(self, guild_id, channel_id, types):
        ordered = sorted(types, key=af.ALLOWED_TYPES.index)
        await self.bot.db_pool.execute(
            "UPDATE anilist_feeds SET types = $3::text[] "
            "WHERE guild_id = $1 AND channel_id = $2;",
            guild_id,
            channel_id,
            ordered,
        )

    async def _toggle_self_add(self, guild_id, channel_id):
        await self.bot.db_pool.execute(
            "UPDATE anilist_feeds SET self_add = NOT self_add "
            "WHERE guild_id = $1 AND channel_id = $2;",
            guild_id,
            channel_id,
        )

    # ------------------------------------------------------------------
    # Tracked-releases subscriptions (the per-feed explicit-title circuit)
    # ------------------------------------------------------------------
    async def _channel_subs_for_feed(self, guild_id, channel_id):
        """Every tracked-release subscription of a feed, ordered for the panel."""

        return await self.bot.db_pool.fetch(
            "SELECT media_id, media_type, title FROM anilist_channel_subs "
            "WHERE guild_id = $1 AND channel_id = $2 "
            "ORDER BY media_type, lower(title), media_id;",
            guild_id,
            channel_id,
        )

    async def _channel_sub_count(self, guild_id, channel_id):
        return await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) FROM anilist_channel_subs "
            "WHERE guild_id = $1 AND channel_id = $2;",
            guild_id,
            channel_id,
        )

    async def _add_channel_sub(
        self, guild_id, channel_id, media_id, media_type, title, added_by
    ):
        """Insert/refresh a subscription, enforcing the per-feed cap.

        Returns an error string when the media type is unusable, no usable
        display title survived (romaji/english both empty, which would store a
        silent no-op), or the feed is already at
        :data:`af.MAX_SUBS_PER_FEED` with this title not yet tracked;
        else ``None`` after the upsert (which refreshes the cached title). Never
        touches a token.
        """

        if (
            media_id is None
            or media_type not in ("ANIME", "MANGA")
            or not (title and title.strip())
        ):
            # A subscription with no usable display title is unrecoverable: a
            # MANGA can never be MangaDex-mapped (the search is seeded from this
            # title), and the panel has nothing to render. Reject it rather than
            # store a silent no-op the admin was told is tracked.
            return _("I couldn't read that title - try searching again.")
        already = await self.bot.db_pool.fetchval(
            "SELECT 1 FROM anilist_channel_subs "
            "WHERE guild_id = $1 AND channel_id = $2 AND media_id = $3;",
            guild_id,
            channel_id,
            media_id,
        )
        if af.sub_cap_exceeded(
            await self._channel_sub_count(guild_id, channel_id), bool(already)
        ):
            return _(
                "This feed already tracks the maximum of {max} titles. Remove "
                "one first."
            ).format(max=af.MAX_SUBS_PER_FEED)
        await self.bot.db_pool.execute(
            "INSERT INTO anilist_channel_subs "
            "(guild_id, channel_id, media_id, media_type, title, added_by) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (guild_id, channel_id, media_id) DO UPDATE SET "
            "media_type = EXCLUDED.media_type, title = EXCLUDED.title;",
            guild_id,
            channel_id,
            media_id,
            media_type,
            title,
            added_by,
        )
        return None

    async def _remove_channel_sub(self, guild_id, channel_id, media_id):
        await self.bot.db_pool.execute(
            "DELETE FROM anilist_channel_subs "
            "WHERE guild_id = $1 AND channel_id = $2 AND media_id = $3;",
            guild_id,
            channel_id,
            media_id,
        )

    async def _search_channel_candidates(self, query):
        """Cross-type AniList search (UNAUTHENTICATED) for subscribable titles.

        Reuses the shared ``SEARCH_QUERY`` the update flow uses, so a candidate
        carries the ``type`` (ANIME/MANGA) the subscription needs. Returns the raw
        media dicts, or ``[]`` on an empty query or any AniList failure (the caller
        treats both as "no match"). No token is ever used.
        """

        query = (query or "").strip()
        if not query:
            return []
        # This is an admin-triggered INTERACTIVE search, not a poller call, so it
        # shares the interactive ceiling with the buttons/lookups (allow_global
        # backstop only - the caller carries no per-user identity here). A dropped
        # search degrades to the method's normal "no match" shape; the poller's own
        # _graphql path in _fetch_activities stays untouched.
        throttle = _throttle_for(self.bot)
        if throttle is not None and not throttle.allow_global():
            log.warning(
                "AniList interactive ceiling reached; dropping an admin title "
                "search to protect the pollers"
            )
            return []
        try:
            data = await self._graphql(SEARCH_QUERY, {"search": query})
        except (_RateLimited, _FetchError):
            return []
        page = ((data or {}).get("data") or {}).get("Page") or {}
        return page.get("media") or []

    async def _render_subs_manager(
        self, interaction, guild, author_id, channel_id, *, page=0, note=None, new=False
    ):
        """Render the ephemeral tracked-releases manager from fresh DB state.

        ``new=True`` sends a fresh ephemeral message (the panel button's first
        open); otherwise it edits the manager message in place (a remove / paging /
        add / cancel step). Loads the feed's subscriptions, builds a
        :class:`_SubsManagerView` and binds its ``message`` so the timeout cleanup
        works.
        """

        subs = await self._channel_subs_for_feed(guild.id, channel_id)
        view = _SubsManagerView(
            self, guild, author_id, channel_id, subs, page=page, note=note
        )
        # A Components V2 LayoutView carries its content inside the view, so every
        # send/edit passes ``view=`` only (no embed): Discord rejects an ``embed=``
        # on a CV2 message and this manager is CV2 from its first render.
        if new:
            await interaction.response.send_message(view=view, ephemeral=True)
            view.message = await interaction.original_response()
            return view
        if interaction.response.is_done():
            view.message = await interaction.edit_original_response(view=view)
        else:
            await interaction.response.edit_message(view=view)
            view.message = await interaction.original_response()
        return view

    async def _set_enabled(self, guild_id, channel_id, enabled):
        await self.bot.db_pool.execute(
            "UPDATE anilist_feeds SET enabled = $3, "
            "fail_count = CASE WHEN $3 THEN 0 ELSE fail_count END "
            "WHERE guild_id = $1 AND channel_id = $2;",
            guild_id,
            channel_id,
            enabled,
        )

    async def _delete_feed_rows(self, guild_id, channel_id):
        """Delete a feed and its follows in one transaction.

        Returns ``True`` when a feed row was actually deleted, else ``False``.
        """

        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM anilist_follows "
                    "WHERE guild_id = $1 AND channel_id = $2;",
                    guild_id,
                    channel_id,
                )
                await conn.execute(
                    "DELETE FROM anilist_channel_subs "
                    "WHERE guild_id = $1 AND channel_id = $2;",
                    guild_id,
                    channel_id,
                )
                result = await conn.execute(
                    "DELETE FROM anilist_feeds WHERE guild_id = $1 AND channel_id = $2;",
                    guild_id,
                    channel_id,
                )
        return result.split()[-1] != "0"

    async def _resolve_anilist_user(self, username):
        """Resolve a username via AniList's User(search).

        Returns ``(user_id, name, url, error_message)`` - exactly one of
        ``user_id`` or ``error_message`` is meaningfully set.
        """

        username = (username or "").strip()
        if not username:
            return None, None, None, _("Give me an AniList username to follow.")

        # Admin-triggered INTERACTIVE lookup: share the interactive ceiling with
        # the buttons/lookups (allow_global backstop only). A drop reuses the same
        # rate-limit message the AniList-429 branch already returns; the poller's
        # own _graphql path in _fetch_activities is never gated here.
        throttle = _throttle_for(self.bot)
        if throttle is not None and not throttle.allow_global():
            log.warning(
                "AniList interactive ceiling reached; dropping an admin follow "
                "lookup to protect the pollers"
            )
            return (
                None,
                None,
                None,
                _(
                    "AniList is rate limiting me right now - try again in a "
                    "minute."
                ),
            )

        try:
            data = await self._graphql(USER_SEARCH_QUERY, {"name": username})
        except _RateLimited:
            return (
                None,
                None,
                None,
                _(
                    "AniList is rate limiting me right now - try again in a "
                    "minute."
                ),
            )
        except _FetchError:
            return (
                None,
                None,
                None,
                _("I could not reach AniList - try again shortly."),
            )

        user = ((data or {}).get("data") or {}).get("User")
        if not user or user.get("id") is None:
            return (
                None,
                None,
                None,
                _("I found no AniList user named **{name}**.").format(
                    name=username
                ),
            )
        return user["id"], user.get("name") or username, user.get("siteUrl"), None

    async def _follow_count(self, guild_id, channel_id):
        return await self.bot.db_pool.fetchval(
            "SELECT COUNT(*) FROM anilist_follows "
            "WHERE guild_id = $1 AND channel_id = $2;",
            guild_id,
            channel_id,
        )

    async def _follow_exists(self, guild_id, channel_id, user_id):
        row = await self.bot.db_pool.fetchval(
            "SELECT 1 FROM anilist_follows "
            "WHERE guild_id = $1 AND channel_id = $2 AND anilist_user_id = $3;",
            guild_id,
            channel_id,
            user_id,
        )
        return bool(row)

    async def _insert_follow(self, guild_id, channel_id, user_id, name, added_by):
        await self.bot.db_pool.execute(
            "INSERT INTO anilist_follows "
            "(guild_id, channel_id, anilist_user_id, anilist_username, added_by) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (guild_id, channel_id, anilist_user_id) "
            "DO UPDATE SET anilist_username = EXCLUDED.anilist_username;",
            guild_id,
            channel_id,
            user_id,
            name,
            added_by,
        )

    async def _add_follow(self, guild_id, channel_id, user_id, name, added_by):
        """Insert/refresh a follow, enforcing the per-feed cap.

        Returns an error string when the feed is already at
        :data:`af.MAX_FOLLOWS_PER_FEED`, else None.
        """

        if not await self._follow_exists(guild_id, channel_id, user_id):
            count = await self._follow_count(guild_id, channel_id)
            if count >= af.MAX_FOLLOWS_PER_FEED:
                return _(
                    "This feed already follows the maximum of {max} users."
                ).format(max=af.MAX_FOLLOWS_PER_FEED)
        await self._insert_follow(guild_id, channel_id, user_id, name, added_by)
        return None

    async def _remove_follow(self, guild_id, channel_id, user_id):
        await self.bot.db_pool.execute(
            "DELETE FROM anilist_follows "
            "WHERE guild_id = $1 AND channel_id = $2 AND anilist_user_id = $3;",
            guild_id,
            channel_id,
            user_id,
        )

    async def _open_panel(self, ctx):
        feeds = await self._feeds_for_guild(ctx.guild.id)
        selected_channel_id = feeds[0]["channel_id"] if feeds else None
        follows = (
            await self._follows_for_feed(ctx.guild.id, selected_channel_id)
            if selected_channel_id is not None
            else []
        )
        subs_count = (
            await self._channel_sub_count(ctx.guild.id, selected_channel_id)
            if selected_channel_id is not None
            else 0
        )
        view = AniListFeedPanel(
            self,
            ctx.guild,
            ctx.author.id,
            feeds,
            selected_channel_id,
            follows,
            subs_count,
        )
        view.message = await ctx.send(view=view)

    async def _resolve_target(self, ctx):
        """Pick the feed a follow/unfollow applies to.

        Returns ``(channel_id, error_message)`` with exactly one set. With one
        feed it is used directly; with two, the current channel wins if it is a
        feed, else the user is asked to run the command in a feed channel.
        """

        # Only enabled feeds are valid follow/unfollow targets: an auto-disabled
        # feed is not read by the poller, so attaching a follow to it does
        # nothing, and it must not count toward the multi-feed disambiguation.
        feeds = [
            feed
            for feed in await self._feeds_for_guild(ctx.guild.id)
            if feed["enabled"]
        ]
        if not feeds:
            return None, _(
                "This server has no active AniList feed. Create or re-enable one "
                "with `/anilistfeed set` first."
            )
        if len(feeds) == 1:
            return feeds[0]["channel_id"], None

        channel_ids = {feed["channel_id"] for feed in feeds}
        if ctx.channel.id in channel_ids:
            return ctx.channel.id, None
        channels = ", ".join(f"<#{cid}>" for cid in channel_ids)
        return None, _(
            "This server has several feeds. Run this in a feed channel "
            "({channels})."
        ).format(channels=channels)

    @commands.hybrid_group(name="anilistfeed", aliases=["alfeed"])
    @commands.guild_only()
    async def anilistfeed(self, ctx: commands.Context):
        """Open this server's AniList feed control panel."""

        if ctx.invoked_subcommand is None:
            # The panel is admin-only, but the `me` subcommand deliberately is
            # NOT: a group-level manage_guild check runs before every subcommand
            # (early_invoke), which would wrongly gate `me` too. So the gate
            # lives here, on the bare-panel path only; each admin subcommand
            # carries its own manage_guild check.
            if not ctx.author.guild_permissions.manage_guild:
                raise commands.MissingPermissions(["manage_guild"])
            await self._open_panel(ctx)

    @anilistfeed.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(channel="The channel for the feed (defaults to here).")
    async def anilistfeed_set(
        self,
        ctx: commands.Context,
        channel: typing.Optional[
            typing.Union[discord.TextChannel, discord.Thread]
        ] = None,
    ):
        """Create or re-enable an AniList feed on a channel (default: here)."""

        target = channel or ctx.channel
        row = await self.bot.db_pool.fetchrow(
            "SELECT enabled FROM anilist_feeds WHERE guild_id = $1 AND channel_id = $2;",
            ctx.guild.id,
            target.id,
        )

        if row is None:
            count = await self.bot.db_pool.fetchval(
                "SELECT COUNT(*) FROM anilist_feeds WHERE guild_id = $1;",
                ctx.guild.id,
            )
            if count >= af.MAX_FEEDS_PER_GUILD:
                return await ctx.send(
                    _(
                        "This server already has the maximum of {max} feeds. "
                        "Remove one with `/anilistfeed remove` first."
                    ).format(max=af.MAX_FEEDS_PER_GUILD)
                )
            await self.bot.db_pool.execute(
                "INSERT INTO anilist_feeds (guild_id, channel_id) VALUES ($1, $2);",
                ctx.guild.id,
                target.id,
            )
            message = _(
                "AniList feed created in {channel}. Add users with "
                "`/anilistfeed follow`."
            ).format(channel=target.mention)
        elif not row["enabled"]:
            await self.bot.db_pool.execute(
                "UPDATE anilist_feeds SET enabled = TRUE, fail_count = 0 "
                "WHERE guild_id = $1 AND channel_id = $2;",
                ctx.guild.id,
                target.id,
            )
            message = _("AniList feed re-enabled in {channel}.").format(
                channel=target.mention
            )
        else:
            message = _("{channel} is already an AniList feed.").format(
                channel=target.mention
            )

        await ctx.send(view=_FeedNoticeView(_("AniList feed"), message))

    @anilistfeed.command(name="follow")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(username="The AniList username to follow.")
    async def anilistfeed_follow(self, ctx: commands.Context, *, username: str):
        """Follow an AniList user in this server's feed."""

        channel_id, error = await self._resolve_target(ctx)
        if error:
            return await ctx.send(error)

        async with ctx.typing():
            user_id, name, url, error = await self._resolve_anilist_user(username)
        if error:
            return await ctx.send(error)

        error = await self._add_follow(
            ctx.guild.id, channel_id, user_id, name, ctx.author.id
        )
        if error:
            return await ctx.send(error)

        user_value = f"[{name}]({url})" if url else name
        body = "**{user}:** {value}\n**{feed}:** <#{channel}>".format(
            user=_("User"), value=user_value, feed=_("Feed"), channel=channel_id
        )
        await ctx.send(view=_FeedNoticeView(_("Now following"), body))

    @anilistfeed.command(name="unfollow")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(username="The AniList username to stop following.")
    async def anilistfeed_unfollow(self, ctx: commands.Context, *, username: str):
        """Stop following an AniList user (by stored name, case-insensitive)."""

        channel_id, error = await self._resolve_target(ctx)
        if error:
            return await ctx.send(error)

        name = username.strip()
        row = await self.bot.db_pool.fetchrow(
            "DELETE FROM anilist_follows "
            "WHERE guild_id = $1 AND channel_id = $2 "
            "AND lower(anilist_username) = lower($3) "
            "RETURNING anilist_username;",
            ctx.guild.id,
            channel_id,
            name,
        )
        if row is None:
            return await ctx.send(
                _("This feed is not following **{name}**.").format(name=name)
            )
        await ctx.send(
            _("Unfollowed **{name}**.").format(name=row["anilist_username"])
        )

    @anilistfeed.command(name="me")
    @commands.guild_only()
    async def anilistfeed_me(self, ctx: commands.Context):
        """Join or leave this server's AniList feed with your linked account.

        Toggle: already followed -> leave; not yet followed -> join. Requires
        the feed's member self-add setting to be on, and your AniList account
        linked with ``/anilist login``.
        """

        channel_id, error = await self._resolve_target(ctx)
        if error:
            return await ctx.send(error)

        feed = await self.bot.db_pool.fetchrow(
            "SELECT self_add FROM anilist_feeds WHERE guild_id = $1 AND channel_id = $2;",
            ctx.guild.id,
            channel_id,
        )
        if feed is None or not feed["self_add"]:
            return await ctx.send(
                _(
                    "This feed does not let members join themselves. Ask a "
                    "moderator to turn on **Members can join** in "
                    "`/anilistfeed`."
                )
            )

        anilist = self.bot.get_cog("AniList")
        if anilist is None:
            return await ctx.send(_("AniList actions are unavailable right now."))

        status, token = await anilist._token_status(ctx.author.id)
        if status == "missing":
            return await ctx.send(
                _("Link your AniList account first with `/anilist login`.")
            )
        if status != "ok" or not token:
            return await ctx.send(
                _(
                    "Your AniList link is no longer valid - re-link it with "
                    "`/anilist login`."
                )
            )

        async with ctx.typing():
            data = await anilist._graphql(VIEWER_QUERY, {}, token=token)
        viewer = ((data or {}).get("data") or {}).get("Viewer")
        if not viewer or viewer.get("id") is None:
            return await ctx.send(_("Could not resolve your AniList account."))

        user_id = viewer["id"]
        name = viewer.get("name") or ctx.author.display_name

        if await self._follow_exists(ctx.guild.id, channel_id, user_id):
            await self._remove_follow(ctx.guild.id, channel_id, user_id)
            return await ctx.send(
                _("You have left the AniList feed in <#{channel}>.").format(
                    channel=channel_id
                )
            )

        count = await self._follow_count(ctx.guild.id, channel_id)
        if count >= af.MAX_FOLLOWS_PER_FEED:
            return await ctx.send(
                _("This feed is full right now - ask a moderator to make room.")
            )
        await self._insert_follow(ctx.guild.id, channel_id, user_id, name, ctx.author.id)
        await ctx.send(
            _("You have joined the AniList feed in <#{channel}>.").format(
                channel=channel_id
            )
        )

    @anilistfeed.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def anilistfeed_list(self, ctx: commands.Context):
        """List this server's feeds, their types and followed users."""

        await self._send_feed_list(ctx)

    async def _send_feed_list(self, ctx):
        feeds = await self._feeds_for_guild(ctx.guild.id)
        if not feeds:
            return await ctx.send(
                _(
                    "This server has no AniList feed yet. Create one with "
                    "`/anilistfeed set`."
                )
            )

        follow_rows = await self.bot.db_pool.fetch(
            "SELECT channel_id, anilist_username FROM anilist_follows "
            "WHERE guild_id = $1 ORDER BY anilist_username;",
            ctx.guild.id,
        )
        follows_by_channel = {}
        for row in follow_rows:
            follows_by_channel.setdefault(row["channel_id"], []).append(
                row["anilist_username"]
            )

        blocks = []
        for feed in feeds:
            cid = feed["channel_id"]
            channel = ctx.guild.get_channel_or_thread(cid)
            label = ("#" + channel.name) if channel is not None else str(cid)

            types = ", ".join(feed["types"] or ()) or _("none")
            status = _("enabled") if feed["enabled"] else _("disabled")
            names = follows_by_channel.get(cid) or []
            if names:
                following = ", ".join(names)
                if len(following) > 900:
                    following = following[:900].rstrip() + "..."
            else:
                following = _("no one yet")

            value = _(
                "Status: {status}\nTypes: {types}\nFollowing: {names}"
            ).format(status=status, types=types, names=following)
            blocks.append((label, value))

        await ctx.send(view=_FeedListView(_("AniList feeds"), blocks))

    @anilistfeed.command(name="remove", aliases=["delete"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(channel="The feed's channel (defaults to here).")
    async def anilistfeed_remove(
        self,
        ctx: commands.Context,
        channel: typing.Optional[
            typing.Union[discord.TextChannel, discord.Thread]
        ] = None,
    ):
        """Delete a feed and its follows (default: this channel)."""

        target = channel or ctx.channel
        deleted = await self._delete_feed_rows(ctx.guild.id, target.id)

        if not deleted:
            return await ctx.send(
                _("{channel} is not an AniList feed.").format(
                    channel=target.mention
                )
            )
        await ctx.send(
            _("Removed the AniList feed in {channel}.").format(
                channel=target.mention
            )
        )

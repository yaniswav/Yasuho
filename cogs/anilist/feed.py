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

Rendering is deliberately minimal here: ``_render_activity`` /
``_render_digest`` are the single method boundary a later lot swaps for a
Components V2 layout, so that change stays local.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
import typing

import aiohttp
import discord
from discord.ext import commands, tasks

from .helpers import API_URL
from tools import anilist_feed as af
from tools.http import TIMEOUT
from tools.i18n import _, ngettext

log = logging.getLogger(__name__)

# AniList brand blue, the default embed accent when a media has no cover colour.
ANILIST_BLUE = 0x02A9FF

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


class _RateLimited(Exception):
    """Raised on a 429 so the tick can set an embargo and bail cleanly."""

    def __init__(self, retry_after):
        super().__init__("AniList rate limited (retry after %ss)" % retry_after)
        self.retry_after = retry_after


class _FetchError(Exception):
    """Any non-429 network / HTTP / GraphQL failure while fetching."""


def _parse_retry_after(value, default=60):
    """Parse a Retry-After header (AniList sends integer seconds)."""

    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return default


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


def _colour_from_media(media):
    """Cover accent colour ("#aabbcc") as an int, else the AniList blue."""

    colour = (media.get("coverImage") or {}).get("color")
    if isinstance(colour, str) and colour.startswith("#"):
        try:
            return int(colour[1:], 16)
        except ValueError:
            pass
    return ANILIST_BLUE


def _media_title(media):
    """Best display title for a media dict (userPreferred first)."""

    title = media.get("title") or {}
    return (
        title.get("userPreferred")
        or title.get("romaji")
        or title.get("english")
        or _("Unknown title")
    )


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

    def cog_unload(self):
        self._poll_feeds.cancel()

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

        try:
            full, digest = af.plan_posts(items)
            for activity in full:
                await channel.send(
                    allowed_mentions=discord.AllowedMentions.none(),
                    **self._render_activity(activity),
                )
            if digest:
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
    # Rendering - the boundary Lot 3 replaces with Components V2.
    # ------------------------------------------------------------------
    def _render_activity(self, activity):
        """Render one activity into send kwargs (``dict(embed=...)``)."""

        if activity.get("kind") == "TextActivity":
            return {"embed": self._text_embed(activity)}
        return {"embed": self._list_embed(activity)}

    def _list_embed(self, activity):
        media = activity.get("media") or {}
        status = (activity.get("status") or "").strip()
        progress = af.normalize_progress(activity.get("progress"))
        action = " ".join(part for part in (status, progress) if part)

        embed = discord.Embed(
            title=_media_title(media),
            url=media.get("siteUrl") or activity.get("site_url"),
            description=action or None,
            colour=_colour_from_media(media),
        )
        embed.set_author(
            name=activity.get("user_name") or _("Someone"),
            url=activity.get("user_url"),
            icon_url=activity.get("user_avatar"),
        )
        cover = media.get("coverImage") or {}
        thumb = cover.get("extraLarge") or cover.get("large")
        if thumb:
            embed.set_thumbnail(url=thumb)
        self._stamp(embed, activity)
        return embed

    def _text_embed(self, activity):
        clean, image = af.convert_text(activity.get("text"))
        embed = discord.Embed(description=clean or None, colour=ANILIST_BLUE)
        embed.set_author(
            name=activity.get("user_name") or _("Someone"),
            url=activity.get("site_url") or activity.get("user_url"),
            icon_url=activity.get("user_avatar"),
        )
        if image:
            embed.set_image(url=image)
        self._stamp(embed, activity)
        return embed

    def _render_digest(self, items):
        """Render the coalesced remainder into a single compact digest embed."""

        lines = []
        for acts in af.group_by_user(items).values():
            name = acts[0].get("user_name") or _("Someone")
            count = len(acts)
            lines.append(
                ngettext(
                    "**{name}** posted {count} more update",
                    "**{name}** posted {count} more updates",
                    count,
                ).format(name=name, count=count)
            )
        embed = discord.Embed(
            title=_("More AniList activity"),
            description="\n".join(lines) or None,
            colour=ANILIST_BLUE,
        )
        return {"embed": embed}

    @staticmethod
    def _stamp(embed, activity):
        created = activity.get("created_at")
        if created:
            embed.timestamp = datetime.datetime.fromtimestamp(
                created, tz=datetime.timezone.utc
            )

    # ------------------------------------------------------------------
    # Management commands
    # ------------------------------------------------------------------
    async def _feeds_for_guild(self, guild_id):
        return await self.bot.db_pool.fetch(
            "SELECT channel_id, types, enabled, fail_count "
            "FROM anilist_feeds WHERE guild_id = $1 ORDER BY created_at;",
            guild_id,
        )

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
    @commands.has_permissions(manage_guild=True)
    async def anilistfeed(self, ctx: commands.Context):
        """Manage this server's AniList activity feeds."""

        if ctx.invoked_subcommand is None:
            await self._send_feed_list(ctx)

    @anilistfeed.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
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

        embed = discord.Embed(
            title=_("AniList feed"), description=message, colour=ANILIST_BLUE
        )
        await ctx.send(embed=embed)

    @anilistfeed.command(name="follow")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def anilistfeed_follow(self, ctx: commands.Context, *, username: str):
        """Follow an AniList user in this server's feed."""

        channel_id, error = await self._resolve_target(ctx)
        if error:
            return await ctx.send(error)

        username = username.strip()
        if not username:
            return await ctx.send(_("Give me an AniList username to follow."))

        async with ctx.typing():
            try:
                data = await self._graphql(USER_SEARCH_QUERY, {"name": username})
            except _RateLimited:
                return await ctx.send(
                    _(
                        "AniList is rate limiting me right now - try again in a "
                        "minute."
                    )
                )
            except _FetchError:
                return await ctx.send(
                    _("I could not reach AniList - try again shortly.")
                )

        user = ((data or {}).get("data") or {}).get("User")
        if not user or user.get("id") is None:
            return await ctx.send(
                _("I found no AniList user named **{name}**.").format(name=username)
            )

        user_id = user["id"]
        name = user.get("name") or username
        url = user.get("siteUrl")

        exists = await self.bot.db_pool.fetchval(
            "SELECT 1 FROM anilist_follows "
            "WHERE guild_id = $1 AND channel_id = $2 AND anilist_user_id = $3;",
            ctx.guild.id,
            channel_id,
            user_id,
        )
        if not exists:
            count = await self.bot.db_pool.fetchval(
                "SELECT COUNT(*) FROM anilist_follows "
                "WHERE guild_id = $1 AND channel_id = $2;",
                ctx.guild.id,
                channel_id,
            )
            if count >= af.MAX_FOLLOWS_PER_FEED:
                return await ctx.send(
                    _(
                        "This feed already follows the maximum of {max} users."
                    ).format(max=af.MAX_FOLLOWS_PER_FEED)
                )

        await self.bot.db_pool.execute(
            "INSERT INTO anilist_follows "
            "(guild_id, channel_id, anilist_user_id, anilist_username, added_by) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (guild_id, channel_id, anilist_user_id) "
            "DO UPDATE SET anilist_username = EXCLUDED.anilist_username;",
            ctx.guild.id,
            channel_id,
            user_id,
            name,
            ctx.author.id,
        )

        embed = discord.Embed(title=_("Now following"), colour=ANILIST_BLUE)
        embed.add_field(
            name=_("User"),
            value=f"[{name}]({url})" if url else name,
            inline=True,
        )
        embed.add_field(name=_("Feed"), value=f"<#{channel_id}>", inline=True)
        await ctx.send(embed=embed)

    @anilistfeed.command(name="unfollow")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
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

        embed = discord.Embed(title=_("AniList feeds"), colour=ANILIST_BLUE)
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
            embed.add_field(name=label, value=value, inline=False)

        await ctx.send(embed=embed)

    @anilistfeed.command(name="remove", aliases=["delete"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def anilistfeed_remove(
        self,
        ctx: commands.Context,
        channel: typing.Optional[
            typing.Union[discord.TextChannel, discord.Thread]
        ] = None,
    ):
        """Delete a feed and its follows (default: this channel)."""

        target = channel or ctx.channel
        async with self.bot.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM anilist_follows "
                    "WHERE guild_id = $1 AND channel_id = $2;",
                    ctx.guild.id,
                    target.id,
                )
                result = await conn.execute(
                    "DELETE FROM anilist_feeds "
                    "WHERE guild_id = $1 AND channel_id = $2;",
                    ctx.guild.id,
                    target.id,
                )

        if result.split()[-1] == "0":
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

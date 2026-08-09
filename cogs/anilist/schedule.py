"""Forward-looking airing browse: the ``/anilist schedule`` command.

Where :mod:`cogs.anilist.airing` REACTS to episodes that already aired (a poller
that DMs you after the fact), this surface looks FORWARD: "what airs next, and
when". It answers for one of two title sets:

* ``scope: me``      - the invoker's own AniList **Watching** anime list, read
  UNAUTHENTICATED through the same public ``MediaListCollection`` the airing
  poller uses (:data:`cogs.anilist.airing.AIRING_LIST_QUERY`), so no token ever
  has to be spent to browse;
* ``scope: channel`` - the titles this channel explicitly tracks
  (``anilist_channel_subs``, ``media_type = 'ANIME'``), which come straight from
  Postgres and cost no AniList request at all.

The result is an author-gated Components V2 panel
(:class:`AiringScheduleView`): the window's airings grouped by day, each line a
title link, an episode number and a ``<t:...:R>`` relative timestamp that every
viewer reads in their OWN timezone.

Poller separation. NOTHING here touches the poller's circuit: not its query
constant, not ``anilist_airing_state``, not ``LIST_FETCH_BUDGET``. This is
INTERACTIVE traffic and is paced as such - every AniList call goes through
:meth:`cogs.anilist.base.AniListBase._graphql` (so the process-wide
``allow_global`` ceiling applies) and every fetching button first passes
:func:`cogs.anilist.components._deny_if_throttled` (per-user + per-guild
``allow_interactive``). The command itself carries the house 1/5s per-user
cooldown.

Request budget (the whole point of the design below):

* ``scope: channel`` - **1** GraphQL call per invocation (the schedule window;
  the media ids are a single indexed Postgres read).
* ``scope: me``      - **2** GraphQL calls per invocation (public list +
  schedule window) once the invoker's AniList numeric id is known, and **3** on
  the very first browse of a user whose id was never resolved (one extra
  ``VIEWER_QUERY``). That id is free for anyone who ever opted into airing or
  chapter alerts (it is already stored), and is cached in-process for an hour
  afterwards, so the 3-call path is a once-per-user cold start, not a per-use
  cost. AniList's ``MediaListCollection`` requires a user id or name and GraphQL
  cannot feed one field's result into another in the same document, so the cold
  resolve genuinely cannot be folded into the list call.
* Day-window navigation (Earlier / Later) - **1** call, and only ever within a
  bounded 14-day horizon.
* Page navigation inside the loaded window - **0** calls: the window is fetched
  once and paged locally, which is also why the page buttons deliberately do NOT
  consume an interactive throttle slot (spending AniList quota on a request-free
  action would be a bug, not a safeguard).

Scale story (1000+ guilds). The command holds no background task, no timer and
writes nothing; its cost is strictly per invocation and already double-bounded
(per-user cooldown, then the per-user/per-guild/global interactive windows), so
a promo spike degrades to "slow down" ephemerals rather than to a burned per-IP
budget the pollers depend on. Every fan-in is bounded before it reaches the
wire: the media-id set is deduped, sorted and capped at
:data:`MAX_MEDIA_IDS` (AniList accepts 10k in ``mediaId_in``, but a request that
large is not a browse), the window is ONE API page of
:data:`API_PER_PAGE` rows (never a paginate-until-empty loop), the horizon is
capped at :data:`MAX_HORIZON_DAYS` days and the panel renders
:data:`PAGE_SIZE` entries at a time. The only process-global state is the
bounded, swept viewer-id cache below (ids only - never a token).
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from .account import AccountMixin
from .airing import AIRING_LIST_QUERY
from .components import _deny_if_throttled
from .helpers import _media_title
from .queries import VIEWER_QUERY
from tools import interactions
from tools.i18n import _, ngettext
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)


# AniList brand blue: the same accent the collection dashboard and the feed
# activity cards use, so the schedule panel reads as part of the same family.
SCHEDULE_ACCENT = 0x3DB4F2

DAY = 86400

# One browse window is a week, and the browse never looks further ahead than a
# fortnight: AniList's schedule data thins out past that, and an unbounded
# horizon would just be an unbounded number of fetches per curious user. Two
# windows (0-7 days, 7-14 days) is the whole navigable space.
WINDOW_DAYS = 7
MAX_HORIZON_DAYS = 14
MAX_WINDOW_OFFSET = MAX_HORIZON_DAYS - WINDOW_DAYS

# Rows rendered per panel page. The window is fetched ONCE and paged locally, so
# this is a readability bound (and a Components V2 text-length bound), not a
# request bound.
PAGE_SIZE = 10

# API rows per window fetch. AniList clamps ``perPage`` to 50 on
# ``Page.airingSchedules``; ONE page is fetched per window, never a loop - a
# fuller window is reported honestly instead of paginated.
API_PER_PAGE = 50

# Hard cap on the media ids sent in one ``mediaId_in``. A Watching list this
# large is already pathological for a browse; capping keeps the request (and the
# view's retained state) bounded no matter what the list looks like.
MAX_MEDIA_IDS = 500

# Titles are trimmed to this before being wrapped in a masked link, so one very
# long "Romaji (English)" pair cannot eat the panel's text budget.
TITLE_LIMIT = 80

# Viewer-id cache: Discord user id -> AniList numeric id. Mirrors the bounded,
# monotonic-clocked, swept-past-a-cap caches in :mod:`cogs.anilist.base` and
# :mod:`cogs.anilist.account`. Only the NUMERIC id is ever cached - never a
# token, never a title. A stale entry is harmless (AniList ids are permanent);
# the TTL exists only to bound the map, and the sweep bounds it hard.
_VIEWER_ID_TTL = 3600.0
_VIEWER_ID_SWEEP_AT = 500
_viewer_id_cache: dict = {}


def _viewer_id_get(user_id, now):
    """Return the cached AniList id for a Discord user, or None if stale/absent."""

    hit = _viewer_id_cache.get(user_id)
    if hit is None:
        return None
    ts, anilist_user_id = hit
    if now - ts >= _VIEWER_ID_TTL:
        return None
    return anilist_user_id


def _viewer_id_put(user_id, anilist_user_id, now):
    """Cache a resolved AniList id, sweeping stale rows once past the size cap."""

    _viewer_id_cache[user_id] = (now, anilist_user_id)
    if len(_viewer_id_cache) > _VIEWER_ID_SWEEP_AT:
        cutoff = now - _VIEWER_ID_TTL
        for key in [k for k, (ts, _v) in _viewer_id_cache.items() if ts < cutoff]:
            del _viewer_id_cache[key]


# --- GraphQL ----------------------------------------------------------------

# FORWARD-only airing schedules for a set of media, deliberately SEPARATE from
# the poller's ``AIRING_SCHEDULE_QUERY``: that one scans a short TRAILING window
# and its shape is load-bearing for the cursor, so the two must never be shared
# or "unified". This one is bounded ahead of ``now`` by the caller (see
# :func:`window_bounds`, which can never emit a bound in the past), asks for
# ``pageInfo.hasNextPage`` so a fuller window can be reported honestly instead of
# paginated, and selects only the fields a compact list line needs.
# ``sort: TIME`` (the AiringSort enum) returns oldest-first.
SCHEDULE_WINDOW_QUERY = """
query ($mediaIds: [Int], $greater: Int, $lesser: Int, $page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
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
        siteUrl
        episodes
        format
      }
    }
  }
}
"""


# --- Pure helpers (no I/O; the view and the mixin both lean on these) --------


def clamp_offset(offset_days):
    """Clamp a window offset into the navigable ``[0, MAX_WINDOW_OFFSET]`` range.

    Guarantees the browse can never walk into the past (offset < 0) nor past the
    fortnight horizon, whatever a caller or a double-click asks for.
    """

    try:
        offset = int(offset_days)
    except (TypeError, ValueError):
        return 0
    return max(0, min(offset, MAX_WINDOW_OFFSET))


def window_bounds(now, offset_days):
    """Return ``(greater, lesser)`` unix bounds of a forward-only browse window.

    ``greater`` is never below ``now``: the offset is clamped non-negative first,
    so this surface can structurally never ask AniList for past airings (that is
    the poller's job, with its own cursor).
    """

    start = int(now) + clamp_offset(offset_days) * DAY
    return start, start + WINDOW_DAYS * DAY


def bound_media_ids(ids):
    """Return ``(capped_ids, truncated)``: deduped, sorted, capped at the ceiling.

    Sorting makes the outgoing request deterministic (same list -> same query,
    which is what makes a fetch reproducible in a test), and the cap keeps one
    enormous list from turning a browse into a giant request.
    """

    unique = sorted({int(i) for i in ids or [] if i is not None})
    return unique[:MAX_MEDIA_IDS], len(unique) > MAX_MEDIA_IDS


def day_bucket(airing_at):
    """The UTC day index an airing falls in (grouping key)."""

    return int(airing_at) // DAY


# Hour (UTC) a day header points at. Grouping is by UTC day, but a ``<t:...:D>``
# label is rendered by Discord in the VIEWER's timezone, so labelling a bucket
# with its UTC midnight would show the PREVIOUS date to everyone west of UTC.
# A label at hour H renders the bucket's own date exactly for viewers in
# ``[-H, 24-H)``, and inhabited timezones span UTC-11..UTC+14 - 26 hours, wider
# than a day, so no single instant can satisfy all of them. H = 10 is the choice
# that covers every densely populated offset (UTC-10 Hawaii through UTC+13 NZ in
# summer); the handful of viewers at UTC-11 / UTC+14 may read a day HEADER as the
# neighbouring date. The per-episode ``<t:...:R>`` stamps are exact for everyone
# regardless, so nobody is ever misinformed about WHEN something airs.
DAY_LABEL_HOUR = 10


def day_label_ts(bucket):
    """A timestamp that renders as the bucket's DATE in every populated timezone."""

    return int(bucket) * DAY + DAY_LABEL_HOUR * 3600


def sort_rows(rows):
    """Airing rows in a stable ascending order, dropping ones with no timestamp.

    The API already returns ``sort: TIME`` order, but a panel that re-renders
    from retained state must not depend on that; the tiebreakers make the order
    total (and therefore the rendering deterministic).
    """

    clean = [r for r in rows or [] if (r or {}).get("airingAt") is not None]
    return sorted(
        clean,
        key=lambda r: (
            int(r.get("airingAt")),
            int(r.get("mediaId") or 0),
            int(r.get("episode") or 0),
        ),
    )


def group_by_day(rows):
    """Group already-sorted rows into ``[(day_bucket, [row, ...]), ...]``.

    Consecutive-run grouping (not a dict) so the day order follows the airing
    order rather than insertion luck.
    """

    groups = []
    for row in rows or []:
        bucket = day_bucket(row.get("airingAt"))
        if groups and groups[-1][0] == bucket:
            groups[-1][1].append(row)
        else:
            groups.append((bucket, [row]))
    return groups


def page_count(total, size=PAGE_SIZE):
    """Number of panel pages for ``total`` rows (always at least one)."""

    if total <= 0:
        return 1
    return (total + size - 1) // size


def clamp_page(page, total, size=PAGE_SIZE):
    """Clamp a page index into the pages that actually exist."""

    try:
        page = int(page)
    except (TypeError, ValueError):
        return 0
    return max(0, min(page, page_count(total, size) - 1))


def page_slice(rows, page, size=PAGE_SIZE):
    """The rows shown on ``page`` (clamped), never raising on a stale index."""

    rows = rows or []
    page = clamp_page(page, len(rows), size)
    start = page * size
    return rows[start : start + size]


def _schedule_link(media, limit=TITLE_LIMIT):
    """A masked ``[title](url)`` for a compact list line, or the bare title.

    Mirrors ``airing._title_markup`` (square brackets stripped so a title like
    ``Re:Zero [Director's Cut]`` cannot break the markup) and adds the length cap
    a one-line-per-episode list needs.
    """

    title = str(_media_title(media or {})).replace("[", "").replace("]", "")
    if len(title) > limit:
        title = title[: limit - 3].rstrip() + "..."
    url = (media or {}).get("siteUrl")
    if not url:
        return title
    return "[{title}]({url})".format(title=title, url=url)


# --- Panel ------------------------------------------------------------------


class _PagePrevButton(discord.ui.Button):
    """Previous page of the LOADED window - a local re-render, zero API calls."""

    def __init__(self, owner):
        self._owner = owner
        super().__init__(
            emoji="◀️",
            style=discord.ButtonStyle.secondary,
            disabled=owner.page <= 0,
        )

    async def callback(self, interaction):
        await self._owner._change_page(interaction, -1)


class _PageNextButton(discord.ui.Button):
    """Next page of the LOADED window - a local re-render, zero API calls."""

    def __init__(self, owner):
        self._owner = owner
        super().__init__(
            emoji="▶️",
            style=discord.ButtonStyle.secondary,
            disabled=owner.page >= page_count(len(owner.rows)) - 1,
        )

    async def callback(self, interaction):
        await self._owner._change_page(interaction, +1)


class _WindowPrevButton(discord.ui.Button):
    """Step the browse window back a week (never before now)."""

    def __init__(self, owner):
        self._owner = owner
        super().__init__(
            emoji="⏪",
            label=_("Earlier"),
            style=discord.ButtonStyle.secondary,
            disabled=owner.offset_days <= 0,
        )

    async def callback(self, interaction):
        await self._owner._change_window(interaction, -1)


class _WindowNextButton(discord.ui.Button):
    """Step the browse window forward a week (never past the horizon)."""

    def __init__(self, owner):
        self._owner = owner
        super().__init__(
            emoji="⏩",
            label=_("Later"),
            style=discord.ButtonStyle.secondary,
            disabled=owner.offset_days >= MAX_WINDOW_OFFSET,
        )

    async def callback(self, interaction):
        await self._owner._change_window(interaction, +1)


class AiringScheduleView(AuthorLayoutView):
    """Author-gated Components V2 panel: what airs next, grouped by day.

    Holds the window's rows AND the media-id set it was fetched with, so paging
    inside the window is a pure local re-render and only a window step costs an
    AniList request. Only the window buttons pass through
    :func:`~cogs.anilist.components._deny_if_throttled`: the page buttons issue
    no AniList call, so gating them would spend an interactive quota slot for
    nothing.

    Locale resolution, the author gate and the timeout greying-out all come from
    :class:`~tools.views.AuthorLayoutView`; every edit is ``view=``-only, as a
    Components V2 message requires.
    """

    def __init__(
        self,
        cog,
        author_id,
        scope,
        media_ids,
        rows,
        *,
        window,
        offset_days=0,
        has_more=False,
        truncated=False,
        timeout=180,
    ):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.scope = scope
        self.media_ids = list(media_ids or [])
        self.offset_days = clamp_offset(offset_days)
        self.window = window
        self.truncated = bool(truncated)
        self.page = 0
        self._set_rows(rows, has_more=has_more)
        self._build()

    # -- state ---------------------------------------------------------
    def _set_rows(self, rows, *, has_more=False):
        """Adopt a freshly fetched window, clamping the page index onto it."""

        self.rows = sort_rows(rows)
        self.has_more = bool(has_more)
        self.page = clamp_page(self.page, len(self.rows))

    def _scope_label(self):
        if self.scope == "channel":
            return _("Titles tracked in this channel")
        return _("Your Watching list")

    # -- layout --------------------------------------------------------
    def _header_text(self):
        start, end = self.window
        return (
            "## "
            + _("What airs next")
            + "\n-# "
            + _("{scope} - {start} to {end}").format(
                scope=self._scope_label(),
                start="<t:{ts}:d>".format(ts=int(start)),
                end="<t:{ts}:d>".format(ts=int(end)),
            )
        )

    def _body_text(self):
        """The current page as ONE text block: day headers plus airing lines.

        Deliberately a single :class:`~discord.ui.TextDisplay` rather than one
        component per day - a fortnight of anime can easily be seven day groups,
        and a container that grows a component per group would drift towards the
        Components V2 child limit for no gain.
        """

        rows = page_slice(self.rows, self.page)
        if not rows:
            return _("Nothing airing in this window.")

        blocks = []
        for bucket, group in group_by_day(rows):
            lines = ["### <t:{ts}:D>".format(ts=day_label_ts(bucket))]
            for row in group:
                media = row.get("media") or {}
                when = "<t:{ts}:R>".format(ts=int(row.get("airingAt")))
                episode = row.get("episode")
                link = _schedule_link(media)
                if episode is None:
                    lines.append(
                        _("**{title}** - {when}").format(title=link, when=when)
                    )
                else:
                    lines.append(
                        _("**{title}** - Episode {episode} - {when}").format(
                            title=link, episode=episode, when=when
                        )
                    )
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    def _footer_text(self):
        total = len(self.rows)
        parts = [
            _("Page {current}/{total}").format(
                current=self.page + 1, total=page_count(total)
            ),
            ngettext("{count} episode", "{count} episodes", total).format(count=total),
        ]
        lines = ["-# " + " - ".join(parts)]
        if self.has_more:
            lines.append(
                "-# "
                + _("There are more airings in this window than I can show at once.")
            )
        if self.truncated:
            lines.append(
                "-# "
                + _("That list is very large, so only its first titles are covered here.")
            )
        return "\n".join(lines)

    def _build(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=SCHEDULE_ACCENT)
        container.add_item(discord.ui.TextDisplay(self._header_text()))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._body_text()))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._footer_text()))
        if len(self.rows) > PAGE_SIZE:
            container.add_item(
                discord.ui.ActionRow(_PagePrevButton(self), _PageNextButton(self))
            )
        container.add_item(
            discord.ui.ActionRow(_WindowPrevButton(self), _WindowNextButton(self))
        )
        self.add_item(container)

    # -- callbacks -----------------------------------------------------
    async def _change_page(self, interaction, step):
        """Page inside the LOADED window: no fetch, no throttle slot spent."""

        try:
            self.page = clamp_page(self.page + step, len(self.rows))
            self._build()
            await interactions.refresh_layout(
                interaction, self.message, self, surface="anilist schedule"
            )
        except Exception:
            log.exception("AniList schedule paging failed")
            await interactions.notify_failure(interaction)

    async def _change_window(self, interaction, step):
        """Step a week and refetch - the only button that costs an API call."""

        try:
            # The edge check runs BEFORE the throttle: a step that lands on the
            # window already shown issues no AniList call, so charging it an
            # interactive quota slot would spend the user's budget on nothing.
            # Only reachable from a stale click - both buttons are edge-disabled.
            target = clamp_offset(self.offset_days + step * WINDOW_DAYS)
            if target == self.offset_days:
                return await interactions.reply(
                    interaction,
                    (
                        _("That is as far ahead as I can look.")
                        if step > 0
                        else _("That is as far back as I can look.")
                    ),
                )
            if await _deny_if_throttled(self.cog, interaction):
                return

            await interactions.defer(interaction, surface="anilist schedule")
            rows, has_more, window = await self.cog._fetch_airing_window(
                self.media_ids, offset_days=target
            )
            if rows is None:
                return await interactions.reply(
                    interaction,
                    _("Could not reach AniList right now - try again shortly."),
                )

            self.offset_days = target
            self.window = window
            self.page = 0
            self._set_rows(rows, has_more=has_more)
            self._build()
            await interactions.refresh_layout(
                interaction, self.message, self, surface="anilist schedule"
            )
        except Exception:
            log.exception("AniList schedule window navigation failed")
            await interactions.notify_failure(interaction)


# --- Cog mixin --------------------------------------------------------------


class ScheduleMixin:
    """The ``/anilist schedule`` leaf command, mixed into the composed cog.

    It lives on the same cog as the ``anilist`` hybrid group because discord.py
    rejects a subcommand whose parent group is owned by another cog (the same
    reason :class:`~cogs.anilist.airing.AiringMixin` is a base rather than part
    of the poller cog). ``/anilist airing`` stays the leaf TOGGLE it is; this
    adds a sibling leaf, not a group.
    """

    async def _resolve_anilist_user_id(self, user_id):
        """Return ``(anilist_user_id, error)`` for a Discord user; exactly one set.

        Three rungs, cheapest first, so the common browse costs ZERO GraphQL
        calls to resolve an id: the in-process cache, then the numeric id already
        stored by an airing / chapter opt-in (one indexed Postgres read), then -
        only for a linked user who never opted into either - one ``VIEWER_QUERY``
        whose result is cached. The token is used solely to authenticate that
        last call and is never logged or cached.
        """

        now = time.monotonic()
        cached = _viewer_id_get(user_id, now)
        if cached is not None:
            return cached, None

        row = await self.bot.db_pool.fetchrow(
            "SELECT anilist_user_id FROM anilist_airing_optins WHERE user_id = $1 "
            "UNION ALL "
            "SELECT anilist_user_id FROM anilist_chapter_optins WHERE user_id = $1 "
            "LIMIT 1;",
            user_id,
        )
        if row is not None and row["anilist_user_id"]:
            _viewer_id_put(user_id, row["anilist_user_id"], now)
            return row["anilist_user_id"], None

        status, token = await self._token_status(user_id)
        if status == "missing":
            return None, _(
                "Link your AniList account first with `/anilist login`, then run "
                "this again to see what airs next."
            )
        if status != "ok" or not token:
            return None, _(
                "Your AniList link is no longer valid - re-link it with "
                "`/anilist login`."
            )

        data = await self._graphql(VIEWER_QUERY, {}, token=token)
        viewer = ((data or {}).get("data") or {}).get("Viewer") or {}
        anilist_user_id = viewer.get("id")
        if not anilist_user_id:
            return None, _(
                "Could not resolve your AniList account - try again shortly."
            )
        _viewer_id_put(user_id, anilist_user_id, now)
        return anilist_user_id, None

    async def _fetch_watching_media_ids(self, anilist_user_id):
        """The user's PUBLIC Watching anime media ids, or None on a fetch failure.

        Token-free by design (a public profile's ``MediaListCollection`` needs no
        auth), reusing the poller's list query verbatim so the browse and the
        alerts can never disagree about what "your Watching list" means. A
        PRIVATE profile returns a null collection, which is an empty list (the
        caller says so honestly) - distinct from ``None``, which means the call
        itself did not come back.
        """

        data = await self._graphql(AIRING_LIST_QUERY, {"userId": anilist_user_id})
        if not data or not data.get("data"):
            return None
        collection = (data.get("data") or {}).get("MediaListCollection")
        if collection is None:
            return []
        ids = []
        for lst in collection.get("lists") or []:
            for entry in lst.get("entries") or []:
                mid = entry.get("mediaId")
                if mid is not None:
                    ids.append(mid)
        return ids

    async def _fetch_airing_window(self, media_ids, *, offset_days=0, now=None):
        """Return ``(rows, has_more, window)`` for one forward window.

        ONE GraphQL page, never a paginate-until-empty loop: a window with more
        than :data:`API_PER_PAGE` airings is reported honestly in the panel
        instead of costing extra requests. ``rows`` is ``None`` when the call did
        not come back (so the caller can say "could not reach AniList" rather
        than "nothing airing"); an empty list genuinely means an empty window.
        """

        window = window_bounds(time.time() if now is None else now, offset_days)
        if not media_ids:
            return [], False, window

        greater, lesser = window
        data = await self._graphql(
            SCHEDULE_WINDOW_QUERY,
            {
                "mediaIds": list(media_ids),
                "greater": greater,
                "lesser": lesser,
                "page": 1,
                "perPage": API_PER_PAGE,
            },
        )
        page = ((data or {}).get("data") or {}).get("Page")
        if page is None:
            return None, False, window
        rows = page.get("airingSchedules") or []
        has_more = bool((page.get("pageInfo") or {}).get("hasNextPage"))
        return rows, has_more, window

    async def _channel_media_ids(self, guild_id, channel_id):
        """The ANIME titles this channel explicitly tracks (zero AniList calls)."""

        rows = await self.bot.db_pool.fetch(
            "SELECT media_id FROM anilist_channel_subs "
            "WHERE guild_id = $1 AND channel_id = $2 AND media_type = 'ANIME';",
            guild_id,
            channel_id,
        )
        return [row["media_id"] for row in rows]

    async def _schedule_payload(self, user_id, scope, *, guild_id=None, channel_id=None):
        """Build the schedule panel. Returns ``(error, view)``; exactly one is set.

        ``error`` is an already-localised string covering every honest dead end
        (no linked account, an expired link, a channel tracking nothing, an empty
        or private Watching list, AniList unreachable). Split out from the command
        so a future hub button can reuse it, the way ``_collection_payload`` is
        shared.
        """

        if scope == "channel":
            if guild_id is None:
                return _(
                    "Run this in a server channel to see that channel's tracked "
                    "titles."
                ), None
            ids = await self._channel_media_ids(guild_id, channel_id)
            if not ids:
                return _(
                    "This channel does not track any anime titles yet - add some "
                    "from the `/anilistfeed` panel."
                ), None
        else:
            anilist_user_id, error = await self._resolve_anilist_user_id(user_id)
            if error:
                return error, None
            ids = await self._fetch_watching_media_ids(anilist_user_id)
            if ids is None:
                return _(
                    "Could not reach AniList right now - try again shortly."
                ), None
            if not ids:
                return _(
                    "I cannot see anything on your **Watching** anime list - add "
                    "some titles on AniList, and make sure your anime list is "
                    "public."
                ), None

        media_ids, truncated = bound_media_ids(ids)
        rows, has_more, window = await self._fetch_airing_window(media_ids)
        if rows is None:
            return _("Could not reach AniList right now - try again shortly."), None

        view = AiringScheduleView(
            self,
            user_id,
            scope,
            media_ids,
            rows,
            window=window,
            has_more=has_more,
            truncated=truncated,
        )
        return None, view

    @AccountMixin.anilist.command(name="schedule")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @app_commands.describe(
        scope="me for your Watching list, channel for this channel's tracked titles."
    )
    async def anilist_schedule(self, ctx, scope: str = "me"):
        """Browse what airs next, from your list or this channel's tracked titles."""

        scope = (scope or "me").strip().lower()
        if scope not in ("me", "channel"):
            # Answered BEFORE ctx.typing(), so nothing has been deferred yet and
            # an ephemeral reply is the whole response - no placeholder is left.
            return await ctx.send(
                _("Scope must be `me` or `channel`."),
                ephemeral=ctx.interaction is not None,
            )

        async with ctx.typing():
            error, view = await self._schedule_payload(
                ctx.author.id,
                scope,
                guild_id=ctx.guild.id if ctx.guild is not None else None,
                channel_id=ctx.channel.id,
            )
        # PUBLIC, like the sibling /anilist list: on a slash invocation
        # ctx.typing() IS the defer and it defers publicly, so an ephemeral
        # followup here would leave the visible "thinking" placeholder in the
        # channel forever while the real answer lands somewhere else (the
        # hazard cogs/community/votes.py documents). The panel this command
        # exists to send is public too, so the dead ends match it.
        if error:
            return await ctx.send(error)

        view.message = await ctx.send(view=view)

"""MangaDex chapter tracker: opt-in new-chapter DMs and in-channel alerts.

A user opts in with ``/anilist chapters``; from then on the poller DMs them a
compact Components V2 card whenever a new chapter of a title on their CURRENT
(Reading) AniList manga list drops on MangaDex, carrying a one-click **Read**
button that bumps their AniList progress to that chapter. Independently, a guild
admin may SUBSCRIBE a feed channel to specific manga titles from the feed panel
(``anilist_channel_subs``, media_type MANGA), and then the same alert is posted
once in that channel for each subscribed title. The two circuits are fully
independent: the DM path is driven by users' personal lists, the channel path by
the feed's explicit subscriptions, and they share no rows.

Two moving parts live here, both wired from the package ``__init__``:

* :class:`ChaptersMixin` - a base of the composed ``AniList`` cog that owns the
  ``/anilist chapters`` opt-in toggle (it has to be a subcommand of the shared
  ``anilist`` group, which discord.py only allows from the same cog, exactly as
  :class:`~cogs.anilist.airing.AiringMixin` documents).
* :class:`AniListChapters` - a standalone cog (added like ``AniListAiring``) that
  owns the poller, the per-user CURRENT manga-list cache, the AniList -> MangaDex
  mapping resolution, the per-manga feed poll, the DM / channel fan-out and the
  persistent Read button.

Token discipline. Poll-time reads are UNAUTHENTICATED: a public profile's
``MediaListCollection`` needs no token, and MangaDex is a public API. The only
token use is at opt-in (resolving the user's AniList id via ``VIEWER_QUERY``) and
on a Read click (writing the clicker's own progress). Nothing here logs or stores
a token.

All the non-trivial decisions - AniList -> MangaDex mapping, feed normalisation,
the dedup + cursor core - live as pure, unit-tested functions in
:mod:`tools.mangadex`; this cog is the thin I/O shell that feeds them the network
and the database and fans out what they return.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from .account import AccountMixin
from .airing import _title_markup
from .feed import (
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
from tools import i18n, interactions
from tools import mangadex as md
from tools import round_robin as rr
from tools.http import TIMEOUT, get_session
from tools.i18n import _

log = logging.getLogger(__name__)


# Poller cadence. Chapter releases are not latency-critical - a new-chapter DM a
# few minutes late is fine - and each mapped manga costs its own (un-batchable)
# MangaDex feed request, so 1800s (30 min) keeps the poll cheap and well clear of
# MangaDex's rate limit even with a large tracked set.
POLL_SECONDS = 1800

# Per-user CURRENT manga-list cache TTL. A Reading list changes rarely relative
# to a 30-min tick, so a cached list is reused for ~30 min before a lazy refresh.
LIST_TTL = 1800.0
LIST_SWEEP_AT = 500

# CONSTANT per-tick watch-list request budget, covering missing AND stale
# refreshes together (missing prioritised). Unlike airing, deferring a never-cached
# user here is SAFE - the chapter cursors are PER-MANGA, so a not-yet-loaded user
# only delays THEIR manga entering the round-robin feed wheel; it can never drag a
# shared cursor past anyone's chapters (there is no shared cursor). So this budget
# simply staggers a 1000-guild cold start's list fetches over ceil(missing/budget)
# ticks with no hold and no episode loss.
LIST_FETCH_BUDGET = 10

# CONSTANT per-tick MangaDex feed-request budget. There is NO batch chapter
# endpoint (see tools.mangadex), so each tracked+mapped manga costs its OWN feed
# request; polling every manga every tick is O(M) and 429-storms at scale. Instead
# a fair round-robin wheel polls at most this many manga per tick, so the request
# count is CONSTANT regardless of how many manga are tracked. The trade is a longer
# effective per-manga poll interval of ceil(mapped / FEED_BUDGET) ticks (logged at
# INFO when it exceeds one tick): alert latency degrades linearly and predictably
# instead of the request count exploding. Raising this shortens the interval
# linearly at the cost of more MangaDex requests per tick.
FEED_BUDGET = 25

# Safety cap on feed PAGES fetched per manga per tick. The MangaDex feed is
# newest-first with no ``readableAt_greater`` filter, so :meth:`_fetch_feed` pages
# BACKWARD (``md.FEED_LIMIT`` rows each) from the newest chapter down to the manga's
# stored cursor. This matters because the round-robin wheel widens a manga's
# effective poll interval to ceil(mapped / FEED_BUDGET) ticks: a fast updater (or a
# bulk import) can drop MORE than one page of chapters between two polls, and
# fetching only the newest page would let the per-manga cursor jump to the newest
# chapter and SILENTLY skip the older overflow. ``MAX_FEED_PAGES * md.FEED_LIMIT`` is
# the largest single-tick catch-up handled with zero loss; a rarer one-time burst
# beyond it (a licensor dump) hits the cap, which is LOGGED (never silent) and stays
# delivery-capped by MAX_ALERTS_PER_MANGA. Paging is adaptive - a normally-quiet
# manga stops after the first (short or already-at-cursor) page - so this never costs
# extra requests at the common scale where the interval is a single tick.
MAX_FEED_PAGES = 6

# Initial phase offset (seconds) applied before the chapters poller's FIRST tick so
# its AniList list-refresh burst never overlaps the airing poller's - both hit the
# same unauthenticated AniList endpoint, and the per-poller budgets bound each
# poller's rate but not the aggregate. 60s exceeds a tick's spaced AniList burst, and
# because the 1800s chapters cadence is a whole multiple of airing's 600s the offset
# is preserved every cycle, so the two B2 pollers stay decorrelated for good. The
# feed poller (dominant AniList user, out of B2 scope) is left to its own lot.
POLL_PHASE_OFFSET = 60

# At most this many AniList -> MangaDex title searches per tick. A search is the
# single most expensive call here (a /manga query whose whole candidate page is
# scanned), so new mappings resolve a few per tick; the rest ride later ticks.
MAX_MAPPING_SEARCHES_PER_TICK = 3

# Cap on alerts posted per manga per tick, newest kept. A bulk licensor import can
# dump 100+ chapters of a series onto MangaDex at once; without this cap that
# would spam 100 DMs. The seen memory still records every dropped chapter, so a
# capped chapter is suppressed for good, not re-queued.
MAX_ALERTS_PER_MANGA = 3

# Seen-memory pruning bounds (see the schema's mangadex_seen_chapters prune index):
# a chapter identity is forgotten once it is older than this many days OR falls
# outside the newest this-many chapters for its manga, whichever comes first.
SEEN_PRUNE_DAYS = 90
SEEN_PRUNE_KEEP = 200


# --- GraphQL ----------------------------------------------------------------

# A user's public CURRENT manga list, with each entry's media object (id, titles,
# cover, adult flag, url). Readable UNAUTHENTICATED for public profiles, so the
# poller never needs a user token. The media titles feed the mapping search and
# the alert card; the cover/url/adult flag feed the card.
CHAPTER_LIST_QUERY = """
query ($userId: Int) {
  MediaListCollection(userId: $userId, type: MANGA, status: CURRENT) {
    lists {
      entries {
        mediaId
        progress
        media {
          id
          title { romaji english userPreferred }
          coverImage { large medium color }
          isAdult
          siteUrl
        }
      }
    }
  }
}
"""

# The clicking viewer's own progress + the title, for the Read button. Authed:
# ``mediaListEntry`` resolves per-viewer only when the request carries the user's
# token (the same per-viewer resolution the airing Seen button relies on).
CHAPTER_LOOKUP_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    id
    title { userPreferred romaji english }
    chapters
    mediaListEntry { status progress }
  }
}
"""

# Read button custom_id template. The ``alf:read:`` prefix is disjoint from the
# feed's ``alf:like`` / ``alf:reply`` / ``alf:add`` and the airing ``alf:seen``
# templates so discord.py's fullmatch dispatch can never cross-route: no
# ``alf:read:...`` id can fullmatch any of them, and none of theirs can fullmatch
# this. ``mid`` is the AniList media id; ``chapter`` is the chapter NUMBER string
# (an integer like ``386`` or a decimal like ``110.5``), which is why this
# template - alone among the alf: family - carries an optional ``.<digits>`` tail.
READ_TEMPLATE = r"alf:read:(?P<mid>\d+):(?P<chapter>\d+(?:\.\d+)?)"

# A bare chapter NUMBER (integer or decimal). Used to decide whether a chapter can
# carry a Read button (a numberless oneshot cannot map to an integer progress) and
# to keep the custom_id's chapter part inside :data:`READ_TEMPLATE`.
_CHAPTER_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


# --- Pure helpers (unit-tested; no network, DB or Discord) -------------------


def _serialize_key(key):
    """Serialise a :func:`tools.mangadex.chapter_key` tuple to a TEXT column value.

    ``chapter_key`` returns ``("ch", "386")`` or ``("id", "<uuid>")``; the seen
    table stores that identity as a single string ``"<kind>:<value>"``. The kind
    is always ``ch``/``id`` (no colon), so the split on the FIRST colon in
    :func:`_deserialize_key` round-trips even a value that itself contains a colon
    (a non-numeric label like ``"Extra: Part 1"``). Pure and total.
    """

    return "{kind}:{value}".format(kind=key[0], value=key[1])


def _deserialize_key(text):
    """Inverse of :func:`_serialize_key`: a stored string back to a key tuple.

    Splits on the FIRST colon so a colon inside the value is preserved. Returns
    the ``(kind, value)`` tuple that :func:`tools.mangadex.chapter_key` would
    produce, so the loaded seen set compares identically to fresh keys.
    """

    kind, _sep, value = str(text).partition(":")
    return (kind, value)


def _chapter_number_str(number):
    """The chapter number as a clean, custom_id-safe string, or None if non-numeric.

    A numeric chapter (``"386"``, ``386``, ``"110.5"``) is returned as a string
    that :data:`READ_TEMPLATE` accepts; a numberless oneshot or a named label
    yields ``None`` so no Read button is offered (there is no integer progress to
    set). Pure and total.
    """

    if number is None:
        return None
    text = str(number).strip()
    return text if _CHAPTER_NUMBER_RE.fullmatch(text) else None


def _chapter_timestamp(readable):
    """Epoch seconds (int) for a MangaDex ``readableAt`` ISO string, else None.

    Accepts a ``...Z`` or explicit-offset ISO-8601 string; a naive value is read
    as UTC. Junk or a missing value yields ``None`` so the card simply omits the
    relative timestamp rather than raising. Pure and total.
    """

    if not readable:
        return None
    text = str(readable).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _search_title(media):
    """The title to search MangaDex with: romaji, falling back to english, else None.

    A media with neither romaji nor english cannot be searched this tick (it is
    simply left unmapped and retried once a title is known). Pure and total.
    """

    title = (media or {}).get("title") or {}
    return title.get("romaji") or title.get("english")


def _cap_alerts(alerts, cap=MAX_ALERTS_PER_MANGA):
    """Keep the newest ``cap`` alerts, returning ``(kept, dropped)``.

    ``alerts`` is oldest-first (the order :func:`tools.mangadex.plan_chapter_alerts`
    returns), so the newest ``cap`` are the LAST ``cap`` and the dropped ones are
    the oldest prefix. At or below the cap nothing is dropped. Pure and total.
    """

    if len(alerts) <= cap:
        return alerts, []
    return alerts[-cap:], alerts[:-cap]


def plan_chapter_targets(media_id, dm_lists_by_user, channel_media):
    """Who receives a chapter alert for the manga tracked under ``media_id``.

    ``dm_lists_by_user`` maps a Discord user id to the set of AniList media ids on
    their cached Reading list; ``channel_media`` maps a ``(guild_id, channel_id)``
    feed key to the set of AniList media ids that feed explicitly SUBSCRIBES to
    (``anilist_channel_subs``, media_type MANGA). Returns ``(dm_user_ids,
    channel_keys)`` - the DM opt-in users whose list contains ``media_id`` and the
    feed channels subscribed to it - each sorted for deterministic delivery. Pure
    and total.
    """

    dm_user_ids = sorted(
        uid for uid, mids in dm_lists_by_user.items() if media_id in mids
    )
    channel_keys = sorted(
        key for key, mids in channel_media.items() if media_id in mids
    )
    return dm_user_ids, channel_keys


def _sub_media(media_id, title):
    """Minimal AniList media dict for a channel-subscribed manga not on any list.

    A feed may subscribe to a manga that no DM opt-in user reads, so it never
    arrives via a cached Reading list. This synthesises just enough of a media
    object for the pipeline: the id, and the cached display title under ``romaji``
    so :func:`_search_title` can drive the MangaDex mapping search exactly as a
    list-derived manga does. The chapter card degrades on the fields this omits
    (cover, url, adult flag). Pure and total.
    """

    return {"id": media_id, "title": {"romaji": title}}


# --- Read button ------------------------------------------------------------


async def _run_read(interaction, media_id, chapter):
    """Advance the clicker's AniList progress to ``chapter`` for ``media_id``.

    Mirrors the airing Seen button: apply the invocation locale, gate on the
    shared per-user debounce, then resolve the clicker's token (this action
    WRITES, so a token is required). The chapter string is floored to an integer
    (a decimal ``110.5`` becomes progress ``110``); it looks up the viewer's
    current entry first and only advances when their progress is strictly below
    that number - progress is never regressed. The decrypted token stays a local;
    it is never logged or stored.
    """

    # Component callbacks run in their own task where the invocation locale was
    # never set: resolve it first so every _() below renders in the user's tongue.
    await i18n.apply_interaction_locale(interaction)
    if not await _check_debounce(interaction):
        return
    token = await _resolve_token(interaction)
    if token is None:
        return

    try:
        target = int(float(chapter))  # integer floor for decimals (110.5 -> 110)
    except (TypeError, ValueError):
        return await _feed_ephemeral(
            interaction, _("I couldn't read that chapter number.")
        )

    # Both round-trips can outlast the 3s window; defer, then follow up.
    await interactions.defer(
        interaction, ephemeral=True, thinking=True, surface="anilist chapter seen"
    )

    # 1) Look up the viewer's current progress + the title, as themselves.
    try:
        data = await _authed_graphql(
            interaction.client, token, CHAPTER_LOOKUP_QUERY, {"id": media_id}
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

    media = ((data or {}).get("data") or {}).get("Media") or {}
    if not media:
        return await _feed_ephemeral(
            interaction, _("I couldn't find that title on AniList anymore.")
        )
    title = _media_title(media)
    entry = media.get("mediaListEntry") or {}
    progress = entry.get("progress") or 0
    if progress >= target:
        return await _feed_ephemeral(
            interaction,
            _("You are already at chapter {progress} of **{title}**.").format(
                progress=progress, title=title
            ),
        )

    # 2) Advance progress to the released chapter as the clicking user. Passing
    #    only the progress leaves their status untouched (already Reading).
    try:
        saved = await _authed_graphql(
            interaction.client,
            token,
            SAVE_ENTRY_QUERY,
            {"mediaId": media_id, "progress": target},
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
        _("Marked **{title}** as read up to chapter {chapter}.").format(
            title=title, chapter=target
        ),
    )


class ChapterReadButton(
    discord.ui.DynamicItem[discord.ui.Button], template=READ_TEMPLATE
):
    """Persistent Read button that advances the clicker's progress to the chapter.

    A :class:`discord.ui.DynamicItem`, so the card is persistent (``timeout=None``)
    and the button keeps working forever - on DMs / channel posts sent before a
    restart included - because dispatch matches the custom_id against the
    globally-registered template and rebuilds the item from the live message,
    never from a stored view. The AniList media id and the chapter number are the
    only state and ride inside the custom_id (the number stays a string so a
    decimal chapter round-trips; :func:`_run_read` floors it at click time).
    """

    def __init__(self, media_id, chapter):
        self.media_id = media_id
        self.chapter = chapter
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label=_("Read"),
                emoji="\N{OPEN BOOK}",
                custom_id="alf:read:{mid}:{chapter}".format(
                    mid=media_id, chapter=chapter
                ),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["mid"]), match["chapter"])

    async def callback(self, interaction):
        await _run_read(interaction, self.media_id, self.chapter)


class ChapterCard(discord.ui.LayoutView):
    """One newly-released chapter as a compact Components V2 card.

    A cover-accented :class:`~discord.ui.Container` holds the release line (title
    link + "Chapter N is out." +, when present, the volume and a relative
    timestamp). The AniList cover art is a :class:`~discord.ui.Thumbnail` accessory
    beside the text (its ``description`` alt text is the media title, for screen
    readers), OMITTED when the media is adult (the text stays, only the
    image is dropped). A trailing :class:`~discord.ui.ActionRow` carries the read
    link button (labelled *MangaDex* for a normal chapter, *Official site* for an
    external link-only stub) and the persistent :class:`ChapterReadButton`. Every
    field degrades independently, so a partial dict never breaks the fan-out.

    ``media`` is the AniList media object (id, titles, cover, adult flag, url);
    ``chapter`` is a normalised MangaDex chapter dict (see
    :func:`tools.mangadex.parse_chapter_feed`), whose ``url`` is already the reader
    page or the external stub url.
    """

    def __init__(self, media, chapter, *, timeout=None):
        super().__init__(timeout=timeout)
        try:
            self._build(media or {}, chapter or {})
        except Exception:  # a card must never break the fan-out
            log.exception("AniList chapters: failed to build a card")
            self._fallback()

    def _fallback(self):
        self.clear_items()
        container = discord.ui.Container(accent_colour=_colour_from_media(None))
        container.add_item(discord.ui.TextDisplay(_("A new chapter is out.")))
        self.add_item(container)

    def _build(self, media, chapter):
        media_id = media.get("id")
        number = chapter.get("chapter")
        volume = chapter.get("volume")
        url = chapter.get("url")
        external = chapter.get("externalUrl")

        container = discord.ui.Container(accent_colour=_colour_from_media(media))
        line = _("Chapter {chapter} of **{title}** is out.").format(
            chapter=number, title=_title_markup(media)
        )
        texts = [
            discord.ui.TextDisplay("### " + _("New chapter")),
            discord.ui.TextDisplay(line),
        ]
        subparts = []
        if volume:
            subparts.append(_("Vol. {volume}").format(volume=volume))
        ts = _chapter_timestamp(chapter.get("readableAt"))
        if ts:
            subparts.append("<t:{ts}:R>".format(ts=ts))
        if subparts:
            texts.append(discord.ui.TextDisplay("-# " + " - ".join(subparts)))

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
            label = _("Official site") if external else _("MangaDex")
            action_row.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.link, label=label, url=url
                )
            )
        number_str = _chapter_number_str(number)
        if media_id is not None and number_str is not None:
            action_row.add_item(ChapterReadButton(media_id, number_str))
        if action_row.children:
            container.add_item(discord.ui.Separator())
            container.add_item(action_row)
        self.add_item(container)


# --- Opt-in command (a subcommand of the shared ``anilist`` group) -----------


class ChaptersMixin:
    """The ``/anilist chapters`` opt-in toggle, mixed into the composed AniList cog.

    It has to live on the same cog as the ``anilist`` hybrid group (discord.py
    rejects a subcommand whose parent group is in a different cog), so it is a base
    of ``AniList`` rather than part of the standalone :class:`AniListChapters`
    poller cog. It reuses the base cog's ``_token_status`` / ``_graphql`` and talks
    to the same ``anilist_chapter_optins`` table the poller reads.
    """

    @AccountMixin.anilist.command(name="chapters")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def anilist_chapters(self, ctx):
        """Toggle new-chapter DMs for titles on your Reading manga list."""

        ephemeral = ctx.interaction is not None

        row = await self.bot.db_pool.fetchrow(
            "SELECT enabled FROM anilist_chapter_optins WHERE user_id = $1;",
            ctx.author.id,
        )

        # Already opted in and on -> turn it off. Disabling always works.
        if row is not None and row["enabled"]:
            await self.bot.db_pool.execute(
                "UPDATE anilist_chapter_optins SET enabled = FALSE WHERE user_id = $1;",
                ctx.author.id,
            )
            return await ctx.send(
                _(
                    "Chapter alerts are now **off**. I will not DM you about new "
                    "chapters anymore - run this again to turn them back on."
                ),
                ephemeral=ephemeral,
            )

        # Enabling needs a linked account, to resolve and store their AniList id.
        status, token = await self._token_status(ctx.author.id)
        if status == "missing":
            return await ctx.send(
                _(
                    "Link your AniList account first with `/anilist login`, then "
                    "run this again to turn on chapter alerts."
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
            "INSERT INTO anilist_chapter_optins (user_id, anilist_user_id, enabled) "
            "VALUES ($1, $2, TRUE) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "anilist_user_id = EXCLUDED.anilist_user_id, enabled = TRUE;",
            ctx.author.id,
            anilist_user_id,
        )

        # Best-effort: peek at their PUBLIC Reading list (the poller reads it
        # unauthenticated) so we can warn if it is empty or private. A transient
        # read failure is treated as "unknown" - we neither warn nor falsely
        # reassure.
        note = ""
        chapters_cog = self.bot.get_cog("AniListChapters")
        if chapters_cog is not None:
            try:
                current = await chapters_cog._fetch_public_list(anilist_user_id)
            except Exception:
                current = None
            if current is not None and not current:
                note = "\n" + _(
                    "-# Heads up: I could not see any titles on your Reading list. "
                    "Make sure your AniList manga list is set to public."
                )

        await ctx.send(
            _(
                "Chapter alerts are now **on**. I will DM you when a new chapter of "
                "a title on your **Reading** manga list drops, with a one-click "
                "Read button.\n"
                "-# Your AniList manga list must be public for this to work."
            )
            + note,
            ephemeral=ephemeral,
        )


# --- Poller cog -------------------------------------------------------------


class AniListChapters(commands.Cog):
    """Opt-in chapter tracker: DM (and optionally post) when a tracked manga updates."""

    def __init__(self, bot):
        self.bot = bot
        # Unix timestamp before which the poller stays quiet (429 embargo).
        self._embargo_until = 0
        # Per-tick request pacing flag + counter (both reset each tick in _tick).
        self._spaced = False
        self._req_count = 0
        # anilist_user_id -> (monotonic_ts, {media_id: media_dict}). Bounded cache,
        # swept past a hard size cap - the house pattern (see cogs/anilist/account).
        self._list_cache: dict = {}
        # Fair round-robin wheel markers (tools.round_robin), in-memory only (a
        # restart restarts the wheel, which is harmless): two for the watch-list
        # refresh budget (missing + stale slices) and one for the per-manga feed
        # poll budget over the mapped-manga set.
        self._missing_wheel_after = None
        self._stale_wheel_after = None
        self._feed_wheel_after = None
        self._poll_chapters.start()

    async def cog_load(self):
        # Register the Read DynamicItem process-wide so its clicks dispatch on
        # EVERY chapter card, including ones sent before this start.
        try:
            self.bot.add_dynamic_items(ChapterReadButton)
        except Exception:
            log.exception("AniList chapters: failed to register the Read button")

    def cog_unload(self):
        self._poll_chapters.cancel()
        try:
            self.bot.remove_dynamic_items(ChapterReadButton)
        except Exception:
            log.exception("AniList chapters: failed to remove the Read button")

    # ------------------------------------------------------------------
    # AniList GraphQL plumbing (unauthenticated; one session per call)
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

        if isinstance(data, dict) and data.get("errors") and not data.get("data"):
            raise _FetchError("AniList GraphQL errors: " + str(data.get("errors"))[:200])
        return data

    # ------------------------------------------------------------------
    # MangaDex HTTP plumbing (public API; the module stamps the User-Agent)
    # ------------------------------------------------------------------
    async def _mangadex_get(self, url, params, headers):
        """GET a MangaDex endpoint (search or per-manga feed), returning parsed JSON.

        The request builders in :mod:`tools.mangadex` supply the url, the params
        (a LIST of pairs, which aiohttp takes directly) and the ToS-required
        User-Agent header. Raises :class:`_RateLimited` on a 429 (Retry-After
        seconds) and :class:`_FetchError` on any other failure, reusing the feed's
        typed errors so the tick handles a MangaDex 429 exactly like an AniList one.
        """

        try:
            async with get_session(self.bot).get(
                url, params=params, headers=headers, timeout=TIMEOUT
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
                    raise _FetchError(
                        "MangaDex HTTP %s with no JSON body" % r.status
                    )
        except _RateLimited:
            raise
        except _FetchError:
            raise
        except Exception as exc:
            raise _FetchError(str(exc)) from exc
        return data

    async def _search_manga(self, title):
        """Run the AniList -> MangaDex mapping title search for one title."""

        url, params, headers = md.search_manga_request(title)
        return await self._mangadex_get(url, params, headers)

    async def _fetch_feed_page(self, mangadex_id, offset):
        """GET one page of a manga's chapter feed at ``offset`` (newest-first)."""

        url, params, headers = md.manga_feed_request(mangadex_id, offset=offset)
        return await self._mangadex_get(url, params, headers)

    async def _fetch_feed(self, mangadex_id, cursor):
        """Fetch a manga's chapters back to ``cursor``, paging newest-first.

        MangaDex has no batch endpoint and no ``readableAt_greater`` filter, so the
        only source is the per-manga feed ordered ``readableAt`` DESC. One
        ``md.FEED_LIMIT`` page suffices when a manga is polled every tick, but under
        the round-robin feed wheel a manga's effective interval widens to
        ceil(mapped / FEED_BUDGET) ticks, so a fast updater (or a bulk import) can
        drop MORE than one page between polls. Fetching only the newest page there
        would let the per-manga cursor jump to the newest chapter and SILENTLY skip
        the older overflow. So this pages BACKWARD (increasing offset) until a page
        reaches already-processed ground (its oldest dated row is at or below the
        cursor) or the feed ends, bounded by :data:`MAX_FEED_PAGES`. On the first run
        (``cursor`` is None) a single page anchors the cursor with no backfill.
        Hitting the cap with a still-full page above the cursor is a pathological
        one-time burst (beyond ``MAX_FEED_PAGES * md.FEED_LIMIT`` chapters between two
        polls of the SAME manga); it is LOGGED rather than left silent, and delivery
        stays capped by MAX_ALERTS_PER_MANGA. Returns the accumulated normalised
        chapter dicts (the planner reorders internally). Requests are paced by
        :meth:`_space`; may raise :class:`_RateLimited` / :class:`_FetchError`.
        """

        cursor_ts = md._to_epoch(cursor)
        chapters = []
        capped = False
        for page in range(MAX_FEED_PAGES):
            await self._space()
            page_chapters = md.parse_chapter_feed(
                await self._fetch_feed_page(mangadex_id, page * md.FEED_LIMIT)
            )
            chapters.extend(page_chapters)
            if len(page_chapters) < md.FEED_LIMIT:
                break  # short page -> reached the end of the feed
            if cursor_ts is None:
                break  # first run: one page anchors the cursor (anti-backfill)
            page_ts = [
                ts
                for ts in (md._to_epoch(c.get("readableAt")) for c in page_chapters)
                if ts is not None
            ]
            if page_ts and min(page_ts) <= cursor_ts:
                break  # oldest row on this page is old ground: contiguous, no gap
        else:
            capped = True
        if capped:
            log.warning(
                "AniList chapters: feed page cap (%s x %s) reached for manga %s; a "
                "burst of more than %s chapters since the last poll may skip the "
                "oldest overflow (delivery stays capped at %s/tick)",
                MAX_FEED_PAGES,
                md.FEED_LIMIT,
                mangadex_id,
                MAX_FEED_PAGES * md.FEED_LIMIT,
                MAX_ALERTS_PER_MANGA,
            )
        return chapters

    async def _space(self):
        """Pace successive requests within a tick under the rate limits.

        Sleeps :data:`REQUEST_SPACING` before every request except the first of
        the tick, so the whole burst (list refreshes, mapping searches, per-manga
        feeds) cannot 429 itself. ``self._spaced`` is reset at the top of each tick.
        """

        if self._spaced:
            await asyncio.sleep(REQUEST_SPACING)
        self._spaced = True
        self._req_count += 1

    # ------------------------------------------------------------------
    # Per-user CURRENT manga-list cache (bounded, house pattern)
    # ------------------------------------------------------------------
    def _list_cache_get(self, anilist_user_id):
        """Return the cached ``{media_id: media_dict}`` for a user, or None.

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
        """Fetch a user's PUBLIC CURRENT manga list as ``{media_id: media_dict}``.

        Unauthenticated. A private profile returns a null ``MediaListCollection``,
        which maps to ``{}`` (cached, so we do not hammer it). Each value is the
        AniList media object (id, titles, cover, adult flag, url) the mapping search
        and the alert card need. May raise :class:`_RateLimited` / :class:`_FetchError`.
        """

        data = await self._graphql(CHAPTER_LIST_QUERY, {"userId": anilist_user_id})
        collection = ((data or {}).get("data") or {}).get("MediaListCollection")
        if collection is None:
            return {}
        out = {}
        for lst in collection.get("lists") or []:
            for entry in lst.get("entries") or []:
                media = entry.get("media") or {}
                mid = media.get("id") or entry.get("mediaId")
                if mid is None:
                    continue
                out[mid] = media
        return out

    async def _refresh_lists(self, anilist_user_ids, now):
        """Refresh reading-lists under a CONSTANT per-tick budget, missing first.

        Splits the tracked users into never-cached (missing) and cached-but-stale,
        then spends at most :data:`LIST_FETCH_BUDGET` requests this tick: MISSING
        lists first, then STALE ones with whatever budget remains, each slice drawn
        through a fair round-robin wheel (:func:`tools.round_robin.next_batch`) so no
        user can starve the rest and a 1000-guild cold start cannot burst the rate
        limit. Deferring a never-cached user is SAFE here - unlike airing there is NO
        shared cursor to drag past their chapters; the chapter cursors are per-manga,
        so a not-yet-loaded user merely delays THEIR manga entering the feed wheel,
        and once loaded that manga picks up from its own per-manga cursor (or a
        first-run anchor) with no loss. So this refresh never holds anything; it just
        staggers the fetches. Requests are paced by :meth:`_space`. A single user's
        non-429 failure is skipped (retried later); a 429 propagates so the tick can
        set an embargo across the whole poll.
        """

        missing = [aid for aid in anilist_user_ids if aid not in self._list_cache]
        stale = [
            aid
            for aid in anilist_user_ids
            if aid in self._list_cache and self._list_is_stale(aid, now)
        ]

        to_fetch = []
        if missing:
            batch, self._missing_wheel_after = rr.next_batch(
                missing, self._missing_wheel_after, LIST_FETCH_BUDGET
            )
            to_fetch.extend(batch)
        remaining = LIST_FETCH_BUDGET - len(to_fetch)
        if remaining > 0 and stale:
            batch, self._stale_wheel_after = rr.next_batch(
                stale, self._stale_wheel_after, remaining
            )
            to_fetch.extend(batch)

        for aid in to_fetch:
            await self._space()
            try:
                entries = await self._fetch_public_list(aid)
            except _FetchError as exc:
                log.warning(
                    "AniList chapters: list refresh failed for user %s (%s)", aid, exc
                )
                continue
            self._list_cache_put(aid, entries, now)

    # ------------------------------------------------------------------
    # Database access
    # ------------------------------------------------------------------
    async def _load_dm_optins(self):
        return await self.bot.db_pool.fetch(
            "SELECT user_id, anilist_user_id FROM anilist_chapter_optins "
            "WHERE enabled = TRUE;"
        )

    async def _load_channel_subs(self):
        """Explicit MANGA title subscriptions of every enabled feed (with cached title).

        The channel fan-out is driven ONLY by these rows now
        (``anilist_channel_subs``): a feed posts a subscribed manga's new chapters
        in its channel, independently of the DM opt-ins and of who the feed
        follows. The cached ``title`` seeds the MangaDex mapping search for a
        subscribed manga no opted-in user reads. A disabled feed is excluded (its
        channel must stay quiet).
        """

        return await self.bot.db_pool.fetch(
            "SELECT s.guild_id, s.channel_id, s.media_id, s.title "
            "FROM anilist_channel_subs s "
            "JOIN anilist_feeds fe "
            "  ON fe.guild_id = s.guild_id AND fe.channel_id = s.channel_id "
            "WHERE fe.enabled = TRUE AND s.media_type = 'MANGA';"
        )

    async def _load_mappings(self, media_ids):
        """Return ``{anilist_media_id: {mangadex_id, status}}`` for known media."""

        rows = await self.bot.db_pool.fetch(
            "SELECT anilist_media_id, mangadex_id, status FROM mangadex_mapping "
            "WHERE anilist_media_id = ANY($1::int[]);",
            list(media_ids),
        )
        return {
            row["anilist_media_id"]: {
                "mangadex_id": row["mangadex_id"],
                "status": row["status"],
            }
            for row in rows
        }

    async def _upsert_mapping(self, media_id, mangadex_id, status):
        await self.bot.db_pool.execute(
            "INSERT INTO mangadex_mapping "
            "(anilist_media_id, mangadex_id, status, checked_at) "
            "VALUES ($1, $2, $3, now()) "
            "ON CONFLICT (anilist_media_id) DO UPDATE SET "
            "mangadex_id = EXCLUDED.mangadex_id, status = EXCLUDED.status, "
            "checked_at = now();",
            media_id,
            mangadex_id,
            status,
        )

    async def _load_chapter_cursor(self, mangadex_id):
        row = await self.bot.db_pool.fetchrow(
            "SELECT last_readable_at FROM mangadex_chapter_state WHERE mangadex_id = $1;",
            mangadex_id,
        )
        return row["last_readable_at"] if row is not None else None

    async def _save_chapter_cursor(self, mangadex_id, last_readable_at):
        # Single-writer poller, so no GREATEST guard is needed: the planner already
        # returns a cursor that never regresses below the one it was given.
        await self.bot.db_pool.execute(
            "INSERT INTO mangadex_chapter_state "
            "(mangadex_id, last_readable_at, updated_at) "
            "VALUES ($1, $2, now()) "
            "ON CONFLICT (mangadex_id) DO UPDATE SET "
            "last_readable_at = EXCLUDED.last_readable_at, updated_at = now();",
            mangadex_id,
            last_readable_at,
        )

    async def _load_seen(self, mangadex_id):
        """The already-alerted chapter identities for a manga, as key tuples."""

        rows = await self.bot.db_pool.fetch(
            "SELECT chapter_key FROM mangadex_seen_chapters WHERE mangadex_id = $1;",
            mangadex_id,
        )
        return {_deserialize_key(row["chapter_key"]) for row in rows}

    async def _insert_seen(self, mangadex_id, keys):
        """Record freshly-alerted chapter identities (idempotent)."""

        if not keys:
            return
        serialized = [_serialize_key(key) for key in keys]
        await self.bot.db_pool.executemany(
            "INSERT INTO mangadex_seen_chapters (mangadex_id, chapter_key) "
            "VALUES ($1, $2) ON CONFLICT (mangadex_id, chapter_key) DO NOTHING;",
            [(mangadex_id, key) for key in serialized],
        )

    async def _prune_seen(self, mangadex_id):
        """Prune a manga's seen memory by age OR beyond the newest N chapters.

        Rides the ``mangadex_seen_chapters_prune_idx`` (mangadex_id, first_seen_at)
        so both the age cut and the newest-N window are index-served.
        """

        await self.bot.db_pool.execute(
            "DELETE FROM mangadex_seen_chapters "
            "WHERE mangadex_id = $1 AND ("
            "  first_seen_at < now() - ($2 || ' days')::interval "
            "  OR chapter_key NOT IN ("
            "    SELECT chapter_key FROM mangadex_seen_chapters "
            "    WHERE mangadex_id = $1 ORDER BY first_seen_at DESC LIMIT $3"
            "  )"
            ");",
            mangadex_id,
            str(SEEN_PRUNE_DAYS),
            SEEN_PRUNE_KEEP,
        )

    async def _disable_optin(self, user_id):
        try:
            await self.bot.db_pool.execute(
                "UPDATE anilist_chapter_optins SET enabled = FALSE WHERE user_id = $1;",
                user_id,
            )
        except Exception:
            log.exception("AniList chapters: could not disable opt-in for %s", user_id)

    # ------------------------------------------------------------------
    # Poller
    # ------------------------------------------------------------------
    @tasks.loop(seconds=POLL_SECONDS)
    async def _poll_chapters(self):
        # Fully wrapped: an unexpected error must never kill the loop.
        try:
            await self._tick()
        except Exception:
            log.exception("AniList chapters: poll tick failed")

    @_poll_chapters.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()
        # Phase-stagger off the airing poller so their AniList list-refresh bursts,
        # which share the one unauthenticated endpoint, never overlap (see
        # POLL_PHASE_OFFSET). Chapters are not latency-critical, so a one-time delay
        # before the first tick is free.
        await asyncio.sleep(POLL_PHASE_OFFSET)

    @_poll_chapters.error
    async def _poll_error(self, error):
        log.exception("AniList chapters: poll loop crashed; restarting", exc_info=error)
        self._poll_chapters.restart()

    async def _tick(self):
        now = int(time.time())
        if now < self._embargo_until:
            return  # still under a 429 backoff

        dm_optins = await self._load_dm_optins()
        channel_subs = await self._load_channel_subs()
        if not dm_optins and not channel_subs:
            return  # nobody tracked -> no API call

        # a. Tracked AniList users = every DM opt-in (the channel fan-out is driven
        # by explicit subscriptions, not by followed users' lists). Refresh their
        # public Reading lists under a constant per-tick budget (missing first, then
        # stale; no hold - per-manga cursors make deferring a user safe), then build
        # the tracked union.
        tracked_users = {row["anilist_user_id"] for row in dm_optins}

        self._spaced = False
        self._req_count = 0
        now_mono = time.monotonic()
        try:
            await self._refresh_lists(tracked_users, now_mono)
        except _RateLimited as exc:
            self._embargo_until = now + exc.retry_after
            log.warning(
                "AniList chapters: rate limited during list refresh, backing off %ss",
                exc.retry_after,
            )
            return

        # DM recipients keyed by Discord id, and a media-id -> media-dict map for
        # titles/covers. A user whose list fetch failed this tick simply contributes
        # nothing (never a crash). Populate media_by_id from the lists FIRST so a
        # manga that is BOTH on a list and channel-subscribed keeps its rich
        # list-derived media (cover/url/adult); the subscription only backfills the
        # ones no list carries.
        dm_lists_by_user = {}
        for row in dm_optins:
            cached = self._list_cache_get(row["anilist_user_id"])
            if cached is None:
                continue
            dm_lists_by_user[row["user_id"]] = set(cached.keys())

        media_by_id = {}
        for aid in tracked_users:
            cached = self._list_cache_get(aid)
            if cached is None:
                continue
            for mid, media in cached.items():
                media_by_id.setdefault(mid, media)

        # Channel fan-out is driven ONLY by explicit subscriptions. Each subscribed
        # manga joins the tracked-media union with a minimal media dict carrying its
        # cached title, so it enters the mapping-resolution pipeline (max 3
        # searches/tick) exactly like a list-derived manga - the C4 invariant that
        # keeps the shared cursors from advancing past a subscribed title's chapters.
        channel_media = {}
        for row in channel_subs:
            key = (row["guild_id"], row["channel_id"])
            channel_media.setdefault(key, set()).add(row["media_id"])
            media_by_id.setdefault(
                row["media_id"], _sub_media(row["media_id"], row["title"])
            )

        union_media = set(media_by_id)
        if not union_media:
            return  # nothing tracked yet -> no mapping / feed calls

        # b. Resolve up to a few new mappings this tick (searches are expensive).
        mapping_rows = await self._load_mappings(union_media)
        if not await self._resolve_new_mappings(
            union_media, media_by_id, mapping_rows, now
        ):
            return  # a 429 during a search set an embargo; abort the tick

        # Map each currently-tracked manga to the single AniList media that found
        # it (the mapping is effectively injective - one MangaDex manga carries one
        # links.al, so at most one AniList id resolves to it).
        mdx_to_media = {}
        for mid in union_media:
            row = mapping_rows.get(mid)
            if row and row["status"] == "found" and row["mangadex_id"]:
                mdx_to_media[row["mangadex_id"]] = mid
        if not mdx_to_media:
            return  # no mapped manga to poll yet

        # c/d/e/f. Poll a CONSTANT round-robin slice of the mapped manga this tick,
        # plan, persist, cap and fan out. There is no batch chapter endpoint, so each
        # manga costs its own feed request(s); the wheel bounds the per-tick request
        # budget to FEED_BUDGET manga no matter how many are tracked. Cursor safety
        # has TWO halves, both PER-MANGA (cursor + seen memory live per manga - see
        # _process_manga / mangadex_chapter_state): a manga NOT polled this tick keeps
        # its DB cursor untouched and catches up when the wheel next reaches it; a
        # manga that IS polled pages BACKWARD to its own stored cursor (_fetch_feed),
        # so even when the widened interval let more than one md.FEED_LIMIT page of
        # chapters accumulate, its cursor never jumps past un-fetched overflow. The
        # cost is a per-manga poll interval of ceil(mapped / FEED_BUDGET) ticks.
        mapped = sorted(mdx_to_media)
        interval = rr.poll_interval_ticks(len(mapped), FEED_BUDGET)
        if interval > 1:
            log.info(
                "AniList chapters: %s mapped manga tracked, each polled once every "
                "~%s ticks (~%.1fh) at %s feeds/tick",
                len(mapped),
                interval,
                interval * POLL_SECONDS / 3600.0,
                FEED_BUDGET,
            )
        batch, self._feed_wheel_after = rr.next_batch(
            mapped, self._feed_wheel_after, FEED_BUDGET
        )
        for mangadex_id in batch:
            media_id = mdx_to_media[mangadex_id]
            # Read the cursor once and thread it through: _fetch_feed pages back to
            # it, then _process_manga plans against the SAME value (no double read).
            cursor = await self._load_chapter_cursor(mangadex_id)
            try:
                chapters = await self._fetch_feed(mangadex_id, cursor)
            except _RateLimited as exc:
                self._embargo_until = now + exc.retry_after
                log.warning(
                    "AniList chapters: rate limited during a feed poll, backing "
                    "off %ss",
                    exc.retry_after,
                )
                return
            except _FetchError as exc:
                # One manga's transient failure holds only its cursor; the rest of
                # the tick continues (unlike a 429, which embargoes the whole poll).
                log.warning(
                    "AniList chapters: feed poll failed for %s (%s)", mangadex_id, exc
                )
                continue

            await self._process_manga(
                mangadex_id,
                media_id,
                cursor,
                chapters,
                media_by_id.get(media_id) or {},
                dm_lists_by_user,
                channel_media,
            )

        # Per-tick instrumentation, logged only on a tick that actually spent
        # requests (the quotas-heartbeat precedent: cheap, quiet when idle).
        if self._req_count:
            log.info(
                "AniList chapters: tick stats %s",
                {
                    "requests": self._req_count,
                    "tracked_users": len(tracked_users),
                    "union_media": len(union_media),
                    "mapped_manga": len(mapped),
                    "feeds_polled": len(batch),
                    "poll_interval_ticks": interval,
                    "wheel_after": self._feed_wheel_after,
                },
            )

    async def _resolve_new_mappings(self, union_media, media_by_id, mapping_rows, now):
        """Search MangaDex for a few unmapped media, recording found AND missing.

        Only media with NO mapping row at all are searched (a recorded ``missing``
        is not retried in this lot - ``checked_at`` is the future staleness clock);
        at most :data:`MAX_MAPPING_SEARCHES_PER_TICK` per tick. Every outcome is
        upserted and mirrored into ``mapping_rows`` so the caller sees it this tick.
        Returns ``False`` (and sets an embargo) when a search 429s, so the caller
        can abort the tick; ``True`` otherwise.
        """

        unmapped = [
            mid
            for mid in union_media
            if mid not in mapping_rows and _search_title(media_by_id.get(mid))
        ]
        for mid in sorted(unmapped)[:MAX_MAPPING_SEARCHES_PER_TICK]:
            title = _search_title(media_by_id.get(mid))
            await self._space()
            try:
                payload = await self._search_manga(title)
            except _RateLimited as exc:
                self._embargo_until = now + exc.retry_after
                log.warning(
                    "AniList chapters: rate limited during a mapping search, "
                    "backing off %ss",
                    exc.retry_after,
                )
                return False
            except _FetchError as exc:
                log.warning(
                    "AniList chapters: mapping search failed for media %s (%s)",
                    mid,
                    exc,
                )
                continue

            uuid = md.pick_mapping(payload, mid)
            if uuid:
                await self._upsert_mapping(mid, uuid, "found")
                mapping_rows[mid] = {"mangadex_id": uuid, "status": "found"}
            else:
                await self._upsert_mapping(mid, None, "missing")
                mapping_rows[mid] = {"mangadex_id": None, "status": "missing"}
        return True

    async def _process_manga(
        self,
        mangadex_id,
        media_id,
        cursor,
        chapters,
        media,
        dm_lists_by_user,
        channel_media,
    ):
        """Plan, persist and fan out one manga's feed for this tick.

        ``cursor`` is the manga's stored ``last_readable_at``, already read by
        :meth:`_tick` (which threaded it into :meth:`_fetch_feed` to page back to it),
        so it is passed in rather than re-read here. Loads the seen memory, runs the
        pure planner (which anchors silently on the first run), persists the advanced
        cursor and the fresh seen rows, prunes the seen memory, caps the alerts
        newest-first and fans each survivor out to the DM recipients and feed channels
        that track it. Delivery is fully guarded, so a closed DM or a dead channel
        never aborts the rest of the manga or the tick.
        """

        seen = await self._load_seen(mangadex_id)
        alerts, new_cursor, new_seen = md.plan_chapter_alerts(chapters, cursor, seen)

        # Persist the cursor + the freshly-handled identities BEFORE delivery, so a
        # delivery failure can never cause a re-alert (a capped/dropped chapter is
        # in new_seen too, so it is suppressed for good - never re-queued).
        if new_cursor != cursor and new_cursor is not None:
            await self._save_chapter_cursor(mangadex_id, new_cursor)
        added = new_seen - seen
        if added:
            await self._insert_seen(mangadex_id, added)
            await self._prune_seen(mangadex_id)

        if not alerts:
            return  # first run (anchor only) or nothing new

        kept, dropped = _cap_alerts(alerts)
        if dropped:
            log.warning(
                "AniList chapters: capped %s alert(s) for manga %s at %s/tick "
                "(newest kept)",
                len(dropped),
                mangadex_id,
                MAX_ALERTS_PER_MANGA,
            )

        dm_user_ids, channel_keys = plan_chapter_targets(
            media_id, dm_lists_by_user, channel_media
        )
        for chapter in kept:
            await self._deliver_alert(media, chapter, dm_user_ids, channel_keys)

    # ------------------------------------------------------------------
    # Fan-out
    # ------------------------------------------------------------------
    async def _deliver_alert(self, media, chapter, dm_user_ids, channel_keys):
        """DM every opted-in reader, then post once per opted-in feed channel."""

        for user_id in dm_user_ids:
            await self._deliver_dm(user_id, media, chapter)
        for guild_id, channel_id in channel_keys:
            await self._deliver_channel(channel_id, media, chapter)

    async def _deliver_dm(self, user_id, media, chapter):
        """DM one reader one chapter card, disabling them on a closed-DM Forbidden."""

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                log.warning("AniList chapters: could not resolve user %s", user_id)
                return

        # Render the card inside the recipient's own language: the LayoutView calls
        # _() at construction, so build and send both run under the locale context.
        loc = await i18n.resolve_locale(self.bot, user_id=user_id)
        try:
            with i18n.locale(loc):
                await user.send(view=ChapterCard(media, chapter))
        except discord.Forbidden:
            log.info(
                "AniList chapters: DMs closed for user %s; disabling their opt-in",
                user_id,
            )
            await self._disable_optin(user_id)
        except discord.HTTPException:
            log.warning("AniList chapters: failed to DM user %s", user_id)
        except Exception:
            log.exception("AniList chapters: unexpected error DMing user %s", user_id)

    async def _deliver_channel(self, channel_id, media, chapter):
        """Post one chapter card once in a feed channel (guarded; never raises)."""

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log.warning(
                "AniList chapters: feed channel %s is unresolvable", channel_id
            )
            return

        loc = await i18n.resolve_guild_locale(
            self.bot, getattr(channel, "guild", None)
        )
        try:
            with i18n.locale(loc):
                await channel.send(
                    view=ChapterCard(media, chapter),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.Forbidden, discord.NotFound):
            log.warning(
                "AniList chapters: delivery to channel %s failed (forbidden/gone)",
                channel_id,
            )
        except discord.HTTPException:
            log.warning(
                "AniList chapters: HTTP error posting to channel %s", channel_id
            )
        except Exception:
            log.exception(
                "AniList chapters: unexpected error posting to channel %s", channel_id
            )

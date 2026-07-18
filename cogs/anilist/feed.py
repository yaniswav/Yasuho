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

import discord
from discord.ext import commands, tasks

from .components import EditEntryModal
from .helpers import API_URL
from .queries import SAVE_ENTRY_QUERY, SEARCH_QUERY, VIEWER_QUERY
from tools import anilist_feed as af
from tools import i18n, interactions
from tools.cooldowns import Cooldowns
from tools.http import TIMEOUT, get_session
from tools.i18n import N_, _, ngettext
from tools.views import _DISABLEABLE, AuthorLayoutView, AuthorView, LocaleModal

log = logging.getLogger(__name__)

# AniList brand blue, the accent for the management-command embeds.
ANILIST_BLUE = 0x02A9FF

# Accent colours for the feed management panel's Components V2 container: green
# when the selected feed is enabled, red when disabled, and the neutral card
# blue (:data:`CARD_ACCENT`) when no feed exists yet.
PANEL_ENABLED = 0x2ECC71
PANEL_DISABLED = 0xE74C3C

# Accent for the activity/digest cards: the media's own cover colour when it has
# one, else this fixed AniList blue (used for every text activity and any list
# activity whose cover carries no colour).
CARD_ACCENT = 0x3DB4F2

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


class _RateLimited(Exception):
    """Raised on a 429 so the tick can set an embargo and bail cleanly."""

    def __init__(self, retry_after):
        super().__init__("AniList rate limited (retry after %ss)" % retry_after)
        self.retry_after = retry_after


class _FetchError(Exception):
    """Any non-429 network / HTTP / GraphQL failure while fetching."""


class _AuthError(Exception):
    """A 401 on an authenticated call: the user's AniList link is invalid now."""


class _GoneError(Exception):
    """A 400/404 (or data-less GraphQL error): the target activity is gone."""


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
    """Cover accent colour ("#aabbcc") as an int, else the card blue.

    Parses the media's ``coverImage.color`` with the pure, defensive
    :func:`tools.anilist_feed.parse_hex_colour`; a missing media, missing colour
    or malformed value all fall back to :data:`CARD_ACCENT`.
    """

    colour = (media or {}).get("coverImage") or {}
    return af.parse_hex_colour(colour.get("color")) or CARD_ACCENT


def _media_title(media):
    """Best display title for a media dict (userPreferred first)."""

    title = (media or {}).get("title") or {}
    return (
        title.get("userPreferred")
        or title.get("romaji")
        or title.get("english")
        or _("Unknown title")
    )


# AniList ListActivity ``status`` is a lowercase verb phrase ("watched episode",
# "plans to watch", ...). Each maps to a localisable action template; the
# progress-bearing ones interpolate ``{progress}`` (normalised elsewhere), the
# rest ignore it. ``N_`` marks the msgids for extraction at import time; ``_()``
# resolves them at render time (the documented store-then-translate trick). An
# UNKNOWN status is never in this map and degrades to its raw text (see
# :func:`_list_action`), so a status AniList adds later renders verbatim instead
# of crashing.
_LIST_ACTION_TEMPLATES = {
    "watched episode": N_("watched episode {progress} of"),
    "rewatched episode": N_("rewatched episode {progress} of"),
    "read chapter": N_("read chapter {progress} of"),
    "reread chapter": N_("reread chapter {progress} of"),
    "completed": N_("completed"),
    "plans to watch": N_("plans to watch"),
    "plans to read": N_("plans to read"),
    "paused watching": N_("paused watching"),
    "paused reading": N_("paused reading"),
    "dropped": N_("dropped"),
}


def _list_action(status, progress):
    """Localised action phrase for a ListActivity, e.g. 'watched episode 5 of'.

    A known status maps to a template (with ``{progress}`` filled in where the
    template uses it; templates without it simply ignore the argument). An
    unknown status degrades to its raw text plus any progress so a newly-added
    AniList status still renders. Internal whitespace is collapsed so a missing
    progress never leaves a double space ('watched episode  of').
    """

    key = (status or "").strip().lower()
    template = _LIST_ACTION_TEMPLATES.get(key)
    if template is not None:
        phrase = _(template).format(progress=progress)
    else:
        phrase = " ".join(part for part in (status or "", progress) if part)
    return " ".join(phrase.split())


def _bold_link(text, url):
    """A bold masked link ``**[text](url)**``, or bold text when no url.

    Square brackets are stripped from ``text`` so a title like
    ``Re:Zero [Director's Cut]`` cannot break the ``[...]`` markup.
    """

    label = str(text or "").replace("[", "").replace("]", "")
    if url:
        return "**[{label}]({url})**".format(label=label, url=url)
    return "**{label}**".format(label=label)


def _card_subline(activity, media):
    """Small ``-#`` metadata line, or ``None`` when there is nothing to show.

    Assembles, in order and only when present: the media format, a relative
    timestamp, and non-zero like / reply counts. ``media`` is ``None`` for text
    activities (no format shown).
    """

    parts = []
    fmt = (media or {}).get("format")
    if fmt:
        parts.append(str(fmt).replace("_", " "))
    created = activity.get("created_at")
    if created:
        parts.append("<t:{ts}:R>".format(ts=int(created)))
    likes = activity.get("like_count") or 0
    if likes:
        parts.append(ngettext("{n} like", "{n} likes", likes).format(n=likes))
    replies = activity.get("reply_count") or 0
    if replies:
        parts.append(ngettext("{n} reply", "{n} replies", replies).format(n=replies))
    if not parts:
        return None
    return "-# " + " - ".join(parts)


def _user_summary(acts):
    """Terse per-type counts for a digest line, e.g. '3 anime updates, 1 post'."""

    counts = {"ANIME_LIST": 0, "MANGA_LIST": 0, "TEXT": 0}
    for act in acts:
        kind = act.get("type")
        if kind in counts:
            counts[kind] += 1
    parts = []
    if counts["ANIME_LIST"]:
        n = counts["ANIME_LIST"]
        parts.append(ngettext("{n} anime update", "{n} anime updates", n).format(n=n))
    if counts["MANGA_LIST"]:
        n = counts["MANGA_LIST"]
        parts.append(ngettext("{n} manga update", "{n} manga updates", n).format(n=n))
    if counts["TEXT"]:
        n = counts["TEXT"]
        parts.append(ngettext("{n} post", "{n} posts", n).format(n=n))
    if not parts:  # only unexpected types seen: fall back to a plain total
        n = len(acts)
        parts.append(ngettext("{n} update", "{n} updates", n).format(n=n))
    return ", ".join(parts)


# --- Interactive Like / Reply -----------------------------------------------
#
# The feed card carries two persistent buttons that act AS the clicking user,
# through the AniList account they linked with ``/anilist login``. They are
# :class:`discord.ui.DynamicItem` buttons so they keep working forever - even on
# cards posted before a restart - because dispatch matches the custom_id against
# a globally-registered template and rebuilds the item from the live message,
# never from a stored (and long-gone) view. The activity id is the only state
# and it rides inside the custom_id.

# ``ToggleLikeV2`` returns a LikeableUnion; the two inline fragments read the
# result for the only two activity kinds our feed ever renders (a MessageActivity
# never appears here). ``LikeableType.ACTIVITY`` targets an activity by id.
TOGGLE_LIKE_MUTATION = """
mutation ($id: Int, $type: LikeableType) {
  ToggleLikeV2(id: $id, type: $type) {
    __typename
    ... on ListActivity { isLiked likeCount }
    ... on TextActivity { isLiked likeCount }
  }
}
"""

# ``SaveActivityReply`` posts a reply on the activity as the authenticated user.
SAVE_REPLY_MUTATION = """
mutation ($activityId: Int, $text: String) {
  SaveActivityReply(activityId: $activityId, text: $text) {
    id
  }
}
"""

# The clicking viewer's own status for a media, plus its title for the reply
# copy. ``mediaListEntry`` resolves against the AUTHENTICATED viewer only when
# the request carries that user's OAuth token - the same per-viewer resolution
# the media editor and update wizard already rely on (see ``MEDIA_ENTRY_QUERY``
# and ``SEARCH_ENTRY_QUERY`` in ``queries.py``, and ``AniListBase._viewer_entry``).
# ``userPreferred`` honours the viewer's title-language setting; romaji is the
# fallback. The follow-up add reuses ``SAVE_ENTRY_QUERY`` from ``queries.py``.
ADD_LOOKUP_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    id
    type
    title { userPreferred romaji english }
    episodes
    chapters
    mediaListEntry { status }
  }
}
"""

# custom_id templates. The three literal prefixes are disjoint so discord.py's
# fullmatch dispatch can never route a like click to the reply/add handler or
# vice versa; ``aid`` is the activity id and ``mid`` the media id (both positive
# ints, so each id part is short and the whole id stays well under the 100-char
# custom_id limit).
LIKE_TEMPLATE = r"alf:like:(?P<aid>\d+)"
REPLY_TEMPLATE = r"alf:reply:(?P<aid>\d+)"
ADD_TEMPLATE = r"alf:add:(?P<mid>\d+)"

# Human-readable words for the viewer's current list status on a media, mirroring
# the wording used by the media editor's status picker (see ``components.py``).
# ``N_`` marks the msgids for extraction; ``_()`` resolves them at click time. An
# unknown status degrades to its raw enum value.
_ADD_STATUS_WORDS = {
    "CURRENT": N_("Watching"),
    "PLANNING": N_("Planning"),
    "COMPLETED": N_("Completed"),
    "DROPPED": N_("Dropped"),
    "PAUSED": N_("Paused"),
    "REPEATING": N_("Repeating"),
}


def _status_word(status):
    """Localised word for an existing list status, or the raw enum if unknown."""

    template = _ADD_STATUS_WORDS.get((status or "").upper())
    return _(template) if template is not None else (status or "")

# The longest reply AniList's box accepts comfortably; keeps us inside Discord's
# modal input limit too.
REPLY_MAX_LENGTH = 1500

# One shared per-user debounce for both action buttons (not a durable rate
# limit, just an in-memory anti-hammer). 3s between clicks per user.
_ACTION_DEBOUNCE = Cooldowns(3.0)


def _activity_url(activity_id):
    """The canonical AniList permalink for an activity id.

    Deterministic from the id alone, so the reply confirmation can link back to
    the activity even on a card rebuilt after a restart (where the card object
    no longer carries the original ``siteUrl``).
    """

    return "https://anilist.co/activity/{aid}".format(aid=activity_id)


async def _authed_graphql(bot, token, query, variables):
    """POST an authenticated GraphQL request to AniList as the linked user.

    The bearer token is placed ONLY in the Authorization header - never logged,
    never echoed, never woven into a raised exception (the raised errors carry
    fixed, tokenless messages). Maps AniList's responses to the typed feed
    errors so the click handlers can render a clean, localised hint:

      * 429            -> :class:`_RateLimited` (with Retry-After seconds);
      * 401            -> :class:`_AuthError` (link revoked/invalid);
      * 400 / 404      -> :class:`_GoneError` (activity deleted);
      * a data-less GraphQL error -> :class:`_GoneError` (most often deleted);
      * anything else  -> :class:`_FetchError` (generic failure).
    """

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
    }
    payload = {"query": query, "variables": variables}

    try:
        async with get_session(bot).post(
            API_URL, json=payload, headers=headers, timeout=TIMEOUT
        ) as r:
            status = r.status
            if status == 429:
                raise _RateLimited(
                    _parse_retry_after(r.headers.get("Retry-After"))
                )
            if status == 401:
                raise _AuthError()
            try:
                data = await r.json()
            except Exception:
                data = None
            if status in (400, 404):
                raise _GoneError()
            if data is None:
                raise _FetchError("AniList HTTP %s with no JSON body" % status)
    except (_RateLimited, _AuthError, _GoneError, _FetchError):
        raise
    except Exception as exc:
        # aiohttp errors reference the URL/reason, never request headers, so the
        # token cannot leak here; still, keep the message generic and tokenless.
        raise _FetchError("network failure talking to AniList") from exc

    # A logical GraphQL error with no data payload is, in practice, an activity
    # that was deleted between the card being posted and the click.
    if isinstance(data, dict) and data.get("errors") and not data.get("data"):
        raise _GoneError()
    return data


async def _feed_ephemeral(interaction, message, *, view=None):
    """Deliver an ephemeral reply to a feed-action interaction, first or follow-up.

    Returns the sent message when the reply went out as a follow-up (so a caller
    can bind ``view.message`` for a clean timeout), else ``None``. ``view`` is
    only attached when provided - Discord rejects an explicit ``view=None``.
    """

    kwargs = {"ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(message, **kwargs)
        await interaction.response.send_message(message, **kwargs)
    except discord.HTTPException:
        log.debug("AniList feed: could not deliver an ephemeral action reply")
    return None


async def _check_debounce(interaction):
    """Gate a click behind the per-user debounce.

    Returns ``True`` when the click may proceed; otherwise sends an ephemeral
    'slow down' and returns ``False``. Touches the window only on an allowed
    click so a burst of denied clicks does not extend it indefinitely.
    """

    if _ACTION_DEBOUNCE.is_active(interaction.user.id):
        await _feed_ephemeral(
            interaction, _("You are clicking too fast - give it a moment.")
        )
        return False
    _ACTION_DEBOUNCE.touch(interaction.user.id)
    return True


async def _resolve_token(interaction):
    """Resolve the clicker's AniList token, or reply with the right hint.

    Returns the decrypted token string on success (a local value only, never
    logged), or ``None`` after having sent the appropriate ephemeral hint:
    not linked -> point at ``/anilist login``; expired or undecryptable ->
    ask them to re-link.
    """

    anilist = interaction.client.get_cog("AniList")
    if anilist is None:
        await _feed_ephemeral(
            interaction, _("AniList actions are unavailable right now.")
        )
        return None

    status, token = await anilist._token_status(interaction.user.id)
    if status == "missing":
        await _feed_ephemeral(
            interaction,
            _(
                "Link your AniList account first with `/anilist login`, then "
                "you can like and reply straight from the feed."
            ),
        )
        return None
    if status != "ok" or not token:
        await _feed_ephemeral(
            interaction,
            _(
                "Your AniList link is no longer valid - re-link it with "
                "`/anilist login`."
            ),
        )
        return None
    return token


async def _run_like(interaction, activity_id):
    """Toggle the clicking user's like on the activity, then confirm ephemerally."""

    # Component callbacks run in their own task, where the invocation locale was
    # never set: resolve it first so every _() below renders in the user's tongue.
    await i18n.apply_interaction_locale(interaction)
    if not await _check_debounce(interaction):
        return
    token = await _resolve_token(interaction)
    if token is None:
        return

    # The mutation is a network round-trip that can outlast the 3s window; defer
    # first, then follow up with the outcome.
    await interactions.defer(
        interaction, ephemeral=True, thinking=True, surface="anilist feed like"
    )

    try:
        data = await _authed_graphql(
            interaction.client,
            token,
            TOGGLE_LIKE_MUTATION,
            {"id": activity_id, "type": "ACTIVITY"},
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
            interaction, _("This activity no longer exists on AniList.")
        )
    except _FetchError:
        return await _feed_ephemeral(
            interaction, _("I could not reach AniList - try again shortly.")
        )

    result = ((data or {}).get("data") or {}).get("ToggleLikeV2") or {}
    liked = bool(result.get("isLiked"))
    count = result.get("likeCount") or 0
    if liked:
        message = ngettext(
            "Liked - this activity now has {n} like.",
            "Liked - this activity now has {n} likes.",
            count,
        ).format(n=count)
    else:
        message = ngettext(
            "Like removed - this activity now has {n} like.",
            "Like removed - this activity now has {n} likes.",
            count,
        ).format(n=count)
    await _feed_ephemeral(interaction, message)


class _ConfigureEntryView(discord.ui.View):
    """One-button ephemeral follow-up to configure a freshly added entry.

    Attached to the "Added ... to your planning." confirmation, it turns a
    two-step chore (add, then go find the entry to set progress/score) into one
    gesture. The button opens the shared :class:`EditEntryModal` for the media
    just added, pre-selected to PLANNING; the modal resolves the clicker's token
    lazily at submit (never logged or stored). The confirmation is ephemeral, so
    only the adding user can ever see or click this - no extra author gate is
    needed.
    """

    def __init__(self, cog, media, *, timeout=120):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.media = media
        self.message = None
        self.configure.label = _("Set progress / score")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Set progress / score", style=discord.ButtonStyle.primary, emoji="✏️"
    )
    async def configure(self, interaction, button):
        # (Label is localised in __init__; the decorator needs a placeholder.)
        # Component callbacks run in their own task, where the invocation locale
        # was never set: resolve it so the modal renders in the user's tongue.
        await i18n.apply_interaction_locale(interaction)
        try:
            score_format = await self.cog._get_score_format(interaction.user.id)
            await interaction.response.send_modal(
                EditEntryModal(
                    self.cog,
                    self.media,
                    entry={"status": "PLANNING"},
                    score_format=score_format,
                )
            )
        except discord.HTTPException:
            log.debug("AniList feed: could not open the configure-entry modal")


async def _run_add(interaction, media_id):
    """Add the media to the clicking user's planning list, or say it is already there.

    Mirrors :func:`_run_like` exactly: apply the invocation locale, gate on the
    shared per-user debounce, then resolve the clicker's token (same not-linked /
    re-link hints). It then acts AS the clicking user (Bearer): first an authed
    lookup of their existing entry (``mediaListEntry`` resolves per-viewer), and
    only when the media is not already on their list a ``SaveMediaListEntry`` to
    PLANNING. The token stays a local; it is never logged or stored.
    """

    await i18n.apply_interaction_locale(interaction)
    if not await _check_debounce(interaction):
        return
    token = await _resolve_token(interaction)
    if token is None:
        return

    # Both round-trips can outlast the 3s window; defer, then follow up.
    await interactions.defer(
        interaction, ephemeral=True, thinking=True, surface="anilist feed add"
    )

    # 1) Look up the viewer's existing entry (and the title) as themselves.
    try:
        data = await _authed_graphql(
            interaction.client, token, ADD_LOOKUP_QUERY, {"id": media_id}
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
    entry = media.get("mediaListEntry")
    if entry:
        return await _feed_ephemeral(
            interaction,
            _("**{title}** is already on your list ({status}).").format(
                title=title, status=_status_word(entry.get("status"))
            ),
        )

    # 2) Not tracked yet: add it to PLANNING as the clicking user.
    try:
        saved = await _authed_graphql(
            interaction.client,
            token,
            SAVE_ENTRY_QUERY,
            {"mediaId": media_id, "status": "PLANNING"},
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

    # Offer a one-gesture follow-up: a single button that opens the pre-filled
    # editor for the entry we just created, so setting progress/score never
    # means hunting the title down again. The AniList cog owns the token/GraphQL
    # helpers the modal needs; if it is somehow unavailable, the plain
    # confirmation still stands.
    cog = interaction.client.get_cog("AniList")
    view = _ConfigureEntryView(cog, media) if cog is not None else None
    sent = await _feed_ephemeral(
        interaction,
        _("Added **{title}** to your planning.").format(title=title),
        view=view,
    )
    if view is not None and sent is not None:
        view.message = sent


async def _run_reply(interaction, activity_id):
    """Open the reply modal for the clicking user (after locale + token checks)."""

    await i18n.apply_interaction_locale(interaction)
    if not await _check_debounce(interaction):
        return
    # Fail fast with a clear hint before the user types a whole reply; the modal
    # re-fetches the token at submit time, so we deliberately drop this one and
    # never park the decrypted secret on the modal object while they type.
    if await _resolve_token(interaction) is None:
        return

    try:
        await interaction.response.send_modal(_ReplyModal(activity_id))
    except discord.HTTPException:
        log.debug("AniList feed: could not open the reply modal")


class _ReplyModal(LocaleModal):
    """One paragraph field that posts an AniList reply as the submitting user."""

    def __init__(self, activity_id):
        super().__init__(title=_("Reply on AniList"))
        self.activity_id = activity_id
        self.reply_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=REPLY_MAX_LENGTH,
            required=True,
            placeholder=_("Write your reply..."),
        )
        self.add_item(
            discord.ui.Label(text=_("Your reply"), component=self.reply_input)
        )

    async def on_submit(self, interaction):
        # Defer first: posting the reply is a network round-trip.
        await interactions.defer(
            interaction, ephemeral=True, thinking=True, surface="anilist feed reply modal"
        )

        # Re-resolve the token now (it may have expired while typing), keeping the
        # decrypted secret's lifetime confined to this submit task.
        token = await _resolve_token(interaction)
        if token is None:
            return

        text = (self.reply_input.value or "").strip()
        if not text:
            return await _feed_ephemeral(
                interaction, _("Your reply was empty - nothing was posted.")
            )

        try:
            data = await _authed_graphql(
                interaction.client,
                token,
                SAVE_REPLY_MUTATION,
                {"activityId": self.activity_id, "text": text},
            )
        except _RateLimited:
            return await _feed_ephemeral(
                interaction,
                _("AniList is rate limiting me right now - try again shortly."),
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
                interaction, _("This activity no longer exists on AniList.")
            )
        except _FetchError:
            return await _feed_ephemeral(
                interaction, _("I could not reach AniList - try again shortly.")
            )

        reply = ((data or {}).get("data") or {}).get("SaveActivityReply") or {}
        if not reply.get("id"):
            return await _feed_ephemeral(
                interaction, _("AniList did not accept that reply - try again shortly.")
            )
        await _feed_ephemeral(
            interaction,
            _("Your reply was posted. [See it on AniList]({url})").format(
                url=_activity_url(self.activity_id)
            ),
        )


class FeedLikeButton(discord.ui.DynamicItem[discord.ui.Button], template=LIKE_TEMPLATE):
    """Persistent heart button that toggles the clicker's like on the activity."""

    def __init__(self, activity_id):
        self.activity_id = activity_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji="\N{HEAVY BLACK HEART}",
                custom_id="alf:like:{aid}".format(aid=activity_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["aid"]))

    async def callback(self, interaction):
        await _run_like(interaction, self.activity_id)


class FeedReplyButton(
    discord.ui.DynamicItem[discord.ui.Button], template=REPLY_TEMPLATE
):
    """Persistent speech-bubble button that opens the reply modal for the clicker."""

    def __init__(self, activity_id):
        self.activity_id = activity_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji="\N{SPEECH BALLOON}",
                custom_id="alf:reply:{aid}".format(aid=activity_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["aid"]))

    async def callback(self, interaction):
        await _run_reply(interaction, self.activity_id)


class FeedAddButton(discord.ui.DynamicItem[discord.ui.Button], template=ADD_TEMPLATE):
    """Persistent plus button that adds the media to the clicker's planning list.

    Keyed on the MEDIA id (not the activity id), so it only appears on list
    activities that carry one; a click adds that title to the clicking user's
    AniList planning list, or reports the status it is already tracked under.
    """

    def __init__(self, media_id):
        self.media_id = media_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji="\N{HEAVY PLUS SIGN}",
                custom_id="alf:add:{mid}".format(mid=media_id),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["mid"]))

    async def callback(self, interaction):
        await _run_add(interaction, self.media_id)


class ActivityCard(discord.ui.LayoutView):
    """One AniList activity as a polished Components V2 card.

    A coloured :class:`~discord.ui.Container` (the media's cover accent, else
    :data:`CARD_ACCENT`) holds the update. A list activity renders a
    :class:`~discord.ui.Section` - the cover as a :class:`~discord.ui.Thumbnail`
    accessory (its ``description`` alt text is the media title, for screen
    readers) beside a bold headline (username link + action + title link) and a
    small subline - while a text activity drops the thumbnail and renders the
    post body (plus any image via a :class:`~discord.ui.MediaGallery`) straight
    in the container. A trailing :class:`~discord.ui.ActionRow` carries the
    'AniList' link button plus the persistent Like and Reply buttons (see
    :class:`FeedLikeButton` / :class:`FeedReplyButton`), and - on a list activity
    with a media id - an Add-to-planning button (:class:`FeedAddButton`). Every
    field degrades independently, so a partial activity dict (missing media,
    avatar, progress, ...) never raises.

    Persistence: the Like / Reply buttons are :class:`discord.ui.DynamicItem`
    instances, so ``timeout=None`` and the card is persistent. discord.py stores
    a sent view only when it ``is_dispatchable()`` (see ``abc.Messageable.send``);
    this one now is, but it is fully dynamic (its only stateful items are the two
    DynamicItems), so no per-message entry is retained and no timeout task is
    created. The buttons keep working forever - on old cards, across restarts -
    because dispatch matches their custom_id against the globally-registered
    templates and rebuilds each item from the live message, never from this view.
    """

    def __init__(self, activity, *, timeout=None):
        super().__init__(timeout=timeout)
        try:
            self._build(activity)
        except Exception:  # a card must never break delivery of the whole batch
            log.exception("AniList feed: failed to build an activity card")
            self._fallback(_("An AniList update could not be rendered."))

    def _fallback(self, message):
        self.clear_items()
        container = discord.ui.Container(accent_colour=CARD_ACCENT)
        container.add_item(discord.ui.TextDisplay(message))
        self.add_item(container)

    def _build(self, activity):
        if activity.get("kind") == "TextActivity":
            container = discord.ui.Container(accent_colour=CARD_ACCENT)
            self._build_text(container, activity)
        else:
            container = discord.ui.Container(
                accent_colour=_colour_from_media(activity.get("media"))
            )
            self._build_list(container, activity)
        self._add_action_row(container, activity)
        self.add_item(container)

    def _build_list(self, container, activity):
        media = activity.get("media") or {}
        user_link = _bold_link(
            activity.get("user_name") or _("Someone"), activity.get("user_url")
        )
        title_link = _bold_link(
            _media_title(media), media.get("siteUrl") or activity.get("site_url")
        )
        action = _list_action(
            activity.get("status"), af.normalize_progress(activity.get("progress"))
        )
        # Collapse whitespace so a degenerate activity with no status at all
        # (empty action) does not leave a double space between name and title.
        headline = " ".join(
            _("{user} {action} {title}")
            .format(user=user_link, action=action, title=title_link)
            .split()
        )

        texts = [discord.ui.TextDisplay(headline)]
        subline = _card_subline(activity, media)
        if subline:
            texts.append(discord.ui.TextDisplay(subline))

        cover = media.get("coverImage") or {}
        thumb = cover.get("extraLarge") or cover.get("large")
        if thumb:
            # A Section requires an accessory; only build one when we have a
            # cover to hang on it, otherwise degrade to plain text displays.
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

    def _build_text(self, container, activity):
        user_link = _bold_link(
            activity.get("user_name") or _("Someone"),
            activity.get("user_url") or activity.get("site_url"),
        )
        container.add_item(
            discord.ui.TextDisplay(
                _("{user} posted an update").format(user=user_link)
            )
        )
        clean, image = af.convert_text(activity.get("text"))
        if clean:
            container.add_item(discord.ui.TextDisplay(clean))
        if image:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media=image)
            container.add_item(gallery)
        subline = _card_subline(activity, None)
        if subline:
            container.add_item(discord.ui.TextDisplay(subline))

    def _add_action_row(self, container, activity):
        # One ActionRow (max five buttons) with, in order: the 'AniList' link
        # button (only when we have a url), the persistent Like + Reply buttons
        # keyed on the activity id, and - only on a list activity that carries a
        # media id - the persistent Add-to-planning button keyed on the MEDIA id
        # (a text/digest card has no media, so it never gets one; this keeps the
        # busiest row at 4 of 5 slots). The activity id is present on every
        # rendered activity (``_normalize`` drops id-less ones), but stay
        # defensive so a degenerate dict cannot raise mid-build.
        activity_id = activity.get("id")
        url = activity.get("site_url")

        row = discord.ui.ActionRow()
        if url:
            row.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.link, label=_("AniList"), url=url
                )
            )
        if activity_id is not None:
            row.add_item(FeedLikeButton(activity_id))
            row.add_item(FeedReplyButton(activity_id))
        if activity.get("kind") == "ListActivity":
            media_id = (activity.get("media") or {}).get("id")
            if media_id is not None:
                row.add_item(FeedAddButton(media_id))

        if not row.children:  # nothing to show: no separator, no empty row
            return
        container.add_item(discord.ui.Separator())
        container.add_item(row)


class ActivityDigest(discord.ui.LayoutView):
    """The coalesced remainder of a busy tick as one compact Components V2 card.

    A single :data:`CARD_ACCENT` container: a heading ('...and N more updates')
    and one terse line per user (bold profile link + per-type counts), capped at
    :attr:`MAX_USERS` so a huge burst cannot blow the component budget - the
    overflow collapses into a small '+N others' trailer. No thumbnails: compact
    by design. Purely presentational (no interactive components), so it is never
    stored or dispatched.
    """

    MAX_USERS = 10

    def __init__(self, items, *, timeout=600):
        super().__init__(timeout=timeout)
        try:
            self._build(items)
        except Exception:
            log.exception("AniList feed: failed to build the digest card")
            self.clear_items()
            container = discord.ui.Container(accent_colour=CARD_ACCENT)
            container.add_item(
                discord.ui.TextDisplay(_("More AniList activity"))
            )
            self.add_item(container)

    def _build(self, items):
        total = len(items)
        users = list(af.group_by_user(items).values())

        container = discord.ui.Container(accent_colour=CARD_ACCENT)
        container.add_item(
            discord.ui.TextDisplay(
                "### "
                + ngettext(
                    "...and {count} more update",
                    "...and {count} more updates",
                    total,
                ).format(count=total)
            )
        )
        container.add_item(discord.ui.Separator())

        lines = []
        for acts in users[: self.MAX_USERS]:
            first = acts[0]
            user_link = _bold_link(
                first.get("user_name") or _("Someone"),
                first.get("user_url") or first.get("site_url"),
            )
            lines.append(
                _("{user} - {summary}").format(
                    user=user_link, summary=_user_summary(acts)
                )
            )
        extra = len(users) - self.MAX_USERS
        if extra > 0:
            lines.append(
                "-# "
                + ngettext("+{count} other", "+{count} others", extra).format(
                    count=extra
                )
            )
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        self.add_item(container)


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


# --- Management panel --------------------------------------------------------
#
# The interactive feed control panel opened by the bare ``/anilistfeed``
# command (an :class:`~tools.views.AuthorView`). It edits ONE feed at a time -
# ``selected_channel_id`` - defaulting to the guild's only feed, or its first
# feed when there are two (a select lets the admin switch). Every mutation
# writes straight to the DB through a cog helper, then the panel reloads its
# state fresh from the DB and re-renders in place, mirroring the WelcomePanel
# pattern in ``cogs/config/welcome.py``.

_TYPE_LABELS = {
    "ANIME_LIST": N_("Anime"),
    "MANGA_LIST": N_("Manga"),
    "TEXT": N_("Posts"),
}


class _FeedSwitchSelect(discord.ui.Select):
    """Pick which of the guild's (up to two) feeds the panel is editing."""

    def __init__(self, panel):
        self._owner = panel
        options = []
        for feed in panel.feeds:
            cid = feed["channel_id"]
            options.append(
                discord.SelectOption(
                    label=panel.feed_option_label(cid)[:100],
                    value=str(cid),
                    default=cid == panel.selected_channel_id,
                )
            )
        super().__init__(
            placeholder=_("Switch feed..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        try:
            await self._owner.reload_and_refresh(
                interaction, selected_channel_id=int(self.values[0])
            )
        except Exception:
            log.exception("AniList feed panel switch select failed")
            await interactions.notify_failure(interaction)


class _FeedChannelSelect(discord.ui.ChannelSelect):
    """No feed selected: create one here. A feed selected: move it here."""

    def __init__(self, panel):
        self._owner = panel
        defaults = []
        cid = panel.selected_channel_id
        if cid:
            # Only a text/news channel may be a default here: the select is
            # restricted to those types, and Discord rejects a default value
            # whose type is outside channel_types. A legacy thread-based feed
            # (get_channel returns None for a thread) simply gets no default.
            channel = panel.guild.get_channel(cid)
            if channel is not None and channel.type in (
                discord.ChannelType.text,
                discord.ChannelType.news,
            ):
                defaults = [channel]
        placeholder = (
            _("Move this feed to...")
            if cid is not None
            else _("Pick a channel to create a feed...")
        )
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            default_values=defaults,
        )

    async def callback(self, interaction):
        try:
            target = self.values[0]
            cog = self._owner.cog
            if self._owner.selected_channel_id is None:
                error = await cog._create_feed(self._owner.guild.id, target.id)
            else:
                error = await cog._move_feed(
                    self._owner.guild.id, self._owner.selected_channel_id, target.id
                )
            if error:
                return await interactions.reply(interaction, error)
            await self._owner.reload_and_refresh(
                interaction, selected_channel_id=target.id
            )
        except Exception:
            log.exception("AniList feed panel channel select failed")
            await interactions.notify_failure(interaction)


class _TypeToggleButton(discord.ui.Button):
    """One ANIME_LIST/MANGA_LIST/TEXT toggle; green on, grey off."""

    def __init__(self, panel, type_key):
        self._owner = panel
        self.type_key = type_key
        on = type_key in (panel.selected_feed["types"] or ())
        super().__init__(
            label=_(_TYPE_LABELS[type_key]),
            style=(
                discord.ButtonStyle.success if on else discord.ButtonStyle.secondary
            ),
        )

    async def callback(self, interaction):
        try:
            types = set(self._owner.selected_feed["types"] or ())
            on = self.type_key in types
            if on and len(types) <= 1:
                return await interactions.reply(
                    interaction,
                    _("At least one activity type must stay enabled."),
                )
            if on:
                types.discard(self.type_key)
            else:
                types.add(self.type_key)
            await self._owner.cog._set_types(
                self._owner.guild.id, self._owner.selected_channel_id, types
            )
            await self._owner.reload_and_refresh(interaction)
        except Exception:
            log.exception("AniList feed panel type toggle failed")
            await interactions.notify_failure(interaction)


class _SelfAddToggleButton(discord.ui.Button):
    """Flips whether members may join/leave the feed with ``/anilistfeed me``."""

    def __init__(self, panel):
        self._owner = panel
        on = bool(panel.selected_feed["self_add"])
        super().__init__(
            label=_("Members can join: {state}").format(
                state=_("On") if on else _("Off")
            ),
            style=(
                discord.ButtonStyle.success if on else discord.ButtonStyle.secondary
            ),
        )

    async def callback(self, interaction):
        try:
            await self._owner.cog._toggle_self_add(
                self._owner.guild.id, self._owner.selected_channel_id
            )
            await self._owner.reload_and_refresh(interaction)
        except Exception:
            log.exception("AniList feed panel self-add toggle failed")
            await interactions.notify_failure(interaction)


# --- Tracked-releases manager (per-feed explicit title subscriptions) --------
#
# The feed panel's "Tracked releases (N)" button opens this ephemeral manager for
# the selected feed. A feed's subscriptions are the EXPLICIT titles whose new
# episodes (ANIME) / chapters (MANGA) are posted in that channel - a circuit fully
# independent of the DM opt-ins and of who the feed follows. The manager lists the
# tracked titles, offers a modal-driven AniList search to add one (the top matches
# become a confirm select), and a paginated select to remove one; every mutation
# persists through a cog helper and re-renders the same ephemeral message. It is
# ephemeral, so only the admin who opened it can see or click it; both views are
# Components V2 LayoutViews built on the panel's house style, so - like the panel -
# they extend AuthorLayoutView (tools.views), which reapplies the invoker's locale
# and the author gate that AuthorView normally supplies (a LayoutView cannot
# subclass it).


class _TrackConfirmSelect(discord.ui.Select):
    """Pick which AniList search match to subscribe the feed to."""

    def __init__(self, view, candidates):
        self._view = view
        self._by_id = {}
        options = []
        for media in candidates[:25]:
            mid = media.get("id")
            mtype = media.get("type")
            if mid is None or mtype not in ("ANIME", "MANGA"):
                continue
            title = (media.get("title") or {})
            romaji = title.get("romaji") or title.get("english")
            kind = _("Anime") if mtype == "ANIME" else _("Manga")
            year = media.get("seasonYear") or "?"
            label = "[{kind}] {romaji}".format(
                kind=kind, romaji=romaji or _("Unknown title")
            )
            self._by_id[str(mid)] = media
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=str(year)[:100],
                    value=str(mid),
                )
            )
        super().__init__(
            placeholder=_("Pick the title to track..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        try:
            media = self._by_id.get(self.values[0])
            await self._view.confirm(interaction, media)
        except Exception:
            log.exception("AniList feed panel track-confirm select failed")
            await interactions.notify_failure(interaction)


class _SubsBackButton(discord.ui.Button):
    """Return from the confirm picker to the tracked-releases manager."""

    def __init__(self, view):
        self._view = view
        super().__init__(label=_("Back"), style=discord.ButtonStyle.secondary)

    async def callback(self, interaction):
        try:
            await self._view.back(interaction)
        except Exception:
            log.exception("AniList feed panel track-confirm back failed")
            await interactions.notify_failure(interaction)


class _SubsConfirmView(AuthorLayoutView):
    """Ephemeral confirm picker shown after a title search returns matches.

    A single ANILIST_BLUE :class:`~discord.ui.Container` in the panel's house
    style: a ``###`` heading and short prompt, a :class:`_TrackConfirmSelect`
    turning the searched-for candidates into one select, and a Back button.
    Picking a match inserts the subscription (cap-enforced) and re-renders the
    manager; Back simply re-renders the manager unchanged. Reuses the manager's
    cog/guild/channel so it can rebuild it in place on the SAME ephemeral message.
    """

    def __init__(self, cog, guild, author_id, channel_id, candidates, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.channel_id = channel_id
        self._build(candidates)

    def _build(self, candidates):
        container = discord.ui.Container(accent_colour=ANILIST_BLUE)
        container.add_item(
            discord.ui.TextDisplay(
                "### "
                + _("Track a title")
                + "\n"
                + _("Pick the exact title to track:")
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_TrackConfirmSelect(self, candidates)))
        container.add_item(discord.ui.ActionRow(_SubsBackButton(self)))
        container.add_item(
            discord.ui.TextDisplay("-# " + _("Only you can use these controls."))
        )
        self.add_item(container)

    async def confirm(self, interaction, media):
        if media is None:
            return await interactions.reply(
                interaction, _("I couldn't read that selection - try again.")
            )
        mid = media.get("id")
        mtype = media.get("type")
        title = (media.get("title") or {})
        cached = title.get("romaji") or title.get("english")
        error = await self.cog._add_channel_sub(
            self.guild.id, self.channel_id, mid, mtype, cached, interaction.user.id
        )
        note = (
            error
            if error
            else _("Now tracking **{title}** in this feed.").format(
                title=cached or str(mid)
            )
        )
        self.stop()
        await self.cog._render_subs_manager(
            interaction, self.guild, self.author_id, self.channel_id, note=note
        )

    async def back(self, interaction):
        self.stop()
        await self.cog._render_subs_manager(
            interaction, self.guild, self.author_id, self.channel_id
        )


class _TrackTitleModal(LocaleModal):
    """Ask for a title, search AniList (unauthenticated), then show the matches."""

    def __init__(self, manager):
        super().__init__(title=_("Track a title"))
        self.manager = manager
        self.query_field = discord.ui.TextInput(
            label=_("Title to search"),
            required=True,
            max_length=100,
        )
        self.add_item(self.query_field)

    async def on_submit(self, interaction):
        # Defer as a message update so we can edit the manager message in place
        # after the (possibly slow) AniList search.
        await interactions.defer(interaction, surface="anilist feed track-title modal")
        try:
            await self.manager.run_search(interaction, self.query_field.value)
        except Exception:
            log.exception("AniList feed panel track-title modal failed")
            await interactions.notify_failure(interaction)


class _TrackTitleButton(discord.ui.Button):
    """Open the search modal to add a title to this feed's tracked releases."""

    def __init__(self, manager):
        self._manager = manager
        super().__init__(label=_("Track a title"), style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        try:
            if self._manager.at_cap:
                return await interactions.reply(
                    interaction,
                    _(
                        "This feed already tracks the maximum of {max} titles. "
                        "Remove one first."
                    ).format(max=af.MAX_SUBS_PER_FEED),
                )
            await interaction.response.send_modal(_TrackTitleModal(self._manager))
        except Exception:
            log.exception("AniList feed panel track-title launch failed")
            await interactions.notify_failure(interaction)


class _RemoveSubSelect(discord.ui.Select):
    """Pick a currently-tracked title (by cached name) to stop tracking."""

    def __init__(self, manager, window):
        self._manager = manager
        options = []
        for row in window:
            mtype = row["media_type"]
            kind = _("Anime") if mtype == "ANIME" else _("Manga")
            title = row["title"] or str(row["media_id"])
            options.append(
                discord.SelectOption(
                    label=title[:100],
                    description=kind,
                    value=str(row["media_id"]),
                )
            )
        super().__init__(
            placeholder=_("Stop tracking a title..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        try:
            media_id = int(self.values[0])
            await self._manager.cog._remove_channel_sub(
                self._manager.guild.id, self._manager.channel_id, media_id
            )
            # Stop the old view first so its timeout can never fire and clobber the
            # re-rendered message with a stale, disabled layout.
            self._manager.stop()
            await self._manager.cog._render_subs_manager(
                interaction,
                self._manager.guild,
                self._manager.author_id,
                self._manager.channel_id,
                page=self._manager.page,
            )
        except Exception:
            log.exception("AniList feed panel remove-sub select failed")
            await interactions.notify_failure(interaction)


class _SubsPageButton(discord.ui.Button):
    """Page the remove select through a feed's subscriptions (25 per page)."""

    def __init__(self, manager, *, forward, disabled):
        self._manager = manager
        self._forward = forward
        super().__init__(
            label=_("Next") if forward else _("Previous"),
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )

    async def callback(self, interaction):
        try:
            page = self._manager.page + (1 if self._forward else -1)
            self._manager.stop()
            await self._manager.cog._render_subs_manager(
                interaction,
                self._manager.guild,
                self._manager.author_id,
                self._manager.channel_id,
                page=page,
            )
        except Exception:
            log.exception("AniList feed panel subs paging failed")
            await interactions.notify_failure(interaction)


class _SubsManagerView(AuthorLayoutView):
    """Ephemeral per-feed tracked-releases manager (list / add / remove).

    A single ANILIST_BLUE :class:`~discord.ui.Container` in the panel's house
    style: a ``###`` heading, an optional note paragraph, the "posted in
    {channel}" explanation, the subscribed titles as a bullet list with a ``-#``
    "Tracking N/max" subline, then the controls - a remove
    :class:`~discord.ui.ActionRow` select (paged at 25 per page), a Track-a-title
    button and, when there is more than one page, Previous/Next buttons. Every
    mutation persists through a cog helper and the cog re-renders this same
    ephemeral message from fresh DB state, so the list can never drift from what
    is stored.
    """

    PAGE_SIZE = 25

    def __init__(
        self, cog, guild, author_id, channel_id, subs, *, page=0, note=None,
        timeout=180,
    ):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.channel_id = channel_id
        self.subs = list(subs)
        self.note = note
        page_count = max(1, (len(self.subs) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(page, page_count - 1))
        self._page_count = page_count
        self._build()

    @property
    def at_cap(self):
        return len(self.subs) >= af.MAX_SUBS_PER_FEED

    def _build(self):
        container = discord.ui.Container(accent_colour=ANILIST_BLUE)

        channel = self.guild.get_channel_or_thread(self.channel_id)
        label = channel.mention if channel is not None else str(self.channel_id)
        header_parts = ["### " + _("Tracked releases")]
        if self.note:
            # The note already carries its own inline emphasis (e.g. a bold
            # title), so it is added as its own paragraph, not re-wrapped in bold.
            header_parts.append(self.note)
        header_parts.append(
            _(
                "New episodes (anime) and chapters (manga) of these titles are "
                "posted in {channel}, independently of any DM alerts. Up to {max} "
                "titles per feed."
            ).format(channel=label, max=af.MAX_SUBS_PER_FEED)
        )
        container.add_item(discord.ui.TextDisplay("\n\n".join(header_parts)))

        lines = []
        for row in self.subs:
            kind = _("Anime") if row["media_type"] == "ANIME" else _("Manga")
            title = row["title"] or str(row["media_id"])
            lines.append("- [{kind}] {title}".format(kind=kind, title=title))
        listing = "\n".join(lines) if lines else _("No titles tracked yet.")
        if len(listing) > 3500:
            listing = listing[:3500].rstrip() + "\n..."
        tracking = "-# " + _("Tracking {count}/{max}").format(
            count=len(self.subs), max=af.MAX_SUBS_PER_FEED
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(listing + "\n\n" + tracking))

        # Controls: the remove select (own ActionRow, only when the page has
        # rows), then a button row with Track-a-title plus, when paged, Prev/Next.
        start = self.page * self.PAGE_SIZE
        window = self.subs[start : start + self.PAGE_SIZE]
        container.add_item(discord.ui.Separator())
        if window:
            container.add_item(discord.ui.ActionRow(_RemoveSubSelect(self, window)))
        button_row = discord.ui.ActionRow()
        button_row.add_item(_TrackTitleButton(self))
        if self._page_count > 1:
            button_row.add_item(
                _SubsPageButton(self, forward=False, disabled=self.page == 0)
            )
            button_row.add_item(
                _SubsPageButton(
                    self, forward=True, disabled=self.page >= self._page_count - 1
                )
            )
        container.add_item(button_row)

        container.add_item(
            discord.ui.TextDisplay("-# " + _("Only you can use these controls."))
        )
        self.add_item(container)

    async def run_search(self, interaction, query):
        """Search AniList for ``query`` and edit the manager into the confirm picker.

        Runs UNAUTHENTICATED (no token at any point). No matches leaves the manager
        in place with a note; matches replace it with a :class:`_SubsConfirmView` on
        the SAME ephemeral message so the whole add flow stays on one message.
        """

        candidates = await self.cog._search_channel_candidates(query)
        if not candidates:
            self.stop()
            return await self.cog._render_subs_manager(
                interaction,
                self.guild,
                self.author_id,
                self.channel_id,
                page=self.page,
                note=_("No AniList match for **{query}**.").format(
                    query=(query or "").strip() or "?"
                ),
            )
        confirm = _SubsConfirmView(
            self.cog, self.guild, self.author_id, self.channel_id, candidates
        )
        confirm.message = self.message
        self.stop()
        try:
            # A Components V2 message carries its content inside the view, so edit
            # with ``view=`` only (Discord rejects an ``embed=`` on such an edit).
            await interaction.edit_original_response(view=confirm)
        except discord.HTTPException:
            log.warning(
                "AniList feed: could not render the track-confirm picker", exc_info=True
            )


class _TrackedReleasesButton(discord.ui.Button):
    """Open the ephemeral tracked-releases manager for the selected feed.

    Manage-guild gated exactly like the sibling controls: the panel is only opened
    from the admin-only bare-panel path and is author-restricted, so no per-button
    permission check is needed. The count in the label is a snapshot from the last
    panel render; the manager itself always shows live state.
    """

    def __init__(self, panel):
        self._owner = panel
        super().__init__(
            label=_("Tracked releases ({count})").format(count=panel.subs_count),
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction):
        try:
            panel = self._owner
            await panel.cog._render_subs_manager(
                interaction,
                panel.guild,
                panel.author_id,
                panel.selected_channel_id,
                new=True,
            )
        except Exception:
            log.exception("AniList feed panel tracked-releases open failed")
            await interactions.notify_failure(interaction)


class _EnableButton(discord.ui.Button):
    """Enable/disable the selected feed; re-enabling clears fail_count."""

    def __init__(self, panel):
        self._owner = panel
        enabled = bool(panel.selected_feed["enabled"])
        super().__init__(
            label=_("Disable") if enabled else _("Enable"),
            style=(
                discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success
            ),
        )

    async def callback(self, interaction):
        try:
            enabled = bool(self._owner.selected_feed["enabled"])
            await self._owner.cog._set_enabled(
                self._owner.guild.id, self._owner.selected_channel_id, not enabled
            )
            await self._owner.reload_and_refresh(interaction)
        except Exception:
            log.exception("AniList feed panel enable toggle failed")
            await interactions.notify_failure(interaction)


class _DeleteConfirmView(AuthorView):
    """Ephemeral Confirm/Cancel prompt for deleting the selected feed."""

    def __init__(self, panel, timeout=30):
        super().__init__(
            panel.author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.panel = panel
        self.confirm_button.label = _("Delete")
        self.cancel_button.label = _("Cancel")

    def build_embed(self):
        return discord.Embed(
            title=_("Delete this feed?"),
            description=_(
                "This permanently deletes the AniList feed in {channel} and "
                "everyone it follows. This cannot be undone."
            ).format(channel=self.panel.feed_label(self.panel.selected_channel_id)),
            colour=0xE74C3C,
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction, button):
        try:
            await self.panel.cog._delete_feed_rows(
                self.panel.guild.id, self.panel.selected_channel_id
            )
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=_("Feed deleted."), embed=None, view=self
            )
            await self.panel.sync_message()
        except Exception:
            log.exception("AniList feed panel delete confirm failed")
            await interactions.notify_failure(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction, button):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(
                content=_("Cancelled."), embed=None, view=self
            )
        except discord.HTTPException:
            pass


class _DeleteButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(
            label=_("Delete feed"), style=discord.ButtonStyle.danger
        )

    async def callback(self, interaction):
        try:
            view = _DeleteConfirmView(self._owner)
            await interaction.response.send_message(
                embed=view.build_embed(), view=view, ephemeral=True
            )
        except Exception:
            log.exception("AniList feed panel delete launch failed")
            await interactions.notify_failure(interaction)


class AddFollowModal(LocaleModal):
    """Ask for an AniList username, resolve it, then follow it on the feed."""

    def __init__(self, panel):
        super().__init__(title=_("Add a follow"))
        self.panel = panel
        self.username_field = discord.ui.TextInput(
            label=_("AniList username"),
            required=True,
            max_length=50,
        )
        self.add_item(self.username_field)

    async def on_submit(self, interaction):
        await interactions.defer(
            interaction, ephemeral=True, thinking=True, surface="anilist feed add-follow modal"
        )
        try:
            cog = self.panel.cog
            user_id, name, _url, error = await cog._resolve_anilist_user(
                self.username_field.value
            )
            if error:
                return await interactions.reply(interaction, error)
            error = await cog._add_follow(
                self.panel.guild.id,
                self.panel.selected_channel_id,
                user_id,
                name,
                interaction.user.id,
            )
            if error:
                return await interactions.reply(interaction, error)
            await self.panel.reload_and_refresh(interaction)
            await interactions.reply(
                interaction, _("Now following **{name}**.").format(name=name)
            )
        except Exception:
            log.exception("AniList feed panel add-follow modal failed")
            await interactions.notify_failure(interaction)


class _AddFollowButton(discord.ui.Button):
    def __init__(self, panel):
        self._owner = panel
        super().__init__(label=_("Add follow"), style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        try:
            if len(self._owner.follows) >= af.MAX_FOLLOWS_PER_FEED:
                return await interactions.reply(
                    interaction,
                    _("This feed already follows the maximum of {max} users.").format(
                        max=af.MAX_FOLLOWS_PER_FEED
                    ),
                )
            await interaction.response.send_modal(AddFollowModal(self._owner))
        except Exception:
            log.exception("AniList feed panel add-follow launch failed")
            await interactions.notify_failure(interaction)


class _RemoveFollowSelect(discord.ui.Select):
    """Pick a currently-followed user (by cached name) to unfollow."""

    def __init__(self, panel):
        self._owner = panel
        options = [
            discord.SelectOption(
                label=(row["anilist_username"] or str(row["anilist_user_id"]))[:100],
                value=str(row["anilist_user_id"]),
            )
            for row in panel.follows[:25]
        ]
        super().__init__(
            placeholder=_("Remove a follow..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        try:
            user_id = int(self.values[0])
            await self._owner.cog._remove_follow(
                self._owner.guild.id, self._owner.selected_channel_id, user_id
            )
            await self._owner.reload_and_refresh(interaction)
        except Exception:
            log.exception("AniList feed panel remove-follow select failed")
            await interactions.notify_failure(interaction)


async def _refresh_layout(interaction, message, view):
    """Edit a LayoutView panel in place with ``view=`` only (no embed/content).

    Mirrors :func:`tools.interactions.refresh_in_place` but never passes an embed:
    a Components V2 message carries its content inside the view and Discord
    rejects an ``embed=`` on such an edit. Tries the live interaction edit first,
    then falls back to editing the stored message when the interaction was
    already answered (e.g. a deferred modal submit).
    """

    await interactions.refresh_layout(
        interaction, message, view, surface="anilist feed panel"
    )


class AniListFeedPanel(discord.ui.LayoutView):
    """Author-restricted AniList feed control panel (the panel entry point).

    A single Components V2 :class:`~discord.ui.Container` whose accent tracks the
    selected feed's state - green enabled, red disabled, neutral card blue when
    no feed exists - giving it visual kinship with the activity cards it
    configures. Edits exactly one feed at a time (``selected_channel_id``); with
    two feeds a switch select sits under the header. With no feed at all only a
    creation ChannelSelect is shown. Every mutation persists through a cog helper
    and the panel reloads fresh state from the DB before re-rendering, so it can
    never drift from what is actually stored.

    LayoutView cannot subclass :class:`~tools.views.AuthorView` (that is a plain
    ``discord.ui.View``), so the author gate and locale resolution are
    reimplemented here in :meth:`interaction_check` exactly as AuthorView does
    them, and :meth:`on_timeout` disables every control and edits the message.
    """

    def __init__(
        self,
        cog,
        guild,
        author_id,
        feeds,
        selected_channel_id,
        follows,
        subs_count=0,
        timeout=180,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.author_id = author_id
        self.message = None
        self.feeds = list(feeds)
        self.selected_channel_id = selected_channel_id
        self.follows = list(follows)
        self.subs_count = subs_count
        self._build()

    async def interaction_check(self, interaction):
        # Component callbacks run in their own task where get_context never set
        # the locale; resolve it here so this check AND the callback localize.
        await i18n.apply_interaction_locale(interaction)
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                _("This panel isn't for you."), ephemeral=True
            )
            return False
        return True

    def _disable_all(self):
        """Disable every button/select in the layout (walks nested ActionRows)."""

        for child in self.walk_children():
            if isinstance(child, _DISABLEABLE):
                child.disabled = True

    async def on_timeout(self):
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @property
    def selected_feed(self):
        for feed in self.feeds:
            if feed["channel_id"] == self.selected_channel_id:
                return feed
        return None

    def feed_label(self, channel_id):
        """A clickable ``<#id>`` mention, for use in the panel's text."""

        channel = self.guild.get_channel_or_thread(channel_id)
        return channel.mention if channel is not None else str(channel_id)

    def feed_option_label(self, channel_id):
        """A plain-text label, for use in select option labels (no markdown)."""

        channel = self.guild.get_channel_or_thread(channel_id)
        return ("#" + channel.name) if channel is not None else str(channel_id)

    def _build(self):
        """(Re)assemble the layout from the current feed/follow state."""

        self.clear_items()
        feed = self.selected_feed

        if feed is None:
            accent = PANEL_DISABLED if self.feeds else CARD_ACCENT
        else:
            accent = PANEL_ENABLED if feed["enabled"] else PANEL_DISABLED
        container = discord.ui.Container(accent_colour=accent)

        # Zero-feed state: a friendly creation prompt plus the ChannelSelect.
        if not self.feeds:
            container.add_item(
                discord.ui.TextDisplay(
                    "### "
                    + _("AniList activity feed")
                    + "\n"
                    + _(
                        "This server has no AniList feed yet. Pick a channel "
                        "below to create one (up to {max} per server)."
                    ).format(max=af.MAX_FEEDS_PER_GUILD)
                )
            )
            container.add_item(discord.ui.ActionRow(_FeedChannelSelect(self)))
            container.add_item(
                discord.ui.TextDisplay(
                    "-# " + _("Only you can use these controls.")
                )
            )
            self.add_item(container)
            return

        # Header: title, a short reassurance, and the selected feed's channel +
        # status line (fail_count only when it is non-zero). Reading order is
        # header first (identity + state), then the scope selects right under it
        # (switch feed / move channel), then the Types and Follows configuration,
        # then the destructive actions - a clean top-down flow.
        status = _("Enabled") if feed["enabled"] else _("Disabled")
        if feed["fail_count"]:
            status = _("{status} ({count} recent failures)").format(
                status=status, count=feed["fail_count"]
            )
        header_lines = [
            "### " + _("AniList activity feed"),
            _(
                "Configure how AniList activity is mirrored into this server. "
                "Every change saves instantly."
            ),
            "**{channel}:** {mention}   **{status}:** {value}".format(
                channel=_("Channel"),
                mention=self.feed_label(feed["channel_id"]),
                status=_("Status"),
                value=status,
            ),
        ]
        if len(self.feeds) >= 2:
            header_lines.append(
                "-# "
                + _("Feeds")
                + ": "
                + ", ".join(self.feed_label(f["channel_id"]) for f in self.feeds)
            )
        container.add_item(discord.ui.TextDisplay("\n".join(header_lines)))

        if len(self.feeds) >= 2:
            container.add_item(discord.ui.ActionRow(_FeedSwitchSelect(self)))
        container.add_item(discord.ui.ActionRow(_FeedChannelSelect(self)))

        # Types: a label above the row of the three type toggles + the self-add
        # toggle (the buttons themselves carry the on/off state via their colour).
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("**" + _("Types") + "**"))
        type_row = discord.ui.ActionRow()
        for type_key in af.ALLOWED_TYPES:
            type_row.add_item(_TypeToggleButton(self, type_key))
        type_row.add_item(_SelfAddToggleButton(self))
        container.add_item(type_row)

        # Tracked releases: the feed's EXPLICIT title subscriptions (new episodes /
        # chapters posted in this channel). A circuit independent of the DM opt-ins
        # and of who the feed follows, so it gets its own button opening a dedicated
        # manager rather than sitting among the activity-type toggles.
        container.add_item(discord.ui.ActionRow(_TrackedReleasesButton(self)))

        # Follows: the followed-user list, then (when there are any) the remove
        # select, then the enable/delete/add-follow action row.
        container.add_item(discord.ui.Separator())
        if self.follows:
            names = ", ".join(
                row["anilist_username"] or str(row["anilist_user_id"])
                for row in self.follows
            )
            if len(names) > 900:
                names = names[:900].rstrip() + "..."
        else:
            names = _("no one yet")
        container.add_item(
            discord.ui.TextDisplay(
                "**"
                + _("Following ({count})").format(count=len(self.follows))
                + "**\n"
                + names
            )
        )
        if self.follows:
            container.add_item(discord.ui.ActionRow(_RemoveFollowSelect(self)))
        container.add_item(
            discord.ui.ActionRow(
                _EnableButton(self), _DeleteButton(self), _AddFollowButton(self)
            )
        )

        container.add_item(
            discord.ui.TextDisplay("-# " + _("Only you can use these controls."))
        )
        self.add_item(container)

    async def _reloaded(self, selected_channel_id):
        cog = self.cog
        feeds = await cog._feeds_for_guild(self.guild.id)
        if selected_channel_id is None:
            selected_channel_id = self.selected_channel_id
        channel_ids = {feed["channel_id"] for feed in feeds}
        if selected_channel_id not in channel_ids:
            selected_channel_id = feeds[0]["channel_id"] if feeds else None
        follows = (
            await cog._follows_for_feed(self.guild.id, selected_channel_id)
            if selected_channel_id is not None
            else []
        )
        subs_count = (
            await cog._channel_sub_count(self.guild.id, selected_channel_id)
            if selected_channel_id is not None
            else 0
        )
        new = AniListFeedPanel(
            cog,
            self.guild,
            self.author_id,
            feeds,
            selected_channel_id,
            follows,
            subs_count,
        )
        new.message = self.message
        return new

    async def reload_and_refresh(self, interaction, *, selected_channel_id=None):
        """Reload feed/follow state from the DB and re-render in place."""

        new = await self._reloaded(selected_channel_id)
        self.stop()
        await _refresh_layout(interaction, self.message, new)

    async def sync_message(self):
        """Re-render the stored panel message directly (used by the delete confirm)."""

        if self.message is None:
            return
        new = await self._reloaded(None)
        self.stop()
        try:
            await self.message.edit(view=new)
        except discord.HTTPException:
            pass


class _FeedNoticeView(discord.ui.LayoutView):
    """A one-shot ANILIST_BLUE notice card in the feed panel's house style.

    Non-interactive replacement for the classic ``discord.Embed`` replies of the
    ``/anilistfeed set`` / ``follow`` commands: a ``###`` heading over a body
    block inside a single :class:`~discord.ui.Container`. Carries no components,
    so it needs no author gating and spawns no timeout task.
    """

    def __init__(self, heading, body, *, timeout=None):
        super().__init__(timeout=timeout)
        container = discord.ui.Container(accent_colour=ANILIST_BLUE)
        text = "### " + heading
        if body:
            text += "\n" + body
        container.add_item(discord.ui.TextDisplay(text))
        self.add_item(container)


class _FeedListView(discord.ui.LayoutView):
    """The ``/anilistfeed list`` output as a Components V2 card.

    A ``###`` heading over one titled block per feed (channel label in bold, then
    its status / types / follows lines), each block separated by a rule - the
    house-style equivalent of the per-feed embed fields it replaces. ``blocks`` is
    a list of ``(label, body)`` pairs. Non-interactive, so no gating or timeout.
    """

    def __init__(self, heading, blocks, *, timeout=None):
        super().__init__(timeout=timeout)
        container = discord.ui.Container(accent_colour=ANILIST_BLUE)
        container.add_item(discord.ui.TextDisplay("### " + heading))
        for label, body in blocks:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.TextDisplay(
                    "**{label}**\n{body}".format(label=label, body=body)
                )
            )
        self.add_item(container)


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

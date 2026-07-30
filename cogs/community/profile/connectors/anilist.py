"""Purpose: the AniList profile connector - link a public AniList username,
cache its stats, draw a sober card section from that cache.

``link`` is a single PUBLIC GraphQL request (the query AniList's own
``USER_STATS_QUERY`` already uses for ``/anilist profile`` - reused verbatim
rather than restated, since the two surfaces want the exact same shape:
avatar, anime/manga statistics, and a few truncated favourites). No token: a
public username is all a v1 handle needs, and the AniList cog's OWN account
linking (encrypted ``anilist_tokens``, list editing) is a separate, unrelated
feature this connector does not touch.

AniList's GraphQL API has one quirk worth pinning: a ``User`` query for a name
that does not exist can come back either as HTTP 404 or as HTTP 200 with
``data.User`` null, depending on the query shape. :func:`_public_graphql`
folds both into the same "not found" signal so the caller does not have to
know which one AniList chose this time.

Like lastfm.py and backloggd.py (this package's other network connectors),
the ``Connector`` interface carries no ``bot`` reference, so this module takes
its lazily-created ``aiohttp.ClientSession`` from the package's own
``sessions`` registry rather than from ``tools.http.get_session`` (which needs
one). That registry is also what CLOSES it, in ``Profiles.cog_unload`` - see
sessions.py. It reuses ``tools/http.py``'s ``TIMEOUT`` constant, which is
bot-independent.

Rate is the one place this connector DOES want the real bot: AniList's per-IP
budget is SHARED with the airing/feed/chapter pollers (see
cogs/anilist/throttle.py), and a burst of profile links/refreshes should not
be free to eat into it. So :class:`AniListConnector` exposes an OPTIONAL
``bind_bot(bot)`` - not part of the base :class:`~.base.Connector` contract,
just a method this one connector happens to define - that ``Profiles.__init__``
calls best-effort on every connector that has one (see that cog's docstring).
When bound, :meth:`_throttle_for` reads the single
:class:`~cogs.anilist.throttle.AniListThrottle` instance off the AniList cog
(``bot.get_cog('AniList')._throttle``, exactly like
``cogs.anilist.feed_delivery._throttle_for``) and this connector's calls count
against the SAME process-wide ceiling the pollers already share the per-IP
budget with. Unbound (a test that never calls it, or the AniList cog not
being loaded) simply degrades to no throttling - never blocking - which is
the whole point of asking "is it accessible" rather than requiring it.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging

import discord

from .. import views as profile_views
from . import sessions
from .base import (
    Connector,
    ConnectorUnavailable,
    InvalidHandle,
    LinkResult,
    register,
    safe_number,
    safe_url,
)
from cogs.anilist.helpers import API_URL
from cogs.anilist.queries import USER_STATS_QUERY
from tools.http import TIMEOUT
from tools.i18n import N_, _

log = logging.getLogger(__name__)

# How many favourite titles the card ever shows, across anime AND manga
# together - the section is meant to be a glance, not a second list page.
_FAVOURITES_SHOWN = 3

# A title is user-authored text on AniList's side; clipped so one absurdly
# long entry cannot dominate the section (the payload cap catches a hostile
# blob regardless, this is just for a sane on-screen line).
_TITLE_CLIP = 80

# An avatar URL is third-party text too, and it goes into the SAME 8 KiB
# payload as the rest: it is filtered by base.safe_url (absolute http(s) only)
# and DROPPED past this length rather than truncated - half a url is not a
# url, and a Thumbnail Discord cannot fetch takes the whole card down at SEND
# time. Discord CDN-scale generous.
_URL_CLIP = 400

# How stale a cached payload may get before the scheduling hook in
# cogs/community/profile/cog.py bothers calling :meth:`AniListConnector.refresh`
# again - see that module's ``_connector_ttl``. An hour of lag on a mean score
# or a watch-minutes counter is invisible to a passive profile viewer.
REFRESH_TTL_SECONDS = 3600


async def _get_session():
    return await sessions.get_session("anilist")


def _throttle_for(bot):
    """The ONE shared interactive throttle, if a bot was bound and the AniList
    cog is loaded on it.

    Mirrors ``cogs.anilist.feed_delivery._throttle_for``: reads the single
    :class:`~cogs.anilist.throttle.AniListThrottle` instance off the AniList
    cog rather than owning a second one, so this connector's calls count
    against the SAME process-wide ceiling the pollers already share the
    per-IP budget with. Degrades to None (never blocks) when no bot is bound
    or that cog is not loaded.
    """
    if bot is None:
        return None
    get_cog = getattr(bot, "get_cog", None)
    if get_cog is None:
        return None
    return getattr(get_cog("AniList"), "_throttle", None)


async def _public_graphql(bot, connector_name, query, variables):
    """POST an unauthenticated GraphQL request to AniList.

    Returns the parsed JSON body, or ``None`` for AniList's "no such user"
    signal (see the module docstring). Anything else that goes wrong - a
    timeout, a non-2xx status, a 429 - raises :class:`ConnectorUnavailable`
    rather than handing the caller a partial or malformed body.
    """
    throttle = _throttle_for(bot)
    if throttle is not None and not throttle.allow_global():
        log.warning(
            "AniList interactive ceiling reached; dropping a profile "
            "connector request to protect the pollers"
        )
        raise ConnectorUnavailable(connector_name, "remote")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    session = await _get_session()
    try:
        async with session.post(
            API_URL,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=TIMEOUT,
        ) as r:
            status = r.status
            if status == 404:
                return None
            if status == 429:
                if throttle is not None:
                    throttle.note_throttled()
                log.warning(
                    "AniList returned HTTP 429 to a profile connector request"
                )
                raise ConnectorUnavailable(connector_name, "remote")
            if status >= 400:
                raise ConnectorUnavailable(connector_name, "remote")
            return await r.json()
    except ConnectorUnavailable:
        raise
    except Exception as exc:
        log.warning("AniList profile connector request failed: %s", exc)
        raise ConnectorUnavailable(connector_name, "remote") from exc


def _titles(section):
    """Up to 3 favourite titles (romaji) out of one favourites sub-list."""
    nodes = (section or {}).get("nodes") or []
    out = []
    for node in nodes[:_FAVOURITES_SHOWN]:
        title = ((node or {}).get("title") or {}).get("romaji")
        if title:
            out.append(str(title)[:_TITLE_CLIP])
    return out


def _build_payload(user):
    """The display cache: avatar, the two statistics blocks, a few favourites.

    Deliberately a SUBSET of what USER_STATS_QUERY fetches (it also carries
    favourite characters and per-genre breakdowns) - the section is sober by
    design, and a smaller payload is also more room under the 8 KiB cap for
    whatever the next connector needs.

    Every value here is a bounded number (base.safe_number, the same
    magnitude bound steam.py and osu.py apply) or a filtered url: nothing
    that Postgres could re-serialise LONGER than Python measured it, which is
    the margin base.PAYLOAD_MAX_BYTES asks for - an exponent-form float is
    written by json.dumps as ``1e+50`` and stored by Postgres as its 51
    digits (see the CHECK comment in schema.sql).
    """
    stats = user.get("statistics") or {}
    anime = stats.get("anime") or {}
    manga = stats.get("manga") or {}
    favourites = user.get("favourites") or {}
    return {
        "avatar": safe_url((user.get("avatar") or {}).get("large"), _URL_CLIP),
        "anime_count": safe_number(anime.get("count")),
        "anime_mean_score": safe_number(anime.get("meanScore")),
        "anime_minutes_watched": safe_number(anime.get("minutesWatched")),
        "manga_count": safe_number(manga.get("count")),
        "manga_mean_score": safe_number(manga.get("meanScore")),
        "manga_chapters_read": safe_number(manga.get("chaptersRead")),
        "favourite_anime": _titles(favourites.get("anime")),
        "favourite_manga": _titles(favourites.get("manga")),
    }


class AniListConnector(Connector):
    """Public AniList username -> a sober stats section."""

    name = "anilist"
    handle_hint = N_("your AniList username")

    def __init__(self):
        # Optional and opportunistic only - see the module docstring. None
        # until (and unless) Profiles.__init__ calls bind_bot.
        self._bot = None

    def bind_bot(self, bot):
        """Let this connector share the AniList cog's interactive throttle.

        Not part of the base Connector contract - a duck-typed extra that
        ``Profiles.__init__`` calls best-effort on any connector that defines
        it. Never required: unbound, this connector still links and refreshes
        normally, just without participating in that shared rate ceiling.
        """
        self._bot = bot

    async def link(self, user_id, raw_input):
        handle = (raw_input or "").strip()
        if not handle:
            raise InvalidHandle(self.name, "format")
        data = await _public_graphql(self._bot, self.name, USER_STATS_QUERY, {"name": handle})
        user = ((data or {}).get("data") or {}).get("User") if data else None
        if not user or not user.get("name"):
            raise InvalidHandle(self.name, "not_found")
        # external_id and display_name would be the exact same string (AniList
        # hands back the canonical casing of what was queried) - None says so
        # rather than storing the duplicate.
        return LinkResult(
            external_id=user["name"], display_name=None, payload=_build_payload(user)
        )

    async def refresh(self, user_id, connection):
        handle = connection.get("external_id")
        if not handle:
            raise ConnectorUnavailable(self.name, "remote")
        data = await _public_graphql(self._bot, self.name, USER_STATS_QUERY, {"name": handle})
        user = ((data or {}).get("data") or {}).get("User") if data else None
        if not user:
            # The account existed at link time and does not answer now
            # (renamed, deleted, AniList having a bad day) - never erase the
            # last good payload over that; the caller keeps what it had.
            raise ConnectorUnavailable(self.name, "remote")
        return _build_payload(user)


def _or_dash(value):
    return "-" if value is None else value


async def _render(container, field, viewer, connection, budget):
    """Draw the section from ``connection['payload']`` ONLY.

    No network, by contract (see views.register_section_renderer and every
    sibling connector's renderer): the cache this reads is filled at link time
    and topped up by the lazy refresh the profile cog schedules. Async only to
    match the signature the framework awaits - there is nothing to await here.
    """
    payload = connection.get("payload") or {}
    lines = ["**" + _(field.label) + "**"]

    anime_count = payload.get("anime_count")
    if anime_count:
        lines.append(
            _("Anime: {count} - {hours}h watched, mean score {score}").format(
                count=anime_count,
                hours=round((payload.get("anime_minutes_watched") or 0) / 60),
                score=_or_dash(payload.get("anime_mean_score")),
            )
        )

    manga_count = payload.get("manga_count")
    if manga_count:
        lines.append(
            _("Manga: {count} - {chapters} chapters read, mean score {score}").format(
                count=manga_count,
                chapters=payload.get("manga_chapters_read") or 0,
                score=_or_dash(payload.get("manga_mean_score")),
            )
        )

    favourites = (
        (payload.get("favourite_anime") or []) + (payload.get("favourite_manga") or [])
    )[:_FAVOURITES_SHOWN]
    if favourites:
        lines.append(_("Favourites: {titles}").format(titles=", ".join(favourites)))

    if len(lines) == 1:
        # A brand-new account (no entries, no favourites) would otherwise be a
        # bold heading over nothing. The handle is a fact the framework has
        # already decided this viewer may see, so it is the honest filler.
        handle = connection.get("display_name") or connection.get("external_id")
        if handle:
            lines.append(str(handle))

    text = discord.ui.TextDisplay("\n".join(lines))
    # Re-filtered here and not only at the parse: the payload is a row a PAST
    # version of this module wrote, and an unusable Thumbnail url is rejected
    # by Discord when the card is SENT - after render_sections' fallback to
    # the badge can no longer save the profile. Same discipline as every
    # sibling connector's renderer.
    avatar = safe_url(payload.get("avatar"))
    if avatar:
        container.add_item(discord.ui.Section(text, accessory=discord.ui.Thumbnail(avatar)))
    else:
        container.add_item(text)


register(AniListConnector())
profile_views.register_section_renderer("anilist", _render)

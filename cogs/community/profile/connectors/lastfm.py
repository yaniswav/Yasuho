"""Purpose: the Last.fm connector - handle = Last.fm username, no OAuth.

Follows the P4 shape the framework (``connectors/base.py``) prescribes: a
``Connector`` subclass that self-registers at module level, plus a section
renderer wired through ``views.register_section_renderer``. Nothing here
touches the database directly and nothing here is imported by ``__init__.py``
(that file's auto-discovery - see its own docstring - is what finds this
module; P4B's file list deliberately never edits it).

Three Last.fm calls, all unauthenticated (a public username is enough):

* ``user.getinfo``    - confirms the account exists (a 404-shaped answer from
  Last.fm is error code 6, "Invalid parameters", which every endpoint uses for
  "no such user"), and returns the scrobble count and an avatar url.
* ``user.gettopartists`` (period=1month, limit=3) - the "what have they been
  into lately" line.
* ``user.getrecenttracks`` (limit=1) - the most recent scrobble, flagged
  ``nowplaying`` when Last.fm still considers it in progress.

``link()`` makes all three calls up front so a freshly linked profile already
has something to show (the framework's "refresh network at LINK" half);
``refresh()`` repeats them for an already-linked account (the "LAZY at view"
half - wired in by whichever lot adds the scheduling hook, see
:data:`REFRESH_TTL_SECONDS`).

The renderer draws from ``connection["payload"]`` ONLY - no network in a
render, per the contract in views.py - and is deliberately plain: a scrobble
count, up to three top artists, and the last (or "now scrobbling") track.

Artist and track strings come from whatever SCROBBLER a member points at
their account, not from Last.fm's own catalogue, so they are treated as
hostile input: clipped to :data:`_NAME_MAX` before they can approach
``base.PAYLOAD_MAX_BYTES`` (a cap that refuses the write outright, which
would cost the link rather than a title), and the avatar is filtered down to
an absolute http(s) url because Discord rejects an entire message over one
Thumbnail it cannot fetch.

The API key is read LAZILY (``getstr`` inside a coroutine, never at import
time) so importing this module never requires ``tokens.ini`` to carry
``[APITokens] lastfmKey`` - its absence surfaces as
:class:`~.base.ConnectorUnavailable` with reason ``not_configured``, exactly
like every other "not set up on this bot yet" case the cog already renders.

No bot instance flows into a :class:`~.base.Connector` (the interface is
``link(user_id, raw_input)`` / ``refresh(user_id, connection)`` - see
base.py), so this module takes a lazily-created ``aiohttp.ClientSession`` from
the package's own ``sessions`` registry rather than reaching for the bot-wide
one in ``tools/http.py`` (which needs a ``bot`` to look up). That registry
holds the session under this connector's name and closes it in
``Profiles.cog_unload`` - see sessions.py. It reuses ``tools/http.py``'s
``TIMEOUT`` constant, which is bot-independent, so a slow Last.fm response
still cannot hang a link or a refresh.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
import re

import discord

from .. import views as profile_views
from . import base, sessions
from tools.config_loader import config_loader
from tools.http import TIMEOUT
from tools.i18n import N_, _, ngettext

log = logging.getLogger(__name__)

API_URL = "https://ws.audioscrobbler.com/2.0/"

# Last.fm usernames: historically 2-15 characters, letters/digits/underscore
# (a few legacy accounts carry a hyphen). Offline shape check only - existence
# is confirmed by the ``user.getinfo`` round trip in :meth:`link`.
HANDLE_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{2,15}\Z")

# How stale a cached payload may get before whatever schedules a lazy refresh
# should bother calling :meth:`LastFmConnector.refresh` again. "Now scrobbling"
# is a live-ish signal, so this is short next to Backloggd's 12h scraping
# courtesy window - Last.fm's API has no such politeness expectation for a
# public, unauthenticated read. Not enforced by this module itself (see the
# module docstring): it is metadata for the future scheduling hook, which
# P4B's file list does not include.
REFRESH_TTL_SECONDS = 15 * 60

# ---------------------------------------------------------------------------
# Bounds. An artist or track name is whatever a scrobbling client SENT - a
# user can scrobble a track titled with a hundred kilobytes - and it lands in
# ``profile_connections.payload``, which base.PAYLOAD_MAX_BYTES caps at 8 KiB
# by REFUSING the write. Clipping at the parse costs a truncated title;
# trusting the remote costs the link.
# ---------------------------------------------------------------------------

_NAME_MAX = 120

# Last.fm's error codes that mean "this bot's key is the problem", not "this
# user is": 10 invalid key, 26 suspended key. They deserve the same answer as
# a key that was never provisioned, because the fix is the same and it is an
# admin's, not the member's.
_KEY_ERROR_CODES = frozenset({10, 26})

# "no such user". Every Last.fm endpoint answers a bad username with error 6
# and an HTTP 200 - the status line says nothing.
_NO_SUCH_USER = 6


async def _get_session():
    return await sessions.get_session("lastfm")


def _api_key():
    """The Last.fm API key, or a typed refusal - read LAZILY, never at import.

    Same posture as steam.py's and osu.py's own ``_api_key``: ``getstr`` raises
    when ``tokens.ini`` has no ``[APITokens] lastfmKey`` at all, which is the
    common case until an admin provisions one, so any failure to read it
    means exactly one thing here - not configured yet.
    """
    try:
        key = config_loader.getstr("APITokens", "lastfmKey")
    except Exception:
        key = None
    if not key:
        raise base.ConnectorUnavailable("lastfm", "not_configured")
    return key


async def _request(method, user, extra=None):
    """One Last.fm API call; returns the parsed JSON body or raises.

    :class:`~.base.InvalidHandle` (reason ``not_found``) for Last.fm's own
    "no such user" answer (error code 6, returned with an HTTP 200 - Last.fm
    does not use the status line for this); :class:`~.base.ConnectorUnavailable`
    with reason ``not_configured`` for the two codes that mean this bot's KEY
    is the problem (:data:`_KEY_ERROR_CODES`), so an admin is told to fix the
    key rather than to wait out an outage; and reason ``remote`` for anything
    else network/HTTP/JSON-shaped.

    The code is read through :func:`_to_int` because Last.fm has shipped it
    as both a JSON number and a JSON string.
    """
    api_key = _api_key()
    params = {"method": method, "user": user, "api_key": api_key, "format": "json"}
    if extra:
        params.update(extra)
    session = await _get_session()
    try:
        async with session.get(API_URL, params=params, timeout=TIMEOUT) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = None
    except base.ConnectorError:
        raise
    except Exception as exc:  # timeout / connection reset / ...
        raise base.ConnectorUnavailable("lastfm", "remote") from exc
    if not isinstance(data, dict):
        raise base.ConnectorUnavailable("lastfm", "remote")
    error = data.get("error")
    if error is not None:
        error = _to_int(error)
        if error == _NO_SUCH_USER:
            raise base.InvalidHandle("lastfm", "not_found")
        if error in _KEY_ERROR_CODES:
            raise base.ConnectorUnavailable("lastfm", "not_configured")
        raise base.ConnectorUnavailable("lastfm", "remote")
    return data


# ---------------------------------------------------------------------------
# Pure parsing: no network, so these are unit-tested directly against canned
# API responses.
# ---------------------------------------------------------------------------


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip(value, limit=_NAME_MAX):
    """A bounded, stripped string, or ``None`` when there is nothing left."""
    text = str(value or "").strip()
    if not text:
        return None
    return text[:limit]


# The framework's own filter, consumed rather than restated: absolute http(s)
# or nothing, and over-long is DROPPED (half a url is not a url). Every
# connector in this package uses the same one, on both sides of the payload.
_safe_url = base.safe_url


def parse_info(data):
    """``user.getinfo`` -> ``{"name", "playcount", "avatar"}``, or ``None``."""
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        return None
    avatar = None
    images = user.get("image")
    if isinstance(images, list):
        # Last.fm lists sizes smallest-first; the last non-empty url is the
        # biggest one actually served (a blank "#text" is common for missing
        # sizes, never assume the LAST entry is populated).
        for entry in reversed(images):
            url = _safe_url(entry.get("#text")) if isinstance(entry, dict) else None
            if url:
                avatar = url
                break
    return {
        "name": _clip(user.get("name"), base.DISPLAY_NAME_MAX),
        "playcount": _to_int(user.get("playcount")),
        "avatar": avatar,
    }


def _as_list(value):
    """Last.fm's JSON collapses a ONE-element collection into the object
    itself instead of a one-element array (a scrobbler with a single track in
    the period, an account with one top artist). Both shapes are the same
    thing, so both are read as a list here rather than in two callers."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def parse_top_artists(data, limit=3):
    """``user.gettopartists`` -> up to ``limit`` ``{"name", "playcount"?}``."""
    top = data.get("topartists") if isinstance(data, dict) else None
    artists = _as_list(top.get("artist") if isinstance(top, dict) else None)
    if not artists:
        return []
    out = []
    for entry in artists[:limit]:
        if not isinstance(entry, dict):
            continue
        name = _clip(entry.get("name"))
        if not name:
            continue
        item = {"name": name}
        playcount = _to_int(entry.get("playcount"))
        if playcount is not None:
            item["playcount"] = playcount
        out.append(item)
    return out


def parse_recent_track(data):
    """``user.getrecenttracks`` -> the newest track dict, or ``None``."""
    recent = data.get("recenttracks") if isinstance(data, dict) else None
    tracks = _as_list(recent.get("track") if isinstance(recent, dict) else None)
    if not tracks:
        return None
    track = tracks[0]
    if not isinstance(track, dict):
        return None
    name = _clip(track.get("name"))
    artist = track.get("artist")
    artist_name = _clip(artist.get("#text") if isinstance(artist, dict) else artist)
    if not name or not artist_name:
        return None
    attr = track.get("@attr")
    # Last.fm marks the in-progress scrobble with @attr.nowplaying, and that
    # entry alone carries NO `date` - which is exactly how the two are told
    # apart. The flag is authoritative when present; a track that has a date
    # is, by construction, finished.
    nowplaying = isinstance(attr, dict) and str(attr.get("nowplaying")) == "true"
    result = {"artist": artist_name, "name": name, "nowplaying": bool(nowplaying)}
    album = track.get("album")
    album_name = _clip(album.get("#text") if isinstance(album, dict) else None)
    if album_name:
        result["album"] = album_name
    return result


def _one_line(text):
    """Flatten to one line - a scrobbled title/artist is third-party text
    riding into a Components V2 card, same discipline as the profile card's
    own label/value rows (see views.py's ``_one_line``)."""
    return " ".join(str(text).split())


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------


class LastFmConnector(base.Connector):
    name = "lastfm"
    handle_hint = N_("your Last.fm username")

    async def _fetch_all(self, handle):
        info = parse_info(await _request("user.getinfo", handle))
        if info is None:
            raise base.ConnectorUnavailable("lastfm", "remote")
        top_artists = parse_top_artists(
            await _request(
                "user.gettopartists", handle, {"period": "1month", "limit": 3}
            )
        )
        last_track = parse_recent_track(
            await _request("user.getrecenttracks", handle, {"limit": 1})
        )
        payload = {}
        if info.get("playcount") is not None:
            payload["playcount"] = info["playcount"]
        if info.get("avatar"):
            payload["avatar"] = info["avatar"]
        if top_artists:
            payload["top_artists"] = top_artists
        if last_track:
            payload["last_track"] = last_track
        display_name = info.get("name") or handle
        return payload, display_name

    async def link(self, user_id, raw_input):
        handle = (raw_input or "").strip()
        if not HANDLE_PATTERN.match(handle):
            raise base.InvalidHandle(self.name, "format")
        payload, display_name = await self._fetch_all(handle)
        return base.LinkResult(
            external_id=handle.lower(), display_name=display_name, payload=payload
        )

    async def refresh(self, user_id, connection):
        handle = connection.get("external_id") or ""
        if not handle:
            raise base.NotLinked(self.name)
        payload, _display_name = await self._fetch_all(handle)
        return payload


base.register(LastFmConnector())


# ---------------------------------------------------------------------------
# The renderer: draws from ``connection["payload"]`` only, never the network.
# ---------------------------------------------------------------------------


async def _render(container, field, viewer, connection, budget):
    payload = connection.get("payload") or {}
    lines = ["**" + _(field.label) + "**"]

    playcount = payload.get("playcount")
    if isinstance(playcount, int) and not isinstance(playcount, bool):
        lines.append(
            ngettext("{count} scrobble", "{count} scrobbles", playcount).format(
                count=playcount
            )
        )

    top_artists = payload.get("top_artists") or []
    names = ", ".join(
        _one_line(artist["name"])
        for artist in top_artists
        if isinstance(artist, dict) and artist.get("name")
    )
    if names:
        lines.append(_("Top artists (past month): {names}").format(names=names))

    last_track = payload.get("last_track")
    if not isinstance(last_track, dict):
        last_track = {}
    if last_track.get("name") and last_track.get("artist"):
        track = "{artist} - {title}".format(
            artist=_one_line(last_track["artist"]), title=_one_line(last_track["name"])
        )
        if last_track.get("nowplaying"):
            lines.append(_("Now scrobbling: {track}").format(track=track))
        else:
            lines.append(_("Last scrobble: {track}").format(track=track))

    text = discord.ui.TextDisplay("\n".join(lines))
    # Re-checked here and not only at the parse: the payload is a row a PAST
    # version of this module wrote, and an unusable Thumbnail url is rejected
    # by Discord when the card is SENT - after render_sections' fallback to
    # the badge can no longer save the profile. See :func:`_safe_url`.
    avatar = _safe_url(payload.get("avatar"))
    if avatar:
        container.add_item(discord.ui.Section(text, accessory=discord.ui.Thumbnail(avatar)))
    else:
        container.add_item(text)


profile_views.register_section_renderer("lastfm", _render)

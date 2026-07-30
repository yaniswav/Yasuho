"""Purpose: the Steam profile connector - a SteamID64 or a vanity URL name,
resolved and cached into a sober card section.

A handle is one of two shapes: a raw SteamID64 (17 digits) or a vanity name
(``ResolveVanityURL``, what ``steamcommunity.com/id/<name>`` uses). Either way
``link`` ends at the same real ``GetPlayerSummaries`` account, so both are
accepted and normalised down to the numeric id - the one identifier that
never changes even if the owner edits their vanity name later. A pasted
PROFILE URL (either form) is unwrapped to the same two shapes rather than
refused: it is what the Steam client puts in the clipboard, and it literally
contains the name the hint asks for.

Privacy is a real state Steam reports, not an error: ``communityvisibilitystate
!= 3`` means the profile is private (Steam's own API only ever returns 1 or 3
without an authenticated session - see the note on ``_is_private``), and the
payload says so rather than guessing at data Steam will not hand back anyway.
``GetRecentlyPlayedGames`` / ``GetOwnedGames`` are skipped entirely for a
private profile: Steam's "game details" privacy flag routinely hides them
even when the public summary answers, and asking for data that is not coming
either way would just be a second silent failure to swallow.

Requires ``[APITokens] steamKey`` in tokens.ini - NOT present by default.
Missing key is :class:`~.base.ConnectorUnavailable` with reason
``'not_configured'``, exactly like every other admin-must-set-this-up
connector; nothing here writes to config.

Like lastfm.py and backloggd.py (this package's other network connectors),
the ``Connector`` interface carries no ``bot`` reference, so this module takes
its lazily-created ``aiohttp.ClientSession`` from the package's own
``sessions`` registry rather than from ``tools.http.get_session`` (which needs
one). That registry is also what CLOSES it, in ``Profiles.cog_unload`` - see
sessions.py. It reuses ``tools/http.py``'s ``TIMEOUT`` constant, which is
bot-independent.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
import re

import discord

from .. import views as profile_views
from . import sessions
from .base import (
    MAX_SANE_NUMBER as _MAX_SANE_NUMBER,
)
from .base import (
    Connector,
    ConnectorUnavailable,
    InvalidHandle,
    LinkResult,
    register,
    safe_url,
)
from tools.config_loader import config_loader
from tools.http import TIMEOUT
from tools.i18n import N_, _

log = logging.getLogger(__name__)

API_BASE = "https://api.steampowered.com"

# A SteamID64 is always a 17-digit number (the 64-bit id, not the shorter
# legacy STEAM_0:.. form which the profile UI never shows). Anything else
# typed is treated as a vanity name and resolved through the API instead.
_STEAMID64_PATTERN = re.compile(r"\A\d{17}\Z")

# Steam's own vanity name charset (letters, digits, underscore and dash), 2 to
# 32 characters - generous next to the 32-character cap Steam's own profile
# editor enforces.
_VANITY_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{2,32}\Z")

# What people actually have in the clipboard: the profile URL Steam shows
# them, in either of its two forms (``/id/<vanity>`` or ``/profiles/<id64>``).
# The handle hint asks for the NAME, but refusing the URL that contains it -
# with a "that does not look right" - would be a bad joke, so it is unwrapped
# to the same two shapes everything below already understands.
_PROFILE_URL_PATTERN = re.compile(
    r"\A(?:https?://)?(?:www\.)?steamcommunity\.com/(?:id|profiles)/"
    r"([A-Za-z0-9_-]+)/*\Z",
    re.IGNORECASE,
)

# Steam's PUBLIC (unauthenticated) API only ever returns 1 (not visible to
# you: private, friends-only, ...) or 3 (public) for this field - there is no
# way to tell "private" from "friends-only" without the viewer's own Steam
# session, which this bot never has. Treat anything but 3 as private.
_PUBLIC_VISIBILITY = 3

# How many recently-played titles the section shows - a glance, not a list.
_RECENT_GAMES_SHOWN = 3
_GAME_NAME_CLIP = 80

# Third-party strings that go into the same 8 KiB payload as the rest: a
# persona name is 32 characters in Steam's own UI and an avatar URL is short,
# but neither is validated by anything this bot controls. The name is clipped;
# the url goes through base.safe_url instead (absolute http(s), and DROPPED
# past this length rather than truncated - half a url is not a url, and a
# Thumbnail Discord cannot fetch takes the whole card down at SEND time).
_PERSONA_CLIP = 80
_URL_CLIP = 400

# How stale a cached payload may get before the scheduling hook in
# cogs/community/profile/cog.py bothers calling :meth:`SteamConnector.refresh`
# again - see that module's ``_connector_ttl``.
REFRESH_TTL_SECONDS = 3600


async def _get_session():
    return await sessions.get_session("steam")


async def _get_json(connector_name, path, params):
    """GET one Steam Web API endpoint; return its parsed ``response`` object.

    Steam answers with HTTP 200 and an EMPTY ``{"response": {}}`` for almost
    every error condition (a bad key, an unknown steamid, ...) rather than a
    4xx, so there is no status-code signal worth branching on here beyond
    "the request itself failed" - that maps to :class:`ConnectorUnavailable`,
    and an empty/missing field is for the caller to interpret.
    """
    session = await _get_session()
    try:
        async with session.get(
            API_BASE + path, params=params, timeout=TIMEOUT
        ) as r:
            if r.status != 200:
                raise ConnectorUnavailable(connector_name, "remote")
            data = await r.json()
    except ConnectorUnavailable:
        raise
    except Exception as exc:
        # The exception TYPE only, never the exception: aiohttp puts the
        # request URL in its message, and every url here carries ``key=`` -
        # this bot's Steam API key - in its query string. A log line is not a
        # place to leak a credential.
        log.warning("Steam API request to %s failed: %s", path, type(exc).__name__)
        raise ConnectorUnavailable(connector_name, "remote") from exc
    return (data or {}).get("response") or {}


async def _resolve_vanity(connector_name, key, vanity):
    """SteamID64 for a vanity name, or None when Steam has no such name."""
    response = await _get_json(
        connector_name,
        "/ISteamUser/ResolveVanityURL/v0001/",
        {"key": key, "vanityurl": vanity},
    )
    if response.get("success") == 1 and response.get("steamid"):
        return str(response["steamid"])
    return None


async def _player_summary(connector_name, key, steamid):
    """The one player summary for ``steamid``, or None when Steam has none."""
    response = await _get_json(
        connector_name,
        "/ISteamUser/GetPlayerSummaries/v0002/",
        {"key": key, "steamids": steamid},
    )
    players = response.get("players") or []
    return players[0] if players else None


def _is_private(summary):
    return summary.get("communityvisibilitystate") != _PUBLIC_VISIBILITY


async def _recent_games(connector_name, key, steamid):
    response = await _get_json(
        connector_name,
        "/IPlayerService/GetRecentlyPlayedGames/v0001/",
        {"key": key, "steamid": steamid, "count": _RECENT_GAMES_SHOWN},
    )
    games = []
    for game in (response.get("games") or [])[:_RECENT_GAMES_SHOWN]:
        name = game.get("name")
        if not name:
            continue
        games.append(
            {
                "name": str(name)[:_GAME_NAME_CLIP],
                "hours_2weeks": _hours(game.get("playtime_2weeks")),
            }
        )
    return games


async def _owned_games_count(connector_name, key, steamid):
    response = await _get_json(
        connector_name,
        "/IPlayerService/GetOwnedGames/v0001/",
        {"key": key, "steamid": steamid},
    )
    return _count(response.get("game_count"))


def _clip(value, limit):
    """Third-party text, bounded and stringified, or None."""
    if value is None:
        return None
    return str(value)[:limit]


# The magnitude bound below is the framework's (base.MAX_SANE_NUMBER),
# imported under the name this module already used: above it a number is not
# a playtime or a library size, it is garbage - or an attack on the payload
# SIZE. See that constant's own comment for the exponent-form float story.


def _hours(minutes):
    """Minutes (Steam sends an int, but nothing here controls that) as hours
    with one decimal; 0 for anything missing, non-numeric or absurd."""
    try:
        value = float(minutes)
    except (TypeError, ValueError):
        return 0
    if not -_MAX_SANE_NUMBER < value < _MAX_SANE_NUMBER:
        return 0
    return round(value / 60, 1)


def _count(value):
    """A third-party integer, or None when it is not one."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if -_MAX_SANE_NUMBER < number < _MAX_SANE_NUMBER else None


def _build_payload(summary, private, recent_games, owned_count):
    """The display cache. Every value is a clipped string, a bool, a small
    integer or a one-decimal float - nothing Postgres could re-serialise
    longer than Python measured it, which is the margin
    base.PAYLOAD_MAX_BYTES asks for (see the CHECK comment in schema.sql).
    """
    payload = {
        "persona_name": _clip(summary.get("personaname"), _PERSONA_CLIP),
        "avatar": safe_url(
            summary.get("avatarfull") or summary.get("avatarmedium"), _URL_CLIP
        ),
        "private": private,
    }
    if not private:
        payload["recent_games"] = recent_games
        payload["owned_games_count"] = owned_count
    return payload


class SteamConnector(Connector):
    """SteamID64 or vanity name -> persona, avatar and recent activity."""

    name = "steam"
    handle_hint = N_("your SteamID64 or your steamcommunity.com/id/ name")

    def _api_key(self):
        try:
            key = config_loader.getstr("APITokens", "steamKey")
        except Exception:
            key = None
        if not key:
            raise ConnectorUnavailable(self.name, "not_configured")
        return key

    async def _resolve_steamid(self, key, handle):
        url = _PROFILE_URL_PATTERN.match(handle)
        if url is not None:
            handle = url.group(1)
        if _STEAMID64_PATTERN.match(handle):
            return handle
        if not _VANITY_PATTERN.match(handle):
            raise InvalidHandle(self.name, "format")
        steamid = await _resolve_vanity(self.name, key, handle)
        if steamid is None:
            raise InvalidHandle(self.name, "not_found")
        return steamid

    async def _fetch(self, key, steamid):
        """Everything a link/refresh needs about one account, as a payload."""
        summary = await _player_summary(self.name, key, steamid)
        if summary is None:
            return None
        private = _is_private(summary)
        recent_games, owned_count = [], None
        if not private:
            recent_games = await _recent_games(self.name, key, steamid)
            owned_count = await _owned_games_count(self.name, key, steamid)
        return summary, _build_payload(summary, private, recent_games, owned_count)

    async def link(self, user_id, raw_input):
        handle = (raw_input or "").strip()
        if not handle:
            raise InvalidHandle(self.name, "format")
        key = self._api_key()
        steamid = await self._resolve_steamid(key, handle)
        fetched = await self._fetch(key, steamid)
        if fetched is None:
            raise InvalidHandle(self.name, "not_found")
        summary, payload = fetched
        return LinkResult(
            external_id=steamid,
            display_name=summary.get("personaname"),
            payload=payload,
        )

    async def refresh(self, user_id, connection):
        steamid = connection.get("external_id")
        if not steamid:
            raise ConnectorUnavailable(self.name, "remote")
        key = self._api_key()
        fetched = await self._fetch(key, steamid)
        if fetched is None:
            # The account existed at link time (banned, deleted, or Steam
            # having a bad day now) - never erase the last good payload.
            raise ConnectorUnavailable(self.name, "remote")
        _summary, payload = fetched
        return payload


async def _render(container, field, viewer, connection, budget):
    """Draw the section from ``connection['payload']`` ONLY.

    No network, by contract (see views.register_section_renderer and every
    sibling connector's renderer): the cache this reads is filled at link time
    and topped up by the lazy refresh the profile cog schedules. Async only to
    match the signature the framework awaits - there is nothing to await here.
    """
    payload = connection.get("payload") or {}
    lines = ["**" + _(field.label) + "**"]

    if payload.get("private"):
        lines.append(_("This Steam profile is private."))
    else:
        recent = payload.get("recent_games") or []
        if recent:
            # Same msgid as the Backloggd section's own recently-played line,
            # reused rather than re-minted with a different placeholder name.
            lines.append(
                _("Recently played: {names}").format(
                    names=", ".join(
                        "{name} ({hours}h)".format(
                            name=game.get("name"), hours=game.get("hours_2weeks")
                        )
                        for game in recent
                        if isinstance(game, dict) and game.get("name")
                    )
                )
            )
        owned = payload.get("owned_games_count")
        if owned:
            lines.append(_("Owns {count} games").format(count=owned))

    if len(lines) == 1:
        # Public profile, but Steam's separate "game details" privacy flag hid
        # everything worth counting. Name the account rather than leave a bold
        # heading standing over nothing.
        handle = (
            payload.get("persona_name")
            or connection.get("display_name")
            or connection.get("external_id")
        )
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


register(SteamConnector())
profile_views.register_section_renderer("steam", _render)

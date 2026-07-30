"""Purpose: the osu! profile connector - a public username, cached into a
sober card section (rank, pp, accuracy, level, country).

Uses the osu! API v1 ``get_user`` endpoint and the ``osuKey`` already sitting
in tokens.ini for the existing ``?osu`` lookup command (cogs/utility/searchweb.py)
- the same key, read the same way, so there is nothing new to provision. v1
answers with a JSON array: one element per matching user, empty when there is
none, which is what tells a bad handle apart from a working one.

The user TYPES a username, but what gets stored is the numeric ``user_id``,
with the username kept as the display name. osu! lets people rename, and v1's
``u`` parameter is ambiguous between the two - so refreshing by name would
quietly start failing the day someone renames (forever: a failed refresh never
updates anything, it just gets re-attempted). Refreshing by id with
``type=id`` cannot drift.

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

API_URL = "https://osu.ppy.sh/api/get_user"

# Third-party strings sharing the one 8 KiB payload with everything else: an
# osu! username is at most 15 characters and a country is an ISO-3166 pair,
# but neither is validated by anything this bot controls.
_NAME_CLIP = 80
_COUNTRY_CLIP = 8

# The magnitude bound (base.MAX_SANE_NUMBER, imported above under the name
# this module already used): past it a value is not a rank, a pp total or an
# accuracy - see _number, and that constant's own comment for why the bound
# is about the payload SIZE as much as about plausibility.

# A stored external_id that is all digits is a user id (what link() writes);
# anything else is a legacy/hand-written handle and is looked up by name.
_USER_ID_PATTERN = re.compile(r"\A\d+\Z")

# How stale a cached payload may get before the scheduling hook in
# cogs/community/profile/cog.py bothers calling :meth:`OsuConnector.refresh`
# again - see that module's ``_connector_ttl``.
REFRESH_TTL_SECONDS = 3600


async def _get_session():
    return await sessions.get_session("osu")


async def _fetch_user(connector_name, key, handle, kind=None):
    """The one osu! user matching ``handle``, or None when there isn't one.

    ``kind`` is v1's ``type`` parameter: ``'id'`` pins the lookup to a numeric
    user id (what :meth:`OsuConnector.refresh` stores), ``None`` leaves v1 to
    guess, which is what a freshly typed handle needs.
    """
    params = {"k": key, "u": handle}
    if kind:
        params["type"] = kind
    session = await _get_session()
    try:
        async with session.get(
            API_URL, params=params, timeout=TIMEOUT
        ) as r:
            if r.status != 200:
                raise ConnectorUnavailable(connector_name, "remote")
            data = await r.json()
    except ConnectorUnavailable:
        raise
    except Exception as exc:
        # The exception TYPE only, never the exception: aiohttp puts the
        # request URL in its message, and every url here carries ``k=`` -
        # this bot's osu! API key - in its query string. A log line is not a
        # place to leak a credential.
        log.warning("osu! API request failed: %s", type(exc).__name__)
        raise ConnectorUnavailable(connector_name, "remote") from exc
    if not data:
        return None
    return data[0]


def _clip(value, limit):
    """Third-party text, bounded and stringified, or None."""
    if value is None:
        return None
    return str(value)[:limit]


def _number(value, digits):
    """A v1 numeric field (they all arrive as STRINGS) as a rounded float, or
    None when it is missing, not a number, or absurd.

    Rounding happens HERE, not in the renderer: a renderer that parses is a
    renderer that can raise, and the framework would answer that with the
    generic "Linked" badge for an account whose data was perfectly fine. The
    magnitude bound is what keeps an exponent-form float out of the payload -
    json.dumps writes ``1e+50`` where Postgres stores its 51 digits, the one
    case where base.encode_payload's count under-measures the schema CHECK
    (see that constraint's own comment).
    """
    try:
        number = round(float(value), digits)
    except (TypeError, ValueError):
        return None
    return number if -_MAX_SANE_NUMBER < number < _MAX_SANE_NUMBER else None


def _integer(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if -_MAX_SANE_NUMBER < number < _MAX_SANE_NUMBER else None


def _build_payload(user):
    """The display cache.

    Numbers are parsed and rounded to a couple of decimals, strings clipped:
    nothing here can be re-serialised by Postgres longer than Python measured
    it (no exponent-form floats), which is the margin base.PAYLOAD_MAX_BYTES
    asks for - see the CHECK comment in schema.sql.
    """
    return {
        "username": _clip(user.get("username"), _NAME_CLIP),
        "rank": _integer(user.get("pp_rank")),
        "pp": _number(user.get("pp_raw"), 1),
        "accuracy": _number(user.get("accuracy"), 2),
        "level": _number(user.get("level"), 1),
        "country": _clip(user.get("country"), _COUNTRY_CLIP),
        # Built by this module, not received - but still filtered, so the
        # ONE rule about what may become a Thumbnail lives in one place (and
        # so a future change to the avatar host cannot quietly ship a url the
        # card would choke on). See base.safe_url.
        "avatar": safe_url(
            "https://a.ppy.sh/{id}".format(id=_integer(user.get("user_id")))
        )
        if _integer(user.get("user_id")) is not None
        else None,
    }


class OsuConnector(Connector):
    """osu! username -> rank, pp, accuracy, level and country."""

    name = "osu"
    handle_hint = N_("your osu! username")

    def _api_key(self):
        try:
            key = config_loader.getstr("APITokens", "osuKey")
        except Exception:
            key = None
        if not key:
            raise ConnectorUnavailable(self.name, "not_configured")
        return key

    async def link(self, user_id, raw_input):
        handle = (raw_input or "").strip()
        if not handle:
            raise InvalidHandle(self.name, "format")
        key = self._api_key()
        user = await _fetch_user(self.name, key, handle)
        if user is None:
            raise InvalidHandle(self.name, "not_found")
        payload = _build_payload(user)
        # The numeric id is what survives a rename; the name is what people
        # read. Storing the id with no display name would show a bare number
        # in `connections list`, so both are kept.
        user_id_value = _integer(user.get("user_id"))
        if user_id_value is None:
            # v1 always returns one, but a handle with no id behind it is not
            # something to store and then fail to refresh forever.
            raise InvalidHandle(self.name, "not_found")
        return LinkResult(
            external_id=str(user_id_value),
            display_name=payload.get("username") or handle,
            payload=payload,
        )

    async def refresh(self, user_id, connection):
        handle = connection.get("external_id")
        if not handle:
            raise ConnectorUnavailable(self.name, "remote")
        key = self._api_key()
        kind = "id" if _USER_ID_PATTERN.match(str(handle)) else None
        user = await _fetch_user(self.name, key, handle, kind)
        if user is None:
            # Renamed, restricted or the API having a bad day - never erase
            # the last good payload over that.
            raise ConnectorUnavailable(self.name, "remote")
        return _build_payload(user)


async def _render(container, field, viewer, connection, budget):
    """Draw the section from ``connection['payload']`` ONLY.

    No network, by contract (see views.register_section_renderer and every
    sibling connector's renderer): the cache this reads is filled at link time
    and topped up by the lazy refresh the profile cog schedules. Async only to
    match the signature the framework awaits - there is nothing to await here.
    """
    payload = connection.get("payload") or {}
    lines = ["**" + _(field.label) + "**"]
    rank = payload.get("rank")
    if rank:
        lines.append(
            _("Rank #{rank} - {pp} pp").format(rank=rank, pp=payload.get("pp") or 0)
        )
    accuracy = payload.get("accuracy")
    if accuracy is not None:
        # Both values were parsed and rounded when the payload was built, so
        # there is nothing to convert (and nothing to raise) here.
        lines.append(
            _("Accuracy {accuracy}% - level {level}").format(
                accuracy=accuracy,
                level=payload.get("level") if payload.get("level") is not None else "-",
            )
        )
    country = payload.get("country")
    if country:
        lines.append(_("Country: {country}").format(country=country))

    if len(lines) == 1:
        # A brand-new account has no rank, no pp and no accuracy yet.
        handle = payload.get("username") or connection.get("display_name")
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


register(OsuConnector())
profile_views.register_section_renderer("osu", _render)

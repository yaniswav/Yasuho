"""Purpose: THE contract an external connector implements, plus the bounded
registry the cog routes every user word through.

This lot ships the FRAMEWORK, not the connectors. AniList, Steam, Last.fm, osu!
and Backloggd land in P4 as one module each; what lives here is the shape they
must take, the typed failures they may raise, and the guard rails that stop a
connector from writing something the database, the card or a translator cannot
digest.

Three ideas carry the design:

* A connector is a small object with a NAME that is already a profile section.
  The parent registry reserved seven section names (``anilist``, ``steam``,
  ``lastfm``, ``osu``, ``backloggd``, ``presence_gaming``, ``spotify_presence``)
  and this module derives its own vocabulary from that tuple instead of
  restating it, so a rename there cannot leave an orphan here.
* ``link()`` VALIDATES, it does not fetch. A handle must be accepted or refused
  from its shape alone; touching the remote is allowed but optional, and a
  remote that is down raises :class:`ConnectorUnavailable` rather than storing a
  handle nobody checked or refusing a handle that is perfectly good. Linking
  therefore keeps working when a third party has a bad day, and P4 modules can
  be unit-tested without the network.
* ``refresh()`` returns DISPLAYABLE data only, as a JSON-serialisable dict, and
  the result is capped here before it can reach SQL. The payload is a cache of
  what the card draws, never a credential and never a raw page.

NO TOKENS LIVE IN THIS PACKAGE. AniList keeps its own encrypted ``anilist_tokens``
row (Fernet, shipped long before this chantier); every other v1 connector is
keyed by a public username or id, so there is nothing secret to store. A token
table for connectors is deliberately NOT created in advance: the day a second
OAuth connector exists it will need its own scopes, refresh cadence and
revocation path, and inventing that table now would be guessing (YAGNI). What is
NOT negotiable is where such a secret may live - encrypted at rest, in its own
table, never in ``profile_connections.payload``, which is exported verbatim by
/mydata.

Pure module: no bot, no database, no discord. Typography rule: ASCII '-' and
'...' only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .. import registry as profile_registry

# ---------------------------------------------------------------------------
# Vocabulary, derived from the parent registry so the two cannot drift.
# ---------------------------------------------------------------------------

# Every profile section a connector may own. Mirrored by the CHECK on
# profile_connections.connector in schema.sql (a test pins the three lists
# together).
SECTIONS = tuple(
    entry.name for entry in profile_registry.FIELDS if entry.kind == "connector"
)

# The two sections fed by Discord PRESENCE, not by a handle the user types.
# They are reachable in the schema (P5 may want a marker row) but they are not
# linkable: there is nothing to type, and the opt-in is a different consent
# question, so they are refused by this cog rather than half-supported.
PRESENCE_SECTIONS = ("presence_gaming", "spotify_presence")

# What ``connections link`` can ever accept. The cog spells the same tuple as a
# Literal - an annotation has to be static - and a test pins the two together.
LINKABLE = tuple(name for name in SECTIONS if name not in PRESENCE_SECTIONS)

# ---------------------------------------------------------------------------
# Caps. Mirrored by the CHECKs in schema.sql; the belt to their suspenders.
# ---------------------------------------------------------------------------

# Generous next to every real handle (a SteamID64 is 17 characters, a Last.fm
# username at most 15, an AniList name 20, an osu! name 15), and small enough
# that a hostile or buggy connector cannot turn a row into a blob.
EXTERNAL_ID_MAX = 190
DISPLAY_NAME_MAX = 190

# The payload is a CACHE of what the card draws: a handful of counts, a few
# titles, an avatar URL. 8 KiB is roomy for that and bounds the table at seven
# connectors per user - the difference between a few MB and a few GB at the
# scale this bot is designed for.
#
# Aim UNDER it, do not fill it: the CHECK in schema.sql measures the canonical
# text Postgres re-serialises, which matches this count byte for byte for
# strings, integers, booleans, nulls and nesting but NOT for floats written in
# exponent form (1e+50 is stored as its 51 digits). See that constraint's own
# comment for the probe.
PAYLOAD_MAX_BYTES = 8192

# The bound on any URL a connector may put in its payload. A url is
# third-party text that ends up in a Components V2 ``Thumbnail``, and it
# shares the 8 KiB above with everything else the card draws: generous next to
# any real CDN avatar, small enough that one pathological string cannot be the
# reason a whole refresh is refused.
URL_MAX = 400

# Above this, a third-party number is not a count, a playtime, a rank or a
# score - it is garbage, or an attack on the payload SIZE: a float big enough
# to need exponent form is written by json.dumps as ``1e+50`` where Postgres
# re-serialises its 51 digits, which is the one way :func:`encode_payload`'s
# count can under-measure what the schema CHECK then refuses (see that
# constraint's own comment).
MAX_SANE_NUMBER = 10**12


# ---------------------------------------------------------------------------
# Third-party value hygiene, shared by every connector.
#
# These live HERE and not five times over because all five modules face the
# same two hazards with the same answer, on BOTH sides of the payload: at the
# parse (what may be stored) and at the render (what a row written by a PAST
# version of the module may contain).
# ---------------------------------------------------------------------------


def safe_url(value, limit=URL_MAX):
    """An absolute http(s) URL, or ``None`` - never a truncated one.

    Discord rejects the WHOLE message over a ``Thumbnail`` url it cannot
    fetch, at SEND time - which takes the entire ``/profile view`` card down,
    past the point where ``views.render_sections`` can still fall back to the
    "Linked" badge (it has already returned by then). So a url that is
    relative, protocol-relative or carries an exotic scheme is DROPPED here
    rather than trusted.

    Over-long is dropped too, not clipped: half a url is not a url, and a
    truncated one is exactly the unfetchable Thumbnail this exists to avoid.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > limit:
        return None
    if not text.lower().startswith(("https://", "http://")):
        return None
    return text


def safe_number(value, limit=MAX_SANE_NUMBER):
    """A third-party number, or ``None`` when it is missing, not a number or
    absurd (see :data:`MAX_SANE_NUMBER`).

    Booleans are refused on purpose: ``isinstance(True, int)`` is True in
    Python, and a JSON ``true`` where a count was promised is not a count.
    NaN and the infinities fall out for free - every comparison against them
    is False, so they fail the range test.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not -limit < value < limit:
        return None
    return value


# ---------------------------------------------------------------------------
# Typed errors, so a caller maps a failure to a message without string matching
# (the same posture as registry.py and cogs/community/leveling/rank_card.py).
# ---------------------------------------------------------------------------


class ConnectorError(Exception):
    """Base class for every connector rejection."""


class UnknownConnector(ConnectorError):
    """The name is not one of the reserved profile sections at all."""

    def __init__(self, name):
        super().__init__(f"unknown connector: {name!r}")
        self.name = name


class InvalidHandle(ConnectorError):
    """The user typed something this connector cannot accept.

    ``reason`` drives the message: 'format' (shape refused offline), 'not_found'
    (the remote says no such account), 'too_long' (past :data:`EXTERNAL_ID_MAX`).
    """

    def __init__(self, connector, reason="format", limit=None):
        super().__init__(f"invalid handle for {connector!r}: {reason}")
        self.connector = connector
        self.reason = reason
        self.limit = limit


class ConnectorUnavailable(ConnectorError):
    """This connector cannot serve the request right now.

    ``reason`` is 'coming_soon' (reserved, no implementation yet - the state
    every connector is in until P4), 'not_configured' (the API key is absent
    from tokens.ini) or 'remote' (the third party failed or timed out). One
    error type, three reasons: they all mean "not your fault, try later".
    """

    def __init__(self, connector, reason="remote"):
        super().__init__(f"connector {connector!r} unavailable: {reason}")
        self.connector = connector
        self.reason = reason


class NotLinked(ConnectorError):
    """The user has no row for that connector (unlinked, or never linked)."""

    def __init__(self, connector):
        super().__init__(f"connector {connector!r} is not linked")
        self.connector = connector


class InvalidPayload(ConnectorError):
    """A refresh returned something that must not reach the database."""

    def __init__(self, connector, reason, limit=None):
        super().__init__(f"invalid payload for {connector!r}: {reason}")
        self.connector = connector
        self.reason = reason
        self.limit = limit


# ---------------------------------------------------------------------------
# What a link produces.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkResult:
    """The normalised outcome of ``Connector.link``.

    ``external_id`` is what the bot will query the remote with forever after (a
    SteamID64, a lowercased username, an AniList numeric id); ``display_name``
    is the pretty form to show, and may be None when the two are the same.
    ``payload`` seeds the display cache so a fresh link already draws something
    before the first refresh.
    """

    external_id: str
    display_name: str | None = None
    payload: dict = field(default_factory=dict)


def validate_link_result(connector, result):
    """Normalise and cap a :class:`LinkResult` before it can reach SQL.

    Connectors are ordinary Python written by future lots; this is the one place
    that decides a returned handle is storable. An empty or over-long
    ``external_id`` is a hard :class:`InvalidHandle` (it would either query the
    remote with nothing or violate the CHECK), while an over-long
    ``display_name`` is merely trimmed - a cosmetic field must never cost the
    user their link.
    """
    if not isinstance(result, LinkResult):
        raise InvalidHandle(connector, "format")
    external_id = (result.external_id or "").strip()
    if not external_id:
        raise InvalidHandle(connector, "format")
    if len(external_id) > EXTERNAL_ID_MAX:
        raise InvalidHandle(connector, "too_long", EXTERNAL_ID_MAX)
    display_name = result.display_name
    if isinstance(display_name, str):
        display_name = display_name.strip()[:DISPLAY_NAME_MAX] or None
    elif display_name is not None:
        display_name = None
    return LinkResult(
        external_id=external_id,
        display_name=display_name,
        payload=result.payload if isinstance(result.payload, dict) else {},
    )


def encode_payload(connector, payload):
    """Return the payload as a compact JSON object string, or raise.

    ``default=str`` keeps a datetime a connector forgot to format from failing a
    whole refresh; the size cap is what actually matters, because the payload is
    written by third-party data and read back into every profile card.
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InvalidPayload(connector, "type")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        raise InvalidPayload(connector, "serialisation") from exc
    if len(encoded.encode("utf-8")) > PAYLOAD_MAX_BYTES:
        raise InvalidPayload(connector, "too_large", PAYLOAD_MAX_BYTES)
    return encoded


# ---------------------------------------------------------------------------
# The interface.
# ---------------------------------------------------------------------------


class Connector:
    """One external source of profile data.

    Subclasses set ``name`` (a member of :data:`LINKABLE`), and may set
    ``label`` and ``handle_hint`` (wrap both in ``N_`` so the extractor sees
    them and the cog translates them at render time), then implement ``link``
    and ``refresh``.

    Contract, in the order the framework calls them:

    * ``link(user_id, raw_input)`` -> :class:`LinkResult`. Validates the handle
      and returns what to store. MUST be decidable without the network; a
      connector that chooses to confirm the account remotely raises
      :class:`ConnectorUnavailable` when the remote fails instead of guessing.
      Raises :class:`InvalidHandle` for anything it will not accept.
    * ``refresh(user_id, connection)`` -> dict. Fetches the displayable cache
      for an already-linked account. ``connection`` is the stored row (mapping
      with ``external_id``, ``display_name``, ``payload``, ...). Raises
      :class:`ConnectorUnavailable` on a remote failure - never returns a
      half-empty payload, which would erase the last good card.
    * ``unlink(user_id)`` - optional hook for a connector that holds state
      elsewhere (revoking an OAuth grant, dropping a cache entry). The row and
      the visibility line are deleted by the framework either way, so the
      default does nothing and a failure here must never block the unlink.

    Nothing in here is allowed to store a credential. See the module docstring.
    """

    # Both default to empty rather than to a generic string: an unset label
    # falls back to the section's real name in the parent registry ("Steam",
    # "osu!"), and an unset hint falls back to the generic refusal message. A
    # placeholder default would ship "Connection" to users and a dead msgid to
    # translators.
    name = ""
    label = ""
    handle_hint = ""

    async def link(self, user_id, raw_input):
        raise NotImplementedError

    async def refresh(self, user_id, connection):
        raise NotImplementedError

    async def unlink(self, user_id):
        return None


# ---------------------------------------------------------------------------
# The registry: bounded by construction.
# ---------------------------------------------------------------------------

# name -> Connector instance. The keys are a SUBSET of SECTIONS and each name
# may be registered once, so this mapping can never hold more than seven entries
# however many extensions are loaded - there is no user-driven growth path into
# it, which is the whole point of routing user words through a whitelist.
CONNECTORS = {}


def register(connector):
    """Add a connector to the registry; return it, so a module can register at
    definition site.

    Refuses a name outside :data:`LINKABLE` (an unreserved section, or one of
    the presence sections which are not handle-linkable) and refuses to replace
    an existing entry - a silent overwrite would make load order decide which
    Steam connector users get.
    """
    name = getattr(connector, "name", "")
    if name not in LINKABLE:
        raise UnknownConnector(name)
    if name in CONNECTORS:
        raise ValueError(f"connector {name!r} is already registered")
    CONNECTORS[name] = connector
    return connector


def unregister(name):
    """Remove a connector (used by tests and by an extension unload)."""
    return CONNECTORS.pop(name, None)


def is_section(name):
    """True when ``name`` is one of the reserved connector sections."""
    return name in SECTIONS


def get(name):
    """Return the connector called ``name``, or raise a typed error.

    :class:`UnknownConnector` means "no such section, check your spelling";
    :class:`ConnectorUnavailable` with reason 'coming_soon' means "reserved, but
    P4 has not landed it yet" - which is what every name answers today.
    """
    if name not in LINKABLE:
        raise UnknownConnector(name)
    try:
        return CONNECTORS[name]
    except KeyError:
        raise ConnectorUnavailable(name, "coming_soon") from None


def available():
    """Names with a working implementation, in registry order."""
    return tuple(name for name in LINKABLE if name in CONNECTORS)


def coming_soon():
    """Linkable names still waiting for their P4 implementation."""
    return tuple(name for name in LINKABLE if name not in CONNECTORS)


def label_for(name):
    """The untranslated label for a section, connector-provided or registry."""
    connector = CONNECTORS.get(name)
    if connector is not None and connector.label:
        return connector.label
    return profile_registry.get(name).label

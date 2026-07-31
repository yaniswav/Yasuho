"""Purpose: THE registry of profile fields - which names exist, what a valid
value looks like, and how big it may get.

This module is pure (no bot, no database, no discord) and is the single source
of truth every other half of the chantier reads: storage.py validates through it
before any write, visibility.py resolves through it, and the P2 panel / P3-P4
connectors add a line here instead of a column in schema.sql.

Two kinds of entry live side by side:

* STORED fields - the socle written by this lot into ``user_profiles``. Each one
  names its column, so SQL never has to guess (and the column identifier is
  never user input).
* CONNECTOR sections - names that are valid VISIBILITY targets today but have no
  storage here (``anilist``, ``steam``, ``lastfm``, ...). A user can already be
  told "this section is private"; P3/P4 fill in where the data comes from. They
  are refused by ``set_field`` with a typed error rather than silently ignored.

The two namespaces (gamer-ID KEYS and field/section NAMES) must not overlap:
they are routed by the same user-typed word in the cog, so a shared name would
be ambiguous. Hence the Steam friend code is keyed ``steam_id`` and the linked
account section is ``steam``.

Caps live here as module constants, not as magic numbers at call sites, because
three writers (this cog, the P2 panel and the Node dashboard) must agree on
them. ``schema.sql`` mirrors the hard ones as CHECK constraints - the belt to
this module's suspenders when a second writer forgets to ask.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cogs.community.leveling.rank_card import InvalidAccent, validate_accent
from tools.i18n import N_

# ---------------------------------------------------------------------------
# Caps. Changing one is a product decision, so it is made in exactly one place.
# ---------------------------------------------------------------------------

BIO_MAX = 300
PRONOUNS_MAX = 40
CUSTOM_FIELDS_MAX = 5
CUSTOM_LABEL_MAX = 30
CUSTOM_VALUE_MAX = 100

# Inherited verbatim from the legacy `profiles` cog (its `profile set` refused
# anything over 1000 chars). The migration copies existing rows as-is, so the cap
# stays exactly where it was: no stored value can become unre-settable, and no
# friend code is silently truncated on the way into the new table.
GAMING_ID_MAX = 1000

# The gamer-ID keys the legacy table carried, in display order. These are KEYS
# inside the `gaming_ids` mapping, not field names of their own: visibility is
# decided for the whole `gaming_ids` section at once.
#
# The Steam key is `steam_id`, NOT `steam`: `steam` is already a connector
# SECTION below (the P3 linked account), and the two namespaces meet in
# `profile set` / `section_for()`, where one name has to route to exactly one
# thing. A typed friend code and a linked account are different data with
# different visibility, so they get different names. The class of bug is guarded
# by a test: no gamer-ID key may equal a registry field name.
GAMING_ID_KEYS = ("switch", "3ds", "battletag", "riot", "steam_id")

GAMING_ID_LABELS = {
    "switch": N_("Switch Friend Code"),
    "3ds": N_("3DS Friend Code"),
    "battletag": N_("BattleTag"),
    "riot": N_("Riot ID"),
    "steam_id": N_("Steam ID"),
}


# ---------------------------------------------------------------------------
# Typed errors, so callers map a failure to their own message without string
# matching (the same posture as cogs/community/leveling/rank_card.py).
# ---------------------------------------------------------------------------


class ProfileFieldError(ValueError):
    """Base class for every registry rejection."""


class UnknownField(ProfileFieldError):
    """The name is not in the registry at all (typo, or a newer version's field)."""

    def __init__(self, name):
        super().__init__(f"unknown profile field: {name!r}")
        self.name = name


class FieldNotStored(ProfileFieldError):
    """A real field, but one this lot does not persist (a P3/P4 connector)."""

    def __init__(self, name):
        super().__init__(f"profile field {name!r} has no storage in this version")
        self.name = name


class InvalidValue(ProfileFieldError):
    """The value is refused; ``reason`` and ``limit`` drive the user message."""

    def __init__(self, name, reason, limit=None):
        super().__init__(f"invalid value for profile field {name!r}: {reason}")
        self.name = name
        self.reason = reason
        self.limit = limit


# ---------------------------------------------------------------------------
# Validators. Each takes the field it belongs to, so the error carries the name.
# ---------------------------------------------------------------------------


def _clean_text(field, value, limit):
    """Strip, refuse non-strings, and map "emptied" to None."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    else:
        raise InvalidValue(field.name, "type")
    if not text:
        # An emptied box clears the field rather than storing "".
        return None
    if len(text) > limit:
        raise InvalidValue(field.name, "too_long", limit)
    return text


def _text(field, value):
    return _clean_text(field, value, field.max_length)


def _colour(field, value):
    """Reuse the rank-card colour parser so #FFF means the same thing everywhere."""
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return validate_accent(value)
    except InvalidAccent as exc:
        raise InvalidValue(field.name, "colour") from exc


def _pairs(field, value):
    """Normalise free-form label/value pairs into a bounded list of dicts."""
    if value is None:
        return []
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, (list, tuple)):
        items = []
        for entry in value:
            if isinstance(entry, dict):
                items.append((entry.get("label"), entry.get("value")))
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                items.append((entry[0], entry[1]))
            else:
                raise InvalidValue(field.name, "type")
    else:
        raise InvalidValue(field.name, "type")

    pairs = []
    for label, text in items:
        label = _clean_text(field, label, CUSTOM_LABEL_MAX)
        text = _clean_text(field, text, CUSTOM_VALUE_MAX)
        if label is None or text is None:
            # A half-filled row is dropped, not stored as a blank line.
            continue
        pairs.append({"label": label, "value": text})
    if len(pairs) > CUSTOM_FIELDS_MAX:
        raise InvalidValue(field.name, "too_many", CUSTOM_FIELDS_MAX)
    return pairs


def _gaming_ids(field, value):
    """Normalise the gamer-ID mapping: whitelisted keys, capped values."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidValue(field.name, "type")
    if set(value) - set(GAMING_ID_KEYS):
        raise InvalidValue(field.name, "unknown_key")
    ids = {}
    for key in GAMING_ID_KEYS:
        if key not in value:
            continue
        text = _clean_text(field, value[key], GAMING_ID_MAX)
        if text is not None:
            ids[key] = text
    return ids


def _not_stored(field, value):
    raise FieldNotStored(field.name)


# ---------------------------------------------------------------------------
# The registry itself.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One addressable profile field: its storage, its label and its validator."""

    name: str
    label: str
    kind: str
    validator: Callable
    column: str | None = None
    json_column: bool = False
    max_length: int | None = None

    @property
    def stored(self):
        """True when this lot persists the field in ``user_profiles``."""
        return self.column is not None

    def normalise(self, value):
        """Return the storable form of ``value`` or raise a typed error."""
        return self.validator(self, value)


FIELDS = (
    Field("bio", N_("Bio"), "text", _text, column="bio", max_length=BIO_MAX),
    Field(
        "pronouns",
        N_("Pronouns"),
        "text",
        _text,
        column="pronouns",
        max_length=PRONOUNS_MAX,
    ),
    Field("accent", N_("Accent colour"), "colour", _colour, column="accent"),
    Field(
        "custom_fields",
        N_("Custom fields"),
        "pairs",
        _pairs,
        column="custom_fields",
        json_column=True,
    ),
    Field(
        "gaming_ids",
        N_("Gaming IDs"),
        "mapping",
        _gaming_ids,
        column="gaming_ids",
        json_column=True,
    ),
    # Connector sections: valid visibility targets now, storage lands in P3/P4.
    Field("anilist", N_("AniList"), "connector", _not_stored),
    Field("steam", N_("Steam"), "connector", _not_stored),
    Field("lastfm", N_("Last.fm"), "connector", _not_stored),
    Field("osu", N_("osu!"), "connector", _not_stored),
    Field("backloggd", N_("Backloggd"), "connector", _not_stored),
    # "Recently played", NOT "Now playing": the section is a CUMULATIVE history
    # (a top-N of minutes per game, collected over weeks), and the live game is
    # only its optional first line when there happens to be one. The label is
    # shared - it names the section on the card, in the visibility panel and in
    # every "X is now visible to ..." answer - so it has to be true in the case
    # where nothing is playing, which is most of the time.
    Field("presence_gaming", N_("Recently played"), "connector", _not_stored),
    Field("spotify_presence", N_("Spotify"), "connector", _not_stored),
)

BY_NAME = {field.name: field for field in FIELDS}
FIELD_NAMES = tuple(field.name for field in FIELDS)
STORED_FIELDS = tuple(field for field in FIELDS if field.stored)
STORED_NAMES = tuple(field.name for field in STORED_FIELDS)
COLUMNS = {field.name: field.column for field in STORED_FIELDS}


def is_known(name):
    """True when ``name`` is an addressable field or section of this version."""
    return name in BY_NAME


def get(name):
    """Return the :class:`Field` called ``name`` or raise :class:`UnknownField`."""
    try:
        return BY_NAME[name]
    except KeyError:
        raise UnknownField(name) from None


def stored_field(name):
    """Return a field this lot can WRITE, or raise a typed error explaining why not."""
    field = get(name)
    if not field.stored:
        raise FieldNotStored(name)
    return field


def normalise(name, value):
    """Validate ``value`` for the stored field ``name`` and return its stored form."""
    return stored_field(name).normalise(value)

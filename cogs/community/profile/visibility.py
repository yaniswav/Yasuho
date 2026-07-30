"""Purpose: decide WHO may see WHICH profile field - the pure rule engine.

Three levels, chosen per field by its owner:

* ``public``  - anyone, including a viewer the bot shares nothing with;
* ``server``  - members of a server the owner is also in;
* ``private`` - the owner alone.

The load-bearing invariant: an ABSENT row means ``private``. The default is
never materialised in the database, so "the user never decided" and "the user
chose private" are the same state and both fail closed. A profile therefore
starts fully invisible and only what the owner explicitly turned on ever leaves.

Forward compatibility is the second invariant: a field the running code does not
know (a P4 connector written by a newer deploy, or an old row left after a
rename) is IGNORED, never shown. Unknown means invisible, both ways round -
unknown keys in the profile and unknown keys in the visibility map.

Pure module: no bot, no database, no discord. The caller supplies a
:class:`ViewerContext` describing who is looking; computing ``shares_guild`` is
the caller's job precisely so this stays testable and free.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import registry

PUBLIC = "public"
SERVER = "server"
PRIVATE = "private"

# Ordered from most open to most closed; mirrored by the CHECK on
# profile_visibility.level in schema.sql.
LEVELS = (PUBLIC, SERVER, PRIVATE)
DEFAULT_LEVEL = PRIVATE


class InvalidLevel(ValueError):
    """The level is not one of :data:`LEVELS`."""

    def __init__(self, level):
        super().__init__(f"invalid visibility level: {level!r}")
        self.level = level


def normalise_level(level):
    """Return a canonical level string, or raise :class:`InvalidLevel`."""
    if not isinstance(level, str):
        raise InvalidLevel(level)
    candidate = level.strip().lower()
    if candidate not in LEVELS:
        raise InvalidLevel(level)
    return candidate


@dataclass(frozen=True)
class ViewerContext:
    """Who is looking at whose profile, and whether they share a server.

    ``viewer_id`` is None for an anonymous read (a future public dashboard page),
    which can only ever see ``public`` fields. ``shares_guild`` is supplied by
    the caller: the cog knows it in O(1) from the invocation guild, and a global
    mutual-guild scan is deliberately NOT done here (it would be O(guilds) per
    profile view).
    """

    owner_id: int
    viewer_id: int | None = None
    shares_guild: bool = False

    @property
    def is_owner(self):
        return self.viewer_id is not None and self.viewer_id == self.owner_id


def level_for(visibility_map, name):
    """The level chosen for ``name``; an absent (or unreadable) row is private."""
    if not visibility_map:
        return DEFAULT_LEVEL
    level = visibility_map.get(name)
    if level not in LEVELS:
        # Includes None (no row) and any junk a future/rolled-back writer left.
        return DEFAULT_LEVEL
    return level


def can_view(level, viewer):
    """True when ``viewer`` may see a field published at ``level``."""
    if viewer.is_owner:
        return True
    if level == PUBLIC:
        return True
    if level == SERVER:
        return bool(viewer.shares_guild)
    return False


def _is_empty(value):
    """True for "nothing to show". Note accent 0 (black) is a REAL value."""
    if value is None:
        return True
    return isinstance(value, (str, bytes, list, tuple, dict, set)) and not value


def resolve_visible_fields(profile, visibility_map, viewer):
    """Return the subset of ``profile`` that ``viewer`` is allowed to see.

    Iterates the registry rather than the profile mapping, so the result is in a
    stable display order and any key the registry does not know (metadata such
    as ``user_id``/``updated_at``, or a newer version's field) is dropped. Empty
    values are dropped too: an unset field is not a hidden one.
    """
    visible = {}
    if not profile:
        return visible
    for field in registry.FIELDS:
        if field.name not in profile:
            continue
        value = profile[field.name]
        if _is_empty(value):
            continue
        if not can_view(level_for(visibility_map, field.name), viewer):
            continue
        visible[field.name] = value
    return visible


def visible_field_names(visibility_map, viewer):
    """Names the viewer is allowed to see, regardless of whether they are set.

    The P2 panel needs this to render "shown / hidden" toggles for sections whose
    data lives elsewhere (the P3/P4 connectors), where there is no stored value
    to hand to :func:`resolve_visible_fields`.
    """
    return tuple(
        field.name
        for field in registry.FIELDS
        if can_view(level_for(visibility_map, field.name), viewer)
    )

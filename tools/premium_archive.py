"""Shared LAZY archival helper (M4a-1: .claude/plans/monetisation/4-plan-retenu.md,
"Expiration / remboursement / revocation").

THE RULE (one line). When a guild or user's usage of some capped resource
(server playlists today; the same shape will fit menus, hubs, feeds, ... in a
later lot) exceeds the CURRENT effective limit - because a subscription
ended, was refunded, or was revoked - the excess is never deleted and never
silently truncated. It becomes ARCHIVED: still visible, still exportable
where an export exists, still deletable, but not usable (not loadable, not
editable) until either the limit rises again (Yasuho+ comes back) or the
excess is deleted by hand. The plan's own words: "l'admin choisit ce qui
reste dans les emplacements gratuits, sinon les plus anciens (ordre stable)".

LAZY, ON PURPOSE. There is NO stored ``archived`` column anywhere and NO
background job that sweeps rows the moment a subscription lapses. This
module computes the verdict at USE time, from the resource's own creation
order and whatever :func:`tools.premium.EntitlementCache.for_guild`/
``.for_user`` says the limit is RIGHT NOW. That is what makes expiry and
reinstatement symmetric with zero extra bookkeeping: the exact instant
Yasuho+ lapses, the next read already reports the excess archived (nothing
to react to); the exact instant it comes back (a renewal, a reconciliation
pass catching up, an owner grant), the next read reports it active again -
no "unarchive" step, nothing to catch up, because nothing was ever written
down in the first place.

Pure and synchronous: NO I/O, no cogs import (tools/ must not import cogs -
see tools/premium.py's own module docstring for why), no knowledge of what
kind of resource it is classifying. A caller fetches its own rows (bounded by
the SAFETY CEILING for that resource - see tools.premium.GUILD_CEILINGS/
USER_CEILINGS - never unbounded, since a stored row count can only ever have
grown under a cap that was itself clamped to that ceiling), reads the
EFFECTIVE limit from :data:`tools.premium.premium_limits` (or ``bot.premium``)
itself, and calls :func:`classify` with both.

THE ORDER (kept first, then oldest, ties by id). Every resource is read via
:func:`_field` as ``id`` (hashable, used only as an opaque key - an int
surrogate key where one exists, a string natural key like a casefolded name
where it does not), ``created_at`` (anything comparable: a
:class:`datetime.datetime`, an int sequence number, ...) and an optional
``kept`` flag (falsy by default - no caller sets it true yet; it exists for a
FUTURE admin choice of which resources ride out a downgrade in the free
slots, exactly as the plan's own prose promises, without this module's shape
needing to change the day that admin surface ships). Resources are ordered
``kept`` first (so an admin's pick always wins a free slot over something
merely older), then oldest ``created_at`` first within each group, then by
``id`` ascending to break an exact tie deterministically regardless of the
order ``resources`` arrived in or how the caller's own query sorted them.
The first ``limit`` resources in that order are ACTIVE; everything past it is
ARCHIVED - including a ``kept`` resource, if more are kept than the limit
allows: "kept" wins a free slot over "merely older", not over the limit
itself.
"""

from __future__ import annotations

import dataclasses
import typing

_MISSING = object()


def _field(resource: typing.Any, name: str, default: typing.Any = _MISSING) -> typing.Any:
    """Read one field off ``resource``: mapping access first (a dict, an
    asyncpg.Record), then attribute access (a dataclass, a SimpleNamespace, an
    ORM row) - the same two-step :func:`tools.premium._get` uses, so a caller
    can hand this whatever row shape it already has. Raises ``KeyError`` when
    the field is missing and no ``default`` was given - :func:`classify`
    always supplies one for the optional ``kept`` field, and relies on this
    raising for ``id``/``created_at`` (a caller bug, not a value to paper
    over: an uncomparable/missing creation order breaks the whole point of
    this module)."""
    try:
        return resource[name]
    except (TypeError, KeyError, IndexError):
        pass
    value = getattr(resource, name, default)
    if value is _MISSING:
        raise KeyError(name)
    return value


@dataclasses.dataclass(frozen=True)
class ArchivalResult:
    """The verdict from one :func:`classify` call: which resource ids are
    ACTIVE and which are ARCHIVED. The two sets always partition the input -
    every id handed to :func:`classify` lands in exactly one of them."""

    active_ids: typing.FrozenSet[typing.Any]
    archived_ids: typing.FrozenSet[typing.Any]

    def is_active(self, resource_id: typing.Any) -> bool:
        """True when ``resource_id`` is in the ACTIVE set.

        An id :func:`classify` never saw at all (a caller's bug - it must
        feed this EVERY resource in the scope it is classifying, or the
        ordering/limit math is against an incomplete picture) reads as
        ``False`` here, the fail-closed direction: "not known active" is the
        safe default for a resource this verdict was never asked about.
        """
        return resource_id in self.active_ids

    def is_archived(self, resource_id: typing.Any) -> bool:
        """True when ``resource_id`` is in the ARCHIVED set (see
        :meth:`is_active` for the fail-closed default on an unseen id - here
        that same unseen id reads as ``False`` too, so the two methods only
        ever agree on a resource this call actually classified)."""
        return resource_id in self.archived_ids


def classify(resources: typing.Iterable[typing.Any], limit: int) -> ArchivalResult:
    """Partition ``resources`` into ACTIVE (the first ``limit``, in the
    kept-first/oldest-first/id-tiebreak order - see the module docstring) and
    ARCHIVED (the rest).

    ``limit`` <= 0 archives everything (every call site clamps its own
    GuildLimits/UserLimits field to a non-negative safety ceiling already -
    see tools/premium.py - so this is a defensive floor, not a path any real
    caller exercises); ``limit`` >= the number of resources keeps everything
    active. Every resource must carry a distinct ``id`` - this is the
    contract every call site already provides (a per-guild/per-user primary
    key), not something this function can check for free without paying for
    a second pass over possibly-unhashable ids.
    """
    ordered = sorted(
        resources,
        key=lambda resource: (
            not bool(_field(resource, "kept", False)),
            _field(resource, "created_at"),
            _field(resource, "id"),
        ),
    )
    ordered_ids = [_field(resource, "id") for resource in ordered]
    cutoff = max(int(limit), 0)
    return ArchivalResult(
        active_ids=frozenset(ordered_ids[:cutoff]),
        archived_ids=frozenset(ordered_ids[cutoff:]),
    )

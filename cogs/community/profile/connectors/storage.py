"""Purpose: the only door to ``profile_connections`` - link one account, unlink
one account, read them, refresh one display cache.

Every statement is mono-statement (asyncpg refuses anything else for a
parameterized query) and touches ONE (user_id, connector) row, so two connectors
refreshing at the same moment cannot clobber each other. There is no
read-modify-write anywhere: linking is a single upsert and refreshing is a
single UPDATE.

Two invariants are load-bearing enough to spell out:

* ``set_payload`` is an UPDATE, never an upsert. A refresh that lands after the
  user unlinked must write nothing - an upsert would resurrect the row (and the
  handle) that the user just deleted. The absent row raises
  :class:`~.base.NotLinked` so the caller can drop the account from its queue.
* ``unlink`` deletes the row AND the section's visibility line, in one
  transaction. A published-but-empty section is not neutral: it is a promise the
  card cannot keep, and, more importantly, re-linking later would silently
  re-expose data at a level the user chose months ago for a different account.
  The visibility delete goes through the PARENT ``set_visibility(..., private)``
  rather than a second copy of the SQL, so the "private is an absent row"
  invariant has exactly one implementation.

Two whitelists, not one. ``link`` is guarded by :data:`~.base.LINKABLE` (the
five services a user types a handle for); ``set_marker`` is guarded by
:data:`~.base.PRESENCE_SECTIONS` (the two fed by Discord presence, where the
row IS the consent and there is nothing to type); and the writes that are
neither - ``unlink`` and ``set_payload`` - accept any reserved
:data:`~.base.SECTIONS` name, because both kinds of row live in the same table
and must be un-published and refreshed by the same code. That widening opens no
user-facing door: the connectors cog checks ``LINKABLE`` itself before it calls
either one, and its slash Literal offers only the five.

Validation happens BEFORE the statement, through :mod:`base`, so a rejected
handle or an oversized payload never reaches Postgres and the caller gets a
typed error instead of an asyncpg constraint violation.

Deletion of EVERYTHING (the user-forget path) is not here: it lives with the
other profile tables in ``tools.privacy.PROFILE_DELETE_QUERIES``, so /mydata and
this package cannot drift.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import json

from .. import storage as profile_storage
from .. import visibility
from . import base
from tools.db import affected_rows

_COLUMNS = (
    "connector, external_id, display_name, linked_at, last_refresh, payload"
)

# ``xmax = 0`` is true only for a row this statement INSERTed; an ON CONFLICT
# update leaves the deleting-transaction id behind. It is the cheapest way to
# tell "linked" from "re-linked" without a second round trip, and it was probed
# against the real local Postgres.
_LINK = (
    "INSERT INTO profile_connections "
    "(user_id, connector, external_id, display_name, payload) "
    "VALUES ($1, $2, $3, $4, $5::jsonb) "
    "ON CONFLICT (user_id, connector) DO UPDATE SET "
    "external_id = EXCLUDED.external_id, "
    "display_name = EXCLUDED.display_name, "
    "payload = EXCLUDED.payload, "
    "linked_at = now(), "
    "last_refresh = NULL "
    f"RETURNING {_COLUMNS}, (xmax = 0) AS created"
)

# The PRESENCE opt-in marker. Same table, same one statement, one deliberate
# difference from _LINK above: the conflict path touches ``external_id`` and
# NOTHING else. A presence section has no handle to replace, so the only thing
# a second `/profile presence gaming on` could do by reusing _LINK is wipe the
# payload the collector spent weeks filling - an opt-in that silently deletes
# data is a bug, not an idempotent write. ``linked_at`` is left alone for the
# same reason (it is when the user consented, not when they last re-said so),
# and ``last_refresh`` too, so the age of the aggregate stays truthful.
#
# ``xmax = 0`` still tells a first opt-in from a repeat one (see _LINK), which
# is what lets the cog answer "already on" without a second round trip. Probed
# against the real local Postgres in a rolled-back transaction: the repeat
# insert returns created = false AND the previously stored payload intact.
_SET_MARKER = (
    "INSERT INTO profile_connections (user_id, connector, external_id) "
    "VALUES ($1, $2, $3) "
    "ON CONFLICT (user_id, connector) DO UPDATE SET "
    "external_id = EXCLUDED.external_id "
    f"RETURNING {_COLUMNS}, (xmax = 0) AS created"
)


def _decode_payload(value):
    """asyncpg hands JSONB back as text (no codec is registered on the pool)."""
    if value is None:
        return {}
    if isinstance(value, (str, bytes, bytearray)):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
    else:
        decoded = value
    # A hand-edited row (or a future writer) can hold a list or a scalar; the
    # card indexes the payload by key, so anything else degrades to empty
    # rather than crashing the profile of whoever owns it.
    return decoded if isinstance(decoded, dict) else {}


def _row_to_connection(row):
    """Plain dict, with the payload decoded. Never returns the raw Record."""
    connection = dict(row)
    connection["payload"] = _decode_payload(connection.get("payload"))
    return connection


async def link(pool, user_id, connector, result):
    """Store (or replace) one linked account; return the stored row as a dict.

    The dict carries ``created``: True for a first link, False when the user
    re-linked the same connector to another handle. Re-linking resets
    ``last_refresh`` to NULL in the same statement, because the cached payload
    now describes somebody else's account and must be refetched before it is
    shown again.

    Raises :class:`~.base.UnknownConnector` or :class:`~.base.InvalidHandle`
    before touching the database.
    """
    if connector not in base.LINKABLE:
        raise base.UnknownConnector(connector)
    checked = base.validate_link_result(connector, result)
    row = await pool.fetchrow(
        _LINK,
        user_id,
        connector,
        checked.external_id,
        checked.display_name,
        base.encode_payload(connector, checked.payload),
    )
    return _row_to_connection(row)


async def set_marker(pool, user_id, connector, external_id=None):
    """Store (or confirm) the OPT-IN marker of a PRESENCE section.

    Presence sections are not handle-linkable (see
    :data:`~.base.PRESENCE_SECTIONS`): there is nothing for the user to type,
    so the row itself is the consent and ``external_id`` defaults to the
    owner's own Discord id - the only identifier this kind of section is keyed
    by. The returned dict carries ``created``, False when the user was already
    opted in.

    IDEMPOTENT ON THE PAYLOAD, which is the whole reason this is not
    :func:`link`: saying "on" twice must never erase the aggregate the
    collector has been filling. See :data:`_SET_MARKER`.
    """
    if connector not in base.PRESENCE_SECTIONS:
        raise base.UnknownConnector(connector)
    handle = str(external_id if external_id is not None else user_id)
    if not handle or len(handle) > base.EXTERNAL_ID_MAX:
        raise base.InvalidHandle(connector, "too_long", base.EXTERNAL_ID_MAX)
    row = await pool.fetchrow(_SET_MARKER, user_id, connector, handle)
    return _row_to_connection(row)


async def get_opted_users(pool, connector):
    """Every user id that has a row for ``connector``, as a set.

    The one read behind a presence collector's in-memory opt-in set: the hot
    listener answers "is this user opted in" from that set in O(1) with no
    await at all, and this is what fills it once at cog load.

    ``profile_connections_refresh_idx`` leads on ``connector``, so this is the
    query it serves. Verified with EXPLAIN against the real local Postgres: a
    bitmap index scan while the section is a minority of the table (which it
    is - opting in is a deliberate act), and the planner correctly falls back
    to a sequential scan once it is not, because reading half a table through
    an index is slower than reading it. Either way this runs ONCE, at cog
    load, never on an event path.
    """
    if connector not in base.SECTIONS:
        raise base.UnknownConnector(connector)
    rows = await pool.fetch(
        "SELECT user_id FROM profile_connections WHERE connector = $1",
        connector,
    )
    return {row["user_id"] for row in rows}


async def unlink(pool, user_id, connector):
    """Delete one linked account and un-publish its section; return whether a
    row was actually removed.

    Both statements run in one transaction: a section can never stay published
    with no connection behind it, nor the reverse.

    Accepts any reserved section, presence included: opting OUT of presence
    sharing has to delete the marker AND the visibility line, and that is
    exactly what this already does. See the module docstring on the two
    whitelists.
    """
    if connector not in base.SECTIONS:
        raise base.UnknownConnector(connector)
    async with pool.acquire() as connection:
        async with connection.transaction():
            status = await connection.execute(
                "DELETE FROM profile_connections "
                "WHERE user_id = $1 AND connector = $2",
                user_id,
                connector,
            )
            # The parent seam takes any executor (pool or connection): it issues
            # exactly one statement. Reusing it here means "private = no row"
            # is implemented once, in the module that owns that invariant.
            await profile_storage.set_visibility(
                connection, user_id, connector, visibility.PRIVATE
            )
    return affected_rows(status) > 0


async def get_connection(pool, user_id, connector):
    """Return ONE linked account as a dict, or None when it is not linked."""
    row = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM profile_connections "
        "WHERE user_id = $1 AND connector = $2",
        user_id,
        connector,
    )
    if row is None:
        return None
    return _row_to_connection(row)


async def get_connections(pool, user_id):
    """Return every linked account of one user, in a stable order.

    Bounded by construction: the primary key is (user_id, connector) and
    ``connector`` is a seven-value CHECK, so this can never return more than
    seven rows however hostile the caller.
    """
    rows = await pool.fetch(
        f"SELECT {_COLUMNS} FROM profile_connections "
        "WHERE user_id = $1 ORDER BY connector",
        user_id,
    )
    return [_row_to_connection(row) for row in rows]


async def get_payloads(pool, connector, user_ids):
    """``{user_id: payload}`` for one connector and a BOUNDED list of users.

    The batched read a collector's periodic flush opens with: it turns "do
    these N people still have a row, and what is in it" into ONE statement
    instead of N, and an absent key is how the caller learns somebody opted
    out while the interval was accumulating.

    The caller is responsible for keeping ``user_ids`` bounded (the presence
    flush caps it per tick) - this is a point read over a composite primary
    key, not a scan, but an unbounded array parameter would still be an
    unbounded round trip.
    """
    if connector not in base.SECTIONS:
        raise base.UnknownConnector(connector)
    ids = [int(user_id) for user_id in user_ids or ()]
    if not ids:
        return {}
    rows = await pool.fetch(
        "SELECT user_id, payload FROM profile_connections "
        "WHERE connector = $1 AND user_id = ANY($2::bigint[])",
        connector,
        ids,
    )
    return {row["user_id"]: _decode_payload(row["payload"]) for row in rows}


async def set_payload(pool, user_id, connector, payload, display_name=None):
    """Refresh ONE display cache; stamp ``last_refresh``.

    UPDATE, never upsert - see the module docstring. ``display_name`` is only
    overwritten when the caller passes one, so a connector that cannot resolve
    the pretty name this time does not erase the one already shown.

    Raises :class:`~.base.NotLinked` when the user has no such connection, and
    :class:`~.base.InvalidPayload` before any SQL when the payload is not a
    bounded JSON object.

    Accepts any reserved section, presence included: the presence collector's
    batched flush writes its aggregate through this exact seam, and NotLinked
    is precisely how it learns a user opted out while the interval was
    accumulating. See the module docstring on the two whitelists.
    """
    if connector not in base.SECTIONS:
        raise base.UnknownConnector(connector)
    encoded = base.encode_payload(connector, payload)
    if isinstance(display_name, str):
        display_name = display_name.strip()[: base.DISPLAY_NAME_MAX] or None
    status = await pool.execute(
        "UPDATE profile_connections SET payload = $3::jsonb, "
        "display_name = COALESCE($4, display_name), last_refresh = now() "
        "WHERE user_id = $1 AND connector = $2",
        user_id,
        connector,
        encoded,
        display_name,
    )
    if affected_rows(status) < 1:
        raise base.NotLinked(connector)
    return True

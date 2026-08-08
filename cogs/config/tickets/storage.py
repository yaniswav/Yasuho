"""The only door to the ``tickets`` table: open one, count a member's open ones,
read one back by its thread.

METADATA ONLY. Nothing in this module writes a subject, a transcript or any
message text, and there is no column that could hold one (see schema.sql). The
conversation lives in the Discord thread and dies with it.

The one interesting statement is :data:`_OPEN_TICKET`. It does three things in a
single round trip, and it has to, because each of them is a race the naive
version loses:

* the per-guild human NUMBER is ``MAX(ticket_number) + 1`` computed INSIDE the
  INSERT (the ``cases`` precedent, cogs/moderation/modactions.create_case);
* the per-user CAP is a ``WHERE (SELECT COUNT(*) ...) < $cap`` guard on the same
  statement (the ``guild_playlists`` precedent,
  cogs/music/playlists_shared._save_guild_playlist), so "am I under the cap" and
  "take a slot" cannot be separated by a click;
* a lost race is a clean answer, not an exception: the INSERT simply inserts no
  row and :func:`open_ticket` returns ``None``, which the caller renders as
  "you already have the maximum number of tickets open".

Why that is actually atomic. Under READ COMMITTED, two simultaneous statements
CAN both see the same ``COUNT(*)``, so the cap guard alone would not be enough.
What closes it is the number: both also compute the same ``MAX + 1``, so the
second blocks on ``UNIQUE (guild_id, ticket_number)`` until the first finishes
and then fails with a unique violation - at which point the bounded retry below
re-runs the WHOLE statement, recomputing the count against the winner's now
committed row and refusing. Probed: six simultaneous opens by one member at cap
2 create exactly two rows, and twenty two-way races at cap 1 never created two.

The retry budget is a CONCURRENCY ceiling, not a safety margin. Simultaneous
opens in one guild serialise on the number, so the k-th finisher needs k
attempts: a budget of B means the (B + 1)-th concurrent open in a guild RAISES,
and the caller answers "something went wrong" to a member who did nothing wrong.
Measured on PostgreSQL - with a budget of 5, 6-way contention already produced
one raise and 16-way produced eleven; at 20, every 8/12/16/20-way run landed
cleanly. Hence :data:`OPEN_RETRIES` below, which is sized for the herd a public
button can produce during an incident rather than for a moderator's typing speed
(the ``cases`` precedent's 5 is behind a moderator command, which is why it can
be that small).

Retries are scoped to the number collision. A violation of
``tickets_thread_id_key`` means the caller passed a thread that is already a
ticket - retrying that would loop until the budget ran out and then raise the
same error anyway, so it is re-raised immediately.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)

# How many times a ticket-number collision is re-tried. Each retry recomputes
# MAX + 1, so it only ever loses to a NEW concurrent open - which makes this the
# number of members who may click Open at the same instant in one guild before
# somebody is told "something went wrong". A public button during an incident can
# easily produce a dozen, so the budget is sized well past that while still
# keeping a pathological loop bounded (each attempt is one short indexed
# statement, so the worst case is bounded work, not an open-ended spin).
OPEN_RETRIES = 20

# The two unique constraints on the table (schema.sql). Named rather than
# matched on message text so the retry can tell "somebody took my number"
# (retryable) from "this thread is already a ticket" (never retryable).
NUMBER_CONSTRAINT = "tickets_guild_id_ticket_number_key"
THREAD_CONSTRAINT = "tickets_thread_id_key"

_OPEN_TICKET = (
    "INSERT INTO tickets (guild_id, ticket_number, thread_id, opener_id) "
    "SELECT $1, "
    "(SELECT COALESCE(MAX(ticket_number), 0) + 1 FROM tickets "
    "WHERE guild_id = $1), "
    "$2, $3 "
    "WHERE (SELECT COUNT(*) FROM tickets "
    "WHERE guild_id = $1 AND opener_id = $3 AND status = 'open') < $4 "
    "RETURNING ticket_number"
)

_COUNT_OPEN_FOR_USER = (
    "SELECT COUNT(*) FROM tickets "
    "WHERE guild_id = $1 AND opener_id = $2 AND status = 'open'"
)

_BY_THREAD = (
    "SELECT id, guild_id, ticket_number, thread_id, opener_id, status, "
    "opened_at, closed_at, closed_by FROM tickets WHERE thread_id = $1"
)


async def open_ticket(pool, guild_id, thread_id, opener_id, max_open):
    """Record a newly created ticket thread; return its number, or ``None``.

    ``None`` means the member is AT the cap - the only non-exceptional refusal,
    and the authoritative one: the caller's own pre-check is a courtesy that
    keeps a capped member from ever reaching Discord, while this is what makes
    two clicks that both passed that pre-check unable to produce a third ticket.

    A caller that gets ``None`` after already creating the thread must delete it
    (see cogs/config/tickets/open.py) - the row is the record, so a thread with
    no row is not a ticket.
    """
    last_exc = None
    for _attempt in range(OPEN_RETRIES):
        try:
            row = await pool.fetchrow(
                _OPEN_TICKET, guild_id, thread_id, opener_id, max_open
            )
            return row["ticket_number"] if row is not None else None
        except asyncpg.UniqueViolationError as exc:
            if getattr(exc, "constraint_name", None) == THREAD_CONSTRAINT:
                # This thread is already a ticket. Retrying cannot change that.
                raise
            last_exc = exc
            log.debug(
                "tickets: ticket_number collision in guild %s, retrying", guild_id
            )
    raise last_exc


async def count_open_for_user(pool, guild_id, opener_id) -> int:
    """How many tickets this member currently has open in this guild.

    Served by the partial index ``tickets_guild_open_idx`` (probed: index scan on
    guild_id, filter on opener_id), so it stays a few pages even for a guild with
    a long ticket history - closed rows are not in that index at all.
    """
    return int(await pool.fetchval(_COUNT_OPEN_FOR_USER, guild_id, opener_id) or 0)


async def fetch_by_thread(pool, thread_id):
    """The ticket row a thread IS, or ``None`` when that thread is not one.

    The seam lot T2's in-thread controls read: a ``tk:`` button knows its thread
    and nothing else, so this is how it learns which ticket it is acting on (and
    refuses cleanly in a thread that is not a ticket).
    """
    return await pool.fetchrow(_BY_THREAD, thread_id)

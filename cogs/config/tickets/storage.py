"""The only door to the ``tickets`` table: open one, count a member's open ones,
read one back by its thread, claim it, close it, and sweep the stale ones.

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

Lot T2 adds three statements, and each one is a race decided by the DATABASE
rather than by the caller reading first and acting second:

* :func:`claim_ticket` takes the ticket in a single data-modifying CTE whose
  ``WHERE`` carries every rule (still open, not already claimed, not the
  opener's own), and whose fallback branch reads the row's pre-update snapshot -
  so the loser of a two-staff race is TOLD who holds it, from the same round
  trip that refused them;
* :func:`close_ticket` is the whole close flow's mutual exclusion. Its
  ``AND status = 'open'`` means exactly one caller ever gets the row back, so
  two simultaneous Close clicks (or a Close racing the auto-archive listener)
  produce one transcript, one archive and one log line, not two;
* :func:`fetch_sweep_candidates` is the backstop's ONE query per pass: a bounded
  window over the open set in id order from a rotating cursor.

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
    "opened_at, closed_at, closed_by, claimed_by FROM tickets WHERE thread_id = $1"
)

# Take the ticket, in one statement that also EXPLAINS a refusal.
#
# The four rules live in the UPDATE's WHERE, so none of them can be defeated by
# a click that lands between a read and a write: the ticket must still be open,
# nobody may hold it yet, and the opener may not claim their own. When the UPDATE
# matches, the first branch returns the row it just wrote (``taken`` true).
#
# When it does not match, the second branch reads the SAME row - and reads it
# from the statement's snapshot, i.e. as it was before this UPDATE, which is
# exactly the state that refused us: the winner's ``claimed_by``, or
# ``status = 'closed'``, or the ``opener_id`` that is the clicker's own. So the
# caller can say WHICH rule refused without a second round trip. A thread that is
# not a ticket at all matches neither branch and the statement returns no row,
# which is the fourth answer. All four outcomes probed on PostgreSQL.
_CLAIM_TICKET = (
    "WITH claimed AS ("
    "UPDATE tickets SET claimed_by = $2 "
    "WHERE thread_id = $1 AND status = 'open' AND claimed_by IS NULL "
    "AND opener_id <> $2 "
    "RETURNING ticket_number, status, opener_id, claimed_by"
    ") "
    "SELECT ticket_number, status, opener_id, claimed_by, TRUE AS taken "
    "FROM claimed "
    "UNION ALL "
    "SELECT ticket_number, status, opener_id, claimed_by, FALSE AS taken "
    "FROM tickets WHERE thread_id = $1 AND NOT EXISTS (SELECT 1 FROM claimed)"
)

# Close it, and hand the caller everything the log line needs in the same trip.
#
# ``AND status = 'open'`` is the whole feature's exactly-once gate (see the
# module docstring): a second closer gets no row and must therefore do nothing.
# ``closed_by`` is NULL for every close nobody clicked - the auto-archive
# listener, the sweep, a deleted thread - which is what the log summary reads to
# say "auto-closed" instead of naming somebody.
_CLOSE_TICKET = (
    "UPDATE tickets SET status = 'closed', closed_at = now(), closed_by = $2 "
    "WHERE thread_id = $1 AND status = 'open' "
    "RETURNING id, guild_id, ticket_number, thread_id, opener_id, claimed_by, "
    "opened_at, closed_at, closed_by"
)

# One bounded window over the open set, in id order, from a rotating cursor.
#
# ``id > $1`` is the cursor and ``ORDER BY id`` is what makes it advance, so a
# pass that skips its whole batch (every thread still live) does not re-read the
# same rows forever - the next pass starts after them. ``id`` is a BIGSERIAL, so
# ordering by it IS oldest-first without needing a second sort key.
#
# The age cut is the WIDEST any guild could want (the minimum configurable
# window), not the guild's own: the per-guild hours live in a JSONB settings
# blob, and the untrusted-payload rule says that value is coerced in Python
# (guild_config), never cast inside SQL where a dashboard bug would raise. So
# this is the cheap first cut and the caller applies each guild's real window.
#
# Served by the partial ``tickets_open_sweep_idx`` (schema.sql). Probed: 50 rows
# out of 2300 (2000 of them closed) is an index scan touching 2 shared buffers.
_SWEEP_CANDIDATES = (
    "SELECT id, guild_id, ticket_number, thread_id, opener_id, claimed_by, "
    "opened_at FROM tickets "
    "WHERE status = 'open' AND id > $1 "
    "AND opened_at < now() - make_interval(hours => $2) "
    "ORDER BY id LIMIT $3"
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


async def claim_ticket(pool, thread_id, claimer_id):
    """Take this ticket for ``claimer_id``; return the outcome row, or ``None``.

    ``None`` means the thread is not a ticket. Otherwise the row's ``taken``
    says whether the claim landed, and when it did not the other columns say
    why: ``status`` is not ``'open'`` (already closed), ``claimed_by`` is
    somebody else (they got there first), or ``opener_id`` is the clicker (you
    do not claim your own ticket). Exactly one round trip either way.
    """
    return await pool.fetchrow(_CLAIM_TICKET, thread_id, claimer_id)


async def close_ticket(pool, thread_id, closed_by):
    """Close this ticket; return its row, or ``None`` if it was already closed.

    THE gate for everything the close flow does afterwards. Only the caller that
    gets a row back may write the transcript to the log channel, archive the
    thread and post the summary - so a Close click that races another Close (or
    races the auto-archive listener reacting to our own archive) does its work
    exactly once. ``closed_by`` is ``None`` for every close no human clicked.
    """
    return await pool.fetchrow(_CLOSE_TICKET, thread_id, closed_by)


async def fetch_sweep_candidates(pool, *, after_id, min_age_hours, limit):
    """One bounded, ordered window over the open set for the inactivity sweep.

    ``after_id`` is the caller's rotating cursor (0 restarts the scan) and
    ``min_age_hours`` is the widest age cut - the caller still has to apply each
    guild's own inactivity window, because that value is untrusted JSONB and is
    coerced in Python, never inside this statement.
    """
    return await pool.fetch(_SWEEP_CANDIDATES, after_id, min_age_hours, limit)

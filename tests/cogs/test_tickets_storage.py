"""Unit tests for ``cogs.config.tickets.storage`` (lot T1).

Three statements, and one of them carries the whole safety story of the feature:
:func:`storage.open_ticket` computes the per-guild ticket NUMBER as MAX + 1
inside the INSERT, guards the per-user CAP in the same statement, and retries a
number collision (the ``cases`` + ``guild_playlists`` precedents fused).

These are shape-and-behaviour tests against a scripted pool: what the statement
says, how many times it is re-issued, and which refusal is exceptional. The
statement's actual atomicity was verified against PostgreSQL - six simultaneous
opens by one member at cap 2 create exactly two rows - which no in-memory fake
can prove.
"""

import asyncpg
import pytest

from cogs.config.tickets import storage

GUILD_ID = 909
OPENER_ID = 77


class _ScriptedPool:
    """A pool whose ``fetchrow`` replays a scripted list of outcomes.

    Each entry is either a value to return or an exception to raise. Every call
    is recorded as ``(query, args)``.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.fetchval_return = 0

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return self.fetchval_return


def _unique_violation(constraint):
    exc = asyncpg.UniqueViolationError("duplicate key")
    exc.constraint_name = constraint
    return exc


# ---------------------------------------------------------------------------
# The statement itself
# ---------------------------------------------------------------------------


async def test_the_number_is_max_plus_one_computed_inside_the_insert():
    pool = _ScriptedPool([{"ticket_number": 12}])

    number = await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2)

    assert number == 12
    query, args = pool.calls[0]
    assert "INSERT INTO tickets" in query
    assert "COALESCE(MAX(ticket_number), 0) + 1" in query
    assert "RETURNING ticket_number" in query
    assert args == (GUILD_ID, 555, OPENER_ID, 2)


async def test_the_cap_is_guarded_in_the_same_statement_as_the_insert():
    # The whole point: "am I under the cap" and "take a slot" cannot be
    # separated by a click, so the count lives in the INSERT's WHERE.
    pool = _ScriptedPool([{"ticket_number": 1}])

    await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2)

    query, _args = pool.calls[0]
    assert "WHERE (SELECT COUNT(*) FROM tickets" in query
    assert "opener_id = $3 AND status = 'open') < $4" in query


async def test_the_open_statement_never_writes_any_content():
    pool = _ScriptedPool([{"ticket_number": 1}])
    await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2)
    query, _args = pool.calls[0]
    # The columns written are exhaustively these four. A subject, a transcript
    # or any message text has no column and no parameter to arrive through.
    assert (
        "INSERT INTO tickets (guild_id, ticket_number, thread_id, opener_id)" in query
    )
    for word in ("subject", "content", "message", "transcript"):
        assert word not in query.lower()


# ---------------------------------------------------------------------------
# At the cap: a clean answer, not an exception
# ---------------------------------------------------------------------------


async def test_a_member_at_the_cap_gets_none_rather_than_an_error():
    # The guarded INSERT inserts no row, so fetchrow returns None.
    pool = _ScriptedPool([None])
    assert await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2) is None
    assert len(pool.calls) == 1  # and it is NOT retried


# ---------------------------------------------------------------------------
# The retry
# ---------------------------------------------------------------------------


async def test_a_ticket_number_collision_is_retried_until_it_lands():
    pool = _ScriptedPool(
        [
            _unique_violation(storage.NUMBER_CONSTRAINT),
            _unique_violation(storage.NUMBER_CONSTRAINT),
            {"ticket_number": 9},
        ]
    )

    assert await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2) == 9
    assert len(pool.calls) == 3


async def test_the_retry_re_evaluates_the_cap_and_can_still_refuse():
    # THE race this design closes: the loser of a same-guild collision retries,
    # sees the winner's now-committed row, and is refused instead of overshooting.
    pool = _ScriptedPool([_unique_violation(storage.NUMBER_CONSTRAINT), None])

    assert await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2) is None
    assert len(pool.calls) == 2


async def test_contention_well_past_a_handful_still_lands():
    # The budget is a per-guild CONCURRENCY ceiling: the k-th finisher of k
    # simultaneous opens needs k attempts. A public button can produce a dozen
    # clicks in one second during an incident, so twelve losses in a row must
    # still end in a ticket rather than in "something went wrong".
    pool = _ScriptedPool(
        [_unique_violation(storage.NUMBER_CONSTRAINT)] * 12 + [{"ticket_number": 13}]
    )

    assert await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2) == 13
    assert len(pool.calls) == 13
    assert storage.OPEN_RETRIES >= 20  # sized for a herd, not for a moderator


async def test_the_retry_budget_is_bounded_and_the_last_error_surfaces():
    pool = _ScriptedPool(
        [_unique_violation(storage.NUMBER_CONSTRAINT)] * storage.OPEN_RETRIES
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2)
    assert len(pool.calls) == storage.OPEN_RETRIES


async def test_a_duplicate_thread_is_raised_immediately_and_never_retried():
    # Retrying "this thread is already a ticket" would burn the whole budget and
    # then raise the same error anyway.
    pool = _ScriptedPool([_unique_violation(storage.THREAD_CONSTRAINT)])

    with pytest.raises(asyncpg.UniqueViolationError):
        await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2)
    assert len(pool.calls) == 1


async def test_an_unnamed_unique_violation_is_treated_as_retryable():
    # constraint_name is optional on the exception; defaulting to "retry" keeps
    # the common (number) case working rather than failing an open outright.
    pool = _ScriptedPool([asyncpg.UniqueViolationError("dup"), {"ticket_number": 4}])

    assert await storage.open_ticket(pool, GUILD_ID, 555, OPENER_ID, 2) == 4


# ---------------------------------------------------------------------------
# The two reads
# ---------------------------------------------------------------------------


async def test_the_open_count_is_scoped_to_guild_user_and_open_status(fake_pool):
    fake_pool.fetchval_return = 3

    assert await storage.count_open_for_user(fake_pool, GUILD_ID, OPENER_ID) == 3

    _method, query, args = fake_pool.calls[0]
    assert "SELECT COUNT(*) FROM tickets" in query
    # Exactly the predicate the partial index tickets_guild_open_idx serves.
    assert "guild_id = $1 AND opener_id = $2 AND status = 'open'" in query
    assert args == (GUILD_ID, OPENER_ID)


async def test_the_open_count_reads_a_missing_value_as_zero(fake_pool):
    fake_pool.fetchval_return = None
    assert await storage.count_open_for_user(fake_pool, GUILD_ID, OPENER_ID) == 0


async def test_fetch_by_thread_is_keyed_on_the_thread_alone(fake_pool):
    fake_pool.fetchrow_return = {"ticket_number": 2}

    row = await storage.fetch_by_thread(fake_pool, 4242)

    assert row == {"ticket_number": 2}
    _method, query, args = fake_pool.calls[0]
    assert "FROM tickets WHERE thread_id = $1" in query
    assert args == (4242,)


async def test_fetch_by_thread_returns_none_for_a_thread_that_is_not_a_ticket(
    fake_pool,
):
    fake_pool.fetchrow_return = None
    assert await storage.fetch_by_thread(fake_pool, 1) is None

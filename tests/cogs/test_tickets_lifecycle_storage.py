"""Unit tests for the three statements lot T2 added to ``tickets.storage``.

Shape-and-behaviour against a recording pool: what each statement SAYS, what it
is given, and what the caller does with each answer. The behaviour that no
in-memory fake can prove - that the claim is atomic, that the close is
exactly-once, that the sweep window is an index scan - was probed against
PostgreSQL inside a rolled-back transaction:

* two claims of one ticket: the first returns ``taken`` true, the second returns
  ``taken`` false carrying the FIRST claimer's id (its branch reads the
  pre-update snapshot), and the row keeps the first claimer;
* an opener claiming their own ticket: ``taken`` false, ``claimed_by`` still
  NULL, row unchanged;
* claiming a closed ticket: ``taken`` false with ``status = 'closed'``; claiming
  a thread that is not a ticket: no row at all;
* two closes of one ticket: the first returns the row, the second returns
  nothing (``UPDATE 0``);
* the sweep window over 2300 rows, 2000 of them closed: ``Index Scan using
  tickets_open_sweep_idx``, 50 rows, 2 shared buffers.
"""

import pytest

from cogs.config.tickets import storage

THREAD_ID = 7001
CLAIMER_ID = 43


class _RecordingPool:
    """Records every statement and replays one scripted answer per method."""

    def __init__(self):
        self.calls = []
        self.fetchrow_return = None
        self.fetch_return = []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.fetchrow_return

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.fetch_return


@pytest.fixture
def pool():
    return _RecordingPool()


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


async def test_every_claim_rule_lives_inside_the_update(pool):
    """None of the four can be defeated by a click between a read and a write."""
    await storage.claim_ticket(pool, THREAD_ID, CLAIMER_ID)

    _method, query, args = pool.calls[0]
    assert query.startswith("WITH claimed AS (UPDATE tickets SET claimed_by = $2")
    assert "thread_id = $1" in query
    assert "status = 'open'" in query
    assert "claimed_by IS NULL" in query
    assert "opener_id <> $2" in query
    assert args == (THREAD_ID, CLAIMER_ID)


async def test_the_refusal_branch_reads_the_same_row_so_it_can_explain(pool):
    await storage.claim_ticket(pool, THREAD_ID, CLAIMER_ID)

    _method, query, _args = pool.calls[0]
    assert "UNION ALL" in query
    assert "NOT EXISTS (SELECT 1 FROM claimed)" in query
    # Everything the caller needs to name WHICH rule refused.
    assert "SELECT ticket_number, status, opener_id, claimed_by, FALSE AS taken" in query


async def test_a_claim_is_one_round_trip_whatever_the_answer(pool):
    pool.fetchrow_return = {"taken": False, "claimed_by": 99}

    await storage.claim_ticket(pool, THREAD_ID, CLAIMER_ID)

    assert len(pool.calls) == 1


async def test_a_thread_that_is_not_a_ticket_claims_nothing(pool):
    pool.fetchrow_return = None
    assert await storage.claim_ticket(pool, THREAD_ID, CLAIMER_ID) is None


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_the_close_is_guarded_on_open_which_is_the_exactly_once_gate(pool):
    await storage.close_ticket(pool, THREAD_ID, CLAIMER_ID)

    _method, query, args = pool.calls[0]
    assert "UPDATE tickets SET status = 'closed'" in query
    assert "closed_at = now()" in query
    assert "WHERE thread_id = $1 AND status = 'open'" in query
    assert args == (THREAD_ID, CLAIMER_ID)


async def test_the_close_returns_everything_the_log_line_needs(pool):
    await storage.close_ticket(pool, THREAD_ID, None)

    _method, query, _args = pool.calls[0]
    for column in (
        "guild_id",
        "ticket_number",
        "opener_id",
        "claimed_by",
        "opened_at",
        "closed_at",
    ):
        assert column in query.split("RETURNING")[1]


async def test_an_automatic_close_writes_no_closer(pool):
    """Auto-archive, the sweep and a deleted thread all name nobody."""
    await storage.close_ticket(pool, THREAD_ID, None)

    _method, _query, args = pool.calls[0]
    assert args == (THREAD_ID, None)


async def test_losing_the_close_race_answers_none(pool):
    pool.fetchrow_return = None
    assert await storage.close_ticket(pool, THREAD_ID, CLAIMER_ID) is None


# ---------------------------------------------------------------------------
# the sweep window
# ---------------------------------------------------------------------------


async def test_the_sweep_window_is_cursored_ordered_and_limited(pool):
    await storage.fetch_sweep_candidates(
        pool, after_id=120, min_age_hours=1, limit=50
    )

    method, query, args = pool.calls[0]
    assert method == "fetch"
    assert "status = 'open' AND id > $1" in query
    assert "ORDER BY id LIMIT $3" in query
    assert args == (120, 1, 50)


async def test_the_sweep_never_casts_the_untrusted_guild_setting(pool):
    """The per-guild window is coerced in Python; a bad JSONB value cannot raise here."""
    await storage.fetch_sweep_candidates(pool, after_id=0, min_age_hours=1, limit=50)

    _method, query, _args = pool.calls[0]
    assert "guild_settings" not in query
    assert "::int" not in query
    assert "make_interval(hours => $2)" in query


async def test_the_sweep_reads_only_what_a_close_needs(pool):
    """No message columns exist to select - the table has none - and none are asked for."""
    await storage.fetch_sweep_candidates(pool, after_id=0, min_age_hours=1, limit=50)

    _method, query, _args = pool.calls[0]
    selected = query.split("FROM tickets")[0]
    assert "opened_at" in selected
    assert "thread_id" in selected
    assert "subject" not in selected
    assert "transcript" not in selected


# ---------------------------------------------------------------------------
# the read T1 left as the seam
# ---------------------------------------------------------------------------


async def test_the_read_back_by_thread_now_carries_the_claimer(pool):
    await storage.fetch_by_thread(pool, THREAD_ID)

    _method, query, _args = pool.calls[0]
    assert "claimed_by" in query
    # ... and is still keyed on the thread alone (the T1 contract).
    assert "FROM tickets WHERE thread_id = $1" in query

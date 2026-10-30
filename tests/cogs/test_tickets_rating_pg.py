"""Live PostgreSQL checks of the V5a ticket SQL (skipped without a DSN):
the exactly-once rating claim, opener-only, the 7-day window, the comment gates,
the erasure UPDATE and the claim timestamp."""

import datetime
import os
import uuid
from pathlib import Path

import pytest

from cogs.config.tickets import rating, storage
from tools import privacy

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"
OPENER, OTHER, STAFF, GUILD = 11, 12, 13, 99
DAYS = rating.RATING_WINDOW_DAYS


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"tk_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn=PG_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=PG_DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        await pool.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _closed_ticket(pool, thread_id=1, closed_days_ago=0):
    await storage.open_ticket(pool, GUILD, thread_id, OPENER, 5)
    row = await storage.close_ticket(pool, thread_id, STAFF)
    if closed_days_ago:
        await pool.execute(
            "UPDATE tickets SET closed_at = now() - make_interval(days => $2) "
            "WHERE id = $1",
            row["id"],
            closed_days_ago,
        )
    return row["id"]


async def test_the_rating_is_claimed_exactly_once(pool):
    tid = await _closed_ticket(pool)
    first = await storage.rate_ticket(pool, tid, OPENER, 4, window_days=DAYS)
    again = await storage.rate_ticket(pool, tid, OPENER, 1, window_days=DAYS)
    assert first["ticket_number"] == 1 and again is None
    assert await pool.fetchval("SELECT rating FROM tickets WHERE id = $1", tid) == 4


async def test_only_the_opener_can_rate(pool):
    tid = await _closed_ticket(pool)
    assert await storage.rate_ticket(pool, tid, OTHER, 5, window_days=DAYS) is None
    assert await pool.fetchval("SELECT rating FROM tickets WHERE id = $1", tid) is None


async def test_an_open_ticket_cannot_be_rated(pool):
    await storage.open_ticket(pool, GUILD, 5, OPENER, 5)
    tid = await pool.fetchval("SELECT id FROM tickets WHERE thread_id = 5")
    assert await storage.rate_ticket(pool, tid, OPENER, 5, window_days=DAYS) is None


async def test_the_window_closes_after_seven_days(pool):
    inside = await _closed_ticket(pool, 1, closed_days_ago=DAYS - 1)
    outside = await _closed_ticket(pool, 2, closed_days_ago=DAYS + 1)
    assert await storage.rate_ticket(pool, inside, OPENER, 3, window_days=DAYS)
    assert await storage.rate_ticket(pool, outside, OPENER, 3, window_days=DAYS) is None


async def test_the_rating_must_be_one_to_five(pool):
    import asyncpg

    tid = await _closed_ticket(pool)
    with pytest.raises(asyncpg.CheckViolationError):
        await storage.rate_ticket(pool, tid, OPENER, 6, window_days=DAYS)


async def test_the_comment_needs_a_rating_once_and_the_opener(pool):
    tid = await _closed_ticket(pool)
    assert await storage.comment_ticket(pool, tid, OPENER, "hi", window_days=DAYS) is None
    await storage.rate_ticket(pool, tid, OPENER, 2, window_days=DAYS)
    assert await storage.comment_ticket(pool, tid, OTHER, "no", window_days=DAYS) is None
    assert await storage.comment_ticket(pool, tid, OPENER, "first", window_days=DAYS)
    assert await storage.comment_ticket(pool, tid, OPENER, "second", window_days=DAYS) is None
    assert await pool.fetchval("SELECT rating_comment FROM tickets") == "first"


async def test_erasure_clears_what_the_user_wrote_and_keeps_the_row(pool):
    tid = await _closed_ticket(pool)
    other = await _closed_ticket(pool, 2)
    await pool.execute("UPDATE tickets SET opener_id = $2 WHERE id = $1", other, OTHER)
    await storage.rate_ticket(pool, tid, OPENER, 5, window_days=DAYS)
    await storage.comment_ticket(pool, tid, OPENER, "secret", window_days=DAYS)
    await storage.rate_ticket(pool, other, OTHER, 1, window_days=DAYS)

    await privacy.delete_user_data(pool, OPENER)

    row = await pool.fetchrow("SELECT * FROM tickets WHERE id = $1", tid)
    assert (row["rating"], row["rating_comment"], row["rated_at"]) == (None, None, None)
    assert row["opener_id"] == OPENER  # the server's bookkeeping stays
    assert await pool.fetchval("SELECT rating FROM tickets WHERE id = $1", other) == 1


async def test_the_export_carries_the_rating_and_comment(pool):
    tid = await _closed_ticket(pool)
    await storage.rate_ticket(pool, tid, OPENER, 4, window_days=DAYS)
    await storage.comment_ticket(pool, tid, OPENER, "thanks", window_days=DAYS)
    data, _avatars = await privacy.collect_user_export(pool, OPENER)
    (ticket,) = data["tickets_opened"]
    assert ticket["rating"] == 4 and ticket["rating_comment"] == "thanks"


async def test_a_claim_stamps_claimed_at_and_the_stats_rows_carry_it(pool):
    await storage.open_ticket(pool, GUILD, 7, OPENER, 5)
    await storage.claim_ticket(pool, 7, STAFF)
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    (row,) = await storage.fetch_stats_rows(pool, GUILD, since, 10)
    assert row["claimed_by"] == STAFF and row["claimed_at"] is not None
    assert "opener_id" not in row.keys() and "rating_comment" not in row.keys()
    assert await storage.fetch_stats_rows(pool, GUILD + 1, since, 10) == []

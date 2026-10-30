"""Live PostgreSQL checks of the temp-role SQL (skipped without a DSN)."""

import datetime
import os
import uuid
from pathlib import Path

import pytest

from cogs.moderation import temprole
from tools import privacy

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"
WHEN = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"tr_test_{uuid.uuid4().hex}"
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


async def _timers(pool):
    return await pool.fetchval("SELECT count(*) FROM timers WHERE event = $1", temprole.TIMER_EVENT)


async def test_a_grant_writes_row_and_timer_together_and_regrant_moves_the_expiry(pool):
    assert (await temprole.grant(pool, 1, 2, 3, WHEN, 5))[0] in ("new", "extended")
    later = WHEN + datetime.timedelta(days=1)
    assert (await temprole.grant(pool, 1, 2, 3, later, 5))[0] in ("new", "extended")
    assert await pool.fetchval("SELECT expires_at FROM temp_roles") == later
    assert await _timers(pool) == 1  # replaced, not stacked


async def test_the_limit_counts_new_grants_only(pool):
    for role in range(5):
        assert (await temprole.grant(pool, 1, 2, role, WHEN, 5))[0] in ("new", "extended")
    assert (await temprole.grant(pool, 1, 2, 99, WHEN, 5))[0] == "limit"
    assert (await temprole.grant(pool, 1, 2, 0, WHEN, 5))[0] in ("new", "extended")  # extending is free
    assert (await temprole.grant(pool, 2, 2, 99, WHEN, 5))[0] in ("new", "extended")  # other server
    assert (await temprole.grant(pool, 1, 2, 99, WHEN, 100))[0] in ("new", "extended")  # Yasuho+
    assert await _timers(pool) == 7


async def test_revoke_removes_both_rows_and_reports_absence(pool):
    await temprole.grant(pool, 1, 2, 3, WHEN, 5)
    assert await temprole.revoke(pool, 1, 2, 3) is True
    assert await temprole.revoke(pool, 1, 2, 3) is False
    assert await pool.fetchval("SELECT count(*) FROM temp_roles") == 0
    assert await _timers(pool) == 0


async def test_erasure_deletes_the_record_but_not_the_timer(pool):
    await temprole.grant(pool, 1, 2, 3, WHEN, 5)
    query = dict(privacy.USER_DELETE_QUERIES)["temp_roles"]
    await pool.execute(query, 2)
    assert await pool.fetchval("SELECT count(*) FROM temp_roles") == 0
    assert await _timers(pool) == 1  # the scheduled removal survives


async def test_export_reads_the_users_own_rows_only(pool):
    await temprole.grant(pool, 1, 2, 3, WHEN, 5)
    await temprole.grant(pool, 1, 8, 3, WHEN, 5)
    data, _avatars = await privacy.collect_user_export(pool, 2)
    assert [r["role_id"] for r in data["temp_roles_held"]] == [3]


async def test_grant_reports_new_then_extended_with_the_previous_end(pool):
    assert await temprole.grant(pool, 1, 2, 3, WHEN, 5) == ("new", None)
    later = WHEN + datetime.timedelta(days=1)
    assert await temprole.grant(pool, 1, 2, 3, later, 5) == ("extended", WHEN)


async def test_sweep_rearms_only_rows_without_a_timer_and_is_idempotent(pool):
    await temprole.grant(pool, 1, 2, 3, WHEN, 5)
    await temprole.grant(pool, 1, 2, 4, WHEN, 5)
    await pool.execute(
        "DELETE FROM timers WHERE event = $1 AND extra->>'role_id' = '4'", temprole.TIMER_EVENT
    )  # a dispatch that reached nobody
    assert await temprole.sweep(pool) == 1
    assert await temprole.sweep(pool) == 0
    assert await _timers(pool) == 2
    row = await pool.fetchrow(
        "SELECT expires, extra FROM timers WHERE extra->>'role_id' = '4'"
    )
    assert row["expires"] == WHEN


async def test_sweep_is_bounded_per_batch(pool):
    for role in range(3):
        await temprole.grant(pool, 1, 2, role, WHEN, 10**9)
    await pool.execute("DELETE FROM timers WHERE event = $1", temprole.TIMER_EVENT)
    assert await temprole.sweep(pool) == 3

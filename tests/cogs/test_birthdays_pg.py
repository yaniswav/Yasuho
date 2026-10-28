"""Live PostgreSQL checks of the birthdays SQL (skipped without a DSN)."""

import os
import uuid
from pathlib import Path

import pytest

from cogs.community.birthdays import store

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"bd_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn=PG_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=PG_DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
        await pool.execute(ddl)
        await pool.execute(ddl)  # idempotent
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_check_constraints_refuse_impossible_dates(pool):
    import asyncpg

    await store.upsert(pool, 1, 10, 2, 29)  # a leap day is allowed
    for month, day in ((4, 31), (2, 30), (13, 1), (1, 0)):
        with pytest.raises(asyncpg.CheckViolationError):
            await store.upsert(pool, 1, 11, month, day)


async def test_per_guild_rows_and_day_lookup(pool):
    await store.upsert(pool, 1, 10, 3, 5)
    await store.upsert(pool, 2, 10, 7, 9)  # same user, other server
    await store.upsert(pool, 1, 10, 3, 6)  # update, not duplicate
    assert await store.on_day(pool, 1, 3, 6, False) == [10]
    assert await store.on_day(pool, 1, 3, 5, False) == []
    assert await store.on_day(pool, 2, 7, 9, False) == [10]
    assert await store.remove(pool, 1, 10) is True
    assert await store.remove(pool, 1, 10) is False
    assert await store.on_day(pool, 2, 7, 9, False) == [10]


async def test_feb29_joins_the_feb28_lookup_only_when_asked(pool):
    await store.upsert(pool, 1, 10, 2, 29)
    assert await store.on_day(pool, 1, 2, 28, True) == [10]
    assert await store.on_day(pool, 1, 2, 28, False) == []


async def test_claim_day_is_exactly_once_and_monotonic(pool):
    import datetime

    d = datetime.date
    assert await store.claim_day(pool, 1, d(2026, 10, 28)) is True
    assert await store.claim_day(pool, 1, d(2026, 10, 28)) is False
    assert await store.claim_day(pool, 1, d(2026, 10, 27)) is False
    assert await store.claim_day(pool, 1, d(2026, 10, 29)) is True
    assert await store.claim_day(pool, 2, d(2026, 10, 28)) is True  # per guild


async def test_in_months_filters_in_sql(pool):
    await store.upsert(pool, 1, 10, 10, 30)
    await store.upsert(pool, 1, 11, 12, 1)
    await store.upsert(pool, 2, 12, 10, 30)
    rows = await store.in_months(pool, 1, {10, 11})
    assert [r["user_id"] for r in rows] == [10]

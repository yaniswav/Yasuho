"""Live PostgreSQL checks of the V4a count guards (skipped without a DSN).

The guards must hold under READ COMMITTED with concurrent connections: a plain
INSERT...SELECT COUNT admitted 8 of 8 concurrent sets at limit 1.
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from cogs.anilist import airing_roles, watchalong
from tools import retention

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"
G = 111111111111111111  # above int4: exercises the lock-key fold


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"v4a_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn=PG_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=PG_DSN, min_size=1, max_size=10, server_settings={"search_path": schema}
    )
    try:
        await pool.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_concurrent_role_sets_never_exceed_the_limit(pool):
    results = await asyncio.gather(
        *[airing_roles.set_role(pool, G, 100 + i, 9000 + i, "x", 1) for i in range(8)]
    )
    assert results.count(True) == 1
    assert await pool.fetchval("SELECT count(*) FROM anilist_airing_roles") == 1


async def test_role_edit_of_an_existing_mapping_is_allowed_at_the_limit(pool):
    assert await airing_roles.set_role(pool, G, 1, 10, "T", 1)
    assert not await airing_roles.set_role(pool, G, 2, 11, "U", 1)
    assert await airing_roles.set_role(pool, G, 1, 12, "T2", 1)
    assert await pool.fetchval("SELECT role_id FROM anilist_airing_roles") == 12


async def test_concurrent_watchalong_starts_never_exceed_the_limit(pool):
    results = await asyncio.gather(
        *[
            watchalong.add_watchalong(pool, G, 100 + i, 5, 700 + i, None, "x", 2)
            for i in range(8)
        ]
    )
    assert [r[0] for r in results].count("added") == 2
    assert await pool.fetchval("SELECT count(*) FROM anilist_watchalongs") == 2


async def test_concurrent_starts_of_one_title_report_exists_not_limit(pool):
    results = await asyncio.gather(
        *[
            watchalong.add_watchalong(pool, G, 7, 5, 700 + i, None, "x", 5)
            for i in range(6)
        ]
    )
    states = [r[0] for r in results]
    assert states.count("added") == 1 and states.count("exists") == 5
    winner = await pool.fetchval("SELECT thread_id FROM anilist_watchalongs")
    assert {r[1] for r in results if r[0] == "exists"} == {winner}


async def test_slot_state_precheck(pool):
    assert (await watchalong.slot_state(pool, G, 7, 1))[0] == "ok"
    await watchalong.add_watchalong(pool, G, 7, 5, 700, None, "x", 1)
    assert await watchalong.slot_state(pool, G, 7, 1) == ("exists", 700)
    assert (await watchalong.slot_state(pool, G, 8, 1))[0] == "limit"


async def test_guild_purge_deletes_both_tables(pool):
    await airing_roles.set_role(pool, G, 1, 10, "T", 5)
    await watchalong.add_watchalong(pool, G, 1, 5, 700, None, "x", 5)
    queries = dict(retention.GUILD_DELETE_QUERIES)
    for name in ("anilist_airing_roles", "anilist_watchalongs"):
        await pool.execute(queries[name], G)
        assert await pool.fetchval(f"SELECT count(*) FROM {name}") == 0

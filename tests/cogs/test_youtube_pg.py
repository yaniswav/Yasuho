"""Live PostgreSQL checks of the YouTube follow SQL (skipped without a DSN)."""

import os
import uuid
from pathlib import Path

import pytest

from cogs.config import youtube
from tools import retention

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"
UC_A = "UC" + "a" * 22
UC_B = "UC" + "b" * 22


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"yt_test_{uuid.uuid4().hex}"
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


async def test_add_follow_limit_baseline_and_purge(pool):
    assert await youtube.add_follow(pool, 1, UC_A, 9, 1, None) == "new"
    row = await pool.fetchrow("SELECT baselined, seen FROM youtube_follows")
    assert row["baselined"] is False and list(row["seen"]) == []
    assert await youtube.add_follow(pool, 1, UC_B, 9, 1, None) == "limit"
    assert await youtube.add_follow(pool, 1, UC_A, 10, 1, None) == "updated"
    assert await pool.fetchval("SELECT post_channel FROM youtube_follows") == 10
    await pool.execute(dict(retention.GUILD_DELETE_QUERIES)["youtube_follows"], 1)
    assert await pool.fetchval("SELECT count(*) FROM youtube_follows") == 0

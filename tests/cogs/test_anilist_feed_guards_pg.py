"""Live PostgreSQL checks of the feed / tracked-title count guards (skipped
without a DSN). Under READ COMMITTED a plain count-then-insert admits every
concurrent caller at cap-1; the per-guild advisory lock must hold them at the cap.
"""

import asyncio
import os
import types
import uuid
from pathlib import Path

import pytest

from cogs.anilist import feed_policy as af
from cogs.anilist.feed import AniListFeed

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"
G = 111111111111111111  # above int4: exercises the lock-key fold


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"feedguard_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn=PG_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=PG_DSN, min_size=12, max_size=12, server_settings={"search_path": schema}
    )
    try:
        await pool.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _cog(pool):
    cog = AniListFeed.__new__(AniListFeed)
    cog.bot = types.SimpleNamespace(db_pool=pool)  # no premium: FREE limits
    return cog


async def test_concurrent_feed_creations_stop_at_the_cap(pool):
    cog = _cog(pool)
    cap = af.MAX_FEEDS_PER_GUILD
    for i in range(cap - 1):
        assert await cog._create_feed(G, 1000 + i) is None
    results = await asyncio.gather(*[cog._create_feed(G, 5000 + i) for i in range(8)])
    assert results.count(None) == 1
    assert await pool.fetchval("SELECT count(*) FROM anilist_feeds") == cap


async def test_concurrent_creations_of_one_channel_make_one_feed(pool):
    cog = _cog(pool)
    results = await asyncio.gather(*[cog._create_feed(G, 42) for _i in range(6)])
    assert results.count(None) == 1
    assert await pool.fetchval("SELECT count(*) FROM anilist_feeds") == 1


async def test_concurrent_title_adds_stop_at_the_cap(pool):
    cog = _cog(pool)
    cap = af.MAX_SUBS_PER_FEED
    assert await cog._create_feed(G, 7) is None
    for i in range(cap - 1):
        assert await cog._add_channel_sub(G, 7, i + 1, "ANIME", "T%d" % i, 9) is None
    results = await asyncio.gather(
        *[cog._add_channel_sub(G, 7, 9000 + i, "ANIME", "N%d" % i, 9) for i in range(8)]
    )
    assert results.count(None) == 1
    assert await pool.fetchval("SELECT count(*) FROM anilist_channel_subs") == cap


async def test_readding_a_tracked_title_at_the_cap_is_allowed(pool):
    cog = _cog(pool)
    assert await cog._create_feed(G, 7) is None
    for i in range(af.MAX_SUBS_PER_FEED):
        assert await cog._add_channel_sub(G, 7, i + 1, "ANIME", "T%d" % i, 9) is None
    assert await cog._add_channel_sub(G, 7, 1, "ANIME", "Renamed", 9) is None
    assert await pool.fetchval(
        "SELECT title FROM anilist_channel_subs WHERE media_id = 1"
    ) == "Renamed"

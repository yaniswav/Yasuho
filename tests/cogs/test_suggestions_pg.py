"""Live PostgreSQL checks of the suggestion box SQL (skipped without a DSN).

Set YASUHO_TEST_PG_DSN to a throwaway database; each test runs in a fresh schema
with schema.sql applied twice (idempotence).
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from cogs.community.suggestions import store

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"sg_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn=PG_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=PG_DSN, min_size=2, max_size=8, server_settings={"search_path": schema}
    )
    try:
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
        await pool.execute(ddl)
        await pool.execute(ddl)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _make(pool, guild=1, author=10, title="T", details="D"):
    row = await store.create(pool, guild, 5, author, title, details)
    await store.attach_message(pool, row["id"], 100 + row["id"])
    return row


async def test_numbers_are_unique_and_gapless_under_concurrency(pool):
    rows = await asyncio.gather(*(store.create(pool, 1, 5, 10, "t", "d") for _ in range(20)))
    assert sorted(r["number"] for r in rows) == list(range(1, 21))
    other = await store.create(pool, 2, 5, 10, "t", "d")
    assert other["number"] == 1  # per guild


async def test_vote_toggle_switch_and_counts(pool):
    row = await _make(pool)
    sid = row["id"]
    assert await store.toggle_vote(pool, sid, 1, 20, 1) == "cast"
    assert await store.toggle_vote(pool, sid, 1, 21, 1) == "cast"
    assert await store.counts(pool, sid) == (2, 0)
    assert await store.toggle_vote(pool, sid, 1, 20, -1) == "cast"  # switch
    assert await store.counts(pool, sid) == (1, 1)
    assert await store.toggle_vote(pool, sid, 1, 20, -1) == "removed"  # same again
    assert await store.counts(pool, sid) == (1, 0)
    assert await store.toggle_vote(pool, sid, 99, 20, 1) == "missing"  # wrong guild


async def test_closed_suggestions_refuse_votes_and_reopening_allows_them(pool):
    row = await _make(pool)
    sid = row["id"]
    await store.toggle_vote(pool, sid, 1, 20, 1)
    updated = await store.set_status(pool, sid, 1, "rejected", "no")
    assert updated["previous"] == "open" and updated["status_reason"] == "no"
    assert await store.toggle_vote(pool, sid, 1, 20, 1) == "closed"  # no removal either
    assert await store.counts(pool, sid) == (1, 0)
    await store.set_status(pool, sid, 1, "approved", None)
    assert await store.toggle_vote(pool, sid, 1, 20, 1) == "removed"
    assert await store.set_status(pool, 424242, 1, "approved", None) is None


async def test_digest_top_and_erasure_cascade(pool):
    a = await _make(pool, title="a")
    b = await _make(pool, title="b")
    c = await _make(pool, title="c")
    await store.toggle_vote(pool, b["id"], 1, 20, 1)
    await store.toggle_vote(pool, b["id"], 1, 21, 1)
    await store.toggle_vote(pool, a["id"], 1, 20, 1)
    await store.set_status(pool, c["id"], 1, "approved", None)
    top = await store.top_recent(pool, 1)
    assert [r["title"] for r in top] == ["b", "a"]  # c is not open
    refs = await store.message_refs(pool, 10)
    assert len(refs) == 3
    await pool.execute("DELETE FROM suggestion_votes WHERE user_id = $1", 20)
    await pool.execute("DELETE FROM suggestions WHERE author_id = $1", 10)
    assert await pool.fetchval("SELECT count(*) FROM suggestion_votes") == 0


async def test_digest_timer_rows_are_single_and_follow_the_config(pool):
    import types

    from cogs.community.suggestions.cog import Suggestions

    cog = Suggestions(types.SimpleNamespace(db_pool=pool, get_cog=lambda name: None))
    on = store.normalise({"enabled": True, "channel_id": 5, "digest": True})

    async def rows():
        return await pool.fetch(
            "SELECT expires FROM timers WHERE event = 'suggestion_digest' "
            "AND extra->>'guild_id' = '1'"
        )

    await cog._write_timer(1, on, replace=False)
    await cog._write_timer(1, on, replace=False)
    assert len(await rows()) == 1
    moved = store.normalise({**on, "digest_weekday": 3})
    await cog._write_timer(1, moved, replace=True)
    found = await rows()
    assert len(found) == 1 and found[0]["expires"].weekday() == 3
    await cog._write_timer(1, store.normalise({**on, "digest": False}), replace=True)
    assert await rows() == []

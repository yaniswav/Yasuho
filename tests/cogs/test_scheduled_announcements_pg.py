"""Live PG11 schema, cascade and row-lock races on a throwaway database.

Skipped unless YASUHO_TEST_PG_DSN is set, as in test_premium_status_view.py.
Each test applies schema.sql twice in a fresh schema and drops it afterward.
Only connection A's claim SELECT is changed for the negative controls; all
transactions, row locks, cascades and competing store writes are real SQL.
"""

import asyncio
import datetime
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from cogs.community.announcements_store import AnnouncementStore

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    PG_DSN is None,
    reason="YASUHO_TEST_PG_DSN not set; requires a throwaway PostgreSQL 11+ database",
)
NOW = datetime.datetime(2026, 1, 1, 9, tzinfo=datetime.timezone.utc)
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


@pytest.fixture
async def connections(monkeypatch):
    import asyncpg

    monkeypatch.setattr(discord.utils, "utcnow", lambda: NOW)
    schema = f"s1a_test_{uuid.uuid4().hex}"
    conn_a = await asyncpg.connect(dsn=PG_DSN)
    conn_b = None
    try:
        await conn_a.execute(f'CREATE SCHEMA "{schema}"')
        await conn_a.execute(f'SET search_path TO "{schema}"')
        await conn_a.execute("SET deadlock_timeout TO '100ms'")
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
        await conn_a.execute(ddl)
        await conn_a.execute(ddl)
        conn_b = await asyncpg.connect(
            dsn=PG_DSN, server_settings={"search_path": schema, "deadlock_timeout": "100ms"},
        )
        yield conn_a, conn_b
    finally:
        if conn_b is not None:
            await conn_b.close()
        try:
            await conn_a.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await conn_a.close()


async def seed(conn):
    row = await conn.fetchrow(
        "INSERT INTO scheduled_announcements "
        "(guild_id, channel_id, author_id, content, timezone, local_time, recurrence, next_run_at) "
        "VALUES (3, 9, 5, 'hello', 'UTC', $1, 'daily', $2) RETURNING *",
        NOW.replace(tzinfo=None), NOW,
    )
    timer = await conn.fetchrow(
        "INSERT INTO timers (event, expires, announcement_id) "
        "VALUES ('announcement', $1, $2) RETURNING *", NOW, row["id"],
    )
    return row, timer


class ConnectionPool:
    """Give each store its own real connection, without adding serialization."""

    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def fetchrow(self, query, *args):
        return await self.conn.fetchrow(query, *args)


def store(conn):
    return AnnouncementStore(SimpleNamespace(
        db_pool=ConnectionPool(conn), get_cog=lambda name: None,
    ))


class PausedClaim:
    """Pause A after DELETE-as-claim, with its transaction still open."""

    def __init__(self, conn, lock_parent=True):
        self.conn = conn
        self.lock_parent = lock_parent
        self.claimed = asyncio.Event()
        self.resume = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def fetchrow(self, query, *args):
        if query.startswith("SELECT * FROM scheduled_announcements WHERE id="):
            assert query.endswith(" FOR UPDATE")
            if not self.lock_parent:
                query = query.removesuffix(" FOR UPDATE")
        row = await self.conn.fetchrow(query, *args)
        if query.startswith("DELETE FROM timers") and row is not None:
            self.claimed.set()
            await self.resume.wait()
        return row


async def wait_for_block(conn_a, conn_b, task):
    """Observe B waiting on A in PostgreSQL, rather than guessing with a sleep."""
    async def blocked():
        while True:
            if task.done():
                pytest.fail(f"Competing write finished before the barrier: {task.result()!r}")
            blockers = await conn_a.fetchval("SELECT pg_blocking_pids($1)", conn_b.get_server_pid())
            if conn_a.get_server_pid() in blockers:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(blocked(), timeout=5)


async def race(connections, operation, lock_parent):
    conn_a, conn_b = connections
    row, timer = await seed(conn_a)
    paused = PausedClaim(conn_a, lock_parent)
    claim_store, write_store = store(paused), store(conn_b)
    claim = asyncio.create_task(claim_store.claim_and_reschedule(timer, now=NOW))
    write = None
    try:
        await asyncio.wait_for(paused.claimed.wait(), timeout=5)
        if operation == "delete":
            write = asyncio.create_task(write_store.delete(3, row["id"]))
        else:
            write = asyncio.create_task(write_store.update(
                3, row["id"], content="edited",
                local_time=(NOW + datetime.timedelta(days=2)).replace(tzinfo=None),
            ))
        await wait_for_block(conn_a, conn_b, write)
        paused.resume.set()
        results = await asyncio.wait_for(
            asyncio.gather(claim, write, return_exceptions=True), timeout=5,
        )
        return row, results, claim_store
    finally:
        paused.resume.set()
        tasks = [task for task in (claim, write) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_schema_rerun_and_pending_timer_fk_cascade(connections):
    conn_a, _conn_b = connections
    row, timer = await seed(conn_a)
    assert timer["claimed_at"] is None
    assert await conn_a.fetchval("SELECT count(*) FROM timers") == 1
    await conn_a.execute("DELETE FROM scheduled_announcements WHERE id=$1", row["id"])
    assert await conn_a.fetchval("SELECT count(*) FROM scheduled_announcements") == 0
    assert await conn_a.fetchval("SELECT count(*) FROM timers") == 0


@pytest.mark.parametrize("operation", ["delete", "update"])
async def test_claim_serializes_competing_write(connections, operation):
    conn_a, _conn_b = connections
    row, results, claim_store = await race(connections, operation, lock_parent=True)
    claimed, written = results
    assert all(not isinstance(result, BaseException) for result in results), results
    assert claimed is not None
    assert claimed["extra"]["announcement"]["revision"] == 1
    # A stale delivery result must not recreate a deleted row or change an edit.
    assert await claim_store.record_result(claimed["extra"]["announcement"], "Forbidden") is None
    pending = await conn_a.fetch("SELECT * FROM timers WHERE announcement_id=$1", row["id"])
    current = await conn_a.fetchrow("SELECT * FROM scheduled_announcements WHERE id=$1", row["id"])
    if operation == "delete":
        assert written is True
        assert current is None and not pending
    else:
        assert written["revision"] == current["revision"] == 2
        assert current["content"] == "edited" and current["failures"] == 0
        assert len(pending) == 1 and pending[0]["claimed_at"] is None
        assert pending[0]["expires"] == current["next_run_at"] == NOW + datetime.timedelta(days=2)
        assert pending[0]["id"] != claimed["id"]


@pytest.mark.parametrize("operation", ["delete", "update"])
async def test_without_parent_lock_race_deadlocks(connections, operation):
    import asyncpg

    # Without FOR UPDATE, B locks the parent and waits on A's deleted timer.
    # A then tries to update B's parent: PostgreSQL must abort one transaction.
    # The healthy test's no-exceptions assertion would fail, even if a later
    # retry could repair the queue. This is a real lock-order negative control.
    _row, results, _claim_store = await race(connections, operation, lock_parent=False)
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], asyncpg.DeadlockDetectedError), results

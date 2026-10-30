"""Live PostgreSQL check of the multi-starboard migration (skipped without a DSN)."""

import os
import uuid
from pathlib import Path

import pytest

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"

# The pre-V5c shape: PK on message_id alone, no slot column, no boards table.
OLD = """
CREATE TABLE starboard (guild_id BIGINT PRIMARY KEY, channel_id BIGINT NOT NULL,
    threshold INTEGER NOT NULL DEFAULT 3);
CREATE TABLE starboard_entries (message_id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL,
    star_message_id BIGINT, channel_id BIGINT, star_count INTEGER NOT NULL DEFAULT 0);
INSERT INTO starboard VALUES (1, 10, 3);
INSERT INTO starboard_entries VALUES (100, 1, 500, 10, 4);
"""


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"sb_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn=PG_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=PG_DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        await pool.execute(OLD)
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
        await pool.execute(ddl)
        await pool.execute(ddl)  # idempotent
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_existing_single_board_survives_as_board_one(pool):
    row = await pool.fetchrow("SELECT * FROM starboard_entries WHERE message_id = 100")
    assert (row["slot"], row["star_message_id"], row["star_count"]) == (1, 500, 4)
    assert (await pool.fetchrow("SELECT * FROM starboard"))["threshold"] == 3


async def test_a_message_can_sit_on_two_boards_but_not_twice_on_one(pool):
    import asyncpg

    await pool.execute(
        "INSERT INTO starboard_entries (message_id, guild_id, slot) VALUES (100, 1, 2)"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "INSERT INTO starboard_entries (message_id, guild_id, slot) VALUES (100, 1, 2)"
        )


async def test_boards_need_board_one_and_unique_emoji_and_cascade(pool):
    import asyncpg

    await pool.execute(
        "INSERT INTO starboard_boards (guild_id, slot, channel_id, emoji) VALUES (1, 2, 11, 'x')"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "INSERT INTO starboard_boards (guild_id, slot, channel_id, emoji) VALUES (1, 3, 12, 'x')"
        )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute(
            "INSERT INTO starboard_boards (guild_id, slot, channel_id, emoji) VALUES (9, 2, 12, 'y')"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO starboard_boards (guild_id, slot, channel_id, emoji) VALUES (1, 1, 12, 'z')"
        )
    await pool.execute("DELETE FROM starboard WHERE guild_id = 1")  # dashboard disable
    assert await pool.fetchval("SELECT count(*) FROM starboard_boards") == 0

"""Live PostgreSQL checks of the 24/7 playlist SQL (skipped without a DSN)."""

import os
import uuid
from pathlib import Path

import pytest

from cogs.music import always_on as ao

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"r247_test_{uuid.uuid4().hex}"
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


async def _seed(pool, tracks=("a", "b", "c", "d")):
    await pool.execute(
        "INSERT INTO music_247 (guild_id, channel_id) VALUES (1, 10)"
    )
    await pool.execute(
        "INSERT INTO guild_playlists (guild_id, name, name_norm, creator_id, tracks,"
        " track_count) VALUES (1, 'Chill', 'chill', 5, $1, $2)",
        list(tracks),
        len(tracks),
    )


async def test_defaults_and_set_radio(pool):
    await _seed(pool)
    row = await pool.fetchrow("SELECT * FROM music_247 WHERE guild_id = 1")
    assert row["radio_playlist"] is None
    assert row["radio_shuffle"] is True
    assert row["radio_pos"] == 0
    store = ao.AlwaysOnStore()
    assert await store.set_radio(pool, 2, "chill", True) is False  # no 24/7 row
    assert await store.set_radio(pool, 1, "chill", False) is True
    fresh = ao.AlwaysOnStore()
    await fresh.ensure_loaded(pool)
    assert fresh.radio(1) == ("chill", False)


async def test_state_and_blob_fetch_in_requested_order(pool):
    await _seed(pool)
    await ao.AlwaysOnStore().set_radio(pool, 1, "chill", True)
    state = await ao.fetch_radio_state(pool, 1, "chill")
    assert (state["radio_pos"], state["radio_shuffle"], state["track_count"]) == (
        0,
        True,
        4,
    )
    assert await ao.fetch_radio_state(pool, 1, "other") is None
    blobs = await ao.fetch_radio_blobs(pool, 1, "chill", [3, 0, 2, 9])
    assert blobs == ["d", "a", "c", None]
    assert await ao.fetch_radio_blobs(pool, 1, "gone", [0]) == []


async def test_cas_moves_only_from_the_expected_position(pool):
    await _seed(pool)
    await ao.AlwaysOnStore().set_radio(pool, 1, "chill", True)
    assert await ao.advance_radio_pos(pool, 1, "chill", 0, 10) is True
    assert await ao.advance_radio_pos(pool, 1, "chill", 0, 20) is False
    assert await ao.advance_radio_pos(pool, 1, "other", 10, 20) is False
    assert await pool.fetchval("SELECT radio_pos FROM music_247") == 10


async def test_rename_and_delete_retarget_the_row(pool):
    await _seed(pool)
    await ao.AlwaysOnStore().set_radio(pool, 1, "chill", True)
    await ao.advance_radio_pos(pool, 1, "chill", 0, 7)
    async with pool.acquire() as conn:
        await ao.retarget_radio_row(conn, 1, "chill", "lofi")
    row = await pool.fetchrow("SELECT radio_playlist, radio_pos FROM music_247")
    assert (row["radio_playlist"], row["radio_pos"]) == ("lofi", 7)
    async with pool.acquire() as conn:
        await ao.retarget_radio_row(conn, 1, "other", None)  # not ours: no-op
        await ao.retarget_radio_row(conn, 1, "lofi", None)
    row = await pool.fetchrow("SELECT radio_playlist, radio_pos FROM music_247")
    assert (row["radio_playlist"], row["radio_pos"]) == (None, 0)


async def test_playlist_mixin_rename_and_delete_follow_through(pool):
    import types

    from cogs.music import playlists_shared as ps

    class _Cog(ps.ServerPlaylistMixin):
        def __init__(self):
            self.bot = types.SimpleNamespace(db_pool=pool)
            self.always_on = ao.AlwaysOnStore()

    await _seed(pool)
    cog = _Cog()
    await cog.always_on.set_radio(pool, 1, "chill", True)
    await cog._rename_guild_playlist(1, "chill", "Lofi", "lofi")
    assert cog.always_on.radio(1) == ("lofi", True)
    assert await pool.fetchval("SELECT radio_playlist FROM music_247") == "lofi"
    assert (await ao.fetch_radio_state(pool, 1, "lofi"))["track_count"] == 4
    assert await cog._delete_guild_playlist(1, "lofi") is True
    assert cog.always_on.radio(1) is None
    assert await pool.fetchval("SELECT radio_playlist FROM music_247") is None

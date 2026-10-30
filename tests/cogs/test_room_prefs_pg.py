"""Live PostgreSQL checks of the remembered-room SQL (skipped without a DSN)."""

import os
import uuid
from pathlib import Path

import pytest

from tools import privacy, retention, roomprefs

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"rp_test_{uuid.uuid4().hex}"
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


async def test_saves_merge_field_by_field_per_hub(pool):
    assert await roomprefs.load(pool, 1, 2, 10) is None
    await roomprefs.save(pool, 1, 2, 10, name="Quiet")
    await roomprefs.save(pool, 1, 2, 10, user_limit=4, locked=True)
    await roomprefs.save(pool, 1, 2, 10, locked=False)
    assert await roomprefs.load(pool, 1, 2, 10) == {
        "name": "Quiet", "user_limit": 4, "locked": False, "hidden": None,
    }
    assert await roomprefs.load(pool, 1, 2, 11) is None  # another hub
    assert await roomprefs.load(pool, 9, 2, 10) is None  # another server


async def test_opt_out_survives_forget_and_remember_keeps_settings(pool):
    await roomprefs.save(pool, 1, 2, 10, name="Quiet")
    await roomprefs.set_remember(pool, 1, 2, False)
    assert await roomprefs.load(pool, 1, 2, 10) is None  # wiped, opted out
    await roomprefs.save(pool, 1, 2, 10, name="Again")
    assert await roomprefs.load(pool, 1, 2, 10) is None  # blocked
    await roomprefs.forget(pool, 1, 2)  # forget keeps the opt-out
    await roomprefs.save(pool, 1, 2, 10, name="Still blocked")
    assert await roomprefs.load(pool, 1, 2, 10) is None
    await roomprefs.set_remember(pool, 1, 2, True)
    await roomprefs.save(pool, 1, 2, 10, name="Back")
    await roomprefs.set_remember(pool, 1, 2, True)  # turning on again wipes nothing
    assert (await roomprefs.load(pool, 1, 2, 10))["name"] == "Back"
    await roomprefs.forget(pool, 1, 2)
    assert await roomprefs.load(pool, 1, 2, 10) is None


async def test_export_erasure_and_purge_reach_the_rows(pool):
    await roomprefs.save(pool, 1, 2, 10, name="Quiet")
    await roomprefs.save(pool, 3, 2, 10, name="Other")
    await roomprefs.set_remember(pool, 5, 2, False)  # opt-out in server 5
    data, _avatars = await privacy.collect_user_export(pool, 2)
    rows = data["room_prefs_saved"]
    assert {(r["guild_id"], r["hub_channel_id"]) for r in rows} == {(1, 10), (3, 10)}
    assert [r["guild_id"] for r in data["room_prefs_opted_out"]] == [5]
    assert all("updated_at" in r for r in rows)
    await pool.execute(dict(retention.GUILD_DELETE_QUERIES)["room_prefs"], 1)
    assert await pool.fetchval("SELECT count(*) FROM room_prefs") == 1
    # Profile erasure removes the settings but leaves the opt-out.
    await pool.execute(dict(privacy.USER_DELETE_QUERIES)["room_prefs"], 2)
    assert await pool.fetchval("SELECT count(*) FROM room_prefs") == 0
    assert await pool.fetchval("SELECT count(*) FROM room_prefs_off") == 1
    await roomprefs.save(pool, 5, 2, 10, name="Blocked after erasure")
    assert await roomprefs.load(pool, 5, 2, 10) is None
    await pool.execute(dict(retention.GUILD_DELETE_QUERIES)["room_prefs_off"], 5)
    assert await pool.fetchval("SELECT count(*) FROM room_prefs_off") == 0

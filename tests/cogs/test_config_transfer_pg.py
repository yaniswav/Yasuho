"""Live PostgreSQL checks of the config-transfer SQL (skipped without a DSN)."""

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from cogs.config import config_transfer as ct
from tools import retention, settings

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"cfg_test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(dsn=PG_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn=PG_DSN, min_size=1, max_size=6, server_settings={"search_path": schema}
    )
    settings.invalidate_all()
    try:
        await pool.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        yield pool
    finally:
        settings.invalidate_all()
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


PAYLOAD = '{"format": "yasuho-config", "version": 1, "sections": {}}'


async def test_template_crud_cap_and_purge(pool):
    assert await ct.save_template(pool, 1, "a", PAYLOAD, 2) == "saved"
    assert await ct.save_template(pool, 1, "b", PAYLOAD, 2) == "saved"
    assert await ct.save_template(pool, 1, "c", PAYLOAD, 2) == "full"
    assert await ct.save_template(pool, 1, "a", PAYLOAD, 2) == "replaced"  # a replace never counts
    assert await ct.save_template(pool, 2, "a", PAYLOAD, 2) == "saved"  # per server
    assert [r["name"] for r in await ct.list_templates(pool, 1)] == ["a", "b"]
    stored = await ct.load_template(pool, 1, "a")
    assert ct.parse_document(stored.encode()) == {}
    assert await ct.load_template(pool, 2, "b") is None  # never another server's
    assert await ct.delete_template(pool, 1, "b") is True
    assert await ct.delete_template(pool, 1, "b") is False
    await pool.execute(dict(retention.GUILD_DELETE_QUERIES)["config_templates"], 1)
    assert [r["name"] for r in await ct.list_templates(pool, 1)] == []
    assert [r["name"] for r in await ct.list_templates(pool, 2)] == ["a"]
    assert await pool.fetchval("SELECT count(*) FROM (" + retention.STORED_GUILD_IDS_QUERY + ") s") >= 1


async def test_concurrent_saves_never_pass_the_cap(pool):
    outcomes = await asyncio.gather(*(ct.save_template(pool, 5, "t%d" % i, PAYLOAD, 3) for i in range(8)))
    assert sorted(outcomes).count("saved") == 3
    assert await pool.fetchval("SELECT count(*) FROM config_templates WHERE guild_id = 5") == 3


async def test_flat_section_write_deletes_absent_keys_through_the_real_settings_seam(pool):
    spec = ct.SECTIONS["serverstats"]
    await ct.write_section(pool, 9, spec, {"serverstats_digest_channel": 5, "serverstats_daily_channel": 6})
    assert await ct.read_section(pool, 9, spec) == (
        True, {"serverstats_digest_channel": 5, "serverstats_daily_channel": 6}
    )
    await ct.write_section(pool, 9, spec, {"serverstats_digest_channel": 5, "serverstats_daily_channel": None})
    raw = await pool.fetchval("SELECT settings FROM guild_settings WHERE guild_id = 9")
    assert "serverstats_daily_channel" not in raw
    assert await ct.read_section(pool, 9, spec) == (
        True, {"serverstats_digest_channel": 5, "serverstats_daily_channel": None}
    )


async def test_a_flat_section_never_half_applies(pool):
    """Review P3: a value Postgres refuses (here a NUL, which parse_document now
    rejects upstream) must leave the WHOLE section untouched."""
    spec = ct.SECTIONS["tickets"]
    before = {"tickets_max_open_per_user": 2, "tickets_rating": True}
    await ct.write_section(pool, 4, spec, spec.clean(before))
    bad = spec.clean({"tickets_max_open_per_user": 3, "tickets_panel_message": "hi\x00there"})
    with pytest.raises(Exception):
        await ct.write_section(pool, 4, spec, bad)
    assert (await ct.read_section(pool, 4, spec))[1] == spec.clean(before)
    with pytest.raises(ct.DocumentError):
        ct.parse_document(
            b'{"format":"yasuho-config","version":1,"sections":{"tickets":'
            b'{"tickets_panel_message":"hi\\u0000there"}}}'
        )

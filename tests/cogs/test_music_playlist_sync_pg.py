"""Live PostgreSQL checks of the linked-playlist SQL (skipped without a DSN)."""

import datetime
import os
import types
import uuid
from pathlib import Path

import pytest

from cogs.music import always_on as ao
from cogs.music import playlist_sync as psync
from cogs.music import playlists_shared as ps

PG_DSN = os.environ.get("YASUHO_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(PG_DSN is None, reason="YASUHO_TEST_PG_DSN not set")
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"
DAY = datetime.timedelta(days=1)
URL = "https://open.spotify.com/playlist/abc"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


@pytest.fixture
async def pool():
    import asyncpg

    schema = f"psync_test_{uuid.uuid4().hex}"
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


class _Cog(ps.ServerPlaylistMixin, psync.PlaylistSyncMixin):
    def __init__(self, pool, *, linked=10, tracks=500, result=None, entitled=True):
        limits = types.SimpleNamespace(
            max_linked_playlists=linked if entitled else 0, max_playlist_tracks=tracks
        )
        self.result = result or psync.SourceResult(psync.OK, ("n1", "n2"), 2000, (1000, 1000))
        self.bot = types.SimpleNamespace(
            db_pool=pool,
            get_guild=lambda gid: types.SimpleNamespace(id=gid),
            premium=types.SimpleNamespace(for_guild=lambda gid: limits),
        )
        self.always_on = ao.AlwaysOnStore()

    async def _fetch_source_tracks(self, url):
        return self.result

    async def _sync_pause(self):
        pass


async def _link(cog, name="Mix", *, guild=1, due=True, url=URL):
    return await cog._import_new_playlist(
        guild, name, name.lower(), 5, ["old"], 1000, 50,
        source_url=url, next_sync_at=_now() - DAY if due else _now() + DAY, max_linked=10,
    )


async def test_new_columns_default_to_unlinked_for_old_rows(pool):
    await pool.execute(
        "INSERT INTO guild_playlists (guild_id, name, name_norm, creator_id) VALUES (1,'A','a',5)"
    )
    row = await pool.fetchrow("SELECT * FROM guild_playlists")
    assert row["source_url"] is None and row["next_sync_at"] is None
    assert row["sync_failures"] == 0 and row["sync_error"] is None


async def test_import_caps_are_enforced_inside_the_statement(pool):
    cog = _Cog(pool)
    assert await cog._import_new_playlist(1, "A", "a", 5, ["x"], 1, 1) == "saved"
    assert await cog._import_new_playlist(1, "B", "b", 5, ["x"], 1, 1) == "full"
    assert await cog._import_new_playlist(1, "A", "a", 5, ["x"], 1, 5) == "exists"
    # linked cap: one linked allowed, the second refused, an unlinked import fine
    kw = {"source_url": URL, "next_sync_at": _now(), "max_linked": 1}
    assert await cog._import_new_playlist(2, "L1", "l1", 5, ["x"], 1, 9, **kw) == "saved"
    assert await cog._import_new_playlist(2, "L2", "l2", 5, ["x"], 1, 9, **kw) == "link_full"
    assert await cog._import_new_playlist(2, "P", "p", 5, ["x"], 1, 9) == "saved"
    assert await cog._linked_count(2) == 1


async def test_replace_keeps_identity_and_unlinks_or_relinks(pool):
    cog = _Cog(pool)
    await cog._import_new_playlist(1, "Mix", "mix", 5, ["a"], 1, 9)
    created = await pool.fetchval("SELECT created_at FROM guild_playlists")
    kw = {"tracks": ["b", "c"], "total_ms": 2000, "source_url": URL, "next_sync_at": _now() + DAY}
    assert await cog._replace_playlist(1, "mix", max_linked=1, **kw) == "replaced"
    row = await pool.fetchrow("SELECT * FROM guild_playlists")
    assert (row["track_count"], row["source_url"], row["creator_id"]) == (2, URL, 5)
    assert row["created_at"] == created and row["synced_at"] is not None
    # re-linking the already-linked row never trips the cap it already occupies
    assert await cog._replace_playlist(1, "mix", max_linked=1, **kw) == "replaced"
    await cog._import_new_playlist(1, "Other", "other", 5, ["a"], 1, 9)
    assert await cog._replace_playlist(1, "other", max_linked=1, **kw) == "link_full"
    # a plain re-import unlinks
    kw["source_url"], kw["next_sync_at"] = None, None
    assert await cog._replace_playlist(1, "mix", **kw) == "replaced"
    assert await pool.fetchval("SELECT source_url FROM guild_playlists WHERE name_norm='mix'") is None
    assert await cog._replace_playlist(1, "gone", **kw) == "gone"


async def test_unlink_keeps_the_tracks(pool):
    cog = _Cog(pool)
    await _link(cog)
    assert await cog._unlink_playlist(1, "mix") is True
    assert await cog._unlink_playlist(1, "mix") is False
    row = await pool.fetchrow("SELECT * FROM guild_playlists")
    assert row["source_url"] is None and row["next_sync_at"] is None and row["tracks"] == ["old"]


async def test_claim_is_at_most_once_and_skips_unlinked_and_paused(pool):
    cog = _Cog(pool)
    await _link(cog)
    now = _now()
    assert await psync.claim_row(pool, 1, "mix", now, now + 7 * DAY) == URL
    assert await psync.claim_row(pool, 1, "mix", now, now + 7 * DAY) is None
    await pool.execute("UPDATE guild_playlists SET next_sync_at = NULL")  # paused
    assert await psync.due_rows(pool, now + 30 * DAY) == []
    assert await psync.claim_row(pool, 1, "mix", now + 30 * DAY, now) is None


async def test_three_strikes_pause_and_a_reimport_resumes(pool):
    cog = _Cog(pool, result=psync.SourceResult(psync.LOAD_FAILED))
    await _link(cog)
    now = _now()
    for strike in (1, 2, 3):
        stats = await cog._run_sync_sweep(now)
        assert stats["failed"] == 1
        row = await pool.fetchrow("SELECT * FROM guild_playlists")
        assert row["sync_failures"] == strike and row["sync_error"] == psync.LOAD_FAILED
        assert row["tracks"] == ["old"]  # the old tracks always survive
        if strike < 3:
            assert row["next_sync_at"] is not None
            now += 8 * DAY  # next week
    assert row["next_sync_at"] is None  # paused: kept link, no more attempts
    assert psync.is_paused(row["source_url"], row["next_sync_at"])
    assert (await cog._run_sync_sweep(now + 60 * DAY))["failed"] == 0
    line = ps.link_line(row)
    assert "sync paused" in line
    # relinking through a replace clears the streak
    kw = {"tracks": ["z"], "total_ms": 1, "source_url": URL, "next_sync_at": now + DAY}
    await cog._replace_playlist(1, "mix", max_linked=10, **kw)
    row = await pool.fetchrow("SELECT * FROM guild_playlists")
    assert (row["sync_failures"], row["sync_error"]) == (0, None)


async def test_success_replaces_tracks_and_resets_the_streak(pool):
    cog = _Cog(pool)
    await _link(cog)
    await pool.execute("UPDATE guild_playlists SET sync_failures = 2, sync_error = 'x'")
    assert (await cog._run_sync_sweep(_now()))["synced"] == 1
    row = await pool.fetchrow("SELECT * FROM guild_playlists")
    assert row["tracks"] == ["n1", "n2"] and row["track_count"] == 2 and row["total_ms"] == 2000
    assert (row["sync_failures"], row["sync_error"]) == (0, None)
    assert row["next_sync_at"] > _now() + 6 * DAY


async def test_a_load_that_lands_after_an_unlink_changes_nothing(pool):
    cog = _Cog(pool)
    await _link(cog)
    assert await psync.record_success(pool, 1, "mix", "https://other.test/x", ["q"], 1, _now()) is False
    assert await pool.fetchval("SELECT tracks FROM guild_playlists") == ["old"]


async def test_lapse_skips_then_resumes_without_touching_the_playlist(pool):
    lapsed = _Cog(pool, entitled=False)
    await _link(lapsed)
    now = _now()
    assert (await lapsed._run_sync_sweep(now))["skipped"] == 1
    row = await pool.fetchrow("SELECT * FROM guild_playlists")
    assert row["tracks"] == ["old"] and row["source_url"] == URL
    assert (await lapsed._run_sync_sweep(now))["skipped"] == 0  # not re-selected today
    back = _Cog(pool)
    assert (await back._run_sync_sweep(now + DAY + DAY))["synced"] == 1


async def test_global_cap_over_a_real_table(pool):
    cog = _Cog(pool)
    for i in range(psync.MAX_RESYNCS_PER_SWEEP + 5):
        await _link(cog, f"P{i}", guild=100 + i)
    assert (await cog._run_sync_sweep(_now()))["synced"] == psync.MAX_RESYNCS_PER_SWEEP
    assert len(await psync.due_rows(pool, _now())) == 5


async def test_rename_and_delete_a_linked_playlist_follow_through(pool):
    cog = _Cog(pool)
    await _link(cog)
    await pool.execute("INSERT INTO music_247 (guild_id, channel_id) VALUES (1, 10)")
    await cog.always_on.set_radio(pool, 1, "mix", True)
    await cog._rename_guild_playlist(1, "mix", "Fresh", "fresh")
    row = await pool.fetchrow("SELECT * FROM guild_playlists")
    assert (row["name_norm"], row["source_url"]) == ("fresh", URL)  # the link rides along
    assert cog.always_on.radio(1) == ("fresh", True)
    assert (await cog._run_sync_sweep(_now()))["synced"] == 1  # still synced under the new name
    assert await cog._delete_guild_playlist(1, "fresh") is True
    assert await pool.fetchval("SELECT count(*) FROM guild_playlists") == 0
    assert (await cog._run_sync_sweep(_now() + 30 * DAY))["synced"] == 0


async def test_sweep_timer_row_is_unique(pool):
    assert await psync.ensure_sweep_timer(pool, _now()) is True
    assert await psync.ensure_sweep_timer(pool, _now()) is False
    assert await pool.fetchval("SELECT count(*) FROM timers WHERE event = 'playlist_sync'") == 1


async def test_list_query_carries_the_link_columns(pool):
    cog = _Cog(pool)
    await _link(cog)
    rows = await cog._list_guild_playlists(1)
    assert rows[0]["source_url"] == URL and "synced_at" in rows[0].keys()
    assert "linked to open.spotify.com" in ps.link_line(rows[0])


async def test_concurrent_ensure_never_duplicates_the_sweep_row(pool):
    import asyncio

    for _ in range(20):
        await pool.execute("DELETE FROM timers WHERE event = 'playlist_sync'")
        await asyncio.gather(*(psync.ensure_sweep_timer(pool, _now()) for _ in range(4)))
        assert await pool.fetchval("SELECT count(*) FROM timers WHERE event = 'playlist_sync'") == 1


async def test_a_replace_pinned_to_a_recreated_row_changes_nothing(pool):
    cog = _Cog(pool)
    await cog._import_new_playlist(1, "Mix", "mix", 5, ["a"], 1, 9)
    row = await pool.fetchrow("SELECT creator_id, created_at FROM guild_playlists")
    kw = {"tracks": ["z"], "total_ms": 1}
    await pool.execute("DELETE FROM guild_playlists")  # deleted and recreated by someone else
    await cog._import_new_playlist(1, "Mix", "mix", 77, ["a"], 1, 9)
    res = await cog._replace_playlist(
        1, "mix", expect_creator=row["creator_id"], expect_created=row["created_at"], **kw
    )
    assert res == "changed"
    assert await pool.fetchval("SELECT tracks FROM guild_playlists") == ["a"]
    gone = await cog._replace_playlist(1, "nope", expect_creator=5, **kw)
    assert gone == "gone"


async def test_linked_cap_blocking_an_insert_reports_link_full(pool):
    cog = _Cog(pool)
    kw = {"source_url": URL, "next_sync_at": _now(), "max_linked": 1}
    assert await cog._import_new_playlist(1, "L1", "l1", 5, ["x"], 1, 9, **kw) == "saved"
    assert await cog._import_new_playlist(1, "L2", "l2", 5, ["x"], 1, 9, **kw) == "link_full"
    assert await cog._import_new_playlist(1, "P", "p", 5, ["x"], 1, 1, **{**kw, "source_url": None}) == "full"

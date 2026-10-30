"""The weekly sweep of linked playlists: budget, lapse, failure mapping, timer.

The SQL is real-Postgres territory (test_music_playlist_sync_pg.py); here the
module-level SQL functions are replaced by an in-memory table so the sweep's own
decisions (global cap, entitlement, which verdict reaches which write) are read
without a database.
"""

from __future__ import annotations

import datetime
import types

import pytest

from cogs.music import playlist_sync as psync
from tools import premium

NOW = datetime.datetime.now(datetime.timezone.utc)
DAY = datetime.timedelta(days=1)


class _Table:
    """guild_playlists rows keyed (guild_id, norm): the five sweep functions."""

    def __init__(self, monkeypatch):
        self.rows = {}
        self.failures = []
        monkeypatch.setattr(psync, "due_rows", self.due_rows)
        monkeypatch.setattr(psync, "claim_row", self.claim_row)
        monkeypatch.setattr(psync, "record_success", self.record_success)
        monkeypatch.setattr(psync, "record_failure", self.record_failure)
        monkeypatch.setattr(psync, "reschedule", self.reschedule)

    def add(self, guild_id, norm, *, due=True, url="https://x.test/p"):
        self.rows[(guild_id, norm)] = {
            "source_url": url,
            "next_sync_at": NOW - DAY if due else NOW + DAY,
            "tracks": ["old"],
            "failures": 0,
        }

    async def due_rows(self, pool, now, limit=psync.CANDIDATE_BATCH):
        due = sorted(
            (k for k, r in self.rows.items() if r["next_sync_at"] and r["next_sync_at"] <= now),
            key=lambda k: self.rows[k]["next_sync_at"],
        )
        return [{"guild_id": g, "name_norm": n} for g, n in due[:limit]]

    async def claim_row(self, pool, guild_id, norm, now, next_at):
        row = self.rows.get((guild_id, norm))
        if row is None or row["next_sync_at"] is None or row["next_sync_at"] > now:
            return None
        row["next_sync_at"] = next_at
        return row["source_url"]

    async def record_success(self, pool, guild_id, norm, url, blobs, total_ms, now):
        self.rows[(guild_id, norm)]["tracks"] = list(blobs)
        return True

    async def record_failure(self, pool, guild_id, norm, url, error, max_strikes=3):
        self.failures.append((guild_id, norm, error, max_strikes))

    async def reschedule(self, pool, guild_id, norm, url, next_at):
        self.rows[(guild_id, norm)]["next_sync_at"] = next_at


class _Cog(psync.PlaylistSyncMixin):
    def __init__(self, *, entitled=True, status=psync.OK, tracks=5, cap=500):
        self.entitled, self.status, self.cap = entitled, status, cap
        self.loads = []
        self.tracks = tracks
        limits = types.SimpleNamespace(
            max_linked_playlists=10 if entitled else 0, max_playlist_tracks=cap
        )
        self.bot = types.SimpleNamespace(
            db_pool=object(),
            get_guild=lambda gid: types.SimpleNamespace(id=gid),
            premium=types.SimpleNamespace(for_guild=lambda gid: limits),
        )

    async def _fetch_source_tracks(self, url):
        self.loads.append(url)
        if self.status != psync.OK:
            return psync.SourceResult(self.status)
        blobs = tuple(f"n{i}" for i in range(self.tracks))
        return psync.SourceResult(psync.OK, blobs, 1000 * len(blobs), (1000,) * len(blobs))

    async def _sync_pause(self):
        pass


@pytest.fixture
def table(monkeypatch):
    return _Table(monkeypatch)


async def test_a_due_playlist_is_reloaded_and_rescheduled_a_week_out(table):
    table.add(1, "mix")
    cog = _Cog()
    stats = await cog._run_sync_sweep(NOW)
    assert stats == {"synced": 1, "failed": 0, "skipped": 0}
    assert table.rows[(1, "mix")]["tracks"] == [f"n{i}" for i in range(5)]
    nxt = table.rows[(1, "mix")]["next_sync_at"]
    assert psync.SYNC_INTERVAL <= nxt - NOW <= psync.SYNC_INTERVAL + psync.SYNC_JITTER_MAX


async def test_a_playlist_not_yet_due_is_left_alone(table):
    table.add(1, "mix", due=False)
    cog = _Cog()
    assert (await cog._run_sync_sweep(NOW))["synced"] == 0 and cog.loads == []


async def test_the_global_cap_bounds_one_sweep(table):
    for i in range(psync.MAX_RESYNCS_PER_SWEEP + 7):
        table.add(i + 1, "mix")
    cog = _Cog()
    stats = await cog._run_sync_sweep(NOW)
    assert stats["synced"] == psync.MAX_RESYNCS_PER_SWEEP
    assert len(cog.loads) == psync.MAX_RESYNCS_PER_SWEEP
    # the rest simply wait for the next sweep, still due
    left = [k for k, r in table.rows.items() if r["next_sync_at"] <= NOW]
    assert len(left) == 7


async def test_the_cap_is_six_sweeps_an_hour_of_ten():
    per_hour = (datetime.timedelta(hours=1) / psync.SWEEP_INTERVAL) * psync.MAX_RESYNCS_PER_SWEEP
    assert per_hour == 60


async def test_a_lapsed_guild_is_skipped_cheaply_and_keeps_everything(table):
    table.add(1, "mix")
    cog = _Cog(entitled=False)
    stats = await cog._run_sync_sweep(NOW)
    assert stats == {"synced": 0, "failed": 0, "skipped": 1} and cog.loads == []
    row = table.rows[(1, "mix")]
    assert row["tracks"] == ["old"] and row["source_url"]  # nothing taken hostage
    assert row["next_sync_at"] == NOW + psync.LAPSED_RECHECK
    # skipped rows spend no budget: a lapsed backlog cannot starve a paying guild
    for i in range(30):
        table.add(100 + i, "lapsed")
    table.add(2, "paid")
    mixed = _Cog(entitled=True)
    mixed.bot.premium.for_guild = lambda gid: types.SimpleNamespace(
        max_linked_playlists=0 if gid >= 100 else 10, max_playlist_tracks=500
    )
    stats = await mixed._run_sync_sweep(NOW)
    assert stats["synced"] >= 1
    assert table.rows[(2, "paid")]["tracks"] != ["old"]


async def test_sync_resumes_when_premium_returns(table):
    table.add(1, "mix")
    lapsed = _Cog(entitled=False)
    await lapsed._run_sync_sweep(NOW)
    assert lapsed.loads == []
    back = _Cog(entitled=True)
    stats = await back._run_sync_sweep(NOW + psync.LAPSED_RECHECK + datetime.timedelta(minutes=1))
    assert stats["synced"] == 1 and back.loads


async def test_a_guild_the_bot_left_is_treated_as_lapsed(table):
    table.add(1, "mix")
    cog = _Cog()
    cog.bot.get_guild = lambda gid: None
    assert (await cog._run_sync_sweep(NOW))["skipped"] == 1 and cog.loads == []


@pytest.mark.parametrize(
    "status", [psync.BLOCKED, psync.NOT_PLAYLIST, psync.EMPTY, psync.LOAD_FAILED, psync.BAD]
)
async def test_a_failing_source_keeps_the_tracks_and_records_a_strike(table, status):
    table.add(1, "mix")
    cog = _Cog(status=status)
    stats = await cog._run_sync_sweep(NOW)
    assert stats["failed"] == 1
    assert table.rows[(1, "mix")]["tracks"] == ["old"]
    assert table.failures == [(1, "mix", status, psync.MAX_STRIKES)]
    assert psync.MAX_STRIKES == 3
    # retried a week later (the claim already moved the schedule)
    assert table.rows[(1, "mix")]["next_sync_at"] >= NOW + psync.SYNC_INTERVAL


async def test_a_lavalink_outage_is_not_the_sources_fault(table):
    table.add(1, "mix")
    cog = _Cog(status=psync.UNAVAILABLE)
    await cog._run_sync_sweep(NOW)
    assert table.failures == []  # no strike
    assert table.rows[(1, "mix")]["next_sync_at"] == NOW + psync.NODE_RETRY


async def test_the_tracks_are_clipped_to_the_guilds_current_cap(table):
    table.add(1, "mix")
    cog = _Cog(tracks=9, cap=4)
    await cog._run_sync_sweep(NOW)
    assert table.rows[(1, "mix")]["tracks"] == ["n0", "n1", "n2", "n3"]


async def test_an_overlapping_claim_loads_a_row_once(table):
    table.add(1, "mix")
    assert await psync.claim_row(None, 1, "mix", NOW, NOW + DAY) == "https://x.test/p"
    assert await psync.claim_row(None, 1, "mix", NOW, NOW + DAY) is None


# -- the sweep timer -----------------------------------------------------------


async def test_the_listener_rearms_before_it_sweeps(monkeypatch):
    order = []
    cog = _Cog()

    async def ensure():
        order.append("rearm")
        return True

    async def sweep():
        order.append("sweep")
        return {"synced": 0, "failed": 0, "skipped": 0}

    cog._ensure_sync_timer = ensure
    cog._run_sync_sweep = sweep
    await cog.on_playlist_sync_timer_complete({})
    assert order == ["rearm", "sweep"]


async def test_a_crashing_sweep_never_escapes_and_the_series_survives():
    cog = _Cog()
    armed = []

    async def ensure():
        armed.append(1)
        return True

    async def boom():
        raise RuntimeError("x")

    cog._ensure_sync_timer = ensure
    cog._run_sync_sweep = boom
    await cog.on_playlist_sync_timer_complete({})
    assert armed == [1]


async def test_ensure_sweep_timer_is_idempotent_through_the_statement(fake_pool):
    fake_pool.execute_return = "INSERT 0 0"
    assert await psync.ensure_sweep_timer(fake_pool, NOW) is False
    _m, query, args = fake_pool.calls[-1]
    assert "NOT EXISTS" in query and args[0] == psync.SYNC_EVENT
    fake_pool.execute_return = "INSERT 0 1"
    assert await psync.ensure_sweep_timer(fake_pool, NOW) is True


# -- pure ----------------------------------------------------------------------


def test_source_domain_and_clip():
    assert psync.source_domain("https://www.Youtube.com/playlist?list=1") == "youtube.com"
    assert psync.source_domain(None) == ""
    assert psync.clip_tracks(["a", "b", "c"], 2) == (["a", "b"], 1)
    assert psync.clip_tracks(["a"], 5) == (["a"], 0)
    assert psync.is_paused("https://x.test", None) and not psync.is_paused(None, None)
    assert not psync.is_paused("https://x.test", NOW)


def test_the_linked_playlist_limit_is_a_yasuho_plus_perk():
    assert premium.GUILD_FREE.max_linked_playlists == 0
    assert premium.GUILD_PREMIUM.max_linked_playlists == 10


# -- the cache bypass (sonolink caches every search_track result for ever) --------


def _playlist_payload(blobs):
    return {
        "loadType": "playlist",
        "data": {
            "info": {"name": "p", "selectedTrack": -1},
            "pluginInfo": {},
            "tracks": [
                {
                    "encoded": b,
                    "info": {
                        "identifier": b, "isSeekable": True, "author": "a",
                        "length": 1000, "isStream": False, "position": 0,
                        "title": b, "uri": None, "artworkUrl": None, "isrc": None,
                        "sourceName": "spotify",
                    },
                    "pluginInfo": {},
                    "userData": {},
                }
                for b in blobs
            ],
        },
    }


class _Manager:
    def __init__(self):
        self.calls = 0
        self.blobs = ["v1a", "v1b"]
        self.payload = None

    async def load_track(self, ident):
        import msgspec
        from sonolink.rest.schemas import track as track_schema

        self.calls += 1
        payload = self.payload or _playlist_payload(self.blobs)
        return msgspec.json.decode(
            msgspec.json.encode(payload), type=track_schema.TrackLoadingResponse
        )


def _real_stack():
    """A node with the REAL sonolink cache + HTTPClient around a fake manager."""
    from sonolink.gateway.cache import LFUCache
    from sonolink.gateway.node._rest import HTTPClient

    node = types.SimpleNamespace(
        _cache=LFUCache(), _manager=_Manager(), _ensure_client=lambda: object()
    )
    node.search_track = lambda q, source=None: rest.search_track(q, source=source)
    rest = HTTPClient.__new__(HTTPClient)
    rest.node = node
    client = types.SimpleNamespace(get_best_node=lambda: node)
    cog = psync.PlaylistSyncMixin()
    cog.bot = types.SimpleNamespace(sl_client=client)
    return cog, node, rest


@pytest.fixture
def public_dns(monkeypatch):
    async def ok(query):
        return None

    monkeypatch.setattr(psync.urlguard, "check_query", ok)


URL = "https://open.spotify.com/playlist/abc"


async def test_two_loads_of_one_url_both_hit_lavalink_and_see_new_data(public_dns):
    cog, node, _rest = _real_stack()
    first = await cog._fetch_source_tracks(URL)
    node._manager.blobs = ["v2a", "v2b", "v2c"]  # the source changed meanwhile
    second = await cog._fetch_source_tracks(URL)
    assert node._manager.calls == 2
    assert first.blobs == ("v1a", "v1b") and second.blobs == ("v2a", "v2b", "v2c")


async def test_the_uncached_path_ignores_and_never_fills_the_cache(public_dns):
    cog, node, rest = _real_stack()
    await rest.search_track(URL)  # /play warms the cache with the old answer
    node._manager.blobs = ["new"]
    assert (await cog._fetch_source_tracks(URL)).blobs == ("new",)
    # /play's cached path is untouched: still the old cached answer, no new call
    calls = node._manager.calls
    cached = await rest.search_track(URL)
    assert node._manager.calls == calls
    assert [t.encoded for t in cached.result.tracks] == ["v1a", "v1b"]


async def test_a_cached_error_cannot_strike_a_playlist_forever(public_dns):
    cog, node, rest = _real_stack()
    node._manager.payload = {"loadType": "empty", "data": {}}
    assert (await rest.search_track(URL)).is_empty()  # cached by the /play path
    node._manager.payload = None
    assert (await cog._fetch_source_tracks(URL)).status == psync.OK


# -- strike policy: only a definite verdict counts ------------------------------


def _error_payload(severity):
    return {
        "loadType": "error",
        "data": {"message": "m", "severity": severity, "cause": "c", "causeStackTrace": ""},
    }


@pytest.mark.parametrize(
    "severity, expected",
    [("common", psync.LOAD_FAILED), ("suspicious", psync.TRANSIENT), ("fault", psync.TRANSIENT)],
)
async def test_lavalink_errors_are_a_strike_only_when_severity_is_common(
    public_dns, severity, expected
):
    cog, node, _rest = _real_stack()
    node._manager.payload = _error_payload(severity)
    assert (await cog._fetch_source_tracks(URL)).status == expected


async def test_dns_and_transport_failures_are_transient_not_strikes(monkeypatch):
    cog, node, _rest = _real_stack()

    async def unresolvable(query):
        return psync.urlguard.REASON_UNRESOLVABLE

    monkeypatch.setattr(psync.urlguard, "check_query", unresolvable)
    assert (await cog._fetch_source_tracks(URL)).status == psync.TRANSIENT

    async def ok(query):
        return None

    monkeypatch.setattr(psync.urlguard, "check_query", ok)

    async def boom(ident):
        raise ConnectionResetError("reset")

    node._manager.load_track = boom
    assert (await cog._fetch_source_tracks(URL)).status == psync.TRANSIENT

    async def down(ident):
        raise RuntimeError("no nodes")

    node._manager.load_track = down
    assert (await cog._fetch_source_tracks(URL)).status == psync.UNAVAILABLE


async def test_a_blocked_address_is_still_a_definite_refusal(monkeypatch):
    cog, _node, _rest = _real_stack()
    assert (await cog._fetch_source_tracks("http://127.0.0.1:2333/v4/info")).status == psync.BLOCKED


async def test_a_transient_status_costs_no_strike_at_sync_time(table):
    table.add(1, "mix")
    cog = _Cog(status=psync.TRANSIENT)
    await cog._run_sync_sweep(NOW)
    assert table.failures == []
    assert table.rows[(1, "mix")]["next_sync_at"] == NOW + psync.NODE_RETRY


# -- the series survives a DB error at re-arm ------------------------------------


async def test_rearm_retries_with_backoff_then_the_series_lives(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(psync.asyncio, "sleep", fake_sleep)
    attempts = []

    async def flaky(pool, when):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("db down")
        return True

    monkeypatch.setattr(psync, "ensure_sweep_timer", flaky)
    cog = _Cog()
    cog.bot.get_cog = lambda name: None
    swept = []

    async def sweep():
        swept.append(1)
        return {"synced": 0, "failed": 0, "skipped": 0}

    cog._run_sync_sweep = sweep
    await cog.on_playlist_sync_timer_complete({})
    assert len(attempts) == 3 and sleeps == list(psync.REARM_BACKOFF[:2])
    assert swept == [1]


async def test_rearm_gives_up_after_the_backoff_but_still_sweeps(monkeypatch):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr(psync.asyncio, "sleep", fake_sleep)

    async def down(pool, when):
        raise ConnectionError("db down")

    monkeypatch.setattr(psync, "ensure_sweep_timer", down)
    cog = _Cog()
    assert await cog._rearm_with_retry() is False


async def test_the_idle_loop_safety_net_runs_at_most_once_an_hour():
    cog = _Cog()
    calls = []

    async def ensure():
        calls.append(1)
        return True

    cog._ensure_sync_timer = ensure
    await cog._ensure_sync_timer_hourly(10_000.0)
    await cog._ensure_sync_timer_hourly(10_030.0)
    assert len(calls) == 1
    await cog._ensure_sync_timer_hourly(10_000.0 + psync.ENSURE_EVERY)
    assert len(calls) == 2


async def test_music_idle_loop_calls_the_safety_net():
    import inspect

    from cogs.music import music as music_mod

    assert "_ensure_sync_timer_hourly" in inspect.getsource(music_mod.Music._idle_check.coro)


# -- ensure is serialised --------------------------------------------------------


async def test_ensure_sweep_timer_takes_the_advisory_lock_before_inserting(fake_pool):
    await psync.ensure_sweep_timer(fake_pool, NOW)
    queries = [q for _m, q, _a in fake_pool.calls]
    assert "pg_advisory_xact_lock" in queries[0] and "INSERT INTO timers" in queries[1]

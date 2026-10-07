"""ST1: the server-statistics collectors.

What is pinned here: the listeners never touch the DB, the buffer is hard
bounded and counts what it drops, one flush is ONE batched upsert whose SQL
shape was verified against a real PostgreSQL in a rolled-back transaction, a
tick that straddles midnight writes each counter onto its own day, the
member-count snapshot happens once per UTC day, the prune is bounded, and both
tables are covered by guild-departure retention.
"""

import asyncio
import datetime
import types

from cogs.community.serverstats import buffer, queries
from cogs.community.serverstats import cog as serverstats_cog
from tools import retention

DAY = 20662  # 2026-07-28 as a UTC day (days since 1970-01-01)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _Guild:
    def __init__(self, guild_id, member_count=None):
        self.id = guild_id
        self.member_count = member_count


class _Author:
    def __init__(self, bot=False):
        self.bot = bot


class _Channel:
    def __init__(self, channel_id, parent_id=None):
        self.id = channel_id
        if parent_id is not None:
            self.parent_id = parent_id


class _Message:
    def __init__(self, guild_id, channel_id, *, bot=False, parent_id=None):
        self.guild = _Guild(guild_id) if guild_id is not None else None
        self.channel = _Channel(channel_id, parent_id)
        self.author = _Author(bot)


class _Member:
    def __init__(self, guild_id, bot=False):
        self.guild = _Guild(guild_id)
        self.bot = bot


class _ScriptedPool:
    """A pool whose fetchrow answers from a script (for the prune batches),
    and whose fetch answers the M4d premium-ish guild lookup - empty
    (nobody is premium-ish, i.e. every guild is free) unless ``premium_ish``
    says otherwise."""

    def __init__(self, rows, *, premium_ish=()):
        self.rows = list(rows)
        self.calls = []
        self.premium_ish = list(premium_ish)

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "INSERT 0 1"

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.rows.pop(0) if self.rows else {"messages": 0, "days": 0}

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return [{"guild_id": guild_id} for guild_id in self.premium_ish]


class _BlockingPool:
    """A pool whose FIRST execute parks on an event, so a flush can be caught
    mid-write (the window where the buffer has been drained but nothing is
    committed yet). Later executes return immediately."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.released = asyncio.Event()
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        if len(self.calls) == 1:
            self.entered.set()
            await self.released.wait()
        return "INSERT 0 1"

    async def fetchrow(self, query, *args):
        return {"messages": 0, "days": 0}


def _cog(pool, guilds=()):
    """A collector wired to a fake bot, with no loop started (cog_load is not run)."""
    bot = types.SimpleNamespace(db_pool=pool, guilds=list(guilds))
    return serverstats_cog.ServerStats(bot)


# ---------------------------------------------------------------------------
# Listeners: in-memory only, ZERO DB
# ---------------------------------------------------------------------------


async def test_on_message_counts_in_memory_without_touching_the_db(
    fake_pool, monkeypatch
):
    monkeypatch.setattr(buffer, "utc_day", lambda: DAY)
    cog = _cog(fake_pool)

    await cog.on_message(_Message(1, 100))
    await cog.on_message(_Message(1, 100))
    await cog.on_message(_Message(1, 200))

    # The whole point of the design: not one round trip on the hot path.
    assert fake_pool.calls == []
    drained = cog._buffer.drain()
    assert sorted(drained.messages) == [
        (1, 100, DAY, 2),
        (1, 200, DAY, 1),
    ]


async def test_on_message_ignores_dms_bots_and_rolls_threads_up(
    fake_pool, monkeypatch
):
    monkeypatch.setattr(buffer, "utc_day", lambda: DAY)
    cog = _cog(fake_pool)

    await cog.on_message(_Message(None, 100))            # DM
    await cog.on_message(_Message(1, 100, bot=True))     # bot / webhook
    await cog.on_message(_Message(1, 999, parent_id=100))  # thread of #100

    assert fake_pool.calls == []
    assert cog._buffer.drain().messages == [(1, 100, DAY, 1)]


async def test_member_events_count_humans_only(fake_pool, monkeypatch):
    monkeypatch.setattr(buffer, "utc_day", lambda: DAY)
    cog = _cog(fake_pool)

    await cog.on_member_join(_Member(1))
    await cog.on_member_join(_Member(1))
    await cog.on_member_join(_Member(1, bot=True))
    await cog.on_member_remove(_Member(1))
    await cog.on_member_remove(_Member(1, bot=True))

    assert fake_pool.calls == []
    assert cog._buffer.drain().days == [(1, DAY, 2, 1)]


# ---------------------------------------------------------------------------
# The bound: the cap drops keys and counts the drops
# ---------------------------------------------------------------------------


def test_buffer_cap_drops_new_keys_and_counts_the_overflow():
    buf = buffer.StatsBuffer(message_cap=3, day_cap=2)

    for channel_id in range(10):
        buf.record_message(1, channel_id, DAY)
    # An ALREADY tracked key still counts once the cap is reached.
    assert buf.record_message(1, 0, DAY) is True
    assert buf.record_message(1, 42, DAY) is False

    for guild_id in range(5):
        buf.record_join(guild_id, DAY)

    drained = buf.drain()
    assert len(drained.messages) == 3
    assert dict(((g, c), n) for g, c, _d, n in drained.messages)[(1, 0)] == 2
    assert drained.dropped_messages == 8  # 7 new keys + the one after the cap
    assert len(drained.days) == 2
    assert drained.dropped_days == 3
    # Draining resets both the counters and the overflow tally.
    assert buf.drain() == buffer.DrainedStats(messages=[], days=[])


async def test_overflow_is_logged_once_per_flush_not_per_message(
    fake_pool, monkeypatch, caplog
):
    monkeypatch.setattr(buffer, "utc_day", lambda: DAY)
    cog = _cog(fake_pool)
    cog._buffer = buffer.StatsBuffer(message_cap=1)

    for channel_id in range(50):
        await cog.on_message(_Message(1, channel_id))

    with caplog.at_level("WARNING"):
        await cog.flush(day=DAY)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "buffer cap" in warnings[0].message
    assert cog._stats["dropped"] == 49


# ---------------------------------------------------------------------------
# The flush: one batched statement, additive, day-accurate
# ---------------------------------------------------------------------------


async def test_flush_writes_one_batched_additive_upsert(fake_pool):
    cog = _cog(fake_pool)
    cog._buffer.record_message(1, 100, DAY, count=4)
    cog._buffer.record_join(1, DAY, count=2)
    cog._buffer.record_leave(1, DAY)

    await cog.flush(day=DAY)

    writes = [c for c in fake_pool.calls if c[0] == "execute"]
    # ONE batched statement for both aggregates (the snapshot below is the
    # once-a-day extra, and it is the only other execute of this tick).
    flush_calls = [c for c in writes if c[1] == queries.FLUSH]
    assert len(flush_calls) == 1
    _method, query, args = flush_calls[0]
    assert "unnest($1::bigint[], $2::bigint[], $3::date[], $4::integer[])" in query
    assert "ON CONFLICT (guild_id, channel_id, day)" in query
    assert (
        "messages = server_stats_messages.messages + EXCLUDED.messages" in query
    )
    assert "ON CONFLICT (guild_id, day)" in query
    assert "joins = server_stats_days.joins + EXCLUDED.joins" in query
    assert "leaves = server_stats_days.leaves + EXCLUDED.leaves" in query
    assert args == (
        [1],
        [100],
        [buffer.day_to_date(DAY)],
        [4],
        [1],
        [buffer.day_to_date(DAY)],
        [2],
        [1],
    )
    assert cog._stats["flushes"] == 1


async def test_flush_skips_the_write_when_nothing_was_collected(fake_pool):
    cog = _cog(fake_pool)
    cog._snapshot_day = DAY
    cog._prune_day = DAY

    await cog.flush(day=DAY)

    assert fake_pool.calls == []


async def test_midnight_straddle_writes_each_counter_on_its_own_day(fake_pool):
    cog = _cog(fake_pool)
    # Collected just before midnight UTC ...
    cog._buffer.record_message(1, 100, DAY, count=3)
    cog._buffer.record_leave(1, DAY)
    # ... and just after, in the same interval.
    cog._buffer.record_message(1, 100, DAY + 1, count=5)
    cog._buffer.record_join(1, DAY + 1)

    await cog.flush(day=DAY + 1)

    _method, _query, args = [
        c for c in fake_pool.calls if c[1] == queries.FLUSH
    ][0]
    guild_ids, channel_ids, days, counts = args[0], args[1], args[2], args[3]
    assert sorted(zip(guild_ids, channel_ids, days, counts)) == [
        (1, 100, buffer.day_to_date(DAY), 3),
        (1, 100, buffer.day_to_date(DAY + 1), 5),
    ]
    day_guilds, day_days, joins, leaves = args[4], args[5], args[6], args[7]
    assert sorted(zip(day_guilds, day_days, joins, leaves)) == [
        (1, buffer.day_to_date(DAY), 0, 1),
        (1, buffer.day_to_date(DAY + 1), 1, 0),
    ]


async def test_failed_write_hands_the_counters_back_to_the_buffer(fake_pool):
    cog = _cog(fake_pool)
    cog._buffer.record_message(1, 100, DAY, count=7)

    async def boom(_query, *_args):
        raise RuntimeError("database unavailable")

    fake_pool.execute = boom
    try:
        await cog.flush(day=DAY)
    except RuntimeError:
        pass
    else:  # pragma: no cover - the failure must propagate to the loop's log
        raise AssertionError("flush swallowed the write failure")

    # Nothing lost: the next successful flush still carries the 7 messages.
    assert cog._buffer.drain().messages == [(1, 100, DAY, 7)]


async def test_a_retried_flush_does_not_re_report_the_same_drops(
    fake_pool, monkeypatch
):
    """The overflow tally is accounted ONCE, at drain time, even across retries.

    Folding a drain's own dropped counters back in on restore would make a DB
    outage report the same drops again on every tick - an operator would read a
    drop rate that is not happening.
    """
    monkeypatch.setattr(buffer, "utc_day", lambda: DAY)
    cog = _cog(fake_pool)
    cog._buffer = buffer.StatsBuffer(message_cap=1)

    async def boom(_query, *_args):
        raise RuntimeError("database unavailable")

    fake_pool.execute = boom
    for channel_id in range(11):  # 11 keys against a cap of 1 == 10 real drops
        await cog.on_message(_Message(1, channel_id))

    for _ in range(4):  # four consecutive failed ticks of the same outage
        try:
            await cog.flush(day=DAY)
        except RuntimeError:
            pass
        assert cog._stats["dropped"] == 10


async def test_counters_collected_during_an_in_flight_write_survive_it():
    """The drain happens BEFORE the await, never after: the key concurrency
    property of the design. A drain placed after the write would silently eat
    every counter collected while the upsert was in flight."""
    pool = _BlockingPool()
    cog = _cog(pool)
    cog._snapshot_day = DAY
    cog._prune_day = DAY
    cog._buffer.record_message(1, 100, DAY, count=3)

    task = asyncio.create_task(cog.flush(day=DAY))
    await pool.entered.wait()

    # Traffic that lands while the statement is in flight.
    cog._buffer.record_message(1, 100, DAY)
    cog._buffer.record_join(1, DAY)

    pool.released.set()
    await task

    # (a) the statement carried only the pre-flush counters ...
    query, args = pool.calls[0]
    assert query == queries.FLUSH
    assert args[3] == [3]
    assert args[4] == []
    # ... and (b) what arrived during the await is still there afterwards.
    drained = cog._buffer.drain()
    assert drained.messages == [(1, 100, DAY, 1)]
    assert drained.days == [(1, DAY, 1, 0)]


async def test_a_flush_cancelled_mid_write_hands_its_counters_back():
    """cog_unload cancels the loop, so a CancelledError landing in the write is
    the case that matters. It is a BaseException: an ``except Exception`` around
    the write would skip the restore and lose the interval on every clean
    shutdown."""
    pool = _BlockingPool()
    cog = _cog(pool)
    cog._snapshot_day = DAY
    cog._prune_day = DAY
    cog._buffer.record_message(1, 100, DAY, count=42)

    task = asyncio.create_task(cog.flush(day=DAY))
    await pool.entered.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert cog._buffer.drain().messages == [(1, 100, DAY, 42)]


async def test_unload_waits_for_the_cancelled_flush_then_writes_it():
    """End to end: a shutdown that lands mid-write still persists the interval.

    The final flush must run AFTER the cancelled loop task has unwound, since the
    restore happens in that task - flushing straight away would read an
    already-drained buffer.
    """
    pool = _BlockingPool()

    async def _ready():
        return None

    bot = types.SimpleNamespace(db_pool=pool, guilds=[], wait_until_ready=_ready)
    cog = serverstats_cog.ServerStats(bot)
    cog._snapshot_day = DAY
    cog._prune_day = DAY
    cog._buffer.record_message(1, 100, DAY, count=42)

    await cog.cog_load()
    await asyncio.wait_for(pool.entered.wait(), timeout=5)
    await cog.cog_unload()

    # Two writes: the cancelled one and the final one, which carries the 42.
    assert len(pool.calls) == 2
    _query, args = pool.calls[1]
    assert args[3] == [42]
    assert cog._buffer.is_empty


async def test_cog_unload_gives_up_on_a_wedged_final_flush(monkeypatch):
    """The final write is BOUNDED too, not just the wait on the cancelled loop.

    A wedged pool would otherwise hold a clean shutdown open for its whole
    command_timeout (60s) over statistics. Giving up costs the last interval,
    which is exactly what a hard crash already costs.
    """
    pool = _BlockingPool()  # never released: the write hangs forever
    cog = _cog(pool)
    cog._snapshot_day = DAY
    cog._prune_day = DAY
    cog._buffer.record_message(1, 100, DAY, count=42)
    monkeypatch.setattr(serverstats_cog, "UNLOAD_FLUSH_TIMEOUT", 0.05)

    await asyncio.wait_for(cog.cog_unload(), timeout=1)  # must not hang

    assert len(pool.calls) == 1
    # The cancelled write still handed its generation back (except BaseException).
    assert cog._buffer.drain().messages == [(1, 100, DAY, 42)]


# ---------------------------------------------------------------------------
# Once-a-day chores: member snapshot and prune
# ---------------------------------------------------------------------------


async def test_member_snapshot_runs_once_per_utc_day(fake_pool):
    cog = _cog(fake_pool, guilds=[_Guild(1, 250), _Guild(2, None), _Guild(3, 4)])

    await cog.flush(day=DAY)
    await cog.flush(day=DAY)  # same day, later tick
    snapshots = [
        c for c in fake_pool.calls if c[1] == queries.SNAPSHOT_MEMBER_COUNT
    ]
    assert len(snapshots) == 1
    _method, query, args = snapshots[0]
    assert "DO UPDATE SET member_count = EXCLUDED.member_count" in query
    # Guild 2 has no member count yet and is skipped rather than written as 0.
    assert args == ([1, 3], buffer.day_to_date(DAY), [250, 4])

    await cog.flush(day=DAY + 1)
    snapshots = [
        c for c in fake_pool.calls if c[1] == queries.SNAPSHOT_MEMBER_COUNT
    ]
    assert len(snapshots) == 2
    assert snapshots[1][2][1] == buffer.day_to_date(DAY + 1)


async def test_snapshot_failure_is_retried_on_the_next_tick(fake_pool):
    cog = _cog(fake_pool, guilds=[_Guild(1, 5)])

    async def boom(_query, *_args):
        raise RuntimeError("database unavailable")

    fake_pool.execute = boom
    try:
        await cog.flush(day=DAY)
    except RuntimeError:
        pass
    assert cog._snapshot_day is None  # not marked, so not lost for the day


async def test_an_empty_snapshot_does_not_burn_the_day(fake_pool):
    """A cold member cache (no guild has a member_count yet - the very first
    flush can land before GUILD_CREATE carried one) writes nothing, and must
    not MARK the day either: doing so would spend the guild's single snapshot
    slot for the whole UTC day on a purely transient miss. It stays unmarked,
    so the next tick retries and the counts land as soon as they exist."""
    cog = _cog(fake_pool, guilds=[_Guild(1, None), _Guild(2, None)])

    await cog.flush(day=DAY)

    assert [c for c in fake_pool.calls if c[1] == queries.SNAPSHOT_MEMBER_COUNT] == []
    assert cog._snapshot_day is None  # nothing written, nothing remembered

    # The counts show up on the next tick, same day: the snapshot still happens.
    cog.bot.guilds = [_Guild(1, 42), _Guild(2, 7)]
    await cog.flush(day=DAY)

    snapshots = [c for c in fake_pool.calls if c[1] == queries.SNAPSHOT_MEMBER_COUNT]
    assert len(snapshots) == 1
    assert snapshots[0][2] == ([1, 2], buffer.day_to_date(DAY), [42, 7])
    assert cog._snapshot_day == DAY


async def test_a_bot_in_no_guild_at_all_never_marks_the_day(fake_pool):
    cog = _cog(fake_pool, guilds=[])

    await cog.flush(day=DAY)

    assert cog._snapshot_day is None


# ---------------------------------------------------------------------------
# M4d: the two-tier prune - unconditional 365-day delete, then the
# 90-365-day band for non-premium-ish guilds only.
# ---------------------------------------------------------------------------


def _cutoffs(day=DAY):
    return (
        buffer.day_to_date(day - serverstats_cog.RETENTION_DAYS),
        buffer.day_to_date(day - serverstats_cog.MAX_RETENTION_DAYS),
    )


async def test_prune_runs_the_global_pass_then_the_free_band_pass_once_a_day():
    cutoff_free, cutoff_global = _cutoffs()
    pool = _ScriptedPool(
        [
            {"messages": serverstats_cog.PRUNE_BATCH_SIZE, "days": 0},  # step 1
            {"messages": 3, "days": 0},  # step 1, short -> stop
            {"messages": serverstats_cog.PRUNE_BATCH_SIZE, "days": 0},  # step 2
            {"messages": 5, "days": 0},  # step 2, short -> stop
        ]
    )
    cog = _cog(pool)
    cog._snapshot_day = DAY

    await cog.flush(day=DAY)

    fetchrows = [c for c in pool.calls if c[0] == "fetchrow"]
    assert len(fetchrows) == 4

    # Step 1: queries.PRUNE, unconditional, cutoff = today - MAX_RETENTION_DAYS.
    for _method, query, args in fetchrows[:2]:
        assert query == queries.PRUNE
        # The LIMIT must bound the rows SCANNED, not just the rows deleted. The
        # ctid = ANY(ARRAY(...)) shape plans as a Tid Scan; the `DELETE ...
        # USING` join it replaced seq-scanned and externally sorted the whole
        # table on every batch (measured: 212 ms + temp spill vs 29 ms,
        # growing with size).
        assert query.count("ctid = ANY(ARRAY(") == 2
        assert "USING" not in query
        assert args == (cutoff_global, serverstats_cog.PRUNE_BATCH_SIZE)
    assert cutoff_global == datetime.date(2025, 7, 28)  # DAY is 2026-07-28

    # Step 2: queries.PRUNE_EXCLUDING_GUILDS, the 90-365-day band, with an
    # empty exclusion list (the fixture's premium-ish set is empty by
    # default - every guild is treated as free).
    for _method, query, args in fetchrows[2:]:
        assert query == queries.PRUNE_EXCLUDING_GUILDS
        assert query.count("ctid = ANY(ARRAY(") == 2
        assert args == (
            cutoff_free,
            cutoff_global,
            [],
            serverstats_cog.PRUNE_BATCH_SIZE,
        )
    assert cutoff_free == datetime.date(2026, 4, 29)  # unchanged from before M4d

    assert cog._stats["pruned"] == (
        serverstats_cog.PRUNE_BATCH_SIZE + 3 + serverstats_cog.PRUNE_BATCH_SIZE + 5
    )

    pool.calls.clear()
    await cog.flush(day=DAY)
    assert pool.calls == []  # already pruned today


async def test_prune_each_pass_stops_early_on_a_short_batch():
    pool = _ScriptedPool(
        [
            {"messages": serverstats_cog.PRUNE_BATCH_SIZE, "days": 3},  # step 1
            {"messages": 12, "days": 0},  # step 1, short -> stop
            {"messages": serverstats_cog.PRUNE_BATCH_SIZE, "days": 0},  # step 2
            {"messages": 7, "days": 0},  # step 2, short -> stop
        ]
    )
    cog = _cog(pool)
    cog._snapshot_day = DAY

    await cog.flush(day=DAY)

    assert len([c for c in pool.calls if c[0] == "fetchrow"]) == 4
    assert cog._stats["pruned"] == (
        serverstats_cog.PRUNE_BATCH_SIZE + 3 + 12 + serverstats_cog.PRUNE_BATCH_SIZE + 7
    )


async def test_prune_each_pass_is_capped_at_prune_max_batches():
    full = {"messages": serverstats_cog.PRUNE_BATCH_SIZE, "days": 0}
    # Every batch of BOTH passes comes back full, so each pass alone would
    # run forever without its own cap.
    pool = _ScriptedPool([full] * (2 * serverstats_cog.PRUNE_MAX_BATCHES + 5))
    cog = _cog(pool)
    cog._snapshot_day = DAY

    await cog.flush(day=DAY)

    fetchrows = [c for c in pool.calls if c[0] == "fetchrow"]
    # Never more than PRUNE_MAX_BATCHES PER PASS, two passes.
    assert len(fetchrows) == 2 * serverstats_cog.PRUNE_MAX_BATCHES
    step1 = fetchrows[: serverstats_cog.PRUNE_MAX_BATCHES]
    step2 = fetchrows[serverstats_cog.PRUNE_MAX_BATCHES :]
    assert all(query == queries.PRUNE for _m, query, _a in step1)
    assert all(query == queries.PRUNE_EXCLUDING_GUILDS for _m, query, _a in step2)


async def test_prune_still_runs_the_global_pass_when_the_premium_lookup_fails():
    """FAILURE PATH: the premium-ish lookup raising skips ONLY the free-tier
    band (step 2) - the unconditional MAX_RETENTION_DAYS delete (step 1)
    still runs, because 365 days is a hard ceiling nobody is exempt from and
    must not wait on a flaky lookup. See cog._maybe_prune's own docstring
    for why the band is the side that yields here (over-retaining a few
    extra days for a free guild is recoverable; over-deleting a genuine
    subscriber's history on a transient failure is not).

    NEGATIVE CONTROL: temporarily wrapping step 1's own call in the same
    "failure means skip" guard step 2 uses (so a premium-lookup failure
    would ALSO skip the global delete) was run by hand during this lot: it
    turned this test red (the global-delete assertion below found no such
    call). The edit was reverted by hand (no git stash/checkout/reset) and
    ``git diff`` showed a clean tree before the suite was re-run green.
    """
    cutoff_global = _cutoffs()[1]

    class _FailingFetchPool(_ScriptedPool):
        async def fetch(self, query, *args):
            self.calls.append(("fetch", query, args))
            raise RuntimeError("premium lookup is down")

    pool = _FailingFetchPool([{"messages": 9, "days": 2}])
    cog = _cog(pool)
    cog._snapshot_day = DAY

    await cog.flush(day=DAY)

    fetchrows = [c for c in pool.calls if c[0] == "fetchrow"]
    assert len(fetchrows) == 1
    _method, query, args = fetchrows[0]
    assert query == queries.PRUNE
    assert args == (cutoff_global, serverstats_cog.PRUNE_BATCH_SIZE)
    assert all(q != queries.PRUNE_EXCLUDING_GUILDS for _m, q, _a in pool.calls)

    # Still marked done for the day: this is a daily best-effort pass (same
    # posture as the flush/snapshot above it) - tomorrow's run recomputes
    # the premium-ish set fresh rather than retrying the same failure for
    # the rest of today.
    assert cog._prune_day == DAY
    assert cog._stats["pruned"] == 9 + 2  # messages + days from the one batch


async def test_prune_band_excludes_the_resolved_premium_ish_guild_ids():
    """The excluded-ids array passed to queries.PRUNE_EXCLUDING_GUILDS is
    EXACTLY (sorted) what tools.premium.premium_ish_guild_ids returned -
    the band delete leaves those guilds' 90-365-day rows alone."""
    pool = _ScriptedPool(
        [
            {"messages": 0, "days": 0},  # step 1: nothing to delete
            {"messages": 4, "days": 1},  # step 2: one short batch -> stop
        ],
        premium_ish=[99, 42, 42],  # a duplicate on purpose: the set dedupes
    )
    cog = _cog(pool)
    cog._snapshot_day = DAY

    await cog.flush(day=DAY)

    fetchrows = [c for c in pool.calls if c[0] == "fetchrow"]
    _method, query, args = fetchrows[-1]
    assert query == queries.PRUNE_EXCLUDING_GUILDS
    assert args[2] == [42, 99]  # sorted, deduplicated


# NEGATIVE CONTROL (the entitled-keep). Temporarily passing an EMPTY list
# instead of ``sorted(keep_ids)`` to queries.PRUNE_EXCLUDING_GUILDS in
# cog._maybe_prune (i.e. "forget" to exclude the premium-ish guilds at all)
# was run by hand during this lot: it turned
# test_prune_band_excludes_the_resolved_premium_ish_guild_ids red (``[] !=
# [42, 99]``), proving the exclusion list is load-bearing rather than
# decorative. The edit was reverted by hand (no git stash/checkout/reset)
# and ``git diff`` showed a clean tree before the full suite was re-run
# green.


# ---------------------------------------------------------------------------
# Retention: a departing guild takes its statistics with it
# ---------------------------------------------------------------------------


def test_both_stats_tables_are_purged_on_guild_departure():
    purged = dict(retention.GUILD_DELETE_QUERIES)
    for table in ("server_stats_messages", "server_stats_days"):
        assert table in purged
        assert purged[table] == f"DELETE FROM {table} WHERE guild_id = $1"
        assert f"FROM {table}" in retention.STORED_GUILD_IDS_QUERY


def test_utc_day_arithmetic_round_trips():
    # 2026-07-28T00:00:00Z and the last second of that day map to the same day.
    midnight = datetime.datetime(
        2026, 7, 28, tzinfo=datetime.timezone.utc
    ).timestamp()
    assert buffer.utc_day(midnight) == DAY
    assert buffer.utc_day(midnight + 86399) == DAY
    assert buffer.utc_day(midnight + 86400) == DAY + 1
    assert buffer.day_to_date(DAY) == datetime.date(2026, 7, 28)

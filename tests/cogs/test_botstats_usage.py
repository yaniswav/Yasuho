"""BS2: the PERSISTED half of the ?botstats usage counters.

cogs/system/usage_stats.py owns the bounded buffer, the additive upsert, the
bounded window reads and the retention prune; cogs/system/botstats.py wires them
into the completion listeners, one flush loop and the Usage page.

Covered here, in order of how much it would hurt to get wrong:

1. The FLUSH CONTRACT. Drain before the await, hand the generation back on ANY
   failure - including the CancelledError that cog_unload throws into an
   in-flight write - and never drain twice. This is the exact incident
   cogs/community/serverstats/cog.py documents in its own ``except
   BaseException``; the tests below fail if this module weakens it to ``except
   Exception``, or if teardown flushes without waiting for the cancelled task.
2. THE DAY IS CAPTURED AT INCREMENT TIME. A flush at 00:02 must write the 23:59
   completions onto the day they happened on, never migrate them into the new
   day.
3. The upsert is ADDITIVE and the windows are honest: an unavailable read says
   so, an empty table says so, and a 30-day heading that only has six days of
   history behind it says THAT too. None of the three is ever a zero.
4. The table is GLOBAL AGGREGATES: no guild_id, no user_id, which is why the two
   structural guards (guild purge / mydata export) correctly ignore it.

Everything here is offline: no bot, no pool, no Discord.
"""

import asyncio
import datetime
import os
import re

import pytest

from cogs.system import botstats, usage_stats

UTC = datetime.timezone.utc
DAY = datetime.date(2026, 7, 31)
NEXT_DAY = datetime.date(2026, 8, 1)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _Pool:
    """asyncpg pool stand-in for the writer side (execute + the prune fetchrow).

    ``on_execute`` is called with the 1-based execute index, so a test can make
    exactly the first write hang or fail.
    """

    def __init__(self, *, on_execute=None, prune_batches=None):
        self.calls = []
        self._on_execute = on_execute
        self._prune_batches = list(prune_batches or [])

    @property
    def executes(self):
        return [call for call in self.calls if call[0] == "execute"]

    @property
    def fetchrows(self):
        return [call for call in self.calls if call[0] == "fetchrow"]

    async def execute(self, query, *args, **kwargs):
        self.calls.append(("execute", query, args))
        if self._on_execute is not None:
            await self._on_execute(len(self.executes))

    async def fetchrow(self, query, *args, **kwargs):
        self.calls.append(("fetchrow", query, args))
        if self._prune_batches:
            return {"rows": self._prune_batches.pop(0)}
        return {"rows": 0}

    async def fetch(self, query, *args, **kwargs):
        self.calls.append(("fetch", query, args))
        return []


class _Bot:
    def __init__(self, pool):
        self.db_pool = pool

    async def wait_until_ready(self):
        return None


def _cog(pool=None):
    """A BotStats cog built without add_cog (no listeners, no loop started)."""
    return botstats.BotStats(_Bot(pool if pool is not None else _Pool()))


def _ctx(name, *, slash=False):
    return type(
        "Ctx",
        (),
        {
            "command": type("Cmd", (), {"qualified_name": name})(),
            "interaction": object() if slash else None,
        },
    )()


def _app_command(name, *, hybrid=False):
    attrs = {"qualified_name": name}
    if hybrid:
        attrs["__commands_is_hybrid_app_command__"] = True
    return type("AppCmd", (), attrs)()


def _freeze_day(monkeypatch, day):
    monkeypatch.setattr(botstats.usage_stats, "utc_today", lambda now=None: day)


def _flat(sections):
    return "\n".join(
        heading + "\n" + "\n".join(lines) for heading, lines in sections
    )


# ---------------------------------------------------------------------------
# utc_today: the day a count belongs to
# ---------------------------------------------------------------------------
def test_utc_today_is_the_utc_calendar_day():
    moment = datetime.datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC)
    assert usage_stats.utc_today(moment) == datetime.date(2026, 7, 31)


def test_utc_today_converts_a_non_utc_clock_before_taking_the_date():
    """23:30 in UTC+2 is already the NEXT UTC day: reading .date() off a local
    datetime would file the count one day early."""
    east = datetime.timezone(datetime.timedelta(hours=2))
    moment = datetime.datetime(2026, 7, 31, 23, 30, tzinfo=east)
    assert usage_stats.utc_today(moment) == datetime.date(2026, 7, 31)
    assert usage_stats.utc_today(
        datetime.datetime(2026, 8, 1, 1, 30, tzinfo=east)
    ) == datetime.date(2026, 7, 31)


# ---------------------------------------------------------------------------
# The buffer
# ---------------------------------------------------------------------------
def test_buffer_counts_per_day_and_command_split_by_surface():
    buffer = usage_stats.UsageBuffer()
    buffer.record(DAY, "play")
    buffer.record(DAY, "play", slash=True)
    buffer.record(NEXT_DAY, "play")
    buffer.record(DAY, "rank", slash=True)
    assert sorted(buffer.drain().rows) == [
        (DAY, "play", 1, 1),
        (DAY, "rank", 0, 1),
        (NEXT_DAY, "play", 1, 0),
    ]


def test_buffer_ignores_an_empty_command_name():
    buffer = usage_stats.UsageBuffer()
    assert buffer.record(DAY, "") is False
    assert buffer.record(DAY, None) is False
    assert buffer.is_empty


def test_buffer_truncates_an_overlong_command_name():
    buffer = usage_stats.UsageBuffer()
    buffer.record(DAY, "x" * 500)
    ((_day, name, _prefix, _slash),) = buffer.drain().rows
    assert len(name) == usage_stats.COMMAND_NAME_LIMIT


def test_buffer_drops_new_keys_at_the_cap_but_keeps_counting_known_ones():
    buffer = usage_stats.UsageBuffer(cap=2)
    assert buffer.record(DAY, "a") is True
    assert buffer.record(DAY, "b") is True
    assert buffer.record(DAY, "c") is False  # dropped, not stored
    assert buffer.record(DAY, "a") is True  # known key still counts
    drained = buffer.drain()
    assert drained.dropped == 1
    assert sorted(drained.rows) == [(DAY, "a", 2, 0), (DAY, "b", 1, 0)]


def test_drain_detaches_and_resets_the_live_counters():
    buffer = usage_stats.UsageBuffer()
    buffer.record(DAY, "play")
    drained = buffer.drain()
    assert buffer.is_empty
    assert buffer.key_count == 0
    # Counting resumes into a FRESH generation while the flush is in flight.
    buffer.record(DAY, "play")
    assert buffer.drain().rows == [(DAY, "play", 1, 0)]
    assert drained.rows == [(DAY, "play", 1, 0)]


def test_drain_reports_and_clears_the_overflow_tally():
    buffer = usage_stats.UsageBuffer(cap=1)
    buffer.record(DAY, "a")
    buffer.record(DAY, "b")
    assert buffer.drain().dropped == 1
    assert buffer.drain().dropped == 0


def test_restore_folds_a_failed_flush_back_in_with_both_surfaces():
    buffer = usage_stats.UsageBuffer()
    buffer.record(DAY, "play", count=3)
    buffer.record(DAY, "play", slash=True, count=4)
    drained = buffer.drain()
    buffer.record(DAY, "play")  # counted while the flush was in flight
    buffer.restore(drained)
    assert buffer.drain().rows == [(DAY, "play", 4, 4)]


def test_restore_still_respects_the_cap():
    buffer = usage_stats.UsageBuffer(cap=1)
    buffer.record(DAY, "a")
    drained = buffer.drain()
    buffer.record(DAY, "b")  # the fresh generation took the only slot
    buffer.restore(drained)
    restored = buffer.drain()
    assert restored.rows == [(DAY, "b", 1, 0)]
    assert restored.dropped == 1


def test_a_capped_restore_tallies_one_drop_per_LOST_KEY_not_per_surface():
    """A restored row with both surfaces used calls record() twice for the SAME
    key. Counting each refusal would report two lost keys for one, and the
    WARNING the next flush prints is a count of KEYS."""
    buffer = usage_stats.UsageBuffer(cap=1)
    buffer.record(DAY, "a", slash=False)
    buffer.record(DAY, "a", slash=True)
    drained = buffer.drain()
    assert drained.rows == [(DAY, "a", 1, 1)]
    buffer.record(DAY, "b")  # the fresh generation took the only slot
    buffer.restore(drained)
    assert buffer.drain().dropped == 1


def test_restore_does_not_re_report_the_drains_own_drops():
    """A multi-tick outage must not report the same drops on every retry - that
    would publish a drop RATE that is not happening."""
    buffer = usage_stats.UsageBuffer(cap=1)
    buffer.record(DAY, "a")
    buffer.record(DAY, "b")  # dropped
    drained = buffer.drain()
    assert drained.dropped == 1
    buffer.restore(drained)
    assert buffer.drain().dropped == 0


def test_build_flush_payload_yields_four_aligned_arrays():
    buffer = usage_stats.UsageBuffer()
    buffer.record(DAY, "play", count=2)
    buffer.record(NEXT_DAY, "rank", slash=True, count=5)
    days, commands, prefix_counts, slash_counts = usage_stats.build_flush_payload(
        buffer.drain()
    )
    assert len(days) == len(commands) == len(prefix_counts) == len(slash_counts) == 2
    rows = sorted(zip(days, commands, prefix_counts, slash_counts))
    assert rows == [(DAY, "play", 2, 0), (NEXT_DAY, "rank", 0, 5)]
    # Real date objects: asyncpg binds these straight into a date[] parameter.
    assert all(isinstance(day, datetime.date) for day in days)


def test_build_flush_payload_on_an_empty_drain():
    assert usage_stats.build_flush_payload(usage_stats.DrainedUsage()) == (
        [],
        [],
        [],
        [],
    )


# ---------------------------------------------------------------------------
# SQL shape
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query",
    [usage_stats.FLUSH, usage_stats.WINDOWS, usage_stats.TOP, usage_stats.PRUNE],
)
def test_every_statement_is_a_single_command(query):
    """asyncpg's extended protocol prepares exactly ONE statement per call."""
    assert query.strip().rstrip(";").count(";") == 0


def test_the_flush_upsert_adds_onto_the_row_it_finds():
    """DO UPDATE SET x = EXCLUDED.x would OVERWRITE the day with one tick's
    batch, silently discarding every earlier flush of the same day."""
    assert (
        "prefix_count = command_usage.prefix_count + EXCLUDED.prefix_count"
        in usage_stats.FLUSH
    )
    assert (
        "slash_count  = command_usage.slash_count  + EXCLUDED.slash_count"
        in usage_stats.FLUSH
    )
    assert "ON CONFLICT (day, command)" in usage_stats.FLUSH


def test_the_flush_unnests_four_parallel_arrays():
    assert "unnest($1::date[], $2::text[], $3::bigint[], $4::bigint[])" in (
        usage_stats.FLUSH
    )


@pytest.mark.parametrize("query", [usage_stats.WINDOWS, usage_stats.TOP])
def test_windows_span_exactly_the_days_they_advertise(query):
    """`day >= $1 - $2` selects $2 + 1 calendar days (today included), which
    would publish an 8-day sum under a heading that says 7. Same convention as
    serverstats' rollups.window_bounds: today - (days - 1)."""
    assert re.search(r"\$1::date - \(\$\d::int - 1\)", query)
    assert not re.search(r"\$1::date - \$\d::int(?! - 1)", query)


@pytest.mark.parametrize("query", [usage_stats.WINDOWS, usage_stats.TOP])
def test_windows_are_bounded_above_by_the_day_they_are_asked_for(query):
    """A row dated in the future (a clock jump) must not inflate a window."""
    assert "day <= $1::date" in query


@pytest.mark.parametrize(
    "query",
    [usage_stats.FLUSH, usage_stats.WINDOWS, usage_stats.TOP, usage_stats.PRUNE],
)
def test_no_statement_reads_current_date(query):
    """The rows are keyed by a UTC day computed in Python; CURRENT_DATE is the
    DB session's calendar day, which is only the same thing while the server's
    TimeZone is UTC."""
    assert "CURRENT_DATE" not in query


def test_every_window_sum_is_read_with_the_days_it_actually_covers():
    """Each multi-day SUM is paired with a COUNT(DISTINCT day) over the SAME
    range, because a day with no row is a day nobody was counting on. Without
    them the renderer has no way to qualify a sum, and ``since`` only ever
    catches a short history at the start of collection - never a later gap."""
    assert (
        "(COUNT(DISTINCT day)\n"
        "            FILTER (WHERE day >= $1::date - ($2::int - 1)))::bigint"
        " AS week_recorded" in usage_stats.WINDOWS
    )
    assert "(COUNT(DISTINCT day))::bigint AS month_recorded" in usage_stats.WINDOWS


def test_the_windows_read_takes_min_day_over_the_whole_table():
    """Scoped to the window, MIN(day) could never reveal that the window is
    wider than the history collected so far."""
    assert "(SELECT MIN(day) FROM command_usage) AS since" in usage_stats.WINDOWS


def test_the_ranking_breaks_ties_on_the_name():
    assert "ORDER BY total DESC, command ASC" in usage_stats.TOP


def test_the_prune_is_bounded_by_a_limited_ctid_subselect():
    assert "LIMIT $2" in usage_stats.PRUNE
    assert "ctid = ANY(ARRAY(" in usage_stats.PRUNE
    assert "day < $1::date" in usage_stats.PRUNE


# ---------------------------------------------------------------------------
# Collection: both halves, exactly once, dated at increment time
# ---------------------------------------------------------------------------
async def test_a_prefix_completion_feeds_both_the_counters_and_the_buffer(monkeypatch):
    cog = _cog()
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    assert cog.usage.commands["play"] == 1
    assert cog.buffer.drain().rows == [(DAY, "play", 1, 0)]


async def test_a_slash_completion_is_recorded_as_slash_in_the_buffer(monkeypatch):
    cog = _cog()
    _freeze_day(monkeypatch, DAY)
    await cog.on_app_command_completion(object(), _app_command("serverstats"))
    assert cog.buffer.drain().rows == [(DAY, "serverstats", 0, 1)]


async def test_a_hybrid_slash_invocation_is_buffered_exactly_once(monkeypatch):
    """discord.py dispatches BOTH completion events for one hybrid slash call.
    The dedup lives in ONE place, so it cannot ever apply to the since-boot
    counter and not to the persisted one."""
    cog = _cog()
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("rank", slash=True))
    await cog.on_app_command_completion(object(), _app_command("rank", hybrid=True))
    assert cog.usage.total == 1
    assert cog.buffer.drain().rows == [(DAY, "rank", 0, 1)]


async def test_a_nameless_or_missing_command_is_ignored_by_both_halves(monkeypatch):
    cog = _cog()
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(type("Ctx", (), {"command": None})())
    await cog.on_app_command_completion(object(), type("AppCmd", (), {})())
    assert cog.usage.total == 0
    assert cog.buffer.is_empty


async def test_the_day_is_captured_at_increment_time_not_at_flush_time(monkeypatch):
    """THE trap. A tick straddling midnight UTC must write the 23:59
    completions onto the day they happened on; keying the buffer at flush time
    would migrate a whole evening into the new day."""
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    # ... midnight passes before the flush loop next ticks.
    _freeze_day(monkeypatch, NEXT_DAY)
    await cog.on_command_completion(_ctx("play"))
    await cog.flush_usage()

    _method, query, args = pool.executes[0]
    assert query is usage_stats.FLUSH
    days, commands, prefix_counts, _slash = args
    assert sorted(zip(days, commands, prefix_counts)) == [
        (DAY, "play", 1),
        (NEXT_DAY, "play", 1),
    ]


# ---------------------------------------------------------------------------
# The flush
# ---------------------------------------------------------------------------
async def test_a_flush_writes_one_statement_and_empties_the_buffer(monkeypatch):
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    await cog.flush_usage(today=DAY)
    assert len(pool.executes) == 1
    assert cog.buffer.is_empty


async def test_an_empty_buffer_writes_nothing_at_all(monkeypatch):
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.flush_usage(today=DAY)
    assert pool.executes == []


async def test_a_failed_write_hands_the_generation_back_to_the_buffer(monkeypatch):
    async def _boom(index):
        raise RuntimeError("pool is down")

    pool = _Pool(on_execute=_boom)
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    with pytest.raises(RuntimeError):
        await cog.flush_usage(today=DAY)
    # Not lost: the next tick writes it.
    assert cog.buffer.drain().rows == [(DAY, "play", 1, 0)]


async def test_a_cancelled_write_restores_the_generation_and_re_raises(monkeypatch):
    """``except Exception`` here would let a CancelledError skip the restore -
    and cog_unload's whole job is to throw exactly that into this await."""

    async def _cancel(index):
        raise asyncio.CancelledError()

    pool = _Pool(on_execute=_cancel)
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    with pytest.raises(asyncio.CancelledError):
        await cog.flush_usage(today=DAY)
    assert cog.buffer.drain().rows == [(DAY, "play", 1, 0)]


async def test_the_loop_iteration_logs_a_failed_flush_instead_of_dying(monkeypatch):
    async def _boom(index):
        raise RuntimeError("pool is down")

    pool = _Pool(on_execute=_boom)
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    await cog._flush_loop.coro(cog)  # the loop body, not the loop
    assert cog.buffer.drain().rows == [(DAY, "play", 1, 0)]


async def test_the_overflow_tally_is_logged_once_per_flush_not_per_command(
    monkeypatch, caplog
):
    pool = _Pool()
    cog = _cog(pool)
    cog.buffer = usage_stats.UsageBuffer(cap=1)
    _freeze_day(monkeypatch, DAY)
    for name in ("a", "b", "c", "d"):
        await cog.on_command_completion(_ctx(name))
    with caplog.at_level("WARNING"):
        await cog.flush_usage(today=DAY)
    warnings = [r for r in caplog.records if "usage buffer cap" in r.getMessage()]
    assert len(warnings) == 1
    assert cog._flush_stats["dropped"] == 3


# ---------------------------------------------------------------------------
# Teardown: cancel the loop first, then write what is left - exactly once
# ---------------------------------------------------------------------------
async def test_cog_unload_flushes_what_the_buffer_still_holds(monkeypatch):
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    await cog.cog_unload()
    assert len(pool.executes) == 1
    assert pool.executes[0][2][1] == ["play"]


async def test_cog_unload_with_an_empty_buffer_writes_nothing():
    pool = _Pool()
    cog = _cog(pool)
    await cog.cog_unload()
    assert pool.executes == []


async def test_cog_unload_swallows_a_failing_final_flush(monkeypatch):
    """Shutdown must never hang or raise on statistics."""

    async def _boom(index):
        raise RuntimeError("pool is closing")

    pool = _Pool(on_execute=_boom)
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    await cog.cog_unload()  # must not raise
    assert len(pool.executes) == 1


async def test_cog_unload_gives_up_on_a_wedged_final_flush(monkeypatch):
    """The final write is BOUNDED too, not just the wait on the cancelled loop.

    A wedged pool would otherwise hold a clean shutdown open for its whole
    command_timeout (60s) over statistics. Giving up costs the last interval,
    which is exactly what a hard crash already costs.
    """
    never = asyncio.Event()

    async def _hang(index):
        await never.wait()

    pool = _Pool(on_execute=_hang)
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    monkeypatch.setattr(botstats, "UNLOAD_FLUSH_TIMEOUT", 0.05)

    await asyncio.wait_for(cog.cog_unload(), timeout=1)  # must not hang

    assert len(pool.executes) == 1
    # The cancelled write still handed its generation back (except BaseException).
    assert not cog.buffer.is_empty


async def test_cog_unload_waits_for_the_cancelled_flush_then_writes_it_once(
    monkeypatch,
):
    """The ordering incident, end to end.

    The loop is mid-write when teardown starts. cog_unload cancels it, the
    cancelled write restores its drained generation IN THE LOOP'S TASK, and only
    then does the final flush drain - so the counters are written exactly once
    and none are stranded in a buffer nobody writes again. Flushing without
    waiting would drain an already-drained buffer (writing nothing) and lose the
    restored generation.
    """
    started = asyncio.Event()
    never = asyncio.Event()

    async def _hang_then_succeed(index):
        if index == 1:
            started.set()
            await never.wait()  # cancelled by cog_unload

    pool = _Pool(on_execute=_hang_then_succeed)
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))

    await cog.cog_load()
    await asyncio.wait_for(started.wait(), timeout=1)
    assert cog.buffer.is_empty  # drained by the in-flight write

    await cog.cog_unload()

    assert len(pool.executes) == 2, "the in-flight write, then the final one"
    days, commands, prefix_counts, slash_counts = pool.executes[1][2]
    assert (days, commands, prefix_counts, slash_counts) == (
        [DAY],
        ["play"],
        [1],
        [0],
    )
    assert cog.buffer.is_empty
    assert cog._flush_loop.get_task() is None or cog._flush_loop.get_task().done()


async def test_cog_unload_stops_the_loop_so_it_cannot_tick_again(monkeypatch):
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.cog_load()
    await asyncio.sleep(0)
    await cog.cog_unload()
    assert not cog._flush_loop.is_running()


# ---------------------------------------------------------------------------
# Retention prune
# ---------------------------------------------------------------------------
async def test_the_prune_cuts_at_the_retention_window_once_a_day(monkeypatch):
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.flush_usage(today=DAY)
    (_method, query, args) = pool.fetchrows[0]
    assert query is usage_stats.PRUNE
    assert args == (
        DAY - datetime.timedelta(days=usage_stats.RETENTION_DAYS),
        usage_stats.PRUNE_BATCH_SIZE,
    )
    # Same day again: the marker holds, no second prune.
    await cog.flush_usage(today=DAY)
    assert len(pool.fetchrows) == 1
    # A new UTC day: it runs again.
    await cog.flush_usage(today=NEXT_DAY)
    assert len(pool.fetchrows) == 2


async def test_the_prune_keeps_batching_while_batches_come_back_full():
    size = usage_stats.PRUNE_BATCH_SIZE
    pool = _Pool(prune_batches=[size, size, 3])
    cog = _cog(pool)
    await cog.flush_usage(today=DAY)
    assert len(pool.fetchrows) == 3  # stopped on the short batch
    assert cog._flush_stats["pruned"] == 2 * size + 3


async def test_the_prune_can_never_run_more_than_its_ceiling():
    size = usage_stats.PRUNE_BATCH_SIZE
    pool = _Pool(prune_batches=[size] * 50)
    cog = _cog(pool)
    await cog.flush_usage(today=DAY)
    assert len(pool.fetchrows) == usage_stats.PRUNE_MAX_BATCHES


async def test_a_failed_prune_is_retried_on_the_next_tick():
    class _FailingPool(_Pool):
        async def fetchrow(self, query, *args, **kwargs):
            self.calls.append(("fetchrow", query, args))
            raise RuntimeError("pool is down")

    pool = _FailingPool()
    cog = _cog(pool)
    with pytest.raises(RuntimeError):
        await cog.flush_usage(today=DAY)
    assert cog._prune_day is None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
class _ReadPool:
    def __init__(self, row, ranking):
        self.calls = []
        self._row = row
        self._ranking = ranking

    async def fetchrow(self, query, *args, **kwargs):
        self.calls.append(("fetchrow", query, args, kwargs))
        return self._row

    async def fetch(self, query, *args, **kwargs):
        self.calls.append(("fetch", query, args, kwargs))
        return self._ranking


async def test_fetch_persisted_usage_shapes_both_reads():
    pool = _ReadPool(
        {
            "today": 3,
            "week": 20,
            "month": 100,
            "week_recorded": 5,
            "month_recorded": 22,
            "since": datetime.date(2026, 7, 1),
        },
        [{"command": "play", "total": 12}, {"command": "rank", "total": 8}],
    )
    persisted = await usage_stats.fetch_persisted_usage(
        pool, timeout=15.0, today=DAY
    )
    assert persisted.as_of == DAY
    assert (persisted.today, persisted.week, persisted.month) == (3, 20, 100)
    assert (persisted.week_recorded, persisted.month_recorded) == (5, 22)
    assert persisted.since == datetime.date(2026, 7, 1)
    assert persisted.top == (("play", 12), ("rank", 8))
    # Both reads are bounded, and both are told which day to end on.
    assert all(call[3]["timeout"] == 15.0 for call in pool.calls)
    assert pool.calls[0][2] == (DAY, usage_stats.WEEK_DAYS, usage_stats.MONTH_DAYS)
    assert pool.calls[1][2] == (DAY, usage_stats.WEEK_DAYS, usage_stats.TOP_LIMIT)


async def test_fetch_persisted_usage_on_an_empty_table():
    pool = _ReadPool(
        {
            "today": 0,
            "week": 0,
            "month": 0,
            "week_recorded": 0,
            "month_recorded": 0,
            "since": None,
        },
        [],
    )
    persisted = await usage_stats.fetch_persisted_usage(
        pool, timeout=15.0, today=DAY
    )
    assert persisted.since is None
    assert persisted.covered_days == 0
    assert persisted.top == ()


def test_covered_days_is_inclusive_and_never_negative():
    def _persisted(since):
        return usage_stats.PersistedUsage(
            as_of=DAY,
            today=1,
            week=1,
            month=1,
            week_days=7,
            month_days=30,
            week_recorded=1,
            month_recorded=1,
            since=since,
            top=(),
        )

    assert _persisted(DAY).covered_days == 1
    assert _persisted(DAY - datetime.timedelta(days=5)).covered_days == 6
    # A row dated in the future (clock jump) must not report negative history.
    assert _persisted(DAY + datetime.timedelta(days=3)).covered_days == 1


def test_window_is_full_compares_against_collected_history():
    persisted = usage_stats.PersistedUsage(
        as_of=DAY,
        today=1,
        week=1,
        month=1,
        week_days=7,
        month_days=30,
        week_recorded=7,
        month_recorded=7,
        since=DAY - datetime.timedelta(days=6),
        top=(),
    )
    assert persisted.window_is_full(7) is True
    assert persisted.window_is_full(30) is False


# ---------------------------------------------------------------------------
# Rendering: the honesty rules
# ---------------------------------------------------------------------------
def _persisted(**overrides):
    kwargs = dict(
        as_of=DAY,
        today=42,
        week=300,
        month=1200,
        week_days=7,
        month_days=30,
        week_recorded=7,
        month_recorded=30,
        since=DAY - datetime.timedelta(days=60),
        top=(("play", 120), ("rank", 60)),
    )
    kwargs.update(overrides)
    return usage_stats.PersistedUsage(**kwargs)


def test_recorded_usage_renders_the_three_windows():
    text = "\n".join(botstats.render_persisted_usage(_persisted()))
    assert "Today: 42" in text
    assert "7 days: 300 (7 of 7 day(s) recorded)" in text
    assert "30 days: 1,200 (30 of 30 day(s) recorded)" in text
    assert "today is a partial day" in text


def test_every_multi_day_total_names_the_days_it_actually_covers():
    """A missing day is a day nobody was counting on, never a zero (rule 1). A
    bot down for 20 of the last 30 days must not print an unqualified 30-day
    sum - and ``since`` cannot catch that, because collection started long
    before the gap."""
    text = "\n".join(
        botstats.render_persisted_usage(
            _persisted(week_recorded=3, month_recorded=10)
        )
    )
    assert "7 days: 300 (3 of 7 day(s) recorded)" in text
    assert "30 days: 1,200 (10 of 30 day(s) recorded)" in text
    # The history is long, so the since-note stays silent: coverage is the only
    # thing that reports this hole.
    assert "not full yet" not in text


def test_recorded_usage_unavailable_is_said_not_zeroed():
    text = "\n".join(botstats.render_persisted_usage(None))
    assert "unavailable" in text
    assert "0" not in text


def test_recorded_usage_with_no_history_says_so_rather_than_printing_zeros():
    """An empty table means nothing was ever recorded - which is not the same
    claim as "zero commands were run"."""
    text = "\n".join(
        botstats.render_persisted_usage(_persisted(since=None, today=0, week=0, month=0))
    )
    assert "Nothing has been recorded yet" in text
    assert "Today: 0" not in text


def test_a_window_wider_than_the_history_names_its_real_coverage():
    text = "\n".join(
        botstats.render_persisted_usage(
            _persisted(since=DAY - datetime.timedelta(days=5))
        )
    )
    assert "Recorded since 2026-07-26" in text
    assert "6 day(s)" in text
    assert "not full yet" in text


def test_a_full_history_carries_no_partial_window_note():
    text = "\n".join(
        botstats.render_persisted_usage(
            _persisted(since=DAY - datetime.timedelta(days=29))
        )
    )
    assert "not full yet" not in text


def test_the_persisted_ranking_is_listed_in_order():
    lines = botstats.render_persisted_ranking(_persisted())
    assert lines[0].endswith("`play` - 120")
    assert lines[1].endswith("`rank` - 60")


def test_an_empty_persisted_ranking_says_so():
    assert "No command has been recorded" in "\n".join(
        botstats.render_persisted_ranking(_persisted(top=()))
    )


def test_an_unavailable_persisted_ranking_says_so():
    assert "unavailable" in "\n".join(botstats.render_persisted_ranking(None))


def test_the_usage_page_keeps_the_since_boot_block_next_to_the_windows():
    counters = botstats.UsageCounters(
        started_at=datetime.datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
    )
    counters.record("play")
    text = _flat(
        botstats.render_usage(
            counters,
            None,
            _persisted(),
            now=datetime.datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        )
    )
    assert "1 commands run since boot" in text
    assert "reset on every restart" in text
    assert "Today: 42" in text
    assert "Most used commands (7 days)" in text


def test_the_usage_page_degrades_both_db_blocks_independently():
    counters = botstats.UsageCounters(
        started_at=datetime.datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
    )
    text = _flat(
        botstats.render_usage(
            counters, None, None, now=datetime.datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        )
    )
    assert "Recorded usage is unavailable right now." in text
    assert "Observed activity is unavailable right now." in text
    assert "commands run since boot" in text  # the live half still renders


def test_the_usage_page_stays_inside_the_components_v2_text_budget():
    """Components V2 caps a message's TOTAL text at 4000 characters, and this
    page now carries TWO rankings plus three window blocks."""
    counters = botstats.UsageCounters(
        started_at=datetime.datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    )
    name = "x" * usage_stats.COMMAND_NAME_LIMIT
    for index in range(botstats.TOP_COMMANDS_LIMIT * 2):
        counters.record("{0}{1:02d}".format(name, index), slash=True)
    activity = botstats.ObservedActivity(
        days=7,
        messages=999999999,
        message_guilds=99999,
        message_days=7,
        joins=999999,
        leaves=999999,
        day_guilds=99999,
        guild_days=999999,
    )
    persisted = _persisted(
        today=999999999,
        week=999999999,
        month=999999999,
        since=DAY,
        top=tuple(
            ("{0}{1:02d}".format(name, index), 999999999)
            for index in range(usage_stats.TOP_LIMIT)
        ),
    )
    sections = botstats.render_usage(
        counters,
        activity,
        persisted,
        now=datetime.datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    )
    assert len(_flat(sections)) < 4000


# ---------------------------------------------------------------------------
# Structural: a GLOBAL aggregate table, deliberately outside both privacy guards
# ---------------------------------------------------------------------------
_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "schema.sql"
)


def _command_usage_ddl():
    with open(_SCHEMA_PATH, encoding="utf-8") as handle:
        ddl = re.sub(r"--[^\n]*", "", handle.read())
    match = re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+command_usage\s*\((.*?)\n\s*\)\s*;",
        ddl,
        re.S | re.I,
    )
    assert match, "command_usage must be declared in schema.sql"
    return match.group(1)


def test_command_usage_is_declared_with_its_composite_primary_key():
    body = _command_usage_ddl()
    for column in ("day", "command", "prefix_count", "slash_count"):
        assert re.search(r"^\s*{0}\b".format(column), body, re.M)
    assert "PRIMARY KEY (day, command)" in body


def test_command_usage_carries_no_guild_id_and_no_user_id():
    """This is what makes the table exempt from BOTH structural guards, and the
    exemption is derived, not declared: tests/tools/test_retention.py enumerates
    tables with a guild_id (guild purge) and tests/tools/test_privacy.py
    enumerates tables with a user_id and no guild_id (/mydata export). A table
    with neither is global operational data and is invisible to both by
    construction - so this test is the one that has to fail if a later lot adds
    an id column here without wiring the matching privacy path."""
    body = _command_usage_ddl()
    assert not re.search(r"\bguild_id\b", body, re.I)
    assert not re.search(r"\buser_id\b", body, re.I)

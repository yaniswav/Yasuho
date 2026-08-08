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


def _freeze_day(monkeypatch, day, hour=9):
    monkeypatch.setattr(botstats.usage_stats, "utc_today", lambda now=None: day)
    monkeypatch.setattr(
        botstats.usage_stats, "utc_day_hour", lambda now=None: (day, hour)
    )


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
    payload = usage_stats.build_flush_payload(buffer.drain())
    days, commands, prefix_counts, slash_counts = payload[:4]
    assert len(days) == len(commands) == len(prefix_counts) == len(slash_counts) == 2
    rows = sorted(zip(days, commands, prefix_counts, slash_counts))
    assert rows == [(DAY, "play", 2, 0), (NEXT_DAY, "rank", 0, 5)]
    # Real date objects: asyncpg binds these straight into a date[] parameter.
    assert all(isinstance(day, datetime.date) for day in days)


def test_build_flush_payload_on_an_empty_drain():
    assert usage_stats.build_flush_payload(usage_stats.DrainedUsage())[:4] == (
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
    days, commands, prefix_counts, _slash, _dows, _hours, _counts = args
    assert sorted(zip(days, commands, prefix_counts)) == [
        (DAY, "play", 1),
        (NEXT_DAY, "play", 1),
    ]


# ---------------------------------------------------------------------------
# The flush
# ---------------------------------------------------------------------------
async def test_a_flush_writes_one_statement_and_empties_the_buffer(monkeypatch):
    """ONE statement for BOTH tables: the per-day rows and the weekly profile
    ride the same data-modifying CTE, so they can never describe different sets
    of completions (and a retry can never re-add one without the other)."""
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.on_command_completion(_ctx("play"))
    await cog.flush_usage(today=DAY)
    assert [call[1] for call in pool.executes].count(usage_stats.FLUSH) == 1
    assert cog.buffer.is_empty


async def test_an_empty_buffer_writes_nothing_at_all(monkeypatch):
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.flush_usage(today=DAY)
    # The once-a-day marker seed still runs (it is the maintenance hook, not a
    # write of counters); no counter statement does.
    assert usage_stats.FLUSH not in [call[1] for call in pool.executes]


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
    days, commands, prefix_counts, slash_counts, dows, hours, counts = (
        pool.executes[1][2]
    )
    assert (days, commands, prefix_counts, slash_counts) == (
        [DAY],
        ["play"],
        [1],
        [0],
    )
    # The profile half of the SAME generation came back with it.
    assert (dows, hours, counts) == ([DAY.weekday()], [9], [1])
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
    # Same day again: the marker holds, no second maintenance pass.
    prunes = [call for call in pool.fetchrows if call[1] is usage_stats.PRUNE]
    assert len(prunes) == 1
    await cog.flush_usage(today=DAY)
    assert len([c for c in pool.fetchrows if c[1] is usage_stats.PRUNE]) == 1
    # A new UTC day: it runs again.
    await cog.flush_usage(today=NEXT_DAY)
    assert len([c for c in pool.fetchrows if c[1] is usage_stats.PRUNE]) == 2


def _prune_calls(pool):
    return [call for call in pool.fetchrows if call[1] is usage_stats.PRUNE]


async def test_the_prune_keeps_batching_while_batches_come_back_full():
    size = usage_stats.PRUNE_BATCH_SIZE
    pool = _Pool(prune_batches=[size, size, 3])
    cog = _cog(pool)
    await cog.flush_usage(today=DAY)
    assert len(_prune_calls(pool)) == 3  # stopped on the short batch
    assert cog._flush_stats["pruned"] == 2 * size + 3


async def test_the_prune_can_never_run_more_than_its_ceiling():
    size = usage_stats.PRUNE_BATCH_SIZE
    pool = _Pool(prune_batches=[size] * 50)
    cog = _cog(pool)
    await cog.flush_usage(today=DAY)
    assert len(_prune_calls(pool)) == usage_stats.PRUNE_MAX_BATCHES


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
    """Read-side double. Answers each of the four reads by its own statement.

    ``command_usage_hourly_state`` CONTAINS ``command_usage``, so the marker row
    is matched first - the same trap the dashboard's own double documents.
    """

    def __init__(self, row, ranking, profile=(), state=None):
        self.calls = []
        self._row = row
        self._ranking = ranking
        self._profile = list(profile)
        self._state = state

    async def fetchrow(self, query, *args, **kwargs):
        self.calls.append(("fetchrow", query, args, kwargs))
        if "command_usage_hourly_state" in query:
            return self._state
        return self._row

    async def fetch(self, query, *args, **kwargs):
        self.calls.append(("fetch", query, args, kwargs))
        if "command_usage_hourly" in query:
            return self._profile
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
        hourly=((0, 3, 1), (2, 14, 40), (5, 20, 90), (6, 21, 60)),
        hourly_since=DAY - datetime.timedelta(days=20),
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
        hourly=tuple(
            (dow, hour, 999999999)
            for dow in range(usage_stats.DOW_COUNT)
            for hour in range(usage_stats.HOURS_PER_DAY)
        ),
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


def test_command_usage_is_not_given_an_hour_column_by_this_lot():
    """The WHEN lives in its own 168-row table. Adding an hour dimension HERE
    would multiply this table's cardinality by 24 to answer a question that is
    never asked of a single command, and the prune/window SQL above assumes the
    (day, command) key throughout."""
    body = _command_usage_ddl()
    assert not re.search(r"\bhour\b", body, re.I)
    assert not re.search(r"\bdow\b", body, re.I)


# ===========================================================================
# A3: the hourly profile - the quiet-hour signal
# ===========================================================================
MONDAY = datetime.date(2026, 8, 3)  # weekday() == 0
SATURDAY = datetime.date(2026, 8, 8)  # weekday() == 5


# ---------------------------------------------------------------------------
# The instant a completion is filed against
# ---------------------------------------------------------------------------
def test_utc_day_hour_reads_the_day_and_the_hour_from_ONE_clock_reading():
    moment = datetime.datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC)
    assert usage_stats.utc_day_hour(moment) == (datetime.date(2026, 7, 31), 23)


def test_utc_day_hour_converts_a_non_utc_clock_before_splitting_it():
    """23:30 in UTC+2 is 21:30 of the SAME UTC day; reading .hour off a local
    datetime would file the completion two slots late (and, an hour later, on
    the wrong day AND the wrong weekday)."""
    east = datetime.timezone(datetime.timedelta(hours=2))
    moment = datetime.datetime(2026, 7, 31, 23, 30, tzinfo=east)
    assert usage_stats.utc_day_hour(moment) == (datetime.date(2026, 7, 31), 21)
    assert usage_stats.utc_day_hour(
        datetime.datetime(2026, 8, 1, 1, 30, tzinfo=east)
    ) == (datetime.date(2026, 7, 31), 23)


def test_the_weekday_convention_is_python_monday_zero():
    """Three conventions exist for the same seven days (Python's Monday=0,
    Postgres' DOW Sunday=0, its ISODOW Monday=1). The stored one is Python's,
    and it is computed here rather than in SQL precisely so it cannot drift."""
    assert usage_stats.day_of_week(MONDAY) == 0
    assert usage_stats.day_of_week(SATURDAY) == 5
    assert usage_stats.SLOTS_PER_WEEK == 168


# ---------------------------------------------------------------------------
# The buffer's second axis
# ---------------------------------------------------------------------------
def test_the_profile_aggregates_over_commands_and_surfaces():
    """The hourly table has neither a command nor a surface column, so the
    aggregation happens at increment time - one dict entry per slot, whatever
    the traffic mix."""
    buffer = usage_stats.UsageBuffer()
    buffer.record(MONDAY, "play", hour=9)
    buffer.record(MONDAY, "rank", slash=True, hour=9)
    buffer.record(MONDAY, "play", hour=10, count=3)
    drained = buffer.drain()
    assert sorted(drained.slots) == [(0, 9, 2), (0, 10, 3)]
    # ... and the per-day rows are untouched by any of it.
    assert sorted(drained.rows) == [
        (MONDAY, "play", 4, 0),
        (MONDAY, "rank", 0, 1),
    ]


def test_the_per_day_key_stays_day_and_command_only():
    """THE reason the hour is a second dict rather than a wider key: the daily
    upsert's dedup IS this dict's key. Two hours of the same command must still
    be ONE per-day row, or ON CONFLICT would try to touch it twice."""
    buffer = usage_stats.UsageBuffer()
    buffer.record(MONDAY, "play", hour=9)
    buffer.record(MONDAY, "play", hour=10)
    drained = buffer.drain()
    assert drained.rows == [(MONDAY, "play", 2, 0)]
    assert sorted(drained.slots) == [(0, 9, 1), (0, 10, 1)]
    days, commands = usage_stats.build_flush_payload(drained)[:2]
    assert len(set(zip(days, commands))) == len(days) == 1


def test_a_completion_with_no_hour_still_counts_on_the_per_day_axis():
    buffer = usage_stats.UsageBuffer()
    assert buffer.record(MONDAY, "play") is True
    drained = buffer.drain()
    assert drained.rows == [(MONDAY, "play", 1, 0)]
    assert drained.slots == []


@pytest.mark.parametrize("hour", [-1, 24, 99])
def test_an_impossible_hour_costs_nothing_on_the_per_day_axis(hour):
    """The two axes fail independently: a bad hour must not be able to drop a
    per-day count, which is the load-bearing one (and the column is a SMALLINT
    with a CHECK, so writing it would fail the whole flush)."""
    buffer = usage_stats.UsageBuffer()
    assert buffer.record(MONDAY, "play", hour=hour) is True
    drained = buffer.drain()
    assert drained.rows == [(MONDAY, "play", 1, 0)]
    assert drained.slots == []


def test_a_key_dropped_at_the_cap_drops_BOTH_axes():
    """One refusal, both axes - otherwise the profile would count completions
    the per-day table never heard about, and the two tables would describe
    different sets of commands."""
    buffer = usage_stats.UsageBuffer(cap=1)
    buffer.record(MONDAY, "a", hour=9)
    assert buffer.record(MONDAY, "b", hour=9) is False
    drained = buffer.drain()
    assert drained.rows == [(MONDAY, "a", 1, 0)]
    assert drained.slots == [(0, 9, 1)]


def test_the_profile_dict_cannot_grow_past_the_168_slots_of_a_week():
    """No cap is needed on this dict and none exists: its key space IS the week.
    Even a wedged flush retaining generation after generation cannot widen it."""
    buffer = usage_stats.UsageBuffer()
    day = MONDAY
    for offset in range(30):  # a month of days folded onto seven weekdays
        for hour in range(usage_stats.HOURS_PER_DAY):
            buffer.record(day + datetime.timedelta(days=offset), "play", hour=hour)
    assert len(buffer.drain().slots) == usage_stats.SLOTS_PER_WEEK


def test_drain_detaches_the_profile_too_and_is_empty_accounts_for_it():
    buffer = usage_stats.UsageBuffer()
    buffer.record(MONDAY, "play", hour=9)
    drained = buffer.drain()
    assert buffer.is_empty
    assert not drained.is_empty
    assert usage_stats.DrainedUsage(rows=[], slots=[(0, 9, 1)]).is_empty is False


def test_the_live_buffer_is_empty_only_when_BOTH_axes_are():
    """Symmetry with DrainedUsage.is_empty, which does check both.

    Unreachable today - slots are a subset of rows by construction - and pinned
    at the seam rather than through ``record`` for exactly that reason: the day
    someone adds an hour-only path, a shutdown must not skip its final flush
    over a buffer that still holds something.
    """
    buffer = usage_stats.UsageBuffer()
    assert buffer.is_empty
    buffer._slots[(0, 9)] = 1
    assert buffer.is_empty is False
    assert buffer.drain().slots == [(0, 9, 1)]
    assert buffer.is_empty


def test_restore_folds_the_profile_back_with_the_rows():
    """A write that did not happen must leave BOTH axes waiting for the next
    one; restoring only the per-day half would silently lose the slot."""
    buffer = usage_stats.UsageBuffer()
    buffer.record(MONDAY, "play", hour=9, count=2)
    drained = buffer.drain()
    buffer.record(MONDAY, "play", hour=9)  # counted while the flush was in flight
    buffer.restore(drained)
    restored = buffer.drain()
    assert restored.rows == [(MONDAY, "play", 3, 0)]
    assert restored.slots == [(0, 9, 3)]


def test_a_capped_restore_still_folds_the_profile_back():
    """The slots go back DIRECTLY, not through the cap: they are already
    aggregated (no day, no command in their key) and their dict cannot overflow,
    so a per-day key lost to the cap must not take a counted slot with it.

    This is ALSO the one path on which the two tables diverge, and it is meant
    to: the hourly side keeps a completion the daily side dropped. The
    docstrings on ``restore``, ``record`` and ``FLUSH`` say so rather than
    claiming the two can never describe different sets - the flush is ATOMIC
    across both tables, which is a different promise.
    """
    buffer = usage_stats.UsageBuffer(cap=1)
    buffer.record(MONDAY, "a", hour=9)
    drained = buffer.drain()
    buffer.record(MONDAY, "b", hour=10)  # the fresh generation took the only slot
    buffer.restore(drained)
    restored = buffer.drain()
    assert restored.rows == [(MONDAY, "b", 1, 0)]
    assert restored.dropped == 1
    assert sorted(restored.slots) == [(0, 9, 1), (0, 10, 1)]


def test_build_flush_payload_yields_seven_arrays_aligned_within_each_group():
    buffer = usage_stats.UsageBuffer()
    buffer.record(MONDAY, "play", hour=9, count=2)
    buffer.record(MONDAY, "rank", slash=True, hour=9)
    buffer.record(SATURDAY, "play", hour=20)
    payload = usage_stats.build_flush_payload(buffer.drain())
    days, commands, prefix, slash, dows, hours, counts = payload
    assert len(days) == len(commands) == len(prefix) == len(slash) == 3
    # The two groups have DIFFERENT lengths on purpose: three (day, command)
    # rows, two slots.
    assert len(dows) == len(hours) == len(counts) == 2
    assert sorted(zip(dows, hours, counts)) == [(0, 9, 3), (5, 20, 1)]


def test_build_flush_payload_on_an_empty_drain_yields_seven_empty_arrays():
    assert usage_stats.build_flush_payload(usage_stats.DrainedUsage()) == (
        [], [], [], [], [], [], [],
    )


# ---------------------------------------------------------------------------
# SQL shape: the flush writes both tables, the decay claims its week
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query",
    [
        usage_stats.FLUSH,
        usage_stats.SEED_HOURLY_STATE,
        usage_stats.DECAY_HOURLY,
        usage_stats.HOURLY_PROFILE,
        usage_stats.HOURLY_STATE,
    ],
)
def test_every_hourly_statement_is_a_single_command(query):
    assert query.strip().rstrip(";").count(";") == 0


@pytest.mark.parametrize(
    "query",
    [usage_stats.SEED_HOURLY_STATE, usage_stats.DECAY_HOURLY],
)
def test_no_hourly_statement_reads_current_date(query):
    """Same rule as every other statement in this module: the day is computed in
    Python, in UTC, and passed in - CURRENT_DATE is the DB session's day."""
    assert "CURRENT_DATE" not in query


def test_the_flush_writes_both_tables_in_one_statement():
    """Two statements would leave a window where the per-day rows commit and the
    profile does not, and the generation handed back on failure would then re-add
    the half that already landed."""
    assert usage_stats.FLUSH.strip().startswith("WITH daily AS (")
    assert "INSERT INTO command_usage (" in usage_stats.FLUSH
    assert "INSERT INTO command_usage_hourly (dow, hour, count)" in usage_stats.FLUSH


def test_the_profile_upsert_adds_onto_the_slot_it_finds():
    """DO UPDATE SET count = EXCLUDED.count would overwrite the slot with one
    tick's batch - i.e. throw the whole profile away every five minutes."""
    assert (
        "DO UPDATE SET count = command_usage_hourly.count + EXCLUDED.count"
        in usage_stats.FLUSH
    )
    assert "ON CONFLICT (dow, hour)" in usage_stats.FLUSH


def test_the_flush_unnests_the_profile_arrays_separately():
    assert "unnest($5::smallint[], $6::smallint[], $7::bigint[])" in usage_stats.FLUSH


def test_the_decay_claims_the_week_before_it_halves_anything():
    """The cadence is a CLAIM (UPDATE ... WHERE not-done-yet RETURNING), the
    repo's at-most-once idiom: its row lock is what makes two callers racing
    impossible to double-apply, and the halving is gated on it having returned a
    row."""
    assert "halved_on <= $1::date - $2::int" in usage_stats.DECAY_HOURLY
    assert "SET halved_on = $1::date" in usage_stats.DECAY_HOURLY
    assert "EXISTS (SELECT 1 FROM due)" in usage_stats.DECAY_HOURLY


def test_the_decay_halves_with_integer_division_so_it_floors_at_zero():
    """count * 0.5 would need a rounding rule and could never reach 0; integer
    division lets a slot that stopped being used leave the profile."""
    assert "SET count = count / 2" in usage_stats.DECAY_HOURLY


def test_the_decay_marker_is_seeded_without_overwriting_an_existing_one():
    """ON CONFLICT DO NOTHING: the seed rides the daily hook and must be a no-op
    on every call but the first, or started_on would move every day and the
    profile would never be considered old enough to show."""
    assert "ON CONFLICT (id) DO NOTHING" in usage_stats.SEED_HOURLY_STATE
    assert "VALUES (1, $1::date, $1::date)" in usage_stats.SEED_HOURLY_STATE


def test_the_profile_read_is_unbounded_because_the_table_is():
    """168 rows is the table's ceiling for ever, so there is nothing to LIMIT -
    and a LIMIT would silently hide slots from the ranking."""
    assert "FROM command_usage_hourly" in usage_stats.HOURLY_PROFILE
    assert "LIMIT" not in usage_stats.HOURLY_PROFILE
    assert "WHERE" not in usage_stats.HOURLY_PROFILE


# ---------------------------------------------------------------------------
# Collection + the weekly decay, wired into the cog
# ---------------------------------------------------------------------------
async def test_a_completion_is_filed_on_the_slot_it_happened_in(monkeypatch):
    cog = _cog()
    _freeze_day(monkeypatch, SATURDAY, hour=20)
    await cog.on_command_completion(_ctx("play"))
    assert cog.buffer.drain().slots == [(5, 20, 1)]


async def test_the_hour_is_captured_at_increment_time_not_at_flush_time(
    monkeypatch,
):
    """The same trap as the day, one axis finer: a tick straddling 20:59 must
    file each completion in the hour it happened in."""
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, SATURDAY, hour=20)
    await cog.on_command_completion(_ctx("play"))
    _freeze_day(monkeypatch, SATURDAY, hour=21)
    await cog.on_command_completion(_ctx("play"))
    await cog.flush_usage(today=SATURDAY)
    _method, query, args = pool.executes[0]
    assert query is usage_stats.FLUSH
    dows, hours, counts = args[4], args[5], args[6]
    assert sorted(zip(dows, hours, counts)) == [(5, 20, 1), (5, 21, 1)]


async def test_the_day_and_the_hour_come_from_ONE_clock_reading(monkeypatch):
    """Two readings could straddle a midnight and file a 23:59 completion as
    hour 0 of the day before - a slot 23 hours away from the truth."""
    seen = []

    def _clock(now=None):
        seen.append(now)
        return (SATURDAY, 23)

    monkeypatch.setattr(botstats.usage_stats, "utc_day_hour", _clock)
    monkeypatch.setattr(
        botstats.usage_stats,
        "utc_today",
        lambda now=None: pytest.fail("the day must come from utc_day_hour"),
    )
    cog = _cog()
    await cog.on_command_completion(_ctx("play"))
    assert len(seen) == 1
    assert cog.buffer.drain().rows == [(SATURDAY, "play", 1, 0)]


async def test_the_decay_rides_the_daily_prune_hook(monkeypatch):
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    await cog.flush_usage(today=DAY)
    assert [call[1] for call in pool.executes] == [usage_stats.SEED_HOURLY_STATE]
    decays = [c for c in pool.fetchrows if c[1] is usage_stats.DECAY_HOURLY]
    assert len(decays) == 1
    assert decays[0][2] == (DAY, usage_stats.HOURLY_HALVE_DAYS)
    # Same day again: the hook's marker holds for the decay exactly as it does
    # for the prune.
    await cog.flush_usage(today=DAY)
    assert len([c for c in pool.fetchrows if c[1] is usage_stats.DECAY_HOURLY]) == 1
    await cog.flush_usage(today=NEXT_DAY)
    assert len([c for c in pool.fetchrows if c[1] is usage_stats.DECAY_HOURLY]) == 2


async def test_the_week_is_enforced_by_the_DATABASE_not_by_this_process(
    monkeypatch,
):
    """THE arbitration of this lot. The daily hook ASKS every day and after every
    restart; the WEEK is decided in SQL against a durable marker row. The prune
    next door can afford an in-memory cadence marker because re-deleting expired
    rows is free - halving is not idempotent, and this bot restarts on every
    deploy, so an in-memory weekly marker would halve the profile several times a
    day and flatten it inside a week.

    So: no in-memory weekly marker anywhere on the cog, and a FRESH cog (i.e. a
    restart) asks again immediately.
    """
    pool = _Pool()
    cog = _cog(pool)
    _freeze_day(monkeypatch, DAY)
    for day in (DAY, NEXT_DAY, NEXT_DAY + datetime.timedelta(days=1)):
        await cog.flush_usage(today=day)
    asked = [c for c in pool.fetchrows if c[1] is usage_stats.DECAY_HOURLY]
    assert len(asked) == 3, "the hook asks daily; SQL decides whether it fires"
    assert [call[2][1] for call in asked] == [usage_stats.HOURLY_HALVE_DAYS] * 3
    assert not any(
        "halve" in name or "decay" in name for name in vars(cog)
    ), "the weekly cadence must not be tracked in memory"

    restarted = _cog(pool)
    await restarted.flush_usage(today=DAY)
    assert len([c for c in pool.fetchrows if c[1] is usage_stats.DECAY_HOURLY]) == 4


async def test_a_failed_decay_leaves_the_daily_hook_unmarked(monkeypatch):
    """Same rule as the prune: the day marker is set only after the whole hook
    ran, so a failure is retried on the next tick rather than skipped for a day."""

    class _FailingDecay(_Pool):
        async def fetchrow(self, query, *args, **kwargs):
            self.calls.append(("fetchrow", query, args))
            if query is usage_stats.DECAY_HOURLY:
                raise RuntimeError("pool is down")
            return {"rows": 0}

    pool = _FailingDecay()
    cog = _cog(pool)
    with pytest.raises(RuntimeError):
        await cog.flush_usage(today=DAY)
    assert cog._prune_day is None


async def test_a_halving_is_counted_in_the_flush_instrumentation(monkeypatch):
    pool = _Pool(prune_batches=[0, 12])  # prune: nothing; decay: 12 slots halved
    cog = _cog(pool)
    await cog.flush_usage(today=DAY)
    assert cog._flush_stats["halved"] == 12


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def test_fetch_persisted_usage_carries_the_profile_and_its_marker():
    pool = _ReadPool(
        {
            "today": 3,
            "week": 20,
            "month": 100,
            "week_recorded": 5,
            "month_recorded": 22,
            "since": datetime.date(2026, 7, 1),
        },
        [],
        profile=[{"dow": 5, "hour": 20, "count": 9}],
        state={"started_on": datetime.date(2026, 7, 20), "halved_on": DAY},
    )
    persisted = await usage_stats.fetch_persisted_usage(pool, timeout=15.0, today=DAY)
    assert persisted.hourly == ((5, 20, 9),)
    assert persisted.hourly_since == datetime.date(2026, 7, 20)
    assert persisted.hourly_covered_days == 12
    # One memo, four bounded reads - not a second memo key for the profile.
    assert [call[0] for call in pool.calls] == [
        "fetchrow", "fetch", "fetch", "fetchrow",
    ]
    assert all(call[3]["timeout"] == 15.0 for call in pool.calls)


async def test_a_missing_marker_row_reads_as_no_hourly_history():
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
        profile=[],
        state=None,
    )
    persisted = await usage_stats.fetch_persisted_usage(pool, timeout=15.0, today=DAY)
    assert persisted.hourly == ()
    assert persisted.hourly_since is None
    assert persisted.hourly_covered_days == 0


def test_hourly_coverage_is_the_profiles_own_history_not_the_tables():
    """The per-day table can be a year old on the day the profile table is
    created. Reading coverage from it would publish a week-of-the-day pattern
    built from a few hours of data."""
    persisted = _persisted(
        since=DAY - datetime.timedelta(days=365),
        hourly_since=DAY - datetime.timedelta(days=2),
    )
    assert persisted.covered_days == 366
    assert persisted.hourly_covered_days == 3


# ---------------------------------------------------------------------------
# Ranking the week
# ---------------------------------------------------------------------------
def _full_profile(**counts):
    """A profile where every one of the 168 slots has a row (value 10 unless
    overridden by a ``d<dow>h<hour>`` keyword)."""
    rows = []
    for dow in range(usage_stats.DOW_COUNT):
        for hour in range(usage_stats.HOURS_PER_DAY):
            key = "d{0}h{1}".format(dow, hour)
            rows.append((dow, hour, counts.get(key, 10)))
    return tuple(rows)


def test_a_profile_younger_than_a_week_is_refused_outright():
    """THE honesty rule of this block. Below one full week some of the 168 slots
    have not HAPPENED yet, so their emptiness describes the calendar rather than
    the traffic - and pointing the owner at one would send them to restart in an
    hour nobody has measured."""
    profile = _full_profile()
    assert usage_stats.rank_hour_slots(profile, covered_days=6) is None
    assert usage_stats.rank_hour_slots(profile, covered_days=7) is not None


def test_an_empty_profile_is_refused_rather_than_called_quiet():
    assert usage_stats.rank_hour_slots((), covered_days=400) is None
    assert usage_stats.rank_hour_slots(
        ((0, 0, 0), (1, 1, 0)), covered_days=400
    ) is None


def test_a_slot_with_no_row_is_the_quietest_once_the_week_has_been_lived():
    """Past the coverage gate, no row means no command ran then - which is
    exactly what the caller is looking for. The full grid is materialised."""
    ranked = usage_stats.rank_hour_slots(
        ((5, 20, 90), (2, 14, 10)), covered_days=30
    )
    assert [(slot.dow, slot.hour, slot.count) for slot in ranked.quietest] == [
        (0, 0, 0),
        (0, 1, 0),
        (0, 2, 0),
    ]
    # ...and the 166 slots with no traffic are counted, so the renderer can say
    # how many there are instead of naming three arbitrary ones.
    assert ranked.quiet_slots == 166
    assert ranked.total_slots == usage_stats.SLOTS_PER_WEEK


def test_a_zero_count_slot_is_never_called_the_busiest_anything():
    """The self-refuting line this pins: "Busiest: Mon 04:00 (100.0%), Mon 00:00
    (0.0%)". Reachable in steady state, not just at birth - the weekly halving
    floors to 0, so a quiet install decays toward exactly this shape. The list
    may be SHORTER than the limit; it is never padded."""
    ranked = usage_stats.rank_hour_slots(((5, 20, 90),), covered_days=30)
    assert [(slot.dow, slot.hour, slot.count) for slot in ranked.busiest] == [
        (5, 20, 90)
    ]
    assert all(slot.count > 0 for slot in ranked.busiest)


def test_the_busiest_side_is_never_empty_past_the_gate():
    # The gate refuses a profile with no traffic at all, so whatever survives it
    # has at least one non-zero slot to name.
    ranked = usage_stats.rank_hour_slots(((0, 0, 1),), covered_days=400)
    assert len(ranked.busiest) == 1
    assert ranked.busiest[0].share == 1.0


def test_shares_are_of_the_whole_profile():
    ranked = usage_stats.rank_hour_slots(
        ((5, 20, 90), (2, 14, 10)), covered_days=30
    )
    assert ranked.busiest[0].share == pytest.approx(0.9)
    assert ranked.busiest[1].share == pytest.approx(0.1)
    assert ranked.quietest[0].share == 0.0


def test_the_ranking_is_total_so_two_renders_of_one_profile_agree():
    """Ties break on weekday then hour: dict order must never be able to
    reshuffle the list between two opens of the card."""
    profile = _full_profile(d3h4=1, d0h5=1, d3h3=1)
    ranked = usage_stats.rank_hour_slots(profile, covered_days=30)
    assert [(slot.dow, slot.hour) for slot in ranked.quietest] == [
        (0, 5), (3, 3), (3, 4)
    ]
    profile_reversed = tuple(reversed(profile))
    again = usage_stats.rank_hour_slots(profile_reversed, covered_days=30)
    assert [(slot.dow, slot.hour) for slot in again.quietest] == [
        (0, 5), (3, 3), (3, 4)
    ]


def test_the_busiest_side_breaks_ties_the_same_way():
    profile = _full_profile(d6h23=99, d1h2=99)
    ranked = usage_stats.rank_hour_slots(profile, covered_days=30)
    assert [(slot.dow, slot.hour) for slot in ranked.busiest][:2] == [
        (1, 2), (6, 23)
    ]


def test_an_impossible_slot_in_the_data_is_ignored_not_rendered():
    """Defence in depth against a row the CHECK constraint should have refused:
    a dow of 9 would index past the weekday labels and crash the render."""
    ranked = usage_stats.rank_hour_slots(
        ((9, 0, 500), (5, 20, 4), (0, 99, 500)), covered_days=30
    )
    assert ranked is not None
    busiest = ranked.busiest
    assert (busiest[0].dow, busiest[0].hour, busiest[0].count) == (5, 20, 4)
    assert busiest[0].share == 1.0


def test_readiness_is_its_own_question_from_emptiness():
    """The two ``None`` causes are not interchangeable to a reader, and this is
    the seam that keeps them apart."""
    assert usage_stats.hourly_is_ready(usage_stats.HOURLY_MIN_DAYS - 1) is False
    assert usage_stats.hourly_is_ready(usage_stats.HOURLY_MIN_DAYS) is True
    # Old enough, and still None - because there is no traffic, not because of
    # the calendar.
    assert usage_stats.hourly_is_ready(400) is True
    assert usage_stats.rank_hour_slots((), covered_days=400) is None


# ---------------------------------------------------------------------------
# Rendering the block
# ---------------------------------------------------------------------------
def test_the_quiet_hours_block_names_both_ends_of_the_week():
    text = "\n".join(botstats.render_quiet_hours(_persisted()))
    assert "Quietest:" in text
    assert "Busiest:" in text
    assert "Sat 20:00" in text  # dow 5, hour 20 - the busiest slot in the fixture
    assert "%" in text


def test_the_quiet_hours_block_says_the_hours_are_utc_and_that_it_fades():
    sections = botstats.render_usage(botstats.UsageCounters(), None, _persisted())
    headings = [heading for heading, _lines in sections]
    assert "Quiet hours (UTC)" in headings
    text = _flat(sections)
    assert "UTC hour of the week" in text
    assert "halves every 7 days" in text


def test_a_young_profile_says_so_instead_of_naming_a_slot():
    text = "\n".join(
        botstats.render_quiet_hours(
            _persisted(hourly_since=DAY - datetime.timedelta(days=1))
        )
    )
    assert "Not enough hourly history yet" in text
    assert "7 day(s)" in text
    assert "Quietest" not in text


def test_a_profile_that_has_never_collected_says_so():
    text = "\n".join(
        botstats.render_quiet_hours(_persisted(hourly=(), hourly_since=None))
    )
    assert "Not enough hourly history yet" in text


def test_an_old_but_empty_profile_does_not_contradict_itself():
    """The bug: ``rank_hour_slots`` returns None for two reasons and the renderer
    mapped BOTH to the day count, so a profile collecting since January with no
    rows printed "needs 7 day(s) of collection and has 232" - it has 232, which
    is more than the 7 it says it needs."""
    text = "\n".join(
        botstats.render_quiet_hours(
            _persisted(hourly=(), hourly_since=DAY - datetime.timedelta(days=231))
        )
    )
    assert "Not enough hourly history yet" not in text
    assert "No commands have been recorded" in text
    assert "Quietest" not in text
    assert "Busiest" not in text


def test_many_slots_tied_at_zero_are_counted_not_named():
    """Naming three of 166 zeros always answers "Mon 00:00, Mon 01:00, Mon
    02:00" - deterministic, but it is the start of the week rather than a
    finding. The count is the honest answer."""
    text = "\n".join(
        botstats.render_quiet_hours(_persisted(hourly=((5, 20, 90), (2, 14, 10))))
    )
    assert "166 of 168 slots recorded nothing at all" in text
    assert "Mon 00:00" not in text
    # The busy side is still named, and holds no zero-count filler.
    assert "Sat 20:00" in text
    assert "(0.0%)" not in text


def test_a_handful_of_quiet_slots_is_still_named():
    """Below the limit the zeros ARE the answer and naming them is exact, so the
    count message must not swallow a usable list."""
    text = "\n".join(
        botstats.render_quiet_hours(
            _persisted(hourly=_full_profile(d3h3=0, d3h4=0))
        )
    )
    assert "slots recorded nothing at all" not in text
    assert "Thu 03:00" in text and "Thu 04:00" in text


def test_an_unavailable_read_leaves_the_quiet_hours_block_honest():
    text = "\n".join(botstats.render_quiet_hours(None))
    assert "unavailable" in text
    assert "Quietest" not in text


def test_the_weekday_labels_are_seven_and_start_on_monday():
    labels = botstats.weekday_abbreviations()
    assert len(labels) == usage_stats.DOW_COUNT
    assert labels[0] == "Mon" and labels[6] == "Sun"


def test_a_slot_is_rendered_as_a_zero_padded_utc_hour():
    slot = usage_stats.HourSlot(dow=0, hour=4, count=1, share=0.012)
    assert botstats.format_hour_slots((slot,)) == "Mon 04:00 (1.2%)"


# ---------------------------------------------------------------------------
# Structural: the profile is two GLOBAL aggregate tables, like command_usage
# ---------------------------------------------------------------------------
def _table_ddl(name):
    with open(_SCHEMA_PATH, encoding="utf-8") as handle:
        ddl = re.sub(r"--[^\n]*", "", handle.read())
    match = re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{0}\s*\((.*?)\n\s*\)\s*;".format(name),
        ddl,
        re.S | re.I,
    )
    assert match, "{0} must be declared in schema.sql".format(name)
    return match.group(1)


def test_the_profile_table_is_a_fixed_168_slot_grid():
    body = _table_ddl("command_usage_hourly")
    assert "PRIMARY KEY (dow, hour)" in body
    assert re.search(r"^\s*count\s+BIGINT", body, re.M | re.I)
    # The CHECK is what keeps the grid a grid - and what an out-of-range hour
    # from the buffer would hit if the buffer ever stopped filtering it.
    assert "dow BETWEEN 0 AND 6" in body
    assert "hour BETWEEN 0 AND 23" in body
    assert "count >= 0" in body


def test_the_profile_marker_is_a_singleton_row():
    body = _table_ddl("command_usage_hourly_state")
    assert "id" in body and "CHECK (id = 1)" in body
    assert re.search(r"^\s*started_on\s+DATE\s+NOT NULL", body, re.M | re.I)
    assert re.search(r"^\s*halved_on\s+DATE\s+NOT NULL", body, re.M | re.I)


@pytest.mark.parametrize(
    "name", ["command_usage_hourly", "command_usage_hourly_state"]
)
def test_the_profile_tables_carry_no_guild_id_and_no_user_id(name):
    """Same derivation as command_usage: a table with neither column is global
    operational data, invisible to the guild purge and to the /mydata export by
    construction. A slot count is what the WHOLE fleet did in an hour of the
    week, which cannot describe a person."""
    body = _table_ddl(name)
    assert not re.search(r"\bguild_id\b", body, re.I)
    assert not re.search(r"\buser_id\b", body, re.I)

"""B2: the Health page of ?botstats.

Pure render / SQL-shape tests for tools/botstats_health.py, the persisted error
counter (buffer, the global error handler hook, the flush) and the card wiring in
cogs/system/botstats.py.
"""

import asyncio
import datetime
import os
import re
import types

import discord
import pytest
from discord.ext import commands

from cogs.system import botstats, errors
from tools import botstats_health as bh

DAY = datetime.date(2026, 11, 2)
NEXT_DAY = datetime.date(2026, 11, 3)


def _flat(sections):
    return "\n".join(h + "\n" + "\n".join(lines) for h, lines in sections)


def _dur(seconds):
    return "{}s".format(int(seconds))


def _snapshot(**overrides):
    """A fully green snapshot; tests degrade one field at a time."""
    base = dict(
        errors=bh.ErrorStats(DAY, 0, 0, DAY - datetime.timedelta(days=30), ()),
        nodes=[("main", "connected")],
        players=2,
        breaker=bh.BreakerState("closed", 0.0, 0, 0),
        chapters_embargo_seconds=0.0,
        timers=bh.TimerStats(0, None, (), 0),
        actions=bh.ActionStats({"done": 3}, (), 0),
        latency=0.05,
        pool=bh.PoolState(3, 2, 10),
        gateway=(1, 2),
        failed_extensions=(),
        loaded_extensions=90,
    )
    base.update(overrides)
    return bh.HealthSnapshot(**base)


# ---------------------------------------------------------------------------
# The error buffer
# ---------------------------------------------------------------------------
def test_buffer_counts_per_day_command_and_class():
    buf = bh.ErrorBuffer()
    assert buf.record(DAY, "play", "KeyError")
    buf.record(DAY, "play", "KeyError")
    buf.record(DAY, "play", "ValueError")
    buf.record(NEXT_DAY, "play", "KeyError")
    drained = buf.drain()
    assert sorted(drained.rows) == [
        (DAY, "play", "KeyError", 2),
        (DAY, "play", "ValueError", 1),
        (NEXT_DAY, "play", "KeyError", 1),
    ]
    assert buf.is_empty and buf.seen == 4


def test_buffer_ignores_a_nameless_error():
    buf = bh.ErrorBuffer()
    assert not buf.record(DAY, "", "KeyError")
    assert not buf.record(DAY, "play", "")
    assert buf.is_empty and buf.seen == 0


def test_buffer_cap_drops_new_keys_counts_them_and_keeps_existing_ones():
    buf = bh.ErrorBuffer(cap=2)
    buf.record(DAY, "a", "E")
    buf.record(DAY, "b", "E")
    assert not buf.record(DAY, "c", "E")  # new key past the cap
    assert buf.record(DAY, "a", "E")  # existing key still counts
    assert buf.dropped == 1 and buf.key_count == 2
    # the since-boot total counts every offered error, dropped ones included
    assert buf.seen == 4
    drained = buf.drain()
    assert drained.dropped == 1 and buf.dropped == 0
    assert buf.seen == 4  # a drain never resets the since-boot figure


def test_buffer_truncates_names_to_the_stored_width():
    buf = bh.ErrorBuffer()
    buf.record(DAY, "x" * 500, "Y" * 500)
    ((_day, command, error_type, _n),) = buf.drain().rows
    assert len(command) == bh.COMMAND_NAME_LIMIT
    assert len(error_type) == bh.ERROR_TYPE_LIMIT


def test_restore_folds_back_additively_within_the_cap_without_recounting():
    buf = bh.ErrorBuffer(cap=2)
    buf.record(DAY, "a", "E")
    buf.record(DAY, "b", "E")
    drained = buf.drain()
    buf.record(DAY, "a", "E")  # counted while the flush was in flight
    buf.record(DAY, "z", "E")
    seen = buf.seen
    buf.restore(drained)
    assert buf.seen == seen
    rows = {(c, e): n for _d, c, e, n in buf.drain().rows}
    assert rows == {("a", "E"): 2, ("z", "E"): 1}  # "b" refused at the cap
    assert buf.dropped == 0  # drain() reset it; the refusal was counted before


def test_restore_tallies_one_drop_per_refused_key():
    buf = bh.ErrorBuffer(cap=1)
    drained = bh.DrainedErrors(rows=[(DAY, "a", "E", 1), (DAY, "b", "E", 4)])
    buf.restore(drained)
    assert buf.dropped == 1 and buf.key_count == 1


def test_seen_on_only_answers_for_the_day_last_seen():
    buf = bh.ErrorBuffer()
    buf.record(DAY, "a", "E")
    buf.record(DAY, "a", "E")
    assert buf.seen_on(DAY) == 2
    assert buf.seen_on(NEXT_DAY) == 0
    buf.record(NEXT_DAY, "a", "E")
    assert buf.seen_on(NEXT_DAY) == 1 and buf.seen_on(DAY) == 0


# ---------------------------------------------------------------------------
# note_error: never raises, counts through the BotStats cog
# ---------------------------------------------------------------------------
def _ctx_with_cog(cog, command_name="explode"):
    bot = types.SimpleNamespace(get_cog=lambda name: cog if name == "BotStats" else None)
    return types.SimpleNamespace(
        bot=bot, command=types.SimpleNamespace(qualified_name=command_name)
    )


def test_note_error_counts_command_and_exception_class():
    cog = types.SimpleNamespace(errors=bh.ErrorBuffer())
    assert bh.note_error(_ctx_with_cog(cog), KeyError("x"))
    ((_day, command, error_type, n),) = cog.errors.drain().rows
    assert (command, error_type, n) == ("explode", "KeyError", 1)


def test_note_error_stores_a_placeholder_for_a_missing_command():
    cog = types.SimpleNamespace(errors=bh.ErrorBuffer())
    ctx = _ctx_with_cog(cog)
    ctx.command = None
    bh.note_error(ctx, RuntimeError())
    assert cog.errors.drain().rows[0][1] == bh.UNKNOWN_COMMAND


def test_note_error_stores_no_exception_text():
    cog = types.SimpleNamespace(errors=bh.ErrorBuffer())
    bh.note_error(_ctx_with_cog(cog), RuntimeError("token=hunter2 user 1234"))
    assert "hunter2" not in repr(cog.errors.drain().rows)


def test_note_error_swallows_every_failure():
    class Exploding:
        def record(self, *args, **kwargs):
            raise RuntimeError("buffer bug")

    assert bh.note_error(_ctx_with_cog(types.SimpleNamespace(errors=Exploding())), KeyError()) is False
    assert bh.note_error(_ctx_with_cog(None), KeyError()) is False  # cog not loaded
    assert bh.note_error(object(), KeyError()) is False  # no ctx.bot at all

    def boom(name):
        raise RuntimeError("mid-reload")

    ctx = types.SimpleNamespace(bot=types.SimpleNamespace(get_cog=boom), command=None)
    assert bh.note_error(ctx, KeyError()) is False


# ---------------------------------------------------------------------------
# The global error handler: which errors are counted
# ---------------------------------------------------------------------------
def _handler(bot_cog):
    bot = types.SimpleNamespace(
        user=types.SimpleNamespace(name="Yasuho"),
        on_command_error=None,
        get_cog=lambda name: bot_cog if name == "BotStats" else None,
    )
    cog = errors.Errors(bot)
    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    command = types.SimpleNamespace(qualified_name="explode", cog_name="Test", signature="")
    command.__str__ = lambda self: "explode"
    ctx = types.SimpleNamespace(
        command=command,
        cog=None,
        author=types.SimpleNamespace(
            id=42, display_avatar=types.SimpleNamespace(url="https://example.com/a.png")
        ),
        guild=types.SimpleNamespace(id=7),
        message=types.SimpleNamespace(created_at=discord.utils.utcnow()),
        prefix="!",
        clean_prefix="!",
        me=types.SimpleNamespace(mention="<@1>"),
        bot=bot,
        send=send,
        invoked_with="xyz",
        interaction=None,
    )
    return cog, ctx, sent


async def test_a_command_crash_is_counted_with_the_real_exception_class():
    stats_cog = types.SimpleNamespace(errors=bh.ErrorBuffer())
    cog, ctx, sent = _handler(stats_cog)
    await cog._on_command_error(ctx, commands.CommandInvokeError(KeyError("k")))
    ((_day, command, error_type, n),) = stats_cog.errors.drain().rows
    assert (command, error_type, n) == ("explode", "KeyError", 1)
    assert sent  # the user still got the report


async def test_a_hybrid_slash_crash_is_counted_like_a_prefix_one():
    stats_cog = types.SimpleNamespace(errors=bh.ErrorBuffer())
    cog, ctx, _sent = _handler(stats_cog)
    app_error = discord.app_commands.CommandInvokeError(
        types.SimpleNamespace(name="explode"), ValueError("v")
    )
    await cog._on_command_error(ctx, commands.HybridCommandError(app_error))
    assert [(c, e) for _d, c, e, _n in stats_cog.errors.drain().rows] == [
        ("explode", "ValueError")
    ]


async def test_an_unhandled_error_shape_is_counted_by_its_own_class():
    stats_cog = types.SimpleNamespace(errors=bh.ErrorBuffer())
    cog, ctx, _sent = _handler(stats_cog)
    await cog._on_command_error(ctx, commands.CommandError("weird"))
    assert [(c, e) for _d, c, e, _n in stats_cog.errors.drain().rows] == [
        ("explode", "CommandError")
    ]


@pytest.mark.parametrize(
    "error",
    [
        commands.MissingRequiredArgument(
            types.SimpleNamespace(name="x", displayed_name=None, annotation=str)
        ),
        commands.BadArgument("nope"),
        commands.MissingPermissions(["manage_guild"]),
        commands.BotMissingPermissions(["send_messages"]),
        commands.CheckFailure("no"),
        commands.NotOwner(),
        commands.NoPrivateMessage(),
        commands.CommandOnCooldown(commands.Cooldown(1, 5), 3.0, commands.BucketType.user),
        commands.TooManyArguments(),
        commands.DisabledCommand(),
    ],
)
async def test_user_errors_are_never_counted(error):
    stats_cog = types.SimpleNamespace(errors=bh.ErrorBuffer())
    cog, ctx, _sent = _handler(stats_cog)
    await cog._on_command_error(ctx, error)
    assert stats_cog.errors.is_empty and stats_cog.errors.seen == 0


async def test_command_not_found_is_not_counted():
    stats_cog = types.SimpleNamespace(errors=bh.ErrorBuffer())
    cog, ctx, _sent = _handler(stats_cog)
    ctx.command = None
    ctx.bot.get_command = lambda name: None
    ctx.bot.commands = []
    ctx.message.content = "!zz"
    await cog._on_command_error(ctx, commands.CommandNotFound("zz"))
    assert stats_cog.errors.seen == 0


async def test_a_broken_counter_never_breaks_the_error_report():
    class Exploding:
        def record(self, *args, **kwargs):
            raise RuntimeError("buffer bug")

    cog, ctx, sent = _handler(types.SimpleNamespace(errors=Exploding()))
    await cog._on_command_error(ctx, commands.CommandInvokeError(KeyError("k")))
    assert sent and "embed" in sent[0][1]

    cog, ctx, sent = _handler(None)  # BotStats cog not loaded
    await cog._on_command_error(ctx, commands.CommandInvokeError(KeyError("k")))
    assert sent


# ---------------------------------------------------------------------------
# The flush: the ONLY writer, additive, bounded
# ---------------------------------------------------------------------------
def test_flush_sql_is_an_additive_upsert_over_four_parallel_arrays():
    sql = bh.FLUSH
    assert "INSERT INTO command_errors" in sql
    assert "unnest($1::date[], $2::text[], $3::text[], $4::bigint[])" in sql
    assert "ON CONFLICT (day, command, error_type)" in sql
    assert "command_errors.count + EXCLUDED.count" in sql
    assert sql.count(";") == 1  # one statement


def test_build_flush_payload_aligns_the_arrays():
    buf = bh.ErrorBuffer()
    buf.record(DAY, "a", "E1")
    buf.record(DAY, "b", "E2", count=3)
    days, commands_, types_, counts = bh.build_flush_payload(buf.drain())
    assert len(days) == len(commands_) == len(types_) == len(counts) == 2
    assert dict(zip(zip(commands_, types_), counts)) == {("a", "E1"): 1, ("b", "E2"): 3}


class _FlushPool:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def execute(self, query, *args, **kwargs):
        self.calls.append(("execute", query, args))
        if self.fail and query is bh.FLUSH:
            raise RuntimeError("pool gone")

    async def fetchrow(self, query, *args, **kwargs):
        self.calls.append(("fetchrow", query, args))
        return {"rows": 0}

    async def fetch(self, query, *args, **kwargs):
        self.calls.append(("fetch", query, args))
        return []


def _stats_cog(pool):
    bot = types.SimpleNamespace(db_pool=pool)
    return botstats.BotStats(bot)


async def test_flush_writes_the_errors_once_and_empties_the_buffer():
    pool = _FlushPool()
    cog = _stats_cog(pool)
    cog.errors.record(DAY, "play", "KeyError")
    cog.errors.record(DAY, "play", "KeyError")
    await cog.flush_usage(today=DAY)
    writes = [c for c in pool.calls if c[1] is bh.FLUSH]
    assert len(writes) == 1
    assert writes[0][2] == ([DAY], ["play"], ["KeyError"], [2])
    assert cog.errors.is_empty
    await cog.flush_usage(today=DAY)
    assert len([c for c in pool.calls if c[1] is bh.FLUSH]) == 1  # nothing to write


async def test_an_idle_flush_adds_no_error_statement():
    pool = _FlushPool()
    cog = _stats_cog(pool)
    await cog.flush_usage(today=DAY)
    assert [c for c in pool.calls if c[1] is bh.FLUSH] == []


async def test_a_failed_error_flush_hands_the_generation_back_and_keeps_pruning():
    pool = _FlushPool(fail=True)
    cog = _stats_cog(pool)
    cog.errors.record(DAY, "play", "KeyError")
    await cog.flush_usage(today=DAY)  # must not raise
    assert not cog.errors.is_empty  # restored, to be retried next tick
    assert any(c[1] is bh.PRUNE for c in pool.calls)  # the daily hook still ran


async def test_a_cancelled_error_flush_is_handed_back_too():
    never = asyncio.Event()

    class Hanging(_FlushPool):
        async def execute(self, query, *args, **kwargs):
            await never.wait()

    cog = _stats_cog(Hanging())
    cog.errors.record(DAY, "play", "KeyError")
    task = asyncio.ensure_future(cog._write_errors())
    await asyncio.sleep(0)
    assert cog.errors.is_empty  # drained before the await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not cog.errors.is_empty


async def test_the_error_cap_overflow_is_logged_once_per_flush(caplog):
    pool = _FlushPool()
    cog = _stats_cog(pool)
    cog.errors = bh.ErrorBuffer(cap=1)
    for name in ("a", "b", "c"):
        cog.errors.record(DAY, name, "E")
    with caplog.at_level("WARNING", logger=botstats.log.name):
        await cog.flush_usage(today=DAY)
    assert len([r for r in caplog.records if "error buffer cap" in r.getMessage()]) == 1
    assert cog._flush_stats["error_dropped"] == 2


async def test_the_daily_prune_cuts_errors_at_the_retention_window():
    pool = _FlushPool()
    cog = _stats_cog(pool)
    await cog.flush_usage(today=DAY)
    (call,) = [c for c in pool.calls if c[1] is bh.PRUNE]
    assert call[2] == (DAY - datetime.timedelta(days=bh.RETENTION_DAYS),)
    assert "DELETE FROM command_errors WHERE day < $1::date" in bh.PRUNE


async def test_unload_flushes_errors_even_when_no_command_ran():
    pool = _FlushPool()
    cog = _stats_cog(pool)
    cog.errors.record(DAY, "play", "KeyError")
    await cog.cog_unload()
    assert len([c for c in pool.calls if c[1] is bh.FLUSH]) == 1


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def _schema():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "schema.sql")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_command_errors_table_is_additive_and_holds_nothing_personal():
    schema = _schema()
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS command_errors \((.*?)\n\);", schema, re.S
    )
    assert match
    ddl = match.group(1)
    assert "PRIMARY KEY (day, command, error_type)" in ddl
    for forbidden in ("user_id", "guild_id", "channel_id", "message"):
        assert forbidden not in ddl


# ---------------------------------------------------------------------------
# Fetchers (query shape + row mapping)
# ---------------------------------------------------------------------------
class _ReadPool:
    def __init__(self, fetch=None, fetchrow=None):
        self.calls = []
        self._fetch = fetch or (lambda q: [])
        self._fetchrow = fetchrow or (lambda q: None)

    async def fetch(self, query, *args, **kwargs):
        self.calls.append(("fetch", query, args, kwargs))
        return self._fetch(query)

    async def fetchrow(self, query, *args, **kwargs):
        self.calls.append(("fetchrow", query, args, kwargs))
        return self._fetchrow(query)


async def test_fetch_error_stats_maps_rows_and_bounds_every_statement():
    pool = _ReadPool(
        fetchrow=lambda q: {"today": 2, "week": 9, "since": DAY},
        fetch=lambda q: [{"command": "play", "error_type": "KeyError", "total": 9}],
    )
    stats = await bh.fetch_error_stats(pool, timeout=7, today=DAY)
    assert (stats.today, stats.week, stats.since) == (2, 9, DAY)
    assert stats.top == (("play", "KeyError", 9),)
    assert all(call[3] == {"timeout": 7} for call in pool.calls)
    assert pool.calls[0][2] == (DAY, bh.ERROR_WINDOW_DAYS)
    assert pool.calls[1][2][2] == bh.ERROR_TOP_LIMIT


async def test_fetch_error_stats_on_an_empty_table_is_zero_with_no_history():
    stats = await bh.fetch_error_stats(_ReadPool(), timeout=1, today=DAY)
    assert (stats.today, stats.week, stats.since, stats.top) == (0, 0, None, ())
    assert stats.covered_days == 0


async def test_fetch_timer_stats_splits_the_three_arms():
    rows = [
        {"kind": "total", "event": None, "n": 3, "age": 900.5},
        {"kind": "event", "event": "reminder", "n": 2, "age": None},
        {"kind": "event", "event": "tempban", "n": 1, "age": None},
        {"kind": "retrying", "event": None, "n": 4, "age": None},
    ]
    stats = await bh.fetch_timer_stats(_ReadPool(fetch=lambda q: rows), timeout=1)
    assert stats == bh.TimerStats(3, 900.5, (("reminder", 2), ("tempban", 1)), 4)


async def test_fetch_timer_stats_with_nothing_overdue_has_no_age():
    rows = [
        {"kind": "total", "event": None, "n": 0, "age": None},
        {"kind": "retrying", "event": None, "n": 0, "age": None},
    ]
    stats = await bh.fetch_timer_stats(_ReadPool(fetch=lambda q: rows), timeout=1)
    assert stats.overdue == 0 and stats.oldest_seconds is None


async def test_fetch_action_stats_splits_the_three_arms():
    rows = [
        {"section": "status", "key": "done", "n": 5},
        {"section": "status", "key": "failed", "n": 2},
        {"section": "failed", "key": "set_prefix", "n": 2},
        {"section": "stuck", "key": None, "n": 1},
    ]
    pool = _ReadPool(fetch=lambda q: rows)
    stats = await bh.fetch_action_stats(pool, timeout=3)
    assert stats == bh.ActionStats({"done": 5, "failed": 2}, (("set_prefix", 2),), 1)
    assert pool.calls[0][2] == (
        bh.ACTION_FAILED_DAYS,
        bh.ACTION_FAILED_LIMIT,
        bh.ACTION_STUCK_MINUTES,
        bh.ACTION_STATUS_HOURS,
    )


def test_action_statuses_match_the_schema_constraint():
    schema = _schema()
    match = re.search(r"dashboard_actions_status_valid\s+CHECK \(status IN \(([^)]*)\)", schema)
    assert match
    declared = tuple(re.findall(r"'(\w+)'", match.group(1)))
    assert declared == bh.ACTION_STATUSES


# ---------------------------------------------------------------------------
# Live readers
# ---------------------------------------------------------------------------
class _Node:
    def __init__(self, node_id, connected=False, connecting=False):
        self.id = node_id
        self.is_connected = connected
        self.is_connecting = connecting


def test_node_states_reports_sonolinks_status_as_is():
    client = types.SimpleNamespace(
        nodes=[_Node("a", connected=True), _Node("b", connecting=True), _Node("c")]
    )
    assert bh.node_states(client) == [
        ("a", "connected"),
        ("b", "connecting"),
        ("c", "disconnected"),
    ]


def test_node_states_unknown_without_a_client_or_on_a_read_failure():
    assert bh.node_states(None) is None
    assert bh.node_states(types.SimpleNamespace()) is None  # no .nodes


def test_node_states_with_no_node_is_empty_not_unknown():
    assert bh.node_states(types.SimpleNamespace(nodes=[])) == []


class _Breaker:
    def __init__(self, tripped, left, opens=1, skipped=5):
        self.tripped = tripped
        self._left = left
        self.opens = opens
        self.skipped = skipped

    def seconds_left(self):
        return self._left


def test_breaker_state_covers_closed_open_and_half_open():
    assert bh.breaker_state(_Breaker(False, 0.0)).state == "closed"
    opened = bh.breaker_state(_Breaker(True, 120.0))
    assert (opened.state, opened.seconds_left) == ("open", 120.0)
    assert bh.breaker_state(_Breaker(True, 0.0)).state == "half-open"
    assert bh.breaker_state(None) is None
    assert bh.breaker_state(object()) is None


def test_the_real_breaker_maps_onto_the_three_states():
    from cogs.anilist.breaker import AniListBreaker, CircuitOpen

    now = [0.0]
    breaker = AniListBreaker(clock=lambda: now[0])
    assert bh.breaker_state(breaker).state == "closed"
    for _ in range(3):
        breaker.require_closed("t")
        breaker.note_failure("t", "x")
    state = bh.breaker_state(breaker)
    assert state.state == "open" and state.seconds_left > 0 and state.opens == 1
    with pytest.raises(CircuitOpen):
        breaker.require_closed("t")
    now[0] += 10_000
    assert bh.breaker_state(breaker).state == "half-open"
    breaker.note_success("t")
    assert bh.breaker_state(breaker).state == "closed"


def test_pool_state_and_saturation():
    pool = types.SimpleNamespace(get_size=lambda: 10, get_idle_size=lambda: 0, get_max_size=lambda: 10)
    assert bh.pool_state(pool).saturated
    pool = types.SimpleNamespace(get_size=lambda: 1, get_idle_size=lambda: 0, get_max_size=lambda: 30)
    assert not bh.pool_state(pool).saturated  # headroom left: lazy pool, not saturation
    assert bh.pool_state(None) is None
    assert bh.pool_state(object()) is None


def test_finite_latency_hides_nan_and_inf():
    assert bh.finite_latency(float("nan")) is None
    assert bh.finite_latency(float("inf")) is None
    assert bh.finite_latency(None) is None
    assert bh.finite_latency(0.25) == 0.25


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def test_all_green():
    assert bh.render_verdict(_snapshot()) == ["All green"]


def test_each_degradation_is_an_issue():
    cases = {
        "Lavalink node not connected": dict(nodes=[("main", "connecting")]),
        "no Lavalink node": dict(nodes=[]),
        "AniList breaker open": dict(breaker=bh.BreakerState("open", 90.0, 1, 3)),
        "AniList breaker half-open": dict(breaker=bh.BreakerState("half-open", 0.0, 1, 3)),
        "2 overdue timer(s)": dict(timers=bh.TimerStats(2, 400.0, (), 0)),
        "1 stuck dashboard action(s)": dict(actions=bh.ActionStats({}, (), 1)),
        "3 error(s) today": dict(errors=bh.ErrorStats(DAY, 3, 3, DAY, ())),
        "database pool saturated": dict(pool=bh.PoolState(10, 0, 10)),
        "1 extension(s) not loaded": dict(failed_extensions=("cogs.x",)),
        "MangaDex paused by a rate limit": dict(chapters_embargo_seconds=30.0),
    }
    for expected, override in cases.items():
        line = bh.render_verdict(_snapshot(**override))[0]
        assert line.startswith("1 issue(s):") and expected in line, (expected, line)


def test_unreadable_mangadex_latency_and_gateway_are_listed_not_green():
    line = bh.render_verdict(
        _snapshot(chapters_embargo_seconds=None, latency=None, gateway=None)
    )[0]
    assert line.startswith("No issue found, but 3 check(s) could not be read")
    for name in ("MangaDex", "gateway latency", "gateway counters"):
        assert name in line


def test_stuck_pending_actions_are_judged_by_updated_at():
    # A row the boot reconcile put back to pending keeps its old created_at.
    assert "updated_at < now() - $3::int" in bh.ACTION_HEALTH
    assert "created_at < now() - $3" not in bh.ACTION_HEALTH


def test_issue_count_and_list():
    line = bh.render_verdict(
        _snapshot(
            nodes=[("main", "disconnected")],
            timers=bh.TimerStats(1, 700.0, (("reminder", 1),), 0),
        )
    )[0]
    assert line.startswith("2 issue(s):")
    assert "Lavalink node not connected" in line and "1 overdue timer(s)" in line


def test_a_fresh_in_memory_error_counts_before_the_first_flush():
    line = bh.render_verdict(_snapshot(errors_today_in_memory=2))[0]
    assert "2 error(s) today" in line


def test_old_persisted_errors_are_not_an_issue_today():
    stats = bh.ErrorStats(DAY, 0, 12, DAY, (("play", "KeyError", 12),))
    assert bh.render_verdict(_snapshot(errors=stats)) == ["All green"]


def test_unknown_checks_are_never_green_and_never_issues():
    line = bh.render_verdict(_snapshot(nodes=None, breaker=None))[0]
    assert line.startswith("No issue found, but 2 check(s) could not be read")
    assert "Lavalink" in line and "AniList breaker" in line
    assert "All green" not in line


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def test_green_page_has_every_block():
    sections = bh.render_health(_snapshot(errors_since_boot=0), fmt_duration=_dur)
    assert [h for h, _l in sections] == [
        "Verdict",
        "Errors",
        "Lavalink",
        "AniList and MangaDex",
        "Timers",
        "Dashboard actions",
        "Process",
    ]
    text = _flat(sections)
    assert "Node main: connected" in text and "Active players: 2" in text
    assert "AniList breaker: closed" in text
    assert "No timer is more than 5 minutes overdue." in text
    assert "Gateway latency: 50 ms" in text
    assert "Database pool: 3 open, 2 idle, max 10" in text
    assert "Gateway since boot: 1 resume(s), 2 disconnect(s)" in text


def test_degraded_page_shows_the_details():
    snap = _snapshot(
        errors=bh.ErrorStats(
            DAY, 4, 20, DAY - datetime.timedelta(days=2),
            (("play", "KeyError", 15), ("queue", "ValueError", 5)),
        ),
        errors_since_boot=6,
        errors_dropped=2,
        nodes=[("main", "connecting")],
        breaker=bh.BreakerState("open", 600.0, 2, 14),
        chapters_embargo_seconds=300.0,
        timers=bh.TimerStats(5, 3700.0, (("reminder", 4), ("tempban", 1)), 2),
        actions=bh.ActionStats(
            {"done": 8, "pending": 1, "failed": 2}, (("set_prefix", 2),), 1
        ),
        failed_extensions=("cogs.a", "cogs.b"),
    )
    text = _flat(bh.render_health(snap, fmt_duration=_dur))
    assert "Today (UTC): 4 - last 7 days: 20" in text
    assert "play / KeyError: 15" in text
    assert "Oldest recorded error: %s" % (DAY - datetime.timedelta(days=2)).isoformat() in text
    assert "Counting is recent" not in text
    assert "Since boot: 6 (this process only) - 2 not counted (buffer full)" in text
    assert "Node main: connecting" in text
    assert "AniList breaker: open - next probe in 600s" in text
    assert "Outages since boot: 2 - poller requests skipped: 14" in text
    assert "MangaDex chapter alerts: paused by a rate limit, 300s left" in text
    assert "5 timer(s) overdue by more than 5 minutes" in text
    assert "Oldest overdue: 3700s" in text and "reminder: 4" in text
    assert "Timers retrying after a failed delivery: 2" in text
    assert "Last 24 h: pending 1, done 8, failed 2" in text
    assert "Pending for over 10 minutes: 1" in text
    assert "set_prefix: 2" in text
    assert "Extensions not loaded: cogs.a, cogs.b" in text


def test_every_unknown_renders_as_unknown_never_zero():
    snap = bh.HealthSnapshot()  # nothing could be read
    text = _flat(bh.render_health(snap, fmt_duration=_dur))
    assert "Persisted error counts are unavailable right now." in text
    assert "Lavalink nodes: unknown" in text
    assert "Active players: unknown" in text
    assert "AniList breaker: unknown" in text
    assert "MangaDex chapter alerts: unknown" in text
    assert "Timer health is unavailable right now." in text
    assert "Dashboard action health is unavailable right now." in text
    assert "Gateway latency: unknown" in text
    assert "Database pool: unknown" in text
    assert "Gateway since boot: unknown" in text
    assert "Extensions: unknown" in text
    assert "All green" not in text
    assert "Active players: 0" not in text and "Gateway latency: 0" not in text


def test_no_node_is_stated_not_hidden():
    text = _flat(bh.render_health(_snapshot(nodes=[]), fmt_duration=_dur))
    assert "No Lavalink node is registered." in text


def test_nodes_and_top_lists_are_truncated():
    nodes = [("n%d" % i, "connected") for i in range(bh.NODE_LIMIT + 3)]
    lines = bh.render_lavalink(_snapshot(nodes=nodes))
    assert lines[bh.NODE_LIMIT] == "... and 3 more"


def test_an_empty_error_week_says_so_and_a_failed_read_is_not_a_zero():
    quiet = bh.ErrorStats(DAY, 0, 0, DAY - datetime.timedelta(days=40), ())
    text = _flat(bh.render_health(_snapshot(errors=quiet), fmt_duration=_dur))
    assert "No unexpected error in the last 7 days." in text
    assert "Oldest recorded error: %s" % (DAY - datetime.timedelta(days=40)).isoformat() in text


def test_the_whole_page_fits_the_card_text_budget():
    snap = _snapshot(
        errors=bh.ErrorStats(
            DAY, 999999, 999999, DAY,
            tuple(("some very long command name %d" % i, "SomeLongExceptionName", 99999) for i in range(8)),
        ),
        nodes=[("node-%d" % i, "connecting") for i in range(bh.NODE_LIMIT)],
        timers=bh.TimerStats(99999, 99999.0, tuple(("event_name_%d" % i, 9999) for i in range(5)), 99),
        actions=bh.ActionStats(
            {"pending": 9999, "running": 9999, "done": 9999, "failed": 9999},
            tuple(("kind_name_%d" % i, 999) for i in range(5)),
            9999,
        ),
        failed_extensions=tuple("cogs.module_%d" % i for i in range(20)),
    )
    assert len(_flat(bh.render_health(snap, fmt_duration=_dur))) < 3500


# ---------------------------------------------------------------------------
# Card wiring
# ---------------------------------------------------------------------------
class _CardPool:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def _go(self, query):
        self.calls.append(query)
        if self.fail:
            raise RuntimeError("boom")

    async def fetchrow(self, query, *args, **kwargs):
        self._go(query)
        return {"today": 1, "week": 3, "since": DAY}

    async def fetch(self, query, *args, **kwargs):
        self._go(query)
        if query is bh.ERROR_TOP:
            return [{"command": "play", "error_type": "KeyError", "total": 3}]
        if query is bh.TIMER_HEALTH:
            return [
                {"kind": "total", "event": None, "n": 0, "age": None},
                {"kind": "retrying", "event": None, "n": 0, "age": None},
            ]
        return [{"section": "stuck", "key": None, "n": 0}]

    def get_size(self):
        return 2

    def get_idle_size(self):
        return 1

    def get_max_size(self):
        return 10


def _card(pool, **bot_attrs):
    bot = types.SimpleNamespace(
        guilds=[],
        users=[],
        latency=float("nan"),
        shard_count=None,
        db_pool=pool,
        voice_clients=[],
        extensions={"a": 1, "b": 2},
        failed_extensions=(),
        sl_client=types.SimpleNamespace(nodes=[_Node("main", connected=True)]),
        get_cog=lambda name: None,
    )
    for key, value in bot_attrs.items():
        setattr(bot, key, value)
    return botstats.BotStatsDashboard(botstats.BotStats(bot), author_id=1)


async def test_health_page_loads_and_memoises_its_reads():
    pool = _CardPool()
    card = _card(pool)
    sections, _taken = await card._load(botstats.PAGE_HEALTH)
    text = _flat(sections)
    assert "play / KeyError: 3" in text
    assert "Node main: connected" in text
    assert "Gateway latency: unknown" in text  # nan latency is unknown, not "nan ms"
    assert "Active players: 0" in text  # a real, readable zero
    assert "AniList breaker: unknown" in text  # no breaker was ever created
    assert "Extensions: 2 loaded, none failed" in text
    first_calls = len(pool.calls)
    await card._load(botstats.PAGE_HEALTH)
    assert len(pool.calls) == first_calls  # every DB read memoised


async def test_health_page_live_half_is_recomputed_on_every_open():
    pool = _CardPool()
    card = _card(pool)
    first, _t = await card._load(botstats.PAGE_HEALTH)
    card.bot.sl_client = types.SimpleNamespace(nodes=[_Node("main", connecting=True)])
    second, _t = await card._load(botstats.PAGE_HEALTH)
    assert "Node main: connected" in _flat(first)
    assert "Node main: connecting" in _flat(second)


async def test_health_page_failed_reads_are_unknown_and_retried():
    pool = _CardPool(fail=True)
    card = _card(pool)
    sections, _t = await card._load(botstats.PAGE_HEALTH)
    text = _flat(sections)
    assert "Persisted error counts are unavailable right now." in text
    assert "Timer health is unavailable right now." in text
    assert "Dashboard action health is unavailable right now." in text
    assert "All green" not in text
    first_calls = len(pool.calls)
    await card._load(botstats.PAGE_HEALTH)
    assert len(pool.calls) > first_calls  # a failure is not memoised


async def test_health_page_without_lavalink_or_extension_info_is_unknown():
    card = _card(_CardPool(), sl_client=None)
    del card.bot.failed_extensions
    text = _flat((await card._load(botstats.PAGE_HEALTH))[0])
    assert "Lavalink nodes: unknown" in text
    assert "Extensions: unknown" in text


async def test_the_page_adds_no_timer_and_no_writer():
    """On-demand only: opening the page never writes, and the cog's only loop is
    the existing flush loop."""
    pool = _CardPool()
    card = _card(pool)
    await card._load(botstats.PAGE_HEALTH)
    assert not any("INSERT" in str(q) or "DELETE" in str(q) for q in pool.calls)
    assert not hasattr(botstats.BotStats, "_health_loop")


async def test_card_lays_out_nine_buttons_in_three_rows():
    card = _card(_CardPool())
    sections, taken = await card._load(botstats.PAGE_HEALTH)
    card.index = botstats.PAGE_HEALTH
    card._rerender(sections, taken)
    container = card.children[0]
    rows = [
        c
        for c in container.children
        if hasattr(c, "children")
        and any(isinstance(i, botstats._PageButton) for i in c.children)
    ]
    assert [len(r.children) for r in rows] == [3, 3, 3]


def test_page_identity_is_append_only():
    assert botstats.PAGE_HEALTH == 6
    assert (
        botstats.PAGE_OVERVIEW,
        botstats.PAGE_GUILDS,
        botstats.PAGE_USAGE,
        botstats.PAGE_DATA,
        botstats.PAGE_FEATURES,
        botstats.PAGE_PREMIUM,
    ) == (0, 1, 2, 3, 4, 5)
    assert set(botstats.BotStatsDashboard.PAGE_READS) == set(range(9))
    assert botstats.page_labels()[botstats.PAGE_HEALTH] == "Health"


def test_worst_case_labels_stay_inside_the_components_v2_text_budget():
    """Discord caps a V2 message's TOTAL text at 4000 characters. Every
    free-text field at its longest (64 for stored names, 80 for the rest) with
    every list at its cap, plus room for the title and footer."""
    long = lambda tag: (tag * 80)[:80]  # noqa: E731
    snap = _snapshot(
        errors=bh.ErrorStats(
            DAY, 999999, 999999, DAY - datetime.timedelta(days=30),
            tuple(("c" * bh.COMMAND_NAME_LIMIT, "E" * bh.ERROR_TYPE_LIMIT, 999999)
                  for _n in range(bh.ERROR_TOP_LIMIT)),
        ),
        errors_since_boot=999999,
        errors_dropped=999999,
        nodes=[(long("n"), "disconnected") for _n in range(bh.NODE_LIMIT + 3)],
        breaker=bh.BreakerState("open", 99999.0, 9999, 999999),
        chapters_embargo_seconds=99999.0,
        timers=bh.TimerStats(
            999999, 999999.0,
            tuple((long("e"), 999999) for _n in range(bh.TIMER_TOP_LIMIT)), 999999,
        ),
        actions=bh.ActionStats(
            {long("s%d" % i): 999999 for i in range(4)},
            tuple((long("k"), 999999) for _n in range(bh.ACTION_FAILED_LIMIT)),
            999999,
        ),
        failed_extensions=tuple(long("x") for _n in range(40)),
    )
    text = _flat(bh.render_health(snap, fmt_duration=_dur))
    assert len(text) + 300 < 4000, len(text)


def test_missing_extensions_are_computed_against_what_is_loaded_now():
    bot = types.SimpleNamespace(
        expected_extensions=("cogs.a", "cogs.b", "cogs.c"),
        extensions={"cogs.a": 1, "cogs.c": 1},
        failed_extensions=("cogs.zzz",),
    )
    assert botstats.missing_extensions(bot) == ("cogs.b",)
    bot.extensions["cogs.b"] = 1  # a later ?reload fixed it
    assert botstats.missing_extensions(bot) == ()
    del bot.extensions["cogs.c"]  # one that fails later shows up
    assert botstats.missing_extensions(bot) == ("cogs.c",)
    # No discovery list: the boot failure list, filtered against what is loaded.
    del bot.expected_extensions
    bot.extensions["cogs.zzz"] = 1
    assert botstats.missing_extensions(bot) == ()
    assert botstats.missing_extensions(types.SimpleNamespace()) is None

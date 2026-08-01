"""BS1: the owner bot-stats dashboard (cogs/system/botstats.py).

Covers, in order of how much it would hurt to get wrong:

1. The HONESTY rules the pages promise. An unknown member count never sorts or
   renders as zero, and the observed-activity block never publishes a silence
   nobody measured: zero observed servers prints "not observed", not "0
   messages". These are the same rules cogs/community/serverstats renders
   against, restated for the bot-wide surface.
2. The usage counters: O(1) increments, the prefix/slash split, a deterministic
   top-N, and - the one that actually bites - a hybrid slash invocation counted
   EXACTLY ONCE despite discord.py dispatching both completion events for it.
3. /proc parsing against real text fixtures, including the comm field with
   spaces and parentheses in it (the classic off-by-N that would report an
   uptime of centuries).
4. The card itself: each page's reads run only when that page is opened, the
   snapshot is reused afterwards, and a DB failure degrades one page rather
   than the click.

Everything here is offline: no bot, no pool, no Discord. The page builders take
plain data on purpose so they are testable without any of them.
"""

import asyncio
import datetime
import types

import pytest

from cogs.system import botstats, usage_stats

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _Pool:
    """asyncpg pool stand-in that accepts the module's ``timeout=`` kwarg.

    conftest's shared ``FakePool`` does not take keyword arguments, and every
    read in botstats.py passes a per-statement timeout, so this local double
    exists purely to keep that (deliberate) bound testable.
    """

    def __init__(self, *, fetch_return=None, fetchrow_return=None, fetchval_return=None):
        self.calls = []
        self._fetch = fetch_return if fetch_return is not None else []
        self._fetchrow = fetchrow_return
        self._fetchval = fetchval_return
        self.fail = False

    def _record(self, method, query, args):
        self.calls.append((method, query, args))
        if self.fail:
            raise RuntimeError("boom")

    async def fetch(self, query, *args, **kwargs):
        self._record("fetch", query, args)
        value = self._fetch
        return value(query) if callable(value) else value

    async def fetchrow(self, query, *args, **kwargs):
        self._record("fetchrow", query, args)
        value = self._fetchrow
        return value(query) if callable(value) else value

    async def fetchval(self, query, *args, **kwargs):
        self._record("fetchval", query, args)
        return self._fetchval


def _guild(guild_id, name="Guild", member_count=10, joined_at=NOW, channels=3):
    me = types.SimpleNamespace(joined_at=joined_at) if joined_at is not None else None
    return types.SimpleNamespace(
        id=guild_id,
        name=name,
        member_count=member_count,
        me=me,
        channels=[object()] * channels,
    )


def _cog(bot=None):
    """A BotStats cog built without add_cog (no listeners registered, no loop)."""
    return botstats.BotStats(bot or types.SimpleNamespace())


# ---------------------------------------------------------------------------
# Usage counters
# ---------------------------------------------------------------------------
def test_usage_counters_start_empty():
    counters = botstats.UsageCounters(started_at=NOW)
    assert counters.total == 0
    assert counters.top() == []


def test_usage_counters_increment_per_name_and_surface():
    counters = botstats.UsageCounters(started_at=NOW)
    counters.record("play")
    counters.record("play")
    counters.record("rank", slash=True)
    assert counters.commands["play"] == 2
    assert counters.commands["rank"] == 1
    assert counters.prefix_total == 2
    assert counters.slash_total == 1
    assert counters.total == 3


def test_usage_counters_ignore_an_empty_name():
    counters = botstats.UsageCounters(started_at=NOW)
    counters.record("")
    counters.record(None)
    assert counters.total == 0
    assert counters.commands == {}


def test_usage_counters_top_is_ranked_and_capped():
    counters = botstats.UsageCounters(started_at=NOW)
    for _unused in range(5):
        counters.record("play")
    for _unused in range(3):
        counters.record("rank")
    counters.record("ping")
    assert counters.top(2) == [("play", 5), ("rank", 3)]


def test_usage_counters_top_breaks_ties_by_name_not_insertion_order():
    """Counter.most_common ties on insertion order, which would reshuffle the
    tail between two renders of the very same data."""
    counters = botstats.UsageCounters(started_at=NOW)
    for name in ("zebra", "alpha", "middle"):
        counters.record(name)
    assert counters.top(3) == [("alpha", 1), ("middle", 1), ("zebra", 1)]


def test_usage_counters_cardinality_is_bounded_by_distinct_names():
    counters = botstats.UsageCounters(started_at=NOW)
    for _unused in range(1000):
        counters.record("play")
    assert len(counters.commands) == 1
    assert counters.total == 1000


# ---------------------------------------------------------------------------
# Completion listeners: exactly-once for a hybrid slash invocation
# ---------------------------------------------------------------------------
def test_is_hybrid_app_command_reads_discord_py_marker():
    hybrid = types.SimpleNamespace(__commands_is_hybrid_app_command__=True)
    plain = types.SimpleNamespace()
    assert botstats.is_hybrid_app_command(hybrid) is True
    assert botstats.is_hybrid_app_command(plain) is False


async def test_on_command_completion_counts_a_prefix_invocation():
    cog = _cog()
    ctx = types.SimpleNamespace(
        command=types.SimpleNamespace(qualified_name="play"), interaction=None
    )
    await cog.on_command_completion(ctx)
    assert cog.usage.commands["play"] == 1
    assert cog.usage.prefix_total == 1
    assert cog.usage.slash_total == 0


async def test_on_command_completion_counts_a_hybrid_slash_as_slash():
    cog = _cog()
    ctx = types.SimpleNamespace(
        command=types.SimpleNamespace(qualified_name="rank"),
        interaction=object(),
    )
    await cog.on_command_completion(ctx)
    assert cog.usage.slash_total == 1
    assert cog.usage.prefix_total == 0


async def test_on_command_completion_ignores_a_missing_command():
    cog = _cog()
    await cog.on_command_completion(types.SimpleNamespace(command=None))
    assert cog.usage.total == 0


async def test_on_app_command_completion_counts_a_pure_slash_command():
    cog = _cog()
    command = types.SimpleNamespace(qualified_name="serverstats")
    await cog.on_app_command_completion(object(), command)
    assert cog.usage.commands["serverstats"] == 1
    assert cog.usage.slash_total == 1


async def test_a_hybrid_slash_invocation_is_counted_exactly_once():
    """discord.py dispatches BOTH completion events for one hybrid slash call
    (the tree dispatches app_command_completion, the wrapped ext command's
    after-hooks dispatch command_completion). Counting both would double every
    hybrid in the ranking."""
    cog = _cog()
    ctx = types.SimpleNamespace(
        command=types.SimpleNamespace(qualified_name="rank"), interaction=object()
    )
    app_command = types.SimpleNamespace(
        qualified_name="rank", __commands_is_hybrid_app_command__=True
    )
    await cog.on_command_completion(ctx)
    await cog.on_app_command_completion(object(), app_command)
    assert cog.usage.commands["rank"] == 1
    assert cog.usage.total == 1


async def test_on_app_command_completion_ignores_a_nameless_command():
    cog = _cog()
    await cog.on_app_command_completion(object(), types.SimpleNamespace())
    assert cog.usage.total == 0


# ---------------------------------------------------------------------------
# Guild shaping
# ---------------------------------------------------------------------------
def test_collect_guild_rows_reads_every_field():
    rows = botstats.collect_guild_rows([_guild(1, "Alpha", 42, NOW, channels=3)])
    assert rows == [
        botstats.GuildRow(
            guild_id=1, name="Alpha", member_count=42, joined_at=NOW, channels=3
        )
    ]


def test_collect_guild_rows_tolerates_a_guild_without_me_or_name():
    guild = types.SimpleNamespace(id=7, name=None, member_count=None, me=None)
    (row,) = botstats.collect_guild_rows([guild])
    assert row.name == "7"
    assert row.member_count is None
    assert row.joined_at is None


def test_top_guild_rows_sorts_by_member_count_descending():
    rows = botstats.collect_guild_rows(
        [_guild(1, "Small", 10), _guild(2, "Big", 900), _guild(3, "Mid", 100)]
    )
    assert [r.name for r in botstats.top_guild_rows(rows)] == ["Big", "Mid", "Small"]


def test_top_guild_rows_sorts_an_unknown_count_last_never_as_zero():
    rows = botstats.collect_guild_rows(
        [_guild(1, "Unknown", None), _guild(2, "Tiny", 1)]
    )
    ordered = botstats.top_guild_rows(rows)
    assert [r.name for r in ordered] == ["Tiny", "Unknown"]
    assert ordered[-1].member_count is None


def test_top_guild_rows_breaks_ties_on_guild_id():
    rows = botstats.collect_guild_rows(
        [_guild(30, "C", 5), _guild(10, "A", 5), _guild(20, "B", 5)]
    )
    assert [r.guild_id for r in botstats.top_guild_rows(rows)] == [10, 20, 30]


def test_top_guild_rows_honours_the_limit():
    rows = botstats.collect_guild_rows(
        [_guild(i, "G{0}".format(i), i) for i in range(1, 40)]
    )
    assert len(botstats.top_guild_rows(rows, 15)) == 15


def test_guild_totals_excludes_unknown_counts_from_the_sum():
    rows = botstats.collect_guild_rows(
        [_guild(1, "A", 10), _guild(2, "B", None), _guild(3, "C", 5)]
    )
    assert botstats.guild_totals(rows) == (3, 15, 1)


def test_count_channels_tolerates_a_guild_with_none():
    guilds = [
        _guild(1, channels=4),
        types.SimpleNamespace(id=2, name="B", member_count=1, me=None, channels=None),
    ]
    assert botstats.count_channels(botstats.collect_guild_rows(guilds)) == 4


def test_channels_are_counted_in_the_single_guild_pass():
    """Guild.channels rebuilds a list on every access, so the fleet is walked
    once and every page reads the count off the rows it already has."""
    accesses = []

    class _CountingGuild:
        id = 1
        name = "Alpha"
        member_count = 10
        me = None

        @property
        def channels(self):
            accesses.append(1)
            return [object()] * 7

    rows = botstats.collect_guild_rows([_CountingGuild()])
    assert botstats.count_channels(rows) == 7
    assert len(accesses) == 1


# ---------------------------------------------------------------------------
# /proc parsing (text fixtures)
# ---------------------------------------------------------------------------
# A real /proc/self/stat line, with the classic trap baked in: comm is
# "(py thon) weird)" - it holds a space AND parentheses, so a naive split()
# would read the wrong field entirely.
STAT_SIMPLE = (
    "1234 (python3.13) S 1 1234 1234 0 -1 4194304 100 0 0 0 "
    "10 5 0 0 20 0 12 0 987654 300000 5000 "
    + " ".join(str(n) for n in range(30))
)
STAT_TRICKY = (
    "1234 (py thon) weird) S 1 1234 1234 0 -1 4194304 100 0 0 0 "
    "10 5 0 0 20 0 12 0 987654 300000 5000 "
    + " ".join(str(n) for n in range(30))
)


def test_parse_proc_starttime_reads_field_22():
    assert botstats.parse_proc_starttime_ticks(STAT_SIMPLE) == 987654.0


def test_parse_proc_starttime_survives_a_comm_with_spaces_and_parens():
    """The scan starts after the LAST ')' precisely for this line."""
    assert botstats.parse_proc_starttime_ticks(STAT_TRICKY) == 987654.0


def test_parse_proc_starttime_returns_none_without_a_closing_paren():
    assert botstats.parse_proc_starttime_ticks("1234 python S 1 2 3") is None


def test_parse_proc_starttime_returns_none_on_a_truncated_line():
    assert botstats.parse_proc_starttime_ticks("1234 (python) S 1 2 3") is None


def test_parse_proc_starttime_returns_none_on_a_non_numeric_field():
    line = "1234 (python) " + " ".join(["x"] * 25)
    assert botstats.parse_proc_starttime_ticks(line) is None


def test_parse_proc_uptime_reads_the_first_field():
    assert botstats.parse_proc_uptime_seconds("123456.78 654321.00\n") == 123456.78


def test_parse_proc_uptime_returns_none_on_garbage():
    assert botstats.parse_proc_uptime_seconds("") is None
    assert botstats.parse_proc_uptime_seconds("nope nope") is None


STATUS_TEXT = (
    "Name:\tpython3.13\n"
    "State:\tS (sleeping)\n"
    "VmPeak:\t 1234567 kB\n"
    "VmRSS:\t  305176 kB\n"
    "Threads:\t12\n"
)


def test_parse_vm_rss_converts_kb_to_bytes():
    assert botstats.parse_vm_rss_bytes(STATUS_TEXT) == 305176 * 1024


def test_parse_vm_rss_is_not_fooled_by_vmpeak():
    """VmPeak precedes VmRSS and starts with the same three letters."""
    assert botstats.parse_vm_rss_bytes(STATUS_TEXT) != 1234567 * 1024


def test_parse_vm_rss_returns_none_when_absent_or_unparsable():
    assert botstats.parse_vm_rss_bytes("Name:\tpython\n") is None
    assert botstats.parse_vm_rss_bytes("VmRSS:\tlots kB\n") is None
    assert botstats.parse_vm_rss_bytes("VmRSS:\n") is None


def test_parse_vm_rss_rejects_an_unknown_unit_rather_than_assuming_kb():
    assert botstats.parse_vm_rss_bytes("VmRSS:\t 100 pages\n") is None


def test_process_uptime_subtracts_starttime_from_boot_uptime():
    # starttime 987654 ticks / 100 Hz = 9876.54s after boot; boot was 20000s ago.
    uptime = botstats.process_uptime_seconds(STAT_SIMPLE, "20000.00 0.0\n", 100)
    assert uptime == pytest.approx(20000.0 - 9876.54)


def test_process_uptime_is_none_when_either_read_is_unusable():
    assert botstats.process_uptime_seconds("garbage", "20000.0", 100) is None
    assert botstats.process_uptime_seconds(STAT_SIMPLE, "garbage", 100) is None


def test_process_uptime_is_none_on_a_bogus_clock_tick():
    assert botstats.process_uptime_seconds(STAT_SIMPLE, "20000.0", 0) is None
    assert botstats.process_uptime_seconds(STAT_SIMPLE, "20000.0", None) is None


def test_process_uptime_is_none_rather_than_negative():
    """A process that "started after boot ended" is a broken read, not an
    uptime of -3 seconds."""
    assert botstats.process_uptime_seconds(STAT_SIMPLE, "10.0 0.0", 100) is None


def test_read_helpers_degrade_to_none_without_proc(monkeypatch):
    monkeypatch.setattr(botstats, "_read_text", lambda path: None)
    assert botstats.read_process_uptime_seconds() is None
    assert botstats.read_rss_bytes() is None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def test_format_count_uses_ascii_thousands_separators():
    assert botstats.format_count(1234567) == "1,234,567"
    assert botstats.format_count(0) == "0"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (-5, "0s"),
        (45, "45s"),
        (90, "1m 30s"),
        (3600, "1h"),
        (3725, "1h 2m 5s"),
        (90000, "1d 1h"),
        (273120, "3d 3h 52m"),
    ],
)
def test_format_duration(seconds, expected):
    assert botstats.format_duration(seconds) == expected


def test_format_duration_drops_seconds_past_a_day():
    """Seconds only carry information under a day; past that they are noise."""
    assert botstats.format_duration(86400 + 61) == "1d 1m"


# ---------------------------------------------------------------------------
# SQL construction
# ---------------------------------------------------------------------------
def test_row_counts_sql_covers_every_featured_table_in_one_statement():
    sql = botstats.build_row_counts_sql(botstats.FEATURED_TABLES)
    for name in botstats.FEATURED_TABLES:
        assert "FROM {0}".format(name) in sql
    assert sql.count("UNION ALL") == len(botstats.FEATURED_TABLES) - 1
    assert ";" not in sql  # asyncpg prepares exactly one statement per call


def test_featured_tables_are_unique():
    assert len(set(botstats.FEATURED_TABLES)) == len(botstats.FEATURED_TABLES)


@pytest.mark.parametrize(
    "query", [botstats.OBSERVED_MESSAGES, botstats.OBSERVED_DAYS]
)
def test_observed_window_spans_exactly_the_days_it_advertises(query):
    """`day >= $1 - $2` selects $2 + 1 calendar days (today included), which
    would publish an 8-day sum under a heading that says 7. Same convention as
    serverstats' rollups.window_bounds: today - (days - 1)."""
    assert "$1::date - ($2::int - 1)" in query
    assert "$1::date - $2" not in query


@pytest.mark.parametrize(
    "query", [botstats.OBSERVED_MESSAGES, botstats.OBSERVED_DAYS]
)
def test_the_observed_window_ends_on_the_day_it_is_given(query):
    """CURRENT_DATE is the DATABASE SESSION's calendar day, so on a server whose
    TimeZone is not UTC it would put this block and the recorded-usage block on
    the SAME card on two different "today"s."""
    assert "CURRENT_DATE" not in query


def test_table_sizes_covers_every_relation_kind_that_holds_storage():
    """The page labels the sum "every table"; matviews and partitioned parents
    must not be silently dropped from it. TOAST ('t') stays out - it is already
    folded into its parent by pg_total_relation_size."""
    assert "c.relkind IN ('r', 'p', 'm')" in botstats.TABLE_SIZES
    assert "'t'" not in botstats.TABLE_SIZES


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def test_fetch_observed_activity_shapes_both_aggregates():
    def _row(query):
        if "server_stats_messages" in query:
            return {"messages": 1500, "guilds": 14, "days": 7}
        return {"joins": 30, "leaves": 12, "guilds": 181, "guild_days": 362}

    pool = _Pool(fetchrow_return=_row)
    day = datetime.date(2026, 7, 31)
    activity = await botstats.fetch_observed_activity(pool, days=7, today=day)
    assert activity == botstats.ObservedActivity(
        days=7,
        messages=1500,
        message_guilds=14,
        message_days=7,
        joins=30,
        leaves=12,
        day_guilds=181,
        guild_days=362,
    )
    # Both aggregates end on the SAME day, and it is the caller's day.
    assert [call[2] for call in pool.calls] == [(day, 7), (day, 7)]


async def test_fetch_table_sizes_reads_the_total_off_the_window_sum():
    pool = _Pool(
        fetch_return=[
            {"name": "avatar_history", "bytes": 900, "total_bytes": 1200},
            {"name": "levels", "bytes": 300, "total_bytes": 1200},
        ]
    )
    tables, total = await botstats.fetch_table_sizes(pool, limit=10)
    assert tables == [
        botstats.TableSize("avatar_history", 900),
        botstats.TableSize("levels", 300),
    ]
    assert total == 1200


async def test_fetch_table_sizes_on_an_empty_database():
    tables, total = await botstats.fetch_table_sizes(_Pool(fetch_return=[]))
    assert tables == []
    assert total == 0


async def test_fetch_row_counts_returns_name_count_pairs():
    pool = _Pool(fetch_return=[{"name": "levels", "rows": 10}])
    assert await botstats.fetch_row_counts(pool) == [("levels", 10)]


# ---------------------------------------------------------------------------
# Rendering: the honesty rules
# ---------------------------------------------------------------------------
def _flat(sections):
    return "\n".join(
        heading + "\n" + "\n".join(lines) for heading, lines in sections
    )


def test_observed_activity_with_no_observed_guild_never_prints_a_zero():
    """The SUM is a structural 0 when nothing was watched. Publishing it would
    claim a silence nobody measured."""
    activity = botstats.ObservedActivity(
        days=7,
        messages=0,
        message_guilds=0,
        message_days=0,
        joins=0,
        leaves=0,
        day_guilds=0,
        guild_days=0,
    )
    lines = botstats.render_observed_activity(activity)
    text = "\n".join(lines)
    assert "0 messages" not in text
    assert "0 joins" not in text
    assert "not a zero" in text
    assert "was not being" in text  # the standing footnote


def test_observed_activity_states_its_coverage_alongside_every_sum():
    activity = botstats.ObservedActivity(
        days=7,
        messages=1500,
        message_guilds=14,
        message_days=7,
        joins=30,
        leaves=12,
        day_guilds=181,
        guild_days=362,
    )
    text = "\n".join(botstats.render_observed_activity(activity))
    assert "1,500 messages" in text
    assert "14 observed server(s)" in text
    assert "181 observed server(s)" in text
    assert "362 observed server-day(s)" in text


def test_observed_activity_unavailable_is_said_not_zeroed():
    text = "\n".join(botstats.render_observed_activity(None))
    assert "unavailable" in text
    assert "0" not in text


def test_render_usage_headline_and_ranking():
    counters = botstats.UsageCounters(started_at=NOW - datetime.timedelta(hours=2))
    counters.record("play")
    counters.record("play")
    counters.record("rank", slash=True)
    text = _flat(botstats.render_usage(counters, None, now=NOW))
    assert "3 commands run since boot (2h ago)" in text
    assert "2 prefix - 1 slash" in text
    assert "`play` - 2" in text
    assert "reset on every restart" in text


def test_render_usage_with_no_command_yet():
    counters = botstats.UsageCounters(started_at=NOW)
    text = _flat(botstats.render_usage(counters, None, now=NOW))
    assert "No command has completed yet." in text


def test_render_usage_caps_the_ranking():
    counters = botstats.UsageCounters(started_at=NOW)
    for index in range(30):
        for _unused in range(30 - index):
            counters.record("cmd{0:02d}".format(index))
    sections = botstats.render_usage(counters, None, now=NOW)
    _heading, ranking = sections[1]
    assert len(ranking) == botstats.TOP_COMMANDS_LIMIT
    assert ranking[0].endswith("`cmd00` - 30")


# ---------------------------------------------------------------------------
# Rendering: overview / top servers / data
# ---------------------------------------------------------------------------
def _overview(**overrides):
    kwargs = dict(
        guilds=181,
        members=250000,
        guilds_without_count=0,
        cached_users=4321,
        channels=5678,
        latency_seconds=0.042,
        uptime_seconds=273120,
        uptime_is_process=True,
        rss_bytes=305176 * 1024,
        shards=1,
        python_version="3.13.3",
        discord_version="2.7.1",
        db_bytes=138089119,
    )
    kwargs.update(overrides)
    return _flat(botstats.render_overview(**kwargs))


def test_render_overview_renders_every_metric():
    text = _overview()
    assert "Servers: 181" in text
    assert "Members: 250,000 (approximate, bots included)" in text
    assert "Cached users: 4,321" in text
    assert "Channels: 5,678" in text
    assert "Uptime: 3d 3h 52m" in text
    assert "Memory (RSS): 298.0 MiB" in text
    assert "Gateway latency: 42 ms" in text
    assert "Python 3.13.3 - discord.py 2.7.1" in text
    assert "131.7 MiB" in text


def test_render_overview_labels_member_counts_as_gateway_sourced():
    assert "never chunks one" in _overview()


def test_render_overview_says_unknown_rather_than_zero_or_nan():
    text = _overview(
        latency_seconds=None,
        uptime_seconds=None,
        uptime_is_process=False,
        rss_bytes=None,
        db_bytes=None,
    )
    assert "Uptime: unknown" in text
    assert "Memory (RSS): unknown" in text
    assert "Gateway latency: unknown" in text
    assert "Database size: unknown" in text
    assert "nan" not in text


def test_render_overview_labels_the_cog_load_fallback():
    text = _overview(uptime_seconds=600, uptime_is_process=False)
    assert "10m (since this cog loaded)" in text


def test_render_overview_reports_guilds_without_a_member_count():
    text = _overview(guilds_without_count=3)
    assert "3 server(s) report no member count and are excluded." in text


def test_render_top_guilds_lists_rank_name_members_and_join_date():
    rows = botstats.top_guild_rows(
        botstats.collect_guild_rows([_guild(11, "Alpha", 9000, NOW)])
    )
    text = _flat(
        botstats.render_top_guilds(
            rows, guilds=1, members=9000, guilds_without_count=0
        )
    )
    assert "**Alpha** - 9,000 members" in text
    assert "`11`" in text
    assert "<t:{0}:d>".format(int(NOW.timestamp())) in text
    assert "1 servers, 9,000 members total" in text


def test_render_top_guilds_shows_a_question_mark_for_an_unknown_count():
    rows = botstats.top_guild_rows(
        botstats.collect_guild_rows([_guild(11, "Alpha", None, None)])
    )
    text = _flat(
        botstats.render_top_guilds(rows, guilds=1, members=0, guilds_without_count=1)
    )
    assert "- ? members" in text
    assert "joined unknown" in text
    assert "1 server(s) report no member count" in text


def test_render_top_guilds_escapes_and_truncates_a_hostile_name():
    long_name = "**bold**" + "x" * 200
    rows = botstats.top_guild_rows(
        botstats.collect_guild_rows([_guild(11, long_name, 5, NOW)])
    )
    text = _flat(
        botstats.render_top_guilds(rows, guilds=1, members=5, guilds_without_count=0)
    )
    assert "\\*\\*bold\\*\\*" in text
    assert "x" * 100 not in text


def test_top_guilds_page_stays_inside_the_components_v2_text_budget():
    """Components V2 caps a message's TOTAL text at 4000 characters. The worst
    case is a full ranking of maximum-length all-special names, every one of
    which escape_markdown doubles - bumping TOP_GUILDS_LIMIT or
    GUILD_NAME_LIMIT past that budget would make Discord 400 the send with no
    local signal at all."""
    hostile = "*" * botstats.GUILD_NAME_LIMIT
    rows = botstats.top_guild_rows(
        botstats.collect_guild_rows(
            [
                _guild(10 ** 17 + i, hostile, 999999999, NOW)
                for i in range(botstats.TOP_GUILDS_LIMIT * 2)
            ]
        )
    )
    sections = botstats.render_top_guilds(
        rows, guilds=99999, members=999999999, guilds_without_count=99
    )
    assert len(_flat(sections)) < 4000


def test_render_top_guilds_on_an_empty_fleet():
    text = _flat(
        botstats.render_top_guilds([], guilds=0, members=0, guilds_without_count=0)
    )
    assert "This bot is in no server." in text


def test_render_data_lists_counts_sizes_and_the_total():
    text = _flat(
        botstats.render_data(
            [("levels", 1234), ("timers", 7)],
            [botstats.TableSize("avatar_history", 93552640)],
            128466944,
        )
    )
    assert "`levels` - 1,234" in text
    assert "`avatar_history` - 89.2 MiB" in text
    assert "Every table: 122.5 MiB" in text


def test_render_data_says_unavailable_rather_than_showing_an_empty_table():
    text = _flat(botstats.render_data(None, None, 0))
    assert "Row counts are unavailable right now." in text
    assert "Table sizes are unavailable right now." in text


# ---------------------------------------------------------------------------
# The dashboard card
# ---------------------------------------------------------------------------
def _bot(pool, guilds=None):
    return types.SimpleNamespace(
        guilds=guilds if guilds is not None else [_guild(1, "Alpha", 100)],
        users=[object(), object()],
        latency=0.042,
        shard_count=None,
        db_pool=pool,
    )


def _card(pool, guilds=None):
    cog = _cog(_bot(pool, guilds))
    return botstats.BotStatsDashboard(cog, author_id=1)


async def test_overview_page_issues_only_the_database_size_read():
    pool = _Pool(fetchval_return=1000)
    card = _card(pool)
    await card.start()
    assert [call[0] for call in pool.calls] == ["fetchval"]
    assert pool.calls[0][1] == botstats.DB_SIZE


async def test_top_guilds_page_issues_no_query_at_all():
    pool = _Pool()
    card = _card(pool, guilds=[_guild(1, "Alpha", 100), _guild(2, "Beta", 900)])
    card.index = botstats.PAGE_GUILDS
    sections, _taken = await card._load(botstats.PAGE_GUILDS)
    assert pool.calls == []
    assert "Beta" in _flat(sections)


def _usage_row(query):
    """fetchrow double for the Usage page's three reads (BS1 + BS2)."""
    if "server_stats_messages" in query:
        return {"messages": 5, "guilds": 1, "days": 1}
    if "command_usage" in query:
        return {
            "today": 3,
            "week": 9,
            "month": 20,
            "week_recorded": 7,
            "month_recorded": 30,
            "since": datetime.date(2026, 1, 1),
        }
    return {"joins": 1, "leaves": 0, "guilds": 1, "guild_days": 1}


def _usage_pool():
    return _Pool(
        fetchrow_return=_usage_row,
        fetch_return=lambda query: [{"command": "play", "total": 9}],
    )


async def test_usage_page_issues_the_activity_and_recorded_usage_reads():
    """Two aggregates for the observed-activity block, then the two persisted
    usage reads (windows + ranking) behind a single memo key."""
    pool = _usage_pool()
    card = _card(pool)
    await card._load(botstats.PAGE_USAGE)
    assert [call[0] for call in pool.calls] == ["fetchrow", "fetchrow", "fetchrow", "fetch"]


async def test_every_window_on_the_usage_page_ends_on_the_same_utc_day(monkeypatch):
    """Observed activity and recorded usage are windows on ONE card, so they
    must not be able to end on different days - which is what a click at the UTC
    midnight boundary, or a DB session whose TimeZone is not UTC, would do if
    either side computed its own "today".

    The clock below advances on every call, so the page passes only if it reads
    "today" ONCE and hands the same day to both reads."""
    clock = iter(
        [datetime.date(2026, 7, 31) + datetime.timedelta(days=n) for n in range(9)]
    )
    monkeypatch.setattr(usage_stats, "utc_today", lambda: next(clock))
    pool = _usage_pool()
    card = _card(pool)
    await card._load(botstats.PAGE_USAGE)
    days = {call[2][0] for call in pool.calls}
    assert days == {datetime.date(2026, 7, 31)}


async def test_data_page_issues_exactly_the_two_data_reads():
    pool = _Pool(
        fetch_return=lambda query: (
            [{"name": "levels", "rows": 3}]
            if "COUNT(*)" in query
            else [{"name": "levels", "bytes": 10, "total_bytes": 10}]
        )
    )
    card = _card(pool)
    await card._load(botstats.PAGE_DATA)
    assert [call[0] for call in pool.calls] == ["fetch", "fetch"]


async def test_a_page_is_queried_once_then_served_from_its_memo():
    pool = _Pool(fetchval_return=1000)
    card = _card(pool)
    await card._load(botstats.PAGE_OVERVIEW)
    await card._load(botstats.PAGE_OVERVIEW)
    await card._load(botstats.PAGE_OVERVIEW)
    assert len(pool.calls) == 1


async def test_a_failing_read_degrades_the_page_not_the_card():
    pool = _Pool()
    pool.fail = True
    card = _card(pool)
    sections, _taken = await card._load(botstats.PAGE_DATA)
    text = _flat(sections)
    assert "Row counts are unavailable right now." in text
    assert "Table sizes are unavailable right now." in text


async def test_a_failed_read_is_never_memoised_so_the_next_open_retries():
    """Caching a failure alongside a success would freeze a transient pool
    error into the card for its whole 5 minute life, with no way back."""
    pool = _Pool(
        fetch_return=lambda query: (
            [{"name": "levels", "rows": 3}]
            if "COUNT(*)" in query
            else [{"name": "levels", "bytes": 10, "total_bytes": 10}]
        )
    )
    pool.fail = True
    card = _card(pool)
    sections, _taken = await card._load(botstats.PAGE_DATA)
    assert "Row counts are unavailable right now." in _flat(sections)

    pool.fail = False
    sections, _taken = await card._load(botstats.PAGE_DATA)
    text = _flat(sections)
    assert "`levels` - 3" in text
    assert "unavailable" not in text


async def test_usage_counters_are_live_on_every_open_but_the_db_is_read_once():
    """The counters cost nothing to re-read; freezing them would make the page
    lie about a live process."""
    pool = _usage_pool()
    card = _card(pool)
    card.cog.usage.record("play")
    sections, _taken = await card._load(botstats.PAGE_USAGE)
    assert "1 commands run since boot" in _flat(sections)

    for _unused in range(4):
        card.cog.usage.record("play")
    sections, _taken = await card._load(botstats.PAGE_USAGE)
    assert "5 commands run since boot" in _flat(sections)
    # ... while the DB half stayed at its one memoised set of reads.
    assert [call[0] for call in pool.calls] == [
        "fetchrow",
        "fetchrow",
        "fetchrow",
        "fetch",
    ]


async def test_the_footer_dates_the_page_against_the_memoised_read():
    """A figure fetched minutes ago must not be footnoted with the current
    time just because the page re-rendered its in-memory half."""
    pool = _Pool(fetchval_return=1000)
    card = _card(pool)
    _sections, first = await card._load(botstats.PAGE_OVERVIEW)
    _sections, second = await card._load(botstats.PAGE_OVERVIEW)
    assert second == first
    # A page with no DB read behind it is always live, so it dates itself now.
    _sections, guilds_taken = await card._load(botstats.PAGE_GUILDS)
    assert guilds_taken >= first


async def test_show_page_acks_the_click_before_it_ever_reads(make_interaction):
    """Discord kills the token 3s after the click while QUERY_TIMEOUT allows
    15s of reads: without the ACK first, a slow page answers a dead token and
    the owner is left on "This interaction failed" with nothing rendered."""
    pool = _Pool(fetchval_return=1000)
    card = _card(pool)
    await card.start()
    interaction = make_interaction(user_id=1)
    card.message = interaction.message

    deferred_before_load = []
    original = card._load

    async def _spy(index):
        deferred_before_load.append(bool(interaction.defers))
        return await original(index)

    card._load = _spy
    await card.show_page(interaction, botstats.PAGE_GUILDS)
    assert deferred_before_load == [True]


async def test_show_page_switches_page_and_edits_the_card(make_interaction):
    pool = _Pool(fetchval_return=1000)
    card = _card(pool, guilds=[_guild(1, "Alpha", 100)])
    await card.start()
    interaction = make_interaction(user_id=1)
    card.message = interaction.message
    await card.show_page(interaction, botstats.PAGE_GUILDS)
    assert card.index == botstats.PAGE_GUILDS
    # Deferred first, so the refresh lands through the message edit.
    assert len(interaction.message_edits) == 1
    assert interaction.message_edits[0][1]["view"] is card


async def test_show_page_suppresses_mentions_on_the_edit(make_interaction):
    """The layout carries attacker-chosen guild names, and an edit that says
    nothing inherits the client default (users=True) and re-parses them."""
    pool = _Pool(fetchval_return=1000)
    card = _card(pool)
    await card.start()
    interaction = make_interaction(user_id=1)
    card.message = interaction.message
    await card.show_page(interaction, botstats.PAGE_GUILDS)
    mentions = interaction.message_edits[0][1]["allowed_mentions"]
    assert (mentions.users, mentions.roles, mentions.everyone) == (False, False, False)


async def test_show_page_falls_back_to_the_interaction_message(make_interaction):
    """view.message is assigned just after ctx.send returns; a click landing in
    that sliver must still refresh the card it was attached to."""
    pool = _Pool(fetchval_return=1000)
    card = _card(pool)
    await card.start()
    assert card.message is None
    interaction = make_interaction(user_id=1)
    await card.show_page(interaction, botstats.PAGE_GUILDS)
    assert len(interaction.message_edits) == 1


async def test_show_page_answers_the_clicker_when_a_page_blows_up(make_interaction):
    pool = _Pool(fetchval_return=1000)
    card = _card(pool)
    await card.start()

    async def _boom(index):
        raise RuntimeError("boom")

    card._load = _boom
    interaction = make_interaction(user_id=1)
    card.message = interaction.message
    await card.show_page(interaction, botstats.PAGE_USAGE)
    # Deferred, so the apology lands as a followup - never Discord's own error.
    assert interaction.followups, "the click must never be left on Discord's error"
    # The cursor must not move to a page that never rendered.
    assert card.index == botstats.PAGE_OVERVIEW
    assert interaction.edits == []
    assert interaction.message_edits == []


async def test_two_racing_clicks_leave_the_card_on_the_last_one(make_interaction):
    """Two page buttons clicked inside one load window run two callback tasks
    over the SAME view. Serialising them is what keeps self.index and the last
    edit on the wire in agreement with the last page actually clicked - without
    it the slow first click overwrites the fast second one."""
    pool = _Pool(
        fetchrow_return=lambda query: (
            {"messages": 5, "guilds": 1, "days": 1}
            if "server_stats_messages" in query
            else {"joins": 1, "leaves": 0, "guilds": 1, "guild_days": 1}
        ),
        fetch_return=lambda query: (
            [{"name": "levels", "rows": 3}]
            if "COUNT(*)" in query
            else [{"name": "levels", "bytes": 10, "total_bytes": 10}]
        ),
    )
    card = _card(pool)
    await card.start()
    interaction = make_interaction(user_id=1)
    card.message = interaction.message

    original = card._load

    async def _slow_usage(index):
        if index == botstats.PAGE_USAGE:
            await asyncio.sleep(0.02)
        return await original(index)

    card._load = _slow_usage
    first = asyncio.create_task(card.show_page(interaction, botstats.PAGE_USAGE))
    await asyncio.sleep(0)  # let the first click reach its load
    second = asyncio.create_task(card.show_page(interaction, botstats.PAGE_DATA))
    await asyncio.gather(first, second)
    assert card.index == botstats.PAGE_DATA
    assert len(interaction.message_edits) == 2


async def test_card_is_author_gated(make_interaction):
    pool = _Pool(fetchval_return=1000)
    card = _card(pool)
    intruder = make_interaction(user_id=999)
    assert await card.interaction_check(intruder) is False
    assert intruder.sent


def test_page_labels_cover_every_page_emoji():
    assert len(botstats.page_labels()) == len(botstats.PAGE_EMOJI) == 4


# ---------------------------------------------------------------------------
# The command surface
# ---------------------------------------------------------------------------
def test_botstats_is_a_hidden_prefix_only_command_with_its_alias():
    command = botstats.BotStats.botstats
    assert command.hidden is True
    assert command.aliases == ["bstats"]
    # Not a hybrid: this owner surface must never appear in the slash picker.
    assert not hasattr(command, "app_command")


async def test_cog_check_delegates_to_bot_is_owner():
    async def _is_owner(user):
        return user.id == 42

    cog = _cog(types.SimpleNamespace(is_owner=_is_owner))
    owner = types.SimpleNamespace(author=types.SimpleNamespace(id=42))
    other = types.SimpleNamespace(author=types.SimpleNamespace(id=7))
    assert await cog.cog_check(owner) is True
    assert await cog.cog_check(other) is False

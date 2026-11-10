"""B3: the Growth and Summary pages of ?botstats.

Pure render / SQL-shape tests for tools/botstats_growth.py and
tools/botstats_summary.py, the join / leave writers in cogs/system/events.py and
the card wiring in cogs/system/botstats.py. The SQL itself is exercised against a
real PostgreSQL in the lot's probe, not here.
"""

import datetime
import types

from cogs.system import botstats, events
from tools import botstats_growth as bg
from tools import botstats_health as bh
from tools import botstats_summary as bs

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 4, 12, 0, tzinfo=UTC)  # a Wednesday


def _flat(sections):
    return "\n".join(h + "\n" + "\n".join(lines) for h, lines in sections)


def _dur(seconds):
    return "{}s".format(int(seconds))


def _days_ago(days):
    return NOW - datetime.timedelta(days=days)


def _stats(**overrides):
    base = dict(
        as_of=NOW,
        since=_days_ago(120),
        windows={7: (2, 1), 30: (6, 3), 90: (10, 8)},
        weekly={datetime.date(2026, 11, 2): (1, 0), datetime.date(2026, 10, 26): (2, 1)},
        stay_known=4,
        early=(1, 2),
        median_stay=7200.0,
        join_members=120.0,
        leave_members=None,
    )
    base.update(overrides)
    return bg.GrowthStats(**base)


def _guild(joined_at, members=50, has_me=True):
    me = types.SimpleNamespace(joined_at=joined_at) if has_me else None
    return types.SimpleNamespace(me=me, member_count=members)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
class _Pool:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    async def execute(self, query, *args):
        if self.fail:
            raise RuntimeError("boom")
        self.rows.append(args)


async def test_join_stores_kind_and_member_count_only():
    pool = _Pool()
    await bg.record_guild_event(pool, _guild(_days_ago(1), members=80), "join", now=NOW)
    assert pool.rows == [("join", 80, None)]


async def test_leave_stores_the_stay_from_the_bot_member():
    pool = _Pool()
    await bg.record_guild_event(pool, _guild(_days_ago(3)), "leave", now=NOW)
    assert pool.rows == [("leave", 50, 3 * 86400)]


async def test_unknown_values_are_null_never_zero():
    pool = _Pool()
    await bg.record_guild_event(pool, _guild(None, members=None), "leave", now=NOW)
    await bg.record_guild_event(pool, _guild(None, members=0, has_me=False), "leave", now=NOW)
    assert pool.rows == [("leave", None, None), ("leave", None, None)]


async def test_a_clock_skewed_join_date_never_stores_a_negative_stay():
    pool = _Pool()
    await bg.record_guild_event(pool, _guild(NOW + datetime.timedelta(hours=1)), "leave", now=NOW)
    assert pool.rows[0][2] == 0


async def test_a_failed_write_is_swallowed():
    await bg.record_guild_event(_Pool(fail=True), _guild(_days_ago(1)), "join", now=NOW)


async def test_the_events_cog_logs_a_join_and_a_leave(monkeypatch):
    async def cancel(_pool, guild_id, **_kwargs):
        raise RuntimeError("retention outage")

    monkeypatch.setattr(events.retention, "cancel_guild_purge", cancel)
    monkeypatch.setattr(events.retention, "schedule_guild_purge", cancel)
    monkeypatch.setattr(events.retention, "invalidate_guild_caches", lambda *_: None)
    pool = _Pool()
    cog = object.__new__(events.Events)
    cog.bot = types.SimpleNamespace(db_pool=pool)
    guild = types.SimpleNamespace(id=7, member_count=10, me=None, unavailable=False)
    await cog.on_guild_join(guild)  # the restore failed, the statistic still lands
    await cog.on_guild_remove(guild)
    assert pool.rows == [("join", 10, None), ("leave", 10, None)]


def test_the_guild_event_row_has_no_identifier_column():
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "..", "schema.sql")
    with open(path, encoding="utf-8") as fh:
        schema = fh.read()
    body = schema.split("CREATE TABLE IF NOT EXISTS guild_events (")[1].split(");")[0]
    assert "guild_id" not in body and "user_id" not in body and "name" not in body
    assert "date_trunc('hour', now())" in bg.INSERT  # the minute is never stored


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_week_starts_are_the_last_eight_mondays_oldest_first():
    weeks = bg.week_starts(NOW)
    assert len(weeks) == 8 and weeks[-1] == datetime.date(2026, 11, 2)
    assert all(week.weekday() == 0 for week in weeks)
    assert weeks[0] == datetime.date(2026, 9, 14)


def test_prune_cutoff_is_midnight_utc_400_days_back():
    cutoff = bg.prune_cutoff(datetime.date(2026, 11, 4))
    assert cutoff == datetime.datetime(2025, 9, 30, tzinfo=UTC)


def test_join_age_counts_buckets_and_unknowns():
    guilds = [
        _guild(_days_ago(1)),
        _guild(_days_ago(7)),
        _guild(_days_ago(20)),
        _guild(_days_ago(60)),
        _guild(_days_ago(200)),
        _guild(None),
        _guild(None, has_me=False),
    ]
    assert bg.join_age_counts(guilds, NOW) == (2, 1, 1, 1, 2)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_windows_show_net_and_flag_a_window_older_than_the_history():
    text = _flat(bg.render_growth(_stats(since=_days_ago(40)), (0,) * 5, fmt_duration=_dur))
    assert "7 days: 2 joined, 1 left, net +1" in text
    assert "90 days: 10 joined, 8 left, net +2 - history starts 2026-09-25" in text
    assert "30 days: 6 joined, 3 left, net +3\n" in text  # covered: no caveat


def test_an_empty_log_is_not_presented_as_zero_leaves():
    text = _flat(bg.render_growth(_stats(since=None), (3, 0, 0, 0, 0), fmt_duration=_dur))
    assert "No server join or leave has been recorded yet." in text
    assert "0 left" not in text
    assert "Joined in the last 7 days: 3" in text  # the in-memory half still works


def test_a_failed_read_is_unavailable_but_the_free_half_stays():
    text = _flat(bg.render_growth(None, (1, 0, 0, 0, 0), fmt_duration=_dur))
    assert "unavailable" in text and "Joined in the last 7 days: 1" in text


def test_weeks_before_the_history_are_absent_not_zero():
    stats = _stats(since=datetime.datetime(2026, 10, 28, tzinfo=UTC))
    lines = bg.render_weeks(stats)
    weeks = [line for line in lines if line.startswith("Week of")]
    assert weeks[0].startswith("Week of 2026-10-26") and len(weeks) == 2
    assert "Week of 2026-11-02: 1 joined, 0 left" in weeks[1]


def test_a_week_inside_the_history_without_events_is_a_real_zero():
    stats = _stats(weekly={})
    assert "Week of 2026-10-19: 0 joined, 0 left" in bg.render_weeks(stats)


def test_stay_block_reports_median_early_churn_and_member_medians():
    text = _flat(bg.render_growth(_stats(), (0,) * 5, fmt_duration=_dur))
    assert "Median stay of servers that left: 7200s" in text
    assert "Left within 24 h: 1 of 4 (25%)" in text
    assert "Left within 7 days: 2 of 4 (50%)" in text
    assert "Median size: 120 members when joining, ? when leaving" in text


def test_short_history_warns_that_long_stays_cannot_show_up():
    text = _flat(bg.render_growth(_stats(since=_days_ago(10)), (0,) * 5, fmt_duration=_dur))
    assert "Small or short sample: only 11 day(s) of history" in text


def test_no_known_stay_says_so():
    text = _flat(bg.render_growth(_stats(stay_known=0, median_stay=None), (0,) * 5, fmt_duration=_dur))
    assert "has a known stay" in text and "Median stay" not in text


async def test_fetch_growth_shapes_rows_and_uses_the_pinned_now():
    seen = {}

    class Pool:
        async def fetchrow(self, query, *args, **kwargs):
            seen["summary"] = args
            return {
                "joins_7": 1, "leaves_7": 0, "joins_30": 2, "leaves_30": 1,
                "joins_90": 3, "leaves_90": 2, "stay_known": 1, "early_1": 1,
                "early_2": 1, "median_stay": 60.0, "join_members": None,
                "leave_members": 9.0, "since": _days_ago(5),
            }

        async def fetch(self, query, *args, **kwargs):
            seen["weekly"] = args
            return [{"week": datetime.date(2026, 11, 2), "joins": 1, "leaves": 0}]

    stats = await bg.fetch_growth(Pool(), timeout=1, now=NOW)
    assert seen["summary"] == (NOW, 86400, 7 * 86400)
    assert seen["weekly"] == (datetime.datetime(2026, 9, 14, tzinfo=UTC),)
    assert stats.windows[30] == (2, 1) and stats.weekly[datetime.date(2026, 11, 2)] == (1, 0)
    assert stats.covered_days == 6 and not stats.covers(30) and stats.covers(5)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def _health(**overrides):
    base = dict(
        errors=bh.ErrorStats(NOW.date(), 0, 0, NOW.date(), ()),
        chapters_embargo_seconds=0.0, latency=0.05, gateway=(1, 2),
        nodes=[("main", "connected")],
        players=0,
        breaker=bh.BreakerState("closed", 0.0, 0, 0),
        timers=bh.TimerStats(0, None, (), 0),
        actions=bh.ActionStats({}, (), 0),
        pool=bh.PoolState(1, 1, 10),
        failed_extensions=(),
        loaded_extensions=10,
    )
    base.update(overrides)
    return bh.HealthSnapshot(**base)


def _persisted(**overrides):
    base = dict(
        as_of=NOW.date(), today=5, week=50, month=90, week_days=7, month_days=30,
        week_recorded=7, month_recorded=30, since=NOW.date() - datetime.timedelta(days=40),
        top=(("play", 1234),), covered_days=41,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_all_unknown_renders_one_not_enough_data_line():
    assert bs.build_insights() == ["Not enough data yet to say anything useful."]
    sections = bs.render_summary()
    assert sections[0][1] == ["Not enough data yet to say anything useful."]


def test_adoption_names_the_top_feature_and_the_under_5_percent_ones():
    counts = {"leveling": 42, "tickets": 1, "welcome": 20, "starboard": 0}
    (line,) = bs.build_insights(adoption=counts, guild_total=100)
    assert line.startswith("Leveling enabled is the most used feature (42% of servers)")
    assert "2 feature(s) are under 5%: Starboard, Tickets." in line


def test_adoption_without_a_low_feature_and_without_guilds():
    (line,) = bs.build_insights(adoption={"leveling": 9}, guild_total=10)
    assert line.endswith("no feature is under 5%.")
    assert bs.build_insights(adoption={"leveling": 9}, guild_total=0) == [
        "Not enough data yet to say anything useful."
    ]


def test_upsell_picks_the_most_pressed_limit_and_says_when_none_is():
    wall = {"guild_playlists": {"at_80": 5, "at_100": 2, "scopes": 20}}
    (line,) = bs.build_insights(wall=wall)
    assert line.startswith("Best upsell argument:") and "2 at the free limit" in line
    (none,) = bs.build_insights(wall={})
    assert "no upsell argument yet" in none


def test_health_gives_the_verdict_and_the_top_error_only_when_failing_today():
    green = bs.build_insights(health=_health())
    assert green == ["Health: All green"]
    errors = bh.ErrorStats(NOW.date(), 3, 9, NOW.date(), (("play", "KeyError", 7),))
    lines = bs.build_insights(health=_health(errors=errors))
    assert lines[0].startswith("Health: 1 issue(s): 3 error(s) today")
    assert lines[1] == "Most frequent error this week: play / KeyError (7)."
    quiet = bh.ErrorStats(NOW.date(), 0, 9, NOW.date(), (("play", "KeyError", 7),))
    assert len(bs.build_insights(health=_health(errors=quiet))) == 1


def test_growth_needs_thirty_days_before_it_calls_a_trend():
    (young,) = bs.build_insights(growth=_stats(since=_days_ago(9)))
    assert young == "Growth: not enough history yet (10 day(s) recorded)."
    (empty,) = bs.build_insights(growth=_stats(since=None))
    assert "(0 day(s) recorded)" in empty
    (full,) = bs.build_insights(growth=_stats())
    assert full == (
        "Over 30 days the bot gained 6 and lost 3 server(s), net +3; "
        "50% of the 4 servers that left in the last 90 day(s) stayed under 7 days."
    )
    (no_stay,) = bs.build_insights(growth=_stats(stay_known=0))
    assert no_stay.endswith("net +3.")


def test_usage_reports_the_top_command_and_the_hour_extremes():
    lines = bs.build_insights(
        persisted=_persisted(), busiest="Mon 14:00 (3.0%)", quietest="Tue 04:00 (0.1%)"
    )
    assert lines == [
        "Most used command over 7 day(s): play (1,234).",
        "Busiest hour (UTC): Mon 14:00 (3.0%); quietest: Tue 04:00 (0.1%).",
    ]
    only = bs.build_insights(persisted=_persisted(top=()), busiest="Mon 14:00 (3.0%)")
    assert only == ["Busiest hour (UTC): Mon 14:00 (3.0%)."]
    assert bs.build_insights(persisted=_persisted(since=None)) == [
        "Not enough data yet to say anything useful."
    ]


def test_full_data_stays_within_seven_lines():
    errors = bh.ErrorStats(NOW.date(), 3, 9, NOW.date(), (("play", "KeyError", 7),))
    lines = bs.build_insights(
        adoption={"leveling": 5, "tickets": 0},
        guild_total=10,
        wall={"guild_playlists": {"at_80": 5, "at_100": 2, "scopes": 20}},
        health=_health(errors=errors),
        growth=_stats(),
        persisted=_persisted(),
        busiest="a",
        quietest="b",
    )
    assert 3 <= len(lines) <= 7 and len(lines) == 7
    assert all(ch not in line for line in lines for ch in (chr(0x2013), chr(0x2014), chr(0x2026)))


def test_peak_hours_refuses_a_young_profile():
    persisted = types.SimpleNamespace(hourly=(), hourly_covered_days=1)
    assert botstats.peak_hours(persisted) == (None, None)


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
        if query is bg.SUMMARY:
            return {
                "joins_7": 1, "leaves_7": 0, "joins_30": 1, "leaves_30": 0,
                "joins_90": 1, "leaves_90": 0, "stay_known": 0, "early_1": 0,
                "early_2": 0, "median_stay": None, "join_members": 10.0,
                "leave_members": None, "since": datetime.datetime.now(UTC),
            }
        return {"today": 0, "week": 0, "month": 0, "week_recorded": 0,
                "month_recorded": 0, "started_on": None, "since": None, "scopes": 0, "median": None,
                "p90": None, "p99": None, "max": None, "at_80": 0, "at_100": 0}

    async def fetch(self, query, *args, **kwargs):
        self._go(query)
        return []

    def get_size(self):
        return 1

    def get_idle_size(self):
        return 1

    def get_max_size(self):
        return 10


def _card(pool):
    bot = types.SimpleNamespace(
        guilds=[_guild(datetime.datetime.now(UTC)) for _n in range(2)],
        users=[], latency=0.1, shard_count=None, db_pool=pool, voice_clients=[],
        extensions={}, failed_extensions=(), premium=None,
    )
    for guild in bot.guilds:
        guild.id = 1
    return botstats.BotStatsDashboard(botstats.BotStats(bot), author_id=1)


async def test_growth_page_reads_once_and_retries_a_failure():
    pool = _CardPool()
    card = _card(pool)
    sections, _t = await card._load(botstats.PAGE_GROWTH)
    assert "Joined in the last 7 days: 2" in _flat(sections)
    await card._load(botstats.PAGE_GROWTH)
    assert pool.calls.count(bg.SUMMARY) == 1  # memoised

    failing = _CardPool(fail=True)
    card = _card(failing)
    sections, _t = await card._load(botstats.PAGE_GROWTH)
    assert "unavailable" in _flat(sections) and "Joined in the last 7 days: 2" in _flat(sections)
    await card._load(botstats.PAGE_GROWTH)
    assert failing.calls.count(bg.SUMMARY) == 2  # a failure is not memoised


async def test_summary_reuses_the_other_pages_reads_and_never_writes():
    pool = _CardPool()
    card = _card(pool)
    await card._load(botstats.PAGE_GROWTH)
    await card._load(botstats.PAGE_FEATURES)
    before = len(pool.calls)
    sections, taken = await card._load(botstats.PAGE_SUMMARY)
    assert pool.calls.count(bg.SUMMARY) == 1
    assert "Over 30 days" not in _flat(sections) and "Growth: not enough history" in _flat(sections)
    assert len(pool.calls) > before  # the reads nobody opened yet ran once
    again = len(pool.calls)
    await card._load(botstats.PAGE_SUMMARY)
    assert len(pool.calls) == again
    assert not any("INSERT" in str(q) or "DELETE" in str(q) for q in pool.calls)

    card.index = botstats.PAGE_SUMMARY
    card._rerender(sections, taken)
    rows = [
        c for c in card.children[0].children
        if hasattr(c, "children")
        and any(isinstance(i, botstats._PageButton) for i in c.children)
    ]
    assert [len(r.children) for r in rows] == [3, 3, 3]


async def test_summary_with_every_read_failing_degrades_to_what_is_known():
    card = _card(_CardPool(fail=True))
    sections, _t = await card._load(botstats.PAGE_SUMMARY)
    text = _flat(sections)
    assert text.count("Health:") == 1  # the live half is still known
    assert "Growth" not in text


def test_pages_are_appended_in_order():
    assert (botstats.PAGE_GROWTH, botstats.PAGE_SUMMARY) == (7, 8)
    assert set(botstats.BotStatsDashboard.PAGE_READS) == set(range(9))
    assert botstats.page_labels()[7:] == ("Growth", "Summary")


def test_summary_sql_casts_the_pinned_now():
    # Without the cast Postgres infers $1 as an interval and rejects the query
    # (found by the real-Postgres probe, invisible to the fake pools above).
    assert "$1 - interval" not in bg.SUMMARY
    assert bg.SUMMARY.count("$1::timestamptz - interval") == 5

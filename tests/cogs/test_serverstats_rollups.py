"""ST2: the server-statistics READ layer (cogs/community/serverstats/rollups.py).

What is pinned here, in order of how much it would hurt to lose it:

1. HONEST WINDOWS. A guild with 3 days of collection is told "3 days" and its
   average divides by 3; a delta is published ONLY when the current AND the
   previous window are both complete (never partial-vs-full, never a division by
   an empty previous window). COMPLETE DAYS ONLY on the comparison: the overview
   stops at yesterday, so flat traffic reads 0% at 03:00 UTC exactly as it does
   at 23:00 - the sawtooth a partial current day would print on the headline
   number is the single most visible way this module could lie.
2. HOLES ARE HOLES. A day with no snapshot comes back as unknown (``None``),
   never as a zero - "we do not know" and "nothing happened" must stay
   distinguishable all the way to the card, and the growth series and the
   activity series must agree on WHICH days those are. A week that ended before
   the guild installed the bot is a hole too, not a calm "net +0".
3. ONE INDEXED QUERY PER QUESTION: each read is asserted to issue the statements
   it claims with the window bounds it claims. Two of them take exactly two
   queries - retention on a leveling guild, and the activity series, which pairs
   its message read with the watched-days read that makes it honest.
4. AGGREGATES ONLY. No read may return user ids, and none may lean on the
   partial member cache: the SQL is asserted to touch ``user_id`` only inside a
   COUNT(DISTINCT ...).

The SQL itself was probed against the REAL local PostgreSQL inside a rolled-back
transaction (values verified, not just execution, and EXPLAIN captured); the
tests below stay hermetic and cover the shaping plus the query contract. That
probe also ran the sawtooth counter-test on real rows: on 39 days of flat
traffic plus a partial today, the today-inclusive bounds returned 6125 vs 7000
(-12.5%) where the complete-day bounds return 7000 vs 7000 (0.0%).
"""

import datetime

import pytest

from cogs.community.leveling.engine import iso_week_period_key
from cogs.community.serverstats import rollups

TODAY = datetime.date(2026, 7, 28)  # Tuesday, ISO week W2026-31
GUILD = 111222333444555666


def day(offset):
    """``offset`` days BEFORE TODAY."""
    return TODAY - datetime.timedelta(days=offset)


class ScriptedPool:
    """asyncpg pool stand-in serving one canned answer per call, in order."""

    def __init__(self, *, fetch=(), fetchrow=None, fetchval=None):
        self.fetch_script = list(fetch)
        self.fetchrow_return = fetchrow
        self.fetchval_return = fetchval
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.fetch_script.pop(0) if self.fetch_script else []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.fetchrow_return

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.fetchval_return


# ---------------------------------------------------------------------------
# Pure helpers: windows, clamps, week keys
# ---------------------------------------------------------------------------


def test_window_includes_today_and_previous_window_abuts_it():
    start, previous_start = rollups.window_bounds(TODAY, 7)
    assert start == day(6)  # today plus the six days before it
    assert previous_start == day(13)
    # The two windows touch without overlapping or leaving a gap.
    assert (start - previous_start).days == 7


def test_window_of_one_day_is_today():
    start, previous_start = rollups.window_bounds(TODAY, 1)
    assert start == TODAY
    assert previous_start == day(1)


def test_overview_bounds_stop_at_the_last_complete_day():
    start, previous_start, end = rollups.overview_bounds(TODAY, 7)
    # The day in progress is in NEITHER window: the comparison is complete days
    # against complete days, whatever time it is.
    assert end == day(1)
    assert start == day(7)
    assert previous_start == day(14)
    assert (start - previous_start).days == 7
    assert rollups.day_span(start, end) == [day(o) for o in range(7, 0, -1)]


def test_overview_bounds_of_one_day_is_yesterday():
    start, previous_start, end = rollups.overview_bounds(TODAY, 1)
    assert (start, end) == (day(1), day(1))
    assert previous_start == day(2)


def test_day_span_is_inclusive_and_empty_when_reversed():
    assert rollups.day_span(day(2), TODAY) == [day(2), day(1), TODAY]
    assert rollups.day_span(TODAY, day(1)) == []


def test_clamps_keep_every_window_bounded():
    assert rollups.clamp_days(7) == 7
    assert rollups.clamp_days(0) == 1
    assert rollups.clamp_days(9999) == rollups.MAX_WINDOW_DAYS
    assert rollups.clamp_days(None) == rollups.DEFAULT_SERIES_DAYS
    assert rollups.clamp_days("nope", default=7) == 7
    assert rollups.clamp_limit(500) == rollups.MAX_TOP_CHANNELS
    assert rollups.clamp_limit(-3) == 1
    assert rollups.clamp_limit(None) == 10
    assert rollups.clamp_weeks(99) == rollups.MAX_RETENTION_WEEKS
    assert rollups.clamp_weeks(0) == 1
    assert rollups.clamp_weeks(None) == rollups.DEFAULT_RETENTION_WEEKS


def test_week_keys_are_the_leveling_keys_oldest_first():
    keys = rollups.week_keys(TODAY, 8)
    assert len(keys) == 8
    assert keys[-1] == iso_week_period_key(TODAY) == "W2026-31"
    assert keys[0] == "W2026-24"
    assert keys == sorted(keys)  # zero padded, so lexical order IS chronological


def test_week_keys_cross_the_iso_year_boundary():
    # 2026-01-03 is ISO week 2026-01, so eight weeks back reaches into 2025.
    keys = rollups.week_keys(datetime.date(2026, 1, 3), 8)
    assert keys[-1] == "W2026-01"
    assert keys[0] == "W2025-46"
    assert keys == sorted(keys)


def test_week_window_start_is_the_monday_opening_the_oldest_week():
    start = rollups.week_window_start(TODAY, 8)
    assert start.weekday() == 0  # Monday
    assert iso_week_period_key(start) == rollups.week_keys(TODAY, 8)[0]


# ---------------------------------------------------------------------------
# shape_overview: the honesty rules
# ---------------------------------------------------------------------------


def overview_row(current, previous, first_day):
    return {
        "current_messages": current,
        "previous_messages": previous,
        "first_day": first_day,
    }


class DayTablePool:
    """A pool stand-in that REALLY evaluates the OVERVIEW aggregate.

    Canned rows would let a wrong window pass: the split and the range bounds
    are what is under test, so this fake applies them to a day -> messages table
    exactly as the SQL does (FILTER on the split, BETWEEN on the range, MIN(day)
    over what was scanned).
    """

    def __init__(self, by_day):
        self.by_day = dict(by_day)
        self.calls = []

    async def fetchrow(self, query, guild_id, split, range_start, range_end):
        self.calls.append((query, (guild_id, split, range_start, range_end)))
        scanned = {
            when: count
            for when, count in self.by_day.items()
            if range_start <= when <= range_end
        }
        return {
            "current_messages": sum(
                count for when, count in scanned.items() if when >= split
            ),
            "previous_messages": sum(
                count for when, count in scanned.items() if when < split
            ),
            "first_day": min(scanned) if scanned else None,
        }


async def test_flat_traffic_reads_zero_delta_whatever_time_it_is():
    """THE test for the sawtooth: 60 days of exactly 1000 messages a day, read
    at 03:00 UTC (today has only 125 of its messages in yet).

    Including the day in progress would put 6 full days + an eighth of one
    against 7 full days and print -14% on the headline number of a guild whose
    traffic never moved - a delta that would climb back to 0% by 23:59 without a
    single message being sent. The window stops at yesterday, so it reads 0.0.
    """
    table = {day(offset): 1000 for offset in range(1, 60)}
    table[TODAY] = 125  # three hours into the day
    pool = DayTablePool(table)

    shaped = await rollups.overview(pool, GUILD, days=7, today=TODAY, since=day(59))

    assert shaped.delta_pct == 0.0
    assert shaped.comparable is True
    assert shaped.total_messages == 7000
    assert shaped.previous_messages == 7000
    assert shaped.days_available == 7
    assert shaped.average_per_day == 1000.0
    assert shaped.end_day == day(1)
    # Today never even entered the scan.
    assert pool.calls[0][1] == (GUILD, day(7), day(14), day(1))


@pytest.mark.parametrize("messages_so_far", [0, 125, 999, 7900])
async def test_the_day_in_progress_cannot_move_the_overview(messages_so_far):
    """Same guild, same flat history, four different moments of the same day:
    every read must return the identical numbers."""
    table = {day(offset): 1000 for offset in range(1, 60)}
    table[TODAY] = messages_so_far
    shaped = await rollups.overview(
        DayTablePool(table), GUILD, days=7, today=TODAY, since=day(59)
    )
    assert (shaped.total_messages, shaped.previous_messages) == (7000, 7000)
    assert shaped.delta_pct == 0.0


async def test_a_real_trend_still_shows_through_complete_days():
    # The guard against a fake delta must not swallow a true one: the last seven
    # complete days doubled, and that is what the card should say.
    table = {day(offset): 1000 for offset in range(8, 30)}
    table.update({day(offset): 2000 for offset in range(1, 8)})
    table[TODAY] = 3  # irrelevant either way
    shaped = await rollups.overview(
        DayTablePool(table), GUILD, days=7, today=TODAY, since=day(29)
    )
    assert shaped.delta_pct == 100.0


def test_overview_full_windows_publish_a_delta():
    shaped = rollups.shape_overview(overview_row(140, 70, day(30)), TODAY, 7)
    assert shaped.total_messages == 140
    assert shaped.days_available == 7
    assert shaped.average_per_day == 20.0
    assert shaped.previous_days_available == 7
    assert shaped.delta_pct == 100.0
    assert shaped.comparable is True
    assert shaped.partial is False
    assert shaped.end_day == day(1)  # the last COMPLETE day


def test_overview_delta_can_be_negative():
    shaped = rollups.shape_overview(overview_row(70, 140, day(30)), TODAY, 7)
    assert shaped.delta_pct == -50.0


def test_overview_partial_window_reports_real_days_and_no_delta():
    # Collection started 3 days ago (today included), so it covers TWO complete
    # days - the average must divide by 2, and there is nothing honest to
    # compare against.
    shaped = rollups.shape_overview(overview_row(90, 0, day(2)), TODAY, 7)
    assert shaped.days_available == 2
    assert shaped.average_per_day == 45.0
    assert shaped.previous_days_available == 0
    assert shaped.delta_pct is None
    assert shaped.partial is True


def test_overview_of_a_guild_installed_today_has_nothing_complete_to_show():
    # Every row this guild owns is in the day still running: zero complete days,
    # so no average is invented from a fraction of a day.
    shaped = rollups.shape_overview(overview_row(0, 0, None), TODAY, 7, since=TODAY)
    assert shaped.days_available == 0
    assert shaped.average_per_day == 0.0
    assert shaped.partial is True
    assert shaped.delta_pct is None


def test_overview_refuses_a_delta_when_only_the_previous_window_is_partial():
    # Data starts inside the PREVIOUS window (day(14)..day(8)): the current
    # window is complete but the previous one covers 3 days, so comparing them
    # would invent a trend.
    shaped = rollups.shape_overview(overview_row(140, 40, day(10)), TODAY, 7)
    assert shaped.days_available == 7
    assert shaped.previous_days_available == 3
    assert shaped.delta_pct is None
    assert shaped.comparable is False


def test_overview_refuses_a_delta_when_the_previous_window_is_silent():
    # Complete windows, but the previous one holds zero messages: a percentage
    # against zero is undefined, not "infinite growth".
    shaped = rollups.shape_overview(overview_row(140, 0, day(30)), TODAY, 7)
    assert shaped.previous_days_available == 7
    assert shaped.delta_pct is None


def test_overview_without_any_data_is_all_zeroes_and_no_division():
    for row in (None, overview_row(0, 0, None)):
        shaped = rollups.shape_overview(row, TODAY, 7)
        assert shaped.total_messages == 0
        assert shaped.days_available == 0
        assert shaped.average_per_day == 0.0
        assert shaped.delta_pct is None
        assert shaped.partial is True


def test_overview_data_older_than_both_windows_still_counts_as_full():
    shaped = rollups.shape_overview(overview_row(10, 10, day(89)), TODAY, 30)
    assert shaped.days_available == 30
    assert shaped.previous_days_available == 30


def test_overview_since_tells_a_silent_guild_from_a_young_one():
    # No message row anywhere in the two windows. Without `since` that reads as
    # "no history"; with it (collection started a month ago) it is what it
    # really is - a full, honest window that happens to be silent.
    silent = rollups.shape_overview(overview_row(0, 0, None), TODAY, 7, since=day(30))
    assert silent.days_available == 7
    assert silent.average_per_day == 0.0
    assert silent.partial is False
    assert silent.delta_pct is None  # nothing to divide by, still no fake trend

    young = rollups.shape_overview(overview_row(0, 0, None), TODAY, 7)
    assert young.days_available == 0
    assert young.partial is True


def test_overview_since_wins_over_the_scanned_first_day():
    # The scan only sees the last two windows, so its first_day can be far more
    # recent than the day collection actually started.
    shaped = rollups.shape_overview(overview_row(140, 70, day(3)), TODAY, 7)
    assert shaped.days_available == 3 and shaped.delta_pct is None
    with_since = rollups.shape_overview(
        overview_row(140, 70, day(3)), TODAY, 7, since=day(60)
    )
    assert with_since.days_available == 7
    assert with_since.previous_days_available == 7
    assert with_since.delta_pct == 100.0


async def test_overview_forwards_since_to_the_shaping():
    pool = ScriptedPool(fetchrow=overview_row(0, 0, None))
    shaped = await rollups.overview(pool, GUILD, days=7, today=TODAY, since=day(30))
    assert shaped.days_available == 7
    assert len(pool.calls) == 1  # still ONE query - `since` is data, not a read


def test_overview_clamps_an_absurd_window():
    shaped = rollups.shape_overview(overview_row(10, 10, day(5)), TODAY, 10**6)
    assert shaped.days == rollups.MAX_WINDOW_DAYS


def test_overview_falls_back_to_the_overview_default_not_the_series_one():
    # A junk window must land on the OVERVIEW default (7): re-clamping to the
    # 30-day series default would silently widen the card's headline window.
    for junk in (None, "seven"):
        shaped = rollups.shape_overview(overview_row(10, 10, day(60)), TODAY, junk)
        assert shaped.days == rollups.DEFAULT_OVERVIEW_DAYS == 7
        assert shaped.days_available == 7


def test_overview_tolerates_null_sums_from_an_empty_scan():
    shaped = rollups.shape_overview(overview_row(None, None, None), TODAY, 7)
    assert (shaped.total_messages, shaped.previous_messages) == (0, 0)


# ---------------------------------------------------------------------------
# shape_top_channels
# ---------------------------------------------------------------------------


def test_top_channels_keeps_the_query_order_and_drops_empty_channels():
    rows = [
        {"channel_id": 900, "messages": 70},
        {"channel_id": 901, "messages": 15},
        {"channel_id": 902, "messages": 0},
        {"channel_id": 903, "messages": None},
    ]
    ranked = rollups.shape_top_channels(rows, limit=10)
    assert [(c.channel_id, c.messages) for c in ranked] == [(900, 70), (901, 15)]


def test_top_channels_reapplies_the_limit_and_clamps_it():
    rows = [{"channel_id": 900 + i, "messages": 100 - i} for i in range(40)]
    assert len(rollups.shape_top_channels(rows, limit=3)) == 3
    assert len(rollups.shape_top_channels(rows, limit=999)) == rollups.MAX_TOP_CHANNELS


def test_top_channels_of_a_silent_guild_is_empty():
    assert rollups.shape_top_channels([], limit=10) == []
    assert rollups.shape_top_channels(None, limit=10) == []


# ---------------------------------------------------------------------------
# shape_growth: holes stay holes
# ---------------------------------------------------------------------------


def growth_row(offset, member_count=100, joins=2, leaves=1):
    return {
        "day": day(offset),
        "member_count": member_count,
        "joins": joins,
        "leaves": leaves,
    }


def test_growth_marks_the_days_that_have_no_snapshot():
    rows = [growth_row(offset) for offset in (4, 3, 1, 0)]  # 2 is missing
    shaped = rollups.shape_growth(rows, TODAY, 5)
    assert len(shaped.points) == 5
    assert [p.day for p in shaped.points] == rollups.day_span(day(4), TODAY)
    hole = next(p for p in shaped.points if p.day == day(2))
    assert hole.has_data is False
    assert (hole.joins, hole.leaves, hole.member_count, hole.net) == (
        None,
        None,
        None,
        None,
    )
    assert shaped.days_with_data == 4
    # Totals sum only the days that exist - the hole contributes nothing.
    assert (shaped.total_joins, shaped.total_leaves, shaped.net) == (8, 4, 4)


def test_growth_keeps_joins_when_only_the_member_snapshot_is_missing():
    rows = [
        growth_row(2, member_count=None),
        growth_row(1, member_count=500),
        growth_row(0, member_count=505),
    ]
    shaped = rollups.shape_growth(rows, TODAY, 3)
    first = shaped.points[0]
    assert first.has_data is True
    assert first.member_count is None
    assert (first.joins, first.net) == (2, 1)
    # first/last come from the SNAPSHOTS that exist, so the delta is real.
    assert (shaped.member_count_first, shaped.member_count_last) == (500, 505)
    assert shaped.member_delta == 5


def test_growth_without_two_snapshots_has_no_member_delta():
    shaped = rollups.shape_growth([growth_row(0, member_count=None)], TODAY, 3)
    assert shaped.member_count_first is None
    assert shaped.member_delta is None


def test_growth_of_a_guild_with_no_rows_is_all_unknown():
    shaped = rollups.shape_growth([], TODAY, 7)
    assert len(shaped.points) == 7
    assert all(p.has_data is False for p in shaped.points)
    assert (shaped.total_joins, shaped.total_leaves, shaped.net) == (0, 0, 0)
    assert shaped.days_with_data == 0
    assert shaped.member_delta is None


def test_growth_ignores_a_row_outside_the_window():
    rows = [growth_row(0), growth_row(40)]
    shaped = rollups.shape_growth(rows, TODAY, 3)
    assert len(shaped.points) == 3
    assert shaped.days_with_data == 1
    assert shaped.total_joins == 2


# ---------------------------------------------------------------------------
# shape_activity: a zero inside the collection era, unknown before it
# ---------------------------------------------------------------------------


def activity_row(offset, messages):
    return {"day": day(offset), "messages": messages}


def test_activity_zero_inside_the_era_unknown_before_it():
    rows = [activity_row(3, 30), activity_row(1, 12)]  # day 2 silent, day 0 silent
    shaped = rollups.shape_activity(rows, TODAY, 7)
    by_day = {p.day: p for p in shaped.points}
    assert by_day[day(5)].has_data is False  # before the first collected day
    assert by_day[day(5)].messages is None
    assert by_day[day(2)].has_data is True  # inside the era: a real zero
    assert by_day[day(2)].messages == 0
    assert by_day[day(0)].messages == 0
    assert shaped.total_messages == 42
    assert shaped.days_with_data == 4  # day(3) .. day(0)


def test_activity_uses_an_explicit_since_when_given():
    rows = [activity_row(1, 12)]
    shaped = rollups.shape_activity(rows, TODAY, 5, since=day(3))
    by_day = {p.day: p for p in shaped.points}
    assert by_day[day(4)].has_data is False
    assert by_day[day(3)].has_data is True and by_day[day(3)].messages == 0
    assert shaped.days_with_data == 4


def test_activity_peak_is_the_earliest_of_the_tied_maxima():
    rows = [activity_row(2, 10), activity_row(1, 10), activity_row(0, 3)]
    shaped = rollups.shape_activity(rows, TODAY, 3)
    assert shaped.peak_messages == 10
    assert shaped.peak_day == day(2)


def test_activity_of_a_silent_guild_has_no_peak():
    shaped = rollups.shape_activity([], TODAY, 7)
    assert shaped.total_messages == 0
    assert shaped.days_with_data == 0
    assert shaped.peak_day is None
    assert all(p.messages is None for p in shaped.points)


# ---------------------------------------------------------------------------
# shape_activity + shape_growth agree on what a hole is
# ---------------------------------------------------------------------------


def test_activity_and_growth_mark_the_same_downtime_day():
    """The bot was down on day(2): no guild-day snapshot, and no message row.

    Growth already renders that day as a hole. Read from the message table
    alone, activity would render it as a confident ZERO - a day the guild is
    told it produced nothing, dragging the curve and every average taken off it.
    The watched days are the fix, and the two series must agree hole for hole.
    """
    grown = rollups.shape_growth(
        [growth_row(offset) for offset in (4, 3, 1, 0)], TODAY, 5
    )
    shaped = rollups.shape_activity(
        [activity_row(offset, 100) for offset in (4, 3, 1, 0)],
        TODAY,
        5,
        watched_days=grown.watched_days,
    )
    assert [(p.day, p.has_data) for p in shaped.points] == [
        (p.day, p.has_data) for p in grown.points
    ]
    hole = next(p for p in shaped.points if p.day == day(2))
    assert (hole.has_data, hole.messages) == (False, None)
    # The hole is out of the total AND out of the denominator.
    assert shaped.days_with_data == 4
    assert shaped.total_messages == 400


def test_activity_keeps_a_real_zero_on_a_watched_but_silent_day():
    # Watched and silent is NOT the same fact as not watched: the snapshot for
    # day(2) exists, so nobody spoke that day and 0 is the honest answer.
    grown = rollups.shape_growth(
        [growth_row(offset) for offset in (4, 3, 2, 1, 0)], TODAY, 5
    )
    shaped = rollups.shape_activity(
        [activity_row(offset, 100) for offset in (4, 3, 1, 0)],
        TODAY,
        5,
        watched_days=grown.watched_days,
    )
    quiet = next(p for p in shaped.points if p.day == day(2))
    assert (quiet.has_data, quiet.messages) == (True, 0)
    assert shaped.days_with_data == 5
    assert shaped.total_messages == 400


def test_growth_watched_days_is_exactly_the_days_that_carry_a_snapshot():
    grown = rollups.shape_growth(
        [growth_row(offset) for offset in (4, 3, 1, 0)], TODAY, 5
    )
    assert grown.watched_days == {day(4), day(3), day(1), day(0)}


def test_activity_watched_days_wins_over_the_era_fallback():
    # An explicit (empty) watch set means "we were never up in this window",
    # which must beat the "first message row starts the era" guess.
    shaped = rollups.shape_activity(
        [activity_row(1, 12)], TODAY, 5, since=day(4), watched_days=()
    )
    assert all(p.has_data is False for p in shaped.points)
    assert shaped.days_with_data == 0
    assert shaped.total_messages == 0
    assert shaped.peak_day is None


# ---------------------------------------------------------------------------
# shape_retention: aggregates only, and an honest leveling flag
# ---------------------------------------------------------------------------


KEYS = rollups.week_keys(TODAY, 8)


def net_row(key, joins, leaves):
    return {"week_key": key, "joins": joins, "leaves": leaves}


def active_row(key, actives):
    return {"week_key": key, "active_members": actives}


def test_retention_fills_every_requested_week():
    shaped = rollups.shape_retention([net_row(KEYS[-1], 10, 4)], (), KEYS)
    assert [w.week for w in shaped.weeks] == KEYS
    assert shaped.weeks[-1].net == 6
    # A week with no row is a quiet week for joins/leaves (the guild-day table
    # is written every day), so zero is the right answer there - as long as the
    # week WAS inside the collection era, which is what has_data says.
    assert shaped.weeks[0].joins == 0 and shaped.weeks[0].net == 0
    assert all(w.has_data is True for w in shaped.weeks)


def test_week_end_is_the_sunday_of_the_key():
    end = rollups.week_end("W2026-31")
    assert end == datetime.date(2026, 8, 2)  # Sunday closing TODAY's week
    assert iso_week_period_key(end) == "W2026-31"
    assert rollups.week_end("W2026-01") == datetime.date(2026, 1, 4)


@pytest.mark.parametrize(
    "installed_days_ago, unknown, real",
    [
        (10, 5, 3),  # 2026-07-18, ISO week W2026-29: weeks 24..28 never seen
        (6, 6, 2),  # 2026-07-22, ISO week W2026-30: weeks 24..29 never seen
    ],
)
def test_retention_marks_the_weeks_before_the_guild_installed_the_bot(
    installed_days_ago, unknown, real
):
    """A guild installed inside the window did not live the earlier weeks.

    Without this, an 8-week block on a 10-day-old guild draws six flat "net +0"
    bars the server never experienced - the same lie the activity series refuses
    to tell for a day the bot was down.
    """
    since = day(installed_days_ago)
    shaped = rollups.shape_retention(
        [net_row(KEYS[-1], 10, 4)], (), KEYS, since=since
    )
    assert unknown + real == len(KEYS)
    for week in shaped.weeks[:unknown]:
        assert week.has_data is False
        assert (week.joins, week.leaves, week.net) == (None, None, None)
        # Every unknown week really did end before collection started.
        assert rollups.week_end(week.week) < since
    for week in shaped.weeks[unknown:]:
        assert week.has_data is True
        assert week.net is not None
    assert shaped.weeks[-1].net == 6


def test_retention_keeps_the_straddling_week_as_real_data():
    # Collection started on a Wednesday: that week was partly observed, and the
    # row it produced is real - only the weeks entirely before it are unknown.
    since = day(6)  # 2026-07-22, a Wednesday in W2026-30
    shaped = rollups.shape_retention(
        [net_row("W2026-30", 4, 1)], (), KEYS, since=since
    )
    straddling = next(w for w in shaped.weeks if w.week == "W2026-30")
    assert straddling.has_data is True
    assert (straddling.joins, straddling.net) == (4, 3)


def test_retention_without_since_assumes_every_week_was_watched():
    # Backwards-compatible default: a caller that cannot say when collection
    # started gets the old behaviour rather than a window of fake unknowns.
    shaped = rollups.shape_retention([], (), KEYS)
    assert all(w.has_data is True and w.net == 0 for w in shaped.weeks)


def test_retention_without_leveling_has_no_activity_half():
    shaped = rollups.shape_retention(
        [net_row(KEYS[-1], 3, 1)], [active_row(KEYS[-1], 42)], KEYS, leveling=False
    )
    assert shaped.leveling is False
    assert shaped.has_activity_data is False
    # Even handed activity rows are ignored when the guild has no leveling.
    assert all(w.active_members is None for w in shaped.weeks)


def test_retention_with_leveling_but_no_xp_rows_still_hides_the_half():
    shaped = rollups.shape_retention([net_row(KEYS[-1], 3, 1)], [], KEYS, leveling=True)
    assert shaped.leveling is True
    assert shaped.has_activity_data is False


def test_retention_activity_covers_only_the_unpruned_weeks():
    # xp_period keeps a few periods back (cogs.community.leveling.engine.PRUNE_PERIODS_BACK), so
    # the older half of an 8-week window is legitimately empty: those weeks must
    # read None (unknown), never 0 (nobody was active).
    rows = [active_row(KEYS[-1], 30), active_row(KEYS[-2], 25)]
    shaped = rollups.shape_retention([], rows, KEYS, leveling=True)
    assert shaped.has_activity_data is True
    assert shaped.weeks[-1].active_members == 30
    assert shaped.weeks[-2].active_members == 25
    assert all(w.active_members is None for w in shaped.weeks[:-2])


def test_retention_counts_a_zero_active_week_as_data():
    shaped = rollups.shape_retention([], [active_row(KEYS[-1], 0)], KEYS, leveling=True)
    assert shaped.weeks[-1].active_members == 0
    assert shaped.has_activity_data is True


# ---------------------------------------------------------------------------
# The read contract: one indexed statement per question, right parameters
# ---------------------------------------------------------------------------


async def test_overview_runs_one_statement_with_both_window_bounds():
    pool = ScriptedPool(fetchrow=overview_row(140, 70, day(30)))
    shaped = await rollups.overview(pool, GUILD, days=7, today=TODAY)
    assert len(pool.calls) == 1
    method, query, args = pool.calls[0]
    assert method == "fetchrow"
    assert query is rollups.OVERVIEW
    # Split at day(7), scanned range day(14)..day(1): today is OUT of the scan.
    assert args == (GUILD, day(7), day(14), day(1))
    assert shaped.total_messages == 140


async def test_top_channels_passes_the_clamped_limit_to_sql():
    pool = ScriptedPool(fetch=[[{"channel_id": 900, "messages": 5}]])
    await rollups.top_channels(pool, GUILD, days=30, limit=10**6, today=TODAY)
    assert len(pool.calls) == 1
    method, query, args = pool.calls[0]
    assert (method, query) == ("fetch", rollups.TOP_CHANNELS)
    assert args == (GUILD, day(29), TODAY, rollups.MAX_TOP_CHANNELS)


async def test_growth_runs_one_bounded_statement_including_today():
    pool = ScriptedPool(fetch=[[growth_row(0)]])
    await rollups.growth(pool, GUILD, days=30, today=TODAY)
    assert [c[1] for c in pool.calls] == [rollups.GROWTH]
    # A series, not a comparison: the day in progress IS the last point.
    assert pool.calls[0][2] == (GUILD, day(29), TODAY)


async def test_activity_pairs_its_read_with_the_watched_days_read():
    pool = ScriptedPool(
        fetch=[[activity_row(0, 5)], [{"day": day(1)}, {"day": TODAY}]]
    )
    shaped = await rollups.activity_series(pool, GUILD, days=30, today=TODAY)
    assert [c[1] for c in pool.calls] == [
        rollups.ACTIVITY_SERIES,
        rollups.WATCHED_DAYS,
    ]
    # Both statements cover the SAME window, so the two answers line up day for
    # day - otherwise the honesty flag would be read off the wrong range.
    assert pool.calls[0][2] == pool.calls[1][2] == (GUILD, day(29), TODAY)
    # Only the two watched days carry data; the other 28 are holes, not zeroes.
    assert shaped.days_with_data == 2
    assert shaped.total_messages == 5


async def test_activity_skips_its_second_read_when_given_the_watched_days():
    pool = ScriptedPool(fetch=[[activity_row(0, 5)]])
    shaped = await rollups.activity_series(
        pool, GUILD, days=30, today=TODAY, watched_days={TODAY}
    )
    assert [c[1] for c in pool.calls] == [rollups.ACTIVITY_SERIES]
    assert shaped.days_with_data == 1


async def test_retention_without_leveling_never_touches_xp_period():
    pool = ScriptedPool(fetch=[[net_row(KEYS[-1], 3, 1)]])
    shaped = await rollups.retention(pool, GUILD, weeks=8, today=TODAY)
    assert len(pool.calls) == 1
    assert pool.calls[0][1] is rollups.RETENTION_NET
    assert pool.calls[0][2] == (GUILD, rollups.week_window_start(TODAY, 8), TODAY)
    assert shaped.has_activity_data is False


async def test_retention_with_leveling_adds_exactly_one_keyed_query():
    pool = ScriptedPool(
        fetch=[[net_row(KEYS[-1], 3, 1)], [active_row(KEYS[-1], 12)]]
    )
    shaped = await rollups.retention(
        pool, GUILD, weeks=8, leveling=True, today=TODAY
    )
    assert [c[1] for c in pool.calls] == [
        rollups.RETENTION_NET,
        rollups.RETENTION_ACTIVITY,
    ]
    assert pool.calls[1][2] == (GUILD, KEYS[0], KEYS[-1])
    assert shaped.has_activity_data is True


async def test_retention_forwards_since_to_the_shaping():
    pool = ScriptedPool(fetch=[[net_row(KEYS[-1], 3, 1)]])
    shaped = await rollups.retention(
        pool, GUILD, weeks=8, today=TODAY, since=day(6)
    )
    assert len(pool.calls) == 1  # still ONE query - `since` is data, not a read
    assert [w.has_data for w in shaped.weeks] == [False] * 6 + [True] * 2


async def test_data_since_is_one_scalar_read():
    pool = ScriptedPool(fetchval=day(30))
    assert await rollups.data_since(pool, GUILD) == day(30)
    assert len(pool.calls) == 1
    assert pool.calls[0][:2] == ("fetchval", rollups.DATA_SINCE)


async def test_reads_default_to_the_collector_s_own_utc_day(monkeypatch):
    # The window must be aligned on the SAME day arithmetic the collectors write
    # with, otherwise "today" could be off by one against the rows.
    monkeypatch.setattr(rollups.buffer, "utc_day", lambda: 20662)
    assert rollups.today_utc() == datetime.date(2026, 7, 28)
    pool = ScriptedPool(fetchrow=overview_row(1, 1, None))
    await rollups.overview(pool, GUILD, days=7)
    # The overview stops one day short of that day on purpose (complete days).
    assert pool.calls[0][2][3] == datetime.date(2026, 7, 27)
    pool = ScriptedPool(fetch=[[], []])
    await rollups.activity_series(pool, GUILD, days=7)
    # The series does reach it.
    assert pool.calls[0][2][2] == datetime.date(2026, 7, 28)


# ---------------------------------------------------------------------------
# SQL hygiene: single statements, guild scoped, aggregates only
# ---------------------------------------------------------------------------


ALL_SQL = {
    "OVERVIEW": rollups.OVERVIEW,
    "TOP_CHANNELS": rollups.TOP_CHANNELS,
    "GROWTH": rollups.GROWTH,
    "ACTIVITY_SERIES": rollups.ACTIVITY_SERIES,
    "WATCHED_DAYS": rollups.WATCHED_DAYS,
    "RETENTION_NET": rollups.RETENTION_NET,
    "RETENTION_ACTIVITY": rollups.RETENTION_ACTIVITY,
    "DATA_SINCE": rollups.DATA_SINCE,
}


@pytest.mark.parametrize("name", sorted(ALL_SQL))
def test_every_read_is_a_single_guild_scoped_statement(name):
    sql = ALL_SQL[name]
    # asyncpg prepares ONE statement per call: a stray ';' would be a runtime
    # error, not a style issue.
    assert sql.strip().count(";") == 1
    assert sql.strip().endswith(";")
    assert "guild_id = $1" in sql


@pytest.mark.parametrize("name", sorted(ALL_SQL))
def test_no_read_ever_selects_a_user_id(name):
    sql = ALL_SQL[name]
    if "user_id" not in sql:
        return
    # The ONE query allowed near user rows counts them, never lists them.
    assert name == "RETENTION_ACTIVITY"
    assert "COUNT(DISTINCT user_id)" in sql
    assert sql.count("user_id") == 1


def test_module_never_reads_the_partial_member_cache():
    # chunk_guilds_at_startup=False means guild.members is an arbitrary subset,
    # so a statistic built on it would be silently wrong. Checked on the parsed
    # CODE, not the text, so the prose that explains the rule cannot trip it.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(rollups))
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "members" not in attributes
    # The only member-ish attributes left are this module's OWN snapshot fields,
    # which come from the stored member_count, not from the cache.
    assert {name for name in attributes if name.startswith("member")} <= {
        "member_count",
        "member_count_first",
        "member_count_last",
        "member_delta",
    }

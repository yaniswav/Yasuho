"""D1: the weekly server-stats digest (delivery layer).

What is pinned here:

1. THE WINDOW. The digest reports the last FULL ISO week (Monday..Sunday) and
   never the day in progress, on every weekday the loop could run on.
2. THE HONESTY RULES, carried over from rollups.py unchanged: a day with no
   ``server_stats_days`` row is NOT a zero (it is left out of every total), the
   week-over-week delta exists ONLY when both weeks were fully observed, and a
   week with zero observed days is not posted at all.
3. EXACTLY ONCE PER GUILD PER WEEK: the claim SQL is an atomic
   INSERT ... ON CONFLICT whose RETURNING is the permission to send, the claim
   happens BEFORE the message, and every failure after it (guild gone, channel
   gone, permissions revoked, no data, HTTP error) leaves the row claimed - no
   retry storm.
4. THE FAN-OUT is bounded per tick and the candidate query EXCLUDES the guilds
   already delivered this week, so later ticks make progress.
   Delivery is allowed on ANY day (a bot down for one Monday must not drop that
   week for the whole fleet), which is safe precisely because the reported
   window and the claim key are constant across the week.
5. The state table is guild-scoped and dies with the guild.

Nothing here touches a database, Discord or the network: the SQL shapes were
probed separately in a rolled-back transaction (see the lot report).
"""

import asyncio
import datetime
import pathlib
import types

import discord
import pytest

from cogs.community.leveling.engine import iso_week_period_key
from cogs.community.serverstats import charts, digest, rollups
from cogs.community.serverstats import cog as serverstats_cog
from tools import retention, settings

# A Monday (2026-08-03) - a delivery day. The week it reports is
# 2026-07-27 (Mon) .. 2026-08-02 (Sun), i.e. yesterday and the six days before.
MONDAY = datetime.date(2026, 8, 3)
WEEK_START = datetime.date(2026, 7, 27)
WEEK_END = datetime.date(2026, 8, 2)
PREVIOUS_START = datetime.date(2026, 7, 20)
PREVIOUS_END = datetime.date(2026, 7, 26)


def _moment(day, hour=6):
    return datetime.datetime(
        day.year, day.month, day.day, hour, tzinfo=datetime.timezone.utc
    )


def _growth_rows(days, joins=0, leaves=0):
    """A ``server_stats_days`` row per day - i.e. the days we were WATCHING."""
    return [
        {"day": day, "member_count": 100, "joins": joins, "leaves": leaves}
        for day in days
    ]


def _activity_rows(by_day):
    return [{"day": day, "messages": count} for day, count in by_day.items()]


def _span(start, end):
    return rollups.day_span(start, end)


# ---------------------------------------------------------------------------
# The window: the last FULL week, never the day in progress
# ---------------------------------------------------------------------------


def test_the_period_is_the_week_that_ended_yesterday_on_a_monday():
    start, end = digest.digest_period(MONDAY)
    assert (start, end) == (WEEK_START, WEEK_END)
    assert end == MONDAY - datetime.timedelta(days=1)
    assert start.weekday() == 0 and end.weekday() == 6


@pytest.mark.parametrize("offset", range(7))
def test_the_period_never_contains_today_whatever_day_it_is(offset):
    """The delivery day is Monday, but the maths must not depend on it: widening
    the window later must not change WHICH week a digest reports."""
    today = MONDAY + datetime.timedelta(days=offset)
    start, end = digest.digest_period(today)
    assert end < today
    assert (end - start).days == 6
    assert start.weekday() == 0 and end.weekday() == 6
    # A full ISO week: both ends carry the same week key.
    assert iso_week_period_key(start) == iso_week_period_key(end)


def test_the_period_key_is_the_house_iso_week_key():
    start, _end = digest.digest_period(MONDAY)
    assert digest.period_key(start) == iso_week_period_key(start)
    # ... and the week the CLAIM is keyed on is the current one, not the
    # reported one: they must never be the same string.
    assert digest.period_key(MONDAY) != digest.period_key(start)
    assert rollups.week_end(digest.period_key(start)) == WEEK_END


def test_the_previous_period_is_contiguous_with_the_reported_one():
    start, _end = digest.digest_period(MONDAY)
    previous_start, previous_end = digest.previous_period(start)
    assert (previous_start, previous_end) == (PREVIOUS_START, PREVIOUS_END)
    assert previous_end + datetime.timedelta(days=1) == start


def test_every_day_of_one_week_reports_that_week_and_claims_the_same_key():
    """The invariant that makes a LATE digest safe to allow at all.

    Delivery is not pinned to Monday (a bot down for one Monday would otherwise
    drop that week for the whole fleet, permanently). That is only sound because
    both the REPORTED window and the CLAIM key are constant across the week: a
    Tuesday tick can only repeat Monday's answer, and the claim forbids
    repeating Monday's message.
    """
    for offset in range(7):
        day = MONDAY + datetime.timedelta(days=offset)
        assert digest.digest_period(day) == (WEEK_START, WEEK_END)
        assert digest.period_key(day) == digest.period_key(MONDAY)


# ---------------------------------------------------------------------------
# Shaping: the honesty rules
# ---------------------------------------------------------------------------


def _full_two_weeks(current=700, previous=700, joins=0, leaves=0):
    days = _span(PREVIOUS_START, WEEK_END)
    growth = _growth_rows(days)
    for row in growth:
        if WEEK_START <= row["day"] <= WEEK_END:
            row["joins"] = joins
            row["leaves"] = leaves
    activity = _activity_rows(
        {
            **{day: previous // 7 for day in _span(PREVIOUS_START, PREVIOUS_END)},
            **{day: current // 7 for day in _span(WEEK_START, WEEK_END)},
        }
    )
    return activity, growth


def test_two_fully_observed_weeks_publish_a_delta():
    activity, growth = _full_two_weeks(current=700, previous=350)
    report = digest.shape_digest(activity, growth, None, MONDAY)

    assert report.observed_days == 7 and report.previous_observed_days == 7
    assert report.messages == 700 and report.previous_messages == 350
    assert report.delta_pct == 100.0
    assert report.has_data and not report.partial


def test_a_partially_observed_week_publishes_no_delta_and_never_a_zero():
    activity, growth = _full_two_weeks(current=700, previous=700)
    # Drop one day of the reported week: the collector was down.
    growth = [row for row in growth if row["day"] != WEEK_END]
    report = digest.shape_digest(activity, growth, None, MONDAY)

    assert report.observed_days == 6
    assert report.partial
    assert report.delta_pct is None  # NOT 0.0 - the weeks are not comparable


def test_a_partially_observed_previous_week_publishes_no_delta():
    activity, growth = _full_two_weeks()
    growth = [row for row in growth if row["day"] != PREVIOUS_START]
    report = digest.shape_digest(activity, growth, None, MONDAY)

    assert report.observed_days == 7
    assert report.previous_observed_days == 6
    assert report.delta_pct is None


def test_a_silent_previous_week_publishes_no_delta():
    """Dividing by zero is not a comparison, and 'up infinity percent' is not a
    statistic - the same guard rollups.shape_overview applies."""
    activity, growth = _full_two_weeks(current=700, previous=0)
    report = digest.shape_digest(activity, growth, None, MONDAY)
    assert report.previous_messages == 0
    assert report.delta_pct is None


def test_a_week_nobody_watched_has_no_data_and_is_not_a_wall_of_zeroes():
    report = digest.shape_digest([], [], None, MONDAY)
    assert report.observed_days == 0
    assert not report.has_data
    assert report.messages == 0  # structural, and precisely why it is not posted
    assert report.busiest_day is None and report.delta_pct is None


def test_messages_on_an_unwatched_day_are_never_counted():
    """A message row without its guild-day row means the day was not observed
    (rollups.WATCHED_DAYS is the authority), so it is a hole, not a total."""
    watched = _span(WEEK_START, WEEK_END)[:-1]
    growth = _growth_rows(watched)
    activity = _activity_rows({day: 10 for day in _span(WEEK_START, WEEK_END)})
    report = digest.shape_digest(activity, growth, None, MONDAY)

    assert report.observed_days == 6
    assert report.messages == 60  # not 70
    assert report.busiest_day != WEEK_END


def test_the_busiest_day_is_the_peak_of_the_observed_days():
    days = _span(WEEK_START, WEEK_END)
    growth = _growth_rows(days)
    counts = {day: 5 for day in days}
    counts[days[3]] = 99
    report = digest.shape_digest(_activity_rows(counts), growth, None, MONDAY)

    assert report.busiest_day == days[3]
    assert report.busiest_messages == 99


def test_a_tie_keeps_the_earliest_day_and_a_silent_week_names_none():
    days = _span(WEEK_START, WEEK_END)
    growth = _growth_rows(days)

    tied = digest.shape_digest(
        _activity_rows({day: 7 for day in days}), growth, None, MONDAY
    )
    assert tied.busiest_day == days[0]

    silent = digest.shape_digest([], growth, None, MONDAY)
    assert silent.has_data  # the days WERE observed...
    assert silent.messages == 0
    assert silent.busiest_day is None  # ... there is simply no "most active" one


def test_joins_and_leaves_come_from_the_reported_week_only():
    days = _span(PREVIOUS_START, WEEK_END)
    growth = _growth_rows(days, joins=1, leaves=0)
    for row in growth:
        if row["day"] < WEEK_START:
            row["joins"], row["leaves"] = 50, 50
        else:
            row["joins"], row["leaves"] = 3, 1
    report = digest.shape_digest([], growth, None, MONDAY)

    assert (report.joins, report.leaves, report.net) == (21, 7, 14)


def test_active_members_is_carried_through_untouched_including_none():
    growth = _growth_rows(_span(WEEK_START, WEEK_END))
    assert digest.shape_digest([], growth, None, MONDAY).active_members is None
    assert digest.shape_digest([], growth, 12, MONDAY).active_members == 12


# ---------------------------------------------------------------------------
# U3: the chart points shape_digest builds alongside the totals
# ---------------------------------------------------------------------------
def test_shape_digest_chart_points_span_the_reported_week():
    activity, growth = _full_two_weeks(current=700, previous=350)
    report = digest.shape_digest(activity, growth, None, MONDAY)

    assert len(report.chart_points) == 7
    assert report.chart_points[0].day == WEEK_START
    assert report.chart_points[-1].day == WEEK_END
    assert all(point.messages is not None for point in report.chart_points)


def test_shape_digest_ghost_present_only_when_both_weeks_fully_observed():
    """The exact same gate delta_pct uses (both weeks 7/7 observed) - a
    ghost is never offered for a week the collector partly missed."""
    activity, growth = _full_two_weeks(current=700, previous=350)
    full = digest.shape_digest(activity, growth, None, MONDAY)
    assert full.chart_previous_points is not None
    assert len(full.chart_previous_points) == 7
    assert full.chart_previous_points[0].day == PREVIOUS_START

    partial_growth = [row for row in growth if row["day"] != WEEK_END]
    partial = digest.shape_digest(activity, partial_growth, None, MONDAY)
    assert partial.chart_previous_points is None
    # The reported week's own points are still built, holes and all - only
    # the GHOST is withheld, never the main series.
    assert len(partial.chart_points) == 7
    assert any(point.messages is None for point in partial.chart_points)


def test_shape_digest_chart_holes_match_the_totals_holes():
    """The same day excluded from the message total (see
    test_messages_on_an_unwatched_day_are_never_counted) is a hole in
    chart_points too - one honesty test, not two that could drift apart."""
    watched = _span(WEEK_START, WEEK_END)[:-1]
    growth = _growth_rows(watched)
    activity = _activity_rows({day: 10 for day in _span(WEEK_START, WEEK_END)})
    report = digest.shape_digest(activity, growth, None, MONDAY)

    holes = [point for point in report.chart_points if point.messages is None]
    assert len(holes) == 1
    assert holes[0].day == WEEK_END
    assert holes[0].net is None


def test_shape_digest_chart_net_is_joins_minus_leaves_per_day():
    days = _span(WEEK_START, WEEK_END)
    growth = _growth_rows(days, joins=3, leaves=1)
    report = digest.shape_digest([], growth, None, MONDAY)

    assert all(point.net == 2 for point in report.chart_points)


def test_shape_digest_chart_points_are_charts_chartpoint_instances():
    activity, growth = _full_two_weeks()
    report = digest.shape_digest(activity, growth, None, MONDAY)
    assert all(isinstance(point, charts.ChartPoint) for point in report.chart_points)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fields(embed):
    return {field.name: field.value for field in embed.fields}


def test_the_embed_names_the_period_explicitly_in_its_footer():
    activity, growth = _full_two_weeks()
    embed = digest.render(digest.shape_digest(activity, growth, None, MONDAY), "Guild")

    assert "2026-07-27" in embed.footer.text
    assert "2026-08-02" in embed.footer.text
    assert digest.period_key(WEEK_START) in embed.footer.text


def test_an_incomparable_week_says_so_instead_of_printing_a_percentage():
    activity, growth = _full_two_weeks()
    growth = [row for row in growth if row["day"] != PREVIOUS_END]
    embed = digest.render(digest.shape_digest(activity, growth, None, MONDAY), "Guild")

    body = "\n".join(_fields(embed).values())
    assert "%" not in body


def test_a_partial_week_is_labelled_and_a_full_one_is_not():
    activity, growth = _full_two_weeks()
    full = digest.render(digest.shape_digest(activity, growth, None, MONDAY), "G")
    partial = digest.render(
        digest.shape_digest(
            activity, [row for row in growth if row["day"] != WEEK_END], None, MONDAY
        ),
        "G",
    )
    assert len(_fields(partial)) == len(_fields(full)) + 1


def test_the_optional_sections_are_omitted_rather_than_dashed():
    growth = _growth_rows(_span(WEEK_START, WEEK_END))
    without = digest.render(digest.shape_digest([], growth, None, MONDAY), "G")
    with_extras = digest.render(
        digest.shape_digest(
            _activity_rows({WEEK_START: 5}), growth, 9, MONDAY
        ),
        "G",
    )
    assert len(_fields(with_extras)) == len(_fields(without)) + 2
    assert all(value.strip() for value in _fields(with_extras).values())


def test_render_sets_the_embed_image_only_when_a_chart_filename_is_given():
    """render() is still pure either way - it never renders anything itself,
    it only points the embed at whatever attachment the caller says it is
    sending. No filename means the embed carries no image, same as before
    U3."""
    activity, growth = _full_two_weeks()
    report = digest.shape_digest(activity, growth, None, MONDAY)

    plain = digest.render(report, "Guild")
    assert plain.image.url is None

    with_chart = digest.render(report, "Guild", chart_filename=digest.CHART_FILENAME)
    assert with_chart.image.url == f"attachment://{digest.CHART_FILENAME}"


def test_every_rendered_field_stays_inside_discord_limits():
    activity, growth = _full_two_weeks(current=10**9, previous=1, joins=10**6, leaves=1)
    embed = digest.render(digest.shape_digest(activity, growth, 10**6, MONDAY), "G" * 100)
    for name, value in _fields(embed).items():
        assert 0 < len(name) <= 256
        assert 0 < len(value) <= 1024
    assert len(embed.footer.text) <= 2048


# ---------------------------------------------------------------------------
# The SQL shapes (probed live in a rolled-back transaction; pinned here)
# ---------------------------------------------------------------------------


def test_the_claim_is_atomic_and_returns_only_to_the_winner():
    sql = " ".join(digest.CLAIM.split())
    assert sql.startswith("INSERT INTO serverstats_digest_state")
    assert "ON CONFLICT (guild_id) DO UPDATE" in sql
    assert "WHERE serverstats_digest_state.last_iso_week <> EXCLUDED.last_iso_week" in sql
    assert sql.rstrip(";").endswith("RETURNING guild_id")


def test_the_candidate_query_excludes_the_guilds_already_delivered():
    """Without the anti-join a LIMITed fan-out would hand every tick the same
    first rows and the guilds behind them would never be reached."""
    sql = " ".join(digest.CANDIDATES.split())
    assert "LEFT JOIN serverstats_digest_state st" in sql
    assert "st.last_iso_week = $1" in sql
    assert "st.guild_id IS NULL" in sql
    assert "gs.settings ? 'serverstats_digest_channel'" in sql
    assert "LIMIT $2" in sql


def test_a_json_null_value_reads_as_off_not_as_an_opt_in():
    """``settings ? key`` is TRUE for a JSON *null*, so the presence test alone
    would treat ``{"serverstats_digest_channel": null}`` as opted in, claim it
    and warn about it every single week. The bot's own `off` path DELETEs the
    key, but the dashboard writes this key too and nulling it out is the obvious
    way to get that row. ``->>`` yields SQL NULL for a JSON null."""
    sql = " ".join(digest.CANDIDATES.split())
    assert "gs.settings ->> 'serverstats_digest_channel' IS NOT NULL" in sql


def test_the_presence_test_matches_the_partial_index_in_the_schema():
    schema = (
        pathlib.Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )
    assert "CREATE TABLE IF NOT EXISTS serverstats_digest_state" in schema
    assert "guild_settings_digest_channel_idx" in schema
    # The index predicate and the query's WHERE clause must be the SAME
    # expression, or the planner silently ignores the index.
    assert "WHERE settings ? 'serverstats_digest_channel'" in schema


def test_turning_the_digest_off_deletes_the_key_rather_than_nulling_it():
    assert digest.CLEAR_KEY == (
        "UPDATE guild_settings SET settings = settings - $2::text WHERE guild_id = $1"
    )


async def test_clear_channel_deletes_the_key_and_evicts_the_cached_blob(
    fake_pool, monkeypatch
):
    evicted = []
    monkeypatch.setattr(settings, "invalidate_guild", evicted.append)

    await digest.clear_channel(fake_pool, 42)

    assert fake_pool.calls == [("execute", digest.CLEAR_KEY, (42, digest.KEY_DIGEST_CHANNEL))]
    assert evicted == [42]


async def test_set_channel_writes_through_the_settings_layer(fake_pool, monkeypatch):
    written = []

    async def _set_guild(pool, guild_id, key, value):
        written.append((guild_id, key, value))

    monkeypatch.setattr(settings, "set_guild", _set_guild)
    await digest.set_channel(fake_pool, 42, 777)

    assert written == [(42, digest.KEY_DIGEST_CHANNEL, 777)]


async def test_claim_reports_the_winner_and_only_the_winner(fake_pool):
    fake_pool.fetchval_return = 42
    assert await digest.claim(fake_pool, 42, "W2026-32") is True
    fake_pool.fetchval_return = None
    assert await digest.claim(fake_pool, 42, "W2026-32") is False
    assert [call[1] for call in fake_pool.calls] == [digest.CLAIM, digest.CLAIM]


# ---------------------------------------------------------------------------
# collect(): the query budget, COUNTED
# ---------------------------------------------------------------------------


class _RoutingPool:
    """Answers each rollups SQL constant with its own canned result."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.answers.get(query, [])

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.answers.get(query)

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 1"


async def test_collect_issues_two_reads_without_leveling_over_fourteen_days():
    pool = _RoutingPool()
    await digest.collect(pool, 1, MONDAY)

    assert [query for _m, query, _a in pool.calls] == [
        rollups.ACTIVITY_SERIES,
        rollups.GROWTH,
    ]
    for _method, _query, args in pool.calls:
        assert args == (1, PREVIOUS_START, WEEK_END)


async def test_collect_pays_exactly_one_extra_read_on_a_leveling_guild():
    key = digest.period_key(WEEK_START)
    pool = _RoutingPool({rollups.RETENTION_ACTIVITY: [{"active_members": 5}]})
    report = await digest.collect(pool, 1, MONDAY, leveling=True)

    assert len(pool.calls) == 3
    assert pool.calls[-1][1] == rollups.RETENTION_ACTIVITY
    assert pool.calls[-1][2] == (1, key, key)
    assert report.active_members == 5


async def test_a_leveling_week_with_no_xp_row_stays_unknown_never_zero():
    """xp_period is pruned a few periods back, so an absent row cannot be told
    from a genuinely idle week - the digest says neither."""
    pool = _RoutingPool()
    report = await digest.collect(pool, 1, MONDAY, leveling=True)
    assert report.active_members is None


async def test_collect_never_reads_the_collection_start():
    pool = _RoutingPool()
    await digest.collect(pool, 1, MONDAY, leveling=True)
    assert all(query != rollups.DATA_SINCE for _m, query, _a in pool.calls)


# ---------------------------------------------------------------------------
# The loop: claim before send, bounded fan-out, failures cost one line
# ---------------------------------------------------------------------------


class _Permissions:
    def __init__(self, **granted):
        self._granted = granted

    def __getattr__(self, name):
        return self._granted.get(name, False)


# The three REQUIRED permissions (digest.DIGEST_PERMISSIONS), plus
# attach_files - which is deliberately NOT required: a channel without it
# still gets the whole text digest, only without the U3 chart, so it is
# granted here and revoked explicitly by the tests that care.
ALL_GRANTED = {name: True for name in digest.DIGEST_PERMISSIONS}
ALL_GRANTED["attach_files"] = True


class _Channel:
    def __init__(self, channel_id=500, permissions=None, configurer_permissions=None):
        self.id = channel_id
        self._permissions = _Permissions(**(permissions or ALL_GRANTED))
        # /serverstats digest set preflights TWO parties against this channel -
        # the bot and the member configuring it - so the fake has to be able to
        # answer differently for each, or a test can pass on the wrong half.
        self._configurer_permissions = (
            _Permissions(**configurer_permissions)
            if configurer_permissions is not None
            else None
        )
        self.sends = []
        self.raises = None

    def permissions_for(self, member):
        if self._configurer_permissions is not None and getattr(
            member, "is_configurer", False
        ):
            return self._configurer_permissions
        return self._permissions

    async def send(self, *args, **kwargs):
        if self.raises is not None:
            raise self.raises
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=1)


class _Guild:
    def __init__(self, guild_id=7, channel=None, name="Guild", unavailable=False):
        self.id = guild_id
        self.name = name
        self.me = object()
        self.preferred_locale = "en-US"
        self.unavailable = unavailable
        self._channel = channel

    def get_channel(self, channel_id):
        if self._channel is not None and self._channel.id == channel_id:
            return self._channel
        return None


class _DeliveryPool(_RoutingPool):
    """A routing pool that also answers the digest's own two statements."""

    def __init__(self, candidates=(), claim=True, answers=None):
        super().__init__(answers)
        self.answers[digest.CANDIDATES] = list(candidates)
        self.claim = claim
        self.trace = []

    async def fetch(self, query, *args):
        self.trace.append("candidates" if query == digest.CANDIDATES else "read")
        return await super().fetch(query, *args)

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if query == digest.CLAIM:
            self.trace.append("claim")
            return args[0] if self.claim else None
        return self.answers.get(query)


def _cog(pool, guild=None, leveling=False):
    bot = types.SimpleNamespace(
        db_pool=pool,
        is_ready=lambda: True,
        get_guild=lambda gid: guild if guild is not None and gid == guild.id else None,
        get_cog=lambda name: (
            types.SimpleNamespace(is_enabled=lambda gid: True)
            if leveling and name == "Leveling"
            else None
        ),
    )
    return serverstats_cog.ServerStats(bot)


def _candidate(guild_id=7, channel_id="500"):
    return {"guild_id": guild_id, "channel_id": channel_id}


def _observed_week():
    return {
        rollups.GROWTH: _growth_rows(_span(PREVIOUS_START, WEEK_END), joins=2, leaves=1),
        rollups.ACTIVITY_SERIES: _activity_rows(
            {day: 10 for day in _span(PREVIOUS_START, WEEK_END)}
        ),
    }


async def test_a_missed_monday_is_recovered_later_in_the_same_week():
    """A bot down for the whole of one Monday must not drop that week for the
    ENTIRE fleet, permanently. A later tick of the same week delivers the same
    week - a LATE digest, never a stale or a different one."""
    channel = _Channel()
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog(pool, guild=_Guild(channel=channel))

    tuesday = MONDAY + datetime.timedelta(days=1)
    assert await cog.run_digest_once(_moment(tuesday)) == 1

    claim = [call for call in pool.calls if call[1] == digest.CLAIM][0]
    assert claim[2] == (7, digest.period_key(MONDAY))
    _args, kwargs = channel.sends[0]
    assert digest.period_key(WEEK_START) in kwargs["embed"].footer.text


async def test_a_week_already_claimed_is_never_posted_twice_however_many_ticks():
    """The other half of the same trade: allowing every day to deliver may never
    turn into a second message. The claim is what forbids it."""
    channel = _Channel()
    pool = _DeliveryPool(
        candidates=[_candidate()], claim=False, answers=_observed_week()
    )
    cog = _cog(pool, guild=_Guild(channel=channel))

    for offset in range(7):
        day = MONDAY + datetime.timedelta(days=offset)
        assert await cog.run_digest_once(_moment(day)) == 0
    assert channel.sends == []


async def test_a_delivery_tick_claims_before_it_posts():
    channel = _Channel()
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog(pool, guild=_Guild(channel=channel))

    assert await cog.run_digest_once(_moment(MONDAY)) == 1

    assert pool.trace[0] == "candidates"
    assert pool.trace[1] == "claim"
    assert channel.sends, "the digest was not posted"
    _args, kwargs = channel.sends[0]
    assert isinstance(kwargs["embed"], discord.Embed)
    assert kwargs["allowed_mentions"].everyone is False
    assert cog._stats["digests"] == 1


async def test_the_claim_key_is_the_current_week_and_the_report_the_one_before():
    channel = _Channel()
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog(pool, guild=_Guild(channel=channel))

    await cog.run_digest_once(_moment(MONDAY))

    claim = [call for call in pool.calls if call[1] == digest.CLAIM][0]
    assert claim[2] == (7, digest.period_key(MONDAY))
    _args, kwargs = channel.sends[0]
    assert digest.period_key(WEEK_START) in kwargs["embed"].footer.text


async def test_the_fan_out_is_bounded_per_tick():
    pool = _DeliveryPool(candidates=[])
    cog = _cog(pool)

    await cog.run_digest_once(_moment(MONDAY))

    candidates = [call for call in pool.calls if call[1] == digest.CANDIDATES][0]
    assert candidates[2] == (digest.period_key(MONDAY), digest.FAN_OUT_LIMIT)
    assert digest.FAN_OUT_LIMIT <= 100  # a tick must stay a tick


async def test_a_guild_that_lost_the_claim_is_not_posted_to():
    channel = _Channel()
    pool = _DeliveryPool(candidates=[_candidate()], claim=False, answers=_observed_week())
    cog = _cog(pool, guild=_Guild(channel=channel))

    assert await cog.run_digest_once(_moment(MONDAY)) == 0
    assert channel.sends == []
    # ... and it did not even pay the reads.
    assert all(call[1] != rollups.GROWTH for call in pool.calls)


async def test_a_missing_channel_is_claimed_anyway_and_logged_once(caplog):
    pool = _DeliveryPool(candidates=[_candidate(channel_id="999")], answers=_observed_week())
    cog = _cog(pool, guild=_Guild(channel=_Channel()))

    with caplog.at_level("WARNING"):
        assert await cog.run_digest_once(_moment(MONDAY)) == 0

    assert any(call[1] == digest.CLAIM for call in pool.calls)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


async def test_a_stored_id_that_is_not_a_snowflake_is_claimed_and_skipped(caplog):
    """The dashboard writes this key too, and JavaScript can hand over anything."""
    pool = _DeliveryPool(candidates=[_candidate(channel_id="nope")], answers=_observed_week())
    cog = _cog(pool, guild=_Guild(channel=_Channel()))

    with caplog.at_level("WARNING"):
        assert await cog.run_digest_once(_moment(MONDAY)) == 0
    assert any(call[1] == digest.CLAIM for call in pool.calls)


async def test_a_channel_nobody_can_post_in_is_claimed_and_skipped():
    """A category or a forum resolves fine and has no ``send`` - it must be
    caught before the post, not by an AttributeError in the tick."""
    category = types.SimpleNamespace(id=500)  # no send()
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    guild = _Guild()
    guild._channel = category
    cog = _cog(pool, guild=guild)

    assert await cog.run_digest_once(_moment(MONDAY)) == 0
    assert any(call[1] == digest.CLAIM for call in pool.calls)


async def test_an_unwritable_channel_is_claimed_anyway_and_not_posted_to(caplog):
    channel = _Channel(permissions={"view_channel": True, "send_messages": True})
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog(pool, guild=_Guild(channel=channel))

    with caplog.at_level("WARNING"):
        assert await cog.run_digest_once(_moment(MONDAY)) == 0

    assert channel.sends == []
    assert any(call[1] == digest.CLAIM for call in pool.calls)
    assert "embed_links" in caplog.text


async def test_a_guild_this_process_cannot_see_is_claimed_and_skipped():
    pool = _DeliveryPool(candidates=[_candidate(guild_id=404)], answers=_observed_week())
    cog = _cog(pool, guild=_Guild(channel=_Channel()))

    assert await cog.run_digest_once(_moment(MONDAY)) == 0
    assert [call[2] for call in pool.calls if call[1] == digest.CLAIM] == [
        (404, digest.period_key(MONDAY))
    ]


async def test_an_unavailable_guild_is_NOT_claimed_so_a_later_tick_retries():
    """An outage - or a re-IDENTIFY, which re-adds every guild as a stub - would
    otherwise burn the week's claim for every server at once."""
    channel = _Channel()
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog(pool, guild=_Guild(channel=channel, unavailable=True))

    assert await cog.run_digest_once(_moment(MONDAY)) == 0
    assert channel.sends == []
    assert all(call[1] != digest.CLAIM for call in pool.calls)


async def test_a_week_nobody_watched_is_claimed_and_never_posted():
    """Posting "we saw nothing" every week would be noise, and it would also be
    a claim about a week the collector cannot speak for."""
    channel = _Channel()
    pool = _DeliveryPool(candidates=[_candidate()])  # no rows at all
    cog = _cog(pool, guild=_Guild(channel=channel))

    assert await cog.run_digest_once(_moment(MONDAY)) == 0
    assert channel.sends == []
    assert any(call[1] == digest.CLAIM for call in pool.calls)


async def test_a_discord_failure_costs_one_warning_and_never_the_tick(caplog):
    channel = _Channel()
    channel.raises = discord.HTTPException(
        types.SimpleNamespace(status=403, reason="Forbidden"), "nope"
    )
    pool = _DeliveryPool(
        candidates=[_candidate(), _candidate(guild_id=404)], answers=_observed_week()
    )
    cog = _cog(pool, guild=_Guild(channel=channel))

    with caplog.at_level("WARNING"):
        assert await cog.run_digest_once(_moment(MONDAY)) == 0

    # The second guild was still processed: one broken guild cannot stop a tick.
    assert len([call for call in pool.calls if call[1] == digest.CLAIM]) == 2


async def test_a_cold_bot_delivers_nothing():
    pool = _DeliveryPool(candidates=[_candidate()])
    cog = _cog(pool)
    cog.bot.is_ready = lambda: False
    assert await cog.run_digest_once(_moment(MONDAY)) == 0
    assert pool.calls == []


def _cog_with_loop(pool, guild=None, leveling=False, semaphore=None):
    """The same fake bot :func:`_cog` builds, PLUS a real event loop.

    Every OTHER delivery test above uses a bot with no ``.loop`` at all,
    which is what already exercises the chart's fallback path implicitly
    (tools.rendering.run_image_job needs ``bot.loop`` and raises
    AttributeError without one, caught by cog._render_chart_file). These
    chart-specific tests need a WORKING loop to prove the success path too.
    """
    bot = types.SimpleNamespace(
        db_pool=pool,
        is_ready=lambda: True,
        get_guild=lambda gid: guild if guild is not None and gid == guild.id else None,
        get_cog=lambda name: (
            types.SimpleNamespace(is_enabled=lambda gid: True)
            if leveling and name == "Leveling"
            else None
        ),
        loop=asyncio.get_event_loop(),
    )
    if semaphore is not None:
        bot.image_render_semaphore = semaphore
    return serverstats_cog.ServerStats(bot)


async def test_a_delivered_digest_attaches_a_rendered_chart():
    channel = _Channel()
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog_with_loop(pool, guild=_Guild(channel=channel))

    assert await cog.run_digest_once(_moment(MONDAY)) == 1

    _args, kwargs = channel.sends[0]
    assert kwargs["file"] is not None
    assert kwargs["file"].filename == digest.CHART_FILENAME
    assert kwargs["embed"].image.url == f"attachment://{digest.CHART_FILENAME}"


async def test_a_digest_chart_render_failure_falls_back_to_text_only(monkeypatch):
    """A broken chart must never cost the whole digest - the same guild
    still gets its (text-only) message this week."""
    channel = _Channel()
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog_with_loop(pool, guild=_Guild(channel=channel))

    def boom(*_args, **_kwargs):
        raise RuntimeError("pillow exploded")

    monkeypatch.setattr(charts, "render_activity_chart", boom)

    assert await cog.run_digest_once(_moment(MONDAY)) == 1

    _args, kwargs = channel.sends[0]
    assert kwargs["file"] is None
    assert kwargs["embed"].image.url is None


async def test_a_saturated_semaphore_never_blocks_digest_delivery(monkeypatch):
    """A permanently-empty semaphore must not hang a weekly broadcast - it
    gives up after CHART_RENDER_TIMEOUT and posts without the attachment."""
    channel = _Channel()
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    monkeypatch.setattr(digest, "CHART_RENDER_TIMEOUT", 0.05)
    cog = _cog_with_loop(
        pool, guild=_Guild(channel=channel), semaphore=asyncio.Semaphore(0)
    )

    delivered = await asyncio.wait_for(
        cog.run_digest_once(_moment(MONDAY)), timeout=5
    )

    assert delivered == 1
    _args, kwargs = channel.sends[0]
    assert kwargs["file"] is None


async def test_a_channel_without_attach_files_still_gets_its_text_digest(monkeypatch):
    """attach_files is deliberately NOT one of digest.DIGEST_PERMISSIONS: a
    guild that never granted it configured its digest before U3 and must
    keep receiving it. The chart is skipped BEFORE it is rendered - an
    upload that could only be refused is not worth a Pillow job, and the
    refusal would have cost the whole broadcast."""
    granted = dict(ALL_GRANTED)
    granted["attach_files"] = False
    channel = _Channel(permissions=granted)
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog_with_loop(pool, guild=_Guild(channel=channel))

    renders = []
    monkeypatch.setattr(
        charts, "render_activity_chart", lambda *a, **k: renders.append(a) or b""
    )

    assert await cog.run_digest_once(_moment(MONDAY)) == 1

    assert renders == []
    _args, kwargs = channel.sends[0]
    assert kwargs["file"] is None
    assert kwargs["embed"].image.url is None


class _UploadRefusingChannel(_Channel):
    """A channel that takes a plain message but refuses any upload - the
    race the preflight cannot close (attach_files revoked between the
    permission read and the send)."""

    async def send(self, *args, **kwargs):
        if kwargs.get("file") is not None:
            raise discord.HTTPException(
                types.SimpleNamespace(status=403, reason="Forbidden"),
                "Missing Permissions",
            )
        return await super().send(*args, **kwargs)


async def test_a_refused_upload_falls_back_to_the_text_digest(caplog):
    """The week's numbers are the point; the chart is decoration. A refused
    attachment costs the picture, never the broadcast."""
    channel = _UploadRefusingChannel()
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog_with_loop(pool, guild=_Guild(channel=channel))

    with caplog.at_level("WARNING"):
        assert await cog.run_digest_once(_moment(MONDAY)) == 1

    assert len(channel.sends) == 1
    _args, kwargs = channel.sends[0]
    assert kwargs.get("file") is None
    # The embed no longer points at an attachment that was never uploaded.
    assert kwargs["embed"].image.url is None
    assert "text-only" in caplog.text


async def test_a_digest_that_cannot_be_posted_at_all_is_still_only_a_warning(caplog):
    """The retry is a fallback, not a second chance at an unusable channel:
    when the plain send fails too, the guild is skipped exactly as it was
    before U3 - one warning, tick unharmed."""
    channel = _Channel()
    channel.raises = discord.HTTPException(
        types.SimpleNamespace(status=403, reason="Forbidden"), "nope"
    )
    pool = _DeliveryPool(candidates=[_candidate()], answers=_observed_week())
    cog = _cog_with_loop(pool, guild=_Guild(channel=channel))

    with caplog.at_level("WARNING"):
        assert await cog.run_digest_once(_moment(MONDAY)) == 0


async def test_a_leveling_guild_gets_its_actives_line():
    channel = _Channel()
    answers = dict(_observed_week())
    answers[rollups.RETENTION_ACTIVITY] = [{"active_members": 11}]
    pool = _DeliveryPool(candidates=[_candidate()], answers=answers)
    cog = _cog(pool, guild=_Guild(channel=channel), leveling=True)

    assert await cog.run_digest_once(_moment(MONDAY)) == 1
    _args, kwargs = channel.sends[0]
    assert any("11" in field.value for field in kwargs["embed"].fields)


# ---------------------------------------------------------------------------
# The command surface
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, guild):
        self.guild = guild
        # The member running the command. ``is_configurer`` is what _Channel
        # keys its second permission answer on.
        self.author = types.SimpleNamespace(id=42, is_configurer=True)
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=1)


def test_the_digest_controls_hang_off_the_serverstats_surface():
    group = serverstats_cog.ServerStats.serverstats
    assert isinstance(group, discord.ext.commands.HybridGroup)
    digest_group = group.get_command("digest")
    assert digest_group is not None
    assert sorted(command.name for command in digest_group.commands) == ["off", "set"]


def test_every_digest_control_is_gated_on_manage_guild():
    group = serverstats_cog.ServerStats.serverstats.get_command("digest")
    for command in [group, *group.commands]:
        checks = " ".join(
            repr(check.__closure__[0].cell_contents if check.__closure__ else check)
            for check in command.checks
        )
        assert "manage_guild" in checks, command.qualified_name


async def test_setting_a_channel_the_bot_cannot_post_in_writes_nothing(monkeypatch):
    written = []
    monkeypatch.setattr(
        digest, "set_channel", lambda *args: written.append(args)
    )
    # The configurer is fine here; it is the BOT that cannot post.
    channel = _Channel(
        permissions={"view_channel": True}, configurer_permissions=ALL_GRANTED
    )
    channel.mention = "<#500>"
    guild = _Guild(channel=channel)
    ctx = _Ctx(guild)
    cog = _cog(_RoutingPool(), guild=guild)

    await serverstats_cog.ServerStats.serverstats_digest_set.callback(cog, ctx, channel)

    assert written == []
    said = ctx.sends[0][0][0]
    assert "Send Messages" in said
    assert said.startswith("I need")  # the bot's half, not the configurer's


async def test_setting_a_channel_the_configurer_cannot_post_in_writes_nothing(
    monkeypatch,
):
    """Manage Server is not a licence to post anywhere. The bot holds every
    permission on this channel, so without the configurer preflight a manager
    who cannot write a word in it themselves could still schedule a RECURRING
    post there and have the bot deliver it every Monday."""
    written = []

    async def _set_channel(pool, guild_id, channel_id):
        written.append((guild_id, channel_id))

    monkeypatch.setattr(digest, "set_channel", _set_channel)
    channel = _Channel(configurer_permissions={"view_channel": True})
    channel.mention = "<#500>"
    guild = _Guild(channel=channel)
    ctx = _Ctx(guild)
    cog = _cog(_RoutingPool(), guild=guild)

    await serverstats_cog.ServerStats.serverstats_digest_set.callback(cog, ctx, channel)

    assert written == []
    said = ctx.sends[0][0][0]
    assert said.startswith("You need")
    assert "Send Messages" in said


async def test_a_configurer_who_cannot_even_see_the_channel_is_refused(monkeypatch):
    """The other half of the same rule: a channel they cannot view at all."""
    written = []

    async def _set_channel(pool, guild_id, channel_id):
        written.append((guild_id, channel_id))

    monkeypatch.setattr(digest, "set_channel", _set_channel)
    channel = _Channel(configurer_permissions={})
    channel.mention = "<#500>"
    guild = _Guild(channel=channel)
    ctx = _Ctx(guild)
    cog = _cog(_RoutingPool(), guild=guild)

    await serverstats_cog.ServerStats.serverstats_digest_set.callback(cog, ctx, channel)

    assert written == []
    assert "View Channel" in ctx.sends[0][0][0]


def test_the_configurer_is_not_held_to_the_bots_embed_permission():
    """Deliberately shorter than DIGEST_PERMISSIONS: embed_links is the bot's
    problem (it is the one embedding), and refusing a manager over it would
    block a working configuration for nothing."""
    assert set(digest.CONFIGURER_PERMISSIONS) == {"view_channel", "send_messages"}
    assert "embed_links" not in digest.CONFIGURER_PERMISSIONS
    assert set(digest.CONFIGURER_PERMISSIONS) < set(digest.DIGEST_PERMISSIONS)


async def test_setting_a_usable_channel_turns_the_digest_on(monkeypatch):
    written = []

    async def _set_channel(pool, guild_id, channel_id):
        written.append((guild_id, channel_id))

    monkeypatch.setattr(digest, "set_channel", _set_channel)
    channel = _Channel()
    channel.mention = "<#500>"
    guild = _Guild(channel=channel)
    ctx = _Ctx(guild)
    cog = _cog(_RoutingPool(), guild=guild)

    await serverstats_cog.ServerStats.serverstats_digest_set.callback(cog, ctx, channel)

    assert written == [(7, 500)]
    assert ctx.sends[0][1]["allowed_mentions"].everyone is False


async def test_turning_it_off_goes_through_the_deleting_path(monkeypatch):
    cleared = []

    async def _clear_channel(pool, guild_id):
        cleared.append(guild_id)

    monkeypatch.setattr(digest, "clear_channel", _clear_channel)
    guild = _Guild()
    ctx = _Ctx(guild)
    cog = _cog(_RoutingPool(), guild=guild)

    await serverstats_cog.ServerStats.serverstats_digest_off.callback(cog, ctx)

    assert cleared == [7]
    assert ctx.sends


# ---------------------------------------------------------------------------
# Retention: the state table is guild data and dies with the guild
# ---------------------------------------------------------------------------


def test_the_digest_state_table_is_purged_on_guild_departure():
    purged = dict(retention.GUILD_DELETE_QUERIES)
    assert (
        purged["serverstats_digest_state"]
        == "DELETE FROM serverstats_digest_state WHERE guild_id = $1"
    )
    assert "FROM serverstats_digest_state" in retention.STORED_GUILD_IDS_QUERY

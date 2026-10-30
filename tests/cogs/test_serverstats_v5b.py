"""V5b: the Yasuho+ daily summary and CSV export of serverstats.

Pinned here: the daily claim (exactly once per UTC day, restart-safe, late but
never skipped), None-is-not-zero in the shaping, the per-guild delivery hour,
the lapse behaviour (summary stops, config kept), and the CSV export (content,
holes, formula-injection escaping, day cap, premium refusal).

The heatmap of the V5b brief is NOT built: the stored granularity is the UTC
day, so there is no hour to draw (see the lot report). The last test pins that
fact so the day somebody adds an hour column has to revisit the decision.

No database, no Discord, no real clock: every date is derived from FIXED dates.
"""

import datetime
import pathlib
import types

import discord
import pytest

from cogs.community.serverstats import cog as serverstats_cog
from cogs.community.serverstats import daily, export, rollups
from tools import premium

TODAY = datetime.date(2026, 8, 4)
YESTERDAY = datetime.date(2026, 8, 3)
BEFORE = datetime.date(2026, 8, 2)
ONE = datetime.timedelta(days=1)


def _at(day, hour=0):
    return datetime.datetime(
        day.year, day.month, day.day, hour, tzinfo=datetime.timezone.utc
    )


def _growth(day, joins=0, leaves=0, members=100):
    return {"day": day, "member_count": members, "joins": joins, "leaves": leaves}


def _act(day, messages):
    return {"day": day, "messages": messages}


# ---------------------------------------------------------------------------
# Shaping: None is never zero
# ---------------------------------------------------------------------------


def test_the_period_is_yesterday_against_the_day_before():
    assert daily.daily_period(TODAY) == (YESTERDAY, BEFORE)


def test_an_unobserved_yesterday_is_not_a_report():
    report = daily.shape_daily([_act(YESTERDAY, 50)], [_growth(BEFORE)], TODAY)
    assert not report.has_data


def test_an_unobserved_day_before_gives_no_comparison_rather_than_zero():
    report = daily.shape_daily(
        [_act(BEFORE, 99), _act(YESTERDAY, 10)], [_growth(YESTERDAY)], TODAY
    )
    assert report.has_data
    assert report.previous_messages is None
    assert report.delta_pct is None


def test_a_day_before_with_no_messages_gives_no_percentage():
    report = daily.shape_daily(
        [_act(YESTERDAY, 10)], [_growth(BEFORE), _growth(YESTERDAY)], TODAY
    )
    assert report.previous_messages == 0
    assert report.delta_pct is None


def test_the_delta_and_totals_when_both_days_were_observed():
    report = daily.shape_daily(
        [_act(BEFORE, 40), _act(YESTERDAY, 50)],
        [_growth(BEFORE), _growth(YESTERDAY, joins=5, leaves=2)],
        TODAY,
    )
    assert (report.messages, report.previous_messages) == (50, 40)
    assert report.delta_pct == 25.0
    assert (report.joins, report.leaves, report.net) == (5, 2, 3)


def test_a_missing_member_snapshot_is_none_and_never_shown_as_zero_members():
    row = _growth(YESTERDAY, members=None)
    report = daily.shape_daily([], [row], TODAY)
    assert report.member_count is None
    embed = daily.render(report, "G")
    text = " ".join(field.value for field in embed.fields)
    assert "start of the day" not in text
    assert "No comparable day before this one." in text


def test_render_shows_the_member_total_and_the_delta_when_known():
    report = daily.shape_daily(
        [_act(BEFORE, 40), _act(YESTERDAY, 50)],
        [_growth(BEFORE), _growth(YESTERDAY, members=321)],
        TODAY,
    )
    text = " ".join(field.value for field in daily.render(report, "G").fields)
    assert "+25.0% vs the day before" in text
    assert "321 members at the start of the day" in text


# ---------------------------------------------------------------------------
# SQL shapes
# ---------------------------------------------------------------------------


def test_the_claim_is_atomic_and_only_moves_forward():
    sql = " ".join(daily.CLAIM.split())
    assert sql.startswith("INSERT INTO serverstats_daily_state")
    assert "WHERE serverstats_daily_state.last_day < EXCLUDED.last_day" in sql
    assert sql.rstrip(";").endswith("RETURNING guild_id")


def test_the_candidate_query_spreads_the_fleet_over_the_day_and_skips_the_claimed():
    sql = " ".join(daily.CANDIDATES.split())
    assert "(gs.guild_id % 23) + 1 <= $2" in sql
    assert "gs.guild_id <> ALL($4::bigint[])" in sql
    assert "st.last_day = $1" in sql and "st.guild_id IS NULL" in sql
    assert "gs.settings ->> 'serverstats_daily_channel' IS NOT NULL" in sql
    assert "LIMIT $3" in sql


def test_the_presence_test_matches_the_partial_index_in_the_schema():
    schema = (
        pathlib.Path(__file__).resolve().parents[2].joinpath("schema.sql").read_text("utf-8")
    )
    assert "CREATE TABLE IF NOT EXISTS serverstats_daily_state" in schema
    assert "WHERE settings ? 'serverstats_daily_channel'" in schema
    assert "gs.settings ? 'serverstats_daily_channel'" in daily.CANDIDATES


def test_delivery_hours_are_1_to_23_so_nothing_lands_at_midnight_before_the_flush():
    assert {daily.delivery_hour(g) for g in range(92)} == set(range(1, 24))


# ---------------------------------------------------------------------------
# Delivery: fakes
# ---------------------------------------------------------------------------


class _Pool:
    """Answers the daily statements; the claim table is REAL state."""

    def __init__(self, candidates=(), observed=True):
        self.candidates = list(candidates)
        self.claimed = {}  # guild_id -> last_day, with the SQL's `<` rule
        self.calls = []
        days = [BEFORE + ONE * n for n in range(6)]  # covers the next day too
        self.answers = {
            rollups.GROWTH: [_growth(d, joins=1) for d in days] if observed else [],
            rollups.ACTIVITY_SERIES: [_act(d, 10) for d in days],
        }

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if query == daily.CANDIDATES:
            today = args[0]
            return [
                row
                for row in self.candidates
                if self.claimed.get(row["guild_id"]) != today
                and daily.delivery_hour(row["guild_id"]) <= args[1]
                and row["guild_id"] not in args[3]
            ][: args[2]]
        return self.answers.get(query, [])

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        if query == daily.CLAIM:
            guild_id, day = args
            if guild_id in self.claimed and not self.claimed[guild_id] < day:
                return None
            self.claimed[guild_id] = day
            return guild_id
        return None

    async def execute(self, query, *args):
        self.calls.append((query, args))


class _Perms:
    def __getattr__(self, name):
        return True


class _Channel:
    def __init__(self, channel_id=500):
        self.id = channel_id
        self.sends = []

    def permissions_for(self, member):
        return _Perms()

    async def send(self, *args, **kwargs):
        self.sends.append(kwargs)


class _Guild:
    def __init__(self, guild_id, channel):
        self.id = guild_id
        self.name = "Guild"
        self.me = object()
        self.preferred_locale = "en-US"
        self.unavailable = False
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel if self._channel.id == channel_id else None


def _limits(entitled):
    return types.SimpleNamespace(
        serverstats_daily_summary=entitled,
        serverstats_export=entitled,
        serverstats_retention_days=365 if entitled else 90,
    )


def _cog(pool, guilds, entitled=True):
    bot = types.SimpleNamespace(
        db_pool=pool,
        is_ready=lambda: True,
        get_guild=lambda gid: guilds.get(gid),
        get_cog=lambda name: None,
        premium=types.SimpleNamespace(for_guild=lambda gid: _limits(entitled)),
    )
    return serverstats_cog.ServerStats(bot)


def _setup(guild_id=23):  # 23 % 23 + 1 == 1: due from hour 1
    channel = _Channel()
    pool = _Pool([{"guild_id": guild_id, "channel_id": "500"}])
    return channel, pool, {guild_id: _Guild(guild_id, channel)}


async def test_two_ticks_on_the_same_day_post_once():
    channel, pool, guilds = _setup()
    cog = _cog(pool, guilds)
    assert await cog.run_daily_once(_at(TODAY, 1)) == 1
    assert await cog.run_daily_once(_at(TODAY, 2)) == 0
    assert len(channel.sends) == 1
    embed = channel.sends[0]["embed"]
    assert YESTERDAY.isoformat() in embed.footer.text


async def test_a_restart_does_not_repost_the_same_day_and_the_next_day_posts_again():
    channel, pool, guilds = _setup()
    assert await _cog(pool, guilds).run_daily_once(_at(TODAY, 1)) == 1
    # A brand new cog instance (a restart): the state lives in the database.
    restarted = _cog(pool, guilds)
    assert await restarted.run_daily_once(_at(TODAY, 20)) == 0
    assert await restarted.run_daily_once(_at(TODAY + ONE, 1)) == 1
    assert len(channel.sends) == 2


async def test_a_late_tick_reports_the_same_day_an_early_one_would_have():
    channel, pool, guilds = _setup()
    cog = _cog(pool, guilds)
    # Bot was offline all morning: the first tick of the day is at 22:00.
    assert await cog.run_daily_once(_at(TODAY, 22)) == 1
    assert YESTERDAY.isoformat() in channel.sends[0]["embed"].footer.text


async def test_a_guild_is_not_due_before_its_delivery_hour():
    channel, pool, guilds = _setup(guild_id=23 + 4)  # hour 5
    cog = _cog(pool, guilds)
    assert await cog.run_daily_once(_at(TODAY, 4)) == 0
    assert pool.claimed == {}
    assert await cog.run_daily_once(_at(TODAY, 5)) == 1


async def test_an_unobserved_day_is_claimed_but_not_posted():
    channel, pool, guilds = _setup()
    pool.answers[rollups.GROWTH] = []
    cog = _cog(pool, guilds)
    assert await cog.run_daily_once(_at(TODAY, 1)) == 0
    assert channel.sends == []
    assert pool.claimed  # no retry storm


async def test_a_lapsed_server_gets_nothing_and_loses_no_configuration():
    channel, pool, guilds = _setup()
    cog = _cog(pool, guilds, entitled=False)
    assert await cog.run_daily_once(_at(TODAY, 1)) == 0
    assert channel.sends == []
    # Config kept: nothing ever executed a CLEAR_KEY, and the day is NOT burned
    # (a failed premium load must not cost a paying guild its day).
    assert all(query != daily.CLEAR_KEY for query, _a in pool.calls)
    assert pool.claimed == {}
    # ... and when Yasuho+ comes back the summary resumes by itself.
    cog_back = _cog(pool, guilds, entitled=True)
    assert await cog_back.run_daily_once(_at(TODAY, 2)) == 1


def test_the_delta_msgid_carries_no_format_spec_and_the_value_is_preformatted():
    report = daily.shape_daily(
        [_act(BEFORE, 3), _act(YESTERDAY, 4)],
        [_growth(BEFORE), _growth(YESTERDAY)],
        TODAY,
    )
    assert "+33.3% vs the day before" in daily.render(report, "G").fields[0].value


def test_the_member_line_is_singular_for_one_member():
    report = daily.shape_daily([], [_growth(YESTERDAY, members=1)], TODAY)
    text = daily.render(report, "G").fields[1].value
    assert "1 member at the start of the day" in text


async def test_the_daily_pass_uses_the_now_it_is_given_not_the_clock():
    channel, pool, guilds = _setup()
    cog = _cog(pool, guilds)
    await cog.run_daily_once(_at(TODAY, 3))
    candidates = [a for q, a in pool.calls if q == daily.CANDIDATES]
    assert candidates[0][:2] == (TODAY, 3)


async def test_the_loop_reads_the_clock_once_for_both_passes():
    cog = _cog(_Pool(), {})
    seen = []

    async def digest_once(now=None):
        seen.append(now)

    async def daily_once(now=None):
        seen.append(now)

    cog.run_digest_once = digest_once
    cog.run_daily_once = daily_once
    await serverstats_cog.ServerStats._digest_loop.coro(cog)
    assert len(seen) == 2 and seen[0] is not None and seen[0] is seen[1]


async def test_nothing_is_delivered_at_hour_zero():
    channel, pool, guilds = _setup()
    assert await _cog(pool, guilds).run_daily_once(_at(TODAY, 0)) == 0
    assert channel.sends == []


async def test_a_lapsed_guild_does_not_starve_the_guilds_behind_it():
    channel = _Channel()
    lapsed = [{"guild_id": 23 * (n + 1), "channel_id": "500"} for n in range(daily.FAN_OUT_LIMIT)]
    paying = {"guild_id": 23 * 100, "channel_id": "500"}
    pool = _Pool(lapsed + [paying])
    guilds = {r["guild_id"]: _Guild(r["guild_id"], channel) for r in lapsed + [paying]}
    bot_limits = lambda gid: _limits(gid == paying["guild_id"])  # noqa: E731
    cog = _cog(pool, guilds)
    cog.bot.premium = types.SimpleNamespace(for_guild=bot_limits)
    assert await cog.run_daily_once(_at(TODAY, 1)) == 1
    assert set(pool.claimed) == {paying["guild_id"]}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_the_csv_has_one_row_per_day_with_holes_left_empty():
    start, end = BEFORE, YESTERDAY + ONE
    text = export.build_csv(
        [_act(BEFORE, 7), _act(YESTERDAY, 9)],
        [_growth(BEFORE, joins=3, leaves=5, members=None), _growth(YESTERDAY + ONE)],
        start,
        end,
    )
    lines = text.splitlines()
    assert lines[0] == "day,messages,joins,leaves,net,member_count_start"
    # net is a negative NUMBER: it must not be mistaken for a formula.
    assert lines[1] == "2026-08-02,7,3,5,-2,"
    # 2026-08-03 has activity rows but was not observed: a hole, not a zero.
    assert lines[2] == "2026-08-03,,,,,"
    assert lines[3] == "2026-08-04,0,0,0,0,100"


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\t=1", "\r=1"])
def test_text_cells_that_look_like_formulas_get_an_apostrophe(value):
    assert export.safe_cell(value) == "'" + value


def test_plain_text_numbers_and_none_are_untouched():
    assert export.safe_cell("general") == "general"
    assert export.safe_cell(-5) == -5
    assert export.safe_cell(None) == ""


def test_build_csv_sends_every_cell_through_the_escape(monkeypatch):
    seen = []
    monkeypatch.setattr(export, "safe_cell", lambda v: seen.append(v) or v)
    export.build_csv([], [_growth(YESTERDAY)], YESTERDAY, YESTERDAY)
    assert len(seen) == len(export.HEADER)


def test_the_day_cap_is_the_yasuho_plus_retention_ceiling():
    assert export.MAX_EXPORT_DAYS == premium.GUILD_PREMIUM.serverstats_retention_days


class _Ctx:
    def __init__(self):
        self.guild = types.SimpleNamespace(id=1)
        self.author = types.SimpleNamespace(id=2)
        self.interaction = None
        self.sent = []

    async def defer(self):
        pass

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


async def test_export_refuses_a_free_server_with_the_upsell_text_and_reads_nothing():
    pool = _Pool()
    ctx = _Ctx()
    cog = _cog(pool, {}, entitled=False)
    await cog.serverstats_export.callback(cog, ctx, 30)
    assert pool.calls == []
    assert len(ctx.sent) == 1 and "Yasuho+" in ctx.sent[0][0][0]
    assert "file" not in ctx.sent[0][1]


async def test_export_clamps_the_window_to_the_retention_limit_and_attaches_a_csv(
    monkeypatch,
):
    monkeypatch.setattr(rollups, "today_utc", lambda: TODAY)
    pool = _Pool()
    ctx = _Ctx()
    cog = _cog(pool, {}, entitled=True)
    await cog.serverstats_export.callback(cog, ctx, 100000)
    (query, args) = pool.calls[0]
    assert args[1:] == (YESTERDAY - datetime.timedelta(days=364), YESTERDAY)
    (_a, kwargs) = ctx.sent[0]
    assert isinstance(kwargs["file"], discord.File)
    assert kwargs["file"].filename == export.FILENAME
    assert len(kwargs["file"].fp.read()) < 50_000


def test_the_export_has_a_per_guild_cooldown():
    from discord.ext import commands

    cooldown = serverstats_cog.ServerStats.serverstats_export.callback.__commands_cooldown__
    assert cooldown.type is commands.BucketType.guild


# ---------------------------------------------------------------------------
# Heatmap: not buildable from the stored granularity
# ---------------------------------------------------------------------------


def test_the_stored_aggregates_have_no_hour_so_no_heatmap_is_built():
    schema = pathlib.Path(__file__).resolve().parents[2].joinpath("schema.sql").read_text("utf-8")
    start = schema.index("CREATE TABLE IF NOT EXISTS server_stats_messages")
    block = schema[start : schema.index("server_stats_days_day_idx", start)]
    assert "day " in block and "hour" not in block.lower()

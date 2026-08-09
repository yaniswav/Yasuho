"""Purpose: the WEEKLY DIGEST - what last week looked like, posted once a week
into a channel the server chose.

This is a DELIVERY layer on top of ST1/ST2: it collects nothing of its own and
adds no SQL to the read side. Every number below comes out of the statements
rollups.py already owns (ACTIVITY_SERIES, GROWTH and - for a leveling guild -
RETENTION_ACTIVITY), which is what keeps one honesty contract for the card and
the digest instead of two that can drift.

Five rules shape this module.

1. THE HONEST WINDOW. The digest reports the LAST FULL ISO WEEK (Monday..Sunday)
   and never the week in progress, so it can never print a partial week next to
   a full one: :func:`digest_period` ends on the Sunday BEFORE the current
   week's Monday, which is yesterday-or-earlier on every day of the week. The
   day in progress is outside the window by construction - the same call
   rollups.overview_bounds makes for the card's comparison, one period up.

2. A MISSING DAY IS NOT A ZERO. ``server_stats_days`` gets one row per guild per
   UTC day from the collector's snapshot, so the presence of a row is the proof
   the bot was watching (rollups.WATCHED_DAYS says exactly this). Every total
   here is summed over OBSERVED days only, the week-over-week delta is published
   only when BOTH weeks were fully observed (7 days each), and a week with ZERO
   observed days is not posted at all - see :func:`shape_digest` and the loop in
   cog.py. "We saw nothing" is not a statistic worth a weekly message.

3. THE CONFIGURATION IS READ FRESH, FROM THE DATABASE, AT DELIVERY TIME.
   :data:`CANDIDATES` reads ``serverstats_digest_channel`` straight off the
   ``guild_settings`` row and deliberately does NOT go through
   ``tools.settings`` (the in-process LRU). That is a choice, not an oversight:
   the read happens once an hour, so the cache saves nothing measurable, and
   skipping it means the dashboard needs NO invalidation kind for this key - a
   dashboard write lands in the table and the very next tick sees it. The
   command surface still writes THROUGH ``tools.settings`` so any other reader
   of the blob stays coherent. Said completely: this KEY never goes through the
   LRU, but DELIVERING to a guild does warm that guild's whole settings blob
   into it, because the locale resolution one line later (i18n.
   resolve_guild_locale -> settings.get_guild) reads the same row. That read is
   a primary-key lookup at weekly frequency and it cannot serve a stale digest
   channel - the channel came from the fresh read above, not from the cache.

4. EXACTLY ONCE PER GUILD PER WEEK, AND NEVER A RETRY STORM. The claim
   (:data:`CLAIM`) writes the ISO week into ``serverstats_digest_state`` BEFORE
   anything is sent and only the writer that changed the row proceeds. A guild
   whose channel is gone, unwritable, or whose week has no data is claimed all
   the same and simply not posted to: the alternative is an hourly retry against
   a channel that will still be missing next hour.

5. DELIVERY IS NOT PINNED TO ONE WEEKDAY. The natural moment is the Monday that
   follows the reported week - the first day on which that week is COMPLETE -
   and on a healthy fleet that is when every guild gets it, on the first tick of
   the day. The loop is nevertheless allowed to run on ANY hour of the week,
   because refusing to deliver on a Tuesday means a bot that was down for the
   whole of one Monday drops that week for EVERY guild, permanently. It cannot
   double-post: the claim of rule 4 is keyed on the CURRENT ISO week and
   :func:`digest_period` returns the SAME reported week on every day of that
   week, so a later day can only ever produce a LATE digest, never a second one
   and never a stale one. It also lifts the ceiling of the bounded fan-out from
   one day's ticks to the whole week's (see cog.run_digest_once).

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import discord

from . import charts, rollups
from cogs.community.leveling.engine import iso_week_period_key
from tools import settings
from tools.formats import random_colour
from tools.i18n import N_, _

# The guild_settings key. ABSENT means OFF - it is never materialised with a
# neutral value, so a guild that never configured a digest and one that turned
# it off are byte-identical rows (the tickets rule, cogs/config/tickets/
# guild_config.set_key, applied to this one key).
KEY_DIGEST_CHANNEL = "serverstats_digest_channel"

# A full reported period, in days. Named rather than spelled 7 in five places:
# every honesty test below ("was the week fully observed") is a comparison
# against it.
DAYS_PER_WEEK = 7

# How many guilds ONE tick may deliver to. See the pacing story in cog.py's
# run_digest_once: 50 per hourly tick x 168 ticks a week = 8400 opted-in guilds
# per week, which is the honest ceiling of this design. The digest of a healthy
# fleet still lands on Monday - the ticks after it exist so a late one is
# possible at all (rule 5), not so the load can be spread across the week.
FAN_OUT_LIMIT = 50

# What the bot must hold in the target channel. Checked at configuration time
# (so the manager is told immediately) AND again at delivery time (a role or an
# overwrite can change any time in between) - the same double check
# cogs/config/tickets/preflight.py performs, against its own list.
DIGEST_PERMISSIONS = ("view_channel", "send_messages", "embed_links")

# U3: the PNG chart attached alongside the digest embed. Same rendering seam
# and the same graceful-fallback discipline as views.py's card chart (see
# CHART_RENDER_TIMEOUT there) - cog.py's _deliver_digest never lets a
# saturated semaphore or a Pillow error hold up a weekly broadcast; a digest
# without its chart is still the text-only embed this feature shipped with
# before U3.
CHART_FILENAME = "serverstats_digest_chart.png"
CHART_RENDER_TIMEOUT = 2.0

_PERMISSION_LABELS = {
    "view_channel": N_("View Channel"),
    "send_messages": N_("Send Messages"),
    "embed_links": N_("Embed Links"),
}


# ---------------------------------------------------------------------------
# SQL. Two statements of its own; every STATISTIC is read with a rollups
# statement, so this module adds nothing to the read side.
# ---------------------------------------------------------------------------

# The opted-in guilds this tick may still deliver to, most-recent-week-aware.
#
# THE ANTI-JOIN IS LOAD BEARING, not a nicety. The fan-out is LIMITed, so a
# candidate list that still contained the guilds already delivered this week
# would hand every tick the same first 50 rows and nothing behind them would
# ever be reached. Excluding the delivered ones is what makes the later ticks of
# the same week drain the queue.
#
# ``settings ? key`` is the exact presence test, and it is what the partial index
# in schema.sql (guild_settings_digest_channel_idx) is declared on. MEASURED with
# psql (which uses the simple query protocol, so no prepared-statement plan cache
# can replay a stale plan the way a naive asyncpg EXPLAIN pair does) in a
# rolled-back transaction against a 50k-row guild_settings fixture with 1250
# guilds opted in - a fleet far past this bot's target: without the index the
# plan is a Seq Scan reading every guild's whole blob (1334 buffers, ~9 ms,
# 48760 rows thrown away by the filter); with it the plan is `Index Scan using
# guild_settings_digest_channel_idx` (30 buffers, 0.16 ms) and it is FLAT in
# fleet size because the index holds only the opted-in guilds. The drained case
# - a later tick of a week where every opted-in guild is already claimed - walks
# that partial index whole and finds nothing: 681 buffers, 1.3 ms. All three are
# trivial at this size; the point of the index is that only the first is
# proportional to the FLEET.
#
# The presence test alone is not the opt-in test: `settings ? key` is TRUE for a
# JSON *null* value, so `{"serverstats_digest_channel": null}` would read as
# opted in and be claimed - and warned about - every week for ever. The `off`
# path here DELETES the key so the bot cannot produce that row, but the dashboard
# writes this same key and "null it out to disable" is the obvious mistake on
# that side, so the ``->> ... IS NOT NULL`` line treats a null value as OFF.
# ``->>`` yields SQL NULL for a JSON null, which is exactly the test. The
# partial index still applies: its predicate is the untouched ``?`` line above,
# and the null check is a filter on top of it.
#
# The key is spelled as a LITERAL three times rather than interpolated from
# KEY_DIGEST_CHANNEL: it has to be a constant for the planner to match the
# partial index predicate. test_serverstats_digest greps the same literal out of
# this statement AND out of schema.sql, so the two cannot drift apart silently.
#
# ORDER BY guild_id makes a tick deterministic (and rides the same index). Said
# honestly: it is also the order the ceiling in cog.run_digest_once bites in - if
# a week's 168 ticks ever ran out of slots, the SAME low-id guilds would be
# served every week and the tail would never be. That is a reason to raise the
# limit before reaching the ceiling, not a reason to shuffle: a random order
# would make WHICH guilds are missing unpredictable instead of fixing anything.
#
# The value is returned as TEXT and coerced in Python (tools.snowflake.coerce_id):
# the dashboard writes this same key, and JavaScript serialises a snowflake as a
# string, so nothing here may assume the JSON type.
CANDIDATES = """
    SELECT gs.guild_id,
           gs.settings ->> 'serverstats_digest_channel' AS channel_id
      FROM guild_settings gs
      LEFT JOIN serverstats_digest_state st
             ON st.guild_id = gs.guild_id AND st.last_iso_week = $1
     WHERE gs.settings ? 'serverstats_digest_channel'
       AND gs.settings ->> 'serverstats_digest_channel' IS NOT NULL
       AND st.guild_id IS NULL
     ORDER BY gs.guild_id
     LIMIT $2;
    """

# The claim. ONE statement, atomic, and the row it returns is the permission to
# send: the ``WHERE`` on the conflict path is what makes a second attempt in the
# same week return NOTHING, whether the second attempt comes from a concurrent
# tick, a restart mid-fan-out, or a duplicated loop.
#
# INSERT ... ON CONFLICT rather than a bare UPDATE because a guild that has
# never received a digest has no row to update: the insert path IS the first
# week's claim, and it is subject to the same uniqueness (the primary key), so
# two concurrent inserts cannot both win.
#
# PROBED in a rolled-back transaction: first week -> guild_id, same week again ->
# None, following week -> guild_id, a fresh guild's first claim -> guild_id, its
# immediate repeat -> None.
CLAIM = """
    INSERT INTO serverstats_digest_state (guild_id, last_iso_week)
    VALUES ($1, $2)
    ON CONFLICT (guild_id) DO UPDATE
       SET last_iso_week = EXCLUDED.last_iso_week
     WHERE serverstats_digest_state.last_iso_week <> EXCLUDED.last_iso_week
    RETURNING guild_id;
    """

# Turning the digest off DELETES the key (see rule 3 in the module docstring).
# ``tools.settings`` can patch one key but not remove one, so this is the same
# out-of-band statement cogs/config/tickets/guild_config._CLEAR_KEY_SQL uses,
# followed by the same cache invalidation. Scoped by key: the sibling features
# sharing this blob are untouched.
CLEAR_KEY = (
    "UPDATE guild_settings SET settings = settings - $2::text WHERE guild_id = $1"
)


# ---------------------------------------------------------------------------
# Pure window maths
# ---------------------------------------------------------------------------


def digest_period(today):
    """``(monday, sunday)`` of the last FULL ISO week before ``today``.

    ``today`` is a UTC date (rollups.today_utc). The window ends on the Sunday
    that closes the week BEFORE the one ``today`` belongs to, which is
    yesterday when ``today`` is a Monday and earlier on any other day - so the
    day in progress is never inside it, on any day the loop could possibly run.

    Delivery normally happens on the Monday that follows the window, so in
    practice this is "the week that ended yesterday". The formula is written for
    any day because the loop is allowed to run on any day (rule 5 of the module
    docstring): a digest delivered late must report exactly the week the Monday
    one would have.
    """
    end = today - datetime.timedelta(days=today.weekday() + 1)
    return end - datetime.timedelta(days=6), end


def previous_period(start):
    """The ``(monday, sunday)`` of the week before the one starting ``start``."""
    return start - datetime.timedelta(days=7), start - datetime.timedelta(days=1)


def period_key(day):
    """The ISO week key ('W2026-31') of ``day``.

    The SAME helper xp_period keys its rows with and rollups' retention block
    reads, so the digest's period label, the leveling actives read and the
    delivery state row all name a week the same way.
    """
    return iso_week_period_key(day)


def missing_permissions(permissions, required=DIGEST_PERMISSIONS):
    """Names in ``required`` that ``permissions`` does not grant, in order.

    A permission the object does not carry counts as MISSING - the safe
    direction, and the one that surfaces a discord.py rename instead of silently
    skipping a check. Pure (mirrors tickets/preflight.missing_permissions).
    """
    if permissions is None:
        return list(required)
    return [name for name in required if not getattr(permissions, name, False)]


def describe_permissions(names):
    """Render permission attribute names as a translated, comma-joined list.

    The label is bound to a LOCAL before ``_()`` sees it: pybabel reads ``_(...)``
    calls literally and a subscript inside one is the shape it refuses.
    """
    parts = []
    for name in names:
        label = _PERMISSION_LABELS.get(name)
        parts.append(_(label) if label else name)
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# The value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DigestReport:
    """One week of a guild, as the digest states it.

    ``observed_days`` is the number of days of the period that carry a
    ``server_stats_days`` row, i.e. the days the collector was actually
    watching. ZERO of them means the bot was not looking (fresh install, long
    downtime) and the digest is NOT posted - see :attr:`has_data`.

    ``delta_pct`` is ``None`` unless BOTH weeks were fully observed (7 days
    each) and the previous week carries messages: comparing a week we watched
    for three days against a full one is a made-up number, and 0% would be a
    lie about a week we did not see. Same rule, same reason as
    rollups.Overview.delta_pct.

    ``busiest_day`` is ``None`` when no observed day carries a message (a week
    of real silence has no "most active day" to name), and ``active_members`` is
    ``None`` both for a guild without leveling and for a leveling guild whose
    week has no xp_period row at all - the read cannot tell "nobody earned XP"
    from "the week aged out of the leveling prune"
    (cogs.community.leveling.engine.PRUNE_PERIODS_BACK), so it says neither.

    ``chart_points`` (U3) is the reported week as :class:`~.charts.ChartPoint`
    tuples, one per day, oldest first - built by :func:`shape_digest` from the
    SAME ``activity_rows``/``growth_rows`` this dataclass's other fields sum,
    so the chart and the numbers next to it can never disagree about which
    days are holes. ``chart_previous_points`` is the week before it, for the
    ghosted week-over-week overlay, and is ``None`` unless BOTH weeks were
    fully observed - the exact same gate :attr:`delta_pct` uses, because a
    ghost drawn through holes is not an honest comparison, it is a guess with
    a line through it.
    """

    week: str
    period_start: datetime.date
    period_end: datetime.date
    observed_days: int
    previous_observed_days: int
    messages: int
    previous_messages: int
    delta_pct: float | None
    joins: int
    leaves: int
    busiest_day: datetime.date | None
    busiest_messages: int
    active_members: int | None
    chart_points: tuple = ()
    chart_previous_points: tuple | None = None

    @property
    def net(self):
        return self.joins - self.leaves

    @property
    def has_data(self):
        """False when no day of the period was observed - do not post."""
        return self.observed_days > 0

    @property
    def partial(self):
        """True when the collector missed at least one day of the period."""
        return self.observed_days < DAYS_PER_WEEK


# ---------------------------------------------------------------------------
# Pure shaping
# ---------------------------------------------------------------------------


def shape_digest(activity_rows, growth_rows, active_members, today):
    """Turn two window reads into a :class:`DigestReport`. Pure.

    ``activity_rows`` (rollups.ACTIVITY_SERIES) and ``growth_rows``
    (rollups.GROWTH) both cover the FOURTEEN days ending on the period's Sunday:
    the reported week and the one before it, which is what lets a single pass
    produce the totals, the observed-day counts of both weeks and the
    week-over-week delta.

    The growth rows ARE the watched-days set: ``server_stats_days`` holds one row
    per guild per observed UTC day, so a day absent from them is a day nobody
    was counting, and its messages - if the message table somehow holds any -
    are left out of every total rather than folded in as if the day had been
    watched. That is the same set rollups.shape_activity calls ``watched_days``.
    """
    start, end = digest_period(today)
    previous_start, previous_end = previous_period(start)

    observed = {row["day"] for row in growth_rows or ()}
    messages_by_day = {
        row["day"]: int(row["messages"] or 0) for row in activity_rows or ()
    }

    def _total(window_start, window_end):
        return sum(
            messages_by_day.get(day, 0)
            for day in rollups.day_span(window_start, window_end)
            if day in observed
        )

    observed_days = sum(
        1 for day in rollups.day_span(start, end) if day in observed
    )
    previous_observed = sum(
        1 for day in rollups.day_span(previous_start, previous_end) if day in observed
    )
    messages = _total(start, end)
    previous_messages = _total(previous_start, previous_end)

    delta = None
    if (
        observed_days == DAYS_PER_WEEK
        and previous_observed == DAYS_PER_WEEK
        and previous_messages > 0
    ):
        delta = round((messages - previous_messages) * 100.0 / previous_messages, 1)

    joins = 0
    leaves = 0
    for row in growth_rows or ():
        if start <= row["day"] <= end:
            joins += int(row["joins"] or 0)
            leaves += int(row["leaves"] or 0)

    busiest_day = None
    busiest = 0
    for day in rollups.day_span(start, end):
        if day not in observed:
            continue
        count = messages_by_day.get(day, 0)
        if count > busiest:
            busiest = count
            busiest_day = day

    # U3: the chart's raw points, built from the exact same two dicts
    # (messages_by_day, observed) the totals above sum - a day outside
    # `observed` is a HOLE here too (charts.ChartPoint(messages=None,
    # net=None)), never a zero. growth_by_day carries the same rows
    # `observed` was built from, so a day IN `observed` always has a row.
    growth_by_day = {row["day"]: row for row in growth_rows or ()}

    def _chart_points(window_start, window_end):
        result = []
        for day in rollups.day_span(window_start, window_end):
            if day not in observed:
                result.append(charts.ChartPoint(day=day, messages=None, net=None))
                continue
            row = growth_by_day.get(day)
            net = (
                int(row["joins"] or 0) - int(row["leaves"] or 0)
                if row is not None
                else None
            )
            result.append(
                charts.ChartPoint(
                    day=day, messages=messages_by_day.get(day, 0), net=net
                )
            )
        return tuple(result)

    chart_points = _chart_points(start, end)
    chart_previous_points = None
    if observed_days == DAYS_PER_WEEK and previous_observed == DAYS_PER_WEEK:
        # The SAME gate delta_pct uses (see the DigestReport docstring): a
        # ghost is only honest when the whole period it covers was watched.
        chart_previous_points = _chart_points(previous_start, previous_end)

    return DigestReport(
        week=period_key(start),
        period_start=start,
        period_end=end,
        observed_days=observed_days,
        previous_observed_days=previous_observed,
        messages=messages,
        previous_messages=previous_messages,
        delta_pct=delta,
        joins=joins,
        leaves=leaves,
        busiest_day=busiest_day,
        busiest_messages=busiest,
        active_members=active_members,
        chart_points=chart_points,
        chart_previous_points=chart_previous_points,
    )


def _signed(value):
    """``+3`` / ``-2`` / ``+0`` - a plain signed integer (views._signed)."""
    return f"+{value}" if value >= 0 else str(value)


def render(report, guild_name, chart_filename=None):
    """The digest as ONE compact embed. Pure (no I/O, no Discord call).

    A rich embed rather than a Components V2 container on purpose: this message
    is a broadcast nobody clicks - it carries no control, it must survive in a
    channel for weeks, and an embed is what the permission preflight
    (``embed_links``) is stated in terms of.

    ``chart_filename`` (U3) is the attachment name of the PNG chart cog.py
    rendered for THIS call, or ``None`` when no chart was rendered (never
    attempted, timed out, or raised - see CHART_RENDER_TIMEOUT and
    cog.py's _deliver_digest). Still pure either way: this function never
    renders anything itself, it only points the embed's image at whatever
    attachment the caller says it is sending alongside it.
    """
    embed = discord.Embed(
        title="\N{BAR CHART} " + _("Weekly server digest"),
        description=_("Here is what happened on {guild} last week.").format(
            guild=guild_name
        ),
        colour=random_colour(),
    )

    lines = [_("{total} messages").format(total=report.messages)]
    if report.delta_pct is None:
        # Never a silent 0%: with an incomplete week on either side there is no
        # honest comparison to draw (see DigestReport.delta_pct).
        lines.append(_("No comparable full week before this one."))
    else:
        sign = "+" if report.delta_pct >= 0 else ""
        lines.append(
            _("{sign}{pct:.1f}% vs the week before").format(
                sign=sign, pct=report.delta_pct
            )
        )
    embed.add_field(name=_("Messages"), value="\n".join(lines), inline=False)

    embed.add_field(
        name=_("Members"),
        value=_("{joins} joins / {leaves} leaves ({net} net, humans only)").format(
            joins=report.joins, leaves=report.leaves, net=_signed(report.net)
        ),
        inline=False,
    )

    if report.busiest_day is not None:
        embed.add_field(
            name=_("Most active day"),
            value=_("{day} - {messages} messages").format(
                day=report.busiest_day.isoformat(), messages=report.busiest_messages
            ),
            inline=False,
        )

    if report.active_members is not None:
        embed.add_field(
            name=_("Active members"),
            value=_("{count} members earned XP this week").format(
                count=report.active_members
            ),
            inline=False,
        )

    if report.partial:
        # The window is stated in the footer either way; this line is the
        # qualifier that keeps the totals above from reading as a full week.
        embed.add_field(
            name=_("Heads up"),
            value=_("Only {observed} of {days} days were observed.").format(
                observed=report.observed_days, days=DAYS_PER_WEEK
            ),
            inline=False,
        )

    embed.set_footer(
        text=_("Week {week}: {start} to {end} (complete UTC days).").format(
            week=report.week,
            start=report.period_start.isoformat(),
            end=report.period_end.isoformat(),
        )
    )
    if chart_filename:
        embed.set_image(url=f"attachment://{chart_filename}")
    return embed


# ---------------------------------------------------------------------------
# Reads and writes
# ---------------------------------------------------------------------------


async def collect(pool, guild_id, today, leveling=False):
    """Everything one digest needs: TWO queries, THREE on a leveling guild.

    Both queries are the rollups statements, over the 14 days ending on the
    period's Sunday - a guild-prefixed range read on each table's primary key
    (the plans ST2 measured; 14 days is a third of the window the card already
    reads). The third, for a leveling guild, is the distinct-actives COUNT of
    the reported week alone - never a list of members.

    Deliberately NOT read: rollups.data_since. The observed-day set the growth
    rows already carry answers the same question more precisely (it sees a
    downtime day, which a collection-start date cannot), so a fourth query would
    buy nothing.
    """
    start, end = digest_period(today)
    previous_start, _previous_end = previous_period(start)
    activity_rows = await pool.fetch(
        rollups.ACTIVITY_SERIES, guild_id, previous_start, end
    )
    growth_rows = await pool.fetch(rollups.GROWTH, guild_id, previous_start, end)
    active_members = None
    if leveling:
        key = period_key(start)
        rows = await pool.fetch(rollups.RETENTION_ACTIVITY, guild_id, key, key)
        if rows:
            active_members = int(rows[0]["active_members"] or 0)
    return shape_digest(activity_rows, growth_rows, active_members, today)


async def candidates(pool, iso_week, limit=FAN_OUT_LIMIT):
    """The guilds this tick may deliver to - ONE indexed query (:data:`CANDIDATES`)."""
    return await pool.fetch(CANDIDATES, iso_week, limit)


async def claim(pool, guild_id, iso_week):
    """Claim this guild's digest for ``iso_week``; True when WE won it.

    Called BEFORE anything is sent, so a crash between the claim and the post
    costs at most one guild one week's digest - the direction that cannot spam a
    server with duplicates, which is the one failure a weekly broadcast must not
    have.
    """
    return await pool.fetchval(CLAIM, guild_id, iso_week) is not None


async def set_channel(pool, guild_id, channel_id):
    """Turn the digest ON for a guild (writes through tools.settings)."""
    await settings.set_guild(pool, guild_id, KEY_DIGEST_CHANNEL, channel_id)


async def clear_channel(pool, guild_id):
    """Turn the digest OFF by DELETING the key, then evict the cached blob.

    Deleting rather than storing a neutral value is what makes "off" and "never
    configured" the same row; the eviction is the out-of-band write-then-
    invalidate pair (tools.privacy.set_avatar_tracking, tickets' set_key), needed
    because this statement bypasses tools.settings.
    """
    await pool.execute(CLEAR_KEY, guild_id, KEY_DIGEST_CHANNEL)
    settings.invalidate_guild(guild_id)

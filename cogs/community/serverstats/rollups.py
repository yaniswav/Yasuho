"""Purpose: the READ layer of the server statistics - one indexed query per
question, plus the pure shaping that turns raw rows into an honest series.

ST1 (buffer.py / cog.py / queries.py) writes the aggregates; this module only
reads them. Nothing here mutates, caches or holds module state: every call is a
fresh round trip, so the card built on top (ST3) owns any caching it wants.

Two rules shape every function below.

1. HONEST WINDOWS. A guild collected for 3 days must be told "3 days", never an
   average diluted over 7. Each read therefore returns how many days of data the
   window ACTUALLY covers, and a delta is published only when both the current
   and the previous window are complete - comparing a partial window against a
   full one is a made-up number. The same rule applies to the day in progress:
   the COMPARISON read (overview) covers complete UTC days only and stops at
   yesterday, because a window holding three hours of today against a full
   previous window swings the headline delta from deeply negative at 00:00 to
   zero at 23:59 without a single message changing. The SERIES reads (growth,
   activity) do keep today - a curve is read as a curve, its last point is
   visibly the day in progress, and nothing is divided by it.
2. NO MEMBER CACHE, EVER. The bot runs with ``chunk_guilds_at_startup=False``,
   so ``guild.members`` is a partial, arbitrary subset. No statistic here may be
   derived from it. Member counts come from the daily ``member_count`` snapshot,
   and the closest thing to a per-user signal is ``xp_period`` (leveling guilds
   only), read as COUNT DISTINCT aggregates - never as a user list.

Shaping is separated from I/O on purpose: every ``shape_*`` function is pure and
takes plain rows, so the whole honesty story (deltas, partial windows, holes in
a series, the leveling flag) is unit tested without a database.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from . import buffer
from tools.leveling import iso_week_period_key

# Nothing older than the collector's own retention can be asked for (see
# cog.RETENTION_DAYS - duplicated as a literal rather than imported so this pure
# read module never drags discord.py in through cog.py).
MAX_WINDOW_DAYS = 90

# Default windows: the card offers 7 or 30 days for the overview, 30 for the
# series, 8 weeks for the retention block.
DEFAULT_OVERVIEW_DAYS = 7
DEFAULT_SERIES_DAYS = 30
DEFAULT_RETENTION_WEEKS = 8

# Hard ceilings so a caller can never turn a read into an unbounded one.
MAX_TOP_CHANNELS = 25
MAX_RETENTION_WEEKS = 12

ONE_DAY = datetime.timedelta(days=1)


# ---------------------------------------------------------------------------
# SQL. One statement per question (the activity series pairs its read with the
# watched-days read that makes it honest, and retention runs a second one ONLY
# when the guild has leveling). Every one of them is a guild-prefixed range read:
#
#   server_stats_messages PK (guild_id, channel_id, day) - a bitmap index scan
#   applies BOTH the guild equality and the day range inside the index, and the
#   guild's whole range is bounded anyway (its channels x 90 days), exactly as
#   schema.sql adjudicated. MEASURED on a 3.6M-row fixture (1000 guilds x 40
#   channels x 90 days, the design's target scale): every window read below
#   plans as `Bitmap Index Scan on server_stats_messages_pkey` and runs in
#   0.3-0.8 ms. NO NEW INDEX - a (guild_id, day) index shaved ~0.2 ms off one
#   query and would tax the 5-minute flush upsert forever.
#
#   server_stats_days PK (guild_id, day) - guild AND day are both index bounds,
#   so these reads touch exactly the window's rows.
#
#   xp_period index (guild_id, period_key, xp DESC) - guild plus a period_key
#   range, so the weekly-actives read is a pure range scan too.
# ---------------------------------------------------------------------------

# Both windows in ONE pass: the aggregate FILTERs split the scanned rows into
# "current window" and "everything before it (the previous window)", and MIN(day)
# reports the oldest day actually present in the scanned range - that is what
# makes the window honest without a second query.
#
# $4 is the LAST COMPLETE day (yesterday), never today: see overview_bounds. The
# day in progress is deliberately outside the scan, so the two sides of the
# comparison always hold the same amount of elapsed time.
OVERVIEW = """
    SELECT
        COALESCE(SUM(messages) FILTER (WHERE day >= $2), 0)::bigint
            AS current_messages,
        COALESCE(SUM(messages) FILTER (WHERE day < $2), 0)::bigint
            AS previous_messages,
        MIN(day) AS first_day
    FROM server_stats_messages
    WHERE guild_id = $1 AND day >= $3 AND day <= $4;
    """

# Names are NOT resolved here: the card turns ids into channels (and silently
# drops the ones it cannot see). HAVING keeps a channel that only holds zeroes
# out of the ranking, and the channel_id tie-break makes the order deterministic.
TOP_CHANNELS = """
    SELECT channel_id, SUM(messages)::bigint AS messages
    FROM server_stats_messages
    WHERE guild_id = $1 AND day >= $2 AND day <= $3
    GROUP BY channel_id
    HAVING SUM(messages) > 0
    ORDER BY messages DESC, channel_id
    LIMIT $4;
    """

# One row per guild-day, already the shape the series wants. Days with no row
# come back absent and are marked as holes by the shaping - never as zeroes.
GROWTH = """
    SELECT day, member_count, joins, leaves
    FROM server_stats_days
    WHERE guild_id = $1 AND day >= $2 AND day <= $3
    ORDER BY day;
    """

ACTIVITY_SERIES = """
    SELECT day, SUM(messages)::bigint AS messages
    FROM server_stats_messages
    WHERE guild_id = $1 AND day >= $2 AND day <= $3
    GROUP BY day
    ORDER BY day;
    """

# The days the collector was WATCHING, which is a different question from "the
# days that carry messages". The daily snapshot writes one server_stats_days row
# per guild per UTC day, so the presence of a row is the proof we were up and
# counting: a day inside the collection era with a row and no message row really
# is zero messages, a day with NO row is a hole (bot down, host restarted) and
# must not be averaged as a zero. Index-only scan on the PK (guild_id, day),
# bounded to the window - at most 90 tiny rows. MEASURED on a 90k-row fixture
# (1000 guilds x 90 days, the design's target scale): `Index Only Scan using
# server_stats_days_pkey`, 4 buffers, 0.054 ms for a 30-day window.
WATCHED_DAYS = """
    SELECT day
    FROM server_stats_days
    WHERE guild_id = $1 AND day >= $2 AND day <= $3
    ORDER BY day;
    """

# Weekly net movement. to_char(day, '"W"IYYY-IW') produces EXACTLY the key
# tools.leveling.iso_week_period_key builds in Python ('W2026-31'), verified
# against a real PostgreSQL including the Dec/Jan ISO boundary, so the two
# halves of the retention block line up week for week.
RETENTION_NET = """
    SELECT to_char(day, '"W"IYYY-IW') AS week_key,
           SUM(joins)::bigint  AS joins,
           SUM(leaves)::bigint AS leaves
    FROM server_stats_days
    WHERE guild_id = $1 AND day >= $2 AND day <= $3
    GROUP BY week_key
    ORDER BY week_key;
    """

# The only user-level signal the bot owns, and only for leveling guilds: how
# many DISTINCT members earned XP in a given week. Read as a count, never as a
# list of ids. Weekly keys sort lexically in chronological order (both fields
# zero padded), so a plain BETWEEN is a correct - and index-bounded - range.
# The LIKE guard is belt and braces: monthly keys start with 'M' and therefore
# already sort below every 'W' key.
RETENTION_ACTIVITY = """
    SELECT period_key AS week_key,
           COUNT(DISTINCT user_id)::bigint AS active_members
    FROM xp_period
    WHERE guild_id = $1
      AND period_key >= $2 AND period_key <= $3
      AND period_key LIKE 'W%'
    GROUP BY period_key
    ORDER BY period_key;
    """

# server_stats_days ONLY, and that is a measured decision, not a shortcut.
#
# WHY IT IS THE RIGHT TABLE: the collector's daily snapshot writes a row for
# EVERY guild on the first flush of every UTC day (cog._maybe_snapshot_members),
# so this table's oldest guild row IS the day collection started for that guild.
#
# WHY NOT ALSO server_stats_messages: MIN(day) there cannot use the PK (day is
# the third column behind channel_id), so PostgreSQL rewrites it into an ORDERED
# scan of the GLOBAL `day` index, walking every guild's rows from the oldest day
# forward until it meets one of ours. Measured on a 3.6M-row fixture (1000
# guilds): 6.6 ms for a guild with 90 days of history, and unbounded-in-guilds
# for a guild that started TODAY (it must walk the whole index first). Here the
# PK (guild_id, day) answers in an index-only scan, 3 buffers, 0.03 ms.
DATA_SINCE = """
    SELECT MIN(day) AS first_day
    FROM server_stats_days
    WHERE guild_id = $1;
    """


# ---------------------------------------------------------------------------
# Value objects (frozen: a read result is a snapshot, not a mutable model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Overview:
    """Message totals for a window, next to the window before it.

    COMPLETE DAYS ONLY. The window ends at ``end_day`` - yesterday, the last
    fully elapsed UTC day - and so does the window before it. Including the day
    in progress would make the headline delta a function of the clock: a guild
    with perfectly flat traffic would read -14% just after 00:00 UTC and 0% just
    before midnight, on both windows' own terms. The series reads keep today;
    this one, the only COMPARISON, does not.

    ``days_available`` is the honest count: the number of complete UTC days
    between the first day carrying data and ``end_day``, clamped to the window.
    A guild whose collection covers 3 of them reports 3, and ``average_per_day``
    divides by 3 - never by the nominal window length.

    ``first_day`` is the oldest day WITH MESSAGES inside the scanned range (the
    window plus the window before it), not the guild's all-time first day - the
    read deliberately stops at the previous window's start, and that is exactly
    the horizon the two window flags need. When the caller passes ``since`` (the
    collection start from :func:`data_since`, which it should), that value WINS
    and is what lands here: ``first_day`` is then the guild's true horizon, and
    the difference matters for a silent guild - see :func:`shape_overview`. For
    a "collecting since ..." label, ask :func:`data_since` directly.
    """

    days: int
    end_day: datetime.date
    total_messages: int
    days_available: int
    average_per_day: float
    previous_messages: int
    previous_days_available: int
    delta_pct: float | None
    first_day: datetime.date | None

    @property
    def partial(self):
        """True when the window reaches further back than the data does."""
        return self.days_available < self.days

    @property
    def comparable(self):
        """True when a delta could honestly be computed (see shape_overview)."""
        return self.delta_pct is not None


@dataclass(frozen=True)
class ChannelCount:
    """One row of the channel ranking; the card resolves the name."""

    channel_id: int
    messages: int


@dataclass(frozen=True)
class GrowthPoint:
    """One day of the growth series. ``has_data`` False means NO SNAPSHOT was
    written that day (bot down, day predating collection): joins/leaves are
    ``None``, never 0 - "we do not know" and "nothing happened" are different
    facts and the card must be able to tell them apart."""

    day: datetime.date
    member_count: int | None
    joins: int | None
    leaves: int | None
    has_data: bool

    @property
    def net(self):
        if not self.has_data:
            return None
        return (self.joins or 0) - (self.leaves or 0)


@dataclass(frozen=True)
class Growth:
    days: int
    points: tuple
    total_joins: int
    total_leaves: int
    net: int
    days_with_data: int
    member_count_first: int | None
    member_count_last: int | None

    @property
    def watched_days(self):
        """The days of this window the collector actually saw (has a snapshot).

        Hand it to :func:`shape_activity` when both series cover the same window:
        it is exactly the set that tells a silent day from a day the bot was down,
        and passing it saves the extra read :func:`activity_series` would issue.
        """
        return frozenset(point.day for point in self.points if point.has_data)

    @property
    def member_delta(self):
        """Snapshot-to-snapshot member movement, or None without two snapshots."""
        if self.member_count_first is None or self.member_count_last is None:
            return None
        return self.member_count_last - self.member_count_first


@dataclass(frozen=True)
class ActivityPoint:
    """One day of the message series. A day the collector was WATCHING with no
    message row really is zero messages; a day before collection started - or a
    day the bot was down, which the guild-day snapshot proves by its absence -
    is unknown, and ``has_data`` False says so, exactly like
    :class:`GrowthPoint` does for the same day."""

    day: datetime.date
    messages: int | None
    has_data: bool


@dataclass(frozen=True)
class ActivitySeries:
    days: int
    points: tuple
    total_messages: int
    days_with_data: int
    peak_day: datetime.date | None
    peak_messages: int


@dataclass(frozen=True)
class RetentionWeek:
    """One week of the retention block.

    ``has_data`` False means the week is entirely OUTSIDE the collection era -
    the guild had not installed the bot yet (or the week predates the snapshot
    table). Its ``joins``/``leaves``/``net`` are then ``None``, never 0: a
    freshly installed guild would otherwise show six weeks of a perfectly flat
    "net +0" that it never lived, which is the same lie the activity series
    refuses to tell for a day with no snapshot. Inside the era a week with no
    row IS a quiet week (the guild-day table gets a row every day), so 0 is the
    right answer there.

    ``active_members`` is None when the guild has no leveling, or when xp_period
    no longer holds that week (its rows are pruned a few periods back - see
    tools.leveling.PRUNE_PERIODS_BACK)."""

    week: str
    joins: int | None
    leaves: int | None
    active_members: int | None
    has_data: bool = True

    @property
    def net(self):
        if not self.has_data:
            return None
        return (self.joins or 0) - (self.leaves or 0)


@dataclass(frozen=True)
class RetentionReport:
    """Aggregates only, by design: weekly net movement always, plus weekly
    distinct-active-members when the guild runs leveling. ``has_activity_data``
    tells the card whether to render that second half or hide it."""

    weeks: tuple
    leveling: bool
    has_activity_data: bool


# ---------------------------------------------------------------------------
# Pure helpers: windows, clamps, day maths
# ---------------------------------------------------------------------------


def today_utc():
    """Today's UTC day as a ``date``, derived the SAME way the collectors derive
    the day they write (buffer.utc_day), so a window can never be off by one
    against the rows it reads."""
    return buffer.day_to_date(buffer.utc_day())


def clamp_days(days, default=DEFAULT_SERIES_DAYS):
    """Force a window into 1..MAX_WINDOW_DAYS (the collector's retention)."""
    try:
        value = int(days)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_WINDOW_DAYS))


def clamp_limit(limit, maximum=MAX_TOP_CHANNELS, default=10):
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def clamp_weeks(weeks, default=DEFAULT_RETENTION_WEEKS):
    try:
        value = int(weeks)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_RETENTION_WEEKS))


def window_bounds(today, days):
    """``(window_start, previous_start)`` for a ``days``-long window ending today.

    The window INCLUDES today, so a 7-day window is today and the six days
    before it; the previous window is the seven days before that.
    """
    start = today - datetime.timedelta(days=days - 1)
    return start, start - datetime.timedelta(days=days)


def overview_bounds(today, days):
    """``(start, previous_start, end)`` for the overview's COMPARISON windows.

    Unlike :func:`window_bounds`, this one STOPS AT YESTERDAY: ``end`` is
    ``today - 1``, the last fully elapsed UTC day, and the two windows are the
    ``days`` complete days ending there plus the ``days`` complete days before
    those. The day in progress is in neither, so a guild with flat traffic reads
    a 0% delta at 03:00 UTC exactly as it does at 23:00 - the sawtooth a partial
    current day would print on the card's headline number cannot happen.

    The series windows keep today (see :func:`window_bounds`): the last point of
    a curve is allowed to be the day in progress, nothing is divided by it.
    """
    end = today - ONE_DAY
    start = end - datetime.timedelta(days=days - 1)
    return start, start - datetime.timedelta(days=days), end


def day_span(start, end):
    """Every date from ``start`` to ``end`` inclusive (empty when start > end)."""
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += ONE_DAY
    return days


def week_keys(today, weeks):
    """The ``weeks`` most recent ISO week keys, oldest first ('W2026-31' shape).

    Built with tools.leveling.iso_week_period_key so the keys are byte-identical
    to the ones xp_period stores AND to what the SQL to_char produces.
    """
    return [
        iso_week_period_key(today - datetime.timedelta(weeks=offset))
        for offset in range(weeks - 1, -1, -1)
    ]


def week_window_start(today, weeks):
    """The Monday that opens the oldest of the ``weeks`` most recent ISO weeks."""
    monday = today - datetime.timedelta(days=today.weekday())
    return monday - datetime.timedelta(weeks=weeks - 1)


def week_end(key):
    """The Sunday closing the ISO week named by ``key`` ('W2026-31' shape).

    The exact inverse of tools.leveling.iso_week_period_key, via
    ``date.fromisocalendar``, so a week key can be compared against a collection
    start date without re-deriving ISO arithmetic by hand.
    """
    year, week = key[1:].split("-")
    return datetime.date.fromisocalendar(int(year), int(week), 7)


def _available_days(first_day, start, end):
    """How many days of the ``start..end`` window the data can actually cover."""
    if first_day is None or end < start:
        return 0
    effective = max(first_day, start)
    if effective > end:
        return 0
    return (end - effective).days + 1


# ---------------------------------------------------------------------------
# Pure shaping
# ---------------------------------------------------------------------------


def shape_overview(row, today, days, since=None):
    """Turn the OVERVIEW row into an honest :class:`Overview`.

    Both windows are runs of COMPLETE days ending yesterday (see
    :func:`overview_bounds`); ``today`` is passed in only to derive that end, and
    the day in progress never lands in either total or in ``days_available``.

    A delta is published ONLY when both windows are complete. Two partial
    windows (or a full one against a partial one) would compare different
    amounts of time and produce a percentage that means nothing, so the delta is
    left as ``None`` and the card says "not enough history" instead of lying.

    ``since`` (the guild's collection start, from :func:`data_since`) WINS over
    the oldest message row when it is given, and the card should pass it. Message
    rows alone cannot tell "installed 3 days ago" from "nobody has spoken in two
    weeks": both look like an empty scan. With ``since`` the silent guild gets
    its full window and an honest average of 0.0 instead of "not enough
    history".
    """
    days = clamp_days(days, DEFAULT_OVERVIEW_DAYS)
    start, previous_start, end = overview_bounds(today, days)
    first_day = since if since is not None else (
        row["first_day"] if row is not None else None
    )
    current = int(row["current_messages"] or 0) if row is not None else 0
    previous = int(row["previous_messages"] or 0) if row is not None else 0

    available = _available_days(first_day, start, end)
    previous_available = _available_days(
        first_day, previous_start, start - ONE_DAY
    )
    average = round(current / available, 1) if available else 0.0

    delta = None
    if available == days and previous_available == days and previous > 0:
        delta = round((current - previous) * 100.0 / previous, 1)

    return Overview(
        days=days,
        end_day=end,
        total_messages=current,
        days_available=available,
        average_per_day=average,
        previous_messages=previous,
        previous_days_available=previous_available,
        delta_pct=delta,
        first_day=first_day,
    )


def shape_top_channels(rows, limit=10):
    """Ranked channel counts, already ordered by the query; the limit is
    re-applied here so the shaping alone is enough to trust the output."""
    limit = clamp_limit(limit)
    ranked = [
        ChannelCount(int(row["channel_id"]), int(row["messages"] or 0))
        for row in rows or ()
        if int(row["messages"] or 0) > 0
    ]
    return ranked[:limit]


def shape_growth(rows, today, days):
    """Fill the window day by day, marking the days that have NO snapshot.

    Totals only ever sum days that exist, so a week with two missing snapshots
    reports the joins it saw, not zeroes it invented.
    """
    days = clamp_days(days)
    start, _previous_start = window_bounds(today, days)
    by_day = {row["day"]: row for row in rows or ()}

    points = []
    total_joins = 0
    total_leaves = 0
    days_with_data = 0
    first_count = None
    last_count = None
    for day in day_span(start, today):
        row = by_day.get(day)
        if row is None:
            points.append(GrowthPoint(day, None, None, None, False))
            continue
        joins = int(row["joins"] or 0)
        leaves = int(row["leaves"] or 0)
        member_count = row["member_count"]
        member_count = None if member_count is None else int(member_count)
        total_joins += joins
        total_leaves += leaves
        days_with_data += 1
        if member_count is not None:
            if first_count is None:
                first_count = member_count
            last_count = member_count
        points.append(GrowthPoint(day, member_count, joins, leaves, True))

    return Growth(
        days=days,
        points=tuple(points),
        total_joins=total_joins,
        total_leaves=total_leaves,
        net=total_joins - total_leaves,
        days_with_data=days_with_data,
        member_count_first=first_count,
        member_count_last=last_count,
    )


def shape_activity(rows, today, days, since=None, watched_days=None):
    """Fill the window day by day, distinguishing a silent day from a blind one.

    ``watched_days`` is the set of days that carry a guild-day snapshot (from
    WATCHED_DAYS, or straight off :attr:`Growth.watched_days`). It is the ONLY
    thing that can tell "nobody spoke" from "the bot was not running": both look
    like an absent message row. When it is given, a day outside it is a hole -
    ``has_data`` False, ``messages`` None, kept out of the total, the peak and
    ``days_with_data`` - which puts this series in exact agreement with
    :func:`shape_growth` over the same window, hole for hole.

    Without it the shaping falls back to the era test: days at or after ``since``
    (the guild's first day of data, itself defaulting to the oldest day present
    in ``rows``) count as real zeroes, earlier days are unknown. That fallback
    cannot see a downtime day, which is why :func:`activity_series` reads the
    watched days rather than relying on it.
    """
    days = clamp_days(days)
    start, _previous_start = window_bounds(today, days)
    by_day = {row["day"]: int(row["messages"] or 0) for row in rows or ()}
    if since is None:
        since = min(by_day) if by_day else None
    watched = None if watched_days is None else frozenset(watched_days)

    points = []
    total = 0
    days_with_data = 0
    peak_day = None
    peak = 0
    for day in day_span(start, today):
        if watched is not None:
            known = day in watched
        else:
            known = since is not None and day >= since
        if not known:
            points.append(ActivityPoint(day, None, False))
            continue
        messages = by_day.get(day, 0)
        total += messages
        days_with_data += 1
        if messages > peak:
            peak = messages
            peak_day = day
        points.append(ActivityPoint(day, messages, True))

    return ActivitySeries(
        days=days,
        points=tuple(points),
        total_messages=total,
        days_with_data=days_with_data,
        peak_day=peak_day,
        peak_messages=peak,
    )


def shape_retention(net_rows, activity_rows, keys, leveling=False, since=None):
    """Weekly net movement, plus weekly distinct actives when leveling is on.

    ``since`` (the collection start, from :func:`data_since`) marks the weeks
    that ended BEFORE the bot ever looked as unknown rather than as calm: a
    guild installed ten days ago must not print six weeks of "net +0" it never
    lived. Pass it - without it every requested week is assumed to have been
    watched, which is only true for a guild older than the window.

    ``has_activity_data`` is True only when leveling is on AND at least one week
    actually carries a count: xp_period is pruned a few periods back, so the
    older half of an 8-week window is normally empty even on a leveling guild.
    The card uses the flag to label or hide that half - it never renders a hole
    as a zero.
    """
    by_week_net = {row["week_key"]: row for row in net_rows or ()}
    by_week_active = {}
    if leveling:
        by_week_active = {
            row["week_key"]: int(row["active_members"] or 0)
            for row in activity_rows or ()
        }

    weeks = []
    for key in keys:
        row = by_week_net.get(key)
        # A week that ENDED before collection started was never observed. A week
        # straddling that date was partly observed, which the row itself already
        # reflects, so it stays real data.
        watched = since is None or week_end(key) >= since
        joins = leaves = None
        if watched:
            joins = int(row["joins"] or 0) if row is not None else 0
            leaves = int(row["leaves"] or 0) if row is not None else 0
        weeks.append(
            RetentionWeek(
                week=key,
                joins=joins,
                leaves=leaves,
                active_members=by_week_active.get(key),
                has_data=watched,
            )
        )

    return RetentionReport(
        weeks=tuple(weeks),
        leveling=bool(leveling),
        has_activity_data=any(week.active_members is not None for week in weeks),
    )


# ---------------------------------------------------------------------------
# Reads: pool in, value object out. One statement each, except the two noted
# above (activity series, leveling retention), which take exactly two.
# ---------------------------------------------------------------------------


async def overview(
    pool, guild_id, days=DEFAULT_OVERVIEW_DAYS, today=None, since=None
):
    """Total messages, honest daily average and the delta against the window
    before it. ONE query, bounded to twice the window.

    COMPLETE DAYS ONLY: the scan stops at yesterday (:func:`overview_bounds`), so
    the comparison never puts a few hours of today against a full day. The day in
    progress belongs to the series reads, not to this one.

    Pass ``since`` (from :func:`data_since`, which the card asks once for the
    whole page) so a guild that is merely SILENT is not reported as a guild
    without history - see :func:`shape_overview`.
    """
    days = clamp_days(days, DEFAULT_OVERVIEW_DAYS)
    today = today or today_utc()
    start, previous_start, end = overview_bounds(today, days)
    row = await pool.fetchrow(OVERVIEW, guild_id, start, previous_start, end)
    return shape_overview(row, today, days, since)


async def top_channels(
    pool, guild_id, days=DEFAULT_OVERVIEW_DAYS, limit=10, today=None
):
    """The busiest channels of the window, ids only - ONE query.

    A RANKING, not a comparison: the window includes today, because the day in
    progress can only add messages to a channel already in the running order and
    nothing here is divided by an elapsed-time denominator.
    """
    days = clamp_days(days, DEFAULT_OVERVIEW_DAYS)
    limit = clamp_limit(limit)
    today = today or today_utc()
    start, _previous_start = window_bounds(today, days)
    rows = await pool.fetch(TOP_CHANNELS, guild_id, start, today, limit)
    return shape_top_channels(rows, limit)


async def growth(pool, guild_id, days=DEFAULT_SERIES_DAYS, today=None):
    """Daily member count / joins / leaves, holes marked - ONE query."""
    days = clamp_days(days)
    today = today or today_utc()
    start, _previous_start = window_bounds(today, days)
    rows = await pool.fetch(GROWTH, guild_id, start, today)
    return shape_growth(rows, today, days)


async def activity_series(
    pool, guild_id, days=DEFAULT_SERIES_DAYS, today=None, since=None,
    watched_days=None,
):
    """Daily message totals across every channel - TWO indexed queries.

    The second one (WATCHED_DAYS) is the price of honesty: the message table
    alone cannot tell a quiet Sunday from a day the bot was down, and rendering
    downtime as a confident zero would drag the curve - and any average taken
    off it - toward a number nobody produced. It is an index-only scan of at most
    90 tiny rows on the same guild-prefixed PK the growth read uses.

    A caller that already holds a :class:`Growth` for the SAME window passes
    ``growth.watched_days`` here and pays one query instead of two.
    """
    days = clamp_days(days)
    today = today or today_utc()
    start, _previous_start = window_bounds(today, days)
    rows = await pool.fetch(ACTIVITY_SERIES, guild_id, start, today)
    if watched_days is None:
        watched_days = [
            row["day"] for row in await pool.fetch(WATCHED_DAYS, guild_id, start, today)
        ]
    return shape_activity(rows, today, days, since, watched_days)


async def retention(
    pool,
    guild_id,
    weeks=DEFAULT_RETENTION_WEEKS,
    leveling=False,
    today=None,
    since=None,
):
    """Weekly net movement over ``weeks`` weeks, plus weekly distinct active
    members when the guild runs leveling.

    ONE query for a guild without leveling, TWO for a guild with it (the second
    is the only read in this module that touches user-level rows, and it reads
    them as a COUNT DISTINCT - no id ever leaves the database).

    Pass ``since`` (from :func:`data_since`, the same value the overview gets) so
    the weeks that predate the collection era come back as unknown instead of as
    a flat, invented zero - see :func:`shape_retention`.
    """
    weeks = clamp_weeks(weeks)
    today = today or today_utc()
    keys = week_keys(today, weeks)
    start = week_window_start(today, weeks)
    net_rows = await pool.fetch(RETENTION_NET, guild_id, start, today)
    activity_rows = ()
    if leveling:
        activity_rows = await pool.fetch(
            RETENTION_ACTIVITY, guild_id, keys[0], keys[-1]
        )
    return shape_retention(net_rows, activity_rows, keys, leveling, since)


async def data_since(pool, guild_id):
    """The first UTC day this guild has statistics for, or None - ONE query.

    Read from the guild-day table, whose daily snapshot marks the collection era
    (see DATA_SINCE for the measured reason the message table is not consulted).
    The card uses it to say "since <date>" instead of implying the window is
    full, and every read that has to tell "we saw nothing" from "we were not
    looking yet" takes it: :func:`overview`, :func:`activity_series` and
    :func:`retention`.
    """
    return await pool.fetchval(DATA_SINCE, guild_id)

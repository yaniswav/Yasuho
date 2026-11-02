"""Growth numbers for the ``?botstats`` Growth page (cogs/system/botstats.py).

Answers "does the bot grow or churn, and how long do servers stay?" from
``guild_events``, a log of anonymous join / leave events (schema.sql), plus an
in-memory distribution of the current guilds by join age that works from day one.

Honest stats: the log starts EMPTY on the day it ships, so every window is
qualified by where the history really starts. A window older than the history is
never presented as a true zero. Pure Python plus SQL text; the only Discord
object touched is a guild passed in by the event writers.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from tools.i18n import _

log = logging.getLogger(__name__)

# Events older than this are deleted by the daily botstats hook.
RETENTION_DAYS = 400
WINDOWS = (7, 30, 90)
WEEKS = 8
# A leave this soon after the join is read as a bad first impression.
EARLY_SECONDS = (86400, 7 * 86400)

INSERT = """
    INSERT INTO guild_events (kind, member_count, stayed_seconds)
    VALUES ($1, $2, $3)
    """

PRUNE = """
    WITH stale AS (
        DELETE FROM guild_events WHERE at < $1 RETURNING 1
    )
    SELECT count(*)::bigint AS rows FROM stale;
    """

# $1 is "now" (a parameter, never now(), so a page and its tests agree). Every
# window is counted in ONE pass over the last 90 days; ``since`` is the oldest
# row of the WHOLE table, outside the window, and says how much history exists.
# percentile_cont skips NULLs, so an unknown size or stay never counts as zero.
SUMMARY = """
    SELECT
      count(*) FILTER (WHERE kind = 'join'  AND at >= $1::timestamptz - interval '7 days')::bigint  AS joins_7,
      count(*) FILTER (WHERE kind = 'leave' AND at >= $1::timestamptz - interval '7 days')::bigint  AS leaves_7,
      count(*) FILTER (WHERE kind = 'join'  AND at >= $1::timestamptz - interval '30 days')::bigint AS joins_30,
      count(*) FILTER (WHERE kind = 'leave' AND at >= $1::timestamptz - interval '30 days')::bigint AS leaves_30,
      count(*) FILTER (WHERE kind = 'join')::bigint  AS joins_90,
      count(*) FILTER (WHERE kind = 'leave')::bigint AS leaves_90,
      count(*) FILTER (WHERE kind = 'leave' AND stayed_seconds IS NOT NULL)::bigint AS stay_known,
      count(*) FILTER (WHERE kind = 'leave' AND stayed_seconds <= $2)::bigint AS early_1,
      count(*) FILTER (WHERE kind = 'leave' AND stayed_seconds <= $3)::bigint AS early_2,
      percentile_cont(0.5) WITHIN GROUP (ORDER BY stayed_seconds)
          FILTER (WHERE kind = 'leave') AS median_stay,
      percentile_cont(0.5) WITHIN GROUP (ORDER BY member_count)
          FILTER (WHERE kind = 'join') AS join_members,
      percentile_cont(0.5) WITHIN GROUP (ORDER BY member_count)
          FILTER (WHERE kind = 'leave') AS leave_members,
      (SELECT MIN(at) FROM guild_events) AS since
    FROM guild_events
    WHERE at >= $1::timestamptz - interval '90 days'
    """

# Weeks are ISO weeks (Monday start) in UTC; $1 is the start of the oldest one.
WEEKLY = """
    SELECT date_trunc('week', at AT TIME ZONE 'UTC')::date AS week,
           count(*) FILTER (WHERE kind = 'join')::bigint  AS joins,
           count(*) FILTER (WHERE kind = 'leave')::bigint AS leaves
    FROM guild_events
    WHERE at >= $1
    GROUP BY 1
    ORDER BY 1
    """


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def prune_cutoff(today):
    """Midnight UTC, RETENTION_DAYS before the UTC day ``today`` (a date)."""
    day = today - datetime.timedelta(days=RETENTION_DAYS)
    return datetime.datetime.combine(day, datetime.time(), tzinfo=datetime.timezone.utc)


def week_starts(now, weeks=WEEKS):
    """The Mondays (dates) of the last ``weeks`` ISO weeks, oldest first."""
    today = now.astimezone(datetime.timezone.utc).date()
    monday = today - datetime.timedelta(days=today.weekday())
    return [monday - datetime.timedelta(days=7 * i) for i in range(weeks - 1, -1, -1)]


# ---------------------------------------------------------------------------
# Writers (called from cogs/system/events.py)
# ---------------------------------------------------------------------------
def stay_seconds(guild, now=None):
    """Seconds the bot has been in ``guild``, or ``None`` when unknowable."""
    try:
        joined = guild.me.joined_at
    except Exception:
        return None
    if joined is None:
        return None
    if joined.tzinfo is None:
        joined = joined.replace(tzinfo=datetime.timezone.utc)
    return max(0, int(((now or utc_now()) - joined).total_seconds()))


async def record_guild_event(pool, guild, kind, *, now=None):
    """Log one anonymous join / leave. Never raises: it is a statistic.

    Stores only the kind, the member count (``None`` when Discord gave none,
    never 0) and, on a leave, how long the bot had been there. No id, no name.
    """
    try:
        members = getattr(guild, "member_count", None)
        members = members if isinstance(members, int) and members > 0 else None
        stayed = stay_seconds(guild, now) if kind == "leave" else None
        await pool.execute(INSERT, kind, members, stayed)
    except Exception:
        log.exception("botstats: could not record a guild %s event", kind)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class GrowthStats:
    """``windows`` maps days -> (joins, leaves); ``since`` is the oldest event."""

    as_of: datetime.datetime
    since: datetime.datetime | None
    windows: dict
    weekly: dict
    stay_known: int
    early: tuple
    median_stay: float | None
    join_members: float | None
    leave_members: float | None

    @property
    def covered_days(self):
        if self.since is None:
            return 0
        return max((self.as_of.date() - self.since.date()).days + 1, 1)

    def covers(self, days):
        """Does the history reach back ``days`` days, i.e. is a zero a real zero?"""
        return self.since is not None and self.since <= self.as_of - datetime.timedelta(days=days)


async def fetch_growth(pool, *, timeout, now=None):
    now = now or utc_now()
    row = await pool.fetchrow(SUMMARY, now, *EARLY_SECONDS, timeout=timeout)
    start = datetime.datetime.combine(
        week_starts(now)[0], datetime.time(), tzinfo=datetime.timezone.utc
    )
    weekly = await pool.fetch(WEEKLY, start, timeout=timeout)
    return GrowthStats(
        as_of=now,
        since=row["since"],
        windows={
            days: (row["joins_%d" % days], row["leaves_%d" % days]) for days in WINDOWS
        },
        weekly={w["week"]: (w["joins"], w["leaves"]) for w in weekly},
        stay_known=row["stay_known"],
        early=(row["early_1"], row["early_2"]),
        median_stay=row["median_stay"],
        join_members=row["join_members"],
        leave_members=row["leave_members"],
    )


def join_age_counts(guilds, now=None):
    """``(7d, 30d, 90d, older, unknown)`` disjoint counts from each guild's own
    join date (``guild.me.joined_at``): free, and valid before the log has data."""
    now = now or utc_now()
    counts = [0, 0, 0, 0, 0]
    for guild in guilds:
        age = stay_seconds(guild, now)
        if age is None:
            counts[4] += 1
            continue
        days = age / 86400
        counts[0 if days <= 7 else 1 if days <= 30 else 2 if days <= 90 else 3] += 1
    return tuple(counts)


# ---------------------------------------------------------------------------
# Rendering - [(heading, lines)] from plain data only
# ---------------------------------------------------------------------------
def _percent(count, total):
    return "{:.0f}%".format(count * 100 / total) if total else "-"


def _members(value):
    return "{:,}".format(round(value)) if value is not None else "?"


def render_windows(stats):
    if stats is None:
        return [_("Join and leave figures are unavailable right now.")]
    if stats.since is None:
        return [
            _("No server join or leave has been recorded yet."),
            "-# " + _("The log starts with the first one after this feature shipped."),
        ]
    lines = []
    for days in WINDOWS:
        joins, leaves = stats.windows[days]
        line = _("{days} days: {joins} joined, {leaves} left, net {net}").format(
            days=days, joins=joins, leaves=leaves, net="{:+d}".format(joins - leaves)
        )
        if not stats.covers(days):
            line += " - " + _("history starts {date}").format(date=stats.since.date().isoformat())
        lines.append(line)
    return lines


def render_weeks(stats):
    if stats is None or stats.since is None:
        return [_("No weekly figures yet.")]
    lines = []
    for monday in week_starts(stats.as_of):
        if monday + datetime.timedelta(days=7) <= stats.since.date():
            continue  # the whole week predates the history: not a zero, just absent
        joins, leaves = stats.weekly.get(monday, (0, 0))
        lines.append(
            _("Week of {date}: {joins} joined, {leaves} left").format(
                date=monday.isoformat(), joins=joins, leaves=leaves
            )
        )
    lines.append("-# " + _("ISO weeks in UTC; the last one is not over yet."))
    return lines


def render_stay(stats, *, fmt_duration):
    if stats is None:
        return [_("Stay figures are unavailable right now.")]
    if stats.since is None:
        return [_("No server join or leave has been recorded yet.")]
    if not stats.stay_known:
        lines = [_("No server that left in the last 90 days has a known stay.")]
    else:
        early_day, early_week = stats.early
        lines = [
            _("Median stay of servers that left: {duration}").format(
                duration=fmt_duration(stats.median_stay)
            ),
            _("Left within 24 h: {count} of {total} ({percent})").format(
                count=early_day, total=stats.stay_known, percent=_percent(early_day, stats.stay_known)
            ),
            _("Left within 7 days: {count} of {total} ({percent})").format(
                count=early_week, total=stats.stay_known, percent=_percent(early_week, stats.stay_known)
            ),
        ]
    lines.append(
        _("Median size: {joined} members when joining, {left} when leaving").format(
            joined=_members(stats.join_members), left=_members(stats.leave_members)
        )
    )
    if not stats.covers(90):
        lines.append(
            "-# "
            + _(
                "Only {days} day(s) of history: long stays cannot show up yet, so "
                "these figures lean towards early leavers."
            ).format(days=stats.covered_days)
        )
    return lines


def render_join_age(counts):
    labels = (
        _("Joined in the last 7 days"),
        _("Joined 8 to 30 days ago"),
        _("Joined 31 to 90 days ago"),
        _("Joined more than 90 days ago"),
        _("Join date unknown"),
    )
    lines = [
        _("{label}: {count}").format(label=label, count=count)
        for label, count in zip(labels, counts)
    ]
    lines.append(
        "-# "
        + _("From each server's own join date; a rejoin counts from the latest one.")
    )
    return lines


def render_growth(stats, join_ages, *, fmt_duration):
    """The Growth page: ``[(heading, lines)]``. ``stats`` is ``None`` on a failed read."""
    return [
        (_("Servers gained and lost"), render_windows(stats)),
        (_("Week by week"), render_weeks(stats)),
        (_("First impressions (last 90 days)"), render_stay(stats, fmt_duration=fmt_duration)),
        (_("Servers still here, by join date"), render_join_age(join_ages)),
    ]

"""Purpose: the durable half of the ?botstats usage counters - a bounded
in-memory buffer, the single additive upsert that flushes it, and the bounded
reads the Usage page asks for its day / week / month windows.

The in-memory counters in cogs/system/botstats.py answer "what is THIS process
being asked to do" and reset on every restart (which, on this deployment, means
every deploy). This module is the other half: the same completions are also
counted per (UTC day, command) and written to ``command_usage`` every few
minutes, so the dashboard can answer "what was used today / this week / this
month" across restarts.

Shape borrowed VERBATIM from cogs/community/serverstats (buffer.py + queries.py),
because that shape is what makes the guarantees hold:

* the increment is synchronous, O(1) and awaits nothing, so it is safe on the
  completion listener of every command in the bot;
* the buffer is CAPPED, so a wedged flush can never grow the process;
* the flush DRAINS BEFORE it awaits, and hands the drained generation back on
  ANY failure - including a cancellation (see BotStats._write_usage);
* the upsert is ADDITIVE, so a day can be written by any number of ticks and a
  retried batch is a re-add rather than an overwrite.

Semantics are AT-LEAST-ONCE, stated honestly: a cancellation landing after the
upsert committed but before the flush returns gives the same generation back to
the buffer, so a batch can be counted twice. These are usage counters, not
money, and the alternative (dropping the batch on every DB blip) loses real data
far more often.

PRIVACY: the only things this module can hold are a DATE, a command name defined
in this repository's own source, and integers. No user id, no guild id, no
channel id, no argument the user typed - by construction, not by convention.

Nothing here imports discord: every rule that matters (which day a count belongs
to, what a drain returns, what the SQL asks for) is testable as plain data.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

# Hard ceiling on distinct (day, command) keys held between two flushes. The
# realistic peak is "every command this bot has, used inside one 5-minute
# window", i.e. a few hundred - even straddling midnight UTC that is two days'
# worth. 4096 is an order of magnitude above it, so legitimate load never reaches
# the cap; only a wedged flush retaining generation after generation does, and
# then keys are DROPPED and counted instead of growing the process.
USAGE_KEY_CAP = 4096

# Defensive width cap on a stored command name. Names come from this repo's own
# source (the longest is well under 40 characters), so this never truncates a
# real one; it exists so that a future dynamically-named command cannot widen
# every row of the table nor the card that renders them.
COMMAND_NAME_LIMIT = 64

# The two windows the Usage page reports next to "today". Calendar UTC days,
# INCLUSIVE of today (see the SQL below).
WEEK_DAYS = 7
MONTH_DAYS = 30

# How many commands the persisted ranking lists.
TOP_LIMIT = 10

# How long a day of usage is kept. Long enough to compare a month against the
# month before it, short enough that the table stays a few tens of thousands of
# rows for ever.
RETENTION_DAYS = 400

# The prune deletes at most PRUNE_BATCH_SIZE rows per statement and runs at most
# PRUNE_MAX_BATCHES statements per day. Steady state is ONE short batch (a day's
# expiry is a few hundred rows); the ceiling only matters the first time the
# prune ever runs on a long-lived install.
PRUNE_BATCH_SIZE = 2000
PRUNE_MAX_BATCHES = 10


def utc_today(now=None):
    """The current UTC calendar day as a ``datetime.date``.

    Called on the increment path, so the day a count belongs to is decided WHEN
    THE COMMAND RAN, never when the flush happens: a flush at 00:02 must write
    the 23:59 completions onto the day they happened on, not migrate them into
    the new day. ``now`` is injectable for tests.

    A real ``date`` (rather than serverstats' "days since the epoch" int) is
    affordable here because this path is per-COMMAND, not per-MESSAGE: a few
    completions per second at the very worst, against a hot gateway path that
    sees every message on every guild.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).date()


@dataclass
class DrainedUsage:
    """One flush's worth of counters, detached from the live buffer.

    ``rows`` is a list of ``(day, command, prefix_count, slash_count)``. Each row
    carries its OWN day, so a flush that straddles midnight UTC writes each
    counter onto the day it was actually collected on.
    """

    rows: list = field(default_factory=list)
    dropped: int = 0

    @property
    def is_empty(self):
        return not self.rows


class UsageBuffer:
    """Bounded in-memory command counters keyed by ``(UTC day, command)``.

    Every mutator is synchronous, O(1) and allocation-light (one tuple key and
    one two-slot list on first sight of a key, nothing afterwards): the
    completion listeners call these and must never await.
    """

    __slots__ = ("_counts", "_cap", "_dropped")

    def __init__(self, cap=USAGE_KEY_CAP):
        # (day, command) -> [prefix_count, slash_count]
        self._counts: dict[tuple[datetime.date, str], list[int]] = {}
        self._cap = cap
        self._dropped = 0

    # ------------------------------------------------------------------
    # Hot path (called from the listeners; never awaits)
    # ------------------------------------------------------------------
    def record(self, day, command, *, slash=False, count=1):
        """Count ``count`` completion(s). False == ignored or dropped at the cap."""
        if not command:
            return False
        key = (day, command[:COMMAND_NAME_LIMIT])
        counts = self._counts.get(key)
        if counts is None:
            if len(self._counts) >= self._cap:
                self._dropped += 1
                return False
            counts = [0, 0]
            self._counts[key] = counts
        counts[1 if slash else 0] += count
        return True

    # ------------------------------------------------------------------
    # Flush path
    # ------------------------------------------------------------------
    @property
    def is_empty(self):
        return not self._counts

    @property
    def key_count(self):
        """Live key count, for the instrumentation line in the flush log."""
        return len(self._counts)

    def drain(self):
        """Detach everything collected so far and reset the live counters.

        The buffer is cleared BEFORE the write so the listeners keep counting
        into a fresh generation while the flush is in flight; a failed write
        hands the result back via :meth:`restore`.
        """
        drained = DrainedUsage(
            rows=[
                (day, command, counts[0], counts[1])
                for (day, command), counts in self._counts.items()
            ],
            dropped=self._dropped,
        )
        self._counts = {}
        self._dropped = 0
        return drained

    def restore(self, drained):
        """Fold a failed flush's counters back in, still respecting the cap.

        A DB blip must not silently eat 5 minutes of counters, but it must not be
        able to grow the buffer without bound either: the restore goes through
        the same capped mutator, so at worst the newest keys are dropped and
        counted like any other overflow.

        The drain's OWN overflow tally is deliberately NOT folded back in. It was
        already reported by the flush that drained it, so re-adding it here would
        make a multi-tick outage report the same drops again on every retry - a
        drop RATE that is not happening.
        """
        for day, command, prefix_count, slash_count in drained.rows:
            dropped_before = self._dropped
            if prefix_count:
                self.record(day, command, slash=False, count=prefix_count)
            if slash_count:
                self.record(day, command, slash=True, count=slash_count)
            # ONE lost key must tally as ONE drop. A restored row with both
            # surfaces non-zero calls record() TWICE for the SAME key, so a
            # capped restore would otherwise report two drops for one key and
            # inflate the WARNING the next flush prints.
            if self._dropped > dropped_before:
                self._dropped = dropped_before + 1


def build_flush_payload(drained):
    """Turn a drain into the four parallel arrays the upsert unnests.

    Returns ``(days, commands, prefix_counts, slash_counts)``, each built in the
    SAME single pass so the four are aligned by construction.
    """
    days = []
    commands = []
    prefix_counts = []
    slash_counts = []
    for day, command, prefix_count, slash_count in drained.rows:
        days.append(day)
        commands.append(command)
        prefix_counts.append(prefix_count)
        slash_counts.append(slash_count)
    return days, commands, prefix_counts, slash_counts


# ---------------------------------------------------------------------------
# SQL - one statement per constant (asyncpg prepares exactly one per call)
# ---------------------------------------------------------------------------
# EVERY statement below takes the UTC day as a PARAMETER instead of reading
# CURRENT_DATE. That is deliberate: the rows are keyed by a day computed in
# Python from UTC (see utc_today), while CURRENT_DATE is the DATABASE SESSION's
# calendar day - equal only as long as the server's TimeZone is UTC. Passing the
# day keeps the windows aligned with the data on a host that is not, and it makes
# every query below deterministic under test.

# One round trip per flush. unnest turns the parallel arrays
# (build_flush_payload) into rows, and the upsert ADDS its batch onto whatever
# the row already holds, so a day can be written by any number of ticks and a
# retried batch is a re-add rather than an overwrite.
#
# NOTE on ON CONFLICT: a single INSERT may not touch the same row twice ("cannot
# affect row a second time"). The batch comes straight from dict keys, so
# (day, command) is unique by construction - the buffer's dict IS the dedup.
#
# An empty batch is fine: unnest over empty arrays yields zero rows and the
# INSERT ... SELECT writes nothing.
FLUSH = """
    INSERT INTO command_usage (day, command, prefix_count, slash_count)
    SELECT day, command, prefix_count, slash_count
    FROM unnest($1::date[], $2::text[], $3::bigint[], $4::bigint[])
         AS batch(day, command, prefix_count, slash_count)
    ON CONFLICT (day, command)
    DO UPDATE SET prefix_count = command_usage.prefix_count + EXCLUDED.prefix_count,
                  slash_count  = command_usage.slash_count  + EXCLUDED.slash_count;
    """

# The three window totals in ONE statement, plus the honesty figure that goes
# with them.
#
# Each window is INCLUSIVE of today and spans exactly N calendar days, i.e.
# ``$1 - (N - 1) .. $1``. Plain ``$1 - N`` would sum N + 1 days under a heading
# that says N; the house convention is
# cogs/community/serverstats/rollups.window_bounds, which this matches.
#
# ``since`` is MIN(day) over the WHOLE table, deliberately outside the WHERE: it
# is what tells the renderer that a 30-day heading is only covering 6 days of
# collected history yet, and a MIN scoped to the window could never say that. It
# is an Index Only Scan on the primary key, not a second pass over the data.
#
# The COUNT(DISTINCT day) figures are the same honesty device as
# botstats.OBSERVED_MESSAGES' "observed day(s)": a day with no row is a day
# NOBODY WAS COUNTING (the bot was down, or this table did not exist yet), never
# a day on which zero commands were run. ``since`` alone cannot say that - it
# only catches a short history at the START of collection, so a gap AFTER
# collection began (a bot down 20 of the last 30 days) would print an
# unqualified 30-day sum. These count the days the sum above actually covers,
# per window, so every multi-day total is rendered next to its own coverage.
#
# The upper bound (``day <= $1``) is not decoration either: it keeps the widest
# window and the WHERE clause describing the same set of rows, so a row dated in
# the future (a clock jump on a restore) cannot inflate the month total.
WINDOWS = """
    SELECT COALESCE(SUM(prefix_count + slash_count)
                    FILTER (WHERE day = $1::date), 0)::bigint AS today,
           COALESCE(SUM(prefix_count + slash_count)
                    FILTER (WHERE day >= $1::date - ($2::int - 1)), 0)::bigint AS week,
           COALESCE(SUM(prefix_count + slash_count), 0)::bigint AS month,
           (COUNT(DISTINCT day)
            FILTER (WHERE day >= $1::date - ($2::int - 1)))::bigint AS week_recorded,
           (COUNT(DISTINCT day))::bigint AS month_recorded,
           (SELECT MIN(day) FROM command_usage) AS since
    FROM command_usage
    WHERE day >= $1::date - ($3::int - 1) AND day <= $1::date
    """

# The persisted ranking over the middle window. Ties break on the NAME so the
# same data always renders in the same order (the in-memory ranking in
# botstats.UsageCounters.top obeys the same rule for the same reason).
TOP = """
    SELECT command,
           SUM(prefix_count + slash_count)::bigint AS total
    FROM command_usage
    WHERE day >= $1::date - ($2::int - 1) AND day <= $1::date
    GROUP BY command
    ORDER BY total DESC, command ASC
    LIMIT $3::int
    """

# Bounded retention prune, same shape as the serverstats prune
# (cogs/community/serverstats/queries.py). The LIMITed ctid sub-select runs as an
# InitPlan, so the number of rows SELECTED for deletion - and therefore deleted,
# and therefore locked - is capped at $2 whatever the state of the table; the
# caller repeats the statement until a short batch comes back.
#
# Measured plans differ by server version, and the comment says what was actually
# seen rather than a generic claim: PostgreSQL 14+ turns `ctid = ANY(ARRAY(...))`
# into a Tid Scan (that is where the serverstats measurement comes from), while
# PostgreSQL 11 plans the outer half as a filtered scan. That is fine HERE and
# would not have been there: this table holds at most a few hundred rows per day
# for RETENTION_DAYS days, i.e. tens of thousands total, once a day.
PRUNE = """
    WITH stale AS (
        DELETE FROM command_usage
        WHERE ctid = ANY(ARRAY(
            SELECT ctid FROM command_usage WHERE day < $1::date LIMIT $2
        ))
        RETURNING 1
    )
    SELECT count(*)::bigint AS rows FROM stale;
    """


# ---------------------------------------------------------------------------
# Reads - value object + the bounded queries that fill it
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PersistedUsage:
    """The persisted half of the Usage page, with its own coverage baked in.

    ``as_of`` is the UTC day every window ends on - the SAME day the queries were
    given, so the renderer's "how much history do we actually have" arithmetic
    can never disagree with the sums it is annotating.

    ``since`` is the oldest day on record (``None`` when nothing has been written
    yet). A window whose length exceeds ``as_of - since`` is NOT full, and the
    renderer has to say so rather than let a 30-day heading imply 30 days of
    collection.

    ``week_recorded`` / ``month_recorded`` are how many days each window actually
    HAS a row for. They carry what ``since`` cannot: a hole in the middle of the
    history. A missing day is a day nobody was counting, so the renderer prints
    these next to the sums they qualify (same rule as the observed-activity block
    on the same page).
    """

    as_of: datetime.date
    today: int
    week: int
    month: int
    week_days: int
    month_days: int
    week_recorded: int
    month_recorded: int
    since: datetime.date | None
    top: tuple

    @property
    def covered_days(self):
        """Calendar days of history actually collected, inclusive; 0 if none.

        Clamped at 1 for a non-empty table: a row dated in the future (clock
        jump) would otherwise produce a negative "days collected".
        """
        if self.since is None:
            return 0
        return max((self.as_of - self.since).days + 1, 1)

    def window_is_full(self, days):
        """Has enough history been collected to fill a ``days``-long window?"""
        return self.covered_days >= days


async def fetch_persisted_usage(
    pool,
    *,
    timeout,
    today=None,
    week_days=WEEK_DAYS,
    month_days=MONTH_DAYS,
    limit=TOP_LIMIT,
):
    """Two bounded aggregates over ``command_usage``.

    ``timeout`` is required rather than defaulted: every read on this dashboard
    is bounded, and the bound belongs to the caller that owns the interaction
    budget (cogs/system/botstats.py:QUERY_TIMEOUT).
    """
    if today is None:
        today = utc_today()
    row = await pool.fetchrow(WINDOWS, today, week_days, month_days, timeout=timeout)
    ranking = await pool.fetch(TOP, today, week_days, limit, timeout=timeout)
    return PersistedUsage(
        as_of=today,
        today=int(row["today"]) if row else 0,
        week=int(row["week"]) if row else 0,
        month=int(row["month"]) if row else 0,
        week_days=week_days,
        month_days=month_days,
        week_recorded=int(row["week_recorded"]) if row else 0,
        month_recorded=int(row["month_recorded"]) if row else 0,
        since=row["since"] if row else None,
        top=tuple((entry["command"], int(entry["total"])) for entry in ranking),
    )

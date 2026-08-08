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

The SAME drain also feeds a second, much smaller table: ``command_usage_hourly``,
a 168-row (UTC weekday, UTC hour) profile of when this bot is actually used. It
is kept STRICTLY SEPARATE from the per-day table rather than derived from it,
because the per-day table has no hour dimension at all and never will (adding one
would multiply its cardinality by 24 for a question nobody asks of it). Its only
consumer is the quiet-hours block on ?botstats: "which slots of the week is a
restart cheapest in". See :data:`FLUSH` for how one statement writes both, and
:data:`DECAY_HOURLY` for why the profile FADES instead of accumulating for ever.

PRIVACY: the only things this module can hold are a DATE, an HOUR OF THE DAY, a
command name defined in this repository's own source, and integers. No user id,
no guild id, no channel id, no argument the user typed - by construction, not by
convention. The hourly profile is bot-wide and command-free: a slot count is the
number of commands the WHOLE fleet ran in that hour of the week, which cannot
describe a person.

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

# --- the hourly profile ----------------------------------------------------
# The shape of the rolling profile: 7 UTC weekdays x 24 UTC hours = 168 slots,
# and that is the WHOLE table, for ever, whatever the install.
DOW_COUNT = 7
HOURS_PER_DAY = 24
SLOTS_PER_WEEK = DOW_COUNT * HOURS_PER_DAY

# Every this many days, all 168 counts are HALVED (integer division, floor 0).
# The profile is otherwise purely additive, so without this a habit from a year
# ago would weigh exactly as much as last week's - and the whole point of the
# block is "where should I put a restart THIS week". Seven days is one full
# cycle of the very period being profiled.
#
# AT MOST ONCE PER RUN OF THE HOOK, not once per elapsed week: DECAY_HOURLY sets
# ``halved_on = today`` rather than ``halved_on + 7``, so a bot that was off for
# a month halves ONCE when it comes back, not four times. That is the deliberate
# choice (never halve data twice for one week that passed), and it is why the
# "a week of silence costs half its weight" reading holds only while the bot is
# actually running: across an outage the profile ages by one halving, whatever
# the outage's length.
HOURLY_HALVE_DAYS = 7

# How many days of hourly collection are needed before the block says anything.
# It is not a taste threshold: a slot with NO row means "no command ran then",
# which is only true once that slot has actually been LIVED THROUGH. Below one
# full week, some of the 168 slots have not happened yet, and calling them the
# quietest would be reporting the calendar rather than the traffic.
HOURLY_MIN_DAYS = 7

# How many slots each side of the profile lists (quietest / busiest).
QUIET_SLOT_LIMIT = 3


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


def utc_day_hour(now=None):
    """``(UTC calendar day, UTC hour of the day)`` for one instant.

    The pair is taken from ONE reading of the clock on purpose: calling
    :func:`utc_today` and then ``datetime.now().hour`` separately could straddle
    a midnight between the two calls and file a 23:59 completion as hour 0 of the
    day before. Like the day, the hour is captured WHEN THE COMMAND RAN, never
    when the flush happens.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    moment = moment.astimezone(datetime.timezone.utc)
    return moment.date(), moment.hour


def day_of_week(day):
    """Weekday index of a ``date``: 0 = Monday ... 6 = Sunday.

    This is ``date.weekday()``, i.e. the PYTHON convention, and it is the one
    stored in ``command_usage_hourly.dow``. Postgres' own ``EXTRACT(DOW)`` is
    Sunday = 0 and ``EXTRACT(ISODOW)`` is Monday = 1 - three conventions for the
    same seven days, which is exactly why nothing in this module ever asks the
    DATABASE what weekday a row belongs to: the value is computed here, in
    Python, from a day that was itself computed here in UTC.
    """
    return day.weekday()


@dataclass
class DrainedUsage:
    """One flush's worth of counters, detached from the live buffer.

    ``rows`` is a list of ``(day, command, prefix_count, slash_count)``. Each row
    carries its OWN day, so a flush that straddles midnight UTC writes each
    counter onto the day it was actually collected on.

    ``slots`` is the SAME generation seen through the other axis: a list of
    ``(dow, hour, count)`` for the rolling weekly profile, already aggregated
    over commands and surfaces (the profile has neither dimension). It is not a
    second buffer - it is drained, restored and written with ``rows``, in one
    generation and ONE statement, so a flush can never land on one table and not
    the other.

    That is an atomicity claim, not an equality one. The two tables can still
    drift, in exactly one direction and for exactly one reason: the per-day
    dict has a key cap and the slot dict does not (168 keys, by construction),
    so a restored generation can put its slots back while the daily rows it came
    with are refused at the cap - see :meth:`UsageBuffer.restore`. The profile
    then counts a completion the per-day table dropped. Whatever the daily side
    loses that way is already counted and reported as ``dropped``.
    """

    rows: list = field(default_factory=list)
    slots: list = field(default_factory=list)
    dropped: int = 0

    @property
    def is_empty(self):
        return not self.rows and not self.slots


class UsageBuffer:
    """Bounded in-memory command counters keyed by ``(UTC day, command)``.

    Every mutator is synchronous, O(1) and allocation-light (one tuple key and
    one two-slot list on first sight of a key, nothing afterwards): the
    completion listeners call these and must never await.

    ``_slots`` is the same generation aggregated by ``(dow, hour)`` for the
    rolling weekly profile. It is a SECOND DICT, not a second buffer and not a
    wider key, and both of those are deliberate:

    * a wider key (day, command, HOUR) would make ``(day, command)`` no longer
      unique in a drained batch, and the daily upsert's "the buffer's dict IS the
      dedup" invariant - the thing that keeps ON CONFLICT from failing with
      "cannot affect row a second time" - would have to move into SQL;
    * this dict cannot grow: it has at most :data:`SLOTS_PER_WEEK` (168) keys by
      construction, whatever traffic does, so it needs no cap of its own and the
      cap accounting above stays about the thing that can actually grow.
    """

    __slots__ = ("_counts", "_slots", "_cap", "_dropped")

    def __init__(self, cap=USAGE_KEY_CAP):
        # (day, command) -> [prefix_count, slash_count]
        self._counts: dict[tuple[datetime.date, str], list[int]] = {}
        # (dow, hour) -> count, bounded at 168 keys by construction
        self._slots: dict[tuple[int, int], int] = {}
        self._cap = cap
        self._dropped = 0

    # ------------------------------------------------------------------
    # Hot path (called from the listeners; never awaits)
    # ------------------------------------------------------------------
    def record(self, day, command, *, slash=False, count=1, hour=None):
        """Count ``count`` completion(s). False == ignored or dropped at the cap.

        ``hour`` is the UTC hour the completion happened in, captured by the
        caller at increment time (see :func:`utc_day_hour`). It is optional
        because the two axes fail independently: an hour outside 0..23 must not
        cost the per-day counter, which is the load-bearing one. A dropped
        per-day key drops its slot with it - one refusal, both axes - so nothing
        this method ACCEPTS is ever counted on one axis only.

        :meth:`restore` is the one path that does not go through here for both
        axes, and it says why.
        """
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
        if hour is not None and 0 <= hour < HOURS_PER_DAY:
            slot = (day_of_week(day), hour)
            self._slots[slot] = self._slots.get(slot, 0) + count
        return True

    # ------------------------------------------------------------------
    # Flush path
    # ------------------------------------------------------------------
    @property
    def is_empty(self):
        # BOTH dicts, mirroring DrainedUsage.is_empty. Slots are a subset of
        # rows by construction today, so the second test cannot currently change
        # the answer - it is here so that an hour-only path added later cannot
        # make a shutdown skip its final flush over a buffer that is not empty.
        return not self._counts and not self._slots

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
            slots=[
                (dow, hour, count)
                for (dow, hour), count in self._slots.items()
            ],
            dropped=self._dropped,
        )
        self._counts = {}
        self._slots = {}
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

        The slots are folded back by the same rule: they came from the same
        generation, and a write that did not happen must leave BOTH axes waiting
        for the next one. They go back DIRECTLY rather than through ``record``,
        because they are already aggregated (their key holds no day and no
        command) and because their dict cannot overflow - 168 keys, cap or no
        cap.

        WHICH IS THE ONE PLACE THE TWO AXES CAN PART COMPANY, and it is worth
        being exact about: the slots go back unconditionally, the rows go back
        through the capped mutator, so a restore that overflows keeps a hourly
        completion whose daily row was refused. It is the honest trade - the slot
        dict genuinely cannot overflow, so refusing its entries to stay in step
        with a cap that does not apply to them would throw away good data to
        preserve a symmetry nobody reads. The daily loss is what ``dropped``
        already reports; the profile's counts are shares, not totals, and are
        halved weekly anyway.
        """
        for dow, hour, count in drained.slots:
            slot = (dow, hour)
            self._slots[slot] = self._slots.get(slot, 0) + count
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
    """Turn a drain into the seven parallel arrays the upsert unnests.

    Returns ``(days, commands, prefix_counts, slash_counts, dows, hours,
    slot_counts)``. The first four are the per-day batch and the last three are
    the weekly-profile batch; each group is built in the SAME single pass over
    its own list, so within a group the arrays are aligned by construction. The
    two groups have DIFFERENT lengths on purpose - they are two aggregations of
    one generation, not two views of one list - and :data:`FLUSH` unnests them
    separately for exactly that reason.
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
    dows = []
    hours = []
    slot_counts = []
    for dow, hour, count in drained.slots:
        dows.append(dow)
        hours.append(hour)
        slot_counts.append(count)
    return days, commands, prefix_counts, slash_counts, dows, hours, slot_counts


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
#
# BOTH tables are written by this ONE statement, as a data-modifying CTE. That
# is the point: one round trip, one transaction, so a flush lands on both tables
# or on neither. Two statements would open a window where the first commits and
# the second does not, and the generation handed back by _write_usage would then
# re-add the half that already landed. (Atomicity, not equality: the per-day
# side can still lose rows to the buffer's key cap on a restore - see
# UsageBuffer.restore, which is the only place the two axes part company.)
# PostgreSQL guarantees a data-modifying WITH sub-statement is executed
# exactly once and to completion even when nothing reads its output, which is
# what lets the per-day upsert live in the CTE and the profile in the main query.
#
# The two unnests are INDEPENDENT and may have different lengths: the per-day
# batch has one row per (day, command), the profile batch one per (dow, hour).
# Each is unique by construction because each comes from its own dict's keys.
FLUSH = """
    WITH daily AS (
        INSERT INTO command_usage (day, command, prefix_count, slash_count)
        SELECT day, command, prefix_count, slash_count
        FROM unnest($1::date[], $2::text[], $3::bigint[], $4::bigint[])
             AS batch(day, command, prefix_count, slash_count)
        ON CONFLICT (day, command)
        DO UPDATE SET prefix_count = command_usage.prefix_count + EXCLUDED.prefix_count,
                      slash_count  = command_usage.slash_count  + EXCLUDED.slash_count
        RETURNING 1
    )
    INSERT INTO command_usage_hourly (dow, hour, count)
    SELECT dow, hour, count
    FROM unnest($5::smallint[], $6::smallint[], $7::bigint[])
         AS profile(dow, hour, count)
    ON CONFLICT (dow, hour)
    DO UPDATE SET count = command_usage_hourly.count + EXCLUDED.count;
    """

# The weekly profile's cadence marker, seeded once and then never rewritten by
# this statement. ``started_on`` is what makes the block on the dashboard honest:
# it says when the PROFILE began collecting, which on an existing install is far
# more recent than command_usage's own MIN(day) - reading coverage off the per-day
# table would claim months of hourly history on the day this table is created.
#
# ON CONFLICT DO NOTHING, so it is a no-op on every call but the first: it rides
# the same once-a-day hook as the prune and costs one index probe.
SEED_HOURLY_STATE = """
    INSERT INTO command_usage_hourly_state (id, started_on, halved_on)
    VALUES (1, $1::date, $1::date)
    ON CONFLICT (id) DO NOTHING;
    """

# The decay pass: halve all 168 counts, at most once every HOURLY_HALVE_DAYS.
#
# WHY DECAY AT ALL. The flush is additive, so without this the profile is a
# lifetime average and last week's habits can never move it. Halving weekly makes
# it an exponential moving average with a one-week half-life OF UPTIME: recent
# weeks dominate, old ones fade to nothing instead of being deleted at some
# arbitrary cliff. Integer division floors, so a 1 becomes 0 and a slot that
# stopped being used eventually leaves the profile entirely - which is the honest
# outcome. "Of uptime" because the marker below is set to TODAY and not to
# ``halved_on + 7``: several due weeks collapse into a single halving, so an
# outage ages the profile by one step however long it lasted.
#
# WHY THE MARKER IS IN THE DATABASE. The daily prune's cadence marker is IN
# MEMORY (botstats.BotStats._prune_day) and says so: a restart re-runs it, and
# re-deleting already-expired rows is harmless. Halving is NOT idempotent, and
# this deployment restarts on every deploy - an in-memory marker would halve the
# profile several times a day and collapse it to zeros within a week. So the
# cadence lives in a row, and the CLAIM is the same single-flight idiom the rest
# of the repo uses for "do this at most once": UPDATE ... WHERE <not done yet>
# RETURNING, whose row lock makes two callers racing impossible to double-apply.
#
# ONE statement, three sub-statements: the UPDATE that claims the week, the
# UPDATE that halves (gated on the claim having returned a row), and the SELECT
# that reports. ``halved`` reads ``due`` by name, which is a normal CTE read and
# well defined - what sub-statements cannot see is each other's effects on the
# TARGET TABLES, not each other's RETURNING output.
DECAY_HOURLY = """
    WITH due AS (
        UPDATE command_usage_hourly_state
           SET halved_on = $1::date
         WHERE id = 1 AND halved_on <= $1::date - $2::int
        RETURNING 1
    ), halved AS (
        UPDATE command_usage_hourly
           SET count = count / 2
         WHERE count > 0 AND EXISTS (SELECT 1 FROM due)
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM halved)::bigint AS rows
    """

# The whole profile, in one read. 168 rows is the ceiling, for ever, so this is
# an unfiltered scan of a table that cannot grow - no window, no LIMIT, nothing
# to bound. Slots with no row are returned by NOBODY and are materialised as
# zeros by the renderer, which is only legitimate once every slot has been lived
# through (see HOURLY_MIN_DAYS).
HOURLY_PROFILE = """
    SELECT dow, hour, count
    FROM command_usage_hourly
    ORDER BY dow, hour
    """

# The marker row, read for ``started_on`` alone: how long the profile has been
# collecting, which is what decides whether it may be shown at all. Absent (no
# row) means the flush loop has never completed a maintenance pass, i.e. nothing
# is being profiled yet - never "zero commands".
HOURLY_STATE = """
    SELECT started_on, halved_on
    FROM command_usage_hourly_state
    WHERE id = 1
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

    ``hourly`` is the weekly profile as ``((dow, hour, count), ...)`` and
    ``hourly_since`` the day it started collecting (``None`` when it has not).
    They travel with the rest rather than in their own read: they answer a
    question about the SAME table's traffic, on the same page, under the same
    memo - one open of the page, one batch of reads.
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
    hourly: tuple = ()
    hourly_since: datetime.date | None = None

    @property
    def hourly_covered_days(self):
        """Calendar days the HOURLY profile has been collecting, inclusive.

        Deliberately NOT :attr:`covered_days`: the per-day table can be a year
        old on the day the profile table is created, and using its history would
        publish a week-of-the-day pattern built from a few hours of data.
        """
        if self.hourly_since is None:
            return 0
        return max((self.as_of - self.hourly_since).days + 1, 1)

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
    # The weekly profile: 168 rows at most and a single marker row, both under
    # the same bound and the same memo as the windows above.
    profile = await pool.fetch(HOURLY_PROFILE, timeout=timeout)
    state = await pool.fetchrow(HOURLY_STATE, timeout=timeout)
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
        hourly=tuple(
            (int(slot["dow"]), int(slot["hour"]), int(slot["count"]))
            for slot in profile
        ),
        hourly_since=state["started_on"] if state else None,
    )


# ---------------------------------------------------------------------------
# The weekly profile - pure shaping over what the read returned
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HourSlot:
    """One (UTC weekday, UTC hour) slot of the rolling profile.

    ``share`` is the slot's fraction of every command in the profile, which is
    the only figure worth showing: the counts themselves are decayed (halved
    weekly), so their absolute value means nothing to a reader and everything to
    a comparison.
    """

    dow: int
    hour: int
    count: int
    share: float


@dataclass(frozen=True)
class HourRanking:
    """Both ends of one profile, plus the figure that decides how to say it.

    ``quiet_slots`` is how many of the :data:`SLOTS_PER_WEEK` slots recorded
    NOTHING at all, and it exists because naming three of them would be
    dishonest whenever there are more than three: the tie-break that picks which
    three (count, then weekday, then hour) is deterministic, which is not the
    same as meaningful - on a small install it always answers "Mon 00:00, Mon
    01:00, Mon 02:00", the earliest zeros, which reads as a finding when it is
    just the start of the week. The renderer counts them instead.
    """

    quietest: tuple
    busiest: tuple
    quiet_slots: int
    total_slots: int = SLOTS_PER_WEEK


def hourly_is_ready(covered_days, min_days=HOURLY_MIN_DAYS):
    """Whether every one of the 168 slots has been LIVED THROUGH yet.

    Its own function because two callers need the same answer for two different
    sentences: :func:`rank_hour_slots` refuses to rank below it, and the renderer
    has to tell "too young to say" apart from "old enough, and nothing ran" -
    which is the one distinction a bare ``None`` cannot carry.
    """
    return covered_days >= min_days


def rank_hour_slots(hourly, *, covered_days, limit=QUIET_SLOT_LIMIT,
                    min_days=HOURLY_MIN_DAYS):
    """A :class:`HourRanking` for the week, or ``None`` when unusable.

    ``None`` - which the renderer turns into an explicit refusal, never into a
    slot list - means one of two things, and both are the same honesty rule the
    rest of this dashboard obeys:

    * the profile has been collecting for fewer than ``min_days`` days, so some
      of the 168 slots have not HAPPENED yet and their emptiness describes the
      calendar rather than the traffic;
    * nothing has been recorded at all, which is not the same as a week that was
      uniformly quiet.

    The two are NOT interchangeable to a reader, so the renderer asks
    :func:`hourly_is_ready` first and only then reads this ``None`` as "no
    traffic". Returning one sentinel for two causes is what let an old-but-empty
    profile print "needs 7 day(s) of collection and has 232".

    Past that gate the full 168-slot grid IS materialised, missing rows included:
    once a slot has been lived through, no row for it means no command ran then,
    and that slot is exactly what the caller is looking for. Ordering is total -
    count, then weekday, then hour - so the same profile always renders the same
    way, and the quiet list is not reshuffled by dict order between two opens of
    the card.

    ``busiest`` holds only slots with traffic. A zero-count slot is never a
    "busiest" anything, and padding the list to ``limit`` with zeros produced the
    self-refuting "Busiest: Mon 04:00 (100.0%), Mon 00:00 (0.0%)" - reachable in
    steady state, not just at birth, because the weekly halving floors to 0 and a
    quiet install decays toward exactly that shape. It can therefore be SHORTER
    than ``limit``, but never empty: the gate above guarantees some traffic.
    """
    counts = {
        (int(dow), int(hour)): max(int(count), 0)
        for dow, hour, count in hourly
        if 0 <= int(dow) < DOW_COUNT and 0 <= int(hour) < HOURS_PER_DAY
    }
    total = sum(counts.values())
    if not hourly_is_ready(covered_days, min_days) or total <= 0:
        return None
    grid = [
        (counts.get((dow, hour), 0), dow, hour)
        for dow in range(DOW_COUNT)
        for hour in range(HOURS_PER_DAY)
    ]
    grid.sort()
    quietest = tuple(
        HourSlot(dow, hour, count, count / total)
        for count, dow, hour in grid[:limit]
    )
    loudest_first = sorted(
        (slot for slot in grid if slot[0] > 0),
        key=lambda slot: (-slot[0], slot[1], slot[2]),
    )
    busiest = tuple(
        HourSlot(dow, hour, count, count / total)
        for count, dow, hour in loudest_first[:limit]
    )
    return HourRanking(
        quietest=quietest,
        busiest=busiest,
        quiet_slots=sum(1 for count, _dow, _hour in grid if count == 0),
        total_slots=len(grid),
    )

"""Health signals for the ``?botstats`` Health page (cogs/system/botstats.py).

Answers "what is broken or degraded right now, and what failed recently?" from
two kinds of source, and keeps them apart on purpose:

* PERSISTED, memoised per card like every other DB read: the unexpected-error
  counters (``command_errors``), the overdue timers (``timers``) and the
  dashboard action queue (``dashboard_actions``);
* LIVE, recomputed on every open and free: the Lavalink node state, the AniList
  breaker, the DB pool, the gateway latency and counters.

The error counter is the one piece of new state, and it copies the persisted
command usage (cogs/system/usage_stats.py) rule for rule:

* the increment (:func:`note_error`) is synchronous, O(1), awaits nothing and
  touches no database, so it is safe inside the global error handler;
* the buffer is CAPPED (:data:`ERROR_KEY_CAP` distinct keys between two flushes)
  and counts what it drops, so a wedged flush can never grow the process;
* the ONLY writer is the existing periodic flush in botstats.py, an ADDITIVE
  upsert (:data:`FLUSH`), so the write rate is a function of time, not of traffic;
* a flush drains BEFORE it awaits and hands the generation back on any failure.

PRIVACY: the table holds a DATE, a command name defined in this repository's own
source, an exception CLASS name and an integer. No user id, no guild id, no
message, no argument, no exception text - by construction, not by convention.

Honest stats, same contract as the rest of the dashboard: a figure that could
not be read is UNKNOWN and renders as such, never as zero. Pure Python plus SQL
text; no Discord, no cog import, no pool reference beyond the ``fetch_*`` helpers.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import math

from tools.i18n import N_, _

log = logging.getLogger(__name__)

# Distinct (day, command, exception class) keys held between two flushes. The
# realistic peak is a handful (a bug in one command is ONE key however often it
# fires); 1024 is far above that, so only a wedged flush retaining generation
# after generation can reach it, and then keys are DROPPED and counted.
ERROR_KEY_CAP = 1024

# Defensive width caps on the two stored names (see usage_stats.COMMAND_NAME_LIMIT).
COMMAND_NAME_LIMIT = 64
ERROR_TYPE_LIMIT = 64

# How long a day of error counts is kept: enough for the 7-day window plus a
# comparison week, short enough that the table stays a few hundred rows.
RETENTION_DAYS = 30
ERROR_WINDOW_DAYS = 7
ERROR_TOP_LIMIT = 8

# A timer this far past its expiry is overdue: a healthy dispatcher fires within
# seconds, so five minutes is "it is not keeping up", not jitter.
TIMER_OVERDUE_SECONDS = 300
TIMER_TOP_LIMIT = 5

# Dashboard actions: statuses counted over 24 hours, failures ranked over 7 days,
# and a PENDING row older than this is stuck (the listener claims one in seconds).
ACTION_STATUS_HOURS = 24
ACTION_FAILED_DAYS = 7
ACTION_FAILED_LIMIT = 5
ACTION_STUCK_MINUTES = 10
ACTION_STATUSES = ("pending", "running", "done", "failed")

# Lines per list before the rest is summarised, so the card stays inside
# Discord's text budget.
NODE_LIMIT = 5
EXTENSION_LIMIT = 5

UNKNOWN_COMMAND = "?"


def utc_today(now=None):
    """The current UTC calendar day (injectable for tests)."""
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).date()


# ---------------------------------------------------------------------------
# The error buffer (hot path: sync, O(1), no await, no DB)
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class DrainedErrors:
    """One flush's worth of counters: ``rows`` is ``[(day, command, type, n)]``."""

    rows: list = dataclasses.field(default_factory=list)
    dropped: int = 0

    @property
    def is_empty(self):
        return not self.rows


class ErrorBuffer:
    """Bounded in-memory error counters keyed by ``(UTC day, command, class)``.

    ``seen`` is the since-boot total: every error offered to :meth:`record`,
    including the ones dropped at the cap, and never reset by a drain. It is the
    in-memory half of the page and says "this process", nothing more.
    """

    __slots__ = ("_counts", "_cap", "_dropped", "_seen", "_seen_day", "_seen_today")

    def __init__(self, cap=ERROR_KEY_CAP):
        self._counts: dict[tuple[datetime.date, str, str], int] = {}
        self._cap = cap
        self._dropped = 0
        self._seen = 0
        # Errors offered on the UTC day last seen, so the verdict can say
        # "today" without trusting a flush that may not have run yet.
        self._seen_day = None
        self._seen_today = 0

    def record(self, day, command, error_type, *, count=1):
        """Count ``count`` error(s). False == ignored or dropped at the cap."""
        if not command or not error_type:
            return False
        self._seen += count
        if self._seen_day != day:
            self._seen_day = day
            self._seen_today = 0
        self._seen_today += count
        key = (day, command[:COMMAND_NAME_LIMIT], error_type[:ERROR_TYPE_LIMIT])
        if key not in self._counts:
            if len(self._counts) >= self._cap:
                self._dropped += 1
                return False
            self._counts[key] = 0
        self._counts[key] += count
        return True

    @property
    def seen(self):
        return self._seen

    @property
    def dropped(self):
        """Keys refused at the cap since the last drain."""
        return self._dropped

    def seen_on(self, day):
        """Errors offered on ``day`` by this process (0 for any other day)."""
        return self._seen_today if self._seen_day == day else 0

    @property
    def is_empty(self):
        return not self._counts

    @property
    def key_count(self):
        return len(self._counts)

    def drain(self):
        """Detach everything collected so far; counting resumes into a fresh dict."""
        drained = DrainedErrors(
            rows=[
                (day, command, error_type, n)
                for (day, command, error_type), n in self._counts.items()
            ],
            dropped=self._dropped,
        )
        self._counts = {}
        self._dropped = 0
        return drained

    def restore(self, drained):
        """Fold a failed flush back in, still respecting the cap.

        Goes through the same capped insert (the newest keys are the ones
        refused). The drain's own overflow tally is NOT re-added: it was already
        reported by the flush that drained it. ``seen`` is untouched: restoring
        is not a new error.
        """
        for day, command, error_type, n in drained.rows:
            key = (day, command, error_type)
            if key not in self._counts:
                if len(self._counts) >= self._cap:
                    self._dropped += 1
                    continue
                self._counts[key] = 0
            self._counts[key] += n


def error_command_name(command):
    """The stored name for a context's command, or the placeholder."""
    return getattr(command, "qualified_name", None) or UNKNOWN_COMMAND


def note_error(ctx, error):
    """Count one UNEXPECTED command error. Called from the global error handler.

    Total on purpose: it runs inside ``on_command_error`` and a broken counter
    must never cost the user their error report, so EVERY failure (no BotStats
    cog, a cog mid-reload, a context without a bot, a buffer bug) is swallowed
    here - the ``ctx`` reads included. Synchronous and O(1): one dict lookup, no
    await, no database.
    """
    try:
        cog = ctx.bot.get_cog("BotStats")
        buffer = getattr(cog, "errors", None)
        if buffer is None:
            return False
        return buffer.record(
            utc_today(), error_command_name(ctx.command), type(error).__name__
        )
    except Exception:
        log.debug("botstats: could not count a command error", exc_info=True)
        return False


def build_flush_payload(drained):
    """The four parallel arrays :data:`FLUSH` unnests."""
    days = []
    commands = []
    error_types = []
    counts = []
    for day, command, error_type, n in drained.rows:
        days.append(day)
        commands.append(command)
        error_types.append(error_type)
        counts.append(n)
    return days, commands, error_types, counts


# ---------------------------------------------------------------------------
# SQL - one statement per constant (asyncpg prepares exactly one per call)
# ---------------------------------------------------------------------------
# ADDITIVE upsert, like usage_stats.FLUSH: a day can be written by any number of
# ticks and a retried batch is a re-add rather than an overwrite. The batch comes
# straight from dict keys, so (day, command, error_type) is unique by
# construction - the buffer's dict IS the dedup ON CONFLICT needs.
FLUSH = """
    INSERT INTO command_errors (day, command, error_type, count)
    SELECT day, command, error_type, count
    FROM unnest($1::date[], $2::text[], $3::text[], $4::bigint[])
         AS batch(day, command, error_type, count)
    ON CONFLICT (day, command, error_type)
    DO UPDATE SET count = command_errors.count + EXCLUDED.count;
    """

# The table holds at most a few hundred rows (see RETENTION_DAYS), so one plain
# DELETE is already bounded; the CTE only hands back how many rows went.
PRUNE = """
    WITH stale AS (
        DELETE FROM command_errors WHERE day < $1::date RETURNING 1
    )
    SELECT count(*)::bigint AS rows FROM stale;
    """

# Today and the window in ONE statement. The window is INCLUSIVE of today and
# spans exactly $2 calendar days (same convention as usage_stats.WINDOWS); the
# day is a PARAMETER, never CURRENT_DATE, so it matches the Python-side UTC day
# the rows were filed under. ``since`` is MIN(day) over the WHOLE table, outside
# the window: it says how much history there is to be summed.
ERROR_TOTALS = """
    SELECT COALESCE(SUM(count) FILTER (WHERE day = $1::date), 0)::bigint AS today,
           COALESCE(SUM(count), 0)::bigint AS week,
           (SELECT MIN(day) FROM command_errors) AS since
    FROM command_errors
    WHERE day >= $1::date - ($2::int - 1) AND day <= $1::date
    """

ERROR_TOP = """
    SELECT command, error_type, SUM(count)::bigint AS total
    FROM command_errors
    WHERE day >= $1::date - ($2::int - 1) AND day <= $1::date
    GROUP BY command, error_type
    ORDER BY total DESC, command ASC, error_type ASC
    LIMIT $3::int
    """

# Overdue timers, the top events among them and the rows that failed delivery,
# in ONE statement of three tagged arms. ``timers`` holds only PENDING work (a
# fired timer is deleted), so it is as small as the outstanding reminders; the
# overdue arm rides timers_expires_idx. Ages come from the DATABASE clock, the
# same one that decided "overdue".
#
# ``last_error`` is set by the dispatcher's failure path (reminders.py
# _deliver_durable), which also pushes ``expires`` into the future for a retry:
# a row with it is "failed at least once and still pending", not "overdue".
TIMER_HEALTH = """
    WITH due AS (
        SELECT event, expires FROM timers
        WHERE expires < now() - $1::int * interval '1 second'
    ), by_event AS (
        SELECT event, count(*)::bigint AS n FROM due
        GROUP BY event ORDER BY n DESC, event ASC LIMIT $2::int
    )
    SELECT 'total' AS kind, NULL::text AS event, count(*)::bigint AS n,
           EXTRACT(EPOCH FROM (now() - MIN(expires)))::float8 AS age
    FROM due
    UNION ALL
    SELECT 'event', event, n, NULL::float8 FROM by_event
    UNION ALL
    SELECT 'retrying', NULL::text, count(*)::bigint, NULL::float8
    FROM timers WHERE last_error IS NOT NULL
    """

# One scan of the recent slice (the last $1 days by updated_at, plus every
# pending row, however old) feeds three tagged arms: status counts for the last
# $4 hours (by created_at), the failed kinds ranking and the stuck pending count.
# updated_at >= created_at, so everything created in the last 24 hours is inside
# the slice. dashboard_actions has no time index; the table is bounded (user
# rows are pruned at 30 days, guild rows die with their guild) and the read
# runs under QUERY_TIMEOUT.
ACTION_HEALTH = """
    WITH recent AS (
        SELECT status, kind, created_at, updated_at FROM dashboard_actions
        WHERE updated_at >= now() - $1::int * interval '1 day'
           OR status = 'pending'
    ), failed AS (
        SELECT kind, count(*)::bigint AS n FROM recent
        WHERE status = 'failed'
          AND updated_at >= now() - $1::int * interval '1 day'
        GROUP BY kind ORDER BY n DESC, kind ASC LIMIT $2::int
    )
    SELECT 'status' AS section, status AS key, count(*)::bigint AS n
    FROM recent
    WHERE created_at >= now() - $4::int * interval '1 hour'
    GROUP BY status
    UNION ALL
    SELECT 'failed', kind, n FROM failed
    UNION ALL
    SELECT 'stuck', NULL::text, count(*)::bigint
    FROM recent
    WHERE status = 'pending'
      AND created_at < now() - $3::int * interval '1 minute'
    """


# ---------------------------------------------------------------------------
# Reads - value objects + the bounded queries that fill them
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ErrorStats:
    """Persisted error counts. ``since`` is the oldest day on record (None: none)."""

    as_of: datetime.date
    today: int
    week: int
    since: datetime.date | None
    top: tuple

    @property
    def covered_days(self):
        if self.since is None:
            return 0
        return max((self.as_of - self.since).days + 1, 1)


@dataclasses.dataclass(frozen=True)
class TimerStats:
    overdue: int
    oldest_seconds: float | None
    by_event: tuple
    retrying: int


@dataclasses.dataclass(frozen=True)
class ActionStats:
    by_status: dict
    failed: tuple
    stuck: int


async def fetch_error_stats(pool, *, timeout, today=None, days=ERROR_WINDOW_DAYS,
                            limit=ERROR_TOP_LIMIT):
    if today is None:
        today = utc_today()
    row = await pool.fetchrow(ERROR_TOTALS, today, days, timeout=timeout)
    ranking = await pool.fetch(ERROR_TOP, today, days, limit, timeout=timeout)
    return ErrorStats(
        as_of=today,
        today=int(row["today"]) if row else 0,
        week=int(row["week"]) if row else 0,
        since=row["since"] if row else None,
        top=tuple(
            (entry["command"], entry["error_type"], int(entry["total"]))
            for entry in ranking
        ),
    )


async def fetch_timer_stats(pool, *, timeout, overdue_seconds=TIMER_OVERDUE_SECONDS,
                            limit=TIMER_TOP_LIMIT):
    rows = await pool.fetch(TIMER_HEALTH, overdue_seconds, limit, timeout=timeout)
    overdue = 0
    oldest = None
    retrying = 0
    by_event = []
    for row in rows:
        if row["kind"] == "total":
            overdue = int(row["n"])
            oldest = float(row["age"]) if row["age"] is not None else None
        elif row["kind"] == "retrying":
            retrying = int(row["n"])
        else:
            by_event.append((row["event"], int(row["n"])))
    return TimerStats(overdue, oldest, tuple(by_event), retrying)


async def fetch_action_stats(pool, *, timeout, days=ACTION_FAILED_DAYS,
                             limit=ACTION_FAILED_LIMIT,
                             stuck_minutes=ACTION_STUCK_MINUTES,
                             hours=ACTION_STATUS_HOURS):
    rows = await pool.fetch(
        ACTION_HEALTH, days, limit, stuck_minutes, hours, timeout=timeout
    )
    by_status = {}
    failed = []
    stuck = 0
    for row in rows:
        if row["section"] == "status":
            by_status[row["key"]] = int(row["n"])
        elif row["section"] == "failed":
            failed.append((row["key"], int(row["n"])))
        else:
            stuck = int(row["n"])
    return ActionStats(by_status, tuple(failed), stuck)


# ---------------------------------------------------------------------------
# Live signals - read off objects the caller hands in, never imported
# ---------------------------------------------------------------------------
# Words for the states below; marked for extraction and translated at render.
STATE_LABELS = {
    "connected": N_("connected"),
    "connecting": N_("connecting"),
    "disconnected": N_("disconnected"),
    "closed": N_("closed"),
    "open": N_("open"),
    "half-open": N_("half-open"),
}


@dataclasses.dataclass(frozen=True)
class BreakerState:
    state: str  # closed | open | half-open
    seconds_left: float
    opens: int
    skipped: int


@dataclasses.dataclass(frozen=True)
class PoolState:
    size: int
    idle: int
    max: int

    @property
    def saturated(self):
        # Same rule as cogs/system/health.py: zero idle AND no room to grow.
        return self.idle == 0 and self.size >= self.max


def node_states(client):
    """``[(node_id, state)]`` for every registered Lavalink node, or ``None``.

    ``state`` is sonolink's own status read through its public properties
    (``is_connected`` / ``is_connecting``), as-is: a node whose TCP connect was
    refused sits in ``connecting`` and is reported as such.
    """
    if client is None:
        return None
    try:
        nodes = list(client.nodes)
        out = []
        for node in nodes:
            if node.is_connected:
                state = "connected"
            elif node.is_connecting:
                state = "connecting"
            else:
                state = "disconnected"
            out.append((str(getattr(node, "id", "?")), state))
        return out
    except Exception:
        log.debug("botstats: could not read the Lavalink nodes", exc_info=True)
        return None


def breaker_state(breaker):
    """A :class:`BreakerState` read off the AniList breaker's public API."""
    if breaker is None:
        return None
    try:
        if not breaker.tripped:
            state = "closed"
        elif breaker.seconds_left() > 0:
            state = "open"
        else:
            # The wait is over and no success has been reported yet: the next
            # request (or the one in flight) is the single probe.
            state = "half-open"
        return BreakerState(
            state, float(breaker.seconds_left()), int(breaker.opens), int(breaker.skipped)
        )
    except Exception:
        log.debug("botstats: could not read the AniList breaker", exc_info=True)
        return None


def pool_state(pool):
    if pool is None:
        return None
    try:
        return PoolState(pool.get_size(), pool.get_idle_size(), pool.get_max_size())
    except Exception:
        log.debug("botstats: could not read the DB pool", exc_info=True)
        return None


def finite_latency(latency):
    """Gateway latency in seconds, or ``None`` for nan / inf / missing."""
    if latency is None or not math.isfinite(latency):
        return None
    return latency


@dataclasses.dataclass(frozen=True)
class HealthSnapshot:
    """Everything the page shows. ``None`` always means UNKNOWN, never zero."""

    errors: ErrorStats | None = None
    errors_since_boot: int = 0
    errors_dropped: int = 0
    errors_today_in_memory: int = 0
    nodes: list | None = None
    players: int | None = None
    breaker: BreakerState | None = None
    chapters_embargo_seconds: float | None = None
    timers: TimerStats | None = None
    actions: ActionStats | None = None
    latency: float | None = None
    pool: PoolState | None = None
    gateway: tuple | None = None
    failed_extensions: tuple | None = None
    loaded_extensions: int | None = None


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
def assess(snapshot):
    """``(issues, unknown)``: degraded things, and checks that could not be read."""
    issues = []
    unknown = []

    if snapshot.nodes is None:
        unknown.append(_("Lavalink"))
    elif not snapshot.nodes:
        issues.append(_("no Lavalink node"))
    elif any(state != "connected" for _id, state in snapshot.nodes):
        issues.append(_("Lavalink node not connected"))

    if snapshot.breaker is None:
        unknown.append(_("AniList breaker"))
    elif snapshot.breaker.state != "closed":
        issues.append(
            _("AniList breaker {state}").format(
                state=_(STATE_LABELS[snapshot.breaker.state])
            )
        )

    if snapshot.timers is None:
        unknown.append(_("timers"))
    elif snapshot.timers.overdue:
        issues.append(
            _("{count} overdue timer(s)").format(count=snapshot.timers.overdue)
        )

    if snapshot.actions is None:
        unknown.append(_("dashboard actions"))
    elif snapshot.actions.stuck:
        issues.append(
            _("{count} stuck dashboard action(s)").format(count=snapshot.actions.stuck)
        )

    persisted = snapshot.errors.today if snapshot.errors is not None else 0
    errors_today = max(persisted, snapshot.errors_today_in_memory)
    if errors_today:
        issues.append(_("{count} error(s) today").format(count=errors_today))
    if snapshot.errors is None:
        unknown.append(_("persisted errors"))

    if snapshot.pool is None:
        unknown.append(_("database pool"))
    elif snapshot.pool.saturated:
        issues.append(_("database pool saturated"))

    if snapshot.failed_extensions is None:
        unknown.append(_("extensions"))
    elif snapshot.failed_extensions:
        issues.append(
            _("{count} extension(s) not loaded").format(
                count=len(snapshot.failed_extensions)
            )
        )
    return issues, unknown


def render_verdict(snapshot):
    issues, unknown = assess(snapshot)
    if issues:
        line = _("{count} issue(s): {issues}").format(
            count=len(issues), issues=", ".join(issues)
        )
    elif unknown:
        line = _("No issue found, but {count} check(s) could not be read: {names}").format(
            count=len(unknown), names=", ".join(unknown)
        )
    else:
        line = _("All green")
    return [line]


# ---------------------------------------------------------------------------
# Rendering - [(heading, lines)] from plain data only
# ---------------------------------------------------------------------------
def _unknown():
    return _("unknown")


def render_errors(snapshot):
    stats = snapshot.errors
    if stats is None:
        lines = [_("Persisted error counts are unavailable right now.")]
    else:
        lines = [
            _("Today (UTC): {today} - last {days} days: {week}").format(
                today=stats.today, days=ERROR_WINDOW_DAYS, week=stats.week
            )
        ]
        if stats.top:
            lines.extend(
                _("{command} / {error}: {count}").format(
                    command=command, error=error, count=total
                )
                for command, error, total in stats.top
            )
        else:
            lines.append(
                _("No unexpected error in the last {days} days.").format(
                    days=ERROR_WINDOW_DAYS
                )
            )
        if stats.covered_days < ERROR_WINDOW_DAYS:
            lines.append(
                "-# "
                + _("Counting is recent: {days} day(s) of history.").format(
                    days=stats.covered_days
                )
            )
    since_boot = _("Since boot: {count} (this process only)").format(
        count=snapshot.errors_since_boot
    )
    if snapshot.errors_dropped:
        since_boot += " - " + _("{count} not counted (buffer full)").format(
            count=snapshot.errors_dropped
        )
    lines.append(since_boot)
    lines.append(
        "-# "
        + _(
            "Unexpected failures only (not bad input, cooldowns or missing "
            "permissions), by UTC day, kept {days} days."
        ).format(days=RETENTION_DAYS)
    )
    return lines


def render_lavalink(snapshot):
    if snapshot.nodes is None:
        lines = [_("Lavalink nodes: {state}").format(state=_unknown())]
    elif not snapshot.nodes:
        lines = [_("No Lavalink node is registered.")]
    else:
        lines = [
            _("Node {node}: {state}").format(node=node_id, state=_(STATE_LABELS[state]))
            for node_id, state in snapshot.nodes[:NODE_LIMIT]
        ]
        if len(snapshot.nodes) > NODE_LIMIT:
            lines.append(
                _("... and {count} more").format(count=len(snapshot.nodes) - NODE_LIMIT)
            )
    lines.append(
        _("Active players: {count}").format(
            count=_unknown() if snapshot.players is None else snapshot.players
        )
    )
    return lines


def render_providers(snapshot, *, fmt_duration):
    breaker = snapshot.breaker
    if breaker is None:
        lines = [_("AniList breaker: {state}").format(state=_unknown())]
    else:
        text = _("AniList breaker: {state}").format(
            state=_(STATE_LABELS[breaker.state])
        )
        if breaker.state == "open":
            text += " - " + _("next probe in {duration}").format(
                duration=fmt_duration(breaker.seconds_left)
            )
        lines = [text]
        if breaker.opens:
            lines.append(
                _("Outages since boot: {opens} - poller requests skipped: {skipped}").format(
                    opens=breaker.opens, skipped=breaker.skipped
                )
            )
    embargo = snapshot.chapters_embargo_seconds
    if embargo is None:
        lines.append(_("MangaDex chapter alerts: {state}").format(state=_unknown()))
    elif embargo > 0:
        lines.append(
            _("MangaDex chapter alerts: paused by a rate limit, {duration} left").format(
                duration=fmt_duration(embargo)
            )
        )
    else:
        lines.append(_("MangaDex chapter alerts: no rate-limit pause"))
    return lines


def render_timers(snapshot, *, fmt_duration):
    stats = snapshot.timers
    if stats is None:
        return [_("Timer health is unavailable right now.")]
    minutes = TIMER_OVERDUE_SECONDS // 60
    if stats.overdue:
        lines = [
            _("{count} timer(s) overdue by more than {minutes} minutes").format(
                count=stats.overdue, minutes=minutes
            )
        ]
        if stats.oldest_seconds is not None:
            lines.append(
                _("Oldest overdue: {age}").format(age=fmt_duration(stats.oldest_seconds))
            )
        lines.extend(
            _("{event}: {count}").format(event=event, count=n)
            for event, n in stats.by_event
        )
    else:
        lines = [_("No timer is more than {minutes} minutes overdue.").format(minutes=minutes)]
    lines.append(
        _("Timers retrying after a failed delivery: {count}").format(count=stats.retrying)
    )
    return lines


def render_actions(snapshot):
    stats = snapshot.actions
    if stats is None:
        return [_("Dashboard action health is unavailable right now.")]
    seen = [
        "{0} {1}".format(status, stats.by_status[status])
        for status in ACTION_STATUSES
        if status in stats.by_status
    ]
    seen.extend(
        "{0} {1}".format(status, n)
        for status, n in sorted(stats.by_status.items())
        if status not in ACTION_STATUSES
    )
    if seen:
        lines = [
            _("Last {hours} h: {counts}").format(
                hours=ACTION_STATUS_HOURS, counts=", ".join(seen)
            )
        ]
    else:
        lines = [
            _("No dashboard action in the last {hours} hours.").format(
                hours=ACTION_STATUS_HOURS
            )
        ]
    lines.append(
        _("Pending for over {minutes} minutes: {count}").format(
            minutes=ACTION_STUCK_MINUTES, count=stats.stuck
        )
    )
    if stats.failed:
        lines.append(_("Failed in the last {days} days:").format(days=ACTION_FAILED_DAYS))
        lines.extend(
            _("{kind}: {count}").format(kind=kind, count=n) for kind, n in stats.failed
        )
    else:
        lines.append(
            _("No failed action in the last {days} days.").format(days=ACTION_FAILED_DAYS)
        )
    return lines


def render_process(snapshot):
    lines = [
        _("Gateway latency: {latency}").format(
            latency=(
                "{0} ms".format(round(snapshot.latency * 1000))
                if snapshot.latency is not None
                else _unknown()
            )
        )
    ]
    pool = snapshot.pool
    if pool is None:
        lines.append(_("Database pool: {state}").format(state=_unknown()))
    else:
        lines.append(
            _("Database pool: {size} open, {idle} idle, max {max}").format(
                size=pool.size, idle=pool.idle, max=pool.max
            )
        )
    if snapshot.gateway is None:
        lines.append(_("Gateway since boot: {state}").format(state=_unknown()))
    else:
        resumes, disconnects = snapshot.gateway
        lines.append(
            _("Gateway since boot: {resumes} resume(s), {disconnects} disconnect(s)").format(
                resumes=resumes, disconnects=disconnects
            )
        )
    failed = snapshot.failed_extensions
    if failed is None:
        lines.append(_("Extensions: {state}").format(state=_unknown()))
    elif not failed:
        lines.append(
            _("Extensions: {loaded} loaded, none failed").format(
                loaded=_unknown() if snapshot.loaded_extensions is None
                else snapshot.loaded_extensions
            )
        )
    else:
        lines.append(_("Extensions not loaded: {names}").format(
            names=", ".join(failed[:EXTENSION_LIMIT])
            + ("" if len(failed) <= EXTENSION_LIMIT else ", ...")
        ))
    return lines


def render_health(snapshot, *, fmt_duration):
    """The Health page: ``[(heading, lines)]`` from a :class:`HealthSnapshot`."""
    return [
        (_("Verdict"), render_verdict(snapshot)),
        (_("Errors"), render_errors(snapshot)),
        (_("Lavalink"), render_lavalink(snapshot)),
        (_("AniList and MangaDex"), render_providers(snapshot, fmt_duration=fmt_duration)),
        (_("Timers"), render_timers(snapshot, fmt_duration=fmt_duration)),
        (_("Dashboard actions"), render_actions(snapshot)),
        (_("Process"), render_process(snapshot)),
    ]

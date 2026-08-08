"""Owner-only bot-wide statistics dashboard (`?botstats` / `?bstats`).

Purpose: answer "which servers actually use this bot, and what is the process
doing?" from Discord, without a shell on the host. It is a PREFIX-ONLY surface
(no slash command, so it never appears in anyone's command picker) gated by
``cog_check`` -> ``bot.is_owner``, exactly like cogs/system/admin.py, and its
command is ``hidden=True`` for the same reason.

Boundaries with the neighbours:

* cogs/system/health.py owns the CONTINUOUS signal (the 60s LOAD line, gateway
  churn counters, the DB pool saturation warning). Nothing here duplicates it:
  this module is a pull surface, opened on demand, with no loop and no timer.
* cogs/community/serverstats owns PER-GUILD statistics for everyone. This is the
  bot-wide, operator-facing counterpart, and it borrows that package's honesty
  contract verbatim for the one section it shares with it (see below).

Three rules the pages below obey:

1. HONEST STATS. The observed-activity block on the Usage page reads
   ``server_stats_messages`` / ``server_stats_days``. A guild with no row in the
   window was NOT BEING WATCHED - that is not a zero, and it is never rendered
   as one. Every figure there is qualified by how many guilds and how many
   guild-days actually carry rows, and the footnote says so.
2. APPROXIMATE MEMBER COUNTS. ``core.Yasuho`` runs with
   ``chunk_guilds_at_startup=False``, so the member CACHE is nearly empty.
   ``Guild.member_count`` still works (it comes from the gateway's guild
   payload), but it is Discord's own periodically-refreshed figure and includes
   bots - the Overview and Top servers pages label it as approximate rather than
   presenting it as a headcount. A guild whose count is missing outright sorts
   LAST and renders as "?" - never as zero members.
3. ON-DEMAND ONLY. Every query is bounded and runs when its page is first
   opened, never on a timer and never all four at once. A SUCCESSFUL read is
   then memoised for the card's lifetime, so re-clicking a page costs nothing;
   a FAILED one is not, so a transient pool error is retried on the next open
   instead of being frozen into the card. The in-memory half of a page is
   recomputed every time (it is free), and the footer dates the page against
   the oldest memoised read it still shows.

The usage counters come in two halves, and the Usage page shows both because
they answer different questions:

* SINCE BOOT - the in-memory :class:`UsageCounters`, like health.py's gateway
  counters. They show what a LIVE process is being asked to do and reset on
  every restart (which, on this deployment, means every deploy).
* PERSISTED - the same completions are also counted per (UTC day, command) into
  a bounded buffer and flushed to ``command_usage`` every few minutes, so the
  day / week / month windows survive restarts (cogs/system/usage_stats.py owns
  that half: the buffer, the additive upsert, the bounded reads and the prune).
* WHEN, not how much - the same flush also folds every completion into a 168-slot
  (UTC weekday, UTC hour) profile, which the Usage page turns into the quietest
  and busiest slots of the week. That is the restart-window question: this bot is
  redeployed by hand, and the profile says which hours cost the fewest people.
  It is a DECAYED profile (halved weekly), so it describes recent habits rather
  than the install's whole history, and it stays silent until a full week has
  been collected - a slot nobody has lived through yet is not a quiet slot.

Both listeners stay O(1), await nothing and touch no DB - the ONLY writer is the
flush loop below, so the write rate is a function of TIME (one statement per 5
minutes, bot-wide), never of traffic. Both counters' cardinality is bounded by
the number of distinct command names.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import math
import os
import platform
from collections import Counter
from dataclasses import dataclass

import discord
from discord.ext import commands, tasks

from cogs.system import usage_stats
from tools import interactions
from tools.backup import human_size
from tools.formats import format_dt, random_colour
from tools.i18n import _
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)

# How many guilds the Top servers page ranks. Owner-only and O(N) over
# bot.guilds at invocation time only, so the cost is a sort of a few thousand
# tuples at worst - never a background job.
TOP_GUILDS_LIMIT = 15

# Guild names are attacker-chosen text on a card with 15 of them: cut to a
# readable width (and escaped at render time) so one server cannot push the rest
# of the ranking out of the block.
GUILD_NAME_LIMIT = 60

# How many command names the Usage page lists, and how many tables the Data page
# ranks by on-disk size.
TOP_COMMANDS_LIMIT = 10
TOP_TABLES_LIMIT = 10

# Window of the observed-activity block on the Usage page.
ACTIVITY_WINDOW_DAYS = 7

# One batched write every 5 minutes, bot-wide - the same clock as the serverstats
# collector and the voice-XP sweep. It bounds the DB write rate independently of
# how many commands are run, and losing at most one interval of counters to a
# hard crash (a kill -9, never a clean shutdown: see cog_unload) is acceptable
# for aggregates.
FLUSH_INTERVAL = 300

# How long cog_unload waits for a cancelled in-flight flush to unwind before it
# runs the final one. Generous next to a single upsert, tiny next to a shutdown:
# the point is that teardown is bounded even if the pool is wedged.
UNLOAD_CANCEL_TIMEOUT = 5

# ... and how long the final flush itself gets. Without it the last write is
# bounded only by the pool's command_timeout (core.main: 60s), so a wedged DB
# would hold a clean shutdown open for a minute over statistics. Losing the last
# interval to a wedged pool is the same loss a kill -9 already causes.
UNLOAD_FLUSH_TIMEOUT = 5

# Per-statement ceiling for every read below. The row-count query seq-scans the
# featured tables, which grows with the install, so the wait has to be bounded
# or a click on an oversized deployment hangs forever.
#
# This is deliberately LONGER than Discord's 3s initial-response deadline: it
# does not defend that deadline and cannot (see BotStatsDashboard.show_page,
# which ACKs the click before any read runs, leaving a 15 min deferred token).
# What it bounds is the work behind that ACK.
QUERY_TIMEOUT = 15.0

# The tables the Data page counts rows for: one per major feature, chosen so the
# page reads as "what is actually being used", not as a schema dump.
# SCALE: these are EXACT counts, i.e. one seq scan per table. On a large install
# `levels` / `xp_period` are the two that grow without bound, so the count is
# what QUERY_TIMEOUT above is really protecting: past that, the page says "row
# counts unavailable" rather than holding the interaction open. That is
# acceptable precisely because this is an owner-only, on-demand surface that
# runs the query at most once per card - a reltuples ESTIMATE would be cheap but
# would publish a stale figure as if it were a count, which this dashboard's
# honesty rules do not allow anywhere else either.
FEATURED_TABLES = (
    "levels",
    "xp_period",
    "user_profiles",
    "profile_connections",
    "anilist_feeds",
    "mangadex_mapping",
    "guild_playlists",
    "cases",
    "timers",
)


# ---------------------------------------------------------------------------
# SQL - one statement per constant (asyncpg prepares exactly one per call)
# ---------------------------------------------------------------------------
DB_SIZE = "SELECT pg_database_size(current_database())::bigint AS bytes"

# Observed messages in the window. COUNT(DISTINCT guild_id) / COUNT(DISTINCT day)
# are what make the SUM honest: they say how much of the fleet the sum actually
# covers, so an absent guild reads as unobserved rather than silent (rule 1).
#
# The window is INCLUSIVE of today and spans exactly $2 calendar days, i.e.
# $1 - ($2 - 1) .. $1. Plain `$1 - $2` would sum $2 + 1 days under a heading
# that says $2; the house convention is
# cogs/community/serverstats/rollups.window_bounds, which this matches.
#
# The day the window ends on is a PARAMETER rather than CURRENT_DATE, for the
# same reason as cogs/system/usage_stats.py's queries: CURRENT_DATE is the
# DATABASE SESSION's calendar day, which equals the UTC day only while the
# server's TimeZone is UTC. Both blocks on this page must key off ONE "today",
# and the rows themselves are written per UTC day.
OBSERVED_MESSAGES = """
    SELECT COALESCE(SUM(messages), 0)::bigint AS messages,
           COUNT(DISTINCT guild_id)::bigint   AS guilds,
           COUNT(DISTINCT day)::bigint        AS days
    FROM server_stats_messages
    WHERE day >= $1::date - ($2::int - 1)
    """

# Same shape for the guild-day rollup. COUNT(*) is the number of guild-days that
# EXIST - the denominator the footnote quotes; it is never compared against
# "guilds x days", which would invent the days nobody watched.
OBSERVED_DAYS = """
    SELECT COALESCE(SUM(joins), 0)::bigint  AS joins,
           COALESCE(SUM(leaves), 0)::bigint AS leaves,
           COUNT(DISTINCT guild_id)::bigint AS guilds,
           COUNT(*)::bigint                 AS guild_days
    FROM server_stats_days
    WHERE day >= $1::date - ($2::int - 1)
    """

# Biggest tables on disk plus the fleet total, in ONE statement: the window
# SUM() is evaluated over the whole result set BEFORE ORDER BY / LIMIT, so
# total_bytes covers every public table and not just the ten rows returned.
#
# relkind: 'r' ordinary tables (which includes every partition), 'p'
# partitioned parents and 'm' materialized views. The page labels the sum
# "every table", so the filter has to keep matching that claim as the schema
# grows - a matview holds real storage that 'r' alone would silently drop. TOAST
# relations ('t') stay out: pg_total_relation_size already folds each one into
# its parent, so counting them would double the bytes they hold.
TABLE_SIZES = """
    SELECT c.relname                                            AS name,
           pg_total_relation_size(c.oid)::bigint                AS bytes,
           (SUM(pg_total_relation_size(c.oid)) OVER ())::bigint AS total_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'm')
    ORDER BY bytes DESC
    LIMIT $1::int
    """


def build_row_counts_sql(tables):
    """One UNION ALL statement counting the rows of every table in ``tables``.

    The identifiers are interpolated because there is no parameter form for a
    table name in SQL. That is safe HERE and only here: ``tables`` is the
    module-level :data:`FEATURED_TABLES` literal, never a runtime value and
    never anything a user can reach.
    """
    return "\nUNION ALL ".join(
        "SELECT '{name}' AS name, COUNT(*)::bigint AS rows FROM {name}".format(
            name=name
        )
        for name in tables
    )


ROW_COUNTS = build_row_counts_sql(FEATURED_TABLES)


# ---------------------------------------------------------------------------
# /proc parsing - pure helpers, each total on garbage input
# ---------------------------------------------------------------------------
def parse_proc_starttime_ticks(stat_text):
    """Field 22 of ``/proc/<pid>/stat`` (process start, in clock ticks).

    Field 2 (``comm``) is the executable name wrapped in parentheses and MAY
    itself contain spaces and parentheses, so splitting the whole line on
    whitespace shifts every later field. The scan therefore starts after the
    LAST ``)`` of the line: the first field of that remainder is field 3
    (``state``), which puts field 22 at index 19.

    Returns ``None`` on any line this cannot read, never a guessed value.
    """
    close = stat_text.rfind(")")
    if close == -1:
        return None
    fields = stat_text[close + 1:].split()
    if len(fields) < 20:
        return None
    try:
        return float(fields[19])
    except ValueError:
        return None


def parse_proc_uptime_seconds(uptime_text):
    """Seconds since boot - the first field of ``/proc/uptime``."""
    fields = uptime_text.split()
    if not fields:
        return None
    try:
        return float(fields[0])
    except ValueError:
        return None


# Units /proc/self/status may report VmRSS in. It is kB in practice on every
# kernel, but the suffix is parsed rather than assumed so a different one can
# never be read as kB and under-report memory by three orders of magnitude.
_RSS_UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}


def parse_vm_rss_bytes(status_text):
    """Resident set size in BYTES from a ``/proc/<pid>/status`` dump."""
    for line in status_text.splitlines():
        if not line.startswith("VmRSS:"):
            continue
        fields = line.split()
        if len(fields) < 2:
            return None
        try:
            value = int(fields[1])
        except ValueError:
            return None
        unit = fields[2].lower() if len(fields) > 2 else "kb"
        multiplier = _RSS_UNITS.get(unit)
        if multiplier is None:
            return None
        return value * multiplier
    return None


def process_uptime_seconds(stat_text, uptime_text, clock_ticks):
    """How long this process has been alive, from the two /proc reads.

    ``starttime`` is measured in clock ticks since BOOT, so the process uptime
    is ``/proc/uptime`` minus ``starttime / SC_CLK_TCK``. Returns ``None`` when
    either read is unusable or the arithmetic comes out negative (a clock the
    caller must not present as an uptime).
    """
    ticks = parse_proc_starttime_ticks(stat_text)
    boot_seconds = parse_proc_uptime_seconds(uptime_text)
    if ticks is None or boot_seconds is None or not clock_ticks or clock_ticks <= 0:
        return None
    uptime = boot_seconds - (ticks / clock_ticks)
    return uptime if uptime >= 0 else None


def _read_text(path):
    """Read a small /proc file, or ``None`` on any OS-level failure.

    Synchronous on purpose: a /proc file is a kernel-generated buffer of a few
    hundred bytes with no disk behind it, so this costs microseconds and never
    blocks - handing it to an executor would cost more than the read.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def read_process_uptime_seconds():
    """Process uptime from /proc, or ``None`` where /proc is unavailable."""
    stat_text = _read_text("/proc/self/stat")
    uptime_text = _read_text("/proc/uptime")
    if stat_text is None or uptime_text is None:
        return None
    try:
        clock_ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    return process_uptime_seconds(stat_text, uptime_text, clock_ticks)


def read_rss_bytes():
    """Resident memory from /proc, or ``None`` where /proc is unavailable."""
    status_text = _read_text("/proc/self/status")
    if status_text is None:
        return None
    return parse_vm_rss_bytes(status_text)


# ---------------------------------------------------------------------------
# Formatting - pure
# ---------------------------------------------------------------------------
def format_count(value):
    """Thousands-separated integer, ASCII (``1,234,567``)."""
    return "{:,}".format(int(value))


def format_duration(seconds):
    """Coarse ASCII duration: ``3d 4h 12m``, ``12m 30s``, ``0s``.

    Seconds are only shown while the value is under a day, where they carry
    information; beyond that the three coarsest units are enough.
    """
    total = int(seconds)
    if total <= 0:
        return "0s"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append("{0}d".format(days))
    if hours:
        parts.append("{0}h".format(hours))
    if minutes:
        parts.append("{0}m".format(minutes))
    if secs and not days:
        parts.append("{0}s".format(secs))
    return " ".join(parts) or "0s"


# ---------------------------------------------------------------------------
# Guild shaping - plain data, no discord objects downstream
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GuildRow:
    """One guild reduced to the fields the dashboard renders.

    ``channels`` is carried here rather than recounted from ``bot.guilds``
    because ``Guild.channels`` rebuilds a list on every access: one pass over
    the fleet fills every page (see :func:`count_channels`).
    """

    guild_id: int
    name: str
    member_count: int | None
    joined_at: datetime.datetime | None
    channels: int = 0


def collect_guild_rows(guilds):
    """Reduce ``bot.guilds`` to :class:`GuildRow` values in ONE pass.

    Every attribute is read defensively: an unavailable guild has no
    ``member_count``, and ``guild.me`` is ``None`` whenever the bot's own member
    is missing from the (deliberately unchunked) member cache - neither may
    raise here, and neither is substituted with a made-up value.
    """
    rows = []
    for guild in guilds:
        me = getattr(guild, "me", None)
        rows.append(
            GuildRow(
                guild_id=guild.id,
                name=getattr(guild, "name", None) or str(guild.id),
                member_count=getattr(guild, "member_count", None),
                joined_at=getattr(me, "joined_at", None),
                channels=len(getattr(guild, "channels", ()) or ()),
            )
        )
    return rows


def top_guild_rows(rows, limit=TOP_GUILDS_LIMIT):
    """The ``limit`` largest guilds, biggest first.

    A row with an UNKNOWN member count sorts after every known one rather than
    being folded in at zero (rule 2): an unavailable guild is not a small guild.
    Ties break on guild id so the same fleet always renders in the same order.
    """
    ordered = sorted(
        rows,
        key=lambda row: (
            row.member_count is None,
            -(row.member_count or 0),
            row.guild_id,
        ),
    )
    return ordered[:limit]


def guild_totals(rows):
    """``(guilds, known_members, guilds_without_a_count)`` over every row."""
    known = [row.member_count for row in rows if row.member_count is not None]
    return len(rows), sum(known), len(rows) - len(known)


def count_channels(rows):
    """Total channels across the fleet, from the rows already collected."""
    return sum(row.channels for row in rows)


# ---------------------------------------------------------------------------
# Usage counters - in memory, since boot
# ---------------------------------------------------------------------------
class UsageCounters:
    """Since-boot command counters: one Counter keyed by ``qualified_name``.

    ``record`` is O(1), awaits nothing and writes nothing, so it is safe on the
    completion listeners of every command in the bot. Memory is bounded by the
    number of DISTINCT command names (a few hundred at most), never by traffic.
    The prefix/slash split is kept as two integers rather than two Counters:
    the per-name ranking is what the page shows, the surface split is a single
    headline figure.
    """

    def __init__(self, started_at=None):
        self.started_at = started_at or datetime.datetime.now(datetime.timezone.utc)
        self.commands = Counter()
        self.prefix_total = 0
        self.slash_total = 0

    def record(self, name, *, slash=False):
        if not name:
            return
        self.commands[name] += 1
        if slash:
            self.slash_total += 1
        else:
            self.prefix_total += 1

    @property
    def total(self):
        return self.prefix_total + self.slash_total

    def top(self, limit=TOP_COMMANDS_LIMIT):
        """The ``limit`` most-used commands as ``(name, count)``.

        Sorted by count then NAME - ``Counter.most_common`` breaks ties by
        insertion order, which would reshuffle the tail between two renders of
        the very same data.
        """
        return sorted(
            self.commands.items(), key=lambda item: (-item[1], item[0])
        )[:limit]


def is_hybrid_app_command(command):
    """Is this app command the slash half of a hybrid (prefix) command?

    discord.py dispatches BOTH ``on_command_completion`` (from the wrapped ext
    command's after-hooks) and ``on_app_command_completion`` (from the tree) for
    one hybrid slash invocation, so counting both would double every hybrid.
    ``__commands_is_hybrid_app_command__`` is discord.py's own marker for that
    class; read via getattr so a plain app command simply answers False.
    """
    return bool(getattr(command, "__commands_is_hybrid_app_command__", False))


# ---------------------------------------------------------------------------
# Page data - value objects + the reads that fill them
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ObservedActivity:
    """The observed-activity block, with its own coverage baked in.

    ``message_guilds`` / ``day_guilds`` and ``message_days`` / ``guild_days``
    are not decoration: they are what turns a bare SUM into an honest one. Zero
    observed guilds means "nothing was watched", and the renderer must say that
    instead of printing the structural 0 the SUM produced.
    """

    days: int
    messages: int
    message_guilds: int
    message_days: int
    joins: int
    leaves: int
    day_guilds: int
    guild_days: int


@dataclass(frozen=True)
class TableSize:
    name: str
    bytes: int


async def fetch_database_size(pool):
    return await pool.fetchval(DB_SIZE, timeout=QUERY_TIMEOUT)


async def fetch_observed_activity(pool, days=ACTIVITY_WINDOW_DAYS, today=None):
    """Two bounded aggregates over the last ``days`` UTC days.

    ``today`` is the UTC day the window ends on, computed in Python so that this
    block and the recorded-usage block above it cannot end up on two different
    "today"s on a database whose session TimeZone is not UTC.
    """
    if today is None:
        today = usage_stats.utc_today()
    messages = await pool.fetchrow(
        OBSERVED_MESSAGES, today, days, timeout=QUERY_TIMEOUT
    )
    member_days = await pool.fetchrow(
        OBSERVED_DAYS, today, days, timeout=QUERY_TIMEOUT
    )
    return ObservedActivity(
        days=days,
        messages=messages["messages"],
        message_guilds=messages["guilds"],
        message_days=messages["days"],
        joins=member_days["joins"],
        leaves=member_days["leaves"],
        day_guilds=member_days["guilds"],
        guild_days=member_days["guild_days"],
    )


async def fetch_row_counts(pool):
    rows = await pool.fetch(ROW_COUNTS, timeout=QUERY_TIMEOUT)
    return [(row["name"], row["rows"]) for row in rows]


async def fetch_table_sizes(pool, limit=TOP_TABLES_LIMIT):
    """``(top tables, total bytes of EVERY public table)``."""
    rows = await pool.fetch(TABLE_SIZES, limit, timeout=QUERY_TIMEOUT)
    total = rows[0]["total_bytes"] if rows else 0
    return [TableSize(row["name"], row["bytes"]) for row in rows], total


# ---------------------------------------------------------------------------
# Rendering - each returns [(heading, lines)] from PLAIN data only
# ---------------------------------------------------------------------------
def _unknown():
    return _("unknown")


def render_overview(
    *,
    guilds,
    members,
    guilds_without_count,
    cached_users,
    channels,
    latency_seconds,
    uptime_seconds,
    uptime_is_process,
    rss_bytes,
    shards,
    python_version,
    discord_version,
    db_bytes,
):
    """The Overview page: reach, process, versions."""
    reach = [
        _("Servers: {count}").format(count=format_count(guilds)),
        _("Members: {count} (approximate, bots included)").format(
            count=format_count(members)
        ),
        _("Cached users: {count}").format(count=format_count(cached_users)),
        _("Channels: {count}").format(count=format_count(channels)),
    ]
    if guilds_without_count:
        # Never folded into the total as zero - say how many guilds are missing.
        reach.append(
            _("{count} server(s) report no member count and are excluded.").format(
                count=format_count(guilds_without_count)
            )
        )
    reach.append(
        "-# "
        + _(
            "Member counts come from Discord's guild payloads, not from a "
            "member cache (this bot never chunks one)."
        )
    )

    if uptime_seconds is None:
        uptime = _unknown()
    elif uptime_is_process:
        uptime = format_duration(uptime_seconds)
    else:
        # /proc was unreadable, so this is time since the COG loaded - shorter
        # than the process has really been alive after a hot reload. Labeled,
        # never passed off as the process uptime.
        uptime = _("{duration} (since this cog loaded)").format(
            duration=format_duration(uptime_seconds)
        )

    process = [
        _("Uptime: {uptime}").format(uptime=uptime),
        _("Memory (RSS): {size}").format(
            size=human_size(rss_bytes) if rss_bytes is not None else _unknown()
        ),
        _("Gateway latency: {latency}").format(
            latency=(
                "{0} ms".format(round(latency_seconds * 1000))
                if latency_seconds is not None
                else _unknown()
            )
        ),
        _("Shards: {count}").format(count=format_count(shards)),
    ]

    versions = [
        _("Python {python} - discord.py {discord}").format(
            python=python_version, discord=discord_version
        ),
        _("Database size: {size}").format(
            size=human_size(db_bytes) if db_bytes is not None else _unknown()
        ),
    ]

    return [
        (_("Reach"), reach),
        (_("Process"), process),
        (_("Versions"), versions),
    ]


def render_top_guilds(rows, *, guilds, members, guilds_without_count):
    """The Top servers page: the ranking plus the fleet total."""
    if not rows:
        return [(_("Top servers"), [_("This bot is in no server.")])]

    lines = []
    for position, row in enumerate(rows, start=1):
        members_text = (
            format_count(row.member_count) if row.member_count is not None else "?"
        )
        joined = (
            format_dt(row.joined_at, "d") if row.joined_at is not None else _unknown()
        )
        lines.append(
            _("`{rank:>2}.` **{name}** - {members} members - joined {joined}").format(
                rank=position,
                name=discord.utils.escape_markdown(row.name[:GUILD_NAME_LIMIT]),
                members=members_text,
                joined=joined,
            )
        )
        lines.append("-# `{0}`".format(row.guild_id))

    total = [
        _("{guilds} servers, {members} members total").format(
            guilds=format_count(guilds), members=format_count(members)
        )
    ]
    if guilds_without_count:
        total.append(
            _("{count} server(s) report no member count and are excluded.").format(
                count=format_count(guilds_without_count)
            )
        )
    total.append(
        "-# "
        + _("Member counts are Discord's approximation and include bots.")
    )

    return [
        (
            _("Top {count} servers by members").format(count=format_count(len(rows))),
            lines,
        ),
        (_("Fleet total"), total),
    ]


def render_usage(counters, activity, persisted=None, *, now=None):
    """The Usage page: since-boot counters, persisted windows, observed activity.

    ``persisted`` is the ``command_usage`` half (``None`` when the read failed or
    was not attempted). The since-boot block is kept as-is next to it on purpose:
    it is the only thing on this dashboard that describes the LIVE process, and
    the persisted windows cannot replace it - a bot restarted two minutes ago has
    a full 30-day history and an empty since-boot count, and both facts matter.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    elapsed = (now - counters.started_at).total_seconds()

    headline = [
        _("{total} commands run since boot ({elapsed} ago)").format(
            total=format_count(counters.total), elapsed=format_duration(elapsed)
        ),
        _("{prefix} prefix - {slash} slash").format(
            prefix=format_count(counters.prefix_total),
            slash=format_count(counters.slash_total),
        ),
        "-# " + _("In-memory counters: they reset on every restart."),
    ]

    top = counters.top(TOP_COMMANDS_LIMIT)
    if not top:
        ranking = [_("No command has completed yet.")]
    else:
        ranking = [
            _("`{rank:>2}.` `{name}` - {count}").format(
                rank=position, name=name, count=format_count(count)
            )
            for position, (name, count) in enumerate(top, start=1)
        ]

    window_days = (
        persisted.week_days if persisted is not None else usage_stats.WEEK_DAYS
    )
    return [
        (_("Commands since boot"), headline),
        (_("Most used commands"), ranking),
        (_("Recorded usage (UTC days)"), render_persisted_usage(persisted)),
        (_("Quiet hours (UTC)"), render_quiet_hours(persisted)),
        (
            _("Most used commands ({days} days)").format(days=window_days),
            render_persisted_ranking(persisted),
        ),
        (
            _("Observed activity ({days} days)").format(
                days=activity.days if activity is not None else ACTIVITY_WINDOW_DAYS
            ),
            render_observed_activity(activity),
        ),
    ]


def render_persisted_usage(persisted):
    """The day / week / month totals, with the coverage they really have.

    Same honesty rule as the observed-activity block below: a failed read says
    "unavailable" and an empty table says "nothing recorded yet". Neither is
    rendered as a zero - a zero would claim that nobody ran a command, which is
    not what either of those states means.

    The same rule applies INSIDE the numbers: a day with no row is a day nobody
    was counting on, so every multi-day total names how many of its days are
    actually recorded. "1,200" over a month the bot was up for ten days of is a
    true sum and a false impression; "1,200 (10 of 30 day(s) recorded)" is both
    true. The ``since`` note below only catches a short history at the START of
    collection, which is why it is not enough on its own.

    The windows are CALENDAR UTC days ending TODAY, today included. That is
    deliberately NOT the serverstats convention (which ends yesterday, because a
    partial day would corrupt a comparison between periods): this block is lived
    usage, not a comparison, and an operator asking "what happened today" must be
    shown today. The footnote says so, so the partial day is never a surprise.
    """
    if persisted is None:
        return [_("Recorded usage is unavailable right now.")]
    if persisted.since is None:
        return [
            _(
                "Nothing has been recorded yet - collection starts with the "
                "first flush after this bot came up."
            )
        ]

    lines = [
        # No noun on this line on purpose: "1 commands" is what a count plus a
        # hardcoded plural produces, and the heading above already says what is
        # being counted.
        _("Today: {count}").format(count=format_count(persisted.today)),
        _("{days} days: {count} ({recorded} of {days} day(s) recorded)").format(
            days=persisted.week_days,
            count=format_count(persisted.week),
            recorded=format_count(persisted.week_recorded),
        ),
        _("{days} days: {count} ({recorded} of {days} day(s) recorded)").format(
            days=persisted.month_days,
            count=format_count(persisted.month),
            recorded=format_count(persisted.month_recorded),
        ),
        "-# "
        + _(
            "Calendar UTC days ending today, today included - so today is a "
            "partial day."
        ),
    ]
    if not persisted.window_is_full(persisted.month_days):
        # The heading says 30 days; the table may hold six. Say which.
        lines.append(
            "-# "
            + _(
                "Recorded since {date} ({days} day(s)): the longer window is "
                "not full yet."
            ).format(
                date=persisted.since.isoformat(),
                days=format_count(persisted.covered_days),
            )
        )
    return lines


def weekday_abbreviations():
    """The seven weekday labels, Monday first - the ``dow`` 0..6 order.

    Built at render time (like page_labels) so they localize for the reader, and
    NOT taken from the C library's locale: the process locale has nothing to do
    with the reader's chosen language, and calendar.day_abbr would answer in
    whatever the host is set to.
    """
    return (
        # Translators: the seven weekday abbreviations, Monday first. They label
        # hour slots such as "Mon 04:00", so keep them SHORT (3 characters).
        _("Mon"),
        _("Tue"),
        _("Wed"),
        _("Thu"),
        _("Fri"),
        _("Sat"),
        _("Sun"),
    )


def format_hour_slots(slots):
    """``Mon 04:00 (0.3%)`` for each slot, comma-separated.

    The share is what is shown rather than the count: the profile is decayed
    (halved weekly), so its absolute numbers are not a number of commands and
    must never be printed as if they were.
    """
    names = weekday_abbreviations()
    return ", ".join(
        "{day} {hour:02d}:00 ({share:.1f}%)".format(
            day=names[slot.dow], hour=slot.hour, share=slot.share * 100
        )
        for slot in slots
    )


def render_quiet_hours(persisted):
    """When a restart hurts least: the quietest and busiest slots of the week.

    Same honesty rules as every other block on this page, and THREE refusals
    rather than one, because a reader cannot act on them the same way:

    * the read failed - say so;
    * the profile is too young to have lived through all 168 slots - say how
      young. A slot that has not happened yet is not a quiet slot, and that is
      the one way this block could quietly send the owner to restart during
      their busiest hour;
    * the profile is old enough and holds nothing at all - say THAT, not the
      day count. An old-but-empty profile used to print "needs 7 day(s) of
      collection and has 232", which contradicts itself in one sentence.

    The quiet side then refuses a fourth time, softly: when more than
    :data:`usage_stats.QUIET_SLOT_LIMIT` slots are tied at zero, naming three of
    them would dress the earliest hours of the week up as a finding, so it says
    how many there are instead.
    """
    if persisted is None:
        return [_("Recorded usage is unavailable right now.")]
    if not usage_stats.hourly_is_ready(persisted.hourly_covered_days):
        return [
            _(
                "Not enough hourly history yet: this needs {days} day(s) of "
                "collection and has {have}."
            ).format(
                days=usage_stats.HOURLY_MIN_DAYS,
                have=format_count(persisted.hourly_covered_days),
            )
        ]
    ranked = usage_stats.rank_hour_slots(
        persisted.hourly, covered_days=persisted.hourly_covered_days
    )
    if ranked is None:
        return [
            _("No commands have been recorded in the hourly profile yet.")
        ]
    if ranked.quiet_slots > usage_stats.QUIET_SLOT_LIMIT:
        quietest = _(
            "Quietest: {count} of {total} slots recorded nothing at all."
        ).format(
            count=format_count(ranked.quiet_slots),
            total=format_count(ranked.total_slots),
        )
    else:
        quietest = _("Quietest: {slots}").format(
            slots=format_hour_slots(ranked.quietest)
        )
    return [
        quietest,
        _("Busiest: {slots}").format(slots=format_hour_slots(ranked.busiest)),
        "-# "
        + _(
            "Share of all recorded commands, by UTC hour of the week. The "
            "profile halves every {days} days, so recent weeks weigh most."
        ).format(days=usage_stats.HOURLY_HALVE_DAYS),
    ]


def render_persisted_ranking(persisted):
    """The persisted top-N over the middle window."""
    if persisted is None:
        return [_("Recorded usage is unavailable right now.")]
    if not persisted.top:
        return [_("No command has been recorded in this window.")]
    return [
        _("`{rank:>2}.` `{name}` - {count}").format(
            rank=position, name=name, count=format_count(count)
        )
        for position, (name, count) in enumerate(persisted.top, start=1)
    ]


def render_observed_activity(activity):
    """The honest-stats block (rule 1): coverage first, sums second.

    A guild with no row in the window was not being watched. So a window nobody
    was watched over prints THAT, not the structural zero the SUM returned, and
    every figure that is printed names how many guilds and guild-days it covers.
    """
    if activity is None:
        return [_("Observed activity is unavailable right now.")]

    lines = []
    if activity.message_guilds == 0:
        lines.append(
            _("No server was observed in this window - this is not a zero.")
        )
    else:
        lines.append(
            _(
                "{messages} messages, across {guilds} observed server(s) "
                "and {days} observed day(s)"
            ).format(
                messages=format_count(activity.messages),
                guilds=format_count(activity.message_guilds),
                days=format_count(activity.message_days),
            )
        )

    if activity.day_guilds == 0:
        lines.append(_("No join/leave day was recorded in this window."))
    else:
        lines.append(
            _(
                "{joins} joins / {leaves} leaves, across {guilds} observed "
                "server(s) and {guild_days} observed server-day(s)"
            ).format(
                joins=format_count(activity.joins),
                leaves=format_count(activity.leaves),
                guilds=format_count(activity.day_guilds),
                guild_days=format_count(activity.guild_days),
            )
        )

    lines.append(
        "-# "
        + _(
            "Observed servers only: a server with no row was not being "
            "watched, which is never the same as zero."
        )
    )
    return lines


def render_data(row_counts, table_sizes, total_bytes):
    """The Data page: featured row counts, then the biggest tables on disk."""
    if row_counts is None:
        counts = [_("Row counts are unavailable right now.")]
    elif not row_counts:
        counts = [_("No featured table was counted.")]
    else:
        counts = [
            "`{name}` - {rows}".format(name=name, rows=format_count(rows))
            for name, rows in row_counts
        ]

    if table_sizes is None:
        sizes = [_("Table sizes are unavailable right now.")]
    elif not table_sizes:
        sizes = [_("No table in this database.")]
    else:
        sizes = [
            "`{rank:>2}.` `{name}` - {size}".format(
                rank=position, name=table.name, size=human_size(table.bytes)
            )
            for position, table in enumerate(table_sizes, start=1)
        ]
        sizes.append(
            _("Every table: {size}").format(size=human_size(total_bytes or 0))
        )
        sizes.append(
            "-# " + _("Sizes include indexes and TOAST storage.")
        )

    return [
        (_("Featured tables"), counts),
        (_("Largest tables"), sizes),
    ]


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------
# Page identity is an index into this tuple: (emoji, N_-free label callable).
# Labels are built at render time so they localize for the reader.
PAGE_OVERVIEW, PAGE_GUILDS, PAGE_USAGE, PAGE_DATA = range(4)
PAGE_EMOJI = (
    "\N{BAR CHART}",
    "\N{TROPHY}",
    "\N{HIGH VOLTAGE SIGN}",
    "\N{FLOPPY DISK}",
)


def page_labels():
    return (_("Overview"), _("Top servers"), _("Usage"), _("Data"))


class _PageButton(discord.ui.Button):
    """Jump to one dashboard page. Holds its owner as ``_owner``.

    NEVER named ``parent``: ``discord.ui.Item`` already owns that attribute and
    shadowing it breaks the layout walk (see tests/test_view_hygiene.py).
    """

    def __init__(self, owner, index, label, emoji, *, current):
        self._owner = owner
        self._index = index
        super().__init__(
            label=label,
            emoji=emoji,
            style=(
                discord.ButtonStyle.primary
                if current
                else discord.ButtonStyle.secondary
            ),
            disabled=current,
        )

    async def callback(self, interaction):
        await self._owner.show_page(interaction, self._index)


class BotStatsDashboard(AuthorLayoutView):
    """The ``?botstats`` Components V2 card: four pages, four button jumps.

    Each DB read runs the FIRST time a page that needs it is opened and is then
    memoised for the card's lifetime, so the card costs at most four bounded
    query batches total no matter how much the owner clicks, and it never
    refreshes itself on a timer. The IN-MEMORY half of a page (guild rows,
    /proc, the usage counters) is recomputed on every open - it costs nothing
    and freezing it would make the card lie about a live process.

    The rebuild method is ``_rerender``: ``_refresh`` is a real discord.py
    ``View`` internal and shadowing it crashes the view on MESSAGE_UPDATE.
    """

    # Memoised reads each page shows, and therefore the ones its footer has to
    # date itself against.
    READ_DB_SIZE = "db_size"
    READ_ACTIVITY = "activity"
    READ_USAGE = "usage"
    READ_ROW_COUNTS = "row_counts"
    READ_TABLE_SIZES = "table_sizes"
    PAGE_READS = {
        PAGE_OVERVIEW: (READ_DB_SIZE,),
        PAGE_GUILDS: (),
        PAGE_USAGE: (READ_ACTIVITY, READ_USAGE),
        PAGE_DATA: (READ_ROW_COUNTS, READ_TABLE_SIZES),
    }

    def __init__(self, cog, author_id, *, timeout=300):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.cog = cog
        self.bot = cog.bot
        self.index = PAGE_OVERVIEW
        # One page at a time: the card is rebuilt in place, so two clicks
        # racing inside one load window could leave self.index disagreeing
        # with whichever edit landed last on the wire.
        self._lock = asyncio.Lock()
        # key -> (value, read_at). Populated on first use, and ONLY by a read
        # that succeeded: memoising a failure would freeze a transient pool
        # error into the card for its whole 5 minute life, with no way to retry
        # short of re-running the command.
        self._reads = {}

    # -- data ---------------------------------------------------------------

    async def _read(self, key, fetch, what):
        """Run one bounded read at most once per card, or serve the memo.

        Returns ``None`` when the read failed, which every renderer turns into
        an explicit "unavailable" line rather than a zero. That failure is NOT
        cached, so the next open of the page retries it.
        """
        cached = self._reads.get(key)
        if cached is not None:
            return cached[0]
        try:
            value = await fetch()
        except Exception:
            log.exception("botstats: %s read failed", what)
            return None
        self._reads[key] = (value, datetime.datetime.now(datetime.timezone.utc))
        return value

    def _taken_at(self, keys):
        """When the data on this page was gathered.

        The in-memory half is always live, so the honest claim is the OLDEST
        memoised read still on display: a figure fetched four minutes ago must
        not be footnoted with the current time.
        """
        times = [self._reads[key][1] for key in keys if key in self._reads]
        return min(times) if times else datetime.datetime.now(datetime.timezone.utc)

    async def _load(self, index):
        """Build one page's sections. Every read is defensive: a DB failure
        degrades that page to an "unavailable" line rather than leaving the
        click on Discord's opaque interaction error."""
        if index == PAGE_OVERVIEW:
            sections = await self._overview_sections()
        elif index == PAGE_GUILDS:
            sections = self._guild_sections()
        elif index == PAGE_USAGE:
            sections = await self._usage_sections()
        else:
            sections = await self._data_sections()
        return sections, self._taken_at(self.PAGE_READS[index])

    async def _overview_sections(self):
        rows = collect_guild_rows(self.bot.guilds)
        guilds, members, missing = guild_totals(rows)
        # discord.py reports nan for the latency until the first heartbeat ack
        # lands; that must render as "unknown", never as "nan ms".
        latency = self.bot.latency
        if latency is None or not math.isfinite(latency):
            latency = None

        uptime = read_process_uptime_seconds()
        uptime_is_process = uptime is not None
        if uptime is None:
            # /proc unavailable: fall back to the cog's own load time. Shorter
            # than the truth after a hot reload, and labeled as such.
            loaded_at = getattr(self.cog, "loaded_at", None)
            if loaded_at is not None:
                uptime = (
                    datetime.datetime.now(datetime.timezone.utc) - loaded_at
                ).total_seconds()

        db_bytes = await self._read(
            self.READ_DB_SIZE,
            lambda: fetch_database_size(self.bot.db_pool),
            "database size",
        )

        return render_overview(
            guilds=guilds,
            members=members,
            guilds_without_count=missing,
            cached_users=len(self.bot.users),
            channels=count_channels(rows),
            latency_seconds=latency,
            uptime_seconds=uptime,
            uptime_is_process=uptime_is_process,
            rss_bytes=read_rss_bytes(),
            shards=self.bot.shard_count or 1,
            python_version=platform.python_version(),
            discord_version=discord.__version__,
            db_bytes=db_bytes,
        )

    def _guild_sections(self):
        """The central question of this dashboard - and the only page with no
        query at all: one O(N) pass plus one sort over ``bot.guilds``."""
        rows = collect_guild_rows(self.bot.guilds)
        guilds, members, missing = guild_totals(rows)
        return render_top_guilds(
            top_guild_rows(rows, TOP_GUILDS_LIMIT),
            guilds=guilds,
            members=members,
            guilds_without_count=missing,
        )

    async def _usage_sections(self):
        # Only the DB halves are memoised. The since-boot counters are in memory
        # and cost nothing to re-read, so re-opening Usage always shows what the
        # process has been asked to do since the last look.
        #
        # ONE "today" for both blocks: they are windows on the same card, so
        # they must not be able to end on different days (a click at the UTC
        # midnight boundary would otherwise split them).
        today = usage_stats.utc_today()
        activity = await self._read(
            self.READ_ACTIVITY,
            lambda: fetch_observed_activity(self.bot.db_pool, today=today),
            "observed activity",
        )
        persisted = await self._read(
            self.READ_USAGE,
            lambda: usage_stats.fetch_persisted_usage(
                self.bot.db_pool, timeout=QUERY_TIMEOUT, today=today
            ),
            "recorded usage",
        )
        return render_usage(self.cog.usage, activity, persisted)

    async def _data_sections(self):
        row_counts = await self._read(
            self.READ_ROW_COUNTS,
            lambda: fetch_row_counts(self.bot.db_pool),
            "row counts",
        )
        sizes = await self._read(
            self.READ_TABLE_SIZES,
            lambda: fetch_table_sizes(self.bot.db_pool),
            "table sizes",
        )
        table_sizes, total_bytes = sizes if sizes is not None else (None, 0)
        return render_data(row_counts, table_sizes, total_bytes)

    # -- layout -------------------------------------------------------------

    def _rerender(self, sections, taken_at):
        """(Re)assemble the container for the current page, fresh every time."""
        self.clear_items()
        container = discord.ui.Container(accent_colour=random_colour())
        labels = page_labels()
        container.add_item(
            discord.ui.TextDisplay(
                "### {emoji} {title}".format(
                    emoji=PAGE_EMOJI[self.index],
                    title=_("Bot statistics - {page}").format(
                        page=labels[self.index]
                    ),
                )
            )
        )
        container.add_item(discord.ui.Separator())

        for heading, lines in sections:
            container.add_item(
                discord.ui.TextDisplay(
                    "**" + heading + "**\n" + "\n".join(lines)
                )
            )
            container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.ActionRow(
                *(
                    _PageButton(
                        self,
                        index,
                        labels[index],
                        PAGE_EMOJI[index],
                        current=index == self.index,
                    )
                    for index in range(len(labels))
                )
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _("Snapshot taken {when}").format(when=format_dt(taken_at, "T"))
                + " - "
                + _("times out after 5 min")
            )
        )
        self.add_item(container)

    async def start(self):
        """Render the first page before the card is ever sent."""
        sections, taken_at = await self._load(self.index)
        self._rerender(sections, taken_at)

    async def show_page(self, interaction, index):
        try:
            # ACK FIRST, before any read. Discord kills the interaction token
            # 3s after the click, while a page's reads are bounded at
            # QUERY_TIMEOUT (15s) - so on a big install the edit would answer a
            # token that died twelve seconds earlier, and the owner would be
            # left on "This interaction failed" with the page never rendered.
            # The deferred token is good for 15 min, which the reads cannot
            # outlast. A defer that fails is logged by the helper and does not
            # abort the render: refresh_layout still has the message to edit.
            await interactions.defer(interaction, surface="botstats")
            async with self._lock:
                # Load BEFORE moving the cursor: a page that failed to build
                # must not leave the card claiming to be on a page it never
                # rendered.
                sections, taken_at = await self._load(index)
                self.index = index
                self._rerender(sections, taken_at)
                # Mentions suppressed: this layout carries attacker-chosen
                # guild names, and an edit that says nothing inherits the
                # client default (users=True) and re-parses them - the same
                # hazard AuthorLayoutView.on_timeout documents.
                # interaction.message is the fallback target for the sliver of
                # time between ctx.send returning and view.message being set.
                await interactions.refresh_layout(
                    interaction,
                    self.message or getattr(interaction, "message", None),
                    self,
                    surface="botstats",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:
            log.exception("botstats page %s failed", index)
            # Never leave the click on Discord's opaque "This interaction
            # failed" - same discipline as the serverstats card.
            await interactions.notify_failure(interaction, _("Something went wrong."))


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------
class BotStats(commands.Cog):
    """Owner-only bot-wide statistics: the ``?botstats`` dashboard."""

    def __init__(self, bot):
        self.bot = bot
        self.usage = UsageCounters()
        # The persisted half: a bounded buffer the listeners feed and the flush
        # loop drains (cogs/system/usage_stats.py).
        self.buffer = usage_stats.UsageBuffer()
        # UTC day (a date) whose retention prune has already run. In-memory
        # marker: a restart re-runs it once, which is a bounded delete of
        # already-expired rows, i.e. harmless.
        self._prune_day = None
        # Cumulative instrumentation (scale story).
        self._flush_stats = {
            "flushes": 0,
            "rows": 0,
            "dropped": 0,
            "pruned": 0,
            "halved": 0,
        }
        # Fallback uptime source for a host without /proc (see the Overview
        # page). It is the COG's load time, so it is always <= the process
        # uptime, and it is rendered with that caveat spelled out.
        self.loaded_at = datetime.datetime.now(datetime.timezone.utc)

    async def cog_load(self):
        self._flush_loop.start()

    async def cog_unload(self):
        """Stop the loop, THEN write whatever the buffer still holds.

        The order is the whole point. Cancelling first means the in-flight
        iteration (if any) unwinds through _write_usage's ``except
        BaseException`` and hands its drained generation BACK to the buffer -
        but that unwind runs in the LOOP's task, not in this one, so we WAIT for
        it before draining here. Flushing without waiting would read an
        already-drained buffer, and the counters restored a moment later would
        then sit in a buffer nobody writes again.

        asyncio.wait never raises (a timeout or a cancelled child is just a
        result). BOTH awaits below are bounded, which is what makes "shutdown
        can never hang on statistics" true rather than aspirational: the wait on
        the cancelled loop AND the final write, which is otherwise bounded only
        by the pool's own command_timeout (core.main: 60s). A timed-out final
        write cancels the coroutine, whose ``except BaseException`` hands the
        counters back to a buffer nobody will write again - i.e. the timeout
        costs at most the last interval, exactly like a hard crash does.

        The final flush is best effort only: on a clean shutdown the pool
        outlives cog teardown (core.main nests the bot inside the pool's
        context), so the last partial interval is saved instead of dropped, and
        any failure here - timeout included - is logged and swallowed.
        """
        task = self._flush_loop.get_task()
        self._flush_loop.cancel()
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=UNLOAD_CANCEL_TIMEOUT)
        if self.buffer.is_empty:
            return
        try:
            await asyncio.wait_for(self._write_usage(), UNLOAD_FLUSH_TIMEOUT)
        except Exception:
            log.exception("botstats: final usage flush on unload failed")

    async def cog_check(self, ctx):
        return await self.bot.is_owner(ctx.author)

    # -- usage listeners: O(1), no await, no DB ------------------------------

    def _record(self, name, *, slash):
        """Count one completed command in BOTH halves of the usage stats.

        ONE place, so the exactly-once dedup above it cannot ever apply to one
        counter and not the other. The UTC day is captured HERE, at increment
        time: a flush at 00:02 must write the 23:59 completions onto the day
        they happened on, never migrate them into the new day.
        """
        if not name:
            return
        # ONE reading of the clock for both axes: taking the day and the hour
        # separately could straddle a midnight between the two calls and file a
        # 23:59 completion as hour 0 of the day before.
        day, hour = usage_stats.utc_day_hour()
        self.usage.record(name, slash=slash)
        self.buffer.record(day, name, slash=slash, hour=hour)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx):
        command = ctx.command
        if command is None:
            return
        # A hybrid invoked as a slash lands here too (its after-hooks run in the
        # tree's invoke path), so the surface is read off the context rather
        # than assumed - and on_app_command_completion skips hybrids so this
        # invocation is counted exactly once.
        self._record(command.qualified_name, slash=ctx.interaction is not None)

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction, command):
        if is_hybrid_app_command(command):
            return  # already counted by on_command_completion
        self._record(getattr(command, "qualified_name", None), slash=True)

    # -- the flush loop: the ONLY writer ------------------------------------

    @tasks.loop(seconds=FLUSH_INTERVAL)
    async def _flush_loop(self):
        try:
            await self.flush_usage()
        except Exception:
            log.exception("botstats usage flush iteration failed")

    @_flush_loop.before_loop
    async def _before_flush_loop(self):
        await self.bot.wait_until_ready()

    @_flush_loop.error
    async def _flush_loop_error(self, error):
        log.exception("botstats usage flush crashed; restarting", exc_info=error)
        self._flush_loop.restart()

    async def flush_usage(self, today=None):
        """Write the buffered counters, then run the once-a-day prune.

        ``today`` is injectable for tests; it defaults to the current UTC day.
        The buffered rows carry their OWN day, so a tick that straddles midnight
        writes each counter onto the day it was collected on - only the prune
        below cares about which day it is NOW.
        """
        if today is None:
            today = usage_stats.utc_today()
        await self._write_usage()
        await self._maybe_prune(today)

    async def _write_usage(self):
        """One additive upsert for everything counted since the last tick.

        AT-LEAST-ONCE, stated honestly (same contract as the serverstats
        collector): a cancellation landing after the upsert committed but before
        this returns hands the same generation back to the buffer, so the next
        flush can recount a batch. Counters, not money - a duplicated interval
        nudges a daily total, and the alternative (dropping the batch on every DB
        blip) loses real data far more often.
        """
        drained = self.buffer.drain()
        if drained.dropped:
            # Logged ONCE per flush, never per command: the cap exists to keep
            # memory bounded, and a per-event log would be its own flood.
            self._flush_stats["dropped"] += drained.dropped
            log.warning(
                "botstats: usage buffer cap reached, dropped %d command-day "
                "key(s) this interval",
                drained.dropped,
            )
        if drained.is_empty:
            return
        payload = usage_stats.build_flush_payload(drained)
        try:
            await self.bot.db_pool.execute(usage_stats.FLUSH, *payload)
        except BaseException:
            # Hand the counters back so a DB blip costs nothing; restore goes
            # through the same cap, so the buffer stays bounded either way.
            #
            # BaseException, not Exception, ON PURPOSE: cog_unload cancels this
            # loop and THEN runs a final flush, so the very window that matters
            # is a CancelledError thrown into this await. CancelledError is a
            # BaseException, so an `except Exception` would skip the restore and
            # the drained generation would be gone before the final flush could
            # write it. Nothing is swallowed - the exception is re-raised.
            self.buffer.restore(drained)
            raise
        self._flush_stats["flushes"] += 1
        self._flush_stats["rows"] += len(drained.rows)
        log.debug("botstats usage flush: %d command-day row(s)", len(drained.rows))

    async def _maybe_prune(self, today):
        """The once-a-day maintenance hook: the retention prune, then the decay.

        Both ride this one gate because both are cheap, bounded and pointless to
        run more often - and because the day marker below is what makes "once a
        day" true for the pair rather than for one of them.
        """
        if self._prune_day == today:
            return
        cutoff = today - datetime.timedelta(days=usage_stats.RETENTION_DAYS)
        deleted = 0
        # Never name a throwaway loop variable ``_`` in this codebase: ``_`` is
        # the gettext translation callable by house convention (tools.i18n).
        for _batch in range(usage_stats.PRUNE_MAX_BATCHES):
            row = await self.bot.db_pool.fetchrow(
                usage_stats.PRUNE, cutoff, usage_stats.PRUNE_BATCH_SIZE
            )
            batch = int(row["rows"]) if row else 0
            deleted += batch
            if batch < usage_stats.PRUNE_BATCH_SIZE:
                break
        await self._decay_hourly(today)
        # Marked only after the batches ran, so a failed prune is retried on the
        # next tick instead of being lost for the whole day.
        self._prune_day = today
        self._flush_stats["pruned"] += deleted
        if deleted:
            log.info(
                "botstats usage prune: removed %d row(s) older than %s",
                deleted,
                cutoff,
            )

    async def _decay_hourly(self, today):
        """Age the weekly usage profile: halve all 168 counts, once a week.

        Two statements, both trivial and both bounded by the 168-row ceiling of
        the table they touch:

        1. seed the marker row if this install has never had one (a no-op on
           every call but the first);
        2. the decay itself, which CLAIMS the week with ``UPDATE ... WHERE
           halved_on <= today - 7 RETURNING`` and halves only when that claim
           returned a row.

        WHY THE CADENCE LIVES IN THE DATABASE. The prune above marks its day in
        memory (``self._prune_day``) and can afford to: a restart re-runs it, and
        re-deleting already-expired rows is a no-op. Halving is not idempotent
        and this bot restarts on every deploy, so an in-memory weekly marker
        would halve the profile several times a day and flatten it to zeros
        inside a week. The claim-by-UPDATE-RETURNING shape is the repo's standard
        at-most-once idiom (tools/retention.claim_due_guild, the dashboard action
        queue), and its row lock also makes a racing second caller a no-op rather
        than a second halving.

        A failure here propagates: ``_prune_day`` is only marked after this
        returns, so the whole hook is retried on the next tick.
        """
        await self.bot.db_pool.execute(usage_stats.SEED_HOURLY_STATE, today)
        row = await self.bot.db_pool.fetchrow(
            usage_stats.DECAY_HOURLY, today, usage_stats.HOURLY_HALVE_DAYS
        )
        halved = int(row["rows"]) if row else 0
        if halved:
            self._flush_stats["halved"] += halved
            log.info(
                "botstats usage profile: halved %d hourly slot(s) (weekly decay)",
                halved,
            )

    # -- the command ---------------------------------------------------------

    @commands.command(hidden=True, name="botstats", aliases=["bstats"])
    async def botstats(self, ctx):
        """Open the owner bot-statistics dashboard (4 pages)."""
        async with ctx.typing():
            view = BotStatsDashboard(self, ctx.author.id)
            await view.start()
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot):
    await bot.add_cog(BotStats(bot))

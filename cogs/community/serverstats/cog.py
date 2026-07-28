"""Purpose: the Discord side of the collectors - three zero-await listeners and
the ONE batched flush loop that turns their counters into rows.

The shape is the voice-XP pattern (cogs/community/voice_xp.py): listeners do
pure in-memory dict work and never await, and a single bot-wide
:class:`~discord.ext.tasks.Loop` does all the I/O, so the DB write rate is a
function of TIME, not of traffic - one round trip every
:data:`FLUSH_INTERVAL` seconds no matter how many messages the bot saw.

Collection is ON for every guild (no config in v1) and stores AGGREGATES ONLY:
counts per (guild, channel, UTC day) and per (guild, UTC day). No message
content, no author, no user id of any kind is read, kept or written.

Scale story: the non-countable branch of on_message (a bot author, a DM) is two
attribute reads and a return - zero awaits, zero allocations. A counted message
costs one getattr, one tuple key and one dict bump. All the real work is the
flush: ONE statement per tick bot-wide, plus one snapshot statement per UTC day
and one bounded prune run per UTC day.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from . import buffer, queries, rollups, views

log = logging.getLogger(__name__)

# One batched write every 5 minutes, bot-wide. Same clock as the voice-XP sweep:
# it bounds the DB write rate independently of traffic, and losing at most one
# interval of counters to a hard crash is acceptable for aggregates.
FLUSH_INTERVAL = 300

# How long a day of statistics is kept. Enforced by the lazy prune below, not by
# a cron: the collector owns its own retention.
RETENTION_DAYS = 90

# The prune deletes at most PRUNE_BATCH_SIZE rows per table per statement and
# runs at most PRUNE_MAX_BATCHES statements per day, so its worst case is
# bounded (100k rows/day) and its steady state is one or two short batches.
PRUNE_BATCH_SIZE = 5000
PRUNE_MAX_BATCHES = 20

# How long cog_unload waits for a cancelled in-flight flush to unwind before it
# runs the final flush. Generous next to a single upsert, tiny next to a
# shutdown: the point is that teardown is bounded even if the pool is wedged.
UNLOAD_CANCEL_TIMEOUT = 5


class ServerStats(commands.Cog):
    """Aggregate activity collectors: message/join/leave counters and a daily
    member-count snapshot, batched into Postgres every few minutes."""

    def __init__(self, bot):
        self.bot = bot
        self._buffer = buffer.StatsBuffer()
        # UTC day (int) whose member-count snapshot has been written, and whose
        # prune has run. In-memory markers: a restart re-runs both once, which
        # is harmless (the snapshot REPLACES the day's value, the prune is a
        # bounded delete of already-expired rows).
        self._snapshot_day = None
        self._prune_day = None
        # Cumulative instrumentation (scale story).
        self._stats = {
            "flushes": 0,
            "message_rows": 0,
            "day_rows": 0,
            "snapshots": 0,
            "pruned": 0,
            "dropped": 0,
        }

    async def cog_load(self):
        self._flush_loop.start()

    async def cog_unload(self):
        task = self._flush_loop.get_task()
        self._flush_loop.cancel()
        if task is not None and not task.done():
            # WAIT for the cancellation to actually land before flushing. A flush
            # cancelled mid-write hands its counters back to the buffer while it
            # unwinds (see _write_buffer), and that unwind runs in the LOOP's
            # task, not this one: flushing here without waiting would read an
            # already-drained buffer and the restored counters would then sit in
            # a buffer nobody writes again. asyncio.wait never raises (a timeout
            # or a cancelled child is just a result), and the timeout keeps the
            # promise that shutdown can never hang on statistics.
            await asyncio.wait({task}, timeout=UNLOAD_CANCEL_TIMEOUT)
        # Best effort only: on a clean shutdown the pool outlives cog teardown
        # (core.main nests the bot inside the pool's context), so the last
        # partial interval is saved instead of dropped. Any failure here is
        # logged and swallowed - shutdown must never hang or raise on stats.
        if self._buffer.is_empty:
            return
        try:
            await self._write_buffer()
        except Exception:
            log.exception("serverstats: final flush on unload failed")

    # ------------------------------------------------------------------
    # Listeners: in-memory only, ZERO awaits, ZERO DB
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message):
        """Count one guild message. HOT GLOBAL event: every message on every
        guild lands here, so the non-countable path (DMs, bots and webhooks)
        must be a couple of attribute reads and a return - no await, no
        allocation, no DB. Threads and forum posts are rolled up onto their
        PARENT channel so a channel's history stays stable when a thread dies.
        """
        if message.guild is None or message.author.bot:
            return
        channel = message.channel
        self._buffer.record_message(
            message.guild.id,
            getattr(channel, "parent_id", None) or channel.id,
            buffer.utc_day(),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Count one human join (bots joining are infrastructure, not growth)."""
        if member.bot:
            return
        self._buffer.record_join(member.guild.id, buffer.utc_day())

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        """Count one human departure (leave, kick or ban - all are a departure)."""
        if member.bot:
            return
        self._buffer.record_leave(member.guild.id, buffer.utc_day())

    # ------------------------------------------------------------------
    # The flush loop
    # ------------------------------------------------------------------
    @tasks.loop(seconds=FLUSH_INTERVAL)
    async def _flush_loop(self):
        try:
            await self.flush()
        except Exception:
            log.exception("serverstats flush iteration failed")

    @_flush_loop.before_loop
    async def _before_flush_loop(self):
        await self.bot.wait_until_ready()

    @_flush_loop.error
    async def _flush_loop_error(self, error):
        log.exception("serverstats flush crashed; restarting", exc_info=error)
        self._flush_loop.restart()

    async def flush(self, day=None):
        """Write the buffered counters, then run the once-a-day chores.

        ``day`` is injectable for tests; it defaults to the current UTC day. The
        buffered rows carry their OWN day, so a tick that straddles midnight
        writes each counter onto the day it was collected on - only the chores
        below care about which day it is NOW.
        """
        if day is None:
            day = buffer.utc_day()
        await self._write_buffer()
        await self._maybe_snapshot_members(day)
        await self._maybe_prune(day)

    async def _write_buffer(self):
        """One batched upsert for everything collected since the last tick.

        AT-LEAST-ONCE, stated honestly: a cancellation that lands after the
        upsert committed but before this returns hands the same generation back
        to the buffer, so the next flush can recount a batch (aggregates only,
        rarissime). Counters, not money - a duplicated interval nudges a daily
        total, and the alternative (dropping the batch on every DB blip) loses
        real data far more often.
        """
        drained = self._buffer.drain()
        if drained.dropped_messages or drained.dropped_days:
            # Logged ONCE per flush, never per message: the cap exists to keep
            # memory bounded, and a per-event log would be its own flood.
            self._stats["dropped"] += (
                drained.dropped_messages + drained.dropped_days
            )
            log.warning(
                "serverstats: buffer cap reached, dropped %d channel-day key(s) "
                "and %d guild-day key(s) this interval",
                drained.dropped_messages,
                drained.dropped_days,
            )
        if drained.is_empty:
            return
        payload = buffer.build_flush_payload(drained)
        try:
            await self.bot.db_pool.execute(queries.FLUSH, *payload)
        except BaseException:
            # Hand the counters back so a DB blip costs nothing; restore goes
            # through the same caps, so the buffer stays bounded either way.
            #
            # BaseException, not Exception, ON PURPOSE: cog_unload cancels the
            # flush loop and THEN runs a final flush, so the very window that
            # matters is a CancelledError thrown into this await. CancelledError
            # is a BaseException, so an `except Exception` would skip the restore
            # and the drained generation would be gone before the final flush
            # could write it. Nothing is swallowed - the exception is re-raised.
            self._buffer.restore(drained)
            raise
        self._stats["flushes"] += 1
        self._stats["message_rows"] += len(drained.messages)
        self._stats["day_rows"] += len(drained.days)
        log.debug(
            "serverstats flush: %d channel-day row(s), %d guild-day row(s)",
            len(drained.messages),
            len(drained.days),
        )

    async def _maybe_snapshot_members(self, day):
        """On the first flush of a new UTC day, record every guild's member count.

        Reads the gateway cache only (``guild.member_count``), so this is O(number
        of guilds) of pure memory work once a day, batched into a single upsert.
        Guilds whose count is not available yet (no members intent data) are
        skipped rather than written as zero.
        """
        if self._snapshot_day == day:
            return
        guild_ids = []
        member_counts = []
        for guild in self.bot.guilds:
            count = guild.member_count
            if count is None:
                continue
            guild_ids.append(guild.id)
            member_counts.append(int(count))
        if not guild_ids:
            # Nothing to write, and nothing to REMEMBER either: the cache is
            # simply not populated yet (the first flush can land before any
            # GUILD_CREATE carried a member_count). Marking the day here would
            # burn the guild's one snapshot slot for the whole UTC day on a
            # purely transient cold cache; leaving it unmarked costs one extra
            # cheap in-memory sweep per tick until the counts show up.
            return
        await self.bot.db_pool.execute(
            queries.SNAPSHOT_MEMBER_COUNT,
            guild_ids,
            buffer.day_to_date(day),
            member_counts,
        )
        self._stats["snapshots"] += 1
        # Marked only after the write succeeded, so a failed snapshot is retried
        # on the next tick instead of being lost for the whole day.
        self._snapshot_day = day

    async def _maybe_prune(self, day):
        """Drop everything older than RETENTION_DAYS, once a day, in bounded batches."""
        if self._prune_day == day:
            return
        cutoff = buffer.day_to_date(day - RETENTION_DAYS)
        deleted_messages = 0
        deleted_days = 0
        # Never name a throwaway loop variable ``_`` in this codebase: ``_`` is
        # the gettext translation callable by house convention (tools.i18n), so
        # binding it here would shadow it for the rest of the scope.
        for _batch in range(PRUNE_MAX_BATCHES):
            row = await self.bot.db_pool.fetchrow(
                queries.PRUNE, cutoff, PRUNE_BATCH_SIZE
            )
            batch_messages = int(row["messages"]) if row else 0
            batch_days = int(row["days"]) if row else 0
            deleted_messages += batch_messages
            deleted_days += batch_days
            if batch_messages < PRUNE_BATCH_SIZE and batch_days < PRUNE_BATCH_SIZE:
                break
        self._prune_day = day
        self._stats["pruned"] += deleted_messages + deleted_days
        if deleted_messages or deleted_days:
            log.info(
                "serverstats prune: removed %d message row(s) and %d day row(s) "
                "older than %s",
                deleted_messages,
                deleted_days,
                cutoff,
            )

    # ------------------------------------------------------------------
    # ST3: /serverstats - a PUBLIC read of the aggregates above (no
    # manage_guild gate: this is a guild-wide statistic, not a moderation
    # tool, and nothing it shows is per-member). The card (views.py) owns
    # every rendering decision; this command only gathers the reads.
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="serverstats")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def serverstats(self, ctx):
        """Show this server's activity and growth statistics."""
        await self.cmd_serverstats(ctx)

    async def cmd_serverstats(self, ctx):
        """The ``/serverstats`` body - gathers every read the card needs, then
        hands them to :class:`~.views.ServerStatsCard`. A read-only aggregate
        query can run comfortably under the 3s slash budget, but the command
        still defers first (house discipline, and cheap insurance against a
        slow connection pool)."""
        await ctx.defer()
        pool = self.bot.db_pool
        guild = ctx.guild

        since = await rollups.data_since(pool, guild.id)
        overview = await rollups.overview(
            pool, guild.id, days=rollups.DEFAULT_OVERVIEW_DAYS, since=since
        )
        top_channels = await rollups.top_channels(
            pool,
            guild.id,
            days=rollups.DEFAULT_OVERVIEW_DAYS,
            limit=views.TOP_CHANNELS_LIMIT,
        )
        growth = await rollups.growth(
            pool, guild.id, days=rollups.DEFAULT_SERIES_DAYS
        )
        # watched_days is threaded straight from growth: same window, so this
        # is the free reuse rollups.activity_series documents - zero extra
        # query for the honesty check that tells a silent day from a blind one.
        activity = await rollups.activity_series(
            pool,
            guild.id,
            days=rollups.DEFAULT_SERIES_DAYS,
            since=since,
            watched_days=growth.watched_days,
        )
        leveling_cog = self.bot.get_cog("Leveling")
        leveling_enabled = bool(leveling_cog and leveling_cog.is_enabled(guild.id))
        retention_report = await rollups.retention(
            pool,
            guild.id,
            weeks=rollups.DEFAULT_RETENTION_WEEKS,
            leveling=leveling_enabled,
            since=since,
        )

        view = views.ServerStatsCard(
            pool,
            guild,
            # The invoking MEMBER (guild_only, so ctx.author always is one):
            # the card cuts the top-channels ranking to this member's own
            # channel visibility, and a Member re-resolved from the partial
            # member cache would be None. Never guild.get_member here.
            ctx.author,
            since,
            overview,
            top_channels,
            growth,
            activity,
            retention_report,
        )
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

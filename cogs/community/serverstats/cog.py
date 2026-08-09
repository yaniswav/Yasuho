"""Purpose: the Discord side of the collectors - three zero-await listeners and
the ONE batched flush loop that turns their counters into rows.

The shape is the voice-XP pattern (cogs/community/leveling/voice_xp.py): listeners do
pure in-memory dict work and never await, and a single bot-wide
:class:`~discord.ext.tasks.Loop` does all the I/O, so the DB write rate is a
function of TIME, not of traffic - one round trip every
:data:`FLUSH_INTERVAL` seconds no matter how many messages the bot saw.

Collection is ON for every guild (no config in v1) and stores AGGREGATES ONLY:
counts per (guild, channel, UTC day) and per (guild, UTC day). No message
content, no author, no user id of any kind is read, kept or written.

The same shape carries the WEEKLY DIGEST (digest.py): one hourly loop bot-wide,
never a timer per guild, delivering to a bounded number of guilds per tick.

Scale story: the non-countable branch of on_message (a bot author, a DM) is two
attribute reads and a return - zero awaits, zero allocations. A counted message
costs one getattr, one tuple key and one dict bump. All the real work is the
flush: ONE statement per tick bot-wide, plus one snapshot statement per UTC day
and one bounded prune run per UTC day. The digest loop adds ONE indexed
candidate query per hourly tick (168 a week, and the ~167 that find nothing left
to deliver are an index-only walk of the opted-in guilds alone), plus per
delivered guild a claim, 2-3 range reads, one primary-key settings read for the
locale and one message - capped at digest.FAN_OUT_LIMIT guilds per tick, see
run_digest_once for the ceiling.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import asyncio
import datetime
import logging

import discord
from discord.ext import commands, tasks

from . import buffer, digest, queries, rollups, views
from tools import i18n
from tools.i18n import _
from tools.snowflake import coerce_id

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

# The weekly digest ticks HOURLY, bot-wide - one loop for the whole fleet, never
# a timer per guild. The first tick of a Monday normally delivers the whole
# fleet; every later tick of the week finds the queue already drained and costs
# one index walk, which is the price of being able to recover a Monday the bot
# spent offline. Each tick drains at most digest.FAN_OUT_LIMIT guilds. See
# run_digest_once for the pacing story.
DIGEST_TICK_HOURS = 1

# ... and how long the final flush itself gets. Without it the last write is
# bounded only by the pool's command_timeout (core.main: 60s), so a wedged DB
# would hold a clean shutdown open for a minute over statistics. Losing the
# last interval to a wedged pool is the same loss a kill -9 already causes.
UNLOAD_FLUSH_TIMEOUT = 5


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
            "digests": 0,
        }

    async def cog_load(self):
        self._flush_loop.start()
        self._digest_loop.start()

    async def cog_unload(self):
        # The digest loop owns no buffered state (it claims in the database and
        # posts, nothing is held in memory), so cancelling it is the whole of its
        # teardown - unlike the flush loop below, which has counters to save.
        self._digest_loop.cancel()
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
        #
        # BOUNDED, same as the cancel wait above: without it the final write is
        # bounded only by the pool's own command_timeout (60s), so a wedged pool
        # would hold a clean shutdown open for a minute over statistics. A
        # timed-out write cancels _write_buffer, whose ``except BaseException``
        # hands the counters back to a buffer nobody will write again - i.e. the
        # timeout costs at most the last interval, exactly like a hard crash.
        if self._buffer.is_empty:
            return
        try:
            await asyncio.wait_for(self._write_buffer(), UNLOAD_FLUSH_TIMEOUT)
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
    # D1: the weekly digest - ONE hourly loop, bot-wide
    # ------------------------------------------------------------------
    @tasks.loop(hours=DIGEST_TICK_HOURS)
    async def _digest_loop(self):
        try:
            await self.run_digest_once()
        except Exception:
            # A pass that raises must not kill the loop: the next hour's pass
            # re-reads the same candidates, minus whatever was already claimed.
            log.exception("serverstats digest iteration failed")

    @_digest_loop.before_loop
    async def _before_digest_loop(self):
        # READY is what makes ``get_guild``/``get_channel`` trustworthy; a pass
        # over a cold cache would read every guild as "gone" and burn its claim.
        await self.bot.wait_until_ready()

    @_digest_loop.error
    async def _digest_loop_error(self, error):
        log.exception("serverstats digest crashed; restarting", exc_info=error)
        self._digest_loop.restart()

    async def run_digest_once(self, now=None):
        """One bounded delivery pass. Returns how many digests it posted.

        PACING, stated honestly. The loop ticks every hour, all week, and each
        tick takes at most :data:`digest.FAN_OUT_LIMIT` (50) guilds, claims and
        posts to each, and leaves the rest to the following hours - the
        candidate query excludes the guilds already delivered this ISO week, so
        every later tick makes progress instead of re-reading the same head of
        the list, and a tick with nothing left to do costs ONE index-only walk
        of the opted-in guilds (see the measurement on digest.CANDIDATES).

        In practice every digest lands on the first Monday tick after the week
        closes: that is the first hour at which the reported week exists, and a
        healthy fleet drains in one tick. The later ticks are not a load-
        spreading device, they are the RECOVERY - a bot that was down for the
        whole of Monday delivers on Tuesday instead of dropping the week for the
        entire fleet, permanently. This cannot double-post (digest.py rule 5):
        the claim is keyed by the CURRENT ISO week and digest_period returns the
        same reported week on every day of it, so a later tick can only produce
        a LATE digest, never a second one and never a stale week.

        The ceiling that follows, said out loud: 50 guilds x 168 hourly ticks =
        8400 opted-in guilds per week. Past that a guild slips to the next week
        and misses one - it still never receives two. This bot is nowhere near
        8400 servers WITH the digest turned on; when it is, the fix is to raise
        FAN_OUT_LIMIT, whose per-guild cost is four tiny indexed queries (five
        with leveling) plus one message.

        Per guild: one claim, two range reads (three with leveling), one
        primary-key read of guild_settings for the locale - i18n.
        resolve_guild_locale, an LRU miss at weekly frequency - and at most one
        message. No timer, no state and no per-guild task anywhere.
        """
        pool = getattr(self.bot, "db_pool", None)
        if pool is None or not self.bot.is_ready():
            return 0
        now = now or datetime.datetime.now(datetime.timezone.utc)

        # The CURRENT ISO week is the claim key ("this guild has had its digest
        # for this week"); the REPORT covers the week before it. Two different
        # weeks on purpose - see digest.digest_period.
        week = digest.period_key(now.date())
        rows = await digest.candidates(pool, week, digest.FAN_OUT_LIMIT)
        delivered = 0
        for row in rows:
            try:
                if await self._deliver_digest(pool, row, week, now.date()):
                    delivered += 1
            except Exception:
                # Claimed and lost: no retry storm, one line, next guild.
                log.exception(
                    "serverstats digest: delivery to guild %s failed",
                    row["guild_id"],
                )
        if delivered:
            self._stats["digests"] += delivered
            log.info("serverstats digest: posted %d digest(s)", delivered)
        return delivered

    async def _deliver_digest(self, pool, row, week, today):
        """Deliver ONE guild's digest. True when a message was actually posted.

        CLAIM FIRST, then look at whether we can post. Every PERMANENT failure
        below (guild gone, channel deleted, permissions revoked, no data)
        therefore leaves the row claimed and logs exactly one line: the
        alternative is retrying against a channel that will still be missing in
        an hour, 168 times a week, for every broken guild in the fleet.

        The ONE exception is checked before the claim: a guild held as an
        UNAVAILABLE stub (an outage, or a re-IDENTIFY, which re-adds every guild
        as a channel-less stub while is_ready() is still true - see
        cogs/config/tickets/lifecycle.py for the same trap) would read as "no
        channel" for every server at once. That is transient, so it is skipped
        WITHOUT claiming and a later tick of the same week delivers it.

        Queries, counted honestly: the claim, then - only for a guild that gets
        this far - the two range reads of digest.collect (three with leveling),
        then ONE primary-key read of guild_settings for the locale, which is the
        LRU-backed settings.get_guild inside i18n.resolve_guild_locale and is a
        miss at weekly frequency.
        """
        guild_id = int(row["guild_id"])
        # The stored id is untrusted: the dashboard writes this same key and
        # JavaScript serialises a snowflake as a string.
        channel_id = coerce_id(row["channel_id"])
        guild = self.bot.get_guild(guild_id)
        if guild is not None and guild.unavailable:
            log.debug(
                "serverstats digest: guild %s is unavailable; retrying on a "
                "later tick",
                guild_id,
            )
            return False
        if not await digest.claim(pool, guild_id, week):
            # Another tick (or another process) is delivering this one.
            return False

        if guild is None or guild.me is None:
            log.warning(
                "serverstats digest: guild %s is not in this process' cache; "
                "skipping its %s digest",
                guild_id,
                week,
            )
            return False
        channel = guild.get_channel(channel_id) if channel_id else None
        # Not just "does it resolve": the dashboard writes this key too, and a
        # category or a forum resolves perfectly well and cannot be posted in.
        # Duck-typed rather than an isinstance against discord.abc.Messageable so
        # the check states exactly what the next line needs.
        if channel is None or not callable(getattr(channel, "send", None)):
            log.warning(
                "serverstats digest: guild %s has no usable digest channel (%r)",
                guild_id,
                row["channel_id"],
            )
            return False
        missing = digest.missing_permissions(channel.permissions_for(guild.me))
        if missing:
            log.warning(
                "serverstats digest: missing %s in channel %s of guild %s",
                ", ".join(missing),
                channel.id,
                guild_id,
            )
            return False

        report = await digest.collect(
            pool, guild_id, today, leveling=self._leveling_enabled(guild_id)
        )
        if not report.has_data:
            # NOT observed is not "nothing happened" (rollups' rule 1): a guild
            # the collector never watched that week has nothing to report, and a
            # weekly "we saw nothing" message would be pure noise.
            log.info(
                "serverstats digest: guild %s has no observed day in %s; "
                "nothing posted",
                guild_id,
                report.week,
            )
            return False

        # The digest is a PUBLIC, per-guild artifact the whole server reads, so
        # it renders in the GUILD's locale - there is no invoker here at all.
        locale = await i18n.resolve_guild_locale(self.bot, guild)
        with i18n.locale(locale):
            embed = digest.render(report, guild.name)
        try:
            await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            # WARNING without a traceback: the interesting part is which guild
            # and what Discord said, and a fleet-wide outage must not print a
            # stack per guild.
            log.warning(
                "serverstats digest: could not post in channel %s of guild %s: %r",
                channel.id,
                guild_id,
                exc,
            )
            return False
        return True

    def _leveling_enabled(self, guild_id):
        """Whether leveling runs here (in-memory read, no query)."""
        cog = self.bot.get_cog("Leveling")
        return bool(cog and cog.is_enabled(guild_id))

    # ------------------------------------------------------------------
    # ST3: /serverstats - a PUBLIC read of the aggregates above (no
    # manage_guild gate: this is a guild-wide statistic, not a moderation
    # tool, and nothing it shows is per-member). The card (views.py) owns
    # every rendering decision; this command only gathers the reads.
    # ------------------------------------------------------------------
    # A GROUP with a fallback, not a plain command: the card keeps its own
    # invocation (``?serverstats`` unchanged, ``/serverstats show`` on the slash
    # side, the shape /autoroom, /warnings and /playlist already use) and the
    # manage_guild digest controls hang off the same surface instead of
    # inventing a second top-level name for the same feature.
    #
    # ``invoke_without_command=True`` means the PUBLIC card callback does not run
    # (and its checks are not applied) when a subcommand is invoked - each
    # subcommand below carries its own gate, which is what lets a public read and
    # a manager-only configuration live under one name.
    @commands.hybrid_group(
        name="serverstats", fallback="show", invoke_without_command=True
    )
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def serverstats(self, ctx):
        """Show this server's activity and growth statistics."""
        await self.cmd_serverstats(ctx)

    # -- the weekly digest ------------------------------------------------
    # manage_guild + guild_only on the group AND on each leaf, so no path into
    # them is ungated. Stated precisely, because the check is not the same thing
    # as visibility: Discord only honours ``default_member_permissions`` on a
    # TOP-LEVEL command, and this tree's top level is the deliberately public
    # card, so ``/serverstats digest set`` is LISTED in every member's picker
    # and refused at invoke time. That is the same trade every mixed group in
    # this repo makes; the alternative is a second top-level name for one
    # feature.
    @serverstats.group(name="digest")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def serverstats_digest(self, ctx):
        """Configure the weekly server digest (post last week's stats)."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @serverstats_digest.command(name="set")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(
        channel="Where the weekly digest is posted, once a week."
    )
    async def serverstats_digest_set(self, ctx, channel: discord.TextChannel):
        """Turn the weekly digest on and choose where it is posted."""
        # Preflight BEFORE writing anything: a digest configured onto a channel
        # the bot cannot post in would look set up and only fail, silently, in a
        # log line on a Monday morning. Checked again at delivery time, because
        # an overwrite can change any time in between.
        #
        # The confirmation says "once a week, normally on Monday" and not "every
        # Monday" on purpose: enabling the digest mid-week makes this guild a
        # candidate on the very next hourly tick, so its FIRST digest - the last
        # complete week, labelled with its exact window in the footer - can
        # arrive today. Every one after that lands on a Monday.
        missing = digest.missing_permissions(
            channel.permissions_for(ctx.guild.me)
        )
        if missing:
            return await ctx.send(
                _("I need these permissions in {channel} first: {permissions}.").format(
                    channel=channel.mention,
                    permissions=digest.describe_permissions(missing),
                )
            )
        await digest.set_channel(self.bot.db_pool, ctx.guild.id, channel.id)
        await ctx.send(
            _(
                "Weekly digest on - I will post the last complete week's "
                "statistics in {channel} once a week, normally on Monday. "
                "Weeks I did not observe are skipped rather than reported as "
                "empty."
            ).format(channel=channel.mention),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @serverstats_digest.command(name="off")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def serverstats_digest_off(self, ctx):
        """Turn the weekly digest off."""
        # DELETES the key rather than storing a neutral value, so a server that
        # turns the digest off is exactly as unconfigured as one that never
        # turned it on (see digest.clear_channel).
        await digest.clear_channel(self.bot.db_pool, ctx.guild.id)
        await ctx.send(_("Weekly digest off. Nothing else changes - I keep collecting."))

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
        leveling_enabled = self._leveling_enabled(guild.id)
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

"""Daily bounded cleanup for avatars and departed guilds."""

import asyncio
import logging
import os

from discord.ext import commands, tasks

from tools import backup, retention

log = logging.getLogger(__name__)

# A maintenance tick whose observed guild count has collapsed to under this
# fraction of the last healthy count is treated as a partial gateway state and
# skipped, so a reconnect can never enrol live guilds as orphans.
GUILD_COUNT_HEALTH_RATIO = 0.5

# backups/ lives at the repo root; this file is cogs/system/retention.py.
_BACKUPS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "backups")
)


class DataRetention(commands.Cog):
    """Apply the approved data lifecycle without an external scheduler."""

    def __init__(self, bot):
        self.bot = bot
        # Operator control (?purges pause/resume): in-memory, resets on restart.
        self._paused = False
        # Guild count of the last successful pass; guards against acting on a
        # transiently partial gateway state (see _is_healthy).
        self._last_healthy_guild_count = None
        # Backup guard state (see _check_backups): where dumps live, and whether
        # the once-per-boot integrity probe has already run this process.
        self._backups_dir = _BACKUPS_DIR
        self._backup_verified = False
        self.maintenance.start()

    def cog_unload(self):
        self.maintenance.cancel()

    async def cog_check(self, ctx):
        # The only commands this cog exposes are the owner-only ?purges controls.
        return await self.bot.is_owner(ctx.author)

    def _is_healthy(self):
        """Guard destructive maintenance against a partial gateway state.

        ``wait_until_ready`` only gates the FIRST tick; a later tick can fire
        mid-reconnect when ``bot.guilds``/``bot.get_guild`` are transiently
        partial. Acting then could enrol an active guild as an orphan and even
        let the purge-time recheck see ``None`` for a guild the bot is really in.
        So every tick re-gates: the bot must be ready, must see at least one
        guild, and (once a healthy count is on record) must not have lost more
        than half of it.
        """
        if not self.bot.is_ready():
            return False
        count = len(getattr(self.bot, "guilds", ()))
        if count == 0:
            log.warning("Skipping retention pass: bot reports zero guilds")
            return False
        last = self._last_healthy_guild_count
        if last is not None and count < last * GUILD_COUNT_HEALTH_RATIO:
            log.warning(
                "Skipping retention pass: guild count %s is far below the last "
                "healthy count %s (likely a partial gateway state)",
                count,
                last,
            )
            return False
        return True

    async def run_once(self):
        """Run one bounded maintenance pass; exposed for deterministic tests."""
        purged_guilds = 0
        avatar_rows = 0
        avatar_bytes = 0
        scheduled_guilds = await retention.reconcile_guild_jobs(
            self.bot.db_pool,
            (guild.id for guild in getattr(self.bot, "guilds", ())),
        )

        for _ in range(retention.GUILD_PURGES_PER_RUN):
            job = await retention.claim_due_guild(self.bot.db_pool)
            if job is None:
                break
            guild_id = int(job["guild_id"])

            # A rejoin is authoritative even if its gateway event raced this
            # maintenance tick. Cancel instead of deleting an active guild.
            if self.bot.get_guild(guild_id) is not None:
                await retention.cancel_guild_purge(
                    self.bot.db_pool, guild_id
                )
                log.info(
                    "Cancelled retention purge for active guild %s", guild_id
                )
                continue

            try:
                counts = await retention.purge_claimed_guild(
                    self.bot.db_pool, guild_id
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await retention.release_guild_claim(
                    self.bot.db_pool, guild_id, exc
                )
                log.exception("Guild retention purge failed for %s", guild_id)
                continue

            if counts is None:
                continue
            retention.invalidate_guild_caches(self.bot, guild_id)
            purged_guilds += 1
            log.info(
                "Purged departed guild %s retention data (%s rows)",
                guild_id,
                sum(counts.values()),
            )

        for _ in range(retention.AVATAR_PRUNE_MAX_BATCHES):
            deleted, reclaimed = await retention.prune_avatar_history_batch(
                self.bot.db_pool
            )
            avatar_rows += deleted
            avatar_bytes += reclaimed
            if deleted < retention.AVATAR_PRUNE_BATCH_SIZE:
                break
            await asyncio.sleep(0)

        if scheduled_guilds or purged_guilds or avatar_rows:
            log.info(
                "Retention pass complete: scheduled_guilds=%s guilds=%s "
                "avatar_rows=%s avatar_bytes=%s",
                scheduled_guilds,
                purged_guilds,
                avatar_rows,
                avatar_bytes,
            )
        return {
            "scheduled_guilds": scheduled_guilds,
            "guilds": purged_guilds,
            "avatar_rows": avatar_rows,
            "avatar_bytes": avatar_bytes,
        }

    async def _check_backups(self):
        """Freshness + integrity guard for the pg_dump archives.

        FRESHNESS SEMANTICS (the deliberate choice). Dumps are taken only at
        startup and on demand via ?backup - never on a timer (house rule: no
        cron/services). A dump's age therefore simply tracks the bot's uptime:
        a process healthily up for three weeks legitimately has a three-week-old
        newest dump. An age threshold (e.g. "warn if > 26h") would thus fire on
        exactly the healthy long-uptime case it is meant to reassure us about -
        a guaranteed false positive on any well-behaved deployment. The only
        actionable freshness failure is the TOTAL ABSENCE of a dump: the startup
        dump silently failed, or backups/ was wiped. So absence is the sole
        freshness condition we warn on. It is a single listdir, so we do it on
        every tick.

        INTEGRITY. pg_restore --list reads the archive's table of contents
        without touching any database - cheap, but not free. The newest dump
        does not change unless a ?backup runs, so re-listing the same file every
        24h is wasted work. We verify ONCE PER BOOT (the first tick that finds a
        dump) instead; a corrupt newest dump surfaces as a grep-able error line.
        """
        report = backup.latest_backup_report(self._backups_dir)
        if report is None:
            log.warning(
                "BACKUP-FRESHNESS: no Postgres dump found in %s - the startup "
                "dump may have failed. Run ?backup to take one.",
                self._backups_dir,
            )
            return
        if self._backup_verified:
            return
        # First tick with a dump present: probe it once, then never again this
        # boot (set the flag before awaiting so a slow probe cannot double-fire).
        self._backup_verified = True
        result = await backup.verify_backup(report.path)
        if result.ok:
            log.info(
                "Backup integrity OK: %s (%s)",
                report.name,
                backup.human_size(report.size),
            )
        else:
            log.error(
                "BACKUP-CORRUPT: pg_restore --list failed for %s: %s",
                report.name,
                result.error,
            )

    @tasks.loop(hours=24)
    async def maintenance(self):
        if self._paused:
            log.info("Retention maintenance is paused; skipping tick")
            return
        if not self._is_healthy():
            return
        try:
            await self.run_once()
            await self._check_backups()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A global DB failure should not stop the loop permanently.
            log.exception("Data retention maintenance pass failed")
        else:
            # Record the healthy baseline only after a completed pass.
            self._last_healthy_guild_count = len(
                getattr(self.bot, "guilds", ())
            )

    @maintenance.before_loop
    async def before_maintenance(self):
        await self.bot.wait_until_ready()

    @commands.group(name="purges", hidden=True, invoke_without_command=True)
    async def purges(self, ctx):
        """Owner-only controls for the guild data-retention purge subsystem."""
        await self.purges_list(ctx)

    @purges.command(name="list")
    async def purges_list(self, ctx):
        """List scheduled guild purge jobs and the maintenance loop state."""
        jobs = await retention.list_guild_jobs(self.bot.db_pool)
        state = "PAUSED" if self._paused else "running"
        if not jobs:
            await ctx.send(
                f"Maintenance is {state}. No scheduled purge jobs."
            )
            return
        lines = [
            f"Maintenance is {state}. {len(jobs)} scheduled purge job(s):",
            "```",
        ]
        for job in jobs:
            guild_id = int(job["guild_id"])
            guild = self.bot.get_guild(guild_id)
            name = guild.name if guild is not None else "unknown"
            due = job["purge_after"]
            due_str = due.strftime("%Y-%m-%d %H:%M UTC") if due else "?"
            claimed = "claimed" if job["claimed_at"] is not None else "pending"
            lines.append(
                f"{guild_id}  {name[:24]:<24}  due {due_str}  "
                f"{claimed}  attempts={job['attempts']}"
            )
        lines.append("```")
        await ctx.send("\n".join(lines))

    @purges.command(name="cancel")
    async def purges_cancel(self, ctx, guild_id: int):
        """Cancel one guild's scheduled purge job by guild id."""
        cancelled = await retention.cancel_guild_purge(
            self.bot.db_pool, guild_id
        )
        if cancelled:
            await ctx.send(
                f"Cancelled the scheduled purge for guild {guild_id}."
            )
        else:
            await ctx.send(
                f"No scheduled purge found for guild {guild_id}."
            )

    @purges.command(name="pause")
    async def purges_pause(self, ctx):
        """Pause the maintenance loop (in-memory; resets on restart)."""
        self._paused = True
        await ctx.send(
            "Retention maintenance paused. Ticks will be skipped until resumed."
        )

    @purges.command(name="resume")
    async def purges_resume(self, ctx):
        """Resume the maintenance loop after a pause."""
        self._paused = False
        await ctx.send("Retention maintenance resumed.")


async def setup(bot):
    await bot.add_cog(DataRetention(bot))

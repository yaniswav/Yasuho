"""Bot-wide observability: gateway health counters and a periodic load line.

Two blind spots this cog closes:

* Gateway churn (a promo-time throttle, a mid-session reconnect) is only
  visible today buried inside discord.py's own INFO logging - there is no
  grep-able, bot-owned signal, and no running count across the process's
  lifetime. ``on_resumed`` / ``on_disconnect`` listeners here each log one
  line with a lifetime counter, so a spike is visible at a glance.
* A DB pool running low on idle connections is otherwise invisible until a
  command hangs waiting for a slot. asyncpg does not expose a per-acquire
  hook without subclassing Pool (out of scope here - YAGNI, and the task
  explicitly rules out a per-call timeout or circuit breaker), so the
  zero-idle check rides the same 60s tick as the load line below: a
  tick-cadence signal is exactly the "once-ish, not per-acquire spam"
  cadence asked for, at zero extra plumbing.

The periodic load line folds the ALREADY-EXISTING per-subsystem
instrumentation - :class:`tools.quotas.QuotaRegistry` (owned by the Music
cog), the webhook :class:`tools.rate_limit.FixedWindowRateLimiter` (owned by
Webstats), the interactive :class:`cogs.anilist.throttle.AniListThrottle`
(owned by the AniList cog), the DB pool, and the gateway counters above -
into one compact, grep-able INFO line every 60s. Every read is O(1)
(counters, dict sizes, or asyncpg's own
get_size()/get_idle_size()/get_max_size() getters), so the loop can never
itself become a load problem. Every other cog is read defensively
(``get_cog`` + ``getattr``) so this cog degrades to "n/a" for a subsystem
that failed to load, rather than crashing the loop.
"""

import asyncio
import logging

from discord.ext import commands, tasks

log = logging.getLogger(__name__)

# How often the load line ticks. Matches the other bot-internal tasks.loop
# heartbeats (music's idle/quota check, retention's daily pass) in spirit:
# cheap, bounded, no external scheduler.
LOAD_LOG_INTERVAL = 60  # seconds


def _format_kv(stats: dict) -> str:
    """Render a flat ``{name: count}`` dict as ``name=count name=count``."""
    return " ".join(f"{k}={v}" for k, v in stats.items())


def _format_nested(stats: dict) -> str:
    """Render a ``{member: {name: count}}`` dict as ``member(name=count ...)``.

    Same shape as ``cogs.music.effects.format_quota_stats``; kept as a small
    local formatter rather than importing across the music/system boundary
    for one function.
    """
    return " ".join(
        f"{member}({_format_kv(inner)})" for member, inner in stats.items()
    )


def format_load_line(
    *,
    pool_size: int,
    pool_idle: int,
    pool_max: int,
    quota_stats: dict | None,
    webhook_stats: dict | None,
    anilist_stats: dict | None,
    gw_resumes: int,
    gw_disconnects: int,
) -> str:
    """Fold every subsystem's counters into one compact, grep-able line.

    Pure and side-effect free so it is unit-testable without a bot, a pool,
    or a running loop. ``quota_stats``/``webhook_stats``/``anilist_stats`` are
    ``None`` when the owning cog has not (yet) loaded; that subsystem then
    renders as ``n/a`` instead of failing the whole line.
    """
    parts = [f"pool={pool_size}/{pool_max}", f"idle={pool_idle}"]
    parts.append(
        "quotas=" + (_format_nested(quota_stats) if quota_stats else "n/a")
    )
    parts.append(
        "webhook=" + (_format_kv(webhook_stats) if webhook_stats else "n/a")
    )
    parts.append(
        "anilist=" + (_format_kv(anilist_stats) if anilist_stats else "n/a")
    )
    parts.append(f"gw_resumes={gw_resumes}")
    parts.append(f"gw_disconnects={gw_disconnects}")
    return "LOAD " + " ".join(parts)


class Health(commands.Cog):
    """Gateway health counters plus a periodic bot-wide load line."""

    def __init__(self, bot):
        self.bot = bot
        # Lifetime (process-uptime) counters, deliberately in-memory only -
        # like the retention cog's pause flag, these reset on restart and
        # that is fine: they exist to make a live-process anomaly visible,
        # not to be a durable audit log.
        self.gw_resumes = 0
        self.gw_disconnects = 0
        self.load_line.start()

    def cog_unload(self):
        self.load_line.cancel()

    @commands.Cog.listener()
    async def on_resumed(self):
        # A resume is the healthy recovery path after a dropped gateway
        # connection, but a burst of them (promo-time throttling, a flaky
        # network) is exactly the signal this exists to surface - WARNING so
        # it is never mistaken for routine INFO noise.
        self.gw_resumes += 1
        log.warning("Gateway session resumed (count=%d)", self.gw_resumes)

    @commands.Cog.listener()
    async def on_disconnect(self):
        self.gw_disconnects += 1
        log.info("Gateway disconnected (count=%d)", self.gw_disconnects)

    def _quota_stats(self):
        """QuotaRegistry.stats() from the Music cog, or None if unavailable."""
        music = self.bot.get_cog("Music")
        quotas = getattr(music, "quotas", None)
        return quotas.stats() if quotas is not None else None

    def _webhook_stats(self):
        """The webhook rate limiter's stats(), or None if unavailable."""
        webstats = self.bot.get_cog("Webstats")
        limiter = getattr(webstats, "_limiter", None)
        return limiter.stats() if limiter is not None else None

    def _anilist_stats(self):
        """Interactive AniList throttle counters, or None if unavailable.

        Folds :attr:`~cogs.anilist.throttle.AniListThrottle.throttled_count`
        (lifetime interactive 429s) and the process-wide global window's
        ``stats()['global']`` (hits/rejections against ``GLOBAL_LIMIT``) so an
        operator watching the LOAD line can see "AniList is throttling us"
        without grepping a separate log line.
        """
        anilist = self.bot.get_cog("AniList")
        throttle = getattr(anilist, "_throttle", None)
        if throttle is None:
            return None
        global_stats = throttle.stats()["global"]
        return {
            "throttled_429": throttle.throttled_count,
            "global_hits": global_stats["hits"],
            "global_rejections": global_stats["rejections"],
        }

    @tasks.loop(seconds=LOAD_LOG_INTERVAL)
    async def load_line(self):
        try:
            pool = self.bot.db_pool
            pool_idle = pool.get_idle_size()
            line = format_load_line(
                pool_size=pool.get_size(),
                pool_idle=pool_idle,
                pool_max=pool.get_max_size(),
                quota_stats=self._quota_stats(),
                webhook_stats=self._webhook_stats(),
                anilist_stats=self._anilist_stats(),
                gw_resumes=self.gw_resumes,
                gw_disconnects=self.gw_disconnects,
            )
            if pool_idle == 0:
                # Zero idle connections: the next acquire has to wait for one
                # to free. Once per tick (never per-acquire) is the cheap,
                # non-spammy cadence asked for - see the module docstring.
                log.warning("DB pool has zero idle connections: %s", line)
            else:
                log.info(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A bad read here (e.g. a cog mid-teardown) must never take the
            # loop down permanently; the next tick simply tries again.
            log.exception("load-line tick failed")

    @load_line.before_loop
    async def _before_load_line(self):
        await self.bot.wait_until_ready()

    @load_line.error
    async def _load_line_error(self, error):
        log.exception("load-line loop crashed; restarting", exc_info=error)
        self.load_line.restart()


async def setup(bot):
    await bot.add_cog(Health(bot))

"""Real-time cache invalidation driven by the Remix dashboard.

The dashboard is a SEPARATE Node process that writes per-guild settings straight
into the SAME Postgres database, then emits::

    SELECT pg_notify('yasuho_dashboard', $1)

with a JSON payload ``{"kind": "...", "guildId": "..."}`` where ``kind`` is one of
``prefix | autorole | modlog | muterole | welcome | starboard | automod |
leveling | warn_escalation | verify_role | locale``. The bot mirrors those
settings in memory (``bot.prefixes`` / ``bot.autoroles`` / ``bot.muteroles``,
the ModLog cog's ``_channels`` cache, the ``tools.settings`` LRU for the
welcome + automod + modlog_events + warn_escalation + verify_role + locale
JSONB blobs, the Starboard cog's ``_config`` cache, the AutoMod cog's
``_settings`` cache for its boolean toggle table, and the Leveling cog's three
caches - ``_configs`` (level_config scalar knobs), ``_no_xp`` (level_no_xp
snapshot) and ``_multipliers`` (xp_multipliers + level_config event columns)),
so without this cog it would keep serving the stale in-memory value until the
next restart.

This cog LISTENs on the ``yasuho_dashboard`` channel over a DEDICATED asyncpg
connection (kept open for the cog's lifetime, separate from the shared pool) and,
per notification, RE-READS the authoritative value from Postgres and updates the
SAME in-memory structure the bot's own commands mutate - so a change made in the
dashboard takes effect on the very next event, no restart.

Design mirrors the existing house patterns:
* prefix/autorole/muterole updates mirror ``cogs/config/settings.py`` and
  ``cogs/moderation/moderation.py`` (``bot.prefixes[gid] = row`` / ``pop`` etc.);
* the modlog invalidation drops the ModLog cog's negative-cached ``_channels``
  entry, exactly as ``tools/retention.invalidate_guild_caches`` does, AND evicts
  the ``tools.settings`` blob (the dashboard also writes ``modlog_events`` there);
* the welcome and warn_escalation invalidations evict the guild's cached
  ``tools.settings`` blob (same helper retention uses) - both settings live in
  the same JSONB row, so evicting the blob is enough to pick up either key;
* the starboard invalidation re-reads the guild's ``(channel_id, threshold)`` row
  and writes it into the Starboard cog's ``_config`` cache exactly as that cog's
  own ``_apply_set`` / ``starboard_disable`` do (set the tuple, or ``None`` when
  the row is gone);
* the supervised background task started in ``__init__`` via
  ``bot.loop.create_task`` with a done-callback mirrors
  ``cogs/system/webstats.py``.

Everything is defensive: a malformed / unknown payload is a no-op, a missing cog
or dict is a no-op, and a dropped listen connection is re-established with backoff
without ever crashing the bot.
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg
from discord.ext import commands

from tools import settings
from tools.config_loader import config_loader

log = logging.getLogger(__name__)

# The Postgres NOTIFY channel the dashboard publishes on (see module docstring).
CHANNEL = "yasuho_dashboard"

# The settings the dashboard can change; anything else is ignored.
VALID_KINDS = frozenset(
    {
        "prefix",
        "autorole",
        "modlog",
        "muterole",
        "welcome",
        "starboard",
        "automod",
        "leveling",
        "warn_escalation",
        "verify_role",
        "locale",
    }
)

# Reconnect backoff bounds for the listen connection supervisor.
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0
# How often to actively probe the listen connection for liveness. A dropped TCP
# socket is not always reflected by ``is_closed()`` until a query is attempted,
# so a light ``SELECT 1`` on this cadence detects a dead connection promptly.
_KEEPALIVE_INTERVAL = 30.0


def _parse_payload(payload):
    """Parse a NOTIFY payload defensively into ``(kind, guild_id)`` or ``None``.

    Rejects anything that is not a JSON object carrying a known ``kind`` and a
    numeric ``guildId`` (accepted as int or numeric string, since JS serialises
    large ids as strings). Never raises.
    """
    if not isinstance(payload, (str, bytes, bytearray)):
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    if kind not in VALID_KINDS:
        return None
    try:
        guild_id = int(data.get("guildId"))
    except (TypeError, ValueError):
        return None
    return kind, guild_id


# ---------------------------------------------------------------------------
# Invalidators: RE-READ the authoritative value and update the SAME in-memory
# structure the bot's own commands mutate. Each guards a missing dict / cog so a
# stray notification can never crash the loop.
# ---------------------------------------------------------------------------


async def _invalidate_prefix(bot, gid):
    """Mirror ``cogs/config/settings.py``: set ``bot.prefixes[gid]`` or pop it."""
    cache = getattr(bot, "prefixes", None)
    if cache is None:
        return
    row = await bot.db_pool.fetchval(
        "SELECT prefix FROM prefixes WHERE guild_id = $1", gid
    )
    if row is not None:
        cache[gid] = row
    else:
        cache.pop(gid, None)


async def _invalidate_autorole(bot, gid):
    """Mirror ``settings.py`` autorole set/remove: ``bot.autoroles[gid]`` / pop."""
    cache = getattr(bot, "autoroles", None)
    if cache is None:
        return
    row = await bot.db_pool.fetchval(
        "SELECT role_id FROM autorole WHERE guild_id = $1", gid
    )
    if row is not None:
        cache[gid] = row
    else:
        cache.pop(gid, None)


async def _invalidate_muterole(bot, gid):
    """Mirror ``moderation.py`` mute-role handling: ``bot.muteroles[gid]`` / pop."""
    cache = getattr(bot, "muteroles", None)
    if cache is None:
        return
    row = await bot.db_pool.fetchval(
        "SELECT role_id FROM muterole WHERE guild_id = $1", gid
    )
    if row is not None:
        cache[gid] = row
    else:
        cache.pop(gid, None)


async def _invalidate_modlog(bot, gid):
    """Refresh BOTH stores a dashboard mod-log write can touch, for one guild.

    The mod-log CHANNEL lives in the ``modlog`` table and is cached in the
    ModLog cog's negative-cached ``_channels`` dict (``None`` means "looked up,
    not configured"); popping the guild's entry forces ``get_log_channel`` to
    re-query on the next event - the same eviction
    ``tools/retention.invalidate_guild_caches`` performs. The dashboard's
    "Moderation" section ALSO writes the guild_settings JSONB key
    ``modlog_events`` (which events get logged), served from the
    ``tools.settings`` LRU via ``settings.get_guild``/``_get_events``
    (``cogs/moderation/modlog.py``) - so this invalidator evicts that blob too,
    unconditionally, so an events-only change takes effect immediately even
    without touching the channel. No ModLog cog loaded => the ``_channels`` pop
    is skipped, but the settings eviction still runs.
    """
    settings.invalidate_guild(gid)
    cog = bot.get_cog("ModLog")
    if cog is None:
        return
    channels = getattr(cog, "_channels", None)
    if isinstance(channels, dict):
        channels.pop(gid, None)


async def _invalidate_welcome(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    Welcome state lives under the ``guild_settings`` JSONB key ``'welcome'`` and is
    served from the ``tools.settings`` LRU. ``invalidate_guild`` drops the guild's
    cached blob (the same helper retention uses), so the next
    ``settings.get_guild(..., 'welcome', ...)`` re-reads the authoritative row.
    """
    settings.invalidate_guild(gid)


async def _invalidate_warn_escalation(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    Warn escalation policy lives under the ``guild_settings`` JSONB key
    ``'warn_escalation'`` (``tools/warn_escalation.py`` ``SETTINGS_KEY``) and is
    read via ``tools.modactions.load_escalation_policy`` -> ``settings.get_guild``,
    served from the SAME ``tools.settings`` LRU as welcome/automod/modlog_events.
    ``invalidate_guild`` drops the guild's cached blob (the same helper retention
    uses), so the next read re-fetches the authoritative row instead of serving a
    stale policy until the next restart.
    """
    settings.invalidate_guild(gid)


async def _invalidate_verify_role(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    The verification role lives under the ``guild_settings`` JSONB key
    ``'verify_role'`` and is read via ``settings.get_guild(..., 'verify_role',
    None)`` in ``cogs/config/verification.py`` (both the ``/verify setup``
    command and the persistent ``verify_button`` view re-read it on every
    click), served from the SAME ``tools.settings`` LRU as
    welcome/automod/modlog_events/warn_escalation. ``invalidate_guild`` drops
    the guild's cached blob (the same helper retention uses), so the very next
    click re-reads the authoritative row instead of granting a stale role
    until the next restart.
    """
    settings.invalidate_guild(gid)


async def _invalidate_locale(bot, gid):
    """Evict the guild's cached settings blob so the next read re-fetches it.

    The server language lives under the ``guild_settings`` JSONB key
    ``'locale'`` and is read via ``settings.get_guild(..., 'locale', ...)``,
    served from the SAME ``tools.settings`` LRU as
    welcome/automod/modlog_events/warn_escalation/verify_role.
    ``invalidate_guild`` drops the guild's cached blob (the same helper
    retention uses), so the next read re-fetches the authoritative row instead
    of serving a stale language until the next restart.
    """
    settings.invalidate_guild(gid)


async def _invalidate_starboard(bot, gid):
    """Refresh the Starboard cog's ``_config`` entry from the authoritative row.

    The Starboard cog caches per-guild ``(channel_id, threshold)`` in ``_config``
    - a NEGATIVE cache where ``None`` means "looked up, not configured"
    (``cogs/config/starboard.py`` ``get_config``, l.152-163). It keeps that cache
    coherent on its own writes: ``_apply_set`` sets the tuple
    (``cogs/config/starboard.py:176``) and ``starboard_disable`` sets ``None``
    (``cogs/config/starboard.py:266``). Mirror that exactly here - re-read the row
    and store the tuple when configured, else ``None`` (a dashboard "disable"
    deletes the row). No cog loaded / no cache dict => safe no-op.
    """
    cog = bot.get_cog("Starboard")
    if cog is None:
        return
    cache = getattr(cog, "_config", None)
    if not isinstance(cache, dict):
        return
    row = await bot.db_pool.fetchrow(
        "SELECT channel_id, threshold FROM starboard WHERE guild_id = $1", gid
    )
    cache[gid] = (row["channel_id"], row["threshold"]) if row else None


async def _invalidate_automod(bot, gid):
    """Refresh BOTH AutoMod stores for a guild after a dashboard write.

    AutoMod config is split across two stores, so this invalidator touches both:

    * The ``guild_settings`` JSONB keys ``antiinvite`` / ``automod_action`` /
      ``automod_exempt_roles`` / ``automod_exempt_channels`` are served from the
      ``tools.settings`` LRU (``cogs/moderation/automod.py`` reads them via
      ``settings.get_guild`` - l.172-197 / l.285-297 / l.417). ``invalidate_guild``
      evicts the guild's cached blob (the SAME helper the welcome path uses), so
      the next ``settings.get_guild`` re-reads the authoritative row. This part is
      unconditional - the blob is cached whether or not the AutoMod cog object is
      currently loaded.
    * The ``automod`` TABLE booleans (``antilink`` / ``antispam``) ARE cached, in
      the AutoMod cog's ``_settings`` dict - a NEGATIVE cache holding the fetched
      Record, or ``None`` when the guild has no row
      (``cogs/moderation/automod.py`` ``get_settings``, l.136-143). Mirror that
      exactly: re-read the row and store it under ``gid`` (Record, or ``None``), so
      the next ``on_message`` sees the dashboard's new toggles. No cog loaded / no
      cache dict => safe no-op (the settings eviction above still ran).
    """
    settings.invalidate_guild(gid)
    cog = bot.get_cog("AutoMod")
    if cog is None:
        return
    cache = getattr(cog, "_settings", None)
    if not isinstance(cache, dict):
        return
    row = await bot.db_pool.fetchrow(
        "SELECT antilink, antispam FROM automod WHERE guild_id = $1", gid
    )
    cache[gid] = row


async def _invalidate_leveling(bot, gid):
    """Refresh EVERY leveling cache the Leveling cog keeps, for one guild.

    Leveling config is spread across THREE in-memory caches on the Leveling cog
    (``cogs/community/leveling.py``), so this invalidator refreshes each by
    calling the cog's OWN public refresh hook - the SAME method
    ``cogs/community/level_config_ui.py`` invokes after every leveling write, so a
    dashboard change takes effect on the very next message / voice sweep, no
    restart:

    * ``self._configs`` - a plain dict of ``tools.leveling.LevelConfig``, the
      on_message hot-path mirror of the level_config scalar knobs (enabled,
      cooldown, xp band, announce, voice_xp; leveling.py l.252) - is refreshed by
      ``refresh_guild_config`` (leveling.py l.347), which re-reads level_config
      and re-resolves the cached config (or drops the guild when disabled).
    * ``self._no_xp`` - a ``BoundedLRU`` of ``tools.leveling.NoXpSnapshot``
      (leveling.py l.268) - is refreshed by ``refresh_no_xp_snapshot``
      (leveling.py l.526), which re-reads the guild's ``level_no_xp`` rows.
    * ``self._multipliers`` - a ``BoundedLRU`` of
      ``tools.leveling.MultiplierSnapshot`` (leveling.py l.276) - is refreshed by
      ``refresh_multiplier_snapshot`` (leveling.py l.562), which re-reads the
      guild's ``xp_multipliers`` rows AND the ``level_config`` event columns.

    ``level_rewards`` rows and the ``level_config.rewards_mode`` column are NOT
    cached (``cogs/community/level_rewards.py`` reads both fresh on each level-up -
    l.19-22 / ``_fetch_mode``), so a dashboard rewards/mode change needs no
    invalidation here. Leveling config is served from these dict / LRU caches, NOT
    the ``tools.settings`` LRU (that LRU is only a legacy read-through fallback in
    ``refresh_guild_config``, which a level_config row - always written by the
    dashboard - makes moot), so no ``settings.invalidate_guild`` call is needed.
    No Leveling cog loaded, or a missing refresh method, is a safe no-op.
    """
    cog = bot.get_cog("Leveling")
    if cog is None:
        return
    for method_name in (
        "refresh_guild_config",
        "refresh_no_xp_snapshot",
        "refresh_multiplier_snapshot",
    ):
        refresh = getattr(cog, method_name, None)
        if callable(refresh):
            await refresh(gid)


_INVALIDATORS = {
    "prefix": _invalidate_prefix,
    "autorole": _invalidate_autorole,
    "muterole": _invalidate_muterole,
    "modlog": _invalidate_modlog,
    "welcome": _invalidate_welcome,
    "starboard": _invalidate_starboard,
    "automod": _invalidate_automod,
    "leveling": _invalidate_leveling,
    "warn_escalation": _invalidate_warn_escalation,
    "verify_role": _invalidate_verify_role,
    "locale": _invalidate_locale,
}


async def dispatch(bot, payload):
    """Parse ``payload`` and run the matching invalidator. Returns the handled
    ``kind`` on success, else ``None``.

    Pure and side-effect-scoped to the caches: malformed JSON, an unknown kind,
    or a bad guild id are ignored, and an invalidator that raises is logged and
    swallowed so a single bad notification can never take down the listener.
    """
    parsed = _parse_payload(payload)
    if parsed is None:
        return None
    kind, gid = parsed
    invalidator = _INVALIDATORS.get(kind)
    if invalidator is None:  # defensive: VALID_KINDS and _INVALIDATORS agree
        return None
    try:
        await invalidator(bot, gid)
    except Exception:
        log.exception("dashboard_sync: invalidation failed for kind=%s", kind)
        return None
    log.debug("dashboard_sync: invalidated kind=%s guild=%s", kind, gid)
    return kind


class DashboardSync(commands.Cog):
    """LISTENs on Postgres NOTIFY and invalidates the bot's in-memory caches."""

    def __init__(self, bot):
        self.bot = bot
        self._conn = None
        self._closing = False
        self._supervisor = None
        # Strong refs to per-notification handler tasks so the loop can't GC one
        # mid-run (the sponsorblock / core startup-backup pattern).
        self._handlers = set()

        # Resolve the DSN the same way core.py does. Missing config -> the cog
        # loads but stays idle (mirrors webstats' top.gg fallback guard).
        self._dsn = config_loader.get("Database", "PostgreSQL", fallback=None)
        if not self._dsn:
            log.info("dashboard_sync: no PostgreSQL DSN configured; sync disabled.")
            return

        self._supervisor = self.bot.loop.create_task(self._supervise())

        def _on_supervisor_done(task):
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                log.error("dashboard_sync: supervisor exited unexpectedly: %s", exc)

        self._supervisor.add_done_callback(_on_supervisor_done)

    # -- teardown -------------------------------------------------------
    async def cog_unload(self):
        self._closing = True
        if self._supervisor is not None:
            self._supervisor.cancel()
        for task in list(self._handlers):
            task.cancel()
        await self._teardown_connection()

    async def _teardown_connection(self):
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            await conn.remove_listener(CHANNEL, self._on_notify)
        except Exception:
            pass
        try:
            await conn.close()
        except Exception:
            pass

    # -- listener callback ---------------------------------------------
    def _on_notify(self, connection, pid, channel, payload):
        """asyncpg listener callback: runs in the loop, so it must NOT await.

        Any awaited DB re-read is handed off to a tracked task (per the task
        brief). Never raises: a failure here would otherwise surface inside
        asyncpg's dispatch.
        """
        try:
            task = self.bot.loop.create_task(self._handle(payload))
        except Exception:
            log.exception("dashboard_sync: failed to schedule handler")
            return
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle(self, payload):
        try:
            await dispatch(self.bot, payload)
        except Exception:
            log.exception("dashboard_sync: handler crashed")

    # -- supervised listen connection ----------------------------------
    async def _supervise(self):
        """Keep a dedicated listen connection alive, reconnecting with backoff.

        Gated on ``wait_until_ready`` so the pool and the other cogs exist before
        we start reacting to notifications. Every failure path is caught so the
        bot is never brought down by a DB blip; logs carry no secrets.
        """
        try:
            await self.bot.wait_until_ready()
        except Exception:
            pass

        backoff = _BACKOFF_START
        while not self._closing:
            try:
                await self._connect_and_listen()
                backoff = _BACKOFF_START  # healthy connect resets the backoff
                await self._watch_connection()
            except asyncio.CancelledError:
                break
            except Exception:
                # No secrets in the message (never log the DSN).
                log.warning(
                    "dashboard_sync: listen connection error; reconnecting in %.0fs",
                    backoff,
                )
            finally:
                await self._teardown_connection()

            if self._closing:
                break
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, _BACKOFF_MAX)

        log.info("dashboard_sync: listener supervisor stopped.")

    async def _connect_and_listen(self):
        """Open the dedicated connection and register the LISTEN callback."""
        conn = await asyncpg.connect(self._dsn)
        self._conn = conn
        await conn.add_listener(CHANNEL, self._on_notify)
        log.info("dashboard_sync: listening on Postgres channel '%s'.", CHANNEL)

    async def _watch_connection(self):
        """Block while the connection is healthy; return to trigger a reconnect.

        Actively probes with ``SELECT 1`` on a fixed cadence because a dropped
        socket is not always reflected by ``is_closed()`` until a query runs.
        """
        while not self._closing:
            conn = self._conn
            if conn is None or conn.is_closed():
                return
            try:
                await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("dashboard_sync: keepalive failed; reconnecting.")
                return
            await asyncio.sleep(_KEEPALIVE_INTERVAL)


async def setup(bot):
    await bot.add_cog(DashboardSync(bot))

"""Dashboard -> bot action queue: the bot side of an in-process work queue.

The Remix dashboard is a SEPARATE Node process with no Discord gateway
connection, so it cannot itself do things that require the live bot - e.g.
posting the persistent Verify button into a channel. Instead it enqueues the
request as a row in the ``dashboard_actions`` table (written under its
``requireManageGuild`` gate) and fires::

    SELECT pg_notify('yasuho_dashboard_action', '<id>')

on a channel DEDICATED to this queue (distinct from the ``yasuho_dashboard``
cache-invalidation channel that ``cogs/system/dashboard_sync.py`` owns). This
cog LISTENs on that channel over its OWN dedicated asyncpg connection (separate
from both the shared pool and the sync cog's listen connection) and, per
notification, drives the action to completion.

Design (mirrors the house patterns and the security brief):

* CLAIM-then-run, single-flight: ``_claim`` runs
  ``UPDATE dashboard_actions SET status='running' ... WHERE id=$1 AND
  status='pending' RETURNING guild_id, kind, payload``. Because the guard is
  ``status='pending'`` and the UPDATE is atomic, exactly ONE caller can claim a
  row; a duplicate notify (or a notify racing the boot reconciliation) finds no
  ``pending`` row and is a silent no-op. This is the idempotence backstop.
* The claimed ``guild_id`` is AUTHORITATIVE (the dashboard wrote it under its
  manage-guild check); the executor re-validates EVERYTHING else in the payload
  against the live gateway state (guild present, channel present + a text
  channel, bot may send) and NEVER trusts the payload. ``result`` never carries
  a secret or a stack trace - only short machine-readable error codes.
* Boot reconciliation (``reconcile``): a notify emitted while the bot was
  restarting is lost (LISTEN/NOTIFY does not buffer), so once at startup we
  expire actions too old to still be wanted, reset any ``running`` row orphaned
  by a previous process back to ``pending``, and re-drive every remaining
  ``pending`` row through the SAME claim path. Delivery is therefore
  at-least-once: an action interrupted after its side effect but before its
  status write can run twice (a duplicate Verify button, low harm) - the price
  of never silently dropping one.

Everything is defensive: a malformed payload, a missing guild/channel, a DB
blip or an executor exception is caught, logged without secrets, and recorded as
a ``failed`` result; a single bad action can never take down the listener, and a
dropped listen connection is re-established with backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg
import discord
from discord.ext import commands

from tools import i18n
from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)

# The Postgres NOTIFY channel the dashboard publishes action ids on. DEDICATED
# to this queue - deliberately NOT 'yasuho_dashboard' (the cache-sync channel).
CHANNEL = "yasuho_dashboard_action"

# Reconnect backoff bounds for the listen connection supervisor (match dashboard_sync).
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0
# Active liveness probe cadence: a dropped TCP socket is not always reflected by
# is_closed() until a query runs, so a light SELECT 1 detects a dead conn promptly.
_KEEPALIVE_INTERVAL = 30.0

# A pending/running action older than this at boot is considered stale and is
# marked failed rather than replayed - a request enqueued long before a restart
# is very likely no longer wanted. Generous enough to survive a slow restart.
_STALE_ACTION_MINUTES = 60

# Defensive cap on a custom embed message copied from the payload (Discord's
# embed description limit is 4096; the /verify setup path is bounded like this).
_MAX_MESSAGE_LEN = 2000


# ---------------------------------------------------------------------------
# Defensive payload / id parsing (never raises).
# ---------------------------------------------------------------------------


def _parse_action_id(payload):
    """Parse a NOTIFY payload (a bare decimal action id) into a positive int.

    The dashboard notifies with just ``String(id)``. Anything that is not a
    positive integer string is rejected (the row-level claim then never runs).
    """
    if not isinstance(payload, (str, bytes, bytearray)):
        return None
    try:
        value = int(payload)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _coerce_payload(raw):
    """Coerce a JSONB column value into a dict. Never raises.

    asyncpg returns a JSONB column as a ``str`` unless a codec is registered
    (this bot registers none - see ``tools.settings._load``, which handles both
    shapes), so accept a dict, a JSON string, or fall back to ``{}``.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# Executors: kind -> async handler(bot, guild_id, payload) -> result dict.
# Each RE-VALIDATES the payload against live state and returns a JSON-safe dict
# ``{"ok": bool, ...}``. A short ``error`` code on failure - never a secret.
# ---------------------------------------------------------------------------


def _verify_view_cls():
    """Return the persistent ``VerifyView`` class, imported lazily.

    ``cogs.config.verification`` builds ``discord.ui`` classes at import time
    (discord.py 2.x only), so importing it at module load would break this cog's
    import on the 3.7/discord.py-1.5 test box. Deferring the import keeps the
    module importable everywhere; the seam is also the monkeypatch point the
    executor tests use to avoid pulling in ``discord.ui`` at all.
    """
    from cogs.config.verification import VerifyView

    return VerifyView


async def _exec_verify_button_post(bot, guild_id, payload):
    """Post the persistent Verify button embed into a channel.

    Payload: ``{"channel_id": "<snowflake>", "message"?: "<custom text>"}``.
    ``guild_id`` is authoritative (from the claimed row); EVERYTHING else is
    re-validated here against the live gateway - the payload is never trusted:
    the guild must be present, the channel must exist, be a text channel, and
    the bot must be allowed to send there. The Verify ROLE is intentionally NOT
    required to be configured: the button reads the role at click time and
    reports if it is unset, so posting the button first (then setting the role)
    is a valid order.
    """
    try:
        channel_id = int(payload.get("channel_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_channel_id"}

    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}

    channel = guild.get_channel(channel_id)
    if channel is None:
        return {"ok": False, "error": "channel_not_found"}
    if not isinstance(channel, discord.TextChannel):
        return {"ok": False, "error": "not_text_channel"}

    me = guild.me
    if me is None:
        return {"ok": False, "error": "guild_unavailable"}
    if not channel.permissions_for(me).send_messages:
        return {"ok": False, "error": "missing_send_permission"}

    # Custom message is optional free text; bound it and never translate it. Only
    # the default copy is localised, to the guild's configured language.
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        message = message[:_MAX_MESSAGE_LEN]
    else:
        message = None

    loc = await i18n.resolve_guild_locale(bot, guild)
    with i18n.locale(loc):
        embed = discord.Embed(
            title=_("Verification"),
            description=(
                message
                or _("Click the button below to verify and unlock the server.")
            ),
            colour=random_colour(),
        )

    sent = await channel.send(embed=embed, view=_verify_view_cls()())
    return {
        "ok": True,
        "channel_id": str(channel.id),
        "message_id": str(getattr(sent, "id", "")),
    }


_EXECUTORS = {
    "verify_button_post": _exec_verify_button_post,
}


# ---------------------------------------------------------------------------
# Claim / finish / dispatch (pure-ish, testable without the listen connection).
# All queries ride the SHARED pool (bot.db_pool); the dedicated connection below
# is ONLY for LISTEN.
# ---------------------------------------------------------------------------


async def _claim(pool, action_id):
    """Atomically claim a pending action. Returns the claimed row or ``None``.

    The ``status='pending'`` guard makes this single-flight: a duplicate notify
    (or a notify racing the boot reconciliation) finds no pending row and gets
    ``None`` back - the idempotence backstop.
    """
    return await pool.fetchrow(
        "UPDATE dashboard_actions "
        "SET status = 'running', updated_at = now() "
        "WHERE id = $1 AND status = 'pending' "
        "RETURNING guild_id, kind, payload",
        action_id,
    )


async def _finish(pool, action_id, status, result):
    """Write the terminal ``status`` + ``result`` JSON back for an action."""
    await pool.execute(
        "UPDATE dashboard_actions "
        "SET status = $1, result = $2::jsonb, updated_at = now() "
        "WHERE id = $3",
        status,
        json.dumps(result),
        action_id,
    )


async def handle_action(bot, action_id):
    """Claim, dispatch and finalise one action. Never raises.

    Returns the terminal status (``'done'`` / ``'failed'``) it wrote, or
    ``None`` when there was nothing to do (already claimed/processed, or the
    claim itself errored). Shared by both the notify path and reconciliation.
    """
    pool = bot.db_pool
    try:
        claimed = await _claim(pool, action_id)
    except Exception:
        # A claim failure (DB blip) must not crash the listener; the boot
        # reconciliation is the backstop that re-drives a still-pending row.
        log.exception("dashboard_actions: claim failed for id=%s", action_id)
        return None
    if claimed is None:
        return None  # already claimed elsewhere / not pending: silent no-op

    guild_id = claimed["guild_id"]
    kind = claimed["kind"]
    payload = _coerce_payload(claimed["payload"])

    executor = _EXECUTORS.get(kind)
    if executor is None:
        await _finalise(pool, action_id, {"ok": False, "error": "unknown_kind"})
        return "failed"

    try:
        result = await executor(bot, guild_id, payload)
    except Exception:
        # Never surface the exception text/stack to the dashboard - only a fixed
        # code. The full traceback is logged server-side.
        log.exception(
            "dashboard_actions: executor %r failed for id=%s", kind, action_id
        )
        await _finalise(pool, action_id, {"ok": False, "error": "internal_error"})
        return "failed"

    if not isinstance(result, dict):
        result = {"ok": False, "error": "internal_error"}
    return await _finalise(pool, action_id, result)


async def _finalise(pool, action_id, result):
    """Persist ``result`` with the derived status; returns that status.

    An ``ok`` result is ``done``; a well-formed failure (validation, unknown
    kind, ...) is ``failed`` so the dashboard can surface ``result.error``. The
    write itself is guarded so a persistence blip cannot crash the loop.
    """
    status = "done" if result.get("ok") else "failed"
    try:
        await _finish(pool, action_id, status, result)
    except Exception:
        log.exception("dashboard_actions: failed to persist result for id=%s", action_id)
    return status


async def reconcile(bot):
    """Boot backstop: recover actions a missed notify would otherwise strand.

    LISTEN/NOTIFY does not buffer, so a notify fired while the bot was down is
    gone. Once at startup we (1) fail actions too old to still be wanted, (2)
    reset any ``running`` row orphaned by a dead previous process back to
    ``pending`` (this fresh process holds no in-flight handler yet, so every
    ``running`` row is orphaned), and (3) re-drive every remaining ``pending``
    row through the normal atomic claim - so a concurrent live notify for the
    same row still can't double-run it. Never raises out of a per-row failure.
    """
    pool = bot.db_pool

    # (1) Expire the too-old. Bound age is a fixed constant, not user input.
    await pool.execute(
        "UPDATE dashboard_actions "
        "SET status = 'failed', result = $1::jsonb, updated_at = now() "
        "WHERE status IN ('pending', 'running') "
        "AND created_at < now() - INTERVAL '%d minutes'" % _STALE_ACTION_MINUTES,
        json.dumps({"ok": False, "error": "expired"}),
    )

    # (2) Reset orphaned 'running' rows (only recent ones remain after step 1).
    await pool.execute(
        "UPDATE dashboard_actions "
        "SET status = 'pending', updated_at = now() "
        "WHERE status = 'running'"
    )

    # (3) Re-drive everything still pending, oldest first, one at a time.
    rows = await pool.fetch(
        "SELECT id FROM dashboard_actions WHERE status = 'pending' ORDER BY id"
    )
    for row in rows:
        try:
            await handle_action(bot, row["id"])
        except Exception:
            # handle_action already swallows its own errors; this is belt-and-
            # suspenders so one bad row never aborts the rest of the sweep.
            log.exception(
                "dashboard_actions: reconcile failed for id=%s", row["id"]
            )


# ---------------------------------------------------------------------------
# Cog: supervised dedicated LISTEN connection (mirrors DashboardSync).
# ---------------------------------------------------------------------------


class DashboardActions(commands.Cog):
    """LISTENs for dashboard action ids and drives each to completion."""

    def __init__(self, bot):
        self.bot = bot
        self._conn = None
        self._closing = False
        self._supervisor = None
        self._reconciled = False
        # Strong refs to per-notification / reconcile tasks so the loop can't GC
        # one mid-run (the dashboard_sync / sponsorblock pattern).
        self._handlers = set()

        self._dsn = config_loader.get("Database", "PostgreSQL", fallback=None)
        if not self._dsn:
            log.info(
                "dashboard_actions: no PostgreSQL DSN configured; queue disabled."
            )
            return

        self._supervisor = self.bot.loop.create_task(self._supervise())

        def _on_supervisor_done(task):
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                log.error(
                    "dashboard_actions: supervisor exited unexpectedly: %s", exc
                )

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

    def _track(self, task):
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    # -- listener callback ---------------------------------------------
    def _on_notify(self, connection, pid, channel, payload):
        """asyncpg listener callback: runs in the loop, so it must NOT await.

        Hands the (awaiting) work off to a tracked task. Never raises: a failure
        here would otherwise surface inside asyncpg's dispatch.
        """
        try:
            task = self.bot.loop.create_task(self._handle(payload))
        except Exception:
            log.exception("dashboard_actions: failed to schedule handler")
            return
        self._track(task)

    async def _handle(self, payload):
        action_id = _parse_action_id(payload)
        if action_id is None:
            return
        try:
            await handle_action(self.bot, action_id)
        except Exception:
            log.exception("dashboard_actions: handler crashed")

    # -- supervised listen connection ----------------------------------
    async def _supervise(self):
        """Keep the dedicated listen connection alive, reconnecting with backoff.

        Gated on ``wait_until_ready`` so the pool and the guilds exist before we
        react. Every failure path is caught; logs never carry the DSN.
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
                self._maybe_reconcile()
                await self._watch_connection()
            except asyncio.CancelledError:
                break
            except Exception:
                log.warning(
                    "dashboard_actions: listen connection error; reconnecting in %.0fs",
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

        log.info("dashboard_actions: listener supervisor stopped.")

    def _maybe_reconcile(self):
        """Schedule the one-shot boot reconciliation as a tracked task.

        Runs AFTER the listener is attached (so no live notify is lost while it
        works) and only once per process. Decoupled from the watch loop so a
        large backlog can't delay keepalive.
        """
        if self._reconciled:
            return
        self._reconciled = True

        async def _run():
            try:
                await reconcile(self.bot)
            except Exception:
                log.exception("dashboard_actions: boot reconciliation failed")

        self._track(self.bot.loop.create_task(_run()))

    async def _connect_and_listen(self):
        conn = await asyncpg.connect(self._dsn)
        self._conn = conn
        await conn.add_listener(CHANNEL, self._on_notify)
        log.info("dashboard_actions: listening on Postgres channel '%s'.", CHANNEL)

    async def _watch_connection(self):
        """Block while the connection is healthy; return to trigger a reconnect."""
        while not self._closing:
            conn = self._conn
            if conn is None or conn.is_closed():
                return
            try:
                await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("dashboard_actions: keepalive failed; reconnecting.")
                return
            await asyncio.sleep(_KEEPALIVE_INTERVAL)


async def setup(bot):
    await bot.add_cog(DashboardActions(bot))

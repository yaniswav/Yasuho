import asyncio
import logging
import logging.handlers
import os
import sys

import aiohttp
import asyncpg
import discord
import sonolink
from discord.ext import commands

from tools import backup, fixups, i18n, music_state, tree_sync
from tools.config_loader import config_loader
from tools.http import TIMEOUT
from tools.mobile_status import enable_mobile_status
from tools.translator import YasuhoTranslator

log = logging.getLogger(__name__)

DEFAULT_PREFIX = config_loader.get("BotInfo", "DefaultPrefix")
TOKEN = config_loader.get("Bot_Token", "Token")
POSTGRESQL_URI = config_loader.get("Database", "PostgreSQL")
BACKUPS_DIR = os.path.join(os.path.dirname(__file__), "backups")
PROJECT_ROOT = os.path.dirname(__file__)

# Strong references to fire-and-forget background tasks (the startup backup),
# so the loop does not garbage-collect a task that is still running. Mirrors the
# sponsorblock._pending pattern.
_background_tasks: set[asyncio.Task] = set()

# How often _supervise_lavalink checks a healthy (connected) node - one cheap
# attribute read bot-wide, not per guild.
LAVALINK_CHECK_INTERVAL = 30.0
# Backoff between retry attempts while the node is genuinely down: starts low
# so a Lavalink restart that finishes in a few seconds is picked up quickly,
# doubles so a longer outage does not spam connection attempts, capped well
# under LAVALINK_CHECK_INTERVAL's neighbourhood so it still reacts promptly.
# Also the poll interval while CONNECTING but not yet stuck (see
# LAVALINK_STUCK_AFTER) - a CONNECTING node deserves a closer look than a
# settled CONNECTED one.
LAVALINK_BACKOFF_START = 5.0
LAVALINK_BACKOFF_CAP = 60.0

# sonolink's own retry count for ONE connect burst (the initial connect, or one
# runtime reconnect - gateway/node/_connection.py attempt_connect/reconnect
# share this same counter). Finite on purpose: sonolink does SHORT bursts,
# _supervise_lavalink owns LONG-TERM retrying. With this set, an exhausted
# burst (handshake failures it actually catches, e.g. a 503 while Lavalink
# loads plugins) ends in attempt_connect's exhausted branch, which sets
# NodeStatus.DISCONNECTED and - for a runtime reconnect - dispatches
# "node_close" (_connection.py attempt_connect, ~138-152); _supervise_lavalink
# then re-kicks it with its own backoff. So a routine Lavalink restart stays
# unbounded IN TIME (the supervisor never stops trying), but every state is
# now observable from here - unlike retries=None (the previous value), which
# makes attempt_connect's retry counter itertools.count() (infinite): the
# whole burst then runs as one single `await` with no way for this supervisor
# to see it, and - the actual hole that prompted this change - it does not
# even cover the case below: a REFUSED connection (Lavalink down, or a restart
# that briefly closes the port) raises aiohttp.ClientConnectorError, which is
# not a WebSocketError, so it escapes attempt_connect's `except WebSocketError`
# on attempt 1 regardless of how many retries are configured, leaving the node
# stuck CONNECTING (see LAVALINK_STUCK_AFTER) with no loop running at all - a
# bigger retries value would not have helped.
LAVALINK_NODE_RETRIES = 3

# Bounds ONE call to sl_client.start(). sonolink's websocket connect
# (network/_aiohttp.py AioWebsocketManager.connect -> session.ws_connect) sets
# no per-call timeout, so it inherits aiohttp's session default,
# ClientTimeout(total=300) (aiohttp.client.DEFAULT_TIMEOUT) - a genuinely
# hanging TCP peer could in theory hold one attempt that long. The realistic
# worst case for a LEGIT burst (LAVALINK_NODE_RETRIES handshake failures
# sonolink itself retries) is far smaller: 3 attempts, each a fast
# connect-and-reject, plus the 0.5s/1.0s/2.0s sleeps between them
# (attempt_connect's own backoff, base_delay=0.5/attempt - ~3.5s total) - so
# 60s leaves wide headroom above that without waiting anywhere near the 300s
# aiohttp ceiling for the escape case this exists to bound. A node still stuck
# CONNECTING when this fires (the escape case, cancelled mid-flight) is left
# for LAVALINK_STUCK_AFTER to clean up next.
LAVALINK_START_TIMEOUT = 60.0

# How long a node may sit CONNECTING, WITHOUT a live watcher, before
# _supervise_lavalink treats it as stuck (the escape case above:
# connect()/reconnect() set NodeStatus.CONNECTING before attempt_connect runs,
# and nothing resets it when attempt_connect's exception escapes uncaught) and
# force-closes it so the next start() can begin fresh. "Without a live
# watcher" is the point: a CONNECTING node WITH one (sonolink genuinely
# retrying - see the live-watcher check in _supervise_lavalink) is left alone
# past this and bounded by LAVALINK_WEDGED_AFTER instead. The supervisor never
# checks while it is itself awaiting start(), so a dead-watcher CONNECTING
# this catches is always the escape case, never a legit burst. It also sets
# the retry cadence while Lavalink refuses connections: about this value plus
# LAVALINK_BACKOFF_START.
LAVALINK_STUCK_AFTER = 30.0

# Hard ceiling for a CONNECTING node that DOES have a live watcher (a runtime
# reconnect burst actually running inside sonolink's keep-alive task, or a
# websocket that opened and is waiting on Lavalink's "ready" op - see the
# live-watcher check in _supervise_lavalink). Such a node is making progress
# sonolink itself is responsible for, so LAVALINK_STUCK_AFTER does not apply;
# but it cannot run forever either, because ONE sonolink websocket attempt
# (AioWebsocketManager.connect -> session.ws_connect, network/_aiohttp.py) has
# no per-call timeout of its own, so it inherits aiohttp's ClientSession
# default, aiohttp.client.DEFAULT_TIMEOUT = ClientTimeout(total=300) (the
# session is created with no explicit timeout in AioHTTPManager.setup, same
# file) - a genuinely hanging TCP peer could in theory hold one attempt that
# long. This mirrors that ceiling so a wedged live watcher is still force-
# closed and retried, just far later than the dead-watcher case above.
LAVALINK_WEDGED_AFTER = 300.0


def _module_has_setup(path):
    """Cheap text check for a `setup` entry point, without importing the module.

    An UNREADABLE file answers True, not False. The check is only a cheap filter
    on what to hand to `load_extension`; a file we cannot read is one we cannot
    clear, and the two ways of being wrong are not symmetrical:

    * answering False hides the module completely - it never reaches the load
      loop, so `failed_extensions` stays empty, so `tools.tree_sync` sees a tree
      it believes is COMPLETE and bulk-overwrites Discord with it, DELETING every
      command that cog owns. Silent, in production.
    * answering True at worst hands `load_extension` something with no `setup`.
      That raises, `setup_hook` logs it and records the name, and the sync is
      refused. Loud, and the previous registration stays live.

    So an IO fault degrades towards the refusal. A syntax error needs none of
    this: the check is textual, so a broken module still reads as an extension
    and still fails at load time.
    """
    try:
        with open(path, encoding="utf-8") as fp:
            return "def setup(" in fp.read()
    except OSError:
        log.warning(
            "Could not read %s while discovering cogs; treating it as an "
            "extension so the load failure is loud and the slash tree "
            "auto-sync refuses a partial tree.",
            path,
            exc_info=True,
        )
        return True


def discover_extensions():
    """Find every cog under cogs/: any module or package exposing `setup`.

    A package whose __init__ defines `setup` (e.g. cogs.anilist) is loaded whole
    and not descended into; a category folder (with an empty __init__) is
    descended so its cog modules load as cogs.<category>.<name>. This lets cogs be
    organised into folders freely, with no extension list to maintain.
    """
    base_dir = os.path.dirname(__file__)
    cogs_dir = os.path.join(base_dir, "cogs")
    found = []
    for root, dirs, files in os.walk(cogs_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, base_dir).replace(os.sep, ".")
        init_path = os.path.join(root, "__init__.py")
        # lexists, not isfile: a dangling symlink (or anything else we cannot
        # stat through) named __init__.py still MEANS "this folder is a
        # package". isfile answers False there, which would silently downgrade
        # the folder to a category and descend - and a package whose commands
        # all come from its __init__ would then contribute nothing, with no
        # load failure to show for it. _module_has_setup turns the unreadable
        # case into a claim, which fails loudly at load time.
        if root != cogs_dir and os.path.lexists(init_path) and _module_has_setup(init_path):
            found.append(rel)
            dirs[:] = []
            continue
        for fname in files:
            if (
                fname.endswith(".py")
                and fname != "__init__.py"
                and _module_has_setup(os.path.join(root, fname))
            ):
                found.append(f"{rel}.{fname[:-3]}")
    return sorted(found)


class Yasuho(commands.Bot):
    """Main bot subclass wiring up intents, prefixes, and extensions."""

    def __init__(self, db_pool: asyncpg.Pool):
        allowed_mentions = discord.AllowedMentions(
            roles=False, everyone=False, users=True
        )
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.moderation = True
        intents.emojis_and_stickers = True
        intents.voice_states = True
        intents.presences = True
        intents.messages = True
        intents.reactions = True
        intents.message_content = True

        super().__init__(
            command_prefix=get_prefix,
            chunk_guilds_at_startup=False,
            heartbeat_timeout=150.0,
            allowed_mentions=allowed_mentions,
            intents=intents,
            # enable_debug_events is deliberately NOT set. It makes discord.py
            # dispatch on_socket_raw_receive and on_socket_event_type for EVERY
            # inbound gateway packet - the raw payload string included - and
            # nothing in this bot listens for either event (no cog, no tool, no
            # test). That was a per-packet dispatch, on the busiest path there
            # is, whose every listener list was empty.
            help_command=None,
        )

        self.db_pool = db_pool
        self.http_session = None
        self.image_render_semaphore = asyncio.Semaphore(2)
        self.default_prefix = DEFAULT_PREFIX
        # sonolink client for music (Lavalink v4). Created here but only started
        # in setup_hook, and only when [Lavalink] is configured.
        self.sl_client = sonolink.Client(self)
        # The bot-wide Lavalink reconnect supervisor task (see _start_lavalink),
        # or None when Lavalink is not configured / not started yet.
        self._lavalink_task: asyncio.Task | None = None
        # In-memory caches for hot / rarely-changing data, loaded in setup_hook
        # and invalidated by the owning cogs (mirrors the prefixes cache).
        self.prefixes = {}
        self.blacklist = set()
        self.autoroles = {}
        self.muteroles = {}
        # Serialises the two ways those four maps change: the whole-map reload
        # below and the per-id refreshes cogs/system/dashboard_sync.py performs
        # on a NOTIFY. Without it a notify that lands mid-reload writes into the
        # dict the reload is about to REPLACE, and the write is silently lost.
        self.eager_cache_lock = asyncio.Lock()
        # Set by the Reminder cog on load; defaulted here so the tools.time
        # converters can read bot.reminder even if that cog fails to load.
        self.reminder = None

    async def get_context(self, *args, **kwargs):
        """Set the per-invocation i18n locale before a command runs.

        This runs for every message (via process_commands) and every hybrid
        slash invocation, but the locale is resolved only for real commands. It
        runs in the same task that then executes the command body, so the
        ContextVar that _() reads is correct for the whole invocation.
        """
        ctx = await super().get_context(*args, **kwargs)
        if ctx.command is not None:
            try:
                i18n.current_locale.set(
                    await i18n.resolve_locale(
                        self,
                        user_id=ctx.author.id,
                        guild_id=ctx.guild.id if ctx.guild else None,
                        interaction=ctx.interaction,
                    )
                )
            except Exception:
                i18n.current_locale.set(i18n.DEFAULT_LOCALE)
        return ctx

    def _schedule_startup_backup(self) -> None:
        """Kick off a pg_dump in the background; never blocks or fails startup.

        run_backup never raises, so the wrapper only translates its result into
        one INFO line on success (path + human size) or one WARNING on failure.
        The task is held in _background_tasks so it is not garbage-collected
        while running (the sponsorblock strong-ref pattern).
        """

        async def _run():
            result = await backup.run_backup(POSTGRESQL_URI, BACKUPS_DIR)
            if result.ok:
                log.info(
                    "Startup backup written: %s (%d bytes, %d rotated)",
                    result.path,
                    result.size or 0,
                    result.deleted,
                )
            else:
                log.warning("Startup backup failed: %s", result.error)

        task = asyncio.ensure_future(_run())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def load_eager_caches(self) -> None:
        """(Re)load the EAGERLY-primed hot-path caches from the database.

        These four maps are the ones primed once in setup_hook and then read
        SYNCHRONOUSLY, with no read-through: get_prefix reads self.prefixes on
        every message, events.py reads self.autoroles/self.muteroles on join and
        self.blacklist to refuse a blacklisted member. Absence is therefore a
        MEANING here ("no custom prefix", "not blacklisted"), not a cache miss -
        which is why they can only ever be RELOADED, never cleared: an emptied
        self.prefixes would silently reset every custom-prefix guild to the
        default with nothing left to notice it.

        Factored out of setup_hook so the dashboard_sync cog can re-run exactly
        the startup queries after its LISTEN connection was down (Postgres drops
        NOTIFY for an absent listener, so a dashboard write during the gap is
        invisible to the invalidators - see cogs/system/dashboard_sync.py).

        Every row is fetched BEFORE any attribute is rebound, and each map is
        replaced wholesale rather than mutated in place, so a concurrent reader
        always sees a complete map - the previous one or the new one, never a
        half-filled one. Nothing aliases these dicts (every reader goes through
        bot.<attr> on each access), so rebinding is safe.

        WRITERS are a different matter, hence eager_cache_lock: the dashboard's
        per-id invalidators mutate the map OBJECT in place (bot.prefixes[gid] =
        ...), so one that fetched its row while this method was between its own
        fetch and its rebind would write into the dict this rebind discards, and
        the dashboard's change would vanish until the next write. Holding the
        lock across fetch AND rebind makes the two orderings the only possible
        ones: the invalidator's write is either already visible to the fetch
        below, or lands on the new map afterwards.
        """
        async with self.eager_cache_lock:
            prefixes = await self.db_pool.fetch(
                "SELECT guild_id, prefix FROM prefixes;"
            )
            blacklist = await self.db_pool.fetch("SELECT member_id FROM blbot;")
            autoroles = await self.db_pool.fetch(
                "SELECT guild_id, role_id FROM autorole;"
            )
            muteroles = await self.db_pool.fetch(
                "SELECT guild_id, role_id FROM muterole;"
            )

            self.prefixes = dict(prefixes)
            self.blacklist = {r["member_id"] for r in blacklist}
            self.autoroles = dict(autoroles)
            self.muteroles = dict(muteroles)

    async def setup_hook(self) -> None:
        # schema.sql is THE schema source of truth and is applied on every boot.
        # It is idempotent (CREATE ... IF NOT EXISTS, additive ALTER ... IF NOT
        # EXISTS, and guarded NOT VALID constraints) and carries no params, so
        # asyncpg runs it via the simple query protocol where the multi-statement
        # script executes as one implicit transaction.
        schema_path = os.path.join(PROJECT_ROOT, "schema.sql")
        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as fp:
                await self.db_pool.execute(fp.read())

        # One-shot, idempotent DATA repairs that DDL cannot express. This NEVER
        # blocks startup: run_fixups swallows per-fixup errors, and the outer
        # guard covers an unexpected failure of the runner itself.
        try:
            applied_fixups = await fixups.run_fixups(self.db_pool)
            if applied_fixups:
                log.info("Applied data fixups: %s", ", ".join(applied_fixups))
        except Exception:
            log.exception("Data fixups runner failed; continuing startup")

        # The DB is confirmed up (schema applied). Take a backup in the
        # background: fire-and-forget so it never delays readiness, with a strong
        # ref + done-callback so the loop cannot drop the task mid-run and so a
        # failure is logged rather than swallowed silently.
        self._schedule_startup_backup()

        self.http_session = aiohttp.ClientSession(timeout=TIMEOUT)

        await self.load_eager_caches()

        # A cog that cannot attach is logged and skipped, never raised: one
        # broken extension must not take the whole bot down. The names are kept
        # because that tolerance has a consequence downstream - the command tree
        # is then INCOMPLETE, and a global sync is a bulk OVERWRITE that would
        # delete every command the missing cog owns. tree_sync refuses on a
        # non-empty list; see tools/tree_sync.decide.
        failed_extensions = []
        for extension in discover_extensions():
            try:
                await self.load_extension(extension)
                log.info("Loaded %s", extension)
            except Exception:
                log.exception("Error while trying to load %s", extension)
                failed_extensions.append(extension)

        log.info("Prefix count: %d", len(self.prefixes))
        log.info("i18n locales: %s", ", ".join(sorted(i18n.LOCALES)))

        # Localize slash command descriptions/choices in the Discord command
        # picker (the response text is handled separately by tools/i18n.py).
        await self.tree.set_translator(YasuhoTranslator())

        # Register the slash tree with Discord if - and only if - its payload
        # changed since the last successful sync.
        #
        # WHY HERE, AND NOWHERE EARLIER. Three things must already be true when
        # the payload is computed, and this is the first point at which all
        # three are:
        #   1. every extension has been through the loop above, so the tree is
        #      whole (or we know exactly which cogs are missing from it);
        #   2. the translator is installed, and it is PART OF THE PAYLOAD -
        #      CommandTree.sync runs it to build description_localizations, so a
        #      hash taken before this line would be blind to a translation-only
        #      change, which is precisely what broke /config on 2026-09-13;
        #   3. application_id is set - discord.py fills it in Client.login
        #      (client.py:682) before it awaits setup_hook (client.py:693).
        #
        # It never raises and never blocks past its own timeout; the worst case
        # is a WARNING and an unchanged stored hash, which the next boot retries.
        await tree_sync.sync_if_changed(self, failed_extensions=failed_extensions)

        # Connect to Lavalink for music ONLY if it is configured. Skipping the
        # attempt avoids the startup delay and reconnect spam when there is no
        # Lavalink server (music is deferred). Set [Lavalink] uri (and password)
        # in config to enable it.
        await self._start_lavalink()

    async def _start_lavalink(self) -> None:
        """Register the Lavalink node and launch its reconnect supervisor.

        NOT AN OBSERVED INCIDENT - found by the 2026-09-30 code review reading
        sonolink's installed source, not from production logs. This used to be
        ``create_node(...)`` followed by a plain ``await self.sl_client.start()``
        right here, inline, with ``retries`` left at sonolink's default of
        ``None`` (sonolink/gateway/node/_connection.py attempt_connect:
        ``retries=None`` -> ``itertools.count()``, an INFINITE counter). A 503
        while Lavalink is still loading plugins is one of the statuses that
        loop retries by itself, sleeping up to 10s between attempts, so an
        inline ``await`` there could have left ``setup_hook`` never returning,
        and discord.py never finishing login.

        So no Lavalink I/O happens on this coroutine's call stack at all: this
        method only registers the node (synchronous) and hands the actual
        connect attempts to a background task, _supervise_lavalink, which
        setup_hook never awaits. See LAVALINK_NODE_RETRIES, LAVALINK_START_TIMEOUT
        and LAVALINK_STUCK_AFTER above for why a finite retry count plus a
        supervisor-side timeout and stuck-state reset are both still needed on
        top of that split - a finite retries value alone does not cover every
        way a connect attempt can go wrong (see _supervise_lavalink).
        """
        try:
            lavalink_uri = config_loader.get("Lavalink", "uri")
        except Exception:
            lavalink_uri = None
        if not lavalink_uri:
            log.info("Lavalink not configured; music disabled.")
            return

        try:
            lavalink_pw = config_loader.get("Lavalink", "password")
        except Exception:
            lavalink_pw = "youshallnotpass"

        try:
            # NOTE: sonolink's create_node(session=...) takes an HTTP client
            # session (aiohttp/curl_cffi) to reuse, NOT a Lavalink resume
            # session id - there is no public way to seed a previous Lavalink
            # session across a process restart (Node always starts with no
            # resume session; it is only set from a live "ready" event). A
            # previous attempt to pass our saved session id there broke the
            # websocket connection outright. Cross-restart gap-free resume is
            # therefore not attempted here; music/music.py's cold-restore
            # path (music_state table) is what survives a restart.
            # resume_timeout=0: since resume is never used (see NOTE above),
            # a positive timeout only keeps the DEAD process's session and
            # its zombie players alive server-side after a restart - they
            # hold the guild's stale voice session and race the restored
            # player, showing up as "voice WS closed, 4006 Session is no
            # longer valid" churn right after every restore.
            self.sl_client.create_node(
                uri=lavalink_uri,
                password=lavalink_pw,
                id=music_state.MUSIC_NODE_ID,
                resume_timeout=0,
                # Finite on purpose - see LAVALINK_NODE_RETRIES above. This same
                # counter also drives RUNTIME reconnects (gateway/node/
                # _connection.py reconnect -> attempt_connect shares it with the
                # initial connect), so a runtime reconnect burst is bounded too;
                # that does NOT make a Lavalink restart fatal, because
                # _supervise_lavalink below re-kicks a DISCONNECTED node forever
                # with its own backoff - sonolink's retries only bound one
                # burst, never the bot's lifetime.
                retries=LAVALINK_NODE_RETRIES,
            )
        except Exception:
            log.exception("Failed to register the Lavalink node; music disabled")
            return

        task = asyncio.ensure_future(self._supervise_lavalink())
        self._lavalink_task = task
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        def _on_supervisor_done(done: asyncio.Task) -> None:
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None:
                log.error("Lavalink supervisor exited unexpectedly: %s", exc)

        task.add_done_callback(_on_supervisor_done)

    async def _supervise_lavalink(self) -> None:
        """Keep the Lavalink node connected for the bot's whole lifetime.

        Runs as a background task (started by _start_lavalink, never awaited by
        setup_hook) so a dead or slow-to-boot Lavalink can never delay the
        Discord gateway login again.

        NodeStatus (sonolink/gateway/enums.py) has three values: DISCONNECTED,
        CONNECTING, CONNECTED. A healthy CONNECTED node is just polled on
        LAVALINK_CHECK_INTERVAL. CONNECTING is where this gets interesting:
        connect()/reconnect() (gateway/node/_connection.py) set
        NodeStatus.CONNECTING THEMSELVES, before running attempt_connect, and
        nothing resets it if attempt_connect's exception escapes uncaught - and
        it can: AioWebsocketManager.connect (network/_aiohttp.py) only wraps
        aiohttp.WSServerHandshakeError as a WebSocketError, so a refused TCP
        connection (aiohttp.ClientConnectorError - Lavalink not up yet, or
        mid-restart) is NOT caught by attempt_connect's `except WebSocketError`
        and escapes straight through connect()/reconnect(), past Client.start()
        (which only wraps the call in a bare `except Exception` and continues -
        gateway/client/__init__.py), leaving the node stuck CONNECTING forever
        with no loop running.

        But not every CONNECTING node is that escape - a runtime reconnect
        burst genuinely in progress is ALSO CONNECTING the whole time it
        retries (reconnect() sets the status once, before attempt_connect's
        loop runs). The two are told apart by whether ``_keep_alive`` is a
        LIVE task: ``connect_ws`` (gateway/node/_websocket.py) sets
        ``node._keep_alive`` to the keep-alive task right after a successful
        websocket handshake, and a reconnect burst runs *inside* that same
        task (``_handle_disconnect`` calls ``self.node.reconnect()`` directly,
        same file) - so while the burst runs, ``_keep_alive`` is that task,
        and it is not done. The escape case leaves it in one of two states
        instead: ``None`` (the very first connect attempt - connect_ws's
        ``ClientConnectorError`` happens before ``_keep_alive`` is ever
        assigned, since connect() isn't running inside any keep-alive task to
        begin with), or a DONE task (a runtime reconnect whose burst itself
        dies on the escape - the same ``ClientConnectorError`` now escapes
        ``reconnect()``, ``_handle_disconnect()`` and ``keep_alive_coro()`` in
        turn, ending that task with an exception, while ``_connection.py``'s
        ``except NodeError`` (1.2.1) / bare call (1.2.0) never catches a
        ``ClientConnectorError`` to reset the status it left at CONNECTING).
        Verified by reading both sonolink releases this bot runs: 1.2.0
        (gateway/node/_connection.py:44-101, _websocket.py:69-90) and 1.2.1
        (same paths, :44-108 / :69-90) - identical on every point above; 1.2.1
        only adds a ``try/except NodeError`` around ``attempt_connect()`` that
        a plain ``aiohttp.ClientConnectorError`` never triggers (it is not a
        ``NodeError``), so the escape is unchanged.

        So CONNECTING is tracked with a timer (``connecting_since``, the
        loop's own monotonic clock so no wall-clock drift matters) AND the
        live-watcher check above, each poll: without a live watcher, past
        LAVALINK_STUCK_AFTER is the escape, exactly as before. WITH one,
        sonolink is doing real work on its own schedule - a reconnect burst,
        or a websocket that opened and is waiting on Lavalink's "ready" op -
        and is left alone past LAVALINK_STUCK_AFTER, but not forever: past the
        higher LAVALINK_WEDGED_AFTER ceiling it is force-closed too (see that
        constant for why - the aiohttp session timeout bounding one connect
        attempt). Either way, closing force-resets the node to DISCONNECTED
        with ``_keep_alive`` cleared, so the NEXT iteration's start() can
        begin a fresh connect - connect() would otherwise refuse with
        "already connected; ignoring" while ``_keep_alive`` is set.

        A DISCONNECTED node needs the same live-watcher read before this
        supervisor touches it, for a different reason: the exhausted-retries
        branch (attempt_connect, handled WebSocketError path - not the escape
        above) sets ``node._status = NodeStatus.DISCONNECTED`` synchronously,
        then the SAME keep-alive task still has to dispatch "node_close" and
        run ``node.cleanup()`` before the task itself finishes - so there is a
        real window where this supervisor can observe DISCONNECTED while
        ``_keep_alive`` is still a live (not done) task. Touching it in that
        window - clearing it, or calling start(), which would itself be a
        no-op while ``_keep_alive is not None`` but races the task's own
        cleanup regardless - is skipped: this iteration just sleeps
        LAVALINK_BACKOFF_START and rechecks. Only once that task is DONE is
        the stale ``_keep_alive`` reference cleared (neither ``reconnect()``
        nor attempt_connect's exhausted branch ever clears it themselves -
        only ``close()`` does, and ``close()`` refuses to run on an
        already-DISCONNECTED node) and ``start()`` called. Left uncleared
        forever, that stale reference would make every future connect()
        silently no-op ("ignoring") forever, with no exception and no log
        louder than that one sonolink WARNING - a node that looks like it is
        being retried (this supervisor keeps calling start()) but never
        actually is.

        ``sl_client.start()`` is bounded with LAVALINK_START_TIMEOUT so this
        loop can never block on it, the same escape case above but during the
        INITIAL connect rather than a runtime reconnect (there the hang has no
        keep_alive task watching it at all, so nothing would otherwise return
        control here).
        """
        node_id = music_state.MUSIC_NODE_ID
        backoff = LAVALINK_BACKOFF_START
        outage = False
        connecting_since: float | None = None

        while True:
            try:
                node = self.sl_client.get_node(node_id)

                if node is not None and node.is_connected:
                    if outage:
                        log.info("Lavalink node %r is back.", node_id)
                        outage = False
                        backoff = LAVALINK_BACKOFF_START
                    connecting_since = None
                    await asyncio.sleep(LAVALINK_CHECK_INTERVAL)
                    continue

                if node is not None and node.is_connecting:
                    now = asyncio.get_running_loop().time()
                    if connecting_since is None:
                        connecting_since = now
                    else:
                        # Recomputed every pass: a live burst can die between
                        # one poll and the next, which should make this node
                        # eligible for the short STUCK_AFTER reset right away
                        # rather than waiting out the rest of WEDGED_AFTER.
                        ka = getattr(node, "_keep_alive", None)
                        live = ka is not None and not ka.done()
                        threshold = (
                            LAVALINK_WEDGED_AFTER if live else LAVALINK_STUCK_AFTER
                        )
                        if now - connecting_since > threshold:
                            # One WARNING per OUTAGE, not per reset: while
                            # Lavalink keeps refusing, every retry ends stuck
                            # again, and a line per cycle would just be noise.
                            if not outage:
                                if live:
                                    log.warning(
                                        "Lavalink node %r has been CONNECTING "
                                        "(sonolink still actively retrying) "
                                        "for over %ds - past the aiohttp "
                                        "session timeout a single attempt can "
                                        "take; resetting it and retrying in "
                                        "the background until it answers.",
                                        node_id,
                                        int(LAVALINK_WEDGED_AFTER),
                                    )
                                else:
                                    log.warning(
                                        "Lavalink node %r has been stuck "
                                        "CONNECTING for over %ds (no loop "
                                        "watching it - likely a refused "
                                        "connection that escaped sonolink's "
                                        "own retry handling); resetting it "
                                        "and retrying in the background until "
                                        "it answers.",
                                        node_id,
                                        int(LAVALINK_STUCK_AFTER),
                                    )
                                outage = True
                            else:
                                log.debug("Resetting stuck Lavalink node %r again.", node_id)
                            try:
                                await node.close()
                            except RuntimeError:
                                # Already DISCONNECTED by the time we got here
                                # (e.g. it resolved on its own in between) -
                                # fine, the next iteration retries normally.
                                pass
                            except Exception:
                                log.exception(
                                    "Failed to reset stuck Lavalink node %r",
                                    node_id,
                                )
                            connecting_since = None
                    await asyncio.sleep(LAVALINK_BACKOFF_START)
                    continue

                # DISCONNECTED (or no node registered at all). Not currently
                # CONNECTING, so any earlier streak is over.
                connecting_since = None

                if node is not None:
                    ka = getattr(node, "_keep_alive", None)
                    if ka is not None:
                        if ka.done():
                            # Stale reference from an exhausted runtime
                            # reconnect - see the docstring above. Clearing it
                            # is safe here precisely because node.close() is
                            # NOT an option: status is already DISCONNECTED,
                            # and close() raises RuntimeError on that.
                            node._keep_alive = None
                        else:
                            # The exhausted branch runs INSIDE this same task -
                            # it may still be dispatching "node_close" / running
                            # cleanup() after setting DISCONNECTED but before
                            # actually finishing. Leave it alone and recheck;
                            # start() would be a no-op anyway while
                            # _keep_alive is not None, and clearing it here
                            # would race that task's own cleanup.
                            await asyncio.sleep(LAVALINK_BACKOFF_START)
                            continue

                try:
                    await asyncio.wait_for(
                        self.sl_client.start(), timeout=LAVALINK_START_TIMEOUT
                    )
                except TimeoutError:
                    if not outage:
                        log.warning(
                            "Lavalink node %r did not finish connecting within "
                            "%ds; retrying in the background until it answers.",
                            node_id,
                            int(LAVALINK_START_TIMEOUT),
                        )
                        outage = True

                node = self.sl_client.get_node(node_id)
                if node is not None and (node.is_connected or node.is_connecting):
                    # Connected, or now actively (re)trying on its own (the
                    # handled-503 case) - either way, the top of the loop
                    # handles it next, with no extra sleep here.
                    continue

                if not outage:
                    log.warning(
                        "Lavalink node %r is unreachable; retrying in the "
                        "background (backoff up to %ds) until it answers.",
                        node_id,
                        int(LAVALINK_BACKOFF_CAP),
                    )
                    outage = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, LAVALINK_BACKOFF_CAP)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Lavalink supervisor iteration failed; continuing")
                # Never let a bug in one iteration become a zero-await hot
                # loop (e.g. get_node() itself raising every single pass).
                await asyncio.sleep(LAVALINK_BACKOFF_START)

    async def close(self) -> None:
        try:
            # Let cogs stop their background tasks before tearing down the
            # connector those tasks share.
            await super().close()
        finally:
            if self._lavalink_task is not None:
                self._lavalink_task.cancel()
                # Wait for the supervisor to actually unwind before anything
                # below touches the same sonolink Node it was polling - a bare
                # cancel() only schedules that, it does not wait for it, so
                # without this await the supervisor could still be mid-iteration
                # (e.g. awaiting node.close()) when sl_client.close() runs right
                # after it below.
                try:
                    await self._lavalink_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception(
                        "Lavalink supervisor raised while shutting down"
                    )
            try:
                await self.sl_client.close()
            except Exception:
                log.exception("Failed to close the Lavalink client cleanly")
            if self.http_session is not None and not self.http_session.closed:
                await self.http_session.close()


async def get_prefix(bot: Yasuho, message: discord.Message):
    if not message.guild:
        return DEFAULT_PREFIX

    # The DB only stores custom prefixes (overrides); everything else falls back
    # to DEFAULT_PREFIX so changing the default later applies everywhere at once.
    prefix = bot.prefixes.get(message.guild.id) or DEFAULT_PREFIX
    return commands.when_mentioned_or(prefix)(bot, message)


def _attach_file_logging():
    """Add a rotating file handler to the root logger, alongside stderr.

    Everything (discord.*, our cogs, aiohttp.access) also lands in
    logs/yasuho.log so the terminal output stays as-is but there is a durable
    on-disk trail. This is bootstrap code: any failure here (permissions, disk)
    must never stop the bot from starting, so it degrades to terminal-only
    logging with a one-line warning.
    """
    try:
        log_dir = os.path.join(os.path.dirname(__file__), "logs")
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "yasuho.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "[{asctime}] [{levelname:<8}] {name}: {message}",
                "%Y-%m-%d %H:%M:%S",
                style="{",
            )
        )
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
    except Exception as e:
        # Fall back to terminal-only logging; startup must proceed regardless.
        print(f"Warning: file logging disabled ({e}); using terminal only.", file=sys.stderr)


async def main():
    # Configure logging ourselves since we use asyncio.run + bot.start (not bot.run,
    # which would call this for us). Routes discord.py + our own loggers to stderr.
    discord.utils.setup_logging(level=logging.INFO)
    _attach_file_logging()
    enable_mobile_status()
    # max_size=30: local Postgres allows up to 100 connections, so this leaves
    # ample headroom for psql/backup/other clients while giving the bot more
    # room than the old ceiling of 20 before callers start queuing for a slot.
    async with asyncpg.create_pool(
        POSTGRESQL_URI, min_size=5, max_size=30, command_timeout=60
    ) as pool:
        async with Yasuho(db_pool=pool) as bot:
            await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

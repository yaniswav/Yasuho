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

from tools import backup, fixups, i18n, music_state
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


def _module_has_setup(path):
    """Cheap text check for a `setup` entry point, without importing the module."""
    try:
        with open(path, encoding="utf-8") as fp:
            return "def setup(" in fp.read()
    except OSError:
        return False


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
        if root != cogs_dir and os.path.isfile(init_path) and _module_has_setup(init_path):
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
            enable_debug_events=True,
            help_command=None,
        )

        self.db_pool = db_pool
        self.http_session = None
        self.image_render_semaphore = asyncio.Semaphore(2)
        self.default_prefix = DEFAULT_PREFIX
        # sonolink client for music (Lavalink v4). Created here but only started
        # in setup_hook, and only when [Lavalink] is configured.
        self.sl_client = sonolink.Client(self)
        # In-memory caches for hot / rarely-changing data, loaded in setup_hook
        # and invalidated by the owning cogs (mirrors the prefixes cache).
        self.prefixes = {}
        self.blacklist = set()
        self.autoroles = {}
        self.muteroles = {}
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

        self.prefixes = dict(
            await self.db_pool.fetch("SELECT guild_id, prefix FROM prefixes;")
        )
        self.blacklist = {
            r["member_id"]
            for r in await self.db_pool.fetch("SELECT member_id FROM blbot;")
        }
        self.autoroles = dict(
            await self.db_pool.fetch("SELECT guild_id, role_id FROM autorole;")
        )
        self.muteroles = dict(
            await self.db_pool.fetch("SELECT guild_id, role_id FROM muterole;")
        )

        for extension in discover_extensions():
            try:
                await self.load_extension(extension)
                log.info("Loaded %s", extension)
            except Exception:
                log.exception("Error while trying to load %s", extension)

        log.info("Prefix count: %d", len(self.prefixes))
        log.info("i18n locales: %s", ", ".join(sorted(i18n.LOCALES)))

        # Localize slash command descriptions/choices in the Discord command
        # picker (the response text is handled separately by tools/i18n.py).
        await self.tree.set_translator(YasuhoTranslator())

        # Connect to Lavalink for music ONLY if it is configured. Skipping the
        # attempt avoids the startup delay and reconnect spam when there is no
        # Lavalink server (music is deferred). Set [Lavalink] uri (and password)
        # in config to enable it.
        try:
            lavalink_uri = config_loader.get("Lavalink", "uri")
        except Exception:
            lavalink_uri = None
        if lavalink_uri:
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
                )
                await self.sl_client.start()
                # Persist the session id for diagnostics only (nothing reads it
                # back yet). node.session_id raises RuntimeError, not
                # AttributeError, until connected - check is_connected first so
                # a slow/failed connect can't crash startup.
                node = self.sl_client.get_node(music_state.MUSIC_NODE_ID)
                if node is not None and node.is_connected:
                    await music_state.save_session(
                        self.db_pool, music_state.MUSIC_NODE_ID, node.session_id
                    )
            except Exception as e:
                log.warning("Lavalink unavailable, music disabled: %s", e)
        else:
            log.info("Lavalink not configured; music disabled.")

    async def close(self) -> None:
        try:
            # Let cogs stop their background tasks before tearing down the
            # connector those tasks share.
            await super().close()
        finally:
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
    async with asyncpg.create_pool(
        POSTGRESQL_URI, min_size=5, max_size=20, command_timeout=60
    ) as pool:
        async with Yasuho(db_pool=pool) as bot:
            await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

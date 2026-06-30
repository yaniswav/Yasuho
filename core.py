import asyncio
import logging
import os

import asyncpg
import discord
import sonolink
from discord.ext import commands

from tools import i18n
from tools.config_loader import config_loader
from tools.mobile_status import enable_mobile_status

log = logging.getLogger(__name__)

DEFAULT_PREFIX = config_loader.get("BotInfo", "DefaultPrefix")
TOKEN = config_loader.get("Bot_Token", "Token")
POSTGRESQL_URI = config_loader.get("Database", "PostgreSQL")


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
        intents = discord.Intents.all()

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

    async def setup_hook(self) -> None:
        # Ensure the database schema exists (idempotent CREATE TABLE IF NOT EXISTS).
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as fp:
                await self.db_pool.execute(fp.read())

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
                self.sl_client.create_node(uri=lavalink_uri, password=lavalink_pw)
                await self.sl_client.start()
            except Exception as e:
                log.warning("Lavalink unavailable, music disabled: %s", e)
        else:
            log.info("Lavalink not configured; music disabled.")


async def get_prefix(bot: Yasuho, message: discord.Message):
    if not message.guild:
        return DEFAULT_PREFIX

    # The DB only stores custom prefixes (overrides); everything else falls back
    # to DEFAULT_PREFIX so changing the default later applies everywhere at once.
    prefix = bot.prefixes.get(message.guild.id) or DEFAULT_PREFIX
    return commands.when_mentioned_or(prefix)(bot, message)


async def main():
    # Configure logging ourselves since we use asyncio.run + bot.start (not bot.run,
    # which would call this for us). Routes discord.py + our own loggers to stderr.
    discord.utils.setup_logging(level=logging.INFO)
    enable_mobile_status()
    async with asyncpg.create_pool(
        POSTGRESQL_URI, min_size=5, max_size=20, command_timeout=60
    ) as pool:
        async with Yasuho(db_pool=pool) as bot:
            await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

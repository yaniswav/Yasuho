import asyncio
import logging
import os

import asyncpg
import discord
import wavelink
from discord.ext import commands

from tools.config_loader import config_loader
from tools.mobile_status import enable_mobile_status

log = logging.getLogger(__name__)

DEFAULT_PREFIX = config_loader.get("BotInfo", "DefaultPrefix")
TOKEN = config_loader.get("Bot_Token", "Token")
POSTGRESQL_URI = config_loader.get("Database", "PostgreSQL")
EXTENSIONS = config_loader.getlist("Extension", "Extensions")


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
        self.prefixes = {}

    async def setup_hook(self) -> None:
        # Ensure the database schema exists (idempotent CREATE TABLE IF NOT EXISTS).
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as fp:
                await self.db_pool.execute(fp.read())

        self.prefixes = dict(
            await self.db_pool.fetch("SELECT guild_id, prefix FROM prefixes;")
        )

        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
                log.info("Loading %s", extension)
            except commands.ExtensionNotFound:
                log.error("Extension not found: %s", extension)
            except Exception:
                log.exception("Error while trying to load %s", extension)

        log.info("Prefix count: %d", len(self.prefixes))

        # Connect to Lavalink for music. Non-fatal AND non-blocking: wavelink
        # retries forever on failure, so we cap it with a timeout — if no
        # Lavalink server answers, give up and start the bot without music.
        try:
            node = wavelink.Node(uri="http://0.0.0.0:2333", password="youshallnotpass")
            await asyncio.wait_for(
                wavelink.Pool.connect(client=self, nodes=[node]), timeout=8
            )
        except Exception as e:
            log.warning("Lavalink unavailable, music disabled: %s", e)


async def get_prefix(bot: Yasuho, message: discord.Message):
    if not message.guild:
        return DEFAULT_PREFIX

    prefix = bot.prefixes.get(message.guild.id, None)

    if not prefix:
        query = """
                INSERT INTO prefixes
                (guild_id, prefix)
                VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET prefix = $3;"""

        await bot.db_pool.execute(
            query, message.guild.id, DEFAULT_PREFIX, DEFAULT_PREFIX
        )
        prefix = bot.prefixes[message.guild.id] = DEFAULT_PREFIX

    return commands.when_mentioned_or(prefix)(bot, message)


async def main():
    # Configure logging ourselves since we use asyncio.run + bot.start (not bot.run,
    # which would call this for us). Routes discord.py + our own loggers to stderr.
    discord.utils.setup_logging(level=logging.INFO)
    enable_mobile_status()
    async with asyncpg.create_pool(POSTGRESQL_URI, command_timeout=60) as pool:
        async with Yasuho(db_pool=pool) as bot:
            await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

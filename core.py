import asyncio
import asyncpg
import discord
from discord.ext import commands
import wavelink
from tools.config_loader import config_loader

DEFAULT_PREFIX = config_loader.get('BotInfo', 'DefaultPrefix')
TOKEN = config_loader.get('Bot_Token', 'Token')
POSTGRESQL_URI = config_loader.get('Database', 'PostgreSQL')
EXTENSIONS = config_loader.getlist('Extension', 'Extensions')

class Yasuho(commands.Bot):
    def __init__(self, *args, db_pool: asyncpg.Pool):
        allowed_mentions = discord.AllowedMentions(
            roles=False, everyone=True, users=True
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

    async def setup_hook(self) -> None:
        self.prefixes = dict(
            await self.db_pool.fetch("SELECT guild_id, prefix FROM prefixes;")
        )

        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
                print(f"[Loading] {extension}")
            except commands.ExtensionNotFound:
                print(f"[Not found]: {extension}")
            except Exception as e:
                print(f"[Error] While trying to load {extension}: {e}")

        print(f"Prefix count: {len(self.prefixes)}")

        node: wavelink.Node = wavelink.Node(
            uri="http://0.0.0.0:2333",
            password="youshallnotpass",
        )

        await wavelink.Pool.connect(client=self, nodes=[node])


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
    async with asyncpg.create_pool(POSTGRESQL_URI, command_timeout=60) as pool: 
        async with Yasuho(commands.when_mentioned, db_pool=pool) as bot:
            await bot.start(TOKEN) 

asyncio.run(main())

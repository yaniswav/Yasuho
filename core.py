import asyncio
import logging
import config
import logging.handlers
import os
import asyncpg
import random
import wavelink

from typing import List, Optional
from aiohttp import ClientSession
from asyncio import *

import discord
from discord import Interaction
from discord.ext import commands, tasks
from discord.app_commands import AppCommandError

initial_extensions = config.initial_extensions


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

        for extension in initial_extensions:
            print(f"loading {extension}")
            await self.load_extension(extension)

        print(f"Prefix count: {len(self.prefixes)}")

        node: wavelink.Node = wavelink.Node(
            uri="http://0.0.0.0:2333",
            password="youshallnotpass",
        )

        await wavelink.Pool.connect(client=self, nodes=[node])


async def get_prefix(bot: Yasuho, message: discord.Message):
    if not message.guild:
        return config.default_prefix

    prefix = bot.prefixes.get(message.guild.id, None)

    if not prefix:
        query = """
                INSERT INTO prefixes 
                (guild_id, prefix) 
                VALUES ($1, $2) 
                ON CONFLICT (guild_id) DO UPDATE SET prefix = $3;"""

        await bot.db_pool.execute(
            query, message.guild.id, config.default_prefix, config.default_prefix
        )
        prefix = bot.prefixes[message.guild.id] = config.default_prefix

    return commands.when_mentioned_or(prefix)(bot, message)


async def main():
    async with asyncpg.create_pool(config.postgresql, command_timeout=60) as pool:
        async with Yasuho(commands.when_mentioned, db_pool=pool) as bot:
            await bot.start(config.token)


asyncio.run(main())

import asyncio
import discord
import random
import datetime
import config
import logging
from itertools import cycle

from discord.ext import commands, tasks
from discord.ext.commands.cooldowns import BucketType
from discord.utils import find

status = cycle(["@Yasuho help", "https://yasuho.xyz"])


class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        channel = None
        self.status = status
        self.change_status.start()

    def cog_unload(self):
        self.change_status.cancel()

    @tasks.loop(seconds=15)
    async def change_status(self):
        await self.bot.change_presence(
            status=discord.Status.idle,
            activity=discord.CustomActivity(
                type=discord.ActivityType.custom,
                emoji=discord.PartialEmoji(name="🌺"),
                name=next(self.status),
            ),
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        query = """
        
            INSERT INTO prefixes 
            (guild_id, prefix) 
            VALUES ($1, $2) 
            ON CONFLICT (guild_id) DO UPDATE SET prefix = $3;

            """
        await self.bot.pool.execute(
            query, guild.id, config.default_prefix, config.default_prefix
        )
        self.bot.cache[guild.id] = config.default_prefix
        names = [
            "general",
            "général",
            "lobby",
            "chat",
            "welcome",
            "bienvenue",
            "commands",
            "cmds",
            "hub",
            "arrival",
            "command",
            "bots-commands",
            "bots",
            
        ]

        general = find(lambda x: x.name in names, guild.text_channels)

        if general and general.permissions_for(guild.me).send_messages:
            await general.send(
                f"🌺 Beep boop **{guild.name}**! To get started type `y!help`"
            )

        else:
            try:
                await guild.system_channel.send(
                    f"🌺 Beep boop **{guild.name}**! To get started type `y!help`"
                )
            except:
                await guild.owner.send(
                    f"🌺 Beep boop **{guild.name}**! To get started type `y!help`"
                )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        query = """
                DELETE FROM prefixes
                WHERE guild_id = $1

                """
        await self.bot.pool.execute(query, guild.id)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

async def setup(bot):
    await bot.add_cog(Events(bot))

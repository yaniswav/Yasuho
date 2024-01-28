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


    @tasks.loop(seconds=20)
    async def change_status(self):
        await self.bot.change_presence(
            status=discord.Status.idle,
            activity=discord.CustomActivity(
                type=discord.ActivityType.custom,
                emoji=discord.PartialEmoji(name="🌺"),
                name=next(self.status),
            ),
        )

    @change_status.before_loop
    async def before_change_status(self):
        print("[STATUS] Waiting for bot to be ready to set custom status.")
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        query = """
        
            INSERT INTO prefixes 
            (guild_id, prefix) 
            VALUES ($1, $2) 
            ON CONFLICT (guild_id) DO UPDATE SET prefix = $3;

            """
        await self.bot.db_pool.execute(
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
        await self.bot.db_pool.execute(query, guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_id = member.guild.id
        pool = self.bot.db_pool

        # Verify if the member is blacklisted
        query = "SELECT member_id FROM blbot WHERE member_id = $1;"
        blacklisted = await pool.fetchval(query, member.id)
        if blacklisted:
            try:
                await member.guild.ban(member, reason="Blacklisted from bot")
                try:
                    await member.send("You are blacklisted from bot. You can ask to be unblacklisted by send a message to <@228895251576782858>")
                except discord.HTTPException:
                    pass
            except discord.HTTPException:
                pass
            return

        # Attribute a role to the member if the guild has autorole
        query = "SELECT role_id FROM autorole WHERE guild_id = $1;"
        role_id = await pool.fetchval(query, guild_id)
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role)
                except discord.HTTPException:
                    pass


    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

async def setup(bot):
    await bot.add_cog(Events(bot))

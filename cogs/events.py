import logging
from itertools import cycle

import discord
from discord.ext import commands, tasks
from discord.utils import find

from tools.config_loader import config_loader

log = logging.getLogger(__name__)

DEFAULT_PREFIX = config_loader.get("BotInfo", "DefaultPrefix")

status = cycle(["@Yasuho help", "https://yasuho.xyz"])


class Events(commands.Cog):
    """Global event listeners (status loop, guild joins/leaves, member events)."""

    def __init__(self, bot):
        self.bot = bot
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
        log.info("Waiting for bot to be ready to set custom status.")
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
            query, guild.id, DEFAULT_PREFIX, DEFAULT_PREFIX
        )
        self.bot.prefixes[guild.id] = DEFAULT_PREFIX
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
            msg = f"🌺 Beep boop **{guild.name}**! To get started type `y!help`"
            try:
                if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                    await guild.system_channel.send(msg)
                elif guild.owner:
                    await guild.owner.send(msg)
            except Exception:
                log.exception("Failed to send welcome message")

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

        # Re-apply the mute role to evaders who left while muted and rejoined.
        muted = await pool.fetchval(
            "SELECT member_id FROM mutedmembers WHERE mguild_id = $1 AND member_id = $2;",
            guild_id,
            member.id,
        )
        if muted:
            mute_role_id = await pool.fetchval(
                "SELECT role_id FROM muterole WHERE guild_id = $1;", guild_id
            )
            mute_role = member.guild.get_role(mute_role_id) if mute_role_id else None
            if mute_role:
                try:
                    await member.add_roles(
                        mute_role, reason="Re-muted on rejoin (mute evasion)"
                    )
                except discord.HTTPException:
                    log.exception("Failed to re-apply mute")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        # Keep the mute role effective in newly created channels so muted members
        # cannot simply talk in a freshly created channel.
        mute_role_id = await self.bot.db_pool.fetchval(
            "SELECT role_id FROM muterole WHERE guild_id = $1;", channel.guild.id
        )
        if not mute_role_id:
            return
        mute_role = channel.guild.get_role(mute_role_id)
        if mute_role is None:
            return
        try:
            if isinstance(channel, discord.VoiceChannel):
                await channel.set_permissions(mute_role, speak=False)
            elif isinstance(channel, (discord.TextChannel, discord.CategoryChannel)):
                await channel.set_permissions(
                    mute_role,
                    send_messages=False,
                    add_reactions=False,
                    send_tts_messages=False,
                )
        except discord.HTTPException:
            log.exception("Failed to sync mute role perms")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return


async def setup(bot):
    await bot.add_cog(Events(bot))

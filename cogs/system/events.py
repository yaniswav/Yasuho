import logging
from itertools import cycle

import discord
from discord.ext import commands, tasks

from tools import retention
from tools.i18n import _

log = logging.getLogger(__name__)

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
        try:
            await self.bot.change_presence(
                status=discord.Status.online,
                activity=discord.CustomActivity(
                    type=discord.ActivityType.custom,
                    emoji=discord.PartialEmoji(name="🌺"),
                    name=next(self.status),
                ),
            )
        except (discord.ConnectionClosed, ConnectionError):
            # The tick landed while the gateway was mid-reconnect, so the write
            # hit a closing socket. This is transient and self-heals (the next
            # tick retries on the resumed connection), so it is not worth an
            # ERROR + traceback - just note it at debug.
            log.debug("status rotation skipped: gateway reconnecting")
        except Exception:
            # Never let a one-off failure permanently stop the rotation;
            # the next iteration will try again.
            log.exception("status rotation failed")

    @change_status.before_loop
    async def before_change_status(self):
        log.info("Waiting for bot to be ready to set custom status.")
        await self.bot.wait_until_ready()

    @change_status.error
    async def change_status_error(self, error):
        log.exception("status rotation loop crashed; restarting", exc_info=error)
        self.change_status.restart()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        # A rejoin inside the grace period restores the existing configuration.
        # Cancel its scheduled purge and refill the startup caches evicted when
        # the bot left.
        try:
            cancelled = await retention.cancel_guild_purge(
                self.bot.db_pool, guild.id
            )
            row = await self.bot.db_pool.fetchrow(
                "SELECT "
                "(SELECT prefix FROM prefixes WHERE guild_id = $1) AS prefix, "
                "(SELECT role_id FROM autorole WHERE guild_id = $1) AS autorole, "
                "(SELECT role_id FROM muterole WHERE guild_id = $1) AS muterole",
                guild.id,
            )
            for attr, column in (
                ("prefixes", "prefix"),
                ("autoroles", "autorole"),
                ("muteroles", "muterole"),
            ):
                cache = getattr(self.bot, attr)
                value = row[column]
                if value is None:
                    cache.pop(guild.id, None)
                else:
                    cache[guild.id] = value

            leveling = self.bot.get_cog("Leveling")
            if leveling is not None:
                await leveling.refresh_guild_config(guild.id)

            rooms = self.bot.get_cog("TemporaryRooms")
            if rooms is not None:
                rooms._index_guild(
                    guild.id, await rooms._load_hubs(guild.id)
                )
            if cancelled:
                log.info(
                    "Cancelled scheduled data purge after guild %s rejoined",
                    guild.id,
                )
        except Exception:
            # Joining must remain usable during a retention DB outage; the
            # maintenance worker also refuses to purge active cached guilds.
            log.exception(
                "Failed to cancel/restore retention state for guild %s",
                guild.id,
            )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        try:
            purge_after = await retention.schedule_guild_purge(
                self.bot.db_pool, guild.id
            )
            log.info(
                "Guild %s data scheduled for purge at %s",
                guild.id,
                purge_after.isoformat(),
            )
        except Exception:
            # Never turn a transient outage into immediate data loss.
            log.exception(
                "Failed to schedule data purge for departed guild %s", guild.id
            )
        finally:
            retention.invalidate_guild_caches(self.bot, guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_id = member.guild.id
        pool = self.bot.db_pool

        # Verify if the member is blacklisted (cached in memory at startup).
        if member.id in self.bot.blacklist:
            try:
                await member.guild.ban(member, reason="Blacklisted from bot")
                try:
                    await member.send(
                        _(
                            "You are blacklisted from bot. You can ask to be "
                            "unblacklisted by send a message to "
                            "<@228895251576782858>"
                        )
                    )
                except discord.HTTPException:
                    pass
            except discord.HTTPException:
                pass
            return

        # Attribute a role to the member if the guild has autorole (cached).
        role_id = self.bot.autoroles.get(guild_id)
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
            mute_role_id = self.bot.muteroles.get(guild_id)
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
        mute_role_id = self.bot.muteroles.get(channel.guild.id)
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

async def setup(bot):
    await bot.add_cog(Events(bot))

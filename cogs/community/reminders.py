import asyncio
import datetime
import json
import logging

import discord
from discord.ext import commands

from tools.time import ShortTime, UserFriendlyTime, human_timedelta

log = logging.getLogger(__name__)


class Reminder(commands.Cog):
    """Reminders and time-based timers (reminders, temp-bans)."""

    def __init__(self, bot):
        self.bot = bot
        self.bot.reminder = self
        self._have_data = asyncio.Event()
        self._task = self.bot.loop.create_task(self.dispatch_timers())

    def cog_unload(self):
        self._task.cancel()
        self.bot.reminder = None

    async def get_tzinfo(self, user_id):
        return datetime.timezone.utc

    async def create_timer(self, when, event, **extra):
        row = await self.bot.db_pool.fetchrow(
            "INSERT INTO timers(event, expires, created, extra) "
            "VALUES($1, $2, $3, $4::jsonb) RETURNING id",
            event,
            when,
            datetime.datetime.now(datetime.timezone.utc),
            json.dumps(extra),
        )
        self._have_data.set()
        return row

    async def get_active_timer(self):
        return await self.bot.db_pool.fetchrow(
            "SELECT * FROM timers ORDER BY expires LIMIT 1"
        )

    async def dispatch_timers(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                self._have_data.clear()
                row = await self.get_active_timer()
                if row is None:
                    await self._have_data.wait()
                    continue

                now = datetime.datetime.now(datetime.timezone.utc)
                delta = (row["expires"] - now).total_seconds()
                if delta > 0:
                    try:
                        await asyncio.wait_for(
                            self._have_data.wait(), timeout=min(delta, 86400)
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue

                await self.bot.db_pool.execute(
                    "DELETE FROM timers WHERE id=$1", row["id"]
                )
                await self.call_timer(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error while dispatching timers")
                await asyncio.sleep(5)

    async def call_timer(self, row):
        extra = row["extra"]
        extra = json.loads(extra) if isinstance(extra, str) else (extra or {})
        event = row["event"]
        try:
            if event == "reminder":
                ch = self.bot.get_channel(extra["channel_id"])
                if ch is None:
                    try:
                        ch = await self.bot.fetch_channel(extra["channel_id"])
                    except discord.HTTPException:
                        ch = None
                if ch:
                    await ch.send(
                        f"<@{extra['author_id']}>, "
                        f"{human_timedelta(row['created'])}: {extra['message']}"
                    )
            elif event == "tempban":
                g = self.bot.get_guild(extra["guild_id"])
                if g:
                    await g.unban(
                        discord.Object(id=extra["user_id"]),
                        reason="Temp-ban expired",
                    )
        except Exception:
            log.exception("Error while calling timer")

    @commands.hybrid_command()
    async def remind(
        self,
        ctx,
        *,
        when: UserFriendlyTime(commands.clean_content, default="something"),
    ):
        """Reminds you of something after a certain amount of time."""

        await self.create_timer(
            when.dt,
            "reminder",
            author_id=ctx.author.id,
            channel_id=ctx.channel.id,
            message=when.arg,
        )
        await ctx.send(
            f"Okay, reminding you {discord.utils.format_dt(when.dt, 'R')}: {when.arg}"
        )

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    async def tempban(
        self,
        ctx,
        member: discord.User,
        duration: ShortTime,
        *,
        reason: str = None,
    ):
        """Temporarily bans a member for the given duration."""

        await ctx.guild.ban(member, reason=reason)
        await self.create_timer(
            duration.dt,
            "tempban",
            guild_id=ctx.guild.id,
            user_id=member.id,
        )
        await ctx.send(
            f"Banned {member} until {discord.utils.format_dt(duration.dt, 'F')}."
        )


async def setup(bot):
    await bot.add_cog(Reminder(bot))

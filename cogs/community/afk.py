import logging

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.i18n import _
from tools.time import human_timedelta

log = logging.getLogger(__name__)


class AFK(commands.Cog):
    """Let members set an AFK status and notify others when they are mentioned."""

    def __init__(self, bot):
        self.bot = bot
        self.afk_users = set()

    async def cog_load(self):
        rows = await self.bot.db_pool.fetch("SELECT user_id FROM afk")
        self.afk_users = {row["user_id"] for row in rows}

    @commands.hybrid_command()
    @commands.guild_only()
    @discord.app_commands.describe(message="Why you're away (shown to anyone who pings you).")
    async def afk(self, ctx, *, message: str = "AFK"):
        """Set your AFK status, with an optional message."""

        query = """
            INSERT INTO afk
            (user_id, message)
            VALUES
            ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET message = $2, since = now();
            """

        await self.bot.db_pool.execute(query, ctx.author.id, message)
        self.afk_users.add(ctx.author.id)
        embed = discord.Embed(colour=random_colour())
        embed.description = _("{user} you are now AFK: {message}").format(
            user=ctx.author.mention, message=message
        )
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        try:
            # (1) The author is back from being AFK.
            if message.author.id in self.afk_users:
                deleted = await self.bot.db_pool.fetchrow(
                    "DELETE FROM afk WHERE user_id = $1 AND now() - since > interval '3 seconds' RETURNING since",
                    message.author.id,
                )
                if deleted:
                    self.afk_users.discard(message.author.id)
                    await message.channel.send(
                        _("Welcome back {user}, you were AFK for {duration}.").format(
                            user=message.author.mention,
                            duration=human_timedelta(deleted["since"], suffix=False),
                        ),
                        delete_after=10,
                    )

            # (2) Notify when an AFK user gets mentioned.
            for user in message.mentions:
                if user.id not in self.afk_users:
                    continue
                r = await self.bot.db_pool.fetchrow(
                    "SELECT message, since FROM afk WHERE user_id = $1", user.id
                )
                if r:
                    await message.channel.send(
                        _("{name} is AFK: {message} ({duration})").format(
                            name=user.display_name,
                            message=r["message"],
                            duration=human_timedelta(r["since"]),
                        )
                    )

        except Exception:
            log.exception("on_message handler failed")


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))

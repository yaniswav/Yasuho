import logging
import random
import time

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.paginator import Paginator, paginate_lines

log = logging.getLogger(__name__)


class Leveling(commands.Cog):
    """XP and leveling commands."""

    COOLDOWN = 60

    def __init__(self, bot):
        self.bot = bot
        self._cooldowns = {}

    @staticmethod
    def level_for_xp(xp):
        return int((xp / 100) ** 0.5)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        key = (message.guild.id, message.author.id)
        now = time.time()

        if now - self._cooldowns.get(key, 0) < self.COOLDOWN:
            return

        self._cooldowns[key] = now
        gain = random.randint(15, 25)

        try:
            query = """
                INSERT INTO levels (guild_id, user_id, xp)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET xp = levels.xp + $3
                RETURNING xp;
                """

            new_xp = await self.bot.db_pool.fetchval(
                query, message.guild.id, message.author.id, gain
            )
            old_level = self.level_for_xp(new_xp - gain)
            new_level = self.level_for_xp(new_xp)

            if new_level > old_level:
                await message.channel.send(
                    f"{message.author.mention} reached level **{new_level}**!"
                )

        except Exception:
            log.exception("Failed to update XP")

    @commands.hybrid_command()
    @commands.guild_only()
    async def rank(self, ctx, member: discord.Member = None):
        """Shows your level and XP, or another member's."""

        member = member or ctx.author

        query = """
            SELECT xp FROM levels
            WHERE guild_id = $1 AND user_id = $2;
            """

        xp = await self.bot.db_pool.fetchval(query, ctx.guild.id, member.id) or 0
        level = self.level_for_xp(xp)
        needed = (level + 1) ** 2 * 100

        embed = discord.Embed(
            title=f"Rank | {member.display_name}",
            colour=random_colour(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=f"**{level}**")
        embed.add_field(name="XP", value=f"**{xp}**")
        embed.add_field(
            name="XP for next level",
            value=f"**{needed - xp}** ({xp}/{needed})",
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["leaderboard", "top"])
    @commands.guild_only()
    async def levels(self, ctx):
        """Shows the ranked members of the guild."""

        query = """
            SELECT user_id, xp FROM levels
            WHERE guild_id = $1
            ORDER BY xp DESC
            LIMIT 50;
            """

        rows = await self.bot.db_pool.fetch(query, ctx.guild.id)

        if not rows:
            embed = discord.Embed(
                title=f"Leaderboard | {ctx.guild.name}",
                description="No one has earned any XP yet!",
                colour=random_colour(),
            )
            return await ctx.send(embed=embed)

        lines = []
        for index, row in enumerate(rows, start=1):
            uid = row["user_id"]
            xp = row["xp"]
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            level = self.level_for_xp(xp)
            lines.append(f"**{index}.** {name} — level **{level}** ({xp} XP)")

        embeds = paginate_lines(lines, title=f"Leaderboard | {ctx.guild.name}")
        await Paginator(embeds, author_id=ctx.author.id).start(ctx)


async def setup(bot):
    await bot.add_cog(Leveling(bot))

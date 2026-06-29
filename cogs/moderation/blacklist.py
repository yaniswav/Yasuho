import logging

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.paginator import Paginator, paginate_lines

log = logging.getLogger(__name__)


class Blacklist(commands.Cog):
    """Owner-only bot-wide blacklist management."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        return await self.bot.is_owner(ctx.author)

    @commands.hybrid_group(name="blacklist", aliases=["bl"])
    async def blacklist(self, ctx):
        """Bot-wide blacklist related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @blacklist.command(name="add")
    async def blacklist_add(self, ctx, user: discord.User):
        """Add a user to the bot-wide blacklist."""

        query = """
            INSERT INTO blbot (member_id)
            VALUES ($1)
            ON CONFLICT (member_id) DO NOTHING;
            """

        await self.bot.db_pool.execute(query, user.id)
        self.bot.blacklist.add(user.id)
        await ctx.send(f"{user} blacklisted.")

        for g in self.bot.guilds:
            try:
                await g.ban(user, reason="Blacklisted")
            except Exception:
                log.exception("Failed to ban %s in %s", user, g)

    @blacklist.command(name="remove")
    async def blacklist_remove(self, ctx, user: discord.User):
        """Remove a user from the bot-wide blacklist."""

        query = """DELETE FROM blbot WHERE member_id = $1;"""

        await self.bot.db_pool.execute(query, user.id)
        self.bot.blacklist.discard(user.id)
        await ctx.send(f"{user} removed from the blacklist.")

        for g in self.bot.guilds:
            try:
                await g.unban(user, reason="Unblacklisted")
            except discord.NotFound:
                pass
            except Exception:
                log.exception("Failed to unban %s in %s", user, g)

    @blacklist.command(name="list")
    async def blacklist_list(self, ctx):
        """List every blacklisted user."""

        if not self.bot.blacklist:
            embed = discord.Embed(
                title="Blacklist",
                description="The blacklist is empty.",
                colour=random_colour(),
            )
            await ctx.send(embed=embed)
            return

        lines = []
        for member_id in self.bot.blacklist:
            user = self.bot.get_user(member_id)
            if user is not None:
                lines.append(f"{user} (`{member_id}`)")
            else:
                lines.append(f"`{member_id}`")

        await Paginator(
            paginate_lines(lines, title="Blacklist"), author_id=ctx.author.id
        ).start(ctx)


async def setup(bot):
    await bot.add_cog(Blacklist(bot))

import logging

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.paginator import Paginator, paginate_lines

log = logging.getLogger(__name__)


class ReactionRoles(commands.Cog):
    """Assign roles to members when they react to a message."""

    def __init__(self, bot):
        self.bot = bot
        self.cache = {}

    async def cog_load(self):
        query = "SELECT message_id, emoji, role_id FROM reaction_roles;"
        rows = await self.bot.db_pool.fetch(query)
        self.cache = {
            (row["message_id"], row["emoji"]): row["role_id"] for row in rows
        }

    @commands.hybrid_group(aliases=["rr"])
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole(self, ctx):
        """Reaction-role related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @reactionrole.command(name="add")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole_add(
        self, ctx, message_id: str, emoji: str, role: discord.Role
    ):
        """Map an emoji on a message to a role."""

        try:
            mid = int(message_id)
        except ValueError:
            await ctx.send("That doesn't look like a valid message ID.")
            return

        # Defer the slash interaction (and show a typing indicator for prefix)
        # so the message fetch, reaction add and DB write can't blow the 3s
        # interaction window.
        async with ctx.typing():
            try:
                msg = await ctx.channel.fetch_message(mid)
                await msg.add_reaction(emoji)
            except Exception:
                log.exception("Failed to pre-add reaction")

            stored_emoji = emoji.replace("\uFE0F", "")

            query = """
                INSERT INTO reaction_roles
                (message_id, emoji, role_id, guild_id)
                VALUES
                ($1, $2, $3, $4)
                ON CONFLICT (message_id, emoji) DO UPDATE SET role_id = $3;
                """

            await self.bot.db_pool.execute(
                query, mid, stored_emoji, role.id, ctx.guild.id
            )

        self.cache[(mid, stored_emoji)] = role.id

        embed = discord.Embed(
            title="Reaction role added",
            colour=random_colour(),
        )
        embed.add_field(name="Message", value=f"`{mid}`")
        embed.add_field(name="Emoji", value=emoji)
        embed.add_field(name="Role", value=f"<@&{role.id}>")
        await ctx.send(embed=embed)

    @reactionrole.command(name="remove")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole_remove(self, ctx, message_id: str, emoji: str):
        """Remove an emoji-to-role mapping from a message."""

        try:
            mid = int(message_id)
        except ValueError:
            await ctx.send("That doesn't look like a valid message ID.")
            return

        stored_emoji = emoji.replace("\uFE0F", "")

        query = """
            DELETE FROM reaction_roles
            WHERE message_id = $1 AND emoji = $2;
            """

        await self.bot.db_pool.execute(query, mid, stored_emoji)

        self.cache.pop((mid, stored_emoji), None)

        embed = discord.Embed(
            title="Reaction role removed",
            colour=random_colour(),
        )
        embed.add_field(name="Message", value=f"`{mid}`")
        embed.add_field(name="Emoji", value=emoji)
        await ctx.send(embed=embed)

    @reactionrole.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole_list(self, ctx):
        """List all reaction-role mappings for this guild."""

        query = """
            SELECT message_id, emoji, role_id FROM reaction_roles
            WHERE guild_id = $1;
            """

        rows = await self.bot.db_pool.fetch(query, ctx.guild.id)

        if not rows:
            embed = discord.Embed(
                title="Reaction roles",
                description="No reaction roles have been set up for this guild.",
                colour=random_colour(),
            )
            await ctx.send(embed=embed)
            return

        lines = [
            f"Message `{row['message_id']}` - {row['emoji']} -> <@&{row['role_id']}>"
            for row in rows
        ]
        await Paginator(
            paginate_lines(lines, title="Reaction roles"), author_id=ctx.author.id
        ).start(ctx)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return

        key = (payload.message_id, str(payload.emoji).replace("\uFE0F", ""))
        rid = self.cache.get(key)

        if not rid:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(rid)

        if role:
            try:
                await payload.member.add_roles(role, reason="Reaction role")
            except Exception:
                log.exception("Failed to add role")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if payload.guild_id is None:
            return

        key = (payload.message_id, str(payload.emoji).replace("\uFE0F", ""))
        rid = self.cache.get(key)

        if not rid:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            member = None
        role = guild.get_role(rid)

        if member and role:
            try:
                await member.remove_roles(role, reason="Reaction role removed")
            except Exception:
                log.exception("Failed to remove role")


async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))

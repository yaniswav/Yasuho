import discord
from discord.ext import commands

import asyncio
import asyncpg
import random
import traceback


class Twitch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        try:
            role = discord.utils.get(before.guild.roles, name="Live 🔴")

            if not role:
                pass

            if any(
                isinstance(activity, discord.Streaming) for activity in after.activities
            ) and not any(
                isinstance(activity, discord.Streaming)
                for activity in before.activities
            ):
                for activity in after.activities:
                    if isinstance(activity, discord.Streaming):
                        query = (
                            """ SELECT user_id FROM twitch_alert WHERE guild_id = $1"""
                        )
                        uid = await self.bot.pool.fetchval(query, after.guild.id)

                        if not (uid):
                            return

                        try:
                            try:
                                query = """ SELECT channel_id FROM twitch_alert WHERE user_id = $1 and guild_id = $2 """
                                fetch_msgid = await self.bot.pool.fetchval(
                                    query, after.id, after.guild.id
                                )
                                ch_ = discord.utils.get(
                                    before.guild.channels, id=fetch_msgid
                                )

                            except:
                                return

                            query = """ SELECT message FROM twitch_alert WHERE user_id = $1 and guild_id = $2 """
                            f = await self.bot.pool.fetchval(
                                query, after.id, after.guild.id
                            )

                            if "[url]" in f:
                                f = f.replace("[url]", f"{activity.url}")

                            if "[game]" in f:
                                f = f.replace("[game]", f"{activity.game}")

                            if "[game]" in f and "[url]" in f:
                                f = f.replace("[game]", f"{activity.game}")
                                f = f.replace("[url]", f"{activity.url}")

                            await ch_.send(f"{f}")

                        except:
                            return

                        await after.add_roles(role, reason="Live Streamer Update")

            elif not any(
                isinstance(activity, discord.Streaming) for activity in after.activities
            ) and any(
                isinstance(activity, discord.Streaming)
                for activity in before.activities
            ):
                await after.remove_roles(role, reason="Live Streamer Update")

        except:
            pass

    @commands.hybrid_group(aliases=["stream"])
    @commands.has_permissions(manage_messages=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def twitch(self, ctx: commands.Context):
        """Setup a Stream role when a Discord Member of your server is streaming"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @twitch.command(aliases=["add-member"])
    @commands.guild_only()
    async def add(
        self,
        ctx: commands.Context,
        member: discord.Member = None,
        channel: discord.TextChannel = None,
        *,
        message: str,
    ):
        """Informations : [url] Your Twitch url | [game] Your Twitch game"""

        await ctx.send(
            "⚠️ Info | Put [url] to set your Twitch URL in the string and [game] to set your game ;)",
            delete_after=3,
        )

        member = member or ctx.author
        query = """ 
                INSERT INTO twitch_alert(guild_id, user_id, channel_id, message) VALUES($1,$2,$3,$4) ON CONFLICT (guild_id, user_id, channel_id) DO UPDATE SET channel_id = $5, message = $6;  
                """

        try:
            await self.bot.pool.execute(
                query, ctx.guild.id, member.id, channel.id, message, channel.id, message
            )
            await ctx.send(
                f"Added {member.mention} Twitch alerts into the <#{channel.id}> channel!"
            )
            try:
                ch_ = discord.utils.get(member.guild.channels, id=channel.id)
                await ch_.send(f"Twitch alerts set for {member.name}", delete_after=30)
            except:
                pass

        except:
            return

    @twitch.command(aliases=["remove-member", "del"])
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, member: discord.Member = None):
        """Remove a member from the Twitch alert DB."""
        member = member or ctx.author
        query = """ 
                DELETE FROM twitch_alert WHERE guild_id = $1 AND user_id = $2;  
                """

        await self.bot.pool.execute(query, ctx.guild.id, member.id)
        await ctx.send(f"Removed {member.mention} Twitch alerts.")

    @twitch.command(aliases=["info-member"])
    @commands.guild_only()
    async def info(self, ctx: commands.Context, member: discord.Member = None):
        """Gives Twitch info."""

        member = member or ctx.author
        query = """ 
                SELECT channel_id FROM twitch_alert WHERE guild_id = $1 AND user_id = $2;  
                """

        channel = await self.bot.pool.fetchval(query, ctx.guild.id, member.id)

        if not (channel):
            embed = discord.Embed(
                title="Twitch alerts", colour=random.randint(0x000000, 0xFFFFFF)
            )
            embed.add_field(
                name="The current Twitch alert channel:",
                value=f"There is no channel for this user",
            )
            await ctx.send(embed=embed)
            return

        embed = discord.Embed(
            title="Twitch alerts", colour=random.randint(0x000000, 0xFFFFFF)
        )
        embed.add_field(name="The current Twitch alert channel:", value=f"<#{channel}>")
        await ctx.send(embed=embed)

    @twitch.command(aliases=["setup-role"])
    @commands.guild_only()
    async def setup(self, ctx: commands.Context):
        """Setup a Live Role"""
        role: discord.Role = discord.utils.get(
            ctx.guild.roles, name="Live 🔴"
        )  # Invisible Emote

        if role:
            return await ctx.send("Your guild already has a Live streamer role.")

        try:
            await ctx.guild.create_role(
                name="Live 🔴", hoist=True, reason="Twitch Live Role"
            )
        except discord.HTTPException as e:
            return await ctx.send(
                f"An error occurred while creating the Streamer Role.\n\n{e}"
            )

        await ctx.send(
            "Live streamer role was successfully created. You may now move it to your preferred position."
        )

    @twitch.command(aliases=["delete-role", "del-role"])
    @commands.guild_only()
    async def removerole(self, ctx: commands.Context):
        """Remove the Live Role"""

        role: discord.Role = discord.utils.get(
            ctx.guild.roles, name="Live 🔴"
        )  # Invisible Emote

        if not role:
            return await ctx.send(
                "Your guild does not have a Live streamer role setup."
            )

        try:
            await role.delete()
        except discord.HTTPException as e:
            return await ctx.send(
                f"An error occurred while deleting the Streamer Role.\n\n{e}"
            )

        await ctx.send("Live streamer role was successfully removed.")


async def setup(bot):
    await bot.add_cog(Twitch(bot))

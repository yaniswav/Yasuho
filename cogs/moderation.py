import asyncio
import random
import discord
from discord.ext import commands
from discord.ext.commands import MemberConverter
from discord.ext.commands.cooldowns import BucketType
import os
import time
import typing
import logging
import re
from re import *
from inspect import cleandoc
import json
import enum
import config
import argparse
import shlex
import datetime
from collections import deque
from random import randint as rint


class Moderation(commands.Cog):
    """Ultracool moderator commands"""

    def __init__(self, bot):
        self.bot = bot
        self.units = {
            "minute": 60,
            "hour": 3600,
            "day": 86400,
            "week": 604800,
            "month": 2592000,
        }

    @commands.command(aliases=["newmembers"])
    @commands.guild_only()
    async def newusers(self, ctx, *, count=5):
        """Tells you the newest members of the server.
        This is useful to check if any suspicious members have
        joined.
        The count parameter can only be up to 25.
        """
        try:
            count = max(min(count, 25), 5)

            if not ctx.guild.chunked:
                await self.bot.request_offline_members(ctx.guild)

            members = sorted(
                ctx.guild.members, key=lambda m: m.joined_at, reverse=True
            )[:count]

            e = discord.Embed(
                title="New Members", colour=random.randint(0x000000, 0xFFFFFF)
            )

            for member in members:
                body = f"joined {time.human_timedelta(member.joined_at)}, created {time.human_timedelta(member.created_at)}"
                e.add_field(
                    name=f"{member} (ID: {member.id})", value=body, inline=False
                )

            await ctx.send(embed=e)

        except:
            pass

    @commands.hybrid_command(name="kick", aliases=["k"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(kick_members=True)
    async def _kick(self, ctx, target: discord.User, *, reason: str = None):
        """Kicks an annoying user. Requires kick members permission. Also bot must have this permission."""

        if reason is None:
            reason = "No reason specified"

        embedkick = discord.Embed(
            color=random.randint(0x000000, 0xFFFFFF),
            timestamp=ctx.message.created_at,
            title=f"Kick | {ctx.author.name} has kicked {target.name}",
        )
        embedkick.set_thumbnail(url=target.avatar_url)
        embedkick.add_field(
            name=f"**🔴 Kick Info**",
            value=f"Moderator: **{ctx.author.mention}**\nReason: **{reason if len(reason) <= 100 else f'{reason[:100]}...'}**\nTime: **{ctx.message.created_at}**",
        )
        embedkick.set_footer(text=ctx.guild, icon_url=ctx.guild.icon_url)

        try:
            await ctx.guild.kick(
                target,
                reason=f"{ctx.author}: {reason if len(reason) <= 100 else f'{reason[:100]}...'}",
            )
            await ctx.send(embed=embedkick)

        except:
            await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )

    @commands.command(name="voicekick", aliases=["vkick", "voicek"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(kick_members=True)
    async def _voicekick(self, ctx, user: discord.Member, *, reason: str = None):
        """Kicks an annoying user. Requires kick members permission. Also bot must have this permission."""

        if reason is None:
            reason = "No reason specified"

        embedkick = discord.Embed(
            color=random.randint(0x000000, 0xFFFFFF),
            timestamp=ctx.message.created_at,
            title=f"Kick | {ctx.author.name} has kicked {user.name}",
        )
        embedkick.set_thumbnail(url=user.avatar_url)
        embedkick.add_field(
            name=f"**🔴 Voice Kick Info**",
            value=f"Moderator: **{ctx.author.mention}**\nReason: **{reason if len(reason) <= 100 else f'{reason[:100]}...'}**\nTime: **{ctx.message.created_at}**",
        )
        embedkick.set_footer(text=ctx.guild, icon_url=ctx.guild.icon_url)

        try:
            await user.move_to(
                None,
                reason=f"{ctx.author}: {reason if len(reason) <= 100 else f'{reason[:100]}...'}",
            )
            await ctx.send(embed=embedkick)
        except:
            await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )

    @commands.command(name="move")
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(kick_members=True)
    async def _move(self, ctx, user: discord.Member, room: str):
        """Moves an annoying user to a channel."""

        channel = discord.utils.get(ctx.guild.voice_channels, name=room)
        try:
            await user.move_to(channel, reason=None)
            await ctx.send(f"{user.name} has been moved to {channel}")
        except Exception as error:
            await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )
            print(f"[ERROR] {error}")

    @commands.hybrid_command(name="ban", aliases=["b"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(ban_members=True)
    async def _ban(self, ctx, target: discord.User, *, reason: str = None):
        """Bans an annoying user. Requires ban members permission. Also bot must have this permission."""

        if reason is None:
            reason = "No reason specified"

        embedban = discord.Embed(
            color=random.randint(0x000000, 0xFFFFFF),
            timestamp=ctx.message.created_at,
            title=f"Ban | {ctx.author.name} has banned {target.name}",
        )
        embedban.set_thumbnail(url=target.avatar_url)
        embedban.add_field(
            name=f"**🔴 Ban Info**",
            value=f"Moderator: **{ctx.author.mention}**\nReason: **{reason if len(reason) <= 100 else f'{reason[:100]}...'}**\nTime: **{ctx.message.created_at}**",
        )
        embedban.set_footer(text=ctx.guild, icon_url=ctx.guild.icon_url)

        try:
            await ctx.guild.ban(
                target,
                reason=f"{ctx.author}: {reason if len(reason) <= 100 else f'{reason[:100]}...'}",
            )
            await ctx.send(embed=embedban)
        except:
            await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )

    @commands.hybrid_command(name="unban", aliases=["ub"])
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(ban_members=True)
    async def _unban(self, ctx, target: discord.User, *, reason: str = None):
        """Bans an annoying user. Requires ban members permission. Also bot must have this permission."""

        if reason is None:
            reason = "No reason specified"

        embedunban = discord.Embed(
            color=random.randint(0x000000, 0xFFFFFF),
            timestamp=ctx.message.created_at,
            title=f"Unban | {ctx.author.name} ❌🔨 {target.name}",
        )
        embedunban.set_thumbnail(url=target.avatar_url)
        embedunban.add_field(
            name=f"**🔴 Unban Info**",
            value=f"Moderator: **{ctx.author.mention}**\nReason: **{reason if len(reason) <= 100 else f'{reason[:100]}...'}**\nTime: **{ctx.message.created_at}**",
        )
        embedunban.set_footer(text=ctx.guild, icon_url=ctx.guild.icon_url)

        try:
            await ctx.guild.unban(
                target,
                reason=f"{ctx.author}: {reason if len(reason) <= 100 else f'{reason[:100]}...'}",
            )
            await ctx.send(embed=embedunban)
        except:
            await ctx.send(
                "**:x: Sorry, I am missing permissions to do this!**", delete_after=10
            )

    @commands.hybrid_command(
        name="purge", aliases=["pg", "massclean", "massdelete", "prune"]
    )
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    @commands.has_permissions(manage_messages=True)
    async def _purge(self, ctx, count: int):
        """Purges messages. Requires manage messages permission"""

        if ctx.interaction:
            await ctx.interaction.response.defer()

        if count > 999 or count < 1:
            return await ctx.send(
                ":warning: | **Count can't be lesser than 0 and greater than 999**",
                delete_after=3,
            )

        else:
            try:
                await ctx.channel.purge(
                    limit=count + 1, before=datetime.datetime.utcnow()
                )
            except:
                return await ctx.send(
                    "**:x: Sorry, I am missing permissions to do this**", delete_after=5
                )

        if ctx.interaction:
            return await ctx.interaction.response.send_message(
                f"{config.e_verif} **Deleted succefully !**", epheremal=True
            )

        return await ctx.send(
            f"{config.e_verif} **Deleted succefully !**", delete_after=3
        )

    @commands.command(description="Clears X messages.")
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    @commands.has_permissions(manage_messages=True)
    async def clean(self, ctx, num: int, target: discord.Member):
        """Clears X messages of a member"""

        if num > 500 or num < 0:
            return await ctx.send("Invalid amount. Maximum is 500.")

        def msgcheck(amsg):
            if target:
                return amsg.author.id == target.id
            return True

        deleted = await ctx.channel.purge(limit=num, check=msgcheck)
        await ctx.send(
            f"{config.e_verif} Deleted **{len(deleted)}/{num}** possible messages for you.",
            delete_after=3,
        )

    async def create_mute_role(self, ctx):
        perms = discord.Permissions(
            send_messages=False,
            read_messages=True,
            add_reactions=False,
            send_tts_messages=False,
            read_message_history=True,
            speak=False,
        )
        role = "Muted"
        await ctx.guild.create_role(name=role, permissions=perms)
        await ctx.send(f"{ctx.guild.id}, {role}")

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def mute(self, ctx, user: discord.Member, *, reason: str = None):
        """Mutes the specified member."""

        if reason is None:
            reason = "No reason specified"

        con = self.bot.db_pool

        query = """
                
        SELECT role_id FROM muterole 
        WHERE guild_id = $1;
                    
        """

        await con.execute(query, ctx.guild.id)
        role = await con.fetchval(query, ctx.guild.id)

        try:
            if role is None:
                try:
                    await ctx.send("Mute role is not defined", delete_after=3)
                    await ctx.send("Creating role...", delete_after=1)
                    perms = discord.Permissions(
                        send_messages=False,
                        add_reactions=False,
                        send_tts_messages=False,
                        speak=False,
                    )
                    role = "Muted"
                    mrole = await ctx.guild.create_role(name=role, permissions=perms)
                    await ctx.send(content="Mute role created!", delete_after=5)
                    query = """INSERT INTO muterole (guild_id, role_id) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET role_id = $3;"""
                    await self.bot.db_pool.execute(query, ctx.guild.id, mrole.id, mrole.id)

                    for channel in ctx.guild.text_channels:
                        await channel.set_permissions(
                            mrole,
                            overwrite=discord.PermissionOverwrite(
                                send_messages=False,
                                add_reactions=False,
                                send_tts_messages=False,
                            ),
                        )
                    for channel in ctx.guild.voice_channels:
                        await channel.set_permissions(
                            mrole, overwrite=discord.PermissionOverwrite(speak=False)
                        )
                    for channel in ctx.guild.categories:
                        await channel.set_permissions(
                            mrole,
                            overwrite=discord.PermissionOverwrite(
                                send_messages=False,
                                add_reactions=False,
                                send_tts_messages=False,
                                speak=False,
                            ),
                        )

                    await user.add_roles(
                        mrole, reason=f"""Muted By: {ctx.author} for: {reason} """
                    )
                    embed = discord.Embed(
                        title="Done!",
                        description=f":red_circle: {user} has been muted.",
                        colour=random.randint(0x000000, 0xFFFFFF),
                    )

                    query = """INSERT INTO mutedmembers (mguild_id, member_id) VALUES ($1, $2)"""
                    await self.bot.db_pool.execute(query, ctx.guild.id, user.id)

                    return await ctx.send(embed=embed)

                except:
                    pass

            mutedrole = discord.utils.get(ctx.guild.roles, id=role)
            await user.add_roles(
                mutedrole, reason=f"""Muted By: {ctx.author} for: {reason} """
            )
            embed = discord.Embed(
                title="Done!",
                description=f":red_circle: {user} has been muted.",
                colour=random.randint(0x000000, 0xFFFFFF),
            )
            query = (
                """INSERT INTO mutedmembers (mguild_id, member_id) VALUES ($1, $2)"""
            )
            await self.bot.db_pool.execute(query, ctx.guild.id, user.id)
            await ctx.send(embed=embed)

            for channel in ctx.guild.text_channels:
                await channel.set_permissions(
                    mutedrole,
                    overwrite=discord.PermissionOverwrite(
                        send_messages=False,
                        add_reactions=False,
                        send_tts_messages=False,
                    ),
                )
            for channel in ctx.guild.voice_channels:
                await channel.set_permissions(
                    mutedrole, overwrite=discord.PermissionOverwrite(speak=False)
                )
            for channel in ctx.guild.categories:
                await channel.set_permissions(
                    mutedrole,
                    overwrite=discord.PermissionOverwrite(
                        send_messages=False,
                        add_reactions=False,
                        send_tts_messages=False,
                        speak=False,
                    ),
                )

        except:
            embed = discord.Embed(
                title="Already Muted",
                colour=random.randint(0x000000, 0xFFFFFF),
                description=f":red_circle: {user} is already muted!",
                timestamp=datetime.datetime.utcnow(),
            )
            await ctx.send(embed=embed)
            return

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(self, ctx, user: discord.Member):
        """Un-mutes the specified member."""

        con = self.bot.db_pool

        query = """
                
        SELECT role_id FROM muterole 
        WHERE guild_id = $1;
                    
        """

        role = await con.fetchval(query, ctx.guild.id)

        try:
            mutedrole = discord.utils.get(ctx.guild.roles, id=role)
            await user.remove_roles(mutedrole, reason=f"""Unmuted by {ctx.author}""")
            embed = discord.Embed(
                title="Done!",
                description=f":red_circle: {user.mention} has been un-muted.",
                colour=random.randint(0x000000, 0xFFFFFF),
            )
            query = (
                """DELETE FROM mutedmembers WHERE mguild_id = $1 AND member_id = $2;"""
            )
            await self.bot.db_pool.execute(query, ctx.guild.id, user.id)
            await ctx.send(embed=embed)

        except Exception as e:
            await ctx.send(e, delete_after=3)
            embed = discord.Embed(
                title="Not Muted",
                colour=random.randint(0x000000, 0xFFFFFF),
                description=f""":red_circle: {user} was never muted!""",
                timestamp=datetime.datetime.utcnow(),
            )
            await ctx.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def addrole(self, ctx, member, role: discord.Role):
        """Set a role to a specified member."""

        # rank = discord.utils.get(ctx.guild.roles, role=role)

        if member == "-all":
            for m in ctx.message.guild.members:
                if not role in m.roles:
                    await m.add_roles(role)

            return await ctx.send(
                f"Added to all guilds members **`{role.name}`** role."
            )

        converter = MemberConverter()
        m = await converter.convert(ctx, member)
        await m.add_roles(role)
        return await ctx.send(
            f"{config.e_verif} **`{role.name}`** role has been added to **{m.name}**"
        )

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def removerole(self, ctx, member, role: discord.Role):
        """Remove a role to a specified member."""

        if member == "-all":
            for m in ctx.message.guild.members:
                if not role in m.roles:
                    await m.remove_roles(role)

            return await ctx.send(
                f"Removed to all guilds members **`{role.name}`** role."
            )

        converter = MemberConverter()
        m = await converter.convert(ctx, member)
        await m.remove_roles(role)
        return await ctx.send(
            f"{config.e_verif} **`{role.name}`** role has been removed to **{m.name}**"
        )

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def moverole(self, ctx, role: discord.Role, pos: int):
        try:
            await role.edit(position=pos)
            await ctx.send(f"{role} moved.")
        except discord.Forbidden:
            await ctx.send("You do not have permission to do that")
        except discord.HTTPException:
            await ctx.send("Failed to move role")
        except discord.InvalidArgument:
            await ctx.send("Invalid argument")

    @commands.command()
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    async def warninfo(self, ctx, member: discord.Member = None):
        if member is None:
            return await ctx.send_help(ctx.command)

        query = """
                
        SELECT warns_count FROM warns 
        WHERE guild_id = $1 AND user_id = $2;
                    
        """

        fetch = await self.bot.db_pool.fetchval(query, ctx.guild.id, member.id)

        if not fetch:
            return await ctx.send(f"{member.mention} has no warns.")

        await ctx.send(f"{member.mention} has {fetch} warn(s)")

    @commands.hybrid_command()
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    async def warn(self, ctx, member: discord.Member = None):
        """Warn a member of the guild (auto-kick at 3 warns)"""

        if member is None:
            return await ctx.send_help(ctx.command)

        query = """
                
        SELECT warns_count FROM warns 
        WHERE guild_id = $1 AND user_id = $2;
                    
        """

        fetch = await self.bot.db_pool.fetchval(query, ctx.guild.id, member.id)
        await ctx.send(fetch)

        if not fetch:
            query = """ INSERT INTO warns (guild_id, user_id, warns_count) VALUES ($1, $2, 1) ON CONFLICT (guild_id, user_id) DO UPDATE SET  = 1;"""
            await self.bot.db_pool.execute(query, ctx.guild.id, member.id)
            return await ctx.send(f"{member.mention} has been warned! [1 warn]")

        elif fetch + 1 >= 3:
            query = """ INSERT INTO warns
                        (guild_id, user_id, warns_count)
                        VALUES
                        ($1, $2, 0) ON CONFLICT (guild_id, user_id) DO UPDATE SET warns_count = 0;
                        """
            await self.bot.db_pool.execute(query, ctx.guild.id, member.id)
            try:
                await member.kick()
                await member.send("You have been kick from the server!")
                return await ctx.send(
                    f"{member.mention} has been kicked from the server!"
                )
            except:
                return await ctx.send(
                    f"{member.mention} has 3 warns but I don't have permissions to kick him from the guild."
                )

        else:
            await ctx.send(type(fetch), fetch)
            query = """ INSERT INTO warns (guild_id, user_id, warns_count) VALUES ($1, $2, $3) ON CONFLICT (guild_id, user_id) DO UPDATE SET warns_count = warns_count + $4;"""
            await self.bot.db_pool.execute(
                query, ctx.guild.id, member.id, fetch + 1, fetch + 1
            )
            await ctx.send(f"{member.mention} has been warned! [{fetch + 1} warns]")

    @commands.hybrid_command(aliases=["rmwarn", "removewarn"])
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    async def delwarn(self, ctx, member: discord.Member = None, num: int = 1):
        """Remove a warn from a member of the guild."""

        if member is None:
            return await ctx.send_help(ctx.command)

        query = (
            """SELECT warns_count FROM warns WHERE guild_id = $1 AND user_id = $2;"""
        )
        fetch = await self.bot.db_pool.fetchval(query, ctx.guild.id, member.id)

        if not fetch:
            return await ctx.send(f"{member.mention} has no warns!")

        if fetch - num < 0:
            query = f""" UPDATE warns SET warns_count = 0 WHERE guild_id = $1 AND user_id = $2;"""
            await self.bot.db_pool.execute(query, ctx.guild.id, member.id)
            return await ctx.send(f"Removed all warns for {member.mention}.")

        query = f""" UPDATE warns SET warns_count = warns_count - {int(num)} WHERE guild_id = $1 AND user_id = $2;"""
        await self.bot.db_pool.execute(query, ctx.guild.id, member.id)
        await ctx.send(
            f"Removed {num} warn(s) for {member.mention}. [{fetch - 1} warns]"
        )


async def setup(bot):
    await bot.add_cog(Moderation(bot))

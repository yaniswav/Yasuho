import asyncio
import logging
from collections import defaultdict

import discord
from discord.ext import commands

log = logging.getLogger(__name__)


class TemporaryRooms(commands.Cog):
    """Create and clean up temporary voice rooms from auto-room channels."""

    def __init__(self, bot):
        self.bot = bot
        self.active_temp_rooms = {}  # Stores the active temp rooms
        self._locks = defaultdict(asyncio.Lock)  # Per-guild creation locks
        self._cleanup_tasks = set()  # Outstanding fire-and-forget cleanup tasks
        # {guild_id: set(channel_ids)} of auto-room hub channels.
        # Negative-cached: a guild with no hubs maps to an empty set, and a
        # guild missing from the dict is treated the same (no hubs) so
        # unconfigured guilds cost zero queries on every voice event.
        self._auto_rooms = {}

    async def cog_load(self):
        """Load every auto-room hub channel into memory once at startup."""
        self._auto_rooms = {}
        rows = await self.bot.db_pool.fetch(
            "SELECT guild_id, channel_id FROM auto_room"
        )
        for row in rows:
            self._auto_rooms.setdefault(int(row["guild_id"]), set()).add(
                int(row["channel_id"])
            )

    async def cog_unload(self):
        """Cancel any outstanding per-room cleanup tasks on unload."""
        for task in list(self._cleanup_tasks):
            task.cancel()

    async def remove_empty_room(self, channel_id, guild_id, room_identifier):
        """Deletes the temporary room if it is empty and cleans up the dictionary."""
        await self.bot.wait_until_ready()

        while True:
            await asyncio.sleep(15)
            try:
                temp_channel = self.bot.get_channel(channel_id)
                if temp_channel is None:
                    # Channel already gone; just clean up the dictionary.
                    self.active_temp_rooms.pop((guild_id, room_identifier), None)
                    return
                if len(temp_channel.members) == 0:
                    try:
                        await temp_channel.delete()
                    except discord.HTTPException:
                        pass
                    # Clean up the dictionary
                    self.active_temp_rooms.pop((guild_id, room_identifier), None)
                    return
            except Exception:
                log.exception("Failed to clean up temporary room %s", channel_id)
                return

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not after.channel:
            return

        # Resolve the guild's hub set from memory; bail out for guilds with no
        # hubs or when the joined channel isn't a hub, costing zero queries.
        hubs = self._auto_rooms.get(member.guild.id)
        if not hubs or after.channel.id not in hubs:
            return

        try:
            auto_room_channel = after.channel
            category = auto_room_channel.category
            if category is None:
                return

            room_identifier = f"{category.id}-{auto_room_channel.id}"
            channel_name = f"{auto_room_channel.name} | {member.name}"

            # Serialize creation per guild so concurrent joins do not
            # create duplicate temp rooms for the same auto-room.
            async with self._locks[member.guild.id]:
                existing_temp_channel_id = self.active_temp_rooms.get(
                    (member.guild.id, room_identifier)
                )
                if existing_temp_channel_id:
                    existing_channel = self.bot.get_channel(existing_temp_channel_id)
                    if existing_channel:
                        await member.move_to(existing_channel)
                        return

                new_temp_channel = await member.guild.create_voice_channel(
                    channel_name, category=category
                )
                self.active_temp_rooms[
                    (member.guild.id, room_identifier)
                ] = new_temp_channel.id
                await member.move_to(new_temp_channel)
                task = asyncio.create_task(
                    self.remove_empty_room(
                        new_temp_channel.id, member.guild.id, room_identifier
                    )
                )
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._cleanup_tasks.discard)

        except Exception:
            log.exception("Failed to handle auto-room creation")

    @commands.hybrid_group(aliases=["auto_room", "autorooms", "auto_rooms", "room", "rooms"])
    @commands.is_owner()
    async def autoroom(self, ctx):
        """Manage the auto-room system for temporary voice channels."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @autoroom.command()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def setup(self, ctx):
        """Setup an auto-room system"""

        try:
            async with ctx.typing():
                existing_rooms = await self.bot.db_pool.fetch(
                    "SELECT channel_id FROM auto_room WHERE guild_id = $1", ctx.guild.id
                )

                if len(existing_rooms) >= 3:
                    return await ctx.send(
                        "You have reached the maximum number of Auto-Rooms for this server."
                    )

                try:
                    cat = await ctx.guild.create_category_channel("Temp-Rooms")
                    chan = await ctx.guild.create_voice_channel(
                        "Auto-Temp Room", category=cat
                    )
                except discord.HTTPException:
                    return await ctx.send(
                        "Something went wrong while creating your Auto-Room."
                    )

                await self.bot.db_pool.execute(
                    "INSERT INTO auto_room(guild_id, channel_id) VALUES($1, $2);",
                    ctx.guild.id,
                    chan.id,
                )
                self._auto_rooms.setdefault(ctx.guild.id, set()).add(chan.id)

            await ctx.send(
                "Successfully created your Auto-Room. You can rename category and channel to whatever you want."
            )

        except Exception:
            log.exception("Failed to set up auto-room")
            await ctx.send("Something went wrong while creating your Auto-Room.")

    @autoroom.command(aliases=["delete", "del"])
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def remove(self, ctx, *, category_name: str):
        """Remove the auto-room system based on category name"""

        matching_categories = [
            c for c in ctx.guild.categories if c.name == category_name
        ]

        if not matching_categories:
            return await ctx.send("No category found with that name.")

        if len(matching_categories) > 1:
            return await ctx.send(
                "Multiple categories found with that name. Please be more specific."
            )

        category = matching_categories[0]

        async with ctx.typing():
            removed_any = False
            for channel in category.channels:
                if isinstance(channel, discord.VoiceChannel):
                    try:
                        fetch = await self.bot.db_pool.fetchval(
                            "SELECT channel_id FROM auto_room WHERE channel_id = $1;",
                            channel.id,
                        )
                        if fetch:
                            await channel.delete()
                            await self.bot.db_pool.execute(
                                "DELETE FROM auto_room WHERE channel_id = $1;", channel.id
                            )
                            # Write through to the cache, leaving an empty set
                            # so the guild stays negatively cached.
                            hubs = self._auto_rooms.get(ctx.guild.id)
                            if hubs is not None:
                                hubs.discard(channel.id)
                            removed_any = True
                    except discord.HTTPException:
                        pass

            if not removed_any:
                return await ctx.send("That category is not an Auto-Room.")

            try:
                await category.delete()
            except discord.HTTPException:
                return await ctx.send(
                    "Something went wrong while deleting the category."
                )

        await ctx.send("Successfully removed the Auto-Room category and its channels.")

    @autoroom.command(aliases=["list"])
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    @commands.guild_only()
    async def list_autorooms(self, ctx):
        """List all autorooms in the server"""
        autorooms = await self.bot.db_pool.fetch(
            "SELECT channel_id FROM auto_room WHERE guild_id = $1;", ctx.guild.id
        )

        if not autorooms:
            return await ctx.send("There are no autorooms set up in this server.")

        embed = discord.Embed(title="Auto Rooms in Server", color=discord.Color.blue())

        for room in autorooms:
            channel = self.bot.get_channel(room["channel_id"])
            if channel:
                embed.add_field(
                    name=f"Channel ID: {channel.id}",
                    value=f"Name: {channel.name}",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Channel ID: Unknown",
                    value="This channel might have been deleted.",
                    inline=False,
                )

        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(TemporaryRooms(bot))

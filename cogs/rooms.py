import asyncio
import discord
import asyncpg
from discord.ext import commands
import traceback

class TemporaryRooms(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_temp_rooms = {}  # Stores the active temp rooms

    async def remove_empty_room(self, channel_id, guild_id, room_identifier):
        """Deletes the temporary salon if it is empty and cleans up the dictionary."""
        await self.bot.wait_until_ready()

        while True:
            await asyncio.sleep(15)
            temp_channel = self.bot.get_channel(channel_id)
            if not temp_channel or len(temp_channel.members) == 0:
                try:
                    await temp_channel.delete()
                    # Clean up the dictionary
                    if (guild_id, room_identifier) in self.active_temp_rooms:
                        del self.active_temp_rooms[(guild_id, room_identifier)]
                except discord.HTTPException:
                    pass
                finally:
                    return

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not after.channel:
            return

        try:
            auto_rooms = await self.bot.db_pool.fetch(
                "SELECT channel_id FROM auto_room WHERE guild_id = $1", member.guild.id
            )
            if not auto_rooms:
                return

            for auto_room in auto_rooms:
                auto_room_channel = self.bot.get_channel(int(auto_room["channel_id"]))
                if not auto_room_channel or after.channel.id != auto_room_channel.id:
                    continue

                room_identifier = (
                    f"{auto_room_channel.category.id}-{auto_room_channel.id}"
                )
                channel_name = f"{auto_room_channel.name} | {member.name}"

                existing_temp_channel_id = self.active_temp_rooms.get(
                    (member.guild.id, room_identifier)
                )
                if existing_temp_channel_id:
                    existing_channel = self.bot.get_channel(existing_temp_channel_id)
                    if existing_channel:
                        await member.move_to(existing_channel)
                        continue

                new_temp_channel = await member.guild.create_voice_channel(
                    channel_name, category=auto_room_channel.category
                )
                self.active_temp_rooms[
                    (member.guild.id, room_identifier)
                ] = new_temp_channel.id
                await member.move_to(new_temp_channel)
                asyncio.create_task(
                    self.remove_empty_room(
                        new_temp_channel.id, member.guild.id, room_identifier
                    )
                )

        except:
            pass

    @commands.group(aliases=["auto_room", "autorooms", "auto_rooms", "room", "rooms"])
    @commands.is_owner()
    async def autoroom(self, ctx):

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @autoroom.command()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def setup(self, ctx):
        """Setup an auto-room system"""

        try:
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
                    f"Something went wrong while creating your Auto-Room."
                )

            await self.bot.db_pool.execute(
                "INSERT INTO auto_room(guild_id, channel_id) VALUES($1, $2);",
                ctx.guild.id,
                chan.id,
            )
            await ctx.send(
                "Successfully created your Auto-Room. You can rename category and channel to whatever you want."
            )

        except Exception as e:
            print(e)

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
                except discord.HTTPException:
                    pass

        try:
            await category.delete()
        except discord.HTTPException:
            return await ctx.send("Something went wrong while deleting the category.")

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

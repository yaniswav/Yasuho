import asyncio
import copy
import datetime
import math
import random
import typing
import async_timeout
import discord
import wavelink

from discord.ext import commands
from typing import cast

import config


class NoChannelProvided(commands.CommandError):
    """Error raised when no suitable voice channel was supplied."""

    pass


class IncorrectChannelError(commands.CommandError):
    """Error raised when commands are issued outside of the players session channel."""

    pass


class Player(wavelink.Player):
    def __init__(self, ctx: commands.Context, *args: typing.Any, **kwargs: typing.Any):
        super().__init__(*args, **kwargs)

        self.ctx = ctx
        self.dj: discord.Member = ctx.author

        self.controller = None

        self.waiting = False
        self.updating = False

        self.pause_votes = set()
        self.resume_votes = set()
        self.skip_votes = set()
        self.shuffle_votes = set()
        self.stop_votes = set()

    def clear_votes(self):
        print("[Function] clear_votes")
        self.pause_votes.clear()
        self.resume_votes.clear()
        self.skip_votes.clear()
        self.shuffle_votes.clear()
        self.stop_votes.clear()

    async def do_next(self) -> None:

        print("[Function] do_next")
        if self.current or self.waiting:
            return

        self.clear_votes()

        try:
            self.waiting = True
            with async_timeout.timeout(300):
                track = await self.queue.get()
        except asyncio.TimeoutError:
            # No music has been played for 5 minutes, cleanup and disconnect...
            return await self.disconnect()

        await self.play(track)
        self.waiting = False

    def build_embed(self) -> typing.Optional[discord.Embed]:
        """Method which builds our players controller embed."""
        track = self.current
        if not track:
            return

        channel = self.bot.get_channel(int(self.channel_id))
        qsize = self.queue.qsize()

        embed = discord.Embed(
            title=f"Music Controller | {config.e_voice} **{channel.name}**",
            colour=random.randint(0x000000, 0xFFFFFF),
        )
        embed.description = f"■ **Now Playing:**\n[{track.title}]({track.uri})\n■ **Artist:** `{track.author}`"
        embed.set_footer(text="If you enjoy the bot, don't forget to upvote :)")
        embed.set_thumbnail(url=track.thumb)

        embed.add_field(name="Requested By", value=track.requester.mention, inline=True)
        embed.add_field(
            name="Duration",
            value=str(datetime.timedelta(milliseconds=int(track.length))),
            inline=True,
        )
        embed.add_field(name="Volume", value=f"**`{self.volume}%`**", inline=True)
        embed.add_field(name="DJ", value=self.dj.mention, inline=False)
        embed.add_field(name="Queue Length", value=str(qsize), inline=True)

        return embed


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, player: Player, **payload: dict):
        print(f'[TRACK ENDED] {payload.reason}')
        await payload.player.do_next()

    @commands.Cog.listener()
    async def on_track_exception(self, node: wavelink.Node, payload):
        print(f"[TRACK EXCEPTION] {payload.exception}")
        await payload.player.do_next()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        
        if member.bot:
            return

        player: Player = member.guild.voice_client
        if not player or not player.connected:
            # Exit if the player does not exist or is not connected
            return

        print(f"[COG LISTENER] Player : {player}")

        player.ctx = member

        channel = self.bot.get_channel(player.channel_id)
        if not channel:
            # Exit if the channel does not exist
            return

        # Update the DJ if the current DJ leaves the channel
        if member == player.dj and after.channel != channel:
            player.dj = next((m for m in channel.members if not m.bot), None)

        # Assign a new DJ if the current DJ is not in the channel
        elif after.channel == channel and player.dj not in channel.members:
            player.dj = member

        print(f"[COG LISTENER] Player.dj : {player.dj}")
        # Disconnect the player if it's the only member left in the channel
        if len(channel.members) == 1:
            await asyncio.sleep(15)
            if len(channel.members) == 1:
                await player.disconnect()

    @commands.Cog.listener()
    async def on_wavelink_track_start(
        self, payload: wavelink.TrackStartEventPayload
    ) -> None:
        

        player: Player | None = payload.player
        print(f"[ON_WAVELINK_TRACK_START] Player: {payload.player}")

        if not player:
            print("[ON_WAVELINK_TRACK_START] Player not found")
            # Handle edge cases...
            return

        original: wavelink.Playable | None = payload.original
        track: wavelink.Playable = payload.track
        print(f"[ON_WAVELINK_TRACK_START]: {track.title}")

        embed: discord.Embed = discord.Embed(title="Now Playing")
        embed.description = f"**{track.title}** by `{track.author}`"

        if track.artwork:
            embed.set_image(url=track.artwork)

        if original and original.recommended:
            embed.description += f"\n\n`This track was recommended via {track.source}`"

        if track.album.name:
            embed.add_field(name="Album", value=track.album.name)

        await player.home.send(embed=embed)

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        """Cog wide error handler."""
        if isinstance(error, IncorrectChannelError):
            return

        if isinstance(error, NoChannelProvided):
            return await ctx.send(
                "You must be in a voice channel or provide one to connect to."
            )

    async def cog_check(self, ctx: commands.Context):
        """Cog wide check, which disallows commands in DMs."""
        if not ctx.guild:
            await ctx.send("Music commands are not available in Private Messages.")
            return False

        return True

    def required(self, ctx: commands.Context):
        """Method which returns required votes based on amount of members in a channel."""
        player: Player = Player(ctx.voice_client)

        channel = self.bot.get_channel(int(player.channel_id))
        required = math.ceil((len(channel.members) - 1) / 2.5)

        if ctx.command.name == "stop":
            if len(channel.members) == 3:
                required = 2

        return required

    def is_privileged(self, ctx: commands.Context) -> bool:
        """Check whether the user is an Admin or DJ."""
        player: Player = ctx.voice_client
        if player and player.dj:
            return player.dj == ctx.author or ctx.author.guild_permissions.kick_members
        return False

    @commands.command()
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        """Play a song from a query."""
        player: Player = cast(Player, ctx.voice_client)
        
        if not player:
            try:
                player: Player = await ctx.author.voice.channel.connect(cls=Player(ctx=ctx))  # type: ignore
            except AttributeError:
                print(f"AttributeError : {AttributeError}")
                await ctx.send(
                    "Please join a voice channel first before using this command."
                )
                return
            except discord.ClientException:
                await ctx.send(
                    "I was unable to join this voice channel. Please try again."
                )
                return
            
        print(f"[PLAY] Player : {player}")

        await ctx.send(f"player :{player} ")
        player.autoplay = wavelink.AutoPlayMode.disabled

        if not hasattr(player, "home"):
            player.home = ctx.channel
        elif player.home != ctx.channel:
            await ctx.send(
                f"You can only play songs in {player.home.mention}, as the player has already started there."
            )
            return

        tracks: wavelink.Search = await wavelink.Playable.search(query)
        if not tracks:
            await ctx.send(
                f"{ctx.author.mention} - Could not find any tracks with that query. Please try again."
            )
            return

        if isinstance(tracks, wavelink.Playlist):
            added: int = await player.queue.put_wait(tracks)
            await ctx.send(
                f"Added the playlist **`{tracks.name}`** ({added} songs) to the queue."
            )
        else:
            track: wavelink.Playable = tracks[0]
            await player.queue.put_wait(track)
            await ctx.send(f"Added **`{track}`** to the queue.")

        if not player.playing:
            await player.play(player.queue.get(), volume=30)

    @commands.command()
    async def stop(self, ctx: commands.Context):
        """Stop the player, and disconnect from the channel."""
        player: Player = ctx.voice_client

        if not player.connected:
            return

        if self.is_privileged(ctx):
            await ctx.send("An admin or DJ has stopped the player.", delete_after=10)
            return await player.disconnect()

        required = self.required(ctx)
        player.stop_votes.add(ctx.author)

        if len(player.stop_votes) >= required:
            await ctx.send("Vote to stop passed. Stopping the player.", delete_after=10)
            await player.disconnect()
        else:
            await ctx.send(
                f"{ctx.author.mention} has voted to stop the player.", delete_after=15
            )

    @commands.command()
    async def skip(self, ctx: commands.Context) -> None:
        """Skip the current song."""
        player: Player = cast(Player, ctx.voice_client)
        if not player:
            return

        print(f"[SKIP] Player : {player}")

        if self.is_privileged(ctx):
            await ctx.send("An admin or DJ has skipped the song.", delete_after=10)
            player.skip_votes.clear()
            print(f"[SKIP] Privileged Clearing votes...")
            return await player.skip()

        if ctx.author == player.current.requester:
            await print(f"[SKIP] Requester has skipped the song.")
            await ctx.send('The song requester has skipped the song.', delete_after=10)
            player.skip_votes.clear()
            return await player.skip()

        required = self.required(ctx)
        player.skip_votes.add(ctx.author)

        if len(player.skip_votes) >= required:
            print(f"[SKIP] Vote to skip passed. Skipping song.")
            await ctx.send('Vote to skip passed. Skipping song.', delete_after=10)
            player.skip_votes.clear()
            await player.skip()
        else:
            await ctx.send(f'{ctx.author.mention} has voted to skip the song.', delete_after=15)



    @commands.command(name="toggle", aliases=["pause", "resume"])
    async def pause_resume(self, ctx: commands.Context) -> None:
        """Toggle pause and resume on the player."""
        player: Player = ctx.voice_client

        if not player:
            return

        await player.pause(not player.paused)
        await ctx.message.add_reaction("\u2705")

    @commands.command()
    async def volume(self, ctx: commands.Context, value: int) -> None:
        """Set the player volume."""
        player: Player = ctx.voice_client

        if not player:
            return

        await player.set_volume(value)
        await ctx.message.add_reaction("\u2705")

    @commands.command(aliases=["dc"])
    async def disconnect(self, ctx: commands.Context) -> None:
        """Disconnect the player from the voice channel."""
        player: Player = ctx.voice_client
        if not player:
            return

        await player.disconnect()
        await ctx.message.add_reaction("\u2705")

    @commands.command(aliases=["q", "que"])
    async def queue(self, ctx: commands.Context):
        """Display the next 10 songs in the player's queue."""
        player: wavelink.Player = ctx.voice_client

        if not player.connected:
            return await ctx.send("The player is not connected to a voice channel.")

        queue_length = len(player.queue)
        if queue_length == 0:
            return await ctx.send(
                "There are no more songs in the queue.", delete_after=15
            )

        # Get up to the next 10 songs
        next_songs = player.queue[:10]
        entries = [track.title for track in next_songs]

        queue_message = "\n".join(entries) if entries else "No upcoming songs."
        await ctx.send(f"Next songs in the queue:\n{queue_message}")

    @commands.command(aliases=["mix"])
    async def shuffle(self, ctx: commands.Context):
        """Shuffle the player's queue."""
        player: Player = cast(Player, ctx.voice_client)
        if not player:
            return await ctx.send("The player is not connected to a voice channel.")

        queue_length = len(player.queue)
        if queue_length < 3:
            return await ctx.send(
                "Add more songs to the queue before shuffling.", delete_after=15
            )

        if self.is_privileged(ctx):
            await ctx.send("An admin or DJ has shuffled the playlist.", delete_after=10)
            player.shuffle_votes.clear()
            await player.queue.shuffle()

        else:
            required = self.required(ctx)
            player.shuffle_votes.add(ctx.author)

            if len(player.shuffle_votes) >= required:
                await ctx.send(
                    "Vote to shuffle passed. Shuffling the playlist.", delete_after=10
                )
                player.shuffle_votes.clear()
                await player.queue.shuffle()  # Utilisez ici la méthode intégrée de Wavelink pour mélanger
            else:
                await ctx.send(
                    f"{ctx.author.mention} has voted to shuffle the playlist.",
                    delete_after=15,
                )

    @commands.command()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def progress(self, ctx):
        """Display the progress of the current song."""
        player: Player = cast(Player, ctx.voice_client)

        if player is None or not player.current:
            return await ctx.send("I'm not playing anything!", delete_after=5)

        track = player.current
        position = datetime.timedelta(milliseconds=int(player.position))
        length = datetime.timedelta(milliseconds=int(track.length))

        embed = discord.Embed(
            title="Now Playing",
            description=f"[{track.title}]({track.uri})\n**`[{position}:{length}]`**",
            colour=random.randint(0x000000, 0xFFFFFF),
        )

        await ctx.send(embed=embed, delete_after=15)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
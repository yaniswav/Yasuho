import asyncio
import copy
import datetime
import logging
import math
import typing
from typing import cast

import async_timeout
import discord
import wavelink
from discord.ext import commands

from tools.config_loader import config_loader
from tools.formats import random_colour

log = logging.getLogger(__name__)

E_VOICE = config_loader.getstr("Emojis", "voice")


class NoChannelProvided(commands.CommandError):
    """Error raised when no suitable voice channel was supplied."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        log.warning("No suitable voice channel was supplied")


class IncorrectChannelError(commands.CommandError):
    """Error raised when commands are issued outside of the players session channel."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        log.warning("Command issued outside of the player's session channel")


class Track(wavelink.Playable):
    """Wavelink Track object with a requester attribute."""

    __slots__ = ('requester', )

    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self.requester = kwargs.get('requester')
        log.debug("Created track with requester: %s", self.requester)


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
        self.pause_votes.clear()
        self.resume_votes.clear()
        self.skip_votes.clear()
        self.shuffle_votes.clear()
        self.stop_votes.clear()
        log.debug("cleared votes")

    async def do_next(self) -> None:
        log.debug("do_next start")
        if self.current or self.waiting:
            log.debug("do_next: currently playing or waiting")
            return

        self.clear_votes()

        try:
            self.waiting = True
            with async_timeout.timeout(300):
                if len(self.queue) > 0:
                    track = self.queue.get()
                log.debug("do_next: going to play %s", track.title)
                await self.play(track)

                # next_songs = self.queue[:5]
                # entries = [track.title for track in next_songs]

                # queue_message = "\n".join(entries) if entries else "No upcoming songs."
                # print(f"Next {len(next_songs)} songs in the queue:\n{queue_message}")

        except asyncio.TimeoutError:
            # No music has been played for 5 minutes, cleanup and disconnect...
            return await self.teardown()

        self.waiting = False

        log.debug("do_next: playing %s", track.title)
        await self.invoke_controller()

    def build_embed(self) -> typing.Optional[discord.Embed]:
        log.debug("building embed")
        track = self.current

        next_track_title = self.queue.get().title

        if not track:
            log.debug("build_embed: no current track")
            return

        channel = self.client.get_channel(int(self.channel.id))
        qsize = len(self.queue)

        duration_seconds = int(track.length / 1000)
        duration_minutes, duration_seconds = divmod(duration_seconds, 60)
        duration_formatted = f"{duration_minutes:02}:{duration_seconds:02}"


        embed = discord.Embed(
            title=f"Music Controller | {E_VOICE} **{channel.name}**",
            colour=random_colour(),
        )
        embed.description = f"■ **Now Playing:**\n[{track.title}]({track.uri})\n■ **Artist:** `{track.author}`"
        embed.set_footer(text="If you enjoy the bot, don't forget to upvote :)")
        embed.set_image(url=track.artwork)

        embed.add_field(
            name="Duration",
            value=duration_formatted,
            inline=False,
        )

        embed.add_field(name="Next Track", value=f"{next_track_title}", inline=True)
        embed.add_field(name="Queue Length", value=str(qsize), inline=True)

        embed.add_field(name="Volume", value=f"**`{self.volume}%`**", inline=False)
        embed.add_field(name="DJ", value=self.dj.mention, inline=False)


        log.debug("embed built")
        return embed


    async def is_position_fresh(self) -> bool:
        log.debug("checking if controller position is fresh")
        try:
            async for message in self.ctx.channel.history(limit=5):
                if message.id == self.controller.message.id:
                    log.debug("controller position is fresh")
                    return True
        except (discord.HTTPException, AttributeError):
            log.exception("failed to check controller position freshness")
            return False

        log.debug("controller position is not fresh")
        return False

    async def teardown(self):
        log.debug("tearing down player")
        try:
            self.controller.disable_buttons()
            await self.disconnect()
            log.debug("controller disabled")
        except KeyError:
            log.exception("teardown failed")


    async def invoke_controller(self) -> None:
        """Method which updates or sends a new player controller."""
        log.debug("invoke_controller start")
        log.debug("invoke_controller updating status: %s", self.updating)
        if self.updating:
            log.debug("invoke_controller: currently updating, exiting")
            return

        self.updating = True

        if not self.controller:
            log.debug("creating new controller")
            self.controller = InteractiveController(embed=self.build_embed(), player=self)
            await self.controller.start(self.ctx)

        elif not await self.is_position_fresh():
            log.debug("position not fresh, updating controller")
            try:
                await self.controller.message.delete()
                log.debug("controller message deleted")
            except discord.HTTPException:
                log.error("failed to delete controller message")

            self.controller.stop()
            log.debug("controller stopped")

            self.controller = InteractiveController(embed=self.build_embed(), player=self)
            await self.controller.start(self.ctx)
        else:
            log.debug("position fresh, updating embed only")
            embed = self.build_embed()
            await self.controller.message.edit(content=None, embed=embed)

        self.updating = False
        log.debug("finished updating controller")


class InteractiveController(discord.ui.View):
    def __init__(self, *, embed: discord.Embed, player: Player):
        super().__init__(timeout=None)
        self.embed = embed
        self.player = player

    async def start(self, context: commands.Context):
        self.message = await context.send(embed=self.embed, view=self)
        self.ctx = context

    def update_context(self, interaction: discord.Interaction, button: discord.ui.Button, payload):
        """Update our context with the user who reacted."""
        ctx = copy.copy(self.ctx)
        ctx.author = payload.member

        return ctx

    def disable_buttons(self):
        """Disable all buttons."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

        # Update the buttons display
        asyncio.create_task(self.update_message())

    async def update_message(self):
        """Update the message with the disabled buttons."""
        if self.message:
            await self.message.edit(view=self)

    async def send_initial_message(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> discord.Message:
        return await channel.send(embed=self.embed)

    @discord.ui.button(style=discord.ButtonStyle.gray, emoji="\U000023ef", custom_id="pause_resume", row=0)
    async def pause_resume_command(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.debug("pause/resume button pressed")
        try:
            ctx = await self.player.client.get_context(interaction.message)
            command = self.player.client.get_command("resume")
            ctx.command = command
            await self.player.client.invoke(ctx)
            log.debug("pause/resume command invoked")
        except Exception:
            log.exception("pause/resume command failed")


    @discord.ui.button(style=discord.ButtonStyle.gray, emoji="\u23F9", custom_id="stop", row=0)
    async def stop_command(self, interaction: discord.Interaction, button: discord.ui.Button, payload):
        """Stop button."""
        ctx = self.update_context(payload)

        command = self.bot.get_command("stop")
        ctx.command = command

        await self.bot.invoke(ctx)

    @discord.ui.button(style=discord.ButtonStyle.gray, emoji="\u23ED", custom_id="skip", row=0)
    async def skip_command(self, interaction: discord.Interaction, button: discord.ui.Button, payload):
        """Skip button."""
        ctx = self.update_context(payload)

        command = self.bot.get_command("skip")
        ctx.command = command

        await self.bot.invoke(ctx)


    @discord.ui.button(style=discord.ButtonStyle.gray, emoji="\U0001f504", custom_id="restart", row=0)
    async def restart_command(self, interaction: discord.Interaction, button: discord.ui.Button, payload):
        """Restart."""
        ctx = self.update_context(payload)

        command = self.bot.get_command("restart")
        ctx.command = command

        await self.bot.invoke(ctx)

    @discord.ui.button(style=discord.ButtonStyle.gray, emoji="\U0001F500", custom_id="shuffle", row=1)
    async def shuffle_command(self, interaction: discord.Interaction, button: discord.ui.Button, payload):
        """Shuffle button."""
        ctx = self.update_context(payload)

        command = self.bot.get_command("shuffle")
        ctx.command = command

        await self.bot.invoke(ctx)

    @discord.ui.button(style=discord.ButtonStyle.gray, emoji="\U0001f4dc", custom_id="queue", row=1)
    async def queue_command(self, interaction: discord.Interaction, button: discord.ui.Button, payload):
        """Player queue button."""


        ctx = await self.player.client.get_context(interaction.message)
        command = self.player.client.get_command("queue")
        ctx.command = command
        await self.player.client.invoke(ctx)


class Music(commands.Cog):
    """Music playback commands powered by Wavelink/Lavalink."""

    def __init__(self, bot):
        self.bot = bot


    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):

        if member.bot:
            return

        player: Player = cast(Player, member.guild.voice_client)

        if not player or not player.connected:
            # Exit if the player does not exist or is not connected
            return

        log.debug("voice channel member count: %s", len(player.channel.members))

        channel = self.bot.get_channel(player.channel.id)
        if not channel:
            # Exit if the channel does not exist
            log.warning("channel not found")
            return
        log.debug("channel: %s", channel)

        # Update the DJ if the current DJ leaves the channel
        if member == player.dj and after.channel != channel:
            player.dj = next((m for m in channel.members if not m.bot), None)

        # Assign a new DJ if the current DJ is not in the channel
        elif after.channel == channel and player.dj not in channel.members:
            player.dj = member

        log.debug("player dj: %s", player.dj)
        # Disconnect the player if it's the only member left in the channel
        if len(channel.members) == 1:
            log.debug("channel size is 1")
            await asyncio.sleep(15)
            if len(channel.members) == 1:
                player.controller.disable_buttons()
                await player.disconnect()

    @commands.Cog.listener()
    async def on_wavelink_track_start(
        self, payload: wavelink.TrackStartEventPayload
    ) -> None:


        player: Player | None = payload.player
        log.debug("track start - player: %s", payload.player)

        if not player:
            # Handle edge cases...
            return

        original: wavelink.Playable | None = payload.original
        track: wavelink.Playable = payload.track
        log.debug("track start: %s", track.title)

        embed: discord.Embed = discord.Embed(title="Now Playing")
        embed.description = f"**{track.title}** by `{track.author}`"

        if track.artwork:
            embed.set_image(url=track.artwork)

        if original and original.recommended:
            embed.description += f"\n\n`This track was recommended via {track.source}`"

        if track.album.name:
            embed.add_field(name="Album", value=track.album.name)

        # await player.home.send(embed=embed, delete_after=5)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player: Player | None = payload.player

        log.debug("track ended: %s, reason: %s", payload.track.title, payload.reason)
        log.debug("moving to next track in queue")

        await player.do_next()

    @commands.Cog.listener()
    async def on_track_exception(self, node: wavelink.Node, payload):
        log.error("track exception: %s, reason: %s", payload.exception, payload.reason)
        await payload.player.do_next()

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        await player.channel.send(f"The player has been inactive for `{player.inactive_timeout}` seconds. Goodbye!")
        await player.disconnect()


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
        player: Player = cast(Player, ctx.voice_client)

        channel = self.bot.get_channel(int(player.channel.id))
        required = math.ceil((len(channel.members) - 1) / 2.5)

        if ctx.command.name == "stop":
            if len(channel.members) == 3:
                required = 2

        return required

    def is_privileged(self, ctx: commands.Context) -> bool:
        """Check whether the user is an Admin or DJ."""
        player: Player = cast(Player, ctx.voice_client)
        if player and player.dj:
            return player.dj == ctx.author or ctx.author.guild_permissions.kick_members
        return False

    @commands.command()
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        """Play a song from a query."""
        player: Player = cast(Player, ctx.voice_client)

        if not player:
            try:
                player: Player = await ctx.author.voice.channel.connect(cls=Player(ctx=ctx))
            except AttributeError:
                log.error("could not connect: user is not in a voice channel")
                await ctx.send(
                    "Please join a voice channel first before using this command."
                )
                return
            except discord.ClientException:
                await ctx.send(
                    "I was unable to join this voice channel. Please try again."
                )
                return

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
            # Play now since we aren't playing anything...
            await player.play(player.queue.get())

    @commands.command()
    async def stop(self, ctx: commands.Context):
        """Stop the player, and disconnect from the channel."""
        player: Player = cast(Player, ctx.voice_client)

        if not player.connected:
            return

        if self.is_privileged(ctx):
            await ctx.send("An admin or DJ has stopped the player.", delete_after=10)
            return await player.teardown()

        required = self.required(ctx)
        player.stop_votes.add(ctx.author)

        if len(player.stop_votes) >= required:
            await ctx.send("Vote to stop passed. Stopping the player.", delete_after=10)
            await player.teardown()
        else:
            await ctx.send(
                f"{ctx.author.mention} has voted to stop the player.", delete_after=15
            )

    @commands.command()
    async def skip(self, ctx: commands.Context) -> None:
        """Skip the current song."""
        player: Player = cast(Player, ctx.voice_client)
        if not player:
            await ctx.send("No player found.")
            return

        if self.is_privileged(ctx):
            await ctx.send("An admin or DJ has skipped the song.", delete_after=10)
            player.skip_votes.clear()
            log.debug("privileged user clearing skip votes")
            try:
                await player.skip()
                log.debug("song skipped by privileged user")
            except Exception:
                log.exception("error skipping song")

            return

        if ctx.author == player.current.requester:
            await ctx.send('The song requester has skipped the song.', delete_after=10)
            player.skip_votes.clear()
            try:
                await player.skip()
                log.debug("song skipped by requester")
            except Exception:
                log.exception("error skipping song")
            return

        required = self.required(ctx)
        player.skip_votes.add(ctx.author)

        if len(player.skip_votes) >= required:
            log.debug("vote to skip passed, skipping song")
            await ctx.send('Vote to skip passed. Skipping song.', delete_after=10)
            player.skip_votes.clear()
            try:
                await player.skip()
                log.debug("song skipped by vote")
            except Exception:
                log.exception("error skipping song")
        else:
            await ctx.send(f'{ctx.author.mention} has voted to skip the song.', delete_after=15)

    @commands.command(name="toggle", aliases=["pause", "resume"])
    async def pause_resume(self, ctx: commands.Context) -> None:
        """Toggle pause and resume on the player."""
        player: Player = cast(Player, ctx.voice_client)

        if not player:
            return

        await player.pause(not player.paused)
        await ctx.message.add_reaction("\u2705")

    @commands.command()
    async def volume(self, ctx: commands.Context, value: int) -> None:
        """Set the player volume."""
        player: Player = cast(Player, ctx.voice_client)

        if not player:
            return

        await player.set_volume(value)
        await ctx.message.add_reaction("\u2705")

    @commands.command(aliases=["dc"])
    async def disconnect(self, ctx: commands.Context) -> None:
        """Disconnect the player from the voice channel."""
        player: Player = cast(Player, ctx.voice_client)
        if not player:
            return

        await player.disconnect()
        await ctx.message.add_reaction("\u2705")

    @commands.command(aliases=['np', 'now_playing', 'current'])
    async def nowplaying(self, ctx: commands.Context):
        """Update the player controller."""
        player: Player = ctx.voice_client

        if not player.current:
            return

        await player.invoke_controller()

    @commands.command(aliases=["q", "que"])
    async def queue(self, ctx: commands.Context):
        """Display the next 10 songs in the player's queue."""
        player: Player = cast(Player, ctx.voice_client)

        if not player.connected:
            return await ctx.send("The player is not connected to a voice channel.")

        queue_length = len(player.queue)
        if queue_length == 0:
            return await ctx.send(
                "There are no more songs in the queue.", delete_after=15
            )

        # Get up to the next 10 songs
        next_songs = player.queue[:20]
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
                await player.queue.shuffle()
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
            colour=random_colour(),
        )

        await ctx.send(embed=embed, delete_after=15)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))

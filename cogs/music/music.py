import asyncio
import logging
import typing

import discord
import sonolink
import sonolink.models
from discord import app_commands
from discord.ext import commands
from sonolink.rest.enums import TrackSourceType

from tools.config_loader import config_loader
from tools.formats import random_colour

log = logging.getLogger(__name__)

E_VOICE = config_loader.getstr("Emojis", "voice")

# Default search source for plain (non-URL) queries. Full URLs are still resolved
# directly by Lavalink regardless of this value.
SEARCH_SOURCE = TrackSourceType.YOUTUBE


class Player(sonolink.Player):
    """A sonolink player that also tracks the DJ, home text channel, and controller.

    sonolink connects players via the discord.py class-pass form
    (``channel.connect(cls=Player)``), so these extras are populated by the cog
    immediately after the connection is established rather than in ``__init__``.
    """

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self.dj: typing.Optional[discord.Member] = None
        self.home: typing.Optional[discord.abc.MessageableChannel] = None
        self.controller: typing.Optional["PlayerController"] = None


def format_duration(track: sonolink.models.Playable) -> str:
    """Return a track's duration as ``mm:ss`` (or ``LIVE`` for streams)."""
    if track.is_stream:
        return "LIVE"
    total_seconds = track.length // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def build_now_playing_embed(player: Player) -> typing.Optional[discord.Embed]:
    """Build a pretty "now playing" embed for the player's current track."""
    track = player.current
    if track is None:
        return None

    # Temporary diagnostic: shows whether Lavalink returned an artwork URL.
    log.info("now-playing: %s | artwork=%r", track.title, track.artwork)

    embed = discord.Embed(
        title=track.title[:256],
        url=track.uri or None,
        description=f"by **{track.author}**",
        colour=random_colour(),
    )
    embed.set_author(name="🎵 Now Playing")

    # The track artwork as a big banner is what makes the controller look good.
    if track.artwork:
        embed.set_image(url=track.artwork)

    status = "⏸ Paused" if player.paused else "▶ Playing"
    embed.add_field(name="Status", value=status)
    embed.add_field(name="Duration", value=f"`{format_duration(track)}`")
    embed.add_field(name="Volume", value=f"`{player.volume}%`")

    channel_name = player.channel.name if player.channel else "voice"
    embed.add_field(name="Channel", value=f"{E_VOICE} {channel_name}")
    if player.dj is not None:
        embed.add_field(name="DJ", value=player.dj.mention)
    requester_id = getattr(track.extras, "requester", None)
    if requester_id:
        embed.add_field(name="Requested by", value=f"<@{requester_id}>")

    upcoming = player.queue.tracks
    if upcoming:
        lines = "\n".join(
            f"`{i}.` {t.title[:60]}" for i, t in enumerate(upcoming[:5], 1)
        )
        if len(upcoming) > 5:
            lines += f"\n`+{len(upcoming) - 5}` more in the queue"
        embed.add_field(name=f"Up Next ({len(upcoming)})", value=lines, inline=False)
    else:
        embed.add_field(
            name="Up Next",
            value="Nothing queued. Add a song to keep the music going!",
            inline=False,
        )

    embed.set_footer(text="Use the buttons below to control playback")
    return embed


class PlayerController(discord.ui.View):
    """Interactive now-playing controls, restricted to listeners in the channel."""

    def __init__(self, cog: "Music", player: Player, *, timeout: float = 600.0) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.player = player
        self.message: typing.Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow members currently in the player's voice channel."""
        channel = getattr(self.player, "channel", None)
        if channel is None:
            await interaction.response.send_message(
                "The player is no longer active.", ephemeral=True
            )
            return False

        user = interaction.user
        if (
            not isinstance(user, discord.Member)
            or user.voice is None
            or user.voice.channel != channel
        ):
            await interaction.response.send_message(
                "You must be in my voice channel to use these controls.",
                ephemeral=True,
            )
            return False

        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                log.exception("Failed to disable controller on timeout")

    async def _report_failure(self, interaction: discord.Interaction) -> None:
        """Best-effort error notice when a button callback raises."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong handling that action.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong handling that action.", ephemeral=True
                )
        except discord.HTTPException:
            log.exception("Failed to report controller error to the user")

    @discord.ui.button(label="Pause/Resume", emoji="⏯️", style=discord.ButtonStyle.secondary)
    async def pause_resume(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            if self.player.paused:
                await self.player.resume()
                await interaction.response.send_message("Resumed.", ephemeral=True)
            else:
                await self.player.pause()
                await interaction.response.send_message("Paused.", ephemeral=True)
        except Exception:
            log.exception("Controller pause/resume failed")
            await self._report_failure(interaction)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            await self.player.skip()
            await interaction.response.send_message("Skipped.", ephemeral=True)
        except sonolink.QueueEmpty:
            await interaction.response.send_message(
                "There is nothing left to skip to.", ephemeral=True
            )
        except Exception:
            log.exception("Controller skip failed")
            await self._report_failure(interaction)

    @discord.ui.button(label="Shuffle", emoji="\U0001f500", style=discord.ButtonStyle.secondary)
    async def shuffle(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            if len(self.player.queue.tracks) < 2:
                await interaction.response.send_message(
                    "Add a few more tracks before shuffling.", ephemeral=True
                )
                return
            self.player.queue.shuffle()
            await interaction.response.send_message("Shuffled the queue.", ephemeral=True)
        except Exception:
            log.exception("Controller shuffle failed")
            await self._report_failure(interaction)

    @discord.ui.button(label="Queue", emoji="\U0001f4dc", style=discord.ButtonStyle.secondary)
    async def show_queue(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            upcoming = self.player.queue.tracks
            if not upcoming:
                await interaction.response.send_message(
                    "The queue is empty.", ephemeral=True
                )
                return
            lines = [
                f"`{index}.` {track.title} by `{track.author}`"
                for index, track in enumerate(upcoming[:10], start=1)
            ]
            if len(upcoming) > 10:
                lines.append(f"*...and {len(upcoming) - 10} more.*")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        except Exception:
            log.exception("Controller queue failed")
            await self._report_failure(interaction)

    @discord.ui.button(label="Disconnect", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def disconnect(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            await interaction.response.edit_message(view=self)
            await self.player.disconnect()
            self.stop()
        except Exception:
            log.exception("Controller disconnect failed")
            await self._report_failure(interaction)


class Music(commands.Cog):
    """Music playback commands powered by sonolink (Lavalink v4)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _client(self) -> typing.Optional[sonolink.Client]:
        return getattr(self.bot, "sl_client", None)

    def _nodes_available(self) -> bool:
        client = self._client()
        return bool(client and client.nodes)

    async def _send_controller(self, player: Player) -> None:
        """Send a fresh now-playing controller in the player's home channel."""
        if player.home is None:
            return

        embed = build_now_playing_embed(player)
        if embed is None:
            return

        old = player.controller
        if old is not None:
            old.stop()
            if old.message is not None:
                try:
                    await old.message.delete()
                except discord.HTTPException:
                    log.exception("Failed to delete the previous controller message")

        view = PlayerController(self, player)
        try:
            message = await player.home.send(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Failed to send the now-playing controller")
            return

        view.message = message
        player.controller = view

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_sonolink_track_start(
        self, player: Player, event: sonolink.gateway.TrackStartEvent
    ) -> None:
        log.debug("Track started: %s", event.track.title)
        if getattr(player, "home", None) is None:
            return
        await self._send_controller(player)

    @commands.Cog.listener()
    async def on_sonolink_track_exception(
        self, player: Player, event: sonolink.gateway.TrackExceptionEvent
    ) -> None:
        log.error(
            "Track exception on %s: %s",
            event.track.title,
            event.exception.message,
        )
        home = getattr(player, "home", None)
        if home is not None:
            try:
                await home.send(
                    f"There was a problem playing **{event.track.title}**, skipping it."
                )
            except discord.HTTPException:
                log.exception("Failed to notify channel of track exception")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Leave the channel a short while after the last human leaves."""
        if member.bot:
            return

        player = member.guild.voice_client
        if not isinstance(player, sonolink.Player):
            return

        channel = player.channel
        if channel is None:
            return

        humans = [m for m in channel.members if not m.bot]
        if humans:
            return

        await asyncio.sleep(15)

        channel = player.channel
        if channel is None:
            return
        if any(not m.bot for m in channel.members):
            return

        try:
            await player.disconnect()
        except Exception:
            log.exception("Failed to auto-disconnect from an empty channel")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="play", aliases=["p"])
    @commands.guild_only()
    @app_commands.describe(query="A song name or URL to search for and play.")
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        """Play a track or playlist, or add it to the queue."""
        await ctx.defer()

        if not self._nodes_available():
            await ctx.send("Music is currently unavailable - no Lavalink node is connected.")
            return

        player = ctx.voice_client
        if player is None:
            if not ctx.author.voice or not ctx.author.voice.channel:
                await ctx.send("You must be in a voice channel first.")
                return
            try:
                player = await ctx.author.voice.channel.connect(cls=Player)
            except discord.ClientException:
                log.exception("Failed to connect to the voice channel")
                await ctx.send("I was unable to join your voice channel. Please try again.")
                return
            player.dj = ctx.author
            player.home = ctx.channel

        if player.home is None:
            player.home = ctx.channel
        elif player.home != ctx.channel:
            await ctx.send(f"The player is already active in {player.home.mention}.")
            return

        try:
            result = await self.bot.sl_client.search_track(query, source=SEARCH_SOURCE)
        except RuntimeError:
            log.exception("Track search failed: no node available")
            await ctx.send("Music is currently unavailable - no Lavalink node is connected.")
            return

        if result.is_error() or result.is_empty() or result.result is None:
            await ctx.send("Could not find any tracks for that query.")
            return

        data = result.result

        if isinstance(data, sonolink.models.Playlist):
            for track in data.tracks:
                track.extras.requester = ctx.author.id
            player.queue.put(data.tracks)
            await ctx.send(
                f"Added the playlist **{data.name}** ({len(data.tracks)} tracks) to the queue."
            )
        else:
            track = data[0] if isinstance(data, list) else data
            track.extras.requester = ctx.author.id
            player.queue.put(track)
            await ctx.send(f"Added **{track.title}** by `{track.author}` to the queue.")

        if not player.current:
            await player.play(player.queue.get())

    @commands.hybrid_command(name="pause")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context) -> None:
        """Pause the current track."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send("I'm not connected to a voice channel.")
            return
        if player.paused:
            await ctx.send("The player is already paused.")
            return
        await player.pause()
        await ctx.send("Paused the player.")

    @commands.hybrid_command(name="resume")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context) -> None:
        """Resume the player if it is paused."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send("I'm not connected to a voice channel.")
            return
        if not player.paused:
            await ctx.send("The player is not paused.")
            return
        await player.resume()
        await ctx.send("Resumed the player.")

    @commands.hybrid_command(name="skip", aliases=["next"])
    @commands.guild_only()
    async def skip(self, ctx: commands.Context) -> None:
        """Skip the current track and play the next one."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send("I'm not connected to a voice channel.")
            return
        try:
            track = await player.skip()
        except sonolink.QueueEmpty:
            await ctx.send("There are no more tracks in the queue to skip to.")
            return
        if track:
            await ctx.send(f"Skipped to **{track.title}** by `{track.author}`.")
        else:
            await ctx.send("Skipped. The queue is now empty.")

    @commands.hybrid_command(name="stop")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context) -> None:
        """Stop playback and clear the queue (stays connected)."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send("I'm not connected to a voice channel.")
            return
        await player.stop(clear_queue=True)
        await ctx.send("Stopped playback and cleared the queue.")

    @commands.hybrid_command(name="volume", aliases=["vol"])
    @commands.guild_only()
    @app_commands.describe(value="Volume level between 0 and 1000 (100 is default).")
    async def volume(
        self, ctx: commands.Context, value: commands.Range[int, 0, 1000]
    ) -> None:
        """Set the player volume (0-1000)."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send("I'm not connected to a voice channel.")
            return
        await player.set_volume(value)
        await ctx.send(f"Set the volume to {value}%.")

    @commands.hybrid_command(name="shuffle", aliases=["mix"])
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context) -> None:
        """Shuffle the upcoming tracks in the queue."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send("I'm not connected to a voice channel.")
            return
        if len(player.queue.tracks) < 2:
            await ctx.send("Add a few more tracks to the queue before shuffling.")
            return
        player.queue.shuffle()
        await ctx.send("Shuffled the queue.")

    @commands.hybrid_command(name="loop")
    @commands.guild_only()
    @app_commands.describe(mode="One of: track, all, off.")
    async def loop(
        self,
        ctx: commands.Context,
        mode: typing.Literal["track", "all", "off"] = "track",
    ) -> None:
        """Set the loop mode for the queue."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send("I'm not connected to a voice channel.")
            return
        mapping = {
            "track": sonolink.QueueMode.LOOP,
            "all": sonolink.QueueMode.LOOP_ALL,
            "off": sonolink.QueueMode.NORMAL,
        }
        player.queue.mode = mapping[mode]
        await ctx.send(f"Loop mode set to `{mode}`.")

    @commands.hybrid_command(name="queue", aliases=["q", "que"])
    @commands.guild_only()
    async def queue(self, ctx: commands.Context) -> None:
        """Show the currently playing track and the next tracks in the queue."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send("I'm not connected to a voice channel.")
            return

        upcoming = player.queue.tracks
        if not upcoming and not player.current:
            await ctx.send("The queue is empty.")
            return

        lines: list[str] = []
        if player.current:
            lines.append(
                f"**Now Playing:** {player.current.title} by `{player.current.author}`\n"
            )
        if upcoming:
            lines.append("**Up Next:**")
            for index, track in enumerate(upcoming[:10], start=1):
                lines.append(f"`{index}.` {track.title} by `{track.author}`")
            if len(upcoming) > 10:
                lines.append(f"*...and {len(upcoming) - 10} more.*")

        embed = discord.Embed(
            title="Queue",
            description="\n".join(lines),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="nowplaying", aliases=["np", "current"])
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context) -> None:
        """Show the interactive now-playing controller."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player) or not player.current:
            await ctx.send("Nothing is playing right now.")
            return
        player.home = ctx.channel
        await self._send_controller(player)
        if ctx.interaction is not None:
            await ctx.send("Here is the player.", ephemeral=True)

    @commands.hybrid_command(name="disconnect", aliases=["dc", "leave"])
    @commands.guild_only()
    async def disconnect(self, ctx: commands.Context) -> None:
        """Disconnect the player from the voice channel."""
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send("I'm not connected to a voice channel.")
            return
        await player.disconnect()
        await ctx.send("Disconnected from the voice channel.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))

import asyncio
import logging
import time
import typing
from datetime import datetime, timezone

import discord
import sonolink
import sonolink.models
from discord import app_commands
from discord.ext import commands, tasks
from sonolink.rest.enums import TrackSourceType

from tools import interactions, music_state
from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.i18n import _
from tools.paginator import Paginator, paginate_lines
from tools.views import LocaleModal

log = logging.getLogger(__name__)

E_VOICE = config_loader.getstr("Emojis", "voice")

# Default search source for plain (non-URL) queries. Full URLs are still resolved
# directly by Lavalink regardless of this value.
SEARCH_SOURCE = TrackSourceType.YOUTUBE

# How long (in seconds) a player may stay idle before it is disconnected to free
# resources. A player counts as idle when it is paused, has nothing playing and
# an empty queue, or is alone in its voice channel. See the idle-timeout loop.
IDLE_TIMEOUT = 300

# Only resume a persisted player younger than this (seconds). Scopes the
# survive-restart behaviour to a quick restart, so the bot never rejoins a
# channel and starts blasting music after a long downtime.
RESTORE_MAX_AGE = 600

# How many players to restore in parallel on startup. Bounded so a large fleet
# never fires a burst of voice reconnects at Discord's rate limits at once - a
# few hundred active players then restore in seconds instead of minutes, with no
# thundering herd.
RESTORE_CONCURRENCY = 5


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
        self.controller: typing.Optional["MusicController"] = None
        # Monotonic timestamp of when this player first became idle, or None
        # while it is active. Maintained by the cog's idle-timeout loop.
        self.idle_since: typing.Optional[float] = None
        # On a cold restore the cog posts the controller explicitly (track_start
        # may never fire for a paused restore); this holds the restored track's
        # encoded id so the ONE matching track_start skips its own post and the
        # two never race into a duplicate. Consumed on first match.
        self._suppress_controller_track: typing.Optional[str] = None


def format_duration(track: sonolink.models.Playable) -> str:
    """Return a track's duration as ``mm:ss`` (or ``LIVE`` for streams)."""
    if track.is_stream:
        return "LIVE"
    total_seconds = track.length // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _first_track(
    result: typing.Optional[typing.Any],
) -> typing.Optional[sonolink.models.Playable]:
    """Return the first usable :class:`Playable` from a search result, or None.

    Normalises the three shapes ``search_track`` can hand back (a Playlist, a
    list of tracks, or a single track) into one track ready to be queued.
    """
    if result is None or result.is_error() or result.is_empty() or result.result is None:
        return None
    data = result.result
    if isinstance(data, sonolink.models.Playlist):
        return data.tracks[0] if data.tracks else None
    if isinstance(data, list):
        return data[0] if data else None
    return data


def _loop_to_int(mode) -> int:
    """Map a sonolink QueueMode to the persisted loop_mode column value."""
    if mode == sonolink.QueueMode.LOOP:
        return music_state.LOOP_TRACK
    if mode == sonolink.QueueMode.LOOP_ALL:
        return music_state.LOOP_QUEUE
    return music_state.LOOP_OFF


def _int_to_loop(value):
    """Map a persisted loop_mode value back to a sonolink QueueMode."""
    if value == music_state.LOOP_TRACK:
        return sonolink.QueueMode.LOOP
    if value == music_state.LOOP_QUEUE:
        return sonolink.QueueMode.LOOP_ALL
    return sonolink.QueueMode.NORMAL


class AddSongModal(LocaleModal, title="Add a song"):
    """Modal that queues a track from a search query or a full URL.

    A modal is used instead of listening for a follow-up chat message so the
    flow stays self-contained and does not leak extra messages into the channel.
    """

    song: discord.ui.TextInput = discord.ui.TextInput(
        label="Song or URL",
        placeholder="A song name to search, or a full URL",
        style=discord.TextStyle.short,
        required=True,
        max_length=400,
    )

    def __init__(self, cog: "Music", controller: "MusicController") -> None:
        super().__init__()
        self.cog = cog
        self.controller = controller

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            player = self.controller.player
            if not isinstance(player, sonolink.Player) or player.channel is None:
                await interaction.response.send_message(
                    _("The player is no longer active."), ephemeral=True
                )
                return

            query = self.song.value.strip()
            if not query:
                await interaction.response.send_message(
                    _("Give me a song name or URL to add."), ephemeral=True
                )
                return

            track = _first_track(await self.cog._search(query))
            if track is None:
                await interaction.response.send_message(
                    _("Could not find anything for `{query}`.").format(query=query),
                    ephemeral=True,
                )
                return

            track.extras.requester = interaction.user.id
            player.queue.put(track)
            if not player.current:
                await player.play(player.queue.get())
            await self.cog._snapshot(player)

            await interaction.response.send_message(
                _("Queued **{title}**.").format(title=track.title), ephemeral=True
            )
            await self.controller._rerender()
        except Exception:
            log.exception("Add-song modal submit failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong adding that song.")
            )


class _ControllerButton(discord.ui.Button):
    """A controller button whose callback delegates to a bound handler.

    Components V2 layouts cannot use the ``@discord.ui.button`` decorator (buttons
    live inside :class:`discord.ui.ActionRow` children), so each button is a plain
    instance that forwards its click to a coroutine on the owning view.
    """

    def __init__(
        self,
        handler: typing.Callable[
            [discord.Interaction], typing.Awaitable[None]
        ],
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(**kwargs)
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._handler(interaction)


class MusicController(discord.ui.LayoutView):
    """Interactive now-playing controls as a Components V2 layout.

    A coloured container holds the track details and the playback buttons. The
    view is restricted to listeners currently in the player's voice channel.
    """

    def __init__(
        self,
        cog: "Music",
        player: Player,
        *,
        track: typing.Optional[sonolink.models.Playable] = None,
        timeout=None,
    ) -> None:
        # timeout=None so the controls never die mid-track (a long song or a
        # livestream fires no track_start to refresh the timer). The controller
        # is explicitly stopped + deleted on track change, idle teardown and
        # disconnect, so it never lingers.
        super().__init__(timeout=timeout)
        self.cog = cog
        self.player = player
        # Fallback track for the first render only. sonolink's Player.play() sets
        # player.current after its REST update returns, but Lavalink's track_start
        # arrives over the websocket first, so a controller built straight off
        # that event would see player.current is None. Render from the event's
        # track until player.current catches up (see _build).
        self._track = track
        self.message: typing.Optional[discord.Message] = None
        self._build()

    def _make_button(
        self,
        handler: typing.Callable[
            [discord.Interaction], typing.Awaitable[None]
        ],
        **kwargs: typing.Any,
    ) -> _ControllerButton:
        return _ControllerButton(handler, **kwargs)

    def _build(self) -> None:
        """(Re)assemble the layout from the player's current state."""
        self.clear_items()

        # player.current wins once sonolink has set it; self._track only covers
        # the brief window during a cold restore / track change where the
        # websocket track_start beat play()'s REST update and current is None.
        track = self.player.current or self._track
        if track is None:
            self.add_item(discord.ui.TextDisplay(_("Nothing is playing right now.")))
            return

        container = discord.ui.Container(accent_colour=random_colour())

        title = track.title[:256]
        header = f"## [{title}]({track.uri})" if track.uri else f"## {title}"
        container.add_item(discord.ui.TextDisplay(_("### 🎵 Now Playing")))
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(
            discord.ui.TextDisplay(_("by **{author}**").format(author=track.author))
        )
        container.add_item(discord.ui.Separator())

        status = _("⏸ Paused") if self.player.paused else _("▶ Playing")
        mode = self.player.queue.mode
        if mode == sonolink.QueueMode.LOOP_ALL:
            loop_state = _("On (queue)")
        elif mode == sonolink.QueueMode.LOOP:
            loop_state = _("On (track)")
        else:
            loop_state = _("Off")
        container.add_item(
            discord.ui.TextDisplay(
                _(
                    "**Status:** {status}\n"
                    "**Duration:** `{duration}`\n"
                    "**Volume:** `{volume}%`\n"
                    "**Loop:** {loop}"
                ).format(
                    status=status,
                    duration=format_duration(track),
                    volume=self.player.volume,
                    loop=loop_state,
                )
            )
        )

        channel_name = self.player.channel.name if self.player.channel else "voice"
        meta_lines = [
            _("**Channel:** {emoji} {channel}").format(
                emoji=E_VOICE, channel=channel_name
            )
        ]
        if self.player.dj is not None:
            meta_lines.append(
                _("**DJ:** {dj}").format(dj=self.player.dj.mention)
            )
        requester_id = getattr(track.extras, "requester", None)
        if requester_id:
            meta_lines.append(
                _("**Requested by:** <@{requester_id}>").format(
                    requester_id=requester_id
                )
            )
        container.add_item(discord.ui.TextDisplay("\n".join(meta_lines)))

        container.add_item(discord.ui.Separator())

        upcoming = self.player.queue.tracks
        if upcoming:
            lines = "\n".join(
                f"`{i}.` {t.title[:60]}" for i, t in enumerate(upcoming[:5], 1)
            )
            if len(upcoming) > 5:
                lines += _("\n`+{count}` more in the queue").format(
                    count=len(upcoming) - 5
                )
            up_next = _("**Up Next ({count})**\n{lines}").format(
                count=len(upcoming), lines=lines
            )
        else:
            up_next = _(
                "**Up Next**\nNothing queued. Add a song to keep the music going!"
            )
        container.add_item(discord.ui.TextDisplay(up_next))

        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.ActionRow(
                self._make_button(
                    self._pause_resume,
                    label=_("Pause/Resume"),
                    emoji="⏯️",
                    style=discord.ButtonStyle.secondary,
                ),
                self._make_button(
                    self._skip,
                    label=_("Skip"),
                    emoji="⏭️",
                    style=discord.ButtonStyle.secondary,
                ),
                self._make_button(
                    self._volume_down,
                    label=_("Vol -"),
                    emoji="🔉",
                    style=discord.ButtonStyle.secondary,
                ),
                self._make_button(
                    self._volume_up,
                    label=_("Vol +"),
                    emoji="🔊",
                    style=discord.ButtonStyle.secondary,
                ),
                self._make_button(
                    self._loop_toggle,
                    label=_("Loop"),
                    emoji="🔁",
                    style=discord.ButtonStyle.secondary,
                ),
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                self._make_button(
                    self._shuffle,
                    label=_("Shuffle"),
                    emoji="\U0001f500",
                    style=discord.ButtonStyle.secondary,
                ),
                self._make_button(
                    self._show_queue,
                    label=_("Queue"),
                    emoji="\U0001f4dc",
                    style=discord.ButtonStyle.secondary,
                ),
                self._make_button(
                    self._add_song,
                    label=_("Add"),
                    emoji="➕",
                    style=discord.ButtonStyle.success,
                ),
                self._make_button(
                    self._favorite,
                    label=_("Favorite"),
                    emoji="⭐",
                    style=discord.ButtonStyle.secondary,
                ),
                self._make_button(
                    self._disconnect,
                    label=_("Disconnect"),
                    emoji="⏹️",
                    style=discord.ButtonStyle.danger,
                ),
            )
        )

        container.add_item(
            discord.ui.TextDisplay(_("-# Use the buttons to control playback"))
        )

        self.add_item(container)

    def _disable_all(self) -> None:
        """Disable every button in the layout (walks nested ActionRows)."""
        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow members currently in the player's voice channel."""
        channel = getattr(self.player, "channel", None)
        if channel is None:
            await interaction.response.send_message(
                _("The player is no longer active."), ephemeral=True
            )
            return False

        user = interaction.user
        if (
            not isinstance(user, discord.Member)
            or user.voice is None
            or user.voice.channel != channel
        ):
            await interaction.response.send_message(
                _("You must be in my voice channel to use these controls."),
                ephemeral=True,
            )
            return False

        return True

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                log.exception("Failed to disable controller on timeout")

    async def _report_failure(self, interaction: discord.Interaction) -> None:
        """Best-effort error notice when a button callback raises."""
        await interactions.notify_failure(
            interaction, _("Something went wrong handling that action.")
        )

    async def _rerender(self) -> None:
        """Re-render the now-playing layout in place so it reflects new state."""
        if self.message is None:
            return
        if self.player.current is None:
            return
        self._build()
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            log.exception("Failed to refresh the controller view")

    async def _pause_resume(self, interaction: discord.Interaction) -> None:
        try:
            if self.player.paused:
                await self.player.resume()
                message = _("Resumed.")
            else:
                await self.player.pause()
                message = _("Paused.")
            await self._rerender()
            await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            log.exception("Controller pause/resume failed")
            await self._report_failure(interaction)

    async def _skip(self, interaction: discord.Interaction) -> None:
        try:
            await self.player.skip()
            await interaction.response.send_message(_("Skipped."), ephemeral=True)
        except sonolink.QueueEmpty:
            await interaction.response.send_message(
                _("There is nothing left to skip to."), ephemeral=True
            )
        except Exception:
            log.exception("Controller skip failed")
            await self._report_failure(interaction)

    async def _volume_down(self, interaction: discord.Interaction) -> None:
        try:
            new_volume = max(0, self.player.volume - 10)
            await self.player.set_volume(new_volume)
            await self._rerender()
            await interaction.response.send_message(
                _("Volume set to {volume}%.").format(volume=new_volume), ephemeral=True
            )
        except Exception:
            log.exception("Controller volume-down failed")
            await self._report_failure(interaction)

    async def _volume_up(self, interaction: discord.Interaction) -> None:
        try:
            # Cap the button at 150 to spare ears, but never snap a higher
            # volume (set via the volume command, 0-1000) back down.
            current = self.player.volume
            new_volume = current if current >= 150 else min(150, current + 10)
            await self.player.set_volume(new_volume)
            await self._rerender()
            await interaction.response.send_message(
                _("Volume set to {volume}%.").format(volume=new_volume), ephemeral=True
            )
        except Exception:
            log.exception("Controller volume-up failed")
            await self._report_failure(interaction)

    async def _loop_toggle(self, interaction: discord.Interaction) -> None:
        try:
            if self.player.queue.mode == sonolink.QueueMode.LOOP_ALL:
                self.player.queue.mode = sonolink.QueueMode.NORMAL
                state = _("off")
            else:
                self.player.queue.mode = sonolink.QueueMode.LOOP_ALL
                state = _("on")
            await self._rerender()
            await interaction.response.send_message(
                _("Queue loop turned {state}.").format(state=state), ephemeral=True
            )
        except Exception:
            log.exception("Controller loop toggle failed")
            await self._report_failure(interaction)

    async def _shuffle(self, interaction: discord.Interaction) -> None:
        try:
            if len(self.player.queue.tracks) < 2:
                await interaction.response.send_message(
                    _("Add a few more tracks before shuffling."), ephemeral=True
                )
                return
            self.player.queue.shuffle()
            await self._rerender()
            await interaction.response.send_message(
                _("Shuffled the queue."), ephemeral=True
            )
        except Exception:
            log.exception("Controller shuffle failed")
            await self._report_failure(interaction)

    async def _show_queue(self, interaction: discord.Interaction) -> None:
        try:
            upcoming = self.player.queue.tracks
            if not upcoming:
                await interaction.response.send_message(
                    _("The queue is empty."), ephemeral=True
                )
                return
            lines = [
                _("`{index}.` {title} by `{author}`").format(
                    index=index, title=track.title, author=track.author
                )
                for index, track in enumerate(upcoming[:10], start=1)
            ]
            if len(upcoming) > 10:
                lines.append(
                    _("*...and {count} more.*").format(count=len(upcoming) - 10)
                )
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        except Exception:
            log.exception("Controller queue failed")
            await self._report_failure(interaction)

    async def _add_song(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.send_modal(AddSongModal(self.cog, self))
        except Exception:
            log.exception("Controller add-song failed")
            await self._report_failure(interaction)

    async def _favorite(self, interaction: discord.Interaction) -> None:
        try:
            track = self.player.current
            if track is None:
                await interaction.response.send_message(
                    _("Nothing is playing to favourite right now."), ephemeral=True
                )
                return
            added = await self.cog.add_favourite(interaction.user.id, track)
            if added:
                await interaction.response.send_message(
                    _("Added **{title}** to your favourites.").format(
                        title=track.title
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    _("**{title}** is already in your favourites.").format(
                        title=track.title
                    ),
                    ephemeral=True,
                )
        except Exception:
            log.exception("Controller favourite failed")
            await self._report_failure(interaction)

    async def _disconnect(self, interaction: discord.Interaction) -> None:
        try:
            self._disable_all()
            await interaction.response.edit_message(view=self)
            guild = getattr(self.player.channel, "guild", None)
            await self.player.disconnect()
            if guild is not None:
                await self.cog._clear(guild.id)
            self.stop()
        except Exception:
            log.exception("Controller disconnect failed")
            await self._report_failure(interaction)


class Music(commands.Cog):
    """Music playback commands powered by sonolink (Lavalink v4)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Guards the one-shot startup restore (on_ready can fire repeatedly).
        self._restored = False
        self._idle_check.start()

    def cog_unload(self) -> None:
        self._idle_check.cancel()

    def _client(self) -> typing.Optional[sonolink.Client]:
        return getattr(self.bot, "sl_client", None)

    def _nodes_available(self) -> bool:
        client = self._client()
        return bool(client and client.nodes)

    async def _require_player(self, ctx):
        """Return the connected player, or None after telling the user there is none."""
        player = ctx.voice_client
        if isinstance(player, sonolink.Player):
            return player
        await ctx.send(_("I'm not connected to a voice channel."))
        return None

    async def _search(
        self, query: str, *, source: TrackSourceType = SEARCH_SOURCE
    ) -> typing.Optional[typing.Any]:
        """Run a sonolink track search, returning the result (or None on node loss).

        Full URLs are resolved by Lavalink regardless of ``source``, so this is
        safe to call with a stored favourite's URI.
        """
        try:
            return await self.bot.sl_client.search_track(query, source=source)
        except RuntimeError:
            log.exception("Track search failed: no node available")
            return None

    # ------------------------------------------------------------------
    # Favourites (per-user playlist)
    # ------------------------------------------------------------------

    async def add_favourite(
        self, user_id: int, track: sonolink.models.Playable
    ) -> bool:
        """Store a track in a user's favourites, deduped on the track identifier.

        Returns True if a new row was inserted, False if it already existed.
        """
        query = """
            INSERT INTO music_favorites
                (user_id, identifier, title, author, uri, source_name)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (user_id, identifier) DO NOTHING
        """
        status = await self.bot.db_pool.execute(
            query,
            user_id,
            track.identifier,
            track.title,
            track.author,
            track.uri,
            track.source_name,
        )
        # asyncpg returns a status string like "INSERT 0 1" (or "... 0" on conflict).
        return status.rsplit(" ", 1)[-1] == "1"

    async def _fetch_favourites(self, user_id: int) -> list:
        """Return a user's favourites, newest first."""
        query = """
            SELECT identifier, title, author, uri, source_name
            FROM music_favorites
            WHERE user_id = $1
            ORDER BY added_at DESC
        """
        return await self.bot.db_pool.fetch(query, user_id)

    async def _send_controller(
        self,
        player: Player,
        track: typing.Optional[sonolink.models.Playable] = None,
    ) -> None:
        """Send a fresh now-playing controller in the player's home channel.

        ``track`` is the just-started track from a track_start event. It lets the
        controller render during the brief window before sonolink sets
        player.current (its REST update lands after Lavalink's websocket event) -
        the cold-restore race that otherwise posts no controller.
        """
        if player.home is None:
            return

        track = track if track is not None else player.current
        if track is None:
            return

        old = player.controller
        if old is not None:
            old.stop()
            if old.message is not None:
                try:
                    await old.message.delete()
                except discord.HTTPException:
                    log.exception("Failed to delete the previous controller message")

        # A LayoutView carries its own content; it must be sent with no embed.
        # Components V2 TextDisplay resolves mentions (unlike an embed), so
        # suppress pings or the DJ/requester would be notified on every repost.
        view = MusicController(self, player, track=track)
        try:
            message = await player.home.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            log.exception("Failed to send the now-playing controller")
            return
        view.message = message
        player.controller = view

    # ------------------------------------------------------------------
    # Restart persistence (snapshot live players, restore them on startup)
    # ------------------------------------------------------------------

    async def _snapshot(self, player: Player) -> None:
        """Persist a player's live state so a restart can resume it (best-effort)."""
        try:
            channel = player.channel
            current = player.current
            if channel is None or current is None or not current.encoded:
                return
            home = getattr(player, "home", None)
            dj = getattr(player, "dj", None)
            controller = getattr(player, "controller", None)
            controller_message_id = (
                controller.message.id
                if controller is not None and controller.message is not None
                else None
            )
            await music_state.save_state(
                self.bot.db_pool,
                guild_id=channel.guild.id,
                voice_channel_id=channel.id,
                home_channel_id=home.id if home is not None else None,
                dj_id=dj.id if dj is not None else None,
                volume=int(getattr(player, "volume", 100) or 100),
                loop_mode=_loop_to_int(player.queue.mode),
                position_ms=int(getattr(player, "position", 0) or 0),
                paused=bool(getattr(player, "paused", False)),
                current_track=current.encoded,
                queue=[
                    t.encoded
                    for t in player.queue.tracks
                    if getattr(t, "encoded", None)
                ],
                controller_message_id=controller_message_id,
            )
        except Exception:
            log.exception("Failed to snapshot player state")

    async def _clear(self, guild_id: int) -> None:
        """Forget a guild's persisted player state (best-effort)."""
        await music_state.clear_state(self.bot.db_pool, guild_id)

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_sonolink_track_start(
        self, player: Player, event: sonolink.gateway.TrackStartEvent
    ) -> None:
        log.debug(
            "Track start: %s (guild=%s)",
            event.track.title,
            player.channel.guild.id if player.channel else None,
        )
        await self._snapshot(player)
        # A cold restore posts the controller itself and arms this token with the
        # restored track's id; skip the one matching track_start so the two do
        # not race into a duplicate. Consume it so a later track (or a loop
        # repeat) still posts normally. Match by encoded id so an unrelated
        # track is never suppressed.
        suppress = getattr(player, "_suppress_controller_track", None)
        if suppress is not None and getattr(event.track, "encoded", None) == suppress:
            player._suppress_controller_track = None
            return
        if getattr(player, "home", None) is None:
            return
        # Pass the event's track so the controller renders even while play()'s
        # REST update is still in flight and player.current is not set yet.
        await self._send_controller(player, event.track)

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
                    _("There was a problem playing **{title}**, skipping it.").format(
                        title=event.track.title
                    )
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

        guild_id = channel.guild.id
        try:
            await player.disconnect()
        except Exception:
            log.exception("Failed to auto-disconnect from an empty channel")
        await self._clear(guild_id)

    # ------------------------------------------------------------------
    # Idle timeout
    # ------------------------------------------------------------------

    async def _teardown(self, player: Player) -> None:
        """Disconnect a player cleanly and drop its controller message."""
        controller = getattr(player, "controller", None)
        if controller is not None:
            controller.stop()
            if controller.message is not None:
                try:
                    await controller.message.delete()
                except discord.HTTPException:
                    log.exception("Failed to delete controller during idle teardown")
            player.controller = None
        guild = getattr(player.channel, "guild", None)
        try:
            await player.disconnect()
        except Exception:
            log.exception("Failed to disconnect an idle player")
        if guild is not None:
            await self._clear(guild.id)

    @staticmethod
    def _is_idle(player: Player) -> bool:
        """A player is idle when paused, empty, or alone in its voice channel."""
        if player.paused:
            return True
        if player.current is None and not player.queue.tracks:
            return True
        channel = player.channel
        if channel is not None and not any(not m.bot for m in channel.members):
            return True
        return False

    @tasks.loop(seconds=60)
    async def _idle_check(self) -> None:
        """Disconnect players that have stayed idle longer than ``IDLE_TIMEOUT``."""
        try:
            now = time.monotonic()
            for voice_client in list(self.bot.voice_clients):
                if not isinstance(voice_client, Player):
                    continue
                # Refresh the persisted snapshot: volume / loop / pause / position
                # drift between the event-driven snapshots.
                if voice_client.current is not None:
                    await self._snapshot(voice_client)
                if self._is_idle(voice_client):
                    if voice_client.idle_since is None:
                        voice_client.idle_since = now
                    elif now - voice_client.idle_since >= IDLE_TIMEOUT:
                        log.info(
                            "Disconnecting idle player in guild %s",
                            getattr(voice_client.channel, "guild", None),
                        )
                        await self._teardown(voice_client)
                else:
                    voice_client.idle_since = None
        except Exception:
            log.exception("idle-timeout loop iteration failed")

    @_idle_check.before_loop
    async def _before_idle_check(self) -> None:
        await self.bot.wait_until_ready()

    @_idle_check.error
    async def _idle_check_error(self, error: BaseException) -> None:
        log.exception("idle-timeout loop crashed; restarting", exc_info=error)
        self._idle_check.restart()

    # ------------------------------------------------------------------
    # Startup restore (survive a restart)
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Resume players left behind by a restart, exactly once.

        ``on_ready`` can fire repeatedly on reconnects, so a flag keeps this to a
        single run. It waits for a Lavalink node (decoding the stored tracks needs
        one) and must never crash startup, hence the broad guard.
        """
        if self._restored:
            return
        if not self._nodes_available():
            # Try again on the next on_ready, once the node has connected.
            return
        self._restored = True
        try:
            await self._restore_players()
        except Exception:
            log.exception("Music startup restore failed")

    async def _restore_players(self) -> None:
        """Rejoin and resume every recently-active player, bounded-concurrently.

        Restores run in parallel but capped at ``RESTORE_CONCURRENCY`` so a large
        fleet cannot fire a burst of voice reconnects at Discord's rate limits;
        each restore is isolated so one failure never sinks the others.
        """
        rows = await music_state.load_all_states(self.bot.db_pool)
        if not rows:
            return
        now = datetime.now(timezone.utc)

        # Which guilds is Lavalink still playing (its session was resumed)? Those
        # can be rebound gap-free; everything else gets a cold restore. Also
        # record the current session id for diagnostics (nothing reads it back -
        # see core.py). node.session_id raises RuntimeError, not AttributeError,
        # until connected, so check is_connected first.
        node = self.bot.sl_client.get_best_node()
        live_guilds = set()
        if node is not None:
            if node.is_connected:
                await music_state.save_session(
                    self.bot.db_pool, music_state.MUSIC_NODE_ID, node.session_id
                )
            try:
                for lavalink_player in await node.fetch_players():
                    gid = getattr(lavalink_player, "guild_id", None) or getattr(
                        lavalink_player, "guild", None
                    )
                    if gid is not None:
                        live_guilds.add(int(gid))
            except Exception:
                log.exception("Failed to fetch resumed Lavalink players")

        semaphore = asyncio.Semaphore(RESTORE_CONCURRENCY)

        async def _guarded(row) -> None:
            async with semaphore:
                try:
                    await self._restore_one(row, now, live_guilds)
                except Exception:
                    log.exception(
                        "Failed to restore music for guild %s", row["guild_id"]
                    )

        await asyncio.gather(*(_guarded(row) for row in rows))
        log.info("Music restore complete: processed %d player(s)", len(rows))

    async def _restore_one(self, row, now: datetime, live_guilds) -> None:
        """Resume a single guild's playback, or forget a stale/unusable row.

        Prefers a gap-free rebind when Lavalink still holds the player (resumed
        session); otherwise cold-restores by rejoining and replaying at the saved
        position. Either way it leaves exactly one fresh, working controller.
        """
        guild_id = row["guild_id"]

        age = (now - row["updated_at"]).total_seconds()
        if age > RESTORE_MAX_AGE or not row["current_track"]:
            await self._clear(guild_id)
            return

        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(row["voice_channel_id"]) if guild else None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await self._clear(guild_id)
            return

        # Nobody left to listen -> do not rejoin.
        if not any(not m.bot for m in channel.members):
            await self._clear(guild_id)
            return

        home_text = (
            guild.get_channel(row["home_channel_id"])
            if row["home_channel_id"]
            else None
        )
        home_text = home_text if isinstance(home_text, discord.abc.Messageable) else None
        # Fall back to the voice channel's own text chat when the saved home text
        # channel is missing / unresolved / was never persisted. A VoiceChannel
        # is Messageable, so the controller still lands somewhere sensible; a
        # None home used to skip BOTH the stale-delete and the controller post,
        # leaving the guild with only the dead pre-restart controller.
        home = home_text if home_text is not None else channel
        dj = guild.get_member(row["dj_id"]) if row["dj_id"] else None
        loop_mode = _int_to_loop(row["loop_mode"])

        # Drop the now-dead controller from before the restart, so its buttons
        # (bound to the old process) do not linger unresponsive. Only the saved
        # text channel can hold it (not the voice fallback), so key off home_text.
        stale_id = row["controller_message_id"]
        if stale_id and home_text is not None:
            try:
                await home_text.get_partial_message(stale_id).delete()
            except (discord.HTTPException, AttributeError):
                pass

        # Decode the exact tracks (no re-search) via the node.
        current = await self.bot.sl_client.decode_track(row["current_track"])
        if current is None:
            await self._clear(guild_id)
            return
        queue_tracks = []
        if row["queue"]:
            queue_tracks = await self.bot.sl_client.decode_tracks(list(row["queue"]))

        # --- Gap-free path: Lavalink still holds a usable player for this guild.
        if guild_id in live_guilds:
            node = self.bot.sl_client.get_best_node()
            resumed = node.get_player(guild_id) if node is not None else None
            if (
                isinstance(resumed, Player)
                and resumed.channel is not None
                and resumed.current is not None
            ):
                resumed.home = home
                resumed.dj = dj
                resumed.queue.mode = loop_mode
                if not resumed.queue.tracks:
                    for track in queue_tracks or []:
                        resumed.queue.put(track)
                await self._send_controller(resumed)
                await self._snapshot(resumed)
                log.info(
                    "Rebound live (gap-free) music player in guild %s", guild_id
                )
                return

        # --- Cold restore: rejoin and replay at the extrapolated position. The
        # track_start event posts a fresh controller, but a track restored in a
        # paused state (or a missed/late event) emits no track_start, so we also
        # post one explicitly below.
        player = guild.voice_client
        if not isinstance(player, Player):
            player = await channel.connect(cls=Player)
        player.home = home
        player.dj = dj
        player.queue.mode = loop_mode
        for track in queue_tracks or []:
            player.queue.put(track)

        position = music_state.extrapolate_position(
            row["position_ms"],
            row["updated_at"],
            now,
            paused=row["paused"],
            length_ms=getattr(current, "length", None),
        )
        # Post the controller ourselves after play() returns (below), and tell
        # this track's one track_start to skip its own post, so the two never
        # race into a duplicate. We must post explicitly because track_start may
        # not fire at all for a paused restore, which used to leave no working
        # controller.
        player._suppress_controller_track = getattr(current, "encoded", None)
        await player.play(
            current,
            start=position,
            paused=bool(row["paused"]),
            volume=int(row["volume"] or 100),
        )
        # player.current is set now that play() has returned, so this renders and
        # posts a working controller regardless of whether track_start fired.
        await self._send_controller(player)
        log.info(
            "Cold-restored music in guild %s at %dms (home_id=%s, home=%s, controller=%s)",
            guild_id,
            position,
            row["home_channel_id"],
            "text" if home_text is not None else "voice-fallback",
            "ok" if player.controller is not None else "missing",
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="play", aliases=["p"])
    @commands.guild_only()
    @app_commands.describe(query="A song name or URL to search for and play.")
    async def play(
        self, ctx: commands.Context, *, query: typing.Optional[str] = None
    ) -> None:
        """Play a track or playlist, or add it to the queue.

        Called with no query, this re-posts the now-playing controller at the
        bottom of the channel so it is not buried under newer messages.
        """
        if not query or not query.strip():
            player = ctx.voice_client
            if isinstance(player, sonolink.Player) and player.current:
                player.home = ctx.channel
                await self._send_controller(player)
                if ctx.interaction is not None:
                    await ctx.send(_("Here is the player."), ephemeral=True)
                return
            await ctx.send(
                _("Give me something to play, e.g. `play never gonna give you up`.")
            )
            return

        await ctx.defer()

        if not self._nodes_available():
            await ctx.send(
                _("Music is currently unavailable - no Lavalink node is connected.")
            )
            return

        player = ctx.voice_client
        if player is None:
            if not ctx.author.voice or not ctx.author.voice.channel:
                await ctx.send(_("You must be in a voice channel first."))
                return
            try:
                player = await ctx.author.voice.channel.connect(cls=Player)
            except discord.ClientException:
                log.exception("Failed to connect to the voice channel")
                await ctx.send(
                    _("I was unable to join your voice channel. Please try again.")
                )
                return
            player.dj = ctx.author
            player.home = ctx.channel

        if player.home is None:
            player.home = ctx.channel
        elif player.home != ctx.channel:
            await ctx.send(
                _("The player is already active in {channel}.").format(
                    channel=player.home.mention
                )
            )
            return

        try:
            result = await self.bot.sl_client.search_track(query, source=SEARCH_SOURCE)
        except RuntimeError:
            log.exception("Track search failed: no node available")
            await ctx.send(
                _("Music is currently unavailable - no Lavalink node is connected.")
            )
            return

        if result.is_error() or result.is_empty() or result.result is None:
            await ctx.send(_("Could not find any tracks for that query."))
            return

        data = result.result

        if isinstance(data, sonolink.models.Playlist):
            for track in data.tracks:
                track.extras.requester = ctx.author.id
            player.queue.put(data.tracks)
            await ctx.send(
                _(
                    "Added the playlist **{name}** ({count} tracks) to the queue."
                ).format(name=data.name, count=len(data.tracks))
            )
        else:
            track = data[0] if isinstance(data, list) else data
            track.extras.requester = ctx.author.id
            player.queue.put(track)
            await ctx.send(
                _("Added **{title}** by `{author}` to the queue.").format(
                    title=track.title, author=track.author
                )
            )

        if not player.current:
            await player.play(player.queue.get())
        await self._snapshot(player)

    @commands.hybrid_command(name="pause")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context) -> None:
        """Pause the current track."""
        player = await self._require_player(ctx)
        if player is None:
            return
        if player.paused:
            await ctx.send(_("The player is already paused."))
            return
        await player.pause()
        await ctx.send(_("Paused the player."))

    @commands.hybrid_command(name="resume")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context) -> None:
        """Resume the player if it is paused."""
        player = await self._require_player(ctx)
        if player is None:
            return
        if not player.paused:
            await ctx.send(_("The player is not paused."))
            return
        await player.resume()
        await ctx.send(_("Resumed the player."))

    @commands.hybrid_command(name="skip", aliases=["next"])
    @commands.guild_only()
    async def skip(self, ctx: commands.Context) -> None:
        """Skip the current track and play the next one."""
        player = await self._require_player(ctx)
        if player is None:
            return
        try:
            track = await player.skip()
        except sonolink.QueueEmpty:
            await ctx.send(_("There are no more tracks in the queue to skip to."))
            return
        if track:
            await ctx.send(
                _("Skipped to **{title}** by `{author}`.").format(
                    title=track.title, author=track.author
                )
            )
        else:
            await self._clear(ctx.guild.id)
            await ctx.send(_("Skipped. The queue is now empty."))

    @commands.hybrid_command(name="stop")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context) -> None:
        """Stop playback and clear the queue (stays connected)."""
        player = await self._require_player(ctx)
        if player is None:
            return
        await player.stop(clear_queue=True)
        await self._clear(ctx.guild.id)
        await ctx.send(_("Stopped playback and cleared the queue."))

    @commands.hybrid_command(name="volume", aliases=["vol"])
    @commands.guild_only()
    @app_commands.describe(value="Volume level between 0 and 1000 (100 is default).")
    async def volume(
        self, ctx: commands.Context, value: commands.Range[int, 0, 1000]
    ) -> None:
        """Set the player volume (0-1000)."""
        player = await self._require_player(ctx)
        if player is None:
            return
        await player.set_volume(value)
        await ctx.send(_("Set the volume to {volume}%.").format(volume=value))

    @commands.hybrid_command(name="shuffle", aliases=["mix"])
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context) -> None:
        """Shuffle the upcoming tracks in the queue."""
        player = await self._require_player(ctx)
        if player is None:
            return
        if len(player.queue.tracks) < 2:
            await ctx.send(_("Add a few more tracks to the queue before shuffling."))
            return
        player.queue.shuffle()
        await ctx.send(_("Shuffled the queue."))

    @commands.hybrid_command(name="loop")
    @commands.guild_only()
    @app_commands.describe(mode="One of: track, all, off.")
    async def loop(
        self,
        ctx: commands.Context,
        mode: typing.Literal["track", "all", "off"] = "track",
    ) -> None:
        """Set the loop mode for the queue."""
        player = await self._require_player(ctx)
        if player is None:
            return
        mapping = {
            "track": sonolink.QueueMode.LOOP,
            "all": sonolink.QueueMode.LOOP_ALL,
            "off": sonolink.QueueMode.NORMAL,
        }
        player.queue.mode = mapping[mode]
        await ctx.send(_("Loop mode set to `{mode}`.").format(mode=mode))

    @commands.hybrid_command(name="queue", aliases=["q", "que"])
    @commands.guild_only()
    async def queue(self, ctx: commands.Context) -> None:
        """Show the currently playing track and the next tracks in the queue."""
        player = await self._require_player(ctx)
        if player is None:
            return

        upcoming = player.queue.tracks
        if not upcoming and not player.current:
            await ctx.send(_("The queue is empty."))
            return

        lines: list[str] = []
        if player.current:
            lines.append(
                _("**Now Playing:** {title} by `{author}`\n").format(
                    title=player.current.title, author=player.current.author
                )
            )
        if upcoming:
            lines.append(_("**Up Next:**"))
            for index, track in enumerate(upcoming[:10], start=1):
                lines.append(
                    _("`{index}.` {title} by `{author}`").format(
                        index=index, title=track.title, author=track.author
                    )
                )
            if len(upcoming) > 10:
                lines.append(
                    _("*...and {count} more.*").format(count=len(upcoming) - 10)
                )

        embed = discord.Embed(
            title=_("Queue"),
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
            await ctx.send(_("Nothing is playing right now."))
            return
        player.home = ctx.channel
        await self._send_controller(player)
        if ctx.interaction is not None:
            await ctx.send(_("Here is the player."), ephemeral=True)

    @commands.hybrid_command(name="disconnect", aliases=["dc", "leave"])
    @commands.guild_only()
    async def disconnect(self, ctx: commands.Context) -> None:
        """Disconnect the player from the voice channel."""
        player = await self._require_player(ctx)
        if player is None:
            return
        await player.disconnect()
        await self._clear(ctx.guild.id)
        await ctx.send(_("Disconnected from the voice channel."))

    # ------------------------------------------------------------------
    # Favourites / playlist commands
    # ------------------------------------------------------------------

    async def _show_favourites(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        """Send a paginated, numbered list of a member's favourites (newest first)."""
        rows = await self._fetch_favourites(member.id)
        if not rows:
            if member == ctx.author:
                await ctx.send(_("You have no saved favourites yet."))
            else:
                await ctx.send(
                    _("{name} has no saved favourites yet.").format(
                        name=member.display_name
                    )
                )
            return

        lines: list[str] = []
        for index, row in enumerate(rows, start=1):
            title = row["title"] or _("Unknown title")
            author = row["author"] or _("Unknown artist")
            uri = row["uri"]
            label = f"[{title}]({uri})" if uri else title
            lines.append(
                _("`{index}.` {label} by `{author}`").format(
                    index=index, label=label, author=author
                )
            )

        embeds = paginate_lines(
            lines,
            title=_("{name}'s Favourites").format(name=member.display_name),
        )
        await Paginator(embeds, author_id=ctx.author.id).start(ctx)

    @commands.hybrid_group(
        name="playlist",
        aliases=["fav", "favorites", "pl"],
        fallback="list",
        invoke_without_command=True,
    )
    @commands.guild_only()
    @app_commands.describe(member="Whose favourites to show (defaults to you).")
    async def playlist(
        self, ctx: commands.Context, member: typing.Optional[discord.Member] = None
    ) -> None:
        """Show your saved favourite tracks, or another member's."""
        await self._show_favourites(ctx, member or ctx.author)

    @playlist.command(name="play")
    @commands.guild_only()
    async def playlist_play(self, ctx: commands.Context) -> None:
        """Queue every track in your favourites and start playing."""
        await ctx.defer()

        if not self._nodes_available():
            await ctx.send(
                _("Music is currently unavailable - no Lavalink node is connected.")
            )
            return

        rows = await self._fetch_favourites(ctx.author.id)
        if not rows:
            await ctx.send(_("You have no saved favourites to play."))
            return

        player = ctx.voice_client
        if player is None:
            if not ctx.author.voice or not ctx.author.voice.channel:
                await ctx.send(_("You must be in a voice channel first."))
                return
            try:
                player = await ctx.author.voice.channel.connect(cls=Player)
            except discord.ClientException:
                log.exception("Failed to connect to the voice channel")
                await ctx.send(
                    _("I was unable to join your voice channel. Please try again.")
                )
                return
            player.dj = ctx.author
            player.home = ctx.channel

        if player.home is None:
            player.home = ctx.channel

        queued = 0
        for row in rows:
            uri = row["uri"]
            if not uri:
                continue
            track = _first_track(await self._search(uri))
            if track is None:
                continue
            track.extras.requester = ctx.author.id
            player.queue.put(track)
            queued += 1

        if queued == 0:
            await ctx.send(_("None of your favourites could be loaded right now."))
            return

        if not player.current:
            await player.play(player.queue.get())
        await self._snapshot(player)

        await ctx.send(
            _("Queued {count} track(s) from your favourites.").format(count=queued)
        )

    @playlist.command(name="add")
    @commands.guild_only()
    @app_commands.describe(
        query="A song to search for and save (defaults to the current track)."
    )
    async def playlist_add(
        self, ctx: commands.Context, *, query: typing.Optional[str] = None
    ) -> None:
        """Save the current track, or a searched track, to your favourites."""
        if not query or not query.strip():
            player = ctx.voice_client
            if not isinstance(player, sonolink.Player) or not player.current:
                await ctx.send(
                    _("Nothing is playing - give me a song name or URL to save.")
                )
                return
            track = player.current
        else:
            if not self._nodes_available():
                await ctx.send(
                    _("Music is currently unavailable - no Lavalink node is connected.")
                )
                return
            await ctx.defer()
            track = _first_track(await self._search(query))
            if track is None:
                await ctx.send(_("Could not find any tracks for that query."))
                return

        added = await self.add_favourite(ctx.author.id, track)
        if added:
            await ctx.send(
                _("Added **{title}** by `{author}` to your favourites.").format(
                    title=track.title, author=track.author
                )
            )
        else:
            await ctx.send(
                _("**{title}** is already in your favourites.").format(
                    title=track.title
                )
            )

    @playlist.command(name="remove", aliases=["rm", "delete", "del"])
    @commands.guild_only()
    @app_commands.describe(index="The 1-based position of the favourite to remove.")
    async def playlist_remove(self, ctx: commands.Context, index: int) -> None:
        """Remove the favourite at the given position in your list."""
        rows = await self._fetch_favourites(ctx.author.id)
        if not rows:
            await ctx.send(_("You have no saved favourites to remove."))
            return
        if index < 1 or index > len(rows):
            await ctx.send(
                _("Pick a number between 1 and {max}.").format(max=len(rows))
            )
            return

        row = rows[index - 1]
        await self.bot.db_pool.execute(
            "DELETE FROM music_favorites WHERE user_id = $1 AND identifier = $2",
            ctx.author.id,
            row["identifier"],
        )
        await ctx.send(
            _("Removed **{title}** from your favourites.").format(
                title=row["title"] or _("Unknown title")
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))

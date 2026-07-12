"""Discord UI layer for the music cog.

This module owns every interactive surface the music feature presents: the
now-playing controller, the queue view, the vibe / join cards, and the modals,
selects, buttons and small render / gate helpers they use. The layering rule for
the ``cogs/music`` package:

* ``views.py`` (here) - the Discord UI. ``LayoutView`` / ``Modal`` / ``Select`` /
  ``Button`` subclasses and the helpers that build or gate them. It never talks
  to Lavalink, the database or the gateway itself; it drives playback only
  through the cog it is handed (duck-typed as ``self.cog``).
* ``music.py`` - the engine. The ``Music`` cog (commands, listeners, poller /
  idle loops, snapshot / restore), the sonolink ``Player`` subclass and autoplay
  handler, and the pure playback / queue helper functions.
* ``vibes.py`` - pure, side-effect-free domain logic (the genre catalog, mix
  detection, seek parsing, the bounded watch / played-track maps).

The UI classes reference the cog only through ``self.cog`` and string
annotations, and take the engine's pure helpers by import, so this module
depends on ``music.py`` but ``music.py`` does not need any UI name until run
time. ``music.py`` imports this module at its very bottom, after its own helpers
are defined, and it is always the package's import entry point (the loaded
extension), so the import cycle resolves music-first without a partial-import
error. ``sonolink`` is imported the same lazy-safe way as in ``music.py`` so the
stub-sonolink dev box keeps importing this module.
"""


import logging
import time
import typing

import discord
import sonolink
import sonolink.models

from cogs.music import effects, vibes, voteskip
from cogs.music.music import (
    MAX_FAVOURITES,
    Player,
    _autoplay_on,
    _first_track,
    _set_autoplay,
    can_go_previous,
    can_skip,
    effect_select_options,
    format_duration,
    is_autoplay_track,
    purge_queue_lanes,
    queue_page,
    queued_track_count,
    station_select_options,
)
from tools import interactions
from tools.config_loader import config_loader
from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorLayoutView, LocaleModal

if typing.TYPE_CHECKING:  # the cog type is used only in string annotations here
    from cogs.music.music import Music

log = logging.getLogger(__name__)


E_VOICE = config_loader.getstr("Emojis", "voice")


# Short per-user debounce on the station select: a zap runs two searches and
# replaces playback, so a double-click would fire two competing replace
# sequences. Touched only on an allowed click, so a burst collapses to one.
_STATION_DEBOUNCE = Cooldowns(2.0)


async def _ensure_in_voice(
    player: "Player", interaction: discord.Interaction
) -> bool:
    """Reject anyone not currently in ``player``'s voice channel.

    Shared by the now-playing controller and the queue view: both are room
    surfaces (anyone in the voice channel may drive them), not author-gated
    panels. Sends the refusal ephemerally and returns ``False`` when the player
    is gone or the clicker is not in its channel.
    """
    channel = getattr(player, "channel", None)
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


async def _ensure_can_control(
    cog: "Music", player: "Player", interaction: discord.Interaction
) -> bool:
    """DJ/mod gate for a playback-control surface; same-voice is checked separately.

    Returns True when the clicker may drive playback: the session DJ, a
    Manage-Server member, or anyone when the session has no DJ (the radio/vote
    precedent that "no DJ -> no gate"). Otherwise sends the ephemeral refusal and
    returns False. Reuses the cog's single :meth:`Music._can_control` predicate so
    a button gate can never drift from its mirror command, and the message matches
    the ``control=True`` command refusal verbatim.
    """
    if cog._can_control(player, interaction.user):
        return True
    dj = getattr(player, "dj", None)
    # _can_control returns True when the session has no DJ, so dj is a real member
    # on this deny path; guard anyway so a racing clear never crashes .mention.
    if dj is None:
        return True
    await interaction.response.send_message(
        _("Only the DJ ({dj}) or a moderator can control playback.").format(
            dj=dj.mention
        ),
        ephemeral=True,
    )
    return False


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

    def __init__(
        self, cog: "Music", owner: 'typing.Union["MusicController", "QueueView"]'
    ) -> None:
        super().__init__()
        self.cog = cog
        # The surface that opened this modal: the now-playing controller or the
        # queue view. Both expose ``.player`` and a no-arg async ``_rerender()``
        # that edits their own bound message, so the add flow works from either.
        self.owner = owner

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            player = self.owner.player
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
            # An explicit add turns a radio session into a normal one: the station
            # select disappears on the rerender below.
            player.radio_genre = None
            if not player.current:
                await player.play(player.queue.get())
            await self.cog._snapshot(player)

            await interaction.response.send_message(
                _("Queued **{title}**.").format(title=track.title), ephemeral=True
            )
            await self.owner._rerender()
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
        # Identifier of the track this panel's message currently RENDERS, set by
        # _build. _send_controller compares an incoming track_start against THIS
        # (not player.current, which has already advanced to the new track by the
        # time its event lands) to tell a reconnect re-fire from a real change.
        self._rendered_id: typing.Optional[str] = None
        # When this view was created; _send_controller only keeps a very recent
        # controller on a same-track re-fire (a reconnect re-fire arrives within
        # seconds), so a later same-track start (loop mode) still re-posts.
        self.created_at = time.monotonic()
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
        # Record what this render actually shows so _send_controller can tell a
        # same-track re-fire from a genuine change without consulting the live
        # player.current (which advances ahead of the track_start event).
        self._rendered_id = getattr(track, "identifier", None)
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
        # Recommendation notice: only when THIS track came from autoplay, so a
        # user-queued track never claims to be a pick. sonolink stamps the flag on
        # every autoplay-sourced track (see is_autoplay_track).
        if is_autoplay_track(track):
            container.add_item(
                discord.ui.TextDisplay(
                    _(
                        "✨ I'm keeping the music going with recommendations based on "
                        "this session's listening. Tap Autoplay below to turn it off."
                    )
                )
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
        station = self._station_genre()
        if station is not None:
            meta_lines.append(
                _("**Station:** {emoji} {label}").format(
                    emoji=station.emoji, label=station.label
                )
            )
        preset = self._effect_preset()
        if preset is not None:
            meta_lines.append(
                _("**Effect:** {emoji} {label}").format(
                    emoji=preset.emoji, label=preset.label
                )
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

        # Third row: Back then the Autoplay toggle. Back belongs beside Skip, but
        # the first row is already full at five buttons, so the previous-track
        # control lands here. Autoplay is green when armed, grey when off, so the
        # button shows the current session state at a glance - the house style.
        autoplay_on = _autoplay_on(self.player)
        container.add_item(
            discord.ui.ActionRow(
                self._make_button(
                    self._back,
                    label=_("Back"),
                    emoji="⏮️",
                    style=discord.ButtonStyle.secondary,
                ),
                self._make_button(
                    self._autoplay_toggle,
                    label=_("Autoplay"),
                    emoji="✨",
                    style=(
                        discord.ButtonStyle.success
                        if autoplay_on
                        else discord.ButtonStyle.secondary
                    ),
                ),
                self._make_button(
                    self._effects,
                    label=_("Effects"),
                    emoji="🎛️",
                    style=discord.ButtonStyle.secondary,
                ),
            )
        )

        # Radio mode only: the DJ-gated station picker. Shown solely while a
        # radio session is live (a genre key is set); it disappears the moment a
        # user plays an explicit query and the session turns normal.
        if station is not None:
            container.add_item(
                discord.ui.ActionRow(_StationSelect(self, station.key))
            )

        container.add_item(
            discord.ui.TextDisplay(_("-# Use the buttons to control playback"))
        )

        self.add_item(container)

    def _station_genre(self) -> typing.Optional["vibes.Genre"]:
        """The active station's Genre, or None outside radio mode.

        Guards against a stale key by validating it against the catalog, so a
        removed genre simply drops the station UI rather than rendering blanks.
        """
        key = getattr(self.player, "radio_genre", None)
        return vibes.GENRES_BY_KEY.get(key) if key else None

    def _effect_preset(self) -> typing.Optional["effects.Preset"]:
        """The active audio-effect preset, or None when no effect is set.

        Off (or an unknown/stale key) renders no line - only a real, active
        effect earns the controller's "Effect:" row. Guards against a retired key
        the same way :meth:`_station_genre` guards a removed genre.
        """
        key = getattr(self.player, "effect_preset", None)
        if not key or key == effects.OFF_KEY:
            return None
        return effects.PRESETS_BY_KEY.get(key)

    def _disable_all(self) -> None:
        """Disable every button in the layout (walks nested ActionRows)."""
        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow members currently in the player's voice channel."""
        return await _ensure_in_voice(self.player, interaction)

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

    async def _rerender_for_track(
        self, track: typing.Optional[sonolink.models.Playable]
    ) -> bool:
        """Re-render the panel in place for a just-started track; True on success.

        Unlike :meth:`_rerender` (a button-driven refresh that stands down when
        nothing is playing), this updates the fallback track FIRST so the render
        is correct even while player.current is still catching up - the
        track_start event beats play()'s REST update, so current may briefly be
        the OLD track or None, and rendering off the stale fallback is exactly the
        trap that keeps the panel on the previous track. Returns False when there
        is no bound message or the edit failed (the message was deleted out of
        band), so the caller can fall back to a fresh repost.
        """
        if self.message is None:
            return False
        self._track = track
        self._build()
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            log.exception("Failed to re-render the controller for a new track")
            return False
        # This panel now shows a NEW track: restart the re-fire keep window from
        # here so a reconnect re-fire of THIS track (arriving seconds later) is
        # still kept without flicker, exactly as it would be for a freshly posted
        # panel. Without this the window kept measuring from the first track's
        # post, so a reconnect after any track change wrongly reposted.
        self.created_at = time.monotonic()
        return True

    async def _pause_resume(self, interaction: discord.Interaction) -> None:
        try:
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
            if self.player.paused:
                await self.player.resume()
                message = _("Resumed.")
            else:
                await self.player.pause()
                message = _("Paused.")
            # Snapshot right away: the persisted paused flag drives the restore
            # position maths, and waiting for the 60s idle tick would let a
            # restart resume playing (at a wrongly advanced position) in a
            # channel everyone expected to stay silent.
            await self.cog._snapshot(self.player)
            await self._rerender()
            await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            log.exception("Controller pause/resume failed")
            await self._report_failure(interaction)

    async def _skip(self, interaction: discord.Interaction) -> None:
        try:
            # Scaled vote-skip (lot P6): a non-exempt member in a room of more than
            # two humans opens (or joins) a public vote; the DJ, Manage-Server
            # members and tiny rooms keep the instant skip below, byte-identical.
            decision = await self.cog._request_skip(
                self.player, interaction.user, interaction.channel
            )
            if decision != voteskip.SKIP_INSTANT:
                await interaction.response.send_message(
                    voteskip.skip_ack(decision), ephemeral=True
                )
                return
            # Pre-check: sonolink stops playback BEFORE raising QueueEmpty, so a
            # skip with nowhere to land must be refused up front, not caught.
            if not can_skip(self.player):
                await interaction.response.send_message(
                    _("There is nothing left to skip to."), ephemeral=True
                )
                return
            await self.player.skip()
            await interaction.response.send_message(_("Skipped."), ephemeral=True)
        except sonolink.QueueEmpty:
            await interaction.response.send_message(
                _("There is nothing left to skip to."), ephemeral=True
            )
        except Exception:
            log.exception("Controller skip failed")
            await self._report_failure(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        try:
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
            # Pre-check so the "nothing before this" case gets its own message,
            # mirroring the skip button; the shared cog seam does the replay so
            # both surfaces run one implementation. No _rerender here: the direct
            # play() fires a track_start that refreshes the controller through the
            # normal event path, exactly like skip.
            if not can_go_previous(self.player):
                await interaction.response.send_message(
                    _("There's no previous track to go back to."), ephemeral=True
                )
                return
            track = await self.cog._play_previous(self.player)
            if track is None:
                await interaction.response.send_message(
                    _("I can't go back - the previous track is no longer available."),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                _("Went back to **{title}** by `{author}`.").format(
                    title=track.title, author=track.author
                ),
                ephemeral=True,
            )
        except Exception:
            log.exception("Controller back failed")
            await self._report_failure(interaction)

    async def _volume_down(self, interaction: discord.Interaction) -> None:
        try:
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
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
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
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
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
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

    async def _autoplay_toggle(self, interaction: discord.Interaction) -> None:
        try:
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
            enabled = not _autoplay_on(self.player)
            _set_autoplay(self.player, enabled)
            # Persist right away so a restart restores the same autoplay mode,
            # mirroring how pause/resume snapshot their flag immediately.
            await self.cog._snapshot(self.player)
            await self._rerender()
            if enabled:
                message = _(
                    "Autoplay is on. I'll keep the music going with recommendations "
                    "when the queue runs out."
                )
            else:
                message = _(
                    "Autoplay is off. Playback will stop once the queue is empty."
                )
            await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            log.exception("Controller autoplay toggle failed")
            await self._report_failure(interaction)

    async def _shuffle(self, interaction: discord.Interaction) -> None:
        try:
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
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
            view = QueueView(self.cog, self.player)
            await interaction.response.send_message(
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # Bind the sent (ephemeral) message so an out-of-band add-track
            # refresh has something to edit; button clicks edit in place via the
            # component interaction and do not depend on this.
            view.message = await interaction.original_response()
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
            result = await self.cog.add_favourite(interaction.user.id, track)
            if result == "added":
                message = _("Added **{title}** to your favourites.").format(
                    title=track.title
                )
            elif result == "full":
                message = _(
                    "Your favourites are full (max {max}). Remove some first."
                ).format(max=MAX_FAVOURITES)
            else:
                message = _("**{title}** is already in your favourites.").format(
                    title=track.title
                )
            await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            log.exception("Controller favourite failed")
            await self._report_failure(interaction)

    async def _disconnect(self, interaction: discord.Interaction) -> None:
        try:
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
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

    async def _effects(self, interaction: discord.Interaction) -> None:
        """Open the ephemeral effect picker (keeps the controller budget flat).

        A private one-select card is sent to the clicker instead of adding a
        select row to the shared controller, so the panel stays identical for
        everyone. The picker's own callback re-checks same-voice and runs the
        cog's quota-gated apply seam.
        """
        try:
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
            await interaction.response.send_message(
                view=EffectsView(self.cog, self.player),
                ephemeral=True,
            )
        except Exception:
            log.exception("Controller effects launch failed")
            await self._report_failure(interaction)

    async def _change_station(self, interaction: discord.Interaction, key: str) -> None:
        """Zap the station to ``key``: DJ-gated, replaces playback with the genre.

        The base same-voice ``interaction_check`` has already run. This adds a
        short debounce (a zap is expensive) and the DJ gate - only the session
        DJ or a member with Manage Server may change the station - then runs the
        shared replace sequence (:meth:`Music._apply_genre` with ``replace=True``)
        and confirms ephemerally. When no DJ is assigned (e.g. a restored session
        whose DJ left the guild) the gate is open to the channel's listeners.
        """
        try:
            if not await _check_station_debounce(interaction):
                return

            # Same single DJ/mod decision as every other control (no-DJ opens the
            # gate); the station keeps its own specific refusal wording.
            user = interaction.user
            if not self.cog._can_control(self.player, user):
                dj = self.player.dj
                if dj is not None:
                    await interaction.response.send_message(
                        _("Only the DJ ({dj}) can change the station.").format(
                            dj=dj.mention
                        ),
                        ephemeral=True,
                    )
                    return

            genre = vibes.GENRES_BY_KEY.get(key)
            if genre is None:
                await interaction.response.send_message(
                    _("That vibe isn't available right now."), ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True)
            _tier, tracks = await self.cog._apply_genre(
                self.player, genre, user.id, replace=True
            )
            if not tracks:
                await interaction.followup.send(
                    _("I couldn't find any {genre} tracks right now.").format(
                        genre=genre.label
                    ),
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                _("Switched to the {genre} station ({count} track(s)).").format(
                    genre=genre.label, count=len(tracks)
                ),
                ephemeral=True,
            )
        except Exception:
            log.exception("Controller station change failed")
            await self._report_failure(interaction)


class _EffectsSelect(discord.ui.Select):
    """The audio-effect picker shown in the controller's ephemeral effects card.

    One option per preset in catalog order; the active preset (or Off when none
    is set) is preselected. Choosing one re-checks same-voice, then delegates to
    the cog's quota-gated apply seam and confirms ephemerally.
    """

    def __init__(self, cog: "Music", player: Player) -> None:
        self._cog = cog
        self._player = player
        super().__init__(
            placeholder=_("Pick an effect..."),
            min_values=1,
            max_values=1,
            options=effect_select_options(getattr(player, "effect_preset", None)),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if not await _ensure_in_voice(self._player, interaction):
                return
            if not await _ensure_can_control(self._cog, self._player, interaction):
                return
            guild = getattr(self._player.channel, "guild", None)
            if guild is None:
                await interaction.response.send_message(
                    _("The player is no longer active."), ephemeral=True
                )
                return
            message = await self._cog._run_effect_change(
                self._player, guild.id, interaction.user, self.values[0]
            )
            await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            log.exception("Effect select failed")
            await interactions.notify_failure(interaction)


class EffectsView(discord.ui.View):
    """Ephemeral one-select card for choosing an audio effect.

    Sent privately from the controller's Effects button, so it needs no author
    gate (only the clicker sees it) and no persistence (it lives for the
    interaction). The select re-checks same-voice before applying.
    """

    def __init__(self, cog: "Music", player: Player, *, timeout: float = 120) -> None:
        super().__init__(timeout=timeout)
        self.add_item(_EffectsSelect(cog, player))


class _ModalPlayContext:
    """A minimal ``commands.Context`` stand-in for the vibe search modal.

    It exposes exactly the attributes :meth:`Music._play_query` reads - ``author``,
    ``channel``, ``voice_client`` and an awaitable ``send`` - so a modal submit runs
    the byte-identical ``/play <query>`` body without a real Context (a modal
    submit interaction cannot build one). The modal defers ephemerally before
    handing this over, so ``send`` posts ephemeral followups and the search
    feedback stays self-contained.
    """

    def __init__(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        self.author = interaction.user
        self.channel = interaction.channel

    @property
    def voice_client(self) -> typing.Optional[sonolink.Player]:
        guild = self._interaction.guild
        return guild.voice_client if guild is not None else None

    async def send(self, content: typing.Optional[str] = None, **kwargs: typing.Any) -> None:
        kwargs.setdefault("ephemeral", True)
        await self._interaction.followup.send(content, **kwargs)


class _GenreSelect(discord.ui.Select):
    """The eight-genre picker; choosing one starts or extends that genre's mix."""

    def __init__(self, card: "VibeCard") -> None:
        self._card = card
        options = [
            discord.SelectOption(
                label=genre.label,
                value=genre.key,
                description=_(genre.description),
                emoji=genre.emoji,
            )
            for genre in vibes.GENRE_CATALOG
        ]
        super().__init__(
            placeholder=_("Pick a vibe..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self._card._pick_genre(interaction, self.values[0])
        except Exception:
            log.exception("Vibe card genre select failed")
            await interactions.notify_failure(interaction)


async def _check_station_debounce(interaction: discord.Interaction) -> bool:
    """Gate a station-select click behind the per-user debounce.

    Returns True when the click may proceed; otherwise sends an ephemeral 'slow
    down' and returns False. The window is touched only on an allowed click, so a
    burst of denied clicks never extends it - the same shape as the AniList feed's
    action debounce.
    """
    if _STATION_DEBOUNCE.is_active(interaction.user.id):
        await interaction.response.send_message(
            _("You are changing the station too fast - give it a moment."),
            ephemeral=True,
        )
        return False
    _STATION_DEBOUNCE.touch(interaction.user.id)
    return True


class QueueView(discord.ui.LayoutView):
    """The upcoming queue as a paginated Components V2 layout.

    A single accent :class:`~discord.ui.Container` in the controller's house
    style: a "Queue" heading, the now-playing line, the upcoming tracks paged
    ten at a time, and one action row (Prev / Next / Add track / Clear queue).
    Like the controller it is a room surface, not an author-gated panel - anyone
    in the player's voice channel may drive it (see :func:`_ensure_in_voice`).

    Every render re-reads ``player.queue.tracks`` live, so the view never shows
    stale state after an add or a clear, and the page index is re-clamped by
    :func:`queue_page` whenever the queue shrinks under the viewer.
    """

    def __init__(self, cog: "Music", player: Player, *, timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.player = player
        self.page = 0
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
        """(Re)assemble the layout from the player's live queue state."""
        self.clear_items()
        container = discord.ui.Container(accent_colour=random_colour())

        container.add_item(discord.ui.TextDisplay(_("### 🎶 Queue")))

        current = self.player.current
        if current is not None:
            container.add_item(
                discord.ui.TextDisplay(
                    _("**Now Playing:** {title} by `{author}` `{duration}`").format(
                        title=current.title[:120],
                        author=current.author,
                        duration=format_duration(current),
                    )
                )
            )
        else:
            container.add_item(
                discord.ui.TextDisplay(_("**Now Playing:** Nothing right now."))
            )
        container.add_item(discord.ui.Separator())

        upcoming = self.player.queue.tracks
        total = len(upcoming)
        self.page, total_pages, start, end = queue_page(total, self.page)

        if total == 0:
            container.add_item(
                discord.ui.TextDisplay(
                    _("The queue is empty. Add a track to keep the music going!")
                )
            )
        else:
            lines = [
                _("{index}. {title} by {author} ({duration})").format(
                    index=index,
                    title=track.title[:60],
                    author=track.author,
                    duration=format_duration(track),
                )
                for index, track in enumerate(upcoming[start:end], start=start + 1)
            ]
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))
            container.add_item(
                discord.ui.TextDisplay(
                    _("-# Page {page}/{pages} - {count} track(s)").format(
                        page=self.page + 1, pages=total_pages, count=total
                    )
                )
            )
        container.add_item(discord.ui.Separator())

        single_page = total_pages <= 1
        has_queued = queued_track_count(self.player.queue) > 0
        container.add_item(
            discord.ui.ActionRow(
                self._make_button(
                    self._prev,
                    label=_("Prev"),
                    emoji="◀️",
                    style=discord.ButtonStyle.secondary,
                    disabled=single_page or self.page <= 0,
                ),
                self._make_button(
                    self._next,
                    label=_("Next"),
                    emoji="▶️",
                    style=discord.ButtonStyle.secondary,
                    disabled=single_page or self.page >= total_pages - 1,
                ),
                self._make_button(
                    self._add,
                    label=_("Add track"),
                    emoji="➕",
                    style=discord.ButtonStyle.success,
                ),
                self._make_button(
                    self._clear,
                    label=_("Clear queue"),
                    emoji="🗑️",
                    style=discord.ButtonStyle.danger,
                    disabled=not has_queued,
                ),
            )
        )

        self.add_item(container)

    def _disable_all(self) -> None:
        """Disable every button in the layout (walks nested ActionRows)."""
        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow members currently in the player's voice channel."""
        return await _ensure_in_voice(self.player, interaction)

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                log.exception("Failed to disable the queue view on timeout")

    async def _report_failure(self, interaction: discord.Interaction) -> None:
        await interactions.notify_failure(
            interaction, _("Something went wrong handling that action.")
        )

    async def _rerender(self) -> None:
        """Re-render in place off the live queue (used by the add-track modal)."""
        if self.message is None:
            return
        self._build()
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            log.exception("Failed to refresh the queue view")

    async def _rerender_from(self, interaction: discord.Interaction) -> None:
        """Re-render in place, editing the message the click landed on."""
        self._build()
        await interaction.response.edit_message(view=self)

    async def _prev(self, interaction: discord.Interaction) -> None:
        try:
            self.page -= 1
            await self._rerender_from(interaction)
        except Exception:
            log.exception("Queue view prev failed")
            await self._report_failure(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        try:
            self.page += 1
            await self._rerender_from(interaction)
        except Exception:
            log.exception("Queue view next failed")
            await self._report_failure(interaction)

    async def _add(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.send_modal(AddSongModal(self.cog, self))
        except Exception:
            log.exception("Queue view add-track failed")
            await self._report_failure(interaction)

    async def _clear(self, interaction: discord.Interaction) -> None:
        try:
            # Destructive: DJ-gated to match /clearqueue and the controller
            # controls (Prev/Next/Add stay open to the room; only the purge locks).
            if not await _ensure_can_control(self.cog, self.player, interaction):
                return
            # Same semantics as the /clearqueue command: count both lanes, purge
            # both lanes, persist, then confirm - reusing its exact wordings.
            count = queued_track_count(self.player.queue)
            if count == 0:
                await interaction.response.send_message(
                    _("The queue is already empty."), ephemeral=True
                )
                return
            purge_queue_lanes(self.player.queue)
            await self.cog._snapshot(self.player)
            # Refresh this surface off the now-empty queue, then confirm.
            self._build()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                _("Cleared {count} track(s) from the queue.").format(count=count),
                ephemeral=True,
            )
        except Exception:
            log.exception("Queue view clear failed")
            await self._report_failure(interaction)


class _StationSelect(discord.ui.Select):
    """The DJ-gated station picker shown on a radio-mode controller.

    Same eight genres as the vibe card; the live station is preselected. Choosing
    one delegates to the controller's zap handler, which debounces, DJ-gates and
    replaces playback with the new genre.
    """

    def __init__(self, controller: "MusicController", current_key: str) -> None:
        self._controller = controller
        super().__init__(
            placeholder=_("Change station..."),
            min_values=1,
            max_values=1,
            options=station_select_options(current_key),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self._controller._change_station(interaction, self.values[0])
        except Exception:
            log.exception("Station select failed")
            await interactions.notify_failure(interaction)


class _VibeSearchModal(LocaleModal):
    """Free-text search from the vibe card, routed through the /play <query> path."""

    def __init__(self, cog: "Music", author_id: int) -> None:
        super().__init__(title=_("Search for music"))
        self.cog = cog
        self.author_id = author_id
        self.query_field = discord.ui.TextInput(
            label=_("Song or URL"),
            placeholder=_("A song name to search, or a full URL"),
            style=discord.TextStyle.short,
            required=True,
            max_length=400,
        )
        self.add_item(self.query_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            query = self.query_field.value.strip()
            if not query:
                await interaction.response.send_message(
                    _("Give me a song name or URL to add."), ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            await self.cog._play_query(_ModalPlayContext(interaction), query)
        except Exception:
            log.exception("Vibe search modal submit failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong searching for that.")
            )


class _VibeSearchButton(discord.ui.Button):
    """Open the free-text search modal from the vibe card."""

    def __init__(self, card: "VibeCard") -> None:
        self._card = card
        super().__init__(
            label=_("Search for music instead"),
            style=discord.ButtonStyle.secondary,
            emoji="\N{RIGHT-POINTING MAGNIFYING GLASS}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.send_modal(
                _VibeSearchModal(self._card.cog, self._card.author_id)
            )
        except Exception:
            log.exception("Vibe card search launch failed")
            await interactions.notify_failure(interaction)


class VibeCard(AuthorLayoutView):
    """The "choose your vibe" card: a genre picker plus a free-search escape hatch.

    A single accent :class:`~discord.ui.Container` in the music controller's house
    style - a heading, a genre :class:`_GenreSelect`, a separator and a
    :class:`_VibeSearchButton`. Author-gated through
    :class:`~tools.views.AuthorLayoutView`. Picking a genre delegates to the cog's
    playback seams; the search button opens a modal routed through the exact
    ``/play <query>`` path.
    """

    def __init__(self, cog: "Music", author_id: int, *, timeout: float = 180) -> None:
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self._build()

    def _build(self) -> None:
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay(_("## 🎧 Choose your vibe")))
        container.add_item(
            discord.ui.TextDisplay(
                _("Pick a genre and I'll spin up a mix, or search for a track.")
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_GenreSelect(self)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(_VibeSearchButton(self)))
        container.add_item(
            discord.ui.TextDisplay(_("-# Only you can use this menu."))
        )
        self.add_item(container)

    async def _pick_genre(self, interaction: discord.Interaction, key: str) -> None:
        genre = vibes.GENRES_BY_KEY.get(key)
        if genre is None:
            await interaction.response.send_message(
                _("That vibe isn't available right now."), ephemeral=True
            )
            return
        await self.cog._start_genre(interaction, genre)


class JoinVoiceCard(AuthorLayoutView):
    """The auto-updating "join a voice channel" welcome card.

    Shown on a bare /play when the invoker is not in voice. It lists up to five
    voice channels they may connect to; a cog-side voice-state watch edits this
    same message into the vibe card the instant they join (see
    :meth:`Music._fire_voice_watch`). It carries no interactive components - the
    author gate is inert - but it keeps AuthorLayoutView's timeout cleanup so the
    card retires gracefully once the join window (``WATCH_TTL``) elapses.
    """

    def __init__(
        self,
        author_id: int,
        channels: typing.Sequence[discord.VoiceChannel],
        *,
        timeout: float = vibes.WATCH_TTL,
    ) -> None:
        super().__init__(author_id, timeout=timeout)
        self._build(channels)

    def _build(self, channels: typing.Sequence[discord.VoiceChannel]) -> None:
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay(_("## 👋 Welcome")))
        container.add_item(
            discord.ui.TextDisplay(
                _("I'm all set to bring the music - let's get you into a room first.")
            )
        )
        container.add_item(discord.ui.Separator())
        if channels:
            lines = [_("To get started, join a voice channel:")]
            lines.extend(f"- {channel.mention}" for channel in channels)
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        else:
            container.add_item(
                discord.ui.TextDisplay(
                    _("I couldn't find a voice channel here that you can join.")
                )
            )
        container.add_item(
            discord.ui.TextDisplay(
                _("-# Once you join, this message will automatically update.")
            )
        )
        self.add_item(container)

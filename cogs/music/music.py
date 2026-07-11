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

from cogs.music import vibes
from tools import interactions, music_state, settings
from tools.config_loader import config_loader
from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import _
from tools.paginator import Paginator, paginate_lines
from tools.views import AuthorLayoutView, LocaleModal

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

# Cap a user's saved favourites so the table cannot grow without bound.
MAX_FAVOURITES = 100

# Per-user preference key (JSONB in user_settings, owned by the UserSettings cog)
# that seeds a NEW session's autoplay mode. Kept in sync with the matching
# Preference in cogs/community/usersettings.py - both must use this exact string.
AUTOPLAY_PREF_KEY = "music_autoplay"

# Most consecutive suspected-mix autoplay tracks the controller will auto-skip
# before it gives up and lets one play, so a run of nothing-but-mixes can never
# spin forever skipping. The counter resets the instant any track plays normally.
ANTI_MIX_SKIP_CAP = 3

# Short per-user debounce on the station select: a zap runs two searches and
# replaces playback, so a double-click would fire two competing replace
# sequences. Touched only on an allowed click, so a burst collapses to one.
_STATION_DEBOUNCE = Cooldowns(2.0)


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
        # Radio-mode session state. ``radio_genre`` is the active station's genre
        # key (None outside radio mode); every genre pick sets it and playing an
        # explicit query clears it. ``played_ids`` is the bounded set the refill
        # excludes so a station never loops the same tracks. The two private
        # counters guard the refill (one in-flight at a time) and the anti-mix
        # auto-skip streak.
        self.radio_genre: typing.Optional[str] = None
        self.played_ids = vibes.PlayedTracks()
        self._radio_refilling = False
        self._automix_skips = 0


def format_clock(total_ms: int) -> str:
    """Render a millisecond duration/position as ``mm:ss`` (or ``h:mm:ss``).

    Hours only appear once the value crosses an hour, so a short track reads
    ``03:42`` while a long one reads ``1:05:09``. Negative input is floored to
    zero. Pure - shared by :func:`format_duration` and the /seek confirmation so
    a track's length and a seek target always render identically.
    """
    total_seconds = max(total_ms, 0) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_duration(track: sonolink.models.Playable) -> str:
    """Return a track's duration as ``mm:ss``/``h:mm:ss`` (or ``LIVE`` for streams)."""
    if track.is_stream:
        return "LIVE"
    return format_clock(track.length)


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


def _normalize_result_tracks(
    result: typing.Optional[typing.Any],
) -> typing.List[sonolink.models.Playable]:
    """Flatten a search result's Playlist / list / single-track shapes to a list.

    Returns the raw candidate tracks (no filtering) or ``[]`` when the result is
    absent, errored or empty. Pure - the shared normaliser behind
    :func:`select_playable` and the genre ladder.
    """
    if (
        result is None
        or result.is_error()
        or result.is_empty()
        or result.result is None
    ):
        return []
    data = result.result
    if isinstance(data, sonolink.models.Playlist):
        return list(data.tracks)
    if isinstance(data, list):
        return list(data)
    return [data]


def filter_tracks(
    tracks: typing.Sequence[sonolink.models.Playable],
    limit: int,
    *,
    seen_ids: typing.Optional[typing.Iterable[str]] = None,
    max_duration_ms: typing.Optional[int] = None,
    reject: typing.Optional[typing.Callable[[sonolink.models.Playable], bool]] = None,
) -> typing.List[sonolink.models.Playable]:
    """Return up to ``limit`` non-stream, de-duplicated tracks from ``tracks``.

    Skips live streams (a genre seed should be seekable tracks, not endless
    radio), drops any track whose source identifier is already in ``seen_ids``
    (seeded with what is already queued or playing so a genre pick never
    double-queues a track), rejects anything longer than ``max_duration_ms`` when
    given, and drops any track for which ``reject`` returns True (used to plug in
    the mix detector). Pure - the list-based primitive the genre ladder tiers over.
    """
    seen = set(seen_ids or ())
    picked: typing.List[sonolink.models.Playable] = []
    for track in tracks:
        if getattr(track, "is_stream", False):
            continue
        identifier = getattr(track, "identifier", None)
        if identifier is not None and identifier in seen:
            continue
        if max_duration_ms is not None and (getattr(track, "length", 0) or 0) > max_duration_ms:
            continue
        if reject is not None and reject(track):
            continue
        if identifier is not None:
            seen.add(identifier)
        picked.append(track)
        if len(picked) >= limit:
            break
    return picked


def select_playable(
    result: typing.Optional[typing.Any],
    limit: int,
    *,
    seen_ids: typing.Optional[typing.Iterable[str]] = None,
    max_duration_ms: typing.Optional[int] = None,
    reject: typing.Optional[typing.Callable[[sonolink.models.Playable], bool]] = None,
) -> typing.List[sonolink.models.Playable]:
    """Return up to ``limit`` non-stream, de-duplicated tracks from a search result.

    The multi-track sibling of :func:`_first_track`: normalises the Playlist /
    list / single-track shapes then runs :func:`filter_tracks`. ``max_duration_ms``
    and ``reject`` are optional and default to the original behaviour, so existing
    callers keep working unchanged. Pure.
    """
    return filter_tracks(
        _normalize_result_tracks(result),
        limit,
        seen_ids=seen_ids,
        max_duration_ms=max_duration_ms,
        reject=reject,
    )


# Genre-seed ladder ceilings. The strict tier treats a single track past
# GENRE_TRACK_MAX_MS as a mix even without a keyword tell (an individual song
# almost never runs this long); the middle tier relaxes to GENRE_MIX_MAX_MS, the
# same 20-minute line the mix detector calls near-certain.
GENRE_TRACK_MAX_MS = 15 * 60 * 1000  # 15 min
GENRE_MIX_MAX_MS = 20 * 60 * 1000  # 20 min


def choose_genre_tracks(
    tracks: typing.Sequence[sonolink.models.Playable],
    limit: int,
    *,
    seen_ids: typing.Optional[typing.Iterable[str]] = None,
) -> typing.Tuple[int, typing.List[sonolink.models.Playable]]:
    """Pick genre-seed tracks from interleaved candidates via a 3-tier ladder.

    Returns ``(tier, tracks)`` where ``tier`` is:

    * 1 - strict: reject anything the mix detector flags OR longer than
      :data:`GENRE_TRACK_MAX_MS` (individual songs only).
    * 2 - duration-only: reject anything longer than :data:`GENRE_MIX_MAX_MS`.
    * 3 - raw: only streams and duplicates are dropped.

    The ladder descends a tier only when the current one yields fewer than three
    tracks, so a good query stays on the strict tier and a thin one still seeds
    something rather than nothing. Pure, so the tier choice is unit-tested without
    a node. The caller logs the chosen tier.
    """

    def _is_mix(track: sonolink.models.Playable) -> bool:
        return vibes.looks_like_mix(
            getattr(track, "title", "") or "",
            getattr(track, "author", "") or "",
            getattr(track, "length", 0),
        )

    strict = filter_tracks(
        tracks,
        limit,
        seen_ids=seen_ids,
        max_duration_ms=GENRE_TRACK_MAX_MS,
        reject=_is_mix,
    )
    if len(strict) >= 3:
        return 1, strict

    duration_only = filter_tracks(
        tracks, limit, seen_ids=seen_ids, max_duration_ms=GENRE_MIX_MAX_MS
    )
    if len(duration_only) >= 3:
        return 2, duration_only

    return 3, filter_tracks(tracks, limit, seen_ids=seen_ids)


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


def resolve_session_autoplay(user_pref):
    """Initial autoplay for a NEW session: the starter's saved preference, ON if unset.

    Deliverable-4's personal preference seeds a new session only (it never flips a
    live one); a missing preference (``None``) falls back to ON so autoplaying
    recommendations is the default experience. Pure so the precedence is unit-tested
    without a database or a live player.
    """
    if user_pref is None:
        return True
    return bool(user_pref)


def is_autoplay_track(track):
    """True when ``track`` was sourced by sonolink autoplay (a recommendation).

    Reads the read-only ``Playable.autoplay`` flag sonolink stamps on every
    autoplay-discovered track (in ``AutoPlayHandler._apply_discovery`` and
    ``Queue.put_autoplay``), so the controller shows its recommendation notice
    only on autoplay-sourced tracks. Pure and None-safe.
    """
    return bool(getattr(track, "autoplay", False))


def _autoplay_on(player):
    """Whether sonolink's native autoplay is currently armed for this session."""
    return player.autoplay != sonolink.AutoPlayMode.DISABLED


def can_skip(player):
    """Whether a skip has somewhere to land, so it will not kill playback.

    sonolink's ``skip()`` STOPS the player before raising ``QueueEmpty`` when
    nothing can follow, so a bare "skip" on the last track silences the room and
    only then reports there was nothing to skip to. Callers use this pre-check
    to refuse the skip up front instead. A skip can land when:

    * the user lane holds tracks, or
    * the hidden autoplay lane holds pre-staged recommendations, or
    * the queue loops (``LOOP`` re-serves the current track, ``LOOP_ALL``
      restores from history), or
    * native autoplay is armed (the skip fetches a recommendation).

    Pure and total over the player/queue shapes the fakes mirror.
    """

    queue = player.queue
    if getattr(queue, "tracks", None):
        return True
    if getattr(queue, "autoplay_tracks", None):
        return True
    if getattr(queue, "mode", None) in (
        sonolink.QueueMode.LOOP,
        sonolink.QueueMode.LOOP_ALL,
    ):
        return True
    return _autoplay_on(player)


def _set_autoplay(player, enabled):
    """Arm (ENABLED) or disarm (DISABLED) sonolink's native autoplay for a session.

    ENABLED pre-fills a hidden autoplay lane when the queue empties, so playback
    continues gaplessly with recommendations seeded by what the session has played.
    Our players keep history enabled (sonolink requires it for autoplay), so this
    setter never raises.
    """
    player.autoplay = (
        sonolink.AutoPlayMode.ENABLED if enabled else sonolink.AutoPlayMode.DISABLED
    )


def _track_looks_like_mix(track: typing.Any) -> bool:
    """True when ``track`` scores as an hour-long mix (None-safe field reads).

    The single-track adapter over :func:`vibes.looks_like_mix` used by the
    anti-mix auto-skip guard, mirroring the closure the genre ladder uses.
    """
    return vibes.looks_like_mix(
        getattr(track, "title", "") or "",
        getattr(track, "author", "") or "",
        getattr(track, "length", 0),
    )


def decide_anti_mix_skip(
    is_autoplay: bool,
    is_mix: bool,
    consecutive: int,
    *,
    cap: int = ANTI_MIX_SKIP_CAP,
) -> typing.Tuple[bool, int]:
    """Decide whether to auto-skip a suspected mix, and the new streak counter.

    Returns ``(should_skip, new_count)``. An autoplay-sourced track that looks
    like an hour-long mix is skipped while fewer than ``cap`` skips have happened
    back-to-back; each skip increments the streak. The moment a track is allowed
    to play - a real song, a user-queued track, or the ``cap`` being reached -
    the streak resets to 0, so at most ``cap`` mixes are ever skipped in a row.
    Pure, so the bound is unit-tested without a node.
    """
    if is_autoplay and is_mix and consecutive < cap:
        return True, consecutive + 1
    return False, 0


def radio_seen_ids(
    played_ids: typing.Iterable[str],
    queued_ids: typing.Iterable[typing.Optional[str]],
    current_id: typing.Optional[str],
) -> typing.Set[str]:
    """Identifiers a radio refill must exclude, as a set.

    A refill appends to the user lane, so it must skip everything already played
    this session (so a station never loops), everything still queued (so it never
    double-queues), and the current track. Falsy identifiers are dropped. Pure.
    """
    seen: typing.Set[str] = {i for i in played_ids if i}
    seen.update(i for i in queued_ids if i)
    if current_id:
        seen.add(current_id)
    return seen


def queued_track_count(queue: typing.Any) -> int:
    """Count the tracks waiting in BOTH lanes of a sonolink queue.

    Sums the user lane (``tracks``) and the hidden autoplay lane
    (``autoplay_tracks``). The CURRENT track belongs to neither lane, so it is
    never counted - clearing the queue leaves it playing. None-safe over both
    fields, so it is total over the queue shapes the fakes mirror. Pure.
    """
    tracks = getattr(queue, "tracks", None) or ()
    autoplay = getattr(queue, "autoplay_tracks", None) or ()
    return len(tracks) + len(autoplay)


def purge_queue_lanes(queue: typing.Any) -> None:
    """Clear BOTH the user lane and the hidden autoplay lane of a sonolink queue.

    ``Queue.clear()`` empties only the user lane; a radio zap must also drop the
    staged autoplay picks so the new station starts from a clean queue. The
    autoplay lane exposes no public clear, so its deque is emptied directly
    (verified against the installed sonolink ``Queue`` source, which stores it as
    ``_autoplay_items``).
    """
    queue.clear()
    queue._autoplay_items.clear()


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
            # An explicit add turns a radio session into a normal one: the station
            # select disappears on the rerender below.
            player.radio_genre = None
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
        # When this view was created; _send_controller's dedupe keep-path only
        # keeps a very recent controller (a reconnect re-fire arrives within
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

        # Autoplay toggle on its own row (the two rows above are already full at
        # five buttons each). Green when armed, grey when off, so the button shows
        # the current session state at a glance - the controller house style.
        autoplay_on = _autoplay_on(self.player)
        container.add_item(
            discord.ui.ActionRow(
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

    async def _autoplay_toggle(self, interaction: discord.Interaction) -> None:
        try:
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

            dj = self.player.dj
            user = interaction.user
            is_manager = (
                isinstance(user, discord.Member)
                and user.guild_permissions.manage_guild
            )
            if dj is not None and not is_manager and user.id != dj.id:
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


def joinable_voice_channels(
    guild: discord.Guild,
    member: discord.Member,
    *,
    limit: int = 5,
) -> typing.List[discord.VoiceChannel]:
    """Return up to ``limit`` of ``guild``'s voice channels ``member`` may join.

    Honours each channel's view + connect permissions so the join card never
    lists a room the member cannot actually enter. Ordered by channel position
    (``guild.voice_channels`` is already position-sorted).
    """
    channels: typing.List[discord.VoiceChannel] = []
    for channel in guild.voice_channels:
        perms = channel.permissions_for(member)
        if perms.view_channel and perms.connect:
            channels.append(channel)
            if len(channels) >= limit:
                break
    return channels


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


def station_select_options(
    current_key: typing.Optional[str],
) -> typing.List[discord.SelectOption]:
    """Build the station select's options, marking ``current_key`` as default.

    One option per catalog genre; exactly the current station is preselected so
    the picker opens showing where the session already is. Extracted so the
    "current genre is marked" invariant is unit-tested without a live view.
    """
    return [
        discord.SelectOption(
            label=genre.label,
            value=genre.key,
            description=_(genre.description),
            emoji=genre.emoji,
            default=(genre.key == current_key),
        )
        for genre in vibes.GENRE_CATALOG
    ]


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


class Music(commands.Cog):
    """Music playback commands powered by sonolink (Lavalink v4)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Guards the one-shot startup restore (on_ready can fire repeatedly).
        self._restored = False
        # One live controller per guild, tracked at cog level so concurrent
        # posters (explicit restore post, track_start, reconnect re-fires) can
        # never leave two controllers standing - even across different Player
        # object instances. The lock serialises delete+post per guild.
        self._controllers: typing.Dict[int, MusicController] = {}
        self._controller_locks: typing.Dict[int, asyncio.Lock] = {}
        # Bounded, in-memory map of open "join a voice channel" cards awaiting the
        # invoker to join voice, so on_voice_state_update can swap each into the
        # vibe card exactly once. Lost on restart by design (the orphan card just
        # times out on its own view timeout).
        self._pending_watches = vibes.PendingVoiceWatches()
        # Strong refs to in-flight radio-refill tasks so they are not garbage
        # collected mid-await; each removes itself on completion.
        self._refill_tasks: typing.Set[asyncio.Task] = set()
        self._idle_check.start()

    def cog_unload(self) -> None:
        self._idle_check.cancel()

    def _client(self) -> typing.Optional[sonolink.Client]:
        return getattr(self.bot, "sl_client", None)

    def _nodes_available(self) -> bool:
        client = self._client()
        return bool(client and client.nodes)

    def _nodes_connected(self) -> bool:
        """True when at least one node is actually CONNECTED, not just registered.

        The restore path must use this, not _nodes_available: a node exists in
        client.nodes as soon as create_node() runs, well before its websocket is
        up, and consuming the one-shot restore flag at that point would make
        every decode/play fail and silently kill the restore forever.
        """
        client = self._client()
        return bool(
            client
            and any(getattr(n, "is_connected", False) for n in client.nodes)
        )

    async def _require_player(self, ctx, *, in_channel=True):
        """Return the connected player, or None after telling the user why not.

        With ``in_channel`` (the default, for control actions like skip/stop/
        volume/disconnect) the invoker must be in the bot's voice channel, so a
        bystander cannot drive playback from anywhere - the controller buttons
        already enforce this, and this makes the commands match. Read-only
        callers (queue) pass ``in_channel=False``.
        """
        player = ctx.voice_client
        if not isinstance(player, sonolink.Player):
            await ctx.send(_("I'm not connected to a voice channel."))
            return None
        if in_channel:
            author = ctx.author
            channel = getattr(player, "channel", None)
            if (
                channel is None
                or not isinstance(author, discord.Member)
                or author.voice is None
                or author.voice.channel != channel
            ):
                await ctx.send(_("You must be in my voice channel to do that."))
                return None
        return player

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
    ) -> str:
        """Store a track in a user's favourites, deduped on the track identifier.

        Returns "added" on a new row, "exists" if it was already saved, or
        "full" when the user is at the MAX_FAVOURITES cap and a new track was
        refused. The INSERT only fires while under the cap, so growth is bounded.
        """
        query = """
            INSERT INTO music_favorites
                (user_id, identifier, title, author, uri, source_name)
            SELECT $1, $2, $3, $4, $5, $6
            WHERE (SELECT COUNT(*) FROM music_favorites WHERE user_id = $1) < $7
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
            MAX_FAVOURITES,
        )
        # asyncpg returns a status string like "INSERT 0 1" (or "... 0" on a
        # conflict OR when the cap guard skipped the insert).
        if status.rsplit(" ", 1)[-1] == "1":
            return "added"
        exists = await self.bot.db_pool.fetchval(
            "SELECT 1 FROM music_favorites WHERE user_id = $1 AND identifier = $2",
            user_id,
            track.identifier,
        )
        return "exists" if exists else "full"

    async def _fetch_favourites(self, user_id: int) -> list:
        """Return a user's favourites, newest first (bounded by the cap)."""
        query = """
            SELECT identifier, title, author, uri, source_name
            FROM music_favorites
            WHERE user_id = $1
            ORDER BY added_at DESC
            LIMIT $2
        """
        return await self.bot.db_pool.fetch(query, user_id, MAX_FAVOURITES)

    async def _send_controller(
        self,
        player: Player,
        track: typing.Optional[sonolink.models.Playable] = None,
        *,
        dedupe: bool = False,
    ) -> None:
        """Send a fresh now-playing controller in the player's home channel.

        ``track`` is the just-started track from a track_start event. It lets the
        controller render during the brief window before sonolink sets
        player.current (its REST update lands after Lavalink's websocket event) -
        the cold-restore race that otherwise posts no controller.

        ``dedupe`` is set by event-driven posters (track_start): if a controller
        for the SAME track is already up, keep it instead of delete+repost, so a
        reconnect re-fire does not visibly flicker the panel. User-driven
        reposts (/play, /nowplaying) leave it False and always get a fresh
        message at the bottom of the channel.
        """
        if player.home is None:
            return

        track = track if track is not None else player.current
        if track is None:
            return

        guild_id = (
            player.channel.guild.id
            if player.channel is not None
            else getattr(player.home, "guild", None) and player.home.guild.id
        )
        if guild_id is None:
            return

        # Serialise per guild: two concurrent posters (explicit restore post +
        # a track_start, or a reconnect re-fire) would otherwise both read "no
        # controller yet" and both post. Inside the lock, delete every known
        # previous controller (the cog registry catches ones attached to a
        # different Player instance), then post exactly one.
        lock = self._controller_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            existing = self._controllers.get(guild_id)
            if (
                dedupe
                and existing is not None
                and existing.message is not None
                # Only a very recent controller counts as a duplicate: reconnect
                # re-fires land within seconds of the original post, while a
                # same-track loop iteration (QueueMode.LOOP) comes minutes later
                # and SHOULD re-post so the panel returns to the channel bottom.
                and time.monotonic() - existing.created_at < 30
            ):
                shown = existing.player.current or existing._track
                # Match by source identifier, not encoded: Lavalink may
                # re-serialise the track in its events, so the encoded base64
                # of a re-fired track_start is not guaranteed to equal the one
                # we decoded from the DB even for the same track.
                shown_id = getattr(shown, "identifier", None)
                if shown_id is not None and shown_id == getattr(
                    track, "identifier", ""
                ):
                    # Same track already has a live controller - rebind it to
                    # this (possibly new) player instance and keep the message.
                    existing.player = player
                    player.controller = existing
                    log.debug(
                        "Controller kept (dedupe) for guild %s: %s",
                        guild_id,
                        shown_id,
                    )
                    return

            for old in {player.controller, existing}:
                if old is None:
                    continue
                old.stop()
                if old.message is not None:
                    try:
                        await old.message.delete()
                    except discord.HTTPException:
                        # Keep this visible: a failed delete is exactly how a
                        # duplicate controller ends up lingering in the channel.
                        log.exception(
                            "Failed to delete the previous controller message"
                        )

            # A LayoutView carries its own content; it must be sent with no
            # embed. Components V2 TextDisplay resolves mentions (unlike an
            # embed), so suppress pings or the DJ/requester would be notified
            # on every repost.
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
            self._controllers[guild_id] = view
            # Persist this controller's id right away so the next restart's
            # stale delete targets THIS message, not whatever the last full
            # snapshot captured.
            await music_state.save_controller_message_id(
                self.bot.db_pool, guild_id, message.id
            )

    async def _init_autoplay(self, player: Player, member_id: int) -> None:
        """Seed a NEW session's autoplay from the member who started it.

        The default is that member's saved personal preference (deliverable 4),
        falling back to ON when it is unset. This seeds a session's INITIAL state
        only; the controller toggle flips it live afterwards and never re-reads the
        preference. Best-effort: a settings read hiccup must not break playback, so
        it degrades to ON.
        """
        try:
            pref = await settings.get_user(
                self.bot.db_pool, member_id, AUTOPLAY_PREF_KEY, True
            )
        except Exception:
            log.exception("Failed to read autoplay preference for %s", member_id)
            pref = True
        _set_autoplay(player, resolve_session_autoplay(pref))

    # ------------------------------------------------------------------
    # Restart persistence (snapshot live players, restore them on startup)
    # ------------------------------------------------------------------

    async def _snapshot(
        self,
        player: Player,
        track: typing.Optional[sonolink.models.Playable] = None,
    ) -> None:
        """Persist a player's live state so a restart can resume it (best-effort).

        ``track`` is the just-started track from a track_start event: during the
        window where the websocket event beats play()'s REST update,
        player.current is still the OLD track (or None), so snapshotting without
        it would persist stale state on every natural queue advance.
        """
        try:
            channel = player.channel
            current = track if track is not None else player.current
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
                # A plain "or 100" would coerce a legitimate volume of 0 (muted
                # but playing) back to full blast on restore; only None falls
                # back to the default.
                volume=(
                    100
                    if getattr(player, "volume", None) is None
                    else int(player.volume)
                ),
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
                autoplay=_autoplay_on(player),
                radio_genre=getattr(player, "radio_genre", None),
            )
        except Exception:
            log.exception("Failed to snapshot player state")

    async def _clear(self, guild_id: int) -> None:
        """Forget a guild's persisted player state (best-effort)."""
        self._controllers.pop(guild_id, None)
        self._controller_locks.pop(guild_id, None)
        await music_state.clear_state(self.bot.db_pool, guild_id)

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_sonolink_track_start(
        self, player: Player, event: sonolink.gateway.TrackStartEvent
    ) -> None:
        track = event.track
        guild_id = player.channel.guild.id if player.channel else None
        log.debug("Track start: %s (guild=%s)", track.title, guild_id)

        # Anti-mix guard: sonolink autoplay occasionally surfaces an hour-long
        # mix/compilation instead of a song. Skip it before it ever posts a
        # controller, bounded so a run of nothing-but-mixes cannot loop forever
        # skipping; the streak resets the instant a track plays normally.
        should_skip, player._automix_skips = decide_anti_mix_skip(
            is_autoplay_track(track),
            _track_looks_like_mix(track),
            getattr(player, "_automix_skips", 0),
        )
        if should_skip:
            log.info(
                "Auto-skipping suspected mix '%s' by '%s' (%d in a row, guild=%s)",
                getattr(track, "title", ""),
                getattr(track, "author", ""),
                player._automix_skips,
                guild_id,
            )
            try:
                await player.skip()
            except (sonolink.QueueEmpty, sonolink.AutoPlaySeedMissing):
                # Nothing to skip to (empty lane, autoplay off or no seed): the
                # player has stopped itself, so just stand down.
                pass
            return

        # Remember what actually played so a radio refill never re-seeds it.
        player.played_ids.add(getattr(track, "identifier", None))

        if getattr(player, "home", None) is not None:
            # Pass the event's track so the controller renders even while
            # play()'s REST update is still in flight and player.current is not
            # set yet. dedupe=True: the per-guild lock in _send_controller
            # resolves any race with the explicit restore post or a reconnect
            # re-fire - the second poster keeps the first one's message.
            await self._send_controller(player, track, dedupe=True)
        # Snapshot AFTER the controller work so a reconnect that swapped in a
        # fresh Player instance persists the rebound controller's message id,
        # not None (which would defeat the next restart's stale delete). Pass
        # the event's track: player.current may still be the previous track (or
        # None) while play()'s REST update is in flight.
        await self._snapshot(player, track)

        # Radio refill: top the station's user lane back up while it is winding
        # down (a track start with the lane nearly empty), so playback stays
        # on-genre instead of falling through to generic autoplay. Guarded so two
        # rapid starts cannot double-refill.
        if getattr(player, "radio_genre", None) and len(player.queue.tracks) <= 1:
            self._schedule_radio_refill(player)

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
    async def on_sonolink_websocket_closed(
        self, player: Player, event: sonolink.gateway.WebSocketClosedEvent
    ) -> None:
        """Self-heal remote voice closes that sonolink leaves dead.

        sonolink force-disconnects on 4014/4022 and re-negotiates closes it
        initiated itself, but a REMOTE 4006 (voice session invalidated) or 4009
        (session timeout) is only logged - the player then sits in the channel
        with no audio until someone manually reconnects. Re-running the voice
        handshake negotiates a fresh session; Lavalink keeps the player's track
        and position, so playback resumes where it broke.
        """
        if getattr(event, "code", None) not in (4006, 4009):
            return
        if not getattr(event, "by_remote", False):
            return
        if player.channel is None:
            return
        guild_id = player.channel.guild.id
        try:
            await player.connect(timeout=10.0, reconnect=True)
            log.info(
                "Re-negotiated voice session after remote close %s in guild %s",
                event.code,
                guild_id,
            )
        except Exception:
            log.exception(
                "Failed to recover from voice close %s in guild %s",
                event.code,
                guild_id,
            )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """React to voice joins/leaves.

        Two concerns share this listener: fire a pending "join a voice channel"
        watch the moment the invoker joins (or moves into) any voice channel of
        this guild, then run the empty-channel auto-disconnect.
        """
        if (
            not member.bot
            and after.channel is not None
            and after.channel != before.channel
        ):
            await self._fire_voice_watch(member)

        if member.bot:
            return

        player = member.guild.voice_client
        if not isinstance(player, sonolink.Player):
            return

        channel = player.channel
        if channel is None:
            return

        # DJ handoff: when the current DJ leaves the player's channel, pass the
        # role to the first remaining human so control never dies with them.
        # Runs before, and independent of, the empty-channel sleep below - if the
        # room is now empty the handoff clears the DJ (None) and that block then
        # handles the disconnect.
        dj = getattr(player, "dj", None)
        if (
            dj is not None
            and before.channel == channel
            and after.channel != channel
            and member.id == dj.id
        ):
            player.dj = vibes.next_dj(channel.members, leaving_id=member.id)
            await self._snapshot(player)
            controller = getattr(player, "controller", None)
            if controller is not None:
                await controller._rerender()

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
            # _clear also drops the controller registry/lock entries.
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
        if not self._nodes_connected():
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

        semaphore = asyncio.Semaphore(RESTORE_CONCURRENCY)

        async def _guarded(row) -> None:
            async with semaphore:
                try:
                    await self._restore_one(row, now)
                except Exception:
                    log.exception(
                        "Failed to restore music for guild %s", row["guild_id"]
                    )

        await asyncio.gather(*(_guarded(row) for row in rows))
        log.info("Music restore complete: processed %d player(s)", len(rows))

    async def _restore_one(self, row, now: datetime) -> None:
        """Cold-restore a single guild's playback, or forget a stale/unusable row.

        Rejoins the voice channel and replays the saved track at the
        extrapolated position, leaving exactly one fresh, working controller.
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
        # (bound to the old process) do not linger unresponsive. Use home (with
        # the voice fallback applied): that is the same channel resolution the
        # previous run used to post it, so a controller posted into the voice
        # chat is deleted too, not just one in the saved text channel.
        stale_id = row["controller_message_id"]
        if stale_id:
            try:
                await home.get_partial_message(stale_id).delete()
            except (discord.HTTPException, AttributeError):
                pass

        # Decode the exact tracks (no re-search) in ONE round trip to Lavalink:
        # the current track first, then the queue.
        decoded = await self.bot.sl_client.decode_tracks(
            row["current_track"], *(row["queue"] or [])
        )
        if not decoded or decoded[0] is None:
            await self._clear(guild_id)
            return
        current, queue_tracks = decoded[0], decoded[1:]

        # Rejoin and replay at the extrapolated position. The track_start event
        # posts a fresh controller, but a track restored in a paused state (or a
        # missed/late event) emits no track_start, so we also post one
        # explicitly below.
        player = guild.voice_client
        if not isinstance(player, Player):
            player = await channel.connect(cls=Player)
        player.home = home
        player.dj = dj
        player.queue.mode = loop_mode
        for track in queue_tracks or []:
            player.queue.put(track)
        # Restore the persisted session autoplay mode so a cold restart resumes
        # with the same behaviour. Defensive ON default if the column somehow
        # predates this row (it is added by schema.sql's additive migration).
        _set_autoplay(
            player, bool(row["autoplay"]) if "autoplay" in row else True
        )
        # Restore the radio station so the controller shows its picker again and
        # the refill keeps the genre going. Validate the key still exists in the
        # catalog (a genre could be retired between versions), else drop to None.
        radio_key = row["radio_genre"] if "radio_genre" in row else None
        player.radio_genre = radio_key if radio_key in vibes.GENRES_BY_KEY else None

        position = music_state.extrapolate_position(
            row["position_ms"],
            row["updated_at"],
            now,
            paused=row["paused"],
            length_ms=getattr(current, "length", None),
        )
        await player.play(
            current,
            start=position,
            paused=bool(row["paused"]),
            # None-check, not "or 100": volume 0 is legitimate (muted) and must
            # not come back at full blast after a restart.
            volume=100 if row["volume"] is None else int(row["volume"]),
        )
        # Post the controller explicitly: track_start may not fire at all for a
        # track restored paused, which used to leave no working controller.
        # dedupe=True lets _send_controller's per-guild lock resolve the race
        # with a track_start that DID fire, whichever lands first - the second
        # poster keeps the first one's message instead of duplicating it.
        await self._send_controller(player, dedupe=True)
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

        Called with no query this re-posts the now-playing controller when
        something is playing; otherwise it opens the "choose your vibe" picker
        (when you are in a voice channel) or an auto-updating join-a-channel
        prompt (when you are not).
        """
        if not query or not query.strip():
            await self._play_no_query(ctx)
            return

        await ctx.defer()
        await self._play_query(ctx, query)

    async def _play_no_query(self, ctx: commands.Context) -> None:
        """Handle a bare /play: repost the controller, or offer the vibe / join card.

        The already-playing branch preserves the original behaviour (re-post the
        now-playing controller at the bottom of the channel). With nothing playing,
        a member in a voice channel gets the vibe picker and a member not in voice
        gets the auto-updating join prompt.
        """
        player = ctx.voice_client
        if isinstance(player, sonolink.Player) and player.current:
            player.home = ctx.channel
            await self._send_controller(player)
            if ctx.interaction is not None:
                await ctx.send(_("Here is the player."), ephemeral=True)
            return

        author = ctx.author
        if (
            isinstance(author, discord.Member)
            and author.voice is not None
            and author.voice.channel is not None
        ):
            await self._send_vibe_card(ctx)
        else:
            await self._send_join_card(ctx)

    async def _send_vibe_card(self, ctx: commands.Context) -> None:
        """Post the "choose your vibe" card, gated to the invoker."""
        view = VibeCard(self, ctx.author.id)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _send_join_card(self, ctx: commands.Context) -> None:
        """Post the auto-updating "join a voice channel" card and arm its watch."""
        channels = joinable_voice_channels(ctx.guild, ctx.author)
        view = JoinVoiceCard(ctx.author.id, channels)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        # Overwrites any earlier pending watch for this user in this guild, so only
        # the latest join card auto-updates (the older one just times out).
        self._pending_watches.add(ctx.guild.id, ctx.author.id, view)

    async def _play_query(self, ctx, query: str) -> None:
        """Search for ``query`` and queue the result - the shared /play <query> body.

        Extracted verbatim from the play command so the vibe card's "Search for
        music instead" modal runs the identical path through a minimal ctx adapter
        (:class:`_ModalPlayContext`). The caller has already deferred.
        """
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
            # Fresh session: seed autoplay from the starter's saved preference.
            await self._init_autoplay(player, ctx.author.id)

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

        # An explicit query ends radio mode: a station session becomes a normal
        # one and the controller drops its station select on the next rerender.
        player.radio_genre = None
        if not player.current:
            await player.play(player.queue.get())
        await self._snapshot(player)

    async def _search_genre_tracks(self, genre, seen_ids):
        """Run both curated queries, blend them and pick genre tracks (tier, list).

        Runs the trending + all-time searches, interleaves them so a session is
        neither all this month's virals nor all evergreen classics, then filters
        the blend down the mix-detector ladder excluding ``seen_ids``. The shared
        search core behind both the initial seed/zap (:meth:`_apply_genre`) and
        the radio refill (:meth:`_radio_refill`).
        """
        result_trending = await self._search(vibes.resolve_query(genre.query_trending))
        result_alltime = await self._search(vibes.resolve_query(genre.query_alltime))
        candidates = vibes.interleave_results(
            _normalize_result_tracks(result_trending),
            _normalize_result_tracks(result_alltime),
        )
        return choose_genre_tracks(
            candidates, vibes.TRACKS_PER_GENRE, seen_ids=seen_ids
        )

    async def _apply_genre(self, player, genre, requester_id, *, replace):
        """Seed ``genre`` onto ``player``; the shared vibe-card / station-select core.

        Returns ``(tier, tracks)``; an empty ``tracks`` means the search found
        nothing (the caller reports it) and the player is left untouched.

        ``replace=True`` is the radio zap: purge BOTH queue lanes (the user lane
        and the hidden autoplay lane) and start the new genre immediately,
        replacing whatever was playing - without touching the cog's restore
        snapshot the way ``/stop`` would. ``replace=False`` is the start-from-
        silence path: playback only kicks off when nothing is current. Either way
        the tracks are radio-tagged and ``player.radio_genre`` is set (before the
        play, so the reposted controller shows the new station), and a fresh
        snapshot is written. The seed excludes the current track and everything
        played this session; the non-replace path also excludes what is queued
        (the replace path is about to purge it).
        """
        seen = radio_seen_ids(
            player.played_ids,
            () if replace else (getattr(t, "identifier", None) for t in player.queue.tracks),
            getattr(player.current, "identifier", None),
        )
        tier, tracks = await self._search_genre_tracks(genre, seen)
        log.info(
            "Genre seed for %s: tier %d (%d tracks, replace=%s)",
            genre.key,
            tier,
            len(tracks),
            replace,
        )
        if not tracks:
            return tier, []

        if replace:
            purge_queue_lanes(player.queue)
            # Single-track LOOP makes queue.get() re-serve the OUTGOING track
            # (its current_track survives the purge), so the zap would replay the
            # old song forever and strand the new station in the lane. A station
            # is a stream, not a one-track loop: drop LOOP to NORMAL so get()
            # serves the new genre. LOOP_ALL still serves the new track (its lane
            # is non-empty), so leave it untouched.
            if player.queue.mode == sonolink.QueueMode.LOOP:
                player.queue.mode = sonolink.QueueMode.NORMAL
        for track in tracks:
            track.extras.requester = requester_id
            track.extras.radio = True
            player.queue.put(track)
        player.radio_genre = genre.key
        if replace or not player.current:
            await player.play(player.queue.get())
        await self._snapshot(player)
        return tier, tracks

    def _schedule_radio_refill(self, player) -> None:
        """Kick off a background radio refill unless one is already in flight.

        The in-flight flag is set synchronously (no await between the check and
        the set), so two track-start handlers racing on the same player can never
        both launch a refill.
        """
        if getattr(player, "_radio_refilling", False):
            return
        player._radio_refilling = True
        task = asyncio.create_task(self._radio_refill(player))
        self._refill_tasks.add(task)
        task.add_done_callback(self._refill_tasks.discard)

    async def _radio_refill(self, player) -> None:
        """Append TRACKS_PER_GENRE more of the station's genre to the user lane.

        Excludes every identifier already played this session, everything still
        queued and the current track, so the station keeps moving rather than
        looping. Fills the USER lane (``queue.put``) so the tracks show in
        "Up Next" and keep the player off the idle path. If it finds nothing new
        it does NOT stop - an ENABLED autoplay session then fills the gap, and an
        autoplay-off session simply ends, respecting that choice.
        """
        try:
            key = getattr(player, "radio_genre", None)
            genre = vibes.GENRES_BY_KEY.get(key) if key else None
            if genre is None:
                return
            guild_id = player.channel.guild.id if player.channel else None
            seen = radio_seen_ids(
                player.played_ids,
                (getattr(t, "identifier", None) for t in player.queue.tracks),
                getattr(player.current, "identifier", None),
            )
            tier, tracks = await self._search_genre_tracks(genre, seen)
            if not tracks:
                log.info(
                    "Radio refill for %s: nothing new, leaving to autoplay (guild=%s)",
                    genre.key,
                    guild_id,
                )
                return
            # The station can change or end while our two searches are in flight:
            # a zap moves radio_genre to a new key, and an explicit query / Add /
            # favourites clears it to None. Either way these stale-genre tracks
            # must not be injected into what is now a different (or normal)
            # session, so bail if the station is no longer the one we searched.
            if getattr(player, "radio_genre", None) != key:
                log.info(
                    "Radio refill for %s: station changed mid-search, discarding "
                    "(guild=%s)",
                    genre.key,
                    guild_id,
                )
                return
            dj = getattr(player, "dj", None)
            requester_id = dj.id if dj is not None else None
            for track in tracks:
                if requester_id is not None:
                    track.extras.requester = requester_id
                track.extras.radio = True
                player.queue.put(track)
            log.info(
                "Radio refill for %s: tier %d, +%d track(s) (guild=%s)",
                genre.key,
                tier,
                len(tracks),
                guild_id,
            )
            await self._snapshot(player)
            controller = getattr(player, "controller", None)
            if controller is not None:
                await controller._rerender()
        except Exception:
            log.exception("Radio refill failed")
        finally:
            player._radio_refilling = False

    async def _start_genre(self, interaction: discord.Interaction, genre) -> None:
        """Join the author's voice channel and start (or zap to) a genre station.

        Reuses the exact playback seams: the same connect
        (``channel.connect(cls=Player)``) and the same search/queue/play/snapshot
        core (:meth:`_apply_genre`) the controller station select uses. When
        something is already playing the pick REPLACES it (the radio zap);
        otherwise it starts a fresh session and the existing track_start ->
        controller flow takes over. All feedback is ephemeral.
        """
        author = interaction.user
        if (
            not isinstance(author, discord.Member)
            or author.voice is None
            or author.voice.channel is None
        ):
            await interaction.response.send_message(
                _("You must be in a voice channel first."), ephemeral=True
            )
            return
        if not self._nodes_available():
            await interaction.response.send_message(
                _("Music is currently unavailable - no Lavalink node is connected."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        player = interaction.guild.voice_client
        if not isinstance(player, sonolink.Player):
            try:
                player = await author.voice.channel.connect(cls=Player)
            except discord.ClientException:
                log.exception("Failed to connect to the voice channel")
                await interaction.followup.send(
                    _("I was unable to join your voice channel. Please try again."),
                    ephemeral=True,
                )
                return
            player.dj = author
            player.home = interaction.channel
            # Fresh session: seed autoplay from the starter's saved preference.
            await self._init_autoplay(player, author.id)
        if player.home is None:
            player.home = interaction.channel

        replace = player.current is not None
        _tier, tracks = await self._apply_genre(
            player, genre, author.id, replace=replace
        )
        if not tracks:
            await interaction.followup.send(
                _("I couldn't find any {genre} tracks right now.").format(
                    genre=genre.label
                ),
                ephemeral=True,
            )
            return

        if replace:
            await interaction.followup.send(
                _("Switched to the {genre} station ({count} track(s)).").format(
                    genre=genre.label, count=len(tracks)
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                _("Starting a {genre} session with {count} track(s). Enjoy!").format(
                    genre=genre.label, count=len(tracks)
                ),
                ephemeral=True,
            )

    async def _fire_voice_watch(self, member: discord.Member) -> None:
        """Swap a member's open join card into the vibe card once they join voice."""
        view = self._pending_watches.pop(member.guild.id, member.id)
        if view is None:
            return
        try:
            card = VibeCard(self, member.id)
            await view.message.edit(view=card)
            card.message = view.message
            view.stop()
        except discord.HTTPException:
            log.exception("Failed to swap the join card into the vibe card")

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
        # Persist the paused flag now; it drives the restore position maths.
        await self._snapshot(player)
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
        # Persist the resumed flag now; it drives the restore position maths.
        await self._snapshot(player)
        await ctx.send(_("Resumed the player."))

    @commands.hybrid_command(name="skip", aliases=["next"])
    @commands.guild_only()
    async def skip(self, ctx: commands.Context) -> None:
        """Skip the current track and play the next one."""
        player = await self._require_player(ctx)
        if player is None:
            return
        # Pre-check: sonolink stops playback BEFORE raising QueueEmpty, so a skip
        # with nowhere to land must be refused up front - never kill the current
        # track just to say there was nothing after it.
        if not can_skip(player):
            await ctx.send(_("There are no more tracks in the queue to skip to."))
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

    @commands.hybrid_command(name="seek")
    @commands.guild_only()
    @app_commands.describe(
        position="A timestamp (1:23 or 1:02:03), whole seconds (90), or a relative +30 / -15."
    )
    async def seek(self, ctx: commands.Context, *, position: str) -> None:
        """Jump to a position in the current track."""
        player = await self._require_player(ctx)
        if player is None:
            return
        track = player.current
        if track is None:
            await ctx.send(_("There's nothing playing right now."))
            return
        if track.is_stream:
            await ctx.send(_("I can't seek within a live stream."))
            return
        target = vibes.parse_seek_target(position)
        if target is None:
            await ctx.send(
                _("I couldn't read that position. Try `1:23`, `90`, or `+30`.")
            )
            return
        target_ms = vibes.resolve_seek_ms(target, player.position, track.length)
        await player.seek(target_ms)
        await ctx.send(
            _("Jumped to {position}.").format(position=format_clock(target_ms))
        )

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
    @app_commands.describe(value="Volume level between 0 and 200 (100 is default).")
    async def volume(
        self, ctx: commands.Context, value: commands.Range[int, 0, 200]
    ) -> None:
        """Set the player volume (0-200)."""
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

    @commands.hybrid_command(name="clearqueue", aliases=["cq", "clearq"])
    @commands.guild_only()
    async def clearqueue(self, ctx: commands.Context) -> None:
        """Clear the upcoming queue while the current track keeps playing."""
        player = await self._require_player(ctx)
        if player is None:
            return
        count = queued_track_count(player.queue)
        if count == 0:
            await ctx.send(_("The queue is already empty."))
            return
        # Empties both the user lane and the hidden autoplay lane; the current
        # track is never touched, so playback keeps going. In radio mode the
        # station stays set and restocks at the natural track boundary - that is
        # the intended radio semantics, so we do not clear player.radio_genre.
        purge_queue_lanes(player.queue)
        # Persist the purge so a restart restores the now-empty queue.
        await self._snapshot(player)
        await ctx.send(
            _("Cleared {count} track(s) from the queue.").format(count=count)
        )

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
        player = await self._require_player(ctx, in_channel=False)
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
            # Fresh session: seed autoplay from the starter's saved preference.
            await self._init_autoplay(player, ctx.author.id)

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

        # Playing favourites is an explicit choice: it ends any radio session.
        player.radio_genre = None
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

        result = await self.add_favourite(ctx.author.id, track)
        if result == "added":
            await ctx.send(
                _("Added **{title}** by `{author}` to your favourites.").format(
                    title=track.title, author=track.author
                )
            )
        elif result == "full":
            await ctx.send(
                _("Your favourites are full (max {max}). Remove some first.").format(
                    max=MAX_FAVOURITES
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

"""Playback engine core for the music cog.

The lowest layer of the music package: the :class:`Player` (a sonolink player
enriched with the DJ, home channel and controller handles), its YouTube-seed
autoplay handler, the search-result normalisers and the voice-channel resolver
it leans on. The cog, its commands and listeners live in music.py; the
interactive Discord UI lives in views.py; the pure genre / mix logic lives in
vibes.py. This module imports nothing from music.py, so the layering stays
acyclic - music.py imports from here.
"""


import logging
import typing

import sonolink
import sonolink.models
from sonolink.rest.enums import TrackSourceType

from cogs.music import vibes

# sonolink's autoplay builds its discovery query from the seed track's raw
# identifier, which only resolves for a YouTube seed (see
# _YouTubeSeedAutoPlayHandler). We subclass its private handler to repair a
# non-YouTube seed first. The import is guarded so cogs still import under the
# stub sonolink on the 3.10 dev box, where these internals are absent.
try:
    from sonolink.gateway.player.handlers._autoplay import (
        AutoPlayHandler as _SonoAutoPlayHandler,
    )
except Exception:  # pragma: no cover - stub sonolink lacks the internals
    _SonoAutoPlayHandler = None

if typing.TYPE_CHECKING:
    import discord

    from cogs.music.views import MusicController

log = logging.getLogger(__name__)


# Default search source for plain (non-URL) queries. Full URLs are still resolved
# directly by Lavalink regardless of this value.
SEARCH_SOURCE = TrackSourceType.YOUTUBE


# Per-player history cap (sonolink defaults to unbounded): enough for
# Back-stepping, autoplay seeding and LOOP_ALL restore, hard-bounded memory.
HISTORY_MAX_ITEMS = 100


class Player(sonolink.Player):
    """A sonolink player that also tracks the DJ, home text channel, and controller.

    sonolink connects players via the discord.py class-pass form
    (``channel.connect(cls=Player)``), so these extras are populated by the cog
    immediately after the connection is established rather than in ``__init__``.
    """

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        # Scale guard: sonolink's history deque is UNBOUNDED by default
        # (HistorySettings.max_items=None), so a marathon session would grow one
        # Playable per track forever. 100 covers Back-stepping, autoplay seeding
        # and LOOP_ALL restore in practice while hard-bounding memory per player.
        # Guarded so a stub sonolink (no HistorySettings) keeps working.
        if "history_settings" not in kwargs:
            history_cls = getattr(
                getattr(sonolink, "models", None), "HistorySettings", None
            )
            if history_cls is not None:
                kwargs["history_settings"] = history_cls(
                    enabled=True, max_items=HISTORY_MAX_ITEMS
                )
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
        # Active audio-effect preset key (None = no effect). Set by the effects
        # seam, read by the controller and the snapshot; restored on cold restart.
        self.effect_preset: typing.Optional[str] = None
        # Repair autoplay for non-YouTube seeds: swap sonolink's stock autoplay
        # handler for our subclass, reusing the SAME settings object so the
        # player.autoplay getter/setter (and _set_autoplay) keep operating on it.
        # Guarded so a stub sonolink (no internals) leaves the base handler as-is.
        if _YouTubeSeedAutoPlayHandler is not None:
            self._autoplay_handler = _YouTubeSeedAutoPlayHandler(
                self, settings=self._autoplay_handler._settings
            )

    async def on_voice_state_update(self, data: typing.Any) -> None:
        """Follow a server-initiated move by syncing the discord.py channel ref.

        THE fix for the "bot snaps back when dragged" bug. sonolink's ``DpyPlayer``
        forwards the new channel to Lavalink on a move (so the audio follows) but
        NEVER updates the inherited ``discord.VoiceProtocol`` ``channel`` attribute
        - it only writes its private ``_connection.channel_id`` (see sonolink
        ``handlers/_events.py::on_voice_state_update``). discord.py, in turn, hands
        a custom VoiceProtocol its own voice-state events verbatim and never
        touches ``channel`` (``state.py::parse_voice_state_update``). So after a
        drag ``player.channel`` stays pinned to the ORIGINAL room, and everything
        that reads it misbehaves: the same-voice gates refuse the people actually
        with the bot, the DJ handoff / empty-channel auto-leave / idle check read
        the wrong room, the snapshot persists the wrong ``voice_channel_id``, and -
        worst - the websocket-close self-heal ``connect()``s back to the stale
        channel, literally dragging the bot to its old room.

        We delegate to sonolink first (audio + its connection channel_id), then
        point ``self.channel`` at the new channel so all of the above sees the room
        the bot is really in. This is a subclass-level seam (no module
        monkeypatching). A None channel_id (a real disconnect / kick) is left to
        sonolink and discord.py cleanup, which remove the voice client entirely.
        Best-effort: a resolution hiccup never propagates into the gateway.
        """
        await super().on_voice_state_update(data)
        try:
            channel_id = data.get("channel_id") if isinstance(data, dict) else None
            new_channel = resolve_voice_channel(getattr(self, "_guild", None), channel_id)
            if new_channel is not None and new_channel != getattr(
                self, "channel", None
            ):
                self.channel = new_channel
        except Exception:
            log.exception("Failed to sync player channel after a voice-state update")


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


def youtube_seed_query(reference: typing.Any) -> str:
    """Build a ``"{author} {title}"`` YouTube search query from a seed track.

    Used to find a YouTube equivalent of a non-YouTube autoplay seed. Returns ``""``
    when the track carries neither author nor title (nothing to search on). Pure and
    None-safe, so the query shape is unit-tested without a node.
    """
    author = (getattr(reference, "author", "") or "").strip()
    title = (getattr(reference, "title", "") or "").strip()
    return " ".join(part for part in (author, title) if part)


def _is_youtube_seed_source(source_name: typing.Any) -> bool:
    """Whether a track's ``source_name`` is YouTube (its id already seeds a Radio mix)."""
    return (source_name or "").strip().lower() == "youtube"


def _is_youtube_radio_provider(provider: typing.Any) -> bool:
    """Whether the autoplay provider is the YouTube Radio URL (needs a YouTube id)."""
    return "youtube.com" in str(provider or "").lower()


def seed_needs_youtube_resolution(reference: typing.Any, provider: typing.Any) -> bool:
    """Whether an autoplay seed must be re-resolved to a YouTube track first.

    sonolink formats the seed's raw ``identifier`` into the YouTube Radio URL, which
    only loads when the seed came from YouTube. Returns True only when there IS a
    usable seed, the discovery provider is that YouTube Radio URL, and the seed is
    NOT a YouTube track (e.g. a Spotify/LavaSrc seed whose 22-char id YouTube rejects
    with "AllClientsFailedException"). A YouTube seed, a missing seed, or a
    non-YouTube provider (Spotify/Deezer recommendations accept their own ids) all
    return False so sonolink's native flow runs unchanged. Pure, so the decision is
    unit-tested without a node or a live player.
    """
    if reference is None or not getattr(reference, "identifier", None):
        return False
    if not _is_youtube_radio_provider(provider):
        return False
    return not _is_youtube_seed_source(getattr(reference, "source_name", None))


if _SonoAutoPlayHandler is not None:

    class _YouTubeSeedAutoPlayHandler(_SonoAutoPlayHandler):
        """AutoPlay handler that repairs a non-YouTube seed before discovery.

        sonolink's ``AutoPlayHandler`` builds its discovery query by formatting the
        seed track's raw ``identifier`` into the YouTube Radio URL
        (``watch?v={identifier}&list=RD{identifier}``). That only works for a YouTube
        seed: a LavaSrc seed (e.g. Spotify) carries a 22-char provider id that
        YouTube rejects ("AllClientsFailedException"), so autoplay silently yields
        nothing once a Spotify playlist ends.

        The YouTube-seeded path is left EXACTLY as sonolink ships it (delegated to
        ``super()._fill_auto_queue``), so live-verified YouTube autoplay never
        changes. For a non-YouTube seed we first resolve a YouTube equivalent via
        ``ytsearch:{author} {title}`` and seed the Radio mix off THAT track's id.
        Every failure path (no query, no node, empty search, node error) degrades to
        "no autoplay this cycle" by returning None: playback has already ended, so
        the skip path stops cleanly instead of crashing mid-nothing. Whatever this
        produces is still vetted by the anti-mix guard in on_sonolink_track_start.

        Instance-level only (no monkeypatching): the Player swaps this in for the
        stock handler in ``__init__``. The private sonolink internals it leans on
        are pinned by ``test_autoplay_handler_pins_sonolink_internals`` so a
        sonolink refactor fails loudly rather than silently disabling the repair.
        """

        __slots__ = ()

        async def _fill_auto_queue(self):
            reference = self._player.current or (
                self._player.queue.history[-1]
                if self._player.queue.history
                else None
            )
            if not seed_needs_youtube_resolution(
                reference, self._settings.provider
            ):
                # Missing seed (super() raises AutoPlaySeedMissing, preserved) or a
                # seed the provider already handles (YouTube): sonolink's own flow
                # is correct, so run it verbatim and never break YouTube autoplay.
                return await super()._fill_auto_queue()

            shadow = await self._resolve_youtube_seed(reference)
            if shadow is None or not shadow.identifier:
                # No YouTube equivalent found: skip autoplay this cycle rather than
                # firing a query doomed to fail. Playback has already ended.
                log.info(
                    "AutoPlay: no YouTube seed for %r by %r (source=%s), "
                    "skipping this cycle",
                    getattr(reference, "title", ""),
                    getattr(reference, "author", ""),
                    getattr(reference, "source_name", ""),
                )
                return None
            return await self._fill_from_seed(shadow.identifier)

        async def _resolve_youtube_seed(self, reference):
            """Find a YouTube track equivalent to a non-YouTube seed, or None.

            Runs a single ``ytsearch:{author} {title}`` on the player's node and
            returns the first candidate. The search is naturally spaced (at most one
            per autoplay cycle, already serialised by ``auto_play``'s lock) and any
            failure degrades to None so the caller skips autoplay this cycle.
            """
            query = youtube_seed_query(reference)
            if not query:
                return None
            try:
                search = await self._player.node.search_track(
                    query, source=SEARCH_SOURCE
                )
            except Exception:
                log.exception("AutoPlay: YouTube seed search failed")
                return None
            return _first_track(search)

        async def _fill_from_seed(self, identifier):
            """Discover a Radio mix seeded by ``identifier`` (a YouTube video id).

            Mirrors the tail of sonolink's ``_fill_auto_queue`` (seed bookkeeping,
            the Radio query, de-dup against prior seeds) but seeds off the id we
            resolved, then hands the winners to sonolink's own ``_apply_discovery``
            so queueing / autoplay flagging / play stay identical. The resolved id is
            added to ``_seeds`` so the seed video itself is filtered out of its own
            Radio mix, exactly as the native path does. Any failure degrades to None.
            """
            if len(self._seeds) > self._settings.max_seeds:
                self._seeds.clear()
            self._seeds.add(identifier)
            query = str(self._settings.provider).format(identifier=identifier)
            # Wrap the search AND _apply_discovery together, exactly as sonolink's
            # own _fill_auto_queue does: _apply_discovery calls player.play(), and
            # a Lavalink REST hiccup there must degrade to "no autoplay this cycle"
            # (return None -> skip() stops cleanly) rather than propagate out of
            # skip() past the QueueEmpty/AutoPlaySeedMissing its callers expect.
            try:
                search = await self._player.node.search_track(query)
                discovery = [
                    track
                    for track in _normalize_result_tracks(search)
                    if track.identifier not in self._seeds
                ]
                if not discovery:
                    return None
                return await self._apply_discovery(discovery)
            except Exception:
                log.exception("AutoPlay: Radio discovery failed for resolved seed")
                return None

else:  # pragma: no cover - stub sonolink on the 3.10 dev box has no internals
    _YouTubeSeedAutoPlayHandler = None


def resolve_voice_channel(guild: typing.Any, channel_id: typing.Any) -> typing.Any:
    """Resolve a voice-state payload's ``channel_id`` to a channel via ``guild``.

    Returns the channel object or None (a falsy id - a disconnect - or a missing
    guild / unknown channel). Pure over the guild's ``get_channel`` seam so the
    move-follow channel sync in :meth:`Player.on_voice_state_update` is unit tested
    without a real gateway player.
    """
    if not channel_id or guild is None:
        return None
    return guild.get_channel(int(channel_id))

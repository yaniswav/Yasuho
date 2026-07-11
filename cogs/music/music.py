import asyncio
import logging
import math
import time
import typing
from datetime import datetime, timezone

import discord
import sonolink
import sonolink.models
from discord import app_commands
from discord.ext import commands, tasks
from sonolink.rest.enums import TrackSourceType

from cogs.music import effects, lyrics, sponsorblock, vibes, voteskip
from tools import music_state, settings
from tools.i18n import _, ngettext
from tools.paginator import Paginator, paginate_lines
from tools.quotas import QuotaRegistry

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

log = logging.getLogger(__name__)


# Default search source for plain (non-URL) queries. Full URLs are still resolved
# directly by Lavalink regardless of this value.
SEARCH_SOURCE = TrackSourceType.YOUTUBE

# How long (in seconds) a player may stay idle before it is disconnected to free
# resources. A player counts as idle when it is paused, has nothing playing and
# an empty queue, or is alone in its voice channel. See the idle-timeout loop.
IDLE_TIMEOUT = 300

# Per-player history cap (sonolink defaults to unbounded): enough for
# Back-stepping, autoplay seeding and LOOP_ALL restore, hard-bounded memory.
HISTORY_MAX_ITEMS = 100

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

# How long (seconds) after a controller is posted a same-track track_start still
# counts as a reconnect re-fire (keep the message, no flicker) rather than a
# fresh play of that track. A /loop track iteration re-fires the SAME track long
# after its panel went up, past this window, so it reposts the panel to the
# channel bottom instead of silently keeping the old message.
CONTROLLER_REFIRE_WINDOW = 30.0

# How often (seconds) the idle loop folds and logs the QuotaRegistry snapshot.
# The loop ticks every 60s; this gates the log to a ~10-minute heartbeat, and it
# only ever logs when a counter is nonzero (see effects.stats_are_nonzero).
QUOTA_LOG_INTERVAL = 600.0

# Slash choices for /filter, built once from the effect catalog. Text (prefix)
# callers pass the raw key/label and are resolved with effects.resolve_preset.
EFFECT_CHOICES = [
    app_commands.Choice(name=f"{preset.emoji} {preset.label}", value=preset.key)
    for preset in effects.PRESET_CATALOG
]


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


def can_go_previous(player):
    """Whether there is a genuinely previous track to step back to.

    The pure sibling of :func:`can_skip` for the Back control. sonolink's history
    holds the tracks played STRICTLY BEFORE the current one, newest at the right
    end: the current track is never in history - it is pushed there only when the
    NEXT track is popped (see sonolink ``Queue.pop`` / ``History._push``). So a
    non-empty history means exactly one thing - there is a previous track that
    ``Player.previous()`` will pop and replay. An empty history (the first track
    of a session, or a fresh cold-restore whose history has not rebuilt yet) means
    there is nothing sensible to go back to, so callers refuse up front instead of
    letting ``Player.previous()`` raise ``HistoryEmpty`` after it has already moved
    the current track to the queue front.

    ``bool(history)`` reads sonolink ``ReadableCollection.__bool__`` (``len > 0``),
    so a plain-list fake and the real ``History`` behave identically. Pure,
    None-safe, and total over the player/queue shapes the fakes mirror.
    """
    return bool(getattr(player.queue, "history", None))


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


def decide_controller_action(
    *,
    dedupe: bool,
    has_live_controller: bool,
    displayed_id: typing.Optional[str],
    incoming_id: typing.Optional[str],
    age_seconds: typing.Optional[float],
    refire_window: float = CONTROLLER_REFIRE_WINDOW,
) -> str:
    """Decide how a controller (re)post should update the live now-playing panel.

    Returns one of:

    * ``"repost"`` - delete any previous controller and send a fresh message at
      the bottom of the channel. Used for user-driven reposts (``/play`` with no
      query, ``/nowplaying``), when there is no live controller to touch, and for
      a /loop track re-fire (the SAME track starting again long after its panel
      went up, which should come back to the channel bottom).
    * ``"keep"`` - a reconnect re-fire of the track the panel ALREADY displays,
      within ``refire_window`` seconds of the post: keep the message untouched so
      it never flickers.
    * ``"rerender"`` - a GENUINE change to a different track: edit the existing
      panel in place so it reflects the new track without churning the channel.

    The keep/rerender split turns on ``displayed_id`` - the identifier of the
    track the controller's message currently RENDERS - NOT the player's live
    ``current``. During a natural queue advance sonolink sets ``player.current``
    to the new track BEFORE that track's track_start reaches this cog, so
    comparing the incoming track against ``current`` always matched and wrongly
    KEPT the stale panel on every real track change (the live-reported bug).
    Comparing against what the panel actually rendered is what lets a real change
    update. Pure, so the classification is unit-tested without a node.
    """
    if not dedupe or not has_live_controller:
        return "repost"
    if displayed_id is not None and displayed_id == incoming_id:
        if age_seconds is not None and age_seconds < refire_window:
            return "keep"
        return "repost"
    return "rerender"


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


# How many upcoming tracks the queue view lists per page.
QUEUE_PAGE_SIZE = 10


def queue_page(
    total: int, page: int, per_page: int = QUEUE_PAGE_SIZE
) -> typing.Tuple[int, int, int, int]:
    """Resolve the paginated slice of ``total`` queued tracks for ``page``.

    Returns ``(clamped_page, total_pages, start, end)`` where ``[start:end]``
    slices the upcoming-tracks list for the requested page. ``page`` is
    0-indexed and clamped into ``[0, total_pages - 1]`` so a queue that shrank
    under the viewer never lands on a blank page; ``total_pages`` is at least 1
    even for an empty queue. Pure - the queue view's paging math lives here so it
    can be tested without any discord objects.
    """
    safe_total = max(total, 0)
    total_pages = max(1, (safe_total + per_page - 1) // per_page)
    clamped = max(0, min(page, total_pages - 1))
    start = clamped * per_page
    end = min(start + per_page, safe_total)
    return clamped, total_pages, start, end


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


def effect_select_options(
    current_key: typing.Optional[str],
) -> typing.List[discord.SelectOption]:
    """Build the effect picker's options, marking ``current_key`` as default.

    One option per preset in catalog order; the active preset (or Off when none
    is set) is preselected so the ephemeral picker opens on the current state.
    Extracted so the "current effect is marked" invariant is unit-tested without
    a live view. Descriptions are translated here (in-task); labels are proper
    names shown verbatim.
    """
    active = current_key or effects.OFF_KEY
    return [
        discord.SelectOption(
            label=preset.label,
            value=preset.key,
            description=_(preset.description),
            emoji=preset.emoji,
            default=(preset.key == active),
        )
        for preset in effects.PRESET_CATALOG
    ]


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
        # The chantier's shared quota registry (effects rate limit + the process-
        # wide filtered-players ceiling; lyrics/vote-skip lots read it too). One
        # instance per cog so its counters and bounded maps live for the process.
        self.quotas = QuotaRegistry()
        # Live synced-lyrics sessions (lot P5), one per guild, bounded by the
        # process-wide synced_lyrics ceiling. Lives on the cog so its loops and
        # its bounded map span the process; torn down in cog_unload.
        self.lyrics_sessions = lyrics.LyricsSessions(self.quotas.synced_lyrics)
        # Live democratic skip votes (lot P6), at most one per guild. In-memory
        # and self-limiting (a 30s vote, self-detaching on pass / expiry / track
        # change / teardown), so it needs no quota slot; cleaned via _clear.
        self.skip_votes = voteskip.SkipVotes()
        # Monotonic timestamp of the last quota-stats heartbeat log (see _idle_check).
        self._last_quota_log = time.monotonic()
        self._idle_check.start()

    def cog_unload(self) -> None:
        self._idle_check.cancel()
        self.lyrics_sessions.shutdown()

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
            incoming_id = getattr(track, "identifier", None)
            action = decide_controller_action(
                dedupe=dedupe,
                has_live_controller=(
                    existing is not None and existing.message is not None
                ),
                # What the panel actually RENDERS, not existing.player.current: a
                # natural advance sets current to the new track BEFORE its
                # track_start reaches us, so current already equals the incoming
                # track and comparing against it wrongly kept the stale panel on
                # every real change. _rendered_id is the identity of the track on
                # screen, which only a genuine change actually differs from.
                displayed_id=getattr(existing, "_rendered_id", None),
                incoming_id=incoming_id,
                age_seconds=(
                    time.monotonic() - existing.created_at
                    if existing is not None
                    else None
                ),
            )

            if action == "keep":
                # Reconnect re-fire of the track already on screen: rebind to
                # this (possibly new) Player instance and keep the message so the
                # panel never flickers.
                existing.player = player
                player.controller = existing
                log.debug(
                    "Controller kept (re-fire) for guild %s: %s",
                    guild_id,
                    incoming_id,
                )
                return

            if action == "rerender":
                # Genuine change to a different track: update the existing panel
                # in place (no delete+repost churn). Rebind first so the render
                # reads this player, and thread the event's track through so the
                # render is correct even before player.current catches up.
                existing.player = player
                if await existing._rerender_for_track(track):
                    player.controller = existing
                    log.debug(
                        "Controller re-rendered for guild %s: %s",
                        guild_id,
                        incoming_id,
                    )
                    return
                # The message was gone/stale: fall through and repost a fresh one.

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
                effect=getattr(player, "effect_preset", None),
            )
        except Exception:
            log.exception("Failed to snapshot player state")

    async def _clear(self, guild_id: int) -> None:
        """Forget a guild's persisted player state (best-effort).

        Also the universal effect-ceiling release point: every disconnect / stop
        / idle-teardown / restore-drop routes through here, so releasing the
        guild's ``filtered_players`` slot (idempotent - a no-op when it held
        none) keeps the process-wide ceiling honest without touching each path.
        """
        self._controllers.pop(guild_id, None)
        self._controller_locks.pop(guild_id, None)
        self.quotas.filtered_players.release(guild_id)
        # End any live synced-lyrics session (idempotent; releases its ceiling
        # slot) - every disconnect / stop / idle-teardown / restore-drop lands here.
        await self.lyrics_sessions.stop(guild_id)
        # Cancel a live skip vote the same way (idempotent) - playback for this
        # guild is going away, so a pending vote can no longer resolve.
        await self.skip_votes.clear(guild_id)
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

        # A new track is playing: end a synced-lyrics session and cancel any live
        # skip vote whose track this is not (a reconnect re-fire of the same track
        # keeps both - the ids match; see notify_track). Done before the anti-mix
        # guard below so a genuine change proactively finalises a stale vote/session
        # rather than leaving it to time out.
        if guild_id is not None:
            track_id = getattr(track, "identifier", None)
            await self.lyrics_sessions.notify_track(guild_id, track_id)
            await self.skip_votes.notify_track(guild_id, track_id)

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
    async def on_sonolink_unknown_event(
        self, player: Player, data: dict
    ) -> None:
        """Log SponsorBlock plugin telemetry (segment skips) at debug.

        sonolink surfaces every event type it does not model as
        ``sonolink_unknown_event``; SponsorBlock's SegmentSkipped / SegmentsLoaded
        and chapter events arrive here. Instrumentation only - no playback effect
        and nothing user-facing.
        """
        sponsorblock.log_ws_event(player, data)

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
            # Quota-stats heartbeat: fold the whole registry into one INFO line
            # about every QUOTA_LOG_INTERVAL, and only when something has actually
            # happened, so an idle process stays silent.
            if now - self._last_quota_log >= QUOTA_LOG_INTERVAL:
                self._last_quota_log = now
                stats = self.quotas.stats()
                if effects.stats_are_nonzero(stats):
                    log.info("Music quota stats: %s", effects.format_quota_stats(stats))
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
        # Player birth: hand the node its SponsorBlock skip categories (best-effort,
        # backgrounded so the 404 retry never stalls the restore).
        sponsorblock.schedule_apply(player)
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
        # Re-apply the persisted audio effect, re-acquiring a filtered-players
        # ceiling slot. A stale/unknown key is dropped (resolve_preset -> None);
        # a FULL ceiling skips the effect and keeps playing, holding no slot -
        # the session simply plays unfiltered until one frees and it is re-picked.
        effect_key = row["effect"] if "effect" in row else None
        if effect_key and effects.resolve_preset(effect_key) is not None:
            result = await effects.apply_preset(
                player, effect_key, quotas=self.quotas
            )
            if result == effects.RESULT_CEILING_FULL:
                log.info(
                    "Effect '%s' skipped on restore for guild %s: filtered-player "
                    "ceiling full",
                    effect_key,
                    guild_id,
                )
            elif result == effects.RESULT_OK:
                log.info("Restored effect '%s' for guild %s", effect_key, guild_id)

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
            # Player birth: configure SponsorBlock skip categories on the node.
            sponsorblock.schedule_apply(player)

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
            # Player birth: configure SponsorBlock skip categories on the node.
            sponsorblock.schedule_apply(player)
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

    def _skip_exempt(self, player: Player, actor: typing.Any) -> bool:
        """Whether ``actor`` skips instantly, bypassing a vote (DJ or Manage Server).

        Reuses the P4 effects exemption predicate (:func:`effects.is_effect_exempt`)
        and the shared :meth:`_has_manage_guild` helper rather than re-deriving the
        DJ / manager gate, so "who is trusted to drive the room" stays one rule.
        """
        dj = getattr(player, "dj", None)
        return effects.is_effect_exempt(
            dj.id if dj is not None else None,
            getattr(actor, "id", 0),
            self._has_manage_guild(actor),
        )

    async def _request_skip(
        self, player: Player, actor: typing.Any, fallback_channel: typing.Any
    ) -> str:
        """Route a skip request: instant skip, or open/join a public vote.

        Returns :data:`voteskip.SKIP_INSTANT` when the caller should perform its
        own (unchanged) skip - a privileged actor, a room of two or fewer humans,
        or a player with nothing playing - or a vote-record outcome (which the
        caller acks ephemerally) when a public vote was opened or joined instead.
        The exempt / threshold decision is the pure :func:`voteskip.skip_mode`.
        """
        if getattr(player, "current", None) is None:
            return voteskip.SKIP_INSTANT
        channel = getattr(player, "channel", None)
        humans = voteskip.count_humans(getattr(channel, "members", ()))
        mode = voteskip.skip_mode(humans, exempt=self._skip_exempt(player, actor))
        if mode == voteskip.SKIP_INSTANT:
            return voteskip.SKIP_INSTANT
        return await self.skip_votes.open(self, player, actor, fallback_channel)

    async def _execute_skip(
        self, player: Player
    ) -> typing.Tuple[str, typing.Optional[sonolink.models.Playable]]:
        """The shared skip engine behind /skip and a passed vote (can_skip precheck).

        Returns ``(result, track)``: :data:`voteskip.SKIP_RESULT_NONE` when there
        is nothing to skip to (playback is left untouched),
        :data:`voteskip.SKIP_RESULT_ADVANCED` with the new track, or
        :data:`voteskip.SKIP_RESULT_ENDED` when the skip emptied the queue (state
        cleared). sonolink stops the player BEFORE raising QueueEmpty, so the
        can_skip precheck refuses up front instead of silencing the room.
        """
        if not can_skip(player):
            return voteskip.SKIP_RESULT_NONE, None
        try:
            track = await player.skip()
        except sonolink.QueueEmpty:
            return voteskip.SKIP_RESULT_NONE, None
        if track:
            return voteskip.SKIP_RESULT_ADVANCED, track
        guild = getattr(player, "guild", None)
        if guild is not None:
            await self._clear(guild.id)
        return voteskip.SKIP_RESULT_ENDED, None

    @commands.hybrid_command(name="skip", aliases=["next"])
    @commands.guild_only()
    async def skip(self, ctx: commands.Context) -> None:
        """Skip the current track and play the next one."""
        player = await self._require_player(ctx)
        if player is None:
            return
        # Scaled vote-skip (lot P6): a non-exempt member in a room of more than two
        # humans opens (or joins) a public vote instead of skipping outright; the
        # DJ, Manage-Server members, and tiny rooms keep the instant skip below,
        # byte-identical to before.
        decision = await self._request_skip(player, ctx.author, ctx.channel)
        if decision != voteskip.SKIP_INSTANT:
            await ctx.send(voteskip.skip_ack(decision), ephemeral=True)
            return
        result, track = await self._execute_skip(player)
        if result == voteskip.SKIP_RESULT_NONE:
            await ctx.send(_("There are no more tracks in the queue to skip to."))
        elif result == voteskip.SKIP_RESULT_ADVANCED:
            await ctx.send(
                _("Skipped to **{title}** by `{author}`.").format(
                    title=track.title, author=track.author
                )
            )
        else:
            await ctx.send(_("Skipped. The queue is now empty."))

    async def _play_previous(
        self, player: Player
    ) -> typing.Optional[sonolink.models.Playable]:
        """Step back to the previous track; the shared /previous + Back seam.

        The single engine implementation both the command and the controller
        button call. Returns the now-playing previous track on success, or None
        for a clean refusal - either there is nothing to go back to, or the most
        recent history entry can no longer be dispatched (its ``encoded`` blob is
        gone). On a None return playback, the queue and history are left EXACTLY
        as they were: the encoded guard peeks the candidate BEFORE any state is
        mutated, so a dead entry never silences the room.

        On success this defers to ``Player.previous()`` (sonolink's
        ``queue.previous()`` + a direct ``play()``): the current track is pushed
        to the FRONT of the user lane so a natural end returns the listener to
        where they were (the Rythm/Spotify convention), and the previous track is
        dispatched through the direct ``play()`` path - a REPLACED end reason on
        the outgoing track, no autoplay fire - the same seam the radio zap uses in
        ``_apply_genre``. Repeated calls step further back through history. A
        successful step writes a fresh snapshot (both the queue and the current
        track changed). Re-recording the replayed track in ``played_ids`` (via its
        own track_start) is harmless and accepted: the bounded set simply refreshes
        that id's recency.
        """
        if not can_go_previous(player):
            return None
        # Peek the exact entry Player.previous() will pop (history's right end)
        # and refuse up front if it can no longer be dispatched, so the queue /
        # history mutation inside previous() never runs against a dead track.
        candidate = player.queue.history[-1]
        if not getattr(candidate, "encoded", None):
            return None
        try:
            track = await player.previous()
        except sonolink.HistoryEmpty:
            # Unreachable after can_go_previous under the single-threaded loop
            # (nothing mutates history between the check and here), but mirrored
            # on skip's QueueEmpty catch so a future refactor cannot silence the
            # room by surprise.
            return None
        await self._snapshot(player)
        return track

    @commands.hybrid_command(name="previous", aliases=["back"])
    @commands.guild_only()
    async def previous(self, ctx: commands.Context) -> None:
        """Replay the previous track and requeue the current one."""
        player = await self._require_player(ctx)
        if player is None:
            return
        # Pre-check so the "nothing before this" case gets its own precise
        # message; a None from _play_previous past this gate means the history
        # entry is no longer playable (mirrors skip's can_skip pre-check).
        if not can_go_previous(player):
            await ctx.send(_("There's no previous track to go back to."))
            return
        track = await self._play_previous(player)
        if track is None:
            await ctx.send(
                _("I can't go back - the previous track is no longer available.")
            )
            return
        await ctx.send(
            _("Went back to **{title}** by `{author}`.").format(
                title=track.title, author=track.author
            )
        )

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

        # Always post the interactive view, even for an empty queue: the
        # empty-state still offers the Add-track affordance, so a viewer can
        # populate the queue straight from the surface (the controller's Queue
        # button reaches the same view).
        view = QueueView(self, player)
        view.message = await ctx.send(
            view=view, allowed_mentions=discord.AllowedMentions.none()
        )

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
    # Audio effects
    # ------------------------------------------------------------------

    @staticmethod
    def _has_manage_guild(actor: typing.Any) -> bool:
        """True when ``actor`` is a Member holding the Manage Server permission."""
        return (
            isinstance(actor, discord.Member)
            and actor.guild_permissions.manage_guild
        )

    @staticmethod
    def _format_retry_delay(seconds: float) -> str:
        """Humanise a quota retry_after (seconds) as a short, localised phrase."""
        seconds = max(1, int(math.ceil(seconds)))
        if seconds < 60:
            return ngettext("{count} second", "{count} seconds", seconds).format(
                count=seconds
            )
        minutes = int(math.ceil(seconds / 60))
        return ngettext("{count} minute", "{count} minutes", minutes).format(
            count=minutes
        )

    async def _run_effect_change(
        self, player: Player, guild_id: int, actor: typing.Any, key: str
    ) -> str:
        """Gate, apply and confirm an effect change; return a translated line.

        The single seam behind both /filter and the controller's ephemeral
        picker. Same-voice is enforced by the callers. Here: resolve the preset,
        spend the guild effects quota (unless the actor is the DJ or has Manage
        Server, or the change is Off), apply through the effects seam (which owns
        the filtered-players ceiling), then refresh the controller and snapshot.
        Never raises - the effects seam swallows node errors and returns a code.
        """
        preset = effects.resolve_preset(key)
        if preset is None:
            return _("That effect isn't available.")
        is_off = preset.key == effects.OFF_KEY
        dj = getattr(player, "dj", None)
        exempt = effects.is_effect_exempt(
            dj.id if dj is not None else None,
            actor.id,
            self._has_manage_guild(actor),
        )
        # Quota gate: only an ordinary listener switching to a real effect pays.
        if not is_off and not exempt and not self.quotas.effects_guild.check(guild_id):
            delay = self._format_retry_delay(
                self.quotas.effects_guild.retry_after(guild_id)
            )
            return _(
                "You're changing effects too quickly. Try again in {delay}."
            ).format(delay=delay)
        result = await effects.apply_preset(player, preset.key, quotas=self.quotas)
        if result == effects.RESULT_CEILING_FULL:
            return _(
                "A lot of servers are using effects right now - try again in a moment."
            )
        if result in (effects.RESULT_ERROR, effects.RESULT_UNKNOWN):
            return _("Something went wrong applying that effect.")
        # Success: charge the quota (non-off, non-exempt), refresh UI, persist.
        if not is_off and not exempt:
            self.quotas.effects_guild.hit(guild_id)
        controller = getattr(player, "controller", None)
        if controller is not None:
            await controller._rerender()
        await self._snapshot(player)
        if is_off:
            return _("Effects cleared.")
        return _("Effect set to {emoji} {label}.").format(
            emoji=preset.emoji, label=preset.label
        )

    @commands.hybrid_command(name="filter", aliases=["fx", "effect"])
    @commands.guild_only()
    @app_commands.describe(preset="The audio effect to apply, or Off to clear.")
    @app_commands.choices(preset=EFFECT_CHOICES)
    async def filter_command(self, ctx: commands.Context, *, preset: str) -> None:
        """Apply an audio effect preset to the current playback (Off to clear)."""
        player = await self._require_player(ctx)
        if player is None:
            return
        message = await self._run_effect_change(
            player, ctx.guild.id, ctx.author, preset
        )
        await ctx.send(message, ephemeral=True)

    # ------------------------------------------------------------------
    # Lyrics
    # ------------------------------------------------------------------

    async def _start_lyrics_follow(
        self, player: Player, result: lyrics.LyricsResult
    ) -> str:
        """Start (or replace) this guild's synced-lyrics session; return a code.

        The seam the static card's Follow button calls. Posts the live message in
        the player's home (music) channel and drives it off the timed lines. Only
        a timed result with a home channel can follow; a full process-wide ceiling
        refuses cleanly (:data:`lyrics.START_CEILING_FULL`) and the card says so.
        """
        channel = getattr(player, "home", None)
        guild = getattr(getattr(player, "channel", None), "guild", None)
        if channel is None or guild is None or not result.is_timed:
            return lyrics.START_UNAVAILABLE
        session = await self.lyrics_sessions.start(
            guild_id=guild.id,
            player=player,
            channel=channel,
            result=result,
            track=getattr(player, "current", None),
        )
        return lyrics.START_OK if session is not None else lyrics.START_CEILING_FULL

    @commands.hybrid_command(name="lyrics", aliases=["ly"])
    @commands.guild_only()
    async def lyrics_command(self, ctx: commands.Context) -> None:
        """Show the lyrics for the current track, with an optional live follow."""
        # Read-only: anyone may look up lyrics (no DJ / same-voice gate). The
        # synced follow, which posts publicly, re-checks same-voice on its button.
        player = await self._require_player(ctx, in_channel=False)
        if player is None:
            return
        if player.current is None:
            await ctx.send(_("There's nothing playing right now."), ephemeral=True)
            return

        # Two-axis rate limit, charged once PER FETCH (never per synced edit):
        # per user (stop one person hammering) and per guild (stop a whole guild
        # hammering the provider). Check both before touching the node; refuse
        # cleanly with a localised retry delay.
        user_id = ctx.author.id
        guild_id = ctx.guild.id
        if not self.quotas.lyrics_user.check(user_id):
            delay = self._format_retry_delay(
                self.quotas.lyrics_user.retry_after(user_id)
            )
            await ctx.send(
                _(
                    "You've looked up lyrics too many times. Try again in {delay}."
                ).format(delay=delay),
                ephemeral=True,
            )
            return
        if not self.quotas.lyrics_guild.check(guild_id):
            delay = self._format_retry_delay(
                self.quotas.lyrics_guild.retry_after(guild_id)
            )
            await ctx.send(
                _(
                    "This server has looked up too many lyrics. Try again in {delay}."
                ).format(delay=delay),
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)
        # Charge both axes for the fetch we are about to make: the provider is hit
        # regardless of whether any lyrics come back.
        self.quotas.lyrics_user.hit(user_id)
        self.quotas.lyrics_guild.hit(guild_id)
        result = await lyrics.fetch_lyrics(player)
        if not result.has_lyrics:
            await ctx.send(
                _("I couldn't find any lyrics for this track."), ephemeral=True
            )
            return
        await ctx.send(
            view=lyrics.StaticLyricsCard(self, player, result), ephemeral=True
        )

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
            # Player birth: configure SponsorBlock skip categories on the node.
            sponsorblock.schedule_apply(player)

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


# ---------------------------------------------------------------------------
# UI layer
# ---------------------------------------------------------------------------
# The interactive Discord UI (now-playing controller, queue view, vibe / join
# cards, their modals and selects) lives in views.py. It is imported here at the
# BOTTOM, after this module's engine helpers are defined, because views.py
# imports those helpers; music.py is always the package's import entry point (the
# loaded extension), so the import cycle resolves music-first. These view classes
# are re-bound into this module's namespace so the cog's call sites - and the
# test suite, which references them as cogs.music.music.<name> - keep working.
from cogs.music.views import (
    JoinVoiceCard,
    MusicController,
    QueueView,
    VibeCard,
)

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

from cogs.music import effects, guild_config, lyrics, sponsorblock, vibes, voteskip

# The playback engine core (Player, its YouTube-seed autoplay handler, the
# search-result normalisers and the voice-channel resolver) lives in player.py,
# the package's lowest layer. The cog uses SEARCH_SOURCE / Player / _first_track
# / _normalize_result_tracks directly; the rest are re-bound into this module's
# namespace so the test suite (which references them as cogs.music.music.<name>)
# and every runtime consumer keep working with no edit.
from cogs.music.player import (
    SEARCH_SOURCE,
    Player,
    _first_track,
    _normalize_result_tracks,
    _YouTubeSeedAutoPlayHandler,  # noqa: F401
    resolve_voice_channel,  # noqa: F401
    seed_needs_youtube_resolution,  # noqa: F401
    youtube_seed_query,  # noqa: F401
)
from cogs.music.playlists_shared import ServerPlaylistMixin
from tools import music_state, settings
from tools.i18n import _, ngettext
from tools.quotas import QuotaRegistry

log = logging.getLogger(__name__)


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

# How many now-playing panels the idle tick may refresh at once. The refreshes
# are Discord edits, so awaiting them one after another made a tick as long as
# the sum of its HTTP round-trips (a fleet with hundreds of players could push
# the 60s loop past its own period); fanning them out bounded keeps the tick
# roughly constant-time while never bursting a whole fleet at the rate limiter.
PROGRESS_REFRESH_CONCURRENCY = 10

# Hard ceiling on how many tracks ONE guild may keep waiting in its player queue,
# counted over BOTH lanes (the user lane and the hidden autoplay lane).
#
# Arbitration: 500 is deliberately generous - at a ~4 minute average that is over
# a full day of continuous music, past any real session - but it is FINITE, and
# that is the whole point. Nothing in the package bounded the queue before, so a
# scripted /play loop, or a handful of 200-track playlist loads, could grow one
# guild's in-memory queue (and the JSONB snapshot written from it) without limit;
# at 1000+ guilds that is a per-guild memory leak with no ceiling. The check is a
# len() over the two lanes, so it costs no await and no query on any path.
MAX_QUEUE_TRACKS = 500

# Cap a user's saved favourites so the table cannot grow without bound.
MAX_FAVOURITES = 100

# How many LEGACY favourites (rows saved before the `encoded` column existed, so
# they carry no blob to bulk-decode) one `/playlist play` may resolve by search.
# Every resolved row is backfilled with its blob, so a list drains into the
# one-round-trip decode path after at most ceil(100/25) runs and then never
# searches again. 25 keeps the worst case a handful of seconds instead of the
# hundred serial round trips this replaces.
FAVOURITE_SEARCH_CAP = 25

# How many of those legacy searches run at once. Small on purpose: the searches
# fan out to Lavalink, and at 1000+ guilds a burst of one-per-favourite requests
# from several members at once is exactly the load pattern that starves a node.
# Three cuts the wall time by ~3x while keeping the burst per invocation tiny.
FAVOURITE_SEARCH_CONCURRENCY = 3

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


def resolve_session_autoplay(user_pref, guild_default=None):
    """Initial autoplay for a NEW session, most specific signal first.

    Precedence, from most to least specific:

    1. ``user_pref`` - the starter's OWN saved preference (deliverable 4). An
       explicit personal choice always wins: it is the most specific signal there
       is, and the member set it deliberately.
    2. ``guild_default`` - this server's dashboard-configured default
       (``guild_config.KEY_AUTOPLAY``). A DEFAULT by definition applies only where
       nothing more specific was chosen, so it fills in for members who never
       touched their preference. ``None`` means the server never configured one.
    3. ON - the bot's own default, so autoplaying recommendations stays the
       out-of-the-box experience.

    Seeds a session's INITIAL state only; the controller toggle flips it live
    afterwards and neither signal is re-read. With both arguments absent this
    returns True, exactly as the one-argument version always did - which is what
    keeps an unconfigured guild's behaviour identical. Pure, so the whole
    precedence is unit-tested without a database or a live player.
    """
    if user_pref is not None:
        return bool(user_pref)
    if guild_default is not None:
        return bool(guild_default)
    return True


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


async def refresh_progress_bars(
    controllers: typing.Sequence[typing.Any],
    *,
    concurrency: int = PROGRESS_REFRESH_CONCURRENCY,
) -> int:
    """Advance a batch of now-playing progress bars; returns how many edits went out.

    Each controller decides for itself whether anything moved (an unchanged bar
    posts no edit at all), so this only owns the fan-out policy:

    * bounded-concurrent, like the startup restore - the edits are HTTP calls, and
      awaiting them in series made the 60s idle tick grow linearly with the number
      of active players, so a large fleet could spend its whole period editing;
    * isolated per controller - one guild's failed edit is logged and dropped, it
      can never sink the batch nor cost the other guilds their idle disconnect.

    Free when the batch is empty, so a bot with no playing player pays nothing.

    Batch membership is the caller's call and is deliberately NOT locked: when a
    voice reconnect rebinds a guild's controller between the moment the batch was
    collected and the moment these edits go out, that panel can take one redundant
    progress edit. Accepted rather than serialised - it is rare, it costs a single
    HTTP call, and the bar drawn is correct either way.
    """
    if not controllers:
        return 0

    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def _guarded(controller: typing.Any) -> bool:
        async with semaphore:
            try:
                return bool(await controller.refresh_progress())
            except Exception:
                log.exception("Failed to refresh a controller progress bar")
                return False

    results = await asyncio.gather(*(_guarded(c) for c in controllers))
    return sum(1 for edited in results if edited)


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


def queue_room_left(queue: typing.Any) -> int:
    """How many more tracks fit under :data:`MAX_QUEUE_TRACKS`. Never negative.

    Reads the SAME two lanes :func:`queued_track_count` counts, so the cap covers
    the hidden autoplay lane too and cannot be walked around by a path that fills
    it. Clamped at 0 so a queue that somehow sits over the cap (an old snapshot,
    a cap lowered between versions) reads as "no room" instead of a negative
    slice. Pure, in-memory, no await.

    Cheap but not allocation-free: sonolink's ``tracks`` / ``autoplay_tracks`` are
    COPYING properties, so each call builds two lists of up to
    :data:`MAX_QUEUE_TRACKS` pointers. Bounded and fine on the enqueue seams that
    call it once per add; do not put it in a per-tick loop.
    """
    return max(0, MAX_QUEUE_TRACKS - queued_track_count(queue))


def fit_queue_additions(
    queue: typing.Any, tracks: typing.Iterable[typing.Any]
) -> typing.Tuple[typing.List[typing.Any], int]:
    """Split ``tracks`` into ``(accepted, dropped)`` against the queue cap.

    The one seam every BULK enqueue goes through: it takes as many tracks as fit
    and reports how many were cut, so the caller can queue the head and say so
    honestly rather than silently swallowing the tail. Materialises the iterable
    once (callers pass generators and playlist track lists alike). Pure.
    """
    candidates = list(tracks)
    accepted = candidates[: queue_room_left(queue)]
    return accepted, len(candidates) - len(accepted)


def queue_full_message() -> str:
    """The refusal a SINGLE add gets when the queue already sits at the cap."""
    return _("The queue is full ({cap} tracks). Skip or clear a few first.").format(
        cap=MAX_QUEUE_TRACKS
    )


def queue_full_suffix(dropped: int) -> str:
    """Tail a BULK add appends when the cap cut it short ("" when nothing was cut).

    Deliberately the same shape as the "could not be loaded" skip line the
    playlist and favourites paths already append, so one message can carry both
    facts ("Queued 40 tracks. 3 were skipped ... 12 were not added ...") without
    inventing a second grammar for the same kind of bad news.
    """
    if dropped <= 0:
        return ""
    return ngettext(
        " {dropped} track was not added - the queue is full at {cap}.",
        " {dropped} tracks were not added - the queue is full at {cap}.",
        dropped,
    ).format(dropped=dropped, cap=MAX_QUEUE_TRACKS)


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


def queued_track_at(
    queue: typing.Any,
    index: int,
    expected: typing.Any = None,
) -> typing.Optional[typing.Any]:
    """Resolve the upcoming track at ``index``, or None when the click went stale.

    The queue browser renders absolute queue indexes into a select, and minutes
    can pass before someone picks one: the queue may have advanced, been
    shuffled, cleared or purged in between. This is the single re-check every
    per-track action runs at ACTION time, so a stale index can never raise
    IndexError - it returns None and the caller answers "that track is no longer
    in the queue".

    ``expected`` is the track object the surface was rendered from. When given,
    the slot must still hold an equal track (sonolink's ``Playable.__eq__``
    compares the ``encoded`` blob), otherwise the queue shifted under the viewer
    and the index now addresses a DIFFERENT song - refusing beats silently acting
    on the wrong one. Two byte-identical copies of the same song compare equal, so
    a duplicate at that slot is accepted: it is the very track the member picked.

    Reads only the user lane (``queue.tracks``), the lane the browser lists; the
    hidden autoplay lane is never addressable from the view. Pure - no mutation.
    """
    tracks = getattr(queue, "tracks", None) or ()
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    if index < 0 or index >= len(tracks):
        return None
    track = tracks[index]
    if expected is not None and track != expected:
        return None
    return track


def remove_queue_index(
    queue: typing.Any,
    index: int,
    expected: typing.Any = None,
) -> typing.Optional[typing.Any]:
    """Drop the upcoming track at ``index`` from the user lane, index-faithfully.

    Returns the removed track, or None when :func:`queued_track_at` says the
    index (or the track that sat there) is gone - the stale-click path, which
    never mutates anything.

    Why not ``Queue.remove_wait(track, remove_all=False)``: sonolink compares
    tracks by their ``encoded`` blob (``models/track.py::Playable.__eq__``) and
    ``BaseQueue.remove`` walks the lane dropping the FIRST equal item, so with the
    same song queued twice, a click on the SECOND copy would delete the first.
    That is not cosmetic - removing index 2 instead of index 5 out of
    ``[A, B, X, C, D, X]`` leaves ``[A, B, C, D, X]`` instead of
    ``[A, B, X, C, D]``, a different playback order. ``Queue.pop_at`` IS
    index-faithful but also PROMOTES the popped track to ``current_track`` (it is
    the jump primitive), so it cannot serve a removal either. The user lane is a
    plain ``deque`` (``Queue._items``), so the index-faithful delete is a direct
    ``del`` - the same grounded private access :func:`purge_queue_lanes` already
    makes on ``_autoplay_items``, verified against the installed sonolink source.

    ``queue.tracks`` is ``list(queue._items)``, so the index read and the delete
    address the same slot, and nothing is awaited between them.
    """
    track = queued_track_at(queue, index, expected)
    if track is None:
        return None
    del queue._items[index]
    return track


def history_entries(queue: typing.Any) -> typing.List[typing.Any]:
    """Return a player's played tracks, MOST RECENT FIRST.

    sonolink appends to ``queue.history``, so its natural order is oldest-first;
    a "what did we just play" surface reads the other way round. The lane is
    already hard-bounded at :data:`cogs.music.player.HISTORY_MAX_ITEMS`, so this
    copy is bounded too. None-safe over a queue with no history lane at all (the
    stub-sonolink dev box), returning an empty list rather than raising. Pure.

    The CURRENT track is never in this lane - sonolink pushes a track to history
    only when it is replaced - so the history card never lists what is playing.
    """
    history = getattr(queue, "history", None)
    if not history:
        return []
    return list(reversed(list(history)))


def history_entry_at(
    entries: typing.Sequence[typing.Any],
    index: int,
    expected: typing.Any = None,
) -> typing.Optional[typing.Any]:
    """Resolve the history entry at ``index``, or None when the click went stale.

    The sibling of :func:`queued_track_at` for the history card, and stale for
    the same reason: the lane keeps moving while a rendered page sits on screen
    (every finished track appends, and a full lane drops its oldest), so the
    index a member picked minutes ago may now address a DIFFERENT song. Takes the
    already-reversed :func:`history_entries` list rather than the queue, so the
    index the select rendered and the index re-checked here are the same
    most-recent-first index.

    ``expected`` is the track the option was rendered from; the slot must still
    hold an equal track (sonolink's ``Playable.__eq__`` compares the ``encoded``
    blob). Two byte-identical copies of the same song compare equal, which is
    correct here: replaying a song puts a second equal entry in the lane, and
    either one re-queues exactly the song the member picked. Pure - no mutation.
    """
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    if index < 0 or index >= len(entries):
        return None
    entry = entries[index]
    if expected is not None and entry != expected:
        return None
    return entry


def plan_favourite_resolution(
    rows: typing.Sequence[typing.Any],
    cap: int = FAVOURITE_SEARCH_CAP,
) -> typing.Tuple[
    typing.List[typing.Any], typing.List[typing.Any], int
]:
    """Split stored favourites into ``(decodable, searchable, deferred)``.

    The whole point of the ``encoded`` column: a favourite that carries its blob
    is rebuilt by ONE bulk ``decode_tracks`` call for the entire list, exactly as
    the cold restore and the shared server playlists do. Only LEGACY rows (saved
    before the column existed, or saved from a track that had no blob) need a
    network search each, so only those are capped - at ``cap`` per invocation,
    with the rest reported as deferred rather than silently dropped.

    A row with no ``uri`` either is unplayable by both paths and is dropped here
    (it counts as deferred nothing - it is simply not resolvable and shows up in
    the "could not be loaded" tally downstream).

    Order is preserved inside each bucket, so the caller can re-thread the
    resolved tracks back into the stored (newest-first) order. Pure.
    """
    decodable: typing.List[typing.Any] = []
    searchable: typing.List[typing.Any] = []
    for row in rows:
        if row["encoded"]:
            decodable.append(row)
        elif row["uri"]:
            searchable.append(row)
    safe_cap = max(cap, 0)
    deferred = max(len(searchable) - safe_cap, 0)
    return decodable, searchable[:safe_cap], deferred


def pair_decoded_favourites(
    rows: typing.Sequence[typing.Any],
    decoded: typing.Optional[typing.Sequence[typing.Any]],
) -> typing.Tuple[typing.List[typing.Tuple[typing.Any, typing.Any]], int]:
    """Zip decoded tracks back onto the rows they came from; count the failures.

    ``decode_tracks`` answers positionally, and may return ``None`` entries (a
    blob Lavalink can no longer decode) or a SHORT list (a truncated / failed
    batch); both are failures, so the skipped count is derived from the rows, not
    from the answer's length. The pairing is what
    :func:`cogs.music.playlists_shared.account_decoded` cannot give - the shared
    playlists only need a tally, whereas a favourites load must know WHICH row
    produced which track to keep the list's order. Pure.
    """
    pairs = [
        (row, track)
        for row, track in zip(rows, decoded or ())
        if track is not None
    ]
    return pairs, max(len(rows) - len(pairs), 0)


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


def is_bot_channel_move(
    is_own: bool,
    before_channel_id: typing.Optional[int],
    after_channel_id: typing.Optional[int],
) -> bool:
    """True when this voice-state change is OUR bot moving between two channels.

    A move has a real channel on BOTH sides and they differ - a mod dragging the
    bot from one room to another. A fresh connect (None -> B) and a disconnect /
    kick (B -> None) are NOT moves (sonolink's ``connect`` and the disconnect /
    cleanup paths own those, and the empty-channel auto-leave handles a leave), so
    both are excluded. Pure so the voice-state listener's bot-move branch is unit
    tested without a gateway. See :meth:`Player.on_voice_state_update` for the
    protocol-level channel sync this pairs with.
    """
    return (
        is_own
        and before_channel_id is not None
        and after_channel_id is not None
        and before_channel_id != after_channel_id
    )


class Music(ServerPlaylistMixin, commands.Cog):
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
        # NEVER evicted, deliberately: _clear used to pop a guild's lock, which
        # could destroy the very object a _send_controller was holding - the next
        # poster then created a FRESH lock, took it uncontended and posted a
        # second controller, exactly what this map exists to prevent. One
        # uncontended asyncio.Lock per guild that has ever played is a few dozen
        # bytes, binds no event loop until it is awaited, and is the same
        # unbounded-by-design trade the sibling per-guild lock maps take
        # (cogs/system/dashboard_music_actions.py _MUSIC_LOCKS,
        # cogs/system/dashboard_actions.py _AUTOROOM_LOCKS).
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

    async def _require_player(self, ctx, *, in_channel=True, control=False):
        """Return the connected player, or None after telling the user why not.

        With ``in_channel`` (the default, for control actions like skip/stop/
        volume/disconnect) the invoker must be in the bot's voice channel, so a
        bystander cannot drive playback from anywhere - the controller buttons
        already enforce this, and this makes the commands match. Read-only
        callers (queue) pass ``in_channel=False``.

        ``control`` adds the DJ/mod gate on top of same-voice for the DJ-locked
        mirror commands (pause/resume/volume/loop/shuffle/disconnect/stop/previous/
        seek/clearqueue/filter): only the session DJ or a Manage-Server member may
        drive them, and a session with no DJ opens the gate to the room. Gated
        identically to the controller buttons (:meth:`_can_control`) so typing the
        command can never bypass the button gate. The refusal is ephemeral and
        only formats the DJ mention when a DJ exists (a None DJ opens the gate).
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
        if control and not await self._can_control(player, ctx.author):
            dj = getattr(player, "dj", None)
            # A None DJ opens the gate in _can_control, so dj is a real member on
            # this deny path; guard anyway so a racing clear never crashes .mention.
            if dj is None:
                return player
            await ctx.send(
                _(
                    "Only the DJ ({dj}) or a moderator can control playback."
                ).format(dj=dj.mention),
                ephemeral=True,
            )
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

        The track's ``encoded`` blob is stored alongside the metadata so
        ``/playlist play`` can rebuild the whole list in one bulk decode instead
        of one search per track. It is read defensively (a stub / partially built
        track may not carry one), and a missing blob simply lands NULL - the
        legacy search path resolves and backfills that row later.

        A conflicting row is left ALONE (``DO NOTHING``, not a blob-filling
        ``DO UPDATE``): an update would make asyncpg report "INSERT 0 1" and this
        method would answer "added" for a track that was already saved. The
        backfill belongs to the load path, which knows it is backfilling.
        """
        query = """
            INSERT INTO music_favorites
                (user_id, identifier, title, author, uri, source_name, encoded)
            SELECT $1, $2, $3, $4, $5, $6, $7
            WHERE (SELECT COUNT(*) FROM music_favorites WHERE user_id = $1) < $8
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
            getattr(track, "encoded", None),
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
            SELECT identifier, title, author, uri, source_name, encoded
            FROM music_favorites
            WHERE user_id = $1
            ORDER BY added_at DESC
            LIMIT $2
        """
        return await self.bot.db_pool.fetch(query, user_id, MAX_FAVOURITES)

    async def delete_favourite(self, user_id: int, identifier: str) -> bool:
        """Drop one favourite by its identifier. True when a row was deleted.

        Identifier-addressed on purpose: the favourites card acts on the value
        carried by the option a member picked, so a list that changed under the
        card (a second surface removed something, an add pushed the ordering
        around) can never make a click delete a DIFFERENT track the way a
        positional delete could. False means the row was already gone, which the
        card answers by re-rendering rather than by claiming a removal.
        """
        status = await self.bot.db_pool.execute(
            "DELETE FROM music_favorites WHERE user_id = $1 AND identifier = $2",
            user_id,
            identifier,
        )
        return status.rsplit(" ", 1)[-1] != "0"

    async def _backfill_favourite_blobs(
        self, user_id: int, pairs: typing.Sequence[typing.Tuple[typing.Any, typing.Any]]
    ) -> None:
        """Store the blobs of freshly searched legacy favourites (best-effort).

        One statement for the whole batch (asyncpg is one-statement-per-call, and
        a per-row UPDATE loop would trade the serial search storm this lot
        removes for a serial write storm). ``encoded IS NULL`` keeps it a pure
        backfill: a row that gained a blob in the meantime is never rewritten,
        so two concurrent loads cannot fight over it.

        The row is addressed by its STORED identifier while the blob comes from
        the track the stored URI resolved to - that is the point (the blob is
        what plays the URI). Failure is logged and swallowed: the tracks are
        already queued, and a missed backfill only means this row is searched
        again next time.
        """
        rows = [(row["identifier"], getattr(track, "encoded", None)) for row, track in pairs]
        rows = [(identifier, blob) for identifier, blob in rows if blob]
        if not rows:
            return
        try:
            await self.bot.db_pool.execute(
                """
                UPDATE music_favorites AS f
                   SET encoded = v.encoded
                  FROM unnest($2::text[], $3::text[]) AS v(identifier, encoded)
                 WHERE f.user_id = $1
                   AND f.identifier = v.identifier
                   AND f.encoded IS NULL
                """,
                user_id,
                [identifier for identifier, _blob in rows],
                [blob for _identifier, blob in rows],
            )
        except Exception:
            log.exception("Favourite blob backfill failed")

    async def _search_favourite_rows(
        self, rows: typing.Sequence[typing.Any]
    ) -> typing.List[typing.Tuple[typing.Any, typing.Any]]:
        """Resolve legacy favourites by URI, BOUNDED-parallel, order preserved.

        The paper cut this replaces: the old load awaited one search per
        favourite, one after another, so a full list was a hundred serial
        Lavalink round trips - a stall measured in minutes on a slow node, with
        the invoker staring at a deferred response the whole time.

        Only rows with no stored blob ever reach here (the rest are one bulk
        decode), the batch is capped by the caller, and a semaphore of
        :data:`FAVOURITE_SEARCH_CONCURRENCY` keeps the fan-out per invocation
        tiny - the same bounded-concurrency shape as the startup restore, chosen
        for the same reason: never turn one member's command into a burst that a
        shared node feels. ``gather`` answers positionally, so the surviving
        pairs keep the stored order.
        """
        semaphore = asyncio.Semaphore(FAVOURITE_SEARCH_CONCURRENCY)

        async def resolve(row: typing.Any) -> typing.Optional[typing.Any]:
            async with semaphore:
                return _first_track(await self._search(row["uri"]))

        results = await asyncio.gather(
            *(resolve(row) for row in rows), return_exceptions=True
        )
        pairs: typing.List[typing.Tuple[typing.Any, typing.Any]] = []
        for row, track in zip(rows, results):
            if isinstance(track, BaseException):
                log.exception(
                    "Favourite search failed for %s", row["identifier"], exc_info=track
                )
                continue
            if track is not None:
                pairs.append((row, track))
        return pairs

    async def resolve_favourites(
        self, user_id: int, rows: typing.Sequence[typing.Any]
    ) -> typing.Tuple[typing.List[typing.Any], int, int]:
        """Rebuild playable tracks for stored favourites: ``(tracks, failed, deferred)``.

        The two-path load, in stored (newest-first) order:

        * every row that carries an ``encoded`` blob is rebuilt in ONE bulk
          ``decode_tracks`` round trip - a full 100-track list costs a single
          request, the seam ``_restore_one`` and the shared server playlists
          already use;
        * only LEGACY rows are searched, capped and bounded-parallel, and their
          blobs are backfilled so the same list never pays that cost twice.

        ``failed`` counts rows that resolved to nothing (a dead blob, a search
        that found nothing, a row with no URI at all); ``deferred`` counts legacy
        rows left for a later run by the cap. The caller states both - a load
        that silently queued 25 of 100 would be the dishonest half of the fix.
        """
        decodable, searchable, deferred = plan_favourite_resolution(rows)

        pairs: typing.List[typing.Tuple[typing.Any, typing.Any]] = []
        failed = len(rows) - len(decodable) - len(searchable) - deferred
        if decodable:
            try:
                decoded = await self.bot.sl_client.decode_tracks(
                    *[row["encoded"] for row in decodable]
                )
            except RuntimeError:
                log.exception("Favourite decode failed: no node available")
                decoded = None
            paired, skipped = pair_decoded_favourites(decodable, decoded)
            pairs.extend(paired)
            failed += skipped

        if searchable:
            found = await self._search_favourite_rows(searchable)
            failed += len(searchable) - len(found)
            pairs.extend(found)
            await self._backfill_favourite_blobs(user_id, found)

        # Re-thread both buckets into the order the member sees on their card.
        by_identifier = {row["identifier"]: track for row, track in pairs}
        tracks = [
            by_identifier[row["identifier"]]
            for row in rows
            if row["identifier"] in by_identifier
        ]
        return tracks, failed, deferred

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

    def _settings_pool(self) -> typing.Any:
        """The pool the per-guild music configuration reads through, or ``None``.

        Resolved defensively (``getattr`` down the chain) so every configuration
        read degrades to "unconfigured" - i.e. to the bot's historical behaviour -
        rather than raising, on a cog stand-in that has no bot or a bot whose pool
        is not up yet.
        """
        return getattr(getattr(self, "bot", None), "db_pool", None)

    async def _init_autoplay(self, player: Player, member_id: int) -> None:
        """Seed a NEW session's autoplay: personal preference, guild default, ON.

        Reads the starter's saved preference (deliverable 4) and this guild's
        dashboard-configured default (``guild_config.KEY_AUTOPLAY``) and hands both
        to :func:`resolve_session_autoplay`, which owns the precedence. This seeds a
        session's INITIAL state only; the controller toggle flips it live afterwards
        and never re-reads either signal. Best-effort: a settings read hiccup must
        not break playback, so a failed personal read degrades to "unset" and the
        guild read degrades to "unconfigured" (both leaving the ON default).

        With no personal preference and no guild default this arms autoplay ON,
        byte-identically to before the guild default existed.
        """
        try:
            pref = await settings.get_user(
                self.bot.db_pool, member_id, AUTOPLAY_PREF_KEY, None
            )
        except Exception:
            log.exception("Failed to read autoplay preference for %s", member_id)
            pref = None
        guild_id = getattr(getattr(player, "guild", None), "id", None)
        guild_default = await guild_config.autoplay_default(
            self._settings_pool(), guild_id
        )
        _set_autoplay(player, resolve_session_autoplay(pref, guild_default))

    async def _apply_default_volume(self, player: Player) -> None:
        """Apply this guild's configured starting volume to a BRAND-NEW player.

        Runs at player birth, before the first track is played, so the very first
        second of audio is already at the configured level. An UNCONFIGURED guild
        makes NO call at all - sonolink's own default (100) stands and the node
        sees exactly the traffic it saw before this setting existed.

        Best-effort by design: the player was connected microseconds ago and its
        node-side registration is asynchronous, so a volume PATCH can lose that
        race. Losing it costs the room a default volume, never the session, so the
        failure is logged at debug and swallowed.
        """
        volume = await guild_config.default_volume(
            self._settings_pool(), getattr(getattr(player, "guild", None), "id", None)
        )
        if volume is None:
            return
        try:
            await player.set_volume(volume)
        except Exception:
            log.debug(
                "Could not apply the configured default volume for guild %s",
                getattr(getattr(player, "guild", None), "id", None),
                exc_info=True,
            )

    async def _init_session(self, player: Player, member: typing.Any) -> None:
        """Configure a BRAND-NEW player: autoplay seed, default volume, SponsorBlock.

        THE single player-birth seam, shared by every fresh-connect entry point
        (``/play``, the vibe card's genre start, ``/playlist play`` and the shared
        server-playlist connect) so a new setting can never reach three of the four
        and be forgotten in the fourth. The caller has already assigned
        ``player.dj`` and ``player.home``; this handles only what is configurable.

        Order matters: autoplay is armed before anything can start playing, the
        volume lands before the first track, and SponsorBlock's categories PUT is
        backgrounded last so its 404-retry never delays the caller.
        """
        await self._init_autoplay(player, member.id)
        await self._apply_default_volume(player)
        if await guild_config.sponsorblock_enabled(
            self._settings_pool(), getattr(getattr(player, "guild", None), "id", None)
        ):
            sponsorblock.schedule_apply(player)

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
        # The guild's controller LOCK is deliberately NOT popped here: _clear
        # runs on disconnect / stop / idle-teardown, any of which can land while
        # a _send_controller is holding that lock across its delete+post awaits.
        # Dropping it there left the holder guarding an orphan while the next
        # poster built a fresh lock and walked straight in - two controllers, the
        # exact race the lock exists to close. See the map's note in __init__ for
        # why keeping one lock per guild is the cheap side of the trade.
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

        Three concerns share this listener: fire a pending "join a voice channel"
        watch the moment the invoker joins (or moves into) any voice channel of
        this guild; follow OUR OWN bot's move (persist + refresh the controller
        when a mod drags it to another room); then run the DJ handoff and the
        empty-channel auto-disconnect for a departing human.
        """
        if (
            not member.bot
            and after.channel is not None
            and after.channel != before.channel
        ):
            await self._fire_voice_watch(member)

        # The bot itself was dragged to another channel. Player.on_voice_state_update
        # has already pointed player.channel at the new room (and sonolink moved the
        # audio); here we persist the new voice_channel_id and refresh the controller
        # so the panel shows the room the bot is really in. Runs BEFORE the member.bot
        # early-return, which otherwise skips every bot voice event.
        bot_user = self.bot.user
        if bot_user is not None and is_bot_channel_move(
            member.id == bot_user.id,
            before.channel.id if before.channel is not None else None,
            after.channel.id if after.channel is not None else None,
        ):
            player = member.guild.voice_client
            if isinstance(player, sonolink.Player):
                # Belt-and-suspenders: make the channel ref the new room even if this
                # listener happened to run before the protocol handler updated it, so
                # the snapshot never persists the stale voice_channel_id.
                player.channel = after.channel
                await self._snapshot(player)
                controller = getattr(player, "controller", None)
                if controller is not None:
                    await controller._rerender()

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
            # Panels to advance this tick, collected here and refreshed together
            # once the (fast, local) idle bookkeeping is done - see
            # refresh_progress_bars for why the edits must not be serialised.
            pending: typing.List[typing.Tuple[Player, typing.Any]] = []
            for voice_client in list(self.bot.voice_clients):
                if not isinstance(voice_client, Player):
                    continue
                # Refresh the persisted snapshot: volume / loop / pause / position
                # drift between the event-driven snapshots.
                if voice_client.current is not None:
                    await self._snapshot(voice_client)
                    # Same tick advances the now-playing progress bar. The view
                    # itself decides whether anything moved, so an unchanged bar
                    # (paused player, live stream, long track between segments)
                    # posts no edit.
                    controller = getattr(voice_client, "controller", None)
                    if controller is not None:
                        pending.append((voice_client, controller))
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
            # Advance every surviving panel at once. A player torn down above has
            # had its controller dropped (and its message deleted), so it is
            # filtered out here rather than edited into a 404.
            await refresh_progress_bars(
                [
                    controller
                    for player, controller in pending
                    if getattr(player, "controller", None) is controller
                ]
            )
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

        # Read the guild's SponsorBlock choice BEFORE connecting: the categories
        # PUT below is the only birth configuration a restore applies, and doing
        # the settings read here keeps the connect -> wire-up sequence free of any
        # new await, so the window in which a player exists without its .home /
        # .dj is exactly the one that already existed.
        sponsorblock_on = await guild_config.sponsorblock_enabled(
            self._settings_pool(), guild_id
        )

        # Rejoin and replay at the extrapolated position. The track_start event
        # posts a fresh controller, but a track restored in a paused state (or a
        # missed/late event) emits no track_start, so we also post one
        # explicitly below.
        player = guild.voice_client
        if not isinstance(player, Player):
            player = await channel.connect(cls=Player)
        player.home = home
        player.dj = dj
        # Player birth: hand the node its SponsorBlock skip categories (best-effort,
        # backgrounded so the 404 retry never stalls the restore) - unless this
        # guild turned SponsorBlock off, in which case the restored session comes
        # back with segment skipping disarmed, like a fresh one would.
        #
        # The rest of a restore is deliberately NOT re-configured from the guild
        # settings: the persisted volume and autoplay mode below are THIS session's
        # own live state, and a cold restart must resume the session it saved, not
        # reset it to the server defaults.
        if sponsorblock_on:
            sponsorblock.schedule_apply(player)
        player.queue.mode = loop_mode
        # Restores obey the cap too, defensively: a snapshot written by a build
        # from before MAX_QUEUE_TRACKS existed can carry more rows than the cap
        # now allows, and the restore must not be the one path that quietly
        # reintroduces an unbounded queue. Silent by design - a cold restore
        # speaks through the controller it reposts, not through chat, and there
        # is no invoker standing there to tell.
        restored, dropped = fit_queue_additions(player.queue, queue_tracks or [])
        for track in restored:
            player.queue.put(track)
        if dropped:
            log.warning(
                "Restore for guild %s dropped %d queued track(s) over the %d cap",
                guild_id,
                dropped,
                MAX_QUEUE_TRACKS,
            )
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

        # Opt-in search picker: for a NON-URL slash query when the member enabled
        # it, this shows the top matches and returns True (fully handled). It
        # returns False for a URL, a prefix invocation or the preference OFF (the
        # default), so the two lines below run byte-identically to before.
        if await search.maybe_play_picker(self, ctx, query):
            return

        await ctx.defer()
        await self._play_query(ctx, query)

    @commands.hybrid_command(name="search")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @app_commands.describe(
        query="What to search for - a song, album, artist or playlist."
    )
    async def search_cmd(self, ctx: commands.Context, *, query: str) -> None:
        """Browse tracks, albums, artists and playlists, and queue any of them.

        Opens an ephemeral tabbed browser powered by the LavaSearch plugin. No
        voice channel is needed to browse; you are only asked to join when you
        pick something to play.
        """
        await search.run_search(self, ctx, query)

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
            # Player birth: autoplay seed, this guild's default volume, and the
            # SponsorBlock categories - the one shared configuration seam.
            await self._init_session(player, ctx.author)

        if player.home is None:
            player.home = ctx.channel
        elif player.home != ctx.channel:
            await ctx.send(
                _("The player is already active in {channel}.").format(
                    channel=player.home.mention
                )
            )
            return

        # The cap is checked BEFORE the search: a full queue cannot take this
        # result either way, so refusing here spares the node a round trip that a
        # thousand guilds could otherwise fire at it for nothing. It also leaves
        # the single-track branch below with at least one slot guaranteed.
        if queue_room_left(player.queue) <= 0:
            await ctx.send(queue_full_message())
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
            # A playlist is a bulk add: queue the head that fits and state the
            # tail that did not, rather than refusing a 300-track load outright
            # over the last few slots.
            queued, dropped = fit_queue_additions(player.queue, data.tracks)
            if not queued:
                # The queue filled during the search: nothing landed, so say the
                # plain refusal rather than "added the playlist (0 tracks)".
                await ctx.send(queue_full_message())
                return
            for track in queued:
                track.extras.requester = ctx.author.id
            player.queue.put(queued)
            # Plural-aware: the cap can truncate a playlist down to exactly ONE
            # accepted track, so "(1 tracks)" is now an ordinary outcome rather
            # than the edge case a 1-track playlist used to be. Same ngettext
            # shape the favourites path uses for the identical fact.
            await ctx.send(
                ngettext(
                    "Added the playlist **{name}** ({count} track) to the queue.",
                    "Added the playlist **{name}** ({count} tracks) to the queue.",
                    len(queued),
                ).format(name=data.name, count=len(queued))
                + queue_full_suffix(dropped)
            )
        else:
            # Re-checked at the put: the search above is an await, so a bulk load
            # can have taken the slot the pre-search check saw.
            if queue_room_left(player.queue) <= 0:
                await ctx.send(queue_full_message())
                return
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
        # The seed obeys the cap and RETURNS only what it queued, so both callers'
        # "({count} track(s))" line stays honest without either of them learning
        # about the cap. A zap (replace=True) purged both lanes a few lines up, so
        # it always has the full room; only a seed onto a loaded queue can be cut.
        tracks, dropped = fit_queue_additions(player.queue, tracks)
        if dropped:
            log.info(
                "Genre seed for %s trimmed by the queue cap: %d track(s) dropped",
                genre.key,
                dropped,
            )
        if not tracks:
            # Only reachable on the non-replace path (a zap purged both lanes
            # just above, so it always has the full cap to spend): the queue was
            # already full, so the caller reports "nothing found right now" and
            # the player is left exactly as it was.
            return tier, []
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
            # A refill is best-effort filler, so a full queue simply skips this
            # cycle - SILENTLY and before the two searches, since there is nobody
            # to tell (no invoker, no interaction) and a chat line about a queue
            # nobody asked to extend would be pure noise. The station is unharmed:
            # the next track-start retries, and by then the queue has drained.
            if queue_room_left(player.queue) <= 0:
                log.info(
                    "Radio refill for %s skipped: queue at the %d cap (guild=%s)",
                    genre.key,
                    MAX_QUEUE_TRACKS,
                    guild_id,
                )
                return
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
            # Re-check the cap at the put: the two searches above are awaits, and
            # a bulk add can land in that window. Truncate to whatever fits (the
            # log below then reports the real +N), still silently.
            tracks, dropped = fit_queue_additions(player.queue, tracks)
            if not tracks:
                log.info(
                    "Radio refill for %s dropped all %d track(s): queue hit the "
                    "%d cap mid-search (guild=%s)",
                    genre.key,
                    dropped,
                    MAX_QUEUE_TRACKS,
                    guild_id,
                )
                return
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
            # Player birth: autoplay seed, this guild's default volume, and the
            # SponsorBlock categories - the one shared configuration seam.
            await self._init_session(player, author)
        if player.home is None:
            player.home = interaction.channel

        replace = player.current is not None
        # A pick that REPLACES a live session is the same destructive station zap
        # as the controller's station select, so it takes the same DJ/mod gate
        # (only the DJ or a Manage-Server member may zap; a no-DJ session opens).
        # Starting from silence stays open - the vibe card is the /play entry for
        # everyone. Reuses the station wording (no new msgid), the whole-room seam
        # via _can_control, so this can never drift from _change_station.
        if replace and not await self._can_control(player, author):
            dj = getattr(player, "dj", None)
            # replace implies a live session and _can_control opens on a None DJ,
            # so dj is a real member here; guard anyway against a racing clear.
            if dj is not None:
                await interaction.followup.send(
                    _("Only the DJ ({dj}) can change the station.").format(
                        dj=dj.mention
                    ),
                    ephemeral=True,
                )
                return
        # A zap purges both lanes before it seeds, so only the start-from-silence
        # pick can meet a full queue. Say so plainly here: the seed seam refuses
        # by returning nothing, and "I couldn't find any tracks" would be a lie.
        if not replace and queue_room_left(player.queue) <= 0:
            await interaction.followup.send(queue_full_message(), ephemeral=True)
            return
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
        player = await self._require_player(ctx, control=True)
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
        player = await self._require_player(ctx, control=True)
        if player is None:
            return
        if not player.paused:
            await ctx.send(_("The player is not paused."))
            return
        await player.resume()
        # Persist the resumed flag now; it drives the restore position maths.
        await self._snapshot(player)
        await ctx.send(_("Resumed the player."))

    async def _is_music_manager(
        self, player: Player, actor: typing.Any
    ) -> bool:
        """Whether ``actor`` holds this guild's MUSIC-manager privilege.

        THE one place the "Manage Server" half of every music gate is decided, so
        the per-guild DJ role (``guild_config.KEY_DJ_ROLE``) reaches the skip
        exemption, the playback-control lock and the effects quota exemption
        together instead of being wired into one of the three.

        A guild with no DJ role configured gets exactly ``_has_manage_guild``
        back - the same boolean, from the same check - so an untouched guild's
        privilege rule is unchanged. When a role IS configured, holding it is
        equivalent to Manage Server for music and NOTHING else; this never grants
        a moderation permission, it only opens the music gates.

        The read rides the ``tools.settings`` LRU (one shared guild blob, evicted
        by the dashboard's ``music_config`` notification), so a warm guild costs a
        dict lookup, not a query. Manage Server is checked FIRST and short-circuits,
        so the common privileged path does not read settings at all.
        """
        if self._has_manage_guild(actor):
            return True
        # Identity, not truthiness: a duck-typed stand-in could be falsy without
        # being absent, and falling through to the actor's guild would then read
        # the wrong (or no) configuration.
        guild = getattr(player, "guild", None)
        if guild is None:
            guild = getattr(actor, "guild", None)
        role_id = await guild_config.dj_role_id(
            self._settings_pool(), getattr(guild, "id", None)
        )
        return guild_config.member_has_role(actor, role_id)

    async def _privileged(
        self, predicate: typing.Any, player: Player, actor: typing.Any
    ) -> bool:
        """Run a pure privilege predicate, resolving the manager bit only if it matters.

        ``predicate`` is one of the pure ``effects`` decisions
        (:func:`effects.can_control_playback` or :func:`effects.is_effect_exempt`),
        both of which take ``(dj_id, actor_id, has_manage_guild)`` and are MONOTONE
        in that last argument: a manager is never refused something a plain listener
        is allowed. So asking the predicate with ``False`` first and only consulting
        :meth:`_is_music_manager` when that says no is EXACTLY equivalent - the pure
        predicate still owns the rule on both branches, and neither branch can
        answer something the one-call version would not have.

        Why bother: the two commonest gated paths are already decided without the
        manager bit - a session with no DJ (every restored session, and any session
        whose DJ left) opens the control gate outright, and the DJ acting on their
        own session is exempt everywhere. Both would otherwise pay a settings read
        whose result is discarded, which on a cold guild blob is a real database
        round trip inside a button callback. Warm reads were already free; this
        makes the hot paths free even on an LRU miss.
        """
        dj = getattr(player, "dj", None)
        dj_id = dj.id if dj is not None else None
        actor_id = getattr(actor, "id", 0)
        if predicate(dj_id, actor_id, False):
            return True
        return predicate(
            dj_id, actor_id, await self._is_music_manager(player, actor)
        )

    async def _skip_exempt(self, player: Player, actor: typing.Any) -> bool:
        """Whether ``actor`` skips instantly, bypassing a vote (privileged actor).

        Reuses the P4 effects exemption predicate (:func:`effects.is_effect_exempt`)
        and the shared :meth:`_is_music_manager` helper rather than re-deriving the
        DJ / manager gate, so "who is trusted to drive the room" stays one rule.
        Runs it through :meth:`_privileged`, which spares the settings read on the
        paths the predicate already decides (here: the session DJ).
        """
        return await self._privileged(effects.is_effect_exempt, player, actor)

    async def _can_control(self, player: Player, actor: typing.Any) -> bool:
        """Whether ``actor`` may drive the DJ-locked playback controls for ``player``.

        THE single gate behind every DJ-locked command and controller button:
        reuses the effects "trusted to drive the room" predicate plus the
        no-DJ-opens fallback (:func:`effects.can_control_playback`), threading the
        shared :meth:`_is_music_manager` check so the rule lives in exactly one
        place and a button gate can never drift from its mirror command. Same-voice
        is enforced separately (``_require_player``/``_ensure_in_voice``).

        Goes through :meth:`_privileged`, so the paths the predicate already
        decides - a session with no DJ, or the DJ driving their own session - cost
        no settings read at all.
        """
        return await self._privileged(effects.can_control_playback, player, actor)

    async def _request_skip(
        self, player: Player, actor: typing.Any, fallback_channel: typing.Any
    ) -> str:
        """Route a skip request: instant skip, or open/join a public vote.

        Returns :data:`voteskip.SKIP_INSTANT` when the caller should perform its
        own (unchanged) skip - a privileged actor, a room of two or fewer humans,
        a player with nothing playing, or a guild that turned vote-skip off - or a
        vote-record outcome (which the caller acks ephemerally) when a public vote
        was opened or joined instead. The exempt / threshold decision is the pure
        :func:`voteskip.skip_mode`.

        The per-guild vote-skip toggle (``guild_config.KEY_VOTESKIP``, default ON)
        is read HERE, the one routing point every VOTING skip surface passes
        through: ``/skip`` and the controller's Skip button both end up in this
        method, so turning votes off cannot leave one of them still voting. Off
        means everyone who is allowed to ask for a skip at all - the same-voice
        gate upstream still applies - skips directly, with no vote message posted.

        The dashboard's skip executor deliberately does NOT come through here: it
        calls ``_execute_skip`` directly because the dashboard's own Manage-Guild
        gate already authorised it (see
        ``cogs/system/dashboard_music_actions._exec_music_skip``), and a
        Manage-Server actor is vote-exempt on this path anyway. A NEW skip surface
        open to ordinary listeners must route through this method, not around it.
        """
        if getattr(player, "current", None) is None:
            return voteskip.SKIP_INSTANT
        guild_id = getattr(getattr(player, "guild", None), "id", None)
        if not await guild_config.voteskip_enabled(self._settings_pool(), guild_id):
            return voteskip.SKIP_INSTANT
        channel = getattr(player, "channel", None)
        humans = voteskip.count_humans(getattr(channel, "members", ()))
        mode = voteskip.skip_mode(
            humans, exempt=await self._skip_exempt(player, actor)
        )
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
        player = await self._require_player(ctx, control=True)
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
        player = await self._require_player(ctx, control=True)
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
        # Nudge a live synced-lyrics session so it resyncs to the new position at
        # once rather than waiting out its current sleep (best-effort: no-op when
        # no session is following in this guild).
        session = self.lyrics_sessions.get(ctx.guild.id)
        if session is not None:
            session.nudge()
        await ctx.send(
            _("Jumped to {position}.").format(position=format_clock(target_ms))
        )

    @commands.hybrid_command(name="stop")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context) -> None:
        """Stop playback and clear the queue (stays connected)."""
        player = await self._require_player(ctx, control=True)
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
        player = await self._require_player(ctx, control=True)
        if player is None:
            return
        await player.set_volume(value)
        await ctx.send(_("Set the volume to {volume}%.").format(volume=value))

    @commands.hybrid_command(name="shuffle", aliases=["mix"])
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context) -> None:
        """Shuffle the upcoming tracks in the queue."""
        player = await self._require_player(ctx, control=True)
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
        player = await self._require_player(ctx, control=True)
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
        player = await self._require_player(ctx, control=True)
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

    # NOT named "history": the moderation cog's /cases already claims that as an
    # alias, and a prefix user typing ?history must keep reaching the moderation
    # case log they have always reached (test_command_tree_hygiene enforces it).
    @commands.hybrid_command(name="played", aliases=["hist", "recent"])
    @commands.guild_only()
    async def played(self, ctx: commands.Context) -> None:
        """Show what this session has already played, newest first."""
        # Read-only browse, so the same gate as /queue: connected player, no
        # same-voice requirement to LOOK. Re-queueing from the card is gated on
        # the card itself (same-voice, like the queue view's Add track).
        player = await self._require_player(ctx, in_channel=False)
        if player is None:
            return

        entries = history_entries(player.queue)
        if not entries:
            await ctx.send(_("Nothing has played yet in this session."))
            return

        view = HistoryCard(self, player)
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
        player = await self._require_player(ctx, control=True)
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
        spend the guild effects quota (unless the actor is the DJ or a music
        manager, or the change is Off), apply through the effects seam (which owns
        the filtered-players ceiling), then refresh the controller and snapshot.
        Never raises - the effects seam swallows node errors and returns a code.

        The exemption goes through :meth:`_privileged` (hence
        :meth:`_is_music_manager`), not the raw Manage-Server check, so a guild's
        configured DJ role is exempt from the effects quota exactly as it is exempt
        from the vote and the control lock - one privilege rule, three gates, and
        the same "read the setting only when it can change the answer" ordering.
        """
        preset = effects.resolve_preset(key)
        if preset is None:
            return _("That effect isn't available.")
        is_off = preset.key == effects.OFF_KEY
        exempt = await self._privileged(effects.is_effect_exempt, player, actor)
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

    @commands.hybrid_command(name="filter", aliases=["fx", "effect", "effects", "filters"])
    @commands.guild_only()
    @app_commands.describe(preset="The audio effect to apply, or Off to clear.")
    @app_commands.choices(preset=EFFECT_CHOICES)
    async def filter_command(self, ctx: commands.Context, *, preset: str) -> None:
        """Apply an audio effect preset to the current playback (Off to clear)."""
        player = await self._require_player(ctx, control=True)
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
        """Send the interactive favourites card for a member (newest first).

        Own list: every listed track carries Play / Remove actions, so removing
        one is a pick rather than a number typed against a listing that may have
        moved. Someone else's list stays a read-only card - their saved tracks
        are theirs to manage.
        """
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

        card = FavouritesCard(self, ctx.author.id, member, list(rows))
        card.message = await ctx.send(
            view=card, allowed_mentions=discord.AllowedMentions.none()
        )

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
        """Queue every track in your favourites and start playing.

        Resolve first, connect second - the sibling ``/serverplaylist play``
        shape, so a load that turns up nothing never drags the bot into a voice
        channel to announce its own failure.
        """
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

        # Cheap refusal BEFORE the resolve. The connect seam below says the same
        # thing, but reaching it costs up to a full batch of Lavalink searches
        # (legacy favourites carry no blob) plus a backfill write - work for a
        # caller who cannot hear the result. Word-for-word the connect seam's
        # line, so there is one refusal wording, not two.
        if ctx.voice_client is None and (
            not ctx.author.voice or not ctx.author.voice.channel
        ):
            await ctx.send(_("You must be in a voice channel first."))
            return

        # Second cheap refusal, same reasoning as the one above: a queue already
        # at the cap has no room for ANY of these, and the resolve below can cost
        # a full batch of Lavalink searches plus a backfill write for a caller who
        # will hear none of it. Only a live player can be full - no voice client
        # (or one with no queue at all) reads as "all the room in the world", so
        # a session about to be born is never refused here.
        if queue_room_left(getattr(ctx.voice_client, "queue", None)) <= 0:
            await ctx.send(queue_full_message())
            return

        tracks, skipped, deferred = await self.resolve_favourites(ctx.author.id, rows)
        if not tracks:
            message = _("None of your favourites could be loaded right now.")
            if deferred:
                # The cap is stated even when the batch it covered failed, so a
                # list whose loadable half is still queued behind it never looks
                # like it was tried in full. Same wording as the success path.
                message += ngettext(
                    " {deferred} older favourite is still waiting - run this again"
                    " to add it.",
                    " {deferred} older favourites are still waiting - run this again"
                    " to add them.",
                    deferred,
                ).format(deferred=deferred)
            await ctx.send(message)
            return

        player = await self._connect_for_playlist(ctx)
        if player is None:
            return

        # The cap decides last, at the put: the connect and the resolve above are
        # awaits, so the queue can have grown since the cheap refusal. Queue the
        # head that fits and state the tail below.
        tracks, over_cap = fit_queue_additions(player.queue, tracks)
        if not tracks:
            await ctx.send(queue_full_message())
            return

        for track in tracks:
            track.extras.requester = ctx.author.id
            player.queue.put(track)

        # Playing favourites is an explicit choice: it ends any radio session.
        player.radio_genre = None
        if not player.current:
            await player.play(player.queue.get())
        await self._snapshot(player)

        count = len(tracks)
        message = ngettext(
            "Queued {count} track from your favourites.",
            "Queued {count} tracks from your favourites.",
            count,
        ).format(count=count)
        if skipped:
            # Byte-identical to the shared-playlist skip line on purpose: same
            # fact, same wording, one msgid already translated in every locale.
            message += ngettext(
                " {skipped} track was skipped - it could not be loaded.",
                " {skipped} tracks were skipped - they could not be loaded.",
                skipped,
            ).format(skipped=skipped)
        if deferred:
            # Honest about the cap instead of pretending the list is done: these
            # are older favourites with no stored blob, and every run converts a
            # batch of them, so running it again finishes the job for good.
            message += ngettext(
                " {deferred} older favourite is still waiting - run this again"
                " to add it.",
                " {deferred} older favourites are still waiting - run this again"
                " to add them.",
                deferred,
            ).format(deferred=deferred)
        message += queue_full_suffix(over_cap)
        await ctx.send(message)

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
        """Remove the favourite at the given position in your list.

        Kept for back-compat (and for anyone who prefers typing), but the card
        ``/playlist`` opens is the primary surface now: it removes by pick, so
        there is no position to miscount. Both paths end at the same
        identifier-addressed delete.
        """
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
        await self.delete_favourite(ctx.author.id, row["identifier"])
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
# cards, their modals and selects) lives in views.py, and the /search browser and
# /play picker in search.py. Both are imported here at the BOTTOM, after this
# module's engine helpers are defined, because views.py imports those helpers at
# module load (and search.py imports _ModalPlayContext from views lazily, only
# inside its pick callbacks); music.py is always the package's import entry point
# (the loaded extension), so the cycle resolves music-first. The view classes are
# re-bound into this module's namespace so the cog's call sites - and the test
# suite, which references them as cogs.music.music.<name> - keep working.
from cogs.music import search
from cogs.music.views import (
    FavouritesCard,
    HistoryCard,
    JoinVoiceCard,
    MusicController,
    QueueView,
    VibeCard,
)

"""Track lyrics for the music player: a static card and a live, synced follow.

This module owns one concern end to end - fetching a track's lyrics from the
node's ``lyrics`` plugin (DuncteBot java-timed-lyrics, reported as ``lyrics`` in
``/v4/info``) and presenting them two ways:

* a **static card** - the full lyrics, paginated when long, sent ephemerally by
  ``/lyrics``. Every track that has any lyrics gets this.
* a **synced session** - for tracks whose lyrics are *timed*, a single public
  message in the music channel that edits itself to follow playback, the current
  line in bold with a little context around it. Plain-text-only tracks get the
  static card plus a gentle note that sync is unavailable.

Layering mirrors ``sponsorblock.py`` / ``effects.py``:

* The parsing, current-line selection, window rendering, text pagination and the
  edit-cadence decision are all **pure** - no discord, no sonolink, no i18n, no
  clock behind the caller's back - so they unit-test without any backend.
* The single node-touching seam is :func:`fetch_lyrics`, which reuses sonolink's
  authenticated ``node.send`` (the node's own credentialed HTTP client, exactly
  the SponsorBlock precedent), so no credentials are handled here and the module
  imports cleanly under the stubbed sonolink on the dev box (no sonolink import).
* The Discord UI (the two Components V2 cards) and the per-guild session machinery
  live here too, self-contained, so ``lyrics.py`` stays a leaf the cog imports.

Scale. A synced session sleeps precisely until the next line boundary (via the
pure :func:`next_wake` scheduler) rather than polling on a fixed clock, so the
bold line turns over exactly at the transition instead of lagging up to a tick.
Each wake reads the extrapolated player position, picks the current line - O(1),
no I/O - and edits its one Discord message only when that line index actually
changed since the last edit. The scheduler enforces a ``MIN_EDIT_GAP`` (1.5 s)
floor between edits (machine-gun rap lines coalesce onto the first allowed
transition) and a ``MAX_TICK_SLEEP`` (8 s) ceiling (a periodic re-check so a
seek, a pause or clock drift can never strand the session), which bounds a
session to at most one autonomous edit per 1.5 s. The number of live sessions is capped
process-wide by the ``synced_lyrics`` :class:`~tools.quotas.GlobalCeiling` (25),
acquired per session and released on every stop path, so the background work and
the per-cog session map are bounded by 25 regardless of guild count.
"""

from __future__ import annotations

import asyncio
import bisect
import dataclasses
import functools
import logging
import re
import time
import typing
import urllib.parse

import discord

from tools.formats import random_colour
from tools.i18n import _

if typing.TYPE_CHECKING:  # the cog type is only used in string annotations here
    from cogs.music.music import Music, Player

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables. Every knob lives here so the cadence is documented in one place.
# ---------------------------------------------------------------------------

# Minimum seconds between two edits of a synced session's message. The scheduler
# never wakes to edit sooner than this after the previous edit, so a run of
# machine-gun lines coalesces onto the first transition this floor allows. At the
# 1.5 s floor a channel sees at most one autonomous edit / 1.5 s (0.67 / s), still
# under Discord's per-channel edit budget (5 / 5 s = 1 / s). (The offset buttons
# add user-paced edits on top, but those are interaction-driven and rate-limited
# by Discord's own bucket - see the SCALE STORY.)
MIN_EDIT_GAP = 1.5

# Longest a session ever sleeps before re-checking. It caps the lag after a seek,
# a resume or extrapolation drift (worst case <= this), and is the fallback sleep
# when there is no upcoming line boundary to aim at (an outro, or a paused
# player). Purely in-process work on wake, so a periodic re-check is cheap.
MAX_TICK_SLEEP = 8.0

# Lavalink pushes a position update only every ~5 s, so the locally extrapolated
# position can trail the node's by a few hundred ms. Waking this much AFTER the
# nominal line start makes the edit land on the new line rather than a hair
# before it (which would render the old line and need a second edit to correct).
DRIFT_MARGIN = 0.15

# Per-session manual calibration. Musixmatch times its lines against the YouTube
# MUSIC audio, but the track actually playing is often the music VIDEO (intros,
# skits, outros) - a roughly constant offset no scheduler can infer. The live card
# gives listeners buttons to nudge a per-session ``offset_ms`` (added to the
# player position before line selection) until the bold line lands on the beat.
# The steps the buttons apply and the symmetric bound they clamp to:
OFFSET_STEP_SMALL_MS = 1000
OFFSET_STEP_LARGE_MS = 5000
OFFSET_LIMIT_MS = 30000

# Context around the current line in the synced window: how many lines to show
# before it and after it. The current line renders bold between them.
CONTEXT_BEFORE = 1
CONTEXT_AFTER = 2

# Character budget for one page of the static card. A Components V2 message caps
# near 4000 chars total; 1800 leaves ample room for the heading, footer and
# buttons while keeping a page comfortably readable.
PAGE_CHAR_LIMIT = 1800

# Rendered in place of an empty timed line (an instrumental break) so the current
# marker still shows something rather than an empty bold run.
_INSTRUMENTAL = "\U0001f3b5"  # musical note


# parse_lyrics result kinds.
KIND_TIMED = "timed"  # has line-level timestamps -> eligible for the synced mode
KIND_PLAIN = "plain"  # plain text only -> static card, no sync
KIND_NONE = "none"  # nothing usable came back

# Sentinel line index meaning "before the first line" (an intro / lead-in).
BEFORE_FIRST = -1

# _start_lyrics_follow / registry.start result codes (the cog maps each to a
# translated line, keeping this module i18n-light for the control flow).
START_OK = "ok"
START_CEILING_FULL = "ceiling_full"
START_UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Pure structures.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TimedLine:
    """One timed lyric line: when it starts (ms) and its text.

    ``content`` may be empty for an instrumental beat; the renderer substitutes a
    visible marker so the line still reads as "here now".
    """

    start_ms: int
    content: str


@dataclasses.dataclass(frozen=True)
class LyricsResult:
    """The parsed outcome of a lyrics fetch.

    ``kind`` is one of :data:`KIND_TIMED` / :data:`KIND_PLAIN` / :data:`KIND_NONE`.
    Only a timed result carries ``lines``; only a plain result carries ``text``.
    ``source`` is the provider name the plugin reported (e.g. ``LyricFind``), or
    None when it was absent.
    """

    kind: str
    source: typing.Optional[str]
    lines: typing.Tuple[TimedLine, ...]
    text: str

    @property
    def is_timed(self) -> bool:
        return self.kind == KIND_TIMED

    @property
    def has_lyrics(self) -> bool:
        return self.kind != KIND_NONE


_NONE_RESULT = LyricsResult(KIND_NONE, None, (), "")


# ---------------------------------------------------------------------------
# Pure parsing.
# ---------------------------------------------------------------------------


def _line_start_ms(entry: typing.Mapping[str, typing.Any]) -> typing.Optional[int]:
    """Extract a timed line's start (ms) from either plugin dialect, or None.

    The java-timed-lyrics plugin nests it as ``range.start``; the LavaLyrics
    dialect uses a flat ``timestamp``. Support both so the parser is robust to a
    node swap, and reject anything non-numeric (a malformed entry is skipped, not
    guessed at).
    """
    rng = entry.get("range")
    if isinstance(rng, typing.Mapping):
        start = rng.get("start")
        if isinstance(start, (int, float)) and not isinstance(start, bool):
            return int(start)
    ts = entry.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return int(ts)
    return None


def _parse_timed_lines(raw_lines: typing.Sequence[typing.Any]) -> list[TimedLine]:
    """Turn the plugin's ``lines`` array into sorted :class:`TimedLine` values.

    Entries without a numeric start or a string body are dropped. The result is
    sorted ascending by start so the current-line search can bisect it, and so a
    provider that returns lines out of order still renders correctly.
    """
    out: list[TimedLine] = []
    for entry in raw_lines:
        if not isinstance(entry, typing.Mapping):
            continue
        start = _line_start_ms(entry)
        if start is None:
            continue
        content = entry.get("line")
        if not isinstance(content, str):
            continue
        out.append(TimedLine(max(0, start), content))
    out.sort(key=lambda line: line.start_ms)
    return out


def parse_lyrics(payload: typing.Any) -> LyricsResult:
    """Parse a raw lyrics payload into a :class:`LyricsResult` (pure, never raises).

    Keys off the DATA that is present rather than trusting the ``type`` field: a
    non-empty ``lines`` array wins as timed; otherwise a non-empty ``text`` string
    is plain; anything else (None, a non-dict, an empty/garbled body) degrades to
    :data:`KIND_NONE`. This tolerates a provider that mislabels ``type`` or omits
    it entirely.
    """
    if not isinstance(payload, typing.Mapping):
        return _NONE_RESULT

    source = payload.get("source") or payload.get("provider")
    if not isinstance(source, str) or not source.strip():
        source = None
    else:
        source = source.strip()

    raw_lines = payload.get("lines")
    if isinstance(raw_lines, typing.Sequence) and not isinstance(raw_lines, (str, bytes)):
        timed = _parse_timed_lines(raw_lines)
        if timed:
            return LyricsResult(KIND_TIMED, source, tuple(timed), "")

    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return LyricsResult(KIND_PLAIN, source, (), text.strip())

    return LyricsResult(KIND_NONE, source, (), "")


# ---------------------------------------------------------------------------
# Pure selection / rendering.
# ---------------------------------------------------------------------------


def current_line_index(lines: typing.Sequence[TimedLine], position_ms: int) -> int:
    """Index of the line active at ``position_ms``; :data:`BEFORE_FIRST` if none.

    ``lines`` is sorted ascending by ``start_ms``. The active line is the last one
    whose start is at or before the position; a position exactly on a line's start
    selects that line (that beat has arrived). A position before the first line's
    start returns :data:`BEFORE_FIRST` (an intro / lead-in). O(log n) via bisect.
    """
    return (
        bisect.bisect_right(lines, position_ms, key=lambda line: line.start_ms) - 1
    )


def clamp_offset(offset_ms: int, *, limit: int = OFFSET_LIMIT_MS) -> int:
    """Clamp a calibration offset to +/- ``limit`` ms (pure, symmetric)."""
    return max(-limit, min(int(offset_ms), limit))


def format_offset(offset_ms: int) -> str:
    """Render an offset (ms) as signed seconds with one decimal, e.g. ``+1.0`` (pure).

    Uses an ASCII sign and never a Unicode minus; the footer template appends the
    trailing ``s`` so this returns only the number ("+2.5", "-5.0", "+0.0").
    """
    return f"{offset_ms / 1000.0:+.1f}"


def _fmt_line(content: str, *, bold: bool) -> str:
    """Render one lyric line, substituting a marker for an instrumental gap."""
    text = content.strip() or _INSTRUMENTAL
    return f"**{text}**" if bold else text


def render_window(
    lines: typing.Sequence[TimedLine],
    index: int,
    *,
    before: int = CONTEXT_BEFORE,
    after: int = CONTEXT_AFTER,
) -> str:
    """Render the current line (bold) with a few lines of context around it.

    When ``index`` is :data:`BEFORE_FIRST` (the song has not reached the first
    line yet) the opening lines are previewed with none bold. Otherwise a window
    of ``before`` lines above and ``after`` below is shown with the current line
    bold. Pure - returns the ready-to-display markdown body (empty for no lines).
    """
    n = len(lines)
    if n == 0:
        return ""
    if index < 0:
        preview = lines[0 : after + 1]
        return "\n".join(_fmt_line(line.content, bold=False) for line in preview)
    start = max(0, index - before)
    end = min(n, index + after + 1)
    return "\n".join(
        _fmt_line(lines[i].content, bold=(i == index)) for i in range(start, end)
    )


def result_as_text(result: LyricsResult) -> str:
    """Flatten a result to plain text for the static card (timed loses its timing)."""
    if result.kind == KIND_TIMED:
        return "\n".join(line.content for line in result.lines)
    return result.text


def paginate_text(text: str, *, limit: int = PAGE_CHAR_LIMIT) -> list[str]:
    """Split ``text`` into pages of at most ``limit`` chars, breaking on lines.

    Never splits a line across pages unless the line itself exceeds ``limit`` (a
    pathological single line is hard-wrapped). Always returns at least one page so
    the caller can index ``[0]`` unconditionally. Pure.
    """
    pages: list[str] = []
    buf: list[str] = []
    size = 0
    for raw in text.split("\n"):
        line = raw
        while len(line) > limit:
            # A single over-long line: flush the buffer, then emit hard chunks.
            if buf:
                pages.append("\n".join(buf))
                buf, size = [], 0
            pages.append(line[:limit])
            line = line[limit:]
        added = len(line) + (1 if buf else 0)
        if buf and size + added > limit:
            pages.append("\n".join(buf))
            buf, size = [line], len(line)
        else:
            buf.append(line)
            size += added
    if buf:
        pages.append("\n".join(buf))
    return pages or [""]


def next_wake(
    lines: typing.Sequence[TimedLine],
    position_ms: int,
    *,
    min_gap: float = MIN_EDIT_GAP,
    max_sleep: float = MAX_TICK_SLEEP,
) -> float:
    """Seconds to sleep until the next useful edit for a session (pure, O(log n)).

    Returns the time from ``position_ms`` to the next line transition worth
    editing at, offset by :data:`DRIFT_MARGIN` so the wake lands just after the
    boundary. Two clamps shape it:

    * ``min_gap`` floor - only a transition at least ``min_gap`` seconds ahead of
      the current position (the moment of the last edit, in steady state) counts.
      A burst of lines closer together than the gap coalesces: the scheduler skips
      to the FIRST transition the floor allows, so a channel is never edited more
      than once per ``min_gap``.
    * ``max_sleep`` ceiling - the wait is capped so a seek, a pause or clock drift
      can never strand the session past this, and it is the fallback when there is
      NO upcoming allowed transition (an outro, an empty list, or a position past
      the last line): re-check after ``max_sleep`` rather than sleep forever.

    ``lines`` is sorted ascending by ``start_ms``. Pure - no clock, no I/O.
    """
    threshold = position_ms + int(min_gap * 1000)
    idx = bisect.bisect_left(lines, threshold, key=lambda line: line.start_ms)
    if idx < len(lines):
        sleep = (lines[idx].start_ms - position_ms) / 1000.0 + DRIFT_MARGIN
        return max(min_gap, min(sleep, max_sleep))
    return max_sleep


def should_edit(*, last_index: int, current_index: int) -> bool:
    """True when the rendered line changed since the last edit (pure, O(1)).

    The one remaining edit gate: the time floor now lives in :func:`next_wake`
    (which decides WHEN the loop wakes), so all this dedupe does is suppress a
    redundant edit when the wake landed on the same line as last time - a
    max_sleep re-check mid-line, or a wake nudged by a seek that did not cross a
    boundary. Every edit therefore shows a genuinely new line.
    """
    return current_index != last_index


# ---------------------------------------------------------------------------
# Fetch seam (the only node-touching code).
# ---------------------------------------------------------------------------


def lyrics_path(session_id: str, guild_id: int) -> str:
    """Return the plugin's per-player lyrics REST path (pure).

    FALLBACK ONLY. For youtube-sourced tracks the plugin feeds the raw
    youtube.com watch id to its YouTube Music lookup, which knows nothing about
    watch ids - a near-guaranteed LyricsNotFoundException 404 (the P5 scout's
    core finding, re-confirmed live when SCH/Gambi lookups failed). The primary
    path is client-side search-then-fetch (:func:`search_lyrics_path` +
    :func:`video_lyrics_path`); this session route only remains as a last try,
    where the plugin's own ISRC path can win for non-youtube sources.
    """
    return f"/sessions/{session_id}/players/{guild_id}/lyrics"


def search_lyrics_path(query: str) -> str:
    """The plugin's search route: ``/v4/lyrics/search/{query}`` (pure)."""
    return "/lyrics/search/" + urllib.parse.quote(query, safe="")


def video_lyrics_path(video_id: str) -> str:
    """The plugin's fetch route for a YouTube MUSIC video id (pure)."""
    return "/lyrics/" + urllib.parse.quote(str(video_id), safe="")


# Bracketed title noise that pollutes a YouTube Music search ("(Clip officiel)",
# "[Official Video]"...). Only chunks containing one of these markers are
# stripped - "(2022 Remaster)" or "(avec Ninho)" carry real signal and stay.
_TITLE_NOISE = re.compile(
    r"[\(\[][^)\]]*(?:official|officiel|clip|video|lyric|paroles|audio|"
    r"visuali[sz]er|\bmv\b)[^)\]]*[\)\]]",
    re.IGNORECASE,
)
# The " - Topic" suffix of auto-generated YouTube artist channels.
_TOPIC_SUFFIX = re.compile(r"\s*-\s*Topic\s*$", re.IGNORECASE)


def search_query_for(track: typing.Any) -> str:
    """Build the YouTube Music search query for a track: cleaned title + author.

    Pure. Strips bracketed upload noise from the title and the ``- Topic``
    suffix from auto-generated channel names; collapses whitespace. Returns an
    empty string when the track carries no usable title (caller skips the
    search).
    """
    title = _TITLE_NOISE.sub(" ", str(getattr(track, "title", "") or ""))
    if not title.strip():
        return ""  # no usable title -> nothing to search on
    author = _TOPIC_SUFFIX.sub("", str(getattr(track, "author", "") or ""))
    return " ".join(f"{title} {author}".split())


# How many search candidates to fetch-and-try before giving up: the right song is
# almost always near the top once ranked, but a live/sped-up variant can shadow it.
SEARCH_CANDIDATES = 3

# Title markers of a re-timed variant (sped up, slowed, live, remix, nightcore).
# When the PLAYING title carries none of these, a candidate that does is a
# different recording with different line timings even if its lyrics match, so it
# is deprioritised. When the playing title IS such a variant, the marker carries
# real signal and is not penalised. Word-bounded so "live" does not match "livin".
_VARIANT_MARKERS = re.compile(
    r"\b(?:sped[\s-]?up|slowed|nightcore|remix|live)\b",
    re.IGNORECASE,
)


def _has_variant_marker(title: str) -> bool:
    """True when a title names a re-timed variant (pure)."""
    return bool(_VARIANT_MARKERS.search(title or ""))


def _title_tokens(title: str) -> typing.Set[str]:
    """Casefolded alphanumeric token set of a title, upload noise stripped (pure)."""
    cleaned = _TITLE_NOISE.sub(" ", title or "")
    cleaned = _TOPIC_SUFFIX.sub("", cleaned)
    return {tok for tok in re.split(r"[^0-9a-z]+", cleaned.casefold()) if tok}


def title_similarity(a: str, b: str) -> float:
    """Token-set (Jaccard) similarity of two titles in [0, 1] (pure).

    Casefolded token overlap over token union: identical titles score 1.0,
    disjoint 0.0, a shared subset in between. Robust to word order and to trailing
    upload noise (both sides go through :func:`_title_tokens`). Empty on either
    side scores 0.0 (nothing to compare).
    """
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def rank_candidates(
    candidates: typing.Any,
    playing_title: str,
    *,
    limit: int = SEARCH_CANDIDATES,
) -> list:
    """Order search candidates best-first for the playing title, top ``limit`` (pure).

    Sort key, ascending: (variant penalty, -similarity, original index). So a
    non-variant candidate always precedes a penalised variant one; within a tier a
    higher title similarity wins; ties keep the search engine's own order (stable).
    Candidates that are not mappings or carry no ``videoId`` are dropped (they can
    never be fetched, so they must not consume a try slot). Never raises.
    """
    scored: list[tuple[int, float, int, typing.Any]] = []
    playing_is_variant = _has_variant_marker(playing_title)
    for index, cand in enumerate(candidates or ()):
        if not isinstance(cand, typing.Mapping) or not cand.get("videoId"):
            continue
        title = str(cand.get("title") or "")
        penalty = 1 if (not playing_is_variant and _has_variant_marker(title)) else 0
        scored.append((penalty, -title_similarity(playing_title, title), index, cand))
    scored.sort(key=lambda item: item[:3])
    return [cand for _, _, _, cand in scored[:limit]]


def _node_of(player: typing.Any) -> typing.Any:
    """Return the player's node, or None if it is not attached yet."""
    try:
        return player.node
    except Exception:
        return None


def _guild_id_of(player: typing.Any) -> typing.Optional[int]:
    """Return the player's guild id, or None if it cannot be resolved."""
    try:
        guild = getattr(player, "guild", None)
    except Exception:
        return None
    return getattr(guild, "id", None)


async def fetch_lyrics(
    player: typing.Any, *, skip_track_source: bool = False
) -> LyricsResult:
    """Fetch the current track's lyrics via sonolink's authenticated REST seam.

    PRIMARY path is the scout-proven search-then-fetch: the plugin's
    ``/v4/lyrics/{videoId}`` wants YouTube MUSIC video ids, so we search the
    cleaned "title author" first and try the top candidates. The per-player
    session route only runs as a LAST resort (its internal lookup feeds raw
    watch ids for youtube-sourced tracks and 404s - the SCH/Gambi regression).

    Best-effort: any node error (no player, no lyrics, a 4xx from the plugin) is
    logged once at debug and degrades to :data:`KIND_NONE` - the feature is
    read-only and must never propagate a failure into the command. Reuses
    ``node.send`` (the node's credentialed client), so no credentials are handled
    or logged here.
    """
    node = _node_of(player)
    if node is None:
        return _NONE_RESULT
    guild_id = _guild_id_of(player)
    if guild_id is None:
        return _NONE_RESULT

    # 1) Search-then-fetch off the track's own metadata (source-agnostic). Rank the
    # results by title similarity (deprioritising re-timed variants) before trying,
    # so an exact match beats a live/sped-up cover the search may have surfaced first.
    track = getattr(player, "current", None)
    query = search_query_for(track) if track is not None else ""
    if query:
        try:
            candidates = await node.send("GET", search_lyrics_path(query))
        except Exception as exc:
            log.debug("Lyrics search failed for guild %s (%s)", guild_id, exc)
            candidates = None
        playing_title = str(getattr(track, "title", "") or "")
        for candidate in rank_candidates(candidates, playing_title):
            video_id = candidate.get("videoId")  # ranker guarantees a truthy id
            try:
                payload = await node.send("GET", video_lyrics_path(video_id))
            except Exception:
                continue  # this candidate has no lyrics; try the next
            result = parse_lyrics(payload)
            if result.kind != KIND_NONE:
                return result

    # 2) Last resort: the plugin's own per-player lookup (ISRC path can win for
    # non-youtube sources even when the name search found nothing).
    try:
        session_id = node.session_id
    except Exception:
        return _NONE_RESULT
    path = lyrics_path(session_id, guild_id)
    params = {"skipTrackSource": "true" if skip_track_source else "false"}
    try:
        payload = await node.send("GET", path, params=params)
    except Exception as exc:
        log.debug("Lyrics fetch failed for guild %s (%s)", guild_id, exc)
        return _NONE_RESULT
    return parse_lyrics(payload)


# ---------------------------------------------------------------------------
# Duck-typed helpers shared by the UI.
# ---------------------------------------------------------------------------


def _in_players_voice(player: typing.Any, member: typing.Any) -> bool:
    """True when ``member`` is currently in ``player``'s voice channel.

    The synced session posts publicly, so - like the now-playing controller - it
    is a room surface any listener in the channel may drive, but a bystander
    outside it may not. Duck-typed so this module needs no import from the cog.
    """
    channel = getattr(player, "channel", None)
    if channel is None:
        return False
    voice = getattr(member, "voice", None)
    return voice is not None and getattr(voice, "channel", None) == channel


class _DelegateButton(discord.ui.Button):
    """A Components V2 button whose callback forwards to a bound coroutine.

    CV2 layouts cannot use the ``@discord.ui.button`` decorator (buttons live
    inside :class:`discord.ui.ActionRow` children), so each is a plain instance
    that delegates its click - the same shape as the controller's button.
    """

    def __init__(
        self,
        handler: typing.Callable[[discord.Interaction], typing.Awaitable[None]],
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(**kwargs)
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._handler(interaction)


def _track_header(track: typing.Any) -> str:
    """Render the track's title (linked when it has a URI) and author, or ''."""
    if track is None:
        return ""
    title = (getattr(track, "title", "") or "")[:256]
    uri = getattr(track, "uri", None)
    header = f"## [{title}]({uri})" if uri else f"## {title}"
    author = getattr(track, "author", None)
    if author:
        header += "\n" + _("by **{author}**").format(author=author)
    return header


# ---------------------------------------------------------------------------
# Static card (ephemeral, paginated).
# ---------------------------------------------------------------------------


class StaticLyricsCard(discord.ui.LayoutView):
    """The ephemeral ``/lyrics`` card: the full lyrics, paginated, with controls.

    A single accent container in the music house style: a heading, the track,
    the current page of lyrics, an attribution / page footer, and an action row -
    Prev/Next when there is more than one page, plus a "Follow along" button when
    the track has timed lyrics (the synced mode). Plain-text tracks show a gentle
    note that sync is unavailable instead of the button.

    Ephemeral, so only the invoker can see or click it - no author gate is needed
    (Discord blocks anyone else from the interaction), matching ``EffectsView``.
    """

    def __init__(
        self,
        cog: "Music",
        player: "Player",
        result: LyricsResult,
        *,
        timeout: float = 300,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.player = player
        self.result = result
        self.pages = paginate_text(result_as_text(result))
        self.index = 0
        # A one-shot notice shown after the Follow button is used (started, or a
        # clean refusal). Set it and rebuild to replace the button with feedback.
        self._notice: typing.Optional[str] = None
        self._followed = False
        self.message: typing.Optional[discord.Message] = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay(_("### 🎤 Lyrics")))

        header = _track_header(getattr(self.player, "current", None))
        if header:
            container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self.pages[self.index] or _INSTRUMENTAL))
        container.add_item(discord.ui.Separator())

        footer: list[str] = []
        if self.result.source:
            footer.append(_("-# Lyrics from {source}").format(source=self.result.source))
        if len(self.pages) > 1:
            footer.append(
                _("-# Page {current} of {total}").format(
                    current=self.index + 1, total=len(self.pages)
                )
            )
        if self.result.kind == KIND_PLAIN:
            footer.append(_("-# Live sync isn't available for this track."))
        if footer:
            container.add_item(discord.ui.TextDisplay("\n".join(footer)))

        if self._notice:
            container.add_item(discord.ui.TextDisplay(self._notice))

        row: list[discord.ui.Button] = []
        if len(self.pages) > 1:
            row.append(
                _DelegateButton(
                    self._prev,
                    emoji="\N{BLACK LEFT-POINTING TRIANGLE}",
                    style=discord.ButtonStyle.secondary,
                    disabled=self.index == 0,
                )
            )
            row.append(
                _DelegateButton(
                    self._next,
                    emoji="\N{BLACK RIGHT-POINTING TRIANGLE}",
                    style=discord.ButtonStyle.secondary,
                    disabled=self.index >= len(self.pages) - 1,
                )
            )
        if self.result.kind == KIND_TIMED and not self._followed:
            row.append(
                _DelegateButton(
                    self._follow,
                    label=_("Follow along"),
                    emoji="\N{MUSICAL NOTE}",
                    style=discord.ButtonStyle.primary,
                )
            )
        if row:
            container.add_item(discord.ui.ActionRow(*row))

        self.add_item(container)

    async def _rerender(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(view=self)

    async def _prev(self, interaction: discord.Interaction) -> None:
        if self.index > 0:
            self.index -= 1
        await self._rerender(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        if self.index < len(self.pages) - 1:
            self.index += 1
        await self._rerender(interaction)

    async def _follow(self, interaction: discord.Interaction) -> None:
        if not _in_players_voice(self.player, interaction.user):
            await interaction.response.send_message(
                _("You must be in my voice channel to use these controls."),
                ephemeral=True,
            )
            return
        code = await self.cog._start_lyrics_follow(self.player, self.result)
        if code == START_OK:
            self._followed = True
            channel = getattr(self.player, "home", None)
            mention = channel.mention if channel is not None else _("the music channel")
            self._notice = _("Now following the lyrics in {channel}.").format(
                channel=mention
            )
        elif code == START_CEILING_FULL:
            self._notice = _(
                "A lot of servers are following lyrics right now - try again in a moment."
            )
        else:
            self._notice = _("I can't follow the lyrics here right now.")
        await self._rerender(interaction)


# ---------------------------------------------------------------------------
# Synced card (public, one per session, edited in place).
# ---------------------------------------------------------------------------


class SyncedLyricsCard(discord.ui.LayoutView):
    """The public live-lyrics message a session edits in place as the song plays.

    Owned by one :class:`SyncedLyricsSession`, rebuilt (not recreated) on every
    edit - the same reuse pattern as the now-playing controller - so a long
    session never churns view objects. Carries a Stop button (a room surface: any
    listener in the voice channel may stop it) until the session finalises, when
    the button is dropped and a closing note replaces it.
    """

    def __init__(self, session: "SyncedLyricsSession") -> None:
        super().__init__(timeout=None)
        self._session = session
        self._body = ""
        self._stopped = False
        self._build()

    def set_state(self, *, body: str, stopped: bool = False) -> None:
        self._body = body
        self._stopped = stopped
        self._build()

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_colour=random_colour())
        container.add_item(discord.ui.TextDisplay(_("### 🎤 Live Lyrics")))
        header = _track_header(self._session.track)
        if header:
            container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._body or _INSTRUMENTAL))
        container.add_item(discord.ui.Separator())
        if self._session.source:
            container.add_item(
                discord.ui.TextDisplay(
                    _("-# Lyrics from {source}").format(source=self._session.source)
                )
            )
        if self._session.offset_ms:
            container.add_item(
                discord.ui.TextDisplay(
                    _("-# Lyrics offset: {offset}s").format(
                        offset=format_offset(self._session.offset_ms)
                    )
                )
            )
        if self._stopped:
            container.add_item(
                discord.ui.TextDisplay(_("-# Stopped following the lyrics."))
            )
        else:
            container.add_item(
                discord.ui.TextDisplay(
                    _("-# Following along - the lyrics update as the song plays.")
                )
            )
            # Row 1: cooperative calibration - shift the per-session offset so the
            # bold line lands on the beat when the video's timing differs from the
            # lyrics' (open to anyone in voice, same gate as Stop).
            container.add_item(
                discord.ui.ActionRow(
                    _DelegateButton(
                        functools.partial(
                            self._session.shift_offset_from_interaction,
                            delta_ms=-OFFSET_STEP_LARGE_MS,
                        ),
                        label=_("-5s"),
                        style=discord.ButtonStyle.secondary,
                    ),
                    _DelegateButton(
                        functools.partial(
                            self._session.shift_offset_from_interaction,
                            delta_ms=-OFFSET_STEP_SMALL_MS,
                        ),
                        label=_("-1s"),
                        style=discord.ButtonStyle.secondary,
                    ),
                    _DelegateButton(
                        functools.partial(
                            self._session.shift_offset_from_interaction,
                            delta_ms=OFFSET_STEP_SMALL_MS,
                        ),
                        label=_("+1s"),
                        style=discord.ButtonStyle.secondary,
                    ),
                    _DelegateButton(
                        functools.partial(
                            self._session.shift_offset_from_interaction,
                            delta_ms=OFFSET_STEP_LARGE_MS,
                        ),
                        label=_("+5s"),
                        style=discord.ButtonStyle.secondary,
                    ),
                )
            )
            # Row 2: Stop (kept on its own row so the four calibration steps read
            # as one group and the row budget stays comfortably legal).
            container.add_item(
                discord.ui.ActionRow(
                    _DelegateButton(
                        self._session.stop_from_interaction,
                        label=_("Stop"),
                        emoji="\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}",
                        style=discord.ButtonStyle.secondary,
                    )
                )
            )
        self.add_item(container)


# ---------------------------------------------------------------------------
# Per-guild synced session.
# ---------------------------------------------------------------------------


class SyncedLyricsSession:
    """Drives ONE public message that follows playback for a single guild.

    Holds the timed lines, the player it follows and the message it edits. A
    background loop sleeps until the next line boundary (via :func:`next_wake`),
    reads the player's (self-extrapolating) position, picks the current line and -
    via :func:`should_edit` - edits the message when the line index changed. The
    scheduler bounds edits to at most one per ``min_edit_gap`` and re-checks at
    least every ``max_tick_sleep`` so a seek or pause never strands it; a paused
    player is detected (its position is frozen) and skipped without an edit. A
    :meth:`nudge` from the cog's /seek wakes the loop early for a prompt resync.
    It stops itself when the track ends, changes or the player disconnects;
    external stops (the Stop button, a track change, the cog's teardown) go
    through :meth:`stop`, which is idempotent.

    The process-wide ``synced_lyrics`` ceiling slot is owned by the registry: it
    acquires on start and releases via :meth:`LyricsSessions._detach` on every
    stop path, so a session can never leak a slot.
    """

    def __init__(
        self,
        *,
        guild_id: int,
        player: "Player",
        channel: typing.Any,
        result: LyricsResult,
        track: typing.Any,
        registry: "LyricsSessions",
        clock: typing.Callable[[], float] = time.monotonic,
        min_edit_gap: float = MIN_EDIT_GAP,
        max_tick_sleep: float = MAX_TICK_SLEEP,
    ) -> None:
        self.guild_id = guild_id
        self.player = player
        self.channel = channel
        self.lines = result.lines
        self.source = result.source
        self.track = track
        self.track_id = getattr(track, "identifier", None)
        self._registry = registry
        self._clock = clock
        self._min_edit_gap = min_edit_gap
        self._max_tick_sleep = max_tick_sleep
        # Per-session calibration offset (ms), added to the player position before
        # line selection. Starts at 0 for every session; a new track is always a
        # new session, so the offset resets on track change (a new cut). The
        # calibration buttons shift it, clamped to +/- OFFSET_LIMIT_MS.
        self.offset_ms = 0
        self.message: typing.Optional[discord.Message] = None
        self._card = SyncedLyricsCard(self)
        self._task: typing.Optional[asyncio.Task] = None
        self._stopped = False
        self._last_index = BEFORE_FIRST
        self._last_edit_ts = 0.0
        self._last_body = ""
        # Set by nudge() (a seek) to wake the sleeping loop early for a resync.
        self._wake = asyncio.Event()

    def _position_ms(self) -> int:
        # sonolink's Player.position already interpolates the last playerUpdate on
        # a monotonic wall clock and freezes when paused (verified in _base.py), so
        # we read it as the source of truth and do NOT extrapolate again here.
        return int(getattr(self.player, "position", 0) or 0)

    def _effective_position_ms(self) -> int:
        """Player position shifted by the calibration offset, for line selection."""
        return self._position_ms() + self.offset_ms

    def _is_paused(self) -> bool:
        """True when the player is paused (its position is frozen).

        The loop must not edit while paused: the extrapolated position stops
        advancing, so re-picking the current line would either be a no-op (same
        index) or, if the seam kept advancing on wall clock, wrongly race ahead.
        Duck-typed so this module needs no import from the cog.
        """
        return bool(getattr(self.player, "paused", False))

    def nudge(self) -> None:
        """Wake the loop early so the next tick resyncs at once (best-effort).

        Called by the cog after a /seek: the position jumped, so re-picking the
        current line right away lands the bold line on the new spot instead of
        waiting out the current sleep. A no-op when the loop is not running (the
        loop clears the flag on its next wake), so it is always safe to call.
        """
        self._wake.set()

    async def _sleep(self, delay: float) -> None:
        """Sleep ``delay`` seconds, returning early if :meth:`nudge` fires."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    def _next_sleep(self) -> float:
        """Seconds to sleep before the next tick, honouring pause and the schedule."""
        if self._is_paused():
            # Position is frozen: nothing to aim at, just re-check periodically.
            return self._max_tick_sleep
        return next_wake(
            self.lines,
            self._effective_position_ms(),
            min_gap=self._min_edit_gap,
            max_sleep=self._max_tick_sleep,
        )

    def _is_terminated(self, track: typing.Any) -> bool:
        """True when the session should end: player gone, or the track changed."""
        if getattr(self.player, "channel", None) is None:
            return True
        if track is None:
            return True
        return getattr(track, "identifier", None) != self.track_id

    async def start(self) -> None:
        """Post the initial message at the current position and start the loop.

        Guards the initial send against being superseded mid-flight: a concurrent
        replace (a second Follow for the same guild), a ``notify_track`` or a
        teardown can stop this session while the ``channel.send`` is still in
        flight. If that happened, the just-posted message would otherwise be
        orphaned - live-looking but never updated, with a dead Stop button - so it
        is deleted and no loop is started for the dead session.
        """
        if self._stopped:
            return
        index = current_line_index(self.lines, self._effective_position_ms())
        self._last_body = render_window(self.lines, index)
        self._last_index = index
        self._last_edit_ts = self._clock()
        self._card.set_state(body=self._last_body)
        message = await self.channel.send(
            view=self._card, allowed_mentions=discord.AllowedMentions.none()
        )
        if self._stopped:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return
        self.message = message
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self._stopped:
                await self._sleep(self._next_sleep())
                await self._tick_once()
        except asyncio.CancelledError:  # external stop cancelled us; cleanup done
            pass
        except Exception:
            log.exception("Synced lyrics loop crashed for guild %s", self.guild_id)
            await self.stop(finalize=True)

    async def _tick_once(self) -> None:
        """One loop iteration: end if the track is gone, else maybe edit.

        The scheduler already enforced the time floor by choosing when to wake, so
        the only edit gate left here is the line-index dedupe. A paused player is
        skipped without an edit (its frozen position would render a stale line).
        """
        if self._stopped:
            return
        track = getattr(self.player, "current", None)
        if self._is_terminated(track):
            await self.stop(finalize=True)
            return
        if self._is_paused():
            return
        index = current_line_index(self.lines, self._effective_position_ms())
        if not should_edit(last_index=self._last_index, current_index=index):
            return
        self._last_index = index
        self._last_edit_ts = self._clock()
        self._last_body = render_window(self.lines, index)
        await self._edit(self._last_body)

    async def _edit(self, body: str) -> None:
        if self.message is None:
            return
        self._card.set_state(body=body)
        try:
            await self.message.edit(view=self._card)
        except discord.HTTPException:
            # The message was deleted out of band: no point following a ghost.
            log.debug("Synced lyrics edit failed for guild %s; stopping", self.guild_id)
            await self.stop(finalize=False)

    async def stop(self, *, finalize: bool = False) -> None:
        """Stop the session and release its resources (idempotent).

        Detaches from the registry (which releases the ceiling slot), cancels the
        loop when called from outside it, and - when ``finalize`` - edits the
        message one last time into a closed state and frees the view. Safe to call
        from any stop path, including from within the loop's own termination.
        """
        if self._stopped:
            return
        self._stopped = True
        self._registry._detach(self.guild_id)
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if finalize:
            await self._finalize()
        self._card.stop()

    async def _finalize(self) -> None:
        if self.message is None:
            return
        self._card.set_state(body=self._last_body, stopped=True)
        try:
            await self.message.edit(view=self._card)
        except discord.HTTPException:
            pass

    async def stop_from_interaction(self, interaction: discord.Interaction) -> None:
        """Stop button handler: same-voice gated, then finalise the message."""
        if not _in_players_voice(self.player, interaction.user):
            await interaction.response.send_message(
                _("You must be in my voice channel to use these controls."),
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self.stop(finalize=True)

    async def shift_offset_from_interaction(
        self, interaction: discord.Interaction, *, delta_ms: int
    ) -> None:
        """Calibration button handler: nudge the offset and resync immediately.

        Cooperative, not destructive - any listener in the voice channel may
        calibrate (same-voice gate only, matching Stop). Shifts the per-session
        offset by ``delta_ms`` (clamped to +/- OFFSET_LIMIT_MS), re-picks the
        current line under the new offset, edits the message via the interaction
        (immediate feedback, one interaction-driven edit) and re-primes the loop's
        last-edit state so its next autonomous tick does not re-edit the same line.
        A :meth:`nudge` wakes the loop so it re-plans its schedule around the new
        effective position at once.
        """
        if not _in_players_voice(self.player, interaction.user):
            await interaction.response.send_message(
                _("You must be in my voice channel to use these controls."),
                ephemeral=True,
            )
            return
        if self._stopped:
            # Raced with a stop/finalise: acknowledge without reviving the message.
            await interaction.response.defer()
            return
        self.offset_ms = clamp_offset(self.offset_ms + delta_ms)
        index = current_line_index(self.lines, self._effective_position_ms())
        self._last_index = index
        self._last_body = render_window(self.lines, index)
        self._last_edit_ts = self._clock()
        self._card.set_state(body=self._last_body)
        await interaction.response.edit_message(view=self._card)
        self.nudge()


# ---------------------------------------------------------------------------
# Bounded per-cog registry.
# ---------------------------------------------------------------------------


class LyricsSessions:
    """The live synced-lyrics sessions, at most one per guild, bounded by a ceiling.

    Wraps the process-wide ``synced_lyrics`` :class:`~tools.quotas.GlobalCeiling`:
    :meth:`start` acquires a slot (refusing cleanly when the ceiling is full) and
    every stop path releases it through :meth:`_detach`. Because a session only
    lands in the map after a slot is acquired, the map holds at most ``capacity``
    entries (25) no matter how many guilds exist - the whole feature's background
    footprint is bounded by the ceiling, not by the guild count.
    """

    def __init__(
        self,
        ceiling: typing.Any,
        *,
        clock: typing.Callable[[], float] = time.monotonic,
    ) -> None:
        self._ceiling = ceiling
        self._clock = clock
        self._sessions: typing.Dict[int, SyncedLyricsSession] = {}

    def get(self, guild_id: int) -> typing.Optional[SyncedLyricsSession]:
        return self._sessions.get(guild_id)

    def count(self) -> int:
        return len(self._sessions)

    def _detach(self, guild_id: int) -> None:
        """Drop a guild's session and release its ceiling slot (idempotent)."""
        self._sessions.pop(guild_id, None)
        self._ceiling.release(guild_id)

    async def start(
        self,
        *,
        guild_id: int,
        player: "Player",
        channel: typing.Any,
        result: LyricsResult,
        track: typing.Any,
    ) -> typing.Optional[SyncedLyricsSession]:
        """Start (replacing any existing) a session; None when the ceiling is full.

        A re-invoke replaces the guild's session rather than stacking a second, so
        there is never more than one live message per guild. The old session is
        stopped WITHOUT a closing edit (it is being superseded, not ended).
        """
        await self.stop(guild_id, finalize=False)
        if not self._ceiling.acquire(guild_id):
            return None
        session = SyncedLyricsSession(
            guild_id=guild_id,
            player=player,
            channel=channel,
            result=result,
            track=track,
            registry=self,
            clock=self._clock,
        )
        self._sessions[guild_id] = session
        try:
            await session.start()
        except Exception:
            log.exception("Failed to start synced lyrics for guild %s", guild_id)
            self._detach(guild_id)
            return None
        return session

    async def stop(self, guild_id: int, *, finalize: bool = True) -> bool:
        """Stop a guild's session; True if one was running. Releases the slot."""
        session = self._sessions.get(guild_id)
        if session is None:
            # Idempotent safety: release even if the map has no session (a slot
            # can never linger past its session).
            self._ceiling.release(guild_id)
            return False
        await session.stop(finalize=finalize)
        return True

    async def notify_track(self, guild_id: int, track_id: typing.Optional[str]) -> None:
        """A track_start fired: end the session only if the track actually changed.

        A reconnect re-fires track_start for the SAME track; comparing ids keeps a
        live session across that (it only ends on a genuine change), while a real
        next-track / natural end ends it and finalises the message.
        """
        session = self._sessions.get(guild_id)
        if session is not None and session.track_id != track_id:
            await session.stop(finalize=True)

    def shutdown(self) -> None:
        """Cancel every session's loop synchronously (for the cog's cog_unload).

        cog_unload cannot await, so this cancels the tasks and clears the map and
        ceiling without a closing edit - the process is going down anyway.
        """
        for guild_id, session in list(self._sessions.items()):
            task = session._task
            if task is not None:
                task.cancel()
            self._ceiling.release(guild_id)
        self._sessions.clear()

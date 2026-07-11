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

Scale. A synced session does in-process work every ``TICK_INTERVAL`` seconds
(read the extrapolated player position, pick the current line - O(1), no I/O) but
only *edits* its one Discord message at most once per ``EDIT_INTERVAL`` (>= 5 s)
AND only when the current line actually changes. The number of live sessions is
capped process-wide by the ``synced_lyrics`` :class:`~tools.quotas.GlobalCeiling`
(25), acquired per session and released on every stop path, so the background
work and the per-cog session map are bounded by 25 regardless of guild count.
"""

from __future__ import annotations

import asyncio
import bisect
import dataclasses
import logging
import time
import typing

import discord

from tools.formats import random_colour
from tools.i18n import _

if typing.TYPE_CHECKING:  # the cog type is only used in string annotations here
    from cogs.music.music import Music, Player

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables. Every knob lives here so the cadence is documented in one place.
# ---------------------------------------------------------------------------

# Minimum seconds between edits of a synced session's message. The task floor is
# 5 s; 6 s leaves headroom under Discord's per-channel edit budget (5 / 5 s) even
# in the pathological case of a line changing on every tick.
EDIT_INTERVAL = 6.0

# How often the session's loop wakes to re-read the position and re-decide. This
# is in-process only (an int read plus a comparison), so a tight-ish tick keeps
# the highlighted line fresh without ever driving an edit faster than the
# interval above.
TICK_INTERVAL = 2.0

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


def should_edit(
    *,
    now: float,
    last_edit_ts: float,
    last_index: int,
    current_index: int,
    interval: float = EDIT_INTERVAL,
) -> bool:
    """Decide whether the synced message should be edited this tick (pure, O(1)).

    Two gates, both must pass: the current line must have CHANGED since the last
    edit (no point re-sending the same window), and at least ``interval`` seconds
    must have elapsed since the last edit (the rate cap that bounds edits/sec). A
    rapid run of line changes therefore collapses to one edit per interval, and
    that edit always shows the line live at the moment it fires.
    """
    if current_index == last_index:
        return False
    return (now - last_edit_ts) >= interval


# ---------------------------------------------------------------------------
# Fetch seam (the only node-touching code).
# ---------------------------------------------------------------------------


def lyrics_path(session_id: str, guild_id: int) -> str:
    """Return the plugin's per-player lyrics REST path (pure).

    Mirrors ``sponsorblock.categories_path``: the leading slash is what makes
    sonolink's REST client prepend ``/v4``, yielding the full
    ``/v4/sessions/{sessionId}/players/{guildId}/lyrics``. That route fetches
    lyrics for the track the player is CURRENTLY playing (verified live: it 400s
    with "Not currently playing anything" when idle), so it works across every
    source the node can play, not only YouTube.
    """
    return f"/sessions/{session_id}/players/{guild_id}/lyrics"


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

    Best-effort: any node error (no player, no lyrics, a 4xx from the plugin) is
    logged once at debug and degrades to :data:`KIND_NONE` - the feature is
    read-only and must never propagate a failure into the command. Reuses
    ``node.send`` (the node's credentialed client), so no credentials are handled
    or logged here. ``skip_track_source`` maps to the plugin's ``skipTrackSource``
    query flag (skip the track's own source lyrics and go straight to fallback).
    """
    node = _node_of(player)
    if node is None:
        return _NONE_RESULT
    try:
        session_id = node.session_id
    except Exception:
        return _NONE_RESULT
    guild_id = _guild_id_of(player)
    if guild_id is None:
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
    background loop wakes every ``tick_interval`` seconds to read the player's
    (self-extrapolating) position, pick the current line and - via
    :func:`should_edit` - edit the message at most once per ``edit_interval`` and
    only when the line changed. It stops itself when the track ends, changes or
    the player disconnects; external stops (the Stop button, a track change, the
    cog's teardown) go through :meth:`stop`, which is idempotent.

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
        edit_interval: float = EDIT_INTERVAL,
        tick_interval: float = TICK_INTERVAL,
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
        self._edit_interval = edit_interval
        self._tick_interval = tick_interval
        self.message: typing.Optional[discord.Message] = None
        self._card = SyncedLyricsCard(self)
        self._task: typing.Optional[asyncio.Task] = None
        self._stopped = False
        self._last_index = BEFORE_FIRST
        self._last_edit_ts = 0.0
        self._last_body = ""

    def _position_ms(self) -> int:
        return int(getattr(self.player, "position", 0) or 0)

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
        index = current_line_index(self.lines, self._position_ms())
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
                await asyncio.sleep(self._tick_interval)
                await self._tick_once()
        except asyncio.CancelledError:  # external stop cancelled us; cleanup done
            pass
        except Exception:
            log.exception("Synced lyrics loop crashed for guild %s", self.guild_id)
            await self.stop(finalize=True)

    async def _tick_once(self) -> None:
        """One loop iteration: end if the track is gone, else maybe edit."""
        if self._stopped:
            return
        track = getattr(self.player, "current", None)
        if self._is_terminated(track):
            await self.stop(finalize=True)
            return
        index = current_line_index(self.lines, self._position_ms())
        now = self._clock()
        if not should_edit(
            now=now,
            last_edit_ts=self._last_edit_ts,
            last_index=self._last_index,
            current_index=index,
            interval=self._edit_interval,
        ):
            return
        self._last_index = index
        self._last_edit_ts = now
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

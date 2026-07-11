"""Unit tests for ``cogs/music/lyrics.py``.

The live per-player fetch (``GET /v4/sessions/{s}/players/{g}/lyrics``) needs a
connected node with a track actually playing, so it cannot run under pytest.
Verified live against the node instead (node reports the ``lyrics`` plugin
v2.6.1, the route 400s "Not currently playing anything" when idle - proving it
is registered - and the response uses the ``source`` / ``lines[].range.start`` /
``lines[].line`` / ``text`` shape the parser's primary path targets).

What is deterministic without a backend is covered here:

* the pure parser, across BOTH plugin dialects (``range.start`` and flat
  ``timestamp``), plain text, and malformed / empty payloads;
* current-line selection by position, at the boundaries;
* window rendering (bold current line, before-first preview, instrumental gap);
* text pagination and the edit-cadence decision helper;
* the fetch seam, driven against a fake ``player``/``node`` that records the
  ``node.send`` call (path, params) and the best-effort "never raises" contract;
* the session machinery - replace-not-duplicate, the process-wide ceiling
  (bounded, released on every stop path), ``notify_track`` and ``shutdown`` -
  and one deterministic ``_tick_once`` drive (edit only when the line changed AND
  the interval elapsed; auto-stop on track change / disconnect) with fakes.

``lyrics.py`` duck-types the player/node and imports no sonolink types, so it
imports identically under the stub and the real sonolink.
"""

import asyncio
import types

from cogs.music import lyrics
from tools.quotas import GlobalCeiling

# ---------------------------------------------------------------------------
# Payload builders (the two dialects the parser must tolerate)
# ---------------------------------------------------------------------------


def _duncte_timed():
    """DuncteBot java-timed-lyrics shape: source + lines[].range.start + line."""
    return {
        "type": "timed",
        "source": "LyricFind",
        "lines": [
            {"line": "first", "range": {"start": 1000, "end": 2000}},
            {"line": "second", "range": {"start": 3000, "end": 4000}},
            {"line": "third", "range": {"start": 5000, "end": 6000}},
        ],
    }


def _lavalyrics_timed():
    """LavaLyrics/appujet shape: provider + flat lines[].timestamp + line."""
    return {
        "sourceName": "spotify",
        "provider": "MusixMatch",
        "text": None,
        "lines": [
            {"timestamp": 1000, "duration": 2000, "line": "first", "plugin": {}},
            {"timestamp": 3000, "duration": 2000, "line": "second", "plugin": {}},
        ],
    }


# ---------------------------------------------------------------------------
# parse_lyrics
# ---------------------------------------------------------------------------


def test_parse_timed_duncte_dialect():
    result = lyrics.parse_lyrics(_duncte_timed())
    assert result.kind == lyrics.KIND_TIMED
    assert result.is_timed and result.has_lyrics
    assert result.source == "LyricFind"
    assert [line.start_ms for line in result.lines] == [1000, 3000, 5000]
    assert [line.content for line in result.lines] == ["first", "second", "third"]


def test_parse_timed_lavalyrics_dialect():
    result = lyrics.parse_lyrics(_lavalyrics_timed())
    assert result.kind == lyrics.KIND_TIMED
    # No ``source`` key: falls back to ``provider`` for attribution.
    assert result.source == "MusixMatch"
    assert [line.start_ms for line in result.lines] == [1000, 3000]


def test_parse_plain_text():
    result = lyrics.parse_lyrics({"type": "text", "source": "Genius", "text": "  la la  "})
    assert result.kind == lyrics.KIND_PLAIN
    assert not result.is_timed and result.has_lyrics
    assert result.text == "la la"
    assert result.source == "Genius"


def test_parse_keys_off_data_not_type_field():
    # A payload MISLABELLED type=text but carrying real timed lines is timed.
    payload = {"type": "text", "lines": [{"line": "x", "range": {"start": 0}}]}
    assert lyrics.parse_lyrics(payload).kind == lyrics.KIND_TIMED
    # Empty lines + a text body degrades to plain, whatever the type says.
    payload = {"type": "timed", "lines": [], "text": "body"}
    assert lyrics.parse_lyrics(payload).kind == lyrics.KIND_PLAIN


def test_parse_lines_sorted_ascending():
    payload = {
        "lines": [
            {"line": "b", "range": {"start": 5000}},
            {"line": "a", "range": {"start": 1000}},
            {"line": "c", "range": {"start": 9000}},
        ]
    }
    result = lyrics.parse_lyrics(payload)
    assert [line.start_ms for line in result.lines] == [1000, 5000, 9000]
    assert [line.content for line in result.lines] == ["a", "b", "c"]


def test_parse_skips_malformed_lines():
    payload = {
        "lines": [
            {"line": "ok", "range": {"start": 1000}},
            {"range": {"start": 2000}},  # no line text
            {"line": 123, "range": {"start": 2500}},  # non-string body
            {"line": "no-start"},  # no timestamp at all
            "not-a-mapping",
            {"line": "flat", "timestamp": 4000},  # other dialect, still valid
        ]
    }
    result = lyrics.parse_lyrics(payload)
    assert [line.content for line in result.lines] == ["ok", "flat"]


def test_parse_missing_source_is_none():
    result = lyrics.parse_lyrics({"lines": [{"line": "x", "range": {"start": 0}}]})
    assert result.source is None


def test_parse_empty_lines_falls_through_to_none():
    # No usable lines and no text -> KIND_NONE (not a crash, not an empty timed).
    result = lyrics.parse_lyrics({"lines": [], "text": "   "})
    assert result.kind == lyrics.KIND_NONE
    assert not result.has_lyrics


def test_parse_bool_start_rejected():
    # bool is an int subclass; a True/False timestamp must not be accepted as 1/0.
    payload = {"lines": [{"line": "x", "range": {"start": True}}]}
    assert lyrics.parse_lyrics(payload).kind == lyrics.KIND_NONE


def test_parse_negative_start_clamped_to_zero():
    payload = {"lines": [{"line": "x", "range": {"start": -50}}]}
    result = lyrics.parse_lyrics(payload)
    assert result.lines[0].start_ms == 0


def test_parse_garbage_never_raises():
    for bad in (None, 5, "text", [], (), {"lines": "notalist"}, {"lines": None}):
        result = lyrics.parse_lyrics(bad)
        assert result.kind == lyrics.KIND_NONE
        assert not result.has_lyrics


# ---------------------------------------------------------------------------
# current_line_index (boundaries)
# ---------------------------------------------------------------------------


def _lines(*starts):
    return tuple(lyrics.TimedLine(s, f"line-{s}") for s in starts)


def test_current_line_before_first():
    lines = _lines(1000, 3000, 5000)
    assert lyrics.current_line_index(lines, 0) == lyrics.BEFORE_FIRST
    assert lyrics.current_line_index(lines, 999) == lyrics.BEFORE_FIRST


def test_current_line_exactly_on_start_selects_that_line():
    lines = _lines(1000, 3000, 5000)
    assert lyrics.current_line_index(lines, 1000) == 0
    assert lyrics.current_line_index(lines, 3000) == 1


def test_current_line_between_and_after():
    lines = _lines(1000, 3000, 5000)
    assert lyrics.current_line_index(lines, 2999) == 0
    assert lyrics.current_line_index(lines, 4999) == 1
    assert lyrics.current_line_index(lines, 5000) == 2
    assert lyrics.current_line_index(lines, 999999) == 2


def test_current_line_empty_lines():
    assert lyrics.current_line_index((), 1234) == lyrics.BEFORE_FIRST


# ---------------------------------------------------------------------------
# render_window
# ---------------------------------------------------------------------------


def test_render_window_bolds_current_with_context():
    lines = _lines(0, 1000, 2000, 3000, 4000)
    body = lyrics.render_window(lines, 2, before=1, after=2)
    # index 2 with 1 before + 2 after -> lines 1..4, current (line-2000) bold.
    assert body == "line-1000\n**line-2000**\nline-3000\nline-4000"


def test_render_window_before_first_previews_unbolded():
    lines = _lines(0, 1000, 2000, 3000)
    body = lyrics.render_window(lines, lyrics.BEFORE_FIRST, after=2)
    assert "**" not in body
    assert body == "line-0\nline-1000\nline-2000"


def test_render_window_clamps_at_edges():
    lines = _lines(0, 1000)
    # index 0 near the start: no crash, only what exists.
    assert lyrics.render_window(lines, 0, before=2, after=2) == "**line-0**\nline-1000"


def test_render_window_instrumental_marker_for_empty_line():
    lines = (lyrics.TimedLine(0, "  "), lyrics.TimedLine(1000, "sung"))
    body = lyrics.render_window(lines, 0, before=0, after=0)
    assert body == f"**{lyrics._INSTRUMENTAL}**"


def test_render_window_no_lines_is_empty():
    assert lyrics.render_window((), 0) == ""


# ---------------------------------------------------------------------------
# result_as_text / paginate_text
# ---------------------------------------------------------------------------


def test_result_as_text_timed_joins_lines():
    result = lyrics.parse_lyrics(_duncte_timed())
    assert lyrics.result_as_text(result) == "first\nsecond\nthird"


def test_result_as_text_plain_returns_text():
    result = lyrics.parse_lyrics({"text": "just words"})
    assert lyrics.result_as_text(result) == "just words"


def test_paginate_single_short_page():
    assert lyrics.paginate_text("a\nb\nc", limit=100) == ["a\nb\nc"]


def test_paginate_splits_on_line_boundaries():
    text = "\n".join(["x" * 40] * 10)  # 10 lines of 40 chars
    pages = lyrics.paginate_text(text, limit=100)
    assert len(pages) > 1
    assert all(len(p) <= 100 for p in pages)
    # No line is split across a page boundary (each page is whole 40-char lines).
    for page in pages:
        for line in page.split("\n"):
            assert line == "x" * 40


def test_paginate_hard_wraps_overlong_single_line():
    pages = lyrics.paginate_text("z" * 250, limit=100)
    assert pages == ["z" * 100, "z" * 100, "z" * 50]


def test_paginate_empty_returns_one_empty_page():
    assert lyrics.paginate_text("") == [""]


# ---------------------------------------------------------------------------
# should_edit (edit-cadence decision)
# ---------------------------------------------------------------------------


def test_should_edit_false_when_line_unchanged():
    # Same index: never edit, no matter how much time has passed.
    assert not lyrics.should_edit(
        now=1000.0, last_edit_ts=0.0, last_index=2, current_index=2, interval=6.0
    )


def test_should_edit_false_when_changed_but_too_soon():
    assert not lyrics.should_edit(
        now=3.0, last_edit_ts=0.0, last_index=1, current_index=2, interval=6.0
    )


def test_should_edit_true_when_changed_and_interval_elapsed():
    assert lyrics.should_edit(
        now=6.0, last_edit_ts=0.0, last_index=1, current_index=2, interval=6.0
    )


def test_should_edit_boundary_exactly_interval():
    # Exactly the interval counts as elapsed (>=).
    assert lyrics.should_edit(
        now=6.0, last_edit_ts=0.0, last_index=lyrics.BEFORE_FIRST,
        current_index=0, interval=6.0,
    )


# ---------------------------------------------------------------------------
# lyrics_path
# ---------------------------------------------------------------------------


def test_lyrics_path_shape():
    assert lyrics.lyrics_path("abc123", 42) == "/sessions/abc123/players/42/lyrics"


def test_lyrics_path_leading_slash():
    # The leading slash is what makes sonolink's REST client prepend "/v4".
    assert lyrics.lyrics_path("s", 1).startswith("/sessions/")


# ---------------------------------------------------------------------------
# Fetch seam fakes
# ---------------------------------------------------------------------------


class _FakeNode:
    def __init__(self, session_id="sess-1", payload=None, raise_exc=None):
        self._session_id = session_id
        self.payload = payload
        self.raise_exc = raise_exc
        self.calls = []

    @property
    def session_id(self):
        if self._session_id is None:
            raise RuntimeError("no session id")
        return self._session_id

    async def send(self, method, path, *, params=None, **kwargs):
        self.calls.append((method, path, params))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.payload


class _FakePlayer:
    def __init__(self, node, guild_id=42):
        self._node = node
        self.guild = types.SimpleNamespace(id=guild_id) if guild_id is not None else None

    @property
    def node(self):
        if self._node is None:
            raise RuntimeError("no node")
        return self._node


async def test_fetch_lyrics_happy_path_parses_timed():
    node = _FakeNode(payload=_duncte_timed())
    result = await lyrics.fetch_lyrics(_FakePlayer(node))
    assert result.kind == lyrics.KIND_TIMED
    assert node.calls == [
        ("GET", "/sessions/sess-1/players/42/lyrics", {"skipTrackSource": "false"})
    ]


async def test_fetch_lyrics_skip_track_source_flag():
    node = _FakeNode(payload={"text": "x"})
    await lyrics.fetch_lyrics(_FakePlayer(node), skip_track_source=True)
    assert node.calls[0][2] == {"skipTrackSource": "true"}


async def test_fetch_lyrics_no_node_returns_none():
    result = await lyrics.fetch_lyrics(_FakePlayer(None))
    assert result.kind == lyrics.KIND_NONE


async def test_fetch_lyrics_no_session_id_returns_none():
    node = _FakeNode(session_id=None)
    result = await lyrics.fetch_lyrics(_FakePlayer(node))
    assert result.kind == lyrics.KIND_NONE
    assert node.calls == []


async def test_fetch_lyrics_no_guild_returns_none():
    node = _FakeNode(payload=_duncte_timed())
    result = await lyrics.fetch_lyrics(_FakePlayer(node, guild_id=None))
    assert result.kind == lyrics.KIND_NONE
    assert node.calls == []


async def test_fetch_lyrics_swallows_send_error():
    node = _FakeNode(raise_exc=RuntimeError("boom"))
    result = await lyrics.fetch_lyrics(_FakePlayer(node))
    assert result.kind == lyrics.KIND_NONE


# ---------------------------------------------------------------------------
# Session machinery fakes
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self):
        self.edits = 0
        self.deleted = 0

    async def edit(self, **kwargs):
        self.edits += 1

    async def delete(self):
        self.deleted += 1


class _FakeChannel:
    def __init__(self):
        self.sent = 0
        self.message = _FakeMessage()

    async def send(self, **kwargs):
        self.sent += 1
        return self.message


def _track(identifier="T1"):
    return types.SimpleNamespace(
        identifier=identifier, title="Song", uri="http://x", author="Artist"
    )


class _FakeSessionPlayer:
    def __init__(self, position=0, track=None, connected=True):
        self.position = position
        self.current = track
        self.channel = object() if connected else None
        self.guild = types.SimpleNamespace(id=7)


def _timed_result():
    return lyrics.parse_lyrics(_duncte_timed())


async def _start(sessions, guild_id, channel=None, track=None, position=0):
    player = _FakeSessionPlayer(position=position, track=track or _track())
    return await sessions.start(
        guild_id=guild_id,
        player=player,
        channel=channel or _FakeChannel(),
        result=_timed_result(),
        track=track or _track(),
    )


# ---------------------------------------------------------------------------
# LyricsSessions bookkeeping
# ---------------------------------------------------------------------------


async def test_session_start_acquires_slot_and_registers():
    ceiling = GlobalCeiling(25)
    sessions = lyrics.LyricsSessions(ceiling)
    session = await _start(sessions, 7)
    try:
        assert session is not None
        assert sessions.count() == 1
        assert ceiling.count() == 1
        assert sessions.get(7) is session
    finally:
        await sessions.stop(7)


async def test_session_reinvoke_replaces_not_duplicates():
    ceiling = GlobalCeiling(25)
    sessions = lyrics.LyricsSessions(ceiling)
    first = await _start(sessions, 7)
    second = await _start(sessions, 7)
    try:
        assert first is not second
        assert sessions.get(7) is second
        assert sessions.count() == 1  # never a second live message per guild
        assert ceiling.count() == 1
    finally:
        await sessions.stop(7)


async def test_session_bounded_by_ceiling():
    ceiling = GlobalCeiling(2)
    sessions = lyrics.LyricsSessions(ceiling)
    try:
        assert await _start(sessions, 1) is not None
        assert await _start(sessions, 2) is not None
        # Third distinct guild is refused cleanly when the ceiling is full.
        assert await _start(sessions, 3) is None
        assert sessions.count() == 2
        assert ceiling.count() == 2
    finally:
        await sessions.stop(1)
        await sessions.stop(2)


async def test_session_stop_releases_slot():
    ceiling = GlobalCeiling(25)
    sessions = lyrics.LyricsSessions(ceiling)
    await _start(sessions, 7)
    assert await sessions.stop(7) is True
    assert sessions.count() == 0
    assert ceiling.count() == 0
    # Stopping an absent guild is a harmless no-op that still frees any slot.
    assert await sessions.stop(7) is False


async def test_session_notify_track_keeps_same_track_ends_on_change():
    ceiling = GlobalCeiling(25)
    sessions = lyrics.LyricsSessions(ceiling)
    await _start(sessions, 7, track=_track("SAME"))
    try:
        # A reconnect re-fires track_start for the SAME track: session survives.
        await sessions.notify_track(7, "SAME")
        assert sessions.count() == 1
        # A genuine next track ends it and frees the slot.
        await sessions.notify_track(7, "DIFFERENT")
        assert sessions.count() == 0
        assert ceiling.count() == 0
    finally:
        await sessions.stop(7)


async def test_session_shutdown_cancels_and_clears():
    ceiling = GlobalCeiling(25)
    sessions = lyrics.LyricsSessions(ceiling)
    await _start(sessions, 1)
    await _start(sessions, 2)
    sessions.shutdown()
    assert sessions.count() == 0
    assert ceiling.count() == 0
    await asyncio.sleep(0)  # let the cancelled loops settle


# ---------------------------------------------------------------------------
# SyncedLyricsSession._tick_once (deterministic drive, injected clock)
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self):
        self.detached = []

    def _detach(self, guild_id):
        self.detached.append(guild_id)


def _make_session(clock_holder, player):
    session = lyrics.SyncedLyricsSession(
        guild_id=7,
        player=player,
        channel=_FakeChannel(),
        result=_timed_result(),
        track=_track("T1"),
        registry=_FakeRegistry(),
        clock=lambda: clock_holder[0],
        edit_interval=6.0,
    )
    # Prime the state ``start()`` would set, without spinning the background loop.
    session.message = _FakeMessage()
    session._last_index = 0
    session._last_edit_ts = 0.0
    return session


async def test_tick_no_edit_when_line_unchanged():
    clock = [1000.0]
    # position 1000 -> index 0, same as primed _last_index.
    player = _FakeSessionPlayer(position=1000, track=_track("T1"))
    session = _make_session(clock, player)
    await session._tick_once()
    assert session.message.edits == 0


async def test_tick_no_edit_when_changed_but_too_soon():
    clock = [3.0]  # 3s since last edit at 0.0, interval is 6s
    player = _FakeSessionPlayer(position=3000, track=_track("T1"))  # index 1
    session = _make_session(clock, player)
    await session._tick_once()
    assert session.message.edits == 0
    assert session._last_index == 0  # unchanged: no edit committed


async def test_tick_edits_when_changed_and_interval_elapsed():
    clock = [6.0]
    player = _FakeSessionPlayer(position=3000, track=_track("T1"))  # index 1
    session = _make_session(clock, player)
    await session._tick_once()
    assert session.message.edits == 1
    assert session._last_index == 1
    assert session._last_edit_ts == 6.0
    assert "**second**" in session._last_body


async def test_tick_stops_on_track_change():
    clock = [6.0]
    player = _FakeSessionPlayer(position=3000, track=_track("T2"))  # different id
    session = _make_session(clock, player)
    await session._tick_once()
    assert session._stopped
    assert 7 in session._registry.detached
    assert session.message.edits == 1  # finalised into the stopped state


async def test_tick_stops_on_disconnect():
    clock = [6.0]
    player = _FakeSessionPlayer(position=3000, track=None, connected=False)
    session = _make_session(clock, player)
    await session._tick_once()
    assert session._stopped
    assert 7 in session._registry.detached


def _bare_session(channel, clock_holder=None):
    """A session wired to ``channel`` without priming start()'s state."""
    holder = clock_holder or [0.0]
    return lyrics.SyncedLyricsSession(
        guild_id=7,
        player=_FakeSessionPlayer(position=0, track=_track("T1")),
        channel=channel,
        result=_timed_result(),
        track=_track("T1"),
        registry=_FakeRegistry(),
        clock=lambda: holder[0],
    )


async def test_start_deletes_orphan_when_stopped_mid_send():
    # A concurrent replace / notify_track / teardown can stop a session while its
    # initial channel.send is still in flight. The just-posted message must be
    # deleted (not orphaned live-looking with a dead Stop button) and no loop
    # started for the dead session.
    class _StoppingChannel:
        def __init__(self):
            self.message = _FakeMessage()
            self.session = None

        async def send(self, **kwargs):
            self.session._stopped = True  # superseded during the send
            return self.message

    channel = _StoppingChannel()
    session = _bare_session(channel)
    channel.session = session
    await session.start()
    assert channel.message.deleted == 1
    assert session.message is None
    assert session._task is None


async def test_start_noops_when_already_stopped():
    # Stopped before start() ran (a race lost outright): never touch the channel.
    class _CountingChannel:
        def __init__(self):
            self.sent = 0

        async def send(self, **kwargs):
            self.sent += 1
            return _FakeMessage()

    channel = _CountingChannel()
    session = _bare_session(channel)
    session._stopped = True
    await session.start()
    assert channel.sent == 0
    assert session._task is None

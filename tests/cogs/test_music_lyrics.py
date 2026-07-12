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
* text pagination, the line-boundary scheduler (``next_wake`` truth table: mid-
  line, on-boundary, machine-gun coalescing, outro/empty fallback, drift margin)
  and the line-index dedupe (``should_edit``);
* the fetch seam, driven against a fake ``player``/``node`` that records the
  ``node.send`` call (path, params) and the best-effort "never raises" contract;
* the session machinery - replace-not-duplicate, the process-wide ceiling
  (bounded, released on every stop path), ``notify_track`` and ``shutdown`` -
  and one deterministic ``_tick_once`` drive (edit only when the line index
  changed; skip while paused; auto-stop on track change / disconnect), plus the
  pause / nudge scheduling hooks, with fakes.

``lyrics.py`` duck-types the player/node and imports no sonolink types, so it
imports identically under the stub and the real sonolink.
"""

import asyncio
import json
import time
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
# next_wake (line-boundary scheduler)
# ---------------------------------------------------------------------------


def test_next_wake_sleeps_to_next_boundary_mid_line():
    # Lines spaced well beyond MIN_EDIT_GAP: from mid-line, sleep to the next
    # start, offset by the drift margin so the wake lands just past it.
    lines = _lines(1000, 5000, 9000)
    # position 2000 (inside line 0): next allowed boundary is 5000.
    assert lyrics.next_wake(lines, 2000, min_gap=lyrics.MIN_EDIT_GAP, max_sleep=8.0) == (
        (5000 - 2000) / 1000.0 + lyrics.DRIFT_MARGIN
    )


def test_next_wake_exactly_on_boundary_aims_at_the_following_line():
    # Sitting exactly on a line's start (just transitioned to it): aim at the
    # NEXT line, again drift-offset. 4000 -> 8000 is 4s, well past the gap.
    lines = _lines(0, 4000, 8000)
    assert lyrics.next_wake(lines, 4000, min_gap=lyrics.MIN_EDIT_GAP, max_sleep=8.0) == (
        (8000 - 4000) / 1000.0 + lyrics.DRIFT_MARGIN
    )


def test_next_wake_machine_gun_lines_coalesce_to_first_allowed():
    # A burst closer together than MIN_EDIT_GAP: every boundary within the gap is
    # skipped, the wake aims at the first start at or beyond position + gap.
    lines = _lines(0, 500, 1000, 1500, 2000, 2500, 3000)
    # From 0 with the 1.5s floor, threshold is 1500 -> first allowed start is 1500.
    sleep = lyrics.next_wake(lines, 0, min_gap=lyrics.MIN_EDIT_GAP, max_sleep=8.0)
    assert sleep == 1.5 + lyrics.DRIFT_MARGIN
    assert sleep >= 1.5  # the min-gap floor is honoured


def test_next_wake_no_upcoming_line_falls_back_to_max_sleep():
    # Position past every line (an outro): nothing to aim at, re-check after max.
    lines = _lines(1000, 3000, 5000)
    assert lyrics.next_wake(lines, 9000, min_gap=lyrics.MIN_EDIT_GAP, max_sleep=8.0) == 8.0
    # A last line whose only remaining boundary is within the gap also falls back.
    assert lyrics.next_wake(lines, 5000, min_gap=lyrics.MIN_EDIT_GAP, max_sleep=8.0) == 8.0


def test_next_wake_empty_lines_is_max_sleep():
    assert lyrics.next_wake((), 1234, min_gap=lyrics.MIN_EDIT_GAP, max_sleep=8.0) == 8.0


def test_next_wake_far_boundary_clamped_to_max_sleep():
    # The next boundary is 20s out: clamp to max so drift/seek can't strand us.
    lines = _lines(0, 20000)
    assert lyrics.next_wake(lines, 0, min_gap=lyrics.MIN_EDIT_GAP, max_sleep=8.0) == 8.0


def test_next_wake_drift_margin_lands_after_the_transition():
    # The returned sleep is strictly greater than the raw time-to-boundary, so the
    # wake fires AFTER the nominal start (extrapolation trails the node by ~ms).
    lines = _lines(0, 6000)
    raw = (6000 - 0) / 1000.0
    assert lyrics.next_wake(lines, 0, min_gap=lyrics.MIN_EDIT_GAP, max_sleep=8.0) == raw + lyrics.DRIFT_MARGIN


# ---------------------------------------------------------------------------
# should_edit (line-index dedupe; the time floor now lives in next_wake)
# ---------------------------------------------------------------------------


def test_should_edit_false_when_line_unchanged():
    # Same index: never edit, the wake landed on the same line (a re-check).
    assert not lyrics.should_edit(last_index=2, current_index=2)


def test_should_edit_true_when_line_changed():
    assert lyrics.should_edit(last_index=1, current_index=2)


def test_should_edit_true_from_before_first_to_first_line():
    assert lyrics.should_edit(last_index=lyrics.BEFORE_FIRST, current_index=0)


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


async def test_tick_edits_when_line_changed():
    # The scheduler owns the time floor now: the tick edits whenever the wake
    # landed on a new line, regardless of the clock.
    clock = [3.0]
    player = _FakeSessionPlayer(position=3000, track=_track("T1"))  # index 1
    session = _make_session(clock, player)
    await session._tick_once()
    assert session.message.edits == 1
    assert session._last_index == 1
    assert session._last_edit_ts == 3.0
    assert "**second**" in session._last_body


async def test_tick_no_edit_when_paused():
    # A paused player's position is frozen; the tick must skip editing even when
    # the (stale) position would pick a different line than last time.
    clock = [6.0]
    player = _FakeSessionPlayer(position=3000, track=_track("T1"))  # index 1
    player.paused = True
    session = _make_session(clock, player)
    await session._tick_once()
    assert session.message.edits == 0
    assert session._last_index == 0  # untouched while paused


def test_next_sleep_is_max_when_paused():
    # A paused session re-checks at the ceiling rather than aiming at a boundary
    # its frozen position will never reach.
    clock = [0.0]
    player = _FakeSessionPlayer(position=0, track=_track("T1"))
    player.paused = True
    session = _make_session(clock, player)
    assert session._next_sleep() == lyrics.MAX_TICK_SLEEP


async def test_nudge_wakes_the_sleep_early():
    # nudge() sets the wake flag _sleep waits on, so a long sleep returns at once
    # (a seek resyncs immediately) and the flag is cleared for the next cycle.
    clock = [0.0]
    player = _FakeSessionPlayer(position=0, track=_track("T1"))
    session = _make_session(clock, player)
    session.nudge()
    await asyncio.wait_for(session._sleep(1000.0), timeout=1.0)  # returns promptly
    assert not session._wake.is_set()  # cleared, ready to arm again


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


# ---------------------------------------------------------------------------
# Search-then-fetch recipe (the SCH/Gambi regression fix)
# ---------------------------------------------------------------------------


def _meta_track(title, author):
    return types.SimpleNamespace(title=title, author=author)


def test_search_query_strips_bracketed_upload_noise():
    q = lyrics.search_query_for(_meta_track("A7 (Clip Officiel)", "SCH"))
    assert q == "A7 SCH"
    q = lyrics.search_query_for(_meta_track("POPOPOP [Official Video]", "Gambi"))
    assert q == "POPOPOP Gambi"


def test_search_query_keeps_meaningful_brackets():
    # Remaster years and featured artists carry real search signal.
    q = lyrics.search_query_for(
        _meta_track("Never Gonna Give You Up (2022 Remaster)", "Rick Astley")
    )
    assert "(2022 Remaster)" in q
    q = lyrics.search_query_for(_meta_track("Mayday (avec Ninho)", "SCH"))
    assert "(avec Ninho)" in q


def test_search_query_strips_topic_suffix():
    assert lyrics.search_query_for(_meta_track("A7", "SCH - Topic")) == "A7 SCH"


def test_search_query_empty_title_yields_empty():
    assert lyrics.search_query_for(_meta_track("", "SCH")) == ""
    assert lyrics.search_query_for(_meta_track(None, None)) == ""


def test_search_and_video_paths_are_quoted():
    assert lyrics.search_lyrics_path("SCH A7") == "/lyrics/search/SCH%20A7"
    assert (
        lyrics.search_lyrics_path("a/b?c") == "/lyrics/search/a%2Fb%3Fc"
    )
    assert lyrics.video_lyrics_path("jQShQsMWepM") == "/lyrics/jQShQsMWepM"


class _RoutedNode:
    """Scripted node: maps GET paths to payloads or exceptions."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.session_id = "sess"

    async def send(self, method, path, params=None):
        self.calls.append(path)
        result = self.routes.get(path, KeyError(path))
        if isinstance(result, Exception):
            raise result
        return result


def _routed_player(node, title="A7", author="SCH"):
    return types.SimpleNamespace(
        node=node,
        guild=types.SimpleNamespace(id=1),
        current=_meta_track(title, author),
    )


def _timed_payload():
    return {
        "type": "timed",
        "source": "Musixmatch",
        "lines": [{"line": "x", "range": {"start": 0, "end": 1000}}],
    }


def test_fetch_prefers_search_then_fetch(anyio_run=None):
    import asyncio as _a

    node = _RoutedNode({
        "/lyrics/search/A7%20SCH": [{"videoId": "vid1", "title": "A7"}],
        "/lyrics/vid1": _timed_payload(),
    })
    result = _a.run(lyrics.fetch_lyrics(_routed_player(node)))
    assert result.kind == lyrics.KIND_TIMED
    # jamais la route session quand la recherche a gagne
    assert not any(p.startswith("/sessions/") for p in node.calls)


def test_fetch_tries_next_candidate_on_404():
    import asyncio as _a

    node = _RoutedNode({
        "/lyrics/search/A7%20SCH": [
            {"videoId": "dead"},
            {"videoId": "vid2"},
        ],
        "/lyrics/dead": RuntimeError("404"),
        "/lyrics/vid2": _timed_payload(),
    })
    result = _a.run(lyrics.fetch_lyrics(_routed_player(node)))
    assert result.kind == lyrics.KIND_TIMED


def test_fetch_falls_back_to_session_route_when_search_dry():
    import asyncio as _a

    node = _RoutedNode({
        "/lyrics/search/A7%20SCH": [],
        "/sessions/sess/players/1/lyrics": _timed_payload(),
    })
    result = _a.run(lyrics.fetch_lyrics(_routed_player(node)))
    assert result.kind == lyrics.KIND_TIMED
    assert any(p.startswith("/sessions/") for p in node.calls)


def test_fetch_all_paths_dry_degrades_to_none():
    import asyncio as _a

    node = _RoutedNode({
        "/lyrics/search/A7%20SCH": RuntimeError("boom"),
        "/sessions/sess/players/1/lyrics": RuntimeError("404"),
    })
    result = _a.run(lyrics.fetch_lyrics(_routed_player(node)))
    assert result.kind == lyrics.KIND_NONE


# ---------------------------------------------------------------------------
# Tighter edit floor (MIN_EDIT_GAP 2.5 -> 1.5)
# ---------------------------------------------------------------------------


def test_min_edit_gap_is_1_5():
    # The floor was lowered from 2.5s so the bold line turns over more tightly;
    # the SCALE STORY re-derives the per-channel edit budget against this value.
    assert lyrics.MIN_EDIT_GAP == 1.5


def test_next_wake_default_floor_coalesces_at_1_5():
    # Using the module default min_gap, a machine-gun burst coalesces at 1.5s.
    lines = _lines(0, 500, 1000, 1500, 2000)
    assert lyrics.next_wake(lines, 0) == 1.5 + lyrics.DRIFT_MARGIN


# ---------------------------------------------------------------------------
# Position truth: sonolink self-interpolates; the session must not re-extrapolate
# ---------------------------------------------------------------------------


async def test_session_reads_player_position_without_double_extrapolation():
    # sonolink's Player.position already interpolates the last playerUpdate on a
    # monotonic clock and freezes when paused (verified in _base.py:355-373), so
    # the session reads it as-is and layers NO clock of its own. With a fixed fake
    # position, repeated reads are identical - a local extrapolation would drift.
    player = _FakeSessionPlayer(position=4321, track=_track("T1"))
    session = _make_session([0.0], player)
    first = session._effective_position_ms()
    time.sleep(0.02)
    assert session._effective_position_ms() == first == 4321


# ---------------------------------------------------------------------------
# Offset calibration: pure maths (clamp, signed render)
# ---------------------------------------------------------------------------


def test_clamp_offset_within_and_at_bounds():
    assert lyrics.clamp_offset(0) == 0
    assert lyrics.clamp_offset(5000) == 5000
    assert lyrics.clamp_offset(-5000) == -5000
    assert lyrics.clamp_offset(lyrics.OFFSET_LIMIT_MS) == lyrics.OFFSET_LIMIT_MS
    assert lyrics.clamp_offset(-lyrics.OFFSET_LIMIT_MS) == -lyrics.OFFSET_LIMIT_MS


def test_clamp_offset_beyond_bounds_clamps():
    assert lyrics.clamp_offset(999999) == lyrics.OFFSET_LIMIT_MS
    assert lyrics.clamp_offset(-999999) == -lyrics.OFFSET_LIMIT_MS


def test_format_offset_signed_one_decimal():
    assert lyrics.format_offset(1000) == "+1.0"
    assert lyrics.format_offset(-5000) == "-5.0"
    assert lyrics.format_offset(2500) == "+2.5"
    assert lyrics.format_offset(-30000) == "-30.0"
    assert lyrics.format_offset(0) == "+0.0"
    # ASCII hyphen-minus only, never a Unicode minus sign (U+2212).
    assert "\u2212" not in lyrics.format_offset(-5000)


# ---------------------------------------------------------------------------
# Offset calibration: selection shift and per-session reset
# ---------------------------------------------------------------------------


def test_session_offset_defaults_to_zero():
    session = _make_session([0.0], _FakeSessionPlayer(position=0, track=_track("T1")))
    assert session.offset_ms == 0


def test_offset_shifts_effective_position_and_selection():
    # Lines at 1000/3000/5000. At position 1000 the current line is index 0; a
    # +2000ms offset advances the effective position to 3000 (index 1); a large
    # negative offset pulls it before the first line.
    player = _FakeSessionPlayer(position=1000, track=_track("T1"))
    session = _make_session([0.0], player)
    idx = lyrics.current_line_index(session.lines, session._effective_position_ms())
    assert idx == 0
    session.offset_ms = 2000
    assert session._effective_position_ms() == 3000
    assert lyrics.current_line_index(session.lines, session._effective_position_ms()) == 1
    session.offset_ms = -2000
    assert (
        lyrics.current_line_index(session.lines, session._effective_position_ms())
        == lyrics.BEFORE_FIRST
    )


def test_offset_resets_per_track_new_session():
    # A new track is always a NEW session object, so a calibrated offset never
    # leaks across a track change (a new cut resets to 0).
    s1 = _make_session([0.0], _FakeSessionPlayer(position=0, track=_track("T1")))
    s1.offset_ms = 7000
    s2 = _make_session([0.0], _FakeSessionPlayer(position=0, track=_track("T2")))
    assert s1.offset_ms == 7000
    assert s2.offset_ms == 0


# ---------------------------------------------------------------------------
# Offset calibration: the button interaction (same-voice gate, edit, clamp)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self):
        self.edited = 0
        self.deferred = 0
        self.messages = []

    async def edit_message(self, **kwargs):
        self.edited += 1

    async def defer(self):
        self.deferred += 1

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.response = _FakeResponse()


def _voice_interaction(session):
    """An interaction from a member in the player's voice channel (allowed)."""
    member = types.SimpleNamespace(
        voice=types.SimpleNamespace(channel=session.player.channel)
    )
    return _FakeInteraction(member)


def _outside_interaction():
    """An interaction from a member not in any voice channel (refused)."""
    return _FakeInteraction(types.SimpleNamespace(voice=None))


async def test_offset_button_shifts_selection_and_edits():
    player = _FakeSessionPlayer(position=1000, track=_track("T1"))  # index 0
    session = _make_session([5.0], player)
    interaction = _voice_interaction(session)
    await session.shift_offset_from_interaction(interaction, delta_ms=2000)
    assert session.offset_ms == 2000
    # Effective 3000 -> line index 1 ("second") now current; re-primed for the loop.
    assert session._last_index == 1
    assert "**second**" in session._last_body
    assert session._last_edit_ts == 5.0
    assert interaction.response.edited == 1  # edited via the interaction, immediately
    assert session._wake.is_set()  # loop nudged to re-plan its schedule


async def test_offset_button_refuses_outside_voice():
    player = _FakeSessionPlayer(position=1000, track=_track("T1"))
    session = _make_session([0.0], player)
    interaction = _outside_interaction()
    await session.shift_offset_from_interaction(interaction, delta_ms=5000)
    assert session.offset_ms == 0  # untouched
    assert interaction.response.messages  # an ephemeral refusal was sent
    assert interaction.response.edited == 0


async def test_offset_button_clamps_over_repeated_clicks():
    player = _FakeSessionPlayer(position=0, track=_track("T1"))
    session = _make_session([0.0], player)
    interaction = _voice_interaction(session)
    for _ in range(10):  # 10 x +5s would be 50s without the clamp
        await session.shift_offset_from_interaction(
            interaction, delta_ms=lyrics.OFFSET_STEP_LARGE_MS
        )
    assert session.offset_ms == lyrics.OFFSET_LIMIT_MS


async def test_offset_button_noops_when_stopped():
    player = _FakeSessionPlayer(position=0, track=_track("T1"))
    session = _make_session([0.0], player)
    session._stopped = True
    interaction = _voice_interaction(session)
    await session.shift_offset_from_interaction(interaction, delta_ms=5000)
    assert session.offset_ms == 0  # a stopped session is not revived
    assert interaction.response.edited == 0
    assert interaction.response.deferred == 1


# ---------------------------------------------------------------------------
# Offset calibration: the card render (footer + button rows)
# ---------------------------------------------------------------------------


def test_card_footer_shows_signed_offset_when_nonzero():
    session = _make_session([0.0], _FakeSessionPlayer(position=0, track=_track("T1")))
    session._card.set_state(body="x")
    assert "Lyrics offset" not in json.dumps(session._card.to_components())
    session.offset_ms = -1000
    session._card.set_state(body="x")
    assert "Lyrics offset: -1.0s" in json.dumps(session._card.to_components())


def _button_labels(view):
    """Every button (component type 2) label in a rendered view, in tree order."""
    out = []

    def walk(node):
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            if node.get("type") == 2:
                out.append(node.get("label"))
            for value in node.values():
                walk(value)

    walk(view.to_components())
    return out


def test_card_has_offset_row_and_stop_when_active_dropped_when_stopped():
    session = _make_session([0.0], _FakeSessionPlayer(position=0, track=_track("T1")))
    session._card.set_state(body="x")
    # Calibration steps first (a group), then Stop on its own row; total legal.
    assert _button_labels(session._card) == ["-5s", "-1s", "+1s", "+5s", "Stop"]
    session._card.set_state(body="x", stopped=True)
    assert _button_labels(session._card) == []  # a stopped card drops all buttons


# ---------------------------------------------------------------------------
# Smarter candidate pick: title similarity, variant markers, ranking
# ---------------------------------------------------------------------------


def test_title_similarity_bounds():
    assert lyrics.title_similarity("A7", "A7") == 1.0
    assert lyrics.title_similarity("A7", "Bohemian Rhapsody") == 0.0
    assert lyrics.title_similarity("A7", "") == 0.0
    # Shared tokens over the union: {never,gonna} of {never,gonna,give|run} = 0.5.
    assert lyrics.title_similarity("Never Gonna Give", "Never Gonna Run") == 0.5


def test_title_similarity_ignores_order_and_upload_noise():
    # Word order does not matter, and bracketed upload noise is stripped both sides.
    assert lyrics.title_similarity("A7 SCH", "SCH A7") == 1.0
    assert lyrics.title_similarity("A7 (Clip Officiel)", "A7") == 1.0


def test_has_variant_marker_truth():
    for good in [
        "Song (sped up)",
        "Song - Sped Up",
        "Song (spedup)",
        "Song (Slowed + Reverb)",
        "Song (Live)",
        "Song (Nightcore)",
        "Song (Radio Remix)",
    ]:
        assert lyrics._has_variant_marker(good), good
    for plain in ["Song", "A7", "Bohemian Rhapsody", "Thriller"]:
        assert not lyrics._has_variant_marker(plain), plain


def test_rank_candidates_exact_non_variant_first():
    cands = [
        {"videoId": "live", "title": "A7 (Live)"},
        {"videoId": "studio", "title": "A7"},
    ]
    assert [c["videoId"] for c in lyrics.rank_candidates(cands, "A7")] == [
        "studio",
        "live",
    ]


def test_rank_candidates_deprioritizes_sped_up():
    cands = [
        {"videoId": "fast", "title": "Blinding Lights (sped up)"},
        {"videoId": "orig", "title": "Blinding Lights"},
    ]
    ranked = lyrics.rank_candidates(cands, "Blinding Lights")
    assert ranked[0]["videoId"] == "orig"


def test_rank_candidates_keeps_variant_when_playing_is_variant():
    # The playing upload is itself a sped-up cut: the marker carries real signal,
    # so a sped-up candidate is NOT penalised and wins on the exact title match.
    cands = [
        {"videoId": "orig", "title": "Blinding Lights"},
        {"videoId": "fast", "title": "Blinding Lights (sped up)"},
    ]
    ranked = lyrics.rank_candidates(cands, "Blinding Lights (sped up)")
    assert ranked[0]["videoId"] == "fast"


def test_rank_candidates_foreign_junk_last():
    cands = [
        {"videoId": "junk", "title": "Totally Unrelated Track"},
        {"videoId": "match", "title": "A7"},
    ]
    assert lyrics.rank_candidates(cands, "A7")[0]["videoId"] == "match"


def test_rank_candidates_drops_unfetchable():
    cands = [
        {"title": "A7"},  # no videoId
        None,  # not a mapping
        {"videoId": "", "title": "A7"},  # empty videoId
        {"videoId": "keep", "title": "A7"},
    ]
    assert [c["videoId"] for c in lyrics.rank_candidates(cands, "A7")] == ["keep"]


def test_rank_candidates_stable_within_a_tier_and_limited():
    cands = [{"videoId": f"v{i}", "title": "A7"} for i in range(5)]
    ranked = lyrics.rank_candidates(cands, "A7", limit=3)
    assert [c["videoId"] for c in ranked] == ["v0", "v1", "v2"]


def test_fetch_ranks_studio_before_live_variant():
    import asyncio as _a

    node = _RoutedNode({
        "/lyrics/search/A7%20SCH": [
            {"videoId": "live", "title": "A7 (Live)"},
            {"videoId": "studio", "title": "A7"},
        ],
        "/lyrics/studio": _timed_payload(),
        "/lyrics/live": _timed_payload(),
    })
    result = _a.run(lyrics.fetch_lyrics(_routed_player(node)))
    assert result.kind == lyrics.KIND_TIMED
    # The non-variant studio cut is fetched first; the live variant is never tried.
    assert node.calls == ["/lyrics/search/A7%20SCH", "/lyrics/studio"]

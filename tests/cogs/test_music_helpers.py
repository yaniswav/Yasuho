"""Unit tests for the pure helpers in ``cogs/music/music.py``.

These cover the two module-level building blocks that have no live I/O:

* ``format_duration`` - renders a track's length as ``mm:ss`` (integer minutes
  and seconds), or the literal ``LIVE`` for a stream.
* ``_first_track`` - normalises the three shapes a sonolink search result can
  take (a ``Playlist``, a list of tracks, or a single track) into one usable
  track, returning ``None`` for the missing / errored / empty cases.

Fakes are built with ``types.SimpleNamespace``; the ``Playlist`` branch swaps
``sonolink.models.Playlist`` for a small local class via monkeypatch (music.py
resolves that name at call time, and the real Playlist exposes ``tracks`` as a
read-only property, so it cannot be hand-built). ``sonolink`` is stubbed by the
repo-root conftest on the 3.10 dev box and imported for real on 3.12+ CI.

A small guard also asserts the module's interactive UI classes never reintroduce
the ``_refresh`` name collision that caused a production crash: discord.py calls
``View._refresh(self, components)`` on MESSAGE_UPDATE, and a subclass method of
the same name shadows it and crashes the gateway. The music controller's own
refresh method was renamed to ``_rerender`` for exactly this reason.
"""

import inspect
import types

import discord

from cogs.music import music, views


def _result(*, is_error=False, is_empty=False, result=None):
    """Build a fake search result mimicking sonolink's result envelope.

    ``_first_track`` calls ``result.is_error()`` / ``result.is_empty()`` and
    reads ``result.result``; a SimpleNamespace with zero-arg lambdas covers all
    three without pulling in the real sonolink result type.
    """
    return types.SimpleNamespace(
        is_error=lambda: is_error,
        is_empty=lambda: is_empty,
        result=result,
    )


def _track(title="song"):
    return types.SimpleNamespace(title=title)


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


def test_format_duration_stream_returns_live():
    # A stream has no meaningful length; length is ignored entirely.
    track = types.SimpleNamespace(is_stream=True, length=999999)
    assert music.format_duration(track) == "LIVE"


def test_format_duration_ms_to_mm_ss():
    # 125000 ms -> 125 s -> 2 min 5 s -> "02:05".
    track = types.SimpleNamespace(is_stream=False, length=125000)
    assert music.format_duration(track) == "02:05"


def test_format_duration_zero_length():
    track = types.SimpleNamespace(is_stream=False, length=0)
    assert music.format_duration(track) == "00:00"


def test_format_duration_pads_single_digits():
    # 65000 ms -> 65 s -> 1 min 5 s -> "01:05" (both fields zero-padded).
    track = types.SimpleNamespace(is_stream=False, length=65000)
    assert music.format_duration(track) == "01:05"


def test_format_duration_floors_partial_second():
    # 125999 ms floors to 125 s via integer division; no rounding up to 02:06.
    track = types.SimpleNamespace(is_stream=False, length=125999)
    assert music.format_duration(track) == "02:05"


def test_format_duration_over_ten_minutes_not_truncated():
    # 630000 ms -> 630 s -> 10 min 30 s; minutes are not clamped to two chars.
    track = types.SimpleNamespace(is_stream=False, length=630000)
    assert music.format_duration(track) == "10:30"


def test_format_duration_long_track_rolls_into_hours():
    # 3661000 ms -> 3661 s -> 1 h 1 min 1 s -> "1:01:01" once past an hour.
    track = types.SimpleNamespace(is_stream=False, length=3661000)
    assert music.format_duration(track) == "1:01:01"


# ---------------------------------------------------------------------------
# _first_track
# ---------------------------------------------------------------------------


def test_first_track_none_result():
    assert music._first_track(None) is None


def test_first_track_error_result():
    # is_error() True short-circuits to None before any track is read.
    assert music._first_track(_result(is_error=True)) is None


def test_first_track_empty_result():
    assert music._first_track(_result(is_empty=True)) is None


def test_first_track_result_payload_none():
    # Envelope is fine but carries no payload -> None.
    assert music._first_track(_result(result=None)) is None


class _FakePlaylist:
    """Stand-in for sonolink.models.Playlist with a settable ``tracks`` list.

    The real Playlist exposes ``tracks`` as a read-only property, so the tests
    swap the type in via monkeypatch (music.py resolves sonolink.models.Playlist
    at call time) rather than constructing the real one.
    """

    def __init__(self, tracks):
        self.tracks = tracks


def test_first_track_playlist_returns_first(monkeypatch):
    import sonolink.models as sonolink_models

    monkeypatch.setattr(sonolink_models, "Playlist", _FakePlaylist)
    first = _track("first")
    second = _track("second")
    playlist = _FakePlaylist([first, second])
    assert music._first_track(_result(result=playlist)) is first


def test_first_track_empty_playlist_returns_none(monkeypatch):
    import sonolink.models as sonolink_models

    monkeypatch.setattr(sonolink_models, "Playlist", _FakePlaylist)
    playlist = _FakePlaylist([])
    assert music._first_track(_result(result=playlist)) is None


def test_first_track_list_returns_first():
    first = _track("first")
    second = _track("second")
    assert music._first_track(_result(result=[first, second])) is first


def test_first_track_empty_list_returns_none():
    assert music._first_track(_result(result=[])) is None


def test_first_track_single_object_returns_itself():
    # Not a Playlist and not a list -> the object is handed back untouched.
    track = _track("only")
    assert music._first_track(_result(result=track)) is track


# ---------------------------------------------------------------------------
# Regression: the controller must render from a fallback track during the
# cold-restore race where player.current is not set yet
# ---------------------------------------------------------------------------


def test_controller_renders_from_fallback_track_before_current_set():
    """A controller built off a track_start event must render even while
    sonolink's play() REST update is still in flight and player.current is
    None (the cold-restore race that used to post no controller).
    """
    import sonolink

    track = types.SimpleNamespace(
        title="Race Track", uri="http://example/x", author="DJ",
        is_stream=False, length=125000,
        extras=types.SimpleNamespace(requester=None),
    )
    player = types.SimpleNamespace(
        current=None, paused=False, volume=100,
        queue=types.SimpleNamespace(mode=sonolink.QueueMode.NORMAL, tracks=[]),
        channel=types.SimpleNamespace(name="General"), dj=None,
        autoplay=sonolink.AutoPlayMode.ENABLED,
    )
    view = music.MusicController(None, player, track=track)
    texts = [
        c.content for c in view.walk_children()
        if isinstance(c, discord.ui.TextDisplay)
    ]
    assert any("Race Track" in t for t in texts)
    assert not any("Nothing is playing" in t for t in texts)


def test_controller_current_track_overrides_fallback():
    """Once player.current is set it wins over the fallback track."""
    import sonolink

    live = types.SimpleNamespace(
        title="Live Track", uri=None, author="A", is_stream=False,
        length=1000, extras=types.SimpleNamespace(requester=None),
    )
    stale = types.SimpleNamespace(
        title="Stale Track", uri=None, author="B", is_stream=False,
        length=1000, extras=types.SimpleNamespace(requester=None),
    )
    player = types.SimpleNamespace(
        current=live, paused=False, volume=100,
        queue=types.SimpleNamespace(mode=sonolink.QueueMode.NORMAL, tracks=[]),
        channel=types.SimpleNamespace(name="G"), dj=None,
        autoplay=sonolink.AutoPlayMode.ENABLED,
    )
    view = music.MusicController(None, player, track=stale)
    texts = [
        c.content for c in view.walk_children()
        if isinstance(c, discord.ui.TextDisplay)
    ]
    assert any("Live Track" in t for t in texts)
    assert not any("Stale Track" in t for t in texts)


# ---------------------------------------------------------------------------
# Regression: a GENUINE track change must UPDATE the controller. The keep vs
# rerender vs repost decision must key off what the panel actually RENDERS, not
# the live player.current (which sonolink advances to the new track before that
# track's track_start event reaches the cog - so comparing against current
# always matched and wrongly kept the panel stuck on the previous track).
# ---------------------------------------------------------------------------


def test_decide_controller_action_user_driven_reposts():
    # dedupe=False (a /play-no-query or /nowplaying repost) always reposts fresh
    # at the bottom of the channel, even if the same track is already shown.
    assert (
        music.decide_controller_action(
            dedupe=False,
            has_live_controller=True,
            displayed_id="a",
            incoming_id="a",
            age_seconds=1.0,
        )
        == "repost"
    )


def test_decide_controller_action_no_live_controller_reposts():
    # No controller (or its message is gone): nothing to keep or edit -> repost.
    assert (
        music.decide_controller_action(
            dedupe=True,
            has_live_controller=False,
            displayed_id=None,
            incoming_id="a",
            age_seconds=None,
        )
        == "repost"
    )


def test_decide_controller_action_same_track_recent_keeps():
    # Reconnect re-fire: the panel already shows this track and went up seconds
    # ago -> keep the message untouched so it never flickers.
    assert (
        music.decide_controller_action(
            dedupe=True,
            has_live_controller=True,
            displayed_id="a",
            incoming_id="a",
            age_seconds=2.0,
        )
        == "keep"
    )


def test_decide_controller_action_same_track_old_reposts():
    # /loop track: the SAME track re-fires long after its panel went up -> repost
    # so it returns to the channel bottom (preserves the loop-track behaviour).
    assert (
        music.decide_controller_action(
            dedupe=True,
            has_live_controller=True,
            displayed_id="a",
            incoming_id="a",
            age_seconds=music.CONTROLLER_REFIRE_WINDOW + 1.0,
        )
        == "repost"
    )


def test_decide_controller_action_different_track_rerenders():
    # The core bug: a genuine advance to a DIFFERENT track, even seconds after
    # the previous track's panel went up, must update the panel - never keep it.
    assert (
        music.decide_controller_action(
            dedupe=True,
            has_live_controller=True,
            displayed_id="a",
            incoming_id="b",
            age_seconds=1.0,
        )
        == "rerender"
    )


def test_decide_controller_action_empty_panel_rerenders_onto_track():
    # Panel currently shows "nothing playing" (displayed_id None); a track start
    # differs from None -> rerender the empty panel onto the new track.
    assert (
        music.decide_controller_action(
            dedupe=True,
            has_live_controller=True,
            displayed_id=None,
            incoming_id="a",
            age_seconds=1.0,
        )
        == "rerender"
    )


def test_controller_rerender_updates_fallback_track_and_rendered_id():
    """The fallback-track trap: on a genuine change during the current-is-None
    race, _build reads ``player.current or self._track``, so the fallback must be
    updated to the NEW track or the panel stays stuck on the previous one. This
    exercises the pure build half of ``_rerender_for_track`` (the message edit is
    live-only) and confirms ``_rendered_id`` tracks what was rendered.
    """
    import sonolink

    def _mk(title, ident):
        return types.SimpleNamespace(
            title=title, uri=None, author="A", is_stream=False, length=1000,
            identifier=ident, extras=types.SimpleNamespace(requester=None),
        )

    first = _mk("First Track", "id-first")
    player = types.SimpleNamespace(
        current=None, paused=False, volume=100,
        queue=types.SimpleNamespace(mode=sonolink.QueueMode.NORMAL, tracks=[]),
        channel=types.SimpleNamespace(name="G"), dj=None,
        autoplay=sonolink.AutoPlayMode.ENABLED,
    )
    view = music.MusicController(None, player, track=first)
    assert view._rendered_id == "id-first"

    # A different track starts while player.current is still None (the race
    # window). _rerender_for_track updates the fallback before _build; emulate
    # that pure part here so the trap is covered without live discord I/O.
    second = _mk("Second Track", "id-second")
    view._track = second
    view._build()

    texts = [
        c.content for c in view.walk_children()
        if isinstance(c, discord.ui.TextDisplay)
    ]
    assert any("Second Track" in t for t in texts)
    assert not any("First Track" in t for t in texts)
    assert view._rendered_id == "id-second"


# ---------------------------------------------------------------------------
# Regression guard: no music UI class may shadow View._refresh
# ---------------------------------------------------------------------------


def test_music_ui_classes_do_not_shadow_refresh():
    """Guard against the prod crash: a subclass ``_refresh`` shadows the base.

    discord.py invokes ``View._refresh(self, components)`` on MESSAGE_UPDATE. A
    View/Modal/LayoutView subclass that defines its own ``_refresh`` overrides it
    with an incompatible signature and crashes the gateway (the music
    controller's method was renamed to ``_rerender`` to fix this). Any UI class
    defined in the music package's views module must not reintroduce that name.
    """
    ui_bases = tuple(
        base
        for name in ("View", "Modal", "LayoutView")
        for base in (getattr(discord.ui, name, None),)
        if base is not None
    )
    checked = []
    for obj in vars(views).values():
        if (
            inspect.isclass(obj)
            and obj.__module__ == views.__name__
            and issubclass(obj, ui_bases)
        ):
            checked.append(obj)
            assert "_refresh" not in obj.__dict__, (
                f"{obj.__name__} defines its own _refresh and shadows "
                "discord.ui.View._refresh; rename it (the prod fix used "
                "_rerender)."
            )

    # Sanity: the guard actually inspected the known controller/modal, so a
    # future refactor that renames or moves them does not silently no-op.
    names = {cls.__name__ for cls in checked}
    assert {"MusicController", "AddSongModal"} <= names


# ---------------------------------------------------------------------------
# Now-playing progress bar (lot B4): a PURE (position, duration) -> str render,
# plus the anti-noop key that lets the existing 60s idle tick advance the bar
# without ever posting an edit that changes nothing.
# ---------------------------------------------------------------------------


def _bar_of(line):
    """Extract the segment glyphs from a rendered progress line."""
    return "".join(
        c for c in line if c in (views.PROGRESS_FILLED, views.PROGRESS_EMPTY)
    )


def test_progress_line_at_zero_is_empty_bar():
    # Nothing played yet: twelve empty segments, elapsed 00:00, total intact.
    line = views.render_progress_line(0, 240000)
    assert _bar_of(line) == views.PROGRESS_EMPTY * views.PROGRESS_SEGMENTS
    assert "00:00" in line and "04:00" in line


def test_progress_line_at_half_fills_half_the_segments():
    # 120000 / 240000 -> exactly six of the twelve segments filled.
    line = views.render_progress_line(120000, 240000)
    assert _bar_of(line) == (
        views.PROGRESS_FILLED * 6 + views.PROGRESS_EMPTY * 6
    )
    assert "02:00" in line


def test_progress_line_at_end_fills_every_segment():
    line = views.render_progress_line(240000, 240000)
    assert _bar_of(line) == views.PROGRESS_FILLED * views.PROGRESS_SEGMENTS
    assert views.PROGRESS_EMPTY not in line


def test_progress_line_past_the_end_clamps_instead_of_overflowing():
    # sonolink interpolates position from the last node update, so it can read a
    # touch past the length between the final tick and track_end. The bar must
    # stay twelve segments wide and the elapsed stamp must not exceed the total.
    line = views.render_progress_line(999999, 240000)
    assert _bar_of(line) == views.PROGRESS_FILLED * views.PROGRESS_SEGMENTS
    assert "04:00" in line and "16:39" not in line


def test_progress_line_negative_position_clamps_to_zero():
    line = views.render_progress_line(-5000, 240000)
    assert _bar_of(line) == views.PROGRESS_EMPTY * views.PROGRESS_SEGMENTS
    assert "00:00" in line


def test_progress_line_live_stream_shows_badge_not_a_bar():
    # duration 0 is how a stream reaches the renderer: no end to fill towards.
    line = views.render_progress_line(123456, 0)
    assert "LIVE" in line
    assert _bar_of(line) == ""


def test_progress_line_duration_none_shows_badge_not_a_bar():
    line = views.render_progress_line(123456, None)
    assert "LIVE" in line
    assert _bar_of(line) == ""


def test_progress_line_over_an_hour_rolls_into_h_mm_ss():
    # 3661000 ms total, 3600000 ms in: both stamps switch to h:mm:ss.
    line = views.render_progress_line(3600000, 3661000)
    assert "1:00:00" in line and "1:01:01" in line


def test_track_duration_ms_returns_none_for_streams_and_junk():
    assert views.track_duration_ms(None) is None
    assert views.track_duration_ms(
        types.SimpleNamespace(is_stream=True, length=999999)
    ) is None
    assert views.track_duration_ms(
        types.SimpleNamespace(is_stream=False, length=0)
    ) is None
    assert views.track_duration_ms(
        types.SimpleNamespace(is_stream=False, length=None)
    ) is None
    assert views.track_duration_ms(
        types.SimpleNamespace(is_stream=False, length=240000)
    ) == 240000


def test_progress_state_is_constant_for_a_live_stream():
    # A badge has nothing that can move, so every position keys the same and the
    # idle tick never edits a livestream's panel.
    assert views.progress_state(0, None) == views.progress_state(9999999, None)
    assert views.progress_state(0, 0) == views.progress_state(60000, 0)


def test_progress_state_keys_on_segment_and_displayed_minute():
    # Same segment AND same displayed minute -> identical key (no edit owed).
    assert views.progress_state(61000, 3600000) == views.progress_state(
        90000, 3600000
    )
    # Crossing a minute changes the key even inside one segment (a 60-minute
    # track keeps five minutes per segment).
    assert views.progress_state(61000, 3600000) != views.progress_state(
        121000, 3600000
    )
    # Crossing a segment changes the key even inside one minute (a 12-second
    # track holds a whole segment per second).
    assert views.progress_state(1000, 12000) != views.progress_state(2000, 12000)


# ---------------------------------------------------------------------------
# Anti-noop: the 60s tick edits ONLY when the rendered bar would change.
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Counts edits so a no-op refresh is provably free of HTTP calls."""

    def __init__(self):
        self.edits = 0

    async def edit(self, **kwargs):
        self.edits += 1


def _progress_controller(*, length=240000, position=0, is_stream=False):
    """A controller bound to a fake message over a one-track fake player."""
    import sonolink

    track = types.SimpleNamespace(
        title="Song", uri=None, author="A", is_stream=is_stream, length=length,
        identifier="id-1", extras=types.SimpleNamespace(requester=None),
    )
    player = types.SimpleNamespace(
        current=track, paused=False, volume=100, position=position,
        queue=types.SimpleNamespace(mode=sonolink.QueueMode.NORMAL, tracks=[]),
        channel=types.SimpleNamespace(name="G"), dj=None,
        autoplay=sonolink.AutoPlayMode.ENABLED,
    )
    view = music.MusicController(None, player)
    view.message = _FakeMessage()
    return view, player


def _progress_text(view):
    """The controller's rendered progress line (bar or LIVE badge)."""
    for child in view.walk_children():
        if not isinstance(child, discord.ui.TextDisplay):
            continue
        content = child.content
        if (
            views.PROGRESS_FILLED in content
            or views.PROGRESS_EMPTY in content
            or "🔴" in content
        ):
            return content
    return ""


async def test_controller_tick_skips_the_edit_when_nothing_moved():
    # 240 s track: a segment is 20 s wide. Advancing 5 s inside the same segment
    # AND the same displayed minute must post no edit at all.
    view, player = _progress_controller(position=5000)
    player.position = 10000
    assert await view.refresh_progress() is False
    assert view.message.edits == 0


async def test_controller_tick_edits_when_the_segment_advances():
    # Crossing into the next 20 s segment is a visible change -> one edit, and
    # the re-rendered bar actually shows the new fill.
    view, player = _progress_controller(position=5000)
    assert _bar_of(_progress_text(view)) == views.PROGRESS_EMPTY * 12
    player.position = 25000
    assert await view.refresh_progress() is True
    assert view.message.edits == 1
    assert _bar_of(_progress_text(view)) == (
        views.PROGRESS_FILLED * 1 + views.PROGRESS_EMPTY * 11
    )


async def test_controller_tick_edits_when_only_the_minute_rolls_over():
    # One-hour track: five minutes per segment, so a minute change is the only
    # thing that moves for most ticks. It still earns exactly one edit.
    view, player = _progress_controller(length=3600000, position=61000)
    player.position = 121000
    assert await view.refresh_progress() is True
    assert view.message.edits == 1


async def test_controller_tick_never_edits_a_live_stream():
    view, player = _progress_controller(length=0, is_stream=True, position=1000)
    assert "LIVE" in _progress_text(view)
    player.position = 600000
    assert await view.refresh_progress() is False
    assert view.message.edits == 0


async def test_controller_tick_stands_down_without_a_message_or_track():
    view, player = _progress_controller(position=5000)
    view.message = None
    player.position = 200000
    assert await view.refresh_progress() is False

    view, player = _progress_controller(position=5000)
    player.current = None
    assert await view.refresh_progress() is False
    assert view.message.edits == 0


def test_controller_renders_zero_while_player_current_lags_the_new_track():
    # The track_start race: the panel renders the event's track while
    # player.current is still the PREVIOUS one, whose position is far along.
    # Reading it blindly would draw a nearly-full bar under a song that just
    # began, so the render falls back to zero.
    import sonolink

    def _mk(title, ident):
        return types.SimpleNamespace(
            title=title, uri=None, author="A", is_stream=False, length=240000,
            identifier=ident, extras=types.SimpleNamespace(requester=None),
        )

    old = _mk("Old", "id-old")
    new = _mk("New", "id-new")
    player = types.SimpleNamespace(
        current=old, paused=False, volume=100, position=200000,
        queue=types.SimpleNamespace(mode=sonolink.QueueMode.NORMAL, tracks=[]),
        channel=types.SimpleNamespace(name="G"), dj=None,
        autoplay=sonolink.AutoPlayMode.ENABLED,
    )
    view = music.MusicController(None, player, track=new)
    # player.current (the old track) wins in _build, so this render is the old
    # track at its real position...
    assert _bar_of(_progress_text(view)) != views.PROGRESS_EMPTY * 12
    # ...and once current catches up to None mid-change, the new track renders
    # from zero rather than inheriting the previous track's position.
    player.current = None
    view._build()
    assert _bar_of(_progress_text(view)) == views.PROGRESS_EMPTY * 12


# ---------------------------------------------------------------------------
# The tick must not disturb the panel's identity state (dedup key, accent) nor
# state the track length twice, and its badge must be translatable.
# ---------------------------------------------------------------------------


def _panel_text(view):
    """Every TextDisplay of a controller, joined - the whole rendered panel."""
    return "\n".join(
        child.content
        for child in view.walk_children()
        if isinstance(child, discord.ui.TextDisplay)
    )


def _accent_of(view):
    """Accent colour of the controller's container."""
    for child in view.children:
        if isinstance(child, discord.ui.Container):
            return child.accent_colour
    return None


def _fake_track(ident, *, title="Song", length=240000):
    return types.SimpleNamespace(
        title=title, uri=None, author="A", is_stream=False, length=length,
        identifier=ident, extras=types.SimpleNamespace(requester=None),
    )


async def test_controller_tick_does_not_touch_the_dedup_rendered_id():
    """A clock refresh must leave decide_controller_action's inputs immobile.

    ``_rendered_id`` is what ``_send_controller`` compares an incoming
    track_start against. A tick landing in the window where ``player.current``
    has already advanced but the new track's track_start has not reached the cog
    would, if it re-keyed the panel, turn the genuine change that follows into a
    keep (stale panel) or a repost (channel churn) instead of a rerender.
    """
    view, player = _progress_controller(position=5000)
    assert view._rendered_id == "id-1"

    player.current = _fake_track("id-2", title="Next")
    player.position = 90000
    assert await view.refresh_progress() is True
    assert view.message.edits == 1
    # The bar advanced (the panel re-rendered) but the dedup key did not move.
    assert view._rendered_id == "id-1"
    # ...so the track_start that follows is still classified as a real change.
    assert (
        music.decide_controller_action(
            dedupe=True,
            has_live_controller=True,
            displayed_id=view._rendered_id,
            incoming_id="id-2",
            age_seconds=1.0,
        )
        == "rerender"
    )


async def test_controller_button_rerender_still_records_what_it_rendered():
    # Non-regression on the default: only the tick freezes the dedup key, an
    # event-driven re-render keeps recording the track it drew.
    view, player = _progress_controller(position=5000)
    player.current = _fake_track("id-2", title="Next")
    await view._rerender()
    assert view._rendered_id == "id-2"


async def test_controller_accent_colour_is_stable_across_ticks(monkeypatch):
    # The bar makes the panel re-render every minute; re-drawing the accent then
    # made the container blink. One draw per track, reused by later builds.
    colours = iter([0x111111, 0x222222, 0x333333])
    monkeypatch.setattr(views, "random_colour", lambda: next(colours))

    view, player = _progress_controller(position=5000)
    first = _accent_of(view)
    player.position = 25000
    assert await view.refresh_progress() is True
    assert _accent_of(view) == first
    # A plain rebuild is just as stable (pause/resume/volume all land here).
    view._build()
    assert _accent_of(view) == first


def test_controller_accent_colour_is_redrawn_for_a_new_track(monkeypatch):
    colours = iter([0x111111, 0x222222])
    monkeypatch.setattr(views, "random_colour", lambda: next(colours))

    view, player = _progress_controller(position=5000)
    first = _accent_of(view)
    player.current = _fake_track("id-2", title="Next")
    view._build()
    assert _accent_of(view) != first


def test_controller_states_the_track_length_exactly_once():
    # The bar carries elapsed/total, so the old "Duration" row printed the same
    # value a second line below it.
    view, _player = _progress_controller(length=240000, position=0)
    panel = _panel_text(view)
    assert "Duration:" not in panel
    assert panel.count("04:00") == 1


def test_progress_line_live_badge_is_translatable(monkeypatch):
    # The emoji stays OUT of the msgid (the onboarding-card pattern), so the
    # badge a translator sees is a plain short string.
    monkeypatch.setattr(
        views, "_", lambda text: "**EN DIRECT**" if text == "**LIVE**" else text
    )
    line = views.render_progress_line(1000, None)
    assert line == "🔴 **EN DIRECT**"


# ---------------------------------------------------------------------------
# Idle-tick fan-out: bounded-concurrent edits, isolated per player.
# ---------------------------------------------------------------------------


class _FakeController:
    """A controller whose refresh either reports an edit, or blows up."""

    def __init__(self, result=True):
        self.result = result
        self.calls = 0

    async def refresh_progress(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_refresh_progress_bars_isolates_a_failing_panel():
    # One guild's failed edit must not sink the batch (nor, in the loop, the
    # other guilds' idle disconnects).
    boom = _FakeController(RuntimeError("edit failed"))
    edited = _FakeController(True)
    unchanged = _FakeController(False)

    assert await music.refresh_progress_bars([boom, edited, unchanged]) == 1
    assert boom.calls == 1 and edited.calls == 1 and unchanged.calls == 1


async def test_refresh_progress_bars_is_free_when_nothing_is_playing():
    assert await music.refresh_progress_bars([]) == 0


async def test_refresh_progress_bars_runs_edits_concurrently_not_in_series():
    """The tick's cost must not grow linearly with the number of players."""
    import asyncio

    inflight = 0
    peak = 0

    class _SlowController:
        async def refresh_progress(self):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0)
            inflight -= 1
            return True

    controllers = [_SlowController() for _ in range(5)]
    assert await music.refresh_progress_bars(controllers, concurrency=10) == 5
    # Serial execution would never hold more than one edit in flight.
    assert peak == 5


async def test_refresh_progress_bars_caps_the_concurrency():
    # Bounded, so a large fleet never bursts its whole edit batch at once.
    import asyncio

    inflight = 0
    peak = 0

    class _SlowController:
        async def refresh_progress(self):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0)
            inflight -= 1
            return True

    controllers = [_SlowController() for _ in range(6)]
    assert await music.refresh_progress_bars(controllers, concurrency=2) == 6
    assert peak == 2

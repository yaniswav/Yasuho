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

from cogs.music import music


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


def test_format_duration_long_track_minutes_grow():
    # 3661000 ms -> 3661 s -> 61 min 1 s -> "61:01".
    track = types.SimpleNamespace(is_stream=False, length=3661000)
    assert music.format_duration(track) == "61:01"


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
# Regression guard: no music UI class may shadow View._refresh
# ---------------------------------------------------------------------------


def test_music_ui_classes_do_not_shadow_refresh():
    """Guard against the prod crash: a subclass ``_refresh`` shadows the base.

    discord.py invokes ``View._refresh(self, components)`` on MESSAGE_UPDATE. A
    View/Modal/LayoutView subclass that defines its own ``_refresh`` overrides it
    with an incompatible signature and crashes the gateway (the music
    controller's method was renamed to ``_rerender`` to fix this). Any UI class
    defined in the music module must not reintroduce that name.
    """
    ui_bases = tuple(
        base
        for name in ("View", "Modal", "LayoutView")
        for base in (getattr(discord.ui, name, None),)
        if base is not None
    )
    checked = []
    for obj in vars(music).values():
        if (
            inspect.isclass(obj)
            and obj.__module__ == music.__name__
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

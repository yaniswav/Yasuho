"""Tests for the pure position maths behind music-state restore.

The DB helpers are thin best-effort wrappers around asyncpg; the only real logic
is extrapolating a track's current position from a slightly stale snapshot, which
these tests pin down.
"""

from datetime import datetime, timedelta, timezone

from tools import music_state


def _ts(seconds=0):
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=seconds)


def test_playing_track_advances_by_elapsed_time():
    # Snapshot at 10s in; 5s of wall-clock later it should be ~15s in.
    pos = music_state.extrapolate_position(
        10_000, _ts(0), _ts(5), paused=False
    )
    assert pos == 15_000


def test_paused_track_does_not_advance():
    pos = music_state.extrapolate_position(
        10_000, _ts(0), _ts(5), paused=True
    )
    assert pos == 10_000


def test_position_is_clamped_to_length():
    # A stale snapshot must never seek past the end of the track.
    pos = music_state.extrapolate_position(
        119_000, _ts(0), _ts(30), paused=False, length_ms=120_000
    )
    assert pos == 120_000


def test_position_never_negative():
    pos = music_state.extrapolate_position(
        -5_000, _ts(0), _ts(0), paused=True
    )
    assert pos == 0


def test_none_position_defaults_to_zero():
    pos = music_state.extrapolate_position(
        None, _ts(0), _ts(3), paused=True
    )
    assert pos == 0


async def test_save_controller_message_id_updates_only_that_column(fake_pool):
    # Persisting the fresh controller id must be a targeted UPDATE keyed by guild,
    # so the next restart's stale-delete targets the actual last controller.
    await music_state.save_controller_message_id(fake_pool, 111, 222)
    assert len(fake_pool.calls) == 1
    kind, query, args = fake_pool.calls[0]
    assert kind == "execute"
    assert "UPDATE music_state" in query
    assert "controller_message_id" in query
    assert args == (111, 222)


def _save_state_kwargs(**overrides):
    """Minimal valid save_state kwargs, overridable per test."""
    base = dict(
        guild_id=1,
        voice_channel_id=2,
        home_channel_id=3,
        dj_id=4,
        volume=100,
        loop_mode=0,
        position_ms=0,
        paused=False,
        current_track="enc",
        queue=[],
        controller_message_id=None,
    )
    base.update(overrides)
    return base


async def test_save_state_persists_autoplay_flag(fake_pool):
    # The session autoplay mode rides along in the snapshot so a cold restart can
    # restore it. radio_genre and effect were appended after it, so autoplay is
    # now the third-to-last bound parameter.
    await music_state.save_state(fake_pool, **_save_state_kwargs(autoplay=False))
    assert len(fake_pool.calls) == 1
    kind, query, args = fake_pool.calls[0]
    assert kind == "execute"
    assert "autoplay" in query
    assert args[-3] is False


async def test_save_state_autoplay_defaults_true(fake_pool):
    # An older caller that omits the flag persists autoplay ON (the fallback).
    await music_state.save_state(fake_pool, **_save_state_kwargs())
    _, _query, args = fake_pool.calls[0]
    assert args[-3] is True


async def test_save_state_persists_radio_genre(fake_pool):
    # The active radio station key rides along so a cold restart resumes the
    # station (controller picker + refill); effect was appended after it, so it
    # is the second-to-last bound parameter.
    await music_state.save_state(
        fake_pool, **_save_state_kwargs(radio_genre="phonk")
    )
    _, query, args = fake_pool.calls[0]
    assert "radio_genre" in query
    assert args[-2] == "phonk"


async def test_save_state_radio_genre_defaults_none(fake_pool):
    # A normal (non-radio) session persists NULL, so a restart shows no station.
    await music_state.save_state(fake_pool, **_save_state_kwargs())
    _, _query, args = fake_pool.calls[0]
    assert args[-2] is None


async def test_save_state_persists_effect(fake_pool):
    # The active audio-effect preset key rides along so a cold restart re-applies
    # it; it is the last bound parameter.
    await music_state.save_state(
        fake_pool, **_save_state_kwargs(effect="nightcore")
    )
    _, query, args = fake_pool.calls[0]
    assert "effect" in query
    assert args[-1] == "nightcore"


async def test_save_state_effect_defaults_none(fake_pool):
    # A session with no effect persists NULL, so a restart plays unfiltered.
    await music_state.save_state(fake_pool, **_save_state_kwargs())
    _, _query, args = fake_pool.calls[0]
    assert args[-1] is None

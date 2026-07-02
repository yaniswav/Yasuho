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

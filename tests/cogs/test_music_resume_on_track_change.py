"""Unit tests: a USER action that starts another track resumes a paused player.

Owner decision (lot S2-1): skip (the controller's Skip button, ``/skip``, a
resolved vote, the dashboard's skip executor), Back (the controller's Back
button, ``/music previous``) and jump-to-track (the queue manager's "Play now")
must all leave the player PLAYING, even when it was paused - because sonolink's
``skip()`` / ``previous()`` both resend the player's OLD ``paused`` flag to
Lavalink (``play()``'s ``paused = paused if paused is not None else
self._player._paused``), so without this fix the new track lands paused on
Lavalink while the controller still reads "Playing" (nothing follows a click
that looked like it should start something).

This pins the single helper, ``cogs.music.music.resume_after_track_change``, and
every path that must call it (or pass ``paused=False`` to a direct ``play()``):

* ``Music._execute_skip`` - the shared engine behind ``/skip``, a resolved vote
  (``voteskip.SkipVote._resolve``) and the dashboard's ``_exec_music_skip``.
* ``Music._play_previous`` - the shared engine behind ``/music previous`` and
  the controller's Back button.
* ``MusicController._skip`` - the controller's Skip button, which calls
  ``player.skip()`` directly (NOT through ``_execute_skip``).
* ``QueueView._jump`` - the queue manager's "Play now", which calls
  ``player.play()`` directly (pinned in ``test_music_queue_view.py``, where its
  fakes already live).

A non-paused player must see NO spurious pause/resume call on any of these
paths (unchanged behaviour).
"""

from __future__ import annotations

import types

import pytest
import sonolink

from cogs.music import music, views, voteskip

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTrack:
    def __init__(self, identifier="next-track", title="Song", author="Someone"):
        self.identifier = identifier
        self.title = title
        self.author = author
        self.encoded = "enc-" + identifier


class FakeQueue:
    """Enough of sonolink's Queue surface for can_skip / can_go_previous."""

    def __init__(self, *, tracks=None, history=None, mode=None, autoplay_tracks=None):
        self.tracks = tracks if tracks is not None else [FakeTrack("queued")]
        self.history = history if history is not None else [FakeTrack("previous")]
        self.mode = mode
        self.autoplay_tracks = autoplay_tracks if autoplay_tracks is not None else []

    def pop_at(self, index):
        return self.tracks.pop(index)


class FakePlayer:
    """Records every sonolink call; mirrors how real skip()/previous()/play()
    resend the CURRENT paused flag unless told otherwise - so paused stays
    whatever it was unless our code (or an explicit paused= kwarg) changes it.
    """

    def __init__(self, *, paused, next_track=None, queue=None):
        self.paused = paused
        self._next_track = (
            next_track if next_track is not None else FakeTrack()
        )
        self.queue = queue if queue is not None else FakeQueue()
        self.current = FakeTrack("current")
        self.guild = types.SimpleNamespace(id=1)
        self.autoplay = sonolink.AutoPlayMode.DISABLED
        self.calls = []

    async def skip(self):
        self.calls.append(("skip",))
        return self._next_track

    async def previous(self):
        self.calls.append(("previous",))
        return self._next_track

    async def play(self, track, **kwargs):
        self.calls.append(("play", track, kwargs))
        if "paused" in kwargs and kwargs["paused"] is not None:
            self.paused = kwargs["paused"]
        return track

    async def pause(self):
        self.calls.append(("pause",))
        self.paused = True

    async def resume(self):
        self.calls.append(("resume",))
        self.paused = False


class FakeCog:
    """Minimal stand-in for Music - only what _play_previous touches."""

    def __init__(self):
        self.snapshots = []

    async def _snapshot(self, player):
        self.snapshots.append(player)


class FakeSkipCog:
    """Minimal stand-in for Music - only what the controller's _skip touches."""

    async def _request_skip(self, player, actor, channel):
        return voteskip.SKIP_INSTANT


# ---------------------------------------------------------------------------
# resume_after_track_change - the helper itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_after_track_change_resumes_a_paused_player():
    player = FakePlayer(paused=True)
    await music.resume_after_track_change(player)
    assert player.paused is False
    assert player.calls == [("resume",)]


@pytest.mark.asyncio
async def test_resume_after_track_change_is_a_no_op_when_already_playing():
    player = FakePlayer(paused=False)
    await music.resume_after_track_change(player)
    assert player.paused is False
    assert player.calls == []


# ---------------------------------------------------------------------------
# Music._execute_skip - /skip, a resolved vote, the dashboard skip executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_skip_resumes_a_paused_player_on_a_successful_skip():
    player = FakePlayer(paused=True)
    result, track = await music.Music._execute_skip(None, player)
    assert result == music.voteskip.SKIP_RESULT_ADVANCED
    assert track is player._next_track
    assert player.paused is False
    assert ("resume",) in player.calls


@pytest.mark.asyncio
async def test_execute_skip_leaves_a_playing_player_unchanged():
    player = FakePlayer(paused=False)
    result, track = await music.Music._execute_skip(None, player)
    assert result == music.voteskip.SKIP_RESULT_ADVANCED
    assert track is player._next_track
    assert player.paused is False
    assert ("resume",) not in player.calls
    assert ("pause",) not in player.calls


@pytest.mark.asyncio
async def test_execute_skip_with_nowhere_to_land_never_touches_pause_state():
    # can_skip refuses up front (empty queue, no loop, no autoplay): playback
    # must be left EXACTLY as it was, paused or not.
    player = FakePlayer(
        paused=True,
        queue=FakeQueue(tracks=[], history=[], autoplay_tracks=[]),
    )
    result, track = await music.Music._execute_skip(None, player)
    assert result == music.voteskip.SKIP_RESULT_NONE
    assert track is None
    assert player.calls == []
    assert player.paused is True


# ---------------------------------------------------------------------------
# Music._play_previous - /music previous and the controller's Back button
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_play_previous_resumes_a_paused_player_on_a_successful_step_back():
    player = FakePlayer(paused=True)
    cog = FakeCog()
    track = await music.Music._play_previous(cog, player)
    assert track is player._next_track
    assert player.paused is False
    assert ("resume",) in player.calls
    assert cog.snapshots == [player]


@pytest.mark.asyncio
async def test_play_previous_leaves_a_playing_player_unchanged():
    player = FakePlayer(paused=False)
    cog = FakeCog()
    track = await music.Music._play_previous(cog, player)
    assert track is player._next_track
    assert player.paused is False
    assert ("resume",) not in player.calls
    assert ("pause",) not in player.calls


@pytest.mark.asyncio
async def test_play_previous_with_no_history_never_touches_pause_state():
    player = FakePlayer(paused=True, queue=FakeQueue(history=[]))
    cog = FakeCog()
    track = await music.Music._play_previous(cog, player)
    assert track is None
    assert player.calls == []
    assert player.paused is True
    assert cog.snapshots == []


# ---------------------------------------------------------------------------
# MusicController._skip - the controller's Skip button (calls player.skip()
# directly, NOT through Music._execute_skip).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_controller_skip_resumes_a_paused_player(make_interaction):
    player = FakePlayer(paused=True)
    self = types.SimpleNamespace(cog=FakeSkipCog(), player=player)
    interaction = make_interaction()
    interaction.channel = object()

    await views.MusicController._skip(self, interaction)

    assert ("skip",) in player.calls
    assert player.paused is False
    assert ("resume",) in player.calls
    assert "Skipped" in interaction.sent[0][0][0]


@pytest.mark.asyncio
async def test_controller_skip_on_a_playing_player_does_not_touch_pause_state(
    make_interaction,
):
    player = FakePlayer(paused=False)
    self = types.SimpleNamespace(cog=FakeSkipCog(), player=player)
    interaction = make_interaction()
    interaction.channel = object()

    await views.MusicController._skip(self, interaction)

    assert ("skip",) in player.calls
    assert player.paused is False
    assert ("resume",) not in player.calls
    assert ("pause",) not in player.calls


# ---------------------------------------------------------------------------
# Negative control: with resume_after_track_change made a no-op, the two
# shared-engine tests above must fail - proving they are not vacuously true.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_control_execute_skip_without_the_helper_stays_paused(
    monkeypatch,
):
    async def _noop(player):
        return None

    monkeypatch.setattr(music, "resume_after_track_change", _noop)
    player = FakePlayer(paused=True)
    result, track = await music.Music._execute_skip(None, player)
    assert result == music.voteskip.SKIP_RESULT_ADVANCED
    # Without the fix, the player is STILL reported paused - the bug this lot
    # closes.
    assert player.paused is True


@pytest.mark.asyncio
async def test_negative_control_play_previous_without_the_helper_stays_paused(
    monkeypatch,
):
    async def _noop(player):
        return None

    monkeypatch.setattr(music, "resume_after_track_change", _noop)
    player = FakePlayer(paused=True)
    cog = FakeCog()
    await music.Music._play_previous(cog, player)
    assert player.paused is True

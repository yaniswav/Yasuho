"""Unit tests for the pure building blocks of the Rythm-style autoplay lot.

Everything here is side-effect free - no discord, sonolink node, database or
voice connection is touched. It pins down the two pure predicates the cog and the
controller lean on:

* ``cogs.music.music.resolve_session_autoplay`` - the precedence that seeds a NEW
  session's autoplay mode from the starter's saved preference, defaulting ON.
* ``cogs.music.music.is_autoplay_track`` - the notice-condition predicate that
  decides whether the now-playing controller shows its "autoplaying
  recommendations" notice (only for autoplay-sourced tracks).

The live-mode helpers (``_autoplay_on`` / ``_set_autoplay``) touch
``sonolink.AutoPlayMode`` and drive a real Player, so they are exercised end to
end by the running bot rather than unit tests; the precedence and the notice
predicate are the parts worth pinning here. ``sonolink`` is stubbed by the
repo-root conftest on the 3.10 dev box and imported for real on 3.13, mirroring
test_music_vibes.
"""

import types

from cogs.music import music

# ---------------------------------------------------------------------------
# resolve_session_autoplay (precedence)
# ---------------------------------------------------------------------------


def test_resolve_session_autoplay_unset_defaults_on():
    # A member with no saved preference gets autoplay ON (the default experience).
    assert music.resolve_session_autoplay(None) is True


def test_resolve_session_autoplay_honours_true():
    assert music.resolve_session_autoplay(True) is True


def test_resolve_session_autoplay_honours_false():
    # An explicit opt-out seeds the session OFF.
    assert music.resolve_session_autoplay(False) is False


def test_resolve_session_autoplay_coerces_truthy_values():
    # Whatever shape the JSONB blob hands back, the result is a plain bool.
    assert music.resolve_session_autoplay(1) is True
    assert music.resolve_session_autoplay(0) is False
    assert music.resolve_session_autoplay("") is False


# ---------------------------------------------------------------------------
# is_autoplay_track (notice condition)
# ---------------------------------------------------------------------------


def _track(autoplay):
    return types.SimpleNamespace(autoplay=autoplay)


def test_is_autoplay_track_true_for_autoplay_sourced():
    assert music.is_autoplay_track(_track(True)) is True


def test_is_autoplay_track_false_for_user_queued():
    # A user-added track (autoplay flag False) never claims to be a recommendation.
    assert music.is_autoplay_track(_track(False)) is False


def test_is_autoplay_track_missing_attr_is_false():
    # A stand-in without the flag (or None) reads as not-autoplay, never crashes.
    assert music.is_autoplay_track(types.SimpleNamespace()) is False
    assert music.is_autoplay_track(None) is False


# ---------------------------------------------------------------------------
# decide_anti_mix_skip (bounded auto-skip of autoplay-sourced mixes)
# ---------------------------------------------------------------------------


def test_anti_mix_skip_skips_an_autoplay_mix_and_counts():
    # An autoplay-sourced mix is skipped and the consecutive streak increments.
    assert music.decide_anti_mix_skip(True, True, 0) == (True, 1)
    assert music.decide_anti_mix_skip(True, True, 2) == (True, 3)


def test_anti_mix_skip_gives_up_at_the_cap():
    # At the cap we stop skipping: the mix is allowed to play and the streak
    # resets, so a run of nothing-but-mixes can never loop forever skipping.
    assert music.decide_anti_mix_skip(True, True, music.ANTI_MIX_SKIP_CAP) == (
        False,
        0,
    )


def test_anti_mix_skip_resets_streak_on_a_normal_track():
    # Autoplay track that is not a mix -> play, reset.
    assert music.decide_anti_mix_skip(True, False, 2) == (False, 0)
    # A mix that is NOT autoplay-sourced (a user pick) is never auto-skipped.
    assert music.decide_anti_mix_skip(False, True, 2) == (False, 0)
    # A plain user track resets too.
    assert music.decide_anti_mix_skip(False, False, 1) == (False, 0)


def test_anti_mix_skip_honours_a_custom_cap():
    assert music.decide_anti_mix_skip(True, True, 0, cap=1) == (True, 1)
    assert music.decide_anti_mix_skip(True, True, 1, cap=1) == (False, 0)


# ---------------------------------------------------------------------------
# can_skip (the "never kill playback on an empty skip" pre-check)
# ---------------------------------------------------------------------------


def _skip_player(
    tracks=(), autoplay_tracks=(), mode=None, autoplay="disabled"
):
    """Minimal player/queue shape for can_skip; mirrors the sonolink surface."""
    import sonolink

    queue = types.SimpleNamespace(
        tracks=list(tracks),
        autoplay_tracks=list(autoplay_tracks),
        mode=mode if mode is not None else sonolink.QueueMode.NORMAL,
    )
    ap = (
        sonolink.AutoPlayMode.DISABLED
        if autoplay == "disabled"
        else sonolink.AutoPlayMode.ENABLED
    )
    return types.SimpleNamespace(queue=queue, autoplay=ap)


def test_can_skip_false_when_nothing_can_follow():
    # Empty lanes, no loop, autoplay off: a skip would stop playback -> refuse.
    assert music.can_skip(_skip_player()) is False


def test_can_skip_true_with_user_lane_tracks():
    assert music.can_skip(_skip_player(tracks=["t"])) is True


def test_can_skip_true_with_prestaged_autoplay_lane():
    assert music.can_skip(_skip_player(autoplay_tracks=["r"])) is True


def test_can_skip_true_when_autoplay_armed():
    # Autoplay fetches a recommendation on skip, so there is somewhere to land.
    assert music.can_skip(_skip_player(autoplay="enabled")) is True


def test_can_skip_true_under_loop_modes():
    import sonolink

    assert music.can_skip(_skip_player(mode=sonolink.QueueMode.LOOP)) is True
    assert music.can_skip(_skip_player(mode=sonolink.QueueMode.LOOP_ALL)) is True

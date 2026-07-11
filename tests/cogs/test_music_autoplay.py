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

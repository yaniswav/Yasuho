"""Unit tests for the pure building blocks of the autoplay feature.

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

import pytest

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


# ---------------------------------------------------------------------------
# can_go_previous (the "is there a track to step back to" Back pre-check)
# ---------------------------------------------------------------------------


def _previous_player(history=()):
    """Minimal player/queue shape for can_go_previous.

    ``history`` mirrors sonolink's History collection: a non-empty sequence means
    there is a strictly-previous track to replay. ``bool()`` over a plain list
    matches ``History.__bool__`` (``len > 0``), so this fake is faithful to the
    real surface.
    """
    return types.SimpleNamespace(queue=types.SimpleNamespace(history=list(history)))


def test_can_go_previous_false_on_empty_history():
    # No track played before the current one: nothing to go back to -> refuse.
    assert music.can_go_previous(_previous_player()) is False


def test_can_go_previous_true_with_a_prior_track():
    assert music.can_go_previous(_previous_player(history=["prev"])) is True


def test_can_go_previous_true_with_several_prior_tracks():
    assert music.can_go_previous(_previous_player(history=["a", "b", "c"])) is True


def test_can_go_previous_none_safe_over_missing_history():
    # A queue with no history attribute coerces to False rather than raising, so
    # the predicate is total over the shapes the fakes mirror.
    player = types.SimpleNamespace(queue=types.SimpleNamespace())
    assert music.can_go_previous(player) is False


def test_can_go_previous_mirrors_real_history_semantics():
    # Pin against the REAL sonolink History: the CURRENT track is never in
    # history (it enters only when the NEXT track is popped), so on the first
    # track - current set, history empty - there is no previous to step back to.
    from sonolink.gateway.queue import Queue

    queue = Queue()
    queue._current_track = types.SimpleNamespace(encoded="A", title="A")
    player = types.SimpleNamespace(queue=queue)
    assert music.can_go_previous(player) is False

    # Once that track advances, sonolink pushes it to history; now the PREVIOUS
    # track is present and Back has somewhere to land.
    queue._history._push(types.SimpleNamespace(encoded="A", title="A"))
    assert music.can_go_previous(player) is True


# ---------------------------------------------------------------------------
# _play_previous (the shared /previous + Back engine seam)
# ---------------------------------------------------------------------------
#
# The reinsertion of the current track to the queue front and the actual replay
# are sonolink's Player.previous() (queue.previous() + a direct play()), so they
# are exercised live on the running bot, not here. What is worth pinning is the
# seam's own decision logic: it must refuse (return None) WITHOUT touching
# playback when there is nothing playable to go back to, and only otherwise
# replay + snapshot.


class _FakePreviousPlayer:
    """Fake player exposing just the surface ``_play_previous`` touches."""

    def __init__(self, history=()):
        self.queue = types.SimpleNamespace(history=list(history))
        self.previous_calls = 0
        self.now_playing = types.SimpleNamespace(title="P", author="art", encoded="P")

    async def previous(self):
        self.previous_calls += 1
        return self.now_playing


class _SnapshotCog:
    """Cog stand-in that records _snapshot calls so the seam can be driven bare."""

    def __init__(self):
        self.snapshots = 0

    async def _snapshot(self, player):
        self.snapshots += 1


async def test_play_previous_replays_and_snapshots_on_success():
    playable = types.SimpleNamespace(title="P", author="art", encoded="enc-P")
    player = _FakePreviousPlayer(history=[playable])
    cog = _SnapshotCog()
    result = await music.Music._play_previous(cog, player)
    assert result is player.now_playing
    assert player.previous_calls == 1
    assert cog.snapshots == 1


async def test_play_previous_refuses_when_encoded_missing():
    # The most-recent history entry can no longer be dispatched: refuse cleanly
    # and leave playback untouched (previous() never runs, no snapshot written).
    dead = types.SimpleNamespace(title="X", author="a", encoded=None)
    player = _FakePreviousPlayer(history=[dead])
    cog = _SnapshotCog()
    result = await music.Music._play_previous(cog, player)
    assert result is None
    assert player.previous_calls == 0
    assert cog.snapshots == 0


async def test_play_previous_refuses_on_empty_history():
    player = _FakePreviousPlayer(history=[])
    cog = _SnapshotCog()
    result = await music.Music._play_previous(cog, player)
    assert result is None
    assert player.previous_calls == 0
    assert cog.snapshots == 0


# ---------------------------------------------------------------------------
# queued_track_count (the /clearqueue counter)
# ---------------------------------------------------------------------------


def _count_queue(tracks=(), autoplay_tracks=()):
    """Minimal queue shape for queued_track_count; mirrors the sonolink lanes."""
    return types.SimpleNamespace(
        tracks=list(tracks),
        autoplay_tracks=list(autoplay_tracks),
    )


def test_queued_track_count_sums_both_lanes():
    # The user lane and the hidden autoplay lane are both counted.
    assert music.queued_track_count(
        _count_queue(tracks=["a", "b"], autoplay_tracks=["r"])
    ) == 3


def test_queued_track_count_zero_on_empty_lanes():
    assert music.queued_track_count(_count_queue()) == 0


def test_queued_track_count_none_safe_over_missing_lanes():
    # Missing/None lanes coerce to empty rather than raising, so the counter is
    # total over the queue shapes the fakes mirror.
    assert music.queued_track_count(types.SimpleNamespace()) == 0
    assert music.queued_track_count(
        types.SimpleNamespace(tracks=None, autoplay_tracks=None)
    ) == 0


# ---------------------------------------------------------------------------
# seed_needs_youtube_resolution (which autoplay seeds need re-resolving)
# ---------------------------------------------------------------------------

# The stock YouTube Radio provider template sonolink formats the seed id into.
_YT_PROVIDER = "https://www.youtube.com/watch?v={identifier}&list=RD{identifier}"


def _seed(identifier="dQw4w9WgXcQ", source_name="youtube", title="t", author="a"):
    """A minimal seed-track stand-in exposing the fields the predicate reads."""
    return types.SimpleNamespace(
        identifier=identifier, source_name=source_name, title=title, author=author
    )


def test_seed_needs_resolution_for_spotify_seed():
    # A LavaSrc/Spotify seed under the YouTube Radio provider must be re-resolved:
    # its 22-char id is what YouTube rejects with AllClientsFailedException.
    spotify = _seed(identifier="1toNKayLMeCcVlsLGXJl7n", source_name="spotify")
    assert music.seed_needs_youtube_resolution(spotify, _YT_PROVIDER) is True


def test_seed_needs_no_resolution_for_youtube_seed():
    # The live-verified working path: a YouTube seed is left to sonolink verbatim.
    assert music.seed_needs_youtube_resolution(_seed(), _YT_PROVIDER) is False


def test_seed_source_match_is_case_insensitive():
    yt = _seed(source_name="YouTube")
    assert music.seed_needs_youtube_resolution(yt, _YT_PROVIDER) is False


def test_seed_needs_no_resolution_without_a_seed():
    # No reference, or a reference with no identifier: leave it to sonolink, which
    # raises AutoPlaySeedMissing. We must not swallow that contract.
    assert music.seed_needs_youtube_resolution(None, _YT_PROVIDER) is False
    assert music.seed_needs_youtube_resolution(
        _seed(identifier=""), _YT_PROVIDER
    ) is False
    assert music.seed_needs_youtube_resolution(
        _seed(identifier=None), _YT_PROVIDER
    ) is False


def test_seed_needs_no_resolution_for_non_youtube_provider():
    # If the discovery provider is Spotify/Deezer recommendations (which accept
    # their own ids), a Spotify seed is already correct: no re-resolution.
    spotify = _seed(identifier="1toNKayLMeCcVlsLGXJl7n", source_name="spotify")
    assert music.seed_needs_youtube_resolution(spotify, "sprec:{identifier}") is False
    assert music.seed_needs_youtube_resolution(spotify, "dzrec:{identifier}") is False


def test_seed_missing_source_name_resolves_under_youtube_provider():
    # A seed with an id but an unknown/None source is not YouTube, so under the
    # YouTube provider we re-resolve rather than risk a doomed query.
    unknown = _seed(source_name=None)
    assert music.seed_needs_youtube_resolution(unknown, _YT_PROVIDER) is True


# ---------------------------------------------------------------------------
# youtube_seed_query (the "{author} {title}" ytsearch text)
# ---------------------------------------------------------------------------


def test_youtube_seed_query_joins_author_and_title():
    assert music.youtube_seed_query(_seed(author="Daft Punk", title="One More Time")) == (
        "Daft Punk One More Time"
    )


def test_youtube_seed_query_falls_back_to_a_single_field():
    assert music.youtube_seed_query(_seed(author="", title="Solo")) == "Solo"
    assert music.youtube_seed_query(_seed(author="Artist", title="")) == "Artist"


def test_youtube_seed_query_strips_and_is_none_safe():
    assert music.youtube_seed_query(_seed(author="  a  ", title="  b  ")) == "a b"
    # Neither field present -> empty, so the caller skips autoplay this cycle.
    assert music.youtube_seed_query(types.SimpleNamespace()) == ""
    assert music.youtube_seed_query(_seed(author=None, title=None)) == ""


# ---------------------------------------------------------------------------
# Shape guard: pin the private sonolink internals our handler subclass leans on.
# Fails loudly if a sonolink upgrade renames/reshapes them, rather than silently
# reverting to the broken non-YouTube autoplay. Skipped under the stub sonolink.
# ---------------------------------------------------------------------------


def test_autoplay_handler_pins_sonolink_internals():
    autoplay_mod = pytest.importorskip(
        "sonolink.gateway.player.handlers._autoplay"
    )
    handler_cls = autoplay_mod.AutoPlayHandler
    # The overridable method and the discovery method we delegate to must exist.
    assert callable(getattr(handler_cls, "_fill_auto_queue", None))
    assert callable(getattr(handler_cls, "_apply_discovery", None))
    # The instance state we read (seed set) and reference (settings) must exist.
    assert "_seeds" in handler_cls.__slots__
    assert "_settings" in handler_cls.__slots__
    # Our subclass must actually derive from it and be what the Player swaps in.
    assert music._YouTubeSeedAutoPlayHandler is not None
    assert issubclass(music._YouTubeSeedAutoPlayHandler, handler_cls)
    # AutoPlaySettings must still carry the id-templated provider we format and the
    # int seed cap we bound against.
    from sonolink.models.settings import AutoPlaySettings

    settings = AutoPlaySettings.default()
    assert "{identifier}" in str(settings.provider)
    assert "youtube.com" in str(settings.provider).lower()
    assert isinstance(settings.max_seeds, int)

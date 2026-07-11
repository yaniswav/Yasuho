"""Unit tests for the pure helpers behind shared server playlists.

``cogs/music/playlists_shared.py`` folds a ``/serverplaylist`` group into the
Music cog, but its decision logic is factored into small pure functions with no
database, node or discord I/O:

* ``clean_name`` / ``normalize_name`` - whitespace cleanup and the casefolded
  uniqueness key that the ``(guild_id, name_norm)`` primary key enforces on.
* ``name_error`` / ``track_cap_error`` / ``guild_cap_reached`` - the empty /
  too-long / too-many / guild-full cap decisions, enforced in code.
* ``snapshot_tracks`` - the save snapshot shape (current track first, then the
  user-lane queue), assembled from a fake player.
* ``account_decoded`` - the "queued N, skipped M" accounting over a decode
  result that may contain ``None`` entries or be short.
* ``can_manage`` - the creator-or-moderator permission decision.
* ``_like_prefix`` - LIKE-wildcard escaping for the autocomplete prefix.

Fakes are plain ``types.SimpleNamespace``; ``sonolink`` is stubbed by the
repo-root conftest on the dev box and imported for real on CI, so importing the
module is safe either way.
"""

import types

from cogs.music import playlists_shared as ps

# ---------------------------------------------------------------------------
# name cleanup / normalisation / uniqueness key
# ---------------------------------------------------------------------------


def test_clean_name_collapses_and_trims_whitespace():
    assert ps.clean_name("  My   Road   Trip  ") == "My Road Trip"


def test_clean_name_handles_none():
    assert ps.clean_name(None) == ""


def test_normalize_name_casefolds():
    assert ps.normalize_name("My Jams") == "my jams"


def test_normalize_name_uniqueness_is_case_and_space_insensitive():
    # Two names that differ only in case / whitespace share one uniqueness key,
    # so the guild's primary key treats them as the same playlist.
    assert ps.normalize_name("  Chill   Vibes ") == ps.normalize_name("chill vibes")


def test_normalize_name_distinct_names_differ():
    assert ps.normalize_name("Party") != ps.normalize_name("Study")


# ---------------------------------------------------------------------------
# name validity
# ---------------------------------------------------------------------------


def test_name_error_empty():
    assert ps.name_error("") == "empty"


def test_name_error_too_long():
    assert ps.name_error("x" * (ps.MAX_NAME_LEN + 1)) == "too_long"


def test_name_error_at_limit_is_ok():
    assert ps.name_error("x" * ps.MAX_NAME_LEN) is None


def test_name_error_ordinary_name_ok():
    assert ps.name_error("Road Trip") is None


# ---------------------------------------------------------------------------
# cap decisions
# ---------------------------------------------------------------------------


def test_track_cap_error_empty():
    assert ps.track_cap_error(0) == "empty"


def test_track_cap_error_negative_treated_as_empty():
    assert ps.track_cap_error(-3) == "empty"


def test_track_cap_error_at_cap_is_ok():
    assert ps.track_cap_error(ps.MAX_PLAYLIST_TRACKS) is None


def test_track_cap_error_over_cap():
    assert ps.track_cap_error(ps.MAX_PLAYLIST_TRACKS + 1) == "too_many"


def test_track_cap_error_one_is_ok():
    assert ps.track_cap_error(1) is None


def test_guild_cap_reached_under_cap_false():
    assert ps.guild_cap_reached(ps.MAX_GUILD_PLAYLISTS - 1) is False


def test_guild_cap_reached_at_cap_true():
    assert ps.guild_cap_reached(ps.MAX_GUILD_PLAYLISTS) is True


# ---------------------------------------------------------------------------
# save snapshot shape
# ---------------------------------------------------------------------------


def _track(encoded, length=1000):
    return types.SimpleNamespace(encoded=encoded, length=length)


def _player(current, queued):
    return types.SimpleNamespace(
        current=current,
        queue=types.SimpleNamespace(tracks=list(queued)),
    )


def test_snapshot_current_first_then_user_lane():
    player = _player(_track("A", 1000), [_track("B", 2000), _track("C", 3000)])
    blobs, total_ms = ps.snapshot_tracks(player)
    assert blobs == ["A", "B", "C"]
    assert total_ms == 6000


def test_snapshot_skips_blobless_tracks():
    # A track with no encoded blob cannot be restored, so it is dropped from the
    # snapshot (and its length is not counted).
    player = _player(_track("A", 1000), [_track("", 9999), _track("C", 3000)])
    blobs, total_ms = ps.snapshot_tracks(player)
    assert blobs == ["A", "C"]
    assert total_ms == 4000


def test_snapshot_no_current_uses_only_queue():
    player = _player(None, [_track("B", 2000)])
    blobs, total_ms = ps.snapshot_tracks(player)
    assert blobs == ["B"]
    assert total_ms == 2000


def test_snapshot_empty_player():
    player = _player(None, [])
    assert ps.snapshot_tracks(player) == ([], 0)


# ---------------------------------------------------------------------------
# decode-failure accounting
# ---------------------------------------------------------------------------


def test_account_decoded_all_usable():
    usable, skipped = ps.account_decoded(["t1", "t2", "t3"], 3)
    assert usable == ["t1", "t2", "t3"]
    assert skipped == 0


def test_account_decoded_none_entries_counted_as_skipped():
    # Lavalink returns None for a stale blob it cannot decode.
    usable, skipped = ps.account_decoded(["t1", None, "t3"], 3)
    assert usable == ["t1", "t3"]
    assert skipped == 1


def test_account_decoded_short_result_counts_missing_as_skipped():
    usable, skipped = ps.account_decoded(["t1"], 3)
    assert usable == ["t1"]
    assert skipped == 2


def test_account_decoded_empty_result():
    usable, skipped = ps.account_decoded([], 5)
    assert usable == []
    assert skipped == 5


def test_account_decoded_none_result():
    usable, skipped = ps.account_decoded(None, 2)
    assert usable == []
    assert skipped == 2


# ---------------------------------------------------------------------------
# creator-or-moderator permission decision
# ---------------------------------------------------------------------------


def test_can_manage_creator_allowed():
    actor = types.SimpleNamespace(id=42)
    assert ps.can_manage(actor, creator_id=42, has_manage_guild=False) is True


def test_can_manage_moderator_allowed():
    actor = types.SimpleNamespace(id=7)
    assert ps.can_manage(actor, creator_id=42, has_manage_guild=True) is True


def test_can_manage_bystander_denied():
    actor = types.SimpleNamespace(id=7)
    assert ps.can_manage(actor, creator_id=42, has_manage_guild=False) is False


# ---------------------------------------------------------------------------
# LIKE-prefix escaping (autocomplete)
# ---------------------------------------------------------------------------


def test_like_prefix_plain_term():
    assert ps._like_prefix("road") == "road%"


def test_like_prefix_escapes_wildcards():
    # % and _ must match literally, not as SQL wildcards.
    assert ps._like_prefix("50%_off") == "50\\%\\_off%"


def test_like_prefix_escapes_backslash_first():
    assert ps._like_prefix("a\\b") == "a\\\\b%"

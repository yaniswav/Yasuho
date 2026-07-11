"""Unit tests for the pure building blocks of the "choose your vibe" music UX.

Everything here is side-effect free - no discord, sonolink node, database or
voice connection is touched:

* ``cogs.music.vibes.GENRE_CATALOG`` - the fixed genre catalog (shape, ASCII-safe
  labels/queries/descriptions, emoji handling, key uniqueness).
* ``cogs.music.vibes.PendingVoiceWatches`` - the bounded, TTL-scoped, fire-once
  join-card watch map (the bookkeeping the voice-state listener relies on).
* ``cogs.music.music.select_playable`` - the multi-track search-result normaliser
  that skips streams and de-duplicates against what is already queued.
* ``cogs.music.music.joinable_voice_channels`` - the permission-aware channel
  picker the join card lists.

``sonolink`` is stubbed by the repo-root conftest on the 3.10 dev box and imported
for real on 3.12+ CI; the Playlist branch swaps ``sonolink.models.Playlist`` for a
small local class via monkeypatch (the real Playlist exposes ``tracks`` as a
read-only property, so it cannot be hand-built), mirroring test_music_helpers.
"""

import types

from cogs.music import music, vibes

# Punctuation the project forbids everywhere (em dash, en dash, fancy ellipsis).
# Written as escapes so this source file itself stays pure ASCII.
_FORBIDDEN = ("\u2014", "\u2013", "\u2026")


# ---------------------------------------------------------------------------
# GENRE_CATALOG
# ---------------------------------------------------------------------------


def test_catalog_has_eight_genres():
    assert len(vibes.GENRE_CATALOG) == 8


def test_catalog_keys_are_unique():
    keys = [g.key for g in vibes.GENRE_CATALOG]
    assert len(keys) == len(set(keys))


def test_genres_by_key_matches_catalog():
    assert set(vibes.GENRES_BY_KEY) == {g.key for g in vibes.GENRE_CATALOG}
    for key, genre in vibes.GENRES_BY_KEY.items():
        assert genre.key == key


def test_catalog_labels_queries_descriptions_non_empty():
    for g in vibes.GENRE_CATALOG:
        assert g.label.strip(), g
        assert g.query.strip(), g
        assert g.description.strip(), g


def test_catalog_labels_queries_descriptions_are_ascii():
    # Proper-name labels, curated queries and descriptive text must stay plain
    # ASCII (no fancy typography); only the emoji field carries non-ASCII.
    for g in vibes.GENRE_CATALOG:
        assert g.label.isascii(), g.label
        assert g.query.isascii(), g.query
        assert g.description.isascii(), g.description


def test_catalog_has_no_forbidden_punctuation():
    for g in vibes.GENRE_CATALOG:
        for field in (g.label, g.query, g.description):
            for bad in _FORBIDDEN:
                assert bad not in field, (g.key, field)


def test_catalog_emojis_are_non_empty_strings():
    # Emojis are intentionally non-ASCII; assert each is a truthy str so a
    # SelectOption can always render one and none is accidentally blank.
    for g in vibes.GENRE_CATALOG:
        assert isinstance(g.emoji, str)
        assert g.emoji != ""


def test_tracks_per_genre_is_positive_int():
    assert isinstance(vibes.TRACKS_PER_GENRE, int)
    assert vibes.TRACKS_PER_GENRE > 0


# ---------------------------------------------------------------------------
# select_playable
# ---------------------------------------------------------------------------


def _result(*, is_error=False, is_empty=False, result=None):
    return types.SimpleNamespace(
        is_error=lambda: is_error,
        is_empty=lambda: is_empty,
        result=result,
    )


def _track(identifier, *, stream=False):
    return types.SimpleNamespace(identifier=identifier, is_stream=stream)


class _FakePlaylist:
    """Stand-in for sonolink.models.Playlist with a settable ``tracks`` list."""

    def __init__(self, tracks):
        self.tracks = tracks


def test_select_playable_none_error_empty_return_empty():
    assert music.select_playable(None, 5) == []
    assert music.select_playable(_result(is_error=True), 5) == []
    assert music.select_playable(_result(is_empty=True), 5) == []
    assert music.select_playable(_result(result=None), 5) == []


def test_select_playable_list_caps_at_limit():
    tracks = [_track(f"id{i}") for i in range(10)]
    picked = music.select_playable(_result(result=tracks), 3)
    assert [t.identifier for t in picked] == ["id0", "id1", "id2"]


def test_select_playable_skips_streams():
    tracks = [_track("a"), _track("b", stream=True), _track("c")]
    picked = music.select_playable(_result(result=tracks), 5)
    assert [t.identifier for t in picked] == ["a", "c"]


def test_select_playable_dedupes_against_seen_ids():
    tracks = [_track("a"), _track("b"), _track("c")]
    picked = music.select_playable(_result(result=tracks), 5, seen_ids={"b"})
    assert [t.identifier for t in picked] == ["a", "c"]


def test_select_playable_dedupes_within_result():
    tracks = [_track("a"), _track("a"), _track("d")]
    picked = music.select_playable(_result(result=tracks), 5)
    assert [t.identifier for t in picked] == ["a", "d"]


def test_select_playable_single_track_payload():
    picked = music.select_playable(_result(result=_track("solo")), 5)
    assert [t.identifier for t in picked] == ["solo"]


def test_select_playable_playlist_payload(monkeypatch):
    import sonolink.models as sonolink_models

    monkeypatch.setattr(sonolink_models, "Playlist", _FakePlaylist)
    playlist = _FakePlaylist([_track("p0"), _track("p1", stream=True), _track("p2")])
    picked = music.select_playable(_result(result=playlist), 5)
    assert [t.identifier for t in picked] == ["p0", "p2"]


# ---------------------------------------------------------------------------
# PendingVoiceWatches
# ---------------------------------------------------------------------------


def test_watch_add_then_pop_returns_payload_once():
    watches = vibes.PendingVoiceWatches(ttl=300)
    payload = object()
    watches.add(1, 2, payload, now=1000.0)
    assert watches.pop(1, 2, now=1010.0) is payload
    # Fire-once: the entry is gone after a successful pop.
    assert watches.pop(1, 2, now=1011.0) is None


def test_watch_pop_missing_key_returns_none():
    watches = vibes.PendingVoiceWatches()
    assert watches.pop(9, 9, now=0.0) is None


def test_watch_expires_after_ttl():
    watches = vibes.PendingVoiceWatches(ttl=300)
    watches.add(1, 2, object(), now=1000.0)
    # At the TTL boundary the watch has expired and reads as absent.
    assert watches.pop(1, 2, now=1300.0) is None


def test_watch_pop_removes_expired_entry():
    watches = vibes.PendingVoiceWatches(ttl=300)
    watches.add(1, 2, object(), now=1000.0)
    assert watches.pop(1, 2, now=1400.0) is None
    assert (1, 2) not in watches


def test_watch_discard_removes_entry():
    watches = vibes.PendingVoiceWatches()
    watches.add(1, 2, object(), now=0.0)
    assert (1, 2) in watches
    watches.discard(1, 2)
    assert (1, 2) not in watches
    watches.discard(1, 2)  # idempotent


def test_watch_add_overwrites_same_key():
    watches = vibes.PendingVoiceWatches()
    first, second = object(), object()
    watches.add(1, 2, first, now=0.0)
    watches.add(1, 2, second, now=0.0)
    assert len(watches) == 1
    assert watches.pop(1, 2, now=0.0) is second


def test_watch_sweep_bounds_the_map():
    watches = vibes.PendingVoiceWatches(ttl=10, sweep_at=3)
    watches.add(0, 0, object(), now=0.0)
    watches.add(0, 1, object(), now=0.0)
    watches.add(0, 2, object(), now=0.0)
    assert len(watches) == 3  # still at the cap, no sweep yet

    # A fourth add well past the window trips the sweep, dropping the stale ones.
    watches.add(1, 0, object(), now=1000.0)
    assert len(watches) == 1
    assert (1, 0) in watches


# ---------------------------------------------------------------------------
# joinable_voice_channels
# ---------------------------------------------------------------------------


def _perm(view=True, connect=True):
    return types.SimpleNamespace(view_channel=view, connect=connect)


def _voice_channel(name, perm):
    return types.SimpleNamespace(name=name, permissions_for=lambda _m, _p=perm: _p)


def _guild(channels):
    return types.SimpleNamespace(voice_channels=channels)


def test_joinable_channels_filters_by_permissions():
    ok = _voice_channel("ok", _perm())
    no_connect = _voice_channel("noc", _perm(connect=False))
    no_view = _voice_channel("nov", _perm(view=False))
    guild = _guild([ok, no_connect, no_view])
    result = music.joinable_voice_channels(guild, member=object())
    assert result == [ok]


def test_joinable_channels_respects_limit_and_order():
    channels = [_voice_channel(f"c{i}", _perm()) for i in range(8)]
    guild = _guild(channels)
    result = music.joinable_voice_channels(guild, member=object(), limit=5)
    assert result == channels[:5]


def test_joinable_channels_none_joinable():
    channels = [_voice_channel("x", _perm(connect=False))]
    guild = _guild(channels)
    assert music.joinable_voice_channels(guild, member=object()) == []

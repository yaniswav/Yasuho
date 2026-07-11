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
        assert g.query_trending.strip(), g
        assert g.query_alltime.strip(), g
        assert g.description.strip(), g


def test_catalog_labels_queries_descriptions_are_ascii():
    # Proper-name labels, both curated queries and descriptive text must stay plain
    # ASCII (no fancy typography); only the emoji field carries non-ASCII.
    for g in vibes.GENRE_CATALOG:
        assert g.label.isascii(), g.label
        assert g.query_trending.isascii(), g.query_trending
        assert g.query_alltime.isascii(), g.query_alltime
        assert g.description.isascii(), g.description


def test_catalog_has_no_forbidden_punctuation():
    for g in vibes.GENRE_CATALOG:
        for field in (g.label, g.query_trending, g.query_alltime, g.description):
            for bad in _FORBIDDEN:
                assert bad not in field, (g.key, field)


def test_catalog_trending_and_alltime_queries_differ():
    # The two queries per genre must be distinct so blending them actually widens
    # the candidate pool rather than doubling one search.
    for g in vibes.GENRE_CATALOG:
        assert g.query_trending != g.query_alltime, g.key


def test_catalog_alltime_queries_have_no_year_placeholder():
    # Only the trending query carries recency; the evergreen one must not.
    for g in vibes.GENRE_CATALOG:
        assert "{year}" not in g.query_alltime, g.key


def test_catalog_no_hardcoded_year_literals():
    # No stale year may be baked into a query; recency is spliced in at runtime.
    for g in vibes.GENRE_CATALOG:
        for field in (g.query_trending, g.query_alltime):
            assert not any(tok.isdigit() and len(tok) == 4 for tok in field.split()), (
                g.key,
                field,
            )


def test_catalog_queries_resolve_to_ascii_with_year():
    # Splicing the runtime year in must keep the query ASCII and placeholder-free.
    for g in vibes.GENRE_CATALOG:
        for template in (g.query_trending, g.query_alltime):
            resolved = vibes.resolve_query(template)
            assert resolved.isascii(), resolved
            assert "{year}" not in resolved, resolved


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


# ---------------------------------------------------------------------------
# looks_like_mix / mix_score  (the weighted mix/compilation detector)
# ---------------------------------------------------------------------------

_MIN = 60 * 1000  # one minute in milliseconds


# Hard false positives: real single tracks that must NEVER be flagged as a mix.
# Each is a title/author/minutes triple drawn from the lot R1 guard roster.
_NOT_MIX = [
    ("Mixed Signals", "Some Artist", 3.5),  # "mixed" is not the word "mix"
    ("Radio Ga Ga", "Queen", 5.8),  # lone weak "radio"
    ("Nonstop", "Drake", 3.97),  # lone weak "nonstop"
    ("DJ Got Us Fallin' In Love", "Usher", 3.67),  # "dj" without "set"
    ("Midnight Drive", "PhonkMix", 4.0),  # author ends in "Mix", nothing else
    ("lofi study beat", "ChillHop", 2.0),  # short, no signal
    ("Strobe (Club Mix)", "deadmau5", 6.0),  # remix single, under 8 min
    ("Levels (Radio Edit)", "Avicii", 3.5),  # radio edit single
    ("Set Fire to the Rain", "Adele", 4.1),  # "set" must not be a keyword
    # "best of" is a superlative FRAGMENT that titles real singles, not just
    # compilations - a normal-length track carrying it must not be dropped.
    ("Best of You", "Foo Fighters", 4.27),  # rock single
    ("The Best of Me", "Bryan Adams", 4.5),  # pop single
    ("Best of My Love", "The Emotions", 3.7),  # soul single
    ("Best of Both Worlds", "Van Halen", 5.0),  # rock single
]

# Hard true positives: hour-long mixes/compilations that must ALWAYS be flagged.
_IS_MIX = [
    ("PHONK MIX 2025", "Twisco", 61.0),  # keyword + year + long duration
    ("1 Hour Lofi Compilation", "Lofi Girl", 60.0),  # hour marker + compilation
    ("Best of Jazz 2010-2020", "Jazz Cafe", 45.0),  # best of + year range
    ("Rock Classics Full Album", "Rock Vault", 50.0),  # full album phrase
    ("untitled track", "unknown", 45.0),  # 45 min alone is near-certain
]


def test_looks_like_mix_hard_false_positives():
    for title, author, minutes in _NOT_MIX:
        assert not vibes.looks_like_mix(title, author, int(minutes * _MIN)), (
            title,
            vibes.mix_score(title, author, int(minutes * _MIN)),
        )


def test_looks_like_mix_hard_true_positives():
    for title, author, minutes in _IS_MIX:
        assert vibes.looks_like_mix(title, author, int(minutes * _MIN)), (
            title,
            vibes.mix_score(title, author, int(minutes * _MIN)),
        )


def test_mix_title_only_positives_flag_at_song_length():
    # The unambiguous phrases must flag on the title alone, even at a normal
    # 3-minute duration (no help from the duration signal).
    for title in (
        "1 Hour Lofi Compilation",
        "Best of Jazz 2010-2020",
        "Rock Classics Full Album",
    ):
        assert vibes.looks_like_mix(title, "x", 3 * _MIN), title


def test_mix_weak_keyword_alone_does_not_flag():
    # A single weak signal on a normal-length song stays under the threshold.
    for title in ("Summer Mix Vibes", "Radio Heart", "Nonstop Lover"):
        score = vibes.mix_score(title, "Artist", 3 * _MIN)
        assert score < vibes.MIX_SCORE_THRESHOLD, (title, score)
        assert not vibes.looks_like_mix(title, "Artist", 3 * _MIN)


def test_mix_keyword_plus_long_duration_flags():
    # The same weak keyword tips over once the duration is abnormal (8-20 min).
    assert not vibes.looks_like_mix("Phonk Mix", "Artist", 3 * _MIN)
    assert vibes.looks_like_mix("Phonk Mix", "Artist", 10 * _MIN)


def test_mix_very_long_duration_alone_flags():
    # Past ~20 minutes a track is a near-certain mix even with a blank title.
    assert vibes.looks_like_mix("", "", 25 * _MIN)
    # ...but an 8-20 min stretch alone (no keyword) is only a suspicion.
    assert not vibes.looks_like_mix("", "", 12 * _MIN)


def test_mix_several_weak_signals_concord_to_flag():
    # Two weak title signals plus a weak author tell reach the threshold with no
    # duration help: "mix" + a bare year + an author ending in "Radio".
    score = vibes.mix_score("Night Mix 2024", "Chill Radio", 3 * _MIN)
    assert score >= vibes.MIX_SCORE_THRESHOLD, score
    assert vibes.looks_like_mix("Night Mix 2024", "Chill Radio", 3 * _MIN)


def test_mix_score_threshold_boundary():
    # Exactly at the threshold flags; one below does not.
    assert vibes.mix_score("Rock Classics Full Album", "x", 3 * _MIN) >= (
        vibes.MIX_SCORE_THRESHOLD
    )
    # A 10-minute track with no keyword scores the long-duration points only.
    assert vibes.mix_score("Just A Song", "x", 10 * _MIN) < vibes.MIX_SCORE_THRESHOLD


def test_mix_author_suffix_is_weak_not_decisive():
    # An artist channel ending in "Mix"/"Radio"/"Compilation" alone never flags.
    for author in ("PhonkMix", "Chill Radio", "Jazz Compilation"):
        assert not vibes.looks_like_mix("Some Song", author, 3 * _MIN), author


def test_mix_author_multi_artist_credit_is_weak():
    # Lavalink's "and N more" multi-artist credit is a nudge, not a verdict.
    assert not vibes.looks_like_mix("A Song", "Artist and 3 more", 3 * _MIN)


def test_mix_none_and_zero_duration_safe():
    # None title/author/duration must not raise and score nothing on their own.
    assert not vibes.looks_like_mix(None, None, None)
    assert vibes.mix_score(None, None, None) == 0
    assert not vibes.looks_like_mix("A Song", "Artist", 0)


def test_mix_float_duration_is_handled():
    # A decimal (float) duration must be coerced, not crash.
    assert not vibes.looks_like_mix("lofi beat", "ChillHop", 120000.0)
    assert vibes.looks_like_mix("lofi beat", "ChillHop", 25.0 * _MIN)


def test_mix_accent_folded_keywords_flag():
    # A multilingual compilation title with accents still matches the ASCII rules.
    assert vibes.looks_like_mix("Les Meilleurs Tubes", "x", 3 * _MIN)
    assert vibes.looks_like_mix("Recopilacion de Exitos", "x", 3 * _MIN)


def test_mix_hour_marker_multilingual():
    # "N hours" alone is a medium signal (2), one short of the line...
    assert not vibes.looks_like_mix("2 Heures de Lofi", "x", 3 * _MIN)
    # ...but paired with a weak keyword it crosses, even at song length.
    assert vibes.looks_like_mix("1 Hour Phonk Mix", "x", 3 * _MIN)


def test_mix_best_of_is_medium_needs_corroboration():
    # "best of" is a medium signal, NOT strong: a normal-length single carrying it
    # (e.g. "Best of You") stays under the threshold on the phrase alone...
    assert vibes.mix_score("Best of You", "Foo Fighters", 4 * _MIN) < (
        vibes.MIX_SCORE_THRESHOLD
    )
    assert not vibes.looks_like_mix("Best of You", "Foo Fighters", 4 * _MIN)
    # ...but a genuine "Best Of" compilation is always album-length, so the
    # duration bracket corroborates it past the line.
    assert vibes.looks_like_mix("Best of Queen", "Queen Fans", 60 * _MIN)
    # A year corroborator does the same at song length (a real compilation label).
    assert vibes.looks_like_mix("Best of 2024", "Some Channel", 3 * _MIN)


# ---------------------------------------------------------------------------
# interleave_results
# ---------------------------------------------------------------------------


def test_interleave_alternates_and_preserves_order():
    a = [_track("a0"), _track("a1"), _track("a2")]
    b = [_track("b0"), _track("b1"), _track("b2")]
    out = vibes.interleave_results(a, b)
    assert [t.identifier for t in out] == ["a0", "b0", "a1", "b1", "a2", "b2"]


def test_interleave_dedupes_by_identifier_first_wins():
    a = [_track("x"), _track("a1")]
    b = [_track("x"), _track("b1")]  # duplicate "x" is dropped on second sight
    out = vibes.interleave_results(a, b)
    assert [t.identifier for t in out] == ["x", "a1", "b1"]


def test_interleave_uneven_lengths():
    a = [_track("a0"), _track("a1"), _track("a2")]
    b = [_track("b0")]
    out = vibes.interleave_results(a, b)
    assert [t.identifier for t in out] == ["a0", "b0", "a1", "a2"]


def test_interleave_empty_inputs():
    assert vibes.interleave_results([], []) == []
    a = [_track("a0")]
    assert [t.identifier for t in vibes.interleave_results(a, [])] == ["a0"]


def test_interleave_keeps_identifierless_tracks():
    # Tracks with no identifier cannot be deduped, so they are always kept.
    n0 = types.SimpleNamespace(identifier=None)
    n1 = types.SimpleNamespace(identifier=None)
    out = vibes.interleave_results([n0], [n1])
    assert out == [n0, n1]


# ---------------------------------------------------------------------------
# resolve_query / current_year
# ---------------------------------------------------------------------------


def test_resolve_query_fills_year():
    import datetime

    fixed = datetime.datetime(2031, 5, 1, tzinfo=datetime.timezone.utc)
    assert vibes.resolve_query("phonk sped up {year}", now=fixed) == "phonk sped up 2031"


def test_resolve_query_passthrough_without_placeholder():
    assert vibes.resolve_query("classic rock single") == "classic rock single"


def test_current_year_injectable():
    import datetime

    fixed = datetime.datetime(2029, 1, 1, tzinfo=datetime.timezone.utc)
    assert vibes.current_year(fixed) == 2029


# ---------------------------------------------------------------------------
# select_playable new params + filter_tracks
# ---------------------------------------------------------------------------


def _dtrack(identifier, *, length=180000, stream=False, title="", author=""):
    return types.SimpleNamespace(
        identifier=identifier,
        is_stream=stream,
        length=length,
        title=title,
        author=author,
    )


def test_select_playable_max_duration_filters():
    tracks = [
        _dtrack("short", length=3 * _MIN),
        _dtrack("long", length=30 * _MIN),
        _dtrack("mid", length=5 * _MIN),
    ]
    picked = music.select_playable(
        _result(result=tracks), 5, max_duration_ms=10 * _MIN
    )
    assert [t.identifier for t in picked] == ["short", "mid"]


def test_select_playable_reject_predicate_filters():
    tracks = [_dtrack("keep"), _dtrack("drop"), _dtrack("keep2")]
    picked = music.select_playable(
        _result(result=tracks), 5, reject=lambda t: t.identifier == "drop"
    )
    assert [t.identifier for t in picked] == ["keep", "keep2"]


def test_select_playable_backwards_compatible_defaults():
    # Existing callers pass neither new kwarg and must behave exactly as before.
    tracks = [_dtrack("a", length=90 * _MIN), _dtrack("b")]
    picked = music.select_playable(_result(result=tracks), 5)
    assert [t.identifier for t in picked] == ["a", "b"]


def test_filter_tracks_list_primitive():
    tracks = [_dtrack("a"), _dtrack("a"), _dtrack("b", stream=True), _dtrack("c")]
    picked = music.filter_tracks(tracks, 5)
    assert [t.identifier for t in picked] == ["a", "c"]


# ---------------------------------------------------------------------------
# choose_genre_tracks  (the 3-tier fallback ladder)
# ---------------------------------------------------------------------------


def _song(identifier, minutes):
    # A clean individual song: no mix keywords, normal length.
    return _dtrack(identifier, length=int(minutes * _MIN), title="Some Song", author="An Artist")


def _mix(identifier, minutes):
    # A titled mix at the given length (flagged by looks_like_mix at 8+ min).
    return _dtrack(identifier, length=int(minutes * _MIN), title="Genre Mix", author="DJ")


def test_ladder_tier1_when_enough_clean_songs():
    tracks = [_song(f"s{i}", 3) for i in range(6)]
    tier, picked = music.choose_genre_tracks(tracks, 7)
    assert tier == 1
    assert len(picked) == 6


def test_ladder_tier1_keeps_songs_and_drops_mixes():
    tracks = [_song("s0", 3), _mix("m0", 15), _song("s1", 3), _mix("m1", 60), _song("s2", 3)]
    tier, picked = music.choose_genre_tracks(tracks, 7)
    assert tier == 1
    assert [t.identifier for t in picked] == ["s0", "s1", "s2"]


def test_ladder_descends_to_tier2_when_strict_thin():
    # All candidates are 15-min "mixes" (flagged, but under 20 min): the strict
    # tier yields nothing, so the ladder falls to the duration-only tier.
    tracks = [_mix(f"m{i}", 15) for i in range(5)]
    tier, picked = music.choose_genre_tracks(tracks, 7)
    assert tier == 2
    assert len(picked) == 5


def test_ladder_descends_to_tier3_when_all_too_long():
    # Everything is a 25-min mix: strict and duration-only both come up short, so
    # the raw tier seeds something rather than nothing.
    tracks = [_mix(f"m{i}", 25) for i in range(4)]
    tier, picked = music.choose_genre_tracks(tracks, 7)
    assert tier == 3
    assert len(picked) == 4


def test_ladder_descends_when_strict_under_three():
    # Two clean songs is below the descend cutoff, so the ladder drops to tier 2
    # and blends the songs with the short mixes rather than returning just two.
    tracks = [_song("s0", 3), _song("s1", 3), _mix("m0", 15), _mix("m1", 15), _mix("m2", 15)]
    tier, picked = music.choose_genre_tracks(tracks, 7)
    assert tier == 2
    assert len(picked) == 5


def test_ladder_respects_seen_ids_and_limit():
    tracks = [_song(f"s{i}", 3) for i in range(10)]
    tier, picked = music.choose_genre_tracks(tracks, 3, seen_ids={"s0", "s1"})
    assert tier == 1
    assert [t.identifier for t in picked] == ["s2", "s3", "s4"]

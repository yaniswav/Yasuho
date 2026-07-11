"""Unit tests for the premium /search browser and the opt-in /play picker.

Everything here is side-effect free: no Discord gateway, no Lavalink node, no
database and no network. The LavaSearch parsing is pinned against a fixture
RECORDED from the live node (``GET /v4/loadsearch?query=spsearch:daft%20punk``)
and then SCRUBBED - the long base64 ``encoded`` blobs are replaced with a
placeholder (the browser routes by URI, never by the encoded track) and only the
display fields the tabs read are kept. The rest exercises the pure decision and
rendering helpers, and drives the two async command bodies against small fakes.

``sonolink`` is imported for real on 3.12+ and stubbed by the repo-root conftest
on the 3.10 dev box; nothing here touches it directly - the track/result fakes
are duck-typed to the tiny surface :mod:`cogs.music.search` reads.
"""

import types

import pytest

from cogs.community import usersettings
from cogs.music import search
from tools import settings

# ---------------------------------------------------------------------------
# Recorded + scrubbed live LavaSearch fixture (spsearch:daft punk, trimmed to 2
# of each type). The second playlist item is a real url-less entry, kept to pin
# the "drop the unplayable" parse path. Volatile base64 encoded blobs scrubbed.
# ---------------------------------------------------------------------------

LAVASEARCH_FIXTURE = {
    "tracks": [
        {
            "encoded": "SCRUBBED_BASE64",
            "info": {
                "identifier": "0DiWol3AO6WpXZgp0goxAV",
                "isSeekable": True,
                "author": "Daft Punk",
                "length": 320357,
                "isStream": False,
                "position": 0,
                "title": "One More Time",
                "uri": "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV",
                "sourceName": "spotify",
                "artworkUrl": "https://i.scdn.co/image/track-one",
                "isrc": "GBDUW0000053",
            },
            "pluginInfo": {
                "albumName": "Discovery",
                "albumUrl": "https://open.spotify.com/album/2noRn2Aes5aoNVsU6iWThc",
                "artistUrl": "https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi",
            },
            "userData": {},
        },
        {
            "encoded": "SCRUBBED_BASE64",
            "info": {
                "identifier": "69kOkLUCkxIZYexIgSG8rq",
                "isSeekable": True,
                "author": "Daft Punk",
                "length": 369626,
                "isStream": False,
                "position": 0,
                "title": "Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
                "uri": "https://open.spotify.com/track/69kOkLUCkxIZYexIgSG8rq",
                "sourceName": "spotify",
                "artworkUrl": "https://i.scdn.co/image/track-two",
                "isrc": "USQX91300108",
            },
            "pluginInfo": {
                "albumName": "Random Access Memories",
                "albumUrl": "https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa",
                "artistUrl": "https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi",
            },
            "userData": {},
        },
    ],
    "albums": [
        {
            "info": {"name": "Random Access Memories", "selectedTrack": -1},
            "pluginInfo": {
                "author": "Daft Punk",
                "totalTracks": 13,
                "artworkUrl": "https://i.scdn.co/image/album-ram",
                "type": "album",
                "url": "https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa",
            },
            "tracks": [],
        },
        {
            "info": {"name": "Discovery", "selectedTrack": -1},
            "pluginInfo": {
                "author": "Daft Punk",
                "totalTracks": 14,
                "artworkUrl": "https://i.scdn.co/image/album-disc",
                "type": "album",
                "url": "https://open.spotify.com/album/2noRn2Aes5aoNVsU6iWThc",
            },
            "tracks": [],
        },
    ],
    "artists": [
        {
            "info": {"name": "Daft Punk's Top Tracks", "selectedTrack": -1},
            "pluginInfo": {
                "author": "Daft Punk",
                "totalTracks": None,
                "artworkUrl": "https://i.scdn.co/image/artist-dp",
                "type": "artist",
                "url": "https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi",
            },
            "tracks": [],
        },
        {
            "info": {"name": "David Guetta's Top Tracks", "selectedTrack": -1},
            "pluginInfo": {
                "author": "David Guetta",
                "totalTracks": None,
                "artworkUrl": "https://i.scdn.co/image/artist-dg",
                "type": "artist",
                "url": "https://open.spotify.com/artist/1Cs0zKBU1kc0i8ypK3B9ai",
            },
            "tracks": [],
        },
    ],
    "playlists": [
        {
            "info": {"name": "Daft Punk Greatest Hits", "selectedTrack": -1},
            "pluginInfo": {
                "author": "Red Franzen",
                "totalTracks": 41,
                "artworkUrl": "https://image-cdn/playlist-hits",
                "type": "playlist",
                "url": "https://open.spotify.com/playlist/2jTy5QwqWJ1ZUv2XeJPYbn",
            },
            "tracks": [],
        },
        {
            # Real-world url-less entry: unplayable, must be dropped by the parser.
            "info": {"name": "", "selectedTrack": -1},
            "pluginInfo": {
                "author": None,
                "totalTracks": 0,
                "artworkUrl": None,
                "type": "playlist",
                "url": None,
            },
            "tracks": [],
        },
    ],
    "texts": [],
    "plugin": {},
}


# ---------------------------------------------------------------------------
# Small duck-typed fakes for the sonolink Playable / SearchResult surface
# ---------------------------------------------------------------------------


def _playable(uri, title="T", author="A", length=1000, artwork="art"):
    return types.SimpleNamespace(
        uri=uri, title=title, author=author, length=length, artwork=artwork
    )


class _FakeResult:
    """Duck-typed sonolink SearchResult: is_error()/is_empty()/result."""

    def __init__(self, result, *, error=False, empty=False):
        self._result = result
        self._error = error
        self._empty = empty

    def is_error(self):
        return self._error

    def is_empty(self):
        return self._empty

    @property
    def result(self):
        return self._result


class _FakePlaylist:
    def __init__(self, tracks):
        self.tracks = list(tracks)


# ===========================================================================
# Parsing (recorded fixture)
# ===========================================================================


def test_parse_tracks_from_fixture():
    results = search.parse_search_results(LAVASEARCH_FIXTURE)
    assert len(results.tracks) == 2
    first = results.tracks[0]
    assert first.title == "One More Time"
    assert first.subtitle == "Daft Punk"
    assert first.uri == "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV"
    assert first.length_ms == 320357
    assert first.artwork == "https://i.scdn.co/image/track-one"
    assert first.total_tracks is None


def test_parse_albums_from_fixture():
    results = search.parse_search_results(LAVASEARCH_FIXTURE)
    assert len(results.albums) == 2
    ram = results.albums[0]
    assert ram.title == "Random Access Memories"
    assert ram.subtitle == "Daft Punk"  # pluginInfo.author
    assert ram.total_tracks == 13
    assert ram.uri == "https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa"


def test_parse_artists_from_fixture():
    results = search.parse_search_results(LAVASEARCH_FIXTURE)
    assert len(results.artists) == 2
    dp = results.artists[0]
    assert dp.title == "Daft Punk's Top Tracks"  # info.name
    assert dp.subtitle == "Daft Punk"  # bare artist name for the fallback search
    assert dp.total_tracks is None
    assert dp.uri == "https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi"


def test_parse_playlists_drops_urlless_entry():
    results = search.parse_search_results(LAVASEARCH_FIXTURE)
    # Two playlist items in the fixture, but the second has url=None -> dropped.
    assert len(results.playlists) == 1
    assert results.playlists[0].title == "Daft Punk Greatest Hits"
    assert results.playlists[0].total_tracks == 41
    assert results.degraded is False
    assert results.has_any() is True


def test_parse_none_payload_is_empty():
    results = search.parse_search_results(None)
    assert results.has_any() is False
    assert results.degraded is False
    assert results.tracks == ()


def test_parse_non_dict_and_bad_categories():
    assert search.parse_search_results("nope").has_any() is False
    assert search.parse_search_results({}).has_any() is False
    # A category that is not a list is skipped, not fatal.
    assert search.parse_search_results({"tracks": "notalist"}).tracks == ()


def test_parse_track_without_uri_is_dropped():
    payload = {"tracks": [{"info": {"title": "No URI", "author": "X"}}]}
    assert search.parse_search_results(payload).tracks == ()


def test_parse_clamps_to_max_per_tab():
    many = {
        "tracks": [
            {
                "info": {
                    "title": "t{}".format(i),
                    "author": "a",
                    "uri": "https://open.spotify.com/track/{}".format(i),
                    "length": 1000,
                }
            }
            for i in range(search.MAX_PER_TAB + 5)
        ]
    }
    assert len(search.parse_search_results(many).tracks) == search.MAX_PER_TAB


# ===========================================================================
# Tabs, labels and select building
# ===========================================================================


def test_build_select_options_track_label_and_duration():
    results = search.parse_search_results(LAVASEARCH_FIXTURE)
    options = search.build_select_options(results.tracks, search.TAB_TRACKS)
    assert len(options) == 2
    assert options[0].label == "One More Time - Daft Punk"
    assert options[0].description == "05:20"  # 320357 ms
    assert options[0].value == "0"
    assert options[1].value == "1"


def test_build_select_options_collection_count_pluralized():
    results = search.parse_search_results(LAVASEARCH_FIXTURE)
    album_opts = search.build_select_options(results.albums, search.TAB_ALBUMS)
    assert album_opts[0].label == "Random Access Memories - Daft Punk"
    assert album_opts[0].description == "13 tracks"


def test_build_select_options_singular_track_count():
    one = search.Entry(title="Solo", subtitle="Artist", uri="u", total_tracks=1)
    opts = search.build_select_options([one], search.TAB_ALBUMS)
    assert opts[0].description == "1 track"


def test_build_select_options_empty_category():
    assert search.build_select_options([], search.TAB_ALBUMS) == []


def test_truncate_uses_ascii_ellipsis():
    out = search.truncate("x" * 120, 100)
    assert len(out) == 100
    assert out.endswith("...")
    assert "\u2026" not in out  # never the fancy (unicode) ellipsis
    assert search.truncate("short", 100) == "short"


def test_entry_label_unknown_title_fallback():
    assert search._entry_label(search.Entry(title="", subtitle="", uri="u")) == (
        "Unknown title"
    )
    assert search._entry_label(search.Entry(title="", subtitle="X", uri="u")) == (
        "Unknown title - X"
    )


def test_long_label_truncated_to_field_limit():
    entry = search.Entry(title="A" * 200, subtitle="B" * 200, uri="u")
    opt = search.build_select_options([entry], search.TAB_TRACKS)[0]
    assert len(opt.label) <= 100


def test_initial_tab_prefers_first_nonempty():
    only_albums = search.SearchResults(albums=(search.Entry("n", "a", "u"),))
    assert search._initial_tab(only_albums) == search.TAB_ALBUMS
    assert search._initial_tab(search.SearchResults()) == search.TAB_TRACKS


def test_entries_for_unknown_tab_is_empty():
    assert search.SearchResults().entries_for("bogus") == ()


# ===========================================================================
# Prefix / degradation decision
# ===========================================================================


def test_decide_prefix_default_is_spsearch():
    assert search.decide_prefix(False) == "spsearch"
    assert search.decide_prefix(False) == search.DEFAULT_SEARCH_PREFIX


def test_decide_prefix_error_degrades_to_ytsearch():
    assert search.decide_prefix(True) == "ytsearch"
    assert search.decide_prefix(True) == search.FALLBACK_SEARCH_PREFIX


def test_build_query_prefixes_and_strips():
    assert search.build_query("spsearch", "  daft punk ") == "spsearch:daft punk"
    assert search.build_query("ytsearch", "x") == "ytsearch:x"


# ===========================================================================
# Picker routing decisions (pure)
# ===========================================================================


def test_is_url_query():
    assert search.is_url_query("https://open.spotify.com/track/x") is True
    assert search.is_url_query("http://youtu.be/x") is True
    assert search.is_url_query("  https://x  ") is True
    assert search.is_url_query("daft punk") is False
    assert search.is_url_query("spotify:track:x") is False  # no scheme://
    assert search.is_url_query("") is False


def test_should_show_picker_default_off():
    # Preference OFF (the default) never shows the picker.
    assert search.should_show_picker(False, False, True) is False


def test_should_show_picker_url_always_bypasses():
    assert search.should_show_picker(True, True, True) is False


def test_should_show_picker_needs_an_interaction():
    assert search.should_show_picker(True, False, False) is False


def test_should_show_picker_on_for_text_slash_query():
    assert search.should_show_picker(True, False, True) is True


# ===========================================================================
# top_track_choices / entry_from_playable / _result_is_empty
# ===========================================================================


def test_entry_from_playable():
    entry = search.entry_from_playable(_playable("u", title="Ti", author="Au"))
    assert entry.title == "Ti"
    assert entry.subtitle == "Au"
    assert entry.uri == "u"
    assert search.entry_from_playable(_playable(None)) is None


def test_top_track_choices_from_list_limits_and_drops_urlless():
    tracks = [_playable("u{}".format(i)) for i in range(10)]
    tracks.append(_playable(None))  # unplayable, dropped
    result = _FakeResult(tracks)
    choices = search.top_track_choices(result, 5)
    assert len(choices) == 5
    assert [c.uri for c in choices] == ["u0", "u1", "u2", "u3", "u4"]


def test_top_track_choices_from_playlist_shape():
    playlist = _FakePlaylist([_playable("a"), _playable("b")])
    choices = search.top_track_choices(_FakeResult(playlist), 20)
    assert [c.uri for c in choices] == ["a", "b"]


def test_top_track_choices_from_single_track():
    choices = search.top_track_choices(_FakeResult(_playable("solo")), 20)
    assert [c.uri for c in choices] == ["solo"]


def test_top_track_choices_empty_error_and_none():
    assert search.top_track_choices(None, 5) == []
    assert search.top_track_choices(_FakeResult(None), 5) == []
    assert search.top_track_choices(_FakeResult([_playable("u")], error=True), 5) == []
    assert search.top_track_choices(_FakeResult([_playable("u")], empty=True), 5) == []


def test_result_is_empty():
    assert search._result_is_empty(None) is True
    assert search._result_is_empty(_FakeResult(None)) is True
    assert search._result_is_empty(_FakeResult([], error=True)) is True
    assert search._result_is_empty(_FakeResult([], empty=True)) is True
    assert search._result_is_empty(_FakeResult(_playable("u"))) is False


# ===========================================================================
# Browser view assembly (offline component build)
# ===========================================================================


def test_search_browser_builds_and_selects_first_nonempty_tab():
    results = search.parse_search_results(LAVASEARCH_FIXTURE)
    browser = search.SearchBrowser(None, author_id=1, query="daft punk", results=results)
    assert browser.active_tab == search.TAB_TRACKS
    # Re-building each tab (incl. the url-less-dropped playlists tab) never raises.
    for tab in search._TAB_ORDER:
        browser.active_tab = tab
        browser._build()


def test_search_browser_degraded_non_track_tab_builds_note():
    degraded = search.SearchResults(
        tracks=(search.Entry("t", "a", "https://x"),), degraded=True
    )
    browser = search.SearchBrowser(None, author_id=1, query="q", results=degraded)
    browser.active_tab = search.TAB_ALBUMS
    browser._build()  # exercises the "Spotify unavailable" note branch


# ===========================================================================
# Preference wiring parity with the autoplay precedent
# ===========================================================================


def test_picker_pref_key_and_default():
    assert search.SEARCH_PICKER_PREF_KEY == "music_search_picker"
    assert search.PICKER_DEFAULT is False


def test_usersettings_registers_the_picker_pref():
    match = [p for p in usersettings.PREFS if p.key == search.SEARCH_PICKER_PREF_KEY]
    assert len(match) == 1
    assert match[0].default is False


# ===========================================================================
# maybe_play_picker routing (async, small fakes)
# ===========================================================================


class _FakeCtx:
    def __init__(self, *, interaction, author_id=1):
        self.interaction = interaction
        self.author = types.SimpleNamespace(id=author_id)
        self.defers = []
        self.sends = []

    async def defer(self, **kwargs):
        self.defers.append(kwargs)

    async def send(self, content=None, **kwargs):
        self.sends.append((content, kwargs))
        return types.SimpleNamespace()


class _FakeCog:
    def __init__(self, pool, *, result=None, nodes=True):
        self.bot = types.SimpleNamespace(db_pool=pool)
        self._result = result
        self._nodes = nodes
        self.searched = []

    def _nodes_available(self):
        return self._nodes

    async def _search(self, query):
        self.searched.append(query)
        return self._result

    async def _play_query(self, ctx, query):
        raise AssertionError("classic /play path must not run when picker handles it")


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    settings._cache.clear()
    yield
    settings._cache.clear()


async def test_maybe_play_picker_url_bypasses_without_reading_pref(fake_pool):
    cog = _FakeCog(fake_pool)
    ctx = _FakeCtx(interaction=object())
    handled = await search.maybe_play_picker(cog, ctx, "https://open.spotify.com/track/x")
    assert handled is False
    assert fake_pool.calls == []  # no preference read at all
    assert ctx.defers == []


async def test_maybe_play_picker_prefix_invocation_bypasses(fake_pool):
    cog = _FakeCog(fake_pool)
    ctx = _FakeCtx(interaction=None)  # a text (prefix) /play has no interaction
    handled = await search.maybe_play_picker(cog, ctx, "daft punk")
    assert handled is False
    assert fake_pool.calls == []
    assert ctx.defers == []


async def test_maybe_play_picker_default_off_falls_through(fake_pool):
    # fetchval None -> empty settings -> pref defaults OFF.
    cog = _FakeCog(fake_pool)
    ctx = _FakeCtx(interaction=object(), author_id=42)
    handled = await search.maybe_play_picker(cog, ctx, "daft punk")
    assert handled is False
    assert ctx.defers == []  # did not defer -> classic /play runs unchanged
    assert ctx.sends == []


async def test_maybe_play_picker_on_shows_top5(fake_pool):
    settings._cache[("user_settings", 7)] = {"music_search_picker": True}
    tracks = [_playable("u{}".format(i)) for i in range(8)]
    cog = _FakeCog(fake_pool, result=_FakeResult(tracks))
    ctx = _FakeCtx(interaction=object(), author_id=7)
    handled = await search.maybe_play_picker(cog, ctx, "daft punk")
    assert handled is True
    assert ctx.defers and ctx.defers[0].get("ephemeral") is True
    assert cog.searched == ["daft punk"]
    # A picker view was sent with exactly the top five track choices.
    (_content, kwargs) = ctx.sends[-1]
    view = kwargs["view"]
    assert isinstance(view, search.PlayPickerView)
    assert len(view.entries) == search.PLAY_PICKER_LIMIT


async def test_maybe_play_picker_on_but_no_matches_reports(fake_pool):
    settings._cache[("user_settings", 9)] = {"music_search_picker": True}
    cog = _FakeCog(fake_pool, result=_FakeResult(None, empty=True))
    ctx = _FakeCtx(interaction=object(), author_id=9)
    handled = await search.maybe_play_picker(cog, ctx, "obscure")
    assert handled is True
    # Reported "couldn't find" as text, no picker view sent.
    (content, kwargs) = ctx.sends[-1]
    assert "view" not in kwargs
    assert content is not None

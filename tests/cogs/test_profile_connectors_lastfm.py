"""Tests for the Last.fm connector (cogs/community/profile/connectors/lastfm.py).

Offline throughout: :func:`lastfm._request` (the one function that ever
touches the network) is monkeypatched everywhere, so nothing here opens a
socket or needs a real API key. What is actually at risk in a scraping-free,
JSON-API connector is smaller than Backloggd's: existence/format validation
before any network call, error-code mapping (Last.fm answers "no such user"
with error 6 and an HTTP 200, never a 404), defensive parsing of a payload a
third party controls, and a renderer that never touches the network.
"""

import configparser
import json

import discord
import pytest

from cogs.community.profile.connectors import base, lastfm

USER = 4242


def _text(container):
    """Every TextDisplay's content, in ``container``, Section-wrapped or not."""
    out = []
    for item in container.children:
        if hasattr(item, "content"):
            out.append(item.content)
        elif hasattr(item, "children"):
            for child in item.children:
                if hasattr(child, "content"):
                    out.append(child.content)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_module_self_registers_at_import_time():
    assert isinstance(base.CONNECTORS.get("lastfm"), lastfm.LastFmConnector)


def test_the_renderer_is_registered():
    from cogs.community.profile import views

    assert views.SECTION_RENDERERS.get("lastfm") is lastfm._render


def test_the_refresh_ttl_is_a_positive_number_of_seconds():
    assert isinstance(lastfm.REFRESH_TTL_SECONDS, int)
    assert 0 < lastfm.REFRESH_TTL_SECONDS <= 3600


# ---------------------------------------------------------------------------
# Offline handle validation - no network for a malformed handle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handle", ("", "  ", "a", "x" * 16, "no spaces", "bad!name"))
async def test_link_refuses_a_malformed_handle_without_any_network_call(
    handle, monkeypatch
):
    called = []

    async def _boom(method, user, extra=None):
        called.append(method)
        raise AssertionError("must not be called for an offline-rejected handle")

    monkeypatch.setattr(lastfm, "_request", _boom)
    connector = lastfm.LastFmConnector()
    with pytest.raises(base.InvalidHandle) as caught:
        await connector.link(USER, handle)
    assert caught.value.reason == "format"
    assert called == []


@pytest.mark.parametrize("handle", ("ab", "Yanis_W", "a" * 15, "under_score-1"))
def test_the_handle_pattern_accepts_realistic_usernames(handle):
    assert lastfm.HANDLE_PATTERN.match(handle)


# ---------------------------------------------------------------------------
# The API key: read lazily, never at import
# ---------------------------------------------------------------------------


def test_the_api_key_is_read_lazily_and_missing_is_not_configured(monkeypatch):
    from tools.config_loader import config_loader

    def _missing(section, option):
        raise configparser.NoOptionError(option, section)

    monkeypatch.setattr(config_loader, "getstr", _missing)
    with pytest.raises(base.ConnectorUnavailable) as caught:
        lastfm._api_key()
    assert caught.value.reason == "not_configured"


async def test_link_surfaces_the_missing_key_before_touching_the_network(
    monkeypatch,
):
    """``_api_key`` is the first thing ``_request`` does, so a missing key
    surfaces before ``_get_session`` (and therefore any real network setup)
    is ever reached - checked here by never installing a session at all."""
    from tools.config_loader import config_loader

    def _missing(section, option):
        raise configparser.NoSectionError(section)

    monkeypatch.setattr(config_loader, "getstr", _missing)
    connector = lastfm.LastFmConnector()
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await connector.link(USER, "Yanis")
    assert caught.value.reason == "not_configured"


# ---------------------------------------------------------------------------
# link() / refresh(): the three-call fetch, and what it produces
# ---------------------------------------------------------------------------


def _responses(*, playcount="1234", name="Yanis", top=None, track=None):
    top = top if top is not None else [
        {"name": "Radiohead", "playcount": "50"},
        {"name": "Boards of Canada", "playcount": "30"},
    ]
    info = {"user": {"name": name, "playcount": playcount, "image": [
        {"size": "small", "#text": ""},
        {"size": "extralarge", "#text": "https://lastfm/avatar.png"},
    ]}}
    topartists = {"topartists": {"artist": top}}
    recenttracks = {"recenttracks": {"track": [track] if track else []}}

    async def _request(method, user, extra=None):
        if method == "user.getinfo":
            return info
        if method == "user.gettopartists":
            return topartists
        if method == "user.getrecenttracks":
            return recenttracks
        raise AssertionError(f"unexpected method {method!r}")

    return _request


async def test_link_builds_the_payload_from_all_three_calls(monkeypatch):
    monkeypatch.setattr(lastfm, "_request", _responses())
    connector = lastfm.LastFmConnector()
    result = await connector.link(USER, "Yanis")
    assert result.external_id == "yanis"
    assert result.display_name == "Yanis"
    assert result.payload["playcount"] == 1234
    assert result.payload["avatar"] == "https://lastfm/avatar.png"
    assert result.payload["top_artists"] == [
        {"name": "Radiohead", "playcount": 50},
        {"name": "Boards of Canada", "playcount": 30},
    ]
    assert "last_track" not in result.payload


async def test_link_captures_a_now_playing_track(monkeypatch):
    monkeypatch.setattr(
        lastfm,
        "_request",
        _responses(
            track={
                "name": "Everything In Its Right Place",
                "artist": {"#text": "Radiohead"},
                "album": {"#text": "Kid A"},
                "@attr": {"nowplaying": "true"},
            }
        ),
    )
    connector = lastfm.LastFmConnector()
    result = await connector.link(USER, "Yanis")
    assert result.payload["last_track"] == {
        "artist": "Radiohead",
        "name": "Everything In Its Right Place",
        "nowplaying": True,
        "album": "Kid A",
    }


async def test_refresh_re_fetches_and_returns_a_bare_payload(monkeypatch):
    monkeypatch.setattr(lastfm, "_request", _responses(playcount="99"))
    connector = lastfm.LastFmConnector()
    payload = await connector.refresh(
        USER, {"external_id": "yanis", "payload": {}}
    )
    assert payload["playcount"] == 99
    assert "external_id" not in payload


async def test_refresh_refuses_a_connection_with_no_handle(monkeypatch):
    connector = lastfm.LastFmConnector()
    with pytest.raises(base.NotLinked):
        await connector.refresh(USER, {"external_id": "", "payload": {}})


# ---------------------------------------------------------------------------
# _request(): status/error mapping (offline, via a fake aiohttp session)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None, timeout=None):
        return _FakeResponse(self._payload)


def _async_return(value):
    """``async def _get_session() -> value`` - :func:`lastfm._get_session` is
    awaited by :func:`lastfm._request`, so the monkeypatch replacing it must
    be a coroutine function too, not a plain lambda."""

    async def _factory():
        return value

    return _factory


async def test_request_maps_error_6_to_not_found(monkeypatch):
    monkeypatch.setattr(lastfm, "_api_key", lambda: "test-key")
    session = _FakeSession({"error": 6, "message": "no"})
    monkeypatch.setattr(lastfm, "_get_session", _async_return(session))
    with pytest.raises(base.InvalidHandle) as caught:
        await lastfm._request("user.getinfo", "ghost")
    assert caught.value.reason == "not_found"


async def test_request_maps_any_other_error_code_to_unavailable(monkeypatch):
    monkeypatch.setattr(lastfm, "_api_key", lambda: "test-key")
    session = _FakeSession({"error": 29, "message": "slow"})
    monkeypatch.setattr(lastfm, "_get_session", _async_return(session))
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await lastfm._request("user.getinfo", "someone")
    assert caught.value.reason == "remote"


async def test_request_maps_a_non_json_body_to_unavailable(monkeypatch):
    monkeypatch.setattr(lastfm, "_api_key", lambda: "test-key")
    session = _FakeSession(None)
    monkeypatch.setattr(lastfm, "_get_session", _async_return(session))
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await lastfm._request("user.getinfo", "someone")
    assert caught.value.reason == "remote"


async def test_request_maps_a_raised_exception_to_unavailable(monkeypatch):
    class _ExplodingSession:
        def get(self, *a, **k):
            raise TimeoutError("slow network")

    monkeypatch.setattr(lastfm, "_api_key", lambda: "test-key")
    monkeypatch.setattr(lastfm, "_get_session", _async_return(_ExplodingSession()))
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await lastfm._request("user.getinfo", "someone")
    assert caught.value.reason == "remote"


# ---------------------------------------------------------------------------
# Pure parsing: defensive against a payload a third party controls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data", (None, {}, {"user": "not a dict"}, {"user": None}, "not a dict at all")
)
def test_parse_info_never_raises_on_junk(data):
    assert lastfm.parse_info(data) is None


def test_parse_info_picks_the_largest_available_image():
    data = {
        "user": {
            "name": "Someone",
            "playcount": "10",
            "image": [
                {"size": "small", "#text": "https://small.png"},
                {"size": "large", "#text": ""},
                {"size": "extralarge", "#text": "https://big.png"},
            ],
        }
    }
    assert lastfm.parse_info(data)["avatar"] == "https://big.png"


def test_parse_info_tolerates_a_non_numeric_playcount():
    data = {"user": {"name": "Someone", "playcount": "not a number"}}
    assert lastfm.parse_info(data)["playcount"] is None


@pytest.mark.parametrize(
    "data",
    (
        None,
        {},
        {"topartists": "not a dict"},
        {"topartists": {"artist": "not a list"}},
        {"topartists": {"artist": [{"name": ""}, "junk", {"no_name": 1}]}},
    ),
)
def test_parse_top_artists_never_raises_on_junk(data):
    assert lastfm.parse_top_artists(data) == []


def test_parse_top_artists_is_capped_at_the_requested_limit():
    data = {
        "topartists": {
            "artist": [{"name": f"Artist {i}"} for i in range(10)],
        }
    }
    assert len(lastfm.parse_top_artists(data, limit=3)) == 3


@pytest.mark.parametrize(
    "data",
    (
        None,
        {},
        {"recenttracks": "not a dict"},
        {"recenttracks": {"track": "not a list"}},
        {"recenttracks": {"track": []}},
        {"recenttracks": {"track": [{"name": "only a name"}]}},
    ),
)
def test_parse_recent_track_never_raises_on_junk(data):
    assert lastfm.parse_recent_track(data) is None


def test_parse_recent_track_reads_the_nowplaying_flag():
    played = {
        "recenttracks": {
            "track": [
                {
                    "name": "Song",
                    "artist": {"#text": "Band"},
                    "@attr": {"nowplaying": "true"},
                }
            ]
        }
    }
    scrobbled = {
        "recenttracks": {
            "track": [{"name": "Song", "artist": {"#text": "Band"}}]
        }
    }
    assert lastfm.parse_recent_track(played)["nowplaying"] is True
    assert lastfm.parse_recent_track(scrobbled)["nowplaying"] is False


# ---------------------------------------------------------------------------
# Renderer: draws from payload only, never the network
# ---------------------------------------------------------------------------


class _Field:
    label = "Last.fm"


async def test_renderer_never_touches_the_network(monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("the renderer must never call the network")

    monkeypatch.setattr(lastfm, "_request", _boom)
    container = discord.ui.Container()
    connection = {
        "payload": {
            "playcount": 42,
            "avatar": "https://lastfm/avatar.png",
            "top_artists": [{"name": "Radiohead"}, {"name": "Boards of Canada"}],
            "last_track": {"artist": "Radiohead", "name": "Idioteque", "nowplaying": False},
        }
    }
    await lastfm._render(container, _Field(), None, connection, budget=None)
    text = _text(container)
    assert "42 scrobbles" in text
    assert "Radiohead, Boards of Canada" in text
    assert "Last scrobble: Radiohead - Idioteque" in text


async def test_renderer_shows_now_scrobbling_when_fresh():
    container = discord.ui.Container()
    connection = {
        "payload": {
            "last_track": {"artist": "Band", "name": "Song", "nowplaying": True}
        }
    }
    await lastfm._render(container, _Field(), None, connection, budget=None)
    assert "Now scrobbling: Band - Song" in _text(container)


async def test_renderer_flattens_a_multi_line_track_name():
    container = discord.ui.Container()
    connection = {
        "payload": {
            "last_track": {
                "artist": "Band",
                "name": "Song\n## Fake Heading",
                "nowplaying": False,
            }
        }
    }
    await lastfm._render(container, _Field(), None, connection, budget=None)
    text = _text(container)
    assert "\n## Fake Heading" not in text
    assert "Song ## Fake Heading" in text


async def test_renderer_handles_an_empty_payload_without_raising():
    container = discord.ui.Container()
    await lastfm._render(container, _Field(), None, {"payload": {}}, budget=None)
    assert _text(container) == "**Last.fm**"


# ---------------------------------------------------------------------------
# Payload stays bounded
# ---------------------------------------------------------------------------


async def test_the_built_payload_fits_the_shared_budget(monkeypatch):
    monkeypatch.setattr(
        lastfm,
        "_request",
        _responses(
            track={
                "name": "Song",
                "artist": {"#text": "Band"},
                "album": {"#text": "Album"},
            }
        ),
    )
    connector = lastfm.LastFmConnector()
    result = await connector.link(USER, "Yanis")
    encoded = base.encode_payload("lastfm", result.payload)
    assert len(encoded.encode("utf-8")) <= base.PAYLOAD_MAX_BYTES
    json.loads(encoded)  # round-trips as plain JSON


# ---------------------------------------------------------------------------
# Hostile remote data: everything below is a value Last.fm really can return,
# because a scrobbling client - not Last.fm - chooses the artist and track
# strings, and any member can point one at their own account.
# ---------------------------------------------------------------------------


async def test_an_absurdly_long_scrobble_still_produces_a_storable_payload(
    monkeypatch,
):
    """A scrobbler can send a title of any length. Unclipped it blows
    base.PAYLOAD_MAX_BYTES, and encode_payload REFUSES the write - so the
    link fails outright rather than showing a truncated title."""
    monkeypatch.setattr(
        lastfm,
        "_request",
        _responses(
            top=[{"name": "Y" * 20000, "playcount": "1"}],
            track={"name": "X" * 20000, "artist": {"#text": "Z" * 20000}},
        ),
    )
    result = await lastfm.LastFmConnector().link(USER, "Yanis")
    assert len(result.payload["last_track"]["name"]) <= lastfm._NAME_MAX
    assert len(result.payload["last_track"]["artist"]) <= lastfm._NAME_MAX
    assert len(result.payload["top_artists"][0]["name"]) <= lastfm._NAME_MAX
    encoded = base.encode_payload("lastfm", result.payload)
    assert len(encoded.encode("utf-8")) <= base.PAYLOAD_MAX_BYTES


@pytest.mark.parametrize(
    "url",
    (
        "javascript:alert(1)",
        "//protocol-relative.example/x.png",
        "/relative.png",
        "https://lastfm.example/" + "a" * 400,
    ),
)
def test_an_unusable_avatar_url_never_reaches_the_card(url):
    """Discord rejects the WHOLE message over a Thumbnail url it cannot
    fetch, at send time - past the point where views.render_sections can
    still fall back to the badge."""
    info = lastfm.parse_info({"user": {"name": "n", "image": [{"#text": url}]}})
    assert info["avatar"] is None


def test_the_largest_USABLE_image_is_picked_over_a_later_unusable_one():
    info = lastfm.parse_info(
        {
            "user": {
                "name": "n",
                "image": [
                    {"size": "small", "#text": "https://lastfm/small.png"},
                    {"size": "extralarge", "#text": "javascript:alert(1)"},
                ],
            }
        }
    )
    assert info["avatar"] == "https://lastfm/small.png"


async def test_the_renderer_drops_an_unusable_avatar_from_a_stored_payload():
    container = discord.ui.Container()
    connection = {"payload": {"avatar": "javascript:alert(1)", "playcount": 3}}
    await lastfm._render(container, _Field(), None, connection, budget=None)
    assert not any(
        isinstance(item, discord.ui.Section) for item in container.children
    )
    assert "3 scrobbles" in _text(container)


async def test_the_renderer_says_one_scrobble_in_the_singular():
    container = discord.ui.Container()
    await lastfm._render(
        container, _Field(), None, {"payload": {"playcount": 1}}, budget=None
    )
    assert "1 scrobble" in _text(container)
    assert "1 scrobbles" not in _text(container)


@pytest.mark.parametrize("code", (10, 26))
async def test_request_maps_a_key_error_to_not_configured(monkeypatch, code):
    """Codes 10 (invalid key) and 26 (suspended key) are the BOT's problem,
    not the member's: same answer as a key that was never provisioned, so an
    admin reads the right message instead of 'Last.fm is having a bad day'."""
    monkeypatch.setattr(lastfm, "_api_key", lambda: "stale-key")
    session = _FakeSession({"error": code, "message": "Invalid API key"})
    monkeypatch.setattr(lastfm, "_get_session", _async_return(session))
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await lastfm._request("user.getinfo", "someone")
    assert caught.value.reason == "not_configured"


async def test_request_maps_a_string_error_code_the_same_way(monkeypatch):
    """Last.fm has shipped its error code as a JSON string more than once."""
    monkeypatch.setattr(lastfm, "_api_key", lambda: "test-key")
    session = _FakeSession({"error": "6", "message": "no"})
    monkeypatch.setattr(lastfm, "_get_session", _async_return(session))
    with pytest.raises(base.InvalidHandle) as caught:
        await lastfm._request("user.getinfo", "ghost")
    assert caught.value.reason == "not_found"


def test_a_single_element_collection_arrives_as_an_object_not_a_list():
    """Last.fm's JSON collapses a one-element collection into the object
    itself - the exact shape an account with one top artist, or a scrobbler
    with a single track in the window, produces."""
    artists = lastfm.parse_top_artists(
        {"topartists": {"artist": {"name": "Solo", "playcount": "7"}}}
    )
    assert artists == [{"name": "Solo", "playcount": 7}]
    track = lastfm.parse_recent_track(
        {"recenttracks": {"track": {"name": "Song", "artist": {"#text": "Band"}}}}
    )
    assert track == {"artist": "Band", "name": "Song", "nowplaying": False}

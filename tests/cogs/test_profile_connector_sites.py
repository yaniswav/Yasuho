"""Tests for the P4A profile connectors (AniList, Steam, osu!) and the
auto-discovery / lazy-refresh scaffolding this lot adds around them.

Offline throughout: every aiohttp session is a fake (the same
request/response shape as tests/cogs/test_anilist_http.py), so no test here
ever reaches a real network, database or Discord.
"""

import asyncio
import importlib
import sys
import types

import pytest

from cogs.community.profile import cog as profile_cog
from cogs.community.profile import views as profile_views
from cogs.community.profile.connectors import (
    anilist,
    base,
    osu,
    sessions,
    steam,
)
from cogs.community.profile.connectors import (
    backloggd as backloggd_module,
)
from tools import cooldowns as cooldowns_module

# ---------------------------------------------------------------------------
# Shared aiohttp fakes (same shape as tests/cogs/test_anilist_http.py)
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, **kwargs):
        return self._payload


class _Request:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Session:
    """Records every GET/POST; replays responses from a queue (or one fixed
    response, when the test does not care about call-by-call ordering)."""

    closed = False

    def __init__(self, response=None, responses=None):
        self._responses = list(responses) if responses is not None else None
        self._fixed = response
        self.calls = []

    def _next(self, url):
        if self._responses is not None:
            return self._responses.pop(0)
        return self._fixed

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Request(self._next(url))

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _Request(self._next(url))


class _BrokenSession:
    """Every call raises - simulates a hung/failed connection."""

    closed = False

    def get(self, *args, **kwargs):
        raise ConnectionError("boom")

    def post(self, *args, **kwargs):
        raise ConnectionError("boom")


class _HostileSession:
    """A session every renderer test installs: touching it at all is the
    failure. The renderer contract is "draw from connection['payload']", so a
    renderer that reaches for the network must not merely be slow, it must be
    caught."""

    closed = False

    def get(self, *args, **kwargs):
        raise AssertionError("a renderer must not touch the network")

    def post(self, *args, **kwargs):
        raise AssertionError("a renderer must not touch the network")


class _Container:
    """The smallest thing a section renderer needs: something to add items to."""

    def __init__(self):
        self.items = []

    def add_item(self, item):
        self.items.append(item)

    @property
    def item(self):
        return self.items[-1]

    @property
    def text(self):
        """The last item's text, whether it was added bare or wrapped in a
        Section with an avatar thumbnail (which is what every connector does
        when the payload carries one)."""
        item = self.items[-1]
        content = getattr(item, "content", None)
        if content is not None:
            return content
        return "\n".join(
            child.content
            for child in item.children
            if getattr(child, "content", None) is not None
        )


@pytest.fixture(autouse=True)
def _reset_module_sessions():
    """Every connector takes its session from the package-wide registry
    (connectors/sessions.py); make sure one test's fake session cannot leak
    into the next.

    The REGISTERED AniList connector is process-wide too, and building a
    Profiles cog binds a throwaway bot onto it (that is the whole point of
    bind_bot), so its binding is put back as well - a fake bot outliving its
    test would be a booby trap for every file that runs after this one."""
    bound = base.CONNECTORS["anilist"]._bot
    sessions._SESSIONS.clear()
    yield
    sessions._SESSIONS.clear()
    base.CONNECTORS["anilist"]._bot = bound


def _patch_session(monkeypatch, module, session):
    async def _get_session():
        return session

    monkeypatch.setattr(module, "_get_session", _get_session)
    return session


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


def test_every_p4_connector_is_registered_by_plain_import():
    """Importing the package is the ONLY wiring: every connector module that
    sits in it must end up registered, and nothing that is not a reserved
    LINKABLE name may.

    The expectation is derived from what is on disk rather than spelled out,
    so this keeps testing the discovery MECHANISM (which P4B's modules rely on
    too) instead of a hardcoded roster that would have to be edited every time
    a connector lands or leaves.
    """
    import pkgutil

    from cogs.community.profile import connectors as connectors_pkg

    on_disk = {
        info.name
        for info in pkgutil.iter_modules(connectors_pkg.__path__)
        if not info.name.startswith("_")
        and info.name not in connectors_pkg._FRAMEWORK_MODULES
    }
    assert on_disk  # a discovery test over an empty package proves nothing
    assert {"anilist", "steam", "osu"} <= on_disk  # this lot's own three
    assert on_disk <= set(base.CONNECTORS)
    assert set(base.CONNECTORS) <= set(base.LINKABLE)


def test_every_p4_connector_has_a_section_renderer():
    for name in ("anilist", "steam", "osu"):
        assert name in profile_views.SECTION_RENDERERS


def test_a_broken_connector_module_does_not_take_the_others_down(monkeypatch):
    """importlib.import_module raising for one module must not stop discovery.

    The fake is swapped onto the PACKAGE's own ``importlib`` name rather than
    onto the stdlib module object: patching the real ``importlib.import_module``
    would redirect every import in the whole process for the duration of the
    test, including ones pytest itself makes.
    """
    from cogs.community.profile import connectors as connectors_pkg

    real_import = importlib.import_module
    calls = []

    def _flaky(name, package=None):
        if name == ".steam":
            calls.append(name)
            raise RuntimeError("simulated import failure")
        return real_import(name, package)

    monkeypatch.setattr(
        connectors_pkg, "importlib", types.SimpleNamespace(import_module=_flaky)
    )
    connectors_pkg._discover()
    assert calls == [".steam"]
    # anilist and osu still made it through this second discovery pass.
    assert "anilist" in base.CONNECTORS
    assert "osu" in base.CONNECTORS


# ---------------------------------------------------------------------------
# AniList
# ---------------------------------------------------------------------------


_ANILIST_USER = {
    "data": {
        "User": {
            "name": "Yanis",
            "avatar": {"large": "https://example.test/avatar.png"},
            "statistics": {
                "anime": {"count": 120, "meanScore": 78, "minutesWatched": 60000},
                "manga": {"count": 10, "meanScore": 82, "chaptersRead": 500},
            },
            "favourites": {
                "anime": {"nodes": [{"title": {"romaji": "Made in Abyss"}}]},
                "manga": {"nodes": [{"title": {"romaji": "Berserk"}}]},
            },
        }
    }
}


async def test_anilist_link_happy_path(monkeypatch):
    session = _patch_session(monkeypatch, anilist, _Session(_Response(200, _ANILIST_USER)))
    connector = anilist.AniListConnector()

    result = await connector.link(1, "  Yanis  ")

    assert result.external_id == "Yanis"
    assert result.display_name is None
    assert result.payload["anime_count"] == 120
    assert result.payload["favourite_anime"] == ["Made in Abyss"]
    assert result.payload["favourite_manga"] == ["Berserk"]
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert kwargs["json"]["variables"] == {"name": "Yanis"}


async def test_anilist_link_not_found_on_null_user(monkeypatch):
    _patch_session(monkeypatch, anilist, _Session(_Response(200, {"data": {"User": None}})))
    connector = anilist.AniListConnector()
    with pytest.raises(base.InvalidHandle) as caught:
        await connector.link(1, "ghost")
    assert caught.value.reason == "not_found"


async def test_anilist_link_not_found_on_http_404(monkeypatch):
    _patch_session(monkeypatch, anilist, _Session(_Response(404, {})))
    connector = anilist.AniListConnector()
    with pytest.raises(base.InvalidHandle) as caught:
        await connector.link(1, "ghost")
    assert caught.value.reason == "not_found"


async def test_anilist_link_refuses_an_empty_handle():
    connector = anilist.AniListConnector()
    with pytest.raises(base.InvalidHandle) as caught:
        await connector.link(1, "   ")
    assert caught.value.reason == "format"


async def test_anilist_service_down_raises_unavailable(monkeypatch):
    _patch_session(monkeypatch, anilist, _BrokenSession())
    connector = anilist.AniListConnector()
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await connector.link(1, "Yanis")
    assert caught.value.reason == "remote"


async def test_anilist_429_counts_on_the_shared_throttle_and_never_hits_the_wire(
    monkeypatch,
):
    session = _patch_session(monkeypatch, anilist, _Session(_Response(429, {})))
    connector = anilist.AniListConnector()

    class _Throttle:
        def __init__(self):
            self.notes = 0

        def allow_global(self):
            return True

        def note_throttled(self):
            self.notes += 1

    throttle = _Throttle()
    fake_bot = types.SimpleNamespace(get_cog=lambda name: types.SimpleNamespace(_throttle=throttle))
    connector.bind_bot(fake_bot)

    with pytest.raises(base.ConnectorUnavailable):
        await connector.link(1, "Yanis")
    assert throttle.notes == 1
    assert len(session.calls) == 1  # the 429 itself did go out; a SECOND call must not


async def test_anilist_throttle_ceiling_drops_the_call_before_the_wire(monkeypatch):
    session = _patch_session(monkeypatch, anilist, _Session(_Response(200, _ANILIST_USER)))
    connector = anilist.AniListConnector()

    class _Throttle:
        def allow_global(self):
            return False

        def note_throttled(self):
            raise AssertionError("must not be called when the ceiling drops the request")

    fake_bot = types.SimpleNamespace(get_cog=lambda name: types.SimpleNamespace(_throttle=_Throttle()))
    connector.bind_bot(fake_bot)

    with pytest.raises(base.ConnectorUnavailable):
        await connector.link(1, "Yanis")
    assert session.calls == []  # dropped before reaching the wire


async def test_the_connector_consumes_the_pollers_real_throttle_window(monkeypatch):
    """Not a look-alike window: the REAL AniListThrottle, reached the way
    feed_delivery reaches it (``bot.get_cog('AniList')._throttle``).

    Two claims at once - a connector call really spends a slot of the shared
    process-wide ceiling, and once that ceiling is spent the connector is
    refused BEFORE the wire, which is the whole point of sharing it with the
    airing / feed / chapter pollers rather than opening a second window.
    """
    from cogs.anilist.throttle import GLOBAL_LIMIT, AniListThrottle

    throttle = AniListThrottle()
    session = _patch_session(monkeypatch, anilist, _Session(_Response(200, _ANILIST_USER)))
    connector = anilist.AniListConnector()
    connector.bind_bot(
        types.SimpleNamespace(
            get_cog=lambda name: types.SimpleNamespace(_throttle=throttle)
            if name == "AniList"
            else None
        )
    )

    # One link = exactly one slot off the shared window.
    for _slot in range(GLOBAL_LIMIT - 1):
        assert throttle.allow_global() is True
    await connector.link(1, "Yanis")
    assert throttle.allow_global() is False
    assert len(session.calls) == 1

    # ... and the connector is now refused without touching the network.
    with pytest.raises(base.ConnectorUnavailable):
        await connector.link(1, "Yanis")
    assert len(session.calls) == 1


async def test_anilist_unbound_bot_never_blocks(monkeypatch):
    """No bind_bot call at all (an isolated connector) must still work."""
    _patch_session(monkeypatch, anilist, _Session(_Response(200, _ANILIST_USER)))
    connector = anilist.AniListConnector()
    result = await connector.link(1, "Yanis")
    assert result.external_id == "Yanis"


async def test_anilist_refresh_never_erases_the_payload_on_a_bad_answer(monkeypatch):
    _patch_session(monkeypatch, anilist, _Session(_Response(200, {"data": {"User": None}})))
    connector = anilist.AniListConnector()
    with pytest.raises(base.ConnectorUnavailable):
        await connector.refresh(1, {"external_id": "Yanis"})


async def test_anilist_renderer_reads_the_payload_only_zero_session_calls(monkeypatch):
    """The renderer contract: draw from connection['payload'], never the network."""
    monkeypatch.setitem(sessions._SESSIONS, "anilist", _HostileSession())
    field = types.SimpleNamespace(label="AniList")
    container = _Container()
    connection = {
        "payload": {
            "anime_count": 5,
            "anime_mean_score": 80,
            "anime_minutes_watched": 600,
            "manga_count": 0,
            "favourite_anime": ["A", "B"],
            "favourite_manga": [],
            "avatar": None,
        }
    }
    await anilist._render(container, field, None, connection, None)
    assert "AniList" in container.item.content
    assert "A, B" in container.item.content


async def test_anilist_renderer_names_the_account_when_there_is_no_stat_yet():
    """A brand-new AniList account must not render as a bold heading over
    nothing."""
    field = types.SimpleNamespace(label="AniList")
    container = _Container()
    connection = {"external_id": "Yanis", "display_name": None, "payload": {}}
    await anilist._render(container, field, None, connection, None)
    assert "Yanis" in container.item.content


async def test_anilist_drops_a_hostile_avatar_url_rather_than_truncating_it():
    """An over-long url is DROPPED, never clipped: half a url is exactly the
    Thumbnail Discord refuses the whole message over."""
    payload = anilist._build_payload(
        {"avatar": {"large": "https://example.test/" + "x" * 5000}}
    )
    assert payload["avatar"] is None
    encoded = base.encode_payload("anilist", payload)
    assert len(encoded.encode("utf-8")) <= base.PAYLOAD_MAX_BYTES


# ---------------------------------------------------------------------------
# Steam
# ---------------------------------------------------------------------------

_STEAMID = "76561197960265728"


def _summary(visible=True):
    return {
        "personaname": "Yanis",
        "avatarfull": "https://example.test/steam.png",
        "communityvisibilitystate": 3 if visible else 1,
    }


async def test_steam_link_with_a_raw_steamid64(monkeypatch):
    responses = [
        _Response(200, {"response": {"players": [_summary()]}}),
        _Response(200, {"response": {"games": [{"name": "Portal 2", "playtime_2weeks": 120}]}}),
        _Response(200, {"response": {"game_count": 42}}),
    ]
    session = _patch_session(monkeypatch, steam, _Session(responses=responses))
    connector = steam.SteamConnector()
    connector._api_key = lambda: "test-key"

    result = await connector.link(1, _STEAMID)

    assert result.external_id == _STEAMID
    assert result.display_name == "Yanis"
    assert result.payload["owned_games_count"] == 42
    assert result.payload["recent_games"][0]["name"] == "Portal 2"
    # Vanity resolution is skipped entirely for a raw SteamID64.
    assert len(session.calls) == 3
    assert "ResolveVanityURL" not in session.calls[0][1]


async def test_steam_link_resolves_a_vanity_name(monkeypatch):
    responses = [
        _Response(200, {"response": {"success": 1, "steamid": _STEAMID}}),
        _Response(200, {"response": {"players": [_summary()]}}),
        _Response(200, {"response": {"games": []}}),
        _Response(200, {"response": {"game_count": 0}}),
    ]
    session = _patch_session(monkeypatch, steam, _Session(responses=responses))
    connector = steam.SteamConnector()
    connector._api_key = lambda: "test-key"

    result = await connector.link(1, "yaniswav")

    assert result.external_id == _STEAMID
    assert "ResolveVanityURL" in session.calls[0][1]


async def test_steam_link_reports_not_found_for_an_unknown_vanity(monkeypatch):
    _patch_session(
        monkeypatch,
        steam,
        _Session(_Response(200, {"response": {"success": 42, "message": "No match"}})),
    )
    connector = steam.SteamConnector()
    connector._api_key = lambda: "test-key"
    with pytest.raises(base.InvalidHandle) as caught:
        await connector.link(1, "totally-fake-user")
    assert caught.value.reason == "not_found"


async def test_steam_link_refuses_a_malformed_handle():
    connector = steam.SteamConnector()
    connector._api_key = lambda: "test-key"
    with pytest.raises(base.InvalidHandle) as caught:
        await connector.link(1, "!!not valid!!")
    assert caught.value.reason == "format"


async def test_steam_link_private_profile_marks_the_payload_and_skips_game_calls(
    monkeypatch,
):
    session = _patch_session(
        monkeypatch,
        steam,
        _Session(_Response(200, {"response": {"players": [_summary(visible=False)]}})),
    )
    connector = steam.SteamConnector()
    connector._api_key = lambda: "test-key"

    result = await connector.link(1, _STEAMID)

    assert result.payload["private"] is True
    assert "recent_games" not in result.payload
    assert "owned_games_count" not in result.payload
    # Only the summary call - no GetRecentlyPlayedGames / GetOwnedGames.
    assert len(session.calls) == 1


async def test_steam_missing_api_key_is_not_configured(monkeypatch):
    """No [APITokens] steamKey (the default, no-admin-setup-yet state) is a
    typed 'not_configured', decided at CALL time.

    The key is read lazily on purpose: this module is imported by the
    connectors package on every boot, including on a machine whose config/
    holds no tokens.ini at all, so reading it at import (or in __init__) would
    turn a missing key into a connector that never registers. Constructing the
    connector under a config that raises is half the assertion here; the whole
    test suite importing this module is the other half."""
    from tools.config_loader import config_loader

    def _boom(section, option):
        raise Exception("no such option")

    monkeypatch.setattr(config_loader, "getstr", _boom)
    connector = steam.SteamConnector()  # construction must not read the key
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await connector.link(1, _STEAMID)
    assert caught.value.reason == "not_configured"


@pytest.mark.parametrize(
    "handle",
    [
        "https://steamcommunity.com/id/yaniswav",
        "https://steamcommunity.com/id/yaniswav/",
        "steamcommunity.com/id/yaniswav",
        "https://www.steamcommunity.com/id/yaniswav",
    ],
)
async def test_steam_accepts_a_pasted_profile_url(monkeypatch, handle):
    """What the Steam client puts in the clipboard must not be refused."""
    responses = [
        _Response(200, {"response": {"success": 1, "steamid": _STEAMID}}),
        _Response(200, {"response": {"players": [_summary()]}}),
        _Response(200, {"response": {"games": []}}),
        _Response(200, {"response": {"game_count": 0}}),
    ]
    session = _patch_session(monkeypatch, steam, _Session(responses=responses))
    connector = steam.SteamConnector()
    connector._api_key = lambda: "test-key"

    result = await connector.link(1, handle)

    assert result.external_id == _STEAMID
    assert session.calls[0][2]["params"]["vanityurl"] == "yaniswav"


async def test_steam_accepts_a_pasted_numeric_profile_url(monkeypatch):
    responses = [
        _Response(200, {"response": {"players": [_summary()]}}),
        _Response(200, {"response": {"games": []}}),
        _Response(200, {"response": {"game_count": 0}}),
    ]
    session = _patch_session(monkeypatch, steam, _Session(responses=responses))
    connector = steam.SteamConnector()
    connector._api_key = lambda: "test-key"

    result = await connector.link(1, "https://steamcommunity.com/profiles/" + _STEAMID)

    assert result.external_id == _STEAMID
    # No vanity resolution: the URL already carried the id.
    assert "ResolveVanityURL" not in session.calls[0][1]


async def test_steam_survives_junk_where_a_number_was_promised(monkeypatch):
    """Steam sends playtime as an int and game_count as an int - but nothing
    in this bot enforces that, and a TypeError halfway through a refresh would
    surface as the generic failure message for an account that is fine."""
    responses = [
        _Response(200, {"response": {"players": [_summary()]}}),
        _Response(
            200,
            {
                "response": {
                    "games": [{"name": "Portal 2", "playtime_2weeks": "lots"}]
                }
            },
        ),
        _Response(200, {"response": {"game_count": None}}),
    ]
    _patch_session(monkeypatch, steam, _Session(responses=responses))
    connector = steam.SteamConnector()
    connector._api_key = lambda: "test-key"

    result = await connector.link(1, _STEAMID)

    assert result.payload["recent_games"][0]["hours_2weeks"] == 0
    assert result.payload["owned_games_count"] is None
    # Nothing exponent-shaped can reach the payload either (see schema.sql's
    # payload-size CHECK on canonicalised numbers).
    assert "e+" not in base.encode_payload("steam", result.payload)


def test_absurd_numbers_never_reach_a_payload_in_exponent_form():
    """The one way base.encode_payload can under-measure what Postgres will
    store: a float big enough for exponent notation."""
    assert steam._count(10**30) is None
    assert osu._number("1e300", 1) is None
    steam_payload = steam._build_payload(
        _summary(),
        False,
        [{"name": "x", "hours_2weeks": steam._hours(1e300)}],
        steam._count(10**30),
    )
    osu_payload = osu._build_payload(
        {"username": "peppy", "pp_raw": "1e300", "user_id": "2"}
    )
    for name, payload in (("steam", steam_payload), ("osu", osu_payload)):
        assert "e+" not in base.encode_payload(name, payload)


async def test_steam_clips_a_hostile_persona_name_and_drops_its_avatar():
    payload = steam._build_payload(
        {
            "personaname": "p" * 5000,
            "avatarfull": "https://example.test/" + "x" * 5000,
            "communityvisibilitystate": 3,
        },
        False,
        [],
        0,
    )
    assert len(payload["persona_name"]) == steam._PERSONA_CLIP
    # The url is DROPPED, not clipped - see base.safe_url.
    assert payload["avatar"] is None
    encoded = base.encode_payload("steam", payload)
    assert len(encoded.encode("utf-8")) <= base.PAYLOAD_MAX_BYTES


async def test_steam_service_down_raises_unavailable(monkeypatch):
    _patch_session(monkeypatch, steam, _BrokenSession())
    connector = steam.SteamConnector()
    connector._api_key = lambda: "test-key"
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await connector.link(1, _STEAMID)
    assert caught.value.reason == "remote"


async def test_steam_renderer_reads_the_payload_only(monkeypatch):
    monkeypatch.setitem(sessions._SESSIONS, "steam", _HostileSession())
    field = types.SimpleNamespace(label="Steam")
    container = _Container()
    connection = {"payload": {"private": True, "avatar": None}}
    await steam._render(container, field, None, connection, None)
    assert "private" in container.item.content.lower()


async def test_steam_renderer_names_a_public_profile_with_hidden_game_details():
    """Public summary, but Steam's separate game-details flag hid the rest:
    the section must still say WHO, not stand as a bold heading over nothing."""
    field = types.SimpleNamespace(label="Steam")
    container = _Container()
    connection = {
        "external_id": _STEAMID,
        "display_name": "Yanis",
        "payload": {"private": False, "persona_name": "Yanis", "avatar": None},
    }
    await steam._render(container, field, None, connection, None)
    assert "Yanis" in container.item.content


# ---------------------------------------------------------------------------
# osu!
# ---------------------------------------------------------------------------

_OSU_USER = [
    {
        "username": "peppy",
        "pp_rank": "1",
        "pp_raw": "12345.6",
        "accuracy": "99.12",
        "level": "100.5",
        "country": "AU",
        "user_id": "2",
    }
]


async def test_osu_link_happy_path(monkeypatch):
    session = _patch_session(monkeypatch, osu, _Session(_Response(200, _OSU_USER)))
    connector = osu.OsuConnector()
    connector._api_key = lambda: "test-key"

    result = await connector.link(1, "peppy")

    # The numeric id is what is stored (it survives a rename); the username is
    # kept as the display name so nothing shows a bare number.
    assert result.external_id == "2"
    assert result.display_name == "peppy"
    assert result.payload["rank"] == 1
    assert result.payload["pp"] == 12345.6
    assert result.payload["accuracy"] == 99.12
    assert result.payload["avatar"] == "https://a.ppy.sh/2"
    method, url, kwargs = session.calls[0]
    # No `type` on a freshly typed handle: v1 decides name-or-id itself.
    assert kwargs["params"] == {"k": "test-key", "u": "peppy"}


async def test_osu_refresh_looks_the_account_up_by_id_not_by_name(monkeypatch):
    """The rename guard: a stored numeric id is refreshed with type=id, so a
    user who renames does not silently break their own section."""
    session = _patch_session(monkeypatch, osu, _Session(_Response(200, _OSU_USER)))
    connector = osu.OsuConnector()
    connector._api_key = lambda: "test-key"

    await connector.refresh(1, {"external_id": "2"})

    assert session.calls[0][2]["params"] == {"k": "test-key", "u": "2", "type": "id"}


async def test_osu_refresh_of_a_non_numeric_handle_stays_a_name_lookup(monkeypatch):
    session = _patch_session(monkeypatch, osu, _Session(_Response(200, _OSU_USER)))
    connector = osu.OsuConnector()
    connector._api_key = lambda: "test-key"

    await connector.refresh(1, {"external_id": "peppy"})

    assert "type" not in session.calls[0][2]["params"]


async def test_osu_garbage_numbers_never_reach_the_payload(monkeypatch):
    """v1 hands every number back as a STRING; anything unparseable becomes
    None here rather than an exception in the renderer later."""
    payload = osu._build_payload(
        {
            "username": "peppy",
            "pp_rank": "not-a-number",
            "pp_raw": None,
            "accuracy": "",
            "level": "100.5555",
            "country": None,
            "user_id": "2",
        }
    )
    assert payload["rank"] is None
    assert payload["pp"] is None
    assert payload["accuracy"] is None
    assert payload["level"] == 100.6

    # Nothing renderable is left, so the section names the account instead of
    # standing as a bold heading over nothing - and nothing raised on the way.
    field = types.SimpleNamespace(label="osu!")
    container = _Container()
    await osu._render(container, field, None, {"payload": payload}, None)
    assert "peppy" in container.text


async def test_osu_link_not_found_on_empty_array(monkeypatch):
    _patch_session(monkeypatch, osu, _Session(_Response(200, [])))
    connector = osu.OsuConnector()
    connector._api_key = lambda: "test-key"
    with pytest.raises(base.InvalidHandle) as caught:
        await connector.link(1, "ghost")
    assert caught.value.reason == "not_found"


async def test_osu_missing_key_is_not_configured(monkeypatch):
    from tools.config_loader import config_loader

    def _boom(section, option):
        raise Exception("no such option")

    monkeypatch.setattr(config_loader, "getstr", _boom)
    connector = osu.OsuConnector()
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await connector.link(1, "peppy")
    assert caught.value.reason == "not_configured"


async def test_osu_service_down_raises_unavailable(monkeypatch):
    _patch_session(monkeypatch, osu, _BrokenSession())
    connector = osu.OsuConnector()
    connector._api_key = lambda: "test-key"
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await connector.link(1, "peppy")
    assert caught.value.reason == "remote"


async def test_osu_renderer_reads_the_payload_only(monkeypatch):
    monkeypatch.setitem(sessions._SESSIONS, "osu", _HostileSession())
    field = types.SimpleNamespace(label="osu!")
    container = _Container()
    connection = {
        "payload": {
            "rank": 1,
            "pp": 100.0,
            "accuracy": 99.5,
            "level": 100,
            "country": "FR",
        }
    }
    await osu._render(container, field, None, connection, None)
    assert "FR" in container.item.content


# ---------------------------------------------------------------------------
# The payload cap, respected by every P4A connector alike (framework-owned,
# consumed here rather than restated).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        anilist._build_payload(_ANILIST_USER["data"]["User"]),
    ],
)
def test_anilist_payload_encodes_under_the_shared_cap(payload):
    encoded = base.encode_payload("anilist", payload)
    assert len(encoded.encode("utf-8")) <= base.PAYLOAD_MAX_BYTES


def test_steam_payload_encodes_under_the_shared_cap():
    payload = steam._build_payload(
        _summary(),
        False,
        [{"name": "Portal 2", "hours_2weeks": 2.0}],
        42,
    )
    encoded = base.encode_payload("steam", payload)
    assert len(encoded.encode("utf-8")) <= base.PAYLOAD_MAX_BYTES


def test_osu_payload_encodes_under_the_shared_cap():
    payload = osu._build_payload(_OSU_USER[0])
    encoded = base.encode_payload("osu", payload)
    assert len(encoded.encode("utf-8")) <= base.PAYLOAD_MAX_BYTES


# ---------------------------------------------------------------------------
# The lazy-refresh scheduling hook added to cogs/community/profile/cog.py
# ---------------------------------------------------------------------------


def _profiles_cog():
    bot = types.SimpleNamespace(db_pool=object(), get_cog=lambda name: None)
    return profile_cog.Profiles(bot)


def test_profiles_init_binds_the_bot_on_connectors_that_want_it():
    cog = _profiles_cog()
    assert base.CONNECTORS["anilist"]._bot is cog.bot


def test_connector_ttl_reads_the_declaring_modules_own_constant():
    assert profile_cog._connector_ttl(base.CONNECTORS["anilist"]) == anilist.REFRESH_TTL_SECONDS
    assert profile_cog._connector_ttl(base.CONNECTORS["steam"]) == steam.REFRESH_TTL_SECONDS
    assert profile_cog._connector_ttl(base.CONNECTORS["osu"]) == osu.REFRESH_TTL_SECONDS


def test_connector_ttl_falls_back_when_a_module_declares_none():
    class _Bare:
        pass

    fake_module = types.ModuleType("fake_no_ttl_module")
    fake_module.SomeConnector = _Bare
    sys.modules["fake_no_ttl_module"] = fake_module
    try:
        instance = _Bare()
        assert (
            profile_cog._connector_ttl(instance)
            == profile_cog.DEFAULT_CONNECTOR_REFRESH_TTL
        )
    finally:
        del sys.modules["fake_no_ttl_module"]


async def test_schedule_stale_refreshes_skips_a_fresh_connection(monkeypatch):
    import datetime

    cog = _profiles_cog()
    now = datetime.datetime.now(datetime.timezone.utc)
    connections = [{"connector": "osu", "external_id": "peppy", "last_refresh": now}]
    cog._schedule_stale_refreshes(1, connections)
    assert cog._connector_tasks == set()


async def test_a_freshly_linked_row_is_not_refetched_on_the_very_next_view():
    """``link`` fetched and stored a payload seconds ago but leaves
    ``last_refresh`` NULL (the column means "last BACKGROUND refresh"). Reading
    ``linked_at`` as the fallback stamp is what stops every single link from
    paying for a second, pointless round trip."""
    import datetime

    cog = _profiles_cog()
    now = datetime.datetime.now(datetime.timezone.utc)
    connections = [
        {
            "connector": "osu",
            "external_id": "2",
            "linked_at": now,
            "last_refresh": None,
        }
    ]
    cog._schedule_stale_refreshes(1, connections)
    assert cog._connector_tasks == set()


async def test_a_naive_timestamp_cannot_take_the_card_down():
    """asyncpg cannot hand back a naive datetime for a TIMESTAMPTZ, but
    subtracting one would raise TypeError inside `/profile view` - a
    hand-edited row is read as UTC instead."""
    import datetime

    cog = _profiles_cog()
    connections = [
        {
            "connector": "osu",
            "external_id": "2",
            "last_refresh": datetime.datetime.now(datetime.timezone.utc).replace(
                tzinfo=None
            ),  # naive on purpose
        }
    ]
    cog._schedule_stale_refreshes(1, connections)
    assert cog._connector_tasks == set()


async def test_a_malformed_row_is_skipped_rather_than_breaking_the_scheduling():
    cog = _profiles_cog()
    cog._schedule_stale_refreshes(1, [object(), {"nope": 1}])
    assert cog._connector_tasks == set()


async def test_schedule_stale_refreshes_kicks_off_a_task_for_a_stale_connection(
    monkeypatch,
):
    import datetime

    cog = _profiles_cog()
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    connections = [{"connector": "osu", "external_id": "peppy", "last_refresh": old}]

    async def fake_refresh(user_id, connection):
        return {"rank": 1}

    stored = []

    async def fake_set_payload(pool, user_id, connector, payload, display_name=None):
        stored.append((user_id, connector, payload))
        return True

    monkeypatch.setattr(base.CONNECTORS["osu"], "refresh", fake_refresh)
    monkeypatch.setattr(profile_cog.connectors_storage, "set_payload", fake_set_payload)

    cog._schedule_stale_refreshes(1, connections)
    assert len(cog._connector_tasks) == 1
    await next(iter(cog._connector_tasks))

    assert stored == [(1, "osu", {"rank": 1})]
    assert cog._connector_inflight == set()


async def test_schedule_stale_refreshes_never_spawns_a_second_task_for_the_same_pair():
    import datetime

    cog = _profiles_cog()
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    connections = [{"connector": "osu", "external_id": "peppy", "last_refresh": old}]
    cog._connector_inflight.add((1, "osu"))

    cog._schedule_stale_refreshes(1, connections)

    assert cog._connector_tasks == set()


async def test_a_failing_lazy_refresh_never_raises_out_of_the_task(monkeypatch):
    cog = _profiles_cog()

    async def boom(user_id, connection):
        raise RuntimeError("remote is down")

    monkeypatch.setattr(base.CONNECTORS["osu"], "refresh", boom)
    connections = [{"connector": "osu", "external_id": "peppy", "last_refresh": None}]

    cog._schedule_stale_refreshes(1, connections)
    task = next(iter(cog._connector_tasks))
    await task  # must not raise
    assert cog._connector_inflight == set()


async def test_a_not_linked_refresh_race_is_swallowed(monkeypatch):
    """The user unlinked while the refresh was in flight - storing must not
    resurrect the row, and the task must not raise."""
    cog = _profiles_cog()

    async def fake_refresh(user_id, connection):
        return {"rank": 1}

    async def fake_set_payload(pool, user_id, connector, payload, display_name=None):
        raise base.NotLinked(connector)

    monkeypatch.setattr(base.CONNECTORS["osu"], "refresh", fake_refresh)
    monkeypatch.setattr(profile_cog.connectors_storage, "set_payload", fake_set_payload)
    connections = [{"connector": "osu", "external_id": "peppy", "last_refresh": None}]

    cog._schedule_stale_refreshes(1, connections)
    task = next(iter(cog._connector_tasks))
    await task  # must not raise


async def test_an_expected_remote_failure_logs_a_line_not_a_stack_trace(
    monkeypatch, caplog
):
    """A third party being down is an EXPECTED outcome (a typed
    ConnectorError), so it must cost one warning line - not one traceback per
    viewer of every profile linked to it."""
    async def unavailable(user_id, connection):
        raise base.ConnectorUnavailable("osu", "remote")

    monkeypatch.setattr(base.CONNECTORS["osu"], "refresh", unavailable)
    cog = _profiles_cog()
    cog._schedule_stale_refreshes(1, [{"connector": "osu", "external_id": "2"}])
    with caplog.at_level("WARNING"):
        await next(iter(cog._connector_tasks))

    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert caplog.records[0].exc_info is None


async def test_a_failing_connector_is_not_re_attempted_on_every_single_view(
    monkeypatch,
):
    """THE storm guard. A failed refresh never stamps ``last_refresh``, so the
    TTL alone would re-attempt a dead third party on every view of a popular
    profile. The attempt floor is what makes that once per window instead."""
    attempts = []

    async def boom(user_id, connection):
        attempts.append(user_id)
        raise RuntimeError("remote is down")

    monkeypatch.setattr(base.CONNECTORS["osu"], "refresh", boom)
    cog = _profiles_cog()
    connections = [{"connector": "osu", "external_id": "2", "last_refresh": None}]

    for _view in range(20):
        cog._schedule_stale_refreshes(1, connections)
        for task in list(cog._connector_tasks):
            await task

    assert attempts == [1]

    # Someone ELSE's account is a different key and is not held back by it.
    cog._schedule_stale_refreshes(2, connections)
    for task in list(cog._connector_tasks):
        await task
    assert attempts == [1, 2]


async def test_the_in_flight_ceiling_caps_a_cold_cache_stampede(monkeypatch):
    """Many DIFFERENT members viewed at once (the minutes after a restart,
    when every cached payload is cold) is the one case the per-pair guards
    cannot bound - the process-wide ceiling is."""
    released = asyncio.Event()

    async def slow_refresh(user_id, connection):
        await released.wait()
        return {"rank": 1}

    async def fake_set_payload(pool, user_id, connector, payload, display_name=None):
        return True

    monkeypatch.setattr(base.CONNECTORS["osu"], "refresh", slow_refresh)
    monkeypatch.setattr(profile_cog.connectors_storage, "set_payload", fake_set_payload)
    cog = _profiles_cog()
    connections = [{"connector": "osu", "external_id": "2", "last_refresh": None}]

    for owner in range(profile_cog.MAX_CONNECTOR_REFRESHES_IN_FLIGHT + 5):
        cog._schedule_stale_refreshes(owner, connections)

    assert (
        len(cog._connector_tasks) == profile_cog.MAX_CONNECTOR_REFRESHES_IN_FLIGHT
    )
    released.set()
    for task in list(cog._connector_tasks):
        await task


# ---------------------------------------------------------------------------
# The shared url filter, applied on BOTH sides of the payload by every module
# (the parse decides what may be stored; the renderer re-decides, because the
# row it reads was written by a PAST version of the module).
# ---------------------------------------------------------------------------


_UNUSABLE_URLS = (
    "/relative/avatar.png",
    "javascript:alert(1)",
    "data:image/png;base64,AAAA",
    "//protocol-relative.example/x.png",
    "https://example.invalid/" + "a" * 500,
)


@pytest.mark.parametrize("url", _UNUSABLE_URLS)
def test_an_unusable_avatar_never_reaches_an_anilist_payload(url):
    assert anilist._build_payload({"avatar": {"large": url}})["avatar"] is None


@pytest.mark.parametrize("url", _UNUSABLE_URLS)
def test_an_unusable_avatar_never_reaches_a_steam_payload(url):
    summary = {"personaname": "Yanis", "avatarfull": url, "communityvisibilitystate": 3}
    assert steam._build_payload(summary, False, [], 0)["avatar"] is None


@pytest.mark.parametrize("module", (anilist, steam, osu))
@pytest.mark.parametrize("url", _UNUSABLE_URLS)
async def test_a_renderer_drops_an_unusable_avatar_from_a_stored_payload(module, url):
    """Discord rejects the WHOLE message over a Thumbnail it cannot fetch, at
    SEND time - past the point where views.render_sections can still fall back
    to the badge. A row written before this filter existed must not be able to
    take a card down."""
    field = types.SimpleNamespace(label="Section")
    container = _Container()
    connection = {
        "external_id": "2",
        "display_name": "Yanis",
        "payload": {"avatar": url, "rank": 1, "anime_count": 1, "private": True},
    }
    await module._render(container, field, None, connection, None)
    assert not any(hasattr(item, "accessory") for item in container.items)


@pytest.mark.parametrize("module", (anilist, steam, osu))
async def test_a_renderer_keeps_a_usable_avatar(module):
    """The counter-test to the one above: the filter must not be a blanket
    refusal, or every card would silently lose its thumbnail."""
    field = types.SimpleNamespace(label="Section")
    container = _Container()
    connection = {
        "external_id": "2",
        "display_name": "Yanis",
        "payload": {
            "avatar": "https://example.test/a.png",
            "rank": 1,
            "anime_count": 1,
            "private": True,
        },
    }
    await module._render(container, field, None, connection, None)
    assert any(hasattr(item, "accessory") for item in container.items)


# ---------------------------------------------------------------------------
# The shared magnitude bound, now applied by AniList too (it was the only P4A
# module storing a remote's numbers unchecked).
# ---------------------------------------------------------------------------


def test_an_absurd_anilist_number_never_reaches_the_payload():
    """A float big enough to need exponent form is written by json.dumps as
    ``1e+50`` and re-serialised by Postgres as its 51 digits: the one way
    base.encode_payload's count under-measures the schema CHECK."""
    payload = anilist._build_payload(
        {
            "statistics": {
                "anime": {
                    "count": 10**40,
                    "meanScore": float("inf"),
                    "minutesWatched": 1e50,
                },
                "manga": {"count": 3, "meanScore": 80, "chaptersRead": "nope"},
            }
        }
    )
    assert payload["anime_count"] is None
    assert payload["anime_mean_score"] is None
    assert payload["anime_minutes_watched"] is None
    assert payload["manga_chapters_read"] is None
    # ... and the plausible values are untouched.
    assert payload["manga_count"] == 3
    assert payload["manga_mean_score"] == 80
    assert "e+" not in base.encode_payload("anilist", payload)


# ---------------------------------------------------------------------------
# F9: the API key rides in the query string, so an exception is never logged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module, connector_class",
    ((steam, steam.SteamConnector), (osu, osu.OsuConnector)),
)
async def test_a_failed_request_logs_the_exception_type_not_the_exception(
    module, connector_class, monkeypatch, caplog
):
    """aiohttp puts the request URL in its exception message, and every URL
    here carries this bot's API key in its query string."""

    class _Leaky:
        closed = False

        def get(self, *args, **kwargs):
            raise ConnectionError(
                "Cannot connect to https://api.example/x?key=SUPERSECRETKEY"
            )

        def post(self, *args, **kwargs):
            return self.get()

    _patch_session(monkeypatch, module, _Leaky())
    connector = connector_class()
    connector._api_key = lambda: "SUPERSECRETKEY"
    with caplog.at_level("WARNING"):
        with pytest.raises(base.ConnectorUnavailable):
            await connector.link(1, "76561197960265728")

    assert caplog.records, "the failure must still be logged"
    for record in caplog.records:
        assert "SUPERSECRETKEY" not in record.getMessage()
    assert "ConnectionError" in caplog.records[-1].getMessage()


# ---------------------------------------------------------------------------
# F4: the retry floor DOUBLES per consecutive failure, up to the connector's
# own TTL - a permanently-dead account settles at the rate that connector
# already declared is polite for it.
# ---------------------------------------------------------------------------


def test_the_retry_interval_starts_at_the_flat_floor():
    cog = _profiles_cog()
    assert (
        cog._retry_interval(base.CONNECTORS["osu"], (1, "osu"))
        == profile_cog.CONNECTOR_REFRESH_MIN_INTERVAL
    )


def test_three_consecutive_failures_climb_the_retry_interval_towards_the_cap():
    cog = _profiles_cog()
    key = (1, "backloggd")
    implementation = base.CONNECTORS["backloggd"]
    seen = []
    for _failure in range(3):
        cog._connector_failures.record(key)
        seen.append(cog._retry_interval(implementation, key))

    base_floor = profile_cog.CONNECTOR_REFRESH_MIN_INTERVAL
    assert seen == [base_floor * 2, base_floor * 4, base_floor * 8]
    assert seen == sorted(seen)
    assert all(value > base_floor for value in seen)


def test_the_retry_interval_never_passes_the_connectors_own_ttl():
    """Backloggd's 12h scraping courtesy is the ceiling: the backoff walks up
    to the rate the connector already declares, never past it."""
    cog = _profiles_cog()
    key = (1, "backloggd")
    implementation = base.CONNECTORS["backloggd"]
    for _failure in range(40):
        cog._connector_failures.record(key)
    assert cog._retry_interval(implementation, key) == profile_cog._connector_ttl(
        implementation
    )


def test_a_short_ttl_can_never_shorten_the_flat_floor():
    """A hypothetical connector declaring a 60-second TTL must not be able to
    weaken the guard that protects it."""

    class _Impatient:
        pass

    fake_module = types.ModuleType("fake_short_ttl_module")
    fake_module.REFRESH_TTL_SECONDS = 60
    fake_module._Impatient = _Impatient
    _Impatient.__module__ = "fake_short_ttl_module"
    sys.modules["fake_short_ttl_module"] = fake_module
    try:
        cog = _profiles_cog()
        key = (1, "whatever")
        for _failure in range(5):
            cog._connector_failures.record(key)
        assert (
            cog._retry_interval(_Impatient(), key)
            == profile_cog.CONNECTOR_REFRESH_MIN_INTERVAL
        )
    finally:
        sys.modules.pop("fake_short_ttl_module", None)


async def test_a_success_resets_the_backoff(monkeypatch):
    outcomes = ["boom", "boom", "ok"]

    async def flaky(user_id, connection):
        if outcomes.pop(0) == "boom":
            raise base.ConnectorUnavailable("osu", "remote")
        return {"rank": 1}

    async def fake_set_payload(pool, user_id, connector, payload, display_name=None):
        return True

    monkeypatch.setattr(base.CONNECTORS["osu"], "refresh", flaky)
    monkeypatch.setattr(profile_cog.connectors_storage, "set_payload", fake_set_payload)
    cog = _profiles_cog()
    key = (1, "osu")
    connection = {"connector": "osu", "external_id": "2", "last_refresh": None}

    for _attempt in range(3):
        # Bypass the attempt floor itself; what is under test is the counter.
        cog._connector_attempts = profile_cog.Cooldowns(0)
        cog._schedule_stale_refreshes(1, [connection])
        for task in list(cog._connector_tasks):
            await task

    assert cog._connector_failures.count(key) == 0
    assert (
        cog._retry_interval(base.CONNECTORS["osu"], key)
        == profile_cog.CONNECTOR_REFRESH_MIN_INTERVAL
    )


async def test_a_dead_account_is_attempted_far_less_often_than_the_flat_floor(
    monkeypatch,
):
    """The whole point of F4, in numbers: with a flat 300s floor a permanently
    404 Backloggd handle is 288 scrapes a day; the backoff caps it at that
    connector's declared 12-hour courtesy window."""
    monkeypatch.setattr(
        base.CONNECTORS["backloggd"],
        "refresh",
        _always_not_found,
    )
    cog = _profiles_cog()
    key = (1, "backloggd")
    connection = {"connector": "backloggd", "external_id": "ghost", "last_refresh": None}

    for _view in range(8):
        cog._connector_attempts = profile_cog.Cooldowns(0)
        cog._schedule_stale_refreshes(1, [connection])
        for task in list(cog._connector_tasks):
            await task

    assert cog._connector_failures.count(key) == 8
    window = cog._retry_interval(base.CONNECTORS["backloggd"], key)
    assert window == backloggd_module.REFRESH_TTL_SECONDS
    assert 24 * 60 * 60 / window <= 2
    # ... where the flat floor alone would have been 288 a day.
    assert 24 * 60 * 60 / profile_cog.CONNECTOR_REFRESH_MIN_INTERVAL == 288


async def _always_not_found(user_id, connection):
    raise base.InvalidHandle("backloggd", "not_found")


async def test_a_sweep_cannot_reset_a_dead_handles_backoff(monkeypatch):
    """REGRESSION: the attempt map is shared by every (owner, connector) pair,
    so an entry seated under the flat 300s floor is the window the SWEEP judges
    it by, whatever backoff the pair had actually earned. A sweep tripped by
    unrelated traffic therefore evicted a key whose real window was Backloggd's
    12-hour courtesy and handed a permanently-dead handle its 288 scrapes a day
    straight back - the exact number F4 exists to kill. The scheduler now names
    the backoff window on touch as well as on the check."""
    calls = []

    async def _counting(user_id, connection):
        calls.append(user_id)
        raise base.InvalidHandle("backloggd", "not_found")

    monkeypatch.setattr(base.CONNECTORS["backloggd"], "refresh", _counting)
    clock = {"t": 0.0}
    monkeypatch.setattr(
        cooldowns_module, "time", types.SimpleNamespace(monotonic=lambda: clock["t"])
    )

    cog = _profiles_cog()
    key = (1, "backloggd")
    connection = {"connector": "backloggd", "external_id": "ghost", "last_refresh": None}
    # sweep_at=1 so the unrelated traffic below trips the sweep immediately.
    cog._connector_attempts = profile_cog.Cooldowns(
        profile_cog.CONNECTOR_REFRESH_MIN_INTERVAL, sweep_at=1
    )
    for _failure in range(40):
        cog._connector_failures.record(key)
    window = cog._retry_interval(base.CONNECTORS["backloggd"], key)
    assert window == backloggd_module.REFRESH_TTL_SECONDS  # 12h, not the 300s floor

    cog._schedule_stale_refreshes(1, [connection])
    for task in list(cog._connector_tasks):
        await task
    await asyncio.sleep(0)  # let the done callbacks clear the inflight guard
    assert calls == [1]
    assert cog._connector_inflight == set()  # the refusal below is the COOLDOWN

    # Unrelated traffic an hour later: past the 300s floor, deep inside the 12h.
    clock["t"] = 3600.0
    cog._connector_attempts.touch(("other", "osu"))
    cog._connector_attempts.touch(("other", "lastfm"))

    assert cog._connector_attempts.is_active(key, seconds=window) is True
    cog._schedule_stale_refreshes(1, [connection])
    for task in list(cog._connector_tasks):
        await task
    assert calls == [1]  # not scraped again


def test_the_failure_map_prunes_itself():
    """Bounded in memory: a process that views a million profiles must not
    accumulate one entry per pair that ever failed."""
    failures = profile_cog._ConnectorFailures(60, sweep_at=10)
    for index in range(50):
        # Ten seconds apart, against a 60-second horizon: everything past it
        # can no longer lengthen any window, so the sweep drops it.
        failures.record((index, "osu"), now=index * 10)
    assert len(failures) <= 11


# ---------------------------------------------------------------------------
# F5: the lifecycle - nothing this cog started outlives it
# ---------------------------------------------------------------------------


async def test_cog_unload_cancels_the_in_flight_refreshes(monkeypatch):
    released = asyncio.Event()

    async def slow_refresh(user_id, connection):
        await released.wait()
        return {"rank": 1}

    monkeypatch.setattr(base.CONNECTORS["osu"], "refresh", slow_refresh)
    cog = _profiles_cog()
    cog._schedule_stale_refreshes(
        1, [{"connector": "osu", "external_id": "2", "last_refresh": None}]
    )
    task = next(iter(cog._connector_tasks))

    await cog.cog_unload()

    assert task.cancelled() or task.done()
    assert cog._connector_tasks == set()


async def test_cog_unload_closes_the_connector_sessions():
    class _FakeSession:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    opened = {name: _FakeSession() for name in ("anilist", "steam", "osu")}
    sessions._SESSIONS.update(opened)

    await _profiles_cog().cog_unload()

    assert all(session.closed for session in opened.values())
    assert sessions._SESSIONS == {}


async def test_a_session_that_refuses_to_close_never_breaks_the_unload():
    class _Stubborn:
        closed = False

        async def close(self):
            raise RuntimeError("nope")

    class _Fine:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    fine = _Fine()
    sessions._SESSIONS.update({"anilist": _Stubborn(), "steam": fine})

    await _profiles_cog().cog_unload()  # must not raise

    assert fine.closed is True
    assert sessions._SESSIONS == {}


async def test_a_closed_session_is_replaced_rather_than_reused():
    """close_all is not a one-way door: a link landing after an unload opens a
    fresh session instead of failing on a closed one."""

    class _Closed:
        closed = True

    sessions._SESSIONS["osu"] = _Closed()
    session = await sessions.get_session("osu")
    try:
        assert session is not sessions._SESSIONS.get("nothing")
        assert session.closed is False
    finally:
        await session.close()
        sessions._SESSIONS.clear()


def test_every_network_connector_takes_its_session_from_the_registry():
    """No module-level session global may come back: one that nothing owns is
    one nothing closes."""
    import os

    connectors_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "cogs",
        "community",
        "profile",
        "connectors",
    )
    for name in os.listdir(connectors_dir):
        if not name.endswith(".py") or name == "sessions.py":
            continue
        with open(os.path.join(connectors_dir, name), encoding="utf-8") as handle:
            text = handle.read()
        assert "aiohttp.ClientSession(" not in text, name

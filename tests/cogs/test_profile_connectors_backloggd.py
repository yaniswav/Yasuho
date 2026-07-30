"""Tests for the Backloggd connector (cogs/community/profile/connectors/backloggd.py).

The parse is exercised OFFLINE against two vendored real-world fixtures
(``tests/fixtures/backloggd/*.html``, see :mod:`backloggd`'s own docstring for
provenance): a populated profile (favourites, a journal, three reviews) and a
mostly-empty one (no favourites/journal containers at all, a single review,
zero-valued stats). A selector that stops matching real Backloggd markup goes
red HERE, not in production - the connector itself never raises on a page
shape it does not recognise (see ``parse_profile``'s docstring), it just draws
less.

The populated fixture is ANONYMISED: its markup is real, every person-shaped
value in it ('FixtureUser', the bio, the avatar token, the review ids, the
outbound social links) is made up. Never re-vendor a live profile page here -
a real one carries someone's self-written bio. The empty fixture keeps
'Qewertyy', the author of the MIT library this parse is ported from, as part
of that attribution.

No real HTTP anywhere: :func:`backloggd._fetch_profile_html` is monkeypatched
for the link/refresh tests, and its own status-mapping is exercised through a
fake aiohttp-shaped session.
"""

import os

import discord
import pytest

from cogs.community.profile.connectors import backloggd, base

USER = 4242

_FIXTURES = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "backloggd"
)


def _fixture(name):
    with open(os.path.join(_FIXTURES, f"{name}.html"), encoding="utf-8") as handle:
        return handle.read()


def _text(container):
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
    assert isinstance(base.CONNECTORS.get("backloggd"), backloggd.BackloggdConnector)


def test_the_renderer_is_registered():
    from cogs.community.profile import views

    assert views.SECTION_RENDERERS.get("backloggd") is backloggd._render


def test_the_refresh_ttl_is_at_least_twelve_hours_the_scraping_courtesy_window():
    assert backloggd.REFRESH_TTL_SECONDS >= 12 * 60 * 60


# ---------------------------------------------------------------------------
# beautifulsoup4 is imported ONLY here among the connectors
# ---------------------------------------------------------------------------


def test_beautifulsoup4_is_imported_only_in_backloggd_py():
    connectors_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "cogs",
        "community",
        "profile",
        "connectors",
    )
    hits = {}
    for name in os.listdir(connectors_dir):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(connectors_dir, name), encoding="utf-8") as handle:
            text = handle.read()
        if "bs4" in text or "BeautifulSoup" in text:
            hits[name] = True
    assert set(hits) == {"backloggd.py"}


# ---------------------------------------------------------------------------
# parse_profile: the real fixtures
# ---------------------------------------------------------------------------


def test_parse_profile_reads_a_populated_profile():
    parsed = backloggd.parse_profile(_fixture("populated"))
    assert parsed["display_name"] == "FixtureUser"
    assert parsed["avatar"].startswith("https://backloggd-avatars.b-cdn.net/")
    assert parsed["stats"] == {"played": 137, "played_this_year": 32, "backlog": 31}
    assert [g["name"] for g in parsed["favorites"]] == [
        "Clair Obscur: Expedition 33",
        "Metaphor: ReFantazio",
        "Hollow Knight",
    ]
    assert parsed["favorites"][2]["most_favorite"] is True
    assert "most_favorite" not in parsed["favorites"][0]
    assert [g["name"] for g in parsed["recently_played"]] == [
        "Splatoon Raiders",
        "Ghost Trick: Phantom Detective",
        "Mother 3",
    ]
    assert parsed["recently_played"][0]["date"] == "Jul 29"
    # Favourites/journal are capped at 3 even though the fixture has 5 of each.
    assert len(parsed["favorites"]) == 3
    assert len(parsed["recently_played"]) == 3


def test_parse_profile_reads_an_empty_profile_without_favorites_or_journal():
    """No #profile-favorites / #profile-journal container exists at all on this
    profile - the exact shape a brand-new account has - and the parser must
    degrade to empty lists, never raise."""
    parsed = backloggd.parse_profile(_fixture("empty"))
    assert parsed["display_name"] == "Qewertyy"
    assert parsed["avatar"] == "https://backloggd.b-cdn.net/no_avatar.jpg"
    assert parsed["stats"] == {"played": 2, "played_this_year": 0, "backlog": 0}
    assert parsed["favorites"] == []
    assert parsed["recently_played"] == []


def test_parse_profile_never_raises_on_a_page_that_matches_nothing():
    """A future redesign - or a completely different page - must degrade to
    empty data, never a crash: see the module's own resilience docstring."""
    assert backloggd.parse_profile("<html><body>not a profile</body></html>") == {
        "display_name": None,
        "avatar": None,
        "stats": {},
        "favorites": [],
        "recently_played": [],
    }
    assert backloggd.parse_profile("") == {
        "display_name": None,
        "avatar": None,
        "stats": {},
        "favorites": [],
        "recently_played": [],
    }
    assert backloggd.parse_profile(None) == {
        "display_name": None,
        "avatar": None,
        "stats": {},
        "favorites": [],
        "recently_played": [],
    }


# ---------------------------------------------------------------------------
# The review-card port: ratings, and the merge onto recently-played
# ---------------------------------------------------------------------------


def test_extract_recent_reviews_reads_the_star_rating_and_text():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_fixture("populated"), "html.parser")
    reviews = backloggd._extract_recent_reviews(soup)
    assert [r["name"] for r in reviews] == [
        "Persona 5 Royal",
        "Deltarune: Chapter 5",
        "Metroid Fusion",
    ]
    assert [r["rating"] for r in reviews] == [5.0, 4.0, 3.5]
    assert all(r["review"] for r in reviews)


@pytest.mark.parametrize(
    "style, expected",
    (
        ("width:100%", 5.0),
        ("width:80%", 4.0),
        ("width:70%", 3.5),
        ("width: 60.0%", 3.0),
        (None, None),
        ("", None),
        ("height:10px", None),
    ),
)
def test_calculate_rating(style, expected):
    assert backloggd._calculate_rating(style) == expected


def test_merge_review_ratings_backfills_a_matching_title_only():
    recently_played = [{"name": "Persona 5 Royal", "image": "x"}, {"name": "Unrelated"}]
    reviews = [{"name": "Persona 5 Royal", "rating": 4.5}, {"name": "Someone Else"}]
    merged = backloggd._merge_review_ratings(recently_played, reviews)
    assert merged[0]["rating"] == 4.5
    assert "rating" not in merged[1]


def test_merge_review_ratings_never_overwrites_an_existing_inline_rating():
    recently_played = [{"name": "Persona 5 Royal", "rating": 2.0}]
    reviews = [{"name": "Persona 5 Royal", "rating": 4.5}]
    merged = backloggd._merge_review_ratings(recently_played, reviews)
    assert merged[0]["rating"] == 2.0


# ---------------------------------------------------------------------------
# _stat_key: known labels, and a forward-compatible fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, key",
    (
        ("Games Played", "played"),
        ("Played in 2026", "played_this_year"),
        ("Games Backloggd", "backlog"),
        ("Something New!!", "something_new"),
        ("", "stat"),
    ),
)
def test_stat_key_mapping(label, key):
    assert backloggd._stat_key(label) == key


# ---------------------------------------------------------------------------
# Offline handle validation - no network for a malformed handle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handle", ("", "  ", "a", "x" * 31, "no spaces", "bad!name"))
async def test_link_refuses_a_malformed_handle_without_any_network_call(
    handle, monkeypatch
):
    async def _boom(username):
        raise AssertionError("must not be called for an offline-rejected handle")

    monkeypatch.setattr(backloggd, "_fetch_profile_html", _boom)
    connector = backloggd.BackloggdConnector()
    with pytest.raises(base.InvalidHandle) as caught:
        await connector.link(USER, handle)
    assert caught.value.reason == "format"


def test_the_handle_pattern_preserves_case():
    """Backloggd's own urls are case-preserving, so the external_id must be
    the exact string used to re-fetch, never lowercased."""
    assert backloggd.HANDLE_PATTERN.match("FixtureUser")


# ---------------------------------------------------------------------------
# link() / refresh(): end to end against the fixtures
# ---------------------------------------------------------------------------


async def test_link_builds_a_link_result_from_the_populated_fixture(monkeypatch):
    async def _fake_fetch(username):
        assert username == "FixtureUser"
        return _fixture("populated")

    monkeypatch.setattr(backloggd, "_fetch_profile_html", _fake_fetch)
    connector = backloggd.BackloggdConnector()
    result = await connector.link(USER, "FixtureUser")
    assert result.external_id == "FixtureUser"
    assert result.display_name == "FixtureUser"
    assert result.payload["stats"]["played"] == 137
    assert len(result.payload["favorites"]) == 3
    assert "display_name" not in result.payload


async def test_refresh_re_fetches_and_returns_a_bare_payload(monkeypatch):
    async def _fake_fetch(username):
        return _fixture("empty")

    monkeypatch.setattr(backloggd, "_fetch_profile_html", _fake_fetch)
    connector = backloggd.BackloggdConnector()
    payload = await connector.refresh(
        USER, {"external_id": "Qewertyy", "payload": {}}
    )
    assert payload["stats"] == {"played": 2, "played_this_year": 0, "backlog": 0}
    assert "display_name" not in payload


async def test_refresh_refuses_a_connection_with_no_handle():
    connector = backloggd.BackloggdConnector()
    with pytest.raises(base.NotLinked):
        await connector.refresh(USER, {"external_id": "", "payload": {}})


# ---------------------------------------------------------------------------
# _fetch_profile_html: 404 vs "the site had a bad day" (offline, fake session)
# ---------------------------------------------------------------------------


class _FakeContent:
    """The ``response.content`` stream reader, in the one shape
    :func:`backloggd._read_bounded` uses: a bounded ``read(n)``."""

    def __init__(self, raw):
        self._raw = raw

    async def read(self, limit=-1):
        if limit is None or limit < 0:
            return self._raw
        return self._raw[:limit]


class _FakeResponse:
    def __init__(self, status, body="", charset=None):
        self.status = status
        self.charset = charset
        self.content = _FakeContent(
            body if isinstance(body, bytes) else body.encode(charset or "utf-8")
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.last_call = None

    def get(self, url, headers=None, timeout=None):
        self.last_call = (url, headers)
        return self._response


def _async_return(value):
    """``async def _get_session() -> value`` - :func:`backloggd._get_session`
    is awaited by :func:`backloggd._fetch_profile_html`, so the monkeypatch
    replacing it must be a coroutine function too, not a plain lambda."""

    async def _factory():
        return value

    return _factory


async def test_fetch_profile_html_maps_404_to_invalid_handle(monkeypatch):
    session = _FakeSession(_FakeResponse(404))
    monkeypatch.setattr(backloggd, "_get_session", _async_return(session))
    with pytest.raises(base.InvalidHandle) as caught:
        await backloggd._fetch_profile_html("ghost")
    assert caught.value.reason == "not_found"


async def test_fetch_profile_html_maps_any_other_status_to_unavailable(monkeypatch):
    session = _FakeSession(_FakeResponse(503))
    monkeypatch.setattr(backloggd, "_get_session", _async_return(session))
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await backloggd._fetch_profile_html("someone")
    assert caught.value.reason == "remote"


async def test_fetch_profile_html_maps_a_raised_exception_to_unavailable(monkeypatch):
    class _ExplodingSession:
        def get(self, *a, **k):
            raise TimeoutError("slow network")

    monkeypatch.setattr(
        backloggd, "_get_session", _async_return(_ExplodingSession())
    )
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await backloggd._fetch_profile_html("someone")
    assert caught.value.reason == "remote"


async def test_fetch_profile_html_stamps_the_turbolinks_and_referer_headers(
    monkeypatch,
):
    session = _FakeSession(_FakeResponse(200, "<html></html>"))
    monkeypatch.setattr(backloggd, "_get_session", _async_return(session))
    await backloggd._fetch_profile_html("FixtureUser")
    _url, headers = session.last_call
    referer = "https://www.backloggd.com/search/users/FixtureUser"
    assert headers["Referer"] == referer
    assert headers["Turbolinks-Referrer"] == referer
    assert "User-Agent" in headers


# ---------------------------------------------------------------------------
# Renderer: draws from payload only, never the network
# ---------------------------------------------------------------------------


class _Field:
    label = "Backloggd"


async def test_renderer_never_touches_the_network(monkeypatch):
    async def _boom(username):
        raise AssertionError("the renderer must never call the network")

    monkeypatch.setattr(backloggd, "_fetch_profile_html", _boom)
    container = discord.ui.Container()
    connection = {
        "payload": {
            "avatar": "https://backloggd-avatars.b-cdn.net/x",
            "stats": {"played": 137, "played_this_year": 32, "backlog": 31},
            "favorites": [{"name": "Hollow Knight"}],
            "recently_played": [
                {"name": "Persona 5 Royal", "rating": 5.0},
                {"name": "Mother 3"},
            ],
        }
    }
    await backloggd._render(container, _Field(), None, connection, budget=None)
    text = _text(container)
    assert "137 played" in text
    assert "31 backlogged" in text
    assert "Favourites: Hollow Knight" in text
    assert "Recently played: Persona 5 Royal (5.0/5), Mother 3" in text


async def test_renderer_handles_an_empty_payload_without_raising():
    container = discord.ui.Container()
    await backloggd._render(container, _Field(), None, {"payload": {}}, budget=None)
    assert _text(container) == "**Backloggd**"


async def test_renderer_flattens_a_multi_line_game_title():
    container = discord.ui.Container()
    connection = {
        "payload": {"favorites": [{"name": "Great Game\n## Fake Heading"}]}
    }
    await backloggd._render(container, _Field(), None, connection, budget=None)
    text = _text(container)
    assert "\n## Fake Heading" not in text
    assert "Great Game ## Fake Heading" in text


# ---------------------------------------------------------------------------
# Payload stays bounded even for a heavily populated profile
# ---------------------------------------------------------------------------


def test_the_parsed_populated_payload_fits_the_shared_budget():
    parsed = backloggd.parse_profile(_fixture("populated"))
    parsed.pop("display_name")
    encoded = base.encode_payload("backloggd", parsed)
    assert len(encoded.encode("utf-8")) <= base.PAYLOAD_MAX_BYTES


# ---------------------------------------------------------------------------
# Hostile / mangled markup: the parse degrades, it never raises, and what it
# produces still fits the payload cap. Each of these is a page shape that
# DID raise (or DID produce an unstorable payload) before it was guarded.
# ---------------------------------------------------------------------------


def _stats_page(count_text, label="Games Played"):
    return (
        '<div id="profile-stats"><div><a><h1>{count}</h1></a>'
        "<h4>{label}</h4></div></div>"
    ).format(count=count_text, label=label)


def _journal_page(inner):
    return '<div id="profile-journal"><div>{inner}</div></div>'.format(inner=inner)


def _cover(alt="A Game", src="https://images.igdb.com/x.jpg"):
    return (
        '<div class="overflow-wrapper"><img alt="{alt}" src="{src}"/></div>'
    ).format(alt=alt, src=src)


def test_a_counter_with_absurdly_many_digits_is_skipped_not_raised():
    """CPython refuses ``int()`` past 4300 digits (sys.int_info's
    str_digits_check_threshold) with a ValueError, which would escape a parse
    whose whole contract is that it degrades."""
    assert backloggd.parse_profile(_stats_page("1" * 5000))["stats"] == {}
    # ... and a plausible counter still parses, leading zeros and all.
    assert backloggd.parse_profile(_stats_page("032"))["stats"] == {"played": 32}


def test_the_stats_block_cannot_grow_the_payload_without_bound():
    page = '<div id="profile-stats">{blocks}</div>'.format(
        blocks="".join(
            "<div><a><h1>{i}</h1></a><h4>Stat {i}</h4></div>".format(i=i)
            for i in range(500)
        )
    )
    parsed = backloggd.parse_profile(page)
    assert len(parsed["stats"]) <= backloggd._STATS_MAX
    base.encode_payload("backloggd", parsed)


def test_an_absurd_star_width_never_becomes_a_json_infinity():
    """``float("9" * 400)`` is ``inf``; json.dumps writes it as the literal
    ``Infinity``, which is not JSON at all and which Postgres' jsonb cast
    rejects - turning a scraped oddity into a failed link."""
    page = _journal_page(
        _cover()
        + '<div class="star-ratings-static"><div class="stars-top" '
        'style="width:{w}%"></div></div>'.format(w="9" * 400)
    )
    game = backloggd.parse_profile(page)["recently_played"][0]
    assert "rating" not in game
    assert "Infinity" not in base.encode_payload("backloggd", {"g": game})


@pytest.mark.parametrize(
    "style",
    (
        "width:120%",
        "width:-10%",
        "width:999%",
        # Would match its own tail as "000" -> a confident 0.0 if the regex
        # captured a bounded run of digits instead of the whole number.
        "width:1000%",
        "width:1.2.3%",
    ),
)
def test_a_rating_outside_the_zero_to_five_range_is_dropped(style):
    page = _journal_page(
        _cover()
        + '<div class="star-ratings-static"><div class="stars-top" '
        'style="{style}"></div></div>'.format(style=style)
    )
    assert "rating" not in backloggd.parse_profile(page)["recently_played"][0]


def test_a_hostile_review_id_cannot_break_the_css_selector():
    """``review_id`` is an attribute a third party wrote. Interpolated into a
    CSS selector it raises soupsieve's SelectorSyntaxError; compared as a
    plain id it is just a string that matches nothing."""
    from bs4 import BeautifulSoup

    page = (
        '<div class="review-card"><div review_id="1 ,x["></div>'
        + _cover()
        + "</div>"
    )
    parsed = backloggd.parse_profile(page)
    assert parsed["recently_played"] == []
    reviews = backloggd._extract_recent_reviews(BeautifulSoup(page, "html.parser"))
    assert reviews[0]["review"] == ""


def test_an_enormous_title_is_clipped_so_the_payload_still_stores():
    page = _journal_page(_cover(alt="A" * 20000))
    parsed = backloggd.parse_profile(page)
    assert len(parsed["recently_played"][0]["name"]) <= backloggd._NAME_MAX
    base.encode_payload("backloggd", parsed)


@pytest.mark.parametrize(
    "url",
    (
        "/relative/avatar.png",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "//protocol-relative.example/x.png",
        "https://example.invalid/" + "a" * 400,
    ),
)
def test_an_unusable_avatar_url_never_reaches_the_card(url):
    """Discord rejects the WHOLE message over a Thumbnail url it cannot
    fetch, at send time - past the point where views.render_sections can
    still fall back to the badge."""
    page = '<meta property="og:image" content="{url}"/>'.format(url=url)
    assert backloggd.parse_profile(page)["avatar"] is None


async def test_the_renderer_drops_an_unusable_avatar_from_a_stored_payload():
    container = discord.ui.Container()
    connection = {"payload": {"avatar": "javascript:alert(1)", "stats": {"played": 1}}}
    await backloggd._render(container, _Field(), None, connection, budget=None)
    assert not any(
        isinstance(item, discord.ui.Section) for item in container.children
    )
    assert "1 played" in _text(container)


async def test_the_renderer_survives_a_non_numeric_rating_in_a_stored_payload():
    container = discord.ui.Container()
    connection = {"payload": {"favorites": [{"name": "Game", "rating": "five"}]}}
    await backloggd._render(container, _Field(), None, connection, budget=None)
    assert "Favourites: Game" in _text(container)


async def test_a_parse_failure_becomes_a_typed_unavailable(monkeypatch):
    """The braces to parse_profile's belt: anything unforeseen out of bs4
    must reach the cog as ConnectorUnavailable, not as a bare traceback the
    cog can only answer with its generic failure message."""

    async def _html(username):
        return "<html></html>"

    def _explode(html):
        raise RuntimeError("bs4 said no")

    monkeypatch.setattr(backloggd, "_fetch_profile_html", _html)
    monkeypatch.setattr(backloggd, "parse_profile", _explode)
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await backloggd.BackloggdConnector().link(USER, "FixtureUser")
    assert caught.value.reason == "remote"


async def test_an_oversized_page_body_is_refused_rather_than_buffered(monkeypatch):
    body = "<html>" + "x" * (backloggd.MAX_PAGE_BYTES + 10) + "</html>"
    session = _FakeSession(_FakeResponse(200, body))
    monkeypatch.setattr(backloggd, "_get_session", _async_return(session))
    with pytest.raises(base.ConnectorUnavailable) as caught:
        await backloggd._fetch_profile_html("FixtureUser")
    assert caught.value.reason == "remote"


async def test_a_page_at_the_size_cap_still_parses(monkeypatch):
    session = _FakeSession(_FakeResponse(200, _fixture("populated")))
    monkeypatch.setattr(backloggd, "_get_session", _async_return(session))
    html = await backloggd._fetch_profile_html("FixtureUser")
    assert backloggd.parse_profile(html)["display_name"] == "FixtureUser"


async def test_the_profile_url_is_percent_encoded(monkeypatch):
    """refresh() re-fetches whatever external_id the DATABASE holds, which
    never went through link()'s HANDLE_PATTERN in this call."""
    session = _FakeSession(_FakeResponse(200, "<html></html>"))
    monkeypatch.setattr(backloggd, "_get_session", _async_return(session))
    await backloggd._fetch_profile_html("../admin?x=1")
    url, headers = session.last_call
    assert url == "https://www.backloggd.com/u/..%2Fadmin%3Fx%3D1"
    assert "?" not in headers["Referer"].split("/search/users/")[1]

"""SSRF guard: a member must not be able to aim Lavalink at our own host.

Lavalink runs with ``sources.http: true`` (lavalink/application.yml), and
sonolink hands a query to ``/loadtracks`` RAW whenever it starts with
``http://`` or ``https://`` - so ``/play http://127.0.0.1:2333/v4/info``, typed
by any member with no permissions, made the bot's own box issue that request and
told the member whether it worked. Loopback Postgres, the Lavalink REST port
whose password sits in that same yml, and a cloud metadata service at
169.254.169.254 are all one command away.

Pinned here:

* the pure decision - which addresses are refused (every RFC1918 / loopback /
  link-local / CGNAT / multicast / reserved / unspecified form, plus the IPv6
  wrappers that smuggle an IPv4 address past a naive check), which schemes are
  allowed, and that free-text search is not touched at all;
* the DNS-REBINDING property: EVERY address in the answer is checked, not the
  first, so a name that answers "1.2.3.4 and 127.0.0.1" is refused;
* both seams the cog hands user queries to Lavalink through - ``_play_query``
  (``/play``, the search-browser picks, the vibe modal) and ``_search`` (the
  Add-track modal, the ``/search`` degrade path, the favourites resolver) - and
  the counter-test that an ordinary public link still plays.

No network: the resolver is injected (or monkeypatched onto the module), and a
non-URL query never reaches one by construction.
"""

import types

import pytest

from cogs.music import music, urlguard

# ---------------------------------------------------------------------------
# is_url_shaped - only a URL is examined at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "http://example.com/song.mp3",
        "https://youtube.com/watch?v=x",
        "HTTPS://YOUTUBE.COM/watch?v=x",
        "  https://example.com/x  ",
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
    ],
)
def test_url_shaped_queries_are_examined(query):
    assert urlguard.is_url_shaped(query)


@pytest.mark.parametrize(
    "query",
    [
        "daft punk one more time",
        "",
        None,
        "   ",
        # A Lavalink search PREFIX is not a URL: sonolink never passes it raw.
        "ytsearch:daft punk",
        "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        "a song with :// in the title?",
        "://nohost",
    ],
)
def test_free_text_is_never_examined(query):
    assert not urlguard.is_url_shaped(query)


def test_a_scheme_with_a_leading_digit_is_not_a_url():
    assert not urlguard.is_url_shaped("1http://example.com")


# ---------------------------------------------------------------------------
# split_target - where the host really is
# ---------------------------------------------------------------------------


def test_userinfo_does_not_disguise_the_host():
    """``http://youtube.com@127.0.0.1/`` targets 127.0.0.1, not youtube.com."""
    assert urlguard.split_target("http://youtube.com@127.0.0.1/x") == (
        "http",
        "127.0.0.1",
    )


def test_an_ipv6_literal_is_unwrapped_from_its_brackets():
    assert urlguard.split_target("http://[::1]:2333/v4/info") == ("http", "::1")


def test_the_scheme_is_lowercased():
    scheme, host = urlguard.split_target("HTTP://Example.COM/x")
    assert scheme == "http"
    assert host == "example.com"


def test_a_url_with_no_host_has_none():
    assert urlguard.split_target("http:///etc/passwd")[1] is None


# ---------------------------------------------------------------------------
# is_blocked_address - the pure address decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # the cloud metadata service
        "100.64.0.1",  # CGNAT
        "224.0.0.1",  # multicast: is_global says True, we still refuse
        "240.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "ff02::1",  # IPv6 multicast: is_global True as well
        "::ffff:127.0.0.1",  # v4-mapped
        "2002:7f00:1::",  # 6to4 wrapping 127.0.0.1
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1 - the is_global hole
    ],
)
def test_internal_addresses_are_refused(address):
    assert urlguard.is_blocked_address(address)


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "1.1.1.1", "142.250.185.78", "2606:4700:4700::1111"]
)
def test_ordinary_public_addresses_are_allowed(address):
    assert not urlguard.is_blocked_address(address)


def test_something_that_is_not_an_address_at_all_fails_closed():
    """"I could not tell" must never read as "allowed"."""
    assert urlguard.is_blocked_address("not-an-ip")
    assert urlguard.is_blocked_address("")
    assert urlguard.is_blocked_address(None)


# ---------------------------------------------------------------------------
# check_query - the whole guard
# ---------------------------------------------------------------------------


def _resolver(*addresses):
    async def resolve(_host):
        return list(addresses)

    return resolve


async def test_a_plain_search_is_allowed_without_resolving_anything():
    async def boom(_host):
        raise AssertionError("a text query must never reach the resolver")

    assert await urlguard.check_query("daft punk", resolver=boom) is None


async def test_a_public_url_is_allowed():
    assert (
        await urlguard.check_query(
            "https://www.youtube.com/watch?v=x", resolver=_resolver("142.250.185.78")
        )
        is None
    )


async def test_a_soundcloud_link_still_plays():
    assert (
        await urlguard.check_query(
            "https://soundcloud.com/artist/track", resolver=_resolver("13.32.99.1")
        )
        is None
    )


async def test_a_loopback_url_is_refused():
    assert (
        await urlguard.check_query(
            "http://127.0.0.1:2333/v4/info", resolver=_resolver("127.0.0.1")
        )
        == urlguard.REASON_BLOCKED_ADDRESS
    )


async def test_the_metadata_service_is_refused():
    assert (
        await urlguard.check_query(
            "http://169.254.169.254/latest/meta-data/",
            resolver=_resolver("169.254.169.254"),
        )
        == urlguard.REASON_BLOCKED_ADDRESS
    )


async def test_a_name_that_resolves_internally_is_refused():
    """The host looks innocent; only the ANSWER gives it away."""
    assert (
        await urlguard.check_query(
            "https://music.evil.example/song.mp3", resolver=_resolver("10.1.2.3")
        )
        == urlguard.REASON_BLOCKED_ADDRESS
    )


async def test_dns_rebinding_every_answer_is_checked_not_the_first():
    """THE PROPERTY. A public A record first, a loopback one behind it."""
    assert (
        await urlguard.check_query(
            "https://rebind.evil.example/x",
            resolver=_resolver("8.8.8.8", "1.1.1.1", "127.0.0.1"),
        )
        == urlguard.REASON_BLOCKED_ADDRESS
    )


@pytest.mark.parametrize(
    "query",
    [
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
        "ftp://internal.example/x",
        "dict://127.0.0.1:11211/stat",
    ],
)
async def test_non_http_schemes_are_refused(query):
    async def boom(_host):
        raise AssertionError("a refused scheme must not cost a lookup")

    assert await urlguard.check_query(query, resolver=boom) == urlguard.REASON_SCHEME


async def test_a_url_with_no_host_is_refused():
    assert (
        await urlguard.check_query("http:///etc/passwd", resolver=_resolver())
        == urlguard.REASON_NO_HOST
    )


async def test_an_unresolvable_name_is_refused():
    async def fails(_host):
        raise OSError("NXDOMAIN")

    assert (
        await urlguard.check_query("https://nope.invalid/x", resolver=fails)
        == urlguard.REASON_UNRESOLVABLE
    )


async def test_an_empty_answer_is_refused():
    assert (
        await urlguard.check_query("https://nope.invalid/x", resolver=_resolver())
        == urlguard.REASON_UNRESOLVABLE
    )


# ---------------------------------------------------------------------------
# The cog seams
# ---------------------------------------------------------------------------


class _Track:
    def __init__(self, title="New"):
        self.title = title
        self.author = "Artist"
        self.identifier = title
        self.uri = "https://example.test/{0}".format(title)
        self.length = 1000
        self.is_stream = False
        self.source_name = "http"
        self.encoded = "enc"
        self.extras = types.SimpleNamespace(requester=None, radio=False)


class _Result:
    def __init__(self, payload):
        self.result = payload

    def is_error(self):
        return False

    def is_empty(self):
        return False


class _SLClient:
    def __init__(self):
        self.searches = []

    async def search_track(self, query, source=None):
        self.searches.append(query)
        return _Result(_Track())


def _cog():
    cog = music.Music.__new__(music.Music)
    cog.bot = types.SimpleNamespace(sl_client=_SLClient(), db_pool=None)
    cog._nodes_available = lambda: True

    async def snapshot(_player, track=None):
        pass

    cog._snapshot = snapshot
    return cog


def _ctx():
    ctx = types.SimpleNamespace(
        author=types.SimpleNamespace(id=9, voice=None),
        channel=types.SimpleNamespace(id=77),
        guild=types.SimpleNamespace(id=99),
        voice_client=None,
        sends=[],
    )

    async def defer(*_a, **_kw):
        return None

    async def send(*args, **kwargs):
        ctx.sends.append((args, kwargs))

    ctx.defer = defer
    ctx.send = send
    return ctx


@pytest.fixture
def loopback_dns(monkeypatch):
    """Every name resolves to 127.0.0.1 (and nothing touches the network)."""

    async def resolve(_host):
        return ["127.0.0.1"]

    monkeypatch.setattr(urlguard, "resolve_addresses", resolve)


@pytest.fixture
def public_dns(monkeypatch):
    async def resolve(_host):
        return ["142.250.185.78"]

    monkeypatch.setattr(urlguard, "resolve_addresses", resolve)


async def test_play_query_never_hands_an_internal_url_to_lavalink(loopback_dns):
    cog, ctx = _cog(), _ctx()

    await cog._play_query(ctx, "http://127.0.0.1:2333/v4/info")

    assert cog.bot.sl_client.searches == []
    assert ctx.sends  # the member is told, once, in one wording
    assert "public links" in ctx.sends[-1][0][0]


async def test_play_query_refuses_before_it_would_connect_to_voice(loopback_dns):
    """Refusing early means a hostile URL costs no voice connect either."""
    cog, ctx = _cog(), _ctx()
    ctx.author.voice = types.SimpleNamespace(channel=None)

    await cog._play_query(ctx, "http://169.254.169.254/latest/meta-data/")

    # Not the "you must be in a voice channel" refusal - the URL was refused first.
    assert "public links" in ctx.sends[-1][0][0]


async def test_play_query_says_the_same_thing_whatever_the_reason(loopback_dns):
    """No oracle: scheme, private address and NXDOMAIN read identically."""
    messages = set()
    for query in (
        "file:///etc/passwd",
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
    ):
        cog, ctx = _cog(), _ctx()
        await cog._play_query(ctx, query)
        messages.add(ctx.sends[-1][0][0])
    assert len(messages) == 1


async def test_search_seam_refuses_an_internal_url_and_finds_nothing(loopback_dns):
    cog = _cog()
    assert await cog._search("http://127.0.0.1:5432/") is None
    assert cog.bot.sl_client.searches == []


async def test_search_seam_still_runs_an_ordinary_public_link(public_dns):
    cog = _cog()
    result = await cog._search("https://www.youtube.com/watch?v=x")
    assert result is not None
    assert cog.bot.sl_client.searches == ["https://www.youtube.com/watch?v=x"]


async def test_search_seam_costs_a_plain_query_nothing(monkeypatch):
    async def boom(_host):
        raise AssertionError("a text query must never reach the resolver")

    monkeypatch.setattr(urlguard, "resolve_addresses", boom)
    cog = _cog()
    await cog._search("daft punk one more time")
    assert cog.bot.sl_client.searches == ["daft punk one more time"]

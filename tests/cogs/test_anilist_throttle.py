"""Tests for the interactive AniList API-abuse throttle (audit P-2).

Side-effect free: no Discord gateway, no network, no database. The throttle is
pure and clock-injected, so time is driven explicitly; the ``_graphql`` 429 path
and the button-callback guard are exercised against tiny fakes.
"""

import types

from cogs.anilist import airing, chapters, components, feed_delivery
from cogs.anilist.base import AniListBase
from cogs.anilist.feed import AniListFeed
from cogs.anilist.throttle import (
    GLOBAL_LIMIT,
    GUILD_LIMIT,
    USER_LIMIT,
    AniListThrottle,
)


# ---------------------------------------------------------------------------
# Fake aiohttp session (mirrors tests/cogs/test_anilist_http.py).
# ---------------------------------------------------------------------------
class _Response:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def json(self):
        return self.payload


class _Request:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    closed = False

    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Request(self.response)


class _Clock:
    """A hand-cranked monotonic clock for deterministic window tests."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ---------------------------------------------------------------------------
# Pure throttle behaviour.
# ---------------------------------------------------------------------------
def test_global_ceiling_bounds_then_frees():
    clock = _Clock()
    throttle = AniListThrottle(clock=clock)

    # The whole interactive surface may sustain at most GLOBAL_LIMIT per window.
    for _ in range(GLOBAL_LIMIT):
        assert throttle.allow_global() is True
    assert throttle.allow_global() is False

    # A slot frees exactly one window after the first hit.
    clock.now += 60.0
    assert throttle.allow_global() is True


def test_per_user_quota_triggers_on_button_path():
    clock = _Clock()
    throttle = AniListThrottle(clock=clock)

    # One member clicking buttons is bounded per user, independent of guild.
    for _ in range(USER_LIMIT):
        assert throttle.allow_interactive(42, 7) is True
    assert throttle.allow_interactive(42, 7) is False

    # A different member in the same guild is unaffected by the first's spending.
    assert throttle.allow_interactive(99, 7) is True


def test_per_guild_quota_triggers_across_members():
    clock = _Clock()
    throttle = AniListThrottle(clock=clock)

    # Spread the guild budget across distinct members so no per-user cap trips
    # first (GUILD_LIMIT < USER_LIMIT * GUILD_LIMIT members).
    for member in range(GUILD_LIMIT):
        assert throttle.allow_interactive(member, 7) is True
    # The guild window is now full: a fresh member is refused on the guild axis.
    assert throttle.allow_interactive(9999, 7) is False


def test_rejection_does_not_consume_the_other_axis():
    clock = _Clock()
    throttle = AniListThrottle(clock=clock)

    # Exhaust one user; a further click from them is refused...
    for _ in range(USER_LIMIT):
        throttle.allow_interactive(1, 7)
    assert throttle.allow_interactive(1, 7) is False

    # ...and that refusal must not have burned the guild's budget: the guild has
    # only USER_LIMIT hits banked, well under GUILD_LIMIT, so a new member passes.
    assert throttle.allow_interactive(2, 7) is True
    assert throttle.stats()["guild"]["hits"] == USER_LIMIT + 1


def test_dm_has_no_guild_axis():
    throttle = AniListThrottle(clock=_Clock())
    # guild_id None (a DM) only spends the per-user window.
    for _ in range(USER_LIMIT):
        assert throttle.allow_interactive(1, None) is True
    assert throttle.allow_interactive(1, None) is False


# ---------------------------------------------------------------------------
# _graphql: global ceiling + 429 visibility.
# ---------------------------------------------------------------------------
async def test_graphql_counts_and_logs_429(caplog):
    session = _Session(_Response(429, {"errors": ["rate limited"]}))
    bot = types.SimpleNamespace(http_session=session)
    base = AniListBase(bot)

    with caplog.at_level("WARNING"):
        result = await base._graphql("query { ok }", {})

    # The body is still returned (callers degrade on it), but the 429 is surfaced.
    assert result == {"errors": ["rate limited"]}
    assert base._throttle.throttled_count == 1
    assert any("429" in rec.message for rec in caplog.records)


async def test_graphql_ceiling_drops_call_before_the_wire():
    session = _Session(_Response(200, {"data": {"ok": True}}))
    bot = types.SimpleNamespace(http_session=session)
    base = AniListBase(bot)

    # Burn the whole process-wide interactive budget directly on the throttle.
    for _ in range(GLOBAL_LIMIT):
        assert base._throttle.allow_global() is True

    # The next user-driven _graphql is refused WITHOUT any HTTP call, protecting
    # the pollers' share of the shared per-IP budget.
    result = await base._graphql("query { ok }", {})
    assert result is None
    assert session.calls == []


async def test_graphql_200_still_works_and_does_not_count_429():
    session = _Session(_Response(200, {"data": {"ok": True}}))
    bot = types.SimpleNamespace(http_session=session)
    base = AniListBase(bot)

    result = await base._graphql("query { ok }", {})
    assert result == {"data": {"ok": True}}
    assert base._throttle.throttled_count == 0
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# Button-callback guard: terse ephemeral 'slow down' when the quota is spent.
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self):
        self.sent = None

    async def send_message(self, content, ephemeral=False):
        self.sent = (content, ephemeral)


class _FakeInteraction:
    def __init__(self, user_id, guild_id):
        self.user = types.SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.response = _FakeResponse()


async def test_deny_if_throttled_lets_first_click_through():
    cog = types.SimpleNamespace(_throttle=AniListThrottle(clock=_Clock()))
    interaction = _FakeInteraction(1, 7)

    denied = await components._deny_if_throttled(cog, interaction)
    assert denied is False
    assert interaction.response.sent is None


async def test_deny_if_throttled_refuses_once_quota_is_spent():
    cog = types.SimpleNamespace(_throttle=AniListThrottle(clock=_Clock()))

    # Exhaust the per-user window.
    for _ in range(USER_LIMIT):
        await components._deny_if_throttled(cog, _FakeInteraction(1, 7))

    interaction = _FakeInteraction(1, 7)
    denied = await components._deny_if_throttled(cog, interaction)
    assert denied is True
    # A terse ephemeral 'slow down' was sent on the button path.
    content, ephemeral = interaction.response.sent
    assert ephemeral is True
    assert "Slow down" in content


async def test_deny_if_throttled_no_throttle_never_blocks():
    cog = types.SimpleNamespace()  # older wiring: no _throttle attribute
    interaction = _FakeInteraction(1, 7)
    assert await components._deny_if_throttled(cog, interaction) is False


# ---------------------------------------------------------------------------
# Feed surface: the DynamicItem Like/Reply/Add buttons and the admin searches
# share the SAME throttle as the lookup commands, so a click storm cannot burn
# the pollers' share of the per-IP budget. The pollers stay isolated.
# ---------------------------------------------------------------------------
class _FeedResponse:
    """interaction.response for a feed action: records the ephemeral it sends."""

    def __init__(self):
        self.sent = None
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content, **kwargs):
        self.sent = (content, kwargs)
        self._done = True


class _FeedClient:
    """Minimal interaction.client / bot: resolves the AniList cog, holds a session."""

    def __init__(self, cog, session=None):
        self._cog = cog
        self.http_session = session

    def get_cog(self, name):
        return self._cog if name == "AniList" else None


class _FeedInteraction:
    def __init__(self, user_id, guild_id, cog, session=None):
        self.user = types.SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.client = _FeedClient(cog, session)
        self.response = _FeedResponse()


class _FakeCog:
    """Stand-in for the composed AniList cog: carries the shared throttle and a
    token-status spy so a test can prove the click bailed BEFORE token resolution.
    """

    def __init__(self, throttle):
        self._throttle = throttle
        self.token_calls = 0

    async def _token_status(self, user_id):
        self.token_calls += 1
        return "ok", "tok"


async def test_run_like_refused_when_global_ceiling_spent():
    # (a) With the process-wide ceiling already spent, a feed Like is refused with
    # no AniList round-trip and without even resolving the token.
    throttle = AniListThrottle(clock=_Clock())
    for _ in range(GLOBAL_LIMIT):
        assert throttle.allow_global() is True

    cog = _FakeCog(throttle)
    session = _Session(_Response(200, {"data": {}}))
    interaction = _FeedInteraction(50001, 7, cog, session)

    await feed_delivery._run_like(interaction, 123)

    assert session.calls == []  # nothing left for the wire
    assert cog.token_calls == 0  # bailed at the throttle gate, before the token
    content, kwargs = interaction.response.sent
    assert "Slow down" in content
    assert kwargs.get("ephemeral") is True


async def test_run_like_refusal_is_ephemeral_slow_down():
    # (c) The refusal reuses the shared 'slow down' ephemeral (per-user axis here).
    throttle = AniListThrottle(clock=_Clock())
    for _ in range(USER_LIMIT):
        assert throttle.allow_interactive(50002, 7) is True

    interaction = _FeedInteraction(50002, 7, _FakeCog(throttle))
    await feed_delivery._run_like(interaction, 999)

    content, kwargs = interaction.response.sent
    assert kwargs.get("ephemeral") is True
    assert "Slow down" in content


async def test_authed_graphql_429_counts_on_the_shared_throttle(caplog):
    # (b) A 429 on the feed action path records on the SAME shared counter the
    # lookup path uses, at WARNING, and raises the typed rate-limit error.
    throttle = AniListThrottle(clock=_Clock())
    cog = _FakeCog(throttle)
    session = _Session(
        _Response(429, {"errors": ["rate limited"]}, {"Retry-After": "7"})
    )
    bot = _FeedClient(cog, session)

    raised = False
    with caplog.at_level("WARNING"):
        try:
            await feed_delivery._authed_graphql(bot, "tok", "mutation {}", {})
        except feed_delivery._RateLimited as exc:
            raised = True
            assert exc.retry_after == 7

    assert raised is True
    assert throttle.throttled_count == 1
    assert any("429" in rec.message for rec in caplog.records)


async def test_feed_poller_graphql_consumes_nothing_from_the_ceiling():
    # (d) Non-regression on isolation: the AniListFeed poller's own _graphql never
    # touches the interactive throttle - neither a 200 nor a 429 moves its windows
    # or its 429 counter (the pollers keep their own embargo).
    throttle = AniListThrottle(clock=_Clock())
    cog = _FakeCog(throttle)

    feed = AniListFeed.__new__(AniListFeed)
    feed.bot = _FeedClient(cog, _Session(_Response(200, {"data": {"ok": True}})))
    assert await feed._graphql("query { ok }", {}) == {"data": {"ok": True}}

    feed.bot = _FeedClient(
        cog, _Session(_Response(429, {"errors": []}, {"Retry-After": "3"}))
    )
    raised = False
    try:
        await feed._graphql("query { ok }", {})
    except feed_delivery._RateLimited:
        raised = True
    assert raised is True

    assert throttle.throttled_count == 0
    assert throttle.stats()["global"]["hits"] == 0
    assert throttle.stats()["user"]["hits"] == 0


async def test_lookup_and_feed_share_one_global_window():
    # (e) A hit on the LOOKUP side (AniListBase._graphql) counts against the feed
    # buttons' ceiling: both read the one shared throttle instance.
    bot = types.SimpleNamespace(
        http_session=_Session(_Response(200, {"data": {"ok": True}}))
    )
    base = AniListBase(bot)

    for _ in range(GLOBAL_LIMIT):
        assert await base._graphql("query { ok }", {}) == {"data": {"ok": True}}

    cog = _FakeCog(base._throttle)
    interaction = _FeedInteraction(50003, 7, cog)
    await feed_delivery._run_like(interaction, 42)

    content, kwargs = interaction.response.sent
    assert "Slow down" in content
    assert kwargs.get("ephemeral") is True


async def test_run_seen_refused_when_global_ceiling_spent():
    # (review MAJOR) The airing 'Seen' button is the same interactive AniList
    # write surface as the feed buttons: with the process-wide ceiling spent it
    # is refused before the token and before any wire call.
    throttle = AniListThrottle(clock=_Clock())
    for _ in range(GLOBAL_LIMIT):
        assert throttle.allow_global() is True

    cog = _FakeCog(throttle)
    session = _Session(_Response(200, {"data": {}}))
    interaction = _FeedInteraction(50004, 7, cog, session)

    await airing._run_seen(interaction, 321, 5)

    assert session.calls == []
    assert cog.token_calls == 0
    content, kwargs = interaction.response.sent
    assert "Slow down" in content
    assert kwargs.get("ephemeral") is True


async def test_run_read_refused_when_global_ceiling_spent():
    # (review MAJOR) Same guarantee for the chapters 'Read' button.
    throttle = AniListThrottle(clock=_Clock())
    for _ in range(GLOBAL_LIMIT):
        assert throttle.allow_global() is True

    cog = _FakeCog(throttle)
    session = _Session(_Response(200, {"data": {}}))
    interaction = _FeedInteraction(50005, 7, cog, session)

    await chapters._run_read(interaction, 654, "110.5")

    assert session.calls == []
    assert cog.token_calls == 0
    content, kwargs = interaction.response.sent
    assert "Slow down" in content
    assert kwargs.get("ephemeral") is True

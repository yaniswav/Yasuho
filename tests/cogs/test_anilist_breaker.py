"""The shared AniList poller circuit breaker (the 2026-09 ten-day 403 outage).

From 2026-09-02 to 2026-09-12 AniList answered every request with HTTP 403 and
"The AniList API has been temporarily disabled due to severe stability issues.",
and the three pollers kept going at full rate for ten days. These tests pin the
behaviour that stops a repeat, and - just as important - the things that must
NOT change: cursors are still held on a failure, a 429 keeps its own Retry-After
path, and the interactive surface still fails one request at a time in front of
the person who asked.

Side-effect free: no network, no database, no Discord, no Lavalink. The breaker
is clock-injected, so every backoff is driven by hand rather than slept through,
and the HTTP layer is the same tiny fake session the rest of the AniList tests
use.

Nothing here reads source text or a docstring. Every claim is made by building
an input, running the production function, and looking at what it did - and
every guard whose pass condition is a SILENCE (no request, no log line) is
paired with the same harness in the state where it MUST speak, so a guard that
went blind cannot read as a guard that is satisfied.
"""

import logging
import time
import types

import pytest

from cogs.anilist import airing as ai
from cogs.anilist import breaker as br
from cogs.anilist import chapters as ch
from cogs.anilist import feed as fd
from cogs.anilist import feed_delivery
from cogs.anilist.base import AniListBase
from cogs.system import health

BREAKER_LOGGER = "cogs.anilist.breaker"

# The body AniList actually served for ten days, verbatim in shape: a 403 whose
# GraphQL error list carries the disabled message. This is the input the whole
# feature exists for, so it is written down once and reused.
DISABLED_MESSAGE = (
    "The AniList API has been temporarily disabled due to severe stability issues."
)
DISABLED_403_BODY = {
    "data": None,
    "errors": [{"message": DISABLED_MESSAGE, "status": 403}],
}

# The same outage shape as served by an edge that strips the per-error status.
DISABLED_403_BODY_NO_STATUS = {
    "data": None,
    "errors": [{"message": DISABLED_MESSAGE}],
}

# A GraphQL miss about the THING WE ASKED FOR: a deleted AniList account. One of
# these must never convince the breaker that AniList is down for everybody.
NOT_FOUND_BODY = {
    "data": None,
    "errors": [{"message": "Not Found", "status": 404}],
}


# ---------------------------------------------------------------------------
# Fakes (the session trio mirrors tests/cogs/test_anilist_http.py).
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
    """Serves ``self.response``, which a test may swap mid-run (a service heals)."""

    closed = False

    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Request(self.response)

    def get(self, url, **kwargs):
        return self.post(url, **kwargs)


class _Clock:
    """A hand-cranked monotonic clock, so backoffs are stepped, never slept."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _bot(session=None, clock=None):
    """A bot stand-in carrying the shared session and a clock-driven breaker."""

    bot = types.SimpleNamespace(http_session=session)
    if clock is not None:
        setattr(bot, br.BOT_ATTR, br.AniListBreaker(clock=clock))
    return bot


def _fail(breaker, times, reason="HTTP 403", clock=None):
    """Report ``times`` hard failures, taking the probe token each time."""

    for _ in range(times):
        breaker.require_closed("test")
        breaker.note_failure("test", reason)


# ---------------------------------------------------------------------------
# The pure breaker: threshold, schedule, cap, close.
# ---------------------------------------------------------------------------
def test_it_takes_a_run_of_failures_to_open_not_a_single_blip():
    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)

    for _ in range(br.FAILURE_THRESHOLD - 1):
        assert breaker.note_failure("feed", "timeout") is False
        assert breaker.is_open() is False
        assert breaker.tripped is False

    assert breaker.note_failure("feed", "timeout") is True
    assert breaker.is_open() is True
    assert breaker.tripped is True


def test_one_success_inside_the_run_clears_it():
    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)

    for _ in range(br.FAILURE_THRESHOLD - 1):
        breaker.note_failure("feed", "timeout")
    breaker.note_success("feed")

    # The counter restarted, so the next THRESHOLD-1 failures still do not open.
    for _ in range(br.FAILURE_THRESHOLD - 1):
        assert breaker.note_failure("feed", "timeout") is False
    assert breaker.is_open() is False


def test_an_open_circuit_refuses_before_the_socket():
    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)
    _fail(breaker, br.FAILURE_THRESHOLD)

    with pytest.raises(br.CircuitOpen) as caught:
        breaker.require_closed("airing")

    assert caught.value.source == "airing"
    assert caught.value.seconds_left == pytest.approx(br.OPEN_BASE_SECONDS)
    assert breaker.skipped == 1
    # NOT a _FetchError: the per-user handlers in _refresh_lists must not see it.
    assert not isinstance(caught.value, feed_delivery._FetchError)


def test_the_backoff_doubles_and_stops_at_one_hour():
    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)

    # The run that opens it, then one failed probe per step.
    _fail(breaker, br.FAILURE_THRESHOLD)
    schedule = [breaker.seconds_left()]
    for _ in range(6):
        clock.advance(schedule[-1])  # wait it out, probe, fail again
        breaker.require_closed("feed")
        breaker.note_failure("feed", "HTTP 403")
        schedule.append(breaker.seconds_left())

    assert schedule == [240.0, 480.0, 960.0, 1920.0, 3600.0, 3600.0, 3600.0]
    assert max(schedule) == br.OPEN_CAP_SECONDS


def test_the_first_wait_is_two_ticks_of_the_fastest_poller():
    """The base backoff is a claim ABOUT the feed period, so bind it to one.

    Written as a live comparison rather than a repeated literal: someone who
    retunes ``feed.POLL_SECONDS`` has to come back here and decide, instead of
    silently leaving the breaker's first step at an unrelated number.
    """

    assert br.OPEN_BASE_SECONDS == 2 * fd.POLL_SECONDS
    # And the cap really is the hour the design promises, not a rounding of it.
    assert br.OPEN_CAP_SECONDS == 3600.0


def test_an_expiry_costs_exactly_one_probe_for_the_whole_process():
    """Three pollers waking together must not mean three probes."""

    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)
    _fail(breaker, br.FAILURE_THRESHOLD)

    clock.advance(br.OPEN_BASE_SECONDS)
    assert breaker.is_open() is False  # the wait is over: somebody may probe

    breaker.require_closed("feed")  # the feed takes the single token
    assert breaker.is_open() is True  # ...and everyone else is refused again
    with pytest.raises(br.CircuitOpen):
        breaker.require_closed("airing")
    with pytest.raises(br.CircuitOpen):
        breaker.require_closed("chapters")

    # The probe reports failure: the token is handed back and the wait doubles.
    breaker.note_failure("feed", "HTTP 403")
    assert breaker.seconds_left() == pytest.approx(br.OPEN_BASE_SECONDS * 2)


def test_the_first_success_closes_it_whatever_the_streak():
    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)
    _fail(breaker, br.FAILURE_THRESHOLD)
    for _ in range(4):
        clock.advance(breaker.seconds_left())
        breaker.require_closed("feed")
        breaker.note_failure("feed", "HTTP 403")

    clock.advance(breaker.seconds_left())
    breaker.require_closed("feed")
    breaker.note_success("feed")

    assert breaker.tripped is False
    assert breaker.is_open() is False
    assert breaker.seconds_left() == 0.0
    # A fresh outage starts its backoff from the base again, not from the cap.
    _fail(breaker, br.FAILURE_THRESHOLD)
    assert breaker.seconds_left() == pytest.approx(br.OPEN_BASE_SECONDS)


def test_a_probe_that_neither_proves_nor_disproves_hands_the_token_back():
    """A 429 during a probe: nothing learned, nothing counted, retry allowed."""

    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)
    _fail(breaker, br.FAILURE_THRESHOLD)
    opens_before = breaker.opens
    left_before = breaker.seconds_left()

    clock.advance(br.OPEN_BASE_SECONDS)
    breaker.require_closed("feed")
    breaker.note_inconclusive()

    assert breaker.opens == opens_before  # no new outage was recorded
    # The backoff did NOT step: the deadline is still the one the opening set,
    # now simply elapsed. A step would have pushed it out to 480s instead.
    assert breaker.seconds_left() == pytest.approx(max(0.0, left_before - br.OPEN_BASE_SECONDS))
    assert breaker.seconds_left() != pytest.approx(br.OPEN_BASE_SECONDS * 2)
    assert breaker.tripped is True  # ...and a 429 did not close it either
    breaker.require_closed("airing")  # the token is free for the next poller


def test_a_rate_limit_neither_charges_nor_clears_the_failure_streak():
    """The 429's effect on the counters is ZERO, pinned in BOTH directions.

    Charging it would punish AniList twice - its own ``Retry-After`` embargo
    already owns that backoff - and two hard failures plus one rate limit would
    then open the circuit on a service that answered us. Clearing the streak
    would do the opposite and forget an outage in progress on the strength of a
    rate limit. Neither is asserted on the counter itself: each is driven to the
    one observable boundary where the two answers differ.
    """

    # Charging it: fail, 429, fail would reach the threshold one short.
    charging = br.AniListBreaker(clock=_Clock())
    charging.note_failure("feed", "HTTP 403")
    charging.note_inconclusive()
    charging.note_failure("airing", "HTTP 403")
    assert charging.tripped is False, "a 429 counted as a hard failure"
    # CONTROL: the very next hard failure is the third, and it DOES open.
    charging.note_failure("chapters", "HTTP 403")
    assert charging.tripped is True

    # Clearing it: fail, fail, 429, fail must still be a run of three.
    clearing = br.AniListBreaker(clock=_Clock())
    clearing.note_failure("feed", "HTTP 403")
    clearing.note_failure("airing", "HTTP 403")
    clearing.note_inconclusive()
    clearing.note_failure("chapters", "HTTP 403")
    assert clearing.tripped is True, "a 429 forgot an outage already under way"
    assert clearing.opens == 1


def test_opens_counts_outages_and_not_backoff_steps():
    """``breaker_opens`` is read by a human, so it counts THINGS THAT HAPPENED.

    One outage that takes six failed probes to heal is one outage. Reporting it
    as six would tell the operator AniList went down six times; the step the
    backoff has reached is already live on the same line as ``breaker_left``.
    """

    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)

    _fail(breaker, br.FAILURE_THRESHOLD)
    for _ in range(5):  # five failed probes, one continuous outage
        clock.advance(breaker.seconds_left())
        breaker.require_closed("feed")
        breaker.note_failure("feed", "HTTP 403")

    assert breaker.seconds_left() > br.OPEN_BASE_SECONDS  # it really did step
    assert breaker.opens == 1

    # It heals, then a SECOND, separate outage starts. That one is a new count.
    clock.advance(breaker.seconds_left())
    breaker.require_closed("feed")
    breaker.note_success("feed")
    assert breaker.opens == 1  # healing keeps the trace

    _fail(breaker, br.FAILURE_THRESHOLD)
    assert breaker.opens == 2
    assert breaker.seconds_left() == pytest.approx(br.OPEN_BASE_SECONDS)


# ---------------------------------------------------------------------------
# Monotonic time. CI runs this whole suite again 400 days ahead.
# ---------------------------------------------------------------------------
def test_the_backoff_is_immune_to_the_wall_clock(monkeypatch):
    """A 400-day wall-clock jump must not shorten or lengthen a backoff.

    The suite runs a second time under ``YASUHO_TIME_TRAVEL_DAYS=400``, which
    moves ``time.time`` and every ``datetime.now`` but leaves the monotonic
    clock ticking normally. This test makes the same move locally and inside one
    open circuit, which the harness cannot: it proves the deadline is a
    monotonic DELTA rather than a stamp that a clock step can jump over.
    """

    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)
    _fail(breaker, br.FAILURE_THRESHOLD)
    assert breaker.is_open() is True

    real_time = time.time()
    monkeypatch.setattr(time, "time", lambda: real_time + 400 * 86400)

    assert breaker.is_open() is True
    assert breaker.seconds_left() == pytest.approx(br.OPEN_BASE_SECONDS)

    # Only the monotonic clock can end it.
    clock.advance(br.OPEN_BASE_SECONDS)
    assert breaker.is_open() is False


def test_the_default_clock_is_monotonic():
    assert br.AniListBreaker()._clock is time.monotonic


# ---------------------------------------------------------------------------
# THE CLASSIFIER, aimed at inputs it must report and inputs it must clear.
# ---------------------------------------------------------------------------
def test_the_graphql_classifier_reports_and_clears_the_right_payloads():
    must_be_service = [
        DISABLED_403_BODY["errors"],  # the real ten-day outage
        DISABLED_403_BODY_NO_STATUS["errors"],  # same, status stripped
        [{"message": "Internal Server Error", "status": 500}],
        [{"message": "Not Found", "status": 404}, {"message": "boom", "status": 500}],
        [],  # an empty list says nothing; assume the service
        None,  # not even a list
        "Not Found",  # a string, not the shape we parse
    ]
    must_be_target = [
        NOT_FOUND_BODY["errors"],
        [{"message": "Not Found", "status": 404}, {"message": "bad id", "status": 400}],
    ]

    reported = [e for e in must_be_service if br.errors_are_target_level(e) is False]
    cleared = [e for e in must_be_target if br.errors_are_target_level(e) is True]

    assert len(reported) == len(must_be_service) == 7
    assert len(cleared) == len(must_be_target) == 2


def test_the_error_types_declare_their_own_verdict():
    """The breaker reads ``service_down`` off the exception, so pin every type."""

    assert feed_delivery._FetchError("boom").service_down is True
    assert feed_delivery._FetchError("miss", service_down=False).service_down is False
    assert feed_delivery._RateLimited(30).service_down is False
    assert feed_delivery._AuthError().service_down is False
    assert feed_delivery._GoneError().service_down is False
    assert br.CircuitOpen("feed", 12.0).service_down is False


# ---------------------------------------------------------------------------
# What trips it, through the REAL poller fetch.
# ---------------------------------------------------------------------------
async def _airing_graphql(session, clock, times=1):
    """Drive ``AniListAiring._graphql`` ``times`` times; return (bot, outcomes)."""

    cog = object.__new__(ai.AniListAiring)
    cog.bot = _bot(session, clock)
    outcomes = []
    for _ in range(times):
        try:
            outcomes.append(("ok", await cog._graphql("query { ok }", {})))
        except Exception as exc:  # noqa: BLE001 - the outcome IS the assertion
            outcomes.append(("raised", exc))
    return cog.bot, outcomes


async def test_the_disabled_403_trips_the_breaker():
    clock = _Clock()
    session = _Session(_Response(403, DISABLED_403_BODY))

    bot, outcomes = await _airing_graphql(session, clock, br.FAILURE_THRESHOLD)

    assert [kind for kind, _v in outcomes] == ["raised"] * br.FAILURE_THRESHOLD
    assert all(isinstance(v, feed_delivery._FetchError) for _k, v in outcomes)
    assert all(v.service_down is True for _k, v in outcomes)
    assert br.breaker_state(bot).is_open() is True
    assert len(session.calls) == br.FAILURE_THRESHOLD  # and no request after


async def test_the_disabled_403_trips_it_even_without_a_per_error_status():
    clock = _Clock()
    session = _Session(_Response(403, DISABLED_403_BODY_NO_STATUS))

    bot, _outcomes = await _airing_graphql(session, clock, br.FAILURE_THRESHOLD)

    assert br.breaker_state(bot).is_open() is True


async def test_a_5xx_with_no_json_body_trips_the_breaker():
    class _NoJson(_Response):
        async def json(self):
            raise ValueError("not json")

    clock = _Clock()
    session = _Session(_NoJson(503, None))

    bot, _outcomes = await _airing_graphql(session, clock, br.FAILURE_THRESHOLD)

    assert br.breaker_state(bot).is_open() is True


async def test_a_timeout_trips_the_breaker():
    class _Boom(_Session):
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            raise TimeoutError()

    clock = _Clock()
    bot, _outcomes = await _airing_graphql(_Boom(), clock, br.FAILURE_THRESHOLD)

    assert br.breaker_state(bot).is_open() is True


async def test_a_429_never_trips_the_breaker():
    """A 429 keeps its own Retry-After embargo; the breaker stays out of it."""

    clock = _Clock()
    session = _Session(_Response(429, {"errors": []}, {"Retry-After": "30"}))

    bot, outcomes = await _airing_graphql(session, clock, br.FAILURE_THRESHOLD * 3)

    assert all(isinstance(v, feed_delivery._RateLimited) for _k, v in outcomes)
    assert all(v.retry_after == 30 for _k, v in outcomes)
    breaker = br.breaker_state(bot)
    assert breaker.is_open() is False
    assert breaker.opens == 0
    # Every one of them went out on the wire: the 429 path is untouched.
    assert len(session.calls) == br.FAILURE_THRESHOLD * 3


async def test_a_not_found_for_one_account_never_trips_the_breaker():
    clock = _Clock()
    session = _Session(_Response(200, NOT_FOUND_BODY))

    bot, outcomes = await _airing_graphql(session, clock, br.FAILURE_THRESHOLD * 3)

    assert all(v.service_down is False for _k, v in outcomes)
    assert br.breaker_state(bot).is_open() is False


async def test_a_graphql_miss_that_still_carries_data_is_a_normal_result():
    """A null field plus an error is what a private profile looks like."""

    clock = _Clock()
    body = {"data": {"MediaListCollection": None}, "errors": [{"status": 404}]}
    session = _Session(_Response(200, body))

    bot, outcomes = await _airing_graphql(session, clock, br.FAILURE_THRESHOLD * 3)

    assert [kind for kind, _v in outcomes] == ["ok"] * (br.FAILURE_THRESHOLD * 3)
    assert br.breaker_state(bot).is_open() is False


async def test_an_exception_from_our_own_code_cannot_mute_the_pollers():
    """A bug in this repository is not evidence that AniList is down."""

    clock = _Clock()
    bot = _bot(None, clock)

    async def _explode():
        raise KeyError("a parsing bug of ours")

    for _ in range(br.FAILURE_THRESHOLD * 3):
        with pytest.raises(KeyError):
            await br.guarded_request(bot, "feed", _explode)

    assert br.breaker_state(bot).is_open() is False
    assert br.breaker_state(bot).opens == 0


async def test_a_mangadex_failure_leaves_the_anilist_breaker_closed():
    """Chapters talk to two services; only one of them is behind this breaker."""

    clock = _Clock()
    cog = object.__new__(ch.AniListChapters)
    cog.bot = _bot(_Session(_Response(503, None)), clock)

    for _ in range(br.FAILURE_THRESHOLD * 3):
        with pytest.raises(feed_delivery._FetchError):
            await cog._mangadex_get("https://api.mangadex.org/manga", [], {})

    assert br.breaker_state(cog.bot).is_open() is False


# ---------------------------------------------------------------------------
# ONE breaker, shared by the three pollers.
# ---------------------------------------------------------------------------
async def test_the_three_pollers_share_one_breaker():
    clock = _Clock()
    bot = _bot(_Session(_Response(403, DISABLED_403_BODY)), clock)

    feed = object.__new__(fd.AniListFeed)
    airing = object.__new__(ai.AniListAiring)
    chapters = object.__new__(ch.AniListChapters)
    feed.bot = airing.bot = chapters.bot = bot

    # The FEED is the only one that fails; it polls fastest, so in a real outage
    # it is the one that learns first.
    for _ in range(br.FAILURE_THRESHOLD):
        with pytest.raises(feed_delivery._FetchError):
            await feed._poller_graphql("query { ok }", {})
    calls_after_feed = len(bot.http_session.calls)

    # ...and the other two are already off the network, without ever having
    # failed themselves.
    with pytest.raises(br.CircuitOpen):
        await airing._graphql("query { ok }", {})
    with pytest.raises(br.CircuitOpen):
        await chapters._graphql("query { ok }", {})

    assert len(bot.http_session.calls) == calls_after_feed
    assert br.breaker_for(bot) is br.breaker_state(bot)


def test_every_poller_gets_the_same_object_from_the_accessor():
    bot = _bot()
    first = br.breaker_for(bot)
    assert br.breaker_for(bot) is first
    assert br.breaker_for(bot) is first
    # A different bot is a different circuit.
    assert br.breaker_for(_bot()) is not first


def test_the_read_only_accessor_never_creates_one():
    bot = _bot()
    assert br.breaker_state(bot) is None
    assert getattr(bot, br.BOT_ATTR, None) is None


# ---------------------------------------------------------------------------
# The tick: no network, no database, no log line - with the positive control
# that the same harness DOES all three when the circuit is closed.
# ---------------------------------------------------------------------------
def _airing_tick_cog(session, clock):
    """A real AniListAiring tick over recorded seams, no task loop and no pool."""

    cog = object.__new__(ai.AniListAiring)
    cog.bot = _bot(session, clock)
    cog._embargo_until = 0
    cog._list_cache = {}
    cog._list_fail_counts = {}
    cog._missing_wheel_after = None
    cog._stale_wheel_after = None
    cog._spaced = False
    cog._req_count = 0
    db = []

    async def _load_optins():
        db.append("optins")
        return [{"anilist_user_id": 7, "user_id": 42}]

    async def _load_channel_subs():
        db.append("channel_subs")
        return []

    async def _load_cursor():
        db.append("cursor")
        return 1_700_000_000

    async def _save_cursor(value):
        db.append("save_cursor")

    async def _no_space():
        cog._req_count += 1

    cog._load_optins = _load_optins
    cog._load_channel_subs = _load_channel_subs
    cog._load_cursor = _load_cursor
    cog._save_cursor = _save_cursor
    cog._space = _no_space
    return cog, db


async def test_a_closed_circuit_really_does_poll(caplog):
    """The POSITIVE CONTROL for the silence test below.

    Without this, a harness that stopped driving the tick at all would make the
    "no request, no query, no line" assertions pass for the wrong reason.
    """

    clock = _Clock()
    session = _Session(_Response(403, DISABLED_403_BODY))
    cog, db = _airing_tick_cog(session, clock)

    with caplog.at_level(logging.INFO):
        for _ in range(br.FAILURE_THRESHOLD):
            cog._spaced = False
            await cog._tick()

    assert db.count("optins") == br.FAILURE_THRESHOLD  # the database WAS read
    assert len(session.calls) == br.FAILURE_THRESHOLD  # AniList WAS called
    assert caplog.records  # and it WAS noisy
    assert br.breaker_state(cog.bot).is_open() is True


async def test_an_open_circuit_skips_the_tick_with_no_request_no_query_no_line(caplog):
    clock = _Clock()
    session = _Session(_Response(403, DISABLED_403_BODY))
    cog, db = _airing_tick_cog(session, clock)

    # Open it the way production does: through real failing ticks.
    for _ in range(br.FAILURE_THRESHOLD):
        cog._spaced = False
        await cog._tick()
    calls_at_open = len(session.calls)
    queries_at_open = len(db)
    caplog.clear()  # the opening lines are the subject of another test

    # Now the outage. Ticks keep firing inside the backoff window (the probe
    # that ENDS a window is a different behaviour, budgeted further down);
    # nothing at all must come out of a skipped one.
    with caplog.at_level(logging.DEBUG):
        for _ in range(50):
            cog._spaced = False
            await cog._tick()
            clock.advance(1.0)
    assert br.breaker_state(cog.bot).seconds_left() > 0  # still inside it

    assert len(session.calls) - calls_at_open == 0
    assert len(db) - queries_at_open == 0
    assert [r.getMessage() for r in caplog.records] == []


async def test_a_skipped_tick_never_counts_against_a_users_own_failure_budget():
    """The airing escape hatch must not fire during a service-wide outage.

    Three straight per-user failures cache that user's watch-list EMPTY so one
    dead account cannot hold the global cursor. A refused request is not that
    user's failure: if it counted, a service outage would empty every tracked
    user's list and release the cursor over a union missing everybody.
    """

    clock = _Clock()
    session = _Session(_Response(403, DISABLED_403_BODY))
    cog, _db = _airing_tick_cog(session, clock)

    for _ in range(200):
        cog._spaced = False
        await cog._tick()
        clock.advance(ai.POLL_SECONDS)

    assert br.breaker_state(cog.bot).is_open() is True
    assert cog._list_cache == {}  # nobody was cached EMPTY
    assert cog._list_fail_counts.get(7, 0) < ai.LIST_FAIL_THRESHOLD


async def test_the_dead_account_hatch_still_fires_when_anilist_is_fine():
    """The COUNTER-TEST for the fork above: only an OUTAGE is exempted.

    A single deleted account, with AniList answering everybody else perfectly,
    must still be cached EMPTY after LIST_FAIL_THRESHOLD tries - otherwise one
    dead profile holds the global airing cursor for every guild, which is the
    whole reason that hatch exists.
    """

    clock = _Clock()
    cog = object.__new__(ai.AniListAiring)
    cog.bot = _bot(None, clock)
    cog._list_cache = {}
    cog._list_fail_counts = {}
    cog._missing_wheel_after = None
    cog._stale_wheel_after = None
    cog._req_count = 0

    async def _no_space():
        cog._req_count += 1

    async def _dead_account(_aid):
        raise feed_delivery._FetchError("Not Found", service_down=False)

    cog._space = _no_space
    cog._fetch_public_list = _dead_account

    for _ in range(ai.LIST_FAIL_THRESHOLD):
        cog._spaced = False
        await cog._refresh_lists({7}, now=1000.0)

    assert br.breaker_state(cog.bot).tripped is False  # AniList was never blamed
    assert cog._list_cache_get(7) == {}  # ...and the hatch did its job


def _chapters_tick_cog(clock):
    """A real AniListChapters tick over recorded seams; MangaDex is the payload.

    Four DM opt-ins: user 7 has a CACHED reading list (so the tick has something
    to poll even with AniList refusing), users 8-10 are never-cached, which is
    what makes the refresh burst fail three times and trip the breaker MID-tick.
    """

    cog = object.__new__(ch.AniListChapters)
    cog.bot = _bot(_Session(_Response(403, DISABLED_403_BODY)), clock)
    cog._embargo_until = 0
    cog._list_cache = {7: (clock.now, {101: {"id": 101, "title": {"romaji": "M"}}})}
    cog._missing_wheel_after = None
    cog._stale_wheel_after = None
    cog._feed_wheel_after = None
    cog._spaced = False
    cog._req_count = 0
    mangadex = []

    async def _no_space():
        cog._req_count += 1

    async def _load_dm_optins():
        return [{"user_id": 40 + aid, "anilist_user_id": aid} for aid in (7, 8, 9, 10)]

    async def _load_channel_subs():
        return []

    async def _load_mappings(_union):
        return {101: {"status": "found", "mangadex_id": "md-101"}}

    async def _resolve_new_mappings(*_args):
        return True

    async def _load_dm_languages(_users):
        return {}

    async def _load_chapter_cursor(_mangadex_id):
        return None

    async def _fetch_feed(mangadex_id, _cursor, _languages=None):
        mangadex.append(mangadex_id)
        return []

    async def _process_manga(*_args):
        return None

    cog._space = _no_space
    cog._load_dm_optins = _load_dm_optins
    cog._load_channel_subs = _load_channel_subs
    cog._load_mappings = _load_mappings
    cog._resolve_new_mappings = _resolve_new_mappings
    cog._load_dm_languages = _load_dm_languages
    cog._load_chapter_cursor = _load_chapter_cursor
    cog._fetch_feed = _fetch_feed
    cog._process_manga = _process_manga
    return cog, mangadex


async def test_the_chapters_tick_reaches_mangadex_through_an_anilist_outage():
    """The tick that has TWO services must only lose the one that is down."""

    clock = _Clock()
    cog, mangadex = _chapters_tick_cog(clock)

    # Tick 1: the breaker trips PART WAY through the AniList refresh burst.
    await cog._tick()
    assert br.breaker_state(cog.bot).is_open() is True
    assert mangadex == ["md-101"]  # MangaDex was still polled

    # Tick 2: the circuit is open from the start, so the refresh is skipped
    # outright - and MangaDex is polled again off the cached list.
    await cog._tick()
    assert mangadex == ["md-101", "md-101"]


async def test_chapters_keep_polling_mangadex_while_anilist_is_down():
    """AniList only says WHICH manga; MangaDex is where the chapters come from."""

    clock = _Clock()
    cog = object.__new__(ch.AniListChapters)
    cog.bot = _bot(_Session(_Response(403, DISABLED_403_BODY)), clock)
    cog._list_cache = {}
    cog._missing_wheel_after = None
    cog._stale_wheel_after = None
    cog._spaced = False
    cog._req_count = 0

    async def _no_space():
        cog._req_count += 1

    cog._space = _no_space

    # Open the circuit through the real refresh path.
    for _ in range(br.FAILURE_THRESHOLD):
        cog._spaced = False
        await cog._refresh_lists({7}, now=1000.0)
    assert br.breaker_state(cog.bot).is_open() is True

    # The refresh is now a silent no-op that does NOT raise into the tick, so
    # everything after it in _tick (the MangaDex half) still runs.
    calls_at_open = len(cog.bot.http_session.calls)
    for _ in range(10):
        cog._spaced = False
        await cog._refresh_lists({7}, now=1000.0)
    assert len(cog.bot.http_session.calls) == calls_at_open


def _feed_tick_cog(session, clock):
    """A real AniListFeed tick over recorded seams, no task loop and no pool."""

    cog = object.__new__(fd.AniListFeed)
    cog.bot = _bot(session, clock)
    cog._embargo_until = 0
    db = []

    async def _prune_coalesce_posts():
        db.append("prune")

    async def _load_feeds():
        db.append("feeds")
        return [{"guild_id": 1, "channel_id": 2, "types": None}]

    async def _load_follows():
        db.append("follows")
        return [{"guild_id": 1, "channel_id": 2, "anilist_user_id": 7}]

    async def _load_state():
        db.append("state")
        return (10, 1_700_000_000)

    async def _save_state(_new_id, _new_created):
        db.append("save_state")

    cog._prune_coalesce_posts = _prune_coalesce_posts
    cog._load_feeds = _load_feeds
    cog._load_follows = _load_follows
    cog._load_state = _load_state
    cog._save_state = _save_state
    return cog, db


async def test_the_feed_fetch_is_the_one_behind_the_breaker():
    """``_fetch_activities`` must use the GATED entry point, not the bare one.

    Both exist on this cog by design (the admin searches need the ungated one),
    which is exactly the pair that can be wired up the wrong way round without
    anything looking odd.
    """

    clock = _Clock()
    bot = _bot(_Session(_Response(200, {"data": {"Page": {"activities": []}}})), clock)
    feed = object.__new__(fd.AniListFeed)
    feed.bot = bot
    _fail(br.breaker_for(bot), br.FAILURE_THRESHOLD)

    with pytest.raises(br.CircuitOpen):
        await feed._fetch_activities([1, 2, 3], 1_700_000_000)

    assert bot.http_session.calls == []


async def test_a_closed_circuit_really_does_run_the_feed_tick():
    """POSITIVE CONTROL for the feed gate: the harness does poll when healthy."""

    clock = _Clock()
    session = _Session(_Response(403, DISABLED_403_BODY))
    cog, db = _feed_tick_cog(session, clock)

    await cog._tick()

    assert db.count("feeds") == 1
    assert len(session.calls) == 1


async def test_an_open_circuit_skips_the_feed_tick_before_the_database():
    clock = _Clock()
    session = _Session(_Response(403, DISABLED_403_BODY))
    cog, db = _feed_tick_cog(session, clock)
    _fail(br.breaker_for(cog.bot), br.FAILURE_THRESHOLD)

    for _ in range(20):
        await cog._tick()

    assert db == []  # not even the coalesce prune, which is pure bookkeeping
    assert session.calls == []


# ---------------------------------------------------------------------------
# Log behaviour: ONE warning per outage, one INFO when it heals.
# ---------------------------------------------------------------------------
def _breaker_records(caplog, level=None):
    return [
        rec
        for rec in caplog.records
        if rec.name == BREAKER_LOGGER and (level is None or rec.levelno == level)
    ]


async def test_one_warning_when_it_opens_and_one_info_when_it_closes(caplog):
    clock = _Clock()
    session = _Session(_Response(403, DISABLED_403_BODY))
    cog = object.__new__(ai.AniListAiring)
    cog.bot = _bot(session, clock)

    with caplog.at_level(logging.DEBUG, logger=BREAKER_LOGGER):
        # The run that opens it.
        for _ in range(br.FAILURE_THRESHOLD):
            with pytest.raises(feed_delivery._FetchError):
                await cog._graphql("query { ok }", {})

        warnings = _breaker_records(caplog, logging.WARNING)
        assert len(warnings) == 1
        opened = warnings[0].getMessage()
        assert "circuit OPEN" in opened
        assert DISABLED_MESSAGE in opened  # the REASON is in the line
        assert "240" in opened  # ...and how long it will stay off

        # Two more failed probes: the outage continues, the WARNING does not.
        for _ in range(2):
            clock.advance(br.breaker_state(cog.bot).seconds_left())
            with pytest.raises(feed_delivery._FetchError):
                await cog._graphql("query { ok }", {})
        assert len(_breaker_records(caplog, logging.WARNING)) == 1

        # Twenty-eight minutes into the outage, the service answers again.
        clock.advance(br.breaker_state(cog.bot).seconds_left())
        session.response = _Response(200, {"data": {"ok": True}})
        assert await cog._graphql("query { ok }", {}) == {"data": {"ok": True}}

    closing = [
        rec.getMessage()
        for rec in _breaker_records(caplog, logging.INFO)
        if "CLOSED" in rec.getMessage()
    ]
    assert len(closing) == 1
    # 240 open + 480 + 960 waited out = 1680s of outage, reported as such.
    assert "1680s open" in closing[0]
    assert br.breaker_state(cog.bot).tripped is False


async def test_the_open_warning_says_which_failure_it_was(caplog):
    """A timeout's ``str()`` is empty, so the type name has to carry the line."""

    class _Boom(_Session):
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            raise TimeoutError()

    clock = _Clock()
    cog = object.__new__(ai.AniListAiring)
    cog.bot = _bot(_Boom(), clock)

    with caplog.at_level(logging.WARNING, logger=BREAKER_LOGGER):
        for _ in range(br.FAILURE_THRESHOLD):
            with pytest.raises(feed_delivery._FetchError):
                await cog._graphql("query { ok }", {})

    warnings = _breaker_records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "TimeoutError" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# The health line.
# ---------------------------------------------------------------------------
def _health(bot):
    cog = object.__new__(health.Health)
    cog.bot = bot
    return cog


def test_the_health_line_shows_the_breaker_open_and_closed():
    clock = _Clock()
    bot = _bot(None, clock)
    bot.get_cog = lambda _name: None
    cog = _health(bot)

    closed = health.format_load_line(
        pool_size=1, pool_idle=1, pool_max=10,
        quota_stats=None, webhook_stats=None,
        anilist_stats=cog._anilist_stats(),
        gw_resumes=0, gw_disconnects=0,
    )
    assert "breaker=closed" in closed
    assert "breaker_left" not in closed

    _fail(br.breaker_state(bot), br.FAILURE_THRESHOLD)
    clock.advance(40.0)
    open_line = health.format_load_line(
        pool_size=1, pool_idle=1, pool_max=10,
        quota_stats=None, webhook_stats=None,
        anilist_stats=cog._anilist_stats(),
        gw_resumes=0, gw_disconnects=0,
    )
    assert "breaker=open" in open_line
    assert "breaker_left=200" in open_line
    assert "breaker_opens=1" in open_line
    assert "breaker_skipped=0" in open_line


def test_the_health_line_keeps_the_trace_after_the_outage_heals():
    clock = _Clock()
    bot = _bot(None, clock)
    bot.get_cog = lambda _name: None
    breaker = br.breaker_state(bot)
    _fail(breaker, br.FAILURE_THRESHOLD)
    with pytest.raises(br.CircuitOpen):
        breaker.require_closed("airing")
    clock.advance(br.OPEN_BASE_SECONDS)
    breaker.require_closed("feed")
    breaker.note_success("feed")

    fields = _health(bot)._anilist_stats()

    assert fields["breaker"] == "closed"
    assert fields["breaker_opens"] == 1  # it HAPPENED, and the morning can see it
    assert fields["breaker_skipped"] == 1
    assert "breaker_left" not in fields


def test_the_health_line_stays_open_through_the_half_open_gap():
    """The gap between "the wait elapsed" and "somebody probed" is still an outage.

    The gate has to say "a request is allowed now" in that window or no probe
    ever happens, but the operator's line must not read all-clear on the
    strength of a wait running out. Two different questions, two properties.
    """

    clock = _Clock()
    bot = _bot(None, clock)
    bot.get_cog = lambda _name: None
    breaker = br.breaker_state(bot)
    _fail(breaker, br.FAILURE_THRESHOLD)
    clock.advance(br.OPEN_BASE_SECONDS)

    assert breaker.is_open() is False  # the gate lets the probe through...

    fields = _health(bot)._anilist_stats()
    assert fields["breaker"] == "open"  # ...and the operator still reads OPEN
    assert fields["breaker_left"] == 0


def test_the_health_read_does_not_create_the_breaker():
    bot = types.SimpleNamespace(get_cog=lambda _name: None)

    assert _health(bot)._anilist_stats() is None
    assert br.breaker_state(bot) is None


def test_the_health_line_still_reports_the_throttle_when_no_poller_ran():
    """The two halves of ``anilist=`` degrade independently."""

    throttle_cog = types.SimpleNamespace(_throttle=None)
    base = AniListBase(types.SimpleNamespace(http_session=None))
    throttle_cog._throttle = base._throttle
    bot = types.SimpleNamespace(get_cog=lambda name: throttle_cog)

    fields = _health(bot)._anilist_stats()

    assert fields["throttled_429"] == 0
    assert "breaker" not in fields  # no poller has ever run on this bot


# ---------------------------------------------------------------------------
# The monitor must outlive the thing it monitors.
#
# ``cogs.anilist.breaker`` is a leaf module, but ``cogs.anilist`` is a PACKAGE
# whose __init__ pulls the whole AniList cog in. A module-scope
# ``from cogs.anilist.breaker import ...`` in cogs/system/health.py therefore
# makes an import fault ANYWHERE in AniList take the health cog - and with it
# the LOAD line, the surface an operator reads to find out something is broken -
# down as well. This was measured, not guessed: with that import at module scope
# the first probe below RAISED.
# ---------------------------------------------------------------------------
_POISON_PROBE = '''
import sys, importlib, importlib.abc


class Poison(importlib.abc.MetaPathFinder):
    """Makes exactly one module name unimportable, and counts the attempts."""

    def __init__(self, target):
        self.target = target
        self.hits = 0

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.target:
            self.hits += 1
            raise ImportError("simulated import fault in " + fullname)
        return None


poison = Poison(sys.argv[1])
sys.meta_path.insert(0, poison)
try:
    importlib.import_module(sys.argv[2])
except ImportError:
    print("RAISED %d" % poison.hits)
else:
    print("CLEAN %d" % poison.hits)
'''


def _import_under_poison(broken_module, imported_module):
    """Import ``imported_module`` in a FRESH interpreter with one module broken.

    Returns ``(raised, hits)``: whether the import blew up, and how many times
    the broken name was actually reached. A subprocess because the claim is
    about import time, and re-importing half of cogs/ inside the running test
    session would leave duplicate module objects behind for everyone else.
    """

    import os
    import subprocess
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))
    env = dict(os.environ, PYTHONPATH=repo_root, PYTHONIOENCODING="utf-8")
    out = subprocess.run(
        [sys.executable, "-c", _POISON_PROBE, broken_module, imported_module],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert out.returncode == 0, out.stderr
    verdict, hits = out.stdout.strip().split()
    return verdict == "RAISED", int(hits)


def test_the_poison_probe_really_does_break_an_import():
    """The detector, aimed at the case it MUST report.

    Importing the breaker BY NAME runs ``cogs/anilist/__init__.py`` first, so
    the fault is reached and the import dies. Without this control the guard
    below could pass because the harness is broken rather than because the
    health cog is clean.
    """

    raised, hits = _import_under_poison("cogs.anilist.feed", "cogs.anilist.breaker")

    assert raised is True
    assert hits == 1  # reached exactly once, so the count means something


def test_the_poison_probe_can_kill_the_health_cog_when_aimed_at_its_own_import():
    """Second control: this harness CAN make importing health fail.

    Aimed at something health really does import at module scope, so a clean
    result in the guard below is a fact about health's imports and not about a
    subprocess that silently swallows everything.
    """

    raised, hits = _import_under_poison("discord.ext.tasks", "cogs.system.health")

    assert raised is True
    assert hits >= 1


def test_an_anilist_import_fault_does_not_take_the_health_cog_with_it():
    """The guard. Health reads the breaker; it must not DEPEND on AniList."""

    raised, hits = _import_under_poison("cogs.anilist.feed", "cogs.system.health")

    assert raised is False
    assert hits == 0, "importing the health cog dragged the AniList package in"


def test_the_load_line_still_forms_when_the_breaker_will_not_import(monkeypatch):
    """And at RUN time: a breaker that will not load drops its own fields only.

    ``None`` in ``sys.modules`` is what CPython uses to mark a module as
    unimportable, so this is the real failure mode of the deferred import rather
    than a stand-in for it.
    """

    import sys

    clock = _Clock()
    bot = _bot(None, clock)  # a breaker exists on the bot
    bot.get_cog = lambda _name: None
    _fail(br.breaker_state(bot), br.FAILURE_THRESHOLD)
    cog = _health(bot)

    # CONTROL: with the import working, the breaker fields are there.
    assert "breaker" in cog._anilist_stats()

    monkeypatch.setitem(sys.modules, "cogs.anilist.breaker", None)

    stats = cog._anilist_stats()
    line = health.format_load_line(
        pool_size=1, pool_idle=1, pool_max=10,
        quota_stats=None, webhook_stats=None,
        anilist_stats=stats,
        gw_resumes=3, gw_disconnects=1,
    )

    assert stats is None  # nothing else was readable on this bot either
    assert "gw_resumes=3" in line  # ...and the rest of the line survived


# ---------------------------------------------------------------------------
# The interactive surface stays OUT of the breaker (deliberate carve-out).
# ---------------------------------------------------------------------------
async def test_a_failing_lookup_command_never_trips_the_poller_breaker():
    clock = _Clock()
    bot = _bot(_Session(_Response(403, DISABLED_403_BODY)), clock)
    base = AniListBase(bot)

    for _ in range(br.FAILURE_THRESHOLD * 3):
        await base._graphql("query { ok }", {})

    assert br.breaker_state(bot).is_open() is False
    assert len(bot.http_session.calls) == br.FAILURE_THRESHOLD * 3


async def test_an_open_circuit_still_lets_a_person_run_a_command():
    """A remote outage must not look like a bot that stopped answering."""

    clock = _Clock()
    bot = _bot(_Session(_Response(200, {"data": {"ok": True}})), clock)
    _fail(br.breaker_for(bot), br.FAILURE_THRESHOLD)
    base = AniListBase(bot)

    assert await base._graphql("query { ok }", {}) == {"data": {"ok": True}}
    assert len(bot.http_session.calls) == 1


async def test_the_admin_feed_search_is_not_behind_the_breaker():
    """``AniListFeed._graphql`` is shared with the poller; only one is gated."""

    clock = _Clock()
    bot = _bot(_Session(_Response(403, DISABLED_403_BODY)), clock)
    feed = object.__new__(fd.AniListFeed)
    feed.bot = bot

    for _ in range(br.FAILURE_THRESHOLD * 3):
        with pytest.raises(feed_delivery._FetchError):
            await feed._graphql("query { ok }", {})
    assert br.breaker_state(bot).is_open() is False

    # The SAME failing response through the poller entry point does trip it.
    for _ in range(br.FAILURE_THRESHOLD):
        with pytest.raises(feed_delivery._FetchError):
            await feed._poller_graphql("query { ok }", {})
    assert br.breaker_state(bot).is_open() is True

    # ...and the admin search is still served while it is open.
    calls_before = len(bot.http_session.calls)
    with pytest.raises(feed_delivery._FetchError):
        await feed._graphql("query { ok }", {})
    assert len(bot.http_session.calls) == calls_before + 1


# ---------------------------------------------------------------------------
# The outage request budget, as a number rather than a promise.
# ---------------------------------------------------------------------------
def test_a_day_long_outage_costs_about_one_request_per_cap_window():
    """Before: 720 feed + 144 airing failed requests a day. After: this."""

    clock = _Clock()
    breaker = br.AniListBreaker(clock=clock)
    day_ends = clock.now + 86400.0
    requests = 0

    # Every poller tick of a whole day, at the three real periods.
    ticks = sorted(
        [clock.now + n * fd.POLL_SECONDS for n in range(86400 // fd.POLL_SECONDS)]
        + [clock.now + n * ai.POLL_SECONDS for n in range(86400 // ai.POLL_SECONDS)]
        + [clock.now + n * ch.POLL_SECONDS for n in range(86400 // ch.POLL_SECONDS)]
    )
    for when in ticks:
        clock.now = when
        if breaker.is_open():
            continue
        try:
            breaker.require_closed("poller")
        except br.CircuitOpen:
            continue
        requests += 1
        breaker.note_failure("poller", "HTTP 403")

    assert clock.now < day_ends
    # The threshold run, the ramp to the cap, then one probe per capped hour.
    ceiling = br.FAILURE_THRESHOLD + 4 + int(86400 // br.OPEN_CAP_SECONDS)
    assert requests <= ceiling
    assert requests == 29
    assert requests < 864 // 25  # under 4% of what the September outage cost

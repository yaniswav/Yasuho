"""Tests for the AniList 429 diagnostic (audit 2026-10-08).

Goal: when AniList returns HTTP 429, tell whether it is OUR request volume or
AniList's own throttling. Covers the pure :class:`AniListCallRecorder` (60s
window, by-source counts, last-seen rate-limit headers), the
:func:`note_response` 429 log line, a census over every AniList HTTP call
site this cog makes (with a negative control proving the census actually
detects a missing recording), and the ``anilist_60s`` field folded into the
bot-wide LOAD line.

Side-effect free: no real network, no database. Fake aiohttp session mirrors
tests/cogs/test_anilist_throttle.py.
"""

import types

from cogs.anilist import airing as airing_mod
from cogs.anilist import chapters as chapters_mod
from cogs.anilist import feed_delivery
from cogs.anilist.base import AniListBase
from cogs.anilist.feed import AniListFeed
from cogs.anilist.helpers import API_URL
from cogs.anilist.throttle import AniListCallRecorder, note_response, recorder_for
from cogs.system import health
from tools.http import TIMEOUT, get_session


# ---------------------------------------------------------------------------
# Fake aiohttp session (mirrors tests/cogs/test_anilist_throttle.py).
# ---------------------------------------------------------------------------
class _Response:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self.payload = payload if payload is not None else {"data": {}}
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

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Request(self.response)


class _Clock:
    """A hand-cranked monotonic clock for deterministic window tests."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ---------------------------------------------------------------------------
# AniListCallRecorder: pure, clock-injected.
# ---------------------------------------------------------------------------
def test_recorder_trims_events_past_the_60s_window():
    clock = _Clock()
    recorder = AniListCallRecorder(clock=clock)

    recorder.record("feed")
    clock.now += 30
    recorder.record("feed")
    assert recorder.snapshot()["total_60s"] == 2

    # Advancing past the window drops the first event but keeps the second.
    clock.now += 31  # 61s after the first record, 31s after the second
    assert recorder.snapshot()["total_60s"] == 1

    clock.now += 31
    assert recorder.snapshot()["total_60s"] == 0


def test_recorder_counts_by_source():
    clock = _Clock()
    recorder = AniListCallRecorder(clock=clock)

    for _ in range(3):
        recorder.record("feed")
    for _ in range(2):
        recorder.record("airing")
    recorder.record("lookup")

    snap = recorder.snapshot()
    assert snap["total_60s"] == 6
    assert snap["by_source"] == {"feed": 3, "airing": 2, "lookup": 1}


def test_recorder_record_returns_the_previous_headers_not_the_new_ones():
    clock = _Clock()
    recorder = AniListCallRecorder(clock=clock)

    # Nothing seen yet: the first call's "previous" is (None, None).
    previous = recorder.record("feed", limit="30", remaining="29")
    assert previous == (None, None)

    # The second call's "previous" is what the FIRST call just set - not the
    # second call's own (newer) values. This is what lets a 429 handler log
    # "remaining_before" from the LAST GOOD response, not the 429 itself.
    previous = recorder.record("feed", limit="30", remaining="5")
    assert previous == ("30", "29")


def test_recorder_reset_drops_events_and_last_seen_headers():
    recorder = AniListCallRecorder()
    recorder.record("feed", limit="30", remaining="10")
    recorder.reset()
    assert recorder.snapshot() == {"total_60s": 0, "by_source": {}}
    # last-seen headers are cleared too: a fresh record has no "previous".
    assert recorder.record("feed") == (None, None)


# ---------------------------------------------------------------------------
# note_response: the 429 diagnostic log line.
# ---------------------------------------------------------------------------
async def test_note_response_429_logs_one_line_with_parsed_headers(caplog):
    clock = _Clock()
    recorder = AniListCallRecorder(clock=clock)

    # Two prior healthy responses set "remaining_before" for the 429 below.
    note_response(recorder, "feed", {"X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "12"}, 200)
    clock.now += 1
    note_response(recorder, "feed", {"X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "11"}, 200)
    clock.now += 1

    with caplog.at_level("WARNING"):
        note_response(
            recorder,
            "airing",
            {
                "X-RateLimit-Limit": "30",
                "Retry-After": "7",
                "X-RateLimit-Reset": "1700000000",
            },
            429,
        )

    lines = [r.message for r in caplog.records if "ANILIST-429" in r.message]
    assert len(lines) == 1
    line = lines[0]
    assert "source=airing" in line
    assert "ours_60s=3" in line
    assert "by_source=airing:1,feed:2" in line
    assert "remaining_before=11" in line
    assert "limit=30" in line
    assert "retry_after=7" in line
    assert "reset=1700000000" in line


async def test_note_response_429_missing_headers_renders_question_marks(caplog):
    recorder = AniListCallRecorder(clock=_Clock())

    with caplog.at_level("WARNING"):
        note_response(recorder, "chapters", {}, 429)

    lines = [r.message for r in caplog.records if "ANILIST-429" in r.message]
    assert len(lines) == 1
    line = lines[0]
    assert "remaining_before=?" in line
    assert "limit=?" in line
    assert "retry_after=?" in line
    assert "reset=?" in line
    assert "by_source=chapters:1" in line
    assert "ours_60s=1" in line


async def test_note_response_non_429_records_but_never_logs(caplog):
    recorder = AniListCallRecorder(clock=_Clock())

    with caplog.at_level("WARNING"):
        note_response(recorder, "lookup", {"X-RateLimit-Remaining": "20"}, 200)

    assert recorder.snapshot()["total_60s"] == 1
    assert not any("ANILIST-429" in r.message for r in caplog.records)


async def test_note_response_with_no_recorder_is_a_total_noop(caplog):
    # A cog that has not loaded (or an older/partial test wiring) must never
    # make a 429 diagnostic failure, let alone block the request it observes.
    with caplog.at_level("WARNING"):
        note_response(None, "lookup", {}, 429)
    assert caplog.records == []


async def test_note_response_swallows_a_broken_headers_object(caplog):
    # A diagnostic must never break the request it is observing, even if the
    # headers object is unusable for some reason.
    recorder = AniListCallRecorder(clock=_Clock())
    with caplog.at_level("WARNING"):
        note_response(recorder, "lookup", None, 429)  # .get() would raise
    assert any("failed to record" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Census: every AniList HTTP call site records, via the SAME shared recorder
# a production bot wires up (bot.get_cog("AniList")._call_recorder).
# ---------------------------------------------------------------------------
def _wired_bot(response):
    """A bot wired like production: get_cog("AniList") resolves to the ONE
    AniListBase instance that owns ``_call_recorder`` - exactly how
    ``throttle.recorder_for`` finds it from the separate poller cogs and from
    ``feed_delivery``'s module-level ``_authed_graphql``.
    """
    session = _Session(response)
    bot = types.SimpleNamespace(http_session=session)
    base = AniListBase(bot)
    bot.get_cog = lambda name: base if name == "AniList" else None
    return bot, base


async def test_census_every_poller_and_interactive_call_site_records():
    response = _Response(
        200, headers={"X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "29"}
    )
    bot, base = _wired_bot(response)

    # Interactive lookup (AniListBase._graphql).
    await base._graphql("query { ok }", {})

    # Feed poller (AniListFeed._graphql).
    feed = AniListFeed.__new__(AniListFeed)
    feed.bot = bot
    await feed._graphql("query { ok }", {})

    # Airing poller (AniListAiring._graphql_raw).
    airing_cog = object.__new__(airing_mod.AniListAiring)
    airing_cog.bot = bot
    await airing_cog._graphql_raw("query { ok }", {})

    # Chapters poller (AniListChapters._graphql_raw).
    chapters_cog = object.__new__(chapters_mod.AniListChapters)
    chapters_cog.bot = bot
    await chapters_cog._graphql_raw("query { ok }", {})

    # Feed card action, acting as the clicking user (feed_delivery._authed_graphql).
    await feed_delivery._authed_graphql(bot, "tok", "mutation {}", {})

    snap = base._call_recorder.snapshot()
    assert snap["by_source"] == {
        "lookup": 1,
        "feed": 1,
        "airing": 1,
        "chapters": 1,
        "list_action": 1,
    }
    assert snap["total_60s"] == 5


async def test_census_oauth_token_exchange_call_site_records(monkeypatch):
    # _exchange_code also stores the token (crypto + DB) and re-points any
    # existing airing/chapter opt-in; neither is the thing under test here, so
    # storage is stubbed and the viewer payload carries no AniList id (the
    # repoint helpers are then a guaranteed no-op, by their own contract).
    response = _Response(200, payload={"access_token": "tok-123"})
    bot, base = _wired_bot(response)

    async def _noop_store(*args, **kwargs):
        return None

    monkeypatch.setattr(base, "_store_token", _noop_store)

    await base._exchange_code(user_id=1, code="pin")

    snap = base._call_recorder.snapshot()
    assert snap["by_source"].get("oauth") == 1


async def test_negative_control_census_catches_a_missing_recording():
    """Prove the census's by_source check actually detects an unrecorded call
    site: a stand-in poller shaped exactly like a real one but that forgets
    the ``note_response(...)`` line leaves no trace at all - the same
    assertion style the census test above relies on would fail to find it.
    """
    response = _Response(
        200, headers={"X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "29"}
    )
    bot, base = _wired_bot(response)

    async def _broken_poller_graphql(bot):
        # Deliberately omits the note_response(...) call every real AniList
        # call site in this cog has.
        async with get_session(bot).post(
            API_URL, json={}, headers={}, timeout=TIMEOUT
        ) as r:
            return await r.json()

    await _broken_poller_graphql(bot)

    snap = base._call_recorder.snapshot()
    assert snap["total_60s"] == 0  # the broken site left no trace at all
    assert "broken" not in snap["by_source"]
    # A real census asserting the exact by_source set would fail here -
    # exactly the signal that catches a forgotten note_response() call.
    expected_sources = {"lookup", "feed", "airing", "chapters", "list_action"}
    assert set(snap["by_source"]) != expected_sources


def test_recorder_for_degrades_to_none_when_anilist_cog_absent():
    bot = types.SimpleNamespace(get_cog=lambda name: None)
    assert recorder_for(bot) is None


def test_recorder_for_degrades_to_none_without_get_cog():
    bot = types.SimpleNamespace()  # older wiring: no get_cog at all
    assert recorder_for(bot) is None


# ---------------------------------------------------------------------------
# health.py: anilist_60s folded into the LOAD line.
# ---------------------------------------------------------------------------
def _health_cog(bot):
    cog = object.__new__(health.Health)
    cog.bot = bot
    cog.gw_resumes = 0
    cog.gw_disconnects = 0
    return cog


def test_anilist_stats_includes_anilist_60s_when_recorder_present():
    recorder = AniListCallRecorder()
    recorder.record("feed")
    recorder.record("airing")
    anilist = types.SimpleNamespace(_call_recorder=recorder)
    cog = _health_cog(types.SimpleNamespace(get_cog=lambda name: anilist))

    assert cog._anilist_stats() == {"anilist_60s": 2}


def test_anilist_stats_omits_anilist_60s_when_recorder_absent():
    # Older/partial wiring: the cog loaded but never set up the recorder.
    anilist = types.SimpleNamespace()
    cog = _health_cog(types.SimpleNamespace(get_cog=lambda name: anilist))
    assert cog._anilist_stats() is None


async def test_load_line_folds_in_anilist_60s(caplog):
    import logging

    class _Pool:
        def get_size(self):
            return 5

        def get_idle_size(self):
            return 3

        def get_max_size(self):
            return 30

    recorder = AniListCallRecorder()
    recorder.record("feed")
    recorder.record("feed")
    recorder.record("lookup")
    anilist = types.SimpleNamespace(_call_recorder=recorder)

    cog = _health_cog(
        types.SimpleNamespace(
            db_pool=_Pool(),
            get_cog=lambda name: anilist if name == "AniList" else None,
        )
    )
    with caplog.at_level(logging.INFO, logger=health.log.name):
        await health.Health.load_line.coro(cog)

    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("anilist=anilist_60s=3" in r.message for r in infos)

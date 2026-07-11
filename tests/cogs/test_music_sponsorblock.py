"""Unit tests for the pure parts of ``cogs/music/sponsorblock.py``.

The live PUT that configures the SponsorBlock plugin on a Lavalink player cannot
run under pytest (it needs a connected node and a server-side player), so these
cover only what is deterministic without I/O:

* ``DEFAULT_CATEGORIES`` - the exact category set, and the invariant that
  ``filler`` is never included (it marks real content, not an interruption).
* ``categories_path`` - the pure REST-path builder.
* ``apply_categories`` - driven against a hand-built fake ``player``/``node`` that
  records the ``node.send`` call. This exercises the pure decision logic (path,
  body, the retry-once-on-404 behaviour, and the best-effort "never raises"
  contract) with an injected ``retry_delay=0`` so no real time passes.
* ``log_ws_event`` - the event-type filter (SponsorBlock types recognised, others
  ignored) with no exceptions on odd payloads.

``sponsorblock.py`` is deliberately sonolink-free (it duck-types the player and
node), so it imports identically under the stub and real sonolink.
"""

import asyncio

import pytest

from cogs.music import sponsorblock

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id


class _FakeNode:
    """Records send() calls and replays a scripted sequence of outcomes.

    Each entry in ``outcomes`` is either an exception to raise or a value to
    return; ``send`` consumes them in order so a test can script "404 then OK".
    """

    def __init__(self, session_id="sess-1", outcomes=None):
        self._session_id = session_id
        self.outcomes = list(outcomes or [None])
        self.calls = []

    @property
    def session_id(self):
        if self._session_id is None:
            raise RuntimeError("no session id")
        return self._session_id

    async def send(self, method, path, *, json=None, **kwargs):
        self.calls.append((method, path, json))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakePlayer:
    def __init__(self, node, guild_id=42):
        self._node = node
        self.guild = _FakeGuild(guild_id) if guild_id is not None else None

    @property
    def node(self):
        if self._node is None:
            raise RuntimeError("no node")
        return self._node


class _HTTP404(Exception):
    """Stand-in for sonolink's HTTPException: exposes a ``status`` like the real one."""

    status = 404


class _HTTP500(Exception):
    status = 500


# ---------------------------------------------------------------------------
# DEFAULT_CATEGORIES
# ---------------------------------------------------------------------------


def test_default_categories_exact_set():
    assert sponsorblock.DEFAULT_CATEGORIES == (
        "sponsor",
        "selfpromo",
        "interaction",
        "intro",
        "outro",
        "preview",
        "music_offtopic",
    )


def test_default_categories_excludes_filler():
    # filler marks real (if tangential) content; skipping it would cut the video
    # the user asked to hear. It must never be in the default skip set.
    assert "filler" not in sponsorblock.DEFAULT_CATEGORIES


def test_default_categories_is_immutable_tuple():
    assert isinstance(sponsorblock.DEFAULT_CATEGORIES, tuple)


def test_default_categories_no_duplicates():
    cats = sponsorblock.DEFAULT_CATEGORIES
    assert len(set(cats)) == len(cats)


# ---------------------------------------------------------------------------
# categories_path
# ---------------------------------------------------------------------------


def test_categories_path_shape():
    assert (
        sponsorblock.categories_path("abc123", 42)
        == "/sessions/abc123/players/42/sponsorblock/categories"
    )


def test_categories_path_leading_slash():
    # The leading slash is what makes sonolink's REST client prepend "/v4".
    assert sponsorblock.categories_path("s", 1).startswith("/sessions/")


# ---------------------------------------------------------------------------
# apply_categories
# ---------------------------------------------------------------------------


async def test_apply_categories_puts_default_body():
    node = _FakeNode(outcomes=[None])
    ok = await sponsorblock.apply_categories(_FakePlayer(node))
    assert ok is True
    assert len(node.calls) == 1
    method, path, body = node.calls[0]
    assert method == "PUT"
    assert path == "/sessions/sess-1/players/42/sponsorblock/categories"
    assert body == list(sponsorblock.DEFAULT_CATEGORIES)


async def test_apply_categories_custom_categories():
    node = _FakeNode(outcomes=[None])
    ok = await sponsorblock.apply_categories(
        _FakePlayer(node), categories=("sponsor",)
    )
    assert ok is True
    assert node.calls[0][2] == ["sponsor"]


async def test_apply_categories_retries_once_on_404_then_succeeds():
    node = _FakeNode(outcomes=[_HTTP404(), None])
    ok = await sponsorblock.apply_categories(_FakePlayer(node), retry_delay=0)
    assert ok is True
    # One retry: exactly two send attempts.
    assert len(node.calls) == 2


async def test_apply_categories_gives_up_after_second_404():
    node = _FakeNode(outcomes=[_HTTP404(), _HTTP404()])
    ok = await sponsorblock.apply_categories(_FakePlayer(node), retry_delay=0)
    assert ok is False
    # Retry-once means it stops after the second attempt, never a third.
    assert len(node.calls) == 2


async def test_apply_categories_no_retry_on_non_404():
    node = _FakeNode(outcomes=[_HTTP500(), None])
    ok = await sponsorblock.apply_categories(_FakePlayer(node), retry_delay=0)
    assert ok is False
    # A non-404 error is terminal: only the first attempt is made.
    assert len(node.calls) == 1


async def test_apply_categories_swallows_unexpected_exception():
    node = _FakeNode(outcomes=[ValueError("boom")])
    # Best-effort contract: never raises, returns False.
    ok = await sponsorblock.apply_categories(_FakePlayer(node), retry_delay=0)
    assert ok is False


async def test_apply_categories_no_node():
    ok = await sponsorblock.apply_categories(_FakePlayer(None))
    assert ok is False


async def test_apply_categories_no_session_id():
    node = _FakeNode(session_id=None)
    ok = await sponsorblock.apply_categories(_FakePlayer(node))
    assert ok is False
    assert node.calls == []


async def test_apply_categories_no_guild():
    node = _FakeNode(outcomes=[None])
    ok = await sponsorblock.apply_categories(_FakePlayer(node, guild_id=None))
    assert ok is False
    assert node.calls == []


# ---------------------------------------------------------------------------
# log_ws_event
# ---------------------------------------------------------------------------


def test_log_ws_event_ignores_non_sponsorblock():
    # Must not raise on an unrelated unknown event with no sponsorblock fields.
    sponsorblock.log_ws_event(
        _FakePlayer(_FakeNode()), {"type": "SomethingElse"}
    )


def test_log_ws_event_handles_segment_skipped(caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="cogs.music.sponsorblock"):
        sponsorblock.log_ws_event(
            _FakePlayer(_FakeNode()),
            {
                "type": "SegmentSkipped",
                "guildId": "42",
                "segment": {"category": "sponsor", "start": 1000, "end": 2000},
            },
        )
    assert any("SponsorBlock skipped" in r.message for r in caplog.records)


def test_log_ws_event_segment_skipped_missing_segment():
    # A malformed SegmentSkipped (no segment dict) must still not raise.
    sponsorblock.log_ws_event(
        _FakePlayer(_FakeNode()), {"type": "SegmentSkipped"}
    )


def test_log_ws_event_other_sponsorblock_types():
    for event_type in ("SegmentsLoaded", "ChaptersLoaded", "ChapterStarted"):
        sponsorblock.log_ws_event(
            _FakePlayer(_FakeNode()), {"type": event_type}
        )


# ---------------------------------------------------------------------------
# schedule_apply
# ---------------------------------------------------------------------------


async def test_schedule_apply_runs_in_background():
    node = _FakeNode(outcomes=[None])
    task = sponsorblock.schedule_apply(_FakePlayer(node))
    assert task is not None
    result = await task
    assert result is True
    assert len(node.calls) == 1


def test_schedule_apply_no_running_loop_returns_none():
    # Outside an event loop there is nowhere to schedule; degrade to None.
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()
    assert sponsorblock.schedule_apply(_FakePlayer(_FakeNode())) is None

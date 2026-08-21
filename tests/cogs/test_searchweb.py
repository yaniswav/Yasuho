"""Tests for the wikipedia timeout proxy in cogs/utility/searchweb.py.

The wikipedia library issues requests.get() with no timeout; searchweb wraps
its requests reference so a hung upstream cannot tie up an executor thread
forever. These tests are hermetic (no network): they check the proxy's
behaviour and that the wikipedia module actually picked it up on import.
"""

import asyncio
import types

import wikipedia

from cogs.utility import searchweb


def test_timeout_requests_injects_default(monkeypatch):
    captured = {}

    def fake_get(*args, **kwargs):
        captured.update(kwargs)
        return "resp"

    monkeypatch.setattr(searchweb.requests, "get", fake_get)
    proxy = searchweb._TimeoutRequests(15)
    assert proxy.get("http://example/api") == "resp"
    assert captured["timeout"] == 15


def test_timeout_requests_preserves_explicit_timeout(monkeypatch):
    captured = {}

    def fake_get(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(searchweb.requests, "get", fake_get)
    searchweb._TimeoutRequests(15).get("http://example/api", timeout=3)
    assert captured["timeout"] == 3


def test_timeout_requests_forwards_other_attributes():
    proxy = searchweb._TimeoutRequests(15)
    # anything that is not get() falls through to the real requests module.
    assert proxy.exceptions is searchweb.requests.exceptions


def test_wikipedia_module_uses_the_timeout_proxy():
    assert isinstance(wikipedia.wikipedia.requests, searchweb._TimeoutRequests)


# ---------------------------------------------------------------------------
# The blocking wikipedia lookup must not run on the loop's DEFAULT executor.
#
# That pool is the one tools/rendering.py hands every Pillow render to once a
# job has taken one of its two semaphore slots, so a wiki call there both
# escaped the bot-wide image ceiling (a thread without a slot) and could stall
# it (a render holding a slot still queues behind the pool). It gets its own
# bounded pool instead - and NOT run_image_job, which would park a 15s network
# wait in one of only two image slots.
# ---------------------------------------------------------------------------
class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Ctx:
    def __init__(self):
        self.sends = []

    def typing(self):
        return _Typing()

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


class _Loop:
    """Records which executor the cog hands its blocking work to."""

    def __init__(self, result="SUMMARY"):
        self.executors = []
        self._result = result

    async def run_in_executor(self, executor, function):
        self.executors.append(executor)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _make_cog(loop=None):
    loop = _Loop() if loop is None else loop
    cog = searchweb.SearchWeb(types.SimpleNamespace(loop=loop))
    return cog, loop


async def test_wiki_runs_on_the_cogs_own_pool_not_the_default_executor():
    cog, loop = _make_cog()
    ctx = _Ctx()

    await cog.lookup_wiki.callback(cog, ctx, query="Python")

    assert loop.executors == [cog._wiki_pool]
    # None IS the default executor - the bug being fixed.
    assert loop.executors[0] is not None
    assert ctx.sends[0][1]["embed"].description == "SUMMARY"
    cog.cog_unload()


def test_wiki_pool_is_bounded_and_small():
    cog, _ = _make_cog()
    assert searchweb.WIKI_WORKERS <= 4
    assert cog._wiki_pool._max_workers == searchweb.WIKI_WORKERS
    cog.cog_unload()


async def test_wiki_turns_a_crowd_away_instead_of_queueing_forever(monkeypatch):
    """A ThreadPoolExecutor queue is unbounded, and every waiter sits inside
    ctx.typing() hitting the API while it waits - so the wait is bounded."""

    monkeypatch.setattr(searchweb, "WIKI_ACQUIRE_TIMEOUT", 0.01)
    cog, loop = _make_cog()
    ctx = _Ctx()

    for _ in range(searchweb.WIKI_WORKERS):  # every worker busy
        await cog._wiki_slots.acquire()

    await cog.lookup_wiki.callback(cog, ctx, query="Python")

    assert loop.executors == []  # never joined the queue
    args, kwargs = ctx.sends[0]
    assert "embed" not in kwargs  # a short plain answer, not a result
    cog.cog_unload()


async def test_wiki_slot_is_released_when_the_lookup_raises():
    cog, _ = _make_cog(_Loop(result=RuntimeError("boom")))
    ctx = _Ctx()

    await cog.lookup_wiki.callback(cog, ctx, query="Python")

    # A leaked slot per failure would shrink the pool to nothing.
    assert cog._wiki_slots._value == searchweb.WIKI_WORKERS
    cog.cog_unload()


async def test_wiki_concurrency_never_exceeds_the_pool_width():
    running = 0
    peak = 0

    class _SlowLoop:
        async def run_in_executor(self, executor, function):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                await asyncio.sleep(0.01)
                return "SUMMARY"
            finally:
                running -= 1

    cog, _ = _make_cog(_SlowLoop())
    await asyncio.gather(
        *(
            cog.lookup_wiki.callback(cog, _Ctx(), query=f"q{i}")
            for i in range(searchweb.WIKI_WORKERS + 3)
        )
    )

    assert peak <= searchweb.WIKI_WORKERS
    cog.cog_unload()


def test_cog_unload_retires_the_pool():
    cog, _ = _make_cog()
    cog.cog_unload()
    assert cog._wiki_pool._shutdown is True

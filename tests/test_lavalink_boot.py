"""The Lavalink boot/reconnect hole: found by code review, not production.

THE ORIGINAL BUG (confirmed by reading the installed sonolink source, not
observed live). ``core.py`` ``setup_hook`` used to do ``create_node(...)``
then a plain, inline ``await self.sl_client.start()``. sonolink's own retry
loop (``sonolink/gateway/node/_connection.py`` ``attempt_connect``) defaulted
``retries`` to ``None``, which becomes ``itertools.count()`` - an INFINITE
counter - and ``handle_connection_error`` only raises for a handful of fatal
statuses (3000/3003/401 bad password, 1014/404 bad URI); any other handshake
failure, including the 503 Lavalink answers while it is still loading plugins,
was logged and retried forever, sleeping up to 10s between tries. An inline
``await`` on that meant ``setup_hook`` itself could never return.

THE FIRST FIX. ``_start_lavalink`` only registers the node (synchronous) and
hands the actual connect attempts to a background task,
``_supervise_lavalink``, which ``setup_hook`` never awaits.

THE SECOND, DEEPER HOLE (found re-reading the source once more, 2026-09-30): a
REFUSED connection (Lavalink not up yet, or restarting) raises
``aiohttp.ClientConnectorError``, which is not a ``WebSocketError`` -
``AioWebsocketManager.connect`` (``sonolink/network/_aiohttp.py``) only wraps
``aiohttp.WSServerHandshakeError`` that way. So it escapes
``attempt_connect``'s ``except WebSocketError`` on the very first attempt,
regardless of ``retries``, leaving ``NodeStatus.CONNECTING`` set (by
``connect()``/``reconnect()``, BEFORE ``attempt_connect`` runs) with nothing to
reset it - the node is then stuck CONNECTING forever, invisible to the old
supervisor, which treated CONNECTING as healthy. The fix: sonolink now gets a
small, finite ``retries`` (``LAVALINK_NODE_RETRIES``) so an exhausted burst is
at least OBSERVABLE (DISCONNECTED, not an infinite inline await); the
supervisor bounds its own ``start()`` call (``LAVALINK_START_TIMEOUT``) so it
can never block on the escape case either; and it tracks how long a node has
sat CONNECTING, force-closing it past ``LAVALINK_STUCK_AFTER`` so the next
``start()`` begins fresh. A further wrinkle in the EXHAUSTED-reconnect path
(not the escape) - sonolink leaves a stale ``_keep_alive`` reference behind
that would otherwise make every future ``connect()`` silently no-op forever -
is covered by clearing it defensively before every DISCONNECTED retry.

THE RESTORE SIDE (unchanged by the above). ``cogs/music/music.py``'s
cold-restore used to run only from ``on_ready``, checking whether a node
happened to already be connected by then. With the node connecting on its own
schedule, ``on_ready`` and the node's actual connect can land in either order
(or concurrently), so there are two triggers - ``on_ready`` and the new
``on_sonolink_node_ready`` listener - sharing one one-shot guard,
``_maybe_restore``.

Everything here is offline: ``core.Yasuho`` and ``cogs.music.music.Music`` are
built with ``__new__`` (the house pattern - see
``tests/cogs/test_music_node_startup.py``) and fed hand-rolled fakes; nothing
touches a real Lavalink server, Discord gateway, or database. ``asyncio.sleep``
is captured inside the supervisor test by swapping ``core.asyncio`` for a thin
proxy (same shape as the boundary-faking in ``tests/test_tree_sync_startup.py``,
which swaps ``core.config_loader`` / ``core.aiohttp`` / ``core.fixups``), so the
backoff math is asserted without any real waiting. ``asyncio.wait_for`` and
``asyncio.get_running_loop().time()`` are left real (the proxy only overrides
``.sleep``), so the few tests that rely on them use real, sub-second
wall-clock time, never a long wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import types

import pytest
from discord.ext import commands

import core

from cogs.music import music
from tools import music_state

# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def _lavalink_configured_config(uri="http://lavalink.example:2333", password="secret"):
    """The real config, but with [Lavalink] answering as CONFIGURED.

    The committed config/bot.ini ships that section commented out (music
    disabled by default), so these tests supply their own uri/password rather
    than depending on what happens to be on disk.
    """
    real_get = core.config_loader.get

    def get(section, key, *args, **kwargs):
        if section == "Lavalink" and key == "uri":
            return uri
        if section == "Lavalink" and key == "password":
            return password
        return real_get(section, key, *args, **kwargs)

    return types.SimpleNamespace(get=get)


class FakeNode:
    """A sonolink Node stand-in: the flags and the close() the supervisor uses.

    ``_keep_alive`` mirrors the real private attribute the supervisor reaches
    into (node.py's own guard: ``connect()`` refuses to run while it is not
    ``None``) - defaulted to ``None`` like the real Node's ``__init__``, so a
    test only sets it when deliberately simulating the stale-reference bug.
    """

    def __init__(self, node_id):
        self.id = node_id
        self.is_connected = False
        self.is_connecting = False
        self._keep_alive = None
        self.close_calls = 0
        self.close_raises: Exception | None = None

    async def close(self):
        self.close_calls += 1
        if self.close_raises is not None:
            raise self.close_raises
        self.is_connected = False
        self.is_connecting = False
        self._keep_alive = None


class FakeSonolinkClient:
    """Stands in for ``sonolink.Client`` with fully controllable connects.

    ``start_hangs`` makes ``start()`` await an Event that is never set (the
    exact shape of the original bug: a node that never finishes connecting).
    ``start_side_effect`` (if set) is awaited on every ``start()`` call and may
    mutate the node to simulate a connect eventually succeeding.
    """

    def __init__(self):
        self._nodes: dict[str, FakeNode] = {}
        self.create_node_calls: list[dict] = []
        self.start_calls = 0
        self.close_calls = 0
        self.start_hangs = False
        self.start_side_effect = None
        self.close_raises: Exception | None = None
        self._hang_forever = asyncio.Event()

    def create_node(self, **kwargs):
        self.create_node_calls.append(kwargs)
        node = FakeNode(kwargs.get("id"))
        self._nodes[node.id] = node
        return node

    def get_node(self, node_id):
        return self._nodes.get(node_id)

    async def start(self):
        self.start_calls += 1
        if self.start_hangs:
            await self._hang_forever.wait()
            return
        if self.start_side_effect is not None:
            await self.start_side_effect()

    async def close(self):
        self.close_calls += 1
        if self.close_raises is not None:
            raise self.close_raises


class _AsyncioProxy:
    """Swaps out ``asyncio.sleep`` while leaving every other name untouched.

    core.py's supervisor refers to ``asyncio.sleep`` / ``asyncio.CancelledError``
    / ``asyncio.ensure_future`` via the module-level ``asyncio`` name in its own
    namespace, so replacing ``core.asyncio`` with this proxy (rather than
    patching the real, process-wide ``asyncio`` module) is enough, and leaves
    everyone else's ``asyncio.sleep`` alone.
    """

    def __init__(self, real_module, fake_sleep):
        self._real = real_module
        self.sleep = fake_sleep

    def __getattr__(self, name):
        return getattr(self._real, name)


class _StopSupervisor(BaseException):
    """Deliberately not an ``Exception`` - the supervisor must not swallow it.

    Raised from inside the fake ``sleep`` to end the test's otherwise-infinite
    ``while True`` loop at an exact, chosen point.
    """


class _FakeTask:
    """Records ``cancel()`` without needing a real asyncio.Task."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self.closed = True


# ---------------------------------------------------------------------------
# core.py: the launcher never blocks on Lavalink I/O
# ---------------------------------------------------------------------------


async def test_start_lavalink_returns_even_though_the_fake_client_never_connects(
    monkeypatch,
):
    """THE REGRESSION ITSELF. ``start_hangs=True`` reproduces sonolink's
    infinite retry loop exactly: ``start()`` never returns. Bounded with
    ``wait_for`` so a regression FAILS this test instead of hanging the whole
    suite.
    """
    monkeypatch.setattr(core, "config_loader", _lavalink_configured_config())

    bot = core.Yasuho.__new__(core.Yasuho)
    bot._lavalink_task = None
    client = FakeSonolinkClient()
    client.start_hangs = True
    bot.sl_client = client

    try:
        await asyncio.wait_for(bot._start_lavalink(), timeout=2.0)
    finally:
        if bot._lavalink_task is not None:
            bot._lavalink_task.cancel()
            with contextlib.suppress(BaseException):
                await bot._lavalink_task

    assert client.create_node_calls, "the node was never even registered"
    assert bot._lavalink_task is not None, "no supervisor was launched"


async def test_create_node_gets_a_small_finite_retries(monkeypatch):
    """The split: sonolink does SHORT bursts, the supervisor owns LONG-TERM
    retrying.

    ``retries`` used to be left at sonolink's default (``None`` = retry
    forever) specifically because the SAME counter also drives RUNTIME
    reconnects (sonolink/gateway/node/_connection.py ``reconnect`` ->
    ``attempt_connect`` share one ``retries``/counter) - a finite number
    seemed, at first, like it would make a routine Lavalink restart fatal.

    It does not, because of where the bound now lives: with a finite count, an
    exhausted burst (initial connect OR a runtime reconnect) ends in
    attempt_connect's exhausted branch, which sets NodeStatus.DISCONNECTED
    (and, for a runtime reconnect, dispatches "node_close" -
    ``_connection.py`` ~145-152) rather than running forever inside one
    un-observable ``await``. ``_supervise_lavalink`` then re-kicks a
    DISCONNECTED node forever with its own backoff - so a Lavalink restart
    still stays unbounded IN TIME, it is just observable now. ``retries=None``
    was rejected for two reasons: the resulting loop is unobservable from here
    AND blocks the caller for as long as it runs, and - the actual reason this
    changed - it does not even cover a refused connection
    (aiohttp.ClientConnectorError escapes attempt_connect's
    ``except WebSocketError`` regardless of the retries value), which is the
    hole LAVALINK_STUCK_AFTER exists to close.
    """
    monkeypatch.setattr(core, "config_loader", _lavalink_configured_config())

    bot = core.Yasuho.__new__(core.Yasuho)
    bot._lavalink_task = None
    client = FakeSonolinkClient()
    client.start_hangs = True
    bot.sl_client = client

    try:
        await asyncio.wait_for(bot._start_lavalink(), timeout=2.0)
    finally:
        if bot._lavalink_task is not None:
            bot._lavalink_task.cancel()
            with contextlib.suppress(BaseException):
                await bot._lavalink_task

    assert len(client.create_node_calls) == 1
    kwargs = client.create_node_calls[0]
    assert kwargs.get("retries") == core.LAVALINK_NODE_RETRIES
    assert isinstance(kwargs.get("retries"), int) and kwargs["retries"] > 0
    assert kwargs["id"] == music_state.MUSIC_NODE_ID


async def test_start_lavalink_is_a_noop_without_a_configured_uri(monkeypatch):
    """The pre-existing "music disabled" path must survive the refactor."""

    def get(section, key, *args, **kwargs):
        if section == "Lavalink":
            raise KeyError(section)
        raise AssertionError("should not read any other section")

    monkeypatch.setattr(core, "config_loader", types.SimpleNamespace(get=get))

    bot = core.Yasuho.__new__(core.Yasuho)
    bot._lavalink_task = None
    client = FakeSonolinkClient()
    bot.sl_client = client

    await bot._start_lavalink()

    assert client.create_node_calls == []
    assert bot._lavalink_task is None


# ---------------------------------------------------------------------------
# core.py: the supervisor itself
# ---------------------------------------------------------------------------


async def test_supervisor_backs_off_then_stops_retrying_once_connected(
    monkeypatch, caplog
):
    """Retries with doubling backoff while down; one WARNING, one INFO."""

    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    node = client.create_node(id=music_state.MUSIC_NODE_ID, uri="x", password="y")
    bot.sl_client = client

    attempts = {"n": 0}

    async def _start_side_effect():
        attempts["n"] += 1
        if attempts["n"] >= 3:
            node.is_connected = True

    client.start_side_effect = _start_side_effect

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if seconds == core.LAVALINK_CHECK_INTERVAL:
            raise _StopSupervisor()

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with caplog.at_level(logging.INFO, logger=core.log.name):
        with pytest.raises(_StopSupervisor):
            await bot._supervise_lavalink()

    assert client.start_calls == 3, "must stop calling start() once connected"
    assert sleeps == [
        core.LAVALINK_BACKOFF_START,
        core.LAVALINK_BACKOFF_START * 2,
        core.LAVALINK_CHECK_INTERVAL,
    ]

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    recoveries = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "is back" in r.getMessage()
    ]
    assert len(warnings) == 1, "exactly one WARNING per outage streak"
    assert len(recoveries) == 1, "exactly one INFO on recovery"


async def test_supervisor_treats_an_actively_connecting_node_as_healthy(
    monkeypatch, caplog
):
    """The 503-while-loading-plugins case: sonolink is retrying BY ITSELF.

    While ``is_connecting`` is true, start() is a no-op on sonolink's side
    (Client.start skips connected/connecting nodes), so the supervisor must
    not call it and must not log an outage - it just checks back later.
    """
    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    node = client.create_node(id=music_state.MUSIC_NODE_ID, uri="x", password="y")
    node.is_connecting = True
    bot.sl_client = client

    async def fake_sleep(seconds):
        raise _StopSupervisor()

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with caplog.at_level(logging.WARNING, logger=core.log.name):
        with pytest.raises(_StopSupervisor):
            await bot._supervise_lavalink()

    assert client.start_calls == 0
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


async def test_supervisor_moves_on_after_a_start_that_never_returns(
    monkeypatch, caplog
):
    """The escape case during the INITIAL connect: start() must not block
    forever.

    ``start_hangs=True`` is the same shape as
    ``test_start_lavalink_returns_even_though_the_fake_client_never_connects``,
    but exercised here through the supervisor's own bound
    (``asyncio.wait_for(..., timeout=LAVALINK_START_TIMEOUT)``) rather than the
    launcher's. ``LAVALINK_START_TIMEOUT`` is monkeypatched to a few
    milliseconds so this is a real, but tiny, wait - ``asyncio.wait_for`` and
    the fake client's ``asyncio.Event`` are left real (only ``core.asyncio
    .sleep`` is faked), there is nothing else to drive here.
    """
    monkeypatch.setattr(core, "LAVALINK_START_TIMEOUT", 0.01)

    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    client.create_node(id=music_state.MUSIC_NODE_ID, uri="x", password="y")
    client.start_hangs = True
    bot.sl_client = client

    async def fake_sleep(seconds):
        raise _StopSupervisor()

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with caplog.at_level(logging.WARNING, logger=core.log.name):
        with pytest.raises(_StopSupervisor):
            await bot._supervise_lavalink()

    assert client.start_calls == 1, "start() was called, it just never returned"
    timeout_warnings = [
        r
        for r in caplog.records
        if "did not finish connecting" in r.getMessage()
    ]
    assert len(timeout_warnings) == 1


async def test_stuck_connecting_past_threshold_closes_then_restarts(
    monkeypatch, caplog
):
    """The core new behaviour: a node stuck CONNECTING past
    LAVALINK_STUCK_AFTER gets exactly one node.close(), then start() is called
    again on the next iteration - with exactly one WARNING for the streak.

    ``LAVALINK_STUCK_AFTER`` is monkeypatched to 0.0 so that ANY elapsed real
    time (the loop's own monotonic clock, never faked) between two
    iterations counts as "past the threshold" - the first observation of
    CONNECTING always has an elapsed time of exactly 0.0 relative to itself
    (same ``now`` read used for both), so it takes a second iteration to
    trip, which is why this needs at least two CONNECTING passes before the
    close().
    """
    monkeypatch.setattr(core, "LAVALINK_STUCK_AFTER", 0.0)

    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    node = client.create_node(id=music_state.MUSIC_NODE_ID, uri="x", password="y")
    node.is_connecting = True
    bot.sl_client = client

    calls = {"n": 0}

    async def fake_sleep(seconds):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise _StopSupervisor()

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with caplog.at_level(logging.WARNING, logger=core.log.name):
        with pytest.raises(_StopSupervisor):
            await bot._supervise_lavalink()

    assert node.close_calls == 1, "exactly one close() for the stuck streak"
    assert client.start_calls == 1, "the next iteration kicked start() again"
    stuck_warnings = [
        r for r in caplog.records if "stuck CONNECTING" in r.getMessage()
    ]
    assert len(stuck_warnings) == 1, "exactly one WARNING per stuck streak"


async def test_a_long_refusal_outage_warns_once_across_many_resets(
    monkeypatch, caplog
):
    """Lavalink down at boot and refusing: every start() escapes and leaves the
    node stuck CONNECTING, so the supervisor resets it again and again. That
    whole episode is ONE outage: one WARNING, however many resets it takes,
    then one INFO when Lavalink finally answers.
    """
    monkeypatch.setattr(core, "LAVALINK_STUCK_AFTER", 0.0)

    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    node = client.create_node(id=music_state.MUSIC_NODE_ID, uri="x", password="y")
    bot.sl_client = client

    async def refused_then_up():
        # The escape: connect() set CONNECTING, then the refusal bypassed
        # sonolink's retry loop and nothing reset it. The 4th start() works.
        if client.start_calls >= 4:
            node.is_connected = True
        else:
            node.is_connecting = True

    client.start_side_effect = refused_then_up

    calls = {"n": 0}

    async def fake_sleep(seconds):
        calls["n"] += 1
        if node.is_connected:
            raise _StopSupervisor()
        if calls["n"] >= 50:
            raise AssertionError("supervisor never recovered")

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with caplog.at_level(logging.INFO, logger=core.log.name):
        with pytest.raises(_StopSupervisor):
            await bot._supervise_lavalink()

    assert node.close_calls == 3, "one reset per refused attempt"
    assert client.start_calls == 4
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if "is back" in r.getMessage()]
    assert len(warnings) == 1, "one WARNING for the whole outage, not per reset"
    assert len(infos) == 1


async def test_connecting_for_less_than_stuck_after_is_left_alone(monkeypatch):
    """A node still inside a normal connect burst must never be touched.

    Uses the REAL ``LAVALINK_STUCK_AFTER`` (90s): a few fast, faked-sleep
    iterations pass nowhere near that much real wall-clock time, so this
    proves the threshold - not just the mechanism - without waiting.
    """
    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    node = client.create_node(id=music_state.MUSIC_NODE_ID, uri="x", password="y")
    node.is_connecting = True
    bot.sl_client = client

    calls = {"n": 0}

    async def fake_sleep(seconds):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise _StopSupervisor()

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with pytest.raises(_StopSupervisor):
        await bot._supervise_lavalink()

    assert node.close_calls == 0
    assert client.start_calls == 0


async def test_mid_session_stuck_connecting_recovers_to_connected(
    monkeypatch, caplog
):
    """The mid-session scenario end to end: CONNECTED -> the websocket drops
    and sonolink's reconnect() hits the refused-connection escape (simulated
    directly as is_connected=False/is_connecting=True, since that is exactly
    the state reconnect() leaves behind - see _supervise_lavalink's docstring)
    -> detected stuck -> close() resets it -> the next start() succeeds ->
    CONNECTED again, with exactly one WARNING and one recovery INFO for the
    whole episode (the stuck-reset counts as the SAME outage streak as the
    DISCONNECTED gap in between, not a second one).
    """
    monkeypatch.setattr(core, "LAVALINK_STUCK_AFTER", 0.0)

    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    node = client.create_node(id=music_state.MUSIC_NODE_ID, uri="x", password="y")
    node.is_connected = True
    bot.sl_client = client

    async def _start_side_effect():
        node.is_connected = True

    client.start_side_effect = _start_side_effect

    calls = {"n": 0}

    async def fake_sleep(seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            # The healthy CONNECTED sleep just ended - simulate the drop.
            node.is_connected = False
            node.is_connecting = True
        elif seconds == core.LAVALINK_CHECK_INTERVAL and calls["n"] > 2:
            raise _StopSupervisor()

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with caplog.at_level(logging.INFO, logger=core.log.name):
        with pytest.raises(_StopSupervisor):
            await bot._supervise_lavalink()

    assert node.close_calls == 1
    assert client.start_calls == 1
    assert node.is_connected is True

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    recoveries = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "is back" in r.getMessage()
    ]
    assert len(warnings) == 1, "one WARNING for the whole stuck episode"
    assert len(recoveries) == 1, "one INFO once it is really back"


async def test_supervisor_clears_a_stale_keep_alive_before_retrying(monkeypatch):
    """sonolink's reconnect() EXHAUSTING its retries (as opposed to the escape
    case above) never resets ``_keep_alive`` - neither ``reconnect()`` nor
    attempt_connect's exhausted branch touches it (``_connection.py``; only
    ``close()`` does, and close() refuses to run on an already-DISCONNECTED
    node). ``connect()`` refuses to even try again while ``_keep_alive is not
    None`` ("already connected; ignoring", ``_connection.py`` ~49) - so
    without this reset, a mid-session reconnect that outlasts its retries
    would leave the node DISCONNECTED FOREVER, silently (start() just returns
    having skipped the node, no exception, no WARNING of ours).
    """
    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    node = client.create_node(id=music_state.MUSIC_NODE_ID, uri="x", password="y")
    node._keep_alive = object()  # stale: a finished task's leftover reference

    bot.sl_client = client

    async def fake_sleep(seconds):
        raise _StopSupervisor()

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with pytest.raises(_StopSupervisor):
        await bot._supervise_lavalink()

    assert node._keep_alive is None, "the stale reference must be cleared"
    assert client.start_calls == 1, "clearing it must not skip retrying"


async def test_supervisor_survives_an_unexpected_exception_and_keeps_looping(
    monkeypatch,
):
    """An iteration bug must log and continue, not kill the whole supervisor."""
    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    bot.sl_client = client

    calls = {"n": 0}

    def _boom(node_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        raise _StopSupervisor()

    monkeypatch.setattr(client, "get_node", _boom)

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with pytest.raises(_StopSupervisor):
        await bot._supervise_lavalink()

    assert calls["n"] == 2, "one broken iteration, then the loop ran again"


async def test_supervisor_cancellation_propagates(monkeypatch):
    """asyncio.CancelledError must not be swallowed by the broad except."""
    bot = core.Yasuho.__new__(core.Yasuho)
    client = FakeSonolinkClient()
    bot.sl_client = client

    async def fake_sleep(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(core, "asyncio", _AsyncioProxy(asyncio, fake_sleep))

    with pytest.raises(asyncio.CancelledError):
        await bot._supervise_lavalink()


# ---------------------------------------------------------------------------
# core.py: close()
# ---------------------------------------------------------------------------


async def test_close_cancels_the_supervisor_and_closes_the_lavalink_client(
    monkeypatch,
):
    async def _fake_super_close(self):
        return None

    monkeypatch.setattr(commands.Bot, "close", _fake_super_close)

    bot = core.Yasuho.__new__(core.Yasuho)
    task = _FakeTask()
    bot._lavalink_task = task
    client = FakeSonolinkClient()
    bot.sl_client = client
    session = _FakeSession()
    bot.http_session = session

    await bot.close()

    assert task.cancelled is True
    assert client.close_calls == 1
    assert session.close_calls == 1


async def test_close_still_closes_http_session_when_sl_client_close_raises(
    monkeypatch,
):
    async def _fake_super_close(self):
        return None

    monkeypatch.setattr(commands.Bot, "close", _fake_super_close)

    bot = core.Yasuho.__new__(core.Yasuho)
    bot._lavalink_task = None
    client = FakeSonolinkClient()
    client.close_raises = RuntimeError("Lavalink socket already gone")
    bot.sl_client = client
    session = _FakeSession()
    bot.http_session = session

    await bot.close()  # must not raise

    assert session.close_calls == 1


async def test_close_is_safe_with_a_real_unconfigured_lavalink_client():
    """End-to-end: a bot that never configured Lavalink closes cleanly.

    Uses the REAL ``sonolink.Client`` (installed in this venv) with zero
    nodes, rather than the fake, so ``Client.close()``'s own "skip nodes that
    are neither connected nor connecting" guard is exercised for real.
    """

    class _Pool:
        async def execute(self, *a, **k):
            return "ok"

        async def fetch(self, *a, **k):
            return []

        async def fetchrow(self, *a, **k):
            return None

        async def fetchval(self, *a, **k):
            return None

    bot = core.Yasuho(db_pool=_Pool())
    bot.http_session = None

    await bot.close()  # must not raise

    assert bot.sl_client.nodes == []


# ---------------------------------------------------------------------------
# cogs/music/music.py: the restore trigger
# ---------------------------------------------------------------------------


class _FakeSlClientForRestore:
    def __init__(self, nodes=None):
        self.nodes = nodes or []


class _FakeMusicBot:
    def __init__(self, ready, nodes=None):
        self._ready = ready
        self.sl_client = _FakeSlClientForRestore(nodes)
        self.db_pool = None

    def is_ready(self):
        return self._ready


class _FakeReadyEvent:
    def __init__(self, session_id="session-abc"):
        self.session_id = session_id


def _make_cog(bot):
    cog = music.Music.__new__(music.Music)
    cog.bot = bot
    cog._restored = False
    cog.restore_calls = 0

    async def _fake_restore_players():
        cog.restore_calls += 1

    cog._restore_players = _fake_restore_players
    return cog


@pytest.fixture(autouse=True)
def _stub_save_session(monkeypatch):
    """The session-save diagnostics write is not what these tests are about."""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(music.music_state, "save_session", _noop)


async def test_node_ready_after_on_ready_restores_exactly_once():
    bot = _FakeMusicBot(ready=True, nodes=[])
    cog = _make_cog(bot)

    await cog.on_ready()
    assert cog.restore_calls == 0, "no node yet - must wait"

    bot.sl_client.nodes.append(types.SimpleNamespace(is_connected=True))
    await cog.on_sonolink_node_ready(_FakeReadyEvent())

    assert cog.restore_calls == 1
    assert cog._restored is True


async def test_on_ready_after_node_ready_restores_exactly_once():
    bot = _FakeMusicBot(ready=True, nodes=[types.SimpleNamespace(is_connected=True)])
    cog = _make_cog(bot)

    await cog.on_sonolink_node_ready(_FakeReadyEvent())
    assert cog.restore_calls == 1

    await cog.on_ready()  # a later reconnect-fired on_ready

    assert cog.restore_calls == 1, "the flag must block the second trigger"


async def test_both_triggers_at_once_restore_exactly_once():
    bot = _FakeMusicBot(ready=True, nodes=[types.SimpleNamespace(is_connected=True)])
    cog = _make_cog(bot)

    await asyncio.gather(
        cog.on_ready(),
        cog.on_sonolink_node_ready(_FakeReadyEvent()),
    )

    assert cog.restore_calls == 1


async def test_node_ready_before_the_bot_is_ready_does_not_restore_yet():
    bot = _FakeMusicBot(ready=False, nodes=[types.SimpleNamespace(is_connected=True)])
    cog = _make_cog(bot)

    await cog.on_sonolink_node_ready(_FakeReadyEvent())

    assert cog.restore_calls == 0
    assert cog._restored is False, "on_ready must still get a chance to run it"

    bot._ready = True
    await cog.on_ready()

    assert cog.restore_calls == 1

"""Integration tests for the hardened top.gg webhook app.

These drive ``cogs.system.webstats.build_webhook_app`` through a real aiohttp
test client (TestServer + TestClient), so the middleware, the app-level body
cap, and the byte-equivalent vote handler are all exercised over HTTP. No
Discord, DB, or real network egress - the test server binds loopback.

The test client always connects from 127.0.0.1, so the per-IP throttle here
behaves as a single-source throttle; per-key isolation is covered exhaustively
in ``tests/tools/test_rate_limit.py``.
"""

from aiohttp.test_utils import TestClient, TestServer

from cogs.system import webstats
from cogs.system.webstats import MAX_BODY_BYTES, build_webhook_app
from tools.rate_limit import FixedWindowRateLimiter

SECRET = "s3cret-password"


def _make_app(*, limit=100):
    """Build the app plus a recorder for dispatched events."""
    dispatched = []
    limiter = FixedWindowRateLimiter(limit=limit, window=60.0, capacity=64)
    app = build_webhook_app(SECRET, lambda *a: dispatched.append(a), limiter)
    return app, dispatched


async def _client(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_valid_vote_is_byte_equivalent_and_dispatches():
    app, dispatched = _make_app()
    client = await _client(app)
    try:
        resp = await client.post(
            "/dblwebhook",
            json={"type": "test", "user": "123"},
            headers={"Authorization": SECRET},
        )
        assert resp.status == 200
        assert await resp.text() == "OK"
        # Dispatched exactly the stock topgg event with a BotVoteData payload.
        assert len(dispatched) == 1
        event, data = dispatched[0]
        assert event == "dbl_vote"
        assert data["type"] == "test"
        assert data["user"] == "123"
    finally:
        await client.close()


async def test_wrong_secret_is_401_and_does_not_dispatch():
    app, dispatched = _make_app()
    client = await _client(app)
    try:
        resp = await client.post(
            "/dblwebhook",
            json={"type": "test"},
            headers={"Authorization": "wrong"},
        )
        assert resp.status == 401
        assert await resp.text() == "Unauthorized"
        assert dispatched == []
    finally:
        await client.close()


async def test_missing_auth_is_401():
    app, dispatched = _make_app()
    client = await _client(app)
    try:
        resp = await client.post("/dblwebhook", json={"type": "test"})
        assert resp.status == 401
        assert dispatched == []
    finally:
        await client.close()


async def test_unknown_path_is_terse_404():
    app, _ = _make_app()
    client = await _client(app)
    try:
        resp = await client.get("/wp-login.php")
        assert resp.status == 404
        body = await resp.text()
        # Terse status line, not a stack trace or an app internals dump.
        assert len(body) < 100
        assert "Traceback" not in body
    finally:
        await client.close()


async def test_wrong_method_on_route_is_405():
    app, _ = _make_app()
    client = await _client(app)
    try:
        resp = await client.get("/dblwebhook")
        assert resp.status == 405
    finally:
        await client.close()


async def test_oversized_content_length_is_rejected_413():
    app, dispatched = _make_app()
    client = await _client(app)
    try:
        big = b"x" * (MAX_BODY_BYTES + 1)
        resp = await client.post(
            "/dblwebhook", data=big, headers={"Authorization": SECRET},
        )
        assert resp.status == 413
        assert dispatched == []
    finally:
        await client.close()


async def test_body_cap_enforced_without_content_length():
    """Chunked bodies (no Content-Length) are still capped by client_max_size."""
    app, dispatched = _make_app()
    client = await _client(app)

    async def _stream():
        yield b"x" * (MAX_BODY_BYTES + 1)

    try:
        resp = await client.post(
            "/dblwebhook", data=_stream(), headers={"Authorization": SECRET},
        )
        assert resp.status == 413
        assert dispatched == []
    finally:
        await client.close()


async def test_app_is_configured_with_body_cap():
    app, _ = _make_app()
    # The real enforcement is the app-level client_max_size; assert it is wired.
    assert app._client_max_size == MAX_BODY_BYTES


async def test_rate_limit_returns_429_after_threshold():
    app, _ = _make_app(limit=3)
    client = await _client(app)
    try:
        # First 3 pass the throttle (they get 401 from the handler - wrong auth,
        # but they were allowed through the middleware).
        for _ in range(3):
            resp = await client.post("/dblwebhook", json={"type": "test"})
            assert resp.status == 401
        # 4th from the same source is throttled before reaching the handler.
        resp = await client.post("/dblwebhook", json={"type": "test"})
        assert resp.status == 429
        assert await resp.text() == "Too Many Requests"
    finally:
        await client.close()


async def test_malformed_json_on_authed_path_is_terse_400():
    app, dispatched = _make_app()
    client = await _client(app)
    try:
        resp = await client.post(
            "/dblwebhook",
            data=b"not json",
            headers={"Authorization": SECRET, "Content-Type": "application/json"},
        )
        assert resp.status == 400
        body = await resp.text()
        assert "Traceback" not in body
        assert dispatched == []
    finally:
        await client.close()


def test_module_constants_are_sane():
    # Guard the hardening bounds against accidental drift.
    assert webstats.MAX_BODY_BYTES == 64 * 1024
    assert webstats.RATE_LIMIT >= 1
    assert webstats.RATE_WINDOW > 0
    assert webstats.RATE_CAPACITY >= 1
    assert webstats.WEBHOOK_PORT == 55000
    assert webstats.WEBHOOK_ROUTE == "/dblwebhook"

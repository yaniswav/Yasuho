"""Integration tests for the hardened top.gg webhook app.

These drive ``cogs.system.webstats.build_webhook_app`` through a real aiohttp
test client (TestServer + TestClient), so the middleware, the app-level body
cap, and the byte-equivalent vote handler are all exercised over HTTP. No
Discord, DB, or real network egress - the test server binds loopback.

The test client always connects from 127.0.0.1, so the per-IP throttle here
behaves as a single-source throttle; per-key isolation is covered exhaustively
in ``tests/tools/test_rate_limit.py``.
"""

import logging
import sys

from aiohttp.http_exceptions import BadHttpMessage
from aiohttp.test_utils import TestClient, TestServer

from cogs.system import webstats
from cogs.system.webstats import (
    DEFAULT_WEBHOOK_HOST,
    MAX_BODY_BYTES,
    ScannerNoiseFilter,
    build_webhook_app,
    resolve_client_key,
    resolve_webhook_host,
)
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


async def test_non_ascii_password_accepts_correct_header_and_rejects_wrong():
    # hmac.compare_digest raises TypeError on two str objects when either one
    # contains a non-ASCII character. The middleware's catch-all used to turn
    # that into a silent 400 - the vote never credited, with nothing but a
    # debug line saying why. Comparing as bytes must accept the right header,
    # reject a wrong one, and never raise either way.
    non_ascii_secret = "s3cret-mot-de-passe-éé"
    limiter = FixedWindowRateLimiter(limit=100, window=60.0, capacity=64)
    dispatched = []
    app = build_webhook_app(
        non_ascii_secret, lambda *a: dispatched.append(a), limiter
    )
    client = await _client(app)
    try:
        resp = await client.post(
            "/dblwebhook",
            json={"type": "test", "user": "123"},
            headers={"Authorization": non_ascii_secret},
        )
        assert resp.status == 200
        assert len(dispatched) == 1

        resp = await client.post(
            "/dblwebhook",
            json={"type": "test"},
            headers={"Authorization": "wrong"},
        )
        assert resp.status == 401
        assert len(dispatched) == 1
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


# --- resolve_webhook_host (W1: [Webhook] host) -------------------------------

def test_webhook_host_defaults_to_0000_when_key_absent():
    assert resolve_webhook_host(None) == "0.0.0.0" == DEFAULT_WEBHOOK_HOST


def test_webhook_host_accepts_configured_loopback():
    assert resolve_webhook_host("127.0.0.1") == "127.0.0.1"


def test_webhook_host_accepts_quoted_value():
    # bot.ini string values may be quoted; ConfigLoader._unquote strips one
    # matching pair before validation, same as every other string key.
    assert resolve_webhook_host('"127.0.0.1"') == "127.0.0.1"


def test_webhook_host_falls_back_and_warns_on_invalid_value(caplog):
    with caplog.at_level(logging.WARNING, logger=webstats.log.name):
        host = resolve_webhook_host("not-an-ip")
    assert host == DEFAULT_WEBHOOK_HOST
    assert any("invalid" in r.message.lower() for r in caplog.records)


def test_webhook_host_falls_back_silently_only_when_absent(caplog):
    with caplog.at_level(logging.WARNING, logger=webstats.log.name):
        host = resolve_webhook_host(None)
    assert host == DEFAULT_WEBHOOK_HOST
    assert caplog.records == []


# --- resolve_client_key (W1: rate-limiter key behind the proxy) -------------

def test_client_key_loopback_peer_with_xff_uses_the_entry_apache_appended():
    key = resolve_client_key("127.0.0.1", "203.0.113.5")
    assert key == "203.0.113.5"


def test_client_key_ignores_a_client_supplied_xff_prefix():
    """A client may send its own X-Forwarded-For; Apache appends the address
    it really saw AFTER it. Keying on the left would let a flood rotate forged
    addresses and dodge the limiter: only the right-most entry counts."""
    key = resolve_client_key("127.0.0.1", "1.2.3.4, 203.0.113.5")
    assert key == "203.0.113.5"


def test_client_key_loopback_peer_with_ipv6_loopback_xff():
    key = resolve_client_key("::1", "203.0.113.5")
    assert key == "203.0.113.5"


def test_client_key_loopback_peer_without_xff_falls_back_to_remote():
    assert resolve_client_key("127.0.0.1", None) == "127.0.0.1"


def test_client_key_non_loopback_peer_ignores_spoofed_xff():
    # A direct internet client controls its own X-Forwarded-For; trusting it
    # would let it pin its flood onto an arbitrary victim IP.
    key = resolve_client_key("198.51.100.9", "203.0.113.5")
    assert key == "198.51.100.9"


def test_client_key_loopback_peer_with_invalid_xff_falls_back_to_remote():
    key = resolve_client_key("127.0.0.1", "not-an-ip")
    assert key == "127.0.0.1"


def test_client_key_loopback_peer_with_empty_xff_falls_back_to_remote():
    assert resolve_client_key("127.0.0.1", "") == "127.0.0.1"


# --- ScannerNoiseFilter (W1: scanner noise) ----------------------------------

def _make_record(exc_info, level=logging.ERROR):
    try:
        raise exc_info
    except Exception:
        record = logging.LogRecord(
            name="aiohttp.server.yasuho_webhook", level=level, pathname=__file__,
            lineno=1, msg="Error handling request from %s", args=("1.2.3.4",),
            exc_info=sys.exc_info(),
        )
    return record


def test_scanner_noise_filter_demotes_bad_http_message():
    record = _make_record(BadHttpMessage("garbage"))
    assert record.levelno == logging.ERROR
    ScannerNoiseFilter().filter(record)
    assert record.levelno == logging.INFO
    assert record.levelname == "INFO"


def test_scanner_noise_filter_leaves_generic_exception_at_error():
    record = _make_record(ValueError("a real bug in our own handler"))
    ScannerNoiseFilter().filter(record)
    assert record.levelno == logging.ERROR
    assert record.levelname == "ERROR"


def test_scanner_noise_filter_always_returns_true():
    # A logging.Filter returning False would drop the record entirely; this
    # filter only ever demotes the level, never silences anything.
    record = _make_record(BadHttpMessage("garbage"))
    assert ScannerNoiseFilter().filter(record) is True


# --- negative control: spoof-trust regression would be caught --------------
#
# This is a diff-of-the-fix control, not a standing test: it is run manually
# against a deliberately broken copy of webstats.py (XFF trusted from ANY
# peer, not just loopback) to prove test_client_key_non_loopback_peer_
# ignores_spoofed_xff actually fails without the loopback guard. See the
# task report for the transcript; left here as documentation only.

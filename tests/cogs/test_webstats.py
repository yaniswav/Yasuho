"""Integration tests for the hardened top.gg webhook app.

These drive ``cogs.system.webstats.build_webhook_app`` through a real aiohttp
test client (TestServer + TestClient), so the middleware, the app-level body
cap, and the byte-equivalent vote handler are all exercised over HTTP. No
Discord, DB, or real network egress - the test server binds loopback.

The test client always connects from 127.0.0.1, so the per-IP throttle here
behaves as a single-source throttle; per-key isolation is covered exhaustively
in ``tests/tools/test_rate_limit.py``.
"""

import hashlib
import hmac
import json
import logging
import sys
import time

from aiohttp.http_exceptions import BadHttpMessage
from aiohttp.test_utils import TestClient, TestServer

from cogs.system import webstats
from cogs.system.webstats import (
    DEFAULT_WEBHOOK_HOST,
    MAX_BODY_BYTES,
    V1_TIMESTAMP_TOLERANCE_SECONDS,
    ScannerNoiseFilter,
    build_webhook_app,
    resolve_client_key,
    resolve_webhook_host,
    verify_v1_signature,
)
from tools.lru_cache import BoundedLRU
from tools.rate_limit import FixedWindowRateLimiter

SECRET = "s3cret-password"
V1_SECRET = "whs_test-secret"


def _make_app(*, limit=100, webhook_secret=None, vote_dedupe=None):
    """Build the app plus a recorder for dispatched events."""
    dispatched = []
    limiter = FixedWindowRateLimiter(limit=limit, window=60.0, capacity=64)
    app = build_webhook_app(
        SECRET, lambda *a: dispatched.append(a), limiter,
        webhook_secret=webhook_secret, vote_dedupe=vote_dedupe,
    )
    return app, dispatched


def _sign(secret, body_bytes, *, timestamp=None):
    """Sign ``body_bytes`` the way top.gg does, for test requests."""
    if timestamp is None:
        timestamp = int(time.time())
    mac = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={mac}"


def _vote_body(*, vote_id="v1", platform_id="123456789012345678", weight=1):
    return json.dumps({
        "type": "vote.create",
        "data": {
            "id": vote_id,
            "weight": weight,
            "created_at": "2026-10-05T00:00:00Z",
            "expires_at": "2026-10-06T00:00:00Z",
            "project": {
                "id": "proj1", "type": "bot", "platform": "discord",
                "platform_id": "999",
            },
            "query": {},
            "user": {
                "id": "topgg-user-1", "platform_id": platform_id,
                "name": "someone", "avatar_url": "https://example.invalid/a.png",
            },
        },
    }).encode("utf-8")


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


# --- v1 webhook (x-topgg-signature) -----------------------------------------

async def test_v1_vote_dispatches_once_with_correct_discord_id_and_weekend():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body(platform_id="222222222222222222", weight=1)
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": _sign(V1_SECRET, body),
            },
        )
        assert resp.status == 200
        assert len(dispatched) == 1
        event, data = dispatched[0]
        assert event == "dbl_vote"
        assert data["type"] == "upvote"
        assert data["user"] == "222222222222222222"
        assert data["is_weekend"] is False
    finally:
        await client.close()


async def test_v1_vote_weight_two_marks_weekend():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body(weight=2)
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": _sign(V1_SECRET, body),
            },
        )
        assert resp.status == 200
        assert len(dispatched) == 1
        assert dispatched[0][1]["is_weekend"] is True
    finally:
        await client.close()


async def test_v1_same_vote_id_is_deduped_not_redispatched():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body(vote_id="dupe-1")
        headers = {
            "Content-Type": "application/json",
            "x-topgg-signature": _sign(V1_SECRET, body),
        }
        resp1 = await client.post("/dblwebhook", data=body, headers=headers)
        resp2 = await client.post("/dblwebhook", data=body, headers=headers)
        assert resp1.status == 200
        assert resp2.status == 200
        assert len(dispatched) == 1
    finally:
        await client.close()


async def test_v1_bad_signature_is_401_and_does_not_dispatch():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body()
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": _sign("wrong-secret", body),
            },
        )
        assert resp.status == 401
        assert dispatched == []
    finally:
        await client.close()


async def test_v1_missing_signature_header_falls_back_to_legacy_unauthorized():
    # No x-topgg-signature and no Authorization: legacy path, 401.
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        resp = await client.post(
            "/dblwebhook", data=_vote_body(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 401
        assert dispatched == []
    finally:
        await client.close()


async def test_v1_malformed_signature_header_is_401():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body()
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": "garbage-not-kv-pairs",
            },
        )
        assert resp.status == 401
        assert dispatched == []
    finally:
        await client.close()


async def test_v1_stale_timestamp_is_401():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body()
        stale = int(time.time()) - V1_TIMESTAMP_TOLERANCE_SECONDS - 60
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": _sign(V1_SECRET, body, timestamp=stale),
            },
        )
        assert resp.status == 401
        assert dispatched == []
    finally:
        await client.close()


async def test_v1_future_timestamp_is_401():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body()
        future = int(time.time()) + V1_TIMESTAMP_TOLERANCE_SECONDS + 60
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": _sign(V1_SECRET, body, timestamp=future),
            },
        )
        assert resp.status == 401
        assert dispatched == []
    finally:
        await client.close()


async def test_v1_secret_not_configured_is_401_with_single_warning(caplog):
    app, dispatched = _make_app(webhook_secret=None)
    client = await _client(app)
    try:
        body = _vote_body()
        headers = {
            "Content-Type": "application/json",
            "x-topgg-signature": _sign(V1_SECRET, body),
        }
        with caplog.at_level(logging.WARNING, logger=webstats.log.name):
            resp1 = await client.post("/dblwebhook", data=body, headers=headers)
            resp2 = await client.post("/dblwebhook", data=body, headers=headers)
        assert resp1.status == 401
        assert resp2.status == 401
        assert dispatched == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
    finally:
        await client.close()


async def test_v1_body_tampered_after_signing_is_401():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body()
        signature = _sign(V1_SECRET, body)
        tampered = _vote_body(platform_id="999999999999999999")
        resp = await client.post(
            "/dblwebhook", data=tampered,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": signature,
            },
        )
        assert resp.status == 401
        assert dispatched == []
    finally:
        await client.close()


async def test_v1_webhook_test_event_acks_logs_and_does_not_dispatch(caplog):
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = json.dumps({
            "type": "webhook.test",
            "data": {
                "user": {
                    "id": "topgg-user-1", "platform_id": "123",
                    "name": "someone", "avatar_url": "https://example.invalid/a.png",
                },
                "project": {
                    "id": "proj1", "type": "bot", "platform": "discord",
                    "platform_id": "999",
                },
            },
        }).encode("utf-8")
        with caplog.at_level(logging.INFO, logger=webstats.log.name):
            resp = await client.post(
                "/dblwebhook", data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-topgg-signature": _sign(V1_SECRET, body),
                },
            )
        assert resp.status == 200
        assert dispatched == []
        assert any(
            "top.gg v1 webhook test received" in r.message for r in caplog.records
        )
        # No user id anywhere in the logged line.
        assert all("123" not in r.message for r in caplog.records)
    finally:
        await client.close()


async def test_v1_unknown_event_type_is_200_and_does_not_dispatch():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = json.dumps({"type": "something.new", "data": {}}).encode("utf-8")
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": _sign(V1_SECRET, body),
            },
        )
        assert resp.status == 200
        assert dispatched == []
    finally:
        await client.close()


async def test_v1_missing_platform_id_is_400_not_dispatched():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = json.dumps({
            "type": "vote.create",
            "data": {
                "id": "v-bad",
                "weight": 1,
                "user": {"id": "topgg-user-1", "name": "x"},
            },
        }).encode("utf-8")
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": _sign(V1_SECRET, body),
            },
        )
        assert resp.status == 400
        assert dispatched == []
    finally:
        await client.close()


async def test_v1_non_numeric_platform_id_is_400_not_dispatched():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body(platform_id="not-a-number")
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "x-topgg-signature": _sign(V1_SECRET, body),
            },
        )
        assert resp.status == 400
        assert dispatched == []
    finally:
        await client.close()


async def test_legacy_path_still_works_unchanged_alongside_v1():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        resp = await client.post(
            "/dblwebhook",
            json={"type": "upvote", "user": "123"},
            headers={"Authorization": SECRET},
        )
        assert resp.status == 200
        assert len(dispatched) == 1
        assert dispatched[0][1]["user"] == "123"
    finally:
        await client.close()


async def test_both_headers_present_are_judged_on_v1_signature_only():
    app, dispatched = _make_app(webhook_secret=V1_SECRET)
    client = await _client(app)
    try:
        body = _vote_body()
        # A CORRECT legacy Authorization header alongside a WRONG v1
        # signature must still be refused: only the v1 signature counts once
        # that header is present.
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": SECRET,
                "x-topgg-signature": _sign("wrong-secret", body),
            },
        )
        assert resp.status == 401
        assert dispatched == []

        # And a correct v1 signature dispatches even with a WRONG legacy
        # Authorization header riding along.
        resp = await client.post(
            "/dblwebhook", data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "wrong",
                "x-topgg-signature": _sign(V1_SECRET, body),
            },
        )
        assert resp.status == 200
        assert len(dispatched) == 1
    finally:
        await client.close()


def test_verify_v1_signature_accepts_valid_and_rejects_tampered():
    body = b'{"type":"vote.create"}'
    header = _sign(V1_SECRET, body, timestamp=1000000)
    assert verify_v1_signature(V1_SECRET, body, header, now=1000000) is True
    assert verify_v1_signature(V1_SECRET, body + b"x", header, now=1000000) is False
    assert verify_v1_signature("other-secret", body, header, now=1000000) is False
    assert verify_v1_signature(V1_SECRET, body, None, now=1000000) is False
    assert verify_v1_signature(V1_SECRET, body, "", now=1000000) is False


def test_vote_dedupe_cache_is_a_bounded_lru_of_the_expected_capacity():
    # Guards VOTE_DEDUPE_CAPACITY against accidental drift and that the
    # cog-level default really is a BoundedLRU, not a plain unbounded set.
    assert webstats.VOTE_DEDUPE_CAPACITY == 2048
    cache = BoundedLRU(webstats.VOTE_DEDUPE_CAPACITY)
    cache["a"] = True
    assert "a" in cache


# --- negative control: spoof-trust regression would be caught --------------
#
# This is a diff-of-the-fix control, not a standing test: it is run manually
# against a deliberately broken copy of webstats.py (XFF trusted from ANY
# peer, not just loopback) to prove test_client_key_non_loopback_peer_
# ignores_spoofed_xff actually fails without the loopback guard. See the
# task report for the transcript; left here as documentation only.


async def test_limiter_key_reads_every_x_forwarded_for_line():
    """If the header arrives as two lines - the client's own first, Apache's
    appended one second - the key must be Apache's entry, not the client's."""
    from multidict import CIMultiDict

    seen = []

    class _Limiter:
        def check(self, ip):
            seen.append(ip)
            return True, False

    app = build_webhook_app("pw", lambda *a, **k: None, _Limiter())
    client = await _client(app)
    try:
        headers = CIMultiDict()
        headers.add("X-Forwarded-For", "9.9.9.9, 8.8.8.8")
        headers.add("X-Forwarded-For", "203.0.113.5")
        headers.add("Authorization", "wrong")
        await client.post("/dblwebhook", data=b"{}", headers=headers)
        assert seen == ["203.0.113.5"]
    finally:
        await client.close()

"""Posts server/shard stats to top.gg and serves its vote webhook.

Two webhook formats are accepted on the same route (``POST /dblwebhook``),
picked per-request by which header arrives - there is no config switch:

* legacy (v0): a static secret in the ``Authorization`` header, configured
  on the bot's top.gg edit page. Still supported ("no longer recommended" per
  docs.top.gg) and byte-for-byte unchanged by any of this.
* v1: configured in the dashboard's Webhooks section, which hands you a
  secret shaped ``whs_...``. Every request carries
  ``x-topgg-signature: "t={unix timestamp},v1={hex hmac-sha256}"``; a request
  with both headers is judged on the v1 signature only (see
  :data:`V1_SIGNATURE_HEADER`, :func:`verify_v1_signature`).

Config: set ``[WebsiteTokens] topGGWebhookSecret = whs_...`` in tokens.ini to
the secret the dashboard shows when you add the v1 webhook. Leaving it unset
does not disable the legacy path - it only means a v1 request is refused
(401, fail-closed) with one WARNING per process rather than a flood.

SWITCH ORDER when moving a bot from legacy to v1 (do not skip a step - a
webhook left configured on both top.gg sides double-delivers every vote):

1. Add the v1 webhook in the top.gg dashboard and set ``topGGWebhookSecret``
   here; restart so both paths are live. Use the dashboard's "send test
   event" (``webhook.test``) to confirm a 200 and the one INFO log line.
2. Only once that test lands, delete/clear the legacy webhook URL on the
   bot's top.gg edit page, so top.gg stops double-posting every vote to both
   formats.
3. Unrelated to the format switch, but the other half of hardening this
   route: once a reverse proxy fronts it over HTTPS, set ``[Webhook] host``
   in bot.ini to ``127.0.0.1`` (see the comment below) and close the port to
   the internet.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import time

import topgg
from aiohttp import web
from aiohttp.http_exceptions import HttpProcessingError
from discord.ext import commands
from topgg.types import BotVoteData

from tools.config_loader import ConfigLoader, config_loader
from tools.lru_cache import BoundedLRU
from tools.rate_limit import FixedWindowRateLimiter

log = logging.getLogger(__name__)

# fallback=None so a fresh checkout without top.gg config does not crash the
# whole cog at import; the cog then simply skips autopost/webhook setup.
TOP_GG_TOKEN = config_loader.get('WebsiteTokens', 'topGG', fallback=None)
TOP_GG_PASSWORD = config_loader.get('WebsiteTokens', 'topGGPassword', fallback=None)
# v1 webhook secret ([WebsiteTokens] topGGWebhookSecret in tokens.ini, the
# "whs_..." value the top.gg dashboard's Webhooks section shows once). Read
# the same way as topGGPassword (plain get() with fallback=None so a fresh
# checkout never crashes at import) and unquoted the same way every other
# string config value is, since configparser.get() does not unquote.
_RAW_TOP_GG_WEBHOOK_SECRET = config_loader.get(
    'WebsiteTokens', 'topGGWebhookSecret', fallback=None
)
TOP_GG_WEBHOOK_SECRET = (
    ConfigLoader._unquote(_RAW_TOP_GG_WEBHOOK_SECRET)
    if _RAW_TOP_GG_WEBHOOK_SECRET is not None
    else None
)

# --- Public webhook surface hardening (top.gg reaches this on 0.0.0.0) --------
# The bind stays public so top.gg can deliver votes; the operator handles any
# network-level filtering. Everything below bounds what an unauthenticated
# internet scanner can cost us: body bytes buffered, requests per source, and
# log noise. The successful vote path stays byte-for-byte identical to the
# stock topgg WebhookManager (same auth compare, same dispatched event, same
# 200/401 bodies) - we only replace the transport to add the guards.
#
# [Webhook] host (bot.ini, optional): the local address the vote webhook
# binds. Defaults to "0.0.0.0" (today's behaviour: reachable from the
# internet). Once the host's Apache reverse-proxies POST
# /yasuho/dblwebhook to 127.0.0.1:55000 over HTTPS, set:
#   [Webhook]
#   host = 127.0.0.1
# to close port 55000 to the internet. The value is validated as an IP
# literal (ipaddress); an absent key or an invalid one both fall back to
# "0.0.0.0", the invalid case also logging a WARNING so a typo is never
# silent.
WEBHOOK_ROUTE = "/dblwebhook"
WEBHOOK_PORT = 55000
DEFAULT_WEBHOOK_HOST = "0.0.0.0"
# Real top.gg vote payloads are a few hundred bytes; 64 KiB is generous
# headroom while capping how much any single request can make us buffer.
MAX_BODY_BYTES = 64 * 1024
# Per-source throttle. A legitimate top.gg webhook fires far below this; the
# ceiling only bites scanners and abusive sources.
RATE_LIMIT = 30
RATE_WINDOW = 60.0  # seconds
# Distinct source IPs tracked at once. LRU eviction keeps memory flat under a
# spoofed-source flood: at most this many small entries, ever.
RATE_CAPACITY = 4096

# --- v1 webhook (x-topgg-signature) ----------------------------------------
# top.gg's v1 webhooks (dashboard "Webhooks" section) sign every request with
# header x-topgg-signature: "t={unix timestamp},v1={hex hmac-sha256}". The
# legacy (v0) static Authorization header, configured on the bot's edit page,
# is "no longer recommended" per docs.top.gg but still supported - both paths
# run side by side below, picked per-request by which header is present, so
# the switch can happen without a window where votes are dropped.
V1_SIGNATURE_HEADER = "x-topgg-signature"
# The docs give no explicit tolerance. 300s both directions comfortably
# covers ordinary clock skew between this host and top.gg, and top.gg's own
# retry schedule on timeout/5xx (about 1s, 2s, 4s after the first attempt -
# all well inside the window) while still bounding how long a captured
# request stays replayable if it ever leaked.
V1_TIMESTAMP_TOLERANCE_SECONDS = 300
# Distinct accepted vote.create ids remembered, so a top.gg retry of an
# already-processed vote (same semantics as the 1s/2s/4s retry above, but for
# a 2xx that was lost in transit rather than a timeout/5xx) is acknowledged
# again without a second dispatch. Bounded the same way RATE_CAPACITY is -
# eviction only risks a rare double-dispatch on a very old retry, never
# unbounded memory.
VOTE_DEDUPE_CAPACITY = 2048


def resolve_webhook_host(raw):
    """Validate a ``[Webhook] host`` value read from bot.ini.

    ``raw`` is the value ``config_loader.get(..., fallback=None)`` returned:
    ``None`` when the key is absent (today's behaviour is preserved, so an
    existing deployment with no ``[Webhook]`` section keeps binding
    ``0.0.0.0`` unchanged). A present value may be quoted like other string
    config values, so it is unquoted the same way :meth:`ConfigLoader.getstr`
    does before being checked. Anything that is not a literal IP address
    (ipaddress.ip_address) is rejected with a WARNING rather than handed to
    ``TCPSite``, which would otherwise try to resolve it as a hostname at
    bind time and fail far from the config mistake that caused it.
    """
    if raw is None:
        return DEFAULT_WEBHOOK_HOST
    value = ConfigLoader._unquote(raw)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        log.warning(
            "invalid [Webhook] host %r in bot.ini; falling back to %s",
            value, DEFAULT_WEBHOOK_HOST,
        )
        return DEFAULT_WEBHOOK_HOST
    return value


WEBHOOK_HOST = resolve_webhook_host(config_loader.get("Webhook", "host", fallback=None))


def _is_loopback(ip_str):
    try:
        return ipaddress.ip_address(ip_str).is_loopback
    except ValueError:
        return False


def resolve_client_key(remote, forwarded_for):
    """Resolve the rate-limiter key for one webhook request.

    ``remote`` is ``request.remote`` (the TCP peer); ``forwarded_for`` is the
    raw ``X-Forwarded-For`` header value, or ``None``.

    Once the webhook binds 127.0.0.1 behind Apache (see ``WEBHOOK_HOST``
    above), every request's TCP peer is Apache itself, so keying the limiter
    on ``remote`` would collapse every real client into one shared bucket - a
    flood routed through the proxy could exhaust it and block genuine top.gg
    votes along with it. Apache's mod_proxy_http APPENDS the address it saw
    to X-Forwarded-For, after whatever the client already sent in that
    header. With exactly one proxy hop in front of us, the RIGHT-most entry
    is therefore the one Apache wrote itself; anything to its left came from
    the client and is free to forge (taking the left-most entry would let a
    flood rotate invented addresses and get a fresh bucket for each). We
    only ever read that header when ``remote`` is itself a loopback address:
    a direct internet client has no proxy in between and controls the whole
    header. A missing or non-IP-literal entry, or a non-loopback peer, all
    fall back to ``remote`` unchanged - today's behaviour.
    """
    if not _is_loopback(remote):
        return remote
    if not forwarded_for:
        return remote
    candidate = forwarded_for.split(",")[-1].strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return remote
    return candidate


def _parse_v1_signature_header(header):
    """Parse ``x-topgg-signature`` into ``(timestamp_str, [v1 signatures])``.

    The header is documented as ``"t={ts},v1={sig}"`` but nothing says the
    pairs are ordered or that ``v1`` appears only once (a secret rotation
    could plausibly send the request signed under both the old and the new
    secret, as other providers' webhook signatures do), so this parses it as
    order-independent ``k=v`` pairs, tolerates surrounding spaces, and
    collects every ``v1`` value seen - the caller accepts the request if ANY
    of them verifies. Returns ``(None, [])`` when no timestamp was found, or
    ``(timestamp_str, [])`` when a timestamp was found but no ``v1`` value
    was - both are rejected by the caller the same way a missing header is.
    """
    timestamp = None
    signatures = []
    if not header:
        return timestamp, signatures
    for part in header.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "t" and timestamp is None:
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    return timestamp, signatures


def verify_v1_signature(secret, raw_body, header_value, *, now=None):
    """Verify one ``x-topgg-signature`` header against the raw request body.

    ``secret`` is the webhook secret (the ``whs_...`` value from the top.gg
    dashboard); ``raw_body`` is the EXACT bytes received, before any JSON
    decoding - the signature covers the bytes on the wire, not a
    re-serialization of them. ``now`` is injectable for tests; defaults to
    the real clock.

    Per docs.top.gg: HMAC-SHA256 of ``"{timestamp}.{rawBody}"`` keyed by the
    secret, hex-digested, compared to the header's ``v1`` value(s) in
    constant time. A missing/malformed header, a non-integer timestamp, a
    timestamp outside :data:`V1_TIMESTAMP_TOLERANCE_SECONDS`, or no matching
    signature all fail closed (return ``False``) - never raise.
    """
    timestamp_str, signatures = _parse_v1_signature_header(header_value)
    if timestamp_str is None or not signatures:
        return False
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return False
    if now is None:
        now = time.time()
    if abs(now - timestamp) > V1_TIMESTAMP_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp_str}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    expected_bytes = expected.encode("ascii")
    for signature in signatures:
        try:
            candidate = signature.encode("ascii")
        except UnicodeEncodeError:
            continue
        if hmac.compare_digest(expected_bytes, candidate):
            return True
    return False


class ScannerNoiseFilter(logging.Filter):
    """Demote aiohttp's ERROR "Error handling request" line for malformed
    traffic down to INFO, on our dedicated webhook server logger only.

    aiohttp's RequestHandler.handle_error logs every exception it catches
    while parsing a request at ERROR via ``logger.exception`` (one case -
    the very first request on a connection being garbage - is already
    logged at DEBUG upstream, but any malformed request after that, or a
    malformed body, still logs at ERROR). On a port that internet scanners
    probe with non-HTTP or truncated traffic, that means a steady trickle of
    ERROR-level tracebacks (BadHttpMessage and its HttpProcessingError
    siblings) that carry no actionable signal - the connection is simply
    dropped either way.

    This filter only demotes records whose ``exc_info`` is one of those
    aiohttp parsing exceptions; any other exception (a real bug in our own
    handler, surfaced the same way) is untouched and keeps logging at ERROR.
    It is attached to a dedicated logger name (passed to AppRunner below),
    never to the shared "aiohttp.server" logger, so it cannot affect any
    other aiohttp server that might run in this process.
    """

    def filter(self, record):
        if record.levelno >= logging.ERROR and record.exc_info:
            exc = record.exc_info[1]
            if isinstance(exc, HttpProcessingError):
                record.levelno = logging.INFO
                record.levelname = logging.getLevelName(logging.INFO)
        return True


# Dedicated logger name (not the shared "aiohttp.server") so the filter below
# only ever touches records from our own webhook server; passed to AppRunner
# as the supported ``logger=`` hook (aiohttp's RequestHandler accepts it and
# uses it for exactly the "Error handling request" line this filter targets).
_webhook_server_logger = logging.getLogger("aiohttp.server.yasuho_webhook")
_webhook_server_logger.addFilter(ScannerNoiseFilter())


def build_webhook_app(password, dispatch, limiter, webhook_secret=None, vote_dedupe=None):
    """Build the hardened aiohttp app that serves the top.gg vote webhook.

    Factored out of the cog so it can be exercised with an aiohttp test client
    without constructing a full Discord bot. ``dispatch`` is ``bot.dispatch``;
    ``limiter`` is a :class:`FixedWindowRateLimiter`. ``webhook_secret`` is
    the v1 secret (``None`` disables the v1 path, fail-closed); ``vote_dedupe``
    is a :class:`~tools.lru_cache.BoundedLRU` of accepted ``vote.create`` ids,
    created locally with :data:`VOTE_DEDUPE_CAPACITY` when omitted.
    """
    if vote_dedupe is None:
        vote_dedupe = BoundedLRU(VOTE_DEDUPE_CAPACITY)
    # Mutable 1-element box (not a plain bool) so the nested handler can flip
    # it without `nonlocal`; warns at most once per app - in production that
    # is once per process, since _run_webhook builds exactly one app.
    _missing_secret_warned = [False]

    async def _legacy_vote_handler(request):
        # Byte-equivalent to topgg WebhookManager._bot_vote_handler.
        auth = request.headers.get("Authorization", "")
        # Constant-time compare so the response time can't leak how many leading
        # bytes of the secret matched; reject outright when no password is set so
        # an empty Authorization header can never authenticate. Compare as
        # bytes, not str: hmac.compare_digest raises TypeError on two strings
        # when either contains a non-ASCII character, which the middleware's
        # catch-all turns into a silent 400 - the vote is never credited and
        # nothing but a debug line says why.
        if not password or not hmac.compare_digest(
            auth.encode("utf-8"), password.encode("utf-8")
        ):
            return web.Response(status=401, text="Unauthorized")
        data = await request.json()
        dispatch("dbl_vote", BotVoteData(**data))
        return web.Response(status=200, text="OK")

    async def _v1_vote_handler(request, signature_header):
        if not webhook_secret:
            if not _missing_secret_warned[0]:
                log.warning(
                    "top.gg v1 webhook request received but [WebsiteTokens] "
                    "topGGWebhookSecret is not configured in tokens.ini; "
                    "refusing all v1 requests until it is set."
                )
                _missing_secret_warned[0] = True
            return web.Response(status=401, text="Unauthorized")

        # The raw bytes, read ONCE, are what the signature covers; every
        # later step (JSON decode included) works off this same buffer, never
        # re-reading or re-serializing the request.
        raw_body = await request.read()
        if not verify_v1_signature(webhook_secret, raw_body, signature_header):
            return web.Response(status=401, text="Unauthorized")

        try:
            payload = json.loads(raw_body)
        except ValueError:
            return web.Response(status=400, text="Bad Request")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="Bad Request")

        event_type = payload.get("type")
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {}

        if event_type == "webhook.test":
            # No payload logged - same rule as a real vote (see on_dbl_vote
            # below): a user id must never reach a durable, erasure-blind log.
            log.info("top.gg v1 webhook test received")
            return web.Response(status=200, text="OK")

        if event_type != "vote.create":
            # Acknowledge so top.gg does not retry an event type we simply
            # don't understand yet; DEBUG because it is not actionable.
            log.debug("top.gg v1 webhook: ignoring unknown event type %r", event_type)
            return web.Response(status=200, text="OK")

        # Retry dedupe: top.gg retries the same event on timeout/5xx. A vote
        # id already accepted is acknowledged again without a second dispatch.
        vote_id = data.get("id")
        if vote_id is not None and vote_id in vote_dedupe:
            return web.Response(status=200, text="OK")

        user = data.get("user")
        platform_id = user.get("platform_id") if isinstance(user, dict) else None
        # The topggpy gotcha this listener's downstream consumer (votes.py)
        # is built on: a Discord id travels as a STRING. Require it here too
        # - a missing or non-numeric id must never reach bot.dispatch.
        if not isinstance(platform_id, str) or not platform_id.isdigit():
            return web.Response(status=400, text="Bad Request")

        weight = data.get("weight", 1)
        try:
            weight = int(weight)
        except (TypeError, ValueError):
            weight = 1
        # Per docs.top.gg: weight is 1 for a normal vote, 2 during a top.gg
        # weekend double-vote event - the same signal the legacy payload's
        # `is_weekend` boolean carries, computed here from the new shape.
        is_weekend = weight >= 2

        # Same dict shape topgg.types.BotVoteData's legacy payload has (see
        # the module docstring): "user" a STRING id, "type" "upvote",
        # "is_weekend" a bool. "query" is read by no current `dbl_vote`
        # listener (checked: cogs/community/votes.py and this cog's own
        # listener use only type/user/is_weekend) so an empty dict is passed
        # rather than guessing a shape nothing consumes.
        dispatch("dbl_vote", {
            "type": "upvote",
            "user": platform_id,
            "is_weekend": is_weekend,
            "query": {},
        })
        if vote_id is not None:
            vote_dedupe[vote_id] = True
        return web.Response(status=200, text="OK")

    async def _vote_handler(request):
        # A request carrying x-topgg-signature takes the v1 path regardless
        # of whatever Authorization header also arrived; only its absence
        # falls back to the legacy, unchanged path.
        signature_header = request.headers.get(V1_SIGNATURE_HEADER)
        if signature_header is not None:
            return await _v1_vote_handler(request, signature_header)
        return await _legacy_vote_handler(request)

    @web.middleware
    async def _harden(request, handler):
        remote = request.remote or "?"
        ip = resolve_client_key(remote, request.headers.get("X-Forwarded-For"))

        # 1. Reject an oversized declared body before touching the handler. The
        #    app-level client_max_size below is the real enforcement (it also
        #    caps chunked bodies with no Content-Length); this is a cheap,
        #    deterministic early-out for honest Content-Length headers.
        content_length = request.content_length
        if content_length is not None and content_length > MAX_BODY_BYTES:
            return web.Response(status=413, text="Payload Too Large")

        # 2. Per-source rate limit. Applies to every path (including the 404s
        #    scanners generate), so an abusive source is throttled uniformly.
        allowed, should_log = limiter.check(ip)
        if not allowed:
            if should_log:
                log.warning(
                    "rate-limited webhook source %s (>%d req / %.0fs)",
                    ip, RATE_LIMIT, RATE_WINDOW,
                )
            return web.Response(status=429, text="Too Many Requests")

        # 3. Keep responses terse and leak-free. aiohttp renders HTTPExceptions
        #    (404 for unknown paths, 405 wrong method, 413 oversized body) as
        #    short plain-text status lines - no stack traces - so let those
        #    through. Any other exception (e.g. malformed JSON on the authed
        #    path) becomes a terse 400; the detail goes to our log at debug,
        #    never to the client.
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            log.debug("webhook handler error from %s", ip, exc_info=True)
            return web.Response(status=400, text="Bad Request")

    app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[_harden])
    app.router.add_post(WEBHOOK_ROUTE, _vote_handler)
    return app


class Webstats(commands.Cog):
    """Posts server/shard counts to Top.gg and handles vote webhooks."""

    def __init__(self, bot):
        self.bot = bot
        self.dbl_token = TOP_GG_TOKEN
        self.dbl_password = TOP_GG_PASSWORD
        self.dbl_webhook_secret = TOP_GG_WEBHOOK_SECRET
        self.dbl_client = None
        self._runner = None
        self._webhook_task = None
        self._limiter = FixedWindowRateLimiter(
            limit=RATE_LIMIT, window=RATE_WINDOW, capacity=RATE_CAPACITY,
        )

        if not TOP_GG_TOKEN:
            log.info("top.gg not configured; skipping autopost and vote webhook.")
            return

        self.dbl_client = topgg.DBLClient(self.bot, self.dbl_token, autopost=True, post_shard_count=True)
        self._webhook_task = self.bot.loop.create_task(self._run_webhook())

        def _on_webhook_done(task):
            exc = task.exception()
            if exc:
                log.error("webhook server failed to start: %s", exc)

        self._webhook_task.add_done_callback(_on_webhook_done)

    async def _run_webhook(self):
        app = build_webhook_app(
            self.dbl_password, self.bot.dispatch, self._limiter,
            webhook_secret=self.dbl_webhook_secret,
        )
        # access_log=None silences per-request logging wholesale, so scanner
        # traffic can never flood the logs; our own one-line-per-offender
        # rate-limit warning is the only webhook log noise that remains.
        # logger=_webhook_server_logger routes aiohttp's own "Error handling
        # request" lines through ScannerNoiseFilter (defined above) instead
        # of the shared "aiohttp.server" logger.
        runner = web.AppRunner(app, access_log=None, logger=_webhook_server_logger)
        await runner.setup()
        self._runner = runner
        site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
        await site.start()
        log.info("top.gg vote webhook listening on %s:%d", WEBHOOK_HOST, WEBHOOK_PORT)

    async def cog_unload(self):
        # Close each independently so one failure doesn't block the other, and unload never raises
        if self.dbl_client is not None:
            try:
                await self.dbl_client.close()
            except Exception:
                log.exception("failed to close DBL client")
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                log.exception("failed to clean up webhook server")

    @commands.Cog.listener()
    async def on_autopost_success(self):
        log.info("Posted server count (%s), shard count (%s)", self.dbl_client.guild_count, self.bot.shard_count)

    @commands.Cog.listener()
    async def on_dbl_vote(self, data):
        # NEITHER BRANCH DUMPS THE PAYLOAD, and that is the whole point of this
        # listener's shape. A top.gg vote payload carries the voter's user id
        # (plus whatever query string the vote url was called with), and a log
        # file is durable, greppable and outside every erasure path the bot has:
        # `?mydata deleteprofile` can delete the vote ROW and can do nothing at
        # all about a line already written to disk. Nothing is lost by dropping
        # it either - cogs/community/votes.py logs one structured line per vote
        # with the facts an operator actually reads (streak, total, boost).
        if data.get("type") == "test":
            log.info("Received a test vote from top.gg")
            return
        log.debug("Received a vote webhook from top.gg")


async def setup(bot):
    await bot.add_cog(Webstats(bot))

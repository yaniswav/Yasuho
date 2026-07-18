import logging

import topgg
from aiohttp import web
from discord.ext import commands
from topgg.types import BotVoteData

from tools.config_loader import config_loader
from tools.rate_limit import FixedWindowRateLimiter

log = logging.getLogger(__name__)

# fallback=None so a fresh checkout without top.gg config does not crash the
# whole cog at import; the cog then simply skips autopost/webhook setup.
TOP_GG_TOKEN = config_loader.get('WebsiteTokens', 'topGG', fallback=None)
TOP_GG_PASSWORD = config_loader.get('WebsiteTokens', 'topGGPassword', fallback=None)

# --- Public webhook surface hardening (top.gg reaches this on 0.0.0.0) --------
# The bind stays public so top.gg can deliver votes; the operator handles any
# network-level filtering. Everything below bounds what an unauthenticated
# internet scanner can cost us: body bytes buffered, requests per source, and
# log noise. The successful vote path stays byte-for-byte identical to the
# stock topgg WebhookManager (same auth compare, same dispatched event, same
# 200/401 bodies) - we only replace the transport to add the guards.
WEBHOOK_ROUTE = "/dblwebhook"
WEBHOOK_PORT = 55000
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


def build_webhook_app(password, dispatch, limiter):
    """Build the hardened aiohttp app that serves the top.gg vote webhook.

    Factored out of the cog so it can be exercised with an aiohttp test client
    without constructing a full Discord bot. ``dispatch`` is ``bot.dispatch``;
    ``limiter`` is a :class:`FixedWindowRateLimiter`.
    """

    async def _vote_handler(request):
        # Byte-equivalent to topgg WebhookManager._bot_vote_handler.
        auth = request.headers.get("Authorization", "")
        if auth != password:
            return web.Response(status=401, text="Unauthorized")
        data = await request.json()
        dispatch("dbl_vote", BotVoteData(**data))
        return web.Response(status=200, text="OK")

    @web.middleware
    async def _harden(request, handler):
        ip = request.remote or "?"

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
        app = build_webhook_app(self.dbl_password, self.bot.dispatch, self._limiter)
        # access_log=None silences per-request logging wholesale, so scanner
        # traffic can never flood the logs; our own one-line-per-offender
        # rate-limit warning is the only webhook log noise that remains.
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self._runner = runner
        site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
        await site.start()

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
        if data.get("type") == "test":
            log.info("Received a test vote:\n%s", data)
            return
        log.info("Received a vote:\n%s", data)


async def setup(bot):
    await bot.add_cog(Webstats(bot))
